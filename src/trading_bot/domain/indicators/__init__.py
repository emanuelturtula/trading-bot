"""Indicator registry: a closed, validated catalog of technical indicators (spec 005).

Pure like the rest of ``domain/``: indicators receive a candle frame and return per-candle
series, with no I/O, clock or mutable state. TA-Lib computes the values but is imported only by
``talib_kernels.py``, so it can be replaced without touching the registry API.

Import from the submodules: ``trading_bot.domain.indicators.catalog`` for the default
``REGISTRY``, ``registry``, ``spec``, ``params`` and ``errors`` for the types. This package
re-exports nothing, so importing the types does not load TA-Lib.
"""
