# 027 — Ticker chart and signal history

- **Status:** approved (the user asked for a TradingView-like chart per ticker with BUY/SELL
  markers; review items 24 and 27)
- **Branch:** `feature/ticker-chart`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Make it easy to see why the bot suggests buying or selling, and to calibrate rules. Each ticker's page
shows a candlestick chart with the indicators of its rules and a marker for every signal. A history
page lists the user's signals.

## Out of scope

- Telegram and the notifier (multi-user PR 3 owns them).
- Drawing tools, zoom presets, or picking indicators by hand: the chart shows what the rules use.
- A full backtest with results (item 25).

## Acceptance criteria

- [ ] **AC1:** a ticker's page shows a candlestick chart of its last closed candles: 250 for `1d`,
  300 for `4h` and `1h`. Each indicator that one of the ticker's assigned rules reads is drawn:
  - moving averages and Bollinger bands over the price;
  - every other indicator in its own pane below, with the levels the rules compare it to (e.g. RSI
    30) as horizontal lines.
- [ ] **AC2:** every signal recorded for the ticker in that window is a marker on its candle: a green
  "BUY" arrow below or a red "SELL" arrow above, labeled with the rule.
- [ ] **AC3:** the chart also marks, in a muted style, the candles where an assigned rule *would
  have* fired in the window (its conditions held and its cooldown had passed), so a rule can be
  calibrated before it is enabled. A candle that already has a recorded signal of that rule shows
  only the recorded one. A legend explains both styles.
- [ ] **AC4:** the ticker's page lists its last 20 signals (time, rule, side, close, whether Telegram
  was notified).
- [ ] **AC5:** a "Signals" page lists the user's signals, newest first, 50 per page with "Older"
  links, and can be filtered by ticker.
- [ ] **AC6:** the data comes from `GET /dashboard/tickers/{id}/chart.json`, which needs a session
  and shows only the signed-in user's ticker. When the market data cannot be read, the page says
  so and the rest of it works.
- [ ] **AC7:** the chart library is TradingView Lightweight Charts 5.2.1 (Apache-2.0), vendored
  under `dashboard/static/vendor/` with its license; the npm tarball's SHA-512 matches the
  registry's integrity value. The CSP allows `script-src 'self'` and `connect-src 'self'` only.
- [ ] **AC8:** times follow `TB_DISPLAY_TIMEZONE`; daily candles are labeled with their trading day.
- [ ] **AC9:** `scripts/check.py` is green.

## Design

- `dashboard/chart.py` builds the payload from a candle frame, the assigned rules and the recorded
  signals, with no I/O. It reads indicator series with `REGISTRY.compute` and simulated firings with
  `evaluate_each`. It walks the cooldown over the window only, so a firing just before the window is
  not counted.
- The route reads the rules and signals in one unit of work. It fetches
  `shown + max(stable_warmup)` candles (capped at `MAX_LOOKBACK`) from the runtime's provider at the
  current time. If the last candle is not published yet, it retries once with one candle less.
- `dashboard/static/chart.js` fetches the JSON, draws it with Lightweight Charts and writes the
  legend with `textContent` only.
- Signal times map to candles by `label = candle_close_ts - timeframe.duration`.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| Payload: candles, overlays per pane, levels, sent and simulated markers, cooldown, de-duplication | unit | AC1–AC3 |
| Daily and intraday times | unit | AC8 |
| Chart JSON needs a session, is per user, and degrades on provider errors | unit | AC6 |
| Ticker page signal list; signals page, paging, filter | unit | AC4, AC5 |
| CSP and vendored file hash | unit | AC7 |
| The chart renders with markers and panes, with no console errors | manual (Playwright) | AC1–AC3 |

## Risks and security

- The chart reads market data on page load through the same provider and cache as the engine; it
  is one request per chart view.
- The vendored file is third-party code: it is pinned by version and hash, has no network access of
  its own and uses no `eval`.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
