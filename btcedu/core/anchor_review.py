"""The avatar review gate: a person looks at the presenter before the render.

Everything upstream of this point is verifiable by machine — the clip exists,
its hash matches, its length is right. What no check can answer is whether the
woman on screen looks like a news presenter or like a waxwork, and that is the
one thing an audience notices first. So between ``anchorgen`` and ``render``
there is a gate whose only job is to put the raw clips in front of somebody.

It is deliberately *not* the final review. ``review_gate_3`` still runs after
the render and still judges the finished bulletin — studio, monitor, reporter
pictures, sound, subtitles. This gate judges the presenter alone, early, while
a bad take costs one clip to replace rather than a whole render.

**Binding.** An approval is worth nothing unless it is nailed to what was
actually seen. The existing artifact-bound review machinery hashes the *bytes*
of a list of files, which covers the scene plan and the anchor manifest but not
the ledger: a job that quietly moved to ``reconcile_required``, or a look that
was swapped in the database, would leave both files untouched. So this module
writes a digest sidecar — a small deterministic JSON file containing every
input the requirement names — and hands the existing machinery three files to
hash. Change the look, the audio, the plan, a clip or a job status and the
sidecar changes, the content hash changes, and yesterday's approval stops
matching. No new staleness mechanism was needed; the old one just had to be
given something that actually moves.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.review import ReviewStatus, ReviewTask

logger = logging.getLogger(__name__)

#: The pipeline stage name these review tasks carry.
REVIEW_STAGE = "anchor"

DIGEST_FILENAME = "review_digest.json"
DIGEST_SCHEMA_VERSION = 1

#: Statuses in which a clip is usable by the renderer.
USABLE_JOB_STATUSES = frozenset({AvatarJobStatus.COMPLETED.value})


class AnchorReviewError(RuntimeError):
    """The avatar review cannot be performed or decided as asked."""


def anchor_dir(settings: Settings, episode_id: str) -> Path:
    return Path(settings.outputs_dir) / episode_id / "anchor"


def digest_path(settings: Settings, episode_id: str) -> Path:
    return anchor_dir(settings, episode_id) / DIGEST_FILENAME


def manifest_path(settings: Settings, episode_id: str) -> Path:
    return anchor_dir(settings, episode_id) / "manifest.json"


def plan_path(settings: Settings, episode_id: str) -> Path:
    from btcedu.core.scene_planner import scene_plan_path

    return scene_plan_path(settings.outputs_dir, episode_id)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class SceneView:
    """One presenter scene as an operator needs to see it."""

    scene_id: str
    chapter_id: str
    order: int
    purpose: str
    template_id: str
    speaker_role: str
    duration_seconds: float
    look_id: str
    job_status: str
    provider_job_id: str
    cost_usd: float
    audio_path: str
    audio_hash: str
    content_hash: str
    video_path: str
    mime_type: str
    preview_available: bool
    revision: int
    flagged: bool
    flag_note: str

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "chapter_id": self.chapter_id,
            "order": self.order,
            "purpose": self.purpose,
            "template_id": self.template_id,
            "speaker_role": self.speaker_role,
            "duration_seconds": round(self.duration_seconds, 3),
            # Shortened: an operator has to recognise the outfit, not retype
            # a provider identifier into a support ticket.
            "look_id": _short(self.look_id),
            "job_status": self.job_status,
            "provider_job_id": _short(self.provider_job_id),
            "cost_usd": round(self.cost_usd, 6),
            "audio_path": self.audio_path,
            "audio_hash": _short(self.audio_hash),
            "content_hash": _short(self.content_hash),
            "mime_type": self.mime_type,
            "preview_available": self.preview_available,
            "revision": self.revision,
            "flagged": self.flagged,
            "flag_note": self.flag_note,
        }


@dataclass
class AnchorReviewState:
    """Everything the gate, the API and the dashboard need about one episode."""

    episode_id: str
    profile: str
    enabled: bool
    reason: str = ""
    provider: str = ""
    engine: str = ""
    avatar_type: str = ""
    studio_mode: str = ""
    look_id: str = ""
    look_name: str = ""
    rotation_strategy: str = ""
    plan_hash: str = ""
    assignment_hash: str = ""
    manifest_hash: str = ""
    review_hash: str = ""
    review_status: str = "not_required"
    review_task_id: int | None = None
    reviewer_notes: str = ""
    expected_scene_count: int = 0
    completed_scene_count: int = 0
    blocked_scene_count: int = 0
    cost_per_second_usd: float = 0.0
    reserved_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    estimated_remaining_usd: float = 0.0
    scenes: list[SceneView] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    open_flags: list[str] = field(default_factory=list)

    @property
    def approvable(self) -> bool:
        return self.enabled and not self.blockers

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "episode_id": self.episode_id,
            "profile": self.profile,
            "anchor_enabled": self.enabled,
            "reason": self.reason,
            "provider": self.provider,
            "engine": self.engine,
            "avatar_type": self.avatar_type,
            "studio_mode": self.studio_mode,
            "look_name": self.look_name,
            "look_id": _short(self.look_id),
            "rotation_strategy": self.rotation_strategy,
            "scene_plan_hash": _short(self.plan_hash),
            "presenter_assignment_hash": _short(self.assignment_hash),
            "anchor_manifest_hash": _short(self.manifest_hash),
            "review_hash": _short(self.review_hash),
            "review_status": self.review_status,
            "review_task_id": self.review_task_id,
            "reviewer_notes": self.reviewer_notes,
            "expected_scene_count": self.expected_scene_count,
            "completed_scene_count": self.completed_scene_count,
            "blocked_scene_count": self.blocked_scene_count,
            "cost_per_second_usd": round(self.cost_per_second_usd, 6),
            "reserved_cost_usd": round(self.reserved_cost_usd, 6),
            "actual_cost_usd": round(self.actual_cost_usd, 6),
            "estimated_remaining_usd": round(self.estimated_remaining_usd, 6),
            "approvable": self.approvable,
            "blockers": list(self.blockers),
            "open_flags": list(self.open_flags),
            "scenes": [scene.to_dict() for scene in self.scenes],
        }


def _file_hash(path: Path) -> str:
    """Identity of a manifest as it sits on disk."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _short(value: str) -> str:
    """Enough of an identifier to recognise, not enough to be a credential."""
    text = str(value or "")
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"


def gate_applies(config, settings: Settings) -> tuple[bool, str]:
    """Whether this episode's presenter needs a human before the render.

    Three independent conditions, each of which alone is a good reason not to
    invent a review: the deployment has the avatar switched off, the profile
    does not want the gate, or the provider is not the one this gate was built
    for. D-ID's chapter path has no scene plan and no presenter clips to look
    at, so a gate there would be a task nobody could action.
    """
    if config is None:
        return False, "no anchor configuration"
    if not getattr(settings, "anchor_enabled", False):
        return False, "anchor disabled for this deployment"
    if not getattr(config, "review_required", False):
        return False, "profile does not require an avatar review"
    if config.provider != "heygen":
        return False, f"provider {config.provider!r} has no scene-level presenter review"
    return True, ""


def _config_for(session: Session, episode_id: str, settings: Settings):
    """Resolve this episode's avatar configuration, or None if unavailable."""
    from btcedu.core.anchor_config import resolve_anchor_config
    from btcedu.models.episode import Episode

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    profile = getattr(episode, "content_profile", "") or "bitcoin_podcast"
    try:
        return resolve_anchor_config(profile, settings)
    except Exception:  # noqa: BLE001 - a broken profile yields an empty digest
        return None


def build_digest(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    config=None,
) -> dict:
    """Assemble the deterministic inputs an approval is bound to.

    Ordered and rounded on purpose: two runs over unchanged state must produce
    byte-identical output, or every run would invalidate the last approval.
    """
    from btcedu.core.avatar_regeneration import episode_revisions
    from btcedu.core.presenter_assignment import get_assignment
    from btcedu.core.scene_planner import ROLE_ANCHOR

    # The digest must not depend on whether the caller happened to have the
    # configuration to hand; otherwise the same state hashes two ways and an
    # approval goes stale for no reason at all.
    if config is None:
        config = _config_for(session, episode_id, settings)

    plan = _read_json(plan_path(settings, episode_id))
    manifest = _read_json(manifest_path(settings, episode_id))
    assignment = get_assignment(session, episode_id)

    scenes = []
    for entry in manifest.get("scenes", []) or []:
        scenes.append(
            {
                "scene_id": entry.get("scene_id", ""),
                "chapter_id": entry.get("chapter_id", ""),
                "look_id": entry.get("avatar_look_id", ""),
                "audio_hash": entry.get("audio_hash", ""),
                "content_hash": entry.get("content_hash", ""),
                "status": entry.get("status", ""),
                "duration_seconds": round(float(entry.get("duration_seconds") or 0.0), 3),
            }
        )
    scenes.sort(key=lambda item: item["scene_id"])

    # Only the presenter's own scenes are folded in. Binding to the whole scene
    # plan would make a swapped reporter photo invalidate an approval that was
    # never about the reporter — the finished broadcast is judged again at the
    # final gate, which is where that belongs.
    planned = [
        {
            "scene_id": scene.get("scene_id", ""),
            "chapter_id": scene.get("chapter_id", ""),
            "order": int(scene.get("order") or 0),
            "text_hash": scene.get("text_hash", ""),
            "audio_file": scene.get("audio_file") or "",
            "duration_seconds": round(float(scene.get("expected_duration_seconds") or 0.0), 3),
        }
        for scene in plan.get("scenes", []) or []
        if scene.get("needs_avatar") and scene.get("speaker_role") == ROLE_ANCHOR
    ]
    planned.sort(key=lambda item: item["scene_id"])

    # Job statuses come from the ledger rather than the manifest: a run that
    # ends in reconciliation updates the row but may never rewrite the file,
    # and an approval must not survive that.
    jobs = {
        job.scene_id: job.status
        for job in session.query(AvatarJob).filter(AvatarJob.episode_id == episode_id).all()
    }

    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "episode_id": episode_id,
        "planned_anchor_scenes": planned,
        "presenter_look_id": plan.get("presenter_look_id", ""),
        "presenter_assignment": {
            "look_id": assignment.avatar_look_id if assignment else "",
            "look_name": assignment.look_name if assignment else "",
            "engine": assignment.engine if assignment else "",
            "content_hash": assignment.content_hash if assignment else "",
        },
        "anchor_manifest": {
            "provider": manifest.get("anchor_provider", ""),
            "engine": manifest.get("engine", ""),
            "avatar_look_id": manifest.get("avatar_look_id", ""),
            "output_format": manifest.get("output_format", ""),
            "schema_version": manifest.get("schema_version", ""),
        },
        "provider": getattr(config, "provider", "") if config else "",
        "engine": getattr(config, "engine", "") if config else "",
        "scenes": scenes,
        "job_statuses": sorted(jobs.items()),
        # A confirmed regeneration means one of these clips is about to be
        # replaced. Approving the outgoing version would carry a signature
        # onto footage nobody has seen.
        "pending_revisions": sorted(episode_revisions(session, episode_id).items()),
    }


def write_digest(session: Session, episode_id: str, settings: Settings, *, config=None) -> Path:
    """Write the digest sidecar, but only when its content actually changed.

    Rewriting an identical file would change nothing that is hashed, yet it
    would churn mtimes and make the artifact look newer than the decision — so
    the write is skipped when the bytes match.
    """
    path = digest_path(settings, episode_id)
    payload = json.dumps(
        build_digest(session, episode_id, settings, config=config),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == payload:
                return path
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path


def review_artifacts(settings: Settings, episode_id: str) -> list[str]:
    """The files an anchor approval is bound to, in a stable order."""
    # Deliberately just the digest: it already carries the plan's presenter
    # scenes, the assignment, every clip hash and the ledger's job statuses,
    # and nothing that belongs to the reporter.
    return [str(digest_path(settings, episode_id))]


def review_hash(session: Session, episode_id: str, settings: Settings, *, config=None) -> str:
    """The composite fingerprint an operator approves."""
    digest = build_digest(session, episode_id, settings, config=config)
    payload = json.dumps(digest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def latest_task(session: Session, episode_id: str) -> ReviewTask | None:
    return (
        session.query(ReviewTask)
        .filter(ReviewTask.episode_id == episode_id, ReviewTask.stage == REVIEW_STAGE)
        .order_by(ReviewTask.created_at.desc(), ReviewTask.id.desc())
        .first()
    )


def _flag_notes(session: Session, episode_id: str) -> dict[str, str]:
    """Scene complaints an operator recorded against the current task."""
    task = latest_task(session, episode_id)
    if task is None:
        return {}
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    rows = (
        session.query(ReviewItemDecision)
        .filter(
            ReviewItemDecision.review_task_id == task.id,
            ReviewItemDecision.action == ReviewItemAction.REJECTED.value,
        )
        .all()
    )
    return {row.item_id: (row.edited_text or "") for row in rows}


def collect_state(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    profile: str = "",
    episode=None,
) -> AnchorReviewState:
    """Everything known about this episode's presenter, read-only."""
    from btcedu.core.anchor_config import resolve_anchor_config
    from btcedu.core.avatar_regeneration import episode_revisions
    from btcedu.core.presenter_assignment import get_assignment
    from btcedu.core.scene_planner import ROLE_ANCHOR

    if episode is None:
        from btcedu.models.episode import Episode

        episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    profile_name = profile or getattr(episode, "content_profile", "") or "bitcoin_podcast"

    state = AnchorReviewState(episode_id=episode_id, profile=profile_name, enabled=False)

    try:
        config = resolve_anchor_config(profile_name, settings)
    except Exception as exc:  # noqa: BLE001 - a broken profile is a reason, not a crash
        state.reason = f"anchor configuration unavailable: {exc}"
        return state

    state.provider = config.provider
    state.engine = config.engine
    state.avatar_type = config.avatar_type
    state.studio_mode = config.studio_mode
    state.rotation_strategy = config.rotation_strategy

    applies, reason = gate_applies(config, settings)
    state.enabled = applies
    state.reason = reason
    if not applies:
        # Nothing below is meaningful for an episode this gate does not cover,
        # and a deployment without the avatar need not even have the tables.
        return state

    assignment = get_assignment(session, episode_id)
    if assignment is not None:
        state.look_id = assignment.avatar_look_id
        state.look_name = assignment.look_name
        state.assignment_hash = assignment.content_hash

    plan = _read_json(plan_path(settings, episode_id))
    manifest_file = manifest_path(settings, episode_id)
    manifest = _read_json(manifest_file)
    state.plan_hash = str(plan.get("content_hash") or "")
    state.manifest_hash = _file_hash(manifest_file)

    planned_anchor_ids = [
        scene.get("scene_id")
        for scene in plan.get("scenes", []) or []
        if scene.get("needs_avatar") and scene.get("speaker_role") == ROLE_ANCHOR
    ]
    state.expected_scene_count = len(planned_anchor_ids)

    jobs = {
        job.scene_id: job
        for job in session.query(AvatarJob).filter(AvatarJob.episode_id == episode_id).all()
    }
    revisions = episode_revisions(session, episode_id)
    flags = _flag_notes(session, episode_id)
    outputs = Path(settings.outputs_dir) / episode_id

    plan_scenes = {s.get("scene_id"): s for s in plan.get("scenes", []) or []}
    entries = {e.get("scene_id"): e for e in manifest.get("scenes", []) or []}

    for order, scene_id in enumerate(planned_anchor_ids):
        entry = entries.get(scene_id, {})
        planned = plan_scenes.get(scene_id, {})
        job = jobs.get(scene_id)
        video_rel = str(entry.get("video_path") or "")
        preview = bool(video_rel) and (outputs / video_rel).is_file()
        status = (job.status if job is not None else "") or str(entry.get("status") or "missing")
        state.scenes.append(
            SceneView(
                scene_id=scene_id,
                chapter_id=str(entry.get("chapter_id") or planned.get("chapter_id") or ""),
                order=int(planned.get("order", order)),
                purpose=str(planned.get("purpose") or ""),
                template_id=str(entry.get("template_id") or planned.get("template_id") or ""),
                speaker_role=str(planned.get("speaker_role") or "anchor_female"),
                duration_seconds=float(
                    entry.get("duration_seconds")
                    or planned.get("expected_duration_seconds")
                    or 0.0
                ),
                look_id=str(entry.get("avatar_look_id") or state.look_id),
                job_status=status,
                provider_job_id=str(entry.get("provider_job_id") or ""),
                cost_usd=float(job.cost_usd if job is not None else entry.get("cost_usd") or 0.0),
                audio_path=str(entry.get("audio_path") or planned.get("audio_file") or ""),
                audio_hash=str(entry.get("audio_hash") or ""),
                content_hash=str(entry.get("content_hash") or ""),
                video_path=video_rel,
                mime_type=str(entry.get("mime_type") or ""),
                preview_available=preview,
                revision=int(revisions.get(scene_id, 0)),
                flagged=scene_id in flags,
                flag_note=flags.get(scene_id, ""),
            )
        )

    state.completed_scene_count = sum(
        1 for s in state.scenes if s.job_status in USABLE_JOB_STATUSES and s.preview_available
    )
    state.blocked_scene_count = sum(
        1
        for s in state.scenes
        if s.job_status
        in {
            AvatarJobStatus.RESERVED.value,
            AvatarJobStatus.RECONCILE_REQUIRED.value,
            AvatarJobStatus.ABANDONED.value,
            AvatarJobStatus.FAILED.value,
        }
    )
    state.reserved_cost_usd = sum(float(j.cost_usd or 0.0) for j in jobs.values())
    state.actual_cost_usd = sum(
        float(j.cost_usd or 0.0)
        for j in jobs.values()
        if j.status == AvatarJobStatus.COMPLETED.value
    )
    missing_seconds = sum(
        s.duration_seconds for s in state.scenes if s.job_status not in USABLE_JOB_STATUSES
    )
    state.cost_per_second_usd = float(config.cost_per_second_usd or 0.0)
    state.estimated_remaining_usd = round(missing_seconds * config.cost_per_second_usd, 6)
    state.open_flags = sorted(flags)

    # Refresh the sidecar before any decision is read from it. The digest is
    # derived state, not a record: if the clips moved on, the file has to say
    # so before an approval is compared against it.
    if state.enabled and state.expected_scene_count:
        write_digest(session, episode_id, settings, config=config)

    task = latest_task(session, episode_id)
    if task is not None:
        state.review_task_id = int(task.id)
        state.reviewer_notes = task.reviewer_notes or ""
    state.review_hash = review_hash(session, episode_id, settings, config=config)
    state.review_status = _status_for(session, episode_id, settings, task, state)
    state.blockers = approval_blockers(session, episode_id, settings, state=state, config=config)
    return state


def _status_for(
    session: Session,
    episode_id: str,
    settings: Settings,
    task: ReviewTask | None,
    state: AnchorReviewState,
) -> str:
    """The reviewable status, including the ones only this gate has."""
    from btcedu.core.reviewer import review_task_matches_artifacts

    if not state.enabled:
        return "not_required"
    if state.expected_scene_count == 0:
        return "not_required"
    if task is None:
        return "not_started"
    artifacts = review_artifacts(settings, episode_id)
    if task.status in (ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value):
        if not review_task_matches_artifacts(task, artifacts):
            # The decision was about clips that no longer exist in this form.
            return "stale"
        return task.status
    if task.status == ReviewStatus.SUPERSEDED.value:
        return "stale"
    return "pending"


def approval_blockers(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    state: AnchorReviewState | None = None,
    config=None,
) -> list[str]:
    """Why this episode's presenter may not be approved yet.

    Approving is an assertion that the audience may see these clips. Every
    entry here is something an operator cannot see in a preview, which is
    exactly why the button has to be disabled rather than merely discouraged.
    """
    if state is None:
        state = collect_state(session, episode_id, settings)
    if not state.enabled:
        return []

    blockers: list[str] = []
    if state.expected_scene_count == 0:
        return []

    if state.completed_scene_count < state.expected_scene_count:
        blockers.append(
            f"{state.expected_scene_count - state.completed_scene_count} of "
            f"{state.expected_scene_count} presenter clips are not finished"
        )

    for scene in state.scenes:
        if scene.job_status == AvatarJobStatus.RECONCILE_REQUIRED.value:
            blockers.append(
                f"scene {scene.scene_id} awaits reconciliation "
                "(`btcedu avatar-reconcile`)"
            )
        elif scene.job_status == AvatarJobStatus.RESERVED.value:
            blockers.append(
                f"scene {scene.scene_id} is still reserved: the provider order was "
                "never confirmed"
            )
        elif scene.job_status == AvatarJobStatus.ABANDONED.value:
            blockers.append(f"scene {scene.scene_id} was abandoned and has no clip")
        elif scene.job_status == AvatarJobStatus.FAILED.value:
            blockers.append(f"scene {scene.scene_id} failed and has no clip")
        elif scene.job_status in USABLE_JOB_STATUSES and not scene.preview_available:
            blockers.append(f"scene {scene.scene_id} is marked complete but its clip is missing")
        elif scene.job_status not in USABLE_JOB_STATUSES:
            blockers.append(f"scene {scene.scene_id} has no presenter clip yet")
        if scene.look_id and state.look_id and scene.look_id != state.look_id:
            blockers.append(
                f"scene {scene.scene_id} was generated with a different look than "
                "the outfit assigned to this episode"
            )

    if state.open_flags:
        blockers.append(
            f"{len(state.open_flags)} scene(s) are flagged: {', '.join(state.open_flags[:4])}"
        )

    del config
    blockers.extend(_rights_and_readiness_blockers(state, settings))
    # Same reason twice helps nobody; order is preserved so the first cause
    # stays first.
    return list(dict.fromkeys(blockers))


def _rights_and_readiness_blockers(state: AnchorReviewState, settings: Settings) -> list[str]:
    """Readiness findings that bear on the faces about to be broadcast.

    Deliberately narrowed to configuration and rights. A missing studio asset
    blocks the *render* and is caught by the final gate, but it says nothing
    about whether these presenter clips are usable — and a gate that refuses
    to let anyone look at the clips until the scenery is finished would stop
    the very review that is meant to happen early.
    """
    from btcedu.core.anchor_readiness import (
        AREA_CONFIGURATION,
        AREA_RIGHTS,
        evaluate_readiness,
    )

    try:
        report = evaluate_readiness(state.profile, settings, studio_mode=None, online=False)
    except Exception as exc:  # noqa: BLE001 - readiness is advisory here
        return [f"readiness could not be evaluated: {exc}"]

    relevant = {AREA_RIGHTS, AREA_CONFIGURATION}
    return [
        f"readiness: {result.detail}" for result in report.blocked if result.area in relevant
    ]


def ensure_review_task(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    config=None,
) -> ReviewTask:
    """The pending task for the current clips, creating it if needed.

    Any earlier task is superseded first. An approval that referred to clips
    which have since been replaced must not sit in the list looking actionable.
    """
    from btcedu.core.reviewer import (
        create_review_task,
        review_task_matches_artifacts,
        supersede_pending_reviews,
    )

    write_digest(session, episode_id, settings, config=config)
    artifacts = review_artifacts(settings, episode_id)

    task = latest_task(session, episode_id)
    if (
        task is not None
        and task.status in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value)
        and review_task_matches_artifacts(task, artifacts)
    ):
        return task

    supersede_pending_reviews(session, episode_id, REVIEW_STAGE)
    return create_review_task(
        session,
        episode_id,
        stage=REVIEW_STAGE,
        artifact_paths=artifacts,
    )


def has_current_approval(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    config=None,
) -> bool:
    """True only when the newest decision approved exactly these clips."""
    from btcedu.core.reviewer import review_task_matches_artifacts

    write_digest(session, episode_id, settings, config=config)
    task = latest_task(session, episode_id)
    if task is None or task.status != ReviewStatus.APPROVED.value:
        return False
    return review_task_matches_artifacts(task, review_artifacts(settings, episode_id))


def has_current_rejection(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    config=None,
) -> bool:
    """True when these exact clips were rejected and nothing has changed since."""
    from btcedu.core.reviewer import review_task_matches_artifacts

    write_digest(session, episode_id, settings, config=config)
    task = latest_task(session, episode_id)
    if task is None or task.status != ReviewStatus.REJECTED.value:
        return False
    return review_task_matches_artifacts(task, review_artifacts(settings, episode_id))


def approve(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    notes: str = "",
    expected_review_hash: str = "",
    operator_ref: str = "",
) -> ReviewTask:
    """Record that a human accepts these presenter clips.

    ``expected_review_hash`` is the fingerprint the dashboard displayed. If the
    clips moved on between the page load and the click, the operator would be
    approving something they never saw, so the mismatch is refused rather than
    resolved in their favour.
    """
    from btcedu.core.reviewer import approve_review

    state = collect_state(session, episode_id, settings)
    if not state.enabled:
        raise AnchorReviewError(
            f"No avatar review applies to {episode_id}: {state.reason or 'gate inactive'}"
        )
    if expected_review_hash and not _hash_matches(expected_review_hash, state.review_hash):
        raise StaleAnchorReviewError(
            "The presenter clips changed since this page was loaded. Reload the "
            "avatar review before approving."
        )
    if state.blockers:
        raise AnchorReviewError(
            "The avatar stage cannot be approved yet: " + "; ".join(state.blockers)
        )

    task = ensure_review_task(session, episode_id, settings)
    if task.status not in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value):
        raise AnchorReviewError(
            f"Avatar review {task.id} is '{task.status}' and cannot be approved again"
        )
    note = _decorate(notes, operator_ref)
    approve_review(session, int(task.id), notes=note)
    session.refresh(task)
    logger.info("Avatar stage approved for %s (task %d)", episode_id, task.id)
    return task


def reject(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    notes: str,
    expected_review_hash: str = "",
    operator_ref: str = "",
) -> ReviewTask:
    """Record that these presenter clips must not be used."""
    from btcedu.core.reviewer import reject_review

    if not notes.strip():
        raise AnchorReviewError("Rejecting the avatar stage requires a reason")

    state = collect_state(session, episode_id, settings)
    if not state.enabled:
        raise AnchorReviewError(
            f"No avatar review applies to {episode_id}: {state.reason or 'gate inactive'}"
        )
    if expected_review_hash and not _hash_matches(expected_review_hash, state.review_hash):
        raise StaleAnchorReviewError(
            "The presenter clips changed since this page was loaded. Reload the "
            "avatar review before rejecting."
        )

    task = ensure_review_task(session, episode_id, settings)
    if task.status not in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value):
        raise AnchorReviewError(
            f"Avatar review {task.id} is '{task.status}' and cannot be rejected again"
        )
    reject_review(session, int(task.id), notes=_decorate(notes, operator_ref))
    session.refresh(task)
    logger.warning("Avatar stage rejected for %s (task %d): %s", episode_id, task.id, notes)
    return task


def flag_scene(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    scene_id: str,
    note: str,
    operator_ref: str = "",
) -> None:
    """Complain about one scene without deciding the whole stage.

    A flag is a note, not a purchase: it neither orders a new clip nor blocks
    the ledger. It does block approval, which is the point — the operator has
    said something is wrong and has to either clear it or act on it.
    """
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    if not note.strip():
        raise AnchorReviewError("A scene complaint needs a note")

    state = collect_state(session, episode_id, settings)
    if not state.enabled:
        raise AnchorReviewError(f"No avatar review applies to {episode_id}")
    if scene_id not in {scene.scene_id for scene in state.scenes}:
        raise AnchorReviewError(f"{scene_id!r} is not a presenter scene of this episode")

    task = ensure_review_task(session, episode_id, settings)
    row = (
        session.query(ReviewItemDecision)
        .filter(
            ReviewItemDecision.review_task_id == task.id,
            ReviewItemDecision.item_id == scene_id,
        )
        .first()
    )
    text = _decorate(note, operator_ref)
    if row is None:
        session.add(
            ReviewItemDecision(
                review_task_id=int(task.id),
                item_id=scene_id,
                operation_type="anchor_scene",
                action=ReviewItemAction.REJECTED.value,
                edited_text=text,
                decided_at=_task_now(),
            )
        )
    else:
        row.action = ReviewItemAction.REJECTED.value
        row.edited_text = text
        row.decided_at = _task_now()
    session.commit()


def clear_scene_flag(session: Session, episode_id: str, *, scene_id: str) -> None:
    """Withdraw a complaint, e.g. after a regeneration was ordered."""
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    task = latest_task(session, episode_id)
    if task is None:
        return
    row = (
        session.query(ReviewItemDecision)
        .filter(
            ReviewItemDecision.review_task_id == task.id,
            ReviewItemDecision.item_id == scene_id,
        )
        .first()
    )
    if row is None:
        return
    row.action = ReviewItemAction.PENDING.value
    session.commit()


class StaleAnchorReviewError(AnchorReviewError):
    """The decision refers to clips that have since changed."""


def _hash_matches(expected: str, actual: str) -> bool:
    """Accept the shortened form the dashboard shows as well as the full hash."""
    expected = (expected or "").strip()
    if not expected:
        return True
    if expected == actual:
        return True
    return expected == _short(actual)


def _decorate(note: str, operator_ref: str) -> str:
    note = (note or "").strip()
    ref = (operator_ref or "").strip()[:64]
    if not ref:
        return note
    return f"[{ref}] {note}" if note else f"[{ref}]"


def _task_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
