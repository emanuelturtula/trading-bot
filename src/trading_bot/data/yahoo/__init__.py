"""The Yahoo Finance market data provider (spec 011).

- ``history``: ``HistoryQuery``, ``ChartMetadata``, ``YahooHistory`` and the blocking
  ``YahooClient`` port.
- ``instruments``: ``check_yahoo_symbol`` and ``ticker_info``, which map Yahoo symbols and chart
  metadata to ``TickerInfo`` without any I/O.
- ``client``: ``YFinanceClient``, ``configure_yfinance`` and ``map_yahoo_error``. It is the only
  module that imports yfinance, so the source can be replaced by rewriting it.
- ``provider``: ``YFinanceProvider``, ``plan_history``, ``is_unpublished_hour`` and
  ``publishable_4h``: ``1h`` and ``1d`` fetches, ``4h`` candles built from hourly bars, and ticker
  validation.
- ``factory``: ``build_yfinance_provider``, which wires the client, the transport and the
  provider (called by the app lifespan from #16 on).

Import from the submodules: this package re-exports nothing, and only ``client`` and ``factory``
load yfinance.
"""
