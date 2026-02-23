# Sprint 3 — Implementation Plan: Review System + Dashboard Integration

**Sprint**: 3 (Phase 1, Part 2)
**Planning date**: 2026-02-23

---

## 1. Sprint Scope Summary

Sprint 3 adds the **human review system** to complete Phase 1. In scope: reviewer module (`btcedu/core/reviewer.py`), Review Gate 1 (after CORRECT, before future TRANSLATE), review queue API endpoints, review queue + diff viewer in the dashboard UI, approve/reject/request-changes workflow, reviewer feedback injection into re-correction, and CLI review commands. After Sprint 3, the pipeline can: correct → pause for human review → resume on approval (or re-correct on rejection with feedback).

**Not in scope**: Review Gates 2-3 (adaptation, render), multi-reviewer, inline editing, email notifications, review analytics, auto-approve rules, TRANSLATE/ADAPT stages.

---

## 2. File-Level Plan

### New Files

| File | Description |
|------|-------------|
| `btcedu/core/reviewer.py` | Core reviewer module: create/approve/reject/request-changes, queries, helpers |
| `tests/test_reviewer.py` | Unit tests for reviewer module (~16 tests) |
| `tests/test_review_api.py` | Flask route tests for review API (~9 tests) |

### Modified Files

| File | Changes |
|------|---------|
| `btcedu/core/pipeline.py` | Add `review_gate_1` to `_V2_STAGES`, add `_run_stage` branch, filter awaiting-review episodes from `run_pending()`/`run_latest()` |
| `btcedu/core/corrector.py` | Inject reviewer feedback into system prompt via `{{ reviewer_feedback }}` |
| `btcedu/prompts/templates/correct_transcript.md` | Add optional `{{ reviewer_feedback }}` block before `# Transkript` |
| `btcedu/web/api.py` | Add 6 review routes: list, count, detail, approve, reject, request-changes |
| `btcedu/web/templates/index.html` | Add Reviews nav button with badge, review panel HTML, diff viewer markup |
| `btcedu/web/static/app.js` | Add review queue view, diff viewer renderer, action handlers, badge polling (~250 lines) |
| `btcedu/web/static/styles.css` | Add review queue, diff viewer, badge styles (~100 lines) |
| `btcedu/cli.py` | Add `review` command group with `list`, `approve`, `reject` subcommands |

---

## 3. Reviewer Module Design (`btcedu/core/reviewer.py`)

### Public Functions

```python
def create_review_task(
    session: Session, episode_id: str, stage: str,
    artifact_paths: list[str], diff_path: str | None = None,
    prompt_version_id: int | None = None,
) -> ReviewTask
```
Creates a PENDING ReviewTask. Stores artifact_paths as JSON text. Computes artifact_hash (SHA-256 of file contents).

```python
def approve_review(session: Session, review_task_id: int, notes: str | None = None) -> ReviewDecision
```
Sets task status → APPROVED, creates ReviewDecision. Does NOT advance episode status — pipeline checks on next run.

```python
def reject_review(session: Session, review_task_id: int, notes: str | None = None) -> ReviewDecision
```
Sets task status → REJECTED, creates ReviewDecision, reverts episode CORRECTED → TRANSCRIBED.

```python
def request_changes(session: Session, review_task_id: int, notes: str) -> ReviewDecision
```
Sets task status → CHANGES_REQUESTED, creates ReviewDecision, stores notes on task.reviewer_notes, reverts episode to TRANSCRIBED, creates `.stale` marker on artifact files.

```python
def get_pending_reviews(session: Session) -> list[ReviewTask]
```
Returns PENDING + IN_REVIEW tasks, newest first.

```python
def get_review_detail(session: Session, review_task_id: int) -> dict
```
Returns dict with task info, episode title, diff_data (loaded from diff_path JSON), original_text (from episode.transcript_path), artifact content, and decision history.

```python
def get_latest_reviewer_feedback(session: Session, episode_id: str, stage: str) -> str | None
```
Returns reviewer_notes from the most recent CHANGES_REQUESTED task for episode+stage. Used by corrector for feedback injection.

```python
def has_approved_review(session: Session, episode_id: str, stage: str) -> bool
```
True if the latest ReviewTask for episode+stage is APPROVED.

```python
def has_pending_review(session: Session, episode_id: str) -> bool
```
True if any PENDING/IN_REVIEW task exists for this episode. Used to filter `run_pending()`.

```python
def pending_review_count(session: Session) -> int
```
Count of PENDING + IN_REVIEW tasks. Used for dashboard badge.

### Internal Helpers

- `_get_task_or_raise(session, task_id)` — query or ValueError
- `_validate_actionable(task)` — raise if status not in {PENDING, IN_REVIEW}
- `_revert_episode(session, episode_id)` — CORRECTED → TRANSCRIBED
- `_mark_output_stale(task)` — create `.stale` marker files (corrector already checks these via `_is_correction_current()`)
- `_compute_artifact_hash(paths)` — SHA-256 of file contents

---

## 4. Review Gate Integration

### Pipeline Changes (`pipeline.py`)

**`_V2_STAGES` update:**
```python
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),  # NEW
    # Future: ("translate", EpisodeStatus.CORRECTED),  # after review approved
]
```

**`_run_stage` new branch for `review_gate_1`:**
1. Check `has_approved_review()` → if True, return StageResult with status `"success"`
2. Check for existing PENDING/IN_REVIEW ReviewTask → if exists, return StageResult with status `"review_pending"` (detail="awaiting review")
3. Otherwise, call `create_review_task()` with corrected_path and diff_path, return StageResult with status `"review_pending"` (detail="review task created")

**New `"review_pending"` status handling in `run_episode_pipeline` loop:**

The existing loop (pipeline.py lines 266-300) only checks for `status == "failed"`. Add a new check:

```python
if result.status == "failed":
    # ... existing error handling ...
    break
elif result.status == "review_pending":
    logger.info("  Stage %s: %s", stage_name, result.detail)
    break  # Stop pipeline gracefully, no error
else:
    logger.info("  Stage %s: %s", stage_name, result.detail)
```

This cleanly pauses the pipeline without recording an error. The episode stays CORRECTED and the report shows success=True (no error recorded).

**`run_pending()` / `run_latest()` filtering:**

After querying episodes, filter out those with active review tasks to prevent wasteful re-processing:

```python
from btcedu.core.reviewer import has_pending_review
episodes = [ep for ep in episodes if not has_pending_review(session, ep.episode_id)]
```

### Flow Summary

1. **After correct succeeds:** review_gate_1 creates ReviewTask → pipeline pauses
2. **On approval:** Next run → review_gate_1 sees approved → returns "success" → pipeline continues (to future translate stage)
3. **On rejection:** Episode reverted to TRANSCRIBED → next run → correct re-runs → new ReviewTask created
4. **On request-changes:** Same as rejection + `.stale` marker + feedback stored → re-correction uses feedback

---

## 5. Flask Routes (`btcedu/web/api.py`)

All routes added to the existing `api_bp` blueprint, following the same `_get_session()` / try-finally pattern:

| Method | Route | Purpose | Response |
|--------|-------|---------|----------|
| GET | `/api/reviews` | List pending + recent tasks | `{pending_count, tasks: [{id, episode_id, episode_title, stage, status, created_at, reviewed_at}]}` |
| GET | `/api/reviews/count` | Pending count for badge | `{pending_count: int}` |
| GET | `/api/reviews/<id>` | Full detail + diff data | `{id, episode_id, episode_title, stage, status, diff, artifacts, original_text, decisions}` |
| POST | `/api/reviews/<id>/approve` | Approve review | `{success: true, decision_id, decision}` |
| POST | `/api/reviews/<id>/reject` | Reject review | `{success: true, decision_id, decision}` |
| POST | `/api/reviews/<id>/request-changes` | Request changes (requires `notes` in JSON body) | `{success: true, decision_id, decision}` |

`GET /api/reviews` accepts optional `?status=` query param. Default returns all pending tasks + last 20 resolved.

---

## 6. Dashboard UI Design

### Architecture

The dashboard is an SPA (single HTML page, vanilla JS, all data via `/api/*` JSON endpoints). The review system follows this exact pattern:

- **Reviews button** in topbar with badge showing pending count
- **Review panel** toggles visibility with the main episode panel (show one, hide other)
- **Review panel** uses 2-column grid (list left, detail right) matching existing `.main` layout
- **Diff viewer** renders inside review detail using correction_diff.json data

### index.html Additions

1. **Topbar**: Add Reviews button between "Cost" and "Refresh":
   ```html
   <button class="btn" onclick="showReviews()">
     Reviews <span class="review-badge" id="review-badge"></span>
   </button>
   ```

2. **Review panel** (hidden by default, placed after `.main` div):
   ```html
   <div class="review-panel" id="review-panel" style="display:none;">
     <div class="review-header">
       <h2>Review Queue</h2>
       <button class="btn btn-sm" onclick="hideReviews()">Back</button>
     </div>
     <div class="review-layout">
       <div class="review-list" id="review-list"></div>
       <div class="review-detail" id="review-detail"></div>
     </div>
   </div>
   ```

### app.js Additions (~250 lines)

**State:** `reviewView`, `reviewTasks`, `selectedReview`

**Badge polling:** `updateReviewBadge()` called in `refresh()` and on 30-second interval. Fetches `/api/reviews/count`.

**View toggle:** `showReviews()` hides `.main`, shows `#review-panel`, loads list. `hideReviews()` reverses.

**List rendering:** `loadReviewList()` fetches `/api/reviews`, renders each task as a clickable row with status badge, episode title, stage, time ago.

**Detail rendering:** `selectReview(id)` fetches `/api/reviews/<id>`, renders:
- Episode info header (title, stage, status, timestamps)
- Diff viewer component (summary + change list + side-by-side)
- Decision history timeline (past decisions as entries)
- Action buttons: Approve (green), Reject (red), Request Changes (yellow)
- Notes textarea for reject/request-changes

**Actions:** `approveReview()`, `rejectReview()`, `requestChanges()` — POST to API, show toast, refresh list + badge.

### styles.css Additions (~100 lines)

- `.review-badge` — small pill counter on the Reviews button (background: `var(--red)`, white text)
- `.review-panel` — full-width panel replacing `.main`
- `.review-layout` — `display: grid; grid-template-columns: 1fr 1fr;`
- `.review-list` — scrollable panel, reuses `.panel` patterns
- `.review-item` — clickable row with hover/selected states
- `.diff-summary` — flex row with change count stats
- `.diff-change` — card with colored left border
- `.diff-change.replace` / `.insert` / `.delete` — left border: `var(--yellow)` / `var(--green)` / `var(--red)`
- `.diff-original` — `text-decoration: line-through; color: var(--red);`
- `.diff-corrected` — `color: var(--green); font-weight: 500;`
- `.diff-context` — `font-size: 12px; color: var(--text-dim);`
- `.diff-sidebyside` — 2-column grid for original vs corrected full text
- `.review-actions` — button row with gap spacing
- `.review-notes-textarea` — textarea input matching existing form styles

---

## 7. Diff Viewer Component

Renders `correction_diff.json` (from Sprint 2's `compute_correction_diff()`) as:

1. **Summary bar:** Total changes with breakdown by type (replace/insert/delete)
2. **Change list:** Scrollable cards, each with:
   - Color-coded type badge (replace=yellow, insert=green, delete=red)
   - Original text (strikethrough) → corrected text (highlighted)
   - Context line showing surrounding words
3. **Side-by-side panels:** Original transcript (left) and corrected transcript (right) in monospace `<pre>` blocks, for full-text comparison

The JS function `renderDiffViewer(diff, originalText, correctedText)` builds the HTML string. Uses existing `esc()` helper for HTML escaping.

---

## 8. Reviewer Feedback Injection

### Prompt Template Change

Add before `# Transkript` in `correct_transcript.md`:

```markdown
{{ reviewer_feedback }}

# Transkript
```

[ASSUMPTION] The template uses simple `{{ }}` string replacement (not a Jinja2 engine). The feedback variable is replaced in corrector.py — if no feedback exists, it's replaced with an empty string.

### Corrector Change

In `correct_transcript()`, after loading the template and before processing segments:

1. Import and call `get_latest_reviewer_feedback(session, episode_id, "correct")`
2. If feedback exists, build a feedback block:
   ```
   ## Reviewer-Korrekturen (bitte diese Anmerkungen bei der Korrektur berücksichtigen)\n\n{feedback}
   ```
3. Replace `{{ reviewer_feedback }}` in the template body with the feedback block (or empty string if no feedback)
4. This happens **before** `_split_prompt()`, so the feedback ends up in the system prompt section

The `.stale` marker mechanism already works: when `request_changes()` creates the marker, the corrector's `_is_correction_current()` returns False, triggering re-correction with feedback injected.

---

## 9. CLI Review Commands

Added as a Click command group `btcedu review`:

```python
@cli.group()
@click.pass_context
def review(ctx):
    """Review system commands."""
    pass

@review.command(name="list")
@click.option("--status", default=None,
    help="Filter by status (pending, approved, rejected, changes_requested).")
@click.pass_context
def review_list(ctx, status):
    """List review tasks."""

@review.command()
@click.argument("review_id", type=int)
@click.option("--notes", default=None, help="Optional approval notes.")
@click.pass_context
def approve(ctx, review_id, notes):
    """Approve a review task."""

@review.command()
@click.argument("review_id", type=int)
@click.option("--notes", default=None, help="Optional rejection notes.")
@click.pass_context
def reject(ctx, review_id, notes):
    """Reject a review task (reverts episode to TRANSCRIBED)."""
```

Output follows existing CLI pattern: `[OK]`/`[FAIL]` prefix, tabular list output with columns: ID, Episode, Stage, Status, Created.

---

## 10. Test Plan

### tests/test_reviewer.py (16 tests)

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_create_review_task` | Task created with correct fields, status PENDING |
| 2 | `test_create_review_task_artifact_hash` | Hash computed from file contents |
| 3 | `test_approve_review` | Status→APPROVED, reviewed_at set, decision created |
| 4 | `test_approve_review_does_not_advance_episode` | Episode stays CORRECTED |
| 5 | `test_reject_review_reverts_episode` | Episode status→TRANSCRIBED |
| 6 | `test_reject_review_creates_decision` | ReviewDecision with decision="rejected" |
| 7 | `test_request_changes_requires_notes` | ValueError if notes empty |
| 8 | `test_request_changes_marks_stale` | `.stale` marker file created |
| 9 | `test_request_changes_stores_feedback` | reviewer_notes stored on task |
| 10 | `test_cannot_act_on_decided_task` | ValueError if task already APPROVED/REJECTED |
| 11 | `test_get_pending_reviews` | Returns PENDING/IN_REVIEW only, newest first |
| 12 | `test_get_review_detail` | Dict with diff data, decisions, original_text |
| 13 | `test_has_approved_review` | True/False based on latest task status |
| 14 | `test_get_latest_reviewer_feedback` | Returns notes from CHANGES_REQUESTED; None otherwise |
| 15 | `test_pending_review_count` | Counts correctly |
| 16 | `test_has_pending_review` | True when PENDING task exists |

### tests/test_review_api.py (9 tests)

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_list_reviews_empty` | Returns empty tasks, count 0 |
| 2 | `test_list_reviews_with_pending` | Returns pending tasks with episode titles |
| 3 | `test_review_count_endpoint` | Correct pending_count |
| 4 | `test_get_review_detail_404` | 404 for nonexistent review |
| 5 | `test_get_review_detail_with_diff` | Diff data loaded and returned |
| 6 | `test_approve_via_api` | POST approve returns success |
| 7 | `test_reject_via_api` | POST reject returns success, episode reverted |
| 8 | `test_request_changes_via_api` | POST with notes succeeds |
| 9 | `test_request_changes_missing_notes` | Returns 400 error |

### Additional tests in existing files

| File | Test | Asserts |
|------|------|---------|
| `test_corrector.py` | `test_reviewer_feedback_injection` | Feedback appears in system prompt |
| `test_corrector.py` | `test_stale_marker_triggers_rerun` | `.stale` causes re-correction |

### Fixtures needed

- `corrected_episode(db_session, tmp_path)` — Episode at CORRECTED with transcript + corrected files
- `review_task(db_session, corrected_episode)` — PENDING ReviewTask for the episode
- Flask test client fixture using `create_app(test_settings)`

---

## 11. Implementation Order

1. **Core reviewer module** — create `btcedu/core/reviewer.py` + `tests/test_reviewer.py`
2. **Pipeline review gate** — modify `pipeline.py` (add review_gate_1 stage, review_pending handling, run_pending filtering)
3. **Prompt template + corrector** — modify `correct_transcript.md` + `corrector.py` for feedback injection + tests
4. **Flask API routes** — add review routes to `api.py` + `tests/test_review_api.py`
5. **CLI commands** — add `review` group to `cli.py`
6. **Dashboard UI** — modify `index.html`, `app.js`, `styles.css`
7. **Full test suite** — run all tests, verify 0 failures
8. **Sprint output doc** — write `docs/sprints/outputs/sprint3-implement-output.md`

---

## 12. Definition of Done

- [ ] `btcedu/core/reviewer.py` exists with all public functions
- [ ] Review Gate 1 creates ReviewTask after correct stage
- [ ] Pipeline pauses at review_gate_1 (episode stays CORRECTED)
- [ ] Approve unblocks pipeline on next run
- [ ] Reject reverts episode to TRANSCRIBED
- [ ] Request-changes stores feedback + creates `.stale` marker + reverts episode
- [ ] Re-correction injects reviewer feedback into prompt
- [ ] 6 Flask API review routes work
- [ ] Dashboard shows Reviews button with pending count badge
- [ ] Dashboard review queue lists pending + recent tasks
- [ ] Dashboard diff viewer shows color-coded changes
- [ ] Dashboard approve/reject/request-changes buttons work
- [ ] CLI `btcedu review list|approve|reject` work
- [ ] All new tests pass (~27 new tests)
- [ ] Full test suite passes (309+ existing + new)
- [ ] v1 pipeline completely unaffected
- [ ] No new migrations needed (ReviewTask/ReviewDecision tables created in Sprint 1)

---

## 13. Non-Goals

- Review Gates 2-3 (after ADAPT, after RENDER)
- Multi-reviewer / concurrent review support
- Authentication / role-based review access
- Inline transcript editing in diff viewer
- Email/webhook notifications for pending reviews
- Review analytics (turnaround time, approval rates)
- Auto-approve rules for trivial corrections
- Stale review detection (invalidating pending reviews on upstream change)
- TRANSLATE or ADAPT stages

---

## Assumptions

- `[ASSUMPTION]` No new database migration needed — `review_tasks` and `review_decisions` tables were created in Sprint 1 migration v4
- `[ASSUMPTION]` The prompt template uses simple `{{ }}` string replacement, not a Jinja2 engine — conditional feedback block handled in Python
- `[ASSUMPTION]` Review routes are added to the existing `api_bp` blueprint (not a separate blueprint) — matches the single-file API pattern
- `[ASSUMPTION]` Dashboard review UI is integrated into the existing SPA (no separate HTML pages) — matches the index.html + vanilla JS architecture
- `[ASSUMPTION]` `review_gate_1` returning `"review_pending"` status is a new StageResult status that causes a clean pipeline break without error
- `[ASSUMPTION]` `_revert_episode()` only handles CORRECTED → TRANSCRIBED reversion (scope limited to Review Gate 1)
- `[ASSUMPTION]` Reviewer feedback is injected into the system prompt section (before the `# Transkript` split marker)
