"""Scene-based anchor generation: only the presenter, only once, only paid for.

Every test here answers a question that costs real money to get wrong: did we
send something we shouldn't have, did we send it twice, or did we lose track of
something we already bought.
"""

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.anchor_generator import AnchorReconciliationRequired, generate_anchors
from btcedu.core.avatar_jobs import episode_avatar_cost, episode_jobs, get_job
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    ROLE_REPORTER,
    TEMPLATE_ANCHOR,
    TEMPLATE_REPORTER,
    TEMPLATE_WEATHER,
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
from btcedu.services.anchor_service import AnchorAPIError, AnchorResponse

LOOK_ID = "almanya24_look_02"
EPISODE_ID = "ep_scene_001"


@pytest.fixture(autouse=True)
def clean_profile_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    MediaBase.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    maker = sessionmaker(bind=engine)
    sess = maker()
    yield sess
    sess.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        anchor_enabled=True,
        heygen_api_key="test-key",
        did_api_key="test-key",
        dry_run=False,
        pipeline_version=2,
        max_episode_cost_usd=15.0,
    )


@pytest.fixture
def episode(session):
    ep = Episode(
        episode_id=EPISODE_ID,
        title="Tagesschau",
        url="https://example.com/t",
        status=EpisodeStatus.SCENE_PLANNED,
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
            look_name="Look 2",
            strategy="least_recently_used",
            config_version=1,
            status="assigned",
            content_hash="pool-hash",
            provenance_path="",
            cost_per_second_usd=0.0167,
        )
    )
    session.commit()
    return ep


class FakeAnchorService:
    """Stands in for HeyGen. It never reaches the network and never bills."""

    provider = "heygen"

    def __init__(self, anchor_dir: Path, avatar_id: str):
        self.anchor_dir = Path(anchor_dir)
        self.avatar_id = avatar_id
        self.submissions: list[str] = []
        self.collections: list[str] = []
        self.submit_error: Exception | None = None
        self.collect_error: Exception | None = None
        self._counter = 0

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(max(0.0, duration_seconds) * 0.0167, 6)

    def submit_anchor_video(self, request) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        self.submissions.append(request.output_name)
        self._counter += 1
        return f"heygen_job_{self._counter}"

    def collect_anchor_video(self, provider_job_id: str, request) -> AnchorResponse:
        if self.collect_error is not None:
            raise self.collect_error
        self.collections.append(provider_job_id)
        self.anchor_dir.mkdir(parents=True, exist_ok=True)
        out = self.anchor_dir / f"{request.output_name}.webm"
        out.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
        duration = request.expected_duration_seconds or 5.0
        return AnchorResponse(
            video_path=str(out),
            chapter_id=request.chapter_id,
            duration_seconds=duration,
            size_bytes=out.stat().st_size,
            cost_usd=self.estimate_cost(duration),
            provider="heygen",
            provider_job_id=provider_job_id,
            output_format="webm",
            mime_type="video/webm",
        )

    def generate_anchor_video(self, request) -> AnchorResponse:
        return self.collect_anchor_video(self.submit_anchor_video(request), request)


def _scene(
    scene_id: str,
    chapter_id: str,
    order: int,
    role: str,
    indices: list[int],
    *,
    template: str = TEMPLATE_ANCHOR,
    duration: float = 6.0,
    text_hash: str = "t-hash",
    needs_avatar: bool | None = None,
) -> Scene:
    studio = role == ROLE_ANCHOR
    return Scene(
        scene_id=scene_id,
        chapter_id=chapter_id,
        order=order,
        beat_index=0,
        speaker_role=role,
        purpose="report",
        segment_indices=indices,
        text_hash=text_hash,
        audio_file=None,
        expected_duration_seconds=duration,
        visual_mode=VISUAL_MODE_STUDIO if studio else VISUAL_MODE_FULLSCREEN,
        template_id=template,
        background_asset=None,
        background_asset_type="none",
        display_zone_id="monitor" if studio else None,
        display_fit_mode="contain",
        focus_point=None,
        transition_in="cut",
        overlays={},
        needs_avatar=studio if needs_avatar is None else needs_avatar,
    )


def _write_episode_files(
    settings: Settings,
    scenes: list[Scene],
    *,
    parts_by_chapter: dict[str, list[dict]] | None = None,
) -> Path:
    outputs_dir = Path(settings.outputs_dir) / EPISODE_ID
    tts_dir = outputs_dir / "tts" / "parts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    chapter_ids = sorted({s.chapter_id for s in scenes})
    parts_by_chapter = parts_by_chapter or {
        cid: [
            {"file": f"{cid}_p00.mp3", "duration_seconds": 6.0, "speaker_role": ROLE_ANCHOR},
            {"file": f"{cid}_p01.mp3", "duration_seconds": 4.0, "speaker_role": ROLE_REPORTER},
        ]
        for cid in chapter_ids
    }

    segments = []
    for cid in chapter_ids:
        parts = parts_by_chapter.get(cid, [])
        for index, part in enumerate(parts):
            (tts_dir / part["file"]).write_bytes(
                b"\xff\xfb\x90" + f"{cid}-{index}".encode() + b"\x00" * 64
            )
        whole = outputs_dir / "tts" / f"{cid}.mp3"
        whole.write_bytes(b"\xff\xfb\x90" + b"\x00" * 64)
        segments.append(
            {
                "chapter_id": cid,
                "file_path": f"tts/{cid}.mp3",
                "duration_seconds": sum(float(p["duration_seconds"]) for p in parts) or 6.0,
                "metadata": {"speaker_parts": parts},
            }
        )

    (outputs_dir / "tts" / "manifest.json").write_text(
        json.dumps({"episode_id": EPISODE_ID, "segments": segments}), encoding="utf-8"
    )
    (outputs_dir / "chapters.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": EPISODE_ID,
                "title": "Tagesschau",
                "total_chapters": len(chapter_ids),
                "estimated_duration_seconds": 6 * len(chapter_ids),
                "chapters": [
                    {
                        "chapter_id": cid,
                        "title": f"Chapter {cid}",
                        "order": i + 1,
                        "narration": {
                            "text": "Bugun Almanya gundeminde onemli gelismeler yasandi.",
                            "word_count": 7,
                            "estimated_duration_seconds": 6,
                        },
                        "visual": {"type": "talking_head", "description": "Studio"},
                        "overlays": [],
                        "transitions": {"in": "fade", "out": "fade"},
                    }
                    for i, cid in enumerate(chapter_ids)
                ],
            }
        ),
        encoding="utf-8",
    )

    write_scene_plan(
        outputs_dir / "scene_plan.json",
        EPISODE_ID,
        scenes,
        presenter_look_id=LOOK_ID,
        presenter_assignment_id=1,
    )
    return outputs_dir


def _default_scenes() -> list[Scene]:
    return [
        _scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0]),
        _scene("sc_002", "ch_01", 2, ROLE_REPORTER, [1], template=TEMPLATE_REPORTER, duration=4.0),
        _scene("sc_003", "ch_02", 3, ROLE_ANCHOR, [0], text_hash="t-hash-2"),
    ]


def _run(session, settings, service, force=False):
    with patch(
        "btcedu.core.anchor_generator._create_anchor_service",
        side_effect=lambda config, s, anchor_dir, avatar_id=None: (
            setattr(service, "anchor_dir", Path(anchor_dir)),
            setattr(service, "avatar_id", avatar_id or config.avatar_id),
            service,
        )[-1],
    ):
        return generate_anchors(session, EPISODE_ID, settings, force=force)


def _manifest(settings) -> dict:
    path = Path(settings.outputs_dir) / EPISODE_ID / "anchor" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestOnlyThePresenterIsOrdered:
    def test_only_anchor_scenes_are_submitted(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        result = _run(session, settings, service)

        assert result.submitted_count == 2
        assert sorted(service.submissions) == ["sc_001", "sc_003"]
        assert sorted(result.scene_ids) == ["sc_001", "sc_003"]

    def test_reporter_scene_creates_no_job_at_all(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        _run(session, settings, service)

        scene_ids = {job.scene_id for job in episode_jobs(session, EPISODE_ID)}
        assert "sc_002" not in scene_ids
        assert all(entry["speaker_role"] == ROLE_ANCHOR for entry in _manifest(settings)["scenes"])

    def test_weather_handover_is_never_animated(self, session, settings, episode):
        scenes = _default_scenes()
        scenes.append(
            _scene("sc_004", "ch_03", 4, ROLE_ANCHOR, [0], template=TEMPLATE_WEATHER)
        )
        _write_episode_files(settings, scenes)
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        _run(session, settings, service)

        assert "sc_004" not in service.submissions
        reasons = {e["scene_id"]: e["reason"] for e in _manifest(settings)["excluded_scenes"]}
        assert reasons["sc_004"] == "weather_is_rendered"


class TestOneOutfitPerEpisode:
    def test_all_anchor_scenes_share_the_persisted_look(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        _run(session, settings, service)

        assert service.avatar_id == LOOK_ID
        looks = {job.avatar_look_id for job in episode_jobs(session, EPISODE_ID)}
        assert looks == {LOOK_ID}
        assert {e["avatar_look_id"] for e in _manifest(settings)["scenes"]} == {LOOK_ID}

    def test_plan_built_for_another_look_fails_closed(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        plan_path = Path(settings.outputs_dir) / EPISODE_ID / "scene_plan.json"
        doc = json.loads(plan_path.read_text(encoding="utf-8"))
        doc["presenter_look_id"] = "some_other_look"
        plan_path.write_text(json.dumps(doc), encoding="utf-8")
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        with pytest.raises(ValueError, match="was built for look"):
            _run(session, settings, service)
        assert service.submissions == []


class TestNothingIsBoughtTwice:
    def test_submitted_job_is_only_polled(self, session, settings, episode):
        _write_episode_files(settings, [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])])
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        service.collect_error = RuntimeError("provider timed out")

        with pytest.raises(RuntimeError, match="timed out"):
            _run(session, settings, service)

        job = episode_jobs(session, EPISODE_ID)[0]
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert job.provider_job_id == "heygen_job_1"

        # An operator confirms the provider did produce the clip.
        job.status = AvatarJobStatus.SUBMITTED.value
        session.commit()

        service.collect_error = None
        service.submissions.clear()
        result = _run(session, settings, service, force=True)

        assert service.submissions == []
        assert service.collections == ["heygen_job_1"]
        assert result.resumed_count == 1
        assert result.submitted_count == 0

    def test_completed_job_is_reused(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)

        service.submissions.clear()
        service.collections.clear()
        result = _run(session, settings, service, force=True)

        assert service.submissions == []
        assert service.collections == []
        assert result.reused_count == 2
        assert result.cost_usd == 0.0

    def test_reconcile_required_blocks_instead_of_rebuying(self, session, settings, episode):
        _write_episode_files(settings, [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])])
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)

        job = episode_jobs(session, EPISODE_ID)[0]
        job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
        job.error_message = "unknown outcome"
        session.commit()
        service.submissions.clear()

        with pytest.raises(AnchorReconciliationRequired):
            _run(session, settings, service, force=True)
        assert service.submissions == []

    def test_reserved_job_is_held_not_repurchased(self, session, settings, episode):
        _write_episode_files(settings, [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])])
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        service.submit_error = RuntimeError("connection reset")

        with pytest.raises(RuntimeError):
            _run(session, settings, service)

        job = episode_jobs(session, EPISODE_ID)[0]
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

        service.submit_error = None
        service.submissions.clear()
        with pytest.raises(AnchorReconciliationRequired):
            _run(session, settings, service, force=True)
        assert service.submissions == []

    def test_refused_request_may_be_retried(self, session, settings, episode):
        _write_episode_files(settings, [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])])
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        service.submit_error = AnchorAPIError("heygen", 401, "bad key")

        with pytest.raises(AnchorAPIError):
            _run(session, settings, service)

        job = episode_jobs(session, EPISODE_ID)[0]
        assert job.status == AvatarJobStatus.FAILED.value
        assert job.cost_usd == 0.0

        service.submit_error = None
        result = _run(session, settings, service, force=True)
        assert result.submitted_count == 1


class TestBudget:
    def test_unresolved_jobs_count_against_the_budget(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)

        spent = episode_avatar_cost(session, EPISODE_ID)
        assert spent > 0

        job = episode_jobs(session, EPISODE_ID)[0]
        job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
        session.commit()
        assert episode_avatar_cost(session, EPISODE_ID) == pytest.approx(spent)

    def test_preflight_refuses_when_the_stage_budget_is_too_small(
        self, session, settings, episode
    ):
        settings.max_episode_cost_usd = 0.0001
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        with pytest.raises(Exception, match="budget|cost limit"):
            _run(session, settings, service)
        assert service.submissions == []


class TestRestartAndInvalidation:
    def test_restart_after_partial_progress_finishes_the_rest(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        original_submit = service.submit_anchor_video

        def fail_on_second(request):
            if len(service.submissions) >= 1:
                raise AnchorAPIError("heygen", 400, "simulated crash before generation")
            return original_submit(request)

        service.submit_anchor_video = fail_on_second
        with pytest.raises(AnchorAPIError):
            _run(session, settings, service)

        done = [j for j in episode_jobs(session, EPISODE_ID) if j.status == "completed"]
        assert len(done) == 1

        service.submit_anchor_video = original_submit
        service.submissions.clear()
        result = _run(session, settings, service, force=True)

        assert service.submissions == ["sc_003"]
        assert result.submitted_count == 1
        assert result.reused_count == 1
        assert result.segment_count == 2

    def test_changed_reporter_scene_does_not_invalidate_anchor_jobs(
        self, session, settings, episode
    ):
        scenes = _default_scenes()
        _write_episode_files(settings, scenes)
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)
        first_ids = [j.provider_job_id for j in episode_jobs(session, EPISODE_ID)]

        scenes[1] = _scene(
            "sc_002",
            "ch_01",
            2,
            ROLE_REPORTER,
            [1],
            template=TEMPLATE_REPORTER,
            duration=4.0,
            text_hash="reporter-rewritten",
        )
        _write_episode_files(settings, scenes)
        service.submissions.clear()

        result = _run(session, settings, service)

        assert service.submissions == []
        assert result.reused_count == 2
        assert [j.provider_job_id for j in episode_jobs(session, EPISODE_ID)] == first_ids

    def test_changed_anchor_audio_yields_a_new_content_hash(self, session, settings, episode):
        scenes = [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])]
        _write_episode_files(settings, scenes)
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)
        first_hash = _manifest(settings)["scenes"][0]["content_hash"]

        part = Path(settings.outputs_dir) / EPISODE_ID / "tts" / "parts" / "ch_01_p00.mp3"
        part.write_bytes(b"\xff\xfb\x90" + b"\x11" * 128)
        service.submissions.clear()

        _run(session, settings, service)
        second_hash = _manifest(settings)["scenes"][0]["content_hash"]

        assert second_hash != first_hash
        assert service.submissions == ["sc_001"]
        assert get_job(session, EPISODE_ID, "sc_001", first_hash) is not None
        assert get_job(session, EPISODE_ID, "sc_001", second_hash) is not None


class TestManifestAndProvenance:
    def test_manifest_records_everything_needed_to_audit_a_purchase(
        self, session, settings, episode
    ):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        result = _run(session, settings, service)

        manifest = _manifest(settings)
        entry = manifest["scenes"][0]
        for key in (
            "scene_id",
            "chapter_id",
            "speaker_role",
            "avatar_look_id",
            "audio_hash",
            "content_hash",
            "provider_job_id",
            "status",
            "duration_seconds",
            "cost_usd",
        ):
            assert entry[key] not in (None, "")
        assert entry["audio_path"].startswith("tts/parts/")
        # The renderer still cuts by chapter; an empty segment list keeps it
        # behaving exactly as before until WP-3 teaches it about scenes.
        assert manifest["segments"] == []
        assert manifest["schema_version"] == "2.0"

        provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
        assert len(provenance["clips"]) == 2
        assert provenance["avatar_look_id"] == LOOK_ID

    def test_audio_source_is_the_existing_tts_part_file(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)
        _run(session, settings, service)

        outputs_dir = Path(settings.outputs_dir) / EPISODE_ID
        for entry in _manifest(settings)["scenes"]:
            assert (outputs_dir / entry["audio_path"]).exists()

    def test_multi_part_anchor_scene_yields_one_clip_per_take(self, session, settings, episode):
        scenes = [_scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0, 1])]
        parts = {
            "ch_01": [
                {"file": "ch_01_p00.mp3", "duration_seconds": 5.0, "speaker_role": ROLE_ANCHOR},
                {"file": "ch_01_p01.mp3", "duration_seconds": 3.0, "speaker_role": ROLE_ANCHOR},
            ]
        }
        _write_episode_files(settings, scenes, parts_by_chapter=parts)
        service = FakeAnchorService(Path(settings.outputs_dir), LOOK_ID)

        _run(session, settings, service)

        assert service.submissions == ["sc_001_p00", "sc_001_p01"]


class TestDryRun:
    def test_dry_run_writes_no_ledger_rows_and_calls_nothing_real(
        self, session, settings, episode
    ):
        settings.dry_run = True
        _write_episode_files(settings, _default_scenes())

        result = generate_anchors(session, EPISODE_ID, settings)

        assert result.segment_count == 2
        assert result.cost_usd == 0.0
        assert episode_jobs(session, EPISODE_ID) == []
        assert all(e["status"] == "dry_run" for e in _manifest(settings)["scenes"])
        assert episode.status == EpisodeStatus.ANCHOR_GENERATED


class TestBackwardCompatibility:
    def test_without_a_scene_plan_the_chapter_path_still_runs(self, session, settings, episode):
        _write_episode_files(settings, _default_scenes())
        (Path(settings.outputs_dir) / EPISODE_ID / "scene_plan.json").unlink()
        settings.dry_run = True

        result = generate_anchors(session, EPISODE_ID, settings)

        manifest = _manifest(settings)
        assert "scenes" not in manifest
        assert len(manifest["segments"]) == result.segment_count

    def test_disabled_anchor_path_is_untouched(self, session, settings, episode):
        settings.anchor_enabled = False
        _write_episode_files(settings, _default_scenes())

        result = generate_anchors(session, EPISODE_ID, settings)

        assert result.skipped is True
        assert episode.status == EpisodeStatus.ANCHOR_GENERATED
        assert episode_jobs(session, EPISODE_ID) == []

    def test_scene_dataclass_round_trips_through_the_plan(self):
        scene = _scene("sc_001", "ch_01", 1, ROLE_ANCHOR, [0])
        assert Scene(**asdict(scene)) == scene
