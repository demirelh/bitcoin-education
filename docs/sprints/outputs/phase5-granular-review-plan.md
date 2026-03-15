# Phase 5: Granular Review Actions — Implementation Plan

## Section 1: Design Decision — Data Model

**Selected Approach: B — New table `review_item_decisions` (normalized, queryable)**

**Justification:**

Option A (extending `ReviewDecision` with per-item JSON payload) conflates two distinct concepts: a whole-review decision (approved/rejected/changes_requested) is semantically different from a per-item action (accept/reject/edit one diff change). Mixing them into a single row with a nullable JSON field creates ambiguous semantics — `decision` would sometimes mean "approved" (whole-review) and sometimes "accepted" (single item), with no clean distinction at the DB layer. Queries like "how many items did the reviewer accept?" require JSON parsing in application code.

Option C (JSON blob on `ReviewTask`) is worse: it co-locates mutable item-action state with the immutable task record, grows unboundedly with diff size, provides no indexing, and requires full-table reads plus application-side parsing for any query. It also breaks the existing cascade-delete guarantee.

Option B is the right choice because:
- It follows the existing normalization pattern: `ReviewTask` → `ReviewDecision` (one-to-many) extended with `ReviewTask` → `ReviewItemDecision` (one-to-many)
- Each row is independently queryable: count accepted items per task, find all edited items, audit when a decision was made — all without JSON parsing
- The `(review_task_id, item_id)` composite index makes upsert and lookup O(log n)
- Cascade delete is automatic via SQLAlchemy relationship, consistent with how `ReviewDecision` already works
- The model is easily extensible (add `reviewer_id`, `time_spent_ms`, etc. in future phases)
- It stays entirely within the `btcedu.db.Base` metadata, unlike `MediaAsset`'s isolated base — no test-setup complexity

---

## Section 2: New Model — ReviewItemDecision

**File to create:** `btcedu/models/review_item.py`

```python
"""Per-item review decisions for granular diff review (Phase 5)."""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewItemAction(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"
    UNCHANGED = "unchanged"


class ReviewItemDecision(Base):
    """Per-item decision for a single change in a correction or adaptation diff."""

    __tablename__ = "review_item_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("review_tasks.id"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReviewItemAction.PENDING.value
    )
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    review_task: Mapped["ReviewTask"] = relationship(back_populates="item_decisions")  # type: ignore[name-defined]

    __table_args__ = (
        Index("idx_review_item_decisions_task", "review_task_id"),
        Index("idx_review_item_decisions_task_item", "review_task_id", "item_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewItemDecision(id={self.id}, review_task_id={self.review_task_id}, "
            f"item_id='{self.item_id}', action='{self.action}')>"
        )
```

**Modification to `btcedu/models/review.py` — add relationship to ReviewTask:**

Add import at top of file:
```python
# Add to imports section (use TYPE_CHECKING to avoid circular import at runtime)
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from btcedu.models.review_item import ReviewItemDecision
```

Add relationship field inside `ReviewTask` class, after the `decisions` relationship:
```python
    item_decisions: Mapped[list["ReviewItemDecision"]] = relationship(
        "ReviewItemDecision",
        back_populates="review_task",
        cascade="all, delete-orphan",
    )
```

**Note on import strategy:** To avoid circular imports, `ReviewItemDecision` imports `Base` from `btcedu.db` (same as `ReviewTask`). The back-reference in `review_item.py` uses a forward string reference `"ReviewTask"`. The relationship in `review.py` uses a string class name `"ReviewItemDecision"` and a `TYPE_CHECKING` guard for type hints only.

---

## Section 3: Diff Artifact Contract Evolution

### correction_diff.json — add `item_id`

Each entry in the `changes` array gains an `item_id` field. The format is `corr-{index:04d}`, generated from the array position (0-based).

**Updated schema:**

```json
{
  "episode_id": "SJFLLZxlWqk",
  "original_length": 65101,
  "corrected_length": 65135,
  "changes": [
    {
      "item_id": "corr-0000",
      "type": "replace",
      "original": "original text span",
      "corrected": "corrected text span",
      "context": "...surrounding context...",
      "position": { "start_word": 10, "end_word": 12 },
      "category": "auto"
    },
    {
      "item_id": "corr-0042",
      "type": "insert",
      "original": "",
      "corrected": "inserted word",
      "context": "...surrounding context...",
      "position": { "start_word": 210, "end_word": 210 },
      "category": "auto"
    }
  ],
  "summary": {
    "total_changes": 82,
    "by_type": { "replace": 75, "insert": 3, "delete": 4 }
  }
}
```

**Change to `compute_correction_diff()` in `btcedu/core/corrector.py`:**

In the loop body where `change` dict is assembled, add `"item_id": f"corr-{len(changes):04d}"` before appending to `changes`. Since `item_id` is derived from the list index at time of append, it is deterministic and stable as long as the diff algorithm output order does not change (SequenceMatcher opcodes are always ordered by position in the original sequence).

```python
change = {
    "item_id": f"corr-{len(changes):04d}",   # ← add this line
    "type": tag,
    "original": orig_span,
    "corrected": corr_span,
    "context": f"...{context}...",
    "position": {"start_word": i1, "end_word": i2},
    "category": "auto",
}
changes.append(change)
```

### adaptation_diff.json — add `item_id`

Each entry in the `adaptations` array gains an `item_id` field. Format: `adap-{index:04d}`.

**Updated schema:**

```json
{
  "episode_id": "SJFLLZxlWqk",
  "original_length": 56011,
  "adapted_length": 25794,
  "adaptations": [
    {
      "item_id": "adap-0000",
      "tier": "T1",
      "category": "tone_adjustment",
      "original": "original Turkish text",
      "adapted": "culturally adapted text",
      "context": "...50 chars before/after...",
      "position": { "start": 12163, "end": 12183 }
    },
    {
      "item_id": "adap-0013",
      "tier": "T2",
      "category": "cultural_reference",
      "original": "German reference",
      "adapted": "Turkish equivalent",
      "context": "...surrounding context...",
      "position": { "start": 45000, "end": 45020 }
    }
  ],
  "summary": {
    "total_adaptations": 14,
    "tier1_count": 11,
    "tier2_count": 3,
    "by_category": { "tone_adjustment": 8, "cultural_reference": 6 }
  }
}
```

**Change to `compute_adaptation_diff()` in `btcedu/core/adapter.py`:**

In the loop body where adaptation dict is assembled, add `"item_id": f"adap-{len(adaptations):04d}"` before appending:

```python
adaptations.append(
    {
        "item_id": f"adap-{len(adaptations):04d}",   # ← add this line
        "tier": tier,
        "category": category,
        "original": original_text,
        "adapted": adapted_text,
        "context": context,
        "position": {"start": start, "end": end},
    }
)
```

### Backward compatibility

Old diff files written before Phase 5 will not have `item_id` on their entries. The API layer must handle this gracefully. In `get_review_detail()`, when loading item_decisions: if the diff entry has no `item_id`, generate it on-the-fly from the array index using the same formula. The `get_item_decisions()` function returns an empty dict `{}` for reviews with old-format diffs (no crash, no items to act on). Tests must verify this path explicitly.

---

## Section 4: Migration 007

**File to create:** `btcedu/migrations/007_add_review_item_decisions.py`

```python
"""Migration 007: Create review_item_decisions table for granular diff review."""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from btcedu.migrations import Migration

logger = logging.getLogger(__name__)


class AddReviewItemDecisionsMigration(Migration):
    """Migration 007: Add review_item_decisions table (Phase 5 granular review)."""

    @property
    def version(self) -> str:
        return "007_add_review_item_decisions"

    @property
    def description(self) -> str:
        return "Create review_item_decisions table for per-item diff review actions"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='review_item_decisions'"
            )
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE review_item_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        review_task_id INTEGER NOT NULL,
                        item_id VARCHAR(64) NOT NULL,
                        operation_type VARCHAR(32) NOT NULL,
                        original_text TEXT,
                        proposed_text TEXT,
                        action VARCHAR(32) NOT NULL DEFAULT 'pending',
                        edited_text TEXT,
                        decided_at TIMESTAMP,
                        FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
                    )
                """)
            )
            session.execute(
                text(
                    "CREATE INDEX idx_review_item_decisions_task "
                    "ON review_item_decisions(review_task_id)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX idx_review_item_decisions_task_item "
                    "ON review_item_decisions(review_task_id, item_id)"
                )
            )
            session.commit()
            logger.info("Created review_item_decisions table with indexes")
        else:
            logger.info("review_item_decisions table already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")
```

**Modification to `btcedu/migrations/__init__.py`:**

Import and register Migration 007 at the end of the file:

```python
# At bottom of imports section:
from btcedu.migrations.review_item_decisions import AddReviewItemDecisionsMigration

# In MIGRATIONS list, append:
MIGRATIONS = [
    AddChannelsSupportMigration(),
    AddV2PipelineColumnsMigration(),
    CreatePromptVersionsTableMigration(),
    CreateReviewTablesMigration(),
    CreateMediaAssetsTableMigration(),
    CreatePublishJobsTableMigration(),
    AddReviewItemDecisionsMigration(),   # ← new
]
```

Note: The migration file is placed at `btcedu/migrations/007_add_review_item_decisions.py`. Since Python module names cannot start with a digit, import it using the module name `review_item_decisions_migration` or place the class inside a file named `_007_review_item_decisions.py`. The cleanest approach matching the existing pattern (all migrations are classes inside `__init__.py`) is to add the class directly into `btcedu/migrations/__init__.py` rather than a separate file, consistent with how migrations 001–006 are structured. This avoids the module naming issue entirely.

---

## Section 5: Reviewer Module Changes

**New functions to add to `btcedu/core/reviewer.py`:**

```python
def upsert_item_decision(
    session: Session,
    review_task_id: int,
    item_id: str,
    action: str,
    edited_text: str | None = None,
) -> "ReviewItemDecision":
    """Create or update a per-item decision.

    On first call for a given (review_task_id, item_id): creates record,
    sets decided_at to now.
    On subsequent calls: updates action (and edited_text), updates decided_at.

    Args:
        session: DB session.
        review_task_id: FK to review_tasks.id.
        item_id: Stable item identifier from diff JSON (e.g. "corr-0042").
        action: One of ReviewItemAction values.
        edited_text: Required when action == "edited", else None.

    Returns:
        The created or updated ReviewItemDecision.

    Raises:
        ValueError: If review task not found or not actionable.
    """
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    task = _get_task_or_raise(session, review_task_id)
    _validate_actionable(task)

    # Load existing record
    existing = (
        session.query(ReviewItemDecision)
        .filter(
            ReviewItemDecision.review_task_id == review_task_id,
            ReviewItemDecision.item_id == item_id,
        )
        .first()
    )

    now = _utcnow()

    if existing:
        existing.action = action
        existing.edited_text = edited_text if action == ReviewItemAction.EDITED.value else None
        existing.decided_at = now
        session.commit()
        return existing
    else:
        # Populate original/proposed from diff on first create
        original_text, proposed_text, operation_type = _load_item_texts_from_diff(
            task, item_id
        )
        record = ReviewItemDecision(
            review_task_id=review_task_id,
            item_id=item_id,
            operation_type=operation_type,
            original_text=original_text,
            proposed_text=proposed_text,
            action=action,
            edited_text=edited_text if action == ReviewItemAction.EDITED.value else None,
            decided_at=now,
        )
        session.add(record)
        session.commit()
        return record


def get_item_decisions(
    session: Session,
    review_task_id: int,
) -> dict[str, "ReviewItemDecision"]:
    """Return all item decisions for a review task, keyed by item_id.

    Returns empty dict if no decisions exist yet.
    """
    from btcedu.models.review_item import ReviewItemDecision

    records = (
        session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == review_task_id)
        .all()
    )
    return {r.item_id: r for r in records}


def apply_item_decisions(
    session: Session,
    review_task_id: int,
) -> str:
    """Assemble final reviewed text from per-item decisions and write sidecar file.

    Pending items default to accepting the proposed change.

    Args:
        session: DB session.
        review_task_id: FK to review_tasks.id.

    Returns:
        Absolute path string to the written sidecar file.

    Raises:
        ValueError: If review task not found, diff file missing, or source text missing.
    """
    task = _get_task_or_raise(session, review_task_id)
    item_decisions = get_item_decisions(session, review_task_id)

    if not task.diff_path:
        raise ValueError(f"Review task {review_task_id} has no diff_path")

    diff_file = Path(task.diff_path)
    if not diff_file.exists():
        raise ValueError(f"Diff file not found: {task.diff_path}")

    diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
    settings = _get_runtime_settings()

    if task.stage == "correct":
        # Load original transcript
        episode = session.query(Episode).filter(Episode.episode_id == task.episode_id).first()
        if not episode or not episode.transcript_path:
            raise ValueError(f"Original transcript not found for episode {task.episode_id}")
        original_text = Path(episode.transcript_path).read_text(encoding="utf-8")
        changes = diff_data.get("changes", [])
        # Inject item_ids for old diffs (backward compat)
        _ensure_item_ids_correction(changes)
        reviewed = _assemble_correction_review(original_text, changes, item_decisions)
        out_path = _sidecar_path(task.episode_id, "correct", settings)

    elif task.stage == "adapt":
        # Load adapted script
        adapted_path = Path(settings.outputs_dir) / task.episode_id / "script.adapted.tr.md"
        if not adapted_path.exists():
            raise ValueError(f"Adapted script not found: {adapted_path}")
        adapted_text = adapted_path.read_text(encoding="utf-8")
        adaptations = diff_data.get("adaptations", [])
        _ensure_item_ids_adaptation(adaptations)
        reviewed = _assemble_adaptation_review(adapted_text, adaptations, item_decisions)
        out_path = _sidecar_path(task.episode_id, "adapt", settings)

    else:
        raise ValueError(
            f"apply_item_decisions not supported for stage '{task.stage}'"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(reviewed, encoding="utf-8")
    logger.info("Wrote reviewed sidecar to %s", out_path)
    return str(out_path)


def _sidecar_path(episode_id: str, stage: str, settings) -> Path:
    """Return the sidecar file path for a reviewed artifact.

    correction stage → data/outputs/{ep_id}/review/transcript.reviewed.de.txt
    adapt stage      → data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md
    """
    base = Path(settings.outputs_dir) / episode_id / "review"
    if stage == "correct":
        return base / "transcript.reviewed.de.txt"
    elif stage == "adapt":
        return base / "script.adapted.reviewed.tr.md"
    else:
        raise ValueError(f"No sidecar path defined for stage '{stage}'")


def _load_item_texts_from_diff(
    task: "ReviewTask",
    item_id: str,
) -> tuple[str | None, str | None, str]:
    """Extract original_text, proposed_text, operation_type for a given item_id.

    Returns (original_text, proposed_text, operation_type).
    Falls back to ("", "", "unknown") if item not found.
    """
    if not task.diff_path:
        return (None, None, "unknown")
    diff_file = Path(task.diff_path)
    if not diff_file.exists():
        return (None, None, "unknown")
    try:
        diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return (None, None, "unknown")

    if task.stage == "correct":
        changes = diff_data.get("changes", [])
        _ensure_item_ids_correction(changes)
        for i, c in enumerate(changes):
            if c.get("item_id", f"corr-{i:04d}") == item_id:
                return (c.get("original", ""), c.get("corrected", ""), c.get("type", "replace"))
    elif task.stage == "adapt":
        adaptations = diff_data.get("adaptations", [])
        _ensure_item_ids_adaptation(adaptations)
        for i, a in enumerate(adaptations):
            if a.get("item_id", f"adap-{i:04d}") == item_id:
                return (a.get("original", ""), a.get("adapted", ""), a.get("tier", "T1"))

    return (None, None, "unknown")


def _ensure_item_ids_correction(changes: list[dict]) -> None:
    """Mutate changes in-place to add item_id if missing (backward compat)."""
    for i, c in enumerate(changes):
        if "item_id" not in c:
            c["item_id"] = f"corr-{i:04d}"


def _ensure_item_ids_adaptation(adaptations: list[dict]) -> None:
    """Mutate adaptations in-place to add item_id if missing (backward compat)."""
    for i, a in enumerate(adaptations):
        if "item_id" not in a:
            a["item_id"] = f"adap-{i:04d}"


def _assemble_correction_review(
    original_text: str,
    diff_changes: list[dict],
    item_decisions: dict[str, "ReviewItemDecision"],
) -> str:
    """Reconstruct reviewed transcript from original text + per-item decisions.

    Algorithm (word-level reconstruction):
    1. Tokenize original_text into words by splitting on whitespace.
    2. Sort diff_changes by position.start_word ascending.
    3. Walk through changes + inter-change gaps in order:
       - Gap words (not covered by any change): emit as-is.
       - For each change:
         - action=accepted or action=pending (default): emit proposed (corrected) text
         - action=rejected or action=unchanged: emit original words
         - action=edited: emit edited_text
    4. Join output with single space, preserve paragraph breaks.

    Note: This is a word-level reconstruction. Whitespace normalization is
    acceptable since the original Whisper transcript is also whitespace-normalized.
    Paragraph structure (double newlines) is preserved at word boundaries.
    """
    from btcedu.models.review_item import ReviewItemAction

    orig_words = original_text.split()
    sorted_changes = sorted(diff_changes, key=lambda c: c["position"]["start_word"])

    output_tokens: list[str] = []
    cursor = 0  # current position in orig_words

    for change in sorted_changes:
        start_word = change["position"]["start_word"]
        end_word = change["position"]["end_word"]
        item_id = change.get("item_id", "")
        decision = item_decisions.get(item_id)
        action = decision.action if decision else ReviewItemAction.PENDING.value

        # Emit gap words between last change and this one
        if cursor < start_word:
            output_tokens.extend(orig_words[cursor:start_word])

        # Emit the change based on action
        if action in (ReviewItemAction.ACCEPTED.value, ReviewItemAction.PENDING.value):
            proposed = change.get("corrected", "")
            if proposed:
                output_tokens.extend(proposed.split())
        elif action in (ReviewItemAction.REJECTED.value, ReviewItemAction.UNCHANGED.value):
            output_tokens.extend(orig_words[start_word:end_word])
        elif action == ReviewItemAction.EDITED.value:
            edited = (decision.edited_text or change.get("corrected", "")) if decision else change.get("corrected", "")
            if edited:
                output_tokens.extend(edited.split())

        # For "insert" type changes, end_word == start_word (no original words consumed)
        cursor = end_word

    # Emit remaining words after last change
    if cursor < len(orig_words):
        output_tokens.extend(orig_words[cursor:])

    return " ".join(output_tokens)


def _assemble_adaptation_review(
    adapted_text: str,
    diff_adaptations: list[dict],
    item_decisions: dict[str, "ReviewItemDecision"],
) -> str:
    """Reconstruct reviewed adaptation from adapted text + per-item decisions.

    Algorithm (character-level, reverse-order splicing):
    1. Sort adaptations by position.start DESCENDING (so splicing doesn't
       shift earlier positions).
    2. For each adaptation:
       - Extract original and adapted spans.
       - Based on action, determine replacement text:
         - accepted/pending: keep adapted text (no change — it's already in adapted_text)
         - rejected/unchanged: replace adapted span with original text
         - edited: replace adapted span with edited_text
    3. Splice in reverse order to preserve character positions.

    Note: The adapted_text contains [T1: ...] and [T2: ...] marker tags at the
    positions recorded in diff_adaptations. The positions refer to character
    positions in adapted_text. Accepted/pending actions keep the existing
    tag-annotated text as-is. Rejected actions revert to original Turkish.
    Edited actions substitute reviewer text.
    """
    from btcedu.models.review_item import ReviewItemAction

    sorted_adaptations = sorted(
        diff_adaptations,
        key=lambda a: a["position"]["start"],
        reverse=True,  # process end-first to preserve earlier positions
    )

    result = adapted_text

    for adaptation in sorted_adaptations:
        start = adaptation["position"]["start"]
        end = adaptation["position"]["end"]
        item_id = adaptation.get("item_id", "")
        decision = item_decisions.get(item_id)
        action = decision.action if decision else ReviewItemAction.PENDING.value

        if action in (ReviewItemAction.ACCEPTED.value, ReviewItemAction.PENDING.value):
            # Keep the existing adapted text (marker tag remains)
            continue
        elif action in (ReviewItemAction.REJECTED.value, ReviewItemAction.UNCHANGED.value):
            # Revert to original Turkish text (replace marker tag with original)
            replacement = adaptation.get("original", "")
            result = result[:start] + replacement + result[end:]
        elif action == ReviewItemAction.EDITED.value:
            edited = (decision.edited_text or adaptation.get("adapted", "")) if decision else adaptation.get("adapted", "")
            result = result[:start] + edited + result[end:]

    return result
```

**Update `get_review_detail()` to include item_decisions:**

At the end of the function, before building the return dict, add:

```python
    # Load per-item decisions (Phase 5)
    item_decisions_map = {}
    if task.stage in ("correct", "adapt"):
        from btcedu.models.review_item import ReviewItemDecision
        records = (
            session.query(ReviewItemDecision)
            .filter(ReviewItemDecision.review_task_id == task.id)
            .all()
        )
        item_decisions_map = {
            r.item_id: {
                "action": r.action,
                "edited_text": r.edited_text,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
            for r in records
        }
```

And add `"item_decisions": item_decisions_map` to the returned dict.

---

## Section 6: API Endpoints

**Add to `btcedu/web/api.py`:**

```python
# ---------------------------------------------------------------------------
# Granular review item actions (Phase 5)
# ---------------------------------------------------------------------------

def _get_review_task_or_404(session, review_id: int):
    """Helper: fetch ReviewTask or return 404 response tuple."""
    from btcedu.models.review import ReviewTask
    task = session.query(ReviewTask).filter(ReviewTask.id == review_id).first()
    if not task:
        return None, (jsonify({"error": f"Review not found: {review_id}"}), 404)
    return task, None


def _check_review_actionable(task) -> tuple[bool, any]:
    """Return (is_ok, error_response_or_None)."""
    from btcedu.models.review import ReviewStatus
    if task.status not in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value):
        return False, (
            jsonify({"error": f"Review {task.id} is '{task.status}', must be pending or in_review"}),
            400,
        )
    return True, None


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/accept", methods=["POST"])
def accept_review_item(review_id: int, item_id: str):
    """Accept a single diff item."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.ACCEPTED.value)
        return jsonify({"success": True, "item_id": item_id, "action": "accepted"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/reject", methods=["POST"])
def reject_review_item(review_id: int, item_id: str):
    """Reject a single diff item (revert to original)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.REJECTED.value)
        return jsonify({"success": True, "item_id": item_id, "action": "rejected"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/edit", methods=["POST"])
def edit_review_item(review_id: int, item_id: str):
    """Set reviewer-provided replacement text for a single diff item."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        body = request.get_json(silent=True) or {}
        text_value = body.get("text", "").strip()
        if not text_value:
            return jsonify({"error": "Request body must include non-empty 'text' field"}), 400

        upsert_item_decision(
            session, review_id, item_id, ReviewItemAction.EDITED.value, edited_text=text_value
        )
        return jsonify({
            "success": True,
            "item_id": item_id,
            "action": "edited",
            "edited_text": text_value,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/reset", methods=["POST"])
def reset_review_item(review_id: int, item_id: str):
    """Reset a diff item back to pending (undo any action)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.PENDING.value)
        return jsonify({"success": True, "item_id": item_id, "action": "pending"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/apply", methods=["POST"])
def apply_review_items(review_id: int):
    """Assemble and write the reviewed sidecar file from per-item decisions.

    Does NOT approve the review. Returns pending_count so UI can warn reviewer.
    """
    session = _get_session()
    try:
        from btcedu.core.reviewer import apply_item_decisions, get_item_decisions
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        # Require at least some item decisions to exist
        decisions = get_item_decisions(session, review_id)
        if not decisions:
            return jsonify({"error": "No item decisions found. Act on at least one item first."}), 400

        try:
            reviewed_file = apply_item_decisions(session, review_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # Count pending items among all diff items
        if task.diff_path:
            import json as _json
            from pathlib import Path as _Path
            diff_data = {}
            try:
                diff_data = _json.loads(_Path(task.diff_path).read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                pass
            all_items = diff_data.get("changes", diff_data.get("adaptations", []))
            total_items = len(all_items)
        else:
            total_items = 0

        pending_count = sum(
            1 for item_id, d in decisions.items()
            if d.action == ReviewItemAction.PENDING.value
        )
        # Items with no decision record at all are also pending
        pending_count += total_items - len(decisions)

        return jsonify({
            "success": True,
            "reviewed_file": reviewed_file,
            "pending_count": max(0, pending_count),
            "total_items": total_items,
        })
    finally:
        session.close()
```

---

## Section 7: UI Changes

### Per-item action bar

Each `.diff-change` element gets a `.diff-item-actions` div inserted after `.diff-context`. The actions bar is only rendered when `isActionable` is true (review status is `pending` or `in_review`).

**Updated `renderDiffViewer` signature:**

```javascript
function renderDiffViewer(diff, originalText, correctedText, itemDecisions) {
  // itemDecisions: object keyed by item_id, from GET /reviews/<id> response
  // e.g. { "corr-0000": { action: "accepted", edited_text: null, decided_at: "..." } }
  itemDecisions = itemDecisions || {};
  // ...
}
```

**Pass from `selectReview()`:**

```javascript
// In selectReview(), where renderDiffViewer is called:
if (data.diff) {
  html += renderDiffViewer(data.diff, data.original_text, data.corrected_text, data.item_decisions || {});
}
```

Also store `data.status` for the review panel so item action bar visibility can check it:

```javascript
// Near top of selectReview, after data is fetched:
const isActionable = (data.status === "pending" || data.status === "in_review");
```

Pass `isActionable` into `renderDiffViewer` as a 5th argument or bind it in closure.

**Per-item rendering in `renderDiffViewer`:**

For each change/adaptation entry in the `changes.forEach` loop, after the existing `diff-context` span, add:

```javascript
// Determine item_id (new format or backward-compat generated from index)
const itemId = c.item_id || (isAdaptation ? `adap-${String(idx).padStart(4, "0")}` : `corr-${String(idx).padStart(4, "0")}`);

// Get current action state
const decision = itemDecisions[itemId] || { action: "pending", edited_text: null };
const currentAction = decision.action;

// Build action state CSS class on the .diff-change container
// (applied as data attribute or extra class)
const actionClass = currentAction !== "pending" ? ` diff-item-${currentAction}` : "";

// Per-item action bar (only when review is actionable)
let actionsHtml = "";
if (isActionable) {
  actionsHtml = `<div class="diff-item-actions" data-item-id="${esc(itemId)}" data-review-id="${reviewId}">
    <button class="diff-item-btn accept ${currentAction === "accepted" ? "active" : ""}"
      onclick="itemAction(${reviewId}, '${esc(itemId)}', 'accept')">&#10003; Accept</button>
    <button class="diff-item-btn reject ${currentAction === "rejected" ? "active" : ""}"
      onclick="itemAction(${reviewId}, '${esc(itemId)}', 'reject')">&#10007; Reject</button>
    <button class="diff-item-btn edit ${currentAction === "edited" ? "active" : ""}"
      onclick="toggleEditInline(${reviewId}, '${esc(itemId)}')">&#9998; Edit</button>
    <button class="diff-item-btn reset ${currentAction === "pending" ? "active" : ""}"
      onclick="itemAction(${reviewId}, '${esc(itemId)}', 'reset')">&#9675; Reset</button>
  </div>`;

  // Inline edit panel (hidden by default, toggled by Edit button)
  const prefill = esc(decision.edited_text || (isAdaptation ? (c.adapted || "") : (c.corrected || "")));
  actionsHtml += `<div class="diff-edit-inline" id="edit-inline-${esc(itemId)}" style="display:none">
    <textarea class="diff-edit-textarea" id="edit-text-${esc(itemId)}">${prefill}</textarea>
    <div class="diff-edit-actions">
      <button class="btn btn-sm btn-primary" onclick="saveEditInline(${reviewId}, '${esc(itemId)}')">Save</button>
      <button class="btn btn-sm" onclick="cancelEditInline('${esc(itemId)}')">Cancel</button>
    </div>
  </div>`;
}
```

The `.diff-change` div opening tag must include the `actionClass`:

```javascript
html += `<div class="diff-change ${typeClass}${actionClass}" data-item-id="${esc(itemId)}">`;
```

### Dynamic summary counts bar

Below the existing `.diff-summary` bar, add a `.diff-item-summary` row that shows counts of each action state. This is rendered from `itemDecisions` and the total `changes.length`:

```javascript
function renderItemSummary(changes, itemDecisions, isAdaptation) {
  const counts = { accepted: 0, rejected: 0, edited: 0, unchanged: 0, pending: 0 };
  changes.forEach((c, idx) => {
    const itemId = c.item_id || (isAdaptation
      ? `adap-${String(idx).padStart(4, "0")}`
      : `corr-${String(idx).padStart(4, "0")}`);
    const action = (itemDecisions[itemId] || {}).action || "pending";
    counts[action] = (counts[action] || 0) + 1;
  });
  return `<div class="diff-item-summary" id="diff-item-summary">
    <span class="dim-count accepted">&#10003; ${counts.accepted} accepted</span>
    <span class="dim-count rejected">&#10007; ${counts.rejected} rejected</span>
    <span class="dim-count edited">&#9998; ${counts.edited} edited</span>
    <span class="dim-count unchanged">&mdash; ${counts.unchanged} unchanged</span>
    <span class="dim-count pending">&#8943; ${counts.pending} pending</span>
  </div>`;
}
```

Call this function and insert its output immediately after the `.diff-summary` div.

### New global JS functions

```javascript
async function itemAction(reviewId, itemId, action) {
  // action: "accept" | "reject" | "reset"
  const r = await POST(`/reviews/${reviewId}/items/${itemId}/${action}`);
  if (r.error) {
    toast(r.error, false);
  } else {
    // Optimistically update the item's visual state
    _updateItemVisual(itemId, r.action);
    _updateItemSummary(reviewId);
  }
}
window.itemAction = itemAction;

function toggleEditInline(reviewId, itemId) {
  const el = document.getElementById(`edit-inline-${itemId}`);
  if (el) el.style.display = el.style.display === "none" ? "block" : "none";
}
window.toggleEditInline = toggleEditInline;

async function saveEditInline(reviewId, itemId) {
  const textarea = document.getElementById(`edit-text-${itemId}`);
  if (!textarea) return;
  const text = textarea.value.trim();
  if (!text) { toast("Edited text cannot be empty", false); return; }
  const r = await POST(`/reviews/${reviewId}/items/${itemId}/edit`, { text });
  if (r.error) {
    toast(r.error, false);
  } else {
    _updateItemVisual(itemId, "edited");
    _updateItemSummary(reviewId);
    document.getElementById(`edit-inline-${itemId}`).style.display = "none";
    toast("Edit saved");
  }
}
window.saveEditInline = saveEditInline;

function cancelEditInline(itemId) {
  const el = document.getElementById(`edit-inline-${itemId}`);
  if (el) el.style.display = "none";
}
window.cancelEditInline = cancelEditInline;

function _updateItemVisual(itemId, action) {
  // Find the .diff-change element with this itemId and update its classes/button states
  const container = document.querySelector(`[data-item-id="${itemId}"].diff-change`);
  if (!container) return;
  const actionClasses = ["diff-item-accepted", "diff-item-rejected", "diff-item-edited",
                         "diff-item-unchanged"];
  actionClasses.forEach(cls => container.classList.remove(cls));
  if (action && action !== "pending") {
    container.classList.add(`diff-item-${action}`);
  }
  // Update active button state
  container.querySelectorAll(".diff-item-btn").forEach(btn => btn.classList.remove("active"));
  const activeBtn = container.querySelector(`.diff-item-btn.${action}`);
  if (activeBtn) activeBtn.classList.add("active");
}

function _updateItemSummary(reviewId) {
  // Re-render the item summary bar from current DOM state
  // (counts diff-item-accepted etc. classes on all .diff-change elements)
  const counts = { accepted: 0, rejected: 0, edited: 0, unchanged: 0, pending: 0 };
  document.querySelectorAll(".diff-change").forEach(el => {
    if (el.classList.contains("diff-item-accepted")) counts.accepted++;
    else if (el.classList.contains("diff-item-rejected")) counts.rejected++;
    else if (el.classList.contains("diff-item-edited")) counts.edited++;
    else if (el.classList.contains("diff-item-unchanged")) counts.unchanged++;
    else counts.pending++;
  });
  const bar = document.getElementById("diff-item-summary");
  if (bar) {
    bar.innerHTML = `
      <span class="dim-count accepted">&#10003; ${counts.accepted} accepted</span>
      <span class="dim-count rejected">&#10007; ${counts.rejected} rejected</span>
      <span class="dim-count edited">&#9998; ${counts.edited} edited</span>
      <span class="dim-count unchanged">&mdash; ${counts.unchanged} unchanged</span>
      <span class="dim-count pending">&#8943; ${counts.pending} pending</span>`;
  }
}
```

### Apply button

Add to the review action buttons area (near the existing Approve/Reject/Request Changes buttons):

```javascript
// In selectReview(), when building the review-actions HTML:
if (isActionable) {
  html += `<button class="btn btn-secondary" onclick="applyReviewItems(${data.id})">
    Apply Accepted Changes
  </button>`;
}
```

```javascript
async function applyReviewItems(reviewId) {
  const r = await POST(`/reviews/${reviewId}/apply`);
  if (r.error) {
    toast(r.error, false);
  } else {
    const msg = r.pending_count > 0
      ? `Reviewed file saved. ${r.pending_count} of ${r.total_items} items still pending.`
      : `Reviewed file saved. All ${r.total_items} items decided.`;
    toast(msg);
  }
}
window.applyReviewItems = applyReviewItems;
```

### New CSS classes to add to `btcedu/web/static/styles.css`

```css
/* ── Phase 5: Granular diff item actions ─────────────────────────── */

.diff-item-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
  margin-top: 4px;
}

.diff-item-btn {
  padding: 2px 8px;
  font-size: 12px;
  border-radius: 12px;
  border: 1px solid currentColor;
  background: transparent;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.diff-item-btn.accept  { color: var(--green, #2a9d4a); }
.diff-item-btn.reject  { color: var(--red, #c0392b); }
.diff-item-btn.edit    { color: var(--indigo, #4f46e5); }
.diff-item-btn.reset   { color: var(--text-dim, #888); }

.diff-item-btn.accept.active  { background: var(--green, #2a9d4a); color: #fff; }
.diff-item-btn.reject.active  { background: var(--red, #c0392b); color: #fff; }
.diff-item-btn.edit.active    { background: var(--indigo, #4f46e5); color: #fff; }
.diff-item-btn.reset.active   { background: var(--text-dim, #888); color: #fff; }

.diff-item-accepted {
  background: rgba(42, 157, 74, 0.08);
  border-left: 3px solid #2a9d4a;
}

.diff-item-rejected {
  background: rgba(192, 57, 43, 0.08);
  border-left: 3px solid #c0392b;
}
.diff-item-rejected .diff-corrected,
.diff-item-rejected .diff-adapted {
  text-decoration: line-through;
  opacity: 0.6;
}

.diff-item-edited {
  background: rgba(79, 70, 229, 0.08);
  border-left: 3px solid #4f46e5;
}

.diff-item-unchanged {
  background: rgba(136, 136, 136, 0.08);
  border-left: 3px solid #888;
}

.diff-edit-inline {
  margin-top: 4px;
}

.diff-edit-textarea {
  width: 100%;
  font-size: 13px;
  font-family: inherit;
  padding: 6px 8px;
  border: 1px solid var(--border, #ddd);
  border-radius: 4px;
  resize: vertical;
  min-height: 60px;
  box-sizing: border-box;
}

.diff-edit-actions {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}

.diff-item-summary {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--text-dim, #888);
  margin-top: 4px;
  padding: 4px 0;
}

.diff-item-summary .dim-count { white-space: nowrap; }
.diff-item-summary .dim-count.accepted { color: #2a9d4a; }
.diff-item-summary .dim-count.rejected { color: #c0392b; }
.diff-item-summary .dim-count.edited   { color: #4f46e5; }
```

---

## Section 8: Whole-Review Integration Policy

The following rules govern how item-level review integrates with the existing whole-review approval flow:

1. **Existing "Approve" button behavior is unchanged.** `approve_review()` in `reviewer.py` operates identically to pre-Phase-5. It sets `ReviewTask.status = APPROVED`, creates a `ReviewDecision`, writes review history. Item-level decisions are informational only and do not block or alter this path.

2. **Item decisions are optional.** A reviewer can approve, reject, or request-changes on a review at any time without ever clicking any per-item action. Phase 5 adds capability without removing flexibility.

3. **"Apply Accepted Changes" writes the sidecar file; it does NOT approve the review.** The `POST /api/reviews/<id>/apply` endpoint calls `apply_item_decisions()` which writes the sidecar file to `data/outputs/{ep_id}/review/transcript.reviewed.de.txt` or `script.adapted.reviewed.tr.md`. The `ReviewTask.status` remains `PENDING` or `IN_REVIEW` after this call.

4. **Recommended reviewer workflow when using item-level review:**
   - Review each diff item: accept / reject / edit / leave pending
   - Click "Apply Accepted Changes" — sidecar file written, toast shows pending count
   - (Optional) review remaining pending items
   - Click "Approve" to finalize the whole review — this is what unblocks the pipeline

5. **Downstream stage behavior — sidecar file detection:**

   In `translator.py` (`translate_transcript` function), after resolving `corrected_path`, add a check:
   ```python
   # Check for reviewed sidecar (Phase 5 granular review output)
   reviewed_path = Path(settings.outputs_dir) / episode_id / "review" / "transcript.reviewed.de.txt"
   if reviewed_path.exists():
       corrected_path = reviewed_path
       logger.info("Using reviewed transcript sidecar for episode %s", episode_id)
   ```

   In `chapterizer.py` (`chapterize_script` function) or whichever downstream stage consumes `script.adapted.tr.md`, add:
   ```python
   # Check for reviewed sidecar (Phase 5 granular review output)
   reviewed_path = Path(settings.outputs_dir) / episode_id / "review" / "script.adapted.reviewed.tr.md"
   if reviewed_path.exists():
       adapted_path = reviewed_path
       logger.info("Using reviewed adaptation sidecar for episode %s", episode_id)
   ```

6. **The whole review can be approved at any time.** Items that are still `pending` when "Apply" is called are treated as accepted (proposed change wins). Items that were never acted on at all (no `ReviewItemDecision` record) are also treated as accepted if "Apply" is called. If the reviewer approves without ever calling "Apply", no sidecar is written and the downstream stage uses the original pipeline output (corrected transcript or adapted script).

---

## Section 9: Files to Modify / Create

### CREATE

| File | Purpose |
|------|---------|
| `btcedu/models/review_item.py` | `ReviewItemAction` enum + `ReviewItemDecision` SQLAlchemy model |
| (Migration 007 class added to `btcedu/migrations/__init__.py` — see below) | — |

### MODIFY

| File | Change |
|------|--------|
| `btcedu/models/review.py` | Add `item_decisions: Mapped[list["ReviewItemDecision"]]` relationship to `ReviewTask`; add `__future__` annotations import |
| `btcedu/core/corrector.py` | Add `"item_id": f"corr-{len(changes):04d}"` to each change dict in `compute_correction_diff()` |
| `btcedu/core/adapter.py` | Add `"item_id": f"adap-{len(adaptations):04d}"` to each adaptation dict in `compute_adaptation_diff()` |
| `btcedu/core/reviewer.py` | Add: `upsert_item_decision`, `get_item_decisions`, `apply_item_decisions`, `_assemble_correction_review`, `_assemble_adaptation_review`, `_sidecar_path`, `_load_item_texts_from_diff`, `_ensure_item_ids_correction`, `_ensure_item_ids_adaptation`; update `get_review_detail()` to include `item_decisions` key |
| `btcedu/migrations/__init__.py` | Add `AddReviewItemDecisionsMigration` class (inline, consistent with 001–006 pattern); add instance to `MIGRATIONS` list |
| `btcedu/web/api.py` | Add 5 new item routes: accept, reject, edit, reset, apply; add `_get_review_task_or_404` and `_check_review_actionable` helpers |
| `btcedu/web/static/app.js` | Extend `renderDiffViewer()` with item_id tracking, action bars, inline editor, item summary bar; add `itemAction`, `toggleEditInline`, `saveEditInline`, `cancelEditInline`, `_updateItemVisual`, `_updateItemSummary`, `applyReviewItems` functions; update `selectReview()` to pass `item_decisions` and `isActionable` |
| `btcedu/web/static/styles.css` | Add all `.diff-item-*` CSS classes listed in Section 7 |

**Optional (Section 8 downstream sidecar detection):**

| File | Change |
|------|--------|
| `btcedu/core/translator.py` | Check for `transcript.reviewed.de.txt` sidecar and use it if present |
| `btcedu/core/chapterizer.py` | Check for `script.adapted.reviewed.tr.md` sidecar and use it if present |

---

## Section 10: Test Plan

### `tests/test_review_item_model.py` (NEW)

```
test_create_review_item_decision
  — Create ReviewItemDecision with required fields
  — Verify defaults: action="pending", decided_at=None, edited_text=None

test_upsert_item_decision_create
  — Call upsert_item_decision() for a new item_id
  — Verify record created with action="accepted", decided_at set

test_upsert_item_decision_update
  — Call upsert_item_decision() twice for same item_id
  — Verify second call updates action and decided_at; no duplicate rows

test_item_decision_cascade_delete
  — Create ReviewTask with ReviewItemDecision records
  — Delete ReviewTask
  — Verify ReviewItemDecision rows are gone (cascade)

test_get_item_decisions
  — Create 3 item decisions for one task, 1 for another
  — Call get_item_decisions(session, task_id)
  — Verify returns dict of 3 entries, keyed by item_id
  — Verify other task's decisions not included
```

### `tests/test_diff_item_ids.py` (NEW)

```
test_correction_diff_has_item_ids
  — Call compute_correction_diff(original, corrected, "ep1")
  — Verify every entry in result["changes"] has "item_id" key

test_correction_item_id_format
  — Verify item_ids match regex r"^corr-\\d{4}$"
  — Verify item_ids are sequential: corr-0000, corr-0001, ...

test_adaptation_diff_has_item_ids
  — Call compute_adaptation_diff(translation, adapted, "ep1") where adapted has [T1:...] markers
  — Verify every entry in result["adaptations"] has "item_id" key

test_adaptation_item_id_format
  — Verify item_ids match regex r"^adap-\\d{4}$"
  — Verify item_ids are sequential: adap-0000, adap-0001, ...

test_item_id_stable_across_reruns
  — Call compute_correction_diff twice with identical inputs
  — Verify item_ids in both outputs are identical for same index positions
```

### `tests/test_review_item_api.py` (NEW)

All tests use Flask test client with in-memory DB and a pre-created ReviewTask in PENDING status.

```
test_accept_item
  — POST /api/reviews/<id>/items/corr-0000/accept
  — Assert 200, {"success": True, "item_id": "corr-0000", "action": "accepted"}
  — Assert ReviewItemDecision record created with action="accepted"

test_reject_item
  — POST /api/reviews/<id>/items/corr-0001/reject
  — Assert 200, {"success": True, ..., "action": "rejected"}

test_edit_item_valid
  — POST /api/reviews/<id>/items/corr-0002/edit with {"text": "corrected text"}
  — Assert 200, {"success": True, ..., "action": "edited", "edited_text": "corrected text"}
  — Assert edited_text stored in DB

test_edit_item_missing_text
  — POST /api/reviews/<id>/items/corr-0002/edit with {} (no text)
  — Assert 400

test_edit_item_empty_text
  — POST /api/reviews/<id>/items/corr-0002/edit with {"text": "   "}
  — Assert 400

test_reset_item
  — POST accept, then reset on same item_id
  — Assert 200, action="pending"
  — Assert DB record updated to action="pending"

test_item_action_on_approved_review
  — Approve the ReviewTask first
  — POST /api/reviews/<id>/items/corr-0000/accept
  — Assert 400 (not actionable)

test_item_action_on_nonexistent_review
  — POST /api/reviews/99999/items/corr-0000/accept
  — Assert 404

test_apply_corrections
  — Create ReviewTask with stage="correct", diff_path pointing to fixture correction_diff.json
  — Create some item decisions (accepted, rejected)
  — POST /api/reviews/<id>/apply
  — Assert 200, "reviewed_file" path exists on disk, "total_items" matches diff

test_apply_adaptations
  — Same as above but stage="adapt" with adaptation_diff.json fixture

test_apply_no_decisions_yet
  — POST /api/reviews/<id>/apply with no item decisions created
  — Assert 400

test_review_detail_includes_item_decisions
  — Create task, add item decisions via upsert_item_decision
  — GET /api/reviews/<id>
  — Assert response contains "item_decisions" dict
  — Assert corr-0000 entry has action="accepted"
```

### `tests/test_assemble_reviewed.py` (NEW)

```
test_assemble_correction_all_accepted
  — Provide original text, diff_changes, all decisions=accepted
  — Result should match corrected text exactly

test_assemble_correction_all_rejected
  — All decisions=rejected
  — Result should match original text exactly

test_assemble_correction_mixed
  — 3 changes: first=accepted, second=rejected, third=accepted
  — Verify output uses proposed for first/third, original for second

test_assemble_correction_edited
  — One decision with action=edited, edited_text="custom replacement"
  — Verify edited_text appears in output

test_assemble_correction_pending_defaults_to_proposed
  — Leave decisions dict empty (no decisions recorded)
  — Verify result matches corrected text (pending defaults to accept)

test_assemble_adaptation_accepted
  — adapted_text contains [T1: original → adapted] markers
  — decision=accepted → marker tag retained in output (no change)

test_assemble_adaptation_rejected
  — decision=rejected → marker tag replaced with original text

test_assemble_adaptation_edited
  — decision=edited with edited_text → marker tag replaced with edited_text

test_sidecar_path_correction
  — _sidecar_path("ep123", "correct", mock_settings) returns expected path
  — Path ends with "ep123/review/transcript.reviewed.de.txt"

test_sidecar_path_adaptation
  — _sidecar_path("ep123", "adapt", mock_settings) returns expected path
  — Path ends with "ep123/review/script.adapted.reviewed.tr.md"
```

### Modifications to existing test files

**`tests/test_reviewer.py`:**
```
test_get_review_detail_includes_item_decisions_key
  — Create task, get_review_detail(session, task.id)
  — Assert "item_decisions" key present in result
  — Assert value is dict (empty if no decisions)
```

**`tests/test_corrector.py`:**
```
test_compute_correction_diff_has_item_id
  — Call compute_correction_diff with sample texts
  — Assert each change has item_id field
```

**`tests/test_adapter.py`:**
```
test_compute_adaptation_diff_has_item_id
  — Call compute_adaptation_diff with text containing [T1:...] markers
  — Assert each adaptation has item_id field
```

### Backward compatibility test

```
test_old_diff_without_item_id
  — Create ReviewTask with diff_path pointing to old-format diff (no item_id fields)
  — Call get_review_detail(session, task.id)
  — Assert result["item_decisions"] == {} (empty, no crash)
  — Call POST /apply — should still work by generating item_ids on-the-fly
```

---

## Section 11: Definition of Done

- [ ] `btcedu/models/review_item.py` created with `ReviewItemAction` enum and `ReviewItemDecision` model using `btcedu.db.Base`
- [ ] `ReviewTask` in `btcedu/models/review.py` has `item_decisions` relationship with cascade delete
- [ ] `compute_correction_diff()` adds `item_id` field (`corr-NNNN`) to every change entry
- [ ] `compute_adaptation_diff()` adds `item_id` field (`adap-NNNN`) to every adaptation entry
- [ ] Migration 007 creates `review_item_decisions` table with indexes on `review_task_id` and `(review_task_id, item_id)`
- [ ] `btcedu migrate` runs migration 007 successfully on a fresh or existing database
- [ ] `upsert_item_decision()` creates/updates `ReviewItemDecision` records correctly
- [ ] `get_item_decisions()` returns correct dict keyed by item_id
- [ ] `apply_item_decisions()` writes correct sidecar file for both `correct` and `adapt` stages
- [ ] `_assemble_correction_review()` correctly handles all 5 action states (accepted, rejected, edited, unchanged, pending)
- [ ] `_assemble_adaptation_review()` correctly handles all action states using character-position splicing
- [ ] All 5 new API endpoints respond correctly: accept, reject, edit, reset, apply
- [ ] Invalid requests return correct HTTP status codes (400 for bad input, 404 for not found)
- [ ] `GET /api/reviews/<id>` response includes `item_decisions` dict
- [ ] Per-item action bars render in the UI for PENDING and IN_REVIEW reviews
- [ ] Item visual states update immediately on action button click
- [ ] Summary counts bar shows correct counts and updates dynamically
- [ ] Inline edit textarea pre-fills with proposed text and saves correctly
- [ ] "Apply Accepted Changes" button only visible for actionable reviews
- [ ] Apply button shows toast with pending count; does NOT change review status
- [ ] Existing Approve/Reject/Request-Changes buttons work identically to pre-Phase-5
- [ ] Old-format diffs (no `item_id`) do not crash: API returns empty `item_decisions` dict
- [ ] All new test files pass: `test_review_item_model.py`, `test_diff_item_ids.py`, `test_review_item_api.py`, `test_assemble_reviewed.py`
- [ ] Existing test suite (629 tests) remains fully green after changes
- [ ] `btcedu migrate-status` shows migration 007 as applied

---

## Section 12: Non-Goals

The following are explicitly out of scope for Phase 5:

- **Bulk accept/reject all items** — no "Accept All" or "Reject All" button. Reviewers act on items individually.
- **Re-running the LLM correction/adaptation with item decisions as feedback** — apply writes a static sidecar file; it does not trigger a new LLM call.
- **Collaborative multi-reviewer support** — each item has a single action state; no per-reviewer tracking, no conflict resolution.
- **Item-level review for stages other than `correct` and `adapt`** — render review (RG3), stock image review, and any future stages are not included.
- **Undo history beyond reset** — the Reset action restores `pending` state; there is no multi-step undo stack.
- **Saving item decisions to git or external audit system** — the existing `review_history.json` file-level audit trail is not extended for item decisions.
- **Diff item reordering or filtering in the UI** — items are always shown in their natural diff order; no sort/filter controls.
- **Automatic pipeline re-run after apply** — clicking "Apply Accepted Changes" writes the sidecar; the reviewer must separately click "Approve" and then trigger pipeline re-run.
- **Mobile-optimized item action UI** — the per-item action bars are desktop-first; no special mobile layout is required for Phase 5.
- **Performance optimization for very large diffs (>1000 items)** — no virtualized list rendering or pagination for the diff viewer.
- **TTS or image review item decisions** — these stages produce binary assets, not text diffs, so granular text editing is not applicable.

---

### Critical Files for Implementation

- `/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/models/review.py` - ReviewTask model to extend with item_decisions relationship
- `/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/core/reviewer.py` - Core reviewer logic; all new assembly and upsert functions go here
- `/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/core/corrector.py` - compute_correction_diff() to patch with item_id; also the word-tokenization approach here is the reference for _assemble_correction_review
- `/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/migrations/__init__.py` - Migration 007 class to add inline, following exact pattern of migrations 001-006
- `/home/pi/AI-Startup-Lab/bitcoin-education/btcedu/web/static/app.js` - renderDiffViewer() and selectReview() to extend; largest UI surface area for Phase 5"
  },
  {
    "type": "text",
    "text": "agentId: a47434b346c0afa54 (for resuming to continue this agent's work if needed)
<usage>total_tokens: 76376
tool_uses: 18
duration_ms: 274711</usage>"
  }
]

<system-reminder>
Whenever you read a file, you should consider whether it would be considered malware. You CAN and SHOULD provide analysis of malware, what it is doing. But you MUST refuse to improve or augment the code. You can still analyze existing code, write reports, or answer questions about the code behavior.
</system-reminder>
