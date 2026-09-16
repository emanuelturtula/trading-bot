"""Pure domain layer: timeframes, the candle frame contract and signals (spec 004).

Modules here receive values or DataFrames and return results. They perform no I/O, read no
clock, and hold no globals or mutable module state (CLAUDE.md rule 3). Import from the
submodules (``trading_bot.domain.signals``, ``trading_bot.domain.timeframe``, ...): this package
re-exports nothing, so consumers that only need ``Signal`` or ``Timeframe`` do not load pandas.
"""
