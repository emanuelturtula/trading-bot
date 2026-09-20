"""``SchedulerPolicy`` bounds and defaults (spec 016, T1; AC18).

Every bound of Design 6 is pinned from a literal, and so are the defaults: the policy is what
decides when a fire is too late to run and how long an unpublished candle is retried, so a
silent change to any of those numbers must fail here. The two ``TB_*`` settings the policy is
built from are tested where every other setting is, in ``test_config.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_bot.config import Settings
from trading_bot.scheduler.policy import (
    DEFAULT_POLICY,
    DEFAULT_RETRY_BACKOFF,
    MAX_CLOSE_DELAY,
    MAX_MISFIRE_GRACE,
    MIN_MISFIRE_GRACE,
    SchedulerPolicy,
)

# --- The literals ---------------------------------------------------------------------------


def test_the_bounds_are_the_documented_literals() -> None:
    assert MAX_CLOSE_DELAY.total_seconds() == 900  # the 30-minute gap of a session's last hour
    assert MIN_MISFIRE_GRACE.total_seconds() == 60
    assert MAX_MISFIRE_GRACE.total_seconds() == 3600
    assert [wait.total_seconds() for wait in DEFAULT_RETRY_BACKOFF] == [30, 60, 120, 240]


def test_the_default_policy_is_the_documented_one() -> None:
    assert SchedulerPolicy() == DEFAULT_POLICY
    assert DEFAULT_POLICY.close_delay == timedelta(seconds=120)
    assert DEFAULT_POLICY.misfire_grace == timedelta(minutes=15)
    assert DEFAULT_POLICY.retry_backoff == DEFAULT_RETRY_BACKOFF
    assert DEFAULT_POLICY.retry_window == timedelta(minutes=10)
    assert DEFAULT_POLICY.shutdown_timeout == timedelta(seconds=10)


def test_the_policy_is_frozen_slotted_and_keyword_only() -> None:
    with pytest.raises(TypeError):
        SchedulerPolicy(timedelta(seconds=1))  # type: ignore[misc]
    with pytest.raises(AttributeError):
        DEFAULT_POLICY.close_delay = timedelta(0)  # type: ignore[misc]
    assert not hasattr(DEFAULT_POLICY, "__dict__")
    assert SchedulerPolicy(close_delay=timedelta(seconds=60)) == SchedulerPolicy(
        close_delay=timedelta(seconds=60)
    )


# --- The bounds -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delay", [timedelta(0), timedelta(seconds=1), timedelta(minutes=15)], ids=str
)
def test_a_close_delay_inside_the_range_is_accepted(delay: timedelta) -> None:
    assert SchedulerPolicy(close_delay=delay).close_delay == delay


@pytest.mark.parametrize(
    "delay", [timedelta(seconds=-1), timedelta(minutes=15, seconds=1), timedelta(hours=1)], ids=str
)
def test_a_close_delay_outside_the_range_is_rejected(delay: timedelta) -> None:
    with pytest.raises(ValueError, match="close_delay"):
        SchedulerPolicy(close_delay=delay)


@pytest.mark.parametrize(
    "grace", [timedelta(minutes=1), timedelta(minutes=15), timedelta(hours=1)], ids=str
)
def test_a_misfire_grace_inside_the_range_is_accepted(grace: timedelta) -> None:
    assert SchedulerPolicy(misfire_grace=grace).misfire_grace == grace


@pytest.mark.parametrize(
    "grace",
    [timedelta(0), timedelta(seconds=59), timedelta(hours=1, seconds=1), timedelta(seconds=-60)],
    ids=str,
)
def test_a_misfire_grace_outside_the_range_is_rejected(grace: timedelta) -> None:
    """A zero grace would reject every fire: a fire always arrives after its instant (D146)."""
    with pytest.raises(ValueError, match="misfire_grace"):
        SchedulerPolicy(misfire_grace=grace)


@pytest.mark.parametrize(
    "backoff",
    [
        (),
        (timedelta(seconds=1),),
        (timedelta(seconds=30), timedelta(seconds=30)),
        DEFAULT_RETRY_BACKOFF,
    ],
    ids=["empty", "one", "non-decreasing", "default"],
)
def test_a_valid_backoff_is_accepted(backoff: tuple[timedelta, ...]) -> None:
    assert SchedulerPolicy(retry_backoff=backoff).retry_backoff == backoff


@pytest.mark.parametrize(
    "backoff",
    [
        (timedelta(0),),
        (timedelta(seconds=-1),),
        (timedelta(seconds=60), timedelta(seconds=30)),
        (timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=45)),
    ],
    ids=["zero", "negative", "decreasing", "decreasing-late"],
)
def test_an_invalid_backoff_is_rejected(backoff: tuple[timedelta, ...]) -> None:
    with pytest.raises(ValueError, match="retry_backoff"):
        SchedulerPolicy(retry_backoff=backoff)


def test_a_backoff_that_is_not_a_tuple_is_rejected() -> None:
    with pytest.raises(TypeError, match="retry_backoff"):
        SchedulerPolicy(retry_backoff=[timedelta(seconds=30)])  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [timedelta(0), timedelta(minutes=10)], ids=str)
def test_a_retry_window_of_zero_or_more_is_accepted(window: timedelta) -> None:
    assert SchedulerPolicy(retry_window=window).retry_window == window


def test_a_negative_retry_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="retry_window"):
        SchedulerPolicy(retry_window=timedelta(seconds=-1))


@pytest.mark.parametrize("timeout", [timedelta(0), timedelta(seconds=-1)], ids=str)
def test_a_non_positive_shutdown_timeout_is_rejected(timeout: timedelta) -> None:
    with pytest.raises(ValueError, match="shutdown_timeout"):
        SchedulerPolicy(shutdown_timeout=timeout)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_delay", timedelta(seconds=1, milliseconds=500)),
        ("misfire_grace", timedelta(minutes=5, microseconds=1)),
        ("retry_window", timedelta(seconds=30, milliseconds=1)),
        ("shutdown_timeout", timedelta(seconds=10, microseconds=1)),
    ],
)
def test_a_fractional_second_is_rejected(field: str, value: timedelta) -> None:
    """Whole seconds keep fire times exact and logs readable (Design 6)."""
    with pytest.raises(ValueError, match="whole number of seconds"):
        SchedulerPolicy(**{field: value})


def test_a_fractional_second_in_the_backoff_is_rejected() -> None:
    with pytest.raises(ValueError, match="whole number of seconds"):
        SchedulerPolicy(retry_backoff=(timedelta(seconds=30, milliseconds=500),))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_delay", 120),
        ("misfire_grace", "900"),
        ("retry_window", 600.0),
        ("shutdown_timeout", None),
        ("retry_backoff", (30,)),
    ],
)
def test_a_value_that_is_not_a_timedelta_is_rejected(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=field):
        SchedulerPolicy(**{field: value})


def test_a_boolean_is_not_a_timedelta() -> None:
    with pytest.raises(TypeError, match="close_delay"):
        SchedulerPolicy(close_delay=True)  # type: ignore[arg-type]


# --- The policy built from the settings (AC18) ----------------------------------------------


def test_the_policy_can_be_built_from_the_settings() -> None:
    """The shape #16 uses (spec 016, hand-off 2): the defaults give ``DEFAULT_POLICY``."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    policy = SchedulerPolicy(
        close_delay=timedelta(seconds=settings.candle_close_delay_seconds),
        misfire_grace=timedelta(seconds=settings.scheduler_misfire_grace_seconds),
    )

    assert policy == DEFAULT_POLICY
