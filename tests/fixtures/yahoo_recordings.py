"""Replay fixtures for the Yahoo provider (spec 011, Design 13.5; decisions D29 and D59).

Recordings in ``tests/fixtures/yahoo/`` keep yfinance's frame structure (index epoch seconds,
zone, unit and name; column order and dtypes) and an allowlist of chart metadata. Every price,
volume, dividend and capital-gain value is synthetic (``random-walk/1``, Design 13.4): no real
Yahoo data is stored. ``load_recording`` rebuilds a frame exactly as yfinance returned it,
``load_metadata`` reads the recorded chart metadata, and ``RecordedYahooClient`` replays both at
the ``YahooClient`` port, recording queries and raising scripted failures first-in, first-out.
``hand_frame`` holds the hourly candles of the hand-computed resampling case (Design 5.3), and
``yfinance_as_market_data_provider`` lets strict mypy check the provider against the Protocol
(AC27).

This module imports no yfinance, reads no clock and never touches the network. Always import it
as ``tests.fixtures.yahoo_recordings``.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason
from trading_bot.data.provider import MarketDataProvider
from trading_bot.data.yahoo.history import (
    ChartMetadata,
    HistoryQuery,
    YahooClient,
    YahooHistory,
    YahooInterval,
)
from trading_bot.data.yahoo.provider import YFinanceProvider
from trading_bot.domain.candles import OHLCV_COLUMNS

__all__ = [
    "HISTORY_FORMAT",
    "METADATA_FORMAT",
    "RECORDINGS_DIR",
    "RecordedYahooClient",
    "Recording",
    "as_yahoo_client",
    "hand_frame",
    "load_metadata",
    "load_recording",
    "yfinance_as_market_data_provider",
]

RECORDINGS_DIR: Final = Path(__file__).parent / "yahoo"
HISTORY_FORMAT: Final = "yfinance-history/1"
METADATA_FORMAT: Final = "yfinance-metadata/1"

_CANONICAL_INDEX: Final = pd.DatetimeTZDtype(unit="us", tz="UTC")
_EMPTY_FRAME_RECORDING: Final = "spy_1d_2025-11-24"
_UNITS: Final = ("s", "ms", "us", "ns")
_DTYPES: Final = ("float64", "int64")

type _Column = npt.NDArray[np.float64] | npt.NDArray[np.int64]
type _Unit = Literal["s", "ms", "us", "ns"]

# Design 5.3: label (UTC), open, high, low, close, volume. Synthetic values (decision D29).
_HAND_ROWS: Final[tuple[tuple[str, float, float, float, float, float], ...]] = (
    ("2024-11-27T14:30:00+00:00", 100.0, 102.0, 99.0, 101.0, 1000.0),
    ("2024-11-27T15:30:00+00:00", 101.0, 104.0, 100.0, 103.0, 2000.0),
    ("2024-11-27T17:30:00+00:00", 103.0, 105.0, 98.0, 99.0, 1500.0),
    ("2024-11-27T18:30:00+00:00", 99.0, 100.0, 97.0, 98.0, 800.0),
    ("2024-11-27T19:30:00+00:00", 98.0, 99.0, 96.0, 97.0, 900.0),
    ("2024-11-27T20:30:00+00:00", 97.0, 101.0, 97.0, 100.0, 3000.0),
    ("2024-11-29T14:30:00+00:00", 100.0, 103.0, 100.0, 102.0, 500.0),
    ("2024-11-29T15:30:00+00:00", 102.0, 106.0, 101.0, 105.0, 700.0),
    ("2024-11-29T16:30:00+00:00", 105.0, 105.0, 102.0, 104.0, 600.0),
    ("2024-12-02T14:30:00+00:00", 104.0, 108.0, 103.0, 107.0, 1200.0),
    ("2024-12-02T15:30:00+00:00", 107.0, 109.0, 106.0, 108.0, 1100.0),
    ("2024-12-02T16:30:00+00:00", 108.0, 110.0, 107.0, 109.0, 1000.0),
    ("2024-12-02T17:30:00+00:00", 109.0, 111.0, 105.0, 106.0, 900.0),
    ("2024-12-02T18:30:00+00:00", 106.0, 107.0, 104.0, 105.0, 700.0),
    ("2024-12-02T19:30:00+00:00", 105.0, 106.0, 102.0, 103.0, 650.0),
)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Recording:
    """One recorded ``Ticker.history`` call, rebuilt as yfinance returned it."""

    name: str
    symbol: str
    interval: YahooInterval
    frame: pd.DataFrame  # rebuilt exactly as Design 13.3
    seed: int


def load_recording(name: str) -> Recording:
    """The recording ``tests/fixtures/yahoo/<name>.json``, with a new frame on every call.

    The index is ``pd.to_datetime(epoch_seconds, unit="s", utc=True)`` converted to the recorded
    zone, cast to the recorded unit and named; each column gets its recorded dtype, in the
    recorded order, and ``null`` becomes NaN.
    """
    document = _document(RECORDINGS_DIR / f"{name}.json", HISTORY_FORMAT)
    call = _mapping(document.get("call"), "call")
    frame_document = _mapping(document.get("frame"), "frame")
    synthetic = _mapping(document.get("synthetic"), "synthetic")
    interval = _text(call.get("interval"), "call.interval")
    if interval not in ("1h", "1d"):
        raise ValueError(f"recording {name} has an unsupported interval")
    return Recording(
        name=name,
        symbol=_text(call.get("symbol"), "call.symbol"),
        interval=cast(YahooInterval, interval),
        frame=_frame(frame_document),
        seed=_integer(synthetic.get("seed"), "synthetic.seed"),
    )


def load_metadata() -> Mapping[str, Mapping[str, object]]:
    """The recorded chart metadata by symbol, as read-only mappings (``null`` kept as ``None``)."""
    document = _document(RECORDINGS_DIR / "metadata.json", METADATA_FORMAT)
    symbols = _mapping(document.get("symbols"), "symbols")
    return MappingProxyType(
        {
            symbol: MappingProxyType(_mapping(values, f"symbols.{symbol}"))
            for symbol, values in symbols.items()
        }
    )


def hand_frame() -> pd.DataFrame:
    """The canonical hourly rows of Design 5.3; ``provider_shaped(hand_frame())`` is the client
    frame. A new frame on every call."""
    index = pd.DatetimeIndex([pd.Timestamp(row[0]) for row in _HAND_ROWS], dtype=_CANONICAL_INDEX)
    values = np.array([row[1:] for row in _HAND_ROWS], dtype=np.float64)
    return pd.DataFrame(values, index=index, columns=list(OHLCV_COLUMNS), dtype=np.float64)


class RecordedYahooClient:
    """YahooClient replaying recordings; records queries; scripted failures first-in, first-out."""

    def __init__(
        self,
        *,
        histories: Mapping[tuple[str, YahooInterval], pd.DataFrame] | None = None,
        metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self._histories = {key: frame.copy(deep=True) for key, frame in (histories or {}).items()}
        self._metadata = dict(metadata or {})
        self._queries: list[HistoryQuery] = []
        self._failures: deque[BaseException] = deque()

    @property
    def queries(self) -> tuple[HistoryQuery, ...]:
        """Every query received, in order, including those answered with a scripted failure."""
        return tuple(self._queries)

    def fail_next(self, error: BaseException, *, times: int = 1) -> None:
        """Raise ``error`` on the next ``times`` calls, after recording their queries."""
        if not isinstance(error, BaseException):
            raise TypeError(f"error must be an exception, got {type(error).__name__}")
        if isinstance(times, bool) or not isinstance(times, int):
            raise TypeError(f"times must be an int, got {type(times).__name__}")
        if times < 1:
            raise ValueError(f"times must be at least 1, got {times}")
        self._failures.extend([error] * times)

    def history(self, query: HistoryQuery) -> YahooHistory:
        """Record ``query``, raise the next scripted failure if any, then replay.

        A symbol with neither a frame for ``(symbol, interval)`` nor metadata raises
        ``InvalidTickerError(not_found)``, as the real client does. Otherwise the answer is a copy
        of the frame (an empty frame shaped like ``spy_1d_2025-11-24`` for metadata-only symbols)
        with the symbol's metadata, or ``{"symbol": symbol, "currency": "USD"}``.
        """
        if not isinstance(query, HistoryQuery):
            raise TypeError(f"query must be a HistoryQuery, got {type(query).__name__}")
        self._queries.append(query)
        if self._failures:
            # A fresh traceback per raise: an error scripted several times does not accumulate
            # the frames of earlier raises.
            raise self._failures.popleft().with_traceback(None)
        frame = self._histories.get((query.symbol, query.interval))
        metadata = self._metadata.get(query.symbol)
        if frame is None and metadata is None:
            raise InvalidTickerError(
                InvalidTickerReason.NOT_FOUND,
                "no recording answers this symbol",
                ticker=query.symbol,
            )
        if frame is None:
            frame = load_recording(_EMPTY_FRAME_RECORDING).frame.iloc[:0]
        chart = metadata if metadata is not None else {"symbol": query.symbol, "currency": "USD"}
        return YahooHistory(frame=frame.copy(deep=True), metadata=ChartMetadata.from_mapping(chart))


def as_yahoo_client(client: RecordedYahooClient) -> YahooClient:
    """Returns its argument; strict mypy checks conformance to the port."""
    return client


def yfinance_as_market_data_provider(provider: YFinanceProvider) -> MarketDataProvider:
    """Returns its argument; strict mypy checks that the provider conforms to the Protocol."""
    return provider


def _document(path: Path, expected_format: str) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    document = _mapping(loaded, path.name)
    if document.get("format") != expected_format:
        raise ValueError(f"{path.name} is not a {expected_format} document")
    return document


def _frame(document: Mapping[str, object]) -> pd.DataFrame:
    index_document = _mapping(document.get("index"), "frame.index")
    unit_text = _text(index_document.get("unit"), "frame.index.unit")
    if unit_text not in _UNITS:
        raise ValueError("frame.index.unit is not a pandas datetime unit")
    unit = cast(_Unit, unit_text)
    seconds = [
        _integer(value, "frame.index.epoch_seconds")
        for value in _sequence(index_document.get("epoch_seconds"), "frame.index.epoch_seconds")
    ]
    index = (
        pd.DatetimeIndex(pd.to_datetime(np.array(seconds, dtype=np.int64), unit="s", utc=True))
        .tz_convert(_text(index_document.get("timezone"), "frame.index.timezone"))
        .as_unit(unit)
        .rename(_text(index_document.get("name"), "frame.index.name"))
    )
    columns = [
        _text(value, "frame.columns")
        for value in _sequence(document.get("columns"), "frame.columns")
    ]
    dtypes = [
        _text(value, "frame.dtypes") for value in _sequence(document.get("dtypes"), "frame.dtypes")
    ]
    rows = [_sequence(row, "frame.rows") for row in _sequence(document.get("rows"), "frame.rows")]
    if len(columns) != len(dtypes) or any(dtype not in _DTYPES for dtype in dtypes):
        raise ValueError("frame.dtypes must give float64 or int64 for every column")
    if len(rows) != len(index) or any(len(row) != len(columns) for row in rows):
        raise ValueError("frame.rows must have one row per label and one value per column")
    data: dict[str, _Column] = {}
    for position, (column, dtype) in enumerate(zip(columns, dtypes, strict=True)):
        cells = [row[position] for row in rows]
        if dtype == "int64":
            data[column] = np.array([_integer(cell, column) for cell in cells], dtype=np.int64)
        else:
            data[column] = np.array([_number(cell, column) for cell in cells], dtype=np.float64)
    return pd.DataFrame(data, index=index, columns=columns)


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{what} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a JSON array")
    return list(value)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    return value


def _integer(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    return value


def _number(value: object, what: str) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{what} must be a number or null")
    return float(value)
