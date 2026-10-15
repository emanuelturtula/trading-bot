"""``SecretBox``: the encryption of stored bot tokens (spec 025, D2).

Fake tokens and keys are built at runtime (CLAUDE.md rule 2).
"""

from __future__ import annotations

import io

import pytest
from pydantic import SecretStr

from trading_bot.secretbox import (
    InvalidMasterKeyError,
    SecretBox,
    UnreadableSecretError,
    main,
    new_master_key,
)

FAKE_TOKEN = "123456789" + ":" + "x" * 35
OTHER_TOKEN = "987654321" + ":" + "y" * 35


def box(key: str | None = None) -> SecretBox:
    return SecretBox(SecretStr(key or new_master_key()))


def test_a_token_round_trips_and_the_ciphertext_hides_it() -> None:
    secrets = box()

    ciphertext = secrets.encrypt(SecretStr(FAKE_TOKEN))

    assert FAKE_TOKEN not in ciphertext
    assert "x" * 10 not in ciphertext
    assert secrets.decrypt(ciphertext).get_secret_value() == FAKE_TOKEN
    # Fernet adds a random IV: the same token never encrypts to the same text twice.
    assert secrets.encrypt(SecretStr(FAKE_TOKEN)) != ciphertext


def test_another_key_cannot_read_the_ciphertext() -> None:
    ciphertext = box().encrypt(SecretStr(FAKE_TOKEN))

    with pytest.raises(UnreadableSecretError) as raised:
        box().decrypt(ciphertext)

    assert FAKE_TOKEN not in str(raised.value)


@pytest.mark.parametrize("ciphertext", ["", "garbage", "ñ"])
def test_a_damaged_ciphertext_is_unreadable(ciphertext: str) -> None:
    with pytest.raises(UnreadableSecretError):
        box().decrypt(ciphertext)


def test_the_fingerprint_is_stable_per_key_and_hides_the_token() -> None:
    key = new_master_key()
    first, second = box(key), box(key)

    fingerprint = first.fingerprint(SecretStr(FAKE_TOKEN))

    assert fingerprint == second.fingerprint(SecretStr(FAKE_TOKEN))
    assert fingerprint != first.fingerprint(SecretStr(OTHER_TOKEN))
    assert fingerprint != box().fingerprint(SecretStr(FAKE_TOKEN))  # keyed: not guessable
    assert len(fingerprint) == 64
    assert FAKE_TOKEN not in fingerprint


def test_surrounding_spaces_in_the_key_are_ignored() -> None:
    key = new_master_key()
    ciphertext = box(key).encrypt(SecretStr(FAKE_TOKEN))

    assert box(f"  {key}\n").decrypt(ciphertext).get_secret_value() == FAKE_TOKEN


@pytest.mark.parametrize("key", ["", "short", "ñ" * 44, "a" * 44])
def test_an_invalid_key_is_refused_without_echoing_it(key: str) -> None:
    with pytest.raises(InvalidMasterKeyError) as raised:
        SecretBox(SecretStr(key))

    assert "python -m trading_bot.secretbox" in str(raised.value)
    if key:
        assert key not in str(raised.value)


def test_the_key_must_be_a_secret_str() -> None:
    with pytest.raises(TypeError, match="SecretStr"):
        SecretBox(new_master_key())  # type: ignore[arg-type]


def test_the_repr_shows_no_key() -> None:
    key = new_master_key()

    assert key not in repr(box(key))


def test_the_command_prints_one_new_valid_key() -> None:
    out = io.StringIO()

    assert main(out) == 0

    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    SecretBox(SecretStr(lines[0]))  # a valid key
    other = io.StringIO()
    main(other)
    assert other.getvalue() != out.getvalue()
