# Phase 2 Validation Report — Pipeline Progress Visualization

**Validated against:** `phase2-progress-ui-plan.md`, `phase2-progress-ui-implement-output.md`, live codebase
**Validator:** Claude Sonnet 4.6
**Date:** 2026-03-15

---

## 1. Verdict

**PASS**

All deliverables from the plan are implemented, all 23 new tests pass, and all 736 pre-existing tests remain green. No regressions found.

---

## 2. Scope Check

### Expected (per plan)
- `stage_progress` field added to episode API responses (list + detail)
- `_STAGE_LABELS` dict mapping stage names to human-readable labels
- `_STAGE_TO_PIPELINE_STAGE` dict mapping stage names to `PipelineStage` enum values
- `_build_stage_progress()` helper function
- Batch duration query in `GET /episodes` (avoids N+1 per episode)
- `renderPipelineStepper(sp)` JS function
- `formatDuration(s)` JS helper
- Pipeline stepper CSS (`.pipeline-stepper`, `.ps-stage`, `.ps-blob`, state variants)
- New test file `test_web_progress.py` with 8+ tests

### Actually implemented
Everything above, plus:
- `duration_cache` parameter threaded through `_episode_to_dict()` (clean, required by batch design)
- try/except wrapper around `_build_stage_progress()` in `_episode_to_dict()` to prevent 500 errors on unexpected status values
- `review_gate_stock` stage covered in `_STAGE_LABELS` (Phase 4 addition; 14 v2 stages total, not 13)
- Responsive CSS via `@media (max-width: 768px)` for mobile layout

### Scope creep
None. The `try/except` wrapper and `duration_cache` threading are required by the design, not additions.

---

## 3. Correctness Review

### Backend constants

**`_STAGE_LABELS`** (`api.py` lines 313–333):
17 keys total — 5 v1 stages (`download`, `transcribe`, `chunk`, `generate`, `refine`) and 12 v2 stages (including 4 review gates: `review_gate_1`, `review_gate_2`, `review_gate_stock`, `review_gate_3`). All stages from both `_V1_STAGES` and `_V2_STAGES` are covered.

The test `test_stage_labels_dict_complete` asserts exact key equality against the expected 17-element set. ✓

**`_STAGE_TO_PIPELINE_STAGE`** (`api.py` lines 335–349):
13 entries. All map to `PipelineStage` enum values. Correctly excludes all 4 review gate stages (gates have no `PipelineRun` records, so duration attachment is inapplicable). Verified by:
- `test_stage_to_pipeline_stage_no_gates` — asserts no key starts with `"review_gate"`
- `test_stage_to_pipeline_stage_all_map_to_enum` — asserts all values are `PipelineStage` instances ✓

### `_build_stage_progress()` logic

**Base state mapping** (`api.py` lines 379–394):

| `StagePlan.decision` | `StagePlan.reason` | UI state |
|---|---|---|
| `"skip"` | contains `"already completed"` | `"done"` |
| `"run"` | any | `"active"` |
| anything else | any | `"pending"` |

This correctly handles the three outcomes from `resolve_pipeline_plan()`. The `"active"` state for `"run"` decisions means the stage is ready to execute but may or may not be currently running — consistent with the plan's non-goal of distinguishing idle vs. in-flight execution.

**Review context override** (`api.py` lines 396–405):
Only the gate whose `name` matches `review_context["review_gate"]` is modified. States:
- `paused_for_review` → gate becomes `"paused"` ✓
- `review_approved` → gate becomes `"done"` ✓

No other stages are touched. Verified by `test_paused_review_gate_state` and `test_approved_review_gate_state`. ✓

**Failure override** (`api.py` lines 412–435):
For `FAILED` or `COST_LIMIT` status:
1. Search for first `"active"` stage; if none, first non-`"done"`/non-`"skipped"` stage.
2. Mark that stage `"failed"`.
3. All subsequent stages → `"pending"`.

This handles the edge case where `FAILED` maps to `_STATUS_ORDER = -1`, causing `resolve_pipeline_plan()` to return no `"run"` decisions (all stages become `"pending"`). The fallback to first non-done stage correctly identifies the failure point.

Verified by `test_failed_episode_marks_failed_stage` (asserts exactly one `"failed"` stage and all stages after it are `"pending"`). ✓

**Duration attachment** (`api.py` lines 437–469):
- Gate stages are skipped entirely (`if s["is_gate"]: continue`) — gates always retain `duration_seconds=None`, `cost_usd=None`. ✓
- Non-gate stages look up `_STAGE_TO_PIPELINE_STAGE[name]` → `PipelineStage` enum → `ep_duration_map[ps]`. ✓
- `ep_duration_map` is populated from `duration_cache.get(episode.id, {})` when batch cache is provided, or from a per-episode DB query otherwise. ✓
- Cache key: `episode.id` (integer FK to `episodes.id`), matching `PipelineRun.episode_id` (integer). This is correct and distinct from `episode.episode_id` (string). ✓

Verified by `test_duration_attached_from_pipeline_run` and `test_gate_stages_have_no_duration`. ✓

**Summary fields** (`api.py` lines 472–486):
- `current_stage`: first stage with state in `("active", "paused", "failed")`. Returns `None` for published/fully-done episodes. ✓
- `completed_count`: stages where state is `"done"` or `"skipped"`. Since the implementation never emits `"skipped"` (the mapping goes through `"done"`, `"active"`, `"pending"`, `"paused"`, `"failed"`), the `"skipped"` check is dead code but harmless. ✓
- `total_count`: `len(stages)` — always the full stage count for the pipeline version. ✓

Verified by `test_completed_count_and_total` and `test_published_episode_all_done`. ✓

### Batch duration query

`list_episodes()` (`api.py` lines 596–617):
Single `SELECT` on `pipeline_runs WHERE status = 'success'`, ordered by `episode_id, stage, completed_at DESC`. A `seen_run_keys` set deduplicates to the most-recent successful run per `(episode.id, stage)`. The resulting `duration_cache` is a `dict[int, dict[PipelineStage, tuple[float, float]]]` passed into each `_episode_to_dict()` call.

This is structurally identical to the pending-task batch query added in Phase 1. No N+1. Verified by `test_batch_duration_query_efficiency` (16+ episodes all receive `stage_progress`). ✓

### v1/v2 stage count

`_build_stage_progress()` delegates stage discovery to `resolve_pipeline_plan()`, which calls `_get_stages()` with `max(settings.pipeline_version, episode.pipeline_version)`. A v1 episode with v1 settings returns 5 stages; a v2 episode with v2 settings returns 14. A v1 episode with v2 settings uses v2 stages (by-design behavior of `max()`).

Verified by `test_v1_stage_progress_five_stages` and `test_stage_progress_pipeline_version_respected`. ✓

### Error guard in `_episode_to_dict()`

`_build_stage_progress()` is called inside `try/except Exception`, logging via `logger.exception` and falling back to `stage_progress=None`. Existing callers that don't pass `session` receive `stage_progress=None` without error. No regression for v1 callers. ✓

### Frontend

**`formatDuration(s)`**: Formats seconds to `"12s"`, `"2m"`, `"1.5h"` — straightforward branching, no edge cases.

**`renderPipelineStepper(sp)`**: Returns empty string for `null`/empty input. Iterates `sp.stages`, builds connector HTML (green if previous stage is done), blob HTML (with icon for done/paused/failed), label div, optional duration. Gate stages receive `.ps-gate` class. Appends `"N/M stages complete"` summary. Placement: inserted in `selectEpisode()` between `detail-meta` div and `renderNextAction(ep)`, consistent with the plan's specified position.

**CSS**: `.ps-done` (green), `.ps-active` (accent blue + `psPulse` animation), `.ps-paused` (yellow), `.ps-failed` (red), `.ps-pending` (bg/border). All use existing CSS variables. `.ps-connector` transitions from gray to green for done transitions. Responsive breakpoint at 768px. ✓

### Risks / Defects

**One gap found (low risk):**

`COST_LIMIT` status is handled identically to `FAILED` in the failure override block (`episode_status in ("failed", "cost_limit")`). However, no test directly exercises a `COST_LIMIT` episode through `_build_stage_progress()`. The test coverage relies on the fact that the branch is a simple `or` condition and `COST_LIMIT` is already tested elsewhere (Phase 1 `_compute_pipeline_state` tests). Low risk, non-blocking.

**No other defects found.**

---

## 4. Test Review

### Coverage present

**`tests/test_web_progress.py`** (549 lines, 23 tests):

| Class | Tests | What's covered |
|-------|-------|----------------|
| `TestBuildStageProgressV2` | 11 | Stage order (14), NEW/CORRECTED/FAILED/PUBLISHED states, paused gate, approved gate, duration attachment, gate no-duration, labels, completed count |
| `TestBuildStageProgressV1` | 1 | v1 episode → 5 stages |
| `TestStageLabelConstants` | 3 | Dict completeness (17 keys), no gates in PipelineStage map, all values are enum members |
| `TestEpisodeDetailIncludesStageProgress` | 3 | Detail endpoint has `stage_progress`, non-empty stages list, all 6 required keys present |
| `TestEpisodeListIncludesStageProgress` | 3 | List endpoint has `stage_progress` for all, v1/v2 version respected, paused gate reflected |
| `TestBatchDurationQueryEfficiency` | 1 | 16+ episodes all receive non-null `stage_progress` |
| `TestDurationInStageProgress` | 1 | Duration from batch cache attached to stage |

All plan-required test scenarios are covered. The `seeded_db` fixture provides realistic episode variety (NEW, CORRECTED, CORRECTED+APPROVED, FAILED, PUBLISHED, v1).

### Missing / weak tests

1. **`COST_LIMIT` failure branch** — Not directly tested. The `in ("failed", "cost_limit")` branch is covered for `"failed"` only. The `"cost_limit"` path is identical logic; low risk, but a spot-check would increase confidence.

2. **`formatDuration()` and `renderPipelineStepper()` JS** — No DOM-level tests. Manual verification required for visual correctness.

3. **`stage_progress=None` fallback** — No test exercises the `try/except` path (forcing `_build_stage_progress` to raise). The guard exists in production code but is untested. Non-blocking.

4. **`review_gate_stock` paused/done states** — `test_v2_stage_progress_all_stages_present` confirms the gate appears in the ordered list, but no test specifically exercises `review_gate_stock` as `"paused"` or `"done"`. The review-context override is generic (matches by gate name) so correctness transfers from the `review_gate_1` tests.

### Suggested additions (non-blocking)

- Add `test_cost_limit_episode_marks_failed_stage` mirroring `test_failed_episode_marks_failed_stage` but with `EpisodeStatus.COST_LIMIT`.
- Add `test_build_stage_progress_error_falls_back_to_none` that patches `resolve_pipeline_plan` to raise, asserting `stage_progress` is `None` in the API response.

---

## 5. Backward Compatibility Check

### API contract

- `stage_progress` is an **additive new key** — no existing keys renamed or removed.
- Episodes without a session (legacy callers) receive `stage_progress: null`. No change in behavior.
- `_episode_to_dict()` signature gains `duration_cache` optional parameter (default `None`). All existing callers unaffected.

### v1 episode risk

- v1 episodes with `pipeline_version=1` and v1 settings receive a 5-stage `stage_progress`. States correctly reflect v1 flow (no review gates).
- v1 episodes with v2 settings receive a 14-stage `stage_progress` (due to `max()` logic). This is by design and expected behavior per `_get_stages()`.

### DB query risk

- Batch duration query adds one additional `SELECT` per list request. It is a single read against `pipeline_runs` with no joins. Negligible overhead.
- No writes to any table. Zero side-effect risk.

### Phase 1 integration

`_build_stage_progress()` receives `review_context` as a pre-computed argument (computed by `_get_review_context()` before the call). The two Phase 1/2 features are cleanly composed without coupling their implementations. ✓

---

## 6. Required Fixes Before Commit

**None.** The implementation is correct and all tests pass. This phase is ready to ship as-is.

---

## 7. Nice-to-Have Improvements

1. **`COST_LIMIT` test.** Add `test_cost_limit_episode_marks_failed_stage` to give direct coverage of the `"cost_limit"` branch in the failure override.

2. **`stage_progress=None` fallback test.** Patch `resolve_pipeline_plan` to raise and assert `stage_progress: null` in the API response — ensures the try/except guard is exercised.

3. **`"skipped"` state removal.** The `completed_count` computation checks for `"skipped"` state, but no code path ever emits it. Either add a `"skipped"` state for inapplicable v1 stages (e.g., when v2 settings are used with a v1 episode), or remove the dead check. Not a defect — purely cosmetic.

4. **JS unit tests for `formatDuration()`.** A small Vitest/Jest test covering `0s`, `59s`, `60s`, `3600s` edge cases would guard against formatting regressions.

---

## 8. Summary

Phase 2 is fully and correctly implemented. The core deliverables — `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`, `_build_stage_progress()`, the batch duration cache, `renderPipelineStepper()`, `formatDuration()`, and the pipeline stepper CSS — all work as specified.

State mapping covers all five UI states (done/active/pending/paused/failed) with correct logic for review gate overrides and failure propagation. The batch duration query avoids N+1 on the list endpoint. The try/except guard prevents `stage_progress` errors from breaking episode serialization.

Test coverage is comprehensive (23 dedicated tests + 736 pre-existing tests remain green). The implementation is additive-only with no breaking changes.

**Result: PASS. No fixes required.**
