"""``python -m trading_bot.dashboard.password``: print a hash for ``TB_DASHBOARD_PASSWORD_HASH``.

The password is read without echo from the terminal, or from the first line of standard input
when it is not a terminal (``... < file``). Only the hash is printed (spec 020, AC7).
"""

from __future__ import annotations

import getpass
import sys
from typing import TextIO

from trading_bot.dashboard.auth import hash_password

__all__ = ["main"]

MIN_PASSWORD_LENGTH = 12


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    if source.isatty():
        password = getpass.getpass("Dashboard password: ")
        if getpass.getpass("Repeat it: ") != password:
            out.write("The passwords do not match.\n")
            return 1
    else:
        password = source.readline().rstrip("\r\n")
    if len(password) < MIN_PASSWORD_LENGTH:
        out.write(f"Use at least {MIN_PASSWORD_LENGTH} characters.\n")
        return 1
    out.write(hash_password(password) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
