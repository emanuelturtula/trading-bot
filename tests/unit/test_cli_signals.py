"""``signals list`` (spec 014, T12, AC24).

Every expected line is a literal of Design 10.3, and the command is read-only: each case that
runs it checks the full database dump, signals and bot state included, is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import (
    database_snapshot,
    frozen_clock,
    repositories,
    run_cli,
    sample_rule,
    sample_signal,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database

CLOSE = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
RECORDED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
NOTIFIED_AT = datetime(2026, 1, 2, 4, 0, 0, tzinfo=UTC)

NVDA_LINE = (
    "NVDA|1h|2|2024-01-05T15:00:00+00:00 BUY 'Hourly breakout' close 187.5"
    " (id 4, recorded 2026-01-02T03:04:05+00:00, not notified)"
)
MSFT_LINE = (
    "MSFT|1d|3|2024-01-05T05:00:00+00:00 SELL 'Evening fade' close 412.25"
    " (id 3, recorded 2026-01-02T03:04:05+00:00, not notified)"
)
AAPL_NEWER_LINE = (
    "AAPL|1d|1|2024-01-04T05:00:00+00:00 BUY 'Daily breakout' close 187.5"
    " (id 2, recorded 2026-01-02T03:04:05+00:00, notified 2026-01-02T04:00:00+00:00)"
)
AAPL_OLDER_LINE = (
    "AAPL|1d|1|2024-01-03T05:00:00+00:00 BUY 'Daily breakout' close 187.5"
    " (id 1, recorded 2026-01-02T03:04:05+00:00, not notified)"
)
ALL_LINES = [NVDA_LINE, MSFT_LINE, AAPL_NEWER_LINE, AAPL_OLDER_LINE]


def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir):
        pass
    return data_dir


def seeded(tmp_path: Path) -> Path:
    """Three tickers, three rules and four signals, one of them notified."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        _seed(database)
    return data_dir


def _seed(database: Database) -> None:
    with database.session() as session:
        tools = repositories(session)
        aapl = tools.tickers.add("AAPL", Timeframe.D1)
        msft = tools.tickers.add("MSFT", Timeframe.D1)
        nvda = tools.tickers.add("NVDA", Timeframe.H1)
        daily = tools.rules.add(sample_rule("Daily breakout"))
        hourly = tools.rules.add(sample_rule("Hourly breakout", timeframe="1h"))
        fade = tools.rules.add(sample_rule("Evening fade", signal="SELL"))
    with database.session() as session:
        tools = repositories(session, clock=frozen_clock(RECORDED_AT))
        tools.signals.record(sample_signal(aapl, daily, candle_close_ts=CLOSE))
        tools.signals.record(sample_signal(aapl, daily, candle_close_ts=CLOSE + timedelta(days=1)))
        tools.signals.record(
            sample_signal(msft, fade, candle_close_ts=CLOSE + timedelta(days=2), close_price=412.25)
        )
        tools.signals.record(
            sample_signal(nvda, hourly, candle_close_ts=CLOSE + timedelta(days=2, hours=10))
        )
    with database.session() as session:
        repositories(session, clock=frozen_clock(NOTIFIED_AT)).signals.mark_notified(2)


# --- the listing ---------------------------------------------------------------------------


def test_list_prints_nothing_on_an_empty_database(tmp_path: Path) -> None:
    data_dir = migrated(tmp_path)

    result = run_cli(["signals", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.out == ""


def test_list_prints_the_newest_signals_first(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == ALL_LINES


def test_the_listing_writes_nothing(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)
    before = database_snapshot(data_dir)

    run_cli(["signals", "list"], data_dir=data_dir)

    assert database_snapshot(data_dir) == before


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(["--ticker", "AAPL"], [AAPL_NEWER_LINE, AAPL_OLDER_LINE], id="ticker"),
        pytest.param(["--ticker", " aapl "], [AAPL_NEWER_LINE, AAPL_OLDER_LINE], id="normalized"),
        pytest.param(
            ["--ticker", "NVDA", "--timeframe", "1h"], [NVDA_LINE], id="ticker-and-timeframe"
        ),
        pytest.param(["--timeframe", "1h"], [NVDA_LINE], id="timeframe"),
        pytest.param(
            ["--timeframe", "1d"], [MSFT_LINE, AAPL_NEWER_LINE, AAPL_OLDER_LINE], id="daily"
        ),
        pytest.param(["--timeframe", "4h"], [], id="a-timeframe-without-signals"),
        pytest.param(["--rule", "Daily breakout"], [AAPL_NEWER_LINE, AAPL_OLDER_LINE], id="rule"),
        pytest.param(["--rule", "Evening fade"], [MSFT_LINE], id="another-rule"),
        pytest.param(
            ["--ticker", "AAPL", "--rule", "Daily breakout", "--limit", "1"],
            [AAPL_NEWER_LINE],
            id="everything-at-once",
        ),
        pytest.param(["--limit", "2"], ALL_LINES[:2], id="limit"),
    ],
)
def test_the_filters_select_the_expected_signals(
    tmp_path: Path, argv: list[str], expected: list[str]
) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", *argv], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == expected


def test_the_default_limit_is_twenty(tmp_path: Path) -> None:
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        with database.session() as session:
            tools = repositories(session)
            ticker = tools.tickers.add("AAPL", Timeframe.D1)
            rule = tools.rules.add(sample_rule("Daily breakout"))
        with database.session() as session:
            tools = repositories(session, clock=frozen_clock(RECORDED_AT))
            for index in range(21):
                tools.signals.record(
                    sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=index))
                )

    result = run_cli(["signals", "list"], data_dir=data_dir)

    assert result.code == 0
    assert len(result.lines) == 20
    assert "2024-01-23T05:00:00+00:00" in result.lines[0]  # the newest of the twenty-one


@pytest.mark.parametrize("limit", ["0", "501", "-1", "two", "2.5", ""])
def test_a_limit_outside_the_bounds_is_a_usage_error(tmp_path: Path, limit: str) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--limit", limit], data_dir=data_dir)

    assert result.code == 2
    assert result.err != ""


@pytest.mark.parametrize("limit", ["1", "500"])
def test_the_bounds_of_the_limit_are_accepted(tmp_path: Path, limit: str) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--limit", limit], data_dir=data_dir)

    assert result.code == 0


def test_an_unknown_ticker_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--ticker", "TSLA"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no ticker TSLA 1d is stored"]


def test_a_ticker_stored_on_another_timeframe_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--ticker", "NVDA"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no ticker NVDA 1d is stored"]


def test_an_unknown_rule_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--rule", "No such rule"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors == ["error: no rule named 'No such rule' is stored"]


def test_a_malformed_symbol_is_refused(tmp_path: Path) -> None:
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list", "--ticker", "A B"], data_dir=data_dir)

    assert result.code == 1
    assert result.errors[0].startswith("error: invalid ticker")


def test_the_listing_shows_the_rules_current_name(tmp_path: Path) -> None:
    """Decision D111: the name is read live, so a renamed rule shows its new name."""
    data_dir = seeded(tmp_path)
    with temporary_database(data_dir) as database, database.session() as session:
        tools = repositories(session)
        stored = tools.rules.get_by_name("Evening fade")
        assert stored is not None
        tools.rules.replace(stored.id, sample_rule("Evening reversal", signal="SELL"))

    result = run_cli(["signals", "list", "--rule", "Evening reversal"], data_dir=data_dir)

    assert result.code == 0
    assert result.lines == [MSFT_LINE.replace("'Evening fade'", "'Evening reversal'")]


def test_the_listing_never_prints_the_indicator_values(tmp_path: Path) -> None:
    """A line stays bounded: the values belong to the notification, not to the operator list."""
    data_dir = seeded(tmp_path)

    result = run_cli(["signals", "list"], data_dir=data_dir)

    assert "rsi(length=14)" not in result.out


# --- the configuration envelope is unchanged (AC26) ---------------------------------------


def test_the_configuration_export_is_unchanged_by_the_signals(tmp_path: Path) -> None:
    """Signals are history and the bot state is runtime state: neither is configuration."""
    without = tmp_path / "without"
    with temporary_database(without) as database, database.session() as session:
        tools = repositories(session)
        ticker = tools.tickers.add("AAPL", Timeframe.D1)
        rule = tools.rules.add(sample_rule("Daily breakout"))
        tools.assignments.assign(ticker.id, rule.id)

    with_history = tmp_path / "with-history"
    with temporary_database(with_history) as database:
        with database.session() as session:
            tools = repositories(session)
            ticker = tools.tickers.add("AAPL", Timeframe.D1)
            rule = tools.rules.add(sample_rule("Daily breakout"))
            tools.assignments.assign(ticker.id, rule.id)
        with database.session() as session:
            tools = repositories(session, clock=frozen_clock(RECORDED_AT))
            tools.signals.record(sample_signal(ticker, rule, candle_close_ts=CLOSE))
            tools.state.pause()
            tools.state.record_heartbeat()

    plain = run_cli(["config", "export", "-"], data_dir=without)
    with_signals = run_cli(["config", "export", "-"], data_dir=with_history)

    assert (plain.code, with_signals.code) == (0, 0)
    assert with_signals.out == plain.out
    assert "signals" not in with_signals.out
    assert "bot_state" not in with_signals.out
