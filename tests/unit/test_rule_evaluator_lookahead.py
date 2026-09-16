"""Anti look-ahead tests of the rule evaluator (spec 007, T8, AC17; CLAUDE.md rule 4).

``evaluate(rule, frame)`` is a last-candle function, so it is checked with
``assert_no_lookahead_point_in_time`` against ``evaluations_by_candle(rule)``: the whole-frame
``evaluate_each`` indexed by candle open time. Comparison is exact (``rtol = atol = 0``): TA-Lib
recurrences and the condition layer are bitwise stable, so a tolerance would hide a real peek.
Two cheating references prove the check is not vacuous.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.evaluations import evaluations_by_candle, triggered_by_candle
from tests.fixtures.rules import (
    architecture_example,
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    value_operand,
)
from tests.lookahead import (
    LookaheadError,
    assert_no_lookahead,
    assert_no_lookahead_point_in_time,
)
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.evaluator import Evaluation, evaluate, evaluate_each
from trading_bot.domain.rules.schema import Rule, parse_rule

SCENARIO_SEEDS = {scenario: seed for seed, scenario in enumerate(Scenario, start=71)}


def rule_of(conditions: JsonValue) -> Rule:
    return parse_rule(rule_payload(conditions))


def price_only_rule() -> Rule:
    """Warmup 2 and no indicator: cheap enough for full sweeps."""
    return rule_of(
        {"all": [condition(price_operand("close"), "crosses_above", price_operand("open"))]}
    )


def architecture_rule() -> Rule:
    """The ``## Rule model`` example: a crossover with a constant, a long warmup, ``all``."""
    return parse_rule(architecture_example())


def nested_rule() -> Rule:
    """Both group modes at both levels."""
    return rule_of(
        {
            "any": [
                {
                    "all": [
                        condition(indicator_operand("rsi"), "<", value_operand(50)),
                        condition(price_operand(), ">", indicator_operand("sma")),
                    ]
                },
                {
                    "any": [
                        condition(
                            price_operand(),
                            "crosses_below",
                            indicator_operand("ema", {"length": 10}),
                        ),
                        condition(price_operand("volume"), ">", indicator_operand("volume_sma")),
                    ]
                },
            ]
        }
    )


def wide_rule() -> Rule:
    """Nine conditions over the ten indicators, with multi-output pairs and undefined regions."""
    return rule_of(
        {
            "any": [
                condition(
                    indicator_operand("macd", output="macd"),
                    "crosses_above",
                    indicator_operand("macd", output="signal"),
                ),
                condition(
                    indicator_operand("obv", output="value"),
                    ">",
                    indicator_operand("obv", output="signal"),
                ),
                condition(indicator_operand("adx"), ">", value_operand(25)),
                condition(
                    indicator_operand("stoch", output="k"),
                    "crosses_below",
                    indicator_operand("stoch", output="d"),
                ),
                condition(indicator_operand("rsi"), "<", value_operand(30)),
                condition(price_operand(), "<", indicator_operand("bbands", output="lower")),
                condition(indicator_operand("atr"), ">", value_operand(1)),
                condition(indicator_operand("ema", {"length": 50}), ">", indicator_operand("sma")),
                condition(price_operand("volume"), ">", indicator_operand("volume_sma")),
            ]
        }
    )


# Each frame is at least 120 candles (scenario features need 60) and at least 60 candles longer
# than the rule's warmup, so the checks cover candles on both sides of the warmup gate. The
# harness compares every evaluation field by field, so its cost grows with the frame length.
RULES: dict[str, tuple[Callable[[], Rule], int]] = {
    "price_only": (price_only_rule, 120),
    "architecture": (architecture_rule, 260),
    "nested": (nested_rule, 120),
    "wide": (wide_rule, 150),
}


def last_candle(rule: Rule) -> Callable[[pd.DataFrame], object]:
    def evaluate_last(candles: pd.DataFrame) -> object:
        return evaluate(rule, candles)

    return evaluate_last


def test_the_rule_set_matches_the_spec() -> None:
    wide = wide_rule()
    indicators = {
        operand.indicator
        for item in wide.all_conditions
        for operand in (item.left, item.right)
        if hasattr(operand, "indicator")
    }

    assert price_only_rule().warmup() == 2
    assert architecture_rule().warmup() == 200
    assert {member.mode.value for member in nested_rule().conditions.members} == {"all", "any"}
    assert nested_rule().conditions.mode.value == "any"
    assert len(wide.all_conditions) == 9
    assert len(indicators) == 10
    for factory, length in RULES.values():
        assert length >= max(120, factory().warmup() + 60)


@pytest.mark.parametrize("scenario", list(Scenario))
@pytest.mark.parametrize("label", ["price_only", "architecture", "nested"])
def test_evaluate_has_no_lookahead(label: str, scenario: Scenario) -> None:
    factory, length = RULES[label]
    rule = factory()
    candles = synthetic_candles(length, seed=SCENARIO_SEEDS[scenario], scenario=scenario)

    report = assert_no_lookahead_point_in_time(
        last_candle(rule), evaluations_by_candle(rule), candles
    )

    assert report.comparisons > 0
    assert report.non_missing_values > 0
    assert (report.cuts[0], report.cuts[-1]) == (1, length - 1)


def test_evaluate_has_no_lookahead_on_the_wide_rule() -> None:
    factory, length = RULES["wide"]
    rule = factory()
    candles = synthetic_candles(
        length, seed=SCENARIO_SEEDS[Scenario.MIXED], scenario=Scenario.MIXED
    )

    report = assert_no_lookahead_point_in_time(
        last_candle(rule), evaluations_by_candle(rule), candles
    )

    assert report.non_missing_values > 0
    assert any(evaluation.triggered for evaluation in evaluate_each(rule, candles))


@pytest.mark.parametrize("scenario", [Scenario.RANDOM_WALK, Scenario.MIXED])
def test_a_full_sweep_of_the_price_only_rule(scenario: Scenario) -> None:
    rule = price_only_rule()
    candles = synthetic_candles(60, seed=SCENARIO_SEEDS[scenario], scenario=scenario)
    sweep = len(candles)

    point_in_time = assert_no_lookahead_point_in_time(
        last_candle(rule), evaluations_by_candle(rule), candles, max_cuts=sweep
    )
    trigger_only = assert_no_lookahead(triggered_by_candle(rule), candles, max_cuts=sweep)

    assert point_in_time.cuts == tuple(range(1, sweep))
    assert trigger_only.non_missing_values > 0
    assert sum(evaluation.triggered for evaluation in evaluate_each(rule, candles)) >= 3


def test_the_references_describe_every_candle_by_open_time() -> None:
    rule = price_only_rule()
    candles = synthetic_candles(20, seed=1)

    evaluations = evaluations_by_candle(rule)(candles)
    triggered = triggered_by_candle(rule)(candles)

    assert isinstance(evaluations, pd.Series)
    assert isinstance(triggered, pd.Series)
    assert evaluations.index.equals(candles.index)
    assert triggered.index.equals(candles.index)
    assert evaluations.dtype == object
    assert triggered.dtype == bool
    assert list(evaluations) == list(evaluate_each(rule, candles))
    assert list(triggered) == [evaluation.triggered for evaluation in evaluate_each(rule, candles)]


def peeking_at_the_next_candle(rule: Rule) -> Callable[[pd.DataFrame], object]:
    """A cheat: the trigger reported for candle ``i`` is the one of candle ``i + 1``."""

    def reference(candles: pd.DataFrame) -> object:
        evaluations = evaluate_each(rule, candles)
        following = [evaluation.triggered for evaluation in evaluations[1:]] + [False]
        peeked = [
            dataclasses.replace(evaluation, triggered=triggered)
            for evaluation, triggered in zip(evaluations, following, strict=True)
        ]
        return pd.Series(peeked, index=candles.index, dtype=object)

    return reference


def warmup_gate_over_the_frame(rule: Rule) -> Callable[[pd.DataFrame], object]:
    """A cheat: the warmup gate checks the frame length instead of each candle's history."""

    def reference(candles: pd.DataFrame) -> object:
        ready = len(candles) >= rule.warmup()
        gated: list[Evaluation] = [
            dataclasses.replace(evaluation, triggered=ready and any(evaluation.condition_results))
            for evaluation in evaluate_each(rule, candles)
        ]
        return pd.Series(gated, index=candles.index, dtype=object)

    return reference


@pytest.mark.parametrize(
    ("rule_factory", "cheat"),
    [
        (price_only_rule, peeking_at_the_next_candle),
        (
            lambda: rule_of(
                {
                    "any": [
                        condition(price_operand("close"), ">", price_operand("open")),
                        condition(price_operand(), ">", indicator_operand("sma", {"length": 30})),
                    ]
                }
            ),
            warmup_gate_over_the_frame,
        ),
    ],
    ids=["reads_the_next_candle", "frame_level_warmup_gate"],
)
def test_the_check_detects_a_cheating_reference(
    rule_factory: Callable[[], Rule], cheat: Callable[[Rule], Callable[[pd.DataFrame], object]]
) -> None:
    rule = rule_factory()
    candles = synthetic_candles(60, seed=5, scenario=Scenario.MIXED)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            last_candle(rule), cheat(rule), candles, max_cuts=len(candles)
        )

    assert caught.value.violation.kind == "value_mismatch"
