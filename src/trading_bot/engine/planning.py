"""The configuration snapshot of one run (spec 015, Design 4; decisions D126, D127).

A run reads the pause, the enabled tickers of its timeframe and their enabled rules in **one**
unit of work, into frozen records, and never reads configuration again: a configuration change
takes effect at the next run, which is what an operator editing the CLI while a run is in flight
expects, and a run cannot contradict itself halfway.

``plan_lookback`` decides how many closed candles to ask the provider for. ``stable_warmup`` is
what specs 005 and 006 require for values that do not depend on where the fetch window starts,
and the maximum over the ticker's rules lets one fetch serve them all. The ``cooldown_bars + 1``
term is this module's addition: the cooldown counts **positions in the evaluated frame**
(decision D129), so a shorter frame could not tell "the previous signal is inside the cooldown"
from "it is older than the frame".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from trading_bot.data.provider import MAX_LOOKBACK
from trading_bot.domain.rules.schema import Rule
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.engine.unit_of_work import EngineRepositories
from trading_bot.persistence.records import StoredRule

__all__ = ["RunPlan", "TickerPlan", "normalize_symbols", "plan_lookback", "read_plan"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerPlan:
    """One ticker of the snapshot: what to fetch and which rules to evaluate on it."""

    ticker_id: int
    symbol: str  # normalized
    timeframe: Timeframe
    rules: tuple[StoredRule, ...]  # enabled, ordered by name, at least one
    lookback: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RunPlan:
    """The whole configuration of one run, read in a single unit of work."""

    timeframe: Timeframe
    paused_since: datetime | None
    tickers: tuple[TickerPlan, ...]

    @property
    def paused(self) -> bool:
        """The global pause is the presence of ``paused_since`` (spec 014, D118)."""
        return self.paused_since is not None


def plan_lookback(rules: Sequence[Rule]) -> int:
    """``min(MAX_LOOKBACK, max(max(stable_warmup(), cooldown_bars + 1)))`` (decision D127).

    ``rules`` must hold at least one rule (``ValueError``). With the catalog's bounds the
    maximum is 2 255 and ``MAX_COOLDOWN_BARS + 1`` is 501, both far below ``MAX_LOOKBACK``
    (5 000); the cap is kept so that a future catalog cannot make the provider raise.
    """
    if not rules:
        raise ValueError("a lookback needs at least one rule")
    needed = max(max(rule.stable_warmup(), rule.cooldown_bars + 1) for rule in rules)
    return min(MAX_LOOKBACK, needed)


def normalize_symbols(tickers: Sequence[str] | None) -> tuple[str, ...] | None:
    """The requested subset as normalized symbols, or ``None`` for "every enabled ticker".

    A ``str`` (or ``bytes``) is a ``TypeError``, not thirty one-letter symbols, and a symbol
    ``normalize_ticker`` rejects is a ``ValueError``. Both are raised before any I/O.
    """
    if tickers is None:
        return None
    if isinstance(tickers, str | bytes) or not isinstance(tickers, Sequence):
        raise TypeError(
            f"tickers must be a sequence of symbols or None, got {type(tickers).__name__}"
        )
    return tuple(normalize_ticker(symbol) for symbol in tickers)


def read_plan(
    repositories: EngineRepositories,
    timeframe: Timeframe,
    *,
    tickers: Sequence[str] | None = None,
) -> RunPlan:
    """The whole configuration of one run, read inside one unit of work (decision D126).

    The bot state is read first: while the bot is paused the run does nothing at all, so no
    configuration is read either (decision D133). ``tickers`` restricts the run to a subset of
    the enabled symbols; a symbol that is not enabled for ``timeframe`` is simply absent from
    the plan, and a ticker with no enabled rule is dropped, so neither produces a fetch or an
    outcome. ``StoredRuleError`` from a document that no longer parses propagates (D134).
    """
    if not isinstance(timeframe, Timeframe):
        raise TypeError(
            f"timeframe must be a Timeframe, got {type(timeframe).__name__} "
            "(use Timeframe.parse for text)"
        )
    requested = normalize_symbols(tickers)
    state = repositories.state.load()
    if state.paused_since is not None:
        return RunPlan(timeframe=timeframe, paused_since=state.paused_since, tickers=())
    planned: list[TickerPlan] = []
    for stored in repositories.tickers.list_enabled(timeframe):
        if requested is not None and stored.symbol not in requested:
            continue
        # The composite foreign keys of spec 013 (D86) make "the rule's timeframe equals the
        # ticker's" a schema invariant, so no filtering is needed here and none is written.
        rules = repositories.assignments.rules_for_ticker(stored.id, enabled_only=True)
        if not rules:
            continue
        planned.append(
            TickerPlan(
                ticker_id=stored.id,
                symbol=stored.symbol,
                timeframe=stored.timeframe,
                rules=rules,
                lookback=plan_lookback([item.rule for item in rules]),
            )
        )
    return RunPlan(timeframe=timeframe, paused_since=None, tickers=tuple(planned))
