"""Operator reconciliation: closing a job whose outcome only a human can know.

The ledger deliberately refuses to guess. When a run dies between reserving a
clip and hearing back from HeyGen, the row is held, and nothing in the pipeline
will ever resolve it on its own. This module is the other half of that promise:
the small set of transitions an operator is allowed to record, each one demanding
evidence and a reason, each one written to an audit trail.

One rule shapes all of it. A provider job that cannot be found is *not* proof
that nothing was billed. HeyGen keeps finished videos for a limited window, so
a 404 four weeks later says only that the retention period expired. Treating
that as "free, retry it" is how an episode gets paid for twice, so the only
transitions that release a row for a fresh attempt are the ones where the
provider actively confirmed a refusal.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.avatar_jobs import (
    OUTCOME_ABANDONED,
    OUTCOME_DELIVERED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_NOT_BILLED,
    AvatarJobConflictError,
    resolve_job,
)
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit

logger = logging.getLogger(__name__)

# The operator decisions the CLI exposes, mapped to what each one means.
DECISION_RUNNING = "running"
DECISION_DELIVERED = "delivered"
DECISION_NOT_BILLED = "not-billed"
DECISION_UNRESOLVED = "unresolved"
DECISION_ABANDON = "abandon"

DECISIONS = (
    DECISION_RUNNING,
    DECISION_DELIVERED,
    DECISION_NOT_BILLED,
    DECISION_UNRESOLVED,
    DECISION_ABANDON,
)

DECISION_HELP = {
    DECISION_RUNNING: "Provider confirms the job exists and is still generating.",
    DECISION_DELIVERED: "Provider confirms the job finished; the clip was collected.",
    DECISION_NOT_BILLED: "Provider confirms it refused the request and did not bill it.",
    DECISION_UNRESOLVED: "Provider cannot identify the job; it stays blocked on purpose.",
    DECISION_ABANDON: "Operator gives the clip up. The cost stays on the episode.",
}

_DECISION_TO_OUTCOME = {
    DECISION_RUNNING: OUTCOME_IN_PROGRESS,
    DECISION_DELIVERED: OUTCOME_DELIVERED,
    DECISION_NOT_BILLED: OUTCOME_NOT_BILLED,
    DECISION_ABANDON: OUTCOME_ABANDONED,
}

_DECISION_TO_ACTION = {
    DECISION_RUNNING: AvatarAuditAction.CONFIRM_RUNNING,
    DECISION_DELIVERED: AvatarAuditAction.CONFIRM_DELIVERED,
    DECISION_NOT_BILLED: AvatarAuditAction.CONFIRM_NOT_BILLED,
    DECISION_UNRESOLVED: AvatarAuditAction.UNRESOLVED,
    DECISION_ABANDON: AvatarAuditAction.ABANDONED,
}


class ReconciliationError(RuntimeError):
    """The requested resolution is not permitted or not proven."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobView:
    """What an operator may see about one job. No secrets, no local paths."""

    job_id: int
    episode_id: str
    scene_id: str
    chapter_id: str
    status: str
    provider: str
    engine: str
    provider_job_id: str
    look_id: str
    reserved_cost_usd: float
    duration_seconds: float
    age_hours: float
    attempt_count: int
    note: str

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "episode_id": self.episode_id,
            "scene_id": self.scene_id,
            "chapter_id": self.chapter_id,
            "status": self.status,
            "provider": self.provider,
            "engine": self.engine,
            "provider_job_id": self.provider_job_id,
            "look_id": self.look_id,
            "reserved_cost_usd": round(self.reserved_cost_usd, 6),
            "duration_seconds": round(self.duration_seconds, 3),
            "age_hours": round(self.age_hours, 2),
            "attempt_count": self.attempt_count,
            "note": self.note,
        }


def _age_hours(job: AvatarJob, now: datetime | None = None) -> float:
    reserved = job.reserved_at
    if reserved is None:
        return 0.0
    if reserved.tzinfo is None:
        reserved = reserved.replace(tzinfo=UTC)
    return max(0.0, ((now or _utcnow()) - reserved).total_seconds() / 3600.0)


def job_view(job: AvatarJob, now: datetime | None = None) -> JobView:
    return JobView(
        job_id=int(job.id),
        episode_id=job.episode_id,
        scene_id=job.scene_id,
        chapter_id=job.chapter_id or "",
        status=job.status,
        provider=job.provider,
        engine=job.engine or "",
        provider_job_id=job.provider_job_id or "",
        look_id=job.avatar_look_id or "",
        reserved_cost_usd=float(job.cost_usd or 0.0),
        duration_seconds=float(job.duration_seconds or 0.0),
        age_hours=_age_hours(job, now),
        attempt_count=int(job.attempt_count or 0),
        note=(job.resolution_note or job.error_message or "")[:300],
    )


def list_jobs(
    session: Session,
    *,
    episode_id: str = "",
    unresolved_only: bool = True,
) -> list[JobView]:
    """Jobs an operator may need to act on, oldest first."""
    query = session.query(AvatarJob)
    if episode_id:
        query = query.filter(AvatarJob.episode_id == episode_id)
    if unresolved_only:
        query = query.filter(
            AvatarJob.status.in_(
                [
                    AvatarJobStatus.RESERVED.value,
                    AvatarJobStatus.RECONCILE_REQUIRED.value,
                ]
            )
        )
    jobs = query.order_by(AvatarJob.reserved_at, AvatarJob.id).all()
    now = _utcnow()
    return [job_view(job, now) for job in jobs]


def get_job_by_id(session: Session, job_id: int) -> AvatarJob:
    job = session.query(AvatarJob).filter(AvatarJob.id == int(job_id)).first()
    if job is None:
        raise ReconciliationError(f"No avatar job with id {job_id}")
    return job


def audit_entries(session: Session, job_id: int) -> list[AvatarJobAudit]:
    return (
        session.query(AvatarJobAudit)
        .filter(AvatarJobAudit.job_id == int(job_id))
        .order_by(AvatarJobAudit.created_at, AvatarJobAudit.id)
        .all()
    )


def next_safe_action(job: AvatarJob) -> str:
    """What an operator can do about this row without risking a second charge."""
    if job.status == AvatarJobStatus.COMPLETED.value:
        return "Nothing. The clip is complete and will be reused."
    if job.status == AvatarJobStatus.FAILED.value:
        return "Nothing. The provider refused without billing; the stage will retry it."
    if job.status == AvatarJobStatus.SUBMITTED.value:
        return (
            "Nothing. The provider job id is known, so the stage polls it "
            "instead of ordering again."
        )
    if job.status == AvatarJobStatus.ABANDONED.value:
        return "Nothing automatic. This clip was given up deliberately."
    if job.status == AvatarJobStatus.RESERVED.value:
        return (
            "Look the scene up in the HeyGen dashboard. The next pipeline run "
            "will move this row to reconcile_required rather than retry it."
        )
    if job.provider_job_id:
        return (
            f"Query provider job {job.provider_job_id} and record what it says with "
            "`avatar-reconcile resolve`."
        )
    return (
        "Search the HeyGen dashboard for a generation matching this episode and "
        "duration. If none is found, the safe resolution is 'unresolved' or "
        "'abandon' — never 'not-billed' on the strength of a 404 alone."
    )


def inspect_job(session: Session, job_id: int, *, outputs_dir: str | Path = "") -> dict:
    """Everything locally known about one job, with no provider access."""
    job = get_job_by_id(session, job_id)
    view = job_view(job)

    output_path = ""
    output_exists = False
    if job.output_path:
        candidate = Path(job.output_path)
        if not candidate.is_absolute() and outputs_dir:
            candidate = Path(outputs_dir) / candidate
        output_exists = candidate.is_file()
        # Only the tail of the path: the episode and file name are useful,
        # the deployment's directory layout is nobody's business.
        output_path = "/".join(candidate.parts[-3:])

    manifest_status = _manifest_status(job, outputs_dir)

    return {
        "schema_version": 1,
        "job": view.to_dict(),
        "hashes": {
            "content_hash": job.content_hash,
            "output_format": job.output_format,
        },
        "files": {
            "output_path": output_path,
            "output_exists": output_exists,
        },
        "manifest_status": manifest_status,
        "audit": [
            {
                "action": entry.action,
                "from_status": entry.from_status,
                "to_status": entry.to_status,
                "operator_ref": entry.operator_ref,
                "note": entry.note,
                "created_at": entry.created_at.isoformat() if entry.created_at else "",
            }
            for entry in audit_entries(session, job_id)
        ],
        "next_safe_action": next_safe_action(job),
    }


def _manifest_status(job: AvatarJob, outputs_dir: str | Path) -> str:
    if not outputs_dir:
        return "unknown"
    manifest = Path(outputs_dir) / job.episode_id / "anchor" / "manifest.json"
    if not manifest.is_file():
        return "absent"
    try:
        import json

        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    for scene in data.get("scenes", []) or []:
        if scene.get("scene_id") == job.scene_id:
            return str(scene.get("status") or "present")
    return "missing_scene"


def _record_audit(
    session: Session,
    job: AvatarJob,
    *,
    action: AvatarAuditAction,
    from_status: str,
    to_status: str,
    operator_ref: str,
    note: str,
    cost_before: float,
    cost_after: float,
) -> AvatarJobAudit:
    entry = AvatarJobAudit(
        episode_id=job.episode_id,
        job_id=int(job.id),
        scene_id=job.scene_id,
        action=action.value,
        from_status=from_status,
        to_status=to_status,
        provider_job_id=job.provider_job_id,
        operator_ref=operator_ref[:64],
        note=note[:2000],
        cost_before_usd=float(cost_before),
        cost_after_usd=float(cost_after),
        created_at=_utcnow(),
    )
    session.add(entry)
    session.commit()
    return entry


def attach_provider_job_id(
    session: Session,
    job_id: int,
    *,
    provider_job_id: str,
    operator_ref: str,
    note: str,
    confirm: bool = False,
) -> AvatarJob:
    """Bind a job id an operator found by hand to a held row.

    Deliberately its own step, ahead of any resolution. Attaching an id is the
    moment a human asserts "this generation over there is this scene over here",
    and if that assertion is wrong every later decision inherits the mistake.
    """
    if not confirm:
        raise ReconciliationError(
            "Attaching a provider job id by hand requires explicit confirmation"
        )
    if not provider_job_id.strip():
        raise ReconciliationError("A provider job id is required")
    if not note.strip():
        raise ReconciliationError("Attaching a provider job id requires a note")

    job = get_job_by_id(session, job_id)
    if job.status not in {
        AvatarJobStatus.RESERVED.value,
        AvatarJobStatus.RECONCILE_REQUIRED.value,
    }:
        raise ReconciliationError(
            f"Job {job_id} is '{job.status}': only a held job may be given a job id by hand"
        )
    if job.provider_job_id and job.provider_job_id != provider_job_id.strip():
        raise ReconciliationError(
            f"Job {job_id} already carries provider job id {job.provider_job_id!r}"
        )

    previous = job.status
    job.provider_job_id = provider_job_id.strip()
    session.commit()
    _record_audit(
        session,
        job,
        action=AvatarAuditAction.ATTACH_JOB_ID,
        from_status=previous,
        to_status=job.status,
        operator_ref=operator_ref,
        note=note,
        cost_before=float(job.cost_usd or 0.0),
        cost_after=float(job.cost_usd or 0.0),
    )
    return job


def resolve(
    session: Session,
    job_id: int,
    *,
    decision: str,
    note: str,
    operator_ref: str,
    output_path: str = "",
    duration_seconds: float = 0.0,
    cost_usd: float | None = None,
    provider_job_id: str = "",
    outputs_dir: str | Path = "",
) -> AvatarJob:
    """Record one operator decision about one held job."""
    if decision not in DECISIONS:
        raise ReconciliationError(
            f"Unknown decision {decision!r}. Valid: {', '.join(DECISIONS)}"
        )
    if not note.strip():
        raise ReconciliationError("Every manual resolution requires a reason")
    if not operator_ref.strip():
        raise ReconciliationError("Every manual resolution requires an operator reference")

    job = get_job_by_id(session, job_id)
    if job.status in {
        AvatarJobStatus.COMPLETED.value,
        AvatarJobStatus.FAILED.value,
        AvatarJobStatus.SUBMITTED.value,
    }:
        raise ReconciliationError(
            f"Job {job_id} is '{job.status}' and is not awaiting reconciliation. "
            "A settled job is never reopened."
        )
    if job.status == AvatarJobStatus.ABANDONED.value:
        raise ReconciliationError(
            f"Job {job_id} was abandoned. That decision is final; create new work "
            "with a new content hash instead."
        )
    if job.status == AvatarJobStatus.RESERVED.value:
        raise ReconciliationError(
            f"Job {job_id} is still 'reserved'. Run the pipeline once so the ledger "
            "promotes it to reconcile_required, which is the state these decisions apply to."
        )

    previous_status = job.status
    cost_before = float(job.cost_usd or 0.0)

    if decision == DECISION_UNRESOLVED:
        # No transition at all. The row is meant to stay blocked; what changes
        # is that the failed identification is now on the record.
        _record_audit(
            session,
            job,
            action=AvatarAuditAction.UNRESOLVED,
            from_status=previous_status,
            to_status=previous_status,
            operator_ref=operator_ref,
            note=note,
            cost_before=cost_before,
            cost_after=cost_before,
        )
        return job

    if decision == DECISION_DELIVERED:
        resolved_path = _validate_delivered(job, output_path, outputs_dir)
        output_path = resolved_path

    if decision == DECISION_NOT_BILLED and not provider_job_id and not job.provider_job_id:
        # Releasing a row for a fresh purchase is the only decision that can
        # cost money, so it needs the provider to have said something concrete.
        raise ReconciliationError(
            "Resolving as not-billed requires the provider job the refusal refers to. "
            "A generation nobody can find is not evidence that it was free — use "
            "'unresolved' or 'abandon' instead."
        )

    try:
        resolve_job(
            session,
            job,
            outcome=_DECISION_TO_OUTCOME[decision],
            note=note,
            output_path=output_path,
            duration_seconds=duration_seconds,
            cost_usd=cost_usd,
            provider_job_id=provider_job_id,
        )
    except AvatarJobConflictError as exc:
        raise ReconciliationError(str(exc)) from exc

    _record_audit(
        session,
        job,
        action=_DECISION_TO_ACTION[decision],
        from_status=previous_status,
        to_status=job.status,
        operator_ref=operator_ref,
        note=note,
        cost_before=cost_before,
        cost_after=float(job.cost_usd or 0.0),
    )
    return job


def _validate_delivered(job: AvatarJob, output_path: str, outputs_dir: str | Path) -> str:
    """A clip claimed as delivered has to actually be on disk."""
    candidate = (output_path or job.output_path or "").strip()
    if not candidate:
        raise ReconciliationError(
            "Resolving as delivered requires the path of the collected clip"
        )
    absolute = Path(candidate)
    if not absolute.is_absolute() and outputs_dir:
        absolute = Path(outputs_dir) / candidate
    if not absolute.is_file():
        raise ReconciliationError(
            f"No clip at {candidate}. Download the finished video before recording it as delivered."
        )
    if absolute.stat().st_size == 0:
        raise ReconciliationError(f"The clip at {candidate} is empty")
    return candidate


def _last_touched(job: AvatarJob) -> str:
    """The most recent timestamp this row carries.

    ``AvatarJob`` has no ``updated_at``: every transition writes its own
    column, which is more honest but means the newest of them has to be picked
    explicitly.
    """
    stamps = [
        job.completed_at,
        job.submitted_at,
        job.next_poll_at,
        job.reserved_at,
    ]
    from datetime import UTC as _UTC

    moments = [
        stamp.replace(tzinfo=_UTC) if stamp.tzinfo is None else stamp
        for stamp in stamps
        if stamp is not None
    ]
    latest = max(moments, default=None, key=lambda value: value.timestamp())
    return latest.isoformat() if latest else ""


def decision_digest(session: Session, job: AvatarJob) -> str:
    """Fingerprint of the row an operator is deciding about.

    The dashboard shows a job, the operator thinks, and only then clicks. In
    between, a pipeline run may have moved that very row. Binding the
    confirmation to this digest turns that race into a refusal instead of a
    decision applied to a different state.

    The audit trail is part of the fingerprint, and deliberately so. Recording
    "the provider cannot identify this job" changes no column at all, so a
    digest built from the row alone would still match afterwards and a second
    operator's contradicting decision would land unnoticed on top of the first.
    """
    import hashlib

    last = last_decision(session, int(job.id))
    payload = "|".join(
        str(part)
        for part in (
            job.id,
            job.status,
            job.provider_job_id or "",
            round(float(job.cost_usd or 0.0), 6),
            int(job.attempt_count or 0),
            _last_touched(job),
            last.id if last else 0,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def last_decision(session: Session, job_id: int) -> AvatarJobAudit | None:
    """The most recent operator decision about this job, if there is one."""
    return (
        session.query(AvatarJobAudit)
        .filter(AvatarJobAudit.job_id == int(job_id))
        .order_by(AvatarJobAudit.created_at.desc(), AvatarJobAudit.id.desc())
        .first()
    )


#: Which audit action each decision leaves behind. Used to recognise a repeated
#: confirmation — a double-click has to be one decision, not two.
DECISION_AUDIT_ACTION = {
    DECISION_RUNNING: AvatarAuditAction.CONFIRM_RUNNING.value,
    DECISION_DELIVERED: AvatarAuditAction.CONFIRM_DELIVERED.value,
    DECISION_NOT_BILLED: AvatarAuditAction.CONFIRM_NOT_BILLED.value,
    DECISION_UNRESOLVED: AvatarAuditAction.UNRESOLVED.value,
    DECISION_ABANDON: AvatarAuditAction.ABANDONED.value,
}
