"""Shared pytest configuration.

The ``trading-bot`` Hypothesis profile inherits the active default, so Hypothesis' built-in CI
profile (derandomized, no example database) still applies on CI.
"""

from hypothesis import settings

settings.register_profile("trading-bot", parent=settings.default, max_examples=50, deadline=None)
settings.load_profile("trading-bot")
