"""Notifications: how a decided signal reaches the user (spec 015, Design 7).

- ``notifier``: the async ``Notifier`` Protocol, the ``SignalNotification`` it delivers and
  ``NotificationError``. The engine (#14) decides **what** to send and calls ``notify`` exactly
  once per new signal; an implementation owns the transport, its retries and its rate limits
  behind that one call (decision D132).

``TelegramNotifier`` joins this package with #17, and the chart with #18; ``main.py`` injects the
implementation, so the engine only ever sees the Protocol. Nothing here places an order: a
notifier can only deliver a message (CLAUDE.md rule 1). This package re-exports nothing.
"""
