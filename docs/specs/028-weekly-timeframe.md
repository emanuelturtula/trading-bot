# 028 — Weekly timeframe

- **Status:** approved (the user asked for a weekly option next to 1h, 4h and 1d; defaults below)
- **Branch:** `feature/weekly-timeframe-djduwp`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

The user wants to evaluate rules on weekly candles, not only on `1h`, `4h` and `1d`. This adds a
`1w` timeframe that works everywhere the other three do: rules, tickers, the scheduler, Telegram
`/add` and `/remove`, the dashboard selectors and the rule builder.

## Out of scope

- Removing or changing the existing timeframes.
- Monthly or other timeframes.
- Using Yahoo's own `1wk` bars (see D2).

## Acceptance criteria

- [ ] **AC1:** `Timeframe.parse("1w")` returns the new `Timeframe.W1`; every place that lists the
  timeframes (Telegram help and errors, dashboard selectors, rule builder, CLI choices) offers `1w`.
- [ ] **AC2:** a `1w` candle is one ISO week of NYSE sessions (Monday to Sunday in New York time).
  Its label is 00:00 New York of the week's first session day (the `1d` label of that session), it
  opens with that session and closes with the week's last session. A holiday week just has fewer
  sessions; a week without sessions has no candle.
- [ ] **AC3:** the calendar answers `next_candle_close`, `candle_slot`, `candle_slots` and
  `closed_candles` for `1w`, so the scheduler fires once a week after the last session closes
  (usually Friday 16:00 New York plus the close delay) and only closed weeks are evaluated.
- [ ] **AC4:** the Yahoo provider builds `1w` candles from daily bars: open of the first day, highest
  high, lowest low, close of the last day, summed volume. The last closed week is withheld (the run
  is retried, like any unpublished candle) while the daily bar of its last session is missing.
  Daily history is not capped, so long lookbacks work.
- [ ] **AC5:** a migration lets the database store `1w` tickers, rules, signals and the
  `last_run.1w` state; the downgrade deletes the `1w` rows and restores the old constraints.
- [ ] **AC6:** a `1w` signal message shows the candle as `week of YYYY-MM-DD` (the first session day).
- [ ] **AC7:** anti look-ahead: resampling weeks at `t` with the daily data cut at `t` gives the same
  result as with later rows present.

## Design

- **D1, labels.** The week label is the `1d` label of its first session, not Monday 00:00: it is
  always inside the calendar span and a session label, so `normalize_candles`, `drop_open_candle`,
  the cooldown and the signal identity work unchanged. `Timeframe.W1.duration` is seven days, so
  `nominal_close` (the signal identifier, spec 004) is unique per week.
- **D2, source data.** Weeks are resampled from Yahoo daily bars on the calendar grid, like `4h`
  is resampled from hourly bars (spec 011). Yahoo's `1wk` bars are labelled on Monday even when
  Monday is a holiday and include the week in progress, which would need special cases.
- `domain/timeframe.py`: `W1 = "1w"`, duration seven days.
- `domain/market_calendar/sessions.py`: week tables built once (first and last session of each ISO
  week); `1w` branches in the four grid queries; a `1w` label error is `not_a_session` or `off_grid`.
- `domain/candle_resampling.py`: the `4h` algorithm becomes a private `_resample(source, target)`;
  `resample_daily_to_1w` is the new public entry point. The missing-bar report starts at the slot
  label instead of its open (the same instant for `4h`).
- `data/yahoo/provider.py`: `1w` plans a `1d` request without the intraday cap;
  `publishable_1w` withholds the last week while its closing daily bar is missing; the `4h` build
  and its logs are shared with `1w` (logs say "daily bars" instead of "hourly bars").
- `persistence/state.py`: `StateKey.LAST_RUN_1W`. Migration `0006` rebuilds `tickers`, `rules`,
  `ticker_rules`, `signals` and `bot_state` with the wider `CHECK` constraints.
- `notifications/message.py`: `week of <date>` for `1w`.
- Telegram and dashboard texts list `1w`. No new configuration.
- Dashboard chart (spec 027): a `1w` chart shows the last 156 weeks (three years), each named by
  the date of its first session like daily candles, with the same indicator lines and markers; the
  signal history shows `week of <date>`.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| `1w` parses, has a seven-day duration and a nominal close | unit | AC1, D1 |
| calendar week slots, holiday weeks, closes, labels and label errors | unit | AC2, AC3 |
| `1w` scheduling fires after the last session of the week | unit | AC3 |
| daily-to-weekly resampling values, gaps and the missing closing day | unit | AC4 |
| resampling is the same with daily rows after `now` | unit | AC7 |
| provider: `1d` plan without cap, withheld last week, end-to-end fetch | unit | AC4 |
| migration `0006` keeps every row, stores `1w`, matches the models, downgrades | unit | AC5 |
| signal message text for `1w` | unit | AC6 |
| Telegram `/add AAPL 1w` and the dashboard selector | unit | AC1 |

## Risks and security

- No secrets or new variables. The migration rebuilds five tables; the deploy takes a backup first
  and the migration keeps ids and `sqlite_sequence` counters, as `0005` did.
- A weekly rule with long indicators (for example SMA 200) asks Yahoo for about four years of daily
  bars per ticker, once a week: negligible load.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
