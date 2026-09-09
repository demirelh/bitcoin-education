"""What the newsroom actually produced, counted from its own records (N8).

The numbers here answer operational questions that otherwise get answered by
impression: how much of what we said was checked against independent sources,
how much of our evidence comes from one place, what the research cost, and what
is waiting for a person. Nothing is fetched and nothing is inferred — every
figure is a count over rows that already exist.

The one judgement built in is about independence: evidence that reaches us
through a single provenance family is one source, however many URLs it wore.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from btcedu.models.article import ArticleRevision, ArticleStatus
from btcedu.models.editorial import (
    ClaimAssessment,
    ClaimRevision,
    EvidenceLink,
    ProviderOperation,
    ProviderOperationStatus,
    ResearchRun,
    SourceObservation,
)
from btcedu.models.media_rights import MediaUseDecision
from btcedu.models.publication import Publication, PublicationStatus
from btcedu.models.topic_graph import (
    IssueStatus,
    RecheckJob,
    RecheckStatus,
    SourceIssue,
)

#: Relations that count as support for a claim.
SUPPORTING = ("supports",)


@dataclass
class SourceReport:
    evidence_links: int = 0
    distinct_families: int = 0
    top_families: list[tuple[str, int]] = field(default_factory=list)
    top_publishers: list[tuple[str, int]] = field(default_factory=list)
    #: Share of all evidence coming from the single largest family (0.0-1.0).
    largest_family_share: float = 0.0
    single_family_claims: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)


@dataclass
class CostReport:
    research_runs: int = 0
    total_cost_usd: float = 0.0
    budgeted_cost_usd: float = 0.0
    operations: int = 0
    failed_operations: int = 0
    estimated_operations: int = 0
    by_provider: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class FetchReport:
    observations: int = 0
    by_status: list[tuple[str, int]] = field(default_factory=list)
    rate_limited: int = 0
    cached_bodies: int = 0


@dataclass
class NewsroomReport:
    generated_at: datetime | None = None
    since: datetime | None = None
    claims_by_verdict: list[tuple[str, int]] = field(default_factory=list)
    articles_by_status: list[tuple[str, int]] = field(default_factory=list)
    publications_by_status: list[tuple[str, int]] = field(default_factory=list)
    sources: SourceReport = field(default_factory=SourceReport)
    cost: CostReport = field(default_factory=CostReport)
    fetches: FetchReport = field(default_factory=FetchReport)
    pending_rechecks: int = 0
    open_issues: int = 0
    media_by_status: list[tuple[str, int]] = field(default_factory=list)
    revoked_media: int = 0


def _family_of(observation: SourceObservation) -> str:
    """One name for everything that is really the same source.

    Falls back to the host when no provenance family was recorded, which is
    still better than treating two paths on one domain as two witnesses.
    """
    if observation.provenance_family:
        return observation.provenance_family
    host = urlparse(observation.canonical_url or "").netloc.lower()
    return host or "unknown"


def _counts(pairs) -> list[tuple[str, int]]:
    return sorted(Counter(pairs).items(), key=lambda item: (-item[1], item[0]))


def _source_report(session: Session) -> SourceReport:
    report = SourceReport()
    links = session.query(
        EvidenceLink.source_observation_id, EvidenceLink.provenance_family,
        EvidenceLink.relation, EvidenceLink.claim_revision_id,
    ).all()
    observations = {row.id: row for row in session.query(
        SourceObservation.id, SourceObservation.provenance_family,
        SourceObservation.canonical_url, SourceObservation.publisher,
        SourceObservation.fetch_status,
    )}

    families: list[str] = []
    publishers: list[str] = []
    per_claim: dict[int, set[str]] = {}
    for link in links:
        observation = observations.get(link.source_observation_id)
        if observation is None:
            continue
        family = link.provenance_family or _family_of(observation)
        families.append(family)
        if observation.publisher:
            publishers.append(observation.publisher)
        if observation.fetch_status == "fetched" and link.relation in SUPPORTING:
            per_claim.setdefault(link.claim_revision_id, set()).add(family)

    report.evidence_links = len(links)
    report.distinct_families = len(set(families))
    report.top_families = _counts(families)[:10]
    report.top_publishers = _counts(publishers)[:10]
    if families:
        report.largest_family_share = round(
            report.top_families[0][1] / len(families), 4
        )

    material = (
        session.query(ClaimRevision.id, ClaimRevision.revision_id)
        .filter(ClaimRevision.material.is_(True)).yield_per(100)
    )
    for claim in material:
        supporting = per_claim.get(claim.id, set())
        if not supporting:
            report.unsupported_claims.append(claim.revision_id)
        elif len(supporting) == 1:
            report.single_family_claims.append(claim.revision_id)
    return report


def _cost_report(session: Session) -> CostReport:
    report = CostReport()
    runs = session.query(ResearchRun.max_cost_usd).all()
    report.research_runs = len(runs)
    report.budgeted_cost_usd = round(sum(run.max_cost_usd for run in runs), 4)

    operations = session.query(
        ProviderOperation.actual_cost_usd, ProviderOperation.estimated_cost_usd,
        ProviderOperation.provider, ProviderOperation.status,
    ).all()
    report.operations = len(operations)
    per_provider: Counter[str] = Counter()
    total = 0.0
    for operation in operations:
        if operation.actual_cost_usd is None:
            report.estimated_operations += 1
        cost = (
            operation.actual_cost_usd
            if operation.actual_cost_usd is not None
            else operation.estimated_cost_usd
        )
        total += cost or 0.0
        per_provider[operation.provider] += cost or 0.0
        if operation.status == ProviderOperationStatus.FAILED.value:
            report.failed_operations += 1
    report.total_cost_usd = round(total, 4)
    report.by_provider = sorted(
        ((name, round(value, 4)) for name, value in per_provider.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return report


def _fetch_report(session: Session) -> FetchReport:
    report = FetchReport()
    observations = session.query(
        SourceObservation.fetch_status, SourceObservation.http_status, SourceObservation.body_path,
    ).all()
    report.observations = len(observations)
    report.by_status = _counts(row.fetch_status for row in observations)
    report.rate_limited = sum(1 for row in observations if row.http_status == 429)
    report.cached_bodies = sum(1 for row in observations if row.body_path)
    return report


def build_report(session: Session, *, now=None) -> NewsroomReport:
    """Count everything the newsroom has on record."""
    report = NewsroomReport(generated_at=now() if now else None)

    report.claims_by_verdict = _counts(
        row.verdict for row in session.query(ClaimAssessment.verdict).yield_per(100)
    )
    report.articles_by_status = _counts(
        row.status for row in session.query(ArticleRevision.status).yield_per(100)
    )
    report.publications_by_status = _counts(
        row.status for row in session.query(Publication.status).yield_per(100)
    )
    report.sources = _source_report(session)
    report.cost = _cost_report(session)
    report.fetches = _fetch_report(session)
    report.pending_rechecks = (
        session.query(RecheckJob).filter_by(status=RecheckStatus.PENDING.value).count()
    )
    report.open_issues = (
        session.query(SourceIssue).filter_by(status=IssueStatus.OPEN.value).count()
    )
    decisions = session.query(MediaUseDecision.status, MediaUseDecision.revoked_at).all()
    report.media_by_status = _counts(row.status for row in decisions)
    report.revoked_media = sum(1 for row in decisions if row.revoked_at is not None)
    return report


def report_warnings(report: NewsroomReport) -> tuple[str, ...]:
    """The findings an operator should not have to read a table to notice."""
    warnings: list[str] = []
    if report.sources.largest_family_share >= 0.5 and report.sources.evidence_links:
        family = report.sources.top_families[0][0]
        warnings.append(
            f"{report.sources.largest_family_share:.0%} of all evidence comes from "
            f"{family}; that is one source, not a consensus"
        )
    if report.sources.single_family_claims:
        warnings.append(
            f"{len(report.sources.single_family_claims)} material claim(s) rest on a "
            "single provenance family"
        )
    if report.sources.unsupported_claims:
        warnings.append(
            f"{len(report.sources.unsupported_claims)} material claim(s) have no "
            "supporting evidence at all"
        )
    if report.cost.failed_operations:
        warnings.append(f"{report.cost.failed_operations} provider operation(s) failed")
    if report.cost.estimated_operations:
        warnings.append(
            f"{report.cost.estimated_operations} operation(s) have estimated, not billed, costs"
        )
    if report.fetches.rate_limited:
        warnings.append(f"{report.fetches.rate_limited} fetch(es) were rate limited")
    if report.open_issues:
        warnings.append(f"{report.open_issues} source issue(s) waiting for a person")
    if report.pending_rechecks:
        warnings.append(f"{report.pending_rechecks} recheck(s) pending")
    unpublished = dict(report.articles_by_status).get(ArticleStatus.APPROVED.value, 0)
    published = dict(report.publications_by_status).get(
        PublicationStatus.PUBLISHED.value, 0
    )
    if unpublished and not published:
        warnings.append(f"{unpublished} approved article(s) are not published")
    return tuple(warnings)


def format_report(report: NewsroomReport) -> str:
    """A plain-text rendering for the terminal."""
    lines: list[str] = []

    def section(title: str, rows) -> None:
        lines.append(title)
        if not rows:
            lines.append("  (none)")
            return
        for name, value in rows:
            lines.append(f"  {name:<28} {value}")

    section("claims by verdict", report.claims_by_verdict)
    section("articles by status", report.articles_by_status)
    section("publications by status", report.publications_by_status)
    section("evidence by provenance family", report.sources.top_families)
    section("evidence by publisher", report.sources.top_publishers)
    section("fetches by status", report.fetches.by_status)
    section("research cost by provider (USD)", report.cost.by_provider)
    section("media decisions by status", report.media_by_status)

    lines.append("totals")
    lines.append(f"  evidence links               {report.sources.evidence_links}")
    lines.append(f"  distinct provenance families {report.sources.distinct_families}")
    lines.append(f"  research runs                {report.cost.research_runs}")
    lines.append(f"  research cost (USD)          {report.cost.total_cost_usd}")
    lines.append(f"  cached evidence bodies       {report.fetches.cached_bodies}")
    lines.append(f"  revoked media decisions      {report.revoked_media}")

    warnings = report_warnings(report)
    lines.append("warnings")
    if not warnings:
        lines.append("  (none)")
    for warning in warnings:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)
