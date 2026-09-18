"""The ORM models of the application (spec 012, Design 5).

This feature ships no model: tickers, rules and assignments arrive with #12, and signals and
``bot_state`` with #13. **Every model must be defined here or imported by this module**,
because ``migrations/env.py`` reads ``Base.metadata`` through it and ``--autogenerate`` only
compares what is imported. Timestamp columns always use ``UtcDateTime`` (``types.py``).
"""

from __future__ import annotations

from trading_bot.persistence.base import Base

__all__ = ["Base"]
