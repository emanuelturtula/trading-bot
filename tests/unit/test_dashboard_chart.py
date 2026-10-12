"""The data of a ticker's chart (spec 027, AC1-AC3, AC8)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.session_candles import session_candles
from trading_bot.dashboard.builder import fill
from trading_bot.dashboard.chart import (
    EXCHANGE_ZONE,
    SHOWN_CANDLES,
    chart_payload,
    fetch_lookback,
)
from trading_bot.domain.rules.evaluator import evaluate_each
from trading_bot.domain.rules.schema import parse_rule
from trading_bot.domain.signals import Side, Signal
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredRule
from trading_bot.persistence.signal_records import StoredSignal

ZONE = ZoneInfo("America/Argentina/Buenos_Aires")
END = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def frame(timeframe: Timeframe) -> pd.DataFrame:
    start = (
        datetime(2024, 6, 1, tzinfo=UTC)
        if timeframe is Timeframe.D1
        else datetime(2026, 5, 1, tzinfo=UTC)
    )
    return session_candles(nyse_test_calendar(), timeframe, start, END, seed=7)


def stored_rule(
    rule_id: int,
    name: str,
    conditions: Any,
    *,
    signal: str = "BUY",
    cooldown: int = 5,
    timeframe: Timeframe = Timeframe.D1,
) -> StoredRule:
    document = {
        "name": name,
        "signal": signal,
        "timeframe": timeframe.value,
        "conditions": conditions,
        "cooldown_bars": cooldown,
    }
    return StoredRule(
        id=rule_id,
        rule=parse_rule(document),
        enabled=True,
        created_at=CREATED,
        updated_at=CREATED,
        user_id=1,
    )


def recorded(
    signal_id: int, rule: StoredRule, label: pd.Timestamp, *, timeframe: Timeframe = Timeframe.D1
) -> StoredSignal:
    signal = Signal(
        ticker="AAPL",
        timeframe=timeframe,
        rule_id=str(rule.id),
        side=Side(rule.rule.signal.value),
        candle_close_ts=label.to_pydatetime() + timeframe.duration,
        close_price=10.0,
    )
    return StoredSignal(
        id=signal_id,
        ticker_id=1,
        rule_id=rule.id,
        rule_name=rule.name,
        signal=signal,
        created_at=CREATED,
        notified_at=None,
    )


RSI = stored_rule(1, "RSI low", fill("rsi_level", {"level": 35}))
CROSS = stored_rule(2, "Cross", fill("average_cross", {"fast": 20, "slow": 50}), signal="SELL")
MACD = stored_rule(3, "MACD", fill("macd_cross", {}))


def payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "candles": frame(Timeframe.D1),
        "timeframe": Timeframe.D1,
        "rules": (RSI, CROSS, MACD),
        "signals": (),
        "zone": ZONE,
    }
    values.update(overrides)
    return chart_payload(**values)


def test_daily_candles_are_the_last_window_named_by_trading_day() -> None:
    candles = frame(Timeframe.D1)

    data = payload(candles=candles)

    assert len(data["candles"]) == SHOWN_CANDLES[Timeframe.D1]
    last = data["candles"][-1]
    assert last["time"] == candles.index[-1].tz_convert(EXCHANGE_ZONE).date().isoformat()
    assert last["close"] == candles["close"].iloc[-1]
    assert data["timeframe"] == "1d"


def test_a_short_history_shows_every_candle() -> None:
    candles = frame(Timeframe.D1).iloc[-30:]

    data = payload(candles=candles, rules=())

    assert len(data["candles"]) == 30
    assert data["lines"] == []
    assert data["markers"] == []


def test_intraday_times_are_shifted_to_the_display_zone() -> None:
    candles = frame(Timeframe.H1)
    rule = stored_rule(1, "RSI", fill("rsi_level", {}), timeframe=Timeframe.H1)

    data = payload(candles=candles, timeframe=Timeframe.H1, rules=(rule,))

    label = candles.index[-1]
    assert data["candles"][-1]["time"] == int(label.timestamp()) - 3 * 3600
    assert len(data["candles"]) == SHOWN_CANDLES[Timeframe.H1]


def test_averages_share_the_price_pane_and_oscillators_get_their_own() -> None:
    data = payload()

    panes = {line["label"]: line["pane"] for line in data["lines"]}
    assert panes == {
        "RSI 14": 1,
        "SMA 20": 0,
        "SMA 50": 0,
        "MACD 12 26 9 · MACD line": 2,
        "MACD 12 26 9 · Signal line": 2,
    }
    assert data["levels"] == [{"pane": 1, "value": 35.0, "label": "RSI 14 35"}]
    rsi = next(line for line in data["lines"] if line["label"] == "RSI 14")
    assert 0 < len(rsi["points"]) <= SHOWN_CANDLES[Timeframe.D1]
    assert all(0 <= point["value"] <= 100 for point in rsi["points"])


def test_simulated_firings_follow_the_rule_and_its_cooldown() -> None:
    candles = frame(Timeframe.D1)
    shown = SHOWN_CANDLES[Timeframe.D1]
    triggered = [
        offset
        for offset, evaluation in enumerate(evaluate_each(RSI.rule, candles, last=shown))
        if evaluation.triggered
    ]
    times = [item["time"] for item in payload(rules=(RSI,))["candles"]]

    markers = payload(rules=(RSI,))["markers"]

    offsets = [times.index(marker["time"]) for marker in markers]
    assert offsets
    assert set(offsets) <= set(triggered)
    assert all(later - earlier > RSI.rule.cooldown_bars for earlier, later in pairwise(offsets))
    assert all(not marker["recorded"] and marker["side"] == "BUY" for marker in markers)


def test_a_recorded_signal_replaces_the_simulated_one_on_its_candle() -> None:
    candles = frame(Timeframe.D1)
    simulated = payload(rules=(RSI,))["markers"]
    times = [item["time"] for item in payload(rules=(RSI,))["candles"]]
    offset = times.index(simulated[0]["time"])
    label = candles.index[len(candles) - SHOWN_CANDLES[Timeframe.D1] + offset]
    old = recorded(8, RSI, candles.index[0])  # before the window: ignored

    markers = payload(rules=(RSI,), signals=(recorded(9, RSI, label), old))["markers"]

    on_candle = [marker for marker in markers if marker["time"] == simulated[0]["time"]]
    assert on_candle == [
        {"time": simulated[0]["time"], "side": "BUY", "rule": "RSI low", "recorded": True}
    ]
    assert len(markers) == len(simulated)
    assert [marker["time"] for marker in markers] == sorted(marker["time"] for marker in markers)


def test_a_sell_rule_marks_sells() -> None:
    markers = payload(rules=(CROSS,))["markers"]

    assert markers
    assert {marker["side"] for marker in markers} == {"SELL"}


@pytest.mark.parametrize(
    ("timeframe", "rules", "expected"),
    [
        (Timeframe.D1, (), 250),
        (Timeframe.H1, (), 300),
        (Timeframe.D1, (RSI.rule,), 250 + RSI.rule.stable_warmup()),
        (Timeframe.D1, (MACD.rule, RSI.rule), 250 + MACD.rule.stable_warmup()),
    ],
)
def test_fetch_lookback_adds_the_longest_warmup(
    timeframe: Timeframe, rules: tuple[Any, ...], expected: int
) -> None:
    assert fetch_lookback(timeframe, rules, 5000) == expected
    assert fetch_lookback(timeframe, rules, 100) == 100
