# 026 — Rule builder

- **Status:** approved (the user chose to start it before the multi-user redesign; templates as listed)
- **Branch:** `feature/rule-builder`
- **Spec author:** tech-lead
- **Expected commit type:** feat

## Goal

Writing a rule today means typing a JSON document next to a long guide. The user asked for something
simpler, closer to TradingView: pick a kind of rule from a drop-down and fill in its parameters.
The builder produces the same rule document (spec 006), so the engine, storage and Telegram do not
change.

## Out of scope

- The per-ticker chart with indicators and BUY/SELL markers, and the signal history (review items
  24 and 27): a later spec, after the per-user data change of the multi-user redesign.
- Previewing where a rule would have fired (item 25): it belongs to the chart.
- Any change to the rule grammar (spec 006) or to stored rules.

## Acceptance criteria

- [ ] **AC1:** the new-rule page opens on the builder. A "Kind" drop-down offers templates, and each
  template shows only its parameters, with defaults and the ranges of the indicator registry:
  - "RSI below / above a level" (length, level, side)
  - "Moving averages cross" (SMA or EMA, fast and slow lengths, up or down)
  - "MACD crosses its signal line" (fast, slow, signal, up or down)
  - "Price crosses a moving average" (SMA or EMA, length, up or down)
  - "Price leaves the Bollinger bands" (length, std, lower or upper)
  - "Stochastic below / above a level" (k, d, level)
  - "Custom": a list of conditions, each built from drop-downs (indicator or price or value, output,
    operator, indicator or price or value), combined with "all" or "any".
- [ ] **AC2:** every kind also asks for name, signal (BUY/SELL), timeframe, cooldown, Enabled and
  "assign to every ticker of its timeframe".
- [ ] **AC3:** the builder submits the same form as today (`document`), and the server validates it
  with `parse_rule`; problems are shown next to the builder. "Check without saving" keeps working.
- [ ] **AC4:** "Edit as JSON" switches to the current JSON editor with the document filled in. A rule
  whose document matches a template opens in that template; any other rule opens in "Custom", and
  one the builder cannot represent opens in the JSON editor.
- [ ] **AC5:** the dashboard allows its own scripts only: CSP `script-src 'self'`, files served from
  the package under `/dashboard/static/`, no inline scripts, no CDN. Without JavaScript, the JSON
  editor still works. Scripts never parse HTML: values reach the page as text.
- [ ] **AC6:** `scripts/check.py` is green.

## Design

- Templates are data: `dashboard/builder.py` describes each template by its fields (defaults and
  ranges taken from the indicator registry) and a `conditions` pattern with `{"$": "<field>"}`
  placeholders. The page embeds the templates, the indicator catalog, the candle columns and the
  timeframes as a `<script type="application/json">` block (data, not code).
- `dashboard/static/builder.js` (plain JavaScript, no build step) renders the fields and keeps the
  `document` textarea in sync on every change. On load, and when coming back from the JSON editor,
  it reads the document: a template whose pattern unifies with the conditions opens that template;
  otherwise a document within the grammar opens in Custom; anything else stays in the JSON editor.
- `fill(template, values)` and `match(conditions)` implement the same patterns in Python; the unit
  tests check that every template, at its defaults and at both ends of its ranges, gives a rule
  `parse_rule` accepts and that the stored rule matches the template again. The script mirrors them
  and was exercised in a browser (Playwright, outside CI: the CI image has no browser).
- `StaticFiles` serves `/dashboard/static/` without a session: the files are public code with no
  data. The dashboard's security headers apply to them too.

## Test plan

| Case | Type | What it verifies |
|------|------|------------------|
| Each template with defaults and with edge values parses | unit | AC1, AC3 |
| `template_of(document_for(...))` round-trips; other rules fall back to Custom or JSON | unit | AC4 |
| CSP header allows `'self'` scripts only; no inline script in any page | unit | AC5 |
| The rule form embeds the builder data and the script is served with the CSP | unit | AC1, AC5 |
| Picking a template, changing a parameter and saving stores the expected rule; Custom with a group; back and forth with JSON; 390 px width | manual (Playwright) | AC1–AC4 |

## Risks and security

- Allowing scripts widens the CSP. It stays limited to files shipped in the image; templates are
  rendered as data, and Jinja autoescaping stays on.
- No new configuration or migration.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
