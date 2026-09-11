"""Render the protected review view for the stored, unapproved article draft.

The article revision exists but is deliberately not approved, so the public
site correctly refuses to publish it. The draft, the picture with its licence
and credit, the checked claims with their verdicts and the real source passages
are what a human needs in order to judge it, so they are rendered into the
access-protected preview and marked as unapproved and unpublished.
"""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

if __package__:
    from .paths import data_root
else:  # executed directly from the command line
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root

ROOT = data_root()
DB = ROOT / "current-news.sqlite"
OUT = ROOT / "site" / "current" / "_inceleme" / "index.html"
STORY = "bundesweiter-warntag"


def _drafts(connection):
    rows = connection.execute(
        """SELECT usage_json, completed_at FROM news_provider_operations
           WHERE operation_key LIKE 'draft_article:%' AND status='completed'
           ORDER BY rowid"""
    ).fetchall()
    return [(json.loads(payload)["result"], stamp) for payload, stamp in rows]


def _rejections(connection):
    return connection.execute(
        """SELECT operation_key, error_message, completed_at
           FROM news_provider_operations
           WHERE operation_type='article' AND error_message IS NOT NULL
           ORDER BY rowid"""
    ).fetchall()


def _claims(connection):
    return connection.execute(
        """SELECT cr.id, cr.statement, cr.attribution, cr.numeric_value,
                  (SELECT verdict FROM news_claim_assessments a
                    WHERE a.claim_revision_id = cr.id ORDER BY a.rowid DESC LIMIT 1)
             FROM news_claim_revisions cr ORDER BY cr.rowid DESC LIMIT 5"""
    ).fetchall()


def _evidence(connection, claim_id):
    return connection.execute(
        """SELECT o.publisher, o.canonical_url, l.passage, l.provenance_family
             FROM news_evidence_links l
             JOIN news_source_observations o ON o.id = l.source_observation_id
            WHERE l.claim_revision_id = ? AND l.relation = 'supports'""",
        (claim_id,),
    ).fetchall()


def _paragraphs(draft):
    parts = [f"<h4>{html.escape(draft.get('title') or '')}</h4>"]
    parts.append(f"<p class='lede'>{html.escape(draft.get('lede') or '')}</p>")
    for paragraph in draft.get("paragraphs", []):
        parts.append(f"<p>{html.escape(paragraph.get('text') or '')}</p>")
    return "\n".join(parts)


def _article(connection):
    row = connection.execute(
        """SELECT id, article_revision_id, title, lede, status, content_hash
             FROM news_article_revisions ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return None, []
    paragraphs = connection.execute(
        """SELECT p.position, p.text,
                  (SELECT group_concat(cr.statement, ' || ')
                     FROM news_article_paragraph_claims pc
                     JOIN news_claim_revisions cr ON cr.id = pc.claim_revision_id
                    WHERE pc.article_paragraph_id = p.id)
             FROM news_article_paragraphs p
            WHERE p.article_revision_id = ?
            ORDER BY p.position""",
        (row[0],),
    ).fetchall()
    return row, paragraphs


def _picture(connection, article_row_id):
    return connection.execute(
        """SELECT rm.caption, d.attribution_text, d.role, d.status,
                  o.page_url, o.title, a.width, a.height, a.mime_type
             FROM news_article_revisions ar
             JOIN news_revision_media rm
               ON rm.editorial_revision_id = ar.editorial_revision_id
             JOIN news_media_use_decisions d ON d.id = rm.media_use_decision_id
             JOIN news_media_source_offers o ON o.id = d.media_source_offer_id
             JOIN news_media_assets a ON a.id = d.media_asset_id
            WHERE ar.id = ? AND d.status = 'approved' AND d.revoked_at IS NULL""",
        (article_row_id,),
    ).fetchone()


def build() -> Path:
    connection = sqlite3.connect(DB)
    drafts = _drafts(connection)
    rejections = _rejections(connection)
    article, paragraphs = _article(connection)
    picture = _picture(connection, article[0]) if article else None

    blocks = [
        "<!doctype html><html lang='tr'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<meta name='robots' content='noindex, nofollow'>",
        "<title>ALMANYA24 — inceleme (yayımlanmadı)</title>",
        "<link rel='stylesheet' href='../assets/site.css'>",
        "<style>body{max-width:52rem;margin:0 auto;padding:1.5rem;}"
        ".warn{border:2px solid #b00;background:#fff4f4;padding:1rem;margin:1rem 0;}"
        ".draft{border:1px solid #ccc;padding:1rem;margin:1rem 0;}"
        ".reject{background:#fff8e6;border-left:4px solid #d68000;padding:.75rem;}"
        "blockquote{color:#444;border-left:3px solid #ddd;margin:.4rem 0;padding-left:.7rem;}"
        "</style></head><body>",
        "<h1>ALMANYA24 — dahili inceleme</h1>",
        "<div class='warn'><strong>Yayımlanmadı. Onaylanmadı.</strong><br>"
        "Bu sayfa yalnızca incelemek içindir. Makale taslağı veritabanına kaydedilmiştir, "
        "ancak redaksiyon onayı verilmemiştir; bu nedenle kamuya açık sitede yer almaz. "
        "Metin, kaynaklar, iddia değerlendirmeleri ve görsel gerçektir.</div>",
        f"<p>Konu: <code>{html.escape(STORY)}</code></p>",
    ]

    if article is not None:
        blocks.append("<h2>Makale taslağı</h2>")
        blocks.append("<div class='draft'>")
        blocks.append(f"<h3>{html.escape(article[2])}</h3>")
        blocks.append(
            f"<p>Durum: <code>{html.escape(article[4])}</code> · "
            f"Sürüm: <code>{html.escape(article[1])}</code></p>"
        )
        if picture:
            caption, credit, role, _status, page_url, ptitle, width, height, mime = picture
            label = "Arşiv görseli" if role == "archive" else "Sembolik görsel"
            blocks.append("<figure>")
            blocks.append(
                f"<figcaption><strong>{html.escape(label)}</strong> — "
                f"{html.escape(caption or ptitle or '')}<br>"
                f"Görsel kaynağı: <a href='{html.escape(page_url)}' rel='noopener nofollow'>"
                f"{html.escape(ptitle or page_url)}</a><br>"
                f"Kredi/lisans: {html.escape(credit or 'belirtilmemiş')}<br>"
                f"<small>{width}×{height} · {html.escape(mime)}</small></figcaption>"
            )
            blocks.append("</figure>")
        else:
            blocks.append(
                "<p><em>Uygun ve lisansı denetlenmiş görsel bulunamadı; "
                "alakasız görsel yerine görselsiz gösterilir.</em></p>"
            )
        blocks.append(f"<p class='lede'>{html.escape(article[3])}</p>")
        for _position, text, claim_statements in paragraphs:
            blocks.append(f"<p>{html.escape(text)}</p>")
            if claim_statements:
                blocks.append(
                    "<p><small>Dayandığı iddialar: "
                    + html.escape(claim_statements.replace(" || ", " · "))
                    + "</small></p>"
                )
        blocks.append("</div>")

    blocks.append("<h2>Reddedilen ara taslaklar</h2>")

    for index, (draft, stamp) in enumerate(drafts, start=1):
        blocks.append("<div class='draft'>")
        blocks.append(f"<h3>Taslak {index} <small>({html.escape(str(stamp))})</small></h3>")
        blocks.append(_paragraphs(draft))
        blocks.append("</div>")

    blocks.append("<h2>Denetim kararları</h2>")
    for key, message, stamp in rejections:
        blocks.append(
            f"<p class='reject'><code>{html.escape(key.split(':')[0])}</code> — "
            f"{html.escape(message)} <small>{html.escape(str(stamp))}</small></p>"
        )

    blocks.append("<h2>İddialar ve kaynaklar</h2>")
    for claim_id, statement, attribution, number, verdict in _claims(connection):
        blocks.append("<div class='draft'>")
        blocks.append(f"<p><strong>{html.escape(statement)}</strong></p>")
        blocks.append(
            f"<p>Değerlendirme: <code>{html.escape(str(verdict))}</code>"
            + (f" · Kaynak kişi: {html.escape(attribution)}" if attribution else "")
            + (f" · Sayı: {html.escape(number)}" if number else "")
            + "</p>"
        )
        rows = _evidence(connection, claim_id)
        if not rows:
            blocks.append("<p><em>Bu iddia için kayıtlı destekleyici pasaj yok.</em></p>")
        for publisher, url, passage, family in rows:
            blocks.append(
                f"<p><a href='{html.escape(url)}' rel='noopener nofollow'>"
                f"{html.escape(publisher or url)}</a> "
                f"<small>({html.escape(family or 'bilinmiyor')})</small></p>"
                f"<blockquote>{html.escape((passage or '')[:400])}</blockquote>"
            )
        blocks.append("</div>")

    blocks.append("</body></html>")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(blocks), encoding="utf-8")
    connection.close()
    return OUT


if __name__ == "__main__":
    print(json.dumps({"review_page": str(build())}))
