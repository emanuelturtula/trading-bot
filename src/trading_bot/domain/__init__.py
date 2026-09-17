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
imports ``exchange_calendars``.
"""
