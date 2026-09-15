"""Independent reference vs ``REGISTRY.compute`` over the T13 matrix (spec 005; AC7, AC8).

The reference (``tests/fixtures/indicator_reference.py``) is written from Design 6.2 and 7 alone,
without reading ``talib_kernels.py`` (spec 005, Design 1). NaN positions must match exactly;
every other value must be within AC7's tolerance, ``1e-9 * scale``, where ``scale`` bounds the
magnitude of the compared output in its own unit.

The matrix covers every ``Scenario`` x seeds 0-3 x timeframes ``1d``/``1h`` (500 candles),
``volatility=0`` frames, the two extreme ``start_price`` frames and 60 seeded random draws, times
the default, minimum and large parameter set of every indicator (Test plan T9 row). Both sides
are vectorized (TA-Lib in C; the reference with numpy), so the full matrix runs in about a
second: it is not thinned down for the AC20 budget.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.candles import Scenario, synthetic_candles
from tests.fixtures.indicator_reference import compute_reference
from trading_bot.domain.indicators.catalog import REGISTRY

# name -> [default, minimum, large] parameter sets (spec Design 6.1 ranges; Test plan T9 row).
# Independent of the developer's T9 table in tests/unit/test_indicator_catalog.py: both come
# from the same spec table, but this file never imports the developer's tests.
PARAMETER_SETS: dict[str, list[dict[str, object]]] = {
    "sma": [{}, {"length": 2}, {"length": 50}],
    "volume_sma": [{}, {"length": 2}, {"length": 50}],
    "ema": [{}, {"length": 2}, {"length": 50}],
    "bbands": [{}, {"length": 2, "std": 0.1}, {"length": 50}],
    "rsi": [{}, {"length": 2}, {"length": 50}],
    "atr": [{}, {"length": 1}, {"length": 50}],
    "adx": [{}, {"length": 2}, {"length": 50}],
    "macd": [{}, {"fast": 2, "slow": 3, "signal": 1}, {"fast": 50, "slow": 100, "signal": 30}],
    "stoch": [
        {},
        {"length": 1, "smooth_k": 1, "smooth_d": 1},
        {"length": 30, "smooth_k": 5, "smooth_d": 5},
    ],
    "obv": [{}, {"signal": 2}, {"signal": 50}],
}

_RANDOM_DRAW_SEED = 20260914  # spec 005 approval date: an arbitrary fixed seed, not a secret


def _tolerance_scale(name: str, frame: pd.DataFrame) -> float:
    """AC7's ``scale``: bounds the magnitude of the compared output, in its own unit."""
    if name in ("rsi", "stoch", "adx"):
        return 100.0
    if name == "obv":
        return max(1.0, float(frame["volume"].sum()))
    if name == "volume_sma":
        return max(1.0, float(frame["volume"].max()))
    return float(frame["high"].max())


def _build_frames() -> list[tuple[str, pd.DataFrame]]:
    """The T13 matrix of frames, built once and reused for every indicator and parameter set."""
    frames: list[tuple[str, pd.DataFrame]] = []
    for scenario in Scenario:
        for seed in range(4):
            for timeframe in ("1d", "1h"):
                frame = synthetic_candles(500, seed=seed, scenario=scenario, timeframe=timeframe)
                frames.append((f"{scenario}-seed{seed}-{timeframe}", frame))
    for scenario in Scenario:
        for seed in range(2):
            frame = synthetic_candles(500, seed=seed, scenario=scenario, volatility=0.0)
            frames.append((f"{scenario}-seed{seed}-flat", frame))
    for start_price in (1e-6, 5e7):
        frame = synthetic_candles(
            500, seed=0, scenario=Scenario.RANDOM_WALK, start_price=start_price, volatility=0.25
        )
        frames.append((f"start_price={start_price}", frame))
    rng = np.random.default_rng(_RANDOM_DRAW_SEED)
    scenarios = list(Scenario)
    for draw in range(60):
        n = int(rng.integers(60, 300))
        scenario = scenarios[int(rng.integers(0, len(scenarios)))]
        seed = int(rng.integers(0, 100_000))
        start_price = 10.0 ** rng.uniform(-3.0, 5.0)
        volatility = float(rng.uniform(0.0, 0.3))
        frame = synthetic_candles(
            n, seed=seed, scenario=scenario, start_price=start_price, volatility=volatility
        )
        frames.append((f"draw{draw}-n{n}-{scenario}-seed{seed}", frame))
    return frames


FRAMES = _build_frames()


def _assert_matches_reference(
    name: str, params: Mapping[str, object], frame_label: str, frame: pd.DataFrame
) -> int:
    """Compare one (indicator, params, frame); return the number of finite cells compared."""
    resolved = REGISTRY.validate_params(name, params)
    actual = REGISTRY.compute(name, resolved, frame)
    reference = compute_reference(name, resolved, frame)
    scale = _tolerance_scale(name, frame)
    finite_count = 0
    for output, series in actual.items():
        computed = series.to_numpy()
        expected = reference[output]
        computed_nan = np.isnan(computed)
        where = f"{name}.{output} params={dict(resolved)} frame={frame_label}"
        assert np.array_equal(computed_nan, np.isnan(expected)), f"NaN positions differ: {where}"
        finite = ~computed_nan
        finite_count += int(finite.sum())
        if not finite.any():
            continue
        difference = np.abs(computed[finite] - expected[finite])
        worst = float(difference.max())
        assert worst <= 1e-9 * scale, f"{where}: worst difference {worst}, allowed {1e-9 * scale}"
    return finite_count


@pytest.mark.parametrize("name", REGISTRY.names)
def test_matches_independent_reference_over_the_full_matrix(name: str) -> None:
    finite_count = sum(
        _assert_matches_reference(name, params, frame_label, frame)
        for params in PARAMETER_SETS[name]
        for frame_label, frame in FRAMES
    )

    assert finite_count > 0, f"no finite value was ever compared for {name}"


def test_parameter_sets_cover_every_indicator() -> None:
    assert set(PARAMETER_SETS) == set(REGISTRY.names)


def test_the_frame_matrix_is_not_vacuous() -> None:
    assert len(FRAMES) >= 100
    assert len({label for label, _ in FRAMES}) == len(FRAMES)
