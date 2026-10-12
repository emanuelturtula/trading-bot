"""Rule templates of the dashboard's rule builder (spec 026)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from trading_bot.dashboard.builder import TEMPLATES, builder_payload, fill, match
from trading_bot.domain.rules.errors import RuleValidationError
from trading_bot.domain.rules.schema import dump_rule, parse_rule

IDS = [template.id for template in TEMPLATES]


def document(conditions: object) -> dict[str, object]:
    return {"name": "Rule", "signal": "BUY", "timeframe": "1d", "conditions": conditions}


def stored_conditions(conditions: object) -> Any:
    """The conditions as the dashboard reads them back from storage."""
    return dump_rule(parse_rule(document(conditions)))["conditions"]


def edge_values(template_id: str, *, high: bool) -> dict[str, object]:
    """Every field at one end of its range (the first or last choice)."""
    template = next(item for item in TEMPLATES if item.id == template_id)
    values: dict[str, object] = {}
    for field in template.fields:
        if field.kind == "choice":
            values[field.name] = field.choices[-1 if high else 0][0]
        else:
            bound = field.maximum if high else field.minimum
            values[field.name] = int(bound) if field.kind == "int" else bound
    return values


@pytest.mark.parametrize("high", [False, True])
@pytest.mark.parametrize("template_id", IDS)
def test_every_template_fills_a_valid_rule_and_is_recognized(template_id: str, high: bool) -> None:
    values = edge_values(template_id, high=high)
    if template_id == "average_cross":
        values["slow"] = edge_values(template_id, high=not high)["slow"]

    conditions = fill(template_id, values)
    matched = match(stored_conditions(conditions))

    assert matched is not None
    assert matched[0] == template_id
    assert fill(template_id, matched[1]) == conditions


@pytest.mark.parametrize("template_id", IDS)
def test_defaults_round_trip_through_storage(template_id: str) -> None:
    stored = stored_conditions(fill(template_id, {}))

    matched = match(stored)

    assert matched is not None
    assert matched[0] == template_id
    assert stored_conditions(fill(template_id, matched[1])) == stored


def test_the_server_still_validates_what_a_template_cannot() -> None:
    same_lengths = fill("average_cross", {"fast": 50, "slow": 50})

    with pytest.raises(RuleValidationError, match="same operand"):
        parse_rule(document(same_lengths))


def test_values_outside_a_field_are_refused() -> None:
    with pytest.raises(ValueError, match="level"):
        fill("rsi_level", {"level": 101})
    with pytest.raises(ValueError, match="length"):
        fill("rsi_level", {"length": 14.5})
    with pytest.raises(ValueError, match="op"):
        fill("rsi_level", {"op": "=="})
    with pytest.raises(ValueError, match="length"):
        fill("rsi_level", {"length": True})
    with pytest.raises(KeyError):
        fill("nope", {})


def test_other_rules_match_no_template() -> None:
    nested = stored_conditions(
        {
            "all": [
                {"left": {"indicator": "rsi"}, "op": "<", "right": {"value": 30}},
                {"any": [{"left": {"price": "close"}, "op": ">", "right": {"indicator": "sma"}}]},
            ]
        }
    )
    mixed: Any = stored_conditions(fill("average_cross", {}))
    mixed["all"][0]["right"]["indicator"] = "ema"  # the two averages differ in kind
    beyond: Any = stored_conditions(fill("rsi_level", {}))
    beyond["all"][0]["right"]["value"] = 150.0  # outside the level field

    assert match(nested) is None
    assert match(mixed) is None
    assert match(beyond) is None
    assert match({"any": nested["all"]}) is None
    assert match("all") is None


def test_payload_is_json_with_the_grammar() -> None:
    payload = json.loads(json.dumps(builder_payload()))

    assert [template["id"] for template in payload["templates"]] == IDS
    assert {"rsi", "macd", "sma"} <= {indicator["name"] for indicator in payload["catalog"]}
    assert payload["prices"] == ["open", "high", "low", "close", "volume"]
    assert payload["timeframes"] == ["1h", "4h", "1d", "1w"]
    rsi = payload["templates"][0]["fields"]
    assert rsi[0] == {
        "name": "length",
        "label": "Length",
        "kind": "int",
        "default": 14,
        "min": 2.0,
        "max": 100.0,
    }
    assert rsi[1]["choices"][0] == ["crosses_below", "crosses below"]
