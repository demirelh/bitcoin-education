# btcedu - Bitcoin Education Automation

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

# 4. Run the pipeline
btcedu run-latest
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
| `btcedu detect` | Check RSS feed for new episodes |
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

All automation commands exit with code 0 on success, 1 on any failure.

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

**Architecture:** `Internet → DuckDNS → Router → Caddy (:443) → Gunicorn (:8090)`

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
#    handle_path /dashboard/* {
#        basic_auth {
#            pi PASTE_HASH_HERE
#        }
#        reverse_proxy 127.0.0.1:8090
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
```

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

## System Requirements

- Python 3.11+
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
