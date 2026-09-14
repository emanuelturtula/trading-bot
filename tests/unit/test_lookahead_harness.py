"""Self-tests of the look-ahead harness (spec 003, AC1-AC9)."""

from __future__ import annotations

import dataclasses
import itertools
import math
import pickle
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fixtures.candles import ALL_SCENARIOS, Scenario, synthetic_candles
from tests.lookahead import (
    DEFAULT_KS,
    DEFAULT_MAX_CUTS,
    EDGE_CUTS,
    LookaheadError,
    assert_no_lookahead,
    assert_no_lookahead_point_in_time,
    select_cuts,
    values_equal,
)

UTC_HOURS = pd.date_range("2024-01-01T00:00:00+00:00", periods=3, freq="1h")


def small_frame(n: int = 30) -> pd.DataFrame:
    """Tiny deterministic OHLCV frame for argument checks that do not need realistic data."""
    close = 100.0 + np.arange(n, dtype=np.float64)
    index = pd.date_range("2024-01-01T00:00:00+00:00", periods=n, freq="1D")
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": close},
        index=index,
    )


def cumulative_close(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].cumsum()


def spec_candles(scenario: Scenario = Scenario.RANDOM_WALK) -> pd.DataFrame:
    """The frame the acceptance criteria use: ``synthetic_candles(250, seed=7)``."""
    return synthetic_candles(250, seed=7, scenario=scenario)


def cheat_shift_forward(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].shift(-1)


@dataclasses.dataclass(frozen=True)
class Outcome:
    value: float
    fired: bool


@dataclasses.dataclass(frozen=True)
class OtherOutcome:
    value: float
    fired: bool


# --- T9: values_equal (AC9) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(math.nan, math.nan, id="nan-nan"),
        pytest.param(None, None, id="none-none"),
        pytest.param(pd.NaT, pd.NaT, id="nat-nat"),
        pytest.param(pd.NA, pd.NA, id="na-na"),
        pytest.param(None, math.nan, id="none-nan"),
        pytest.param(np.float64("nan"), pd.NA, id="numpy-nan-na"),
        pytest.param(np.datetime64("NaT", "s"), pd.NaT, id="numpy-nat-nat"),
        pytest.param(-0.0, 0.0, id="signed-zero"),
        pytest.param(math.inf, math.inf, id="inf"),
        pytest.param(np.float64(1.5), 1.5, id="numpy-float-python-float"),
        pytest.param(np.float32(0.5), 0.5, id="float32-python-float"),
        pytest.param(np.int64(3), 3, id="numpy-int-python-int"),
        pytest.param(np.bool_(True), True, id="numpy-bool-python-bool"),
        pytest.param("buy", "buy", id="string"),
        pytest.param(UTC_HOURS[0], UTC_HOURS[0], id="timestamp"),
        pytest.param({"a": math.nan, "b": 1.0}, {"b": 1.0, "a": math.nan}, id="mapping-nan"),
        pytest.param([1.0, math.nan], [1.0, math.nan], id="list-nan"),
        pytest.param((None, "x"), (None, "x"), id="tuple-none"),
        pytest.param(Outcome(math.nan, False), Outcome(math.nan, False), id="dataclass-nan"),
        pytest.param(
            {"outer": [Outcome(math.nan, True)]},
            {"outer": [Outcome(math.nan, True)]},
            id="nested",
        ),
        pytest.param(
            pd.Series([1.0, math.nan], index=UTC_HOURS[:2]),
            pd.Series([1.0, math.nan], index=UTC_HOURS[:2]),
            id="series-nan",
        ),
    ],
)
def test_values_equal_accepts_equal_values(left: object, right: object) -> None:
    assert values_equal(left, right)
    assert values_equal(right, left)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(math.nan, 1.0, id="nan-number"),
        pytest.param(None, 0, id="none-zero"),
        pytest.param(pd.NaT, UTC_HOURS[0], id="nat-timestamp"),
        pytest.param(pd.NA, False, id="na-false"),
        pytest.param(1.0, float(np.nextafter(1.0, 2.0)), id="one-ulp"),
        pytest.param(math.inf, -math.inf, id="inf-signs"),
        pytest.param(True, 1, id="bool-int"),
        pytest.param(np.bool_(False), 0.0, id="numpy-bool-float"),
        pytest.param(False, np.int64(0), id="bool-numpy-int"),
        pytest.param("buy", "sell", id="string"),
        pytest.param({"a": 1.0}, {"a": 1.0, "b": 2.0}, id="mapping-keys"),
        pytest.param({"a": 1.0}, {"a": 2.0}, id="mapping-values"),
        pytest.param([1.0], [1.0, 2.0], id="list-length"),
        pytest.param([1.0, 2.0], (1.0, 2.0), id="list-tuple"),
        pytest.param(Outcome(1.0, True), Outcome(1.0, False), id="dataclass-field"),
        pytest.param(Outcome(1.0, True), OtherOutcome(1.0, True), id="dataclass-type"),
        pytest.param({"a": 1.0}, [1.0], id="mapping-list"),
        pytest.param(Outcome(1.0, True), {"value": 1.0, "fired": True}, id="dataclass-mapping"),
        pytest.param(
            pd.Series([1.0, 2.0], index=UTC_HOURS[:2]),
            pd.Series([1.0, 2.5], index=UTC_HOURS[:2]),
            id="series-cells",
        ),
        pytest.param(
            pd.Series([1.0, 2.0], index=UTC_HOURS[:2]),
            pd.Series([1.0, 2.0], index=UTC_HOURS[1:]),
            id="series-index",
        ),
        pytest.param(
            pd.Series([1.0, 2.0], index=UTC_HOURS[:2]),
            pd.Series([1, 2], index=UTC_HOURS[:2]),
            id="series-dtype",
        ),
        pytest.param(pd.Series([1.0]), 1.0, id="series-scalar"),
    ],
)
def test_values_equal_rejects_different_values(left: object, right: object) -> None:
    assert not values_equal(left, right)
    assert not values_equal(right, left)


def test_values_equal_tolerance_is_opt_in() -> None:
    one_ulp_above = float(np.nextafter(1.0, 2.0))

    assert not values_equal(1.0, one_ulp_above)
    assert values_equal(1.0, one_ulp_above, rtol=1e-12)
    assert values_equal(1.0, one_ulp_above, atol=1e-12)
    assert values_equal(np.float64(100.0), 100.5, atol=0.5)
    assert not values_equal(100.0, 100.5, atol=0.4)
    assert values_equal(100.0, 101.0, rtol=0.01)
    assert values_equal([1.0, {"a": 1.0}], [one_ulp_above, {"a": one_ulp_above}], rtol=1e-12)


def test_values_equal_tolerance_never_equates_infinity_with_finite_values() -> None:
    assert not values_equal(math.inf, 1e308, rtol=1.0)
    assert not values_equal(1e308, math.inf, rtol=1.0, atol=1.0)
    assert values_equal(math.inf, math.inf, rtol=1.0)


def test_values_equal_rejects_values_whose_comparison_is_not_boolean() -> None:
    with pytest.raises(TypeError, match="bool"):
        values_equal(np.array([1.0, 2.0]), np.array([1.0, 2.0]))


@pytest.mark.parametrize(
    ("rtol", "atol"), [(-1e-9, 0.0), (0.0, -1e-9), (math.nan, 0.0), (0.0, math.inf)]
)
def test_values_equal_rejects_invalid_tolerances(rtol: float, atol: float) -> None:
    with pytest.raises(ValueError, match=r"rtol|atol"):
        values_equal(1.0, 1.0, rtol=rtol, atol=atol)


# --- T7: argument validation (AC7) ------------------------------------------------------


def test_candles_must_be_a_dataframe() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        assert_no_lookahead(cumulative_close, small_frame()["close"])  # type: ignore[arg-type]


@pytest.mark.parametrize("rows", [0, 1])
def test_candles_need_at_least_two_rows(rows: int) -> None:
    with pytest.raises(ValueError, match="at least 2 rows"):
        assert_no_lookahead(cumulative_close, small_frame(rows))


def test_candles_index_must_be_unique() -> None:
    candles = small_frame(10)
    candles.index = candles.index[[0, 1, 2, 3, 4, 4, 6, 7, 8, 9]]

    with pytest.raises(ValueError, match="unique"):
        assert_no_lookahead(cumulative_close, candles)


def test_candles_index_must_be_increasing() -> None:
    with pytest.raises(ValueError, match="increasing"):
        assert_no_lookahead(cumulative_close, small_frame(10).iloc[::-1])


@pytest.mark.parametrize("ks", [(0,), (1, -2), (1, 2, 0)])
def test_every_k_must_be_positive(ks: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="ks"):
        assert_no_lookahead(cumulative_close, small_frame(), ks=ks)


def test_ks_must_be_integers() -> None:
    with pytest.raises(TypeError, match="ks"):
        assert_no_lookahead(cumulative_close, small_frame(), ks=(1.5,))  # type: ignore[arg-type]


@pytest.mark.parametrize("cuts", [[0], [5, 30], [-1, 3], []])
def test_explicit_cuts_must_be_within_the_frame(cuts: list[int]) -> None:
    with pytest.raises(ValueError, match="cuts"):
        assert_no_lookahead(cumulative_close, small_frame(30), cuts=cuts)


@pytest.mark.parametrize("max_cuts", [0, -3])
def test_max_cuts_must_be_positive(max_cuts: int) -> None:
    with pytest.raises(ValueError, match="max_cuts"):
        assert_no_lookahead(cumulative_close, small_frame(), max_cuts=max_cuts)


@pytest.mark.parametrize("min_prefix", [0, 30, 31])
def test_min_prefix_must_leave_a_later_candle(min_prefix: int) -> None:
    with pytest.raises(ValueError, match="min_prefix"):
        assert_no_lookahead(cumulative_close, small_frame(30), min_prefix=min_prefix)


@pytest.mark.parametrize(("rtol", "atol"), [(-1e-12, 0.0), (0.0, -1.0)])
def test_tolerances_must_not_be_negative(rtol: float, atol: float) -> None:
    with pytest.raises(ValueError, match=r"rtol|atol"):
        assert_no_lookahead(cumulative_close, small_frame(), rtol=rtol, atol=atol)


# --- T5: cut selection and bounded cost (AC5) -------------------------------------------


def test_select_cuts_defaults_sample_head_middle_and_tail() -> None:
    cuts = select_cuts(250)

    assert cuts[:EDGE_CUTS] == (1, 2, 3, 4, 5)
    assert cuts[-EDGE_CUTS:] == (245, 246, 247, 248, 249)
    assert len(cuts) <= DEFAULT_MAX_CUTS
    assert DEFAULT_KS == (1, 2, 5)


def test_select_cuts_is_a_full_sweep_when_candidates_fit() -> None:
    assert select_cuts(10, min_prefix=3, max_cuts=7) == (3, 4, 5, 6, 7, 8, 9)
    assert select_cuts(2) == (1,)


@given(
    length=st.integers(min_value=2, max_value=2_000),
    min_prefix_offset=st.integers(min_value=0, max_value=2_000),
    max_cuts=st.integers(min_value=1, max_value=60),
)
def test_select_cuts_properties(length: int, min_prefix_offset: int, max_cuts: int) -> None:
    min_prefix = 1 + min_prefix_offset % (length - 1)

    cuts = select_cuts(length, min_prefix=min_prefix, max_cuts=max_cuts)

    candidates = list(range(min_prefix, length))
    assert cuts == select_cuts(length, min_prefix=min_prefix, max_cuts=max_cuts)
    assert all(a < b for a, b in itertools.pairwise(cuts))
    assert all(min_prefix <= cut <= length - 1 for cut in cuts)
    assert 1 <= len(cuts) <= max_cuts
    if len(candidates) <= max_cuts:
        assert list(cuts) == candidates
    else:
        edge = min(EDGE_CUTS, max_cuts // 3)
        assert set(candidates[:edge]) <= set(cuts)
        assert set(candidates[len(candidates) - edge :]) <= set(cuts)


@pytest.mark.parametrize(
    ("length", "min_prefix", "max_cuts"), [(1, 1, 25), (10, 0, 25), (10, 10, 25), (10, 1, 0)]
)
def test_select_cuts_rejects_invalid_arguments(length: int, min_prefix: int, max_cuts: int) -> None:
    with pytest.raises(ValueError, match=r"length|min_prefix|max_cuts"):
        select_cuts(length, min_prefix=min_prefix, max_cuts=max_cuts)


def test_report_uses_selected_cuts_and_normalized_ks() -> None:
    candles = small_frame(120)

    report = assert_no_lookahead(cumulative_close, candles, ks=(5, 1, 5), min_prefix=4, max_cuts=9)

    assert report.cuts == select_cuts(120, min_prefix=4, max_cuts=9)
    assert report.ks == (1, 5)


def test_report_uses_sorted_unique_explicit_cuts() -> None:
    report = assert_no_lookahead(cumulative_close, small_frame(30), cuts=[20, 3, 20, 7])

    assert report.cuts == (3, 7, 20)


@pytest.mark.parametrize(("length", "max_cuts"), [(30, 25), (250, 25), (250, 250), (60, 4)])
def test_calls_are_bounded_and_counted(length: int, max_cuts: int) -> None:
    seen_lengths: list[int] = []

    def spy(candles: pd.DataFrame) -> pd.Series:
        seen_lengths.append(len(candles))
        return candles["close"].rolling(3).mean()

    report = assert_no_lookahead(spy, small_frame(length), max_cuts=max_cuts)

    assert report.calls == len(seen_lengths)
    assert report.calls <= len(report.cuts) * (len(report.ks) + 1) + 2
    assert report.comparisons > 0
    assert report.compared_values >= report.non_missing_values > 0


def test_ks_beyond_the_frame_are_skipped_and_the_full_frame_is_always_compared() -> None:
    seen_lengths: set[int] = set()

    def spy(candles: pd.DataFrame) -> pd.Series:
        seen_lengths.add(len(candles))
        return candles["close"].cumsum()

    report = assert_no_lookahead(spy, small_frame(30), cuts=[27])

    assert seen_lengths == {27, 28, 29, 30}
    assert report.comparisons == 3


def test_full_frame_comparison_catches_length_specific_cheats() -> None:
    def cheat_on_full_length(candles: pd.DataFrame) -> pd.Series:
        bump = 1.0 if len(candles) == 250 else 0.0
        return candles["close"] + bump

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_on_full_length, small_frame(250), ks=(1,), cuts=[10])

    violation = caught.value.violation
    assert violation.kind == "value_mismatch"
    assert (violation.prefix_length, violation.k) == (10, 240)


def test_exceptions_from_the_function_carry_the_cut() -> None:
    candles = small_frame(30)

    def explode_on_seven(frame: pd.DataFrame) -> pd.Series:
        if len(frame) == 7:
            raise ZeroDivisionError("boom")
        return frame["close"]

    with pytest.raises(ZeroDivisionError, match="boom") as caught:
        assert_no_lookahead(explode_on_seven, candles, cuts=[7])

    notes = "\n".join(caught.value.__notes__)
    assert "explode_on_seven" in notes
    assert "n=7" in notes
    assert f"t={candles.index[6].isoformat()}" in notes


# --- T1: cheats are caught (AC1) --------------------------------------------------------


def cheat_centered_window(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(5, center=True).mean()


def cheat_backfilled_warmup(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean().bfill()


def cheat_global_max(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close / close.max()


def cheat_global_mean(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close - close.mean()


def cheat_rule_peeks_next_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close.shift(-1) > close


def cheat_filter_confirmed_by_next_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close[close < close.shift(-1)]


@pytest.mark.parametrize(
    ("cheat", "kind"),
    [
        pytest.param(cheat_shift_forward, "value_mismatch", id="shift-forward"),
        pytest.param(cheat_centered_window, "value_mismatch", id="centered-window"),
        pytest.param(cheat_backfilled_warmup, "value_mismatch", id="bfill"),
        pytest.param(cheat_global_max, "value_mismatch", id="global-max"),
        pytest.param(cheat_global_mean, "value_mismatch", id="global-mean"),
        pytest.param(cheat_rule_peeks_next_candle, "value_mismatch", id="rule-peeks"),
        pytest.param(cheat_filter_confirmed_by_next_candle, "result_appeared", id="filter"),
    ],
)
def test_cheating_functions_are_caught(cheat: Callable[[pd.DataFrame], object], kind: str) -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat, spec_candles())

    assert isinstance(caught.value, AssertionError)
    assert caught.value.violation.kind == kind
    assert caught.value.violation.func_name == cheat.__qualname__
    assert "hint: " in str(caught.value)


# --- T2: failure report (AC2) -----------------------------------------------------------


def test_failure_report_points_to_the_first_offending_candle() -> None:
    candles = spec_candles()

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_shift_forward, candles)

    violation = caught.value.violation
    assert violation.kind == "value_mismatch"
    assert violation.func_name == "cheat_shift_forward"
    assert violation.output == "close"
    assert (violation.prefix_length, violation.k) == (1, 1)
    assert violation.t == violation.stamp == candles.index[0]
    assert isinstance(violation.prefix_value, float)
    assert math.isnan(violation.prefix_value)
    assert violation.extended_value == candles["close"].iloc[1]
    assert violation.offending_count == 1

    message = str(caught.value)
    for element in (
        "look-ahead",
        cheat_shift_forward.__qualname__,
        "value_mismatch",
        "t=2024-01-01T00:00:00+00:00",
        "n=1",
        "k=1",
        "first offending candle: 2024-01-01T00:00:00+00:00",
        "nan",
        repr(float(candles["close"].iloc[1])),
        "hint:",
    ):
        assert element in message


def test_failure_report_marks_the_full_frame_comparison() -> None:
    candles = spec_candles().iloc[:12]

    def cheat_on_full_length(frame: pd.DataFrame) -> pd.Series:
        return frame["close"] * (2.0 if len(frame) == 12 else 1.0)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_on_full_length, candles, ks=(1,), cuts=[3])

    assert "k=9 (prefix length 12) (full frame)" in str(caught.value)


# --- T3: honest functions pass (AC3) ----------------------------------------------------


def honest_sma(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean()


def honest_rolling_std(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).std()


def honest_ema_recursive(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].ewm(span=10, adjust=False).mean()


def honest_ema_adjusted(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].ewm(span=10).mean()


def honest_obv_like(candles: pd.DataFrame) -> pd.Series:
    direction = np.sign(candles["close"].diff()).fillna(0.0)
    return (direction * candles["volume"]).cumsum()


def honest_drawdown(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close / close.cummax() - 1


def honest_diff(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].diff()


def honest_shift_back(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].shift(1)


def honest_bollinger_like(candles: pd.DataFrame) -> pd.DataFrame:
    close = candles["close"]
    middle = close.rolling(20).mean()
    spread = 2 * close.rolling(20).std()
    return pd.DataFrame({"lower": middle - spread, "upper": middle + spread})


def honest_macd_like(candles: pd.DataFrame) -> dict[str, pd.Series]:
    close = candles["close"]
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    return {"macd": macd, "signal": signal, "hist": macd - signal}


def honest_crossover(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    sma = close.rolling(20).mean()
    return (close > sma) & (close.shift(1) <= sma.shift(1))


def honest_warmup_dropped(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(20).mean().dropna()


def honest_windowed(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].rolling(5).mean().iloc[-20:]


HONEST_FUNCTIONS = [
    honest_sma,
    honest_rolling_std,
    honest_ema_recursive,
    honest_ema_adjusted,
    honest_obv_like,
    honest_drawdown,
    honest_diff,
    honest_shift_back,
    honest_bollinger_like,
    honest_macd_like,
    honest_crossover,
    honest_warmup_dropped,
    honest_windowed,
]


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
@pytest.mark.parametrize("func", HONEST_FUNCTIONS, ids=lambda func: func.__name__)
def test_honest_functions_pass(func: Callable[[pd.DataFrame], object], scenario: Scenario) -> None:
    report = assert_no_lookahead(func, spec_candles(scenario))

    assert report.non_missing_values > 0
    assert report.compared_values >= report.non_missing_values


# --- T4: comparison semantics (AC4) -----------------------------------------------------


def drift_one_ulp_at_100(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"].copy()
    if len(candles) >= 100:
        close.iloc[:50] = np.nextafter(close.iloc[:50].to_numpy(), np.inf)
    return close


def object_dtype_at_100(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close.astype(object) if len(candles) >= 100 else close


def extra_output_at_100(candles: pd.DataFrame) -> dict[str, pd.Series]:
    close = candles["close"]
    return {"a": close, "b": close} if len(candles) >= 100 else {"a": close}


def drop_position_50_at_100(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close.drop(candles.index[50]) if len(candles) >= 100 else close


def test_comparison_is_exact_by_default() -> None:
    candles = spec_candles()

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(drift_one_ulp_at_100, candles)

    assert caught.value.violation.kind == "value_mismatch"


def test_tolerance_is_an_explicit_opt_in() -> None:
    # One ulp is far below 1e-12 relative error: the tolerance must absorb it.
    report = assert_no_lookahead(drift_one_ulp_at_100, spec_candles(), rtol=1e-12)

    assert report.non_missing_values > 0


@pytest.mark.parametrize(
    ("func", "kind"),
    [
        pytest.param(object_dtype_at_100, "dtype_mismatch", id="dtype"),
        pytest.param(extra_output_at_100, "outputs_changed", id="outputs"),
        pytest.param(drop_position_50_at_100, "result_disappeared", id="disappeared"),
    ],
)
def test_structural_changes_are_reported(func: Callable[[pd.DataFrame], object], kind: str) -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(func, spec_candles())

    assert caught.value.violation.kind == kind


def test_dtype_mismatch_reports_both_dtypes() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(object_dtype_at_100, spec_candles())

    violation = caught.value.violation
    assert violation.output == "close"
    assert "float64" in violation.detail
    assert "object" in violation.detail


def test_outputs_changed_reports_both_output_lists() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(extra_output_at_100, spec_candles())

    violation = caught.value.violation
    assert violation.prefix_value == ("a",)
    assert violation.extended_value == ("a", "b")


def test_result_disappeared_reports_the_missing_candle() -> None:
    candles = spec_candles()

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(drop_position_50_at_100, candles)

    violation = caught.value.violation
    assert violation.stamp == candles.index[50]
    assert violation.prefix_value == candles["close"].iloc[50]
    assert "<no result>" in str(caught.value)


def test_result_appeared_renders_the_missing_prefix_result() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_filter_confirmed_by_next_candle, spec_candles())

    assert "with data[:t]:   <no result>" in str(caught.value)


# --- T6: output contract and purity (AC6) -----------------------------------------------


def returns_last_close(candles: pd.DataFrame) -> float:
    return float(candles["close"].iloc[-1])


def returns_numpy_array(candles: pd.DataFrame) -> np.ndarray:
    return candles["close"].to_numpy()


def returns_range_index(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].reset_index(drop=True)


def returns_tz_naive_index(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].tz_localize(None)


def returns_future_labels(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].shift(1, freq="1D")


def returns_duplicated_labels(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return pd.concat([close, close.iloc[-1:]])


def returns_decreasing_labels(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].iloc[::-1]


def returns_non_string_columns(candles: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({0: candles["close"]})


def returns_duplicated_columns(candles: pd.DataFrame) -> pd.DataFrame:
    close = candles["close"]
    return pd.concat([close, close], axis=1)


def returns_mapping_with_non_string_key(candles: pd.DataFrame) -> dict[object, pd.Series]:
    return {1: candles["close"]}


def returns_mapping_with_non_series_value(candles: pd.DataFrame) -> dict[str, object]:
    return {"close": candles["close"].to_numpy()}


@pytest.mark.parametrize(
    "func",
    [
        returns_last_close,
        returns_numpy_array,
        returns_range_index,
        returns_tz_naive_index,
        returns_future_labels,
        returns_duplicated_labels,
        returns_decreasing_labels,
        returns_non_string_columns,
        returns_duplicated_columns,
        returns_mapping_with_non_string_key,
        returns_mapping_with_non_series_value,
    ],
    ids=lambda func: func.__name__,
)
def test_invalid_outputs_are_rejected(func: Callable[[pd.DataFrame], object]) -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(func, spec_candles())

    violation = caught.value.violation
    assert violation.kind == "invalid_output"
    assert violation.func_name == func.__qualname__
    assert violation.detail


def test_invalid_output_points_to_the_point_in_time_harness() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(returns_last_close, spec_candles())

    assert "assert_no_lookahead_point_in_time" in str(caught.value)
    assert "float" in caught.value.violation.detail


@pytest.mark.parametrize(
    ("func", "reason"),
    [
        (returns_range_index, "not labels of the input frame"),
        (returns_tz_naive_index, "not labels of the input frame"),
        (returns_future_labels, "not labels of the input frame"),
        (returns_duplicated_labels, "duplicated"),
        (returns_decreasing_labels, "increasing"),
    ],
    ids=["range-index", "tz-naive", "future", "duplicated", "decreasing"],
)
def test_invalid_labels_are_explained(func: Callable[[pd.DataFrame], object], reason: str) -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(func, spec_candles())

    assert reason in caught.value.violation.detail


def mutates_close(candles: pd.DataFrame) -> pd.Series:
    candles["close"] = candles["close"] * 2
    return candles["close"].rolling(3).mean()


def drops_a_column(candles: pd.DataFrame) -> pd.Series:
    candles.drop(columns="volume", inplace=True)  # the mutation under test
    return candles["close"]


@pytest.mark.parametrize("func", [mutates_close, drops_a_column], ids=lambda f: f.__name__)
def test_input_mutation_is_reported(func: Callable[[pd.DataFrame], object]) -> None:
    candles = spec_candles()
    pristine = candles.copy(deep=True)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(func, candles)

    assert caught.value.violation.kind == "input_mutated"
    assert caught.value.violation.prefix_length == len(candles)
    pd.testing.assert_frame_equal(candles, pristine, check_exact=True)


def test_hidden_state_is_reported_as_nondeterministic() -> None:
    calls: list[int] = []

    def counts_calls(candles: pd.DataFrame) -> pd.Series:
        calls.append(len(candles))
        return candles["close"] + len(calls)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(counts_calls, spec_candles())

    violation = caught.value.violation
    assert violation.kind == "nondeterministic"
    assert violation.prefix_length == 250
    assert violation.stamp is not None
    assert "remove hidden state" in str(caught.value)


def test_nondeterministic_labels_are_reported() -> None:
    calls: list[int] = []

    def drops_first_row_on_rerun(candles: pd.DataFrame) -> pd.Series:
        calls.append(len(candles))
        close = candles["close"]
        return close.iloc[1:] if len(calls) > 1 else close

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(drops_first_row_on_rerun, spec_candles())

    assert caught.value.violation.kind == "nondeterministic"


def test_vacuous_checks_fail() -> None:
    def long_warmup(candles: pd.DataFrame) -> pd.Series:
        return candles["close"].rolling(300).mean()

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(long_warmup, spec_candles())

    assert caught.value.violation.kind == "vacuous"
    assert "warmup" in str(caught.value)


def test_lookahead_error_survives_pickling() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_shift_forward, spec_candles())

    restored = pickle.loads(pickle.dumps(caught.value))  # noqa: S301 - round trip of our own object

    assert str(restored) == str(caught.value)
    assert restored.violation.kind == caught.value.violation.kind


# --- T8: point-in-time functions (AC8) --------------------------------------------------


def closes_higher(candles: pd.DataFrame) -> bool:
    close = candles["close"]
    return bool(close.iloc[-1] > close.iloc[-2])


def closes_higher_by_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close > close.shift(1)


def previous_candle_confirmed_by_next(candles: pd.DataFrame) -> bool:
    close = candles["close"]
    return bool(close.iloc[-2] > close.iloc[-3] and close.iloc[-1] > close.iloc[-2])


def peeking_reference(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close.shift(-1) > close


def reference_last_five(candles: pd.DataFrame) -> pd.Series:
    return closes_higher_by_candle(candles).iloc[-5:]


def test_point_in_time_function_with_matching_reference_passes() -> None:
    report = assert_no_lookahead_point_in_time(
        closes_higher, closes_higher_by_candle, spec_candles(), min_prefix=2
    )

    assert report.non_missing_values > 0
    assert report.calls <= len(report.cuts) * (len(report.ks) + 2) + 4


def test_decision_confirmed_by_the_next_candle_is_caught() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            previous_candle_confirmed_by_next,
            closes_higher_by_candle,
            spec_candles(),
            min_prefix=3,
        )

    violation = caught.value.violation
    assert violation.kind == "point_in_time_mismatch"
    assert violation.func_name == previous_candle_confirmed_by_next.__qualname__
    assert violation.stamp == violation.t
    assert violation.k == 0
    assert violation.prefix_value is False
    assert violation.extended_value == np.True_
    assert "reference" in str(caught.value)


def test_peeking_reference_is_reported_by_name() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            closes_higher, peeking_reference, spec_candles(), min_prefix=2
        )

    assert caught.value.violation.kind == "value_mismatch"
    assert caught.value.violation.func_name == peeking_reference.__qualname__


def test_reference_without_a_result_for_every_candle_is_reported() -> None:
    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            closes_higher, reference_last_five, spec_candles(), min_prefix=2
        )

    violation = caught.value.violation
    assert violation.kind == "missing_reference"
    assert violation.func_name == reference_last_five.__qualname__
    assert violation.k is not None
    assert violation.k > 0
    assert "full length" in str(caught.value)


def sma_and_trend_by_candle(candles: pd.DataFrame) -> pd.DataFrame:
    close = candles["close"]
    sma = close.rolling(3).mean()
    return pd.DataFrame({"sma": sma, "above": close > sma})


def sma_and_trend(candles: pd.DataFrame) -> dict[str, object]:
    close = candles["close"]
    sma = float(close.rolling(3).mean().iloc[-1])
    return {"sma": sma, "above": bool(close.iloc[-1] > sma)}


def test_dataframe_reference_pairs_with_a_mapping_result() -> None:
    report = assert_no_lookahead_point_in_time(
        sma_and_trend, sma_and_trend_by_candle, spec_candles()
    )

    assert report.non_missing_values > 0


def outcomes_by_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    sma = close.rolling(4).mean()
    outcomes = [
        Outcome(value=float(value), fired=bool(price > value))
        for price, value in zip(close.to_numpy(), sma.to_numpy(), strict=True)
    ]
    return pd.Series(outcomes, index=candles.index, dtype=object)


def last_outcome(candles: pd.DataFrame) -> Outcome:
    close = candles["close"]
    value = float(close.rolling(4).mean().iloc[-1])
    return Outcome(value=value, fired=bool(close.iloc[-1] > value))


def test_dataclass_results_with_nan_fields_are_compared_recursively() -> None:
    report = assert_no_lookahead_point_in_time(last_outcome, outcomes_by_candle, spec_candles())

    assert report.non_missing_values > 0


def test_point_in_time_validates_arguments() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        assert_no_lookahead_point_in_time(
            closes_higher,
            closes_higher_by_candle,
            [1.0, 2.0],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="ks"):
        assert_no_lookahead_point_in_time(
            closes_higher, closes_higher_by_candle, small_frame(), ks=(0,)
        )


def test_point_in_time_function_must_be_deterministic() -> None:
    calls: list[int] = []

    def flips(candles: pd.DataFrame) -> bool:
        calls.append(len(candles))
        return len(calls) % 2 == 0

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            flips, closes_higher_by_candle, spec_candles(), min_prefix=2
        )

    assert caught.value.violation.kind == "nondeterministic"
    assert caught.value.violation.func_name == flips.__qualname__


def test_point_in_time_function_must_not_mutate_its_input() -> None:
    def mutates(candles: pd.DataFrame) -> bool:
        candles.iloc[-1, 0] = 0.0
        return closes_higher(candles)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            mutates, closes_higher_by_candle, spec_candles(), min_prefix=2
        )

    assert caught.value.violation.kind == "input_mutated"
    assert caught.value.violation.func_name == mutates.__qualname__


def test_point_in_time_results_that_are_always_missing_are_vacuous() -> None:
    def early_rows_only(candles: pd.DataFrame) -> pd.Series:
        return candles["close"].where(np.arange(len(candles)) < 3)

    def last_early_row(candles: pd.DataFrame) -> float:
        return float(candles["close"].iloc[-1]) if len(candles) <= 3 else math.nan

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            last_early_row, early_rows_only, small_frame(30), cuts=[5, 10]
        )

    assert caught.value.violation.kind == "vacuous"
    assert caught.value.violation.func_name == last_early_row.__qualname__
