# btcedu — Bitcoin Education Video Pipeline

[![CI](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml/badge.svg)](https://github.com/demirelh/bitcoin-education/actions/workflows/ci.yml)

Automated pipeline that transforms German podcast episodes into Turkish YouTube videos.

**Stack:** Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings.
Deployed on Raspberry Pi via systemd timers + Caddy reverse proxy.

## Pipeline

New profiles use pipeline v2. Existing database rows with `pipeline_version=1`
remain readable and can complete through the legacy compatibility path.

**v2 pipeline (current):**
```
download → transcribe → transcript_analyze → transcript_verify → correct →
transcript_qa → review_gate_transcript_qa → review_gate_1 → segment →
translate → adapt → review_gate_2 (deterministic + independent LLM QA) →
chapterize → frameextract → imagegen → tts → anchorgen → render →
review_gate_3 → publish
```

Transcript and translation QA use structured findings and GREEN/YELLOW/RED
decisions. Critical findings stop before downstream production. A GREEN
translation gate stores the approved narration SHA-256; chapterize may split
the narration but cannot rewrite it. Review gates create artifact-bound
`ReviewTask` records and resume after approval.

`tagesschau_tr` uses selective secondary transcription only for suspicious
segments, conditional fact-preserving adaptation, profile-routed image/TTS
providers, and `auto_publish: false`.

`tagesschau_tr` is fed by the local `ard-recorder` capture
(`/mnt/photo-backup/tagesschau/recordings/`), which is ready about twenty
minutes after the broadcast — one to two hours before the same broadcast is
uploaded to YouTube. The YouTube feed remains the fallback and is used unchanged
whenever no local recording exists. Both ingest directions are deduplicated on
the broadcast date so a broadcast is never processed twice. See
[docs/local-recorder-ingest.md](docs/local-recorder-ingest.md).

Tagesschau weather chapters use a grounded local HTML/SVG renderer with timed
FFmpeg scenes instead of generative weather images. See
[docs/weather-renderer.md](docs/weather-renderer.md).

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
| `btcedu transcript-analyze --episode-id ID` | Flag suspicious segments deterministically |
| `btcedu transcript-verify --episode-id ID [--dry-run]` | Re-transcribe suspicious regions |
| `btcedu correct --episode-id ID` | LLM transcript correction |
| `btcedu transcript-qa --episode-id ID` | Run the deterministic transcript gate |
| `btcedu translate --episode-id ID` | German → Turkish translation |
| `btcedu adapt --episode-id ID` | Cultural adaptation |
| `btcedu translation-qa --episode-id ID` | Run deterministic per-story translation checks |
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

Features: episode status, transcript/translation QA decisions, findings by
severity and story, source/target excerpts, retry generation, provider/model
and cost, finding lifecycle actions, transcript approval/change requests,
structured restart actions, review management, content inspection, cost
summary, and per-channel profiles.

Production: gunicorn with gthread worker behind Caddy reverse proxy.
See `deploy/` for systemd units and Caddy config.

## Configuration

All settings from `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `OPENAI_API_KEY` | required | OpenAI/Whisper API key |
| `ELEVENLABS_API_KEY` | required (v2) | ElevenLabs TTS key |
| `TRANSCRIPTION_SECONDARY_ENABLED` | `false` | Global secondary-ASR default |
| `TRANSCRIPT_QA_ENABLED` | `true` | Enable deterministic transcript QA |
| `QA_REVIEW_ENABLED` | `true` | Enable independent translation QA globally |
| `QA_MODEL` | `gpt-5.6-sol` | Global fallback QA model |
| `DEFAULT_CONTENT_PROFILE` | `bitcoin_podcast` | Profile for newly detected episodes |
| `GEMINI_API_KEY` | `""` | Gemini API key (for frame editing) |
| `GEMINI_IMAGE_EDIT_ENABLED` | `true` | Enable Gemini frame editing for tagesschau |
| `PIPELINE_VERSION` | `2` | Pipeline version for newly created episodes |
| `DRY_RUN` | `false` | Skip API calls, write placeholders |
| `MAX_EPISODE_COST_USD` | `10` in `.env.example` | Guard checked before paid calls |
| `RENDER_PRESET` | `medium` | ffmpeg encoding preset |
| `RENDER_TIMEOUT_SEGMENT` | `300` | ffmpeg segment timeout (seconds) |
| `DATABASE_URL` | `sqlite:///data/btcedu.db` | Database connection |

Full settings: `btcedu/config.py`.

On the Raspberry Pi production host, `RENDER_PRESET=ultrafast` and
`RENDER_TIMEOUT_SEGMENT=900` are recommended operational tuning values; they
are not the generic defaults above.

Precedence is: code defaults → `.env`/environment → explicit `Settings(...)`
values → profile-owned stage/provider settings → supported CLI overrides.
Profile YAML is authoritative only for fields owned by that profile.

## Data and QA artifacts

Per-episode files are stored below `data/transcripts/{episode_id}/` and
`data/outputs/{episode_id}/`. Important artifacts include:

- `transcript.structured.de.json`, `transcript_analysis.json`, and
  `transcript_verification.json`
- `transcript.corrected.structured.de.json` and
  `transcript/transcript_qa.json`
- `stories.json`, `stories_translated.json`, and `stories_adapted.json`
- `translation_qa.json` and `translation_quality_gate.json`
- `chapters.json`, media/TTS manifests, render provenance, and review history

Legacy text artifacts remain available. Content hashes, provenance files, and
`.stale` markers make reruns idempotent and invalidate only affected
downstream stages.

## Deployment

```bash
./run.sh   # git pull → pip install → migrate → restart services
```

`run.sh` refuses to deploy while the working tree has uncommitted changes;
commit or discard them first, or stash them explicitly with `ALLOW_DIRTY=1
./run.sh`.

See `deploy/README.md` for systemd timer setup and Caddy reverse proxy config.
For `tagesschau_tr`, publishing still requires a final artifact-bound manual
approval; intermediate approvals never upload automatically.

## Development

```bash
pip install -e ".[dev,web]"
pytest
ruff check .
ruff format --check .
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
