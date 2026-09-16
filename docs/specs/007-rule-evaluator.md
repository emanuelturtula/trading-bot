# 007 — Pure rule evaluator

- **Status:** approved
- **Branch:** `feature/rule-evaluator`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Issue:** #7 (depends on #6; closes milestone M1)

## Goal

Decide, with a pure function, whether a rule of #6 fires on the last closed candle of a validated
candle frame, and return everything the rest of the bot needs to act on that decision: the trigger,
the candle identity (`candle_close_ts`), the close price, the indicator values the rule actually
used and the outcome of every condition. The same function, evaluated over the last `N` candles,
serves the dashboard "test rule" (#24), backtesting (F8) and the look-ahead harness of #3.

## Out of scope

- **Fetching, closedness and scheduling.** The evaluator receives a frame and treats its **last row
  as the last closed candle**. Discarding the in-progress candle is #8 (`drop_open_candle`), real
  session closes are #9 and firing times are #15. Nothing here reads a clock (`CLAUDE.md` rules 3
  and 4).
- **Cooldown, deduplication and notification.** `cooldown_bars` is validated by #6 and applied by
  #14 against persisted state; idempotency by `(ticker, timeframe, rule_id, candle_close_ts)` is
  #13/#14. The evaluator reports whether the conditions hold and nothing else (Design §10).
- **`Signal` construction.** The evaluator knows no ticker and no `rule_id`, so it cannot build a
  `Signal`; #14 does, from `rule.signal` and an `Evaluation` (Design §12).
- **New operators, indicators or operand arithmetic.** The grammar is closed by #6 and the catalog
  by #5. Adding `==`, `between`, `not` or arithmetic operands is a later spec.
- **Message rendering.** How an `Evaluation` becomes Telegram text or a chart is #17; how it becomes
  a dashboard table is #24. This spec only fixes the data and the key scheme they read.
- **Multi-ticker or multi-rule orchestration**, and any caching across calls: one call evaluates one
  rule on one frame.

## Acceptance criteria

### Public API and result

- [ ] **AC1 (entry points).** `trading_bot.domain.rules.evaluator` exports exactly `Evaluation`,
      `evaluate` and `evaluate_each` (`__all__`), with the signatures of Design §2:
      `evaluate(rule, candles, *, registry=REGISTRY) -> Evaluation` and
      `evaluate_each(rule, candles, *, last=None, registry=REGISTRY) -> tuple[Evaluation, ...]`.
      `evaluate_each` returns evaluations in frame order, oldest first, one per candle covered.
- [ ] **AC2 (`Evaluation`).** A frozen, slotted, keyword-only dataclass with exactly `triggered:
      bool`, `candle_close_ts: datetime`, `close_price: float`, `indicator_values: IndicatorValues`
      and `condition_results: tuple[bool, ...]`. Instances are immutable (assignment raises),
      hashable, equal by value, `pickle`-able and compared correctly by `tests.lookahead.
      values_equal`, including NaN-free mappings and equal `IndicatorValues` in different insertion
      orders.
- [ ] **AC3 (candle identity).** For the evaluation of the candle at row position `i`:
      `candle_close_ts == rule.timeframe.nominal_close(candles.index[i])`, a stdlib `datetime` in
      `datetime.UTC`, and `close_price == float(candles["close"].iloc[i])`. Literal check for the
      three timeframes (for example `1d` open `2024-01-02T05:00:00Z` → close
      `2024-01-03T05:00:00Z`). `evaluate(rule, candles)` describes `candles.index[-1]`.
      `candle_close_ts` is an identifier, not the market close (spec 004 §5).

### Semantics

- [ ] **AC4 (comparison operators).** `<`, `<=`, `>` and `>=` compare the two operand values at the
      evaluated candle only. The case table of Design §5.1 is verified for every operator over
      `less`, `equal`, `greater`, `NaN left`, `NaN right` and `both NaN`, and the parametrization
      covers every member of `Operator` (asserted as a set).
- [ ] **AC5 (crossovers).** `crosses_above` is `previous_left <= previous_right and left > right`;
      `crosses_below` is `previous_left >= previous_right and left < right`, where `previous` is the
      immediately preceding **row** of the same frame. The touch table of Design §5.2 is verified
      literally, plus: the first candle of a frame never crosses; a NaN at either candle of either
      side gives `False`; a crossover implies the corresponding strict comparison at the evaluated
      candle; `crosses_above` and `crosses_below` are never both true at the same candle; a
      crossover cannot be true at two consecutive candles.
- [ ] **AC6 (NaN never fires and never raises).** A condition whose operands are NaN at any candle it
      reads is `False`, not an error and not a missing value: `Evaluation.condition_results` holds
      plain booleans and `triggered` is a plain `bool`. A rule over an indicator that is undefined on
      flat data (`rsi`, `adx`, `stoch`, decision D4) never fires there, and no numpy or pandas warning
      is raised while evaluating (checked with warnings turned into errors).
- [ ] **AC7 (warmup gate).** An evaluation of the candle at row position `i` has `i + 1` candles of
      history; when `i + 1 < rule.warmup()` the evaluation is `triggered=False` whatever the
      conditions say, while `candle_close_ts`, `close_price`, `indicator_values` and
      `condition_results` stay well defined. Verified with an `any` rule whose cheap leg is true and
      whose expensive leg needs 200 candles: no fire before candle 200, and firing is possible from
      `rule.warmup()` candles on.
- [ ] **AC8 (groups).** `all` is the conjunction and `any` the disjunction of its members; a nested
      group is reduced first and the root group combines conditions and nested groups uniformly.
      Evaluation is **complete, never short-circuited**: `condition_results` has exactly one entry per
      element of `rule.all_conditions`, in that order (duplicated conditions included), and
      `indicator_values` is complete even when an early `all` member is already `False`.
- [ ] **AC9 (operand resolution).** An `indicator` operand resolves to
      `registry.compute(indicator, params, candles)[output]`; a `price` operand to the candle column
      named by `PriceField`; a `value` operand to its constant at every candle. Two operands that are
      equal after #6 normalization (for example `{"indicator": "rsi"}` and `{"indicator": "rsi",
      "params": {"length": 14}, "output": "value"}`) resolve to the same values and produce one
      entry in `indicator_values`.
- [ ] **AC10 (`indicator_values`).** It holds one entry per **distinct indicator operand** of the
      rule, and only entries whose value at the evaluated candle is finite (NaN entries are absent,
      not NaN: spec 004 requires finite values in a `Signal`). Price and constant operands are never
      included. Keys follow Design §6 exactly, asserted as literals in tests
      (`rsi(length=14).value`, `macd(fast=12, slow=26, signal=9).hist`,
      `bbands(length=20, std=2.0).upper`, `obv(signal=20).signal`), and insertion order is the first
      appearance of the operand in document order. `Signal(..., indicator_values=evaluation.
      indicator_values, close_price=evaluation.close_price)` is accepted by #4 unchanged.
- [ ] **AC11 (cooldown is not applied here).** Two rules that differ only in `cooldown_bars` produce
      equal evaluations on the same frame; the string `cooldown` appears nowhere in `evaluator.py`.

### Frames, the `N`-candle form and errors

- [ ] **AC12 (the two forms agree).** For every non-empty frame,
      `evaluate(rule, candles)` equals `evaluate_each(rule, candles)[-1]` exactly (`values_equal`,
      `rtol = atol = 0`), and `evaluate_each(rule, candles, last=k)` equals
      `evaluate_each(rule, candles)[-k:]` for every `k >= 1`. `last=None` covers the whole frame, `last`
      greater than the frame length is clamped to it, `last=0` returns an empty tuple, a negative
      `last` raises `ValueError` and a non-`int` (including `bool`) raises `TypeError`.
- [ ] **AC13 (edge frames).** A frame with one candle evaluates without raising (no crossover can
      fire); a frame with two candles is enough for a crossover of price operands; a frame shorter
      than `rule.warmup()` gives `triggered=False`; an empty frame makes `evaluate` raise
      `ValueError` (there is no candle to describe) and `evaluate_each` return an empty tuple.
- [ ] **AC14 (errors).** An invalid frame raises the `CandleValidationError` of #4 (from
      `validate_candles`, also for rules that use no indicator); arguments of the wrong type raise
      `TypeError`; a broken registry raises the `IndicatorError`/`IndicatorComputationError` of #5
      unchanged. No data condition (NaN, flat prices, short history, extreme values) ever raises.

### Purity, cost and look-ahead

- [ ] **AC15 (purity).** `evaluator.py` passes the purity guard with the **default** allowlist (it
      imports no pydantic, no `json`, no clock and no I/O; it uses the #6 model types through
      `trading_bot.domain.rules.schema`), holds no module-level mutable state and no cache across
      calls. `evaluate` and `evaluate_each` never modify `candles` (compared against a copy) and are
      deterministic (two calls on the same frame give equal results).
- [ ] **AC16 (cost structure, asserted without wall-clock).** With a counting registry double
      injected through `registry=`: each distinct `(indicator, params)` pair of the rule is computed
      **exactly once** per call (`macd.macd` and `macd.signal` share one computation), and the number
      of computations depends only on the rule, not on `len(candles)` nor on `last`; `evaluate` and
      `evaluate_each` issue the same number of computations on the same rule and frame. **No test
      asserts elapsed time** (Design §14); timings are reported as evidence.
- [ ] **AC17 (anti look-ahead, `CLAUDE.md` rule 4).** For every `Scenario` and for the rule set of
      Design §13, `assert_no_lookahead_point_in_time(lambda df: evaluate(rule, df),
      evaluations_by_candle(rule), candles)` passes with exact comparison (`rtol = atol = 0`) and a
      non-vacuous report; the reference is the whole-frame `evaluate_each`, returned as an
      object-dtype `pd.Series` indexed by the frame's own labels (candle **open** times, spec 003
      §7). A control evaluator that reads the next candle raises `LookaheadError`, so the check is
      not vacuous. A Hypothesis property runs the same check over drawn rules and frames.

### Typing, docs, gate and scope

- [ ] **AC18 (typing).** `uv run mypy` (strict) passes; `typing.Any` is absent from
      `src/trading_bot/domain` (the spec 004 `git grep` stays empty) and there is no `# type: ignore`
      in `domain/rules/`. The spec 006 §2 extra-flag command is **extended** with
      `src/trading_bot/domain/rules/evaluator.py` and passes (the tech-lead verified it on a
      prototype of Design §3–§8: the pydantic `explicit-any` problem only affects modules that
      subclass `BaseModel`, which the evaluator does not). If it fails for an unforeseen reason, the
      developer keeps the file out of that command and records why in the PR.
- [ ] **AC19 (docs).** `docs/ARCHITECTURE.md` gains a `## Rule evaluation` section between
      `## Rule model` and `## Look-ahead testing`, matching Design §15; the `domain/` row of the
      Layers table drops "(#7)"; the `indicator_values` key of the `## Domain models` example uses
      the real scheme. Python and JSON blocks pass `ruff format --check` where applicable.
      `docs/ROADMAP.md` is not touched (F1 changes status when it is released).
- [ ] **AC20 (gate, coverage and budget).** `uv run python scripts/check.py` is green;
      `src/trading_bot/domain/rules/evaluator.py` has 100% line and branch coverage; the new tests
      add at most **10 s** to `pytest` and no single new test takes more than **2 s**
      (evidence: `--durations`, reported, not asserted).
- [ ] **AC21 (scope).** Only the files of Design §1 change compared with `be0368c`, plus this spec.
      No new runtime dependency, no new `TB_*` variable, no migration, no `Dockerfile` change.

## Design

### 0. Decisions

| Topic | Decision | Why |
|-------|----------|-----|
| Layout | One new module `domain/rules/evaluator.py`; `errors` stay in `rules/errors.py` and no new error class is added | `ARCHITECTURE` announces `rules/ (schema + evaluator)` and spec 006 §0 reserved the file name. The evaluator imports the models, never the other way round, so `schema.py` keeps working without pandas consumers. |
| Result type | A frozen, slotted, keyword-only dataclass, not a pydantic model | Same shape as `Signal` (#4), no `Any` in the domain public API, hashable and cheap to build `N` times, and `values_equal` already compares dataclasses field by field (spec 003 §3.4, spec 004 AC12). Nothing here parses untrusted input, which is the only reason `rules/` uses pydantic. |
| `N`-candle form returns a tuple, not a `pd.Series` | `evaluate_each(...) -> tuple[Evaluation, ...]` | Measured: `pd.Series[Evaluation]` and `pd.Series[object]` are both rejected by pandas-stubs (`Type argument "object" of "Series" must be a subtype of ...`), so a Series return would force `Any` or a `type: ignore` in `domain/`, which the project forbids. A tuple loses nothing: every `Evaluation` carries its own `candle_close_ts`, and the one caller that needs candle labels (the look-ahead reference) builds the Series in `tests/fixtures/evaluations.py`, where the loose typing is harmless. |
| One evaluation per candle, no dropped rows | `evaluate_each` returns a result for **every** candle it covers, including candles below the warmup | Keeps the harness reference total (`missing_reference` cannot happen), makes `evaluate_each(last=k)` a plain slice of the full computation, and gives #24 a row per candle in its "test rule" table. |
| Warmup is applied **per candle**, at rule level | `triggered` is forced to `False` while the history at that candle is shorter than `rule.warmup()`; `condition_results` still report the raw conditions | The issue requires "insufficient warmup → does not fire". Doing it per candle (history = `i + 1` rows of the same frame) is what makes `evaluate(rule, data[:t])` equal the reference at `t`; a frame-level check would make the two forms disagree. It matters only for `any` groups, where a cheap leg could otherwise fire on a rule whose expensive leg is not computable yet (measured: an `any` rule with `sma(200)` fires at candle 30 without the gate). Keeping the raw `condition_results` means the dashboard can still explain why nothing fired. Confirmed by the user as decision D12. |
| Crossover at equality | `crosses_above` = `previous_left <= previous_right and left > right` (mirror for `crosses_below`) | The convention of the charting tools users come from (a touch followed by a break counts as a cross), and it gives clean properties: a crossover implies the strict comparison at `t`, the two crossovers are mutually exclusive, and the same crossover cannot repeat on consecutive candles. The strict variant (`<` on the previous candle) silently loses every cross that touches first, which is common on `1d` data with round constants such as `30` or `70`. Confirmed by the user as decision D13. |
| No short-circuit | Every condition of the rule is evaluated | `indicator_values` must be complete for the Telegram message (#17) and `condition_results` for the dashboard; the cost is bounded by #6 (20 conditions, 40 operands) and is vectorized anyway, so short-circuiting would save nothing measurable while making the result depend on evaluation order. |
| Per-condition outcomes in the result | `Evaluation.condition_results: tuple[bool, ...]`, aligned with `rule.all_conditions` | It is the "why did this fire?" data #24 needs and the sharpest handle for the mandatory case table: a test can assert the outcome of each operator directly instead of inferring it through the group logic. It costs one tuple of at most 20 booleans, stays hashable and immutable, and #14 may ignore it. |
| Vectorized over the whole frame | Indicators and conditions are computed once as arrays over the frame; only the requested positions are materialized into `Evaluation` objects | Indicator cost is `O(n)` and unavoidable (the value at the last candle needs the whole history), so evaluating per candle in a Python loop would be `O(N x n)`. With arrays, `evaluate` and `evaluate_each` share one code path, which guarantees they agree bitwise. Look-ahead safety is preserved because every per-candle value depends only on that candle and earlier ones (registry outputs are look-ahead-free, #5; comparisons are row-local; crossovers read the previous row), and AC17 verifies it instead of assuming it. |
| Per-call computation cache | A local `dict` keyed by `(indicator, params)` inside the call | Spec 005 §12 requires each distinct pair to be computed once per evaluation. A module-level cache would be mutable global state (purity guard) and would keep frames alive; a per-call dict is enough because one call sees one frame. |
| Injectable registry | `registry: IndicatorRegistry = REGISTRY`, keyword-only | Spec 005 §12 allows it, it makes AC16 testable with a counting double without monkeypatching, and it keeps `compute` catalog-agnostic. Documented limit: `rule.warmup()` and the rule's stored `params` come from the catalog that validated the rule (spec 006 §0), so an injected registry must be catalog-compatible; it is meant for instrumented doubles, not for a different catalog. |
| Empty frame | `evaluate` raises `ValueError`; `evaluate_each` returns `()` | An empty frame is valid for #4 but has no candle to describe, so `evaluate` cannot honour its return contract. No new exception class: callers never branch on this, it is a caller bug (#14 checks the frame before evaluating), and every other domain module already raises plain `ValueError` subclasses only where callers do branch. |
| Indicator value keys | `indicator(param=value, ...).output` (Design §6) | Unique for every distinct operand (a rule with `sma(20)` and `sma(200)`, or with `macd.macd` and `macd.signal`, is common), self-describing in a Telegram message, stable in stored JSON (#13) and derived deterministically from the canonical params of #6. Confirmed by the user as decision D14. |
| Prices are not in `indicator_values` | Only indicator operands | `close_price` is already a field and the full candle is available to #14; duplicating `close` under a second name would make the stored JSON ambiguous and grow every signal. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/rules/evaluator.py` | developer | `Evaluation`, `evaluate`, `evaluate_each` and private helpers (§2–§8) |
| `src/trading_bot/domain/__init__.py` | developer | Docstring: the `rules` subpackage also holds the evaluator (spec 007), as #4–#6 kept this package map current (added in review) |
| `src/trading_bot/domain/rules/__init__.py` | developer | Docstring mentions the evaluator alongside the schema |
| `tests/fixtures/evaluations.py` | developer | `evaluations_by_candle` (the look-ahead reference) and `triggered_by_candle` (§13) |
| `tests/unit/test_rule_evaluator.py` | developer | TDD: T1, T6, T7 |
| `tests/unit/test_rule_operators.py` | developer | TDD: T2, T3, T4, T5 |
| `tests/unit/test_rule_evaluator_lookahead.py` | developer | TDD: T8 |
| `tests/unit/test_domain_purity.py` | developer | T12: `rules/evaluator.py` added to the scanned-module set; **no** new entry in the per-file allowlist |
| `tests/unit/test_rules_typing_guard.py` | tester (#6 file, edited by the developer) | `evaluator.py` added to the exact `rules/` file set the guard pins, nothing else: the guard fails as soon as the module exists (added in review) |
| `tests/fixtures/rule_strategies.py` | tester | Hypothesis strategies over rules (§13) |
| `tests/unit/test_rule_evaluator_properties.py` | tester | T9, T10 |
| `tests/unit/test_rule_evaluator_adversarial.py` | tester | T11 |
| `docs/ARCHITECTURE.md` | developer | `## Rule evaluation` (§15), the Layers row and the example key; explicitly authorized by this spec |
| `docs/specs/007-rule-evaluator.md` | tech-lead | This spec |

No new Protocols, no migrations, no `TB_*` variables, no dependency change.

### 2. Public API

```python
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.rules.schema import Rule
from trading_bot.domain.signals import IndicatorValues

__all__ = ["Evaluation", "evaluate", "evaluate_each"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    triggered: bool
    candle_close_ts: datetime  # rule.timeframe.nominal_close(open time), stdlib UTC
    close_price: float
    indicator_values: IndicatorValues  # finite values of the indicator operands used
    condition_results: tuple[bool, ...]  # aligned with rule.all_conditions


def evaluate(
    rule: Rule, candles: pd.DataFrame, *, registry: IndicatorRegistry = REGISTRY
) -> Evaluation: ...


def evaluate_each(
    rule: Rule,
    candles: pd.DataFrame,
    *,
    last: int | None = None,
    registry: IndicatorRegistry = REGISTRY,
) -> tuple[Evaluation, ...]: ...
```

- `evaluate(rule, candles)` is about `candles.index[-1]`, which the evaluator treats as the last
  closed candle: removing the in-progress candle is the data layer's job (#8).
- `evaluate_each` returns one `Evaluation` per covered candle, oldest first;
  `evaluate_each(rule, candles)[-1] == evaluate(rule, candles)`.
- `last` selects the last `N` candles: `None` means all, `0` gives `()`, values above the frame
  length are clamped, negatives raise `ValueError`, non-integers (including `bool`) raise
  `TypeError`. Clamping keeps "give me the last 500 candles" correct on a short history and makes
  the slice identity of AC12 total.
- Both functions are pure: same arguments, same result; the frame is never modified.

### 3. Pipeline

One private core builds arrays over the whole frame; the public functions materialize the
`Evaluation` objects they need.

1. Type checks on `rule` and `candles`, then `validate_candles(candles)` (#4). Validating up front
   means a rule of prices and constants is validated too, not only rules that reach the registry.
2. **Operand values** (§4): one `float64` array per distinct operand, over the whole frame.
3. **Condition masks** (§5): one boolean array per distinct condition.
4. **Group reduction** (§7): `all` → logical AND, `any` → logical OR, nested groups first.
5. **Warmup gate** (§8): positions with fewer than `rule.warmup()` candles of history are forced to
   `False`.
6. **Materialization**: for each requested position, `triggered`, `candle_close_ts`, `close_price`,
   the finite indicator values (§6) and the condition results, in one `Evaluation`.

Steps 2–5 do not depend on which positions are requested, so `evaluate` and `evaluate_each` produce
bitwise identical values; only step 6 differs in how many objects it builds. Measured on the
developer machine (pandas 3.0.5, numpy 2.5.3, TA-Lib 0.8.0), 1 000 candles: a two-condition rule
takes 0.44 ms for `evaluate` and 12 ms for `evaluate_each` over all candles; a nine-condition rule
with thirteen indicator operands takes 1.8 ms and 36 ms.

### 4. Operand resolution

| Operand | Values over the frame |
|---------|-----------------------|
| `{"indicator": ..., "params": ..., "output": ...}` | `registry.compute(indicator, params, candles)[output]`, as a `float64` array (NaN before `lookback` and where undefined, never `±inf`: spec 005) |
| `{"price": column}` | The candle column named by `PriceField` (`OHLCV_COLUMNS` of #4), `float64` by the frame contract |
| `{"value": x}` | The constant `x` at every candle |

- Operands are de-duplicated by model equality (#6 models are frozen and hashable), and indicator
  computations by `(indicator, params)`, so `macd.macd` and `macd.signal` cost one computation and
  two array views. This is the spec 005 §12 obligation.
- `output` is always explicit in a parsed rule (#6), so the evaluator never calls `resolve_output`.
- Conversions use `np.asarray(series, dtype=np.float64)`; `Series.to_numpy(dtype=...)` is typed as
  `floating[Any]` by pandas-stubs and would need an ignore (measured).

### 5. Operators

#### 5.1 Comparisons

Evaluated at the candle only, with the left value `l` and the right value `r` at that candle:

| Case | `<` | `<=` | `>` | `>=` |
|------|-----|------|-----|------|
| `l < r` | True | True | False | False |
| `l == r` | False | True | False | True |
| `l > r` | False | False | True | True |
| `l` is NaN | False | False | False | False |
| `r` is NaN | False | False | False | False |
| both NaN | False | False | False | False |

IEEE comparison already returns `False` for every NaN case, which is the intended semantics, not an
accident: the tests pin it so a future rewrite with `fillna` or masked arrays cannot change it.
`-0.0 == 0.0`, so a value operand of `0` and a computed `-0.0` compare equal.

#### 5.2 Crossovers

With `pl`, `pr` the values at the **previous row** of the same frame:

- `crosses_above` = `pl <= pr and l > r`
- `crosses_below` = `pl >= pr and l < r`

At row position 0 there is no previous row, so both are `False`. Touch table, `close` against the
constant `100` (verified on the prototype):

| # | previous close | close | `crosses_above` | Why |
|---|----------------|-------|-----------------|-----|
| 0 | — | 99 | False | no previous candle |
| 1 | 99 | 100 | False | equal, not above |
| 2 | 100 | 101 | **True** | touched, then broke above |
| 3 | 101 | 101 | False | already above |
| 4 | 101 | 100 | False | equal, not above |
| 5 | 100 | 99 | False | below |
| 6 | 99 | 100 | False | equal, not above |
| 7 | 100 | 100 | False | equal, not above |
| 8 | 100 | 101 | **True** | touched, then broke above |

Consequences to pin as tests: a crossover implies the strict comparison at the candle; the two
crossovers are never simultaneously true; a crossover cannot be true at two consecutive candles
(after firing, the previous-candle test fails); a NaN at either candle of either side gives `False`,
so a crossover needs **two finite candles** (spec 005 §12).

### 6. Indicator value keys

```text
key = f"{indicator}({p1}={v1}, {p2}={v2}, ...).{output}"
```

- Parameters in the indicator's declaration order, with the canonical values of #6 (integers as
  integers, `bbands` `std` as a float): `rsi(length=14).value`,
  `macd(fast=12, slow=26, signal=9).hist`, `bbands(length=20, std=2.0).upper`,
  `stoch(length=14, smooth_k=3, smooth_d=3).k`, `obv(signal=20).signal`, `sma(length=200).value`.
- The output is always present, including for single-output indicators, so the scheme does not
  change if an indicator ever gains a second output (which would otherwise rewrite the keys of
  stored signals).
- Only finite values are kept; an entry whose value is NaN at the evaluated candle is **absent**.
  `IndicatorValues` (#4) rejects non-finite values, so this is also what makes the mapping
  constructible.
- Insertion order is the first appearance of the operand in document order (`rule.all_conditions`,
  `left` before `right`), which is the order a message would list them in.
- Tests assert literal keys and never rebuild them from the implementation.

### 7. Groups

- `rule.conditions` is always a group (#6 D9). Members are walked through the `mode`/`members`
  properties of #6, so both group families are handled without `getattr`.
- A nested group is reduced to one boolean array first; the root group then combines conditions and
  nested groups. Groups are never empty (#6 rejects them), so no neutral element is needed; a group
  of one member equals that member.
- Evaluation is complete (§0). `condition_results[i]` is the raw outcome of
  `rule.all_conditions[i]`, before the warmup gate.

### 8. Warmup gate

```text
history(i) = i + 1                      # candles at or before the evaluated candle, in this frame
triggered(i) = group_result(i) and history(i) >= rule.warmup()
```

- `rule.warmup()` (#6 §10) already accounts for indicator lookbacks and for the extra candle a
  crossover needs.
- The gate is a function of the row position only, so it is identical on a prefix and on the full
  frame: row positions agree because every prefix starts at the same first candle. This is what
  `assert_no_lookahead_point_in_time` verifies at `k = 0` and beyond.
- It changes an outcome only for `any` groups (with `all`, an operand below its warmup is NaN and
  the condition is already `False`). Measured example: `any` of [`close > 50`, `close > sma(200)`]
  on 30 candles gives `condition_results == (True, False)` and `triggered=False`.
- `stable_warmup` is **not** used here: requiring it would silence young tickers (spec 005 §8).
  Fetching enough history for reproducible values is #8/#14's obligation.

### 9. Purity and cost

- No clock, no I/O, no randomness, no globals; the only module-level names are constants, the
  dataclass and the functions. The purity guard (T12) covers it with the default allowlist: the
  evaluator imports `dataclasses`, `datetime`, `collections.abc`, `typing`, `numpy`, `pandas` and
  `trading_bot.domain.*` only — **no pydantic**, because it consumes model instances rather than
  defining models.
- The frame is read, never written: no `inplace`, no column assignment. The harness checks it
  (`input_mutated`) and T7 checks it directly against a copy.
- Costs are structural, not timed (AC16): computations per call equal the number of distinct
  `(indicator, params)` pairs, independent of the frame length and of `last`.
- `validate_candles` runs once in the evaluator and once inside every `registry.compute` call; that
  is `O(k n)` scans of a validated frame and is left as is (measured cost is negligible next to the
  TA-Lib calls). Optimizing it would mean a private "already validated" flag, which is state.

### 10. What #14 must still do

The evaluator answers "do the conditions hold at this candle?" and nothing else. The engine (#14):

- evaluates only rules whose `timeframe` equals the ticker's (#12, decision D11), on a frame whose
  in-progress candle was dropped by #8 and that holds at least `rule.stable_warmup()` candles when
  available;
- builds `Signal(ticker=..., timeframe=rule.timeframe, rule_id=..., side=rule.signal,
  candle_close_ts=evaluation.candle_close_ts, close_price=evaluation.close_price,
  indicator_values=evaluation.indicator_values)` when `triggered` is true;
- deduplicates by `Signal.idempotency_key` (`CLAUDE.md` rule 5) before notifying;
- applies `cooldown_bars` **by row position** over the evaluated frame (spec 004 §5: never
  `(t2 - t1) / duration`), against the last notified signal of the same ticker and rule;
- never calls the evaluator with an empty frame.

### 11. Errors

| Situation | Result |
|-----------|--------|
| `rule` is not a `Rule`, `candles` is not a `DataFrame`, `last` is not an `int`/`None` | `TypeError` |
| `last` is negative | `ValueError` |
| Empty frame, in `evaluate` | `ValueError` ("no candle to evaluate") |
| Frame that breaks the #4 contract | `CandleValidationError` (raised by `validate_candles`) |
| Broken registry or catalog | `IndicatorError` / `IndicatorComputationError` of #5, unchanged |
| `registry` is not an `IndicatorRegistry` (doubles must subclass it) | `TypeError` |
| An open time with sub-microsecond precision in a **returned** row | `ValueError` from `to_utc` (spec 004): the #4 contract accepts any index unit, but a signal identity is never rounded. Rows outside the returned ones are not identified and do not raise. No provider produces such labels; label normalization belongs to the data layer (#8). Added in review |
| NaN values, flat prices, history shorter than the warmup, extreme magnitudes | **No exception**: `triggered=False` |

Messages are single-line English, and no user-controlled text is echoed (a rule name is never part
of an evaluator message).

### 12. Forward compatibility

| Issue | How it uses #7 |
|-------|----------------|
| #8 | Guarantees the frame contract and that the last row is a **closed** candle; the evaluator trusts it and says so in its docstring. |
| #13 | Stores `indicator_values` as JSON; the §6 keys are the stored keys. Signal identity comes from `candle_close_ts`, which #7 derives from the frame label. |
| #14 | §10. Reads `triggered`, `candle_close_ts`, `close_price`, `indicator_values`; may log `condition_results`. |
| #17 | Renders `rule.name`, the close price, the §6 keys (or `describe()` labels for them) and the chart; never shows a NaN because absent keys do not exist. |
| #24 | "Test rule" calls `evaluate_each(rule, candles, last=N)` and shows `triggered` plus `condition_results` aligned with `rule.all_conditions`. |
| F8 | Backtesting calls `evaluate_each(rule, candles)` over the whole history: one indicator pass for the frame, one `Evaluation` per candle. If materialization ever dominates, a boolean-only entry point can be added without changing these semantics. |

### 13. Test fixtures and rule set

`tests/fixtures/evaluations.py` (developer):

```python
def evaluations_by_candle(
    rule: Rule, *, registry: IndicatorRegistry = REGISTRY
) -> Callable[[pd.DataFrame], object]:
    """The look-ahead reference: one Evaluation per candle, as an object-dtype Series indexed by
    the frame's own labels (candle open times)."""


def triggered_by_candle(
    rule: Rule, *, registry: IndicatorRegistry = REGISTRY
) -> Callable[[pd.DataFrame], object]:
    """The same, reduced to a boolean Series: readable failures for trigger-only checks."""
```

`tests/fixtures/rule_strategies.py` (tester): Hypothesis strategies drawing valid `Rule` objects
from the real catalog, with a cap on indicator parameters so `rule.warmup()` stays small (frames in
property tests are then `warmup + a few` candles). The private strategy of
`tests/unit/test_rule_schema_properties.py` (#6) is deliberately **not** reused: `tests/` must not
import from `tests.unit` (spec 004 AC17b), and the two strategies have different goals (#6 draws
name and parameter edge cases for round-trips, #7 needs cheap, evaluable rules). Unifying them is a
follow-up once #3–#6 are merged.

Rule set for the look-ahead and case-table tests (built from `tests.fixtures.rules` payloads):

| Label | Rule | What it stresses |
|-------|------|------------------|
| `price_only` | `close crosses_above open` | Warmup 2, no indicator, cheap full sweeps |
| `architecture` | The `## Rule model` example (`rsi crosses_below 30` and `close > sma(200)`) | Crossover with a constant, long warmup, `all` |
| `nested` | `any` of [`all` of two conditions, `any` of two conditions] | Both group modes at both levels |
| `wide` | Nine conditions over ten indicators, including `macd.macd` vs `macd.signal` and `obv.value` vs `obv.signal` | Multi-output dedupe, undefined regions (`adx`, `stoch`) |

Measured harness cost on the developer machine (300 candles, default cuts): about 0.35 s per
scenario for `architecture` and 0.9 s for `wide`; a full sweep of `architecture` on 300 candles is
3.4 s. Therefore: sweep every `Scenario` with the cheap rules, check `wide` on `MIXED` only, and
keep full sweeps for `price_only` on a short frame. As implemented (accepted in review), each rule's
frame is sized to at least `max(120, rule.warmup() + 60)` candles instead of a flat 300, which
covers candles on both sides of the warmup gate on every scenario; a test pins the bound.

### 14. Testing rules for this feature

- **No wall-clock assertions.** Budgets are evidence (`--durations`), never assertions: CI runners
  stall and such assertions are flaky (the #6 branch had to remove four of them).
- **No platform-dependent expectations.** Assert the invariant, not the path through an interpreter
  or library internal.
- **Exact comparison** in every look-ahead check (`rtol = atol = 0`, spec 003 §3.4). TA-Lib
  recurrences and the condition layer are bitwise stable; a tolerance would hide a real peek.
- Literal expectations (keys, touch tables, timestamps) are written out, never recomputed with the
  code under test.

### 15. `docs/ARCHITECTURE.md` — `## Rule evaluation`

A new section between `## Rule model` and `## Look-ahead testing`, stating:

1. the two entry points with a short example (`evaluate`, `evaluate_each(last=...)`) and the
   `Evaluation` fields;
2. that the last row of the frame is the last closed candle and that dropping the open candle is
   #8's job;
3. the operator table of §5.1 and the crossover definition with the touch cases of §5.2;
4. NaN never fires and never raises (decision D4), and the warmup gate of §8, including that
   `stable_warmup` is not required to fire;
5. the indicator value key scheme of §6 with two examples, and that only finite values appear;
6. the purity and cost notes of §9 (each distinct `(indicator, params)` computed once, no cache);
7. what #14 must add: cooldown by position, idempotency, `Signal` construction (§10);
8. a link to this spec, as `## Indicators` links to spec 005.

Also: drop "(#7)" from the `domain/` row of the Layers table, and change the
`indicator_values={"rsi_14": 28.4}` of the `## Domain models` example to the real key
(`"rsi(length=14).value"`), so the documentation shows one scheme.

## Test plan

All tests are unit tests, without network, on synthetic frames (decision D2). Mandatory template
cases: **anti look-ahead** is T8 and T9 (`CLAUDE.md` rule 4); **idempotency** is covered indirectly,
because `candle_close_ts` is the identity field of a signal and T1 pins it, while deduplication
itself belongs to #13/#14; **authorization** and **secret redaction** are N/A (no Telegram, API,
config or logging code, no secrets).

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 result shape and identity | unit | `Evaluation` fields, frozen/slots/kw_only, hashable, equal by value, pickle, `values_equal`; `candle_close_ts` literal for `1h`/`4h`/`1d`; `close_price`; element `i` of `evaluate_each` describes candle `i`; UTC tzinfo | AC1–AC3 | developer |
| T2 comparison table | unit | The §5.1 table for every operator over `less`/`equal`/`greater`/NaN cases, on hand-built frames; every `Operator` member covered (set assertion) | AC4, AC6 | developer |
| T3 crossover table | unit | The §5.2 touch table literally for both crossovers; first candle; NaN at `t-1`; implies the strict comparison; mutual exclusion; no two consecutive fires; two-candle frame | AC5, AC6 | developer |
| T4 groups and nesting | unit | `all`/`any` at the root, `all` of `any`, `any` of `all`, single-member group, duplicated and contradictory conditions; `condition_results` aligned with `rule.all_conditions` (length and order); no short-circuit (complete values when an early member is False) | AC8 | developer |
| T5 NaN, warmup, edge frames | unit | Flat frames (`rsi`, `adx`, `stoch` undefined → never fires, keys absent); `any` rule below warmup does not fire but reports `(True, False)`; firing possible exactly at `warmup`; frames of 0, 1 and 2 candles; empty frame (`ValueError` / `()`); `cooldown_bars` ignored | AC6, AC7, AC11, AC13 | developer |
| T6 indicator values | unit | Distinct operands only, dedupe of equal operands, distinct params and outputs; NaN omitted; literal keys for `rsi`, `macd`, `bbands`, `stoch`, `obv`, `sma`; insertion order; the mapping is accepted by `Signal` | AC9, AC10 | developer |
| T7 purity, injection, cost structure | unit | Frame unchanged (compared with a copy); determinism; counting registry double: one computation per distinct `(indicator, params)`, count independent of `len(candles)` and `last`, same count for both entry points; `evaluate` equals `evaluate_each[-1]`; `last` slice identity, clamping, `0`, negative, `bool`, non-int | AC12, AC15, AC16 | developer |
| T8 anti look-ahead | unit | `assert_no_lookahead_point_in_time` with `evaluations_by_candle` per `Scenario` for `price_only`, `architecture` and `nested`; `wide` on `MIXED`; report non-vacuous; a control evaluator reading candle `i + 1` raises `LookaheadError` | AC17 | developer |
| T9 properties | unit (`@given`) | Drawn rules and frames: the two forms agree; slice identity; determinism; no mutation; no fire below warmup; crossover implications and mutual exclusion; `<` and `>=` complementary on finite operands and both False on NaN; monotonicity in a constant operand; the look-ahead harness with `max_cuts=8` | AC5, AC12, AC15, AC17 | tester |
| T10 case-table completeness | unit | The parametrized tables cover every `Operator`, both `GroupMode`s at both levels and every operand-kind pair; set assertions against the enums so a new operator fails the suite | AC4, AC8 | tester |
| T11 adversarial | unit | `Scenario.EXTREME` and `GAPS` frames; prices near the clipping bounds and `1e12` volumes; constants at `±1e15` and `5e-324`; a rule at the #6 bounds (20 conditions, 10 members per group, all ten indicators); `obv`/`adx`/`stoch` undefined regions; warmup above the frame length; no warning escapes (warnings as errors); nothing raises outside §11 | AC6, AC13, AC14 | tester |
| T12 purity guard | unit | `rules/evaluator.py` in the scanned set; it needs **no** per-file allowlist entry (pydantic absent); no clock, no mutable module state | AC15 | developer |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC20 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot.domain --cov-branch --cov-report=term-missing`: 100% line and branch for `domain/rules/evaluator.py`, no regression elsewhere | AC20 |
| V3 budget | `uv run pytest --durations=40 -q`: new tests add at most 10 s, none above 2 s. Reported, never asserted (§14) | AC20 |
| V4 typing | `uv run mypy`; the spec 006 §2 extra-flag command extended with `src/trading_bot/domain/rules/evaluator.py`; `git grep` for `Any` over `src/trading_bot/domain` empty; `git grep -n "type: ignore" src/trading_bot/domain/rules` empty | AC18 |
| V5 dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` identical to the same command on `be0368c`; `git diff be0368c -- pyproject.toml uv.lock` empty | AC21 |
| V6 docs | `## Rule evaluation` read against §15; `uv run ruff format --check docs/ARCHITECTURE.md` | AC19 |
| V7 scope | `git diff be0368c --name-only` plus `git status --porcelain` list only the §1 files; after #3–#6 merge and a rebase, compare with `origin/main` | AC21 |
| V8 secrets and language | `python scripts/secret_scan.py --staged`; English-only review of the diff, including rule names in test payloads | — |
| V9 Docker | The image gains one module but no runtime dependency and no startup import; the local build stays **BLOCKED** (no daemon). The authoritative checks are the PR's `Docker build (arm64)` job and the beta deploy with `/health`, verified by the lead | AC21 |

**TDD order suggested to the developer:** T1 (the dataclass and the last-candle identity on a
price-only rule) → T2 → T3 → T4 → T5 → T6 → T7 (the counting registry double) → T12 → T8 → the
shared fixtures (extract `evaluations_by_candle` as soon as T8 exists) → docs.

## Risks and security

- **A wrong crossover or NaN rule sends a false signal.** The bot only notifies, but a false signal
  costs the user's trust and possibly money through their own decision. Mitigations: the literal
  case tables (T2, T3), the property tests (T9), and NaN semantics asserted rather than inherited
  from IEEE by accident.
- **Look-ahead.** The whole feature is a per-candle computation, exactly what rule 4 targets. The
  vectorized design is the main risk (a single `shift(-1)`, a global statistic or a `bfill` would
  leak the future); T8 and T9 check it on every scenario and on drawn rules, with a control cheat to
  prove the check bites. The warmup gate is deliberately a function of the row position only.
- **Silent divergence between the two entry points.** They share one core (§3) and AC12 pins their
  equality, including through the harness at `k = 0`.
- **Cost on the Raspberry Pi.** One rule over 1 000 candles is under 2 ms, and #14 evaluates a
  handful of rules per ticker at a candle close. `evaluate_each` is only used by the dashboard and
  by backtesting, both on demand.
- **Key-scheme churn.** Changing the §6 keys after #13 stores signals leaves old rows with old keys.
  Fixing the scheme now, before anything is persisted, is the mitigation; the always-present output
  suffix removes the most likely future cause of a change.
- **Purity erosion.** A later "just cache the indicator frames" would introduce global state and
  break both determinism and the harness; the guard and AC15/AC16 forbid it.
- **Sensitive data.** None: no tokens, hosts, IPs, users, `TB_*` variables, migrations or
  `secrets.env` changes. Test payloads use invented English rule names and synthetic candles.
- **Unbreakable rules.** Signal-only (the evaluator returns a boolean and values, never an order),
  pure `domain/`, closed candles (the last row is the last closed candle, the open candle is #8's
  responsibility; the anti look-ahead tests are mandatory here), idempotency (`candle_close_ts` is
  the identity field #14 deduplicates on), UTC (timestamps come from `nominal_close`, always UTC),
  single worker (untouched), no `eval` (the evaluator dispatches over closed enums), English only.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches (#3 → #4 → #5 → #6 → #7).
- **D2 (2026-09-14):** synthetic fixtures only in M1; recorded fixtures arrive with #10.
- **D3 (2026-09-14):** `obv` has a signal line and no default output.
- **D4 (2026-09-14):** undefined indicator values are NaN, so rules never fire on them. This feature
  implements that rule (AC6).
- **D5 (2026-09-14):** indicator parameter names, defaults and ranges as in spec 005; they appear in
  the §6 keys.
- **D6–D10 (2026-09-15):** rule nesting (root plus one level), `cooldown_bars` default `0` in
  `[0, 500]`, rule name bounds, `conditions` always a group, and the size limits. The evaluator
  relies on all of them (§7, §10).
- **D11 (2026-09-15):** a rule's timeframe must equal the timeframe of the tickers it is assigned to
  (enforced in #12); the evaluator uses `rule.timeframe` for `nominal_close` and assumes the frame
  is on that timeframe.
- **D12 (2026-09-16): the warmup gate applies to the whole rule.** A rule does not fire until every
  indicator it uses has enough history: `triggered` is `False` while the candle has fewer than
  `rule.warmup()` candles behind it, whatever the individual conditions say. Otherwise a rule would
  fire with part of its logic uncomputable and the user would believe a rule matched that was never
  fully evaluated. It only changes an outcome for `any` groups (§8, AC7); `condition_results` still
  report the raw conditions, so a non-fire stays explainable.
- **D13 (2026-09-16): a touch counts as a cross.** `crosses_above` is
  `previous_left <= previous_right and left > right` (mirror for `crosses_below`), the convention of
  common charting tools. The strict variant (`<` on the previous candle) would silently drop every
  cross that first touches a round level such as `30` or `70`, which is frequent on daily candles
  (§5.2, AC5).
- **D14 (2026-09-16): indicator value keys are descriptive.** `rsi(length=14).value`,
  `macd(fast=12, slow=26, signal=9).hist`, `bbands(length=20, std=2.0).upper`: unique per operand,
  self-describing in a Telegram message and stable in stored JSON. The compact form (`rsi_14`) was
  rejected as ambiguous for multi-parameter and multi-output indicators (§6, AC10).

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria, and T1–T8 fail without the implementation
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)
- [x] Documentation findings of the first round fixed

Reviewed on 2026-09-16 (first round): REQUEST CHANGES, documentation only. The evaluator and its
tests are accepted as they are: D12–D14 match the implementation, nine runtime mutations
(strict crossover, next-row crossover, no gate, frame-level gate, gate off by one, back-filled
indicators, NaN as zero, swapped group modes, compact keys) each fail the developer tests, and the
three look-ahead mutations fail T8 alone. The property tests also pass under Hypothesis' CI
profile. Accepted deviations: the typing guard edit and the per-rule look-ahead frame sizes (§1,
§13), the `TypeError` for a registry that is not an `IndicatorRegistry` and the sub-microsecond
`ValueError` (§11), and computing indicators on empty frames and for `last=0` (AC16). The AC21,
V5 and V7 baseline is corrected to `be0368c`, which already changed spec 006 and two #6 test
files. Requested: the `domain/__init__.py` package map (§1), the sub-microsecond error in the
`ARCHITECTURE` error contract, and an inaccurate bound comment in `tests/fixtures/rule_strategies.py`.

Reviewed on 2026-09-16 (second round): the three findings are fixed, only those three files
changed since the first round, and `scripts/check.py --fast` is green. Approved.
