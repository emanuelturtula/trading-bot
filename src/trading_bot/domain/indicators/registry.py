"""The indicator registry: exact-name lookup, validation, computation and metadata (spec 005).

``IndicatorRegistry.compute(name, params, candles)`` validates the name, the parameters and the
candle frame, in that order, and returns one full-length ``float64`` series per output on the
candle index: NaN before ``lookback`` and where the value is undefined, never ``±inf``. The
registry keeps no state between calls, works with any catalog of ``IndicatorSpec`` and never
imports TA-Lib (the default catalog lives in ``catalog.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

import numpy as np
import pandas as pd

from trading_bot.domain.candles import validate_candles
from trading_bot.domain.indicators.errors import (
    IndicatorComputationError,
    IndicatorErrorKind,
    InvalidOutputError,
    UnknownIndicatorError,
    _echo,
    _type_name,
)
from trading_bot.domain.indicators.params import IndicatorParams, ParamSpec
from trading_bot.domain.indicators.params import validate_params as _validate_params
from trading_bot.domain.indicators.spec import FloatArray, IndicatorSpec

__all__ = ["IndicatorRegistry", "JsonValue"]

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


class IndicatorRegistry:
    """Immutable catalog of indicator specs, looked up by exact name."""

    __slots__ = ("_by_name", "_specs")

    _by_name: MappingProxyType[str, IndicatorSpec]
    _specs: tuple[IndicatorSpec, ...]

    def __init__(self, specs: Iterable[IndicatorSpec]) -> None:
        catalog = tuple(specs)
        by_name: dict[str, IndicatorSpec] = {}
        for spec in catalog:
            if not isinstance(spec, IndicatorSpec):
                raise TypeError(
                    f"indicator catalog entries must be IndicatorSpec, got {_type_name(spec)}"
                )
            if spec.name in by_name:
                raise ValueError(f"duplicated indicator name {spec.name!r}")
            by_name[spec.name] = spec
        if not catalog:
            raise ValueError("an indicator registry needs at least one indicator")
        object.__setattr__(self, "_specs", catalog)
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and str.__str__(name) in self._by_name

    @property
    def names(self) -> tuple[str, ...]:
        """Indicator names in catalog order."""
        return tuple(spec.name for spec in self._specs)

    def get(self, name: str) -> IndicatorSpec:
        """The spec named exactly ``name`` (no stripping or case folding)."""
        if not isinstance(name, str):
            raise TypeError(f"indicator name must be a str, got {_type_name(name)}")
        spec = self._by_name.get(str.__str__(name))
        if spec is None:
            raise UnknownIndicatorError(
                IndicatorErrorKind.UNKNOWN_INDICATOR,
                f"unknown indicator {_echo(name)}; expected one of {', '.join(self.names)}",
                indicator=name,
            )
        return spec

    def validate_params(self, name: str, params: Mapping[str, object]) -> IndicatorParams:
        """Validated ``params`` of indicator ``name``, with defaults filled (spec 005, AC2)."""
        return self._resolve(name, params)[1]

    def resolve_output(self, name: str, output: str | None) -> str:
        """``output`` if the indicator declares it; its default output when ``output`` is None."""
        spec = self.get(name)
        if output is None:
            if spec.default_output is None:
                choices = ", ".join(spec.output_names)
                raise InvalidOutputError(
                    IndicatorErrorKind.MISSING_OUTPUT,
                    f"{spec.name} has several outputs; choose one of {choices}",
                    indicator=spec.name,
                )
            return spec.default_output
        if not isinstance(output, str):
            raise TypeError(f"output name must be a str or None, got {_type_name(output)}")
        resolved = str.__str__(output)
        if resolved not in spec.output_names:
            raise InvalidOutputError(
                IndicatorErrorKind.UNKNOWN_OUTPUT,
                f"unknown output {_echo(resolved)} of {spec.name}; "
                f"expected one of {', '.join(spec.output_names)}",
                indicator=spec.name,
                output=resolved,
            )
        return resolved

    def lookback(self, name: str, params: Mapping[str, object]) -> int:
        """Leading candles without a value: the first value is at row position ``lookback``."""
        spec, resolved = self._resolve(name, params)
        return _lookback(spec, resolved)

    def warmup(self, name: str, params: Mapping[str, object]) -> int:
        """``lookback + 1``: the minimum frame length for a value at the last candle."""
        spec, resolved = self._resolve(name, params)
        return _lookback(spec, resolved) + 1

    def stable_warmup(self, name: str, params: Mapping[str, object]) -> int:
        """``warmup + settle``: the frame length after which the history start barely matters."""
        spec, resolved = self._resolve(name, params)
        return _stable_warmup(spec, resolved)

    def compute(
        self, name: str, params: Mapping[str, object], candles: pd.DataFrame
    ) -> dict[str, pd.Series[float]]:
        """One ``float64`` series per output, in declared order, on ``candles.index``.

        Validates the name, the parameters and then ``validate_candles(candles)``. Frames with at
        most ``lookback`` candles give all-NaN outputs without running the kernel. Non-finite
        kernel values become NaN. ``candles`` is never modified and results never share memory
        with it or with each other.
        """
        spec, resolved = self._resolve(name, params)
        validate_candles(candles)
        length = len(candles)
        arrays: tuple[FloatArray, ...]
        if length <= _lookback(spec, resolved):
            arrays = tuple(np.full(length, np.nan) for _ in spec.outputs)
        else:
            inputs = {column: candles[column].to_numpy(dtype=np.float64) for column in spec.inputs}
            arrays = _checked_arrays(spec, spec.kernel(inputs, resolved), length)
        return {
            output.name: pd.Series(array, index=candles.index, name=output.name, copy=False)
            for output, array in zip(spec.outputs, arrays, strict=True)
        }

    def describe(self) -> list[dict[str, JsonValue]]:
        """JSON-ready metadata of every indicator in catalog order; a new object on every call."""
        return [_describe(spec) for spec in self._specs]

    def _resolve(
        self, name: str, params: Mapping[str, object]
    ) -> tuple[IndicatorSpec, IndicatorParams]:
        spec = self.get(name)
        return spec, _validate_params(spec.name, spec.params, spec.constraints, params)


def _lookback(spec: IndicatorSpec, params: IndicatorParams) -> int:
    return _count(spec, "lookback", spec.lookback(params))


def _stable_warmup(spec: IndicatorSpec, params: IndicatorParams) -> int:
    return _lookback(spec, params) + 1 + _count(spec, "settle", spec.settle(params))


def _count(spec: IndicatorSpec, field: str, value: object) -> int:
    """``value`` if it is a non-negative ``int``; a broken spec function raises otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        got = _type_name(value)
    elif value < 0:
        got = str(value)
    else:
        return value
    raise IndicatorComputationError(f"{field} of {spec.name} must be a non-negative int, got {got}")


def _checked_arrays(spec: IndicatorSpec, result: object, length: int) -> tuple[FloatArray, ...]:
    """Copies of the kernel arrays with ``±inf`` replaced by NaN, after checking their shape."""
    if not isinstance(result, tuple):
        raise IndicatorComputationError(
            f"kernel of {spec.name} must return a tuple with one array per output, "
            f"got {_type_name(result)}"
        )
    if len(result) != len(spec.outputs):
        raise IndicatorComputationError(
            f"kernel of {spec.name} returned {len(result)} arrays for {len(spec.outputs)} outputs"
        )
    arrays: list[FloatArray] = []
    for output, array in zip(spec.outputs, result, strict=True):
        if (problem := _array_problem(array, length)) is not None:
            raise IndicatorComputationError(
                f"kernel of {spec.name} returned an invalid array for output {output.name!r}: "
                f"expected a 1-D float64 numpy array with {length} rows, {problem}"
            )
        values: FloatArray = np.array(array, dtype=np.float64, copy=True)
        values[np.isinf(values)] = np.nan
        arrays.append(values)
    return tuple(arrays)


def _array_problem(array: object, length: int) -> str | None:
    if not isinstance(array, np.ndarray):
        return f"got {_type_name(array)}"
    if array.dtype != np.float64:
        return f"got dtype {array.dtype}"
    if array.shape != (length,):
        return f"got shape {array.shape}"
    return None


def _describe(spec: IndicatorSpec) -> dict[str, JsonValue]:
    resolved = _validate_params(spec.name, spec.params, spec.constraints, {})
    inputs: list[JsonValue] = [column for column in spec.inputs]
    params: list[JsonValue] = [_describe_param(param) for param in spec.params]
    constraints: list[JsonValue] = [
        {"type": "less_than", "left": constraint.left, "right": constraint.right}
        for constraint in spec.constraints
    ]
    outputs: list[JsonValue] = [
        {"name": output.name, "label": output.label} for output in spec.outputs
    ]
    return {
        "name": spec.name,
        "label": spec.label,
        "description": spec.description,
        "inputs": inputs,
        "params": params,
        "constraints": constraints,
        "outputs": outputs,
        "default_output": spec.default_output,
        "default_warmup": _lookback(spec, resolved) + 1,
        "default_stable_warmup": _stable_warmup(spec, resolved),
    }


def _describe_param(param: ParamSpec) -> dict[str, JsonValue]:
    return {
        "name": param.name,
        "label": param.label,
        "description": param.description,
        "type": param.kind.value,
        "default": param.default,
        "min": param.minimum,
        "max": param.maximum,
    }
