"""The rule repository and the stored-document codec (spec 013, Design 7, 8; D87, D90, D93).

This is the one module of ``persistence/`` allowed to import ``trading_bot.domain.rules``
(decision D99): parsing a rule loads pydantic and the indicator catalog, a cost that is
deliberate and confined here, so ``Base``, the engine and the models stay importable by the
Telegram and API layers without the analysis stack.

An invalid rule is never persisted: the document is serialized with ``dump_rule`` and re-parsed
before the row is flushed, and a mismatch raises ``StoredRuleError`` without writing anything.
"""

from __future__ import annotations

import json

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from trading_bot.domain.rules.errors import RuleErrorKind, RuleProblem, RuleValidationError
from trading_bot.domain.rules.schema import Rule, dump_rule, parse_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import (
    AssignedTimeframeError,
    DuplicateRuleNameError,
    StoredRuleError,
    UnknownRuleError,
)
from trading_bot.persistence.models import RuleRow, TickerRuleRow
from trading_bot.persistence.records import StoredRule

__all__ = ["SqlRuleRepository", "dump_rule_document", "load_rule_document"]

# Raised when a rule does not survive its own canonical serialization, which would mean the
# stored document and the rule the caller believes it stored have drifted apart.
_ROUND_TRIP_PROBLEM = RuleProblem(
    kind=RuleErrorKind.INVALID_RULE,
    path="",
    message="the canonical document does not re-parse to the rule it was built from",
)


def dump_rule_document(rule: Rule) -> str:
    """The canonical text stored in ``rules.definition_json`` (spec 006, decision D87).

    ``ensure_ascii=False`` keeps an accented name readable in a manual ``sqlite3`` session and
    ``allow_nan=False`` guarantees the text is standard JSON.
    """
    return json.dumps(dump_rule(rule), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def load_rule_document(rule_id: int | None, document: str) -> Rule:
    """Parse a stored document, raising ``StoredRuleError`` when it is no longer valid.

    The validation error is never chained: its rendering would echo the document, which must
    not reach a traceback or a log record (decision D93).
    """
    try:
        return parse_rule(document)
    except RuleValidationError as error:
        raise StoredRuleError(rule_id, error.problems) from None


class SqlRuleRepository:
    """``RuleRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def add(self, rule: Rule, *, enabled: bool = False) -> StoredRule:
        document = self._serialized(rule)
        if self._row_by_name(rule.name) is not None:
            raise DuplicateRuleNameError(rule.name)
        now = to_utc(self._clock())
        row = RuleRow(
            name=rule.name,
            signal=rule.signal,
            timeframe=rule.timeframe,
            definition_json=document,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _to_record(row, rule)

    def get(self, rule_id: int) -> StoredRule | None:
        row = self._session.get(RuleRow, rule_id)
        return None if row is None else _to_record(row)

    def get_by_name(self, name: str) -> StoredRule | None:
        row = self._row_by_name(name)
        return None if row is None else _to_record(row)

    def list_all(self) -> tuple[StoredRule, ...]:
        return self._listed(select(RuleRow))

    def list_enabled(self, timeframe: Timeframe | None = None) -> tuple[StoredRule, ...]:
        statement = select(RuleRow).where(RuleRow.enabled.is_(True))
        if timeframe is not None:
            statement = statement.where(RuleRow.timeframe == timeframe)
        return self._listed(statement)

    def replace(self, rule_id: int, rule: Rule, *, enabled: bool | None = None) -> StoredRule:
        row = self._session.get(RuleRow, rule_id)
        if row is None:
            raise UnknownRuleError(rule_id)
        document = self._serialized(rule, rule_id)
        if rule.timeframe != row.timeframe:
            assigned = self._assignment_count(rule_id)
            if assigned:
                raise AssignedTimeframeError(rule_id, assigned, row.timeframe, rule.timeframe)
        if rule.name != row.name and self._row_by_name(rule.name) is not None:
            raise DuplicateRuleNameError(rule.name)
        flag_moves = enabled is not None and row.enabled != enabled
        if row.definition_json != document or flag_moves:
            row.name = rule.name
            row.signal = rule.signal
            row.timeframe = rule.timeframe
            row.definition_json = document
            if enabled is not None:
                row.enabled = enabled
            row.updated_at = to_utc(self._clock())
            self._session.flush()
        return _to_record(row, rule)

    def set_enabled(self, rule_id: int, enabled: bool) -> StoredRule:
        row = self._session.get(RuleRow, rule_id)
        if row is None:
            raise UnknownRuleError(rule_id)
        if row.enabled != enabled:
            row.enabled = enabled
            row.updated_at = to_utc(self._clock())
            self._session.flush()
        return _to_record(row)

    def delete(self, rule_id: int) -> bool:
        row = self._session.get(RuleRow, rule_id)
        if row is None:
            return False
        # The assignments go with it through the database cascade (D86).
        self._session.delete(row)
        self._session.flush()
        return True

    def _serialized(self, rule: Rule, rule_id: int | None = None) -> str:
        """The canonical document, proven to re-parse to the same rule (AC12).

        The round trip costs about a millisecond on a write path used a handful of times a
        day and turns "an invalid rule is never persisted" into a runtime check.
        """
        document = dump_rule_document(rule)
        if load_rule_document(rule_id, document) != rule:
            raise StoredRuleError(rule_id, (_ROUND_TRIP_PROBLEM,))
        return document

    def _row_by_name(self, name: str) -> RuleRow | None:
        return self._session.execute(
            select(RuleRow).where(RuleRow.name == name)
        ).scalar_one_or_none()

    def _assignment_count(self, rule_id: int) -> int:
        statement = (
            select(func.count()).select_from(TickerRuleRow).where(TickerRuleRow.rule_id == rule_id)
        )
        return int(self._session.execute(statement).scalar_one())

    def _listed(self, statement: Select[tuple[RuleRow]]) -> tuple[StoredRule, ...]:
        ordered = statement.order_by(RuleRow.name)
        return tuple(_to_record(row) for row in self._session.execute(ordered).scalars())


def _to_record(row: RuleRow, rule: Rule | None = None) -> StoredRule:
    """The frozen record of a row; ``rule`` is the parsed document when the caller has it."""
    return StoredRule(
        id=row.id,
        rule=rule if rule is not None else load_rule_document(row.id, row.definition_json),
        enabled=row.enabled,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )
