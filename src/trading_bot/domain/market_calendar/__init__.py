"""Market calendar: exchange sessions, the candle grid and real candle closes (spec 009).

``sessions`` holds the model (``MarketCalendar``, ``Session``, ``CandleSlot`` and the calendar
errors) and every query; it imports only the standard library and the domain. ``nyse`` holds
``build_nyse_calendar``, the only module that imports ``exchange_calendars``. Import from the
submodules: this package re-exports nothing.
"""
