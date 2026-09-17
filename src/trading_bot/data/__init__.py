"""Market data layer: the provider port, the glue every provider shares and the Yahoo provider
(specs 010 and 011).

- ``provider``: the async ``MarketDataProvider`` Protocol, ``CandleRequest`` and
  ``MAX_LOOKBACK``. Providers take ``now`` explicitly and never read the wall clock.
- ``errors``: the typed ``MarketDataError`` hierarchy, with a ``retryable`` flag per class.
- ``tickers``: ``TickerInfo``, ``parse_ticker`` and ``ensure_supported``, the single
  implementation of the v1 instrument policy (US-listed stocks and ETFs quoted in USD).
- ``pipeline``: ``candle_window`` to plan a fetch, ``prepare_candles``, which every provider
  uses to normalize a raw frame, drop bad rows and the open candle, and require the last closed
  candle, and ``log_dropped_rows`` for its dropped-row records.
- ``transport``: ``ProviderTransport``, which runs blocking provider calls in a worker thread,
  one at a time, with pacing (``TokenBucket``), per-attempt timeouts and retries with backoff and
  jitter (``RetryPolicy``) for ``ProviderUnavailableError`` only. It knows nothing about Yahoo.
- ``yahoo``: ``YFinanceProvider`` and its parts; only ``yahoo.client`` imports yfinance.

The pure frame operations live in ``trading_bot.domain.candle_normalization``,
``trading_bot.domain.closed_candles`` and ``trading_bot.domain.candle_resampling``. Records carry
codes, normalized tickers, timeframe codes and ISO labels, never provider text. This package
re-exports nothing and decides no signals.
"""
