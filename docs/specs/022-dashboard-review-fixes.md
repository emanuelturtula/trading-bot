# 022 — Dashboard review fixes

- **Status:** approved (the user's review of the first deploy, items 1–23)
- **Branch:** `feature/dashboard-polish`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Fix the errors, risks and UI problems the user found on the configuration dashboard of spec
[020](020-config-dashboard.md), so the pages explain the real model and state of the bot and are
safer to operate.

## Out of scope

- The signal history, rule preview, visual rule builder, integration status and the rest of the
  review's feature list (items 24–33): later specs.
- Renaming the `price` operand for `volume` (item 10): it changes the rule grammar of spec 006 and
  every stored rule; documented in the guide instead and left for a schema spec.
- Syntax highlighting and live validation in the JSON editor (item 20): they need JavaScript, and
  the dashboard's CSP allows none (spec 020, D1). A server-side "Check without saving" replaces
  live validation.
- mTLS and HTTPS in front of the dashboard (spec 021, another branch).

## Acceptance criteria

- [ ] **AC1 (item 1):** opening a delete link only shows the confirmation page; deleting is a
  `POST` with the CSRF token, and the button looks destructive. Pinned by a test.
- [ ] **AC2 (item 2):** the tickers and rules pages describe one model: a new ticker gets every
  rule of its timeframe, a new rule can be assigned to every ticker of its timeframe, and each
  ticker's page fine-tunes its rules.
- [ ] **AC3 (item 3):** each timeframe option of the add form says how many enabled rules a new
  ticker of that timeframe gets, and the tickers list says "no signal" for a ticker without an
  enabled rule.
- [ ] **AC4 (items 4–7):** the overview says "Running, with nothing to evaluate" when no watched
  ticker has an enabled rule, and otherwise how many tickers are evaluated; the counts drop the
  "x of y" form; a table per timeframe shows how many tickers are evaluated, the last candle
  close evaluated, the next run, and explains "never" and that failures are in the logs.
- [ ] **AC5 (item 8):** `TB_DISPLAY_TIMEZONE` defaults to `America/Argentina/Buenos_Aires`, and the
  footer names the zone in use.
- [ ] **AC6 (item 9):** the rule editor has the Enabled checkbox; saving applies it.
- [ ] **AC7 (items 10, 11):** the guide says that `"price": "volume"` reads the volume column and
  shows a nested `any` inside an `all`, which `parse_rule` accepts (pinned by a test).
- [ ] **AC8 (items 12–19, 21–23):** the navigation marks the current page (`aria-current`); the
  scrollbar gutter is stable; rule and ticker names are the only link to their page and are styled
  as text; Delete is red and set apart; state is an On/Off badge that switches it; BUY and SELL are
  green and red; empty states invite a first item; the symbol input has a visible label and shows
  capitals; a favicon is inline; the disclaimer is in a common footer; tables fit a 390 px screen.
- [ ] **AC9 (item 20):** the guide is split into collapsible sections, and "Check without saving"
  validates a document without storing it.
- [ ] **AC10:** `scripts/check.py` is green.

## Design

Templates and `dashboard/routes.py` only, plus the default zone in `config.py`. The overview reads
the next fire times from the running scheduler (`runtime.scheduler.next_fire_times()`); without a
started runtime it shows "not scheduled". The rule form sends `intent=check` to validate only. The
ticker switch accepts `back=ticker` to return to the ticker's page.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| A delete link renders a POST form and deletes nothing | unit | AC1 |
| Timeframe options with rule counts | unit | AC3 |
| Overview states (nothing to evaluate, evaluated, no rules) | unit | AC4 |
| Default zone and footer | unit | AC5 |
| Edit with and without Enabled; check without saving | unit | AC6, AC9 |
| Navigation, favicon, footer, labels, badges, side colors | unit | AC8 |

## Risks and security

- No new input reaches the database without `parse_rule`; "check" writes nothing.
- The default zone change also moves Telegram times from Madrid to Buenos Aires for anyone who did
  not set `TB_DISPLAY_TIMEZONE`.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
