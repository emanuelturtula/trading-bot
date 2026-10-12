"""The user repository (spec 023, AC1-AC4).

Every instant comes from an injected clock, so expiry is tested with literal instants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.repositories import frozen_clock
from trading_bot.persistence.database import Database
from trading_bot.persistence.errors import DuplicateUsernameError, UnknownUserError
from trading_bot.persistence.repositories.users import SqlUserRepository, normalize_username

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
LIFETIME = timedelta(hours=72)
HASH = "scrypt:" + "h" * 20  # an opaque value: the repository never parses it


class MovableClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Ana", "ana"), ("  bob.smith ", "bob.smith"), ("x_1-2", "x_1-2"), ("a" * 32, "a" * 32)],
)
def test_usernames_are_normalized(text: str, expected: str) -> None:
    assert normalize_username(text) == expected


@pytest.mark.parametrize("text", ["", "ab", "a" * 33, "-ana", ".ana", "ana maria", "añá", "a/b"])
def test_invalid_usernames_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="3 to 32 characters"):
        normalize_username(text)


def test_add_get_list_and_count(database: Database) -> None:
    with database.session() as session:
        repository = SqlUserRepository(session, clock=frozen_clock(NOW))
        assert repository.count() == 1  # migration 0005 creates the owner of existing data
        admin = repository.add("Emi", is_admin=True, password_hash=HASH)
        other = repository.add("ana")

        assert admin.username == "emi"
        assert admin.is_admin
        assert admin.active
        assert admin.can_log_in
        assert admin.created_at == NOW == admin.updated_at
        assert not other.is_admin
        assert other.password_hash is None
        assert not other.can_log_in
        assert repository.count() == 3
        assert repository.count_active_admins() == 2
        assert repository.get(admin.id) == admin
        assert repository.get(999) is None
        assert repository.get_by_username(" EMI ") == admin
        assert repository.get_by_username("nobody") is None
        assert [user.username for user in repository.list_all()] == ["admin", "ana", "emi"]
        assert HASH not in repr(admin)


def test_a_duplicate_username_is_refused_and_the_session_stays_usable(database: Database) -> None:
    with database.session() as session:
        repository = SqlUserRepository(session, clock=frozen_clock(NOW))
        repository.add("ana")
        with pytest.raises(DuplicateUsernameError):
            repository.add("ANA")
        assert repository.count() == 2


def test_an_access_link_sets_the_password_once(database: Database) -> None:
    clock = MovableClock(NOW)
    with database.session() as session:
        repository = SqlUserRepository(session, clock=clock)
        user = repository.add("ana", password_hash=HASH)

        invited = repository.issue_invite(user.id, "d" * 64, LIFETIME)
        assert invited.password_hash is None  # the old password stops working
        assert invited.invite_expires_at == NOW + LIFETIME
        assert repository.invited("d" * 64) == invited
        assert repository.invited("e" * 64) is None
        assert repository.invited("") is None

        clock.instant = NOW + timedelta(hours=1)
        redeemed = repository.redeem_invite("d" * 64, "new-hash")
        assert redeemed is not None
        assert redeemed.password_hash == "new-hash"
        assert redeemed.invite_expires_at is None
        assert redeemed.updated_at == NOW + timedelta(hours=1)
        assert repository.redeem_invite("d" * 64, "other") is None
        assert repository.get(user.id) == redeemed


def test_an_expired_or_replaced_link_does_not_work(database: Database) -> None:
    clock = MovableClock(NOW)
    with database.session() as session:
        repository = SqlUserRepository(session, clock=clock)
        user = repository.add("ana")
        repository.issue_invite(user.id, "a" * 64, LIFETIME)
        repository.issue_invite(user.id, "b" * 64, LIFETIME)
        assert repository.invited("a" * 64) is None  # replaced

        clock.instant = NOW + LIFETIME
        assert repository.invited("b" * 64) is None  # expired, exactly at the limit
        assert repository.redeem_invite("b" * 64, "x") is None


def test_a_disabled_user_cannot_use_a_link_or_log_in(database: Database) -> None:
    with database.session() as session:
        repository = SqlUserRepository(session, clock=frozen_clock(NOW))
        user = repository.add("ana", is_admin=True, password_hash=HASH)
        repository.issue_invite(user.id, "c" * 64, LIFETIME)

        disabled = repository.set_active(user.id, False)
        assert not disabled.active
        assert not disabled.can_log_in
        assert repository.count_active_admins() == 1  # only the migration's admin
        assert repository.invited("c" * 64) is None
        assert repository.set_active(user.id, False) == disabled
        assert repository.set_active(user.id, True).active


def test_set_password_cancels_a_pending_link(database: Database) -> None:
    with database.session() as session:
        repository = SqlUserRepository(session, clock=frozen_clock(NOW))
        user = repository.add("ana")
        repository.issue_invite(user.id, "f" * 64, LIFETIME)

        updated = repository.set_password(user.id, HASH)
        assert updated.can_log_in
        assert updated.invite_expires_at is None
        assert repository.invited("f" * 64) is None


def test_unknown_users_raise(database: Database) -> None:
    with database.session() as session:
        repository = SqlUserRepository(session, clock=frozen_clock(NOW))
        with pytest.raises(UnknownUserError):
            repository.set_password(5, HASH)
        with pytest.raises(UnknownUserError):
            repository.issue_invite(5, "a" * 64, LIFETIME)
        with pytest.raises(UnknownUserError):
            repository.set_active(5, False)


def test_users_persist_across_sessions(database: Database) -> None:
    with database.session() as session:
        SqlUserRepository(session, clock=frozen_clock(NOW)).add("ana", password_hash=HASH)
    with database.session() as session:
        stored = SqlUserRepository(session).get_by_username("ana")
        assert stored is not None
        assert stored.password_hash == HASH
        assert stored.created_at == NOW
