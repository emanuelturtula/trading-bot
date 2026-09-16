"""Reusable look-ahead test harness (spec 003, CLAUDE.md rule 4).

The result at candle ``t`` computed with ``data[:t]`` must be identical to the one computed
with ``data[:t+k]`` truncated to ``t``. ``data[:t]`` is inclusive of ``t``; a *cut* is a prefix
length ``n`` whose candle is ``t = candles.index[n - 1]``.

Always import this module as ``tests.lookahead``: a second import path would create a second
``LookaheadError`` class that ``pytest.raises`` does not match.

The harness is pure (no globals, clock or randomness) and never relies on ``assert``
statements: violations raise ``LookaheadError`` and misuse raises ``TypeError``/``ValueError``.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, TypeGuard

import numpy as np
import numpy.typing as npt
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

type _Number = int | float | np.integer[Any] | np.floating[Any]
type _Positions = npt.NDArray[np.intp]


@dataclass(frozen=True, slots=True)
class Violation:
    """A single look-ahead or contract violation, as reported by ``LookaheadError``."""

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
    """Raised when the function under test violates CLAUDE.md rule 4 or the output contract."""

    violation: Violation

    def __init__(self, violation: Violation, *, frame_length: int | None = None) -> None:
        super().__init__(_format_violation(violation, frame_length))
        self.violation = violation
        self.frame_length = frame_length

    def __reduce__(self) -> tuple[Callable[..., LookaheadError], tuple[Violation, int | None]]:
        return _rebuild_error, (self.violation, self.frame_length)


def _rebuild_error(violation: Violation, frame_length: int | None) -> LookaheadError:
    return LookaheadError(violation, frame_length=frame_length)


@dataclass(frozen=True, slots=True)
class LookaheadReport:
    """Summary of a passing check."""

    cuts: tuple[int, ...]  # prefix lengths checked
    ks: tuple[int, ...]  # normalized requested ks (sorted, unique)
    calls: int  # invocations of the functions under test
    comparisons: int  # (n, k, output) comparisons made
    compared_values: int  # cells compared
    non_missing_values: int  # compared cells whose prefix value is not missing


def select_cuts(
    length: int, *, min_prefix: int = 1, max_cuts: int = DEFAULT_MAX_CUTS
) -> tuple[int, ...]:
    """Deterministic prefix lengths to check: a full sweep, or head, tail and evenly spaced cuts.

    The candidates are ``range(min_prefix, length)``: every prefix with at least one later candle.
    """
    _validate_selection(length, min_prefix, max_cuts)
    candidates = range(min_prefix, length)
    if len(candidates) <= max_cuts:
        return tuple(candidates)
    edge = min(EDGE_CUTS, max_cuts // 3)
    middle = np.rint(np.linspace(min_prefix, length - 1, max_cuts - 2 * edge)).astype(np.int64)
    selected = {*candidates[:edge], *candidates[len(candidates) - edge :], *middle.tolist()}
    return tuple(sorted(selected))


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
) -> LookaheadReport:
    """Check that a series-valued function gives the same result at ``t`` for ``data[:t]`` and
    ``data[:t+k]`` truncated to ``t``.

    ``func`` must return a ``Series``, a ``DataFrame`` or a ``Mapping[str, Series]`` stamped by
    labels of the input frame. Comparison is exact unless ``rtol``/``atol`` are given; a tolerance
    needs a comment at the call site justifying it.
    """
    plan = _make_plan(candles, ks, cuts, min_prefix, max_cuts, rtol, atol)
    evaluator = _Evaluator(func, plan.candles)
    tally = _Tally()
    _check_series(evaluator, plan, tally, retain_cache=False)
    return LookaheadReport(
        cuts=plan.cuts,
        ks=plan.ks,
        calls=evaluator.calls,
        comparisons=tally.comparisons,
        compared_values=tally.compared_values,
        non_missing_values=tally.non_missing_values,
    )


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
) -> LookaheadReport:
    """Check a last-candle function (``func(frame)`` is a result about ``frame.index[-1]``).

    Such a function cannot be checked on its own: each prefix yields one result about a different
    candle, so there is never a second computation of the same ``t``. It is therefore paired with
    a series-valued ``reference`` giving the same result for every candle of a frame. The
    reference is checked with ``assert_no_lookahead``; then ``func(data[:t])`` must equal the
    reference at ``t`` computed on ``data[:t+k]`` for ``k = 0``, the requested ``ks`` and the
    full frame.
    """
    plan = _make_plan(candles, ks, cuts, min_prefix, max_cuts, rtol, atol)
    reference_evaluator = _Evaluator(reference, plan.candles)
    tally = _Tally()
    _check_series(reference_evaluator, plan, tally, retain_cache=True)
    func_evaluator = _Evaluator(func, plan.candles)
    _check_deterministic_result(func_evaluator, plan)
    non_missing_results = 0
    for n in plan.cuts:
        actual = func_evaluator.call(n)
        actual_is_missing = _is_missing(actual)
        non_missing_results += 0 if actual_is_missing else 1
        for k in sorted({0, *plan.ks_for(n)}):
            tally.comparisons += 1
            tally.compared_values += 1
            tally.non_missing_values += 0 if actual_is_missing else 1
            _compare_point_in_time(
                actual,
                reference_evaluator.evaluate(n + k),
                plan,
                n,
                k,
                func_name=func_evaluator.name,
                reference_name=reference_evaluator.name,
            )
    if non_missing_results == 0:
        raise _vacuous_error(
            func_evaluator.name,
            plan,
            f"the function returned a missing result at all {len(plan.cuts)} cuts",
        )
    return LookaheadReport(
        cuts=plan.cuts,
        ks=plan.ks,
        calls=reference_evaluator.calls + func_evaluator.calls,
        comparisons=tally.comparisons,
        compared_values=tally.compared_values,
        non_missing_values=tally.non_missing_values,
    )


def values_equal(left: object, right: object, *, rtol: float = 0.0, atol: float = 0.0) -> bool:
    """Compare two results with the harness semantics (spec 003, Design 3.4).

    Missing values (``None``, NaN, ``NaT``, ``pd.NA``) are equal to each other only. Numbers use
    ``==`` or, when a tolerance is given, ``abs(left - right) <= atol + rtol * abs(left)``.
    Booleans only equal booleans. Mappings, lists, tuples, dataclass instances and Series are
    compared recursively; anything else must compare to a ``bool`` with ``==``.
    """
    _validate_tolerance(rtol, atol)
    return _values_equal(left, right, rtol, atol)


# --- Planning and argument validation -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    candles: pd.DataFrame  # private deep copy: the pristine data
    cuts: tuple[int, ...]
    ks: tuple[int, ...]
    rtol: float
    atol: float

    @property
    def length(self) -> int:
        return len(self.candles)

    def label(self, n: int) -> Hashable:
        label: Hashable = self.candles.index[n - 1]
        return label

    def ks_for(self, n: int) -> tuple[int, ...]:
        """Requested ``ks`` that fit after cut ``n``, plus the full-frame comparison."""
        return tuple(sorted({k for k in self.ks if n + k <= self.length} | {self.length - n}))


@dataclass(slots=True)
class _Tally:
    comparisons: int = 0
    compared_values: int = 0
    non_missing_values: int = 0


def _make_plan(
    candles: pd.DataFrame,
    ks: Sequence[int],
    cuts: Sequence[int] | None,
    min_prefix: int,
    max_cuts: int,
    rtol: float,
    atol: float,
) -> _Plan:
    if not isinstance(candles, pd.DataFrame):
        raise TypeError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    length = len(candles)
    if length < 2:
        raise ValueError(f"candles must have at least 2 rows, got {length}")
    if not candles.index.is_unique:
        raise ValueError("candles index must be unique")
    if not candles.index.is_monotonic_increasing:
        raise ValueError("candles index must be increasing")
    normalized_ks = _normalize_integers(ks, "ks")
    if any(k < 1 for k in normalized_ks):
        raise ValueError(f"every value in ks must be >= 1, got {list(ks)!r}")
    _validate_selection(length, min_prefix, max_cuts)
    if cuts is None:
        selected = select_cuts(length, min_prefix=min_prefix, max_cuts=max_cuts)
    else:
        selected = _normalize_integers(cuts, "cuts")
        if not selected:
            raise ValueError("explicit cuts must not be empty")
        if selected[0] < 1 or selected[-1] > length - 1:
            raise ValueError(
                f"every value in cuts must be in [1, {length - 1}], got {list(cuts)!r}"
            )
    _validate_tolerance(rtol, atol)
    return _Plan(candles.copy(deep=True), selected, normalized_ks, rtol, atol)


def _normalize_integers(values: Sequence[int], name: str) -> tuple[int, ...]:
    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool | np.bool_) or not isinstance(value, int | np.integer):
            raise TypeError(f"every value in {name} must be an int, got {value!r}")
        normalized.add(int(value))
    return tuple(sorted(normalized))


def _validate_selection(length: int, min_prefix: int, max_cuts: int) -> None:
    if length < 2:
        raise ValueError(f"length must be >= 2, got {length}")
    if not 1 <= min_prefix <= length - 1:
        raise ValueError(f"min_prefix must be in [1, {length - 1}], got {min_prefix}")
    if max_cuts < 1:
        raise ValueError(f"max_cuts must be >= 1, got {max_cuts}")


def _validate_tolerance(rtol: float, atol: float) -> None:
    for name, value in (("rtol", rtol), ("atol", atol)):
        if not (math.isfinite(value) and value >= 0.0):
            raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")


# --- Evaluation ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Output:
    name: str
    series: pd.Series[Any]
    positions: _Positions  # positions of the output labels in the candles index


@dataclass(frozen=True, slots=True)
class _Evaluation:
    outputs: tuple[_Output, ...]
    single: bool  # the function returned a Series (not a DataFrame or a Mapping)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(output.name for output in self.outputs)


class _Evaluator:
    """Calls one function on deep copies of prefixes of the pristine candles; caches outputs."""

    def __init__(self, func: Callable[[pd.DataFrame], object], candles: pd.DataFrame) -> None:
        self.func = func
        self.name = _qualname(func)
        self.calls = 0
        self._candles = candles
        self._cache: dict[int, _Evaluation] = {}

    def call(self, n: int) -> object:
        """Call the function on a deep copy of ``data[:t]`` and check that it is not mutated."""
        pristine = self._candles.iloc[:n]
        frame = pristine.copy(deep=True)
        self.calls += 1
        try:
            result = self.func(frame)
        except Exception as error:
            label = _render_label(self._candles.index[n - 1])
            error.add_note(f"while evaluating {self.name} on data[:t] with n={n}, t={label}")
            raise
        if not _same_frame(frame, pristine):
            raise self.error(
                "input_mutated", n, "the frame passed to the function was modified in place"
            )
        return result

    def evaluate(self, n: int) -> _Evaluation:
        cached = self._cache.get(n)
        if cached is None:
            cached = self.evaluate_uncached(n)
            self._cache[n] = cached
        return cached

    def evaluate_uncached(self, n: int) -> _Evaluation:
        return self._normalize(self.call(n), n)

    def forget_below(self, n: int) -> None:
        for length in [length for length in self._cache if length < n]:
            del self._cache[length]

    def error(self, kind: ViolationKind, n: int, detail: str) -> LookaheadError:
        return LookaheadError(
            Violation(
                kind=kind,
                func_name=self.name,
                output=None,
                prefix_length=n,
                t=self._candles.index[n - 1],
                k=None,
                stamp=None,
                prefix_value=None,
                extended_value=None,
                offending_count=0,
                detail=detail,
            ),
            frame_length=len(self._candles),
        )

    def _normalize(self, result: object, n: int) -> _Evaluation:
        pairs: list[tuple[str, object]]
        if isinstance(result, pd.Series):
            pairs = [("value" if result.name is None else str(result.name), result)]
        elif isinstance(result, pd.DataFrame):
            columns = list(result.columns)
            if not all(isinstance(column, str) for column in columns):
                raise self.error("invalid_output", n, f"DataFrame columns must be str: {columns!r}")
            if len(set(columns)) != len(columns):
                raise self.error(
                    "invalid_output", n, f"DataFrame columns are duplicated: {columns!r}"
                )
            pairs = [(str(column), result.iloc[:, i]) for i, column in enumerate(columns)]
        elif isinstance(result, Mapping):
            pairs = []
            for key, value in result.items():
                if not isinstance(key, str):
                    raise self.error("invalid_output", n, f"Mapping keys must be str, got {key!r}")
                pairs.append((key, value))
        else:
            raise self.error(
                "invalid_output",
                n,
                f"returned {type(result).__name__}; expected a Series, a DataFrame or a "
                "Mapping[str, Series]",
            )
        outputs = tuple(self._output(name, value, n) for name, value in pairs)
        return _Evaluation(outputs, single=isinstance(result, pd.Series))

    def _output(self, name: str, value: object, n: int) -> _Output:
        if not isinstance(value, pd.Series):
            raise self.error(
                "invalid_output", n, f"output {name!r} is a {type(value).__name__}, not a Series"
            )
        not_frame_labels = f"output {name!r} has labels that are not labels of the input frame"
        try:
            positions: _Positions = np.asarray(
                self._candles.index.get_indexer(value.index), dtype=np.intp
            )
        except (TypeError, ValueError) as error:
            raise self.error("invalid_output", n, f"{not_frame_labels} ({error})") from error
        if len(positions) > 0 and (positions.min() < 0 or positions.max() >= n):
            raise self.error(
                "invalid_output", n, f"{not_frame_labels} (index dtype {value.index.dtype})"
            )
        if not value.index.is_unique:
            raise self.error("invalid_output", n, f"output {name!r} has duplicated labels")
        if len(positions) > 1 and bool((np.diff(positions) <= 0).any()):
            raise self.error("invalid_output", n, f"output {name!r} labels are not increasing")
        return _Output(name, value, positions)


def _same_frame(frame: pd.DataFrame, pristine: pd.DataFrame) -> bool:
    return (
        frame.equals(pristine)
        and list(frame.index.names) == list(pristine.index.names)
        and list(frame.columns.names) == list(pristine.columns.names)
    )


def _qualname(func: object) -> str:
    name = getattr(func, "__qualname__", None)
    return name if isinstance(name, str) else repr(func)


# --- Comparison ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Finding:
    kind: ViolationKind
    output: str | None
    stamp: Hashable | None
    prefix_value: object
    extended_value: object
    count: int
    detail: str


def _check_series(evaluator: _Evaluator, plan: _Plan, tally: _Tally, *, retain_cache: bool) -> None:
    _check_deterministic(evaluator, plan)
    for n in plan.cuts:
        if not retain_cache:
            evaluator.forget_below(n)
        prefix = evaluator.evaluate(n)
        for k in plan.ks_for(n):
            finding = _first_difference(prefix, evaluator.evaluate(n + k), n, plan, tally)
            if finding is not None:
                raise _error_from(finding, evaluator.name, plan, n, k)
    if tally.non_missing_values == 0:
        raise _vacuous_error(
            evaluator.name,
            plan,
            f"{tally.compared_values} values compared in {tally.comparisons} comparisons "
            "and every one was missing",
        )


def _check_deterministic(evaluator: _Evaluator, plan: _Plan) -> None:
    length = plan.length
    first = evaluator.evaluate(length)
    second = evaluator.evaluate_uncached(length)
    finding = _first_difference(first, second, length, plan, _Tally(), strict_labels=True)
    if finding is not None:
        detail = f"two evaluations of the full frame differ ({finding.kind})"
        if finding.detail:
            detail = f"{detail}: {finding.detail}"
        nondeterministic = dataclasses.replace(finding, kind="nondeterministic", detail=detail)
        raise _error_from(nondeterministic, evaluator.name, plan, length, None)


def _vacuous_error(func_name: str, plan: _Plan, detail: str) -> LookaheadError:
    return LookaheadError(
        Violation(
            kind="vacuous",
            func_name=func_name,
            output=None,
            prefix_length=None,
            t=None,
            k=None,
            stamp=None,
            prefix_value=None,
            extended_value=None,
            offending_count=0,
            detail=detail,
        ),
        frame_length=plan.length,
    )


def _check_deterministic_result(evaluator: _Evaluator, plan: _Plan) -> None:
    length = plan.length
    first = evaluator.call(length)
    second = evaluator.call(length)
    if _values_equal(first, second, plan.rtol, plan.atol):
        return
    finding = _Finding(
        kind="nondeterministic",
        output=None,
        stamp=plan.label(length),
        prefix_value=first,
        extended_value=second,
        count=1,
        detail="two evaluations of the full frame differ",
    )
    raise _error_from(finding, evaluator.name, plan, length, None)


def _compare_point_in_time(
    actual: object,
    reference: _Evaluation,
    plan: _Plan,
    n: int,
    k: int,
    *,
    func_name: str,
    reference_name: str,
) -> None:
    """Compare ``func(data[:t])`` with the reference output at ``t`` from ``data[:t+k]``."""
    expected: dict[str, object] = {}
    for output in reference.outputs:
        row = int(np.searchsorted(output.positions, n - 1))
        if row == len(output.positions) or int(output.positions[row]) != n - 1:
            missing = _Finding(
                kind="missing_reference",
                output=output.name,
                stamp=plan.label(n),
                prefix_value=actual,
                extended_value=None,
                count=1,
                detail=f"the reference on data[:t+k] has no result at t for output {output.name!r}",
            )
            raise _error_from(missing, reference_name, plan, n, k)
        expected[output.name] = output.series.iloc[row]
    single_name = reference.outputs[0].name if reference.single else None
    expected_value: object = expected if single_name is None else expected[single_name]
    if _values_equal(actual, expected_value, plan.rtol, plan.atol):
        return
    mismatch = _Finding(
        kind="point_in_time_mismatch",
        output=single_name,
        stamp=plan.label(n),
        prefix_value=actual,
        extended_value=expected_value,
        count=1,
        detail="the function result on data[:t] differs from the reference at t on data[:t+k]",
    )
    raise _error_from(mismatch, func_name, plan, n, k)


def _error_from(
    finding: _Finding, func_name: str, plan: _Plan, n: int, k: int | None
) -> LookaheadError:
    return LookaheadError(
        Violation(
            kind=finding.kind,
            func_name=func_name,
            output=finding.output,
            prefix_length=n,
            t=plan.label(n),
            k=k,
            stamp=finding.stamp,
            prefix_value=finding.prefix_value,
            extended_value=finding.extended_value,
            offending_count=finding.count,
            detail=finding.detail,
        ),
        frame_length=plan.length,
    )


def _first_difference(
    prefix: _Evaluation,
    extended: _Evaluation,
    n: int,
    plan: _Plan,
    tally: _Tally,
    *,
    strict_labels: bool = False,
) -> _Finding | None:
    """First difference between two evaluations, restricted to labels before position ``n``."""
    if prefix.names != extended.names:
        return _Finding(
            kind="outputs_changed",
            output=None,
            stamp=None,
            prefix_value=prefix.names,
            extended_value=extended.names,
            count=0,
            detail=f"outputs with data[:t]: {list(prefix.names)!r}; "
            f"with data[:t+k]: {list(extended.names)!r}",
        )
    for prefix_output, extended_output in zip(prefix.outputs, extended.outputs, strict=True):
        tally.comparisons += 1
        finding = _compare_output(
            prefix_output, extended_output, n, plan, tally, strict_labels=strict_labels
        )
        if finding is not None:
            return finding
    return None


def _compare_output(
    prefix: _Output,
    extended: _Output,
    n: int,
    plan: _Plan,
    tally: _Tally,
    *,
    strict_labels: bool,
) -> _Finding | None:
    prefix_dtype = prefix.series.dtype
    extended_dtype = extended.series.dtype
    if prefix_dtype != extended_dtype:
        return _Finding(
            kind="dtype_mismatch",
            output=prefix.name,
            stamp=None,
            prefix_value=prefix_dtype,
            extended_value=extended_dtype,
            count=0,
            detail=f"dtype with data[:t]: {prefix_dtype}; with data[:t+k]: {extended_dtype}",
        )
    prefix_positions = prefix.positions
    extended_positions = extended.positions[: int(np.searchsorted(extended.positions, n))]
    if np.array_equal(prefix_positions, extended_positions):
        # Fast path: the same labels on both sides, in the same rows.
        return _compare_values(
            prefix,
            prefix.series.to_numpy(),
            extended.series.to_numpy()[: len(extended_positions)],
            prefix_positions,
            plan,
            tally,
        )
    appeared = np.flatnonzero(~np.isin(extended_positions, prefix_positions, assume_unique=True))
    if len(appeared) > 0:
        first_appeared = int(appeared[0])
        return _Finding(
            kind="result_appeared",
            output=prefix.name,
            stamp=plan.candles.index[int(extended_positions[first_appeared])],
            prefix_value=None,
            extended_value=extended.series.iloc[first_appeared],
            count=len(appeared),
            detail="data[:t+k] has results at or before t that data[:t] does not have",
        )
    disappeared_mask = ~np.isin(prefix_positions, extended_positions, assume_unique=True)
    if not strict_labels and len(extended.positions) > 0:
        # Dropping results from the start is allowed (windowed outputs).
        disappeared_mask &= prefix_positions >= extended.positions[0]
    disappeared = np.flatnonzero(disappeared_mask)
    if len(disappeared) > 0:
        first_disappeared = int(disappeared[0])
        return _Finding(
            kind="result_disappeared",
            output=prefix.name,
            stamp=plan.candles.index[int(prefix_positions[first_disappeared])],
            prefix_value=prefix.series.iloc[first_disappeared],
            extended_value=None,
            count=len(disappeared),
            detail="data[:t] has results at or before t that data[:t+k] no longer has",
        )
    common, prefix_rows, extended_rows = np.intersect1d(
        prefix_positions, extended_positions, assume_unique=True, return_indices=True
    )
    return _compare_values(
        prefix,
        prefix.series.to_numpy()[prefix_rows],
        extended.series.to_numpy()[extended_rows],
        common,
        plan,
        tally,
    )


def _compare_values(
    prefix: _Output,
    prefix_values: npt.NDArray[Any],
    extended_values: npt.NDArray[Any],
    common: _Positions,
    plan: _Plan,
    tally: _Tally,
) -> _Finding | None:
    """Compare aligned cells at the candle positions ``common`` (``value_mismatch``)."""
    mismatched, missing = _mismatch_masks(
        prefix_values, extended_values, prefix.series.dtype, plan.rtol, plan.atol
    )
    tally.compared_values += len(common)
    tally.non_missing_values += int(np.count_nonzero(~missing))
    offending = np.flatnonzero(mismatched)
    if len(offending) == 0:
        return None
    first = int(offending[0])
    return _Finding(
        kind="value_mismatch",
        output=prefix.name,
        stamp=plan.candles.index[int(common[first])],
        prefix_value=prefix_values[first],
        extended_value=extended_values[first],
        count=len(offending),
        detail="",
    )


def _mismatch_masks(
    prefix_values: npt.NDArray[Any],
    extended_values: npt.NDArray[Any],
    dtype: object,
    rtol: float,
    atol: float,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """Return (cells that differ, prefix cells that are missing) with ``values_equal`` rules."""
    tolerant = rtol != 0.0 or atol != 0.0
    if isinstance(dtype, np.dtype) and dtype.kind == "f":
        prefix_nan = np.isnan(prefix_values)
        equal = (prefix_values == extended_values) | (prefix_nan & np.isnan(extended_values))
        if tolerant:
            finite = np.isfinite(prefix_values) & np.isfinite(extended_values)
            with np.errstate(invalid="ignore", over="ignore"):
                close = np.abs(prefix_values - extended_values) <= atol + rtol * np.abs(
                    prefix_values
                )
            equal |= finite & close
        return ~equal, prefix_nan
    if isinstance(dtype, np.dtype) and (dtype.kind == "b" or (dtype.kind in "iu" and not tolerant)):
        return prefix_values != extended_values, np.zeros(len(prefix_values), dtype=np.bool_)
    mismatched = np.fromiter(
        (
            not _values_equal(left, right, rtol, atol)
            for left, right in zip(prefix_values, extended_values, strict=True)
        ),
        dtype=np.bool_,
        count=len(prefix_values),
    )
    missing = np.fromiter(
        (_is_missing(value) for value in prefix_values), dtype=np.bool_, count=len(prefix_values)
    )
    return mismatched, missing


# --- Reporting ----------------------------------------------------------------------------

_NO_RESULT = "<no result>"


def _render_label(label: object) -> str:
    isoformat = getattr(label, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(label)


def _render_value(value: object) -> str:
    return repr(value.item() if isinstance(value, np.generic) else value)


def _format_violation(violation: Violation, frame_length: int | None) -> str:
    lines = [f"look-ahead check failed in {violation.func_name}: {violation.kind}"]
    if violation.output is not None:
        lines.append(f"  output: {violation.output}")
    n = violation.prefix_length
    if n is not None and violation.t is not None:
        lines.append(f"  cut: t={_render_label(violation.t)} (prefix length n={n})")
    if n is not None and violation.k is not None:
        full = " (full frame)" if frame_length == n + violation.k else ""
        lines.append(
            f"  compared with: data[:t+k] with k={violation.k} (prefix length {n + violation.k})"
            f"{full}"
        )
    if violation.stamp is not None:
        lines.append(f"  first offending candle: {_render_label(violation.stamp)}")
        lines.extend(_value_lines(violation))
        lines.append(f"  offending candles at or before t: {violation.offending_count}")
    if violation.detail:
        lines.append(f"  detail: {violation.detail}")
    lines.append(f"  hint: {_hint(violation.kind)}")
    return "\n".join(lines)


def _value_lines(violation: Violation) -> list[str]:
    match violation.kind:
        case "point_in_time_mismatch" | "missing_reference":
            labels = ("function on data[:t]", "reference on data[:t+k]")
        case "nondeterministic":
            labels = ("first run", "second run")
        case _:
            labels = ("with data[:t]", "with data[:t+k]")
    prefix_value = (
        _NO_RESULT if violation.kind == "result_appeared" else _render_value(violation.prefix_value)
    )
    extended_value = (
        _NO_RESULT
        if violation.kind in {"result_disappeared", "missing_reference"}
        else _render_value(violation.extended_value)
    )
    width = max(len(label) for label in labels) + 1
    return [
        f"  {labels[0] + ':':<{width}} {prefix_value}",
        f"  {labels[1] + ':':<{width}} {extended_value}",
    ]


def _hint(kind: ViolationKind) -> str:
    match kind:
        case "value_mismatch":
            return (
                "a value at or before t changed when later candles were appended; look for "
                "shift(-n), centered windows, bfill/interpolate or statistics over the whole frame"
            )
        case "dtype_mismatch":
            return (
                "the output dtype depends on later candles; make it independent of the frame "
                "length, for example with an explicit astype()"
            )
        case "result_appeared":
            return (
                "a result for a candle at or before t only exists once later candles arrive; look "
                "for filters, joins or dropna() on conditions that read later rows"
            )
        case "result_disappeared":
            return (
                "a result for a candle at or before t was removed when later candles were "
                "appended; only results at the start of the output may be dropped"
            )
        case "outputs_changed":
            return "return the same output names, in the same order, for every frame length"
        case "point_in_time_mismatch":
            return (
                "the last-candle result differs from the reference at t; the function may decide "
                "about an earlier candle or read candles after t, or the reference may not "
                "describe the same result"
            )
        case "missing_reference":
            return (
                "the reference must return a result for every candle of the frame it receives; "
                "evaluate it over the whole frame (pass the full length as the window)"
            )
        case "invalid_output":
            return (
                "return a Series, a DataFrame or a Mapping[str, Series] stamped with labels of "
                "the input frame; for a function that returns one result about the last candle "
                "use assert_no_lookahead_point_in_time with a reference"
            )
        case "input_mutated":
            return "do not modify the input frame; work on a copy or use non-mutating operations"
        case "nondeterministic":
            return (
                "the same frame gave different results; remove hidden state such as caches, "
                "counters, randomness or the clock"
            )
        case "vacuous":
            return (
                "nothing was really compared; use a frame longer than the warmup, or a larger "
                "min_prefix, so that some compared values are not missing"
            )


# --- values_equal -------------------------------------------------------------------------


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float | np.floating):
        return math.isnan(value)
    if isinstance(value, np.datetime64 | np.timedelta64):
        return bool(np.isnat(value))
    if isinstance(value, Decimal):
        return value.is_nan()
    return False


def _is_bool(value: object) -> bool:
    return isinstance(value, bool | np.bool_)


def _is_number(value: object) -> TypeGuard[_Number]:
    return isinstance(value, int | float | np.integer | np.floating) and not _is_bool(value)


def _as_python_number(value: _Number) -> int | float:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _values_equal(left: object, right: object, rtol: float, atol: float) -> bool:
    left_missing = _is_missing(left)
    right_missing = _is_missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing
    if _is_bool(left) or _is_bool(right):
        return _is_bool(left) and _is_bool(right) and bool(left) == bool(right)
    if _is_number(left) and _is_number(right):
        return _numbers_equal(_as_python_number(left), _as_python_number(right), rtol, atol)
    if isinstance(left, pd.Series) or isinstance(right, pd.Series):
        return _series_equal(left, right, rtol, atol)
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return _mappings_equal(left, right, rtol, atol)
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        return _sequences_equal(left, right, rtol, atol)
    if _is_dataclass_instance(left) or _is_dataclass_instance(right):
        return _dataclasses_equal(left, right, rtol, atol)
    result = left == right
    if not isinstance(result, bool | np.bool_):
        raise TypeError(
            f"comparing {type(left).__name__} with {type(right).__name__} with == "
            f"produced {type(result).__name__}, not a bool"
        )
    return bool(result)


def _numbers_equal(left: int | float, right: int | float, rtol: float, atol: float) -> bool:
    if left == right:
        return True
    if (rtol == 0.0 and atol == 0.0) or not (math.isfinite(left) and math.isfinite(right)):
        return False
    return abs(left - right) <= atol + rtol * abs(left)


def _series_equal(left: object, right: object, rtol: float, atol: float) -> bool:
    if not (isinstance(left, pd.Series) and isinstance(right, pd.Series)):
        return False
    if left.dtype != right.dtype or not left.index.equals(right.index):
        return False
    return all(
        _values_equal(a, b, rtol, atol) for a, b in zip(left.tolist(), right.tolist(), strict=True)
    )


def _mappings_equal(left: object, right: object, rtol: float, atol: float) -> bool:
    if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
        return False
    if left.keys() != right.keys():
        return False
    return all(_values_equal(left[key], right[key], rtol, atol) for key in left)


def _sequences_equal(left: object, right: object, rtol: float, atol: float) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        pairs = (left, right)
    elif isinstance(left, tuple) and isinstance(right, tuple):
        pairs = (list(left), list(right))
    else:
        return False
    if len(pairs[0]) != len(pairs[1]):
        return False
    return all(_values_equal(a, b, rtol, atol) for a, b in zip(*pairs, strict=True))


def _is_dataclass_instance(value: object) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _dataclasses_equal(left: object, right: object, rtol: float, atol: float) -> bool:
    if type(left) is not type(right) or isinstance(left, type):
        return False
    if not dataclasses.is_dataclass(left):
        return False
    return all(
        _values_equal(getattr(left, field.name), getattr(right, field.name), rtol, atol)
        for field in dataclasses.fields(left)
    )
