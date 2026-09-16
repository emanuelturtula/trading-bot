"""Errors of the rule schema: kinds, problems and their bounds (spec 006, Design 8).

``RuleValidationError`` subclasses ``ValueError``: it is always about user input (a rule document
sent from the dashboard, the API or an import file). It carries one ``RuleProblem`` per rejected
field, each with a ``RuleErrorKind``, a dotted path and a one-line English message, so the API
(#22) can answer with field-level errors and the CLI (#12) can report them.

Every message and every path is sanitized and bounded on construction: rule documents are
attacker-controlled and these strings end up in logs, Telegram messages and HTTP responses.
Raw input is never copied in; user text only appears as a ``repr()``-escaped, truncated echo.

This module imports neither pydantic, nor the indicator catalog, nor pandas.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_MESSAGE_LENGTH",
    "MAX_PATH_LENGTH",
    "MAX_PATH_SEGMENT_LENGTH",
    "MAX_PROBLEMS",
    "RuleErrorKind",
    "RuleProblem",
    "RuleValidationError",
]

MAX_PROBLEMS: Final = 20  # problems kept in one error
MAX_PATH_SEGMENT_LENGTH: Final = 32  # columns of one rendered path segment
MAX_PATH_LENGTH: Final = 160  # columns of a whole path
MAX_MESSAGE_LENGTH: Final = 200  # columns of one problem message

_ECHO_LIMIT: Final = 32  # characters of rejected input echoed in messages and paths
_ECHO_WIDTH: Final = 64  # columns of that echo once rendered with repr()
_ELLIPSIS: Final = "..."


class RuleErrorKind(StrEnum):
    """What a rule document violates. Stable vocabulary for the API, the CLI and the dashboard."""

    INVALID_JSON = "invalid_json"
    TOO_LARGE = "too_large"
    TOO_DEEP = "too_deep"
    DUPLICATE_KEY = "duplicate_key"
    NOT_AN_OBJECT = "not_an_object"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    UNKNOWN_VALUE = "unknown_value"  # an enum: operator, price column, signal, timeframe
    INVALID_OPERAND = "invalid_operand"  # not exactly one of indicator, price, value
    INVALID_GROUP = "invalid_group"  # not exactly one of all, any
    NESTED_TOO_DEEP = "nested_too_deep"
    TOO_MANY_CONDITIONS = "too_many_conditions"
    DEGENERATE_CONDITION = "degenerate_condition"
    INVALID_NAME = "invalid_name"
    UNKNOWN_INDICATOR = "unknown_indicator"
    INVALID_PARAMETER = "invalid_parameter"
    INVALID_OUTPUT = "invalid_output"
    INVALID_RULE = "invalid_rule"  # catch-all, including an unmapped pydantic error type


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleProblem:
    """One rejected field. ``path`` is ``""`` for the whole document.

    ``path`` and ``message`` are sanitized and truncated on construction, so a problem built
    anywhere in the code base is safe to log or display.
    """

    kind: RuleErrorKind
    path: str  # "" or "conditions.all[1].left.params.length"
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _bounded_path(self.path))
        object.__setattr__(self, "message", _bounded_message(self.message))

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class RuleValidationError(ValueError):
    """An invalid rule document. ``str()`` is one line: the first problem and how many follow."""

    kind: RuleErrorKind  # kind of the first problem
    path: str  # path of the first problem
    problems: tuple[RuleProblem, ...]  # document order, at most MAX_PROBLEMS

    def __init__(self, problems: Iterable[RuleProblem]) -> None:
        collected = tuple(problems)[:MAX_PROBLEMS]
        if not collected:
            raise ValueError("a RuleValidationError needs at least one problem")
        # args holds only the positional argument, so the default exception copying and pickling
        # (``cls(*args)``) restores every field.
        super().__init__(collected)
        self.problems = collected
        self.kind = collected[0].kind
        self.path = collected[0].path
        self._message = _summary(collected)

    def __str__(self) -> str:
        return self._message

    @classmethod
    def single(cls, kind: RuleErrorKind, message: str, *, path: str = "") -> RuleValidationError:
        """An error with one problem, for failures found before or outside field validation."""
        return cls((RuleProblem(kind=kind, path=path, message=message),))


def _summary(problems: tuple[RuleProblem, ...]) -> str:
    text = str(problems[0])
    remaining = len(problems) - 1
    if remaining == 0:
        return text
    noun = "problem" if remaining == 1 else "problems"
    return f"{text} (and {remaining} more {noun})"


def _sanitized(text: str) -> str:
    """``text`` with every non-printable character replaced by a space (never multi-line)."""
    return "".join(character if character.isprintable() else " " for character in text)


def _bounded_message(message: str) -> str:
    sanitized = _sanitized(message)
    if len(sanitized) <= MAX_MESSAGE_LENGTH:
        return sanitized
    return sanitized[: MAX_MESSAGE_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS


def _bounded_path(path: str) -> str:
    """``path`` sanitized and cut at a segment boundary when it is too long."""
    sanitized = _sanitized(path)
    if len(sanitized) <= MAX_PATH_LENGTH:
        return sanitized
    cut = sanitized[:MAX_PATH_LENGTH]
    boundary = max(cut.rfind("."), cut.rfind("["))
    return cut[:boundary] if boundary > 0 else cut


def _echo(text: str, width: int = _ECHO_WIDTH) -> str:
    """``repr()`` of the longest prefix of ``text`` (at most 32 characters) that fits ``width``.

    Escapes can make ``repr()`` up to 10 times longer than its input, so the prefix shrinks
    until the rendering is short enough: messages and paths that echo user input stay
    single-line and bounded.
    """
    prefix = str.__str__(text)[:_ECHO_LIMIT]
    while len(rendered := repr(prefix)) > width:
        prefix = prefix[:-1]
    return rendered


def _segment(text: str) -> str:
    """A path segment: a plain identifier as it is, anything else as a bounded echo."""
    if text.isascii() and text.isidentifier() and len(text) <= MAX_PATH_SEGMENT_LENGTH:
        return text
    return _echo(text, MAX_PATH_SEGMENT_LENGTH)


def _build_path(loc: Sequence[object], *, suffix: str | None = None) -> str:
    """The dotted path of a pydantic ``loc``, with ``suffix`` appended as a last segment.

    Integers become ``[i]``, strings become ``.key`` and the tags of the discriminated unions
    are dropped: they always carry a dot by construction (``operand.indicator``), so they can
    never be confused with a JSON key.
    """
    parts: list[str] = []
    for part in loc:
        if isinstance(part, str):
            if "." not in part:
                parts.append(f".{_segment(part)}")
        elif isinstance(part, int):
            parts.append(f"[{part}]")
        else:  # pydantic reports a non-string mapping key as the loc itself
            parts.append(f".{_echo(str(part), MAX_PATH_SEGMENT_LENGTH)}")
    if suffix is not None:
        parts.append(f".{_segment(suffix)}")
    return _joined(parts)


def _joined(parts: Sequence[str]) -> str:
    path = ""
    for part in parts:
        if len(path) + len(part) > MAX_PATH_LENGTH:
            break
        path += part
    return path.removeprefix(".")
