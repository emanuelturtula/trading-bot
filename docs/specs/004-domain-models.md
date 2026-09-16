# 004 — Domain models: Timeframe, OHLCV candles and Signal

- **Status:** approved
- **Branch:** `feature/domain-models` (stacked on `feature/lookahead-harness`, #3, commit `cc44a8c`)
- **Spec author:** tech-lead
- **Issue:** #4 (milestone M1 · Analysis core; stacked series #3 → #4 → #5 → #6 → #7)
- **Expected commit type:** `feat:`. The change adds public runtime code in `src/trading_bot/domain/` (the first domain API, which #5–#22 build on) and moves `pandas`/`numpy` into the runtime dependency set, so the published image changes. `refactor:` or `test:` would hide a new capability and a runtime change; `build:` describes only the dependency move. `feat` → minor bump.

## Goal

Give the rest of the bot one pure, typed vocabulary for time and signals: a `Timeframe` with durations and a nominal candle close, a strict contract for OHLCV candle frames enforced by `validate_candles` with typed errors, and an immutable `Signal` whose idempotency key `(ticker, timeframe, rule_id, candle_close_ts)` is stable across restarts (`CLAUDE.md` rules 3, 5 and 6).

## Out of scope

- **Removing the open candle** (`drop_open_candle(candles, timeframe, now)`): #8 owns it (its issue says so), and it needs the real session closes of #9 (see Design §5). #4 adds no function that takes `now`.
- Market calendars, sessions, holidays and real candle closes (#9); `next_candle_close` (#9).
- Normalizing provider data into the contract: time zone conversion, column renaming, casting `volume` to `float64`, sorting, de-duplication and repairing inconsistent OHLC (#8). `validate_candles` checks and never coerces.
- 4h resampling (#10), ticker existence or exchange validation (#10), label conventions of yfinance bars (#8/#10, see Risks).
- The `Rule` model and JSON schema (#6), `Evaluation` (#7), the indicator registry and TA-Lib (#5).
- Persistence mapping of `Signal` (#13), Telegram formatting (#17), API schemas (#22).
- A per-row `Candle` class. The candle model is the validated `DataFrame` (Design §0).
- Timeframes other than `1h`, `4h` and `1d`. A `DEFAULT_TIMEFRAME` constant (#12 decides where the default `1d` lives).
- Changes to `src/trading_bot/main.py`, `config.py`, `logging_setup.py`, `scripts/`, `.github/`, `deploy/`, `Dockerfile`, `.claude/` or `CLAUDE.md`. No `TB_*` variables and no migrations.

## Acceptance criteria

### UTC helper (`domain/utc.py`)

- [ ] **AC1 (`to_utc`):**
  - for a tz-aware `datetime` (including `pd.Timestamp`), returns an object with `type(result) is datetime` and `result.tzinfo is datetime.UTC` that represents the same instant (`result == value.astimezone(UTC)`; tests must not assert `result == value`, which PEP 495 makes `False` for wall times in a DST fold);
  - wall times in a DST fold are accepted: `2024-11-03T01:30` in `America/New_York` gives `05:30:00+00:00` with `fold=0` and `06:30:00+00:00` with `fold=1`;
  - equal instants in different representations give equal results: stdlib UTC, a fixed offset such as `timezone(timedelta(hours=-5))`, `pd.Timestamp` in `UTC` and in `America/New_York`;
  - it is idempotent: `to_utc(to_utc(x)) == to_utc(x)`, same type and `tzinfo`;
  - a naive `datetime`, a naive `pd.Timestamp` and `pd.NaT` raise `ValueError`;
  - a `pd.Timestamp` with a non-zero `nanosecond` raises `ValueError` (the conversion must be lossless);
  - a non-`datetime` (`str`, `int`, `datetime.date`, `None`) raises `TypeError`.

### Timeframe (`domain/timeframe.py`)

- [ ] **AC2 (members and durations):** `Timeframe` is a `StrEnum` with exactly the members `H1 = "1h"`, `H4 = "4h"` and `D1 = "1d"`, in that order. `str(member)` is its code. `duration` is `timedelta(hours=1)`, `timedelta(hours=4)` and `timedelta(days=1)`.
- [ ] **AC3 (`parse`):**
  - `Timeframe.parse(code)` returns the member for each code, also with surrounding whitespace (`" 1d "`, `"4h\n"`), and returns a member unchanged when given one;
  - anything else raises `UnknownTimeframeError`, a subclass of `ValueError`: other letter cases (`"1H"`, `"1D"`), aliases (`"60m"`, `"240m"`, `"24h"`, `"1hour"`, `"daily"`, `"D"`), unsupported codes (`"15m"`, `"1w"`, `"1wk"`, `"1mo"`), `""`, `"   "` and inner whitespace (`"1 h"`);
  - the message contains `repr()` of at most the first 32 characters of the input and the list `1h, 4h, 1d`; a 10 000-character input produces a message shorter than 200 characters;
  - a non-`str` input (`None`, `1`, `b"1h"`) raises `TypeError`.
- [ ] **AC4 (`nominal_close`):**
  - `timeframe.nominal_close(open_time)` returns `to_utc(open_time) + timeframe.duration`, with `type(result) is datetime` and `tzinfo is datetime.UTC`;
  - golden cases: `D1` at `2024-01-02T05:00:00+00:00` → `2024-01-03T05:00:00+00:00`; `H1` at `2024-01-02T14:30:00+00:00` → `2024-01-02T15:30:00+00:00`; `H4` at `2024-01-02T18:30:00+00:00` → `2024-01-02T22:30:00+00:00`;
  - it applies no calendar: the `D1` nominal close of a Friday candle is on Saturday, and `H1` at `2024-03-10T06:30:00+00:00` (the US daylight saving change) still adds exactly one hour;
  - the same instant in different aware representations gives equal results; naive input raises `ValueError`.

### Candle frame contract (`domain/candles.py`)

- [ ] **AC5 (valid frames are accepted):** `validate_candles(frame)` returns the **same object** (`result is frame`) and does not modify it (`pd.testing.assert_frame_equal` against a deep copy taken before the call, with `check_exact=True`, plus equal `index.dtype`) for:
  - every `Scenario` × timeframe in `("1h", "4h", "1d")` × `n` in `(1, 2, 60, 250)` from `synthetic_candles`;
  - every `candle_frames()` draw (property test), and every prefix `frame.iloc[:n]` with `1 <= n <= len(frame)` of a drawn frame (prefix closure, property test);
  - an empty frame with the five `float64` columns and an empty UTC `DatetimeIndex`;
  - index units `s`, `ms`, `us` and `ns`; the time zone given as `"UTC"`, `datetime.timezone.utc` or `zoneinfo.ZoneInfo("UTC")`; index names `None`, `"Date"` and `"Datetime"`;
  - labels that are not on the UTC hour grid (a `1h` candle at `13:30:00+00:00`) and irregular spacing (nights, weekends, holidays);
  - zero volume and flat candles (`open == high == low == close`).
- [ ] **AC6 (invalid frames are rejected):** each case in Design §4 raises the stated subclass of `CandleValidationError` with the stated `kind`, `column`, `timestamp`, `position` and `count`. Row-level cases are built from a valid `synthetic_candles(20, seed=1)` frame with the defect at rows 3 and 7, so `position == 3`, `timestamp == frame.index[3]` (`None` for `NaT`) and `count == 2`, unless the table states other rows.
- [ ] **AC7 (error contract):**
  - `CandleValidationError` subclasses `ValueError`, and `CandleIndexError`, `CandleColumnsError` and `CandleValuesError` subclass it;
  - `str(error)` contains the kind value, the column when set, the first offending timestamp in ISO 8601 (`isoformat()`) when set, the position when set, and the count for row-level kinds;
  - checks run in the order of Design §4 and the first failing check raises. A frame with several defects reports the earliest check (for example a naive index with NaN values reports `timezone`; `high < low` at row 2 plus NaN at row 8 reports `missing_value`), and within one check the earliest offending row;
  - a non-`DataFrame` argument (`None`, a `Series`, a `dict`) raises `TypeError`, not `CandleValidationError`.
- [ ] **AC8 (constants):** `OHLCV_COLUMNS == ("open", "high", "low", "close", "volume")` and `PRICE_COLUMNS == ("open", "high", "low", "close")`.

### Signal (`domain/signals.py`)

- [ ] **AC9 (`Side`):** `Side` is a `StrEnum` with exactly `BUY = "BUY"` and `SELL = "SELL"`.
- [ ] **AC10 (`normalize_ticker`):**
  - it strips surrounding whitespace and upper-cases: `" aapl "` → `"AAPL"`, `"brk-b"` → `"BRK-B"`;
  - it accepts yfinance symbols unchanged apart from case: `AAPL`, `BRK-B`, `^GSPC`, `EURUSD=X`, `BTC-USD`, `RELIANCE.NS`, `M&M.NS`, `ES=F`;
  - it raises `ValueError` for `""`, whitespace only, inner whitespace or control characters (`"AA PL"`, `"AA\nPL"`, `"AA\tPL"`), `|`, non-ASCII characters (`"ÄAPL"`) and more than 32 characters after stripping. The message contains `repr()` of at most the first 32 characters of the input;
  - it raises `TypeError` for a non-`str`.
- [ ] **AC11 (`Signal` construction):**
  - `Signal` is a frozen, slotted, keyword-only dataclass with the fields of Design §6;
  - `ticker` goes through `normalize_ticker`, and `candle_close_ts` through `to_utc` (the stored value is a stdlib `datetime` in `datetime.UTC`);
  - `rule_id` is a `str` of 1–64 printable ASCII characters with no whitespace and no `|`, case preserved and not stripped (`" 42"` raises `ValueError`); a non-`str` (`42`) raises `TypeError`;
  - `timeframe` must be a `Timeframe` and `side` a `Side`; plain strings (`"1d"`, `"BUY"`) raise `TypeError`;
  - `close_price` must be a real number that is not a `bool` (`int`, `float`, `numpy.float64`, `numpy.int64` are accepted and stored as a built-in `float`); `bool`, `str`, `Decimal` and `None` raise `TypeError`; `0.0`, negative values, NaN and ±inf raise `ValueError`;
  - `indicator_values` defaults to empty and is always stored as an `IndicatorValues` copy that keeps insertion order. Keys must be non-empty `str` (`ValueError`, or `TypeError` for a non-`str` key); values follow the `close_price` type rules and must be finite (NaN or ±inf raise `ValueError`), and they are stored as built-in `float`.
- [ ] **AC12 (immutability and value semantics):**
  - assigning any `Signal` or `SignalKey` field raises `dataclasses.FrozenInstanceError`;
  - item assignment and deletion on `indicator_values` raise `TypeError`, and setting an attribute on an `IndicatorValues` raises `AttributeError`;
  - mutating the source `dict` after construction does not change the signal;
  - `dataclasses.replace(signal, ticker="msft")` validates and normalizes again (`ticker == "MSFT"`), and an invalid replacement raises;
  - `pickle` round-trips, `copy.deepcopy` and `dataclasses.asdict` work for `Signal`, `SignalKey` and `IndicatorValues`;
  - equal signals are equal and have equal hashes. `IndicatorValues` equality and hashing ignore insertion order (`{"a": 1.0, "b": 2.0}` equals `{"b": 2.0, "a": 1.0}`);
  - `tests.lookahead.values_equal` treats two equal signals as equal, which #7 needs.
- [ ] **AC13 (idempotency key):**
  - `signal.idempotency_key` returns `SignalKey(ticker=..., timeframe=..., rule_id=..., candle_close_ts=...)` built from the normalized fields;
  - **stable:** two calls return equal keys with equal hashes and equal `str()`. Signals built from equivalent inputs (`" aapl "` and `"AAPL"`; the same instant as stdlib UTC, fixed offset `-05:00`, `pd.Timestamp` in `UTC` and in `America/New_York`) have equal keys and equal `str()`. A Hypothesis property checks this over drawn symbols, letter cases, surrounding whitespace, rule ids, timeframes, instants between 1970 and 2100 and fixed offsets;
  - **scoped:** changing `side`, `close_price` or `indicator_values` keeps the key equal; changing any one of `ticker`, `timeframe`, `rule_id` or `candle_close_ts` (by one microsecond) makes it different;
  - **golden string form:** `str(key)` is `f"{ticker}|{timeframe}|{rule_id}|{candle_close_ts.isoformat()}"`. The test asserts literal strings: `"AAPL|1d|42|2024-01-03T05:00:00+00:00"` and `"BRK-B|4h|rule-7|2024-01-02T22:30:00+00:00"`;
  - a `SignalKey` built directly from raw values (`ticker="aapl"`, a `pd.Timestamp` in `America/New_York`) validates and normalizes exactly like `Signal` and equals the signal's key;
  - `SignalKey` never equals a plain `tuple` with the same values, and works as a `dict` key and a `set` member.

### Typing, purity, dependencies, fixtures, docs and gate

- [ ] **AC14 (typing):**
  - `uv run mypy` (strict) passes;
  - `uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated src/trading_bot/domain` passes (the tech-lead verified these flags on a prototype of Design §2–§6 with pandas-stubs);
  - `typing.Any` is neither imported nor used in `src/trading_bot/domain`: `git grep -n -E "import .*\bAny\b|\bAny\b[],)]|: Any\b|-> Any\b" src/trading_bot/domain` matches nothing (prose in docstrings does not count). There is no bare `# type: ignore` in the new or changed files;
  - every domain module except `__init__.py` defines `__all__` with exactly the public names of Design §2–§6.
- [ ] **AC15 (purity and import weight):**
  - modules under `src/trading_bot/domain/` import only `__future__`, `collections.abc`, `dataclasses`, `datetime`, `enum`, `math`, `numbers`, `types`, `typing`, `numpy`, `pandas` and `trading_bot.domain.*`;
  - they call no clock (`now`, `utcnow`, `today`, `time.time`), and module-level objects are immutable (`Final` constants, tuples, enums, functions and classes: no module-level `dict`, `list` or `set` other than `__all__`);
  - importing `trading_bot.domain.utc`, `trading_bot.domain.timeframe` or `trading_bot.domain.signals` in a fresh interpreter leaves `pandas` and `numpy` out of `sys.modules`. Consumers that only need `Signal` or `Timeframe` (Telegram, persistence, API) do not load pandas.
- [ ] **AC16 (dependencies):**
  - `pandas` and `numpy` move from the `dev` group to `[project.dependencies]` with `uv add pandas numpy` followed by `uv remove --dev pandas numpy`. This writes `"numpy>=2.5.3"` and `"pandas>=3.0.5"`, and the locked versions do not change;
  - `hypothesis` and `pandas-stubs` stay dev-only; no file adds `pandas-ta`/`pandas_ta`;
  - `uv export --no-dev --no-hashes --locked --no-emit-project` differs from the same command on `cc44a8c` **only** by these added entries: `numpy==2.5.3`, `pandas==3.0.5`, `python-dateutil==2.9.0.post0`, `six==1.17.0` and `tzdata==2026.4 ; sys_platform == 'emscripten' or sys_platform == 'win32'` (plus their `# via` comment lines). The tech-lead simulated this on a scratch copy of `pyproject.toml` and `uv.lock`.
- [ ] **AC17 (fixture hand-off and #3 review carry-over):**
  - **(a) Timeframe in fixtures:** `synthetic_candles(timeframe=...)` and `candle_frames(timeframes=...)` accept `Timeframe` members as well as the existing codes. `tests/fixtures/candles.py`, `tests/fixtures/strategies.py` and the generator tests derive durations from `Timeframe.duration`: the private tables `_HOURS_PER_SLOT`, `_DURATIONS` and `DURATIONS` are gone;
  - **(b) shared assertions:** candle assertion helpers live in `tests/fixtures/candle_assertions.py`, and its `assert_valid_candles(candles, timeframe)` calls `validate_candles` and then checks the generator-only invariants (grid alignment and price bounds). Helpers raise `AssertionError` with a message explicitly (no bare `assert`, which pytest does not rewrite outside test modules). `git grep -n "from tests.unit" tests` matches nothing;
  - **(c) EXTREME overclaim:** the property test over drawn parameters (`test_scenarios_are_restricted_to_extreme_values`) asserts only parameter-independent features (a volume `>= 1e12` and a zero volume). The price-ratio features stay asserted by the fixed-seed tests at default parameters. The docstring of `synthetic_candles` and the fixtures paragraph of `docs/ARCHITECTURE.md` state the assumption (wording in Design §8). Regression check: `synthetic_candles(112, seed=3370146904, scenario=Scenario.EXTREME, start_price=1e-3, volatility=0.25)` has no `close <= 0.1 * open` candle today, and no test asserts that it has one;
  - **(d) sampling-coupled assertion:** `test_lookahead_harness_adversarial.py` no longer asserts that the narrow-window cheat **passes** with default arguments. It keeps the full-sweep and explicit-cut assertions, and the docstring may still explain the sampling limit.
- [ ] **AC18 (docs):**
  - in `docs/ARCHITECTURE.md`, the `domain/` row of the Layers table lists the models of this spec;
  - a new `## Domain models` section right before `## Rule model` covers Design §8;
  - the fixtures paragraph under `## Look-ahead testing` is corrected (AC17c);
  - in `docs/ROADMAP.md`, F1 says "domain models" instead of `domain/models.py`;
  - Python blocks pass `ruff format --check`.
- [ ] **AC19 (gate and budget):**
  - `uv run python scripts/check.py` is green;
  - every module under `src/trading_bot/domain/` has 100% line and branch coverage;
  - the new tests need no network and add at most **5 s** to `pytest`, with no single new test above **2 s** (evidence: `--durations`).
- [ ] **AC20 (scope):** only the files of Design §1 change (compared with `cc44a8c`), plus this spec.

## Design

### 0. Decisions

| Topic | Decision | Why |
|-------|----------|-----|
| Module layout | `domain/utc.py`, `domain/timeframe.py`, `domain/candles.py`, `domain/signals.py`; `domain/__init__.py` has only a docstring (no re-exports) | #5 adds `indicators/` and #6/#7 add `rules/`; one `models.py` would mix a pandas-heavy validator with lightweight value types. Importing from submodules keeps pandas out of consumers that only need `Signal`/`Timeframe` (AC15) and avoids import cycles once subpackages import these modules. `signals.py` (plural) avoids a module named like the stdlib `signal`. |
| Candle model | The validated `pd.DataFrame` is the candle model. No `Candle` row class and no `NewType` | Indicators, rules and the harness work on frames. A `NewType("Candles", DataFrame)` would be lost on every slice (`iloc[:t]`) and would not fit the harness `Callable[[pd.DataFrame], object]` signatures. |
| `Timeframe` | `StrEnum` with codes as values; `parse` strips whitespace only and matches the exact case; no aliases | One canonical spelling everywhere (rule JSON through pydantic, API, Telegram, CLI and database). Pydantic already rejects `"1H"` for a `StrEnum` field (verified), so a lenient `parse` would make the text commands and the API disagree. Case folding would also become ambiguous once minute and month codes exist (`1m`/`1M`). The error lists the valid codes, so the stricter parsing costs users little. |
| Candle close | `nominal_close(open) = open + duration` in UTC, used as an **identifier** only | Pure, calendar-free and deterministic from the provider label. §5 lists its limits and what later issues must do because of them. |
| Closed candles | No helper in #4 | #8 owns `drop_open_candle` with an injected `now`, and it must use real session closes (#9), not nominal ones. |
| Candle contract | Strict: exactly the five columns, all `float64`, UTC index, finite values, positive prices, OHLC consistency; any index unit or name; empty frames valid | One canonical shape for #5–#7. TA-Lib needs `float64`. Coercion and repair belong to #8 (it logs and decides), not to a validator. |
| Validator result | Returns the same object, never copies or mutates | Allows `candles = validate_candles(normalize(raw))` at no cost. The harness checks input mutation. |
| Typed errors | `CandleValidationError(ValueError)` with a `kind` enum, and 3 category subclasses | `kind` gives exhaustive tables and structured logs (the `Violation.kind` pattern of #3). The subclasses allow precise `except` clauses. `ValueError` lets pydantic validators in #22 wrap it. |
| `Signal` | Frozen slotted keyword-only dataclass with `__post_init__` validation, not a pydantic model | Without the pydantic mypy plugin, `BaseModel.__init__(**data: Any)` puts `Any` in the public API (issue criterion). A stdlib dataclass keeps the domain free of serialization concerns; #13/#22 map to their own schemas. |
| `indicator_values` | `IndicatorValues`: a read-only, hashable, picklable `Mapping[str, float]` of finite floats | `MappingProxyType` cannot be pickled or deep-copied (verified), so `dataclasses.asdict(signal)` would raise. Finite values keep `==` reflexive (NaN breaks it), serialize to strict JSON in #13, and matter for display. #7 omits missing values instead of storing NaN. |
| `ticker` | `normalize_ticker`: strip, upper-case, 1–32 printable ASCII, no whitespace or `|` | Equal symbols must give equal keys. Yahoo symbols are ASCII and case-insensitive. The charset blocks log and message injection without rejecting `^GSPC`, `EURUSD=X` or `M&M.NS`. Existence checks are #10. |
| `rule_id` | Opaque `str` (1–64 printable ASCII, no whitespace or `|`), case-sensitive | The domain does not depend on the database key type. #13 maps `str(rules.id)`, and previews or backtests can use ids that are not from the database. |
| `candle_close_ts` | Any tz-aware `datetime` accepted and normalized with `to_utc`; naive rejected | An aware value is an unambiguous instant, so converting it is lossless and equal instants give equal keys. A naive value is ambiguous (rule 6). |
| Key type | `SignalKey`: a frozen slotted dataclass with a canonical, injective `str()` | Rule 5 names four fields. A dataclass does not compare equal to arbitrary tuples. `str()` is stable across processes and versions, while `hash()` is not (`PYTHONHASHSEED`), so it serves logs and tests. `|` cannot appear in any field. |
| Fixture validation | The generator does not call `validate_candles`; tests do | If the validator had a bug, it would show up in the validator tests instead of breaking every harness test. |

### 1. Files and owners

| File | Owner | Change |
|------|-------|--------|
| `src/trading_bot/domain/__init__.py` | developer | Package docstring: pure layer, no I/O, clock, globals or mutable state. No re-exports |
| `src/trading_bot/domain/utc.py` | developer | `to_utc` (§2) |
| `src/trading_bot/domain/timeframe.py` | developer | `Timeframe`, `UnknownTimeframeError` (§3) |
| `src/trading_bot/domain/candles.py` | developer | Contract constants, error types, `validate_candles` (§4) |
| `src/trading_bot/domain/signals.py` | developer | `Side`, `normalize_ticker`, `IndicatorValues`, `SignalKey`, `Signal` (§6) |
| `tests/unit/test_utc.py` | developer | TDD, AC1 |
| `tests/unit/test_timeframe.py` | developer | TDD, AC2–AC4 |
| `tests/unit/test_candle_validation.py` | developer | TDD, AC5–AC8 |
| `tests/unit/test_signals.py` | developer | TDD, AC9–AC13 |
| `tests/fixtures/candle_assertions.py` | developer | New shared helpers (AC17b) |
| `tests/fixtures/candles.py` | developer | Accept `Timeframe`, durations from `Timeframe`, docstring (AC17a, AC17c) |
| `tests/fixtures/strategies.py` | developer | Accept `Timeframe`, durations from `Timeframe` (AC17a) |
| `tests/unit/test_synthetic_candles.py` | developer | Use the shared helpers; a case with `Timeframe` members (AC17a, AC17b) |
| `tests/unit/test_candle_strategies.py` | developer | Helpers from fixtures; EXTREME property restricted; `validate_candles` on draws (AC5, AC17b, AC17c) |
| `pyproject.toml`, `uv.lock` | developer | Dependency move (AC16) |
| `docs/ARCHITECTURE.md` | developer | Domain row, `## Domain models`, fixtures paragraph (AC18); authorized by this spec |
| `docs/ROADMAP.md` | developer | F1 wording only (AC18); authorized by this spec |
| `tests/unit/test_lookahead_harness_adversarial.py` | tester | AC17d |
| `tests/unit/test_domain_models_adversarial.py` | tester | Test plan T12–T14 |
| `tests/unit/test_domain_purity.py` | tester | Test plan T15 (AC15) |
| `docs/specs/004-domain-models.md` | tech-lead | This spec |

`[tool.mypy] files` already covers `src` and `tests/fixtures`, so it does not change. There are no new Protocols, no migrations and no `TB_*` variables.

### 2. `domain/utc.py`

```python
from datetime import datetime

__all__ = ["to_utc"]


def to_utc(value: datetime) -> datetime: ...
```

- Check `isinstance(value, datetime)` first (`TypeError`), then `value.tzinfo is None or value.utcoffset() is None` (`ValueError`). Check `tzinfo` before calling `utcoffset()`: `pd.NaT` has `tzinfo None` and raises from `utcoffset()`.
- Convert with `converted = value.astimezone(UTC)` and rebuild a stdlib `datetime(year, …, microsecond, tzinfo=UTC)` from it. If the rebuilt value does not equal **`converted`**, the input had sub-microsecond precision → `ValueError`. pandas compares with nanosecond precision (verified), so no pandas import is needed.
- **Compare with `converted`, never with `value`.** PEP 495 makes an inter-zone `==` return `False` for wall times in a DST fold. The tech-lead verified that comparing with `value` wrongly rejects `2024-11-03T01:30` in `America/New_York` for both `fold=0` and `fold=1`, while comparing with `converted` returns `05:30Z` and `06:30Z` and still rejects nanoseconds.

### 3. `domain/timeframe.py`

```python
from datetime import datetime, timedelta
from enum import StrEnum

__all__ = ["Timeframe", "UnknownTimeframeError"]


class UnknownTimeframeError(ValueError): ...


class Timeframe(StrEnum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @classmethod
    def parse(cls, value: str) -> Timeframe: ...

    @property
    def duration(self) -> timedelta: ...

    def nominal_close(self, open_time: datetime) -> datetime: ...
```

- `Timeframe("1d")` (the enum constructor) stays exact. `parse` is the text entry point for Telegram and CLI input (§0).
- `duration` has no module-level mutable table: use a `match` statement or an immutable mapping.
- For whole frames, #7 uses `candles.index + timeframe.duration` (a vectorized nominal close); no helper is added for it.

### 4. `domain/candles.py`

```python
from enum import StrEnum
from typing import Final

import pandas as pd

__all__ = [
    "OHLCV_COLUMNS",
    "PRICE_COLUMNS",
    "CandleColumnsError",
    "CandleErrorKind",
    "CandleIndexError",
    "CandleValidationError",
    "CandleValuesError",
    "validate_candles",
]

OHLCV_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")


class CandleErrorKind(StrEnum):
    INDEX_TYPE = "index_type"
    TIMEZONE = "timezone"
    MISSING_TIMESTAMP = "missing_timestamp"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    UNSORTED_INDEX = "unsorted_index"
    COLUMNS = "columns"
    DTYPE = "dtype"
    MISSING_VALUE = "missing_value"
    INFINITE_VALUE = "infinite_value"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    HIGH_BELOW_LOW = "high_below_low"
    BODY_OUTSIDE_RANGE = "body_outside_range"


class CandleValidationError(ValueError):
    kind: CandleErrorKind
    column: str | None  # offending column, when the kind is about one column
    timestamp: pd.Timestamp | None  # first offending label, when there is one
    position: int | None  # 0-based row position of the first offender
    count: int  # offending rows for row-level kinds; 0 for frame-level kinds

    def __init__(
        self,
        kind: CandleErrorKind,
        message: str,
        *,
        column: str | None = None,
        timestamp: pd.Timestamp | None = None,
        position: int | None = None,
        count: int = 0,
    ) -> None: ...


class CandleIndexError(CandleValidationError): ...


class CandleColumnsError(CandleValidationError): ...


class CandleValuesError(CandleValidationError): ...


def validate_candles(candles: pd.DataFrame) -> pd.DataFrame: ...
```

**The contract.** A candle frame is a `pd.DataFrame` where:

- the index is a `pd.DatetimeIndex` of candle **open** times, tz-aware in UTC, with no `NaT`, unique and strictly increasing. "UTC" means what pandas treats as UTC: `index.dtype == pd.DatetimeTZDtype(unit=index.unit, tz="UTC")`, which accepts `"UTC"`, `datetime.timezone.utc` and `ZoneInfo("UTC")` and rejects zero-offset aliases such as `Etc/UTC`, `GMT` and `Europe/London` (verified on pandas 3.0.5). #8 converts with `tz_convert("UTC")`. Any unit (`s`/`ms`/`us`/`ns`) and any index name are accepted;
- the column labels are exactly `OHLCV_COLUMNS` in that order: no missing, extra, duplicated or renamed columns, and no `MultiIndex` (the yfinance multi-ticker shape);
- every column has the numpy `float64` dtype (not `float32`, `int64`, `object` or the nullable `Float64`). #8 casts integer volume, which is exact below 2^53;
- no value is NaN or ±inf; `open, high, low, close > 0`; `volume >= 0`; `high >= low`; and `low <= open <= high` and `low <= close <= high`;
- an empty frame that meets the structural rules is valid. Minimum lengths are the caller's policy (warmup in #5–#7, "no data" in #8).

The validator does **not** check grid alignment (real `1h` bars open at `:30`), gaps (sessions, weekends, holidays and halts are legitimate), whether the last candle is closed (#8), or the index `freq`. Every check is row-local or compares adjacent labels, so **every prefix of a valid frame is valid** (AC5). Domain entry points can therefore validate the `data[:t]` prefixes that the harness and the evaluator pass them.

**Checks, in order.** The first failing check raises. "First" means the smallest row position. For multi-column value checks, `column` is the first offending column in `OHLCV_COLUMNS` order at that row, and `count` is the number of rows with at least one offending cell.

| # | Kind | Subclass | Fails when | `column` | `timestamp` / `position` | `count` | AC6 cases (at least) |
|---|------|----------|------------|----------|--------------------------|---------|----------------------|
| 1 | `index_type` | `CandleIndexError` | the index is not a `pd.DatetimeIndex` | `None` | `None` / `None` | 0 | `RangeIndex`; an index of ISO strings; `PeriodIndex` |
| 2 | `timezone` | `CandleIndexError` | the index is naive or not UTC (the message names the zone found) | `None` | `None` / `None` | 0 | naive; `America/New_York`; `Etc/UTC`; `Europe/London`; fixed `+01:00` |
| 3 | `missing_timestamp` | `CandleIndexError` | a label is `NaT` | `None` | `None` / first `NaT` | NaT labels | `NaT` at rows 3 and 7 |
| 4 | `duplicate_timestamp` | `CandleIndexError` | a label equals an earlier label | `None` | the repeated label / position of its second occurrence | rows repeating an earlier label | row 4 equal to row 3 (adjacent), `position == 4`; a non-adjacent repeat |
| 5 | `unsorted_index` | `CandleIndexError` | a label is earlier than the previous one | `None` | that label / its position | such positions | rows 5 and 6 swapped: `position == 6`, `count == 1` |
| 6 | `columns` | `CandleColumnsError` | the labels are not exactly `OHLCV_COLUMNS` in order (the message shows expected and found labels, truncated) | `None` | `None` / `None` | 0 | missing `volume`; extra `adj_close`; reordered; `Open`; duplicated `close`; `MultiIndex`; no columns |
| 7 | `dtype` | `CandleColumnsError` | a column is not numpy `float64` (the message names the dtype) | first such column | `None` / `None` | 0 | `volume` `int64`; `close` `float32`; `open` `object`; `high` `Float64` |
| 8 | `missing_value` | `CandleValuesError` | a cell is NaN | yes | first row | rows | NaN `close` at rows 3 and 7; NaN in `volume` only; NaN in `low` and `open` at the same row → `open` |
| 9 | `infinite_value` | `CandleValuesError` | a cell is ±inf | yes | first row | rows | `+inf` `high`; `-inf` `low`; `inf` `volume` |
| 10 | `non_positive_price` | `CandleValuesError` | a price is `<= 0` | yes | first row | rows | `0.0` `open`; `-1.0` `close`; `-0.0` `low` |
| 11 | `negative_volume` | `CandleValuesError` | `volume < 0` | `"volume"` | first row | rows | `-1.0` at rows 3 and 7 |
| 12 | `high_below_low` | `CandleValuesError` | `high < low` | `None` | first row | rows | high and low swapped at rows 3 and 7 |
| 13 | `body_outside_range` | `CandleValuesError` | `open` or `close` is outside `[low, high]` | `"open"` or `"close"` | first row | rows | `open > high` at row 3 and `close < low` at row 7 → `"open"`, `count == 2` |

- The implementation is vectorized (numpy/pandas over whole columns, no Python loop per row). Column access happens only after checks 6–7 pass.
- Messages are built from kinds, column names, positions, counts and ISO timestamps; for `columns` and `timezone` they use truncated `repr()` of labels and zones. They never include a whole frame.

### 5. Nominal close: meaning and limits

**Decision.** `candle_close_ts` is `timeframe.nominal_close(open_time)`, where `open_time` is the index label (the candle open, as yfinance and the synthetic generator label bars). It is an **identifier**, not a market fact.

Rejected alternatives:

- **Real close from a calendar.** It is not available until #9, it is not pure without calendar data, and it changes if a calendar library update corrects a half day. The keys of stored signals would then change and signals could be sent again.
- **Open time as the identifier.** It contradicts the field named in `CLAUDE.md` rule 5, ARCHITECTURE and the issue, and it gains nothing: for a fixed timeframe the nominal close maps one-to-one to the open time.

Limits for US equities (regular session 09:30–16:00 ET):

| Timeframe | Bar label (yfinance) | Real close | Nominal close |
|-----------|----------------------|------------|---------------|
| `1h` | 09:30, 10:30, …, 15:30 ET | open + 1h, except the last bar: 16:00 ET (13:00 ET on half days) | always open + 1h: 16:30 ET for the last bar |
| `4h` (#10 resample, session-aligned) | 09:30 and 13:30 ET | 13:30 and 16:00 ET | 13:30 and 17:30 ET |
| `1d` | 00:00 ET of the session date (05:00 or 04:00 UTC) | 16:00 ET the same day | 00:00 ET the **next** day |

What later issues must do because of this:

- **#8 / #9 / #15:** never use `nominal_close` to decide whether a candle is closed or to schedule a run. With nominal closes the daily candle would be dropped as "open" until about 05:00 UTC the next day and the last hourly bar would be missed. Use the real session closes of #9.
- **#8 / #10:** keys depend on provider labels. Fix the label convention (open times; the `1d` label policy) before #13 persists signals. Changing it later requires migrating the stored keys, otherwise signals that were already notified can be sent again.
- **#7 / #14:** count candles by row position (`cooldown_bars`, crossovers), never with `(t2 - t1) / duration`, which is wrong across nights, weekends and holidays.
- **#17 / #25:** do not present the nominal close as the market close. For `1d` it is midnight of the next day. Show the session date or the calendar close.

### 6. `domain/signals.py`

```python
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trading_bot.domain.timeframe import Timeframe

__all__ = ["IndicatorValues", "Side", "Signal", "SignalKey", "normalize_ticker"]


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def normalize_ticker(value: str) -> str: ...


class IndicatorValues(Mapping[str, float]):
    """Read-only, hashable, picklable mapping of indicator names to finite floats."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, float] | None = None) -> None: ...
    def __getitem__(self, key: str) -> float: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...
    def __hash__(self) -> int: ...  # hash(frozenset(items)): consistent with Mapping equality
    def __setattr__(self, name: str, value: object) -> None: ...  # always AttributeError
    def __delattr__(self, name: str) -> None: ...  # always AttributeError
    def __reduce__(self) -> tuple[type[IndicatorValues], tuple[dict[str, float]]]: ...
    def __repr__(self) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalKey:
    ticker: str
    timeframe: Timeframe
    rule_id: str
    candle_close_ts: datetime

    def __post_init__(self) -> None: ...  # validates and normalizes (AC10, AC11)
    def __str__(self) -> str: ...  # "AAPL|1d|42|2024-01-03T05:00:00+00:00"


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal:
    ticker: str
    timeframe: Timeframe
    rule_id: str
    side: Side
    candle_close_ts: datetime
    close_price: float
    indicator_values: Mapping[str, float] = field(default_factory=IndicatorValues)

    def __post_init__(self) -> None: ...

    @property
    def idempotency_key(self) -> SignalKey: ...
```

- `__post_init__` writes normalized values with `object.__setattr__`. `Signal` validates and normalizes `ticker`, `timeframe`, `rule_id` and `candle_close_ts` with the **same** private helpers as `SignalKey`, so both always agree (AC13). The tech-lead verified this pattern (frozen, slots, kw-only, `object.__setattr__`, `IndicatorValues`) with strict mypy and the AC14 flags, and checked hashing, `pickle`, `deepcopy`, `asdict`, `replace` and `values_equal` at runtime.
- The `indicator_values` annotation stays `Mapping[str, float]`, so callers pass a `dict`. The stored value is always an `IndicatorValues`.
- Signals compare by value (all fields). Identity for idempotency is `idempotency_key` only.
- `hash(key)` is process-specific. #13 persists the key **fields** (or `str(key)`), never `hash(key)`.
- `signals.py` imports neither pandas nor numpy (AC15). Real numbers are recognized through `numbers.Real` minus `bool`: `numpy.float64` and `numpy.int64` are `numbers.Real`, and `numpy.bool_` is not (verified).

### 7. Fixtures and #3 carry-over

- **`tests/fixtures/candles.py`:** `timeframe: Timeframe | TimeframeCode`. Keep `TimeframeCode` and `TIMEFRAMES` for compatibility, derive slot hours and grid durations from `Timeframe(timeframe).duration`, and keep the `ValueError` for unknown codes. Update the docstring (§8).
- **`tests/fixtures/strategies.py`:** `timeframes: Sequence[Timeframe | TimeframeCode]`, with durations from `Timeframe`.
- **`tests/fixtures/candle_assertions.py`** (new, type-checked). It contains `assert_valid_candles(candles, timeframe)` (`validate_candles` plus grid alignment and price bounds), `gap_positions`, `assert_has_gap_features`, `assert_has_flat_run`, `assert_has_extreme_volume_features` (volume spike and zero volume) and `assert_has_extreme_features` (the volume features plus the −90%/+900% candles). All of them raise `AssertionError` explicitly.
- **`tests/unit/test_candle_strategies.py`:** import from `tests.fixtures.candle_assertions`. `test_scenarios_are_restricted_to_extreme_values` uses `assert_has_extreme_volume_features`. Add `@given` tests asserting that `validate_candles(frame) is frame` for draws and for their prefixes (AC5).
- **`tests/unit/test_synthetic_candles.py`:** import the helpers, remove `DURATIONS`, and add one validity case passing `Timeframe` members.
- **`tests/unit/test_lookahead_harness_adversarial.py`** (tester): in `test_narrow_window_cheat_escapes_default_sampling_but_full_sweep_catches_it`, remove the default-arguments `assert_no_lookahead` call and its assertion, and rename the test (for example `test_narrow_window_cheat_is_caught_by_full_sweep_and_explicit_cut`).

### 8. `docs/ARCHITECTURE.md` and `docs/ROADMAP.md`

- **Layers table, `domain/` row:** "Models (`Timeframe`, the candle frame contract with `validate_candles`, `Signal`/`SignalKey`/`Side`; `Rule` in #6), indicator registry (whitelist → TA-Lib), rule evaluator".
- **New `## Domain models` section** before `## Rule model`, short, with:
  1. timeframe codes, strict parsing and durations;
  2. the candle frame contract as a compact list, and the statement that #8 normalizes and the validator never coerces;
  3. the nominal close decision, the §5 table and its four obligations;
  4. signal identity: the key fields, the string form, normalization of ticker and time, and "never persist `hash()`".
- **Fixtures paragraph:** replace "Scenario features are guaranteed from `FEATURE_MIN_CANDLES` (60) candles on." with: "From `FEATURE_MIN_CANDLES` (60) candles on, the gap timestamps, flat runs, volume spike and zero-volume candles are guaranteed for any parameters. The price features (the −90% and +900% candles and the price jump after a gap) also need prices away from the `[1e-6, 1e9]` clipping bounds and, for the jump, `volatility > 0`. Fixed-seed tests check them at the default `start_price` and `volatility`, and property tests over drawn parameters assert only the parameter-independent features." Mirror this in the `synthetic_candles` docstring. The tech-lead reproduced both overclaims: the crash is missing for `n=112, seed=3370146904, start_price=1e-3, volatility=0.25`, and `GAPS` has no price jump at `volatility=0.0`.
- **`docs/ROADMAP.md` F1 scope:** "`domain/models.py`" → "domain models".

### 9. Forward compatibility (#5–#25)

| Issue | How it uses #4 |
|-------|----------------|
| #5 | May call `validate_candles` at the top of `registry.compute` (prefix closure keeps the harness prefixes valid). Inputs are `float64` by contract, as TA-Lib requires. |
| #6 | `timeframe: Timeframe` and `signal: Side` as pydantic field types (exact codes). |
| #7 | `Evaluation.candle_close_ts = rule.timeframe.nominal_close(candles.index[-1])`; the over-N-candles reference uses `candles.index + duration` but stays indexed by open time (spec 003 §7). Only finite indicator values go into `Signal`. Crossovers and cooldown work by position. |
| #8 | Normalizes to the contract, then calls `validate_candles` and wraps `CandleValidationError` in its typed provider error (logging `kind`, `column`, `timestamp`). `drop_open_candle(candles, timeframe, now)` uses `to_utc(now)` and #9 real closes. |
| #9 | `next_candle_close(timeframe, now)` returns real closes; `to_utc` for inputs. |
| #10 | Fixes and documents the label convention (§5) and the repair policy for inconsistent OHLC in recorded fixtures. |
| #12 | Stores symbols through `normalize_ticker`; `Timeframe.parse` for CLI input; the default timeframe is `Timeframe.D1`. |
| #13 | Maps `SignalKey` to the unique `(ticker_id, rule_id, timeframe, candle_close_ts)`: symbol → `ticker_id`, `rule_id` ↔ `str(rules.id)`, UTC timestamps, `indicator_values` as strict JSON. |
| #14 | Builds `Signal`, de-duplicates by `idempotency_key`, logs `str(key)`. |
| #15 | Schedules at #9 real closes plus the delay, never at nominal closes. |
| #17, #25 | Show times in `TB_DISPLAY_TIMEZONE`; for `1d` the session date, not the nominal close. |
| #19, #22 | `Timeframe.parse` for text commands; exact codes in JSON. `UnknownTimeframeError` and `normalize_ticker` messages are safe to echo (truncated `repr()`). |

## Test plan

All tests are unit tests, without network. Mandatory template cases:

- **Idempotency:** T9–T10 (key stability, scope, golden string form, direct `SignalKey` construction).
- **Anti look-ahead:** the harness does **not** apply to #4. `validate_candles` returns its input or raises (there is no per-candle output), and `to_utc`, `nominal_close` and `Signal` are scalar. The rule 4 properties that do apply are tested instead: prefix closure of the contract (T4) and `nominal_close` depending only on the open time it receives (T2).
- **Authorization** and **secret redaction:** N/A. There is no Telegram, API, configuration or logging change, and no secret is involved. Error messages that echo user-controlled text are bounded and escaped (T11, T14).

| Case | Type | What it verifies | AC | Owner |
|------|------|------------------|----|-------|
| T1 `to_utc` | unit | Aware conversions and type, idempotence, naive/`NaT`/nanosecond → `ValueError`, non-datetime → `TypeError` | AC1 | developer |
| T2 timeframe | unit | Members, order, `str`, durations; `parse` valid/invalid table, message bounds, `TypeError`; `nominal_close` golden cases, DST and Friday cases, representations, naive | AC2–AC4 | developer |
| T3 validator accepts | unit | Scenario × timeframe × size matrix; empty frame; units; UTC spellings; index names; `:30` labels; zero volume; flat candles; identity and no mutation | AC5, AC8 | developer |
| T4 validator properties | unit (`@given`) | Every `candle_frames()` draw and every prefix is valid and returned by identity | AC5 | developer |
| T5 validator rejects | unit | Parametrized over every row of the §4 table: subclass, kind, column, timestamp, position, count | AC6 | developer |
| T6 error contract | unit | `ValueError` hierarchy, message elements, check order with several defects, `TypeError` for non-frames | AC7 | developer |
| T7 ticker and ids | unit | `normalize_ticker` valid symbols and invalid table; `rule_id` rules; wrong enum types | AC10, AC11 | developer |
| T8 signal values | unit | `close_price` and `indicator_values` type/finite rules, float conversion, defensive copy, insertion order | AC11 | developer |
| T9 immutability | unit | Frozen fields, read-only mapping, `replace` re-validates, `pickle`/`deepcopy`/`asdict`, value equality and hashing, `values_equal` | AC12 | developer |
| T10 idempotency key | unit + `@given` | Stability across representations (property with fixed offsets, no tz database needed), scope, one-field sensitivity, golden strings, direct `SignalKey`, not equal to a tuple, `dict`/`set` use | AC13 | developer |
| T11 fixtures and carry-over | unit | `Timeframe` members accepted by the generator and strategy; shared helpers; EXTREME property restricted; no `from tests.unit` imports | AC17a–c | developer |
| T12 adversarial candles | unit | Accepted: `-0.0` volume; subnormal positive prices (`5e-324`); a `DatetimeIndex` with `freq` set; a frame with `attrs` set (returned by identity). Rejected: a `bool` column (`dtype`); NaN and inf in the same row (`missing_value`); a 100 000-row frame whose only defect is in the last row (reports that row and `count == 1`) | AC5–AC7 | tester |
| T13 adversarial signals | unit | Rejected: full-width letters (`Ａ`) and zero-width characters in tickers; a 33-character ticker; a 65-character `rule_id`. Accepted: `"aapl\n"` (stripped to `AAPL`); 32-character tickers; 64-character `rule_id`; `IndicatorValues` with a `numpy.float32` value and 10 000 entries; a `datetime` subclass other than `pd.Timestamp` (stored as a plain `datetime`). The ambiguous wall time `2024-11-03T01:30` in `America/New_York` with `fold=0` and `fold=1` gives two different UTC instants and two different keys | AC1, AC10–AC13 | tester |
| T14 message safety | unit | Inputs with newlines, ANSI escapes and 10 000 characters in `Timeframe.parse`, `normalize_ticker` and `rule_id` produce single-line, bounded messages (`"\n" not in str(error)`, length < 200) | AC3, AC10, AC11 | tester |
| T15 purity guard | unit | AST scan of `src/trading_bot/domain/**/*.py`: import allowlist, no clock calls, no module-level `dict`/`list`/`set` values other than `__all__`; fresh-interpreter import leaves `pandas`/`numpy` unloaded for `utc`, `timeframe` and `signals` | AC15 | tester |
| T16 carry-over (d) | unit | Narrow-window cheat test keeps only the full-sweep and explicit-cut assertions | AC17d | tester |

Verification evidence (the tester reports each; the tech-lead re-checks in review):

| Check | Command or method | AC |
|-------|-------------------|----|
| V1 gate | `uv run python scripts/check.py` green | AC19 |
| V2 coverage | `uv run pytest tests/unit --cov=trading_bot.domain --cov-branch --cov-report=term-missing`: 100% for every domain module | AC19 |
| V3 budget | `uv run pytest --durations=30 -q`: new tests total ≤ 5 s, none above 2 s | AC19 |
| V4 typing | `uv run mypy`; `uv run mypy --disallow-any-explicit --disallow-any-unimported --disallow-any-decorated src/trading_bot/domain`; the AC14 `git grep` for `Any` empty; `git grep -n "type: ignore"` on changed files shows only coded ignores | AC14 |
| V5 runtime dependencies | `uv export --no-dev --no-hashes --locked --no-emit-project` on the branch compared with the same command in a temporary `git worktree` of `cc44a8c` (in the scratch directory, removed afterwards): only the AC16 additions | AC16 |
| V6 runtime-only import | `UV_PROJECT_ENVIRONMENT=<scratch>/venv uv sync --locked --no-dev`, then that interpreter runs `import trading_bot.domain.candles, trading_bot.domain.signals, trading_bot.domain.timeframe, trading_bot.domain.utc`: the domain imports with runtime dependencies only (no `pandas-stubs`, `hypothesis`). Do not use `uv run --no-dev` on the project environment: it would uninstall the dev group | AC16 |
| V7 no `pandas-ta` | `git grep -n -i -E "pandas[-_]ta\b"` matches only prohibition notes in Markdown | AC16 |
| V8 docs | Section placement; `uv run ruff format --check docs/ARCHITECTURE.md`; manual read against §8 | AC18 |
| V9 scope | `git diff cc44a8c --name-only` plus `git status --porcelain` list only the §1 files. After #3 merges and the branch is rebased, compare with `origin/main` | AC20 |
| V10 secrets and language | `python scripts/secret_scan.py --history`; English-only review of the diff | — |
| V11 Docker | **BLOCKED locally** (the daemon is not running), and this change **does** alter the image (new runtime wheels). V6 is the local substitute. The authoritative checks are the `Docker build (arm64)` job on the PR and the beta deploy with `/health` verified by the lead. Report it as BLOCKED with this justification, not as PASS | — |

**TDD order suggested to the developer:** dependency move (AC16, an isolated lock diff) → T1 → T2 → T5/T6 (checks 1–7, then 8–13) → T3/T4 → T7 → T8 → T9 → T10 → T11 (fixtures and carry-over) → docs.

## Risks and security

- **Nominal close misuse.** Using `nominal_close` to decide closedness or to schedule would delay daily signals by hours and miss the last hourly bar. Mitigations: the explicit method name, §5 and the ARCHITECTURE section, and the obligations listed for #8/#9/#15.
- **Label convention drift.** Keys are derived from provider labels; if #8/#10 change how bars are labelled after #13 persists signals, already-notified signals can be sent again. Mitigation: #8/#10 fix the convention first, and any later change ships with a key migration.
- **Strict contract against real data.** yfinance returns `int64` volume, capitalized column names, extra columns, zones other than UTC, occasional NaN rows and rare inconsistent OHLC. Rejecting them is intended: #8 normalizes and decides whether to repair, drop or fail, with logging, and #10 records fixtures that exercise it.
- **Runtime image growth.** The arm64 image gains `numpy` 2.5.3 (15.7 MB wheel) and `pandas` 3.0.5 (10.5 MB wheel) plus `python-dateutil` and `six`, a few tens of MB once installed. Both publish `manylinux` aarch64 wheels for cp312 (in `uv.lock`), so no compilation is needed. `main.py` does not import the domain yet, so startup time and memory of the running app do not change in #4. `uv.lock` changes invalidate the builder cache layer. The PR's `Docker build (arm64)` and the beta deploy are the authoritative checks (V11).
- **pandas / pandas-stubs skew.** They are now in different groups; Dependabot may bump them separately. mypy in CI catches mismatches; bump them together.
- **`hash()` is not stable across processes.** Documented; #13 persists fields or `str(key)`.
- **Log and message injection.** Ticker and rule id charsets exclude whitespace, control characters and `|`; echoed inputs are truncated and rendered with `repr()` (T14).
- **Sensitive data.** None: synthetic data only; no tokens, hosts, IPs or users; no `TB_*` variables, migrations or `secrets.env` changes.
- **Unbreakable rules.** Signal-only (no order concepts), pure `domain/` (T15), closed candles (the contract carries open times; removal stays in #8), idempotency (AC13), UTC (AC1, contract), single worker (untouched), no `eval`, English only: all preserved.

## User decisions

- **D1 (2026-09-14):** one PR per issue with stacked branches (#3 → #4 → #5 → #6 → #7).
- **D2 (2026-09-14):** synthetic fixtures only in M1; recorded real-data fixtures deferred to #10.
- Earlier project decisions that apply: yfinance stocks and ETFs first (strictly positive prices are enough for now); timeframe per ticker with default `1d`; rules defined from the dashboard as JSON, without `eval`.
- No open product questions: every decision in this spec is technical, and the user-visible consequences (display of times, repair policy for bad candles) are assigned to #8, #10 and #17.

## Review checklist (tech-lead)

- [x] Meets the unbreakable rules of CLAUDE.md
- [x] Diff contains no secrets, IPs, hostnames or users
- [x] Tests cover the acceptance criteria, and T2, T5, T6, T9 and T10 fail without the implementation
- [x] Runtime dependency diff is exactly AC16; no `pandas-ta`
- [x] No `Any` in the domain public API (V4); domain purity guard green (T15)
- [x] #3 carry-over findings resolved (AC17)
- [x] Scope limited to Design §1
- [x] `scripts/check.py` green
- [x] Every artifact is in English (CLAUDE.md, rule 9)
