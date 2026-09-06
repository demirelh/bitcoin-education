"""The gate between anchorgen and render.

The rule this file protects is short: with the avatar active, an unapproved
presenter never reaches a rendered bulletin, and an approval never travels
from one set of clips to another.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core import anchor_review
from btcedu.core.anchor_review import approve, reject
from btcedu.core.avatar_jobs import reserve_scene
from btcedu.core.pipeline import _V2_STAGES, _run_stage
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    ROLE_REPORTER,
    TEMPLATE_ANCHOR,
    TEMPLATE_REPORTER,
    VISUAL_MODE_FULLSCREEN,
    VISUAL_MODE_STUDIO,
    Scene,
    write_scene_plan,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJobStatus
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.presenter_assignment import PresenterAssignment
from btcedu.profiles import reset_registry

EPISODE_ID = "ep_gate_001"
LOOK_ID = "heygen_look_alpha"


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def no_readiness_blockers():
    with patch.object(anchor_review, "_rights_and_readiness_blockers", return_value=[]):
        yield


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        anchor_enabled=True,
        heygen_api_key="test-key",
        pipeline_version=2,
    )


@pytest.fixture
def episode(session):
    ep = Episode(
        episode_id=EPISODE_ID,
        title="Tagesschau",
        url="https://example.com/t",
        status=EpisodeStatus.ANCHOR_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    session.add(ep)
    session.add(
        PresenterAssignment(
            episode_id=EPISODE_ID,
            provider="heygen",
            engine="avatar_iii",
            avatar_type="digital_twin",
            avatar_look_id=LOOK_ID,
            look_name="look_02",
            strategy="least_recently_used",
            config_version=1,
            status="assigned",
            content_hash="assignment-hash",
            provenance_path="",
            cost_per_second_usd=0.0167,
        )
    )
    session.commit()
    return ep


def _scene(scene_id, chapter_id, order, role):
    studio = role == ROLE_ANCHOR
    return Scene(
        scene_id=scene_id,
        chapter_id=chapter_id,
        order=order,
        beat_index=0,
        speaker_role=role,
        purpose="report",
        segment_indices=[0],
        text_hash=f"text-{scene_id}",
        audio_file=None,
        expected_duration_seconds=6.0,
        visual_mode=VISUAL_MODE_STUDIO if studio else VISUAL_MODE_FULLSCREEN,
        template_id=TEMPLATE_ANCHOR if studio else TEMPLATE_REPORTER,
        background_asset=None if studio else "images/ch_01.jpg",
        background_asset_type="none" if studio else "image",
        display_zone_id="monitor" if studio else None,
        display_fit_mode="contain",
        focus_point=None,
        transition_in="cut",
        overlays={},
        needs_avatar=studio,
    )


def _seed(session, settings, *, scenes=None, anchor_ids=("sc_001", "sc_003")):
    outputs = Path(settings.outputs_dir) / EPISODE_ID
    outputs.mkdir(parents=True, exist_ok=True)
    write_scene_plan(
        outputs / "scene_plan.json",
        EPISODE_ID,
        scenes
        if scenes is not None
        else [
            _scene("sc_001", "ch_01", 1, ROLE_ANCHOR),
            _scene("sc_002", "ch_01", 2, ROLE_REPORTER),
            _scene("sc_003", "ch_02", 3, ROLE_ANCHOR),
        ],
        presenter_look_id=LOOK_ID,
        presenter_assignment_id=1,
    )
    anchor_dir = outputs / "anchor"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for scene_id in anchor_ids:
        clip = anchor_dir / f"{scene_id}.webm"
        clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
        entries.append(
            {
                "scene_id": scene_id,
                "chapter_id": "ch_01",
                "speaker_role": ROLE_ANCHOR,
                "template_id": TEMPLATE_ANCHOR,
                "avatar_look_id": LOOK_ID,
                "audio_hash": "a1",
                "content_hash": f"hash-{scene_id}",
                "provider": "heygen",
                "provider_job_id": f"heygen_{scene_id}",
                "status": "completed",
                "video_path": f"anchor/{scene_id}.webm",
                "duration_seconds": 6.0,
                "cost_usd": 0.1002,
                "mime_type": "video/webm",
            }
        )
        job = reserve_scene(
            session,
            episode_id=EPISODE_ID,
            scene_id=scene_id,
            chapter_id="ch_01",
            content_hash=f"hash-{scene_id}",
            provider="heygen",
            engine="avatar_iii",
            avatar_look_id=LOOK_ID,
            output_format="webm",
            estimated_cost_usd=0.1002,
        ).job
        job.status = AvatarJobStatus.COMPLETED.value
        job.cost_usd = 0.1002
        job.duration_seconds = 6.0
    session.commit()
    (anchor_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "episode_id": EPISODE_ID,
                "anchor_provider": "heygen",
                "engine": "avatar_iii",
                "avatar_look_id": LOOK_ID,
                "output_format": "webm",
                "scenes": entries,
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    return outputs


class TestTheStageIsWiredIn:
    def test_it_sits_between_anchorgen_and_render(self):
        names = [name for name, _ in _V2_STAGES]
        assert names.index("anchorgen") < names.index("review_gate_anchor")
        assert names.index("review_gate_anchor") < names.index("render")

    def test_it_requires_the_v2_pipeline(self, session, settings, episode):
        episode.pipeline_version = 1
        session.commit()
        with pytest.raises(ValueError, match="requires v2"):
            _run_stage(session, episode, settings, "review_gate_anchor")


class TestWhenTheGateStandsAside:
    def test_a_deployment_without_avatars_walks_through(self, session, settings, episode):
        settings.anchor_enabled = False
        _seed(session, settings)
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "skipped"

    def test_another_profile_walks_through(self, session, settings, episode):
        episode.content_profile = "bitcoin_podcast"
        session.commit()
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "skipped"

    def test_an_episode_without_presenter_scenes_walks_through(
        self, session, settings, episode
    ):
        _seed(
            session,
            settings,
            scenes=[_scene("sc_002", "ch_01", 1, ROLE_REPORTER)],
            anchor_ids=(),
        )
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "skipped"
        assert "no presenter scenes" in result.detail


class TestWhenTheGateHolds:
    def test_it_opens_a_review_and_waits(self, session, settings, episode):
        _seed(session, settings)
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "review_pending"

        from btcedu.core.anchor_review import latest_task

        assert latest_task(session, EPISODE_ID) is not None

    def test_a_second_run_does_not_pile_up_tasks(self, session, settings, episode):
        _seed(session, settings)
        _run_stage(session, episode, settings, "review_gate_anchor")
        _run_stage(session, episode, settings, "review_gate_anchor")

        from btcedu.models.review import ReviewTask

        assert session.query(ReviewTask).filter_by(stage="anchor").count() == 1

    def test_approval_opens_the_gate(self, session, settings, episode):
        _seed(session, settings)
        _run_stage(session, episode, settings, "review_gate_anchor")
        approve(session, EPISODE_ID, settings, notes="ok")
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "success"

    def test_rejection_stops_the_pipeline(self, session, settings, episode):
        _seed(session, settings)
        _run_stage(session, episode, settings, "review_gate_anchor")
        reject(session, EPISODE_ID, settings, notes="Blickrichtung falsch")
        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "failed"
        assert "rejected" in result.error

    def test_a_replaced_clip_reopens_the_gate(self, session, settings, episode):
        outputs = _seed(session, settings)
        _run_stage(session, episode, settings, "review_gate_anchor")
        approve(session, EPISODE_ID, settings, notes="ok")
        assert _run_stage(session, episode, settings, "review_gate_anchor").status == "success"

        manifest = outputs / "anchor" / "manifest.json"
        data = json.loads(manifest.read_text())
        data["scenes"][0]["content_hash"] = "hash-different"
        manifest.write_text(json.dumps(data))

        result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "review_pending"

    def test_auto_approve_reviews_does_not_open_this_gate(self, session, settings, episode):
        """The editorial gates may run unattended; the face on air may not."""
        _seed(session, settings)
        with patch(
            "btcedu.core.pipeline._profile_pipeline_flags", return_value=(True, False)
        ):
            result = _run_stage(session, episode, settings, "review_gate_anchor")
        assert result.status == "review_pending"


class TestTheRendererIsFailClosed:
    def test_it_refuses_unapproved_presenter_clips(self, session, settings, episode):
        from btcedu.core.renderer import render_video

        _seed(session, settings)
        with pytest.raises(ValueError, match="avatar review"):
            render_video(session, EPISODE_ID, settings)

    def test_it_proceeds_once_approved(self, session, settings, episode):
        """Approval must be the only thing standing between here and ffmpeg."""
        from btcedu.core import renderer

        _seed(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        # Getting past the guard is all this test claims; the render itself
        # needs assets that phase 1 has not delivered.
        renderer._require_anchor_approval(session, EPISODE_ID, settings)

    def test_other_profiles_are_not_guarded(self, session, settings, episode):
        from btcedu.core import renderer

        episode.content_profile = "bitcoin_podcast"
        session.commit()
        _seed(session, settings)
        renderer._require_anchor_approval(session, EPISODE_ID, settings)

    def test_a_disabled_deployment_is_not_guarded(self, session, settings, episode):
        from btcedu.core import renderer

        settings.anchor_enabled = False
        _seed(session, settings)
        renderer._require_anchor_approval(session, EPISODE_ID, settings)
