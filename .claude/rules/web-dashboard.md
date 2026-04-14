---
paths:
  - btcedu/web/**
---

# Web Dashboard Rules

- Flask SPA: `app.py` (factory), `api.py` (REST endpoints), `jobs.py` (background pipeline jobs)
- Frontend: vanilla JS (`static/app.js`), no framework
- All endpoints return JSON; background jobs use `JobManager` with in-memory status
- Dashboard served at `/dashboard/*` via Caddy reverse proxy in production
- Channel management: each channel has a `content_profile` — dashboard auto-selects matching profile on channel change
- Never expose API keys or secrets to the browser; `.env` is server-side only
