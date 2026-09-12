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
    tagline: str = "Almanya, Türkiye ve dünyadan doğrulanmış haberler"
    preview_notice: str = ""
    imprint: str = ""
    privacy: str = ""
    contact: str = ""
    usage_rights: str = ""
    #: Development only; see ``Settings.newsroom_dev_auto_release``.
    dev_auto_release: bool = False

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
            dev_auto_release=bool(getattr(settings, "newsroom_dev_auto_release", False)),
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


def _category_label(section: str) -> str:
    return {
        "almanya": "Almanya",
        "turkiye": "Türkiye",
        "dunya": "Dünya",
        "ekonomi": "Ekonomi",
        "yasam": "Yaşam",
        "kultur": "Kültür",
        "spor": "Spor",
        "haber": "Gündem",
    }.get(section, section.replace("-", " ").title())


def _media_markup(
    config: SiteConfig,
    article: PublicArticle,
    *,
    class_name: str,
) -> str:
    if not article.media:
        return (
            f'<div class="{class_name} story-image-placeholder" aria-hidden="true">'
            f"<span>{_e(_category_label(article.section))}</span></div>"
        )
    item = article.media[0]
    notice = {"archive": "ARŞİV", "symbolic": "SEMBOL GÖRSEL", "portrait": "PORTRE"}.get(
        item.role, ""
    )
    media_url = _site_path(config, f"/media/{item.file_name}")
    return (
        f'<figure class="{class_name}"><img src={quoteattr(media_url)} '
        f'alt={quoteattr(item.caption)}'
        + (f' width="{item.width}"' if item.width else "")
        + (f' height="{item.height}"' if item.height else "")
        + ' loading="lazy">'
        f"<figcaption>{_e(notice)} {_e(item.caption)} "
        f'<span class="credit">{_e(item.attribution)} · {_e(item.license)}</span>'
        "</figcaption></figure>"
    )


def _page(
    config: SiteConfig,
    title: str,
    body: str,
    *,
    head: str = "",
    sections: tuple[str, ...] = (),
) -> str:
    home = _site_path(config, "/")
    search = _site_path(config, "/arama/")
    stylesheet = _site_path(config, "/assets/site.css")
    imprint = _site_path(config, "/kunye/")
    privacy = _site_path(config, "/gizlilik/")
    navigation = "".join(
        f'<a href={quoteattr(_site_path(config, f"/{section}/"))}>'
        f"{_e(_category_label(section))}</a>"
        for section in sections
    )
    notice = (
        f'<aside class="preview-notice">{_e(config.preview_notice)}'
        + (
            f' · <a href={quoteattr(_site_path(config, "/_durum/"))}>İşletim durumu</a>'
            if config.dev_auto_release
            else ""
        )
        + "</aside>\n"
        if config.preview_notice
        else ""
    )
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="tr">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n"
        f'<link rel="stylesheet" href={quoteattr(stylesheet)}>\n'
        f"{head}</head>\n<body>\n"
        '<header class="site"><div class="masthead">'
        f'<a class="brand" href={quoteattr(home)}>{_e(config.site_name)}</a>'
        f'<p>{_e(config.tagline)}</p></div>'
        f'<form class="search" action={quoteattr(search)} method="get">'
        '<input type="search" name="q" aria-label="Haber ara" placeholder="Haber ara">'
        '<button type="submit">Ara</button></form>'
        f'<nav class="categories" aria-label="Haber kategorileri">{navigation}</nav>'
        "</header>\n"
        f"{notice}"
        f"<main>\n{body}\n</main>\n"
        '<footer class="site">'
        f'<a href={quoteattr(imprint)}>Künye</a> · '
        f'<a href={quoteattr(privacy)}>Gizlilik</a>'
        "</footer>\n</body>\n</html>\n"
    )


def _dev_release_banner(article: PublicArticle) -> str:
    """The notice an auto-released draft must carry on its own page.

    It names the unmet conditions rather than summarising them, because the
    point of the development switch is to look at the draft *and* at what is
    still wrong with it. Saying only "not approved" would let a reader assume
    the checks were fine and a signature was merely missing.
    """
    if not article.is_dev_auto_released:
        return ""
    items = "".join(f"<li>{_e(reason)}</li>" for reason in article.dev_auto_release_reasons)
    return (
        '<aside class="dev-release" role="note">'
        "<strong>Geliştirme sürümü — otomatik yayına alındı</strong>"
        "<p>Bu taslak insan onayı almadan geliştirme önizlemesinde gösteriliyor. "
        "Yayımlanmış bir haber değildir ve aşağıdaki koşullar sağlanmamıştır:</p>"
        f"<ul>{items}</ul>"
        "</aside>"
    )


def _article_html(
    config: SiteConfig,
    article: PublicArticle,
    *,
    sections: tuple[str, ...] = (),
) -> str:
    parts = [
        '<article class="article-page">',
        f'<p class="category">{_e(_category_label(article.section))}</p>',
        f"<h1>{_e(article.title)}</h1>",
    ]
    parts.append(_dev_release_banner(article))
    parts.append(
        f'<p class="lede">{_e(article.lede)}</p>'
        '<p class="meta">Yayın: '
        f'<time datetime={quoteattr(article.published_on)}>{_e(article.published_on)}</time>'
        + (
            " · Güncelleme: "
            f'<time datetime={quoteattr(article.updated_on)}>{_e(article.updated_on)}</time>'
            if article.updated_on != article.published_on
            else ""
        )
        + "</p>"
    )
    for correction in article.corrections:
        parts.append(
            f'<aside class="correction"><strong>Düzeltme</strong> '
            f"({_e(correction.published_on)}): {_e(correction.summary)}</aside>"
        )
    if article.media:
        parts.append(_media_markup(config, article, class_name="article-visual"))
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
            dates = (
                f"Yayın: {_e(source.published_on)} · "
                if source.published_on
                else ""
            )
            parts.append(
                f'<li id="kaynak-{index}"><a href={quoteattr(source.url)} '
                f'rel="nofollow noopener">{_e(label)}</a> '
                f'<span class="retrieved">({dates}Erişim: '
                f"{_e(source.retrieved_on)})</span></li>"
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
    return _page(
        config,
        f"{article.title} — {config.site_name}",
        "\n".join(parts),
        head=head,
        sections=sections,
    )


def _index_html(config: SiteConfig, articles: list[PublicArticle], *, title: str) -> str:
    if not articles:
        return _page(
            config,
            f"{title} — {config.site_name}",
            f'<section class="empty-news"><h1>{_e(title)}</h1>'
            "<p>Bu bölümde henüz yayıma hazır haber bulunmuyor.</p></section>",
        )

    sections = tuple(dict.fromkeys(article.section for article in articles))

    def story(article: PublicArticle, class_name: str) -> str:
        article_url = _site_path(config, f"/{article.section}/{article.slug}/")
        badge = (
            '<p class="dev-badge">Geliştirme sürümü — otomatik</p>'
            if article.is_dev_auto_released
            else ""
        )
        return (
            f'<article class="{class_name}">'
            f'<a class="story-link" href={quoteattr(article_url)}>'
            f'{_media_markup(config, article, class_name="story-image")}'
            '<div class="story-copy">'
            f'<p class="category">{_e(_category_label(article.section))}</p>'
            f"{badge}"
            f"<h2>{_e(article.title)}</h2><p>{_e(article.lede)}</p>"
            f'<time datetime={quoteattr(article.published_on)}>'
            f"Yayın: {_e(article.published_on)}</time></div></a></article>"
        )

    hero = story(articles[0], "lead-story")
    secondary = "".join(story(article, "secondary-story") for article in articles[1:3])
    cards = "".join(story(article, "news-card") for article in articles[3:])
    body = (
        f'<section class="page-heading"><p class="eyebrow">Güncel dosya</p>'
        f"<h1>{_e(title)}</h1></section>"
        f'<section class="lead-grid">{hero}<div class="secondary-grid">{secondary}</div></section>'
        + (f'<section class="news-grid">{cards}</section>' if cards else "")
    )
    return _page(
        config,
        f"{title} — {config.site_name}",
        body,
        sections=sections,
    )


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
                    session,
                    publication,
                    base_url=config.base_url,
                    dev_auto_release=config.dev_auto_release,
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

        sections = tuple(dict.fromkeys(article.section for article in articles))
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
                _article_html(
                    config,
                    replace(article, related=related),
                    sections=sections,
                ),
            )
        _write(target / "index.html", _index_html(config, articles, title="Son haberler"))
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
                sections=sections,
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
                sections=sections,
            ),
        )
        _write(
            target / "gizlilik" / "index.html",
            _page(
                config,
                f"Gizlilik — {config.site_name}",
                f"<article><h1>Gizlilik</h1><p>{_e(config.privacy)}</p></article>",
                sections=sections,
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


_CSS = """:root {
  color-scheme: light;
  --ink: #161616;
  --muted: #666;
  --line: #dedede;
  --paper: #fff;
  --soft: #f3f4f5;
  --brand: #c9001f;
  --brand-dark: #8f0016;
  --content: 1180px;
}
* { box-sizing: border-box; }
html { background: #e9eaec; }
body {
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.55 Arial, Helvetica, sans-serif;
  margin: 0 auto;
  min-height: 100vh;
}
a { color: inherit; }
img { display: block; height: auto; max-width: 100%; }
.site {
  background: #fff;
  border-bottom: 1px solid var(--line);
}
.masthead {
  align-items: end;
  display: flex;
  justify-content: space-between;
  margin: 0 auto;
  max-width: var(--content);
  padding: 1.45rem 1.25rem 1rem;
}
.brand {
  color: var(--brand);
  font-size: clamp(2rem, 5vw, 3.65rem);
  font-weight: 950;
  letter-spacing: -.065em;
  line-height: .9;
  text-decoration: none;
}
.masthead p {
  color: var(--muted);
  font-size: .82rem;
  margin: 0 0 .15rem;
  text-align: right;
}
.search {
  display: flex;
  margin: 0 auto;
  max-width: var(--content);
  padding: 0 1.25rem .9rem;
}
.search input {
  border: 1px solid var(--line);
  border-radius: .2rem 0 0 .2rem;
  min-height: 2.5rem;
  padding: .55rem .75rem;
  width: min(22rem, 100%);
}
.search button {
  background: var(--ink);
  border: 0;
  color: #fff;
  cursor: pointer;
  font-weight: 700;
  padding: .55rem 1rem;
}
.categories {
  background: var(--ink);
  display: flex;
  gap: 0;
  overflow-x: auto;
  padding: 0 max(1.25rem, calc((100vw - var(--content)) / 2 + 1.25rem));
  scrollbar-width: none;
}
.categories a {
  color: #fff;
  flex: 0 0 auto;
  font-size: .82rem;
  font-weight: 800;
  letter-spacing: .035em;
  padding: .72rem 1.1rem;
  text-decoration: none;
  text-transform: uppercase;
}
.categories a:hover { background: var(--brand); }
.dev-release {
  background: #fdecea;
  border: 2px solid #c0392b;
  border-radius: 4px;
  color: #7b241c;
  margin: 1rem 0 1.5rem;
  padding: .9rem 1.1rem;
}
.dev-release strong {
  display: block;
  font-size: .95rem;
  letter-spacing: .02em;
  margin-bottom: .4rem;
  text-transform: uppercase;
}
.dev-release p { margin: 0 0 .5rem; }
.dev-release ul { margin: 0; padding-left: 1.2rem; }
.dev-release li { font-size: .88rem; }
.dev-badge {
  background: #c0392b;
  color: #fff;
  display: inline-block;
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .04em;
  margin: 0 0 .35rem;
  padding: .15rem .45rem;
  text-transform: uppercase;
}
.preview-notice {
  background: #fff4d8;
  border-bottom: 1px solid #efd48e;
  color: #5e4300;
  font-size: .82rem;
  font-weight: 700;
  padding: .55rem 1.25rem;
  text-align: center;
}
main {
  margin: 0 auto;
  max-width: var(--content);
  min-height: 65vh;
  padding: 1.5rem 1.25rem 4rem;
}
.page-heading {
  align-items: baseline;
  border-bottom: 4px solid var(--ink);
  display: flex;
  justify-content: space-between;
  margin-bottom: 1rem;
}
.page-heading h1 {
  font-size: 1.55rem;
  letter-spacing: -.035em;
  margin: 0 0 .55rem;
}
.eyebrow, .category {
  color: var(--brand);
  font-size: .72rem !important;
  font-weight: 900 !important;
  letter-spacing: .08em;
  margin: 0 0 .4rem !important;
  text-transform: uppercase;
}
.lead-grid {
  border-bottom: 1px solid var(--line);
  display: grid;
  gap: 1.25rem;
  grid-template-columns: minmax(0, 1.8fr) minmax(17rem, .85fr);
  padding-bottom: 1.5rem;
}
.story-link { display: block; text-decoration: none; }
.story-link:hover h2 { color: var(--brand); }
.story-image {
  background: #dfe2e5;
  margin: 0 0 .85rem;
  overflow: hidden;
}
.story-image img {
  aspect-ratio: 16 / 9;
  object-fit: cover;
  width: 100%;
}
.story-image figcaption { display: none; }
.story-image-placeholder {
  align-items: end;
  aspect-ratio: 16 / 9;
  background:
    linear-gradient(135deg, rgb(201 0 31 / 92%), rgb(80 0 13 / 94%)),
    repeating-linear-gradient(45deg, #fff2 0 1px, transparent 1px 18px);
  color: #fff;
  display: flex;
  font-size: .75rem;
  font-weight: 900;
  letter-spacing: .12em;
  padding: 1rem;
  text-transform: uppercase;
}
.lead-story h2 {
  font-size: clamp(2rem, 4vw, 3.25rem);
  letter-spacing: -.055em;
  line-height: 1.02;
  margin: 0;
}
.story-copy > p:not(.category) {
  color: var(--muted);
  margin: .6rem 0;
}
.story-copy time {
  color: var(--muted);
  font-size: .76rem;
  font-weight: 700;
}
.secondary-grid { display: grid; gap: 1rem; }
.secondary-story + .secondary-story {
  border-top: 1px solid var(--line);
  padding-top: 1rem;
}
.secondary-story h2 {
  font-size: 1.25rem;
  letter-spacing: -.025em;
  line-height: 1.15;
  margin: 0;
}
.secondary-story .story-copy > p:not(.category) {
  display: -webkit-box;
  font-size: .88rem;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
.news-grid {
  display: grid;
  gap: 1.25rem;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  padding-top: 1.5rem;
}
.news-card {
  border-bottom: 3px solid var(--ink);
  padding-bottom: 1rem;
}
.news-card h2 {
  font-size: 1.2rem;
  letter-spacing: -.025em;
  line-height: 1.18;
  margin: 0;
}
.news-card .story-copy > p:not(.category) {
  display: -webkit-box;
  font-size: .86rem;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
}
.article-page {
  margin: 1rem auto;
  max-width: 760px;
}
.article-page h1 {
  font-size: clamp(2rem, 5vw, 3.5rem);
  letter-spacing: -.055em;
  line-height: 1.03;
  margin: 0;
}
.article-page > p {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.08rem;
}
.article-page .lede {
  color: #3f3f3f;
  font: 700 1.3rem/1.45 Arial, Helvetica, sans-serif;
}
.meta {
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font: 700 .78rem/1.5 Arial, Helvetica, sans-serif !important;
  padding-bottom: .75rem;
}
.article-visual { margin: 1.25rem 0; }
.article-visual img {
  aspect-ratio: 16 / 9;
  object-fit: cover;
  width: 100%;
}
figcaption {
  color: var(--muted);
  font-size: .76rem;
  line-height: 1.4;
  margin-top: .4rem;
}
.credit { display: block; font-size: .7rem; }
.correction {
  background: #fff1f2;
  border-left: 4px solid var(--brand);
  padding: .75rem 1rem;
}
.sources {
  border-top: 4px solid var(--ink);
  font-size: .88rem;
  margin-top: 2rem;
  padding-top: .75rem;
}
.sources a { color: var(--brand-dark); overflow-wrap: anywhere; }
.empty-news { padding: 4rem 0; text-align: center; }
footer.site {
  background: var(--ink);
  border: 0;
  color: #fff;
  gap: 1rem;
  justify-content: center;
  padding: 1.4rem;
}
footer.site a { font-size: .82rem; }
@media (max-width: 760px) {
  .masthead { align-items: flex-start; display: block; padding-top: 1rem; }
  .brand { font-size: 2.55rem; }
  .masthead p { margin-top: .45rem; text-align: left; }
  .search { padding-bottom: .7rem; }
  .search input { width: 100%; }
  .categories { padding: 0; }
  .categories a { padding: .7rem .85rem; }
  main { padding: 1rem .9rem 3.5rem; }
  .page-heading { display: block; }
  .eyebrow { margin-bottom: .1rem !important; }
  .lead-grid { display: block; }
  .secondary-grid {
    border-top: 1px solid var(--line);
    gap: .85rem;
    margin-top: 1rem;
    padding-top: 1rem;
  }
  .secondary-story .story-link {
    display: grid;
    gap: .75rem;
    grid-template-columns: 8.5rem minmax(0, 1fr);
  }
  .secondary-story .story-image { margin: 0; }
  .secondary-story h2 { font-size: 1.05rem; }
  .secondary-story .story-copy > p:not(.category) { display: none; }
  .news-grid { grid-template-columns: 1fr; }
  .news-card .story-link {
    display: grid;
    gap: .85rem;
    grid-template-columns: 8.5rem minmax(0, 1fr);
  }
  .news-card .story-image { margin: 0; }
  .news-card .story-copy > p:not(.category) { display: none; }
  .article-page h1 { font-size: 2.1rem; }
  .article-page > p { font-size: 1rem; }
}
@media (max-width: 420px) {
  .preview-notice { text-align: left; }
  .lead-story h2 { font-size: 1.85rem; }
  .secondary-story .story-link,
  .news-card .story-link { grid-template-columns: 7.25rem minmax(0, 1fr); }
}
"""
