"""The avatar review gate: nobody's face goes on air unlooked-at.

These tests are about two failure modes that cost either money or credibility.
The first is rendering a bulletin with a presenter clip no one checked. The
second is carrying an old approval forward onto clips that have since changed —
which looks exactly like a review from the outside and is worth nothing.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core import anchor_review
from btcedu.core.anchor_review import (
    AnchorReviewError,
    StaleAnchorReviewError,
    approve,
    collect_state,
    ensure_review_task,
    flag_scene,
    gate_applies,
    has_current_approval,
    reject,
    review_hash,
)
from btcedu.core.avatar_jobs import reserve_scene
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
from btcedu.models.review import ReviewStatus
from btcedu.profiles import reset_registry

EPISODE_ID = "ep_review_001"
LOOK_ID = "heygen_look_alpha"


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


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
        dry_run=False,
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


def _scenes():
    return [
        _scene("sc_001", "ch_01", 1, ROLE_ANCHOR),
        _scene("sc_002", "ch_01", 2, ROLE_REPORTER),
        _scene("sc_003", "ch_02", 3, ROLE_ANCHOR),
    ]


def _write_plan(settings, scenes=None):
    outputs = Path(settings.outputs_dir) / EPISODE_ID
    outputs.mkdir(parents=True, exist_ok=True)
    write_scene_plan(
        outputs / "scene_plan.json",
        EPISODE_ID,
        scenes if scenes is not None else _scenes(),
        presenter_look_id=LOOK_ID,
        presenter_assignment_id=1,
    )
    return outputs


def _manifest_entry(scene_id, chapter_id, *, look_id=LOOK_ID, audio_hash="a1", status="completed"):
    return {
        "scene_id": scene_id,
        "clip_id": scene_id,
        "chapter_id": chapter_id,
        "speaker_role": ROLE_ANCHOR,
        "template_id": TEMPLATE_ANCHOR,
        "avatar_look_id": look_id,
        "part_index": 0,
        "audio_path": f"tts/parts/{chapter_id}_p00.mp3",
        "audio_hash": audio_hash,
        "content_hash": f"c-{scene_id}-{audio_hash}",
        "provider": "heygen",
        "provider_job_id": f"heygen_{scene_id}",
        "status": status,
        "video_path": f"anchor/{scene_id}.webm",
        "duration_seconds": 6.0,
        "size_bytes": 68,
        "cost_usd": 0.1002,
        "output_format": "webm",
        "mime_type": "video/webm",
    }


def _write_manifest(settings, entries, *, look_id=LOOK_ID):
    outputs = Path(settings.outputs_dir) / EPISODE_ID
    anchor_dir = outputs / "anchor"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        clip = outputs / entry["video_path"]
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    (anchor_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "episode_id": EPISODE_ID,
                "anchor_provider": "heygen",
                "engine": "avatar_iii",
                "avatar_look_id": look_id,
                "output_format": "webm",
                "scenes": entries,
                "segments": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return anchor_dir


def _complete_jobs(session, settings, scene_ids):
    """A ledger row per scene, as a successful anchorgen run would leave it."""
    for scene_id in scene_ids:
        job = reserve_scene(
            session,
            episode_id=EPISODE_ID,
            scene_id=scene_id,
            chapter_id="ch_01",
            content_hash=f"c-{scene_id}-a1",
            avatar_look_id=LOOK_ID,
            provider="heygen",
            engine="avatar_iii",
            output_format="webm",
            estimated_cost_usd=0.1002,
        ).job
        job.status = AvatarJobStatus.COMPLETED.value
        job.cost_usd = 0.1002
    session.commit()


def _ready_episode(session, settings):
    _write_plan(settings)
    _write_manifest(
        settings,
        [_manifest_entry("sc_001", "ch_01"), _manifest_entry("sc_003", "ch_02")],
    )
    _complete_jobs(session, settings, ["sc_001", "sc_003"])


@pytest.fixture(autouse=True)
def no_readiness_blockers():
    """Phase 1 assets do not exist yet; those blockers are tested separately."""
    with patch.object(anchor_review, "_rights_and_readiness_blockers", return_value=[]):
        yield


class TestWhenTheGateApplies:
    def test_active_for_almanya24(self, session, settings, episode):
        state = collect_state(session, EPISODE_ID, settings)
        assert state.enabled is True
        assert state.provider == "heygen"
        assert state.engine == "avatar_iii"

    def test_inactive_when_the_deployment_has_avatars_off(self, session, settings, episode):
        settings.anchor_enabled = False
        state = collect_state(session, EPISODE_ID, settings)
        assert state.enabled is False
        assert "disabled" in state.reason

    def test_inactive_for_other_profiles(self, session, settings, episode):
        episode.content_profile = "bitcoin_podcast"
        session.commit()
        state = collect_state(session, EPISODE_ID, settings)
        assert state.enabled is False

    def test_inactive_when_the_profile_does_not_ask_for_a_review(self, settings):
        config = type("C", (), {"provider": "heygen", "review_required": False})()
        applies, reason = gate_applies(config, settings)
        assert applies is False
        assert "does not require" in reason

    def test_inactive_for_the_did_path(self, settings):
        config = type("C", (), {"provider": "d-id", "review_required": True})()
        applies, reason = gate_applies(config, settings)
        assert applies is False
        assert "d-id" in reason

    def test_no_anchor_scenes_means_nothing_to_review(self, session, settings, episode):
        _write_plan(settings, [_scene("sc_002", "ch_01", 1, ROLE_REPORTER)])
        state = collect_state(session, EPISODE_ID, settings)
        assert state.scenes == []
        assert state.expected_scene_count == 0


class TestWhatMayBeApproved:
    def test_a_complete_episode_is_approvable(self, session, settings, episode):
        _ready_episode(session, settings)
        state = collect_state(session, EPISODE_ID, settings)
        assert state.blockers == []
        assert state.approvable is True
        assert state.completed_scene_count == 2

    def test_a_missing_clip_blocks_approval(self, session, settings, episode):
        _write_plan(settings)
        _write_manifest(settings, [_manifest_entry("sc_001", "ch_01")])
        _complete_jobs(session, settings, ["sc_001"])
        state = collect_state(session, EPISODE_ID, settings)
        assert state.blockers
        assert any("sc_003" in b for b in state.blockers)

    def test_a_reserved_job_blocks_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        from btcedu.models.avatar_job import AvatarJob

        job = session.query(AvatarJob).filter_by(scene_id="sc_001").first()
        job.status = AvatarJobStatus.RESERVED.value
        session.commit()
        state = collect_state(session, EPISODE_ID, settings)
        assert any("reserved" in b for b in state.blockers)

    def test_reconcile_required_blocks_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        from btcedu.models.avatar_job import AvatarJob

        job = session.query(AvatarJob).filter_by(scene_id="sc_003").first()
        job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
        session.commit()
        state = collect_state(session, EPISODE_ID, settings)
        assert any("reconcile" in b.lower() for b in state.blockers)

    def test_a_mismatched_look_blocks_approval(self, session, settings, episode):
        _write_plan(settings)
        _write_manifest(
            settings,
            [
                _manifest_entry("sc_001", "ch_01"),
                _manifest_entry("sc_003", "ch_02", look_id="some_other_look"),
            ],
        )
        _complete_jobs(session, settings, ["sc_001", "sc_003"])
        state = collect_state(session, EPISODE_ID, settings)
        assert any("look" in b.lower() for b in state.blockers)

    def test_an_open_complaint_blocks_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        flag_scene(
            session,
            EPISODE_ID,
            settings,
            scene_id="sc_001",
            note="Lippen asynchron",
            operator_ref="op1",
        )
        state = collect_state(session, EPISODE_ID, settings)
        assert any("sc_001" in b for b in state.blockers)
        with pytest.raises(AnchorReviewError):
            approve(session, EPISODE_ID, settings)

    def test_rights_blockers_prevent_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        with patch.object(
            anchor_review,
            "_rights_and_readiness_blockers",
            return_value=["readiness: presenter consent is missing"],
        ):
            state = collect_state(session, EPISODE_ID, settings)
            assert any("consent" in b for b in state.blockers)
            with pytest.raises(AnchorReviewError):
                approve(session, EPISODE_ID, settings)

    def test_a_flagged_scene_can_be_cleared_again(self, session, settings, episode):
        _ready_episode(session, settings)
        flag_scene(
            session, EPISODE_ID, settings, scene_id="sc_001", note="zu dunkel", operator_ref="op1"
        )
        anchor_review.clear_scene_flag(session, EPISODE_ID, scene_id="sc_001")
        state = collect_state(session, EPISODE_ID, settings)
        assert state.blockers == []

    def test_flagging_an_unknown_scene_is_refused(self, session, settings, episode):
        _ready_episode(session, settings)
        with pytest.raises(AnchorReviewError):
            flag_scene(session, EPISODE_ID, settings, scene_id="sc_999", note="x")

    def test_a_complaint_needs_a_note(self, session, settings, episode):
        _ready_episode(session, settings)
        with pytest.raises(AnchorReviewError):
            flag_scene(session, EPISODE_ID, settings, scene_id="sc_001", note="   ")


class TestApprovalIsBoundToTheseClips:
    def test_approval_sticks_while_nothing_changes(self, session, settings, episode):
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="sieht gut aus", operator_ref="op1")
        assert has_current_approval(session, EPISODE_ID, settings) is True
        assert has_current_approval(session, EPISODE_ID, settings) is True

    def test_a_new_clip_hash_invalidates_the_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        _write_manifest(
            settings,
            [
                _manifest_entry("sc_001", "ch_01", audio_hash="a2"),
                _manifest_entry("sc_003", "ch_02"),
            ],
        )
        assert has_current_approval(session, EPISODE_ID, settings) is False

    def test_a_new_look_invalidates_the_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        from btcedu.core.presenter_assignment import get_assignment

        get_assignment(session, EPISODE_ID).avatar_look_id = "another_look"
        session.commit()
        assert has_current_approval(session, EPISODE_ID, settings) is False

    def test_a_changed_job_status_invalidates_the_approval(self, session, settings, episode):
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        from btcedu.models.avatar_job import AvatarJob

        session.query(AvatarJob).filter_by(
            scene_id="sc_001"
        ).first().status = AvatarJobStatus.RECONCILE_REQUIRED.value
        session.commit()
        assert has_current_approval(session, EPISODE_ID, settings) is False

    def test_a_swapped_reporter_asset_does_not_invalidate_the_approval(
        self, session, settings, episode
    ):
        """The reporter is judged in the final review, not in this one."""
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        before = review_hash(session, EPISODE_ID, settings)

        scenes = _scenes()
        scenes[1].background_asset = "images/ch_01_replacement.jpg"
        _write_plan(settings, scenes)

        assert review_hash(session, EPISODE_ID, settings) == before
        assert has_current_approval(session, EPISODE_ID, settings) is True

    def test_a_changed_anchor_scene_does_invalidate_the_approval(
        self, session, settings, episode
    ):
        _ready_episode(session, settings)
        approve(session, EPISODE_ID, settings, notes="ok")
        scenes = _scenes()
        scenes[0].text_hash = "text-changed"
        _write_plan(settings, scenes)
        assert has_current_approval(session, EPISODE_ID, settings) is False

    def test_the_digest_is_not_rewritten_when_nothing_changed(self, session, settings, episode):
        _ready_episode(session, settings)
        path = anchor_review.write_digest(session, EPISODE_ID, settings)
        stamp = path.stat().st_mtime_ns
        anchor_review.write_digest(session, EPISODE_ID, settings)
        assert path.stat().st_mtime_ns == stamp

    def test_approving_a_hash_the_operator_never_saw_is_refused(
        self, session, settings, episode
    ):
        _ready_episode(session, settings)
        with pytest.raises(StaleAnchorReviewError):
            approve(session, EPISODE_ID, settings, expected_review_hash="deadbeef" * 8)

    def test_the_shortened_hash_from_the_dashboard_is_accepted(self, session, settings, episode):
        _ready_episode(session, settings)
        shown = collect_state(session, EPISODE_ID, settings).to_dict()["review_hash"]
        approve(session, EPISODE_ID, settings, expected_review_hash=shown, notes="ok")
        assert has_current_approval(session, EPISODE_ID, settings) is True


class TestRejection:
    def test_rejection_needs_a_reason(self, session, settings, episode):
        _ready_episode(session, settings)
        with pytest.raises(AnchorReviewError):
            reject(session, EPISODE_ID, settings, notes="")

    def test_rejection_is_recorded_and_blocks(self, session, settings, episode):
        _ready_episode(session, settings)
        task = reject(session, EPISODE_ID, settings, notes="Blickrichtung falsch")
        assert task.status == ReviewStatus.REJECTED.value
        assert anchor_review.has_current_rejection(session, EPISODE_ID, settings) is True
        assert has_current_approval(session, EPISODE_ID, settings) is False

    def test_a_rejected_episode_can_be_rejudged_after_new_clips(
        self, session, settings, episode
    ):
        _ready_episode(session, settings)
        reject(session, EPISODE_ID, settings, notes="nein")
        _write_manifest(
            settings,
            [
                _manifest_entry("sc_001", "ch_01", audio_hash="a2"),
                _manifest_entry("sc_003", "ch_02", audio_hash="a2"),
            ],
        )
        _complete_jobs(session, settings, ["sc_001"])
        assert anchor_review.has_current_rejection(session, EPISODE_ID, settings) is False

    def test_a_rejection_can_be_recorded_even_with_blockers(self, session, settings, episode):
        """Refusing broken clips must not require them to be perfect first."""
        _write_plan(settings)
        _write_manifest(settings, [_manifest_entry("sc_001", "ch_01")])
        task = reject(session, EPISODE_ID, settings, notes="unbrauchbar")
        assert task.status == ReviewStatus.REJECTED.value


class TestTheReviewTask:
    def test_a_pending_task_is_reused_not_duplicated(self, session, settings, episode):
        _ready_episode(session, settings)
        first = ensure_review_task(session, EPISODE_ID, settings)
        second = ensure_review_task(session, EPISODE_ID, settings)
        assert first.id == second.id

    def test_a_changed_episode_gets_a_fresh_task(self, session, settings, episode):
        _ready_episode(session, settings)
        first = ensure_review_task(session, EPISODE_ID, settings)
        _write_manifest(
            settings,
            [
                _manifest_entry("sc_001", "ch_01", audio_hash="a9"),
                _manifest_entry("sc_003", "ch_02"),
            ],
        )
        second = ensure_review_task(session, EPISODE_ID, settings)
        assert second.id != first.id
        session.refresh(first)
        assert first.status == ReviewStatus.SUPERSEDED.value

    def test_the_state_reports_a_status_for_the_dashboard(self, session, settings, episode):
        _ready_episode(session, settings)
        assert collect_state(session, EPISODE_ID, settings).review_status == "not_started"
        ensure_review_task(session, EPISODE_ID, settings)
        assert collect_state(session, EPISODE_ID, settings).review_status == "pending"
        approve(session, EPISODE_ID, settings, notes="ok")
        assert collect_state(session, EPISODE_ID, settings).review_status == "approved"


class TestNothingConfidentialLeaks:
    def test_identifiers_are_shortened_in_the_payload(self, session, settings, episode):
        _ready_episode(session, settings)
        payload = collect_state(session, EPISODE_ID, settings).to_dict()
        assert payload["look_id"] != LOOK_ID
        assert "…" in payload["look_id"] or len(LOOK_ID) <= 16

    def test_no_api_key_appears_anywhere(self, session, settings, episode):
        _ready_episode(session, settings)
        payload = json.dumps(collect_state(session, EPISODE_ID, settings).to_dict())
        assert "test-key" not in payload
        assert settings.heygen_api_key not in payload

    def test_absolute_paths_are_not_exposed(self, session, settings, episode):
        _ready_episode(session, settings)
        payload = json.dumps(collect_state(session, EPISODE_ID, settings).to_dict())
        assert str(settings.outputs_dir) not in payload
