# Phase 2: Pipeline Progress Visualization — Implementation Plan

**Date:** 2026-03-15
**Depends on:** Phase 1 (Review UX) — complete
**Goal:** Jenkins-like stage-progress stepper on the episode detail page.

---

## 1. Problem Statement

After Phase 1, users can see *that* an episode is paused for review, but they still cannot see *where in the pipeline* the episode sits. A `corrected` status tells you the last completed stage, but not how many stages came before or remain. There is no visual pipeline map.

The existing `resolve_pipeline_plan()` function in `pipeline.py` already computes exactly this — which stages are done, which would run, which are pending — but it is **never exposed to the API or UI**.

---

## 2. Current State

### What exists

| Asset | Status | Notes |
|-------|--------|-------|
| `_V1_STAGES` / `_V2_STAGES` | In pipeline.py | Authoritative ordered stage lists |
| `_STATUS_ORDER` | In pipeline.py | Maps EpisodeStatus → numeric order |
| `resolve_pipeline_plan()` | In pipeline.py | Returns `list[StagePlan]` (stage, decision, reason) |
| `PipelineRun` model | In episode.py | Has `started_at`, `completed_at`, `stage`, `status`, `estimated_cost_usd` |
| `_get_review_context()` | In api.py (Phase 1) | Returns pending/approved review info for an episode |
| Episode detail UI | In app.js | Shows status badge, Next Action block, tabs |

### What's missing

- No API endpoint or computed field that returns the stage list with per-stage state
- No UI component showing the pipeline as a sequence of stage blobs
- Duration data in `PipelineRun` is not surfaced per-stage to the UI
- Review gates in the stage list have no visual representation

---

## 3. Design Decisions

### 3.1 Stage state model

Each stage in the progress display gets one of these states:

| State | Meaning | Visual |
|-------|---------|--------|
| `done` | Stage completed successfully | Green filled blob |
| `skipped` | Stage was idempotently skipped (already current) | Green filled blob (same as done) |
| `active` | Stage is the next to run (current position) | Blue pulsing blob |
| `paused` | Review gate waiting for human approval | Yellow blob with pause icon |
| `failed` | Stage failed with error | Red blob with X |
| `pending` | Stage not yet reached | Gray empty blob |

**Assumption:** `done` and `skipped` look identical — both mean "this stage's work is complete." The user doesn't need to distinguish between "ran this time" and "ran previously."

### 3.2 Stage sequence source of truth

The stage list comes from `_V1_STAGES` / `_V2_STAGES` in `pipeline.py`. The API will reuse `resolve_pipeline_plan()` for the skip/run/pending base classification, then enrich it with review context and PipelineRun duration data.

**No hardcoded stage lists in JS.** The API returns the full ordered list; the JS renders whatever it receives.

### 3.3 Review gates as visual stages

Review gates (`review_gate_1`, `review_gate_2`, `review_gate_stock`, `review_gate_3`) appear as distinct nodes in the stepper, but are rendered smaller/differently — they are gates, not computation stages. They use the `paused` state when a review is pending, `done` when approved, and `pending` when not yet reached.

### 3.4 Duration data

Duration is computed from `PipelineRun.completed_at - PipelineRun.started_at` for the most recent successful run of each stage. Review gates don't have PipelineRun records, so they show no duration. Stages without a completed run show no duration.

### 3.5 Rendering locations

| Location | What renders | Detail level |
|----------|-------------|-------------|
| Episode detail page | Full horizontal stepper with labels, durations, state colors | Rich |
| Episode list | Not included | Too compact; Phase 1 badges are sufficient |

**Assumption:** The list view already has the `⏸ review` badge from Phase 1. Adding a full stepper to each row would be too noisy. The stepper goes on the detail page only.

---

## 4. Data Contract

### 4.1 New computed field: `stage_progress`

Added to `_episode_to_dict()` when `session` is available. Returned as a new key in both `GET /episodes/<id>` and `GET /episodes`.

```json
{
  "...existing fields...",
  "stage_progress": {
    "pipeline_version": 2,
    "stages": [
      {
        "name": "download",
        "label": "Download",
        "state": "done",
        "is_gate": false,
        "duration_seconds": 12.3,
        "cost_usd": 0.0
      },
      {
        "name": "transcribe",
        "label": "Transcribe",
        "state": "done",
        "is_gate": false,
        "duration_seconds": 45.1,
        "cost_usd": 0.02
      },
      {
        "name": "correct",
        "label": "Correct",
        "state": "done",
        "is_gate": false,
        "duration_seconds": 8.7,
        "cost_usd": 0.05
      },
      {
        "name": "review_gate_1",
        "label": "Review 1",
        "state": "done",
        "is_gate": true,
        "duration_seconds": null,
        "cost_usd": null
      },
      {
        "name": "translate",
        "label": "Translate",
        "state": "active",
        "is_gate": false,
        "duration_seconds": null,
        "cost_usd": null
      },
      {
        "name": "adapt",
        "label": "Adapt",
        "state": "pending",
        "is_gate": false,
        "duration_seconds": null,
        "cost_usd": null
      }
    ],
    "current_stage": "translate",
    "completed_count": 4,
    "total_count": 14
  }
}
```

### 4.2 State derivation logic

New function `_build_stage_progress(session, episode, settings, review_context)`:

1. Call `resolve_pipeline_plan(session, episode, force=False, settings=settings)` to get the base `StagePlan` list.

2. Map each `StagePlan.decision` to a UI state:

   | `StagePlan.decision` | `StagePlan.reason` | UI state |
   |---------------------|-------------------|----------|
   | `"skip"` | `"already completed"` | `"done"` |
   | `"skip"` | `"not ready"` | `"pending"` |
   | `"run"` | any | `"active"` |
   | `"pending"` | any | `"pending"` |

3. Override with review context:
   - If stage is a review gate AND `review_context` exists AND `review_context.state == "paused_for_review"` AND `review_context.review_gate == stage_name` → state = `"paused"`
   - If stage is a review gate AND has an approved ReviewTask → state = `"done"`

4. Override with failure:
   - If `episode.status == FAILED` and `episode.error_message` mentions a stage → that stage = `"failed"`, all subsequent stages = `"pending"`
   - **Simplified approach:** If `episode.status == FAILED`, the first `"active"` stage in the list becomes `"failed"`.

5. Fetch PipelineRun durations:
   - Single batch query: most recent successful PipelineRun per stage for this episode.
   - Map `PipelineStage` enum values to stage names in `_V2_STAGES`.
   - Attach `duration_seconds` and `cost_usd` to matching stages.

6. Compute `current_stage`: the name of the first stage with state `"active"`, `"paused"`, or `"failed"`. If all done → `null`.

### 4.3 Stage name → label mapping

```python
_STAGE_LABELS = {
    # v1
    "download": "Download",
    "transcribe": "Transcribe",
    "chunk": "Chunk",
    "generate": "Generate",
    "refine": "Refine",
    # v2
    "correct": "Correct",
    "review_gate_1": "Review 1",
    "translate": "Translate",
    "adapt": "Adapt",
    "review_gate_2": "Review 2",
    "chapterize": "Chapterize",
    "imagegen": "Images",
    "review_gate_stock": "Review Stock",
    "tts": "TTS",
    "render": "Render",
    "review_gate_3": "Review 3",
    "publish": "Publish",
}
```

### 4.4 Stage name → PipelineStage mapping (for PipelineRun query)

```python
_STAGE_TO_PIPELINE_STAGE = {
    "download": PipelineStage.DOWNLOAD,
    "transcribe": PipelineStage.TRANSCRIBE,
    "chunk": PipelineStage.CHUNK,
    "generate": PipelineStage.GENERATE,
    "refine": PipelineStage.REFINE,
    "correct": PipelineStage.CORRECT,
    "translate": PipelineStage.TRANSLATE,
    "adapt": PipelineStage.ADAPT,
    "chapterize": PipelineStage.CHAPTERIZE,
    "imagegen": PipelineStage.IMAGEGEN,
    "tts": PipelineStage.TTS,
    "render": PipelineStage.RENDER,
    "publish": PipelineStage.PUBLISH,
}
```

Review gates have no PipelineStage mapping — they get `duration_seconds: null`.

### 4.5 Batch optimization for list endpoint

The list endpoint already has a batch query pattern (Phase 1's `pending_cache`). For Phase 2:

- **Episode detail** (`GET /episodes/<id>`): compute `stage_progress` inline — single episode, cost is acceptable.
- **Episode list** (`GET /episodes`): compute `stage_progress` for each episode. The `resolve_pipeline_plan()` call doesn't hit the DB (it only reads `episode.status` + `_STATUS_ORDER`). The PipelineRun duration query is one batch query per request.

**Batch duration query** (added to `list_episodes()`):

```sql
SELECT episode_id, stage, MAX(started_at) AS started_at,
       completed_at, estimated_cost_usd
FROM pipeline_runs
WHERE status = 'success'
GROUP BY episode_id, stage
```

This returns the most recent successful run per (episode, stage). Build a nested dict: `{episode.id: {stage: (duration, cost)}}`.

**Assumption:** This is efficient enough for the expected episode count (<100 total). If performance becomes an issue, the list endpoint can omit `stage_progress` and only include it in the detail endpoint. But computing it now keeps list-view stepper possible later.

---

## 5. UI Component: Pipeline Stepper

### 5.1 HTML structure (rendered by JS)

```html
<div class="pipeline-stepper">
  <div class="ps-stage ps-done" title="Download (12.3s)">
    <div class="ps-blob"></div>
    <div class="ps-label">Download</div>
    <div class="ps-duration">12s</div>
  </div>
  <div class="ps-connector ps-done"></div>
  <div class="ps-stage ps-done" title="Transcribe (45.1s, $0.02)">
    <div class="ps-blob"></div>
    <div class="ps-label">Transcribe</div>
    <div class="ps-duration">45s</div>
  </div>
  <div class="ps-connector ps-done"></div>
  <div class="ps-stage ps-gate ps-paused" title="Review 1: pending">
    <div class="ps-blob">⏸</div>
    <div class="ps-label">Review 1</div>
  </div>
  <div class="ps-connector ps-pending"></div>
  <div class="ps-stage ps-pending">
    <div class="ps-blob"></div>
    <div class="ps-label">Translate</div>
  </div>
  ...
</div>
```

### 5.2 Rendering function

New function `renderPipelineStepper(stageProgress)` in `app.js`:

```javascript
function renderPipelineStepper(sp) {
    if (!sp || !sp.stages || sp.stages.length === 0) return "";
    let html = '<div class="pipeline-stepper">';
    sp.stages.forEach((stage, i) => {
        if (i > 0) {
            // Connector line between stages
            const prevState = sp.stages[i - 1].state;
            const connClass = (prevState === "done" || prevState === "skipped")
                ? "ps-done" : "ps-pending";
            html += `<div class="ps-connector ${connClass}"></div>`;
        }
        const gateClass = stage.is_gate ? " ps-gate" : "";
        const stateClass = `ps-${stage.state}`;
        const icon = stage.state === "paused" ? "⏸"
                   : stage.state === "failed" ? "✗"
                   : stage.state === "done" ? "✓"
                   : "";
        const dur = stage.duration_seconds != null
            ? formatDuration(stage.duration_seconds)
            : "";
        const cost = stage.cost_usd != null && stage.cost_usd > 0
            ? `$${stage.cost_usd.toFixed(3)}`
            : "";
        const tooltip = [stage.label, dur, cost].filter(Boolean).join(" · ");

        html += `
            <div class="ps-stage${gateClass} ${stateClass}" title="${esc(tooltip)}">
                <div class="ps-blob">${icon}</div>
                <div class="ps-label">${esc(stage.label)}</div>
                ${dur ? `<div class="ps-duration">${dur}</div>` : ""}
            </div>`;
    });
    html += '</div>';

    // Progress summary line
    html += `<div class="ps-summary">${sp.completed_count}/${sp.total_count} stages complete</div>`;

    return html;
}

function formatDuration(s) {
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
}
```

### 5.3 Integration with `selectEpisode()`

Insert the stepper between the `detail-meta` div and the Next Action block (or before the action buttons if no Next Action):

```javascript
// In selectEpisode():
det.innerHTML = `
  <div class="detail-header">
    <h2>...</h2>
    <div class="detail-meta">...</div>
    ${renderPipelineStepper(ep.stage_progress)}
    ${renderNextAction(ep)}
    <div class="detail-actions">...</div>
  </div>
  ...
`;
```

This places the stepper visually between the episode title/status line and any action prompts — the natural reading order is: "what is this episode → where is it in the pipeline → what do I need to do."

### 5.4 CSS

```css
/* ── Pipeline Stepper ────────────────────────────────── */
.pipeline-stepper {
    display: flex;
    align-items: flex-start;
    gap: 0;
    margin: 10px 0;
    overflow-x: auto;
    padding: 4px 0 8px;
}

/* Individual stage column */
.ps-stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 48px;
    flex-shrink: 0;
}

/* Gate stages are visually narrower */
.ps-gate {
    min-width: 36px;
}

/* Blob (circle indicator) */
.ps-blob {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 700;
    border: 2px solid var(--border);
    background: var(--bg);
    color: var(--text-dim);
    transition: all 0.2s;
}

/* Gate blobs are smaller */
.ps-gate .ps-blob {
    width: 18px;
    height: 18px;
    font-size: 9px;
    margin-top: 3px;
}

/* State colors */
.ps-done .ps-blob {
    background: var(--green);
    border-color: var(--green);
    color: #fff;
}

.ps-active .ps-blob {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    animation: psPulse 2s ease-in-out infinite;
}

.ps-paused .ps-blob {
    background: var(--yellow);
    border-color: var(--yellow);
    color: #fff;
}

.ps-failed .ps-blob {
    background: var(--red);
    border-color: var(--red);
    color: #fff;
}

.ps-pending .ps-blob {
    background: var(--bg);
    border-color: var(--border);
    color: var(--text-dim);
}

@keyframes psPulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(88, 166, 255, 0.4); }
    50% { box-shadow: 0 0 0 6px rgba(88, 166, 255, 0); }
}

/* Labels */
.ps-label {
    font-size: 9px;
    color: var(--text-dim);
    margin-top: 4px;
    text-align: center;
    white-space: nowrap;
}

.ps-done .ps-label { color: var(--green); }
.ps-active .ps-label { color: var(--accent); font-weight: 600; }
.ps-paused .ps-label { color: var(--yellow); }
.ps-failed .ps-label { color: var(--red); }

/* Duration sub-label */
.ps-duration {
    font-size: 8px;
    color: var(--text-dim);
    opacity: 0.7;
}

/* Connector lines between stages */
.ps-connector {
    flex: 1 0 8px;
    max-width: 24px;
    height: 2px;
    margin-top: 12px;  /* Aligns with blob center */
    background: var(--border);
}

.ps-connector.ps-done {
    background: var(--green);
}

/* Summary line */
.ps-summary {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 2px;
    margin-bottom: 4px;
}

/* Mobile: allow horizontal scroll, reduce label size */
@media (max-width: 768px) {
    .pipeline-stepper {
        padding-bottom: 12px;
    }
    .ps-stage {
        min-width: 40px;
    }
    .ps-gate {
        min-width: 30px;
    }
    .ps-label {
        font-size: 8px;
    }
}
```

---

## 6. Files to Modify

| File | Change Type | Scope |
|------|------------|-------|
| `btcedu/web/api.py` | Backend | Add `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`, `_build_stage_progress()`; call from `_episode_to_dict()`; add batch duration query to `list_episodes()` |
| `btcedu/web/static/app.js` | Frontend | Add `renderPipelineStepper()`, `formatDuration()`; update `selectEpisode()` to include stepper |
| `btcedu/web/static/styles.css` | Frontend | Add `.pipeline-stepper` and related styles (~80 lines) |
| `tests/test_web_review_ux.py` | Tests | Extend with stage_progress tests (or create `tests/test_web_progress.py`) |

---

## 7. Test Plan

### 7.1 Unit tests for `_build_stage_progress()`

| Test | What it verifies |
|------|-----------------|
| `test_v2_stage_progress_all_stages_present` | v2 episode returns all 14 stages in correct order |
| `test_v1_stage_progress_five_stages` | v1 episode returns all 5 stages |
| `test_new_episode_all_pending_except_first` | NEW episode: download=active, rest=pending |
| `test_corrected_episode_marks_done_stages` | CORRECTED: download/transcribe/correct=done, review_gate_1=active or paused, rest=pending |
| `test_paused_review_gate_state` | Episode with pending ReviewTask at review_gate_1: gate shows `"paused"` |
| `test_approved_review_gate_state` | Episode with approved ReviewTask: gate shows `"done"` |
| `test_failed_episode_marks_failed_stage` | FAILED episode: first active stage becomes `"failed"` |
| `test_published_episode_all_done` | PUBLISHED: all stages=done |
| `test_duration_attached_from_pipeline_run` | Stage with PipelineRun gets duration_seconds and cost_usd |
| `test_gate_stages_have_no_duration` | Review gates always have `duration_seconds: null` |
| `test_stage_labels_correct` | Every stage has a non-empty label |
| `test_completed_count_and_total` | `completed_count` and `total_count` match stage states |

### 7.2 Integration tests

| Test | What it verifies |
|------|-----------------|
| `test_episode_detail_includes_stage_progress` | `GET /episodes/<id>` returns `stage_progress` dict |
| `test_episode_list_includes_stage_progress` | `GET /episodes` returns `stage_progress` for each episode |
| `test_stage_progress_pipeline_version_respected` | v1 episode gets 5 stages, v2 gets 14 |
| `test_batch_duration_query_not_n_plus_1` | 10+ episodes still returns quickly; durations present where PipelineRun exists |

### 7.3 Manual smoke test checklist

- [ ] v2 episode at CORRECTED with pending review: stepper shows download/transcribe/correct as green, review_gate_1 as yellow, rest gray
- [ ] v2 episode at RENDERED: all stages up to render green, review_gate_3 next
- [ ] v2 episode at PUBLISHED: all 14 stages green, "14/14 stages complete"
- [ ] v1 episode at GENERATED: download/transcribe/chunk/generate green, refine gray
- [ ] Failed episode: one red blob visible at the failure point
- [ ] Duration labels: completed stages show "12s", "2m", etc.
- [ ] Mobile: stepper scrolls horizontally without breaking layout
- [ ] Stepper appears above Next Action block and below episode title

---

## 8. Definition of Done

1. `GET /episodes` and `GET /episodes/<id>` return `stage_progress` with ordered stages, per-stage state, and durations
2. Episode detail page renders horizontal stepper with color-coded blobs, labels, and connectors
3. Review gates render as distinct smaller nodes with pause icon when pending
4. Durations from PipelineRun appear on completed stages
5. Failed stages show red blob
6. v1 and v2 episodes render their respective stage sets
7. All new tests pass (12+ unit, 4+ integration)
8. Existing tests unbroken (736+ passing)
9. Ruff lint clean
10. Manual smoke test checklist completed

---

## 9. Non-Goals

- **No stepper in the episode list view.** Too compact; the Phase 1 `⏸ review` badge is sufficient for list-level awareness.
- **No ETA or time estimates.** Only actual durations from completed PipelineRun records.
- **No real-time stage-by-stage updates.** The stepper reflects the last-known DB state. User clicks Refresh (or it auto-refreshes after job completion) to see progress.
- **No clickable stages.** Clicking a stepper blob doesn't navigate anywhere or trigger actions. That's future work.
- **No pipeline architecture changes.** `resolve_pipeline_plan()` is reused as-is.
- **No new DB columns or migrations.** All data derived from existing `PipelineRun` + `ReviewTask` tables.
- **No changes to `resolve_pipeline_plan()`.** It is consumed read-only.

---

## 10. Implementation Order

1. **Backend:** Add `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`, `_build_stage_progress()` in `api.py`.
2. **Backend:** Wire into `_episode_to_dict()` — add `stage_progress` field.
3. **Backend:** Add batch duration query to `list_episodes()`.
4. **Tests:** Write unit + integration tests for `_build_stage_progress()`.
5. **CSS:** Add `.pipeline-stepper` styles.
6. **JS:** Add `renderPipelineStepper()`, `formatDuration()`.
7. **JS:** Update `selectEpisode()` to insert stepper.
8. **Smoke test:** Manual verification.

Estimated scope: ~150 lines backend, ~80 lines JS, ~80 lines CSS, ~200 lines tests. Single commit.

---

## 11. Open Questions (resolved with assumptions)

| Question | Assumption | Rationale |
|----------|-----------|-----------|
| Should the stepper go in the list view too? | No, detail only | List view is already dense; Phase 1 badges cover it |
| How to determine the "failed" stage? | First `"active"` stage becomes `"failed"` when `episode.status == FAILED` | The pipeline's `_run_stage` sets error, breaks, and leaves episode at the pre-failure status. The next stage that *would* run is the one that failed. |
| Should `skipped` be a distinct visual state? | No, same as `done` | Users care about "is this stage's output available", not "did it run this invocation" |
| Connector color between done→paused? | Use `ps-done` (green) | The stage *before* the gate completed successfully |
| What about `COST_LIMIT` status? | Treat same as `FAILED` | Both mean the pipeline stopped abnormally |
