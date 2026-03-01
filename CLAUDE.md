# btcedu — Bitcoin Education Video Pipeline

## Project Summary

Automated pipeline converting German Bitcoin podcast episodes into Turkish YouTube videos.
**Stack**: Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy + SQLite + FTS5, Pydantic settings.
**Deployed** on Raspberry Pi via systemd timers + Caddy reverse proxy.

## Pipeline Architecture

Two pipeline versions coexist. `pipeline_version` config (default=1) controls which flow runs. v1 episodes remain valid; no migration needed.

### v1 Pipeline (legacy, still operational)
```
NEW → DOWNLOADED → TRANSCRIBED → CHUNKED → GENERATED → REFINED → COMPLETED
```

### v2 Pipeline (MASTERPLAN — active development)
```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review_gate_1] →
TRANSLATED → ADAPTED → [review_gate_2] → CHAPTERIZED →
IMAGES_GENERATED → TTS_DONE → RENDERED → APPROVED → PUBLISHED
```

### Sprint Status
| Sprint | Phase | Scope | Status |
|--------|-------|-------|--------|
| 1 | Foundation | Enums, migrations 001-004, PromptVersion, ReviewTask, PromptRegistry, config | Complete |
| 2 | Phase 1 | Transcript Correction (`corrector.py`, prompt template, CLI, pipeline v2 branching) | Complete |
| 3 | Phase 1 | Review System + Dashboard (reviewer.py, review queue, diff viewer, Review Gate 1) | Complete |
| 4 | Phase 2 | Translation (`translator.py`, German→Turkish, idempotency, provenance) | Complete |
| 5 | Phase 2 | Adaptation (`adapter.py`, cultural neutralization, Review Gate 2) | Complete |
| 6 | Phase 3 | Chapterization (`chapterizer.py`, production JSON, chapter viewer) | Complete |
| 7 | Phase 3 | Image Generation (`image_generator.py`, image_gen_service, DALL-E 3) | Complete |
| 8 | Phase 4 | TTS (`tts.py`, elevenlabs_service, per-chapter MP3, audio preview) | Complete |
| 9 | Phase 5 | Video Rendering (`renderer.py`, ffmpeg_service, draft MP4 generation) | Complete |
| 10 | Phase 5 | Video Polish (transitions, mixing, thumbnails) | Next |
| 11 | Phase 6 | YouTube Publishing | Future |

## Directory Layout

```
btcedu/
├── cli.py                    # Click CLI (~20 commands)
├── config.py                 # Pydantic BaseSettings from .env
├── db.py                     # SQLAlchemy engine, Base, session factory
├── core/
│   ├── pipeline.py           # Orchestration: _V1_STAGES, _V2_STAGES, run_pending, run_latest
│   ├── detector.py           # YouTube RSS feed scanning
│   ├── transcriber.py        # Whisper API transcription + chunking
│   ├── corrector.py          # LLM transcript correction with diff
│   ├── translator.py         # German → Turkish translation
│   ├── adapter.py            # Cultural adaptation (T1/T2 rules)
│   ├── chapterizer.py        # Production chapter JSON
│   ├── image_generator.py    # Image generation orchestration
│   ├── tts.py                # TTS orchestration (ElevenLabs)
│   ├── reviewer.py           # Review task management
│   └── generator.py          # Legacy v1 content generation
├── services/
│   ├── claude_service.py     # Anthropic API (call_claude pattern)
│   ├── transcription_service.py
│   ├── image_gen_service.py  # DALL-E 3 image generation
│   ├── elevenlabs_service.py # ElevenLabs TTS REST API
│   ├── feed_service.py
│   └── download_service.py
├── models/
│   ├── episode.py            # Episode, PipelineRun, Chunk, EpisodeStatus enum
│   ├── content_artifact.py   # ContentArtifact (per-stage output tracking)
│   ├── media_asset.py        # MediaAsset (images, audio, video) — uses own Base!
│   ├── review.py             # ReviewTask, ReviewDecision, ReviewStatus
│   ├── chapter_schema.py     # ChapterDocument, Chapter, Narration, Visual (Pydantic)
│   └── channel.py            # Channel model
├── prompts/
│   ├── templates/            # YAML frontmatter + Jinja2 body (correct_transcript.md, etc.)
│   ├── registry.py           # PromptRegistry (load, hash, register)
│   └── *.py                  # Legacy prompt builders (kept for v1 compat)
├── web/
│   ├── api.py                # Flask API blueprint (api_bp)
│   ├── jobs.py               # Background JobManager
│   ├── app.py                # Flask app factory
│   └── static/               # app.js, styles.css (SPA dashboard)
└── migrations/               # Abstract Migration class, MIGRATIONS list
```

### Data Layout
```
data/
├── raw/{ep_id}/audio.m4a
├── transcripts/{ep_id}/
│   ├── transcript.de.txt
│   ├── transcript.corrected.de.txt
│   └── transcript.tr.txt
├── outputs/{ep_id}/
│   ├── script.adapted.tr.md
│   ├── chapters.json
│   ├── images/{chapter_id}.png + manifest.json
│   ├── tts/{chapter_id}.mp3 + manifest.json
│   ├── render/draft.mp4 + render_manifest.json (Sprint 9-10)
│   ├── review/correction_diff.json, adaptation_diff.json
│   └── provenance/*.json
└── btcedu.db
```

## Code Patterns

### Models
- SQLAlchemy 2.0: `Mapped[]` / `mapped_column()` with `Base` from `btcedu/db.py`
- **Exception**: `MediaAsset` uses its own `declarative_base()` (not `btcedu.db.Base`)
- Datetime: `datetime.now(UTC)` via `_utcnow()` helper

### Migrations
- Abstract `Migration` class in `btcedu/migrations/__init__.py`
- `MIGRATIONS` list, check-before-act idempotency, `self.mark_applied(session)` at end

### Config
- Pydantic `BaseSettings` in `btcedu/config.py`, reads from `.env`
- Key fields: `pipeline_version`, `max_episode_cost_usd`, `dry_run`, API keys

### Stage Implementation Pattern
Each v2 stage follows the same structure (see `image_generator.py` or `tts.py` as reference):
1. Service layer (`services/`) — external API wrapper with retry, Protocol for future providers
2. Core module (`core/`) — orchestration with `generate_X(session, episode_id, settings, force)` signature
3. Idempotency via content hash (SHA-256) + provenance file + `.stale` markers
4. Partial recovery (skip unchanged chapters on re-run)
5. Cost guard (check cumulative episode cost against `max_episode_cost_usd`)
6. Dry-run support (placeholders instead of API calls)
7. `PipelineRun` record (stage, status, cost, timestamps)
8. `ContentArtifact` record (artifact_type, model, prompt_hash)
9. `MediaAsset` record for media outputs (images, audio)
10. Downstream invalidation via `.stale` marker files

### Pipeline Integration
- `_V2_STAGES` list in `pipeline.py`: `[(stage_name, required_status), ...]`
- `_run_stage()`: lazy import of stage function, returns `StageResult`
- Review gates: check `has_approved_review()` / `has_pending_review()`, create `ReviewTask` if needed
- `run_pending()` / `run_latest()`: filter by actionable statuses, skip episodes with pending reviews

### Tests
- pytest, in-memory SQLite via `db_engine` / `db_session` fixtures in `conftest.py`
- `MediaAsset` tests need `prompt_versions` table + `MediaBase.metadata.create_all(engine)`
- Mock external APIs (no real API calls in tests)
- pydub tests: use `patch.dict(sys.modules, {"pydub": mock_pydub})` for Python 3.13 compat

### Dashboard
- Flask SPA: single-page app with tabs per episode
- `app.js`: tab loading functions (`loadTab`, `loadTTSPanel`, `loadImagesPanel`, etc.)
- `api_bp`: REST endpoints for data + job triggering
- `JobManager`: background job execution with polling

## Key Design Decisions

1. **Raw HTTP for external APIs** (no SDKs) — ElevenLabs, image gen use `requests` directly
2. **Cascade invalidation**: upstream changes write `.stale` markers on downstream outputs; TTS and images are parallel dependencies of RENDER (image changes don't invalidate TTS and vice versa)
3. **Review gates block pipeline**: pipeline pauses, creates `ReviewTask`, resumes on approval
4. **Prompt versioning**: file-based templates with YAML frontmatter, SHA-256 hashing, PromptRegistry
5. **v1/v2 coexistence**: `pipeline_version` field distinguishes; v1 code untouched

## Known Gotchas

- **MediaAsset uses own Base**: `MediaBase` from `media_asset.py`, not `btcedu.db.Base`. Tests must create `prompt_versions` table in `MediaBase.metadata` and call `MediaBase.metadata.create_all(engine)` separately.
- **pydub + Python 3.13**: `audioop` module removed in 3.13. Tests mock via `sys.modules`. Production may need `pyaudioop` package.
- **Chapter schema**: `Chapter.visual` is singular (`Visual`), `Chapter.narration` has `.text`, `.word_count`, `.estimated_duration_seconds`
- **Lazy imports**: Stage functions are lazy-imported inside `_run_stage()` and `generate_*()` to avoid circular imports
- **Cost extraction**: `run_episode_pipeline()` parses cost from `StageResult.detail` string (splits on `$`)

## Reference Documents

For detailed specifications beyond this summary:
- `MASTERPLAN.md` — Full architecture, schemas, phase roadmap, prompt strategy
- `docs/sprints/` — Per-sprint plan/validation/implementation docs
- `docs/sprints/outputs/` — Sprint implementation and validation outputs
