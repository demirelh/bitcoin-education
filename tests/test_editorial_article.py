from __future__ import annotations

import io
import uuid

import pytest

from btcedu.core.editorial.article import (
    ArticleApprovalBlocked,
    ArticleContentRejected,
    StaleArticleApproval,
    UnknownClaimReferenced,
    UnsupportedClaimReferenced,
    approve_article_revision,
    article_gate_reasons,
    article_preview,
    generate_article_revision,
    reject_article_revision,
)
from btcedu.core.editorial.ingest import import_story
from btcedu.core.editorial.jobs import reserve_research_run
from btcedu.core.editorial.media import (
    MediaBlobStore,
    MediaRequirement,
    revoke_media_decision,
    select_media_for_revision,
)
from btcedu.core.editorial.research import research_revision
from btcedu.models.article import (
    ArticleParagraph,
    ArticleParagraphClaim,
    ArticleRevision,
    ArticleStatus,
    EditorialDecision,
)
from btcedu.models.editorial import (
    ClaimAssessment,
    ClaimRevision,
    EditorialRevision,
    ProviderOperation,
    RevisionClaim,
)
from btcedu.models.editorial_schema import ClaimDraft, ResearchQueryPlan
from btcedu.models.media_rights import MediaRole
from btcedu.models.story_schema import Story, StoryCategory, StoryType
from btcedu.services.commons_service import FixtureCommonsProvider, MediaCandidate
from btcedu.services.document_fetcher import (
    DocumentFetcher,
    DocumentSnapshotStore,
    RawDocumentResponse,
)
from btcedu.services.search_service import FixtureSearchProvider, SearchHit, SearchResponse

PUBLIC_IP = "93.184.216.34"
SUPPORT_URL = "https://primary.example/report"
COUNTER_URL = "https://independent.example/report"
IMAGE_URL = "https://upload.example/berlin.png"
PASSAGE = "Berlin meldet 100 neue Wohnungen."


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def _story() -> Story:
    return Story(
        story_id="story-1",
        order=1,
        headline_de="Wohnungsbau in Berlin",
        category=StoryCategory.GESELLSCHAFT,
        story_type=StoryType.MELDUNG,
        text_de=PASSAGE,
        source_text=PASSAGE,
        source_segment_ids=["segment-1"],
        source_start_seconds=4.0,
        source_end_seconds=10.0,
        word_count=5,
        estimated_duration_seconds=6,
    )


def _claim_draft(**overrides) -> ClaimDraft:
    defaults = dict(
        claim_key="housing",
        statement=PASSAGE,
        claim_type="fact",
        subject="Berlin",
        numeric_value="100",
        unit="Wohnungen",
    )
    defaults.update(overrides)
    return ClaimDraft(**defaults)


def _search_provider() -> FixtureSearchProvider:
    return FixtureSearchProvider(
        {
            ("housing evidence", "de"): SearchResponse(
                provider="fixture",
                query="housing evidence",
                hits=(
                    SearchHit(
                        title="Official report",
                        url=SUPPORT_URL,
                        snippet="discovery only",
                        publisher="Primary Office",
                    ),
                ),
            ),
            ("housing counter", "en"): SearchResponse(
                provider="fixture",
                query="housing counter",
                hits=(
                    SearchHit(
                        title="Independent report",
                        url=COUNTER_URL,
                        snippet="discovery only",
                        publisher="Independent News",
                    ),
                ),
            ),
        }
    )


def _plans(claim):
    return [
        ResearchQueryPlan(
            query_key="support-de",
            query_text="housing evidence",
            language="de",
            purpose="support",
        ),
        ResearchQueryPlan(
            query_key="counter-en",
            query_text="housing counter",
            language="en",
            purpose="counter",
        ),
    ]


class _Transport:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url, address, connect_timeout, read_timeout, max_bytes):
        self.calls.append(url)
        return self.responses[url]


def _fetcher(tmp_path):
    page = f"<html><p>{PASSAGE}</p></html>".encode()
    transport = _Transport(
        {
            SUPPORT_URL: RawDocumentResponse(200, {"Content-Type": "text/html"}, page),
            COUNTER_URL: RawDocumentResponse(200, {"Content-Type": "text/html"}, page),
            IMAGE_URL: RawDocumentResponse(200, {"Content-Type": "image/png"}, _png_bytes()),
        }
    )
    return (
        DocumentFetcher(
            store=DocumentSnapshotStore(tmp_path / "documents"),
            resolver=lambda host, port: [PUBLIC_IP],
            transport=transport,
        ),
        transport,
    )


def _evaluator(relation="supports", passage=PASSAGE):
    from btcedu.core.editorial.research import DataOnlyEvidenceEvaluator

    return DataOnlyEvidenceEvaluator(
        lambda payload: [
            {
                "canonical_url": SUPPORT_URL,
                "relation": relation,
                "passage": passage,
                "rationale": "The official passage states the same count.",
            }
        ]
    )


def _candidate() -> MediaCandidate:
    from datetime import UTC, datetime

    return MediaCandidate(
        provider="commons_fixture",
        offer_key="File:Berlin.png",
        page_url="https://commons.example/wiki/File:Berlin.png",
        file_url=IMAGE_URL,
        title="Neubau in Berlin",
        description="Neue Wohnungen in Berlin.",
        author="A. Fotograf",
        license_id="CC BY-SA 4.0",
        captured_at=datetime(2026, 9, 8, tzinfo=UTC),
        captured_at_text="2026-09-08",
        mime_type="image/png",
        width=64,
        height=48,
        byte_size=1024,
    )


def _draft(**overrides):
    payload = {
        "title": "Berlin'de 100 yeni konut",
        "lede": "Berlin yeni konutlar bildirdi.",
        "paragraphs": [
            {
                "kind": "body",
                "text": "Berlin 100 yeni konut bildirdi.",
                "claim_keys": ["housing"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _pipeline(db_session, tmp_path, *, claims=None, relation="supports", with_media=True):
    """Run import, research and media selection, then return the revision."""
    imported = import_story(
        db_session,
        episode_id="episode-1",
        story=_story(),
        claims=claims if claims is not None else [_claim_draft()],
    )
    revision = EditorialRevision(
        revision_id=str(uuid.uuid4()),
        topic_id=imported.topic.id,
        content_hash="e" * 64,
        policy_version="article-v1",
    )
    db_session.add(revision)
    db_session.flush()
    for claim in imported.claim_revisions:
        db_session.add(
            RevisionClaim(editorial_revision_id=revision.id, claim_revision_id=claim.id)
        )
    db_session.commit()

    run = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="f" * 64,
        policy_version="evidence-v1",
        model_name="fixture-evaluator",
        max_queries=20,
        max_cost_usd=0.75,
    )
    fetcher, transport = _fetcher(tmp_path)
    if imported.claim_revisions:
        research_revision(
            db_session,
            research_run=run,
            claim_revisions=imported.claim_revisions,
            plan_builder=_plans,
            search_provider=_search_provider(),
            fetcher=fetcher,
            evaluator=_evaluator(relation=relation),
        )
    if with_media:
        select_media_for_revision(
            db_session,
            editorial_revision=revision,
            requirement=MediaRequirement(
                subject="Berlin",
                role=MediaRole.EVENT,
                event_date=_candidate().captured_at,
                caption="Berlin'de yeni konutlar",
            ),
            provider=FixtureCommonsProvider({"Berlin": (_candidate(),)}),
            fetcher=fetcher,
            blob_store=MediaBlobStore(tmp_path / "media"),
        )
    return revision, run, transport


def test_complete_topic_reaches_an_approved_turkish_article(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    calls: list[dict] = []

    def drafter(payload):
        calls.append(payload)
        return _draft()

    article = generate_article_revision(
        db_session, editorial_revision=revision, research_run=run, drafter=drafter
    )
    preview = article_preview(db_session, article, research_run=run)

    assert article.language == "tr"
    assert article.status == ArticleStatus.DRAFT.value
    assert preview["open_issues"] == []
    assert preview["paragraphs"][0]["claims"][0]["verdict"] == "supported"
    assert preview["media"][0]["attribution"].startswith("A. Fotograf")
    assert preview["media"][0]["license"] == "CC BY-SA 4.0"
    assert calls[0]["untrusted_input"] is True

    decision = approve_article_revision(
        db_session,
        article,
        research_run=run,
        operator_ref="web:editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )

    assert decision.operator_ref == "web:editor"
    assert article.status == ArticleStatus.APPROVED.value
    assert db_session.query(EditorialDecision).count() == 1


def test_article_cannot_reference_a_claim_that_is_not_in_the_revision(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)

    with pytest.raises(UnknownClaimReferenced):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[
                    {
                        "text": "Berlin 100 yeni konut bildirdi.",
                        "claim_keys": ["invented"],
                    }
                ]
            ),
        )
    assert db_session.query(ArticleRevision).count() == 0


def test_article_cannot_assert_a_contradicted_claim(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path, relation="contradicts")

    with pytest.raises(UnsupportedClaimReferenced):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(),
        )


def test_numbers_must_survive_into_the_translation(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)

    with pytest.raises(ArticleContentRejected, match="drops the number"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[
                    {"text": "Berlin yeni konutlar bildirdi.", "claim_keys": ["housing"]}
                ]
            ),
        )


def test_invented_numbers_are_rejected(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)

    with pytest.raises(ArticleContentRejected, match="introduces numbers"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[
                    {
                        "text": "Berlin 100 yeni konut bildirdi, toplam 4200 daire.",
                        "claim_keys": ["housing"],
                    }
                ]
            ),
        )


def test_uncertainty_must_not_be_upgraded_to_certainty(db_session, tmp_path):
    revision, run, _ = _pipeline(
        db_session,
        tmp_path,
        claims=[_claim_draft(modality="mutmaßlich")],
    )

    with pytest.raises(ArticleContentRejected, match="as certain"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(),
        )


def test_uncertainty_marker_is_accepted(db_session, tmp_path):
    revision, run, _ = _pipeline(
        db_session,
        tmp_path,
        claims=[_claim_draft(modality="mutmaßlich")],
    )

    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(
            paragraphs=[
                {
                    "text": "Yetkililere göre Berlin 100 yeni konut bildirdi.",
                    "claim_keys": ["housing"],
                }
            ]
        ),
    )

    assert article.status == ArticleStatus.DRAFT.value


def test_a_quotation_needs_a_quoted_claim(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)

    with pytest.raises(ArticleContentRejected, match="quotation"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[
                    {
                        "text": 'Berlin 100 yeni konut bildirdi ve "her şey yolunda" dedi.',
                        "claim_keys": ["housing"],
                    }
                ]
            ),
        )


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script> Berlin 100 yeni konut bildirdi.",
        "Berlin 100 yeni konut bildirdi. <img src=x onerror=y>",
        "Berlin 100 yeni konut bildirdi. &#106;avascript:",
    ],
)
def test_markup_in_generated_text_is_refused(db_session, tmp_path, text):
    revision, run, _ = _pipeline(db_session, tmp_path)

    with pytest.raises(ArticleContentRejected, match="plain text"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[{"text": text, "claim_keys": ["housing"]}]
            ),
        )


def test_redraft_of_an_unchanged_state_is_not_billed_again(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    calls: list[dict] = []

    def drafter(payload):
        calls.append(payload)
        return _draft()

    first = generate_article_revision(
        db_session, editorial_revision=revision, research_run=run, drafter=drafter
    )
    second = generate_article_revision(
        db_session, editorial_revision=revision, research_run=run, drafter=drafter
    )

    assert second.id == first.id
    assert len(calls) == 1
    assert db_session.query(ArticleRevision).count() == 1
    operations = db_session.query(ProviderOperation).filter_by(operation_type="article").all()
    assert len(operations) == 1
    assert operations[0].status == "completed"


def test_open_core_evidence_blocks_approval(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)
    assessment = db_session.query(ClaimAssessment).one()
    assessment.verdict = "insufficient"
    db_session.commit()

    reasons = article_gate_reasons(
        db_session, editorial_revision=revision, research_run=run
    )
    assert any("insufficient" in reason for reason in reasons)

    with pytest.raises(StaleArticleApproval):
        approve_article_revision(
            db_session,
            article,
            research_run=run,
            operator_ref="web:editor",
            reviewed_content_hash=preview["content_hash"],
            reviewed_evidence_hash=preview["evidence_hash"],
            reviewed_media_hash=preview["media_hash"],
        )
    assert db_session.query(EditorialDecision).count() == 0


def test_unassessed_core_claim_blocks_approval(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    db_session.query(ClaimAssessment).delete()
    db_session.commit()
    fresh = article_preview(db_session, article, research_run=run)

    with pytest.raises(ArticleApprovalBlocked, match="no evidence assessment"):
        approve_article_revision(
            db_session,
            article,
            research_run=run,
            operator_ref="web:editor",
            reviewed_content_hash=fresh["content_hash"],
            reviewed_evidence_hash=fresh["evidence_hash"],
            reviewed_media_hash=fresh["media_hash"],
        )
    assert article.status == ArticleStatus.DRAFT.value
    assert "no evidence assessment" in article.block_reason


def test_changed_media_invalidates_an_earlier_preview(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)
    from btcedu.models.media_rights import MediaUseDecision

    revoke_media_decision(
        db_session,
        db_session.query(MediaUseDecision).one(),
        reason="Rechte zurückgezogen",
    )

    with pytest.raises(StaleArticleApproval):
        approve_article_revision(
            db_session,
            article,
            research_run=run,
            operator_ref="web:editor",
            reviewed_content_hash=preview["content_hash"],
            reviewed_evidence_hash=preview["evidence_hash"],
            reviewed_media_hash=preview["media_hash"],
        )


def test_dropping_the_picture_produces_a_new_article_version(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    first = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    from btcedu.models.media_rights import MediaUseDecision

    revoke_media_decision(
        db_session,
        db_session.query(MediaUseDecision).one(),
        reason="Bild bewusst weggelassen",
    )
    second = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    assert second.id != first.id
    assert second.media_hash != first.media_hash
    assert article_preview(db_session, second, research_run=run)["media"] == []


def test_approval_requires_a_named_operator(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)

    with pytest.raises(ValueError, match="named operator"):
        approve_article_revision(
            db_session,
            article,
            research_run=run,
            operator_ref="  ",
            reviewed_content_hash=preview["content_hash"],
            reviewed_evidence_hash=preview["evidence_hash"],
            reviewed_media_hash=preview["media_hash"],
        )


def test_profile_auto_approve_cannot_release_an_article(db_session, tmp_path, monkeypatch):
    """The video pipeline's auto-approval must not reach the newsroom.

    ``auto_approve_reviews`` releases pipeline stages without a human. An
    article carries claims about the world, so it has no automatic path at all.
    """
    import inspect

    from btcedu.core.editorial import article as article_module

    source = inspect.getsource(article_module)

    assert "auto_approve" not in source
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    assert article.status == ArticleStatus.DRAFT.value
    assert db_session.query(EditorialDecision).count() == 0


def test_rejection_is_recorded_against_the_reviewed_state(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    decision = reject_article_revision(
        db_session,
        article,
        research_run=run,
        operator_ref="web:editor",
        rationale="Beleglage zu dünn",
    )

    assert decision.decision == "reject"
    assert article.status == ArticleStatus.REJECTED.value
    assert decision.reviewed_content_hash == article.content_hash


def test_preview_names_material_claims_the_article_left_out(db_session, tmp_path):
    revision, run, _ = _pipeline(
        db_session,
        tmp_path,
        claims=[_claim_draft(), _claim_draft(claim_key="second", numeric_value=None)],
    )
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    preview = article_preview(db_session, article, research_run=run)

    assert [item["claim_key"] for item in preview["unreferenced_claims"]] == ["second"]


def test_generation_survives_a_restart_in_the_middle_of_research(db_session, tmp_path):
    """Research that stopped halfway must not silently yield a finished article."""
    revision, run, _ = _pipeline(db_session, tmp_path, with_media=False)
    db_session.query(ClaimAssessment).delete()
    db_session.commit()

    with pytest.raises(ArticleContentRejected, match="must reference"):
        generate_article_revision(
            db_session,
            editorial_revision=revision,
            research_run=run,
            drafter=lambda payload: _draft(
                paragraphs=[
                    {"text": "Berlin 100 yeni konut bildirdi.", "claim_keys": []}
                ]
            ),
        )
    reasons = article_gate_reasons(
        db_session, editorial_revision=revision, research_run=run
    )

    assert db_session.query(ArticleRevision).count() == 0
    assert reasons
    assert db_session.query(EditorialDecision).count() == 0


def test_paragraph_claim_links_are_persisted(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    paragraph = db_session.query(ArticleParagraph).one()
    link = db_session.query(ArticleParagraphClaim).one()
    claim = db_session.query(ClaimRevision).one()

    assert paragraph.article_revision_id == article.id
    assert link.article_paragraph_id == paragraph.id
    assert link.claim_revision_id == claim.id
