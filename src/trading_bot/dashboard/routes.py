"""The dashboard pages and actions (spec 020).

``install_dashboard(app, settings)`` mounts everything under ``/dashboard/``. Pages are rendered
on the server with Jinja2 (autoescaping on), every change is a ``POST`` form followed by a
redirect, and no page carries a script (D1). Access goes through ``page`` and ``action``: a valid
session cookie of an active user, and a valid CSRF token on every ``POST`` (AC2, AC6). Spec 023
adds the accounts: a login per user, access links instead of a shared password, and the user
pages only an administrator reaches.

The database and the provider come from the running ``Runtime`` (``app.state.runtime``); every
read or write is one unit of work in a worker thread (D7).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, quote, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trading_bot.config import Settings, is_trusted_proxy
from trading_bot.dashboard.auth import (
    MIN_SESSION_SECRET_LENGTH,
    InvalidPasswordHashError,
    LoginThrottle,
    PasswordHash,
    SessionSigner,
    hash_password,
    link_digest,
    new_link_token,
    parse_password_hash,
    verify_password,
)
from trading_bot.dashboard.builder import builder_payload, fill
from trading_bot.dashboard.password import MIN_LENGTH as MIN_PASSWORD_LENGTH
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
    DuplicateUsernameError,
    TimeframeMismatchError,
)
from trading_bot.persistence.records import StoredRule, StoredTicker, StoredUser
from trading_bot.persistence.repositories.users import normalize_username
from trading_bot.watchlist import WatchOutcome, unwatch_ticker, watch_ticker

__all__ = ["PREFIX", "TEMPLATES_DIR", "Dashboard", "install_dashboard"]

PREFIX: Final = "/dashboard"
SESSION_COOKIE: Final = "tb_session"
FLASH_COOKIE: Final = "tb_flash"
MAX_FORM_BYTES: Final = 256 * 1024  # a rule document is capped at 64 KiB before encoding
MAX_PASSWORD_LENGTH: Final = 1024  # characters; bounds the work one form can ask scrypt for
# How long an access link stays valid (spec 023, D3).
LINK_LIFETIME: Final = timedelta(hours=72)
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
    signer: SessionSigner
    # The first administrator's password hash, used only while the users table is empty (D2).
    bootstrap_hash: str | None
    admin_username: str


def _credentials(settings: Settings, logger: logging.Logger) -> _Credentials | None:
    """The dashboard keys, or ``None`` (with one log line) when it must stay disabled (AC1)."""
    secret = settings.session_secret
    if secret is None:
        logger.warning("dashboard disabled: TB_SESSION_SECRET is required")
        return None
    # Fixed messages only: nothing derived from either value reaches a log line (AC18).
    if len(secret.get_secret_value()) < MIN_SESSION_SECRET_LENGTH:
        logger.error("dashboard disabled: TB_SESSION_SECRET must have at least 32 characters")
        return None
    bootstrap: str | None = None
    hash_value = settings.dashboard_password_hash
    if hash_value is not None:
        try:
            parse_password_hash(hash_value.get_secret_value())
        except InvalidPasswordHashError:
            logger.error(
                "TB_DASHBOARD_PASSWORD_HASH is not a valid hash and is ignored;"
                " generate it with python -m trading_bot.dashboard.password"
            )
        else:
            bootstrap = hash_value.get_secret_value().strip()
    return _Credentials(
        signer=SessionSigner(secret.get_secret_value()),
        bootstrap_hash=bootstrap,
        admin_username=settings.admin_username,
    )


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
        # Per account as well (spec 023, D6): behind a proxy without ``TB_CLIENT_IP_HEADER``
        # every browser shares one address, and a botnet has many.
        self._account_throttle = LoginThrottle(limit=10)
        # Verified against when the username is unknown, so a miss costs what a hit costs.
        self._decoy: PasswordHash | None = None
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
            "user": getattr(request.state, "user", None) if nonce is not None else None,
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
        # Signed in: that user's configuration only (spec 024). Before the gate ran, only the
        # users repository is used, which has no owner.
        user: StoredUser | None = getattr(request.state, "user", None)
        owner = None if user is None else user.id
        return await in_dashboard_unit_of_work(self._unit_of_work(request), work, owner=owner)

    def _provider(self, request: Request) -> MarketDataProvider:
        provider: MarketDataProvider = request.app.state.runtime.provider
        return provider

    async def _session(self, request: Request) -> str | None:
        """The session nonce of an active user, who is stored in ``request.state.user``."""
        if self._credentials is None:
            return None
        token = request.cookies.get(SESSION_COOKIE)
        user_id = SessionSigner.user_id(token)
        if user_id is None:
            return None
        user = await self._work(request, lambda repositories: repositories.users.get(user_id))
        if user is None or not user.can_log_in or user.password_hash is None:
            return None
        nonce = self._signer.verify(token, user.password_hash)
        if nonce is not None:
            request.state.user = user
        return nonce

    def page(
        self, handler: Callable[[Request, str], Awaitable[Response]], *, admin: bool = False
    ) -> Callable[[Request], Awaitable[Response]]:
        """A ``GET`` page: disabled -> 503, no session -> login, not an admin -> 403."""

        async def endpoint(request: Request) -> Response:
            if self._credentials is None:
                return self._disabled(request)
            nonce = await self._session(request)
            if nonce is None:
                return _redirect("/login")
            if admin and not _user(request).is_admin:
                return self._forbidden(request, nonce)
            try:
                return await handler(request, nonce)
            except _NotFoundError:
                return self._not_found(request, nonce)

        return endpoint

    def action(
        self, handler: Callable[[Request, str, Form], Awaitable[Response]], *, admin: bool = False
    ) -> Callable[[Request], Awaitable[Response]]:
        """A ``POST`` action: disabled -> 503, no session -> login, bad CSRF token or not an
        admin -> 403."""

        async def endpoint(request: Request) -> Response:
            if self._credentials is None:
                return self._disabled(request)
            nonce = await self._session(request)
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
            if admin and not _user(request).is_admin:
                return self._forbidden(request, nonce)
            try:
                return await handler(request, nonce, form)
            except _NotFoundError:
                return self._not_found(request, nonce)

        return endpoint

    def _forbidden(self, request: Request, nonce: str) -> HTMLResponse:
        return self._error(request, 403, "Only an administrator can do that.", nonce)

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
        if await self._session(request) is not None:
            return _redirect("/")
        if not await self._has_users(request):
            return self._disabled(request)
        return self._render(request, "login.html")

    async def _has_users(self, request: Request) -> bool:
        """Whether someone can log in: a user exists, or the first one can be created (D2)."""
        if self._credentials is not None and self._credentials.bootstrap_hash is not None:
            return True
        return await self._work(request, _reachable)

    async def login(self, request: Request) -> Response:
        credentials = self._credentials
        if credentials is None:
            return self._disabled(request)
        client = self._client(request)
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
        username = _field(form, "username").strip().lower()[:64]
        password = _field(form, "password")[:MAX_PASSWORD_LENGTH]
        if self._account_throttle.blocked(username):
            self._throttle.record_failure(client)
            self._logger.warning("dashboard login refused: too many failed attempts on an account")
            return self._render(
                request,
                "login.html",
                {"error": "Too many failed attempts. Wait 15 minutes and try again."},
                status_code=429,
            )

        def read(repositories: DashboardRepositories) -> tuple[StoredUser | None, bool]:
            self._bootstrap(repositories, credentials)
            return repositories.users.get_by_username(username), _reachable(repositories)

        user, reachable = await self._work(request, read)
        if not reachable:
            return self._disabled(request)
        stored = _stored_hash(user)
        # scrypt takes tens of milliseconds by design: keep it off the event loop.
        matches = await asyncio.to_thread(verify_password, password, stored or self._decoy_hash())
        if user is None or stored is None or not matches:
            self._throttle.record_failure(client)
            self._account_throttle.record_failure(username)
            self._logger.warning("dashboard login failed")
            return self._render(
                request,
                "login.html",
                {"error": "Wrong username or password.", "username": username},
                status_code=401,
            )
        self._throttle.reset(client)
        self._account_throttle.reset(username)
        self._logger.info("dashboard login: user %d", user.id)
        return self._signed_in(request, _redirect("/"), user)

    async def logout(self, request: Request, nonce: str, form: Form) -> Response:
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path=PREFIX)
        return response

    def _client(self, request: Request) -> str:
        """The browser's address (D6): the configured proxy header when the TCP peer is a
        trusted proxy, else the TCP peer itself."""
        peer = request.client.host if request.client else "unknown"
        header = self._settings.client_ip_header
        if header and is_trusted_proxy(peer, self._settings.trusted_proxy_networks):
            value = request.headers.get(header, "").split(",")[0].strip()
            if value:
                return value[:64]
        return peer

    def _bootstrap(self, repositories: DashboardRepositories, credentials: _Credentials) -> None:
        """Give the first administrator ``TB_DASHBOARD_PASSWORD_HASH`` (spec 023, D2).

        Only while nobody can get in: no user has a password and no access link is pending.
        Migration ``0005`` creates that administrator, without a password, as the owner of the
        existing configuration; an empty table (never migrated past ``0004``) gets a new one.
        """
        if credentials.bootstrap_hash is None or _reachable(repositories):
            return
        users = repositories.users
        admins = [user for user in users.list_all() if user.is_admin]
        if not admins:
            created = users.add(
                credentials.admin_username, is_admin=True, password_hash=credentials.bootstrap_hash
            )
            self._logger.info("dashboard: created the first administrator, user %d", created.id)
            return
        first = min(admins, key=lambda user: user.id)
        if first.username != credentials.admin_username and (
            users.get_by_username(credentials.admin_username) is None
        ):
            users.rename(first.id, credentials.admin_username)
        users.set_active(first.id, True)
        users.set_password(first.id, credentials.bootstrap_hash)
        self._logger.info("dashboard: set the first administrator's password, user %d", first.id)

    def _decoy_hash(self) -> PasswordHash:
        if self._decoy is None:
            self._decoy = parse_password_hash(hash_password(new_link_token()[0]))
        return self._decoy

    def _signed_in(self, request: Request, response: Response, user: StoredUser) -> Response:
        """``response`` with a fresh session cookie for ``user``."""
        if user.password_hash is None:  # pragma: no cover - callers pass a user who can log in
            raise RuntimeError("the user has no password")
        response.set_cookie(
            SESSION_COOKIE,
            self._signer.issue(user.id, user.password_hash),
            max_age=self._signer.lifetime_seconds,
            path=PREFIX,
            httponly=True,
            samesite="strict",
            secure=_is_https(request),
        )
        return response

    # --- access links and the account (spec 023) ---------------------------------------------

    async def invite_page(self, request: Request) -> Response:
        if self._credentials is None:
            return self._disabled(request)
        client = self._client(request)
        if self._throttle.blocked(client):
            return self._render(request, "invite.html", {"blocked": True}, status_code=429)
        token = str(request.path_params["token"])
        user = await self._work(
            request, lambda repositories: repositories.users.invited(link_digest(token))
        )
        if user is None:
            self._throttle.record_failure(client)
            return self._render(request, "invite.html", {"invalid": True}, status_code=404)
        return self._render(request, "invite.html", {"username": user.username, "token": token})

    async def accept_invite(self, request: Request) -> Response:
        if self._credentials is None:
            return self._disabled(request)
        client = self._client(request)
        if self._throttle.blocked(client):
            return self._render(request, "invite.html", {"blocked": True}, status_code=429)
        token = str(request.path_params["token"])
        digest = link_digest(token)
        try:
            form = await _read_form(request)
        except _FormError as error:
            return self._render(request, "error.html", {"message": error.message}, status_code=400)
        user = await self._work(request, lambda repositories: repositories.users.invited(digest))
        if user is None:
            self._throttle.record_failure(client)
            return self._render(request, "invite.html", {"invalid": True}, status_code=404)
        problem = _password_problem(_field(form, "password"), _field(form, "repeat"))
        if problem is not None:
            context = {"username": user.username, "token": token, "error": problem}
            return self._render(request, "invite.html", context, status_code=422)
        new_hash = await asyncio.to_thread(hash_password, _field(form, "password"))
        redeemed = await self._work(
            request, lambda repositories: repositories.users.redeem_invite(digest, new_hash)
        )
        if redeemed is None:  # the link was used or revoked in between
            return self._render(request, "invite.html", {"invalid": True}, status_code=404)
        self._logger.info("dashboard: user %d set a password with an access link", redeemed.id)
        welcome = f"Welcome, {redeemed.username}. Your password is set."
        return self._signed_in(request, _redirect("/", flash=welcome), redeemed)

    async def account_page(self, request: Request, nonce: str) -> Response:
        return self._render(request, "account.html", {}, nonce=nonce)

    async def change_password(self, request: Request, nonce: str, form: Form) -> Response:
        user = _user(request)
        stored = _stored_hash(user)
        current = _field(form, "current")[:MAX_PASSWORD_LENGTH]
        if stored is None or not await asyncio.to_thread(verify_password, current, stored):
            return self._render(
                request,
                "account.html",
                {"error": "The current password is wrong."},
                status_code=422,
                nonce=nonce,
            )
        problem = _password_problem(_field(form, "password"), _field(form, "repeat"))
        if problem is not None:
            return self._render(
                request, "account.html", {"error": problem}, status_code=422, nonce=nonce
            )
        new_hash = await asyncio.to_thread(hash_password, _field(form, "password"))
        updated = await self._work(
            request, lambda repositories: repositories.users.set_password(user.id, new_hash)
        )
        # The old cookie is bound to the old hash: issue a new one so this browser stays in.
        response = _redirect("/account", flash="Password changed. Other sessions are logged out.")
        return self._signed_in(request, response, updated)

    # --- users (administrators only, spec 023) -----------------------------------------------

    async def users_page(self, request: Request, nonce: str) -> Response:
        users = await self._work(request, lambda repositories: repositories.users.list_all())
        return self._render(request, "users.html", {"users": users}, nonce=nonce)

    async def invite_user(self, request: Request, nonce: str, form: Form) -> Response:
        try:
            username = normalize_username(_field(form, "username"))
        except ValueError as error:
            return await self._users_error(request, nonce, str(error))
        is_admin = _field(form, "is_admin") == "on"
        token, digest = new_link_token()

        def work(repositories: DashboardRepositories) -> StoredUser:
            user = repositories.users.add(username, is_admin=is_admin)
            return repositories.users.issue_invite(user.id, digest, LINK_LIFETIME)

        try:
            user = await self._work(request, work)
        except DuplicateUsernameError:
            return await self._users_error(request, nonce, f"{username} already exists.")
        self._logger.info("dashboard: user %d invited by user %d", user.id, _user(request).id)
        return await self._link_page(request, nonce, user, token)

    async def new_link(self, request: Request, nonce: str, form: Form) -> Response:
        user_id = _path_id(request, "user_id")
        if user_id == _user(request).id:
            return await self._users_error(
                request, nonce, "Change your own password from Account instead."
            )
        token, digest = new_link_token()

        def work(repositories: DashboardRepositories) -> StoredUser:
            if repositories.users.get(user_id) is None:
                raise _NotFoundError
            return repositories.users.issue_invite(user_id, digest, LINK_LIFETIME)

        user = await self._work(request, work)
        self._logger.info("dashboard: new access link for user %d", user.id)
        return await self._link_page(request, nonce, user, token)

    async def enable_user(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_user(request, nonce, active=True)

    async def disable_user(self, request: Request, nonce: str, form: Form) -> Response:
        return await self._switch_user(request, nonce, active=False)

    async def _switch_user(self, request: Request, nonce: str, *, active: bool) -> Response:
        user_id = _path_id(request, "user_id")
        if user_id == _user(request).id and not active:
            return await self._users_error(request, nonce, "You cannot disable your own account.")

        def work(repositories: DashboardRepositories) -> str:
            user = repositories.users.get(user_id)
            if user is None:
                raise _NotFoundError
            repositories.users.set_active(user_id, active)
            if active:
                return f"{user.username} is enabled."
            return f"{user.username} is disabled and can no longer log in."

        return _redirect("/users", flash=await self._work(request, work))

    async def _link_page(
        self, request: Request, nonce: str, user: StoredUser, token: str
    ) -> Response:
        link = _origin(request) + f"{PREFIX}/invite/{token}"
        context = {
            "invited": user,
            "link": link,
            "hours": int(LINK_LIFETIME.total_seconds() // 3600),
        }
        return self._render(request, "user_link.html", context, nonce=nonce)

    async def _users_error(self, request: Request, nonce: str, message: str) -> Response:
        users = await self._work(request, lambda repositories: repositories.users.list_all())
        context = {"users": users, "error": message}
        return self._render(request, "users.html", context, status_code=422, nonce=nonce)

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
        ("/account", dashboard.account_page),
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
        ("/account/password", dashboard.change_password),
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
    app.add_api_route(PREFIX + "/invite/{token}", dashboard.invite_page, methods=["GET"])
    app.add_api_route(PREFIX + "/invite/{token}", dashboard.accept_invite, methods=["POST"])
    for path, page in pages:
        app.add_api_route(PREFIX + path, dashboard.page(page), methods=["GET"])
    for path, action in actions:
        app.add_api_route(PREFIX + path, dashboard.action(action), methods=["POST"])
    app.add_api_route(
        PREFIX + "/users", dashboard.page(dashboard.users_page, admin=True), methods=["GET"]
    )
    admin_actions: list[tuple[str, Callable[[Request, str, Form], Awaitable[Response]]]] = [
        # The pause still stops the whole bot, so it is an administrator's switch (spec 024).
        ("/pause", dashboard.pause),
        ("/resume", dashboard.resume),
        ("/users", dashboard.invite_user),
        ("/users/{user_id:int}/link", dashboard.new_link),
        ("/users/{user_id:int}/enable", dashboard.enable_user),
        ("/users/{user_id:int}/disable", dashboard.disable_user),
    ]
    for path, action in admin_actions:
        app.add_api_route(PREFIX + path, dashboard.action(action, admin=True), methods=["POST"])

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


def _reachable(repositories: DashboardRepositories) -> bool:
    """Whether anyone can get in: a user who can log in, or a pending access link."""
    now = datetime.now(UTC)
    return any(
        user.can_log_in
        or (user.active and user.invite_expires_at is not None and user.invite_expires_at > now)
        for user in repositories.users.list_all()
    )


def _origin(request: Request) -> str:
    """Where the browser reached the dashboard: the ``Origin`` of the ``POST`` when it is a
    plain HTTP(S) origin (a tunnel that terminates TLS hides the real scheme and host from the
    app), else the request's own base URL."""
    origin = request.headers.get("origin", "").strip()
    parts = urlsplit(origin)
    if parts.scheme in ("http", "https") and parts.netloc and not parts.path and len(origin) < 256:
        return f"{parts.scheme}://{parts.netloc}"
    return str(request.base_url).rstrip("/")


def _user(request: Request) -> StoredUser:
    """The signed-in user the gate stored on the request."""
    user: StoredUser = request.state.user
    return user


def _stored_hash(user: StoredUser | None) -> PasswordHash | None:
    """The parsed hash of a user who may log in, or ``None``."""
    if user is None or not user.can_log_in or user.password_hash is None:
        return None
    try:
        return parse_password_hash(user.password_hash)
    except InvalidPasswordHashError:
        return None


def _password_problem(password: str, repeat: str) -> str | None:
    """Why a new password is refused, or ``None`` (spec 023, D3)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if password != repeat:
        return "The two passwords do not match."
    return None


def _section(path: str) -> str:
    """The navigation entry a page belongs to, for ``aria-current`` (#12)."""
    for section in ("tickers", "rules", "users", "account"):
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
