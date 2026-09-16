"""Indicator parameter declarations and their validation (spec 005, Design 4).

Parameters are strict: integers must be integral (``14.0`` is rejected), floats accept integers,
booleans are always rejected, undeclared names are rejected and missing names take their
defaults. Validated parameters are an ``IndicatorParams``: read-only, hashable and in
declaration order, so ``(name, params)`` can key a cache.

This module imports neither pandas nor numpy: numpy scalars register with ``numbers``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from numbers import Integral, Real
from typing import Final

from trading_bot.domain.indicators.errors import (
    IndicatorErrorKind,
    InvalidParameterError,
    _echo,
    _type_name,
)

__all__ = ["IndicatorParams", "LessThan", "ParamKind", "ParamSpec", "ParamValue", "validate_params"]

type ParamValue = int | float

_MAX_RENDERED_BITS: Final = 96  # larger integers (about 29 digits) are not echoed


class ParamKind(StrEnum):
    """The type of a parameter value."""

    INT = "int"
    FLOAT = "float"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParamSpec:
    """A declared parameter. ``IndicatorSpec`` checks the declaration (spec 005, AC14)."""

    name: str
    kind: ParamKind
    default: ParamValue
    minimum: ParamValue
    maximum: ParamValue
    label: str
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LessThan:
    """Constraint: the value of parameter ``left`` must be less than that of ``right``."""

    left: str
    right: str


class IndicatorParams(Mapping[str, ParamValue]):
    """Read-only, hashable, picklable mapping of validated parameters in declaration order.

    Equality with any mapping (including ``dict``) and hashing ignore the order.
    """

    __slots__ = ("_items",)

    _items: dict[str, ParamValue]

    def __init__(self, values: Mapping[str, ParamValue] | None = None) -> None:
        items: dict[str, ParamValue] = {}
        if values is not None:
            if not isinstance(values, Mapping):
                raise TypeError(f"parameters must be a mapping, got {_type_name(values)}")
            for name, value in values.items():
                if not isinstance(name, str):
                    raise TypeError(f"parameter names must be str, got {_type_name(name)}")
                items[str.__str__(name)] = _builtin_number(name, value)
        object.__setattr__(self, "_items", items)

    def __getitem__(self, key: str) -> ParamValue:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        # Consistent with Mapping equality, which ignores insertion order.
        return hash(frozenset(self._items.items()))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is read-only")

    def __reduce__(self) -> tuple[type[IndicatorParams], tuple[dict[str, ParamValue]]]:
        # Rebuilt through __init__, so restored copies are checked and read-only too.
        return (type(self), (dict(self._items),))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._items!r})"

    def integer(self, name: str) -> int:
        """The integer parameter ``name``: ``KeyError`` if absent, ``TypeError`` if a float."""
        value = self._items[name]
        if not isinstance(value, int):
            raise TypeError(f"parameter {_echo(name)} is not an integer")
        return value

    def real(self, name: str) -> float:
        """The parameter ``name`` as a ``float``, integers converted; ``KeyError`` if absent."""
        return float(self._items[name])


def validate_params(
    indicator: str,
    specs: Sequence[ParamSpec],
    constraints: Sequence[LessThan],
    params: Mapping[str, object],
) -> IndicatorParams:
    """Validate ``params`` of ``indicator`` against its declared ``specs`` and ``constraints``.

    Checks run in this order and the first failure raises ``InvalidParameterError``: undeclared
    names (in the mapping's iteration order), then each declared parameter in declaration order
    (type, then range), then the constraints. A non-``Mapping`` ``params`` or a non-``str`` name
    raises ``TypeError``. Validating the result again returns an equal value.
    """
    if not isinstance(params, Mapping):
        raise TypeError(f"parameters of {indicator} must be a mapping, got {_type_name(params)}")
    declared = tuple(spec.name for spec in specs)
    provided: dict[str, object] = {}
    for key, value in params.items():
        if not isinstance(key, str):
            raise TypeError(f"parameter names of {indicator} must be str, got {_type_name(key)}")
        name = str.__str__(key)
        if name not in declared:
            expected = (
                f"expected one of {', '.join(declared)}"
                if declared
                else f"{indicator} takes no parameters"
            )
            raise InvalidParameterError(
                IndicatorErrorKind.UNKNOWN_PARAMETER,
                f"unknown parameter {_echo(name)} of {indicator}; {expected}",
                indicator=indicator,
                parameter=name,
            )
        provided[name] = value
    values: dict[str, ParamValue] = {}
    for spec in specs:
        values[spec.name] = _checked_value(indicator, spec, provided.get(spec.name, spec.default))
    for constraint in constraints:
        left, right = values[constraint.left], values[constraint.right]
        if not left < right:
            raise InvalidParameterError(
                IndicatorErrorKind.CONSTRAINT,
                f"parameters of {indicator} must satisfy {constraint.left} < {constraint.right}, "
                f"got {constraint.left}={_render(left)}, {constraint.right}={_render(right)}",
                indicator=indicator,
                parameter=constraint.left,
            )
    return IndicatorParams(values)


def _checked_value(indicator: str, spec: ParamSpec, value: object) -> ParamValue:
    """``value`` as the built-in type of ``spec.kind`` if its type and range are valid."""
    number: ParamValue
    if spec.kind is ParamKind.INT:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise _wrong_type(indicator, spec, "an integer", value)
        number = int(value)
    else:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise _wrong_type(indicator, spec, "a number", value)
        try:
            number = float(value)
        except OverflowError:
            raise _out_of_range(indicator, spec, "a number too large for a float") from None
    if not spec.minimum <= number <= spec.maximum:
        raise _out_of_range(indicator, spec, _render(number))
    return number


def _wrong_type(
    indicator: str, spec: ParamSpec, expected: str, value: object
) -> InvalidParameterError:
    return InvalidParameterError(
        IndicatorErrorKind.WRONG_TYPE,
        f"parameter {spec.name!r} of {indicator} must be {expected}, got {_render(value)}",
        indicator=indicator,
        parameter=spec.name,
    )


def _out_of_range(indicator: str, spec: ParamSpec, got: str) -> InvalidParameterError:
    return InvalidParameterError(
        IndicatorErrorKind.OUT_OF_RANGE,
        f"parameter {spec.name!r} of {indicator} must be in "
        f"[{_render(spec.minimum)}, {_render(spec.maximum)}], got {got}",
        indicator=indicator,
        parameter=spec.name,
    )


def _render(value: object) -> str:
    """A bounded, single-line rendering of a parameter value for error messages."""
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        if value.bit_length() > _MAX_RENDERED_BITS:
            return "an integer too large to show"
        return int.__repr__(value)
    if isinstance(value, float):
        return float.__repr__(value)
    if isinstance(value, str):
        return _echo(value)
    return f"a value of type {_type_name(value)}"


def _builtin_number(name: str, value: object) -> ParamValue:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(
            f"parameter {_echo(name)} must be an int or a float, got {_type_name(value)}"
        )
    return int(value) if isinstance(value, Integral) else float(value)
