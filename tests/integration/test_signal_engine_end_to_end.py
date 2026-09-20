"""End-to-end signal engine runs over a sequence of candle closes (spec 015, T11, AC5, AC9, AC16).

A temporary database, the fake provider over calendar-aligned frames and the fake notifier,
driven across the NYSE holiday week of spec 015 Design 5.2 (2024-07-04 is a holiday, 07-06 and
07-07 a weekend): two tickers, one rule each, one of them with ``cooldown_bars=2``. The run
includes a global pause and a process restart, and finishes by replaying every evaluated run
from the beginning to prove nothing new is ever created or sent (CLAUDE.md rule 5).

Every expectation below is written out by hand from the cooldown table of spec 015 Design 5.1,
never recomputed by calling ``cooldown_decision`` itself.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.calendars import utc
from tests.fixtures.engine import (
    EngineHarness,
    configuration,
    engine_harness,
    publish,
    run_engine,
    stored_signals,
    track,
    triggering_rule,
)
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.results import RunReport, SignalDisposition

HISTORY_START = utc("2024-05-01T00:00")
HISTORY_END = utc("2024-07-12T00:00")

# The holiday week: 2024-07-04 is a holiday, 2024-07-06/07 a weekend (spec 015 Design 5.2).
RUN_MONDAY = utc("2024-07-01T20:00:30")  # closes the candle labelled 2024-07-01
RUN_TUESDAY = utc("2024-07-02T20:00:30")  # closes the candle labelled 2024-07-02
RUN_HALF_DAY = utc("2024-07-03T17:00:30")  # would close 2024-07-03 (a half day); paused instead
RUN_FRIDAY = utc("2024-07-05T20:00:30")  # closes 2024-07-05 (07-04 is a holiday)
RUN_NEXT_MONDAY = utc("2024-07-08T20:00:30")
RUN_NEXT_TUESDAY = utc("2024-07-09T20:00:30")

# The runs actually evaluated, in the order they happened. RUN_HALF_DAY is excluded: replaying
# it after the pause is resumed is a genuinely new run for a previously unevaluated candle
# (decision D133), not a rerun, so it belongs to its own assertions below, not to the replay.
EVALUATED_RUNS = (RUN_MONDAY, RUN_TUESDAY, RUN_FRIDAY, RUN_NEXT_MONDAY, RUN_NEXT_TUESDAY)


def watch(harness: EngineHarness) -> None:
    track(harness, "AAPL", Timeframe.D1, [triggering_rule("Alpha", cooldown_bars=2)])
    track(harness, "MSFT", Timeframe.D1, [triggering_rule("Beta", cooldown_bars=0)])
    publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)
    publish(harness, "MSFT", Timeframe.D1, HISTORY_START, HISTORY_END)


def dispositions(report: RunReport) -> dict[str, SignalDisposition]:
    """One ticker's single-rule disposition, keyed by ticker symbol; empty for a paused run."""
    return {ticker.ticker: ticker.signals[0].disposition for ticker in report.tickers}


def test_a_week_with_a_pause_and_a_restart_notifies_the_right_signals_exactly_once(
    tmp_path: Path,
) -> None:
    with engine_harness(tmp_path) as harness:
        watch(harness)

        # Monday: neither rule has a previous signal, so both are ALLOWED (Design 5.1).
        assert dispositions(run_engine(harness, Timeframe.D1, RUN_MONDAY)) == {
            "AAPL": SignalDisposition.NOTIFIED,
            "MSFT": SignalDisposition.NOTIFIED,
        }
        # Tuesday: one candle since Monday. AAPL needs bars > 2 and gets BLOCKED (COOLDOWN);
        # MSFT needs bars > 0 and is ALLOWED again.
        assert dispositions(run_engine(harness, Timeframe.D1, RUN_TUESDAY)) == {
            "AAPL": SignalDisposition.COOLDOWN,
            "MSFT": SignalDisposition.NOTIFIED,
        }
        fetch_calls_before_pause = len(harness.provider.fetch_calls)
        notified_before_pause = [call.key.ticker for call in harness.notifier.calls]
        assert notified_before_pause == ["AAPL", "MSFT", "MSFT"]
        assert all(harness.notifier.lock_free)

        with configuration(harness) as repos:
            repos.state.pause()
        paused_report = run_engine(harness, Timeframe.D1, RUN_HALF_DAY)
        assert paused_report.paused is True
        assert paused_report.tickers == ()
        assert len(harness.provider.fetch_calls) == fetch_calls_before_pause  # no I/O at all
        assert len(harness.notifier.calls) == 3  # nothing new was sent while paused

        with configuration(harness) as repos:
            repos.state.resume()

    # The process restarts: a fresh provider is wired (spec 015, D124), but the database and
    # its history survive under the same data directory.
    with engine_harness(tmp_path) as harness:
        publish(harness, "AAPL", Timeframe.D1, HISTORY_START, HISTORY_END)
        publish(harness, "MSFT", Timeframe.D1, HISTORY_START, HISTORY_END)

        # Friday: AAPL's previous signal is still Monday's (Tuesday was blocked, so it never
        # became the new "latest"). bars = |{07-02, 07-03, 07-05}| = 3 > 2 -> ALLOWED. The
        # 2024-07-03 candle exists in the frame even though its own run was skipped by the
        # pause: only the *evaluation* of a candle is suppressed by U3, never its history.
        assert dispositions(run_engine(harness, Timeframe.D1, RUN_FRIDAY)) == {
            "AAPL": SignalDisposition.NOTIFIED,
            "MSFT": SignalDisposition.NOTIFIED,
        }
        # The next Monday: bars = |{07-08}| = 1 <= 2 -> BLOCKED for AAPL; MSFT is allowed again.
        assert dispositions(run_engine(harness, Timeframe.D1, RUN_NEXT_MONDAY)) == {
            "AAPL": SignalDisposition.COOLDOWN,
            "MSFT": SignalDisposition.NOTIFIED,
        }
        # The next Tuesday: bars = |{07-08, 07-09}| = 2 <= 2 -> still BLOCKED for AAPL.
        assert dispositions(run_engine(harness, Timeframe.D1, RUN_NEXT_TUESDAY)) == {
            "AAPL": SignalDisposition.COOLDOWN,
            "MSFT": SignalDisposition.NOTIFIED,
        }
        notified_after_restart = [call.key.ticker for call in harness.notifier.calls]
        assert notified_after_restart == ["AAPL", "MSFT", "MSFT", "MSFT"]
        assert all(harness.notifier.lock_free)  # AC5, on every call this file adds

        signals = stored_signals(harness)
        assert len(signals) == 7  # AAPL: Monday, Friday. MSFT: every evaluated run.
        aapl_closes = sorted(
            stored.signal.candle_close_ts.isoformat()
            for stored in signals
            if stored.signal.ticker == "AAPL"
        )
        assert aapl_closes == ["2024-07-02T04:00:00+00:00", "2024-07-06T04:00:00+00:00"]

        # Replaying every evaluated run from the beginning notifies nothing new (AC9, AC10):
        # the persisted claim, not the provider history rebuilt after the restart, is what a
        # rerun consults.
        calls_before_replay = len(harness.notifier.calls)
        replay_monday = dispositions(run_engine(harness, Timeframe.D1, RUN_MONDAY))
        replay_tuesday = dispositions(run_engine(harness, Timeframe.D1, RUN_TUESDAY))
        run_engine(harness, Timeframe.D1, RUN_FRIDAY)
        run_engine(harness, Timeframe.D1, RUN_NEXT_MONDAY)
        replay_last = dispositions(run_engine(harness, Timeframe.D1, RUN_NEXT_TUESDAY))

        # RUN_MONDAY and RUN_TUESDAY both now name a candle *older* than each pair's current
        # latest (Friday for AAPL, the next Tuesday for MSFT): the gate reports SUPERSEDED,
        # never DUPLICATE, because it never consults whether that particular old candle already
        # has a row (decision D129, the branch the unique constraint alone cannot arbitrate).
        assert replay_monday == {
            "AAPL": SignalDisposition.SUPERSEDED,
            "MSFT": SignalDisposition.SUPERSEDED,
        }
        assert replay_tuesday == {
            "AAPL": SignalDisposition.SUPERSEDED,
            "MSFT": SignalDisposition.SUPERSEDED,
        }
        # RUN_NEXT_TUESDAY is exactly each pair's current latest: AAPL was never recorded for
        # it (still COOLDOWN, recomputed identically), MSFT's own row is an exact DUPLICATE.
        assert replay_last == {
            "AAPL": SignalDisposition.COOLDOWN,
            "MSFT": SignalDisposition.DUPLICATE,
        }
        assert len(harness.notifier.calls) == calls_before_replay
        assert stored_signals(harness) == signals
