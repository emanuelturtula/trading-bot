"""The assignment repository over a ``Session`` (spec 013, Design 7; D86).

``assign`` pre-checks both timeframes and raises ``TimeframeMismatchError`` with both codes:
the composite foreign keys refuse the row anyway, but an ``IntegrityError`` is a poor message
for a user and it would end the unit of work. Assigning twice is idempotent: the existing row
is returned with its original ``created_at``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.domain.utc import to_utc
from trading_bot.persistence.clock import Clock, system_clock
from trading_bot.persistence.errors import (
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownTickerError,
)
from trading_bot.persistence.models import RuleRow, TickerRow, TickerRuleRow
from trading_bot.persistence.records import Assignment, StoredRule, StoredTicker
from trading_bot.persistence.repositories.rules import load_rule_document
from trading_bot.persistence.repositories.tickers import to_record as ticker_record

__all__ = ["SqlAssignmentRepository"]


def to_record(row: TickerRuleRow) -> Assignment:
    """The frozen record of a row, with its instant normalized to UTC."""
    return Assignment(
        ticker_id=row.ticker_id,
        rule_id=row.rule_id,
        timeframe=row.timeframe,
        created_at=to_utc(row.created_at),
    )


class SqlAssignmentRepository:
    """``AssignmentRepository`` over one session; build one per unit of work."""

    def __init__(self, session: Session, *, clock: Clock = system_clock) -> None:
        self._session = session
        self._clock = clock

    def assign(self, ticker_id: int, rule_id: int) -> Assignment:
        ticker = self._session.get(TickerRow, ticker_id)
        if ticker is None:
            raise UnknownTickerError(ticker_id)
        rule = self._session.get(RuleRow, rule_id)
        if rule is None:
            raise UnknownRuleError(rule_id)
        if ticker.timeframe != rule.timeframe:
            raise TimeframeMismatchError(ticker.timeframe, rule.timeframe)
        existing = self._session.get(TickerRuleRow, (ticker_id, rule_id))
        if existing is not None:
            return to_record(existing)
        row = TickerRuleRow(
            ticker_id=ticker_id,
            rule_id=rule_id,
            timeframe=ticker.timeframe,
            created_at=to_utc(self._clock()),
        )
        self._session.add(row)
        self._session.flush()
        return to_record(row)

    def unassign(self, ticker_id: int, rule_id: int) -> bool:
        row = self._session.get(TickerRuleRow, (ticker_id, rule_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def list_all(self) -> tuple[Assignment, ...]:
        statement = select(TickerRuleRow).order_by(TickerRuleRow.ticker_id, TickerRuleRow.rule_id)
        return tuple(to_record(row) for row in self._session.execute(statement).scalars())

    def rules_for_ticker(
        self, ticker_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredRule, ...]:
        statement = (
            select(RuleRow)
            .join(TickerRuleRow, TickerRuleRow.rule_id == RuleRow.id)
            .where(TickerRuleRow.ticker_id == ticker_id)
            .order_by(RuleRow.name)
        )
        if enabled_only:
            statement = statement.where(RuleRow.enabled.is_(True))
        return tuple(
            StoredRule(
                id=row.id,
                rule=load_rule_document(row.id, row.definition_json),
                enabled=row.enabled,
                created_at=to_utc(row.created_at),
                updated_at=to_utc(row.updated_at),
            )
            for row in self._session.execute(statement).scalars()
        )

    def tickers_for_rule(
        self, rule_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredTicker, ...]:
        statement = (
            select(TickerRow)
            .join(TickerRuleRow, TickerRuleRow.ticker_id == TickerRow.id)
            .where(TickerRuleRow.rule_id == rule_id)
            .order_by(TickerRow.symbol, TickerRow.timeframe)
        )
        if enabled_only:
            statement = statement.where(TickerRow.enabled.is_(True))
        return tuple(ticker_record(row) for row in self._session.execute(statement).scalars())
