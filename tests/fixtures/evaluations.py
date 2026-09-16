"""Look-ahead references for the rule evaluator (spec 007, Design 13).

``evaluate(rule, candles)`` returns one result about the last candle, so the look-ahead harness
needs a series-valued reference that gives the same result for every candle of a frame
(``assert_no_lookahead_point_in_time``, spec 003). These builders run ``evaluate_each`` over the
whole frame and index its results by the frame's own labels, the candle **open** times.

``evaluate_each`` returns a tuple because pandas-stubs reject ``Series[Evaluation]`` and
``Series[object]`` in ``domain/``; the loosely typed object ``Series`` is built here instead.

Always import this module as ``tests.fixtures.evaluations``.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.rules.evaluator import evaluate_each
from trading_bot.domain.rules.schema import Rule


def evaluations_by_candle(
    rule: Rule, *, registry: IndicatorRegistry = REGISTRY
) -> Callable[[pd.DataFrame], object]:
    """The look-ahead reference: one Evaluation per candle, as an object-dtype Series indexed by
    the frame's own labels (candle open times)."""

    def evaluations_by_candle(candles: pd.DataFrame) -> object:
        evaluations = evaluate_each(rule, candles, registry=registry)
        return pd.Series(list(evaluations), index=candles.index, dtype=object, name="evaluation")

    return evaluations_by_candle


def triggered_by_candle(
    rule: Rule, *, registry: IndicatorRegistry = REGISTRY
) -> Callable[[pd.DataFrame], object]:
    """The same, reduced to a boolean Series: readable failures for trigger-only checks."""

    def triggered_by_candle(candles: pd.DataFrame) -> object:
        triggered = [
            evaluation.triggered for evaluation in evaluate_each(rule, candles, registry=registry)
        ]
        return pd.Series(triggered, index=candles.index, dtype=bool, name="triggered")

    return triggered_by_candle
