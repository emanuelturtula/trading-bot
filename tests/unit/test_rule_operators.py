"""Case tables of the rule evaluator semantics (spec 007, T2-T5).

T2 pins the comparison table of Design 5.1, T3 the crossover touch table of Design 5.2, T4 the
group logic and T5 NaN, the warmup gate and edge frames. Every expectation is a literal written
from the spec, never recomputed with the code under test, and every test runs with warnings
turned into errors: evaluating NaN must neither raise nor warn (AC6).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from tests.fixtures.rules import (
    architecture_example,
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    value_operand,
)
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules import evaluator
from trading_bot.domain.rules.evaluator import Evaluation, evaluate, evaluate_each
from trading_bot.domain.rules.schema import Operator, Rule, ValueOperand, parse_rule
from trading_bot.domain.signals import IndicatorValues

pytestmark = pytest.mark.filterwarnings("error")

COMPARISONS: Final = ("<", "<=", ">", ">=")
CROSSOVERS: Final = ("crosses_above", "crosses_below")

# Design 5.1, row by row: the outcome of `left OP right` at the evaluated candle.
COMPARISON_TABLE: Final = {
    "less": {"<": True, "<=": True, ">": False, ">=": False},
    "equal": {"<": False, "<=": True, ">": False, ">=": True},
    "greater": {"<": False, "<=": False, ">": True, ">=": True},
    "nan_left": {"<": False, "<=": False, ">": False, ">=": False},
    "nan_right": {"<": False, "<=": False, ">": False, ">=": False},
    "both_nan": {"<": False, "<=": False, ">": False, ">=": False},
}

# Design 5.2: `close` against the constant 100, and the same path mirrored around 100.
TOUCH_CLOSES: Final = (99.0, 100.0, 101.0, 101.0, 100.0, 99.0, 100.0, 100.0, 101.0)
TOUCH_CROSSES_ABOVE: Final = (False, False, True, False, False, False, False, False, True)
TOUCH_CROSSES_BELOW: Final = (False, False, False, False, False, True, False, False, False)
MIRRORED_CLOSES: Final = (101.0, 100.0, 99.0, 99.0, 100.0, 101.0, 100.0, 100.0, 99.0)
MIRRORED_CROSSES_ABOVE: Final = (False, False, False, False, False, True, False, False, False)
MIRRORED_CROSSES_BELOW: Final = (False, False, True, False, False, False, False, False, True)

STOCH_1 = indicator_operand("stoch", {"length": 1, "smooth_k": 1, "smooth_d": 1}, "k")
SMA_2 = indicator_operand("sma", {"length": 2})
SMA_3 = indicator_operand("sma", {"length": 3})

# T4: three independent conditions over eight candles, one per combination of outcomes.
CLOSE_ABOVE_100 = condition(price_operand("close"), ">", value_operand(100))
VOLUME_ABOVE_5 = condition(price_operand("volume"), ">", value_operand(5))
HIGH_ABOVE_200 = condition(price_operand("high"), ">", value_operand(200))
A: Final = (False, False, False, False, True, True, True, True)
B: Final = (False, False, True, True, False, False, True, True)
C: Final = (False, True, False, True, False, True, False, True)


def rule_of(conditions: JsonValue, **overrides: JsonValue) -> Rule:
    return parse_rule(rule_payload(conditions, **overrides))


def triggered(evaluations: tuple[Evaluation, ...]) -> tuple[bool, ...]:
    return tuple(evaluation.triggered for evaluation in evaluations)


def results(evaluations: tuple[Evaluation, ...], position: int = 0) -> tuple[bool, ...]:
    return tuple(evaluation.condition_results[position] for evaluation in evaluations)


def eight_combinations() -> pd.DataFrame:
    return candles_from_prices(
        [90.0, 90.0, 90.0, 90.0, 110.0, 110.0, 110.0, 110.0],
        high=[150.0, 250.0, 150.0, 250.0, 150.0, 250.0, 150.0, 250.0],
        volume=[1.0, 1.0, 10.0, 10.0, 1.0, 1.0, 10.0, 10.0],
    )


# --- T2: comparisons (AC4, AC6) -------------------------------------------------------------


def test_the_case_tables_cover_every_operator() -> None:
    rows = {frozenset(row) for row in COMPARISON_TABLE.values()}

    assert rows == {frozenset(COMPARISONS)}
    assert {Operator(operator) for operator in (*COMPARISONS, *CROSSOVERS)} == set(Operator)
    assert set(COMPARISON_TABLE) == {
        "less",
        "equal",
        "greater",
        "nan_left",
        "nan_right",
        "both_nan",
    }


@pytest.mark.parametrize("operator", COMPARISONS)
@pytest.mark.parametrize(("case", "position"), [("less", 0), ("equal", 1), ("greater", 2)])
def test_comparison_of_a_price_on_the_left(operator: str, case: str, position: int) -> None:
    rule = rule_of({"all": [condition(price_operand(), operator, value_operand(100))]})
    candles = candles_from_prices([99.0, 100.0, 101.0])

    evaluation = evaluate_each(rule, candles)[position]

    expected = COMPARISON_TABLE[case][operator]
    assert evaluation.condition_results == (expected,)
    assert evaluation.triggered is expected


@pytest.mark.parametrize("operator", COMPARISONS)
@pytest.mark.parametrize(("case", "position"), [("less", 0), ("equal", 1), ("greater", 2)])
def test_comparison_of_a_price_on_the_right(operator: str, case: str, position: int) -> None:
    rule = rule_of({"all": [condition(value_operand(100), operator, price_operand())]})
    candles = candles_from_prices([101.0, 100.0, 99.0])

    evaluation = evaluate_each(rule, candles)[position]

    expected = COMPARISON_TABLE[case][operator]
    assert evaluation.condition_results == (expected,)
    assert evaluation.triggered is expected


@pytest.mark.parametrize("constant", [-1e15, 0.0, 1e15])
@pytest.mark.parametrize("operator", COMPARISONS)
@pytest.mark.parametrize("case", ["nan_left", "nan_right"])
def test_comparison_with_one_nan_side_is_false(case: str, operator: str, constant: float) -> None:
    sides = (SMA_2, value_operand(constant))
    left, right = sides if case == "nan_left" else sides[::-1]
    rule = rule_of({"all": [condition(left, operator, right)]})

    evaluation = evaluate_each(rule, candles_from_prices([100.0, 100.0, 100.0]))[0]

    assert evaluation.condition_results == (COMPARISON_TABLE[case][operator],)
    assert evaluation.triggered is False


@pytest.mark.parametrize("operator", COMPARISONS)
@pytest.mark.parametrize(("left", "right"), [(SMA_2, SMA_3), (SMA_3, SMA_2)])
def test_comparison_of_two_nan_sides_is_false(
    operator: str, left: dict[str, JsonValue], right: dict[str, JsonValue]
) -> None:
    rule = rule_of({"all": [condition(left, operator, right)]})

    evaluation = evaluate_each(rule, candles_from_prices([100.0, 100.0, 100.0]))[0]

    assert evaluation.condition_results == (COMPARISON_TABLE["both_nan"][operator],)
    assert evaluation.triggered is False


def test_comparison_results_are_plain_booleans() -> None:
    rule = rule_of(
        {"any": [condition(price_operand(), "<", value_operand(100)), condition(SMA_2, ">", SMA_3)]}
    )

    for evaluation in evaluate_each(rule, candles_from_prices([99.0, 100.0, 101.0])):
        assert type(evaluation.triggered) is bool
        assert all(type(result) is bool for result in evaluation.condition_results)


@pytest.mark.parametrize(
    ("operator", "expected"), [("<", False), ("<=", True), (">", False), (">=", True)]
)
def test_a_negative_zero_constant_equals_a_computed_zero(operator: str, expected: bool) -> None:
    hist = indicator_operand("macd", output="hist")
    rule = rule_of({"all": [condition(hist, operator, value_operand(-0.0))]})
    constant = rule.all_conditions[0].right
    assert isinstance(constant, ValueOperand)
    assert math.copysign(1.0, constant.value) == -1.0

    evaluation = evaluate(rule, candles_from_prices([100.0] * 60))

    assert evaluation.indicator_values["macd(fast=12, slow=26, signal=9).hist"] == 0.0
    assert evaluation.condition_results == (expected,)


# --- T3: crossovers (AC5, AC6) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("closes", "operator", "expected"),
    [
        (TOUCH_CLOSES, "crosses_above", TOUCH_CROSSES_ABOVE),
        (TOUCH_CLOSES, "crosses_below", TOUCH_CROSSES_BELOW),
        (MIRRORED_CLOSES, "crosses_above", MIRRORED_CROSSES_ABOVE),
        (MIRRORED_CLOSES, "crosses_below", MIRRORED_CROSSES_BELOW),
    ],
)
def test_the_touch_table(
    closes: tuple[float, ...], operator: str, expected: tuple[bool, ...]
) -> None:
    rule = rule_of({"all": [condition(price_operand(), operator, value_operand(100))]})

    evaluations = evaluate_each(rule, candles_from_prices(closes))

    assert results(evaluations) == expected
    assert triggered(evaluations) == expected


@pytest.mark.parametrize(
    ("operator", "expected"),
    [("crosses_below", TOUCH_CROSSES_ABOVE), ("crosses_above", TOUCH_CROSSES_BELOW)],
)
def test_the_touch_table_with_the_constant_on_the_left(
    operator: str, expected: tuple[bool, ...]
) -> None:
    rule = rule_of({"all": [condition(value_operand(100), operator, price_operand())]})

    evaluations = evaluate_each(rule, candles_from_prices(TOUCH_CLOSES))

    assert results(evaluations) == expected


@pytest.mark.parametrize(
    ("operator", "closes"),
    [("crosses_above", [101.0, 102.0]), ("crosses_below", [99.0, 98.0])],
)
def test_the_first_candle_never_crosses(operator: str, closes: list[float]) -> None:
    rule = rule_of({"all": [condition(price_operand(), operator, value_operand(100))]})

    single = evaluate(rule, candles_from_prices(closes[:1]))
    first = evaluate_each(rule, candles_from_prices(closes))[0]

    assert single.condition_results == (False,)
    assert single.triggered is False
    assert first.condition_results == (False,)


@pytest.mark.parametrize(
    ("operator", "constant", "closes", "highs", "lows", "expected"),
    [
        # stoch(1, 1, 1).k is NaN exactly on a candle whose high equals its low.
        ("crosses_above", 40, [96.0, 104.0], [110.0, 110.0], [90.0, 90.0], True),  # control
        ("crosses_above", 40, [96.0, 100.0], [110.0, 100.0], [90.0, 100.0], False),  # NaN at t
        ("crosses_above", 40, [100.0, 104.0], [100.0, 110.0], [100.0, 90.0], False),  # NaN at t-1
        ("crosses_below", 60, [104.0, 96.0], [110.0, 110.0], [90.0, 90.0], True),  # control
        ("crosses_below", 60, [104.0, 100.0], [110.0, 100.0], [90.0, 100.0], False),  # NaN at t
        ("crosses_below", 60, [100.0, 96.0], [100.0, 110.0], [100.0, 90.0], False),  # NaN at t-1
    ],
)
@pytest.mark.parametrize("indicator_side", ["left", "right"])
def test_a_nan_at_either_candle_of_either_side_never_crosses(
    indicator_side: str,
    operator: str,
    constant: float,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    expected: bool,
) -> None:
    if indicator_side == "left":
        payload = condition(STOCH_1, operator, value_operand(constant))
    else:
        mirrored = "crosses_below" if operator == "crosses_above" else "crosses_above"
        payload = condition(value_operand(constant), mirrored, STOCH_1)
    rule = rule_of({"all": [payload]})

    evaluation = evaluate(rule, candles_from_prices(closes, high=highs, low=lows))

    assert evaluation.condition_results == (expected,)
    assert evaluation.triggered is expected


def test_crossover_consequences_on_a_random_walk() -> None:
    sma = indicator_operand("sma", {"length": 5})
    rule = rule_of(
        {
            "any": [
                condition(price_operand(), "crosses_above", sma),
                condition(price_operand(), "crosses_below", sma),
                condition(price_operand(), ">", sma),
                condition(price_operand(), "<", sma),
            ]
        }
    )
    evaluations = evaluate_each(rule, synthetic_candles(300, seed=5))
    above, below, greater, less = (
        np.array(results(evaluations, position)) for position in range(4)
    )

    assert above.sum() > 5
    assert below.sum() > 5
    assert not (above & ~greater).any(), "crosses_above implies > at the candle"
    assert not (below & ~less).any(), "crosses_below implies < at the candle"
    assert not (above & below).any(), "the two crossovers are mutually exclusive"
    assert not (above[1:] & above[:-1]).any(), "crosses_above never fires twice in a row"
    assert not (below[1:] & below[:-1]).any(), "crosses_below never fires twice in a row"


def test_two_candles_are_enough_for_a_price_crossover() -> None:
    rule = rule_of({"all": [condition(price_operand(), "crosses_above", value_operand(100))]})

    evaluation = evaluate(rule, candles_from_prices([100.0, 101.0]))

    assert rule.warmup() == 2
    assert evaluation.condition_results == (True,)
    assert evaluation.triggered is True


def test_two_candles_are_enough_for_a_crossover_of_two_prices() -> None:
    rule = rule_of({"all": [condition(price_operand(), "crosses_above", price_operand("open"))]})
    # Candle 0 closes below its open (99 < 100), candle 1 above it (101 > 99).
    candles = candles_from_prices([99.0, 101.0], high=[100.0, 101.0], low=[99.0, 99.0])
    candles = candles.assign(open=[100.0, 99.0])

    evaluations = evaluate_each(rule, candles)

    assert rule.warmup() == 2
    assert triggered(evaluations) == (False, True)
    assert evaluate(rule, candles).triggered is True


# --- T4: groups (AC8) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        (
            {"all": [CLOSE_ABOVE_100, VOLUME_ABOVE_5, HIGH_ABOVE_200]},
            (False, False, False, False, False, False, False, True),
        ),
        (
            {"any": [CLOSE_ABOVE_100, VOLUME_ABOVE_5, HIGH_ABOVE_200]},
            (False, True, True, True, True, True, True, True),
        ),
        (
            {"all": [{"any": [CLOSE_ABOVE_100, VOLUME_ABOVE_5]}, HIGH_ABOVE_200]},
            (False, False, False, True, False, True, False, True),
        ),
        (
            {"any": [{"all": [CLOSE_ABOVE_100, VOLUME_ABOVE_5]}, HIGH_ABOVE_200]},
            (False, True, False, True, False, True, True, True),
        ),
        (
            {"all": [{"all": [CLOSE_ABOVE_100]}, {"any": [VOLUME_ABOVE_5, HIGH_ABOVE_200]}]},
            (False, False, False, False, False, True, True, True),
        ),
        (
            {"any": [{"any": [CLOSE_ABOVE_100]}, {"all": [VOLUME_ABOVE_5, HIGH_ABOVE_200]}]},
            (False, False, False, True, True, True, True, True),
        ),
    ],
    ids=["all", "any", "all_of_any", "any_of_all", "all_of_groups", "any_of_groups"],
)
def test_group_logic(conditions: JsonValue, expected: tuple[bool, ...]) -> None:
    evaluations = evaluate_each(rule_of(conditions), eight_combinations())

    assert triggered(evaluations) == expected
    assert [evaluation.condition_results for evaluation in evaluations] == [
        tuple(zip(A, B, C, strict=True))[position] for position in range(8)
    ]


@pytest.mark.parametrize(
    "conditions",
    [
        {"all": [CLOSE_ABOVE_100]},
        {"any": [CLOSE_ABOVE_100]},
        {"all": [{"any": [CLOSE_ABOVE_100]}]},
        {"any": [{"all": [CLOSE_ABOVE_100]}]},
    ],
    ids=["all", "any", "all_of_any", "any_of_all"],
)
def test_a_group_of_one_member_equals_that_member(conditions: JsonValue) -> None:
    evaluations = evaluate_each(rule_of(conditions), eight_combinations())

    assert triggered(evaluations) == A
    assert results(evaluations) == A


@pytest.mark.parametrize(
    "conditions",
    [
        {"all": [CLOSE_ABOVE_100, CLOSE_ABOVE_100]},
        {"any": [CLOSE_ABOVE_100, {"all": [CLOSE_ABOVE_100]}]},
    ],
    ids=["flat", "nested"],
)
def test_duplicated_conditions_keep_one_result_each(conditions: JsonValue) -> None:
    rule = rule_of(conditions)

    evaluations = evaluate_each(rule, eight_combinations())

    assert len(rule.all_conditions) == 2
    assert [evaluation.condition_results for evaluation in evaluations] == [
        (outcome, outcome) for outcome in A
    ]
    assert triggered(evaluations) == A


@pytest.mark.parametrize(("mode", "expected"), [("all", False), ("any", True)])
def test_contradictory_conditions(mode: str, expected: bool) -> None:
    contradiction = [CLOSE_ABOVE_100, condition(price_operand(), "<=", value_operand(100))]

    evaluations = evaluate_each(rule_of({mode: contradiction}), eight_combinations())

    assert triggered(evaluations) == (expected,) * 8


def test_condition_results_follow_the_document_order_of_all_conditions() -> None:
    rule = rule_of(
        {
            "any": [
                HIGH_ABOVE_200,
                {"all": [VOLUME_ABOVE_5, CLOSE_ABOVE_100]},
                {"any": [condition(price_operand(), "<=", value_operand(100))]},
            ]
        }
    )

    evaluations = evaluate_each(rule, eight_combinations())

    assert len(rule.all_conditions) == 4
    assert [evaluation.condition_results for evaluation in evaluations] == [
        (c, b, a, not a) for a, b, c in zip(A, B, C, strict=True)
    ]


def test_evaluation_is_never_short_circuited() -> None:
    never = condition(price_operand(), ">", value_operand(1e15))
    rsi = condition(indicator_operand("rsi", {"length": 2}), "<", value_operand(101))
    sma = condition(SMA_2, ">", value_operand(0))
    rule = rule_of({"all": [never, rsi, sma]})

    evaluation = evaluate(rule, candles_from_prices([100.0, 101.0, 102.0, 101.0]))

    assert evaluation.triggered is False
    assert evaluation.condition_results == (False, True, True)
    assert list(evaluation.indicator_values) == ["rsi(length=2).value", "sma(length=2).value"]


# --- T5: NaN, warmup and edge frames (AC6, AC7, AC11, AC13) ---------------------------------


@pytest.mark.parametrize(
    ("indicator", "output"), [("rsi", None), ("adx", None), ("stoch", "k"), ("stoch", "d")]
)
def test_indicators_undefined_on_flat_data_never_fire(indicator: str, output: str | None) -> None:
    operand = indicator_operand(indicator, output=output)
    rule = rule_of(
        {
            "any": [
                condition(operand, ">=", value_operand(-1)),
                condition(operand, "<=", value_operand(101)),
                condition(operand, "crosses_above", value_operand(-1)),
                condition(value_operand(101), "crosses_below", operand),
            ]
        }
    )

    evaluations = evaluate_each(rule, candles_from_prices([100.0] * 80))

    assert not any(triggered(evaluations))
    assert all(evaluation.condition_results == (False,) * 4 for evaluation in evaluations)
    assert all(evaluation.indicator_values == IndicatorValues() for evaluation in evaluations)


def test_the_warmup_gate_holds_back_a_cheap_any_leg() -> None:
    rule = rule_of(
        {
            "any": [
                condition(price_operand(), ">", value_operand(50)),
                condition(price_operand(), ">", indicator_operand("sma", {"length": 200})),
            ]
        }
    )

    evaluation = evaluate(rule, candles_from_prices([100.0] * 30))

    assert rule.warmup() == 200
    assert evaluation.condition_results == (True, False)
    assert evaluation.triggered is False
    assert evaluation.close_price == 100.0
    assert evaluation.indicator_values == IndicatorValues()


def test_a_rule_fires_from_exactly_its_warmup() -> None:
    rule = rule_of(
        {
            "any": [
                condition(price_operand(), ">", value_operand(50)),
                condition(price_operand(), ">", indicator_operand("sma", {"length": 200})),
            ]
        }
    )

    evaluations = evaluate_each(rule, candles_from_prices([100.0] * 250))

    assert triggered(evaluations) == (False,) * 199 + (True,) * 51
    assert all(evaluation.condition_results == (True, False) for evaluation in evaluations)
    assert "sma(length=200).value" not in evaluations[198].indicator_values
    assert evaluations[199].indicator_values == IndicatorValues({"sma(length=200).value": 100.0})


def test_a_frame_shorter_than_the_warmup_never_fires() -> None:
    rule = parse_rule(architecture_example())
    cheap = rule_of(
        {
            "any": [
                condition(price_operand(), ">", value_operand(0)),
                condition(price_operand(), ">", indicator_operand("sma", {"length": 200})),
            ]
        }
    )
    candles = synthetic_candles(199, seed=11)

    assert not any(triggered(evaluate_each(rule, candles)))
    assert not any(triggered(evaluate_each(cheap, candles)))
    assert evaluate(cheap, candles).condition_results == (True, False)


def test_an_empty_frame_has_no_candle_to_evaluate() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    empty = candles_from_prices([])

    with pytest.raises(ValueError, match="no candle to evaluate"):
        evaluate(rule, empty)
    assert evaluate_each(rule, empty) == ()
    assert evaluate_each(rule, empty, last=3) == ()


@pytest.mark.parametrize(
    ("operator", "expected"), [(">", True), ("crosses_above", False), ("crosses_below", False)]
)
def test_a_single_candle_evaluates_without_raising(operator: str, expected: bool) -> None:
    rule = rule_of({"all": [condition(price_operand(), operator, value_operand(100))]})

    evaluation = evaluate(rule, candles_from_prices([101.0]))

    assert evaluation.condition_results == (expected,)
    assert evaluation.triggered is expected


def test_cooldown_bars_do_not_change_the_evaluation() -> None:
    payload = architecture_example()
    without_cooldown = parse_rule({**payload, "cooldown_bars": 0})
    with_cooldown = parse_rule({**payload, "cooldown_bars": 500})
    candles = synthetic_candles(260, seed=3)

    assert evaluate_each(without_cooldown, candles) == evaluate_each(with_cooldown, candles)


def test_the_evaluator_source_never_mentions_the_cooldown() -> None:
    source = Path(evaluator.__file__).read_text(encoding="utf-8")

    assert "cooldown" not in source.lower()
