"""One resumable selected-transcript-story to private article workflow.

Provider objects are injected. The same workflow runs offline fixtures and
explicitly configured data-only providers; it never approves or publishes.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from btcedu.core.editorial.article import ArticleContentRejected, generate_article_revision
from btcedu.core.editorial.ingest import canonical_hash, import_story
from btcedu.core.editorial.jobs import (
    ProviderCallNotAttempted,
    reserve_provider_operation,
    reserve_research_run,
)
from btcedu.core.editorial.media import MediaBlobStore, select_media_for_revision
from btcedu.core.editorial.research import (
    DataOnlyClaimExtractor,
    DataOnlyEvidenceEvaluator,
    research_revision,
)
from btcedu.models.editorial import EditorialRevision, ProviderOperationStatus, RevisionClaim, Topic
from btcedu.models.editorial_schema import ResearchQueryPlan
from btcedu.models.topic_graph import AliasKind, ProposalStatus, UpdateProposal


@dataclass(frozen=True)
class ModelReply:
    result: object
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class BudgetedCaller:
    def __init__(self, session, run, caller, *, provider, model, max_call_cost_usd):
        self.session, self.run, self.caller = session, run, caller
        self.provider, self.model = provider, model
        self.max_call_cost_usd = max_call_cost_usd

    def __call__(self, payload):
        digest = canonical_hash(payload)
        operation = reserve_provider_operation(
            self.session,
            research_run=self.run,
            operation_key=f"{payload['task']}:{digest}",
            operation_type="llm",
            provider=self.provider,
            model_name=self.model,
            input_hash=digest,
            estimated_cost_usd=self.max_call_cost_usd,
        )
        if operation.status == ProviderOperationStatus.COMPLETED.value:
            if (
                operation.actual_cost_usd is None
                or operation.actual_cost_usd > self.max_call_cost_usd
            ):
                raise RuntimeError("Stored model cost exceeds the approved reservation")
            return json.loads(operation.usage_json)["result"]
        if operation.status == ProviderOperationStatus.FAILED.value:
            # A failed operation is a decided outcome, not one still in flight.
            # Refusing to retry it strands the story permanently; only an
            # uncertain provider outcome may keep blocking.
            operation.status = ProviderOperationStatus.RESERVED.value
            operation.error_message = None
            operation.completed_at = None
            operation.actual_cost_usd = None
            self.session.commit()
        if operation.status != ProviderOperationStatus.RESERVED.value:
            raise RuntimeError("Uncertain model operation requires reconciliation")
        operation.status = ProviderOperationStatus.SUBMITTED.value
        operation.submitted_at = datetime.now(UTC)
        self.session.commit()
        try:
            reply = self.caller(payload)
        except ProviderCallNotAttempted:
            operation.status = ProviderOperationStatus.RESERVED.value
            operation.submitted_at = None
            self.session.commit()
            raise
        except Exception:
            operation.status = ProviderOperationStatus.RECONCILE_REQUIRED.value
            operation.error_message = "Model call failed; outcome requires reconciliation"
            self.session.commit()
            raise
        if not math.isfinite(reply.cost_usd) or reply.cost_usd < 0:
            operation.status = ProviderOperationStatus.RECONCILE_REQUIRED.value
            operation.error_message = "Invalid provider cost"
            self.session.commit()
            raise ValueError("Provider returned an invalid cost")
        operation.actual_cost_usd = reply.cost_usd
        operation.usage_json = json.dumps(
            {
                "result": reply.result,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "model": reply.model,
            },
            ensure_ascii=False,
        )
        operation.status = ProviderOperationStatus.COMPLETED.value
        operation.completed_at = datetime.now(UTC)
        self.session.commit()
        if reply.cost_usd > self.max_call_cost_usd:
            raise RuntimeError("Model exceeded its reserved cost; stop before another call")
        return reply.result


def query_plans(claim):
    """Automatic support and counter-search, never snippets as evidence."""
    query = " ".join(
        filter(
            None,
            [
                claim.subject,
                claim.location,
                claim.event_date,
                claim.statement,
            ],
        )
    )[:850]
    return (
        ResearchQueryPlan(
            query_key="support",
            query_text=query,
            language="de",
            purpose="support",
            estimated_cost_usd=0.005,
        ),
        ResearchQueryPlan(
            query_key="counter",
            query_text=f"{query} Widerspruch Korrektur Faktencheck",
            language="de",
            purpose="counter",
            estimated_cost_usd=0.005,
        ),
    )


def draft_story(
    session: Session,
    *,
    episode_id,
    story,
    settings,
    model_caller,
    search_provider,
    fetcher,
    media_provider,
    media_requirement,
    provider_name="fixture",
    model_name="fixture",
    max_call_cost_usd=0.0,
    plan_builder=query_plans,
    source_published_at=None,
    source_language="de",
    source_uri=None,
    repair_attempts=1,
    dev_auto_release=False,
):
    """``dev_auto_release`` keeps editorial rejections as recorded warnings.

    Off by default. It never silences a check and never marks anything as
    approved: the verdict is stored on the revision and shown in the review
    view. Technical and safety failures still raise.
    """
    if not settings.newsroom_enabled:
        raise ValueError("Newsroom is disabled")
    dev_findings: list[str] = []
    from btcedu.core.editorial.topics import add_alias, propose_updates

    imported = import_story(
        session,
        episode_id=episode_id,
        story=story,
        source_language=source_language,
        source_uri=source_uri,
        published_at=source_published_at,
    )
    propose_updates(session, imported.source_revision)
    proposals = (
        session.query(UpdateProposal)
        .filter_by(source_revision_id=imported.source_revision.id)
        .all()
    )
    open_proposals = [row for row in proposals if row.status == ProposalStatus.OPEN.value]
    if open_proposals and not dev_auto_release:
        raise ValueError("Possible topic update: review the stored proposals before drafting")
    if open_proposals:
        # Deciding that a broadcast continues an existing topic has editorial
        # consequences, so the switch must not make that call. Drafting under
        # the broadcast's own new topic instead decides nothing: the proposals
        # stay OPEN for a reviewer, and the finding says so.
        dev_findings.append(
            f"Topic continuity unresolved: {len(open_proposals)} open update proposal(s); "
            "drafted as a separate topic without merging."
        )
    accepted = [row for row in proposals if row.status == ProposalStatus.ACCEPTED.value]
    if len(accepted) > 1:
        raise ValueError("Several accepted topic destinations require an explicit editorial split")
    target_topic = session.get(Topic, accepted[0].topic_id) if accepted else imported.topic
    run = reserve_research_run(
        session,
        topic_id=target_topic.id,
        input_hash=canonical_hash(
            {
                "source_revision": imported.source_revision.revision_id,
                "content": imported.source_revision.content_hash,
            }
        ),
        policy_version="newsroom-workflow-v2",
        model_name=model_name,
        max_queries=settings.newsroom_research_max_queries,
        max_cost_usd=settings.newsroom_research_max_cost_usd,
    )
    caller = BudgetedCaller(
        session,
        run,
        model_caller,
        provider=provider_name,
        model=model_name,
        max_call_cost_usd=max_call_cost_usd,
    )
    claims = DataOnlyClaimExtractor(caller).extract(
        source_text=imported.source_span.source_text,
        language=source_language,
        span_id=imported.source_span.span_id,
    )
    if not claims or len(claims) > settings.newsroom_research_max_claims:
        raise ValueError("Selected story must contain between one and five core claims")
    imported = import_story(
        session,
        episode_id=episode_id,
        story=story,
        claims=claims,
        target_topic=target_topic,
        source_language=source_language,
        source_uri=source_uri,
        published_at=source_published_at,
    )
    for claim in claims:
        if claim.subject:
            add_alias(session, target_topic, kind=AliasKind.ENTITY, value=claim.subject)
        if claim.unit:
            add_alias(session, target_topic, kind=AliasKind.KEYWORD, value=claim.unit)
        if claim.event_date:
            add_alias(session, target_topic, kind=AliasKind.EVENT_DATE, value=claim.event_date)
    state = canonical_hash([row.revision_id for row in imported.claim_revisions])
    revision = (
        session.query(EditorialRevision)
        .filter_by(
            topic_id=imported.topic.id, content_hash=state, policy_version="newsroom-workflow-v2"
        )
        .one_or_none()
    )
    if revision is None:
        revision = EditorialRevision(
            revision_id=str(uuid.uuid4()),
            topic_id=imported.topic.id,
            content_hash=state,
            policy_version="newsroom-workflow-v2",
        )
        session.add(revision)
        session.flush()
        session.add_all(
            [
                RevisionClaim(editorial_revision_id=revision.id, claim_revision_id=row.id)
                for row in imported.claim_revisions
            ]
        )
        session.commit()
    outcome = research_revision(
        session,
        research_run=run,
        claim_revisions=imported.claim_revisions,
        plan_builder=plan_builder,
        search_provider=search_provider,
        fetcher=fetcher,
        evaluator=DataOnlyEvidenceEvaluator(caller),
        max_claims=settings.newsroom_research_max_claims,
        max_documents=settings.newsroom_research_max_documents,
        deadline_seconds=settings.newsroom_research_timeout_seconds,
    )
    if outcome.blocked:
        if not dev_auto_release:
            raise ValueError(outcome.block_reason)
        # Thin evidence is exactly what the development preview needs to make
        # visible. Suppressing the story hides the finding; carrying it forward
        # as a warning states it on the page instead.
        dev_findings.append(f"Evidence gate: {outcome.block_reason}")
    for revision_id, reason in outcome.unsupported:
        # Excluded from the article, but named so a reviewer can see which
        # statement was dropped and why rather than only noticing a gap.
        dev_findings.append(f"Claim excluded ({revision_id}): {reason}")
    if media_requirement is not None:
        selection = select_media_for_revision(
            session,
            editorial_revision=revision,
            requirement=media_requirement,
            provider=media_provider,
            fetcher=fetcher,
            blob_store=MediaBlobStore.from_settings(settings),
            max_candidates=settings.newsroom_media_max_candidates,
        )
        if not selection.has_picture:
            raise ValueError(
                f"Requested illustration unresolved: {selection.reason}; "
                "explicitly choose no picture or provide cleared media"
            )

    def checked_draft(payload):
        draft = caller(payload)
        verdict = caller(
            {
                "task": "check_article_consistency",
                "claims": payload["claims"],
                "draft": draft,
                "untrusted_input": True,
            }
        )
        findings: list[str] = []
        if not isinstance(verdict, dict) or verdict.get("consistent") is not True:
            findings.append("Semantic article check rejected the draft")
        elif verdict.get("issues") != []:
            issues = verdict.get("issues")
            detail = "; ".join(str(issue) for issue in issues)[:300] if issues else ""
            findings.append(
                "Semantic article check has unresolved findings"
                + (f": {detail}" if detail else "")
            )
        if findings and not dev_auto_release:
            raise ArticleContentRejected(findings[0])
        # In development the draft is kept instead of discarded, and the
        # verdict travels with it. Throwing it away was losing the only thing
        # that could be reviewed: the reviewer never saw what was rejected.
        dev_findings.extend(findings)
        return draft

    article = generate_article_revision(
        session,
        editorial_revision=revision,
        research_run=run,
        drafter=checked_draft,
        model_name=model_name,
        policy_version="newsroom-workflow-v2",
        repair_attempts=repair_attempts,
    )
    if dev_findings:
        # Recorded on the revision, so the review view and the development
        # banner state what failed. This is not an approval and does not
        # pretend the check passed.
        article.block_reason = "; ".join(dict.fromkeys(dev_findings))[:1000]
        session.commit()
    return article
