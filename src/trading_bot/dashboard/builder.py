"""Rule templates of the dashboard's rule builder (spec 026).

A template is data: its fields and a ``conditions`` pattern whose ``{"$": "<field>"}`` placeholders
take the field values. ``builder.js`` fills the same patterns in the browser; this module is the
reference used for the page's initial state and by the tests. Every filled pattern is an ordinary
rule document (spec 006), validated by ``parse_rule`` like a hand-written one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.rules.schema import PriceField
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "TEMPLATES",
    "Field",
    "RuleTemplate",
    "builder_payload",
    "fill",
    "match",
]

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Field:
    """One input of a template: a number in a range, or a choice among JSON values."""

    name: str
    label: str
    kind: str  # "int", "float" or "choice"
    default: Json
    minimum: float = 0.0  # numbers only
    maximum: float = 0.0
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)

    def accepts(self, value: object) -> bool:
        if self.kind == "choice":
            return isinstance(value, str) and value in {choice for choice, _ in self.choices}
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        if self.kind == "int" and value != int(value):
            return False
        return self.minimum <= value <= self.maximum

    def describe(self) -> dict[str, Json]:
        described: dict[str, Json] = {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
        }
        if self.kind == "choice":
            described["choices"] = [[value, label] for value, label in self.choices]
        else:
            described["min"] = self.minimum
            described["max"] = self.maximum
        return described


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleTemplate:
    """A kind of rule the builder offers."""

    id: str
    label: str
    description: str
    fields: tuple[Field, ...]
    conditions: Json

    def describe(self) -> dict[str, Json]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "fields": [field.describe() for field in self.fields],
            "conditions": self.conditions,
        }


def _param(indicator: str, param: str, *, name: str | None = None, default: Json = None) -> Field:
    """A field with the label, range and default of an indicator parameter."""
    spec = next(item for item in REGISTRY.get(indicator).params if item.name == param)
    return Field(
        name=name or param,
        label=spec.label,
        kind=spec.kind.value,
        default=spec.default if default is None else default,
        minimum=float(spec.minimum),
        maximum=float(spec.maximum),
    )


def _level(default: float) -> Field:
    return Field(name="level", label="Level", kind="float", default=default, minimum=0, maximum=100)


def _choice(name: str, label: str, *choices: tuple[str, str]) -> Field:
    return Field(name=name, label=label, kind="choice", default=choices[0][0], choices=choices)


def _slot(name: str) -> dict[str, Json]:
    return {"$": name}


_COMPARISON: Final = (
    ("crosses_below", "crosses below"),
    ("crosses_above", "crosses above"),
    ("<", "is below"),
    (">", "is above"),
)
_CROSSING: Final = (("crosses_above", "crosses above"), ("crosses_below", "crosses below"))
_AVERAGE: Final = (("sma", "Simple (SMA)"), ("ema", "Exponential (EMA)"))
_MACD_PARAMS: Final[dict[str, Json]] = {
    "fast": _slot("fast"),
    "slow": _slot("slow"),
    "signal": _slot("signal"),
}
_STOCH_PARAMS: Final[dict[str, Json]] = {
    "length": _slot("length"),
    "smooth_k": _slot("smooth_k"),
    "smooth_d": _slot("smooth_d"),
}


def _single(left: Json, op: Json, right: Json) -> Json:
    return {"all": [{"left": left, "op": op, "right": right}]}


def _average(length: str) -> Json:
    return {"indicator": _slot("average"), "params": {"length": _slot(length)}, "output": "value"}


TEMPLATES: Final[tuple[RuleTemplate, ...]] = (
    RuleTemplate(
        id="rsi_level",
        label="RSI below or above a level",
        description="Fires when the RSI crosses or stays beyond a level, e.g. crosses below 30.",
        fields=(_param("rsi", "length"), _choice("op", "RSI", *_COMPARISON), _level(30)),
        conditions=_single(
            {"indicator": "rsi", "params": {"length": _slot("length")}, "output": "value"},
            _slot("op"),
            {"value": _slot("level")},
        ),
    ),
    RuleTemplate(
        id="average_cross",
        label="Moving averages cross",
        description="Fires when a fast moving average crosses a slow one (golden or death cross).",
        fields=(
            _choice("average", "Average", *_AVERAGE),
            _param("sma", "length", name="fast", default=50),
            _choice("op", "Fast average", *_CROSSING),
            _param("sma", "length", name="slow", default=200),
        ),
        conditions=_single(_average("fast"), _slot("op"), _average("slow")),
    ),
    RuleTemplate(
        id="macd_cross",
        label="MACD crosses its signal line",
        description="Fires when the MACD line crosses its signal line.",
        fields=(
            _param("macd", "fast"),
            _param("macd", "slow"),
            _param("macd", "signal"),
            _choice("op", "MACD line", *_CROSSING),
        ),
        conditions=_single(
            {"indicator": "macd", "params": _MACD_PARAMS, "output": "macd"},
            _slot("op"),
            {"indicator": "macd", "params": _MACD_PARAMS, "output": "signal"},
        ),
    ),
    RuleTemplate(
        id="price_average",
        label="Price crosses a moving average",
        description="Fires when the close crosses a moving average, e.g. the 200-day SMA.",
        fields=(
            _choice("average", "Average", *_AVERAGE),
            _param("sma", "length", default=200),
            _choice("op", "Close", *_CROSSING),
        ),
        conditions=_single({"price": "close"}, _slot("op"), _average("length")),
    ),
    RuleTemplate(
        id="bollinger",
        label="Price leaves the Bollinger bands",
        description="Fires when the close crosses or stays beyond a Bollinger band.",
        fields=(
            _param("bbands", "length"),
            _param("bbands", "std"),
            _choice("op", "Close", *_COMPARISON),
            _choice("band", "Band", ("lower", "lower band"), ("upper", "upper band")),
        ),
        conditions=_single(
            {"price": "close"},
            _slot("op"),
            {
                "indicator": "bbands",
                "params": {"length": _slot("length"), "std": _slot("std")},
                "output": _slot("band"),
            },
        ),
    ),
    RuleTemplate(
        id="stoch_level",
        label="Stochastic below or above a level",
        description="Fires when the stochastic %K or %D crosses or stays beyond a level.",
        fields=(
            _param("stoch", "length"),
            _param("stoch", "smooth_k"),
            _param("stoch", "smooth_d"),
            _choice("line", "Line", ("k", "%K"), ("d", "%D")),
            _choice("op", "The line", *_COMPARISON),
            _level(20),
        ),
        conditions=_single(
            {"indicator": "stoch", "params": _STOCH_PARAMS, "output": _slot("line")},
            _slot("op"),
            {"value": _slot("level")},
        ),
    ),
)

_BY_ID: Final = {template.id: template for template in TEMPLATES}


def fill(template_id: str, values: Mapping[str, object]) -> Json:
    """The ``conditions`` of ``template_id`` with ``values``; missing values take the defaults.

    Raises ``KeyError`` for an unknown template and ``ValueError`` for a value out of range.
    """
    template = _BY_ID[template_id]
    resolved: dict[str, Json] = {}
    for field in template.fields:
        value = values.get(field.name, field.default)
        if not field.accepts(value):
            raise ValueError(f"{field.name}: {value!r} is not accepted")
        if field.kind == "int" and isinstance(value, int | float):
            value = int(value)
        resolved[field.name] = value  # type: ignore[assignment]
    return _substitute(template.conditions, resolved)


def match(conditions: object) -> tuple[str, dict[str, Json]] | None:
    """The first template whose pattern ``conditions`` (as ``dump_rule`` writes them) fits."""
    for template in TEMPLATES:
        bound: dict[str, Json] = {}
        if _unify(template.conditions, conditions, bound) and all(
            field.accepts(bound.get(field.name)) for field in template.fields
        ):
            return template.id, bound
    return None


def builder_payload() -> dict[str, object]:
    """What ``builder.js`` needs: the templates and the vocabulary of the rule grammar."""
    return {
        "templates": [template.describe() for template in TEMPLATES],
        "catalog": REGISTRY.describe(),
        "prices": [price.value for price in PriceField],
        "timeframes": [timeframe.value for timeframe in Timeframe],
    }


def _substitute(pattern: Json, values: Mapping[str, Json]) -> Json:
    if isinstance(pattern, dict):
        if set(pattern) == {"$"}:
            return values[str(pattern["$"])]
        return {key: _substitute(item, values) for key, item in pattern.items()}
    if isinstance(pattern, list):
        return [_substitute(item, values) for item in pattern]
    return pattern


def _unify(pattern: Json, value: object, bound: dict[str, Json]) -> bool:
    if isinstance(pattern, dict) and set(pattern) == {"$"}:
        name = str(pattern["$"])
        if name in bound:
            return _same(bound[name], value)
        bound[name] = value  # type: ignore[assignment]
        return True
    if isinstance(pattern, dict):
        return (
            isinstance(value, Mapping)
            and set(value) == set(pattern)
            and all(_unify(item, value[key], bound) for key, item in pattern.items())
        )
    if isinstance(pattern, list):
        return (
            isinstance(value, Sequence)
            and not isinstance(value, str)
            and len(value) == len(pattern)
            and all(_unify(item, other, bound) for item, other in zip(pattern, value, strict=True))
        )
    return _same(pattern, value)


def _same(left: object, right: object) -> bool:
    numbers = (int, float)
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, numbers) and isinstance(right, numbers):
        return left == right
    return type(left) is type(right) and left == right
