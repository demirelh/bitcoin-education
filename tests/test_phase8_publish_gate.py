"""Phase 8 requirement E: hardened publish gate for auto_publish=False profiles.

tagesschau (auto_publish=False) must require QA GREEN, no unresolved critical
findings, a current approved-narration hash, a validated render, budget, and an
explicit artifact-bound final-publish approval. Intermediate (RG3) render
approval must never be sufficient.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.publisher import _publish_artifact_paths, publish_video, request_publish_review
from btcedu.core.reviewer import approve_review
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.profiles import get_registry, reset_registry


@pytest.fixture(autouse=True)
def _leave_real_profiles_loaded():
    """Ensure the profile-registry singleton holds real profiles after each test
    so a local ``reset_registry()`` never leaves it empty for later test files."""
    yield
    reset_registry()
    get_registry(Settings())


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    reset_registry()  # ensure real profiles (incl. tagesschau auto_publish:false) load
    s = Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        dry_run=True,
        max_episode_cost_usd=10.0,
        youtube_default_privacy="unlisted",
        youtube_credentials_path=str(tmp_path / ".yt.json"),
    )
    # Force-load real profiles now so the singleton is never left empty for
    # later tests (the profile registry is a process-wide singleton).
    get_registry(s)
    return s


_NARRATION = "Almanya haberleri. Bugun onemli gelismeler yasandi."


def _hash_paths(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        fp = Path(p)
        if fp.exists():
            h.update(fp.read_bytes())
    return h.hexdigest()


def _setup(db_session, settings, *, decision="green", critical=False, narration_current=True):
    """Create an APPROVED tagesschau episode ready to publish, minus the publish review."""
    ep_dir = Path(settings.outputs_dir) / "ep_news"
    (ep_dir / "render").mkdir(parents=True)
    (ep_dir / "provenance").mkdir(parents=True)

    # Canonical narration source + chapters
    (ep_dir / "script.adapted.tr.md").write_text(_NARRATION, encoding="utf-8")
    chapters = {
        "schema_version": "1.0",
        "episode_id": "ep_news",
        "title": "tagesschau",
        "total_chapters": 1,
        "estimated_duration_seconds": 20,
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Haber",
                "order": 1,
                "narration": {
                    "text": _NARRATION,
                    "word_count": len(_NARRATION.split()),
                    "estimated_duration_seconds": 20,
                },
                "visual": {"type": "title_card", "description": "Haber"},
                "overlays": [],
                "transitions": {"in": "fade", "out": "cut"},
            }
        ],
    }
    (ep_dir / "chapters.json").write_text(json.dumps(chapters), encoding="utf-8")

    # Persisted, complete YouTube metadata
    (ep_dir / "render" / "youtube_metadata.json").write_text(
        json.dumps({"title": "tagesschau Türkçe", "description": "aciklama", "tags": ["haber"]}),
        encoding="utf-8",
    )

    # Final render draft
    draft = ep_dir / "render" / "draft.mp4"
    draft.write_bytes(b"final video bytes")

    from btcedu.core.qa_reviewer import narration_sha256

    approved_hash = narration_sha256(settings, "ep_news")
    if not narration_current:
        approved_hash = "0" * 64  # simulate narration drift since QA approval

    gate = {
        "episode_id": "ep_news",
        "decision": decision,
        "status": decision,
        "narration_sha256": approved_hash,
        "narration_approved": decision == "green",
        "findings": (
            [
                {
                    "finding_id": "qa-0001",
                    "severity": "critical",
                    "status": "open",
                    "category": "casualty_claim",
                }
            ]
            if critical
            else []
        ),
    }
    (ep_dir / "translation_quality_gate.json").write_text(json.dumps(gate), encoding="utf-8")

    episode = Episode(
        episode_id="ep_news",
        source="tagesschau_rss",
        title="tagesschau 20:00 Uhr",
        url="https://x",
        status=EpisodeStatus.APPROVED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()

    # Current render inputs and provenance.
    (ep_dir / "images").mkdir()
    (ep_dir / "tts").mkdir()
    (ep_dir / "images" / "manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "images/ch01.png",
                        "generation_method": "template",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ep_dir / "tts" / "manifest.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "tts/ch01.mp3",
                        "duration_seconds": 20,
                        "text_hash": "tts-current",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ep_dir / "render" / "render_manifest.json").write_text(
        json.dumps({"segments": [{"chapter_id": "ch01"}]}),
        encoding="utf-8",
    )
    from btcedu.core.renderer import _current_render_content_hash

    render_hash = _current_render_content_hash(db_session, episode.episode_id, settings)
    (ep_dir / "provenance" / "render_provenance.json").write_text(
        json.dumps({"input_content_hash": render_hash}),
        encoding="utf-8",
    )

    # Intermediate Review Gate 3 (render) approval — bound to the draft.
    render_task = ReviewTask(
        episode_id="ep_news",
        stage="render",
        status=ReviewStatus.APPROVED.value,
        artifact_paths=json.dumps([str(draft)]),
        artifact_hash=_hash_paths([str(draft)]),
    )
    db_session.add(render_task)
    db_session.commit()
    return episode


def test_pipeline_never_auto_uploads_when_profile_disables_auto_publish(db_session, settings):
    from btcedu.core.pipeline import _run_stage

    episode = _setup(db_session, settings)
    task = request_publish_review(db_session, episode.episode_id, settings)
    approve_review(db_session, task.id)

    with patch("btcedu.core.publisher.publish_video") as publish_mock:
        result = _run_stage(db_session, episode, settings, "publish")

    assert result.status == "skipped"
    assert "explicit manual publish required" in result.detail
    publish_mock.assert_not_called()


def _approve_publish_review(db_session, settings):
    task = request_publish_review(db_session, "ep_news", settings)
    approve_review(db_session, task.id, notes="final publish approved")
    return task


# ---------------------------------------------------------------------------


def test_tagesschau_profile_requires_manual_publish(db_session, settings):
    from btcedu.core.publisher import _requires_manual_publish_review

    episode = _setup(db_session, settings)
    assert _requires_manual_publish_review(episode, settings) is True


def test_publish_blocks_without_final_publish_approval(db_session, settings):
    _setup(db_session, settings)
    # Only the intermediate render approval exists — must NOT be enough.
    with pytest.raises(ValueError) as exc:
        publish_video(db_session, "ep_news", settings)
    assert "publish" in str(exc.value).lower()


def test_publish_succeeds_with_artifact_bound_final_approval(db_session, settings):
    _setup(db_session, settings)
    _approve_publish_review(db_session, settings)

    result = publish_video(db_session, "ep_news", settings)
    assert result.dry_run is True
    assert result.error is None
    assert "manual_publish_approval" in result.safety_checks


def test_publish_blocks_on_unresolved_critical_finding(db_session, settings):
    _setup(db_session, settings, decision="green", critical=True)
    _approve_publish_review(db_session, settings)
    with pytest.raises(ValueError, match="safety checks failed"):
        publish_video(db_session, "ep_news", settings)


def test_publish_blocks_on_narration_hash_mismatch(db_session, settings):
    _setup(db_session, settings, narration_current=False)
    _approve_publish_review(db_session, settings)
    with pytest.raises(ValueError, match="safety checks failed"):
        publish_video(db_session, "ep_news", settings)


def test_publish_blocks_when_qa_gate_not_green(db_session, settings):
    _setup(db_session, settings, decision="red")
    _approve_publish_review(db_session, settings)
    with pytest.raises(ValueError, match="safety checks failed"):
        publish_video(db_session, "ep_news", settings)


def test_publish_blocks_when_required_qa_gate_is_missing(db_session, settings):
    episode = _setup(db_session, settings)
    (Path(settings.outputs_dir) / episode.episode_id / "translation_quality_gate.json").unlink()

    with pytest.raises(ValueError, match="Required Translation-QA gate is missing"):
        publish_video(db_session, episode.episode_id, settings)


def test_publish_blocks_when_approved_narration_hash_is_missing(db_session, settings):
    episode = _setup(db_session, settings)
    gate_path = Path(settings.outputs_dir) / episode.episode_id / "translation_quality_gate.json"
    gate = json.loads(gate_path.read_text())
    gate.pop("narration_sha256")
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    with pytest.raises(ValueError, match="no approved narration hash"):
        publish_video(db_session, episode.episode_id, settings)


def test_publish_blocks_when_render_provenance_is_missing(db_session, settings):
    episode = _setup(db_session, settings)
    provenance = (
        Path(settings.outputs_dir) / episode.episode_id / "provenance" / "render_provenance.json"
    )
    provenance.unlink()

    with pytest.raises(ValueError, match="render manifest/provenance missing"):
        publish_video(db_session, episode.episode_id, settings)


def test_final_publish_approval_invalidated_when_render_changes(db_session, settings):
    """An approval bound to the old render must not authorize a re-rendered video."""
    _setup(db_session, settings)
    _approve_publish_review(db_session, settings)
    # Re-render: draft bytes change -> the artifact-bound approval is now stale.
    draft = Path(settings.outputs_dir) / "ep_news" / "render" / "draft.mp4"
    draft.write_bytes(b"a different, newer render")
    with pytest.raises(ValueError) as exc:
        publish_video(db_session, "ep_news", settings)
    assert "publish" in str(exc.value).lower()


def test_final_publish_approval_invalidated_when_metadata_changes(db_session, settings):
    episode = _setup(db_session, settings)
    _approve_publish_review(db_session, settings)
    metadata = Path(settings.outputs_dir) / episode.episode_id / "render" / "youtube_metadata.json"
    metadata.write_text(
        json.dumps({"title": "Changed", "description": "changed", "tags": ["changed"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manual_publish_approval"):
        publish_video(db_session, episode.episode_id, settings)


def test_final_publish_approval_invalidated_by_privacy_override(db_session, settings):
    episode = _setup(db_session, settings)
    _approve_publish_review(db_session, settings)

    with pytest.raises(ValueError, match="manual_publish_approval"):
        publish_video(db_session, episode.episode_id, settings, privacy="public")


def test_unknown_profile_fails_publish_closed(db_session, settings):
    episode = _setup(db_session, settings)
    episode.content_profile = "missing_profile"
    db_session.commit()

    with pytest.raises((ValueError, KeyError), match="Profile"):
        publish_video(db_session, episode.episode_id, settings)


def test_publish_artifact_paths_bind_render_gate_and_narration(db_session, settings):
    _setup(db_session, settings)
    paths = _publish_artifact_paths("ep_news", settings)
    names = {Path(p).name for p in paths}
    assert "draft.mp4" in names
    assert "translation_quality_gate.json" in names
    assert "script.adapted.tr.md" in names  # canonical narration source


def test_legacy_auto_publish_profile_does_not_require_publish_review(db_session, settings):
    """bitcoin_podcast (auto_publish=True) keeps its legacy publish path."""
    from btcedu.core.publisher import _requires_manual_publish_review

    ep_dir = Path(settings.outputs_dir) / "ep_pod"
    (ep_dir / "render").mkdir(parents=True)
    (ep_dir / "render" / "draft.mp4").write_bytes(b"v")
    episode = Episode(
        episode_id="ep_pod",
        source="youtube_rss",
        title="Podcast",
        url="https://x",
        status=EpisodeStatus.APPROVED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()
    assert _requires_manual_publish_review(episode, settings) is False


def test_yellow_gate_narration_is_covered_by_the_artifact_bound_review(db_session, settings):
    """A waved-through gate stores no hash; the QA review carries the narration.

    Without this the override that the QA check honours would still be vetoed
    by the narration check, and no yellow episode could ever be published.
    """
    from btcedu.core.publisher import _run_all_safety_checks
    from btcedu.core.qa_reviewer import gate_review_artifacts
    from btcedu.core.reviewer import approve_review, create_review_task

    episode = _setup(db_session, settings, decision="yellow")
    gate_path = Path(settings.outputs_dir) / episode.episode_id / "translation_quality_gate.json"
    gate = json.loads(gate_path.read_text())
    gate["narration_sha256"] = None
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    artifacts = gate_review_artifacts(settings, episode.episode_id)
    task = create_review_task(db_session, episode.episode_id, "translation_qa", artifacts)
    approve_review(db_session, task.id)

    checks = {
        c.name: c for c in _run_all_safety_checks(db_session, episode, settings, "t", "d", ["tag"])
    }
    assert checks["qa_gate"].passed
    assert checks["narration_current"].passed


def test_yellow_gate_without_qa_review_still_blocks_publish(db_session, settings):
    """No hash and no approved review means the narration is unvouched for."""
    from btcedu.core.publisher import _run_all_safety_checks

    episode = _setup(db_session, settings, decision="yellow")
    gate_path = Path(settings.outputs_dir) / episode.episode_id / "translation_quality_gate.json"
    gate = json.loads(gate_path.read_text())
    gate["narration_sha256"] = None
    gate_path.write_text(json.dumps(gate), encoding="utf-8")

    checks = {
        c.name: c for c in _run_all_safety_checks(db_session, episode, settings, "t", "d", ["tag"])
    }
    assert not checks["narration_current"].passed
