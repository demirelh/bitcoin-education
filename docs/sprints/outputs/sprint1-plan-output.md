# Sprint 1 — Implementation Plan: Foundation & Schema Evolution

**Sprint**: 1 (Phase 0)
**Generated**: 2026-02-22
**Source of truth**: `MASTERPLAN.md` §4 Phase 0, §7, §12 Sprint 1; `docs/sprints/sprint1-plan.md`

---

## 1. Sprint Scope Summary

Sprint 1 lays the database and model foundation for the v2 video production pipeline. It extends the `EpisodeStatus` enum with 10 new statuses, adds `pipeline_version` and related columns to the `episodes` table, creates three new tables (`prompt_versions`, `review_tasks`, `review_decisions`), introduces the `PromptRegistry` class for file-based prompt versioning, creates the first prompt template (`system.md`), and adds two new config fields (`pipeline_version`, `max_episode_cost_usd`). All changes are additive. The existing v1 pipeline continues to operate unchanged. No new CLI commands beyond migration are added. No UI changes. No new pipeline stages are implemented.

---

## 2. Non-Goals (Explicit)

- No new pipeline stages (CORRECT, TRANSLATE, etc.)
- No new CLI commands (except what `btcedu migrate` already handles)
- No dashboard/UI changes
- No `media_assets` or `publish_jobs` tables (deferred to later sprints)
- No refactoring of existing code (generator.py, pipeline.py, etc.)
- No modifications to legacy prompt Python modules (`btcedu/prompts/system.py`, etc.)
- No cascade invalidation logic
- No `published_at_youtube` column — `[ASSUMPTION]` deferred to the sprint that implements PUBLISH stage, since it's unused until then; keeping scope minimal

---

## 3. File-Level Plan

### NEW Files

| File | Description |
|------|-------------|
| `btcedu/models/prompt_version.py` | `PromptVersion` SQLAlchemy model |
| `btcedu/models/review.py` | `ReviewTask` and `ReviewDecision` SQLAlchemy models |
| `btcedu/core/prompt_registry.py` | `PromptRegistry` class (load, hash, register, resolve default) |
| `btcedu/prompts/templates/system.md` | First file-based prompt template (migrated from `system.py`) |
| `tests/test_prompt_registry.py` | Tests for `PromptRegistry` |
| `tests/test_sprint1_models.py` | Tests for new ORM models (`PromptVersion`, `ReviewTask`, `ReviewDecision`) |
| `tests/test_sprint1_migrations.py` | Tests for the three new migrations |

### MODIFIED Files

| File | Changes |
|------|---------|
| `btcedu/models/episode.py` | Add 10 new `EpisodeStatus` values; add 4 new `PipelineStage` values |
| `btcedu/models/__init__.py` | Add imports for `PromptVersion`, `ReviewTask`, `ReviewDecision` |
| `btcedu/migrations/__init__.py` | Add 3 new Migration subclasses; append to `MIGRATIONS` list |
| `btcedu/config.py` | Add `pipeline_version` and `max_episode_cost_usd` to `Settings` |
| `btcedu/core/pipeline.py` | Add new statuses to `_STATUS_ORDER` dict (for forward compat; no logic changes) |

---

## 4. Migration SQL

Following the existing pattern: each migration is a class inheriting `Migration`, uses `session.execute(text(...))`, checks for existence before acting (idempotent), and calls `self.mark_applied(session)` at the end.

### Migration 002: Add V2 Pipeline Columns to Episodes

**Version**: `002_add_v2_pipeline_columns`

```python
class AddV2PipelineColumnsMigration(Migration):
    @property
    def version(self) -> str:
        return "002_add_v2_pipeline_columns"

    @property
    def description(self) -> str:
        return "Add pipeline_version, review_status, youtube_video_id columns to episodes"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("PRAGMA table_info(episodes)"))
        columns = [row[1] for row in result.fetchall()]

        # Step 1: Add pipeline_version column
        if "pipeline_version" not in columns:
            session.execute(
                text("ALTER TABLE episodes ADD COLUMN pipeline_version INTEGER DEFAULT 1")
            )
            session.commit()
            logger.info("Added pipeline_version column")

        # Step 2: Add review_status column
        if "review_status" not in columns:
            session.execute(
                text("ALTER TABLE episodes ADD COLUMN review_status TEXT")
            )
            session.commit()
            logger.info("Added review_status column")

        # Step 3: Add youtube_video_id column
        if "youtube_video_id" not in columns:
            session.execute(
                text("ALTER TABLE episodes ADD COLUMN youtube_video_id TEXT")
            )
            session.commit()
            logger.info("Added youtube_video_id column")

        self.mark_applied(session)
```

**Notes**:
- `[ASSUMPTION]` `published_at_youtube` deferred (unused until Publish sprint).
- All existing episodes automatically get `pipeline_version=1` via `DEFAULT 1`.
- SQLite stores enum values as TEXT, so new `EpisodeStatus` values work without schema changes.

### Migration 003: Create Prompt Versions Table

**Version**: `003_create_prompt_versions`

```python
class CreatePromptVersionsTableMigration(Migration):
    @property
    def version(self) -> str:
        return "003_create_prompt_versions"

    @property
    def description(self) -> str:
        return "Create prompt_versions table for prompt versioning system"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_versions'")
        )
        if not result.fetchone():
            session.execute(text("""
                CREATE TABLE prompt_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    template_path TEXT,
                    model TEXT,
                    temperature REAL,
                    max_tokens INTEGER,
                    is_default BOOLEAN NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL,
                    notes TEXT,
                    UNIQUE(name, version),
                    UNIQUE(name, content_hash)
                )
            """))
            session.execute(
                text("CREATE INDEX idx_prompt_versions_name ON prompt_versions(name)")
            )
            session.execute(
                text("CREATE INDEX idx_prompt_versions_default ON prompt_versions(name, is_default)")
            )
            session.commit()
            logger.info("Created prompt_versions table with indexes")

        self.mark_applied(session)
```

### Migration 004: Create Review Tables

**Version**: `004_create_review_tables`

```python
class CreateReviewTablesMigration(Migration):
    @property
    def version(self) -> str:
        return "004_create_review_tables"

    @property
    def description(self) -> str:
        return "Create review_tasks and review_decisions tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        # Create review_tasks table
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_tasks'")
        )
        if not result.fetchone():
            session.execute(text("""
                CREATE TABLE review_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    artifact_paths TEXT,
                    diff_path TEXT,
                    prompt_version_id INTEGER,
                    created_at TIMESTAMP NOT NULL,
                    reviewed_at TIMESTAMP,
                    reviewer_notes TEXT,
                    artifact_hash TEXT,
                    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
                )
            """))
            session.execute(
                text("CREATE INDEX idx_review_tasks_episode_stage ON review_tasks(episode_id, stage)")
            )
            session.execute(
                text("CREATE INDEX idx_review_tasks_status ON review_tasks(status)")
            )
            session.commit()
            logger.info("Created review_tasks table with indexes")

        # Create review_decisions table
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_decisions'")
        )
        if not result.fetchone():
            session.execute(text("""
                CREATE TABLE review_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_task_id INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    notes TEXT,
                    decided_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
                )
            """))
            session.execute(
                text("CREATE INDEX idx_review_decisions_task ON review_decisions(review_task_id)")
            )
            session.commit()
            logger.info("Created review_decisions table with index")

        self.mark_applied(session)
```

---

## 5. New Models

### 5.1 `btcedu/models/prompt_version.py`

```python
"""PromptVersion ORM model for tracking prompt template versions."""

import enum
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PromptVersion(Base):
    """Tracks versions of prompt templates with content hashes."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_name_version"),
        UniqueConstraint("name", "content_hash", name="uq_prompt_name_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PromptVersion(name='{self.name}', version={self.version}, "
            f"is_default={self.is_default})>"
        )
```

### 5.2 `btcedu/models/review.py`

```python
"""Review ORM models for human review workflow."""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class ReviewTask(Base):
    """A review task created when pipeline reaches a review gate."""

    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ReviewStatus.PENDING.value)
    artifact_paths: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    diff_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prompt_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("prompt_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    decisions: Mapped[list["ReviewDecision"]] = relationship(
        back_populates="review_task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewTask(id={self.id}, episode_id='{self.episode_id}', "
            f"stage='{self.stage}', status='{self.status}')>"
        )


class ReviewDecision(Base):
    """Audit trail entry for a review decision."""

    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("review_tasks.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    review_task: Mapped["ReviewTask"] = relationship(back_populates="decisions")

    def __repr__(self) -> str:
        return (
            f"<ReviewDecision(id={self.id}, review_task_id={self.review_task_id}, "
            f"decision='{self.decision}')>"
        )
```

---

## 6. EpisodeStatus & PipelineStage Enum Extensions

### `btcedu/models/episode.py` — EpisodeStatus

Add after `FAILED`:

```python
class EpisodeStatus(str, enum.Enum):
    # Existing (v1 pipeline)
    NEW = "new"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    CHUNKED = "chunked"              # v1 only
    GENERATED = "generated"          # v1 only
    REFINED = "refined"              # v1 only
    COMPLETED = "completed"
    FAILED = "failed"
    # New (v2 pipeline)
    CORRECTED = "corrected"
    TRANSLATED = "translated"
    ADAPTED = "adapted"
    CHAPTERIZED = "chapterized"
    IMAGES_GENERATED = "images_generated"
    TTS_DONE = "tts_done"
    RENDERED = "rendered"
    APPROVED = "approved"
    PUBLISHED = "published"
    COST_LIMIT = "cost_limit"
```

### `btcedu/models/episode.py` — PipelineStage

Add new stages after `COMPLETE`:

```python
class PipelineStage(str, enum.Enum):
    # Existing (v1)
    DETECT = "detect"
    DOWNLOAD = "download"
    TRANSCRIBE = "transcribe"
    CHUNK = "chunk"
    GENERATE = "generate"
    REFINE = "refine"
    COMPLETE = "complete"
    # New (v2)
    CORRECT = "correct"
    TRANSLATE = "translate"
    ADAPT = "adapt"
    CHAPTERIZE = "chapterize"
    IMAGEGEN = "imagegen"
    TTS = "tts"
    RENDER = "render"
    REVIEW = "review"
    PUBLISH = "publish"
```

### `btcedu/models/episode.py` — New Columns on Episode

Add after `retry_count`:

```python
    # v2 pipeline fields
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

### `btcedu/core/pipeline.py` — _STATUS_ORDER update

Add new statuses to `_STATUS_ORDER` for forward compatibility. `[ASSUMPTION]` Existing pipeline logic only references v1 statuses and is unaffected.

```python
_STATUS_ORDER = {
    EpisodeStatus.NEW: 0,
    EpisodeStatus.DOWNLOADED: 1,
    EpisodeStatus.TRANSCRIBED: 2,
    EpisodeStatus.CHUNKED: 3,
    EpisodeStatus.GENERATED: 4,
    EpisodeStatus.REFINED: 5,
    EpisodeStatus.COMPLETED: 6,
    EpisodeStatus.FAILED: -1,
    # v2 pipeline statuses
    EpisodeStatus.CORRECTED: 10,
    EpisodeStatus.TRANSLATED: 11,
    EpisodeStatus.ADAPTED: 12,
    EpisodeStatus.CHAPTERIZED: 13,
    EpisodeStatus.IMAGES_GENERATED: 14,
    EpisodeStatus.TTS_DONE: 15,
    EpisodeStatus.RENDERED: 16,
    EpisodeStatus.APPROVED: 17,
    EpisodeStatus.PUBLISHED: 18,
    EpisodeStatus.COST_LIMIT: -2,
}
```

---

## 7. PromptRegistry Skeleton

### `btcedu/core/prompt_registry.py`

```python
"""Prompt template registry for versioned, hashed prompt management."""

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from btcedu.models.prompt_version import PromptVersion

logger = logging.getLogger(__name__)

# Default templates directory (relative to project root)
TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates"


class PromptRegistry:
    """Manages prompt template versions with content hashing.

    Loads Markdown templates with YAML frontmatter, computes SHA-256
    content hashes, and tracks versions in the database.
    """

    def __init__(self, session: Session, templates_dir: Path | None = None):
        self._session = session
        self._templates_dir = templates_dir or TEMPLATES_DIR

    def get_default(self, name: str) -> PromptVersion | None:
        """Get the current default PromptVersion for a given prompt name.

        Returns None if no version exists or none is marked as default.
        """
        return (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name, PromptVersion.is_default.is_(True))
            .first()
        )

    def register_version(
        self,
        name: str,
        template_path: str | Path,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        notes: str | None = None,
        set_default: bool = False,
    ) -> PromptVersion:
        """Register a new prompt version from a template file.

        Reads the template, parses YAML frontmatter for metadata,
        computes a SHA-256 content hash, and creates a new PromptVersion
        record. If the content hash already exists for this name, returns
        the existing version instead.

        If set_default=True, promotes this version to default.
        Frontmatter metadata (model, temperature, max_tokens) is used
        as fallback when those parameters are not explicitly provided.
        """
        ...

    def promote_to_default(self, version_id: int) -> None:
        """Promote a specific PromptVersion to be the default for its name.

        Demotes the current default (if any) and sets the given version
        as the new default. Raises ValueError if version_id not found.
        """
        ...

    def get_history(self, name: str) -> list[PromptVersion]:
        """Get all versions of a prompt, ordered by version number descending.

        Returns an empty list if no versions exist for the given name.
        """
        return (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name)
            .order_by(PromptVersion.version.desc())
            .all()
        )

    def compute_hash(self, content: str) -> str:
        """Compute SHA-256 hash of prompt content (after stripping frontmatter).

        Strips YAML frontmatter (delimited by ---) before hashing so that
        metadata changes don't invalidate the content hash.
        """
        body = self._strip_frontmatter(content)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def load_template(self, template_path: str | Path) -> tuple[dict, str]:
        """Load a template file and parse its YAML frontmatter.

        Returns (metadata_dict, body_content). The metadata dict contains
        fields from the YAML frontmatter (name, model, temperature, etc.).
        The body is the template content after the frontmatter.
        """
        ...

    def _next_version(self, name: str) -> int:
        """Get the next version number for a prompt name."""
        latest = (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name)
            .order_by(PromptVersion.version.desc())
            .first()
        )
        return (latest.version + 1) if latest else 1

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter delimited by --- from content."""
        pattern = r"^---\s*\n.*?\n---\s*\n"
        return re.sub(pattern, "", content, count=1, flags=re.DOTALL).strip()
```

---

## 8. Config Changes

### `btcedu/config.py` — New Fields

Add to `Settings` class, in a new section after "Content Generation":

```python
    # Pipeline Version Control
    pipeline_version: int = 1  # 1 = legacy (chunk→generate→refine), 2 = v2 pipeline
    max_episode_cost_usd: float = 10.0  # per-episode cost safety cap
```

`[ASSUMPTION]` These fields are read from `.env` as `PIPELINE_VERSION` and `MAX_EPISODE_COST_USD`. They have safe defaults so no `.env` changes are required for existing deployments.

---

## 9. Prompt Template: `btcedu/prompts/templates/system.md`

```markdown
---
name: system
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 4096
description: Shared Turkish system prompt with hard constraints for all content generation
author: content_owner
---

Sen bir Bitcoin egitim icerigi uzmanisin. Turkce egitim videolari icin icerik uretiyorsun.
Kaynak: "Der Bitcoin Podcast" - Florian Bruce Boye (Almanca).

## ZORUNLU KURALLAR

1. **YALNIZCA KAYNAK KULLAN**: Yanitini YALNIZCA saglanan transkript parcalarina dayandir. Dis bilgi KULLANMA. Kendi bilgini EKLEME.
2. **ALINTI ZORUNLU**: Her bolum ve her onemli iddia icin kaynak belirt. Kaynak formati: [EPISODEID_C####] (#### = chunk sira numarasi, sifir dolgulu, ornek: [ep001_C0003]).
3. **KAYNAK YOKSA**: Eger saglanan kaynaklarda bilgi yoksa, "Kaynaklarda yok" yaz. Bilgi UYDURMA. Tahmin YAPMA.
4. **INTIHAL YASAK**: Kaynaklari ozetle ve yorumla. Uzun kelimesi kelimesine kopyalama yapma. Kendi cumlelerin ile ifade et.
5. **FINANSAL TAVSIYE YASAK**: Fiyat tahmini yapma, alim/satim dili kullanma, yatirim tavsiyesi verme.
6. **DIL**: Turkce yaz. Teknik terimlerin Almanca/Ingilizce karsiligini parantez icinde belirt. Ornek: "Madencilik (Mining / Bergbau)"

## YASAL UYARI
Her ciktinin sonuna su uyariyi ekle:
"Bu icerik yalnizca egitim amaclidir. Yatirim tavsiyesi degildir. Finansal kararlariniz icin profesyonel danismanlik aliniz."
```

---

## 10. Models `__init__.py` Update

```python
from btcedu.models.channel import Channel  # noqa: F401
from btcedu.models.content_artifact import ContentArtifact  # noqa: F401
from btcedu.models.episode import Chunk, Episode, PipelineRun  # noqa: F401
from btcedu.models.prompt_version import PromptVersion  # noqa: F401
from btcedu.models.review import ReviewDecision, ReviewTask  # noqa: F401
```

---

## 11. Test Plan

### `tests/test_sprint1_models.py`

| Test Function | Asserts |
|---|---|
| `TestPromptVersionORM::test_create_prompt_version` | Can create and query a `PromptVersion` with all fields |
| `TestPromptVersionORM::test_unique_name_version` | Inserting duplicate `(name, version)` raises `IntegrityError` |
| `TestPromptVersionORM::test_unique_name_hash` | Inserting duplicate `(name, content_hash)` raises `IntegrityError` |
| `TestPromptVersionORM::test_default_flag` | Can set `is_default=True` and query by it |
| `TestReviewTaskORM::test_create_review_task` | Can create and query a `ReviewTask` with all fields |
| `TestReviewTaskORM::test_review_task_defaults` | Default status is `"pending"`, timestamps work |
| `TestReviewTaskORM::test_review_decision_relationship` | Creating `ReviewDecision` linked to a `ReviewTask` works; cascade delete works |
| `TestReviewDecisionORM::test_create_review_decision` | Can create a standalone decision and query it |
| `TestEpisodeV2Fields::test_pipeline_version_default` | New episodes get `pipeline_version=1` by default |
| `TestEpisodeV2Fields::test_new_status_values` | Can set episode status to each new v2 status (`CORRECTED`, `TRANSLATED`, etc.) |
| `TestEpisodeV2Fields::test_pipeline_stage_new_values` | All new `PipelineStage` values exist and are accessible |

### `tests/test_sprint1_migrations.py`

| Test Function | Asserts |
|---|---|
| `test_migration_002_adds_columns` | After running migration 002, `pipeline_version`, `review_status`, `youtube_video_id` columns exist on `episodes`; existing episodes get `pipeline_version=1` |
| `test_migration_002_idempotent` | Running migration 002 twice does not error |
| `test_migration_003_creates_prompt_versions` | After running migration 003, `prompt_versions` table exists with correct columns and indexes |
| `test_migration_003_idempotent` | Running migration 003 twice does not error |
| `test_migration_004_creates_review_tables` | After running migration 004, `review_tasks` and `review_decisions` tables exist with correct columns, indexes, and foreign keys |
| `test_migration_004_idempotent` | Running migration 004 twice does not error |
| `test_all_migrations_run_sequentially` | Running `run_migrations()` applies all 4 migrations (001-004) in order on a fresh old-schema DB |
| `test_existing_pipeline_works_after_migrations` | After all migrations, existing v1 episodes can still be queried and updated via ORM |

### `tests/test_prompt_registry.py`

| Test Function | Asserts |
|---|---|
| `TestPromptRegistry::test_compute_hash_deterministic` | Same content always produces same hash |
| `TestPromptRegistry::test_compute_hash_strips_frontmatter` | Changing frontmatter metadata doesn't change the body hash |
| `TestPromptRegistry::test_compute_hash_different_content` | Different body content produces different hash |
| `TestPromptRegistry::test_load_template_parses_frontmatter` | YAML frontmatter is parsed into dict with correct keys |
| `TestPromptRegistry::test_load_template_returns_body` | Body content is returned without frontmatter |
| `TestPromptRegistry::test_register_version_creates_record` | Registering a new template creates a `PromptVersion` in DB |
| `TestPromptRegistry::test_register_version_auto_increments` | Registering same name twice increments version number |
| `TestPromptRegistry::test_register_version_deduplicates_by_hash` | Registering same content twice for same name returns existing version |
| `TestPromptRegistry::test_get_default_returns_default` | After setting a default, `get_default()` returns it |
| `TestPromptRegistry::test_get_default_returns_none` | If no default exists, returns `None` |
| `TestPromptRegistry::test_promote_to_default` | Promoting a version sets `is_default=True` and demotes the old default |
| `TestPromptRegistry::test_get_history_ordered` | History returns versions in descending order |
| `TestPromptRegistry::test_register_with_set_default` | `register_version(set_default=True)` makes the new version the default |
| `TestPromptRegistry::test_load_system_template` | Can load the actual `system.md` template file |

---

## 12. Implementation Order

Execute in this order. Each step should be independently testable.

1. **Extend `EpisodeStatus` and `PipelineStage` enums** in `btcedu/models/episode.py`
   - Add 10 new status values, 9 new stage values
   - Run existing tests to confirm nothing breaks

2. **Add Episode model columns** in `btcedu/models/episode.py`
   - Add `pipeline_version`, `review_status`, `youtube_video_id` mapped columns
   - Run existing tests (they use in-memory DB via `Base.metadata.create_all`, so new columns appear automatically)

3. **Update `_STATUS_ORDER`** in `btcedu/core/pipeline.py`
   - Add all new statuses to the dict
   - Run existing pipeline tests to confirm v1 behavior unchanged

4. **Add config fields** in `btcedu/config.py`
   - Add `pipeline_version` and `max_episode_cost_usd`
   - Run existing config tests

5. **Create `PromptVersion` model** — new file `btcedu/models/prompt_version.py`

6. **Create `ReviewTask` and `ReviewDecision` models** — new file `btcedu/models/review.py`

7. **Update `btcedu/models/__init__.py`** — add imports for new models

8. **Create migrations 002, 003, 004** in `btcedu/migrations/__init__.py`
   - Follow existing `AddChannelsSupportMigration` pattern exactly
   - Append all three to `MIGRATIONS` list

9. **Create `btcedu/prompts/templates/` directory and `system.md`**
   - Content migrated from `btcedu/prompts/system.py` into Markdown+YAML format

10. **Implement `PromptRegistry`** in `btcedu/core/prompt_registry.py`
    - `compute_hash()`, `load_template()`, `_strip_frontmatter()`, `_next_version()` first (pure functions)
    - Then `register_version()`, `get_default()`, `promote_to_default()`, `get_history()`

11. **Write tests** for new models (`tests/test_sprint1_models.py`)

12. **Write tests** for migrations (`tests/test_sprint1_migrations.py`)

13. **Write tests** for PromptRegistry (`tests/test_prompt_registry.py`)

14. **Run full test suite** (`pytest tests/`) — all tests must pass

15. **Manual verification** — run `btcedu migrate` on a dev copy of the database; run `btcedu status` to confirm v1 pipeline still works

---

## 13. Definition of Done

- [ ] All existing tests pass (`pytest tests/` — 0 failures)
- [ ] `EpisodeStatus` enum has 18 values (8 existing + 10 new)
- [ ] `PipelineStage` enum has 16 values (7 existing + 9 new)
- [ ] `Episode` model has `pipeline_version`, `review_status`, `youtube_video_id` columns
- [ ] `PromptVersion` model exists with all fields from MASTERPLAN §7.3
- [ ] `ReviewTask` model exists with all fields from MASTERPLAN §7.3
- [ ] `ReviewDecision` model exists with all fields from MASTERPLAN §7.3
- [ ] `prompt_versions` table created by migration 003
- [ ] `review_tasks` and `review_decisions` tables created by migration 004
- [ ] All three migrations are idempotent (running twice is safe)
- [ ] `PromptRegistry` can load a template, compute its hash, register a version, get the default, promote a version, and list history
- [ ] `btcedu/prompts/templates/system.md` exists with migrated content from `system.py`
- [ ] Legacy `btcedu/prompts/system.py` is untouched
- [ ] `Settings` has `pipeline_version` (default=1) and `max_episode_cost_usd` (default=10.0)
- [ ] `_STATUS_ORDER` in `pipeline.py` includes all new statuses
- [ ] `btcedu migrate` runs cleanly on existing database
- [ ] `btcedu status` still works after migration
- [ ] New model tests pass (`test_sprint1_models.py`)
- [ ] Migration tests pass (`test_sprint1_migrations.py`)
- [ ] PromptRegistry tests pass (`test_prompt_registry.py`)

---

## 14. Assumptions

- `[ASSUMPTION]` `published_at_youtube` column is deferred to the PUBLISH sprint since it's unused until then.
- `[ASSUMPTION]` The `artifact_paths` field on `ReviewTask` stores a JSON-encoded list of strings. We use `Text` column type and handle JSON serialization in application code (matches existing pattern in the codebase where JSON is stored as TEXT).
- `[ASSUMPTION]` The `status` field on `ReviewTask` is stored as plain `TEXT` (not as SQLAlchemy `Enum`) to avoid schema changes when adding new review statuses later. The `ReviewStatus` Python enum validates values in application code.
- `[ASSUMPTION]` YAML frontmatter parsing uses `yaml.safe_load()`. The `pyyaml` package is assumed to be available (it's a common dependency of Pydantic/FastAPI stacks).
- `[ASSUMPTION]` The `_STATUS_ORDER` values for v2 stages (10-18) are chosen to be higher than v1 values (0-6) to maintain ordering semantics, but no v2 pipeline logic depends on these values in Sprint 1.
- `[ASSUMPTION]` No `.env` file changes are required for Sprint 1 — both new config fields have safe defaults.
