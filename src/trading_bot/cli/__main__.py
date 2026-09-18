"""``python -m trading_bot.cli``: the entry point the container invokes."""

from __future__ import annotations

import sys

from trading_bot.cli.main import main

raise SystemExit(main(sys.argv[1:]))
