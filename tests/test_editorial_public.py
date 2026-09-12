"""The public site: what leaves the building, and what cannot (N5).

The interesting questions here are negative ones. Does a private field reach a
page, does an article whose rights were revoked stay live, and what does a
reader see if the machine dies between writing a release and pointing at it.

Nothing is served and nothing is published: the build writes into a temporary
directory and the pointer is a symlink inside it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcedu.core.editorial.article import (
    approve_article_revision,
    article_preview,
    generate_article_revision,
)
from btcedu.core.editorial.public import (
    PublicationBlocked,
    build_public_article,
    export_blockers,
    publish_article,
    slugify,
    withdraw_publication,
)
from btcedu.core.editorial.site_export import (
    ReleaseStatus,
    SiteConfig,
    build_site,
    live_release,
    reconcile_releases,
    switch_release,
)
from btcedu.models.editorial import ClaimAssessment
from btcedu.models.publication import (
    CorrectionNotice,
    Publication,
    PublicationStatus,
    PublicationVersion,
    SiteRelease,
)
from tests.test_editorial_article import _draft, _pipeline

CONFIG = SiteConfig(
    site_name="ALMANYA24",
    base_url="https://almanya24.example",
    imprint="Sorumlu: Redaktion",
    privacy="Çerez kullanılmaz.",
    contact="redaksiyon@almanya24.example",
    usage_rights="Metinler CC BY 4.0",
)


def _approved(db_session, tmp_path, *, before_approval=None, **kwargs):
    """A complete, operator-approved article ready to be published."""
    revision, run, _ = _pipeline(db_session, tmp_path, **kwargs)
    if before_approval is not None:
        before_approval()
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)
    approve_article_revision(
        db_session,
        article,
        research_run=run,
        operator_ref="web:editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )
    return article, revision, run


# ---------------------------------------------------------------------------
# Public identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Berlin'de 100 yeni konut", "berlinde-100-yeni-konut"),
        ("Işık ve Şehir", "isik-ve-sehir"),
        ("Çöp — Ödül", "cop-odul"),
        ("   ", "haber"),
    ],
)
def test_a_turkish_headline_becomes_a_readable_url(title, expected):
    """``ı`` is not ``i`` with the dot removed; stripping accents alone loses it."""
    assert slugify(title) == expected


def test_publishing_gives_a_topic_one_stable_address(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)

    publication = publish_article(
        db_session, article, operator_ref="web:editor", section="politika"
    )

    assert publication.slug
    assert publication.status == PublicationStatus.PUBLISHED.value
    assert db_session.query(PublicationVersion).count() == 1


def test_republishing_the_same_revision_does_not_duplicate_the_version(
    db_session, tmp_path
):
    article, _, _ = _approved(db_session, tmp_path)

    publish_article(db_session, article, operator_ref="web:editor")
    publish_article(db_session, article, operator_ref="web:editor")

    assert db_session.query(PublicationVersion).count() == 1
    assert db_session.query(Publication).count() == 1


def test_an_unapproved_article_cannot_be_published(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )

    with pytest.raises(PublicationBlocked):
        publish_article(db_session, article, operator_ref="web:editor")
    assert db_session.query(Publication).count() == 0


def test_publishing_needs_a_named_operator(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)

    with pytest.raises(ValueError):
        publish_article(db_session, article, operator_ref="  ")


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


def test_the_public_article_carries_only_allowlisted_fields(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    public = build_public_article(
        db_session, publication, base_url=CONFIG.base_url
    ).to_dict()

    assert set(public) == {
        "slug",
        "section",
        "language",
        "title",
        "lede",
        "published_on",
        "updated_on",
        "canonical_url",
        "status",
        "paragraphs",
        "sources",
        "media",
        "corrections",
        "related",
    }


def test_no_internal_field_survives_serialisation(db_session, tmp_path):
    """A private column must not be reachable, not merely unrendered."""
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    public = build_public_article(db_session, publication, base_url=CONFIG.base_url)
    blob = json.dumps(public.to_dict(), ensure_ascii=False)

    for forbidden in (
        "operator_ref",
        "rationale",
        "blob_path",
        "body_path",
        "content_text",
        "claim_key",
        "verdict",
        "evidence_hash",
        "research_run",
    ):
        assert forbidden not in blob


def test_a_source_appears_with_its_publisher_and_retrieval_date(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    public = build_public_article(db_session, publication, base_url=CONFIG.base_url)

    assert public.sources
    assert public.sources[0].url.startswith("https://")
    assert public.sources[0].retrieved_on.count("-") == 2
    assert public.sources[0].published_on is None
    assert any(paragraph.sources for paragraph in public.paragraphs)


def test_a_picture_is_published_with_its_credit(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    public = build_public_article(db_session, publication, base_url=CONFIG.base_url)

    assert public.media
    assert public.media[0].attribution
    assert public.media[0].license
    assert "/" not in public.media[0].file_name


# ---------------------------------------------------------------------------
# Freshness at build time
# ---------------------------------------------------------------------------


def test_weakened_evidence_removes_a_live_article_from_the_next_build(
    db_session, tmp_path
):
    """Approval was true when it was given; the build asks again."""
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    assessment = db_session.query(ClaimAssessment).one()
    assessment.verdict = "insufficient"
    db_session.commit()

    assert export_blockers(db_session, article)
    with pytest.raises(PublicationBlocked):
        build_public_article(db_session, publication, base_url=CONFIG.base_url)

    result = build_site(
        db_session, root=tmp_path / "site", config=CONFIG, operator_ref="cli:ops"
    )

    assert result.article_count == 0
    assert result.skipped
    assert not (result.directory / publication.section / publication.slug).exists()


def test_revoked_media_rights_stop_the_article_too(db_session, tmp_path):
    from btcedu.core.editorial.media import revoke_media_decision
    from btcedu.models.media_rights import MediaUseDecision

    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    decision = db_session.query(MediaUseDecision).one()
    revoke_media_decision(db_session, decision, reason="Lizenz unklar")

    with pytest.raises(PublicationBlocked):
        build_public_article(db_session, publication, base_url=CONFIG.base_url)


# ---------------------------------------------------------------------------
# The static build
# ---------------------------------------------------------------------------


def _build(db_session, tmp_path):
    return build_site(
        db_session, root=tmp_path / "site", config=CONFIG, operator_ref="cli:ops"
    )


def test_a_build_produces_a_complete_readable_site(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    result = _build(db_session, tmp_path)
    root = result.directory
    page = (root / publication.section / publication.slug / "index.html").read_text(
        encoding="utf-8"
    )

    assert result.article_count == 1
    assert (root / "index.html").is_file()
    assert (root / "sitemap.xml").is_file()
    assert (root / "feed.xml").is_file()
    assert (root / "arama" / "index.json").is_file()
    assert (root / "kunye" / "index.html").is_file()
    assert '<link rel="canonical"' in page
    assert '"@type": "NewsArticle"' in page
    assert "Kaynaklar" in page
    index = (root / "index.html").read_text(encoding="utf-8")
    assert 'class="brand"' in index
    assert 'class="lead-grid"' in index
    assert 'class="lead-story"' in index
    assert 'aria-label="Haber kategorileri"' in index


def test_a_build_uses_the_configured_public_subpath(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    config = SiteConfig(
        site_name="ALMANYA24 DEV",
        base_url="https://sahimi.app/almanya24-dev/",
        preview_notice="Korumalı geliştirme önizlemesi",
    )

    result = build_site(
        db_session, root=tmp_path / "site", config=config, operator_ref="cli:ops"
    )
    root = result.directory
    index = (root / "index.html").read_text(encoding="utf-8")
    article_page = (
        root / publication.section / publication.slug / "index.html"
    ).read_text(encoding="utf-8")
    search_page = (root / "arama" / "index.html").read_text(encoding="utf-8")
    search_script = (root / "assets" / "search.js").read_text(encoding="utf-8")

    assert 'href="/almanya24-dev/assets/site.css"' in index
    assert (
        f'href="/almanya24-dev/{publication.section}/{publication.slug}/"' in index
    )
    assert 'action="/almanya24-dev/arama/"' in index
    assert 'href="/almanya24-dev/"' in article_page
    assert 'src="/almanya24-dev/media/' in article_page
    assert 'src="/almanya24-dev/assets/search.js"' in search_page
    assert 'const basePath = "/almanya24-dev";' in search_script
    assert "Korumalı geliştirme önizlemesi" in index
    assert f'href="/almanya24-dev/{publication.section}/"' in index


def test_the_build_copies_only_approved_media_bytes(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")

    result = _build(db_session, tmp_path)

    copied = sorted(p.name for p in (result.directory / "media").iterdir())
    assert len(copied) == 1


def test_generated_text_cannot_escape_into_page_structure(db_session, tmp_path):
    """The caption is data. A page must not let it become markup."""
    from btcedu.models.media_rights import MediaUseDecision

    def hostile():
        decision = db_session.query(MediaUseDecision).one()
        decision.attribution_text = "<script>alert(1)</script>"
        db_session.commit()

    article, _, _ = _approved(db_session, tmp_path, before_approval=hostile)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    result = _build(db_session, tmp_path)
    page = (
        result.directory / publication.section / publication.slug / "index.html"
    ).read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_the_search_index_is_public_only_and_bounded(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")

    result = _build(db_session, tmp_path)
    raw = (result.directory / "arama" / "index.json").read_bytes()
    documents = json.loads(raw.decode("utf-8"))

    assert len(raw) <= 1024 * 1024
    assert set(documents[0]) == {"slug", "title", "section", "text", "published_on"}


def test_a_build_never_writes_into_the_live_directory(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")
    first = _build(db_session, tmp_path)
    switch_release(db_session, root=tmp_path / "site", release_id=first.release_id)
    before = (first.directory / "index.html").read_text(encoding="utf-8")

    second = _build(db_session, tmp_path)

    assert second.directory != first.directory
    assert (first.directory / "index.html").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# The pointer
# ---------------------------------------------------------------------------


def test_the_pointer_moves_in_one_step(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")
    first = _build(db_session, tmp_path)
    switch_release(db_session, root=tmp_path / "site", release_id=first.release_id)
    second = _build(db_session, tmp_path)

    pointer = switch_release(
        db_session, root=tmp_path / "site", release_id=second.release_id
    )

    assert pointer.is_symlink()
    assert pointer.resolve() == second.directory.resolve()
    assert live_release(db_session).release_id == second.release_id
    assert (
        db_session.query(SiteRelease).filter_by(release_id=first.release_id).one().status
        == ReleaseStatus.SUPERSEDED.value
    )


def test_a_crash_before_the_switch_leaves_the_old_site_serving(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")
    first = _build(db_session, tmp_path)
    switch_release(db_session, root=tmp_path / "site", release_id=first.release_id)
    interrupted = _build(db_session, tmp_path)
    record = (
        db_session.query(SiteRelease)
        .filter_by(release_id=interrupted.release_id)
        .one()
    )
    record.status = ReleaseStatus.BUILDING.value  # the process died mid-build
    db_session.commit()

    removed = reconcile_releases(db_session, root=tmp_path / "site")

    assert interrupted.release_id in removed
    assert not interrupted.directory.exists()
    assert (tmp_path / "site" / "current").resolve() == first.directory.resolve()
    assert live_release(db_session).release_id == first.release_id


def test_reconciliation_believes_the_pointer_not_the_database(db_session, tmp_path):
    """After a restore, the database may name a release nobody is serving."""
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")
    first = _build(db_session, tmp_path)
    second = _build(db_session, tmp_path)
    switch_release(db_session, root=tmp_path / "site", release_id=first.release_id)
    stale = db_session.query(SiteRelease).filter_by(release_id=second.release_id).one()
    stale.status = ReleaseStatus.LIVE.value
    db_session.commit()

    reconcile_releases(db_session, root=tmp_path / "site")

    assert live_release(db_session).release_id == first.release_id


def test_an_unbuilt_release_cannot_go_live(db_session, tmp_path):
    with pytest.raises(Exception):
        switch_release(db_session, root=tmp_path / "site", release_id="does-not-exist")


# ---------------------------------------------------------------------------
# Corrections and withdrawal
# ---------------------------------------------------------------------------


def _redraft(db_session, revision, run):
    """A second approved article, the way one really arises.

    Redrafting an unchanged state deliberately reuses the stored revision, so a
    correction only exists once the evidence behind it actually moved.
    """
    assessment = db_session.query(ClaimAssessment).one()
    assessment.rationale = "Reassessment: official figure reconfirmed with current evidence"
    db_session.commit()

    second = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(lede="Berlin yeni konut sayısını doğruladı."),
    )
    preview = article_preview(db_session, second, research_run=run)
    approve_article_revision(
        db_session,
        second,
        research_run=run,
        operator_ref="web:editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )
    return second


def test_a_replacement_needs_a_correction_note_and_keeps_the_url(
    db_session, tmp_path
):
    article, revision, run = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    slug = publication.slug

    second = _redraft(db_session, revision, run)

    with pytest.raises(ValueError):
        publish_article(db_session, second, operator_ref="web:editor")

    publication = publish_article(
        db_session,
        second,
        operator_ref="web:editor",
        correction_summary="Rakam kaynağa göre düzeltildi.",
    )

    assert publication.slug == slug
    assert publication.status == PublicationStatus.CORRECTED.value
    assert db_session.query(CorrectionNotice).count() == 1


def test_a_correction_is_visible_on_the_page(db_session, tmp_path):
    article, revision, run = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    second = _redraft(db_session, revision, run)
    publish_article(
        db_session,
        second,
        operator_ref="web:editor",
        correction_summary="Rakam kaynağa göre düzeltildi.",
    )

    result = _build(db_session, tmp_path)
    page = (
        result.directory / publication.section / publication.slug / "index.html"
    ).read_text(encoding="utf-8")

    assert "Düzeltme" in page
    assert "Rakam kaynağa göre düzeltildi." in page


def test_a_withdrawal_leaves_a_tombstone_and_empties_the_feeds(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")
    withdraw_publication(
        db_session,
        publication,
        operator_ref="web:editor",
        summary="Kaynak iddiayı geri çekti.",
    )

    result = _build(db_session, tmp_path)
    root = result.directory
    page = (root / publication.section / publication.slug / "index.html").read_text(
        encoding="utf-8"
    )
    gone = json.loads((root / "gone.json").read_text(encoding="utf-8"))

    assert result.article_count == 0
    assert "geri çekildi" in page
    assert "Kaynak iddiayı geri çekti." in page
    assert f"/{publication.section}/{publication.slug}/" in gone
    assert publication.slug not in (root / "sitemap.xml").read_text(encoding="utf-8")
    assert publication.slug not in (root / "feed.xml").read_text(encoding="utf-8")
    assert (
        publication.slug
        not in (root / "arama" / "index.json").read_text(encoding="utf-8")
    )


def test_a_withdrawal_needs_a_reason_readers_can_see(db_session, tmp_path):
    article, _, _ = _approved(db_session, tmp_path)
    publication = publish_article(db_session, article, operator_ref="web:editor")

    with pytest.raises(ValueError):
        withdraw_publication(
            db_session, publication, operator_ref="web:editor", summary="  "
        )


def test_no_private_path_leaks_into_the_built_site(db_session, tmp_path):
    """A page may reference media by name, never by where it lives on disk."""
    article, _, _ = _approved(db_session, tmp_path)
    publish_article(db_session, article, operator_ref="web:editor")

    result = _build(db_session, tmp_path)
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in Path(result.directory).rglob("*")
        if path.is_file() and path.suffix in {".html", ".xml", ".json", ".css"}
    )

    assert str(tmp_path) not in text
    assert "sqlite" not in text.lower()
    assert "web:editor" not in text


# ---------------------------------------------------------------------------
# The development auto-release switch
# ---------------------------------------------------------------------------


def _unapproved(db_session, tmp_path):
    """A finished draft that nobody signed off."""
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    return article


def test_the_switch_is_off_unless_it_is_asked_for(db_session, tmp_path):
    """The default has to stay the blocking one, or the switch is a back door."""
    article = _unapproved(db_session, tmp_path)

    with pytest.raises(PublicationBlocked):
        publish_article(db_session, article, operator_ref="dev:preview")


def test_the_switch_offers_a_draft_without_inventing_an_approval(
    db_session, tmp_path
):
    """Visible, yes. Approved, no — the stored record must not claim otherwise."""
    from btcedu.core.editorial.public import _DEV_AUTO_RELEASE_DECISION
    from btcedu.models.article import EditorialDecision

    article = _unapproved(db_session, tmp_path)

    publication = publish_article(
        db_session,
        article,
        operator_ref="dev:preview",
        dev_auto_release=True,
    )

    assert publication.slug
    assert db_session.query(EditorialDecision).count() == 0
    version = db_session.query(PublicationVersion).one()
    assert version.decision_id == _DEV_AUTO_RELEASE_DECISION


def test_the_switch_does_not_silence_the_checks(db_session, tmp_path):
    """The blockers are still computed, and they travel with the article."""
    article = _unapproved(db_session, tmp_path)
    publication = publish_article(
        db_session, article, operator_ref="dev:preview", dev_auto_release=True
    )

    public = build_public_article(
        db_session,
        publication,
        base_url=CONFIG.base_url,
        dev_auto_release=True,
    )

    assert public.is_dev_auto_released
    assert "No operator approval recorded" in public.dev_auto_release_reasons


def test_the_switch_does_not_overrule_a_technical_check(db_session, tmp_path):
    """A malformed URL is not an editorial opinion the switch may overrule."""
    article = _unapproved(db_session, tmp_path)
    publication = publish_article(
        db_session, article, operator_ref="dev:preview", dev_auto_release=True
    )
    publication.slug = "Ge\u00e7ersiz Slug"

    with pytest.raises(PublicationBlocked):
        build_public_article(
            db_session,
            publication,
            base_url=CONFIG.base_url,
            dev_auto_release=True,
        )


def test_an_unapproved_draft_stays_out_of_an_ordinary_build(db_session, tmp_path):
    article = _unapproved(db_session, tmp_path)
    publish_article(
        db_session, article, operator_ref="dev:preview", dev_auto_release=True
    )

    result = _build(db_session, tmp_path)

    assert result.article_count == 0


def test_a_development_build_shows_the_draft_and_says_what_is_missing(
    db_session, tmp_path
):
    """The page must not read like a published article that lost a signature."""
    import dataclasses

    article = _unapproved(db_session, tmp_path)
    publication = publish_article(
        db_session, article, operator_ref="dev:preview", dev_auto_release=True
    )

    result = build_site(
        db_session,
        root=tmp_path / "site",
        config=dataclasses.replace(CONFIG, dev_auto_release=True),
        operator_ref="cli:ops",
    )
    page = (
        result.directory / publication.section / publication.slug / "index.html"
    ).read_text(encoding="utf-8")
    index = (result.directory / "index.html").read_text(encoding="utf-8")

    assert result.article_count == 1
    assert 'class="dev-release"' in page
    assert "otomatik yay\u0131na al\u0131nd\u0131" in page
    assert "No operator approval recorded" in page
    assert 'class="dev-badge"' in index


def test_the_development_notice_does_not_leak_into_the_public_payload(
    db_session, tmp_path
):
    """The reasons are internal status strings; the feed and index are public."""
    article = _unapproved(db_session, tmp_path)
    publication = publish_article(
        db_session, article, operator_ref="dev:preview", dev_auto_release=True
    )

    public = build_public_article(
        db_session, publication, base_url=CONFIG.base_url, dev_auto_release=True
    )

    assert public.dev_auto_release_reasons
    assert "dev_auto_release_reasons" not in public.to_dict()
    assert "approval" not in json.dumps(public.to_dict(), ensure_ascii=False)
