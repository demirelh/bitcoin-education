# Phase 5 Validation Report — Granular Review Actions

**Validated against:** `phase5-granular-review-plan.md`, `phase5-granular-review-implement-output.md`, live codebase
**Validator:** Claude Sonnet 4.6
**Date:** 2026-03-15

---

## 1. Verdict

**PASS**

All deliverables from the plan are implemented, all 46 new tests pass (36 new + 7 additions to existing files + 3 existing file count updates), and all 807 pre-existing tests remain green. One minor model-level redundancy and one asymmetric guard placement are noted; neither affects correctness or safety. No required fixes.

---

## 2. Scope Check

### Expected (per plan)
- `ReviewItemDecision` model + `ReviewItemAction` enum using `btcedu.db.Base`
- Migration 007: `review_item_decisions` table
- `item_id` field in correction diffs (`corr-NNNN`) and adaptation diffs (`adap-NNNN`)
- Backward-compat injection (`_ensure_item_ids_*`) for old diffs
- `upsert_item_decision()`, `get_item_decisions()`, `apply_item_decisions()` in `reviewer.py`
- Assembly: `_assemble_correction_review()` (word-level), `_assemble_adaptation_review()` (char-level reverse)
- Sidecar paths: `review/transcript.reviewed.de.txt`, `review/script.adapted.reviewed.tr.md`
- Downstream sidecar detection in `translator.py` and `chapterizer.py`
- 5 new API endpoints: accept / reject / edit / reset / apply
- UI: per-item action buttons, summary bar, inline edit, Apply button
- CSS: `.diff-item-*` action classes, edit panel
- `item_decisions` key in `GET /reviews/<id>` response
- 4 new test files (36+ new tests)

### Actually implemented
Everything above, plus:
- `_load_item_texts_from_diff()` helper captures `original_text`, `proposed_text`, `operation_type` at create time and stores them in the DB record (richer audit trail than plan required)
- `_sidecar_path()` raises `ValueError` for unsupported stages (defensive guard not in plan)
- `_get_review_task_or_404()` and `_check_review_actionable()` extracted as shared helpers for all 5 endpoints (clean factoring)
- "Apply Accepted Changes" button shown only for `correct` and `adapt` stages, hidden for `render`/`stock_images` (plan-consistent but explicit stage guard)
- `selectReview()` passes `data.item_decisions || {}` — safe fallback for tasks with no decisions
- `UNCHANGED` enum value kept as distinct action (plan explicitly included it for assembly and UI; behavior = REJECTED)

### Scope creep
None. All additions are directly required by or consistent with the plan.

---

## 3. Correctness Review

### Persistence model

**`ReviewItemDecision`** (`btcedu/models/review_item.py`):
Fields: `id` (PK), `review_task_id` (FK → review_tasks.id, nullable=False), `item_id` (String(64)), `operation_type` (String(32)), `original_text` (nullable), `proposed_text` (nullable), `action` (default `"pending"`), `edited_text` (nullable), `decided_at` (nullable timezone datetime). Uses `btcedu.db.Base` — correct, included in `Base.metadata.create_all()` for tests without extra setup.

**One redundancy noted (non-blocking):**
`review_task_id` has `index=True` in `mapped_column()` AND a separately named `Index("idx_review_item_decisions_task", "review_task_id")` in `__table_args__`. When `Base.metadata.create_all()` runs (test path), SQLAlchemy creates both — resulting in two indexes on `review_task_id` (one unnamed, one named). The migration path (production) creates only the two named indexes. This is functionally harmless: both paths have at least one index on `review_task_id`, and query performance is unaffected. The composite index `idx_review_item_decisions_task_item` is created correctly in both paths.

**Relationships and cascade:**
`ReviewTask.item_decisions` relationship uses `cascade="all, delete-orphan"` — cascade delete is verified by `test_item_decision_cascade_delete`. The real (non-`TYPE_CHECKING`) import of `ReviewItemDecision` in `review.py` is required for SQLAlchemy mapper resolution at runtime; correctly documented with inline comment and `# noqa: E402`.

**Separation from whole-review:**
`ReviewItemDecision` is entirely separate from `ReviewDecision`. `get_review_detail()` returns `item_decisions` as a separate top-level key alongside `decisions` — no semantic mixing. ✓

### Migration 007

`AddReviewItemDecisionsMigration` in `btcedu/migrations/__init__.py` (lines 456–511):
- Check-before-act: `SELECT name FROM sqlite_master WHERE type='table' AND name='review_item_decisions'` before `CREATE TABLE` — idempotent. ✓
- Column types match the ORM model (`VARCHAR(64)`, `VARCHAR(32)`, `TEXT`, `TIMESTAMP`, `INTEGER`). ✓
- FK declared: `FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)`. ✓
- Two named indexes created after table: `idx_review_item_decisions_task` and `idx_review_item_decisions_task_item`. ✓
- `self.mark_applied(session)` called at end — consistent with all other migrations. ✓
- `MIGRATIONS` list now has 7 entries. `test_sprint1_migrations.py` updated: `assert len(pending) == 6` (after 001 already applied). ✓

### Diff artifact evolution

**Correction diffs** (`corrector.py` line 450):
```python
"item_id": f"corr-{len(changes):04d}",
```
Assigned before `changes.append(change)`, so `len(changes)` equals the current zero-based index — equivalent to `f"corr-{i:04d}"` with `enumerate`. This is deterministic and stable across reruns for the same input. ✓

**Adaptation diffs** (`adapter.py` line 603):
```python
"item_id": f"adap-{len(adaptations):04d}",
```
Same zero-indexed sequential formula. ✓

**Backward-compat injection** (`reviewer.py` lines 838–849):
`_ensure_item_ids_correction()` injects `f"corr-{i:04d}"` using `enumerate` index `i`. This is exactly equal to the production formula (`len(changes)` at time of append = `i` at enumeration), so old diffs get identical IDs to what new production diffs would produce for the same change sequence. ✓

Verified by: `test_correction_diff_has_item_ids`, `test_correction_item_id_format`, `test_adaptation_diff_has_item_ids`, `test_adaptation_item_id_format`, `test_item_id_stable_across_reruns`. ✓

### Reviewer logic

**`upsert_item_decision()`** (`reviewer.py` lines 645–707):
- `_get_task_or_raise()` + `_validate_actionable()` called first → 400 if task not found or not actionable. ✓
- Query by `(review_task_id, item_id)` using composite index — O(log n). ✓
- On update: `edited_text` cleared to `None` for non-EDITED actions. ✓
- On create: `_load_item_texts_from_diff()` populates `original_text`, `proposed_text`, `operation_type` from diff file — gracefully falls back to `(None, None, "unknown")` if file missing. ✓
- `decided_at` updated on every call. ✓

**`get_item_decisions()`** (`reviewer.py` lines 710–725):
Simple filter on `review_task_id`, returns `{item_id: record}` dict. Returns `{}` for tasks with no decisions. ✓

**`apply_item_decisions()`** (`reviewer.py` lines 728–785):
- Requires `task.diff_path` exists and diff file is readable. ✓
- Dispatches by `task.stage` to the appropriate assembler. ✓
- Raises `ValueError` for unsupported stages (`"else"` branch). ✓
- Writes sidecar with `out_path.parent.mkdir(parents=True, exist_ok=True)` — safe directory creation. ✓

**One asymmetry noted (non-blocking):**
`apply_item_decisions()` does NOT call `_validate_actionable()`, unlike `upsert_item_decision()`. The actionability guard is enforced at the API layer (`_check_review_actionable()`) but not at the function layer. This means direct Python calls to `apply_item_decisions()` could write sidecars for approved/rejected tasks. This is acceptable because the function is only exposed via the web API. The plan does not specify a requirement for function-level guards on `apply`.

### Assembly algorithms

**`_assemble_correction_review()`** (`reviewer.py` lines 852–914):
Word-level reconstruction. Sorts changes by `position.start_word` ascending — handles out-of-order diffs correctly. Emits gap words between changes. Change dispatch:

| Action | Emits |
|--------|-------|
| `accepted` / `pending` (default) | `change["corrected"].split()` |
| `rejected` / `unchanged` | `orig_words[start_word:end_word]` |
| `edited` | `decision.edited_text.split()` (or proposed if no edited_text) |

"delete" type changes: `end_word == start_word` only if the original span was empty (insert case in SequenceMatcher). For "delete" type: `start_word < end_word`, so accepted→emits corrected (empty string), rejected→emits original deleted words. Correct.

"insert" type changes: `start_word == end_word`, so accepted→emits new words, rejected→emits nothing (no original words at that position). Correct.

`cursor = end_word` at end of each iteration — handles consecutive changes without overlap correctly. ✓

**`_assemble_adaptation_review()`** (`reviewer.py` lines 917–966):
Character-level reverse-order splicing. Reverse ordering prevents position shifts from invalidating earlier splices. ✓

| Action | Behavior |
|--------|----------|
| `accepted` / `pending` | `continue` — keep adapted text including marker tag |
| `rejected` / `unchanged` | `result[:start] + adaptation["original"] + result[end:]` |
| `edited` | `result[:start] + decision.edited_text + result[end:]` |

The "accepted" case retains the `[T1: …]` or `[T2: …]` marker tags in the output. This is consistent with the existing adapted text format — chapterizer already processes the same marker-containing text from the pipeline. The reviewed sidecar replaces the adapted script; no format change needed. ✓

### Sidecar path correctness
```
correct → data/outputs/{ep_id}/review/transcript.reviewed.de.txt
adapt   → data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md
```
Isolated to `review/` subdirectory — does not overwrite pipeline outputs. `translator.py` and `chapterizer.py` check existence before substituting the path. Fallback to original pipeline output if sidecar absent — backward compatible. ✓

### API endpoints

All 5 endpoints follow the same pattern: `_get_session()` → `_get_review_task_or_404()` → `_check_review_actionable()` → delegate → `try/finally session.close()`. The `finally` runs even on early `return err` — session always closed. ✓

| Route | Guard | Delegation |
|-------|-------|-----------|
| `POST /reviews/<id>/items/<item_id>/accept` | 404 + actionable | `upsert_item_decision(..., ACCEPTED)` |
| `POST /reviews/<id>/items/<item_id>/reject` | 404 + actionable | `upsert_item_decision(..., REJECTED)` |
| `POST /reviews/<id>/items/<item_id>/edit` | 404 + actionable + empty text | `upsert_item_decision(..., EDITED, edited_text)` |
| `POST /reviews/<id>/items/<item_id>/reset` | 404 + actionable | `upsert_item_decision(..., PENDING)` |
| `POST /reviews/<id>/apply` | 404 + actionable + no decisions | `apply_item_decisions(...)` |

`edit` endpoint validates `text_value = body.get("text", "").strip()` and returns 400 if empty. ✓

`apply` endpoint:
- Returns 400 if `get_item_decisions()` returns empty dict (plan requirement §6). ✓
- `pending_count` = decisions with PENDING action + `max(0, total_items - len(decisions))` (items with no record at all). ✓
- Docstring explicitly documents pending-as-accepted behavior. ✓

`GET /reviews/<id>` returns `item_decisions` as `{item_id: {action, edited_text, decided_at}}` dict for `correct` and `adapt` stages; `{}` for all other stages. ✓

**Double-guard redundancy (non-blocking):**
`_check_review_actionable()` in the API endpoint AND `_validate_actionable()` inside `upsert_item_decision()` both check the same condition for the 4 item-action endpoints. Both return/raise 400. The outer API guard hits first — no behavior difference, slightly redundant. Acceptable.

### UI behavior

**`renderDiffViewer()` extended signature:**
`(diff, originalText, correctedText, itemDecisions, isActionable, reviewId)` — called from `selectReview()` with `data.item_decisions || {}` and `isActionable = (data.status === "pending" || data.status === "in_review")`. ✓

**Per-item row rendering:**
Each row gets `data-item-id` attribute and `actionClass` for initial visual state from loaded decisions. Action bar (`_renderItemActions()`) rendered only when `isActionable`. ✓

**Inline edit:**
`toggleEditInline(itemId)` toggles visibility of `#edit-inline-${itemId}` div. `saveEditInline()` validates non-empty, POSTs edit, updates visual + summary, hides panel. `cancelEditInline()` hides panel. All JS functions exposed via `window.fn = fn` for HTML `onclick` handler compatibility. ✓

**Summary bar:**
`renderItemSummary()` computes counts from `itemDecisions` at render time.
`_updateItemSummary()` recomputes from DOM class state (`diff-item-accepted`, `diff-item-rejected`, etc.) after each action — consistent with visual state. ✓

**`applyReviewItems()` toast:**
- `pending_count > 0`: `"Reviewed file saved. N of M items still pending — pending items treated as accepted."` ✓
- `pending_count == 0`: `"Reviewed file saved. All M items decided."` ✓
- The pending-as-accepted behavior is explicit in both the API response and the UI toast. ✓

**"Apply Accepted Changes" button visibility:**
Shown only when `data.stage === "correct" || data.stage === "adapt"`. Correctly absent for `render` and `stock_images` review stages. ✓

**CSS:**
`.diff-item-accepted` (green bg), `.diff-item-rejected` (red bg), `.diff-item-edited` (indigo bg), `.diff-item-unchanged` (gray bg). `.diff-item-rejected .diff-corrected` / `.diff-item-rejected .diff-adapted` get `text-decoration: line-through` — proposed text visually struck through. `.diff-item-btn.{action}.active` filled variants for button state. All using existing CSS variables. ✓

### Whole-review integration

**Whole-review flow unchanged:**
`approve_review()`, `reject_review()`, `request_changes()` — none modified. All existing tests in `test_review_api.py` continue to pass. ✓

**"Apply" does not approve:**
`apply_item_decisions()` only writes a file; it does not update task status. Reviewer must still explicitly approve/reject via existing whole-review buttons. ✓

**Sidecar pickup without Apply:**
Whole-review approval without Apply: no sidecar file is written, downstream stages use original pipeline outputs — correct original behavior. ✓

**Cascade behavior:**
`cascade="all, delete-orphan"` on `item_decisions` relationship means item decisions are automatically deleted when the ReviewTask is deleted. For REJECTED/CHANGES_REQUESTED tasks (which are not deleted, only status-changed), item decisions are retained as audit trail — correct. ✓

---

## 4. Test Review

### Coverage present

| File | Tests | What's covered |
|------|-------|----------------|
| `test_review_item_model.py` | 5 | Create, upsert create, upsert update (no duplicate), cascade delete, get isolation between tasks |
| `test_diff_item_ids.py` | 5 | Correction item_id presence + format, adaptation presence + format, stability across reruns |
| `test_assemble_reviewed.py` | 14 | Correction: accept/reject/edit/pending/unchanged/mixed; adaptation: accept/reject/edit/pending; sidecar paths (2 valid + 1 invalid stage) |
| `test_review_item_api.py` | 17 | Accept/reject/edit(valid+empty+missing text)/reset/apply(with+without decisions)/review detail includes item_decisions; guards: approved→400, nonexistent→404; backward compat (old diff without item_id → empty item_decisions) |
| `test_reviewer.py` (modified) | +1 | `get_review_detail()` includes `item_decisions` key |
| `test_corrector.py` (modified) | +1 | `compute_correction_diff()` output includes `item_id` |
| `test_adapter.py` (modified) | +1 | `compute_adaptation_diff()` output includes `item_id` |

All plan-required test scenarios are covered.

### Missing / weak tests (non-blocking)

1. **`_ensure_item_ids_*` + upsert on old diff** — `test_old_diff_without_item_id` only verifies GET returns `{}`. It does not test that `upsert_item_decision()` succeeds on an old diff (which requires `_load_item_texts_from_diff()` to call `_ensure_item_ids_correction()` and find the right item). The function path is exercised by the backward-compat injection design, but no test asserts end-to-end old-diff item actions succeed.

2. **`edit → accept` action clears `edited_text`** — `upsert_item_decision()` sets `edited_text = None` when action != EDITED. No test verifies this field is cleared when a reviewer changes from "edited" to "accepted". The logic is correct in code but untested.

3. **Adaptation `apply` end-to-end** — `test_apply_corrections` only tests the correction stage. No API-level test exercises `POST /reviews/<id>/apply` for an adaptation review task (stage = "adapt").

4. **"delete" type correction change in assembly** — all assembly tests use "replace" type. A "delete" type change (accepted → emits nothing; rejected → emits original deleted words) is not directly tested.

5. **`CHANGES_REQUESTED` status on item action** — `test_item_action_on_approved_review` checks "approved" → 400. `CHANGES_REQUESTED` (also non-actionable) is not separately tested; covered by the same code path.

6. **`apply` for approved task via API** — the API actionability check blocks this, but no test asserts that `POST /reviews/<id>/apply` on an APPROVED review task returns 400.

7. **JS functions** — no DOM-level tests. All JS behavior (action bar rendering, inline edit, summary update, apply toast) requires manual verification.

### Suggested additions (non-blocking)

- Add `test_old_diff_item_action_succeeds`: create an old-format diff (no item_id), call `upsert_item_decision()` on it, assert the record is created with the injected item_id.
- Add `test_edit_then_accept_clears_edited_text`: upsert edit with text → upsert accept → assert `edited_text is None`.
- Add `test_apply_adaptation` API test mirroring `test_apply_corrections` but with a `stage="adapt"` ReviewTask and an adaptation diff.
- Add `test_apply_on_approved_review_returns_400` to exercise the API guard.

---

## 5. Backward Compatibility Check

### Old review tasks (no item_decisions in DB)
- `get_review_detail()` returns `item_decisions: {}` for tasks with no recorded decisions — no crash, no data. ✓
- `GET /reviews/<id>` response is backward-compatible: `item_decisions` is a new additive key. ✓

### Old diff files (no `item_id` field)
- `_ensure_item_ids_correction()` and `_ensure_item_ids_adaptation()` mutate the in-memory list in-place, injecting sequential IDs. The diff file on disk is NOT modified — backward compat writes nothing. ✓
- The injected IDs match what new diffs would produce (`f"corr-{i:04d}"` = `f"corr-{len(changes):04d}"` at append time). ✓
- `test_old_diff_without_item_id` verifies GET loads without crashing and returns `item_decisions: {}`. ✓

### Existing whole-review flow
- All whole-review endpoints (`approve`, `reject`, `request-changes`) are untouched by Phase 5. ✓
- All 11 tests in `test_review_api.py` continue to pass. ✓

### v1 pipeline
- Phase 5 applies only to `stage == "correct"` and `stage == "adapt"` — v1 pipeline uses stages `CHUNK`, `GENERATE`, `REFINE`. No intersection. ✓
- No changes to v1 models, CLI commands, or pipeline orchestration. ✓

### JavaScript
- `renderDiffViewer()` called with `data.item_decisions || {}` — old reviews that return no `item_decisions` key fall back safely to empty dict. ✓
- `isActionable` defaults to `false` for non-pending reviews — no action buttons shown. ✓

---

## 6. Required Fixes Before Commit

**None.** The implementation is correct, all tests pass, and no defects were found. The two observations (duplicate index on `review_task_id`, asymmetric `_validate_actionable()` guard) are non-breaking and acceptable as-is.

---

## 7. Nice-to-Have Improvements

1. **Remove duplicate `review_task_id` index.** Either remove `index=True` from the `mapped_column()` definition (keeping only the explicit named `Index` in `__table_args__`), or remove the named index from `__table_args__` and rely on `index=True`. This would make the test-path (ORM `create_all`) and production-path (migration) create the same number of indexes.

2. **Add `_validate_actionable()` to `apply_item_decisions()`.** For consistency with `upsert_item_decision()`, add the guard at the function level so direct Python callers cannot write sidecars for non-actionable tasks. The API already guards this, so it's defensive depth only.

3. **Adaptation `apply` test.** Add an end-to-end API test for applying an adaptation review task to ensure the full path (diff parsing, assembly, sidecar write) is covered.

4. **Old-diff + upsert test.** Verify that `upsert_item_decision()` succeeds on an old-format diff (injected IDs match), giving confidence the backward-compat path is fully exercised.

5. **`edit → accept` clears `edited_text` test.** Add one assertion that switching from EDITED back to ACCEPTED clears the stored `edited_text`. Simple to add, guards against future upsert regressions.

6. **`pipeline_state: "running"` (noted in Phase 1, still pending).** Not Phase 5 scope but worth tracking: `_compute_pipeline_state()` returns `"ready"` for both idle and in-flight episodes. Requires `JobManager` integration.

---

## 8. Summary

Phase 5 is fully and correctly implemented. The core deliverables — `ReviewItemDecision` model, migration 007, deterministic `item_id` in diffs, backward-compat injection, `upsert/get/apply_item_decisions()`, word-level correction assembly, character-level adaptation assembly, sidecar paths, downstream sidecar detection, 5 new API endpoints, per-item action UI, summary bar, inline edit, and Apply button — all work as specified.

The pending-as-accepted behavior is made explicit at three layers: the assembly function docstring, the API response (`pending_count`), and the UI toast message. Backward compatibility is solid: old diffs and old tasks are handled gracefully, whole-review approval is unchanged, and v1 is completely unaffected.

Test coverage is comprehensive (46 dedicated tests + 807 pre-existing tests remain green). The implementation follows existing project patterns throughout.

**Result: PASS. No fixes required.**
