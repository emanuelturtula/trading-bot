"""Notifications: how a decided signal reaches the user (spec 015, Design 7).

- ``notifier``: the async ``Notifier`` Protocol, the ``SignalNotification`` it delivers and
  ``NotificationError``. The engine (#14) decides **what** to send and calls ``notify`` exactly
  once per new signal; an implementation owns the transport, its retries and its rate limits
  behind that one call (decision D132).

- ``message``: the pure text of a signal message (spec 017);
- ``telegram``: ``TelegramNotifier``, its retry policy, and ``BotMessageSender``, the only module
  that imports python-telegram-bot (#17);
- ``log_notifier``: ``LogNotifier``, used while Telegram is not configured (spec 017, U2).

The chart joins with #18. ``trading_bot.runtime`` picks the implementation, so the engine only
ever sees the Protocol. Nothing here places an order: a
notifier can only deliver a message (CLAUDE.md rule 1). This package re-exports nothing.
"""
