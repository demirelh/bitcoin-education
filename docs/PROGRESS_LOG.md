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
- Systemd unit for web dashboard
- Episode selection via URL params
- Bulk actions (run-pending from UI)
- WebSocket for live pipeline progress

---
