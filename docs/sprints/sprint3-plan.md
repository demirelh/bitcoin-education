# Sprint 3 — Planning Prompt (Review System + Dashboard Integration)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1 + Sprint 2 completed codebase (especially `btcedu/core/corrector.py`, `btcedu/models/review.py`, `btcedu/web/`, `btcedu/core/pipeline.py`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the reviewer module, review gate pipeline integration, Flask review routes, dashboard review queue UI, diff viewer component, approve/reject flow, CLI review commands, and tests.

---

## Context

You are planning **Sprint 3 (Phase 1, Part 2: Review System + Dashboard)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprint 1 (Foundation) and Sprint 2 (Transcript Correction Stage) are complete:
- `EpisodeStatus` enum has new v2 values including `CORRECTED`
- `PromptVersion`, `ReviewTask`, `ReviewDecision` models and tables exist
- `PromptRegistry` works
- `btcedu/core/corrector.py` with `correct_transcript()` exists
- `btcedu correct <episode_id>` CLI command works
- `correction_diff.json` and provenance are generated
- The CORRECT stage is integrated into the v2 pipeline

Sprint 3 completes Phase 1 by adding the **human review system** — the reviewer module, Review Gate 1 (after CORRECT), the review queue dashboard UI, the diff viewer for transcript corrections, and the approve/reject workflow. After Sprint 3, the pipeline can: correct a transcript → pause for review → resume on approval.

### Sprint 3 Focus (from MASTERPLAN.md §4 Phase 1 and §12 Sprint 3)

1. Implement `btcedu/core/reviewer.py` with functions: `create_review_task()`, `approve_review()`, `reject_review()`, `request_changes()`, `get_pending_reviews()`, `get_review_detail()`.
2. Add Review Gate 1 to the pipeline — after CORRECT, create a `ReviewTask` (status=PENDING) and pause the pipeline.
3. On approval, the pipeline advances episode status. On rejection, episode goes back to TRANSCRIBED for re-correction.
4. Create Flask review routes in `btcedu/web/review_routes.py` (or extend existing routes):
   - `GET /reviews` — review queue list (pending + recent history)
   - `GET /reviews/<id>` — review detail with diff viewer
   - `POST /reviews/<id>/approve` — approve a review
   - `POST /reviews/<id>/reject` — reject a review
   - `POST /reviews/<id>/request-changes` — request changes with notes
5. Build the review queue UI template — list of pending reviews with episode info, stage, creation time.
6. Build the diff viewer component for correction review — side-by-side view of original vs corrected transcript with highlighted changes.
7. Add approve/reject/request-changes buttons with appropriate visual treatment.
8. Add a badge in the navigation showing pending review count.
9. Add CLI commands: `btcedu review list`, `btcedu review approve <review_id>`, `btcedu review reject <review_id>`.
10. Implement reviewer feedback injection — when "request changes" is used, store notes and inject them into re-correction prompt via `{{ reviewer_feedback }}` variable (§5H).
11. Write tests for reviewer module, review API endpoints, and pipeline review gate.

### Relevant Subplans

- **Subplan 5H** (Human Review & Approval Workflow) — slices 1–5 (ReviewTask model done in Sprint 1, core reviewer logic, review queue API, review queue UI, diff viewer for correction review). Slices 6–8 (adaptation review, video review, notifications) are deferred.
- **Subplan 5A** (Transcript Correction + Diff Review) — slices 4–5 (dashboard diff viewer, review gate integration).
- **§9** (Quality Assurance & Human Review Design) — review queue UI wireframe, diff view wireframe, review status state machine.

---

## Your Task

Produce a detailed implementation plan for Sprint 3. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, routes, templates)
3. **Reviewer Module Design** — `btcedu/core/reviewer.py` function signatures with parameters and return types:
   - `create_review_task(session, episode_id, stage, artifact_paths, diff_path, prompt_version_id)` → ReviewTask
   - `approve_review(session, review_task_id, notes=None)` → ReviewDecision
   - `reject_review(session, review_task_id, notes=None)` → ReviewDecision
   - `request_changes(session, review_task_id, notes)` → ReviewDecision
   - `get_pending_reviews(session)` → list[ReviewTask]
   - `get_review_detail(session, review_task_id)` → dict with task, episode, diff_data, decisions
4. **Review Gate Integration** — how the pipeline pauses after CORRECT:
   - After `correct_transcript()` succeeds, call `create_review_task()`
   - Episode status stays at CORRECTED (does not advance)
   - Pipeline's `resolve_pipeline_plan()` checks for approved ReviewTask before proceeding to next stage
   - On approval: episode status advances (ready for TRANSLATE in future sprint)
   - On rejection: episode status reverts to TRANSCRIBED; optionally mark corrected output as stale
   - On request-changes: store feedback notes, revert to TRANSCRIBED, inject notes into re-correction prompt
5. **Flask Routes** — for each route: HTTP method, URL, request parameters, response format, template used.
6. **Dashboard Templates and UI** — describe each template:
   - Review queue page (list view with filtering by status)
   - Review detail page with diff viewer for corrections
   - Approve/reject/request-changes action buttons
   - Navigation badge for pending count
7. **Diff Viewer Component** — how to render `correction_diff.json` as a side-by-side view:
   - Use the diff data from Sprint 2 (changes array with original/corrected/context)
   - Highlight changes by type/category with color coding
   - Show summary statistics above the diff
8. **Reviewer Feedback Injection** — how `{{ reviewer_feedback }}` is passed to the correction prompt on re-run. Where the notes are stored and how the corrector reads them.
9. **CLI Review Commands** — Click command signatures for `btcedu review list`, `btcedu review approve <id>`, `btcedu review reject <id>`.
10. **Test Plan** — list each test function, what it asserts, and which file it belongs to.
11. **Implementation Order** — numbered sequence of steps.
12. **Definition of Done** — checklist.
13. **Non-Goals** — explicit list of what Sprint 3 does NOT include.

---

## Constraints

- **Backward compatibility**: v1 pipeline is unaffected. The review system only applies to v2 pipeline stages.
- **Follow existing web patterns**: Study `btcedu/web/` for Flask route patterns, template structure, static file organization, and Jinja2 usage.
- **Follow existing UI patterns**: The dashboard already has episode list/detail views. Match the styling, layout, and interaction patterns.
- **No external JS frameworks**: Unless the existing dashboard already uses one. Use vanilla JS or whatever the dashboard already uses.
- **Additive changes only**: New routes, new templates, new static files. Minimal changes to existing templates (only to add navigation badge).
- **No rewrites**: Do not refactor existing web code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include code snippets for Flask route definitions, Jinja2 template structure, and reviewer function signatures.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The diff viewer should be functional and readable, not necessarily polished. Simple side-by-side with color-coded highlights is sufficient.
- For the review queue, a simple table/list with status icons, episode ID, stage, and creation time is sufficient.
- The approve/reject flow should be synchronous (click button → immediate effect on pipeline status).
- If the existing dashboard uses a specific CSS framework or pattern, follow it. If not, use minimal custom CSS.
- The navigation badge for pending review count can be a simple span with a number, fetched on page load.
