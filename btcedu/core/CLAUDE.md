# btcedu/core/ — Pipeline Stage Modules

Each v2 stage module follows the same pattern:

## Stage Implementation Pattern

1. **Function signature**: `generate_X(session, episode_id, settings, force=False)` -> result dataclass
2. **Guards**: check `episode.pipeline_version == 2`, check episode status, check `force`
3. **Idempotency**: SHA-256 content hash + provenance JSON file + `.stale` marker check
4. **Partial recovery**: skip unchanged chapters on re-run
5. **Cost guard**: cumulative episode cost vs `settings.max_episode_cost_usd`
6. **Dry-run**: `settings.dry_run` -> placeholders instead of API calls
7. **PipelineRun record**: stage, status, cost, timestamps (created at start, updated at end)
8. **ContentArtifact record**: artifact_type, model, prompt_hash
9. **MediaAsset record**: for media outputs (images, audio, video)
10. **Downstream invalidation**: write `.stale` files for dependent stages
11. **Error handling**: set `episode.error_message` on failure, clear it on success

## Key Modules

- `pipeline.py` — orchestration: `_V2_STAGES`, `_run_stage()` (lazy imports), `run_episode_pipeline()`, `run_pending()`, `run_latest()`
- `reviewer.py` — review CRUD: `create_review_task()`, `approve_review()`, `reject_review()`, `has_approved_review()`, `has_pending_review()`, `approve_stage_for_artifacts()`
- `qa_reviewer.py` — transcript/translation QA plus `adjudicate_quality_gate()` and `adjudicate_transcript_qa_gate()` (independent-model gate decisions, written to `gate_adjudication.json`)
- `translation_qa.py` — deterministic checks + LLM cascade, GREEN/RED gate, bounded targeted retries
- `narration_lock.py` — approved-narration invariant: `normalize_narration_text()`, `compose_chapter_narration()`, `check_narration_lock()`, plus deterministic repair of minor drift/truncation
- `final_review.py` — deterministic weather video checks at `review_gate_3` (asset exists, 1920x1080, segment resolution, blank/freeze/stale frames, narration coverage). Fail-closed: a crash blocks the gate.
- `retention.py` — `prune_expired_episodes()` (files + DB rows), called from `detector.py`. Honors `episode_retention_days` / profile `ingest.retention_days`.
- `regression_runner.py` — `run_recent_episode_regression()`: replays recent episodes stage-by-stage against a cloned DB and copied outputs, never touching production data (`btcedu regression-run`)
- `weather/` — deterministic Tagesschau weather subsystem: `detector.py` (is this a weather chapter?), `extractor.py` (claims from approved narration), `scene_planner.py`, `renderer.py` (HTML/SVG → headless Chromium → PNG/MP4), `validator.py` (claims must be narration-backed), `models.py`, `lexicon.py`, `dates.py` (absolute Turkish date labels), `cities.py` (map projection), `templates/`, `assets/`
- `stock_images.py` (60KB) — Pexels stock search, intent extraction, ranking, candidate finalization
- `renderer.py` — ffmpeg: per-chapter segments -> concat -> draft.mp4, intro/topic-intro cards, ticker
- `frame_editor.py` — Gemini 2.0 Flash frame editing for tagesschau episodes (translates German text overlays to Turkish)

## Common Tasks

- **Adding a new stage**: follow the pattern in `tts.py` (cleanest example), add to `_V2_STAGES` in pipeline.py
- **Fixing a stage**: always clear `episode.error_message = None` on success path
- **Pipeline debugging**: check `PipelineRun` records, `episode.error_message`, `episode.retry_count`
- **Cost extraction**: `run_episode_pipeline()` parses cost from `StageResult.detail` (splits on `$`)
- **Failure notifications**: a failed stage calls `services/notify_service.notify_stage_failure()`; notification errors are swallowed and must never affect the run

<!--
Documentation sync
Baseline: 1d7291b
Synced through: HEAD
Date: 2026-08-04
-->
