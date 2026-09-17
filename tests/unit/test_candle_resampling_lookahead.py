"""Anti look-ahead tests of ``resample_hourly_to_4h`` (spec 011, T3, AC7; CLAUDE.md rule 4).

A ``4h`` row labelled at its slot open is only complete when the slot closes, so the harness
compares rows at the moment they become available: each row is stamped with the first prefix
label whose ``1h`` slot closes at or after the close of the row's ``4h`` slot (its completion
stamp). The harness runs as a full sweep on a gap-free and a gapped frame, two controls that
cheat must fail with the stated kinds, and a ``now`` sweep checks that no row published after
``now`` is read and that returned rows are final. Comparisons are exact.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar, utc
from tests.fixtures.session_candles import session_candles
from tests.lookahead import LookaheadError, LookaheadReport, assert_no_lookahead
from trading_bot.domain.candle_resampling import resample_hourly_to_4h
from trading_bot.domain.timeframe import Timeframe

NYSE = nyse_test_calendar()
H1 = Timeframe.H1
H4 = Timeframe.H4
START = utc("2024-11-20T00:00")
END = utc("2024-12-10T00:00")
CANONICAL_INDEX = pd.DatetimeTZDtype(unit="us", tz="UTC")

type Resample = Callable[[pd.DataFrame], pd.DataFrame]


def gap_free_frame() -> pd.DataFrame:
    return session_candles(NYSE, H1, START, END)


def gapped_frame() -> pd.DataFrame:
    frame = gap_free_frame()
    removed = pd.DatetimeIndex(
        [utc("2024-11-27T16:30"), utc("2024-11-29T17:30"), utc("2024-12-02T20:30")],
        dtype=CANONICAL_INDEX,
    )
    labels = pd.DatetimeIndex(frame.index)
    session_2024_12_04 = (labels >= pd.Timestamp(utc("2024-12-04T00:00"))) & (
        labels < pd.Timestamp(utc("2024-12-05T00:00"))
    )
    return frame[~labels.isin(removed) & ~np.asarray(session_2024_12_04, dtype=np.bool_)]


def hourly_close(label: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(NYSE.candle_slot(H1, label).close_time)


def completion_stamps(prefix: pd.DataFrame, labels: pd.Index) -> pd.DatetimeIndex:
    """For each 4h label, the first prefix label whose 1h slot closes at or after its close."""
    prefix_labels = pd.DatetimeIndex(prefix.index)
    closes = pd.DatetimeIndex([hourly_close(label) for label in prefix_labels])
    stamps = []
    for label in labels:
        block_close = pd.Timestamp(NYSE.candle_slot(H4, label).close_time)
        stamps.append(prefix_labels[int(closes.searchsorted(block_close, side="left"))])
    return pd.DatetimeIndex(stamps, dtype=CANONICAL_INDEX)


def resample_at(prefix: pd.DataFrame) -> pd.DataFrame:
    now = NYSE.candle_slot(H1, prefix.index[-1]).close_time
    candles = resample_hourly_to_4h(prefix, now, calendar=NYSE).candles
    return candles.set_axis(completion_stamps(prefix, candles.index))


def cheat_in_progress(prefix: pd.DataFrame) -> pd.DataFrame:
    """Resamples slots still in progress and stamps each row with its last prefix label."""
    now = NYSE.candle_slot(H1, prefix.index[-1]).close_time + timedelta(days=3)
    candles = resample_hourly_to_4h(prefix, now, calendar=NYSE).candles
    prefix_labels = pd.DatetimeIndex(prefix.index)
    stamps = []
    for label in candles.index:
        block_close = pd.Timestamp(NYSE.candle_slot(H4, label).close_time)
        stamps.append(prefix_labels[int(prefix_labels.searchsorted(block_close, side="left")) - 1])
    return candles.set_axis(pd.DatetimeIndex(stamps, dtype=CANONICAL_INDEX))


def cheat_next_open(prefix: pd.DataFrame) -> pd.DataFrame:
    """Replaces each close with the open of the prefix row after the row's completion stamp."""
    stamped = resample_at(prefix).copy()
    prefix_labels = pd.DatetimeIndex(prefix.index)
    opens = prefix["open"].to_numpy(dtype=np.float64)
    closes = []
    for stamp in stamped.index:
        following = int(prefix_labels.searchsorted(stamp, side="right"))
        closes.append(opens[following] if following < len(prefix) else np.nan)
    stamped["close"] = np.array(closes, dtype=np.float64)
    return stamped


def assert_non_vacuous(report: LookaheadReport, frame: pd.DataFrame) -> None:
    assert report.cuts == tuple(range(1, len(frame)))
    assert report.comparisons > 0
    assert report.non_missing_values > 0


# --- The frames ---------------------------------------------------------------------------------


def test_the_frames_have_the_spec_row_counts() -> None:
    assert len(gap_free_frame()) == 88
    assert len(gapped_frame()) == 78


@pytest.mark.parametrize("build", [gap_free_frame, gapped_frame], ids=["gap_free", "gapped"])
def test_completion_stamps_are_unique(build: Callable[[], pd.DataFrame]) -> None:
    frame = build()

    stamped = resample_at(frame)

    assert stamped.index.is_unique
    assert len(stamped) > 0


# --- 1. The harness with completion stamps --------------------------------------------------------


@pytest.mark.parametrize("build", [gap_free_frame, gapped_frame], ids=["gap_free", "gapped"])
def test_resampling_has_no_lookahead_on_a_full_sweep(build: Callable[[], pd.DataFrame]) -> None:
    frame = build()

    report = assert_no_lookahead(resample_at, frame, max_cuts=len(frame))

    assert_non_vacuous(report, frame)


# --- 2. The controls ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cheat", "kind"),
    [(cheat_in_progress, "result_disappeared"), (cheat_next_open, "value_mismatch")],
    ids=["in_progress", "next_open"],
)
@pytest.mark.parametrize("build", [gap_free_frame, gapped_frame], ids=["gap_free", "gapped"])
def test_the_controls_are_caught(
    cheat: Resample, kind: str, build: Callable[[], pd.DataFrame]
) -> None:
    frame = build()

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat, frame, max_cuts=len(frame))

    assert caught.value.violation.kind == kind


# --- 3. The now sweep -----------------------------------------------------------------------------


def test_every_now_reads_only_rows_published_by_now_and_returns_final_rows() -> None:
    frame = gapped_frame()
    labels = pd.DatetimeIndex(frame.index)
    closes = pd.DatetimeIndex([hourly_close(label) for label in labels])
    final = resample_hourly_to_4h(frame, utc("2024-12-10T00:00"), calendar=NYSE).candles
    instants = [
        instant
        for slot in NYSE.candle_slots(H1, START, END)
        for instant in (slot.close_time - timedelta(microseconds=1), slot.close_time)
    ]
    assert len(instants) == 176

    for now in instants:
        result = resample_hourly_to_4h(frame, now, calendar=NYSE)
        published = frame.iloc[: int(closes.searchsorted(pd.Timestamp(now), side="right"))]
        on_published = resample_hourly_to_4h(published, now, calendar=NYSE)

        pd.testing.assert_frame_equal(result.candles, on_published.candles, check_exact=True)
        assert result.slots == on_published.slots
        assert pd.DatetimeIndex(result.candles.index).isin(final.index).all()
        pd.testing.assert_frame_equal(
            result.candles, final.loc[result.candles.index], check_exact=True
        )
