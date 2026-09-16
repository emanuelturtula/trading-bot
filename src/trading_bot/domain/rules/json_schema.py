"""The JSON Schema of a rule document, generated from the models and the catalog (spec 006, §11).

``rule_json_schema()`` returns a draft 2020-12 document that the dashboard rule builder (#24) can
use to render forms and pre-validate what the server will accept, and that the API (#22) can
serve. The indicator operand is generated from ``REGISTRY.describe()``, so the whitelist, the
parameter ranges, the defaults and the labels can never drift from the catalog.

The server stays authoritative: JSON Schema cannot express the ``less_than`` constraints between
parameters, integral-number checks, the degenerate-condition rules, the rule name rules or the
total condition count (spec 006, §11.4). Nothing is cached here (that would be mutable module
state) and nothing is read from disk: generating the document costs about 1.3 ms.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, TypedDict, cast

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.schema import Rule

__all__ = ["rule_json_schema"]

_DRAFT: Final = "https://json-schema.org/draft/2020-12/schema"
_TITLE: Final = "Trading bot rule"
_DESCRIPTION: Final = (
    "A technical-analysis rule: when its conditions hold on a closed candle, the bot notifies a "
    "signal. It never places an order. The server is authoritative: it also checks the "
    "parameter constraints, the total number of conditions and the rule name."
)
_JSON_TYPE_BY_PARAM_KIND: Final = MappingProxyType({"int": "integer", "float": "number"})


class _DescribedParam(TypedDict):
    """One entry of ``describe()[i]["params"]`` (spec 005, ``IndicatorRegistry.describe``)."""

    name: str
    label: str
    description: str
    type: str
    default: int | float
    min: int | float
    max: int | float


class _DescribedIndicator(TypedDict):
    """One entry of ``REGISTRY.describe()``."""

    name: str
    label: str
    description: str
    inputs: list[str]
    params: list[_DescribedParam]
    constraints: list[dict[str, str]]
    outputs: list[dict[str, str]]
    default_output: str | None
    default_warmup: int
    default_stable_warmup: int


def rule_json_schema() -> dict[str, JsonValue]:
    """The JSON Schema of a rule document. A new, equal object on every call."""
    generated = cast("dict[str, JsonValue]", Rule.model_json_schema())
    definitions = cast("dict[str, JsonValue]", generated["$defs"])
    branches: list[JsonValue] = [_branch(entry) for entry in _described()]
    definitions["IndicatorOperand"] = {"oneOf": branches}
    document: dict[str, JsonValue] = {
        "$schema": _DRAFT,
        "title": _TITLE,
        "description": _DESCRIPTION,
    }
    document.update(generated)
    # The merge brought pydantic's title and the model docstring; ours are the documented ones.
    document["title"] = _TITLE
    document["description"] = _DESCRIPTION
    return document


def _described() -> list[_DescribedIndicator]:
    """The catalog metadata, typed. ``describe()`` builds this shape (spec 005, Design 7)."""
    return [cast("_DescribedIndicator", entry) for entry in REGISTRY.describe()]


def _branch(indicator: _DescribedIndicator) -> dict[str, JsonValue]:
    outputs = [output["name"] for output in indicator["outputs"]]
    default_output = indicator["default_output"]
    output_schema: dict[str, JsonValue] = {"enum": list(outputs)}
    required: list[JsonValue] = ["indicator"]
    if default_output is None:
        required.append("output")
    else:
        output_schema["default"] = default_output
    properties: dict[str, JsonValue] = {
        "indicator": {"const": indicator["name"]},
        "params": _params_schema(indicator["params"]),
        "output": output_schema,
    }
    branch: dict[str, JsonValue] = {
        "title": indicator["label"],
        "description": indicator["description"],
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }
    if indicator["constraints"]:
        # Advisory metadata: draft 2020-12 cannot express "fast < slow", and unknown keywords are
        # ignored by validators. The constraint is enforced by the server (spec 006, §11.4).
        branch["x-constraints"] = [dict(constraint) for constraint in indicator["constraints"]]
    return branch


def _params_schema(params: list[_DescribedParam]) -> dict[str, JsonValue]:
    properties: dict[str, JsonValue] = {param["name"]: _param_schema(param) for param in params}
    return {"type": "object", "additionalProperties": False, "properties": properties}


def _param_schema(param: _DescribedParam) -> dict[str, JsonValue]:
    return {
        "title": param["label"],
        "description": param["description"],
        "type": _JSON_TYPE_BY_PARAM_KIND[param["type"]],
        "minimum": param["min"],
        "maximum": param["max"],
        "default": param["default"],
    }
