"""Tests of ``IndicatorSpec`` and ``IndicatorRegistry`` with fake specs (spec 005, T2-T5).

No TA-Lib here: the kernels are small numpy functions, so the registry contract (validation
order, output shape, copies, non-finite handling, ``describe``) is tested on its own.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import synthetic_candles
from tests.fixtures.indicator_frames import candles_from_prices
from trading_bot.domain.candles import CandleColumnsError, CandleIndexError
from trading_bot.domain.indicators.errors import (
    IndicatorComputationError,
    IndicatorErrorKind,
    InvalidOutputError,
    InvalidParameterError,
    UnknownIndicatorError,
)
from trading_bot.domain.indicators.params import IndicatorParams, LessThan, ParamKind, ParamSpec
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.indicators.spec import FloatArray, IndicatorSpec, InputArrays, OutputSpec


def int_param(
    name: str = "length", default: int = 3, minimum: int = 1, maximum: int = 10
) -> ParamSpec:
    return ParamSpec(
        name=name,
        kind=ParamKind.INT,
        default=default,
        minimum=minimum,
        maximum=maximum,
        label=name.title(),
        description=f"The {name}.",
    )


def float_param(
    name: str = "width", default: float = 1.0, minimum: float = 0.5, maximum: float = 2.0
) -> ParamSpec:
    return ParamSpec(
        name=name,
        kind=ParamKind.FLOAT,
        default=default,
        minimum=minimum,
        maximum=maximum,
        label=name.title(),
        description=f"The {name}.",
    )


def rolling_sum_kernel(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """Sum of the close over ``length`` candles, NaN before ``length - 1``."""
    close = inputs["close"]
    length = params.integer("length")
    value = np.full(len(close), np.nan)
    cumulative = np.cumsum(close)
    value[length - 1 :] = cumulative[length - 1 :] - np.concatenate(([0.0], cumulative[:-length]))
    return (value,)


def make_spec(**overrides: Any) -> IndicatorSpec:
    fields: dict[str, Any] = {
        "name": "fake",
        "label": "Fake",
        "description": "A fake indicator.",
        "inputs": ("close",),
        "params": (int_param(),),
        "constraints": (),
        "outputs": (OutputSpec(name="value", label="Fake"),),
        "lookback": lambda params: params.integer("length") - 1,
        "settle": lambda params: 2 * params.integer("length"),
        "kernel": rolling_sum_kernel,
    }
    fields.update(overrides)
    return IndicatorSpec(**fields)


def two_output_kernel(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    high, low = inputs["high"], inputs["low"]
    return (high - low, (high + low) * params.real("width"))


def make_band_spec(**overrides: Any) -> IndicatorSpec:
    fields: dict[str, Any] = {
        "name": "band",
        "label": "Band",
        "description": "A fake two-output indicator.",
        "inputs": ("high", "low"),
        "params": (int_param("fast", 2, 1, 50), int_param("slow", 5, 2, 60), float_param()),
        "constraints": (LessThan(left="fast", right="slow"),),
        "outputs": (OutputSpec(name="range", label="Range"), OutputSpec(name="mid", label="Mid")),
        "lookback": lambda params: params.integer("slow") - params.integer("fast"),
        "settle": lambda params: 0,
        "kernel": two_output_kernel,
    }
    fields.update(overrides)
    return IndicatorSpec(**fields)


def registry(*specs: IndicatorSpec) -> IndicatorRegistry:
    return IndicatorRegistry(specs or (make_spec(), make_band_spec()))


def raising_kernel(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    raise AssertionError("the kernel must not be called")


def fixed_kernel(result: object) -> Callable[[InputArrays, IndicatorParams], Any]:
    def kernel(inputs: InputArrays, params: IndicatorParams) -> Any:
        return result

    return kernel


def raw_param(**overrides: object) -> ParamSpec:
    fields: dict[str, Any] = {
        "name": "p",
        "kind": ParamKind.INT,
        "default": 1,
        "minimum": 0,
        "maximum": 2,
        "label": "P",
        "description": "D",
    }
    fields.update(overrides)
    return ParamSpec(**fields)


FRAME = candles_from_prices([1.0, 2.0, 3.0, 4.0, 5.0], high=[2, 3, 4, 5, 6], low=[1, 1, 2, 3, 4])


# --- T2: IndicatorSpec construction (AC14) --------------------------------------------------


def test_valid_spec_exposes_output_names_and_default_output() -> None:
    spec = make_spec()
    band = make_band_spec()

    assert spec.output_names == ("value",)
    assert spec.default_output == "value"
    assert band.output_names == ("range", "mid")
    assert band.default_output is None


def test_single_output_default_is_its_name() -> None:
    assert make_spec(outputs=(OutputSpec(name="line", label="Line"),)).default_output == "line"


def test_spec_is_frozen() -> None:
    spec = make_spec()

    for field in dataclasses.fields(IndicatorSpec):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(spec, field.name, None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            delattr(spec, field.name)
    # Unknown names cannot be set either (slots); CPython 3.12 raises TypeError instead of
    # AttributeError for them in frozen slotted dataclasses.
    with pytest.raises((AttributeError, TypeError)):
        spec.extra = 1  # type: ignore[attr-defined]


BAD_NAMES = [
    "",
    "RSI",
    "Rsi",
    "1rsi",
    "_rsi",
    "rsi-2",
    "rsi ",
    " rsi",
    "rsi\n",
    "r" * 33,
    "rs\N{LATIN SMALL LETTER I WITH ACUTE}",
    "\N{ARABIC-INDIC DIGIT ONE}",
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_spec_rejects_bad_indicator_names(name: str) -> None:
    with pytest.raises(ValueError, match="indicator name"):
        make_spec(name=name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_spec_rejects_bad_parameter_names(name: str) -> None:
    with pytest.raises(ValueError, match="parameter name"):
        make_spec(params=(int_param(name),), lookback=lambda params: 0, settle=lambda params: 0)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_spec_rejects_bad_output_names(name: str) -> None:
    with pytest.raises(ValueError, match="output name"):
        make_spec(outputs=(OutputSpec(name=name, label="X"),))


@pytest.mark.parametrize("name", ["a", "rsi", "volume_sma", "a1_b2", "x" * 32])
def test_spec_accepts_valid_names(name: str) -> None:
    spec = make_spec(
        name=name,
        params=(int_param(name),),
        outputs=(OutputSpec(name=name, label="X"),),
        lookback=lambda params: 0,
        settle=lambda params: 0,
    )

    assert spec.name == name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 3),
        ("label", None),
        ("description", b"text"),
        ("inputs", ["close"]),
        ("inputs", ("close", 1)),
        ("params", [int_param()]),
        ("params", ("length",)),
        ("constraints", [LessThan(left="a", right="b")]),
        ("constraints", ("a<b",)),
        ("outputs", [OutputSpec(name="value", label="V")]),
        ("outputs", ("value",)),
        ("lookback", 3),
        ("settle", None),
        ("kernel", "sma"),
    ],
)
def test_spec_rejects_wrong_field_types(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=field):
        make_spec(**{field: value})


def test_spec_rejects_wrong_output_spec_field_types() -> None:
    with pytest.raises(TypeError, match="output name of fake must be a str, got int"):
        make_spec(outputs=(OutputSpec(name=1, label="V"),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="label of output 'value' of fake must be a str, got int"):
        make_spec(outputs=(OutputSpec(name="value", label=1),))  # type: ignore[arg-type]


def test_spec_rejects_duplicated_parameter_names() -> None:
    with pytest.raises(ValueError, match="duplicated parameter name 'length'"):
        make_spec(params=(int_param(), int_param()))


def test_spec_rejects_duplicated_output_names() -> None:
    outputs = (OutputSpec(name="value", label="A"), OutputSpec(name="value", label="B"))

    with pytest.raises(ValueError, match="duplicated output name 'value'"):
        make_spec(outputs=outputs)


def test_spec_rejects_no_outputs() -> None:
    with pytest.raises(ValueError, match="at least one output"):
        make_spec(outputs=())


@pytest.mark.parametrize("inputs", [("price",), ("Close",), ("close", "close"), ()])
def test_spec_rejects_bad_inputs(inputs: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="input"):
        make_spec(inputs=inputs)


def test_spec_accepts_every_ohlcv_input_in_any_order() -> None:
    spec = make_spec(inputs=("volume", "open", "high", "low", "close"))

    assert spec.inputs == ("volume", "open", "high", "low", "close")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"name": 1}, "parameter name of fake must be a str, got int"),
        ({"kind": "int"}, "kind of parameter 'p' of fake must be a ParamKind, got str"),
        ({"label": 1}, "label of parameter 'p' of fake must be a str, got int"),
        (
            {"description": None},
            "description of parameter 'p' of fake must be a str, got NoneType",
        ),
        ({"default": True}, "default of parameter 'p' of fake must be an int, got bool"),
        ({"default": 1.0}, "default of parameter 'p' of fake must be an int, got float"),
        ({"minimum": 0.0}, "minimum of parameter 'p' of fake must be an int, got float"),
        ({"maximum": "2"}, "maximum of parameter 'p' of fake must be an int, got str"),
        ({"maximum": np.int64(2)}, "maximum of parameter 'p' of fake must be an int, got int64"),
        (
            {"kind": ParamKind.FLOAT, "default": 1, "minimum": 0.0, "maximum": 2.0},
            "default of parameter 'p' of fake must be a float, got int",
        ),
        (
            {"kind": ParamKind.FLOAT, "default": 1.0, "minimum": False, "maximum": 2.0},
            "minimum of parameter 'p' of fake must be a float, got bool",
        ),
        (
            {"kind": ParamKind.FLOAT, "default": 1.0, "minimum": 0.5, "maximum": np.float64(2)},
            "maximum of parameter 'p' of fake must be a float, got float64",
        ),
    ],
)
def test_spec_rejects_parameter_declarations_of_the_wrong_type(
    overrides: dict[str, object], match: str
) -> None:
    param = raw_param(**overrides)

    with pytest.raises(TypeError, match=match):
        make_spec(params=(param,), lookback=lambda params: 0, settle=lambda params: 0)


@pytest.mark.parametrize(
    ("param", "match"),
    [
        (
            int_param("p", default=11, minimum=1, maximum=10),
            r"^default 11 of parameter 'p' of fake must be in \[1, 10\]$",
        ),
        (int_param("p", default=0, minimum=1, maximum=10), "default 0 of parameter 'p'"),
        (
            int_param("p", default=5, minimum=6, maximum=4),
            "^minimum 6 of parameter 'p' of fake exceeds its maximum 4$",
        ),
        (float_param("p", default=2.5, minimum=0.5, maximum=2.0), "default 2.5 of parameter 'p'"),
        (
            float_param("p", default=float("nan"), minimum=0.5, maximum=2.0),
            "^default of parameter 'p' of fake must be finite, got nan$",
        ),
        (
            float_param("p", default=1.0, minimum=float("-inf"), maximum=2.0),
            "minimum of parameter 'p' of fake must be finite, got -inf",
        ),
        (
            float_param("p", default=1.0, minimum=0.5, maximum=float("inf")),
            "maximum of parameter 'p' of fake must be finite, got inf",
        ),
    ],
)
def test_spec_rejects_invalid_parameter_values(param: ParamSpec, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make_spec(params=(param,), lookback=lambda params: 0, settle=lambda params: 0)


def test_spec_rejects_constraints_naming_undeclared_parameters() -> None:
    with pytest.raises(
        ValueError, match=r"^constraint of band names an undeclared parameter 'medium'$"
    ):
        make_band_spec(constraints=(LessThan(left="fast", right="medium"),))
    with pytest.raises(ValueError, match="undeclared parameter 'quick'"):
        make_band_spec(constraints=(LessThan(left="quick", right="slow"),))
    with pytest.raises(TypeError, match="constraint parameters of band must be str, got int"):
        make_band_spec(constraints=(LessThan(left="fast", right=1),))  # type: ignore[arg-type]


def test_spec_rejects_constraint_violated_by_the_defaults() -> None:
    with pytest.raises(ValueError, match="defaults of band violate"):
        make_band_spec(constraints=(LessThan(left="slow", right="fast"),))


def test_spec_rejects_constraint_on_itself() -> None:
    with pytest.raises(ValueError, match="defaults of band violate"):
        make_band_spec(constraints=(LessThan(left="fast", right="fast"),))


@pytest.mark.parametrize("field", ["lookback", "settle"])
def test_spec_rejects_negative_counts_at_default_parameters(field: str) -> None:
    with pytest.raises(
        ValueError, match=f"{field} of fake at default parameters must be >= 0, got -1"
    ):
        make_spec(**{field: lambda params: -1})


@pytest.mark.parametrize("value", [1.0, True, None, np.int64(1)])
@pytest.mark.parametrize("field", ["lookback", "settle"])
def test_spec_rejects_non_int_counts_at_default_parameters(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"{field} of fake must return an int"):
        make_spec(**{field: lambda params: value})


def test_spec_accepts_zero_lookback_and_settle() -> None:
    spec = make_spec(lookback=lambda params: 0, settle=lambda params: 0)

    assert spec.lookback(IndicatorParams({"length": 3})) == 0


# --- T2: IndicatorRegistry construction (AC3, AC14) -----------------------------------------


def test_registry_names_follow_catalog_order() -> None:
    assert registry(make_band_spec(), make_spec()).names == ("band", "fake")


def test_registry_accepts_any_iterable_of_specs() -> None:
    specs = [make_spec(), make_band_spec()]

    assert IndicatorRegistry(iter(specs)).names == ("fake", "band")


def test_registry_rejects_an_empty_catalog() -> None:
    with pytest.raises(ValueError, match="at least one indicator"):
        IndicatorRegistry(())


def test_registry_rejects_duplicated_names() -> None:
    with pytest.raises(ValueError, match="duplicated indicator name 'fake'"):
        IndicatorRegistry((make_spec(), make_band_spec(), make_spec(label="Other")))


def test_registry_rejects_non_specs() -> None:
    with pytest.raises(TypeError, match="IndicatorSpec"):
        IndicatorRegistry((make_spec(), "band"))  # type: ignore[arg-type]


def test_registry_is_immutable() -> None:
    catalog = registry()

    with pytest.raises(AttributeError):
        catalog._specs = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        catalog.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del catalog._specs
    with pytest.raises(TypeError):
        catalog._by_name["other"] = make_spec()  # type: ignore[index]


def test_registry_get_and_contains() -> None:
    fake = make_spec()
    catalog = IndicatorRegistry((fake,))

    assert catalog.get("fake") is fake
    assert "fake" in catalog
    assert "FAKE" not in catalog
    assert 3 not in catalog
    assert None not in catalog


@pytest.mark.parametrize("name", ["FAKE", " fake", "fake ", "", "fak", "fake\n"])
def test_registry_get_is_exact(name: str) -> None:
    with pytest.raises(UnknownIndicatorError) as caught:
        registry().get(name)

    error = caught.value
    assert error.kind is IndicatorErrorKind.UNKNOWN_INDICATOR
    assert error.indicator == name
    assert (error.parameter, error.output) == (None, None)
    assert str(error) == f"unknown indicator {name!r}; expected one of fake, band"


def test_registry_get_matches_str_subclasses_by_value() -> None:
    class Name(str):
        __slots__ = ()

        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash("fake")

    assert registry().get(Name("fake")).name == "fake"
    with pytest.raises(UnknownIndicatorError):
        registry().get(Name("band2"))


@pytest.mark.parametrize("name", [None, 3, b"fake", ["fake"]])
def test_registry_get_rejects_non_str_names(name: object) -> None:
    with pytest.raises(TypeError, match="indicator name must be a str"):
        registry().get(name)  # type: ignore[arg-type]


def test_unknown_indicator_message_is_bounded() -> None:
    hostile = ("\n\x1b[2J" + "y" * 40) * 250

    with pytest.raises(UnknownIndicatorError) as caught:
        registry().get(hostile)

    message = str(caught.value)
    assert "\n" not in message
    assert "\x1b" not in message
    assert len(message) < 300
    assert caught.value.indicator == hostile[:32]


# --- Registry parameter helpers (AC2, AC9) --------------------------------------------------


def test_registry_validate_params_uses_the_spec() -> None:
    result = registry().validate_params("band", {"slow": 9})

    assert list(result.items()) == [("fast", 2), ("slow", 9), ("width", 1.0)]
    with pytest.raises(InvalidParameterError) as caught:
        registry().validate_params("band", {"fast": 9, "slow": 9})
    assert caught.value.kind is IndicatorErrorKind.CONSTRAINT


def test_registry_validate_params_checks_the_name_first() -> None:
    with pytest.raises(UnknownIndicatorError):
        registry().validate_params("nope", {"bad": object()})


def test_lookback_warmup_and_stable_warmup() -> None:
    catalog = registry()

    assert catalog.lookback("fake", {}) == 2
    assert catalog.warmup("fake", {}) == 3
    assert catalog.stable_warmup("fake", {}) == 9
    assert catalog.lookback("fake", {"length": 7}) == 6
    assert catalog.warmup("fake", {"length": 7}) == 7
    assert catalog.stable_warmup("fake", {"length": 7}) == 21


@pytest.mark.parametrize("method", ["lookback", "warmup", "stable_warmup"])
def test_warmup_helpers_validate_params(method: str) -> None:
    helper = getattr(registry(), method)

    with pytest.raises(InvalidParameterError):
        helper("fake", {"length": 0})
    with pytest.raises(UnknownIndicatorError):
        helper("nope", {})


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("lookback", -1, "lookback of fake must be a non-negative int, got -1"),
        ("lookback", 2.0, "lookback of fake must be a non-negative int, got float"),
        ("settle", -3, "settle of fake must be a non-negative int, got -3"),
        ("settle", True, "settle of fake must be a non-negative int, got bool"),
    ],
)
def test_counts_that_break_at_other_parameters_raise_computation_errors(
    field: str, value: object, match: str
) -> None:
    def count(params: IndicatorParams) -> object:
        return 0 if params.integer("length") == 3 else value

    catalog = registry(make_spec(**{field: count}))

    with pytest.raises(IndicatorComputationError, match=match):
        catalog.stable_warmup("fake", {"length": 4})


# --- T4: resolve_output (AC3) ---------------------------------------------------------------


def test_resolve_output_defaults_to_the_single_output() -> None:
    assert registry().resolve_output("fake", None) == "value"
    assert registry().resolve_output("fake", "value") == "value"


def test_resolve_output_accepts_each_declared_output() -> None:
    assert registry().resolve_output("band", "range") == "range"
    assert registry().resolve_output("band", "mid") == "mid"


def test_resolve_output_requires_a_choice_for_several_outputs() -> None:
    with pytest.raises(InvalidOutputError) as caught:
        registry().resolve_output("band", None)

    error = caught.value
    assert error.kind is IndicatorErrorKind.MISSING_OUTPUT
    assert (error.indicator, error.parameter, error.output) == ("band", None, None)
    assert str(error) == "band has several outputs; choose one of range, mid"


@pytest.mark.parametrize("output", ["signal", "Value", " value", "", "value\n"])
def test_resolve_output_rejects_unknown_outputs(output: str) -> None:
    with pytest.raises(InvalidOutputError) as caught:
        registry().resolve_output("fake", output)

    error = caught.value
    assert error.kind is IndicatorErrorKind.UNKNOWN_OUTPUT
    assert (error.indicator, error.output) == ("fake", output)
    assert str(error) == f"unknown output {output!r} of fake; expected one of value"


def test_resolve_output_rejects_non_str_outputs_and_unknown_indicators() -> None:
    with pytest.raises(TypeError, match="output name must be a str or None, got int"):
        registry().resolve_output("fake", 1)  # type: ignore[arg-type]
    with pytest.raises(UnknownIndicatorError):
        registry().resolve_output("nope", "value")


def test_unknown_output_message_is_bounded() -> None:
    hostile = ("\r\n\x1b]0;" + "z" * 30) * 400

    with pytest.raises(InvalidOutputError) as caught:
        registry().resolve_output("band", hostile)

    message = str(caught.value)
    assert "\n" not in message
    assert "\r" not in message
    assert "\x1b" not in message
    assert len(message) < 300


# --- T3: compute contract (AC5) -------------------------------------------------------------


def test_compute_returns_named_float64_series_on_the_candle_index() -> None:
    result = registry().compute("fake", {}, FRAME)

    assert type(result) is dict
    assert list(result) == ["value"]
    series = result["value"]
    assert isinstance(series, pd.Series)
    assert series.dtype == np.float64
    assert series.name == "value"
    assert series.index.equals(FRAME.index)
    assert series.index.dtype == FRAME.index.dtype
    np.testing.assert_array_equal(series.to_numpy(), [np.nan, np.nan, 6.0, 9.0, 12.0])


def test_compute_keeps_outputs_in_declared_order() -> None:
    result = registry().compute("band", {"width": 0.5}, FRAME)

    assert list(result) == ["range", "mid"]
    np.testing.assert_array_equal(result["range"].to_numpy(), [1.0, 2.0, 2.0, 2.0, 2.0])
    np.testing.assert_array_equal(result["mid"].to_numpy(), [1.5, 2.0, 3.0, 4.0, 5.0])
    assert result["mid"].name == "mid"


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_compute_keeps_the_index_unit_and_name(unit: str) -> None:
    frame = FRAME.copy()
    frame.index = frame.index.as_unit(unit).rename("stamp")

    series = registry().compute("fake", {}, frame)["value"]

    assert series.index.dtype == frame.index.dtype
    assert series.index.name == "stamp"


def test_compute_passes_float64_input_arrays_and_resolved_params_to_the_kernel() -> None:
    seen: list[tuple[dict[str, FloatArray], IndicatorParams]] = []

    def spy(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
        seen.append((dict(inputs), params))
        return (inputs["volume"] + inputs["open"],)

    catalog = registry(make_spec(inputs=("volume", "open"), kernel=spy))

    catalog.compute("fake", {"length": 2}, FRAME)

    ((inputs, params),) = seen
    assert list(inputs) == ["volume", "open"]
    assert all(array.dtype == np.float64 and array.ndim == 1 for array in inputs.values())
    np.testing.assert_array_equal(inputs["open"], FRAME["open"].to_numpy())
    assert isinstance(params, IndicatorParams)
    assert dict(params) == {"length": 2}


def test_compute_does_not_modify_candles() -> None:
    frame = synthetic_candles(40, seed=1)
    before = frame.copy(deep=True)

    registry().compute("fake", {}, frame)
    registry().compute("band", {}, frame)

    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    assert frame.attrs == before.attrs


def test_mutating_a_result_changes_neither_candles_nor_later_results() -> None:
    def alias(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
        close = inputs["close"]
        return (close, close)  # both outputs alias the input array

    outputs = (OutputSpec(name="a", label="A"), OutputSpec(name="b", label="B"))
    catalog = registry(make_spec(outputs=outputs, kernel=alias))
    frame = FRAME.copy()
    before = frame.copy(deep=True)

    first = catalog.compute("fake", {}, frame)
    a, b = first["a"].to_numpy(), first["b"].to_numpy()
    assert not np.shares_memory(a, b)
    assert not any(np.shares_memory(a, frame[column].to_numpy()) for column in frame)
    first["a"].iloc[3] = -1.0
    first["b"].iloc[4] = -2.0

    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    assert first["b"].iloc[3] == 4.0
    assert first["a"].iloc[4] == 5.0
    second = catalog.compute("fake", {}, frame)
    np.testing.assert_array_equal(second["a"].to_numpy(), [1.0, 2.0, 3.0, 4.0, 5.0])
    np.testing.assert_array_equal(second["b"].to_numpy(), [1.0, 2.0, 3.0, 4.0, 5.0])


def test_compute_returns_fresh_objects_on_each_call() -> None:
    catalog = registry()

    first = catalog.compute("fake", {}, FRAME)
    second = catalog.compute("fake", {}, FRAME)

    assert first is not second
    assert first["value"] is not second["value"]
    assert not np.shares_memory(first["value"].to_numpy(), second["value"].to_numpy())


def test_compute_is_deterministic() -> None:
    frame = synthetic_candles(200, seed=3)
    catalog = registry()

    first = catalog.compute("band", {"fast": 1, "slow": 7}, frame)
    second = catalog.compute("band", {"fast": 1, "slow": 7}, frame)

    for name in first:
        assert first[name].to_numpy().tobytes() == second[name].to_numpy().tobytes()


def test_compute_validates_the_name_before_params_and_candles() -> None:
    with pytest.raises(UnknownIndicatorError):
        registry().compute("nope", {"bad": 1}, pd.DataFrame())
    with pytest.raises(TypeError, match="indicator name must be a str"):
        registry().compute(None, {}, pd.DataFrame())  # type: ignore[arg-type]


def test_compute_validates_params_before_candles() -> None:
    with pytest.raises(InvalidParameterError):
        registry().compute("fake", {"length": 0}, pd.DataFrame())


def test_compute_propagates_candle_validation_errors_unchanged() -> None:
    naive = FRAME.copy()
    naive.index = naive.index.tz_localize(None)
    wrong_columns = FRAME.rename(columns={"close": "Close"})

    with pytest.raises(CandleIndexError):
        registry(make_spec(kernel=raising_kernel)).compute("fake", {}, naive)
    with pytest.raises(CandleColumnsError):
        registry(make_spec(kernel=raising_kernel)).compute("fake", {}, wrong_columns)
    with pytest.raises(TypeError, match="candles must be a pandas DataFrame"):
        registry().compute("fake", {}, FRAME["close"])  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, 1, 2, 3])
def test_short_frames_give_all_nan_outputs_without_calling_the_kernel(length: int) -> None:
    catalog = registry(make_spec(kernel=raising_kernel), make_band_spec(kernel=raising_kernel))
    frame = FRAME.iloc[:length]

    fake = catalog.compute("fake", {"length": 4}, frame)  # lookback 3
    band = catalog.compute("band", {}, frame)  # lookback 3

    for result, names in ((fake, ["value"]), (band, ["range", "mid"])):
        assert list(result) == names
        for name, series in result.items():
            assert series.name == name
            assert series.dtype == np.float64
            assert len(series) == length
            assert series.isna().all()
            assert series.index.equals(frame.index)
            assert series.index.dtype == frame.index.dtype


def test_frame_one_longer_than_lookback_calls_the_kernel() -> None:
    result = registry().compute("fake", {"length": 4}, FRAME.iloc[:4])

    np.testing.assert_array_equal(result["value"].to_numpy(), [np.nan, np.nan, np.nan, 10.0])


def test_empty_frame_gives_empty_float64_outputs() -> None:
    empty = FRAME.iloc[:0]

    result = registry(make_spec(lookback=lambda params: 0, kernel=raising_kernel)).compute(
        "fake", {}, empty
    )

    assert len(result["value"]) == 0
    assert result["value"].dtype == np.float64


def test_non_finite_kernel_values_become_nan() -> None:
    values = np.array([np.inf, -np.inf, np.nan, 1.0, -0.0])
    catalog = registry(make_spec(lookback=lambda params: 0, kernel=fixed_kernel((values,))))

    result = catalog.compute("fake", {}, FRAME)["value"].to_numpy()

    np.testing.assert_array_equal(result, [np.nan, np.nan, np.nan, 1.0, -0.0])
    assert np.isinf(values).sum() == 2  # the kernel's array is not modified


INVALID_ARRAY = "kernel of fake returned an invalid array for output 'value': "
EXPECTED_ARRAY = "expected a 1-D float64 numpy array with 5 rows, "


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (np.ones(5), "kernel of fake must return a tuple with one array per output, got ndarray"),
        ([np.ones(5)], "kernel of fake must return a tuple with one array per output, got list"),
        ((), "kernel of fake returned 0 arrays for 1 outputs"),
        ((np.ones(5), np.ones(5)), "kernel of fake returned 2 arrays for 1 outputs"),
        ((np.ones(5).tolist(),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got list"),
        ((pd.Series(np.ones(5)),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got Series"),
        ((np.ones(5, dtype=np.float32),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got dtype float32"),
        ((np.ones(5, dtype=np.int64),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got dtype int64"),
        ((np.ones((5, 1)),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got shape (5, 1)"),
        ((np.ones(4),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got shape (4,)"),
        ((np.ones(6),), f"{INVALID_ARRAY}{EXPECTED_ARRAY}got shape (6,)"),
    ],
)
def test_wrong_kernel_results_raise_computation_errors(result: object, message: str) -> None:
    catalog = registry(make_spec(lookback=lambda params: 0, kernel=fixed_kernel(result)))

    with pytest.raises(IndicatorComputationError) as caught:
        catalog.compute("fake", {}, FRAME)

    assert str(caught.value) == message


def test_kernel_exceptions_propagate() -> None:
    catalog = registry(make_spec(kernel=raising_kernel))

    with pytest.raises(AssertionError, match="must not be called"):
        catalog.compute("fake", {}, FRAME)


def test_compute_accepts_indicator_params_from_validate_params() -> None:
    catalog = registry()
    params = catalog.validate_params("band", {"slow": 4})

    result = catalog.compute("band", params, FRAME)

    assert list(result) == ["range", "mid"]


# --- T5: describe (AC13) --------------------------------------------------------------------


EXPECTED_FAKE = {
    "name": "fake",
    "label": "Fake",
    "description": "A fake indicator.",
    "inputs": ["close"],
    "params": [
        {
            "name": "length",
            "label": "Length",
            "description": "The length.",
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 10,
        }
    ],
    "constraints": [],
    "outputs": [{"name": "value", "label": "Fake"}],
    "default_output": "value",
    "default_warmup": 3,
    "default_stable_warmup": 9,
}

EXPECTED_BAND = {
    "name": "band",
    "label": "Band",
    "description": "A fake two-output indicator.",
    "inputs": ["high", "low"],
    "params": [
        {
            "name": "fast",
            "label": "Fast",
            "description": "The fast.",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 50,
        },
        {
            "name": "slow",
            "label": "Slow",
            "description": "The slow.",
            "type": "int",
            "default": 5,
            "min": 2,
            "max": 60,
        },
        {
            "name": "width",
            "label": "Width",
            "description": "The width.",
            "type": "float",
            "default": 1.0,
            "min": 0.5,
            "max": 2.0,
        },
    ],
    "constraints": [{"type": "less_than", "left": "fast", "right": "slow"}],
    "outputs": [{"name": "range", "label": "Range"}, {"name": "mid", "label": "Mid"}],
    "default_output": None,
    "default_warmup": 4,
    "default_stable_warmup": 4,
}


def test_describe_returns_json_ready_entries_in_catalog_order() -> None:
    result = registry().describe()

    assert type(result) is list
    assert result == [EXPECTED_FAKE, EXPECTED_BAND]


def test_describe_keeps_key_order_and_python_number_types() -> None:
    fake, band = registry().describe()

    assert list(fake) == list(EXPECTED_FAKE)
    assert list(band["params"][0]) == [  # type: ignore[index]
        "name",
        "label",
        "description",
        "type",
        "default",
        "min",
        "max",
    ]
    width = band["params"][2]  # type: ignore[index]
    assert all(type(width[key]) is float for key in ("default", "min", "max"))  # type: ignore[index]
    length = fake["params"][0]  # type: ignore[index]
    assert all(type(length[key]) is int for key in ("default", "min", "max"))  # type: ignore[index]
    assert json.dumps(band["params"][2]) == (  # type: ignore[index]
        '{"name": "width", "label": "Width", "description": "The width.", '
        '"type": "float", "default": 1.0, "min": 0.5, "max": 2.0}'
    )


def test_describe_serializes_without_nan_and_is_stable() -> None:
    catalog = registry()

    first = catalog.describe()
    second = catalog.describe()

    assert json.dumps(first, allow_nan=False) == json.dumps(second, allow_nan=False)
    assert first == second
    assert first is not second


def test_mutating_describe_results_does_not_change_the_next_result() -> None:
    catalog = registry()
    first = catalog.describe()

    first[0]["name"] = "changed"
    first[0]["inputs"].append("volume")  # type: ignore[union-attr]
    first[1]["params"][0]["default"] = 99  # type: ignore[index]
    first[1]["outputs"].clear()  # type: ignore[union-attr]
    first[1]["constraints"][0]["left"] = "x"  # type: ignore[index]
    first.append({})

    assert catalog.describe() == [EXPECTED_FAKE, EXPECTED_BAND]
