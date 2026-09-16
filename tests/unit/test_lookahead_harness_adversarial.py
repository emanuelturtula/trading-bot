"""Adversarial tests of the look-ahead harness (spec 003, Test plan T16-T18).

These tests go beyond the developer's self-tests (``test_lookahead_harness.py``): trickier
cheats that a naive implementation of ``assert_no_lookahead`` could miss, honest functions with
unusual shapes, and point-in-time edge cases. One test also documents a known, spec-acknowledged
limit of the default cut sampling (see ``test_narrow_window_cheat_...`` below); it is not a
harness defect.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.lookahead import LookaheadError, assert_no_lookahead, assert_no_lookahead_point_in_time

# --- T16: adversarial cheats (AC1, AC5) --------------------------------------------------


def cheat_reversed_cummax(candles: pd.DataFrame) -> pd.Series:
    """Cumulative max of the reversed series: each value is the max of itself and every future
    candle, an acausal statistic disguised as a rolling-looking one-liner."""
    close = candles["close"]
    return close.iloc[::-1].cummax().iloc[::-1]


def cheat_percentile_rank(candles: pd.DataFrame) -> pd.Series:
    """Percentile rank against the whole frame: adding future candles changes past ranks."""
    return candles["close"].rank(pct=True)


def cheat_global_zscore(candles: pd.DataFrame) -> pd.Series:
    """Z-score against the whole-frame mean and standard deviation (a global statistic)."""
    close = candles["close"]
    return (close - close.mean()) / close.std()


def cheat_reversed_ewm(candles: pd.DataFrame) -> pd.Series:
    """EWM computed on the reversed series: decays into the future instead of out of the past."""
    close = candles["close"]
    return close.iloc[::-1].ewm(span=5, adjust=False).mean().iloc[::-1]


@pytest.mark.parametrize(
    "cheat",
    [cheat_reversed_cummax, cheat_percentile_rank, cheat_global_zscore, cheat_reversed_ewm],
    ids=lambda func: func.__name__,
)
def test_global_and_reversed_statistics_are_caught(cheat: Callable[[pd.DataFrame], object]) -> None:
    """These global/acausal one-liners are caught with default arguments, no scenario needed."""
    candles = synthetic_candles(250, seed=7)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat, candles)

    assert caught.value.violation.kind == "value_mismatch"
    assert caught.value.violation.func_name == cheat.__qualname__


def cheat_interpolate_masked(candles: pd.DataFrame) -> pd.Series:
    """Mask zero-volume candles to NaN, then fill them by linear interpolation.

    Interpolation between two known points uses the point *after* the gap, so a candle inside a
    flat, zero-volume run only gets its "corrected" value once a later, non-flat candle exists.
    """
    close = candles["close"]
    masked = close.where(candles["volume"] > 0)
    return masked.interpolate()


def _find_flat_run(candles: pd.DataFrame) -> tuple[int, int]:
    """Positions ``[start, end)`` of the first zero-volume run of at least 5 rows."""
    zero_volume = (candles["volume"] == 0).to_numpy()
    run_start: int | None = None
    for i, flag in enumerate(zero_volume):
        if flag and run_start is None:
            run_start = i
        elif not flag and run_start is not None:
            if i - run_start >= 5:
                return run_start, i
            run_start = None
    if run_start is not None and len(zero_volume) - run_start >= 5:
        return run_start, len(zero_volume)
    raise AssertionError("no flat run of at least 5 candles found")


def test_interpolation_over_a_masked_flat_run_is_caught() -> None:
    candles = synthetic_candles(250, seed=7, scenario=Scenario.FLAT_RUNS)
    start, end = _find_flat_run(candles)
    # A cut strictly inside the run: the prefix cannot see the recovery candle that follows it.
    n = start + 2
    k = (end - n) + 2

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_interpolate_masked, candles, cuts=[n], ks=[k])

    assert caught.value.violation.kind == "value_mismatch"


def cheat_gap_bfill_after_mask(candles: pd.DataFrame) -> pd.Series:
    """Mask candles that open away from the previous close (a gap jump) to NaN, then back-fill.

    On a contiguous series this is a no-op (no candle ever opens away from the previous close).
    On ``GAPS`` it "corrects" the candle right after a gap using whatever candle comes next, which
    is not yet known at the time that candle closes: visible only on ``GAPS``.
    """
    close = candles["close"]
    previous_close = close.shift(1)
    is_gap_jump = (candles["open"] != previous_close) & previous_close.notna()
    return close.mask(is_gap_jump).bfill()


def _first_recoverable_gap(candles: pd.DataFrame) -> int:
    """Position of a gap-jump candle that is not itself immediately followed by another one."""
    close = candles["close"]
    previous_close = close.shift(1)
    is_gap = ((candles["open"] != previous_close) & previous_close.notna()).to_numpy()
    for position in np.flatnonzero(is_gap):
        if position + 1 < len(candles) and not is_gap[position + 1]:
            return int(position)
    raise AssertionError("no recoverable gap-jump candle found")


def test_backfill_after_masking_gap_jumps_is_caught_only_on_gaps() -> None:
    gapped = synthetic_candles(250, seed=7, scenario=Scenario.GAPS)
    gap = _first_recoverable_gap(gapped)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_gap_bfill_after_mask, gapped, cuts=[gap + 1], ks=[1])
    assert caught.value.violation.kind == "value_mismatch"

    contiguous = synthetic_candles(250, seed=7, scenario=Scenario.RANDOM_WALK)
    report = assert_no_lookahead(cheat_gap_bfill_after_mask, contiguous)
    assert report.non_missing_values > 0


def test_length_dependent_cheat_is_caught_by_the_full_frame_comparison_alone() -> None:
    """With default ``ks`` and the default cut sampling, only the mandatory full-frame comparison
    (Design 3.3) catches a cheat that only fires once the whole 250-candle frame is known."""

    def cheat_only_at_full_length(candles: pd.DataFrame) -> pd.Series:
        bump = 1.0 if len(candles) == 250 else 0.0
        return candles["close"] + bump

    candles = synthetic_candles(250, seed=7)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(cheat_only_at_full_length, candles)

    violation = caught.value.violation
    assert violation.kind == "value_mismatch"
    assert violation.prefix_length is not None
    assert violation.k is not None
    assert violation.prefix_length + violation.k == 250


# --- Probing for false negatives beyond T16 -----------------------------------------------


def test_narrow_window_cheat_escapes_default_sampling_but_full_sweep_catches_it() -> None:
    """Documents a known, spec-acknowledged limit of the default cut sampling (spec 003, "Risks
    and security": "Sampling misses length-specific behavior").

    This cheat only disagrees between a prefix and its extension for cuts ``n`` in ``{138, 139,
    140}`` out of 249 possible cuts on a 250-candle frame. ``select_cuts(250)`` (used by
    ``assert_no_lookahead`` with default arguments) does not happen to sample any of them (its
    middle cuts jump from 125 to 143), and the mandatory full-frame comparison does not help
    either: it is only informative at cuts that are themselves checked, and none of the checked
    cuts falls inside the narrow window where the two sides disagree. This is not a defect: the
    spec explicitly documents the sampling trade-off and its mitigations (a full sweep via
    ``max_cuts=len(candles)``, or targeted ``cuts=``), both verified below.
    """

    def leaks_one_fixed_position(candles: pd.DataFrame) -> pd.Series:
        close = candles["close"].copy()
        if len(candles) > 140:
            close.iloc[137] = candles["close"].iloc[140]
        return close

    candles = synthetic_candles(250, seed=7)

    # Default arguments: the cheat is not sampled, so the check passes despite the cheat.
    report = assert_no_lookahead(leaks_one_fixed_position, candles)
    assert report.non_missing_values > 0

    # Mitigation 1: a full sweep (every possible cut) does catch it.
    with pytest.raises(LookaheadError) as full_sweep:
        assert_no_lookahead(leaks_one_fixed_position, candles, max_cuts=len(candles))
    assert full_sweep.value.violation.kind == "value_mismatch"

    # Mitigation 2: a targeted explicit cut inside the narrow window also catches it.
    with pytest.raises(LookaheadError) as targeted:
        assert_no_lookahead(leaks_one_fixed_position, candles, cuts=[138])
    assert targeted.value.violation.kind == "value_mismatch"


# --- T17: adversarial honest cases (AC3, AC5, AC9) -----------------------------------------


def honest_time_based_rolling_mean(candles: pd.DataFrame) -> pd.Series:
    """A calendar-duration window (not a row count): still causal on irregularly spaced data."""
    return candles["close"].rolling("3D").mean()


def test_time_based_rolling_window_passes_on_gaps() -> None:
    candles = synthetic_candles(250, seed=7, scenario=Scenario.GAPS, timeframe="1d")

    report = assert_no_lookahead(honest_time_based_rolling_mean, candles)

    assert report.non_missing_values > 0


def honest_expanding_max(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].expanding().max()


def test_expanding_max_passes_on_extreme_values() -> None:
    candles = synthetic_candles(250, seed=7, scenario=Scenario.MIXED)

    report = assert_no_lookahead(honest_expanding_max, candles)

    assert report.non_missing_values > 0


def honest_zero_volume_streak(candles: pd.DataFrame) -> pd.Series:
    """An int64 output: the number of consecutive zero-volume candles up to and including each
    row (only past and current data, never a fractional or float dtype)."""
    is_zero = (candles["volume"] == 0).to_numpy()
    streak = np.zeros(len(is_zero), dtype=np.int64)
    current = 0
    for i, flag in enumerate(is_zero):
        current = current + 1 if flag else 0
        streak[i] = current
    return pd.Series(streak, index=candles.index, name="zero_volume_streak")


def test_int64_output_passes() -> None:
    candles = synthetic_candles(250, seed=7, scenario=Scenario.FLAT_RUNS)

    report = assert_no_lookahead(honest_zero_volume_streak, candles)

    assert report.non_missing_values > 0


def honest_boolean_new_high(candles: pd.DataFrame) -> pd.Series:
    """A boolean output: whether the candle equals the running maximum so far."""
    close = candles["close"]
    return close >= close.cummax()


def test_boolean_output_passes() -> None:
    candles = synthetic_candles(250, seed=7, scenario=Scenario.MIXED)

    report = assert_no_lookahead(honest_boolean_new_high, candles)

    assert report.non_missing_values > 0


def honest_rolling_stats_as_dicts(candles: pd.DataFrame) -> pd.Series:
    """An object-dtype output: a plain dict per candle, holding NaN during the rolling warmup."""
    close = candles["close"]
    mean = close.rolling(5).mean()
    std = close.rolling(5).std()
    values = [
        {
            "mean": float(m) if pd.notna(m) else math.nan,
            "std": float(s) if pd.notna(s) else math.nan,
        }
        for m, s in zip(mean.to_numpy(), std.to_numpy(), strict=True)
    ]
    return pd.Series(values, index=candles.index, dtype=object)


def test_object_output_with_nan_inside_dicts_passes() -> None:
    candles = synthetic_candles(250, seed=7)

    report = assert_no_lookahead(honest_rolling_stats_as_dicts, candles)

    assert report.non_missing_values > 0


def test_frame_of_exactly_two_rows_passes() -> None:
    candles = synthetic_candles(2, seed=1)

    report = assert_no_lookahead(lambda df: df["close"].cumsum(), candles)

    assert report.cuts == (1,)
    assert report.non_missing_values > 0


def _integer_indexed_frame(n: int = 30) -> pd.DataFrame:
    """A tiny OHLCV frame indexed by strictly increasing, non-datetime integers."""
    close = 100.0 + np.arange(n, dtype=np.float64)
    index = pd.Index(np.arange(n, dtype=np.int64) * 2)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": close},
        index=index,
    )


def test_non_datetime_increasing_index_passes() -> None:
    """The harness does not require a ``DatetimeIndex``: any unique, increasing index works."""
    candles = _integer_indexed_frame()

    report = assert_no_lookahead(lambda df: df["close"].cumsum(), candles)

    assert report.non_missing_values > 0


def test_empty_ks_checks_only_the_full_frame_comparison() -> None:
    candles = synthetic_candles(250, seed=7)

    report = assert_no_lookahead(lambda df: df["close"].diff(), candles, ks=())

    assert report.ks == ()
    assert report.non_missing_values > 0


def test_max_cuts_of_one_still_finds_a_usable_cut() -> None:
    candles = synthetic_candles(250, seed=7)

    report = assert_no_lookahead(lambda df: df["close"].cumsum(), candles, max_cuts=1)

    assert len(report.cuts) == 1
    assert report.non_missing_values > 0


# --- T18: point-in-time edge cases (AC6, AC8) -----------------------------------------------


def close_is_up(candles: pd.DataFrame) -> bool:
    close = candles["close"]
    return bool(close.iloc[-1] > close.iloc[-2])


def close_is_up_by_candle(candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    return close > close.shift(1)


def recent_mean(candles: pd.DataFrame) -> float:
    return float(candles["close"].rolling(5).mean().iloc[-1])


def recent_mean_by_candle_warmup_dropped(candles: pd.DataFrame) -> pd.Series:
    """Series-valued reference that drops its own warmup instead of returning NaN for it."""
    return candles["close"].rolling(5).mean().dropna()


def test_reference_with_dropped_warmup_fails_early_and_passes_past_the_warmup() -> None:
    candles = synthetic_candles(60, seed=7)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            recent_mean, recent_mean_by_candle_warmup_dropped, candles, min_prefix=1
        )
    assert caught.value.violation.kind == "missing_reference"

    report = assert_no_lookahead_point_in_time(
        recent_mean, recent_mean_by_candle_warmup_dropped, candles, min_prefix=5
    )
    assert report.non_missing_values > 0


def test_point_in_time_function_that_adds_a_column_is_reported_as_input_mutated() -> None:
    """A different mutation style than the developer's self-test (adds a column instead of
    changing a value in place), to make sure the shape check, not just the value check, is hit."""

    def adds_a_column(candles: pd.DataFrame) -> bool:
        candles["extra"] = 0.0
        return close_is_up(candles)

    candles = synthetic_candles(60, seed=7)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(
            adds_a_column, close_is_up_by_candle, candles, min_prefix=2
        )

    assert caught.value.violation.kind == "input_mutated"
    assert caught.value.violation.func_name == adds_a_column.__qualname__


def test_nondeterministic_reference_is_reported_through_the_point_in_time_entry_point() -> None:
    """The developer's self-tests only exercise a nondeterministic ``func``; the ``reference`` is
    itself checked with ``assert_no_lookahead`` (Design 3.6), so its own nondeterminism must
    surface too, named by the reference, before ``func`` is ever called."""
    calls: list[int] = []

    def flaky_reference(candles: pd.DataFrame) -> pd.Series:
        calls.append(len(candles))
        close = candles["close"]
        bump = 0.0 if len(calls) % 2 else 1.0
        return close > (close.shift(1) + bump)

    candles = synthetic_candles(60, seed=7)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead_point_in_time(close_is_up, flaky_reference, candles, min_prefix=2)

    assert caught.value.violation.kind == "nondeterministic"
    assert caught.value.violation.func_name == flaky_reference.__qualname__


# --- Extra argument-validation coverage for the developer's deviation 3 (AC5, AC7) ---------
#
# The developer reported stricter validation than the spec's pseudocode signatures: non-int and
# boolean ``ks``/``cuts`` rejected, and ``min_prefix``/``max_cuts`` validated even when explicit
# ``cuts`` are given. The self-tests only exercise a non-int ``ks`` and an out-of-range ``cuts``;
# the cases below close the remaining gaps so the deviation is actually covered by a test, not
# just by reading the implementation.


def honest_cumsum(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].cumsum()


@pytest.mark.parametrize(
    "cuts",
    [
        pytest.param([1.5], id="float-cut"),
        pytest.param([True], id="bool-cut"),
    ],
)
def test_non_integer_and_boolean_cuts_are_rejected(cuts: list[object]) -> None:
    candles = synthetic_candles(30, seed=1)

    with pytest.raises(TypeError, match="cuts"):
        assert_no_lookahead(honest_cumsum, candles, cuts=cuts)  # type: ignore[arg-type]


def test_boolean_ks_are_rejected() -> None:
    candles = synthetic_candles(30, seed=1)

    with pytest.raises(TypeError, match="ks"):
        assert_no_lookahead(honest_cumsum, candles, ks=(True,))


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"min_prefix": 0}, id="min-prefix"),
        pytest.param({"max_cuts": 0}, id="max-cuts"),
    ],
)
def test_min_prefix_and_max_cuts_are_still_validated_with_explicit_cuts(
    kwargs: dict[str, int],
) -> None:
    candles = synthetic_candles(30, seed=1)

    with pytest.raises(ValueError, match=r"min_prefix|max_cuts"):
        assert_no_lookahead(honest_cumsum, candles, cuts=[5], **kwargs)


@pytest.mark.parametrize(
    "kwargs", [{"rtol": math.nan}, {"atol": math.inf}, {"rtol": -1e-9}, {"atol": -1e-9}]
)
def test_assert_no_lookahead_rejects_invalid_tolerances_directly(kwargs: dict[str, float]) -> None:
    """``values_equal`` has its own tolerance-validation tests; ``assert_no_lookahead`` shares the
    same helper but was not exercised with NaN/infinite tolerances directly."""
    candles = synthetic_candles(30, seed=1)

    with pytest.raises(ValueError, match=r"rtol|atol"):
        assert_no_lookahead(honest_cumsum, candles, **kwargs)
