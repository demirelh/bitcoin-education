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
- `reviewer.py` — review CRUD: `create_review_task()`, `approve_review()`, `reject_review()`, `has_approved_review()`, `has_pending_review()`
- `stock_images.py` (60KB) — Pexels stock search, intent extraction, ranking, candidate finalization
- `renderer.py` — ffmpeg: per-chapter segments -> concat -> draft.mp4

## Common Tasks

- **Adding a new stage**: follow the pattern in `tts.py` (cleanest example), add to `_V2_STAGES` in pipeline.py
- **Fixing a stage**: always clear `episode.error_message = None` on success path
- **Pipeline debugging**: check `PipelineRun` records, `episode.error_message`, `episode.retry_count`
- **Cost extraction**: `run_episode_pipeline()` parses cost from `StageResult.detail` (splits on `$`)
