"""The configuration file: envelope, caps, reader and writer (spec 013, Design 9.3, 9.4).

One format, read and written by one command pair (decision D102). Every section is optional and
an **absent** section means "leave it alone", so a rules-only file — which is what
``docs/examples/rules/`` ships — is a valid whole-configuration file.

The file is operator-supplied input: it is bounded before parsing, no database identifier
appears anywhere (decision D103), and ``parse_rule`` is the only way a rule is built from it.
There is no ``eval`` and no dynamic import.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from trading_bot.domain.rules.errors import RuleValidationError
from trading_bot.domain.rules.schema import Rule, dump_rule, parse_rule
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe, UnknownTimeframeError

__all__ = [
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ITEMS",
    "AssignmentItem",
    "Configuration",
    "ConfigurationFileError",
    "RuleItem",
    "TickerItem",
    "read_configuration",
    "render_configuration",
]

FORMAT_VERSION: Final = 1
MAX_IMPORT_BYTES: Final = 1_048_576
MAX_IMPORT_ITEMS: Final = 500  # over the three sections together

_ENVELOPE_KEYS: Final = ("version", "tickers", "rules", "assignments")
_TICKER_KEYS: Final = ("symbol", "timeframe", "enabled")
_RULE_KEYS: Final = ("enabled", "rule")
_ASSIGNMENT_KEYS: Final = ("symbol", "timeframe", "rule")
_DEFAULT_TIMEFRAME: Final = Timeframe.D1


class ConfigurationFileError(Exception):
    """A rejected file. ``path`` locates the item, for example ``assignments[2].rule``."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class TickerItem:
    """A ticker of the file, addressed by ``(symbol, timeframe)``."""

    symbol: str
    timeframe: Timeframe
    enabled: bool


@dataclass(frozen=True, slots=True)
class RuleItem:
    """A rule of the file: the parsed document plus the flag the document cannot carry."""

    enabled: bool
    rule: Rule


@dataclass(frozen=True, slots=True)
class AssignmentItem:
    """An assignment of the file, addressed by natural key (decision D103)."""

    symbol: str
    timeframe: Timeframe
    rule: str


@dataclass(frozen=True, slots=True)
class Configuration:
    """A validated file. An empty section and an absent one are the same: nothing to apply."""

    tickers: tuple[TickerItem, ...] = ()
    rules: tuple[RuleItem, ...] = ()
    assignments: tuple[AssignmentItem, ...] = ()


def read_configuration(payload: bytes) -> Configuration:
    """Validate ``payload`` and return its items, or raise ``ConfigurationFileError``.

    The caps are applied before parsing: spec 006's 64 KiB cap protects a rule document only
    when it is parsed from text, and this reader hands ``parse_rule`` an already decoded
    mapping.
    """
    if len(payload) > MAX_IMPORT_BYTES:
        raise ConfigurationFileError(f"the file is larger than {MAX_IMPORT_BYTES} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationFileError("the file is not valid UTF-8") from None
    try:
        document = json.loads(text)
    except ValueError:
        raise ConfigurationFileError("the file is not valid JSON") from None
    if not isinstance(document, dict):
        raise ConfigurationFileError("the file must hold a JSON object")
    _reject_unknown_keys(document, _ENVELOPE_KEYS, path="")
    if document.get("version") != FORMAT_VERSION:
        raise ConfigurationFileError(f"version must be {FORMAT_VERSION}", path="version")

    ticker_items = _section(document, "tickers")
    rule_items = _section(document, "rules")
    assignment_items = _section(document, "assignments")
    # Both caps are applied before anything is parsed, so an oversized file costs no parsing.
    if len(ticker_items) + len(rule_items) + len(assignment_items) > MAX_IMPORT_ITEMS:
        raise ConfigurationFileError(f"the file holds more than {MAX_IMPORT_ITEMS} items")

    configuration = Configuration(
        tickers=tuple(
            _read_ticker(item, f"tickers[{index}]") for index, item in enumerate(ticker_items)
        ),
        rules=tuple(_read_rule(item, f"rules[{index}]") for index, item in enumerate(rule_items)),
        assignments=tuple(
            _read_assignment(item, f"assignments[{index}]")
            for index, item in enumerate(assignment_items)
        ),
    )
    _reject_duplicates(configuration)
    return configuration


def render_configuration(
    tickers: Sequence[TickerItem],
    rules: Sequence[RuleItem],
    assignments: Sequence[AssignmentItem],
) -> str:
    """The file text of a whole configuration: sorted, UTF-8, with a trailing newline.

    Only the sections that have items are written, so an empty database exports the version
    alone and a rules-only export stays a valid rules-only file.
    """
    document: dict[str, object] = {"version": FORMAT_VERSION}
    if tickers:
        document["tickers"] = [
            {"symbol": item.symbol, "timeframe": item.timeframe.value, "enabled": item.enabled}
            for item in sorted(tickers, key=lambda item: (item.symbol, item.timeframe.value))
        ]
    if rules:
        document["rules"] = [
            {"enabled": item.enabled, "rule": dump_rule(item.rule)}
            for item in sorted(rules, key=lambda item: item.rule.name)
        ]
    if assignments:
        document["assignments"] = [
            {"symbol": item.symbol, "timeframe": item.timeframe.value, "rule": item.rule}
            for item in sorted(
                assignments, key=lambda item: (item.symbol, item.timeframe.value, item.rule)
            )
        ]
    return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _section(document: dict[str, object], name: str) -> list[object]:
    value = document.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigurationFileError(f"{name} must be a list", path=name)
    return value


def _mapping(item: object, path: str, keys: Sequence[str]) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ConfigurationFileError("an item must be a JSON object", path=path)
    _reject_unknown_keys(item, keys, path=path)
    return item


def _reject_unknown_keys(document: dict[str, object], keys: Sequence[str], *, path: str) -> None:
    unknown = sorted(key for key in document if key not in keys)
    if unknown:
        raise ConfigurationFileError(
            f"unknown key {unknown[0]!r}; expected one of {', '.join(keys)}", path=path
        )


def _read_ticker(item: object, path: str) -> TickerItem:
    data = _mapping(item, path, _TICKER_KEYS)
    return TickerItem(
        symbol=_symbol(data, path),
        timeframe=_timeframe(data, path),
        enabled=_flag(data, path, "enabled", default=True),
    )


def _read_rule(item: object, path: str) -> RuleItem:
    data = _mapping(item, path, _RULE_KEYS)
    document = data.get("rule")
    if not isinstance(document, dict):
        raise ConfigurationFileError("rule must be a JSON object", path=f"{path}.rule")
    try:
        rule = parse_rule(document)
    except RuleValidationError as error:
        # The problems are already bounded and escaped by spec 006; the document is not echoed.
        raise ConfigurationFileError(str(error), path=f"{path}.rule") from None
    return RuleItem(enabled=_flag(data, path, "enabled", default=False), rule=rule)


def _read_assignment(item: object, path: str) -> AssignmentItem:
    data = _mapping(item, path, _ASSIGNMENT_KEYS)
    name = data.get("rule")
    if not isinstance(name, str):
        raise ConfigurationFileError("rule must be a rule name", path=f"{path}.rule")
    return AssignmentItem(symbol=_symbol(data, path), timeframe=_timeframe(data, path), rule=name)


def _symbol(data: dict[str, object], path: str) -> str:
    value = data.get("symbol")
    if not isinstance(value, str):
        raise ConfigurationFileError("symbol must be a string", path=f"{path}.symbol")
    try:
        return normalize_ticker(value)
    except ValueError as error:
        raise ConfigurationFileError(str(error), path=f"{path}.symbol") from None


def _timeframe(data: dict[str, object], path: str) -> Timeframe:
    value = data.get("timeframe")
    if value is None:
        return _DEFAULT_TIMEFRAME
    if not isinstance(value, str):
        raise ConfigurationFileError("timeframe must be a string", path=f"{path}.timeframe")
    try:
        return Timeframe.parse(value)
    except UnknownTimeframeError as error:
        raise ConfigurationFileError(str(error), path=f"{path}.timeframe") from None


def _flag(data: dict[str, object], path: str, key: str, *, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigurationFileError(f"{key} must be true or false", path=f"{path}.{key}")
    return value


def _reject_duplicates(configuration: Configuration) -> None:
    seen_tickers: set[tuple[str, Timeframe]] = set()
    for index, ticker in enumerate(configuration.tickers):
        key = (ticker.symbol, ticker.timeframe)
        if key in seen_tickers:
            raise ConfigurationFileError(
                f"{ticker.symbol} {ticker.timeframe.value} appears twice",
                path=f"tickers[{index}]",
            )
        seen_tickers.add(key)
    seen_rules: set[str] = set()
    for index, rule in enumerate(configuration.rules):
        if rule.rule.name in seen_rules:
            raise ConfigurationFileError(
                "a rule with that name appears twice", path=f"rules[{index}]"
            )
        seen_rules.add(rule.rule.name)
    seen_assignments: set[tuple[str, Timeframe, str]] = set()
    for index, assignment in enumerate(configuration.assignments):
        triple = (assignment.symbol, assignment.timeframe, assignment.rule)
        if triple in seen_assignments:
            raise ConfigurationFileError(
                "that assignment appears twice", path=f"assignments[{index}]"
            )
        seen_assignments.add(triple)
