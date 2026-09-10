"""Build the public site as a static release directory (N5).

A release is built somewhere nobody is reading, then the ``current`` pointer is
moved in one atomic step. That is the whole design: a reader either sees the
previous release entirely or the new one entirely, never a directory that is
halfway rewritten. A crash therefore leaves a stray build directory and a
pointer that is still correct, which is the state ``reconcile_releases`` cleans
up.

No server process is required to serve the result, and the builder reads only
the public dataclasses — the private database is never reachable from a page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy.orm import Session

from btcedu.core.editorial.media import approved_revision_media
from btcedu.core.editorial.public import (
    PublicArticle,
    PublicationBlocked,
    build_public_article,
    media_file_name,
    publishable,
    withdrawn,
)
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_rights import MediaUseDecision, NewsroomMediaAsset
from btcedu.models.publication import (
    CorrectionNotice,
    Publication,
    ReleaseStatus,
    SiteRelease,
)

logger = logging.getLogger(__name__)

CURRENT = "current"
BUILD_PREFIX = "build-"
SEARCH_INDEX_LIMIT = 1024 * 1024


class SiteBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class SiteConfig:
    """Operator-owned facts a public page is not allowed to invent."""

    site_name: str = "ALMANYA24"
    base_url: str = "https://example.invalid"
    imprint: str = ""
    privacy: str = ""
    contact: str = ""
    usage_rights: str = ""

    @property
    def base_path(self) -> str:
        path = urlsplit(self.base_url).path.rstrip("/")
        return "" if path == "/" else path

    @classmethod
    def from_settings(cls, settings) -> SiteConfig:
        return cls(
            site_name=settings.newsroom_site_name,
            base_url=settings.newsroom_site_base_url,
            imprint=settings.newsroom_site_imprint,
            privacy=settings.newsroom_site_privacy,
            contact=settings.newsroom_site_contact,
            usage_rights=settings.newsroom_site_usage_rights,
        )


@dataclass
class BuildResult:
    release_id: str
    directory: Path
    article_count: int
    withdrawn_count: int
    content_hash: str
    skipped: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _e(value: str) -> str:
    return escape(str(value))


def _site_path(config: SiteConfig, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("Site paths must start with '/'")
    return f"{config.base_path}{path}"


def _page(config: SiteConfig, title: str, body: str, *, head: str = "") -> str:
    home = _site_path(config, "/")
    search = _site_path(config, "/arama/")
    stylesheet = _site_path(config, "/assets/site.css")
    imprint = _site_path(config, "/kunye/")
    privacy = _site_path(config, "/gizlilik/")
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="tr">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n"
        f'<link rel="stylesheet" href={quoteattr(stylesheet)}>\n'
        f"{head}</head>\n<body>\n"
        f'<header class="site"><a href={quoteattr(home)}>{_e(config.site_name)}</a>'
        f'<form class="search" action={quoteattr(search)} method="get">'
        '<input type="search" name="q" aria-label="Ara"></form></header>\n'
        f"<main>\n{body}\n</main>\n"
        '<footer class="site">'
        f'<a href={quoteattr(imprint)}>Künye</a> · '
        f'<a href={quoteattr(privacy)}>Gizlilik</a>'
        "</footer>\n</body>\n</html>\n"
    )


def _article_html(config: SiteConfig, article: PublicArticle) -> str:
    parts = [f"<article>\n<h1>{_e(article.title)}</h1>"]
    parts.append(
        f'<p class="lede">{_e(article.lede)}</p>'
        f'<p class="meta"><time datetime={quoteattr(article.published_on)}>'
        f"{_e(article.published_on)}</time></p>"
    )
    for correction in article.corrections:
        parts.append(
            f'<aside class="correction"><strong>Düzeltme</strong> '
            f"({_e(correction.published_on)}): {_e(correction.summary)}</aside>"
        )
    for item in article.media:
        notice = {"archive": "ARŞİV", "symbolic": "SEMBOL GÖRSEL", "portrait": "PORTRE"}.get(
            item.role, ""
        )
        media_url = _site_path(config, f"/media/{item.file_name}")
        parts.append(
            f'<figure><img src={quoteattr(media_url)} alt={quoteattr(item.caption)}'
            + (f' width="{item.width}"' if item.width else "")
            + (f' height="{item.height}"' if item.height else "")
            + ' loading="lazy">'
            f"<figcaption>{_e(notice)} {_e(item.caption)} "
            f'<span class="credit">{_e(item.attribution)} · {_e(item.license)}</span>'
            "</figcaption></figure>"
        )
    for paragraph in article.paragraphs:
        refs = "".join(
            f'<sup><a href="#kaynak-{index + 1}">{index + 1}</a></sup>'
            for index in paragraph.sources
        )
        parts.append(f"<p>{_e(paragraph.text)}{refs}</p>")
    if article.related:
        parts.append("<nav aria-label=\"İlgili haberler\"><h2>İlgili haberler</h2>")
        for related in article.related:
            parts.append(
                f'<p><a href={quoteattr(related["url"])}>{_e(related["title"])}</a></p>'
            )
        parts.append("</nav>")
    if article.sources:
        parts.append('<section class="sources"><h2>Kaynaklar</h2><ol>')
        for index, source in enumerate(article.sources, start=1):
            label = f"{source.title} — {source.publisher}" if source.publisher else source.title
            parts.append(
                f'<li id="kaynak-{index}"><a href={quoteattr(source.url)} '
                f'rel="nofollow noopener">{_e(label)}</a> '
                f'<span class="retrieved">({_e(source.retrieved_on)})</span></li>'
            )
        parts.append("</ol></section>")
    parts.append("</article>")

    json_ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": article.title,
            "description": article.lede,
            "datePublished": article.published_on,
            "dateModified": article.updated_on,
            "inLanguage": article.language,
            "mainEntityOfPage": article.canonical_url,
            "publisher": {"@type": "Organization", "name": config.site_name},
        },
        ensure_ascii=False,
    )
    json_ld = json_ld.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    head = (
        f"<link rel=\"canonical\" href={quoteattr(article.canonical_url)}>\n"
        f'<script type="application/ld+json">{json_ld}</script>\n'
    )
    return _page(config, f"{article.title} — {config.site_name}", "\n".join(parts), head=head)


def _index_html(config: SiteConfig, articles: list[PublicArticle], *, title: str) -> str:
    items = [f"<h1>{_e(title)}</h1>", '<ul class="teasers">']
    for article in articles:
        article_url = _site_path(config, f"/{article.section}/{article.slug}/")
        items.append(
            f'<li><a href={quoteattr(article_url)}>'
            f"<h2>{_e(article.title)}</h2><p>{_e(article.lede)}</p></a>"
            f'<time datetime={quoteattr(article.published_on)}>'
            f"{_e(article.published_on)}</time></li>"
        )
    items.append("</ul>")
    return _page(config, f"{title} — {config.site_name}", "\n".join(items))


def _tombstone_html(config: SiteConfig, publication: Publication, summary: str) -> str:
    body = (
        "<article><h1>Bu içerik geri çekildi</h1>"
        f"<p>{_e(summary)}</p>"
        f'<p class="meta">{_e(publication.updated_at.date().isoformat())}</p></article>'
    )
    return _page(config, f"Geri çekildi — {config.site_name}", body)


def _sitemap(articles: list[PublicArticle]) -> str:
    urls = "".join(
        f"<url><loc>{_e(article.canonical_url)}</loc>"
        f"<lastmod>{_e(article.updated_on)}</lastmod></url>"
        for article in articles
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>\n"
    )


def _feed(config: SiteConfig, articles: list[PublicArticle]) -> str:
    items = "".join(
        f"<item><title>{_e(article.title)}</title>"
        f"<link>{_e(article.canonical_url)}</link>"
        f"<guid isPermaLink=\"true\">{_e(article.canonical_url)}</guid>"
        f"<description>{_e(article.lede)}</description></item>"
        for article in articles
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{_e(config.site_name)}</title>"
        f"<link>{_e(config.base_url)}</link>"
        f"<description>{_e(config.site_name)}</description>"
        f"{items}</channel></rss>\n"
    )


def _search_index(articles: list[PublicArticle]) -> str:
    """A public-only index, capped so the site stays usable on a phone."""
    documents = [article.search_document() for article in articles]
    payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
    while len(payload.encode("utf-8")) > SEARCH_INDEX_LIMIT and documents:
        for document in documents:
            document["text"] = document["text"][: max(200, len(document["text"]) // 2)]
        payload = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))
        if all(len(document["text"]) <= 200 for document in documents):
            break
    if len(payload.encode("utf-8")) > SEARCH_INDEX_LIMIT:
        raise SiteBuildError("Public search index exceeds the 1 MiB limit")
    return payload


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _copy_media(session: Session, article_row: ArticleRevision, target: Path | None) -> None:
    revision = session.get(EditorialRevision, article_row.editorial_revision_id)
    for row in approved_revision_media(session, revision):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        if asset is None:
            continue
        source = Path(asset.blob_path)
        if not source.is_file():
            raise SiteBuildError(f"Approved media blob is missing: {asset.asset_id}")
        if source.is_symlink():
            raise SiteBuildError("Public media must not be a symlink")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != asset.content_hash:
            raise SiteBuildError(f"Approved media bytes changed: {asset.asset_id}")
        if target is None:
            continue
        destination = target / "media" / media_file_name(asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def build_site(
    session: Session,
    *,
    root: Path,
    config: SiteConfig,
    operator_ref: str = "",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BuildResult:
    """Render every publishable article into a fresh, unreferenced directory."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    release_id = str(uuid.uuid4())
    target = root / f"{BUILD_PREFIX}{release_id}"
    target.mkdir(parents=True)

    record = SiteRelease(
        release_id=release_id,
        directory=str(target),
        status=ReleaseStatus.BUILDING.value,
        operator_ref=operator_ref,
        created_at=now(),
    )
    session.add(record)
    session.commit()

    try:
        articles: list[PublicArticle] = []
        skipped: list[tuple[str, tuple[str, ...]]] = []
        for publication in publishable(session):
            try:
                article = build_public_article(
                    session, publication, base_url=config.base_url
                )
            except PublicationBlocked as exc:
                # A build never guesses. An article whose evidence or rights no
                # longer hold is left out entirely rather than shipped stale.
                skipped.append((publication.slug, exc.reasons))
                logger.warning(
                    "Skipping %s in public build: %s", publication.slug, exc
                )
                continue
            articles.append(article)
            _write(
                target / article.section / article.slug / "index.html",
                _article_html(config, article),
            )
            row = session.get(ArticleRevision, publication.current_article_revision_id)
            _copy_media(session, row, target)

        for article in articles:
            related = tuple(
                {
                    "url": _site_path(config, f"/{other.section}/{other.slug}/"),
                    "title": other.title,
                }
                for other in articles
                if other.section == article.section and other.slug != article.slug
            )[:3]
            _write(
                target / article.section / article.slug / "index.html",
                _article_html(config, replace(article, related=related)),
            )
        _write(target / "index.html", _index_html(config, articles, title="Son haberler"))
        sections = sorted({article.section for article in articles})
        for section in sections:
            _write(
                target / section / "index.html",
                _index_html(
                    config,
                    [article for article in articles if article.section == section],
                    title=section,
                ),
            )
        _write(target / "sitemap.xml", _sitemap(articles))
        _write(target / "feed.xml", _feed(config, articles))
        _write(target / "arama" / "index.json", _search_index(articles))
        _write(
            target / "arama" / "index.html",
            _page(
                config, "Ara",
                '<h1>Ara</h1><p id="search-status" role="status"></p>'
                '<ul id="search-results"></ul>'
                f'<script src={quoteattr(_site_path(config, "/assets/search.js"))} '
                "defer></script>",
            ),
        )
        _write(target / "assets" / "search.js", _search_js(config))
        _write(
            target / "publication-state.json",
            json.dumps(_publication_state(session, config), ensure_ascii=False, sort_keys=True),
        )
        _write(
            target / "kunye" / "index.html",
            _page(
                config,
                f"Künye — {config.site_name}",
                f"<article><h1>Künye</h1><p>{_e(config.imprint)}</p>"
                f"<p>{_e(config.contact)}</p><p>{_e(config.usage_rights)}</p></article>",
            ),
        )
        _write(
            target / "gizlilik" / "index.html",
            _page(
                config,
                f"Gizlilik — {config.site_name}",
                f"<article><h1>Gizlilik</h1><p>{_e(config.privacy)}</p></article>",
            ),
        )
        _write(target / "assets" / "site.css", _CSS)

        gone: list[str] = []
        for publication in withdrawn(session):
            if any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", component)
                   for component in (publication.section, publication.slug)):
                raise SiteBuildError("Unsafe withdrawn publication path")
            notice = (
                session.query(CorrectionNotice)
                .filter_by(publication_id=publication.id, kind="withdrawal")
                .order_by(CorrectionNotice.id.desc())
                .first()
            )
            summary = notice.summary if notice else "Bu içerik yayından kaldırıldı."
            _write(
                target / publication.section / publication.slug / "index.html",
                _tombstone_html(config, publication, summary),
            )
            gone.append(
                _site_path(config, f"/{publication.section}/{publication.slug}/")
            )
        _write(target / "gone.json", json.dumps(sorted(gone), ensure_ascii=False))

        content_hash = _tree_hash(target)
        record.status = ReleaseStatus.BUILT.value
        record.content_hash = content_hash
        record.article_count = len(articles)
        session.commit()
        return BuildResult(
            release_id=release_id,
            directory=target,
            article_count=len(articles),
            withdrawn_count=len(gone),
            content_hash=content_hash,
            skipped=tuple(skipped),
        )
    except Exception as exc:
        record.status = ReleaseStatus.FAILED.value
        record.error_message = str(exc)
        session.commit()
        raise


def _tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def switch_release(session: Session, *, root: Path, release_id: str) -> Path:
    """Point ``current`` at a finished build in one step.

    ``os.replace`` on a symlink is atomic, so no request can observe a moment
    where the pointer is missing. Nothing is deleted here: the previous release
    stays on disk and remains the rollback target.
    """
    root = Path(root).resolve()
    record = (
        session.query(SiteRelease).filter_by(release_id=release_id).one_or_none()
    )
    if record is None:
        raise SiteBuildError(f"Unknown release {release_id}")
    if record.status not in (ReleaseStatus.BUILT.value, ReleaseStatus.LIVE.value):
        raise SiteBuildError(f"Release {release_id} is {record.status}, not built")
    directory = Path(record.directory).resolve()
    if directory.parent != root or directory.name != f"{BUILD_PREFIX}{release_id}":
        raise SiteBuildError("Release directory is outside its declared build root")
    if not directory.is_dir():
        raise SiteBuildError(f"Release directory is gone: {directory}")
    if _tree_hash(directory) != record.content_hash:
        raise SiteBuildError("Release bytes changed after building")
    snapshot = json.loads((directory / "publication-state.json").read_text(encoding="utf-8"))
    config = SiteConfig(base_url=snapshot["base_url"])
    if snapshot != _publication_state(session, config):
        raise SiteBuildError("Publication state changed since building; build a fresh release")
    for publication in publishable(session):
        from btcedu.core.editorial.public import export_blockers

        article = session.get(ArticleRevision, publication.current_article_revision_id)
        if not export_blockers(session, article):
            _copy_media(session, article, None)

    pointer = root / CURRENT
    temporary = Path(tempfile.mkdtemp(dir=root, prefix=".pointer-")) / CURRENT
    temporary.symlink_to(directory, target_is_directory=True)
    os.replace(temporary, pointer)
    shutil.rmtree(temporary.parent, ignore_errors=True)

    for other in session.query(SiteRelease).filter_by(status=ReleaseStatus.LIVE.value):
        if other.release_id != release_id:
            other.status = ReleaseStatus.SUPERSEDED.value
    record.status = ReleaseStatus.LIVE.value
    record.switched_at = datetime.now(UTC)
    session.commit()
    return pointer


def live_release(session: Session) -> SiteRelease | None:
    return (
        session.query(SiteRelease)
        .filter_by(status=ReleaseStatus.LIVE.value)
        .order_by(SiteRelease.id.desc())
        .first()
    )


def reconcile_releases(session: Session, *, root: Path) -> tuple[str, ...]:
    """Clean up after a crash without ever touching what is being served.

    A build that never reached its pointer is unreferenced by definition, so it
    can be removed. The live release is identified by the pointer on disk, not
    by the database, because the pointer is what readers actually follow.
    """
    root = Path(root).resolve()
    pointer = root / CURRENT
    served = pointer.resolve() if pointer.exists() else None
    removed: list[str] = []

    for record in session.query(SiteRelease).all():
        directory = Path(record.directory)
        if (
            directory.is_symlink() or directory.resolve().parent != root
            or directory.name != f"{BUILD_PREFIX}{record.release_id}"
        ):
            raise SiteBuildError("Refusing to reconcile a release outside its build root")
        if served is not None and directory.resolve() == served:
            if record.status != ReleaseStatus.LIVE.value:
                record.status = ReleaseStatus.LIVE.value
            continue
        if record.status in (ReleaseStatus.BUILDING.value, ReleaseStatus.FAILED.value):
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
            removed.append(record.release_id)
            record.status = ReleaseStatus.FAILED.value
        elif record.status == ReleaseStatus.LIVE.value:
            record.status = ReleaseStatus.SUPERSEDED.value
    session.commit()
    return tuple(removed)


def _publication_state(session: Session, config: SiteConfig) -> dict:
    """Public-only snapshot used to reject stale release activation."""
    current = []
    for publication in publishable(session):
        try:
            current.append(build_public_article(
                session, publication, base_url=config.base_url
            ).to_dict())
        except PublicationBlocked:
            continue
    return {
        "base_url": config.base_url,
        "articles": json.loads(json.dumps(current)),
        "withdrawn": [
            [row.section, row.slug, str(row.updated_at)] for row in withdrawn(session)
        ],
    }


def _search_js(config: SiteConfig) -> str:
    base_path = json.dumps(config.base_path)
    return f"""
"use strict";
const basePath = {base_path};
const query = (new URLSearchParams(location.search).get("q") || "")
  .toLocaleLowerCase("tr").trim();
const status = document.getElementById("search-status");
const results = document.getElementById("search-results");
fetch(basePath + "/arama/index.json").then(response => {{
  if (!response.ok) throw new Error("Search unavailable");
  return response.json();
}}).then(rows => {{
  const matches = query ? rows.filter(row =>
    (row.title + " " + row.text).toLocaleLowerCase("tr").includes(query)) : [];
  status.textContent = String(matches.length) + " haber";
  for (const row of matches) {{
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = basePath + "/" + encodeURIComponent(row.section) + "/" +
      encodeURIComponent(row.slug) + "/";
    a.textContent = row.title;
    li.append(a);
    results.append(li);
  }}
}}).catch(() => {{ status.textContent = "Arama şu anda kullanılamıyor."; }});
"""


_CSS = """:root { color-scheme: light dark; --w: 40rem; }
* { box-sizing: border-box; }
body { margin: 0; font: 17px/1.6 system-ui, sans-serif; }
header.site, footer.site { display: flex; gap: 1rem; align-items: center;
  padding: .75rem 1rem; border-bottom: 1px solid #8884; }
footer.site { border: 0; border-top: 1px solid #8884; }
main { max-width: var(--w); margin: 0 auto; padding: 1rem; }
img { max-width: 100%; height: auto; }
figure { margin: 1rem 0; }
figcaption { font-size: .85rem; opacity: .8; }
.credit { display: block; font-size: .78rem; opacity: .75; }
.lede { font-size: 1.15rem; font-weight: 600; }
.correction { border-left: 4px solid #c33; padding: .5rem .75rem; margin: 1rem 0; }
.teasers { list-style: none; padding: 0; }
.teasers li { border-bottom: 1px solid #8884; padding: .75rem 0; }
.teasers a { text-decoration: none; color: inherit; }
.sources { font-size: .9rem; }
@media (max-width: 30rem) { body { font-size: 16px; } }
"""
