# Phase 5: Granular Review Actions — Implementation Output

## Status: Complete ✓

All 853 tests pass (was 629 before Phase 5; 36 new + 7 added to existing files + 1 hardcoded count updated).

---

## Files Created

| File | Purpose |
|------|---------|
| `btcedu/models/review_item.py` | `ReviewItemAction` enum + `ReviewItemDecision` SQLAlchemy model |
| `tests/test_review_item_model.py` | Model + upsert/get function tests (10 tests) |
| `tests/test_diff_item_ids.py` | item_id presence and format in diffs (5 tests) |
| `tests/test_assemble_reviewed.py` | Assembly functions + sidecar paths (14 tests) |
| `tests/test_review_item_api.py` | API endpoint tests (17 tests) |

## Files Modified

| File | Change |
|------|--------|
| `btcedu/models/review.py` | Added `ReviewItemDecision` import + `item_decisions` relationship on `ReviewTask` |
| `btcedu/migrations/__init__.py` | Added `AddReviewItemDecisionsMigration` class (inline, 007) + instance in `MIGRATIONS` |
| `btcedu/core/corrector.py` | Added `"item_id": f"corr-{len(changes):04d}"` in `compute_correction_diff()` |
| `btcedu/core/adapter.py` | Added `"item_id": f"adap-{len(adaptations):04d}"` in `compute_adaptation_diff()` |
| `btcedu/core/reviewer.py` | Added 10 new functions; updated `get_review_detail()` to include `item_decisions` |
| `btcedu/web/api.py` | Added 5 item-action routes + 2 helper functions |
| `btcedu/web/static/app.js` | Extended `renderDiffViewer()` + 9 new JS functions |
| `btcedu/web/static/styles.css` | Added `.diff-item-*` CSS classes |
| `btcedu/core/translator.py` | Sidecar detection for `transcript.reviewed.de.txt` |
| `btcedu/core/chapterizer.py` | Sidecar detection for `script.adapted.reviewed.tr.md` |
| `tests/test_reviewer.py` | Added `test_get_review_detail_includes_item_decisions_key` |
| `tests/test_corrector.py` | Added `test_compute_correction_diff_has_item_id` |
| `tests/test_adapter.py` | Added `test_compute_adaptation_diff_has_item_id` |
| `tests/test_sprint1_migrations.py` | Updated hardcoded pending-count assertion from 5 → 6 |

---

## Decisions Made (vs. Plan)

### 1. `UNCHANGED` kept as distinct action
**Decision:** Kept. The enum has 5 values: `PENDING`, `ACCEPTED`, `REJECTED`, `EDITED`, `UNCHANGED`.
**Rationale:** The plan explicitly includes `UNCHANGED` in the UI summary bar and assembly logic. Assembly treats `UNCHANGED` identically to `REJECTED` (emit original words). This is documented in `review_item.py` inline comment and the `_assemble_correction_review` logic.

### 2. Item actions allowed only for `PENDING` and `IN_REVIEW`
**Decision:** Actions are disallowed when `status` is `CHANGES_REQUESTED`, `APPROVED`, or `REJECTED`.
**Rationale:** This follows `_validate_actionable()` in `reviewer.py` (existing behavior). When a task is `CHANGES_REQUESTED`, the whole task has been actioned and the episode has been reverted for re-processing. A new ReviewTask will be created when the pipeline re-runs. This is consistent with the whole-review flow.

### 3. Import strategy for `ReviewItemDecision` in `review.py`
**Decision:** Changed from `TYPE_CHECKING`-only guard to a real module-level import.
**Rationale:** SQLAlchemy's string-based relationship `"ReviewItemDecision"` requires the class to be registered in the mapper registry before mappers are configured. A `TYPE_CHECKING`-only import means the class is never registered at runtime, causing `InvalidRequestError`. Since `review_item.py` only back-references `"ReviewTask"` as a forward string (no import of `review.py`), there is no circular import. The real import was added with a `# noqa: E402` comment to flag the unusual placement.

### 4. `apply` requires at least one decision
**Decision:** `POST /api/reviews/<id>/apply` returns 400 if no `ReviewItemDecision` records exist.
**Rationale:** Per plan Section 6. Prevents accidentally writing a "reviewed" sidecar that is identical to the original pipeline output without any reviewer intent.

### 5. Pending items treated as accepted (explicit in UI message)
**Decision:** Pending items default to accepting the proposed change in assembly.
**Rationale:** Per plan Section 8 rule 6. The toast message from "Apply" explicitly says "N items still pending — pending items treated as accepted." when `pending_count > 0`. This makes the implicit behavior explicit to the reviewer.

---

## New API Endpoints

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/reviews/<id>/items/<item_id>/accept` | Accept a diff item |
| POST | `/api/reviews/<id>/items/<item_id>/reject` | Reject a diff item (revert to original) |
| POST | `/api/reviews/<id>/items/<item_id>/edit` | Set custom replacement text |
| POST | `/api/reviews/<id>/items/<item_id>/reset` | Reset item to pending |
| POST | `/api/reviews/<id>/apply` | Assemble sidecar file from decisions |

All endpoints return 404 for nonexistent reviews, 400 for non-actionable status.

---

## New Functions in `btcedu/core/reviewer.py`

- `upsert_item_decision(session, review_task_id, item_id, action, edited_text)` — create/update per-item decision
- `get_item_decisions(session, review_task_id)` → `dict[str, ReviewItemDecision]`
- `apply_item_decisions(session, review_task_id)` → sidecar path string
- `_sidecar_path(episode_id, stage, settings)` → `Path`
- `_load_item_texts_from_diff(task, item_id)` → `(original, proposed, op_type)`
- `_ensure_item_ids_correction(changes)` — backward compat: inject `item_id` if missing
- `_ensure_item_ids_adaptation(adaptations)` — backward compat: inject `item_id` if missing
- `_assemble_correction_review(original_text, diff_changes, item_decisions)` → str
- `_assemble_adaptation_review(adapted_text, diff_adaptations, item_decisions)` → str

---

## Backward Compatibility

Old diff files without `item_id` are handled via `_ensure_item_ids_correction()` / `_ensure_item_ids_adaptation()` which inject sequential IDs on-the-fly (same deterministic formula as new diffs). `GET /api/reviews/<id>` returns `item_decisions: {}` for tasks with old diffs (no crash, no data).

---

## Manual Verification Steps

1. **Run migration 007:**
   ```
   btcedu migrate
   btcedu migrate-status  # should show 007 as applied
   ```

2. **Trigger a correction review and navigate to it in the web dashboard:**
   - Each diff row should show Accept / Reject / Edit / Reset buttons
   - Summary counts bar should appear below the diff summary

3. **Test item actions:**
   - Click Accept on a diff row → row gets green left border, Accept button goes active
   - Click Reject → row gets red left border, proposed text gets strikethrough
   - Click Edit → inline textarea appears pre-filled with proposed text
   - Type custom text, click Save → row gets indigo border, textarea disappears
   - Click Reset → styling clears back to "pending" state

4. **Test Apply:**
   - Click "Apply Accepted Changes" without any item decisions → error toast
   - Accept at least one item, then click Apply → success toast with pending count
   - `data/outputs/{ep_id}/review/transcript.reviewed.de.txt` should exist
   - Run `btcedu translate --episode-id {ep_id}` → should say "Using reviewed transcript sidecar"

5. **Whole-review flow still works:**
   - Clicking Approve / Reject / Request Changes works the same as before Phase 5

6. **Backward compat:**
   - Open an old review task whose diff has no `item_id` → page loads, `item_decisions` dict is empty, no JS errors
