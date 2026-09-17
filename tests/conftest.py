"""Shared pytest configuration.

The ``trading-bot`` Hypothesis profile inherits the active default, so Hypothesis' built-in CI
profile (derandomized, no example database) still applies on CI.

Every test runs under the network guard (spec 011, Design 13.6): outbound connections, remote
name resolution and ``curl_cffi`` requests raise ``NetworkAccessError``, while loopback
connections, ``socket.socketpair()`` and ``asyncio.run`` keep working. Never disable it.
"""

import pytest
from hypothesis import settings

from tests.fixtures.network_guard import install_network_guard

settings.register_profile("trading-bot", parent=settings.default, max_examples=50, deadline=None)
settings.load_profile("trading-bot")


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block network access for the whole test (decision D60)."""
    install_network_guard(monkeypatch)
