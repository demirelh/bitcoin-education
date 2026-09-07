"""WP-8C: what a render from before the byte-bound contract may still do.

WP-8B describes a manifest without ``render_inputs`` as one that "predates the
contract" and lets it pass the publish check, on the grounds that there is
nothing to compare. That sentence is only safe if something *else* stops such a
render from reaching YouTube, and this file is the proof that it does.

Backward compatibility here means the old artifacts stay readable and migrate
under control. It does not mean they stay publishable.

A legacy render is built the honest way: by rendering with the measurement
switched off, which produces exactly the manifest and provenance the pre-WP-8B
code wrote.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Column, DateTime, Integer, String, Table, create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.render_inputs import RENDER_INPUTS_KEY, is_recorded
from btcedu.core.renderer import render_is_current, render_video
from btcedu.db import Base
from btcedu.models import (  # noqa: F401 - registers the avatar tables
    avatar_audio_asset,
    avatar_job,
    avatar_job_audit,
    avatar_provider_breaker,
    avatar_regeneration,
)
from btcedu.models.episode import Episode, EpisodeStatus

EPISODE_ID = "ep_legacy"


@pytest.fixture
def db_engine():
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
    from btcedu.models.media_asset import Base as MediaBase

    if "prompt_versions" not in MediaBase.metadata.tables:
        Table(
            "prompt_versions",
            MediaBase.metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(64)),
            Column("version", Integer),
            Column("content_hash", String(64)),
            Column("is_default", Integer),
            Column("created_at", DateTime),
        )
    MediaBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    session = sessionmaker(bind=db_engine)()
    yield session
    session.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        render_font="NotoSans-Bold",
        dry_run=True,
        max_episode_cost_usd=10.0,
    )


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _seed(settings) -> Path:
    base = Path(settings.outputs_dir) / EPISODE_ID
    chapters = {
        "schema_version": "1.0",
        "episode_id": EPISODE_ID,
        "title": "Test",
        "total_chapters": 2,
        "estimated_duration_seconds": 60,
        "chapters": [
            {
                "chapter_id": f"ch0{index}",
                "title": f"Chapter {index}",
                "order": index,
                "narration": {
                    "text": f"Narration {index}.",
                    "word_count": 2,
                    "estimated_duration_seconds": 30,
                },
                "visual": {"type": "diagram", "description": "d", "image_prompt": "p"},
                "overlays": [],
                "transitions": {"in": "cut", "out": "cut"},
            }
            for index in (1, 2)
        ],
    }
    _write(base / "chapters.json", json.dumps(chapters).encode("utf-8"))
    images = {"episode_id": EPISODE_ID, "schema_version": "1.0", "images": []}
    tts = {"episode_id": EPISODE_ID, "schema_version": "1.0", "segments": []}
    for index in (1, 2):
        chapter = f"ch0{index}"
        _write(base / f"images/{chapter}.png", f"picture {index}".encode())
        _write(base / f"tts/{chapter}.mp3", f"narration {index}".encode())
        images["images"].append(
            {
                "chapter_id": chapter,
                "file_path": f"images/{chapter}.png",
                "generation_method": "template",
            }
        )
        tts["segments"].append(
            {
                "chapter_id": chapter,
                "file_path": f"tts/{chapter}.mp3",
                "duration_seconds": 30.0,
                "text_hash": f"sha256:{index}",
            }
        )
    _write(base / "images/manifest.json", json.dumps(images).encode("utf-8"))
    _write(base / "tts/manifest.json", json.dumps(tts).encode("utf-8"))
    return base


def _episode(db_session) -> Episode:
    episode = Episode(
        episode_id=EPISODE_ID,
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
        content_profile="bitcoin_podcast",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def _draft(base: Path) -> None:
    draft = base / "render" / "draft.mp4"
    if not draft.exists() or draft.stat().st_size == 0:
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(b"placeholder draft")


def _render_as_legacy(db_session, settings) -> tuple[Episode, Path]:
    """Render the way the code did before WP-8B: without measuring anything.

    Patching the measurement rather than editing the manifest afterwards is
    what makes this a real legacy artifact — the provenance hash is genuinely
    computed without the input digest, exactly as it was on disk before.
    """
    episode = _episode(db_session)
    base = _seed(settings)
    with patch("btcedu.core.renderer.render_inputs_block", return_value=None):
        render_video(db_session, EPISODE_ID, settings)
    _draft(base)
    return episode, base


def _render_current(db_session, settings) -> tuple[Episode, Path]:
    episode = _episode(db_session)
    base = _seed(settings)
    render_video(db_session, EPISODE_ID, settings)
    _draft(base)
    return episode, base


def _manifest(base: Path) -> dict:
    return json.loads((base / "render" / "render_manifest.json").read_text(encoding="utf-8"))


def _provenance(base: Path) -> dict:
    return json.loads(
        (base / "provenance" / "render_provenance.json").read_text(encoding="utf-8")
    )


class TestALegacyRenderIsRecognisable:
    def test_it_records_no_input_set_at_all(self, db_session, settings):
        _episode_obj, base = _render_as_legacy(db_session, settings)
        assert RENDER_INPUTS_KEY not in _manifest(base)
        assert not is_recorded(_manifest(base).get(RENDER_INPUTS_KEY))

    def test_its_provenance_carries_no_input_digest(self, db_session, settings):
        _episode_obj, base = _render_as_legacy(db_session, settings)
        assert not _provenance(base).get("input_digest")

    def test_a_current_render_differs_from_it(self, db_session, settings):
        """The two hashes must not collide, or none of the rest holds."""
        _episode_obj, base = _render_as_legacy(db_session, settings)
        legacy_hash = _provenance(base)["input_content_hash"]

        from btcedu.core.renderer import _current_render_content_hash

        assert _current_render_content_hash(db_session, EPISODE_ID, settings) != legacy_hash


class TestALegacyRenderIsStale:
    def test_the_new_content_hash_makes_it_stale(self, db_session, settings):
        _episode_obj, base = _render_as_legacy(db_session, settings)
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert not ok
        assert "changed" in reason

    def test_staleness_does_not_depend_on_the_files_having_moved(self, db_session, settings):
        """Nothing was touched after the render; it is stale on the contract."""
        _episode_obj, base = _render_as_legacy(db_session, settings)
        before = {
            path: path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and "render" not in path.parts
        }
        render_is_current(db_session, EPISODE_ID, settings)
        after = {
            path: path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and "render" not in path.parts
        }
        assert before == after


class TestALegacyRenderCannotBePublished:
    def _checks(self, db_session, settings, episode):
        from btcedu.core.publisher import _run_all_safety_checks

        return {
            check.name: check
            for check in _run_all_safety_checks(
                db_session, episode, settings, "T", "D", ["tag"]
            )
        }

    def test_the_render_validity_check_refuses_it(self, db_session, settings):
        episode, _base = _render_as_legacy(db_session, settings)
        checks = self._checks(db_session, settings, episode)
        assert not checks["render_valid"].passed
        assert "not current" in checks["render_valid"].message

    def test_the_lenient_input_check_is_never_the_last_word(self, db_session, settings):
        """``render_inputs`` passes it; ``render_valid`` still refuses."""
        episode, _base = _render_as_legacy(db_session, settings)
        checks = self._checks(db_session, settings, episode)
        assert checks["render_inputs"].passed
        assert "predates" in checks["render_inputs"].message
        assert not checks["render_valid"].passed

    def test_publish_refuses_the_episode(self, db_session, settings):
        from btcedu.core.publisher import publish_video

        episode, _base = _render_as_legacy(db_session, settings)
        episode.status = EpisodeStatus.APPROVED
        db_session.commit()
        with pytest.raises(ValueError) as excinfo:
            publish_video(db_session, EPISODE_ID, settings)
        assert "render_valid" in str(excinfo.value)
        assert "not current" in str(excinfo.value)
        db_session.refresh(episode)
        assert episode.status != EpisodeStatus.PUBLISHED

    def test_deleting_the_block_removes_the_evidence_not_the_requirement(
        self, db_session, settings
    ):
        """A hand-copied manifest without the block cannot launder a swap.

        Removing the block does make the input check go quiet -- there is
        genuinely nothing recorded left to compare. It buys nothing, because
        the content hash is recomputed from the *files*, not from the block,
        and the provenance still holds the number that was computed with the
        digest folded in. So an untouched episode stays valid and a swapped
        one is caught anyway.
        """
        episode, base = _render_current(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        (base / "render" / "render_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        checks = self._checks(db_session, settings, episode)
        assert checks["render_inputs"].passed  # nothing recorded to compare
        assert checks["render_valid"].passed  # and nothing was changed either

        (base / "images" / "ch01.png").write_bytes(b"a picture nobody reviewed")
        checks = self._checks(db_session, settings, episode)
        assert checks["render_inputs"].passed  # still quiet, still no record
        assert not checks["render_valid"].passed  # the hash is not fooled

    def test_forging_the_provenance_too_does_not_help(self, db_session, settings):
        """Both files rewritten to a pre-contract state: still refused.

        The recomputed hash always folds in the digest of the files that are
        on disk now, so a provenance written without it can never match.
        """
        episode, base = _render_current(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        (base / "render" / "render_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        provenance = _provenance(base)
        provenance.pop("input_digest", None)
        provenance.pop(RENDER_INPUTS_KEY, None)
        provenance["input_content_hash"] = "0" * 64
        (base / "provenance" / "render_provenance.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )
        checks = self._checks(db_session, settings, episode)
        assert not checks["render_valid"].passed


class TestAReRenderRestoresThePath:
    def test_re_rendering_records_the_set_and_clears_the_staleness(
        self, db_session, settings
    ):
        episode, base = _render_as_legacy(db_session, settings)
        assert not render_is_current(db_session, EPISODE_ID, settings)[0]

        episode.status = EpisodeStatus.TTS_DONE
        db_session.commit()
        render_video(db_session, EPISODE_ID, settings, force=True)
        _draft(base)

        assert is_recorded(_manifest(base)[RENDER_INPUTS_KEY])
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert ok, reason

    def test_the_old_approval_does_not_carry_over(self, db_session, settings):
        """A signature on the old draft is not a signature on the new one."""
        from btcedu.core.reviewer import has_approved_review

        episode, base = _render_as_legacy(db_session, settings)
        from btcedu.models.review import ReviewStatus, ReviewTask

        old_draft = (base / "render" / "draft.mp4").read_bytes()
        import hashlib

        task = ReviewTask(
            episode_id=EPISODE_ID,
            stage="render",
            status=ReviewStatus.APPROVED.value,
            artifact_paths=json.dumps([str(base / "render" / "draft.mp4")]),
            artifact_hash=hashlib.sha256(old_draft).hexdigest(),
        )
        db_session.add(task)
        db_session.commit()
        assert has_approved_review(db_session, EPISODE_ID, "render")

        episode.status = EpisodeStatus.TTS_DONE
        db_session.commit()
        render_video(db_session, EPISODE_ID, settings, force=True)
        (base / "render" / "draft.mp4").write_bytes(b"a freshly rendered draft")

        from btcedu.core.publisher import _check_artifact_integrity

        check = _check_artifact_integrity(db_session, episode, settings)
        assert not check.passed

    def test_review_gate_three_does_not_approve_a_stale_legacy_render(
        self, db_session, settings
    ):
        from btcedu.core.pipeline import _run_stage

        episode, _base = _render_as_legacy(db_session, settings)
        episode.status = EpisodeStatus.RENDERED
        db_session.commit()
        with (
            patch("btcedu.core.publisher.generate_metadata_suggestion", return_value=None),
            patch("btcedu.core.final_review.run_weather_video_checks") as checker,
        ):
            checker.return_value = type("R", (), {"publish_blocked": False, "findings": []})()
            _run_stage(db_session, episode, settings, "review_gate_3")
        db_session.refresh(episode)
        assert episode.status != EpisodeStatus.APPROVED


class TestARemoteResultMustHonourTheContract:
    def test_a_result_without_an_input_set_is_refused(self, db_session, settings):
        """The runner ran this repository's code against the shipped files.

        If this machine can measure the set, the runner had the same files and
        the same code and no reason to return nothing. Accepting it anyway
        would be a way to launder a render past the contract by downgrading
        the worker.
        """
        from btcedu.core.remote_render import _verify_returned_inputs
        from btcedu.core.render_inputs import RenderInputError

        episode, base = _render_current(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        with pytest.raises(RenderInputError, match="no byte-bound input set"):
            _verify_returned_inputs(
                settings, episode, base, manifest, base / "render" / "render_manifest.json"
            )

    def test_a_genuinely_unmeasurable_episode_is_still_accepted(self, db_session, settings):
        """Nothing to measure here either, so nothing was withheld."""
        from btcedu.core.remote_render import _verify_returned_inputs

        episode, base = _render_current(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        with patch("btcedu.core.remote_render._local_render_inputs", return_value=None):
            _verify_returned_inputs(
                settings, episode, base, manifest, base / "render" / "render_manifest.json"
            )

    def test_an_empty_set_is_refused_as_well(self, db_session, settings):
        from btcedu.core.remote_render import _verify_returned_inputs
        from btcedu.core.render_inputs import RenderInputError

        episode, base = _render_current(db_session, settings)
        manifest = _manifest(base)
        manifest[RENDER_INPUTS_KEY] = {"schema_version": "1.0", "entries": [], "digest": ""}
        with pytest.raises(RenderInputError, match="no byte-bound input set"):
            _verify_returned_inputs(
                settings, episode, base, manifest, base / "render" / "render_manifest.json"
            )
