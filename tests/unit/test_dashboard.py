"""The configuration dashboard end to end through FastAPI (spec 020, AC1-AC18).

Each test runs the real application lifespan with the offline runtime (temporary database, fake
provider, no network) and drives it with ``TestClient`` like a browser would.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.dashboard import ADMIN, HASH, PASSWORD, SECRET, Browser, make_app
from tests.fixtures.repositories import sample_rule
from tests.fixtures.session_candles import session_candles
from trading_bot.dashboard import routes
from trading_bot.dashboard.routes import SECURITY_HEADERS, SESSION_COOKIE
from trading_bot.dashboard.unit_of_work import DashboardRepositories
from trading_bot.data.errors import ProviderFailure, ProviderUnavailableError
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.domain.rules.schema import dump_rule
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.repositories.signals import SqlSignalRepository

LIGHTWEIGHT_CHARTS = "lightweight-charts-5.2.1.standalone.production.js"


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[Browser]:
    with TestClient(make_app(tmp_path), follow_redirects=False) as client:
        yield Browser(client)


@pytest.fixture
def signed_in(browser: Browser) -> Browser:
    assert browser.login().status_code == 303
    browser.provider.add_ticker(
        TickerInfo(
            symbol="AAPL",
            name="Apple <Inc.>",
            asset_type=AssetType.EQUITY,
            exchange=Exchange.NASDAQ,
            currency="USD",
        )
    )
    return browser


_OWN_SCRIPTS = (
    (("type", "application/json"), ("id", "builder-data")),
    (("src", "/dashboard/static/builder.js"), ("defer", None)),
    (("src", "/dashboard/static/vendor/" + LIGHTWEIGHT_CHARTS), ("defer", None)),
    (("src", "/dashboard/static/chart.js"), ("defer", None)),
)


class _ScriptTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[tuple[str, str | None], ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":  # the parser lowercases tag names
            self.found.append(tuple(attrs))


def assert_only_own_scripts(body: str) -> None:
    """Pages run only the dashboard's own files: every script tag is data or a static file."""
    parser = _ScriptTags()
    parser.feed(body)
    for attrs in parser.found:
        assert attrs in _OWN_SCRIPTS, attrs


def add_rule(
    browser: Browser,
    name: str,
    *,
    timeframe: str = "1d",
    enabled: bool = True,
    signal: str = "BUY",
) -> int:
    def work(repositories: DashboardRepositories) -> int:
        rule = sample_rule(name, timeframe=timeframe, signal=signal)
        stored = repositories.rules.add(rule, enabled=enabled)
        return stored.id

    result: int = browser.repositories(work)
    return result


def add_ticker(browser: Browser, symbol: str = "AAPL", timeframe: Timeframe = Timeframe.D1) -> int:
    def work(repositories: DashboardRepositories) -> int:
        return repositories.tickers.add(symbol, timeframe).id

    result: int = browser.repositories(work)
    return result


def rule_document(name: str, *, timeframe: str = "1d") -> str:
    return json.dumps(dump_rule(sample_rule(name, timeframe=timeframe)))


# --- access ----------------------------------------------------------------------------------


GET_PAGES = [
    "/dashboard/",
    "/dashboard/tickers",
    "/dashboard/tickers/1",
    "/dashboard/tickers/1/delete",
    "/dashboard/rules",
    "/dashboard/rules/new",
    "/dashboard/rules/1",
    "/dashboard/rules/1/delete",
    "/dashboard/signals",
]
POST_ACTIONS = [
    "/dashboard/logout",
    "/dashboard/pause",
    "/dashboard/resume",
    "/dashboard/tickers",
    "/dashboard/tickers/1/rules",
    "/dashboard/tickers/1/enable",
    "/dashboard/tickers/1/disable",
    "/dashboard/tickers/1/delete",
    "/dashboard/rules",
    "/dashboard/rules/1",
    "/dashboard/rules/1/enable",
    "/dashboard/rules/1/disable",
    "/dashboard/rules/1/delete",
]


def test_root_redirects_to_the_dashboard(browser: Browser) -> None:
    for path in ("/", "/dashboard"):
        response = browser.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/"


def test_nothing_is_reachable_without_a_session(browser: Browser) -> None:
    add_ticker(browser)
    add_rule(browser, "Kept")
    for path in GET_PAGES:
        response = browser.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/dashboard/login"
    for path in POST_ACTIONS:
        response = browser.client.post(path, data={"csrf": "x", "symbol": "AAPL"})
        assert response.status_code == 303, path
        assert response.headers["location"] == "/dashboard/login"

    tickers, rules = browser.repositories(
        lambda repositories: (repositories.tickers.list_all(), repositories.rules.list_all())
    )
    assert [(ticker.symbol, ticker.enabled) for ticker in tickers] == [("AAPL", True)]
    assert [(rule.name, rule.enabled) for rule in rules] == [("Kept", True)]


def test_a_forged_session_cookie_is_refused(browser: Browser) -> None:
    browser.login()
    browser.client.cookies.clear()
    for forged in ("9999999999.nonce.mac", "9999999999.1.nonce.mac"):
        browser.client.cookies.set(SESSION_COOKIE, forged, path="/dashboard")
        assert browser.get("/dashboard/").status_code == 303

    assert browser.get("/dashboard/").status_code == 303


@pytest.mark.parametrize(
    "overrides",
    [{"session_secret": None}, {"session_secret": "short"}],
)
def test_the_dashboard_is_disabled_without_valid_secrets(
    tmp_path: Path, overrides: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    app = make_app(tmp_path, **overrides)
    root = logging.getLogger()
    with caplog.at_level(logging.WARNING), TestClient(app, follow_redirects=False) as client:
        root.addHandler(caplog.handler)
        try:
            assert client.get("/health").status_code == 200
            assert client.get("/dashboard/").status_code == 503
            assert client.get("/dashboard/login").status_code == 503
            login = {"username": ADMIN, "password": PASSWORD}
            assert client.post("/dashboard/login", data=login).status_code == 503
            assert client.post("/dashboard/pause", data={}).status_code == 503
            assert "The dashboard is disabled" in client.get("/dashboard/rules").text
        finally:
            root.removeHandler(caplog.handler)


@pytest.mark.parametrize(
    "overrides", [{"dashboard_password_hash": None}, {"dashboard_password_hash": "scrypt:x"}]
)
def test_without_users_and_without_a_first_password_nobody_can_log_in(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with TestClient(make_app(tmp_path, **overrides), follow_redirects=False) as client:
        assert client.get("/dashboard/").status_code == 303
        assert client.get("/dashboard/login").status_code == 503
        login = {"username": ADMIN, "password": PASSWORD}
        assert client.post("/dashboard/login", data=login).status_code == 503


def test_disabled_reason_is_logged_without_the_value(tmp_path: Path) -> None:
    bad_hash = "scrypt:16384:8:1:" + "c2FsdHNhbHQ" + ":" + "!!!!"
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    app_logger = logging.getLogger("trading_bot.dashboard")
    handler = Collect()
    app_logger.addHandler(handler)
    try:
        make_app(tmp_path, dashboard_password_hash=bad_hash)
    finally:
        app_logger.removeHandler(handler)

    messages = [record.getMessage() for record in records]
    assert messages == [
        "TB_DASHBOARD_PASSWORD_HASH is not a valid hash and is ignored;"
        " generate it with python -m trading_bot.dashboard.password"
    ]
    assert all("!!!!" not in message for message in messages)


def test_login_sets_a_strict_http_only_session(browser: Browser) -> None:
    assert browser.get("/dashboard/login").status_code == 200

    response = browser.login()

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/dashboard" in cookie
    assert "max-age=604800" in cookie
    assert "secure" not in cookie  # plain HTTP on the LAN or the tailnet
    page = browser.get("/dashboard/")
    assert page.status_code == 200
    assert "Running, with nothing to evaluate" in page.text
    assert browser.get("/dashboard/login").status_code == 303  # already signed in


def test_wrong_password(browser: Browser) -> None:
    response = browser.login("nope")

    assert response.status_code == 401
    assert "Wrong username or password." in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert browser.get("/dashboard/").status_code == 303


def test_login_is_throttled_after_five_failures(browser: Browser) -> None:
    for _ in range(5):
        assert browser.login("nope").status_code == 401

    response = browser.login()

    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    assert browser.get("/dashboard/").status_code == 303


def test_logout_clears_the_session(signed_in: Browser) -> None:
    response = signed_in.post("/dashboard/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/login"
    assert signed_in.get("/dashboard/").status_code == 303


@pytest.mark.parametrize("token", [None, "", "forged"])
def test_actions_without_a_valid_csrf_token_change_nothing(
    signed_in: Browser, token: str | None
) -> None:
    data = {"other": "x"} if token is None else {"csrf": token}

    response = signed_in.client.post("/dashboard/pause", data=data)

    assert response.status_code == 403
    assert not signed_in.repositories(lambda repositories: repositories.state.load().paused)


def test_forms_must_be_url_encoded_and_bounded(signed_in: Browser) -> None:
    as_json = signed_in.client.post("/dashboard/pause", json={"csrf": signed_in.csrf()})
    huge = signed_in.post("/dashboard/rules", {"document": "x" * 300_000})

    assert as_json.status_code == 415
    assert huge.status_code == 413


def test_security_headers_on_every_dashboard_response(signed_in: Browser) -> None:
    for path in ("/", "/dashboard/", "/dashboard/login", "/dashboard/rules/new"):
        response = signed_in.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)
    assert "script-src 'self';" in SECURITY_HEADERS["Content-Security-Policy"]
    assert "unsafe" not in SECURITY_HEADERS["Content-Security-Policy"].replace(
        "style-src 'unsafe-inline'", ""
    )
    assert "x-frame-options" not in signed_in.get("/health").headers


def test_no_secret_reaches_a_page(signed_in: Browser) -> None:
    add_rule(signed_in, "Rule")
    add_ticker(signed_in)
    bodies = [signed_in.get(path).text for path in GET_PAGES]
    bodies.append(signed_in.get("/dashboard/login").text)

    for body in bodies:
        assert PASSWORD not in body
        assert HASH not in body
        assert SECRET not in body
        assert_only_own_scripts(body)


# --- overview and pause ----------------------------------------------------------------------


def test_pause_and_resume(signed_in: Browser) -> None:
    page = signed_in.follow(signed_in.post("/dashboard/pause"))
    assert "Paused. No candle is evaluated" in page.text
    assert 'class="badge off">Paused</span> since' in page.text
    assert signed_in.repositories(lambda repositories: repositories.state.load().paused)

    again = signed_in.follow(signed_in.post("/dashboard/pause"))
    assert "already paused" in again.text

    resumed = signed_in.follow(signed_in.post("/dashboard/resume"))
    assert "Resumed." in resumed.text
    assert "Running, with nothing to evaluate" in resumed.text
    assert "The bot was not paused." in signed_in.follow(signed_in.post("/dashboard/resume")).text


def test_overview_counts(signed_in: Browser) -> None:
    add_rule(signed_in, "On", enabled=True)
    add_rule(signed_in, "Off", enabled=False)
    add_ticker(signed_in)

    text = signed_in.get("/dashboard/").text

    assert "Tickers watched" in text
    assert re.search(r'<div class="value">1</div>\s*<div class="muted">none stopped</div>', text)
    assert re.search(r'<div class="value">1</div>\s*<div class="muted">1 disabled</div>', text)
    assert "Running, with nothing to evaluate" in text  # the ticker has no rule assigned


def test_overview_says_what_is_evaluated(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    rule_id = add_rule(signed_in, "Daily")
    signed_in.repositories(lambda repositories: repositories.assignments.assign(ticker_id, rule_id))

    text = signed_in.get("/dashboard/").text

    assert 'class="badge on">Running</span> 1 ticker evaluated' in text
    assert re.search(r"<td>1d</td>\s*<td>1 ticker</td>", text)
    assert re.search(r"<td>1h</td>\s*<td><span class=\"muted\">nothing</span></td>", text)
    assert "Next run" in text


def test_overview_without_rules_points_to_them(signed_in: Browser) -> None:
    text = signed_in.get("/dashboard/").text

    assert "Running, with nothing to evaluate" in text
    assert "Enable or create a rule" in text


# --- tickers ---------------------------------------------------------------------------------


def test_add_a_ticker_like_telegram_add(signed_in: Browser) -> None:
    add_rule(signed_in, "Daily", timeframe="1d")
    add_rule(signed_in, "Hourly", timeframe="1h")

    page = signed_in.follow(
        signed_in.post("/dashboard/tickers", {"symbol": "aapl", "timeframe": "1d"})
    )

    assert "Watching AAPL (Apple &lt;Inc.&gt;) on 1d with 1 rule (1 enabled)." in page.text
    assert signed_in.provider.validate_calls == ("aapl",)
    rules = signed_in.repositories(
        lambda repositories: [
            rule.name
            for ticker in repositories.tickers.list_all()
            for rule in repositories.assignments.rules_for_ticker(ticker.id)
        ]
    )
    assert rules == ["Daily"]
    again = signed_in.follow(
        signed_in.post("/dashboard/tickers", {"symbol": "AAPL", "timeframe": "1d"})
    )
    assert "Already watching AAPL" in again.text


def test_add_a_ticker_without_enabled_rules_warns(signed_in: Browser) -> None:
    page = signed_in.follow(
        signed_in.post("/dashboard/tickers", {"symbol": "AAPL", "timeframe": "4h"})
    )

    assert "with 0 rules (0 enabled). No enabled 4h rule yet" in page.text


@pytest.mark.parametrize(
    ("symbol", "timeframe", "message"),
    [
        ("MSFT", "1d", "MSFT was not found."),
        ("AAPL", "5m", "Unknown timeframe. Use 1h, 4h, 1d or 1w."),
        ("<b>", "1d", "&lt;B&gt; was not found."),
    ],
)
def test_invalid_tickers_are_explained(
    signed_in: Browser, symbol: str, timeframe: str, message: str
) -> None:
    page = signed_in.follow(
        signed_in.post("/dashboard/tickers", {"symbol": symbol, "timeframe": timeframe})
    )

    assert message in page.text
    assert 'class="flash error"' in page.text
    assert signed_in.repositories(lambda repositories: repositories.tickers.list_all()) == ()


def test_provider_outage_is_explained(signed_in: Browser) -> None:
    signed_in.provider.fail_next(
        ProviderUnavailableError(ProviderFailure.TIMEOUT, ticker="AAPL"),
        ticker="AAPL",
        method="validate_ticker",
    )

    page = signed_in.follow(
        signed_in.post("/dashboard/tickers", {"symbol": "AAPL", "timeframe": "1d"})
    )

    assert "The market data provider is not answering. Try again later." in page.text


def test_stop_and_watch_again(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)

    stopped = signed_in.follow(signed_in.post(f"/dashboard/tickers/{ticker_id}/disable"))
    assert "Stopped watching AAPL 1d. Its signal history is kept." in stopped.text
    assert (
        "already stopped"
        in signed_in.follow(signed_in.post(f"/dashboard/tickers/{ticker_id}/disable")).text
    )

    watched = signed_in.follow(signed_in.post(f"/dashboard/tickers/{ticker_id}/enable"))
    assert "Watching AAPL 1d again." in watched.text
    assert (
        "Already watching AAPL 1d."
        in signed_in.follow(signed_in.post(f"/dashboard/tickers/{ticker_id}/enable")).text
    )


def test_choose_the_rules_of_a_ticker(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    first = add_rule(signed_in, "First")
    second = add_rule(signed_in, "Second")
    add_rule(signed_in, "Hourly", timeframe="1h")

    page = signed_in.get(f"/dashboard/tickers/{ticker_id}")
    assert "First" in page.text
    assert "Second" in page.text
    assert "Hourly" not in page.text  # another timeframe cannot run on it

    saved = signed_in.follow(
        signed_in.post(f"/dashboard/tickers/{ticker_id}/rules", {"rule": [str(first), str(second)]})
    )
    assert "AAPL 1d now has 2 rules." in saved.text

    narrowed = signed_in.follow(
        signed_in.post(f"/dashboard/tickers/{ticker_id}/rules", {"rule": [str(second), "junk"]})
    )
    assert "AAPL 1d now has 1 rule." in narrowed.text
    names = signed_in.repositories(
        lambda repositories: [
            rule.name for rule in repositories.assignments.rules_for_ticker(ticker_id)
        ]
    )
    assert names == ["Second"]


def test_delete_a_ticker_after_confirmation(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    rule_id = add_rule(signed_in, "Rule")
    signed_in.repositories(lambda repositories: repositories.assignments.assign(ticker_id, rule_id))

    confirm = signed_in.get(f"/dashboard/tickers/{ticker_id}/delete")
    assert "Delete ticker AAPL 1d?" in confirm.text
    assert "1 assignment and" in confirm.text
    assert "0 recorded signals" in confirm.text

    page = signed_in.follow(signed_in.post(f"/dashboard/tickers/{ticker_id}/delete"))

    assert "Deleted AAPL 1d and its history." in page.text
    assert signed_in.repositories(lambda repositories: repositories.tickers.list_all()) == ()
    assert signed_in.get(f"/dashboard/tickers/{ticker_id}").status_code == 404
    assert signed_in.post(f"/dashboard/tickers/{ticker_id}/delete").status_code == 404


# --- rules -----------------------------------------------------------------------------------


def test_the_new_rule_page_offers_a_valid_example_and_the_catalog(signed_in: Browser) -> None:
    page = signed_in.get("/dashboard/rules/new")

    assert page.status_code == 200
    assert "My new rule" in page.text
    assert "crosses_below" in page.text
    assert "<code>macd</code>" in page.text
    assert 'name="assign" checked' in page.text


def test_create_a_rule_and_assign_it(signed_in: Browser) -> None:
    daily = add_ticker(signed_in, "AAPL", Timeframe.D1)
    add_ticker(signed_in, "SPY", Timeframe.H1)

    page = signed_in.follow(
        signed_in.post(
            "/dashboard/rules",
            {"document": rule_document("Fresh"), "enabled": "on", "assign": "on"},
        )
    )

    assert "Created rule Fresh (enabled), assigned to 1 1d ticker." in page.text
    assigned = signed_in.repositories(
        lambda repositories: [
            (rule.name, rule.enabled) for rule in repositories.assignments.rules_for_ticker(daily)
        ]
    )
    assert assigned == [("Fresh", True)]


def test_a_new_rule_is_disabled_and_unassigned_unless_asked(signed_in: Browser) -> None:
    add_ticker(signed_in)

    page = signed_in.follow(
        signed_in.post("/dashboard/rules", {"document": rule_document("Quiet")})
    )

    assert "Created rule Quiet (disabled), assigned to 0 1d tickers." in page.text


def test_an_invalid_rule_is_explained_and_kept(signed_in: Browser) -> None:
    document = '{"name": "Broken", "signal": "HOLD"}'

    response = signed_in.post("/dashboard/rules", {"document": document, "enabled": "on"})

    assert response.status_code == 422
    assert "The rule was not saved" in response.text
    assert "signal:" in response.text
    assert "&#34;HOLD&#34;" in response.text or "&quot;HOLD&quot;" in response.text
    assert 'name="enabled" checked' in response.text
    assert signed_in.repositories(lambda repositories: repositories.rules.list_all()) == ()


def test_invalid_json_is_explained(signed_in: Browser) -> None:
    response = signed_in.post("/dashboard/rules", {"document": "{not json"})

    assert response.status_code == 422
    assert "The rule was not saved" in response.text


def test_duplicate_rule_names_are_refused(signed_in: Browser) -> None:
    add_rule(signed_in, "Taken")

    response = signed_in.post("/dashboard/rules", {"document": rule_document("Taken")})

    assert response.status_code == 422
    assert "A rule with that name already exists" in response.text


def test_edit_a_rule(signed_in: Browser) -> None:
    rule_id = add_rule(signed_in, "Before")

    page = signed_in.get(f"/dashboard/rules/{rule_id}")
    assert page.status_code == 200
    assert "&#34;Before&#34;" in page.text or "&quot;Before&quot;" in page.text

    assert 'name="enabled" checked' in page.text  # the stored flag is offered as is

    saved = signed_in.follow(
        signed_in.post(
            f"/dashboard/rules/{rule_id}", {"document": rule_document("After"), "enabled": "on"}
        )
    )

    assert "Saved rule After (enabled)." in saved.text
    names = signed_in.repositories(
        lambda repositories: [(rule.name, rule.enabled) for rule in repositories.rules.list_all()]
    )
    assert names == [("After", True)]

    disabled = signed_in.follow(
        signed_in.post(f"/dashboard/rules/{rule_id}", {"document": rule_document("After")})
    )
    assert "Saved rule After (disabled)." in disabled.text


@pytest.mark.parametrize("path", ["/dashboard/rules", "/dashboard/rules/{id}"])
def test_check_without_saving(signed_in: Browser, path: str) -> None:
    rule_id = add_rule(signed_in, "Existing")
    target = path.format(id=rule_id)

    valid = signed_in.post(
        target, {"document": rule_document("Checked"), "intent": "check", "enabled": "on"}
    )
    invalid = signed_in.post(target, {"document": "{}", "intent": "check"})

    assert valid.status_code == 200
    assert "The rule is valid. Nothing was saved yet." in valid.text
    assert 'name="enabled" checked' in valid.text
    assert invalid.status_code == 422
    names = signed_in.repositories(
        lambda repositories: [rule.name for rule in repositories.rules.list_all()]
    )
    assert names == ["Existing"]


def test_navigation_marks_the_current_page(signed_in: Browser) -> None:
    rule_id = add_rule(signed_in, "Rule")
    expected = {
        "/dashboard/": "Overview",
        "/dashboard/tickers": "Tickers",
        "/dashboard/rules": "Rules",
        f"/dashboard/rules/{rule_id}": "Rules",
    }
    for path, label in expected.items():
        text = signed_in.get(path).text
        assert re.findall(r'aria-current="page">(\w+)<', text) == [label], path


def test_layout_details(signed_in: Browser) -> None:
    text = signed_in.get("/dashboard/tickers").text

    assert 'rel="icon" href="data:image/svg+xml' in text
    assert "scrollbar-gutter: stable" in text
    assert "<footer>Signals only" in text
    assert "Times are shown in America/Argentina/Buenos_Aires" in text
    assert '<label class="field" for="symbol">Symbol</label>' in text
    assert "for example <code>AAPL</code>" in text  # the empty state invites a first ticker


def test_timeframe_options_say_how_many_rules_a_new_ticker_gets(signed_in: Browser) -> None:
    add_rule(signed_in, "Daily one")
    add_rule(signed_in, "Daily two")
    add_rule(signed_in, "Daily off", enabled=False)

    text = signed_in.get("/dashboard/tickers").text

    assert '<option value="1d" selected>1d · 2 enabled rules</option>' in text
    assert '<option value="1h" >1h · no enabled rule</option>' in text


def test_state_badges_switch_and_sides_are_colored(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    add_rule(signed_in, "Buyer")
    add_rule(signed_in, "Seller", signal="SELL", enabled=False)

    rules = signed_in.get("/dashboard/rules").text
    assert '<span class="side-buy">BUY</span>' in rules
    assert '<span class="side-sell">SELL</span>' in rules
    assert 'class="badge on"' in rules
    assert 'class="badge off"' in rules
    assert 'class="button danger"' in rules

    page = signed_in.follow(
        signed_in.post(f"/dashboard/tickers/{ticker_id}/disable", {"back": "ticker"})
    )
    assert "Stopped watching AAPL 1d." in page.text
    assert ">Stopped</button>" in page.text  # back on the ticker's own page


def test_an_assigned_rule_cannot_change_timeframe(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    rule_id = add_rule(signed_in, "Daily")
    signed_in.repositories(lambda repositories: repositories.assignments.assign(ticker_id, rule_id))

    response = signed_in.post(
        f"/dashboard/rules/{rule_id}", {"document": rule_document("Daily", timeframe="1h")}
    )

    assert response.status_code == 422
    assert "cannot change to 1h" in response.text
    rule = signed_in.repositories(lambda repositories: repositories.rules.get(rule_id))
    assert rule is not None
    assert rule.rule.timeframe is Timeframe.D1


def test_editing_to_an_existing_name_is_refused(signed_in: Browser) -> None:
    add_rule(signed_in, "One")
    two = add_rule(signed_in, "Two")

    response = signed_in.post(f"/dashboard/rules/{two}", {"document": rule_document("One")})

    assert response.status_code == 422
    assert "Another rule already has that name" in response.text


def test_editing_with_an_invalid_document(signed_in: Browser) -> None:
    rule_id = add_rule(signed_in, "Rule")

    response = signed_in.post(f"/dashboard/rules/{rule_id}", {"document": "[]"})

    assert response.status_code == 422


def test_enable_and_disable_a_rule(signed_in: Browser) -> None:
    rule_id = add_rule(signed_in, "Switch", enabled=False)

    assert (
        "Enabled rule Switch."
        in signed_in.follow(signed_in.post(f"/dashboard/rules/{rule_id}/enable")).text
    )
    assert (
        "already enabled"
        in signed_in.follow(signed_in.post(f"/dashboard/rules/{rule_id}/enable")).text
    )
    assert (
        "Disabled rule Switch."
        in signed_in.follow(signed_in.post(f"/dashboard/rules/{rule_id}/disable")).text
    )


def test_delete_a_rule_after_confirmation(signed_in: Browser) -> None:
    rule_id = add_rule(signed_in, "Doomed")

    confirm = signed_in.get(f"/dashboard/rules/{rule_id}/delete")
    assert "Delete rule Doomed?" in confirm.text

    page = signed_in.follow(signed_in.post(f"/dashboard/rules/{rule_id}/delete"))

    assert "Deleted rule Doomed and its history." in page.text
    assert signed_in.repositories(lambda repositories: repositories.rules.list_all()) == ()
    assert signed_in.get(f"/dashboard/rules/{rule_id}").status_code == 404


def test_hostile_names_are_escaped(signed_in: Browser) -> None:
    add_rule(signed_in, "<script>alert(1)</script>")

    text = signed_in.get("/dashboard/rules").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_the_dashboard_shows_beta_outside_production(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, environment="dev")) as client:
        assert "[BETA]" in client.get("/dashboard/login").text
    with TestClient(make_app(tmp_path, environment="prod")) as client:
        assert "[BETA]" not in client.get("/dashboard/login").text


def test_opening_a_delete_link_never_deletes(signed_in: Browser) -> None:
    ticker_id = add_ticker(signed_in)
    rule_id = add_rule(signed_in, "Kept")

    for path in (f"/dashboard/tickers/{ticker_id}/delete", f"/dashboard/rules/{rule_id}/delete"):
        page = signed_in.get(path)
        assert page.status_code == 200
        assert 'method="post"' in page.text
        assert 'name="csrf"' in page.text

    assert len(signed_in.repositories(lambda repositories: repositories.tickers.list_all())) == 1
    assert len(signed_in.repositories(lambda repositories: repositories.rules.list_all())) == 1


# --- behind a TLS-terminating tunnel (spec 021) ----------------------------------------------


@pytest.mark.parametrize(
    ("origin", "secure"),
    [
        ("https://dashboard.example", True),  # through the tunnel: the browser used HTTPS
        ("http://192.0.2.10:8081", False),  # plain HTTP on the LAN or the tailnet
        ("null", False),  # opaque origin: fall back to the request URL (plain HTTP here)
        (None, False),  # no Origin header: same fallback
    ],
)
def test_session_cookie_is_secure_when_the_browser_used_https(
    browser: Browser, origin: str | None, secure: bool
) -> None:
    headers = {"origin": origin} if origin is not None else {}

    response = browser.client.post(
        "/dashboard/login", data={"username": ADMIN, "password": PASSWORD}, headers=headers
    )

    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie
    assert ("; secure" in cookie) is secure


def test_session_cookie_is_secure_when_the_app_itself_serves_https(tmp_path: Path) -> None:
    with TestClient(
        make_app(tmp_path), base_url="https://testserver", follow_redirects=False
    ) as client:
        response = client.post("/dashboard/login", data={"username": ADMIN, "password": PASSWORD})

    assert response.status_code == 303
    assert "; secure" in response.headers["set-cookie"].lower()


def test_rule_form_ships_the_builder(signed_in: Browser) -> None:
    page = signed_in.get("/dashboard/rules/new").text
    data = page.partition('id="builder-data">')[2].partition("<")[0]  # tojson escapes "<"
    script = signed_in.get("/dashboard/static/builder.js")

    assert "rsi_level" in [template["id"] for template in json.loads(data)["templates"]]
    assert '<div id="builder" class="builder" hidden></div>' in page
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert script.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|eval\(", script.text)
    new_rule = re.search(
        r'<textarea id="document" name="document"[^>]*>(.*?)</textarea>', page, re.S
    )
    assert new_rule is not None
    conditions = json.loads(new_rule.group(1).replace("&#34;", '"'))["conditions"]
    assert conditions["all"][0]["left"]["indicator"] == "rsi"  # opens on a template


# --- chart and signal history (spec 027) -----------------------------------------------------

CHART_NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # a Friday after the close


@pytest.fixture
def charted(signed_in: Browser, monkeypatch: pytest.MonkeyPatch) -> Browser:
    """``AAPL 1d`` with candles up to ``CHART_NOW``, a BUY rule assigned and the clock fixed."""
    monkeypatch.setattr(routes, "_utc_now", lambda: CHART_NOW)
    candles = session_candles(
        nyse_test_calendar(), Timeframe.D1, datetime(2024, 6, 1, tzinfo=UTC), CHART_NOW, seed=7
    )
    signed_in.provider.set_candles("AAPL", Timeframe.D1, candles)
    ticker_id = add_ticker(signed_in)
    rule_id = add_rule(signed_in, "Daily")
    signed_in.repositories(lambda r: r.assignments.assign(ticker_id, rule_id))
    return signed_in


def record_signals(browser: Browser, count: int, *, symbol: str = "AAPL", rule_id: int = 1) -> None:
    with browser.app.state.runtime.database.session() as session:
        signals = SqlSignalRepository(session)
        for day in range(count):
            signals.record(
                Signal(
                    ticker=symbol,
                    timeframe=Timeframe.D1,
                    rule_id=str(rule_id),
                    side=Side.BUY,
                    candle_close_ts=datetime(2026, 1, 6, 5, tzinfo=UTC) + timedelta(days=day),
                    close_price=100.0 + day,
                )
            )
        session.commit()


def test_chart_data_of_a_ticker(charted: Browser) -> None:
    response = charted.get("/dashboard/tickers/1/chart.json")

    data = response.json()
    assert response.status_code == 200
    assert data["symbol"] == "AAPL"
    assert data["timeframe"] == "1d"
    assert len(data["candles"]) == 250
    assert data["candles"][-1]["time"] == "2026-10-09"
    assert [line["label"] for line in data["lines"]] == ["SMA 20"]  # close > SMA 20
    assert (
        response.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    )
    assert charted.provider.fetch_calls[-1].now == CHART_NOW


def test_chart_data_needs_a_session_and_an_own_ticker(charted: Browser) -> None:
    assert charted.get("/dashboard/tickers/9/chart.json").status_code == 404
    charted.post("/dashboard/logout")

    response = charted.get("/dashboard/tickers/1/chart.json")

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/login"


def test_chart_waits_for_an_unpublished_candle(charted: Browser) -> None:
    candles = charted.provider  # the newest candle of CHART_NOW is not published yet
    full = session_candles(
        nyse_test_calendar(), Timeframe.D1, datetime(2024, 6, 1, tzinfo=UTC), CHART_NOW, seed=7
    )
    candles.set_candles("AAPL", Timeframe.D1, full.iloc[:-1])

    data = charted.get("/dashboard/tickers/1/chart.json").json()

    assert data["candles"][-1]["time"] == "2026-10-08"
    assert [call.now for call in charted.provider.fetch_calls[-2:]] == [
        CHART_NOW,
        CHART_NOW - Timeframe.D1.duration,
    ]


def test_chart_says_when_market_data_cannot_be_read(charted: Browser) -> None:
    charted.provider.fail_next(
        ProviderUnavailableError(ProviderFailure.TIMEOUT, ticker="AAPL"), ticker="AAPL"
    )

    response = charted.get("/dashboard/tickers/1/chart.json")

    assert response.status_code == 503
    assert response.json() == {"error": "The market data of AAPL cannot be read right now."}
    assert charted.get("/dashboard/tickers/1").status_code == 200


def test_ticker_page_holds_the_chart_and_recent_signals(charted: Browser) -> None:
    record_signals(charted, 25)

    page = charted.get("/dashboard/tickers/1").text

    assert 'data-src="/dashboard/tickers/1/chart.json"' in page
    assert f'src="/dashboard/static/vendor/{LIGHTWEIGHT_CHARTS}"' in page
    assert page.count("<td>Daily</td>") == 20
    assert "<td>2026-01-29</td>" in page  # the newest candle: its trading day
    assert '<a href="/dashboard/signals?ticker=1">All 25 signals</a>' in page


def test_signals_page_lists_pages_and_filters(charted: Browser) -> None:
    record_signals(charted, 55)
    other = add_ticker(charted, "MSFT")
    charted.repositories(lambda r: r.assignments.assign(other, 1))
    record_signals(charted, 1, symbol="MSFT")

    first = charted.get("/dashboard/signals").text
    older = re.search(r'href="(/dashboard/signals\?before=[0-9]+_[0-9]+)"', first)
    filtered = charted.get(f"/dashboard/signals?ticker={other}").text

    assert first.count("<td>Daily</td>") == 50
    assert 'aria-current="page">Signals</a>' in first
    assert older is not None
    assert charted.get(older.group(1)).text.count("<td>Daily</td>") == 6
    assert filtered.count("<td>Daily</td>") == 1
    assert "MSFT</a>" in filtered
    assert (
        charted.get("/dashboard/signals?before=x_1&ticker=abc").text.count("<td>Daily</td>") == 50
    )


def test_signals_page_without_signals(signed_in: Browser) -> None:
    assert "No signal yet." in signed_in.get("/dashboard/signals").text


def test_the_chart_library_is_the_pinned_release(signed_in: Browser) -> None:
    script = signed_in.get(f"/dashboard/static/vendor/{LIGHTWEIGHT_CHARTS}")
    chart = signed_in.get("/dashboard/static/chart.js")

    assert script.status_code == 200
    assert hashlib.sha256(script.content).hexdigest() == (
        "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf"
    )
    assert (
        "Apache License"
        in signed_in.get("/dashboard/static/vendor/lightweight-charts-LICENSE.txt").text
    )
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|eval\(", chart.text)
    assert "connect-src 'self';" in SECURITY_HEADERS["Content-Security-Policy"]
