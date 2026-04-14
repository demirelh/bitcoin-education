# btcedu — Bitcoin Education Video Pipeline

[![CI](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml)

Automated pipeline that transforms German podcast episodes into Turkish YouTube videos.

**Stack:** Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings.
Deployed on Raspberry Pi via systemd timers + Caddy reverse proxy.

## Pipeline

Two pipeline versions coexist (`pipeline_version` config: 1 = v1 legacy, 2 = v2 current).

**v2 pipeline (current):**
```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review] →
TRANSLATED → ADAPTED → [review] → CHAPTERIZED →
IMAGES_GENERATED → TTS_DONE → RENDERED → [review] → APPROVED → PUBLISHED
```

Review gates pause the pipeline and create ReviewTask records.
Approval (via CLI or web dashboard) resumes processing.

**imagegen dispatch:** tagesschau_tr episodes use Gemini 2.0 Flash frame editing (~$0.003/image);
all others use Pexels stock images.

## Quickstart

```bash
# Clone and setup
git clone https://github.com/demirelh/bitcoin-education.git
cd bitcoin-education
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,web]"

# Configure
cp .env.example .env   # edit with your API keys

# Initialize and run
btcedu init-db
btcedu detect
btcedu run-latest
```

## CLI Commands

### Pipeline

| Command | Description |
|---------|-------------|
| `btcedu run --episode-id ID` | Run full pipeline for episode |
| `btcedu run-latest` | Detect + process newest pending episode |
| `btcedu run-pending --max N` | Process all pending episodes |
| `btcedu retry --episode-id ID` | Retry from last failed stage |

### Individual Stages

| Command | Description |
|---------|-------------|
| `btcedu detect` | Check feeds for new episodes |
| `btcedu backfill` | Import full channel history via yt-dlp |
| `btcedu download --episode-id ID` | Download audio |
| `btcedu transcribe --episode-id ID` | Transcribe via Whisper |
| `btcedu correct --episode-id ID` | LLM transcript correction |
| `btcedu translate --episode-id ID` | German → Turkish translation |
| `btcedu adapt --episode-id ID` | Cultural adaptation |
| `btcedu chapterize --episode-id ID` | Production chapter JSON |
| `btcedu imagegen --episode-id ID` | Generate/select images |
| `btcedu frame-edit --episode-id ID` | Gemini frame editing (tagesschau) |
| `btcedu tts --episode-id ID` | Text-to-speech (ElevenLabs) |
| `btcedu render --episode-id ID` | Assemble video (ffmpeg) |
| `btcedu publish --episode-id ID` | Upload to YouTube |

### Monitoring & Review

| Command | Description |
|---------|-------------|
| `btcedu status` | Episode counts by status |
| `btcedu cost [--episode-id ID]` | API cost breakdown |
| `btcedu report --episode-id ID` | Pipeline run report |
| `btcedu review list` | List pending reviews |
| `btcedu review approve ID` | Approve a review gate |
| `btcedu review reject ID --notes "..."` | Reject with feedback |
| `btcedu migrate-status` | Database migration status |

## Web Dashboard

```bash
pip install -e ".[web]"
btcedu web                           # localhost:5000
btcedu web --host 0.0.0.0 --port 5000  # LAN access
```

Features: episode table with status badges, per-episode pipeline actions,
content viewer (transcript, script, chapters, images), review management,
cost summary, batch processing. Channel management with per-channel content profiles.

Production: gunicorn with gthread worker behind Caddy reverse proxy.
See `deploy/` for systemd units and Caddy config.

## Configuration

All settings from `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `OPENAI_API_KEY` | required | OpenAI/Whisper API key |
| `ELEVENLABS_API_KEY` | required (v2) | ElevenLabs TTS key |
| `GEMINI_API_KEY` | `""` | Gemini API key (for frame editing) |
| `GEMINI_IMAGE_EDIT_ENABLED` | `true` | Enable Gemini frame editing for tagesschau |
| `PIPELINE_VERSION` | `2` | Pipeline version (1 or 2) |
| `DRY_RUN` | `false` | Skip API calls, write placeholders |
| `MAX_EPISODE_COST_USD` | `10` | Cost limit per episode |
| `RENDER_PRESET` | `medium` | ffmpeg encoding preset |
| `RENDER_TIMEOUT_SEGMENT` | `300` | ffmpeg segment timeout (seconds) |
| `DATABASE_URL` | `sqlite:///data/btcedu.db` | Database connection |

Full settings: `btcedu/config.py`.

## Deployment

```bash
./run.sh   # git pull → pip install → migrate → restart services
```

See `deploy/README.md` for systemd timer setup and Caddy reverse proxy config.

## Development

```bash
pip install -e ".[dev,web]"
pytest                               # full test suite (~1189 tests)
ruff check btcedu/ tests/            # lint
ruff format btcedu/ tests/           # format
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards and workflow.

## System Requirements

- Python 3.12+
- ffmpeg (for audio/video processing)
- SQLite with FTS5 support

## Documentation

| Document | Description |
|----------|-------------|
| [CLAUDE.md](CLAUDE.md) | Claude Code project context |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and standards |
| [docs/architecture/](docs/architecture/) | Pipeline, review gate, render flow docs |
| [docs/architecture/MASTERPLAN.md](docs/architecture/MASTERPLAN.md) | Original design document (historical) |
| [docs/decisions/](docs/decisions/) | Architecture decision records |
| [docs/runbooks/](docs/runbooks/) | Operator guides |
| [deploy/](deploy/) | Deployment config and docs |
