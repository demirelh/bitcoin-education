"""What the public may see, and what it takes to get there (N5).

Everything a reader receives passes through the dataclasses in this module.
They are an allowlist, not a filter: a field that is not named here cannot
reach a page, a feed or the search index, however convenient it would be.
That is the only defence that survives someone later adding a column to a
private table.

Nothing here writes a file or contacts anything. Publishing records an
intention in the database; building the site is a separate step.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from btcedu.core.editorial.article import (
    article_gate_reasons,
    current_article_state,
    revision_claims,
)
from btcedu.core.editorial.media import approved_revision_media
from btcedu.models.article import (
    ArticleParagraph,
    ArticleParagraphClaim,
    ArticleRevision,
    ArticleStatus,
    EditorialDecision,
    EditorialDecisionType,
)
from btcedu.models.editorial import (
    EditorialRevision,
    EvidenceLink,
    ResearchRun,
    SourceObservation,
)
from btcedu.models.media_rights import (
    LicenseEvidence,
    MediaUseDecision,
    NewsroomMediaAsset,
)
from btcedu.models.publication import (
    CorrectionKind,
    CorrectionNotice,
    Publication,
    PublicationStatus,
    PublicationVersion,
)

SUPPORTING_RELATIONS = frozenset({"supports", "partially_supports"})

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_TURKISH = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }
)


class PublicationError(RuntimeError):
    pass


class PublicationBlocked(PublicationError):
    def __init__(self, reasons: Sequence[str]):
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


def slugify(title: str, *, fallback: str = "haber") -> str:
    """A URL segment a Turkish headline can survive.

    ``ı`` and ``i`` are different letters, and NFKD alone turns the dotless one
    into nothing at all, so the mapping happens before normalisation.
    """
    # The Turkish suffix apostrophe joins a word rather than separating two,
    # so it disappears instead of becoming a hyphen: "Berlin'de" is one word.
    lowered = title.replace("'", "").replace("\u2019", "").translate(_TURKISH).casefold()
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:120].strip("-")
    return slug or fallback


def unique_slug(session: Session, title: str, *, publication_id: int | None = None) -> str:
    base = slugify(title)
    candidate = base
    suffix = 2
    while True:
        row = session.query(Publication).filter_by(slug=candidate).one_or_none()
        if row is None or row.id == publication_id:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicSource:
    title: str
    publisher: str
    url: str
    published_on: str | None
    retrieved_on: str


@dataclass(frozen=True)
class PublicMedia:
    file_name: str
    caption: str
    attribution: str
    license: str
    role: str = "event"
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class PublicParagraph:
    text: str
    sources: tuple[int, ...] = ()


@dataclass(frozen=True)
class PublicCorrection:
    kind: str
    summary: str
    published_on: str


@dataclass(frozen=True)
class PublicArticle:
    slug: str
    section: str
    language: str
    title: str
    lede: str
    published_on: str
    updated_on: str
    canonical_url: str
    status: str
    paragraphs: tuple[PublicParagraph, ...] = ()
    sources: tuple[PublicSource, ...] = ()
    media: tuple[PublicMedia, ...] = ()
    corrections: tuple[PublicCorrection, ...] = ()
    related: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    def search_document(self) -> dict:
        """The public-only search record: headline, lede and body, nothing else."""
        return {
            "slug": self.slug,
            "title": self.title,
            "section": self.section,
            "text": " ".join(
                [self.lede, *[paragraph.text for paragraph in self.paragraphs]]
            ),
            "published_on": self.published_on,
        }


def media_file_name(asset: NewsroomMediaAsset) -> str:
    return f"{asset.content_hash[:32]}{_EXTENSIONS.get(asset.mime_type, '.bin')}"


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def latest_research_run(session: Session, topic_id: int) -> ResearchRun | None:
    return (
        session.query(ResearchRun)
        .filter_by(topic_id=topic_id)
        .order_by(ResearchRun.id.desc())
        .first()
    )


def _approval(session: Session, article: ArticleRevision) -> EditorialDecision | None:
    return (
        session.query(EditorialDecision)
        .filter_by(
            article_revision_id=article.id,
            decision=EditorialDecisionType.APPROVE.value,
        )
        .order_by(EditorialDecision.id.desc())
        .first()
    )


def export_blockers(session: Session, article: ArticleRevision) -> tuple[str, ...]:
    """Why this article may not be offered publicly right now.

    The check is deliberately repeated at export time rather than trusted from
    approval: a licence can be revoked and an assessment can be weakened after
    someone clicked, and a static build would otherwise carry the old answer
    for as long as it is live.
    """
    reasons: list[str] = []
    if article.status != ArticleStatus.APPROVED.value:
        reasons.append(f"Article is {article.status}, not approved")

    decision = _approval(session, article)
    if decision is None:
        reasons.append("No operator approval recorded")

    revision = session.get(EditorialRevision, article.editorial_revision_id)
    run = latest_research_run(session, revision.topic_id)
    if run is None:
        reasons.append("No research run for this topic")
        return tuple(reasons)

    content, evidence, media = current_article_state(session, article, research_run=run)
    if decision is not None and (
        decision.reviewed_content_hash != content
        or decision.reviewed_evidence_hash != evidence
        or decision.reviewed_media_hash != media
    ):
        reasons.append("Approved state no longer matches the current state")

    reasons.extend(
        article_gate_reasons(session, editorial_revision=revision, research_run=run)
    )
    return tuple(reasons)


def publish_article(
    session: Session,
    article: ArticleRevision,
    *,
    operator_ref: str,
    section: str = "haber",
    correction_summary: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Publication:
    """Offer an approved article under a stable public identity.

    A second version of the same topic reuses the slug. The URL that was read
    keeps pointing at the newsroom's current answer, and the change is recorded
    as a correction rather than appearing as a new, unrelated article.
    """
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Publishing needs a named operator")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", section):
        raise ValueError("Section must be a single lowercase URL component")
    blockers = export_blockers(session, article)
    if blockers:
        raise PublicationBlocked(blockers)

    revision = session.get(EditorialRevision, article.editorial_revision_id)
    run = latest_research_run(session, revision.topic_id)
    content, evidence, media = current_article_state(session, article, research_run=run)
    decision = _approval(session, article)
    moment = now()

    publication = (
        session.query(Publication).filter_by(topic_id=revision.topic_id).one_or_none()
    )
    if publication is None:
        publication = Publication(
            publication_id=str(uuid.uuid4()),
            topic_id=revision.topic_id,
            slug=unique_slug(session, article.title),
            section=section,
            language=article.language,
            status=PublicationStatus.PUBLISHED.value,
            current_article_revision_id=article.id,
            first_published_at=moment,
            updated_at=moment,
        )
        session.add(publication)
        session.flush()
    else:
        if publication.current_article_revision_id != article.id:
            if not correction_summary.strip():
                raise ValueError(
                    "Replacing a published article needs a correction summary"
                )
            session.add(
                CorrectionNotice(
                    notice_id=str(uuid.uuid4()),
                    publication_id=publication.id,
                    kind=CorrectionKind.CORRECTION.value,
                    summary=correction_summary.strip(),
                    operator_ref=operator_ref,
                    created_at=moment,
                )
            )
            publication.status = PublicationStatus.CORRECTED.value
        else:
            publication.status = PublicationStatus.PUBLISHED.value
        publication.current_article_revision_id = article.id
        publication.section = section
        publication.updated_at = moment

    existing = (
        session.query(PublicationVersion)
        .filter_by(publication_id=publication.id, article_revision_id=article.id)
        .one_or_none()
    )
    if existing is None:
        session.add(
            PublicationVersion(
                version_id=str(uuid.uuid4()),
                publication_id=publication.id,
                article_revision_id=article.id,
                content_hash=content,
                evidence_hash=evidence,
                media_hash=media,
                decision_id=decision.decision_id,
                published_at=moment,
            )
        )
    session.commit()

    from btcedu.core.editorial.recheck import record_dependencies

    record_dependencies(session, publication)
    return publication


def withdraw_publication(
    session: Session,
    publication: Publication,
    *,
    operator_ref: str,
    summary: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CorrectionNotice:
    """Take an article back without pretending it never existed."""
    if not operator_ref or not operator_ref.strip():
        raise ValueError("A withdrawal needs a named operator")
    if not summary.strip():
        raise ValueError("A withdrawal needs a reason readers can see")
    moment = now()
    notice = CorrectionNotice(
        notice_id=str(uuid.uuid4()),
        publication_id=publication.id,
        kind=CorrectionKind.WITHDRAWAL.value,
        summary=summary.strip(),
        operator_ref=operator_ref,
        created_at=moment,
    )
    session.add(notice)
    publication.status = PublicationStatus.WITHDRAWN.value
    publication.updated_at = moment
    session.commit()
    return notice


# ---------------------------------------------------------------------------
# Rendering data
# ---------------------------------------------------------------------------


def _sources_for(
    session: Session, article: ArticleRevision, revision: EditorialRevision
) -> tuple[list[PublicSource], dict[int, int]]:
    """Public source list plus a claim-revision → source-index map."""
    claims = revision_claims(session, revision)
    if not claims:
        return [], {}
    run = latest_research_run(session, revision.topic_id)
    if run is None:
        raise PublicationBlocked(["No current research run"])
    links = (
        session.query(EvidenceLink)
        .join(SourceObservation, SourceObservation.id == EvidenceLink.source_observation_id)
        .filter(
            EvidenceLink.claim_revision_id.in_([claim.id for claim in claims]),
            EvidenceLink.relation.in_(sorted(SUPPORTING_RELATIONS)),
            SourceObservation.research_run_id == run.id,
            SourceObservation.fetch_status == "fetched",
        )
        .order_by(EvidenceLink.id)
        .all()
    )
    sources: list[PublicSource] = []
    index_by_observation: dict[int, int] = {}
    by_claim: dict[int, int] = {}
    for link in links:
        observation = session.get(SourceObservation, link.source_observation_id)
        if observation is None:
            continue
        if observation.id not in index_by_observation:
            index_by_observation[observation.id] = len(sources)
            sources.append(
                PublicSource(
                    title=observation.title or observation.canonical_url,
                    publisher=observation.publisher or "",
                    url=observation.canonical_url,
                    published_on=(
                        observation.published_at.date().isoformat()
                        if observation.published_at
                        else None
                    ),
                    retrieved_on=observation.retrieved_at.date().isoformat(),
                )
            )
        by_claim.setdefault(link.claim_revision_id, index_by_observation[observation.id])
    return sources, by_claim


def _media_for(session: Session, revision: EditorialRevision) -> list[PublicMedia]:
    items: list[PublicMedia] = []
    for row in approved_revision_media(session, revision):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        evidence = session.get(LicenseEvidence, decision.license_evidence_id)
        if asset is None:
            continue
        items.append(
            PublicMedia(
                file_name=media_file_name(asset),
                caption=row.caption or "",
                attribution=decision.attribution_text,
                license=evidence.license_id if evidence else "",
                role=decision.role,
                width=asset.width,
                height=asset.height,
            )
        )
    return items


def build_public_article(
    session: Session,
    publication: Publication,
    *,
    base_url: str,
) -> PublicArticle:
    """The reader's view of one publication, re-checked at build time."""
    for component in (publication.section, publication.slug):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", component):
            raise PublicationBlocked(("Invalid public URL component",))
    article = session.get(ArticleRevision, publication.current_article_revision_id)
    if article is None:
        raise PublicationBlocked(("Publication has no article revision",))
    blockers = export_blockers(session, article)
    if blockers:
        raise PublicationBlocked(blockers)

    revision = session.get(EditorialRevision, article.editorial_revision_id)
    # The public page names sources, never internal claim keys.
    sources, source_by_claim = _sources_for(session, article, revision)

    paragraphs: list[PublicParagraph] = []
    rows = (
        session.query(ArticleParagraph)
        .filter_by(article_revision_id=article.id)
        .order_by(ArticleParagraph.position)
        .all()
    )
    for row in rows:
        links = (
            session.query(ArticleParagraphClaim)
            .filter_by(article_paragraph_id=row.id)
            .order_by(ArticleParagraphClaim.id)
            .all()
        )
        indexes = []
        for link in links:
            index = source_by_claim.get(link.claim_revision_id)
            if index is not None and index not in indexes:
                indexes.append(index)
        paragraphs.append(PublicParagraph(text=row.text, sources=tuple(indexes)))

    corrections = tuple(
        PublicCorrection(
            kind=notice.kind,
            summary=notice.summary,
            published_on=notice.created_at.date().isoformat(),
        )
        for notice in session.query(CorrectionNotice)
        .filter_by(publication_id=publication.id)
        .order_by(CorrectionNotice.id)
        .all()
    )

    return PublicArticle(
        slug=publication.slug,
        section=publication.section,
        language=publication.language,
        title=article.title,
        lede=article.lede,
        published_on=publication.first_published_at.date().isoformat(),
        updated_on=publication.updated_at.date().isoformat(),
        canonical_url=f"{base_url.rstrip('/')}/{publication.section}/{publication.slug}/",
        status=publication.status,
        paragraphs=tuple(paragraphs),
        sources=tuple(sources),
        media=tuple(_media_for(session, revision)),
        corrections=corrections,
    )


def publishable(session: Session) -> list[Publication]:
    """Publications a build may include, in reverse chronological order."""
    return (
        session.query(Publication)
        .filter(
            Publication.status.in_(
                [PublicationStatus.PUBLISHED.value, PublicationStatus.CORRECTED.value]
            )
        )
        .order_by(Publication.first_published_at.desc(), Publication.id.desc())
        .all()
    )


def withdrawn(session: Session) -> list[Publication]:
    return (
        session.query(Publication)
        .filter_by(status=PublicationStatus.WITHDRAWN.value)
        .order_by(Publication.id)
        .all()
    )
