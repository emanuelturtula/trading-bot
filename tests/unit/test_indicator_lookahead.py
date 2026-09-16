"""Look-ahead tests of every catalog indicator on every scenario (spec 005, T10; AC11).

``REGISTRY.compute`` returns full-length series on the candle index, which is the harness
contract for series-valued functions. Comparison is exact: TA-Lib recurrences and the
undefined-value masks are bitwise stable, so no tolerance is allowed (CLAUDE.md rule 4).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.lookahead import LookaheadError, assert_no_lookahead
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import IndicatorParams
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.indicators.spec import FloatArray, IndicatorSpec, InputArrays, OutputSpec

SCENARIO_SEEDS = {scenario: seed for seed, scenario in enumerate(Scenario, start=21)}


def computing(
    registry: IndicatorRegistry, name: str, params: dict[str, object] | None = None
) -> Callable[[pd.DataFrame], dict[str, pd.Series[float]]]:
    def compute(candles: pd.DataFrame) -> dict[str, pd.Series[float]]:
        return registry.compute(name, params or {}, candles)

    compute.__qualname__ = f"compute_{name}"
    return compute


@pytest.mark.parametrize("scenario", list(Scenario))
@pytest.mark.parametrize("name", REGISTRY.names)
def test_indicator_has_no_lookahead(name: str, scenario: Scenario) -> None:
    candles = synthetic_candles(300, seed=SCENARIO_SEEDS[scenario], scenario=scenario)

    report = assert_no_lookahead(computing(REGISTRY, name), candles)

    assert report.non_missing_values > 0


def peeking_kernel(inputs: InputArrays, params: IndicatorParams) -> tuple[FloatArray, ...]:
    """A cheat: the value at ``i`` is the close of candle ``i + 1``."""
    close = inputs["close"]
    value = np.full(len(close), np.nan)
    value[:-1] = close[1:]
    return (value,)


def test_the_check_detects_a_peeking_kernel_behind_the_registry() -> None:
    """Control: the same check fails for a kernel that reads the next candle."""
    spec = IndicatorSpec(
        name="peek",
        label="Peek",
        description="Reads the next close.",
        inputs=("close",),
        params=(),
        constraints=(),
        outputs=(OutputSpec(name="value", label="Peek"),),
        lookback=lambda params: 0,
        settle=lambda params: 0,
        kernel=peeking_kernel,
    )
    candles = synthetic_candles(60, seed=1, scenario=Scenario.MIXED)

    with pytest.raises(LookaheadError) as caught:
        assert_no_lookahead(computing(IndicatorRegistry((spec,)), "peek"), candles)

    assert caught.value.violation.kind == "value_mismatch"
