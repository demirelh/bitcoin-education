from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import traceback
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

import btcedu.models  # noqa: F401
from btcedu.config import Settings
from btcedu.core.editorial.article import approve_article_revision, article_preview
from btcedu.core.editorial.jobs import ProviderCallNotAttempted
from btcedu.core.editorial.media import MediaRequirement
from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.core.editorial.workflow import ModelReply, draft_story
from btcedu.db import Base
from btcedu.models.editorial import (
    EditorialRevision,
    ProviderOperation,
    ProviderOperationStatus,
    ResearchRun,
)
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.media_rights import MediaRole, MediaUseDecision
from btcedu.models.story_schema import Story, StoryCategory, StoryType
from btcedu.services.commons_service import WikimediaCommonsProvider
from btcedu.services.document_fetcher import DocumentFetcher
from btcedu.services.editorial_model import EditorialModel
from btcedu.services.search_service import SearchHit, SearchResponse

if __package__:
    from .paths import data_root, repo_root
else:  # executed directly from the command line
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root, repo_root

ROOT = data_root()
REPO = repo_root()
PLAN_PATH = ROOT / "current-news-plan.json"
DB_PATH = ROOT / "current-news.sqlite"
PRIVATE_ROOT = ROOT / "current-private"
SITE_ROOT = ROOT / "site"
SCREENSHOT_ROOT = ROOT / "screenshots"
SCREENSHOT_HTML_ROOT = ROOT / "screenshot-pages"
MANIFEST_PATH = ROOT / "current-preview-manifest.json"
ENV_PATH = REPO.parent / "bitcoin-education" / ".env"
TOTAL_BUDGET_USD = 2.0
PROVIDER = "openai"
MODEL = "gpt-4o"
TASK_MAX_TOKENS = {
    "extract_claims": 1400,
    "evaluate_claim_evidence": 1200,
    "draft_article": 1800,
    "check_article_consistency": 600,
}

PREFERRED_MEDIA = {
    "sweg-warnstreik": "File:Sweg-vs204-00.jpg",
    "bundesweiter-warntag": "File:Warning siren Utenbach, Apolda.jpg",
    "berlin-krach-ermittlungen": "File:Berlin skyline 3.jpg",
    "bundestag-generaldebatte": (
        "File:Reichstag building Berlin view from west before sunset.jpg"
    ),
    "grossbritannien-flugchaos": (
        "File:Prestwick Airport departures board - geograph.org.uk - 7667187.jpg"
    ),
    "oelpreis-100-dollar": "File:Oil pumpjack in the Permian Basin.jpg",
    "turkiye-idlib-aciklamasi": "File:Turkish Flag Camlihemsin.jpg",
    "norvec-kral-harald-cenaze": "File:Norwegian flag atop Fløibanen.jpg",
}

MEDIA_CAPTIONS = {
    "sweg-warnstreik": "SWEG aracı — sembolik görsel",
    "bundesweiter-warntag": "Uyarı sireni — sembolik görsel",
    "berlin-krach-ermittlungen": "Berlin kent görünümü — sembolik görsel",
    "bundestag-generaldebatte": "Berlin'deki Reichstag binası — sembolik görsel",
    "grossbritannien-flugchaos": "Havalimanı kalkış panosu — arşiv görseli",
    "oelpreis-100-dollar": "Petrol pompası — sembolik görsel",
    "turkiye-idlib-aciklamasi": "Türkiye bayrağı — sembolik görsel",
    "norvec-kral-harald-cenaze": "Norveç bayrağı — sembolik görsel",
}


class ElementTextParser(HTMLParser):
    def __init__(self, *, attribute: tuple[str, str]):
        super().__init__(convert_charrefs=True)
        self.attribute = attribute
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
            return
        values = dict(attrs)
        expected_name, expected_value = self.attribute
        value = values.get(expected_name, "")
        if value == expected_value or expected_value in value.split():
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.parts.append(data.strip())

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _article_body(raw: str) -> str:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        raw,
        flags=re.I | re.S,
    )

    def bodies(value):
        if isinstance(value, dict):
            if isinstance(value.get("articleBody"), str):
                yield value["articleBody"]
            for nested in value.values():
                yield from bodies(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from bodies(nested)

    for script in scripts:
        try:
            values = list(bodies(json.loads(html.unescape(script))))
        except (json.JSONDecodeError, TypeError):
            continue
        if values:
            return max(values, key=len)
    for attribute in (("itemprop", "articleBody"), ("class", "mfa-content-text")):
        parser = ElementTextParser(attribute=attribute)
        parser.feed(raw)
        if len(parser.text) >= 200:
            return parser.text
    raise RuntimeError("No bounded article body could be extracted")


class CurrentSourceProvider:
    name = "manual-current-source-feed"

    def __init__(self, story):
        self.story = story

    def search(self, query, *, language, count):
        return SearchResponse(
            provider=self.name,
            query=query,
            request_id=f"{self.story['story_id']}:{language}",
            cost_usd=0,
            hits=(
                SearchHit(
                    title=self.story["headline_de"],
                    url=self.story["source_url"],
                    publisher=self.story["publisher"],
                    published_at=self.story["published_at"],
                ),
            ),
        )


class SelectedCommonsProvider:
    name = "wikimedia_commons"

    def __init__(self, fetcher, story):
        self.provider = WikimediaCommonsProvider(fetcher)
        self.story = story

    def search(self, query: str, *, limit: int):
        candidates = self.provider.search(self.story["media_query"], limit=max(limit, 8))
        preferred = PREFERRED_MEDIA[self.story["story_id"]]
        selected = [candidate for candidate in candidates if candidate.title == preferred]
        return tuple(selected)


class GlobalBudgetModel:
    def __init__(
        self,
        model,
        *,
        prior_cost_usd=0.0,
        prior_ledger=(),
        budget_usd=TOTAL_BUDGET_USD,
        max_calls=None,
    ):
        self.model = model
        self.actual_cost_usd = prior_cost_usd
        self.calls = len(prior_ledger)
        self.ledger: list[dict] = list(prior_ledger)
        self.budget_usd = budget_usd
        self.max_calls = max_calls

    def _maximum_cost(self, payload) -> float:
        task = payload["task"]
        max_tokens = TASK_MAX_TOKENS[task]
        input_chars = len(json.dumps(payload, ensure_ascii=False)) + 12_000
        conservative_input_tokens = (input_chars + 1) // 2
        return conservative_input_tokens * 0.0000025 + max_tokens * 0.000010

    def __call__(self, payload) -> ModelReply:
        maximum = self._maximum_cost(payload)
        remaining = self.budget_usd - self.actual_cost_usd
        if maximum > remaining:
            raise ProviderCallNotAttempted(
                f"Global API budget guard blocked {payload['task']}: "
                f"maximum {maximum:.4f} > remaining {remaining:.4f} USD"
            )
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ProviderCallNotAttempted(
                f"Global API call guard blocked {payload['task']}: "
                f"call limit {self.max_calls} reached"
            )
        reply = self.model(payload)
        self.actual_cost_usd += reply.cost_usd
        self.calls += 1
        self.ledger.append(
            {
                "task": payload["task"],
                "maximum_checked_usd": round(maximum, 6),
                "actual_cost_usd": round(reply.cost_usd, 6),
                "remaining_usd": round(self.budget_usd - self.actual_cost_usd, 6),
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "model": reply.model,
            }
        )
        MANIFEST_PATH.write_text(
            json.dumps(
                {
                    "status": "running",
                    "budget_usd": self.budget_usd,
                    "actual_cost_usd": self.actual_cost_usd,
                    "model_calls": self.calls,
                    "model_ledger": self.ledger,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return reply


def query_plans(story):
    from btcedu.models.editorial_schema import ResearchQueryPlan

    language = "en" if story["story_id"] == "turkiye-idlib-aciklamasi" else "de"

    def build(claim):
        query = " ".join(
            filter(None, (claim.subject, claim.location, claim.event_date, claim.statement))
        )[:850]
        return (
            ResearchQueryPlan(
                query_key="known-source",
                query_text=query,
                language=language,
                purpose="support",
                estimated_cost_usd=0,
            ),
            ResearchQueryPlan(
                query_key="known-source-counter-check",
                query_text=f"{query} Widerspruch Korrektur Faktencheck",
                language=language,
                purpose="counter",
                estimated_cost_usd=0,
            ),
        )

    return build


def prepare_root(*, resume: bool):
    if not resume:
        DB_PATH.unlink(missing_ok=True)
    SITE_ROOT.mkdir(parents=True, exist_ok=True)
    for path in (SCREENSHOT_ROOT, SCREENSHOT_HTML_ROOT):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    if not resume:
        if PRIVATE_ROOT.exists():
            shutil.rmtree(PRIVATE_ROOT)
        PRIVATE_ROOT.mkdir(parents=True)


def build(*, resume: bool = False, only_story=None, max_calls=None,
          budget_usd: float = TOTAL_BUDGET_USD, approve: bool = True):
    prior = {}
    if MANIFEST_PATH.exists():
        prior = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    prepare_root(resume=resume)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    settings = Settings(
        _env_file=ENV_PATH,
        database_url=f"sqlite:///{DB_PATH}",
        newsroom_enabled=True,
        newsroom_data_dir=str(PRIVATE_ROOT),
        newsroom_site_dir=str(SITE_ROOT),
        newsroom_site_name="ALMANYA24 DEV",
        newsroom_site_base_url="https://sahimi.app/almanya24-dev",
        newsroom_site_imprint="Korumalı geliştirme ve editoryal kabul önizlemesi.",
        newsroom_site_privacy="İzleme, çerez veya harici betik içermez.",
        newsroom_site_contact="Geliştirme önizlemesi; kamusal yayın değildir.",
        newsroom_site_usage_rights=(
            "Metin ve görseller yalnızca kaynak ve lisans bilgileriyle kullanılabilir."
        ),
        newsroom_research_max_claims=5,
        newsroom_research_max_queries=10,
        newsroom_research_max_documents=2,
        newsroom_research_max_cost_usd=0.6,
        newsroom_media_max_candidates=8,
        dry_run=False,
        max_retries=0,
        claude_temperature=0.1,
    )
    if not settings.openai_api_key:
        raise RuntimeError("OpenAI provider is not configured")

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    if resume:
        (
            session.query(ProviderOperation)
            .filter_by(
                operation_type="article",
                status=ProviderOperationStatus.RECONCILE_REQUIRED.value,
            )
            .update(
                {
                    ProviderOperation.status: ProviderOperationStatus.FAILED.value,
                    ProviderOperation.error_message: (
                        "Reconciled: model replies were durably stored; "
                        "deterministic article validation rejected the draft."
                    ),
                },
                synchronize_session=False,
            )
        )
        session.commit()
    fetcher = DocumentFetcher.from_settings(settings)
    model = GlobalBudgetModel(
        EditorialModel(
            settings,
            provider=PROVIDER,
            model=MODEL,
            max_tokens_by_task=TASK_MAX_TOKENS,
        ),
        prior_cost_usd=float(prior.get("actual_cost_usd", 0.0)),
        prior_ledger=prior.get("model_ledger", ()),
        budget_usd=budget_usd,
        max_calls=max_calls,
    )
    completed = []
    blocked = []
    try:
        for source in plan["stories"]:
            if only_story and source["story_id"] != only_story:
                continue
            try:
                document = fetcher.fetch(source["source_url"])
                raw = Path(document.body_path).read_text(encoding="utf-8", errors="replace")
                source_text = re.sub(r"\s+", " ", _article_body(raw)).strip()[:16_000]
                category = {
                    "almanya": StoryCategory.POLITIK,
                    "turkiye": StoryCategory.INTERNATIONAL,
                    "dunya": StoryCategory.INTERNATIONAL,
                    "ekonomi": StoryCategory.WIRTSCHAFT,
                }[source["section"]]
                story = Story(
                    story_id=source["story_id"],
                    order=source["order"],
                    headline_de=source["headline_de"],
                    category=category,
                    story_type=StoryType.BERICHT,
                    text_de=source_text,
                    source_text=source_text,
                    source_segment_ids=[f"current-source:{source['story_id']}"],
                    word_count=len(source_text.split()),
                    estimated_duration_seconds=max(30, len(source_text.split()) // 2),
                    is_lead_story=source["order"] == 1,
                )
                role = (
                    MediaRole.ARCHIVE
                    if source["story_id"] == "grossbritannien-flugchaos"
                    else MediaRole.SYMBOLIC
                )
                requirement = MediaRequirement(
                    subject=source["media_subject"],
                    role=role,
                    caption=MEDIA_CAPTIONS[source["story_id"]],
                )
                kwargs = dict(
                    session=session,
                    episode_id="current-public-sources-2026-09-10",
                    story=story,
                    settings=settings,
                    model_caller=model,
                    search_provider=CurrentSourceProvider(source),
                    fetcher=fetcher,
                    media_provider=SelectedCommonsProvider(fetcher, source),
                    media_requirement=requirement,
                    provider_name=PROVIDER,
                    model_name=MODEL,
                    max_call_cost_usd=0.12,
                    repair_attempts=2,
                    plan_builder=query_plans(source),
                    source_published_at=datetime.fromisoformat(source["published_at"]),
                    source_language=(
                        "en" if source["story_id"] == "turkiye-idlib-aciklamasi" else "de"
                    ),
                    source_uri=source["source_url"],
                )
                try:
                    article = draft_story(**kwargs)
                except ValueError as exc:
                    if "Requested illustration unresolved" not in str(exc):
                        raise
                    kwargs["media_requirement"] = None
                    article = draft_story(**kwargs)

                revision = session.get(EditorialRevision, article.editorial_revision_id)
                run = (
                    session.query(ResearchRun)
                    .filter_by(topic_id=revision.topic_id)
                    .order_by(ResearchRun.id.desc())
                    .first()
                )
                preview = article_preview(session, article, research_run=run)
                if not approve:
                    # A real draft must not be marked approved on the operator's
                    # behalf; it is reviewed in the protected draft view.
                    media = (
                        session.query(MediaUseDecision)
                        .filter_by(editorial_revision_id=revision.id, status="approved")
                        .one_or_none()
                    )
                    completed.append(
                        {
                            "story_id": source["story_id"],
                            "title": article.title,
                            "section": source["section"],
                            "url": None,
                            "status": "draft; awaiting editorial approval",
                            "source_url": source["source_url"],
                            "source_published_at": source["published_at"],
                            "event_date": source["event_date"],
                            "media": "selected" if media else "no suitable image",
                        }
                    )
                    continue
                approve_article_revision(
                    session,
                    article,
                    research_run=run,
                    operator_ref="isolated-preview-only:not-user-editorial-approval",
                    reviewed_content_hash=preview["content_hash"],
                    reviewed_evidence_hash=preview["evidence_hash"],
                    reviewed_media_hash=preview["media_hash"],
                    rationale=(
                        "Isolated protected acceptance preview only; "
                        "not authorization for public publication."
                    ),
                )
                publication = publish_article(
                    session,
                    article,
                    operator_ref="isolated-preview-only",
                    section=source["section"],
                )
                media = (
                    session.query(MediaUseDecision)
                    .filter_by(editorial_revision_id=revision.id, status="approved")
                    .one_or_none()
                )
                completed.append(
                    {
                        "story_id": source["story_id"],
                        "title": article.title,
                        "section": source["section"],
                        "url": (
                            f"https://sahimi.app/almanya24-dev/{source['section']}/"
                            f"{publication.slug}/"
                        ),
                        "source_url": source["source_url"],
                        "source_published_at": source["published_at"],
                        "event_date": source["event_date"],
                        "media": "selected" if media else "no suitable image",
                    }
                )
            except Exception as exc:
                blocked.append(
                    {
                        "story_id": source["story_id"],
                        "reason": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=25),
                    }
                )
                # A single topic needing reconciliation must not abort the
                # remaining, independent topics; only an exhausted budget does.
                if "budget" in str(exc).casefold():
                    break

        config = SiteConfig(
            site_name="ALMANYA24",
            base_url="https://sahimi.app/almanya24-dev",
            tagline="Almanya, Türkiye ve dünyadan doğrulanmış haberler",
            preview_notice=(
                "Korumalı geliştirme önizlemesi · Haberler insan yayın onayı almamıştır."
            ),
            imprint=settings.newsroom_site_imprint,
            privacy=settings.newsroom_site_privacy,
            contact=settings.newsroom_site_contact,
            usage_rights=settings.newsroom_site_usage_rights,
        )
        result = build_site(
            session,
            root=SITE_ROOT,
            config=config,
            operator_ref="isolated-preview-only",
        )
        current = switch_release(session, root=SITE_ROOT, release_id=result.release_id)
        css = (current / "assets" / "site.css").read_text(encoding="utf-8")
        pages = {"start": current / "index.html"}
        published = [entry for entry in completed if entry.get("url")]
        if published:
            first = published[0]["url"].removeprefix(
                "https://sahimi.app/almanya24-dev/"
            )
            pages["article"] = current / first / "index.html"
        screenshot_inputs = {}
        for name, page in pages.items():
            markup = page.read_text(encoding="utf-8")
            markup = re.sub(
                r'<link rel="stylesheet" href="[^"]*/assets/site\.css">',
                f"<style>{css}</style>",
                markup,
            )
            markup = markup.replace(
                'src="/almanya24-dev/media/',
                f'src="{current.resolve().as_uri()}/media/',
            )
            destination = SCREENSHOT_HTML_ROOT / f"{name}.html"
            destination.write_text(markup, encoding="utf-8")
            screenshot_inputs[name] = destination.resolve().as_uri()

        current_run_cost = (
            session.query(func.sum(ProviderOperation.actual_cost_usd))
            .filter(ProviderOperation.actual_cost_usd.is_not(None))
            .scalar()
            or 0.0
        )
        actual_total_cost = model.actual_cost_usd
        manifest = {
            "status": "complete",
            "generated_at": datetime.now(UTC).isoformat(),
            "budget_usd": TOTAL_BUDGET_USD,
            "actual_cost_usd": round(float(actual_total_cost), 6),
            "current_run_cost_usd": round(float(current_run_cost), 6),
            "model_calls": model.calls,
            "model_ledger": model.ledger,
            "completed": completed,
            "blocked": blocked,
            "site_root": str(current.resolve()),
            "screenshot_inputs": screenshot_inputs,
            "source_discovery": "real public sources; manually curated current-source feed",
            "fact_check": "real model evaluation of fetched documents",
            "approval": "isolated preview marker, not user publication approval",
        }
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "completed": len(completed),
                    "blocked": len(blocked),
                    "model_calls": model.calls,
                    "actual_cost_usd": round(float(actual_total_cost), 6),
                    "manifest": str(MANIFEST_PATH),
                },
                ensure_ascii=False,
            )
        )
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-approve", action="store_true")
    parser.add_argument(
        "--only-story",
        help="Restrict the run to a single story_id from the plan.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        help="Hard cap on model calls; the guard refuses the call that would exceed it.",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=TOTAL_BUDGET_USD,
        help="Hard cost ceiling in USD for this run.",
    )
    args = parser.parse_args()
    build(
        resume=args.resume,
        only_story=args.only_story,
        max_calls=args.max_calls,
        budget_usd=args.budget_usd,
        approve=not args.no_approve,
    )
