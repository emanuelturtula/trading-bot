"""``config export`` and ``config import`` (spec 013, §9.7; AC23, AC24, AC25).

Import is one pass of resolution and then one transaction (decision D106): the whole file is
validated and fully resolved before the first write, so a single invalid item anywhere leaves
the database untouched. It **merges and never deletes** (decision D104): there is no ``--prune``
and no "make the database look exactly like this file" — restoring an exact state is the job of
the binary backup the deploy already takes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from trading_bot.cli.assignments import mismatch_message
from trading_bot.cli.assignments import subject as assignment_subject
from trading_bot.cli.files import (
    AssignmentItem,
    Configuration,
    ConfigurationFileError,
    RuleItem,
    TickerItem,
    read_configuration,
    render_configuration,
)
from trading_bot.cli.main import (
    EXIT_OK,
    Action,
    Context,
    RejectedError,
    SubParsers,
    UnusableEnvironmentError,
    dry_run_option,
    plural,
    quoted,
    unit_of_work,
)
from trading_bot.cli.rules import subject as rule_subject
from trading_bot.cli.tickers import subject as ticker_subject
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.persistence.repositories.assignments import SqlAssignmentRepository
from trading_bot.persistence.repositories.rules import SqlRuleRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository

__all__ = ["add_parser"]

STDIO = "-"
CONFLICT_POLICIES = ("skip", "replace", "fail")


def add_parser(groups: SubParsers) -> None:
    parser = groups.add_parser("config", help="export or import the whole configuration")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    exporting = commands.add_parser("export", help="write the configuration to a file")
    exporting.add_argument("--force", action="store_true", help="overwrite an existing file")
    exporting.add_argument("file", help="destination file, or - for standard output")
    exporting.set_defaults(handler=export_configuration)

    importing = commands.add_parser("import", help="merge a configuration file into the database")
    importing.add_argument(
        "--on-conflict",
        choices=CONFLICT_POLICIES,
        default="skip",
        help="what to do with a ticker or rule that already exists (default: skip)",
    )
    dry_run_option(importing)
    importing.add_argument("file", help="source file, or - for standard input")
    importing.set_defaults(handler=import_configuration)


# --- export --------------------------------------------------------------------------------


def export_configuration(context: Context, args: argparse.Namespace) -> int:
    destination: str = args.file
    with unit_of_work(context) as session:
        text = _rendered(session)
    if destination == STDIO:
        context.out.write(text)
        return EXIT_OK
    path = Path(destination)
    if path.exists() and not args.force:
        raise RejectedError(f"{destination} already exists; pass --force to overwrite it")
    try:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except OSError:
        raise UnusableEnvironmentError(f"{destination} cannot be written") from None
    context.emit(f"exported {destination}")
    return EXIT_OK


def _rendered(session: Session) -> str:
    tickers = SqlTickerRepository(session).list_all()
    rules = SqlRuleRepository(session).list_all()
    assignments = SqlAssignmentRepository(session).list_all()
    by_ticker = {row.id: row for row in tickers}
    by_rule = {row.id: row for row in rules}
    return render_configuration(
        [
            TickerItem(symbol=row.symbol, timeframe=row.timeframe, enabled=row.enabled)
            for row in tickers
        ],
        [RuleItem(enabled=row.enabled, rule=row.rule) for row in rules],
        [
            AssignmentItem(
                symbol=by_ticker[row.ticker_id].symbol,
                timeframe=row.timeframe,
                rule=by_rule[row.rule_id].name,
            )
            for row in assignments
        ],
    )


# --- import --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TickerPlan:
    item: TickerItem
    action: Action
    stored: StoredTicker | None


@dataclass(frozen=True, slots=True)
class _RulePlan:
    item: RuleItem
    action: Action
    stored: StoredRule | None


@dataclass(frozen=True, slots=True)
class _AssignmentPlan:
    item: AssignmentItem
    action: Action


def import_configuration(context: Context, args: argparse.Namespace) -> int:
    source: str = args.file
    policy: str = args.on_conflict
    payload = _read_source(source)
    try:
        configuration = read_configuration(payload)
    except ConfigurationFileError as error:
        raise RejectedError(error.message, path=error.path) from None

    lines: list[tuple[Action, str, str]] = []
    with unit_of_work(context) as session:
        tickers, rules, assignments = _plan(session, configuration, policy)
        _apply(context, session, tickers, rules, assignments, lines)
    for action, subject, detail in lines:
        context.report(action, subject, detail)
    if not context.dry_run:
        context.emit(_summary(source, configuration, [action for action, _, _ in lines]))
    return context.finish()


def _read_source(source: str) -> bytes:
    """The raw bytes of the file or of standard input; the caps are applied by the reader."""
    if source == STDIO:
        try:
            return sys.stdin.read().encode("utf-8")
        except (OSError, UnicodeError):
            raise RejectedError("standard input cannot be read") from None
    try:
        return Path(source).read_bytes()
    except OSError:
        raise RejectedError(f"{source} cannot be read") from None


def _plan(
    session: Session, configuration: Configuration, policy: str
) -> tuple[list[_TickerPlan], list[_RulePlan], list[_AssignmentPlan]]:
    """Decide every item against the file and the database, writing nothing (decision D106)."""
    tickers = SqlTickerRepository(session)
    rules = SqlRuleRepository(session)
    assignments = SqlAssignmentRepository(session)

    ticker_plans = _plan_tickers(tickers, configuration, policy)
    rule_plans = _plan_rules(rules, assignments, configuration, policy)
    assignment_plans = _plan_assignments(
        tickers, rules, assignments, configuration, ticker_plans, rule_plans
    )
    return ticker_plans, rule_plans, assignment_plans


def _plan_tickers(
    tickers: SqlTickerRepository, configuration: Configuration, policy: str
) -> list[_TickerPlan]:
    plans: list[_TickerPlan] = []
    for index, item in enumerate(configuration.tickers):
        stored = tickers.get_by_symbol(item.symbol, item.timeframe)
        if stored is None:
            action = Action.CREATE
        elif policy == "fail":
            raise RejectedError(
                f"ticker {item.symbol} {item.timeframe.value} already exists",
                path=f"tickers[{index}]",
            )
        elif policy == "skip":
            action = Action.SKIP
        else:
            action = Action.UNCHANGED if stored.enabled == item.enabled else Action.REPLACE
        plans.append(_TickerPlan(item=item, action=action, stored=stored))
    return plans


def _plan_rules(
    rules: SqlRuleRepository,
    assignments: SqlAssignmentRepository,
    configuration: Configuration,
    policy: str,
) -> list[_RulePlan]:
    plans: list[_RulePlan] = []
    for index, item in enumerate(configuration.rules):
        stored = rules.get_by_name(item.rule.name)
        if stored is None:
            action = Action.CREATE
        elif policy == "fail":
            raise RejectedError(
                f"a rule named {quoted(item.rule.name)} already exists (id {stored.id})",
                path=f"rules[{index}]",
            )
        elif policy == "skip":
            action = Action.SKIP
        elif stored.rule == item.rule and stored.enabled == item.enabled:
            action = Action.UNCHANGED
        else:
            action = Action.REPLACE
            assigned = len(assignments.tickers_for_rule(stored.id))
            if assigned and stored.rule.timeframe != item.rule.timeframe:
                raise RejectedError(
                    f"rule {quoted(item.rule.name)} is assigned to {plural(assigned, 'ticker')}"
                    f" on {stored.rule.timeframe.value}, so its timeframe cannot change to"
                    f" {item.rule.timeframe.value}",
                    path=f"rules[{index}]",
                )
        plans.append(_RulePlan(item=item, action=action, stored=stored))
    return plans


def _plan_assignments(
    tickers: SqlTickerRepository,
    rules: SqlRuleRepository,
    assignments: SqlAssignmentRepository,
    configuration: Configuration,
    ticker_plans: list[_TickerPlan],
    rule_plans: list[_RulePlan],
) -> list[_AssignmentPlan]:
    """Resolve every assignment against the file **and** the database, checking both ends."""
    in_file: dict[tuple[str, Timeframe], StoredTicker | None] = {
        (plan.item.symbol, plan.item.timeframe): plan.stored for plan in ticker_plans
    }
    rule_timeframes: dict[str, Timeframe] = {}
    for plan in rule_plans:
        stored_rule = plan.stored
        if stored_rule is None or plan.action in (Action.CREATE, Action.REPLACE):
            rule_timeframes[plan.item.rule.name] = plan.item.rule.timeframe
        else:
            # A skipped or unchanged rule keeps the timeframe the database already holds.
            rule_timeframes[plan.item.rule.name] = stored_rule.rule.timeframe

    plans: list[_AssignmentPlan] = []
    for index, item in enumerate(configuration.assignments):
        path = f"assignments[{index}]"
        key = (item.symbol, item.timeframe)
        known = key in in_file
        stored_ticker = in_file[key] if known else tickers.get_by_symbol(*key)
        if not known and stored_ticker is None:
            raise RejectedError(
                f"no ticker {item.symbol} {item.timeframe.value} in the file or the database",
                path=path,
            )
        stored_rule = rules.get_by_name(item.rule)
        timeframe = rule_timeframes.get(item.rule)
        if timeframe is None:
            if stored_rule is None:
                raise RejectedError(
                    f"no rule named {quoted(item.rule)} in the file or the database", path=path
                )
            timeframe = stored_rule.rule.timeframe
        if timeframe != item.timeframe:
            raise RejectedError(
                mismatch_message(item.rule, timeframe, item.symbol, item.timeframe), path=path
            )
        exists = _already_assigned(assignments, stored_ticker, stored_rule)
        plans.append(
            _AssignmentPlan(item=item, action=Action.UNCHANGED if exists else Action.CREATE)
        )
    return plans


def _already_assigned(
    assignments: SqlAssignmentRepository,
    ticker: StoredTicker | None,
    rule: StoredRule | None,
) -> bool:
    if ticker is None or rule is None:
        return False
    return rule.id in {row.id for row in assignments.rules_for_ticker(ticker.id)}


def _apply(
    context: Context,
    session: Session,
    ticker_plans: list[_TickerPlan],
    rule_plans: list[_RulePlan],
    assignment_plans: list[_AssignmentPlan],
    lines: list[tuple[Action, str, str]],
) -> None:
    """Write the decided plan in one transaction, in the order tickers, rules, assignments."""
    tickers = SqlTickerRepository(session, clock=context.clock)
    rules = SqlRuleRepository(session, clock=context.clock)
    assignments = SqlAssignmentRepository(session, clock=context.clock)

    ticker_ids: dict[tuple[str, Timeframe], int] = {}
    for ticker_plan in ticker_plans:
        ticker_item = ticker_plan.item
        if ticker_plan.action is Action.CREATE:
            ticker = tickers.add(
                ticker_item.symbol, ticker_item.timeframe, enabled=ticker_item.enabled
            )
        elif ticker_plan.action is Action.REPLACE:
            ticker = tickers.set_enabled(_resolved(ticker_plan.stored).id, ticker_item.enabled)
        else:
            ticker = _resolved(ticker_plan.stored)
        ticker_ids[(ticker_item.symbol, ticker_item.timeframe)] = ticker.id
        lines.append(
            (
                ticker_plan.action,
                ticker_subject(ticker_item.symbol, ticker_item.timeframe, ticker.enabled),
                "it is already tracked" if ticker_plan.action is Action.SKIP else "",
            )
        )

    rule_ids: dict[str, int] = {}
    show_id = not context.dry_run
    for rule_plan in rule_plans:
        rule_item = rule_plan.item
        detail = ""
        if rule_plan.action is Action.CREATE:
            rule = rules.add(rule_item.rule, enabled=rule_item.enabled)
        elif rule_plan.action is Action.REPLACE:
            rule = rules.replace(
                _resolved(rule_plan.stored).id, rule_item.rule, enabled=rule_item.enabled
            )
        else:
            rule = _resolved(rule_plan.stored)
            if rule_plan.action is Action.SKIP:
                detail = f"a rule with that name already exists (id {rule.id})"
        rule_ids[rule_item.rule.name] = rule.id
        # A skipped rule is named alone: the detail already says what is in the database.
        subject = rule_subject(
            rule, show_id=show_id, with_details=rule_plan.action is not Action.SKIP
        )
        lines.append((rule_plan.action, subject, detail))

    for assignment_plan in assignment_plans:
        assignment_item = assignment_plan.item
        key = (assignment_item.symbol, assignment_item.timeframe)
        ticker_id = ticker_ids.get(key)
        if ticker_id is None:
            ticker_id = _resolved(tickers.get_by_symbol(*key)).id
        rule_id = rule_ids.get(assignment_item.rule)
        if rule_id is None:
            rule_id = _resolved(rules.get_by_name(assignment_item.rule)).id
        assignments.assign(ticker_id, rule_id)
        lines.append(
            (
                assignment_plan.action,
                assignment_subject(
                    assignment_item.symbol, assignment_item.timeframe, assignment_item.rule
                ),
                "",
            )
        )


def _resolved[Record: (StoredTicker, StoredRule)](stored: Record | None) -> Record:
    """The row the plan already resolved; a missing one would be a bug in the planner."""
    if stored is None:  # pragma: no cover - the plan resolves every item before the first write
        raise RuntimeError("an item of the plan lost the row it was resolved against")
    return stored


def _summary(source: str, configuration: Configuration, actions: list[Action]) -> str:
    counts = {
        action: sum(1 for value in actions if value is action)
        for action in (Action.CREATE, Action.REPLACE, Action.UNCHANGED, Action.SKIP)
    }
    sizes = ", ".join(
        (
            plural(len(configuration.tickers), "ticker"),
            plural(len(configuration.rules), "rule"),
            plural(len(configuration.assignments), "assignment"),
        )
    )
    outcome = (
        f"{counts[Action.CREATE]} created, {counts[Action.REPLACE]} replaced,"
        f" {counts[Action.UNCHANGED]} unchanged, {counts[Action.SKIP]} skipped"
    )
    return f"imported {source}: {sizes} ({outcome})"
