# btcedu — Bitcoin Education Video Pipeline

## Project Summary

Automated pipeline converting German Bitcoin podcast episodes into Turkish YouTube videos.
**Stack**: Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings.
**Deployed** on Raspberry Pi via systemd timers + Caddy reverse proxy.
**Entry point**: `btcedu = "btcedu.cli:cli"` (pyproject.toml).
**All 11 sprints are complete.** The full v2 pipeline is operational.

## Pipeline Architecture

Two pipeline versions coexist. `pipeline_version` config (default=1) controls which flow runs.

### v1 Pipeline (legacy, still operational)
```
NEW → DOWNLOADED → TRANSCRIBED → CHUNKED → GENERATED → REFINED → COMPLETED
```

### v2 Pipeline (complete)
```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review_gate_1] →
TRANSLATED → ADAPTED → [review_gate_2] → CHAPTERIZED →
IMAGES_GENERATED → TTS_DONE → RENDERED → [review_gate_3] → APPROVED → PUBLISHED
```

### _V2_STAGES (pipeline.py)
```python
_V2_STAGES = [
    ("download",      EpisodeStatus.NEW),
    ("transcribe",    EpisodeStatus.DOWNLOADED),
    ("correct",       EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate",     EpisodeStatus.CORRECTED),      # after review approved
    ("adapt",         EpisodeStatus.TRANSLATED),
    ("review_gate_2", EpisodeStatus.ADAPTED),
    ("chapterize",    EpisodeStatus.ADAPTED),         # after review approved
    ("imagegen",      EpisodeStatus.CHAPTERIZED),
    ("tts",           EpisodeStatus.IMAGES_GENERATED),
    ("render",        EpisodeStatus.TTS_DONE),
    ("review_gate_3", EpisodeStatus.RENDERED),
    ("publish",       EpisodeStatus.APPROVED),
]
```

## Directory Layout

```
btcedu/
├── cli.py                    # Click CLI (30 commands)
├── config.py                 # Pydantic BaseSettings from .env
├── db.py                     # SQLAlchemy engine, Base, session factory, FTS5
├── core/
│   ├── pipeline.py           # Orchestration: _V1_STAGES, _V2_STAGES, StageResult, PipelineReport
│   ├── detector.py           # RSS feed scanning + download_episode
│   ├── transcriber.py        # Whisper API transcription + chunk_episode
│   ├── corrector.py          # LLM transcript correction (correct_transcript)
│   ├── translator.py         # German→Turkish (translate_transcript)
│   ├── adapter.py            # Cultural adaptation T1/T2 (adapt_script)
│   ├── chapterizer.py        # Production chapter JSON (chapterize_script)
│   ├── image_generator.py    # Per-chapter image gen (generate_images)
│   ├── tts.py                # Per-chapter TTS (generate_tts)
│   ├── renderer.py           # ffmpeg draft MP4 (render_video)
│   ├── publisher.py          # YouTube upload (publish_video)
│   ├── reviewer.py           # Review task CRUD + gate checks
│   ├── generator.py          # Legacy v1 content generation
│   └── prompt_registry.py    # PromptRegistry (load, hash, register)
├── services/
│   ├── claude_service.py     # call_claude(), ClaudeResponse, calculate_cost, compute_prompt_hash
│   ├── transcription_service.py  # transcribe_audio (Whisper)
│   ├── image_gen_service.py  # DallE3ImageService (Protocol: ImageGenService)
│   ├── elevenlabs_service.py # ElevenLabsService (Protocol: TTSService)
│   ├── youtube_service.py    # YouTubeDataAPIService, DryRunYouTubeService, authenticate, check_token_status
│   ├── feed_service.py       # fetch_feed, parse_feed, fetch_channel_videos_ytdlp
│   └── download_service.py   # download_audio (yt-dlp)
├── models/
│   ├── episode.py            # Episode, PipelineRun, Chunk, EpisodeStatus, PipelineStage, RunStatus
│   ├── content_artifact.py   # ContentArtifact
│   ├── media_asset.py        # MediaAsset, MediaAssetType — uses own declarative_base()!
│   ├── publish_job.py        # PublishJob, PublishJobStatus
│   ├── review.py             # ReviewTask, ReviewDecision, ReviewStatus
│   ├── prompt_version.py     # PromptVersion
│   ├── chapter_schema.py     # ChapterDocument, Chapter, Narration, Visual, Overlay, Transitions (Pydantic)
│   ├── channel.py            # Channel
│   ├── migration.py          # SchemaMigration
│   └── schemas.py            # Misc shared schemas
├── prompts/
│   ├── templates/            # YAML frontmatter + Jinja2: correct_transcript.md, translate.md, adapt.md, chapterize.md, imagegen.md, system.md
│   └── *.py                  # Legacy prompt builders (v1 compat)
├── web/
│   ├── app.py                # Flask app factory
│   ├── api.py                # api_bp blueprint (30+ REST endpoints)
│   ├── jobs.py               # JobManager background execution
│   └── static/               # app.js, styles.css (SPA dashboard)
├── utils/                    # Utility modules
└── migrations/               # Abstract Migration, MIGRATIONS list (6 migrations)
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
│   ├── render/draft.mp4 + render_manifest.json
│   ├── review/correction_diff.json, adaptation_diff.json
│   ├── publish/publish_provenance.json
│   └── provenance/*.json
├── client_secret.json            # YouTube OAuth client secrets
├── .youtube_credentials.json     # YouTube OAuth token (auto-created)
└── btcedu.db
```

## Enums

### EpisodeStatus (episode.py)
`NEW`, `DOWNLOADED`, `TRANSCRIBED`, `CHUNKED`, `GENERATED`, `REFINED`, `COMPLETED`, `FAILED`,
`CORRECTED`, `TRANSLATED`, `ADAPTED`, `CHAPTERIZED`, `IMAGES_GENERATED`, `TTS_DONE`, `RENDERED`, `APPROVED`, `PUBLISHED`, `COST_LIMIT`

### PipelineStage (episode.py)
`DETECT`, `DOWNLOAD`, `TRANSCRIBE`, `CHUNK`, `GENERATE`, `REFINE`, `COMPLETE`,
`CORRECT`, `TRANSLATE`, `ADAPT`, `CHAPTERIZE`, `IMAGEGEN`, `TTS`, `RENDER`, `REVIEW`, `PUBLISH`

### ReviewStatus (review.py)
`PENDING`, `IN_REVIEW`, `APPROVED`, `REJECTED`, `CHANGES_REQUESTED`

### PublishJobStatus (publish_job.py)
`PENDING`, `UPLOADING`, `PUBLISHED`, `FAILED`

### MediaAssetType (media_asset.py)
`IMAGE`, `AUDIO`, `VIDEO`

### VisualType (chapter_schema.py)
`TITLE_CARD`, `DIAGRAM`, `B_ROLL`, `TALKING_HEAD`, `SCREEN_SHARE`

## Models (SQLAlchemy 2.0)

All use `Mapped[]`/`mapped_column()` with `Base` from `btcedu/db.py`, **except MediaAsset**.

### Episode (episodes)
`id`, `episode_id` (unique), `channel_id`, `source`, `title`, `published_at`, `duration_seconds`, `url`, `status` (EpisodeStatus), `audio_path`, `transcript_path`, `output_dir`, `detected_at`, `completed_at`, `error_message`, `retry_count`, `pipeline_version` (default=1), `review_status`, `youtube_video_id`, `published_at_youtube`

### PipelineRun (pipeline_runs)
`id`, `episode_id` (FK episodes.id), `stage` (PipelineStage), `status` (RunStatus), `started_at`, `completed_at`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, `error_message`

### Chunk (chunks)
`id`, `chunk_id` (unique), `episode_id`, `ordinal`, `text`, `token_estimate`, `start_char`, `end_char`
FTS5 virtual table: `chunks_fts(chunk_id, episode_id, text)`

### ContentArtifact (content_artifacts)
`id`, `episode_id`, `artifact_type`, `file_path`, `model`, `prompt_hash`, `retrieval_snapshot_path`, `created_at`

### MediaAsset (media_assets) — **uses own `declarative_base()`**, not `btcedu.db.Base`
`id`, `episode_id`, `asset_type` (MediaAssetType), `chapter_id`, `file_path`, `mime_type`, `size_bytes`, `duration_seconds`, `meta` (JSON), `prompt_version_id` (FK prompt_versions.id), `created_at`

### ReviewTask (review_tasks)
`id`, `episode_id`, `stage`, `status`, `artifact_paths` (JSON), `diff_path`, `prompt_version_id` (FK), `created_at`, `reviewed_at`, `reviewer_notes`, `artifact_hash`
→ has `decisions` relationship to `ReviewDecision`

### ReviewDecision (review_decisions)
`id`, `review_task_id` (FK), `decision`, `notes`, `decided_at`

### PublishJob (publish_jobs)
`id`, `episode_id`, `status`, `youtube_video_id`, `youtube_url`, `metadata_snapshot` (JSON), `published_at`, `error_message`, `created_at`

### PromptVersion (prompt_versions)
`id`, `name`, `version`, `content_hash`, `template_path`, `model`, `temperature`, `max_tokens`, `is_default`, `created_at`, `notes`
Unique constraints: `(name, version)`, `(name, content_hash)`

### Channel (channels)
`id`, `channel_id` (unique), `name`, `youtube_channel_id`, `rss_url`, `is_active`, `created_at`, `updated_at`

### SchemaMigration (schema_migrations)
`id`, `version` (unique), `applied_at`

## Pydantic Models (chapter_schema.py)

```
ChapterDocument { schema_version, episode_id, title, total_chapters, estimated_duration_seconds, chapters: [Chapter] }
Chapter { chapter_id, title, order, narration: Narration, visual: Visual, overlays: [Overlay], transitions: Transitions, notes }
Narration { text, word_count, estimated_duration_seconds }
Visual { type: VisualType, description, image_prompt }  ← singular, not list
Overlay { type: OverlayType, text, start_offset_seconds, duration_seconds }
Transitions { in_transition (alias "in"), out_transition (alias "out") }
```
Validators: `ChapterDocument` validates total_chapters == len(chapters), sequential order, duration sum (±5s tolerance).
`Visual` validator: `DIAGRAM`/`B_ROLL` types require `image_prompt`.

## Config (Pydantic BaseSettings, .env)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `anthropic_api_key` | str | "" | |
| `openai_api_key` | str | "" | |
| `whisper_api_key` | str | "" | Falls back to openai_api_key |
| `claude_api_key` | str | "" | Deprecated alias → anthropic_api_key |
| `database_url` | str | "sqlite:///data/btcedu.db" | |
| `source_type` | str | "youtube_rss" | |
| `podcast_youtube_channel_id` | str | "" | |
| `podcast_rss_url` | str | "" | |
| `raw_data_dir` | str | "data/raw" | |
| `audio_format` | str | "m4a" | |
| `max_audio_chunk_mb` | int | 24 | |
| `transcripts_dir` | str | "data/transcripts" | |
| `whisper_model` | str | "whisper-1" | |
| `whisper_language` | str | "de" | |
| `chunks_dir` | str | "data/chunks" | |
| `chunk_size` | int | 1500 | chars |
| `chunk_overlap` | float | 0.15 | |
| `claude_model` | str | "claude-sonnet-4-20250514" | |
| `claude_max_tokens` | int | 4096 | |
| `claude_temperature` | float | 0.3 | |
| `max_retries` | int | 3 | |
| `dry_run` | bool | False | |
| `pipeline_version` | int | 1 | 1=v1, 2=v2 |
| `max_episode_cost_usd` | float | 10.0 | Per-episode safety cap |
| `image_gen_provider` | str | "dalle3" | |
| `image_gen_model` | str | "dall-e-3" | |
| `image_gen_size` | str | "1792x1024" | |
| `image_gen_quality` | str | "standard" | |
| `image_gen_style_prefix` | str | (long) | Prepended to prompts |
| `elevenlabs_api_key` | str | "" | |
| `elevenlabs_voice_id` | str | "" | |
| `elevenlabs_model` | str | "eleven_multilingual_v2" | |
| `elevenlabs_stability` | float | 0.5 | |
| `elevenlabs_similarity_boost` | float | 0.75 | |
| `elevenlabs_style` | float | 0.0 | |
| `elevenlabs_use_speaker_boost` | bool | True | |
| `render_resolution` | str | "1920x1080" | |
| `render_fps` | int | 30 | |
| `render_crf` | int | 23 | |
| `render_preset` | str | "medium" | |
| `render_audio_bitrate` | str | "192k" | |
| `render_font` | str | "NotoSans-Bold" | |
| `render_timeout_segment` | int | 300 | |
| `render_timeout_concat` | int | 600 | |
| `render_transition_duration` | float | 0.5 | |
| `youtube_client_secrets_path` | str | "data/client_secret.json" | |
| `youtube_credentials_path` | str | "data/.youtube_credentials.json" | |
| `youtube_default_privacy` | str | "unlisted" | unlisted/private/public |
| `youtube_upload_chunk_size_mb` | int | 10 | |
| `youtube_category_id` | str | "27" | Education |
| `youtube_default_language` | str | "tr" | |
| `outputs_dir` | str | "data/outputs" | |
| `reports_dir` | str | "data/reports" | |
| `logs_dir` | str | "data/logs" | |

## CLI Commands (`btcedu`) — 34 commands

| Command | Key Options | Purpose |
|---------|-------------|---------|
| `detect` | | Scan RSS feed for new episodes |
| `backfill` | `--max`, `--start/--end`, `--channel-id`, `--source` | Bulk insert episodes |
| `download` | `--episode-id` (multi), `--force` | Download audio files |
| `transcribe` | `--episode-id` (multi), `--force` | Whisper transcription |
| `chunk` | `--episode-id` (multi), `--force` | Split transcript into chunks (v1) |
| `generate` | `--episode-id` (multi), `--force`, `--top-k` | Generate content artifacts (v1) |
| `refine` | `--episode-id` (multi), `--force` | Refine content (v1) |
| `correct` | `--episode-id` (multi), `--force` | LLM transcript correction (v2) |
| `translate` | `--episode-id` (multi), `--force`, `--dry-run` | DE→TR translation (v2) |
| `adapt` | `--episode-id` (multi), `--force`, `--dry-run` | Cultural adaptation (v2) |
| `chapterize` | `--episode-id` (multi), `--force`, `--dry-run` | Produce chapter JSON (v2) |
| `imagegen` | `--episode-id` (multi), `--force`, `--dry-run` | Generate chapter images (v2) |
| `tts` | `--episode-id` (multi), `--force`, `--dry-run` | Generate chapter audio (v2) |
| `render` | `--episode-id` (multi), `--force`, `--dry-run` | Render draft MP4 (v2) |
| `publish` | `--episode-id` (multi), `--force`, `--dry-run`, `--privacy` | Upload to YouTube (v2) |
| `run` | `--episode-id` (multi), `--force` | Run full pipeline for episodes |
| `run-latest` | | Run pipeline on latest episode |
| `run-pending` | `--max`, `--since` | Process all actionable episodes |
| `retry` | `--episode-id` (multi) | Retry failed episodes |
| `status` | | Show episode status summary |
| `report` | `--episode-id` | Detailed episode report |
| `cost` | `--episode-id` (optional) | Show cost breakdown |
| `review` (group) | | Review management subcommands |
| `review list` | `--status` | List review tasks |
| `review approve` | `REVIEW_ID`, `--notes` | Approve review |
| `review reject` | `REVIEW_ID`, `--notes` | Reject review |
| `review request-changes` | `REVIEW_ID`, `--notes` | Request changes |
| `prompt` (group) | | Prompt version management |
| `prompt list` | `--name` | List all prompt versions |
| `prompt promote` | `VERSION_ID` | Promote prompt to default |
| `youtube-auth` | | Interactive OAuth flow |
| `youtube-status` | | Check YouTube credential status |
| `init-db` | | Create all tables |
| `migrate` | `--dry-run` | Run pending migrations |
| `migrate-status` | | Show migration status |
| `journal` | `--tail` | Show pipeline journal |
| `web` | `--host`, `--port`, `--production` | Start Flask dashboard |
| `llm-report` | `--json-only`, `--output` | LLM usage report |

## Migrations (6 total)

| Version | Description |
|---------|-------------|
| `001_add_channels_support` | channels table + channel_id on episodes |
| `002_add_v2_pipeline_columns` | v2 status enums, pipeline_version, review_status, youtube fields |
| `003_create_prompt_versions` | prompt_versions table |
| `004_create_review_tables` | review_tasks + review_decisions tables |
| `005_create_media_assets` | media_assets table |
| `006_create_publish_jobs` | publish_jobs table |

Pattern: `Migration` ABC in `btcedu/migrations/__init__.py`. `MIGRATIONS` list. Each has `version`, `description`, `up()`, `is_applied()`, `mark_applied()`. Check-before-act idempotency.

## Core Module Functions

Each v2 stage follows the same pattern: `generate_X(session, episode_id, settings, force=False)` → returns a result dataclass.

| Module | Main Function | Result Type |
|--------|--------------|-------------|
| `detector.py` | `detect_episodes()`, `download_episode()`, `backfill_episodes()` | `DetectResult` |
| `transcriber.py` | `transcribe_episode()`, `chunk_episode()` | str/int |
| `corrector.py` | `correct_transcript()` | `CorrectionResult(change_count, cost_usd, ...)` |
| `translator.py` | `translate_transcript()` | `TranslationResult(output_char_count, cost_usd, skipped, ...)` |
| `adapter.py` | `adapt_script()` | `AdaptationResult(adaptation_count, tier1_count, tier2_count, cost_usd, skipped, ...)` |
| `chapterizer.py` | `chapterize_script()` | `ChapterizationResult(chapter_count, cost_usd, ...)` |
| `image_generator.py` | `generate_images()` | `ImageGenResult(images: [ImageEntry], total_cost_usd, ...)` |
| `tts.py` | `generate_tts()` | `TTSResult(audio_entries: [AudioEntry], total_cost_usd, ...)` |
| `renderer.py` | `render_video()` | `RenderResult(video_path, total_duration_s, ...)` |
| `publisher.py` | `publish_video()` | `PublishResult(youtube_video_id, youtube_url, cost_usd, ...)` |
| `reviewer.py` | `create_review_task()`, `approve_review()`, `reject_review()`, `request_changes()`, `has_approved_review()`, `has_pending_review()` | `ReviewTask` / bool |
| `generator.py` | `generate_content()`, `refine_content()` (v1 only) | `GenerationResult` |

## Services

| Service | Key Classes/Functions | Notes |
|---------|----------------------|-------|
| `claude_service.py` | `call_claude(prompt, system, settings, ...)` → `ClaudeResponse(text, input_tokens, output_tokens, cost_usd)` | Uses anthropic SDK; `compute_prompt_hash()`, `calculate_cost()` |
| `elevenlabs_service.py` | `ElevenLabsService(api_key, voice_id, settings)` → `.synthesize(TTSRequest)` → `TTSResponse` | Protocol: `TTSService`. Raw HTTP. Chunking for long text. |
| `image_gen_service.py` | `DallE3ImageService(api_key)` → `.generate(ImageGenRequest)` → `ImageGenResponse` | Protocol: `ImageGenService`. Uses openai SDK. |
| `youtube_service.py` | `YouTubeDataAPIService(creds_path)` → `.upload(YouTubeUploadRequest)` → `YouTubeUploadResponse` | Protocol: `YouTubeService`. `DryRunYouTubeService` for testing. `authenticate(client_secrets, creds_path)`, `check_token_status(creds_path)` → `{valid, expired, expiry, can_refresh, error}` |
| `feed_service.py` | `fetch_feed(url)`, `parse_feed(content, source_type)`, `fetch_channel_videos_ytdlp(channel_id)` | Returns `list[EpisodeInfo]` |
| `download_service.py` | `download_audio(url, output_dir, format)` | Uses yt-dlp |
| `transcription_service.py` | `transcribe_audio(audio_path, settings)` | Uses OpenAI Whisper API; auto-chunks large files |

## API Endpoints (web/api.py, prefix: /api)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/health` | Health check |
| GET | `/debug/db-schema` | DB schema info |
| GET | `/episodes` | List episodes |
| GET | `/episodes/<id>` | Episode detail |
| POST | `/detect` | Trigger detection |
| POST | `/episodes/<id>/download` | Download audio |
| POST | `/episodes/<id>/transcribe` | Transcribe |
| POST | `/episodes/<id>/chunk` | Chunk transcript |
| POST | `/episodes/<id>/generate` | Generate content |
| POST | `/episodes/<id>/run` | Run full pipeline |
| POST | `/episodes/<id>/refine` | Refine content |
| POST | `/episodes/<id>/retry` | Retry failed |
| POST | `/episodes/<id>/publish` | Publish to YouTube |
| GET | `/episodes/<id>/publish-status` | Publish job status |
| GET | `/episodes/<id>/action-log` | Pipeline run history |
| GET | `/episodes/<id>/files/<type>` | Serve episode files |
| GET | `/cost` | Cost summary |
| GET | `/whats-new` | Recent activity |
| GET | `/reviews` | List reviews |
| GET | `/reviews/count` | Pending review count |
| GET | `/reviews/<id>` | Review detail |
| POST | `/reviews/<id>/approve` | Approve review |
| POST | `/reviews/<id>/reject` | Reject review |
| POST | `/reviews/<id>/request-changes` | Request changes |
| POST | `/batch/start` | Start batch processing |
| GET | `/batch/<id>` | Batch job status |
| POST | `/batch/<id>/stop` | Stop batch |
| GET | `/batch/active` | Active batches |
| GET | `/jobs/<id>` | Job status polling |

## Code Patterns

### Stage Implementation Pattern
Each v2 stage follows this structure (reference: `image_generator.py`, `tts.py`, `publisher.py`):
1. **Service layer** (`services/`) — external API wrapper, Protocol for future providers
2. **Core module** (`core/`) — orchestration: `generate_X(session, episode_id, settings, force)`
3. **Idempotency** — content hash (SHA-256) + provenance file + `.stale` markers
4. **Partial recovery** — skip unchanged chapters on re-run
5. **Cost guard** — cumulative episode cost vs `max_episode_cost_usd`
6. **Dry-run** — placeholders instead of API calls (`settings.dry_run`)
7. **PipelineRun** record — stage, status, cost, timestamps
8. **ContentArtifact** record — artifact_type, model, prompt_hash
9. **MediaAsset** record — for media outputs (images, audio, video)
10. **Downstream invalidation** — `.stale` marker files

### Pipeline Integration
- `_run_stage()` in `pipeline.py`: lazy import of stage function → returns `StageResult(stage, status, duration_seconds, detail, error)`
- Review gates: `has_approved_review()` / `has_pending_review()` → create `ReviewTask` if needed; return `"review_pending"` status to pause
- `run_pending()` / `run_latest()`: filter by actionable statuses, skip pending reviews
- Cost extraction: `run_episode_pipeline()` parses cost from `StageResult.detail` string (splits on `$`)
- `PipelineReport(episode_id, title, stages: [StageResult], total_cost_usd, success, error)`

### DB Session
- `btcedu/db.py`: `Base` (DeclarativeBase), `get_engine()`, `get_session_factory()`, `init_db()`
- FTS5 virtual table `chunks_fts` created in `init_db()` / `_init_fts()`

### Prompt Templates
6 templates in `btcedu/prompts/templates/`: `correct_transcript.md`, `translate.md`, `adapt.md`, `chapterize.md`, `imagegen.md`, `system.md`
Format: YAML frontmatter (name, version, model, temperature, max_tokens) + Jinja2 body.
`PromptRegistry` loads templates, computes SHA-256 content hash, registers `PromptVersion` in DB.

## Tests (629 passing, pytest)

### Fixtures (conftest.py)
- `db_engine` — in-memory SQLite + FTS5
- `db_session` — scoped session from db_engine
- `chunked_episode` — Episode at CHUNKED status with chunks + FTS
- `SAMPLE_TRANSCRIPT` — from `tests/fixtures/sample_transcript_de.txt`

### Key test patterns
- Mock all external APIs (no real API calls)
- **MediaAsset tests**: must create `prompt_versions` table in `MediaBase.metadata` AND call `MediaBase.metadata.create_all(engine)` separately (different Base!)
- **pydub + Python 3.13**: `audioop` removed; tests mock via `patch.dict(sys.modules, {"pydub": mock_pydub})`
- **Publisher tests**: mock `YouTubeService` Protocol + `ReviewTask` + `PublishJob` records

## Dependencies (pyproject.toml)

**Required**: click, anthropic, openai, sqlalchemy, pydantic, pydantic-settings, python-dotenv, pyyaml, feedparser, yt-dlp, pydub, requests
**Optional (web)**: flask, gunicorn
**Optional (dev)**: pytest, ruff, flask
**Optional (youtube)**: google-api-python-client, google-auth-httplib2, google-auth-oauthlib (`pip install -e ".[youtube]"`)

**Ruff config**: target py312, line-length 100, select E/W/F/I/UP, ignore UP042

## Key Design Decisions

1. **Raw HTTP for external APIs** — ElevenLabs, image gen use `requests` directly (no SDKs)
2. **Cascade invalidation** — upstream changes write `.stale` markers; images + TTS are parallel (don't invalidate each other)
3. **Review gates block pipeline** — pipeline pauses, creates `ReviewTask`, resumes on approval
4. **Prompt versioning** — file-based templates, SHA-256 hashing, PromptRegistry tracks versions
5. **v1/v2 coexistence** — `pipeline_version` field; v1 code untouched
6. **YouTube OAuth** — interactive `youtube-auth` CLI; credentials stored at `youtube_credentials_path`
7. **Safety checks before publish** — approval gate + artifact integrity + metadata completeness + cost sanity
8. **Auto-approve for minor corrections** — corrections with <5 punctuation-only changes are auto-approved (MASTERPLAN §9.4)
9. **Review history file** — all review decisions appended to `data/outputs/{ep_id}/review/review_history.json` for file-level audit trail
10. **Corrector cascade invalidation** — re-correction marks `transcript.tr.txt.stale` to trigger re-translation

## Known Gotchas

- **MediaAsset uses own Base**: `Base = declarative_base()` in `media_asset.py`, NOT `btcedu.db.Base`. Tests must create both metadata sets separately.
- **pydub + Python 3.13**: `audioop` module removed. Tests mock via `sys.modules`. Production may need `pyaudioop`.
- **Chapter schema**: `Chapter.visual` is singular (`Visual`), not a list. `Narration` has `.text`, `.word_count`, `.estimated_duration_seconds`.
- **Lazy imports**: Stage functions lazy-imported inside `_run_stage()` to avoid circular imports.
- **Cost extraction**: `run_episode_pipeline()` parses cost from `StageResult.detail` string (splits on `$`).
- **YouTube deps are optional**: Install via `pip install -e ".[youtube]"`. `run.sh` auto-installs if `data/client_secret.json` exists.
- **check_token_status return**: returns `{valid, expired, expiry, can_refresh, error}` — no `exists` key.
- **Datetime helper**: all models use `_utcnow() → datetime.now(UTC)` for defaults.

## Reference Documents

For detailed specifications beyond this summary:
- `MASTERPLAN.md` — Full architecture, schemas, phase roadmap, prompt strategy
- `docs/sprints/` — Per-sprint plan/validation/implementation docs
- `docs/sprints/outputs/` — Sprint implementation and validation outputs (all 11 sprints)
