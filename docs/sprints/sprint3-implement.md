# Sprint 3 — Implementation Prompt (Review System + Dashboard Integration)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 3 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1 + Sprint 2 completed codebase
> - **Expected output**: All code changes (new files, modified files), templates, CSS/JS, tests — committed and passing.

---

## Context

You are implementing **Sprint 3 (Phase 1, Part 2: Review System + Dashboard)** of the btcedu video production pipeline.

Sprint 1 (Foundation) and Sprint 2 (Transcript Correction) are complete:
- Models: `PromptVersion`, `ReviewTask`, `ReviewDecision` exist in DB
- `btcedu/core/corrector.py` generates corrected transcript + diff JSON + provenance
- `btcedu correct <episode_id>` CLI command works
- `PromptRegistry` loads and registers prompt versions
- v2 pipeline plan includes CORRECT stage after TRANSCRIBED

Sprint 3 adds the **human review system** — the core reviewer logic, Review Gate 1 integration into the pipeline, Flask API endpoints for review operations, and the dashboard UI (review queue + diff viewer + approve/reject).

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 3 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/web/` thoroughly (app.py or routes.py, templates/, static/), `btcedu/models/review.py`, `btcedu/core/pipeline.py`, `btcedu/core/corrector.py`, `btcedu/cli.py`.

2. **Implement reviewer module** — create `btcedu/core/reviewer.py` with:
   - `create_review_task(session, episode_id, stage, artifact_paths, diff_path=None, prompt_version_id=None)`:
     - Creates a `ReviewTask` with status=PENDING
     - Returns the created task
   - `approve_review(session, review_task_id, notes=None)`:
     - Sets ReviewTask status to APPROVED, reviewed_at to now
     - Creates a ReviewDecision record (decision="approved")
     - Computes and stores artifact_hash (SHA-256 of primary artifact)
     - Returns the decision
   - `reject_review(session, review_task_id, notes=None)`:
     - Sets ReviewTask status to REJECTED, reviewed_at to now
     - Creates a ReviewDecision record (decision="rejected")
     - Reverts episode status to the pre-stage status (CORRECTED → TRANSCRIBED)
     - Returns the decision
   - `request_changes(session, review_task_id, notes)`:
     - Sets ReviewTask status to CHANGES_REQUESTED, stores reviewer_notes
     - Creates a ReviewDecision record (decision="changes_requested")
     - Reverts episode status
     - Returns the decision
   - `get_pending_reviews(session)`:
     - Returns all ReviewTasks with status in (PENDING, IN_REVIEW), ordered by created_at
   - `get_review_detail(session, review_task_id)`:
     - Returns dict with: review task, associated episode, diff data (loaded from diff_path), decision history

3. **Integrate Review Gate 1 into pipeline** — modify `btcedu/core/pipeline.py` and/or `btcedu/core/corrector.py`:
   - After `correct_transcript()` succeeds, call `create_review_task()` with stage="correct"
   - The pipeline's `resolve_pipeline_plan()` checks: if episode status is CORRECTED, check for an APPROVED ReviewTask with stage="correct". If not found, pipeline pauses (does not advance).
   - On approval (via API or CLI), the next `run_episode_pipeline()` call will see the approved task and proceed.

4. **Integrate reviewer feedback into corrector** — modify `btcedu/core/corrector.py`:
   - When re-running correction after "request changes", look for the most recent ReviewDecision with notes for this episode+stage
   - Pass notes as `reviewer_feedback` variable to the prompt template
   - Add `{% if reviewer_feedback %}` block to `correct_transcript.md` template (see §5H format)

5. **Create Flask review routes** — new file `btcedu/web/review_routes.py` (or add to existing routes file):
   - `GET /reviews` — render review queue page
     - Query pending + recent (last 20) reviews
     - Pass to template
   - `GET /reviews/<int:review_id>` — render review detail page
     - Load review task, episode, diff data, decision history
     - For correction reviews: load and parse `correction_diff.json`
   - `POST /reviews/<int:review_id>/approve` — approve the review
     - Call `approve_review()`
     - Redirect to `/reviews` with flash message
   - `POST /reviews/<int:review_id>/reject` — reject the review
     - Call `reject_review()`
     - Redirect to `/reviews` with flash message
   - `POST /reviews/<int:review_id>/request-changes` — request changes
     - Read notes from form body
     - Call `request_changes()`
     - Redirect to `/reviews` with flash message
   - Register the blueprint in the Flask app

6. **Create review queue template** — `btcedu/web/templates/reviews.html` (or follow existing template naming):
   - Extend base template
   - Show table/list of reviews: status icon, episode ID, stage, created time, action link
   - Separate sections for pending and completed reviews
   - Follow existing dashboard styling patterns

7. **Create review detail template** — `btcedu/web/templates/review_detail.html`:
   - Show episode info and review metadata
   - **Diff viewer section**: render correction_diff.json as side-by-side comparison
     - Left column: original text segments
     - Right column: corrected text segments
     - Highlighted differences (green for additions, red for deletions, yellow for replacements)
   - Show diff summary statistics (total changes, by category)
   - **Action buttons**: Approve (green), Reject (red), Request Changes (yellow with textarea for notes)
   - Show decision history (previous approvals/rejections for this review)

8. **Add diff viewer CSS/JS** — add to `btcedu/web/static/`:
   - CSS for side-by-side diff layout and color-coded highlights
   - Minimal JS for: textarea toggle on "Request Changes", confirmation dialog on reject
   - Follow existing static file patterns

9. **Add navigation badge** — modify the base template to show pending review count:
   - Add a count of pending reviews to the nav bar (e.g., "Reviews (3)")
   - This can be injected via a template context processor or computed in each route

10. **Add CLI review commands** — modify `btcedu/cli.py`:
    - `btcedu review list` — show pending reviews in tabular format
    - `btcedu review approve <review_id>` — approve a review from CLI
    - `btcedu review reject <review_id> [--notes "reason"]` — reject with optional notes
    - Use Click group: `@cli.group()` for `review`, with subcommands

11. **Write tests**:
    - `tests/test_reviewer.py`: create_review_task, approve, reject, request_changes, get_pending, review detail
    - `tests/test_review_api.py`: Flask test client tests for each review endpoint
    - `tests/test_pipeline_review_gate.py`: test that pipeline pauses at review gate and resumes after approval
    - Update existing corrector tests if feedback injection changes the corrector interface

12. **Verify**:
    - Run `pytest tests/`
    - Start Flask dev server, navigate to `/reviews`
    - Manually test full flow: correct episode → review appears → view diff → approve → episode status advances
    - Test reject flow: reject → episode reverts to TRANSCRIBED
    - Test request-changes flow: notes saved → re-correction uses feedback
    - Verify `btcedu status` still works for v1 episodes

### Anti-scope-creep guardrails

- **Do NOT** implement adaptation review (Review Gate 2) — that's Sprint 4-5.
- **Do NOT** implement video review (Review Gate 3) — that's Sprint 9-10.
- **Do NOT** implement the TRANSLATE or ADAPT stages.
- **Do NOT** implement auto-approve rules — that's a later optimization.
- **Do NOT** implement email/webhook notifications for new reviews.
- **Do NOT** redesign or refactor the existing dashboard layout.
- **Do NOT** add a user/role/authentication system for reviewers.
- **Do NOT** add inline editing capability for corrections (that's a future enhancement per §5A).
- **Do NOT** change existing episode detail or episode list pages beyond minimal changes (e.g., linking to reviews).

### Code patterns to follow

- **Flask routes**: Follow existing patterns in `btcedu/web/`. Check if routes use Blueprints or are registered directly on the app.
- **Templates**: Follow existing Jinja2 template patterns — base template, blocks, includes.
- **Static files**: Follow existing organization for CSS/JS.
- **CLI groups**: If `btcedu/cli.py` already uses Click groups, follow that pattern. If not, create a `review` group.
- **Database sessions**: Follow existing session management patterns in routes (e.g., `with session_factory() as session`).

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred
- Manual verification steps:
  - Start Flask server
  - Navigate to `/reviews` (should be empty initially)
  - Run `btcedu correct <ep_id>` on a v2 episode
  - Navigate to `/reviews` — should show pending review
  - Click into review — should see diff viewer
  - Click Approve — review marked approved, episode status advances
  - Test reject and request-changes flows similarly
  - Run `btcedu review list` in CLI
  - Verify v1 pipeline is unaffected

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- The diff viewer does not need to be pixel-perfect — functional and readable is sufficient.
- Use server-side rendering (Jinja2 templates), not a separate frontend framework, unless the existing dashboard uses one.
- All review state changes must be atomic (single database transaction).
- CSRF protection: follow whatever pattern the existing Flask app uses.

---

## Definition of Done

- [ ] `btcedu/core/reviewer.py` exists with all specified functions
- [ ] Review Gate 1 integrated: pipeline pauses after CORRECT, creates ReviewTask
- [ ] Pipeline resumes on approval (next `run_episode_pipeline()` proceeds past review gate)
- [ ] Pipeline reverts episode on rejection (status → TRANSCRIBED)
- [ ] Request-changes stores notes and injects into re-correction prompt
- [ ] `GET /reviews` shows review queue with pending and recent reviews
- [ ] `GET /reviews/<id>` shows diff viewer for correction reviews
- [ ] `POST /reviews/<id>/approve` works and advances pipeline
- [ ] `POST /reviews/<id>/reject` works and reverts pipeline
- [ ] `POST /reviews/<id>/request-changes` works with notes
- [ ] Navigation shows pending review count
- [ ] `btcedu review list` CLI command works
- [ ] `btcedu review approve <id>` CLI command works
- [ ] `btcedu review reject <id>` CLI command works
- [ ] All tests pass
- [ ] v1 pipeline unaffected
- [ ] Full manual flow works: correct → review → approve → status advances

## Non-Goals

- Adaptation review UI (Review Gate 2) — Sprint 4-5
- Video review UI (Review Gate 3) — Sprint 9-10
- Auto-approve rules for trivial corrections
- Email/webhook notifications
- User authentication / role-based access for reviewers
- Inline editing of corrections
- Batch review (approve multiple at once)
- TRANSLATE or ADAPT stages
