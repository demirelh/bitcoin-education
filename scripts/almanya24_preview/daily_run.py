"""The unattended daily newsroom run, wired to real providers.

``btcedu.core.editorial.daily`` holds the control flow and the limits; this is
the part that decides *which* providers it uses. Keeping them apart is what
lets the offline test drive the identical control flow with fixtures.

Free by construction: the transcript comes from the video pipeline's own
output, search uses public key-less endpoints, and pictures come from Wikimedia
Commons. The only paid component is the editorial model, and it cannot be
called at all unless a daily budget was explicitly granted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.editorial.daily import DailyPaths, RunOutcome, run_daily
from btcedu.core.editorial.limits import DailyLimits
from btcedu.core.editorial.media import MediaRequirement
from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.core.editorial.workflow import draft_story
from btcedu.db import Base
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.media_rights import MediaRole
from btcedu.services.document_fetcher import DocumentFetcher
from btcedu.services.editorial_model import EditorialModel
from btcedu.services.free_search import FreeNewsSearchProvider

if __package__:
    from .paths import data_root
else:  # executed directly from the command line
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root

logger = logging.getLogger("almanya24.daily")

PROVIDER = "openai"
MODEL = "gpt-4o"
PRODUCTION_OUTPUTS = Path("/home/pi/AI-Startup-Lab/bitcoin-education/data/outputs")
PRODUCTION_ENV = Path("/home/pi/AI-Startup-Lab/bitcoin-education/.env")

BASE_URL = "https://sahimi.app/almanya24-dev"
OPERATOR = "almanya24-daily:not-user-editorial-approval"

PREVIEW_NOTICE = (
    "Korumalı geliştirme önizlemesi · İçerikler otomatik olarak geliştirme için "
    "yayına alınmıştır, insan yayın onayı almamıştır."
)


@dataclass(frozen=True)
class DraftResult:
    title: str
    section: str


def _dev_auto_release_enabled() -> bool:
    return os.environ.get("NEWSROOM_DEV_AUTO_RELEASE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def settings_factory(paths: DailyPaths) -> Settings:
    """Development settings: production credentials, development storage.

    The production ``.env`` is read for provider keys and never written. Every
    path that the run writes to is redirected into the development directory,
    so nothing can land in the production database or site.
    """
    return Settings(
        _env_file=PRODUCTION_ENV if PRODUCTION_ENV.exists() else None,
        database_url=f"sqlite:///{paths.database}",
        newsroom_enabled=True,
        newsroom_data_dir=str(paths.private),
        newsroom_site_dir=str(paths.site),
        newsroom_site_name="ALMANYA24",
        newsroom_site_base_url=BASE_URL,
        newsroom_site_imprint="Korumalı geliştirme önizlemesi.",
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
        newsroom_dev_auto_release=_dev_auto_release_enabled(),
        dry_run=False,
        max_retries=0,
        claude_temperature=0.1,
    )


def model_factory(settings: Settings):
    from btcedu.core.editorial.daily import MAX_TOKENS_BY_TASK

    if not settings.openai_api_key:
        raise RuntimeError("No editorial model provider is configured")
    return EditorialModel(
        settings,
        provider=PROVIDER,
        model=MODEL,
        max_tokens_by_task=MAX_TOKENS_BY_TASK,
    )


def _session_for(settings: Settings):
    engine = create_engine(
        settings.database_url, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_drafter(session):
    """Run one broadcast story through the full editorial workflow."""

    def drafter(*, settings, model, selected, published_at: datetime) -> DraftResult:
        from btcedu.services.commons_service import WikimediaCommonsProvider

        fetcher = DocumentFetcher.from_settings(settings)
        requirement = MediaRequirement(
            subject=selected.story.headline_de[:120],
            # Commons holds library pictures, not pictures of tonight's event.
            # Declaring them symbolic is what puts the notice on the page.
            role=MediaRole.SYMBOLIC,
            caption=(
                f"Sembol görsel · {selected.story.headline_de[:110]} "
                f"({selected.broadcast_date.isoformat()})"
            ),
        )
        kwargs = dict(
            session=session,
            episode_id=selected.episode_id,
            story=selected.story,
            settings=settings,
            model_caller=model,
            search_provider=FreeNewsSearchProvider(fetcher),
            fetcher=fetcher,
            media_provider=WikimediaCommonsProvider(fetcher),
            media_requirement=requirement,
            provider_name=PROVIDER,
            model_name=MODEL,
            max_call_cost_usd=0.12,
            repair_attempts=1,
            source_published_at=published_at,
            source_language="de",
            source_uri=None,
        )
        try:
            article = draft_story(**kwargs)
        except ValueError as exc:
            if "Requested illustration unresolved" not in str(exc):
                raise
            # No suitable freely licensed picture. A card with no image is
            # better than a card with an unrelated one.
            kwargs["media_requirement"] = None
            article = draft_story(**kwargs)
        return DraftResult(title=article.title, section=selected.section)

    return drafter


def make_publisher(session):
    """Offer every stored draft and rebuild the protected development site."""

    def publisher(*, settings, paths: DailyPaths) -> dict:
        urls: dict[str, str] = {}
        if not settings.newsroom_dev_auto_release:
            # Without the switch the ordinary approval rules apply and an
            # unapproved draft simply does not appear. Nothing is forced.
            logger.info("Development auto-release is off; only approved articles are built")
        for article in _latest_per_topic(session):
            try:
                publication = publish_article(
                    session,
                    article,
                    operator_ref=OPERATOR,
                    section="haber",
                    dev_auto_release=settings.newsroom_dev_auto_release,
                )
            except Exception as exc:  # noqa: BLE001 - one article is not the run
                session.rollback()
                logger.warning("Could not offer %r: %s", article.title, exc)
                continue
            urls[article.title] = f"{BASE_URL}/{publication.section}/{publication.slug}/"

        config = SiteConfig(
            site_name="ALMANYA24",
            base_url=BASE_URL,
            tagline="Almanya, Türkiye ve dünyadan doğrulanmış haberler",
            preview_notice=PREVIEW_NOTICE,
            imprint=settings.newsroom_site_imprint,
            privacy=settings.newsroom_site_privacy,
            contact=settings.newsroom_site_contact,
            usage_rights=settings.newsroom_site_usage_rights,
            dev_auto_release=settings.newsroom_dev_auto_release,
        )
        result = build_site(
            session, root=paths.site, config=config, operator_ref=OPERATOR
        )
        switch_release(session, root=paths.site, release_id=result.release_id)
        return {"articles": result.article_count, "urls": urls}

    return publisher


def _latest_per_topic(session) -> list[ArticleRevision]:
    """One article per topic: the newest revision, not every repair attempt."""
    latest: dict[int, ArticleRevision] = {}
    rows = (
        session.query(ArticleRevision, EditorialRevision.topic_id)
        .join(
            EditorialRevision,
            EditorialRevision.id == ArticleRevision.editorial_revision_id,
        )
        .order_by(ArticleRevision.id)
    )
    for article, topic_id in rows:
        latest[topic_id] = article
    return list(latest.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--max-stories", type=int, required=True)
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--outputs-dir", type=Path, default=PRODUCTION_OUTPUTS)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    paths = DailyPaths(data_dir=args.data_dir or data_root())
    limits = DailyLimits(
        budget_usd=args.budget_usd,
        max_calls=args.max_calls,
        max_stories=args.max_stories,
    )
    settings = settings_factory(paths)
    session = _session_for(settings)
    try:
        report = run_daily(
            paths=paths,
            limits=limits,
            outputs_dir=args.outputs_dir,
            settings_factory=lambda _paths: settings,
            model_factory=model_factory,
            drafter=make_drafter(session),
            publisher=make_publisher(session),
            lookback_days=args.lookback_days,
        )
    finally:
        session.close()

    _render_status(paths)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2)[:4000])
    # A run that found nothing to do is not a failure the timer should flag.
    return 0 if report.outcome != RunOutcome.ERROR.value else 1


def _render_status(paths: DailyPaths) -> None:
    if __package__:
        from . import build_review_page, build_status_page
    else:
        import build_review_page  # type: ignore[no-redef]
        import build_status_page  # type: ignore[no-redef]

    try:
        build_status_page.build(paths)
    except Exception as exc:  # noqa: BLE001 - the status page is not the run
        logger.warning("Could not render the status page: %s", exc)
    try:
        build_review_page.build()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not render the review page: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
