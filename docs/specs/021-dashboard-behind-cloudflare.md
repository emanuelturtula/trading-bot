# 021 — Dashboard behind a Cloudflare tunnel with client certificates

- **Status:** approved (the user asked for it; no open product questions)
- **Branch:** `feature/dashboard-client-certs-ecfn7l`
- **Spec author:** tech-lead
- **Expected commit type:** feat
- **Roadmap:** F6 (API and dashboard), access from the user's devices.

## Goal

Reach the configuration dashboard (spec [020](020-config-dashboard.md)) from the user's own
devices outside the LAN, reusing the client certificates they already installed for another
self-hosted app. Access goes through the Cloudflare Tunnel that already runs on the Raspberry Pi:
Cloudflare demands a client certificate signed by the account's Cloudflare Managed CA (mTLS) and
a WAF rule blocks every request without a valid, unrevoked one before it reaches the Pi. The
dashboard password stays as a second layer.

## Out of scope

- Issuing new client certificates or a new CA: the existing ones are reused as they are.
- Running `cloudflared` from this repository: the tunnel is host infrastructure, configured by
  the user in Cloudflare and on the Pi (DEPLOYMENT.md).
- Trusting `X-Forwarded-*` or `CF-Connecting-IP` headers: the app does not need the real client
  address (see "Risks").
- Any order placement (CLAUDE.md rule 1).

## Acceptance criteria

- [ ] **AC1:** the session cookie is `Secure` when the browser reached the dashboard over HTTPS,
  even though the tunnel delivers the request to the app over plain HTTP. The browser's scheme is
  read from the `Origin` header of the login `POST`.
- [ ] **AC2:** with an `Origin` that is absent, `null` or not HTTP(S), the decision falls back to
  the request URL, as before: plain HTTP on the LAN or the tailnet still gets a cookie without
  `Secure` (which the browser would otherwise drop), and an app served over HTTPS gets `Secure`.
- [ ] **AC3:** DEPLOYMENT.md explains how to publish the dashboard through the existing tunnel
  and the same client-certificate setup, with placeholders instead of real hostnames.

## Design

- `src/trading_bot/dashboard/routes.py`: a private helper `_is_https(request) -> bool` replaces
  `request.url.scheme == "https"` in the login `set_cookie`. Same approach as the user's other
  app. A forged `Origin` only changes the flag on the forger's own cookie.
- No new configuration, no change to `deploy/compose.yml` or the Dockerfile.
- Cloudflare (manual, documented): a public hostname on the existing tunnel pointing at the
  bot's port 8081; that hostname added to the client-certificate (mTLS) hosts; the existing WAF
  rule widened to cover it.

## Test plan

| Case | Type (unit/integration) | What it verifies |
|------|-------------------------|------------------|
| Login with `Origin: https://…` over plain HTTP | unit (TestClient) | AC1: cookie is `Secure` |
| Login with `Origin: http://…`, `null` or none | unit (TestClient) | AC2: no `Secure` over plain HTTP |
| Login on an app served over HTTPS | unit (TestClient) | AC2: `Secure` from the request URL |

## Risks and security

- Through the tunnel every request comes from `cloudflared`, so the login throttle (5 failures in
  15 minutes per client address) is shared by every device using the tunnel. That fails closed
  and only someone who already holds a valid client certificate can trigger it; LAN access keeps
  its own counter.
- Port 8081 stays published on the Pi's LAN interface: plain HTTP from the LAN or the tailnet
  keeps working. Never forward it from the router; the tunnel needs no open port.
- No secrets involved and no deploy impact: no new variables in `secrets.env`.

## Review checklist (tech-lead)

- [ ] Meets the unbreakable rules of CLAUDE.md
- [ ] Diff contains no secrets, IPs, hostnames or users
- [ ] Tests cover the acceptance criteria
- [ ] `scripts/check.py` green
- [ ] Every artifact is in English (CLAUDE.md, rule 9)
