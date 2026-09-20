"""Property-based idempotency and cooldown invariants of the engine (spec 015, T12).

Every example drives the real ``SignalEngine`` over a fresh temporary database and a single
ticker/rule pair, with an arbitrarily reordered, repeated sequence of candle closes and
interleaved notifier failures. The candidate window is always small enough relative to the
rule's ``cooldown_bars`` that the fetched frame holds every candidate label (Design 5.1: a
frame shorter than ``cooldown_bars + 1`` errs towards ``BLOCKED``, which would make the
positional-gap invariant below trivially true rather than a real check).

No literal cooldown decision is asserted here (T2 and T3 already pin the table and its
look-ahead obligation): this file checks structural properties of the engine's persistence and
delivery, not the arithmetic of ``cooldown_decision`` itself.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.engine import (
    engine_harness,
    publish,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from trading_bot.domain.market_calendar.sessions import CandleSlot
from trading_bot.domain.timeframe import Timeframe
from trading_bot.notifications.notifier import NotificationError
from trading_bot.persistence.signal_records import StoredSignal

PUBLISH_START = utc("2024-05-01T00:00")
PUBLISH_END = utc("2024-08-05T00:00")
CANDIDATE_START = utc("2024-06-20T00:00")
CANDIDATE_END = utc("2024-08-01T00:00")
MAX_COOLDOWN_BARS = 4

_CALENDAR = nyse_test_calendar()
_CANDIDATE_SLOTS: tuple[CandleSlot, ...] = _CALENDAR.candle_slots(
    Timeframe.D1, CANDIDATE_START, CANDIDATE_END
)


def _now_for(slot: CandleSlot) -> datetime:
    """A wall instant safely past ``slot``'s real close (never the nominal close, spec 010)."""
    return slot.close_time + timedelta(seconds=30)


def _label_position(candle_close_ts: datetime, slots: tuple[CandleSlot, ...]) -> int:
    """The index of the candle ``candle_close_ts`` identifies among ``slots``, by its label."""
    label = candle_close_ts - Timeframe.D1.duration
    for position, slot in enumerate(slots):
        if slot.label == label:
            return position
    raise AssertionError(f"{candle_close_ts.isoformat()} does not name a candidate label")


def _assert_positional_spacing(
    signals: tuple[StoredSignal, ...], slots: tuple[CandleSlot, ...], cooldown_bars: int
) -> None:
    ordered = sorted(signals, key=lambda stored: stored.signal.candle_close_ts)
    positions = [_label_position(stored.signal.candle_close_ts, slots) for stored in ordered]
    assert positions == sorted(positions), "stored signals must already be in candle order"
    for earlier, later in pairwise(positions):
        assert later - earlier > cooldown_bars


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_reordered_and_repeated_runs_keep_the_idempotency_invariants(data: st.DataObject) -> None:
    cooldown_bars = data.draw(st.integers(min_value=0, max_value=MAX_COOLDOWN_BARS))
    history = max(2, cooldown_bars + 1)
    candidate_count = data.draw(st.integers(min_value=2, max_value=history))
    offset = data.draw(st.integers(min_value=0, max_value=len(_CANDIDATE_SLOTS) - candidate_count))
    candidates = _CANDIDATE_SLOTS[offset : offset + candidate_count]
    run_order = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=candidate_count - 1),
            min_size=1,
            max_size=2 * candidate_count,
        )
    )
    fail_flags = data.draw(
        st.lists(st.booleans(), min_size=len(run_order), max_size=len(run_order))
    )

    with tempfile.TemporaryDirectory() as raw_path, engine_harness(Path(raw_path)) as harness:
        track(
            harness,
            "AAPL",
            Timeframe.D1,
            [triggering_rule("Alpha", cooldown_bars=cooldown_bars, history=history)],
        )
        publish(harness, "AAPL", Timeframe.D1, PUBLISH_START, PUBLISH_END)

        for index, fail in zip(run_order, fail_flags, strict=True):
            if fail:
                harness.notifier.fail_next(NotificationError("a scripted delivery failure"))
            run_engine(harness, Timeframe.D1, _now_for(candidates[index]))

        signals = stored_signals(harness)
        calls = harness.notifier.calls

        # At least the first run in the order always finds no previous signal and is
        # recorded, so the invariants below are never checked against an empty run.
        assert signals != ()

        # --- no key is ever attempted twice ---
        keys = [call.key for call in calls]
        assert len(keys) == len(set(keys))

        # --- every committed row got exactly one delivery attempt, and vice versa ---
        assert {call.signal_id for call in calls} == {stored.id for stored in signals}

        # --- the positional gap between two stored signals exceeds cooldown_bars ---
        _assert_positional_spacing(signals, candidates, cooldown_bars)

        # --- AC5: no unit of work is open while any of those attempts was sent ---
        assert all(harness.notifier.lock_free)

        # --- replaying the whole sequence again changes nothing (AC9, AC10) ---
        for index in run_order:
            run_engine(harness, Timeframe.D1, _now_for(candidates[index]))
        assert harness.notifier.calls == calls
        assert stored_signals(harness) == signals
