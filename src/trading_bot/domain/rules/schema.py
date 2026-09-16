"""The rule document: models, strict validation and the canonical form (spec 006, Design 3-10).

A rule is declarative data: a name, a side, a timeframe, a group of conditions over the
whitelisted indicators of ``domain/indicators`` and an optional cooldown. Nothing here evaluates
an expression (CLAUDE.md rule 8) and nothing reads a candle: evaluation is #7.

``parse_rule`` is the only entry point for untrusted input (text, bytes or a mapping). It fails
with ``RuleValidationError`` and never with another exception type, so the API and the CLI can
answer with field-level problems. ``dump_rule`` writes the canonical form back: every default
filled, ``output`` explicit and every ``value`` a float, so a stored rule keeps its meaning when
a catalog default changes. ``Rule.model_validate`` stays available for in-process construction
from trusted values (#7 tests) and raises pydantic's ``ValidationError`` instead.

The models are validated against the module constant ``REGISTRY``: the normalized parameters
stored in a rule are only meaningful for the catalog that validated them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PlainSerializer,
    Strict,
    Tag,
    ValidationError,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails

from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.errors import IndicatorError, IndicatorErrorKind
from trading_bot.domain.indicators.params import IndicatorParams, ParamValue
from trading_bot.domain.indicators.registry import JsonValue
from trading_bot.domain.rules.errors import (
    RuleErrorKind,
    RuleProblem,
    RuleValidationError,
    _build_path,
    _echo,
)
from trading_bot.domain.signals import Side
from trading_bot.domain.timeframe import Timeframe

__all__ = [
    "MAX_CONDITIONS",
    "MAX_COOLDOWN_BARS",
    "MAX_GROUP_ITEMS",
    "MAX_NAME_LENGTH",
    "MAX_RULE_BYTES",
    "MAX_VALUE_MAGNITUDE",
    "AllGroup",
    "AnyGroup",
    "Condition",
    "GroupMode",
    "IndicatorOperand",
    "NestedAllGroup",
    "NestedAnyGroup",
    "Operand",
    "Operator",
    "PriceField",
    "PriceOperand",
    "RootGroup",
    "RootItem",
    "Rule",
    "ValueOperand",
    "dump_rule",
    "parse_rule",
]

MAX_GROUP_ITEMS: Final = 10  # members of one group
MAX_CONDITIONS: Final = 20  # conditions of a whole rule
MAX_NAME_LENGTH: Final = 80  # characters of a rule name, after stripping
MAX_COOLDOWN_BARS: Final = 500  # closed candles between two signals of the same rule and ticker
MAX_VALUE_MAGNITUDE: Final = 1e15  # magnitude of a constant operand
MAX_RULE_BYTES: Final = 65_536  # UTF-8 bytes of a rule document

_VALUE_ERROR_PREFIX: Final = "Value error, "
_NULLABLE_KEYS: Final = ("params", "output")
_EXPLICIT_NULL: Final = object()  # marks a key written as null, which is not the same as absent

# pydantic error type -> kind. Error types are internal to pydantic, so anything missing here
# degrades to INVALID_RULE instead of crashing after an upgrade (spec 006, Design 8).
_KIND_BY_ERROR_TYPE: Final = MappingProxyType(
    {
        "missing": RuleErrorKind.MISSING_FIELD,
        "extra_forbidden": RuleErrorKind.UNKNOWN_FIELD,
        "string_type": RuleErrorKind.WRONG_TYPE,
        "int_type": RuleErrorKind.WRONG_TYPE,
        "float_type": RuleErrorKind.WRONG_TYPE,
        "bool_type": RuleErrorKind.WRONG_TYPE,
        "model_type": RuleErrorKind.WRONG_TYPE,
        "dict_type": RuleErrorKind.WRONG_TYPE,
        "list_type": RuleErrorKind.WRONG_TYPE,
        "tuple_type": RuleErrorKind.WRONG_TYPE,
        "is_instance_of": RuleErrorKind.WRONG_TYPE,
        "model_attributes_type": RuleErrorKind.WRONG_TYPE,
        "invalid_key": RuleErrorKind.WRONG_TYPE,
        "greater_than_equal": RuleErrorKind.OUT_OF_RANGE,
        "less_than_equal": RuleErrorKind.OUT_OF_RANGE,
        "greater_than": RuleErrorKind.OUT_OF_RANGE,
        "less_than": RuleErrorKind.OUT_OF_RANGE,
        "string_too_short": RuleErrorKind.OUT_OF_RANGE,
        "string_too_long": RuleErrorKind.OUT_OF_RANGE,
        "too_short": RuleErrorKind.OUT_OF_RANGE,
        "too_long": RuleErrorKind.OUT_OF_RANGE,
        "finite_number": RuleErrorKind.OUT_OF_RANGE,
        "enum": RuleErrorKind.UNKNOWN_VALUE,
        "literal_error": RuleErrorKind.UNKNOWN_VALUE,
    }
)
_KIND_BY_INDICATOR_KIND: Final = MappingProxyType(
    {
        IndicatorErrorKind.UNKNOWN_INDICATOR: RuleErrorKind.UNKNOWN_INDICATOR,
        IndicatorErrorKind.UNKNOWN_PARAMETER: RuleErrorKind.INVALID_PARAMETER,
        IndicatorErrorKind.WRONG_TYPE: RuleErrorKind.INVALID_PARAMETER,
        IndicatorErrorKind.OUT_OF_RANGE: RuleErrorKind.INVALID_PARAMETER,
        IndicatorErrorKind.CONSTRAINT: RuleErrorKind.INVALID_PARAMETER,
        IndicatorErrorKind.UNKNOWN_OUTPUT: RuleErrorKind.INVALID_OUTPUT,
        IndicatorErrorKind.MISSING_OUTPUT: RuleErrorKind.INVALID_OUTPUT,
    }
)
# pydantic reports a container as "too short" after discarding the member that failed, so such a
# problem is dropped when a longer path also failed (spec 006, Design 8).
_CONTAINER_ERROR_TYPES: Final = frozenset({"too_short", "too_long"})


class _RuleSemanticError(ValueError):
    """A rule-level rejection raised inside a validator. Its kind survives pydantic's wrapping."""

    kind: RuleErrorKind

    def __init__(self, kind: RuleErrorKind, message: str) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self._message = message

    def __str__(self) -> str:
        return self._message


class Operator(StrEnum):
    """Comparison between the two sides of a condition."""

    LESS = "<"
    LESS_OR_EQUAL = "<="
    GREATER = ">"
    GREATER_OR_EQUAL = ">="
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"

    @property
    def is_crossover(self) -> bool:
        """Whether the operator compares the last two candles instead of only the last one."""
        return self in (Operator.CROSSES_ABOVE, Operator.CROSSES_BELOW)


class PriceField(StrEnum):
    """A candle column usable as an operand. The values are ``OHLCV_COLUMNS`` (spec 004)."""

    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"


class GroupMode(StrEnum):
    """How the members of a group combine."""

    ALL = "all"
    ANY = "any"


class _Model(BaseModel):
    """Base of every rule model: unknown keys rejected, instances immutable and hashable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _serialized_params(params: IndicatorParams) -> dict[str, ParamValue]:
    """The JSON form of validated parameters: a plain object in declaration order.

    ``IndicatorParams`` is a domain type pydantic cannot serialize on its own, so without this
    every ``model_dump(mode="json")`` of a rule with an indicator operand would fail.
    """
    return dict(params)


class IndicatorOperand(_Model):
    """One output of a whitelisted indicator, with its parameters normalized by the registry."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    indicator: str
    # Validated by the registry: read-only, hashable, in declaration order and with the declared
    # defaults filled, so (indicator, params) can key the per-evaluation cache of #7. Only the
    # JSON form is flattened to a plain object; the Python form keeps the typed mapping.
    params: Annotated[
        IndicatorParams,
        WithJsonSchema({"type": "object"}),
        PlainSerializer(_serialized_params, return_type=dict[str, ParamValue], when_used="json"),
    ] = Field(default=None, validate_default=True)
    output: str = Field(default=None, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def _mark_explicit_nulls(cls, data: object) -> object:
        """Tell an omitted optional key from one written as ``null``, which is rejected."""
        if isinstance(data, Mapping) and any(_is_null(data, key) for key in _NULLABLE_KEYS):
            return {
                key: _EXPLICIT_NULL if key in _NULLABLE_KEYS and value is None else value
                for key, value in data.items()
            }
        return data

    @field_validator("indicator", mode="after")
    @classmethod
    def _check_indicator(cls, value: str) -> str:
        REGISTRY.get(value)  # raises UnknownIndicatorError, which pydantic wraps
        return value

    @field_validator("params", mode="before")
    @classmethod
    def _check_params(cls, value: object, info: ValidationInfo) -> object:
        name = info.data.get("indicator")
        if not isinstance(name, str):
            return IndicatorParams()  # the indicator is unknown: report that problem only
        if value is _EXPLICIT_NULL:
            raise _RuleSemanticError(
                RuleErrorKind.WRONG_TYPE, "params must not be null; omit the key instead"
            )
        if value is None:
            return REGISTRY.validate_params(name, {})
        if not isinstance(value, Mapping):
            raise _RuleSemanticError(RuleErrorKind.WRONG_TYPE, "params must be an object")
        for key in value:
            if not isinstance(key, str):
                raise _RuleSemanticError(
                    RuleErrorKind.WRONG_TYPE, "parameter names must be strings"
                )
        return REGISTRY.validate_params(name, value)

    @field_validator("output", mode="before")
    @classmethod
    def _check_output(cls, value: object, info: ValidationInfo) -> object:
        name = info.data.get("indicator")
        if not isinstance(name, str):
            return ""  # the indicator is unknown: report that problem only
        if value is _EXPLICIT_NULL:
            raise _RuleSemanticError(
                RuleErrorKind.WRONG_TYPE, "output must not be null; omit the key instead"
            )
        if value is not None and not isinstance(value, str):
            raise _RuleSemanticError(RuleErrorKind.WRONG_TYPE, "output must be a string")
        return REGISTRY.resolve_output(name, value)

    def warmup(self, *, stable: bool) -> int:
        """Candles this operand needs for a value at the last candle (spec 005, Warmup)."""
        if stable:
            return REGISTRY.stable_warmup(self.indicator, self.params)
        return REGISTRY.warmup(self.indicator, self.params)


class PriceOperand(_Model):
    """A candle column of the evaluated ticker."""

    price: PriceField


class ValueOperand(_Model):
    """A constant. Always stored as a float, so ``30`` and ``30.0`` give the same rule."""

    value: float = Field(allow_inf_nan=False, ge=-MAX_VALUE_MAGNITUDE, le=MAX_VALUE_MAGNITUDE)

    @field_validator("value", mode="before")
    @classmethod
    def _check_number(cls, value: object) -> object:
        # bool is an int in Python and pydantic reads "30" as a number in lax mode; a rule only
        # accepts a JSON number.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _RuleSemanticError(RuleErrorKind.WRONG_TYPE, "value must be a JSON number")
        return value


def _operand_tag(value: object) -> str | None:
    """The union tag of an operand: exactly one of the three keys, or ``None`` (rejected)."""
    if isinstance(value, IndicatorOperand):
        return "operand.indicator"
    if isinstance(value, PriceOperand):
        return "operand.price"
    if isinstance(value, ValueOperand):
        return "operand.value"
    if isinstance(value, Mapping):
        present = [key for key in ("indicator", "price", "value") if key in value]
        if len(present) == 1:
            return f"operand.{present[0]}"
    return None


# The tags are prefixed so they can never collide with a JSON key when a path is built, and a
# callable discriminator emits a plain oneOf in the JSON Schema, leaking no tag name.
type Operand = Annotated[
    Annotated[IndicatorOperand, Tag("operand.indicator")]
    | Annotated[PriceOperand, Tag("operand.price")]
    | Annotated[ValueOperand, Tag("operand.value")],
    Discriminator(_operand_tag),
]


class Condition(_Model):
    """A comparison between two operands. At least one side must depend on the market."""

    left: Operand
    op: Operator
    right: Operand

    @model_validator(mode="before")
    @classmethod
    def _reject_a_group(cls, data: object) -> object:
        """A group where a condition is expected is a third nesting level (spec 006, AC9)."""
        if isinstance(data, Mapping) and any(mode.value in data for mode in GroupMode):
            raise _RuleSemanticError(
                RuleErrorKind.NESTED_TOO_DEEP,
                "a group may only be nested one level; expected a condition here",
            )
        return data

    @model_validator(mode="after")
    def _reject_degenerate(self) -> Condition:
        if isinstance(self.left, ValueOperand) and isinstance(self.right, ValueOperand):
            raise _RuleSemanticError(
                RuleErrorKind.DEGENERATE_CONDITION,
                "a condition compares two constants, so it cannot depend on the market",
            )
        if self.left == self.right:
            raise _RuleSemanticError(
                RuleErrorKind.DEGENERATE_CONDITION,
                "both sides of the condition are the same operand",
            )
        return self

    def warmup(self, *, stable: bool) -> int:
        """Candles the condition needs; a crossover needs one more candle per series side."""
        extra = 1 if self.op.is_crossover else 0
        return max(
            _operand_warmup(self.left, stable=stable, extra=extra),
            _operand_warmup(self.right, stable=stable, extra=extra),
        )


class NestedAllGroup(_Model):
    """A second-level group whose conditions must all hold."""

    all: tuple[Condition, ...] = Field(min_length=1, max_length=MAX_GROUP_ITEMS)

    @property
    def mode(self) -> Literal[GroupMode.ALL]:
        return GroupMode.ALL

    @property
    def members(self) -> tuple[Condition, ...]:
        return self.all


class NestedAnyGroup(_Model):
    """A second-level group where one condition is enough."""

    any: tuple[Condition, ...] = Field(min_length=1, max_length=MAX_GROUP_ITEMS)

    @property
    def mode(self) -> Literal[GroupMode.ANY]:
        return GroupMode.ANY

    @property
    def members(self) -> tuple[Condition, ...]:
        return self.any


def _item_tag(value: object) -> str | None:
    """The union tag of a root group member: a condition or a second-level group."""
    if isinstance(value, Condition):
        return "item.condition"
    if isinstance(value, NestedAllGroup):
        return "item.all"
    if isinstance(value, NestedAnyGroup):
        return "item.any"
    if isinstance(value, Mapping):
        present = [mode.value for mode in GroupMode if mode.value in value]
        if len(present) == 1:
            return f"item.{present[0]}"
        if not present:
            return "item.condition"
    return None


type RootItem = Annotated[
    Annotated[Condition, Tag("item.condition")]
    | Annotated[NestedAllGroup, Tag("item.all")]
    | Annotated[NestedAnyGroup, Tag("item.any")],
    Discriminator(_item_tag),
]


class AllGroup(_Model):
    """The root group when every member must hold."""

    all: tuple[RootItem, ...] = Field(min_length=1, max_length=MAX_GROUP_ITEMS)

    @property
    def mode(self) -> Literal[GroupMode.ALL]:
        return GroupMode.ALL

    @property
    def members(self) -> tuple[RootItem, ...]:
        return self.all


class AnyGroup(_Model):
    """The root group when one member is enough."""

    any: tuple[RootItem, ...] = Field(min_length=1, max_length=MAX_GROUP_ITEMS)

    @property
    def mode(self) -> Literal[GroupMode.ANY]:
        return GroupMode.ANY

    @property
    def members(self) -> tuple[RootItem, ...]:
        return self.any


def _group_tag(value: object) -> str | None:
    """The union tag of the root group: exactly one of ``all`` or ``any``."""
    if isinstance(value, AllGroup):
        return "group.all"
    if isinstance(value, AnyGroup):
        return "group.any"
    if isinstance(value, Mapping):
        present = [mode.value for mode in GroupMode if mode.value in value]
        if len(present) == 1:
            return f"group.{present[0]}"
    return None


type RootGroup = Annotated[
    Annotated[AllGroup, Tag("group.all")] | Annotated[AnyGroup, Tag("group.any")],
    Discriminator(_group_tag),
]


class Rule(_Model):
    """A notification rule: when its conditions hold on a closed candle, the bot warns the user.

    The bot never places an order (CLAUDE.md rule 1). Build one from untrusted input with
    ``parse_rule``; ``model_validate`` raises pydantic's ``ValidationError`` instead.

    ``dump_rule`` is the canonical serialization. ``model_dump(mode="json")`` and
    ``model_dump_json()`` produce the same document (a test pins the equality), so storing a rule
    (#12) or encoding it in a response (#22) through pydantic is safe; ``model_dump()`` in Python
    mode keeps ``params`` as the typed ``IndicatorParams`` that #7 uses as a cache key.
    """

    name: str
    signal: Side
    timeframe: Timeframe
    conditions: RootGroup
    cooldown_bars: Annotated[int, Strict()] = Field(default=0, ge=0, le=MAX_COOLDOWN_BARS)

    @field_validator("name", mode="after")
    @classmethod
    def _check_name(cls, value: str) -> str:
        stripped = value.strip()
        # The name is shown in Telegram, the dashboard and logs: bound it and forbid control
        # characters. Accented names are fine, they are user data and not a repository artifact.
        if not 0 < len(stripped) <= MAX_NAME_LENGTH or not stripped.isprintable():
            raise _RuleSemanticError(
                RuleErrorKind.INVALID_NAME,
                f"a rule name must be 1 to {MAX_NAME_LENGTH} printable characters",
            )
        return stripped

    @field_validator("conditions", mode="after")
    @classmethod
    def _check_condition_count(cls, value: AllGroup | AnyGroup) -> AllGroup | AnyGroup:
        if _condition_count(value) > MAX_CONDITIONS:
            raise _RuleSemanticError(
                RuleErrorKind.TOO_MANY_CONDITIONS,
                f"a rule may hold at most {MAX_CONDITIONS} conditions",
            )
        return value

    @property
    def all_conditions(self) -> tuple[Condition, ...]:
        """Every condition of the rule, flattened, in document order."""
        conditions: list[Condition] = []
        for member in self.conditions.members:
            if isinstance(member, Condition):
                conditions.append(member)
            else:
                conditions.extend(member.members)
        return tuple(conditions)

    def warmup(self) -> int:
        """Candles needed before the rule can fire at all (spec 006, Design 10)."""
        return self._warmup(stable=False)

    def stable_warmup(self) -> int:
        """Candles needed before the values no longer depend on where the history starts."""
        return self._warmup(stable=True)

    def _warmup(self, *, stable: bool) -> int:
        counts = [condition.warmup(stable=stable) for condition in self.all_conditions]
        return max([1, *counts])


def parse_rule(payload: str | bytes | Mapping[str, object]) -> Rule:
    """Validate an untrusted rule document and return the canonical ``Rule``.

    ``payload`` is JSON text, UTF-8 bytes or an already decoded mapping. Every rejection, from a
    payload above ``MAX_RULE_BYTES`` to a violated indicator constraint, raises
    ``RuleValidationError``; nothing else escapes, whatever a hostile mapping does while it is
    read. ``MAX_RULE_BYTES`` caps the text and bytes forms only; a mapping comes from a caller
    that has already decoded it, and is bounded by the per-field limits instead.
    """
    data: object
    if isinstance(payload, bytes):
        data = _parsed_text(_decoded(payload))
    elif isinstance(payload, str):
        data = _parsed_text(payload)
    elif isinstance(payload, Mapping):
        data = payload
    else:
        raise RuleValidationError.single(
            RuleErrorKind.WRONG_TYPE, "a rule must be JSON text, UTF-8 bytes or a mapping"
        )
    if not isinstance(data, Mapping):
        raise RuleValidationError.single(RuleErrorKind.NOT_AN_OBJECT, "a rule must be an object")
    try:
        return Rule.model_validate(data)
    except ValidationError as error:
        # Never chain the pydantic error: its rendering echoes the raw input, which would leak
        # the payload into tracebacks and logs.
        raise RuleValidationError(_problems(error)) from None
    except TypeError:
        raise RuleValidationError.single(
            RuleErrorKind.WRONG_TYPE, "a rule mapping must only have string keys"
        ) from None
    except Exception as error:
        # A mapping is caller-supplied code: its __getitem__ may raise anything, and #22 catches
        # only RuleValidationError. Catching Exception here, around the validation call alone,
        # keeps the documented contract; BaseException (an interrupt, an exit) still propagates.
        # A bug of ours degrades to this kind instead of crashing, so the message names the
        # exception type, bounded, and the tests pin the expected kind of every other rejection.
        raise RuleValidationError.single(
            RuleErrorKind.INVALID_RULE,
            f"the rule document could not be read ({_echo(type(error).__name__)})",
        ) from None


def dump_rule(rule: Rule) -> dict[str, JsonValue]:
    """The canonical JSON object of ``rule``: every default filled, in the documented key order.

    ``json.dumps(dump_rule(rule), allow_nan=False)`` never raises.
    """
    return {
        "name": rule.name,
        "signal": rule.signal.value,
        "timeframe": rule.timeframe.value,
        "conditions": _dump_group(rule.conditions),
        "cooldown_bars": rule.cooldown_bars,
    }


def _dump_group(group: AllGroup | AnyGroup | NestedAllGroup | NestedAnyGroup) -> JsonValue:
    members: list[JsonValue] = [
        _dump_condition(member) if isinstance(member, Condition) else _dump_group(member)
        for member in group.members
    ]
    return {group.mode.value: members}


def _dump_condition(condition: Condition) -> JsonValue:
    return {
        "left": _dump_operand(condition.left),
        "op": condition.op.value,
        "right": _dump_operand(condition.right),
    }


def _dump_operand(operand: Operand) -> JsonValue:
    if isinstance(operand, PriceOperand):
        return {"price": operand.price.value}
    if isinstance(operand, ValueOperand):
        return {"value": operand.value}
    params: dict[str, JsonValue] = dict(operand.params)
    return {"indicator": operand.indicator, "params": params, "output": operand.output}


def _operand_warmup(operand: Operand, *, stable: bool, extra: int) -> int:
    """Candles one operand needs; a constant needs none, so a crossover does not extend it."""
    if isinstance(operand, ValueOperand):
        return 0
    if isinstance(operand, PriceOperand):
        return 1 + extra
    return operand.warmup(stable=stable) + extra


def _condition_count(group: AllGroup | AnyGroup) -> int:
    return sum(
        1 if isinstance(member, Condition) else len(member.members) for member in group.members
    )


def _is_null(data: Mapping[object, object], key: str) -> bool:
    return key in data and data[key] is None


# --- Text parsing (spec 006, Design 9) ------------------------------------------------------


class _DuplicateKeyError(Exception):
    """A JSON object with the same key twice: the client and the server would read it apart."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _InvalidConstantError(Exception):
    """``NaN``, ``Infinity`` or ``-Infinity``, which are not JSON numbers."""


def _decoded(payload: bytes) -> str:
    if len(payload) > MAX_RULE_BYTES:
        raise _too_large()
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuleValidationError.single(
            RuleErrorKind.INVALID_JSON, f"the rule document is not valid UTF-8 (byte {error.start})"
        ) from None


def _parsed_text(text: str) -> object:
    if len(text) > MAX_RULE_BYTES or len(text.encode("utf-8", "surrogatepass")) > MAX_RULE_BYTES:
        raise _too_large()
    try:
        return json.loads(text, object_pairs_hook=_object_from_pairs, parse_constant=_constant)
    except _DuplicateKeyError as error:
        raise RuleValidationError.single(
            RuleErrorKind.DUPLICATE_KEY, f"duplicate key {_echo(error.key)} in the rule document"
        ) from None
    except _InvalidConstantError:
        raise RuleValidationError.single(
            RuleErrorKind.INVALID_JSON, "NaN, Infinity and -Infinity are not valid JSON numbers"
        ) from None
    except json.JSONDecodeError as error:
        raise RuleValidationError.single(
            RuleErrorKind.INVALID_JSON,
            f"invalid JSON at line {error.lineno} column {error.colno}: {error.msg}",
        ) from None
    except RecursionError:
        raise RuleValidationError.single(
            RuleErrorKind.TOO_DEEP, "the rule document is nested too deeply"
        ) from None


def _object_from_pairs(pairs: Sequence[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    decoded: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in decoded:
            raise _DuplicateKeyError(key)
        decoded[key] = value
    return decoded


def _constant(name: str) -> JsonValue:
    raise _InvalidConstantError(name)


def _too_large() -> RuleValidationError:
    return RuleValidationError.single(
        RuleErrorKind.TOO_LARGE, f"a rule document must not exceed {MAX_RULE_BYTES} bytes"
    )


# --- Mapping pydantic errors to problems (spec 006, Design 8) -------------------------------


def _problems(error: ValidationError) -> tuple[RuleProblem, ...]:
    """One ``RuleProblem`` per pydantic error, in document order, without container noise."""
    mapped = [_mapped_problem(item) for item in error.errors(include_url=False)]
    paths = [problem.path for _, problem in mapped]
    return tuple(
        problem
        for index, (error_type, problem) in enumerate(mapped)
        if not _is_container_noise(error_type, problem.path, paths, index)
    )


def _mapped_problem(item: ErrorDetails) -> tuple[str, RuleProblem]:
    error_type = item["type"]
    loc = item["loc"]
    original = (item.get("ctx") or {}).get("error")
    suffix: str | None = None
    if isinstance(original, IndicatorError):
        kind = _KIND_BY_INDICATOR_KIND[original.kind]
        suffix = original.parameter if kind is RuleErrorKind.INVALID_PARAMETER else None
        message = str(original)
    elif isinstance(original, _RuleSemanticError):
        kind, message = original.kind, str(original)
    elif error_type == "union_tag_not_found":
        kind, message = _union_problem(loc)
    else:
        kind = _KIND_BY_ERROR_TYPE.get(error_type, RuleErrorKind.INVALID_RULE)
        if kind is RuleErrorKind.WRONG_TYPE and not loc:
            kind = RuleErrorKind.NOT_AN_OBJECT
        # pydantic's own messages never echo the input (measured); ours are bounded at the source.
        message = item["msg"].removeprefix(_VALUE_ERROR_PREFIX)
    return error_type, RuleProblem(kind=kind, path=_build_path(loc, suffix=suffix), message=message)


def _union_problem(loc: Sequence[object]) -> tuple[RuleErrorKind, str]:
    tail = loc[-1] if loc else None
    if tail in ("left", "right"):
        return (
            RuleErrorKind.INVALID_OPERAND,
            "an operand must be an object with exactly one of indicator, price or value",
        )
    if isinstance(tail, int):
        return (
            RuleErrorKind.INVALID_GROUP,
            "a group member must be a condition or a group with exactly one of all or any",
        )
    return (
        RuleErrorKind.INVALID_GROUP,
        "a group must be an object with exactly one of all or any",
    )


def _is_container_noise(error_type: str, path: str, paths: Sequence[str], index: int) -> bool:
    if error_type not in _CONTAINER_ERROR_TYPES:
        return False
    return any(
        _is_strict_prefix(path, other) for position, other in enumerate(paths) if position != index
    )


def _is_strict_prefix(prefix: str, path: str) -> bool:
    return path.startswith(prefix) and len(path) > len(prefix) and path[len(prefix)] in ".["
