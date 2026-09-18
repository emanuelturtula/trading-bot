"""The ticker subcommands (spec 013, §9.5; AC33, AC36, AC37).

A symbol is normalized by the domain, ``--timeframe`` defaults to ``1d``, and removing a ticker
that has assignments needs ``--force``: the schema cascade is right, but a command that
silently removes rows the operator did not name is not (decision D108).
"""

from __future__ import annotations

import argparse

from trading_bot.cli.main import (
    EXIT_OK,
    Action,
    Context,
    RejectedError,
    SubParsers,
    dry_run_option,
    plural,
    timeframe_option,
    unit_of_work,
)
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.errors import DuplicateTickerError
from trading_bot.persistence.records import StoredTicker
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

__all__ = ["add_parser"]


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("tickers", help="manage the symbols the bot watches")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    listing = commands.add_parser("list", help="list every ticker")
    listing.set_defaults(handler=list_tickers)

    adding = commands.add_parser("add", help="watch a new symbol")
    adding.add_argument("symbol", help="the symbol, for example AAPL")
    timeframe_option(adding)
    adding.add_argument("--disabled", action="store_true", help="add it without enabling it")
    dry_run_option(adding)
    adding.set_defaults(handler=add_ticker)

    removing = commands.add_parser("remove", help="stop watching a symbol")
    removing.add_argument("symbol")
    timeframe_option(removing)
    removing.add_argument(
        "--force", action="store_true", help="remove it together with its assignments"
    )
    dry_run_option(removing)
    removing.set_defaults(handler=remove_ticker)

    for name, enabled in (("enable", True), ("disable", False)):
        command = commands.add_parser(name, help=f"{name} a ticker")
        command.add_argument("symbol")
        timeframe_option(command)
        dry_run_option(command)
        command.set_defaults(handler=enable_ticker if enabled else disable_ticker)


def subject(symbol: str, timeframe: Timeframe, enabled: bool | None = None) -> str:
    text = f"ticker {symbol} {timeframe.value}"
    if enabled is not None:
        text += f" ({'enabled' if enabled else 'disabled'})"
    return text


def list_tickers(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        tickers = SqlTickerRepository(session, clock=context.clock)
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        for ticker in tickers.list_all():
            assigned = len(assignments.rules_for_ticker(ticker.id))
            state = "enabled" if ticker.enabled else "disabled"
            context.emit(
                f"{ticker.symbol} {ticker.timeframe.value} {state} {plural(assigned, 'rule')}"
            )
    return EXIT_OK


def add_ticker(context: Context, args: argparse.Namespace) -> int:
    symbol: str = args.symbol
    timeframe: Timeframe = args.timeframe
    with unit_of_work(context) as session:
        tickers = SqlTickerRepository(session, clock=context.clock)
        try:
            stored = tickers.add(symbol, timeframe, enabled=not args.disabled)
        except DuplicateTickerError as error:
            raise RejectedError(str(error)) from None
        context.report(Action.CREATE, subject(stored.symbol, stored.timeframe, stored.enabled))
    return context.finish()


def remove_ticker(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        tickers = SqlTickerRepository(session, clock=context.clock)
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        stored = _required(tickers, args)
        assigned = len(assignments.rules_for_ticker(stored.id))
        if assigned and not args.force:
            raise RejectedError(
                f"{subject(stored.symbol, stored.timeframe)} has {plural(assigned, 'assignment')};"
                " pass --force to remove them with it"
            )
        tickers.delete(stored.id)
        detail = f" with {plural(assigned, 'assignment')}" if assigned else ""
        context.report(Action.REMOVE, subject(stored.symbol, stored.timeframe) + detail)
    return context.finish()


def enable_ticker(context: Context, args: argparse.Namespace) -> int:
    return _set_enabled(context, args, enabled=True)


def disable_ticker(context: Context, args: argparse.Namespace) -> int:
    return _set_enabled(context, args, enabled=False)


def _set_enabled(context: Context, args: argparse.Namespace, *, enabled: bool) -> int:
    with unit_of_work(context) as session:
        tickers = SqlTickerRepository(session, clock=context.clock)
        stored = _required(tickers, args)
        tickers.set_enabled(stored.id, enabled)
        if stored.enabled == enabled:
            context.report(Action.UNCHANGED, subject(stored.symbol, stored.timeframe, enabled))
        else:
            # The verb already says which flag it is: repeating the state would add nothing.
            action = Action.ENABLE if enabled else Action.DISABLE
            context.report(action, subject(stored.symbol, stored.timeframe))
    return context.finish()


def _required(tickers: SqlTickerRepository, args: argparse.Namespace) -> StoredTicker:
    symbol = normalize_ticker(args.symbol)
    timeframe: Timeframe = args.timeframe
    stored = tickers.get_by_symbol(symbol, timeframe)
    if stored is None:
        raise RejectedError(f"no {subject(symbol, timeframe)} is stored")
    return stored
