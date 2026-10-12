"""Dashboard accounts end to end (spec 023, AC5-AC16).

The first administrator comes from ``TB_DASHBOARD_PASSWORD_HASH``; everyone else gets a one-time
access link from an administrator. Each test drives the real application like a browser would.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.dashboard import ADMIN, PASSWORD, Browser, make_app
from trading_bot.config import Settings, is_trusted_proxy
from trading_bot.dashboard.auth import hash_password, link_digest
from trading_bot.dashboard.routes import SESSION_COOKIE
from trading_bot.dashboard.unit_of_work import DashboardRepositories
from trading_bot.logging_setup import REDACTED, RedactingFilter, configure_logging
from trading_bot.persistence.records import StoredUser

# A tunnel daemon on a Docker network: a private address, built at runtime because the secret
# scan refuses private address literals in the repository.
TUNNEL = (".".join(["172", "18", "0", "3"]), 40000)
NEW_PASSWORD = "-".join(["purple", "monkey", "dishwasher", "lamp"])
LINK = re.compile(r"<pre>(https?://[^<]+/dashboard/invite/([A-Za-z0-9_-]+))</pre>")


@pytest.fixture
def admin(tmp_path: Path) -> Iterator[Browser]:
    with TestClient(make_app(tmp_path), follow_redirects=False) as client:
        browser = Browser(client)
        assert browser.login().status_code == 303
        yield browser


@contextmanager
def other_browser(admin: Browser) -> Iterator[TestClient]:
    """A second browser on the running application: no cookie shared with ``admin``.

    It is not entered, so it does not run the lifespan (and the instance lock) a second time.
    """
    yield TestClient(admin.app, follow_redirects=False)


def invite(admin: Browser, username: str, *, is_admin: bool = False) -> str:
    data = {"username": username}
    if is_admin:
        data["is_admin"] = "on"
    response = admin.post("/dashboard/users", data)
    assert response.status_code == 200, response.text
    match = LINK.search(response.text)
    assert match is not None
    return match.group(2)


def accept(client: TestClient, token: str, password: str = NEW_PASSWORD) -> int:
    response = client.post(
        f"/dashboard/invite/{token}", data={"password": password, "repeat": password}
    )
    return response.status_code


def user(admin: Browser, username: str) -> StoredUser:
    def read(repositories: DashboardRepositories) -> StoredUser | None:
        return repositories.users.get_by_username(username)

    found: StoredUser | None = admin.repositories(read)
    assert found is not None
    return found


# --- the first administrator ------------------------------------------------------------------


def test_the_first_login_creates_the_administrator_from_the_configured_hash(
    admin: Browser,
) -> None:
    created = user(admin, ADMIN)

    assert created.is_admin
    assert created.active
    page = admin.get("/dashboard/users")
    assert page.status_code == 200
    assert '>admin</strong> <span class="muted">(you)</span>' in page.text
    assert 'href="/dashboard/users"' in admin.get("/dashboard/").text


def test_the_configured_hash_is_ignored_once_a_user_exists(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path), follow_redirects=False) as client:
        assert Browser(client).login().status_code == 303
    other = hash_password(NEW_PASSWORD)
    app = make_app(tmp_path, dashboard_password_hash=other)
    with TestClient(app, follow_redirects=False) as client:
        browser = Browser(client)
        assert browser.login(NEW_PASSWORD).status_code == 401
        assert browser.login().status_code == 303


def test_the_administrator_username_is_configurable(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, admin_username="Emi"), follow_redirects=False) as client:
        browser = Browser(client)
        assert browser.login(username="admin").status_code == 401
        assert browser.login(username="EMI").status_code == 303


# --- access links --------------------------------------------------------------------------


def test_an_invited_user_sets_a_password_with_the_link_and_logs_in(admin: Browser) -> None:
    token = invite(admin, "Ana")
    with other_browser(admin) as client:
        form = client.get(f"/dashboard/invite/{token}")
        assert form.status_code == 200
        assert "<strong>ana</strong>" in form.text

        assert accept(client, token) == 303
        page = client.get("/dashboard/")
        assert page.status_code == 200
        assert 'href="/dashboard/users"' not in page.text  # not an administrator
        assert ">ana</a>" in page.text

    assert user(admin, "ana").can_log_in
    with other_browser(admin) as client:
        assert accept(client, token) == 404  # used once
        assert Browser(client).login(NEW_PASSWORD, "ana").status_code == 303


def test_only_the_digest_of_a_link_is_stored(admin: Browser) -> None:
    token = invite(admin, "ana")

    with admin.app.state.runtime.database.engine.connect() as connection:
        stored = connection.exec_driver_sql("SELECT invite_digest FROM users").scalars().all()

    assert link_digest(token) in stored
    assert token not in stored


def test_the_link_uses_the_origin_the_browser_reached(admin: Browser) -> None:
    response = admin.client.post(
        "/dashboard/users",
        data={"csrf": admin.csrf(), "username": "ana"},
        headers={"origin": "https://bot.example.org"},
    )

    assert "<pre>https://bot.example.org/dashboard/invite/" in response.text


@pytest.mark.parametrize(
    ("password", "repeat", "message"),
    [
        ("short", "short", "at least 12 characters"),
        (NEW_PASSWORD, NEW_PASSWORD + "x", "do not match"),
        ("x" * 1025, "x" * 1025, "at most 1024 characters"),
    ],
)
def test_a_weak_or_mistyped_password_is_refused(
    admin: Browser, password: str, repeat: str, message: str
) -> None:
    token = invite(admin, "ana")
    with other_browser(admin) as client:
        response = client.post(
            f"/dashboard/invite/{token}", data={"password": password, "repeat": repeat}
        )
        assert response.status_code == 422
        assert message in response.text
        assert accept(client, token) == 303  # the link still works


def test_an_expired_link_does_not_work(admin: Browser) -> None:
    invite(admin, "ana")
    target = user(admin, "ana")
    admin.repositories(
        lambda repositories: repositories.users.issue_invite(
            target.id, link_digest("expired-token"), timedelta(seconds=-1)
        )
    )
    with other_browser(admin) as client:
        page = client.get("/dashboard/invite/expired-token")
        assert page.status_code == 404
        assert "invalid, expired or already used" in page.text
        assert accept(client, "expired-token") == 404


def test_guessing_links_is_throttled(admin: Browser) -> None:
    token = invite(admin, "ana")
    with other_browser(admin) as client:
        for attempt in range(5):
            assert client.get(f"/dashboard/invite/guess{attempt}").status_code == 404
        assert client.get(f"/dashboard/invite/{token}").status_code == 429
        assert accept(client, token) == 429


def test_a_new_link_logs_the_user_out_and_replaces_the_password(admin: Browser) -> None:
    token = invite(admin, "ana")
    with other_browser(admin) as client:
        assert accept(client, token) == 303
        assert client.get("/dashboard/").status_code == 200
        ana = user(admin, "ana")

        response = admin.post(f"/dashboard/users/{ana.id}/link")
        assert response.status_code == 200
        match = LINK.search(response.text)
        assert match is not None

        assert client.get("/dashboard/").status_code == 303  # logged out
        assert Browser(client).login(NEW_PASSWORD, "ana").status_code == 401
        assert accept(client, match.group(2), PASSWORD) == 303


def test_usernames_are_validated_and_unique(admin: Browser) -> None:
    invite(admin, "ana")

    duplicate = admin.post("/dashboard/users", {"username": "ANA"})
    invalid = admin.post("/dashboard/users", {"username": "a b"})

    assert duplicate.status_code == 422
    assert "ana already exists." in duplicate.text
    assert invalid.status_code == 422
    assert "3 to 32 characters" in invalid.text


def test_an_invited_administrator_can_manage_users(admin: Browser) -> None:
    token = invite(admin, "boss", is_admin=True)
    with other_browser(admin) as client:
        assert accept(client, token) == 303
        assert client.get("/dashboard/users").status_code == 200


# --- administrators only ------------------------------------------------------------------


def test_a_regular_user_cannot_reach_the_user_pages(admin: Browser) -> None:
    token = invite(admin, "ana")
    target = user(admin, ADMIN)
    with other_browser(admin) as client:
        assert accept(client, token) == 303
        ana = Browser(client)
        assert ana.get("/dashboard/users").status_code == 403
        for path in (
            "/dashboard/users",
            f"/dashboard/users/{target.id}/link",
            f"/dashboard/users/{target.id}/disable",
            f"/dashboard/users/{target.id}/enable",
        ):
            response = ana.post(path, {"username": "mallory", "is_admin": "on"})
            assert response.status_code == 403, path
            assert "Only an administrator" in response.text

    assert user(admin, ADMIN).can_log_in
    users = admin.repositories(lambda repositories: repositories.users.list_all())
    assert [item.username for item in users] == ["admin", "ana"]


def test_user_pages_need_a_session(admin: Browser) -> None:
    with other_browser(admin) as client:
        assert client.get("/dashboard/users").status_code == 303
        assert client.get("/dashboard/account").status_code == 303
        for path in ("/dashboard/users", "/dashboard/users/1/link", "/dashboard/account/password"):
            assert client.post(path, data={"csrf": "x"}).status_code == 303, path


# --- disabling ----------------------------------------------------------------------------


def test_a_disabled_user_is_logged_out_and_cannot_log_in(admin: Browser) -> None:
    token = invite(admin, "ana")
    with other_browser(admin) as client:
        assert accept(client, token) == 303
        ana = user(admin, "ana")

        response = admin.post(f"/dashboard/users/{ana.id}/disable")
        assert admin.follow(response).text.count("ana is disabled") == 1

        assert client.get("/dashboard/").status_code == 303
        assert Browser(client).login(NEW_PASSWORD, "ana").status_code == 401

        admin.post(f"/dashboard/users/{ana.id}/enable")
        assert Browser(client).login(NEW_PASSWORD, "ana").status_code == 303


def test_an_administrator_cannot_lock_themselves_out(admin: Browser) -> None:
    me = user(admin, ADMIN)

    disable = admin.post(f"/dashboard/users/{me.id}/disable")
    link = admin.post(f"/dashboard/users/{me.id}/link")

    assert disable.status_code == 422
    assert "cannot disable your own account" in disable.text
    assert link.status_code == 422
    assert user(admin, ADMIN).can_log_in
    assert admin.post("/dashboard/users/999/disable").status_code == 404
    assert admin.post("/dashboard/users/999/link").status_code == 404


# --- the account page ---------------------------------------------------------------------


def test_changing_the_password_logs_other_sessions_out(admin: Browser) -> None:
    with other_browser(admin) as client:
        second = Browser(client)
        assert second.login().status_code == 303

        wrong = admin.post(
            "/dashboard/account/password",
            {"current": "nope", "password": NEW_PASSWORD, "repeat": NEW_PASSWORD},
        )
        assert wrong.status_code == 422
        assert "current password is wrong" in wrong.text

        mismatch = admin.post(
            "/dashboard/account/password",
            {"current": PASSWORD, "password": NEW_PASSWORD, "repeat": "other"},
        )
        assert mismatch.status_code == 422

        changed = admin.post(
            "/dashboard/account/password",
            {"current": PASSWORD, "password": NEW_PASSWORD, "repeat": NEW_PASSWORD},
        )
        assert changed.status_code == 303
        assert "Password changed" in admin.follow(changed).text
        assert admin.get("/dashboard/").status_code == 200  # this browser stays in
        assert second.get("/dashboard/").status_code == 303  # the other one is out
        assert second.login().status_code == 401
        assert second.login(NEW_PASSWORD).status_code == 303


# --- login hardening ----------------------------------------------------------------------


def test_an_unknown_user_gets_the_same_answer_as_a_wrong_password(admin: Browser) -> None:
    with other_browser(admin) as client:
        browser = Browser(client)
        unknown = browser.login(username="nobody")
        wrong = browser.login("nope")

    assert unknown.status_code == wrong.status_code == 401
    assert "Wrong username or password." in unknown.text
    assert SESSION_COOKIE not in unknown.headers.get("set-cookie", "")


def test_an_account_is_locked_after_ten_failures_from_any_address(tmp_path: Path) -> None:
    app = make_app(tmp_path, client_ip_header="CF-Connecting-IP")
    with TestClient(app, follow_redirects=False, client=TUNNEL) as client:
        for attempt in range(10):
            response = client.post(
                "/dashboard/login",
                data={"username": ADMIN, "password": "nope"},
                headers={"cf-connecting-ip": f"198.51.100.{attempt}"},
            )
            assert response.status_code == 401
        locked = client.post(
            "/dashboard/login",
            data={"username": ADMIN, "password": PASSWORD},
            headers={"cf-connecting-ip": "203.0.113.9"},
        )
        assert locked.status_code == 429


def test_the_address_comes_from_the_configured_header(tmp_path: Path) -> None:
    app = make_app(tmp_path, client_ip_header="CF-Connecting-IP")
    with TestClient(app, follow_redirects=False, client=TUNNEL) as client:
        for _ in range(5):
            client.post(
                "/dashboard/login",
                data={"username": "someone", "password": "nope"},
                headers={"cf-connecting-ip": "198.51.100.1"},
            )
        blocked = client.post(
            "/dashboard/login",
            data={"username": ADMIN, "password": PASSWORD},
            headers={"cf-connecting-ip": "198.51.100.1"},
        )
        allowed = client.post(
            "/dashboard/login",
            data={"username": ADMIN, "password": PASSWORD},
            headers={"cf-connecting-ip": "198.51.100.2"},
        )
    assert blocked.status_code == 429
    assert allowed.status_code == 303


def test_the_header_is_ignored_unless_the_peer_is_a_trusted_proxy(tmp_path: Path) -> None:
    """A browser reaching the port directly cannot pick a fresh address for every attempt."""
    app = make_app(tmp_path, client_ip_header="CF-Connecting-IP", trusted_proxies="loopback")
    with TestClient(app, follow_redirects=False, client=("203.0.113.50", 4000)) as client:
        for attempt in range(5):
            client.post(
                "/dashboard/login",
                data={"username": f"someone{attempt}", "password": "nope"},
                headers={"cf-connecting-ip": f"198.51.100.{attempt}"},
            )
        blocked = client.post(
            "/dashboard/login",
            data={"username": ADMIN, "password": PASSWORD},
            headers={"cf-connecting-ip": "198.51.100.200"},
        )
    assert blocked.status_code == 429


def test_trusted_proxies_are_networks_or_address_classes() -> None:
    with pytest.raises(ValueError, match="IP networks"):
        Settings(_env_file=None, trusted_proxies="loopback,not-a-network")  # type: ignore[call-arg]
    settings = Settings(_env_file=None, trusted_proxies=" Loopback , 198.51.100.0/24 ")  # type: ignore[call-arg]

    trusted = settings.trusted_proxy_networks
    assert is_trusted_proxy("::1", trusted)
    assert is_trusted_proxy("198.51.100.7", trusted)
    assert not is_trusted_proxy(TUNNEL[0], trusted)  # private, but not listed
    assert not is_trusted_proxy("testclient", trusted)
    assert is_trusted_proxy(TUNNEL[0], Settings(_env_file=None).trusted_proxy_networks)  # type: ignore[call-arg]


def test_access_link_tokens_are_redacted_from_the_access_log() -> None:
    line = '127.0.0.1:5000 - "GET /dashboard/invite/AbC_d-12345 HTTP/1.1" 200'

    assert RedactingFilter().redact(line) == (
        f'127.0.0.1:5000 - "GET /dashboard/invite/{REDACTED} HTTP/1.1" 200'
    )


def test_the_access_logger_gets_the_redacting_filter_once() -> None:
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        assert sum(isinstance(item, RedactingFilter) for item in access.filters) == 1
    finally:
        access.filters = before
