# Sprint 3 — Validation Prompt (Review System + Dashboard Integration)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 3 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1-2 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 3 (Phase 1, Part 2: Review System + Dashboard)** implementation of the btcedu video production pipeline.

Sprint 3 was scoped to:
- Implement `btcedu/core/reviewer.py` (create, approve, reject, request-changes)
- Add Review Gate 1 to pipeline after CORRECT stage
- Create Flask review routes (GET /reviews, GET /reviews/<id>, POST approve/reject/request-changes)
- Build review queue UI (list template)
- Build diff viewer component for correction review (detail template)
- Add approve/reject/request-changes buttons
- Add navigation badge for pending review count
- Add CLI commands: `btcedu review list/approve/reject`
- Implement reviewer feedback injection into re-correction prompt
- Write tests

Sprint 3 was NOT scoped to include: adaptation review (RG2), video review (RG3), auto-approve, notifications, user auth, inline editing, TRANSLATE/ADAPT stages.

Sprint 3 completes Phase 1 of the master plan. After this sprint, the full cycle of correct → review → approve/reject → resume should work end-to-end.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Reviewer Module

- [ ] **1.1** `btcedu/core/reviewer.py` exists
- [ ] **1.2** `create_review_task()` creates a ReviewTask with status=PENDING and correct fields
- [ ] **1.3** `approve_review()` sets status to APPROVED, creates ReviewDecision, computes artifact_hash
- [ ] **1.4** `reject_review()` sets status to REJECTED, creates ReviewDecision, reverts episode status to TRANSCRIBED
- [ ] **1.5** `request_changes()` sets status to CHANGES_REQUESTED, stores notes, creates ReviewDecision, reverts episode status
- [ ] **1.6** `get_pending_reviews()` returns pending/in_review tasks ordered by created_at
- [ ] **1.7** `get_review_detail()` returns task + episode + diff data + decision history
- [ ] **1.8** All state changes are atomic (single transaction per operation)
- [ ] **1.9** Functions handle edge cases: nonexistent review_id, already-approved task, etc.

### 2. Review Gate Integration

- [ ] **2.1** After `correct_transcript()` succeeds, a ReviewTask is created with stage="correct"
- [ ] **2.2** Pipeline pauses at CORRECTED status when no approved ReviewTask exists
- [ ] **2.3** Pipeline advances past CORRECTED when an approved ReviewTask exists for stage="correct"
- [ ] **2.4** On rejection: episode status reverts to TRANSCRIBED
- [ ] **2.5** On request-changes: episode status reverts to TRANSCRIBED AND reviewer notes are stored
- [ ] **2.6** Re-running correction after request-changes injects reviewer feedback into prompt
- [ ] **2.7** Review gate does NOT apply to v1 pipeline episodes
- [ ] **2.8** Review gate uses the ReviewTask/ReviewDecision models created in Sprint 1

### 3. Reviewer Feedback Injection

- [ ] **3.1** `correct_transcript.md` template has `{% if reviewer_feedback %}` block
- [ ] **3.2** Feedback block uses clear delimiters and instructions per §5H
- [ ] **3.3** Corrector reads most recent "changes_requested" notes for the episode+stage
- [ ] **3.4** Feedback is passed as template variable, not hardcoded
- [ ] **3.5** If no feedback exists, the block is omitted (not rendered)

### 4. Flask Review Routes

- [ ] **4.1** `GET /reviews` route exists and returns review queue page
- [ ] **4.2** `GET /reviews/<id>` route exists and returns review detail page with diff
- [ ] **4.3** `POST /reviews/<id>/approve` route exists and processes approval
- [ ] **4.4** `POST /reviews/<id>/reject` route exists and processes rejection
- [ ] **4.5** `POST /reviews/<id>/request-changes` route exists with notes parameter
- [ ] **4.6** Routes are registered with Flask app (blueprint or direct)
- [ ] **4.7** Routes follow existing Flask patterns in the codebase
- [ ] **4.8** Routes handle errors gracefully (nonexistent review, already processed, etc.)
- [ ] **4.9** POST routes redirect appropriately after action (PRG pattern)
- [ ] **4.10** CSRF protection follows existing app patterns

### 5. Review Queue UI

- [ ] **5.1** Review queue template exists and extends base template
- [ ] **5.2** Shows pending reviews with: status indicator, episode ID, stage name, creation time
- [ ] **5.3** Shows recent completed reviews (last N)
- [ ] **5.4** Each review links to its detail page
- [ ] **5.5** Empty state handled (message when no pending reviews)
- [ ] **5.6** Follows existing dashboard styling patterns

### 6. Diff Viewer UI

- [ ] **6.1** Review detail template exists for correction reviews
- [ ] **6.2** Shows episode info and review metadata
- [ ] **6.3** Renders correction_diff.json as a readable diff view
- [ ] **6.4** Diff highlights changes (color-coded: additions green, deletions red, replacements yellow — or similar)
- [ ] **6.5** Shows diff summary statistics (total changes, by category/type)
- [ ] **6.6** Approve button (green) is prominent and functional
- [ ] **6.7** Reject button (red) is functional
- [ ] **6.8** Request-changes button with text area for notes is functional
- [ ] **6.9** Shows decision history (previous approvals/rejections)
- [ ] **6.10** Diff viewer handles edge cases: no changes (empty diff), very large diff

### 7. Navigation Badge

- [ ] **7.1** Base template or nav bar shows pending review count
- [ ] **7.2** Count updates when reviews are approved/rejected (on page refresh)
- [ ] **7.3** Count is 0 or hidden when no pending reviews
- [ ] **7.4** Implementation does not break existing navigation

### 8. CLI Review Commands

- [ ] **8.1** `btcedu review list` command exists and shows pending reviews
- [ ] **8.2** `btcedu review approve <review_id>` command works
- [ ] **8.3** `btcedu review reject <review_id>` command works with optional `--notes`
- [ ] **8.4** `btcedu review --help` shows useful help text
- [ ] **8.5** CLI commands follow existing patterns in `btcedu/cli.py`
- [ ] **8.6** CLI commands produce appropriate output messages

### 9. V1 Pipeline Compatibility (Regression)

- [ ] **9.1** `btcedu status` still works for existing episodes
- [ ] **9.2** v1 pipeline stages are completely unmodified
- [ ] **9.3** Existing dashboard pages (episode list, episode detail, etc.) still work
- [ ] **9.4** No existing tests are broken
- [ ] **9.5** No existing CLI commands are broken
- [ ] **9.6** No existing templates are broken or significantly altered (only nav badge added)
- [ ] **9.7** No existing routes are broken

### 10. Test Coverage

- [ ] **10.1** Tests for `create_review_task()` — correct fields, PENDING status
- [ ] **10.2** Tests for `approve_review()` — status change, ReviewDecision created, artifact_hash computed
- [ ] **10.3** Tests for `reject_review()` — status change, episode reverted, ReviewDecision created
- [ ] **10.4** Tests for `request_changes()` — notes stored, episode reverted
- [ ] **10.5** Tests for `get_pending_reviews()` — correct filtering and ordering
- [ ] **10.6** Tests for review gate in pipeline — pauses at review, resumes after approval
- [ ] **10.7** Tests for Flask review endpoints (using test client)
- [ ] **10.8** Tests for reviewer feedback injection into corrector
- [ ] **10.9** All tests pass with `pytest tests/`
- [ ] **10.10** Tests use mocked data / test fixtures (not real API calls)

### 11. Scope Creep Detection

- [ ] **11.1** No adaptation review (Review Gate 2) was implemented
- [ ] **11.2** No video review (Review Gate 3) was implemented
- [ ] **11.3** No auto-approve rules were implemented
- [ ] **11.4** No notification system (email/webhook) was implemented
- [ ] **11.5** No user authentication or role system was added
- [ ] **11.6** No inline editing of corrections was implemented
- [ ] **11.7** No TRANSLATE or ADAPT stages were implemented
- [ ] **11.8** No batch review capability was added
- [ ] **11.9** Existing dashboard pages were not refactored or redesigned
- [ ] **11.10** No unnecessary dependencies were added

### 12. End-to-End Flow Validation

- [ ] **12.1** Full flow works: `btcedu correct <ep_id>` → ReviewTask created → visible in `/reviews`
- [ ] **12.2** Click review → see diff viewer with highlighted changes
- [ ] **12.3** Click Approve → ReviewTask APPROVED → episode status advances
- [ ] **12.4** Click Reject → ReviewTask REJECTED → episode status reverts to TRANSCRIBED
- [ ] **12.5** Request Changes with notes → ReviewTask CHANGES_REQUESTED → re-correction uses feedback
- [ ] **12.6** Pipeline run on approved episode proceeds past review gate
- [ ] **12.7** Pipeline run on non-approved episode stops at review gate (does not error, just pauses)
- [ ] **12.8** CLI `btcedu review list` shows pending review
- [ ] **12.9** CLI `btcedu review approve <id>` works same as dashboard

### 13. Security / Safety

- [ ] **13.1** No XSS vulnerabilities in diff viewer (user-supplied transcript text is escaped)
- [ ] **13.2** No SQL injection in review routes (using parameterized queries / ORM)
- [ ] **13.3** POST routes validate input (review_id exists, notes not excessively long)
- [ ] **13.4** No CSRF vulnerabilities (follows existing app CSRF pattern)
- [ ] **13.5** Reviewer feedback injection does not allow prompt injection (feedback is clearly delimited in prompt template)

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Phase 1 is complete. Ready for Phase 2 (Sprint 4: Translation + Adaptation). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 4 after fixes. |
| **FAIL** | Critical issues found. Sprint 3 must be reworked before proceeding. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Phase 1 Completion Assessment:

After Sprint 3, Phase 1 (Transcript Correction + Review System) should be fully operational:
- [ ] A transcript can be corrected via `btcedu correct <ep_id>`
- [ ] A correction can be reviewed in the dashboard diff viewer
- [ ] Approving the correction advances the episode in the pipeline
- [ ] Rejecting or requesting changes reverts and allows re-correction
- [ ] The entire flow is idempotent, provenance-tracked, and prompt-versioned
- [ ] The v1 pipeline is completely unaffected

If all of the above are true, Phase 1 is complete and Sprint 4 (Translation) can begin.

### Deferred Items Acknowledged:

- Adaptation review UI, Review Gate 2 (Sprint 4-5)
- Video review UI, Review Gate 3 (Sprint 9-10)
- Auto-approve rules for trivial corrections (later optimization)
- Email/webhook notifications (optional, later)
- User authentication / multi-reviewer support (later if needed)
- Inline editing of corrections (future enhancement)
- Batch review capability (future enhancement)
- TRANSLATE stage (Sprint 4)
- ADAPT stage (Sprint 4-5)

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- Pay special attention to the end-to-end flow (§12) — this is the most important validation for Sprint 3.
- Check that reviewer feedback injection does not create prompt injection vulnerabilities.
- Verify that the diff viewer properly escapes user-supplied text to prevent XSS.
