"""Indicator declarations: inputs, parameters, outputs, warmup and kernel (spec 005, Design 5).

An ``IndicatorSpec`` is checked on construction (spec 005, AC14), so a catalog with a bad
declaration fails at import time instead of when a rule is evaluated. The kernel is the only
part that computes values; every other field is metadata the registry validates against.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from trading_bot.domain.candles import OHLCV_COLUMNS
from trading_bot.domain.indicators.errors import _echo, _type_name
from trading_bot.domain.indicators.params import IndicatorParams, LessThan, ParamKind, ParamSpec

__all__ = ["FloatArray", "IndicatorSpec", "InputArrays", "Kernel", "OutputSpec", "ParamsFunction"]

type FloatArray = npt.NDArray[np.float64]
type InputArrays = Mapping[str, FloatArray]  # one float64 array per name in IndicatorSpec.inputs
type Kernel = Callable[[InputArrays, IndicatorParams], tuple[FloatArray, ...]]
type ParamsFunction = Callable[[IndicatorParams], int]

_MAX_NAME_LENGTH: Final = 32
_NAME_START: Final = frozenset("abcdefghijklmnopqrstuvwxyz")
_NAME_CHARACTERS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_NAME_RULE: Final = "expected 1-32 characters from a-z, 0-9 and '_', starting with a letter"


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputSpec:
    """A named output series of an indicator."""

    name: str
    label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class IndicatorSpec:
    """The declaration of one indicator. Construction raises on an invalid declaration."""

    name: str
    label: str
    description: str
    inputs: tuple[str, ...]  # candle columns, in the order the kernel reads them
    params: tuple[ParamSpec, ...]
    constraints: tuple[LessThan, ...]
    outputs: tuple[OutputSpec, ...]
    lookback: ParamsFunction  # leading candles without a value
    settle: ParamsFunction  # extra candles until the seed weighs about 0.1% or less
    kernel: Kernel  # one float64 array per output, each as long as the inputs

    def __post_init__(self) -> None:
        _check_name(self.name, "indicator name", None)
        _check_text(self.label, f"label of {self.name}")
        _check_text(self.description, f"description of {self.name}")
        _check_inputs(self)
        _check_params(self)
        _check_outputs(self)
        for field, function in (
            ("lookback", self.lookback),
            ("settle", self.settle),
            ("kernel", self.kernel),
        ):
            if not callable(function):
                raise TypeError(
                    f"{field} of {self.name} must be callable, got {_type_name(function)}"
                )
        _check_defaults(self)

    @property
    def output_names(self) -> tuple[str, ...]:
        """Output names in declaration order."""
        return tuple(output.name for output in self.outputs)

    @property
    def default_output(self) -> str | None:
        """The only output of a single-output indicator; ``None`` when there are several."""
        return self.outputs[0].name if len(self.outputs) == 1 else None


def _check_name(name: object, what: str, owner: str | None) -> None:
    of_owner = "" if owner is None else f" of {owner}"
    if not isinstance(name, str):
        raise TypeError(f"{what}{of_owner} must be a str, got {_type_name(name)}")
    valid = (
        0 < len(name) <= _MAX_NAME_LENGTH
        and name[0] in _NAME_START
        and all(character in _NAME_CHARACTERS for character in name)
    )
    if not valid:
        raise ValueError(f"invalid {what} {_echo(name)}{of_owner}: {_NAME_RULE}")


def _check_text(text: object, what: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"{what} must be a str, got {_type_name(text)}")


def _check_tuple(items: object, item_type: type, what: str) -> None:
    expected = f"{what} must be a tuple of {item_type.__name__}"
    if not isinstance(items, tuple):
        raise TypeError(f"{expected}, got {_type_name(items)}")
    for item in items:
        if not isinstance(item, item_type):
            raise TypeError(f"{expected}, got an item of type {_type_name(item)}")


def _check_inputs(spec: IndicatorSpec) -> None:
    _check_tuple(spec.inputs, str, f"inputs of {spec.name}")
    if not spec.inputs:
        raise ValueError(f"{spec.name} must read at least one input")
    for position, column in enumerate(spec.inputs):
        if column not in OHLCV_COLUMNS:
            raise ValueError(
                f"input {_echo(column)} of {spec.name} is not a candle column; "
                f"expected one of {', '.join(OHLCV_COLUMNS)}"
            )
        if column in spec.inputs[:position]:
            raise ValueError(f"duplicated input {column!r} of {spec.name}")


def _check_params(spec: IndicatorSpec) -> None:
    _check_tuple(spec.params, ParamSpec, f"params of {spec.name}")
    names = tuple(param.name for param in spec.params)
    for position, param in enumerate(spec.params):
        _check_param(spec.name, param)
        if param.name in names[:position]:
            raise ValueError(f"duplicated parameter name {param.name!r} in {spec.name}")
    _check_tuple(spec.constraints, LessThan, f"constraints of {spec.name}")
    for constraint in spec.constraints:
        for side in (constraint.left, constraint.right):
            if not isinstance(side, str):
                raise TypeError(
                    f"constraint parameters of {spec.name} must be str, got {_type_name(side)}"
                )
            if side not in names:
                raise ValueError(
                    f"constraint of {spec.name} names an undeclared parameter {_echo(side)}"
                )


def _check_param(indicator: str, param: ParamSpec) -> None:
    _check_name(param.name, "parameter name", indicator)
    where = f"parameter {param.name!r} of {indicator}"
    if not isinstance(param.kind, ParamKind):
        raise TypeError(f"kind of {where} must be a ParamKind, got {_type_name(param.kind)}")
    _check_text(param.label, f"label of {where}")
    _check_text(param.description, f"description of {where}")
    integer = param.kind is ParamKind.INT
    for field, value in (
        ("default", param.default),
        ("minimum", param.minimum),
        ("maximum", param.maximum),
    ):
        if type(value) is not (int if integer else float):
            expected = "an int" if integer else "a float"
            raise TypeError(f"{field} of {where} must be {expected}, got {_type_name(value)}")
        if not math.isfinite(value):
            raise ValueError(f"{field} of {where} must be finite, got {value!r}")
    if param.minimum > param.maximum:
        raise ValueError(
            f"minimum {param.minimum!r} of {where} exceeds its maximum {param.maximum!r}"
        )
    if not param.minimum <= param.default <= param.maximum:
        raise ValueError(
            f"default {param.default!r} of {where} must be in "
            f"[{param.minimum!r}, {param.maximum!r}]"
        )


def _check_outputs(spec: IndicatorSpec) -> None:
    _check_tuple(spec.outputs, OutputSpec, f"outputs of {spec.name}")
    if not spec.outputs:
        raise ValueError(f"{spec.name} must have at least one output")
    for position, output in enumerate(spec.outputs):
        _check_name(output.name, "output name", spec.name)
        _check_text(output.label, f"label of output {output.name!r} of {spec.name}")
        if output.name in spec.output_names[:position]:
            raise ValueError(f"duplicated output name {output.name!r} in {spec.name}")


def _check_defaults(spec: IndicatorSpec) -> None:
    defaults = IndicatorParams({param.name: param.default for param in spec.params})
    for constraint in spec.constraints:
        if not defaults[constraint.left] < defaults[constraint.right]:
            raise ValueError(
                f"defaults of {spec.name} violate {constraint.left} < {constraint.right}"
            )
    for field, function in (("lookback", spec.lookback), ("settle", spec.settle)):
        count: object = function(defaults)
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"{field} of {spec.name} must return an int, got {_type_name(count)}")
        if count < 0:
            raise ValueError(
                f"{field} of {spec.name} at default parameters must be >= 0, got {count}"
            )
