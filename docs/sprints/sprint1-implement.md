# Sprint 1 — Implementation Prompt (Foundation & Schema Evolution)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 1 (paste below or provide as context), `MASTERPLAN.md`, current codebase
> - **Expected output**: All code changes (new files, modified files), migration scripts, tests — committed and passing.

---

## Context

You are implementing **Sprint 1 (Phase 0: Foundation & Schema Evolution)** of the btcedu video production pipeline.

The Opus planning output for this sprint is provided below (or in context). Follow it precisely.

This sprint is **foundation only** — you are extending the data model, adding new tables, creating a basic PromptRegistry, and writing tests. No new pipeline stages, no UI changes, no new CLI commands (beyond verifying existing ones still work).

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 1 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — before modifying any file, read it completely to understand current patterns.
2. **Config changes** — add `pipeline_version` (int, default=1) and `max_episode_cost_usd` (float, default=10.0) to `btcedu/config.py` Settings class.
3. **Extend EpisodeStatus enum** — add new values (`CORRECTED`, `TRANSLATED`, `ADAPTED`, `CHAPTERIZED`, `IMAGES_GENERATED`, `TTS_DONE`, `RENDERED`, `APPROVED`, `PUBLISHED`, `COST_LIMIT`) to `btcedu/models/episode.py`. Place them after existing values. Do NOT remove or rename any existing values.
4. **Add Episode model columns** — add `pipeline_version`, `review_status`, `youtube_video_id`, `published_at_youtube` to the Episode model.
5. **Create PromptVersion model** — new file `btcedu/models/prompt_version.py` with all fields from MASTERPLAN.md §7.3.
6. **Create ReviewTask and ReviewDecision models** — new file `btcedu/models/review.py` with all fields from MASTERPLAN.md §7.3.
7. **Update `btcedu/models/__init__.py`** — import new models so they are registered with SQLAlchemy.
8. **Write migrations** — follow the existing migration pattern in `btcedu/migrations/__init__.py`. Create Migration subclasses for:
   - Episode table alterations (pipeline_version, review_status, youtube_video_id, published_at_youtube)
   - prompt_versions table creation
   - review_tasks and review_decisions table creation
9. **Create `btcedu/prompts/templates/` directory** — add `system.md` with the system prompt content migrated from `btcedu/prompts/system.py`. Use the template format from MASTERPLAN.md §6.3 (YAML frontmatter + body).
10. **Implement PromptRegistry** — new file `btcedu/core/prompt_registry.py`. Implement: `get_default()`, `register_version()`, `promote_to_default()`, `get_history()`. The registry should:
    - Parse YAML frontmatter from template files
    - Compute SHA-256 hash of template content (excluding frontmatter)
    - Check DB for existing version with same hash before creating new one
    - Support marking one version as default per prompt name
11. **Write tests** — create `tests/test_models.py` (or extend existing) and `tests/test_prompt_registry.py`.
12. **Verify** — run `pytest tests/` to confirm all tests pass. Run `btcedu status` (or equivalent) to confirm existing pipeline still works.

### Anti-scope-creep guardrails

- **Do NOT** implement any new pipeline stages (correct, translate, adapt, etc.).
- **Do NOT** add any new CLI commands beyond what the plan specifies.
- **Do NOT** modify the web dashboard or templates.
- **Do NOT** change `btcedu/core/pipeline.py` beyond what is strictly needed (if anything).
- **Do NOT** modify existing prompt Python modules (`btcedu/prompts/system.py`, `outline.py`, etc.).
- **Do NOT** refactor existing code for style or cleanup purposes.
- **Do NOT** add dependencies to `pyproject.toml` unless strictly required (e.g., PyYAML for frontmatter parsing — check if already available).

### Code patterns to follow

- **Models**: Follow the pattern in `btcedu/models/episode.py` and `btcedu/models/content_artifact.py` — SQLAlchemy declarative base, column definitions, `__tablename__`, `__repr__`.
- **Migrations**: Follow the pattern in `btcedu/migrations/__init__.py` — check if migrations use a class-based or function-based pattern, and match it.
- **Config**: Follow the Pydantic Settings pattern in `btcedu/config.py`.
- **Imports**: Follow existing import conventions (relative vs absolute).
- **Tests**: Follow existing test patterns in `tests/`.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change (for new files: full content; for modified files: the specific changes with context)

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred to later sprints
- Manual verification steps to confirm the implementation works

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- If a test requires a database, use an in-memory SQLite database (follow existing test patterns).
- All migrations must be idempotent or guarded (e.g., `IF NOT EXISTS`).

---

## Definition of Done

- [ ] `EpisodeStatus` enum has all new values
- [ ] Episode model has `pipeline_version`, `review_status`, `youtube_video_id`, `published_at_youtube` columns
- [ ] `prompt_versions` table exists with correct schema
- [ ] `review_tasks` table exists with correct schema
- [ ] `review_decisions` table exists with correct schema
- [ ] `PromptVersion`, `ReviewTask`, `ReviewDecision` SQLAlchemy models are importable
- [ ] Migrations run cleanly on a fresh database and on an existing database
- [ ] `btcedu/prompts/templates/system.md` exists with valid frontmatter
- [ ] `PromptRegistry` can load a template, hash it, register a version, and retrieve the default
- [ ] `pipeline_version` and `max_episode_cost_usd` are in Settings
- [ ] All tests pass (`pytest tests/`)
- [ ] Existing `btcedu status` command still works
- [ ] No existing tests are broken

## Non-Goals

- New pipeline stages (CORRECT, TRANSLATE, etc.)
- Dashboard / UI changes
- New CLI commands for pipeline stages
- Prompt A/B testing infrastructure
- Media assets table (deferred to later sprint)
- Publish jobs table (deferred to later sprint)
