"""The Telegram commands (spec 018, AC1-AC8, AC10).

Every command runs against a real temporary database through the SQL unit of work and a fake
provider: no network, no python-telegram-bot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.fake_provider import FakeMarketDataProvider
from tests.fixtures.repositories import fixed_clock, repositories, sample_rule
from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database
from trading_bot.telegram_bot.commands import BETA_PREFIX, HELP_TEXT, CommandHandler, parse
from trading_bot.telegram_bot.unit_of_work import sql_command_unit_of_work

START = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
MADRID = ZoneInfo("Europe/Madrid")
BOT = "trading_test_bot"


def info(symbol: str, name: str, asset_type: AssetType = AssetType.EQUITY) -> TickerInfo:
    return TickerInfo(
        symbol=symbol,
        name=name,
        asset_type=asset_type,
        exchange=Exchange.NASDAQ,
        currency="USD",
    )


class Harness:
    def __init__(self, database: Database, *, production: bool = True) -> None:
        self.database = database
        self.provider = FakeMarketDataProvider(calendar=nyse_test_calendar())
        self.provider.add_ticker(info("AAPL", "Apple Inc."))
        self.provider.add_ticker(info("SPY", "SPDR <S&P> 500", AssetType.ETF))
        self.provider.add_ticker(info("BTC-USD", "Bitcoin USD", AssetType.CRYPTOCURRENCY))
        self.fires: Sequence[tuple[Timeframe, datetime | None]] = [
            (Timeframe.H1, datetime(2026, 3, 2, 16, 30, tzinfo=UTC)),
            (Timeframe.H4, None),
            (Timeframe.D1, datetime(2026, 3, 2, 21, 0, tzinfo=UTC)),
        ]
        self.handler = CommandHandler(
            unit_of_work=sql_command_unit_of_work(database, clock=fixed_clock(START)),
            provider=self.provider,
            next_fire_times=lambda: self.fires,
            production=production,
            environment="prod" if production else "beta",
            version="1.2.3",
            display_tz=MADRID,
        )

    def send(self, text: str) -> str | None:
        return asyncio.run(self.handler.handle(text, bot_username=BOT))

    def reply(self, text: str) -> str:
        answer = self.send(text)
        assert answer is not None
        return answer


@pytest.fixture
def bot(database: Database) -> Harness:
    return Harness(database)


def seed_rules(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        handles.rules.add(sample_rule("Daily breakout"), enabled=True)
        handles.rules.add(sample_rule("Daily dip", signal="SELL"), enabled=True)
        handles.rules.add(sample_rule("Daily off"))  # rules are stored disabled by default
        handles.rules.add(sample_rule("Hourly breakout", timeframe="1h"), enabled=True)


# --- parsing --------------------------------------------------------------------------------


def test_parse_splits_the_command_and_its_arguments() -> None:
    command = parse("  /ADD aapl   1h ")

    assert command is not None
    assert command.name == "add"
    assert command.args == ("aapl", "1h")


@pytest.mark.parametrize("text", ["", "   ", "hello", "/", "add AAPL"])
def test_parse_ignores_plain_text(text: str) -> None:
    assert parse(text) is None


def test_parse_accepts_its_own_bot_suffix_and_drops_another() -> None:
    assert parse("/status@Trading_Test_Bot", bot_username=BOT) is not None
    assert parse("/status@other_bot", bot_username=BOT) is None
    assert parse("/status@other_bot") is not None  # username not known yet


# --- AC1, AC8, AC10 -------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["/start", "/help", "/help@trading_test_bot"])
def test_start_and_help_list_the_commands(bot: Harness, command: str) -> None:
    reply = bot.reply(command)

    assert reply == HELP_TEXT
    for name in ("/add", "/remove", "/list", "/rules", "/status", "/pause", "/resume"):
        assert name in reply


def test_plain_text_gets_a_hint(bot: Harness) -> None:
    assert bot.reply("hello there") == "Send /help to see what I can do."


def test_an_unknown_command_is_escaped_in_the_hint(bot: Harness) -> None:
    reply = bot.reply("/<b>buy")

    assert reply == "Unknown command /&lt;b&gt;buy. Send /help to see the commands."


def test_a_command_for_another_bot_gets_no_reply(bot: Harness) -> None:
    assert bot.send("/status@other_bot") is None


def test_every_reply_starts_with_beta_outside_prod(database: Database) -> None:
    beta = Harness(database, production=False)

    assert beta.reply("/help").startswith(f"{BETA_PREFIX} <b>Commands</b>")
    assert beta.reply("hi").startswith(f"{BETA_PREFIX} ")
    assert beta.reply("/list").startswith(f"{BETA_PREFIX} ")


def test_a_failing_command_changes_nothing_and_logs_the_class_only(
    bot: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> Sequence[tuple[Timeframe, datetime | None]]:
        raise RuntimeError("secret detail")

    bot.fires = []
    bot.handler._next_fire_times = broken

    with caplog.at_level(logging.ERROR):
        reply = bot.reply("/status")

    assert reply == "Something went wrong; nothing was changed. Check the logs."
    assert [record.getMessage() for record in caplog.records] == ["/status failed: RuntimeError"]


# --- AC2: /add ------------------------------------------------------------------------------


def test_add_defaults_to_daily_and_assigns_every_rule_of_that_timeframe(
    bot: Harness, database: Database
) -> None:
    seed_rules(database)

    reply = bot.reply("/add aapl")

    assert reply == "Watching <b>AAPL</b> (Apple Inc.) on 1d with 3 rules (2 enabled)."
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.get_by_symbol("AAPL", Timeframe.D1)
        assert ticker is not None
        assert ticker.enabled
        names = sorted(rule.name for rule in handles.assignments.rules_for_ticker(ticker.id))
    assert names == ["Daily breakout", "Daily dip", "Daily off"]


def test_add_with_a_timeframe_assigns_only_its_rules(bot: Harness, database: Database) -> None:
    seed_rules(database)

    assert bot.reply("/add AAPL 1H") == (
        "Watching <b>AAPL</b> (Apple Inc.) on 1h with 1 rule (1 enabled)."
    )


def test_add_accepts_the_weekly_timeframe(bot: Harness) -> None:
    reply = bot.reply("/add AAPL 1w")

    assert reply == (
        "Watching <b>AAPL</b> (Apple Inc.) on 1w with 0 rules (0 enabled).\n"
        "No enabled 1w rule yet: no signal will be sent for it."
    )


def test_add_warns_when_no_enabled_rule_exists(bot: Harness) -> None:
    reply = bot.reply("/add SPY 4h")

    assert reply == (
        "Watching <b>SPY</b> (SPDR &lt;S&amp;P&gt; 500) on 4h with 0 rules (0 enabled).\n"
        "No enabled 4h rule yet: no signal will be sent for it."
    )


def test_adding_twice_is_reported_and_assigns_new_rules(bot: Harness, database: Database) -> None:
    bot.reply("/add AAPL")
    seed_rules(database)

    reply = bot.reply("/add AAPL 1d")

    assert reply.startswith("Already watching <b>AAPL</b> (Apple Inc.) on 1d with 3 rules")


def test_add_re_enables_a_removed_ticker(bot: Harness, database: Database) -> None:
    bot.reply("/add AAPL")
    bot.reply("/remove AAPL")

    reply = bot.reply("/add AAPL")

    assert reply.startswith("Watching again <b>AAPL</b>")
    with database.session() as session:
        tickers = repositories(session).tickers.list_all()
    assert [(ticker.symbol, ticker.enabled) for ticker in tickers] == [("AAPL", True)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/add", "Usage: /add TICKER [1h|4h|1d|1w], for example /add AAPL 1d"),
        ("/add A B C", "Usage: /add TICKER [1h|4h|1d|1w], for example /add AAPL 1d"),
        ("/add AAPL 1mo", "Unknown timeframe 1mo. Use 1h, 4h, 1d or 1w."),
        ("/add AAPL <i>", "Unknown timeframe &lt;i&gt;. Use 1h, 4h, 1d or 1w."),
        ("/add MSFT", "MSFT was not found."),
        ("/add <i>", "&lt;i&gt; was not found."),
        (
            "/add BTC-USD",
            "BTC-USD is not supported: only US-listed stocks and ETFs are, for now.",
        ),
    ],
)
def test_add_explains_what_is_wrong(
    bot: Harness, database: Database, text: str, expected: str
) -> None:
    assert bot.reply(text) == expected
    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


def test_a_long_argument_is_truncated_in_the_reply(bot: Harness) -> None:
    reply = bot.reply("/add AAPL " + "x" * 100)

    assert reply == f"Unknown timeframe {'x' * 32}…. Use 1h, 4h, 1d or 1w."


def test_add_explains_a_malformed_symbol(bot: Harness) -> None:
    bot.provider.fail_next(
        InvalidTickerError(InvalidTickerReason.MALFORMED, "bad characters", ticker="AAPL"),
        ticker="AAPL",
        method="validate_ticker",
    )

    assert bot.reply("/add aapl") == "aapl is not a valid symbol."


def test_add_during_a_provider_outage_says_try_later(
    bot: Harness, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    bot.provider.fail_next(
        ProviderUnavailableError(ProviderFailure.TIMEOUT, ticker="AAPL"),
        ticker="AAPL",
        method="validate_ticker",
    )

    with caplog.at_level(logging.WARNING):
        reply = bot.reply("/add AAPL")

    assert reply == "The market data provider is not answering. Try again later."
    assert "ProviderUnavailableError" in caplog.text
    with database.session() as session:
        assert repositories(session).tickers.list_all() == ()


# --- AC3: /remove ---------------------------------------------------------------------------


def test_remove_disables_every_timeframe_and_keeps_the_assignments(
    bot: Harness, database: Database
) -> None:
    seed_rules(database)
    bot.reply("/add AAPL")
    bot.reply("/add AAPL 1h")

    reply = bot.reply("/remove aapl")

    assert reply == (
        "Stopped watching AAPL (1d, 1h). Its signal history is kept; /add brings it back."
    )
    with database.session() as session:
        handles = repositories(session)
        tickers = handles.tickers.list_all()
        assert [ticker.enabled for ticker in tickers] == [False, False]
        assert all(handles.assignments.rules_for_ticker(ticker.id) for ticker in tickers)


def test_remove_with_a_timeframe_disables_only_that_one(bot: Harness, database: Database) -> None:
    bot.reply("/add AAPL")
    bot.reply("/add AAPL 1h")

    assert bot.reply("/remove AAPL 1h").startswith("Stopped watching AAPL (1h).")
    with database.session() as session:
        enabled = repositories(session).tickers.list_enabled()
    assert [(ticker.symbol, ticker.timeframe) for ticker in enabled] == [("AAPL", Timeframe.D1)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/remove", "Usage: /remove TICKER [1h|4h|1d|1w]"),
        ("/remove MSFT", "MSFT is not watched."),
        ("/remove MSFT 4h", "MSFT is not watched on 4h."),
        ("/remove AAPL 2d", "Unknown timeframe 2d. Use 1h, 4h, 1d or 1w."),
        ("/remove <b>", "&lt;B&gt; is not watched."),
    ],
)
def test_remove_explains_what_is_wrong(bot: Harness, text: str, expected: str) -> None:
    assert bot.reply(text) == expected


def test_removing_twice_says_it_was_already_stopped(bot: Harness) -> None:
    bot.reply("/add AAPL")
    bot.reply("/remove AAPL")

    assert bot.reply("/remove AAPL") == "AAPL was already stopped (1d)."


# --- AC4, AC5: /list and /rules -------------------------------------------------------------


def test_list_and_rules_when_empty(bot: Harness) -> None:
    assert bot.reply("/list") == "No symbol is watched yet. Try /add AAPL"
    assert bot.reply("/rules") == (
        "No rule is stored yet. Create one from the dashboard or the management CLI."
    )


def test_list_shows_state_and_enabled_rules(bot: Harness, database: Database) -> None:
    seed_rules(database)
    bot.reply("/add AAPL")
    bot.reply("/add SPY 1h")
    bot.reply("/remove SPY")

    assert bot.reply("/list") == (
        "<b>Watched symbols</b>\nAAPL 1d · on · 2 enabled rules\nSPY 1h · off · 1 enabled rule"
    )


def test_rules_shows_timeframe_side_state_and_usage(bot: Harness, database: Database) -> None:
    seed_rules(database)
    bot.reply("/add AAPL")

    assert bot.reply("/rules") == (
        "<b>Rules</b>\n"
        "Daily breakout · 1d · BUY · on · 1 ticker\n"
        "Daily dip · 1d · SELL · on · 1 ticker\n"
        "Daily off · 1d · BUY · off · 1 ticker\n"
        "Hourly breakout · 1h · BUY · on · 0 tickers"
    )


# --- AC6, AC7: /status, /pause and /resume -------------------------------------------------


def test_status_shows_version_runs_and_next_fires_in_the_display_zone(
    bot: Harness, database: Database
) -> None:
    bot.reply("/add AAPL")
    with database.session() as session:
        repositories(session).state.record_run(
            Timeframe.D1, datetime(2026, 2, 27, 21, 0, tzinfo=UTC)
        )

    assert bot.reply("/status") == (
        "<b>trading-bot 1.2.3</b> (prod)\n"
        "Running · 1 enabled ticker\n"
        "1h: last candle never · next run 2026-03-02 17:30 CET\n"
        "4h: last candle never · next run not scheduled\n"
        "1d: last candle 2026-02-27 22:00 CET · next run 2026-03-02 22:00 CET\n"
        "1w: last candle never · next run not scheduled"
    )


def test_pause_and_resume(bot: Harness, database: Database) -> None:
    assert bot.reply("/resume") == "The bot was not paused."
    assert bot.reply("/pause") == (
        "Paused. No candle is evaluated and no signal is sent until /resume."
    )
    assert bot.reply("/pause") == "Already paused since 2026-03-02 16:00 CET."
    assert bot.reply("/status").splitlines()[1] == (
        "Paused since 2026-03-02 16:00 CET · 0 enabled tickers"
    )
    with database.session() as session:
        assert repositories(session).state.load().paused

    assert bot.reply("/resume") == "Resumed. The next candle close will be evaluated."
    with database.session() as session:
        assert not repositories(session).state.load().paused
