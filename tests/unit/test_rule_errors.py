"""Tests of the rule error contract, the pydantic mapper and text parsing (spec 006, T6, T7).

The mapper is exercised with hand-built pydantic error entries, so every row of the spec 006
Design 8 table is covered without depending on a particular rule payload, and then on real
documents for the ordering, the noise filter and the bounds (AC14, AC15). T7 covers the guards
of ``parse_rule`` on text, bytes and hostile mappings (AC16).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

import pytest

from tests.fixtures.rules import (
    condition,
    indicator_operand,
    price_operand,
    rule_payload,
    simple_condition,
    value_operand,
)
from trading_bot.domain.indicators.errors import IndicatorError, IndicatorErrorKind
from trading_bot.domain.rules.errors import (
    MAX_MESSAGE_LENGTH,
    MAX_PATH_LENGTH,
    MAX_PATH_SEGMENT_LENGTH,
    MAX_PROBLEMS,
    RuleErrorKind,
    RuleProblem,
    RuleValidationError,
    _build_path,
)
from trading_bot.domain.rules.schema import (
    MAX_RULE_BYTES,
    _mapped_problem,
    _RuleSemanticError,
    parse_rule,
)

# --- RuleProblem ---------------------------------------------------------------------------


def test_a_problem_keeps_its_kind_path_and_message() -> None:
    problem = RuleProblem(
        kind=RuleErrorKind.WRONG_TYPE, path="conditions.all[0].left", message="wrong type"
    )

    assert problem.kind is RuleErrorKind.WRONG_TYPE
    assert problem.path == "conditions.all[0].left"
    assert problem.message == "wrong type"


def test_a_problem_is_frozen() -> None:
    problem = RuleProblem(kind=RuleErrorKind.INVALID_RULE, path="", message="broken")

    with pytest.raises(AttributeError):
        problem.path = "other"  # type: ignore[misc]


def test_a_problem_renders_as_one_line() -> None:
    problem = RuleProblem(kind=RuleErrorKind.MISSING_FIELD, path="name", message="Field required")

    assert str(problem) == "name: Field required"


def test_a_problem_without_a_path_renders_its_message_only() -> None:
    problem = RuleProblem(kind=RuleErrorKind.NOT_AN_OBJECT, path="", message="not an object")

    assert str(problem) == "not an object"


def test_a_problem_truncates_a_long_message() -> None:
    problem = RuleProblem(kind=RuleErrorKind.INVALID_RULE, path="", message="x" * 5_000)

    assert len(problem.message) == MAX_MESSAGE_LENGTH
    assert problem.message.endswith("...")


def test_a_problem_replaces_control_characters_in_its_message() -> None:
    problem = RuleProblem(
        kind=RuleErrorKind.INVALID_RULE, path="", message="line\nbreak\tand\x1b[31m escape"
    )

    assert "\n" not in problem.message
    assert "\t" not in problem.message
    assert "\x1b" not in problem.message
    assert problem.message == "line break and [31m escape"


def test_a_problem_truncates_a_long_path_at_a_segment_boundary() -> None:
    path = ".".join(f"segment{index}" for index in range(40))

    problem = RuleProblem(kind=RuleErrorKind.UNKNOWN_FIELD, path=path, message="unknown")

    assert len(problem.path) <= MAX_PATH_LENGTH
    assert problem.path.split(".")[-1].startswith("segment")


def test_a_problem_sanitizes_control_characters_in_its_path() -> None:
    problem = RuleProblem(kind=RuleErrorKind.UNKNOWN_FIELD, path="a.\nb", message="unknown")

    assert problem.path == "a. b"


def test_a_problem_truncates_a_single_oversized_path_segment() -> None:
    problem = RuleProblem(kind=RuleErrorKind.UNKNOWN_FIELD, path="x" * 500, message="unknown")

    assert len(problem.path) <= MAX_PATH_LENGTH


# --- RuleValidationError -------------------------------------------------------------------


def one_problem(
    kind: RuleErrorKind = RuleErrorKind.INVALID_RULE, path: str = "name", message: str = "broken"
) -> RuleProblem:
    return RuleProblem(kind=kind, path=path, message=message)


def test_the_error_is_a_value_error() -> None:
    error = RuleValidationError([one_problem()])

    assert isinstance(error, ValueError)


def test_the_error_exposes_the_first_problem() -> None:
    first = one_problem(kind=RuleErrorKind.MISSING_FIELD, path="name", message="Field required")
    second = one_problem(kind=RuleErrorKind.UNKNOWN_FIELD, path="nope", message="unknown field")

    error = RuleValidationError([first, second])

    assert error.kind is RuleErrorKind.MISSING_FIELD
    assert error.path == "name"
    assert error.problems == (first, second)


def test_the_error_renders_one_problem_as_path_and_message() -> None:
    error = RuleValidationError([one_problem(path="conditions", message="too many conditions")])

    assert str(error) == "conditions: too many conditions"


def test_the_error_counts_the_remaining_problems() -> None:
    problems = [one_problem(path=f"field{index}") for index in range(3)]

    error = RuleValidationError(problems)

    assert str(error) == "field0: broken (and 2 more problems)"


def test_the_error_uses_the_singular_for_a_single_extra_problem() -> None:
    error = RuleValidationError([one_problem(path="a"), one_problem(path="b")])

    assert str(error) == "a: broken (and 1 more problem)"


def test_the_error_renders_a_problem_without_a_path() -> None:
    error = RuleValidationError([one_problem(path="", message="not an object")])

    assert str(error) == "not an object"


def test_the_error_rendering_is_one_bounded_line() -> None:
    problems = [
        one_problem(path=".".join(f"segment{index}" for index in range(40)), message="x" * 500)
        for _ in range(3)
    ]

    error = RuleValidationError(problems)

    assert "\n" not in str(error)
    assert len(str(error)) <= 400


def test_the_error_keeps_at_most_the_documented_number_of_problems() -> None:
    problems = [one_problem(path=f"field{index}") for index in range(MAX_PROBLEMS + 5)]

    error = RuleValidationError(problems)

    assert len(error.problems) == MAX_PROBLEMS
    assert error.problems[-1].path == f"field{MAX_PROBLEMS - 1}"


def test_an_error_without_problems_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least one problem"):
        RuleValidationError([])


def test_the_error_survives_the_default_exception_copy() -> None:
    error = RuleValidationError([one_problem()])

    rebuilt = type(error)(*error.args)

    assert rebuilt.problems == error.problems
    assert str(rebuilt) == str(error)


def test_the_bounds_are_the_documented_ones() -> None:
    assert (MAX_PROBLEMS, MAX_PATH_SEGMENT_LENGTH, MAX_PATH_LENGTH, MAX_MESSAGE_LENGTH) == (
        20,
        32,
        160,
        200,
    )


def test_every_kind_has_a_snake_case_value() -> None:
    assert {kind.value for kind in RuleErrorKind} == {
        "invalid_json",
        "too_large",
        "too_deep",
        "duplicate_key",
        "not_an_object",
        "missing_field",
        "unknown_field",
        "wrong_type",
        "out_of_range",
        "unknown_value",
        "invalid_operand",
        "invalid_group",
        "nested_too_deep",
        "too_many_conditions",
        "degenerate_condition",
        "invalid_name",
        "unknown_indicator",
        "invalid_parameter",
        "invalid_output",
        "invalid_rule",
    }


# --- T6: mapping hand-built pydantic errors (AC14) -----------------------------------------


def details(error_type: str, loc: tuple[object, ...] = ("field",), **extra: object) -> object:
    """One entry as ``ValidationError.errors(include_url=False)`` returns them."""
    entry: dict[str, object] = {
        "type": error_type,
        "loc": loc,
        "msg": "a message",
        "input": "the raw input",
    }
    entry.update(extra)
    return entry


def rejection(payload: object) -> RuleValidationError:
    with pytest.raises(RuleValidationError) as caught:
        parse_rule(payload)  # type: ignore[arg-type]
    return caught.value


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        ("missing", RuleErrorKind.MISSING_FIELD),
        ("extra_forbidden", RuleErrorKind.UNKNOWN_FIELD),
        ("string_type", RuleErrorKind.WRONG_TYPE),
        ("int_type", RuleErrorKind.WRONG_TYPE),
        ("float_type", RuleErrorKind.WRONG_TYPE),
        ("bool_type", RuleErrorKind.WRONG_TYPE),
        ("model_type", RuleErrorKind.WRONG_TYPE),
        ("dict_type", RuleErrorKind.WRONG_TYPE),
        ("list_type", RuleErrorKind.WRONG_TYPE),
        ("tuple_type", RuleErrorKind.WRONG_TYPE),
        ("is_instance_of", RuleErrorKind.WRONG_TYPE),
        ("model_attributes_type", RuleErrorKind.WRONG_TYPE),
        ("invalid_key", RuleErrorKind.WRONG_TYPE),
        ("greater_than_equal", RuleErrorKind.OUT_OF_RANGE),
        ("less_than_equal", RuleErrorKind.OUT_OF_RANGE),
        ("greater_than", RuleErrorKind.OUT_OF_RANGE),
        ("less_than", RuleErrorKind.OUT_OF_RANGE),
        ("string_too_short", RuleErrorKind.OUT_OF_RANGE),
        ("string_too_long", RuleErrorKind.OUT_OF_RANGE),
        ("too_short", RuleErrorKind.OUT_OF_RANGE),
        ("too_long", RuleErrorKind.OUT_OF_RANGE),
        ("finite_number", RuleErrorKind.OUT_OF_RANGE),
        ("enum", RuleErrorKind.UNKNOWN_VALUE),
        ("literal_error", RuleErrorKind.UNKNOWN_VALUE),
    ],
)
def test_every_mapped_pydantic_type_gives_its_kind(error_type: str, kind: RuleErrorKind) -> None:
    _, problem = _mapped_problem(details(error_type))  # type: ignore[arg-type]

    assert (problem.kind, problem.path, problem.message) == (kind, "field", "a message")


def test_an_unknown_pydantic_type_degrades_to_the_catch_all() -> None:
    entry = details("a_type_added_by_a_future_pydantic")

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert problem.kind is RuleErrorKind.INVALID_RULE


def test_a_wrong_type_on_the_whole_document_becomes_not_an_object() -> None:
    _, problem = _mapped_problem(details("model_type", loc=()))  # type: ignore[arg-type]

    assert (problem.kind, problem.path) == (RuleErrorKind.NOT_AN_OBJECT, "")


def test_a_foreign_value_error_loses_the_pydantic_prefix() -> None:
    entry = details("value_error", msg="Value error, something went wrong")

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert (problem.kind, problem.message) == (RuleErrorKind.INVALID_RULE, "something went wrong")


@pytest.mark.parametrize(
    ("indicator_kind", "kind"),
    [
        (IndicatorErrorKind.UNKNOWN_INDICATOR, RuleErrorKind.UNKNOWN_INDICATOR),
        (IndicatorErrorKind.UNKNOWN_PARAMETER, RuleErrorKind.INVALID_PARAMETER),
        (IndicatorErrorKind.WRONG_TYPE, RuleErrorKind.INVALID_PARAMETER),
        (IndicatorErrorKind.OUT_OF_RANGE, RuleErrorKind.INVALID_PARAMETER),
        (IndicatorErrorKind.CONSTRAINT, RuleErrorKind.INVALID_PARAMETER),
        (IndicatorErrorKind.UNKNOWN_OUTPUT, RuleErrorKind.INVALID_OUTPUT),
        (IndicatorErrorKind.MISSING_OUTPUT, RuleErrorKind.INVALID_OUTPUT),
    ],
)
def test_an_indicator_error_keeps_its_kind_and_message(
    indicator_kind: IndicatorErrorKind, kind: RuleErrorKind
) -> None:
    original = IndicatorError(indicator_kind, "the registry message", indicator="rsi")
    entry = details("value_error", loc=("left", "params"), ctx={"error": original})

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert (problem.kind, problem.message) == (kind, "the registry message")
    assert problem.path == "left.params"


def test_a_parameter_error_extends_the_path_with_the_parameter_name() -> None:
    original = IndicatorError(
        IndicatorErrorKind.OUT_OF_RANGE, "out of range", indicator="rsi", parameter="length"
    )
    entry = details("value_error", loc=("left", "params"), ctx={"error": original})

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert problem.path == "left.params.length"


def test_an_output_error_does_not_extend_the_path() -> None:
    original = IndicatorError(
        IndicatorErrorKind.UNKNOWN_OUTPUT, "unknown output", indicator="rsi", output="nope"
    )
    entry = details("value_error", loc=("left", "output"), ctx={"error": original})

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert problem.path == "left.output"


def test_a_semantic_error_keeps_the_kind_it_carries() -> None:
    original = _RuleSemanticError(RuleErrorKind.NESTED_TOO_DEEP, "too deep")
    entry = details("value_error", loc=("conditions",), ctx={"error": original})

    _, problem = _mapped_problem(entry)  # type: ignore[arg-type]

    assert (problem.kind, problem.message) == (RuleErrorKind.NESTED_TOO_DEEP, "too deep")


@pytest.mark.parametrize(
    ("loc", "kind"),
    [
        (("conditions", "group.all", "all", 0, "item.condition", "left"), "invalid_operand"),
        (("conditions", "group.all", "all", 0, "item.condition", "right"), "invalid_operand"),
        (("conditions", "group.all", "all", 0), "invalid_group"),
        (("conditions",), "invalid_group"),
        ((), "invalid_group"),
    ],
)
def test_a_missing_union_tag_depends_on_the_path_tail(loc: tuple[object, ...], kind: str) -> None:
    _, problem = _mapped_problem(details("union_tag_not_found", loc=loc))  # type: ignore[arg-type]

    assert problem.kind.value == kind
    assert "_operand_tag" not in problem.message
    assert "discriminator" not in problem.message


def test_the_union_tags_never_reach_a_path() -> None:
    loc = (
        "conditions",
        "group.all",
        "all",
        0,
        "item.condition",
        "left",
        "operand.indicator",
        "params",
    )

    assert _build_path(loc) == "conditions.all[0].left.params"


@pytest.mark.parametrize("key", ["\u202e\n", "x" * 500, "with space", "$ref", "1234"])
def test_a_path_segment_is_bounded_for_any_key(key: str) -> None:
    path = _build_path(("conditions", key))
    segment = path.removeprefix("conditions.")

    assert len(segment) <= MAX_PATH_SEGMENT_LENGTH
    assert segment.isprintable()


def test_a_non_string_mapping_key_is_rendered_as_a_segment() -> None:
    assert _build_path(("conditions", 1.5)) == "conditions.'1.5'"


def test_an_index_is_rendered_with_brackets() -> None:
    assert _build_path(("all", 3)) == "all[3]"


def test_a_built_path_is_truncated_at_a_segment_boundary() -> None:
    path = _build_path(tuple(f"segment{index}" for index in range(40)))

    assert len(path) <= MAX_PATH_LENGTH
    assert path.split(".")[-1].startswith("segment")


# --- T6: the mapper on real documents (AC14, AC15) -----------------------------------------


def test_problems_are_reported_in_document_order() -> None:
    payload = rule_payload(
        {"all": [{"left": indicator_operand("nope"), "op": "==", "right": {"value": "30"}}]},
        name="",
    )

    error = rejection(payload)

    assert [problem.path for problem in error.problems] == [
        "name",
        "conditions.all[0].left.indicator",
        "conditions.all[0].op",
        "conditions.all[0].right.value",
    ]


def test_the_container_companion_problem_is_dropped() -> None:
    payload = rule_payload({"all": [condition(price_operand(), "==", value_operand(1))]})

    error = rejection(payload)

    assert [problem.path for problem in error.problems] == ["conditions.all[0].op"]


def test_an_empty_group_keeps_its_own_problem() -> None:
    error = rejection(rule_payload({"all": []}))

    assert [problem.path for problem in error.problems] == ["conditions.all"]


def test_every_problem_of_a_rejected_document_is_bounded() -> None:
    hostile = "x" * 400
    payload = rule_payload({"all": [{"left": {"price": "close"}, "op": "<", hostile: 1}]})

    error = rejection(payload)

    for problem in error.problems:
        assert len(problem.path) <= MAX_PATH_LENGTH
        assert len(problem.message) <= MAX_MESSAGE_LENGTH
        assert problem.path.isprintable()
        assert problem.message.isprintable()
        assert hostile not in problem.path


def test_a_rejected_document_never_echoes_its_raw_input() -> None:
    long_value = "a" * 90
    payload = rule_payload(
        {"all": [{"left": {"price": long_value}, "op": "<", "right": {"value": 1}}]}
    )

    error = rejection(payload)

    assert long_value not in str(error)
    assert all(long_value not in problem.message for problem in error.problems)


# --- T7: parsing text and bytes (AC16) ------------------------------------------------------


def test_valid_text_and_bytes_parse() -> None:
    text = json.dumps(rule_payload({"all": [simple_condition()]}))

    assert parse_rule(text) == parse_rule(text.encode())


def test_invalid_utf8_bytes_are_rejected() -> None:
    error = rejection(b'{"name": "\xff"}')

    assert error.kind is RuleErrorKind.INVALID_JSON
    assert "UTF-8" in error.problems[0].message


@pytest.mark.parametrize("text", ["{", "", "{'name': 'x'}", '{"a": 1} trailing'])
def test_malformed_json_is_rejected(text: str) -> None:
    error = rejection(text)

    assert error.kind is RuleErrorKind.INVALID_JSON
    assert "line" in error.problems[0].message


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_literals_are_rejected(constant: str) -> None:
    error = rejection(f'{{"name": "x", "value": {constant}}}')

    assert error.kind is RuleErrorKind.INVALID_JSON
    assert "NaN" in error.problems[0].message


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "a", "name": "b"}',
        '{"conditions": {"all": [], "all": []}}',
        '{"conditions": {"all": [{"left": {"price": "close"}, "left": {"price": "open"}}]}}',
    ],
)
def test_duplicate_keys_are_rejected_at_every_level(text: str) -> None:
    error = rejection(text)

    assert error.kind is RuleErrorKind.DUPLICATE_KEY


def test_a_duplicate_key_is_echoed_bounded() -> None:
    key = "k" * 400

    error = rejection(f'{{"{key}": 1, "{key}": 2}}')

    assert error.kind is RuleErrorKind.DUPLICATE_KEY
    assert len(error.problems[0].message) <= MAX_MESSAGE_LENGTH


def test_a_deeply_nested_payload_is_rejected_without_a_recursion_error() -> None:
    text = "[" * 5_000 + "]" * 5_000

    assert rejection(text).kind is RuleErrorKind.TOO_DEEP


def test_a_payload_above_the_byte_cap_is_rejected() -> None:
    padding = "x" * (MAX_RULE_BYTES + 1)
    text = f'{{"name": "{padding}"}}'

    assert rejection(text).kind is RuleErrorKind.TOO_LARGE
    assert rejection(text.encode()).kind is RuleErrorKind.TOO_LARGE


def test_a_payload_that_is_long_only_in_utf8_bytes_is_rejected() -> None:
    padding = "é" * (MAX_RULE_BYTES - 100)
    text = f'{{"name": "{padding}"}}'

    assert len(text) <= MAX_RULE_BYTES
    assert rejection(text).kind is RuleErrorKind.TOO_LARGE


def test_a_lone_surrogate_does_not_break_the_byte_count() -> None:
    error = rejection('{"name": "\ud800"}')

    assert error.kind is not RuleErrorKind.TOO_LARGE


@pytest.mark.parametrize("document", ["[]", '"text"', "5", "null", "true"])
def test_a_json_document_that_is_not_an_object_is_rejected(document: str) -> None:
    assert rejection(document).kind is RuleErrorKind.NOT_AN_OBJECT


def test_a_mapping_with_a_non_string_key_is_rejected() -> None:
    payload = rule_payload({"all": [simple_condition()]})
    payload[5] = "x"  # type: ignore[index]

    assert rejection(payload).kind is RuleErrorKind.WRONG_TYPE


def test_a_mapping_that_raises_a_type_error_is_rejected() -> None:
    class HostileMapping(Mapping[str, object]):
        """A mapping that breaks while pydantic reads it: nothing may escape ``parse_rule``."""

        def __getitem__(self, key: str) -> object:
            raise TypeError("hostile mapping")

        def __iter__(self) -> Iterator[str]:
            raise TypeError("hostile mapping")

        def __len__(self) -> int:
            return 1

    error = rejection(HostileMapping())

    assert error.kind is RuleErrorKind.WRONG_TYPE


def _mapping_raising(exception: BaseException) -> Mapping[str, object]:
    """A mapping that iterates cleanly but raises ``exception`` as soon as a key is read."""

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise exception

        def __iter__(self) -> Iterator[str]:
            return iter(["name"])

        def __len__(self) -> int:
            return 1

    return HostileMapping()


@pytest.mark.parametrize(
    "exception",
    [RuntimeError("boom"), ValueError("boom"), OSError("boom"), StopIteration("boom")],
)
def test_a_mapping_that_raises_an_unexpected_exception_is_rejected(
    exception: Exception,
) -> None:
    """``parse_rule`` answers with a rejection whatever a hostile mapping raises while it is read.

    Only ``RuleValidationError`` may escape (Design 9.5), because the API (#22) reads a request
    body as a mapping and catches nothing else.
    """
    error = rejection(_mapping_raising(exception))

    assert error.kind is RuleErrorKind.INVALID_RULE
    assert error.path == ""


def test_the_message_of_an_unexpected_exception_names_its_type_but_not_its_text() -> None:
    """The type helps debugging; the text may carry caller data, so it is never echoed."""
    error = rejection(_mapping_raising(RuntimeError("some caller detail")))

    assert "RuntimeError" in str(error)
    assert "some caller detail" not in str(error)


def test_a_mapping_that_raises_a_base_exception_does_not_become_a_rejection() -> None:
    """Interrupts and exits are control flow, never a verdict about the document."""
    with pytest.raises(KeyboardInterrupt):
        parse_rule(_mapping_raising(KeyboardInterrupt()))


def test_a_parameter_name_that_is_not_a_string_is_rejected() -> None:
    payload = rule_payload(
        {
            "all": [
                {"left": {"indicator": "rsi", "params": {5: 1}}, "op": "<", "right": {"value": 3}}
            ]
        }
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.WRONG_TYPE
    assert error.path == "conditions.all[0].left.params"
