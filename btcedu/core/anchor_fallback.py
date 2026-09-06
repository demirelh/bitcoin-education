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
module holds the policy, the record and the gate. Presenting the decision to an
operator is WP-5B's job — there is deliberately no code here that offers it.
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


class AnchorPolicyError(RuntimeError):
    """The avatar path cannot proceed and no override permits an alternative."""


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
    entry = (
        session.query(AvatarJobAudit)
        .filter(
            AvatarJobAudit.episode_id == episode_id,
            AvatarJobAudit.action == AvatarAuditAction.VOICE_OVER_OVERRIDE.value,
        )
        .order_by(AvatarJobAudit.created_at.desc(), AvatarJobAudit.id.desc())
        .first()
    )
    if entry is None:
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
