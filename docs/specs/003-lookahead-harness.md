# 003 — Reusable look-ahead test harness

- **Status:** approved
- **Branch:** `feature/lookahead-harness`
- **Spec author:** tech-lead
- **Issue:** #3 (milestone M1 · Analysis core; first of the stacked series #3 → #4 → #5 → #6 → #7)
- **Expected commit type:** `test:`. The change adds test infrastructure (`tests/`), dev-only dependencies, the mypy file list and documentation. Runtime code and the runtime dependency set do not change, so `feat` (minor bump) would misrepresent it; `build:`/`docs:` describe only part of it. `test:` → patch bump.

## Goal

Give every indicator and rule a single, reusable way to prove `CLAUDE.md` rule 4: the result at candle `t` computed with `data[:t]` is identical to the one computed with `data[:t+k]` truncated to `t`. The harness, a seeded synthetic OHLCV generator and a Hypothesis strategy live in `tests/`, so #5 (indicator registry) and #7 (rule evaluator) can use them without rework.

Notation used throughout: `data[:t]` is **inclusive** of `t` (label slicing, `.loc[:t]`). A *cut* is a prefix length `n` (1-based); its candle is `t = candles.index[n - 1]`. `k ≥ 1` is the number of candles appended after `t`.

## Out of scope

- **Recorded real-data fixtures. Deliberately deferred to #10 (YFinanceProvider)** by user decision D2: the repository is public and Yahoo's terms restrict redistribution of its data. This consciously narrows the #3 scope item "recorded real-data fixtures in `tests/fixtures/`": M1 uses synthetic data only. #10 records its own provider fixtures and decides their format and licensing.
- Any change under `src/`. `domain/` is not touched: `Timeframe`, the candle contract and `validate_candles` (#4), the indicator registry and TA-Lib (#5), the rule schema (#6) and the evaluator (#7).
- Adding `pandas`/`numpy` as **runtime** dependencies (#4 does it) and `TA-Lib` (#5). **Never `pandas-ta`.**
- Realistic market calendars (NYSE sessions and holidays, #9) and session-aligned 4h resampling (#10). The generator only imitates gaps.
- Type-checking the whole `tests/` tree: only the harness and fixture modules are added to mypy.
- Timing assertions inside tests or benchmarking tooling (the runtime budget is verified as evidence, see Test plan).
- Changes to `scripts/check.py`, `.github/`, `deploy/`, `Dockerfile`, `.claude/`, `CLAUDE.md`, `docs/ROADMAP.md` or existing tests.

## Acceptance criteria

### Harness: series-valued functions (`tests/lookahead.py`)

- [ ] **AC1 (catches cheating functions):** with default arguments on `synthetic_candles(250, seed=7)`, `assert_no_lookahead` raises `LookaheadError` (a subclass of `AssertionError`) with the stated `violation.kind` for each cheating function:

  | Cheating function (on `close = df["close"]`) | Expected `kind` |
  |----------------------------------------------|-----------------|
  | `close.shift(-1)` | `value_mismatch` |
  | `close.rolling(5, center=True).mean()` | `value_mismatch` |
  | `close.rolling(20).mean().bfill()` | `value_mismatch` |
  | `close / close.max()` (global max) | `value_mismatch` |
  | `close - close.mean()` (global mean) | `value_mismatch` |
  | rule-like boolean `close.shift(-1) > close` (peeks at the next candle) | `value_mismatch` |
  | filter `close[close < close.shift(-1)]` (a row appears only once the next candle exists) | `result_appeared` |

- [ ] **AC2 (clear failure message):** for a module-level cheating function `cheat_shift_forward` (`close.shift(-1)`), the violation is reported at the first offending position in check order (Design §3.5): `prefix_length == 1`, `k == 1`, `t == stamp == candles.index[0]`, `prefix_value` is NaN and `extended_value == candles["close"].iloc[1]`. `str(error)` contains: the words `look-ahead`, the function's `__qualname__`, the kind, `t` in ISO 8601 (`2024-01-01T00:00:00+00:00`), `n=1`, `k=1`, the offending candle timestamp, `nan`, `repr(float(candles["close"].iloc[1]))` and a `hint:` line.
- [ ] **AC3 (honest functions pass):** `assert_no_lookahead` returns a `LookaheadReport` without raising, on every `Scenario` and the default timeframe, for: `rolling(20).mean()`, `rolling(20).std()`, `ewm(span=10, adjust=False).mean()`, `ewm(span=10).mean()`, `cumsum()` of signed volume (OBV-like), `close / close.cummax() - 1`, `diff()` and `shift(1)`, a `DataFrame` with two columns (Bollinger-like), a `dict[str, Series]` with three outputs (MACD-like), a boolean crossover `(close > sma) & (close.shift(1) <= sma.shift(1))`, a warmup-dropped output (`rolling(20).mean().dropna()`) and a windowed output (the last 20 rows of `rolling(5).mean()`).
- [ ] **AC4 (comparison semantics):** comparison is exact by default (Design §3.4):
  - a function whose past values change by one ulp (`numpy.nextafter`) once the frame reaches 100 candles fails with `value_mismatch`, and passes with `rtol=1e-12`;
  - NaN at the same stamp on both sides is equal (AC3 warmup cases pass);
  - a function that returns `float64` below 100 candles and `object` from 100 on fails with `dtype_mismatch`;
  - a function that returns `{"a": s}` below 100 candles and `{"a": s, "b": s}` from 100 on fails with `outputs_changed`;
  - a function that drops the stamp at position 50 once the frame reaches 100 candles fails with `result_disappeared`.
- [ ] **AC5 (cut selection and bounded cost):**
  - `select_cuts` follows Design §3.3: strictly increasing, within `[min_prefix, length - 1]`, at most `max_cuts` items, deterministic, a full sweep when `length - min_prefix <= max_cuts`, and it includes the first `min(5, max_cuts // 3)` and last `min(5, max_cuts // 3)` candidates;
  - `report.cuts` equals `select_cuts(...)`, or the sorted, de-duplicated explicit `cuts` when given;
  - `report.calls <= len(report.cuts) * (len(report.ks) + 1) + 2`;
  - a `k` with `n + k > length` is skipped, and the full-frame comparison (`k = length - n`) is always made. With `ks=(1,)` and `cuts=[10]`, a function that differs only when `len(df) == 250` is still caught;
  - a function that raises propagates its exception with an added note naming the prefix length and `t`.
- [ ] **AC6 (contract and purity checks):** `LookaheadError` with kind:
  - `invalid_output` for a return value that is not a `Series`, `DataFrame` or `Mapping[str, Series]` (the message points to `assert_no_lookahead_point_in_time`); for output labels that are not labels of the input frame (for example a `RangeIndex` or a tz-naive index); and for duplicated or non-increasing output labels;
  - `input_mutated` for a function that modifies the frame it receives;
  - `nondeterministic` for a function with hidden state (for example a call counter);
  - `vacuous` when every compared value is missing (for example `rolling(300).mean()` on 250 candles).
- [ ] **AC7 (argument validation):** `TypeError` if `candles` is not a `DataFrame`; `ValueError` for fewer than 2 rows, a non-unique or non-increasing index, any `k < 1`, explicit `cuts` outside `[1, length - 1]`, `max_cuts < 1`, `min_prefix` outside `[1, length - 1]`, or a negative `rtol`/`atol`.

### Harness: point-in-time functions

- [ ] **AC8 (point-in-time support):** `assert_no_lookahead_point_in_time(func, reference, candles, min_prefix=2)` on `synthetic_candles(250, seed=7)` (`min_prefix=3` for the function that reads `close.iloc[-3]`):
  - passes for `func = close.iloc[-1] > close.iloc[-2]` with `reference = close > close.shift(1)`;
  - fails with `point_in_time_mismatch` for a `func` whose decision really belongs to the previous candle and is "confirmed by the next candle": `close.iloc[-2] > close.iloc[-3] and close.iloc[-1] > close.iloc[-2]`, which disagrees with the honest reference at `t`;
  - fails with the series kind (`value_mismatch`) and `violation.func_name` naming the **reference** when the reference peeks (`close.shift(-1) > close`);
  - fails with `missing_reference` when the reference only returns its last 5 candles;
  - supports a `DataFrame` reference paired with a `func` returning `Mapping[str, object]`, and a `Series` reference of dataclass objects whose fields contain NaN.
- [ ] **AC9 (`values_equal`):** NaN/None/NaT/`pd.NA` are equal to themselves, not to non-missing values. `-0.0 == 0.0`. `1.0` differs from `numpy.nextafter(1.0, 2.0)` unless `rtol`/`atol` allows it. Numpy and Python scalars of the same family compare by value, and a `bool` never equals a non-bool number. `Mapping`, `list`/`tuple`, dataclass instances and `Series` compare recursively with the same rules.

### Synthetic generator (`tests/fixtures/candles.py`)

- [ ] **AC10 (validity):** for every combination of `Scenario`, timeframe in `("1h", "4h", "1d")` and `n` in `(1, 2, 60, 500)`, and for `volatility=0.0`, the frame has:
  - exactly `n` rows;
  - a tz-aware UTC `DatetimeIndex`, strictly increasing and unique, where each label is a candle **open** time aligned to the timeframe grid (`ts == ts.floor(freq)`);
  - columns exactly `["open", "high", "low", "close", "volume"]`, all `float64`;
  - no NaN or infinite value, every price in `[1e-6, 1e9]` and `volume >= 0`;
  - `low <= min(open, close)` and `high >= max(open, close)` on every row.
- [ ] **AC11 (scenario features, `n >= 60`, default volatility):**
  - `RANDOM_WALK`: every index step equals the timeframe duration, and `open[i] == close[i-1]`;
  - `GAPS`: no timestamp falls on a Saturday or Sunday (UTC); at least one gap comes from a dropped weekday slot (a step longer than the timeframe duration between two consecutive labels with no Saturday or Sunday between them); and at least one candle right after a gap has `open != previous close`;
  - `FLAT_RUNS`: at least one run of ≥ 5 consecutive candles with `open == high == low == close == previous close` and `volume == 0`;
  - `EXTREME`: at least one candle with `close <= 0.1 * open`, one with `close >= 10 * open`, one with `volume >= 1e12` and one with `volume == 0`;
  - `MIXED`: the `GAPS`, `FLAT_RUNS` and `EXTREME` features at once.
- [ ] **AC12 (determinism):**
  - the same arguments produce identical frames (`pd.testing.assert_frame_equal(..., check_exact=True)`);
  - different seeds produce different `close` values;
  - the output does not depend on the global RNG state (`random.seed`, `numpy.random.seed`) and generating does not change that state;
  - no test asserts golden values from the generator (numpy does not guarantee `Generator` streams across versions).
- [ ] **AC13 (argument validation):** `ValueError` for `n < 1`, an unknown timeframe or scenario, `volatility < 0`, `start_price <= 0` or outside the price bounds, and a `start` that is not tz-aware UTC or not on the timeframe grid.

### Hypothesis integration

- [ ] **AC14 (strategy):** `candle_frames(...)` builds a `SearchStrategy[pd.DataFrame]`:
  - every drawn frame satisfies AC10 and `min_size <= len(frame) <= max_size`;
  - scenarios and timeframes are restricted to the arguments;
  - invalid arguments (`min_size < 1`, `max_size < min_size`, empty `scenarios`/`timeframes`, `max_volatility < 0`) raise `ValueError` when the strategy is built, not when drawn;
  - `@given` tests prove it composes with the harness: `rolling(20).mean()` passes on `candle_frames(min_size=40)` with `max_cuts=10`, and `close.shift(-1)` is always caught.
- [ ] **AC15 (profile):** `tests/conftest.py` registers and loads a profile named `trading-bot`. It inherits the active default (so Hypothesis' built-in CI profile still applies on CI: derandomized, no example database) and sets `max_examples=50` and `deadline=None`. A test asserts `settings.default.max_examples == 50` and `settings.default.deadline is None`.

### Documentation, dependencies, gate

- [ ] **AC16 (docs):** `docs/ARCHITECTURE.md` has a `## Look-ahead testing` section placed right after `## Rule model` with the content of Design §6. Its Python blocks pass `ruff format --check`.
- [ ] **AC17 (dependencies):**
  - the `dev` dependency group gains `hypothesis`, `pandas`, `numpy` and `pandas-stubs` (added with `uv add --dev`) and `uv.lock` is updated;
  - `[project.dependencies]` does not change, and `uv export --no-dev --no-hashes --locked --no-emit-project` gives the same output as on `origin/main`;
  - no file mentions `pandas-ta` or `pandas_ta` as a dependency or import.
- [ ] **AC18 (typing):**
  - `[tool.mypy] files` adds `tests/lookahead.py` and `tests/fixtures`, and `mypy` (strict) passes;
  - there is no bare `# type: ignore`: any ignore names its error code;
  - public signatures of the three modules do not use `Any`, except as the type argument pandas-stubs requires (`pd.Series[Any]`); `object` is used for arbitrary values.
- [ ] **AC19 (gate and budget):**
  - `uv run python scripts/check.py` is green;
  - the new tests need no network and add at most **10 s** to `pytest` on the developer machine, with no single new test above **3 s** (evidence: `pytest --durations=20`);
  - the coverage threshold is unaffected (`src/` does not change).
- [ ] **AC20 (scope):** only the files listed in Design §1 change, plus this spec.

## Design

### 1. Files and owners

No `__init__.py` files are added: `tests/` stays a namespace package, as today (`tests.script_loader`). Modules are **always imported with the `tests.` prefix** (`from tests.lookahead import ...`), never as `lookahead` or `fixtures`. `tests/conftest.py` puts `tests/` on `sys.path`, and a second import path would create a second `LookaheadError` class that `pytest.raises` does not match.

| File | Owner | Change |
|------|-------|--------|
| `tests/lookahead.py` | developer | Harness (§3) |
| `tests/fixtures/candles.py` | developer | Seeded generator (§4) |
| `tests/fixtures/strategies.py` | developer | Hypothesis strategy (§5) |
| `tests/conftest.py` | developer | Hypothesis profile (§5) |
| `tests/unit/test_lookahead_harness.py` | developer | TDD self-tests of the harness (AC1–AC9) |
| `tests/unit/test_synthetic_candles.py` | developer | TDD tests of the generator (AC10–AC13) |
| `tests/unit/test_candle_strategies.py` | developer | TDD tests of the strategy and profile (AC14–AC15) |
| `tests/unit/test_lookahead_harness_adversarial.py` | tester | Additional cheats and edge cases (Test plan T16–T18) |
| `docs/ARCHITECTURE.md` | developer | `## Look-ahead testing` section (§6); explicitly authorized by this spec |
| `pyproject.toml`, `uv.lock` | developer | Dev dependencies and mypy files (§2) |
| `docs/specs/003-lookahead-harness.md` | tech-lead | This spec |

There are no new Protocols in `src/`, no migrations and no `TB_*` variables.

### 2. Dependencies and typing

- `uv add --dev hypothesis pandas numpy pandas-stubs`. At spec time this resolves to hypothesis 6.168.0, pandas 3.0.5, numpy 2.5.3 and pandas-stubs 3.0.5 (build 260914); `uv add` writes those lower bounds.
- **Dev, not runtime, in #3.** `CLAUDE.md` says dependencies arrive with the feature that needs them. Only tests need them here, so the #3 image and its runtime dependency set stay identical (AC17). **#4 moves `pandas` and `numpy` to `[project.dependencies]`** (`uv add pandas numpy` plus `uv remove --dev pandas numpy`) when `domain/` imports them; the lock keeps the same versions, so the move is a no-op for tests. `hypothesis` and `pandas-stubs` stay dev-only.
- `numpy` is declared explicitly because the generator and harness import it directly (not only through pandas).
- **pandas-stubs now.** The harness is the foundation of rule 4, so it is type-checked: `[tool.mypy] files = ["src", "scripts", "deploy", ".claude/hooks", "tests/lookahead.py", "tests/fixtures"]`. pandas ships no `py.typed`, so strict mypy needs the stubs; #4 needs them for `domain/` anyway. The tech-lead verified this layout (namespace packages, `explicit_package_bases`) with mypy 2.3.1, pandas-stubs and hypothesis in strict mode.
- `pd.Series` is generic only in the stubs; `pd.Series[Any]` raises `TypeError` at runtime on pandas 3. Use `from __future__ import annotations` and PEP 695 `type` aliases (evaluated lazily) for any alias that subscripts pandas types.

### 3. Harness — `tests/lookahead.py`

#### 3.1 Public API

```python
from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import Literal

import pandas as pd

DEFAULT_KS: tuple[int, ...] = (1, 2, 5)
DEFAULT_MAX_CUTS = 25
EDGE_CUTS = 5

type ViolationKind = Literal[
    "value_mismatch",
    "dtype_mismatch",
    "result_appeared",
    "result_disappeared",
    "outputs_changed",
    "point_in_time_mismatch",
    "missing_reference",
    "invalid_output",
    "input_mutated",
    "nondeterministic",
    "vacuous",
]


@dataclass(frozen=True, slots=True)
class Violation:
    kind: ViolationKind
    func_name: str  # __qualname__ of the offending callable (func or reference)
    output: str | None  # output name ("value" for an unnamed Series)
    prefix_length: int | None  # n
    t: Hashable | None  # candles.index[n - 1]
    k: int | None  # candles appended after t (0 = same frame, point-in-time only)
    stamp: Hashable | None  # first offending candle label
    prefix_value: object  # computed with data[:t]
    extended_value: object  # computed with data[:t+k]
    offending_count: int  # offending candles at or before t for this (n, k, output)
    detail: str  # kind-specific explanation (for example both dtypes)


class LookaheadError(AssertionError):
    violation: Violation

    def __init__(self, violation: Violation) -> None: ...


@dataclass(frozen=True, slots=True)
class LookaheadReport:
    cuts: tuple[int, ...]  # prefix lengths checked
    ks: tuple[int, ...]  # normalized requested ks (sorted, unique)
    calls: int  # invocations of the functions under test
    comparisons: int  # (n, k, output) comparisons made
    compared_values: int  # cells compared
    non_missing_values: int  # compared cells whose prefix value is not missing


def select_cuts(
    length: int, *, min_prefix: int = 1, max_cuts: int = DEFAULT_MAX_CUTS
) -> tuple[int, ...]: ...


def assert_no_lookahead(
    func: Callable[[pd.DataFrame], object],
    candles: pd.DataFrame,
    *,
    ks: Sequence[int] = DEFAULT_KS,
    cuts: Sequence[int] | None = None,
    min_prefix: int = 1,
    max_cuts: int = DEFAULT_MAX_CUTS,
    rtol: float = 0.0,
    atol: float = 0.0,
) -> LookaheadReport: ...


def assert_no_lookahead_point_in_time(
    func: Callable[[pd.DataFrame], object],
    reference: Callable[[pd.DataFrame], object],
    candles: pd.DataFrame,
    *,
    ks: Sequence[int] = DEFAULT_KS,
    cuts: Sequence[int] | None = None,
    min_prefix: int = 1,
    max_cuts: int = DEFAULT_MAX_CUTS,
    rtol: float = 0.0,
    atol: float = 0.0,
) -> LookaheadReport: ...


def values_equal(left: object, right: object, *, rtol: float = 0.0, atol: float = 0.0) -> bool: ...
```

Callables are typed `Callable[[pd.DataFrame], object]` on purpose: invalid return values must reach the runtime contract check (`invalid_output`) instead of being unrepresentable. Violations of the function under test raise `LookaheadError`; misuse of the harness raises `TypeError`/`ValueError` (AC7). The harness never relies on `assert` statements (they vanish under `python -O` and pytest does not rewrite them outside test modules): it raises explicitly. It is itself pure: no globals, clock or randomness.

#### 3.2 Series-valued output contract

`func(frame)` must return one of:

- `pd.Series` → one output named `str(series.name)`, or `"value"` when the name is `None`;
- `pd.DataFrame` → one output per column (string labels, unique);
- `Mapping[str, pd.Series]` → one output per key (this is the #5 `registry.compute` shape).

Each output is a series of results **stamped by the labels of the input frame**. Its labels must be unique, increasing and a subset of `frame.index`; otherwise the result is `invalid_output`. The labels do not have to cover the whole input: warmup rows may be dropped and a windowed function may return only its last rows.

#### 3.3 Cut and `k` selection

- `select_cuts(length, min_prefix, max_cuts)`:
  - the candidates are `range(min_prefix, length)`: every prefix with at least one later candle;
  - if there are at most `max_cuts` candidates, all of them are used (full sweep);
  - otherwise `edge = min(EDGE_CUTS, max_cuts // 3)`, and the cuts are the sorted union of the first `edge` candidates, the last `edge` candidates and `numpy.rint(numpy.linspace(min_prefix, length - 1, max_cuts - 2 * edge))`.

  Head cuts catch warmup-boundary cheats such as `bfill`, tail cuts catch end-of-frame behavior, and the middle samples the rest. The selection is deterministic: no randomness in the harness.
- Explicit `cuts` replace the selection (sorted, de-duplicated, validated); `min_prefix` and `max_cuts` are then not applied.
- For each cut `n`, the `k` values are `sorted({k for k in ks if n + k <= length} | {length - n})`. **The full-frame comparison is always included**: it catches any forward peek regardless of `ks`, while the small `ks` catch behavior that depends on the frame length.
- Outputs are cached per prefix length within one call, so each distinct length is evaluated once. Bound: `calls <= len(cuts) * (len(ks) + 1) + 2` (the `+ 2` covers the full frame and the determinism re-run).
- **Why sampled by default.** Tech-lead measurements on the developer machine (pandas 3.0.5, numpy 2.5.3, TA-Lib 0.8.0, 600 candles): a full sweep takes 1.5–4.5 s per TA-Lib indicator, while the default (≤ 25 cuts, 300 candles) takes 40–100 ms. Callers may request a full sweep with `max_cuts=len(candles)` or target lengths with `cuts=[...]`.

#### 3.4 Comparison semantics

For each cut `n` (with `t = candles.index[n - 1]`), each `k` and each output: `P` is the output from `data[:t]` and `L` is the same output from `data[:t+k]`, restricted to labels `<= t` (`L'`). Checks, in this order:

1. `outputs_changed`: the ordered output names of `P` and `L` differ.
2. `dtype_mismatch`: the output dtypes differ (`P.dtype != L.dtype`).
3. `result_appeared`: a label of `L'` is missing from `P`. A result for a past candle must not appear only after later candles arrive.
4. `result_disappeared`: a label of `P` is missing from `L'` and is not older than `L`'s first label (or `L` is empty). Dropping results from the **start** is allowed, so windowed outputs pass.
5. `value_mismatch`: on the labels common to `P` and `L'`, values differ under `values_equal` semantics.

**Exact equality by default** (`rtol = atol = 0.0`):

- Rule 4 says "identical"; any tolerance weakens it, and a look-ahead of a global statistic can move values by less than a typical tolerance.
- Both sides are computed in the same process, on the same machine, with the same operations for rows `<= t`. Sequential computations (rolling windows, EWM, cumulative operations, TA-Lib recurrences) are therefore bitwise identical.
- Evidence gathered by the tech-lead: full sweeps over 600–1000 candles, on all scenarios, were bitwise identical for pandas `rolling`/`ewm`/`cumsum`/`cummax`, `numpy.exp`/`numpy.log` paths and all ten whitelisted TA-Lib indicators (`SMA`, `EMA`, `RSI`, `MACD`, `BBANDS`, `ATR`, `ADX`, `STOCH`, `OBV`, volume `SMA`).
- `rtol`/`atol` are an explicit opt-in for computations that genuinely cannot be bitwise stable. The call site must carry a comment justifying the tolerance. Float comparison with tolerance is `abs(prefix - extended) <= atol + rtol * abs(prefix)`.

`values_equal` (also used for object cells and point-in-time results):

- both missing (`None`, NaN, `NaT`, `pd.NA`) → equal; exactly one missing → different;
- floats: `==` semantics (so `-0.0 == 0.0` and `inf == inf`), or the tolerance formula above;
- numpy scalars compare like the Python scalars of the same family; `bool`/`numpy.bool_` only equal other booleans;
- `Mapping` (same keys), `list`/`tuple` (same length), dataclass instances (same type, fields) and `pd.Series` (equal index, dtype and cells) compare recursively;
- anything else uses `==` and must produce a `bool`; otherwise `TypeError`.

#### 3.5 Check order and failure report

1. Validate the arguments (AC7).
2. Evaluate the full frame twice. Validate the output contract (`invalid_output`), then compare both runs over all labels (`nondeterministic`).
3. Iterate cuts ascending, then `k` ascending, then outputs in their returned order. Raise on the **first** violation, reporting its **earliest** offending label and the count of offending labels for that `(n, k, output)`.
4. After all comparisons, raise `vacuous` if `non_missing_values == 0`.

On every call, the function receives a deep copy of the prefix, never a view. Afterwards the copy is compared with the harness's own pristine prefix, and any difference is `input_mutated`. An exception from the function propagates with a note added through `BaseException.add_note`: `while evaluating <qualname> on data[:t] with n=<n>, t=<t>`.

Message format (developer may adjust the wording, but every element is required by AC2):

```text
Look-ahead detected in cheat_shift_forward: value_mismatch
  output: close
  cut: t=2024-01-01T00:00:00+00:00 (prefix length n=1)
  compared with: data[:t+k] with k=1 (prefix length 2)
  first offending candle: 2024-01-01T00:00:00+00:00
  with data[:t]:   nan
  with data[:t+k]: 100.60175469456388
  offending candles at or before t: 1
  hint: a value at or before t changed when later candles were appended; look for shift(-n), centered windows, bfill/interpolate or statistics over the whole frame
```

- Timestamps are rendered with `isoformat()`; other labels with `str()`.
- Values are rendered with `repr()` after converting numpy scalars with `.item()`.
- When `n + k == length`, the "compared with" line adds `(full frame)`.
- Every kind has its own `hint:`. `invalid_output` points to `assert_no_lookahead_point_in_time`. `missing_reference` says the reference must return a result for every candle (pass the full length as the window). `vacuous` says to use a frame longer than the warmup.

#### 3.6 Point-in-time functions

A point-in-time function (`func(frame) -> result about frame.index[-1]`, like #7 `evaluate(rule, candles)`) **cannot be checked for look-ahead on its own**. Each prefix yields exactly one result, about its own last candle, and a longer prefix yields a result about a different candle. There is never a second computation of the same `t` to compare, so a truncation-only check would always pass vacuously.

The harness therefore pairs it with a **reference**: a series-valued function giving the result for every candle of a frame (in #7, the "evaluate over the last N candles" option with `N = len(frame)`). `assert_no_lookahead_point_in_time`:

1. runs `assert_no_lookahead(reference, candles, ...)` with the same options, so a peeking reference is reported with `func_name` naming the reference;
2. checks that `func` is deterministic on the full frame and does not mutate its input;
3. for each cut `n`, evaluates `actual = func(data[:t])`, and for each `k` in `sorted({0} | {k for k in ks if n + k <= length} | {length - n})` takes the reference output from `data[:t+k]` (cached) at label `t`:
   - `Series` reference → the cell at `t`;
   - `DataFrame` reference → `row.to_dict()` at `t`, so `func` returns a `Mapping[str, object]`;
   - `t` absent → `missing_reference`;
   - `not values_equal(actual, expected)` → `point_in_time_mismatch`, where `prefix_value` is `func`'s result and `extended_value` the reference's. `k = 0` checks that both APIs agree on the same frame;
4. applies the same `vacuous` guard to the `actual` values.

Bound: `calls <= len(cuts) * (len(ks) + 2) + 4`.

Note for #7: results containing NaN inside plain containers compare correctly only through `values_equal` (Python's `==` treats two distinct NaN objects as different), which is why dataclasses and mappings are compared recursively.

### 4. Synthetic generator — `tests/fixtures/candles.py`

```python
from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pandas as pd

type TimeframeCode = Literal["1h", "4h", "1d"]

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
TIMEFRAMES: tuple[TimeframeCode, ...] = ("1h", "4h", "1d")
DEFAULT_START = "2024-01-01T00:00:00+00:00"  # a Monday, on every grid
MIN_PRICE = 1e-6
MAX_PRICE = 1e9
FEATURE_MIN_CANDLES = 60


class Scenario(StrEnum):
    RANDOM_WALK = "random_walk"
    GAPS = "gaps"
    FLAT_RUNS = "flat_runs"
    EXTREME = "extreme"
    MIXED = "mixed"


ALL_SCENARIOS: tuple[Scenario, ...] = tuple(Scenario)


def synthetic_candles(
    n: int = 250,
    *,
    seed: int = 0,
    scenario: Scenario | str = Scenario.RANDOM_WALK,
    timeframe: TimeframeCode = "1d",
    start: str | pd.Timestamp = DEFAULT_START,
    start_price: float = 100.0,
    volatility: float = 0.02,
) -> pd.DataFrame: ...
```

- **Randomness:** only `numpy.random.default_rng(seed)`, a local generator. No global RNG, clock or I/O.
- **Index:** tz-aware UTC `DatetimeIndex` of candle **open** times on the timeframe grid, starting at `start`. The unit (`us`/`ns`) is not constrained in #3. Labels are open times because yfinance labels bars by period start, and #4 computes `candle_close_ts = open + duration`.
- **Columns:** `open, high, low, close, volume`, all `float64`. TA-Lib requires `float64` input, and a float volume keeps NaN-capable arithmetic uniform.
- **Guidance algorithm (the invariants of AC10/AC11 are binding; the algorithm is not):**
  - log returns `~ N(0, volatility)`; `close` is the exponential of the clipped cumulative log price;
  - `open[i] = close[i-1]` (`start_price` for the first row), and at a gap `open[i] = close[i-1] * exp(N(0, 3 * volatility))`;
  - wicks: `high = max(open, close) * exp(|N(0, volatility / 2)|)`, `low = min(open, close) * exp(-|N(0, volatility / 2)|)`, clipped to the price bounds while keeping the OHLC inequalities;
  - `volume = round(lognormal(log(1e6), 0.5))`.
- **Scenarios:**
  - `RANDOM_WALK` uses a contiguous grid;
  - `GAPS` removes Saturday and Sunday slots and drops extra weekday slots (at least one when `n >= 60`), with a price jump at each gap;
  - `FLAT_RUNS` injects at least one run of 5–20 flat, zero-volume candles (a source of zero ranges and zero division for RSI/stochastics in #5);
  - `EXTREME` injects at least one −90% candle, one +900% candle, one `1e12` volume spike and zero-volume candles, within the price bounds;
  - `MIXED` combines the three.

  The injected events do not depend on `volatility`, and `volatility=0.0` yields a constant price path (except injected events).
- **Hand-off to #4:** #4 adds a test that `validate_candles` accepts every scenario and every `candle_frames()` draw, and it may widen `timeframe` to accept its `Timeframe` enum (the enum values equal these codes).

### 5. Hypothesis — `tests/fixtures/strategies.py` and `tests/conftest.py`

```python
from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
from hypothesis import strategies as st

from tests.fixtures.candles import ALL_SCENARIOS, TIMEFRAMES, Scenario, TimeframeCode


def candle_frames(
    *,
    min_size: int = 2,
    max_size: int = 120,
    scenarios: Sequence[Scenario] = ALL_SCENARIOS,
    timeframes: Sequence[TimeframeCode] = TIMEFRAMES,
    max_volatility: float = 0.25,
) -> st.SearchStrategy[pd.DataFrame]: ...
```

- The strategy draws **parameters** and delegates to `synthetic_candles`, which keeps validity in one code path: size, scenario, timeframe, `seed` in `[0, 2**32 - 1]`, a start offset of 0–5000 grid steps from `DEFAULT_START`, `start_price = 10 ** floats(-3, 5)` and `volatility` in `[0, max_volatility]`.
- Drawing every OHLCV value individually was rejected. 120 candles with 5 floats each exceeds Hypothesis' default buffer and trips health checks. Parameter draws still shrink well: toward `min_size`, the first scenario (`RANDOM_WALK`), seed 0 and `volatility=0.0` (a flat series).
- Arguments are validated eagerly: a plain function validates, then returns an `@st.composite` strategy.
- `tests/conftest.py`:

  ```python
  from hypothesis import settings

  settings.register_profile("trading-bot", parent=settings.default, max_examples=50, deadline=None)
  settings.load_profile("trading-bot")
  ```

  - `deadline=None` avoids flaky `DeadlineExceeded` on the first pandas call.
  - `parent=settings.default` keeps Hypothesis' automatic CI profile. The tech-lead verified this with hypothesis 6.168.0: with `CI=true` it gives `derandomize=True` and no database; locally it stays random with the example database.
  - `.hypothesis/` is already in `.gitignore`.
- Property tests must only assert detection of cheats whose detection does not depend on the data. `shift(-1)` always leaves a NaN at `t`, but `close / close.max()` is undetectable on a flat series. Honest functions under `@given` need `min_size` above their warmup to avoid `vacuous`.

### 6. `docs/ARCHITECTURE.md` — `## Look-ahead testing`

The developer writes a section right after `## Rule model` that covers:

1. **When it is mandatory:** every new indicator, every rule operator or evaluator change, and any function in `domain/` that returns values per candle (rule 4).
2. **The two entry points and when to use each:** `assert_no_lookahead` for series-valued functions; `assert_no_lookahead_point_in_time` for last-candle functions, with a one-paragraph explanation of why a reference is required (§3.6).
3. **Three short examples** with `tests.`-prefixed imports: a parametrized test over `Scenario`, a `@given(candles=candle_frames(...))` property, and a point-in-time test with a reference. Examples use small illustrative pandas functions rather than APIs that do not exist yet.
4. **The defaults and their meaning:** `ks=(1, 2, 5)` plus the full frame, at most 25 cuts including head and tail, exact comparison. Tolerance is only for computations that cannot be bitwise stable, and needs a justifying comment; TA-Lib indicators must not need it.
5. **How to read a failure:** the fields `n`, `t`, `k`, first offending candle, both values; and a table of the violation kinds with their typical cause.
6. **Fixtures:** the scenarios and what each stresses, `candle_frames`, frame length above the warmup, no golden values from the generator, and a note that recorded real-data fixtures arrive with #10 (Yahoo terms, public repository).

Suggested example block (it is ruff-format clean):

```python
import pandas as pd
import pytest
from hypothesis import given

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.strategies import candle_frames
from tests.lookahead import assert_no_lookahead, assert_no_lookahead_point_in_time


def sma_20(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean()


@pytest.mark.parametrize("scenario", list(Scenario))
def test_sma_20_has_no_lookahead(scenario: Scenario) -> None:
    assert_no_lookahead(sma_20, synthetic_candles(250, seed=7, scenario=scenario))


@given(candles=candle_frames(min_size=40))
def test_sma_20_has_no_lookahead_on_random_frames(candles: pd.DataFrame) -> None:
    assert_no_lookahead(sma_20, candles, max_cuts=10)


def closes_higher(candles: pd.DataFrame) -> bool:
    close = candles["close"]
    return bool(close.iloc[-1] > close.iloc[-2])


def closes_higher_by_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close > close.shift(1)


def test_closes_higher_has_no_lookahead() -> None:
    candles = synthetic_candles(250, seed=3, scenario=Scenario.MIXED)
    assert_no_lookahead_point_in_time(closes_higher, closes_higher_by_candle, candles, min_prefix=2)
```

### 7. Forward compatibility (#4–#7)

| Issue | How it uses #3 |
|-------|----------------|
| #4 | Moves `pandas`/`numpy` to runtime. Validates every scenario and `candle_frames()` draw with `validate_candles`. May accept `Timeframe` in the generator. Index labels stay open times. |
| #5 | `assert_no_lookahead(lambda df: registry.compute(name, params, df), candles)` directly (the `dict[str, Series]` shape). One test per indicator and scenario plus a property. Frames longer than `warmup`. Exact comparison. |
| #6 | No time series; not expected to use the harness beyond fixtures. |
| #7 | `assert_no_lookahead_point_in_time(lambda df: evaluate(rule, df), <evaluations over the whole frame as a Series indexed by candle label>, candles)`. The reference is indexed by input labels (open times), not by `candle_close_ts`. |

## Test plan

All tests are unit tests, without network. The mandatory template cases: **anti look-ahead** is the subject of this feature (self-tests T1–T9 and T16–T17); **idempotency**, **authorization** and **secret redaction** are N/A (no signals, Telegram, API, config or logging changes). No secrets or tokens are involved.

| Case | Type (unit/integration) | What it verifies | AC | Owner |
|------|-------------------------|------------------|----|-------|
| T1 cheats caught | unit | Parametrized over the seven AC1 cheats: `LookaheadError` with the expected `kind` | AC1 | developer |
| T2 failure report | unit | `cheat_shift_forward`: violation fields and every message element | AC2 | developer |
| T3 honest functions | unit | Honest functions parametrized over every `Scenario`: no error; report non-vacuous | AC3 | developer |
| T4 comparison semantics | unit | One-ulp change fails exactly and passes with `rtol=1e-12`; `dtype_mismatch`; `outputs_changed`; `result_disappeared` | AC4 | developer |
| T5 cut selection and cost | unit | `select_cuts` properties (including a small `@given` over `length`, `min_prefix` and `max_cuts`); explicit cuts; `calls` bound; skipped `k`; full-frame-only catch; exception note | AC5 | developer |
| T6 contract and purity | unit | `invalid_output` (non-pandas, `RangeIndex`, tz-naive, duplicated, unsorted labels); `input_mutated`; `nondeterministic`; `vacuous` | AC6 | developer |
| T7 argument validation | unit | Each `TypeError`/`ValueError` of AC7 | AC7 | developer |
| T8 point-in-time | unit | Honest pair passes; next-candle confirmation fails; peeking reference reported by name; `missing_reference`; `DataFrame` reference; dataclass results with NaN | AC8 | developer |
| T9 `values_equal` | unit | Table-driven cases of AC9 | AC9 | developer |
| T10 generator validity | unit | Matrix of scenario, timeframe and size and `volatility=0.0`: invariants | AC10 | developer |
| T11 scenario features | unit | Feature assertions per scenario (`n=250`, several seeds) | AC11 | developer |
| T12 determinism | unit | Same args equal; seeds differ; global RNG seeded differently gives the same frame and its state is unchanged | AC12 | developer |
| T13 generator arguments | unit | Each `ValueError` of AC13 | AC13 | developer |
| T14 strategy | unit (`@given`) | Drawn frames valid and within bounds; restricted options; eager `ValueError`; honest rolling mean passes and `shift(-1)` is caught on drawn frames | AC14 | developer |
| T15 profile | unit | `settings.default.max_examples == 50` and `deadline is None` | AC15 | developer |
| T16 adversarial cheats | unit | `interpolate()` on `close.where(volume > 0)` (`FLAT_RUNS`); reversed `cummax` (`iloc[::-1].cummax().iloc[::-1]`); `rank(pct=True)`; global z-score; `ewm` on the reversed series; a cheat visible only on `GAPS` (for example `bfill` after reindexing to a contiguous grid); a length-dependent cheat caught only by the full-frame comparison | AC1, AC5 | tester |
| T17 adversarial honest cases | unit | Time-based `rolling("3D").mean()` on `GAPS`; `expanding().max()`; boolean and `int64` outputs; object outputs with NaN inside dicts; a frame of exactly 2 rows; a non-datetime increasing index; `ks=()` (full frame only); `max_cuts=1` | AC3, AC5, AC9 | tester |
| T18 point-in-time edge cases | unit | Reference with dropped warmup rows (→ `missing_reference` early, pass with `min_prefix` past warmup); `func` mutating its input; nondeterministic `func` | AC6, AC8 | tester |

Verification evidence (tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC19 |
| V2 budget | `uv run pytest --durations=20 -q`: new tests total ≤ 10 s, none above 3 s | AC19 |
| V3 runtime dependencies unchanged | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch equals the same command run in a temporary `git worktree` of `origin/main` (created in the scratch directory and removed afterwards) | AC17 |
| V4 no `pandas-ta` | `git grep -n -i -E "pandas[-_]ta\b"` matches only prohibition notes in Markdown files (`CLAUDE.md`, docs, agent definitions, this spec), never `pyproject.toml`, `uv.lock` or any Python file | AC17 |
| V5 typing scope | `git diff origin/main -- pyproject.toml` shows the two added mypy paths; `uv run mypy` passes; `git grep -n "type: ignore" tests/lookahead.py tests/fixtures` shows only coded ignores | AC18 |
| V6 docs | `## Look-ahead testing` follows `## Rule model`; `uv run ruff format --check docs/ARCHITECTURE.md` passes; manual read against §6 | AC16 |
| V7 scope | `git diff origin/main --name-only` plus `git status --porcelain` list only the §1 files | AC20 |
| V8 secrets and language | `python scripts/secret_scan.py --history`; a language review of the diff (English only) | — |
| V9 Docker | N/A locally (the daemon is not running), and the change cannot affect the image: `.dockerignore` excludes `tests` and `docs`, and `uv sync --locked --no-dev` installs no dev group (V3 proves the runtime set is unchanged). Reported as N/A with this justification, not as PASS; `Docker build (arm64)` on the PR is the authoritative check | — |

**TDD order suggested to the developer:** T9 → T7 → T5 (`select_cuts`) → T1/T2 → T3 → T4 → T6 → T8 → T10–T13 → T14–T15 → docs. The harness self-tests can use a tiny local frame builder until the generator exists, then switch to `synthetic_candles`.

## Risks and security

- **False confidence (vacuous passes).** A too-short frame, all-NaN outputs or a point-in-time function checked alone would pass silently. Mitigations: the `vacuous` guard, the mandatory reference for point-in-time functions (§3.6), `invalid_output` for non-series returns, and the documented rule "frame longer than warmup".
- **Sampling misses length-specific behavior.** Mitigations: the full-frame comparison at every cut, head and tail cuts, and explicit `cuts`/`max_cuts=len(...)` for targeted checks (documented).
- **Exact comparison flakiness.** Mitigated by the bitwise evidence in §3.4 (same process, sequential operations) and an explicit, commented opt-in tolerance. If #5 hits a real instability, its spec decides a per-indicator tolerance; the harness default stays exact.
- **Generator stream changes on numpy upgrades.** No golden values are asserted (AC12); tests assert invariants and features, so a Dependabot numpy bump cannot break them.
- **pandas / pandas-stubs skew.** Dependabot (`uv` ecosystem) may bump them separately; mypy in CI catches it, and they should be bumped together.
- **Duplicate module identity.** `tests/conftest.py` puts `tests/` on `sys.path`; mandatory `tests.`-prefixed imports (§1) keep a single `LookaheadError` class.
- **Future recorded fixtures (#10).** `.gitignore` ignores every directory named `data/`, so recorded fixtures must not live under a `data/` directory (for example use `tests/fixtures/ohlcv/`). Redistribution terms must be checked there (D2).
- **Sensitive data.** None involved: synthetic data only, no tokens, hosts, IPs or users. No `TB_*` variables, migrations or `secrets.env` changes.
- **Deploy impact.** None functional: dev-only dependencies, and tests/docs are excluded from the image. The `uv.lock` change only invalidates the Docker builder cache layer. Pushing the branch produces a patch beta.
- **Unbreakable rules.** Signal-only, pure `domain/` (untouched), closed candles (enforced by this harness from #5 on), idempotency, UTC (the generator is UTC-only), single worker, no `eval`, English only: all preserved.

## User decisions (2026-09-14)

- **D1:** one PR per issue with stacked branches (#3 → #4 → #5 → #6 → #7); this spec covers #3 only, designed for reuse by #4–#7 (§7).
- **D2:** fixtures are synthetic only in M1 (seeded generator with random walk, gaps, flat runs and extreme values, plus Hypothesis strategies). Recording real-data fixtures is deferred to #10 (YFinanceProvider) because the repository is public and Yahoo's terms restrict redistribution.

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria and T1, T2, T4, T6 and T8 fail without the implementation
- [x] Runtime dependency set unchanged (V3); no `pandas-ta`
- [x] Scope limited to Design §1
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)
