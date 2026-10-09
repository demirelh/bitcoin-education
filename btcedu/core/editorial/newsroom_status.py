"""Read-only view of what the ALMANYA24 newsroom made of a broadcast episode.

The dashboard shows it as the last pipeline stage. Both newsroom databases are
opened read-only; the daily run may be writing to them at the same time.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "ALMANYA24_PREVIEW_DATA_DIR"
SITE_BASE_URL = "https://sahimi.app/almanya24-dev"


def data_dir() -> Path:
    """Same location the daily run writes to (``scripts/almanya24_preview/paths``)."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "almanya24-preview"


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)


@lru_cache(maxsize=4)
def _load(root: str, news_mtime: float, processed_mtime: float) -> dict[str, dict]:
    """Everything per episode, read once per change of either database."""
    by_episode: dict[str, dict] = {}

    def entry(episode_id: str) -> dict:
        return by_episode.setdefault(
            episode_id, {"articles": [], "stories": {}, "failures": [], "built_at": None}
        )

    news = Path(root) / "current-news.sqlite"
    with _connect(news) as db:
        built = db.execute(
            "SELECT MAX(switched_at) FROM news_site_releases WHERE status = 'live'"
        ).fetchone()[0]
        rows = db.execute(
            """
            SELECT si.episode_id, p.slug, p.section, a.title, p.first_published_at
            FROM news_source_items si
            JOIN news_source_revisions sr ON sr.source_item_id = si.id
            JOIN news_topic_sources ts ON ts.source_revision_id = sr.id
            JOIN news_publications p ON p.topic_id = ts.topic_id
            JOIN news_article_revisions a ON a.id = p.current_article_revision_id
            WHERE p.status IN ('published', 'corrected')
            GROUP BY si.episode_id, p.id
            ORDER BY p.first_published_at
            """
        ).fetchall()
    for episode_id, slug, section, title, published_at in rows:
        item = entry(episode_id)
        item["built_at"] = _iso(built)
        item["articles"].append(
            {
                "title": title,
                "url": f"{SITE_BASE_URL}/{section}/{slug}/",
                "published_at": _iso(published_at),
                "built": bool(built and published_at and built >= published_at),
            }
        )

    processed = Path(root) / "daily-processed.sqlite"
    if processed.exists():
        with _connect(processed) as db:
            for episode_id, key, status, detail in db.execute(
                "SELECT episode_id, story_key, status, detail FROM processed_stories"
            ):
                item = entry(episode_id)
                item["stories"][status] = item["stories"].get(status, 0) + 1
                if status == "failed":
                    item["failures"].append({"story": key, "detail": (detail or "")[:300]})
    return by_episode


def episode_newsroom_status(episode_id: str, root: Path | None = None) -> dict | None:
    """Articles, story outcomes and the derived stage state, or None without data."""
    root = root or data_dir()
    news = root / "current-news.sqlite"
    processed = root / "daily-processed.sqlite"
    if not news.exists():
        return None
    try:
        snapshot = _load(
            str(root),
            news.stat().st_mtime,
            processed.stat().st_mtime if processed.exists() else 0.0,
        )
    except sqlite3.Error as exc:
        logger.warning("Newsroom status unavailable: %s", exc)
        return None
    item = snapshot.get(episode_id) or {
        "articles": [],
        "stories": {},
        "failures": [],
        "built_at": None,
    }
    articles, stories = item["articles"], item["stories"]
    if any(article["built"] for article in articles):
        state = "done"
    elif articles or stories.get("drafted"):
        state = "active"
    elif stories.get("needs_decision"):
        state = "paused"
    elif stories.get("failed"):
        state = "failed"
    else:
        state = "pending"
    return {**item, "state": state, "site_url": f"{SITE_BASE_URL}/"}
