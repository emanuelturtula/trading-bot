"""Dashboard authentication primitives (spec 020, AC3-AC5, AC7, D2-D4)."""

from __future__ import annotations

import io

import pytest

from trading_bot.dashboard.auth import (
    InvalidPasswordHashError,
    LoginThrottle,
    SessionSigner,
    hash_password,
    link_digest,
    new_link_token,
    parse_password_hash,
    verify_password,
)
from trading_bot.dashboard.password import main as password_main

# Built at runtime (CLAUDE.md rule 2): no secret-shaped literal in the repository.
PASSWORD = "-".join(["correct", "horse", "battery", "staple"])
SECRET = "k" * 40
HASH = hash_password(PASSWORD)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_hash_round_trip() -> None:
    stored = parse_password_hash(HASH)

    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)
    assert not verify_password("", stored)


def test_hash_format_is_safe_for_compose_env_files() -> None:
    assert HASH.startswith("scrypt:16384:8:1:")
    assert "$" not in HASH
    assert "=" not in HASH
    assert PASSWORD not in HASH


def test_every_hash_has_its_own_salt() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_an_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(ValueError):
        hash_password("")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        "bcrypt:16384:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",
        "scrypt:16384:8:1:c2FsdHNhbHQ",
        "scrypt:abc:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",
        "scrypt:1000:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",  # not a power of two
        "scrypt:4194304:8:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",  # too much memory
        "scrypt:16384:0:1:c2FsdHNhbHQ:ZGlnZXN0ZGlnZXN0ZGlnZXN0",
        "scrypt:16384:8:1::ZGlnZXN0ZGlnZXN0ZGlnZXN0",
        "scrypt:16384:8:1:c2FsdA:ZGlnZXN0ZGlnZXN0ZGlnZXN0",  # salt too short
        "scrypt:16384:8:1:c2FsdHNhbHQ:!!!!",
    ],
)
def test_malformed_hashes_are_rejected_without_echo(text: str) -> None:
    with pytest.raises(InvalidPasswordHashError) as caught:
        parse_password_hash(text)

    if text:
        assert text not in str(caught.value)


def test_session_round_trip_and_csrf() -> None:
    signer = SessionSigner(SECRET)
    token = signer.issue(7, HASH)

    assert SessionSigner.user_id(token) == 7
    nonce = signer.verify(token, HASH)

    assert nonce is not None
    csrf = signer.csrf_token(nonce)
    assert signer.check_csrf(nonce, csrf)
    assert not signer.check_csrf(nonce, csrf[:-1] + ("A" if csrf[-1] != "A" else "B"))
    assert not signer.check_csrf(nonce, "")
    assert not signer.check_csrf(nonce, None)
    assert not signer.check_csrf("other", csrf)


def test_session_expires() -> None:
    clock = Clock()
    signer = SessionSigner(SECRET, lifetime_seconds=60, clock=clock)
    token = signer.issue(1, HASH)

    clock.now += 59
    assert signer.verify(token, HASH) is not None
    clock.now += 1
    assert signer.verify(token, HASH) is None


def test_tampered_or_foreign_sessions_are_rejected() -> None:
    signer = SessionSigner(SECRET)
    token = signer.issue(1, HASH)
    expiry, user, nonce, mac = token.split(".")

    assert signer.verify(f"{int(expiry) + 3600}.{user}.{nonce}.{mac}", HASH) is None
    assert signer.verify(f"{expiry}.2.{nonce}.{mac}", HASH) is None  # another user's id
    assert signer.verify(f"{expiry}.{user}.other.{mac}", HASH) is None
    assert signer.verify(f"{expiry}.{user}.{nonce}.{mac[:-2]}", HASH) is None
    for garbage in (
        None,
        "",
        "a.b.c",
        "a.b.c.d.e",
        "x" * 300,
        f"never.{user}.{nonce}.{mac}",
        f"{expiry}.-1.{nonce}.{mac}",
        f"{expiry}.{'9' * 19}.{nonce}.{mac}",
    ):
        assert signer.verify(garbage, HASH) is None
    for malformed in (None, "", "a.b.c", "1.x.n.m", "1.-1.n.m", "1." + "9" * 19 + ".n.m"):
        assert SessionSigner.user_id(malformed) is None


def test_rotating_the_secret_or_changing_the_password_invalidates_sessions() -> None:
    token = SessionSigner(SECRET).issue(1, HASH)

    assert SessionSigner("j" * 40).verify(token, HASH) is None
    assert SessionSigner(SECRET).verify(token, hash_password(PASSWORD)) is None


def test_a_short_session_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        SessionSigner("k" * 31)


def test_access_link_tokens_are_random_and_only_their_digest_is_kept() -> None:
    token, digest = new_link_token()
    other, other_digest = new_link_token()

    assert token != other
    assert digest != other_digest
    assert len(token) >= 40
    assert digest == link_digest(token)
    assert len(digest) == 64
    assert token not in digest


def test_throttle_blocks_after_five_failures_within_the_window() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.record_failure("a")
    assert not throttle.blocked("a")

    throttle.record_failure("a")

    assert throttle.blocked("a")
    assert not throttle.blocked("b")
    clock.now += 15 * 60
    assert not throttle.blocked("a")


def test_throttle_reset_and_bounded_memory() -> None:
    throttle = LoginThrottle(limit=1, max_clients=2, clock=Clock())
    throttle.record_failure("a")
    throttle.reset("a")
    assert not throttle.blocked("a")

    for client in ("a", "b", "c"):
        throttle.record_failure(client)

    assert not throttle.blocked("a")  # the oldest client was forgotten
    assert throttle.blocked("b")
    assert throttle.blocked("c")


def test_password_helper_prints_a_verifiable_hash() -> None:
    out = io.StringIO()

    assert password_main(io.StringIO(PASSWORD + "\n"), out) == 0

    printed = out.getvalue().strip()
    assert PASSWORD not in printed
    assert verify_password(PASSWORD, parse_password_hash(printed))


def test_password_helper_refuses_short_passwords() -> None:
    out = io.StringIO()

    assert password_main(io.StringIO("short\n"), out) == 1
    assert "at least 12" in out.getvalue()
