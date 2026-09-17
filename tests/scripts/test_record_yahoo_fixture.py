"""Tests of ``scripts/record_yahoo_fixture.py`` (spec 011, T13, AC30; decisions D29 and D59).

Only the pure functions are exercised, without network: synthetic values replace every price,
volume, dividend and capital gain (sentinel values never survive), the frame structure is kept,
``Adj Close`` always differs from ``Close``, unknown columns and outputs outside
``tests/fixtures/yahoo/`` are refused, and the metadata keeps only its allowlist. The script's
network path lives in ``main()``; no test reaches it, and nothing in ``src/`` or CI runs it.
"""

from __future__ import annotations

import ast
import math
import random
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.script_loader import REPO_ROOT, load_script

recorder = load_script("scripts/record_yahoo_fixture.py", "record_yahoo_fixture")

SCRIPT = REPO_ROOT / "scripts" / "record_yahoo_fixture.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "yahoo"
COLUMNS = [
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
    "Dividends",
    "Stock Splits",
    "Capital Gains",
]
DTYPES = [
    "float64",
    "float64",
    "float64",
    "float64",
    "float64",
    "int64",
    "float64",
    "float64",
    "float64",
]
SENTINEL_PRICE = 4321.9876
SENTINEL_VOLUME = 987_654_321
SENTINEL_DIVIDEND = 1.2345


def sentinel_frame(rows: int = 5) -> pd.DataFrame:
    """A yfinance-shaped hourly frame whose values must never survive."""
    labels = pd.date_range("2025-11-24 09:30", periods=rows, freq="h", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "Open": [SENTINEL_PRICE] * rows,
            "High": [SENTINEL_PRICE + 1] * rows,
            "Low": [SENTINEL_PRICE - 1] * rows,
            "Close": [SENTINEL_PRICE + 0.5] * rows,
            "Adj Close": [SENTINEL_PRICE + 0.25] * rows,
            "Volume": np.array([SENTINEL_VOLUME] * rows, dtype=np.int64),
            "Dividends": [0.0, SENTINEL_DIVIDEND, 0.0, 0.0, 0.0][:rows],
            "Stock Splits": [0.0, 0.0, 10.0, 0.0, 0.0][:rows],
            "Capital Gains": [0.0, 0.0, 0.0, SENTINEL_DIVIDEND, 0.0][:rows],
        },
        index=labels.as_unit("s").rename("Datetime"),
    )
    return frame


def reference_rows(seed: int, count: int) -> list[tuple[float, float, float, float, float, int]]:
    """An independent restatement of ``random-walk/1`` (Design 13.4)."""
    rng = random.Random(seed)  # noqa: S311 - synthetic test data, not security
    previous = 100.0
    rows = []
    for _ in range(count):
        open_ = previous
        close = round(open_ * (1 + (rng.random() - 0.5) * 0.004), 4)
        high = round(max(open_, close) * (1 + rng.random() * 0.002), 4)
        low = round(min(open_, close) * (1 - rng.random() * 0.002), 4)
        volume = 1000 + int(rng.random() * 99000)
        previous = close
        rows.append((open_, high, low, close, round(close * 0.97, 4), volume))
    return rows


def history(frame: pd.DataFrame, seed: int = 11) -> dict[str, object]:
    document: dict[str, object] = recorder.history_document(
        frame,
        symbol="SPY",
        interval="1h",
        start=datetime(2025, 11, 24, 14, 0, tzinfo=UTC),
        end=datetime(2025, 12, 3, 0, 0, tzinfo=UTC),
        seed=seed,
        yfinance_version="1.7.0",
    )
    return document


# --- synthetic_rows -----------------------------------------------------------------------------


def test_synthetic_rows_follow_the_random_walk_generator() -> None:
    recorded = [[1.0, 2.0, 0.5, 1.5, 1.4, 10, 0.0, 0.0, 0.0] for _ in range(20)]

    rows = recorder.synthetic_rows(COLUMNS, DTYPES, recorded, 11)

    expected = reference_rows(11, 20)
    assert [tuple(row[:6]) for row in rows] == expected
    assert [row[6:] for row in rows] == [[0.0, 0.0, 0.0]] * 20
    assert all(isinstance(row[5], int) for row in rows)
    assert all(row[4] != row[3] for row in rows)
    for open_, high, low, close, _, _ in expected:
        assert low <= min(open_, close) <= max(open_, close) <= high


def test_synthetic_rows_depend_only_on_the_seed_and_the_row_count() -> None:
    first = recorder.synthetic_rows(COLUMNS, DTYPES, [[1.0] * 5 + [1, 0.0, 0.0, 0.0]] * 3, 7)
    second = recorder.synthetic_rows(COLUMNS, DTYPES, [[9.0] * 5 + [9, 0.0, 0.0, 0.0]] * 3, 7)
    third = recorder.synthetic_rows(COLUMNS, DTYPES, [[1.0] * 5 + [1, 0.0, 0.0, 0.0]] * 3, 8)

    assert first == second
    assert first != third


def test_actions_keep_only_their_shape_and_splits_keep_the_ratio() -> None:
    recorded = [
        [1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.0, 0.0, 0.0],
        [1.0, 1.0, 1.0, 1.0, 1.0, 1, SENTINEL_DIVIDEND, 10.0, 0.37],
    ]

    rows = recorder.synthetic_rows(COLUMNS, DTYPES, recorded, 3)

    assert rows[0][6:] == [0.0, 0.0, 0.0]
    assert rows[1][6:] == [0.25, 10.0, 0.25]


def test_a_missing_cell_stays_missing_and_the_walk_continues() -> None:
    recorded = [
        [1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.0, 0.0, 0.0],
        [math.nan, None, 1.0, math.nan, 1.0, 1, math.nan, 0.0, None],
        [1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.0, 0.0, 0.0],
    ]

    rows = recorder.synthetic_rows(COLUMNS, DTYPES, recorded, 5)

    expected = reference_rows(5, 3)
    assert rows[1][0] is None
    assert rows[1][1] is None
    assert rows[1][2] == expected[1][2]
    assert rows[1][3] is None
    assert rows[1][6] is None
    assert rows[1][8] is None
    assert tuple(rows[2][:6]) == expected[2]


def test_an_unknown_column_is_refused() -> None:
    with pytest.raises(ValueError, match="column"):
        recorder.synthetic_rows(["Open", "Repaired?"], ["float64", "float64"], [[1.0, 1.0]], 1)


@pytest.mark.parametrize(
    ("dtypes", "rows"),
    [
        (["float64"], [[1.0, 1.0]]),
        (["float64", "object"], [[1.0, 1.0]]),
        (["float64", "float64"], [[1.0]]),
    ],
)
def test_inconsistent_shapes_and_dtypes_are_refused(
    dtypes: list[str], rows: list[list[float]]
) -> None:
    with pytest.raises(ValueError):
        recorder.synthetic_rows(["Open", "Close"], dtypes, rows, 1)


# --- history_document ---------------------------------------------------------------------------


def test_the_history_document_keeps_structure_and_replaces_every_value() -> None:
    frame = sentinel_frame()

    document = history(frame)

    assert document["format"] == "yfinance-history/1"
    assert document["recorded_with"] == {"yfinance": "1.7.0"}
    assert document["call"] == {
        "symbol": "SPY",
        "interval": "1h",
        "start": "2025-11-24T14:00:00+00:00",
        "end": "2025-12-03T00:00:00+00:00",
    }
    assert document["synthetic"] == {"generator": "random-walk/1", "seed": 11}
    body = document["frame"]
    assert isinstance(body, dict)
    assert body["index"] == {
        "name": "Datetime",
        "timezone": "America/New_York",
        "unit": "s",
        "epoch_seconds": [int(label.timestamp()) for label in frame.index],
    }
    assert body["columns"] == COLUMNS
    assert body["dtypes"] == DTYPES
    rendered = repr(body["rows"])
    for sentinel in (SENTINEL_PRICE, SENTINEL_PRICE + 1, SENTINEL_VOLUME, SENTINEL_DIVIDEND):
        assert repr(sentinel) not in rendered
    assert body["rows"] == recorder.synthetic_rows(
        COLUMNS, DTYPES, frame.to_numpy(dtype=object).tolist(), 11
    )


def test_a_daily_frame_keeps_its_index_name_and_has_no_capital_gains() -> None:
    labels = pd.date_range("2024-06-03", periods=3, freq="D", tz="America/New_York")
    frame = sentinel_frame(3).drop(columns=["Capital Gains"])
    frame.index = labels.as_unit("s").rename("Date")

    body = history(frame, seed=15)["frame"]

    assert isinstance(body, dict)
    index = body["index"]
    assert isinstance(index, dict)
    assert index["name"] == "Date"
    assert body["columns"] == COLUMNS[:-1]


def test_a_frame_with_an_unknown_column_is_refused() -> None:
    frame = sentinel_frame().assign(Repaired=True)

    with pytest.raises(ValueError, match="column"):
        history(frame)


def test_a_frame_with_a_naive_index_or_sub_second_labels_is_refused() -> None:
    naive = sentinel_frame()
    naive.index = pd.DatetimeIndex(naive.index).tz_localize(None)
    with pytest.raises(ValueError):
        history(naive)

    fractional = sentinel_frame()
    fractional.index = (pd.DatetimeIndex(fractional.index) + pd.Timedelta(milliseconds=1)).as_unit(
        "ms"
    )
    with pytest.raises(ValueError):
        history(fractional)


# --- metadata_document --------------------------------------------------------------------------


class _LazyMetadata(Mapping[str, object]):
    """Like yfinance's HistoryMetadata: iterating would trigger an extra request."""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the metadata must never be iterated")

    def __len__(self) -> int:
        return len(self._values)


def test_the_metadata_document_keeps_exactly_the_allowlist() -> None:
    raw = {
        "symbol": "SPY",
        "instrumentType": "ETF",
        "exchangeName": "PCX",
        "fullExchangeName": "NYSEArca",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "longName": "Synthetic Fund",
        "shortName": None,
        "regularMarketPrice": SENTINEL_PRICE,
        "regularMarketVolume": SENTINEL_VOLUME,
        "fiftyTwoWeekHigh": SENTINEL_PRICE,
        "tradingPeriods": [[{"start": 1}]],
    }

    document = recorder.metadata_document({"SPY": raw}, yfinance_version="1.7.0")

    assert document == {
        "format": "yfinance-metadata/1",
        "recorded_with": {"yfinance": "1.7.0"},
        "symbols": {
            "SPY": {
                "symbol": "SPY",
                "instrumentType": "ETF",
                "exchangeName": "PCX",
                "fullExchangeName": "NYSEArca",
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
                "longName": "Synthetic Fund",
                "shortName": None,
            }
        },
    }
    assert recorder.METADATA_KEYS == (
        "symbol",
        "instrumentType",
        "exchangeName",
        "fullExchangeName",
        "currency",
        "exchangeTimezoneName",
        "longName",
        "shortName",
    )


def test_metadata_values_that_are_not_text_become_null() -> None:
    document = recorder.metadata_document(
        {"ES=F": {"symbol": "ES=F", "longName": 42, "currency": ["USD"]}}, yfinance_version="1.7.0"
    )

    symbols = document["symbols"]
    assert isinstance(symbols, dict)
    assert symbols["ES=F"]["longName"] is None
    assert symbols["ES=F"]["currency"] is None
    assert symbols["ES=F"]["exchangeName"] is None


def test_the_metadata_document_reads_through_get_only() -> None:
    document = recorder.metadata_document(
        {"SPY": _LazyMetadata({"symbol": "SPY"})}, yfinance_version="1.7.0"
    )

    symbols = document["symbols"]
    assert isinstance(symbols, dict)
    assert symbols["SPY"]["symbol"] == "SPY"


# --- output_path and main ------------------------------------------------------------------------


def test_an_output_inside_the_fixtures_directory_is_accepted() -> None:
    assert recorder.output_path(str(FIXTURES_DIR / "spy_1h.json")) == FIXTURES_DIR / "spy_1h.json"


@pytest.mark.parametrize(
    "text",
    [
        "tests/fixtures/spy.json",
        "spy.json",
        str(FIXTURES_DIR / "spy.csv"),
        str(FIXTURES_DIR / "nested" / "spy.json"),
        str(FIXTURES_DIR / ".." / "spy.json"),
        str(FIXTURES_DIR),
        str(REPO_ROOT / "src" / "trading_bot" / "spy.json"),
    ],
)
def test_outputs_outside_the_fixtures_directory_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="tests/fixtures/yahoo"):
        recorder.output_path(text)


def test_main_refuses_a_bad_output_with_status_2_before_any_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "history",
        "--symbol",
        "SPY",
        "--interval",
        "1h",
        "--start",
        "2025-11-24T14:00:00Z",
        "--end",
        "2025-12-03T00:00:00Z",
        "--seed",
        "11",
        "--output",
        "elsewhere.json",
    ]

    with pytest.raises(SystemExit) as caught:
        recorder.main(arguments)

    assert caught.value.code == 2
    assert "tests/fixtures/yahoo" in capsys.readouterr().err


def test_main_refuses_a_bad_metadata_output_with_status_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        recorder.main(["metadata", "--symbols", "SPY", "--output", str(FIXTURES_DIR / "m.txt")])

    assert caught.value.code == 2


def test_the_network_path_is_only_reachable_from_main() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    top_level_imports = [
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    ] + [node.module or "" for node in tree.body if isinstance(node, ast.ImportFrom)]

    assert not [
        name for name in top_level_imports if name.split(".")[0] in {"yfinance", "curl_cffi"}
    ]
    assert not [name for name in top_level_imports if name.startswith("trading_bot.data.yahoo")]


def test_nothing_in_src_or_ci_runs_the_recording_script() -> None:
    searched = [
        *sorted((REPO_ROOT / "src").rglob("*.py")),
        *sorted((REPO_ROOT / ".github").rglob("*")),
    ]
    offenders = [
        path
        for path in searched
        if path.is_file() and "record_yahoo_fixture" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_parser_accepts_the_documented_commands() -> None:
    parser = recorder.build_parser()

    history_args = parser.parse_args(
        [
            "history",
            "--symbol",
            "SPY",
            "--interval",
            "1h",
            "--start",
            "2025-11-24T14:00:00Z",
            "--end",
            "2025-12-03T00:00:00Z",
            "--seed",
            "11",
            "--output",
            "tests/fixtures/yahoo/spy_1h_2025-11-24.json",
        ]
    )
    metadata_args = parser.parse_args(
        ["metadata", "--symbols", "AAPL", "^GSPC", "--output", "tests/fixtures/yahoo/metadata.json"]
    )

    assert history_args.command == "history"
    assert history_args.start == datetime(2025, 11, 24, 14, 0, tzinfo=UTC)
    assert history_args.seed == 11
    assert metadata_args.symbols == ["AAPL", "^GSPC"]


@pytest.mark.parametrize("text", ["2025-11-24T14:00:00", "yesterday"])
def test_the_parser_requires_an_aware_timestamp(text: str) -> None:
    with pytest.raises(SystemExit):
        recorder.build_parser().parse_args(
            [
                "history",
                "--symbol",
                "SPY",
                "--interval",
                "1h",
                "--start",
                text,
                "--end",
                "2025-12-03T00:00:00Z",
                "--seed",
                "11",
                "--output",
                "x.json",
            ]
        )


def test_write_document_writes_utf8_json_with_indent_one_and_a_trailing_newline(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out.json"
    name = "Nestl" + chr(0xE9)  # a non-ASCII name is written as UTF-8, not escaped

    recorder.write_document(target, {"name": name, "rows": [[1.0, None]]})

    expected = '{\n "name": "' + name + '",\n "rows": [\n  [\n   1.0,\n   null\n  ]\n ]\n}\n'
    assert target.read_bytes() == expected.encode("utf-8")
