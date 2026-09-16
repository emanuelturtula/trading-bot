"""Errors of the indicator registry (spec 005, Design 3).

``IndicatorError`` subclasses ``ValueError``: it is always about user input (an indicator name,
a parameter or an output), so rule validators can turn it into a validation error.
``IndicatorComputationError`` is a ``RuntimeError`` because it is never the user's fault.

Messages are one English line built from registered names and bounded echoes of user input.
This module imports neither pandas nor numpy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "IndicatorComputationError",
    "IndicatorError",
    "IndicatorErrorKind",
    "InvalidOutputError",
    "InvalidParameterError",
    "UnknownIndicatorError",
]

_ECHO_LIMIT: Final = 32  # characters of rejected input echoed in error messages
_ECHO_WIDTH: Final = 64  # columns of that echo once rendered with repr()


class IndicatorErrorKind(StrEnum):
    """What a request to the indicator registry violates."""

    UNKNOWN_INDICATOR = "unknown_indicator"
    UNKNOWN_PARAMETER = "unknown_parameter"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    CONSTRAINT = "constraint"
    UNKNOWN_OUTPUT = "unknown_output"
    MISSING_OUTPUT = "missing_output"


class IndicatorError(ValueError):
    """An invalid indicator name, parameter or output. ``str()`` is the one-line message."""

    kind: IndicatorErrorKind
    indicator: str | None  # requested indicator name, at most its first 32 characters
    parameter: str | None  # offending parameter name, at most its first 32 characters
    output: str | None  # offending output name, at most its first 32 characters

    def __init__(
        self,
        kind: IndicatorErrorKind,
        message: str,
        *,
        indicator: str | None = None,
        parameter: str | None = None,
        output: str | None = None,
    ) -> None:
        # args holds only the positional arguments, so the default exception pickling
        # (``cls(*args)`` plus ``__dict__``) restores every field.
        super().__init__(kind, message)
        self.kind = kind
        self.indicator = _truncated(indicator)
        self.parameter = _truncated(parameter)
        self.output = _truncated(output)
        self._message = message

    def __str__(self) -> str:
        return self._message


class UnknownIndicatorError(IndicatorError):
    """The indicator name is not in the registry (kind ``unknown_indicator``)."""


class InvalidParameterError(IndicatorError):
    """A parameter is undeclared, of the wrong type, out of range or violates a constraint."""


class InvalidOutputError(IndicatorError):
    """The output name is unknown, or required because the indicator has several outputs."""


class IndicatorComputationError(RuntimeError):
    """The computation cannot give trustworthy values (a broken kernel or TA-Lib settings)."""


def _truncated(name: str | None) -> str | None:
    return None if name is None else name[:_ECHO_LIMIT]


def _echo(text: str) -> str:
    """``repr()`` of the longest prefix of ``text`` (at most 32 characters) that fits 64 columns.

    Escapes can make ``repr()`` up to 10 times longer than its input, so the prefix shrinks
    until the rendering is short enough: messages that echo user input stay single-line and
    bounded.
    """
    prefix = str.__str__(text)[:_ECHO_LIMIT]
    while len(rendered := repr(prefix)) > _ECHO_WIDTH:
        prefix = prefix[:-1]
    return rendered


def _type_name(value: object) -> str:
    """The name of ``value``'s type, echoed when it is not a short identifier."""
    name = type(value).__name__
    return name if name.isidentifier() and len(name) <= _ECHO_LIMIT else _echo(name)
