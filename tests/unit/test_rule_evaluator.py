"""Tests of the rule evaluator API, result, indicator values and cost structure (spec 007).

T1 pins the public API, the ``Evaluation`` dataclass and the candle identity; T6 the indicator
values and their literal keys; T7 purity, registry injection, the cost structure (with a counting
registry double, never with timings) and the ``last`` argument. Expected values are literals:
nothing is recomputed with the code under test (spec 007, Design 14).
"""

from __future__ import annotations

import dataclasses
import inspect
import pickle
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from tests.fixtures.rules import (
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    value_operand,
)
from tests.lookahead import values_equal
from trading_bot.domain.candles import CandleErrorKind, CandleValidationError
from trading_bot.domain.indicators.catalog import CATALOG, REGISTRY, SMA
from trading_bot.domain.indicators.errors import IndicatorComputationError, UnknownIndicatorError
from trading_bot.domain.indicators.params import IndicatorParams
from trading_bot.domain.indicators.registry import IndicatorRegistry, JsonValue
from trading_bot.domain.rules import evaluator
from trading_bot.domain.rules.evaluator import Evaluation, evaluate, evaluate_each
from trading_bot.domain.rules.schema import PriceField, Rule, parse_rule
from trading_bot.domain.signals import IndicatorValues, Signal

CLOSE_ABOVE_100 = condition(price_operand("close"), ">", value_operand(100))


def rule_of(conditions: JsonValue, **overrides: JsonValue) -> Rule:
    return parse_rule(rule_payload(conditions, **overrides))


def frame_at(opens: Sequence[str], closes: Sequence[float]) -> pd.DataFrame:
    """A valid frame with ``open == close`` whose index is the given UTC open times."""
    frame = candles_from_prices(closes)
    return frame.set_axis(pd.DatetimeIndex(list(opens), tz="UTC"), axis=0)


def an_evaluation(**overrides: object) -> Evaluation:
    fields: dict[str, object] = {
        "triggered": True,
        "candle_close_ts": datetime(2024, 1, 3, 5, 0, tzinfo=UTC),
        "close_price": 101.5,
        "indicator_values": IndicatorValues({"rsi(length=14).value": 28.4, "b": 1.0}),
        "condition_results": (True, False),
    }
    fields.update(overrides)
    return Evaluation(**fields)  # type: ignore[arg-type]


# --- T1: public API (AC1) -------------------------------------------------------------------


def test_the_module_exports_exactly_the_evaluator_api() -> None:
    assert sorted(evaluator.__all__) == ["Evaluation", "evaluate", "evaluate_each"]


def test_evaluate_signature() -> None:
    parameters = inspect.signature(evaluate).parameters

    assert list(parameters) == ["rule", "candles", "registry"]
    assert parameters["rule"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["candles"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["registry"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["registry"].default is REGISTRY


def test_evaluate_each_signature() -> None:
    parameters = inspect.signature(evaluate_each).parameters

    assert list(parameters) == ["rule", "candles", "last", "registry"]
    assert parameters["last"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["last"].default is None
    assert parameters["registry"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["registry"].default is REGISTRY


def test_evaluate_each_returns_one_evaluation_per_candle_oldest_first() -> None:
    candles = candles_from_prices([99.0, 101.0, 100.0, 102.0])

    evaluations = evaluate_each(rule_of({"all": [CLOSE_ABOVE_100]}), candles)

    assert isinstance(evaluations, tuple)
    assert [evaluation.close_price for evaluation in evaluations] == [99.0, 101.0, 100.0, 102.0]
    assert [evaluation.triggered for evaluation in evaluations] == [False, True, False, True]


# --- T1: the Evaluation dataclass (AC2) -----------------------------------------------------


def test_evaluation_has_exactly_the_documented_fields() -> None:
    fields = dataclasses.fields(Evaluation)

    assert [field.name for field in fields] == [
        "triggered",
        "candle_close_ts",
        "close_price",
        "indicator_values",
        "condition_results",
    ]
    assert all(field.kw_only for field in fields)


def test_evaluation_is_slotted_and_keyword_only() -> None:
    evaluation = an_evaluation()

    assert "__slots__" in vars(Evaluation)
    assert not hasattr(evaluation, "__dict__")
    with pytest.raises(TypeError):
        Evaluation(True, evaluation.candle_close_ts, 1.0, IndicatorValues(), ())  # type: ignore[misc]


def test_evaluation_is_immutable() -> None:
    evaluation = an_evaluation()

    with pytest.raises(dataclasses.FrozenInstanceError):
        evaluation.triggered = False  # type: ignore[misc]


def test_evaluations_are_equal_and_hashable_by_value() -> None:
    first = an_evaluation(indicator_values=IndicatorValues({"a": 1.0, "b": 2.0}))
    reordered = an_evaluation(indicator_values=IndicatorValues({"b": 2.0, "a": 1.0}))
    different = an_evaluation(condition_results=(True, True))

    assert first == reordered
    assert hash(first) == hash(reordered)
    assert first != different
    assert len({first, reordered, different}) == 2


def test_evaluation_survives_pickle() -> None:
    evaluation = an_evaluation()

    restored = pickle.loads(pickle.dumps(evaluation))  # noqa: S301 - our own trusted bytes

    assert restored == evaluation
    assert list(restored.indicator_values) == list(evaluation.indicator_values)


def test_values_equal_compares_evaluations_field_by_field() -> None:
    first = an_evaluation(indicator_values=IndicatorValues({"a": 1.0, "b": 2.0}))
    reordered = an_evaluation(indicator_values=IndicatorValues({"b": 2.0, "a": 1.0}))

    assert values_equal(first, reordered)
    empty = an_evaluation(indicator_values=IndicatorValues())
    assert values_equal(empty, an_evaluation(indicator_values=IndicatorValues()))
    assert not values_equal(first, an_evaluation(close_price=101.25))
    assert not values_equal(first, an_evaluation(triggered=False))
    assert not values_equal(first, an_evaluation(condition_results=(True,)))
    assert not values_equal(
        first, an_evaluation(candle_close_ts=datetime(2024, 1, 4, 5, 0, tzinfo=UTC))
    )
    assert not values_equal(first, an_evaluation(indicator_values=IndicatorValues({"a": 1.0})))


# --- T1: candle identity (AC3) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "opens", "expected_closes"),
    [
        (
            "1h",
            ["2024-01-02T14:00:00", "2024-01-02T15:00:00"],
            [datetime(2024, 1, 2, 15, 0, tzinfo=UTC), datetime(2024, 1, 2, 16, 0, tzinfo=UTC)],
        ),
        (
            "4h",
            ["2024-01-02T08:00:00", "2024-01-02T12:00:00"],
            [datetime(2024, 1, 2, 12, 0, tzinfo=UTC), datetime(2024, 1, 2, 16, 0, tzinfo=UTC)],
        ),
        (
            "1d",
            ["2024-01-02T05:00:00", "2024-01-03T05:00:00"],
            [datetime(2024, 1, 3, 5, 0, tzinfo=UTC), datetime(2024, 1, 4, 5, 0, tzinfo=UTC)],
        ),
    ],
)
def test_candle_close_ts_is_the_nominal_close_of_each_candle(
    timeframe: str, opens: list[str], expected_closes: list[datetime]
) -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]}, timeframe=timeframe)
    candles = frame_at(opens, [99.0, 101.0])

    evaluations = evaluate_each(rule, candles)

    assert [evaluation.candle_close_ts for evaluation in evaluations] == expected_closes
    assert evaluate(rule, candles).candle_close_ts == expected_closes[-1]


def test_candle_close_ts_is_a_stdlib_datetime_in_utc() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})

    for evaluation in evaluate_each(rule, candles_from_prices([99.0, 101.0])):
        assert type(evaluation.candle_close_ts) is datetime
        assert evaluation.candle_close_ts.tzinfo is UTC


def test_candle_close_ts_is_converted_from_any_utc_index_unit() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = frame_at(["2024-01-02T05:00:00"], [101.0])
    seconds = candles.set_axis(candles.index.as_unit("s"), axis=0)

    assert evaluate(rule, seconds).candle_close_ts == datetime(2024, 1, 3, 5, 0, tzinfo=UTC)


def test_close_price_is_the_close_of_the_evaluated_candle_as_a_float() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = candles_from_prices([99.25, 101.5, 100.75])

    evaluations = evaluate_each(rule, candles)
    last = evaluate(rule, candles)

    assert [evaluation.close_price for evaluation in evaluations] == [99.25, 101.5, 100.75]
    assert all(type(evaluation.close_price) is float for evaluation in evaluations)
    assert last.close_price == 100.75
    assert type(last.close_price) is float


def test_evaluate_describes_the_last_row_of_the_frame() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = frame_at(["2024-01-02T05:00:00", "2024-01-03T05:00:00"], [101.0, 99.0])

    evaluation = evaluate(rule, candles)

    assert evaluation == Evaluation(
        triggered=False,
        candle_close_ts=datetime(2024, 1, 4, 5, 0, tzinfo=UTC),
        close_price=99.0,
        indicator_values=IndicatorValues(),
        condition_results=(False,),
    )


def test_an_open_time_below_microsecond_precision_has_no_candle_identity() -> None:
    """The frame contract allows any index unit, but ``nominal_close`` needs a stdlib datetime.

    Such labels never come from a provider; the evaluator surfaces the ``to_utc`` error instead
    of silently rounding the identity of a signal.
    """
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = frame_at(["2024-01-02T05:00:00.000000001"], [101.0])

    with pytest.raises(ValueError, match="sub-microsecond"):
        evaluate(rule, candles)


# --- T6: indicator values (AC9, AC10) -------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "expected"),
    [("open", 100.0), ("high", 110.0), ("low", 90.0), ("close", 105.0), ("volume", 7.0)],
)
def test_a_price_operand_reads_the_column_it_names(column: str, expected: float) -> None:
    rule = rule_of(
        {
            "all": [
                condition(price_operand(column), ">=", value_operand(expected)),
                condition(price_operand(column), "<=", value_operand(expected)),
            ]
        }
    )
    candles = candles_from_prices([105.0], high=[110.0], low=[90.0], volume=[7.0])
    candles = candles.assign(open=[100.0])

    evaluation = evaluate(rule, candles)

    assert {field.value for field in PriceField} == {"open", "high", "low", "close", "volume"}
    assert evaluation.condition_results == (True, True)
    assert evaluation.triggered is True


def test_indicator_value_keys_are_descriptive_literals_in_document_order() -> None:
    rule = rule_of(
        {
            "all": [
                condition(indicator_operand("rsi"), "<", value_operand(30)),
                condition(indicator_operand("macd", output="hist"), ">", value_operand(0)),
                condition(
                    price_operand(), "<", indicator_operand("bbands", {"std": 2}, output="upper")
                ),
                condition(indicator_operand("stoch", output="k"), "<", value_operand(20)),
                condition(
                    indicator_operand("obv", output="value"),
                    ">",
                    indicator_operand("obv", output="signal"),
                ),
                condition(price_operand(), ">", indicator_operand("sma", {"length": 200})),
            ]
        }
    )

    evaluation = evaluate(rule, synthetic_candles(300, seed=17))

    assert list(evaluation.indicator_values) == [
        "rsi(length=14).value",
        "macd(fast=12, slow=26, signal=9).hist",
        "bbands(length=20, std=2.0).upper",
        "stoch(length=14, smooth_k=3, smooth_d=3).k",
        "obv(signal=20).value",
        "obv(signal=20).signal",
        "sma(length=200).value",
    ]


def test_indicator_values_are_the_registry_outputs_at_the_evaluated_candle() -> None:
    rule = rule_of(
        {
            "all": [
                condition(indicator_operand("rsi"), "<", value_operand(30)),
                condition(indicator_operand("bbands", output="lower"), "<", price_operand()),
            ]
        }
    )
    candles = synthetic_candles(120, seed=4)

    evaluations = evaluate_each(rule, candles)

    rsi = REGISTRY.compute("rsi", {}, candles)["value"]
    lower = REGISTRY.compute("bbands", {}, candles)["lower"]
    for position in (-1, -2, 50):
        values = evaluations[position].indicator_values
        assert values["rsi(length=14).value"] == float(rsi.iloc[position])
        assert values["bbands(length=20, std=2.0).lower"] == float(lower.iloc[position])
        assert all(type(value) is float for value in values.values())
    assert isinstance(evaluations[-1].indicator_values, IndicatorValues)


def test_equal_operands_after_normalization_share_one_entry() -> None:
    short = indicator_operand("rsi")
    written = indicator_operand("rsi", {"length": 14}, "value")
    rule = rule_of(
        {
            "all": [
                condition(short, "<", value_operand(70)),
                condition(written, ">", value_operand(30)),
                condition(value_operand(50), "<", written),
            ]
        }
    )

    evaluation = evaluate(rule, synthetic_candles(60, seed=8))

    rsi = evaluation.indicator_values["rsi(length=14).value"]
    assert list(evaluation.indicator_values) == ["rsi(length=14).value"]
    assert evaluation.condition_results == (rsi < 70, rsi > 30, rsi > 50)


def test_distinct_params_and_outputs_have_distinct_entries() -> None:
    rule = rule_of(
        {
            "any": [
                condition(
                    indicator_operand("sma", {"length": 20}),
                    ">",
                    indicator_operand("sma", {"length": 200}),
                ),
                condition(
                    indicator_operand("macd", output="macd"),
                    "crosses_above",
                    indicator_operand("macd", output="signal"),
                ),
            ]
        }
    )

    evaluation = evaluate(rule, synthetic_candles(260, seed=9))

    assert list(evaluation.indicator_values) == [
        "sma(length=20).value",
        "sma(length=200).value",
        "macd(fast=12, slow=26, signal=9).macd",
        "macd(fast=12, slow=26, signal=9).signal",
    ]


def test_insertion_order_is_the_first_appearance_left_before_right() -> None:
    rsi = indicator_operand("rsi")
    rule = rule_of(
        {
            "any": [
                condition(indicator_operand("sma", {"length": 50}), "<", rsi),
                {"all": [condition(price_operand(), ">", indicator_operand("sma"))]},
                condition(rsi, ">", indicator_operand("ema", {"length": 10})),
            ]
        }
    )

    evaluation = evaluate(rule, synthetic_candles(80, seed=2))

    assert list(evaluation.indicator_values) == [
        "sma(length=50).value",
        "rsi(length=14).value",
        "sma(length=20).value",
        "ema(length=10).value",
    ]


def test_nan_indicator_values_are_absent() -> None:
    rule = rule_of(
        {
            "all": [
                condition(price_operand(), ">", indicator_operand("sma", {"length": 2})),
                condition(indicator_operand("rsi"), "<", value_operand(50)),
            ]
        }
    )

    evaluations = evaluate_each(rule, candles_from_prices([100.0, 101.0, 99.0, 102.0]))

    assert evaluations[0].indicator_values == IndicatorValues()
    assert list(evaluations[-1].indicator_values) == ["sma(length=2).value"]
    assert evaluations[-1].indicator_values["sma(length=2).value"] == 100.5


def test_prices_and_constants_are_never_indicator_values() -> None:
    rule = rule_of(
        {
            "all": [
                condition(price_operand("close"), ">", price_operand("open")),
                condition(price_operand("volume"), ">", value_operand(100)),
            ]
        }
    )

    for evaluation in evaluate_each(rule, synthetic_candles(20, seed=1)):
        assert evaluation.indicator_values == IndicatorValues()


def test_a_signal_accepts_the_evaluation_unchanged() -> None:
    rule = rule_of(
        {
            "all": [
                condition(indicator_operand("rsi"), "<", value_operand(101)),
                condition(price_operand(), ">", indicator_operand("sma", {"length": 5})),
            ]
        }
    )
    evaluation = evaluate(rule, synthetic_candles(40, seed=6))

    signal = Signal(
        ticker="aapl",
        timeframe=rule.timeframe,
        rule_id="42",
        side=rule.signal,
        candle_close_ts=evaluation.candle_close_ts,
        close_price=evaluation.close_price,
        indicator_values=evaluation.indicator_values,
    )

    assert len(evaluation.indicator_values) == 2
    assert signal.candle_close_ts == evaluation.candle_close_ts
    assert signal.close_price == evaluation.close_price
    assert signal.indicator_values == evaluation.indicator_values
    assert list(signal.indicator_values) == list(evaluation.indicator_values)


# --- T7: purity, injection, cost structure and the N-candle form (AC12-AC16) ----------------


class CountingRegistry(IndicatorRegistry):
    """The real catalog, recording every ``compute`` call as ``(name, params)``."""

    calls: list[tuple[str, IndicatorParams]]

    def __init__(self) -> None:
        super().__init__(CATALOG)
        object.__setattr__(self, "calls", [])

    def compute(
        self, name: str, params: Mapping[str, object], candles: pd.DataFrame
    ) -> dict[str, pd.Series[float]]:
        self.calls.append((name, REGISTRY.validate_params(name, params)))
        return super().compute(name, params, candles)


class FailingRegistry(IndicatorRegistry):
    """A registry whose computation is broken."""

    error: Exception

    def __init__(self, error: Exception) -> None:
        super().__init__(CATALOG)
        object.__setattr__(self, "error", error)

    def compute(
        self, name: str, params: Mapping[str, object], candles: pd.DataFrame
    ) -> dict[str, pd.Series[float]]:
        raise self.error


def many_indicators_rule() -> Rule:
    """Six indicator operands over four distinct ``(indicator, params)`` pairs."""
    return rule_of(
        {
            "any": [
                condition(
                    indicator_operand("macd", output="macd"),
                    "crosses_above",
                    indicator_operand("macd", output="signal"),
                ),
                condition(indicator_operand("rsi"), "<", value_operand(30)),
                {
                    "all": [
                        condition(
                            indicator_operand("rsi", {"length": 14}, "value"),
                            ">",
                            value_operand(10),
                        ),
                        condition(price_operand(), ">", indicator_operand("sma")),
                    ]
                },
                condition(indicator_operand("sma", {"length": 200}), "<", price_operand()),
                condition(indicator_operand("macd", output="hist"), ">", value_operand(0)),
            ]
        }
    )


EXPECTED_COMPUTATIONS = [
    ("macd", IndicatorParams({"fast": 12, "slow": 26, "signal": 9})),
    ("rsi", IndicatorParams({"length": 14})),
    ("sma", IndicatorParams({"length": 20})),
    ("sma", IndicatorParams({"length": 200})),
]


def test_each_distinct_indicator_and_params_is_computed_once_per_call() -> None:
    registry = CountingRegistry()

    evaluate(many_indicators_rule(), synthetic_candles(250, seed=12), registry=registry)

    assert registry.calls == EXPECTED_COMPUTATIONS


@pytest.mark.parametrize("length", [0, 1, 30, 250])
@pytest.mark.parametrize("last", [None, 0, 1, 7, 10_000])
def test_the_computation_count_depends_only_on_the_rule(length: int, last: int | None) -> None:
    rule = many_indicators_rule()
    candles = synthetic_candles(250, seed=12).iloc[:length]
    each = CountingRegistry()

    evaluate_each(rule, candles, last=last, registry=each)

    assert each.calls == EXPECTED_COMPUTATIONS
    if length > 0:
        single = CountingRegistry()
        evaluate(rule, candles, registry=single)
        assert single.calls == EXPECTED_COMPUTATIONS


def test_a_rule_without_indicators_computes_nothing() -> None:
    registry = CountingRegistry()
    rule = rule_of({"all": [condition(price_operand(), "crosses_above", price_operand("open"))]})

    evaluate(rule, synthetic_candles(30, seed=1), registry=registry)
    evaluate_each(rule, synthetic_candles(30, seed=1), registry=registry)

    assert registry.calls == []


def test_the_frame_is_never_modified() -> None:
    candles = synthetic_candles(260, seed=13, scenario=Scenario.MIXED)
    candles.index.name = "open_time"
    snapshot = candles.copy(deep=True)

    evaluate(many_indicators_rule(), candles)
    evaluate_each(many_indicators_rule(), candles, last=20)
    evaluate_each(many_indicators_rule(), candles)

    pd.testing.assert_frame_equal(candles, snapshot, check_exact=True)
    assert candles.index.name == "open_time"


def test_evaluation_is_deterministic() -> None:
    rule = many_indicators_rule()
    candles = synthetic_candles(260, seed=14, scenario=Scenario.MIXED)

    assert evaluate(rule, candles) == evaluate(rule, candles.copy(deep=True))
    assert evaluate_each(rule, candles) == evaluate_each(rule, candles)


@pytest.mark.parametrize("scenario", list(Scenario))
def test_evaluate_equals_the_last_evaluation_of_evaluate_each(scenario: Scenario) -> None:
    rule = many_indicators_rule()
    candles = synthetic_candles(240, seed=15, scenario=scenario)

    single = evaluate(rule, candles)
    each = evaluate_each(rule, candles)

    assert len(each) == 240
    assert values_equal(single, each[-1])
    assert single == each[-1]


def test_last_selects_a_suffix_of_the_whole_frame_evaluation() -> None:
    sma = indicator_operand("sma", {"length": 3})
    rule = rule_of({"all": [condition(price_operand(), "crosses_above", sma)]})
    candles = synthetic_candles(12, seed=16)
    whole = evaluate_each(rule, candles)

    assert len(whole) == 12
    assert evaluate_each(rule, candles, last=None) == whole
    assert evaluate_each(rule, candles, last=0) == ()
    for k in range(1, 16):
        suffix = evaluate_each(rule, candles, last=k)
        assert suffix == whole[-k:]
        assert values_equal(suffix, whole[-k:])
    assert evaluate_each(rule, candles, last=10**30) == whole


def test_a_negative_last_raises_value_error() -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})

    with pytest.raises(ValueError, match="last must not be negative"):
        evaluate_each(rule, candles_from_prices([101.0]), last=-1)


@pytest.mark.parametrize("last", [True, False, 1.0, "3", np.int64(3)])
def test_a_last_that_is_not_an_int_raises_type_error(last: object) -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})

    with pytest.raises(TypeError, match="last must be an int or None"):
        evaluate_each(rule, candles_from_prices([101.0]), last=last)  # type: ignore[arg-type]


@pytest.mark.parametrize("entry_point", ["evaluate", "evaluate_each"])
def test_arguments_of_the_wrong_type_raise_type_error(entry_point: str) -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = candles_from_prices([101.0])
    function = evaluate if entry_point == "evaluate" else evaluate_each

    with pytest.raises(TypeError, match="rule must be a Rule"):
        function(rule_payload({"all": [CLOSE_ABOVE_100]}), candles)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="candles must be a pandas DataFrame"):
        function(rule, candles["close"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="registry must be an IndicatorRegistry"):
        function(rule, candles, registry=CATALOG)  # type: ignore[arg-type]


@pytest.mark.parametrize("entry_point", ["evaluate", "evaluate_each"])
def test_an_invalid_frame_raises_the_candle_validation_error(entry_point: str) -> None:
    rule = rule_of({"all": [CLOSE_ABOVE_100]})
    candles = candles_from_prices([101.0, 102.0])
    function = evaluate if entry_point == "evaluate" else evaluate_each

    with pytest.raises(CandleValidationError) as caught:
        function(rule, candles.assign(close=[101.0, np.nan]))
    with pytest.raises(CandleValidationError):
        function(rule, candles[["open", "high", "low", "close"]])

    assert caught.value.kind is CandleErrorKind.MISSING_VALUE


@pytest.mark.parametrize("entry_point", ["evaluate", "evaluate_each"])
def test_registry_errors_propagate_unchanged(entry_point: str) -> None:
    rule = rule_of({"all": [condition(indicator_operand("rsi"), "<", value_operand(30))]})
    candles = synthetic_candles(40, seed=18)
    function = evaluate if entry_point == "evaluate" else evaluate_each
    broken = IndicatorComputationError("kernel of rsi returned an invalid array")

    with pytest.raises(IndicatorComputationError) as computation:
        function(rule, candles, registry=FailingRegistry(broken))
    with pytest.raises(UnknownIndicatorError):
        function(rule, candles, registry=IndicatorRegistry((SMA,)))

    assert computation.value is broken
