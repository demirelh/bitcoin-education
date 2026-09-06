"""The avatar section of the dashboard.

Two questions decide whether this code is safe: can a request reach a file it
was never meant to, and can a click cost money without someone having agreed
to it. Everything below is one of those two, plus the rule that an approval
belongs to the clips it was given, not to the episode in general.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.core import anchor_review
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
from btcedu.profiles import reset_registry

EPISODE_ID = "ep_web_avatar"
LOOK_ID = "heygen_look_alpha_0001"


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
def settings(tmp_path):
    return Settings(
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        anchor_enabled=True,
        heygen_api_key="super-secret-key",
        pipeline_version=2,
    )


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def session(factory):
    sess = factory()
    yield sess
    sess.close()


@pytest.fixture
def episode(session, profile="tagesschau_tr"):
    ep = Episode(
        episode_id=EPISODE_ID,
        source="local_recorder",
        title="Tagesschau",
        url="/mnt/rec/x.mp4",
        status=EpisodeStatus.ANCHOR_GENERATED,
        pipeline_version=2,
        content_profile=profile,
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


@pytest.fixture
def client(settings, factory, episode):
    from btcedu.web.app import create_app

    app = create_app(settings=settings)
    app.config["session_factory"] = factory
    app.config["TESTING"] = True
    return app.test_client()


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


def _entry(scene_id, chapter_id, *, suffix="webm", look_id=LOOK_ID):
    return {
        "scene_id": scene_id,
        "clip_id": scene_id,
        "chapter_id": chapter_id,
        "speaker_role": ROLE_ANCHOR,
        "template_id": TEMPLATE_ANCHOR,
        "avatar_look_id": look_id,
        "part_index": 0,
        "audio_path": f"tts/parts/{chapter_id}_p00.mp3",
        "audio_hash": "a1",
        "content_hash": f"hash-{scene_id}",
        "provider": "heygen",
        "provider_job_id": f"heygen_{scene_id}",
        "status": "completed",
        "video_path": f"anchor/{scene_id}.{suffix}",
        "duration_seconds": 6.0,
        "size_bytes": 68,
        "cost_usd": 0.1002,
        "output_format": suffix,
        "mime_type": "video/webm" if suffix == "webm" else "video/mp4",
    }


def _seed(session, settings, *, entries=None, suffix="webm", with_jobs=True):
    outputs = Path(settings.outputs_dir) / EPISODE_ID
    outputs.mkdir(parents=True, exist_ok=True)
    write_scene_plan(
        outputs / "scene_plan.json",
        EPISODE_ID,
        [
            _scene("sc_001", "ch_01", 1, ROLE_ANCHOR),
            _scene("sc_002", "ch_01", 2, ROLE_REPORTER),
            _scene("sc_003", "ch_02", 3, ROLE_ANCHOR),
        ],
        presenter_look_id=LOOK_ID,
        presenter_assignment_id=1,
    )
    entries = entries or [
        _entry("sc_001", "ch_01", suffix=suffix),
        _entry("sc_003", "ch_02", suffix=suffix),
    ]
    anchor_dir = outputs / "anchor"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        clip = outputs / entry["video_path"]
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 512)
    (anchor_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "episode_id": EPISODE_ID,
                "anchor_provider": "heygen",
                "engine": "avatar_iii",
                "avatar_look_id": LOOK_ID,
                "output_format": suffix,
                "scenes": entries,
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    if with_jobs:
        for entry in entries:
            job = reserve_scene(
                session,
                episode_id=EPISODE_ID,
                scene_id=entry["scene_id"],
                chapter_id=entry["chapter_id"],
                content_hash=entry["content_hash"],
                provider="heygen",
                engine="avatar_iii",
                avatar_look_id=LOOK_ID,
                output_format=suffix,
                estimated_cost_usd=0.1002,
            ).job
            job.status = AvatarJobStatus.COMPLETED.value
            job.cost_usd = 0.1002
            job.duration_seconds = 6.0
        session.commit()
    return outputs


class TestTheAvatarSummary:
    def test_it_reports_the_presenter_and_the_scenes(self, client, session, settings):
        _seed(session, settings)
        data = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()

        assert data["anchor_enabled"] is True
        assert data["provider"] == "heygen"
        assert data["engine"] == "avatar_iii"
        assert data["look_name"] == "look_02"
        assert data["expected_scene_count"] == 2
        assert data["completed_scene_count"] == 2
        assert [s["scene_id"] for s in data["scenes"]] == ["sc_001", "sc_003"]
        assert data["actual_cost_usd"] == pytest.approx(0.2004)
        assert data["approvable"] is True

    def test_the_reporter_scene_never_appears(self, client, session, settings):
        _seed(session, settings)
        data = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()
        assert all(s["speaker_role"] == ROLE_ANCHOR for s in data["scenes"])
        assert "sc_002" not in json.dumps(data)

    def test_an_unknown_episode_is_a_404(self, client):
        assert client.get("/api/episodes/nope/avatar").status_code == 404

    def test_no_secret_reaches_the_browser(self, client, session, settings):
        _seed(session, settings)
        body = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_data(as_text=True)
        assert "super-secret-key" not in body
        assert str(settings.outputs_dir) not in body
        assert LOOK_ID not in body

    def test_an_episode_without_phase_one_assets_still_answers(
        self, client, session, settings
    ):
        data = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()
        assert data["expected_scene_count"] == 0
        assert data["scenes"] == []

    def test_a_disabled_deployment_answers_without_scenes(
        self, settings, factory, episode, session
    ):
        from btcedu.web.app import create_app

        settings.anchor_enabled = False
        app = create_app(settings=settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True
        _seed(session, settings)
        data = app.test_client().get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()
        assert data["anchor_enabled"] is False
        assert "disabled" in data["reason"]


class TestThePreview:
    def test_a_registered_clip_is_served(self, client, session, settings):
        _seed(session, settings)
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/preview")
        assert response.status_code == 200
        assert response.mimetype == "video/webm"
        assert response.headers["X-Btcedu-Preview-Kind"] == "avatar-raw"

    def test_an_mp4_fallback_clip_is_served(self, client, session, settings):
        _seed(session, settings, suffix="mp4")
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/preview")
        assert response.status_code == 200
        assert response.mimetype == "video/mp4"

    def test_byte_ranges_are_supported(self, client, session, settings):
        _seed(session, settings)
        response = client.get(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/preview",
            headers={"Range": "bytes=0-9"},
        )
        assert response.status_code == 206
        assert len(response.get_data()) == 10

    def test_an_unknown_scene_is_a_404(self, client, session, settings):
        _seed(session, settings)
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_999/preview")
        assert response.status_code == 404

    def test_the_reporter_scene_has_no_preview(self, client, session, settings):
        _seed(session, settings)
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_002/preview")
        assert response.status_code == 404

    def test_a_traversal_attempt_reaches_nothing(self, client, session, settings):
        _seed(session, settings)
        secret = Path(settings.outputs_dir).parent / "secret.webm"
        secret.write_bytes(b"secret")
        response = client.get(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/..%2F..%2Fsecret/preview"
        )
        assert response.status_code in (404, 308)
        assert b"secret" not in response.get_data()

    def test_a_symlinked_clip_is_refused(self, client, session, settings):
        outputs = _seed(session, settings)
        outside = Path(settings.outputs_dir).parent / "outside.webm"
        outside.write_bytes(b"outside")
        clip = outputs / "anchor" / "sc_001.webm"
        clip.unlink()
        clip.symlink_to(outside)
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/preview")
        assert response.status_code == 404
        assert b"outside" not in response.get_data()

    def test_a_missing_file_is_reported_not_streamed(self, client, session, settings):
        outputs = _seed(session, settings)
        (outputs / "anchor" / "sc_001.webm").unlink()
        response = client.get(f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/preview")
        assert response.status_code == 404
        assert "missing" in response.get_json()["error"]


class TestApprovingFromTheDashboard:
    def test_a_complete_episode_can_be_approved(self, client, session, settings):
        _seed(session, settings)
        shown = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()["review_hash"]
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/approve",
            json={"notes": "sieht gut aus", "review_hash": shown, "operator_ref": "op1"},
        )
        assert response.status_code == 200
        assert response.get_json()["avatar"]["review_status"] == "approved"

    def test_a_stale_hash_is_a_409(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/approve",
            json={"review_hash": "0" * 64},
        )
        assert response.status_code == 409
        assert response.get_json()["stale"] is True

    def test_an_incomplete_episode_cannot_be_approved(self, client, session, settings):
        _seed(session, settings, entries=[_entry("sc_001", "ch_01")])
        response = client.post(f"/api/episodes/{EPISODE_ID}/avatar/approve", json={})
        assert response.status_code == 400
        assert "sc_003" in response.get_json()["error"]

    def test_rejection_requires_a_reason(self, client, session, settings):
        _seed(session, settings)
        assert (
            client.post(f"/api/episodes/{EPISODE_ID}/avatar/reject", json={}).status_code == 400
        )

    def test_rejection_is_recorded(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/reject",
            json={"notes": "Blickrichtung falsch"},
        )
        assert response.status_code == 200
        assert response.get_json()["avatar"]["review_status"] == "rejected"

    def test_a_scene_can_be_flagged_and_cleared(self, client, session, settings):
        _seed(session, settings)
        flagged = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/flag",
            json={"note": "Lippen asynchron"},
        )
        assert flagged.status_code == 200
        assert flagged.get_json()["avatar"]["approvable"] is False

        cleared = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/flag", json={"clear": True}
        )
        assert cleared.get_json()["avatar"]["approvable"] is True


class TestRegenerationFromTheDashboard:
    def test_preparing_quotes_but_does_not_order(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare", "reason": "Lippen asynchron"},
        )
        assert response.status_code == 200
        quote = response.get_json()["quote"]
        assert quote["previous_cost_usd"] == pytest.approx(0.1002)
        assert quote["estimated_cost_usd"] > 0
        assert "paid" in quote["warning"]

        from btcedu.core.avatar_regeneration import active_revision

        assert active_revision(session, EPISODE_ID, "sc_001") == 0

    def test_preparing_needs_a_reason(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare"},
        )
        assert response.status_code == 400

    def test_a_double_click_prepares_one_request(self, client, session, settings):
        _seed(session, settings)
        payload = {"action": "prepare", "reason": "x"}
        first = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate", json=payload
        ).get_json()
        second = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate", json=payload
        ).get_json()
        assert first["quote"]["request_id"] == second["quote"]["request_id"]

        from btcedu.models.avatar_regeneration import AvatarRegenerationRequest

        assert session.query(AvatarRegenerationRequest).count() == 1

    def test_confirming_needs_the_revision_that_was_shown(self, client, session, settings):
        _seed(session, settings)
        client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare", "reason": "x"},
        )
        blind = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "confirm"},
        )
        assert blind.status_code == 400

    def test_confirming_registers_exactly_one_order(self, client, session, settings):
        _seed(session, settings)
        quote = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare", "reason": "x"},
        ).get_json()["quote"]
        for _ in range(2):
            response = client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
                json={"action": "confirm", "revision": quote["revision"]},
            )
            assert response.status_code == 200

        from btcedu.core.avatar_regeneration import active_revision, episode_revisions

        assert active_revision(session, EPISODE_ID, "sc_001") == 1
        assert episode_revisions(session, EPISODE_ID) == {"sc_001": 1}

    def test_a_confirmed_regeneration_invalidates_the_approval(
        self, client, session, settings
    ):
        _seed(session, settings)
        client.post(f"/api/episodes/{EPISODE_ID}/avatar/approve", json={"notes": "ok"})
        assert (
            client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()["review_status"]
            == "approved"
        )

        quote = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare", "reason": "x"},
        ).get_json()["quote"]
        client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "confirm", "revision": quote["revision"]},
        )

        after = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()
        assert after["scenes"][0]["revision"] == 1
        assert after["review_status"] != "approved"

    def test_other_scenes_keep_their_clips(self, client, session, settings):
        outputs = _seed(session, settings)
        quote = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "prepare", "reason": "x"},
        ).get_json()["quote"]
        client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "confirm", "revision": quote["revision"]},
        )
        assert (outputs / "anchor" / "sc_001.webm").exists()
        assert (outputs / "anchor" / "sc_003.webm").exists()

    def test_an_unknown_action_is_refused(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
            json={"action": "buy_everything"},
        )
        assert response.status_code == 400


class TestChangingTheOutfit:
    def test_a_look_change_before_the_first_job_is_free(self, client, session, settings):
        outputs = Path(settings.outputs_dir) / EPISODE_ID
        outputs.mkdir(parents=True, exist_ok=True)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/look", json={"look_name": "look_05"}
        )
        # Phase 1 has not delivered look IDs yet, so the pool refuses — but the
        # refusal must be about the pool, never a silent success on a fake ID.
        assert response.status_code == 400
        assert "look" in response.get_json()["error"].lower()

    def test_a_look_change_after_the_first_job_is_a_409(self, client, session, settings):
        _seed(session, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/look", json={"look_name": "look_05"}
        )
        assert response.status_code == 409
        body = response.get_json()
        assert body["requires_confirmation"] is True
        assert body["existing_jobs"] == 2
        assert body["spent_usd"] == pytest.approx(0.2004)

    def test_a_look_change_needs_a_name(self, client, session, settings):
        _seed(session, settings)
        assert (
            client.post(f"/api/episodes/{EPISODE_ID}/avatar/look", json={}).status_code == 400
        )


class TestOtherProfilesAreUntouched:
    def test_the_podcast_profile_has_no_avatar_stage(
        self, settings, factory, session, episode
    ):
        from btcedu.web.app import create_app

        episode.content_profile = "bitcoin_podcast"
        session.commit()
        app = create_app(settings=settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True
        data = app.test_client().get(f"/api/episodes/{EPISODE_ID}/avatar").get_json()
        assert data["anchor_enabled"] is False
        assert data["scenes"] == []

    def test_no_provider_is_ever_contacted(self, client, session, settings):
        _seed(session, settings)
        with patch("btcedu.core.anchor_generator._create_anchor_service") as service:
            client.get(f"/api/episodes/{EPISODE_ID}/avatar")
            client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/scenes/sc_001/regenerate",
                json={"action": "prepare", "reason": "x"},
            )
            client.post(f"/api/episodes/{EPISODE_ID}/avatar/approve", json={"notes": "ok"})
            service.assert_not_called()
