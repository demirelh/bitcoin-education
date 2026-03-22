"""Tests for anchor generator stage."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.anchor_generator import generate_anchors
from btcedu.db import Base
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun
from btcedu.models.media_asset import Base as MediaBase


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    MediaBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Session_ = sessionmaker(bind=engine)
    sess = Session_()
    yield sess
    sess.close()


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        anchor_enabled=True,
        did_api_key="test-key",
        did_source_image_path=str(tmp_path / "anchor.png"),
        dry_run=True,
        pipeline_version=2,
        max_episode_cost_usd=15.0,
    )
    return s


@pytest.fixture
def episode(session):
    ep = Episode(
        episode_id="ep_test_001",
        title="Test Episode",
        url="https://example.com/test",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    session.add(ep)
    session.commit()
    return ep


def _create_chapters_json(outputs_dir: Path, episode_id: str, talking_head: bool = True):
    """Create a valid chapters.json with optional TALKING_HEAD chapters."""
    chapters_dir = outputs_dir / episode_id
    chapters_dir.mkdir(parents=True, exist_ok=True)

    visual_type_1 = "talking_head" if talking_head else "b_roll"
    visual_1 = {"type": visual_type_1, "description": "News anchor"}
    if visual_type_1 == "b_roll":
        visual_1["image_prompt"] = "A news anchor desk"

    chapters = [
        {
            "chapter_id": "ch_01",
            "title": "Introduction",
            "order": 1,
            "narration": {
                "text": "Welcome to today's news broadcast with the latest updates.",
                "word_count": 10,
                "estimated_duration_seconds": 4,
            },
            "visual": visual_1,
            "overlays": [],
            "transitions": {"in": "fade", "out": "fade"},
        },
        {
            "chapter_id": "ch_02",
            "title": "Main Story",
            "order": 2,
            "narration": {
                "text": "In today's main story, global markets are moving significantly today.",
                "word_count": 11,
                "estimated_duration_seconds": 5,
            },
            "visual": {
                "type": "b_roll",
                "description": "Stock chart",
                "image_prompt": "A stock market chart showing upward trend",
            },
            "overlays": [],
            "transitions": {"in": "fade", "out": "cut"},
        },
    ]

    chapters_data = {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "title": "Test Episode",
        "total_chapters": 2,
        "estimated_duration_seconds": 9,
        "chapters": chapters,
    }
    (chapters_dir / "chapters.json").write_text(
        json.dumps(chapters_data, indent=2), encoding="utf-8"
    )
    return chapters_dir


def _create_tts_manifest(outputs_dir: Path, episode_id: str):
    """Create a valid TTS manifest."""
    tts_dir = outputs_dir / episode_id / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    for ch_id in ["ch_01", "ch_02"]:
        (tts_dir / f"{ch_id}.mp3").write_bytes(b"\xff\xfb\x90" + b"\x00" * 100)

    manifest = {
        "episode_id": episode_id,
        "segments": [
            {
                "chapter_id": "ch_01",
                "file_path": "tts/ch_01.mp3",
                "duration_seconds": 3.0,
                "text_hash": "abc123",
            },
            {
                "chapter_id": "ch_02",
                "file_path": "tts/ch_02.mp3",
                "duration_seconds": 5.0,
                "text_hash": "def456",
            },
        ],
    }
    (tts_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


class TestGenerateAnchorsDisabled:
    """Tests when anchor is disabled."""

    def test_disabled_advances_status(self, session, episode, tmp_path):
        settings = Settings(
            outputs_dir=str(tmp_path / "outputs"),
            transcripts_dir=str(tmp_path / "transcripts"),
            anchor_enabled=False,
            pipeline_version=2,
        )
        result = generate_anchors(session, "ep_test_001", settings)
        assert result.skipped is True

        session.refresh(episode)
        assert episode.status == EpisodeStatus.ANCHOR_GENERATED

    def test_disabled_already_generated(self, session, tmp_path):
        ep = Episode(
            episode_id="ep_test_002",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.ANCHOR_GENERATED,
            pipeline_version=2,
        )
        session.add(ep)
        session.commit()

        settings = Settings(
            outputs_dir=str(tmp_path / "outputs"),
            transcripts_dir=str(tmp_path / "transcripts"),
            anchor_enabled=False,
            pipeline_version=2,
        )
        result = generate_anchors(session, "ep_test_002", settings)
        assert result.skipped is True


class TestGenerateAnchorsNoTalkingHead:
    """Tests when no TALKING_HEAD chapters exist."""

    def test_no_talking_head_skips(self, session, episode, settings, tmp_path):
        _create_chapters_json(
            Path(settings.outputs_dir), "ep_test_001", talking_head=False
        )
        result = generate_anchors(session, "ep_test_001", settings)
        assert result.skipped is True
        session.refresh(episode)
        assert episode.status == EpisodeStatus.ANCHOR_GENERATED


class TestGenerateAnchorsNormal:
    """Tests for normal anchor generation."""

    def test_generates_anchor_for_talking_head(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")

        # Create source image
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        result = generate_anchors(session, "ep_test_001", settings)

        assert result.skipped is False
        assert result.segment_count == 1  # Only ch_01 is TALKING_HEAD
        assert result.cost_usd == 0.0  # Dry-run
        assert result.manifest_path.exists()

        # Check manifest
        manifest = json.loads(result.manifest_path.read_text())
        assert len(manifest["segments"]) == 1
        assert manifest["segments"][0]["chapter_id"] == "ch_01"

        # Check episode status
        session.refresh(episode)
        assert episode.status == EpisodeStatus.ANCHOR_GENERATED

    def test_creates_pipeline_run(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        generate_anchors(session, "ep_test_001", settings)

        runs = session.query(PipelineRun).filter_by(
            episode_id="ep_test_001", stage="anchorgen"
        ).all()
        assert len(runs) == 1
        assert runs[0].status == "success"

    def test_creates_content_artifact(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        generate_anchors(session, "ep_test_001", settings)

        artifacts = session.query(ContentArtifact).filter_by(
            episode_id="ep_test_001", artifact_type="anchor_video"
        ).all()
        assert len(artifacts) == 1

    def test_writes_provenance(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        result = generate_anchors(session, "ep_test_001", settings)

        assert result.provenance_path.exists()
        provenance = json.loads(result.provenance_path.read_text())
        assert provenance["stage"] == "anchorgen"
        assert provenance["segment_count"] == 1


class TestGenerateAnchorsIdempotency:
    """Tests for idempotency."""

    def test_second_run_skips(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        # First run
        result1 = generate_anchors(session, "ep_test_001", settings)
        assert result1.skipped is False

        # Second run
        result2 = generate_anchors(session, "ep_test_001", settings)
        assert result2.skipped is True

    def test_force_regenerates(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        # First run
        generate_anchors(session, "ep_test_001", settings)

        # Force re-run
        result = generate_anchors(session, "ep_test_001", settings, force=True)
        assert result.skipped is False


class TestGenerateAnchorsErrors:
    """Tests for error handling."""

    def test_episode_not_found(self, session, settings):
        with pytest.raises(ValueError, match="Episode not found"):
            generate_anchors(session, "nonexistent", settings)

    def test_v1_pipeline_rejected(self, session, settings):
        ep = Episode(
            episode_id="ep_v1",
            title="V1 Episode",
            url="https://example.com",
            status=EpisodeStatus.TTS_DONE,
            pipeline_version=1,
        )
        session.add(ep)
        session.commit()

        with pytest.raises(ValueError, match="v1 pipeline"):
            generate_anchors(session, "ep_v1", settings)

    def test_wrong_status_rejected(self, session, settings):
        ep = Episode(
            episode_id="ep_wrong",
            title="Wrong Status",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
        )
        session.add(ep)
        session.commit()

        with pytest.raises(ValueError, match="expected 'tts_done'"):
            generate_anchors(session, "ep_wrong", settings)

    def test_cost_guard(self, session, episode, settings):
        outputs_dir = Path(settings.outputs_dir)
        _create_chapters_json(outputs_dir, "ep_test_001", talking_head=True)
        _create_tts_manifest(outputs_dir, "ep_test_001")
        Path(settings.did_source_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(settings.did_source_image_path).write_bytes(b"\x89PNG" + b"\x00" * 100)

        # Add a huge existing cost
        run = PipelineRun(
            episode_id="ep_test_001",
            stage="tts",
            status="success",
            estimated_cost_usd=14.5,
        )
        session.add(run)
        session.commit()

        settings.max_episode_cost_usd = 15.0
        # The existing cost (14.5) + anchor cost should exceed limit
        # But DryRun has cost 0, so it won't trigger. Test with a lower limit.
        settings.max_episode_cost_usd = 14.0

        with pytest.raises(RuntimeError, match="cost limit exceeded"):
            generate_anchors(session, "ep_test_001", settings)
