"""Hypothesis strategies for cheap, evaluable rules drawn from the real catalog (spec 007, §13).

``tests.unit.test_rule_schema_properties`` (#6) already draws random rules, but its strategy is
private and tuned for a different goal: it spans the whole parameter range of every indicator to
stress round-tripping and warmup arithmetic. #7's property tests instead evaluate a rule many
times over a frame (``evaluate_each``, the look-ahead harness), so every indicator parameter here
is capped at ``CHEAP_PARAM_CAP`` (``macd``'s ``slow`` may reach ``CHEAP_PARAM_CAP + 1``, the
smallest value that still keeps ``fast < slow`` when ``fast`` is drawn at the cap):
``rule.warmup()`` stays small and a frame of a few dozen candles is enough to exercise every
candle on both sides of the warmup gate. The two strategies are deliberately not shared:
``tests/`` fixtures must not import from ``tests.unit`` (spec 004, AC17b).

Always import this module as ``tests.fixtures.rule_strategies``.
"""

from __future__ import annotations

from typing import Final

from hypothesis import strategies as st

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import IndicatorParams, ParamKind, ParamSpec, ParamValue
from trading_bot.domain.rules.schema import (
    MAX_COOLDOWN_BARS,
    AllGroup,
    AnyGroup,
    Condition,
    IndicatorOperand,
    NestedAllGroup,
    NestedAnyGroup,
    Operand,
    Operator,
    PriceField,
    PriceOperand,
    RootGroup,
    RootItem,
    Rule,
    ValueOperand,
)
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe

CHEAP_PARAM_CAP: Final = 5  # every length-like parameter stays at or below this, except
# macd's slow, which may be CHEAP_PARAM_CAP + 1 (it must be > fast, itself capped here)
MAX_ROOT_ITEMS: Final = 3  # well under MAX_GROUP_ITEMS (10) and MAX_CONDITIONS (20)
MAX_NESTED_ITEMS: Final = 2
VALUE_MAGNITUDE: Final = 1e6  # comfortably inside the schema bound (1e15), plain to reason about

_NAME_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)


def rule_names() -> st.SearchStrategy[str]:
    """Printable ASCII rule names of 1 to 40 characters."""
    return st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=40)


def _capped_int(param: ParamSpec, *, minimum: int | None = None) -> st.SearchStrategy[int]:
    assert isinstance(param.minimum, int)
    assert isinstance(param.maximum, int)
    low = param.minimum if minimum is None else max(param.minimum, minimum)
    high = max(low, min(param.maximum, CHEAP_PARAM_CAP))
    return st.integers(min_value=low, max_value=high)


def _param_value(param: ParamSpec) -> st.SearchStrategy[ParamValue]:
    if param.kind is ParamKind.INT:
        return _capped_int(param)
    assert isinstance(param.minimum, float)
    assert isinstance(param.maximum, float)
    return st.floats(
        min_value=param.minimum, max_value=param.maximum, allow_nan=False, allow_infinity=False
    )


@st.composite
def _cheap_params(draw: st.DrawFn, name: str) -> IndicatorParams:
    """Cheap, valid parameters for indicator ``name`` (``macd``'s ``fast < slow`` respected)."""
    spec = REGISTRY.get(name)
    if name == "macd":
        fast_spec, slow_spec, signal_spec = spec.params
        fast = draw(_capped_int(fast_spec))
        slow = draw(_capped_int(slow_spec, minimum=fast + 1))
        signal = draw(_param_value(signal_spec))
        return IndicatorParams({"fast": fast, "slow": slow, "signal": signal})
    values: dict[str, ParamValue] = {param.name: draw(_param_value(param)) for param in spec.params}
    return IndicatorParams(values)


@st.composite
def cheap_indicator_operands(draw: st.DrawFn) -> IndicatorOperand:
    """A registry indicator and output with parameters capped for a small ``warmup()``."""
    name = draw(st.sampled_from(REGISTRY.names))
    spec = REGISTRY.get(name)
    params = draw(_cheap_params(name))
    output = draw(st.sampled_from(spec.output_names))
    return IndicatorOperand(indicator=name, params=params, output=output)


def price_operands() -> st.SearchStrategy[PriceOperand]:
    return st.builds(PriceOperand, price=st.sampled_from(list(PriceField)))


def value_operands() -> st.SearchStrategy[ValueOperand]:
    return st.builds(
        ValueOperand,
        value=st.floats(
            min_value=-VALUE_MAGNITUDE,
            max_value=VALUE_MAGNITUDE,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def cheap_operands() -> st.SearchStrategy[Operand]:
    """Any of the three operand kinds, with a cheap indicator when it is one."""
    return st.one_of(cheap_indicator_operands(), price_operands(), value_operands())


@st.composite
def distinct_operand_pairs(draw: st.DrawFn) -> tuple[Operand, Operand]:
    """Two operands valid as the two sides of one condition (spec 006, AC9)."""
    left = draw(cheap_operands())
    right = draw(cheap_operands())
    both_values = isinstance(left, ValueOperand) and isinstance(right, ValueOperand)
    if both_values or left == right:
        right = _bump(left)
    return left, right


def _bump(left: Operand) -> Operand:
    """A price operand different from ``left``, breaking degeneracy without redrawing."""
    if isinstance(left, PriceOperand) and left.price is PriceField.OPEN:
        return PriceOperand(price=PriceField.CLOSE)
    return PriceOperand(price=PriceField.OPEN)


@st.composite
def cheap_conditions(draw: st.DrawFn) -> Condition:
    left, right = draw(distinct_operand_pairs())
    op = draw(st.sampled_from(list(Operator)))
    return Condition(left=left, op=op, right=right)


@st.composite
def _nested_groups(draw: st.DrawFn) -> NestedAllGroup | NestedAnyGroup:
    conditions = draw(st.lists(cheap_conditions(), min_size=1, max_size=MAX_NESTED_ITEMS))
    if draw(st.booleans()):
        return NestedAllGroup(all=tuple(conditions))
    return NestedAnyGroup(any=tuple(conditions))


@st.composite
def _root_items(draw: st.DrawFn) -> RootItem:
    if draw(st.booleans()):
        return draw(cheap_conditions())
    return draw(_nested_groups())


@st.composite
def cheap_root_groups(draw: st.DrawFn) -> RootGroup:
    items = draw(st.lists(_root_items(), min_size=1, max_size=MAX_ROOT_ITEMS))
    if draw(st.booleans()):
        return AllGroup(all=tuple(items))
    return AnyGroup(any=tuple(items))


@st.composite
def cheap_rules(draw: st.DrawFn) -> Rule:
    """A structurally valid rule whose ``warmup()`` stays small (spec 007, §13)."""
    return Rule(
        name=draw(rule_names()),
        signal=draw(st.sampled_from(list(Side))),
        timeframe=draw(st.sampled_from(list(Timeframe))),
        conditions=draw(cheap_root_groups()),
        cooldown_bars=draw(st.integers(min_value=0, max_value=MAX_COOLDOWN_BARS)),
    )
