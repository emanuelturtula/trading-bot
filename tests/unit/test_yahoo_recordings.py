"""Tests of the Yahoo recordings and their replay fakes (spec 011, T12, AC29 and AC31).

AC29: the six files of Design 13.2 exist in the format of Design 13.3, the metadata keeps exactly
its allowlist, every price, volume, dividend and capital-gain value is regenerated from the
file's seed with an independent restatement of ``random-walk/1`` (so no recorded value can be
real), and the structural facts hold. AC31: ``load_recording``, ``load_metadata``, ``hand_frame``,
``RecordedYahooClient`` and ``as_yahoo_client`` (Design 13.5).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import tests.fixtures.yahoo_recordings as recordings_module
from tests.fixtures.yahoo_recordings import (
    RECORDINGS_DIR,
    RecordedYahooClient,
    as_yahoo_client,
    hand_frame,
    load_metadata,
    load_recording,
)
from trading_bot.data.errors import InvalidTickerError, InvalidTickerReason, NoDataError
from trading_bot.data.yahoo.history import ChartMetadata, HistoryQuery, YahooHistory

HISTORY_FILES = (
    "spy_1h_2025-11-24",
    "spy_1d_2025-11-24",
    "aapl_1h_2026-01-28",
    "spy_1h_2025-03-06",
    "nvda_1d_2024-06-03",
)
METADATA_SYMBOLS = (
    "AAPL",
    "QQQ",
    "BRK-B",
    "SPY",
    "UEC",
    "ARKB",
    "^GSPC",
    "VFIAX",
    "BTC-USD",
    "EURUSD=X",
    "ES=F",
    "RELIANCE.NS",
    "TCEHY",
    "NSRGY",
    "SHOP.TO",
)
METADATA_ALLOWLIST = (
    "symbol",
    "instrumentType",
    "exchangeName",
    "fullExchangeName",
    "currency",
    "exchangeTimezoneName",
    "longName",
    "shortName",
)
CALLS = {
    "spy_1h_2025-11-24": (
        "SPY",
        "1h",
        "2025-11-24T14:00:00+00:00",
        "2025-12-03T00:00:00+00:00",
        11,
    ),
    "spy_1d_2025-11-24": (
        "SPY",
        "1d",
        "2025-11-24T05:00:00+00:00",
        "2025-12-03T00:00:00+00:00",
        12,
    ),
    "aapl_1h_2026-01-28": (
        "AAPL",
        "1h",
        "2026-01-28T14:00:00+00:00",
        "2026-02-05T00:00:00+00:00",
        13,
    ),
    "spy_1h_2025-03-06": (
        "SPY",
        "1h",
        "2025-03-06T14:00:00+00:00",
        "2025-03-12T00:00:00+00:00",
        14,
    ),
    "nvda_1d_2024-06-03": (
        "NVDA",
        "1d",
        "2024-06-03T04:00:00+00:00",
        "2024-06-15T00:00:00+00:00",
        15,
    ),
}
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
NEW_YORK_SECONDS = pd.DatetimeTZDtype(unit="s", tz="America/New_York")


def read_json(name: str) -> dict[str, object]:
    loaded: object = json.loads((RECORDINGS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def regenerated(seed: int, count: int) -> list[tuple[float, float, float, float, float, int]]:
    """``random-walk/1`` restated from Design 13.4, independently of the recording script."""
    rng = random.Random(seed)  # noqa: S311 - synthetic fixture values, not security
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


def labels_by_day(frame: pd.DataFrame) -> dict[str, list[str]]:
    days: dict[str, list[str]] = {}
    for label in pd.DatetimeIndex(frame.index):
        days.setdefault(label.strftime("%Y-%m-%d"), []).append(label.strftime("%H:%M"))
    return days


# --- AC29: files and format -----------------------------------------------------------------


def test_exactly_the_six_recordings_exist() -> None:
    assert sorted(path.name for path in RECORDINGS_DIR.iterdir()) == sorted(
        [f"{name}.json" for name in HISTORY_FILES] + ["metadata.json"]
    )


@pytest.mark.parametrize("name", HISTORY_FILES)
def test_history_files_have_the_spec_format(name: str) -> None:
    document = read_json(name)
    symbol, interval, start, end, seed = CALLS[name]

    assert list(document) == ["format", "recorded_with", "call", "frame", "synthetic"]
    assert document["format"] == "yfinance-history/1"
    assert document["recorded_with"] == {"yfinance": "1.7.0"}
    assert document["call"] == {"symbol": symbol, "interval": interval, "start": start, "end": end}
    assert document["synthetic"] == {"generator": "random-walk/1", "seed": seed}
    frame = document["frame"]
    assert isinstance(frame, dict)
    assert list(frame) == ["index", "columns", "dtypes", "rows"]
    index = frame["index"]
    assert isinstance(index, dict)
    assert list(index) == ["name", "timezone", "unit", "epoch_seconds"]
    assert index["timezone"] == "America/New_York"
    assert index["unit"] == "s"


@pytest.mark.parametrize("name", [*HISTORY_FILES, "metadata"])
def test_files_are_utf8_json_with_indent_one_lf_and_a_trailing_newline(name: str) -> None:
    raw = (RECORDINGS_DIR / f"{name}.json").read_bytes()

    assert b"\r" not in raw
    assert raw.endswith(b"}\n")
    document = json.loads(raw.decode("utf-8"))
    assert raw.decode("utf-8") == json.dumps(document, indent=1, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name", [*HISTORY_FILES, "metadata"])
def test_recordings_hold_no_url_host_or_crumb(name: str) -> None:
    text = (RECORDINGS_DIR / f"{name}.json").read_text(encoding="utf-8").lower()

    for fragment in ("http", "://", "crumb", "www.", "query1", "query2", "yahoo.com", "cookie"):
        assert fragment not in text


def test_the_metadata_file_keeps_exactly_the_allowlist_for_the_fifteen_symbols() -> None:
    document = read_json("metadata")

    assert list(document) == ["format", "recorded_with", "symbols"]
    assert document["format"] == "yfinance-metadata/1"
    assert document["recorded_with"] == {"yfinance": "1.7.0"}
    symbols = document["symbols"]
    assert isinstance(symbols, dict)
    assert list(symbols) == list(METADATA_SYMBOLS)
    for symbol, values in symbols.items():
        assert isinstance(values, dict)
        assert list(values) == list(METADATA_ALLOWLIST)
        assert values["symbol"] == symbol
        assert all(value is None or isinstance(value, str) for value in values.values())


# --- AC29: synthetic values -------------------------------------------------------------------


@pytest.mark.parametrize("name", HISTORY_FILES)
def test_every_price_volume_and_action_value_is_regenerated_from_the_seed(name: str) -> None:
    recording = load_recording(name)
    frame = recording.frame
    expected = regenerated(recording.seed, len(frame))

    for position, row in enumerate(expected):
        assert tuple(frame.iloc[position][PRICE_COLUMNS[:5]].tolist()) == row[:5]
        assert int(frame["Volume"].iloc[position]) == row[5]
    assert set(frame["Dividends"].tolist()) <= {0.0, 0.25}
    if "Capital Gains" in frame.columns:
        assert set(frame["Capital Gains"].tolist()) <= {0.0, 0.25}
    assert not frame.isna().to_numpy().any()


# --- AC29: structural facts (Design 13.2) -------------------------------------------------------


def test_spy_1h_2025_11_24_structure() -> None:
    frame = load_recording("spy_1h_2025-11-24").frame
    days = labels_by_day(frame)

    assert len(frame) == 38
    assert frame.index.dtype == NEW_YORK_SECONDS
    assert frame.index.name == "Datetime"
    assert list(frame.columns) == [*PRICE_COLUMNS, "Capital Gains"]
    assert "2025-11-27" not in days
    assert days["2025-11-28"] == ["09:30", "10:30", "11:30"]


def test_spy_1d_2025_11_24_structure() -> None:
    frame = load_recording("spy_1d_2025-11-24").frame
    days = labels_by_day(frame)

    assert len(frame) == 6
    assert frame.index.name == "Date"
    assert all(times == ["00:00"] for times in days.values())
    assert "2025-11-27" not in days


def test_aapl_1h_2026_01_28_structure() -> None:
    frame = load_recording("aapl_1h_2026-01-28").frame
    days = labels_by_day(frame)

    assert len(frame) == 33
    assert list(frame.columns) == PRICE_COLUMNS
    assert days["2026-01-30"] == ["09:30", "10:30"]
    assert all(time >= "13:30" for time in days["2026-02-02"])


def test_spy_1h_2025_03_06_structure_across_the_dst_change() -> None:
    frame = load_recording("spy_1h_2025-03-06").frame
    utc_days: dict[str, list[str]] = {}
    for label in pd.DatetimeIndex(frame.index).tz_convert("UTC"):
        utc_days.setdefault(label.strftime("%Y-%m-%d"), []).append(label.strftime("%H:%M"))

    assert len(frame) == 28
    assert utc_days["2025-03-07"][0] == "14:30"
    assert utc_days["2025-03-07"][-1] == "20:30"
    assert utc_days["2025-03-10"][0] == "13:30"
    assert utc_days["2025-03-10"][-1] == "19:30"


def test_nvda_1d_2024_06_03_structure() -> None:
    frame = load_recording("nvda_1d_2024-06-03").frame
    days = [label.strftime("%Y-%m-%d") for label in pd.DatetimeIndex(frame.index)]

    assert len(frame) == 10
    splits = frame["Stock Splits"].to_numpy()
    dividends = frame["Dividends"].to_numpy()
    assert [day for day, value in zip(days, splits, strict=True) if value != 0] == ["2024-06-10"]
    assert float(splits[days.index("2024-06-10")]) == 10.0
    assert [day for day, value in zip(days, dividends, strict=True) if value != 0] == ["2024-06-11"]
    assert not (frame["Close"] == frame["Adj Close"]).any()


# --- AC31: load_recording and load_metadata ----------------------------------------------------


def test_load_recording_rebuilds_the_yfinance_frame_structure() -> None:
    recording = load_recording("spy_1h_2025-11-24")

    assert recording.name == "spy_1h_2025-11-24"
    assert recording.symbol == "SPY"
    assert recording.interval == "1h"
    assert recording.seed == 11
    frame = recording.frame
    assert frame.index.dtype == NEW_YORK_SECONDS
    assert [str(dtype) for dtype in frame.dtypes] == ["float64"] * 5 + ["int64"] + ["float64"] * 3
    assert pd.DatetimeIndex(frame.index)[0] == pd.Timestamp(
        "2025-11-24T09:30", tz="America/New_York"
    )


def test_load_recording_returns_a_new_frame_on_every_call() -> None:
    first = load_recording("spy_1d_2025-11-24").frame
    first.iloc[0, 0] = -1.0

    assert load_recording("spy_1d_2025-11-24").frame.iloc[0, 0] == 100.0


def test_a_null_cell_becomes_nan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = read_json("spy_1d_2025-11-24")
    frame = document["frame"]
    assert isinstance(frame, dict)
    rows = frame["rows"]
    assert isinstance(rows, list)
    rows[0][3] = None
    (tmp_path / "patched.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(recordings_module, "RECORDINGS_DIR", tmp_path)

    rebuilt = recordings_module.load_recording("patched").frame

    assert np.isnan(rebuilt["Close"].iloc[0])


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("format",), "yfinance-history/2"),
        (("call", "interval"), "5m"),
        (("frame", "index", "unit"), "D"),
        (("frame", "dtypes"), ["object"]),
        (("frame", "rows"), [[1.0]]),
        (("frame", "columns"), "Open"),
        (("synthetic", "seed"), "11"),
        (("frame", "index", "epoch_seconds"), [1.5]),
        (("call",), []),
        (("frame", "index", "name"), 7),
    ],
)
def test_a_malformed_recording_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], value: object
) -> None:
    document: dict[str, object] = read_json("spy_1d_2025-11-24")
    holder: object = document
    for key in path[:-1]:
        assert isinstance(holder, dict)
        holder = holder[key]
    assert isinstance(holder, dict)
    holder[path[-1]] = value
    (tmp_path / "broken.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(recordings_module, "RECORDINGS_DIR", tmp_path)

    with pytest.raises(ValueError):
        recordings_module.load_recording("broken")


def test_a_row_cell_that_is_not_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = read_json("spy_1d_2025-11-24")
    frame = document["frame"]
    assert isinstance(frame, dict)
    rows = frame["rows"]
    assert isinstance(rows, list)
    rows[0][0] = "100.0"
    (tmp_path / "text.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(recordings_module, "RECORDINGS_DIR", tmp_path)

    with pytest.raises(ValueError):
        recordings_module.load_recording("text")


def test_load_metadata_returns_read_only_mappings_with_nulls_kept() -> None:
    metadata = load_metadata()

    assert list(metadata) == list(METADATA_SYMBOLS)
    assert metadata["ES=F"]["longName"] is None
    with pytest.raises(TypeError):
        metadata["SPY"]["symbol"] = "QQQ"  # type: ignore[index]
    with pytest.raises(TypeError):
        metadata["XYZ"] = {}  # type: ignore[index]


def test_the_hand_frame_is_new_on_every_call() -> None:
    first = hand_frame()
    first.iloc[0, 0] = -1.0

    assert hand_frame().iloc[0, 0] == 100.0


# --- AC31: RecordedYahooClient ------------------------------------------------------------------


def spy_client() -> RecordedYahooClient:
    return RecordedYahooClient(
        histories={("SPY", "1d"): load_recording("spy_1d_2025-11-24").frame},
        metadata={"SPY": load_metadata()["SPY"], "QQQ": load_metadata()["QQQ"]},
    )


def test_the_client_replays_a_copy_of_the_frame_with_the_metadata() -> None:
    client = spy_client()
    query = HistoryQuery(symbol="SPY", interval="1d", period="1mo")

    first = client.history(query)
    first.frame.iloc[0, 0] = -1.0
    second = client.history(query)

    assert isinstance(second, YahooHistory)
    pd.testing.assert_frame_equal(second.frame, load_recording("spy_1d_2025-11-24").frame)
    assert second.metadata == ChartMetadata.from_mapping(load_metadata()["SPY"])
    assert client.queries == (query, query)


def test_the_client_copies_the_frames_it_is_given() -> None:
    frame = load_recording("spy_1d_2025-11-24").frame
    client = RecordedYahooClient(histories={("SPY", "1d"): frame})
    frame.iloc[0, 0] = -1.0

    replayed = client.history(HistoryQuery(symbol="SPY", interval="1d", period="1mo"))

    assert replayed.frame.iloc[0, 0] == 100.0


def test_a_metadata_only_symbol_gets_an_empty_frame_shaped_like_the_daily_recording() -> None:
    history = spy_client().history(HistoryQuery(symbol="QQQ", interval="1d", period="1mo"))

    reference = load_recording("spy_1d_2025-11-24").frame
    assert len(history.frame) == 0
    assert list(history.frame.columns) == list(reference.columns)
    assert history.frame.index.dtype == reference.index.dtype
    assert history.frame.index.name == "Date"
    assert history.metadata.symbol == "QQQ"


def test_a_frame_without_metadata_gets_a_minimal_usd_metadata() -> None:
    client = RecordedYahooClient(
        histories={("SPY", "1h"): load_recording("spy_1h_2025-11-24").frame}
    )

    history = client.history(HistoryQuery(symbol="SPY", interval="1h", period="1mo"))

    assert history.metadata == ChartMetadata(
        symbol="SPY",
        instrument_type=None,
        exchange_name=None,
        currency="USD",
        long_name=None,
        short_name=None,
    )


def test_an_unknown_symbol_is_not_found_as_the_real_client_reports_it() -> None:
    client = spy_client()
    query = HistoryQuery(symbol="ZZZZNOTREAL", interval="1d", period="1mo")

    with pytest.raises(InvalidTickerError) as caught:
        client.history(query)

    assert caught.value.reason is InvalidTickerReason.NOT_FOUND
    assert caught.value.ticker == "ZZZZNOTREAL"
    assert client.queries == (query,)


def test_a_frame_for_another_interval_does_not_answer() -> None:
    client = RecordedYahooClient(
        histories={("SPY", "1d"): load_recording("spy_1d_2025-11-24").frame}
    )

    with pytest.raises(InvalidTickerError):
        client.history(HistoryQuery(symbol="SPY", interval="1h", period="1mo"))


def test_scripted_failures_are_raised_first_in_first_out_after_recording_the_query() -> None:
    client = spy_client()
    query = HistoryQuery(symbol="SPY", interval="1d", period="1mo")
    first = NoDataError("first", ticker="SPY")
    second = KeyboardInterrupt()
    client.fail_next(first, times=2)
    client.fail_next(second)

    raised: list[BaseException] = []
    for _ in range(3):
        try:
            client.history(query)
        except BaseException as error:  # the scripted failures include a KeyboardInterrupt
            raised.append(error)

    assert raised == [first, first, second]
    assert client.history(query).metadata.symbol == "SPY"
    assert len(client.queries) == 4


def test_fail_next_validates_its_arguments() -> None:
    client = spy_client()

    with pytest.raises(TypeError):
        client.fail_next("boom")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        client.fail_next(NoDataError("x"), times=True)
    with pytest.raises(ValueError):
        client.fail_next(NoDataError("x"), times=0)


def test_history_rejects_a_query_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        spy_client().history("SPY")  # type: ignore[arg-type]


def test_as_yahoo_client_returns_its_argument() -> None:
    client = spy_client()

    assert as_yahoo_client(client) is client
