"""One process per data directory (spec 017, AC4, D4).

``flock`` locks belong to an open file description, so two ``InstanceLock`` objects in one
process contend exactly like two processes; one test also uses a real second process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from trading_bot.instance_lock import LOCK_FILENAME, InstanceLock, InstanceLockError


def test_a_second_lock_on_the_same_directory_is_refused(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(InstanceLockError, match=LOCK_FILENAME):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()


def test_the_lock_is_free_again_after_release(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path)
    first.acquire()
    first.release()
    first.release()  # idempotent

    second = InstanceLock(tmp_path)
    second.acquire()
    assert second.held
    second.release()
    assert not second.held


def test_the_lock_file_holds_the_pid_only(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "missing" / "dir")
    lock.acquire()
    try:
        assert lock.path.read_text() == f"{os.getpid()}\n"
    finally:
        lock.release()


def test_one_object_cannot_acquire_twice(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already held"):
            lock.acquire()
    finally:
        lock.release()


def test_another_process_is_refused_while_the_lock_is_held(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from trading_bot.instance_lock import InstanceLock, InstanceLockError\n"
            "try:\n"
            "    InstanceLock(Path(sys.argv[1])).acquire()\n"
            "except InstanceLockError:\n"
            "    sys.exit(3)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)], check=False, timeout=60
        )
    finally:
        lock.release()

    assert result.returncode == 3
