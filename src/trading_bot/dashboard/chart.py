"""The data of a ticker's chart on the dashboard (spec 027).

``chart_payload`` turns a closed-candle frame, the rules assigned to the ticker and its recorded
signals into the JSON that ``static/chart.js`` draws: candles, indicator lines, rule levels and
BUY/SELL markers. It does no I/O and reads no clock, so the route decides which candles to read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.rules.evaluator import evaluate_each
from trading_bot.domain.rules.schema import IndicatorOperand, Rule, ValueOperand
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredRule
from trading_bot.persistence.signal_records import StoredSignal

__all__ = ["EXCHANGE_ZONE", "SHOWN_CANDLES", "chart_payload", "fetch_lookback"]

SHOWN_CANDLES: Final = {
    Timeframe.W1: 156,  # three years
    Timeframe.D1: 250,
    Timeframe.H4: 300,
    Timeframe.H1: 300,
}
EXCHANGE_ZONE: Final = ZoneInfo("America/New_York")  # daily and weekly candles: by trading day
PRICE_PANE_INDICATORS: Final = frozenset({"sma", "ema", "bbands"})  # drawn over the candles

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


@dataclass(frozen=True, slots=True)
class _Line:
    indicator: str
    params: tuple[tuple[str, object], ...]
    output: str

    @classmethod
    def of(cls, operand: IndicatorOperand) -> _Line:
        return cls(operand.indicator, tuple(operand.params.items()), operand.output)

    @property
    def pane_key(self) -> tuple[str, tuple[tuple[str, object], ...]]:
        return self.indicator, self.params

    @property
    def label(self) -> str:
        spec = REGISTRY.get(self.indicator)
        values = " ".join(_number(value) for _, value in self.params)
        name = f"{spec.label} {values}".strip()
        if len(spec.outputs) == 1:
            return name
        output = next(item.label for item in spec.outputs if item.name == self.output)
        return f"{name} · {output}"


def fetch_lookback(timeframe: Timeframe, rules: Sequence[Rule], maximum: int) -> int:
    """Candles to read: the shown window plus the longest stable warm-up, capped at ``maximum``."""
    warmup = max((rule.stable_warmup() for rule in rules), default=0)
    return min(maximum, SHOWN_CANDLES[timeframe] + warmup)


def chart_payload(
    *,
    candles: pd.DataFrame,
    timeframe: Timeframe,
    rules: Sequence[StoredRule],
    signals: Sequence[StoredSignal],
    zone: tzinfo,
) -> dict[str, Json]:
    """The chart of the last ``SHOWN_CANDLES[timeframe]`` candles of ``candles``.

    ``candles`` is a closed-candle frame (labels are candle open times); the extra rows before
    the window warm the indicators up. ``rules`` are the ticker's assigned rules and ``signals``
    its recorded signals; signals outside the window are ignored.
    """
    shown = min(len(candles), SHOWN_CANDLES[timeframe])
    window = pd.DatetimeIndex(candles.index[len(candles) - shown :])
    times = [_time(label, timeframe, zone) for label in window]
    ohlc = candles[["open", "high", "low", "close"]].to_numpy(dtype=np.float64)[
        len(candles) - shown :
    ]
    payload_candles: list[Json] = [
        {"time": time, "open": row[0], "high": row[1], "low": row[2], "close": row[3]}
        for time, row in zip(times, ohlc.tolist(), strict=True)
    ]
    lines, levels = _lines_and_levels(rules)
    panes = _panes(lines)
    return {
        "timeframe": timeframe.value,
        "candles": payload_candles,
        "lines": [_series(line, panes, candles, times, shown) for line in lines],
        "levels": [
            {
                "pane": panes[line.pane_key],
                "value": value,
                "label": f"{line.label} {_number(value)}",
            }
            for line, value in levels
        ],
        "markers": _markers(candles, timeframe, rules, signals, window, times),
    }


def _lines_and_levels(
    rules: Sequence[StoredRule],
) -> tuple[list[_Line], list[tuple[_Line, float]]]:
    lines: dict[_Line, None] = {}
    levels: dict[tuple[_Line, float], None] = {}
    for stored in rules:
        for condition in stored.rule.all_conditions:
            sides = (condition.left, condition.right)
            for operand in sides:
                if isinstance(operand, IndicatorOperand):
                    lines.setdefault(_Line.of(operand))
            for this, other in (sides, sides[::-1]):
                if (
                    isinstance(this, IndicatorOperand)
                    and this.indicator not in PRICE_PANE_INDICATORS
                    and isinstance(other, ValueOperand)
                ):
                    levels.setdefault((_Line.of(this), other.value))
    return list(lines), list(levels)


def _panes(lines: Sequence[_Line]) -> dict[tuple[str, tuple[tuple[str, object], ...]], int]:
    """Pane 0 holds the candles and price-scale lines; each other indicator gets its own pane."""
    panes: dict[tuple[str, tuple[tuple[str, object], ...]], int] = {}
    for line in lines:
        if line.pane_key not in panes:
            below = line.indicator not in PRICE_PANE_INDICATORS
            panes[line.pane_key] = (
                len({pane for pane in panes.values() if pane}) + 1 if below else 0
            )
    return panes


def _series(
    line: _Line,
    panes: dict[tuple[str, tuple[tuple[str, object], ...]], int],
    candles: pd.DataFrame,
    times: Sequence[Json],
    shown: int,
) -> dict[str, Json]:
    values = REGISTRY.compute(line.indicator, dict(line.params), candles)[line.output]
    tail = np.asarray(values, dtype=np.float64)[len(candles) - shown :]
    points: list[Json] = [
        {"time": time, "value": float(value)}
        for time, value in zip(times, tail.tolist(), strict=True)
        if np.isfinite(value)
    ]
    return {"label": line.label, "pane": panes[line.pane_key], "points": points}


def _markers(
    candles: pd.DataFrame,
    timeframe: Timeframe,
    rules: Sequence[StoredRule],
    signals: Sequence[StoredSignal],
    window: pd.DatetimeIndex,
    times: Sequence[Json],
) -> list[Json]:
    position = {label: offset for offset, label in enumerate(window)}
    recorded: set[tuple[int, int]] = set()
    markers: list[tuple[int, dict[str, Json]]] = []
    for signal in signals:
        label = pd.Timestamp(signal.signal.candle_close_ts - timeframe.duration)
        offset = position.get(label)
        if offset is None:
            continue
        recorded.add((signal.rule_id, offset))
        markers.append(
            (offset, _marker(times[offset], signal.signal.side.value, signal.rule_name, True))
        )
    for stored in rules:
        for offset in _firings(stored.rule, candles, len(window)):
            if (stored.id, offset) not in recorded:
                side = stored.rule.signal.value
                markers.append((offset, _marker(times[offset], side, stored.name, False)))
    # Lightweight Charts needs markers in time order.
    return [marker for _, marker in sorted(markers, key=lambda item: item[0])]


def _firings(rule: Rule, candles: pd.DataFrame, shown: int) -> list[int]:
    """Offsets in the window where ``rule`` fires, its cooldown counted from the window start."""
    fired: list[int] = []
    for offset, evaluation in enumerate(evaluate_each(rule, candles, last=shown)):
        if evaluation.triggered and (not fired or offset - fired[-1] > rule.cooldown_bars):
            fired.append(offset)
    return fired


def _marker(time: Json, side: str, rule: str, recorded: bool) -> dict[str, Json]:
    return {"time": time, "side": side, "rule": rule, "recorded": recorded}


def _time(label: datetime, timeframe: Timeframe, zone: tzinfo) -> Json:
    """A daily candle's trading day (for ``1w``, the week's first one), or an intraday candle's
    open time shifted to ``zone``.

    Lightweight Charts draws UTC timestamps, so the zone offset is added to show local times.
    """
    stamp = pd.Timestamp(label)
    if timeframe in (Timeframe.D1, Timeframe.W1):
        return stamp.tz_convert(EXCHANGE_ZONE).date().isoformat()
    offset = stamp.tz_convert(zone).utcoffset()
    seconds = int(offset.total_seconds()) if offset is not None else 0
    return int(stamp.timestamp()) + seconds


def _number(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
