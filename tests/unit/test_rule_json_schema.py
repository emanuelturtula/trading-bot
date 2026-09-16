"""Tests of the exported JSON Schema (spec 006, T9, AC18, AC19).

The document is checked against the draft 2020-12 metaschema with ``jsonschema`` (a dev-only
dependency) and, branch by branch, against the indicator catalog it is generated from.
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.rules.json_schema import rule_json_schema

DRAFT = "https://json-schema.org/draft/2020-12/schema"

RSI_BRANCH = {
    "title": "RSI",
    "description": (
        "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined "
        "while the close has not changed since the first candle."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["indicator"],
    "properties": {
        "indicator": {"const": "rsi"},
        "params": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "length": {
                    "title": "Length",
                    "description": "Smoothing period in candles.",
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 100,
                    "default": 14,
                }
            },
        },
        "output": {"enum": ["value"], "default": "value"},
    },
}


@pytest.fixture(scope="module")
def schema() -> dict[str, object]:
    return rule_json_schema()


def branches(schema: dict[str, object]) -> list[dict[str, object]]:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    operand = definitions["IndicatorOperand"]
    assert isinstance(operand, dict)
    branch_list = operand["oneOf"]
    assert isinstance(branch_list, list)
    return branch_list


def branch_of(schema: dict[str, object], name: str) -> dict[str, object]:
    for branch in branches(schema):
        properties = branch["properties"]
        assert isinstance(properties, dict)
        if properties["indicator"] == {"const": name}:
            return branch
    raise AssertionError(f"no branch for {name}")


def test_the_document_is_a_valid_draft_2020_12_schema(schema: dict[str, object]) -> None:
    Draft202012Validator.check_schema(schema)


def test_the_document_starts_with_the_documented_keys(schema: dict[str, object]) -> None:
    assert list(schema)[:3] == ["$schema", "title", "description"]
    assert schema["$schema"] == DRAFT
    assert schema["title"] == "Trading bot rule"
    assert isinstance(schema["description"], str)
    assert "\n" not in schema["description"]


def test_the_document_declares_no_identifier(schema: dict[str, object]) -> None:
    assert "$id" not in schema


def test_the_document_describes_the_rule_object(schema: dict[str, object]) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name", "signal", "timeframe", "conditions"]
    assert set(schema["properties"]) == {  # type: ignore[arg-type]
        "name",
        "signal",
        "timeframe",
        "conditions",
        "cooldown_bars",
    }


def test_the_definitions_hold_every_model_and_enum(schema: dict[str, object]) -> None:
    """The exact set: an extra definition would leak an internal model into what #24 reads."""
    assert set(schema["$defs"]) == {  # type: ignore[arg-type]
        # The twelve models and enums of spec 006, Design 11.1.
        "AllGroup",
        "AnyGroup",
        "NestedAllGroup",
        "NestedAnyGroup",
        "Condition",
        "IndicatorOperand",
        "PriceOperand",
        "ValueOperand",
        "Operator",
        "PriceField",
        "Side",
        "Timeframe",
        # The three tagged unions, which pydantic names after their ``type`` aliases.
        "Operand",
        "RootItem",
        "RootGroup",
    }


def test_there_is_one_branch_per_registry_indicator(schema: dict[str, object]) -> None:
    names = [
        branch["properties"]["indicator"]["const"]  # type: ignore[index,call-overload]
        for branch in branches(schema)
    ]

    assert names == list(REGISTRY.names)


def test_the_rsi_branch_is_the_documented_one(schema: dict[str, object]) -> None:
    branch = branch_of(schema, "rsi")

    assert branch == RSI_BRANCH
    assert list(branch) == [
        "title",
        "description",
        "type",
        "additionalProperties",
        "required",
        "properties",
    ]


def test_a_multi_output_indicator_requires_its_output(schema: dict[str, object]) -> None:
    branch = branch_of(schema, "macd")

    assert branch["required"] == ["indicator", "output"]
    assert branch["properties"]["output"] == {"enum": ["macd", "signal", "hist"]}  # type: ignore[index,call-overload]


def test_a_single_output_indicator_defaults_its_output(schema: dict[str, object]) -> None:
    branch = branch_of(schema, "sma")

    assert branch["required"] == ["indicator"]
    assert branch["properties"]["output"] == {"enum": ["value"], "default": "value"}  # type: ignore[index,call-overload]


def test_a_constraint_is_advisory_metadata(schema: dict[str, object]) -> None:
    assert branch_of(schema, "macd")["x-constraints"] == [
        {"type": "less_than", "left": "fast", "right": "slow"}
    ]
    assert "x-constraints" not in branch_of(schema, "rsi")


def test_a_float_parameter_becomes_a_number(schema: dict[str, object]) -> None:
    params = branch_of(schema, "bbands")["properties"]["params"]  # type: ignore[index,call-overload]

    assert params["properties"]["std"] == {  # type: ignore[index,call-overload]
        "title": "Standard deviations",
        "description": (
            "Distance of the bands from the middle band, in population standard deviations."
        ),
        "type": "number",
        "minimum": 0.1,
        "maximum": 5.0,
        "default": 2.0,
    }
    assert params["properties"]["length"]["type"] == "integer"  # type: ignore[index,call-overload]


@pytest.mark.parametrize("name", REGISTRY.names)
def test_every_branch_describes_every_declared_parameter(
    schema: dict[str, object], name: str
) -> None:
    spec = REGISTRY.get(name)
    params = branch_of(schema, name)["properties"]["params"]  # type: ignore[index,call-overload]

    assert params["additionalProperties"] is False
    assert list(params["properties"]) == [param.name for param in spec.params]
    for param in spec.params:
        described = params["properties"][param.name]
        assert described["minimum"] == param.minimum
        assert described["maximum"] == param.maximum
        assert described["default"] == param.default
        assert described["title"] == param.label


def test_the_export_is_deterministic() -> None:
    first, second = rule_json_schema(), rule_json_schema()

    assert first == second
    assert json.dumps(first, allow_nan=False) == json.dumps(second, allow_nan=False)


def test_two_exports_are_independent_objects() -> None:
    first, second = rule_json_schema(), rule_json_schema()
    first["title"] = "mutated"

    assert second["title"] == "Trading bot rule"


def test_the_document_serializes_without_non_finite_numbers(schema: dict[str, object]) -> None:
    assert json.dumps(schema, allow_nan=False)
