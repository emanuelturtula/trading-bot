"""Tests of indicator errors and parameter validation (spec 005, Test plan T1; AC2, AC4, AC14)."""

from __future__ import annotations

import copy
import math
import pickle
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

from trading_bot.domain.indicators.errors import (
    IndicatorComputationError,
    IndicatorError,
    IndicatorErrorKind,
    InvalidOutputError,
    InvalidParameterError,
    UnknownIndicatorError,
)
from trading_bot.domain.indicators.params import (
    IndicatorParams,
    LessThan,
    ParamKind,
    ParamSpec,
    validate_params,
)

HOSTILE = ("\n\x1b[31m" + "x" * 50) * 200  # 10 000+ characters with newlines and ANSI escapes


def int_param(name: str, default: int, minimum: int, maximum: int) -> ParamSpec:
    return ParamSpec(
        name=name,
        kind=ParamKind.INT,
        default=default,
        minimum=minimum,
        maximum=maximum,
        label=name.title(),
        description=f"The {name}.",
    )


def float_param(name: str, default: float, minimum: float, maximum: float) -> ParamSpec:
    return ParamSpec(
        name=name,
        kind=ParamKind.FLOAT,
        default=default,
        minimum=minimum,
        maximum=maximum,
        label=name.title(),
        description=f"The {name}.",
    )


RSI_PARAMS = (int_param("length", 14, 2, 100),)
MACD_PARAMS = (
    int_param("fast", 12, 2, 100),
    int_param("slow", 26, 3, 200),
    int_param("signal", 9, 1, 100),
)
MACD_CONSTRAINTS = (LessThan(left="fast", right="slow"),)
BBANDS_PARAMS = (int_param("length", 20, 2, 500), float_param("std", 2.0, 0.1, 5.0))


def rsi(params: Mapping[str, object]) -> IndicatorParams:
    return validate_params("rsi", RSI_PARAMS, (), params)


def macd(params: Mapping[str, object]) -> IndicatorParams:
    return validate_params("macd", MACD_PARAMS, MACD_CONSTRAINTS, params)


def bbands(params: Mapping[str, object]) -> IndicatorParams:
    return validate_params("bbands", BBANDS_PARAMS, (), params)


# --- Error types (AC4) ---------------------------------------------------------------------


def test_error_kinds_have_the_documented_values() -> None:
    assert [(kind.name, kind.value) for kind in IndicatorErrorKind] == [
        ("UNKNOWN_INDICATOR", "unknown_indicator"),
        ("UNKNOWN_PARAMETER", "unknown_parameter"),
        ("WRONG_TYPE", "wrong_type"),
        ("OUT_OF_RANGE", "out_of_range"),
        ("CONSTRAINT", "constraint"),
        ("UNKNOWN_OUTPUT", "unknown_output"),
        ("MISSING_OUTPUT", "missing_output"),
    ]


@pytest.mark.parametrize(
    "error_type", [UnknownIndicatorError, InvalidParameterError, InvalidOutputError]
)
def test_user_errors_are_indicator_errors_and_value_errors(error_type: type[Exception]) -> None:
    assert issubclass(error_type, IndicatorError)
    assert issubclass(error_type, ValueError)


def test_computation_error_is_a_runtime_error_and_not_a_value_error() -> None:
    assert issubclass(IndicatorComputationError, RuntimeError)
    assert not issubclass(IndicatorComputationError, ValueError)
    assert not issubclass(IndicatorComputationError, IndicatorError)


def test_indicator_error_exposes_its_fields_and_message() -> None:
    error = InvalidParameterError(
        IndicatorErrorKind.OUT_OF_RANGE, "a message", indicator="rsi", parameter="length"
    )

    assert error.kind is IndicatorErrorKind.OUT_OF_RANGE
    assert (error.indicator, error.parameter, error.output) == ("rsi", "length", None)
    assert str(error) == "a message"


def test_indicator_error_fields_default_to_none() -> None:
    error = IndicatorError(IndicatorErrorKind.MISSING_OUTPUT, "message")

    assert (error.indicator, error.parameter, error.output) == (None, None, None)


def test_indicator_error_keeps_at_most_32_characters_of_each_name() -> None:
    long = "n" * 40
    error = InvalidOutputError(
        IndicatorErrorKind.UNKNOWN_OUTPUT, "m", indicator=long, parameter=long, output=long
    )

    assert error.indicator == error.parameter == error.output == "n" * 32


@pytest.mark.parametrize(
    "error",
    [
        UnknownIndicatorError(IndicatorErrorKind.UNKNOWN_INDICATOR, "m1", indicator="RSI"),
        InvalidParameterError(
            IndicatorErrorKind.CONSTRAINT, "m2", indicator="macd", parameter="fast"
        ),
        InvalidOutputError(IndicatorErrorKind.UNKNOWN_OUTPUT, "m3", indicator="rsi", output="x"),
    ],
)
def test_indicator_errors_round_trip_through_pickle(error: IndicatorError) -> None:
    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 - round-trips an error built here

    assert type(restored) is type(error)
    assert (restored.kind, restored.indicator, restored.parameter, restored.output) == (
        error.kind,
        error.indicator,
        error.parameter,
        error.output,
    )
    assert str(restored) == str(error)


# --- validate_params: defaults and normalization (AC2) -------------------------------------


def test_missing_parameters_take_their_defaults_in_declaration_order() -> None:
    result = macd({})

    assert list(result.items()) == [("fast", 12), ("slow", 26), ("signal", 9)]


def test_declaration_order_wins_over_the_mapping_order() -> None:
    result = macd({"signal": 5, "fast": 3})

    assert list(result) == ["fast", "slow", "signal"]
    assert dict(result) == {"fast": 3, "slow": 26, "signal": 5}


@pytest.mark.parametrize("value", [7, np.int64(7), np.int8(7), np.uint16(7)])
def test_integer_parameters_accept_ints_and_numpy_integers_as_builtin_int(value: object) -> None:
    result = rsi({"length": value})

    assert result["length"] == 7
    assert type(result["length"]) is int


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2, 2.0),
        (2.5, 2.5),
        (np.float64(1.5), 1.5),
        (np.float32(0.5), 0.5),
        (np.int32(3), 3.0),
        (Fraction(1, 2), 0.5),
    ],
)
def test_float_parameters_accept_ints_and_reals_as_builtin_float(
    value: object, expected: float
) -> None:
    result = bbands({"std": value})

    assert result["std"] == expected
    assert type(result["std"]) is float


def test_float_default_stays_a_float() -> None:
    assert type(bbands({})["std"]) is float


def test_range_bounds_are_inclusive() -> None:
    assert rsi({"length": 2})["length"] == 2
    assert rsi({"length": 100})["length"] == 100
    assert bbands({"std": 0.1})["std"] == 0.1
    assert bbands({"std": 5})["std"] == 5.0


def test_validating_validated_params_is_idempotent() -> None:
    first = macd({"fast": 5, "slow": 30})

    second = macd(first)

    assert second == first
    assert list(second.items()) == list(first.items())


# --- validate_params: rejections (AC2, AC4) ------------------------------------------------


def test_undeclared_parameter_is_rejected() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({"period": 14})

    error = caught.value
    assert error.kind is IndicatorErrorKind.UNKNOWN_PARAMETER
    assert (error.indicator, error.parameter, error.output) == ("rsi", "period", None)
    assert str(error) == "unknown parameter 'period' of rsi; expected one of length"


def test_undeclared_parameter_of_an_indicator_without_parameters() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        validate_params("obv", (), (), {"length": 3})

    assert str(caught.value) == "unknown parameter 'length' of obv; obv takes no parameters"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (True, "True"),
        (False, "False"),
        (None, "None"),
        ("14", "'14'"),
        (Decimal("14"), "a value of type Decimal"),
        (14.0, "14.0"),
        (np.float64(14.0), "14.0"),
        (Fraction(14, 1), "a value of type Fraction"),
        ([14], "a value of type list"),
        (np.bool_(True), "a value of type bool"),
    ],
)
def test_integer_parameter_rejects_non_integers(value: object, rendered: str) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": value})

    error = caught.value
    assert error.kind is IndicatorErrorKind.WRONG_TYPE
    assert (error.indicator, error.parameter) == ("rsi", "length")
    assert str(error) == f"parameter 'length' of rsi must be an integer, got {rendered}"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (True, "True"),
        (None, "None"),
        ("2.0", "'2.0'"),
        (Decimal("2"), "a value of type Decimal"),
        (np.bool_(False), "a value of type bool"),
        (2j, "a value of type complex"),
    ],
)
def test_float_parameter_rejects_non_reals(value: object, rendered: str) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        bbands({"std": value})

    error = caught.value
    assert error.kind is IndicatorErrorKind.WRONG_TYPE
    assert str(error) == f"parameter 'std' of bbands must be a number, got {rendered}"


@pytest.mark.parametrize(
    ("value", "rendered"), [(1, "1"), (101, "101"), (-(2**40), "-1099511627776")]
)
def test_integer_parameter_rejects_values_outside_its_range(value: int, rendered: str) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": value})

    error = caught.value
    assert error.kind is IndicatorErrorKind.OUT_OF_RANGE
    assert (error.indicator, error.parameter) == ("rsi", "length")
    assert str(error) == f"parameter 'length' of rsi must be in [2, 100], got {rendered}"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (0.0, "0.0"),
        (5.5, "5.5"),
        (math.nan, "nan"),
        (math.inf, "inf"),
        (-math.inf, "-inf"),
        (np.float64("nan"), "nan"),
        (-0.0, "-0.0"),
        (5e-324, "5e-324"),
    ],
)
def test_float_parameter_rejects_values_outside_its_range(value: float, rendered: str) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        bbands({"std": value})

    error = caught.value
    assert error.kind is IndicatorErrorKind.OUT_OF_RANGE
    assert str(error) == f"parameter 'std' of bbands must be in [0.1, 5.0], got {rendered}"


def test_huge_integer_is_out_of_range_for_an_integer_parameter() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": 10**100})

    assert caught.value.kind is IndicatorErrorKind.OUT_OF_RANGE
    assert str(caught.value).endswith("got an integer too large to show")


def test_integer_too_large_for_a_float_is_out_of_range_for_a_float_parameter() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        bbands({"std": 10**400})

    assert caught.value.kind is IndicatorErrorKind.OUT_OF_RANGE
    assert str(caught.value) == (
        "parameter 'std' of bbands must be in [0.1, 5.0], got a number too large for a float"
    )


@pytest.mark.parametrize(("fast", "slow"), [(26, 12), (20, 20)])
def test_constraint_violation_is_rejected(fast: int, slow: int) -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"fast": fast, "slow": slow})

    error = caught.value
    assert error.kind is IndicatorErrorKind.CONSTRAINT
    assert (error.indicator, error.parameter) == ("macd", "fast")
    assert str(error) == (
        f"parameters of macd must satisfy fast < slow, got fast={fast}, slow={slow}"
    )


def test_constraint_uses_defaults_for_missing_parameters() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"fast": 30})

    assert str(caught.value).endswith("got fast=30, slow=26")


# --- validate_params: check order (AC2) ----------------------------------------------------


def test_unknown_names_are_checked_before_declared_values() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"fast": "bad", "zeta": 1})

    assert caught.value.kind is IndicatorErrorKind.UNKNOWN_PARAMETER


def test_unknown_names_are_reported_in_mapping_iteration_order() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"zeta": 1, "alpha": 2})

    assert caught.value.parameter == "zeta"


def test_declared_parameters_are_checked_in_declaration_order() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"signal": "bad", "fast": 0})

    assert (caught.value.kind, caught.value.parameter) == (
        IndicatorErrorKind.OUT_OF_RANGE,
        "fast",
    )


def test_type_is_checked_before_range_and_values_before_constraints() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        macd({"fast": 90, "slow": 3, "signal": 0})

    assert (caught.value.kind, caught.value.parameter) == (
        IndicatorErrorKind.OUT_OF_RANGE,
        "signal",
    )


# --- validate_params: TypeError (AC2) ------------------------------------------------------


@pytest.mark.parametrize("params", [None, [("length", 14)], "length=14", 14])
def test_non_mapping_params_raise_type_error(params: object) -> None:
    with pytest.raises(TypeError, match="parameters of rsi must be a mapping"):
        rsi(params)  # type: ignore[arg-type]


def test_non_str_parameter_name_raises_type_error() -> None:
    with pytest.raises(TypeError, match="parameter names of rsi must be str, got int"):
        rsi({14: 14})


def test_str_subclass_names_are_matched_by_value() -> None:
    class Name(str):
        __slots__ = ()

        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    result = rsi({Name("length"): 9})

    assert dict(result) == {"length": 9}
    with pytest.raises(InvalidParameterError):
        rsi({Name("period"): 9})


# --- Bounded messages (AC4) ----------------------------------------------------------------


def assert_bounded(message: str) -> None:
    assert "\n" not in message
    assert "\x1b" not in message
    assert len(message) < 300


def test_hostile_parameter_name_gives_a_bounded_single_line_message() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({HOSTILE: 1})

    assert_bounded(str(caught.value))
    assert caught.value.parameter == HOSTILE[:32]


def test_escape_heavy_input_is_echoed_within_64_columns() -> None:
    name = "\x1b" * 1000  # repr() renders each character as 4 columns

    with pytest.raises(InvalidParameterError) as caught:
        rsi({name: 1})

    message = str(caught.value)
    assert message == f"unknown parameter {'\x1b' * 15!r} of rsi; expected one of length"
    assert len(repr("\x1b" * 15)) <= 64 < len(repr("\x1b" * 16))


def test_hostile_parameter_value_gives_a_bounded_single_line_message() -> None:
    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": HOSTILE})

    assert_bounded(str(caught.value))


def test_hostile_type_name_gives_a_bounded_single_line_message() -> None:
    hostile_type = type(HOSTILE[:100], (), {})

    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": hostile_type()})
    with pytest.raises(TypeError) as caught_type:
        rsi(hostile_type())  # type: ignore[arg-type]

    assert_bounded(str(caught.value))
    assert_bounded(str(caught_type.value))


def test_long_identifier_type_name_is_bounded() -> None:
    long_type = type("T" * 100, (), {})

    with pytest.raises(InvalidParameterError) as caught:
        rsi({"length": long_type()})

    assert_bounded(str(caught.value))


# --- IndicatorParams (AC14) ----------------------------------------------------------------


def test_indicator_params_is_a_read_only_ordered_mapping() -> None:
    params = IndicatorParams({"fast": 3, "std": 2.0})

    assert isinstance(params, Mapping)
    assert list(params.items()) == [("fast", 3), ("std", 2.0)]
    assert len(params) == 2
    assert params["fast"] == 3
    with pytest.raises(KeyError):
        params["missing"]


def test_indicator_params_defaults_to_empty() -> None:
    assert dict(IndicatorParams()) == {}
    assert IndicatorParams() == {}


def test_indicator_params_rejects_attribute_assignment_and_deletion() -> None:
    params = IndicatorParams({"length": 3})

    with pytest.raises(AttributeError):
        params._items = {}  # type: ignore[misc]
    with pytest.raises(AttributeError):
        params.other = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del params._items  # type: ignore[misc]
    with pytest.raises(TypeError):
        params["length"] = 4  # type: ignore[index]


def test_indicator_params_equals_a_dict_and_hashes_regardless_of_order() -> None:
    first = IndicatorParams({"a": 1, "b": 2.5})
    second = IndicatorParams({"b": 2.5, "a": 1})

    assert first == {"a": 1, "b": 2.5}
    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1
    assert first != IndicatorParams({"a": 1})


def test_indicator_params_round_trips_through_pickle_and_deepcopy() -> None:
    params = IndicatorParams({"length": 20, "std": 2.0})

    pickled = pickle.loads(pickle.dumps(params))  # noqa: S301 - round-trips a value built here

    for restored in (pickled, copy.deepcopy(params), copy.copy(params)):
        assert type(restored) is IndicatorParams
        assert list(restored.items()) == list(params.items())
        with pytest.raises(AttributeError):
            restored.other = 1  # type: ignore[attr-defined]


def test_indicator_params_repr() -> None:
    assert repr(IndicatorParams({"length": 3, "std": 2.0})) == (
        "IndicatorParams({'length': 3, 'std': 2.0})"
    )


def test_indicator_params_does_not_alias_the_source_mapping() -> None:
    source: dict[str, int | float] = {"length": 3}
    params = IndicatorParams(source)

    source["length"] = 4

    assert params["length"] == 3


def test_indicator_params_normalizes_numbers_to_builtins() -> None:
    params = IndicatorParams({"length": np.int64(3), "std": np.float32(0.5)})

    assert type(params["length"]) is int
    assert type(params["std"]) is float


@pytest.mark.parametrize("values", [{"a": True}, {"a": "1"}, {"a": None}, {"a": Decimal(1)}])
def test_indicator_params_rejects_non_numeric_values(values: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="parameter 'a' must be an int or a float"):
        IndicatorParams(values)  # type: ignore[arg-type]


def test_indicator_params_rejects_non_str_keys_and_non_mappings() -> None:
    with pytest.raises(TypeError, match="parameter names must be str, got int"):
        IndicatorParams({1: 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="parameters must be a mapping, got list"):
        IndicatorParams([("a", 1)])  # type: ignore[arg-type]


def test_integer_and_real_accessors() -> None:
    params = IndicatorParams({"length": 3, "std": 2.5})

    assert params.integer("length") == 3
    assert params.real("std") == 2.5
    assert params.real("length") == 3.0
    assert type(params.real("length")) is float
    with pytest.raises(TypeError, match="parameter 'std' is not an integer"):
        params.integer("std")
    with pytest.raises(KeyError):
        params.integer("missing")
    with pytest.raises(KeyError):
        params.real("missing")


def test_param_spec_and_less_than_are_frozen_keyword_only_dataclasses() -> None:
    spec = int_param("length", 14, 2, 100)
    constraint = LessThan(left="fast", right="slow")

    with pytest.raises(AttributeError):
        spec.default = 3  # type: ignore[misc]
    with pytest.raises(AttributeError):
        constraint.left = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        LessThan("fast", "slow")  # type: ignore[misc]
    assert ParamKind.INT == "int"
    assert ParamKind.FLOAT == "float"
