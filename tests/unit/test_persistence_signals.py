"""The signal repository (spec 014, T5-T9, AC10-AC18).

Clocks are injected and every expected instant, message and JSON text is a literal. Raw SQL is
used only to corrupt a stored row or to read the stored text, always between sessions: with
D110 a session holds the write lock, so a second writer would wait and fail.
"""

from __future__ import annotations

import dataclasses
import math
import struct
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.fixtures.database import temporary_database
from tests.fixtures.repositories import (
    frozen_clock,
    repositories,
    sample_rule,
    sample_signal,
)
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import (
    StoredSignalError,
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownSignalError,
    UntrackedTickerError,
)
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.signals import (
    SqlSignalRepository,
    dump_indicator_values,
    load_indicator_values,
)
from trading_bot.persistence.signal_records import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RecordOutcome,
    SignalCursor,
    SignalPage,
    StoredSignal,
)

CLOSE = datetime(2024, 1, 3, 5, 0, tzinfo=UTC)
RECORDED_AT = datetime(2024, 1, 2, 21, 0, 5, tzinfo=UTC)
NOTIFIED_AT = datetime(2024, 1, 2, 21, 0, 6, tzinfo=UTC)
VALUES = {"rsi(length=14).value": 28.4, "close.value": 187.5}
VALUES_JSON = '{"rsi(length=14).value":28.4,"close.value":187.5}'


@pytest.fixture
def seeded(database: Database) -> tuple[StoredTicker, StoredRule]:
    """One daily ticker and one daily rule, committed."""
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule("Daily breakout"))
    return ticker, rule


def signals(session: Session, clock: Clock | None = None) -> SqlSignalRepository:
    return SqlSignalRepository(session, clock=clock if clock else frozen_clock(RECORDED_AT))


def stored_column(database: Database, column: str, signal_id: int = 1) -> object:
    with database.engine.connect() as connection:
        return connection.exec_driver_sql(
            f"SELECT {column} FROM signals WHERE id = ?",  # noqa: S608 - a literal column name
            (signal_id,),
        ).scalar()


def corrupt(database: Database, column: str, value: object, signal_id: int = 1) -> None:
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE signals SET {column} = ? WHERE id = ?",  # noqa: S608 - a literal name
            (value, signal_id),
        )


def bits(value: float) -> bytes:
    """The IEEE 754 bytes of a float, so ``-0.0`` and ``0.0`` compare unequal."""
    return struct.pack("<d", value)


def signal_for(
    symbol: str, timeframe: Timeframe, rule_id: str, *, candle_close_ts: datetime = CLOSE
) -> Signal:
    """A signal built by hand, for the keys ``sample_signal`` would never produce."""
    return Signal(
        ticker=symbol,
        timeframe=timeframe,
        rule_id=rule_id,
        side=Side.BUY,
        candle_close_ts=candle_close_ts,
        close_price=187.5,
    )


# --- T5: record, twice and again (AC11) ----------------------------------------------------


def test_recording_a_signal_returns_the_stored_row(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule, indicator_values=VALUES)

    with database.session() as session:
        outcome = signals(session).record(signal)

    assert isinstance(outcome, RecordOutcome)
    assert outcome.is_new is True
    stored = outcome.stored
    assert isinstance(stored, StoredSignal)
    assert stored.id == 1
    assert stored.ticker_id == ticker.id
    assert stored.rule_id == rule.id
    assert stored.rule_name == "Daily breakout"
    assert stored.signal == signal
    assert stored.created_at == RECORDED_AT
    assert stored.notified_at is None
    assert stored.key == signal.idempotency_key


def test_recording_the_same_signal_twice_in_one_session_is_idempotent(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule)

    with database.session() as session:
        repository = signals(session)
        first = repository.record(signal)
        second = repository.record(signal)

        assert first.is_new is True
        assert second.is_new is False
        assert second.stored == first.stored

    with database.session() as session:
        assert signals(session).count() == 1


def test_recording_the_same_signal_in_two_sessions_is_idempotent(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule)

    with database.session() as session:
        first = signals(session).record(signal)
    with database.session() as session:
        second = signals(session, frozen_clock(NOTIFIED_AT)).record(signal)

    assert (first.is_new, second.is_new) == (True, False)
    assert second.stored == first.stored
    assert second.stored.created_at == RECORDED_AT  # the first write's clock, not the second


def test_recording_the_same_signal_after_a_restart_is_idempotent(tmp_path: Path) -> None:
    """The unique constraint, not memory, is the arbiter (CLAUDE.md rule 5)."""
    data_dir = tmp_path / "volume"
    with temporary_database(data_dir) as database:
        with database.session() as session:
            handles = repositories(session)
            ticker = handles.tickers.add("AAPL", Timeframe.D1)
            rule = handles.rules.add(sample_rule("Daily breakout"))
        signal = sample_signal(ticker, rule)
        with database.session() as session:
            first = signals(session).record(signal)

    with temporary_database(data_dir) as database, database.session() as session:
        second = signals(session, frozen_clock(NOTIFIED_AT)).record(signal)

        assert (first.is_new, second.is_new) == (True, False)
        assert second.stored == first.stored


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"side": Side.SELL}, id="side"),
        pytest.param({"close_price": 1.25}, id="close_price"),
        pytest.param({"indicator_values": {"rsi(length=14).value": 70.0}}, id="values"),
    ],
)
def test_the_first_write_wins_whatever_the_second_payload_says(
    database: Database, seeded: tuple[StoredTicker, StoredRule], overrides: Mapping[str, object]
) -> None:
    ticker, rule = seeded
    first_signal = sample_signal(ticker, rule, indicator_values=VALUES)
    arguments: dict[str, object] = {"indicator_values": VALUES, **overrides}
    second_signal = sample_signal(ticker, rule, **arguments)  # type: ignore[arg-type]
    assert second_signal != first_signal

    with database.session() as session:
        first = signals(session).record(first_signal)
    with database.session() as session:
        second = signals(session).record(second_signal)

    assert first.is_new is True
    assert second.is_new is False
    assert second.stored == first.stored
    assert second.stored.signal == first_signal


def test_a_session_keeps_working_after_an_existing_outcome(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule)
    other = sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=1))

    with database.session() as session:
        repository = signals(session)
        repository.record(signal)
        assert repository.record(signal).is_new is False
        assert repository.record(other).is_new is True

    with database.session() as session:
        assert signals(session).count() == 2


def test_a_failed_unit_of_work_stores_no_signal(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule)

    with pytest.raises(RuntimeError, match="boom"), database.session() as session:  # noqa: PT012
        signals(session).record(signal)
        raise RuntimeError("boom")

    with database.session() as session:
        repository = signals(session)
        assert repository.count() == 0
        assert repository.record(signal).is_new is True


def test_a_signal_identifier_is_not_reused_after_a_delete(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    """Spec 014 AC3, through the repository: ids appear in cursors and references."""
    ticker, rule = seeded
    later = CLOSE + timedelta(days=1)
    with database.session() as session:
        repository = signals(session)
        repository.record(sample_signal(ticker, rule))
        second = repository.record(sample_signal(ticker, rule, candle_close_ts=later))
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM signals WHERE id = ?", (second.stored.id,))

    with database.session() as session:
        again = signals(session).record(sample_signal(ticker, rule, candle_close_ts=later))

    assert (second.stored.id, again.stored.id) == (2, 3)


def test_an_integrity_error_that_is_not_the_signal_key_is_re_raised(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    """Re-reading the key tells a duplicate from any other violation (decision D112).

    A trigger stands in for a constraint the repository cannot foresee: the key is free, so no
    stored row explains the failure and it must reach the caller instead of being reported as
    an existing signal.
    """
    ticker, rule = seeded
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_signals BEFORE INSERT ON signals"
            " BEGIN SELECT RAISE(ABORT, 'refused by a trigger'); END"
        )

    with (
        pytest.raises(IntegrityError, match="refused by a trigger"),
        database.session() as session,
    ):
        signals(session).record(sample_signal(ticker, rule))

    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM signals").scalar() == 0


# --- T5: the key mapping and its rejections (AC12) -----------------------------------------


def test_the_ticker_is_resolved_by_symbol_and_timeframe(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        daily = handles.tickers.add("AAPL", Timeframe.D1)
        hourly = handles.tickers.add("AAPL", Timeframe.H1)
        daily_rule = handles.rules.add(sample_rule("Daily breakout"))
        hourly_rule = handles.rules.add(sample_rule("Hourly breakout", timeframe="1h"))

    with database.session() as session:
        repository = signals(session)
        on_daily = repository.record(sample_signal(daily, daily_rule))
        on_hourly = repository.record(sample_signal(hourly, hourly_rule))

    assert on_daily.stored.ticker_id == daily.id
    assert on_hourly.stored.ticker_id == hourly.id
    assert on_hourly.stored.signal.timeframe is Timeframe.H1


def test_an_untracked_ticker_is_refused_and_leaves_the_session_usable(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    unknown = signal_for("TSLA", Timeframe.D1, str(rule.id))

    with database.session() as session:
        repository = signals(session)
        with pytest.raises(UntrackedTickerError, match="ticker TSLA 1d is not tracked"):
            repository.record(unknown)

        assert repository.count() == 0
        assert repository.record(sample_signal(ticker, rule)).is_new is True


def test_a_symbol_tracked_on_another_timeframe_only_is_untracked(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, _ = seeded
    with database.session() as session:
        hourly_rule = repositories(session).rules.add(
            sample_rule("Hourly breakout", timeframe="1h")
        )
    hourly = signal_for(ticker.symbol, Timeframe.H1, str(hourly_rule.id))

    with database.session() as session, pytest.raises(UntrackedTickerError) as caught:
        signals(session).record(hourly)

    assert str(caught.value) == "ticker AAPL 1h is not tracked"


def test_an_unknown_rule_is_refused(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = signal_for(ticker.symbol, ticker.timeframe, str(rule.id + 98))

    with database.session() as session:
        repository = signals(session)
        with pytest.raises(UnknownRuleError, match="no rule with id 99"):
            repository.record(signal)

        assert repository.count() == 0
        assert repository.record(sample_signal(ticker, rule)).is_new is True


def test_a_rule_of_another_timeframe_is_refused(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, daily_rule = seeded
    with database.session() as session:
        hourly_rule = repositories(session).rules.add(
            sample_rule("Hourly breakout", timeframe="1h")
        )
    signal = signal_for(ticker.symbol, ticker.timeframe, str(hourly_rule.id))

    with database.session() as session:
        repository = signals(session)
        with pytest.raises(TimeframeMismatchError) as caught:
            repository.record(signal)

        assert repository.count() == 0
        assert repository.record(sample_signal(ticker, daily_rule)).is_new is True

    assert str(caught.value) == "the rule is evaluated on 1h, but the ticker is tracked on 1d"


@pytest.mark.parametrize(
    "rule_id",
    ["042", "+42", "0", "-1", "rule-7", "9" * 64, "1.0", "0x1", str(2**63)],
)
def test_a_rule_id_that_is_not_a_canonical_identifier_is_rejected(
    database: Database, seeded: tuple[StoredTicker, StoredRule], rule_id: str
) -> None:
    ticker, rule = seeded
    signal = signal_for(ticker.symbol, ticker.timeframe, rule_id)

    with database.session() as session:
        repository = signals(session)
        with pytest.raises(ValueError, match="rule_id") as caught:
            repository.record(signal)

        assert repository.count() == 0
        assert repository.record(sample_signal(ticker, rule)).is_new is True

    message = str(caught.value)
    assert message.startswith("rule_id must be the decimal identifier of a stored rule, got ")
    assert rule_id[:40] in message
    assert len(message) <= 150  # the echo of the rejected text is bounded (spec 013, 6.2)


def test_anything_that_is_not_a_signal_is_rejected(database: Database) -> None:
    with database.session() as session, pytest.raises(TypeError, match="Signal"):
        signals(session).record("AAPL|1d|1|2024-01-03T05:00:00+00:00")  # type: ignore[arg-type]


# --- T5: the payload round trip (AC13) -----------------------------------------------------


def test_the_candle_close_is_stored_exactly_as_the_signal_carries_it(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    """An instant off every candle grid, with microseconds, is never snapped (D114)."""
    ticker, rule = seeded
    off_grid = datetime(2024, 1, 3, 5, 17, 42, 123456, tzinfo=UTC)
    signal = sample_signal(ticker, rule, candle_close_ts=off_grid)

    with database.session() as session:
        stored = signals(session).record(signal).stored

    assert stored.signal.candle_close_ts == off_grid
    assert stored_column(database, "candle_close_ts") == "2024-01-03 05:17:42.123456"


def test_a_close_in_another_zone_is_stored_as_the_same_utc_instant(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    eastern = timezone(timedelta(hours=-5))
    signal = sample_signal(ticker, rule, candle_close_ts=datetime(2024, 1, 3, 0, 0, tzinfo=eastern))

    with database.session() as session:
        stored = signals(session).record(signal).stored

    assert stored.signal.candle_close_ts == CLOSE
    assert stored.signal.candle_close_ts.tzinfo is UTC


def test_every_value_of_the_payload_round_trips_bit_for_bit(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    extreme = {
        "smallest.value": 5e-324,
        "negative_zero.value": -0.0,
        "largest.value": 1.7976931348623157e308,
        "precise.value": -1.2345678901234567e15,
    }
    signal = sample_signal(
        ticker, rule, close_price=1.7976931348623157e308, indicator_values=extreme
    )

    with database.session() as session:
        signals(session).record(signal)
    with database.session() as session:
        stored = signals(session).get(1)

    assert stored is not None
    assert bits(stored.signal.close_price) == bits(1.7976931348623157e308)
    assert list(stored.signal.indicator_values) == list(extreme)
    for name, value in extreme.items():
        assert bits(stored.signal.indicator_values[name]) == bits(value), name


def test_the_stored_json_text_is_the_canonical_one(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    signal = sample_signal(ticker, rule, indicator_values=VALUES)

    with database.session() as session:
        signals(session).record(signal)

    assert stored_column(database, "indicator_values_json") == VALUES_JSON


def test_an_empty_payload_round_trips(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded

    with database.session() as session:
        stored = signals(session).record(sample_signal(ticker, rule, indicator_values={})).stored

    assert dict(stored.signal.indicator_values) == {}
    assert stored_column(database, "indicator_values_json") == "{}"


def test_the_codec_keeps_non_ascii_names_readable() -> None:
    """``ensure_ascii=False``: a manual ``sqlite3`` session shows the name, not an escape."""
    text = dump_indicator_values({"média.value": 1.5})

    assert text == '{"média.value":1.5}'
    assert load_indicator_values(1, text) == {"média.value": 1.5}


# --- T6: delivery state (AC14) -------------------------------------------------------------


def test_a_recorded_signal_is_not_notified(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded

    with database.session() as session:
        stored = signals(session).record(sample_signal(ticker, rule)).stored

    assert stored.notified_at is None
    assert stored_column(database, "notified_at") is None


def test_mark_notified_stamps_the_clock_once(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        signals(session).record(sample_signal(ticker, rule))

    with database.session() as session:
        updated = signals(session, frozen_clock(NOTIFIED_AT)).mark_notified(1)
    with database.session() as session:
        again = signals(session, frozen_clock(NOTIFIED_AT + timedelta(days=1))).mark_notified(1)

    assert updated.notified_at == NOTIFIED_AT
    assert again.notified_at == NOTIFIED_AT
    assert again == updated


def test_mark_notified_changes_no_other_column(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    columns = (
        "id, ticker_id, rule_id, timeframe, candle_close_ts, side, close_price,"
        " indicator_values_json, created_at"
    )
    with database.session() as session:
        signals(session).record(sample_signal(ticker, rule, indicator_values=VALUES))
    with database.engine.connect() as connection:
        before = connection.exec_driver_sql(f"SELECT {columns} FROM signals").all()  # noqa: S608

    with database.session() as session:
        signals(session, frozen_clock(NOTIFIED_AT)).mark_notified(1)

    with database.engine.connect() as connection:
        after = connection.exec_driver_sql(f"SELECT {columns} FROM signals").all()  # noqa: S608
    assert after == before
    assert stored_column(database, "notified_at") == "2024-01-02 21:00:06.000000"


def test_marking_an_unknown_signal_is_refused(database: Database) -> None:
    with database.session() as session, pytest.raises(UnknownSignalError) as caught:
        signals(session).mark_notified(404)

    assert str(caught.value) == "no signal with id 404"


# --- T7: latest (AC15) ---------------------------------------------------------------------


def test_latest_returns_the_greatest_candle_close_whatever_the_insertion_order(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        repository = signals(session)
        repository.record(sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=2)))
        repository.record(sample_signal(ticker, rule, candle_close_ts=CLOSE))
        repository.record(sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=1)))

    with database.session() as session:
        found = signals(session).latest(ticker.id, rule.id)

    assert found is not None
    assert found.signal.candle_close_ts == CLOSE + timedelta(days=2)


def test_latest_ignores_every_other_pair(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        msft = handles.tickers.add("MSFT", Timeframe.D1)
        first = handles.rules.add(sample_rule("First"))
        second = handles.rules.add(sample_rule("Second"))
    with database.session() as session:
        repository = signals(session)
        repository.record(sample_signal(aapl, first, candle_close_ts=CLOSE))
        repository.record(sample_signal(msft, first, candle_close_ts=CLOSE + timedelta(days=5)))
        repository.record(sample_signal(aapl, second, candle_close_ts=CLOSE + timedelta(days=5)))

    with database.session() as session:
        found = signals(session).latest(aapl.id, first.id)

    assert found is not None
    assert found.signal.candle_close_ts == CLOSE
    assert (found.ticker_id, found.rule_id) == (aapl.id, first.id)


def test_latest_can_be_restricted_to_notified_signals(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        repository = signals(session)
        older = repository.record(sample_signal(ticker, rule, candle_close_ts=CLOSE)).stored
        repository.record(sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=1)))
        repository.mark_notified(older.id)

    with database.session() as session:
        repository = signals(session)
        any_signal = repository.latest(ticker.id, rule.id)
        notified = repository.latest(ticker.id, rule.id, notified_only=True)

    assert any_signal is not None
    assert notified is not None
    assert any_signal.signal.candle_close_ts == CLOSE + timedelta(days=1)
    assert notified.signal.candle_close_ts == CLOSE


def test_latest_is_none_when_the_pair_has_no_signal(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded

    with database.session() as session:
        repository = signals(session)

        assert repository.latest(ticker.id, rule.id) is None
        assert repository.latest(404, 404) is None
        repository.record(sample_signal(ticker, rule))
        assert repository.latest(ticker.id, rule.id, notified_only=True) is None


# --- T8: history and count (AC16, AC17) ----------------------------------------------------

# Seven signals whose ids are 1 to 7 in this order; two of them share a candle close, so the
# ``id DESC`` tie-breaker is exercised, and one is hourly.
HISTORY_ORDER = [7, 3, 2, 6, 5, 4, 1]


def seed_history(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        aapl = handles.tickers.add("AAPL", Timeframe.D1)
        msft = handles.tickers.add("MSFT", Timeframe.D1)
        hourly_ticker = handles.tickers.add("NVDA", Timeframe.H1)
        first = handles.rules.add(sample_rule("First"))
        second = handles.rules.add(sample_rule("Second"))
        hourly_rule = handles.rules.add(sample_rule("Hourly", timeframe="1h"))
    with database.session() as session:
        repository = signals(session)
        for ticker, rule, days in (
            (aapl, first, 0),
            (aapl, first, 3),
            (msft, first, 3),
            (aapl, second, 1),
            (msft, second, 2),
            (hourly_ticker, hourly_rule, 2),
            (aapl, first, 5),
        ):
            repository.record(
                sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=days))
            )
        repository.mark_notified(2)
        repository.mark_notified(6)


def identifiers(page: SignalPage) -> list[int]:
    return [stored.id for stored in page.items]


def test_history_orders_by_candle_close_then_identifier_descending(database: Database) -> None:
    seed_history(database)

    with database.session() as session:
        page = signals(session).history()

    assert identifiers(page) == HISTORY_ORDER
    assert page.next_cursor is None
    assert isinstance(page, SignalPage)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param({"ticker_id": 1}, [7, 2, 4, 1], id="ticker"),
        pytest.param({"rule_id": 1}, [7, 3, 2, 1], id="rule"),
        pytest.param({"timeframe": Timeframe.H1}, [6], id="timeframe"),
        pytest.param({"timeframe": Timeframe.D1}, [7, 3, 2, 5, 4, 1], id="daily"),
        pytest.param({"notified": True}, [2, 6], id="notified"),
        pytest.param({"notified": False}, [7, 3, 5, 4, 1], id="not-notified"),
        pytest.param({"since": CLOSE + timedelta(days=2)}, [7, 3, 2, 6, 5], id="since-inclusive"),
        pytest.param({"until": CLOSE + timedelta(days=3)}, [6, 5, 4, 1], id="until-exclusive"),
        pytest.param(
            {"since": CLOSE + timedelta(days=2), "until": CLOSE + timedelta(days=3)},
            [6, 5],
            id="range",
        ),
        pytest.param({"ticker_id": 1, "rule_id": 1}, [7, 2, 1], id="ticker-and-rule"),
        pytest.param(
            {"ticker_id": 1, "rule_id": 1, "since": CLOSE + timedelta(days=1)},
            [7, 2],
            id="ticker-rule-and-since",
        ),
        pytest.param({"ticker_id": 404}, [], id="unknown-ticker"),
        pytest.param({"since": CLOSE + timedelta(days=9)}, [], id="since-after-the-last"),
        pytest.param({"since": CLOSE, "until": CLOSE}, [], id="empty-range"),
    ],
)
def test_history_filters(
    database: Database, filters: Mapping[str, object], expected: list[int]
) -> None:
    seed_history(database)

    with database.session() as session:
        page = signals(session).history(**filters)  # type: ignore[arg-type]

    assert identifiers(page) == expected


@pytest.mark.parametrize("size", [1, 2, 3, 7, 50])
def test_paginating_with_the_cursor_returns_every_row_exactly_once(
    database: Database, size: int
) -> None:
    seed_history(database)
    seen: list[int] = []
    cursor: SignalCursor | None = None
    pages = 0

    with database.session() as session:
        repository = signals(session)
        while True:
            page = repository.history(limit=size, before=cursor)
            seen.extend(identifiers(page))
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break

    assert seen == HISTORY_ORDER
    assert pages == math.ceil(len(HISTORY_ORDER) / size)


def test_a_page_that_ends_exactly_on_the_last_row_carries_no_cursor(database: Database) -> None:
    seed_history(database)

    with database.session() as session:
        page = signals(session).history(limit=len(HISTORY_ORDER))

    assert identifiers(page) == HISTORY_ORDER
    assert page.next_cursor is None


def test_a_cursor_points_at_the_last_item_of_its_page(database: Database) -> None:
    seed_history(database)

    with database.session() as session:
        page = signals(session).history(limit=2)

    assert page.next_cursor is not None
    assert page.next_cursor == SignalCursor.of(page.items[-1])
    assert page.next_cursor.id == 3


def test_a_row_recorded_between_pages_appears_only_if_it_sorts_after_the_cursor(
    database: Database,
) -> None:
    seed_history(database)
    with database.session() as session:
        handles = repositories(session)
        aapl = handles.tickers.get_by_symbol("AAPL", Timeframe.D1)
        rule = handles.rules.get_by_name("First")
    assert aapl is not None
    assert rule is not None

    with database.session() as session:
        first_page = signals(session).history(limit=2)
    with database.session() as session:
        repository = signals(session)
        repository.record(  # newer than the cursor: the iteration already passed that position
            sample_signal(aapl, rule, candle_close_ts=CLOSE + timedelta(days=4))
        )
        repository.record(  # older than the cursor: it appears in a later page
            sample_signal(aapl, rule, candle_close_ts=CLOSE + timedelta(days=1, hours=12))
        )
    with database.session() as session:
        rest: list[int] = []
        cursor = first_page.next_cursor
        repository = signals(session)
        while cursor is not None:
            page = repository.history(limit=2, before=cursor)
            rest.extend(identifiers(page))
            cursor = page.next_cursor

    assert identifiers(first_page) == [7, 3]
    assert rest == [2, 6, 5, 9, 4, 1]  # 9 is the older one; the newer 8 is never seen
    seen = identifiers(first_page) + rest
    assert len(set(seen)) == len(seen)


def test_the_default_page_size_is_fifty(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule("Daily breakout"))
    with database.session() as session:
        repository = signals(session)
        for index in range(DEFAULT_PAGE_SIZE + 1):
            repository.record(
                sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=index))
            )

    with database.session() as session:
        page = signals(session).history()

    assert DEFAULT_PAGE_SIZE == 50
    assert len(page.items) == DEFAULT_PAGE_SIZE
    assert page.next_cursor is not None


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
def test_a_limit_outside_the_bounds_is_rejected(database: Database, limit: int) -> None:
    with database.session() as session, pytest.raises(ValueError, match="limit"):
        signals(session).history(limit=limit)


@pytest.mark.parametrize("limit", [True, "5", 2.0, None])
def test_a_limit_that_is_not_an_integer_is_rejected(database: Database, limit: object) -> None:
    with database.session() as session, pytest.raises(TypeError, match="limit"):
        signals(session).history(limit=limit)  # type: ignore[arg-type]


def test_the_largest_page_size_is_five_hundred(database: Database) -> None:
    assert MAX_PAGE_SIZE == 500
    with database.session() as session:
        assert signals(session).history(limit=MAX_PAGE_SIZE).items == ()


@pytest.mark.parametrize("bound", ["since", "until"])
def test_a_naive_bound_is_rejected(database: Database, bound: str) -> None:
    naive = datetime(2024, 1, 3, 5, 0)  # a naive instant is the point of the case

    with database.session() as session, pytest.raises(ValueError, match="naive"):
        signals(session).history(**{bound: naive})  # type: ignore[arg-type]


def test_a_range_that_ends_before_it_starts_is_rejected(database: Database) -> None:
    with database.session() as session, pytest.raises(ValueError, match="since"):
        signals(session).history(since=CLOSE + timedelta(days=1), until=CLOSE)


def test_a_cursor_with_a_naive_instant_cannot_be_built() -> None:
    naive = datetime(2024, 1, 3, 5, 0)  # a naive instant is the point of the case

    with pytest.raises(ValueError, match="naive"):
        SignalCursor(candle_close_ts=naive, id=1)


def test_a_cursor_normalizes_its_instant_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))

    cursor = SignalCursor(candle_close_ts=datetime(2024, 1, 3, 0, 0, tzinfo=eastern), id=1)

    assert cursor.candle_close_ts == CLOSE
    assert cursor.candle_close_ts.tzinfo is UTC


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param({}, 7, id="all"),
        pytest.param({"ticker_id": 1}, 4, id="ticker"),
        pytest.param({"rule_id": 1}, 4, id="rule"),
        pytest.param({"ticker_id": 1, "rule_id": 1}, 3, id="both"),
        pytest.param({"ticker_id": 404}, 0, id="unknown-ticker"),
        pytest.param({"rule_id": 404}, 0, id="unknown-rule"),
    ],
)
def test_count_matches_the_history(
    database: Database, filters: Mapping[str, int], expected: int
) -> None:
    seed_history(database)

    with database.session() as session:
        assert signals(session).count(**filters) == expected


# --- T9: a corrupt row fails loudly (AC18) -------------------------------------------------

CORRUPT_VALUES = [
    pytest.param("not json at all", id="not-json"),
    pytest.param("[1, 2]", id="an-array"),
    pytest.param('"a string"', id="a-string"),
    pytest.param("null", id="null"),
    pytest.param('{"a": NaN}', id="nan"),
    pytest.param('{"a": Infinity}', id="infinity"),
    pytest.param('{"a": 1e400}', id="overflowing-literal"),
    pytest.param('{"a": "1.5"}', id="a-string-value"),
    pytest.param('{"a": true}', id="a-boolean-value"),
    pytest.param('{"a": null}', id="a-null-value"),
    pytest.param('{"a": [1]}', id="a-nested-value"),
    pytest.param('{"a": 1' + "0" * 400 + "}", id="an-overflowing-integer"),
]


@pytest.fixture
def recorded(database: Database, seeded: tuple[StoredTicker, StoredRule]) -> tuple[int, int]:
    ticker, rule = seeded
    with database.session() as session:
        signals(session).record(sample_signal(ticker, rule, indicator_values=VALUES))
    return ticker.id, rule.id


@pytest.mark.parametrize("text", CORRUPT_VALUES)
def test_a_corrupt_payload_makes_every_read_fail_loudly(
    database: Database, recorded: tuple[int, int], text: str
) -> None:
    ticker_id, rule_id = recorded
    corrupt(database, "indicator_values_json", text)

    with database.session() as session:
        repository = signals(session)
        reads = (
            lambda: repository.get(1),
            lambda: repository.latest(ticker_id, rule_id),
            lambda: repository.history(),
        )
        for read in reads:
            with pytest.raises(StoredSignalError) as caught:
                read()
            assert str(caught.value) == "the stored signal 1 is not valid: indicator_values"
            assert caught.value.signal_id == 1
            assert caught.value.kind == "indicator_values"


def test_a_corrupt_payload_never_reaches_the_message_or_the_traceback(
    database: Database, recorded: tuple[int, int]
) -> None:
    """Decision D93: a stored text must not leak into a log record or a traceback."""
    corrupt(database, "indicator_values_json", '{"leaked-name": "not a number"}')

    with database.session() as session, pytest.raises(StoredSignalError) as caught:
        signals(session).get(1)

    rendered = "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "leaked-name" not in rendered
    assert "leaked-name" not in str(caught.value)


def test_a_payload_the_domain_refuses_is_reported_as_such(
    database: Database, recorded: tuple[int, int]
) -> None:
    """An infinite close price passes the database's ``close_price > 0`` but not ``Signal``."""
    corrupt(database, "close_price", math.inf)

    with database.session() as session, pytest.raises(StoredSignalError) as caught:
        signals(session).get(1)

    assert str(caught.value) == "the stored signal 1 is not valid: payload"
    assert caught.value.kind == "payload"
    assert caught.value.__cause__ is None


def test_a_corrupt_row_is_never_skipped_in_a_page(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        repository = signals(session)
        for days in range(3):
            repository.record(
                sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=days))
            )
    corrupt(database, "indicator_values_json", "{", signal_id=2)

    with database.session() as session, pytest.raises(StoredSignalError, match="signal 2"):
        signals(session).history(limit=3)


def test_a_healthy_row_still_loads_while_another_is_corrupt(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        repository = signals(session)
        for days in range(2):
            repository.record(
                sample_signal(ticker, rule, candle_close_ts=CLOSE + timedelta(days=days))
            )
    corrupt(database, "indicator_values_json", "{", signal_id=1)

    with database.session() as session:
        healthy = signals(session).get(2)

    assert healthy is not None
    assert healthy.id == 2


def test_the_codec_rejects_the_same_texts_outside_a_session() -> None:
    """The codec is the one place that decides what a stored payload may be (D119)."""
    with pytest.raises(StoredSignalError, match="the stored signal 7 is not valid"):
        load_indicator_values(7, "{oops}")
    assert load_indicator_values(7, '{"a":1,"b":2.5}') == {"a": 1.0, "b": 2.5}
    assert list(load_indicator_values(7, '{"b":1,"a":2}')) == ["b", "a"]


# --- T5: the records and the unit of work (AC10) -------------------------------------------


@pytest.mark.parametrize(
    "record",
    [StoredSignal, RecordOutcome, SignalCursor, SignalPage],
    ids=lambda kind: kind.__name__,
)
def test_every_signal_record_is_a_frozen_slotted_keyword_only_dataclass(
    record: type[object],
) -> None:
    parameters = dataclasses.fields(record)  # type: ignore[arg-type]

    assert dataclasses.is_dataclass(record)
    assert record.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert record.__dataclass_params__.kw_only is True  # type: ignore[attr-defined]
    assert "__slots__" in vars(record)
    assert parameters != ()


def test_a_stored_signal_rejects_attribute_assignment(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    ticker, rule = seeded
    with database.session() as session:
        stored = signals(session).record(sample_signal(ticker, rule)).stored

    with pytest.raises(dataclasses.FrozenInstanceError):
        stored.id = 7  # type: ignore[misc]


def test_the_repository_never_commits(
    database: Database, seeded: tuple[StoredTicker, StoredRule]
) -> None:
    """Leaving the session without the context manager's commit must store nothing (AC10)."""
    ticker, rule = seeded
    session = database.session_factory()
    try:
        signals(session).record(sample_signal(ticker, rule))
        signals(session).mark_notified(1)
    finally:
        session.rollback()
        session.close()

    with database.session() as session:
        assert signals(session).count() == 0
