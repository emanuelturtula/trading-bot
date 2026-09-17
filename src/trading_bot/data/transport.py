"""Blocking provider calls from the event loop: pacing, timeouts and retries (spec 011, Design 9).

``ProviderTransport.call`` runs a blocking operation (a yfinance request, for example) in a
worker thread, one at a time, after taking a token from a ``TokenBucket``, and waits at most
``RetryPolicy.attempt_timeout`` for it. Only ``ProviderUnavailableError`` is retried, with
exponential backoff and equal jitter, a floor after rate limiting and ``retry_after`` honored
(decisions D54 and D55). Every other exception propagates at once.

A thread cannot be cancelled: a timed-out attempt abandons its worker, which keeps the single
in-flight slot until the operation returns or raises, so a library's process-wide session is
never used by two threads at once. Late outcomes are discarded without logging, and ``aclose``
waits for running workers. Clock, sleep and jitter are injected; the default clock is the running
event loop's monotonic ``time``, and no candle decision reads it. This module knows nothing about
Yahoo.

``TokenBucket`` and ``ProviderTransport`` own an ``asyncio.Lock`` and an ``asyncio.Semaphore``,
which bind to the running loop the first time a waiter blocks on them, so each of them belongs to
one event loop (decision D68): build them inside the loop that will use them, and never share
them with another loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from trading_bot.data.errors import ProviderFailure, ProviderUnavailableError
from trading_bot.domain.timeframe import Timeframe

__all__ = ["ProviderTransport", "RateLimit", "RetryPolicy", "TokenBucket"]

_LOGGER_NAME: Final = "trading_bot.data.transport"
_RETRY_MESSAGE: Final = (
    "retrying provider call for %s %s after %s (attempt %d of %d, waiting %.3f s)"
)
_MAX_ATTEMPTS: Final = 10

type _Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """Attempts, backoff and per-attempt timeout of ``ProviderTransport.call``.

    ``max_attempts`` is an ``int`` in ``[1, 10]``; every delay and the timeout are finite
    ``int`` or ``float`` seconds (never ``bool``), with ``base_delay > 0``,
    ``max_delay >= base_delay``, ``0 <= rate_limited_delay <= max_delay`` and
    ``attempt_timeout > 0``. Wrong types raise ``TypeError`` and bad values ``ValueError``.
    """

    max_attempts: int = 3
    base_delay: float = 2.0  # seconds
    max_delay: float = 30.0
    rate_limited_delay: float = 15.0
    attempt_timeout: float = 20.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError(f"max_attempts must be an int, got {type(self.max_attempts).__name__}")
        if not 1 <= self.max_attempts <= _MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be in [1, {_MAX_ATTEMPTS}]")
        _check_seconds(self.base_delay, "base_delay")
        _check_seconds(self.max_delay, "max_delay")
        _check_seconds(self.rate_limited_delay, "rate_limited_delay")
        _check_seconds(self.attempt_timeout, "attempt_timeout")
        if not self.base_delay > 0:
            raise ValueError("base_delay must be greater than zero")
        if not self.max_delay >= self.base_delay:
            raise ValueError("max_delay must be at least base_delay")
        if not 0 <= self.rate_limited_delay <= self.max_delay:
            raise ValueError("rate_limited_delay must be in [0, max_delay]")
        if not self.attempt_timeout > 0:
            raise ValueError("attempt_timeout must be greater than zero")

    def delay(self, attempt: int, error: ProviderUnavailableError, jitter: float) -> float | None:
        """The wait after failed ``attempt`` (1-based), or ``None`` to stop retrying.

        ``cap = min(max_delay, base_delay * 2 ** (attempt - 1))`` and the equal-jitter wait is
        ``cap / 2 + jitter * cap / 2``, raised to ``rate_limited_delay`` after a rate limit and to
        ``error.retry_after``; a wait above ``max_delay`` gives ``None``. ``attempt`` is an ``int``
        ``>= 1`` and ``jitter`` a number in ``[0.0, 1.0)``.
        """
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise TypeError(f"attempt must be an int, got {type(attempt).__name__}")
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        if not isinstance(error, ProviderUnavailableError):
            raise TypeError(f"error must be a ProviderUnavailableError, got {type(error).__name__}")
        if isinstance(jitter, bool) or not isinstance(jitter, int | float):
            raise TypeError(f"jitter must be a float, got {type(jitter).__name__}")
        if not 0.0 <= jitter < 1.0:
            raise ValueError("jitter must be in [0.0, 1.0)")
        try:
            grown = math.ldexp(self.base_delay, attempt - 1)
        except OverflowError:
            grown = math.inf
        cap = min(self.max_delay, grown)
        wait = cap / 2 + jitter * cap / 2
        if error.failure is ProviderFailure.RATE_LIMITED:
            wait = max(wait, self.rate_limited_delay)
        if error.retry_after is not None:
            wait = max(wait, error.retry_after.total_seconds())
        return None if wait > self.max_delay else wait


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    """Token bucket settings: ``rate`` tokens per second (finite, ``> 0``), ``burst >= 1``."""

    rate: float = 1.0  # tokens per second
    burst: int = 5

    def __post_init__(self) -> None:
        _check_seconds(self.rate, "rate")
        if not self.rate > 0:
            raise ValueError("rate must be greater than zero")
        if isinstance(self.burst, bool) or not isinstance(self.burst, int):
            raise TypeError(f"burst must be an int, got {type(self.burst).__name__}")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")


class TokenBucket:
    """Paces attempts: starts full with ``burst`` tokens and refills at ``rate`` per second.

    ``acquire`` calls are serialized, so waiting callers are served in arrival order. A clock
    that goes backwards adds nothing. The default clock is the running loop's ``time``, and the
    bucket belongs to the event loop that first waits on it (D68).
    """

    def __init__(
        self,
        limit: RateLimit,
        *,
        clock: Callable[[], float] | None = None,  # default: the running loop's time()
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        if not isinstance(limit, RateLimit):
            raise TypeError(f"limit must be a RateLimit, got {type(limit).__name__}")
        if clock is not None and not callable(clock):
            raise TypeError(f"clock must be callable or None, got {type(clock).__name__}")
        if not callable(sleep):
            raise TypeError(f"sleep must be callable, got {type(sleep).__name__}")
        self._limit = limit
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(limit.burst)
        self._previous: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Take one token, sleeping until one is available."""
        async with self._lock:
            while True:
                now = asyncio.get_running_loop().time() if self._clock is None else self._clock()
                if self._previous is not None:
                    refill = max(0.0, now - self._previous) * self._limit.rate
                    self._tokens = min(float(self._limit.burst), self._tokens + refill)
                self._previous = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._limit.rate)


class ProviderTransport:
    """Runs blocking provider operations with pacing, one at a time, with timeouts and retries.

    A transport belongs to the event loop that first waits on its in-flight slot or its limiter
    (D68): build one inside the running loop and never share it with another loop.
    """

    def __init__(
        self,
        *,
        policy: RetryPolicy,
        limiter: TokenBucket,
        sleep: _Sleep = asyncio.sleep,
        jitter: Callable[[], float] | None = None,  # default: random() of a private random.Random()
        # default: logging.getLogger("trading_bot.data.transport")
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(policy, RetryPolicy):
            raise TypeError(f"policy must be a RetryPolicy, got {type(policy).__name__}")
        if not isinstance(limiter, TokenBucket):
            raise TypeError(f"limiter must be a TokenBucket, got {type(limiter).__name__}")
        if not callable(sleep):
            raise TypeError(f"sleep must be callable, got {type(sleep).__name__}")
        if jitter is not None and not callable(jitter):
            raise TypeError(f"jitter must be callable or None, got {type(jitter).__name__}")
        if logger is not None and not isinstance(logger, logging.Logger):
            raise TypeError(f"logger must be a logging.Logger, got {type(logger).__name__}")
        self._policy = policy
        self._limiter = limiter
        self._sleep = sleep
        # Jitter only spreads retries; it is not a security decision.
        self._jitter = random.Random().random if jitter is None else jitter  # noqa: S311
        self._logger = logging.getLogger(_LOGGER_NAME) if logger is None else logger
        self._slot = asyncio.Semaphore(1)
        self._workers: set[asyncio.Task[object]] = set()
        self._closed = False

    async def call[T](
        self, operation: Callable[[], T], *, ticker: str, timeframe: Timeframe | None
    ) -> T:
        """The result of ``operation()``, run in a worker thread, retrying on unavailability.

        Raises ``RuntimeError`` after ``aclose()``. Each attempt takes a token, waits for the
        in-flight slot and waits at most ``attempt_timeout`` (``ProviderUnavailableError`` with
        failure ``timeout``, raised ``from None``). A ``ProviderUnavailableError`` is retried
        while attempts remain and ``policy.delay`` is not ``None``, after one ``INFO`` record and
        a sleep; otherwise that same error instance is raised. Any other exception propagates at
        once. ``ticker`` and ``timeframe`` only label errors and records.
        """
        if self._closed:
            raise RuntimeError("provider transport is closed")
        if not callable(operation):
            raise TypeError(f"operation must be callable, got {type(operation).__name__}")
        if not isinstance(ticker, str):
            raise TypeError(f"ticker must be a str, got {type(ticker).__name__}")
        if timeframe is not None and not isinstance(timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe or None, got {type(timeframe).__name__}"
            )
        attempt = 1
        while True:
            await self._limiter.acquire()
            try:
                return await self._attempt(operation, ticker, timeframe)
            except ProviderUnavailableError as error:
                delay = None
                if attempt < self._policy.max_attempts:
                    delay = self._policy.delay(attempt, error, self._jitter())
                if delay is None:
                    raise
                self._logger.info(
                    _RETRY_MESSAGE,
                    ticker,
                    "-" if timeframe is None else timeframe.value,
                    error.failure.value,
                    attempt,
                    self._policy.max_attempts,
                    delay,
                )
            await self._sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        """Refuse new calls and wait for every running worker; outcomes are discarded."""
        self._closed = True
        workers = tuple(self._workers)
        if workers:
            await asyncio.wait(workers)

    async def _attempt[T](
        self, operation: Callable[[], T], ticker: str, timeframe: Timeframe | None
    ) -> T:
        await self._slot.acquire()
        worker = asyncio.ensure_future(asyncio.to_thread(operation))
        self._workers.add(worker)
        # Registered before shield() adds its own callback, so the slot is free again by the time
        # the caller resumes with the result.
        worker.add_done_callback(self._worker_done)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), self._policy.attempt_timeout)
        except TimeoutError:
            raise ProviderUnavailableError(
                ProviderFailure.TIMEOUT, ticker=ticker, timeframe=timeframe
            ) from None

    def _worker_done(self, worker: asyncio.Task[object]) -> None:
        self._workers.discard(worker)
        self._slot.release()
        if not worker.cancelled():
            worker.exception()  # retrieved, so a late failure is never logged by asyncio


def _check_seconds(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be an int or float, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
