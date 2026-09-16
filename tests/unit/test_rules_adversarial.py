"""Adversarial payloads through ``parse_rule`` (spec 006, T11, AC15, AC16).

Code-shaped strings, dunder/prototype-pollution-style keys, megabyte-sized inputs, huge
integers, hostile Mappings and hostile Unicode. The goal is not to find new ``RuleErrorKind``
values (the fixtures in ``tests.fixtures.rules`` already exercise those); it is to prove that
none of this ever executes anything (CLAUDE.md rule 8: no ``eval``/``exec``), that every message
and path stays inside the documented bounds (AC15), and that nothing but ``RuleValidationError``
ever escapes ``parse_rule`` (AC16) -- including through the ``Mapping`` input path, which is not
protected by the text-parsing guards of Design §9 (no byte cap, no JSON recursion guard).
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
from trading_bot.domain.rules.errors import (
    MAX_MESSAGE_LENGTH,
    MAX_PATH_LENGTH,
    MAX_PATH_SEGMENT_LENGTH,
    MAX_PROBLEMS,
    RuleErrorKind,
    RuleValidationError,
)
from trading_bot.domain.rules.schema import MAX_NAME_LENGTH, MAX_RULE_BYTES, parse_rule

CODE_STRINGS = (
    "__import__('os').system('echo pwned')",
    "eval('1 + 1')",
    "exec('import os')",
    "{{7*7}}",
    "${7*7}",
    "'; DROP TABLE rules; --",
    "<script>alert(1)</script>",
)
DUNDER_KEYS = ("__class__", "__init__", "__proto__", "__dict__", "$ref", "$schema", "constructor")
# Explicit escapes, never raw characters: a zero-width space (U+200B), a right-to-left override
# (U+202E, the Trojan Source pattern) and a byte-order mark (U+FEFF) as literal bytes would make
# this file itself render as reordered or invisible text on GitHub.
HOSTILE_UNICODE = "\x1b[31m" + "\u200b" + "\u202e" + "hostile" + "\ufeff"


def rejection(payload: object) -> RuleValidationError:
    with pytest.raises(RuleValidationError) as caught:
        parse_rule(payload)  # type: ignore[arg-type]
    return caught.value


def _assert_bounded(error: RuleValidationError) -> None:
    for problem in error.problems:
        assert len(problem.path) <= MAX_PATH_LENGTH
        assert len(problem.message) <= MAX_MESSAGE_LENGTH
        assert problem.path.isprintable()
        assert problem.message.isprintable()
        assert "\n" not in problem.message
        assert "\n" not in problem.path


# --- Code-shaped strings never execute (CLAUDE.md rule 8) ------------------------------------


@pytest.mark.parametrize("code", CODE_STRINGS)
def test_a_code_shaped_name_is_accepted_and_stored_literally(code: str) -> None:
    """A rule name is free-form user text (Design §7); it is never interpreted."""
    rule = parse_rule(rule_payload({"all": [simple_condition()]}, name=code))

    assert rule.name == code  # neither executed nor templated


@pytest.mark.parametrize("code", CODE_STRINGS)
def test_a_code_shaped_operator_is_rejected_not_executed(code: str) -> None:
    payload = rule_payload({"all": [condition(price_operand(), code, value_operand(1))]})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_VALUE
    _assert_bounded(error)


@pytest.mark.parametrize("code", [*CODE_STRINGS, "eval", "exec", "__import__", "os.system"])
def test_a_code_shaped_indicator_name_is_rejected_not_executed(code: str) -> None:
    payload = rule_payload({"all": [condition(indicator_operand(code), "<", value_operand(1))]})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_INDICATOR
    if len(code) > MAX_PATH_SEGMENT_LENGTH:
        assert code not in error.problems[0].message  # only a bounded, truncated echo
    _assert_bounded(error)


# --- Dunder and prototype-pollution-style keys are just unknown fields -----------------------


@pytest.mark.parametrize("key", DUNDER_KEYS)
def test_a_dunder_or_prototype_key_at_the_top_level_is_an_unknown_field(key: str) -> None:
    payload = rule_payload({"all": [simple_condition()]}, **{key: "x"})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    _assert_bounded(error)


@pytest.mark.parametrize("key", DUNDER_KEYS)
def test_a_dunder_or_prototype_key_inside_an_operand_is_an_unknown_field(key: str) -> None:
    payload = rule_payload(
        {"all": [{"left": {"price": "close", key: "x"}, "op": "<", "right": value_operand(1)}]}
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    _assert_bounded(error)


# --- Megabyte-sized inputs stay bounded and reasonably fast -----------------------------------


def test_a_one_megabyte_name_via_text_is_rejected_before_parsing() -> None:
    huge_name = "n" * (1024 * 1024)
    payload = rule_payload({"all": [simple_condition()]}, name=huge_name)
    text = json.dumps(payload)

    error = rejection(text)

    assert error.kind is RuleErrorKind.TOO_LARGE
    assert huge_name not in str(error)


def test_a_one_megabyte_name_via_a_mapping_is_rejected_on_the_field_bound() -> None:
    """A ``Mapping`` skips the byte cap (Design §9 scopes it to text), but ``name`` has its own."""
    huge_name = "n" * (1024 * 1024)
    payload = rule_payload({"all": [simple_condition()]}, name=huge_name)

    error = rejection(payload)

    assert error.kind is RuleErrorKind.INVALID_NAME
    assert huge_name not in str(error)
    assert len(error.problems[0].message) <= MAX_MESSAGE_LENGTH


def test_a_one_megabyte_unknown_key_via_a_mapping_is_rejected_and_bounded() -> None:
    payload = rule_payload({"all": [simple_condition()]})
    payload["k" * (1024 * 1024)] = 1

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    assert len(error.path) <= MAX_PATH_SEGMENT_LENGTH


def test_a_ten_thousand_character_parameter_name_stays_bounded() -> None:
    payload = rule_payload(
        {"all": [condition(indicator_operand("rsi", {"x" * 10_000: 1}), "<", value_operand(1))]}
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.INVALID_PARAMETER
    _assert_bounded(error)


# --- Huge integers never render in full, never overflow --------------------------------------


def test_a_googol_cooldown_is_rejected_without_rendering_the_full_number() -> None:
    payload = rule_payload({"all": [simple_condition()]}, cooldown_bars=10**100)

    error = rejection(payload)

    assert error.kind is RuleErrorKind.OUT_OF_RANGE
    assert str(10**100) not in error.problems[0].message
    _assert_bounded(error)


def test_a_googol_indicator_parameter_is_rejected_without_rendering_the_full_number() -> None:
    payload = rule_payload(
        {"all": [condition(indicator_operand("rsi", {"length": 10**100}), "<", value_operand(1))]}
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.INVALID_PARAMETER
    assert str(10**100) not in error.problems[0].message
    _assert_bounded(error)


# --- Nested containers as parameter values are rejected, never traversed further -------------


@pytest.mark.parametrize(
    "value",
    [
        [1, 2, 3],
        {"nested": {"deeper": {"deepest": 1}}},
        [[1, 2], [3, 4]],
        {"a": [1, {"b": 2}]},
    ],
)
def test_a_nested_container_as_a_parameter_value_is_rejected(value: object) -> None:
    hostile_params: dict[str, object] = {"length": value}
    left = indicator_operand("rsi", hostile_params)  # type: ignore[arg-type]
    payload = rule_payload({"all": [condition(left, "<", value_operand(1))]})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.INVALID_PARAMETER
    _assert_bounded(error)


# --- ANSI escapes, zero-width and RTL characters in keys are safely escaped -------------------


def test_a_hostile_unicode_unknown_key_is_bounded_and_escaped() -> None:
    payload = rule_payload({"all": [simple_condition()]}, **{HOSTILE_UNICODE: 1})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    assert "\x1b" not in error.path
    assert "\u200b" not in error.path
    assert "\u202e" not in error.path
    _assert_bounded(error)


def test_a_hostile_unicode_parameter_name_is_bounded_and_escaped() -> None:
    payload = rule_payload(
        {"all": [condition(indicator_operand("rsi", {HOSTILE_UNICODE: 1}), "<", value_operand(1))]}
    )

    error = rejection(payload)

    assert error.kind is RuleErrorKind.INVALID_PARAMETER
    assert "\x1b" not in error.problems[0].message
    _assert_bounded(error)


def test_a_hostile_unicode_name_is_rejected_as_an_invalid_name() -> None:
    error = rejection(rule_payload({"all": [simple_condition()]}, name=HOSTILE_UNICODE))

    assert error.kind is RuleErrorKind.INVALID_NAME
    assert "\x1b" not in error.problems[0].message
    _assert_bounded(error)


# --- The Mapping path has no JSON recursion guard, but nesting is bounded by the type ---------


def test_a_deeply_nested_mapping_is_rejected_without_a_recursion_error() -> None:
    """Bypasses ``json.loads`` (and its ``RecursionError`` mapping) entirely: a plain Python
    dict of dicts is built directly, so the only thing standing between this and a stack
    overflow is the type design (two group families instead of recursion, Design §0)."""
    nested: dict[str, object] = {"all": [simple_condition()]}
    for _ in range(10_000):
        nested = {"all": [nested]}
    payload = rule_payload(nested)  # type: ignore[arg-type]

    error = rejection(payload)

    assert error.kind is RuleErrorKind.NESTED_TOO_DEEP
    _assert_bounded(error)


def test_a_mapping_with_many_unknown_keys_is_rejected() -> None:
    """The byte cap only guards text (Design §9); a ``Mapping`` has no equivalent size guard and
    is bounded only by per-field limits instead, so its cost scales with whatever the caller
    already decoded. No wall-clock assertion here (spec 003): a stalled CI runner must
    not fail this test. The actual per-problem cost of this shape of input (every raw pydantic
    error is mapped before ``MAX_PROBLEMS`` truncates the result) is a roughly-linear, not
    quadratic, function of the key count -- tracked as evidence through
    ``pytest --durations``, not asserted here."""
    payload = rule_payload({"all": [simple_condition()]})
    payload.update({f"k{i}": i for i in range(50_000)})

    error = rejection(payload)

    assert error.kind is RuleErrorKind.UNKNOWN_FIELD
    assert len(error.problems) <= MAX_PROBLEMS  # the huge key count never inflates the result


# --- Nothing but RuleValidationError ever escapes parse_rule (AC16) ---------------------------


HOSTILE_PAYLOADS: tuple[object, ...] = (
    None,
    True,
    False,
    3.14,
    ["a", "list"],
    {"name": None},
    {"conditions": {"all": [{"left": {"indicator": None}}]}},
    b"\xff\xfe not utf-8 at all",
    "",
    "{",
    '{"a": NaN}',
)


@pytest.mark.parametrize("payload", HOSTILE_PAYLOADS)
def test_every_hostile_payload_raises_only_rule_validation_error(payload: object) -> None:
    with pytest.raises(RuleValidationError):
        parse_rule(payload)  # type: ignore[arg-type]


# --- A hostile Mapping whose __getitem__ raises something other than TypeError/KeyError -------


def _mapping_raising_on_getitem(exception: BaseException) -> Mapping[str, object]:
    class HostileMapping(Mapping[str, object]):
        """Iterates cleanly (one key, ``"name"``) but explodes as soon as it is read."""

        def __getitem__(self, key: str) -> object:
            raise exception

        def __iter__(self) -> Iterator[str]:
            return iter(["name"])

        def __len__(self) -> int:
            return 1

    return HostileMapping()


@pytest.mark.parametrize(
    "exception", [RuntimeError("boom"), ValueError("boom"), OSError("boom"), StopIteration("boom")]
)
def test_a_hostile_mapping_that_raises_on_getitem_still_raises_rule_validation_error(
    exception: BaseException,
) -> None:
    """``parse_rule`` raises only ``RuleValidationError`` for any input (Design §9.5, the
    ``parse_rule`` docstring), including when a ``Mapping``'s ``__getitem__`` raises an exception
    type that is neither ``TypeError`` nor a ``KeyError`` (``RuntimeError``, plain ``ValueError``,
    ``OSError``, ``StopIteration``, ...). The developer's own red-first tests for this live in
    ``test_rule_errors.py``; this one is independent (same technique, no shared fixture) and
    additionally spot-checks the resulting kind and path below.
    """
    error = rejection(_mapping_raising_on_getitem(exception))

    assert error.kind is RuleErrorKind.INVALID_RULE
    assert error.path == ""
    _assert_bounded(error)


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(1), GeneratorExit()])
def test_a_hostile_mapping_that_raises_a_control_flow_exception_is_not_wrapped(
    exception: BaseException,
) -> None:
    """The wide catch around ``Rule.model_validate`` is ``except Exception``, not
    ``except BaseException``: an interrupt, an exit or a generator close is control flow, never a
    verdict about the document, and must keep propagating so the process can actually stop."""
    with pytest.raises(type(exception)):
        parse_rule(_mapping_raising_on_getitem(exception))


def test_the_unexpected_exception_message_never_contains_its_own_text_however_long() -> None:
    """Only the exception's type name is echoed (bounded); its text never is, however large."""
    hostile_text = "s3cr3t-internal-detail " * 5_000
    error = rejection(_mapping_raising_on_getitem(RuntimeError(hostile_text)))

    assert error.kind is RuleErrorKind.INVALID_RULE
    assert hostile_text not in str(error)
    _assert_bounded(error)


def test_an_unexpected_exception_with_a_hostile_class_name_stays_bounded() -> None:
    """A dynamically built exception type can have an arbitrarily hostile ``__name__``; the
    catch-all echoes it through the same bounded, escaping ``_echo`` as every other rejection."""
    hostile_name = "Weird\nName\x1b[31m" + "X" * 5_000
    hostile_type = type(hostile_name, (RuntimeError,), {})

    error = rejection(_mapping_raising_on_getitem(hostile_type("irrelevant")))

    assert error.kind is RuleErrorKind.INVALID_RULE
    _assert_bounded(error)


def test_a_bug_in_our_own_error_mapper_still_surfaces_instead_of_hiding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wide ``except Exception`` wraps only the ``Rule.model_validate`` call. A real bug in
    our own mapping of a genuine pydantic ``ValidationError`` (a different code path, reached
    through the earlier, more specific ``except ValidationError`` branch) must not silently
    degrade to ``INVALID_RULE``: that would hide a real regression behind a plausible-looking
    rejection instead of failing loudly."""
    import trading_bot.domain.rules.schema as schema_module

    def broken_problems(error: object) -> object:
        raise RuntimeError("a bug in our own mapper, not in the caller's mapping")

    monkeypatch.setattr(schema_module, "_problems", broken_problems)

    with pytest.raises(RuntimeError, match="a bug in our own mapper"):
        parse_rule(rule_payload({"all": [simple_condition()]}, name=5))  # a real ValidationError


def test_a_mapping_name_bound_message_never_contains_the_raw_huge_name() -> None:
    huge_name = "s" * 5_000
    payload = rule_payload({"all": [simple_condition()]}, name=huge_name)

    error = rejection(payload)

    assert huge_name not in str(error)
    for problem in error.problems:
        assert huge_name not in problem.message

    assert MAX_NAME_LENGTH == 80  # sanity: the bound this test relies on has not moved
    assert MAX_RULE_BYTES == 65_536
