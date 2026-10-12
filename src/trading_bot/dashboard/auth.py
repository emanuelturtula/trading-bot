"""Dashboard authentication: password hash, signed sessions, CSRF tokens, login throttle.

Spec 020, D2-D4. Everything is built on the standard library (``hashlib.scrypt``, ``hmac``,
``secrets``), so the dashboard adds no cryptographic dependency.

- The password hash is ``scrypt:<n>:<r>:<p>:<salt>:<hash>`` with unpadded base64url fields: no
  ``$``, which Docker Compose would try to interpolate in ``secrets.env``.
- A session is the cookie ``<expiry>.<nonce>.<mac>``. The MAC key is derived from the session
  secret **and** the password hash, so rotating either one invalidates every session (AC5).
- The CSRF token is a MAC of the session nonce: bound to the session, stored nowhere.

No function here logs, and no error message echoes a password, a hash, a secret or a cookie.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MIN_SESSION_SECRET_LENGTH",
    "SESSION_LIFETIME_SECONDS",
    "InvalidPasswordHashError",
    "LoginThrottle",
    "SessionSigner",
    "hash_password",
    "parse_password_hash",
    "verify_password",
]

MIN_SESSION_SECRET_LENGTH: Final = 32
SESSION_LIFETIME_SECONDS: Final = 7 * 24 * 3600

_SCHEME: Final = "scrypt"
_DEFAULT_N: Final = 2**14
_DEFAULT_R: Final = 8
_DEFAULT_P: Final = 1
_SALT_BYTES: Final = 16
_HASH_BYTES: Final = 32
_MAX_N: Final = 2**20  # bounds the memory a configured hash can make verification use
_MAX_R: Final = 32
_MAX_P: Final = 16
_SCRYPT_MAXMEM: Final = 256 * 1024 * 1024
_NONCE_BYTES: Final = 18


class InvalidPasswordHashError(ValueError):
    """``TB_DASHBOARD_PASSWORD_HASH`` is not a hash this module produced. Never echoes it."""


@dataclass(frozen=True, slots=True)
class PasswordHash:
    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """The storable hash of ``password``, with a fresh random salt unless one is given."""
    if not password:
        raise ValueError("the password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES) if salt is None else salt
    digest = _scrypt(password, salt, _DEFAULT_N, _DEFAULT_R, _DEFAULT_P, _HASH_BYTES)
    return ":".join(
        (_SCHEME, str(_DEFAULT_N), str(_DEFAULT_R), str(_DEFAULT_P), _b64(salt), _b64(digest))
    )


def parse_password_hash(text: str) -> PasswordHash:
    parts = text.strip().split(":")
    if len(parts) != 6 or parts[0] != _SCHEME:
        raise InvalidPasswordHashError("the dashboard password hash has an unknown format")
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        raise InvalidPasswordHashError("the dashboard password hash is malformed") from None
    power_of_two = n > 1 and n & (n - 1) == 0
    if not (power_of_two and n <= _MAX_N and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        raise InvalidPasswordHashError("the dashboard password hash has unsupported parameters")
    if len(salt) < 8 or len(digest) < 16:
        raise InvalidPasswordHashError("the dashboard password hash is too short")
    return PasswordHash(n=n, r=r, p=p, salt=salt, digest=digest)


def verify_password(password: str, stored: PasswordHash) -> bool:
    """``True`` when ``password`` matches; the comparison takes constant time."""
    candidate = _scrypt(password, stored.salt, stored.n, stored.r, stored.p, len(stored.digest))
    return hmac.compare_digest(candidate, stored.digest)


class SessionSigner:
    """Issues and checks session cookies and CSRF tokens (D3, D4)."""

    def __init__(
        self,
        session_secret: str,
        password_hash: str,
        *,
        lifetime_seconds: int = SESSION_LIFETIME_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise ValueError(
                f"the session secret must have at least {MIN_SESSION_SECRET_LENGTH} characters"
            )
        self._key = hmac.new(
            session_secret.encode(), b"session-key." + password_hash.encode(), hashlib.sha256
        ).digest()
        self._lifetime = lifetime_seconds
        self._clock = clock

    @property
    def lifetime_seconds(self) -> int:
        return self._lifetime

    def issue(self) -> str:
        expiry = int(self._clock()) + self._lifetime
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        payload = f"{expiry}.{nonce}"
        return f"{payload}.{self._mac(payload)}"

    def verify(self, token: str | None) -> str | None:
        """The session nonce when ``token`` is authentic and not expired, ``None`` otherwise."""
        if not token or len(token) > 256:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        expiry_text, nonce, mac = parts
        if not hmac.compare_digest(mac, self._mac(f"{expiry_text}.{nonce}")):
            return None
        try:
            expiry = int(expiry_text)
        except ValueError:
            return None
        if expiry <= self._clock():
            return None
        return nonce

    def csrf_token(self, nonce: str) -> str:
        return self._mac(f"csrf.{nonce}")

    def check_csrf(self, nonce: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_token(nonce), token or "")

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())


class LoginThrottle:
    """At most ``limit`` failed logins per client within ``window_seconds`` (AC4).

    In memory: the bot is one process (CLAUDE.md rule 7), and a restart forgetting the failures
    is acceptable. The number of tracked clients is bounded so a flood cannot grow it forever.
    """

    def __init__(
        self,
        *,
        limit: int = 5,
        window_seconds: float = 15 * 60,
        max_clients: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def blocked(self, client: str) -> bool:
        failures = self._failures.get(client)
        return failures is not None and len(self._prune(failures)) >= self._limit

    def record_failure(self, client: str) -> None:
        failures = self._failures.get(client)
        if failures is None:
            if len(self._failures) >= self._max_clients:
                self._failures.pop(next(iter(self._failures)))  # the oldest client tracked
            failures = self._failures[client] = deque()
        self._prune(failures).append(self._clock())

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)

    def _prune(self, failures: deque[float]) -> deque[float]:
        horizon = self._clock() - self._window
        while failures and failures[0] <= horizon:
            failures.popleft()
        return failures


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=length
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    if not text:
        raise ValueError("empty field")
    padded = text + "=" * (-len(text) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)
