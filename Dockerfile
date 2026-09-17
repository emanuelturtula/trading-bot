# syntax=docker/dockerfile:1
# Public image: it must never contain secrets. Runtime secrets come from env_file on the host.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
ARG APP_VERSION=0.0.0-dev
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TB_VERSION=${APP_VERSION}
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
# Fail the build if TA-Lib cannot load or compute on this platform (linux/arm64 in CI).
RUN ["python", "-c", "import numpy as np, talib, trading_bot.domain.indicators.catalog; raise SystemExit(0 if talib.SMA(np.arange(1.0, 6.0), timeperiod=5)[-1] == 3.0 else 1)"]
# Fail the build if the NYSE calendar cannot be built on this platform (library and zone data).
RUN ["python", "-c", "from datetime import UTC, date, datetime; from trading_bot.domain.market_calendar.nyse import build_nyse_calendar; s = build_nyse_calendar(date(2024, 7, 1), date(2024, 7, 31)).session_bounds(date(2024, 7, 3)); raise SystemExit(0 if s is not None and s.close_time == datetime(2024, 7, 3, 17, 0, tzinfo=UTC) else 1)"]
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"]
# Exactly one worker: the bot owns a scheduler and a Telegram long-polling session.
CMD ["uvicorn", "trading_bot.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
