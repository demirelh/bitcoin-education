# Sprint 3 — Implementation Output: Review System + Dashboard Integration

**Sprint**: 3 (Phase 1, Part 2)
**Implementation date**: 2026-02-23
**Test results**: 338 passed (309 existing + 29 new), 0 failures

---

## 1. Implementation Plan Summary

**Scope**: Human review system — reviewer module, Review Gate 1, Flask API endpoints, dashboard review queue + diff viewer, CLI commands, reviewer feedback injection into corrector.

**Files created**:
- `btcedu/core/reviewer.py` — Core reviewer module (10 public functions + 5 internal helpers)
- `tests/test_reviewer.py` — 20 unit tests for reviewer module
- `tests/test_review_api.py` — 9 Flask API tests

**Files modified**:
- `btcedu/core/pipeline.py` — Review Gate 1 stage, review_pending handling, run_pending/run_latest filtering
- `btcedu/core/corrector.py` — Reviewer feedback injection before `_split_prompt()`
- `btcedu/prompts/templates/correct_transcript.md` — Added `{{ reviewer_feedback }}` placeholder
- `btcedu/web/api.py` — 6 review API routes
- `btcedu/web/templates/index.html` — Reviews button with badge, review panel HTML
- `btcedu/web/static/app.js` — Review queue view, diff viewer, action handlers (~200 lines)
- `btcedu/web/static/styles.css` — Review panel, diff viewer, badge styles (~200 lines)
- `btcedu/cli.py` — `review` command group with `list`, `approve`, `reject` subcommands

**Assumptions**:
- `[ASSUMPTION]` No new migration needed — `review_tasks` and `review_decisions` tables created in Sprint 1
- `[ASSUMPTION]` Prompt template uses `{{ }}` string replacement, not Jinja2 — feedback handled in Python
- `[ASSUMPTION]` Review routes added to existing `api_bp` blueprint
- `[ASSUMPTION]` Dashboard review UI is SPA panel (no separate HTML pages)
- `[ASSUMPTION]` `review_gate_1` returning `"review_pending"` causes clean pipeline break without error
- `[ASSUMPTION]` `_revert_episode()` only handles CORRECTED → TRANSCRIBED

---

## 2. Code Changes

### 2.1 `btcedu/core/reviewer.py` (NEW)

Public functions:
- `create_review_task(session, episode_id, stage, artifact_paths, diff_path, prompt_version_id)` → ReviewTask
- `approve_review(session, review_task_id, notes)` → ReviewDecision
- `reject_review(session, review_task_id, notes)` → ReviewDecision (reverts episode)
- `request_changes(session, review_task_id, notes)` → ReviewDecision (reverts + .stale markers)
- `get_pending_reviews(session)` → list[ReviewTask]
- `get_review_detail(session, review_task_id)` → dict
- `get_latest_reviewer_feedback(session, episode_id, stage)` → str | None
- `has_approved_review(session, episode_id, stage)` → bool
- `has_pending_review(session, episode_id)` → bool
- `pending_review_count(session)` → int

Internal helpers: `_get_task_or_raise`, `_validate_actionable`, `_revert_episode`, `_mark_output_stale`, `_compute_artifact_hash`

### 2.2 `btcedu/core/pipeline.py` (MODIFIED)

- Added `("review_gate_1", EpisodeStatus.CORRECTED)` to `_V2_STAGES`
- Added `review_gate_1` branch in `_run_stage()`:
  - Checks `has_approved_review()` → returns "success"
  - Checks `has_pending_review()` → returns "review_pending"
  - Otherwise creates review task → returns "review_pending"
- Added `review_pending` handling in `run_episode_pipeline()` loop — clean break, no error
- Added filtering in `run_pending()` — excludes episodes with active review tasks
- Added filtering in `run_latest()` — skips candidates with active review tasks

### 2.3 `btcedu/core/corrector.py` (MODIFIED)

- Before `_split_prompt()`, calls `get_latest_reviewer_feedback()` for the episode
- If feedback exists, builds German-language feedback block and replaces `{{ reviewer_feedback }}`
- If no feedback, replaces placeholder with empty string

### 2.4 `btcedu/prompts/templates/correct_transcript.md` (MODIFIED)

- Added `{{ reviewer_feedback }}` placeholder between "WAS NICHT ZU KORRIGIEREN IST" section and "# Transkript" header

### 2.5 `btcedu/web/api.py` (MODIFIED)

6 new routes:
- `GET /api/reviews` — List pending + recent (20) resolved tasks
- `GET /api/reviews/count` — Pending count for badge polling
- `GET /api/reviews/<id>` — Full detail with diff data, decisions
- `POST /api/reviews/<id>/approve` — Approve review
- `POST /api/reviews/<id>/reject` — Reject review
- `POST /api/reviews/<id>/request-changes` — Request changes (requires `notes`)

### 2.6 Dashboard UI (MODIFIED)

**index.html**: Reviews button with badge in topbar, review panel (hidden by default) with 2-column layout (list + detail).

**app.js**: Review queue state management, badge polling via `updateReviewBadge()` (called on refresh), review list/detail rendering, diff viewer component, approve/reject/request-changes action handlers, timeAgo helper.

**styles.css**: Review badge (red pill), review panel layout, review item cards with status colors, diff viewer (summary bar, change cards with color-coded borders, side-by-side panels), action buttons (warning/approve/reject), notes textarea, decision history.

### 2.7 `btcedu/cli.py` (MODIFIED)

New Click command group `review` with subcommands:
- `btcedu review list [--status STATUS]` — Tabular list of review tasks
- `btcedu review approve REVIEW_ID [--notes TEXT]` — Approve a review
- `btcedu review reject REVIEW_ID [--notes TEXT]` — Reject a review

---

## 3. Migration Changes

**None required.** The `review_tasks` and `review_decisions` tables were created in Sprint 1 (migration `004_create_review_tables`). All columns used by Sprint 3 already exist.

---

## 4. Tests

### `tests/test_reviewer.py` — 20 tests
| Test | Asserts |
|------|---------|
| `test_creates_pending_task` | Task created with correct fields, status PENDING |
| `test_computes_artifact_hash` | SHA-256 hash computed from file contents |
| `test_sets_approved_status` | Status→APPROVED, reviewed_at set, decision created |
| `test_does_not_advance_episode` | Episode stays CORRECTED after approval |
| `test_reverts_episode` | Episode status→TRANSCRIBED on rejection |
| `test_creates_decision` | ReviewDecision with decision="rejected" |
| `test_requires_notes` | ValueError if notes empty |
| `test_marks_stale` | `.stale` marker file created |
| `test_stores_feedback` | reviewer_notes stored on task |
| `test_cannot_approve_approved` | ValueError if task already decided |
| `test_cannot_reject_rejected` | ValueError if task already decided |
| `test_returns_pending_only` | Only PENDING/IN_REVIEW returned |
| `test_newest_first` | Ordered by created_at DESC |
| `test_returns_diff_data` | Dict with diff, original_text, corrected_text |
| `test_true_when_approved` / `test_false_when_pending` | has_approved_review |
| `test_returns_notes` / `test_returns_none_when_no_feedback` | get_latest_reviewer_feedback |
| `test_counts_correctly` | pending_review_count |
| `test_true_when_pending` | has_pending_review |

### `tests/test_review_api.py` — 9 tests
| Test | Asserts |
|------|---------|
| `test_empty_list` | Returns empty tasks, count 0 |
| `test_with_pending` | Returns pending tasks with episode titles |
| `test_count_endpoint` | Correct pending_count |
| `test_404_for_nonexistent` | 404 for nonexistent review |
| `test_with_diff` | Diff data loaded and returned |
| `test_approve` | POST approve returns success |
| `test_reject` | POST reject returns success |
| `test_with_notes` | POST request-changes with notes succeeds |
| `test_missing_notes` | Returns 400 error for missing notes |

---

## 5. Manual Verification Steps

1. Start Flask server: `btcedu web --host 0.0.0.0`
2. Navigate to dashboard — "Reviews" button should appear in topbar (badge hidden if 0 pending)
3. Run `btcedu correct --episode-id <ep_id>` on a v2 episode
4. Run the pipeline: `btcedu run --episode-id <ep_id>` — should stop at review_gate_1
5. Navigate to dashboard → click "Reviews" → should show pending review
6. Click into review → should see diff viewer with changes
7. Click "Approve" → review marked approved, toast shown
8. Re-run pipeline: `btcedu run --episode-id <ep_id>` → should pass review_gate_1
9. Test reject: create another review, click "Reject" → episode reverts to TRANSCRIBED
10. Test request-changes: create review, click "Request Changes", enter notes → episode reverts, .stale marker created
11. Re-run correction → corrector should inject reviewer feedback into prompt
12. CLI: `btcedu review list` — should show pending reviews
13. CLI: `btcedu review approve <id>` — should approve
14. Verify `btcedu status` still works for v1 episodes

---

## 6. Intentionally Deferred

- **Review Gates 2-3** (after ADAPT, after RENDER) — Sprint 4-5 / Sprint 9-10
- **Multi-reviewer / concurrent review support** — not in scope
- **Authentication / role-based access** — not in scope
- **Auto-approve rules** for trivial corrections — later optimization
- **Email/webhook notifications** for pending reviews — later enhancement
- **Inline transcript editing** in diff viewer — future enhancement per MASTERPLAN §5A
- **Batch review** (approve multiple at once) — not in scope
- **TRANSLATE / ADAPT stages** — Sprint 4-5
- **Review analytics** (turnaround time, approval rates) — not in scope

---

## 7. Rollback / Safe Revert Notes

All changes are additive and backward-compatible:
- **No migrations**: No database schema changes to roll back
- **v1 pipeline**: Completely unaffected — review gate only exists in `_V2_STAGES`
- **New files**: `btcedu/core/reviewer.py`, `tests/test_reviewer.py`, `tests/test_review_api.py` — can be deleted
- **Modified files**: All modifications are isolated additions (new branches, new routes, new CSS/JS blocks) — can be reverted with `git checkout` on individual files
- **Dashboard**: Review panel is hidden by default (`display:none`), no visual change unless user clicks "Reviews"
- **CLI**: `review` group is additive — doesn't affect existing commands

To fully revert: `git revert <commit>` or cherry-pick revert the specific files.
