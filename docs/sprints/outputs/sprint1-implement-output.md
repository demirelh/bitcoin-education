# Sprint 1 — Implementation Output: Foundation & Schema Evolution

**Sprint**: 1 (Phase 0)
**Implemented**: 2026-02-23
**Test result**: 283 passed, 0 failed

---

## 1. Scope Summary

Sprint 1 extends the btcedu data model for the v2 video production pipeline. All changes are additive — no existing code was removed or refactored. The v1 pipeline continues to operate unchanged.

**What was implemented:**
- Extended `EpisodeStatus` enum (10 new values) and `PipelineStage` enum (9 new values)
- Added 4 new columns to Episode model (`pipeline_version`, `review_status`, `youtube_video_id`, `published_at_youtube`)
- Created 3 new ORM models: `PromptVersion`, `ReviewTask`, `ReviewDecision`
- Created 3 database migrations (002-004) following existing patterns
- Added 2 config fields: `pipeline_version`, `max_episode_cost_usd`
- Created first prompt template file (`system.md`) with YAML frontmatter
- Implemented full `PromptRegistry` class (load, hash, register, promote, history)
- Updated `_STATUS_ORDER` in pipeline.py for forward compat
- Wrote 3 test files with comprehensive coverage

**Assumptions made:**
- `[ASSUMPTION]` PyYAML already available (confirmed: v6.0.2)
- `[ASSUMPTION]` `artifact_paths` on ReviewTask stored as JSON-encoded TEXT
- `[ASSUMPTION]` ReviewTask `status` stored as plain TEXT (not SQLAlchemy Enum) for flexibility
- `[ASSUMPTION]` No `.env` changes needed — both new config fields have safe defaults

---

## 2. Files Created

| File | Description |
|------|-------------|
| `btcedu/models/prompt_version.py` | `PromptVersion` SQLAlchemy model |
| `btcedu/models/review.py` | `ReviewTask`, `ReviewDecision` models + `ReviewStatus` enum |
| `btcedu/core/prompt_registry.py` | `PromptRegistry` class with full implementation |
| `btcedu/prompts/templates/system.md` | First prompt template (migrated from system.py content) |
| `tests/test_sprint1_models.py` | 12 tests for new ORM models and enum extensions |
| `tests/test_sprint1_migrations.py` | 8 tests for migrations 002-004 |
| `tests/test_prompt_registry.py` | 22 tests for PromptRegistry |

## 3. Files Modified

| File | Changes |
|------|---------|
| `btcedu/models/episode.py` | +10 EpisodeStatus values, +9 PipelineStage values, +4 Episode columns |
| `btcedu/models/__init__.py` | +2 import lines for new models |
| `btcedu/migrations/__init__.py` | +3 Migration subclasses (002-004), appended to MIGRATIONS list |
| `btcedu/config.py` | +2 Settings fields (pipeline_version, max_episode_cost_usd) |
| `btcedu/core/pipeline.py` | Extended `_STATUS_ORDER` dict with 12 new entries |

## 4. Migration Details

| Migration | Version | Creates/Alters |
|-----------|---------|----------------|
| `AddV2PipelineColumnsMigration` | `002_add_v2_pipeline_columns` | ALTER TABLE episodes ADD COLUMN pipeline_version/review_status/youtube_video_id/published_at_youtube |
| `CreatePromptVersionsTableMigration` | `003_create_prompt_versions` | CREATE TABLE prompt_versions + 2 indexes |
| `CreateReviewTablesMigration` | `004_create_review_tables` | CREATE TABLE review_tasks + review_decisions + 3 indexes |

All migrations are idempotent (check-before-act pattern) and follow the existing Migration base class pattern.

## 5. Test Summary

```
tests/test_sprint1_models.py      - 12 tests (all pass)
tests/test_sprint1_migrations.py  -  8 tests (all pass)
tests/test_prompt_registry.py     - 22 tests (all pass)
----
Total new tests:                    42
Total suite:                       283 passed, 0 failed
```

## 6. Manual Verification Steps

1. `cd /home/pi/AI-Startup-Lab/bitcoin-education`
2. `.venv/bin/python -m pytest tests/ -v` — all 283 tests pass
3. `.venv/bin/python -c "from btcedu.models import PromptVersion, ReviewTask, ReviewDecision; print('Models import OK')"` — no errors
4. `.venv/bin/python -c "from btcedu.config import get_settings; s = get_settings(); print(f'pipeline_version={s.pipeline_version}, max_cost={s.max_episode_cost_usd}')"` — prints `pipeline_version=1, max_cost=10.0`
5. `.venv/bin/python -c "from btcedu.models.episode import EpisodeStatus, PipelineStage; print(f'{len(EpisodeStatus)} statuses, {len(PipelineStage)} stages')"` — prints `18 statuses, 16 stages`
6. `.venv/bin/python -c "from btcedu.core.prompt_registry import TEMPLATES_DIR; print(TEMPLATES_DIR); print((TEMPLATES_DIR / 'system.md').exists())"` — prints path and `True`

## 7. What Was Intentionally Deferred

- `media_assets` table (Phase 3)
- `publish_jobs` table (Phase 6)
- New CLI commands (Sprint 2+)
- Dashboard/UI changes (Sprint 3+)
- New pipeline stages (Sprint 2+)
- Cascade invalidation logic (Sprint 2+)
- Prompt A/B testing infrastructure (later)

## 8. Rollback / Safe Revert Notes

All changes are additive:
- **Migrations**: Can be reverted by dropping the 3 new tables and 4 new columns. However, since SQLite doesn't support DROP COLUMN natively, the safest rollback is `git revert` + restore DB from backup.
- **Code**: All new files can be deleted. Modified files only had additions (no deletions). `git revert` of the commit is safe.
- **No data loss risk**: No existing data is modified by any migration. Existing episodes automatically get `pipeline_version=1` via DEFAULT.
