# Sprint 1 — Validation Prompt (Foundation & Schema Evolution)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 1 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 1 (Phase 0: Foundation & Schema Evolution)** implementation of the btcedu video production pipeline.

Sprint 1 was scoped to:
- Extend `EpisodeStatus` enum with v2 pipeline statuses
- Add `pipeline_version`, `review_status`, `youtube_video_id`, `published_at_youtube` to Episode model
- Create `prompt_versions`, `review_tasks`, `review_decisions` tables and models
- Implement basic `PromptRegistry` (load, hash, register, get_default)
- Create `btcedu/prompts/templates/system.md`
- Add `pipeline_version` and `max_episode_cost_usd` to Settings
- Write tests for all of the above

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Schema & Migration Correctness

- [ ] **1.1** `EpisodeStatus` enum contains all required new values: `CORRECTED`, `TRANSLATED`, `ADAPTED`, `CHAPTERIZED`, `IMAGES_GENERATED`, `TTS_DONE`, `RENDERED`, `APPROVED`, `PUBLISHED`, `COST_LIMIT`
- [ ] **1.2** No existing `EpisodeStatus` values were removed or renamed
- [ ] **1.3** Episode model has `pipeline_version` column (INTEGER, default 1)
- [ ] **1.4** Episode model has `review_status` column (TEXT, nullable)
- [ ] **1.5** Episode model has `youtube_video_id` column (TEXT, nullable)
- [ ] **1.6** Episode model has `published_at_youtube` column (DATETIME, nullable)
- [ ] **1.7** `prompt_versions` table schema matches MASTERPLAN.md §7.3 (all columns, types, constraints, indexes)
- [ ] **1.8** `prompt_versions` has UNIQUE constraints on (name, version) and (name, content_hash)
- [ ] **1.9** `review_tasks` table schema matches MASTERPLAN.md §7.3
- [ ] **1.10** `review_decisions` table schema matches MASTERPLAN.md §7.3 with FK to review_tasks
- [ ] **1.11** Migrations are additive only — no DROP, no RENAME, no destructive operations
- [ ] **1.12** Migrations follow the existing migration pattern in the codebase
- [ ] **1.13** Migrations can run on a fresh database (no prior state required beyond existing migrations)
- [ ] **1.14** Migrations can run on an existing database with data (no data loss)

### 2. Model Correctness

- [ ] **2.1** `PromptVersion` model has all required fields: id, name, version, content_hash, template_path, model, temperature, max_tokens, is_default, created_at, notes
- [ ] **2.2** `ReviewTask` model has all required fields: id, episode_id, stage, status, artifact_paths (JSON), diff_path, prompt_version_id (FK), created_at, reviewed_at, reviewer_notes, artifact_hash
- [ ] **2.3** `ReviewDecision` model has all required fields: id, review_task_id (FK), decision, notes, decided_at
- [ ] **2.4** Models follow existing SQLAlchemy patterns (declarative base, naming conventions, `__repr__`)
- [ ] **2.5** Models are importable from `btcedu/models/__init__.py`

### 3. PromptRegistry

- [ ] **3.1** `PromptRegistry` exists at `btcedu/core/prompt_registry.py`
- [ ] **3.2** `get_default(name)` returns the default PromptVersion for a given name
- [ ] **3.3** `register_version(name, template_path, **params)` creates a new version, computing content hash
- [ ] **3.4** `promote_to_default(version_id)` sets one version as default, unsetting others for that name
- [ ] **3.5** `get_history(name)` returns all versions for a given name
- [ ] **3.6** Content hash is SHA-256 of template body (excluding YAML frontmatter)
- [ ] **3.7** Registry does not create a duplicate version if content hash already exists for that name
- [ ] **3.8** YAML frontmatter is parsed correctly to extract model, temperature, max_tokens, etc.

### 4. Prompt Template

- [ ] **4.1** `btcedu/prompts/templates/system.md` exists
- [ ] **4.2** Has valid YAML frontmatter with: name, model, temperature, max_tokens, description, author
- [ ] **4.3** Body content matches or is derived from existing `btcedu/prompts/system.py`
- [ ] **4.4** Legacy `btcedu/prompts/system.py` is NOT modified (backward compatibility)

### 5. Config Changes

- [ ] **5.1** `pipeline_version` added to Settings (int, default=1)
- [ ] **5.2** `max_episode_cost_usd` added to Settings (float, default=10.0)
- [ ] **5.3** Existing config fields are unchanged
- [ ] **5.4** `.env` or `.env.example` updated if the project uses one (or documented)

### 6. V1 Pipeline Compatibility (Regression)

- [ ] **6.1** `btcedu status` command still works correctly
- [ ] **6.2** Existing episodes are unaffected — no status changes, no data corruption
- [ ] **6.3** Existing pipeline stages (detect, download, transcribe, chunk, generate, refine) still function
- [ ] **6.4** No existing imports or module paths are broken
- [ ] **6.5** No existing tests are broken

### 7. Test Coverage

- [ ] **7.1** Tests exist for new EpisodeStatus values
- [ ] **7.2** Tests exist for new Episode columns (pipeline_version default, nullability)
- [ ] **7.3** Tests exist for PromptVersion model (create, query, unique constraints)
- [ ] **7.4** Tests exist for ReviewTask model (create, query, FK relationships)
- [ ] **7.5** Tests exist for ReviewDecision model (create, query, FK to ReviewTask)
- [ ] **7.6** Tests exist for PromptRegistry (load, hash, register, get_default, promote, idempotent re-register)
- [ ] **7.7** All tests pass with `pytest tests/`
- [ ] **7.8** Tests use in-memory SQLite or follow existing test fixture patterns

### 8. Scope Creep Detection

- [ ] **8.1** No files were modified that are NOT in the sprint plan
- [ ] **8.2** No new pipeline stages were implemented
- [ ] **8.3** No web dashboard changes were made
- [ ] **8.4** No new CLI commands were added (beyond what the plan specifies)
- [ ] **8.5** No existing code was refactored for style/cleanup reasons
- [ ] **8.6** No unnecessary dependencies were added to pyproject.toml
- [ ] **8.7** No prompt Python modules were modified (system.py, outline.py, etc.)

### 9. Code Quality

- [ ] **9.1** New code follows existing naming conventions
- [ ] **9.2** No hardcoded values where config settings should be used
- [ ] **9.3** No obvious security issues (SQL injection in raw queries, etc.)
- [ ] **9.4** Migration SQL is correct SQLite syntax
- [ ] **9.5** Type hints are used consistently with existing codebase patterns

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 1 is complete and ready for Sprint 2. |
| **PASS WITH FIXES** | Minor issues found that can be fixed quickly. List the specific items that need fixing. Sprint can proceed to Sprint 2 after fixes. |
| **FAIL** | Critical issues found. List the specific items that fail. Sprint 1 must be reworked before proceeding. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Deferred Items Acknowledged:

- Media assets table (Sprint 6-7)
- Publish jobs table (Sprint 11)
- Dashboard changes (Sprint 3)
- Pipeline stage implementations (Sprint 2+)
- Prompt A/B testing (later sprint)

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- Check that the implementation matches both the sprint plan AND the MASTERPLAN.md specifications.
- If the implementation deviates from the plan in a reasonable way, note it but do not automatically fail it.
