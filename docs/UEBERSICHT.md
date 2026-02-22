# Repo-Ueberblick: btcedu (bitcoin-education)

## Worum geht es?

`btcedu` ist eine Python-basierte Automations-Pipeline, die **deutsche Bitcoin-Podcast-Episoden** (YouTube/RSS) verarbeitet und daraus **tuerkische, publikationsfertige YouTube-Content-Pakete** erzeugt.

Pro Episode entstehen standardmaessig 6 Artefakte:
- `outline.tr.md`
- `script.long.tr.md`
- `shorts.tr.json`
- `visuals.json`
- `qa.json`
- `publishing_pack.json`

## Pipeline (End-to-End)

High-level:

`detect -> download -> transcribe -> chunk -> generate` (optional: `refine`)

- `detect`: neue Episoden aus Feed in DB eintragen (schnell, keine Medienverarbeitung)
- `download`: Audio via `yt-dlp`
- `transcribe`: Whisper API (OpenAI) -> DE-Transkript (raw + bereinigt)
- `chunk`: Transcript in ueberlappende Text-Chunks + **SQLite FTS5** Index
- `generate`: Claude (Anthropic) erzeugt die 6 Artefakte mit Retrieval aus FTS5 (RAG + Zitierbarkeit)
- `refine`: optionale zweite Politur-Generation (v1 -> v2 Dateien)

Wichtige Eigenschaften:
- **Idempotent**: Stages skippen, wenn bereits erledigt (ausser `--force`).
- **Retry-faehig**: `btcedu retry` setzt an der letzten erfolgreichen Stage wieder an.
- **Kosten-Tracking**: PipelineRuns speichern Token-/Cost-Metriken; Reports als JSON.

## Tech-Stack / Abhaengigkeiten

- Python `>=3.12` (siehe `pyproject.toml`)
- CLI: `click` (`btcedu` Entry-Point)
- Web: `flask` (+ optional `gunicorn` fuer Produktion)
- DB: `sqlite` via `sqlalchemy` + **FTS5**
- Externals:
  - Download: `yt-dlp`
  - Audio-Splitting: `pydub` (benoetigt `ffmpeg` auf dem System)
  - Transkription: OpenAI Whisper (`openai`)
  - Generierung: Anthropic Claude (`anthropic`)

## Projektstruktur (wichtigste Ordner)

- `btcedu/`: Hauptpaket
  - `btcedu/cli.py`: alle CLI-Commands
  - `btcedu/config.py`: Settings aus `.env` (pydantic-settings)
  - `btcedu/db.py`: DB-Init + Session Factory
  - `btcedu/core/`: Pipeline-Stages + Orchestrierung (`pipeline.py`)
  - `btcedu/services/`: Wrapper fuer RSS/yt-dlp/Whisper/Claude
  - `btcedu/models/`: SQLAlchemy ORM + Schemas + Migration-Model
  - `btcedu/migrations/`: DB-Migrations-Registry
  - `btcedu/prompts/`: Prompt-Templates je Artefakt + Refinement
  - `btcedu/web/`: Flask-App + REST API + Job-System + UI (templates/static)
- `data/`: Laufzeitdaten (DB, audio, transcripts, chunks, outputs, reports, logs)
- `deploy/`: systemd Units + Caddy Snippet + Setup Script
- `docs/`: Architektur/Deployment/Feature-Dokus
- `scripts/`: Hilfsskripte (z.B. Migration fuer Multi-Channel)
- `tests/`: pytest Suite

## Einstieg (lokal)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# .env mit API Keys + Channel ID befuellen

btcedu init-db
btcedu migrate
btcedu run-latest
```

Web-Dashboard (dev):

```bash
pip install -e ".[web]"
btcedu web
```

## Konfiguration (.env)

Vorlage: `.env.example`

Wichtige Variablen:
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (optional `WHISPER_API_KEY`)
- Quelle: `SOURCE_TYPE=youtube_rss` + `PODCAST_YOUTUBE_CHANNEL_ID` (oder `PODCAST_RSS_URL`)
- DB: `DATABASE_URL` (Default: `sqlite:///data/btcedu.db`)
- Pfade: `RAW_DATA_DIR`, `TRANSCRIPTS_DIR`, `CHUNKS_DIR`, `OUTPUT_DIR`, `OUTPUTS_DIR`, `REPORTS_DIR`, `LOGS_DIR`
- Chunking: `CHUNK_SIZE`, `CHUNK_OVERLAP`
- Optional Retrieval: `USE_CHROMA`, `CHROMADB_PERSIST_DIR`
- Generation: `CLAUDE_MODEL`, `CLAUDE_MAX_TOKENS`, `CLAUDE_TEMPERATURE`, `DRY_RUN`

## Wichtige CLI-Commands (Auswahl)

- Automation:
  - `btcedu run-latest`
  - `btcedu run-pending --max N --since YYYY-MM-DD`
  - `btcedu retry --episode-id ID`
- Stages:
  - `btcedu detect`, `btcedu backfill`
  - `btcedu download --episode-id ID`
  - `btcedu transcribe --episode-id ID`
  - `btcedu chunk --episode-id ID`
  - `btcedu generate --episode-id ID [--top-k 16] [--force]`
  - `btcedu refine --episode-id ID [--force]`
- Monitoring/DB:
  - `btcedu status`, `btcedu report --episode-id ID`, `btcedu cost`
  - `btcedu migrate-status`, `btcedu migrate`

## Web-Dashboard: Architektur in Kurzform

- Backend: `btcedu/web/app.py` (Flask Factory) + `btcedu/web/api.py` (REST)
- Async/Jobs: `btcedu/web/jobs.py` mit `ThreadPoolExecutor(max_workers=1)` (SQLite single-writer kompatibel)
- UI: `btcedu/web/templates/index.html` + `btcedu/web/static/app.js` + `btcedu/web/static/styles.css`
- Features u.a.: Episode-Table, Artefakt-Viewer, Job-Logs, Kosten, Batch/"Process All", Multi-Channel

## Deployment (Raspberry Pi / systemd / GitHub Actions)

- Update/Deploy Script: `run.sh`
  - `git pull` + `pip install -e ".[web]"` + `btcedu migrate` + `sudo systemctl restart btcedu-web`
- systemd Units: `deploy/btcedu-web.service`, `deploy/btcedu-detect.*`, `deploy/btcedu-run.*`
- Reverse Proxy (optional): `deploy/Caddyfile.dashboard` (HTTPS + basic auth + /dashboard Prefix)
- GitHub Actions:
  - CI: `.github/workflows/ci.yml` (ruff + pytest, Python 3.12/3.13)
  - Security: `.github/workflows/security.yml` (pip-audit + CodeQL)
  - Deploy: `.github/workflows/deploy.yml` (SSH -> `bash .../run.sh`)

## Tests & Quality

```bash
pytest tests/ -v
ruff check btcedu/ tests/
ruff format --check btcedu/ tests/
```

Pre-commit: `.pre-commit-config.yaml` (ruff + format + Standard-Hooks).

## Weitere wichtige Dokus (lesen statt raten)

- Architektur: `docs/ARCHITECTURE.md`
- Server Setup/Service: `docs/SERVER_DEPLOYMENT_GUIDE.md`
- Batch + Multi-Channel: `docs/PROCESS_ALL_MULTI_CHANNEL.md`
- Modelle/Constraints: `docs/CLAUDE_MODELS.md`, `docs/MODELS_TABLE.md`, `docs/CONSTRAINTS_TABLE.md`
- Deployment-Nachweise: `DEPLOYMENT_VERIFICATION_REPORT.md`, `DEPLOYMENT_FINALIZATION_SUMMARY.md`
