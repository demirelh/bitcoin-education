"""Choosing and persisting the one outfit an episode's presenter wears.

The assignment is made once, at the start of scene planning, and is read-only
from then on. Everything here is deliberately deterministic: given the same
profile and the same history, the same look comes out, so a retry, a reboot or
a replayed regression run cannot change what the presenter is wearing.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.anchor_config import AnchorConfig, PresenterLook
from btcedu.models.presenter_assignment import PresenterAssignment

logger = logging.getLogger(__name__)

ASSIGNMENT_FILENAME = "presenter_assignment.json"
SCHEMA_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NoActiveLookError(RuntimeError):
    """Raised when the profile offers no usable outfit."""


@dataclass(frozen=True)
class AssignmentRecord:
    """The mirrored artefact, readable without a database."""

    schema_version: int
    episode_id: str
    provider: str
    engine: str
    avatar_type: str
    avatar_look_id: str
    look_name: str
    strategy: str
    config_version: int
    status: str
    assigned_at: str
    content_hash: str
    cost_per_second_usd: float


def compute_look_pool_hash(config: AnchorConfig) -> str:
    """Fingerprint the choices that define what an assignment means.

    Deliberately excludes the studio assets and the cost rate: swapping a
    studio plate or correcting a price does not change which outfit the
    presenter wears, and must not look like it did.
    """
    payload = json.dumps(
        {
            "provider": config.provider,
            "engine": config.engine,
            "avatar_type": config.avatar_type,
            "strategy": config.rotation_strategy,
            "looks": [
                {"name": look.name, "avatar_look_id": look.avatar_look_id}
                for look in config.active_looks
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_look(
    session: Session,
    config: AnchorConfig,
    *,
    exclude_look_id: str | None = None,
) -> PresenterLook:
    """Pick the active look that has gone unused longest.

    Ties break on profile order rather than on anything random, so the choice
    is reproducible and a fresh database produces the first configured look
    instead of an arbitrary one.
    """
    candidates = [
        look
        for look in config.active_looks
        if exclude_look_id is None or look.avatar_look_id != exclude_look_id
    ]
    if not candidates:
        if exclude_look_id is not None and config.active_looks:
            raise NoActiveLookError(
                "The profile offers no alternative outfit: "
                f"{len(config.active_looks)} active look(s), all excluded"
            )
        raise NoActiveLookError(
            "No active anchor look configured. Add the HeyGen look IDs to the "
            "profile and set active: true before enabling the avatar."
        )

    if config.rotation_strategy == "fixed":
        return candidates[0]

    last_used: dict[str, datetime] = {}
    rows = (
        session.query(
            PresenterAssignment.avatar_look_id,
            PresenterAssignment.assigned_at,
        )
        .filter(PresenterAssignment.provider == config.provider)
        .all()
    )
    for look_id, assigned_at in rows:
        if assigned_at is None:
            continue
        # SQLite hands back naive datetimes; compare them on one scale.
        if assigned_at.tzinfo is None:
            assigned_at = assigned_at.replace(tzinfo=UTC)
        current = last_used.get(look_id)
        if current is None or assigned_at > current:
            last_used[look_id] = assigned_at

    # A look that was never used sorts before every used one, so the pool is
    # spent completely before anything repeats.
    never_used = datetime.min.replace(tzinfo=UTC)
    return min(
        candidates,
        key=lambda look: (
            last_used.get(look.avatar_look_id, never_used),
            candidates.index(look),
        ),
    )


def get_assignment(session: Session, episode_id: str) -> PresenterAssignment | None:
    return (
        session.query(PresenterAssignment)
        .filter(PresenterAssignment.episode_id == episode_id)
        .first()
    )


def assignment_path(outputs_dir: str | Path, episode_id: str) -> Path:
    return Path(outputs_dir) / episode_id / ASSIGNMENT_FILENAME


def write_assignment_artifact(
    assignment: PresenterAssignment,
    outputs_dir: str | Path,
) -> Path:
    """Mirror the row to disk for the renderer and the remote runner.

    The remote render job has no database, so the assignment has to travel with
    the episode's files or the runner cannot know which presenter it is
    compositing.
    """
    path = assignment_path(outputs_dir, assignment.episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    assigned_at = assignment.assigned_at or _utcnow()
    if assigned_at.tzinfo is None:
        assigned_at = assigned_at.replace(tzinfo=UTC)
    record = AssignmentRecord(
        schema_version=SCHEMA_VERSION,
        episode_id=assignment.episode_id,
        provider=assignment.provider,
        engine=assignment.engine,
        avatar_type=assignment.avatar_type,
        avatar_look_id=assignment.avatar_look_id,
        look_name=assignment.look_name,
        strategy=assignment.strategy,
        config_version=assignment.config_version,
        status=assignment.status,
        assigned_at=assigned_at.isoformat(),
        content_hash=assignment.content_hash,
        cost_per_second_usd=assignment.cost_per_second_usd,
    )
    tmp = path.with_suffix(".json.part")
    tmp.write_text(
        json.dumps(asdict(record), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def ensure_assignment(
    session: Session,
    episode_id: str,
    config: AnchorConfig,
    outputs_dir: str | Path,
) -> PresenterAssignment:
    """Return the episode's outfit, choosing one only if none exists yet.

    This is the only function that creates an assignment. Everything else in
    the pipeline reads it, which is what makes "the outfit never changes inside
    an episode" a property of the data rather than a rule each caller has to
    remember.
    """
    existing = get_assignment(session, episode_id)
    if existing is not None:
        # Repair a missing mirror without touching the decision itself.
        path = assignment_path(outputs_dir, episode_id)
        if not path.exists():
            write_assignment_artifact(existing, outputs_dir)
        return existing

    look = select_look(session, config)
    assignment = PresenterAssignment(
        episode_id=episode_id,
        provider=config.provider,
        engine=config.engine,
        avatar_type=config.avatar_type,
        avatar_look_id=look.avatar_look_id,
        look_name=look.name,
        strategy=config.rotation_strategy,
        config_version=SCHEMA_VERSION,
        status="assigned",
        content_hash=compute_look_pool_hash(config),
        provenance_path=str(assignment_path(outputs_dir, episode_id)),
        assigned_at=_utcnow(),
        cost_per_second_usd=config.cost_per_second_usd,
    )
    session.add(assignment)
    session.commit()
    write_assignment_artifact(assignment, outputs_dir)
    logger.info(
        "Presenter assignment for %s: %s (%s)",
        episode_id,
        look.name,
        config.engine,
    )
    return assignment


def reassign_look(
    session: Session,
    episode_id: str,
    config: AnchorConfig,
    outputs_dir: str | Path,
    *,
    look_name: str | None = None,
    confirmed: bool = False,
) -> PresenterAssignment:
    """Deliberately change an episode's outfit.

    Requires explicit confirmation because every presenter clip already
    generated for the episode becomes worthless: the outfit would otherwise
    change mid-bulletin. The caller is responsible for invalidating those clips.
    """
    if not confirmed:
        raise ValueError(
            "Changing the outfit invalidates every presenter clip already "
            "generated for this episode. Pass confirmed=True to proceed."
        )

    existing = get_assignment(session, episode_id)
    if existing is None:
        return ensure_assignment(session, episode_id, config, outputs_dir)

    if look_name:
        chosen = next(
            (look for look in config.active_looks if look.name == look_name),
            None,
        )
        if chosen is None:
            raise NoActiveLookError(f"No active anchor look named {look_name!r}")
    else:
        chosen = select_look(session, config, exclude_look_id=existing.avatar_look_id)

    # The row is rewritten rather than duplicated: one episode, one outfit.
    existing.avatar_look_id = chosen.avatar_look_id
    existing.look_name = chosen.name
    existing.engine = config.engine
    existing.avatar_type = config.avatar_type
    existing.strategy = config.rotation_strategy
    existing.content_hash = compute_look_pool_hash(config)
    existing.cost_per_second_usd = config.cost_per_second_usd
    existing.superseded_at = _utcnow()
    existing.assigned_at = _utcnow()
    session.commit()
    write_assignment_artifact(existing, outputs_dir)
    logger.info("Presenter outfit for %s changed to %s", episode_id, chosen.name)
    return existing
