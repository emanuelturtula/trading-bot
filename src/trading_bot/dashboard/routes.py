"""The dashboard pages and actions (spec 020).

``install_dashboard(app, settings)`` mounts everything under ``/dashboard/``. Pages are rendered
on the server with Jinja2 (autoescaping on), every change is a ``POST`` form followed by a
redirect, and no page carries a script (D1). Access goes through ``_Gate``: a valid session
cookie, and a valid CSRF token on every ``POST`` (AC2, AC6).

The database and the provider come from the running ``Runtime`` (``app.state.runtime``); every
read or write is one unit of work in a worker thread (D7).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trading_bot.config import Settings
from trading_bot.dashboard.auth import (
    MIN_SESSION_SECRET_LENGTH,
    InvalidPasswordHashError,
    LoginThrottle,
    PasswordHash,
    SessionSigner,
    parse_password_hash,
    verify_password,
)
from trading_bot.dashboard.builder import builder_payload, fill
from trading_bot.dashboard.unit_of_work import (
    DashboardRepositories,
    DashboardUnitOfWork,
    in_dashboard_unit_of_work,
    sql_dashboard_unit_of_work,
)
from trading_bot.data.errors import (
    InvalidTickerError,
    InvalidTickerReason,
    ProviderDataError,
    ProviderUnavailableError,
)
from trading_bot.data.provider import MarketDataProvider
from trading_bot.domain.indicators.catalog import REGISTRY
from trading_bot.domain.rules.errors import RuleValidationError
from trading_bot.domain.rules.schema import Operator, PriceField, dump_rule, parse_rule
from trading_bot.domain.signals import normalize_ticker
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.errors import (
    AssignedTimeframeError,
    DuplicateRuleNameError,
    TimeframeMismatchError,
)
from trading_bot.persistence.records import StoredRule, StoredTicker
from trading_bot.watchlist import WatchOutcome, unwatch_ticker, watch_ticker

__all__ = ["PREFIX", "TEMPLATES_DIR", "Dashboard", "install_dashboard"]

PREFIX: Final = "/dashboard"
SESSION_COOKIE: Final = "tb_session"
FLASH_COOKIE: Final = "tb_flash"
MAX_FORM_BYTES: Final = 256 * 1024  # a rule document is capped at 64 KiB before encoding
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_LOGGER_NAME: Final = "trading_bot.dashboard"
TEMPLATES_DIR: Final = Path(__file__).with_name("templates")
STATIC_DIR: Final = Path(__file__).with_name("static")

SECURITY_HEADERS: Final = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

NEW_RULE_TEMPLATE: Final[Mapping[str, object]] = {
    "name": "My new rule",
    "signal": "BUY",
    "timeframe": "1d",
    "conditions": fill("rsi_level", {}),
    "cooldown_bars": 5,
}

_WATCH_VERBS: Final = {
    WatchOutcome.ADDED: "Watching",
    WatchOutcome.ALREADY_WATCHED: "Already watching",
    WatchOutcome.REENABLED: "Watching again",
}


@dataclass(frozen=True, slots=True)
class _Credentials:
    password: PasswordHash
    signer: SessionSigner


def _credentials(settings: Settings, logger: logging.Logger) -> _Credentials | None:
    """The dashboard keys, or ``None`` (with one log line) when it must stay disabled (AC1)."""
    hash_value, secret = settings.dashboard_password_hash, settings.session_secret
    if hash_value is None or secret is None:
        logger.warning(
            "dashboard disabled: TB_DASHBOARD_PASSWORD_HASH and TB_SESSION_SECRET are required"
        )
        return None
    # Fixed messages only: nothing derived from either value reaches a log line (AC18).
    try:
        password = parse_password_hash(hash_value.get_secret_value())
    except InvalidPasswordHashError:
        logger.error(
            "dashboard disabled: TB_DASHBOARD_PASSWORD_HASH is not a valid hash;"
            " generate it with python -m trading_bot.dashboard.password"
        )
        return None
    if len(secret.get_secret_value()) < MIN_SESSION_SECRET_LENGTH:
        logger.error("dashboard disabled: TB_SESSION_SECRET must have at least 32 characters")
        return None
    signer = SessionSigner(secret.get_secret_value(), hash_value.get_secret_value().strip())
    return _Credentials(password=password, signer=signer)


class _FormError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _NotFoundError(Exception):
    pass


type Form = dict[str, list[str]]


async def _read_form(request: Request) -> Form:
    """The url-encoded body, capped at ``MAX_FORM_BYTES`` (D5)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != _FORM_TYPE:
        raise _FormError(415, "Forms must be sent url-encoded.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FORM_BYTES:
            raise _FormError(413, "The form is too large.")
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8")
        return parse_qs(text, keep_blank_values=True, max_num_fields=200, strict_parsing=False)
    except (UnicodeDecodeError, ValueError):
        raise _FormError(400, "The form could not be read.") from None


def _field(form: Form, name: str) -> str:
    values = form.get(name)
    return values[0] if values else ""


class Dashboard:
    """Routes, templates and access control of one application instance."""

    def __init__(self, settings: Settings, credentials: _Credentials | None) -> None:
        self._settings = settings
        self._credentials = credentials
        self._throttle = LoginThrottle()
        self._templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        self._templates.env.globals["prefix"] = PREFIX
        self._templates.env.filters["local"] = self._local
        self._logger = logging.getLogger(_LOGGER_NAME)

    @property
    def templates(self) -> Jinja2Templates:
        return self._templates

    # --- plumbing ---------------------------------------------------------------------------

    def _local(self, instant: datetime | None) -> str:
        if instant is None:
            return "never"
        zone: tzinfo = self._settings.display_zone
        return instant.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")

    def _render(
        self,
        request: Request,
        template: str,
        context: Mapping[str, object] | None = None,
        *,
        status_code: int = 200,
        nonce: str | None = None,
    ) -> HTMLResponse:
        values: dict[str, object] = {
            "environment": self._settings.environment,
            "version": self._settings.version,
            "production": self._settings.environment == "prod",
            "csrf": self._signer.csrf_token(nonce) if nonce and self._credentials else "",
            "signed_in": nonce is not None,
            "flash": _read_flash(request),
            "active": _section(request.url.path),
            "zone": self._settings.display_timezone,
        }
        values.update(context or {})
        response = self._templates.TemplateResponse(
            request, template, values, status_code=status_code
        )
        if values["flash"]:
            response.delete_cookie(FLASH_COOKIE, path=PREFIX)
        return response

    @property
    def _signer(self) -> SessionSigner:
        if self._credentials is None:  # only reached once the gate let the request in
            raise RuntimeError("the dashboard is disabled")
        return self._credentials.signer

    def _unit_of_work(self, request: Request) -> DashboardUnitOfWork:
        unit_of_work: DashboardUnitOfWork | None = getattr(
            request.app.state, "dashboard_unit_of_work", None
        )
        if unit_of_work is None:
            unit_of_work = sql_dashboard_unit_of_work(request.app.state.runtime.database)
            request.app.state.dashboard_unit_of_work = unit_of_work
        return unit_of_work

    async def _work[T](self, request: Request, work: Callable[[DashboardRepositories], T]) -> T:
        return await in_dashboard_unit_of_work(self._unit_of_work(request), work)

    def _provider(self, request: Request) -> MarketDataProvider:
        provider: MarketDataProvider = request.app.state.runtime.provider
        return provider

    def _session(self, request: Request) -> str | None:
        if self._credentials is None:
            return None
        return self._signer.verify(request.cookies.get(SESSION_COOKIE))

    # --- access -----------------------------------------------------------------------------

    def page(
        self, handler: Callable[[Request, str], Awaitable[Response]]
    ) -> Callable[[Request], Awaitable[Response]]:
        """A ``GET`` page: disabled -> 503, no session -> login."""

        async def endpoint(request: Request) -> Response:
            if self._credentials is None:
                return self._disabled(request)
            nonce = self._session(request)
            if nonce is None:
                return _redirect("/login")
            try:
                return await handler(request, nonce)
            except _NotFoundError:
                return self._not_found(request, nonce)

        return endpoint

    def action(
        self, handler: Callable[[Request, str, Form], Awaitable[Response]]
    ) -> Callable[[Request], Awaitable[Response]]:
        """A ``POST`` action: disabled -> 503, no session -> login, bad CSRF token -> 403."""

        async def endpoint(request: Request) -> Response:
            if self._credentials is None:
                return self._disabled(request)
            nonce = self._session(request)
            if nonce is None:
                return _redirect("/login")
            try:
                form = await _read_form(request)
            except _FormError as error:
                return self._error(request, error.status, error.message, nonce)
            if not self._signer.check_csrf(nonce, _field(form, "csrf")):
                return self._error(
                    request, 403, "The form expired. Reload the page and try again.", nonce
                )
            try:
                return await handler(request, nonce, form)
            except _NotFoundError:
                return self._not_found(request, nonce)

        return endpoint

    def _disabled(self, request: Request) -> HTMLResponse:
        return self._render(request, "disabled.html", status_code=503)

    def _not_found(self, request: Request, nonce: str) -> HTMLResponse:
        return self._error(request, 404, "That item does not exist (any more).", nonce)

    def _error(self, request: Request, status: int, message: str, nonce: str) -> HTMLResponse:
        return self._render(
            request, "error.html", {"message": message}, status_code=status, nonce=nonce
        )

    # --- login ------------------------------------------------------------------------------

    async def login_page(self, request: Request) -> Response:
        if self._credentials is None:
            return self._disabled(request)
        if self._session(request) is not None:
            return _redirect("/")
        return self._render(request, "login.html")

    async def login(self, request: Request) -> Response:
        if self._credentials is None:
            return self._disabled(request)
        client = request.client.host if request.client else "unknown"
        if self._throttle.blocked(client):
            self._logger.warning("dashboard login refused: too many failed attempts")
            return self._render(
                request,
                "login.html",
                {"error": "Too many failed attempts. Wait 15 minutes and try again."},
                status_code=429,
            )
        try:
            form = await _read_form(request)
        except _FormError as error:
            return self._render(request, "login.html", {"error": error.message}, status_code=400)
        password = _field(form, "password")
        # scrypt takes tens of milliseconds by design: keep it off the event loop.
        if not await asyncio.to_thread(verify_password, password, self._credentials.password):
            self._throttle.record_failure(client)
            self._logger.warning("dashboard login failed")
            return self._render(
                request, "login.html", {"error": "Wrong password."}, status_code=401
            )
        self._throttle.reset(client)
        self._logger.info("dashboard login")
        response = _redirect("/")
        response.set_cookie(
            SESSION_COOKIE,
            self._signer.issue(),
            max_age=self._signer.lifetime_seconds,
            path=PREFIX,
            httponly=True,
            samesite="strict",
            secure=_is_https(request),
        )
        return response

    async def logout(self, request: Request, nonce: str, form: Form) -> Response:
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path=PREFIX)
        return response

    # --- overview and pause -----------------------------------------------------------------

    async def overview(self, request: Request, nonce: str) -> Response:
        next_runs = dict(_next_fire_times(request))

        def read(repositories: DashboardRepositories) -> dict[str, object]:
            state = repositories.state.load()
            tickers = repositories.tickers.list_all()
            rules = repositories.rules.list_all()
            # What the engine evaluates: enabled tickers with at least one enabled rule.
            active: dict[Timeframe, int] = dict.fromkeys(Timeframe, 0)
            for ticker in tickers:
                if ticker.enabled and repositories.assignments.rules_for_ticker(
                    ticker.id, enabled_only=True
                ):
                    active[ticker.timeframe] += 1
            enabled_rules = [rule for rule in rules if rule.enabled]
            return {
                "paused_since": state.paused_since,
                "tickers_total": len(tickers),
                "tickers_enabled": sum(1 for ticker in tickers if ticker.enabled),
                "rules_total": len(rules),
                "rules_enabled": len(enabled_rules),
                "evaluated": sum(active.values()),
                "timeframes": [
                    {
                        "code": timeframe.value,
                        "evaluated": active[timeframe],
                        "rules": sum(
                            1 for rule in enabled_rules if rule.rule.timeframe is timeframe
                        ),
                        "last": run.scheduled_at if run else None,
                        "next": next_runs.get(timeframe),
                    }
                    for timeframe in Timeframe
                    for run in (state.last_run(timeframe),)
                ],
            }

        context = await self._work(request, read)
        return self._render(request, "overview.html", context, nonce=nonce)

    async def pause(self, request: Request, nonce: str, form: Form) -> Response:
        def work(repositories: DashboardRepositories) -> str:
            before = repositories.state.load().paused_since
            repositories.state.pause()
            if before is not None:
                return "The bot was already paused."
            return "Paused. No candle is evaluated and no signal is sent until you resume."

        return _redirect("/", flash=await self._work(request, work))

    async def resume(self, request: Request, nonce: str, form: Form) -> Response:
        def work(repositories: DashboardRepositories) -> str:
            if repositories.state.resume():
                return "Resumed. The next candle close will be evaluated."
            return "The bot was not paused."

        return _redirect("/", flash=await self._work(request, work))

    # --- tickers ----------------------------------------------------------------------------

    async def tickers_page(self, request: Request, nonce: str) -> Response:
        def read(repositories: DashboardRepositories) -> dict[str, object]:
            rows: list[dict[str, object]] = []
            for ticker in repositories.tickers.list_all():
                assigned = repositories.assignments.rules_for_ticker(ticker.id)
                rows.append(
                    {
                        "ticker": ticker,
                        "assigned": len(assigned),
                        "enabled_rules": sum(1 for rule in assigned if rule.enabled),
                    }
                )
            enabled = repositories.rules.list_enabled()
            # Each option says how many enabled rules a new ticker of that timeframe gets (#3).
            options = [
                {
                    "code": timeframe.value,
                    "rules": sum(1 for rule in enabled if rule.rule.timeframe is timeframe),
                }
                for timeframe in Timeframe
            ]
            return {"rows": rows, "options": options}

        context = await self._work(request, read)
        return self._render(request, "tickers.html", context, nonce=nonce)

    async def add_ticker(self, request: Request, nonce: str, form: Form) -> Response:
        text = _field(form, "symbol").strip()
        timeframe = _timeframe(_field(form, "timeframe"))
        if timeframe is None:
            return _redirect("/tickers", flash="Unknown timeframe. Use 1h, 4h or 1d.", error=True)
        try:
            info = await self._provider(request).validate_ticker(text)
        except InvalidTickerError as error:
            return _redirect("/tickers", flash=_invalid_ticker_text(text, error.reason), error=True)
        except (ProviderUnavailableError, ProviderDataError) as error:
            self._logger.warning("dashboard could not validate a ticker: %s", type(error).__name__)
            return _redirect(
                "/tickers",
                flash="The market data provider is not answering. Try again later.",
                error=True,
            )

        def work(repositories: DashboardRepositories) -> str:
            result = watch_ticker(
                repositories.tickers,
                repositories.rules,
                repositories.assignments,
                info.symbol,
                timeframe,
            )
            assigned, enabled = result.assigned_rules, result.enabled_rules
            message = (
                f"{_WATCH_VERBS[result.outcome]} {result.ticker.symbol} ({info.name}) on "
                f"{timeframe.value} with {_plural(assigned, 'rule')} ({enabled} enabled)."
            )
            if enabled == 0:
                message += f" No enabled {timeframe.value} rule yet: no signal will be sent."
            return message

        return _redirect("/tickers", flash=await self._work(request, work))

    async def ticker_page(self, request: Request, nonce: str) -> Response:
        ticker_id = _path_id(request, "ticker_id")

        def read(repositories: DashboardRepositories) -> dict[str, object]:
            ticker = _ticker(repositories, ticker_id)
            assigned = {rule.id for rule in repositories.assignments.rules_for_ticker(ticker.id)}
            candidates = [
                {"rule": rule, "assigned": rule.id in assigned}
                for rule in repositories.rules.list_all()
                if rule.rule.timeframe is ticker.timeframe
            ]
            return {
                "ticker": ticker,
                "candidates": candidates,
                "signals": repositories.signals.count(ticker_id=ticker.id),
            }

        context = await self._work(request, read)
        return self._render(request, "ticker.html", context, nonce=nonce)

    async def set_ticker_rules(self, request: Request, nonce: str, form: Form) -> Response:
        ticker_id = _path_id(request, "ticker_id")
        wanted = {int(value) for value in form.get("rule", []) if value.isdigit()}

        def work(repositories: DashboardRepositories) -> str:
            ticker = _ticker(repositories, ticker_id)
            current = {rule.id for rule in repositories.assignments.rules_for_ticker(ticker.id)}
            for rule in repositories.rules.list_all():
                if rule.id in wanted and rule.id not in current:
                    repositories.assignments.assign(ticker.id, rule.id)
                elif rule.id in current and rule.id not in wanted:
                    repositories.assignments.unassign(ticker.id, rule.id)
            count = len(repositories.assignments.rules_for_ticker(ticker.id))
            return f"{ticker.symbol} {ticker.timeframe.value} now has {_plural(count, 'rule')}."

        try:
            message = await self._work(request, work)
        except TimeframeMismatchError as error:
            return _redirect(f"/tickers/{ticker_id}", flash=str(error), error=True)
        return _redirect(f"/tickers/{ticker_id}", flash=message)

    async def enable_ticker(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_ticker(request, form, enabled=True)

    async def disable_ticker(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_ticker(request, form, enabled=False)

    async def _switch_ticker(self, request: Request, form: Form, *, enabled: bool) -> Response:
        ticker_id = _path_id(request, "ticker_id")
        back = f"/tickers/{ticker_id}" if _field(form, "back") == "ticker" else "/tickers"

        def work(repositories: DashboardRepositories) -> str:
            ticker = _ticker(repositories, ticker_id)
            subject = f"{ticker.symbol} {ticker.timeframe.value}"
            if enabled:
                if ticker.enabled:
                    return f"Already watching {subject}."
                repositories.tickers.set_enabled(ticker.id, True)
                return f"Watching {subject} again."
            if not unwatch_ticker(repositories.tickers, ticker.symbol, ticker.timeframe).disabled:
                return f"{subject} was already stopped."
            return f"Stopped watching {subject}. Its signal history is kept."

        return _redirect(back, flash=await self._work(request, work))

    async def delete_ticker_page(self, request: Request, nonce: str) -> Response:
        ticker_id = _path_id(request, "ticker_id")

        def read(repositories: DashboardRepositories) -> dict[str, object]:
            ticker = _ticker(repositories, ticker_id)
            return {
                "kind": "ticker",
                "subject": f"{ticker.symbol} {ticker.timeframe.value}",
                "assignments": len(repositories.assignments.rules_for_ticker(ticker.id)),
                "signals": repositories.signals.count(ticker_id=ticker.id),
                "action": f"/tickers/{ticker.id}/delete",
                "back": "/tickers",
            }

        context = await self._work(request, read)
        return self._render(request, "confirm_delete.html", context, nonce=nonce)

    async def delete_ticker(self, request: Request, nonce: str, form: Form) -> Response:
        ticker_id = _path_id(request, "ticker_id")

        def work(repositories: DashboardRepositories) -> str:
            ticker = _ticker(repositories, ticker_id)
            repositories.tickers.delete(ticker.id)  # the cascade takes assignments and signals
            return f"Deleted {ticker.symbol} {ticker.timeframe.value} and its history."

        return _redirect("/tickers", flash=await self._work(request, work))

    # --- rules ------------------------------------------------------------------------------

    async def rules_page(self, request: Request, nonce: str) -> Response:
        def read(repositories: DashboardRepositories) -> list[dict[str, object]]:
            return [
                {"rule": rule, "tickers": len(repositories.assignments.tickers_for_rule(rule.id))}
                for rule in repositories.rules.list_all()
            ]

        rows = await self._work(request, read)
        return self._render(request, "rules.html", {"rows": rows}, nonce=nonce)

    async def new_rule_page(self, request: Request, nonce: str) -> Response:
        document = _pretty(dump_rule(parse_rule(NEW_RULE_TEMPLATE)))
        return self._rule_form(request, nonce, document=document, enabled=False, assign=True)

    async def create_rule(self, request: Request, nonce: str, form: Form) -> Response:
        document = _field(form, "document")
        enabled = _field(form, "enabled") == "on"
        assign = _field(form, "assign") == "on"
        try:
            rule = parse_rule(document)
        except RuleValidationError as error:
            return self._rule_form(
                request, nonce, document=document, enabled=enabled, assign=assign, error=error
            )
        if _field(form, "intent") == "check":
            return self._rule_form(
                request, nonce, document=document, enabled=enabled, assign=assign, checked=True
            )

        def work(repositories: DashboardRepositories) -> str:
            stored = repositories.rules.add(rule, enabled=enabled)
            count = 0
            if assign:
                for ticker in repositories.tickers.list_all():
                    if ticker.timeframe is rule.timeframe:
                        repositories.assignments.assign(ticker.id, stored.id)
                        count += 1
            state = "enabled" if enabled else "disabled"
            return (
                f"Created rule {stored.name} ({state}), "
                f"assigned to {_plural(count, rule.timeframe.value + ' ticker')}."
            )

        try:
            message = await self._work(request, work)
        except DuplicateRuleNameError:
            return self._rule_form(
                request,
                nonce,
                document=document,
                enabled=enabled,
                assign=assign,
                problems=["A rule with that name already exists. Rule names are unique."],
            )
        return _redirect("/rules", flash=message)

    async def edit_rule_page(self, request: Request, nonce: str) -> Response:
        rule_id = _path_id(request, "rule_id")
        stored = await self._work(request, lambda repositories: _rule(repositories, rule_id))
        return self._rule_form(
            request,
            nonce,
            document=_pretty(dump_rule(stored.rule)),
            stored=stored,
            enabled=stored.enabled,
        )

    async def update_rule(self, request: Request, nonce: str, form: Form) -> Response:
        rule_id = _path_id(request, "rule_id")
        stored = await self._work(request, lambda repositories: _rule(repositories, rule_id))
        document = _field(form, "document")
        enabled = _field(form, "enabled") == "on"
        try:
            rule = parse_rule(document)
        except RuleValidationError as error:
            return self._rule_form(
                request, nonce, document=document, stored=stored, enabled=enabled, error=error
            )
        if _field(form, "intent") == "check":
            return self._rule_form(
                request, nonce, document=document, stored=stored, enabled=enabled, checked=True
            )

        def work(repositories: DashboardRepositories) -> StoredRule:
            return repositories.rules.replace(
                _rule(repositories, rule_id).id, rule, enabled=enabled
            )

        problem: str | None = None
        try:
            updated = await self._work(request, work)
        except DuplicateRuleNameError:
            problem = "Another rule already has that name. Rule names are unique."
        except AssignedTimeframeError as error:
            problem = (
                f"The rule is assigned to {_plural(error.assignment_count, 'ticker')} on "
                f"{error.stored.value}, so its timeframe cannot change to "
                f"{error.requested.value}. Unassign it first, or create a new rule."
            )
        if problem is not None:
            return self._rule_form(
                request,
                nonce,
                document=document,
                stored=stored,
                enabled=enabled,
                problems=[problem],
            )
        state = "enabled" if updated.enabled else "disabled"
        return _redirect("/rules", flash=f"Saved rule {updated.name} ({state}).")

    async def enable_rule(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_rule(request, enabled=True)

    async def disable_rule(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_rule(request, enabled=False)

    async def _switch_rule(self, request: Request, *, enabled: bool) -> Response:
        rule_id = _path_id(request, "rule_id")

        def work(repositories: DashboardRepositories) -> str:
            stored = _rule(repositories, rule_id)
            if stored.enabled == enabled:
                return f"Rule {stored.name} was already {'enabled' if enabled else 'disabled'}."
            repositories.rules.set_enabled(stored.id, enabled)
            return f"{'Enabled' if enabled else 'Disabled'} rule {stored.name}."

        return _redirect("/rules", flash=await self._work(request, work))

    async def delete_rule_page(self, request: Request, nonce: str) -> Response:
        rule_id = _path_id(request, "rule_id")

        def read(repositories: DashboardRepositories) -> dict[str, object]:
            stored = _rule(repositories, rule_id)
            return {
                "kind": "rule",
                "subject": stored.name,
                "assignments": len(repositories.assignments.tickers_for_rule(stored.id)),
                "signals": repositories.signals.count(rule_id=stored.id),
                "action": f"/rules/{stored.id}/delete",
                "back": "/rules",
            }

        context = await self._work(request, read)
        return self._render(request, "confirm_delete.html", context, nonce=nonce)

    async def delete_rule(self, request: Request, nonce: str, form: Form) -> Response:
        rule_id = _path_id(request, "rule_id")

        def work(repositories: DashboardRepositories) -> str:
            stored = _rule(repositories, rule_id)
            repositories.rules.delete(stored.id)  # the cascade takes assignments and signals
            return f"Deleted rule {stored.name} and its history."

        return _redirect("/rules", flash=await self._work(request, work))

    def _rule_form(
        self,
        request: Request,
        nonce: str,
        *,
        document: str,
        stored: StoredRule | None = None,
        enabled: bool = False,
        assign: bool = False,
        error: RuleValidationError | None = None,
        problems: list[str] | None = None,
        checked: bool = False,
    ) -> HTMLResponse:
        listed = list(problems or [])
        if error is not None:
            listed += [
                f"{problem.path or 'document'}: {problem.message}" for problem in error.problems
            ]
        context = {
            "document": document,
            "stored": stored,
            "enabled": enabled,
            "assign": assign,
            "problems": listed,
            "checked": checked,
            "catalog": REGISTRY.describe(),
            "operators": [operator.value for operator in Operator],
            "prices": [price.value for price in PriceField],
            "timeframes": [timeframe.value for timeframe in Timeframe],
            "builder": builder_payload(),
        }
        return self._render(
            request, "rule_form.html", context, status_code=422 if listed else 200, nonce=nonce
        )


def install_dashboard(app: FastAPI, settings: Settings) -> None:
    """Mount the dashboard routes and its security headers on ``app``."""
    dashboard = Dashboard(settings, _credentials(settings, logging.getLogger(_LOGGER_NAME)))
    pages: list[tuple[str, Callable[[Request, str], Awaitable[Response]]]] = [
        ("/", dashboard.overview),
        ("/tickers", dashboard.tickers_page),
        ("/tickers/{ticker_id:int}", dashboard.ticker_page),
        ("/tickers/{ticker_id:int}/delete", dashboard.delete_ticker_page),
        ("/rules", dashboard.rules_page),
        ("/rules/new", dashboard.new_rule_page),
        ("/rules/{rule_id:int}", dashboard.edit_rule_page),
        ("/rules/{rule_id:int}/delete", dashboard.delete_rule_page),
    ]
    actions: list[tuple[str, Callable[[Request, str, Form], Awaitable[Response]]]] = [
        ("/logout", dashboard.logout),
        ("/pause", dashboard.pause),
        ("/resume", dashboard.resume),
        ("/tickers", dashboard.add_ticker),
        ("/tickers/{ticker_id:int}/rules", dashboard.set_ticker_rules),
        ("/tickers/{ticker_id:int}/enable", dashboard.enable_ticker),
        ("/tickers/{ticker_id:int}/disable", dashboard.disable_ticker),
        ("/tickers/{ticker_id:int}/delete", dashboard.delete_ticker),
        ("/rules", dashboard.create_rule),
        ("/rules/{rule_id:int}", dashboard.update_rule),
        ("/rules/{rule_id:int}/enable", dashboard.enable_rule),
        ("/rules/{rule_id:int}/disable", dashboard.disable_rule),
        ("/rules/{rule_id:int}/delete", dashboard.delete_rule),
    ]
    # Scripts of the pages (spec 026): public files shipped in the package, no secrets.
    app.mount(PREFIX + "/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
    app.add_api_route(PREFIX + "/login", dashboard.login_page, methods=["GET"])
    app.add_api_route(PREFIX + "/login", dashboard.login, methods=["POST"])
    for path, page in pages:
        app.add_api_route(PREFIX + path, dashboard.page(page), methods=["GET"])
    for path, action in actions:
        app.add_api_route(PREFIX + path, dashboard.action(action), methods=["POST"])

    async def root() -> RedirectResponse:
        return RedirectResponse(PREFIX + "/", status_code=303)

    app.add_api_route("/", root, methods=["GET"], include_in_schema=False)
    app.add_api_route(PREFIX, root, methods=["GET"], include_in_schema=False)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(PREFIX):
            response.headers.update(SECURITY_HEADERS)
        return response


# --- helpers ---------------------------------------------------------------------------------


def _is_https(request: Request) -> bool:
    """Whether the browser reached the dashboard over HTTPS (spec 021).

    Behind a tunnel that terminates TLS (Cloudflare), the app itself sees plain HTTP, but the
    browser still sends the scheme it used in the ``Origin`` header of every ``POST``. An
    ``Origin`` that is absent, ``null`` or not HTTP(S) falls back to the request URL.
    """
    scheme = urlsplit(request.headers.get("origin", "")).scheme.casefold()
    if scheme not in ("http", "https"):
        scheme = request.url.scheme
    return scheme == "https"


def _redirect(path: str, *, flash: str | None = None, error: bool = False) -> RedirectResponse:
    response = RedirectResponse(PREFIX + path, status_code=303)
    if flash:
        level = "error" if error else "ok"
        response.set_cookie(
            FLASH_COOKIE,
            quote(f"{level}|{flash[:400]}"),
            max_age=60,
            path=PREFIX,
            httponly=True,
            samesite="strict",
        )
    return response


def _read_flash(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    level, _, text = unquote(raw)[:450].partition("|")
    if level not in ("ok", "error") or not text:
        return None
    return {"level": level, "text": text}


def _section(path: str) -> str:
    """The navigation entry a page belongs to, for ``aria-current`` (#12)."""
    for section in ("tickers", "rules"):
        if path.startswith(f"{PREFIX}/{section}"):
            return section
    return "overview" if path == PREFIX + "/" else ""


def _next_fire_times(request: Request) -> Sequence[tuple[Timeframe, datetime | None]]:
    try:
        return request.app.state.runtime.scheduler.next_fire_times()  # type: ignore[no-any-return]
    except (AttributeError, RuntimeError):  # the runtime is not started
        return ()


def _path_id(request: Request, name: str) -> int:
    value = request.path_params[name]
    return value if isinstance(value, int) else int(value)


def _ticker(repositories: DashboardRepositories, ticker_id: int) -> StoredTicker:
    ticker = repositories.tickers.get(ticker_id)
    if ticker is None:
        raise _NotFoundError
    return ticker


def _rule(repositories: DashboardRepositories, rule_id: int) -> StoredRule:
    stored = repositories.rules.get(rule_id)
    if stored is None:
        raise _NotFoundError
    return stored


def _timeframe(text: str) -> Timeframe | None:
    try:
        return Timeframe.parse(text.lower())
    except ValueError:
        return None


def _invalid_ticker_text(text: str, reason: InvalidTickerReason) -> str:
    try:
        symbol = normalize_ticker(text)
    except ValueError:
        return "That is not a valid symbol."
    if reason is InvalidTickerReason.MALFORMED:
        return f"{symbol} is not a valid symbol."
    if reason is InvalidTickerReason.NOT_FOUND:
        return f"{symbol} was not found."
    return f"{symbol} is not supported: only US-listed stocks and ETFs are, for now."


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _pretty(document: object) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False)
