"""WP-8B: the byte-bound input set at every real lifecycle boundary.

``test_render_inputs.py`` covers the primitives. This file walks an episode
through the actual stages — render, review gate 3, publish, remote render — and
tampers with one input at a time. The measure of the work is not that the
digests are computed; it is that each boundary in turn refuses to cross when
they disagree.
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
from btcedu.models import (  # noqa: F401
    avatar_audio_asset,
    avatar_job,
    avatar_job_audit,
    avatar_provider_breaker,
    avatar_regeneration,
)
from btcedu.models.episode import Episode, EpisodeStatus

EPISODE_ID = "ep_bind"


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
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS prompt_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash VARCHAR(64),
                    is_default INTEGER DEFAULT 0,
                    created_at DATETIME
                )"""
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


def _seed_inputs(settings) -> Path:
    """Chapters, pictures and narration for a two-chapter episode."""
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


def _render(db_session, settings, status=EpisodeStatus.TTS_DONE) -> tuple[Episode, Path]:
    episode = Episode(
        episode_id=EPISODE_ID,
        title="Test",
        url="https://example.com",
        status=status,
        pipeline_version=2,
        content_profile="bitcoin_podcast",
    )
    db_session.add(episode)
    db_session.commit()
    base = _seed_inputs(settings)
    _dry_render(db_session, EPISODE_ID, settings)
    return episode, base


def _dry_render(db_session, episode_id: str, settings) -> Path:
    """Render, then stand in for the file ffmpeg does not write in dry-run.

    The subject here is the recorded input set, not the encoder; every later
    boundary insists on a non-empty draft before it looks at anything else.
    """
    render_video(db_session, episode_id, settings)
    base = Path(settings.outputs_dir) / episode_id
    draft = base / "render" / "draft.mp4"
    if not draft.exists() or draft.stat().st_size == 0:
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_bytes(b"placeholder draft")
    return base


def _manifest(base: Path) -> dict:
    return json.loads((base / "render" / "render_manifest.json").read_text(encoding="utf-8"))


class TestTheRenderRecordsWhatItUsed:
    def test_the_manifest_carries_the_byte_bound_set(self, db_session, settings):
        _episode, base = _render(db_session, settings)
        block = _manifest(base)[RENDER_INPUTS_KEY]
        assert is_recorded(block)
        keys = {entry["key"] for entry in block["entries"]}
        assert {"media:ch01", "media:ch02", "tts:ch01", "tts:ch02"} <= keys
        assert all(len(entry["sha256"]) == 64 for entry in block["entries"])

    def test_every_entry_records_size_type_and_provenance(self, db_session, settings):
        _episode, base = _render(db_session, settings)
        for entry in _manifest(base)[RENDER_INPUTS_KEY]["entries"]:
            assert entry["size_bytes"] > 0
            assert entry["media_type"]
            assert entry["provenance"]
            assert entry["kind"]

    def test_the_provenance_records_the_same_digest(self, db_session, settings):
        _episode, base = _render(db_session, settings)
        provenance = json.loads(
            (base / "provenance" / "render_provenance.json").read_text(encoding="utf-8")
        )
        assert provenance["input_digest"] == _manifest(base)[RENDER_INPUTS_KEY]["digest"]
        assert provenance[RENDER_INPUTS_KEY]["entries"]

    def test_paths_are_recorded_relative_to_a_named_root(self, db_session, settings):
        """Absolute paths would not survive the trip to a GitHub runner."""
        _episode, base = _render(db_session, settings)
        for entry in _manifest(base)[RENDER_INPUTS_KEY]["entries"]:
            if entry["root"] != "system":
                assert not Path(entry["path"]).is_absolute()
                assert ".." not in Path(entry["path"]).parts


class TestTamperingIsDetectedByTheRender:
    def test_an_untouched_episode_reports_itself_current(self, db_session, settings):
        _render(db_session, settings)
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert ok, reason

    def test_swapping_a_picture_makes_the_render_stale(self, db_session, settings):
        """The hole WP-8B closes: this used to report 'current'."""
        _episode, base = _render(db_session, settings)
        (base / "images" / "ch01.png").write_bytes(b"a different picture!!")
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert not ok
        assert "changed" in reason

    def test_swapping_a_narration_take_makes_the_render_stale(self, db_session, settings):
        """``text_hash`` records what was asked for, not what came back."""
        _episode, base = _render(db_session, settings)
        (base / "tts" / "ch02.mp3").write_bytes(b"an entirely different take")
        ok, _reason = render_is_current(db_session, EPISODE_ID, settings)
        assert not ok

    def test_a_same_length_swap_is_still_detected(self, db_session, settings):
        _episode, base = _render(db_session, settings)
        original = (base / "images" / "ch01.png").read_bytes()
        (base / "images" / "ch01.png").write_bytes(b"Z" * len(original))
        ok, _reason = render_is_current(db_session, EPISODE_ID, settings)
        assert not ok

    def test_rewriting_a_file_with_identical_bytes_changes_nothing(self, db_session, settings):
        """Only the bytes count; a touched mtime is not a new picture."""
        _episode, base = _render(db_session, settings)
        picture = base / "images" / "ch01.png"
        picture.write_bytes(picture.read_bytes())
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert ok, reason

    def test_a_re_render_after_a_swap_records_the_new_bytes(self, db_session, settings):
        episode, base = _render(db_session, settings)
        before = _manifest(base)[RENDER_INPUTS_KEY]["digest"]
        (base / "images" / "ch01.png").write_bytes(b"the replacement picture")
        episode.status = EpisodeStatus.TTS_DONE
        db_session.commit()
        _dry_render(db_session, EPISODE_ID, settings)
        assert _manifest(base)[RENDER_INPUTS_KEY]["digest"] != before
        ok, reason = render_is_current(db_session, EPISODE_ID, settings)
        assert ok, reason


class TestPublishRefusesChangedInputs:
    def _check(self, db_session, settings, episode):
        from btcedu.core.publisher import _check_render_inputs

        return _check_render_inputs(db_session, episode, settings)

    def test_an_untouched_episode_passes_the_publish_check(self, db_session, settings):
        episode, _base = _render(db_session, settings)
        check = self._check(db_session, settings, episode)
        assert check.passed, check.message

    def test_a_swapped_picture_fails_the_publish_check(self, db_session, settings):
        episode, base = _render(db_session, settings)
        (base / "images" / "ch01.png").write_bytes(b"published with a different picture")
        check = self._check(db_session, settings, episode)
        assert not check.passed
        assert "changed since the render" in check.message

    def test_a_deleted_input_fails_the_publish_check(self, db_session, settings):
        episode, base = _render(db_session, settings)
        (base / "tts" / "ch01.mp3").unlink()
        check = self._check(db_session, settings, episode)
        assert not check.passed

    def test_a_legacy_render_passes_and_says_it_could_not_check(self, db_session, settings):
        episode, base = _render(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        (base / "render" / "render_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        check = self._check(db_session, settings, episode)
        assert check.passed
        assert "predates" in check.message

    def test_a_crashing_verification_fails_closed(self, db_session, settings):
        episode, _base = _render(db_session, settings)
        with patch(
            "btcedu.core.render_input_collector.verify_episode_inputs",
            side_effect=RuntimeError("disk on fire"),
        ):
            check = self._check(db_session, settings, episode)
        assert not check.passed
        assert "verification failed" in check.message

    def test_the_check_is_part_of_the_publish_gate(self, db_session, settings):
        from btcedu.core.publisher import _run_all_safety_checks

        episode, _base = _render(db_session, settings)
        checks = _run_all_safety_checks(db_session, episode, settings, "T", "D", ["tag"])
        assert any(check.name == "render_inputs" for check in checks)


class TestReviewGateThreeRefusesChangedInputs:
    def _run_gate(self, db_session, settings, episode):
        from btcedu.core.pipeline import _run_stage

        with (
            patch("btcedu.core.publisher.generate_metadata_suggestion", return_value=None),
            patch("btcedu.core.final_review.run_weather_video_checks") as checker,
        ):
            checker.return_value = type(
                "R", (), {"publish_blocked": False, "findings": []}
            )()
            return _run_stage(db_session, episode, settings, "review_gate_3")

    def test_an_untouched_episode_reaches_the_reviewer(self, db_session, settings):
        episode, _base = _render(db_session, settings, status=EpisodeStatus.TTS_DONE)
        episode.status = EpisodeStatus.RENDERED
        db_session.commit()
        result = self._run_gate(db_session, settings, episode)
        assert result.status in ("success", "review_pending")

    def test_a_swapped_picture_blocks_the_gate(self, db_session, settings):
        episode, base = _render(db_session, settings)
        episode.status = EpisodeStatus.RENDERED
        db_session.commit()
        (base / "images" / "ch02.png").write_bytes(b"not the reviewed picture")
        result = self._run_gate(db_session, settings, episode)
        assert result.status == "failed"
        assert "render inputs changed" in (result.detail or "")
        assert "[integrity]" in (result.error or "")

    def test_the_episode_is_not_marked_approved_when_inputs_changed(
        self, db_session, settings
    ):
        episode, base = _render(db_session, settings)
        episode.status = EpisodeStatus.RENDERED
        db_session.commit()
        (base / "tts" / "ch01.mp3").write_bytes(b"a different voice entirely")
        self._run_gate(db_session, settings, episode)
        db_session.refresh(episode)
        assert episode.status != EpisodeStatus.APPROVED

    def test_a_crashing_verification_blocks_the_gate(self, db_session, settings):
        episode, _base = _render(db_session, settings)
        episode.status = EpisodeStatus.RENDERED
        db_session.commit()
        with patch(
            "btcedu.core.render_input_collector.require_episode_inputs_intact",
            side_effect=RuntimeError("boom"),
        ):
            result = self._run_gate(db_session, settings, episode)
        assert result.status == "failed"
        assert "crashed" in (result.detail or "")


class TestRemoteRenderBoundaries:
    def test_packing_refuses_an_episode_with_a_missing_input(self, db_session, settings):
        from btcedu.core.remote_render import build_job_package

        _episode, base = _render(db_session, settings)
        (base / "images" / "ch01.png").unlink()
        with pytest.raises(RuntimeError, match="incomplete render"):
            build_job_package(
                db_session, EPISODE_ID, settings, Path(settings.outputs_dir) / "pkg"
            )

    def test_packing_records_the_shipped_digest(self, db_session, settings, tmp_path):
        from btcedu.core.remote_render import build_job_package

        _episode, base = _render(db_session, settings)
        archive = build_job_package(db_session, EPISODE_ID, settings, tmp_path / "pkg")
        assert archive.exists()
        import tarfile

        with tarfile.open(archive, "r:*") as tar:
            job = json.loads(tar.extractfile("job.json").read().decode("utf-8"))
        assert job["expected_input_digest"] == _manifest(base)[RENDER_INPUTS_KEY]["digest"]

    def test_take_back_refuses_a_result_built_from_other_bytes(self, db_session, settings):
        """A runner that rendered from something this machine never sent."""
        from btcedu.core.remote_render import _verify_returned_inputs
        from btcedu.core.render_inputs import RenderInputError

        episode, base = _render(db_session, settings)
        manifest = _manifest(base)
        episode_entries = [
            entry
            for entry in manifest[RENDER_INPUTS_KEY]["entries"]
            if entry["root"] != "system"
        ]
        assert episode_entries
        episode_entries[0]["sha256"] = "f" * 64
        with pytest.raises(RenderInputError, match="take-back refused"):
            _verify_returned_inputs(
                settings, episode, base, manifest, base / "render" / "render_manifest.json"
            )

    def test_take_back_accepts_a_matching_result(self, db_session, settings):
        from btcedu.core.remote_render import _verify_returned_inputs

        episode, base = _render(db_session, settings)
        manifest = _manifest(base)
        _verify_returned_inputs(
            settings, episode, base, manifest, base / "render" / "render_manifest.json"
        )

    def test_take_back_rebases_the_runners_font_onto_this_machine(
        self, db_session, settings, tmp_path
    ):
        """The runner's fonts-noto is not the Pi's, and never will be."""
        from btcedu.core.remote_render import _verify_returned_inputs

        episode, base = _render(db_session, settings)
        manifest_path = base / "render" / "render_manifest.json"
        manifest = _manifest(base)
        manifest[RENDER_INPUTS_KEY]["entries"].append(
            {
                "key": "font:overlay",
                "kind": "font",
                "root": "system",
                "path": "/usr/share/fonts/runner-only/Noto.ttf",
                "sha256": "b" * 64,
                "size_bytes": 10,
                "media_type": "font/ttf",
                "provenance": "render config font",
                "declared_sha256": "",
            }
        )
        manifest[RENDER_INPUTS_KEY]["count"] = len(manifest[RENDER_INPUTS_KEY]["entries"])
        _verify_returned_inputs(settings, episode, base, manifest, manifest_path)

        rewritten = _manifest(base)[RENDER_INPUTS_KEY]
        moved = {entry["path"] for entry in rewritten["remote_system_inputs"]}
        assert any(path.endswith("runner-only/Noto.ttf") for path in moved)
        assert not any(
            entry.get("path", "").endswith("runner-only/Noto.ttf")
            for entry in rewritten["entries"]
        )

    def test_a_result_without_an_input_set_is_refused(self, db_session, settings):
        """Tightened in WP-8C: see tests/test_render_input_legacy.py.

        A runner that returns nothing measurable, for files this machine can
        measure, is the one remaining way to launder a render past the
        contract by downgrading the worker.
        """
        from btcedu.core.remote_render import _verify_returned_inputs
        from btcedu.core.render_inputs import RenderInputError

        episode, base = _render(db_session, settings)
        manifest = _manifest(base)
        manifest.pop(RENDER_INPUTS_KEY)
        with pytest.raises(RenderInputError, match="no byte-bound input set"):
            _verify_returned_inputs(
                settings, episode, base, manifest, base / "render" / "render_manifest.json"
            )


class TestScopedInvalidation:
    def test_a_change_to_one_episode_does_not_touch_another(self, db_session, settings):
        """Invalidation follows the bytes, not the machine."""
        episode_one, base_one = _render(db_session, settings)
        assert episode_one is not None

        other_id = "ep_other"
        other = Episode(
            episode_id=other_id,
            title="Other",
            url="https://example.com/2",
            status=EpisodeStatus.TTS_DONE,
            pipeline_version=2,
            content_profile="bitcoin_podcast",
        )
        db_session.add(other)
        db_session.commit()

        other_base = Path(settings.outputs_dir) / other_id
        source = Path(settings.outputs_dir) / EPISODE_ID
        for relative in ("chapters.json", "images/manifest.json", "tts/manifest.json"):
            _write(other_base / relative, (source / relative).read_bytes())
        for relative in ("images/ch01.png", "images/ch02.png", "tts/ch01.mp3", "tts/ch02.mp3"):
            _write(other_base / relative, (source / relative).read_bytes())
        _dry_render(db_session, other_id, settings)

        (base_one / "images" / "ch01.png").write_bytes(b"changed in the first episode only")
        assert not render_is_current(db_session, EPISODE_ID, settings)[0]
        assert render_is_current(db_session, other_id, settings)[0]

    def test_no_provider_work_is_triggered_by_a_changed_input(self, db_session, settings):
        """A repainted picture must never look like a reason to buy a clip."""
        from btcedu.core.avatar_jobs import episode_avatar_cost

        episode, base = _render(db_session, settings)
        before = episode_avatar_cost(db_session, EPISODE_ID)
        (base / "images" / "ch01.png").write_bytes(b"a replacement")
        render_is_current(db_session, EPISODE_ID, settings)
        from btcedu.core.publisher import _check_render_inputs

        _check_render_inputs(db_session, episode, settings)
        assert episode_avatar_cost(db_session, EPISODE_ID) == before
