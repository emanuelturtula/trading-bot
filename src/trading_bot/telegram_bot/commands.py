"""The Telegram commands (spec 018, AC1-AC8, AC10; issue #19).

``CommandHandler.handle(text)`` turns the text of one authorized message into the HTML reply, or
``None`` when the message must be ignored. It never sees an unauthorized chat (the poller checks
the allowlist first, D5), never imports python-telegram-bot and reaches the database only
through ``in_command_unit_of_work``, so every command is tested with a temporary database and a
fake provider.

Every user-provided string is HTML-escaped and every reply starts with ``[BETA]`` outside
``prod`` (AC10). Instants are presented in the display zone; storage stays in UTC (rule 6).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Final

from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    ProviderDataError,
    ProviderUnavailableError,
)
from trading_bot.data.provider import MarketDataProvider
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.records import StoredTicker
from trading_bot.telegram_bot.unit_of_work import (
    CommandRepositories,
    CommandUnitOfWork,
    in_command_unit_of_work,
)
from trading_bot.watchlist import WatchOutcome, unwatch_ticker, watch_ticker

__all__ = ["BETA_PREFIX", "HELP_TEXT", "CommandHandler", "NextFireTimes", "ParsedCommand", "parse"]

BETA_PREFIX: Final = "[BETA]"
_DEFAULT_TIMEFRAME: Final = Timeframe.D1
_LOGGER_NAME: Final = "trading_bot.telegram_bot.commands"
_MAX_ARGUMENT_ECHO: Final = 32
_WATCH_VERBS: Final = {
    WatchOutcome.ADDED: "Watching",
    WatchOutcome.ALREADY_WATCHED: "Already watching",
    WatchOutcome.REENABLED: "Watching again",
}

HELP_TEXT: Final = (
    "<b>Commands</b>\n"
    "/add TICKER [1h|4h|1d|1w] - watch a symbol (default 1d) with every rule of that timeframe\n"
    "/remove TICKER [1h|4h|1d|1w] - stop watching it; its history is kept\n"
    "/list - the watched symbols\n"
    "/rules - the stored rules\n"
    "/status - what the bot is doing\n"
    "/pause - stop your signals\n"
    "/resume - start them again\n"
    "/link CODE - link this chat (the code comes from the dashboard)\n"
    "/help - this list\n"
    "\n"
    "Signals only: the bot never places orders."
)

type NextFireTimes = Callable[[], Sequence[tuple[Timeframe, datetime | None]]]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str  # lower-case, without the slash and the @bot suffix
    args: tuple[str, ...]


def parse(text: str, *, bot_username: str | None = None) -> ParsedCommand | None:
    """``/add aapl 1h`` -> ``ParsedCommand("add", ("aapl", "1h"))``; ``None`` for plain text.

    A command addressed to another bot (``/status@other_bot``) is ``None`` as well, when the
    bot's own username is known.
    """
    words = text.strip().split()
    if not words or not words[0].startswith("/") or len(words[0]) < 2:
        return None
    command, _, target = words[0][1:].partition("@")
    if target and bot_username is not None and target.lower() != bot_username.lower():
        return None
    return ParsedCommand(command.lower(), tuple(words[1:]))


class CommandHandler:
    """Handles the commands of one user's authorized chats (spec 025, D5)."""

    def __init__(
        self,
        *,
        owner_id: int,
        unit_of_work: CommandUnitOfWork,
        provider: MarketDataProvider,
        next_fire_times: NextFireTimes,
        production: bool,
        environment: str,
        version: str,
        display_tz: tzinfo,
    ) -> None:
        self._owner_id = owner_id
        self._unit_of_work = unit_of_work
        self._provider = provider
        self._next_fire_times = next_fire_times
        self._production = production
        self._environment = environment
        self._version = version
        self._display_tz = display_tz
        self._logger = logging.getLogger(_LOGGER_NAME)

    async def handle(self, text: str, *, bot_username: str | None = None) -> str | None:
        """The HTML reply to ``text``, or ``None`` when nothing must be sent."""
        stripped = text.strip()
        command = parse(stripped, bot_username=bot_username)
        if command is None:
            if stripped.startswith("/"):
                return None  # addressed to another bot
            return self._reply("Send /help to see what I can do.")
        try:
            body = await self._dispatch(command)
        except Exception as error:
            self._logger.error("/%s failed: %s", command.name, type(error).__name__)
            body = "Something went wrong; nothing was changed. Check the logs."
        return self._reply(body)

    async def _dispatch(self, command: ParsedCommand) -> str:
        name, args = command.name, command.args
        if name in ("start", "help"):
            return HELP_TEXT
        if name == "add":
            return await self._add(args)
        if name == "remove":
            return await self._remove(args)
        if name == "list":
            return await in_command_unit_of_work(self._unit_of_work, _list_tickers)
        if name == "rules":
            return await in_command_unit_of_work(self._unit_of_work, _list_rules)
        if name == "status":
            return await self._status()
        if name == "pause":
            return await in_command_unit_of_work(self._unit_of_work, self._pause)
        if name == "resume":
            return await in_command_unit_of_work(self._unit_of_work, self._resume)
        if name == "link":
            return "Send /link with the code the dashboard shows on its Telegram page."
        return f"Unknown command /{_escape(name)}. Send /help to see the commands."

    # --- /add and /remove ---------------------------------------------------------------------

    async def _add(self, args: tuple[str, ...]) -> str:
        if not 1 <= len(args) <= 2:
            return "Usage: /add TICKER [1h|4h|1d|1w], for example /add AAPL 1d"
        timeframe = _timeframe(args[1]) if len(args) == 2 else _DEFAULT_TIMEFRAME
        if timeframe is None:
            return f"Unknown timeframe {_echo(args[1])}. Use 1h, 4h, 1d or 1w."
        try:
            info = await self._provider.validate_ticker(args[0])
        except InvalidTickerError as error:
            return _invalid_ticker_text(args[0], error.reason)
        except (ProviderUnavailableError, ProviderDataError) as error:
            self._logger.warning("/add could not validate a ticker: %s", type(error).__name__)
            return "The market data provider is not answering. Try again later."

        def work(repositories: CommandRepositories) -> str:
            return _add_ticker(repositories, info.symbol, info.name, timeframe)

        return await in_command_unit_of_work(self._unit_of_work, work)

    async def _remove(self, args: tuple[str, ...]) -> str:
        if not 1 <= len(args) <= 2:
            return "Usage: /remove TICKER [1h|4h|1d|1w]"
        try:
            symbol = normalize_ticker(args[0])
        except ValueError:
            return f"{_echo(args[0])} is not a valid symbol."
        timeframe: Timeframe | None = None
        if len(args) == 2:
            timeframe = _timeframe(args[1])
            if timeframe is None:
                return f"Unknown timeframe {_echo(args[1])}. Use 1h, 4h, 1d or 1w."

        def work(repositories: CommandRepositories) -> str:
            return _disable_ticker(repositories, symbol, timeframe)

        return await in_command_unit_of_work(self._unit_of_work, work)

    # --- /status and /pause ------------------------------------------------------------------

    async def _status(self) -> str:
        def read(repositories: CommandRepositories) -> tuple[str, list[str]]:
            state = repositories.state.load()
            mine = repositories.users.paused_since(self._owner_id)
            enabled = len(repositories.tickers.list_enabled())
            if state.paused_since is not None:
                head = f"Paused for everyone since {self._local(state.paused_since)}"
            elif mine is not None:
                head = f"Your signals are paused since {self._local(mine)}"
            else:
                head = "Running"
            lines = [f"{head} · {enabled} enabled ticker{'s' if enabled != 1 else ''}"]
            for timeframe in Timeframe:
                last = state.last_run(timeframe)
                last_text = self._local(last.scheduled_at) if last else "never"
                lines.append(f"{timeframe.value}: last candle {last_text}")
            return head, lines

        _, lines = await in_command_unit_of_work(self._unit_of_work, read)
        next_fires = dict(self._next_fire_times())
        for index, timeframe in enumerate(Timeframe, start=1):
            when = next_fires.get(timeframe)
            lines[index] += f" · next run {self._local(when) if when else 'not scheduled'}"
        header = f"<b>trading-bot {_escape(self._version)}</b> ({_escape(self._environment)})"
        return "\n".join([header, *lines])

    def _pause(self, repositories: CommandRepositories) -> str:
        before = repositories.users.paused_since(self._owner_id)
        since = repositories.users.pause(self._owner_id)
        if before is not None:
            return f"Already paused since {self._local(since)}."
        return "Paused. Your tickers are not evaluated and no signal is sent until /resume."

    def _resume(self, repositories: CommandRepositories) -> str:
        if repositories.users.resume(self._owner_id):
            return "Resumed. The next candle close will be evaluated."
        return "Your signals were not paused."

    def _local(self, instant: datetime) -> str:
        return instant.astimezone(self._display_tz).strftime("%Y-%m-%d %H:%M %Z")

    def _reply(self, body: str) -> str:
        return body if self._production else f"{BETA_PREFIX} {body}"


def _add_ticker(
    repositories: CommandRepositories, symbol: str, name: str, timeframe: Timeframe
) -> str:
    result = watch_ticker(
        repositories.tickers, repositories.rules, repositories.assignments, symbol, timeframe
    )
    verb = _WATCH_VERBS[result.outcome]
    assigned, enabled = result.assigned_rules, result.enabled_rules
    text = (
        f"{verb} <b>{_escape(result.ticker.symbol)}</b> ({_escape(name)}) on {timeframe.value} "
        f"with {assigned} rule{'s' if assigned != 1 else ''} ({enabled} enabled)."
    )
    if enabled == 0:
        text += f"\nNo enabled {timeframe.value} rule yet: no signal will be sent for it."
    return text


def _disable_ticker(
    repositories: CommandRepositories, symbol: str, timeframe: Timeframe | None
) -> str:
    result = unwatch_ticker(repositories.tickers, symbol, timeframe)
    if not result.matches:
        where = f" on {timeframe.value}" if timeframe else ""
        return f"{_escape(symbol)} is not watched{where}."
    if not result.disabled:
        return f"{_escape(symbol)} was already stopped ({_timeframes(result.matches)})."
    return (
        f"Stopped watching {_escape(symbol)} ({_timeframes(result.disabled)}). "
        "Its signal history is kept; /add brings it back."
    )


def _list_tickers(repositories: CommandRepositories) -> str:
    tickers = repositories.tickers.list_all()
    if not tickers:
        return "No symbol is watched yet. Try /add AAPL"
    lines = ["<b>Watched symbols</b>"]
    for ticker in tickers:
        rules = repositories.assignments.rules_for_ticker(ticker.id, enabled_only=True)
        state = "on" if ticker.enabled else "off"
        lines.append(
            f"{_escape(ticker.symbol)} {ticker.timeframe.value} · {state} · "
            f"{len(rules)} enabled rule{'s' if len(rules) != 1 else ''}"
        )
    return "\n".join(lines)


def _list_rules(repositories: CommandRepositories) -> str:
    rules = repositories.rules.list_all()
    if not rules:
        return "No rule is stored yet. Create one from the dashboard or the management CLI."
    lines = ["<b>Rules</b>"]
    for stored in rules:
        tickers = repositories.assignments.tickers_for_rule(stored.id)
        state = "on" if stored.enabled else "off"
        lines.append(
            f"{_escape(stored.name)} · {stored.rule.timeframe.value} · "
            f"{stored.rule.signal.value} · {state} · "
            f"{len(tickers)} ticker{'s' if len(tickers) != 1 else ''}"
        )
    return "\n".join(lines)


def _timeframe(text: str) -> Timeframe | None:
    try:
        return Timeframe.parse(text.lower())
    except ValueError:
        return None


def _timeframes(tickers: Sequence[StoredTicker]) -> str:
    return ", ".join(ticker.timeframe.value for ticker in tickers)


def _invalid_ticker_text(text: str, reason: InvalidTickerReason) -> str:
    symbol = _echo(text)
    if reason is InvalidTickerReason.MALFORMED:
        return f"{symbol} is not a valid symbol."
    if reason is InvalidTickerReason.NOT_FOUND:
        return f"{symbol} was not found."
    return f"{symbol} is not supported: only US-listed stocks and ETFs are, for now."


def _echo(text: str) -> str:
    """User text, truncated and escaped, safe to put back in an HTML reply."""
    clipped = text if len(text) <= _MAX_ARGUMENT_ECHO else text[:_MAX_ARGUMENT_ECHO] + "…"
    return _escape(clipped)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)
