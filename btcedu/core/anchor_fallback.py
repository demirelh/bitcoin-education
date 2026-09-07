"""What happens when the presenter cannot be generated: nothing, loudly.

The old ALMANYA24 presentation was a full-frame picture with a voice-over. It
still exists in the code, which makes it a tempting automatic fallback — and
that temptation is exactly what this module refuses.

An episode that silently drops the presenter looks finished. It renders, it
passes the technical checks, and it publishes a bulletin in a format the channel
abandoned, without anybody being told. So while the avatar path is enabled a
missing, invalid or unreconciled clip stops the episode instead. The renderer
never chooses; it only reports.

A voice-over fallback remains possible, but only as an explicit decision, for
one named episode, with a reason, an audit entry and a fresh final review. This
module holds the policy, the record and the gate.

WP-7 added the operator's half: a two-step preparation and confirmation bound to
the artefacts an operator actually looked at, and a revocation for the case
where the presenter becomes available again before the bulletin goes out. Both
halves are recorded; neither invents a fallback on its own.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit

logger = logging.getLogger(__name__)

#: Statuses that make an episode unfit to render with the avatar path enabled.
UNUSABLE_STATUSES = frozenset(
    {
        AvatarJobStatus.RESERVED.value,
        AvatarJobStatus.RECONCILE_REQUIRED.value,
        AvatarJobStatus.ABANDONED.value,
        AvatarJobStatus.FAILED.value,
    }
)

ACTION_STOP = "stop"
ACTION_VOICE_OVER = "voice_over"

#: What the finished video is: a presenter in a studio, or the old full-frame
#: presentation with the same narration. Recorded in the render manifest so the
#: mode is never inferred from the pictures afterwards.
PRESENTATION_AVATAR = "avatar"
PRESENTATION_VOICE_OVER = "voice_over_override"


class AnchorPolicyError(RuntimeError):
    """The avatar path cannot proceed and no override permits an alternative."""


class StaleOverrideError(AnchorPolicyError):
    """The operator confirmed against artefacts that have since changed."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class VoiceOverOverride:
    """One operator's decision to publish one episode without a presenter."""

    episode_id: str
    operator_ref: str
    reason: str
    decided_at: datetime
    #: Always true. An override changes what the audience sees, so the finished
    #: video has to pass the final review again rather than inherit an approval
    #: that was granted for a different presentation.
    requires_final_review: bool = True

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "operator_ref": self.operator_ref,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "requires_final_review": self.requires_final_review,
        }


def record_voice_over_override(
    session: Session,
    episode_id: str,
    *,
    operator_ref: str,
    reason: str,
) -> VoiceOverOverride:
    """Permit exactly one episode to fall back to the voice-over presentation.

    Scoped to a single episode on purpose: a standing permission would quietly
    become the new normal the first time HeyGen had a bad night.
    """
    if not episode_id.strip():
        raise AnchorPolicyError("A voice-over override needs an episode")
    if not operator_ref.strip():
        raise AnchorPolicyError("A voice-over override needs an operator reference")
    if not reason.strip():
        raise AnchorPolicyError("A voice-over override needs a reason")

    now = _utcnow()
    entry = AvatarJobAudit(
        episode_id=episode_id.strip(),
        job_id=None,
        scene_id="",
        action=AvatarAuditAction.VOICE_OVER_OVERRIDE.value,
        from_status="anchor",
        to_status="voice_over",
        operator_ref=operator_ref.strip()[:64],
        note=reason.strip()[:2000],
        created_at=now,
    )
    session.add(entry)
    session.commit()
    logger.warning(
        "Episode %s will be rendered without the presenter by operator decision (%s)",
        episode_id,
        operator_ref,
    )
    return VoiceOverOverride(
        episode_id=episode_id.strip(),
        operator_ref=operator_ref.strip(),
        reason=reason.strip(),
        decided_at=now,
    )


def active_voice_over_override(session: Session, episode_id: str) -> VoiceOverOverride | None:
    """The override for this episode, if an operator recorded one."""
    # Both actions are read, newest first, because a revocation ends an override
    # without deleting it: the history has to keep saying that somebody once
    # decided to broadcast this episode without a presenter.
    entry = (
        session.query(AvatarJobAudit)
        .filter(
            AvatarJobAudit.episode_id == episode_id,
            AvatarJobAudit.action.in_(
                [
                    AvatarAuditAction.VOICE_OVER_OVERRIDE.value,
                    AvatarAuditAction.VOICE_OVER_REVOKED.value,
                ]
            ),
        )
        .order_by(AvatarJobAudit.created_at.desc(), AvatarJobAudit.id.desc())
        .first()
    )
    if entry is None or entry.action == AvatarAuditAction.VOICE_OVER_REVOKED.value:
        return None
    decided = entry.created_at or _utcnow()
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=UTC)
    return VoiceOverOverride(
        episode_id=entry.episode_id,
        operator_ref=entry.operator_ref,
        reason=entry.note,
        decided_at=decided,
    )


def unusable_jobs(session: Session, episode_id: str) -> list[AvatarJob]:
    """Avatar jobs that cannot supply a clip for this episode's render."""
    return (
        session.query(AvatarJob)
        .filter(
            AvatarJob.episode_id == episode_id,
            AvatarJob.status.in_(sorted(UNUSABLE_STATUSES)),
        )
        .order_by(AvatarJob.scene_id)
        .all()
    )


def failure_action(session: Session, episode_id: str) -> str:
    """What may happen to this episode if a presenter clip is missing."""
    override = active_voice_over_override(session, episode_id)
    return ACTION_VOICE_OVER if override else ACTION_STOP


def require_anchor_usable(session: Session, episode_id: str, *, anchor_enabled: bool) -> None:
    """Fail closed unless every avatar job for this episode can be used.

    Called by operator tooling, not by the renderer: the renderer already
    refuses an unusable clip scene by scene, and giving it a second, broader
    veto would make it look as though it were choosing between presentations.
    """
    if not anchor_enabled:
        return
    blocked = unusable_jobs(session, episode_id)
    if not blocked:
        return
    if failure_action(session, episode_id) == ACTION_VOICE_OVER:
        logger.warning(
            "Episode %s has %d unusable avatar jobs but carries a voice-over override",
            episode_id,
            len(blocked),
        )
        return
    detail = ", ".join(f"{job.scene_id}={job.status}" for job in blocked[:6])
    raise AnchorPolicyError(
        f"Episode {episode_id} has avatar jobs that cannot be rendered: {detail}. "
        "Resolve them with `btcedu avatar-reconcile`, or record an explicit "
        "voice-over override. The pipeline does not choose a fallback by itself."
    )


# ---------------------------------------------------------------------------
# The operator's two steps: prepare, then confirm
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverridePreparation:
    """Everything an operator has to see before dropping the presenter.

    Assembled read-only. Preparing changes nothing at all — no audit entry, no
    invalidated review, no stale marker — so an operator can look at the
    consequences and walk away.
    """

    episode_id: str
    digest: str
    anchor_scenes: list[dict]
    blockers: list[str]
    cost_actual_usd: float
    cost_reserved_usd: float
    cost_unresolved_usd: float
    consequences: list[str]
    already_active: bool = False

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "digest": self.digest,
            "anchor_scenes": list(self.anchor_scenes),
            "blockers": list(self.blockers),
            "cost_actual_usd": round(self.cost_actual_usd, 6),
            "cost_reserved_usd": round(self.cost_reserved_usd, 6),
            "cost_unresolved_usd": round(self.cost_unresolved_usd, 6),
            "consequences": list(self.consequences),
            "already_active": self.already_active,
        }


CONSEQUENCES = (
    "Die Moderatorin erscheint in dieser Sendung nicht.",
    "Die Anchor-Szenen werden mit dem bereits zugeordneten Themenmedium "
    "vollflächig gerendert; Ton, Reihenfolge und Kapitel bleiben unverändert.",
    "Eine vorhandene Anchor-Freigabe verliert ihre Gültigkeit.",
    "Der bestehende Render wird als veraltet markiert und neu erzeugt.",
    "Die fertige Sendung muss review_gate_3 erneut durchlaufen.",
    "Bereits entstandene, reservierte und ungeklärte Kosten bleiben bestehen.",
    "Es wird kein Providerauftrag storniert, gestartet oder wiederholt.",
)


def _episode_dir(settings, episode_id: str):
    from pathlib import Path

    return Path(settings.outputs_dir) / episode_id


def override_digest(session: Session, episode_id: str, settings) -> str:
    """Fingerprint of the state an operator is deciding about.

    Deliberately built from the same three things the decision depends on: the
    shot list, the presenter clips as they are recorded, and the jobs whose
    money is at stake. If any of them moves between the two clicks, the second
    click is refused rather than applied to something else.
    """
    import hashlib
    import json

    plan_file = _episode_dir(settings, episode_id) / "scene_plan.json"
    manifest_file = _episode_dir(settings, episode_id) / "anchor" / "manifest.json"

    def _read(path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    plan = _read(plan_file)
    manifest = _read(manifest_file)
    jobs = (
        session.query(AvatarJob)
        .filter(AvatarJob.episode_id == episode_id)
        .order_by(AvatarJob.scene_id, AvatarJob.id)
        .all()
    )
    payload = {
        "episode_id": episode_id,
        "plan_hash": str(plan.get("content_hash") or ""),
        "scenes": sorted(
            (
                str(entry.get("scene_id") or ""),
                str(entry.get("file_sha256") or ""),
                str(entry.get("status") or ""),
            )
            for entry in manifest.get("scenes", []) or []
        ),
        "jobs": sorted(
            (job.scene_id, job.status, round(float(job.cost_usd or 0.0), 6)) for job in jobs
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _anchor_scene_views(session: Session, episode_id: str, settings) -> list[dict]:
    """The scenes that will lose their presenter, and what replaces them."""
    import json

    from btcedu.core.scene_planner import ROLE_ANCHOR

    plan_file = _episode_dir(settings, episode_id) / "scene_plan.json"
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    jobs = {
        job.scene_id: job
        for job in session.query(AvatarJob).filter(AvatarJob.episode_id == episode_id).all()
    }
    views = []
    for scene in plan.get("scenes", []) or []:
        if not scene.get("needs_avatar") or scene.get("speaker_role") != ROLE_ANCHOR:
            continue
        job = jobs.get(str(scene.get("scene_id") or ""))
        views.append(
            {
                "scene_id": scene.get("scene_id", ""),
                "chapter_id": scene.get("chapter_id", ""),
                "order": int(scene.get("order") or 0),
                "job_status": job.status if job else "none",
                "cost_usd": round(float(job.cost_usd or 0.0), 6) if job else 0.0,
                # Empty means 'the chapter's own medium or the studio fallback
                # card'; the renderer resolves it exactly as it does for a
                # reporter scene, so no new asset is invented here.
                "replacement_media": str(scene.get("background_asset") or ""),
            }
        )
    views.sort(key=lambda item: (item["order"], item["scene_id"]))
    return views


def prepare_override(session: Session, episode_id: str, settings) -> OverridePreparation:
    """Assemble the decision without making it."""
    from btcedu.core.avatar_runtime import (
        RESERVED_STATUSES,
        SPENT_STATUSES,
        UNRESOLVED_STATUSES,
    )

    jobs = session.query(AvatarJob).filter(AvatarJob.episode_id == episode_id).all()

    def _sum(statuses) -> float:
        return sum(float(job.cost_usd or 0.0) for job in jobs if job.status in statuses)

    blocked = unusable_jobs(session, episode_id)
    blockers = [f"{job.scene_id}: {job.status}" for job in blocked]

    return OverridePreparation(
        episode_id=episode_id,
        digest=override_digest(session, episode_id, settings),
        anchor_scenes=_anchor_scene_views(session, episode_id, settings),
        blockers=blockers,
        cost_actual_usd=_sum(SPENT_STATUSES),
        cost_reserved_usd=_sum(RESERVED_STATUSES),
        cost_unresolved_usd=_sum(UNRESOLVED_STATUSES),
        consequences=list(CONSEQUENCES),
        already_active=active_voice_over_override(session, episode_id) is not None,
    )


def confirm_override(
    session: Session,
    episode_id: str,
    settings,
    *,
    operator_ref: str,
    reason: str,
    digest: str,
    acknowledged: bool,
) -> VoiceOverOverride:
    """Record the decision, invalidate what it contradicts, change nothing else.

    Idempotent: confirming an override that is already active returns the
    existing record. That is what makes a double-click harmless, and it is why
    the second click must not create a second audit entry — the history would
    then suggest two separate decisions were taken.
    """
    if not acknowledged:
        raise AnchorPolicyError(
            "The consequences of a voice-over override have to be acknowledged explicitly"
        )
    existing = active_voice_over_override(session, episode_id)
    if existing is not None:
        return existing

    current = override_digest(session, episode_id, settings)
    if digest and digest != current:
        raise StaleOverrideError(
            "The presenter clips or jobs changed while this decision was being taken; "
            "review the episode again before overriding it"
        )

    override = record_voice_over_override(
        session, episode_id, operator_ref=operator_ref, reason=reason
    )
    _invalidate_for_override(session, episode_id, settings)
    return override


def revoke_override(
    session: Session,
    episode_id: str,
    settings,
    *,
    operator_ref: str,
    reason: str,
) -> None:
    """Withdraw an override that has not been broadcast yet.

    The audit entry stays; only its effect ends. Afterwards the episode is back
    on the avatar path and therefore blocked again until every clip and review
    is valid — no provider job is started to get it there.
    """
    from btcedu.models.episode import Episode, EpisodeStatus

    if active_voice_over_override(session, episode_id) is None:
        raise AnchorPolicyError(f"Episode {episode_id} has no active voice-over override")
    if not operator_ref.strip() or not reason.strip():
        raise AnchorPolicyError("Revoking an override needs an operator reference and a reason")

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is not None and episode.status == EpisodeStatus.PUBLISHED:
        raise AnchorPolicyError(
            "This episode has already been published in the voice-over presentation; "
            "the record cannot be withdrawn after the fact"
        )

    session.add(
        AvatarJobAudit(
            episode_id=episode_id,
            job_id=None,
            scene_id="",
            action=AvatarAuditAction.VOICE_OVER_REVOKED.value,
            from_status="voice_over",
            to_status="anchor",
            operator_ref=operator_ref.strip()[:64],
            note=reason.strip()[:2000],
            created_at=_utcnow(),
        )
    )
    session.commit()
    _invalidate_for_override(session, episode_id, settings)
    logger.warning("Voice-over override for %s withdrawn by %s", episode_id, operator_ref)


def _invalidate_for_override(session: Session, episode_id: str, settings) -> None:
    """Undo the approvals a change of presentation makes meaningless.

    Two things and no more: the avatar review, because it was given to footage
    that will not be shown, and the render, because it shows the wrong thing.
    Narration, media and every other artefact are untouched — regenerating them
    would cost money for a change that is purely about what is on screen.
    """
    from btcedu.core.reviewer import supersede_pending_reviews
    from btcedu.models.review import ReviewStatus, ReviewTask

    tasks = (
        session.query(ReviewTask)
        .filter(ReviewTask.episode_id == episode_id, ReviewTask.stage.in_(["anchor", "render"]))
        .all()
    )
    for task in tasks:
        if task.status in (ReviewStatus.APPROVED.value, ReviewStatus.PENDING.value):
            task.status = ReviewStatus.SUPERSEDED.value
    try:
        supersede_pending_reviews(session, episode_id, "anchor")
    except Exception:  # noqa: BLE001 - superseding is best effort, the loop above is the record
        logger.debug("No pending anchor review to supersede for %s", episode_id)
    session.commit()

    render_dir = _episode_dir(settings, episode_id) / "render"
    if render_dir.is_dir():
        (render_dir / ".stale").write_text(
            "presentation mode changed (voice-over override)\n", encoding="utf-8"
        )


def presentation_mode(session: Session, episode_id: str) -> str:
    """What the finished video is, for the manifest and for the dashboard."""
    return (
        PRESENTATION_VOICE_OVER
        if active_voice_over_override(session, episode_id)
        else PRESENTATION_AVATAR
    )
