"""Give already drafted articles a picture from the curated collection.

Articles drafted before the collection existed carry no picture. This attaches
the matching collection picture to each one that has none, through the same
rights check and storage as drafting, then rebuilds the development site. No
model is called; the only network traffic is the Commons lookup and download.

    python -m scripts.almanya24_preview.attach_collection_media [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from btcedu.core.editorial.article import media_hash
from btcedu.core.editorial.daily import DailyPaths, RunLock
from btcedu.core.editorial.media import (
    MediaBlobStore,
    approved_revision_media,
    select_media_for_revision,
)
from btcedu.core.editorial.media_collection import (
    CollectionCommonsProvider,
    WikidataPortraitLookup,
    requirement_for,
    select_entry,
)
from btcedu.models.article import ArticleParagraph, ArticleStatus
from btcedu.models.editorial import EditorialRevision
from btcedu.services.document_fetcher import DocumentFetcher

if __package__:
    from .daily_run import (
        _latest_per_topic,
        _render_status,
        _session_for,
        learned_collection_path,
        make_publisher,
        settings_factory,
    )
    from .paths import data_root
else:  # executed directly from the command line
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from daily_run import (  # type: ignore[no-redef]
        _latest_per_topic,
        _render_status,
        _session_for,
        learned_collection_path,
        make_publisher,
        settings_factory,
    )
    from paths import data_root

logger = logging.getLogger("almanya24.collection_media")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="only report the matches")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    paths = DailyPaths(data_dir=args.data_dir or data_root())
    settings = settings_factory(paths)
    session = _session_for(settings)
    fetcher = DocumentFetcher.from_settings(settings)
    lookup = WikidataPortraitLookup(fetcher, learned_collection_path(settings))
    store = MediaBlobStore.from_settings(settings)
    attached = 0
    try:
        with RunLock(paths.lock):
            for article in _latest_per_topic(session):
                revision = session.get(EditorialRevision, article.editorial_revision_id)
                if approved_revision_media(session, revision):
                    continue
                body = " ".join(
                    row.text
                    for row in session.query(ArticleParagraph)
                    .filter_by(article_revision_id=article.id)
                    .order_by(ArticleParagraph.position)
                )
                entry = select_entry([(article.title, 3), (article.lede, 2), (body, 1)], lookup)
                if entry is None:
                    logger.info("No collection match: %s", article.title)
                    continue
                if args.dry_run:
                    logger.info("Would attach %s: %s", entry.key, article.title)
                    continue
                selection = select_media_for_revision(
                    session,
                    editorial_revision=revision,
                    requirement=requirement_for(entry),
                    provider=CollectionCommonsProvider(fetcher, extra=(entry,)),
                    fetcher=fetcher,
                    blob_store=store,
                    decided_by="auto:collection",
                )
                if not selection.has_picture:
                    logger.warning(
                        "%s not usable for %r: %s %s",
                        entry.key,
                        article.title,
                        selection.reason,
                        selection.rejected,
                    )
                    continue
                if article.status == ArticleStatus.DRAFT.value:
                    # Drafting selects the picture first; keep the draft's
                    # recorded state as if it had been chosen then.
                    article.media_hash = media_hash(session, revision)
                    session.commit()
                attached += 1
                logger.info("Attached %s: %s", entry.key, article.title)
            if attached:
                result = make_publisher(session)(settings=settings, paths=paths)
                logger.info("Rebuilt the site with %d article(s)", result["articles"])
    finally:
        session.close()
    if attached:
        _render_status(paths)
    logger.info("Attached %d picture(s)", attached)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
