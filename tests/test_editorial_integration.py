"""Offline external responses; actual workflow, TTS processing and ffmpeg rendering."""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from btcedu.config import Settings
from btcedu.core.editorial.article import (
    ArticleGenerationError,
    approve_article_revision,
    article_preview,
)
from btcedu.core.editorial.media import MediaRequirement
from btcedu.core.editorial.production import bind_episode, production_inputs, require_final
from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.core.editorial.video import (
    EditionBlocked,
    StaleEditionApproval,
    approve_final_video,
    approve_script,
    approve_video_media,
    build_edition,
)
from btcedu.core.editorial.workflow import ModelReply, draft_story
from btcedu.core.pipeline import _get_stages, _run_stage
from btcedu.core.profile_validation import resolve_profile
from btcedu.models.editorial import ResearchRun
from btcedu.models.media_rights import MediaRole
from btcedu.services.commons_service import FixtureCommonsProvider
from btcedu.services.elevenlabs_service import TTSResponse
from tests.test_editorial_article import (
    PASSAGE,
    SUPPORT_URL,
    _candidate,
    _claim_draft,
    _draft,
    _fetcher,
    _plans,
    _search_provider,
    _story,
)
from tests.test_renderer import db_engine  # noqa: F401


def workflow_fixture(
    session,
    tmp_path,
    *,
    relation="supports",
    license_id="CC BY-SA 4.0",
    episode_id="offline-transcript",
):
    settings = Settings(
        newsroom_enabled=True,
        newsroom_data_dir=str(tmp_path / "private"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        render_execution_mode="local",
        anchor_enabled=False,
        render_resolution="1280x720",
        render_fps=12,
        render_preset="ultrafast",
        render_intro_enabled=False,
        render_outro_enabled=False,
        tts_stutter_check_enabled=False,
        dry_run=False,
    )
    tasks = []

    def model(payload):
        task = payload["task"]
        tasks.append(task)
        if task == "extract_claims":
            assert payload["source"]["text"] == PASSAGE
            result = [_claim_draft().model_dump(mode="json")]
        elif task == "evaluate_claim_evidence":
            assert any(PASSAGE in row["text"] for row in payload["documents"])
            result = [
                {
                    "canonical_url": SUPPORT_URL,
                    "relation": relation,
                    "passage": PASSAGE,
                    "rationale": "Fixture: same entity and count in fetched text",
                }
            ]
        elif task == "draft_article":
            result = _draft()
        elif task == "check_article_consistency":
            result = {"consistent": True, "issues": []}
        else:
            raise AssertionError(f"Unexpected model task {task}")
        return ModelReply(result=result, cost_usd=0.001, model="offline-fixture")

    fetcher, _ = _fetcher(tmp_path)
    from dataclasses import replace

    article = draft_story(
        session,
        episode_id=episode_id,
        story=_story(),
        settings=settings,
        model_caller=model,
        search_provider=_search_provider(),
        fetcher=fetcher,
        media_provider=FixtureCommonsProvider(
            {
                "Berlin": (replace(_candidate(), license_id=license_id),),
            }
        ),
        media_requirement=MediaRequirement(subject="Berlin", role=MediaRole.SYMBOLIC),
        plan_builder=_plans,
        max_call_cost_usd=0.01,
    )
    return (
        article,
        session.query(ResearchRun).order_by(ResearchRun.id.desc()).first(),
        settings,
        tasks,
    )


def approve_article(session, article, run):
    preview = article_preview(session, article, research_run=run)
    approve_article_revision(
        session,
        article,
        research_run=run,
        operator_ref="fixture:article-editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )


def test_transcript_to_existing_pipeline_real_synthetic_video(db_session, tmp_path, monkeypatch):
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "Video acceptance needs ffmpeg"
    article, run, settings, tasks = workflow_fixture(db_session, tmp_path)
    assert tasks == [
        "extract_claims",
        "evaluate_claim_evidence",
        "draft_article",
        "check_article_consistency",
    ]
    approve_article(db_session, article, run)
    publication = publish_article(db_session, article, operator_ref="fixture:site-editor")
    site_root = tmp_path / "site"
    build = build_site(db_session, root=site_root, config=SiteConfig())
    switch_release(db_session, root=site_root, release_id=build.release_id)
    page = site_root / "current" / publication.section / publication.slug / "index.html"
    assert "100 yeni konut" in page.read_text()
    assert not list((site_root / "current").rglob("*.db"))
    assert PASSAGE not in "\n".join(p.read_text() for p in build.directory.rglob("*.html"))
    assert (site_root / "current/arama/index.html").is_file()

    profile = resolve_profile(settings, "almanya24_editorial")
    edition = build_edition(db_session, article, research_run=run, profile=profile)
    with pytest.raises(EditionBlocked):
        bind_episode(db_session, edition, settings)
    approve_script(db_session, edition, research_run=run, operator_ref="fixture:script-editor")
    with pytest.raises(EditionBlocked):
        bind_episode(db_session, edition, settings)
    approve_video_media(
        db_session,
        edition,
        research_run=run,
        operator_ref="fixture:video-rights",
        note="Commercial video, crop and on-screen CC attribution reviewed",
    )
    episode = bind_episode(db_session, edition, settings)
    assert edition.episode_id == episode.id
    assert [name for name, _ in _get_stages(settings, episode)] == [
        "script",
        "tts",
        "render",
        "review_gate_3",
    ]
    result = _run_stage(db_session, episode, settings, "script")
    assert result.status == "success", result
    root = Path(settings.outputs_dir) / episode.episode_id
    chapters = json.loads((root / "chapters.json").read_text())
    assert all("Fotograf" in chapter["overlays"][0]["text"] for chapter in chapters["chapters"])
    audio = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-ar",
            "44100",
            "-f",
            "mp3",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    provider = MagicMock()
    provider.synthesize.return_value = TTSResponse(
        audio_bytes=audio,
        duration_seconds=2,
        sample_rate=44100,
        model="synthetic",
        voice_id="synthetic",
        character_count=30,
        cost_usd=0,
    )
    monkeypatch.setattr(
        "btcedu.services.elevenlabs_service.ElevenLabsService", lambda **kwargs: provider
    )
    # The sound is a tone, not speech: provider and semantic speech QA are fixtures.
    monkeypatch.setattr("btcedu.core.tts._noise_floor_db", lambda path: -65.0)
    result = _run_stage(db_session, episode, settings, "tts")
    assert result.status == "success", result
    assert provider.synthesize.call_count == 3
    before = production_inputs(root)
    result = _run_stage(db_session, episode, settings, "render")
    assert result.status == "success", result
    assert production_inputs(root) == before
    video = root / "render/draft.mp4"
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    )
    assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}
    import io

    from PIL import Image

    frame = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    with Image.open(io.BytesIO(frame)) as image:
        assert (
            sum(
                1
                for red, green, blue in image.convert("RGB").getdata()
                if min(red, green, blue) > 180
            )
            > 50
        ), "White credit glyphs must actually be burned into the solid-red fixture"
    from btcedu.core.publisher import _build_youtube_metadata
    from btcedu.core.remote_render import build_job_package

    assert "Fotograf" in _build_youtube_metadata(episode, settings, db_session)[1]
    (root / "internal-investigation.txt").write_text("PRIVATE EVIDENCE")
    package = build_job_package(db_session, episode.episode_id, settings, tmp_path / "package")
    with tarfile.open(package) as archive:
        names = archive.getnames()
    assert "episode/internal-investigation.txt" not in names
    assert "episode/tts/ch01.mp3" in names
    assert set(name.removeprefix("episode/") for name in names if name.startswith("episode/")) == (
        set(production_inputs(root))
    )
    mtime = video.stat().st_mtime_ns
    assert _run_stage(db_session, episode, settings, "render").status == "success"
    assert video.stat().st_mtime_ns == mtime
    assert provider.synthesize.call_count == 3
    assert _run_stage(db_session, episode, settings, "review_gate_3").status == "review_pending"
    approve_final_video(
        db_session,
        edition,
        research_run=run,
        operator_ref="fixture:final-editor",
        video_path=video,
    )
    require_final(db_session, episode, settings)
    assert _run_stage(db_session, episode, settings, "review_gate_3").status == "success"
    from btcedu.core.publisher import (
        _run_all_safety_checks,
        publish_video,
        request_publish_review,
    )
    from btcedu.core.reviewer import approve_review

    metadata = _build_youtube_metadata(episode, settings, db_session)
    checks = _run_all_safety_checks(db_session, episode, settings, *metadata)
    assert {check.name for check in checks if not check.passed} == {
        "profile_publish_permitted",
        "manual_publish_approval",
    }
    # Only this in-memory profile permits the separate, fake publish step.
    monkeypatch.setitem(profile.youtube, "publish_enabled", True)
    publish_settings = settings.model_copy(update={"dry_run": True})
    with pytest.raises(ValueError, match="manual_publish_approval"):
        publish_video(db_session, episode.episode_id, publish_settings, privacy="private")
    publish_review = request_publish_review(
        db_session,
        episode.episode_id,
        publish_settings,
        privacy="private",
    )
    approve_review(db_session, publish_review.id, notes="fixture:explicit-upload-editor")
    result = publish_video(db_session, episode.episode_id, publish_settings, privacy="private")
    assert result.dry_run and result.youtube_video_id == "DRY_RUN"
    assert episode.status.value == "approved"
    original_audio = (root / "tts/ch01.mp3").read_bytes()
    (root / "tts/ch01.mp3").write_bytes(original_audio + b"changed")
    with pytest.raises(StaleEditionApproval):
        require_final(db_session, episode, settings)
    (root / "tts/ch01.mp3").write_bytes(original_audio)
    video.write_bytes(video.read_bytes() + b"changed")
    with pytest.raises(StaleEditionApproval):
        require_final(db_session, episode, settings)


def test_semantically_missing_evidence_blocks_workflow(db_session, tmp_path):
    with pytest.raises(ArticleGenerationError):
        workflow_fixture(db_session, tmp_path, relation="context")


def test_uncleared_requested_picture_blocks_workflow(db_session, tmp_path):
    with pytest.raises(ValueError, match="illustration unresolved"):
        workflow_fixture(db_session, tmp_path, license_id="copyright unknown")


@pytest.mark.parametrize(
    "change",
    [
        "title",
        "lede",
        "paragraph",
        "claim",
        "evidence",
        "source",
        "license",
        "role",
    ],
)
def test_actual_column_changes_invalidate_all_outputs(db_session, tmp_path, change):
    from btcedu.core.editorial.public import export_blockers
    from btcedu.core.editorial.recheck import scan_changes
    from btcedu.core.editorial.site_export import SiteBuildError
    from btcedu.models.article import ArticleParagraph
    from btcedu.models.editorial import ClaimRevision, EvidenceLink, SourceObservation
    from btcedu.models.media_rights import LicenseEvidence, MediaUseDecision

    article, run, settings, _ = workflow_fixture(db_session, tmp_path)
    approve_article(db_session, article, run)
    publish_article(db_session, article, operator_ref="fixture:site")
    root = tmp_path / "site"
    build = build_site(db_session, root=root, config=SiteConfig())
    edition = build_edition(
        db_session,
        article,
        research_run=run,
        profile=resolve_profile(settings, "almanya24_editorial"),
    )
    row, field, value = {
        "title": (article, "title", "Changed title"),
        "lede": (article, "lede", "Changed lede"),
        "paragraph": (db_session.query(ArticleParagraph).one(), "text", "Changed text"),
        "claim": (db_session.query(ClaimRevision).one(), "statement", "Changed claim"),
        "evidence": (db_session.query(EvidenceLink).one(), "passage", "Changed passage"),
        "source": (db_session.query(SourceObservation).first(), "content_text", "Changed source"),
        "license": (db_session.query(LicenseEvidence).one(), "license_id", "Unknown rights"),
        "role": (db_session.query(MediaUseDecision).one(), "role", "archive"),
    }[change]
    setattr(row, field, value)
    db_session.commit()
    assert export_blockers(db_session, article)
    with pytest.raises(SiteBuildError):
        switch_release(db_session, root=root, release_id=build.release_id)
    with pytest.raises(StaleEditionApproval):
        approve_script(db_session, edition, research_run=run, operator_ref="fixture:script")
    changed = scan_changes(db_session)
    assert changed["articles"] == [article.article_revision_id]
    assert changed["editions"] == [edition.edition_id]
    assert len(changed["publications"]) == 1


def test_tool_free_model_contract_and_budgeted_resume(db_session, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from btcedu.core.editorial.workflow import BudgetedCaller
    from btcedu.services.editorial_model import EditorialModel

    _, run, settings, _ = workflow_fixture(db_session, tmp_path)
    response = SimpleNamespace(
        text='{"result": {"consistent": true, "issues": []}}',
        cost_usd=0.01,
        input_tokens=20,
        output_tokens=10,
        model="test-model",
    )
    call = MagicMock(return_value=response)
    monkeypatch.setattr("btcedu.services.claude_service.call_claude", call)
    caller = BudgetedCaller(
        db_session,
        run,
        EditorialModel(settings, provider="openai", model="test-model"),
        provider="openai",
        model="test-model",
        max_call_cost_usd=0.02,
    )
    payload = {"task": "check_article_consistency", "draft": "fixture"}
    assert caller(payload) == caller(payload) == {"consistent": True, "issues": []}
    assert call.call_count == 1
    assert call.call_args.kwargs["provider_override"] == "openai"
    assert "tools" not in call.call_args.kwargs
    response.cost_usd = 0.03
    payload = {"task": "check_article_consistency", "draft": "over-budget"}
    with pytest.raises(RuntimeError, match="exceeded"):
        caller(payload)
    with pytest.raises(RuntimeError, match="exceeds"):
        caller(payload)
    assert call.call_count == 2


def test_legacy_final_decisions_gain_nullable_byte_bindings(db_session):
    from sqlalchemy import inspect, text

    from btcedu.migrations import BindNewsroomVideoBytesMigration
    from btcedu.models.migration import SchemaMigration

    SchemaMigration.__table__.create(db_session.get_bind(), checkfirst=True)
    for column in ("video_sha256", "render_input_hash"):
        db_session.execute(text(f"ALTER TABLE news_edition_decisions DROP COLUMN {column}"))
    db_session.commit()
    migration = BindNewsroomVideoBytesMigration()
    migration.up(db_session)
    migration.up(db_session)
    columns = {
        column["name"]: column
        for column in inspect(db_session.get_bind()).get_columns("news_edition_decisions")
    }
    assert columns["video_sha256"]["nullable"]
    assert columns["render_input_hash"]["nullable"]


def test_revocation_automatically_flags_bound_outputs(db_session, tmp_path):
    from btcedu.core.editorial.media import revoke_media_decision
    from btcedu.models.media_rights import MediaUseDecision
    from btcedu.models.topic_graph import SourceIssue

    article, run, settings, _ = workflow_fixture(db_session, tmp_path)
    approve_article(db_session, article, run)
    edition = build_edition(
        db_session,
        article,
        research_run=run,
        profile=resolve_profile(settings, "almanya24_editorial"),
    )
    revoke_media_decision(
        db_session,
        db_session.query(MediaUseDecision).one(),
        reason="Fixture rights withdrawal",
        revoked_by="fixture:editor",
    )
    assert edition.status == "blocked"
    assert db_session.query(SourceIssue).filter_by(ref=edition.edition_id).count() == 1


def test_explicit_different_channel_is_not_a_duplicate(db_session):
    import re

    from btcedu.core.detector import _stored_broadcast_keys
    from btcedu.models.episode import Episode

    db_session.add(
        Episode(
            episode_id="another-stream",
            source="youtube_rss",
            title="tagesschau 20:00 Uhr, 08.09.2026",
            content_profile="tagesschau_tr",
            channel_id="channel-a",
            url="https://example.invalid/another-stream",
        )
    )
    db_session.commit()
    assert not _stored_broadcast_keys(
        db_session,
        profile_name="tagesschau_tr",
        channel_id="channel-b",
        title_filter=re.compile("tagesschau"),
    )


def test_repeated_story_requires_update_decision_before_another_draft(db_session, tmp_path):
    from btcedu.core.editorial.topics import accept_proposal
    from btcedu.models.article import ArticleRevision
    from btcedu.models.editorial import EditorialRevision, ProviderOperation
    from btcedu.models.topic_graph import UpdateProposal

    article, _, _, _ = workflow_fixture(db_session, tmp_path)
    first_revision = db_session.get(EditorialRevision, article.editorial_revision_id)
    operation_count = db_session.query(ProviderOperation).count()
    with pytest.raises(ValueError, match="topic update"):
        workflow_fixture(db_session, tmp_path, episode_id="next-broadcast")
    assert db_session.query(ArticleRevision).count() == 1
    assert db_session.query(ProviderOperation).count() == operation_count
    proposal = db_session.query(UpdateProposal).one()
    accept_proposal(db_session, proposal, operator_ref="fixture:topic-editor")
    second, _, _, _ = workflow_fixture(db_session, tmp_path, episode_id="next-broadcast")
    second_revision = db_session.get(EditorialRevision, second.editorial_revision_id)
    assert second_revision.topic_id == first_revision.topic_id


def test_operator_cli_connects_automatic_queries_to_private_draft(
    db_session, tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from btcedu.cli import cli
    from btcedu.models.article import ArticleRevision, EditorialDecision
    from btcedu.models.story_schema import StoryDocument
    from btcedu.services.search_service import SearchHit, SearchResponse

    settings = Settings(
        newsroom_enabled=True,
        newsroom_search_provider="brave",
        newsroom_media_provider="wikimedia_commons",
        brave_search_api_key="fixture-only",
        newsroom_data_dir=str(tmp_path / "private"),
        outputs_dir=str(tmp_path / "outputs"),
    )
    fetcher, _ = _fetcher(tmp_path)
    monkeypatch.setattr(
        "btcedu.services.document_fetcher.DocumentFetcher.from_settings", lambda settings: fetcher
    )
    queries = []

    class SearchFixture:
        name = "fixture"

        def search(self, query, *, language, count):
            queries.append(query)
            return SearchResponse(
                provider=self.name,
                query=query,
                hits=(SearchHit(title="Official report", url=SUPPORT_URL),),
                cost_usd=0,
            )

    results = {
        "extract_claims": [_claim_draft().model_dump(mode="json")],
        "evaluate_claim_evidence": [
            {
                "canonical_url": SUPPORT_URL,
                "relation": "supports",
                "passage": PASSAGE,
                "rationale": "Same factual count in retrieved document",
            }
        ],
        "draft_article": _draft(),
        "check_article_consistency": {"consistent": True, "issues": []},
    }
    monkeypatch.setattr(
        "btcedu.services.editorial_model.EditorialModel",
        lambda *args, **kwargs: lambda payload: ModelReply(results[payload["task"]], 0),
    )
    monkeypatch.setattr(
        "btcedu.services.search_service.BraveSearchProvider", lambda key: SearchFixture()
    )
    monkeypatch.setattr(
        "btcedu.services.commons_service.WikimediaCommonsProvider",
        lambda fetcher: FixtureCommonsProvider({"Berlin": (_candidate(),)}),
    )
    document = StoryDocument(
        episode_id="cli-transcript",
        broadcast_date="2026-09-09",
        source_attribution={},
        total_stories=1,
        total_duration_seconds=6,
        stories=[_story()],
    )
    path = tmp_path / "stories.json"
    path.write_text(document.model_dump_json())
    args = [
        "newsroom-draft",
        str(path),
        "--story-id",
        "story-1",
        "--provider",
        "openai",
        "--model",
        "fixture",
        "--max-call-cost",
        "0.01",
        "--image-subject",
        "Berlin",
    ]
    obj = {"settings": settings, "session_factory": lambda: db_session}
    assert CliRunner().invoke(cli, args, obj=obj).exit_code != 0
    assert queries == []
    result = CliRunner().invoke(cli, [*args, "--execute"], obj=obj)
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(queries) == 2
    assert "Widerspruch" in queries[1]
    assert db_session.query(ArticleRevision).one().status == "draft"
    assert db_session.query(EditorialDecision).count() == 0
