# Sprint 3 — Validation Output: Review System + Dashboard Integration

**Sprint**: 3 (Phase 1, Part 2)
**Validation date**: 2026-02-23
**Validator**: Claude Opus 4.6
**Test results**: 338 passed, 0 failures (309 existing + 29 new)

---

# 1) Verdict

**PASS WITH FIXES**

Sprint 3 is well-implemented and complete in scope. The review system, pipeline integration, dashboard UI, CLI commands, and feedback injection all work correctly. Two minor issues require attention before committing; neither is a blocker for Sprint 4.

---

# 2) Scope Check

## In-scope items implemented

| Item | Status | Notes |
|------|--------|-------|
| `btcedu/core/reviewer.py` — 10 public functions + 5 helpers | DONE | Clean, well-structured module |
| Review Gate 1 in pipeline after CORRECT stage | DONE | `review_gate_1` in `_V2_STAGES`, `review_pending` handling |
| 6 Flask API review routes | DONE | list, count, detail, approve, reject, request-changes |
| Dashboard: Reviews button with badge | DONE | Badge polls via `/api/reviews/count` in `refresh()` |
| Dashboard: Review queue panel (list + detail) | DONE | 2-column grid, section headers, status badges |
| Dashboard: Diff viewer component | DONE | Summary bar, change cards, side-by-side panels |
| Dashboard: Approve/reject/request-changes buttons | DONE | With confirmation dialog for reject, notes textarea for changes |
| CLI: `btcedu review list\|approve\|reject` | DONE | Follows existing CLI patterns |
| Reviewer feedback injection into corrector | DONE | `{{ reviewer_feedback }}` placeholder, German-language block |
| 20 reviewer unit tests | DONE | Comprehensive coverage of all public functions |
| 9 Flask API tests | DONE | All endpoints tested including error cases |

## Out-of-scope changes detected

**None.** The implementation is tightly scoped to Sprint 3 deliverables only. No scope creep detected:

- No Review Gate 2 or 3
- No auto-approve rules
- No notification system
- No authentication
- No inline editing
- No TRANSLATE/ADAPT stages
- No batch review
- No unnecessary refactoring of existing code
- Existing dashboard pages untouched (only nav badge added)

---

# 3) Correctness Review

## Key components reviewed

### 3.1 Reviewer Module (`btcedu/core/reviewer.py`)

**PASS** — All functions are correct:

- `create_review_task()`: Creates PENDING task with artifact hash. Commits atomically.
- `approve_review()`: Validates actionable state, sets APPROVED, recomputes artifact hash at approval time, creates ReviewDecision. Does NOT advance episode (correct per plan).
- `reject_review()`: Validates, sets REJECTED, reverts episode CORRECTED→TRANSCRIBED, creates decision.
- `request_changes()`: Requires non-empty notes (ValueError if empty), reverts episode, creates `.stale` markers, stores feedback on task.
- `_validate_actionable()`: Correctly prevents double-action on decided tasks.
- `_revert_episode()`: Only handles CORRECTED→TRANSCRIBED (Gate 1 scope). Logs warning for unexpected states.
- `has_approved_review()`: Checks latest task by `created_at DESC` — correct for multiple review cycles.
- `get_latest_reviewer_feedback()`: Returns notes from most recent CHANGES_REQUESTED task.

### 3.2 Pipeline Integration (`btcedu/core/pipeline.py`)

**PASS** — Review gate integration is clean:

- `review_gate_1` added to `_V2_STAGES` at the correct position (after correct, before future translate).
- Three-way check: approved → success, pending exists → review_pending, otherwise → create task + review_pending.
- `review_pending` status causes clean `break` in pipeline loop — no error recorded, `report.success = True`.
- `run_pending()` and `run_latest()` filter out episodes with active review tasks — prevents wasteful re-processing.
- Lazy imports from reviewer to avoid circular dependencies.
- v1 stages (`_V1_STAGES`) completely untouched.

### 3.3 Corrector Feedback Injection (`btcedu/core/corrector.py`)

**PASS** — Feedback injection is well-positioned:

- Imports `get_latest_reviewer_feedback` lazily inside the function.
- Replaces `{{ reviewer_feedback }}` before `_split_prompt()`, so feedback ends up in the system prompt section.
- German-language feedback block: "Reviewer-Korrekturen (bitte diese Anmerkungen bei der Korrektur berücksichtigen)".
- Empty string replacement when no feedback — clean no-op.
- `.stale` marker mechanism works: `_is_correction_current()` returns False when stale marker exists, triggering re-correction.

### 3.4 Prompt Template (`correct_transcript.md`)

**PASS** — `{{ reviewer_feedback }}` placeholder is correctly positioned between the "WAS NICHT ZU KORRIGIEREN IST" section and the `# Transkript` marker.

### 3.5 Flask API Routes (`btcedu/web/api.py`)

**PASS** — All 6 routes follow existing patterns:

- `_get_session()` / try-finally pattern used throughout.
- POST routes validate input and return 400 for errors.
- `request-changes` route validates notes are non-empty at the route level (defense in depth with reviewer module).
- Error handling: `ValueError` from reviewer functions caught and returned as 400 JSON.
- Routes registered on existing `api_bp` blueprint — no new blueprint needed.

### 3.6 Dashboard UI

**PASS** — SPA integration is correct:

- Reviews button with badge in topbar.
- `showReviews()` / `hideReviews()` toggles between main panel and review panel.
- Badge polling via `updateReviewBadge()` called in `refresh()`.
- Diff viewer uses `esc()` helper for HTML escaping (XSS protection).
- All user-supplied text (titles, notes, diff content) properly escaped.
- Reject action has `confirm()` dialog.
- Request-changes requires non-empty notes before submitting.

### 3.7 CLI Commands (`btcedu/cli.py`)

**PASS** — Clean Click group with 3 subcommands:

- `review list`: Tabular output, default shows pending/in_review, optional `--status` filter.
- `review approve REVIEW_ID`: With optional `--notes`.
- `review reject REVIEW_ID`: With optional `--notes`.
- Error handling via try/except ValueError → `[FAIL]` output.

## Risks / Defects

### Risk 1: `has_pending_review` is episode-wide, not stage-scoped (LOW)

`has_pending_review(session, episode_id)` checks for ANY pending review for the episode, not scoped to a specific stage. This is fine for Sprint 3 (only Review Gate 1 exists), but when Review Gates 2 and 3 are added, a pending RG2 review would also filter the episode from `run_pending()`. **[ASSUMPTION]** This will need refinement in Sprint 4-5 but is not a bug today.

### Risk 2: No CSRF protection on review POST routes (LOW)

The existing Flask app does not appear to use CSRF tokens. The review POST routes follow the same pattern as existing routes (detect, download, etc.) which also lack CSRF. **[ASSUMPTION]** CSRF is a pre-existing condition, not introduced by Sprint 3. Acceptable for a single-user dashboard on a LAN-deployed Raspberry Pi.

### Risk 3: `_task_to_dict` in `list_reviews` does N+1 queries (LOW)

Each task in the review list triggers a separate `session.query(Episode)` lookup. Not a performance issue at current scale (<100 reviews) but could be optimized with a join later.

---

# 4) Test Review

## Coverage present

| Test file | Tests | Coverage |
|-----------|-------|----------|
| `tests/test_reviewer.py` | 20 | All 10 public functions + edge cases (double-action, empty notes, stale markers) |
| `tests/test_review_api.py` | 9 | All 6 API routes, including error cases (404, 400) |

**Total new tests**: 29
**All tests pass**: 338 passed, 0 failures

## Test quality assessment

- **Fixtures**: Well-designed `corrected_episode` and `review_task` fixtures create realistic test data with actual files on disk.
- **Isolation**: Uses in-memory SQLite via `db_session` fixture. Flask test client properly configured.
- **Edge cases covered**: Double-action prevention, empty notes validation, nonexistent review IDs, stale markers on filesystem.
- **Real file I/O**: Tests create actual transcript files and verify `.stale` marker creation — good integration testing.

## Missing or weak tests

1. **No test for `get_review_detail` with missing files** — what happens when `diff_path` points to a nonexistent file? The code handles it (returns `None`), but no test verifies this path.
2. **No test for pipeline `review_gate_1` stage directly** — the pipeline integration is tested implicitly through reviewer function tests, but there's no explicit test that `_run_stage(session, episode, settings, "review_gate_1")` returns the correct `StageResult`.
3. **No test for `run_pending` filtering** — no test verifies that episodes with pending reviews are excluded from `run_pending()`.
4. **No test for corrector feedback injection** — the plan mentioned tests in `test_corrector.py` for `test_reviewer_feedback_injection` and `test_stale_marker_triggers_rerun`, but these were not implemented. [ASSUMPTION] These may have been deferred or deemed covered by reviewer unit tests.

## Suggested additions (non-blocking)

```python
# In test_reviewer.py
def test_get_review_detail_missing_files(db_session, corrected_episode):
    """Handles missing diff/artifact files gracefully."""
    task = create_review_task(db_session, "ep001", "correct", ["/nonexistent/file.txt"])
    detail = get_review_detail(db_session, task.id)
    assert detail["corrected_text"] is None
```

---

# 5) Backward Compatibility Check

**PASS — v1 pipeline is completely unaffected.**

| Check | Result |
|-------|--------|
| `_V1_STAGES` unchanged | PASS — lines 45-51 identical to pre-Sprint 3 |
| `_get_stages()` returns v1 for `pipeline_version=1` | PASS |
| Existing CLI commands unmodified | PASS — `run`, `status`, `detect`, etc. unchanged |
| Existing Flask routes unmodified | PASS — all pre-existing routes in `api.py` untouched |
| Existing dashboard pages | PASS — only addition is Reviews button in topbar |
| No new migrations | PASS — `review_tasks` and `review_decisions` tables created in Sprint 1 |
| `_STATUS_ORDER` includes all v1 statuses | PASS — v1 statuses at positions 0-6 |
| `run_pending()` v1 filtering | PASS — v1 episodes (CHUNKED, GENERATED) still included in query; reviewer filter only applies if episode has active review (v1 episodes won't) |
| All 309 pre-existing tests pass | PASS — confirmed via `pytest` |

---

# 6) Required Fixes Before Commit

1. **Add corrector feedback injection test** — The sprint plan specified tests `test_reviewer_feedback_injection` and `test_stale_marker_triggers_rerun` in `test_corrector.py`. These are missing. Add at least one test confirming that when `get_latest_reviewer_feedback()` returns notes, the `{{ reviewer_feedback }}` placeholder in the prompt template is replaced with the feedback block. This validates the end-to-end feedback loop which is a core Sprint 3 deliverable.

2. **Add `review_gate_1` pipeline stage test** — Add a test that creates an episode at CORRECTED status, runs `_run_stage(session, episode, settings, "review_gate_1")`, and verifies:
   - First call returns `StageResult` with `status="review_pending"` and creates a ReviewTask.
   - After approving the review, a second call returns `status="success"`.

   This validates the most critical integration point of Sprint 3.

---

# 7) Nice-to-Have Improvements (optional)

1. **Scope `has_pending_review` by stage** — Add an optional `stage` parameter to `has_pending_review()` for future-proofing when RG2/RG3 are added. Not needed now but would be a trivial change.

2. **Add `review_gate_1` to `_STATUS_ORDER`** — Currently `review_gate_1` is a pseudo-stage (not an episode status) so it doesn't need an entry, but documenting this in a comment would help future developers understand the design.

3. **Optimize `_task_to_dict` N+1 queries** — In `list_reviews()`, consider joining Episode data in a single query rather than per-task lookups. Not impactful at current scale.

4. **Add `corrected` badge style** — `styles.css` has badges for most statuses but is missing `.badge-corrected`. Episodes in CORRECTED status will render without custom styling. Add: `.badge-corrected { background: #bc8cff33; color: var(--purple); }`.

5. **Mobile responsive review panel** — The review layout correctly collapses to single column at 768px breakpoint. The diff side-by-side also collapses. Good mobile support.

---

## Phase 1 Completion Assessment

After Sprint 3 (with the two required fixes applied), Phase 1 is fully operational:

- [x] A transcript can be corrected via `btcedu correct <ep_id>`
- [x] A correction can be reviewed in the dashboard diff viewer
- [x] Approving the correction allows the pipeline to continue past review_gate_1
- [x] Rejecting reverts the episode to TRANSCRIBED for re-correction
- [x] Requesting changes stores feedback, creates stale markers, and injects feedback into re-correction
- [x] The entire flow is idempotent, provenance-tracked, and prompt-versioned
- [x] The v1 pipeline is completely unaffected
- [x] 338 tests pass with 0 failures

**Phase 1 is complete. Sprint 4 (Translation) can begin after the two required fixes are applied.**
