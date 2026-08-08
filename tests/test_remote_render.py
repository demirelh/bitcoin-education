"""Tests for the GitHub Actions render offload.

No test may reach the network: the GitHub client is always mocked.
"""

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.core.remote_render import (
    JOB_ARCHIVE_NAME,
    RENDER_MODE_KEY,
    VALID_RENDER_MODES,
    _safe_extract,
    build_job_package,
    get_render_mode,
    render_settings_snapshot,
    resolve_repo,
    set_render_mode,
    unpack_result,
)
from btcedu.models.episode import Episode, EpisodeStatus


@pytest.fixture
def episode(db_session):
    ep = Episode(
        episode_id="ep-remote",
        title="Tagesschau",
        url="https://example.invalid/x",
        pipeline_version=2,
        status=EpisodeStatus.ANCHOR_GENERATED,
    )
    db_session.add(ep)
    db_session.commit()
    return ep


@pytest.fixture
def episode_dir(tmp_path, episode):
    """A miniature episode output directory with inputs and stale outputs."""
    base = tmp_path / "outputs" / episode.episode_id
    (base / "images").mkdir(parents=True)
    (base / "tts").mkdir()
    (base / "render" / "segments" / "beats").mkdir(parents=True)
    (base / "render" / "inputs").mkdir()

    (base / "chapters.json").write_text('{"chapters": []}', encoding="utf-8")
    (base / "images" / "ch01.png").write_bytes(b"png-bytes")
    (base / "tts" / "ch01.mp3").write_bytes(b"mp3-bytes")
    (base / "render" / "inputs" / "intro.mp3").write_bytes(b"intro")
    # Outputs that must NOT be shipped to the runner.
    (base / "render" / "draft.mp4").write_bytes(b"x" * 4096)
    (base / "render" / "segments" / "ch01.mp4").write_bytes(b"y" * 4096)
    (base / "render" / "segments" / "beats" / "b1.mp4").write_bytes(b"z" * 4096)
    return base


# ---------------------------------------------------------------------------
# mode switching
# ---------------------------------------------------------------------------


def test_render_mode_defaults_to_env_setting(db_session):
    settings = Settings(render_execution_mode="github")
    assert get_render_mode(db_session, settings) == "github"


def test_render_mode_override_wins_over_env(db_session):
    settings = Settings(render_execution_mode="github")
    set_render_mode(db_session, "local")
    assert get_render_mode(db_session, settings) == "local"


def test_render_mode_rejects_unknown_value(db_session):
    with pytest.raises(ValueError):
        set_render_mode(db_session, "aws")


def test_render_mode_falls_back_to_local_for_garbage_in_db(db_session):
    """A corrupt stored value must not send renders to an undefined target."""
    from btcedu.models.app_setting import set_setting

    set_setting(db_session, RENDER_MODE_KEY, "nonsense")
    assert get_render_mode(db_session, Settings()) == "local"


def test_valid_modes_are_the_two_documented_ones():
    assert VALID_RENDER_MODES == ("github", "local")


# ---------------------------------------------------------------------------
# settings snapshot
# ---------------------------------------------------------------------------


def test_snapshot_carries_render_settings():
    snapshot = render_settings_snapshot(Settings(render_fps=25, render_preset="ultrafast"))
    assert snapshot["render_fps"] == 25
    assert snapshot["render_preset"] == "ultrafast"


def test_snapshot_never_leaks_credentials():
    """The payload is uploaded to a release; secrets must not ride along."""
    settings = Settings(
        anthropic_api_key="sk-secret",
        elevenlabs_api_key="el-secret",
        github_token="ghp-secret",
    )
    snapshot = render_settings_snapshot(settings)
    serialized = json.dumps(snapshot)
    assert "secret" not in serialized
    assert all(key.startswith("render_") for key in snapshot)


def test_snapshot_excludes_the_mode_itself():
    """Shipping the mode would be meaningless on the runner and confusing."""
    assert "render_execution_mode" not in render_settings_snapshot(Settings())


def test_snapshot_covers_every_setting_in_the_render_fingerprint():
    """Any render_* setting the Pi uses must reach the runner.

    If one is missing the runner produces a different video *and* a different
    settings fingerprint, which would make the Pi re-render forever.
    """
    settings = Settings()
    expected = {f for f in type(settings).model_fields if f.startswith("render_")}
    expected.discard("render_execution_mode")
    assert set(render_settings_snapshot(settings)) == expected


# ---------------------------------------------------------------------------
# job package
# ---------------------------------------------------------------------------


def test_job_package_contains_inputs_and_descriptor(db_session, episode, episode_dir):
    settings = Settings(outputs_dir=str(episode_dir.parent))
    archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)

    assert archive.name == JOB_ARCHIVE_NAME
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        job = json.loads(tar.extractfile("job.json").read().decode())

    assert "episode/chapters.json" in names
    assert "episode/images/ch01.png" in names
    assert "episode/tts/ch01.mp3" in names
    assert "episode/render/inputs/intro.mp3" in names
    assert job["episode"]["episode_id"] == episode.episode_id
    assert job["settings"]["render_fps"] == settings.render_fps


def test_job_package_excludes_render_outputs(db_session, episode, episode_dir):
    """Segments and the draft are ~850 MB of output; shipping them is waste."""
    settings = Settings(outputs_dir=str(episode_dir.parent))
    archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())

    assert "episode/render/draft.mp4" not in names
    assert not any(name.startswith("episode/render/segments") for name in names)


def test_job_package_omits_source_url(db_session, episode, episode_dir):
    """The runner has no business knowing where the episode came from."""
    settings = Settings(outputs_dir=str(episode_dir.parent))
    archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)
    with tarfile.open(archive) as tar:
        job = json.loads(tar.extractfile("job.json").read().decode())
    assert job["episode"]["url"] == ""


def test_job_package_names_the_font_file_the_pi_resolved(db_session, episode, episode_dir):
    """A font missing on the runner must be detectable, not silently swapped.

    The runner compares resolved font *files*, so the descriptor has to carry
    the file name rather than the configured font name.
    """
    settings = Settings(outputs_dir=str(episode_dir.parent))
    episode.content_profile = "tagesschau_tr"  # this profile overrides the font
    db_session.commit()
    archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)
    with tarfile.open(archive) as tar:
        job = json.loads(tar.extractfile("job.json").read().decode())

    # The profile's font wins over the global setting, and it is the resolved
    # *file* that ships -- whatever this machine resolves it to.
    from btcedu.services.ffmpeg_service import find_font_path

    assert job["expected_font_file"] == Path(find_font_path("Roboto-Condensed-Bold")).name


def test_job_package_ships_the_profile_audio_the_runner_cannot_have(
    db_session, episode, episode_dir, tmp_path, monkeypatch
):
    """data/assets/ is git-ignored, so without this the intro jingle vanishes.

    A missing intro also changes the idempotency hash, which would leave the
    Pi re-rendering the same episode for ever.
    """
    monkeypatch.chdir(tmp_path)
    jingle = Path("data/assets/demo/intro.mp3")
    jingle.parent.mkdir(parents=True)
    jingle.write_bytes(b"jingle")

    settings = Settings(outputs_dir=str(episode_dir.parent))
    with patch(
        "btcedu.core.remote_render._profile_render_config",
        return_value={"intro_audio": str(jingle)},
    ):
        archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)

    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        job = json.loads(tar.extractfile("job.json").read().decode())

    assert job["assets"] == [str(jingle)]
    assert f"assets/{jingle}" in names


def test_job_package_skips_absolute_asset_paths(
    db_session, episode, episode_dir, tmp_path, monkeypatch
):
    """An absolute path cannot be recreated on the runner, so it is not shipped."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(outputs_dir=str(episode_dir.parent))
    with patch(
        "btcedu.core.remote_render._profile_render_config",
        return_value={"intro_audio": "/etc/hostname"},
    ):
        archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)

    with tarfile.open(archive) as tar:
        job = json.loads(tar.extractfile("job.json").read().decode())
    assert job["assets"] == []


def test_job_package_carries_the_hash_the_pi_will_check(db_session, episode, episode_dir):
    """The runner compares against this; a mismatch means a wasted render."""
    settings = Settings(outputs_dir=str(episode_dir.parent))
    with patch(
        "btcedu.core.renderer._current_render_content_hash", return_value="deadbeef"
    ):
        archive = build_job_package(db_session, episode.episode_id, settings, episode_dir.parent)
    with tarfile.open(archive) as tar:
        job = json.loads(tar.extractfile("job.json").read().decode())
    assert job["expected_content_hash"] == "deadbeef"


def test_job_package_rejects_unknown_episode(db_session, tmp_path):
    settings = Settings(outputs_dir=str(tmp_path))
    with pytest.raises(ValueError, match="Episode not found"):
        build_job_package(db_session, "does-not-exist", settings, tmp_path)


# ---------------------------------------------------------------------------
# result handling
# ---------------------------------------------------------------------------


def _make_result_archive(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    (staging / "render" / "segments").mkdir(parents=True)
    (staging / "provenance").mkdir(parents=True)
    (staging / "render" / "draft.mp4").write_bytes(b"new-draft")
    (staging / "render" / "render_manifest.json").write_text("{}", encoding="utf-8")
    (staging / "render" / "segments" / "ch01.mp4").write_bytes(b"new-segment")
    (staging / "provenance" / "render_provenance.json").write_text("{}", encoding="utf-8")

    archive = tmp_path / "render-result.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for rel in ("render", "provenance/render_provenance.json"):
            tar.add(staging / rel, arcname=rel)
    return archive


def test_unpack_result_replaces_local_render(tmp_path, episode_dir):
    archive = _make_result_archive(tmp_path)
    unpack_result(archive, episode_dir)

    assert (episode_dir / "render" / "draft.mp4").read_bytes() == b"new-draft"
    assert (episode_dir / "render" / "segments" / "ch01.mp4").read_bytes() == b"new-segment"
    assert (episode_dir / "provenance" / "render_provenance.json").exists()


def test_unpack_result_clears_stale_local_segments(tmp_path, episode_dir):
    """Mixing old and new segments would concatenate inconsistent video."""
    stale = episode_dir / "render" / "segments" / "ch99_old.mp4"
    stale.write_bytes(b"old")
    unpack_result(_make_result_archive(tmp_path), episode_dir)
    assert not stale.exists()


def _make_weather_result_archive(tmp_path: Path) -> Path:
    staging = tmp_path / "weather-staging"
    (staging / "render").mkdir(parents=True)
    (staging / "images").mkdir(parents=True)
    (staging / "render" / "render_manifest.json").write_text("{}", encoding="utf-8")
    (staging / "images" / "ch07_weather.mp4").write_bytes(b"weather-video")
    (staging / "images" / "ch07_weather.mp4.provenance.json").write_text(
        '{"method": "ffmpeg_scene_video"}', encoding="utf-8"
    )
    (staging / "images" / "ch07_weather_scenes.json").write_text(
        '{"scenes": []}', encoding="utf-8"
    )
    (staging / "images" / "ch07_weather.png").write_bytes(b"runner-card")

    archive = tmp_path / "weather-result.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging / "render", arcname="render")
        for name in (
            "ch07_weather.mp4",
            "ch07_weather.mp4.provenance.json",
            "ch07_weather_scenes.json",
            "ch07_weather.png",
        ):
            tar.add(staging / "images" / name, arcname=f"images/{name}")
    return archive


def test_unpack_result_brings_back_the_weather_video(tmp_path, episode_dir):
    """review_gate_3 validates images/chXX_weather.mp4, which render builds.

    It lives outside render/, so without an explicit rule it stays on the
    runner and the gate blocks the episode as 'source weather visual missing'.
    """
    unpack_result(_make_weather_result_archive(tmp_path), episode_dir)

    video = episode_dir / "images" / "ch07_weather.mp4"
    assert video.read_bytes() == b"weather-video"
    assert (episode_dir / "images" / "ch07_weather.mp4.provenance.json").exists()
    assert (episode_dir / "images" / "ch07_weather_scenes.json").exists()


def test_unpack_result_leaves_the_other_images_alone(tmp_path, episode_dir):
    """Only what render creates comes back; the Pi's own images stay put."""
    card = episode_dir / "images" / "ch07_weather.png"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_bytes(b"pi-card")

    unpack_result(_make_weather_result_archive(tmp_path), episode_dir)

    assert card.read_bytes() == b"pi-card"


def test_safe_extract_refuses_path_traversal(tmp_path):
    """A malicious archive must not be able to write outside the target."""
    evil = tmp_path / "evil.tar"
    victim = tmp_path / "payload"
    victim.write_text("x", encoding="utf-8")
    with tarfile.open(evil, "w") as tar:
        tar.add(victim, arcname="../escaped.txt")

    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(evil) as tar, pytest.raises(ValueError, match="Refusing to extract"):
        _safe_extract(tar, dest)


# ---------------------------------------------------------------------------
# repo resolution
# ---------------------------------------------------------------------------


def test_resolve_repo_prefers_explicit_setting():
    assert resolve_repo(Settings(github_render_repo="acme/widgets")) == "acme/widgets"


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:demirelh/bitcoin-education.git",
        "https://github.com/demirelh/bitcoin-education.git",
        "https://github.com/demirelh/bitcoin-education",
    ],
)
def test_resolve_repo_derives_slug_from_remote(remote):
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=f"{remote}\n")
        assert resolve_repo(Settings(github_render_repo="")) == "demirelh/bitcoin-education"


# ---------------------------------------------------------------------------
# orchestration guards
# ---------------------------------------------------------------------------


def test_remote_render_skips_when_draft_is_current(db_session, episode, episode_dir):
    """Never ship 30 MB to a runner for work that is already done."""
    from btcedu.core.remote_render import render_video_remote

    settings = Settings(outputs_dir=str(episode_dir.parent))
    with (
        patch("btcedu.core.renderer.render_is_current", return_value=(True, "hash match")),
        patch("btcedu.services.github_actions_service.GitHubActionsClient") as client,
    ):
        result = render_video_remote(db_session, episode.episode_id, settings)

    assert result.skipped is True
    client.assert_not_called()
    assert episode.status == EpisodeStatus.RENDERED


def test_remote_render_refuses_v1_episodes(db_session, tmp_path):
    from btcedu.core.remote_render import render_video_remote

    legacy = Episode(
        episode_id="v1-ep", title="old", url="u", pipeline_version=1, status=EpisodeStatus.NEW
    )
    db_session.add(legacy)
    db_session.commit()

    with pytest.raises(ValueError, match="v1 pipeline"):
        render_video_remote(db_session, "v1-ep", Settings(outputs_dir=str(tmp_path)))


def test_remote_render_refuses_when_local_head_is_unpushed(db_session, episode, episode_dir):
    """The runner checks out a branch; rendering other code would be silent corruption."""
    from btcedu.core.remote_render import render_video_remote

    settings = Settings(outputs_dir=str(episode_dir.parent), github_render_repo="a/b")
    with (
        patch("btcedu.core.renderer.render_is_current", return_value=(False, "changed")),
        patch("btcedu.core.remote_render.current_git_commit", return_value="a" * 40),
        patch("btcedu.core.remote_render.current_branch", return_value="main"),
        patch("btcedu.core.remote_render.remote_branch_head", return_value="b" * 40),
        pytest.raises(RuntimeError, match="differs from origin/main"),
    ):
        render_video_remote(db_session, episode.episode_id, settings)


def test_remote_render_refuses_dry_run(db_session, episode, episode_dir):
    from btcedu.core.remote_render import render_video_remote

    settings = Settings(outputs_dir=str(episode_dir.parent), dry_run=True)
    with (
        patch("btcedu.core.renderer.render_is_current", return_value=(False, "changed")),
        pytest.raises(RuntimeError, match="dry-run"),
    ):
        render_video_remote(db_session, episode.episode_id, settings)


# ---------------------------------------------------------------------------
# pipeline routing
# ---------------------------------------------------------------------------


def _render_result(**kwargs):
    defaults = dict(
        skipped=False, segment_count=8, total_duration_seconds=657.8, total_size_bytes=1024
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _stage_settings(tmp_path):
    return Settings(outputs_dir=str(tmp_path / "outputs"), pipeline_version=2)


def test_pipeline_routes_to_github_when_mode_is_github(db_session, episode, tmp_path):
    from btcedu.core.pipeline import _run_stage

    set_render_mode(db_session, "github")
    with (
        patch(
            "btcedu.core.remote_render.render_video_remote", return_value=_render_result()
        ) as remote,
        patch("btcedu.core.renderer.render_video") as local,
    ):
        result = _run_stage(db_session, episode, _stage_settings(tmp_path), "render")

    remote.assert_called_once()
    local.assert_not_called()
    assert result.status == "success"
    assert "[github]" in result.detail


def test_pipeline_routes_to_local_when_mode_is_local(db_session, episode, tmp_path):
    from btcedu.core.pipeline import _run_stage

    set_render_mode(db_session, "local")
    with (
        patch("btcedu.core.remote_render.render_video_remote") as remote,
        patch("btcedu.core.renderer.render_video", return_value=_render_result()) as local,
    ):
        result = _run_stage(db_session, episode, _stage_settings(tmp_path), "render")

    remote.assert_not_called()
    local.assert_called_once()
    assert "[local]" in result.detail


def test_pipeline_falls_back_to_local_when_github_fails(db_session, episode, tmp_path):
    """Losing an episode because GitHub was unreachable would be worse."""
    from btcedu.core.pipeline import _run_stage

    set_render_mode(db_session, "github")
    with (
        patch(
            "btcedu.core.remote_render.render_video_remote",
            side_effect=RuntimeError("runner exploded"),
        ) as remote,
        patch("btcedu.core.renderer.render_video", return_value=_render_result()) as local,
    ):
        result = _run_stage(db_session, episode, _stage_settings(tmp_path), "render")

    remote.assert_called_once()
    local.assert_called_once()
    assert result.status == "success"
    assert "[local]" in result.detail


def test_pipeline_fallback_clears_the_remote_error_message(db_session, episode, tmp_path):
    """A succeeded local render must not leave the failed remote error behind."""
    from btcedu.core.pipeline import _run_stage

    set_render_mode(db_session, "github")

    def _fail(session, *args, **kwargs):
        episode.error_message = "runner exploded"
        session.commit()
        raise RuntimeError("runner exploded")

    with (
        patch("btcedu.core.remote_render.render_video_remote", side_effect=_fail),
        patch("btcedu.core.renderer.render_video", return_value=_render_result()),
    ):
        _run_stage(db_session, episode, _stage_settings(tmp_path), "render")

    assert episode.error_message is None


def test_pipeline_propagates_failure_when_fallback_disabled(db_session, episode, tmp_path):
    from btcedu.core.pipeline import _run_stage

    set_render_mode(db_session, "github")
    settings = _stage_settings(tmp_path)
    settings.github_render_fallback_local = False

    with (
        patch(
            "btcedu.core.remote_render.render_video_remote",
            side_effect=RuntimeError("runner exploded"),
        ),
        patch("btcedu.core.renderer.render_video") as local,
    ):
        result = _run_stage(db_session, episode, settings, "render")

    local.assert_not_called()
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# dashboard endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'web.db'}")
    from btcedu.db import Base, get_engine
    from btcedu.web.app import create_app

    Base.metadata.create_all(get_engine(f"sqlite:///{tmp_path / 'web.db'}"))
    app = create_app()
    app.config["settings"].database_url = f"sqlite:///{tmp_path / 'web.db'}"
    return app.test_client()


def test_render_mode_endpoint_reports_current_mode(web_client):
    response = web_client.get("/api/render-mode")
    assert response.status_code == 200
    body = response.get_json()
    assert body["mode"] in VALID_RENDER_MODES
    assert body["modes"] == list(VALID_RENDER_MODES)


def test_render_mode_endpoint_persists_the_choice(web_client):
    assert web_client.post("/api/render-mode", json={"mode": "local"}).status_code == 200
    assert web_client.get("/api/render-mode").get_json()["mode"] == "local"

    assert web_client.post("/api/render-mode", json={"mode": "github"}).status_code == 200
    assert web_client.get("/api/render-mode").get_json()["mode"] == "github"


def test_render_mode_endpoint_rejects_invalid_mode(web_client):
    response = web_client.post("/api/render-mode", json={"mode": "aws"})
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_render_mode_endpoint_never_exposes_the_token(web_client):
    """The dashboard sits behind basic auth, but the token stays server-side."""
    body = web_client.get("/api/render-mode").get_json()
    assert "token" not in json.dumps(body).lower()
