"""The configuration snapshot of a run (spec 015, T1, AC1, AC3, AC16, AC20).

``plan_lookback`` is pinned on a literal table of rules. ``read_plan`` is exercised over a
temporary database through ``sql_unit_of_work``, with the statements recorded on the engine, so
"one unit of work per run" is counted rather than assumed. No clock is read: the repositories
take an injected one and every instant is a literal.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import pytest
from sqlalchemy import Engine, event

from tests.fixtures.repositories import fixed_clock, repositories, sample_rule
from tests.fixtures.rules import (
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    value_operand,
)
from trading_bot.data.provider import MAX_LOOKBACK
from trading_bot.domain.rules.errors import RuleErrorKind, RuleProblem
from trading_bot.domain.rules.schema import Rule, parse_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine import planning
from trading_bot.engine.planning import plan_lookback, read_plan
from trading_bot.engine.sql_unit_of_work import sql_unit_of_work
from trading_bot.persistence.clock import Clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import StoredRuleError
from trading_bot.persistence.records import StoredRule

BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
CLOCK_START = "2026-01-02T03:04:05+00:00"


def rule_of(conditions: object, **overrides: object) -> Rule:
    return parse_rule(rule_payload(conditions, **overrides))  # type: ignore[arg-type]


def cheap_rule(**overrides: object) -> Rule:
    """A rule of a price and a constant: warmup 1, stable warmup 1."""
    return rule_of({"all": [condition(price_operand(), ">", value_operand(100))]}, **overrides)


@contextmanager
def recorded_statements(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def spy(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", spy)


@dataclass
class Configuration:
    """The ids the setup created, so expectations name rows instead of guessing."""

    tickers: dict[str, int]
    rules: dict[str, int]


def configure(database: Database) -> Configuration:
    """One daily ticker with two enabled rules, plus everything a run must ignore."""
    tickers: dict[str, int] = {}
    rules: dict[str, int] = {}
    with database.session() as session:
        repos = repositories(session)
        for symbol, timeframe, enabled in (
            ("AAPL", Timeframe.D1, True),
            ("MSFT", Timeframe.D1, True),  # enabled, but no enabled rule
            ("TSLA", Timeframe.D1, False),  # disabled ticker
            ("SPY", Timeframe.H1, True),  # another timeframe
        ):
            tickers[symbol] = repos.tickers.add(symbol, timeframe, enabled=enabled).id
        for name, enabled, timeframe in (
            ("Alpha", True, "1d"),
            ("Beta", True, "1d"),
            ("Gamma", False, "1d"),  # disabled rule
            ("Hourly", True, "1h"),
        ):
            rules[name] = repos.rules.add(
                sample_rule(name, timeframe=timeframe), enabled=enabled
            ).id
        repos.assignments.assign(tickers["AAPL"], rules["Alpha"])
        repos.assignments.assign(tickers["AAPL"], rules["Beta"])
        repos.assignments.assign(tickers["AAPL"], rules["Gamma"])
        repos.assignments.assign(tickers["TSLA"], rules["Alpha"])
        repos.assignments.assign(tickers["SPY"], rules["Hourly"])
    return Configuration(tickers=tickers, rules=rules)


def plan_over(
    database: Database, timeframe: Timeframe = Timeframe.D1, **kwargs: object
) -> planning.RunPlan:
    unit_of_work = sql_unit_of_work(database, clock=fixed_clock_from_start())
    with unit_of_work() as repos:
        return read_plan(repos, timeframe, **kwargs)  # type: ignore[arg-type]


def fixed_clock_from_start() -> Clock:
    return fixed_clock(datetime.fromisoformat(CLOCK_START))


# --- plan_lookback (AC3) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "rule", "expected"),
    [
        ("a price against a constant", cheap_rule(), 1),
        (
            "rsi crossing 30",
            rule_of(
                {
                    "all": [
                        condition(
                            indicator_operand("rsi", {"length": 14}),
                            "crosses_below",
                            value_operand(30),
                        )
                    ]
                }
            ),
            114,
        ),
        (
            "close above sma(200)",
            rule_of(
                {
                    "all": [
                        condition(price_operand(), ">", indicator_operand("sma", {"length": 200}))
                    ]
                }
            ),
            200,
        ),
        (
            "macd crossing its signal line",
            rule_of(
                {
                    "all": [
                        condition(
                            indicator_operand("macd", output="macd"),
                            "crosses_above",
                            indicator_operand("macd", output="signal"),
                        )
                    ]
                }
            ),
            165,
        ),
        (
            "ema(500) crossed by the close",
            rule_of(
                {
                    "all": [
                        condition(
                            price_operand(),
                            "crosses_above",
                            indicator_operand("ema", {"length": 500}),
                        )
                    ]
                }
            ),
            2255,
        ),
        ("a cheap rule with the maximum cooldown", cheap_rule(cooldown_bars=500), 501),
        ("a cheap rule with a cooldown of 19", cheap_rule(cooldown_bars=19), 20),
    ],
)
def test_plan_lookback_on_a_literal_table_of_rules(label: str, rule: Rule, expected: int) -> None:
    assert plan_lookback([rule]) == expected, label


def test_plan_lookback_is_the_maximum_over_the_rules() -> None:
    rules = [cheap_rule(), sample_rule("Sma"), cheap_rule(cooldown_bars=500)]

    assert plan_lookback(rules) == 501


def test_plan_lookback_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="at least one rule"):
        plan_lookback([])


def test_plan_lookback_never_exceeds_the_provider_maximum() -> None:
    """The catalog cannot reach the cap today; the cap is kept so a future one cannot either."""
    assert plan_lookback([cheap_rule(cooldown_bars=500)]) < MAX_LOOKBACK


def test_plan_lookback_applies_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning, "MAX_LOOKBACK", 7)

    assert plan_lookback([cheap_rule(cooldown_bars=500)]) == 7


# --- read_plan: what the snapshot holds (AC1) -----------------------------------------------


def test_the_plan_holds_only_enabled_tickers_with_enabled_rules(database: Database) -> None:
    configuration = configure(database)

    plan = plan_over(database)

    assert plan.timeframe is Timeframe.D1
    assert plan.paused is False
    assert plan.paused_since is None
    assert [ticker.symbol for ticker in plan.tickers] == ["AAPL"]
    assert plan.tickers[0].ticker_id == configuration.tickers["AAPL"]
    assert plan.tickers[0].timeframe is Timeframe.D1
    assert [stored.name for stored in plan.tickers[0].rules] == ["Alpha", "Beta"]


def test_the_plan_sizes_the_lookback_from_the_ticker_rules(database: Database) -> None:
    configure(database)

    plan = plan_over(database)

    assert plan.tickers[0].lookback == plan_lookback(
        [stored.rule for stored in plan.tickers[0].rules]
    )
    assert plan.tickers[0].lookback == 20  # close > sma(length=20)


def test_a_ticker_of_another_timeframe_is_absent(database: Database) -> None:
    configure(database)

    assert [ticker.symbol for ticker in plan_over(database, Timeframe.H1).tickers] == ["SPY"]
    assert plan_over(database, Timeframe.H4).tickers == ()


def test_the_whole_snapshot_is_read_in_one_unit_of_work(database: Database) -> None:
    configure(database)

    with recorded_statements(database.engine) as statements:
        plan = plan_over(database)

    assert statements.count(BEGIN_IMMEDIATE) == 1
    assert len(plan.tickers) == 1


def test_configuration_written_after_the_snapshot_does_not_change_it(database: Database) -> None:
    configure(database)
    plan = plan_over(database)

    with database.session() as session:
        repos = repositories(session)
        repos.tickers.add("NVDA", Timeframe.D1)

    assert [ticker.symbol for ticker in plan.tickers] == ["AAPL"]


# --- read_plan: the pause (AC16) -------------------------------------------------------------


def test_a_paused_state_short_circuits_before_any_configuration_read(database: Database) -> None:
    configure(database)
    with database.session() as session:
        paused_at = repositories(session).state.pause()

    with recorded_statements(database.engine) as statements:
        plan = plan_over(database)

    assert plan.paused is True
    assert plan.paused_since == paused_at
    assert plan.tickers == ()
    assert [statement for statement in statements if "FROM tickers" in statement] == []
    assert [statement for statement in statements if "FROM ticker_rules" in statement] == []


# --- read_plan: the ticker filter (AC20) ------------------------------------------------------


def test_the_ticker_filter_keeps_only_the_requested_symbols(database: Database) -> None:
    configure(database)
    with database.session() as session:
        repos = repositories(session)
        nvda = repos.tickers.add("NVDA", Timeframe.D1)
        repos.assignments.assign(nvda.id, repos.rules.get_by_name("Alpha").id)  # type: ignore[union-attr]

    assert [ticker.symbol for ticker in plan_over(database, tickers=("NVDA",)).tickers] == ["NVDA"]
    assert [
        ticker.symbol for ticker in plan_over(database, tickers=[" nvda ", "aapl"]).tickers
    ] == ["AAPL", "NVDA"]


def test_the_filter_ignores_symbols_that_are_not_enabled_for_the_timeframe(
    database: Database,
) -> None:
    configure(database)

    assert plan_over(database, tickers=("SPY", "TSLA", "MSFT")).tickers == ()


def test_an_empty_filter_plans_nothing(database: Database) -> None:
    configure(database)

    assert plan_over(database, tickers=()).tickers == ()


def test_a_string_of_symbols_is_a_type_error(database: Database) -> None:
    configure(database)

    with pytest.raises(TypeError, match="sequence of symbols"):
        plan_over(database, tickers="AAPL")


def test_an_invalid_symbol_is_a_value_error(database: Database) -> None:
    configure(database)

    with pytest.raises(ValueError, match="invalid ticker"):
        plan_over(database, tickers=("AA PL",))


def test_a_non_timeframe_is_a_type_error(database: Database) -> None:
    configure(database)

    with pytest.raises(TypeError, match="Timeframe"):
        plan_over(database, "1d")  # type: ignore[arg-type]


# --- read_plan: a document that no longer parses (AC14) ---------------------------------------


class BrokenAssignments:
    """An ``AssignmentRepository`` whose ``rules_for_ticker`` raises, like a corrupt document."""

    def assign(self, ticker_id: int, rule_id: int) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    def unassign(self, ticker_id: int, rule_id: int) -> bool:  # pragma: no cover - unused
        raise NotImplementedError

    def list_all(self) -> tuple[object, ...]:  # pragma: no cover - unused
        raise NotImplementedError

    def rules_for_ticker(
        self, ticker_id: int, *, enabled_only: bool = False
    ) -> tuple[StoredRule, ...]:
        raise StoredRuleError(
            ticker_id,
            [
                RuleProblem(
                    kind=RuleErrorKind.UNKNOWN_INDICATOR,
                    path="conditions",
                    message="the stored document names an indicator that no longer exists",
                )
            ],
        )

    def tickers_for_rule(
        self, rule_id: int, *, enabled_only: bool = False
    ) -> tuple[object, ...]:  # pragma: no cover - unused
        raise NotImplementedError


def test_a_stored_rule_that_no_longer_parses_propagates(database: Database) -> None:
    configure(database)
    unit_of_work = sql_unit_of_work(database, clock=fixed_clock_from_start())

    with unit_of_work() as repos:
        broken = dataclasses.replace(repos, assignments=BrokenAssignments())
        with pytest.raises(StoredRuleError):
            read_plan(broken, Timeframe.D1)


# --- The SQL unit of work (AC19) -----------------------------------------------------------


def test_the_sql_unit_of_work_yields_the_four_ports_over_one_session(
    database: Database,
) -> None:
    configure(database)
    unit_of_work = sql_unit_of_work(database, clock=fixed_clock_from_start())

    with recorded_statements(database.engine) as statements, unit_of_work() as repos:
        assert repos.state.load().paused is False
        assert repos.tickers.list_enabled(Timeframe.D1) != ()
        assert repos.assignments.list_all() != ()
        assert repos.signals.count() == 0

    assert statements.count(BEGIN_IMMEDIATE) == 1
    assert sorted(field.name for field in dataclasses.fields(repos)) == [
        "assignments",
        "signals",
        "state",
        "tickers",
    ]


def test_the_sql_unit_of_work_commits_on_a_clean_exit(database: Database) -> None:
    configure(database)
    unit_of_work = sql_unit_of_work(database, clock=fixed_clock_from_start())

    with unit_of_work() as repos:
        repos.state.pause()

    with unit_of_work() as repos:
        assert repos.state.load().paused is True


def test_the_sql_unit_of_work_rolls_back_on_an_error(database: Database) -> None:
    configure(database)
    unit_of_work = sql_unit_of_work(database, clock=fixed_clock_from_start())

    def pause_then_fail() -> None:
        with unit_of_work() as repos:
            repos.state.pause()
            raise RuntimeError("the block failed")

    with pytest.raises(RuntimeError):
        pause_then_fail()

    with unit_of_work() as repos:
        assert repos.state.load().paused is False


def test_the_sql_unit_of_work_needs_a_database() -> None:
    with pytest.raises(TypeError, match="Database"):
        sql_unit_of_work("data/trading_bot.db")  # type: ignore[arg-type]
