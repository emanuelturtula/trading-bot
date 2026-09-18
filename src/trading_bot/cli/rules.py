"""The rule subcommands (spec 013, §9.5; AC34, AC36, AC37).

There is deliberately no ``rules add``: a rule is a JSON document with nested conditions, not
something to type as command-line arguments, and ``parse_rule`` must stay the single entry
point. Rules are created by ``config import`` and removed here. The rule **document** is never
printed: ``config export`` is the way to read it.
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
    quoted,
    unit_of_work,
)
from trading_bot.persistence.records import StoredRule
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.rules import SqlRuleRepository

__all__ = ["add_parser"]


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("rules", help="manage the stored rules")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    listing = commands.add_parser("list", help="list every rule")
    listing.set_defaults(handler=list_rules)

    removing = commands.add_parser("remove", help="remove a rule by name")
    removing.add_argument("name", help="the unique rule name, usually quoted")
    removing.add_argument(
        "--force", action="store_true", help="remove it together with its assignments"
    )
    dry_run_option(removing)
    removing.set_defaults(handler=remove_rule)

    for name, enabled in (("enable", True), ("disable", False)):
        command = commands.add_parser(name, help=f"{name} a rule by name")
        command.add_argument("name")
        dry_run_option(command)
        command.set_defaults(handler=enable_rule if enabled else disable_rule)


def subject(
    stored: StoredRule, *, show_id: bool, with_details: bool = True, with_state: bool = True
) -> str:
    """``rule 'name' (id 1, 1d, BUY, disabled)``; a dry run carries no id (decision D107)."""
    if not with_details:
        return f"rule {quoted(stored.name)}"
    details = [f"id {stored.id}"] if show_id else []
    details += [stored.rule.timeframe.value, stored.rule.signal.value]
    if with_state:
        details.append("enabled" if stored.enabled else "disabled")
    return f"rule {quoted(stored.name)} ({', '.join(details)})"


def list_rules(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        rules = SqlRuleRepository(session, clock=context.clock)
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        for stored in rules.list_all():
            assigned = len(assignments.tickers_for_rule(stored.id))
            state = "enabled" if stored.enabled else "disabled"
            context.emit(
                f"{quoted(stored.name)} {stored.rule.timeframe.value}"
                f" {stored.rule.signal.value} {state} {plural(assigned, 'ticker')}"
            )
    return EXIT_OK


def remove_rule(context: Context, args: argparse.Namespace) -> int:
    with unit_of_work(context) as session:
        rules = SqlRuleRepository(session, clock=context.clock)
        assignments = SqlAssignmentRepository(session, clock=context.clock)
        stored = _required(rules, args)
        assigned = len(assignments.tickers_for_rule(stored.id))
        if assigned and not args.force:
            raise RejectedError(
                f"rule {quoted(stored.name)} has {plural(assigned, 'assignment')};"
                " pass --force to remove them with it"
            )
        rules.delete(stored.id)
        detail = f" with {plural(assigned, 'assignment')}" if assigned else ""
        context.report(Action.REMOVE, subject(stored, show_id=False, with_details=False) + detail)
    return context.finish()


def enable_rule(context: Context, args: argparse.Namespace) -> int:
    return _set_enabled(context, args, enabled=True)


def disable_rule(context: Context, args: argparse.Namespace) -> int:
    return _set_enabled(context, args, enabled=False)


def _set_enabled(context: Context, args: argparse.Namespace, *, enabled: bool) -> int:
    with unit_of_work(context) as session:
        rules = SqlRuleRepository(session, clock=context.clock)
        stored = _required(rules, args)
        updated = rules.set_enabled(stored.id, enabled)
        show_id = not context.dry_run
        if stored.enabled == enabled:
            context.report(Action.UNCHANGED, subject(updated, show_id=show_id))
        else:
            # The verb already says which flag it is: repeating the state would add nothing.
            action = Action.ENABLE if enabled else Action.DISABLE
            context.report(action, subject(updated, show_id=show_id, with_state=False))
    return context.finish()


def _required(rules: SqlRuleRepository, args: argparse.Namespace) -> StoredRule:
    name: str = args.name
    stored = rules.get_by_name(name)
    if stored is None:
        raise RejectedError(f"no rule named {quoted(name)} is stored")
    return stored
