"""Schema/model accept-reject agreement (spec 006, T10, AC20).

``rule_json_schema()`` is generated from the same models it describes, but it cannot express
every server-side check (Design §11.4). This file feeds every payload of
``tests.fixtures.rules`` through a real ``jsonschema`` validator and checks:

- every payload ``parse_rule`` accepts also validates against the exported schema (the schema
  is never stricter than the model);
- every payload ``parse_rule`` rejects is also rejected by the schema, **except** an exact,
  pinned set of documented divergences (Design §11.4): non-integral numbers for an integer
  field, the ``less_than`` parameter constraint, the two ``DEGENERATE_CONDITION`` checks, and
  the rule-name/``MAX_CONDITIONS`` checks the schema cannot express. A new divergence (a payload
  not in this pinned set that the schema wrongly accepts) fails the test.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from tests.fixtures.rules import (
    Payload,
    architecture_example,
    canonical_architecture_example,
    invalid_payloads,
    valid_payloads,
)
from trading_bot.domain.rules.errors import RuleErrorKind, RuleValidationError
from trading_bot.domain.rules.json_schema import rule_json_schema
from trading_bot.domain.rules.schema import parse_rule

# The exact, documented gap list (spec 006, Design §11.4). Each entry is one payload label of
# ``tests.fixtures.rules.invalid_payloads()`` that the schema accepts even though the model
# rejects it, tagged with the §11.4 bullet it belongs to. Ground truth: running the real
# ``jsonschema`` validator over every invalid fixture payload gives exactly this set (verified
# independently while writing this test); any change to this set is a genuine divergence.
_DOCUMENTED_GAPS: dict[str, str] = {
    "parameter as a float": "non_integral_number",  # §11.4.1: 14.0 for an integer parameter
    "cooldown as a float": "non_integral_number",  # §11.4.1: 5.0 for cooldown_bars
    "violated parameter constraint": "less_than_constraint",  # §11.4.2: macd fast < slow
    "constant against constant": "degenerate_condition",  # §11.4.3
    "identical operands": "degenerate_condition",  # §11.4.3
    "blank name": "name_or_condition_count",  # §11.4.4: whitespace-only name
    "name of 81 characters": "name_or_condition_count",  # §11.4.4: length bound
    "name with a newline": "name_or_condition_count",  # §11.4.4: printability
    "twenty one conditions": "name_or_condition_count",  # §11.4.4: MAX_CONDITIONS total
}
_DOCUMENTED_GAP_CATEGORIES = frozenset(_DOCUMENTED_GAPS.values())


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(rule_json_schema())


def _valid_labelled_payloads() -> tuple[tuple[str, Payload], ...]:
    return (
        ("architecture example", architecture_example()),
        ("canonical architecture example", canonical_architecture_example()),
        *valid_payloads(),
    )


# --- Every payload the model accepts is accepted by the schema (AC20) -----------------------


@pytest.mark.parametrize(
    ("label", "payload"),
    _valid_labelled_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_valid_payload_matches_the_exported_schema(
    label: str, payload: Payload, validator: Draft202012Validator
) -> None:
    parse_rule(payload)  # sanity: the model does accept it

    validator.validate(payload)  # raises jsonschema.ValidationError if it does not


def test_the_documented_example_is_schema_valid(validator: Draft202012Validator) -> None:
    validator.validate(architecture_example())


# --- Every payload the model rejects is rejected by the schema too, except the pinned gaps ---


@pytest.mark.parametrize(
    ("label", "payload", "kind", "path"),
    invalid_payloads(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_non_gap_invalid_payload_is_also_rejected_by_the_schema(
    label: str, payload: Payload, kind: RuleErrorKind, path: str, validator: Draft202012Validator
) -> None:
    with pytest.raises(RuleValidationError):
        parse_rule(payload)
    if label in _DOCUMENTED_GAPS:
        pytest.skip(f"documented §11.4 gap: {_DOCUMENTED_GAPS[label]}")

    errors = list(validator.iter_errors(payload))

    assert errors, f"{label!r}: the schema unexpectedly accepts a payload the model rejects"


def test_the_schema_accepts_exactly_the_documented_gap_list(
    validator: Draft202012Validator,
) -> None:
    """No new divergence: the accepted-by-schema-but-rejected-by-model set is pinned exactly."""
    accepted_despite_model_rejection = {
        label
        for label, payload, _kind, _path in invalid_payloads()
        if not list(validator.iter_errors(payload))
    }

    assert accepted_despite_model_rejection == set(_DOCUMENTED_GAPS)


def test_the_gap_list_covers_exactly_the_four_documented_categories() -> None:
    """§11.4 documents four kinds of gap; the pinned list must not invent a fifth."""
    expected = {
        "non_integral_number",
        "less_than_constraint",
        "degenerate_condition",
        "name_or_condition_count",
    }

    assert expected == _DOCUMENTED_GAP_CATEGORIES


def test_every_documented_gap_payload_is_still_rejected_by_the_model() -> None:
    """The gap list is about the schema being laxer, never the model: parse_rule always rejects."""
    by_label = {label: payload for label, payload, _kind, _path in invalid_payloads()}

    for label in _DOCUMENTED_GAPS:
        with pytest.raises(RuleValidationError):
            parse_rule(by_label[label])
