# btcedu - Bitcoin Education Automation

[![CI](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml)
[![Security](https://github.com/demirelh/bitcoin-education/actions/workflows/security.yml/badge.svg)](https://github.com/demirelh/bitcoin-education/actions/workflows/security.yml)

Automated pipeline that transforms German Bitcoin podcast episodes into Turkish educational YouTube content packages.

**Source:** "Der Bitcoin Podcast" by Florian Bruce Boye (YouTube + RSS)

## Pipeline Stages

```
detect -> download -> transcribe -> chunk -> generate
  RSS      yt-dlp     Whisper API   FTS5     Claude Sonnet
```

Each episode produces 6 Turkish content artifacts:
- `outline.tr.md` - Episode outline with citations
- `script.long.tr.md` - Full YouTube video script
- `shorts.tr.json` - 6 short-form video scripts
- `visuals.json` - Visual/slide descriptions
- `qa.json` - Q&A pairs for community posts
- `publishing_pack.json` - Titles, descriptions, tags, thumbnails

## Quickstart

```bash
# 1. Clone and setup
cd /home/pi/AI-Startup-Lab/bitcoin-education
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
# Edit .env with your API keys:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
#   PODCAST_YOUTUBE_CHANNEL_ID=UC...

# 3. Initialize database
btcedu init-db

# 4. Run database migrations (if updating from older version)
btcedu migrate

# 5. Run the pipeline
btcedu run-latest
```

## Updating to a New Version

After pulling new code, always run migrations to update the database schema:

```bash
git pull
btcedu migrate
sudo systemctl restart btcedu-web  # If using systemd
```

Check migration status at any time:

```bash
btcedu migrate-status
```

## CLI Commands

### Automation (cron-ready)

| Command | Description |
|---------|-------------|
| `btcedu run-latest` | Detect + process newest pending episode |
| `btcedu run-pending --max N --since YYYY-MM-DD` | Process all pending episodes |
| `btcedu retry --episode-id ID` | Retry failed episodes from last successful stage |

### Manual operations

| Command | Description |
|---------|-------------|
| `btcedu detect` | Check RSS feed for new episodes (~15 most recent) |
| `btcedu backfill` | Import full channel history via yt-dlp (see below) |
| `btcedu download --episode-id ID` | Download audio |
| `btcedu transcribe --episode-id ID` | Transcribe via Whisper |
| `btcedu chunk --episode-id ID` | Chunk transcript + FTS5 index |
| `btcedu generate --episode-id ID [--force] [--top-k 16]` | Generate content |
| `btcedu run [--episode-id ID] [--force]` | Run full pipeline |

### Monitoring

| Command | Description |
|---------|-------------|
| `btcedu status` | Episode counts by status + last 10 episodes |
| `btcedu cost [--episode-id ID]` | API usage costs breakdown |
| `btcedu report --episode-id ID` | Show latest pipeline report |
| `btcedu journal [--tail N]` | Show project progress log |
| `btcedu migrate-status` | Check database migration status |

### Database Management

| Command | Description |
|---------|-------------|
| `btcedu init-db` | Initialize database (create tables) |
| `btcedu migrate` | Run pending database migrations |
| `btcedu migrate --dry-run` | Preview migrations without applying |
| `btcedu migrate-status` | Show applied and pending migrations |

All automation commands exit with code 0 on success, 1 on any failure.

## Pipeline Actions Explained

### detect

Fetches the RSS feed and inserts new episodes into the database with status `new`. Does not download or process anything. Fast (network I/O only).

### download / transcribe / chunk / generate

Individual pipeline stages. Each runs only its specific step:

| Stage | What it does | Status after |
|-------|-------------|--------------|
| **download** | Downloads audio via yt-dlp | `downloaded` |
| **transcribe** | Sends audio to Whisper API | `transcribed` |
| **chunk** | Splits transcript into overlapping searchable chunks (FTS5) | `chunked` |
| **generate** | Calls Claude to produce 6 Turkish content artifacts | `generated` |

Use `--force` (CLI) or the force checkbox (UI) to re-run a stage even if its output already exists.

### run (Run All)

Runs the full pipeline from the **earliest incomplete stage**:
1. Examines the episode's current status
2. Logs a pipeline plan showing what will run and what will skip
3. Skips stages that are already completed
4. Runs remaining stages in order
5. Stops on first failure, records error

**Idempotent**: running on a `generated` episode does nothing.
**Force mode**: re-runs all stages regardless of current status.

Example: episode at `downloaded` → skips download, runs transcribe → chunk → generate.

### retry

Resumes from the **last failed stage**:
1. Requires the episode to have an error (from a previous failure)
2. Clears the error
3. Re-runs the pipeline from the current status (last successful stage)

Example: episode at `chunked` with "generate failed" error → clears error, skips download/transcribe/chunk, runs generate.

If the episode has no error, retry returns "Nothing to retry. Use 'run' instead."

## Backfill Full Channel History

YouTube's RSS feed only returns the **~15 most recent videos**. To import older videos, use `btcedu backfill` which uses yt-dlp to list all videos from the channel.

```bash
# Import all videos from the channel
btcedu backfill

# Preview what would be imported (no DB changes)
btcedu backfill --dry-run

# Only videos from 2024 onwards
btcedu backfill --since 2024-01-01

# Only videos before 2024
btcedu backfill --until 2023-12-31

# Import at most 10 new videos
btcedu backfill --max 10

# Combine filters
btcedu backfill --since 2023-01-01 --until 2024-06-30 --max 20
```

**How it works:**
- Runs `yt-dlp --flat-playlist -J` on the channel URL (no audio download)
- Parses JSON output for video ID, title, and upload date
- Inserts new episodes with status `new` (idempotent — existing episodes are never modified)
- After backfill, use `btcedu run-pending` or the dashboard to process the new episodes

**detect vs backfill:**
| | `btcedu detect` | `btcedu backfill` |
|---|---|---|
| Source | YouTube RSS feed | yt-dlp channel listing |
| Speed | ~1 second | 10-30 seconds |
| Coverage | ~15 most recent | All videos |
| Use case | Cron / incremental | One-off / catch-up |

## Web Dashboard

A lightweight local web GUI for monitoring episodes and triggering pipeline actions.

```bash
# Install web dependencies
pip install -e ".[web]"

# Start on localhost (default)
btcedu web

# Bind to LAN (accessible from other devices)
btcedu web --host 0.0.0.0 --port 5000
```

Features:
- Episode table with status badges, file presence indicators, and filters
- Per-episode actions: download, transcribe, chunk, generate (with dry-run/force toggles)
- Content viewer with tabs: transcript DE, outline TR, script TR, QA, publishing, report
- Global actions: detect new episodes, cost summary
- "What's new" bar: new episodes, failed episodes, incomplete pipelines

The dashboard reads settings from `.env` server-side only (no secrets exposed to the browser).

## Progress Log

Development progress is tracked in `docs/PROGRESS_LOG.md`. This append-only journal records plans, implementation milestones, and how to resume work in a new session.

```bash
# View recent entries
btcedu journal --tail 30
```

To continue development in a new Claude session, paste the latest journal section as context.

## Configuration

All settings are loaded from `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `OPENAI_API_KEY` | required | OpenAI/Whisper API key |
| `PODCAST_YOUTUBE_CHANNEL_ID` | required | YouTube channel for RSS |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Model for content generation |
| `DRY_RUN` | `false` | Write prompts to disk without API calls |
| `CHUNK_SIZE` | `1500` | Characters per chunk (~350 tokens) |
| `CLAUDE_MAX_TOKENS` | `4096` | Max output tokens per artifact |

## Production Deployment (systemd)

```bash
# Copy unit files
sudo cp deploy/btcedu-detect.service /etc/systemd/system/
sudo cp deploy/btcedu-detect.timer /etc/systemd/system/
sudo cp deploy/btcedu-run.service /etc/systemd/system/
sudo cp deploy/btcedu-run.timer /etc/systemd/system/

# Enable and start timers
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-detect.timer
sudo systemctl enable --now btcedu-run.timer

# Check timer status
systemctl list-timers btcedu-*

# View logs
journalctl -u btcedu-detect --since today
journalctl -u btcedu-run --since today
```

Schedule: detect every 6h, run-pending daily at 02:00.

## Public Dashboard Deployment (DuckDNS + HTTPS)

Expose the web dashboard publicly via Caddy reverse proxy with automatic TLS.

**Architecture:** `Internet → DuckDNS → Router → Caddy (:443) → Gunicorn (:8091)`

### Quick setup

```bash
# 1. Install gunicorn
.venv/bin/pip install 'gunicorn>=22.0.0'

# 2. Install and start the systemd service
sudo cp deploy/btcedu-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcedu-web.service

# 3. Generate a password hash for basic auth
caddy hash-password --plaintext 'YOUR_PASSWORD'

# 4. Edit /etc/caddy/Caddyfile - add inside lnodebtc.duckdns.org { }:
#    @dashboard path /dashboard/*
#    handle @dashboard {
#        uri strip_prefix /dashboard
#        basicauth {
#            pi PASTE_HASH_HERE
#        }
#        reverse_proxy 127.0.0.1:8091
#    }
#    header {
#        X-Content-Type-Options nosniff
#        X-Frame-Options DENY
#        Referrer-Policy no-referrer
#    }

# 5. Reload Caddy
sudo systemctl reload caddy
```

Dashboard will be at: **https://lnodebtc.duckdns.org/dashboard/**

### Change password

```bash
caddy hash-password --plaintext 'NEW_PASSWORD'
# Edit /etc/caddy/Caddyfile, replace the hash, then:
sudo systemctl reload caddy
```

### Monitoring

```bash
sudo systemctl status btcedu-web
sudo journalctl -u btcedu-web -f

# Health check
curl -u pi:PASSWORD https://lnodebtc.duckdns.org/dashboard/api/health
```

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| HTML loads but JS non-functional | Absolute `/static/` or `/api/` paths bypass proxy | Use relative paths (no leading `/`) |
| 401 on all requests | Basic auth credentials wrong | Re-generate hash: `caddy hash-password` |
| 502 Bad Gateway | Gunicorn not running | `sudo systemctl restart btcedu-web` |
| `/dashboard` gives 404 | Missing trailing slash redirect | Add `redir /dashboard /dashboard/ permanent` |

### Security

- Gunicorn binds to `127.0.0.1` only (never exposed directly)
- Basic auth required for all dashboard routes
- HTTPS with auto-renewing Let's Encrypt certificates (managed by Caddy)
- Security headers: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
- No API keys or secrets exposed to the browser

## Cost

~$0.38 per episode (6 Claude Sonnet calls with ~5k input / ~1.5k output tokens each).

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Development

### Running Checks Locally

Before committing, run these checks to ensure code quality:

```bash
# Activate virtual environment
source .venv/bin/activate

# Run linter
ruff check btcedu/ tests/

# Auto-fix linting issues
ruff check btcedu/ tests/ --fix

# Check code formatting
ruff format --check btcedu/ tests/

# Format code
ruff format btcedu/ tests/

# Run tests
ANTHROPIC_API_KEY=dummy OPENAI_API_KEY=dummy WHISPER_API_KEY=dummy DRY_RUN=true pytest tests/ -v
```

### Pre-commit Hooks (Optional)

Install pre-commit hooks to automatically run checks before each commit:

```bash
pip install pre-commit
pre-commit install
```

Now checks will run automatically on `git commit`. To run manually on all files:

```bash
pre-commit run --all-files
```

## System Requirements

- Python 3.12+
- ffmpeg (for audio splitting via pydub)
- SQLite with FTS5 support (standard on most systems)

## Project Structure

```
btcedu/
  cli.py              # Click CLI commands
  config.py           # Pydantic settings
  db.py               # SQLAlchemy engine/session
  core/
    detector.py       # RSS detection + download
    transcriber.py    # Whisper transcription + chunking
    chunker.py        # Text chunking + FTS5
    generator.py      # Claude content generation
    pipeline.py       # Pipeline orchestration + retry
  models/
    episode.py        # Episode, PipelineRun, Chunk ORM
    content_artifact.py # ContentArtifact ORM
    schemas.py        # Pydantic schemas
  services/
    feed_service.py   # RSS feed parsing
    download_service.py # yt-dlp wrapper
    transcription_service.py # Whisper API wrapper
    claude_service.py # Claude API wrapper
  utils/
    journal.py        # Progress log utility
  web/
    app.py            # Flask app factory
    api.py            # REST API endpoints
    templates/        # Jinja2 HTML
    static/           # JS + CSS
  prompts/
    system.py         # Shared Turkish system prompt
    outline.py        # Outline prompt template
    script.py         # Script prompt template
    shorts.py         # Shorts prompt template
    visuals.py        # Visuals prompt template
    qa.py             # Q&A prompt template
    publishing.py     # Publishing pack template
deploy/
  btcedu-detect.*     # systemd units for detection
  btcedu-run.*        # systemd units for processing
  btcedu-web.service  # systemd unit for web dashboard
  Caddyfile.dashboard # Caddy config snippet
  setup-web.sh        # Deployment helper script
tests/
data/
  btcedu.db           # SQLite database
  raw/                # Downloaded audio files
  transcripts/        # Whisper transcripts
  chunks/             # Chunk JSONL files
  outputs/            # Generated content artifacts
  reports/            # Pipeline run reports
  chromadb/           # ChromaDB (optional)
```
