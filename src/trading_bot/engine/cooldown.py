"""The cooldown gate, counted by candle position (spec 015, Design 5; decision D129).

``cooldown_bars`` is "the minimum number of closed candles between two notified signals of the
same rule and ticker" (spec 006), so ``cooldown_bars`` candles must lie **strictly** between
them. They are counted as **rows of the evaluated frame**, never as
``(t2 - t1) / timeframe.duration``, which is wrong across nights, weekends and holidays (spec
004, Design 5): on the NYSE grid the candle of 2024-07-05 is four calendar days after the one of
2024-07-01 but only three candles later.

With ``previous_label = previous_close - timeframe.duration`` (an exact subtraction, never a
division) and ``bars = |{label in labels[: i + 1] : previous_label < label <= labels[i]}|``:

===========================  ============  ======================================================
Case                         Decision      What the engine does
===========================  ============  ======================================================
``previous_close is None``   ``ALLOWED``   record and, if new, notify
``previous_label > label``   ``SUPERSEDED``  nothing is written or sent: a later signal exists
``previous_label == label``  ``ALLOWED``   ``record`` decides: ``is_new=False``, nothing is sent
``bars > cooldown_bars``     ``ALLOWED``   record and, if new, notify
``bars <= cooldown_bars``    ``BLOCKED``   nothing is written or sent
===========================  ============  ======================================================

``cooldown_bars == 0`` therefore never blocks, and the equal-label case is deliberately left to
``SignalRepository.record`` so that CLAUDE.md rule 5 keeps exactly one arbiter (decision D112).
A ``previous_label`` earlier than the first label counts every row of the prefix, so a frame
shorter than ``cooldown_bars + 1`` errs towards ``BLOCKED``: fewer notifications, never extra
ones.

This module reads no clock, no database and no candle value: only labels, a timeframe and an
integer. Every decision depends on its own label and the earlier ones alone, which the
look-ahead tests verify (CLAUDE.md rule 4).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import pandas as pd

from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = ["CooldownDecision", "cooldown_decision", "cooldown_decisions"]


class CooldownDecision(StrEnum):
    """What the cooldown says about one evaluated candle."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


def cooldown_decision(
    labels: pd.DatetimeIndex,
    *,
    timeframe: Timeframe,
    cooldown_bars: int,
    previous_close: datetime | None,
) -> CooldownDecision:
    """The decision about the **last** label of ``labels``, the evaluated candle.

    ``labels`` is the index of the evaluated frame: a non-empty, strictly increasing, tz-aware
    ``DatetimeIndex`` (``TypeError``/``ValueError`` otherwise). ``previous_close`` is the
    ``candle_close_ts`` of the pair's last **recorded** signal (decision D130), or ``None`` when
    it never fired; a naive instant raises ``ValueError``.

    It equals ``cooldown_decisions(labels, ...)[-1]``.
    """
    return cooldown_decisions(
        labels,
        timeframe=timeframe,
        cooldown_bars=cooldown_bars,
        previous_close=previous_close,
    )[-1]


def cooldown_decisions(
    labels: pd.DatetimeIndex,
    *,
    timeframe: Timeframe,
    cooldown_bars: int,
    previous_close: datetime | None,
) -> tuple[CooldownDecision, ...]:
    """One ``CooldownDecision`` per label, in index order: the look-ahead reference.

    ``cooldown_decisions(labels[:n], ...) == cooldown_decisions(labels, ...)[:n]``, the way
    ``evaluate`` and ``evaluate_each`` relate (spec 007). The result is a tuple rather than a
    ``pd.Series`` so that no annotation depends on a pandas-stubs generic parameter for a
    non-numeric dtype; the look-ahead test wraps it in an object-dtype ``Series``.
    """
    index = _checked_labels(labels)
    _check_timeframe(timeframe)
    _check_cooldown_bars(cooldown_bars)
    if previous_close is None:
        return (CooldownDecision.ALLOWED,) * len(index)
    previous_label = pd.Timestamp(to_utc(previous_close) - timeframe.duration)
    # ``side="right"`` counts the labels at or before the previous label, so every later row is
    # one candle of the window. The count is the same for any prefix that reaches it, which is
    # what keeps the decision at a candle independent of the candles after it.
    at_or_before = int(index.searchsorted(previous_label, side="right"))
    return tuple(
        _decide(index[position], previous_label, position + 1 - at_or_before, cooldown_bars)
        for position in range(len(index))
    )


def _decide(
    label: pd.Timestamp, previous_label: pd.Timestamp, bars: int, cooldown_bars: int
) -> CooldownDecision:
    if label < previous_label:
        return CooldownDecision.SUPERSEDED
    if label == previous_label:
        # The duplicate must reach ``record``, the single arbiter of CLAUDE.md rule 5.
        return CooldownDecision.ALLOWED
    return CooldownDecision.ALLOWED if bars > cooldown_bars else CooldownDecision.BLOCKED


def _checked_labels(labels: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if not isinstance(labels, pd.DatetimeIndex):
        raise TypeError(f"labels must be a pandas DatetimeIndex, got {type(labels).__name__}")
    if len(labels) == 0:
        raise ValueError("labels must hold at least one label: there is no candle to decide about")
    if labels.tz is None:
        raise ValueError("labels must be timezone-aware, got a naive DatetimeIndex")
    if not labels.is_monotonic_increasing or not labels.is_unique:
        raise ValueError("labels must be strictly increasing")
    return labels


def _check_timeframe(timeframe: Timeframe) -> None:
    if not isinstance(timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )


def _check_cooldown_bars(cooldown_bars: int) -> None:
    if isinstance(cooldown_bars, bool) or not isinstance(cooldown_bars, int):
        raise TypeError(f"cooldown_bars must be an int, got {type(cooldown_bars).__name__}")
    if cooldown_bars < 0:
        raise ValueError(f"cooldown_bars must not be negative, got {cooldown_bars}")
