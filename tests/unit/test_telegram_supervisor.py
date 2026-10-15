"""``BotSupervisor``, ``UserRouter`` and the import of the environment bot (spec 025, D4-D7).

Bots run for real on ``FakeUpdateSource`` and ``RecordingSender``: no network. Fake tokens and
the master key are built at runtime (CLAUDE.md rule 2).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from tests.fixtures.calendars import nyse_test_calendar
from tests.fixtures.runtime import FakeUpdateSource, RecordingSender
from tests.fixtures.signal_messages import MADRID, notification
from trading_bot.persistence.database import Database
from trading_bot.persistence.repositories.telegram import SqlTelegramRepository
from trading_bot.persistence.repositories.users import SqlUserRepository
from trading_bot.secretbox import SecretBox, new_master_key
from trading_bot.telegram_bot.poller import IncomingMessage, PollError, PollErrorKind
from trading_bot.telegram_bot.supervisor import (
    LINK_CODE_ALPHABET,
    LINK_CODE_LENGTH,
    LINK_LIFETIME,
    BotSupervisor,
    UserRouter,
    import_env_bot,
    link_code_digest,
    new_link_code,
)

ANA_TOKEN = "111111111" + ":" + "a" * 35
BOB_TOKEN = "222222222" + ":" + "b" * 35
NEW_TOKEN = "333333333" + ":" + "c" * 35
CHAT = 4000 + 2
GROUP = -(10**12) - 7
STRANGER = 9000 + 9
SUPERVISOR_LOGGER = "trading_bot.telegram_bot.supervisor"


def run_async(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


class Handler:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    async def handle(self, text: str, *, bot_username: str | None = None) -> str | None:
        return f"user {self.user_id}: {text}"


class Harness:
    """A supervisor whose sources and senders are recorded per token."""

    def __init__(self, database: Database, *, box: SecretBox | None) -> None:
        self.database = database
        self.box = box
        self.sources: dict[str, list[FakeUpdateSource]] = {}
        self.senders: dict[str, list[RecordingSender]] = {}
        self.supervisor = BotSupervisor(
            database=database,
            box=box,
            build_source=self.build_source,
            build_sender=self.build_sender,
            make_handler=Handler,
            production=True,
            display_tz=MADRID,
            calendar=nyse_test_calendar(),
        )

    def build_source(self, token: SecretStr) -> FakeUpdateSource:
        source = FakeUpdateSource(username=f"bot_{token.get_secret_value()[:3]}")
        self.sources.setdefault(token.get_secret_value(), []).append(source)
        return source

    def build_sender(self, token: SecretStr) -> RecordingSender:
        sender = RecordingSender()
        self.senders.setdefault(token.get_secret_value(), []).append(sender)
        return sender

    def source(self, token: str) -> FakeUpdateSource:
        return self.sources[token][-1]

    def save(self, user_id: int, token: str) -> None:
        assert self.box is not None
        secret = SecretStr(token)
        with self.database.session() as session:
            SqlTelegramRepository(session).save_bot(
                user_id,
                ciphertext=self.box.encrypt(secret),
                fingerprint=self.box.fingerprint(secret),
                username=None,
            )

    def link(self, user_id: int, chat_id: int) -> None:
        with self.database.session() as session:
            SqlTelegramRepository(session).link_chat(user_id, chat_id)

    def issue_code(self, user_id: int) -> str:
        code = new_link_code()
        with self.database.session() as session:
            SqlTelegramRepository(session).issue_link(
                user_id, link_code_digest(code), LINK_LIFETIME
            )
        return code

    def chats(self, user_id: int) -> list[int]:
        with self.database.session() as session:
            return [chat.chat_id for chat in SqlTelegramRepository(session).chats(user_id)]


def add_users(database: Database, *names: str) -> list[int]:
    with database.session() as session:
        users = SqlUserRepository(session)
        return [users.add(name).id for name in names]


async def settle(source: FakeUpdateSource) -> None:
    """Wait until the poller consumed everything queued and is long-polling again."""
    # The poller reaches the database in worker threads (/link, the username), so this waits
    # on the clock rather than for a number of loop turns.
    for _ in range(1000):
        if not source.batches and source._wakeup is not None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the poller never became idle")


def incoming(update_id: int, chat_id: int, text: str, title: str | None = None) -> IncomingMessage:
    return IncomingMessage(update_id=update_id, chat_id=chat_id, text=text, chat_title=title)


def test_link_codes_are_random_typable_and_stored_as_digests() -> None:
    codes = {new_link_code() for _ in range(50)}

    assert len(codes) == 50
    for code in codes:
        assert len(code) == LINK_CODE_LENGTH
        assert set(code) <= set(LINK_CODE_ALPHABET)
    code = next(iter(codes))
    assert link_code_digest(f" {code.lower()} ") == link_code_digest(code)
    assert code not in link_code_digest(code)
    assert not set("01ILO") & set(LINK_CODE_ALPHABET)


@run_async
async def test_reload_runs_one_bot_per_active_user_and_follows_the_database(
    database: Database,
) -> None:
    ana, bob = add_users(database, "ana", "bob")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    supervisor = harness.supervisor
    harness.save(ana, ANA_TOKEN)
    harness.link(ana, CHAT)
    harness.save(bob, BOB_TOKEN)
    try:
        await supervisor.reload()
        assert supervisor.running(ana)
        assert supervisor.running(bob)
        assert supervisor.notifier_for(ana) is not None
        assert supervisor.notifier_for(bob) is None  # no linked chat yet
        first_ana = harness.source(ANA_TOKEN)

        await supervisor.reload()  # nothing changed: nothing restarts
        assert len(harness.sources[ANA_TOKEN]) == 1

        harness.save(ana, NEW_TOKEN)  # a new token: only ana's bot restarts
        with database.session() as session:
            SqlUserRepository(session).set_active(bob, False)  # a disabled user's bot stops
        await supervisor.reload()
        assert first_ana.closed
        assert harness.senders[ANA_TOKEN][0].closed
        assert supervisor.running(ana)
        assert NEW_TOKEN in harness.sources
        assert supervisor.notifier_for(ana) is None  # the new bot has no chat yet
        assert not supervisor.running(bob)
        assert harness.source(BOB_TOKEN).closed
    finally:
        await supervisor.aclose()

    assert harness.source(NEW_TOKEN).closed
    assert harness.senders[NEW_TOKEN][0].closed
    assert "running(bots=0)" not in repr(supervisor)  # the repr is safe and small
    await supervisor.reload()  # closed: a late reload starts nothing
    assert not supervisor.running(ana)


@run_async
async def test_a_bot_answers_its_owners_chats_only(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    harness.save(ana, ANA_TOKEN)
    harness.link(ana, CHAT)
    harness.save(bob, BOB_TOKEN)
    harness.link(bob, GROUP)
    try:
        await harness.supervisor.reload()
        ana_source, bob_source = harness.source(ANA_TOKEN), harness.source(BOB_TOKEN)
        await settle(ana_source)
        await settle(bob_source)
        ana_source.queue([incoming(1, CHAT, "/list"), incoming(2, GROUP, "/list")])
        bob_source.queue([incoming(1, GROUP, "/list")])
        await settle(ana_source)
        await settle(bob_source)
    finally:
        await harness.supervisor.aclose()

    assert ana_source.sent == [(CHAT, f"user {ana}: /list")]  # bob's group is ignored
    assert bob_source.sent == [(GROUP, f"user {bob}: /list")]


@run_async
async def test_link_with_a_valid_code_adds_the_chat_and_a_wrong_one_does_not(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    (ana,) = add_users(database, "ana")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    harness.save(ana, ANA_TOKEN)
    try:
        await harness.supervisor.reload()
        source = harness.source(ANA_TOKEN)
        await settle(source)
        code = harness.issue_code(ana)
        with caplog.at_level(logging.INFO):
            source.queue(
                [
                    incoming(1, STRANGER, "/list"),
                    incoming(2, STRANGER, "/link WRONG234"),
                    incoming(3, GROUP, f"/link@bot_111 {code.lower()}", "Family"),
                    incoming(4, GROUP, "/list"),
                    incoming(5, CHAT, f"/link {code}"),  # spent
                ]
            )
            await settle(source)
        assert harness.chats(ana) == [GROUP]
        assert harness.supervisor.notifier_for(ana) is not None
    finally:
        await harness.supervisor.aclose()

    assert [chat for chat, _ in source.sent] == [STRANGER, GROUP, GROUP, CHAT]
    assert "wrong or expired" in source.sent[0][1]
    assert source.sent[1][1].startswith("Linked.")
    assert source.sent[2][1] == f"user {ana}: /list"
    assert "wrong or expired" in source.sent[3][1]
    assert code not in caplog.text
    assert str(GROUP) not in caplog.text
    with database.session() as session:
        stored = SqlTelegramRepository(session).bot(ana)
    assert stored is not None
    assert stored.bot_username == "bot_111"  # what Telegram reported is stored


@run_async
async def test_a_bot_that_cannot_be_decrypted_is_skipped_and_logged(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    ana, bob = add_users(database, "ana", "bob")
    old = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    old.save(ana, ANA_TOKEN)
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    harness.save(bob, BOB_TOKEN)
    try:
        with caplog.at_level(logging.ERROR, logger=SUPERVISOR_LOGGER):
            await harness.supervisor.reload()
        assert not harness.supervisor.running(ana)
        assert harness.supervisor.running(bob)
    finally:
        await harness.supervisor.aclose()

    assert f"the bot of user {ana} cannot be decrypted" in caplog.text
    assert ANA_TOKEN not in caplog.text


@run_async
async def test_without_a_box_reload_does_nothing_and_run_static_serves_the_env_bot(
    database: Database,
) -> None:
    (ana,) = add_users(database, "ana")
    harness = Harness(database, box=None)
    supervisor = harness.supervisor
    assert not supervisor.enabled
    assert supervisor.box is None
    try:
        await supervisor.reload()
        assert not supervisor.running(ana)
        await supervisor.run_static(ana, SecretStr(ANA_TOKEN), [CHAT])
        assert supervisor.running(ana)
        assert supervisor.notifier_for(ana) is not None
        source = harness.source(ANA_TOKEN)
        await settle(source)
        source.queue([incoming(1, CHAT, "/list"), incoming(2, GROUP, f"/link {'A' * 8}")])
        await settle(source)
    finally:
        await supervisor.stop_polling()
        assert not supervisor.running(ana)
        assert not harness.senders[ANA_TOKEN][0].closed  # senders outlive the pollers
        await supervisor.aclose()

    assert harness.senders[ANA_TOKEN][0].closed
    # The env bot has no /link: a stranger's code is ignored like any other message.
    assert source.sent == [(CHAT, f"user {ana}: /list")]


@run_async
async def test_validate_returns_the_username_or_raises_and_closes_the_source(
    database: Database,
) -> None:
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))

    assert await harness.supervisor.validate(SecretStr(ANA_TOKEN)) == "bot_111"
    assert harness.source(ANA_TOKEN).closed

    def rejecting(token: SecretStr) -> FakeUpdateSource:
        source = FakeUpdateSource()
        source.username_errors.append(PollError(PollErrorKind.REJECTED_TOKEN))
        harness.sources.setdefault("rejected", []).append(source)
        return source

    harness.supervisor._build_source = rejecting
    with pytest.raises(PollError):
        await harness.supervisor.validate(SecretStr(BOB_TOKEN))
    assert harness.sources["rejected"][0].closed


@run_async
async def test_a_failing_sender_close_is_logged_and_does_not_stop_the_others(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    ana, bob = add_users(database, "ana", "bob")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    harness.save(ana, ANA_TOKEN)
    harness.save(bob, BOB_TOKEN)
    await harness.supervisor.reload()
    await settle(harness.source(ANA_TOKEN))
    await settle(harness.source(BOB_TOKEN))

    async def broken() -> None:
        raise OSError("boom")

    harness.senders[ANA_TOKEN][0].aclose = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger=SUPERVISOR_LOGGER):
        await harness.supervisor.aclose()

    assert f"closing the telegram sender of user {ana} failed: OSError" in caplog.text
    assert "boom" not in caplog.text
    assert harness.senders[BOB_TOKEN][0].closed


@run_async
async def test_the_router_sends_each_signal_through_its_owners_bot(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    ana, bob = add_users(database, "ana", "bob")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    harness.save(ana, ANA_TOKEN)
    harness.link(ana, CHAT)
    harness.link(ana, GROUP)
    harness.save(bob, BOB_TOKEN)  # no chat: bob's signals are logged
    router = UserRouter(harness.supervisor)
    try:
        await harness.supervisor.reload()
        await settle(harness.source(ANA_TOKEN))
        await settle(harness.source(BOB_TOKEN))
        with caplog.at_level(logging.INFO):
            await router.notify(dataclasses.replace(notification(), owner_id=ana))
            await router.notify(dataclasses.replace(notification(ticker="MSFT"), owner_id=bob))
        await router.aclose()
    finally:
        await harness.supervisor.aclose()

    sent = harness.senders[ANA_TOKEN][0].sent
    assert sorted(chat for chat, _ in sent) == sorted([CHAT, GROUP])
    assert all("AAPL" in text for _, text in sent)
    assert harness.senders[BOB_TOKEN][0].sent == []
    assert "MSFT" in caplog.text


@run_async
async def test_refresh_chats_without_a_running_bot_is_harmless(database: Database) -> None:
    (ana,) = add_users(database, "ana")
    harness = Harness(database, box=SecretBox(SecretStr(new_master_key())))
    await harness.supervisor.refresh_chats(ana)
    assert harness.supervisor.notifier_for(ana) is None


def test_the_env_bot_is_imported_once_with_its_chats(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    box = SecretBox(SecretStr(new_master_key()))

    assert import_env_bot(database, box, ana, SecretStr(ANA_TOKEN), [CHAT, GROUP])
    assert not import_env_bot(database, box, ana, SecretStr(NEW_TOKEN), [CHAT])  # already has one
    assert not import_env_bot(database, box, bob, SecretStr(ANA_TOKEN), [CHAT])  # token taken

    with database.session() as session:
        repository = SqlTelegramRepository(session)
        stored = repository.bot(ana)
        assert stored is not None
        assert box.decrypt(stored.token_ciphertext).get_secret_value() == ANA_TOKEN
        assert sorted(chat.chat_id for chat in repository.chats(ana)) == sorted([CHAT, GROUP])
        assert repository.bot(bob) is None


def test_link_lifetime_is_half_an_hour() -> None:
    assert timedelta(minutes=30) == LINK_LIFETIME
