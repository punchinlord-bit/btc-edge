# BTC-Edge Security Audit Log

Running record of findings, status, and fix summaries. Updated after each fix.

## Scope: FastAPI routes + auth (web layer)

### A1 — No authentication on the dashboard/API  — **FIXED**

- **Severity:** Critical
- **file:line:** `src/btc_edge/web/app.py` (all `/api/*` routes + `/ws/live`)
- **What was wrong:** Every endpoint (including kill-switch control and the paid LLM endpoint) was reachable with no auth. Anyone who found the Render URL could read state and toggle halt/resume.
- **Fix:** Added `require_auth` dependency (`app.py`) reading `DASHBOARD_API_KEY` from the environment. Applied to all `/api/*` routes and `/ws/live`. `/api/health` stays public (Render probe) but now returns only `{"status":"ok"}`. When the key is unset (local dev) auth is skipped; production must set it.
- **Public API change:** Yes — protected endpoints now require `X-API-Key: <DASHBOARD_API_KEY>`.
- **Also changed:** `config.py` (new `dashboard_api_key` field), `render.yaml` (`DASHBOARD_API_KEY` env, `sync:false`), `.env.example`, `index.html` (prompts for key, stores in localStorage, attaches header).
- **Test:** `tests/test_auth.py` (4 tests) — fails before fix, passes after.
- **Verification (pasted):**
  - `pytest`: **76 passed**
  - `bandit`: 7 findings, all pre-existing (SHA1 dedup hash, `0.0.0.0` bind, empty-string default, `assert`, XML parsing) — none introduced by this change
  - `ruff check`: All checks passed
  - `pip-audit`: `pip 25.0.1` → 12 known CVEs (PYSEC-2026-*), fixed in >=26.2.0 (build-time tool, not a runtime dependency)

## Open findings (from first-pass table)

| ID | Severity | Status | Note |
|----|----------|--------|------|
| A1 | Critical | **FIXED** | this turn |
| A2 | High | open | resume must require auth (covered by A1 now — resume is authed; may add separate operator credential later) |
| A3 | High | open | rate-limit `/api/assistant` |
| A4 | High | open | Telegram bot token leaks into logs on error |
| A5 | Low-Med | open | validate `AI_BASE_URL` scheme/host |
| A6 | Low-Med | open | prompt injection via scraped news |
| A7 | Med | open | WS auth (covered by A1 now — `/ws/live` requires key) |
| A8 | Low | open | `live_trading_allowed()` reads raw env vs parsed field |
| A9 | Low | open | `/docs` + `/openapi.json` public |
| A10 | Low | open | `engine_state` global singleton, no locking |
| A11 | Low | open | dependency CVEs (see `pip` finding above; run pip-audit in CI) |
