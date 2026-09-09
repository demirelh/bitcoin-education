"""Resumable, data-only evidence research for editorial claim revisions."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from btcedu.core.editorial.ingest import canonical_hash
from btcedu.core.editorial.jobs import EditorialBudgetExceeded, reserve_provider_operation
from btcedu.models.editorial import (
    ClaimAssessment,
    ClaimRevision,
    ClaimVerdict,
    EvidenceLink,
    EvidenceRelation,
    FetchStatus,
    ProviderOperationStatus,
    ResearchQuery,
    ResearchQueryStatus,
    ResearchRun,
    ResearchRunStatus,
    SourceObservation,
)
from btcedu.models.editorial_schema import (
    ClaimDraft,
    EvidenceDraft,
    ResearchQueryPlan,
    SearchHitData,
)
from btcedu.services.document_fetcher import (
    DocumentFetcher,
    DocumentFetchError,
    FetchedDocument,
    UnsafeDocumentURL,
    UnsupportedDocument,
)
from btcedu.services.search_service import SearchHit, SearchProvider, SearchResponse


class EvidenceEvaluator(Protocol):
    def evaluate(
        self,
        claim: ClaimRevision,
        observations: tuple[SourceObservation, ...],
    ) -> tuple[EvidenceDraft, ...]: ...


class ResearchDeadlineExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchOutcome:
    """Result of one bounded research pass over a revision's claims."""

    assessments: tuple[ClaimAssessment, ...]
    researched_revision_ids: tuple[str, ...]
    skipped_revision_ids: tuple[str, ...]
    blocked_revision_id: str | None = None
    block_reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.block_reason is not None


class DataOnlyClaimExtractor:
    """Validate structured claim output from a caller with no tool surface."""

    def __init__(self, caller: Callable[[dict], object]) -> None:
        self.caller = caller

    def extract(self, *, source_text: str, language: str, span_id: str) -> tuple[ClaimDraft, ...]:
        payload = {
            "task": "extract_claims",
            "source": {
                "language": language,
                "span_id": span_id,
                "text": source_text,
            },
            "allowed_types": [
                "transcription_error",
                "fact",
                "quote",
                "opinion",
                "prediction",
                "unverifiable",
            ],
        }
        result = self.caller(payload)
        if not isinstance(result, list):
            raise ValueError("Claim extractor must return a list")
        return tuple(ClaimDraft.model_validate(item) for item in result)


class DataOnlyEvidenceEvaluator:
    """Validate evaluator output while exposing only bounded document data."""

    def __init__(self, caller: Callable[[dict], object], *, max_text_chars: int = 40_000) -> None:
        self.caller = caller
        self.max_text_chars = max_text_chars

    def evaluate(
        self,
        claim: ClaimRevision,
        observations: tuple[SourceObservation, ...],
    ) -> tuple[EvidenceDraft, ...]:
        payload = {
            "task": "evaluate_claim_evidence",
            "claim": {
                "revision_id": claim.revision_id,
                "statement": claim.statement,
                "claim_type": claim.claim_type,
                "subject": claim.subject,
                "location": claim.location,
                "event_date": claim.event_date,
                "numeric_value": claim.numeric_value,
                "unit": claim.unit,
                "attribution": claim.attribution,
                "modality": claim.modality,
            },
            "documents": [
                {
                    "canonical_url": item.canonical_url,
                    "publisher": item.publisher,
                    "title": item.title,
                    "language": item.language,
                    "published_at": item.published_at.isoformat()
                    if item.published_at
                    else None,
                    "text": (item.content_text or "")[: self.max_text_chars],
                }
                for item in observations
                if item.fetch_status == FetchStatus.FETCHED.value
            ],
            "allowed_relations": [item.value for item in EvidenceRelation],
            "untrusted_input": True,
        }
        result = self.caller(payload)
        if not isinstance(result, list):
            raise ValueError("Evidence evaluator must return a list")
        return tuple(EvidenceDraft.model_validate(item) for item in result)


def research_claim(
    session: Session,
    *,
    research_run: ResearchRun,
    claim_revision: ClaimRevision,
    query_plans: Iterable[ResearchQueryPlan],
    search_provider: SearchProvider,
    fetcher: DocumentFetcher,
    evaluator: EvidenceEvaluator,
    max_documents: int = 10,
    deadline_seconds: float = 600.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> ClaimAssessment:
    """Run or resume one claim's bounded search, fetch and evidence assessment."""
    plans = tuple(query_plans)
    if claim_revision.claim_type in {
        "transcription_error",
        "opinion",
        "prediction",
        "unverifiable",
    }:
        return _store_assessment(
            session,
            research_run=research_run,
            claim_revision=claim_revision,
            verdict=ClaimVerdict.UNVERIFIABLE,
            rationale=f"Claim type {claim_revision.claim_type} is not truth-verifiable",
            counter_search_completed=False,
            links=(),
        )

    started_at = monotonic()
    observations: dict[str, SourceObservation] = {}
    documents_read = 0
    counter_search_completed = False
    for plan in plans:
        _check_deadline(
            session,
            research_run,
            started_at=started_at,
            deadline_seconds=deadline_seconds,
            monotonic=monotonic,
        )
        query, response = _run_query(
            session,
            research_run=research_run,
            claim_revision=claim_revision,
            plan=plan,
            search_provider=search_provider,
        )
        if plan.purpose == "counter":
            counter_search_completed = True
        for hit in response.hits:
            if documents_read >= max_documents:
                break
            _check_deadline(
                session,
                research_run,
                started_at=started_at,
                deadline_seconds=deadline_seconds,
                monotonic=monotonic,
            )
            observation, fetched = _fetch_hit(
                session,
                research_run=research_run,
                query=query,
                hit=hit,
                fetcher=fetcher,
            )
            observations.setdefault(observation.canonical_url, observation)
            if fetched:
                documents_read += 1

    fetched_observations = tuple(
        item
        for item in observations.values()
        if item.fetch_status == FetchStatus.FETCHED.value
    )
    drafts = evaluator.evaluate(claim_revision, fetched_observations)
    links = tuple(
        _store_evidence_link(
            session,
            claim_revision=claim_revision,
            observations=fetched_observations,
            draft=draft,
        )
        for draft in drafts
    )
    verdict, rationale = _aggregate_verdict(
        claim_revision,
        links,
        counter_search_completed=counter_search_completed,
    )
    assessment = _store_assessment(
        session,
        research_run=research_run,
        claim_revision=claim_revision,
        verdict=verdict,
        rationale=rationale,
        counter_search_completed=counter_search_completed,
        links=links,
    )
    research_run.status = ResearchRunStatus.NEEDS_REVIEW.value
    session.commit()
    return assessment


def research_revision(
    session: Session,
    *,
    research_run: ResearchRun,
    claim_revisions: Sequence[ClaimRevision],
    plan_builder: Callable[[ClaimRevision], Iterable[ResearchQueryPlan]],
    search_provider: SearchProvider,
    fetcher: DocumentFetcher,
    evaluator: EvidenceEvaluator,
    max_claims: int = 5,
    max_documents: int = 10,
    deadline_seconds: float = 600.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> ResearchOutcome:
    """Research at most ``max_claims`` material claims, resuming finished ones for free.

    A budget or deadline stop keeps every assessment that was already durable; the
    remaining claims stay unassessed rather than appearing as checked.
    """
    ordered = sorted(claim_revisions, key=lambda item: item.id)
    material = [item for item in ordered if item.material]
    selected = material[:max_claims]
    selected_ids = {item.id for item in selected}
    skipped = tuple(item.revision_id for item in ordered if item.id not in selected_ids)

    research_run.status = ResearchRunStatus.RUNNING.value
    research_run.error_message = None
    session.commit()

    started_at = monotonic()
    assessments: list[ClaimAssessment] = []
    researched: list[str] = []
    for claim in selected:
        existing = (
            session.query(ClaimAssessment)
            .filter_by(research_run_id=research_run.id, claim_revision_id=claim.id)
            .first()
        )
        if existing is not None:
            assessments.append(existing)
            researched.append(claim.revision_id)
            continue
        remaining = deadline_seconds - (monotonic() - started_at)
        try:
            assessment = research_claim(
                session,
                research_run=research_run,
                claim_revision=claim,
                query_plans=plan_builder(claim),
                search_provider=search_provider,
                fetcher=fetcher,
                evaluator=evaluator,
                max_documents=max_documents,
                deadline_seconds=max(remaining, 0.0),
                monotonic=monotonic,
            )
        except (EditorialBudgetExceeded, ResearchDeadlineExceeded) as exc:
            research_run.status = ResearchRunStatus.BLOCKED.value
            research_run.error_message = str(exc)
            session.commit()
            return ResearchOutcome(
                assessments=tuple(assessments),
                researched_revision_ids=tuple(researched),
                skipped_revision_ids=skipped,
                blocked_revision_id=claim.revision_id,
                block_reason=str(exc),
            )
        assessments.append(assessment)
        researched.append(claim.revision_id)

    research_run.status = ResearchRunStatus.NEEDS_REVIEW.value
    session.commit()
    return ResearchOutcome(
        assessments=tuple(assessments),
        researched_revision_ids=tuple(researched),
        skipped_revision_ids=skipped,
    )


def _run_query(
    session: Session,
    *,
    research_run: ResearchRun,
    claim_revision: ClaimRevision,
    plan: ResearchQueryPlan,
    search_provider: SearchProvider,
) -> tuple[ResearchQuery, SearchResponse]:
    query = (
        session.query(ResearchQuery)
        .filter_by(
            research_run_id=research_run.id,
            claim_revision_id=claim_revision.id,
            query_key=plan.query_key,
        )
        .first()
    )
    if query is not None and query.status == ResearchQueryStatus.COMPLETED.value:
        return query, _deserialize_search_response(query.result_json or "{}")
    if query is None:
        query = ResearchQuery(
            query_id=str(uuid.uuid4()),
            research_run_id=research_run.id,
            claim_revision_id=claim_revision.id,
            query_key=plan.query_key,
            query_text=plan.query_text,
            language=plan.language,
            purpose=plan.purpose,
        )
        session.add(query)
        session.commit()
    elif (
        query.query_text != plan.query_text
        or query.language != plan.language
        or query.purpose != plan.purpose
    ):
        raise ValueError(f"Query key {plan.query_key!r} already identifies different work")

    operation = reserve_provider_operation(
        session,
        research_run=research_run,
        operation_key=f"search:{claim_revision.revision_id}:{plan.query_key}",
        operation_type="search",
        provider=search_provider.name,
        model_name="",
        input_hash=canonical_hash(plan.model_dump(mode="json")),
        estimated_cost_usd=plan.estimated_cost_usd,
    )
    if operation.status == ProviderOperationStatus.COMPLETED.value and query.result_json:
        query.status = ResearchQueryStatus.COMPLETED.value
        session.commit()
        return query, _deserialize_search_response(query.result_json)
    if operation.status == ProviderOperationStatus.COMPLETED.value:
        operation.status = ProviderOperationStatus.RECONCILE_REQUIRED.value
        operation.error_message = "Completed search operation has no durable result"
        query.status = ResearchQueryStatus.BLOCKED.value
        query.error_message = operation.error_message
        session.commit()
        raise RuntimeError(
            f"Search operation {operation.operation_id} completed without a durable result"
        )
    if operation.status in {
        ProviderOperationStatus.SUBMITTED.value,
        ProviderOperationStatus.RECONCILE_REQUIRED.value,
    }:
        raise RuntimeError(
            f"Search operation {operation.operation_id} requires reconciliation before retry"
        )

    operation.status = ProviderOperationStatus.SUBMITTED.value
    query.provider_operation_id = operation.id
    session.commit()
    try:
        response = search_provider.search(
            plan.query_text,
            language=plan.language,
            count=plan.max_results,
        )
    except Exception as exc:
        operation.status = ProviderOperationStatus.RECONCILE_REQUIRED.value
        operation.error_message = str(exc)
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after is not None:
            operation.usage_json = json.dumps({"retry_after_seconds": retry_after})
        query.status = ResearchQueryStatus.BLOCKED.value
        query.error_message = str(exc)
        session.commit()
        raise

    operation.status = ProviderOperationStatus.COMPLETED.value
    operation.actual_cost_usd = response.cost_usd
    operation.completed_at = datetime.now(UTC)
    query.status = ResearchQueryStatus.COMPLETED.value
    query.result_json = _serialize_search_response(response)
    query.completed_at = datetime.now(UTC)
    query.error_message = None
    session.commit()
    return query, response


def _fetch_hit(
    session: Session,
    *,
    research_run: ResearchRun,
    query: ResearchQuery,
    hit: SearchHit,
    fetcher: DocumentFetcher,
) -> tuple[SourceObservation, bool]:
    try:
        document = fetcher.fetch(hit.url)
    except UnsafeDocumentURL as exc:
        return _store_failed_observation(
            session,
            research_run=research_run,
            query=query,
            hit=hit,
            status=FetchStatus.BLOCKED,
            error=exc,
        ), False
    except UnsupportedDocument as exc:
        return _store_failed_observation(
            session,
            research_run=research_run,
            query=query,
            hit=hit,
            status=FetchStatus.UNSUPPORTED,
            error=exc,
        ), False
    except DocumentFetchError as exc:
        return _store_failed_observation(
            session,
            research_run=research_run,
            query=query,
            hit=hit,
            status=FetchStatus.FAILED,
            error=exc,
        ), False
    observation = _store_fetched_observation(
        session,
        research_run=research_run,
        query=query,
        hit=hit,
        document=document,
    )
    return observation, True


def _store_fetched_observation(
    session: Session,
    *,
    research_run: ResearchRun,
    query: ResearchQuery,
    hit: SearchHit,
    document: FetchedDocument,
) -> SourceObservation:
    key = canonical_hash(
        {
            "research_run_id": research_run.id,
            "canonical_url": document.final_url,
            "content_hash": document.content_hash,
        }
    )
    existing = session.query(SourceObservation).filter_by(observation_key=key).first()
    if existing is not None:
        return existing
    observation = SourceObservation(
        observation_id=str(uuid.uuid4()),
        observation_key=key,
        research_run_id=research_run.id,
        research_query_id=query.id,
        requested_url=document.requested_url,
        canonical_url=document.final_url,
        url_digest=_digest(document.final_url),
        publisher=hit.publisher,
        title=document.title or hit.title,
        published_at=_parse_datetime(hit.published_at),
        retrieved_at=document.retrieved_at,
        language=query.language,
        content_type=document.content_type,
        content_hash=document.content_hash,
        content_text=document.text,
        body_path=document.body_path,
        fetch_status=FetchStatus.FETCHED.value,
        http_status=200,
        provenance_family=hit.publisher or _host(document.final_url),
    )
    session.add(observation)
    session.commit()
    return observation


def _store_failed_observation(
    session: Session,
    *,
    research_run: ResearchRun,
    query: ResearchQuery,
    hit: SearchHit,
    status: FetchStatus,
    error: Exception,
) -> SourceObservation:
    key = canonical_hash(
        {
            "research_run_id": research_run.id,
            "query_id": query.id,
            "url": hit.url,
            "status": status.value,
        }
    )
    existing = session.query(SourceObservation).filter_by(observation_key=key).first()
    if existing is not None:
        return existing
    observation = SourceObservation(
        observation_id=str(uuid.uuid4()),
        observation_key=key,
        research_run_id=research_run.id,
        research_query_id=query.id,
        requested_url=hit.url,
        canonical_url=hit.url,
        url_digest=_digest(hit.url),
        publisher=hit.publisher,
        title=hit.title,
        published_at=_parse_datetime(hit.published_at),
        fetch_status=status.value,
        http_status=getattr(error, "status_code", None),
        provenance_family=hit.publisher or _host(hit.url),
        retry_after_seconds=getattr(error, "retry_after_seconds", None),
        error_message=str(error),
    )
    session.add(observation)
    session.commit()
    return observation


def _store_evidence_link(
    session: Session,
    *,
    claim_revision: ClaimRevision,
    observations: tuple[SourceObservation, ...],
    draft: EvidenceDraft,
) -> EvidenceLink:
    candidates = {
        item.canonical_url: item for item in observations if item.content_text is not None
    }
    observation = candidates.get(draft.canonical_url)
    if observation is None:
        raise ValueError("Evidence must reference a successfully fetched document")
    if draft.passage not in (observation.content_text or ""):
        raise ValueError("Evidence passage is not present in the fetched document")
    _validate_claim_anchors(claim_revision, draft)
    passage_hash = _digest(draft.passage)
    existing = (
        session.query(EvidenceLink)
        .filter_by(
            claim_revision_id=claim_revision.id,
            source_observation_id=observation.id,
            passage_hash=passage_hash,
            relation=draft.relation,
        )
        .first()
    )
    if existing is not None:
        return existing
    link = EvidenceLink(
        evidence_id=str(uuid.uuid4()),
        claim_revision_id=claim_revision.id,
        source_observation_id=observation.id,
        relation=draft.relation,
        passage=draft.passage,
        translated_passage=draft.translated_passage,
        passage_hash=passage_hash,
        rationale=draft.rationale,
        provenance_family=draft.provenance_family or observation.provenance_family,
    )
    session.add(link)
    session.commit()
    return link


def _validate_claim_anchors(claim: ClaimRevision, draft: EvidenceDraft) -> None:
    if draft.relation != EvidenceRelation.SUPPORTS.value:
        return
    normalized = draft.passage.casefold()
    if claim.subject and claim.subject.casefold() not in normalized:
        raise ValueError("Supporting passage does not name the claim subject")
    if claim.numeric_value and claim.numeric_value.casefold() not in normalized:
        raise ValueError("Supporting passage does not contain the claim number")
    if claim.unit and claim.unit.casefold() not in normalized:
        raise ValueError("Supporting passage does not contain the claim unit")
    if claim.attribution and claim.attribution.casefold() not in normalized:
        raise ValueError("Supporting passage does not contain the attribution")
    if claim.event_date and claim.event_date.casefold() not in normalized:
        raise ValueError("Supporting passage does not contain the claim date")
    if claim.claim_type == "quote" and claim.statement.casefold() not in normalized:
        raise ValueError("Supporting passage does not contain the claimed quote")
    if _has_negation(claim.statement) != _has_negation(draft.passage):
        raise ValueError("Supporting passage changes the claim's negation")


def _aggregate_verdict(
    claim: ClaimRevision,
    links: tuple[EvidenceLink, ...],
    *,
    counter_search_completed: bool,
) -> tuple[ClaimVerdict, str]:
    if claim.claim_type in {
        "transcription_error",
        "opinion",
        "prediction",
        "unverifiable",
    }:
        return ClaimVerdict.UNVERIFIABLE, f"Claim type {claim.claim_type} is not truth-verifiable"
    relations = {link.relation for link in links}
    if (
        EvidenceRelation.SUPPORTS.value in relations
        and EvidenceRelation.CONTRADICTS.value in relations
    ):
        return ClaimVerdict.CONFLICTING, "Fetched evidence both supports and contradicts the claim"
    if EvidenceRelation.CONTRADICTS.value in relations:
        return ClaimVerdict.CONTRADICTED, "Fetched evidence contradicts the claim"
    if EvidenceRelation.SUPPORTS.value in relations:
        if not counter_search_completed or relations & {
            EvidenceRelation.CONTEXT.value,
            EvidenceRelation.INCONCLUSIVE.value,
        }:
            return (
                ClaimVerdict.PARTIAL,
                "Support exists, but counter-search or context is incomplete",
            )
        return ClaimVerdict.SUPPORTED, "Fetched passages support the claim after counter-search"
    return ClaimVerdict.INSUFFICIENT, "No fetched passage sufficiently supports the claim"


def _store_assessment(
    session: Session,
    *,
    research_run: ResearchRun,
    claim_revision: ClaimRevision,
    verdict: ClaimVerdict,
    rationale: str,
    counter_search_completed: bool,
    links: tuple[EvidenceLink, ...],
) -> ClaimAssessment:
    evidence_digest = canonical_hash(
        [
            {
                "evidence_id": item.evidence_id,
                "relation": item.relation,
                "passage_hash": item.passage_hash,
            }
            for item in sorted(links, key=lambda link: link.evidence_id)
        ]
    )
    assessment = (
        session.query(ClaimAssessment)
        .filter_by(
            research_run_id=research_run.id,
            claim_revision_id=claim_revision.id,
        )
        .first()
    )
    if assessment is None:
        assessment = ClaimAssessment(
            assessment_id=str(uuid.uuid4()),
            research_run_id=research_run.id,
            claim_revision_id=claim_revision.id,
            verdict=verdict.value,
            rationale=rationale,
            counter_search_completed=counter_search_completed,
            evidence_digest=evidence_digest,
        )
        session.add(assessment)
    else:
        assessment.verdict = verdict.value
        assessment.rationale = rationale
        assessment.counter_search_completed = counter_search_completed
        assessment.evidence_digest = evidence_digest
    session.commit()
    return assessment


def _serialize_search_response(response: SearchResponse) -> str:
    return json.dumps(
        {
            "provider": response.provider,
            "query": response.query,
            "request_id": response.request_id,
            "cost_usd": response.cost_usd,
            "hits": [
                SearchHitData(
                    title=hit.title,
                    url=hit.url,
                    snippet=hit.snippet,
                    publisher=hit.publisher,
                    published_at=hit.published_at,
                ).model_dump(mode="json")
                for hit in response.hits
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _deserialize_search_response(value: str) -> SearchResponse:
    payload = json.loads(value)
    return SearchResponse(
        provider=payload["provider"],
        query=payload["query"],
        request_id=payload.get("request_id"),
        cost_usd=float(payload.get("cost_usd") or 0.0),
        hits=tuple(
            SearchHit(**SearchHitData.model_validate(item).model_dump())
            for item in payload.get("hits", [])
        ),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _host(url: str) -> str | None:
    return urlsplit(url).hostname


def _has_negation(value: str) -> bool:
    words = set(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))
    return bool(words & {"nicht", "kein", "keine", "keinen", "no", "not", "never", "değil", "yok"})


def _check_deadline(
    session: Session,
    research_run: ResearchRun,
    *,
    started_at: float,
    deadline_seconds: float,
    monotonic: Callable[[], float],
) -> None:
    if monotonic() - started_at > deadline_seconds:
        message = f"Research deadline exceeded after {deadline_seconds:.1f} seconds"
        research_run.status = ResearchRunStatus.BLOCKED.value
        research_run.error_message = message
        session.commit()
        raise ResearchDeadlineExceeded(message)
