"""One run of the pipeline: fetch, evaluate, suppress, record, notify (spec 015, Design 8).

``run(timeframe, now)`` is what the scheduler (#15) calls with the **scheduled candle close**.
The engine reads no clock: ``now`` is that argument, normalized through ``to_utc``, and every
stored instant comes from the repositories' injected clock (decision D125).

The order of one triggered evaluation is the one spec 014 fixed and is the only order in which
a crash cannot resend (decisions D131, D115):

1. one unit of work reads ``latest`` for the cooldown and calls ``record``, then **commits**:
   the durable insert is the claim, and its unique constraint is the single arbiter of
   CLAUDE.md rule 5;
2. only an outcome with ``is_new=True`` on that committed unit of work is notified, with **no**
   session open and outside any worker thread holding one;
3. ``mark_notified`` runs in a unit of work of its own, after the notifier returns.

``_in_unit_of_work`` makes the session discipline structural: the work is a plain function of
the repositories, so it cannot await, cannot reach the provider or the notifier and cannot open
a second unit of work (spec 014, D110).

Errors come in three rings (decision D134): a **ticker** failure (any ``MarketDataError`` or
other ``Exception`` while fetching or evaluating it) is logged and reported, and the run
continues with the next ticker; a **signal** failure (the ticker or rule changed after the
snapshot, or the notifier refused) is logged and reported, and the run continues with the next
signal; everything else — ``StoredRuleError``, ``StoredSignalError``, ``CalendarRangeError``,
any other database failure and every ``BaseException``, ``asyncio.CancelledError`` included —
propagates out of ``run`` after at most one ``ERROR`` record, and no report is returned.

No record in this module passes ``exc_info`` or ``stack_info`` (decision D135): ``RedactingFilter``
rewrites ``record.msg`` only, so a traceback rendered by the formatter is **not** redacted and
could carry a bot token from a notifier exception. Failures are logged with the exception
**class** name, plus the error's own message only for ``MarketDataError``, whose messages spec
010 restricts to codes, symbols, timeframe codes and ISO instants. Issue #50 tracks the filter
itself; nothing here depends on that fix.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import pandas as pd

from trading_bot.data.errors import (
    CandleNotPublishedError,
    InvalidTickerError,
    MarketDataError,
    NoDataError,
    ProviderDataError,
    ProviderUnavailableError,
)
from trading_bot.data.provider import MarketDataProvider
from trading_bot.domain.market_calendar.sessions import CalendarRangeError
from trading_bot.domain.rules.evaluator import Evaluation, evaluate
from trading_bot.domain.signals import Signal, SignalKey
from trading_bot.domain.timeframe import Timeframe
from trading_bot.domain.utc import to_utc
from trading_bot.engine.cooldown import CooldownDecision, cooldown_decision
from trading_bot.engine.planning import RunPlan, TickerPlan, normalize_symbols, read_plan
from trading_bot.engine.results import (
    FailureKind,
    RunReport,
    SignalDisposition,
    SignalOutcome,
    TickerOutcome,
    TickerStatus,
)
from trading_bot.engine.unit_of_work import EngineRepositories, UnitOfWork
from trading_bot.notifications.notifier import Notifier, SignalNotification
from trading_bot.persistence.errors import (
    StoredRuleError,
    StoredSignalError,
    TimeframeMismatchError,
    UnknownRuleError,
    UnknownSignalError,
    UntrackedTickerError,
)
from trading_bot.persistence.records import StoredRule
from trading_bot.persistence.signal_records import RecordOutcome

__all__ = ["SignalEngine"]

_LOGGER_NAME: Final = "trading_bot.engine"

# A corrupt stored rule or signal, and a calendar that does not cover the window, make the
# bot's reasoning unsound: they stop the run rather than skipping one ticker (specs 013 D93,
# 014 D119, 010; decision D134).
_PROPAGATING: Final = (StoredRuleError, StoredSignalError, CalendarRangeError)
# "The ticker or the rule changed after the snapshot": the signal is rejected, never stored
# against the wrong parent (spec 014, D113, D114).
_REJECTING: Final = (UntrackedTickerError, UnknownRuleError, TimeframeMismatchError)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Claim:
    """What one unit of work decided about a triggered evaluation."""

    decision: CooldownDecision
    outcome: RecordOutcome | None  # ``None`` when the cooldown suppressed the signal


class SignalEngine:
    """Orchestrates one run: fetch, evaluate, suppress, record, notify (issue #14).

    One instance is reused for every run and every timeframe: it holds no state between runs,
    so everything a run needs lives in its locals and two runs of one timeframe can only
    overlap if the scheduler lets them (#15 sets ``max_instances=1``).
    """

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        unit_of_work: UnitOfWork,
        notifier: Notifier,
    ) -> None:
        self._provider = provider
        self._unit_of_work = unit_of_work
        self._notifier = notifier
        self._logger = logging.getLogger(_LOGGER_NAME)

    async def run(
        self,
        timeframe: Timeframe,
        now: datetime,
        *,
        tickers: Sequence[str] | None = None,
    ) -> RunReport:
        """Evaluate the last closed candle of every enabled ticker of ``timeframe`` at ``now``.

        ``now`` is the scheduled candle close, not the firing time: it is passed to the provider
        unchanged, so "the last candle closed at ``now``" is the candle the run is about.
        ``tickers`` restricts the run to a subset of the enabled symbols, which is how #15
        retries an unpublished candle **with the same** ``now`` (spec 010, D44).

        Arguments are checked before any I/O: ``timeframe`` must be a ``Timeframe``, ``now`` an
        aware instant, and ``tickers`` a sequence of symbols ``normalize_ticker`` accepts (a
        ``str`` is a ``TypeError``, not thirty one-letter symbols).

        Returns a ``RunReport``; raises only the argument errors above, ``StoredRuleError``,
        ``StoredSignalError``, ``CalendarRangeError`` and any other database failure.
        """
        if not isinstance(timeframe, Timeframe):
            raise TypeError(
                f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
                "(use Timeframe.parse for text)"
            )
        moment = to_utc(now)
        symbols = normalize_symbols(tickers)
        try:
            return await self._run(timeframe, moment, symbols)
        except _PROPAGATING as error:
            self._logger.error(
                "%s run at %s stopped: %s",
                timeframe.value,
                moment.isoformat(),
                type(error).__name__,
            )
            raise

    async def _run(
        self, timeframe: Timeframe, now: datetime, symbols: tuple[str, ...] | None
    ) -> RunReport:
        def read(repositories: EngineRepositories) -> RunPlan:
            return read_plan(repositories, timeframe, tickers=symbols)

        plan = await self._in_unit_of_work(read)
        if plan.paused_since is not None:
            self._logger.info(
                "%s run at %s: skipped, paused since %s",
                timeframe.value,
                now.isoformat(),
                plan.paused_since.isoformat(),
            )
            return RunReport(timeframe=timeframe, now=now, paused=True, tickers=())
        outcomes: list[TickerOutcome] = []
        for ticker_plan in plan.tickers:
            outcomes.append(await self._run_ticker(ticker_plan, now))
        report = RunReport(timeframe=timeframe, now=now, paused=False, tickers=tuple(outcomes))
        self._logger.info("%s", report.summary)
        return report

    async def _run_ticker(self, plan: TickerPlan, now: datetime) -> TickerOutcome:
        """One ticker: one fetch, one evaluation of each rule, then its triggered signals."""
        try:
            frame = await self._provider.fetch_candles(
                plan.symbol, plan.timeframe, plan.lookback, now=now
            )
        except _PROPAGATING:
            raise
        except MarketDataError as error:
            return self._failed(plan, self._log_market_data_failure(plan, error), error.retryable)
        except Exception as error:
            return self._failed_unexpectedly(plan, error)
        self._logger.debug(
            "fetched %d candles for %s %s (lookback %d)",
            len(frame),
            plan.symbol,
            plan.timeframe.value,
            plan.lookback,
        )
        try:
            triggered = await asyncio.to_thread(_evaluate_rules, plan, frame)
        except _PROPAGATING:
            raise
        except Exception as error:
            return self._failed_unexpectedly(plan, error)
        self._logger.debug(
            "evaluated %d rules for %s %s: %d triggered",
            len(plan.rules),
            plan.symbol,
            plan.timeframe.value,
            len(triggered),
        )
        labels = pd.DatetimeIndex(frame.index)
        signals: list[SignalOutcome] = []
        for stored_rule, evaluation in triggered:
            signals.append(await self._deliver(plan, stored_rule, evaluation, frame, labels))
        return TickerOutcome(
            ticker=plan.symbol,
            ticker_id=plan.ticker_id,
            status=TickerStatus.EVALUATED,
            failure=None,
            retryable=False,
            rules_evaluated=len(plan.rules),
            signals=tuple(signals),
        )

    async def _deliver(
        self,
        plan: TickerPlan,
        stored_rule: StoredRule,
        evaluation: Evaluation,
        frame: pd.DataFrame,
        labels: pd.DatetimeIndex,
    ) -> SignalOutcome:
        """The mandatory order: claim and commit, then notify, then stamp (decision D131)."""
        rule = stored_rule.rule
        signal = Signal(
            ticker=plan.symbol,
            timeframe=rule.timeframe,
            rule_id=str(stored_rule.id),
            side=rule.signal,
            candle_close_ts=evaluation.candle_close_ts,
            close_price=evaluation.close_price,
            indicator_values=evaluation.indicator_values,
        )
        key = signal.idempotency_key

        def claim(repositories: EngineRepositories) -> _Claim:
            previous = repositories.signals.latest(plan.ticker_id, stored_rule.id)
            decision = cooldown_decision(
                labels,
                timeframe=plan.timeframe,
                cooldown_bars=rule.cooldown_bars,
                previous_close=None if previous is None else previous.signal.candle_close_ts,
            )
            if decision is not CooldownDecision.ALLOWED:
                return _Claim(decision=decision, outcome=None)
            return _Claim(decision=decision, outcome=repositories.signals.record(signal))

        try:
            claimed = await self._in_unit_of_work(claim)
        except _REJECTING as error:
            self._logger.warning("rejected %s: %s", key, type(error).__name__)
            return _outcome(key, stored_rule, SignalDisposition.REJECTED, None)
        if claimed.outcome is None:
            return self._suppressed(key, stored_rule, claimed.decision, rule.cooldown_bars)
        # The unit of work committed: the claim is durable, and nothing has been sent yet.
        stored = claimed.outcome.stored
        if not claimed.outcome.is_new:
            self._log_duplicate(key, stored.id, stored.signal, signal)
            return _outcome(key, stored_rule, SignalDisposition.DUPLICATE, stored.id)
        try:
            await self._notifier.notify(SignalNotification(signal=stored, rule=rule, candles=frame))
        except Exception as error:
            self._logger.error("delivery failed for %s: %s", key, type(error).__name__)
            return _outcome(key, stored_rule, SignalDisposition.UNDELIVERED, stored.id)
        self._logger.info("notified %s (%s, close %s)", key, signal.side.value, signal.close_price)

        def stamp(repositories: EngineRepositories) -> None:
            repositories.signals.mark_notified(stored.id)

        try:
            await self._in_unit_of_work(stamp)
        except UnknownSignalError:
            self._logger.warning(
                "notified %s but signal %d was removed before it was stamped", key, stored.id
            )
        return _outcome(key, stored_rule, SignalDisposition.NOTIFIED, stored.id)

    async def _in_unit_of_work[T](self, work: Callable[[EngineRepositories], T]) -> T:
        """Run ``work`` in one unit of work, in a worker thread of its own (decision D138).

        ``work`` is a plain function of the repositories: it cannot await, cannot reach the
        notifier or the provider and cannot open a second unit of work, so "never two sessions
        at once in one thread" and "never a session across an ``await``" hold by construction.
        """

        def run() -> T:
            with self._unit_of_work() as repositories:
                return work(repositories)

        return await asyncio.to_thread(run)

    def _suppressed(
        self,
        key: SignalKey,
        stored_rule: StoredRule,
        decision: CooldownDecision,
        cooldown_bars: int,
    ) -> SignalOutcome:
        disposition = (
            SignalDisposition.COOLDOWN
            if decision is CooldownDecision.BLOCKED
            else SignalDisposition.SUPERSEDED
        )
        self._logger.debug("%s %s (cooldown_bars=%d)", decision.value, key, cooldown_bars)
        return _outcome(key, stored_rule, disposition, None)

    def _log_duplicate(
        self, key: SignalKey, signal_id: int, stored: Signal, evaluated: Signal
    ) -> None:
        """A key that already existed. A stored payload that differs is worth a ``WARNING``.

        The first write wins (spec 014, D112), so a revised candle never changes what was
        notified; the record says so instead of hiding it.
        """
        if stored == evaluated:
            self._logger.debug("duplicate %s: already recorded as signal %d", key, signal_id)
        else:
            self._logger.warning(
                "duplicate %s: signal %d was recorded from different values; the stored one stands",
                key,
                signal_id,
            )

    def _log_market_data_failure(self, plan: TickerPlan, error: MarketDataError) -> FailureKind:
        """One record at the level of spec 015 Design 9; ``MarketDataError`` text is safe."""
        kind, level = _classify(error)
        if isinstance(error, CandleNotPublishedError):
            self._logger.info(
                "candle not published for %s %s: %s; the scheduler will retry",
                plan.symbol,
                plan.timeframe.value,
                error,
            )
        else:
            self._logger.log(
                level,
                "%s %s failed (%s): %s: %s",
                plan.symbol,
                plan.timeframe.value,
                kind.value,
                type(error).__name__,
                error,
            )
        return kind

    def _failed_unexpectedly(self, plan: TickerPlan, error: Exception) -> TickerOutcome:
        """An error nothing planned for: isolated, but never silent and never with its text."""
        self._logger.error(
            "%s %s failed (%s): %s",
            plan.symbol,
            plan.timeframe.value,
            FailureKind.UNEXPECTED.value,
            type(error).__name__,
        )
        return self._failed(plan, FailureKind.UNEXPECTED, retryable=False)

    def _failed(self, plan: TickerPlan, kind: FailureKind, retryable: bool) -> TickerOutcome:
        return TickerOutcome(
            ticker=plan.symbol,
            ticker_id=plan.ticker_id,
            status=TickerStatus.FAILED,
            failure=kind,
            retryable=retryable,
            rules_evaluated=0,
            signals=(),
        )


def _evaluate_rules(
    plan: TickerPlan, frame: pd.DataFrame
) -> tuple[tuple[StoredRule, Evaluation], ...]:
    """Every rule of the ticker, once, on the frame as the provider returned it.

    The frame is never truncated, re-sorted or modified: its last row is the last closed candle
    by the provider's contract (spec 010), which is the half of CLAUDE.md rule 4 the engine
    inherits.
    """
    triggered: list[tuple[StoredRule, Evaluation]] = []
    for stored_rule in plan.rules:
        evaluation = evaluate(stored_rule.rule, frame)
        if evaluation.triggered:
            triggered.append((stored_rule, evaluation))
    return tuple(triggered)


def _outcome(
    key: SignalKey,
    stored_rule: StoredRule,
    disposition: SignalDisposition,
    signal_id: int | None,
) -> SignalOutcome:
    return SignalOutcome(
        key=key, rule_id=stored_rule.id, disposition=disposition, signal_id=signal_id
    )


def _classify(error: MarketDataError) -> tuple[FailureKind, int]:
    """The ``FailureKind`` and log level of spec 015 Design 9, by error class."""
    if isinstance(error, CandleNotPublishedError):
        return FailureKind.NOT_PUBLISHED, logging.INFO
    if isinstance(error, InvalidTickerError):
        return FailureKind.INVALID_TICKER, logging.WARNING
    if isinstance(error, NoDataError):
        return FailureKind.NO_DATA, logging.WARNING
    if isinstance(error, ProviderDataError):
        return FailureKind.PROVIDER_DATA, logging.ERROR
    if isinstance(error, ProviderUnavailableError):
        return FailureKind.UNAVAILABLE, logging.WARNING
    # A provider raising a ``MarketDataError`` subclass this release does not know is a bug,
    # not a classified failure: it is reported as unexpected rather than silently grouped.
    return FailureKind.UNEXPECTED, logging.ERROR
