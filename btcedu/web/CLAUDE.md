# btcedu/web/ — Flask SPA Dashboard

## Architecture

- `app.py` — Flask app factory, registers `api_bp` blueprint
- `api.py` — 30+ REST endpoints under `/api` prefix (71KB)
- `jobs.py` — `JobManager` for background pipeline execution (thread-based)
- `static/app.js` — JavaScript SPA (vanilla JS, no framework), 75KB
- `static/styles.css` — styling, 41KB
- `templates/index.html` — HTML shell

## Key API Endpoints

Episodes: `GET/POST /api/episodes/<id>/{download,transcribe,run,retry,publish,...}`
Reviews: `GET/POST /api/reviews/<id>/{approve,reject,request-changes}`
Batch: `POST /api/batch/start`, `GET /api/batch/<id>`, `POST /api/batch/<id>/stop`
Jobs: `GET /api/jobs/<id>` (polling for background job status)
Files: `GET /api/episodes/<id>/files/<type>` (serve episode artifacts)
Channels: `GET/POST /api/channels`, `PATCH /api/channels/<id>` (includes `content_profile`)
Channels: `GET/POST /api/channels`, `PATCH /api/channels/<id>` (includes `content_profile`)

## Conventions

- All endpoints return JSON
- Background jobs use `JobManager._execute_job()` -> updates job status in-memory
- Health check: `GET /api/health` -> `{"status": "ok", ...}`
- Production: gunicorn with gthread worker, behind Caddy reverse proxy
- Dashboard served at `/dashboard/*` path via Caddy with basic auth
