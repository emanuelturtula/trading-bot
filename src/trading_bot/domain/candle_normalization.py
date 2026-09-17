"""Candle normalization: from a provider frame to canonical candles (spec 010, Design 3).

``normalize_candles`` is the shared, pure path from whatever a market data provider returns to
the candle frame contract of spec 004 with the canonical labels of spec 009 (D21):

- the index is converted to UTC with unit ``us``, named ``None`` and sorted; a naive index is
  rejected, never localized;
- the five OHLCV columns are selected case-insensitively (other columns are ignored) and cast to
  ``float64``;
- rows with a bad label or bad values are dropped, never repaired (decision D32), and each one is
  reported once as a ``DroppedRow`` with the first matching ``DropReason``.

Anything structurally ambiguous raises ``CandleNormalizationError``. The domain cannot log, so
the data layer logs the returned report. Every row rule is row-local or compares rows with the
same label, so the output for a prefix of the input is a prefix of the output (CLAUDE.md
rule 4). Messages never echo raw column labels, and dtype names only as a bounded ``repr()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd
from pandas.api.types import is_float_dtype, is_integer_dtype

from trading_bot.domain.candles import OHLCV_COLUMNS, PRICE_COLUMNS, validate_candles
from trading_bot.domain.market_calendar.sessions import CandleLabelError, MarketCalendar
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = [
    "CandleNormalizationError",
    "DropReason",
    "DroppedRow",
    "NormalizationErrorKind",
    "NormalizedCandles",
    "normalize_candles",
]

_ECHO_WIDTH: Final = 32  # characters of a dtype name echo, quotes included
_NO_REASON: Final = -1
_ONE_MICROSECOND: Final = timedelta(microseconds=1)
_CANONICAL_INDEX_DTYPE: Final = pd.DatetimeTZDtype(unit="us", tz="UTC")

type _Codes = npt.NDArray[np.int8]
type _BoolArray = npt.NDArray[np.bool_]
type _FloatMatrix = npt.NDArray[np.float64]


class NormalizationErrorKind(StrEnum):
    """Why a provider frame cannot be normalized, in the order the checks run (Design 3.1)."""

    INDEX_TYPE = "index_type"
    NAIVE_INDEX = "naive_index"
    MULTIINDEX_COLUMNS = "multiindex_columns"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    MISSING_COLUMN = "missing_column"
    NON_NUMERIC_COLUMN = "non_numeric_column"


class CandleNormalizationError(ValueError):
    """A provider frame whose structure is ambiguous. ``str()`` is ``"<kind>: <description>"``."""

    kind: NormalizationErrorKind
    column: str | None  # the canonical column name, when the kind is about one column

    def __init__(
        self, kind: NormalizationErrorKind, message: str, *, column: str | None = None
    ) -> None:
        # args holds only the positional arguments, so the default exception pickling
        # (``cls(*args)`` plus ``__dict__``) restores every field.
        super().__init__(kind, message)
        self.kind = kind
        self.column = column
        self._message = message

    def __str__(self) -> str:
        return f"{self.kind.value}: {self._message}"


class DropReason(StrEnum):
    """Why a row was dropped. Declared in precedence order, ``off_grid`` once (Design 3.3)."""

    MISSING_TIMESTAMP = "missing_timestamp"
    OUTSIDE_CALENDAR = "outside_calendar"
    NOT_A_SESSION = "not_a_session"
    OUTSIDE_SESSION = "outside_session"
    OFF_GRID = "off_grid"
    MISSING_VALUE = "missing_value"
    INFINITE_VALUE = "infinite_value"
    NON_POSITIVE_PRICE = "non_positive_price"
    NEGATIVE_VOLUME = "negative_volume"
    INCONSISTENT_RANGE = "inconsistent_range"
    DUPLICATE = "duplicate"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"


_REASONS: Final = tuple(DropReason)


@dataclass(frozen=True, slots=True, kw_only=True)
class DroppedRow:
    """One dropped row of the raw frame and the first reason that matched it."""

    position: int  # 0-based row position in the raw frame
    label: pd.Timestamp | None  # the label in UTC (input unit kept); None for NaT
    reason: DropReason


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class NormalizedCandles:
    """The canonical frame and the report of dropped rows. Compared by identity (a frame)."""

    candles: pd.DataFrame  # the canonical frame (Design 3.2)
    dropped: tuple[DroppedRow, ...]  # in input position order


def normalize_candles(
    raw: pd.DataFrame, timeframe: Timeframe, *, calendar: MarketCalendar
) -> NormalizedCandles:
    """Normalize a provider frame to canonical candles and report every dropped row.

    ``raw`` needs a tz-aware ``DatetimeIndex`` and one column matching each of ``open``,
    ``high``, ``low``, ``close`` and ``volume`` (case-insensitive, surrounding whitespace
    ignored) with an integer or float dtype; otherwise ``CandleNormalizationError`` is raised.
    Each row gets the first matching ``DropReason`` of Design 3.3, and rows with a reason are
    dropped. The result is a new frame that passes ``validate_candles`` with a
    ``datetime64[us, UTC]`` index; ``raw`` is never modified. Arguments of the wrong type raise
    ``TypeError``.
    """
    if not isinstance(raw, pd.DataFrame):
        raise TypeError(f"raw must be a pandas DataFrame, got {type(raw).__name__}")
    if not isinstance(timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    index = _checked_index(raw.index)
    values = _ohlcv_values(raw)
    labels = index.tz_convert("UTC")
    codes = _label_codes(labels, timeframe, calendar)
    _assign_value_codes(codes, values)
    _assign_duplicate_codes(codes, labels, values)

    kept = np.flatnonzero(codes == _NO_REASON)
    kept_index = pd.DatetimeIndex(labels[kept].as_unit("us").rename(None))
    candles = pd.DataFrame(
        values[kept], index=kept_index, columns=list(OHLCV_COLUMNS), dtype=np.float64
    ).sort_index(kind="stable")
    missing_code = _code(DropReason.MISSING_TIMESTAMP)
    report = tuple(
        DroppedRow(
            position=int(position),
            label=None if codes[position] == missing_code else labels[int(position)],
            reason=_REASONS[codes[position]],
        )
        for position in np.flatnonzero(codes != _NO_REASON)
    )
    return NormalizedCandles(candles=validate_candles(candles), dropped=report)


def _checked_index(index: object) -> pd.DatetimeIndex:
    """Structural checks 1 and 2: a tz-aware ``DatetimeIndex``."""
    if not isinstance(index, pd.DatetimeIndex):
        raise CandleNormalizationError(
            NormalizationErrorKind.INDEX_TYPE,
            "the candle index must be a pandas DatetimeIndex of open times, "
            f"got {_echo(type(index).__name__)}",
        )
    if index.tz is None:
        raise CandleNormalizationError(
            NormalizationErrorKind.NAIVE_INDEX,
            "the candle index must be timezone-aware; a naive index is never localized",
        )
    return index


def _ohlcv_values(raw: pd.DataFrame) -> _FloatMatrix:
    """Structural checks 3 to 6, then the matched columns as a ``float64`` matrix (rows x 5)."""
    columns = raw.columns
    if isinstance(columns, pd.MultiIndex):
        raise CandleNormalizationError(
            NormalizationErrorKind.MULTIINDEX_COLUMNS,
            "the columns must be a flat index, got a MultiIndex (a multi-ticker shape)",
        )
    matches: dict[str, list[int]] = {name: [] for name in OHLCV_COLUMNS}
    for position, label in enumerate(columns):
        if isinstance(label, str) and (key := label.strip().lower()) in matches:
            matches[key].append(position)
    for name in OHLCV_COLUMNS:
        if len(matches[name]) > 1:
            raise CandleNormalizationError(
                NormalizationErrorKind.AMBIGUOUS_COLUMN,
                f"more than one column matches {name!r}",
                column=name,
            )
    for name in OHLCV_COLUMNS:
        if not matches[name]:
            raise CandleNormalizationError(
                NormalizationErrorKind.MISSING_COLUMN,
                f"no column matches {name!r}",
                column=name,
            )
    selected = [raw.iloc[:, matches[name][0]] for name in OHLCV_COLUMNS]
    for name, series in zip(OHLCV_COLUMNS, selected, strict=True):
        if not (is_integer_dtype(series.dtype) or is_float_dtype(series.dtype)):
            raise CandleNormalizationError(
                NormalizationErrorKind.NON_NUMERIC_COLUMN,
                f"column {name!r} must have an integer or float dtype, "
                f"got {_echo(str(series.dtype))}",
                column=name,
            )
    return np.column_stack(
        [series.to_numpy(dtype=np.float64, na_value=np.nan) for series in selected]
    ).reshape(len(raw), len(OHLCV_COLUMNS))


def _label_codes(
    labels: pd.DatetimeIndex, timeframe: Timeframe, calendar: MarketCalendar
) -> _Codes:
    """Reason codes for the label reasons (precedence 1 to 4); ``_NO_REASON`` elsewhere."""
    codes = np.full(len(labels), _NO_REASON, dtype=np.int8)
    missing = np.asarray(labels.isna(), dtype=np.bool_)
    _assign(codes, missing, DropReason.MISSING_TIMESTAMP)
    outside = np.asarray(
        (labels < pd.Timestamp(calendar.coverage_start))
        | (labels >= pd.Timestamp(calendar.coverage_end)),
        dtype=np.bool_,
    )
    _assign(codes, outside, DropReason.OUTSIDE_CALENDAR)
    # A label with sub-microsecond precision is never a grid label, and to_utc rejects it.
    _assign(codes, np.asarray(labels.nanosecond != 0, dtype=np.bool_), DropReason.OFF_GRID)

    candidates = np.flatnonzero(codes == _NO_REASON)
    if len(candidates) == 0:
        return codes
    candidate_labels = labels[candidates].as_unit("us")
    # One grid query over the span of the remaining labels; the calendar is asked for the kind
    # of each distinct label outside the grid only.
    slots = calendar.candle_slots(
        timeframe,
        to_utc(candidate_labels.min()),
        to_utc(candidate_labels.max()) + _ONE_MICROSECOND,
    )
    grid = pd.DatetimeIndex([slot.label for slot in slots], dtype=_CANONICAL_INDEX_DTYPE)
    rejected = candidates[~np.asarray(candidate_labels.isin(grid), dtype=np.bool_)]
    rejection_codes: dict[pd.Timestamp, int] = {}
    for position in rejected:
        label = labels[position]
        if label not in rejection_codes:
            rejection_codes[label] = _rejection_code(calendar, timeframe, label)
        codes[position] = rejection_codes[label]
    return codes


def _rejection_code(calendar: MarketCalendar, timeframe: Timeframe, label: pd.Timestamp) -> int:
    """The code of the calendar's ``CandleLabelError`` kind for ``label``, if it rejects it."""
    code: int = _NO_REASON
    try:
        calendar.candle_slot(timeframe, label)
    except CandleLabelError as error:
        code = _code(DropReason(error.kind.value))
    return code


def _assign_value_codes(codes: _Codes, values: _FloatMatrix) -> None:
    """Reason codes for the value reasons (precedence 5 to 9) of rows without a reason yet."""
    prices = values[:, : len(PRICE_COLUMNS)]
    open_, high, low, close = prices.T
    volume = values[:, len(PRICE_COLUMNS)]
    _assign(codes, np.isnan(values).any(axis=1), DropReason.MISSING_VALUE)
    _assign(codes, np.isinf(values).any(axis=1), DropReason.INFINITE_VALUE)
    _assign(codes, (prices <= 0.0).any(axis=1), DropReason.NON_POSITIVE_PRICE)
    _assign(codes, volume < 0.0, DropReason.NEGATIVE_VOLUME)
    inconsistent = (high < low) | (open_ < low) | (open_ > high) | (close < low) | (close > high)
    _assign(codes, inconsistent, DropReason.INCONSISTENT_RANGE)


def _assign_duplicate_codes(codes: _Codes, labels: pd.DatetimeIndex, values: _FloatMatrix) -> None:
    """Reason codes for repeated labels among the rows that survived every other rule.

    Copies with equal values keep the first one (``duplicate``); if any copy differs, every
    copy is dropped (``conflicting_duplicate``), because nothing tells which one is right.
    """
    survivors = np.flatnonzero(codes == _NO_REASON)
    repeated = survivors[np.asarray(labels[survivors].duplicated(keep=False), dtype=np.bool_)]
    groups: dict[pd.Timestamp, list[int]] = {}
    for position in repeated:
        groups.setdefault(labels[position], []).append(int(position))
    for members in groups.values():
        rows = values[members]
        if bool((rows == rows[0]).all()):
            codes[members[1:]] = _code(DropReason.DUPLICATE)
        else:
            codes[members] = _code(DropReason.CONFLICTING_DUPLICATE)


def _assign(codes: _Codes, rows: _BoolArray, reason: DropReason) -> None:
    """Give ``reason`` to the ``rows`` that have no reason yet (first match wins)."""
    codes[(codes == _NO_REASON) & rows] = _code(reason)


def _code(reason: DropReason) -> int:
    return _REASONS.index(reason)


def _echo(text: str) -> str:
    """``repr()`` of the longest prefix of ``text`` whose rendering is at most 32 characters.

    Escapes can make ``repr()`` up to 10 times longer than its input, so the prefix shrinks
    until the rendering fits: provider-controlled text stays single-line and bounded.
    """
    prefix = text[:_ECHO_WIDTH]
    while len(rendered := repr(prefix)) > _ECHO_WIDTH:
        prefix = prefix[:-1]
    return rendered
