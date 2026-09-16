"""Pure rule evaluation on closed candles (spec 007).

``evaluate(rule, candles)`` decides whether ``rule`` fires on the last row of ``candles``, and
``evaluate_each(rule, candles, last=N)`` gives the same answer for each of the last ``N`` rows.
The last row is treated as the last **closed** candle: dropping the in-progress candle is the
data layer's job (#8), and nothing here reads a clock (CLAUDE.md rules 3 and 4).

Both functions share one vectorized core over the whole frame, so they agree bitwise:

1. every distinct operand becomes a ``float64`` array; each distinct ``(indicator, params)`` is
   computed once per call through the injected registry, and nothing is cached across calls;
2. every distinct condition becomes a boolean array: comparisons read the candle only and
   crossovers the candle and the previous row, and NaN on any value read gives ``False``;
3. nested groups are reduced first, then the root group; the warmup gate forces ``triggered``
   to ``False`` on rows with fewer than ``rule.warmup()`` candles of history;
4. only the requested rows are materialized into ``Evaluation`` objects.

Every per-row value depends only on that row and earlier ones. Spacing between notifications,
deduplication and ``Signal`` construction belong to the engine (#14): the evaluator only reports
whether the conditions hold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt
import pandas as pd

from trading_bot.domain.candles import validate_candles
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.indicators.params import IndicatorParams
from trading_bot.domain.indicators.registry import IndicatorRegistry
from trading_bot.domain.rules.schema import (
    Condition,
    GroupMode,
    IndicatorOperand,
    Operand,
    Operator,
    PriceOperand,
    Rule,
    ValueOperand,
)
from trading_bot.domain.signals import IndicatorValues

__all__ = ["Evaluation", "evaluate", "evaluate_each"]

type _FloatArray = npt.NDArray[np.float64]
type _BoolArray = npt.NDArray[np.bool_]
type _Outputs = dict[str, pd.Series[float]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    """The outcome of a rule at one closed candle.

    - ``triggered``: the conditions hold and the candle has at least ``rule.warmup()`` candles of
      history in the frame (itself included).
    - ``candle_close_ts``: ``rule.timeframe.nominal_close`` of the candle open time, a stdlib
      ``datetime`` in UTC. It identifies the candle; it is not the market close.
    - ``close_price``: the close of the candle.
    - ``indicator_values``: the finite value of each distinct indicator operand at the candle,
      keyed ``indicator(param=value, ...).output`` in document order. NaN values are absent.
    - ``condition_results``: the raw outcome of each entry of ``rule.all_conditions``, in that
      order, before the warmup gate.
    """

    triggered: bool
    candle_close_ts: datetime
    close_price: float
    indicator_values: IndicatorValues
    condition_results: tuple[bool, ...]


def evaluate(
    rule: Rule, candles: pd.DataFrame, *, registry: IndicatorRegistry = REGISTRY
) -> Evaluation:
    """The evaluation of ``rule`` on the last row of ``candles``, the last closed candle.

    ``candles`` must meet the candle frame contract (``CandleValidationError`` otherwise) and hold
    at least one candle (``ValueError``). Arguments of the wrong type raise ``TypeError`` and
    registry errors propagate unchanged. NaN values, flat prices and a history shorter than the
    warmup never raise: the rule simply does not fire. An open time with sub-microsecond
    precision has no ``candle_close_ts`` and raises ``ValueError``. ``candles`` is never modified.

    ``registry`` must be compatible with the catalog that validated ``rule``: the rule's
    parameters and ``rule.warmup()`` come from that catalog.
    """
    _check_arguments(rule, candles, registry)
    validate_candles(candles)
    if len(candles) == 0:
        raise ValueError("no candle to evaluate: the candle frame is empty")
    return _evaluate_rows(rule, candles, registry, first=len(candles) - 1)[0]


def evaluate_each(
    rule: Rule,
    candles: pd.DataFrame,
    *,
    last: int | None = None,
    registry: IndicatorRegistry = REGISTRY,
) -> tuple[Evaluation, ...]:
    """The evaluation of ``rule`` on each of the last ``last`` candles, oldest first.

    ``last=None`` covers the whole frame, a value above the frame length is clamped to it and
    ``0`` gives an empty tuple; a negative ``last`` raises ``ValueError`` and a non-``int``
    (including ``bool``) raises ``TypeError``. An empty frame gives an empty tuple. Every
    evaluation equals ``evaluate`` on the frame truncated at its candle, and the last one equals
    ``evaluate(rule, candles)``. Errors are those of ``evaluate``.
    """
    _check_arguments(rule, candles, registry)
    if last is not None:
        if isinstance(last, bool) or not isinstance(last, int):
            raise TypeError(f"last must be an int or None, got {type(last).__name__}")
        if last < 0:
            raise ValueError("last must not be negative")
    validate_candles(candles)
    length = len(candles)
    covered = length if last is None else min(last, length)
    return _evaluate_rows(rule, candles, registry, first=length - covered)


def _check_arguments(rule: Rule, candles: pd.DataFrame, registry: IndicatorRegistry) -> None:
    if not isinstance(rule, Rule):
        raise TypeError(f"rule must be a Rule, got {type(rule).__name__}")
    if not isinstance(candles, pd.DataFrame):
        raise TypeError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    if not isinstance(registry, IndicatorRegistry):
        raise TypeError(f"registry must be an IndicatorRegistry, got {type(registry).__name__}")


def _evaluate_rows(
    rule: Rule, candles: pd.DataFrame, registry: IndicatorRegistry, *, first: int
) -> tuple[Evaluation, ...]:
    """Evaluations of the rows from position ``first`` to the end of a validated frame.

    The arrays always span the whole frame, whatever ``first`` is, so every entry point computes
    the same values and issues the same registry calls.
    """
    conditions = rule.all_conditions
    operands = _operand_arrays(conditions, candles, registry)
    masks: dict[Condition, _BoolArray] = {}
    for condition in conditions:
        if condition not in masks:
            masks[condition] = _condition_mask(
                condition.op, operands[condition.left], operands[condition.right]
            )
    history = np.arange(1, len(candles) + 1)
    triggered = _rule_mask(rule, masks) & (history >= rule.warmup())
    indicator_columns = tuple(
        (_value_key(operand), values)
        for operand, values in operands.items()
        if isinstance(operand, IndicatorOperand)
    )
    return _materialize(
        rule,
        candles,
        triggered,
        tuple(masks[condition] for condition in conditions),
        indicator_columns,
        first,
    )


def _operand_arrays(
    conditions: Sequence[Condition], candles: pd.DataFrame, registry: IndicatorRegistry
) -> dict[Operand, _FloatArray]:
    """One array per distinct operand, in first-appearance order (``left`` before ``right``)."""
    computed: dict[tuple[str, IndicatorParams], _Outputs] = {}
    arrays: dict[Operand, _FloatArray] = {}
    for condition in conditions:
        for operand in (condition.left, condition.right):
            if operand in arrays:
                continue
            if isinstance(operand, ValueOperand):
                arrays[operand] = np.full(len(candles), operand.value, dtype=np.float64)
            elif isinstance(operand, PriceOperand):
                arrays[operand] = np.asarray(candles[operand.price.value], dtype=np.float64)
            else:
                key = (operand.indicator, operand.params)
                if key not in computed:
                    computed[key] = registry.compute(operand.indicator, operand.params, candles)
                arrays[operand] = np.asarray(computed[key][operand.output], dtype=np.float64)
    return arrays


def _condition_mask(operator: Operator, left: _FloatArray, right: _FloatArray) -> _BoolArray:
    """Per-row outcome of ``left operator right``. IEEE comparisons are ``False`` on NaN."""
    match operator:
        case Operator.LESS:
            return left < right
        case Operator.LESS_OR_EQUAL:
            return left <= right
        case Operator.GREATER:
            return left > right
        case Operator.GREATER_OR_EQUAL:
            return left >= right
        case Operator.CROSSES_ABOVE:
            return _crossover(left, right, above=True)
        # mypy proves this match exhaustive ("Missing return statement" otherwise); coverage
        # cannot see that, so the impossible fall-through arc is excluded.
        case Operator.CROSSES_BELOW:  # pragma: no branch
            return _crossover(left, right, above=False)


def _crossover(left: _FloatArray, right: _FloatArray, *, above: bool) -> _BoolArray:
    """``previous_left <= previous_right and left > right`` (mirrored for a cross below).

    The previous value is the one on the previous row of the same frame, so the first row never
    crosses and a NaN on either row of either side gives ``False``.
    """
    crossed = np.zeros(len(left), dtype=np.bool_)
    if above:
        crossed[1:] = (left[:-1] <= right[:-1]) & (left[1:] > right[1:])
    else:
        crossed[1:] = (left[:-1] >= right[:-1]) & (left[1:] < right[1:])
    return crossed


def _rule_mask(rule: Rule, masks: Mapping[Condition, _BoolArray]) -> _BoolArray:
    """The root group over the condition masks, with nested groups reduced first."""
    members: list[_BoolArray] = []
    for member in rule.conditions.members:
        if isinstance(member, Condition):
            members.append(masks[member])
        else:
            members.append(_group_mask(member.mode, [masks[item] for item in member.members]))
    return _group_mask(rule.conditions.mode, members)


def _group_mask(mode: GroupMode, members: Sequence[_BoolArray]) -> _BoolArray:
    """``all`` is the conjunction and ``any`` the disjunction of non-empty ``members``.

    The arrays are never modified in place: the condition masks are shared with the results.
    """
    combined = members[0]
    for mask in members[1:]:
        combined = combined & mask if mode is GroupMode.ALL else combined | mask
    return combined


def _value_key(operand: IndicatorOperand) -> str:
    """``indicator(param=value, ...).output`` with the canonical parameters in declared order."""
    params = ", ".join(f"{name}={value!r}" for name, value in operand.params.items())
    return f"{operand.indicator}({params}).{operand.output}"


def _materialize(
    rule: Rule,
    candles: pd.DataFrame,
    triggered: _BoolArray,
    condition_masks: tuple[_BoolArray, ...],
    indicator_columns: tuple[tuple[str, _FloatArray], ...],
    first: int,
) -> tuple[Evaluation, ...]:
    """One ``Evaluation`` per row from position ``first``, built from plain Python values."""
    index = candles.index
    closes: list[float] = np.asarray(candles["close"], dtype=np.float64)[first:].tolist()
    fired: list[bool] = triggered[first:].tolist()
    results: list[list[bool]] = [mask[first:].tolist() for mask in condition_masks]
    columns: list[tuple[str, list[float], list[bool]]] = [
        (key, values[first:].tolist(), np.isfinite(values[first:]).tolist())
        for key, values in indicator_columns
    ]
    return tuple(
        Evaluation(
            triggered=fired[offset],
            candle_close_ts=rule.timeframe.nominal_close(index[first + offset]),
            close_price=close,
            indicator_values=IndicatorValues(
                {key: values[offset] for key, values, finite in columns if finite[offset]}
            ),
            condition_results=tuple(result[offset] for result in results),
        )
        for offset, close in enumerate(closes)
    )
