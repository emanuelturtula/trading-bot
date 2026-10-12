"""Session-anchored ``4h`` and ``1w`` candles resampled from finer candles (spec 011, Design 5;
D31, D57; spec 028).

``resample_hourly_to_4h`` builds one ``4h`` row for every ``4h`` slot of the calendar grid that
is closed at ``now`` and holds at least one hourly row: the open of its first row, the highest
high, the lowest low, the close of its last row and the sum of volumes (decision D34: built from
the bars that exist). Each row comes with a report of the hourly slots it lacks, so the data
layer can apply its publication policy and log gaps; this module applies no policy and does not
log.

``resample_daily_to_1w`` does the same for ``1w`` slots from daily candles (spec 028): a week
holds the daily rows of its sessions, and its report lists the session days without a row.

The grid comes from the market calendar (spec 009), so DST changes, half days and late opens
need no special cases. Only rows labelled before the close of the last ``4h`` slot closed at
``now`` are read, so a row never depends on hourly data published after ``now`` (CLAUDE.md
rule 4). The module is pure: no clock, no I/O and no module state (CLAUDE.md rule 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd

from trading_bot.domain.candles import OHLCV_COLUMNS, validate_candles
from trading_bot.domain.market_calendar.sessions import CandleSlot, MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["ResampledCandles", "ResampledSlot", "resample_daily_to_1w", "resample_hourly_to_4h"]

_CANONICAL_INDEX_DTYPE: Final = pd.DatetimeTZDtype(unit="us", tz="UTC")
_ONE_MICROSECOND: Final = timedelta(microseconds=1)

type _Positions = npt.NDArray[np.intp]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResampledSlot:
    """The report of one resampled row. Equal by value and hashable."""

    slot: CandleSlot  # the 4h or 1w slot of the row
    bars: int  # source rows (hourly or daily) aggregated, >= 1
    # labels of the slot's source grid slots (1h or 1d) without a row, stdlib UTC, increasing
    missing: tuple[datetime, ...]
    closing_bar_missing: bool  # the source slot whose close_time equals slot.close_time has no row


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class ResampledCandles:
    """The resampled frame and its per-row report. Compared by identity (it holds a frame)."""

    candles: (
        pd.DataFrame
    )  # canonical 4h or 1w frame, one row per entry of `slots`, in the same order
    slots: tuple[ResampledSlot, ...]


def resample_hourly_to_4h(
    hourly: pd.DataFrame, now: datetime, *, calendar: MarketCalendar
) -> ResampledCandles:
    """The ``4h`` candles of every ``4h`` slot closed at ``now`` that holds hourly rows.

    Checks run in this order: ``TypeError`` for a non-``MarketCalendar``; ``to_utc(now)``
    (``TypeError``/``ValueError``); ``validate_candles(hourly)`` (``TypeError``/
    ``CandleValidationError``); ``CalendarRangeError`` when no ``4h`` slot is closed at ``now``
    inside the calendar. The considered rows are those labelled before the close of the last
    ``4h`` slot closed at ``now``; each must be a ``1h`` slot label, and the calendar's error for
    the first one that is not propagates (``CandleLabelError``, ``CalendarRangeError`` or
    ``ValueError``). Later rows are ignored without checks.

    The result is a new canonical frame (``datetime64[us, UTC]`` index named ``None``, the
    ``OHLCV_COLUMNS`` as ``float64``) that passes ``validate_candles``, with one
    ``ResampledSlot`` per row. ``hourly`` is never modified.
    """
    return _resample(hourly, now, calendar=calendar, source=Timeframe.H1, target=Timeframe.H4)


def resample_daily_to_1w(
    daily: pd.DataFrame, now: datetime, *, calendar: MarketCalendar
) -> ResampledCandles:
    """The ``1w`` candles of every ``1w`` slot closed at ``now`` that holds daily rows (spec 028).

    The contract of ``resample_hourly_to_4h`` with ``1d`` rows and ``1w`` slots: each considered
    row must be a ``1d`` slot label, a week aggregates the rows of its sessions, and its report
    lists the session days of the week without a row.
    """
    return _resample(daily, now, calendar=calendar, source=Timeframe.D1, target=Timeframe.W1)


def _resample(
    rows: pd.DataFrame,
    now: datetime,
    *,
    calendar: MarketCalendar,
    source: Timeframe,
    target: Timeframe,
) -> ResampledCandles:
    """``source`` rows aggregated into the ``target`` slots closed at ``now``.

    Every ``target`` slot is a run of whole ``source`` slots that starts at the slot's label, so
    a ``source`` slot belongs to the ``target`` slot with the greatest label at or before its own.
    """
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    instant = to_utc(now)
    validate_candles(rows)
    last = calendar.closed_candles(target, instant, 1)[-1]

    index = pd.DatetimeIndex(rows.index).tz_convert("UTC")
    count = int(index.searchsorted(pd.Timestamp(last.close_time), side="left"))
    if count == 0:
        return ResampledCandles(candles=_frame([], np.empty((0, len(OHLCV_COLUMNS)))), slots=())
    labels = index[:count]

    # The first considered label locates the grid; its own error is the first one to report.
    first = calendar.candle_slot(source, labels[0])
    blocks = calendar.candle_slots(
        target,
        max(first.label - target.duration + _ONE_MICROSECOND, calendar.coverage_start),
        last.close_time,
    )
    block_labels = _labels(blocks)
    first_block = blocks[
        int(block_labels.searchsorted(pd.Timestamp(first.label), side="right")) - 1
    ]
    hours = calendar.candle_slots(source, first_block.label, last.close_time)
    hour_labels = _labels(hours)

    exact = np.asarray(labels.nanosecond == 0, dtype=np.bool_)
    grid_positions: _Positions = hour_labels.get_indexer(labels.as_unit("us"))
    valid = exact & (grid_positions >= 0)
    if not valid.all():
        # Not a source slot label: the calendar raises the error that names it.
        calendar.candle_slot(source, labels[int(np.argmin(valid))])

    block_of_row = np.asarray(
        block_labels.searchsorted(labels.as_unit("us"), side="right"), dtype=np.intp
    ) - np.intp(1)
    starts = np.flatnonzero(np.diff(block_of_row, prepend=np.intp(-1)) != 0)
    ends = np.append(starts[1:], count)
    values = rows.iloc[:count].to_numpy(dtype=np.float64)
    with np.errstate(over="ignore"):  # an overflowing volume sum fails the postcondition instead
        aggregated = np.column_stack(
            (
                values[starts, 0],
                np.maximum.reduceat(values[:, 1], starts),
                np.minimum.reduceat(values[:, 2], starts),
                values[ends - 1, 3],
                np.add.reduceat(values[:, 4], starts),
            )
        )

    report: list[ResampledSlot] = []
    for start, end in zip(starts.tolist(), ends.tolist(), strict=True):
        block = blocks[int(block_of_row[start])]
        # The label, not the open: a 1w slot opens with its first session, after that day's
        # 00:00 1d label. For 4h both are the same instant.
        first_hour = int(hour_labels.searchsorted(pd.Timestamp(block.label), side="left"))
        stop_hour = int(hour_labels.searchsorted(pd.Timestamp(block.close_time), side="left"))
        present = set(grid_positions[start:end].tolist())
        report.append(
            ResampledSlot(
                slot=block,
                bars=end - start,
                missing=tuple(
                    hours[position].label
                    for position in range(first_hour, stop_hour)
                    if position not in present
                ),
                closing_bar_missing=stop_hour - 1 not in present,
            )
        )
    candles = _frame([entry.slot.label for entry in report], aggregated)
    return ResampledCandles(candles=validate_candles(candles), slots=tuple(report))


def _labels(slots: tuple[CandleSlot, ...]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([slot.label for slot in slots], dtype=_CANONICAL_INDEX_DTYPE)


def _frame(labels: list[datetime], values: npt.NDArray[np.float64]) -> pd.DataFrame:
    return pd.DataFrame(
        values.reshape(len(labels), len(OHLCV_COLUMNS)),
        index=pd.DatetimeIndex(labels, dtype=_CANONICAL_INDEX_DTYPE),
        columns=list(OHLCV_COLUMNS),
        dtype=np.float64,
    )
