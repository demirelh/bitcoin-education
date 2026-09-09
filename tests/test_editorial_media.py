from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from btcedu.core.editorial.ingest import import_story
from btcedu.core.editorial.media import (
    ALLOWED_IMAGE_TYPES,
    BrokenImage,
    MediaBlobStore,
    MediaRequirement,
    approved_revision_media,
    assess_candidate,
    build_attribution,
    evaluate_license,
    inspect_image,
    revoke_media_decision,
    select_media_for_revision,
)
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_rights import (
    LicenseEvidence,
    LicenseFamily,
    MediaRole,
    MediaSourceOffer,
    MediaUseDecision,
    MediaUseStatus,
    NewsroomMediaAsset,
    RevisionMedia,
)
from btcedu.models.story_schema import Story, StoryCategory, StoryType
from btcedu.services.commons_service import (
    FixtureCommonsProvider,
    MediaCandidate,
    WikimediaCommonsProvider,
    parse_catalog_datetime,
    sanitize_catalog_html,
)
from btcedu.services.document_fetcher import (
    DocumentFetcher,
    DocumentHTTPError,
    DocumentSnapshotStore,
    RawDocumentResponse,
    UnsupportedDocument,
)

PUBLIC_IP = "93.184.216.34"
EVENT_DATE = datetime(2026, 9, 8, tzinfo=UTC)


def _png_bytes(width: int = 64, height: int = 48, color: str = "red") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _story() -> Story:
    return Story(
        story_id="story-1",
        order=1,
        headline_de="Wohnungsbau in Berlin",
        category=StoryCategory.GESELLSCHAFT,
        story_type=StoryType.MELDUNG,
        text_de="Berlin meldet 100 neue Wohnungen.",
        source_text="Berlin meldet 100 neue Wohnungen.",
        source_segment_ids=["segment-1"],
        source_start_seconds=4.0,
        source_end_seconds=10.0,
        word_count=5,
        estimated_duration_seconds=6,
    )


def _revision(db_session) -> EditorialRevision:
    imported = import_story(db_session, episode_id="episode-1", story=_story(), claims=[])
    revision = EditorialRevision(
        revision_id=str(uuid.uuid4()),
        topic_id=imported.topic.id,
        content_hash="c" * 64,
        policy_version="article-v1",
    )
    db_session.add(revision)
    db_session.commit()
    return revision


def _candidate(**overrides) -> MediaCandidate:
    defaults = dict(
        provider="commons_fixture",
        offer_key="File:Berlin housing.png",
        page_url="https://commons.example/wiki/File:Berlin_housing.png",
        file_url="https://upload.example/berlin.png",
        title="Neubau in Berlin",
        description="Neue Wohnungen in Berlin, aufgenommen 2026.",
        author="A. Fotograf",
        uploader="Uploader",
        license_id="CC BY-SA 4.0",
        captured_at_text="2026-09-08",
        captured_at=EVENT_DATE,
        mime_type="image/png",
        width=64,
        height=48,
        byte_size=1024,
    )
    defaults.update(overrides)
    return MediaCandidate(**defaults)


def _requirement(**overrides) -> MediaRequirement:
    defaults = dict(
        subject="Berlin",
        role=MediaRole.EVENT,
        event_date=EVENT_DATE,
        caption="Neubau in Berlin",
    )
    defaults.update(overrides)
    return MediaRequirement(**defaults)


@dataclass
class MediaTransport:
    responses: dict[str, RawDocumentResponse]

    def __post_init__(self):
        self.calls: list[str] = []

    def __call__(self, url, address, connect_timeout, read_timeout, max_bytes):
        self.calls.append(url)
        return self.responses[url]


def _fetcher(tmp_path, files: dict[str, tuple[str, bytes]]):
    transport = MediaTransport(
        {
            url: RawDocumentResponse(
                status_code=200,
                headers={"Content-Type": content_type},
                body=body,
            )
            for url, (content_type, body) in files.items()
        }
    )
    fetcher = DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "newsroom" / "documents"),
        resolver=lambda host, port: [PUBLIC_IP],
        transport=transport,
    )
    return fetcher, transport


@pytest.mark.parametrize(
    ("license_id", "family", "usable", "commercial", "derivatives", "attribution"),
    [
        ("CC0 1.0", LicenseFamily.CC0, True, True, True, False),
        ("Public domain", LicenseFamily.PUBLIC_DOMAIN, True, True, True, False),
        ("CC BY 4.0", LicenseFamily.CC_BY, True, True, True, True),
        ("CC BY-SA 3.0", LicenseFamily.CC_BY_SA, True, True, True, True),
        ("CC BY-NC 4.0", LicenseFamily.CC_BY_NC, False, False, True, True),
        ("CC BY-ND 4.0", LicenseFamily.CC_BY_ND, True, True, False, True),
        ("", LicenseFamily.UNKNOWN, False, False, False, True),
        ("All rights reserved", LicenseFamily.UNKNOWN, False, False, False, True),
    ],
)
def test_license_families_are_read_explicitly(
    license_id, family, usable, commercial, derivatives, attribution
):
    policy = evaluate_license(_candidate(license_id=license_id))

    assert policy.family == family
    assert policy.usable is usable
    assert policy.commercial_use_allowed is commercial
    assert policy.derivatives_allowed is derivatives
    assert policy.attribution_required is attribution


def test_multiple_licenses_use_the_most_permissive_recognised_offer():
    policy = evaluate_license(_candidate(license_id="GFDL; CC BY-SA 3.0"))

    assert policy.family == LicenseFamily.CC_BY_SA
    assert policy.usable is True
    assert "unrecognised alternatives" in policy.reason


def test_unrecognised_license_alternatives_alone_stay_unusable():
    policy = evaluate_license(_candidate(license_id="GFDL; Fair use"))

    assert policy.family == LicenseFamily.UNKNOWN
    assert policy.usable is False


def test_share_alike_is_recorded_rather_than_silently_dropped():
    policy = evaluate_license(_candidate(license_id="CC BY-SA 4.0"))

    assert policy.share_alike is True
    assert policy.version == "4.0"


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"license_id": "CC BY-NC 2.0"}, "commercial"),
        ({"license_id": "Unclear"}, "rights unclear"),
        ({"title": "Neubau in Hamburg", "description": "Hamburg"}, "does not depict"),
        ({"captured_at": None, "captured_at_text": None}, "no capture date"),
        ({"captured_at": datetime(2011, 5, 1, tzinfo=UTC)}, "does not match the event"),
        ({"mime_type": "image/gif"}, "unsupported image type"),
        ({"byte_size": 99 * 1024 * 1024}, "larger than the newsroom image limit"),
    ],
)
def test_candidate_problems_are_named_and_block_eligibility(overrides, reason_fragment):
    assessment = assess_candidate(_candidate(**overrides), _requirement())

    assert assessment.eligible is False
    assert any(reason_fragment in reason for reason in assessment.reasons)


def test_no_derivative_license_blocks_a_placement_that_needs_a_crop():
    candidate = _candidate(license_id="CC BY-ND 4.0")

    cropped = assess_candidate(candidate, _requirement(allow_cropping=True))
    uncropped = assess_candidate(candidate, _requirement(allow_cropping=False))

    assert cropped.eligible is False
    assert any("derivative" in reason for reason in cropped.reasons)
    assert uncropped.eligible is True


def test_old_portrait_may_still_serve_as_an_archive_picture():
    candidate = _candidate(captured_at=datetime(2011, 5, 1, tzinfo=UTC))

    as_event = assess_candidate(candidate, _requirement(role=MediaRole.EVENT))
    as_archive = assess_candidate(candidate, _requirement(role=MediaRole.ARCHIVE))

    assert as_event.eligible is False
    assert as_archive.eligible is True


def test_selection_stores_asset_offer_evidence_and_decision(db_session, tmp_path):
    revision = _revision(db_session)
    candidate = _candidate()
    provider = FixtureCommonsProvider({"Berlin": (candidate,)})
    fetcher, transport = _fetcher(
        tmp_path, {candidate.file_url: ("image/png", _png_bytes())}
    )

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=provider,
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "newsroom" / "media"),
        edit_note="beschnitten",
    )

    assert selection.has_picture
    asset = db_session.query(NewsroomMediaAsset).one()
    offer = db_session.query(MediaSourceOffer).one()
    evidence = db_session.query(LicenseEvidence).one()
    decision = selection.decision
    assert asset.width == 64 and asset.height == 48
    assert offer.page_url == candidate.page_url
    assert evidence.license_family == LicenseFamily.CC_BY_SA.value
    assert evidence.share_alike is True
    assert evidence.credit_source == candidate.page_url
    assert decision.role == MediaRole.EVENT.value
    assert decision.license_evidence_id == evidence.id
    assert "A. Fotograf" in decision.attribution_text
    assert "CC BY-SA 4.0" in decision.attribution_text
    assert "beschnitten" in decision.attribution_text
    assert len(transport.calls) == 1
    assert [row.position for row in approved_revision_media(db_session, revision)] == [0]


def test_no_suitable_candidate_is_a_valid_outcome_without_substitute(db_session, tmp_path):
    revision = _revision(db_session)
    provider = FixtureCommonsProvider(
        {
            "Berlin": (
                _candidate(offer_key="a", license_id="CC BY-NC 4.0"),
                _candidate(offer_key="b", title="Hamburg", description="Hamburg"),
                _candidate(offer_key="c", license_id="", captured_at=None),
            )
        }
    )
    fetcher, transport = _fetcher(tmp_path, {})

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=provider,
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "newsroom" / "media"),
    )

    assert selection.has_picture is False
    assert selection.decision is None
    assert len(selection.rejected) == 3
    assert transport.calls == []
    assert db_session.query(NewsroomMediaAsset).count() == 0
    assert db_session.query(MediaUseDecision).count() == 0
    assert approved_revision_media(db_session, revision) == ()


def test_ranking_cannot_promote_a_rights_or_context_failure(db_session, tmp_path):
    revision = _revision(db_session)
    attractive_but_blocked = _candidate(
        offer_key="blocked",
        file_url="https://upload.example/blocked.png",
        license_id="CC BY-NC 4.0",
        author="Berühmter Fotograf",
    )
    plain_but_clean = _candidate(
        offer_key="clean",
        file_url="https://upload.example/clean.png",
        author=None,
        uploader="Uploader",
    )
    provider = FixtureCommonsProvider({"Berlin": (attractive_but_blocked, plain_but_clean)})
    fetcher, transport = _fetcher(
        tmp_path,
        {
            attractive_but_blocked.file_url: ("image/png", _png_bytes(color="blue")),
            plain_but_clean.file_url: ("image/png", _png_bytes(color="green")),
        },
    )

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=provider,
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "newsroom" / "media"),
    )

    assert selection.has_picture
    offer = db_session.query(MediaSourceOffer).one()
    assert offer.offer_key == "clean"
    assert transport.calls == [plain_but_clean.file_url]


def test_identical_bytes_are_stored_once_but_offers_stay_separate(db_session, tmp_path):
    first = _revision(db_session)
    second = EditorialRevision(
        revision_id=str(uuid.uuid4()),
        topic_id=first.topic_id,
        content_hash="d" * 64,
        policy_version="article-v1",
    )
    db_session.add(second)
    db_session.commit()
    body = _png_bytes()
    duplicate_a = _candidate(offer_key="mirror-a", file_url="https://a.example/x.png")
    duplicate_b = _candidate(
        offer_key="mirror-b",
        file_url="https://b.example/x.png",
        page_url="https://other.example/wiki/File:X.png",
        license_id="CC BY 4.0",
    )
    fetcher, _ = _fetcher(
        tmp_path,
        {
            duplicate_a.file_url: ("image/png", body),
            duplicate_b.file_url: ("image/png", body),
        },
    )
    store = MediaBlobStore(tmp_path / "newsroom" / "media")

    select_media_for_revision(
        db_session,
        editorial_revision=first,
        requirement=_requirement(),
        provider=FixtureCommonsProvider({"Berlin": (duplicate_a,)}),
        fetcher=fetcher,
        blob_store=store,
    )
    select_media_for_revision(
        db_session,
        editorial_revision=second,
        requirement=_requirement(),
        provider=FixtureCommonsProvider({"Berlin": (duplicate_b,)}),
        fetcher=fetcher,
        blob_store=store,
    )

    assert db_session.query(NewsroomMediaAsset).count() == 1
    offers = db_session.query(MediaSourceOffer).order_by(MediaSourceOffer.offer_key).all()
    assert [offer.offer_key for offer in offers] == ["mirror-a", "mirror-b"]
    families = {
        row.license_family for row in db_session.query(LicenseEvidence)
    }
    assert families == {LicenseFamily.CC_BY_SA.value, LicenseFamily.CC_BY.value}
    assert len(list((tmp_path / "newsroom" / "media").rglob("*.png"))) == 1


def test_restart_reuses_the_existing_decision_without_new_downloads(db_session, tmp_path):
    revision = _revision(db_session)
    candidate = _candidate()
    provider = FixtureCommonsProvider({"Berlin": (candidate,)})
    fetcher, transport = _fetcher(
        tmp_path, {candidate.file_url: ("image/png", _png_bytes())}
    )
    store = MediaBlobStore(tmp_path / "newsroom" / "media")
    kwargs = dict(
        editorial_revision=revision,
        requirement=_requirement(),
        provider=provider,
        fetcher=fetcher,
        blob_store=store,
    )

    first = select_media_for_revision(db_session, **kwargs)
    second = select_media_for_revision(db_session, **kwargs)

    assert second.from_cache is True
    assert second.decision.id == first.decision.id
    assert len(transport.calls) == 1
    assert len(provider.calls) == 1
    assert db_session.query(RevisionMedia).count() == 1


def test_revoked_decision_cannot_reach_the_article(db_session, tmp_path):
    revision = _revision(db_session)
    candidate = _candidate()
    fetcher, _ = _fetcher(tmp_path, {candidate.file_url: ("image/png", _png_bytes())})

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=FixtureCommonsProvider({"Berlin": (candidate,)}),
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "newsroom" / "media"),
    )
    revoke_media_decision(db_session, selection.decision, reason="Rechte zurückgezogen")

    assert selection.decision.status == MediaUseStatus.REVOKED.value
    assert selection.decision.revoked_reason == "Rechte zurückgezogen"
    assert approved_revision_media(db_session, revision) == ()
    assert db_session.query(MediaUseDecision).count() == 1


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("image/png", b"not really a png"),
        ("image/png", _png_bytes()[:20]),
    ],
)
def test_broken_image_bytes_are_rejected(content_type, body):
    with pytest.raises(BrokenImage):
        inspect_image(body, content_type)


def test_declared_type_must_match_the_actual_bytes():
    with pytest.raises(BrokenImage, match="does not match"):
        inspect_image(_png_bytes(), "image/jpeg")


def test_unfetchable_candidate_falls_through_to_the_next_one(db_session, tmp_path):
    revision = _revision(db_session)
    broken = _candidate(offer_key="broken", file_url="https://a.example/broken.png")
    good = _candidate(offer_key="good", file_url="https://b.example/good.png")
    fetcher, transport = _fetcher(
        tmp_path,
        {
            broken.file_url: ("image/png", b"corrupt"),
            good.file_url: ("image/png", _png_bytes()),
        },
    )

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=FixtureCommonsProvider({"Berlin": (broken, good)}),
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "newsroom" / "media"),
    )

    assert selection.has_picture
    assert db_session.query(MediaSourceOffer).one().offer_key == "good"
    assert any("file could not be used" in reason for _, reason in selection.rejected)
    assert len(transport.calls) == 2


def test_http_failure_leaves_the_revision_without_a_picture(db_session, tmp_path):
    revision = _revision(db_session)
    candidate = _candidate()
    transport = MediaTransport(
        {
            candidate.file_url: RawDocumentResponse(
                status_code=404, headers={"Content-Type": "text/html"}, body=b""
            )
        }
    )
    fetcher = DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "documents"),
        resolver=lambda host, port: [PUBLIC_IP],
        transport=transport,
    )

    selection = select_media_for_revision(
        db_session,
        editorial_revision=revision,
        requirement=_requirement(),
        provider=FixtureCommonsProvider({"Berlin": (candidate,)}),
        fetcher=fetcher,
        blob_store=MediaBlobStore(tmp_path / "media"),
    )

    assert selection.has_picture is False
    assert db_session.query(MediaUseDecision).count() == 0


def test_media_download_uses_the_controlled_fetcher_checks(tmp_path):
    fetcher = DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "documents"),
        resolver=lambda host, port: ["10.0.0.7"],
        transport=lambda *args: RawDocumentResponse(200, {"Content-Type": "image/png"}, b""),
    )

    with pytest.raises(Exception) as excinfo:
        fetcher.fetch_binary(
            "https://private.example/x.png",
            allowed_content_types=ALLOWED_IMAGE_TYPES,
        )

    assert "non-public" in str(excinfo.value)


def test_media_download_rejects_a_wrong_content_type(tmp_path):
    url = "https://upload.example/x.png"
    transport = MediaTransport(
        {url: RawDocumentResponse(200, {"Content-Type": "text/html"}, b"<html></html>")}
    )
    fetcher = DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "documents"),
        resolver=lambda host, port: [PUBLIC_IP],
        transport=transport,
    )

    with pytest.raises(UnsupportedDocument):
        fetcher.fetch_binary(url, allowed_content_types=ALLOWED_IMAGE_TYPES)


def test_uploader_is_not_promoted_to_author_in_the_credit():
    candidate = _candidate(author=None, credit=None, uploader="SomeUploader")

    attribution = build_attribution(candidate, evaluate_license(candidate))

    assert "SomeUploader" in attribution
    assert candidate.page_url in attribution


def test_catalog_html_is_reduced_to_visible_text():
    raw = '<p>Berlin <script>alert("x")</script><a href="https://evil.example">Mitte</a></p>'

    assert sanitize_catalog_html(raw) == "Berlin Mitte"
    assert "script" not in sanitize_catalog_html(raw)
    assert "evil.example" not in sanitize_catalog_html(raw)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-08", datetime(2026, 9, 8, tzinfo=UTC)),
        ("2026-09-08 12:30:00", datetime(2026, 9, 8, 12, 30, tzinfo=UTC)),
        ("2026", None),
        ("2026-09", None),
        ("", None),
        ("undatiert", None),
    ],
)
def test_catalog_dates_are_parsed_or_left_missing(value, expected):
    assert parse_catalog_datetime(value) == expected


def test_commons_provider_sanitizes_and_maps_metadata(tmp_path):
    payload = (
        '{"query": {"pages": [{"title": "File:X.png", "imageinfo": [{'
        '"url": "https://upload.example/x.png",'
        '"descriptionurl": "https://commons.example/wiki/File:X.png",'
        '"mime": "image/png", "size": 1234, "width": 800, "height": 600,'
        '"user": "Uploader",'
        '"extmetadata": {'
        '"ImageDescription": {"value": "<b>Berlin</b><script>bad()</script>"},'
        '"Artist": {"value": "<a href=\\"https://x.example\\">A. Fotograf</a>"},'
        '"LicenseShortName": {"value": "CC BY-SA 4.0"},'
        '"DateTimeOriginal": {"value": "2026-09-08"}}}]}]}}'
    )
    url_holder = {}

    class RecordingFetcher:
        def fetch(self, url):
            url_holder["url"] = url

            @dataclass
            class Result:
                text: str
                body: bytes

            return Result(text=payload, body=payload.encode("utf-8"))

    provider = WikimediaCommonsProvider(RecordingFetcher())

    candidates = provider.search("Berlin", limit=5)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.description == "Berlin"
    assert candidate.author == "A. Fotograf"
    assert candidate.uploader == "Uploader"
    assert candidate.license_id == "CC BY-SA 4.0"
    assert candidate.captured_at == datetime(2026, 9, 8, tzinfo=UTC)
    assert candidate.mime_type == "image/png"
    assert "bad()" not in candidate.description
    assert "gsrlimit=5" in url_holder["url"]


def test_existing_pipeline_assets_are_not_treated_as_cleared(db_session, tmp_path):
    """An old Pexels or frame asset carries no rights evidence, so it stays out.

    Migrating the existing pipeline assets into the rights ledger would mark
    pictures as cleared that nobody ever checked.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from btcedu.models.media_asset import Base as MediaBase
    from btcedu.models.media_asset import MediaAsset, MediaAssetType

    engine = create_engine(f"sqlite:///{tmp_path / 'assets.db'}")
    MediaBase.metadata.create_all(engine)
    NewsroomMediaAsset.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        MediaAsset(
            episode_id="episode-1",
            asset_type=MediaAssetType.IMAGE,
            file_path="outputs/episode-1/images/chapter_1.png",
            mime_type="image/png",
            size_bytes=1024,
        )
    )
    session.commit()

    assert session.query(MediaAsset).count() == 1
    assert session.query(NewsroomMediaAsset).count() == 0
    session.close()

    revision = _revision(db_session)
    assert approved_revision_media(db_session, revision) == ()


def test_http_error_type_is_reported_for_media(tmp_path):
    url = "https://upload.example/x.png"
    transport = MediaTransport(
        {url: RawDocumentResponse(503, {"Content-Type": "image/png"}, b"")}
    )
    fetcher = DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "documents"),
        resolver=lambda host, port: [PUBLIC_IP],
        transport=transport,
    )

    with pytest.raises(DocumentHTTPError):
        fetcher.fetch_binary(url, allowed_content_types=ALLOWED_IMAGE_TYPES)
