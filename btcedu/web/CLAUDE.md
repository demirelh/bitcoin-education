# btcedu/web/ — Flask SPA Dashboard

## Architecture

- `app.py` — Flask app factory, registers `api_bp` under `/api`, serves the page routes (`/`, `/whatsapp`)
- `api.py` — REST endpoints under `/api` prefix (~165KB)
- `jobs.py` — `JobManager` for background pipeline execution (thread-based)
- `static/app.js` — JavaScript SPA (vanilla JS, no framework), ~157KB
- `static/styles.css` — styling, ~66KB
- `templates/index.html` — HTML shell; `templates/whatsapp.html` — WhatsApp pairing page

## Key API Endpoints

Episodes: `GET/POST /api/episodes/<id>/{download,transcribe,run,retry,publish,...}`
Copilot repair: `POST /api/episodes/<id>/fix-problem` starts one autonomous
`copilotfix` tmux session. Automatic failure-triggered attempts are deduplicated
by exact error fingerprint; manual and automatic attribution is persisted in
episode provenance and shown in the pipeline stepper. Both manual and automatic
fixes must pass the affected stage against the profile's three most recent
episodes in the isolated regression runner before commit, push or resume.
Reviews: `GET/POST /api/reviews/<id>/{approve,reject,request-changes}`
QA panel actions: `POST /api/episodes/<id>/qa/transcript/{approve,request-changes}` (reuses/creates the
`transcript_qa` ReviewTask), `POST /api/episodes/<id>/qa/findings/<finding_id>/status` (mutates a
translation QA finding's status open/resolved/dismissed; requires an existing pending/in_review
`translation_qa` ReviewTask; audit trail lives in the finding's `history` inside
`translation_quality_gate.json`, no parallel DB)
Batch: `POST /api/batch/start`, `GET /api/batch/<id>`, `POST /api/batch/<id>/stop`
Jobs: `GET /api/jobs/<id>` (polling for background job status)
Files: `GET /api/episodes/<id>/files/<type>` (serve episode artifacts)
Channels: `GET/POST /api/channels`, `PATCH /api/channels/<id>` (includes `content_profile`)
WhatsApp: `GET /api/whatsapp/status` (pairing state + QR code as data URL, proxied from the local
whatsapp-service), `POST /api/whatsapp/relink`, `POST /api/whatsapp/test`; pairing page at `/whatsapp`
Weather: `GET /api/episodes/<id>/weather` (summary), `.../weather/<chapter_id>/detail|image|video`,
`POST .../weather/<chapter_id>/rerender`, `POST .../weather/<chapter_id>/override`,
`GET .../weather/overrides`
Intro audio: `GET/POST/DELETE /api/intro-audio`, `GET /api/intro-audio/file` (Tagesschau intro MP3, max 20 MB)
Render mode: `GET/POST /api/render-mode` (local `app_settings` override)
Failover: `GET /api/failover/status`, `POST /api/failover/mode` (proxy the
external control plane; operator token required for mode changes)
Credits: `GET /api/credits`, `POST /api/credits/openai-balance` (stores the
current OpenAI balance baseline used for the two-episode warning popup)

## Conventions

- All endpoints return JSON
- Background jobs use `JobManager._execute_job()` -> updates job status in-memory
- Full-pipeline jobs use `run_episode_pipeline_coordinated()` so web-triggered
  work obeys the same failover lease as CLI/timer runs
- Pipeline progress aggregates retries: earliest start, latest completion,
  summed duration/cost and `attempt_count`; timestamps are normalized to UTC
- Health check: `GET /api/health` -> `{"status": "ok", "time": ...}` — the only
  unauthenticated API route, and deliberately minimal; version and commit are
  added only for a logged-in operator
- Authentication (`web/auth.py`, WP-8A): everything except `auth.login`,
  `auth.logout`, `static` and `api.health` requires a session. Browser pages
  redirect to `/login`, `/api/*` answers JSON 401. `create_app()` raises
  `AuthConfigError` when the configuration would be unsafe
- CSRF: `CSRFProtect` on every POST/PUT/PATCH/DELETE, token via `X-CSRFToken`;
  authentication is checked first so an anonymous POST is a 401, not a 403
- `_operator_ref()` reads the session only. Never a request field, never a
  header. Audit refs are prefixed `web:` / `cli:` / `system:`
- Production: gunicorn with gthread worker, behind Caddy reverse proxy
- Dashboard served at `/dashboard/*` path via Caddy with basic auth (now a
  second door, not the only one)
- Templates use relative URLs (`api/...`, `whatsapp`) so the `/dashboard/` prefix keeps working

<!--
Documentation sync
Baseline: d1b4676
Synced through: current working tree
Date: 2026-08-22
-->
