"""Market data layer: the provider port and the glue every provider shares (spec 010).

- ``provider``: the async ``MarketDataProvider`` Protocol, ``CandleRequest`` and
  ``MAX_LOOKBACK``. Providers take ``now`` explicitly and never read the wall clock.
- ``errors``: the typed ``MarketDataError`` hierarchy, with a ``retryable`` flag per class.
- ``tickers``: ``TickerInfo``, ``parse_ticker`` and ``ensure_supported``, the single
  implementation of the v1 instrument policy (US-listed stocks and ETFs quoted in USD).
- ``pipeline``: ``candle_window`` to plan a fetch and ``prepare_candles``, which every provider
  uses to normalize a raw frame, drop bad rows and the open candle, and require the last closed
  candle; it is the only module of the package that logs.

The pure frame operations live in ``trading_bot.domain.candle_normalization`` and
``trading_bot.domain.closed_candles``. This package re-exports nothing and decides no signals.
"""
