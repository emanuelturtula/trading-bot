"""Seeded synthetic OHLCV candles for tests (spec 003, Design 4).

Frames are indexed by tz-aware UTC candle **open** times on the timeframe grid and have
``float64`` columns ``open, high, low, close, volume``. Randomness comes only from a local
``numpy.random.default_rng(seed)``: no global RNG, clock or I/O. numpy does not guarantee
``Generator`` streams across versions, so tests must assert invariants, never golden values.

Always import this module as ``tests.fixtures.candles``.
"""

from __future__ import annotations

import math
from datetime import timedelta
from enum import StrEnum
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

type TimeframeCode = Literal["1h", "4h", "1d"]

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
TIMEFRAMES: tuple[TimeframeCode, ...] = ("1h", "4h", "1d")
DEFAULT_START = "2024-01-01T00:00:00+00:00"  # a Monday, on every grid
MIN_PRICE = 1e-6
MAX_PRICE = 1e9
FEATURE_MIN_CANDLES = 60


class Scenario(StrEnum):
    RANDOM_WALK = "random_walk"
    GAPS = "gaps"
    FLAT_RUNS = "flat_runs"
    EXTREME = "extreme"
    MIXED = "mixed"


ALL_SCENARIOS: tuple[Scenario, ...] = tuple(Scenario)

type _BoolArray = npt.NDArray[np.bool_]

_HOURS_PER_SLOT: dict[str, int] = {"1h": 1, "4h": 4, "1d": 24}
_GAP_DROP_PROBABILITY = 0.03  # extra weekday slots dropped at random (holidays, halts)
_FORCED_GAP_ROWS = 12  # trailing rows where a weekday gap is guaranteed and no event is placed
_MAX_LOG_FACTOR = 50.0  # exp(50) exceeds MAX_PRICE / MIN_PRICE, so clipping it changes nothing
_CRASH_FACTOR = 0.09  # close <= 0.1 * open
_SPIKE_FACTOR = 11.0  # close >= 10 * open
_VOLUME_SPIKE = 1e12
_MIN_FLAT_RUN = 5
_MAX_FLAT_RUN = 20


def synthetic_candles(
    n: int = 250,
    *,
    seed: int = 0,
    scenario: Scenario | str = Scenario.RANDOM_WALK,
    timeframe: TimeframeCode = "1d",
    start: str | pd.Timestamp = DEFAULT_START,
    start_price: float = 100.0,
    volatility: float = 0.02,
) -> pd.DataFrame:
    """Generate ``n`` closed candles for ``scenario`` (see ``Scenario``) deterministically.

    - ``RANDOM_WALK``: contiguous grid, ``open[i] == close[i - 1]``.
    - ``GAPS``: no Saturday/Sunday slots, extra dropped weekday slots and a price jump at gaps.
    - ``FLAT_RUNS``: runs of 5-20 flat, zero-volume candles.
    - ``EXTREME``: a -90% candle, a +900% candle, a 1e12 volume spike and zero-volume candles.
    - ``MIXED``: all of the above.

    Scenario features are guaranteed from ``FEATURE_MIN_CANDLES`` candles on. Injected events do
    not depend on ``volatility``; ``volatility=0.0`` gives a constant path apart from them.
    """
    if isinstance(n, bool) or not isinstance(n, int | np.integer):
        raise TypeError(f"n must be an int, got {n!r}")
    n = int(n)
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {TIMEFRAMES}")
    kind = _parse_scenario(scenario)
    if not (math.isfinite(volatility) and volatility >= 0.0):
        raise ValueError(f"volatility must be a finite number >= 0, got {volatility!r}")
    if not MIN_PRICE <= start_price <= MAX_PRICE:
        raise ValueError(f"start_price must be in [{MIN_PRICE}, {MAX_PRICE}], got {start_price!r}")
    first_open = _parse_start(start, timeframe)

    rng = np.random.default_rng(seed)
    with_gaps = kind in (Scenario.GAPS, Scenario.MIXED)
    slots = _gap_slots(rng, n, first_open, timeframe) if with_gaps else np.arange(n)
    gap_rows = np.zeros(n, dtype=np.bool_)
    gap_rows[1:] = np.diff(slots) > 1
    events = _Events(rng, n, kind, event_stop=n - _FORCED_GAP_ROWS if with_gaps else n)
    frame = _ohlcv(rng, n, start_price, volatility, gap_rows, events)
    offsets = pd.to_timedelta(slots * _HOURS_PER_SLOT[timeframe], unit="h")
    frame.index = pd.DatetimeIndex(first_open + offsets)
    return frame


def _parse_scenario(scenario: Scenario | str) -> Scenario:
    try:
        return Scenario(scenario)
    except ValueError:
        expected = [item.value for item in Scenario]
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {expected}") from None


def _parse_start(start: str | pd.Timestamp, timeframe: TimeframeCode) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(start)
    except (TypeError, ValueError) as error:
        raise ValueError(f"start must be a UTC timestamp, got {start!r}") from error
    if (
        pd.isna(timestamp)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
        or timestamp.tzname() != "UTC"
    ):
        raise ValueError(f"start must be a tz-aware UTC timestamp, got {start!r}")
    duration = pd.Timedelta(hours=_HOURS_PER_SLOT[timeframe])
    if timestamp != timestamp.floor(duration):
        raise ValueError(f"start must be an open time on the {timeframe} grid, got {start!r}")
    return timestamp.tz_convert("UTC")


def _gap_slots(
    rng: np.random.Generator, n: int, first_open: pd.Timestamp, timeframe: TimeframeCode
) -> npt.NDArray[np.int64]:
    """Grid slot of each row: weekends skipped, weekday slots dropped, one drop guaranteed."""
    hours_per_slot = _HOURS_PER_SLOT[timeframe]
    week_hour = int(first_open.dayofweek) * 24 + int(first_open.hour)

    def is_weekday(slot: int) -> bool:
        return (week_hour + slot * hours_per_slot) // 24 % 7 < 5

    def next_weekday(slot: int) -> int:
        while not is_weekday(slot):
            slot += 1
        return slot

    random_drops = rng.random(n) < _GAP_DROP_PROBABILITY
    forced_from: int | None = n - _FORCED_GAP_ROWS if n >= FEATURE_MIN_CANDLES else None
    slot = next_weekday(0)
    slots = [slot]
    for row in range(1, n):
        following = next_weekday(slot + 1)
        consecutive = following == slot + 1 and is_weekday(following + 1)
        if forced_from is not None and row >= forced_from and consecutive:
            # Drop a single weekday slot between two weekday candles: no weekend in between.
            slot = following + 1
            forced_from = None
        elif random_drops[row]:
            slot = next_weekday(following + 1)
        else:
            slot = following
        slots.append(slot)
    return np.asarray(slots, dtype=np.int64)


class _Events:
    """Rows of injected events, placed without overlap in ``[1, event_stop)``."""

    def __init__(
        self, rng: np.random.Generator, n: int, scenario: Scenario, event_stop: int
    ) -> None:
        self.flat = np.zeros(n, dtype=np.bool_)
        self.crash = np.zeros(n, dtype=np.bool_)
        self.spike = np.zeros(n, dtype=np.bool_)
        self.volume_spike = np.zeros(n, dtype=np.bool_)
        self.zero_volume = np.zeros(n, dtype=np.bool_)
        if n < FEATURE_MIN_CANDLES:
            return
        blocks: list[tuple[_BoolArray, int]] = []
        if scenario in (Scenario.FLAT_RUNS, Scenario.MIXED):
            longest = min(_MAX_FLAT_RUN, n // 6)
            runs = int(rng.integers(1, 4)) if n >= 200 else 1
            blocks += [
                (self.flat, int(rng.integers(_MIN_FLAT_RUN, longest + 1))) for _ in range(runs)
            ]
        if scenario in (Scenario.EXTREME, Scenario.MIXED):
            blocks += [(self.crash, 1), (self.spike, 1), (self.volume_spike, 1)]
            blocks += [(self.zero_volume, 1), (self.zero_volume, 1)]
        order = rng.permutation(len(blocks))
        lengths = [blocks[i][1] for i in order]
        for i, first_row in zip(order, _place_blocks(rng, lengths, event_stop), strict=True):
            mask, length = blocks[i]
            mask[first_row : first_row + length] = True


def _place_blocks(rng: np.random.Generator, lengths: list[int], stop: int) -> list[int]:
    """First rows of consecutive blocks in ``[1, stop)``, each followed by at least one free row.

    Callers keep ``sum(lengths) + len(lengths) <= stop - 1``: at most 3 runs of 21 rows plus 10
    rows of single events from 200 candles, and one run of ``n // 6 + 1`` rows plus 10 below.
    """
    slack = stop - 1 - sum(lengths) - len(lengths)
    offsets = np.sort(rng.integers(0, slack + 1, size=len(lengths)))
    first_rows: list[int] = []
    used = 0
    for length, offset in zip(lengths, offsets.tolist(), strict=True):
        first_rows.append(1 + int(offset) + used)
        used += length + 1
    return first_rows


def _ohlcv(
    rng: np.random.Generator,
    n: int,
    start_price: float,
    volatility: float,
    gap_rows: _BoolArray,
    events: _Events,
) -> pd.DataFrame:
    returns = rng.normal(0.0, volatility, size=n).tolist()
    jumps = rng.normal(0.0, 3.0 * volatility, size=n).tolist()
    upper_wicks = np.abs(rng.normal(0.0, volatility / 2.0, size=n))
    lower_wicks = np.abs(rng.normal(0.0, volatility / 2.0, size=n))
    volume = np.round(rng.lognormal(math.log(1e6), 0.5, size=n))
    spike_volumes = np.round(_VOLUME_SPIKE * (1.0 + rng.random(size=n)))

    opens = np.empty(n, dtype=np.float64)
    closes = np.empty(n, dtype=np.float64)
    previous_close = start_price
    for row in range(n):
        open_price = previous_close
        if row > 0 and gap_rows[row] and not events.flat[row]:
            open_price = _scaled(previous_close, jumps[row])
        if events.flat[row]:
            close_price = open_price
        elif events.crash[row]:
            close_price = _clip(open_price * _CRASH_FACTOR)
        elif events.spike[row]:
            close_price = _clip(open_price * _SPIKE_FACTOR)
        else:
            close_price = _scaled(open_price, returns[row])
        opens[row] = open_price
        closes[row] = close_price
        previous_close = close_price

    body_high = np.maximum(opens, closes)
    body_low = np.minimum(opens, closes)
    highs = np.minimum(body_high * np.exp(np.minimum(upper_wicks, _MAX_LOG_FACTOR)), MAX_PRICE)
    lows = np.maximum(body_low * np.exp(-np.minimum(lower_wicks, _MAX_LOG_FACTOR)), MIN_PRICE)
    highs = np.where(events.flat, closes, np.maximum(highs, body_high))
    lows = np.where(events.flat, closes, np.minimum(lows, body_low))
    volume = np.where(events.volume_spike, spike_volumes, volume)
    volume = np.where(events.flat | events.zero_volume, 0.0, volume)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume},
        columns=list(OHLCV_COLUMNS),
        dtype=np.float64,
    )


def _clip(price: float) -> float:
    return min(max(price, MIN_PRICE), MAX_PRICE)


def _scaled(price: float, log_factor: float) -> float:
    bounded = min(max(log_factor, -_MAX_LOG_FACTOR), _MAX_LOG_FACTOR)
    return _clip(price * math.exp(bounded))
