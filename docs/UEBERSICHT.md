# Repo-Ueberblick: btcedu (bitcoin-education)

## Worum geht es?

`btcedu` ist eine Python-basierte Automations-Pipeline, die **deutsche Bitcoin-Podcast-Episoden** (YouTube/RSS) verarbeitet und daraus **tuerkische YouTube-Videos** erzeugt.

Die v2-Pipeline produziert pro Episode ein fertiges Video:
- Korrigiertes DE-Transkript
- Tuerkische Uebersetzung + kulturelle Adaptation
- Kapitelstruktur mit Bildern (DALL-E 3) + Sprachausgabe (ElevenLabs)
- Gerendertes MP4-Video (ffmpeg)
- Upload zu YouTube

## Pipeline

Zwei Pipeline-Versionen koexistieren. `PIPELINE_VERSION` in `.env` (Default=1) steuert den Ablauf.

### v1 Pipeline (Legacy)
```
detect -> download -> transcribe -> chunk -> generate (optional: refine)
```
Erzeugt 6 Text-Artefakte (Outline, Script, Shorts, Visuals, QA, Publishing Pack).

### v2 Pipeline (aktiv, vollstaendig)
```
detect -> download -> transcribe -> correct -> [review_gate_1] ->
translate -> adapt -> [review_gate_2] -> chapterize ->
imagegen -> tts -> render -> [review_gate_3] -> publish
```

Wichtige Eigenschaften:
- **Idempotent**: Stages skippen, wenn bereits erledigt (ausser `--force`).
- **Review Gates**: Pipeline pausiert, erstellt ReviewTask, setzt nach Approval fort.
- **Retry-faehig**: `btcedu retry` setzt an der letzten erfolgreichen Stage wieder an.
- **Kosten-Tracking**: PipelineRuns speichern Token-/Cost-Metriken; Sicherheits-Cap pro Episode.
- **Dry-Run**: `--dry-run` Flag fuer Platzhalter statt API-Calls.

## Tech-Stack / Abhaengigkeiten

- Python `>=3.12` (siehe `pyproject.toml`)
- CLI: `click` (`btcedu` Entry-Point, 34 Commands)
- Web: `flask` (+ `gunicorn` fuer Produktion)
- DB: `sqlite` via `sqlalchemy 2.0` + **FTS5**
- Config: `pydantic-settings` (`.env`)
- Externals:
  - Download: `yt-dlp`
  - Audio-Splitting: `pydub` (benoetigt `ffmpeg`)
  - Transkription: OpenAI Whisper (`openai`)
  - LLM: Anthropic Claude (`anthropic`)
  - Bilder: DALL-E 3 (`openai`)
  - TTS: ElevenLabs (raw HTTP)
  - Video: `ffmpeg` (Rendering)
  - Upload: YouTube Data API v3 (optional: `pip install -e ".[youtube]"`)

## Projektstruktur (wichtigste Ordner)

- `btcedu/`: Hauptpaket
  - `cli.py`: alle CLI-Commands (34 Stueck)
  - `config.py`: Settings aus `.env` (pydantic-settings)
  - `db.py`: DB-Init + Session Factory
  - `core/`: Pipeline-Stages + Orchestrierung (`pipeline.py`)
  - `services/`: Wrapper fuer RSS/yt-dlp/Whisper/Claude/ElevenLabs/DALL-E/YouTube
  - `models/`: SQLAlchemy ORM + Schemas + Migration-Model
  - `migrations/`: DB-Migrations-Registry (6 Migrationen)
  - `prompts/templates/`: YAML-Frontmatter + Jinja2 Templates (6 Stueck)
  - `web/`: Flask-App + REST API (30+ Endpoints) + Job-System + SPA-Dashboard
- `data/`: Laufzeitdaten (DB, audio, transcripts, outputs, reports, logs)
- `deploy/`: systemd Units + Caddy Snippet + Setup Script
- `docs/`: Feature-Dokus + Sprint-Logs
- `tests/`: pytest Suite (629 Tests)

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
- Pipeline: `PIPELINE_VERSION` (1=v1, 2=v2), `MAX_EPISODE_COST_USD`, `DRY_RUN`
- Pfade: `RAW_DATA_DIR`, `TRANSCRIPTS_DIR`, `OUTPUTS_DIR`, `REPORTS_DIR`, `LOGS_DIR`
- LLM: `CLAUDE_MODEL`, `CLAUDE_MAX_TOKENS`, `CLAUDE_TEMPERATURE`
- Bilder: `IMAGE_GEN_PROVIDER`, `IMAGE_GEN_MODEL`, `IMAGE_GEN_SIZE`, `IMAGE_GEN_QUALITY`
- TTS: `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`
- Render: `RENDER_RESOLUTION`, `RENDER_FPS`, `RENDER_CRF`, `RENDER_FONT`
- YouTube: `YOUTUBE_CLIENT_SECRETS_PATH`, `YOUTUBE_DEFAULT_PRIVACY`

Vollstaendige Liste: siehe `CLAUDE.md` Config-Tabelle.

## Wichtige CLI-Commands (Auswahl)

- Automation:
  - `btcedu run-latest`
  - `btcedu run-pending --max N --since YYYY-MM-DD`
  - `btcedu retry --episode-id ID`
- v2 Stages:
  - `btcedu detect`, `btcedu backfill`
  - `btcedu download --episode-id ID`
  - `btcedu transcribe --episode-id ID`
  - `btcedu correct --episode-id ID`
  - `btcedu translate --episode-id ID`
  - `btcedu adapt --episode-id ID`
  - `btcedu chapterize --episode-id ID`
  - `btcedu imagegen --episode-id ID`
  - `btcedu tts --episode-id ID`
  - `btcedu render --episode-id ID`
  - `btcedu publish --episode-id ID`
- v1 Stages (Legacy):
  - `btcedu chunk`, `btcedu generate`, `btcedu refine`
- Reviews:
  - `btcedu review list`, `btcedu review approve ID`, `btcedu review reject ID`
- Prompts:
  - `btcedu prompt list`, `btcedu prompt promote ID`
- YouTube:
  - `btcedu youtube-auth`, `btcedu youtube-status`
- Monitoring/DB:
  - `btcedu status`, `btcedu report --episode-id ID`, `btcedu cost`
  - `btcedu init-db`, `btcedu migrate`, `btcedu migrate-status`
  - `btcedu journal`, `btcedu llm-report`

Vollstaendige Liste: siehe `CLAUDE.md` CLI-Tabelle.

## Web-Dashboard: Architektur in Kurzform

- Backend: `btcedu/web/app.py` (Flask Factory) + `btcedu/web/api.py` (30+ REST Endpoints)
- Async/Jobs: `btcedu/web/jobs.py` mit `ThreadPoolExecutor(max_workers=1)` (SQLite single-writer kompatibel)
- UI: SPA mit `btcedu/web/static/app.js` + `btcedu/web/static/styles.css`
- Features: Episode-Table, Pipeline-Steuerung, Artefakt-Viewer, Review-Queue, TTS-Player, Job-Logs, Kosten, Batch/"Process All", Multi-Channel

## Deployment (Raspberry Pi / systemd / GitHub Actions)

- Update/Deploy Script: `run.sh`
  - `git pull` + `pip install` + `btcedu migrate` + `daemon-reload` + Restart aller Services/Timers
- systemd Units:
  - `btcedu-web.service` (Web-Dashboard, gunicorn auf Port 8091)
  - `btcedu-detect.timer` (RSS-Scan alle 6 Stunden)
  - `btcedu-run.timer` (Pipeline-Verarbeitung taeglich um 02:00)
- Reverse Proxy: `deploy/Caddyfile.dashboard` (HTTPS + basic auth + /dashboard Prefix)
- GitHub Actions:
  - CI: `.github/workflows/ci.yml` (ruff + pytest, Python 3.12/3.13)
  - Security: `.github/workflows/security.yml` (pip-audit + CodeQL)
  - Deploy: `.github/workflows/deploy.yml` (SSH -> `bash .../run.sh`)
- Setup: `deploy/setup-web.sh` (installiert alle Units + Caddy-Config)

## Tests & Quality

```bash
pytest tests/ -v          # 629 Tests
ruff check btcedu/ tests/
ruff format --check btcedu/ tests/
```

Pre-commit: `.pre-commit-config.yaml` (ruff + format + Standard-Hooks).

## Weitere wichtige Dokus

- Design-Dokument: `MASTERPLAN.md`
- Referenz (vollstaendig): `CLAUDE.md`
- Server Setup: `docs/SERVER_DEPLOYMENT_GUIDE.md`
- Batch + Multi-Channel: `docs/PROCESS_ALL_MULTI_CHANNEL.md`
- Sprint-Logs: `docs/sprints/`
- Archiv (historisch): `docs/archive/`
