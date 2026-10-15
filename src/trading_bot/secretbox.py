"""Encryption of the Telegram bot tokens stored in the database (spec 025, D2).

``SecretBox`` encrypts with Fernet (AES-128-CBC plus HMAC-SHA256, from ``cryptography``) under
``TB_MASTER_KEY``, the only new secret, which lives in ``secrets.env`` like the others. A token
is a ``SecretStr`` on both sides, so it is never logged or shown by accident.

``fingerprint`` is a keyed HMAC-SHA256 of the token: stored next to the ciphertext, it lets the
database refuse the same token on two accounts (rule 7) without storing the token in clear and
without making it guessable from the digest.

``python -m trading_bot.secretbox`` prints a new random master key.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from typing import Final, TextIO

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

__all__ = ["InvalidMasterKeyError", "SecretBox", "UnreadableSecretError", "new_master_key"]

_FINGERPRINT_LABEL: Final = b"telegram-token-fingerprint.v1"


class InvalidMasterKeyError(ValueError):
    """``TB_MASTER_KEY`` is not a Fernet key. The message never contains the value."""

    def __init__(self) -> None:
        super().__init__(
            "TB_MASTER_KEY is not a valid key; generate one with python -m trading_bot.secretbox"
        )


class UnreadableSecretError(Exception):
    """A stored ciphertext does not decrypt under the current master key."""

    def __init__(self) -> None:
        super().__init__("a stored secret cannot be decrypted with the current TB_MASTER_KEY")


def new_master_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    """Encrypts, decrypts and fingerprints secrets under one master key."""

    def __init__(self, master_key: SecretStr) -> None:
        if not isinstance(master_key, SecretStr):
            raise TypeError(f"master_key must be a SecretStr, got {type(master_key).__name__}")
        raw = master_key.get_secret_value().strip().encode("ascii", errors="replace")
        try:
            self._fernet = Fernet(raw)
        except (ValueError, TypeError):
            raise InvalidMasterKeyError() from None
        self._fingerprint_key = hmac.new(raw, _FINGERPRINT_LABEL, hashlib.sha256).digest()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def encrypt(self, secret: SecretStr) -> str:
        return self._fernet.encrypt(secret.get_secret_value().encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> SecretStr:
        try:
            plain = self._fernet.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, ValueError):
            raise UnreadableSecretError() from None
        return SecretStr(plain.decode("utf-8"))

    def fingerprint(self, secret: SecretStr) -> str:
        message = secret.get_secret_value().encode("utf-8")
        return hmac.new(self._fingerprint_key, message, hashlib.sha256).hexdigest()


def main(stdout: TextIO | None = None) -> int:
    """Write a new master key and a newline; nothing else."""
    out = sys.stdout if stdout is None else stdout
    out.write(new_master_key() + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
