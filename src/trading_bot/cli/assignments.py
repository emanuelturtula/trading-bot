"""The assignment subcommands (spec 013, §9.6; AC35, AC37).

A timeframe mismatch is turned into a readable line before the write: decision D86 makes the
row impossible in the database, but an ``IntegrityError`` traceback is not an answer for an
operator. Assigning twice is reported ``unchanged`` and exits ``0`` (decision D105).
"""

from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

from trading_bot.cli.main import (
    EXIT_OK,
    Action,
    Context,
    RejectedError,
    SubParsers,
    dry_run_option,
    quoted,
    timeframe_option,
    unit_of_work,
)
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

__all__ = ["add_parser", "mismatch_message"]


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("assignments", help="manage which rule runs on which ticker")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    listing = commands.add_parser("list", help="list every assignment")
    listing.set_defaults(handler=list_assignments)

    for name, handler in (("add", add_assignment), ("remove", remove_assignment)):
        command = commands.add_parser(name, help=f"{name} an assignment")
        command.add_argument("symbol")
        command.add_argument("rule_name", metavar="rule-name", help="the unique rule name")
        timeframe_option(command)
        dry_run_option(command)
        command.set_defaults(handler=handler)


def subject(symbol: str, timeframe: Timeframe, rule_name: str) -> str:
    return f"assignment {symbol} {timeframe.value} -> {quoted(rule_name)}"


def mismatch_message(
    rule_name: str, rule_timeframe: Timeframe, symbol: str, ticker: Timeframe
) -> str:
    """The line of spec 013 §9.6, shared with ``config import``."""
    return (
        f"rule {quoted(rule_name)} is evaluated on {rule_timeframe.value},"
        f" but ticker {symbol} is tracked on {ticker.value}"
    )


def list_assignments(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        tickers = {row.id: row for row in SqlTickerRepository(session).list_all()}
        rules = {row.id: row for row in SqlRuleRepository(session).list_all()}
        assignments = SqlAssignmentRepository(session).list_all()
        lines = sorted(
            (
                tickers[row.ticker_id].symbol,
                row.timeframe.value,
                rules[row.rule_id].name,
            )
            for row in assignments
        )
        for symbol, timeframe, name in lines:
            context.emit(f"{symbol} {timeframe} -> {quoted(name)}")
    return EXIT_OK


def add_assignment(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        ticker, rule = _both_ends(session, args)
        if rule.rule.timeframe != ticker.timeframe:
            raise RejectedError(
                mismatch_message(rule.name, rule.rule.timeframe, ticker.symbol, ticker.timeframe)
            )
        assigned = {stored.id for stored in assignments.rules_for_ticker(ticker.id)}
        action = Action.UNCHANGED if rule.id in assigned else Action.CREATE
        assignments.assign(ticker.id, rule.id)
        context.report(action, subject(ticker.symbol, ticker.timeframe, rule.name))
    return context.finish()


def remove_assignment(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        ticker, rule = _both_ends(session, args)
        if not assignments.unassign(ticker.id, rule.id):
            raise RejectedError(
                f"no {subject(ticker.symbol, ticker.timeframe, rule.name)} is stored"
            )
        context.report(Action.REMOVE, subject(ticker.symbol, ticker.timeframe, rule.name))
    return context.finish()


def _both_ends(session: Session, args: argparse.Namespace) -> tuple[StoredTicker, StoredRule]:
    """Resolve both ends, naming the one that is missing, never both at once (§9.6)."""
    symbol = normalize_ticker(args.symbol)
    timeframe: Timeframe = args.timeframe
    name: str = args.rule_name
    ticker = SqlTickerRepository(session).get_by_symbol(symbol, timeframe)
    if ticker is None:
        raise RejectedError(f"no ticker {symbol} {timeframe.value} is stored")
    rule = SqlRuleRepository(session).get_by_name(name)
    if rule is None:
        raise RejectedError(f"no rule named {quoted(name)} is stored")
    return ticker, rule
