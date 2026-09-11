from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import btcedu.models  # noqa: F401
from btcedu.config import Settings
from btcedu.core.editorial.article import approve_article_revision, article_preview
from btcedu.core.editorial.media import MediaRequirement
from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.core.editorial.workflow import ModelReply, draft_story
from btcedu.db import Base
from btcedu.models.editorial import (
    ClaimAssessment,
    EvidenceLink,
    ResearchRun,
    SourceObservation,
)
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.media_rights import (
    LicenseEvidence,
    MediaRole,
    MediaSourceOffer,
    MediaUseDecision,
)
from btcedu.models.story_schema import Story, StoryCategory, StoryType
from btcedu.services.commons_service import WikimediaCommonsProvider
from btcedu.services.document_fetcher import DocumentFetcher
from btcedu.services.search_service import (
    FixtureSearchProvider,
    SearchHit,
    SearchResponse,
)

if __package__:
    from .paths import data_root, repo_root
else:  # executed directly from the command line
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root, repo_root

ROOT = data_root()
DB_PATH = ROOT / "preview.sqlite"
PRIVATE_ROOT = ROOT / "private"
SITE_ROOT = ROOT / "site"
SCREENSHOT_ROOT = ROOT / "screenshots"
SCREENSHOT_HTML_ROOT = ROOT / "screenshot-pages"
SOURCE_FIXTURE = repo_root() / "tests" / "fixtures" / "sample_transcript_de.txt"

DEV_URL = "https://developer.bitcoin.org/devguide/block_chain.html"
IBM_URL = "https://www.ibm.com/think/topics/blockchain"
DEV_PASSAGE = (
    "Each block also stores the hash of the previous block’s header, "
    "chaining the blocks together."
)
IBM_PASSAGE = (
    "Each block is linked to the previous block and the one after it, "
    "creating a secure chain of data."
)


class SelectedCommonsProvider:
    name = "wikimedia_commons"

    def __init__(self, fetcher: DocumentFetcher) -> None:
        self.provider = WikimediaCommonsProvider(fetcher)
        self.candidates_seen = []

    def search(self, query: str, *, limit: int):
        self.candidates_seen = list(self.provider.search("Bitcoin symbol", limit=10))
        selected = next(
            (row for row in self.candidates_seen if row.title == "File:Bitcoin Sign.png"),
            None,
        )
        if selected is None:
            raise RuntimeError("The expected Commons candidate was not returned")
        return (selected,)


def query_plans(_claim):
    from btcedu.models.editorial_schema import ResearchQueryPlan

    return (
        ResearchQueryPlan(
            query_key="public-primary",
            query_text="bitcoin block chain evidence",
            language="en",
            purpose="support",
            estimated_cost_usd=0,
        ),
        ResearchQueryPlan(
            query_key="public-counter-check",
            query_text="bitcoin block chain counter-check",
            language="en",
            purpose="counter",
            estimated_cost_usd=0,
        ),
    )


def demo_model(payload: dict) -> ModelReply:
    task = payload["task"]
    if task == "extract_claims":
        result = [
            {
                "claim_key": "block-link",
                "statement": (
                    "Jeder Block in der Blockchain enthaelt einen Hash des vorherigen "
                    "Blocks, was eine lueckenlose Kette von Bloecken erzeugt."
                ),
                "claim_type": "fact",
                "subject": "block",
            }
        ]
    elif task == "evaluate_claim_evidence":
        documents = {row["canonical_url"]: row["text"] for row in payload["documents"]}
        if DEV_PASSAGE not in documents.get(DEV_URL, ""):
            raise RuntimeError("Expected passage is missing from the fetched developer guide")
        if IBM_PASSAGE not in documents.get(IBM_URL, ""):
            raise RuntimeError("Expected passage is missing from the fetched IBM page")
        result = [
            {
                "canonical_url": DEV_URL,
                "relation": "supports",
                "passage": DEV_PASSAGE,
                "rationale": (
                    "DEMO assessment: exact fetched passage names the previous block header hash."
                ),
            },
            {
                "canonical_url": IBM_URL,
                "relation": "supports",
                "passage": IBM_PASSAGE,
                "rationale": (
                    "DEMO assessment: independent fetched explainer describes the same linkage."
                ),
            },
        ]
    elif task == "draft_article":
        result = {
            "title": "DEMO: Bitcoin blokları birbirine nasıl bağlanıyor?",
            "lede": (
                "Bu yerel kabul önizlemesi, Newsroom akışını göstermek için hazırlanmıştır; "
                "yayınlanmış bir haber değildir."
            ),
            "paragraphs": [
                {
                    "kind": "context",
                    "text": (
                        "Almanca kaynak metin depo içindeki test transkriptinden alınmıştır. "
                        "Aşağıdaki kaynak sayfaları ve Commons görseli gerçektir; içerik "
                        "değerlendirmesi ve Türkçe metin demonstrasyon verisidir."
                    ),
                    "claim_keys": [],
                },
                {
                    "kind": "body",
                    "text": (
                        "Bitcoin blok zincirinde her blok, önceki bloğun başlık özetini "
                        "saklayarak zincire bağlanır."
                    ),
                    "claim_keys": ["block-link"],
                },
                {
                    "kind": "body",
                    "text": (
                        "Bu bağlantı, geçmiş bir işlemi değiştirme girişiminin onu izleyen "
                        "bloklarla birlikte ele alınmasını gerektiren zincir yapısının "
                        "temelini oluşturur."
                    ),
                    "claim_keys": ["block-link"],
                },
            ],
        }
    elif task == "check_article_consistency":
        result = {"consistent": True, "issues": []}
    else:
        raise RuntimeError(f"Unexpected demonstration task: {task}")
    return ModelReply(result=result, cost_usd=0, model="transparent-demo-fixture")


def prepare_root() -> None:
    for path in (DB_PATH,):
        path.unlink(missing_ok=True)
    for path in (PRIVATE_ROOT, SITE_ROOT, SCREENSHOT_ROOT, SCREENSHOT_HTML_ROOT):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)


def build() -> dict:
    prepare_root()
    settings = Settings(
        database_url=f"sqlite:///{DB_PATH}",
        newsroom_enabled=True,
        newsroom_data_dir=str(PRIVATE_ROOT),
        newsroom_site_dir=str(SITE_ROOT),
        newsroom_site_name="ALMANYA24 DEV — DEMO",
        newsroom_site_base_url="https://sahimi.app/almanya24-dev",
        newsroom_site_imprint=(
            "Yerel kabul önizlemesi. Yayıncı künyesi değildir; yayın öncesi doldurulmalıdır."
        ),
        newsroom_site_privacy=(
            "Yerel statik demo; izleme, çerez veya harici betik içermez."
        ),
        newsroom_site_contact="Bu demo için iletişim bilgisi yayımlanmamıştır.",
        newsroom_site_usage_rights=(
            "Metin demonstrasyon verisidir. Görsel lisansı ve kaynak bağlantıları "
            "makale üzerinde gösterilir."
        ),
        dry_run=True,
    )
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        paragraphs = SOURCE_FIXTURE.read_text(encoding="utf-8").split("\n\n")
        source_text = paragraphs[2].strip()
        story = Story(
            story_id="demo-bitcoin-blockchain",
            order=1,
            headline_de="Wie Bitcoin-Blöcke verkettet werden",
            category=StoryCategory.WIRTSCHAFT,
            story_type=StoryType.BERICHT,
            text_de=source_text,
            source_text=source_text,
            source_segment_ids=["fixture-paragraph-3"],
            source_start_seconds=0,
            source_end_seconds=30,
            word_count=len(source_text.split()),
            estimated_duration_seconds=30,
        )
        search_provider = FixtureSearchProvider(
            {
                ("bitcoin block chain evidence", "en"): SearchResponse(
                    provider="manual-public-source-demo",
                    query="bitcoin block chain evidence",
                    hits=(
                        SearchHit(
                            title="Block Chain — Bitcoin Developer Guide",
                            url=DEV_URL,
                            publisher="developer.bitcoin.org",
                        ),
                    ),
                    request_id="demo-manual-source-1",
                    cost_usd=0,
                ),
                ("bitcoin block chain counter-check", "en"): SearchResponse(
                    provider="manual-public-source-demo",
                    query="bitcoin block chain counter-check",
                    hits=(
                        SearchHit(
                            title="What Is Blockchain?",
                            url=IBM_URL,
                            publisher="IBM",
                        ),
                    ),
                    request_id="demo-manual-source-2",
                    cost_usd=0,
                ),
            }
        )
        fetcher = DocumentFetcher.from_settings(settings)
        media_provider = SelectedCommonsProvider(fetcher)
        article = draft_story(
            session,
            episode_id="demo-transcript-fixture",
            story=story,
            settings=settings,
            model_caller=demo_model,
            search_provider=search_provider,
            fetcher=fetcher,
            media_provider=media_provider,
            media_requirement=MediaRequirement(
                subject="Bitcoin",
                role=MediaRole.SYMBOLIC,
                caption="Bitcoin işareti — sembolik görsel",
            ),
            provider_name="transparent-demo-fixture",
            model_name="no-paid-model",
            max_call_cost_usd=0,
            plan_builder=query_plans,
            source_published_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
        run = session.query(ResearchRun).order_by(ResearchRun.id.desc()).one()
        preview = article_preview(session, article, research_run=run)
        approve_article_revision(
            session,
            article,
            research_run=run,
            operator_ref="demo-preview-only:not-user-approval",
            reviewed_content_hash=preview["content_hash"],
            reviewed_evidence_hash=preview["evidence_hash"],
            reviewed_media_hash=preview["media_hash"],
            rationale=(
                "Isolated acceptance preview only; not an editorial or publication approval."
            ),
        )
        publication = publish_article(
            session,
            article,
            operator_ref="demo-preview-only:not-user-approval",
            section="haber",
        )
        config = SiteConfig.from_settings(settings)
        result = build_site(
            session,
            root=SITE_ROOT,
            config=config,
            operator_ref="demo-preview-only",
        )
        current = switch_release(session, root=SITE_ROOT, release_id=result.release_id)
        css = (current / "assets" / "site.css").read_text(encoding="utf-8")
        screenshot_inputs = {}
        for name, relative in {
            "start": Path("index.html"),
            "article": Path(publication.section) / publication.slug / "index.html",
        }.items():
            html = (current / relative).read_text(encoding="utf-8")
            html = html.replace(
                '<link rel="stylesheet" href="/assets/site.css">',
                f"<style>{css}</style>",
            )
            html = html.replace(
                'src="/media/',
                f'src="{current.resolve().as_uri()}/media/',
            )
            destination = SCREENSHOT_HTML_ROOT / f"{name}.html"
            destination.write_text(html, encoding="utf-8")
            screenshot_inputs[name] = destination.resolve().as_uri()

        observations = session.query(SourceObservation).order_by(SourceObservation.id).all()
        evidence = session.query(EvidenceLink).order_by(EvidenceLink.id).all()
        assessment = session.query(ClaimAssessment).one()
        decision = session.query(MediaUseDecision).one()
        offer = session.get(MediaSourceOffer, decision.media_source_offer_id)
        license_row = session.get(LicenseEvidence, decision.license_evidence_id)
        manifest = {
            "generated_at": datetime.now(UTC).isoformat(),
            "code_commit": "e10767bc64e40d36f9434048bee5955e24887ce8",
            "database": str(DB_PATH),
            "site_root": str(current.resolve()),
            "start_url": "https://sahimi.app/almanya24-dev/",
            "article_url": (
                "https://sahimi.app/almanya24-dev/"
                f"{publication.section}/{publication.slug}/"
            ),
            "screenshot_inputs": screenshot_inputs,
            "source_transcript": {
                "kind": "repository test fixture",
                "path": str(SOURCE_FIXTURE),
                "text_used": source_text,
            },
            "real_public_retrieval": [
                {
                    "url": row.canonical_url,
                    "title": row.title,
                    "publisher": row.publisher,
                    "retrieved_at": row.retrieved_at.isoformat()
                    if row.retrieved_at
                    else None,
                    "content_hash": row.content_hash,
                }
                for row in observations
                if row.fetch_status == "fetched"
            ],
            "evidence_links": [
                {
                    "relation": row.relation,
                    "passage": row.passage,
                    "rationale": row.rationale,
                    "provenance_family": row.provenance_family,
                }
                for row in evidence
            ],
            "assessment": {
                "verdict": assessment.verdict,
                "rationale": assessment.rationale,
                "warning": (
                    "The verdict is produced by transparent demonstration logic, "
                    "not by a paid model and not by a human fact-checker."
                ),
            },
            "real_commons_selection": {
                "candidate_count": len(media_provider.candidates_seen),
                "selected_title": offer.title,
                "page_url": offer.page_url,
                "file_url": offer.file_url,
                "author": offer.author,
                "license": license_row.license_id,
                "license_url": license_row.license_url,
                "attribution": decision.attribution_text,
                "role": decision.role,
            },
            "simulated": [
                "claim extraction",
                "semantic evidence classification",
                "Turkish article drafting",
                "semantic article consistency decision",
                "isolated demo approvals",
                "manual source discovery/search results",
            ],
            "not_user_approved": True,
            "publication_status": "local static preview only",
            "article_count": result.article_count,
            "release_id": result.release_id,
        }
        (ROOT / "preview-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (ROOT / "PREVIEW-README.txt").write_text(
            "ALMANYA24 development acceptance preview\n"
            "========================================\n"
            "Public URL: https://sahimi.app/almanya24-dev/\n"
            f"Article: {manifest['article_url']}\n"
            "This is not a publication and contains demo editorial decisions.\n"
            "See preview-manifest.json for exact real/simulated provenance.\n",
            encoding="utf-8",
        )
        return manifest
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
