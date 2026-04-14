# btcedu/models/ — SQLAlchemy 2.0 Models & Pydantic Schemas

## SQLAlchemy Models

All use `Mapped[]` / `mapped_column()` with `Base` from `btcedu/db.py` **EXCEPT MediaAsset**.

- `episode.py` — Episode, PipelineRun, Chunk, + enums: EpisodeStatus, PipelineStage, RunStatus
- `review.py` — ReviewTask, ReviewDecision, ReviewStatus enum
- `review_item.py` — ReviewItemAction, ReviewItemDecision (Phase 5 granular review)
- `content_artifact.py` — ContentArtifact
- `media_asset.py` — MediaAsset, MediaAssetType (**uses own `declarative_base()`!**)
- `prompt_version.py` — PromptVersion (unique: `(name, version)` and `(name, content_hash)`)
- `publish_job.py` — PublishJob, PublishJobStatus
- `channel.py` — Channel (has `content_profile` column, default `bitcoin_podcast`)
- `migration.py` — SchemaMigration

## Pydantic Models (chapter_schema.py)

ChapterDocument > Chapter > { Narration, Visual, Overlay, Transitions }
- `Visual.type` is a VisualType enum: TITLE_CARD, DIAGRAM, B_ROLL, TALKING_HEAD, SCREEN_SHARE
- `Chapter.visual` is **singular** (one Visual), not a list
- DIAGRAM/B_ROLL types require `image_prompt` field

## Critical Gotchas

- **MediaAsset separate Base**: `Base = declarative_base()` in `media_asset.py`. Tests MUST call `MediaBase.metadata.create_all(engine)` separately from `btcedu.db.Base.metadata.create_all(engine)`.
- **Datetime**: all models use `_utcnow() -> datetime.now(UTC)` for default values
- **String-based relationships** (e.g. `"ReviewItemDecision"`) require actual import at runtime, not just `TYPE_CHECKING`
- **FTS5**: `chunks_fts` virtual table created in `init_db()` / `_init_fts()`, not via standard model
