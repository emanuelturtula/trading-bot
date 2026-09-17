"""Wiring of the Yahoo provider (spec 011, Design 11; decision D63).

``build_yfinance_provider`` assembles the yfinance client, the token bucket, the transport and
the provider. It performs no I/O other than creating the cache directory, and nothing in the
running app calls it yet: the lifespan wiring belongs to #16, which also builds the calendar,
awaits ``provider.aclose()`` at shutdown and removes the cache directory.

Call it inside the running event loop and use the provider only from that loop (decision D68):
the transport's synchronization primitives bind to the first loop that waits on them.
"""

from __future__ import annotations

from pathlib import Path

from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.data.yahoo.client import YFinanceClient
from trading_bot.data.yahoo.provider import YFinanceProvider
from trading_bot.domain.market_calendar.sessions import MarketCalendar

__all__ = ["build_yfinance_provider"]


def build_yfinance_provider(
    *,
    calendar: MarketCalendar,
    cache_dir: Path,
    policy: RetryPolicy | None = None,  # default RetryPolicy()
    rate_limit: RateLimit | None = None,  # default RateLimit()
) -> YFinanceProvider:
    """A ``YFinanceProvider`` with the default clock, sleep and jitter.

    Arguments of the wrong type raise ``TypeError`` before the cache directory is created;
    ``configure_yfinance`` validates ``cache_dir``. A per-process temporary directory is
    recommended for ``cache_dir``, never the data volume (decision D50).
    """
    if not isinstance(calendar, MarketCalendar):
        raise TypeError(f"calendar must be a MarketCalendar, got {type(calendar).__name__}")
    if policy is not None and not isinstance(policy, RetryPolicy):
        raise TypeError(f"policy must be a RetryPolicy or None, got {type(policy).__name__}")
    if rate_limit is not None and not isinstance(rate_limit, RateLimit):
        raise TypeError(f"rate_limit must be a RateLimit or None, got {type(rate_limit).__name__}")
    client = YFinanceClient(cache_dir=cache_dir)
    limiter = TokenBucket(RateLimit() if rate_limit is None else rate_limit)
    transport = ProviderTransport(
        policy=RetryPolicy() if policy is None else policy, limiter=limiter
    )
    return YFinanceProvider(client=client, transport=transport, calendar=calendar)
