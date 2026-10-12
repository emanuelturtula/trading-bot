"""Send each signal only to the bot of the user it belongs to (spec 024, D5).

Until each user configures a Telegram bot of their own (spec 025), the bot of
``TB_TELEGRAM_BOT_TOKEN`` belongs to the default owner, the first administrator: it delivers
that user's signals, and every other user's signal is only logged. Without this, one user's
signals would reach another user's chats.
"""

from __future__ import annotations

from trading_bot.notifications.log_notifier import LogNotifier
from trading_bot.notifications.notifier import Notifier, SignalNotification

__all__ = ["OwnerRouter"]


class OwnerRouter:
    """A ``Notifier`` that hands ``owner_id``'s signals to ``notifier`` and logs the others."""

    def __init__(self, notifier: Notifier, owner_id: int) -> None:
        self._notifier = notifier
        self._owner_id = owner_id
        self._others = LogNotifier()

    async def notify(self, notification: SignalNotification) -> None:
        if notification.owner_id == self._owner_id:
            await self._notifier.notify(notification)
        else:
            await self._others.notify(notification)
