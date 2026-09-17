"""Pure domain layer: timeframes, the candle frame contract, signals, the market calendar,
indicators and rules.

Modules here receive values or DataFrames and return results. They perform no I/O, read no
clock, and hold no globals or mutable module state (CLAUDE.md rule 3). Import from the
submodules (``trading_bot.domain.signals``, ``trading_bot.domain.timeframe``, ...): this package
re-exports nothing, so consumers that only need ``Signal`` or ``Timeframe`` do not load pandas.
The ``indicators`` subpackage holds the indicator registry (spec 005); only its
``talib_kernels`` module imports TA-Lib. The ``rules`` subpackage holds the rule document
(spec 006); only its ``schema`` and ``json_schema`` modules import pydantic, so ``rules.errors``
stays free of pandas, numpy and TA-Lib for the callers that only map error kinds. The ``rules``
subpackage also holds the rule evaluator (``rules.evaluator``: ``evaluate`` and
``evaluate_each``, spec 007), which decides whether a rule fires on closed candles. The
``market_calendar`` subpackage holds the session calendar (spec 009): exchange sessions, the
candle grid and real candle closes for closedness and scheduling; only its ``nyse`` module
imports ``exchange_calendars``. The ``candle_normalization`` and ``closed_candles`` modules turn
provider frames into closed canonical candles (spec 010): ``normalize_candles`` converts a raw
frame to the candle contract and reports the rows it drops, and ``drop_open_candle`` keeps only
the candles closed at an injected ``now``. The ``candle_resampling`` module builds
session-anchored ``4h`` candles from hourly candles (spec 011): ``resample_hourly_to_4h`` emits
the ``4h`` slots closed at ``now`` with a report of their missing hourly bars.
"""
