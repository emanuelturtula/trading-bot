"""What one run produced: the report the scheduler and the alerts read (spec 015, Design 6).

``RunReport`` is a frozen tree of values: nothing is persisted here (decision D136), so the
consumers are #15, which re-runs ``retryable_tickers`` with the same ``now``, and the log, whose
one-line ``summary`` F7 (#26, #27) will parse. The report holds no exception object and no
provider text, only symbols, timeframe codes, enum values, ISO instants, integers and signal
keys, so it can be logged without re-checking what is safe to print.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trading_bot.domain.signals import SignalKey
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc

__all__ = [
    "FailureKind",
    "RunReport",
    "SignalDisposition",
    "SignalOutcome",
    "TickerOutcome",
    "TickerStatus",
]


class FailureKind(StrEnum):
    """Why a ticker was skipped; the classification of spec 015, Design 9."""

    INVALID_TICKER = "invalid_ticker"
    NO_DATA = "no_data"
    NOT_PUBLISHED = "not_published"
    PROVIDER_DATA = "provider_data"
    UNAVAILABLE = "unavailable"
    UNEXPECTED = "unexpected"


class TickerStatus(StrEnum):
    """Whether the ticker's rules were evaluated at all."""

    EVALUATED = "evaluated"
    FAILED = "failed"


class SignalDisposition(StrEnum):
    """What the run did with one triggered evaluation."""

    NOTIFIED = "notified"  # new, recorded, committed and accepted by the notifier
    UNDELIVERED = "undelivered"  # new and recorded; the notifier failed (decision D132)
    DUPLICATE = "duplicate"  # the key already existed: nothing sent (CLAUDE.md rule 5)
    COOLDOWN = "cooldown"  # suppressed by cooldown_bars; nothing written
    SUPERSEDED = "superseded"  # a later signal of the pair exists; nothing written
    REJECTED = "rejected"  # the ticker or rule changed after the snapshot (decision D134)


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalOutcome:
    """One triggered evaluation and what the run decided about it."""

    key: SignalKey
    rule_id: int  # the stored rule's row id
    disposition: SignalDisposition
    signal_id: int | None  # the stored row, for every disposition that has one


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerOutcome:
    """One planned ticker: either its rules were evaluated, or the fetch or evaluation failed."""

    ticker: str  # normalized symbol
    ticker_id: int
    status: TickerStatus
    failure: FailureKind | None  # set exactly when the status is FAILED
    retryable: bool  # the class-level flag of the market data error (spec 010)
    rules_evaluated: int
    signals: tuple[SignalOutcome, ...]

    def __post_init__(self) -> None:
        if (self.status is TickerStatus.FAILED) != (self.failure is not None):
            raise ValueError(
                "a ticker outcome carries a failure exactly when its status is failed,"
                f" got status {self.status.value} with failure {self.failure}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReport:
    """The outcome of one ``SignalEngine.run``, in plan order."""

    timeframe: Timeframe
    now: datetime  # the scheduled candle close the run received, in UTC
    paused: bool
    tickers: tuple[TickerOutcome, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", to_utc(self.now))

    @property
    def signals(self) -> tuple[SignalOutcome, ...]:
        """Every signal outcome of the run, in plan order then rule order."""
        return tuple(signal for ticker in self.tickers for signal in ticker.signals)

    @property
    def notified(self) -> int:
        """How many signals the notifier accepted."""
        return sum(1 for signal in self.signals if signal.disposition is SignalDisposition.NOTIFIED)

    @property
    def failures(self) -> tuple[TickerOutcome, ...]:
        """The tickers whose fetch or evaluation failed, in plan order."""
        return tuple(ticker for ticker in self.tickers if ticker.status is TickerStatus.FAILED)

    @property
    def retryable_tickers(self) -> tuple[str, ...]:
        """The symbols #15 re-runs with the **same** ``now`` (spec 010, decision D44)."""
        return tuple(ticker.ticker for ticker in self.failures if ticker.retryable)

    @property
    def summary(self) -> str:
        """The one-line record of spec 015, Design 10, which F7 parses."""
        when = f"{self.timeframe.value} run at {self.now.isoformat()}"
        if self.paused:
            return f"{when}: skipped, paused"
        rules = sum(ticker.rules_evaluated for ticker in self.tickers)
        signals = self.signals
        failures = self.failures
        return (
            f"{when}: {len(self.tickers)} tickers, {rules} rules,"
            f" {len(signals)} signals{_dispositions(signals)},"
            f" {len(failures)} failed{_retryable(failures, self.retryable_tickers)}"
        )


def _dispositions(signals: tuple[SignalOutcome, ...]) -> str:
    """`` (2 notified, 1 duplicate)``: the non-zero dispositions in their declared order."""
    if not signals:
        return ""
    counts = [
        (disposition, sum(1 for signal in signals if signal.disposition is disposition))
        for disposition in SignalDisposition
    ]
    named = ", ".join(f"{count} {disposition.value}" for disposition, count in counts if count)
    return f" ({named})"


def _retryable(failures: tuple[TickerOutcome, ...], retryable: tuple[str, ...]) -> str:
    return f" ({len(retryable)} retryable)" if failures else ""
