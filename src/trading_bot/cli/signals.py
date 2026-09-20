"""The ``signals list`` subcommand (spec 014, Design 10.3; AC24).

**Read-only**: one session, nothing written, no ``--dry-run``. It is the only way to see what
the engine records before Telegram and the dashboard exist (user decision U4), so a line is an
operator's line: the canonical key of spec 004, the side, the rule's current name, the close
price and the delivery state. Indicator values are deliberately not printed, so a line stays
bounded; showing the candle as its session date belongs to the notification and the dashboard
(spec 004, Design 5).
"""

from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

from trading_bot.cli.main import (
    EXIT_OK,
    Context,
    RejectedError,
    SubParsers,
    quoted,
    timeframe_option,
    unit_of_work,
)
from trading_bot.cli.tickers import subject as ticker_subject
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.signals import SqlSignalRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository
from trading_bot.persistence.signal_records import MAX_PAGE_SIZE, StoredSignal

__all__ = ["add_parser"]

DEFAULT_LIMIT = 20


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("signals", help="read the signals the engine recorded")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    listing = commands.add_parser("list", help="list the recorded signals, newest first")
    listing.add_argument("--ticker", default=None, help="only this symbol, for example AAPL")
    timeframe_option(listing, default=None)
    listing.add_argument("--rule", default=None, help="only this rule, by its unique name")
    listing.add_argument(
        "--limit",
        type=page_size,
        default=DEFAULT_LIMIT,
        help=f"how many signals to print, 1-{MAX_PAGE_SIZE} (default: {DEFAULT_LIMIT})",
    )
    listing.set_defaults(handler=list_signals)


def page_size(text: str) -> int:
    """``--limit`` as an integer in ``[1, MAX_PAGE_SIZE]``; anything else is a usage error."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"expected an integer between 1 and {MAX_PAGE_SIZE}")
    return value


def line_of(stored: StoredSignal) -> str:
    """One signal as Design 10.3 prints it; the key is the one the engine logs."""
    delivery = (
        "not notified"
        if stored.notified_at is None
        else f"notified {stored.notified_at.isoformat()}"
    )
    return (
        f"{stored.key} {stored.signal.side.value} {quoted(stored.rule_name)}"
        f" close {stored.signal.close_price!r}"
        f" (id {stored.id}, recorded {stored.created_at.isoformat()}, {delivery})"
    )


def list_signals(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        page = SqlSignalRepository(session, clock=context.clock).history(
            ticker_id=_ticker_id(context, session, args),
            rule_id=_rule_id(context, session, args),
            timeframe=args.timeframe,
            limit=args.limit,
        )
        for stored in page.items:
            context.emit(line_of(stored))
    return EXIT_OK


def _ticker_id(context: Context, session: Session, args: argparse.Namespace) -> int | None:
    """The ticker of ``--ticker`` on ``--timeframe`` or, without one, on ``1d`` (D109)."""
    if args.ticker is None:
        return None
    symbol = normalize_ticker(args.ticker)
    timeframe: Timeframe = args.timeframe if args.timeframe is not None else Timeframe.D1
    stored = SqlTickerRepository(session, clock=context.clock).get_by_symbol(symbol, timeframe)
    if stored is None:
        raise RejectedError(f"no {ticker_subject(symbol, timeframe)} is stored")
    return stored.id


def _rule_id(context: Context, session: Session, args: argparse.Namespace) -> int | None:
    if args.rule is None:
        return None
    name: str = args.rule
    stored = SqlRuleRepository(session, clock=context.clock).get_by_name(name)
    if stored is None:
        raise RejectedError(f"no rule named {quoted(name)} is stored")
    return stored.id
