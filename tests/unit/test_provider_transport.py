"""Tests of the provider transport (spec 011, T7-T8, AC16-AC19; decisions D54 and D55).

T7: ``RetryPolicy`` and ``RateLimit`` validation (Design 9.1), the ``RetryPolicy.delay`` table
(Design 9.2) and the ``TokenBucket`` sequences (Design 9.5) with a fake clock. T8: ``call``
(Design 9.3) with a fake sleep and a fixed jitter, and one attempt (Design 9.4): the timeout of an
operation blocked on a ``threading.Event``, the in-flight slot held by an abandoned worker, late
outcomes discarded without logging, cancellation and ``aclose``. No test sleeps for real or
asserts elapsed time: the only real waits are thread hand-offs gated by events.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import math
import threading
from collections.abc import Callable
from datetime import timedelta

import pytest

import trading_bot.data.transport as transport_module
from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    NoDataError,
    ProviderFailure,
    ProviderUnavailableError,
)
from trading_bot.data.transport import ProviderTransport, RateLimit, RetryPolicy, TokenBucket
from trading_bot.domain.timeframe import Timeframe

TRANSPORT_LOGGER = "trading_bot.data.transport"


def unavailable(
    failure: ProviderFailure = ProviderFailure.CONNECTION, retry_after: float | None = None
) -> ProviderUnavailableError:
    return ProviderUnavailableError(
        failure,
        ticker="SPY",
        retry_after=None if retry_after is None else timedelta(seconds=retry_after),
    )


class FakeClock:
    """A monotonic clock under test control; ``sleep`` records delays and advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def transport(
    *,
    policy: RetryPolicy | None = None,
    sleep: RecordingSleep | None = None,
    jitter: float = 0.5,
    logger: logging.Logger | None = None,
) -> ProviderTransport:
    clock = FakeClock()
    return ProviderTransport(
        policy=policy or RetryPolicy(),
        limiter=TokenBucket(RateLimit(rate=1.0, burst=1000), clock=clock, sleep=clock.sleep),
        sleep=sleep or RecordingSleep(),
        jitter=lambda: jitter,
        logger=logger,
    )


# --- T7: RetryPolicy and RateLimit validation (Design 9.1) -------------------------------------


def test_the_module_exports_exactly_the_spec_names() -> None:
    assert transport_module.__all__ == [
        "ProviderTransport",
        "RateLimit",
        "RetryPolicy",
        "TokenBucket",
    ]


def test_the_policy_defaults_are_the_spec_values() -> None:
    policy = RetryPolicy()

    assert (
        policy.max_attempts,
        policy.base_delay,
        policy.max_delay,
        policy.rate_limited_delay,
        policy.attempt_timeout,
    ) == (3, 2.0, 30.0, 15.0, 20.0)
    assert (RateLimit().rate, RateLimit().burst) == (1.0, 5)


@pytest.mark.parametrize(
    "fields",
    [
        {"max_attempts": 3.0},
        {"max_attempts": True},
        {"base_delay": "2"},
        {"base_delay": False},
        {"max_delay": None},
        {"rate_limited_delay": "15"},
        {"attempt_timeout": True},
    ],
)
def test_policy_fields_of_the_wrong_type_raise_type_error(fields: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        RetryPolicy(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"base_delay": 0.0},
        {"base_delay": -1.0},
        {"base_delay": math.nan},
        {"max_delay": math.inf},
        {"base_delay": 5.0, "max_delay": 4.0},
        {"rate_limited_delay": -0.5},
        {"rate_limited_delay": 31.0},
        {"attempt_timeout": 0.0},
        {"attempt_timeout": math.inf},
    ],
)
def test_policy_fields_with_bad_values_raise_value_error(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**fields)  # type: ignore[arg-type]


def test_policy_boundaries_are_accepted() -> None:
    RetryPolicy(max_attempts=1, base_delay=1, max_delay=1, rate_limited_delay=0, attempt_timeout=1)
    RetryPolicy(max_attempts=10, base_delay=2.0, max_delay=2.0, rate_limited_delay=2.0)


@pytest.mark.parametrize("fields", [{"rate": "1"}, {"rate": True}, {"burst": 5.0}, {"burst": True}])
def test_rate_limit_fields_of_the_wrong_type_raise_type_error(fields: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        RateLimit(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields", [{"rate": 0.0}, {"rate": -1.0}, {"rate": math.nan}, {"rate": math.inf}, {"burst": 0}]
)
def test_rate_limit_fields_with_bad_values_raise_value_error(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RateLimit(**fields)  # type: ignore[arg-type]


def test_policies_are_frozen_and_keyword_only() -> None:
    policy = RetryPolicy()

    with pytest.raises(AttributeError):
        policy.max_attempts = 5  # type: ignore[misc]
    with pytest.raises(TypeError):
        RetryPolicy(3)  # type: ignore[misc]
    with pytest.raises(TypeError):
        RateLimit(1.0)  # type: ignore[misc]


# --- T7: RetryPolicy.delay (Design 9.2) ----------------------------------------------------------

DELAY_TABLE: tuple[tuple[int, ProviderFailure, float | None, float, float | None], ...] = (
    (1, ProviderFailure.CONNECTION, None, 0.0, 1.0),
    (1, ProviderFailure.CONNECTION, None, 0.5, 1.5),
    (2, ProviderFailure.TIMEOUT, None, 0.75, 3.5),
    (3, ProviderFailure.SERVER_ERROR, None, 0.0, 4.0),
    (5, ProviderFailure.CONNECTION, None, 0.5, 22.5),
    (1, ProviderFailure.RATE_LIMITED, None, 0.5, 15.0),
    (2, ProviderFailure.RATE_LIMITED, None, 0.75, 15.0),
    (1, ProviderFailure.INVALID_RESPONSE, 20.0, 0.0, 20.0),
    (4, ProviderFailure.CONNECTION, 30.0, 0.0, 30.0),
    (1, ProviderFailure.RATE_LIMITED, 31.0, 0.0, None),
)


@pytest.mark.parametrize(("attempt", "failure", "retry_after", "jitter", "expected"), DELAY_TABLE)
def test_delay_gives_the_spec_table(
    attempt: int,
    failure: ProviderFailure,
    retry_after: float | None,
    jitter: float,
    expected: float | None,
) -> None:
    assert RetryPolicy().delay(attempt, unavailable(failure, retry_after), jitter) == expected


def test_delay_grows_to_the_cap_for_huge_attempts() -> None:
    policy = RetryPolicy()

    assert policy.delay(2_000, unavailable(), 0.0) == 15.0
    assert policy.delay(10**30, unavailable(), 0.5) == 22.5


def test_delay_with_a_tiny_base_still_doubles_exactly() -> None:
    policy = RetryPolicy(base_delay=1e-300, max_delay=30.0, rate_limited_delay=0.0)

    assert policy.delay(1_100, unavailable(), 0.0) == 15.0
    assert policy.delay(1, unavailable(), 0.0) == 5e-301


@pytest.mark.parametrize("attempt", [True, 1.0, "1"])
def test_delay_rejects_a_non_int_attempt(attempt: object) -> None:
    with pytest.raises(TypeError):
        RetryPolicy().delay(attempt, unavailable(), 0.0)  # type: ignore[arg-type]


def test_delay_rejects_an_attempt_below_one() -> None:
    with pytest.raises(ValueError):
        RetryPolicy().delay(0, unavailable(), 0.0)


@pytest.mark.parametrize("jitter", [-0.1, 1.0, math.nan, math.inf])
def test_delay_rejects_a_jitter_outside_the_unit_interval(jitter: float) -> None:
    with pytest.raises(ValueError):
        RetryPolicy().delay(1, unavailable(), jitter)


@pytest.mark.parametrize("jitter", [True, "0.5", None])
def test_delay_rejects_a_non_number_jitter(jitter: object) -> None:
    with pytest.raises(TypeError):
        RetryPolicy().delay(1, unavailable(), jitter)  # type: ignore[arg-type]


def test_delay_rejects_an_error_that_is_not_provider_unavailable() -> None:
    with pytest.raises(TypeError):
        RetryPolicy().delay(1, NoDataError("no data"), 0.0)  # type: ignore[arg-type]


# --- T7: TokenBucket (Design 9.5) -----------------------------------------------------------------


def run_bucket(limit: RateLimit, script: list[float | None]) -> list[float]:
    """Run ``script``: ``None`` is one acquire, a number sets the fake clock."""
    clock = FakeClock()
    bucket = TokenBucket(limit, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        for step in script:
            if step is None:
                await bucket.acquire()
            else:
                clock.now = step

    asyncio.run(scenario())
    return clock.sleeps


def test_a_slow_bucket_paces_after_its_burst() -> None:
    assert run_bucket(RateLimit(rate=0.5, burst=2), [None] * 4) == [2.0, 2.0]


def test_the_default_bucket_allows_five_immediate_acquires() -> None:
    assert run_bucket(RateLimit(rate=1.0, burst=5), [None] * 7) == [1.0, 1.0]


def test_elapsed_time_refills_partially() -> None:
    assert run_bucket(RateLimit(rate=1.0, burst=2), [None, None, 1001.5, None, None]) == [0.5]


def test_a_clock_that_goes_backwards_adds_nothing() -> None:
    assert run_bucket(RateLimit(rate=1.0, burst=2), [None, None, 990.0, None]) == [1.0]


def test_the_refill_is_capped_at_the_burst() -> None:
    assert run_bucket(RateLimit(rate=1.0, burst=2), [None, None, 5000.0, None, None, None]) == [1.0]


def test_concurrent_acquires_are_served_in_arrival_order() -> None:
    clock = FakeClock()
    bucket = TokenBucket(RateLimit(rate=1.0, burst=1), clock=clock, sleep=clock.sleep)
    order: list[int] = []

    async def take(number: int) -> None:
        await bucket.acquire()
        order.append(number)

    async def scenario() -> None:
        await asyncio.gather(*(take(number) for number in range(4)))

    asyncio.run(scenario())

    assert order == [0, 1, 2, 3]
    assert clock.sleeps == [1.0, 1.0, 1.0]


def test_the_default_clock_is_the_running_loop_time() -> None:
    readings: list[float] = []

    async def fake_sleep(delay: float) -> None:
        readings.append(delay)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bucket = TokenBucket(RateLimit(rate=1.0, burst=1), sleep=fake_sleep)
        original = loop.time
        instants = [50.0, 50.0, 51.0]
        loop.time = lambda: instants.pop(0) if instants else original()  # type: ignore[method-assign]
        try:
            await bucket.acquire()
            await bucket.acquire()
        finally:
            loop.time = original  # type: ignore[method-assign]

    asyncio.run(scenario())

    assert readings == [1.0]


def test_the_bucket_rejects_arguments_of_the_wrong_type() -> None:
    with pytest.raises(TypeError):
        TokenBucket((1.0, 5))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TokenBucket(RateLimit(), clock=1000.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TokenBucket(RateLimit(), sleep=None)  # type: ignore[arg-type]


# --- T8: call (Design 9.3) ---------------------------------------------------------------------


def test_a_successful_operation_returns_its_result_without_sleeping() -> None:
    sleep = RecordingSleep()
    subject = transport(sleep=sleep)

    result = asyncio.run(subject.call(lambda: 42, ticker="SPY", timeframe=Timeframe.H1))

    assert result == 42
    assert sleep.delays == []


def test_unavailable_errors_are_retried_with_the_policy_delays(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=TRANSPORT_LOGGER)
    sleep = RecordingSleep()
    subject = transport(sleep=sleep, jitter=0.5)
    errors = iter([unavailable(), unavailable(ProviderFailure.TIMEOUT)])
    calls: list[int] = []

    def operation() -> str:
        calls.append(len(calls) + 1)
        error = next(errors, None)
        if error is not None:
            raise error
        return "ok"

    result = asyncio.run(subject.call(operation, ticker="SPY", timeframe=Timeframe.H1))

    assert result == "ok"
    assert calls == [1, 2, 3]
    assert sleep.delays == [1.5, 3.0]
    assert [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == TRANSPORT_LOGGER
    ] == [
        (
            "INFO",
            "retrying provider call for SPY 1h after connection (attempt 1 of 3, waiting 1.500 s)",
        ),
        (
            "INFO",
            "retrying provider call for SPY 1h after timeout (attempt 2 of 3, waiting 3.000 s)",
        ),
    ]


def test_the_last_error_instance_is_raised_after_max_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=TRANSPORT_LOGGER)
    sleep = RecordingSleep()
    subject = transport(sleep=sleep, jitter=0.0)
    errors = [unavailable(), unavailable(), unavailable(ProviderFailure.SERVER_ERROR)]
    calls: list[int] = []

    def operation() -> None:
        calls.append(1)
        raise errors[len(calls) - 1]

    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert caught.value is errors[2]
    assert len(calls) == 3
    assert sleep.delays == [1.0, 2.0]
    messages = [record.getMessage() for record in caplog.records if record.name == TRANSPORT_LOGGER]
    assert messages == [
        "retrying provider call for SPY - after connection (attempt 1 of 3, waiting 1.000 s)",
        "retrying provider call for SPY - after connection (attempt 2 of 3, waiting 2.000 s)",
    ]


def test_a_delay_of_none_stops_retrying_with_that_error_instance() -> None:
    sleep = RecordingSleep()
    subject = transport(sleep=sleep)
    error = unavailable(ProviderFailure.RATE_LIMITED, retry_after=31.0)
    calls: list[int] = []

    def operation() -> None:
        calls.append(1)
        raise error

    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert caught.value is error
    assert calls == [1]
    assert sleep.delays == []


def test_max_attempts_of_one_never_sleeps_and_never_asks_for_jitter() -> None:
    sleep = RecordingSleep()
    jitters: list[int] = []

    def jitter() -> float:
        jitters.append(1)
        return 0.5

    clock = FakeClock()
    subject = ProviderTransport(
        policy=RetryPolicy(max_attempts=1),
        limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
        sleep=sleep,
        jitter=jitter,
    )
    error = unavailable()

    def operation() -> None:
        raise error

    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert caught.value is error
    assert sleep.delays == []
    assert jitters == []


@pytest.mark.parametrize(
    "error",
    [
        NoDataError("no data", ticker="SPY"),
        InvalidTickerError(InvalidTickerReason.NOT_FOUND, "unknown", ticker="SPY"),
        TypeError("a programming error"),
        KeyError("chart"),
    ],
    ids=["no_data", "invalid_ticker", "type_error", "key_error"],
)
def test_other_exceptions_propagate_immediately_without_sleeping(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=TRANSPORT_LOGGER)
    sleep = RecordingSleep()
    subject = transport(sleep=sleep)
    calls: list[int] = []

    def operation() -> None:
        calls.append(1)
        raise error

    with pytest.raises(type(error)) as caught:
        asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert caught.value is error
    assert calls == [1]
    assert sleep.delays == []
    assert [record for record in caplog.records if record.name == TRANSPORT_LOGGER] == []


def test_a_base_exception_that_is_not_an_exception_propagates_unchanged() -> None:
    class Stop(BaseException):
        pass

    sleep = RecordingSleep()
    subject = transport(sleep=sleep)
    error = Stop()

    def operation() -> None:
        raise error

    with pytest.raises(Stop) as caught:
        asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert caught.value is error
    assert sleep.delays == []


def test_each_attempt_takes_one_token() -> None:
    clock = FakeClock()
    sleep = RecordingSleep()
    subject = ProviderTransport(
        policy=RetryPolicy(),
        limiter=TokenBucket(RateLimit(rate=0.5, burst=1), clock=clock, sleep=clock.sleep),
        sleep=sleep,
        jitter=lambda: 0.0,
    )
    errors = iter([unavailable(), unavailable()])

    def operation() -> str:
        error = next(errors, None)
        if error is not None:
            raise error
        return "ok"

    assert asyncio.run(subject.call(operation, ticker="SPY", timeframe=None)) == "ok"
    assert clock.sleeps == [2.0, 2.0]
    assert sleep.delays == [1.0, 2.0]


def test_the_default_logger_and_jitter_are_used_when_not_injected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=TRANSPORT_LOGGER)
    clock = FakeClock()
    sleep = RecordingSleep()
    subject = ProviderTransport(
        policy=RetryPolicy(max_attempts=2),
        limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
        sleep=sleep,
    )
    errors = iter([unavailable()])

    def operation() -> str:
        error = next(errors, None)
        if error is not None:
            raise error
        return "ok"

    assert asyncio.run(subject.call(operation, ticker="SPY", timeframe=Timeframe.D1)) == "ok"
    assert len(sleep.delays) == 1
    assert 1.0 <= sleep.delays[0] < 2.0
    assert [record.name for record in caplog.records if record.name == TRANSPORT_LOGGER] == [
        TRANSPORT_LOGGER
    ]


def test_an_injected_logger_receives_the_retry_record(caplog: pytest.LogCaptureFixture) -> None:
    injected = logging.getLogger("tests.transport.injected")
    caplog.set_level(logging.DEBUG, logger=injected.name)
    caplog.set_level(logging.DEBUG, logger=TRANSPORT_LOGGER)
    subject = transport(logger=injected, policy=RetryPolicy(max_attempts=2))
    errors = iter([unavailable()])

    def operation() -> str:
        error = next(errors, None)
        if error is not None:
            raise error
        return "ok"

    asyncio.run(subject.call(operation, ticker="SPY", timeframe=None))

    assert [record.name for record in caplog.records] == [injected.name]


def test_the_transport_rejects_arguments_of_the_wrong_type() -> None:
    clock = FakeClock()
    limiter = TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep)
    with pytest.raises(TypeError):
        ProviderTransport(policy={"max_attempts": 3}, limiter=limiter)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderTransport(policy=RetryPolicy(), limiter=RateLimit())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderTransport(policy=RetryPolicy(), limiter=limiter, sleep=1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderTransport(policy=RetryPolicy(), limiter=limiter, jitter=0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderTransport(policy=RetryPolicy(), limiter=limiter, logger="log")  # type: ignore[arg-type]


def test_call_rejects_arguments_of_the_wrong_type() -> None:
    async def scenario() -> None:
        subject = transport()  # one transport per event loop (D68)
        with pytest.raises(TypeError):
            await subject.call("not callable", ticker="SPY", timeframe=None)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await subject.call(lambda: 1, ticker=42, timeframe=None)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            await subject.call(lambda: 1, ticker="SPY", timeframe="1h")  # type: ignore[arg-type]

    asyncio.run(scenario())


# --- T8: one attempt (Design 9.4) --------------------------------------------------------------

_HAND_OFF_TIMEOUT = 30.0  # a safety net for a broken test, never reached when the code is right


def blocking_transport(*, logger: logging.Logger | None = None) -> ProviderTransport:
    return transport(policy=RetryPolicy(max_attempts=1, attempt_timeout=0.001), logger=logger)


def test_a_timed_out_attempt_raises_a_timeout_from_none_and_its_late_result_is_discarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    ended = threading.Event()

    def operation() -> int:
        gate.wait(_HAND_OFF_TIMEOUT)
        ended.set()
        return 7

    async def scenario() -> ProviderUnavailableError:
        subject = blocking_transport()
        try:
            await subject.call(operation, ticker="SPY", timeframe=Timeframe.H4)
        except ProviderUnavailableError as error:
            caught = error
        else:
            raise AssertionError("the attempt did not time out")
        assert not ended.is_set()  # the worker is still running
        gate.set()
        await subject.aclose()
        assert ended.is_set()
        return caught

    error = asyncio.run(scenario())

    assert error.failure is ProviderFailure.TIMEOUT
    assert error.ticker == "SPY"
    assert error.timeframe is Timeframe.H4
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_a_late_exception_is_discarded_without_logging(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()

    def operation() -> int:
        gate.wait(_HAND_OFF_TIMEOUT)
        raise ValueError("late failure")

    async def scenario() -> None:
        subject = blocking_transport()
        with pytest.raises(ProviderUnavailableError):
            await subject.call(operation, ticker="SPY", timeframe=None)
        gate.set()
        await subject.aclose()
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    gc.collect()

    assert [
        record
        for record in caplog.records
        if record.levelno >= logging.INFO or record.name == TRANSPORT_LOGGER
    ] == []


def test_an_abandoned_worker_keeps_the_in_flight_slot_until_it_ends() -> None:
    gate = threading.Event()
    events: list[str] = []
    lock = threading.Lock()

    def record(event: str) -> None:
        with lock:
            events.append(event)

    def first() -> int:
        record("first started")
        gate.wait(_HAND_OFF_TIMEOUT)
        record("first ended")
        return 1

    def second() -> int:
        record("second started")
        return 2

    async def scenario() -> None:
        subject = blocking_transport()
        with pytest.raises(ProviderUnavailableError):
            await subject.call(first, ticker="SPY", timeframe=None)
        pending = asyncio.ensure_future(subject.call(second, ticker="QQQ", timeframe=None))
        for _ in range(20):
            await asyncio.sleep(0)
        assert events == ["first started"]
        assert not pending.done()
        gate.set()
        # The second attempt may also exceed its tiny timeout; the order is what counts.
        with contextlib.suppress(ProviderUnavailableError):
            await pending
        await subject.aclose()

    asyncio.run(scenario())

    assert events == ["first started", "first ended", "second started"]


def test_a_cancelled_call_propagates_and_its_worker_keeps_the_slot() -> None:
    gate = threading.Event()
    started = threading.Event()
    events: list[str] = []
    lock = threading.Lock()

    def record(event: str) -> None:
        with lock:
            events.append(event)

    def first() -> int:
        started.set()
        gate.wait(_HAND_OFF_TIMEOUT)
        record("first ended")
        return 1

    def second() -> int:
        record("second started")
        return 2

    async def scenario() -> None:
        clock = FakeClock()
        subject = ProviderTransport(
            policy=RetryPolicy(max_attempts=1, attempt_timeout=_HAND_OFF_TIMEOUT),
            limiter=TokenBucket(RateLimit(), clock=clock, sleep=clock.sleep),
            sleep=RecordingSleep(),
            jitter=lambda: 0.5,
        )
        running = asyncio.ensure_future(subject.call(first, ticker="SPY", timeframe=None))
        while not started.is_set():
            await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        pending = asyncio.ensure_future(subject.call(second, ticker="QQQ", timeframe=None))
        for _ in range(20):
            await asyncio.sleep(0)
        assert events == []
        gate.set()
        assert await pending == 2
        await subject.aclose()

    asyncio.run(scenario())

    assert events == ["first ended", "second started"]


def test_aclose_waits_for_running_workers_and_is_idempotent() -> None:
    gate = threading.Event()
    ended = threading.Event()

    def operation() -> int:
        gate.wait(_HAND_OFF_TIMEOUT)
        ended.set()
        return 1

    async def scenario() -> None:
        subject = blocking_transport()
        with pytest.raises(ProviderUnavailableError):
            await subject.call(operation, ticker="SPY", timeframe=None)
        closing = asyncio.ensure_future(subject.aclose())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not closing.done()
        gate.set()
        await closing
        assert ended.is_set()
        await subject.aclose()

    asyncio.run(scenario())


def test_call_after_aclose_raises_runtime_error_before_anything_else() -> None:
    sleep = RecordingSleep()
    subject = transport(sleep=sleep)
    calls: list[int] = []

    async def scenario() -> None:
        await subject.aclose()
        with pytest.raises(RuntimeError, match="provider transport is closed"):
            await subject.call("not callable", ticker=42, timeframe="1h")  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="provider transport is closed"):
            await subject.call(lambda: calls.append(1), ticker="SPY", timeframe=None)

    asyncio.run(scenario())

    assert calls == []
    assert sleep.delays == []


def test_the_operation_runs_in_a_worker_thread() -> None:
    subject = transport()
    main_thread = threading.get_ident()

    thread = asyncio.run(subject.call(threading.get_ident, ticker="SPY", timeframe=None))

    assert thread != main_thread


def test_concurrent_calls_run_one_operation_at_a_time() -> None:
    subject = transport(policy=RetryPolicy(attempt_timeout=_HAND_OFF_TIMEOUT))
    active: list[int] = []
    peak: list[int] = []
    lock = threading.Lock()

    def operation(number: int) -> Callable[[], int]:
        def run() -> int:
            with lock:
                active.append(number)
                peak.append(len(active))
            with lock:
                active.remove(number)
            return number

        return run

    async def scenario() -> list[int]:
        return list(
            await asyncio.gather(
                *(subject.call(operation(n), ticker="SPY", timeframe=None) for n in range(5))
            )
        )

    assert asyncio.run(scenario()) == [0, 1, 2, 3, 4]
    assert peak == [1, 1, 1, 1, 1]


def test_a_worker_cancelled_at_loop_shutdown_releases_the_slot_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    ended = threading.Event()

    def operation() -> int:
        gate.wait(_HAND_OFF_TIMEOUT)
        ended.set()
        return 1

    async def scenario() -> None:
        subject = blocking_transport()
        with pytest.raises(ProviderUnavailableError):
            await subject.call(operation, ticker="SPY", timeframe=None)
        current = asyncio.current_task()
        workers = [task for task in asyncio.all_tasks() if task is not current]
        assert len(workers) == 1
        # asyncio.run cancels the abandoned worker on shutdown; the thread is released then.
        workers[0].add_done_callback(lambda task: gate.set())

    asyncio.run(scenario())

    assert ended.is_set()
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
