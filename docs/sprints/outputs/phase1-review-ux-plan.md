# Phase 1: Review UX Improvements — Implementation Plan

**Date:** 2026-03-15
**Goal:** Make review pauses obvious and actionable in the existing episode UI.

---

## 1. Problem Statement

The dashboard currently treats the episode list and detail as a status-blind view: a v2 episode at `CORRECTED` status looks identical whether the pipeline is actively running, paused waiting for review gate 1, or ready for the next manual trigger. The user must:

1. Notice the raw status string (e.g. `corrected`) and mentally map it to "this means review_gate_1 is blocking"
2. Navigate to the separate Reviews screen to find the matching review task
3. Click through to take action

There is no visual distinction between "pipeline completed a stage" and "pipeline is paused, waiting for you." The review system exists but is disconnected from the episode view.

---

## 2. Current State Analysis

### What the API exposes today

| Endpoint | Review data | Gap |
|----------|-------------|-----|
| `GET /episodes` | `status` only (raw enum value) | No review state, no pending review info |
| `GET /episodes/<id>` | `status` + `cost` | Same gap — no review context |
| `GET /reviews` | Full list of review tasks | Disconnected from episode view |
| `GET /reviews/<id>` | Full detail + diffs | Only reachable from reviews screen |

### What the UI shows today

| View | Review awareness | Gap |
|------|-----------------|-----|
| Episode list table | Badge shows raw status (`corrected`, `adapted`, etc.) | No "paused for review" indicator |
| Episode detail header | Status badge + error message | No "next action" guidance |
| Episode detail actions | Stage buttons (Download, Transcribe, ...) | No review action button |
| Reviews screen | Separate full-page panel | No way to get there from episode context |

### Status → Review Gate Mapping

| Episode Status | Blocked by | Review stage | Human-readable label |
|---------------|------------|--------------|---------------------|
| `corrected` | `review_gate_1` | `correct` | Transcript Correction Review |
| `adapted` | `review_gate_2` | `adapt` | Adaptation Review |
| `chapterized` | `review_gate_stock` | `stock_images` | Stock Image Review |
| `rendered` | `review_gate_3` | `render` | Video Review |

Note: Not all episodes at these statuses are paused — only those with a PENDING/IN_REVIEW review task. An approved review task at the same status means the pipeline simply hasn't been re-run yet.

---

## 3. Data Contract Changes

### 3.1 New helper: `_get_review_context(session, episode_id)`

**File:** `btcedu/web/api.py` (new function, ~30 lines)

Returns `dict | None`:

```python
{
    "state": "paused_for_review",  # or "review_approved" or None
    "review_task_id": 42,
    "review_stage": "correct",
    "review_stage_label": "Transcript Correction Review",
    "review_status": "pending",
    "review_gate": "review_gate_1",
    "created_at": "2026-03-15T04:32:00",
    "next_action_text": "Pipeline paused — Review Gate 1 requires approval",
    "action_url": "/api/reviews/42"
}
```

Logic:
1. Query `ReviewTask` for this episode where status IN (`pending`, `in_review`), ordered by `created_at DESC`, limit 1.
2. If found → return the context dict with `state: "paused_for_review"`.
3. If not found but episode status is in `{corrected, adapted, chapterized, rendered}` → check for approved review at that stage. If approved → return `state: "review_approved"` (pipeline can resume, just needs `btcedu run`).
4. Otherwise → return `None`.

The stage label mapping is a simple dict:

```python
_REVIEW_GATE_LABELS = {
    "correct": ("review_gate_1", "Transcript Correction Review"),
    "adapt": ("review_gate_2", "Adaptation Review"),
    "stock_images": ("review_gate_stock", "Stock Image Review"),
    "render": ("review_gate_3", "Video Review"),
}
```

### 3.2 Extend `_episode_to_dict()`

**File:** `btcedu/web/api.py`, function `_episode_to_dict` (~line 185)

Add two new fields:

```python
{
    ...,
    "pipeline_version": ep.pipeline_version,
    "review_context": _get_review_context(session, ep.episode_id),  # new
}
```

This requires passing `session` into `_episode_to_dict()`. Update the function signature from `_episode_to_dict(ep, settings)` to `_episode_to_dict(ep, settings, session)` and update all call sites.

### 3.3 Extend `GET /episodes` list endpoint

The list endpoint currently calls `_episode_to_dict()` for each episode. Adding a per-episode review query for the full list could be expensive (N+1). Two options:

**Option A (chosen): Batch query.** Before serialization, run a single query to get all pending review tasks, build a dict keyed by episode_id, and pass it into `_episode_to_dict()`. This is O(1) extra query regardless of episode count.

```sql
SELECT episode_id, id, stage, status, created_at
FROM review_tasks
WHERE status IN ('pending', 'in_review')
ORDER BY created_at DESC
```

Then in Python: `pending_reviews = {rt.episode_id: rt for rt in results}` (first match per episode wins, since ordered DESC).

### 3.4 Computed `pipeline_state` field

Add a computed string field to the episode dict for UI consumption:

```python
"pipeline_state": "paused_for_review" | "failed" | "running" | "completed" | "ready"
```

Derivation rules (in order):
1. `review_context` is not None and `review_context.state == "paused_for_review"` → `"paused_for_review"`
2. `status == "failed"` or `status == "cost_limit"` → `"failed"`
3. `status == "published"` → `"completed"`
4. `status == "approved"` → `"ready"` (ready for publish)
5. Active job exists (optional, skip for now — would require job manager query) → `"running"`
6. Default → `"ready"` (pipeline can be resumed with `btcedu run`)

---

## 4. UI Changes

### 4.1 Episode List — Status Badge Enhancement

**File:** `btcedu/web/static/app.js`, functions `renderTable()` (~line 173) and `renderCards()` (~line 221)

**Current:** Single badge showing raw status text.
```html
<span class="badge badge-corrected">corrected</span>
```

**New:** Two-part badge when paused for review:
```html
<span class="badge badge-corrected">corrected</span>
<span class="badge badge-review-pending" title="Review Gate 1: Transcript Correction Review">⏸ review</span>
```

Implementation:
```javascript
function renderStatusBadges(ep) {
    let html = `<span class="badge badge-${ep.status}">${ep.status}</span>`;
    if (ep.review_context && ep.review_context.state === "paused_for_review") {
        html += ` <span class="badge badge-review-pending" title="${esc(ep.review_context.next_action_text)}">⏸ review</span>`;
    }
    return html;
}
```

Call this in both `renderTable()` and `renderCards()` where the status badge is currently rendered inline.

### 4.2 Episode Detail — "Next Action" Block

**File:** `btcedu/web/static/app.js`, function `selectEpisode()` (~line 282)

Insert a new block between the `.detail-meta` div and the `.detail-actions` div. Only rendered when `review_context` exists.

```html
<div class="next-action next-action-review">
    <div class="next-action-icon">⏸</div>
    <div class="next-action-body">
        <strong>Pipeline paused — Review Gate 1 requires approval</strong>
        <p>Transcript Correction Review is pending since 2h ago.</p>
        <button class="btn btn-primary btn-sm"
                onclick="jumpToReview(42)">
            Review now
        </button>
        <button class="btn btn-sm"
                onclick="actions.run()">
            Resume pipeline
        </button>
    </div>
</div>
```

Rendering logic in `selectEpisode()`:

```javascript
function renderNextAction(ep) {
    const rc = ep.review_context;
    if (!rc) return "";
    if (rc.state === "paused_for_review") {
        return `
        <div class="next-action next-action-review">
            <div class="next-action-icon">⏸</div>
            <div class="next-action-body">
                <strong>${esc(rc.next_action_text)}</strong>
                <p>${esc(rc.review_stage_label)} is ${rc.review_status} since ${timeAgo(rc.created_at)}.</p>
                <button class="btn btn-primary btn-sm" onclick="jumpToReview(${rc.review_task_id})">Review now</button>
                <button class="btn btn-sm" onclick="actions.run()">Resume pipeline</button>
            </div>
        </div>`;
    }
    if (rc.state === "review_approved") {
        return `
        <div class="next-action next-action-approved">
            <div class="next-action-icon">✓</div>
            <div class="next-action-body">
                <strong>${esc(rc.review_stage_label)} approved</strong>
                <p>Run the pipeline to continue to the next stage.</p>
                <button class="btn btn-primary btn-sm" onclick="actions.run()">Continue pipeline</button>
            </div>
        </div>`;
    }
    return "";
}
```

For failed episodes, show a similar block (no review_context needed — use `error_message`):

```javascript
if (ep.status === "failed" || ep.status === "cost_limit") {
    return `
    <div class="next-action next-action-failed">
        <div class="next-action-icon">✗</div>
        <div class="next-action-body">
            <strong>Pipeline failed</strong>
            <p>${esc(trunc(ep.error_message || "Unknown error", 200))}</p>
            <button class="btn btn-sm btn-danger" onclick="actions.retry()">Retry</button>
        </div>
    </div>`;
}
```

### 4.3 `jumpToReview(reviewId)` Navigation Function

**File:** `btcedu/web/static/app.js` (new function, ~15 lines)

```javascript
async function jumpToReview(reviewId) {
    showReviews();               // Switch to review panel
    await loadReviewList();       // Refresh list
    await selectReview(reviewId); // Select and show detail
}
```

This reuses existing `showReviews()`, `loadReviewList()`, and `selectReview()` functions. No new views needed.

### 4.4 CSS Additions

**File:** `btcedu/web/static/styles.css` (append ~40 lines)

```css
/* --- Next Action Block --- */
.next-action {
    display: flex;
    gap: 12px;
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0 12px;
    border-left: 4px solid;
}

.next-action-review {
    background: rgba(255, 193, 7, 0.08);
    border-left-color: var(--yellow, #ffc107);
}

.next-action-approved {
    background: rgba(40, 167, 69, 0.08);
    border-left-color: var(--green, #28a745);
}

.next-action-failed {
    background: rgba(220, 53, 69, 0.08);
    border-left-color: var(--red, #dc3545);
}

.next-action-icon {
    font-size: 1.4em;
    line-height: 1;
    flex-shrink: 0;
    margin-top: 2px;
}

.next-action-body strong {
    display: block;
    margin-bottom: 4px;
}

.next-action-body p {
    margin: 0 0 8px;
    opacity: 0.8;
    font-size: 0.9em;
}

.next-action-body .btn {
    margin-right: 6px;
}
```

### 4.5 Episode List Status Filter Enhancement

**File:** `btcedu/web/static/app.js`, function `applyFilters()` (~line 270)

Add a new filter option to the status dropdown in `index.html`:

**File:** `btcedu/web/templates/index.html` (~line 53, status dropdown)

```html
<option value="review_pending">⏸ Paused for review</option>
```

In `applyFilters()`, add special handling:

```javascript
if (statusFilter === "review_pending") {
    filtered = filtered.filter(ep =>
        ep.review_context && ep.review_context.state === "paused_for_review"
    );
}
```

---

## 5. Files to Modify

| File | Change Type | Scope |
|------|------------|-------|
| `btcedu/web/api.py` | Backend | Add `_get_review_context()`, `_REVIEW_GATE_LABELS`; extend `_episode_to_dict()` signature; add batch review query in `GET /episodes`; add `pipeline_state` field |
| `btcedu/web/static/app.js` | Frontend | Add `renderStatusBadges()`, `renderNextAction()`, `jumpToReview()`; modify `selectEpisode()`, `renderTable()`, `renderCards()`, `applyFilters()` |
| `btcedu/web/static/styles.css` | Frontend | Add `.next-action` block styles (~40 lines) |
| `btcedu/web/templates/index.html` | Frontend | Add "Paused for review" option to status filter dropdown |
| `tests/test_web_review_ux.py` | Test | New file — API response tests + UI rendering logic tests |

---

## 6. Test Plan

### 6.1 API Tests (new file: `tests/test_web_review_ux.py`)

| Test | What it verifies |
|------|-----------------|
| `test_episode_dict_includes_review_context_when_pending` | Episode at CORRECTED with PENDING ReviewTask → `review_context.state == "paused_for_review"`, correct `review_task_id`, `review_stage`, `review_gate`, `next_action_text` |
| `test_episode_dict_review_context_none_when_no_review` | Episode at DOWNLOADED (no review gate) → `review_context is None` |
| `test_episode_dict_review_context_approved` | Episode at CORRECTED with APPROVED ReviewTask → `review_context.state == "review_approved"` |
| `test_episode_list_includes_review_context` | `GET /episodes` returns list where paused episode has `review_context` populated |
| `test_episode_detail_includes_review_context` | `GET /episodes/<id>` returns `review_context` for paused episode |
| `test_pipeline_state_paused` | Episode with pending review → `pipeline_state == "paused_for_review"` |
| `test_pipeline_state_failed` | Episode at FAILED → `pipeline_state == "failed"` |
| `test_pipeline_state_completed` | Episode at PUBLISHED → `pipeline_state == "completed"` |
| `test_review_context_batch_query_efficiency` | Verify only 1 extra query for review context in list endpoint (not N+1) |

### 6.2 Reviewer Helper Tests (extend existing `tests/test_reviewer.py`)

| Test | What it verifies |
|------|-----------------|
| `test_review_gate_labels_complete` | `_REVIEW_GATE_LABELS` has entries for all 4 review stages |

### 6.3 Manual Smoke Test Checklist

- [ ] Episode at `corrected` with pending review → list shows `⏸ review` badge
- [ ] Same episode → detail shows yellow "Next Action" block with "Review now" button
- [ ] "Review now" button → navigates to review panel with correct task selected
- [ ] Approve the review → refresh episode → "Next Action" changes to green "approved" block
- [ ] "Continue pipeline" button → triggers `run` action
- [ ] Episode at `failed` → detail shows red "Next Action" block with "Retry" button
- [ ] Episode at `published` → no "Next Action" block
- [ ] Status filter "Paused for review" → shows only paused episodes
- [ ] Mobile view → "Next Action" block renders correctly (no overflow)

---

## 7. Definition of Done

1. `GET /episodes` and `GET /episodes/<id>` return `review_context` and `pipeline_state` fields
2. Episode list shows `⏸ review` badge next to status for paused episodes
3. Episode detail shows "Next Action" block for paused-for-review, review-approved, and failed states
4. "Review now" button navigates to the correct review task in the review panel
5. Status filter dropdown includes "Paused for review" option
6. All new API tests pass (8+ tests)
7. Existing tests unbroken (711+ passing)
8. Ruff lint clean on all modified files
9. Manual smoke test checklist completed

---

## 8. Non-Goals

- **No redesign of the review panel itself.** The existing diff viewer, approve/reject flow, and review list are untouched.
- **No inline review panel on the episode page.** The "Review now" button navigates to the existing review screen. Inline embedding would require significant HTML restructuring and is deferred.
- **No real-time updates / WebSocket.** The dashboard already uses polling (refresh button + auto-refresh on job completion). No change.
- **No notification system.** Email/webhook/desktop notifications for new reviews are out of scope (MASTERPLAN lists this as optional).
- **No changes to the pipeline or reviewer core logic.** This is purely a read-path / display improvement.
- **No new database columns or migrations.** All data is derived from existing `review_tasks` table queries.
- **No changes to the CLI.** `btcedu review list` and `btcedu status` are unchanged.

---

## 9. Implementation Order

1. **Backend first:** Add `_get_review_context()`, `_REVIEW_GATE_LABELS`, extend `_episode_to_dict()`, update list/detail endpoints.
2. **Tests:** Write API tests against the new response shape.
3. **CSS:** Add `.next-action` styles.
4. **JS — list:** Add `renderStatusBadges()`, update `renderTable()` and `renderCards()`.
5. **JS — detail:** Add `renderNextAction()`, update `selectEpisode()`.
6. **JS — navigation:** Add `jumpToReview()`.
7. **HTML:** Add filter option.
8. **JS — filter:** Update `applyFilters()`.
9. **Smoke test:** Manual verification against live data.

Estimated scope: ~200 lines backend, ~100 lines JS, ~40 lines CSS, ~150 lines tests. Single commit.
