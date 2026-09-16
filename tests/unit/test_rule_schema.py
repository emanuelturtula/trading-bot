"""Tests of the rule schema: canonical form, structure, registry and scalars (spec 006).

T1 (AC1-AC3), T2 (AC4, AC8-AC11), T3 (AC5, AC6), T4 (AC12, AC13) and T5 (AC7, AC10, AC11).
Every payload is a literal built by ``tests.fixtures.rules``; nothing here reads a candle.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from types import MappingProxyType

import pytest

from tests.fixtures.rules import (
    NAME_OF_80_CHARACTERS,
    Payload,
    architecture_example,
    canonical_architecture_example,
    condition,
    default_output,
    default_params,
    indicator_operand,
    invalid_payloads,
    price_operand,
    required_output,
    rule_payload,
    simple_condition,
    valid_payloads,
    value_operand,
)
from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import IndicatorParams
from trading_bot.domain.rules.errors import RuleErrorKind, RuleValidationError
from trading_bot.domain.rules.schema import (
    MAX_CONDITIONS,
    MAX_COOLDOWN_BARS,
    MAX_GROUP_ITEMS,
    MAX_NAME_LENGTH,
    MAX_VALUE_MAGNITUDE,
    AllGroup,
    AnyGroup,
    Condition,
    GroupMode,
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

ARCHITECTURE_DUMP = (
    '{"name": "RSI oversold in uptrend", "signal": "BUY", "timeframe": "1d", "conditions": '
    '{"all": [{"left": {"indicator": "rsi", "params": {"length": 14}, "output": "value"}, '
    '"op": "crosses_below", "right": {"value": 30.0}}, {"left": {"price": "close"}, "op": ">", '
    '"right": {"indicator": "sma", "params": {"length": 200}, "output": "value"}}]}, '
    '"cooldown_bars": 5}'
)


def parsed(conditions: object, **overrides: object) -> Rule:
    return parse_rule(rule_payload(conditions, **overrides))  # type: ignore[arg-type]


def rejection(payload: object) -> RuleValidationError:
    with pytest.raises(RuleValidationError) as caught:
        parse_rule(payload)  # type: ignore[arg-type]
    return caught.value


# --- T1: canonical shape and round trip (AC1-AC3) -------------------------------------------


def test_the_architecture_example_parses() -> None:
    rule = parse_rule(architecture_example())

    assert rule.name == "RSI oversold in uptrend"
    assert rule.signal is Side.BUY
    assert rule.timeframe is Timeframe.D1
    assert rule.cooldown_bars == 5
    assert len(rule.all_conditions) == 2


def test_the_architecture_example_dumps_to_the_documented_json() -> None:
    dumped = dump_rule(parse_rule(architecture_example()))

    assert json.dumps(dumped, allow_nan=False) == ARCHITECTURE_DUMP


def test_the_canonical_dump_matches_the_fixture() -> None:
    assert dump_rule(parse_rule(architecture_example())) == canonical_architecture_example()


def test_the_canonical_dump_fills_every_default() -> None:
    dumped = dump_rule(
        parsed(
            {"all": [condition(indicator_operand("macd", output="hist"), ">", value_operand(0))]}
        )
    )

    operand = dumped["conditions"]["all"][0]["left"]  # type: ignore[index,call-overload]
    assert operand == {
        "indicator": "macd",
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "output": "hist",
    }
    assert list(operand) == ["indicator", "params", "output"]
    assert dumped["cooldown_bars"] == 0


def test_the_canonical_dump_orders_the_keys_of_the_document() -> None:
    dumped = dump_rule(parse_rule(architecture_example()))

    assert list(dumped) == ["name", "signal", "timeframe", "conditions", "cooldown_bars"]
    first = dumped["conditions"]["all"][0]  # type: ignore[index,call-overload]
    assert list(first) == ["left", "op", "right"]


@pytest.mark.parametrize(
    ("label", "payload"),
    valid_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_valid_payload_round_trips(label: str, payload: Payload) -> None:
    rule = parse_rule(payload)
    dumped = dump_rule(rule)

    assert parse_rule(dumped) == rule
    assert dump_rule(parse_rule(dumped)) == dumped
    assert json.dumps(dumped, allow_nan=False)


@pytest.mark.parametrize(
    ("label", "payload"),
    valid_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pydantic_json_serialization_agrees_with_the_canonical_dump(
    label: str, payload: Payload
) -> None:
    """``model_dump_json`` must not explode on the parameters of an indicator operand.

    #12 stores a rule, #22 encodes it in a response and #24 renders it; all three reach pydantic's
    JSON serialization, so it has to produce the same document as ``dump_rule``.
    """
    rule = parse_rule(payload)

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a pydantic serializer warning must fail the test
        as_json = rule.model_dump(mode="json")
        as_text = rule.model_dump_json()

    assert as_json == dump_rule(rule)
    assert json.loads(as_text) == dump_rule(rule)


def test_python_mode_keeps_the_parameters_typed() -> None:
    """Only the JSON form is flattened: #7 keys its cache with the ``IndicatorParams`` object."""
    rule = parse_rule(architecture_example())

    operand = rule.model_dump()["conditions"]["all"][0]["left"]

    assert isinstance(operand["params"], IndicatorParams)


def test_equivalent_payloads_give_equal_rules() -> None:
    written_out = parse_rule(canonical_architecture_example())
    abbreviated = parse_rule(architecture_example())

    assert written_out == abbreviated
    assert hash(written_out) == hash(abbreviated)


def test_key_order_does_not_change_the_rule() -> None:
    payload = architecture_example()
    reordered = dict(reversed(list(payload.items())))

    assert parse_rule(reordered) == parse_rule(payload)


def test_an_integer_value_becomes_a_float() -> None:
    rule = parsed({"all": [condition(price_operand(), "<", value_operand(30))]})
    operand = rule.all_conditions[0].right

    assert isinstance(operand, ValueOperand)
    assert isinstance(operand.value, float)
    assert operand == ValueOperand(value=30.0)


def test_a_rule_is_frozen() -> None:
    rule = parse_rule(architecture_example())

    with pytest.raises(ValueError, match="frozen"):
        rule.name = "other"  # type: ignore[misc]


def test_an_operand_is_frozen() -> None:
    operand = IndicatorOperand(indicator="rsi")

    with pytest.raises(ValueError, match="frozen"):
        operand.indicator = "sma"  # type: ignore[misc]


def test_text_bytes_and_mapping_inputs_agree() -> None:
    payload = architecture_example()
    text = json.dumps(payload)

    from_mapping = parse_rule(payload)
    from_text = parse_rule(text)
    from_bytes = parse_rule(text.encode())
    from_proxy = parse_rule(MappingProxyType(payload))

    assert from_mapping == from_text == from_bytes == from_proxy


def test_a_rule_can_be_built_in_process_from_trusted_values() -> None:
    rule = Rule(
        name="Built in process",
        signal=Side.SELL,
        timeframe=Timeframe.H1,
        conditions=AllGroup(
            all=(
                Condition(
                    left=IndicatorOperand(indicator="rsi"),
                    op=Operator.CROSSES_ABOVE,
                    right=ValueOperand(value=70),
                ),
            )
        ),
    )

    assert parse_rule(dump_rule(rule)) == rule
    assert rule.cooldown_bars == 0


# --- T2: structural rejections (AC4, AC8-AC11) ----------------------------------------------


@pytest.mark.parametrize(
    ("label", "payload", "kind", "path"),
    invalid_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_invalid_payload_is_rejected(
    label: str, payload: Payload, kind: RuleErrorKind, path: str
) -> None:
    error = rejection(payload)

    assert (error.kind, error.path) == (kind, path)


def test_a_group_holds_at_most_the_documented_number_of_members() -> None:
    accepted = parsed({"all": [simple_condition() for _ in range(MAX_GROUP_ITEMS)]})

    assert len(accepted.all_conditions) == MAX_GROUP_ITEMS
    assert (
        rejection(
            rule_payload({"all": [simple_condition() for _ in range(MAX_GROUP_ITEMS + 1)]})
        ).kind
        is RuleErrorKind.OUT_OF_RANGE
    )


def test_a_rule_holds_at_most_the_documented_number_of_conditions() -> None:
    members = [{"all": [simple_condition() for _ in range(10)]} for _ in range(2)]
    accepted = parsed({"all": members})

    assert len(accepted.all_conditions) == MAX_CONDITIONS


def test_a_nested_group_keeps_the_document_order_of_its_conditions() -> None:
    rule = parsed(
        {
            "any": [
                condition(price_operand("open"), "<", value_operand(1)),
                {
                    "all": [
                        condition(price_operand("high"), "<", value_operand(2)),
                        condition(price_operand("low"), "<", value_operand(3)),
                    ]
                },
                condition(price_operand("close"), "<", value_operand(4)),
            ]
        }
    )

    values = [item.right for item in rule.all_conditions]
    assert values == [ValueOperand(value=number) for number in (1.0, 2.0, 3.0, 4.0)]


def test_a_group_exposes_its_mode_and_members() -> None:
    rule = parsed({"any": [simple_condition(), {"all": [simple_condition()]}]})
    group = rule.conditions

    assert isinstance(group, AnyGroup)
    assert group.mode is GroupMode.ANY
    assert len(group.members) == 2
    nested = group.members[1]
    assert not isinstance(nested, Condition)
    assert nested.mode is GroupMode.ALL
    assert len(nested.members) == 1


def test_an_all_group_exposes_its_mode() -> None:
    group = parsed({"all": [simple_condition()]}).conditions

    assert isinstance(group, AllGroup)
    assert group.mode is GroupMode.ALL
    assert group.members == group.all


@pytest.mark.parametrize("document", ["[]", '"text"', "5", "null", "true"])
def test_a_document_that_is_not_an_object_is_rejected(document: str) -> None:
    assert rejection(document).kind is RuleErrorKind.NOT_AN_OBJECT


def test_a_payload_that_is_not_text_bytes_or_a_mapping_is_rejected() -> None:
    assert rejection(5).kind is RuleErrorKind.WRONG_TYPE


def test_unknown_keys_are_rejected_inside_an_operand() -> None:
    payload = rule_payload(
        {"all": [{"left": {"indicator": "rsi", "nope": 1}, "op": "<", "right": value_operand(1)}]}
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    assert error.path == "conditions.all[0].left.nope"


def test_unknown_keys_are_rejected_inside_a_group() -> None:
    payload = rule_payload({"all": [simple_condition()], "$schema": "x"})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    assert error.path == "conditions.'$schema'"


def test_the_operators_are_exactly_the_documented_ones() -> None:
    assert [member.value for member in Operator] == [
        "<",
        "<=",
        ">",
        ">=",
        "crosses_above",
        "crosses_below",
    ]


@pytest.mark.parametrize("operator", list(Operator))
def test_only_the_crossovers_are_crossovers(operator: Operator) -> None:
    assert operator.is_crossover == operator.value.startswith("crosses_")


def test_the_price_fields_are_the_candle_columns() -> None:
    assert tuple(member.value for member in PriceField) == OHLCV_COLUMNS


# --- T3: registry cross-validation (AC5, AC6) -----------------------------------------------


@pytest.mark.parametrize("name", REGISTRY.names)
def test_every_registry_indicator_is_a_valid_operand(name: str) -> None:
    rule = parsed(
        {
            "all": [
                condition(
                    indicator_operand(name, output=required_output(name)), ">", value_operand(1)
                )
            ]
        }
    )
    operand = rule.all_conditions[0].left

    assert isinstance(operand, IndicatorOperand)
    assert operand.indicator == name
    assert dict(operand.params) == default_params(name)
    assert operand.output == default_output(name)


@pytest.mark.parametrize("name", REGISTRY.names)
def test_every_indicator_accepts_its_declared_outputs(name: str) -> None:
    for output in REGISTRY.get(name).output_names:
        rule = parsed(
            {"all": [condition(indicator_operand(name, output=output), ">", value_operand(1))]}
        )
        operand = rule.all_conditions[0].left
        assert isinstance(operand, IndicatorOperand)
        assert operand.output == output


def test_an_indicator_name_is_case_sensitive() -> None:
    payload = rule_payload({"all": [condition(indicator_operand("RSI"), "<", value_operand(1))]})

    assert rejection(payload).kind is RuleErrorKind.UNKNOWN_INDICATOR


def test_an_unknown_indicator_reports_a_single_problem() -> None:
    payload = rule_payload(
        {
            "all": [
                condition(indicator_operand("nope", {"length": 5}, "value"), "<", value_operand(1))
            ]
        }
    )

    error = rejection(payload)

    assert [problem.kind for problem in error.problems] == [RuleErrorKind.UNKNOWN_INDICATOR]


def test_the_registry_message_is_kept() -> None:
    payload = rule_payload({"all": [condition(indicator_operand("nope"), "<", value_operand(1))]})

    assert "unknown indicator 'nope'" in rejection(payload).problems[0].message


def test_parameters_are_stored_with_their_defaults_filled() -> None:
    rule = parsed(
        {"all": [condition(indicator_operand("macd", {"fast": 8}, "macd"), ">", value_operand(0))]}
    )
    operand = rule.all_conditions[0].left

    assert isinstance(operand, IndicatorOperand)
    assert list(operand.params.items()) == [("fast", 8), ("slow", 26), ("signal", 9)]


# --- T4: semantic checks (AC12, AC13) -------------------------------------------------------


def test_a_crossover_against_a_constant_is_valid() -> None:
    rule = parsed(
        {"all": [condition(indicator_operand("rsi"), "crosses_below", value_operand(30))]}
    )

    assert rule.all_conditions[0].op is Operator.CROSSES_BELOW


def test_comparing_different_units_is_valid() -> None:
    rule = parsed({"all": [condition(price_operand(), ">", indicator_operand("rsi"))]})

    assert isinstance(rule.all_conditions[0].right, IndicatorOperand)


def test_duplicated_conditions_are_valid() -> None:
    rule = parsed({"all": [simple_condition(), simple_condition()]})

    assert len(rule.all_conditions) == 2


def test_contradictory_conditions_are_valid() -> None:
    rule = parsed(
        {
            "all": [
                condition(price_operand(), ">", value_operand(100)),
                condition(price_operand(), "<", value_operand(10)),
            ]
        }
    )

    assert len(rule.all_conditions) == 2


def test_identical_price_operands_are_rejected() -> None:
    payload = rule_payload({"all": [condition(price_operand(), "<", price_operand())]})

    assert rejection(payload).kind is RuleErrorKind.DEGENERATE_CONDITION


def test_different_indicator_parameters_are_not_identical() -> None:
    rule = parsed(
        {
            "all": [
                condition(
                    indicator_operand("sma", {"length": 50}),
                    ">",
                    indicator_operand("sma", {"length": 200}),
                )
            ]
        }
    )

    assert len(rule.all_conditions) == 1


# --- T5: scalars (AC7, AC10, AC11) ----------------------------------------------------------


def test_a_name_is_stored_stripped() -> None:
    assert parsed({"all": [simple_condition()]}, name="  Trimmed name \t ").name == "Trimmed name"


def test_a_name_of_one_character_is_valid() -> None:
    assert parsed({"all": [simple_condition()]}, name="A").name == "A"


def test_a_name_of_the_maximum_length_is_valid() -> None:
    assert len(NAME_OF_80_CHARACTERS) == MAX_NAME_LENGTH
    assert parsed({"all": [simple_condition()]}, name=NAME_OF_80_CHARACTERS).name


def test_a_name_is_measured_after_stripping() -> None:
    padded = f"  {NAME_OF_80_CHARACTERS}  "

    assert parsed({"all": [simple_condition()]}, name=padded).name == NAME_OF_80_CHARACTERS


@pytest.mark.parametrize(
    "name",
    ["", "   ", "n" * (MAX_NAME_LENGTH + 1), "tab\there", "zero\u200bwidth", "rtl\u202eflip"],
)
def test_an_invalid_name_is_rejected(name: str) -> None:
    error = rejection(rule_payload({"all": [simple_condition()]}, name=name))

    assert (error.kind, error.path) == (RuleErrorKind.INVALID_NAME, "name")


def test_a_name_never_appears_in_the_error_message() -> None:
    error = rejection(rule_payload({"all": [simple_condition()]}, name="secret\nname"))

    assert "secret" not in error.problems[0].message


@pytest.mark.parametrize("code", ["1h", "4h", "1d"])
def test_every_timeframe_code_is_accepted(code: str) -> None:
    assert parsed({"all": [simple_condition()]}, timeframe=code).timeframe == Timeframe(code)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_every_side_is_accepted(side: str) -> None:
    assert parsed({"all": [simple_condition()]}, signal=side).signal == Side(side)


def test_the_cooldown_defaults_to_zero() -> None:
    assert parsed({"all": [simple_condition()]}).cooldown_bars == 0


@pytest.mark.parametrize("bars", [0, 1, MAX_COOLDOWN_BARS])
def test_a_cooldown_inside_the_range_is_accepted(bars: int) -> None:
    assert parsed({"all": [simple_condition()]}, cooldown_bars=bars).cooldown_bars == bars


def test_a_huge_cooldown_is_rejected() -> None:
    error = rejection(rule_payload({"all": [simple_condition()]}, cooldown_bars=10**100))

    assert error.kind is RuleErrorKind.OUT_OF_RANGE


@pytest.mark.parametrize("number", [0, -0.0, 5e-324, 30, 30.5, -1e15, 1e15])
def test_a_finite_value_inside_the_range_is_accepted(number: float) -> None:
    rule = parsed({"all": [condition(price_operand(), "<", value_operand(number))]})
    operand = rule.all_conditions[0].right

    assert isinstance(operand, ValueOperand)
    assert operand.value == float(number)


@pytest.mark.parametrize("number", [1e15 + 1e2, -1e16, float("nan"), float("inf"), float("-inf")])
def test_a_value_outside_the_range_is_rejected(number: float) -> None:
    error = rejection(
        rule_payload({"all": [condition(price_operand(), "<", value_operand(number))]})
    )

    assert error.kind is RuleErrorKind.OUT_OF_RANGE


def test_the_documented_bounds_are_the_implemented_ones() -> None:
    assert (MAX_GROUP_ITEMS, MAX_CONDITIONS, MAX_NAME_LENGTH, MAX_COOLDOWN_BARS) == (
        10,
        20,
        80,
        500,
    )
    assert MAX_VALUE_MAGNITUDE == 1e15


def test_an_operand_of_each_kind_keeps_its_own_type() -> None:
    rule = parsed(
        {
            "all": [
                condition(price_operand("volume"), ">", indicator_operand("volume_sma")),
                condition(indicator_operand("atr"), ">", value_operand(0.5)),
            ]
        }
    )

    left, right = rule.all_conditions[0].left, rule.all_conditions[0].right
    assert isinstance(left, PriceOperand)
    assert left.price is PriceField.VOLUME
    assert isinstance(right, IndicatorOperand)
    assert isinstance(rule.all_conditions[1].right, ValueOperand)


def test_a_rule_built_from_model_instances_is_accepted() -> None:
    """#7 and #12 build rules in process; the discriminators must accept model instances."""
    price_against_value = Condition(
        left=PriceOperand(price=PriceField.CLOSE), op=Operator.GREATER, right=ValueOperand(value=1)
    )
    indicator_against_price = Condition(
        left=IndicatorOperand(indicator="ema"),
        op=Operator.CROSSES_ABOVE,
        right=PriceOperand(price=PriceField.OPEN),
    )
    rule = Rule(
        name="Built from instances",
        signal=Side.BUY,
        timeframe=Timeframe.H4,
        conditions=AnyGroup(
            any=(
                price_against_value,
                NestedAllGroup(all=(indicator_against_price,)),
                NestedAnyGroup(any=(price_against_value,)),
            )
        ),
        cooldown_bars=3,
    )

    assert parse_rule(dump_rule(rule)) == rule
    assert len(rule.all_conditions) == 3


def test_an_all_group_instance_is_accepted() -> None:
    conditions = AllGroup(
        all=(
            Condition(
                left=PriceOperand(price=PriceField.HIGH),
                op=Operator.LESS,
                right=ValueOperand(value=2),
            ),
        )
    )

    rule = Rule(
        name="All group instance", signal=Side.SELL, timeframe=Timeframe.D1, conditions=conditions
    )

    assert rule.conditions.mode is GroupMode.ALL


def test_the_documented_example_is_the_fixture_one() -> None:
    """AC1 pins the JSON of ``docs/ARCHITECTURE.md``: the fixture and the doc cannot drift."""
    document = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"
    text = document.read_text(encoding="utf-8")
    section = text[text.index("## Rule model") : text.index("## Look-ahead testing")]
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert block is not None

    documented = json.loads(block.group(1))

    assert documented == architecture_example()
    assert dump_rule(parse_rule(documented)) == canonical_architecture_example()
