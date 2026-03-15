# Phase 2: Pipeline Progress Visualization — Implementation Output

**Date:** 2026-03-15
**Status:** Complete
**Tests:** 759 passing (23 new)

---

## What Was Implemented

### 1. Backend: `btcedu/web/api.py`

**New imports:**
- Added `PipelineStage, RunStatus` to the existing `btcedu.models.episode` import line.

**New constants (after Phase 1 helpers):**
- `_STAGE_LABELS` — maps stage names to human-readable labels for all 17 v1/v2 stages.
- `_STAGE_TO_PIPELINE_STAGE` — maps stage names to `PipelineStage` enum values (13 non-gate stages).

**New function `_build_stage_progress(session, episode, settings, review_context, duration_cache=None)`:**
- Calls `resolve_pipeline_plan()` to get base stage decisions.
- Maps `StagePlan.decision` + reason to UI states: `"done"`, `"active"`, `"pending"`.
- Overrides with review context: gates become `"paused"` or `"done"` based on review state.
- Overrides with failure: for `FAILED`/`COST_LIMIT` status, finds first active or first non-done stage and marks it `"failed"`, sets all subsequent to `"pending"`.
- Attaches duration/cost from `duration_cache` (batch) or direct DB query per episode.
- Returns dict: `{pipeline_version, stages, current_stage, completed_count, total_count}`.

**Updated `_episode_to_dict()`:**
- Added `duration_cache` parameter (passed through from `list_episodes`).
- Calls `_build_stage_progress()` when session is available; wraps in try/except to avoid breaking existing responses on error.
- Adds `"stage_progress"` key to returned dict.

**Updated `list_episodes()`:**
- Batch-queries all successful `PipelineRun` records in one query (ordered by `episode_id`, `stage`, `completed_at DESC`).
- Builds `duration_cache: dict[int, dict[PipelineStage, tuple[float, float]]]` keyed by `episode.id` (integer FK).
- Passes `duration_cache` to each `_episode_to_dict()` call, avoiding N+1 duration queries.

### 2. Frontend: `btcedu/web/static/styles.css`

Appended ~110 lines of CSS at the end of the file:
- `.pipeline-stepper` — flex container with horizontal scroll, `overflow-x: auto`.
- `.ps-stage` — column per stage (min-width 48px).
- `.ps-gate` — narrower gate stages (min-width 36px).
- `.ps-blob` — 24px circle indicator; gate blobs are 18px.
- State color classes: `.ps-done` (green), `.ps-active` (accent blue with `psPulse` animation), `.ps-paused` (yellow), `.ps-failed` (red), `.ps-pending` (bg/border).
- `.ps-label` — 9px label with state-matching colors.
- `.ps-duration` — 8px sub-label for duration.
- `.ps-connector` — horizontal line between stages; green when previous stage is done.
- `.ps-summary` — "N/M stages complete" line.
- `@media (max-width: 768px)` — responsive adjustments.

### 3. Frontend: `btcedu/web/static/app.js`

**New function `formatDuration(s)`:**
- Formats seconds to human-readable: `"12s"`, `"2m"`, `"1.5h"`.

**New function `renderPipelineStepper(sp)`:**
- Returns empty string if `sp` is null or has no stages.
- Iterates stages, renders connector lines between them (green if previous stage done).
- Each stage: outer `div.ps-stage` with state class, inner blob (with icon for done/paused/failed), label div, optional duration div.
- Gate stages get `.ps-gate` class for narrower rendering.
- Appends summary line `"N/M stages complete"`.

**Updated `selectEpisode()`:**
- Inserts `${renderPipelineStepper(ep.stage_progress)}` between the `detail-meta` div and `renderNextAction(ep)`.
- The stepper appears between episode metadata and the Next Action block.

### 4. Tests: `tests/test_web_progress.py` (new file, 23 tests)

**Unit tests (`TestBuildStageProgressV2`):**
- `test_v2_stage_progress_all_stages_present` — 14 stages in correct order
- `test_new_episode_all_pending_except_first` — NEW: download=active, rest=pending
- `test_corrected_episode_marks_done_stages` — CORRECTED: first 3 stages done
- `test_paused_review_gate_state` — pending review → gate shows "paused"
- `test_approved_review_gate_state` — approved review context → gate shows "done"
- `test_failed_episode_marks_failed_stage` — FAILED → one stage is "failed", rest pending
- `test_published_episode_all_done` — PUBLISHED → all done, current_stage=None
- `test_duration_attached_from_pipeline_run` — PipelineRun duration attached to stage
- `test_gate_stages_have_no_duration` — gates always have duration_seconds=None
- `test_stage_labels_correct` — all stages have non-empty labels
- `test_completed_count_and_total` — counts match stage states

**Unit tests (`TestBuildStageProgressV1`):**
- `test_v1_stage_progress_five_stages` — v1 episode with v1 settings → 5 stages

**Unit tests (`TestStageLabelConstants`):**
- `test_stage_labels_dict_complete` — all 17 expected keys present
- `test_stage_to_pipeline_stage_no_gates` — no review gates in mapping
- `test_stage_to_pipeline_stage_all_map_to_enum` — all values are PipelineStage instances

**Integration tests:**
- `test_episode_detail_includes_stage_progress` — GET /episodes/<id> returns stage_progress
- `test_episode_detail_stage_list_not_empty` — stages list is non-empty
- `test_episode_detail_each_stage_has_required_keys` — each stage has all 6 required keys
- `test_episode_list_includes_stage_progress` — GET /episodes returns stage_progress for all
- `test_stage_progress_pipeline_version_respected` — v1 settings+episode → 5 stages; v2 → 14
- `test_paused_review_reflected_in_stage_progress` — paused episode gate shows "paused"
- `test_batch_duration_query_efficiency` — 10+ episodes all get stage_progress
- `test_duration_in_stage_progress_when_pipeline_run_exists` — duration from batch query

---

## Key Design Decisions Made During Implementation

1. **FAILED episode handling**: `resolve_pipeline_plan` returns all stages as "skip/not ready" for `FAILED` status (since `_STATUS_ORDER[FAILED] = -1`). The failure override searches for the first non-done/non-skipped stage and marks it `"failed"`.

2. **v1 pipeline_version behavior**: `_get_stages()` uses `max(settings.pipeline_version, episode.pipeline_version)`. A v1 episode with v2 settings will use v2 stages. Tests account for this by pairing v1 settings with v1 episodes.

3. **Batch duration cache key**: `PipelineRun.episode_id` is an integer FK to `episodes.id`, not the string episode_id. The `duration_cache` is keyed by `episode.id` (int) to match correctly.

4. **Stage progress wrapped in try/except**: If `_build_stage_progress` fails (e.g., import error or unexpected status), the episode dict is still returned with `stage_progress=None` rather than a 500 error.

---

## Test Results

```
759 passed, 33 warnings in 68.64s
```

Previous count: 736 passing. New tests: 23.

---

## Files Changed

| File | Change |
|------|--------|
| `btcedu/web/api.py` | Added `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`, `_build_stage_progress()`; updated `_episode_to_dict()` and `list_episodes()` |
| `btcedu/web/static/styles.css` | Appended pipeline stepper CSS (~110 lines) |
| `btcedu/web/static/app.js` | Added `formatDuration()`, `renderPipelineStepper()`; updated `selectEpisode()` |
| `tests/test_web_progress.py` | New file with 23 tests |
| `docs/sprints/outputs/phase2-progress-ui-implement-output.md` | This file |
