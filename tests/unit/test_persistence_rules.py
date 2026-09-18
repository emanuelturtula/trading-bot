"""The rule repository and the stored document (spec 013, T5, T6, AC9, AC12-AC15, AC17, AC19).

Rule payloads come from ``tests.fixtures.rules``; no rule JSON is hand-written here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.fixtures.repositories import fixed_clock, frozen_clock, repositories, sample_rule
from tests.fixtures.rules import Payload, valid_payloads
from trading_bot.domain.rules.errors import RuleErrorKind, RuleProblem
from trading_bot.domain.rules.schema import dump_rule, parse_rule
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import (
    AssignedTimeframeError,
    DuplicateRuleNameError,
    StoredRuleError,
    UnknownRuleError,
)
from trading_bot.persistence.records import StoredRule
from trading_bot.persistence.repositories import rules as rules_module

START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
STEP = timedelta(seconds=1)
DERIVED_COLUMNS = text("SELECT name, signal, timeframe, definition_json FROM rules WHERE id = :id")
RULE_COUNT = text("SELECT COUNT(*) FROM rules")


def canonical_text(payload: Payload) -> str:
    """The serialization spec 013 Design 8 requires, written out independently of the code."""
    rule = parse_rule(payload)
    return json.dumps(dump_rule(rule), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# --- T5: add, lookups and ordering ---------------------------------------------------------


def test_add_stores_a_disabled_rule_with_both_timestamps_from_the_clock(
    database: Database,
) -> None:
    rule = sample_rule("Daily breakout")
    with database.session() as session:
        stored = repositories(session, clock=frozen_clock(START)).rules.add(rule)

    assert stored == StoredRule(id=1, rule=rule, enabled=False, created_at=START, updated_at=START)
    assert stored.name == "Daily breakout"


def test_add_can_store_an_enabled_rule(database: Database) -> None:
    with database.session() as session:
        stored = repositories(session).rules.add(sample_rule(), enabled=True)

    assert stored.enabled is True


def test_the_derived_columns_come_from_the_parsed_rule(database: Database) -> None:
    rule = sample_rule("Hourly exit", signal="SELL", timeframe="1h")
    with database.session() as session:
        stored = repositories(session).rules.add(rule)

    with database.session() as session:
        row = session.execute(DERIVED_COLUMNS, {"id": stored.id}).one()

    assert row.name == "Hourly exit"
    assert row.signal == Side.SELL.value
    assert row.timeframe == Timeframe.H1.value
    assert row.definition_json == canonical_text(dump_rule(rule))


def test_a_duplicate_name_is_refused_and_leaves_the_session_usable(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        handles.rules.add(sample_rule("Same name"))

        with pytest.raises(DuplicateRuleNameError, match="Same name"):
            handles.rules.add(sample_rule("Same name"))

        handles.rules.add(sample_rule("Another name"))

    with database.session() as session:
        assert [rule.name for rule in repositories(session).rules.list_all()] == [
            "Another name",
            "Same name",
        ]


def test_names_differing_only_in_case_are_two_rules(database: Database) -> None:
    """Decision D85: names are compared exactly as stored."""
    with database.session() as session:
        handles = repositories(session)
        handles.rules.add(sample_rule("daily"))
        handles.rules.add(sample_rule("DAILY"))

    with database.session() as session:
        assert [rule.name for rule in repositories(session).rules.list_all()] == ["DAILY", "daily"]


def test_get_and_get_by_name_return_the_stored_record(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session, clock=frozen_clock(START))
        stored = handles.rules.add(sample_rule("Daily breakout"))

        assert handles.rules.get(stored.id) == stored
        assert handles.rules.get_by_name("Daily breakout") == stored
        assert handles.rules.get(stored.id + 1) is None
        assert handles.rules.get_by_name("daily breakout") is None


def test_list_all_is_ordered_by_name(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        for name in ("Zulu", "alpha", "Bravo"):
            handles.rules.add(sample_rule(name))

    with database.session() as session:
        listed = repositories(session).rules.list_all()

    assert [rule.name for rule in listed] == ["Bravo", "Zulu", "alpha"]
    assert listed == tuple(sorted(listed, key=lambda row: row.name))


def test_list_enabled_filters_the_flag_and_optionally_the_timeframe(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        handles.rules.add(sample_rule("Daily on", timeframe="1d"), enabled=True)
        handles.rules.add(sample_rule("Daily off", timeframe="1d"))
        handles.rules.add(sample_rule("Hourly on", timeframe="1h"), enabled=True)

    with database.session() as session:
        handles = repositories(session)

        assert [rule.name for rule in handles.rules.list_enabled()] == ["Daily on", "Hourly on"]
        assert [rule.name for rule in handles.rules.list_enabled(Timeframe.D1)] == ["Daily on"]
        assert handles.rules.list_enabled(Timeframe.H4) == ()


# --- T5: replace, set_enabled and delete ---------------------------------------------------


def test_replace_stores_the_new_document_and_moves_updated_at(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, STEP))
        stored = handles.rules.add(sample_rule("Daily breakout"))

        replaced = handles.rules.replace(stored.id, sample_rule("Daily breakout", cooldown_bars=7))

        assert replaced.rule.cooldown_bars == 7
        assert replaced.created_at == START
        assert replaced.updated_at == START + STEP


def test_replace_that_changes_nothing_leaves_updated_at_untouched(database: Database) -> None:
    rule = sample_rule("Daily breakout")
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, STEP))
        stored = handles.rules.add(rule)

        replaced = handles.rules.replace(stored.id, rule, enabled=False)

        assert replaced == stored


def test_replace_can_switch_the_flag_alone(database: Database) -> None:
    rule = sample_rule("Daily breakout")
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, STEP))
        stored = handles.rules.add(rule)

        replaced = handles.rules.replace(stored.id, rule, enabled=True)

        assert replaced.enabled is True
        assert replaced.updated_at == START + STEP


def test_replace_can_rename_a_rule_but_not_onto_another_name(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        first = handles.rules.add(sample_rule("First"))
        handles.rules.add(sample_rule("Second"))

        renamed = handles.rules.replace(first.id, sample_rule("Third"))

        assert renamed.name == "Third"

        with pytest.raises(DuplicateRuleNameError, match="Second"):
            handles.rules.replace(first.id, sample_rule("Second"))


def test_replace_on_a_missing_rule_is_refused(database: Database) -> None:
    with database.session() as session, pytest.raises(UnknownRuleError, match="404"):
        repositories(session).rules.replace(404, sample_rule())


def test_the_timeframe_of_an_assigned_rule_cannot_change(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        stored = handles.rules.add(sample_rule("Daily breakout", timeframe="1d"))
        handles.assignments.assign(ticker.id, stored.id)

    with database.session() as session:
        handles = repositories(session)

        with pytest.raises(AssignedTimeframeError) as error:
            handles.rules.replace(stored.id, sample_rule("Daily breakout", timeframe="1h"))

        assert "1d" in str(error.value)
        assert "1h" in str(error.value)
        assert error.value.assignment_count == 1


def test_the_timeframe_changes_once_the_rule_has_no_assignment(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        stored = handles.rules.add(sample_rule("Daily breakout", timeframe="1d"))
        handles.assignments.assign(ticker.id, stored.id)
        handles.assignments.unassign(ticker.id, stored.id)

        replaced = handles.rules.replace(stored.id, sample_rule("Daily breakout", timeframe="1h"))

        assert replaced.rule.timeframe is Timeframe.H1


def test_set_enabled_moves_updated_at_only_when_the_flag_changes(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session, clock=fixed_clock(START, STEP))
        stored = handles.rules.add(sample_rule("Daily breakout"))

        assert handles.rules.set_enabled(stored.id, False) == stored

        enabled = handles.rules.set_enabled(stored.id, True)

        assert enabled.enabled is True
        assert enabled.updated_at == START + STEP


def test_set_enabled_on_a_missing_rule_is_refused(database: Database) -> None:
    with database.session() as session, pytest.raises(UnknownRuleError, match="404"):
        repositories(session).rules.set_enabled(404, True)


def test_delete_reports_whether_a_row_was_removed_and_cascades(database: Database) -> None:
    with database.session() as session:
        handles = repositories(session)
        ticker = handles.tickers.add("AAPL", Timeframe.D1)
        stored = handles.rules.add(sample_rule("Daily breakout"))
        handles.assignments.assign(ticker.id, stored.id)

    with database.session() as session:
        handles = repositories(session)

        assert handles.rules.delete(stored.id) is True
        assert handles.rules.delete(stored.id) is False

    with database.session() as session:
        handles = repositories(session)

        assert handles.rules.list_all() == ()
        assert handles.assignments.list_all() == ()
        assert handles.tickers.get(ticker.id) is not None


def test_a_failed_unit_of_work_stores_no_rule(database: Database) -> None:
    def failing_unit_of_work() -> None:
        with database.session() as session:
            repositories(session).rules.add(sample_rule())
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        failing_unit_of_work()

    with database.session() as session:
        assert repositories(session).rules.list_all() == ()


# --- T6: the stored document ---------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [payload for _, payload in valid_payloads()],
    ids=[label for label, _ in valid_payloads()],
)
def test_every_valid_payload_is_stored_canonically_and_re_parses_equal(
    database: Database, payload: Payload
) -> None:
    rule = parse_rule(payload)
    with database.session() as session:
        stored = repositories(session).rules.add(rule)

    with database.session() as session:
        document = session.execute(DERIVED_COLUMNS, {"id": stored.id}).one().definition_json
        reloaded = repositories(session).rules.get(stored.id)

    assert document == canonical_text(payload)
    assert parse_rule(document) == rule
    assert reloaded is not None
    assert reloaded.rule == rule


def test_a_serialization_of_another_rule_is_caught_before_the_flush(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard compares the rule, not just the text: a valid document is not enough."""
    other = canonical_text(dump_rule(sample_rule("Another rule")))
    monkeypatch.setattr(rules_module, "dump_rule_document", lambda rule: other)

    with database.session() as session, pytest.raises(StoredRuleError) as error:
        repositories(session).rules.add(sample_rule("Daily breakout"))

    assert [problem.kind.value for problem in error.value.problems] == ["invalid_rule"]
    assert "Another rule" not in str(error.value)
    # Nothing was stored, so the message must not claim a row (it has no id to name).
    assert error.value.rule_id is None
    assert str(error.value).startswith("the rule document is not valid")

    with database.session() as session:
        assert session.execute(RULE_COUNT).scalar() == 0


def test_an_error_message_bounds_the_rule_name_and_the_problem_list() -> None:
    """Every message is one bounded English line, whatever the input was."""
    duplicate = DuplicateRuleNameError("n" * 300)
    problems = tuple(
        RuleProblem(kind=RuleErrorKind.INVALID_RULE, path=f"field{index}", message="broken")
        for index in range(8)
    )
    stored = StoredRuleError(7, problems)

    escaped = DuplicateRuleNameError("" * 300)

    assert len(str(duplicate)) < 120
    assert str(duplicate).endswith("...' already exists")
    assert len(str(escaped)) < 120
    assert "and 3 more" in str(stored)
    assert len(stored.problems) == 8


def test_a_serialization_that_does_not_re_parse_writes_nothing(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-parse guard of decision D87, exercised on a tampered serializer."""

    def tampered(rule: object) -> str:
        return json.dumps({"name": "tampered"})

    monkeypatch.setattr(rules_module, "dump_rule_document", tampered)

    with database.session() as session, pytest.raises(StoredRuleError) as error:
        repositories(session).rules.add(sample_rule())

    assert "tampered" not in str(error.value)
    assert error.value.rule_id is None
    assert "stored" not in str(error.value)

    with database.session() as session:
        assert session.execute(RULE_COUNT).scalar() == 0


CORRUPT_DOCUMENT = '{"name": "a rule whose text must never be echoed"}'


def test_a_stored_document_that_no_longer_parses_fails_loudly(database: Database) -> None:
    """Decision D93: the row is never skipped, and the document never reaches the message."""
    with database.session() as session:
        stored = repositories(session).rules.add(sample_rule("Daily breakout"))

    with database.session() as session:
        session.execute(
            text("UPDATE rules SET definition_json = :document WHERE id = :id"),
            {"document": CORRUPT_DOCUMENT, "id": stored.id},
        )

    with database.session() as session, pytest.raises(StoredRuleError) as error:
        repositories(session).rules.get(stored.id)

    assert error.value.rule_id == stored.id
    assert str(error.value).startswith(f"the document of rule {stored.id} is not valid")
    assert "must never be echoed" not in str(error.value)
    assert CORRUPT_DOCUMENT not in str(error.value)
    # ``raise ... from None``: no chained validation error carries the document either.
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert {problem.kind.value for problem in error.value.problems} == {"missing_field"}
    assert [problem.path for problem in error.value.problems] == [
        "signal",
        "timeframe",
        "conditions",
    ]


def test_a_corrupt_document_is_reported_by_the_listing_too(database: Database) -> None:
    with database.session() as session:
        stored = repositories(session).rules.add(sample_rule("Daily breakout"))

    with database.session() as session:
        session.execute(
            text("UPDATE rules SET definition_json = 'not json' WHERE id = :id"),
            {"id": stored.id},
        )

    with database.session() as session, pytest.raises(StoredRuleError, match=str(stored.id)):
        repositories(session).rules.list_all()
