"""The Telegram page and the pause of one's own signals (spec 025, AC1-AC8).

The application runs for real with ``FakeUpdateSource``: saving a token "asks Telegram" through
it, and the bot it starts polls it. Fake tokens and keys are built at runtime (CLAUDE.md rule 2).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.fixtures.dashboard import Browser, make_app
from tests.fixtures.runtime import FakeUpdateSource
from trading_bot.dashboard.unit_of_work import DashboardRepositories
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.owner import ALL_OWNERS
from trading_bot.persistence.records import LinkedChat, StoredBot
from trading_bot.persistence.repositories.tickers import SqlTickerRepository
from trading_bot.secretbox import new_master_key
from trading_bot.telegram_bot.poller import PollError, PollErrorKind
from trading_bot.telegram_bot.supervisor import BotSupervisor, link_code_digest

TOKEN = "123456789" + ":" + "x" * 35
OTHER_TOKEN = "987654321" + ":" + "y" * 35
NEW_PASSWORD = "-".join(["purple", "monkey", "dishwasher", "lamp"])
LINK = re.compile(r"/dashboard/invite/([A-Za-z0-9_-]+)</pre>")
CODE = re.compile(r"/link ([A-Z2-9]{8})</code>")
GROUP = -(10**12) - 3


@pytest.fixture
def admin(tmp_path: Path) -> Iterator[Browser]:
    with TestClient(make_app(tmp_path, master_key=new_master_key()), follow_redirects=False) as c:
        browser = Browser(c)
        assert browser.login().status_code == 303
        yield browser


def second_user(admin: Browser, username: str = "ana") -> Browser:
    response = admin.post("/dashboard/users", {"username": username})
    match = LINK.search(response.text)
    assert match is not None
    client = TestClient(admin.app, follow_redirects=False)
    # The running loop of ``admin``: a bot started by this browser must outlive its request,
    # which a client of its own would run in a loop that ends with the request.
    client.portal = admin.client.portal
    browser = Browser(client)
    accepted = browser.client.post(
        f"/dashboard/invite/{match.group(1)}",
        data={"password": NEW_PASSWORD, "repeat": NEW_PASSWORD},
    )
    assert accepted.status_code == 303
    return browser


def bots(browser: Browser) -> BotSupervisor:
    supervisor = browser.app.state.runtime.bots
    assert isinstance(supervisor, BotSupervisor)
    return supervisor


def user_id(browser: Browser, username: str) -> int:
    def read(repositories: DashboardRepositories) -> int:
        found = repositories.users.get_by_username(username)
        assert found is not None
        return found.id

    return browser.repositories(read)


def stored(browser: Browser, owner: int) -> tuple[StoredBot | None, tuple[LinkedChat, ...]]:
    return browser.repositories(
        lambda repositories: (repositories.telegram.bot(owner), repositories.telegram.chats(owner))
    )


def test_the_page_explains_how_to_create_a_bot(admin: Browser) -> None:
    page = admin.get("/dashboard/telegram")

    assert page.status_code == 200
    assert "@BotFather" in page.text
    assert 'name="token"' in page.text
    assert 'aria-current="page">Telegram' in page.text


def test_saving_a_token_checks_it_stores_it_encrypted_and_starts_the_bot(
    admin: Browser, caplog: pytest.LogCaptureFixture
) -> None:
    owner = user_id(admin, "admin")
    with caplog.at_level(logging.DEBUG):
        response = admin.post("/dashboard/telegram/token", {"token": f"  {TOKEN} "})
    page = admin.follow(response)

    assert "@trading_test_bot is connected" in page.text
    assert "Running" in page.text
    assert TOKEN not in page.text
    bot, chats = stored(admin, owner)
    assert bot is not None
    assert bot.bot_username == "trading_test_bot"
    assert TOKEN not in bot.token_ciphertext
    assert chats == ()
    assert bots(admin).running(owner)
    assert TOKEN not in caplog.text

    again = admin.follow(admin.post("/dashboard/telegram/token", {"token": TOKEN}))
    assert "was already connected" in again.text


@pytest.mark.parametrize("token", ["", "not a token", "123:short", TOKEN + "\n" + TOKEN])
def test_a_malformed_token_is_refused_without_calling_telegram(admin: Browser, token: str) -> None:
    response = admin.post("/dashboard/telegram/token", {"token": token})

    assert response.status_code == 422
    assert "does not look like a bot token" in response.text
    assert stored(admin, user_id(admin, "admin"))[0] is None


def test_a_token_telegram_rejects_is_refused_and_throttled(admin: Browser) -> None:
    def rejecting(token: SecretStr) -> FakeUpdateSource:
        source = FakeUpdateSource()
        source.username_errors.append(PollError(PollErrorKind.REJECTED_TOKEN))
        return source

    bots(admin)._build_source = rejecting
    for _ in range(5):
        response = admin.post("/dashboard/telegram/token", {"token": TOKEN})
        assert response.status_code == 422
        assert "Telegram rejected this token" in response.text

    blocked = admin.post("/dashboard/telegram/token", {"token": TOKEN})
    assert blocked.status_code == 429
    assert stored(admin, user_id(admin, "admin"))[0] is None


def test_telegram_not_answering_is_reported(admin: Browser) -> None:
    def unreachable(token: SecretStr) -> FakeUpdateSource:
        source = FakeUpdateSource()
        source.username_errors.append(PollError(PollErrorKind.TRANSIENT))
        return source

    bots(admin)._build_source = unreachable
    response = admin.post("/dashboard/telegram/token", {"token": TOKEN})

    assert response.status_code == 503
    assert "not answering" in response.text


def test_a_token_already_used_by_another_account_is_refused(admin: Browser) -> None:
    ana = second_user(admin)
    admin.post("/dashboard/telegram/token", {"token": TOKEN})

    response = ana.post("/dashboard/telegram/token", {"token": TOKEN})

    assert response.status_code == 409
    assert "already connected to another account" in response.text
    assert stored(admin, user_id(admin, "ana"))[0] is None


def test_a_link_code_links_a_chat_that_can_then_be_unlinked(admin: Browser) -> None:
    owner = user_id(admin, "admin")
    admin.post("/dashboard/telegram/token", {"token": TOKEN})
    assert "No chat is linked yet" in admin.get("/dashboard/telegram").text

    page = admin.post("/dashboard/telegram/code")
    match = CODE.search(page.text)
    assert match is not None
    code = match.group(1)
    assert "expires at" in page.text
    redeemed = admin.repositories(
        lambda repositories: repositories.telegram.redeem_link(
            owner,
            link_code_digest(code),
            GROUP,
            "Family <b>group</b>",
        )
    )
    assert redeemed
    listed = admin.get("/dashboard/telegram")
    assert "Family &lt;b&gt;group&lt;/b&gt;" in listed.text  # escaped
    assert "…0003" in listed.text  # masked
    assert "Link another chat" in listed.text

    unlinked = admin.post("/dashboard/telegram/unlink", {"chat_id": str(GROUP)})
    assert "Chat unlinked" in admin.follow(unlinked).text
    assert stored(admin, owner)[1] == ()
    assert admin.post("/dashboard/telegram/unlink", {"chat_id": str(GROUP)}).status_code == 404
    assert admin.post("/dashboard/telegram/unlink", {"chat_id": "x"}).status_code == 404


def test_a_code_needs_a_bot_and_removing_the_bot_stops_it(admin: Browser) -> None:
    owner = user_id(admin, "admin")
    refused = admin.follow(admin.post("/dashboard/telegram/code"))
    assert "Connect a bot first." in refused.text
    assert admin.post("/dashboard/telegram/delete").status_code == 404

    admin.post("/dashboard/telegram/token", {"token": TOKEN})
    assert bots(admin).running(owner)
    removed = admin.follow(admin.post("/dashboard/telegram/delete"))

    assert "Bot removed" in removed.text
    assert stored(admin, owner)[0] is None
    assert not bots(admin).running(owner)


def test_each_user_sees_and_changes_only_their_own_bot(admin: Browser) -> None:
    ana = second_user(admin)
    admin.post("/dashboard/telegram/token", {"token": TOKEN})
    ana.post("/dashboard/telegram/token", {"token": OTHER_TOKEN})

    assert "Running" in ana.get("/dashboard/telegram").text
    ana.post("/dashboard/telegram/delete")

    assert stored(admin, user_id(admin, "admin"))[0] is not None
    assert stored(admin, user_id(admin, "ana"))[0] is None


def test_disabling_a_user_stops_their_bot_and_enabling_starts_it(admin: Browser) -> None:
    ana = second_user(admin)
    ana_id = user_id(admin, "ana")
    ana.post("/dashboard/telegram/token", {"token": OTHER_TOKEN})
    assert bots(admin).running(ana_id)

    admin.post(f"/dashboard/users/{ana_id}/disable")
    assert not bots(admin).running(ana_id)
    admin.post(f"/dashboard/users/{ana_id}/enable")
    assert bots(admin).running(ana_id)


def test_without_a_master_key_bots_cannot_be_stored(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path), follow_redirects=False) as client:
        browser = Browser(client)
        browser.login()
        page = browser.get("/dashboard/telegram")
        response = browser.post("/dashboard/telegram/token", {"token": TOKEN})

    assert "TB_MASTER_KEY" in page.text
    assert 'name="token"' not in page.text
    assert response.status_code == 503
    assert "TB_MASTER_KEY" in response.text


# --- the pause of one's own signals (D6) -----------------------------------------------------


def test_every_user_pauses_and_resumes_only_their_own_signals(admin: Browser) -> None:
    ana = second_user(admin)
    ana_id, admin_id = user_id(admin, "ana"), user_id(admin, "admin")
    with admin.app.state.runtime.database.session() as session:
        SqlTickerRepository(session, owner=ana_id).add("AAPL", Timeframe.D1)
        SqlTickerRepository(session, owner=admin_id).add("MSFT", Timeframe.D1)

    overview = ana.get("/dashboard/")
    assert "Pause my signals" in overview.text
    assert "Pause for everyone" not in overview.text  # the global pause is an admin's
    assert "Only logged" in overview.text

    paused = ana.follow(ana.post("/dashboard/my/pause"))
    assert "Your signals are paused" in paused.text
    assert "Resume my signals" in paused.text
    again = ana.follow(ana.post("/dashboard/my/pause"))
    assert "already paused" in again.text
    assert "Pause my signals" in admin.get("/dashboard/").text  # the admin is not paused
    with admin.app.state.runtime.database.session() as session:
        enabled = SqlTickerRepository(session, owner=ALL_OWNERS).list_enabled()
        assert [ticker.symbol for ticker in enabled] == ["MSFT"]

    resumed = ana.follow(ana.post("/dashboard/my/resume"))
    assert "Your signals are resumed" in resumed.text
    assert "were not paused" in ana.follow(ana.post("/dashboard/my/resume")).text


def test_the_overview_says_where_the_signals_go(admin: Browser) -> None:
    assert "Connect a Telegram bot" in admin.get("/dashboard/").text
    admin.post("/dashboard/telegram/token", {"token": TOKEN})
    assert "no linked chat yet" in admin.get("/dashboard/").text
    owner = user_id(admin, "admin")
    admin.repositories(lambda repositories: repositories.telegram.link_chat(owner, GROUP))
    assert "Sent to Telegram" in admin.get("/dashboard/").text
