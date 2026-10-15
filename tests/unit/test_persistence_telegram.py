"""Each user's bot, linked chats and pause (spec 025, D1-D4, D6).

The repository only moves ciphertext and digests, so opaque literals stand in for them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.repositories import frozen_clock
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import DuplicateBotTokenError, UnknownUserError
from trading_bot.persistence.owner import ALL_OWNERS
from trading_bot.persistence.repositories.telegram import MAX_CHAT_TITLE, SqlTelegramRepository
from trading_bot.persistence.repositories.tickers import SqlTickerRepository
from trading_bot.persistence.repositories.users import SqlUserRepository

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
LIFETIME = timedelta(minutes=30)
CIPHER = "cipher-" + "a" * 20
FINGER = "f" * 64
OTHER_FINGER = "e" * 64
DIGEST = "d" * 64
GROUP = -(10**12) - 5  # group ids are negative and do not fit 32 bits
PRIVATE = 4000 + 2


class MovableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


def add_users(database: Database, *names: str) -> list[int]:
    with database.session() as session:
        users = SqlUserRepository(session, clock=frozen_clock(NOW))
        return [users.add(name).id for name in names]


def test_a_bot_is_saved_read_and_listed_for_active_users_only(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        assert repository.bot(ana) is None
        saved = repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username="ana_bot")
        repository.save_bot(bob, ciphertext=CIPHER, fingerprint=OTHER_FINGER, username=None)

        assert saved.user_id == ana
        assert saved.bot_username == "ana_bot"
        assert saved.created_at == NOW == saved.updated_at
        assert saved.link_expires_at is None
        assert repository.bot(ana) == saved
        assert [bot.user_id for bot in repository.runnable()] == [ana, bob]
        assert CIPHER not in repr(saved)
        assert FINGER not in repr(saved)

        SqlUserRepository(session).set_active(bob, False)
        assert [bot.user_id for bot in repository.runnable()] == [ana]


def test_the_same_token_cannot_serve_two_accounts(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)

        with pytest.raises(DuplicateBotTokenError, match="another account"):
            repository.save_bot(bob, ciphertext=CIPHER, fingerprint=FINGER, username=None)

        assert repository.bot(bob) is None
        # The owner may save it again (a new username, say).
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username="renamed")
        stored = repository.bot(ana)
        assert stored is not None
        assert stored.bot_username == "renamed"


def test_a_new_token_forgets_the_old_bots_chats_and_code(database: Database) -> None:
    (ana,) = add_users(database, "ana")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)
        repository.link_chat(ana, PRIVATE)
        repository.issue_link(ana, DIGEST, LIFETIME)

        repository.save_bot(ana, ciphertext="new", fingerprint=FINGER, username=None)
        assert [chat.chat_id for chat in repository.chats(ana)] == [PRIVATE]  # same bot

        repository.save_bot(ana, ciphertext="new", fingerprint=OTHER_FINGER, username=None)
        stored = repository.bot(ana)
        assert repository.chats(ana) == ()
        assert stored is not None
        assert stored.link_expires_at is None
        assert not repository.redeem_link(ana, DIGEST, PRIVATE, None)


def test_a_link_code_works_once_and_before_it_expires(database: Database) -> None:
    (ana,) = add_users(database, "ana")
    clock = MovableClock(NOW)
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=clock)
        assert repository.issue_link(ana, DIGEST, LIFETIME) is None  # no bot yet
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)

        assert repository.issue_link(ana, DIGEST, LIFETIME) == NOW + LIFETIME
        assert not repository.redeem_link(ana, "wrong", GROUP, "Family")
        assert not repository.redeem_link(ana, "", GROUP, "Family")
        assert repository.redeem_link(ana, DIGEST, GROUP, "Family")
        assert not repository.redeem_link(ana, DIGEST, PRIVATE, None)  # spent

        repository.issue_link(ana, DIGEST, LIFETIME)
        clock.instant = NOW + LIFETIME
        assert not repository.redeem_link(ana, DIGEST, PRIVATE, None)  # expired

        chats = repository.chats(ana)
        assert [(chat.chat_id, chat.title) for chat in chats] == [(GROUP, "Family")]
        assert str(GROUP) not in repr(chats[0])


def test_a_code_of_one_user_does_not_link_on_another_users_bot(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)
        repository.save_bot(bob, ciphertext=CIPHER, fingerprint=OTHER_FINGER, username=None)
        repository.issue_link(ana, DIGEST, LIFETIME)

        assert not repository.redeem_link(bob, DIGEST, PRIVATE, None)
        assert repository.redeem_link(ana, DIGEST, PRIVATE, None)
        assert repository.chats(bob) == ()


def test_relinking_a_chat_updates_its_title_and_long_titles_are_clipped(
    database: Database,
) -> None:
    (ana,) = add_users(database, "ana")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)
        repository.issue_link(ana, DIGEST, LIFETIME)
        repository.redeem_link(ana, DIGEST, GROUP, "Old")
        repository.issue_link(ana, DIGEST, LIFETIME)
        repository.redeem_link(ana, DIGEST, GROUP, "N" * 500)

        (chat,) = repository.chats(ana)
        assert chat.title == "N" * MAX_CHAT_TITLE


def test_link_unlink_and_delete(database: Database) -> None:
    (ana,) = add_users(database, "ana")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        assert not repository.delete_bot(ana)
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)
        repository.link_chat(ana, PRIVATE)
        repository.link_chat(ana, PRIVATE)  # idempotent
        repository.link_chat(ana, GROUP, "Group")
        assert [chat.chat_id for chat in repository.chats(ana)] == [GROUP, PRIVATE]

        assert repository.unlink(ana, PRIVATE)
        assert not repository.unlink(ana, PRIVATE)
        assert repository.delete_bot(ana)
        assert repository.bot(ana) is None
        assert repository.chats(ana) == ()


def test_set_username_changes_only_an_existing_bot(database: Database) -> None:
    (ana,) = add_users(database, "ana")
    with database.session() as session:
        repository = SqlTelegramRepository(session, clock=frozen_clock(NOW))
        repository.set_username(ana, "nobody")  # no bot: nothing happens
        assert repository.bot(ana) is None
        repository.save_bot(ana, ciphertext=CIPHER, fingerprint=FINGER, username=None)
        repository.set_username(ana, "ana_bot")
        stored = repository.bot(ana)
        assert stored is not None
        assert stored.bot_username == "ana_bot"


# --- the pause of one user (D6) ---------------------------------------------------------------


def test_a_user_pauses_and_resumes_their_own_signals(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    clock = MovableClock(NOW)
    with database.session() as session:
        users = SqlUserRepository(session, clock=clock)
        assert users.paused_since(ana) is None
        assert users.pause(ana) == NOW
        clock.instant = NOW + timedelta(hours=1)
        assert users.pause(ana) == NOW  # idempotent: the first instant is kept
        assert users.paused_since(ana) == NOW
        assert users.paused_since(bob) is None
        assert users.resume(ana)
        assert not users.resume(ana)
        assert users.paused_since(ana) is None
        with pytest.raises(UnknownUserError):
            users.pause(999)


def test_the_engine_skips_the_tickers_of_a_paused_user(database: Database) -> None:
    ana, bob = add_users(database, "ana", "bob")
    with database.session() as session:
        SqlTickerRepository(session, owner=ana).add("AAPL", Timeframe.D1)
        SqlTickerRepository(session, owner=bob).add("MSFT", Timeframe.D1)
        SqlUserRepository(session, clock=frozen_clock(NOW)).pause(ana)

        everyone = SqlTickerRepository(session, owner=ALL_OWNERS)
        assert [ticker.symbol for ticker in everyone.list_enabled()] == ["MSFT"]
        SqlUserRepository(session).resume(ana)
        assert sorted(ticker.symbol for ticker in everyone.list_enabled()) == ["AAPL", "MSFT"]
