"""Adversarial data for the rule evaluator (spec 007, T11).

Extreme but individually valid inputs: ``Scenario.EXTREME`` and ``GAPS`` frames, prices at the
candle-frame clipping bounds, a 1e12 volume spike, constants at the schema bounds (``+-1e15`` and
the smallest positive subnormal double), a rule at the #6 catalog size bounds (20 conditions, 10
members per group, all ten indicators), ``obv`` on zero volume, and a warmup far above the frame
length. None of this may raise or warn (AC6, AC13, AC14): every situation here is a *data*
condition, not a type error, an invalid frame or a broken registry, so the only allowed outcome is
a well-typed, non-firing (or firing) ``Evaluation``. Warnings are turned into errors, as in
``test_rule_operators.py``.
"""

from __future__ import annotations

from typing import Final

import pytest

from tests.fixtures.candles import MAX_PRICE, MIN_PRICE, Scenario, synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from tests.fixtures.rules import (
    Payload,
    condition,
    indicator_operand,
    price_operand,
    required_output,
    rule_payload,
    value_operand,
)
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.evaluator import evaluate, evaluate_each
from trading_bot.domain.rules.schema import (
    MAX_CONDITIONS,
    MAX_GROUP_ITEMS,
    MAX_VALUE_MAGNITUDE,
    Rule,
    parse_rule,
)
from trading_bot.domain.signals import IndicatorValues

pytestmark = pytest.mark.filterwarnings("error")

_TINY: Final = 5e-324  # the smallest positive subnormal double


def rule_of(conditions: JsonValue, **overrides: JsonValue) -> Rule:
    return parse_rule(rule_payload(conditions, **overrides))


def bounds_rule() -> Rule:
    """20 conditions in two 10-member groups, referencing all ten registry indicators twice."""
    names = REGISTRY.names
    greater_than_zero: list[Payload] = [
        condition(indicator_operand(name, output=required_output(name)), ">", value_operand(0))
        for name in names
    ]
    price_below: list[Payload] = [
        condition(price_operand(), "<", indicator_operand(name, output=required_output(name)))
        for name in names
    ]
    assert len(greater_than_zero) == len(price_below) == MAX_GROUP_ITEMS
    return rule_of({"all": [{"any": greater_than_zero}, {"any": price_below}]})


# --- The #6 catalog size bounds, over all ten indicators (AC13, AC14) ------------------------


def test_the_bounds_rule_has_exactly_the_catalog_limits() -> None:
    rule = bounds_rule()
    indicators = {
        operand.indicator
        for item in rule.all_conditions
        for operand in (item.left, item.right)
        if hasattr(operand, "indicator")
    }

    assert len(rule.all_conditions) == MAX_CONDITIONS
    assert {member.mode.value for member in rule.conditions.members} == {"any"}
    assert indicators == set(REGISTRY.names)


def test_the_bounds_rule_evaluates_on_a_long_frame_without_raising() -> None:
    rule = bounds_rule()
    candles = synthetic_candles(300, seed=201, scenario=Scenario.MIXED)

    evaluation = evaluate(rule, candles)

    assert type(evaluation.triggered) is bool
    assert len(evaluation.condition_results) == MAX_CONDITIONS
    assert all(type(result) is bool for result in evaluation.condition_results)


@pytest.mark.parametrize("scenario", [Scenario.EXTREME, Scenario.GAPS])
def test_the_bounds_rule_evaluates_on_extreme_and_gapped_frames(scenario: Scenario) -> None:
    rule = bounds_rule()
    candles = synthetic_candles(200, seed=202, scenario=scenario)

    for evaluation in evaluate_each(rule, candles):
        assert type(evaluation.triggered) is bool
        assert len(evaluation.condition_results) == MAX_CONDITIONS
        assert all(type(result) is bool for result in evaluation.condition_results)
        assert all(type(value) is float for value in evaluation.indicator_values.values())


# --- Prices and volume at the candle-frame clipping bounds (AC6, AC13) -----------------------


def test_prices_and_volume_near_the_clipping_bounds_never_raise() -> None:
    closes = [MIN_PRICE, MIN_PRICE * 2.0, MAX_PRICE / 2.0, MAX_PRICE, MAX_PRICE / 3.0]
    candles = candles_from_prices(closes, volume=[1e12] * len(closes))
    rule = rule_of(
        {
            "any": [
                condition(
                    price_operand(), "crosses_above", indicator_operand("sma", {"length": 2})
                ),
                condition(price_operand("volume"), ">", value_operand(1e11)),
                condition(indicator_operand("rsi", {"length": 2}), "<", value_operand(100)),
            ]
        }
    )

    for evaluation in evaluate_each(rule, candles):
        assert type(evaluation.triggered) is bool
        assert all(type(value) is float for value in evaluation.indicator_values.values())


# --- Constants at the schema bounds (AC6, AC13) ------------------------------------------------


@pytest.mark.parametrize("constant", [MAX_VALUE_MAGNITUDE, -MAX_VALUE_MAGNITUDE, _TINY, -_TINY])
def test_constants_at_the_schema_bounds_never_raise(constant: float) -> None:
    candles = synthetic_candles(30, seed=42)
    rule = rule_of(
        {
            "any": [
                condition(price_operand(), "<", value_operand(constant)),
                condition(indicator_operand("sma", {"length": 3}), ">", value_operand(constant)),
            ]
        }
    )

    evaluation = evaluate(rule, candles)

    assert type(evaluation.triggered) is bool


# --- obv on zero volume (AC6) -------------------------------------------------------------------


def test_obv_and_its_signal_never_raise_on_zero_volume() -> None:
    candles = candles_from_prices([100.0] * 40, volume=[0.0] * 40)
    rule = rule_of(
        {
            "any": [
                condition(indicator_operand("obv", output="value"), ">", value_operand(0)),
                condition(indicator_operand("obv", output="signal"), ">", value_operand(0)),
            ]
        }
    )

    evaluations = evaluate_each(rule, candles)

    assert all(type(evaluation.triggered) is bool for evaluation in evaluations)
    assert all(
        value == 0.0 for evaluation in evaluations for value in evaluation.indicator_values.values()
    )


# --- A warmup far above the frame length (AC7, AC13) -------------------------------------------


def test_a_warmup_far_above_the_frame_length_never_fires_or_raises() -> None:
    rule = rule_of(
        {"all": [condition(price_operand(), ">", indicator_operand("sma", {"length": 500}))]}
    )
    candles = candles_from_prices([100.0, 101.0, 99.0, 102.0, 98.0])

    assert rule.warmup() == 500
    evaluations = evaluate_each(rule, candles)

    assert not any(evaluation.triggered for evaluation in evaluations)
    assert all(evaluation.indicator_values == IndicatorValues() for evaluation in evaluations)
