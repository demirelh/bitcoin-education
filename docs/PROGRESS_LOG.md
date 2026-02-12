# Bitcoin Education - Progress Log

Append-only project journal. Do NOT edit past entries.

---

## Web Dashboard MVP - Plan
_2026-02-12_

**Goal:** Add a lightweight local web dashboard (Flask) to the btcedu project, alongside a project journal utility for tracking development progress across sessions.

**Architecture decisions:**
- Flask chosen over FastAPI: no async needed, built-in Jinja2, lighter on Raspberry Pi
- Single-page dashboard with vanilla JS (no build step, no framework)
- API blueprint pattern for clean separation of routes
- Reuses existing pipeline functions directly (no subprocess calls)
- Journal utility with secret redaction for safe append-only logging

**Deliverables:**
1. `btcedu/utils/journal.py` - Progress log utility with secret redaction
2. `btcedu/web/app.py` - Flask app factory
3. `btcedu/web/api.py` - REST API (13 endpoints)
4. `btcedu/web/templates/index.html` - Dashboard HTML
5. `btcedu/web/static/app.js` - Vanilla JS frontend
6. `btcedu/web/static/styles.css` - Dark theme CSS
7. `tests/test_journal.py` - 14 tests for journal utility
8. `tests/test_web.py` - 23 tests for web API
9. CLI commands: `btcedu web`, `btcedu journal`

---

## Web Dashboard MVP - Implementation Complete
_2026-02-12_

**Files created:**
- `btcedu/utils/__init__.py`
- `btcedu/utils/journal.py` (redact, journal_append, journal_event)
- `btcedu/web/__init__.py`
- `btcedu/web/app.py` (Flask app factory with settings injection)
- `btcedu/web/api.py` (Blueprint: 13 API endpoints)
- `btcedu/web/templates/index.html` (single-page dashboard)
- `btcedu/web/static/app.js` (vanilla JS, ~200 lines)
- `btcedu/web/static/styles.css` (dark theme, ~250 lines)
- `tests/test_journal.py` (14 tests)
- `tests/test_web.py` (23 tests, all mocked)
- `docs/PROGRESS_LOG.md` (this file)

**Files modified:**
- `btcedu/cli.py` - Added `btcedu web` and `btcedu journal` commands
- `pyproject.toml` - Added `[web]` optional dependency group (flask>=3.0.0)
- `README.md` - Added web dashboard + progress log documentation

**API endpoints implemented:**
- GET /api/episodes, GET /api/episodes/{id}
- POST /api/detect, /api/episodes/{id}/download, transcribe, chunk, generate, run, retry
- GET /api/episodes/{id}/files/{type}
- GET /api/cost, GET /api/whats-new

**Test results:** 172 tests passing (135 existing + 14 journal + 23 web)

**How to run:**
```bash
pip install -e ".[web]"
btcedu web                    # localhost:5000
btcedu web --host 0.0.0.0    # LAN access
```

---

## How to Resume

To continue development in a new Claude session, provide this context:

```
Project: /home/pi/AI-Startup-Lab/bitcoin-education
See docs/PROGRESS_LOG.md for what's been built so far.
The web dashboard MVP is complete with 172 passing tests.
Key files: btcedu/web/app.py, btcedu/web/api.py, btcedu/web/static/app.js
Run: btcedu web --host 0.0.0.0
Tests: python -m pytest tests/ -v
```

**Next steps (not yet implemented):**
- Auto-refresh polling (optional timer in JS)
- Episode selection via URL params
- Bulk actions (run-pending from UI)
- WebSocket for live pipeline progress

---

## Public Dashboard Deployment - Plan & Implementation
_2026-02-12_

**Goal:** Expose the btcedu web dashboard publicly at https://lnodebtc.duckdns.org/dashboard/
with basic auth, HTTPS, and production-grade serving.

**Key discovery:** Caddy v2.6.2 already runs on the Pi with auto-TLS for lnodebtc.duckdns.org.
No need for nginx or certbot.

**Architecture:**
```
Internet → DuckDNS → Router → Caddy (:443, auto-TLS) → Gunicorn (:8090)
```

**Files created:**
- `deploy/btcedu-web.service` - systemd unit for gunicorn (2 workers, 300s timeout, localhost-only)
- `deploy/Caddyfile.dashboard` - Caddy config snippet with basic_auth + security headers
- `deploy/setup-web.sh` - Deployment helper script

**Files modified:**
- `pyproject.toml` - Added gunicorn to [web] deps
- `btcedu/cli.py` - Added `--production` flag to `btcedu web` command
- `README.md` - Added "Public Dashboard Deployment" section

**Security measures:**
- Gunicorn binds to 127.0.0.1 only
- Caddy basic_auth with bcrypt password
- Security headers: X-Content-Type-Options, X-Frame-Options, Referrer-Policy
- Auto-renewing TLS via Caddy/Let's Encrypt
- No secrets exposed to frontend

**Deployment steps:**
```bash
.venv/bin/pip install 'gunicorn>=22.0.0'
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now btcedu-web
# Then update /etc/caddy/Caddyfile with handle_path /dashboard/* block
sudo systemctl reload caddy
```

---

## Dashboard Fix: Reverse Proxy URL Routing
_2026-02-12_

**Problem:** Dashboard at `https://lnodebtc.duckdns.org/dashboard/` loaded HTML but was completely non-functional — episode list stuck on "Loading...", all buttons broken.

**Root cause:** Absolute URL paths in HTML and JS conflicted with Caddy's route matching:
- `<link href="/static/styles.css">` → served by btc-dashboard file_server (wrong app)
- `<script src="/static/app.js">` → served by btc-dashboard file_server (wrong app)
- `fetch("/api/episodes")` → routed to btc-dashboard API on :9000 (wrong backend)

Only the initial `/dashboard/` request matched Caddy's `@dashboard path /dashboard/*` matcher. All subsequent absolute-path requests bypassed it.

**Fix:**
1. Changed HTML paths from absolute to relative: `href="static/styles.css"`, `src="static/app.js"`
2. Changed JS fetch from `fetch("/api" + path)` to `fetch("api" + path)` (relative)
3. Added `redir /dashboard /dashboard/ permanent` in Caddy for trailing slash
4. Added `GET /api/health` endpoint for monitoring
5. Added error handling in JS: try/catch on refresh(), error banner in table, toast on API failure
6. Added 5 new tests: health endpoint, relative paths verification, static asset serving, JS path check

**Files modified:**
- `btcedu/web/templates/index.html` - Relative static paths
- `btcedu/web/static/app.js` - Relative API paths + error handling
- `btcedu/web/api.py` - Health endpoint
- `deploy/Caddyfile.dashboard` - Trailing slash redirect
- `tests/test_web.py` - 5 new tests (28 total)

**Test results:** 177 tests passing (172 existing + 5 new)

**Verification:** All routes confirmed working through Caddy proxy (health, episodes API, static assets, redirect).

---
