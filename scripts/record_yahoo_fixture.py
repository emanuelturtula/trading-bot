"""Record a Yahoo fixture with synthetic values (spec 011, Design 13.4; decisions D29 and D59).

Manual and networked: never run by tests or CI. It calls yfinance once, keeps the structure of the
answer (index epoch seconds, zone, unit and name; column order and dtypes; split ratios) and
replaces every price, volume, dividend and capital-gain value with the seeded ``random-walk/1``
generator, so no real Yahoo data is written. Metadata keeps only an allowlist of reference codes
and names.

Usage (from the repository root):
    uv run python scripts/record_yahoo_fixture.py history --symbol SPY --interval 1h \\
      --start 2025-11-24T14:00:00Z --end 2025-12-03T00:00:00Z --seed 11 \\
      --output tests/fixtures/yahoo/spy_1h_2025-11-24.json
    uv run python scripts/record_yahoo_fixture.py metadata --symbols AAPL QQQ \\
      --output tests/fixtures/yahoo/metadata.json

The output must be a ``.json`` file directly inside ``tests/fixtures/yahoo/``; anything else is
refused with exit status 2 before any request. Only the output path and the row count are printed.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pandas as pd

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURES_DIR: Final = REPO_ROOT / "tests" / "fixtures" / "yahoo"
HISTORY_FORMAT: Final = "yfinance-history/1"
METADATA_FORMAT: Final = "yfinance-metadata/1"
GENERATOR: Final = "random-walk/1"
METADATA_KEYS: Final = (
    "symbol",
    "instrumentType",
    "exchangeName",
    "fullExchangeName",
    "currency",
    "exchangeTimezoneName",
    "longName",
    "shortName",
)
REQUEST_TIMEOUT: Final = 30.0

_PRICE_COLUMNS: Final = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
_ACTION_COLUMNS: Final = ("Dividends", "Capital Gains")
_RATIO_COLUMNS: Final = ("Stock Splits",)
_DTYPES: Final = ("float64", "int64")

type Cell = float | int | None
type Document = dict[str, object]


def synthetic_rows(
    columns: Sequence[str],
    dtypes: Sequence[str],
    recorded_rows: Sequence[Sequence[object]],
    seed: int,
) -> list[list[Cell]]:
    """JSON-ready rows of ``random-walk/1`` values, one per recorded row (Design 13.4).

    Per row, in order: ``open`` is the previous close (100.0 first), then ``close``, ``high``,
    ``low`` and ``volume`` each consume one ``random()`` of ``random.Random(seed)``, whatever the
    recorded cells hold. ``Adj Close`` is ``round(close * 0.97, 4)``; ``Dividends`` and ``Capital
    Gains`` are 0.25 where the recorded value is non-zero; ``Stock Splits`` keeps the recorded
    ratio; a missing (NaN or ``None``) recorded cell stays ``None``. Any other column, a dtype
    other than ``float64``/``int64`` or a row of the wrong width raises ``ValueError``.
    """
    known = (*_PRICE_COLUMNS, *_ACTION_COLUMNS, *_RATIO_COLUMNS)
    for column in columns:
        if column not in known:
            raise ValueError(f"column {column!r} has no synthetic generator")
    if len(dtypes) != len(columns) or any(dtype not in _DTYPES for dtype in dtypes):
        raise ValueError("every column needs a float64 or int64 dtype")
    rng = random.Random(seed)  # noqa: S311 - synthetic fixture values, not security
    previous = 100.0
    rows: list[list[Cell]] = []
    for recorded in recorded_rows:
        if len(recorded) != len(columns):
            raise ValueError("every recorded row needs one cell per column")
        open_ = previous
        close = round(open_ * (1 + (rng.random() - 0.5) * 0.004), 4)
        high = round(max(open_, close) * (1 + rng.random() * 0.002), 4)
        low = round(min(open_, close) * (1 - rng.random() * 0.002), 4)
        volume = 1000 + int(rng.random() * 99000)
        previous = close
        generated: dict[str, float] = {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Adj Close": round(close * 0.97, 4),
            "Volume": volume,
        }
        row: list[Cell] = []
        for column, dtype, cell in zip(columns, dtypes, recorded, strict=True):
            if _missing(cell):
                row.append(None)
                continue
            if column in generated:
                value = generated[column]
            elif column in _ACTION_COLUMNS:
                value = 0.25 if _number(cell) != 0 else 0.0
            else:
                value = _number(cell)
            row.append(int(value) if dtype == "int64" else float(value))
        rows.append(row)
    return rows


def history_document(
    frame: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    seed: int,
    yfinance_version: str,
) -> Document:
    """The ``yfinance-history/1`` document of ``frame`` with synthetic values (Design 13.3)."""
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("the frame index must be a timezone-aware DatetimeIndex")
    if not isinstance(index.name, str):
        raise ValueError("the frame index must be named")
    seconds: list[int] = []
    for label in index:
        stamp = label.timestamp()
        if not float(stamp).is_integer():
            raise ValueError("frame labels must be whole seconds")
        seconds.append(int(stamp))
    columns = [str(column) for column in frame.columns]
    if columns != list(frame.columns):
        raise ValueError("every column label must be a string")
    dtypes = [str(dtype) for dtype in frame.dtypes]
    return {
        "format": HISTORY_FORMAT,
        "recorded_with": {"yfinance": yfinance_version},
        "call": {
            "symbol": symbol,
            "interval": interval,
            "start": start.astimezone(UTC).isoformat(),
            "end": end.astimezone(UTC).isoformat(),
        },
        "frame": {
            "index": {
                "name": index.name,
                "timezone": str(index.tz),
                "unit": index.unit,
                "epoch_seconds": seconds,
            },
            "columns": columns,
            "dtypes": dtypes,
            "rows": synthetic_rows(columns, dtypes, frame.to_numpy(dtype=object).tolist(), seed),
        },
        "synthetic": {"generator": GENERATOR, "seed": seed},
    }


def metadata_document(
    metadata_by_symbol: Mapping[str, Mapping[str, object]], *, yfinance_version: str
) -> Document:
    """The ``yfinance-metadata/1`` document: exactly ``METADATA_KEYS`` per symbol (Design 13.3).

    Each value is read with one ``get`` (never by iterating, which would make yfinance load
    ``tradingPeriods``); values that are not text become ``null``.
    """
    symbols: dict[str, dict[str, str | None]] = {}
    for symbol, metadata in metadata_by_symbol.items():
        values: dict[str, str | None] = {}
        for key in METADATA_KEYS:
            value = metadata.get(key)
            values[key] = value if isinstance(value, str) else None
        symbols[symbol] = values
    return {
        "format": METADATA_FORMAT,
        "recorded_with": {"yfinance": yfinance_version},
        "symbols": symbols,
    }


def output_path(text: str) -> Path:
    """``text`` resolved; it must be a ``.json`` file directly inside ``tests/fixtures/yahoo/``."""
    path = Path(text).resolve()
    if path.parent != FIXTURES_DIR.resolve() or path.suffix != ".json":
        raise ValueError("the output must be a .json file directly inside tests/fixtures/yahoo/")
    return path


def write_document(path: Path, document: Document) -> None:
    """UTF-8 JSON with ``indent=1``, non-ASCII kept and a trailing newline."""
    text = json.dumps(document, indent=1, ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record a Yahoo fixture with synthetic values.")
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("history", help="record one Ticker.history call")
    history.add_argument("--symbol", required=True)
    history.add_argument("--interval", required=True, choices=["1h", "1d"])
    history.add_argument("--start", required=True, type=_aware_timestamp)
    history.add_argument("--end", required=True, type=_aware_timestamp)
    history.add_argument("--seed", required=True, type=int)
    history.add_argument("--output", required=True)
    metadata = commands.add_parser("metadata", help="record the chart metadata of symbols")
    metadata.add_argument("--symbols", required=True, nargs="+")
    metadata.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the network
    """Parse, check the output, then call yfinance (the only networked code of this script)."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        output = output_path(arguments.output)
    except ValueError as error:
        parser.error(str(error))

    import yfinance

    from trading_bot.data.yahoo.client import configure_yfinance

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cache:
        configure_yfinance(Path(cache))
        if arguments.command == "history":
            frame = yfinance.Ticker(arguments.symbol).history(
                start=arguments.start,
                end=arguments.end,
                interval=arguments.interval,
                prepost=False,
                actions=True,
                auto_adjust=False,
                back_adjust=False,
                repair=False,
                keepna=False,
                rounding=False,
                timeout=REQUEST_TIMEOUT,
            )
            document = history_document(
                frame,
                symbol=arguments.symbol,
                interval=arguments.interval,
                start=arguments.start,
                end=arguments.end,
                seed=arguments.seed,
                yfinance_version=yfinance.__version__,
            )
            count = len(frame)
        else:
            collected: dict[str, dict[str, object]] = {}
            for symbol in arguments.symbols:
                ticker = yfinance.Ticker(symbol)
                ticker.history(period="1mo", interval="1d", timeout=REQUEST_TIMEOUT)
                raw = ticker.get_history_metadata()
                collected[symbol] = {key: raw.get(key) for key in METADATA_KEYS}
            document = metadata_document(collected, yfinance_version=yfinance.__version__)
            count = len(collected)
    write_document(output, document)
    print(f"{output.relative_to(REPO_ROOT).as_posix()}: {count} rows")
    return 0


def _aware_timestamp(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamps need an offset, for example 2025-11-24T14:00Z")
    return value.astimezone(UTC)


def _missing(cell: object) -> bool:
    return cell is None or (isinstance(cell, float) and math.isnan(cell))


def _number(cell: object) -> float:
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        raise ValueError("recorded cells must be numbers")
    return float(cell)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
