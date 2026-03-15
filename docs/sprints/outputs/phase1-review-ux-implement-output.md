# Phase 1: Review UX Improvements — Implementation Output

**Date:** 2026-03-15
**Plan:** `docs/sprints/outputs/phase1-review-ux-plan.md`
**Status:** Complete

---

## Summary

Made review pauses obvious and actionable in the existing episode UI. Episodes paused at review gates now display clear visual indicators in both the list and detail views, with one-click navigation to the relevant review task.

---

## Changes Made

### Backend: `btcedu/web/api.py`

| Change | Lines | Description |
|--------|-------|-------------|
| `_REVIEW_GATE_LABELS` | 186-191 | Dict mapping review stage names to `(gate_name, label)` tuples |
| `_REVIEW_GATE_STATUS_MAP` | 194-199 | Dict mapping episode statuses to their blocking review stage |
| `_get_review_context()` | 202-283 | New helper: queries pending/approved ReviewTask for an episode and returns a structured dict with state, task ID, stage label, action text, action URL |
| `_compute_pipeline_state()` | 286-295 | New helper: derives `paused_for_review` / `failed` / `completed` / `ready` from status + review context |
| `_episode_to_dict()` | 298-332 | Extended: new `session` and `pending_cache` params; adds `pipeline_version`, `review_context`, `pipeline_state` to output |
| `list_episodes()` | 340-377 | Updated: batch query fetches all pending ReviewTasks in 1 query; passes `pending_cache` to `_episode_to_dict()` |
| `get_episode()` | 381-392 | Updated: passes `session` to `_episode_to_dict()` |

### Frontend: `btcedu/web/static/app.js`

| Change | Description |
|--------|-------------|
| `renderStatusBadges(ep)` | New function: renders status badge + optional `"review"` badge when paused |
| `renderNextAction(ep)` | New function: renders color-coded Next Action block (yellow=review pending, green=approved, red=failed) |
| `jumpToReview(reviewId)` | New function: navigates to review panel and selects specific review task |
| `renderTable()` | Updated: uses `renderStatusBadges()` instead of inline badge |
| `renderCards()` | Updated: uses `renderStatusBadges()` instead of inline badge |
| `selectEpisode()` | Updated: uses `renderStatusBadges()` in meta line; inserts `renderNextAction()` block between meta and actions |
| `applyFilters()` | Updated: handles `review_pending` special filter value |

### Frontend: `btcedu/web/static/styles.css`

Added `.next-action` block styles (~35 lines): flex layout, color-coded variants (review/approved/failed), icon sizing, body typography, button spacing.

### Frontend: `btcedu/web/templates/index.html`

Added `<option value="review_pending">Paused for review</option>` to the status filter dropdown.

---

## API Response Shape Changes

### `GET /episodes` and `GET /episodes/<id>`

Three new fields added to each episode object:

```json
{
  "...existing fields...",
  "pipeline_version": 2,
  "review_context": {
    "state": "paused_for_review",
    "review_task_id": 42,
    "review_stage": "correct",
    "review_stage_label": "Transcript Correction Review",
    "review_status": "pending",
    "review_gate": "review_gate_1",
    "created_at": "2026-03-15T04:32:00",
    "next_action_text": "Pipeline paused \u2014 Review Gate 1 requires approval",
    "action_url": "/api/reviews/42"
  },
  "pipeline_state": "paused_for_review"
}
```

`review_context` is `null` when no review gate applies. `pipeline_state` is always present: one of `paused_for_review`, `failed`, `completed`, or `ready`.

---

## Tests

### New file: `tests/test_web_review_ux.py` (25 tests)

| Test Class | Tests | What it covers |
|-----------|-------|----------------|
| `TestGetReviewContext` | 6 | Unit tests for `_get_review_context()`: pending, approved, no-review, published, cache hit, cache miss |
| `TestComputePipelineState` | 7 | Unit tests for `_compute_pipeline_state()`: all state derivation paths |
| `TestReviewGateLabels` | 2 | Label completeness: all 4 stages present, status map consistency |
| `TestEpisodeListReviewContext` | 6 | Integration: list endpoint includes review_context, pipeline_version, pipeline_state for all status variants |
| `TestEpisodeDetailReviewContext` | 2 | Integration: detail endpoint includes review_context with action_url |
| `TestBatchQueryEfficiency` | 1 | Verifies batch query works with 15 episodes (no N+1) |
| `TestFilterOption` | 1 | HTML includes review_pending filter option |

### Full suite results

```
736 passed, 0 failed, 33 warnings in 71.08s
```

(711 pre-existing + 25 new = 736)

---

## Assumptions Made

1. **No `running` pipeline_state**: The plan noted this would require querying the job manager, so it was deferred. `pipeline_state` returns `"ready"` for in-progress episodes without pending reviews.
2. **Backward compatibility**: `_episode_to_dict(ep, settings)` still works without `session` — returns `review_context: null` and `pipeline_state` based on status alone. Existing callers unaffected.
3. **HTML entities for icons**: Used `&#9208;` (pause) and `&#10003;`/`&#10007;` (check/cross) instead of emoji for broader font compatibility.

---

## Manual Smoke Test Checklist

- [ ] Episode at `corrected` with pending review: list shows `review` badge
- [ ] Same episode: detail shows yellow "Next Action" block with "Review now" button
- [ ] "Review now" button: navigates to review panel with correct task selected
- [ ] Approve the review: refresh episode: "Next Action" changes to green "approved" block
- [ ] "Continue pipeline" button: triggers `run` action
- [ ] Episode at `failed`: detail shows red "Next Action" block with "Retry" button
- [ ] Episode at `published`: no "Next Action" block
- [ ] Status filter "Paused for review": shows only paused episodes
- [ ] Mobile view: "Next Action" block renders correctly (no overflow)

---

## Files Modified

| File | Type | Lines changed |
|------|------|---------------|
| `btcedu/web/api.py` | Backend | ~150 added |
| `btcedu/web/static/app.js` | Frontend | ~60 added |
| `btcedu/web/static/styles.css` | Frontend | ~35 added |
| `btcedu/web/templates/index.html` | Frontend | 1 line added |
| `tests/test_web_review_ux.py` | Tests | 290 lines (new file) |
| `docs/sprints/outputs/phase1-review-ux-implement-output.md` | Docs | This file |
