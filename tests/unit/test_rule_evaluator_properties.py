"""Property tests and case-table completeness for the rule evaluator (spec 007, T9, T10).

T9 draws rules from ``tests.fixtures.rule_strategies`` (cheap indicator parameters, so
``rule.warmup()`` stays small) and checks invariants that must hold for **every** such rule and
frame, not only the hand-picked examples of ``test_rule_evaluator.py`` and
``test_rule_operators.py``: the two entry points agree, purity and determinism, the warmup gate,
crossover mutual exclusion and implication, that ``<``/``>=`` are complementary on finite operands
and both ``False`` on NaN (verified against an independent recomputation of the operand arrays,
not the evaluator's own arrays), monotonicity in a comparison constant, and the anti look-ahead
property. T10 turns the enum coverage of the operator and group-mode tables into an explicit
assertion, so a new ``Operator`` or ``GroupMode`` member that nobody wires into a test fails the
suite instead of being silently unexercised.

Every test runs with warnings turned into errors, like ``test_rule_operators.py`` (AC6).
"""

from __future__ import annotations

import itertools
import math
from typing import Final

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.evaluations import evaluations_by_candle
from tests.fixtures.indicator_frames import candles_from_prices
from tests.fixtures.rule_strategies import (
    cheap_indicator_operands,
    cheap_rules,
    distinct_operand_pairs,
    price_operands,
)
from tests.fixtures.rules import (
    Payload,
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
)
from tests.fixtures.rules import value_operand as value_operand_payload
from tests.lookahead import assert_no_lookahead_point_in_time, values_equal
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.evaluator import evaluate, evaluate_each
from trading_bot.domain.rules.schema import (
    AllGroup,
    AnyGroup,
    Condition,
    GroupMode,
    Operand,
    Operator,
    PriceOperand,
    Rule,
    ValueOperand,
    parse_rule,
)
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe

pytestmark = pytest.mark.filterwarnings("error")

_MAX_EXAMPLES = 20
_LOOKAHEAD_EXAMPLES = 12
_HEALTH_CHECKS = [HealthCheck.filter_too_much, HealthCheck.too_slow]
_EXTRA_CANDLES = 20  # candles beyond the rule's own warmup, so both sides of the gate are covered
_SEED_UPPER_BOUND = 2**16
_SETTINGS: Final = {
    "max_examples": _MAX_EXAMPLES,
    "deadline": None,  # CI runners stall; budgets are reported with --durations, never asserted
    "suppress_health_check": _HEALTH_CHECKS,
}


def _a_rule(conditions: tuple[Condition, ...], *, mode: GroupMode = GroupMode.ALL) -> Rule:
    group = AllGroup(all=conditions) if mode is GroupMode.ALL else AnyGroup(any=conditions)
    return Rule(name="probe", signal=Side.BUY, timeframe=Timeframe.D1, conditions=group)


def _operand_values(operand: Operand, candles: pd.DataFrame) -> np.ndarray:
    """Independent recomputation of Design §4, not the evaluator's own arrays."""
    if isinstance(operand, ValueOperand):
        return np.full(len(candles), operand.value, dtype=np.float64)
    if isinstance(operand, PriceOperand):
        return np.asarray(candles[operand.price.value], dtype=np.float64)
    return np.asarray(
        REGISTRY.compute(operand.indicator, operand.params, candles)[operand.output],
        dtype=np.float64,
    )


# --- T9: the two forms, purity, determinism and the warmup gate -----------------------------


@st.composite
def _rule_and_frame(draw: st.DrawFn) -> tuple[Rule, pd.DataFrame]:
    rule = draw(cheap_rules())
    length = rule.warmup() + _EXTRA_CANDLES
    seed = draw(st.integers(min_value=0, max_value=_SEED_UPPER_BOUND))
    scenario = draw(st.sampled_from(list(Scenario)))
    candles = synthetic_candles(length, seed=seed, scenario=scenario)
    return rule, candles


@settings(**_SETTINGS)
@given(pair=_rule_and_frame())
def test_the_two_forms_agree_are_deterministic_pure_and_respect_the_warmup_gate(
    pair: tuple[Rule, pd.DataFrame],
) -> None:
    rule, candles = pair
    snapshot = candles.copy(deep=True)

    single = evaluate(rule, candles)
    each = evaluate_each(rule, candles)
    again = evaluate(rule, candles.copy(deep=True))

    pd.testing.assert_frame_equal(candles, snapshot, check_exact=True)
    assert single == each[-1]
    assert values_equal(single, each[-1])
    assert values_equal(single, again)
    for k in (0, 1, len(each) // 2, len(each)):
        assert evaluate_each(rule, candles, last=k) == each[len(each) - k :]
    for position, evaluation in enumerate(each):
        if position + 1 < rule.warmup():
            assert evaluation.triggered is False


# --- T9: crossovers over drawn operand pairs (AC5) -------------------------------------------


@st.composite
def _crossover_rule_and_frame(draw: st.DrawFn) -> tuple[Rule, pd.DataFrame]:
    left, right = draw(distinct_operand_pairs())
    rule = _a_rule(
        (
            Condition(left=left, op=Operator.CROSSES_ABOVE, right=right),
            Condition(left=left, op=Operator.CROSSES_BELOW, right=right),
            Condition(left=left, op=Operator.GREATER, right=right),
            Condition(left=left, op=Operator.LESS, right=right),
        ),
        mode=GroupMode.ANY,
    )
    length = rule.warmup() + _EXTRA_CANDLES
    seed = draw(st.integers(min_value=0, max_value=_SEED_UPPER_BOUND))
    candles = synthetic_candles(length, seed=seed, scenario=Scenario.MIXED)
    return rule, candles


@settings(**_SETTINGS)
@given(pair=_crossover_rule_and_frame())
def test_crossover_properties_hold_for_drawn_operand_pairs(
    pair: tuple[Rule, pd.DataFrame],
) -> None:
    rule, candles = pair
    evaluations = evaluate_each(rule, candles)
    above = [e.condition_results[0] for e in evaluations]
    below = [e.condition_results[1] for e in evaluations]
    greater = [e.condition_results[2] for e in evaluations]
    less = [e.condition_results[3] for e in evaluations]

    assert above[0] is False, "the first candle of a frame never crosses"
    assert below[0] is False, "the first candle of a frame never crosses"
    assert not any(a and b for a, b in zip(above, below, strict=True)), "mutually exclusive"
    assert all(g for a, g in zip(above, greater, strict=True) if a), "crosses_above implies >"
    assert all(lt for b, lt in zip(below, less, strict=True) if b), "crosses_below implies <"
    assert not any(a1 and a2 for a1, a2 in itertools.pairwise(above)), "never twice in a row"
    assert not any(b1 and b2 for b1, b2 in itertools.pairwise(below)), "never twice in a row"


# --- T9: "<" and ">=" are complementary on finite operands, both False on NaN (AC4, AC9) ----


@st.composite
def _comparison_rule_and_frame(
    draw: st.DrawFn,
) -> tuple[Rule, Operand, Operand, pd.DataFrame]:
    left, right = draw(distinct_operand_pairs())
    rule = _a_rule(
        (
            Condition(left=left, op=Operator.LESS, right=right),
            Condition(left=left, op=Operator.GREATER_OR_EQUAL, right=right),
        )
    )
    length = rule.warmup() + _EXTRA_CANDLES
    seed = draw(st.integers(min_value=0, max_value=_SEED_UPPER_BOUND))
    candles = synthetic_candles(length, seed=seed, scenario=Scenario.MIXED)
    return rule, left, right, candles


@settings(**_SETTINGS)
@given(pair=_comparison_rule_and_frame())
def test_less_than_and_greater_or_equal_are_complementary_or_both_false_on_nan(
    pair: tuple[Rule, Operand, Operand, pd.DataFrame],
) -> None:
    rule, left, right, candles = pair
    left_values = _operand_values(left, candles)
    right_values = _operand_values(right, candles)
    less = [e.condition_results[0] for e in evaluate_each(rule, candles)]
    greater_or_equal = [e.condition_results[1] for e in evaluate_each(rule, candles)]

    for l_value, r_value, is_less, is_ge in zip(
        left_values, right_values, less, greater_or_equal, strict=True
    ):
        if math.isnan(l_value) or math.isnan(r_value):
            assert (is_less, is_ge) == (False, False)
        else:
            assert is_less != is_ge


# --- T9: monotonicity in a comparison constant -----------------------------------------------


@st.composite
def _monotonic_rule_and_frame(draw: st.DrawFn) -> tuple[Rule, pd.DataFrame]:
    left = draw(st.one_of(cheap_indicator_operands(), price_operands()))
    low = draw(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
    delta = draw(st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False))
    high = low + delta
    rule = _a_rule(
        (
            Condition(left=left, op=Operator.GREATER, right=ValueOperand(value=low)),
            Condition(left=left, op=Operator.GREATER, right=ValueOperand(value=high)),
        )
    )
    length = rule.warmup() + _EXTRA_CANDLES
    seed = draw(st.integers(min_value=0, max_value=_SEED_UPPER_BOUND))
    candles = synthetic_candles(length, seed=seed, scenario=Scenario.MIXED)
    return rule, candles


@settings(**_SETTINGS)
@given(pair=_monotonic_rule_and_frame())
def test_condition_results_are_monotonic_in_a_comparison_constant(
    pair: tuple[Rule, pd.DataFrame],
) -> None:
    rule, candles = pair
    for evaluation in evaluate_each(rule, candles):
        above_low, above_high = evaluation.condition_results
        if above_high:
            assert above_low, "a value above the higher constant is above the lower one too"


# --- T9: anti look-ahead over drawn rules (AC17) ----------------------------------------------


@st.composite
def _lookahead_rule_and_frame(draw: st.DrawFn) -> tuple[Rule, pd.DataFrame]:
    rule = draw(cheap_rules())
    length = rule.warmup() + _EXTRA_CANDLES
    seed = draw(st.integers(min_value=0, max_value=_SEED_UPPER_BOUND))
    scenario = draw(st.sampled_from(list(Scenario)))
    candles = synthetic_candles(length, seed=seed, scenario=scenario)
    return rule, candles


@settings(max_examples=_LOOKAHEAD_EXAMPLES, deadline=None, suppress_health_check=_HEALTH_CHECKS)
@given(pair=_lookahead_rule_and_frame())
def test_evaluate_has_no_lookahead_for_drawn_rules(pair: tuple[Rule, pd.DataFrame]) -> None:
    rule, candles = pair

    def evaluate_last(frame: pd.DataFrame) -> object:
        return evaluate(rule, frame)

    report = assert_no_lookahead_point_in_time(
        evaluate_last, evaluations_by_candle(rule), candles, max_cuts=8
    )

    assert report.comparisons > 0


# --- T10: case-table completeness (AC4, AC8) --------------------------------------------------

_TESTED_OPERATORS: Final = ("<", "<=", ">", ">=", "crosses_above", "crosses_below")
_TESTED_GROUP_MODES: Final = ("all", "any")


def test_the_matrices_below_cover_every_operator_and_group_mode() -> None:
    """A new ``Operator`` or ``GroupMode`` member that nobody adds here fails this assertion."""
    assert {Operator(op) for op in _TESTED_OPERATORS} == set(Operator)
    assert {GroupMode(mode) for mode in _TESTED_GROUP_MODES} == set(GroupMode)


_OPERAND_KINDS: Final = ("indicator", "price", "value")
_OPERAND_KIND_PAIRS: Final = tuple(
    (left, right)
    for left in _OPERAND_KINDS
    for right in _OPERAND_KINDS
    if not (left == "value" and right == "value")
)


def _kind_operand(kind: str, *, alternate: bool) -> Payload:
    if kind == "indicator":
        if alternate:
            return indicator_operand("sma", {"length": 3})
        return indicator_operand("rsi")
    if kind == "price":
        return price_operand("close") if not alternate else price_operand("open")
    return value_operand_payload(50) if not alternate else value_operand_payload(60)


@pytest.mark.parametrize("operator", _TESTED_OPERATORS)
@pytest.mark.parametrize(("left_kind", "right_kind"), _OPERAND_KIND_PAIRS)
def test_every_operator_evaluates_for_every_operand_kind_pair(
    left_kind: str, right_kind: str, operator: str
) -> None:
    left = _kind_operand(left_kind, alternate=False)
    right = _kind_operand(right_kind, alternate=True)
    payload: dict[str, JsonValue] = rule_payload({"all": [condition(left, operator, right)]})
    rule = parse_rule(payload)
    candles = synthetic_candles(40, seed=21)

    evaluation = evaluate(rule, candles)

    assert type(evaluation.triggered) is bool
    assert len(evaluation.condition_results) == 1
    assert type(evaluation.condition_results[0]) is bool


_CLOSE_ABOVE_100: Final = condition(price_operand("close"), ">", value_operand_payload(100))


@pytest.mark.parametrize("root_mode", _TESTED_GROUP_MODES)
@pytest.mark.parametrize("nested_mode", _TESTED_GROUP_MODES)
def test_every_group_mode_pair_reduces_a_single_member_to_itself(
    root_mode: str, nested_mode: str
) -> None:
    payload: dict[str, JsonValue] = rule_payload({root_mode: [{nested_mode: [_CLOSE_ABOVE_100]}]})
    rule = parse_rule(payload)
    candles = candles_from_prices([99.0, 101.0, 100.0, 102.0])

    evaluations = evaluate_each(rule, candles)

    assert [e.triggered for e in evaluations] == [False, True, False, True]
    assert [e.condition_results for e in evaluations] == [
        (False,),
        (True,),
        (False,),
        (True,),
    ]
