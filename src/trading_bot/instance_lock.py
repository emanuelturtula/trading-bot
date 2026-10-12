"""One process per data directory (spec 017, D4; issue #16; CLAUDE.md rule 7).

``InstanceLock`` holds an exclusive ``flock`` on ``<data dir>/trading-bot.lock`` for the life of
the process. The kernel releases it when the descriptor closes, including when the process
dies, so there is never a stale lock to clean up. The file holds the owner's PID for an
operator and nothing else. Linux and macOS only, which covers the container and the Pi.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Final

__all__ = ["LOCK_FILENAME", "InstanceLock", "InstanceLockError"]

LOCK_FILENAME: Final = "trading-bot.lock"


class InstanceLockError(RuntimeError):
    """Another process already holds the lock of this data directory."""


class InstanceLock:
    """An exclusive, non-blocking lock on a file inside ``data_dir``."""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / LOCK_FILENAME
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock or raise ``InstanceLockError`` at once; never waits."""
        if self._fd is not None:
            raise RuntimeError("the instance lock is already held by this object")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise InstanceLockError(
                f"another trading-bot process holds {self._path}; refusing to start"
            ) from None
        except BaseException:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        """Release the lock. Idempotent; the file is left in place on purpose."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
