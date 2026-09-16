"""Shared rule payloads for the rule schema tests (spec 006, Design 13).

Every builder returns a fresh object on each call, so a test may mutate what it receives.
There is no network, no randomness and no clock here: the payloads are literals, the indicator
names and parameters come from the registry so they cannot drift from the catalog.

Always import this module as ``tests.fixtures.rules``.
"""

from __future__ import annotations

from typing import Final

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.errors import RuleErrorKind

type Payload = dict[str, JsonValue]

OPERATORS: Final[tuple[str, ...]] = ("<", "<=", ">", ">=", "crosses_above", "crosses_below")
PRICE_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
NAME_WITH_ACCENTS: Final = "Naïve crossover with a café latte"
NAME_OF_80_CHARACTERS: Final = (
    "Rule name that is exactly eighty characters long for the boundary check".ljust(80, ".")
)


def indicator_operand(
    name: str, params: Payload | None = None, output: str | None = None
) -> Payload:
    """An indicator operand; ``params`` and ``output`` are omitted when they are ``None``."""
    operand: Payload = {"indicator": name}
    if params is not None:
        operand["params"] = params
    if output is not None:
        operand["output"] = output
    return operand


def price_operand(column: str = "close") -> Payload:
    return {"price": column}


def value_operand(number: float) -> Payload:
    return {"value": number}


def condition(left: Payload, operator: str, right: Payload) -> Payload:
    return {"left": left, "op": operator, "right": right}


def simple_condition() -> Payload:
    """A valid condition used wherever the condition itself is not what a test checks."""
    return condition(price_operand(), ">", indicator_operand("sma"))


def rule_payload(conditions: JsonValue, **overrides: JsonValue) -> Payload:
    """A valid rule document whose fields can be overridden (``cooldown_bars`` is omitted)."""
    payload: Payload = {
        "name": "Example rule",
        "signal": "BUY",
        "timeframe": "1d",
        "conditions": conditions,
    }
    payload.update(overrides)
    return payload


def architecture_example() -> Payload:
    """The rule of ``docs/ARCHITECTURE.md`` ``## Rule model``, character for character."""
    return {
        "name": "RSI oversold in uptrend",
        "signal": "BUY",
        "timeframe": "1d",
        "conditions": {
            "all": [
                {
                    "left": {"indicator": "rsi", "params": {"length": 14}},
                    "op": "crosses_below",
                    "right": {"value": 30},
                },
                {
                    "left": {"price": "close"},
                    "op": ">",
                    "right": {"indicator": "sma", "params": {"length": 200}},
                },
            ]
        },
        "cooldown_bars": 5,
    }


def canonical_architecture_example() -> Payload:
    """The canonical dump of ``architecture_example()`` (spec 006, AC1)."""
    return {
        "name": "RSI oversold in uptrend",
        "signal": "BUY",
        "timeframe": "1d",
        "conditions": {
            "all": [
                {
                    "left": {"indicator": "rsi", "params": {"length": 14}, "output": "value"},
                    "op": "crosses_below",
                    "right": {"value": 30.0},
                },
                {
                    "left": {"price": "close"},
                    "op": ">",
                    "right": {"indicator": "sma", "params": {"length": 200}, "output": "value"},
                },
            ]
        },
        "cooldown_bars": 5,
    }


def default_params(name: str) -> Payload:
    """The declared defaults of every parameter of ``name``, in declaration order."""
    return {param.name: param.default for param in REGISTRY.get(name).params}


def default_output(name: str) -> str:
    """The default output of ``name``, or its first declared output when there is none."""
    spec = REGISTRY.get(name)
    return spec.default_output or spec.output_names[0]


def required_output(name: str) -> str | None:
    """The output a rule must name, or ``None`` when the indicator has a default one."""
    spec = REGISTRY.get(name)
    return None if spec.default_output is not None else spec.output_names[0]


def valid_payloads() -> tuple[tuple[str, Payload], ...]:
    """Labelled payloads that ``parse_rule`` must accept."""
    payloads: list[tuple[str, Payload]] = [
        ("architecture example", architecture_example()),
        ("canonical architecture example", canonical_architecture_example()),
        ("minimum size", rule_payload({"all": [simple_condition()]})),
        ("any at the root", rule_payload({"any": [simple_condition()]})),
        (
            "ten conditions in one group",
            rule_payload({"all": [simple_condition() for _ in range(10)]}),
        ),
        (
            "nested all inside any",
            rule_payload(
                {
                    "any": [
                        simple_condition(),
                        {"all": [simple_condition(), simple_condition()]},
                    ]
                }
            ),
        ),
        (
            "twenty conditions in two nested groups",
            rule_payload(
                {
                    "all": [
                        {"any": [simple_condition() for _ in range(10)]},
                        {"all": [simple_condition() for _ in range(10)]},
                    ]
                }
            ),
        ),
        ("cooldown of zero", rule_payload({"all": [simple_condition()]}, cooldown_bars=0)),
        ("maximum cooldown", rule_payload({"all": [simple_condition()]}, cooldown_bars=500)),
        ("sell signal", rule_payload({"all": [simple_condition()]}, signal="SELL")),
        ("name with accents", rule_payload({"all": [simple_condition()]}, name=NAME_WITH_ACCENTS)),
        (
            "name of eighty characters",
            rule_payload({"all": [simple_condition()]}, name=NAME_OF_80_CHARACTERS),
        ),
        (
            "integer value operand",
            rule_payload({"all": [condition(price_operand(), "<", value_operand(30))]}),
        ),
        (
            "negative value operand",
            rule_payload({"all": [condition(price_operand(), ">", value_operand(-1e15))]}),
        ),
    ]
    payloads += [
        (f"timeframe {code}", rule_payload({"all": [simple_condition()]}, timeframe=code))
        for code in ("1h", "4h", "1d")
    ]
    payloads += [
        (
            f"operator {operator}",
            rule_payload({"all": [condition(price_operand(), operator, value_operand(100))]}),
        )
        for operator in OPERATORS
    ]
    payloads += [
        (
            f"price {column}",
            rule_payload(
                {"all": [condition(price_operand(column), ">", indicator_operand("sma"))]}
            ),
        )
        for column in PRICE_FIELDS
    ]
    for name in REGISTRY.names:
        payloads.append(
            (
                f"indicator {name} with defaults",
                rule_payload(
                    {
                        "all": [
                            condition(
                                indicator_operand(name, output=required_output(name)),
                                ">",
                                value_operand(1),
                            )
                        ]
                    }
                ),
            )
        )
        payloads.append(
            (
                f"indicator {name} fully written",
                rule_payload(
                    {
                        "all": [
                            condition(
                                indicator_operand(
                                    name, params=default_params(name), output=default_output(name)
                                ),
                                "<",
                                value_operand(1),
                            )
                        ]
                    }
                ),
            )
        )
    return tuple(payloads)


def invalid_payloads() -> tuple[tuple[str, Payload, RuleErrorKind, str], ...]:
    """Labelled payloads with the kind and the path of the problem ``parse_rule`` must report."""
    condition_path = "conditions.all[0]"
    cases: list[tuple[str, Payload, RuleErrorKind, str]] = [
        # --- the document and its fields
        ("missing name", _without("name"), RuleErrorKind.MISSING_FIELD, "name"),
        ("missing conditions", _without("conditions"), RuleErrorKind.MISSING_FIELD, "conditions"),
        (
            "unknown top level key",
            rule_payload({"all": [simple_condition()]}, **{"$ref": "other.json"}),
            RuleErrorKind.UNKNOWN_FIELD,
            "'$ref'",
        ),
        (
            "dunder key",
            rule_payload({"all": [simple_condition()]}, **{"__class__": "x"}),
            RuleErrorKind.UNKNOWN_FIELD,
            "__class__",
        ),
        (
            "name is a number",
            rule_payload({"all": [simple_condition()]}, name=5),
            RuleErrorKind.WRONG_TYPE,
            "name",
        ),
        (
            "blank name",
            rule_payload({"all": [simple_condition()]}, name="   "),
            RuleErrorKind.INVALID_NAME,
            "name",
        ),
        (
            "name of 81 characters",
            rule_payload({"all": [simple_condition()]}, name="n" * 81),
            RuleErrorKind.INVALID_NAME,
            "name",
        ),
        (
            "name with a newline",
            rule_payload({"all": [simple_condition()]}, name="two\nlines"),
            RuleErrorKind.INVALID_NAME,
            "name",
        ),
        (
            "lower case signal",
            rule_payload({"all": [simple_condition()]}, signal="buy"),
            RuleErrorKind.UNKNOWN_VALUE,
            "signal",
        ),
        (
            "upper case timeframe",
            rule_payload({"all": [simple_condition()]}, timeframe="1D"),
            RuleErrorKind.UNKNOWN_VALUE,
            "timeframe",
        ),
        (
            "padded timeframe",
            rule_payload({"all": [simple_condition()]}, timeframe=" 1d "),
            RuleErrorKind.UNKNOWN_VALUE,
            "timeframe",
        ),
        (
            "cooldown as text",
            rule_payload({"all": [simple_condition()]}, cooldown_bars="5"),
            RuleErrorKind.WRONG_TYPE,
            "cooldown_bars",
        ),
        (
            "cooldown as a float",
            rule_payload({"all": [simple_condition()]}, cooldown_bars=5.0),
            RuleErrorKind.WRONG_TYPE,
            "cooldown_bars",
        ),
        (
            "cooldown as a boolean",
            rule_payload({"all": [simple_condition()]}, cooldown_bars=True),
            RuleErrorKind.WRONG_TYPE,
            "cooldown_bars",
        ),
        (
            "negative cooldown",
            rule_payload({"all": [simple_condition()]}, cooldown_bars=-1),
            RuleErrorKind.OUT_OF_RANGE,
            "cooldown_bars",
        ),
        (
            "cooldown above the maximum",
            rule_payload({"all": [simple_condition()]}, cooldown_bars=501),
            RuleErrorKind.OUT_OF_RANGE,
            "cooldown_bars",
        ),
        # --- groups
        (
            "bare condition as conditions",
            rule_payload(simple_condition()),
            RuleErrorKind.INVALID_GROUP,
            "conditions",
        ),
        (
            "conditions is a list",
            rule_payload([simple_condition()]),
            RuleErrorKind.INVALID_GROUP,
            "conditions",
        ),
        (
            "conditions is a number",
            rule_payload(5),
            RuleErrorKind.INVALID_GROUP,
            "conditions",
        ),
        (
            "member with both group keys",
            rule_payload({"all": [{"all": [simple_condition()], "any": [simple_condition()]}]}),
            RuleErrorKind.INVALID_GROUP,
            "conditions.all[0]",
        ),
        (
            "both group keys",
            rule_payload({"all": [simple_condition()], "any": [simple_condition()]}),
            RuleErrorKind.INVALID_GROUP,
            "conditions",
        ),
        (
            "no group key",
            rule_payload({"none": [simple_condition()]}),
            RuleErrorKind.INVALID_GROUP,
            "conditions",
        ),
        ("empty group", rule_payload({"all": []}), RuleErrorKind.OUT_OF_RANGE, "conditions.all"),
        (
            "eleven members",
            rule_payload({"all": [simple_condition() for _ in range(11)]}),
            RuleErrorKind.OUT_OF_RANGE,
            "conditions.all",
        ),
        (
            "twenty one conditions",
            rule_payload(
                {
                    "all": [
                        {"all": [simple_condition() for _ in range(10)]},
                        {"all": [simple_condition() for _ in range(10)]},
                        simple_condition(),
                    ]
                }
            ),
            RuleErrorKind.TOO_MANY_CONDITIONS,
            "conditions",
        ),
        (
            "group at the third level",
            rule_payload({"all": [{"all": [{"all": [simple_condition()]}]}]}),
            RuleErrorKind.NESTED_TOO_DEEP,
            "conditions.all[0].all[0]",
        ),
        (
            "group members are not a list",
            rule_payload({"all": {"0": simple_condition()}}),
            RuleErrorKind.WRONG_TYPE,
            "conditions.all",
        ),
        (
            "group member is a list",
            rule_payload({"all": [[]]}),
            RuleErrorKind.INVALID_GROUP,
            "conditions.all[0]",
        ),
        # --- conditions
        (
            "condition with an unknown key",
            rule_payload({"all": [{**simple_condition(), "$schema": "x"}]}),
            RuleErrorKind.UNKNOWN_FIELD,
            f"{condition_path}.'$schema'",
        ),
        (
            "condition without an operator",
            rule_payload({"all": [{"left": price_operand(), "right": value_operand(1)}]}),
            RuleErrorKind.MISSING_FIELD,
            f"{condition_path}.op",
        ),
        (
            "unknown operator",
            rule_payload({"all": [condition(price_operand(), "==", value_operand(1))]}),
            RuleErrorKind.UNKNOWN_VALUE,
            f"{condition_path}.op",
        ),
        (
            "constant against constant",
            rule_payload({"all": [condition(value_operand(1), "<", value_operand(2))]}),
            RuleErrorKind.DEGENERATE_CONDITION,
            condition_path,
        ),
        (
            "identical operands",
            rule_payload(
                {
                    "all": [
                        condition(
                            indicator_operand("rsi"),
                            "<",
                            indicator_operand("rsi", params={"length": 14}, output="value"),
                        )
                    ]
                }
            ),
            RuleErrorKind.DEGENERATE_CONDITION,
            condition_path,
        ),
        # --- operands
        (
            "operand without keys",
            rule_payload({"all": [condition({}, "<", value_operand(1))]}),
            RuleErrorKind.INVALID_OPERAND,
            f"{condition_path}.left",
        ),
        (
            "operand with two kinds",
            rule_payload(
                {"all": [condition({"indicator": "rsi", "price": "close"}, "<", value_operand(1))]}
            ),
            RuleErrorKind.INVALID_OPERAND,
            f"{condition_path}.left",
        ),
        (
            "operand is a number",
            rule_payload({"all": [{"left": 5, "op": "<", "right": value_operand(1)}]}),
            RuleErrorKind.INVALID_OPERAND,
            f"{condition_path}.left",
        ),
        (
            "unknown price column",
            rule_payload({"all": [condition(price_operand("adjusted"), "<", value_operand(1))]}),
            RuleErrorKind.UNKNOWN_VALUE,
            f"{condition_path}.left.price",
        ),
        (
            "value as text",
            rule_payload({"all": [{"left": price_operand(), "op": "<", "right": {"value": "30"}}]}),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.right.value",
        ),
        (
            "value as a boolean",
            rule_payload({"all": [{"left": price_operand(), "op": "<", "right": {"value": True}}]}),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.right.value",
        ),
        (
            "value is null",
            rule_payload({"all": [{"left": price_operand(), "op": "<", "right": {"value": None}}]}),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.right.value",
        ),
        (
            "value out of range",
            rule_payload({"all": [{"left": price_operand(), "op": "<", "right": {"value": 1e16}}]}),
            RuleErrorKind.OUT_OF_RANGE,
            f"{condition_path}.right.value",
        ),
        # --- the registry
        (
            "unknown indicator",
            rule_payload({"all": [condition(indicator_operand("eval"), "<", value_operand(1))]}),
            RuleErrorKind.UNKNOWN_INDICATOR,
            f"{condition_path}.left.indicator",
        ),
        (
            "unknown parameter",
            rule_payload(
                {"all": [condition(indicator_operand("rsi", {"nope": 1}), "<", value_operand(1))]}
            ),
            RuleErrorKind.INVALID_PARAMETER,
            f"{condition_path}.left.params.nope",
        ),
        (
            "parameter out of range",
            rule_payload(
                {"all": [condition(indicator_operand("rsi", {"length": 1}), "<", value_operand(1))]}
            ),
            RuleErrorKind.INVALID_PARAMETER,
            f"{condition_path}.left.params.length",
        ),
        (
            "parameter as text",
            rule_payload(
                {
                    "all": [
                        condition(indicator_operand("rsi", {"length": "14"}), "<", value_operand(1))
                    ]
                }
            ),
            RuleErrorKind.INVALID_PARAMETER,
            f"{condition_path}.left.params.length",
        ),
        (
            "parameter as a float",
            rule_payload(
                {
                    "all": [
                        condition(indicator_operand("rsi", {"length": 14.0}), "<", value_operand(1))
                    ]
                }
            ),
            RuleErrorKind.INVALID_PARAMETER,
            f"{condition_path}.left.params.length",
        ),
        (
            "violated parameter constraint",
            rule_payload(
                {
                    "all": [
                        condition(
                            indicator_operand("macd", {"fast": 30}, "hist"), "<", value_operand(1)
                        )
                    ]
                }
            ),
            RuleErrorKind.INVALID_PARAMETER,
            f"{condition_path}.left.params.fast",
        ),
        (
            "params is not an object",
            rule_payload(
                {
                    "all": [
                        {
                            "left": {"indicator": "rsi", "params": 5},
                            "op": "<",
                            "right": value_operand(1),
                        }
                    ]
                }
            ),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.left.params",
        ),
        (
            "params is null",
            rule_payload(
                {
                    "all": [
                        {
                            "left": {"indicator": "rsi", "params": None},
                            "op": "<",
                            "right": value_operand(1),
                        }
                    ]
                }
            ),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.left.params",
        ),
        (
            "output is null",
            rule_payload(
                {
                    "all": [
                        {
                            "left": {"indicator": "rsi", "output": None},
                            "op": "<",
                            "right": value_operand(1),
                        }
                    ]
                }
            ),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.left.output",
        ),
        (
            "output is a number",
            rule_payload(
                {
                    "all": [
                        {
                            "left": {"indicator": "rsi", "output": 1},
                            "op": "<",
                            "right": value_operand(1),
                        }
                    ]
                }
            ),
            RuleErrorKind.WRONG_TYPE,
            f"{condition_path}.left.output",
        ),
        (
            "unknown output",
            rule_payload(
                {"all": [condition(indicator_operand("rsi", output="nope"), "<", value_operand(1))]}
            ),
            RuleErrorKind.INVALID_OUTPUT,
            f"{condition_path}.left.output",
        ),
    ]
    cases += [
        (
            f"missing output of {name}",
            rule_payload(
                {"all": [condition(indicator_operand(name), "<", value_operand(1))]},
            ),
            RuleErrorKind.INVALID_OUTPUT,
            f"{condition_path}.left.output",
        )
        for name in REGISTRY.names
        if REGISTRY.get(name).default_output is None
    ]
    return tuple(cases)


def _without(field: str) -> Payload:
    payload = rule_payload({"all": [simple_condition()]})
    del payload[field]
    return payload
