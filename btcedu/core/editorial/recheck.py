"""Finding everything a change touches, and refusing to decide it (N6).

A published article rests on documents and pictures that keep changing after
it was written. This module records those dependencies, finds every use when
one of them moves, and queues a bounded number of rechecks.

A recheck can end in three ways: clear, review requested, or blocked. There is
deliberately no fourth outcome. Nothing here may approve, publish or re-publish
anything — the whole point of an automatic watcher is that it hands work to a
person rather than quietly deciding on their behalf.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.editorial.article import revision_claims
from btcedu.core.editorial.media import approved_revision_media
from btcedu.core.editorial.public import export_blockers
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import (
    EditorialRevision,
    EvidenceLink,
    SourceObservation,
    TopicSource,
)
from btcedu.models.media_rights import MediaUseDecision, NewsroomMediaAsset
from btcedu.models.publication import Publication, PublicationStatus
from btcedu.models.topic_graph import (
    DependencyKind,
    IssueStatus,
    PublicationDependency,
    RecheckJob,
    RecheckStatus,
    SourceIssue,
)

logger = logging.getLogger(__name__)

MAX_FANOUT = 50


class RecheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecheckOutcome:
    job_id: str
    status: str
    detail: str


def record_dependencies(
    session: Session, publication: Publication
) -> list[PublicationDependency]:
    """Write down what this publication stands on.

    Recorded at publish time rather than derived at query time, because the
    point of the record is to survive the article being redrafted: a source
    that a live page still cites must remain findable even after the newsroom
    moved on internally.
    """
    article = session.get(ArticleRevision, publication.current_article_revision_id)
    if article is None:
        return []
    revision = session.get(EditorialRevision, article.editorial_revision_id)
    refs: list[tuple[str, str]] = []

    claims = revision_claims(session, revision)
    if claims:
        links = (
            session.query(EvidenceLink)
            .filter(EvidenceLink.claim_revision_id.in_([c.id for c in claims]))
            .all()
        )
        for link in links:
            observation = session.get(SourceObservation, link.source_observation_id)
            if observation is not None:
                refs.append(
                    (DependencyKind.SOURCE_OBSERVATION.value, observation.observation_id)
                )

    for row in approved_revision_media(session, revision):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        if asset is not None:
            refs.append((DependencyKind.MEDIA_ASSET.value, asset.asset_id))

    for link in session.query(TopicSource).filter_by(topic_id=revision.topic_id).all():
        refs.append((DependencyKind.SOURCE_REVISION.value, str(link.source_revision_id)))

    created: list[PublicationDependency] = []
    for kind, ref in dict.fromkeys(refs):
        existing = (
            session.query(PublicationDependency)
            .filter_by(publication_id=publication.id, kind=kind, ref=ref)
            .one_or_none()
        )
        if existing is None:
            existing = PublicationDependency(
                publication_id=publication.id, kind=kind, ref=ref
            )
            session.add(existing)
        created.append(existing)
    session.commit()
    return created


def affected_publications(
    session: Session, *, kind: DependencyKind, ref: str
) -> list[Publication]:
    """Every publication a single changed source or asset reaches."""
    rows = (
        session.query(PublicationDependency)
        .filter_by(kind=kind.value, ref=ref)
        .order_by(PublicationDependency.publication_id)
        .all()
    )
    publications = []
    for row in rows:
        publication = session.get(Publication, row.publication_id)
        if publication is not None and publication not in publications:
            publications.append(publication)
    return publications


def open_issue(
    session: Session,
    *,
    kind: str,
    ref: str,
    detail: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SourceIssue:
    """Put a changed source or licence in the inbox, exactly once."""
    existing = (
        session.query(SourceIssue)
        .filter_by(kind=kind, ref=ref, status=IssueStatus.OPEN.value)
        .one_or_none()
    )
    if existing is not None:
        return existing
    issue = SourceIssue(
        issue_id=str(uuid.uuid4()),
        kind=kind,
        ref=ref,
        detail=detail,
        status=IssueStatus.OPEN.value,
        created_at=now(),
    )
    session.add(issue)
    session.commit()
    return issue


def resolve_issue(
    session: Session,
    issue: SourceIssue,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SourceIssue:
    issue.status = IssueStatus.RESOLVED.value
    issue.resolved_at = now()
    session.commit()
    return issue


def queue_rechecks(
    session: Session,
    *,
    kind: DependencyKind,
    ref: str,
    reason: str,
    max_fanout: int = MAX_FANOUT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[RecheckJob]:
    """Queue one job per affected publication, bounded and without duplicates.

    The cap is not tidiness: a licence change on a widely used picture would
    otherwise queue an unbounded number of paid rechecks at once.
    """
    jobs: list[RecheckJob] = []
    for publication in affected_publications(session, kind=kind, ref=ref):
        if len(jobs) >= max_fanout:
            logger.warning("Recheck fanout capped at %s for %s", max_fanout, ref)
            break
        existing = (
            session.query(RecheckJob)
            .filter_by(
                publication_id=publication.id,
                reason=reason,
                status=RecheckStatus.PENDING.value,
            )
            .one_or_none()
        )
        if existing is not None:
            jobs.append(existing)
            continue
        job = RecheckJob(
            job_id=str(uuid.uuid4()),
            publication_id=publication.id,
            reason=reason,
            status=RecheckStatus.PENDING.value,
            created_at=now(),
        )
        session.add(job)
        jobs.append(job)
    session.commit()
    return jobs


def run_recheck(
    session: Session,
    job: RecheckJob,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecheckOutcome:
    """Look again, then hand the answer to a person.

    A blocked publication is not withdrawn here. Withdrawal is a published
    statement about the newsroom's own work and stays an operator's decision;
    what this does is make sure the next build cannot carry the article
    silently, which ``export_blockers`` already guarantees.
    """
    publication = session.get(Publication, job.publication_id)
    if publication is None:
        raise RecheckError(f"Recheck job {job.job_id} has no publication")

    job.status = RecheckStatus.RUNNING.value
    session.commit()

    article = session.get(ArticleRevision, publication.current_article_revision_id)
    if article is None:
        job.status = RecheckStatus.BLOCKED.value
        job.detail = "Publication has no article revision"
    elif publication.status == PublicationStatus.WITHDRAWN.value:
        job.status = RecheckStatus.CLEAR.value
        job.detail = "Publication is already withdrawn"
    else:
        blockers = export_blockers(session, article)
        if blockers:
            job.status = RecheckStatus.BLOCKED.value
            job.detail = "; ".join(blockers)
        else:
            job.status = RecheckStatus.REVIEW_REQUESTED.value if job.reason.startswith(
                "review:"
            ) else RecheckStatus.CLEAR.value
            job.detail = ""
    job.completed_at = now()
    session.commit()
    return RecheckOutcome(job_id=job.job_id, status=job.status, detail=job.detail or "")


def pending_rechecks(session: Session, *, limit: int = MAX_FANOUT) -> Sequence[RecheckJob]:
    return (
        session.query(RecheckJob)
        .filter_by(status=RecheckStatus.PENDING.value)
        .order_by(RecheckJob.id)
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def protected_refs(session: Session) -> set[tuple[str, str]]:
    """Everything a live or withdrawn publication still needs on the record."""
    return {
        (row.kind, row.ref)
        for row in session.query(PublicationDependency).all()
    }


def purge_evidence_cache(
    session: Session,
    *,
    older_than: datetime,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Delete cached page bytes while keeping the publication record intact.

    Two retention classes, not one: the proof that an article rested on a
    document is permanent, the megabytes of that document are not. Only the
    body file is removed, and only for observations no publication depends on.

    The file goes first and the row is updated afterwards, so a crash between
    the two leaves a row pointing at a missing file — which the fetcher treats
    as a cache miss — rather than a row claiming bytes that are gone.
    """
    protected = {ref for kind, ref in protected_refs(session) if kind ==
                 DependencyKind.SOURCE_OBSERVATION.value}
    removed = 0
    kept = 0
    rows = (
        session.query(SourceObservation)
        .filter(SourceObservation.retrieved_at < older_than)
        .all()
    )
    for observation in rows:
        if observation.observation_id in protected or not observation.body_path:
            kept += 1
            continue
        if dry_run:
            removed += 1
            continue
        path = Path(observation.body_path)
        if path.is_file():
            path.unlink()
        observation.body_path = None
        removed += 1
    if not dry_run:
        session.commit()
    return removed, kept
