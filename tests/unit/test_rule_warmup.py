"""Tests of the rule warmup (spec 006, T8, AC17).

Warmup is a pure function of the rule and of the indicator registry: it reads no candle, so
these tests only build rules and compare counts against ``REGISTRY.warmup``/``stable_warmup``.
"""

from __future__ import annotations

import pytest

from tests.fixtures.rules import (
    architecture_example,
    condition,
    indicator_operand,
    price_operand,
    required_output,
    rule_payload,
    simple_condition,
    valid_payloads,
    value_operand,
)
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.rules.schema import Rule, parse_rule


def parsed(conditions: object) -> Rule:
    return parse_rule(rule_payload(conditions))  # type: ignore[arg-type]


def test_the_architecture_rule_needs_the_slowest_indicator() -> None:
    rule = parse_rule(architecture_example())

    assert rule.warmup() == 200
    assert rule.stable_warmup() == 200


def test_a_macd_crossover_against_its_signal_line() -> None:
    rule = parsed(
        {
            "all": [
                condition(
                    indicator_operand("macd", output="macd"),
                    "crosses_above",
                    indicator_operand("macd", output="signal"),
                )
            ]
        }
    )

    assert rule.warmup() == 35
    assert rule.stable_warmup() == 165


def test_an_rsi_crossover_against_a_constant() -> None:
    rule = parsed(
        {"all": [condition(indicator_operand("rsi"), "crosses_below", value_operand(30))]}
    )

    assert rule.warmup() == 16
    assert rule.stable_warmup() == 114


def test_a_rule_of_prices_and_constants_needs_one_candle() -> None:
    rule = parsed({"all": [condition(price_operand(), ">", value_operand(100))]})

    assert rule.warmup() == 1
    assert rule.stable_warmup() == 1


def test_a_price_crossover_needs_two_candles() -> None:
    rule = parsed({"all": [condition(price_operand(), "crosses_above", value_operand(100))]})

    assert rule.warmup() == 2
    assert rule.stable_warmup() == 2


def test_a_constant_side_never_adds_a_candle() -> None:
    comparison = parsed({"all": [condition(indicator_operand("sma"), ">", value_operand(1))]})
    crossover = parsed(
        {"all": [condition(indicator_operand("sma"), "crosses_above", value_operand(1))]}
    )

    assert comparison.warmup() == REGISTRY.warmup("sma", {})
    assert crossover.warmup() == comparison.warmup() + 1


def test_a_crossover_adds_one_candle_to_both_series_sides() -> None:
    rule = parsed({"all": [condition(price_operand(), "crosses_below", indicator_operand("ema"))]})

    assert rule.warmup() == REGISTRY.warmup("ema", {}) + 1


def test_the_rule_takes_the_maximum_over_its_conditions() -> None:
    conditions = [
        condition(indicator_operand("sma", {"length": 50}), ">", value_operand(1)),
        condition(indicator_operand("sma", {"length": 200}), ">", value_operand(2)),
        condition(price_operand(), ">", value_operand(3)),
    ]

    assert parsed({"all": conditions}).warmup() == REGISTRY.warmup("sma", {"length": 200})


def test_the_group_shape_does_not_change_the_warmup() -> None:
    first = condition(indicator_operand("rsi"), "<", value_operand(30))
    second = condition(price_operand(), ">", indicator_operand("sma"))
    flat = parsed({"all": [first, second]})
    nested = parsed({"any": [{"all": [first]}, {"any": [second]}]})

    assert flat.warmup() == nested.warmup()
    assert flat.stable_warmup() == nested.stable_warmup()


@pytest.mark.parametrize("name", REGISTRY.names)
def test_an_indicator_operand_reports_the_registry_warmup(name: str) -> None:
    rule = parsed(
        {
            "all": [
                condition(
                    indicator_operand(name, output=required_output(name)), ">", value_operand(1)
                )
            ]
        }
    )

    assert rule.warmup() == REGISTRY.warmup(name, {})
    assert rule.stable_warmup() == REGISTRY.stable_warmup(name, {})


@pytest.mark.parametrize(
    ("label", "payload"),
    valid_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_valid_rule_has_a_sane_warmup(label: str, payload: dict[str, object]) -> None:
    rule = parse_rule(payload)

    assert 1 <= rule.warmup() <= rule.stable_warmup()


def test_the_condition_warmup_is_the_maximum_of_its_operands() -> None:
    rule = parsed({"all": [simple_condition()]})
    only = rule.all_conditions[0]

    assert only.warmup(stable=False) == REGISTRY.warmup("sma", {})
    assert only.warmup(stable=True) == REGISTRY.stable_warmup("sma", {})
