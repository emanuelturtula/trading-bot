# 006 — JSON rule schema with strict validation

- **Status:** approved
- **Branch:** `feature/rule-schema`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Issue:** #6 (depends on #5)

## Goal

Give the bot a declarative rule model: a JSON document, validated with pydantic and without `eval`
(`CLAUDE.md` rule 8), that names the indicators of the #5 whitelist, the comparison operators and
the `all`/`any` groups of `docs/ARCHITECTURE.md`. Validation is strict and cross-checked against
the indicator registry, the accepted form is canonical (defaults filled) so stored rules do not
drift, and a JSON Schema is exported so the dashboard rule builder (#24) can build forms and
pre-validate what the server will accept.

## Out of scope

- **Evaluation.** Computing indicators, comparing series, crossover semantics and the `Evaluation`
  result are #7. This feature never touches a candle frame.
- **Persistence and identity.** `rule_id`, `enabled`, timestamps, the `rules` table, import/export
  and the ticker assignment (`ticker_rules`) are #12/#13. A rule JSON carries no identifier and no
  ticker list.
- **Cooldown behavior.** #6 validates the number and fixes its meaning; suppressing signals is #14.
- **API and dashboard.** Serving the schema, HTTP status codes and form rendering are #22/#24.
- **New operators or indicators.** `==`, `between`, arithmetic on operands, `not` groups and
  time-of-day filters are deliberately absent; floating-point equality is meaningless on indicator
  values and every addition widens the attack surface of a public schema. A later spec can add them.
- **A `description` field.** The issue lists five fields; a sixth can be added later without
  breaking stored rules (a new optional key never invalidates an existing document).
- **Telegram or dashboard escaping** of the rule name (#17, #24). #6 only bounds it and forbids
  control characters.

## Acceptance criteria

### Canonical shape and round-trip

- [ ] **AC1 (the ARCHITECTURE example parses).** The exact JSON of `docs/ARCHITECTURE.md`
      `## Rule model` is accepted by `parse_rule`, and its canonical dump is exactly:

      ```json
      {"name": "RSI oversold in uptrend", "signal": "BUY", "timeframe": "1d", "conditions": {"all": [{"left": {"indicator": "rsi", "params": {"length": 14}, "output": "value"}, "op": "crosses_below", "right": {"value": 30.0}}, {"left": {"price": "close"}, "op": ">", "right": {"indicator": "sma", "params": {"length": 200}, "output": "value"}}]}, "cooldown_bars": 5}
      ```

- [ ] **AC2 (canonical form).** `dump_rule(rule)` fills every default: `params` holds every declared
      parameter of the indicator in declaration order, `output` is always explicit, `value` is always
      a float, `cooldown_bars` is always present. Keys appear in the order of Design §3.
      `json.dumps(dump_rule(rule), allow_nan=False)` never raises.
- [ ] **AC3 (round-trip and identity).** `parse_rule(dump_rule(rule)) == rule` for every valid
      example, `dump_rule(parse_rule(dump_rule(rule))) == dump_rule(rule)` (the canonical form is a
      fixed point), two payloads that differ only in omitted defaults, key order or `30` against
      `30.0` give equal rules with equal `hash()` in the same process, and rules are frozen
      (attribute assignment raises).

### Operands and cross-validation against the registry

- [ ] **AC4 (operand kinds).** An operand is an object with **exactly one** of `indicator`, `price`
      or `value`. Zero keys, two of them, a non-object or an unknown combination are rejected with
      `RuleErrorKind.INVALID_OPERAND`.
- [ ] **AC5 (indicator operands).** `indicator` must be a name of `REGISTRY` (exact, case-sensitive);
      `params` is optional, validated with `REGISTRY.validate_params` and stored as the resulting
      `IndicatorParams` with defaults filled; `output` is optional where the indicator declares a
      default and required for `macd`, `bbands`, `stoch` and `obv` (`REGISTRY.resolve_output`).
      Explicit `null` for `params` or `output` is rejected (omit the key instead). Every
      `IndicatorError` becomes a rule problem with the registry's message, its kind and the path of
      the offending field.
- [ ] **AC6 (price operands).** `price` is one of `open`, `high`, `low`, `close`, `volume`; the enum
      equals `OHLCV_COLUMNS` of #4 (asserted by a test) and nothing else is accepted.
- [ ] **AC7 (value operands).** `value` is a finite JSON number in `[-1e15, 1e15]`, stored as
      `float`. Integers are accepted (`30` → `30.0`); booleans, strings, `null`, `NaN`, `Infinity`
      and out-of-range magnitudes are rejected.

### Conditions, groups and bounds

- [ ] **AC8 (conditions).** A condition is exactly `{"left": operand, "op": operator, "right":
      operand}`; `op` is one of `<`, `<=`, `>`, `>=`, `crosses_above`, `crosses_below`; any other
      key or operator is rejected.
- [ ] **AC9 (groups and nesting).** `conditions` is a group object with **exactly one** of `all` or
      `any` holding 1 to `MAX_GROUP_ITEMS` (10) members. A root member is a condition or a
      second-level group; a second-level group holds conditions only. A group at the third level is
      rejected with `NESTED_TOO_DEEP` and a path pointing at it. A bare condition as `conditions` is
      rejected (`{"all": [condition]}` is the single-condition form).
- [ ] **AC10 (rule bounds).** A rule holds at most `MAX_CONDITIONS` (20) conditions in total;
      21 are rejected at path `conditions`. `name` is 1–80 characters after stripping surrounding
      whitespace, printable (`str.isprintable()`), and stored stripped; `signal` is `BUY`/`SELL`
      (`Side`); `timeframe` is `1h`/`4h`/`1d` (`Timeframe`, exact codes, no aliases, no stripping);
      `cooldown_bars` is an integer in `[0, 500]`, optional, default `0`.
- [ ] **AC11 (no coercion).** `"14"`, `14.0` and `true` are rejected wherever an integer is expected
      (`params`, `cooldown_bars`), `"30"` and `true` wherever a number is expected, `"1D"`, `" 1d "`
      and `"buy"` for the enums, and a number where a string is expected. Unknown keys are rejected
      **everywhere** (`extra="forbid"`), including `__class__`, `__init__`, `$ref` and `$schema`.

### Semantic checks

- [ ] **AC12 (degenerate conditions).** A condition whose two sides are both `value` operands, or
      whose two operands are equal after normalization, is rejected (`DEGENERATE_CONDITION`): it
      cannot depend on the market and would fire on every candle or never.
- [ ] **AC13 (what stays allowed).** A crossover with one constant side is valid (the ARCHITECTURE
      example), comparing operands of different units (a price with an RSI) is valid, and duplicated
      or contradictory conditions inside a group are valid: the domain does not judge a user's
      strategy, and satisfiability analysis is out of scope.

### Errors

- [ ] **AC14 (typed, bounded errors).** `parse_rule` raises `RuleValidationError` (a `ValueError`)
      with `kind`, `path` and `problems`: every problem carries a `RuleErrorKind`, a JSON-pointer-like
      dotted `path` (`conditions.all[1].left.params.length`) and a one-line English message. Problems
      appear in document order, at most `MAX_PROBLEMS` (20) are kept, and pydantic's internal union
      and validator tags never appear in a path.
- [ ] **AC15 (nothing unbounded is echoed).** No message and no path exceeds the documented bounds
      (each path segment at most 32 characters, the whole path at most 160, each message at most 200)
      and none contains a newline, a control character or raw user input beyond a `repr()`-escaped,
      truncated echo. The raw `input` of a pydantic error is never copied into a message, a path or a
      log.
- [ ] **AC16 (text parsing is guarded).** `parse_rule` accepts `str`, `bytes` (UTF-8) and a
      `Mapping`. Payloads above `MAX_RULE_BYTES` (65 536) are rejected before parsing (`TOO_LARGE`);
      duplicate JSON keys are rejected (`DUPLICATE_KEY`); `NaN`, `Infinity` and `-Infinity` literals
      are rejected (`INVALID_JSON`); a 5 000-level nested payload raises `RuleValidationError`
      (`TOO_DEEP` when the parser overflows, `NOT_AN_OBJECT` when it does not), never
      `RecursionError`, with one bounded, single-line problem; the `RecursionError` → `TOO_DEEP`
      mapping itself is pinned deterministically by a test that makes the parser raise it
      (amendment of 2026-09-16, Design §9.3); invalid UTF-8 and malformed JSON raise
      `INVALID_JSON`; a `Mapping` with a non-`str` key raises `RuleValidationError`, never
      `TypeError`.

### Warmup

- [ ] **AC17 (warmup derived from the indicators).** `rule.warmup()` is the maximum over conditions
      of the maximum over its two operands of: `REGISTRY.warmup(indicator, params)` for an indicator
      operand, `1` for a price operand, `0` for a value operand, **plus 1** for both sides when `op`
      is a crossover; never below `1`. `rule.stable_warmup()` is the same with
      `REGISTRY.stable_warmup`, and `warmup() <= stable_warmup()` always. Verified numbers: the AC1
      rule gives `200`/`200`; `macd` (defaults) `crosses_above` its signal gives `35`/`165`;
      `rsi` (defaults) `crosses_below` `{"value": 30}` gives `16`/`114`; a price-only rule gives
      `1`/`1`.

### JSON Schema

- [ ] **AC18 (exported and valid).** `rule_json_schema()` returns a JSON Schema draft 2020-12
      document that `jsonschema.Draft202012Validator.check_schema` accepts, is deterministic (two
      calls give equal objects and equal `json.dumps` output) and serializes with `allow_nan=False`.
- [ ] **AC19 (the whitelist is in the schema).** The indicator operand is a `oneOf` with one branch
      per `REGISTRY` indicator: `indicator` as a `const`, `params` as an object with
      `additionalProperties: false` and one typed property per declared parameter
      (`integer`/`number` with `minimum`, `maximum`, `default`, `title`, `description` from
      `describe()`), `output` as an `enum` of the declared outputs with a `default` where one exists
      and `required` where none does, plus `x-constraints` for `less_than` constraints. The number of
      branches equals the number of registry names.
- [ ] **AC20 (schema and model agree).** Every payload the model accepts is accepted by the schema.
      The payloads the schema accepts but the model rejects are exactly the documented list of
      Design §11.4 (non-integral numbers for integer fields, `less_than` constraints, the
      `DEGENERATE_CONDITION` checks and the name whitespace/printability rules); a new divergence
      fails the test.

### Typing, purity, dependencies, docs, gate and scope

- [ ] **AC21 (typing).** `uv run mypy` (strict) passes. `typing.Any` is never written in
      `src/trading_bot/domain` (the spec 004 `git grep` stays empty) and there is no bare
      `# type: ignore`. The spec 004 extra-flag run passes over `src/trading_bot/domain` **excluding
      `domain/rules/`**; the exclusion is justified in Design §2 and enforced by a command in the
      evidence table. The `pydantic.mypy` plugin is **not** enabled (Design §2).
- [ ] **AC22 (purity and import weight).** The purity guard allows `pydantic` only in
      `domain/rules/schema.py` and `domain/rules/json_schema.py`, and lists the new modules
      (accepted in review: `schema.py` also needs `json`, the strict reader of §9, and
      `pydantic_core`, which only provides the `ErrorDetails` type of a `ValidationError`; both are
      pure and the guard pins the per-file allowlist exactly);
      `domain/rules/` holds no I/O, no clock and no mutable module-level state. In a fresh
      interpreter, importing `trading_bot.domain.rules.errors` loads neither pandas, numpy nor talib;
      importing `trading_bot.domain.rules.schema` does load them (control assertion) because it
      imports the catalog.
- [ ] **AC23 (dependencies).** `jsonschema` is added to the **dev** group only
      (`uv add --dev jsonschema`). `uv export --no-dev --no-hashes --locked --no-emit-project` is
      byte-identical to the same command on `afe9d48`: the image and the runtime dependency set do
      not change.
- [ ] **AC24 (docs).** `docs/ARCHITECTURE.md` `## Rule model` matches the implementation exactly
      (Design §12): canonical example, operand and group grammar, the nesting definition, the
      bounds, the canonical form, the error contract, the warmup formula and the JSON Schema export.
      Python and JSON blocks pass `ruff format --check` where applicable.
- [ ] **AC25 (gate, coverage and budget).** `uv run python scripts/check.py` is green; every module
      under `src/trading_bot/domain/rules/` has 100% line and branch coverage; the new tests add at
      most **6 s** to `pytest` and no single new test takes more than **2 s** (evidence:
      `--durations`).
- [ ] **AC26 (scope).** Only the files of Design §1 change compared with `afe9d48`, plus this spec.

## Design

### 0. Decisions

| Topic | Decision | Why |
|-------|----------|-----|
| Layout | `domain/rules/` with `errors.py`, `schema.py`, `json_schema.py`; `__init__.py` holds only a docstring, no re-exports | `ARCHITECTURE` announces `rules/ (schema + evaluator)`; #7 adds `evaluator.py` next to them. `errors.py` imports neither pydantic nor the catalog, so #22 can map error kinds without loading TA-Lib. Consistent with #4/#5: import from submodules. |
| Validation library | pydantic v2 models, as `CLAUDE.md` and `ARCHITECTURE` prescribe | pydantic is already a runtime dependency (FastAPI), it is declarative (no `eval`), it produces the JSON Schema for #24 for free, and #22 can reuse the same models. A hand-written validator would duplicate hundreds of lines of parsing and would be a larger attack surface than the library the project already ships. |
| pydantic mypy plugin | **Not enabled** | Measured on a prototype: without the plugin mypy already type-checks `Rule(name=...)` calls through `@dataclass_transform` (`A(x="no")` is an `arg-type` error); **with** the plugin and its default `init_typed = false` that error disappears. The plugin would weaken typing, and its only other effect (removing one `explicit-any` error on the base class) does not solve the flag problem below. This does not invalidate spec 004 §0, which chose a dataclass for `Signal` for other reasons (no serialization concerns in a value type); it only corrects the premise about `__init__`. |
| Extra `Any` flags | `--disallow-any-explicit` is incompatible with pydantic and is scoped out of `domain/rules/` | Measured: a six-line file with `class A(BaseModel): x: int` already raises `Explicit "Any" is not allowed` on the class line, with and without the plugin, because `BaseModel` carries `Any` in its own inherited annotations. The flags stay in force for the rest of `domain/`, and the stronger project rule ("we never write `Any`") is still checked over `rules/` by `git grep`. |
| Registry access | The models use the module constant `REGISTRY` directly; no injection, no validation context | Spec 005 §0 created `REGISTRY` for exactly this. The normalized `params` stored in a rule are only meaningful for the catalog that validated them, so validating with catalog A and computing warmup with catalog B would be incoherent. Tests use the real registry, which is immutable and deterministic. #7 may still take a registry argument for `compute`, which is catalog-agnostic. |
| `params` storage | The `IndicatorParams` returned by `REGISTRY.validate_params`, serialized as a plain object | Read-only, hashable and in declaration order (spec 005 §4), so a rule is hashable and `(indicator, params)` can key #7's per-evaluation cache directly. Spec 005 §12 said "stores `dict(result)`"; storing the richer type keeps the same JSON and the same defaults-filled intent. |
| `output` normalization | Always resolved and stored; the canonical dump always names it | A stored rule keeps meaning if a default output is ever added or changed (spec 005 §12 intent), and #7 never resolves anything at evaluation time. |
| `value` type | Always `float`, JSON integers accepted, range `[-1e15, 1e15]` | One type for #7's comparisons, and `30` and `30.0` become the same rule. The bound is four orders of magnitude above any realistic price, volume or OBV level (the widest fixture volume is `1e12`) and gives the dashboard a `minimum`/`maximum`; a value outside it signals a client bug. |
| Discrimination | Callable `Discriminator` on the key that is present (`operand.*`, `group.*`, `item.*` tags) instead of a plain union | Measured: a plain union reports the errors of **every** member (a nested group produced four spurious "field required" errors from the `Condition` branch). Tags are prefixed with `operand.`/`group.`/`item.` so they can never collide with a JSON key when a path is built, and a callable discriminator emits a plain `oneOf` in the JSON Schema, leaking no tag names. |
| Nesting | Two model families instead of recursion: `AllGroup`/`AnyGroup` at the root, `NestedAllGroup`/`NestedAnyGroup` inside them | The depth limit becomes a property of the type, so the exported schema enforces it client-side, no recursion guard is needed and a third level fails immediately instead of being explored. |
| Root shape | `conditions` must be a group, never a bare condition | One canonical shape for #7, #12 and #24; the builder always produces a group. Normalizing a bare condition into `{"all": [...]}` would silently rewrite what the user sent. |
| Strictness | Global `extra="forbid"` and `frozen=True`, lax-but-exact scalars, `Strict()` on `cooldown_bars`, an explicit number check on `value` | Measured: pydantic's global `strict=True` rejects `"BUY"` for `Side` and a JSON array for a tuple in Python mode, which breaks the `dict` path #22 uses. Per-field strictness gives the same guarantees (`"14"`, `14.0`, `true` rejected) on both the text and the mapping path. |
| Semantic checks | Only the two that cannot depend on data (constant vs constant, identical sides) | They are cheap, local and certainly user errors. Anything beyond that (unit compatibility, contradictions) is the user's strategy. |
| Error mapping | One private mapper turns a pydantic `ValidationError` into `RuleValidationError`, with a catch-all kind | The dashboard and #22 need field-level errors, and pydantic's `msg`/`loc` are stable but internal. The catch-all means a pydantic upgrade that renames an error type degrades to a generic kind instead of crashing. Original exceptions arrive in `ctx["error"]` (measured), so registry kinds survive. |
| Text parsing | `json.loads` with `object_pairs_hook` and `parse_constant`, a byte cap and `RecursionError` mapped to `TOO_DEEP` | Measured: `Rule.model_validate_json` silently keeps the **last** duplicate key, while the stdlib parser can reject duplicates; the stdlib parser raises a clean, catchable `RecursionError` on deep input, at a depth that depends on the interpreter build (Design §9.3). Rejecting duplicates prevents a client and the server from reading different rules out of the same bytes. |
| JSON Schema | pydantic's `model_json_schema()` post-processed: `$schema`, a title, and the `IndicatorOperand` definition replaced by a generated `oneOf` over `REGISTRY.describe()` | Pydantic cannot know the whitelist, and #24 needs labels, ranges and defaults to render forms. Generating from `describe()` means the schema can never drift from the catalog. Measured: about 9.8 KB, 1.3 ms per call, so no caching (which would be mutable module state) is needed. |
| Bounded echo | `rules/errors.py` keeps its own private `_echo`, as `timeframe.py`, `signals.py` and `indicators/errors.py` do | Keeps this feature's diff to new files and avoids touching the still-open #4/#5 branches. Extracting a shared `domain/text.py` is a mechanical follow-up once #3–#6 are merged; it is not part of this feature. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/__init__.py` | developer | Docstring mentions the rules subpackage |
| `src/trading_bot/domain/rules/__init__.py` | developer | Docstring only: pure, pydantic confined to this subpackage, import from submodules |
| `src/trading_bot/domain/rules/errors.py` | developer | `RuleErrorKind`, `RuleProblem`, `RuleValidationError`, bounds (§8) |
| `src/trading_bot/domain/rules/schema.py` | developer | Enums, models, `parse_rule`, `dump_rule` (§3–§10) |
| `src/trading_bot/domain/rules/json_schema.py` | developer | `rule_json_schema` (§11) |
| `tests/fixtures/rules.py` | developer | Shared payload builders: `architecture_example()`, `valid_payloads()`, `invalid_payloads()` (§13) |
| `tests/unit/test_rule_schema.py` | developer | TDD: T1–T5 |
| `tests/unit/test_rule_errors.py` | developer | TDD: T6, T7 |
| `tests/unit/test_rule_warmup.py` | developer | TDD: T8 |
| `tests/unit/test_rule_json_schema.py` | developer | TDD: T9 |
| `tests/unit/test_domain_purity.py` | developer | T14: `pydantic` allowed only in `rules/schema.py` and `rules/json_schema.py`, new modules in the scanned list, `rules.errors` added to the lightweight fresh-interpreter cases (the guard fails as soon as the new files exist, so the developer owns it) |
| `tests/unit/test_rule_schema_conformance.py` | tester | T10 |
| `tests/unit/test_rules_adversarial.py` | tester | T11 |
| `tests/unit/test_rule_schema_properties.py` | tester | T12 |
| `tests/unit/test_rules_typing_guard.py` | tester | T13 |
| `pyproject.toml`, `uv.lock` | developer | `uv add --dev jsonschema` (AC23), in its own commit so the lock diff is reviewable. Add it first: the tester's tests need it |
| `docs/ARCHITECTURE.md` | developer | `## Rule model` rewrite and the `domain/` row of the Layers table (§12); explicitly authorized by this spec |
| `docs/specs/006-rule-schema.md` | tech-lead | This spec |

No new Protocols, no migrations, no `TB_*` variables, no runtime dependency, no `Dockerfile` change.

### 2. Dependencies, typing and the mypy flags

- `uv add --dev jsonschema` (4.26.0 at spec time; it pulls `attrs`, `referencing`,
  `jsonschema-specifications` and `rpds-py`, all dev-only). It is needed to check the exported
  document against the draft 2020-12 metaschema and to prove AC20 with a real validator; without it
  the schema would only ever be checked against itself. The image builds with
  `uv sync --locked --no-dev`, so nothing reaches the runtime image (AC23).
- **Plugin.** Do not add `plugins = ["pydantic.mypy"]`. Evidence in §0; the developer re-runs the
  two-line check if they doubt it.
- **Flags.** `uv run mypy` (strict, the gate) must pass over everything. The spec 004 extra-flag
  command becomes, for this branch:

  ```bash
  uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated \
    src/trading_bot/domain/*.py src/trading_bot/domain/indicators
  ```

  `domain/rules/` is excluded because `class X(BaseModel)` itself trips `explicit-any`
  (measured, plugin or not). Silencing it per class is not an option: strict mode's
  `warn_unused_ignores` would then fail the normal gate.
- `from __future__ import annotations` in every module, as elsewhere in `domain/`.

### 3. The canonical JSON grammar

```text
rule        := {"name": string, "signal": "BUY"|"SELL", "timeframe": "1h"|"4h"|"1d",
                "conditions": group, "cooldown_bars"?: integer}
group       := {"all": [item, ...]} | {"any": [item, ...]}          # exactly one key, 1..10 items
item        := condition | nested                                    # only inside the root group
nested      := {"all": [condition, ...]} | {"any": [condition, ...]} # exactly one key, 1..10 items
condition   := {"left": operand, "op": operator, "right": operand}
operand     := {"indicator": name, "params"?: object, "output"?: string}
             | {"price": "open"|"high"|"low"|"close"|"volume"}
             | {"value": number}
operator    := "<" | "<=" | ">" | ">=" | "crosses_above" | "crosses_below"
```

- **Key order in the canonical dump:** `name`, `signal`, `timeframe`, `conditions`,
  `cooldown_bars`; inside a condition `left`, `op`, `right`; inside an indicator operand
  `indicator`, `params`, `output`; parameters in the indicator's declaration order.
- **Levels.** The root group is level 1, a group inside it is level 2, and a group at level 3 is
  rejected. Two levels express any realistic combination (`all` of `any` groups or the reverse).
- **Bounds.** `MAX_GROUP_ITEMS = 10`, `MAX_CONDITIONS = 20` (the whole rule), `MAX_NAME_LENGTH = 80`,
  `MAX_COOLDOWN_BARS = 500`, `MAX_VALUE_MAGNITUDE = 1e15`, `MAX_RULE_BYTES = 65_536`,
  `MAX_PROBLEMS = 20`. The condition cap bounds #7's cost at 40 operand evaluations per candle
  before deduplication.
- **Absent, not null.** Optional keys (`params`, `output`, `cooldown_bars`) are omitted when unset;
  an explicit `null` is rejected.
- **`cooldown_bars`** is the minimum number of closed candles between two notified signals of the
  same rule and ticker; `0` means no cooldown (idempotency by `(ticker, timeframe, rule_id,
  candle_close_ts)` still prevents resending the same candle). #14 implements it.
- **`timeframe`** is the candle timeframe the rule is evaluated on, a `Timeframe` code with the
  exact #4 semantics. It must equal the timeframe of every ticker the rule is assigned to: #12
  rejects an assignment whose timeframes differ (decision D11). #6 knows nothing about tickers and
  enforces nothing about them; it only fixes the meaning of the field.

### 4. `schema.py`: public names and signatures

```python
MAX_GROUP_ITEMS: Final = 10
MAX_CONDITIONS: Final = 20
MAX_NAME_LENGTH: Final = 80
MAX_COOLDOWN_BARS: Final = 500
MAX_VALUE_MAGNITUDE: Final = 1e15
MAX_RULE_BYTES: Final = 65_536


class Operator(StrEnum):  # "<", "<=", ">", ">=", "crosses_above", "crosses_below"
    @property
    def is_crossover(self) -> bool: ...


class PriceField(StrEnum): ...  # open, high, low, close, volume


class GroupMode(StrEnum): ...  # all, any


class IndicatorOperand(BaseModel):
    indicator: str
    params: IndicatorParams  # validated, defaults filled
    output: str  # always resolved

    def warmup(self, *, stable: bool) -> int: ...


class PriceOperand(BaseModel):
    price: PriceField


class ValueOperand(BaseModel):
    value: float  # finite, |value| <= MAX_VALUE_MAGNITUDE


type Operand = IndicatorOperand | PriceOperand | ValueOperand  # tagged union


class Condition(BaseModel):
    left: Operand
    op: Operator
    right: Operand

    def warmup(self, *, stable: bool) -> int: ...


class NestedAllGroup(BaseModel):  # and NestedAnyGroup
    all: tuple[Condition, ...]

    @property
    def mode(self) -> Literal[GroupMode.ALL]: ...
    @property
    def members(self) -> tuple[Condition, ...]: ...


class AllGroup(BaseModel):  # and AnyGroup; root level
    all: tuple[Condition | NestedAllGroup | NestedAnyGroup, ...]

    @property
    def mode(self) -> Literal[GroupMode.ALL]: ...
    @property
    def members(self) -> tuple[Condition | NestedAllGroup | NestedAnyGroup, ...]: ...


class Rule(BaseModel):
    name: str
    signal: Side
    timeframe: Timeframe
    conditions: AllGroup | AnyGroup
    cooldown_bars: int

    @property
    def all_conditions(self) -> tuple[Condition, ...]: ...  # flattened, document order
    def warmup(self) -> int: ...
    def stable_warmup(self) -> int: ...


def parse_rule(payload: str | bytes | Mapping[str, object]) -> Rule: ...
def dump_rule(rule: Rule) -> dict[str, JsonValue]: ...
```

- Every model sets `model_config = ConfigDict(extra="forbid", frozen=True)` through one private base.
- `mode`/`members` give #7 a uniform walk over both group families without `getattr`.
- `JsonValue` is imported from `trading_bot.domain.indicators.registry` (already defined in #5).
- **`params` serialization (accepted in review).** `IndicatorParams` is a domain type pydantic
  cannot serialize on its own, so the field carries a `PlainSerializer` with
  `when_used="json"`: `model_dump(mode="json")` and `model_dump_json()` produce exactly
  `dump_rule(rule)` (pinned over every valid payload with warnings as errors), while Python mode
  keeps the typed, hashable mapping that #7 uses as a cache key and that makes
  `Rule.model_validate(rule.model_dump())` lossless. Without it any pydantic JSON encoding of a
  rule with an indicator operand raised `PydanticSerializationError` at runtime. The validation
  JSON Schema is unchanged (byte-identical export, §11); only `model_json_schema(mode=
  "serialization")`, which nothing uses, warns that it cannot render the `None` sentinel default.
- `parse_rule` is the **only** supported entry point for untrusted input: it raises
  `RuleValidationError`. `Rule.model_validate` stays available for in-process construction from
  trusted values (#7 tests) and raises pydantic's `ValidationError`; the docstring says so.
- Both `Rule.warmup()` and `Rule.stable_warmup()` are pure functions of the rule and `REGISTRY`.

### 5. Registry cross-validation

Per indicator operand, in this order (the path each problem gets is in brackets):

1. `indicator` field validator (after): `REGISTRY.get(value)` → `UnknownIndicatorError`
   [`....indicator`].
2. `params` field validator (before, reads the already-validated `indicator` from `info.data`):
   rejects a non-mapping or non-`str` keys with a bounded message, then
   `REGISTRY.validate_params(name, value)` → `InvalidParameterError` [`....params`, extended with
   the offending parameter name from `error.parameter` → `....params.length`].
3. `output` field validator (before, `validate_default=True`): rejects a non-string, then
   `REGISTRY.resolve_output(name, value)` → `InvalidOutputError` [`....output`].
4. A model validator (before) rejects an explicit `null` for `params` or `output`.

When `indicator` is unknown, steps 2 and 3 return neutral values instead of raising, so the user
sees one problem (the unknown indicator) and not three. `IndicatorError` is a `ValueError`, so
pydantic wraps it and hands the original object back in `ctx["error"]` (measured), which is how the
mapper recovers `kind` and `parameter` without parsing text.

### 6. Semantic checks (`Condition`)

- A model validator (before) rejects a mapping that carries `all` or `any` where a condition is
  expected, with the `NESTED_TOO_DEEP` message; this is what makes a level-3 group report one clear
  problem.
- A model validator (after) rejects two `ValueOperand` sides and two equal operands
  (`DEGENERATE_CONDITION`). Equality is pydantic model equality, so
  `{"indicator": "rsi"}` and `{"indicator": "rsi", "params": {"length": 14}, "output": "value"}`
  count as equal (both normalize to the same operand), which is the intended behavior.

### 7. Name, enums and numbers

| Field | Rule | Rejected examples |
|-------|------|-------------------|
| `name` | `str`, surrounding whitespace stripped, 1-80 characters, `str.isprintable()` | `""`, `"   "`, 81 characters, a name holding a newline or a tab, a name holding U+200B (zero width space) or U+202E (right-to-left override), `5`, `null` |
| `signal` | `Side` by exact value | `"buy"`, `"Buy"`, `"LONG"`, `0` |
| `timeframe` | `Timeframe` by exact value (no aliases, no stripping, as #4 decided) | `"1D"`, `" 1d "`, `"60m"`, `"daily"` |
| `cooldown_bars` | strict `int`, `0 <= n <= 500`, default `0` | `"5"`, `5.0`, `true`, `-1`, `501`, `10**100` |
| `value` | JSON number (bool rejected explicitly, since `bool` is an `int` in Python), finite, `abs(value) <= 1e15`, stored as `float` | `true`, `"30"`, `null`, `NaN`, `1e308` |

Non-ASCII names are accepted: a rule name is user data shown in the dashboard and in Telegram, not a
repository artifact, so `CLAUDE.md` rule 9 does not apply to it. Printability plus the length bound
is what protects later log lines and messages from injection.

### 8. `errors.py`

```python
class RuleErrorKind(StrEnum):
    INVALID_JSON = "invalid_json"
    TOO_LARGE = "too_large"
    TOO_DEEP = "too_deep"
    DUPLICATE_KEY = "duplicate_key"
    NOT_AN_OBJECT = "not_an_object"
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    UNKNOWN_VALUE = "unknown_value"  # an enum: operator, price, signal, timeframe
    INVALID_OPERAND = "invalid_operand"  # not exactly one of indicator/price/value
    INVALID_GROUP = "invalid_group"  # not exactly one of all/any
    NESTED_TOO_DEEP = "nested_too_deep"
    TOO_MANY_CONDITIONS = "too_many_conditions"
    DEGENERATE_CONDITION = "degenerate_condition"
    INVALID_NAME = "invalid_name"
    UNKNOWN_INDICATOR = "unknown_indicator"
    INVALID_PARAMETER = "invalid_parameter"
    INVALID_OUTPUT = "invalid_output"
    INVALID_RULE = "invalid_rule"  # catch-all


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleProblem:
    kind: RuleErrorKind
    path: str  # "" for the whole document, else "conditions.all[1].left.params.length"
    message: str


class RuleValidationError(ValueError):
    kind: RuleErrorKind  # of the first problem
    path: str  # of the first problem
    problems: tuple[RuleProblem, ...]
```

- `str(error)` is one line: the first problem as `path: message`, plus `(and N more problems)` when
  there are others. It never contains a newline and never exceeds 400 characters (the AC15 path and
  message bounds plus the fixed text).
- **Path building.** From pydantic's `loc`: integers become `[i]`, strings become `.key`, and any
  segment that is a union tag (it contains a `.`, by construction of the tag names) is dropped.
  Each segment is truncated to 32 characters and rendered with the bounded `repr()` echo when it is
  not a plain identifier (an unknown key sent by the client can be hostile); the whole path is
  truncated to 160 characters at a segment boundary. Measured example:
  `('conditions', 'group.all', 'all', 0, 'item.condition', 'left', 'operand.indicator', 'params')`
  → `conditions.all[0].left.params`, extended with `.length` from the registry error.
- **Mapping table** (pydantic error `type` → kind):

  | pydantic `type` | kind |
  |-----------------|------|
  | `missing` | `missing_field` |
  | `extra_forbidden` | `unknown_field` |
  | `string_type`, `int_type`, `float_type`, `bool_type`, `model_type`, `dict_type`, `list_type`, `tuple_type`, `is_instance_of`, `model_attributes_type` | `wrong_type` (`not_an_object` when the path is empty) |
  | `greater_than_equal`, `less_than_equal`, `greater_than`, `less_than`, `string_too_short`, `string_too_long`, `too_short`, `too_long`, `finite_number` | `out_of_range` |
  | `enum`, `literal_error` | `unknown_value` |
  | `union_tag_not_found` | `invalid_operand` / `invalid_group` depending on the path tail (`left`/`right` → operand, `conditions` or a group item → group) |
  | `value_error` with an `IndicatorError` in `ctx["error"]` | its `IndicatorErrorKind` mapped: `unknown_indicator` → `unknown_indicator`; `unknown_parameter`, `wrong_type`, `out_of_range`, `constraint` → `invalid_parameter`; `unknown_output`, `missing_output` → `invalid_output` |
  | `value_error` with a private `_RuleSemanticError` in `ctx["error"]` | the kind it carries (`nested_too_deep`, `degenerate_condition`, `too_many_conditions`, `invalid_name`, `wrong_type`) |
  | anything else | `invalid_rule` |

- **Message.** Structural errors reuse pydantic's `msg` (measured: none of them echoes the input);
  `value_error` messages drop the `Value error, ` prefix and use ours, which are bounded at the
  source. `errors(include_url=False)` keeps pydantic's documentation URL out of user-facing text.
  The `input` field of a pydantic error is never read.
- **Noise filter.** A problem whose kind is `out_of_range` from `too_short`/`too_long` and whose
  path is a strict prefix of another problem's path is dropped: pydantic reports the container as
  "too short" after discarding the member that actually failed (measured).
- Problems keep pydantic's order (document order) and are capped at `MAX_PROBLEMS`.

### 9. `parse_rule` on text

1. `bytes` are decoded as strict UTF-8; a `UnicodeDecodeError` becomes `INVALID_JSON`.
2. Text longer than `MAX_RULE_BYTES` (measured in UTF-8 bytes) is rejected with `TOO_LARGE` before
   parsing. A maximal legal rule is about 4 KB.
3. `json.loads(text, object_pairs_hook=..., parse_constant=...)`:
   - the pairs hook raises on a duplicate key → `DUPLICATE_KEY` (the key is echoed, bounded);
   - `parse_constant` raises for `NaN`, `Infinity` and `-Infinity` → `INVALID_JSON`;
   - `json.JSONDecodeError` → `INVALID_JSON` with the position, never the offending text;
   - `RecursionError` → `TOO_DEEP` (the C scanner raises it cleanly and catchably).

   **Amendment of 2026-09-16 (CI failure on the Linux runner).** The *depth* at which the parser
   overflows is a property of the interpreter build, not of this code: CPython 3.12 guards the C
   scanner with the interpreter's C-recursion limit, which `sys.setrecursionlimit` does not move,
   so the same 5 000-level payload raised `RecursionError` on the developer machine (Windows) and
   parsed successfully on the runner, where the resulting list was then rejected as
   `NOT_AN_OBJECT`. Both are safe, bounded `RuleValidationError` rejections, and no raw
   `RecursionError` escapes either way; only the claim about *which* path is taken was
   unportable. Therefore: the deep-payload test asserts the invariant (a `RuleValidationError`
   with one bounded, single-line problem whose kind is `TOO_DEEP` or `NOT_AN_OBJECT`), and a
   separate test makes `json.loads` raise `RecursionError` to pin this mapping deterministically
   on every platform. The nesting guarantee of a rule document does not come from the parser: it
   comes from the non-recursive model types (§0, §3), which reject a group at level 3 whatever the
   parser accepts.
4. A parsed value that is not an object → `NOT_AN_OBJECT`.
5. `Rule.model_validate(data)`; `ValidationError` → `RuleValidationError` through §8. A `TypeError`
   escaping validation (only reachable from a `Mapping` with non-`str` keys) is caught and reported
   as `WRONG_TYPE`, so `parse_rule` raises nothing but `RuleValidationError` for any input.
   **Accepted in review:** a third handler, `except Exception` around that single call, maps
   anything else a caller-supplied `Mapping` raises while it is read to `INVALID_RULE` with the
   exception class name as a bounded echo. `BaseException` (an interrupt, an exit) still
   propagates, the handlers of the `ValidationError` branch are unaffected (an exception raised in
   an `except` block is not caught by its siblings), and the price is that a non-`ValueError` bug
   of our own inside a validator degrades to `INVALID_RULE` instead of crashing. That is the
   correct trade for a public entry point whose only documented failure is `RuleValidationError`;
   the tests pin the expected kind of every other rejection, so such a regression fails loudly.
   `INVALID_RULE` therefore also means "possible server bug": #22 logs it instead of treating it
   as an ordinary field error.
6. The byte cap guards the text and bytes forms only; a `Mapping` is bounded by the per-field
   limits. Worst case measured in review: about 0.4 s for a 64 KiB document of 20 000 empty group
   members. The work per payload is bounded, but #22 keeps its own request body limit and its
   authentication in front of `parse_rule`.

### 10. Warmup

```text
operand_warmup(o, stable, extra) = 0                                        if o is a value
                                 = 1 + extra                                if o is a price
                                 = REGISTRY.warmup(o.indicator, o.params) + extra
                                 (stable_warmup instead of warmup when stable)
extra                            = 1 if op is crosses_above/crosses_below else 0
condition_warmup(c)              = max(operand_warmup(c.left), operand_warmup(c.right))
rule.warmup()                    = max(1, max over all conditions)
```

A crossover compares the last two candles, so each series side needs one extra candle; a constant
side needs none, and a condition with two constant sides cannot exist (§6). The rule-level floor of
`1` covers a rule made only of price and value operands. The numbers of AC17 come from
`REGISTRY.warmup`/`stable_warmup` and were verified against the implementation of #5.

### 11. `json_schema.py`

#### 11.1 Structure

```python
def rule_json_schema() -> dict[str, JsonValue]: ...
```

Returns, in this key order: `$schema` (`https://json-schema.org/draft/2020-12/schema`), `title`
(`Trading bot rule`), `description`, then pydantic's `model_json_schema()` output (`$defs`,
`type`, `properties`, `required`, `additionalProperties: false`) with `title` overridden after the
merge. `$defs` holds `AllGroup`, `AnyGroup`, `NestedAllGroup`, `NestedAnyGroup`, `Condition`,
`IndicatorOperand`, `PriceOperand`, `ValueOperand`, `Operator`, `PriceField`, `Side` and
`Timeframe`. No `$id` is emitted (nothing in the repository may invent a host name). Accepted in
review: pydantic also emits one `$defs` entry per PEP 695 type alias (`Operand`, `RootItem`,
`RootGroup`), each a plain `oneOf` of `$ref`s; no discriminator tag reaches the document.

#### 11.2 The indicator branches

`$defs.IndicatorOperand` is replaced by `{"oneOf": [branch per indicator in catalog order]}`, each
built from one `REGISTRY.describe()` entry. Literal `rsi` branch (AC19):

```json
{
  "title": "RSI",
  "description": "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined while the close has not changed since the first candle.",
  "type": "object",
  "additionalProperties": false,
  "required": ["indicator"],
  "properties": {
    "indicator": {"const": "rsi"},
    "params": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "length": {"title": "Length", "description": "Smoothing period in candles.", "type": "integer", "minimum": 2, "maximum": 100, "default": 14}
      }
    },
    "output": {"enum": ["value"], "default": "value"}
  }
}
```

- `describe()` `"type"` maps `int` → `"integer"` and `float` → `"number"`; `min`/`max`/`default`
  become `minimum`/`maximum`/`default`; `label`/`description` become `title`/`description`.
- `macd`, `bbands`, `stoch` and `obv` have `"required": ["indicator", "output"]` and no `default`
  on `output`.
- An indicator with `constraints` carries them verbatim under `"x-constraints"` (advisory metadata
  for #24; unknown keywords are ignored by validators and the constraint is enforced server-side).

#### 11.3 Determinism

Two calls return equal objects with equal `json.dumps` output; nothing is cached (no mutable
module state) and generation costs about 1.3 ms.

#### 11.4 Where the schema is laxer than the server (documented gap list, AC20)

1. Non-integral numbers for integer fields: JSON Schema's `integer` accepts `14.0` and `5.0`, the
   model rejects them.
2. `less_than` constraints between parameters (`macd` `fast < slow`), which draft 2020-12 cannot
   express.
3. The `DEGENERATE_CONDITION` checks (constant vs constant, identical operands).
4. Rule-name whitespace stripping and printability, and the `MAX_CONDITIONS` total (the schema only
   bounds items per group).

The reverse never happens: every payload the model accepts validates against the schema.

### 12. `docs/ARCHITECTURE.md`

Rewrite `## Rule model` (keeping the current JSON example, which AC1 pins) so that it states:

1. the grammar of §3 (operands, operators, groups, the level definition and the bounds table);
2. that the accepted form is canonical (defaults filled, `output` explicit, `value` a float) and
   why it matters for stored rules;
3. the entry points `parse_rule`, `dump_rule`, `rule_json_schema` and the `Rule.warmup()` /
   `Rule.stable_warmup()` formula, with the AC17 example numbers;
4. the error contract: `RuleValidationError` with `kind`, `path` and `problems`, bounded messages;
5. the JSON Schema export: draft 2020-12, the indicator whitelist inlined from `describe()`, and
   the gap list of §11.4 (the server is authoritative);
6. that `timeframe` is the timeframe the rule is evaluated on and must equal the timeframe of every
   ticker the rule is assigned to, an assignment rule enforced in #12 (decision D11);
7. a link to this spec, as `## Indicators` links to spec 005.

Also update the `domain/` row of the Layers table (`Rule` is no longer "in #6") and keep the
`cooldown_bars` and ticker-assignment bullets, extending the latter with the timeframe rule of
point 6. `docs/ROADMAP.md` is not touched (F1 still covers the
evaluator, #7).

### 13. Shared fixtures (`tests/fixtures/rules.py`)

- `architecture_example() -> dict[str, JsonValue]`: the AC1 payload, built fresh on every call.
- `valid_payloads() -> tuple[tuple[str, dict[str, JsonValue]], ...]`: labelled payloads covering
  every operator, every operand kind, every indicator (with and without `params`/`output`), `all`
  and `any` at both levels, minimum and maximum sizes, `cooldown_bars` absent/`0`/`500`, a name with
  accents and one of exactly 80 characters.
- `invalid_payloads() -> tuple[tuple[str, dict[str, JsonValue], RuleErrorKind, str], ...]`:
  labelled payloads with the expected kind and path, covering at least the table of T2–T4.
- Type-checked by mypy (`tests/fixtures` is in `files`), no network, no randomness.

### 14. Forward compatibility

| Issue | How it uses #6 |
|-------|----------------|
| #7 | Walks `rule.conditions` through `mode`/`members`, or `rule.all_conditions`; computes each distinct `(indicator, params)` once using `IndicatorParams` as the cache key; reads `operand.output` (always a `str`); uses `Operator.is_crossover` for the two-candle comparison and `rule.warmup()` to decide whether a rule can fire at all. |
| #8, #14 | Fetch at least `max(rule.stable_warmup())` candles over the rules of a ticker, capped by provider limits. |
| #12 | Stores `json.dumps(dump_rule(rule))` in `rules.definition_json`, re-parses with `parse_rule` on load, and owns `rule_id`, `enabled` and the ticker assignment. **Obligation (D11):** an assignment is accepted only when `rule.timeframe` equals the ticker's timeframe, and changing either one revalidates the existing assignments. #12 also decides whether rule names must be unique (D8). The import/export CLI reports `RuleProblem.path` and `message` per problem. |
| #13 | Rule identity comes from the database, not from the JSON; the canonical form keeps stored documents stable so a rule does not silently change meaning when a catalog default changes. |
| #22 | Reads the body as a mapping and calls `parse_rule`, mapping `RuleErrorKind` to HTTP 400/422 and returning `problems` as field errors; it must not bind `Rule` as a FastAPI **request** model, whose 422 body echoes the raw input. Encoding a rule in a response is safe: `model_dump(mode="json")` and `model_dump_json()` equal `dump_rule` (§4). It keeps a request body limit (the byte cap guards text only, §9.6), and logs `invalid_rule` problems, which may signal a server bug (§9.5). Serves `rule_json_schema()` and `REGISTRY.describe()`. |
| #24 | Builds forms from the `oneOf` branches (labels, ranges, defaults, output selectors) and pre-validates client-side, knowing the §11.4 gaps mean the server has the last word. |
| #17 | Shows `rule.name` (bounded, printable) and the condition list; escaping is its own concern. |

## Test plan

All tests are unit tests, without network, with synthetic payloads only. Mandatory template cases:

- **Anti look-ahead:** not applicable. Nothing in this feature reads a candle or produces a
  per-candle value; `warmup` depends only on the rule. #7 carries the look-ahead obligation for the
  evaluator (`assert_no_lookahead_point_in_time`), and this spec records the obligation.
- **Idempotency:** the analogue is covered by T1 (AC3): parse → dump → parse is a fixed point and
  equal payloads give equal rules, which is what keeps stored rule documents and, through them,
  signal identity stable for #12/#13.
- **Authorization:** not applicable (no Telegram, API or dashboard code).
- **Secret redaction:** not applicable (no configuration, logging or secrets). The analogue,
  bounded messages and paths that never echo raw user input, is covered by T6 and T11.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 canonical shape | unit | The AC1 payload and dump literal; every valid payload of the fixtures; defaults filled; `30` → `30.0`; round-trip and fixed point; equality and `hash` across equivalent payloads; frozen models; `bytes`, `str` and `Mapping` inputs agree | AC1–AC3 | developer |
| T2 structural rejections | unit | Missing and unknown keys (`__class__`, `$ref`); wrong types and the no-coercion table of §7; `null` for `params`/`output`; empty group; both group keys; neither; 11 items; 21 conditions; three levels; a bare condition as `conditions`; a list or a string as the document | AC4, AC8–AC11 | developer |
| T3 registry cross-validation | unit | Unknown indicator; unknown, mistyped, out-of-range parameters; `macd` `fast < slow`; missing output for each multi-output indicator; unknown output; a valid operand for every registry indicator; expected kind **and** path for each | AC5, AC6 | developer |
| T4 semantic checks | unit | Constant vs constant; identical operands (including one written with defaults and one without); crossover with a constant side accepted; price against indicator accepted; duplicated conditions accepted | AC12, AC13 | developer |
| T5 scalars | unit | `name` stripping, 1/80/81 boundaries, control characters, accents; `signal` and `timeframe` exact codes; `cooldown_bars` default, bounds and rejections; `value` bounds, `-0.0`, `5e-324`, `1e15`, `1e15 + 1` | AC7, AC10, AC11 | developer |
| T6 errors | unit | `RuleValidationError` is a `ValueError`; `kind`/`path`/`problems`; document order; the `too_short` companion filtered; every row of the §8 mapping table; an unknown pydantic type maps to `invalid_rule`; path and message bounds and single-line rendering; `str(error)` format | AC14, AC15 | developer |
| T7 text parsing | unit | Valid `str` and UTF-8 `bytes`; invalid UTF-8; malformed JSON; trailing content; `NaN`/`Infinity`/`-Infinity`; duplicate keys at every level; a 5 000-level payload (a `RuleValidationError` with one bounded problem whose kind is `TOO_DEEP` or `NOT_AN_OBJECT`, never a `RecursionError`) plus a test that makes the parser raise `RecursionError` and pins the `TOO_DEEP` mapping on every platform; a payload above 64 KiB; a `Mapping` with a non-`str` key (no `TypeError`) | AC16 | developer |
| T8 warmup | unit | The §10 formula per operand kind and per operator; the four AC17 numbers; `warmup <= stable_warmup`; the floor of 1; warmup unchanged by the group shape | AC17 | developer |
| T9 JSON Schema | unit | `check_schema` (draft 2020-12); top-level keys and order; one branch per registry name; the literal `rsi` branch; `macd` requires `output` and carries `x-constraints`; parameter type/range/default mapping for an `int` and for `bbands` `std`; determinism; `allow_nan=False` serialization | AC18, AC19 | developer |
| T10 schema conformance | unit | Every valid fixture payload validates against the exported schema; every invalid fixture payload is rejected by the model and also by the schema, except the §11.4 gap list, which is asserted as an exact set (a new divergence fails) | AC20 | tester |
| T11 adversarial | unit | Code strings in `name` and `op` (`__import__('os')`, `eval`, `{{7*7}}`); `indicator: "eval"`; keys `__class__`, `__init__`, `__proto__`, `$ref`; a 1 MB name and a 1 MB key; 10 000-character parameter names; `10**100` for `cooldown_bars` and parameters; nested containers as parameter values; ANSI escapes, zero-width and RTL characters in `name` and in keys; assert every message and path stays inside the AC15 bounds, single-line, and never contains the raw input; nothing raises other than `RuleValidationError` | AC15, AC16 | tester |
| T12 properties | unit + `@given` | Hypothesis builds valid rules from the catalog: parse → dump → parse is a fixed point and equal; `1 <= warmup <= stable_warmup`; warmup equals the independent maximum computed from the drawn operands; every generated payload validates against the exported schema | AC3, AC17, AC20 | tester |
| T13 typing and imports | unit | `pydantic` imported only in `rules/schema.py` and `rules/json_schema.py` (AST scan over `src/`); `rules/errors.py` imports neither pydantic nor the catalog; no `Any` in `src/trading_bot/domain` (`git grep`); the scoped extra-flag mypy command passes; the `pydantic.mypy` plugin is absent from `pyproject.toml` | AC21, AC22 | tester |
| T14 purity guard | unit | Updated allowlist and scanned-file list; `rules.errors` in the lightweight fresh-interpreter set; `rules.schema` as the control that does load pandas/numpy | AC22 | developer |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC25 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot.domain --cov-branch --cov-report=term-missing`: 100% for every module under `domain/rules/`, and no regression elsewhere | AC25 |
| V3 budget | `uv run pytest --durations=40 -q`: the new tests add at most 6 s, none above 2 s | AC25 |
| V4 typing | `uv run mypy`; the §2 scoped extra-flag command; the spec 004 `git grep` for `Any` over `src/trading_bot/domain` is empty; `git grep -n "type: ignore" src/trading_bot/domain/rules` is empty | AC21 |
| V5 runtime dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch equals the same command in a temporary worktree of `afe9d48` (scratch directory, removed afterwards) | AC23 |
| V6 dev dependency | `uv.lock` adds only `jsonschema` and its transitive dev dependencies; `pyproject.toml` lists it under `[dependency-groups] dev` | AC23 |
| V7 docs | `## Rule model` read against §12; JSON blocks parse (`python -m json.tool`); `uv run ruff format --check docs/ARCHITECTURE.md` | AC24 |
| V8 scope | `git diff afe9d48 --name-only` plus `git status --porcelain` list only the §1 files. After #3–#5 merge and the branch is rebased, compare with `origin/main` | AC26 |
| V9 secrets and language | `python scripts/secret_scan.py --staged`; English-only review of the diff, including test payload strings and rule names | — |
| V10 Docker | The image content changes (new `src/` modules) but nothing new is imported at startup and no runtime dependency changes; the local build stays **BLOCKED** (no daemon). The authoritative checks are the PR's `Docker build (arm64)` job and the beta deploy with `/health`, verified by the lead | AC23 |

**TDD order suggested to the developer:** `uv add --dev jsonschema` (isolated commit) → T14 purity guard
update → `errors.py` with T6's mapping table driven by hand-built pydantic errors → the operand
models with T3 → `Condition` and the groups with T2/T4 → `Rule` with T5 → `parse_rule` text guards
with T7 → `dump_rule` and the round-trip with T1 → warmup with T8 → `json_schema.py` with T9 → the
shared fixtures (extract them as soon as T1 and T2 exist) → docs.

## Risks and security

- **A public, machine-readable schema is an attack surface.** Mitigations: no `eval`/`exec`
  anywhere; a closed whitelist of indicators, outputs, operators and price columns; `extra="forbid"`
  at every level; bounded payload size, nesting, item counts and total conditions; duplicate-key and
  non-finite-literal rejection; every message and path bounded and escaped (T11).
- **Denial of service through parsing.** The byte cap and the depth limit built into the types
  bound the work per payload; measured parse cost is about 7 µs for a typical rule. The load-bearing
  limit is the type design: a document nested beyond level 2 is rejected by the models whatever the
  JSON parser accepts. The `RecursionError` mapping is a safety net whose trigger point depends on
  the interpreter build (Design §9.3), not a bound this feature relies on.
- **Error messages leaking or injecting.** Paths are built from user-controlled keys, so segments
  are truncated and `repr()`-escaped; pydantic's `input` field is never read; messages are
  single-line. This matters because #12/#14/#17 will log and display them.
- **pydantic upgrades.** Error `type` strings and union `loc` shapes are internal. Mitigations: the
  mapper's catch-all kind, tests that assert our kinds and paths (not pydantic strings), and the
  gate that runs on every Dependabot bump. A behavior change would fail T6/T2 loudly rather than
  silently accept a bad rule.
- **Schema drift.** The JSON Schema is generated from the same models and from `describe()`, so it
  cannot diverge from the catalog; T10 pins the accept/reject agreement and the gap list.
- **Stored-rule compatibility.** Renaming a JSON key, tightening a bound or changing an indicator
  parameter name breaks documents already stored by #12. This spec fixes the vocabulary before any
  rule is persisted, and `dump_rule` writes every default explicitly so a later change of a catalog
  default cannot silently change an existing rule.
- **Supply chain.** `jsonschema` is dev-only and never enters the image; `uv.lock` pins hashes.
- **Sensitive data.** None: no tokens, hosts, IPs, users, `TB_*` variables, migrations or
  `secrets.env` changes. Test payloads use invented rule names in English.
- **Unbreakable rules.** Signal-only (a rule expresses a notification condition, never an order),
  pure `domain/` (no I/O, no clock, no mutable module state; the purity guard is extended), closed
  candles (nothing reads candles here), idempotency (the canonical form keeps stored rules stable),
  UTC (no timestamps in a rule), single worker (untouched), no `eval` (declarative whitelist; the
  exported schema contains no expressions), English only: all preserved.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches (#3 → #4 → #5 → #6 → #7).
- **D2 (2026-09-14):** synthetic fixtures only in M1.
- **D3 (2026-09-14):** `obv` has a signal line and no default output, so a rule must name
  `value` or `signal`.
- **D4 (2026-09-14):** undefined indicator values are NaN, so rules never fire on them (#7).
- **D5 (2026-09-14):** indicator parameter names, defaults and ranges are approved as in spec 005;
  renaming one later requires migrating stored rules.
- **D6 (2026-09-15): nesting depth.** "Nested up to 2 levels" means the root group is level 1 and
  one nested group layer is level 2; a group inside a nested group is rejected. It expresses `all`
  of `any` groups and the reverse, which covers realistic rules, and it keeps the exported schema
  non-recursive (§3, §0, AC9).
- **D7 (2026-09-15): `cooldown_bars`.** Optional, default `0` (no cooldown), integer in `[0, 500]`
  (§3, §7, AC10).
- **D8 (2026-09-15): rule name.** 1–80 characters after stripping surrounding whitespace, any
  printable Unicode (accented names are fine), no control characters. Uniqueness is not enforced in
  #6; whether two rules may share a name is #12's decision (§7, AC10).
- **D9 (2026-09-15): root shape.** `conditions` is always a group; the dashboard wraps a single
  condition in `{"all": [...]}`. One shape for the evaluator, storage and the schema (§3, AC9).
- **D10 (2026-09-15): size limits.** 10 members per group, 20 conditions per rule, 64 KiB per
  document (§3, AC9, AC10, AC16).
- **D11 (2026-09-15): rule timeframe against ticker timeframe.** A rule's `timeframe` is the
  timeframe its candles are evaluated on, and it must equal the timeframe of every ticker the rule
  is assigned to: **#12 rejects an assignment whose timeframes differ** (`Rule.timeframe` itself
  keeps the #4 semantics, an exact `Timeframe` code). The alternative, evaluating a rule on its own
  timeframe for a ticker tracked on another one, would mean a second fetch and a second scheduler
  job per ticker. #6 only records the rule, documents the semantics and adds the obligation to §14;
  it enforces nothing about tickers, which it does not know.
- Earlier project decisions that apply: rules are defined from the dashboard as JSON, validated
  against a whitelist of indicators and operators, without `eval`; a rule is assigned to one or more
  tickers; each ticker has a timeframe, `1d` by default.

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria, and T1, T2, T3, T6, T7, T8 and T9 fail without the
      implementation
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)

Reviewed on 2026-09-16 (second round). First round requested five changes: the pydantic JSON
serialization of `params` (§4), raw bidi and invisible characters in three test files, the
`domain/__init__.py` docstring, an exact `$defs` assertion and two test docstrings that described
the review process. All are fixed. Approved.

Amended on 2026-09-16 after the branch failed on the Linux CI runner: AC16, Design §9.3, the
"Text parsing" row of §0, the T7 row and the parsing risk now say that the JSON parser's overflow
depth is interpreter-specific, so the deep-payload test asserts the invariant (a bounded
`RuleValidationError` of kind `TOO_DEEP` or `NOT_AN_OBJECT`, never a raw `RecursionError`) while a
second test pins the `RecursionError` → `TOO_DEEP` mapping deterministically. The source is
unchanged; the amendment corrects a claim of this spec, not the implementation.
