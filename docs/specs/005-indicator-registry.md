# 005 — Indicator registry (whitelist) on top of TA-Lib

- **Status:** approved
- **Branch:** `feature/indicator-registry` (stacked on `feature/domain-models`, #4, commit `5e1ab04`)
- **Spec author:** tech-lead
- **Issue:** #5 (milestone M1 · Analysis core; stacked series #3 → #4 → #5 → #6 → #7)
- **Expected commit type:** `feat:`. The change adds a public runtime API in `src/trading_bot/domain/indicators/` (the indicator catalog that #6, #7, #14 and #24 build on) and a runtime dependency (`ta-lib`) that changes the published image. `feat` → minor bump.

## Goal

Give rules a closed, validated catalog of ten technical indicators behind our own interface: `REGISTRY.compute(name, params, candles)` returns per-candle series with documented NaN, warmup and precision semantics, and `REGISTRY.describe()` returns JSON-ready metadata for the dashboard. TA-Lib computes the values but stays isolated in one module, so it can be replaced (`CLAUDE.md` rules 3, 4 and 8).

## Out of scope

- The rule schema, operand validation and JSON Schema export (#6); the evaluator and crossovers (#7). This spec only provides what they need (Design §12).
- Wiring the registry into `main.py`, the engine (#14), the data provider (#8/#10), persistence (#13), the API (#22) or the dashboard (#24).
- Indicators outside the ten of the issue, TA-Lib functions not listed here, indicators computed on other indicators, and a price-source parameter (`sma` and `ema` always use `close`).
- Caching of results. Recorded real-data fixtures (deferred to #10, user decision D2).
- Refactoring the private `_echo` helpers of `timeframe.py`, `signals.py` and `candles.py` into a shared module (a follow-up candidate; this feature adds its own copy).
- **Never `pandas-ta`**, and no typing stub package for TA-Lib (it ships its own, Design §2).
- Changes to `.github/`, `deploy/`, `scripts/`, `.claude/`, `CLAUDE.md`, `docs/ROADMAP.md`, `main.py`, `config.py` or `logging_setup.py`. No `TB_*` variables and no migrations.

## Acceptance criteria

### Catalog and parameters

- [ ] **AC1 (catalog):** `REGISTRY.names == ("sma", "ema", "rsi", "macd", "bbands", "atr", "adx", "stoch", "obv", "volume_sma")`. For every indicator, the inputs, parameters (name, type, default, minimum, maximum, label, description), constraints, outputs (names, order, labels), default output, label and description are exactly those of Design §6.1 and §6.3. In particular `obv` has the `signal` parameter, the outputs `value` and `signal`, and no default output (user decision D3).
- [ ] **AC2 (parameter validation):** `REGISTRY.validate_params(name, params)` returns an `IndicatorParams` in declaration order with every declared parameter:
  - missing parameters take their default (`validate_params("macd", {})` gives `fast=12, slow=26, signal=9`);
  - integer parameters accept `int` and numpy integers, stored as built-in `int`; float parameters accept `int`, `float` and numpy floating values, stored as built-in `float` (`{"std": 2}` gives `2.0` of type `float`);
  - rejected with `InvalidParameterError` and the stated `kind`: an undeclared name (`rsi` with `{"period": 14}`) → `unknown_parameter`; `True`/`False`, `None`, `"14"`, `Decimal("14")` for any parameter and `14.0` for an integer parameter → `wrong_type`; a value outside `[minimum, maximum]` (`rsi` `length=1` and `101`; `bbands` `std=0.0`, `std=5.5`, `std=nan`, `std=inf`) → `out_of_range`; `macd` with `fast=26, slow=12` or `fast=slow=20` → `constraint`;
  - checks run in this order and the first failure raises: undeclared names (in the mapping's iteration order), then declared parameters in declaration order (type, then range), then constraints;
  - validating an `IndicatorParams` again returns an equal value (idempotent);
  - a `params` value that is not a `Mapping`, or a non-`str` key, raises `TypeError`.
- [ ] **AC3 (indicator names and outputs):**
  - `UnknownIndicatorError` (kind `unknown_indicator`) for `"RSI"`, `" rsi"`, `"rsi "`, `""` and `"pandas_ta"`: names are exact and case-sensitive. A non-`str` name raises `TypeError`;
  - `REGISTRY.resolve_output("rsi", None) == "value"`, `resolve_output("macd", "hist") == "hist"`; `resolve_output("macd", None)` and `resolve_output("obv", None)` raise `InvalidOutputError` with kind `missing_output`, and `resolve_output("rsi", "signal")` with kind `unknown_output`.
- [ ] **AC4 (error contract):**
  - `UnknownIndicatorError`, `InvalidParameterError` and `InvalidOutputError` subclass `IndicatorError`, which subclasses `ValueError`; `IndicatorComputationError` subclasses `RuntimeError` and not `ValueError`;
  - each `IndicatorError` has `kind`, `indicator`, `parameter` and `output` attributes (Design §3). `pickle` round-trips keep every attribute;
  - messages name the indicator, parameter or output and, for `unknown_*` kinds, list the valid names. Echoed user input is `repr()` of at most 32 characters: a 10 000-character name or value containing newlines and ANSI escapes produces a single-line message shorter than 300 characters.

### Computation contract

- [ ] **AC5 (compute contract):** for every indicator and a valid candle frame, `REGISTRY.compute(name, params, candles)`:
  - returns a new `dict` whose keys are the output names in declared order;
  - each value is a new `pd.Series` with dtype `float64`, `name` equal to the output name, `len(candles)` rows and an index equal to `candles.index` with the same dtype (unit and time zone);
  - does not modify `candles`, and mutating a returned series changes neither `candles` nor a later result;
  - is deterministic: the same inputs give bitwise-identical outputs;
  - validates in this order: indicator name, parameters, then `validate_candles(candles)`, whose `CandleValidationError` propagates unchanged (an unknown indicator with an invalid frame raises `UnknownIndicatorError`);
  - for `len(candles) <= lookback` (including an empty frame) returns all-NaN outputs without calling the kernel;
  - never returns `±inf`: non-finite kernel values become NaN (Design §5).
- [ ] **AC6 (golden values):** the hand-computable cases of Design §6.4 give exactly the listed values (absolute tolerance `1e-12`; NaN positions identical).
- [ ] **AC7 (independent reference):** an independent plain-Python implementation of the definitions in Design §6.2, written in `tests/` without TA-Lib, matches `compute` for every indicator and output on the matrix of Test plan T13: NaN positions are identical and every other value satisfies `abs(actual - reference) <= 1e-9 * scale`, where `scale` is `100` for `rsi`, `stoch` and `adx`, `max(1.0, candles["volume"].sum())` for both `obv` outputs (it bounds the OBV level), `max(1.0, candles["volume"].max())` for `volume_sma`, and `candles["high"].max()` for the price-unit outputs of `sma`, `ema`, `macd`, `bbands` and `atr`. (Tech-lead measurement on 160 frames × 3 parameter sets: worst error `7e-12 * scale`, in `bbands`; `obv` with `signal` in `2, 20, 50, 200` on 132 frames: no difference, because the fixture volumes are integers.)
- [ ] **AC8 (undefined values):** values are NaN exactly where Design §7 says they are undefined, and TA-Lib's substituted zeros never reach the caller:
  - on `synthetic_candles(250, scenario=Scenario.RANDOM_WALK, volatility=0.0)` (every candle flat), every output of `rsi`, `adx` and `stoch` is NaN, while `sma`, `ema`, `bbands` (all three bands equal to the close), `macd` (all outputs `0.0`), `atr` (`0.0`), `obv` and `volume_sma` are finite from `lookback`;
  - on `synthetic_candles(250, seed=s, scenario=Scenario.FLAT_RUNS)` for seeds `0..4`, `stoch` with `length=5, smooth_k=1, smooth_d=1` has `k` and `d` NaN at a position `i >= lookback` exactly when `high[i-4..i]` and `low[i-4..i]` all equal one value (a window inside a flat run), and finite otherwise;
  - on `Scenario.RANDOM_WALK`, `GAPS` and `EXTREME` frames (default volatility), every output is NaN before `lookback` and finite from `lookback` on.

### Warmup

- [ ] **AC9 (lookback and warmup):** for every indicator:
  - `REGISTRY.lookback(name, params)`, `warmup(name, params)` and `stable_warmup(name, params)` follow the formulas of Design §6.1 and §8 (`warmup = lookback + 1`, `stable_warmup = warmup + settle`), validate `params` like `validate_params` and give the default values of Design §6.1;
  - with the default, minimum and a large parameter set (Test plan T9), the number of leading NaN rows equals `lookback` on non-flat frames; a frame of `warmup - 1` candles gives all-NaN outputs and a frame of `warmup` candles a finite last value.
- [ ] **AC10 (stable warmup):** on `synthetic_candles(3000, seed=s, scenario=...)` for seeds `0..4` and scenarios `RANDOM_WALK` and `GAPS`, at default parameters and at `length=50` (`macd` `fast=50, slow=100, signal=30`; `obv` `signal=50`), the last value of every output computed on the `stable_warmup` candles ending at candle `t` compares with the value at `t` computed on the whole frame, for several `t`:
  - `ema`, `rsi`, `macd`, `atr`, `adx`: within `1e-3 * close[t]` for price-unit outputs and within `0.1` for `rsi` and `adx`;
  - `sma`, `bbands`, `stoch`, `volume_sma`: within `1e-9 * scale` (AC7);
  - `obv`: the level is not compared, because it depends on the first candle (Design §8). Instead, `value - signal` is within `1e-9 * scale` (AC7), and on a window starting 500 candles after the frame start, `value` differs from the whole-frame `value` by one constant at every position from `lookback` on (spread within `1e-9 * scale`). Tech-lead measurement: worst `value - signal` difference `2.2e-18 * scale`, spread `0`.

### Look-ahead, TA-Lib isolation and metadata

- [ ] **AC11 (look-ahead):** `assert_no_lookahead` with exact comparison (`rtol=atol=0`, no tolerance anywhere) passes for every indicator × every `Scenario` at default parameters (`synthetic_candles(300, ...)`), for every indicator with a large parameter set in a full sweep (`max_cuts=len(candles)`) on `Scenario.MIXED`, and in a Hypothesis property over indicators, parameters and frames (Test plan T14).
- [ ] **AC12 (TA-Lib isolation and global settings):**
  - in `src/`, only `trading_bot/domain/indicators/talib_kernels.py` imports `talib`, and no file references `set_unstable_period`, `set_compatibility`, `talib.abstract` or `talib.stream`;
  - importing `trading_bot.domain.indicators.registry`, `.params`, `.spec` or `.errors` in a fresh interpreter leaves `talib` out of `sys.modules`; importing `.catalog` loads it (control);
  - after computing every indicator, `talib.get_unstable_period(f) == 0` for `EMA`, `RSI`, `ATR` and `ADX`, and `talib.get_compatibility() == 0`;
  - while the TA-Lib unstable period of `EMA`, `RSI`, `ATR` or `ADX` is non-zero, computing `ema` and `macd`, `rsi`, `atr` or `adx` respectively (on a frame longer than `lookback`) raises `IndicatorComputationError` naming the TA-Lib function; after restoring `0` it works again;
  - `talib.set_compatibility(1)` is a no-op: `talib.get_compatibility()` still returns `0` immediately after it (checked inside `try`/`finally`, restoring `0`). TA-Lib C 0.8.1 removed the MetaStock mode, and upstream `include/ta_func.h` documents both functions as deprecated no-ops. This canary fails if an upgrade makes the setting effective again, which requires re-evaluating Design §9. (Comparing outputs under the setting cannot be the canary: while the setter is a no-op, such a comparison can never fail.)
- [ ] **AC13 (describe):** `REGISTRY.describe()` returns a new `list` of JSON-ready `dict`s in `REGISTRY.names` order with the shape of Design §10:
  - `json.dumps(result, allow_nan=False)` succeeds, and two calls return equal values;
  - mutating a returned value does not change the next result;
  - the `rsi` and `macd` entries equal the literal JSON of Design §10.
- [ ] **AC14 (registry and spec construction):**
  - `IndicatorSpec` rejects with `ValueError` (or `TypeError` for wrong types): names not matching `^[a-z][a-z0-9_]{0,31}$` (indicator, parameter and output names), duplicated parameter or output names, no outputs, inputs outside `OHLCV_COLUMNS` or duplicated, a default of the wrong kind or outside `[minimum, maximum]`, `minimum > maximum`, a constraint naming undeclared parameters or violated by the defaults, and a negative `lookback` or `settle` at default parameters;
  - `IndicatorRegistry` rejects an empty catalog and duplicated indicator names with `ValueError`;
  - `IndicatorSpec` is frozen, and setting any attribute of an `IndicatorRegistry` or `IndicatorParams` raises `AttributeError`; `IndicatorParams` is read-only, hashable, picklable and equal to a `dict` with the same items.

### Typing, purity, dependencies, image, docs and gate

- [ ] **AC15 (typing):**
  - `uv run mypy` (strict) passes, and so does `uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated src/trading_bot/domain`;
  - `typing.Any` is neither imported nor used in `src/trading_bot/domain` (the AC14 `git grep` of spec 004 matches nothing);
  - the only `# type: ignore` under `src/trading_bot/domain` is the coded `[attr-defined]` of Design §9, with its comment, and `tests/fixtures/indicator_frames.py` and `tests/fixtures/indicator_reference.py` have none; every new `src` module except `__init__.py` defines `__all__`.
- [ ] **AC16 (purity):** the domain purity guard passes with `talib` allowed only in `talib_kernels.py`; no new module reads the clock or binds a module-level `dict`, `list` or `set` other than `__all__`; module-level objects are immutable (`Final` constants, tuples, frozen specs, the immutable `REGISTRY`).
- [ ] **AC17 (dependencies):**
  - `uv add ta-lib` writes `"ta-lib>=0.8.0"` to `[project.dependencies]` and updates `uv.lock`;
  - `uv export --no-dev --no-hashes --locked --no-emit-project` differs from the same command on `5e1ab04` only by `ta-lib==0.8.0` and its `# via` comment lines (tech-lead simulation on a scratch copy of `pyproject.toml` and `uv.lock`);
  - `uv.lock` pins the `cp312` `manylinux_2_28_aarch64` wheel of `ta-lib` 0.8.0; no typing stub package and no `[[tool.mypy.overrides]]` are added; no file adds `pandas-ta`/`pandas_ta`.
- [ ] **AC18 (image):** the `Dockerfile` runtime stage runs the build-time smoke check of Design §2, and the PR's `Docker build (arm64)` job is green. The lead verifies the beta deploy and `/health`.
- [ ] **AC19 (docs):** `docs/ARCHITECTURE.md` gains an `## Indicators` section right before `## Rule model` with the content of Design §11, the Rule model whitelist bullet links to it, and the TA-Lib decision bullet is updated. Python blocks pass `ruff format --check`.
- [ ] **AC20 (gate and budget):** `uv run python scripts/check.py` is green; every module under `src/trading_bot/domain/indicators/` has 100% line and branch coverage; the new tests need no network and add at most **15 s** to `pytest`, with no single test above **3 s** (evidence: `--durations`).
- [ ] **AC21 (scope):** only the files of Design §1 change (compared with `5e1ab04`), plus this spec.

## Design

### 0. Decisions

| Topic | Decision | Why |
|-------|----------|-----|
| Layout | `domain/indicators/` with `errors.py`, `params.py`, `spec.py`, `registry.py`, `talib_kernels.py`, `catalog.py`; `__init__.py` has only a docstring | `talib` is imported in exactly one module, so replacing TA-Lib means rewriting `talib_kernels.py` only. `registry.py` works with any catalog, so its tests use fake specs without TA-Lib. No re-exports, as in `domain/` (spec 004). |
| Registry shape | An immutable `IndicatorRegistry` class; the default instance is the module constant `REGISTRY` in `catalog.py` | Rule 3 forbids mutable globals, not immutable constants (`OHLCV_COLUMNS` is one). #6's pydantic validators need a registry without injection plumbing, while #7 and tests can pass another instance. A factory with `functools.cache` would add a module-level cache. |
| Parameter names | `length` for the main window everywhere (as in the ARCHITECTURE rule example); `macd` `fast`/`slow`/`signal`; `bbands` `std`; `stoch` `smooth_k`/`smooth_d` | One name for one concept keeps rule JSON predictable. The parameter `signal` of `macd` and its output `signal` live in different JSON keys (`params` and `output`), and labels disambiguate them in the dashboard. |
| Parameter types | Integers strictly integral (`14.0` rejected), floats accept integers; booleans always rejected; unknown names rejected; missing names take defaults | JSON from the dashboard serializes `14` as an integer. Rejecting instead of coercing keeps stored rules unambiguous, mirrors the strict `Timeframe.parse`, and a bool is an `int` in Python but never a length. |
| Constraints | Declarative `LessThan(left, right)` only (`macd` `fast < slow`) | TA-Lib silently **swaps** `fast` and `slow` when `slow < fast` (verified), and `fast == slow` gives a constant zero. A declarative constraint is exported by `describe()` for the dashboard. |
| Default output | `value` for single-output indicators; **none** for `macd`, `bbands`, `stoch` and `obv` (`resolve_output` requires one) | `{"indicator": "bbands"}` is ambiguous. The dashboard builder always picks an output, so requiring it costs users nothing and #6's optional `output` stays optional where it is unambiguous. `obv` joins the required group instead of keeping `value` as a default: one rule ("several outputs, no default") needs no special case in `IndicatorSpec.default_output`, and a bare `{"indicator": "obv"}` compared with a fixed value is exactly the use whose result depends on the history start (§8), so the user must choose `value` explicitly. |
| Output series | Full length, index equal to the input index, `float64`, named after the output, NaN before `lookback`, never back-filled | #7 aligns outputs by label and position. The harness contract (spec 003 §3.2) accepts this shape directly. |
| Undefined values | NaN where the formula divides by zero (`rsi`, `stoch`, `adx` on flat data), replacing TA-Lib's substituted `0` (user decision D4) | TA-Lib returns `0` for a flat window, which reads as "extremely oversold" and would fire `stoch k < 20` on a halted or illiquid ticker. NaN makes #7 not fire, which matches its "NaN does not fire" rule. Details in §7. |
| Warmup | Two numbers: `warmup = lookback + 1` (candles for the first value) and `stable_warmup = warmup + settle` (candles after which the seed of a recursive indicator weighs at most about 0.1%) | A single convergence-based warmup would keep `ema` `length=200` rules silent for the first 3.6 years of a ticker's daily history. A single lookback-based warmup would make values depend on where a moving fetch window starts. #7 fires from `warmup`; #8/#14 fetch `stable_warmup` candles. Details in §8. |
| Input validation | `compute` calls `validate_candles` | It costs about 0.05 ms for 5 000 candles (measured). TA-Lib recurrences never recover from a NaN input, so garbage must be rejected before it is computed. Prefix closure (spec 004) keeps harness prefixes valid. |
| TA-Lib globals | Never mutated; `ema`, `macd`, `rsi`, `atr` and `adx` kernels check the relevant unstable period is `0` on every call and raise otherwise | Unstable periods shift and change results process-wide (verified). A foreign mutation must fail loudly, not silently change signals. `set_compatibility` is a no-op since TA-Lib C 0.8.1 (MetaStock mode removed upstream), which a canary test pins. |
| MA types | Passed explicitly as `MA_Type.SMA` (`bbands`, `stoch`) | Defaults differ between TA-Lib functions (`KDJ` defaults to `RMA`) and could change; explicit arguments plus the reference tests pin the smoothing. |
| Typing | No stubs and no mypy overrides | `ta-lib` 0.8.0 ships `py.typed` and `_ta_lib.pyi`, so `talib.SMA` and the others are typed `NDArray[np.float64]` (verified with strict mypy and the spec 004 flags). |
| Image check | A build-time smoke `RUN` in the `Dockerfile` runtime stage | `main.py` does not import the indicators yet, so neither `Docker build (arm64)` nor the beta `/health` would notice a TA-Lib wheel that fails to load on arm64. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/__init__.py` | developer | Docstring mentions the indicators subpackage |
| `src/trading_bot/domain/indicators/__init__.py` | developer | Docstring only: pure, TA-Lib isolated in `talib_kernels.py`, import from submodules |
| `src/trading_bot/domain/indicators/errors.py` | developer | Error types (§3) |
| `src/trading_bot/domain/indicators/params.py` | developer | `ParamKind`, `ParamSpec`, `LessThan`, `IndicatorParams`, `validate_params` (§4) |
| `src/trading_bot/domain/indicators/spec.py` | developer | `OutputSpec`, `IndicatorSpec`, type aliases (§5) |
| `src/trading_bot/domain/indicators/registry.py` | developer | `IndicatorRegistry`, `JsonValue` (§5, §10) |
| `src/trading_bot/domain/indicators/talib_kernels.py` | developer | The ten kernels, undefined-value masks, unstable-period guard (§6, §7, §9) |
| `src/trading_bot/domain/indicators/catalog.py` | developer | The ten specs, `CATALOG`, `REGISTRY` (§6) |
| `tests/fixtures/indicator_frames.py` | developer | `candles_from_prices(close, *, high=None, low=None, volume=None)`: tiny valid frames for goldens (open = close; high/low default to close) |
| `tests/unit/test_indicator_params.py` | developer | TDD: T1 |
| `tests/unit/test_indicator_registry.py` | developer | TDD with fake specs: T2–T5 |
| `tests/unit/test_indicator_catalog.py` | developer | TDD: T6–T9, T11 |
| `tests/unit/test_indicator_lookahead.py` | developer | TDD: T10 |
| `tests/unit/test_domain_purity.py` | developer | T12: allow `talib` only in `indicators/talib_kernels.py`; expected scanned files become relative paths including `indicators/*.py`. The developer owns this update because the existing guard fails as soon as the new files exist |
| `tests/fixtures/indicator_reference.py` | tester | Independent plain-Python reference of §6.2 (type-checked through `tests/fixtures`) |
| `tests/unit/test_indicator_reference.py` | tester | T13 |
| `tests/unit/test_indicator_lookahead_properties.py` | tester | T14 |
| `tests/unit/test_indicator_warmup.py` | tester | T15 |
| `tests/unit/test_indicator_backend_guard.py` | tester | T16 |
| `tests/unit/test_indicators_adversarial.py` | tester | T17, T18 |
| `pyproject.toml`, `uv.lock` | developer | `uv add ta-lib` (AC17) |
| `Dockerfile` | developer | Smoke check (§2); explicitly authorized by this spec |
| `docs/ARCHITECTURE.md` | developer | `## Indicators` and the two bullets (§11); explicitly authorized |
| `docs/specs/005-indicator-registry.md` | tech-lead | This spec |

There are no new Protocols (the registry is a domain value, not a port), no migrations and no `TB_*` variables.

**Why the tester writes the reference.** An implementer who writes both the code and its reference can encode the same misunderstanding twice. The developer drives the implementation with the golden cases of §6.4 (TDD); the tester writes the reference from §6.2 alone, without reading `talib_kernels.py`. The tech-lead validated §6.2 with a prototype reference before writing this spec (AC7 measurement).

### 2. Dependencies, typing and image

- `uv add ta-lib` (PyPI name `ta-lib`, import name `talib`). At spec time it resolves to `ta-lib` 0.8.0, which bundles TA-Lib C 0.8.1 and depends only on `numpy`. The tech-lead verified on this Windows machine that the `win_amd64` wheel installs and imports in a scratch virtual environment with pandas 3.0.5 and numpy 2.5.3.
- The `manylinux2014/2_17/2_28 aarch64` wheel for `cp312` bundles `ta_lib.libs/libta-lib-*.so` and, besides that bundled library, links only `libc.so.6` and `libpthread.so.0` (inspected), so `python:3.12-slim` needs no compiler or system package. If `uv` ever fell back to the sdist, the build would fail loudly (no C library), not silently.
- `import talib` imports pandas and, if installed, polars (not a dependency). It initializes the C library and registers its shutdown with `atexit`: library initialization, not state of ours.
- **Typing.** `talib.SMA`, `EMA`, `RSI`, `MACD`, `BBANDS`, `ATR`, `ADX`, `STOCH` and `OBV` are typed by the package stubs. Two names are not exported for mypy because `talib.__all__` omits them:
  - `MA_Type`: import it as `from talib._ta_lib import MA_Type` (typed in `_ta_lib.pyi`; at runtime `MA_Type.SMA` is the integer `0`, which TA-Lib expects);
  - `get_unstable_period`: call it through the single coded ignore of §9.
- **Dockerfile.** In the runtime stage, right after `COPY --from=builder ... /app/.venv /app/.venv` and before `USER app`:

  ```dockerfile
  # Fail the build if TA-Lib cannot load or compute on this platform (linux/arm64 in CI).
  RUN ["python", "-c", "import numpy as np, talib, trading_bot.domain.indicators.catalog; raise SystemExit(0 if talib.SMA(np.arange(1.0, 6.0), timeperiod=5)[-1] == 3.0 else 1)"]
  ```

  Importing `catalog` also builds `REGISTRY`, so an invalid spec fails the build. The command writes nothing (`PYTHONDONTWRITEBYTECODE=1`).

### 3. `errors.py`

```python
from enum import StrEnum

__all__ = [
    "IndicatorComputationError",
    "IndicatorError",
    "IndicatorErrorKind",
    "InvalidOutputError",
    "InvalidParameterError",
    "UnknownIndicatorError",
]


class IndicatorErrorKind(StrEnum):
    UNKNOWN_INDICATOR = "unknown_indicator"
    UNKNOWN_PARAMETER = "unknown_parameter"
    WRONG_TYPE = "wrong_type"
    OUT_OF_RANGE = "out_of_range"
    CONSTRAINT = "constraint"
    UNKNOWN_OUTPUT = "unknown_output"
    MISSING_OUTPUT = "missing_output"


class IndicatorError(ValueError):
    kind: IndicatorErrorKind
    indicator: str | None  # requested indicator name, at most its first 32 characters
    parameter: str | None  # offending parameter name, at most its first 32 characters
    output: str | None  # offending output name, at most its first 32 characters

    def __init__(
        self,
        kind: IndicatorErrorKind,
        message: str,
        *,
        indicator: str | None = None,
        parameter: str | None = None,
        output: str | None = None,
    ) -> None: ...


class UnknownIndicatorError(IndicatorError): ...  # unknown_indicator


# kinds: unknown_parameter, wrong_type, out_of_range, constraint
class InvalidParameterError(IndicatorError): ...


class InvalidOutputError(IndicatorError): ...  # unknown_output, missing_output


class IndicatorComputationError(RuntimeError): ...
```

- `IndicatorError` pickles like `CandleValidationError` (`super().__init__(kind, message)`, attributes restored from `__dict__`), and `str(error)` is the message.
- Messages follow `CandleValidationError`: one line, English, built from registered names and bounded echoes (`repr()` of at most 32 characters that fits 64 columns). Examples: `unknown indicator 'RSI'; expected one of sma, ema, rsi, macd, bbands, atr, adx, stoch, obv, volume_sma` · `parameter 'length' of rsi must be in [2, 100], got 1` · `parameters of macd must satisfy fast < slow, got fast=26, slow=12` · `macd has several outputs; choose one of macd, signal, hist`.
- `IndicatorError` is a `ValueError` so #6 pydantic validators turn it into a validation error. `IndicatorComputationError` is a `RuntimeError` because it is never the user's fault.

### 4. `params.py`

```python
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["IndicatorParams", "LessThan", "ParamKind", "ParamSpec", "ParamValue", "validate_params"]

type ParamValue = int | float


class ParamKind(StrEnum):
    INT = "int"
    FLOAT = "float"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParamSpec:
    name: str
    kind: ParamKind
    default: ParamValue
    minimum: ParamValue
    maximum: ParamValue
    label: str
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LessThan:
    left: str
    right: str


class IndicatorParams(Mapping[str, ParamValue]):
    """Read-only, hashable, picklable mapping of validated parameters in declaration order."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, ParamValue] | None = None) -> None: ...
    def __getitem__(self, key: str) -> ParamValue: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...
    def __hash__(self) -> int: ...  # hash(frozenset(items))
    def __setattr__(self, name: str, value: object) -> None: ...  # AttributeError
    def __delattr__(self, name: str) -> None: ...  # AttributeError
    def __reduce__(self) -> tuple[type[IndicatorParams], tuple[dict[str, ParamValue]]]: ...
    def __repr__(self) -> str: ...
    def integer(self, name: str) -> int: ...  # KeyError if absent, TypeError if not an int
    def real(self, name: str) -> float: ...  # KeyError if absent; ints are returned as float


def validate_params(
    indicator: str,
    specs: Sequence[ParamSpec],
    constraints: Sequence[LessThan],
    params: Mapping[str, object],
) -> IndicatorParams: ...
```

- Follows the `IndicatorValues` pattern of spec 004 (verified there: `pickle`, `deepcopy`, hashing, equality with `dict`).
- Integer check: `numbers.Integral` and not `bool`. Float check: `numbers.Real` and not `bool`, converted with `float()` (an `OverflowError` from a huge integer is `out_of_range`). The range check is `minimum <= value <= maximum`, so NaN fails it. No module here imports pandas or numpy (numpy scalars register with `numbers`).
- `IndicatorParams` is the cache key #7 needs: `(name, params)` is hashable.

### 5. `spec.py` and `registry.py`

```python
# spec.py
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from trading_bot.domain.indicators.params import IndicatorParams, LessThan, ParamSpec

__all__ = ["FloatArray", "IndicatorSpec", "InputArrays", "Kernel", "OutputSpec", "ParamsFunction"]

type FloatArray = npt.NDArray[np.float64]
type InputArrays = Mapping[str, FloatArray]  # one float64 array per name in IndicatorSpec.inputs
type Kernel = Callable[[InputArrays, IndicatorParams], tuple[FloatArray, ...]]
type ParamsFunction = Callable[[IndicatorParams], int]


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputSpec:
    name: str
    label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class IndicatorSpec:
    name: str
    label: str
    description: str
    inputs: tuple[str, ...]  # candle columns, in the order the kernel reads them
    params: tuple[ParamSpec, ...]
    constraints: tuple[LessThan, ...]
    outputs: tuple[OutputSpec, ...]
    lookback: ParamsFunction  # leading candles without a value
    settle: ParamsFunction  # extra candles until the seed weighs about 0.1% or less (§8)
    kernel: Kernel  # one float64 array per output, each as long as the inputs

    def __post_init__(self) -> None: ...  # AC14 checks

    @property
    def output_names(self) -> tuple[str, ...]: ...

    @property
    def default_output(self) -> str | None: ...  # "value" for one output, None otherwise
```

```python
# registry.py
from collections.abc import Iterable, Mapping

import pandas as pd

__all__ = ["IndicatorRegistry", "JsonValue"]

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class IndicatorRegistry:
    """Immutable catalog of indicator specs, looked up by exact name."""

    __slots__ = ("_by_name", "_specs")

    def __init__(self, specs: Iterable[IndicatorSpec]) -> None: ...
    def __setattr__(self, name: str, value: object) -> None: ...  # AttributeError
    def __contains__(self, name: object) -> bool: ...

    @property
    def names(self) -> tuple[str, ...]: ...  # catalog order

    def get(self, name: str) -> IndicatorSpec: ...
    def validate_params(self, name: str, params: Mapping[str, object]) -> IndicatorParams: ...
    def resolve_output(self, name: str, output: str | None) -> str: ...
    def lookback(self, name: str, params: Mapping[str, object]) -> int: ...
    def warmup(self, name: str, params: Mapping[str, object]) -> int: ...
    def stable_warmup(self, name: str, params: Mapping[str, object]) -> int: ...
    def compute(
        self, name: str, params: Mapping[str, object], candles: pd.DataFrame
    ) -> dict[str, pd.Series[float]]: ...
    def describe(self) -> list[dict[str, JsonValue]]: ...
```

The tech-lead type-checked a prototype of these signatures (`Mapping` subclass, frozen slotted dataclasses with callable fields, the recursive `JsonValue` alias, `pd.Series[float]` returns) with mypy strict and the spec 004 flags, and ran it against TA-Lib.

**`compute` steps:**

1. `spec = self.get(name)`: `TypeError` for a non-`str`, `UnknownIndicatorError` for an unknown name.
2. `resolved = validate_params(...)`.
3. `validate_candles(candles)`.
4. If `len(candles) <= spec.lookback(resolved)`, every output is `np.full(len(candles), np.nan)` and the kernel is not called.
5. Otherwise call `spec.kernel({column: candles[column].to_numpy(dtype=np.float64) for column in spec.inputs}, resolved)`. If the result does not have exactly one 1-D `float64` array of `len(candles)` rows per output, raise `IndicatorComputationError`. Replace `±inf` with NaN.
6. Wrap each array in `pd.Series(array, index=candles.index, name=output.name)` and return the `dict`.

`get` never strips or case-folds. `lookback`, `warmup` and `stable_warmup` validate `params` first. The registry keeps no state between calls.

### 6. Catalog (`catalog.py`, `talib_kernels.py`)

#### 6.1 Indicators

| Name | Label | Inputs | Parameters: type, default, `[min, max]` | Constraint | Outputs (label) | Default output | `lookback` | `settle` |
|------|-------|--------|-----------------------------------------|------------|-----------------|----------------|------------|----------|
| `sma` | SMA | close | `length` int 20 `[2, 500]` | — | `value` (SMA) | `value` | `length - 1` | `0` |
| `ema` | EMA | close | `length` int 20 `[2, 500]` | — | `value` (EMA) | `value` | `length - 1` | `ema_settle(length)` |
| `rsi` | RSI | close | `length` int 14 `[2, 100]` | — | `value` (RSI) | `value` | `length` | `7 * length` |
| `macd` | MACD | close | `fast` int 12 `[2, 100]`, `slow` int 26 `[3, 200]`, `signal` int 9 `[1, 100]` | `fast < slow` | `macd` (MACD line), `signal` (Signal line), `hist` (Histogram) | none | `slow + signal - 2` | `ema_settle(slow) + ema_settle(signal)` |
| `bbands` | Bollinger Bands | close | `length` int 20 `[2, 500]`, `std` float 2.0 `[0.1, 5.0]` | — | `lower` (Lower band), `middle` (Middle band), `upper` (Upper band) | none | `length - 1` | `0` |
| `atr` | ATR | high, low, close | `length` int 14 `[1, 100]` | — | `value` (ATR) | `value` | `length` | `7 * length` |
| `adx` | ADX | high, low, close | `length` int 14 `[2, 100]` | — | `value` (ADX) | `value` | `2 * length - 1` | `10 * length` |
| `stoch` | Stochastic | high, low, close | `length` int 14 `[1, 100]`, `smooth_k` int 3 `[1, 100]`, `smooth_d` int 3 `[1, 100]` | — | `k` (%K), `d` (%D) | none | `length + smooth_k + smooth_d - 3` | `0` |
| `obv` | OBV | close, volume | `signal` int 20 `[2, 500]` | — | `value` (OBV), `signal` (Signal line) | none | `signal - 1` | `0` |
| `volume_sma` | Volume SMA | volume | `length` int 20 `[2, 500]` | — | `value` (Volume SMA) | `value` | `length - 1` | `0` |

`ema_settle(p) = (7 * (p + 1) + 1) // 2`, that is `ceil(3.5 * (p + 1))`. All formulas are integer arithmetic.

Values at default parameters (for tests and docs):

| Indicator | `lookback` | `warmup` | `stable_warmup` |
|-----------|-----------:|---------:|----------------:|
| `sma`, `volume_sma`, `bbands`, `obv` | 19 | 20 | 20 |
| `ema` | 19 | 20 | 94 |
| `rsi`, `atr` | 14 | 15 | 113 |
| `macd` | 33 | 34 | 164 |
| `adx` | 27 | 28 | 168 |
| `stoch` | 17 | 18 | 18 |

Ranges, defaults and labels are user-visible (dashboard forms and stored rule JSON). The user approved the parameter names, defaults and ranges (D5).

Parameter labels and descriptions:

| Indicator | Parameter | Label | Description |
|-----------|-----------|-------|-------------|
| `sma`, `volume_sma`, `bbands` | `length` | Length | Candles in the window. |
| `ema`, `rsi`, `atr`, `adx` | `length` | Length | Smoothing period in candles. |
| `macd` | `fast` | Fast length | Period of the fast EMA in candles. |
| `macd` | `slow` | Slow length | Period of the slow EMA in candles. |
| `macd` | `signal` | Signal length | Period of the signal line EMA in candles. |
| `bbands` | `std` | Standard deviations | Distance of the bands from the middle band, in population standard deviations. |
| `stoch` | `length` | %K length | Candles in the high-low range. |
| `stoch` | `smooth_k` | %K smoothing | Candles averaged to smooth %K. |
| `stoch` | `smooth_d` | %D smoothing | Candles of %K averaged into %D. |
| `obv` | `signal` | Signal length | Candles in the simple moving average of OBV. |

Indicator descriptions (exact strings):

| Indicator | Description |
|-----------|-------------|
| `sma` | Simple moving average of the close over the last length candles. |
| `ema` | Exponential moving average of the close with smoothing 2 / (length + 1), seeded with the simple average of the first length closes. |
| `rsi` | Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined while the close has not changed since the first candle. |
| `macd` | Fast EMA minus slow EMA of the close (MACD line), an EMA of that line (signal line) and their difference (histogram). |
| `bbands` | Simple moving average of the close (middle band) plus and minus std population standard deviations over the last length candles. |
| `atr` | Wilder's average true range over length candles, in price units. |
| `adx` | Wilder's average directional index, from 0 to 100: trend strength regardless of direction. Undefined until the first candle with directional movement. |
| `stoch` | Slow stochastic oscillator from 0 to 100: %K is the close within the high-low range of the last length candles, averaged over smooth_k candles; %D averages %K over smooth_d candles. Undefined when that range is zero. |
| `obv` | On-balance volume: the first candle's volume, plus the volume of each candle that closes higher and minus the volume of each candle that closes lower, with its simple moving average over signal candles (signal line). The OBV level depends on the first candle of the history; its position relative to the signal line does not. |
| `volume_sma` | Simple moving average of the volume over the last length candles. |

#### 6.2 Definitions

Notation: `i` is a 0-based row position in the frame; `mean(x[a..b])` averages positions `a` to `b` inclusive; every output is NaN for `i < lookback` and may be NaN afterwards only as stated in §7. These are the semantics of TA-Lib 0.8.0 as called by the kernels (the tech-lead matched them with a plain-Python prototype to `7e-12 * scale`). Where TA-Lib differs from a textbook, the difference is stated.

- **`sma`(L)**, **`volume_sma`(L)**: `value[i] = mean(x[i-L+1..i])` of `close` (respectively `volume`). Kernel: `talib.SMA(x, timeperiod=L)`.
- **`ema`(L)**: `α = 2 / (L + 1)`; `value[L-1] = mean(close[0..L-1])`; `value[i] = value[i-1] + α * (close[i] - value[i-1])`. Kernel: `talib.EMA`.
- **`rsi`(L)**: `change[i] = close[i] - close[i-1]`, `gain = max(change, 0)`, `loss = max(-change, 0)`. `G[L] = mean(gain[1..L])`, `D[L] = mean(loss[1..L])`, then `G[i] = (G[i-1] * (L - 1) + gain[i]) / L` and the same for `D`. `value[i] = 100 * G[i] / (G[i] + D[i])`. Kernel: `talib.RSI`.
- **`macd`(F, S, G)**: slow EMA with `α_S = 2 / (S + 1)`, seeded with `mean(close[0..S-1])` at `S-1`; fast EMA with `α_F = 2 / (F + 1)`, seeded with `mean(close[S-F..S-1])` at `S-1`. TA-Lib ends both seed windows on the same candle, so the fast EMA does not start at candle 0. `line[i] = fast[i] - slow[i]` for `i >= S-1`. The signal line is an EMA of `line` with `α_G = 2 / (G + 1)`, seeded with `mean(line[S-1..S+G-2])` at `S+G-2`. When `G = 1`, `signal = line` exactly. `hist = line - signal`. All three outputs start at `S+G-2`, although `line` is defined from `S-1`. Kernel: `talib.MACD`.
- **`bbands`(L, k)**: `middle[i] = mean(close[i-L+1..i])`; `sd[i]` is the **population** standard deviation of the same window (divide by `L`); `upper = middle + k * sd`, `lower = middle - k * sd`. TA-Lib sets `sd` to exactly `0` below a scale-relative variance floor, so flat windows give three equal bands. Kernel: `talib.BBANDS(close, timeperiod=L, nbdevup=k, nbdevdn=k, matype=MA_Type.SMA)`, whose tuple order is `(upper, middle, lower)`: the kernel returns `(lower, middle, upper)`.
- **`atr`(L)**: `tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))` for `i >= 1`. `value[L] = mean(tr[1..L])`, then `value[i] = (value[i-1] * (L - 1) + tr[i]) / L`. With `L = 1`, `value = tr`. Kernel: `talib.ATR`.
- **`adx`(L)**, Wilder with TA-Lib's seeding. For `i >= 1`: `up = high[i] - high[i-1]`, `down = low[i-1] - low[i]`; `+DM[i] = up` if `up > 0` and `up > down`, else `0`; `-DM[i] = down` if `down > 0` and `down > up`, else `0`; `tr` as in `atr`.
  - Seed sums over positions `1..L-1` (**`L - 1` values**): `P = sum(+DM)`, `M = sum(-DM)`, `T = sum(tr)`.
  - For each `i >= L`: `P = P - P / L + +DM[i]`, and the same for `M` with `-DM` and `T` with `tr`. If `T > 0`, `+DI = 100 * P / T` and `-DI = 100 * M / T`; if also `+DI + -DI` is not zero (`abs(...) >= 1e-14`), `DX[i] = 100 * abs(+DI - -DI) / (+DI + -DI)`; otherwise `DX[i]` is undefined.
  - `value[2L-1]` is the sum of the **defined** `DX[L..2L-1]` divided by `L`. For `i >= 2L`, `value[i] = (value[i-1] * (L - 1) + DX[i]) / L` when `DX[i]` is defined; otherwise `value[i] = value[i-1]`.
  - Kernel: `talib.ADX`.
- **`stoch`(N, K, D)**, slow stochastic with simple averages: `hh[i] = max(high[i-N+1..i])`, `ll[i] = min(low[i-N+1..i])`, `raw[i] = 100 * (close[i] - ll[i]) / (hh[i] - ll[i])`; `k[i] = mean(raw[i-K+1..i])`; `d[i] = mean(k[i-D+1..i])`. Both outputs start at `N+K+D-3`, although `k` is defined from `N+K-2`. Kernel: `talib.STOCH(high, low, close, fastk_period=N, slowk_period=K, slowk_matype=MA_Type.SMA, slowd_period=D, slowd_matype=MA_Type.SMA)`.
- **`obv`**(P): `level[0] = volume[0]` (TA-Lib seeds with the first volume, not 0); `level[i] = level[i-1] + volume[i]` if `close[i] > close[i-1]`, `- volume[i]` if lower, unchanged if equal. `value[i] = level[i]` and `signal[i] = mean(level[i-P+1..i])`. Both outputs start at `P-1`, although `level` is defined from `0` (the same alignment as `macd` and `stoch`). Kernel: `level = talib.OBV(close, volume)`, then `talib.SMA(level, timeperiod=P)` for `signal`, and `value` is a copy of `level` set to NaN before `P-1`.

Kernels are module-level functions of `talib_kernels.py`, one per indicator, with the `Kernel` signature. They read only `inputs` and `params`, allocate their outputs, and pass every TA-Lib argument by keyword.

#### 6.3 Output labels

Single-output indicators label `value` with the indicator label. Multi-output labels are in the §6.1 table.

#### 6.4 Golden cases (AC6)

Frames come from `candles_from_prices` (open = close; high and low equal the close unless given; volume `1.0` unless given). Values are listed by position; `nan` means NaN.

| Case | Input | Expected |
|------|-------|----------|
| `sma` `length=3` | close `1, 2, 3, 4, 5` | `nan, nan, 2, 3, 4` |
| `ema` `length=3` | close `1, 2, 3, 4, 5` | `nan, nan, 2, 3, 4` |
| `rsi` `length=2` | close `1, 2, 1, 2, 3` | `nan, nan, 50, 75, 87.5` |
| `obv` `signal=2` | close `1, 2, 2, 1`; volume `10, 20, 30, 40` | `value`: `nan, 30, 30, -10`; `signal`: `nan, 20, 30, 10` |
| `bbands` `length=2, std=1.0` | close `1, 3, 5` | lower `nan, 1, 3`; middle `nan, 2, 4`; upper `nan, 3, 5` |
| `macd` `fast=3, slow=7, signal=3` | close `1, 2, ..., 10` | `macd` and `signal`: 8 × `nan`, then `2, 2`; `hist`: 8 × `nan`, then `0, 0` |
| `atr` `length=2` | high `11, 12, 13, 12`; low `9, 10, 11, 8`; close `10, 11, 12, 9` | `nan, nan, 2, 3` |
| `stoch` `length=2, smooth_k=1, smooth_d=2` | high `10, 12, 11, 13`; low `8, 9, 9, 10`; close `9, 11, 10, 12` | `k`: `nan, nan, 100/3, 75`; `d`: `nan, nan, 325/6, 325/6` |
| `adx` `length=3`, uptrend | close `c = 2, 3, ..., 13`; high `c + 0.5`; low `c - 0.5` | 5 × `nan`, then 7 × `100` |
| `adx` `length=3`, downtrend | the same frame reversed in time (index stays increasing) | 5 × `nan`, then 7 × `100` |
| `rsi` `length=2`, flat start (§7) | close `5, 5, 5, 6` | `nan, nan, nan, 100` |
| `stoch` `length=3, smooth_k=1, smooth_d=2`, flat start (§7) | high `5, 5, 5, 6, 7`; low `5, 5, 5, 5, 6`; close `5, 5, 5, 6, 6.5` | `k`: `nan, nan, nan, 100, 75`; `d`: `nan, nan, nan, nan, 87.5` |
| `adx` `length=2`, flat start (§7) | high `10, 10, 10, 10, 11.5, 12.5, 13.5`; low `10, 10, 10, 10, 10.5, 11.5, 12.5`; close `10, 10, 10, 10, 11, 12, 13` | `nan, nan, nan, nan, 50, 75, 87.5` |

The tech-lead computed every row with TA-Lib 0.8.0 (the `adx` flat-start values keep TA-Lib's treatment of the undefined seed `DX` values, §6.2).

### 7. Undefined values

TA-Lib substitutes `0` where these formulas divide by zero. The kernels replace those values with NaN using masks computed from the inputs only (row-local or cumulative, so no look-ahead):

| Indicator | NaN at `i >= lookback` when | TA-Lib returns |
|-----------|-----------------------------|----------------|
| `rsi` | every close from position `0` to `i` is equal (`G[i] + D[i] == 0`) | `0` |
| `adx` | no candle from position `1` to `i` has `+DM > 0` or `-DM > 0` (no `DX` has been defined yet) | `0` |
| `stoch` `k` | some `raw` in `k`'s window `[i-K+1..i]` is undefined, where `raw[j]` is undefined when `hh[j] - ll[j] <= 1e-14 * (hh[j] + ll[j])` (TA-Lib's scale-relative zero test) | `0` for that `raw` |
| `stoch` `d` | `k` is NaN somewhere in `d`'s window `[i-D+1..i]` | a value averaging those zeros |

- The `stoch` condition uses TA-Lib's own floor, so the mask marks exactly the windows TA-Lib replaced (the tech-lead checked the boundary on the wheel at prices `1e-6`, `1` and `1e3` with relative ranges from `1e-15` to `1e-13`). `rsi` and `adx` use exact conditions: TA-Lib tests `G + D > 0` exactly, and its `1e-14` test on `+DI + -DI` only differs from "no directional movement" for directional moves below about 1e-16 of the true range, which the reference ignores too.
- `sma`, `ema`, `macd`, `bbands`, `atr`, `obv` and `volume_sma` are defined for every valid frame from `lookback` on (a flat window gives `0` for `macd` and `atr` and three equal bands, which are correct values, not substitutes).
- **Known limit:** after a price move, Wilder averages decay by `(L - 1) / L` per flat candle. With `rsi` `length=2`, both averages underflow to `0` after about 1 074 identical closes (measured), and TA-Lib then returns `0` where the mask expects a value. Longer lengths need far longer runs. It is documented, not masked.
- `±inf` cannot come from valid frames of realistic magnitude; the registry turns any into NaN anyway (§5 step 5).

### 8. Warmup and stable warmup

- `lookback(params)`: leading candles without a value; the first value is at position `lookback`.
- `warmup(params) = lookback + 1`: the minimum frame length for a value at the last candle. #7 does not fire below it (the value is NaN anyway); crossovers need one more candle (#6/#7 decide).
- `stable_warmup(params) = warmup + settle(params)`: for recursive indicators, the frame length after which the value no longer depends on where the history starts, within about 0.1%:
  - EMA smoothing (`ema`; `macd` stages): the seed weighs `(1 - α)^m <= e^(-α m)`; with `α = 2 / (p + 1)` and `m = 3.5 * (p + 1)` that is `e^-7 ≈ 9.1e-4`;
  - Wilder smoothing (`rsi`, `atr`, `α = 1 / p`): `m = 7 * p` gives `e^-7`;
  - `adx` chains two Wilder stages: the residual is at most `(1 + α m) * e^(-α m)`, and `m = 10 * p` gives about `5.0e-4`;
  - `macd` adds the slow and signal stages;
  - window-exact indicators (`sma`, `bbands`, `stoch`, `volume_sma`) settle immediately, and so does the difference between the two `obv` outputs (see the `obv` bullet below).

  Tech-lead measurement on 4 000-candle frames (seeds 0–4; `RANDOM_WALK`, `GAPS`, `MIXED`), comparing windows starting 1, 7, 50, 333 and 900 candles later: worst difference `6.9e-4` of the local close (`ema` `length=100`) and `0.019` points for `rsi`. A single Wilder stage for `adx` gave `0.094` points, hence `10 * p`.
- **Why two numbers.** A value computed on a window whose start moves (a rolling fetch) differs from the long-history value until the seed has decayed. That is not look-ahead: each value only depends on past candles. It affects reproducibility between runs and against backtests. Requiring `stable_warmup` to fire would silence young tickers: `ema` `length=200` would need 904 daily candles. With two numbers, #8/#14 fetch `stable_warmup` candles, or everything available. If the history is shorter, the window always starts at the first available candle and results are reproducible anyway.
- **History limits (for #8/#10/#14).** For US equities, 730 days of `1h` data is about 3 500 bars and the session-aligned `4h` resample about 1 000. `stable_warmup` exceeds 1 000 from `ema` `length=222`, `rsi` and `atr` `length=125`, `adx` `length=84`, and `macd` `slow=200` with `signal >= 21`. On `4h`, such rules still fire from `warmup`, but runs may differ by up to about 0.1% as the fetch window moves.
- **`obv` (user decision D3).** Its level is the cumulative sum from the first candle of the frame, so moving the start shifts every `value` by one constant `C`. Once its `signal` window lies inside the frame, `signal` shifts by the same `C`. Therefore `value - signal`, `value` compared with `signal`, and their crossovers are identical for any history start from `warmup` on (`settle = 0`, `stable_warmup = warmup = signal`). Comparing `value` or `signal` with a fixed value, a price or another indicator stays non-reproducible for any history length; the description says so, and the required output (§0) makes the choice of `value` explicit.

### 9. TA-Lib isolation and global settings

- Only `talib_kernels.py` imports `talib`. `catalog.py` imports the kernels, and `registry.py`, `spec.py`, `params.py` and `errors.py` never import `talib` or `catalog`.
- Nothing in `src/` calls `set_unstable_period`, `set_compatibility`, `talib.abstract` or `talib.stream`.
- The `ema` and `macd` kernels require the `EMA` unstable period to be `0` (`MACD` uses the EMA lookback); `rsi` requires `RSI`, `atr` requires `ATR`, and `adx` requires `ADX`. They check it on every call, before calling TA-Lib (a C call, negligible cost):

  ```python
  def _require_default_unstable_period(function: str) -> None:
      # talib.get_unstable_period is not in talib.__all__, so the package stubs do not export it.
      period: object = talib.get_unstable_period(function)  # type: ignore[attr-defined]
      if period != 0:
          raise IndicatorComputationError(
              f"TA-Lib unstable period for {function} is {period!r}; the indicator registry requires 0"
          )
  ```

  The tech-lead type-checked this helper (strict and the spec 004 flags) and confirmed that it raises while `ADX` is `3` and passes after restoring `0`. `sma`, `bbands`, `stoch`, `obv` and `volume_sma` have no unstable period in TA-Lib 0.8.0.
- `set_compatibility` does nothing in the bundled TA-Lib C 0.8.1: the MetaStock mode was removed, `TA_SetCompatibility` is kept only so old code still links, and `TA_GetCompatibility` always returns `TA_COMPATIBILITY_DEFAULT` (upstream `include/ta_func.h` at `v0.8.1`; confirmed at review, where `get_compatibility()` reads `0` right after `set_compatibility(1)`). The spec draft's statement that the functions "do not read it" was an inference from identical outputs; the real reason is that the setting no longer exists. AC12's canary asserts that no-op directly, so it fails if an upgrade brings the setting back.
- **Thread safety.** Kernels hold no state. TA-Lib reads its global settings but only writes them when a setter is called. `compute` is therefore safe to call from several threads as long as nothing in the process calls a TA-Lib setter; the guard turns a violation into an error instead of wrong values.

### 10. `describe()` JSON

One object per indicator, in catalog order. Parameter bounds and defaults keep their Python type (`int` or `float`). `default_warmup` and `default_stable_warmup` are computed at default parameters; clients get values for other parameters from the server (#22), never by evaluating formulas.

Literal `rsi` entry (AC13):

```json
{
  "name": "rsi",
  "label": "RSI",
  "description": "Wilder's relative strength index of close-to-close changes, from 0 to 100. Undefined while the close has not changed since the first candle.",
  "inputs": ["close"],
  "params": [
    {"name": "length", "label": "Length", "description": "Smoothing period in candles.", "type": "int", "default": 14, "min": 2, "max": 100}
  ],
  "constraints": [],
  "outputs": [{"name": "value", "label": "RSI"}],
  "default_output": "value",
  "default_warmup": 15,
  "default_stable_warmup": 113
}
```

Literal `macd` entry (AC13):

```json
{
  "name": "macd",
  "label": "MACD",
  "description": "Fast EMA minus slow EMA of the close (MACD line), an EMA of that line (signal line) and their difference (histogram).",
  "inputs": ["close"],
  "params": [
    {"name": "fast", "label": "Fast length", "description": "Period of the fast EMA in candles.", "type": "int", "default": 12, "min": 2, "max": 100},
    {"name": "slow", "label": "Slow length", "description": "Period of the slow EMA in candles.", "type": "int", "default": 26, "min": 3, "max": 200},
    {"name": "signal", "label": "Signal length", "description": "Period of the signal line EMA in candles.", "type": "int", "default": 9, "min": 1, "max": 100}
  ],
  "constraints": [{"type": "less_than", "left": "fast", "right": "slow"}],
  "outputs": [
    {"name": "macd", "label": "MACD line"},
    {"name": "signal", "label": "Signal line"},
    {"name": "hist", "label": "Histogram"}
  ],
  "default_output": null,
  "default_warmup": 34,
  "default_stable_warmup": 164
}
```

Keys appear in the order shown. `bbands` `std` has `"type": "float", "default": 2.0, "min": 0.1, "max": 5.0`. `obv` has one parameter (`{"name": "signal", "label": "Signal length", "description": "Candles in the simple moving average of OBV.", "type": "int", "default": 20, "min": 2, "max": 500}`), the outputs `value` (OBV) and `signal` (Signal line), `"default_output": null`, `"default_warmup": 20` and `"default_stable_warmup": 20`.

### 11. `docs/ARCHITECTURE.md`

- New `## Indicators` section right before `## Rule model`, short, with:
  1. the API in one example (ruff-format clean), importing `REGISTRY` from `trading_bot.domain.indicators.catalog` and showing `compute`, `warmup` and `stable_warmup`;
  2. the catalog table of §6.1 (names, inputs, parameters with defaults and ranges, outputs, `lookback`, `settle`);
  3. output semantics: full-length series on the candle index, `float64`, NaN before `lookback` and where undefined (§7 table, in words), required output for `macd`, `bbands`, `stoch` and `obv`;
  4. `warmup` against `stable_warmup`, the reproducibility trade-off, the history limits, and the `obv` caveat with its signal line (§8);
  5. TA-Lib isolation, global settings and the unstable-period guard (§9);
  6. **How to add an indicator:** a spec; a `talib_kernels.py` kernel (keyword arguments, explicit MA types, masks for undefined values); a `catalog.py` entry with `lookback` and `settle`; the definition in this section; golden cases; the independent reference and comparison matrix; look-ahead tests on every scenario plus the property; warmup and stable-warmup tests; the updated `describe()` expectations.
- **Rule model** bullet "Indicators (initial whitelist)": link to `## Indicators` for parameters and outputs.
- **Decisions** bullet "TA-Lib instead of pandas-ta": state `ta-lib` 0.8.0 wheels bundle the C library (including `manylinux` aarch64 for Python 3.12), isolation in `talib_kernels.py`, and that TA-Lib global setters must never be called.

### 12. Forward compatibility

| Issue | How it uses #5 |
|-------|----------------|
| #6 | Operand validation calls `REGISTRY.validate_params` and stores `dict(result)`, with defaults filled, so stored rules do not change if a default changes later. `resolve_output` handles `output?`. Rule `warmup`/`stable_warmup` are maxima over operands (+1 for crossovers). The JSON Schema maps `describe()` params to `integer`/`number` with `minimum`, `maximum` and `default`; `less_than` is validated server-side. Validators wrap `IndicatorError` (a `ValueError`). |
| #7 | Computes each distinct `(name, IndicatorParams)` once per evaluation. NaN does not fire; crossovers need two finite candles; only finite values go into `Signal.indicator_values`. Look-ahead: `assert_no_lookahead_point_in_time` with a reference built on `REGISTRY.compute`. May take a `registry` argument that defaults to `REGISTRY`. |
| #8, #10, #14 | Fetch at least the rules' `stable_warmup` candles, capped by provider limits, and log when capped (§8). |
| #13 | Persists normalized params as JSON. |
| #17 | Uses `describe()` labels; never shows NaN values. |
| #22, #24 | Serve `describe()` as JSON. Forms come from `params`; an output selector is required when `default_output` is `null`; warmups for non-default parameters are computed server-side. |
| F8 (backtesting) | Uses the same registry and never calls TA-Lib global setters. |

## Test plan

All tests are unit tests without network. Mandatory template cases:

- **Anti look-ahead:** T10 and T14 (every indicator × every scenario, full sweeps, property), exact comparison.
- **Idempotency:** N/A (no signals). `compute` determinism and `validate_params` idempotence are covered by T3, T1 and T17.
- **Authorization** and **secret redaction:** N/A (no Telegram, API, configuration or logging changes; no secrets). Bounded, single-line error messages are covered by T1 and T17.

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 params and errors | unit | Defaults, int/float normalization, every rejection kind and check order, `TypeError`s, idempotence, `IndicatorParams` behavior (read-only, hash, pickle, `dict` equality, `integer`/`real`), error hierarchy, attributes, pickling and bounded messages | AC2, AC4, AC14 | developer |
| T2 spec and registry construction | unit | Every `IndicatorSpec` and `IndicatorRegistry` rejection of AC14 with fake specs; immutability; `names`, `get`, `__contains__` | AC3, AC14 | developer |
| T3 compute contract | unit | With fake kernels: validation order, short and empty frames skip the kernel (a kernel that raises proves it), wrong kernel results raise `IndicatorComputationError`, `±inf` becomes NaN, fresh series and no input mutation, determinism | AC5 | developer |
| T4 outputs | unit | `resolve_output` cases | AC3 | developer |
| T5 describe | unit | Shape, catalog order, `allow_nan=False` serialization, fresh results, the `rsi` and `macd` literals | AC13 | developer |
| T6 catalog metadata | unit | Every row of §6.1 and the §6.1 label and description tables; default warmups table | AC1, AC9 | developer |
| T7 goldens | unit | Every row of §6.4 through `REGISTRY.compute` | AC6, AC8 | developer |
| T8 undefined values | unit | The flat `RANDOM_WALK` frame; `FLAT_RUNS` with `stoch` `length=5, smooth_k=1, smooth_d=1`; finite outputs on `RANDOM_WALK`, `GAPS` and `EXTREME` | AC8 | developer |
| T9 lookback and warmup | unit | Formulas; leading NaN count equals `lookback` for default, minimum (`sma`/`ema`/`volume_sma`/`bbands` `length=2`, `std=0.1`; `rsi`/`adx` `2`; `atr` `1`; `macd` `2/3/1`; `stoch` `1/1/1`; `obv` `signal=2`) and large (`length=50`; `macd` `50/100/30`; `stoch` `30/5/5`; `obv` `signal=50`) sets on 400-candle non-flat frames; frames of `warmup - 1` and `warmup` candles | AC9 | developer |
| T10 look-ahead per scenario | unit | `assert_no_lookahead` for every indicator × every `Scenario` at defaults on `synthetic_candles(300, seed=...)`, exact | AC11 | developer |
| T11 unstable-period guard | unit | For `EMA` (via `ema` and `macd`), `RSI`, `ATR` and `ADX`: set a non-zero period inside `try`/`finally`, assert `IndicatorComputationError`, restore `0`, assert success | AC12 | developer |
| T12 purity guard | unit | Updated allowlist (`talib` only in `talib_kernels.py`) and scanned-file list; existing checks cover the new modules | AC16 | developer |
| T13 independent reference | unit | Reference written from §6.2 only. Matrix: every `Scenario` × seeds `0..3` × timeframes `1d` and `1h` (500 candles); `volatility=0.0` frames; `start_price=1e-6` and `start_price=5e7` with `volatility=0.25`; 60 seeded random draws of size, scenario, start price (`10^-3..10^5`) and volatility; × the default, minimum and large parameter sets of T9. NaN positions identical; AC7 tolerance | AC7, AC8 | tester |
| T14 look-ahead extended | unit + `@given` | Full sweep (`max_cuts=len(candles)`) on `synthetic_candles(200, scenario=MIXED)` with the T9 large set per indicator; one property drawing the indicator, parameters (integers within `[min, min(max, 30)]`, satisfying constraints) and `candle_frames(min_size=warmup + 5, max_size=warmup + 60)`, with `assume` against all-NaN draws (flat frames) and `max_cuts=10` | AC11 | tester |
| T15 warmup behavior | unit | AC10 window-start invariance; for `obv`, `value - signal` invariance and the constant shift of `value` between a later-starting window and the whole frame | AC10 | tester |
| T16 TA-Lib isolation and globals | unit | AST scan of `src/` for `talib` imports and forbidden names; fresh-interpreter imports (`registry`, `params`, `spec`, `errors` without `talib`; `catalog` with it); settings unchanged after computing everything; `set_compatibility(1)` no-op canary (`get_compatibility() == 0` right after the call), restored in `finally` | AC12 | tester |
| T17 adversarial parameters | unit | 10 000-character names and values with newlines and ANSI escapes; `10**100` for integer and float parameters; `numpy.bool_`, `numpy.float32`, `Fraction`, `-0.0` and `5e-324` for `std`; nested containers as values; `IndicatorParams` passed back to `compute`; Hypothesis: `validate_params` idempotence over drawn valid parameters | AC2, AC4 | tester |
| T18 adversarial frames | unit | Frames of length `0`, `1`, `lookback` and `warmup`; index units `s`/`ms`; `attrs` set; prices at `1e-6` and `1e9`; volume `1e15`; `stoch` `length=1` on flat candles; `macd` `signal=1` (`signal == macd`, `hist == 0`); `atr` `length=1` equals the true range; a near-flat `stoch` window with range `1e-15` relative; returned series independent of each other and of the input | AC5, AC8 | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC20 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot.domain --cov-branch --cov-report=term-missing`: 100% for every module under `domain/` | AC20 |
| V3 budget | `uv run pytest --durations=40 -q`: new tests ≤ 15 s in total, none above 3 s | AC20 |
| V4 typing | `uv run mypy`; `uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated src/trading_bot/domain`; the spec 004 `git grep` for `Any` is empty; `git grep -n "type: ignore" src/trading_bot/domain` shows only the §9 line | AC15 |
| V5 runtime dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch against the same command in a temporary `git worktree` of `5e1ab04` (scratch directory, removed afterwards): only `ta-lib==0.8.0` and `# via` lines | AC17 |
| V6 runtime-only import | `UV_PROJECT_ENVIRONMENT=<scratch>/venv uv sync --locked --no-dev`, then that interpreter imports `trading_bot.domain.indicators.catalog` and runs `REGISTRY.compute("rsi", {}, frame)` on a small valid frame built inline. Do not use `uv run --no-dev` on the project environment | AC17 |
| V7 wheel pin | `uv.lock` lists `ta_lib-0.8.0-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64.whl` | AC17 |
| V8 no `pandas-ta` | `git grep -n -i -E "pandas[-_]ta\b"` matches only prohibition notes in Markdown | AC17 |
| V9 docs | Section placement; `uv run ruff format --check docs/ARCHITECTURE.md`; manual read against §11 | AC19 |
| V10 scope | `git diff 5e1ab04 --name-only` plus `git status --porcelain` list only the §1 files. After #3 and #4 merge and the branch is rebased, compare with `origin/main` | AC21 |
| V11 secrets and language | `python scripts/secret_scan.py --history`; English-only review of the diff | — |
| V12 Docker | **BLOCKED locally** (the daemon is not running), and this change **does** alter the image. V6 is the local substitute. The authoritative checks are the PR's `Docker build (arm64)` job, which runs the §2 smoke check, and the beta deploy with `/health` verified by the lead. Report as BLOCKED with this justification, not as PASS | AC18 |

**TDD order suggested to the developer:** dependency (`uv add ta-lib`, isolated lock diff) → T12 purity guard update → T1 (`errors.py`, `params.py`) → T2 (`spec.py`, `registry.py` construction) → T3–T5 with fake specs → T6 catalog metadata → T7 goldens kernel by kernel (`sma`, `volume_sma`, `ema`, `obv`, `bbands`, `rsi`, `atr`, `macd`, `stoch`, `adx`) → T8 masks → T9 → T10 → T11 → `Dockerfile` → docs.

## Risks and security

- **Semantic drift on TA-Lib upgrades.** TA-Lib C is actively changing numerical details: the bundled C 0.8.1 was released on 2026-09-12, and the upstream sources carry recent changes such as the scale-relative stochastic zero test and the Bollinger variance floor. A Dependabot bump that changes values would silently change signals. Mitigations: the locked version, golden and reference tests with tight tolerances (T7, T13), the lookback tests (T9) and the compatibility canary (T16) fail on such a bump. The Dependabot light path runs `scripts/check.py`, so a failure moves the bump to the full workflow.
- **Global state.** A library in the same process could change TA-Lib unstable periods. Mitigation: the §9 guard raises instead of returning shifted values.
- **Reproducibility.** Recursive indicators computed on moving windows shorter than `stable_warmup` differ slightly between runs, and `obv` levels always do. Mitigations: two documented warmups, fetch guidance for #8/#14, the `obv` signal line whose comparison with `value` does not depend on the history start (D3), the `obv` description and its required output.
- **Masks diverge from TA-Lib in corner cases.** The documented underflow limit (§7) and directional moves below 1e-16 of the true range. Neither occurs in realistic data; both are documented.
- **Supply chain.** `ta-lib` comes from PyPI (the `ta-lib` GitHub organization) with hashes pinned in `uv.lock`, and its only dependency is `numpy`. It replaces the rejected `pandas-ta`. The bundled native library adds attack surface that `gitleaks` does not cover; Dependabot security alerts do.
- **Image and runtime.** The arm64 image gains about 5.5 MB (extension and bundled library). `main.py` does not import the indicators yet, so startup and memory do not change in #5. The build-time smoke check adds a second or two to the Docker build. Pushing produces a minor beta.
- **Sensitive data.** None: synthetic data only; no tokens, hosts, IPs or users; no `TB_*` variables, migrations or `secrets.env` changes.
- **Unbreakable rules.** Signal-only (no order concepts), pure `domain/` (T12, T16), closed candles (the registry computes on the frame it receives; open-candle removal stays in #8), look-ahead tests for every indicator (T10, T14), idempotency (untouched), UTC (the index is passed through), single worker (untouched), no `eval` (declarative whitelist and constraints; `describe()` exports no formulas), English only: all preserved.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches (#3 → #4 → #5 → #6 → #7).
- **D2 (2026-09-14):** synthetic fixtures only in M1; recorded real-data fixtures deferred to #10.
- **D3 (2026-09-14): OBV gets a signal line.** The OBV level depends on the first candle of the fetched history, so comparing it with a fixed value gives different results as the window moves. Of the options offered (keep the single output with a caveat, add a signal line, or remove `obv`), the user chose the signal line: `obv` has a `signal` parameter (int, default 20, `[2, 500]`) and the outputs `value` (OBV) and `signal` (the simple moving average of OBV over `signal` candles). `value` compared with `signal`, including their crossovers, does not depend on the history start (§8). This deliberately extends the issue text, which listed `obv` with a single output. Affected: §0 (default output), §6.1, §6.2, §6.4, §8, §10, AC1, AC3, AC7, AC10, T9 and T15.
- **D4 (2026-09-14): undefined values are NaN.** Where `rsi`, `stoch` or `adx` are mathematically undefined on flat data, the registry returns NaN instead of TA-Lib's substituted `0`, so rules do not fire on flat data such as halted or illiquid tickers (§7, AC8).
- **D5 (2026-09-14): parameters approved.** The parameter names, defaults and ranges of §6.1 are approved as proposed: `length` as the common name, `macd` `fast`/`slow`/`signal`, `bbands` `std`, `stoch` `length`/`smooth_k`/`smooth_d`, `obv` `signal`; defaults 20 (`sma`, `ema`, `bbands`, `volume_sma`, `obv` signal), 14 (`rsi`, `atr`, `adx`, `stoch`), 12/26/9 (`macd`), 2.0 (`std`), 3/3 (`stoch` smoothing); maxima 500 for `sma`, `ema`, `bbands` and `volume_sma` lengths and the `obv` signal, 100 for `rsi`, `atr`, `adx` and `stoch` periods, 100/200/100 for `macd`; `std` from 0.1 to 5.0. Renaming later requires migrating stored rules.
- Earlier project decisions that apply: rules are JSON validated against a whitelist of indicators and operators, without `eval`; rules are defined from the dashboard; yfinance stocks and ETFs first; `pandas-ta` rejected for supply-chain risk in favor of TA-Lib.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria, and T1, T3, T7, T8, T11 and T13 fail without the implementation
- [ ] Every indicator passes the look-ahead harness with exact comparison (T10, T14)
- [ ] Runtime dependency diff is exactly AC17; no `pandas-ta`; `uv.lock` pins the aarch64 wheel
- [ ] No `Any` in the domain public API (V4); TA-Lib isolated and globals guarded (T12, T16)
- [ ] `Docker build (arm64)` green with the smoke check (AC18)
- [ ] Scope limited to Design §1
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
