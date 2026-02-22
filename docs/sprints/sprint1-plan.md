# Sprint 1 — Planning Prompt (Foundation & Schema Evolution)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, current codebase (especially `btcedu/models/`, `btcedu/migrations/`, `btcedu/prompts/`, `btcedu/core/pipeline.py`, `btcedu/config.py`)
> - **Expected output**: A file-level implementation plan covering schema changes, new models, PromptRegistry skeleton, migration SQL, test plan, implementation order, Definition of Done, and Non-Goals.

---

## Context

You are planning **Sprint 1 (Phase 0: Foundation & Schema Evolution)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprint 1 is the foundational sprint — no new pipeline stages are implemented, no UI changes, no new CLI commands beyond what is needed for migration and prompt listing.

### Sprint 1 Focus (from MASTERPLAN.md §4 Phase 0 and §12 Sprint 1)

1. Extend the `EpisodeStatus` enum with new v2 pipeline statuses: `CORRECTED`, `TRANSLATED`, `ADAPTED`, `CHAPTERIZED`, `IMAGES_GENERATED`, `TTS_DONE`, `RENDERED`, `APPROVED`, `PUBLISHED`, `COST_LIMIT`.
2. Add `pipeline_version` (INTEGER DEFAULT 1), `review_status` (TEXT), `youtube_video_id` (TEXT), and `published_at_youtube` (DATETIME) columns to the `episodes` table.
3. Create the `prompt_versions` table and `PromptVersion` SQLAlchemy model (see §7.3).
4. Create the `review_tasks` and `review_decisions` tables and corresponding models (see §7.3).
5. Create the `btcedu/prompts/templates/` directory with the first template file (`system.md`), migrating the existing system prompt content from `btcedu/prompts/system.py`.
6. Implement a basic `PromptRegistry` in `btcedu/core/prompt_registry.py` — load template, compute SHA-256 hash, register version in DB, resolve default.
7. Add `pipeline_version` and `max_episode_cost_usd` to the Pydantic `Settings` in `btcedu/config.py`.
8. Write tests for all new models, migrations, and the PromptRegistry.

### Relevant Subplans

- **Subplan 5J** (Prompt Management / Versioning Framework) — slices 1–2 only (model + registry core).
- **Subplan 5H** (Human Review & Approval Workflow) — slice 1 only (ReviewTask model and migration).
- **§7** (Data Model & Schema Evolution Plan) — migration sequencing N+1 through N+3.

---

## Your Task

Produce a detailed implementation plan for Sprint 1. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, SQL statements)
3. **Migration SQL** — exact `CREATE TABLE` and `ALTER TABLE` statements for each migration, following the existing pattern in `btcedu/migrations/`.
4. **New Models** — `PromptVersion`, `ReviewTask`, `ReviewDecision` with all fields, types, constraints, and indexes.
5. **PromptRegistry Skeleton** — class with method signatures: `get_default()`, `register_version()`, `promote_to_default()`, `get_history()`. Include docstrings describing expected behavior.
6. **Config Changes** — exact new fields for the Pydantic `Settings` class.
7. **Test Plan** — list each test function, what it asserts, and which file it belongs to.
8. **Implementation Order** — numbered sequence of steps a developer should follow.
9. **Definition of Done** — checklist of verifiable criteria.
10. **Non-Goals** — explicit list of what Sprint 1 does NOT include.

---

## Constraints

- **Backward compatibility**: All existing v1 pipeline functionality must continue to work unchanged. Existing episodes keep `pipeline_version=1`.
- **Additive changes only**: No column drops, no table renames, no destructive migrations.
- **Follow existing patterns**: Study `btcedu/models/episode.py`, `btcedu/models/content_artifact.py`, `btcedu/migrations/__init__.py`, and `btcedu/db.py` to match code style, naming conventions, and migration patterns.
- **No rewrites**: Do not refactor existing code unless strictly necessary for the new additions.
- **Preserve existing prompt modules**: The legacy Python prompt files (`btcedu/prompts/system.py`, `outline.py`, etc.) remain untouched. The new template system is additive.
- **Use small, safe, incremental steps.**
- **Preserve compatibility with the existing pipeline and patterns.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include code snippets for migration SQL, model definitions, and PromptRegistry method signatures.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- If the existing migration system uses a pattern (e.g., numbered migration classes, a `MIGRATIONS` list), follow that pattern exactly.
- If the existing codebase uses `datetime` from a specific import (e.g., `datetime.utcnow()` or `datetime.now(UTC)`), use the same convention.
- State clearly which files are NEW vs MODIFIED.
