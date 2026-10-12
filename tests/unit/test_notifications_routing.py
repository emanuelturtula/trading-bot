"""Signals reach only the bot of the user they belong to (spec 024, D5)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

import pytest

from tests.fixtures.signal_messages import notification
from trading_bot.notifications.notifier import SignalNotification
from trading_bot.notifications.routing import OwnerRouter


class Recorder:
    def __init__(self) -> None:
        self.sent: list[SignalNotification] = []

    async def notify(self, sent: SignalNotification) -> None:
        self.sent.append(sent)


def test_the_owners_signals_go_to_the_bot_and_the_others_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder()
    router = OwnerRouter(recorder, owner_id=1)
    mine = notification()
    theirs = dataclasses.replace(notification(ticker="NVDA"), owner_id=2)

    with caplog.at_level(logging.INFO):
        asyncio.run(router.notify(mine))
        asyncio.run(router.notify(theirs))

    assert recorder.sent == [mine]
    assert any("NVDA" in record.getMessage() for record in caplog.records)
    assert not any("AAPL" in record.getMessage() for record in caplog.records)
