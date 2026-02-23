# Sprint 1 — Validation Output: Foundation & Schema Evolution

**Sprint**: 1 (Phase 0)
**Validated**: 2026-02-23
**Test result**: 283 passed, 0 failed (post-fix)

---

## 1) Verdict

### **PASS WITH FIXES** (fixes applied during validation)

One required fix was identified (missing `published_at_youtube` column) and applied in-place. After the fix, all checklist items pass. Sprint 1 is ready for commit and Sprint 2 can proceed.

---

## 2) Scope Check

### In-scope items implemented

| Item | Status |
|------|--------|
| EpisodeStatus enum extended (10 new values) | DONE |
| PipelineStage enum extended (9 new values) | DONE |
| Episode model: pipeline_version, review_status, youtube_video_id, published_at_youtube | DONE (published_at_youtube added during validation) |
| PromptVersion model + table | DONE |
| ReviewTask model + table | DONE |
| ReviewDecision model + table | DONE |
| Migrations 002-004 | DONE |
| PromptRegistry (load, hash, register, promote, history) | DONE |
| btcedu/prompts/templates/system.md | DONE |
| Settings: pipeline_version, max_episode_cost_usd | DONE |
| _STATUS_ORDER updated in pipeline.py | DONE |
| Tests for all new components | DONE |

### Out-of-scope changes detected

None. All changes are strictly within Sprint 1 scope:
- No new pipeline stages implemented
- No CLI commands added
- No dashboard/UI changes
- No existing code refactored
- No legacy prompt modules modified
- No pyproject.toml changes
- No files touched outside the plan

---

## 3) Correctness Review

### Schema & Migration Correctness (Checklist §1)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 1.1 | EpisodeStatus has all 10 new values | PASS | CORRECTED, TRANSLATED, ADAPTED, CHAPTERIZED, IMAGES_GENERATED, TTS_DONE, RENDERED, APPROVED, PUBLISHED, COST_LIMIT |
| 1.2 | No existing EpisodeStatus values removed | PASS | All 8 original values intact |
| 1.3 | Episode.pipeline_version (INT, default 1) | PASS | `mapped_column(Integer, nullable=False, default=1)` |
| 1.4 | Episode.review_status (TEXT, nullable) | PASS | `mapped_column(String(32), nullable=True)` |
| 1.5 | Episode.youtube_video_id (TEXT, nullable) | PASS | `mapped_column(String(64), nullable=True)` |
| 1.6 | Episode.published_at_youtube (DATETIME, nullable) | PASS | **Fixed during validation** — was missing, now added |
| 1.7 | prompt_versions table matches MASTERPLAN §7.3 | PASS | All columns, types, constraints, indexes match |
| 1.8 | prompt_versions UNIQUE constraints | PASS | (name, version) and (name, content_hash) |
| 1.9 | review_tasks table matches MASTERPLAN §7.3 | PASS | All columns present |
| 1.10 | review_decisions FK to review_tasks | PASS | `FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)` |
| 1.11 | Migrations additive only | PASS | No DROP, RENAME, or destructive ops |
| 1.12 | Migrations follow existing pattern | PASS | Class-based, idempotent, `mark_applied()` at end |
| 1.13 | Migrations run on fresh DB | PASS | Tested via `test_all_migrations_run_sequentially` |
| 1.14 | Migrations run on existing DB with data | PASS | Tested via `test_existing_pipeline_works_after_migrations` |

### Model Correctness (Checklist §2)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 2.1 | PromptVersion has all required fields | PASS | id, name, version, content_hash, template_path, model, temperature, max_tokens, is_default, created_at, notes |
| 2.2 | ReviewTask has all required fields | PASS | id, episode_id, stage, status, artifact_paths (JSON Text), diff_path, prompt_version_id (FK), created_at, reviewed_at, reviewer_notes, artifact_hash |
| 2.3 | ReviewDecision has all required fields | PASS | id, review_task_id (FK), decision, notes, decided_at |
| 2.4 | Models follow existing patterns | PASS | Declarative base, `_utcnow()`, `__repr__`, naming conventions all match |
| 2.5 | Models importable from __init__.py | PASS | `from btcedu.models import PromptVersion, ReviewTask, ReviewDecision` works |

### PromptRegistry (Checklist §3)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 3.1 | Exists at correct path | PASS | `btcedu/core/prompt_registry.py` |
| 3.2 | get_default(name) | PASS | Returns default or None |
| 3.3 | register_version() | PASS | Creates new version, computes hash, resolves metadata from frontmatter |
| 3.4 | promote_to_default() | PASS | Sets is_default=True, demotes previous |
| 3.5 | get_history(name) | PASS | Returns list ordered by version desc |
| 3.6 | Content hash is SHA-256 of body | PASS | Frontmatter excluded from hash |
| 3.7 | Deduplication by hash | PASS | Returns existing version if content hash matches |
| 3.8 | YAML frontmatter parsing | PASS | yaml.safe_load with regex extraction |

### Prompt Template (Checklist §4)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 4.1 | system.md exists | PASS | `btcedu/prompts/templates/system.md` |
| 4.2 | Valid YAML frontmatter | PASS | name, model, temperature, max_tokens, description, author |
| 4.3 | Body content derived from system.py | PASS | Content matches — line continuations (`\`) flattened into single lines in .md, which is correct |
| 4.4 | Legacy system.py NOT modified | PASS | `git diff btcedu/prompts/system.py` shows no changes |

### Config Changes (Checklist §5)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 5.1 | pipeline_version (int, default=1) | PASS | |
| 5.2 | max_episode_cost_usd (float, default=10.0) | PASS | |
| 5.3 | Existing config fields unchanged | PASS | Only addition, no modifications |
| 5.4 | .env updated | N/A | No .env file in repo; defaults are safe; no action needed |

### Risks / Defects Found

1. **[FIXED] Missing `published_at_youtube` column** — The Opus plan output explicitly deferred this as an `[ASSUMPTION]`, but the MASTERPLAN §7.2, `sprint1-plan.md`, `sprint1-implement.md`, and `sprint1-validation.md` all require it. Added to Episode model and migration 002 during validation.

2. **[LOW RISK] `compute_hash` double-strips frontmatter** — `compute_hash()` calls `_strip_frontmatter()` on the `content` parameter, but `register_version()` passes the already-stripped `body` from `load_template()`. This means when called via `register_version`, no double-stripping occurs (body has no frontmatter). When called directly with raw content containing frontmatter, the frontmatter is correctly stripped. No bug, but the docstring could be clearer. Non-blocking.

3. **[LOW RISK] Flaky `test_job_error_state`** — Observed one intermittent failure in `tests/test_web.py::TestJobsAndLogs::test_job_error_state` during testing. The test passes when run in isolation and on subsequent full-suite runs. This is a pre-existing timing issue in the web test, unrelated to Sprint 1 changes.

---

## 4) Test Review

### Coverage present

| Area | Test File | Count | Quality |
|------|-----------|-------|---------|
| New ORM models | `test_sprint1_models.py` | 12 | Good — covers CRUD, unique constraints, defaults, cascade delete, enum counts |
| Migrations 002-004 | `test_sprint1_migrations.py` | 8 | Good — covers column creation, table creation, idempotency, sequential run, post-migration queries |
| PromptRegistry | `test_prompt_registry.py` | 22 | Excellent — covers all methods, edge cases (dedup, no frontmatter, explicit params override, missing version), plus integration with real system.md |

### Missing or weak tests

1. **`published_at_youtube` column** — Added assertion to `test_pipeline_version_default` during validation. Migration test also now checks for the column. **Fixed.**

2. **`ReviewTask` FK to `PromptVersion`** — No test verifies that creating a `ReviewTask` with a valid `prompt_version_id` FK actually links correctly. Low priority since FK is tested structurally in migration tests.

3. **`compute_hash` called directly with frontmatter** — Not directly tested (only tested indirectly via `register_version`). Low priority — `_strip_frontmatter` is tested separately.

### Suggested additions (nice-to-have, not blocking)

- A test that creates a `ReviewTask` with `prompt_version_id` pointing to a real `PromptVersion` record.
- A test confirming `PipelineStage.CORRECT` etc. can be used in `PipelineRun` (ORM roundtrip).

---

## 5) Backward Compatibility Check

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 6.1 | `btcedu status` still works | PASS | No CLI changes; `_STATUS_ORDER` extended only |
| 6.2 | Existing episodes unaffected | PASS | All additive columns; `pipeline_version DEFAULT 1` backfills safely |
| 6.3 | Existing pipeline stages function | PASS | `_STAGES` list in pipeline.py untouched; only `_STATUS_ORDER` dict extended |
| 6.4 | No import breakages | PASS | Only additions to `__init__.py`; no removals |
| 6.5 | No existing tests broken | PASS | 283/283 pass including all pre-existing tests |

**Risk assessment**: Very low. All changes are additive. The `_STATUS_ORDER` extension adds new entries but doesn't affect the v1 pipeline execution path, which only iterates `_STAGES` (unchanged). SQLite stores enum values as TEXT, so new enum members don't require schema changes for the status column.

---

## 6) Required Fixes Before Commit

All fixes were applied during validation:

1. **[APPLIED]** Add `published_at_youtube: Mapped[datetime | None]` column to Episode model in `btcedu/models/episode.py`
2. **[APPLIED]** Add `published_at_youtube` ALTER TABLE to migration 002 in `btcedu/migrations/__init__.py`
3. **[APPLIED]** Add `published_at_youtube` assertion to migration 002 test in `tests/test_sprint1_migrations.py`
4. **[APPLIED]** Add `published_at_youtube` assertion to model default test in `tests/test_sprint1_models.py`

Post-fix test result: **283 passed, 0 failed**.

---

## 7) Nice-to-Have Improvements (optional, non-blocking)

1. Add a test for `ReviewTask` with a valid `prompt_version_id` FK relationship.
2. Clarify `compute_hash()` docstring to note it strips frontmatter only if present (safe to call with pre-stripped content).
3. Consider adding `PipelineStage` ORM roundtrip test for new v2 stages.
4. The flaky `test_job_error_state` web test could benefit from investigation (pre-existing, not Sprint 1 related).

---

## Scope Creep Detection (Checklist §8)

| # | Check | Result |
|---|-------|--------|
| 8.1 | Only planned files modified | PASS |
| 8.2 | No new pipeline stages | PASS |
| 8.3 | No web dashboard changes | PASS |
| 8.4 | No new CLI commands | PASS |
| 8.5 | No style/cleanup refactors | PASS |
| 8.6 | No pyproject.toml changes | PASS |
| 8.7 | No prompt Python modules modified | PASS |

## Code Quality (Checklist §9)

| # | Check | Result |
|---|-------|--------|
| 9.1 | Naming conventions match | PASS |
| 9.2 | No hardcoded values | PASS |
| 9.3 | No security issues | PASS — migrations use `text()` with no user input; parameterized queries used where applicable |
| 9.4 | Correct SQLite syntax | PASS |
| 9.5 | Type hints consistent | PASS |

---

## Deferred Items Acknowledged

- Media assets table (Sprint 6-7)
- Publish jobs table (Sprint 11)
- Dashboard changes (Sprint 3)
- Pipeline stage implementations (Sprint 2+)
- Prompt A/B testing infrastructure (later sprint)
- Cascade invalidation logic (Sprint 2+)
