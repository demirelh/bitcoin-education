"""Show the drafts that exist in the development preview, unapproved.

This is the operator side of ``Settings.newsroom_dev_auto_release``. It takes
the article revisions already stored in the isolated preview database, offers
them under a public identity *without* recording an approval, rebuilds the
site with the development notice switched on, and regenerates the review page
so the assessments stay visible next to the article.

Nothing here calls a model, fetches a source or writes a decision: a draft that
was already paid for is made visible, and the reasons it is not publishable
travel onto the page with it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import EditorialRevision

if __package__:
    from .paths import data_root
else:  # executed directly from the command line
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root

ROOT = data_root()
DB_PATH = ROOT / "current-news.sqlite"
SITE_ROOT = ROOT / "site"
OPERATOR = "dev-auto-release:not-user-editorial-approval"


def _switch_is_on() -> bool:
    """Read the switch rather than assume it.

    The point of a switch that defaults to off is lost if the one program that
    uses it hardcodes ``True``. Reading the environment directly keeps the
    production ``.env`` out of it: nothing has to be edited to run this, and
    nothing stays on afterwards.
    """
    return os.environ.get("NEWSROOM_DEV_AUTO_RELEASE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _latest_revision_per_topic(session: Session) -> list[ArticleRevision]:
    """One article per topic: the newest revision, not every discarded attempt.

    A publication is keyed by topic anyway, so an earlier attempt could never
    become a second headline. Selecting here keeps the repair history out of
    the loop entirely instead of relying on that.
    """
    latest: dict[int, ArticleRevision] = {}
    query = (
        session.query(ArticleRevision, EditorialRevision.topic_id)
        .join(
            EditorialRevision,
            EditorialRevision.id == ArticleRevision.editorial_revision_id,
        )
        .order_by(ArticleRevision.id)
    )
    for article, topic_id in query:
        latest[topic_id] = article
    return list(latest.values())


def release(*, section: str = "haber") -> dict:
    if not _switch_is_on():
        raise SystemExit(
            "Refusing to run: the development auto-release switch is off.\n"
            "It is off by default and must be turned on explicitly, for the\n"
            "isolated environment only, e.g.\n"
            "  NEWSROOM_DEV_AUTO_RELEASE=true python -m "
            "scripts.almanya24_preview.release_dev_drafts"
        )
    if not DB_PATH.exists():
        raise SystemExit(f"No preview database at {DB_PATH}")
    engine = create_engine(f"sqlite:///{DB_PATH}")
    offered: list[str] = []
    failed: list[str] = []
    with Session(engine) as session:
        for article in _latest_revision_per_topic(session):
            try:
                publication = publish_article(
                    session,
                    article,
                    operator_ref=OPERATOR,
                    section=section,
                    dev_auto_release=True,
                )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                session.rollback()
                failed.append(f"{article.title}: {type(exc).__name__}: {exc}")
                continue
            offered.append(f"/{publication.section}/{publication.slug}/")

        config = SiteConfig(
            site_name="ALMANYA24",
            base_url="https://sahimi.app/almanya24-dev",
            tagline="Almanya, Türkiye ve dünyadan doğrulanmış haberler",
            preview_notice=(
                "Korumalı geliştirme önizlemesi · İçerikler otomatik olarak "
                "geliştirme için yayına alınmıştır, insan yayın onayı almamıştır."
            ),
            imprint="Künye",
            privacy="Gizlilik",
            contact="dev@localhost",
            usage_rights="Geliştirme önizlemesi",
            dev_auto_release=True,
        )
        result = build_site(
            session, root=SITE_ROOT, config=config, operator_ref=OPERATOR
        )
        current = switch_release(session, root=SITE_ROOT, release_id=result.release_id)

    if __package__:
        from . import build_review_page
    else:
        import build_review_page  # type: ignore[no-redef]

    review = build_review_page.build()
    return {
        "offered": offered,
        "failed": failed,
        "articles": result.article_count,
        "live": str(current),
        "review": str(review),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", default="haber")
    args = parser.parse_args()
    report = release(section=args.section)
    for key, value in report.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
