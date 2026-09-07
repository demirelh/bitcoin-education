"""End-to-end dry run of a whole synthetic ALMANYA24 bulletin.

Everything external is a local double. Nothing here opens a socket, orders a
clip or uploads a video, and every assertion is about structure, ordering,
hashes, money and refusals — never about how the picture looks.

The module builds the episode once, because building it costs real ffmpeg
seconds; the tests that only need refusals build their own smaller worlds.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from btcedu.core import almanya24_smoke as smoke
from btcedu.core.almanya24_smoke import (
    ALPHA_MODE_WEBM,
    FPS,
    HEIGHT,
    WIDTH,
    Patcher,
    approve_anchor_gate,
    build_world,
    bulletin_sections,
    install_fake_providers,
    probe,
    run_dry_run,
    run_stage,
)
from btcedu.core.anchor_config import PLACEHOLDER_LOOK_ID
from btcedu.models.avatar_job import AvatarJobStatus
from btcedu.models.episode import EpisodeStatus

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="the dry run composites real files and needs ffmpeg/ffprobe",
)


# ---------------------------------------------------------------------------
# Shared worlds
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def finished(tmp_path_factory):
    """One complete run, from the shot list to the simulated private upload."""
    root = tmp_path_factory.mktemp("almanya24-e2e")
    world = build_world(root, real_media=True)
    with Patcher() as patcher:
        doubles = install_fake_providers(patcher, world)
        result = run_dry_run(world, doubles=doubles)
        yield world, result, doubles
    world.close()


@pytest.fixture
def planned(tmp_path):
    """A world taken as far as the shot list, cheaply."""
    world = build_world(tmp_path / "planned", real_media=True)
    with Patcher() as patcher:
        doubles = install_fake_providers(patcher, world)
        run_stage(world, "sceneplan")
        yield world, doubles
    world.close()


def _anchorgen(world, doubles=None):
    with Patcher() as patcher:
        live = doubles or install_fake_providers(patcher, world)
        if doubles is not None:
            install_fake_providers(patcher, world)
        return run_stage(world, "anchorgen"), live


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestTheWholeBulletinRuns:
    def test_every_phase_succeeded(self, finished):
        _, result, _ = finished
        failed = [phase.name for phase in result.phases if not phase.ok]
        assert failed == []

    def test_both_review_gates_stopped_before_they_passed(self, finished):
        _, result, _ = finished
        names = [phase.name for phase in result.phases]
        assert names.index("review_gate_anchor stops") < names.index("review_gate_anchor passes")
        assert names.index("review_gate_3 stops") < names.index("review_gate_3 passes")
        assert names.index("review_gate_anchor passes") < names.index("render")

    def test_the_bulletin_has_the_shape_of_a_real_broadcast(self, finished):
        world, _, _ = finished
        scenes = world.scene_plan["scenes"]
        chapters = {scene["chapter_id"] for scene in scenes}
        assert len(chapters) == 11

        by_chapter: dict[str, list[str]] = {}
        for scene in scenes:
            by_chapter.setdefault(scene["chapter_id"], []).append(scene["speaker_role"])

        assert by_chapter["ch_03"] == ["anchor_female", "reporter_male"]
        assert by_chapter["ch_04"] == ["anchor_female", "reporter_male", "anchor_female"]
        assert by_chapter["ch_05"] == ["reporter_male"]
        assert by_chapter["ch_10"] == ["anchor_female", "reporter_male"]

    def test_exactly_one_private_upload_happened(self, finished):
        _, result, doubles = finished
        assert len(result.uploads) == 1
        assert result.uploads[0]["privacy_status"] == "private"
        assert len(doubles["youtube"].uploads) == 1

    def test_a_test_target_upload_is_not_a_publication(self, finished):
        """The episode stays APPROVED, deliberately.

        Only an upload to the production channel marks an episode published;
        a private test upload must not make the pipeline believe the bulletin
        has aired. The publish job is the record that the upload happened.
        """
        world, _, _ = finished
        from btcedu.models.episode import Episode
        from btcedu.models.publish_job import PublishJob

        episode = (
            world.session.query(Episode)
            .filter(Episode.episode_id == world.episode_id)
            .first()
        )
        assert episode.status == EpisodeStatus.APPROVED
        jobs = world.session.query(PublishJob).all()
        assert len(jobs) == 1


class TestOnlyThePresenterIsEverOrdered:
    def test_no_reporter_scene_reached_the_provider(self, finished):
        world, _, _ = finished
        ordered = {entry["scene_id"] for entry in world.anchor_manifest["scenes"]}
        reporter = {
            scene["scene_id"]
            for scene in world.scene_plan["scenes"]
            if scene["speaker_role"] != "anchor_female"
        }
        assert reporter
        assert ordered & reporter == set()

    def test_the_weather_card_itself_was_not_animated(self, finished):
        world, _, _ = finished
        weather = [
            scene["scene_id"]
            for scene in world.scene_plan["scenes"]
            if scene.get("visual_mode") == "weather_renderer"
        ]
        assert weather
        ordered = {entry["scene_id"] for entry in world.anchor_manifest["scenes"]}
        assert ordered.isdisjoint(weather)

    def test_the_presenters_weather_handover_was_animated(self, finished):
        """She is on camera announcing the forecast, like any other studio shot."""
        world, _, _ = finished
        ordered = {entry["scene_id"] for entry in world.anchor_manifest["scenes"]}
        assert "ch_10_s00" in ordered

    def test_every_ordered_scene_is_an_anchor_scene(self, finished):
        world, _, _ = finished
        assert all(
            entry["speaker_role"] == "anchor_female"
            for entry in world.anchor_manifest["scenes"]
        )


class TestOneOutfitForTheWholeEpisode:
    def test_one_look_was_chosen_and_used_everywhere(self, finished):
        world, _, doubles = finished
        looks = {entry["avatar_look_id"] for entry in world.anchor_manifest["scenes"]}
        assert len(looks) == 1
        assert set(doubles["heygen"].looks_seen) <= looks

    def test_the_look_is_persisted_not_recomputed(self, finished):
        world, _, _ = finished
        from btcedu.core.presenter_assignment import get_assignment

        assignment = get_assignment(world.session, world.episode_id)
        looks = {entry["avatar_look_id"] for entry in world.anchor_manifest["scenes"]}
        assert assignment is not None
        assert assignment.avatar_look_id in looks


class TestTheLedgerAndTheManifestAgree:
    REQUIRED = {
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
    }

    def test_every_manifest_entry_is_complete(self, finished):
        world, _, _ = finished
        for entry in world.anchor_manifest["scenes"]:
            assert self.REQUIRED <= set(entry), entry["scene_id"]
            assert entry["provider_job_id"]
            assert entry["content_hash"]

    def test_every_manifest_entry_has_a_ledger_row(self, finished):
        world, _, _ = finished
        from btcedu.core.avatar_jobs import episode_jobs

        jobs = {job.scene_id: job for job in episode_jobs(world.session, world.episode_id)}
        for entry in world.anchor_manifest["scenes"]:
            job = jobs[entry["scene_id"]]
            assert job.provider_job_id == entry["provider_job_id"]
            assert job.status == AvatarJobStatus.COMPLETED.value

    def test_the_audio_is_the_tts_part_file(self, finished):
        world, _, _ = finished
        for entry in world.anchor_manifest["scenes"]:
            audio = world.episode_dir / entry["audio_path"]
            assert audio.is_file()
            assert audio.suffix == ".mp3"
            assert "tts/parts/" in entry["audio_path"]

    def test_the_recorded_cost_matches_the_ledger(self, finished):
        world, result, _ = finished
        from btcedu.core.avatar_jobs import episode_avatar_cost

        assert result.anchor_cost_usd == pytest.approx(
            episode_avatar_cost(world.session, world.episode_id)
        )


class TestTheFinishedVideo:
    def test_it_is_the_expected_picture(self, finished):
        world, result, _ = finished
        streams = probe(result.video_path)["streams"]
        video = [s for s in streams if s["codec_type"] == "video"]
        assert len(video) == 1
        assert (video[0]["width"], video[0]["height"]) == (WIDTH, HEIGHT)
        assert video[0]["r_frame_rate"] == f"{FPS}/1"
        assert video[0]["codec_name"] == "h264"
        assert world  # the world owns the file

    def test_there_is_exactly_one_audio_track(self, finished):
        """The original TTS is the only sound; no provider audio survives."""
        _, result, _ = finished
        audio = [s for s in probe(result.video_path)["streams"] if s["codec_type"] == "audio"]
        assert len(audio) == 1

    def test_it_is_as_long_as_the_narration(self, finished):
        world, result, _ = finished
        manifest = json.loads(
            (world.episode_dir / "tts" / "manifest.json").read_text(encoding="utf-8")
        )
        narration = sum(seg["duration_seconds"] for seg in manifest["segments"])
        duration = float(probe(result.video_path)["format"]["duration"])
        # Intro, topic cards and outro sit between the chapters, so the video is
        # longer than the narration — but not unboundedly so.
        assert narration < duration < narration * 6

    def test_the_file_is_not_a_stub(self, finished):
        _, result, _ = finished
        assert result.video_path.stat().st_size > 100_000

    def test_the_last_frame_is_not_blank(self, finished, tmp_path):
        _, result, _ = finished
        frame = tmp_path / "final.png"
        duration = float(probe(result.video_path)["format"]["duration"])
        smoke._run([
            smoke.ffmpeg_binary(), "-v", "error", "-y",
            "-ss", f"{max(0.0, duration - 0.5):.2f}", "-i", str(result.video_path),
            "-frames:v", "1", str(frame),
        ])
        assert frame.stat().st_size > 1000


class TestTheArtifactsCrossReference:
    def test_all_of_them_exist(self, finished):
        world, _, _ = finished
        for relative in (
            "scene_plan.json",
            "anchor/manifest.json",
            "anchor/review_digest.json",
            "render/render_manifest.json",
            "render/subtitles.tr.srt",
            "render/youtube_metadata.json",
            "provenance/publish.json",
        ):
            assert (world.episode_dir / relative).is_file(), relative

    def test_the_review_digest_names_the_manifest_it_approved(self, finished):
        world, _, _ = finished
        digest = world.read_json("anchor/review_digest.json")
        assert digest["episode_id"] == world.episode_id
        assert json.dumps(digest)  # serialisable, i.e. plain data

    def test_the_publish_provenance_names_the_simulated_video(self, finished):
        world, result, _ = finished
        provenance = world.read_json("provenance/publish.json")
        assert result.uploads[0]["title"]
        assert smoke.SMOKE_MARKER in json.dumps(provenance)

    def test_the_chapter_marks_belong_to_this_render(self, finished):
        world, _, _ = finished
        manifest = world.render_manifest
        assert manifest.get("timeline")
        starts = [float(item["start_seconds"]) for item in manifest["timeline"]]
        assert starts == sorted(starts)


class TestRunningItTwiceChangesNothing:
    def test_a_second_run_buys_nothing_and_uploads_nothing(self, finished):
        world, _, _ = finished
        before_manifest = world.anchor_manifest
        before_render = world.render_manifest

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            for stage in ("sceneplan", "anchorgen", "render", "publish"):
                run_stage(world, stage)

            assert doubles["heygen"].orders == []
            assert doubles["youtube"].uploads == []

        assert world.anchor_manifest == before_manifest
        assert world.render_manifest == before_render

    def test_the_look_is_not_chosen_again(self, finished):
        world, _, _ = finished
        from btcedu.core.presenter_assignment import get_assignment

        before = get_assignment(world.session, world.episode_id).avatar_look_id
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
        assert get_assignment(world.session, world.episode_id).avatar_look_id == before


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestItRefusesToStart:
    @staticmethod
    def _anchorgen_refuses(world) -> bool:
        """Run as far as the first paid call and report that it never happened."""
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "sceneplan")
            result = run_stage(world, "anchorgen")
        return result.status == "failed" and not doubles["heygen"].orders

    def test_a_placeholder_look_is_refused(self, tmp_path):
        world = build_world(tmp_path / "ph", real_media=False, look_ids=[PLACEHOLDER_LOOK_ID])
        try:
            result = run_stage(world, "sceneplan")
            assert result.status == "failed"
            assert "placeholder" in (result.error or "").lower()
        finally:
            world.close()

    def test_a_missing_studio_manifest_is_refused(self, tmp_path):
        world = build_world(tmp_path / "nostudio", real_media=False)
        try:
            (world.studio_dir / "manifest.json").unlink()
            assert self._anchorgen_refuses(world)
        finally:
            world.close()

    def test_an_invalid_studio_manifest_is_refused(self, tmp_path):
        world = build_world(tmp_path / "badstudio", real_media=False)
        try:
            (world.studio_dir / "manifest.json").write_text("{}", encoding="utf-8")
            assert self._anchorgen_refuses(world)
        finally:
            world.close()

    def test_a_monitor_over_the_presenter_is_refused_in_the_opaque_fallback(self, tmp_path):
        """Without a presenter-free zone the topic monitor may cover her face."""
        from btcedu.core.studio_manifest import StudioManifestError, load_studio_manifest

        world = build_world(tmp_path / "unsafe", real_media=False)
        try:
            manifest_file = world.studio_dir / "manifest.json"
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest["display_zone"]["presenter_free"] = False
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            with pytest.raises(StudioManifestError):
                load_studio_manifest(manifest_file)
        finally:
            world.close()

    def test_a_missing_rights_record_is_refused(self, tmp_path):
        world = build_world(tmp_path / "norights", real_media=False)
        try:
            world.rights_file.unlink()
            assert self._anchorgen_refuses(world)
        finally:
            world.close()

    def test_a_revoked_consent_is_refused(self, tmp_path):
        world = build_world(tmp_path / "revoked", real_media=False)
        try:
            record = json.loads(world.rights_file.read_text(encoding="utf-8"))
            record["revoked"] = True
            world.rights_file.write_text(json.dumps(record), encoding="utf-8")
            assert self._anchorgen_refuses(world)
        finally:
            world.close()


class TestItRefusesToBuy:
    def test_a_budget_that_cannot_cover_the_bulletin_stops_it(self, tmp_path):
        world = build_world(
            tmp_path / "poor",
            real_media=True,
            profile_overrides={"stage_config": {}},
        )
        try:
            profile = world.profiles_dir / f"{smoke.SMOKE_PROFILE}.yaml"
            import yaml

            data = yaml.safe_load(profile.read_text(encoding="utf-8"))
            data["stage_config"]["anchor"]["max_cost_usd"] = 0.01
            profile.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

            from btcedu.profiles import reset_registry

            reset_registry()
            with Patcher() as patcher:
                doubles = install_fake_providers(patcher, world)
                run_stage(world, "sceneplan")
                result = run_stage(world, "anchorgen")
            assert result.status == "failed"
            assert doubles["heygen"].orders == []
        finally:
            world.close()

    def test_a_reserved_job_is_not_bought_again(self, planned):
        world, _ = planned
        from btcedu.core.avatar_jobs import episode_jobs

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            first = len(doubles["heygen"].orders)

        job = episode_jobs(world.session, world.episode_id)[0]
        job.status = AvatarJobStatus.RESERVED.value
        job.provider_job_id = ""
        world.session.commit()

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            result = run_stage(world, "anchorgen", force=True)

        assert first > 0
        assert result.status == "failed"
        assert job.scene_id not in {order["title"] for order in doubles["heygen"].orders}

    def test_a_job_awaiting_reconciliation_stops_the_stage(self, planned):
        world, _ = planned
        from btcedu.core.avatar_jobs import episode_jobs

        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")

        job = episode_jobs(world.session, world.episode_id)[0]
        job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
        world.session.commit()

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            result = run_stage(world, "anchorgen", force=True)

        assert result.status == "failed"
        assert doubles["heygen"].orders == []


class TestItRefusesToShow:
    def test_a_missing_presenter_clip_blocks_the_render(self, planned):
        world, _ = planned
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            smoke.approve_anchor_gate(world)
            entry = world.anchor_manifest["scenes"][0]
            (world.episode_dir / entry["video_path"]).unlink()
            result = run_stage(world, "render")
        assert result.status == "failed"

    def test_a_corrupt_presenter_clip_blocks_the_render(self, planned):
        world, _ = planned
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            smoke.approve_anchor_gate(world)
            entry = world.anchor_manifest["scenes"][0]
            clip = world.episode_dir / entry["video_path"]
            clip.write_bytes(b"\x00" * 9000)
            result = run_stage(world, "render")
        assert result.status == "failed"

    def test_an_opaque_clip_is_refused_when_alpha_was_ordered(self, tmp_path):
        """The transparent path must not silently accept a flattened clip."""
        from btcedu.core.avatar_download import (
            ClipExpectation,
            ValidationError,
            probe_clip,
            validate_clip,
        )

        world = build_world(tmp_path / "alpha", real_media=True, alpha_mode=ALPHA_MODE_WEBM)
        try:
            fixtures = world.episode_dir / "fixtures"
            smoke.build_presenter_clips(fixtures, ["flat"], real_media=True)
            expectation = ClipExpectation(output_format="mp4", require_alpha=True)
            with pytest.raises(ValidationError):
                validate_clip(probe_clip(fixtures / "flat.mp4"), expectation)
        finally:
            world.close()

    def test_the_renderer_never_calls_a_provider(self, finished):
        world, _, _ = finished
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "render", force=True)
        assert doubles["heygen"].orders == []
        assert doubles["heygen"].uploads == []


class TestItRefusesToPublish:
    def test_without_the_anchor_review_the_render_never_happens(self, planned):
        world, _ = planned
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            gate = run_stage(world, "review_gate_anchor")
            assert gate.status == "review_pending"
            assert not (world.episode_dir / "render" / "draft.mp4").exists()

    def test_a_changed_clip_makes_the_approval_stale(self, planned):
        world, _ = planned
        from btcedu.core.anchor_review import has_current_approval, review_hash

        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            before = review_hash(world.session, world.episode_id, world.settings)
            smoke.approve_anchor_gate(world)
            assert has_current_approval(world.session, world.episode_id, world.settings)

            # A confirmed regeneration is what actually replaces a clip, and it
            # rewrites the manifest the approval is bound to.
            from btcedu.core import avatar_regeneration

            scene_id = world.anchor_manifest["scenes"][0]["scene_id"]
            quote = avatar_regeneration.prepare(
                world.session,
                world.episode_id,
                scene_id,
                reason="synthetic stale-review check",
                requested_by_ref="SMOKE-operator",
                cost_per_second_usd=0.0167,
            )
            avatar_regeneration.confirm(
                world.session,
                world.episode_id,
                scene_id,
                revision=quote.revision,
                confirmed_by_ref="SMOKE-operator",
            )
            run_stage(world, "anchorgen")

            after = review_hash(world.session, world.episode_id, world.settings)
            assert after != before
            assert not has_current_approval(world.session, world.episode_id, world.settings)
            assert run_stage(world, "review_gate_anchor").status == "review_pending"

    def test_without_the_final_review_nothing_is_uploaded(self, planned):
        world, _ = planned
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
            smoke.approve_anchor_gate(world)
            run_stage(world, "render")
            assert run_stage(world, "review_gate_3").status == "review_pending"
            assert run_stage(world, "publish").status != "success"
            assert doubles["youtube"].uploads == []

    def test_auto_publish_false_stops_at_the_gate(self, tmp_path):
        world = build_world(
            tmp_path / "manual", real_media=True, profile_overrides={"auto_publish": False}
        )
        try:
            with Patcher() as patcher:
                doubles = install_fake_providers(patcher, world)
                run_stage(world, "sceneplan")
                run_stage(world, "anchorgen")
                smoke.approve_anchor_gate(world)
                run_stage(world, "render")
                run_stage(world, "review_gate_3")
                smoke.approve_final_gate(world)
                run_stage(world, "review_gate_3")
                result = run_stage(world, "publish")
            assert result.status != "success"
            assert doubles["youtube"].uploads == []
        finally:
            world.close()


# ---------------------------------------------------------------------------
# Restart and recovery
# ---------------------------------------------------------------------------


class TestItSurvivesARestart:
    def test_the_look_is_the_same_after_a_restart(self, planned):
        world, _ = planned
        from btcedu.core.presenter_assignment import get_assignment

        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")
        before = get_assignment(world.session, world.episode_id).avatar_look_id

        world.session.expunge_all()
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")

        assert get_assignment(world.session, world.episode_id).avatar_look_id == before

    def test_a_known_provider_job_is_polled_and_not_reordered(self, planned):
        world, _ = planned
        from btcedu.core.avatar_jobs import episode_jobs

        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")

        jobs = episode_jobs(world.session, world.episode_id)
        job = jobs[0]
        known = job.provider_job_id
        entry = next(
            e for e in world.anchor_manifest["scenes"] if e["scene_id"] == job.scene_id
        )
        job.status = AvatarJobStatus.SUBMITTED.value
        world.session.commit()
        (world.episode_dir / entry["video_path"]).unlink(missing_ok=True)

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "anchorgen", force=True)

        assert known in doubles["heygen"].polls
        assert all(order["provider_job_id"] != known for order in doubles["heygen"].orders)

    def test_finished_clips_are_reused_after_partial_progress(self, planned):
        world, _ = planned
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")

        entries = world.anchor_manifest["scenes"]
        assert len(entries) > 2
        digests = {
            entry["scene_id"]: (world.episode_dir / entry["video_path"]).read_bytes()
            for entry in entries
        }

        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "anchorgen")

        assert doubles["heygen"].orders == []
        for entry in world.anchor_manifest["scenes"]:
            assert (world.episode_dir / entry["video_path"]).read_bytes() == digests[
                entry["scene_id"]
            ]

    def test_an_already_rendered_episode_is_not_rendered_again(self, finished):
        world, result, _ = finished
        before = result.video_path.stat().st_mtime_ns
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "render")
        assert result.video_path.stat().st_mtime_ns == before

    def test_a_published_episode_is_not_uploaded_twice(self, finished):
        world, _, _ = finished
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "publish")
        assert doubles["youtube"].uploads == []


# ---------------------------------------------------------------------------
# Remote render
# ---------------------------------------------------------------------------


class TestTheRemoteRenderPackage:
    @pytest.fixture
    def package(self, finished, tmp_path):
        from btcedu.core.remote_render import build_job_package

        world, _, _ = finished
        return build_job_package(
            world.session, world.episode_id, world.settings, tmp_path, False
        ), world

    def test_it_carries_no_secrets(self, package):
        job, _ = package
        names = self._names(job)
        assert not any(name.endswith(".env") or ".env/" in name for name in names)
        for suspicious in ("client_secret", "token", "credential", "api_key"):
            assert not any(suspicious in name.lower() for name in names), suspicious

    def test_it_carries_no_symlinks(self, package):
        import tarfile

        job, _ = package
        with tarfile.open(self._archive(job)) as tar:
            assert not any(member.issym() or member.islnk() for member in tar.getmembers())

    def test_it_carries_the_artifacts_the_render_needs(self, package):
        job, _ = package
        names = self._names(job)
        assert any(name.endswith("scene_plan.json") for name in names)
        assert any(name.endswith("job.json") for name in names)
        assert any("/anchor/" in name for name in names)

    def test_the_paths_stay_relative(self, package):
        job, _ = package
        assert all(not name.startswith("/") and ".." not in name for name in self._names(job))

    @staticmethod
    def _archive(job) -> Path:
        if isinstance(job, (str, Path)):
            return Path(job)
        for attribute in ("archive_path", "path", "package_path", "tar_path"):
            value = getattr(job, attribute, None)
            if value:
                return Path(value)
        raise AssertionError(f"no archive path on {job!r}")

    @classmethod
    def _names(cls, job) -> list[str]:
        import tarfile

        with tarfile.open(cls._archive(job)) as tar:
            return tar.getnames()


# ---------------------------------------------------------------------------
# Byte-level integrity (WP-6A)
# ---------------------------------------------------------------------------


class TestTheApprovedBytesAreTheRenderedBytes:
    """A signature belongs to footage, not to a path.

    Each test here tampers with a finished episode and then asks a different
    part of the system whether it notices. The episode is rebuilt per test
    because tampering is destructive; that is slower than sharing the module
    fixture and it is the only honest way to run these.
    """

    @staticmethod
    def _tamper(world, scene_index: int = 0) -> Path:
        entry = world.anchor_manifest["scenes"][scene_index]
        clip = world.episode_dir / entry["video_path"]
        clip.write_bytes(clip.read_bytes() + b"\x00tampered")
        return clip

    @pytest.fixture
    def approved(self, tmp_path):
        """An episode whose anchor clips are generated and approved."""
        from btcedu.core import anchor_review

        world = build_world(tmp_path / "integrity", real_media=True)
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "sceneplan")
            run_stage(world, "anchorgen")
            approve_anchor_gate(world)
            yield world, anchor_review
        world.close()

    def test_the_manifest_records_the_bytes_of_every_clip(self, approved):
        world, _ = approved
        import hashlib

        for entry in world.anchor_manifest["scenes"]:
            recorded = entry.get("file_sha256") or ""
            assert len(recorded) == 64
            clip = world.episode_dir / entry["video_path"]
            assert recorded == hashlib.sha256(clip.read_bytes()).hexdigest()

    def test_the_ledger_records_the_same_bytes(self, approved):
        world, _ = approved
        from btcedu.core.avatar_jobs import episode_jobs

        recorded = {
            entry["scene_id"]: entry["file_sha256"] for entry in world.anchor_manifest["scenes"]
        }
        for job in episode_jobs(world.session, world.episode_id):
            if job.scene_id in recorded:
                assert job.file_sha256 == recorded[job.scene_id]

    def test_an_untouched_episode_keeps_its_approval(self, approved):
        world, review = approved
        assert review.approval_blockers(world.session, world.episode_id, world.settings) == []

    def test_a_changed_clip_makes_the_approval_stale(self, approved):
        world, review = approved
        before = review.review_hash(world.session, world.episode_id, world.settings)
        self._tamper(world)
        after = review.review_hash(world.session, world.episode_id, world.settings)
        assert after != before

    def test_a_changed_clip_blocks_a_new_approval(self, approved):
        world, review = approved
        self._tamper(world)
        blockers = review.approval_blockers(world.session, world.episode_id, world.settings)
        assert any("changed on disk" in blocker for blocker in blockers)
        assert any("regeneration" in blocker for blocker in blockers)

    def test_a_changed_manifest_alone_also_invalidates_it(self, approved):
        world, review = approved
        before = review.review_hash(world.session, world.episode_id, world.settings)
        manifest_file = world.episode_dir / "anchor" / "manifest.json"
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        data["scenes"][0]["content_hash"] = "rewritten-by-hand"
        manifest_file.write_text(json.dumps(data), encoding="utf-8")
        assert review.review_hash(world.session, world.episode_id, world.settings) != before

    def test_clip_and_manifest_changed_together_still_do_not_pass(self, approved):
        """The classic forgery: rewrite the file *and* the digest beside it."""
        world, review = approved
        import hashlib

        clip_path = self._tamper(world)
        manifest_file = world.episode_dir / "anchor" / "manifest.json"
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        data["scenes"][0]["file_sha256"] = hashlib.sha256(clip_path.read_bytes()).hexdigest()
        manifest_file.write_text(json.dumps(data), encoding="utf-8")

        # The bytes and the manifest agree again, so nothing is *broken* — and
        # that is exactly the forgery this has to survive. The approval was
        # given to a digest describing the old footage, so it must no longer
        # count, even though every internal cross-check is consistent.
        assert review.approval_blockers(world.session, world.episode_id, world.settings) == []
        assert not review.has_current_approval(
            world.session, world.episode_id, world.settings
        )

    def test_a_changed_clip_blocks_the_local_render(self, approved):
        from btcedu.core.avatar_integrity import ClipIntegrityError
        from btcedu.core.scene_renderer import load_scene_context

        world, _ = approved
        self._tamper(world)
        with pytest.raises(ClipIntegrityError):
            load_scene_context(world.episode_dir, world.settings, world.episode)

    def test_a_changed_clip_blocks_the_remote_package(self, approved, tmp_path):
        from btcedu.core.avatar_integrity import ClipIntegrityError
        from btcedu.core.remote_render import build_job_package

        world, _ = approved
        self._tamper(world)
        with pytest.raises(ClipIntegrityError):
            build_job_package(
                world.session, world.episode_id, world.settings, tmp_path / "pkg", False
            )

    def test_the_remote_runner_notices_bytes_that_changed_in_transit(self, approved):
        from btcedu.core.remote_render import scene_job_requirements, verify_job_completeness

        world, _ = approved
        job = {
            "scene_render": scene_job_requirements(
                world.episode_dir, world.settings, world.episode
            )
        }
        assert job["scene_render"]["clip_digests"]
        verify_job_completeness(world.episode_dir, job)

        self._tamper(world)
        with pytest.raises(RuntimeError) as excinfo:
            verify_job_completeness(world.episode_dir, job)
        assert "do not match the digests" in str(excinfo.value)

    def test_a_changed_clip_never_causes_a_new_order(self, approved):
        world, _ = approved
        self._tamper(world)
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            result = run_stage(world, "anchorgen", force=True)
        assert doubles["heygen"].orders == []
        assert result.status == "failed"

    def test_the_money_stays_on_the_ledger(self, approved):
        """A broken clip is a problem to resolve, not a purchase to forget."""
        world, _ = approved
        from btcedu.core.avatar_jobs import episode_jobs

        before = sum(job.cost_usd for job in episode_jobs(world.session, world.episode_id))
        self._tamper(world)
        with Patcher() as patcher:
            install_fake_providers(patcher, world)
            run_stage(world, "anchorgen", force=True)
        after = sum(job.cost_usd for job in episode_jobs(world.session, world.episode_id))
        assert after == pytest.approx(before)

    def test_publish_stays_blocked_while_a_clip_is_broken(self, approved):
        world, _ = approved
        self._tamper(world)
        with Patcher() as patcher:
            doubles = install_fake_providers(patcher, world)
            run_stage(world, "render")
            result = run_stage(world, "publish")
        assert doubles["youtube"].uploads == []
        assert result.status == "failed"

    def test_the_dashboard_is_told_what_is_wrong_and_no_more(self, approved):
        world, review = approved
        self._tamper(world)
        state = review.collect_state(world.session, world.episode_id, world.settings)
        payload = json.dumps(state.to_dict())
        assert "changed on disk" in payload
        for secret in (str(world.settings.heygen_api_key), "Bearer", "client_secret"):
            if secret:
                assert secret not in payload


# ---------------------------------------------------------------------------
# Other profiles are untouched
# ---------------------------------------------------------------------------


class TestNothingElseChanged:
    def test_a_profile_without_the_avatar_never_orders_one(self, tmp_path):
        world = build_world(
            tmp_path / "off", real_media=True, settings_overrides={"anchor_enabled": False}
        )
        try:
            with Patcher() as patcher:
                doubles = install_fake_providers(patcher, world)
                run_stage(world, "sceneplan")
                result = run_stage(world, "anchorgen")
            assert result.status == "skipped"
            assert doubles["heygen"].orders == []
        finally:
            world.close()

    def test_the_shipped_profile_still_refuses_to_be_ready(self):
        """No synthetic fixture may make the production profile look prepared."""
        import yaml

        from btcedu.core.anchor_config import parse_looks

        source = Path("btcedu/profiles/tagesschau_tr.yaml")
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        looks = parse_looks(data["stage_config"]["anchor"]["looks"])
        assert looks
        assert all(not look.active for look in looks)
        assert all(look.avatar_look_id == PLACEHOLDER_LOOK_ID for look in looks)


class TestTheFixturesAreObviouslySynthetic:
    def test_every_identifier_is_marked(self, finished):
        world, _, doubles = finished
        assert all(
            smoke.SMOKE_MARKER in entry["avatar_look_id"]
            for entry in world.anchor_manifest["scenes"]
        )
        orders = doubles["heygen"].orders
        assert all(smoke.SMOKE_MARKER in order["provider_job_id"] for order in orders)

    def test_the_sections_describe_a_plausible_bulletin(self):
        sections = bulletin_sections()
        assert len(sections) == 11
        assert sections[0].purpose == "opening"
        assert sections[-1].purpose == "closing"
        assert any(section.is_weather for section in sections)
        assert replace(sections[0], title="x").title == "x"
