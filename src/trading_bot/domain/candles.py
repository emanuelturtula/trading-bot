"""The candle frame contract and its validator (spec 004, Design 4).

A candle frame is a ``pd.DataFrame`` indexed by candle **open** times (a tz-aware UTC
``DatetimeIndex`` without ``NaT``, unique and strictly increasing, any unit and name) whose
columns are exactly ``OHLCV_COLUMNS``, all numpy ``float64``, with finite values, positive prices,
non-negative volume, ``high >= low`` and ``open``/``close`` within ``[low, high]``. Empty frames
that meet the structural rules are valid.

``validate_candles`` checks and never coerces: normalizing provider data belongs to the data
layer. Every check is row-local or compares adjacent labels, so every prefix of a valid frame is
valid.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

import numpy as np
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

_ECHO_LIMIT: Final = 32  # characters of a label or zone echoed in error messages
_ECHO_WIDTH: Final = 34  # columns of that echo once rendered with repr()
_LABELS_SHOWN: Final = 8  # column labels echoed in error messages

type _BoolArray = np.ndarray[tuple[int, ...], np.dtype[np.bool_]]


class CandleErrorKind(StrEnum):
    """What a candle frame violates, in the order the checks run."""

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
    """A candle frame breaks the contract. ``str()`` is one line with the located fields."""

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
    ) -> None:
        # args holds only the positional arguments, so the default exception pickling
        # (``cls(*args)`` plus ``__dict__``) restores every field.
        super().__init__(kind, message)
        self.kind = kind
        self.column = column
        self.timestamp = timestamp
        self.position = position
        self.count = count
        self._description = _describe(kind, message, column, timestamp, position, count)

    def __str__(self) -> str:
        return self._description


class CandleIndexError(CandleValidationError):
    """The index is not a unique, increasing, UTC ``DatetimeIndex`` without ``NaT``."""


class CandleColumnsError(CandleValidationError):
    """The column labels or dtypes break the contract."""


class CandleValuesError(CandleValidationError):
    """A cell is missing, infinite or inconsistent with the OHLCV rules."""


def validate_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Return ``candles`` itself if it meets the contract; raise ``CandleValidationError`` if not.

    The frame is never copied or modified. Checks run in ``CandleErrorKind`` order and the first
    failing check raises, reporting its earliest offending row. A non-``DataFrame`` argument
    raises ``TypeError``.
    """
    if not isinstance(candles, pd.DataFrame):
        raise TypeError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    index = _check_index(candles)
    _check_columns(candles)
    _check_values(candles, index)
    return candles


def _check_index(candles: pd.DataFrame) -> pd.DatetimeIndex:
    index = candles.index
    if not isinstance(index, pd.DatetimeIndex):
        raise CandleIndexError(
            CandleErrorKind.INDEX_TYPE,
            f"candle index must be a pandas DatetimeIndex, got {type(index).__name__}",
        )
    if index.tz is None:
        raise CandleIndexError(
            CandleErrorKind.TIMEZONE, "candle index must be tz-aware UTC, got a naive index"
        )
    if index.dtype != pd.DatetimeTZDtype(unit=index.unit, tz="UTC"):
        raise CandleIndexError(
            CandleErrorKind.TIMEZONE,
            f"candle index must be tz-aware UTC, got time zone {_echo(str(index.tz))}",
        )
    # hasnans, is_unique and is_monotonic_increasing are cached by pandas; offending positions
    # are only computed for invalid frames.
    if index.hasnans:
        raise _row_error(
            CandleIndexError,
            CandleErrorKind.MISSING_TIMESTAMP,
            "candle open times must not be NaT",
            np.asarray(index.isna(), dtype=np.bool_),
            None,
        )
    if not index.is_unique:
        raise _row_error(
            CandleIndexError,
            CandleErrorKind.DUPLICATE_TIMESTAMP,
            "candle open times must be unique",
            index.duplicated(keep="first"),
            index,
        )
    if not index.is_monotonic_increasing:
        earlier = np.zeros(len(index), dtype=np.bool_)
        earlier[1:] = index[1:] < index[:-1]
        raise _row_error(
            CandleIndexError,
            CandleErrorKind.UNSORTED_INDEX,
            "candle open times must be strictly increasing",
            earlier,
            index,
        )
    return index


def _check_columns(candles: pd.DataFrame) -> None:
    columns = candles.columns
    labels = columns[: len(OHLCV_COLUMNS) + 1].tolist()
    exact = len(labels) == len(OHLCV_COLUMNS) and all(
        isinstance(label, str) and label == expected
        for label, expected in zip(labels, OHLCV_COLUMNS, strict=True)
    )
    if isinstance(columns, pd.MultiIndex) or not exact:
        raise CandleColumnsError(
            CandleErrorKind.COLUMNS,
            f"candle columns must be exactly {_echo_labels(OHLCV_COLUMNS, 0)}, "
            f"got {'a MultiIndex ' if isinstance(columns, pd.MultiIndex) else ''}"
            f"{_echo_labels(columns[:_LABELS_SHOWN].tolist(), len(columns))}",
        )
    float64 = np.dtype(np.float64)
    for column, dtype in zip(OHLCV_COLUMNS, candles.dtypes.tolist(), strict=True):
        if not (isinstance(dtype, np.dtype) and dtype == float64):
            raise CandleColumnsError(
                CandleErrorKind.DTYPE,
                f"candle columns must have dtype float64, got {_echo(str(dtype))}",
                column=column,
            )


def _check_values(candles: pd.DataFrame, index: pd.DatetimeIndex) -> None:
    values = candles.to_numpy(dtype=np.float64, copy=False)
    if (missing := np.isnan(values)).any():
        raise _cell_error(
            CandleErrorKind.MISSING_VALUE,
            "candle values must not be NaN",
            missing,
            OHLCV_COLUMNS,
            index,
        )
    if (infinite := np.isinf(values)).any():
        raise _cell_error(
            CandleErrorKind.INFINITE_VALUE,
            "candle values must be finite",
            infinite,
            OHLCV_COLUMNS,
            index,
        )
    open_, high, low, close, volume = values.T
    prices = values[:, : len(PRICE_COLUMNS)]
    if (non_positive := prices <= 0.0).any():
        raise _cell_error(
            CandleErrorKind.NON_POSITIVE_PRICE,
            "candle prices must be greater than zero",
            non_positive,
            PRICE_COLUMNS,
            index,
        )
    if (negative := volume < 0.0).any():
        raise _row_error(
            CandleValuesError,
            CandleErrorKind.NEGATIVE_VOLUME,
            "candle volume must not be negative",
            negative,
            index,
            column="volume",
        )
    if (inverted := high < low).any():
        raise _row_error(
            CandleValuesError,
            CandleErrorKind.HIGH_BELOW_LOW,
            "candle high must not be below low",
            inverted,
            index,
        )
    outside = np.column_stack(((open_ < low) | (open_ > high), (close < low) | (close > high)))
    if outside.any():
        raise _cell_error(
            CandleErrorKind.BODY_OUTSIDE_RANGE,
            "candle open and close must be within [low, high]",
            outside,
            ("open", "close"),
            index,
        )


def _row_error(
    error_type: type[CandleValidationError],
    kind: CandleErrorKind,
    message: str,
    rows: _BoolArray,
    index: pd.DatetimeIndex | None,
    *,
    column: str | None = None,
) -> CandleValidationError:
    """The error for offending ``rows``: first position, its label (if ``index``) and the count."""
    position = int(np.argmax(rows))
    return error_type(
        kind,
        message,
        column=column,
        timestamp=None if index is None else index[position],
        position=position,
        count=int(np.count_nonzero(rows)),
    )


def _cell_error(
    kind: CandleErrorKind,
    message: str,
    cells: _BoolArray,
    columns: tuple[str, ...],
    index: pd.DatetimeIndex,
) -> CandleValidationError:
    """The error for offending ``cells`` (rows x ``columns``): first row, then first column."""
    rows = cells.any(axis=1)
    column = columns[int(np.argmax(cells[int(np.argmax(rows))]))]
    return _row_error(CandleValuesError, kind, message, rows, index, column=column)


def _describe(
    kind: CandleErrorKind,
    message: str,
    column: str | None,
    timestamp: pd.Timestamp | None,
    position: int | None,
    count: int,
) -> str:
    details: list[str] = []
    if column is not None:
        details.append(f"column={column!r}")
    if timestamp is not None:
        details.append(f"timestamp={timestamp.isoformat()}")
    if position is not None:
        details.append(f"position={position}")
        details.append(f"count={count}")
    located = f" [{', '.join(details)}]" if details else ""
    return f"{kind.value}: {message}{located}"


def _echo(value: object) -> str:
    """A bounded, escaped, single-line rendering of a label or zone for error messages.

    ``repr()`` of the longest prefix (at most 32 characters) whose rendering fits 34 columns:
    escapes can make ``repr()`` up to 10 times longer than its input.
    """
    prefix = (value if isinstance(value, str) else repr(value))[:_ECHO_LIMIT]
    while len(rendered := repr(prefix)) > _ECHO_WIDTH:
        prefix = prefix[:-1]
    return rendered


def _echo_labels(labels: Sequence[object], total: int) -> str:
    shown = ", ".join(_echo(label) for label in labels[:_LABELS_SHOWN])
    more = f", ... ({total} labels)" if total > _LABELS_SHOWN else ""
    return f"[{shown}{more}]"
