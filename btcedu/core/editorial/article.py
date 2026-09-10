"""Turkish article drafts bound to the evidence and rights they rest on.

A well-written paragraph is the easiest place for an unchecked statement to
hide, so the text is not trusted on its style. Every substantive sentence must
name the claim revision it is saying, numbers and attributions must survive
into the translation, and approval is recorded against the exact text,
evidence and pictures the operator had on screen.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from btcedu.core.editorial.ingest import canonical_hash
from btcedu.core.editorial.jobs import reserve_provider_operation
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
    Claim,
    ClaimAssessment,
    ClaimRevision,
    ClaimVerdict,
    EditorialRevision,
    ProviderOperationStatus,
    ResearchRun,
    RevisionClaim,
)
from btcedu.models.editorial_schema import ArticleDraft
from btcedu.models.media_rights import (
    LicenseEvidence,
    MediaSourceOffer,
    MediaUseDecision,
    NewsroomMediaAsset,
    RevisionMedia,
)

USABLE_VERDICTS = frozenset({ClaimVerdict.SUPPORTED.value, ClaimVerdict.PARTIAL.value})
BLOCKING_VERDICTS = frozenset(
    {
        ClaimVerdict.CONTRADICTED.value,
        ClaimVerdict.CONFLICTING.value,
        ClaimVerdict.INSUFFICIENT.value,
    }
)
UNCERTAINTY_MARKERS = (
    "iddia",
    "göre",
    "gore",
    "muhtemel",
    "olası",
    "olasi",
    "bildiril",
    "belirtil",
    "öne sür",
    "one sur",
    "laut",
    "angeblich",
    "mutmaß",
    "soll ",
)
_MARKUP = re.compile(r"<\s*/?\s*[a-zA-Z]|<\s*!|&#|javascript:", re.I)
_QUOTED = re.compile(r"[\"“”„»«']([^\"“”„»«']{4,})[\"“”„»«']")
_NUMBER = re.compile(r"\d[\d.,]*")


class ArticleGenerationError(RuntimeError):
    pass


class UnknownClaimReferenced(ArticleGenerationError):
    pass


class UnsupportedClaimReferenced(ArticleGenerationError):
    pass


class ArticleContentRejected(ArticleGenerationError):
    pass


class StaleArticleApproval(RuntimeError):
    pass


class ArticleApprovalBlocked(RuntimeError):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


def revision_claims(session: Session, editorial_revision: EditorialRevision) -> tuple[
    ClaimRevision, ...
]:
    rows = (
        session.query(ClaimRevision)
        .join(RevisionClaim, RevisionClaim.claim_revision_id == ClaimRevision.id)
        .filter(RevisionClaim.editorial_revision_id == editorial_revision.id)
        .order_by(ClaimRevision.id)
        .all()
    )
    return tuple(rows)


def claim_keys(
    session: Session,
    claims: Sequence[ClaimRevision],
) -> dict[int, str]:
    """Map each claim revision to its stable key, which lives on the claim."""
    if not claims:
        return {}
    rows = (
        session.query(ClaimRevision.id, Claim.claim_key)
        .join(Claim, Claim.id == ClaimRevision.claim_id)
        .filter(ClaimRevision.id.in_([claim.id for claim in claims]))
        .all()
    )
    return {revision_id: key for revision_id, key in rows}


def _assessments(
    session: Session,
    research_run: ResearchRun,
    claims: Sequence[ClaimRevision],
) -> dict[int, ClaimAssessment]:
    if not claims:
        return {}
    rows = (
        session.query(ClaimAssessment)
        .filter(
            ClaimAssessment.research_run_id == research_run.id,
            ClaimAssessment.claim_revision_id.in_([claim.id for claim in claims]),
        )
        .all()
    )
    return {row.claim_revision_id: row for row in rows}


def evidence_hash(
    session: Session,
    *,
    research_run: ResearchRun,
    claims: Sequence[ClaimRevision],
) -> str:
    assessments = _assessments(session, research_run, claims)
    from btcedu.models.editorial import (
        Claim,
        ClaimOrigin,
        EvidenceLink,
        SourceObservation,
        SourceRevision,
        SourceSpan,
        TopicSource,
    )

    def evidence_rows(claim):
        rows = (
            session.query(EvidenceLink, SourceObservation)
            .join(SourceObservation, SourceObservation.id == EvidenceLink.source_observation_id)
            .filter(
                EvidenceLink.claim_revision_id == claim.id,
                SourceObservation.research_run_id == research_run.id,
            )
            .order_by(EvidenceLink.id)
            .all()
        )
        return [
            {
                "link": {
                    col.name: getattr(link, col.name)
                    for col in EvidenceLink.__table__.columns
                },
                "source": {
                    col.name: getattr(source, col.name)
                    for col in SourceObservation.__table__.columns
                    if col.name != "body_path"
                },
            }
            for link, source in rows
        ]

    return canonical_hash(
        [
            {
                "claim": claim.revision_id,
                "claim_state": {
                    col.name: getattr(claim, col.name)
                    for col in ClaimRevision.__table__.columns
                },
                "documents": evidence_rows(claim),
                "topic_sources": [
                    {
                        col.name: getattr(source, col.name)
                        for col in SourceRevision.__table__.columns
                    }
                    for source in session.query(SourceRevision)
                    .join(TopicSource, TopicSource.source_revision_id == SourceRevision.id)
                    .join(Claim, Claim.topic_id == TopicSource.topic_id)
                    .filter(Claim.id == claim.claim_id).order_by(SourceRevision.id)
                ],
                "origins": [
                    {
                        "span": span.source_text,
                        "start": span.start_seconds,
                        "end": span.end_seconds,
                        "source": source.source_text,
                        "hash": source.content_hash,
                    }
                    for span, source in session.query(SourceSpan, SourceRevision)
                    .join(ClaimOrigin, ClaimOrigin.source_span_id == SourceSpan.id)
                    .join(SourceRevision, SourceRevision.id == SourceSpan.source_revision_id)
                    .filter(ClaimOrigin.claim_revision_id == claim.id)
                    .order_by(SourceSpan.id)
                ],
                "verdict": (
                    assessments[claim.id].verdict if claim.id in assessments else "unassessed"
                ),
                "evidence": (
                    assessments[claim.id].evidence_digest if claim.id in assessments else ""
                ),
                "assessment_state": (
                    {
                        col.name: getattr(assessments[claim.id], col.name)
                        for col in ClaimAssessment.__table__.columns
                    } if claim.id in assessments else None
                ),
            }
            for claim in sorted(claims, key=lambda item: item.revision_id)
        ]
    )


def media_hash(session: Session, editorial_revision: EditorialRevision) -> str:
    rows = approved_revision_media(session, editorial_revision)
    payload = []
    for row in rows:
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        evidence = session.get(LicenseEvidence, decision.license_evidence_id)
        offer = session.get(MediaSourceOffer, decision.media_source_offer_id)
        payload.append(
            {
                "position": row.position,
                "caption": row.caption,
                "decision": decision.decision_id,
                "attribution": decision.attribution_text,
                "asset": asset.content_hash,
                "use": {
                    col.name: getattr(decision, col.name)
                    for col in MediaUseDecision.__table__.columns
                },
                "license": {
                    col.name: getattr(evidence, col.name)
                    for col in LicenseEvidence.__table__.columns
                } if evidence else None,
                "offer": {
                    col.name: getattr(offer, col.name)
                    for col in MediaSourceOffer.__table__.columns
                } if offer else None,
            }
        )
    return canonical_hash(payload)


def article_gate_reasons(
    session: Session,
    *,
    editorial_revision: EditorialRevision,
    research_run: ResearchRun,
) -> tuple[str, ...]:
    """Why this article may not be approved yet.

    An open core claim blocks the whole article, not only the sentence that
    mentions it: publishing the rest would present a story whose central
    statement nobody could confirm.
    """
    claims = revision_claims(session, editorial_revision)
    assessments = _assessments(session, research_run, claims)
    keys = claim_keys(session, claims)
    reasons: list[str] = []
    from btcedu.core.editorial.research import assessment_input_hash

    for claim in claims:
        if not claim.material:
            continue
        key = keys.get(claim.id, claim.revision_id)
        assessment = assessments.get(claim.id)
        if assessment is None:
            reasons.append(f"core claim {key!r} has no evidence assessment")
        elif assessment.verdict not in USABLE_VERDICTS | {ClaimVerdict.UNVERIFIABLE.value}:
            reasons.append(f"core claim {key!r} is {assessment.verdict}")
        elif assessment.evidence_digest != assessment_input_hash(session, claim, research_run.id):
            reasons.append(f"core claim {key!r} needs a fresh evidence assessment")

    for row in session.query(RevisionMedia).filter_by(
        editorial_revision_id=editorial_revision.id
    ):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        if decision is None or decision.status != "approved" or decision.revoked_at is not None:
            reasons.append("Attached media has no current use approval")
            continue
        evidence = session.get(LicenseEvidence, decision.license_evidence_id)
        offer = session.get(MediaSourceOffer, decision.media_source_offer_id)
        if evidence is None or offer is None:
            reasons.append(f"media decision {decision.decision_id} lost its licence evidence")
            continue
        if evidence.attribution_required and not decision.attribution_text.strip():
            reasons.append(f"media decision {decision.decision_id} has no attribution text")
        if not evidence.commercial_use_allowed:
            reasons.append(f"media decision {decision.decision_id} is not cleared for use")
    return tuple(reasons)


def _checked_numbers(claims: Sequence[ClaimRevision]) -> set[str]:
    """Return the figures these claims were actually checked against.

    A claim carries its figure in ``numeric_value`` only when the extractor
    isolated one; "In Schleswig-Holstein gibt es aktuell rund 2.800 Sirenen"
    keeps the number in the statement alone. That number went through evidence
    linking with the rest of the sentence, so it is checked, and an article
    repeating it is not inventing a figure.
    """
    allowed: set[str] = set()
    for claim in claims:
        if claim.numeric_value:
            allowed.add(claim.numeric_value)
            allowed.update(_NUMBER.findall(claim.numeric_value))
        allowed.update(_NUMBER.findall(claim.statement or ""))
    return allowed


def _validate_paragraph(
    text: str,
    claims: Sequence[ClaimRevision],
    keys: dict[int, str] | None = None,
    *,
    allowed_numbers: frozenset[str] = frozenset(),
    quote_available: bool = False,
) -> None:
    """Check one piece of generated text against the claims it may rest on.

    Numbers and quotations are checked against the whole revision, not just the
    claims this paragraph cites, because a headline legitimately repeats a
    figure that the body paragraph carries.
    """
    if _MARKUP.search(text):
        raise ArticleContentRejected("Article text must be plain text without markup")

    quoted = [match.group(1).strip() for match in _QUOTED.finditer(text)]
    if quoted and not quote_available:
        raise ArticleContentRejected(
            "Paragraph presents a quotation without a quoted claim to back it"
        )

    keys = keys or {}
    for claim in claims:
        key = keys.get(claim.id, claim.revision_id)
        if (
            claim.numeric_value
            and any(char.isdigit() for char in claim.numeric_value)
            and claim.numeric_value not in text
        ):
            raise ArticleContentRejected(
                f"Paragraph drops the number {claim.numeric_value!r} of claim {key!r}"
            )
        if claim.attribution and claim.attribution not in text:
            raise ArticleContentRejected(
                f"Paragraph drops the attribution {claim.attribution!r} of claim {key!r}"
            )
        if claim.modality:
            lowered = text.casefold()
            if not any(marker in lowered for marker in UNCERTAINTY_MARKERS):
                raise ArticleContentRejected(
                    f"Paragraph states claim {key!r} as certain, "
                    "but the source qualified it"
                )

    numbers = set(_NUMBER.findall(text))
    allowed = set(allowed_numbers) | _checked_numbers(claims)
    invented = {
        number
        for number in numbers
        if number not in allowed
    }
    if invented:
        raise ArticleContentRejected(
            f"Paragraph introduces numbers without a claim: {sorted(invented)}"
        )


def _validate_draft_against_claims(
    draft,
    *,
    claims,
    by_key,
    assessments,
    keys,
) -> None:
    """Reject a draft that goes beyond the claims that were actually checked."""
    allowed_numbers = frozenset(_checked_numbers(claims))
    quote_available = any(claim.claim_type == "quote" for claim in claims)
    for paragraph in draft.paragraphs:
        if paragraph.kind == "body" and not paragraph.claim_keys:
            raise ArticleContentRejected("Every body paragraph must reference checked claims")
        unknown = [key for key in paragraph.claim_keys if key not in by_key]
        if unknown:
            raise UnknownClaimReferenced(
                f"Article references claims that are not in this revision: {sorted(unknown)}"
            )
        referenced = [by_key[key] for key in paragraph.claim_keys]
        for claim in referenced:
            assessment = assessments.get(claim.id)
            verdict = assessment.verdict if assessment else "unassessed"
            if verdict not in USABLE_VERDICTS and verdict != ClaimVerdict.UNVERIFIABLE.value:
                raise UnsupportedClaimReferenced(
                    f"Article asserts claim {keys[claim.id]!r} which is {verdict}"
                )
        _validate_paragraph(
            paragraph.text,
            referenced,
            keys,
            allowed_numbers=allowed_numbers,
            quote_available=quote_available,
        )
        for quote in _QUOTED.findall(paragraph.text):
            if not any(
                claim.claim_type == "quote" and quote in claim.statement for claim in referenced
            ):
                raise ArticleContentRejected("Quoted words are not present in a referenced claim")
    for text in (draft.title, draft.lede):
        _validate_paragraph(
            text,
            [],
            keys,
            allowed_numbers=allowed_numbers,
            quote_available=quote_available,
        )


def generate_article_revision(
    session: Session,
    *,
    editorial_revision: EditorialRevision,
    research_run: ResearchRun,
    drafter: Callable[[dict], dict],
    policy_version: str = "article-v1",
    language: str = "tr",
    model_name: str = "",
    prompt_hash: str | None = None,
    estimated_cost_usd: float = 0.0,
    repair_attempts: int = 1,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ArticleRevision:
    """Draft the article for one editorial revision, once per input state.

    Redrafting the same state reuses the stored revision instead of paying for
    a second "finalisation" of text that has not changed.
    """
    claims = revision_claims(session, editorial_revision)
    assessments = _assessments(session, research_run, claims)
    keys = claim_keys(session, claims)
    by_key = {keys[claim.id]: claim for claim in claims if claim.id in keys}
    current_evidence = evidence_hash(session, research_run=research_run, claims=claims)
    current_media = media_hash(session, editorial_revision)

    input_hash = canonical_hash(
        {
            "editorial_revision": editorial_revision.revision_id,
            "evidence": current_evidence,
            "media": current_media,
            "language": language,
            "policy": policy_version,
        }
    )
    existing = (
        session.query(ArticleRevision)
        .filter_by(
            editorial_revision_id=editorial_revision.id,
            language=language,
            evidence_hash=current_evidence,
            media_hash=current_media,
            policy_version=policy_version,
        )
        .order_by(ArticleRevision.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    operation = reserve_provider_operation(
        session,
        research_run=research_run,
        operation_key=f"article:{editorial_revision.revision_id}:{language}:{input_hash[:16]}",
        operation_type="article",
        provider="llm",
        model_name=model_name,
        input_hash=input_hash,
        estimated_cost_usd=estimated_cost_usd,
    )
    if operation.status in {
        ProviderOperationStatus.SUBMITTED.value,
        ProviderOperationStatus.RECONCILE_REQUIRED.value,
    }:
        raise ArticleGenerationError(
            f"Article operation {operation.operation_id} requires reconciliation before retry"
        )
    operation.status = ProviderOperationStatus.SUBMITTED.value
    session.commit()

    payload = {
        "task": "draft_article",
        "language": language,
        "claims": [
            {
                "claim_key": keys.get(claim.id, claim.revision_id),
                "statement": claim.statement,
                "claim_type": claim.claim_type,
                "subject": claim.subject,
                "numeric_value": claim.numeric_value,
                "unit": claim.unit,
                "attribution": claim.attribution,
                "modality": claim.modality,
                "verdict": (
                    assessments[claim.id].verdict if claim.id in assessments else "unassessed"
                ),
            }
            for claim in claims
        ],
        "untrusted_input": True,
    }
    attempt_payload = payload
    for remaining in range(repair_attempts, -1, -1):
        try:
            draft = ArticleDraft.model_validate(drafter(attempt_payload))
        except Exception as exc:
            operation.status = ProviderOperationStatus.RECONCILE_REQUIRED.value
            operation.error_message = str(exc)
            session.commit()
            raise

        try:
            _validate_draft_against_claims(
                draft, claims=claims, by_key=by_key, assessments=assessments, keys=keys
            )
            break
        except ArticleContentRejected as exc:
            if remaining:
                # Hand the deterministic verdict back once instead of asking
                # for the identical draft again. The gate is unchanged; only
                # the model is told what it has to repair.
                attempt_payload = {**payload, "rejected_reason": str(exc)}
                continue
            # The reply arrived and was judged: this is a decided outcome, not
            # an uncertain one. Leaving it in flight would make every later
            # attempt fail with "requires reconciliation before retry".
            operation.status = ProviderOperationStatus.FAILED.value
            operation.error_message = str(exc)
            operation.completed_at = now()
            session.commit()
            raise
        except Exception as exc:
            operation.status = ProviderOperationStatus.FAILED.value
            operation.error_message = str(exc)
            operation.completed_at = now()
            session.commit()
            raise

    content_hash = canonical_hash(
        {
            "title": draft.title,
            "lede": draft.lede,
            "paragraphs": [
                {
                    "kind": paragraph.kind,
                    "text": paragraph.text,
                    "claims": sorted(by_key[key].revision_id for key in paragraph.claim_keys),
                }
                for paragraph in draft.paragraphs
            ],
        }
    )
    article = ArticleRevision(
        article_revision_id=str(uuid.uuid4()),
        editorial_revision_id=editorial_revision.id,
        language=language,
        title=draft.title,
        lede=draft.lede,
        content_hash=content_hash,
        evidence_hash=current_evidence,
        media_hash=current_media,
        policy_version=policy_version,
        prompt_hash=prompt_hash,
        model_name=model_name,
        status=ArticleStatus.DRAFT.value,
        created_at=now(),
    )
    session.add(article)
    session.flush()
    for position, paragraph in enumerate(draft.paragraphs):
        row = ArticleParagraph(
            article_revision_id=article.id,
            position=position,
            kind=paragraph.kind,
            text=paragraph.text,
        )
        session.add(row)
        session.flush()
        for key in paragraph.claim_keys:
            session.add(
                ArticleParagraphClaim(
                    article_paragraph_id=row.id,
                    claim_revision_id=by_key[key].id,
                )
            )

    operation.status = ProviderOperationStatus.COMPLETED.value
    operation.completed_at = now()
    session.commit()
    return article


def current_article_state(
    session: Session,
    article: ArticleRevision,
    *,
    research_run: ResearchRun,
) -> tuple[str, str, str]:
    """The hashes that describe what a reviewer would see right now."""
    revision = session.get(EditorialRevision, article.editorial_revision_id)
    claims = revision_claims(session, revision)
    paragraphs = (
        session.query(ArticleParagraph)
        .filter_by(article_revision_id=article.id)
        .order_by(ArticleParagraph.position)
        .all()
    )
    content = canonical_hash({
        "title": article.title,
        "lede": article.lede,
        "paragraphs": [
            {
                "kind": paragraph.kind,
                "text": paragraph.text,
                "claims": sorted(
                    row.revision_id
                    for row in session.query(ClaimRevision)
                    .join(
                        ArticleParagraphClaim,
                        ArticleParagraphClaim.claim_revision_id == ClaimRevision.id,
                    )
                    .filter(ArticleParagraphClaim.article_paragraph_id == paragraph.id)
                ),
            }
            for paragraph in paragraphs
        ],
    })
    return (
        content,
        evidence_hash(session, research_run=research_run, claims=claims),
        media_hash(session, revision),
    )


def approve_article_revision(
    session: Session,
    article: ArticleRevision,
    *,
    research_run: ResearchRun,
    operator_ref: str,
    reviewed_content_hash: str,
    reviewed_evidence_hash: str,
    reviewed_media_hash: str,
    rationale: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EditorialDecision:
    """Record a named operator's approval of exactly what they reviewed.

    There is deliberately no automatic path into this function. The profile's
    review auto-approval setting governs the video pipeline and must not be
    able to release an article nobody read.
    """
    if not operator_ref or not operator_ref.strip():
        raise ValueError("An editorial approval needs a named operator")

    content, evidence, media = current_article_state(
        session, article, research_run=research_run
    )
    if (
        reviewed_content_hash != content
        or reviewed_evidence_hash != evidence
        or reviewed_media_hash != media
    ):
        raise StaleArticleApproval(
            "Text, evidence or media changed after the preview; review the new state"
        )

    revision = session.get(EditorialRevision, article.editorial_revision_id)
    reasons = article_gate_reasons(
        session, editorial_revision=revision, research_run=research_run
    )
    if reasons:
        article.status = ArticleStatus.DRAFT.value
        article.block_reason = "; ".join(reasons)
        session.commit()
        raise ArticleApprovalBlocked(reasons)
    if (article.content_hash, article.evidence_hash, article.media_hash) != (
        content, evidence, media
    ):
        raise StaleArticleApproval("Revision changed; generate and review a new revision")

    decision = EditorialDecision(
        decision_id=str(uuid.uuid4()),
        article_revision_id=article.id,
        decision=EditorialDecisionType.APPROVE.value,
        operator_ref=operator_ref,
        rationale=rationale,
        reviewed_content_hash=content,
        reviewed_evidence_hash=evidence,
        reviewed_media_hash=media,
        created_at=now(),
    )
    session.add(decision)
    article.status = ArticleStatus.APPROVED.value
    article.block_reason = None
    session.commit()
    return decision


def reject_article_revision(
    session: Session,
    article: ArticleRevision,
    *,
    research_run: ResearchRun,
    operator_ref: str,
    rationale: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EditorialDecision:
    if not operator_ref or not operator_ref.strip():
        raise ValueError("An editorial rejection needs a named operator")
    content, evidence, media = current_article_state(
        session, article, research_run=research_run
    )
    decision = EditorialDecision(
        decision_id=str(uuid.uuid4()),
        article_revision_id=article.id,
        decision=EditorialDecisionType.REJECT.value,
        operator_ref=operator_ref,
        rationale=rationale,
        reviewed_content_hash=content,
        reviewed_evidence_hash=evidence,
        reviewed_media_hash=media,
        created_at=now(),
    )
    session.add(decision)
    article.status = ArticleStatus.REJECTED.value
    session.commit()
    return decision


def article_preview(
    session: Session,
    article: ArticleRevision,
    *,
    research_run: ResearchRun,
) -> dict:
    """A private review view: text, its evidence and its credits side by side.

    Everything is plain data. Nothing here is an export, and nothing leaves the
    machine — the point is that a reviewer sees the gaps next to the prose,
    because fluent text is exactly what hides them.
    """
    revision = session.get(EditorialRevision, article.editorial_revision_id)
    claims = revision_claims(session, revision)
    assessments = _assessments(session, research_run, claims)
    by_id = {claim.id: claim for claim in claims}
    keys = claim_keys(session, claims)

    paragraphs = []
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
        paragraphs.append(
            {
                "position": row.position,
                "kind": row.kind,
                "text": row.text,
                "claims": [
                    {
                        "claim_key": keys.get(
                            link.claim_revision_id,
                            by_id[link.claim_revision_id].revision_id,
                        ),
                        "statement": by_id[link.claim_revision_id].statement,
                        "verdict": (
                            assessments[link.claim_revision_id].verdict
                            if link.claim_revision_id in assessments
                            else "unassessed"
                        ),
                        "rationale": (
                            assessments[link.claim_revision_id].rationale
                            if link.claim_revision_id in assessments
                            else ""
                        ),
                    }
                    for link in links
                ],
            }
        )

    media = []
    for row in approved_revision_media(session, revision):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        evidence = session.get(LicenseEvidence, decision.license_evidence_id)
        media.append(
            {
                "position": row.position,
                "caption": row.caption,
                "role": decision.role,
                "attribution": decision.attribution_text,
                "license": evidence.license_id if evidence else "",
                "content_hash": asset.content_hash if asset else "",
            }
        )

    content, evidence_state, media_state = current_article_state(
        session, article, research_run=research_run
    )
    return {
        "article_revision_id": article.article_revision_id,
        "language": article.language,
        "status": article.status,
        "title": article.title,
        "lede": article.lede,
        "paragraphs": paragraphs,
        "media": media,
        "open_issues": list(
            article_gate_reasons(
                session, editorial_revision=revision, research_run=research_run
            )
        ),
        "content_hash": content,
        "evidence_hash": evidence_state,
        "media_hash": media_state,
        "unreferenced_claims": [
            {
                "claim_key": keys.get(claim.id, claim.revision_id),
                "verdict": (
                    assessments[claim.id].verdict if claim.id in assessments else "unassessed"
                ),
            }
            for claim in claims
            if claim.material
            and not session.query(ArticleParagraphClaim)
            .join(
                ArticleParagraph,
                ArticleParagraph.id == ArticleParagraphClaim.article_paragraph_id,
            )
            .filter(
                ArticleParagraph.article_revision_id == article.id,
                ArticleParagraphClaim.claim_revision_id == claim.id,
            )
            .count()
        ],
    }


def revision_media_rows(
    session: Session, editorial_revision: EditorialRevision
) -> tuple[RevisionMedia, ...]:
    return approved_revision_media(session, editorial_revision)
