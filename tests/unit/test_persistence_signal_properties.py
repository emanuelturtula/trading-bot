"""Property tests for the signal repository (spec 014, T15, AC11, AC13, AC15, AC16).

Complements the developer's curated cases (``test_persistence_signals.py``, T5-T9) with signals
drawn over a much larger space: candle closes in several zones with microseconds, extreme finite
prices and indicator values (Hypothesis tries the boundaries on its own: subnormals, the largest
finite float, ``-0.0``, ...). Mirrors ``test_persistence_repository_properties.py``'s own
pattern: every example opens its own throwaway, migrated database, because Hypothesis reuses one
function-scoped fixture instance across every example of a test node, and a database shared
across draws would leak rows from one example into the next and silently break an ordering or an
idempotency property.
"""

from __future__ import annotations

import shutil
import string
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.repositories import repositories, sample_rule, sample_signal
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.database import Database, open_database
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.signal_records import SignalCursor

# Short: every example opens and migrates its own database (mirrors
# test_persistence_repository_properties.py, which mirrors spec 012, T11).
_SETTINGS = {"max_examples": 20, "deadline": None}

# 1970-01-01 to 2100-01-01, in whatever zone Hypothesis' tzdata-backed catalogue draws: named
# zones with DST, fixed offsets and UTC itself, with microseconds (mirrors
# test_persistence_repository_properties.py's INSTANTS).
CLOSES = st.datetimes(
    min_value=datetime(1970, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.timezones()
)

# Any positive finite float, subnormals and the largest finite value included: Hypothesis' float
# strategy is biased towards its own boundary examples, so this needs no hand-picked list to
# reach 5e-324 or 1.7976931348623157e308 (D119 is pinned by literal, developer-owned cases; this
# is the generative complement).
POSITIVE_FLOATS = st.floats(min_value=0.0, exclude_min=True, allow_nan=False, allow_infinity=False)
ANY_FINITE_FLOAT = st.floats(allow_nan=False, allow_infinity=False)
INDICATOR_NAMES = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8)
INDICATOR_VALUES = st.dictionaries(keys=INDICATOR_NAMES, values=ANY_FINITE_FLOAT, max_size=4)


@contextmanager
def _throwaway_database() -> Iterator[Database]:
    """A freshly migrated database under its own directory, disposed and removed on exit."""
    directory = Path(tempfile.mkdtemp(prefix="tb-signal-properties-"))
    database = open_database(directory, busy_timeout_ms=200)
    try:
        yield database
    finally:
        database.dispose()
        shutil.rmtree(directory, ignore_errors=True)


def _seed(database: Database) -> tuple[StoredTicker, StoredRule]:
    """One daily ticker and one daily rule, committed."""
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        rule = handles.rules.add(sample_rule("Property rule"))
    return ticker, rule


# --- record then get is a fixed point, over the whole drawn space ---------------------------


@given(
    close=CLOSES, side=st.sampled_from(list(Side)), price=POSITIVE_FLOATS, values=INDICATOR_VALUES
)
@settings(**_SETTINGS)
def test_record_then_get_is_a_fixed_point_after_normalizing_to_utc(
    close: datetime, side: Side, price: float, values: dict[str, float]
) -> None:
    with _throwaway_database() as database:
        ticker, rule = _seed(database)
        signal = Signal(
            ticker=ticker.symbol,
            timeframe=ticker.timeframe,
            rule_id=str(rule.id),
            side=side,
            candle_close_ts=close,
            close_price=price,
            indicator_values=values,
        )
        with database.session() as session:
            stored_id = SqlSignalRepository(session).record(signal).stored.id
        with database.session() as session:
            reloaded = SqlSignalRepository(session).get(stored_id)

    assert reloaded is not None
    assert reloaded.ticker_id == ticker.id
    assert reloaded.rule_id == rule.id
    assert reloaded.signal == signal
    assert reloaded.signal.candle_close_ts == to_utc(close)
    assert reloaded.signal.candle_close_ts.tzinfo is UTC


# --- a drawn sequence of recordings leaves exactly the distinct keys, first payload wins -----

# A small, fixed pool: drawing from it repeatedly makes key collisions frequent, which is the
# whole point of this property (an empty or unique-only pool would never exercise "first write
# wins" at all).
_CLOSE_POOL = [
    datetime(2024, 1, 1, tzinfo=UTC),
    datetime(2024, 1, 2, tzinfo=UTC),
    datetime(2024, 1, 3, tzinfo=UTC),
]

_RECORDING = st.tuples(
    st.sampled_from(_CLOSE_POOL), st.sampled_from(list(Side)), POSITIVE_FLOATS, INDICATOR_VALUES
)


@given(recordings=st.lists(_RECORDING, min_size=1, max_size=10))
@settings(**_SETTINGS)
def test_a_drawn_sequence_of_recordings_keeps_each_keys_first_payload(
    recordings: list[tuple[datetime, Side, float, dict[str, float]]],
) -> None:
    with _throwaway_database() as database:
        ticker, rule = _seed(database)
        first_by_close: dict[datetime, Signal] = {}
        with database.session() as session:
            repository = SqlSignalRepository(session)
            for close, side, price, values in recordings:
                signal = Signal(
                    ticker=ticker.symbol,
                    timeframe=ticker.timeframe,
                    rule_id=str(rule.id),
                    side=side,
                    candle_close_ts=close,
                    close_price=price,
                    indicator_values=values,
                )
                outcome = repository.record(signal)
                key = signal.candle_close_ts
                if key not in first_by_close:
                    first_by_close[key] = signal
                    assert outcome.is_new is True
                else:
                    assert outcome.is_new is False
                    assert outcome.stored.signal == first_by_close[key]

        with database.session() as session:
            page = SqlSignalRepository(session).history(limit=500)

    stored_by_close = {item.signal.candle_close_ts: item.signal for item in page.items}
    assert set(stored_by_close) == set(first_by_close)
    for close, expected in first_by_close.items():
        assert stored_by_close[close] == expected


# --- pagination under drawn page sizes and drawn inserts between pages ----------------------


@given(
    pre_closes=st.lists(CLOSES, min_size=1, max_size=10, unique=True),
    page_size=st.integers(min_value=1, max_value=12),
    extra_closes=st.lists(CLOSES, min_size=0, max_size=4, unique=True),
)
@settings(**_SETTINGS)
def test_pagination_never_repeats_a_row_and_returns_every_pre_existing_row_once(
    pre_closes: list[datetime], page_size: int, extra_closes: list[datetime]
) -> None:
    with _throwaway_database() as database:
        ticker, rule = _seed(database)

        pre_ids: set[int] = set()
        with database.session() as session:
            repository = SqlSignalRepository(session)
            for close in pre_closes:
                outcome = repository.record(sample_signal(ticker, rule, candle_close_ts=close))
                pre_ids.add(outcome.stored.id)

        seen: list[int] = []
        cursor: SignalCursor | None = None
        pending_extra = list(extra_closes)
        while True:
            with database.session() as session:
                page = SqlSignalRepository(session).history(limit=page_size, before=cursor)
            seen.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if pending_extra:
                close = pending_extra.pop()
                with database.session() as session:
                    SqlSignalRepository(session).record(
                        sample_signal(ticker, rule, candle_close_ts=close)
                    )
            if cursor is None:
                break

    counts = Counter(seen)
    assert all(count == 1 for count in counts.values()), counts
    assert pre_ids <= set(seen)


# --- latest equals Python's own maximum of the drawn closes ---------------------------------


@given(closes=st.lists(CLOSES, min_size=1, max_size=15, unique=True))
@settings(**_SETTINGS)
def test_latest_equals_pythons_maximum_of_the_drawn_closes(closes: list[datetime]) -> None:
    with _throwaway_database() as database:
        ticker, rule = _seed(database)
        with database.session() as session:
            repository = SqlSignalRepository(session)
            for close in closes:
                repository.record(sample_signal(ticker, rule, candle_close_ts=close))
        with database.session() as session:
            found = SqlSignalRepository(session).latest(ticker.id, rule.id)

    assert found is not None
    assert found.signal.candle_close_ts == max(to_utc(close) for close in closes)
