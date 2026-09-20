"""The ``state show`` subcommand (spec 014, Design 10.4; AC25).

**Read-only**: one session, nothing written, no ``--dry-run``. There is deliberately no
``pause``/``resume`` here: pausing a bot that cannot notify yet is pointless, and once Telegram
exists ``/pause`` is the surface (decision D122, user decision U4). A missing row is printed as
``never``, which is exactly what it means.
"""

from __future__ import annotations

import argparse

from trading_bot.cli.main import EXIT_OK, Context, SubParsers, unit_of_work
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.repositories.bot_state import SqlBotStateRepository
from trading_bot.persistence.state import BotState

__all__ = ["add_parser"]

NEVER = "never"


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("state", help="read the runtime state of the bot")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    showing = commands.add_parser("show", help="show the pause, the heartbeat and the last runs")
    showing.set_defaults(handler=show_state)


def lines_of(state: BotState) -> list[str]:
    """The five lines of Design 10.4, always in this order."""
    paused = "no" if state.paused_since is None else f"since {state.paused_since.isoformat()}"
    heartbeat = NEVER if state.last_heartbeat is None else state.last_heartbeat.isoformat()
    lines = [f"paused: {paused}", f"last heartbeat: {heartbeat}"]
    for timeframe in Timeframe:
        run = state.last_run(timeframe)
        detail = (
            NEVER
            if run is None
            else f"{run.scheduled_at.isoformat()} (completed {run.completed_at.isoformat()})"
        )
        lines.append(f"last run {timeframe.value}: {detail}")
    return lines


def show_state(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        state = SqlBotStateRepository(session, clock=context.clock).load()
    for line in lines_of(state):
        context.emit(line)
    return EXIT_OK
