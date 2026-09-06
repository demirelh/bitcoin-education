"""What travels to a GitHub runner for a scene-based render -- and what must not.

A runner is a third party. It gets the frames it needs to encode and nothing
else: no keys, no ``.env``, no symlinks pointing back out of the package, and
no studio material the manifest does not actually name. It also renders and
only renders -- it never orders an avatar clip, synthesises speech or generates
an image.

Nothing here contacts GitHub or a provider.
"""

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.remote_render import (
    _studio_assets,
    is_secret_name,
    scene_job_requirements,
    verify_job_completeness,
)


@dataclass
class FakeEpisode:
    episode_id: str = "ep_1"
    content_profile: str = "tagesschau_tr"
    title: str = "Bulletin"
    status: str = "TTS_DONE"


def _scene(scene_id: str, order: int, **overrides) -> dict:
    data = {
        "scene_id": scene_id,
        "chapter_id": "ch_01",
        "order": order,
        "beat_index": 0,
        "speaker_role": "anchor_female",
        "purpose": "news",
        "segment_indices": [order - 1],
        "text_hash": "t",
        "audio_file": "tts/parts/ch_01_p0.mp3",
        "expected_duration_seconds": 1.0,
        "visual_mode": "studio_composite",
        "template_id": "studio_anchor_medium",
        "background_asset": "images/topic.png",
        "background_asset_type": "image",
        "display_zone_id": "main_wall",
        "display_fit_mode": "cover",
        "focus_point": [0.5, 0.5],
        "transition_in": "cut",
        "overlays": {},
        "needs_avatar": True,
    }
    data.update(overrides)
    return data


@pytest.fixture
def episode_dir(tmp_path) -> Path:
    root = tmp_path / "outputs" / "ep_1"
    (root / "anchor" / "clips").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "tts" / "parts").mkdir(parents=True)

    (root / "images" / "topic.png").write_bytes(b"topic")
    (root / "tts" / "parts" / "ch_01_p0.mp3").write_bytes(b"part")
    (root / "tts" / "ch_01.mp3").write_bytes(b"chapter")
    (root / "anchor" / "clips" / "sc_001.webm").write_bytes(b"clip")

    (root / "scene_plan.json").write_text(
        json.dumps(
            {
                "content_hash": "plan-7",
                "presenter_look_id": "look-A",
                "scenes": [_scene("sc_001", 1)],
            }
        ),
        encoding="utf-8",
    )
    (root / "anchor" / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "avatar_look_id": "look-A",
                "scenes": [
                    {
                        "scene_id": "sc_001",
                        "video_path": "anchor/clips/sc_001.webm",
                        "status": "completed",
                        "avatar_look_id": "look-A",
                    }
                ],
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def studio(tmp_path, monkeypatch) -> Path:
    """A studio package with one real and one undeclared file."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "assets" / "almanya24" / "studio"
    (root / "plate").mkdir(parents=True)
    (root / "fallback").mkdir(parents=True)
    (root / "plate" / "bg.png").write_bytes(b"plate")
    (root / "fallback" / "neutral.png").write_bytes(b"card")
    # Working material that the manifest does not name. It must stay here.
    (root / "plate" / "unused_layered_source.png").write_bytes(b"x" * 4096)

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "studio_version": "1.0.0",
                "asset_version": "1.0.0",
                "width": 1920,
                "height": 1080,
                "fps": 25,
                "alpha_mode": "alpha_webm",
                "background": {"path": "plate/bg.png", "kind": "image"},
                "fallback_display_media": {"path": "fallback/neutral.png", "kind": "image"},
                "display_zone": {
                    "zone_id": "main_wall",
                    "rect": {"x": 960, "y": 140, "width": 820, "height": 461},
                    "fit_mode": "cover",
                    "focus_point": [0.5, 0.4],
                    "presenter_free": True,
                },
                "presenter": {"anchor_x": 620, "anchor_y": 1080, "scale": 1.0},
                "logo_zone": {"x": 1660, "y": 60, "width": 200, "height": 80},
                "safe_areas": {
                    "lower_third": {"x": 120, "y": 780, "width": 1200, "height": 160},
                    "ticker": {"x": 0, "y": 960, "width": 1920, "height": 80},
                    "subtitle": {"x": 240, "y": 860, "width": 1440, "height": 96},
                },
            }
        ),
        encoding="utf-8",
    )
    return root


class TestSecretNames:
    @pytest.mark.parametrize(
        "name",
        [
            ".env",
            "episode/.env",
            "auth/client_secret.json",
            "data/token.json",
            "certs/server.pem",
            "keys/private.KEY",
            "failover_token",
        ],
    )
    def test_credentials_are_recognised(self, name):
        assert is_secret_name(name) is True

    @pytest.mark.parametrize(
        "name", ["images/topic.png", "tts/ch_01.mp3", "scene_plan.json", "environment.json"]
    )
    def test_render_inputs_are_not(self, name):
        assert is_secret_name(name) is False


class TestStudioAssets:
    def test_only_declared_files_are_shipped(self, studio, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory",
            lambda settings, episode: "assets/almanya24/studio",
        )

        shipped = _studio_assets(Settings(), FakeEpisode())

        assert "assets/almanya24/studio/manifest.json" in shipped
        assert "assets/almanya24/studio/plate/bg.png" in shipped
        assert "assets/almanya24/studio/fallback/neutral.png" in shipped
        assert not any("unused_layered_source" in path for path in shipped)

    def test_a_profile_without_a_studio_ships_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: ""
        )

        assert _studio_assets(Settings(), FakeEpisode()) == []

    def test_a_missing_studio_manifest_ships_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory",
            lambda settings, episode: "assets/almanya24/studio",
        )

        assert _studio_assets(Settings(), FakeEpisode()) == []

    def test_an_absolute_studio_directory_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: "/etc"
        )

        assert _studio_assets(Settings(), FakeEpisode()) == []


class TestSceneJobRequirements:
    def test_an_episode_without_a_plan_declares_nothing(self, tmp_path):
        root = tmp_path / "ep"
        root.mkdir()

        assert scene_job_requirements(root, Settings(), FakeEpisode()) == {}

    def test_the_declared_list_names_every_render_input(self, episode_dir, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: ""
        )

        job = scene_job_requirements(episode_dir, Settings(), FakeEpisode())

        assert job["scene_plan_hash"] == "plan-7"
        assert job["presenter_look_id"] == "look-A"
        assert job["scene_count"] == 1
        assert job["anchor_schema_version"] == "2.0"
        assert "scene_plan.json" in job["episode_files"]
        assert "anchor/manifest.json" in job["episode_files"]
        assert "anchor/clips/sc_001.webm" in job["episode_files"]
        assert "images/topic.png" in job["episode_files"]
        assert "tts/parts/ch_01_p0.mp3" in job["episode_files"]

    def test_a_path_leaving_the_episode_is_never_declared(self, episode_dir, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: ""
        )
        plan = json.loads((episode_dir / "scene_plan.json").read_text(encoding="utf-8"))
        plan["scenes"][0]["background_asset"] = "../../../etc/hosts"
        (episode_dir / "scene_plan.json").write_text(json.dumps(plan), encoding="utf-8")

        job = scene_job_requirements(episode_dir, Settings(), FakeEpisode())

        assert not any(".." in path for path in job["episode_files"])

    def test_an_unreadable_plan_declares_nothing(self, episode_dir):
        (episode_dir / "scene_plan.json").write_text("{ broken", encoding="utf-8")

        assert scene_job_requirements(episode_dir, Settings(), FakeEpisode()) == {}

    def test_the_studio_is_declared_alongside_the_episode_files(
        self, episode_dir, studio, monkeypatch
    ):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory",
            lambda settings, episode: "assets/almanya24/studio",
        )

        job = scene_job_requirements(episode_dir, Settings(), FakeEpisode())

        assert job["studio_dir"] == "assets/almanya24/studio"
        assert "assets/almanya24/studio/manifest.json" in job["studio_assets"]


class TestCompleteness:
    def test_a_complete_package_passes(self, episode_dir, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: ""
        )
        job = {"scene_render": scene_job_requirements(episode_dir, Settings(), FakeEpisode())}

        verify_job_completeness(episode_dir, job)

    def test_a_missing_avatar_clip_fails_closed(self, episode_dir, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory", lambda settings, episode: ""
        )
        job = {"scene_render": scene_job_requirements(episode_dir, Settings(), FakeEpisode())}
        (episode_dir / "anchor" / "clips" / "sc_001.webm").unlink()

        with pytest.raises(RuntimeError, match="incomplete"):
            verify_job_completeness(episode_dir, job)

    def test_a_missing_studio_asset_fails_closed(self, episode_dir, studio, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "btcedu.core.scene_renderer.studio_directory",
            lambda settings, episode: "assets/almanya24/studio",
        )
        job = {"scene_render": scene_job_requirements(episode_dir, Settings(), FakeEpisode())}
        (studio / "plate" / "bg.png").unlink()

        with pytest.raises(RuntimeError, match="bg.png"):
            verify_job_completeness(episode_dir, job, workdir=tmp_path)

    def test_an_episode_without_scenes_is_never_blocked(self, episode_dir):
        """The pre-scene-plan remote path is untouched."""
        verify_job_completeness(episode_dir, {})
        verify_job_completeness(episode_dir, {"scene_render": {}})


class TestPackaging:
    def _episode_with_secret(self, episode_dir: Path) -> Path:
        (episode_dir / ".env").write_text("ANTHROPIC_API_KEY=sk-secret", encoding="utf-8")
        (episode_dir / "auth").mkdir()
        (episode_dir / "auth" / "client_secret.json").write_text("{}", encoding="utf-8")
        return episode_dir

    def test_no_secret_reaches_the_package(self, episode_dir, tmp_path, monkeypatch):
        from btcedu.core.remote_render import _job_filter

        self._episode_with_secret(episode_dir)
        archive = tmp_path / "job.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(episode_dir, arcname="episode", filter=_job_filter(episode_dir))

        with tarfile.open(archive, "r:*") as tar:
            names = tar.getnames()

        assert not any(name.endswith(".env") for name in names)
        assert not any("client_secret" in name for name in names)
        assert "episode/scene_plan.json" in names
        assert "episode/anchor/clips/sc_001.webm" in names

    def test_a_symlink_out_of_the_episode_is_dropped(self, episode_dir, tmp_path):
        from btcedu.core.remote_render import _job_filter

        outside = tmp_path / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (episode_dir / "link.txt").symlink_to(outside)

        archive = tmp_path / "job.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(episode_dir, arcname="episode", filter=_job_filter(episode_dir))

        with tarfile.open(archive, "r:*") as tar:
            assert "episode/link.txt" not in tar.getnames()

    def test_render_outputs_are_still_excluded(self, episode_dir, tmp_path):
        from btcedu.core.remote_render import _job_filter

        (episode_dir / "render" / "segments").mkdir(parents=True)
        (episode_dir / "render" / "segments" / "ch_01.mp4").write_bytes(b"big")
        (episode_dir / "render" / "draft.mp4").write_bytes(b"bigger")

        archive = tmp_path / "job.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(episode_dir, arcname="episode", filter=_job_filter(episode_dir))

        with tarfile.open(archive, "r:*") as tar:
            names = tar.getnames()

        assert not any("render/segments" in name for name in names)
        assert "episode/render/draft.mp4" not in names
