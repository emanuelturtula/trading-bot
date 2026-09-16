"""Property tests over rules drawn from the real indicator catalog (spec 006, T12).

Every fixture in ``tests.fixtures.rules`` is a hand-picked example. This file instead draws
random, structurally valid rules (any registry indicator, any operator, both group modes, one
nesting level) and checks invariants that must hold for **every** such rule, not just the ones
someone remembered to write down:

- ``parse_rule(dump_rule(rule))`` is a fixed point (AC3);
- ``1 <= rule.warmup() <= rule.stable_warmup()`` (AC17);
- ``rule.warmup()``/``rule.stable_warmup()`` equal an independent reference computed directly
  from the drawn operands and ``REGISTRY.warmup``/``stable_warmup``, not by calling the method
  under test on itself (AC17);
- every generated rule's canonical dump validates against ``rule_json_schema()`` (AC20).

Rules are built from model instances (``Rule(...)``), the same trusted-construction path already
used by the developer's tests, then round-tripped through ``dump_rule``/``parse_rule`` so the
JSON path is exercised too.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import ParamKind, ParamSpec
from trading_bot.domain.rules.json_schema import rule_json_schema
from trading_bot.domain.rules.schema import (
    MAX_COOLDOWN_BARS,
    AllGroup,
    AnyGroup,
    Condition,
    IndicatorOperand,
    NestedAllGroup,
    NestedAnyGroup,
    Operator,
    PriceField,
    PriceOperand,
    Rule,
    ValueOperand,
    dump_rule,
    parse_rule,
)
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe

_MAX_ROOT_ITEMS_DRAWN = 4  # kept small so total conditions stay far below MAX_CONDITIONS (20)
_MAX_NESTED_ITEMS_DRAWN = 3

# Every character is outside the Unicode "Other" and "Separator" categories, so every drawn name
# is already `str.isprintable()` and never all-whitespace: no `assume`/filter needed downstream.
_NAME_ALPHABET = st.characters(
    min_codepoint=0x21,
    max_codepoint=0x2E7F,
    blacklist_categories=("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"),
)


def _names() -> st.SearchStrategy[str]:
    return st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=80)


def _param_value_strategy(param: ParamSpec) -> st.SearchStrategy[object]:
    if param.kind is ParamKind.INT:
        assert isinstance(param.minimum, int)
        assert isinstance(param.maximum, int)
        return st.integers(min_value=param.minimum, max_value=param.maximum)
    return st.floats(
        min_value=param.minimum, max_value=param.maximum, allow_nan=False, allow_infinity=False
    )


@st.composite
def _params_for(draw: st.DrawFn, name: str) -> dict[str, object]:
    """Valid parameters for indicator ``name``, respecting ``macd``'s ``fast < slow`` constraint."""
    spec = REGISTRY.get(name)
    if name == "macd":
        fast_spec, slow_spec, signal_spec = spec.params
        assert isinstance(fast_spec.minimum, int)
        assert isinstance(fast_spec.maximum, int)
        assert isinstance(slow_spec.minimum, int)
        assert isinstance(slow_spec.maximum, int)
        fast = draw(st.integers(min_value=fast_spec.minimum, max_value=fast_spec.maximum - 1))
        slow = draw(
            st.integers(min_value=max(slow_spec.minimum, fast + 1), max_value=slow_spec.maximum)
        )
        signal = draw(_param_value_strategy(signal_spec))
        return {"fast": fast, "slow": slow, "signal": signal}
    return {param.name: draw(_param_value_strategy(param)) for param in spec.params}


@st.composite
def _indicator_operands(draw: st.DrawFn) -> IndicatorOperand:
    name = draw(st.sampled_from(REGISTRY.names))
    spec = REGISTRY.get(name)
    params = draw(_params_for(name))
    output = draw(st.sampled_from(spec.output_names))
    return IndicatorOperand(indicator=name, params=params, output=output)  # type: ignore[arg-type]


def _price_operands() -> st.SearchStrategy[PriceOperand]:
    return st.builds(PriceOperand, price=st.sampled_from(list(PriceField)))


def _value_operands() -> st.SearchStrategy[ValueOperand]:
    return st.builds(
        ValueOperand,
        value=st.floats(min_value=-1e15, max_value=1e15, allow_nan=False, allow_infinity=False),
    )


def _operands() -> st.SearchStrategy[IndicatorOperand | PriceOperand | ValueOperand]:
    return st.one_of(_indicator_operands(), _price_operands(), _value_operands())


@st.composite
def _conditions(draw: st.DrawFn) -> Condition:
    left = draw(_operands())
    right = draw(_operands())
    # AC12: two constants, or two operands equal after normalization, are rejected outright.
    assume(not (isinstance(left, ValueOperand) and isinstance(right, ValueOperand)))
    assume(left != right)
    op = draw(st.sampled_from(list(Operator)))
    return Condition(left=left, op=op, right=right)


@st.composite
def _nested_groups(draw: st.DrawFn) -> NestedAllGroup | NestedAnyGroup:
    conditions = draw(st.lists(_conditions(), min_size=1, max_size=_MAX_NESTED_ITEMS_DRAWN))
    if draw(st.booleans()):
        return NestedAllGroup(all=tuple(conditions))
    return NestedAnyGroup(any=tuple(conditions))


@st.composite
def _root_items(draw: st.DrawFn) -> Condition | NestedAllGroup | NestedAnyGroup:
    """A root-level member: a bare condition, or one nested group (spec 006, D6)."""
    if draw(st.booleans()):
        return draw(_conditions())
    return draw(_nested_groups())


@st.composite
def _root_groups(draw: st.DrawFn) -> AllGroup | AnyGroup:
    # At most 4 root items of at most 3 nested conditions each: well under MAX_CONDITIONS (20)
    # and MAX_GROUP_ITEMS (10), so no drawn rule is ever rejected on size alone.
    items = draw(st.lists(_root_items(), min_size=1, max_size=_MAX_ROOT_ITEMS_DRAWN))
    if draw(st.booleans()):
        return AllGroup(all=tuple(items))
    return AnyGroup(any=tuple(items))


@st.composite
def _rules(draw: st.DrawFn) -> Rule:
    return Rule(
        name=draw(_names()),
        signal=draw(st.sampled_from(list(Side))),
        timeframe=draw(st.sampled_from(list(Timeframe))),
        conditions=draw(_root_groups()),
        cooldown_bars=draw(st.integers(min_value=0, max_value=MAX_COOLDOWN_BARS)),
    )


# --- An independent warmup reference, not the private function under test -------------------


def _operand_warmup_reference(operand: object, *, stable: bool, extra: int) -> int:
    if isinstance(operand, ValueOperand):
        return 0
    if isinstance(operand, PriceOperand):
        return 1 + extra
    assert isinstance(operand, IndicatorOperand)
    lookup = REGISTRY.stable_warmup if stable else REGISTRY.warmup
    return lookup(operand.indicator, operand.params) + extra


def _condition_warmup_reference(condition: Condition, *, stable: bool) -> int:
    extra = 1 if condition.op.is_crossover else 0
    return max(
        _operand_warmup_reference(condition.left, stable=stable, extra=extra),
        _operand_warmup_reference(condition.right, stable=stable, extra=extra),
    )


def _rule_warmup_reference(rule: Rule, *, stable: bool) -> int:
    counts = [_condition_warmup_reference(c, stable=stable) for c in rule.all_conditions]
    return max([1, *counts])


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    return Draft202012Validator(rule_json_schema())


_MAX_EXAMPLES = 40
_HEALTH_CHECKS = [HealthCheck.filter_too_much]


@settings(max_examples=_MAX_EXAMPLES, deadline=None, suppress_health_check=_HEALTH_CHECKS)
@given(rule=_rules())
def test_parse_dump_round_trips_and_is_a_fixed_point(rule: Rule) -> None:
    dumped = dump_rule(rule)
    reparsed = parse_rule(dumped)

    assert reparsed == rule
    assert dump_rule(reparsed) == dumped


@settings(max_examples=_MAX_EXAMPLES, deadline=None, suppress_health_check=_HEALTH_CHECKS)
@given(rule=_rules())
def test_warmup_stays_between_one_and_stable_warmup(rule: Rule) -> None:
    assert 1 <= rule.warmup() <= rule.stable_warmup()


@settings(max_examples=_MAX_EXAMPLES, deadline=None, suppress_health_check=_HEALTH_CHECKS)
@given(rule=_rules())
def test_warmup_matches_an_independently_computed_reference(rule: Rule) -> None:
    assert rule.warmup() == _rule_warmup_reference(rule, stable=False)
    assert rule.stable_warmup() == _rule_warmup_reference(rule, stable=True)


@settings(max_examples=_MAX_EXAMPLES, deadline=None, suppress_health_check=_HEALTH_CHECKS)
@given(rule=_rules())
def test_every_generated_rule_validates_against_the_exported_schema(
    rule: Rule, schema_validator: Draft202012Validator
) -> None:
    schema_validator.validate(dump_rule(rule))
