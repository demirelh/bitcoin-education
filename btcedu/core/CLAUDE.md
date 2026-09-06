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

- `pipeline.py` — orchestration: `_V2_STAGES`, `_run_stage()` (lazy imports),
  `run_episode_pipeline()`, lease-aware `run_episode_pipeline_coordinated()`,
  `run_pending()`, `run_latest()`. Scheduled entry points close orphaned
  `RUNNING` rows only after taking `pipeline.lock`, then resume from the
  episode's durable status.
- `reviewer.py` — review CRUD: `create_review_task()`, `approve_review()`, `reject_review()`, `has_approved_review()`, `has_pending_review()`, `approve_stage_for_artifacts()`
- `qa_reviewer.py` — transcript/translation QA plus `adjudicate_quality_gate()` and `adjudicate_transcript_qa_gate()` (independent-model gate decisions, written to `gate_adjudication.json`)
- `translation_qa.py` — deterministic checks + LLM cascade, GREEN/RED gate, bounded targeted retries
- `adapter.py` — conditional per-story adaptation. Keep its returned-operation allowlist strict;
  `anchor_unify` selection checks Turkish markers and German first-person source markers because
  Turkish may encode first person only in verb or possessive suffixes.
- `narration_lock.py` — approved-narration invariant: `normalize_narration_text()`, `compose_chapter_narration()`, `check_narration_lock()`, plus deterministic repair of minor drift/truncation
- `final_review.py` — deterministic weather video checks at `review_gate_3` (asset exists, 1920x1080, segment resolution, blank/freeze/stale frames, narration coverage). Fail-closed: a crash blocks the gate.
- `retention.py` — `prune_expired_episodes()` (files + DB rows), called from `detector.py`. Honors `episode_retention_days` / profile `ingest.retention_days`.
- `regression_runner.py` — `run_recent_episode_regression()`: replays recent
  episodes against a cloned DB and copied outputs, never touching production
  data; `btcedu regression-run --only-stage` can isolate any stage in the
  profile pipeline, while multi-stage runs stop after chapterize. Its temporary
  workspace lives under `outputs_dir/.regression-workspaces`, not the small
  system `/tmp` tmpfs used on the Raspberry Pi.
- `copilot_fix.py` — `start_copilot_fix()`: one-shot autonomous repair of a failed stage in a detached tmux session (`copilotfix`). Called from the failure branch of `run_episode_pipeline()`, at most once per distinct error (marker `provenance/copilot_fixes.json`), never in dry-run and disabled via `copilot_auto_fix_enabled`
- `weather/` — deterministic Tagesschau weather subsystem: `detector.py` (is this a weather chapter?), `extractor.py` (claims from approved narration), `scene_planner.py`, `renderer.py` (HTML/SVG → headless Chromium → PNG/MP4), `validator.py` (claims must be narration-backed), `models.py`, `lexicon.py`, `dates.py` (absolute Turkish date labels), `cities.py` (map projection), `templates/`, `assets/`
- `image_generator.py` — per-chapter pictures. On a provider error the chapter is retried with the profile's `fallback_provider`; if it still has no file the stage **fails** instead of writing a `*_failed.png` entry. Manifest and provenance are written first, so a rerun regenerates only the missing chapters (`_chapters_needing_regen`).
- `stock_images.py` (60KB) — Pexels stock search, intent extraction, ranking, candidate finalization
- `renderer.py` — ffmpeg: per-chapter segments -> concat -> draft.mp4, intro/topic-intro
  cards, ticker. A studio scene plan is ignored while `anchor_enabled=false`,
  preserving the chapter renderer without requiring studio or avatar artifacts;
  reporter-only scene plans remain valid.
- `frame_editor.py` — Gemini 2.0 Flash frame editing for tagesschau episodes (translates German text overlays to Turkish)
- `tts.py` — fresh per-part ElevenLabs synthesis (no reusable take cache),
  750-character provider chunks, −15 LUFS normalization before quality checks,
  adaptive per-voice retry ceilings and a profile-owned hard stage budget
- `publisher.py` — upload safety checks, timeline-based chapters and
  test/production target separation. Successful PublishJobs are idempotent per
  target; test uploads stay private/independent, while production persists the
  YouTube ID before fallible central reconciliation. An `UPLOADING` job without
  an ID is reconciliation-required and blocks automatic retry.
- `../failover/coordination.py` — canonical broadcast IDs, heartbeat checks and
  renewable pipeline/publish `LeaseGuard`s

## Common Tasks

- **Adding a new stage**: follow the pattern in `tts.py` (cleanest example), add to `_V2_STAGES` in pipeline.py
- **Fixing a stage**: always clear `episode.error_message = None` on success path
- **Pipeline debugging**: check `PipelineRun` records, `episode.error_message`, `episode.retry_count`
- **Local transcription**: `faster_whisper` is a supported provider and needs no
  API key. Profiles still route transcription; `tagesschau_tr` uses OpenAI as
  primary and as the independent suspicious-segment verifier.
- **Reboot debugging**: a stage writes and commits its `RUNNING` row before
  work; retry summaries aggregate all attempts instead of showing only the last
- **Failover debugging**: `processing`/`publishing` are intentionally
  fail-closed. Do not delete records or bypass leases; use the authenticated
  `btcedu failover-reconcile` operator command after determining the outcome.
- **Cost extraction**: `run_episode_pipeline()` parses cost from `StageResult.detail` (splits on `$`)
- **Failure notifications**: a failed stage calls `services/notify_service.notify_stage_failure()`; notification errors are swallowed and must never affect the run
- **Structured translation output**: keep strict schema validation after the
  translator's `json_repair` fallback; malformed quoting may be repaired, but
  structurally or factually invalid story translations must still fail.
- **Automatic repair**: the failure branch calls `_trigger_automatic_copilot_fix()`,
  which launches one `gpt-5.6-sol` repair in tmux. The error fingerprint is
  persisted before launch, so the same failure is never retried automatically,
  and a launch error is logged but never replaces the reported stage failure.
  Before any automatic or manually triggered Copilot fix may be committed,
  pushed or resumed, the affected stage must pass
  `btcedu regression-run --only-stage` against the three most recent episodes
  of the same profile in the isolated regression workspace.
- **Copilot model ids expire**: GitHub retires ids without notice and the CLI
  then aborts with `Model "..." from --model flag is not available`, failing the
  stage. The active producer default is `claude-sonnet-5`. Verify any new id in
  `.env`, `config.py` or a profile with
  `copilot --model <id> -p ok` before committing it.

<!--
Documentation sync
Baseline: d1b4676
Synced through: current working tree
Date: 2026-08-22
-->
