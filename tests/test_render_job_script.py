"""Runner-side guards of scripts/render_job.py.

These are the checks that stop a remote render from quietly producing a
different video than the Pi would have produced: a missing font, a missing
profile asset, or any other drift that shows up in the input hash.
"""

import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import render_job  # noqa: E402

from btcedu.config import Settings  # noqa: E402

# ---------------------------------------------------------------------------
# profile assets
# ---------------------------------------------------------------------------


def test_assets_are_restored_where_the_profile_expects_them(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    (workdir / "assets" / "data" / "assets" / "demo").mkdir(parents=True)
    (workdir / "assets" / "data" / "assets" / "demo" / "intro.mp3").write_bytes(b"jingle")

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    render_job._place_assets(workdir, {"assets": ["data/assets/demo/intro.mp3"]})

    restored = cwd / "data" / "assets" / "demo" / "intro.mp3"
    assert restored.read_bytes() == b"jingle"


def test_a_declared_asset_that_is_missing_stops_the_render(tmp_path, monkeypatch):
    """Rendering on would silently drop the intro jingle."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="missing the asset"):
        render_job._place_assets(tmp_path / "work", {"assets": ["data/assets/demo/intro.mp3"]})


def test_no_assets_declared_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    render_job._place_assets(tmp_path / "work", {})


# ---------------------------------------------------------------------------
# input hash
# ---------------------------------------------------------------------------


def _job(hash_value: str) -> dict:
    return {
        "episode": {"episode_id": "ep-1", "content_profile": "tagesschau_tr"},
        "expected_content_hash": hash_value,
    }


def test_matching_hash_passes(monkeypatch):
    monkeypatch.setattr(
        "btcedu.core.renderer._current_render_content_hash", lambda *a, **k: "abc123"
    )
    render_job._verify_content_hash(None, Settings(), _job("abc123"))


def test_diverging_hash_stops_the_render(monkeypatch):
    """Otherwise the Pi rejects the result and immediately renders again."""
    monkeypatch.setattr(
        "btcedu.core.renderer._current_render_content_hash", lambda *a, **k: "different"
    )
    with pytest.raises(SystemExit, match="hash mismatch"):
        render_job._verify_content_hash(None, Settings(), _job("abc123"))


def test_no_expected_hash_skips_the_check(monkeypatch):
    """Older job packages have no hash; they must still render."""
    render_job._verify_content_hash(None, Settings(), _job(""))


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------


def test_font_mismatch_stops_the_render(monkeypatch):
    monkeypatch.setattr(
        "btcedu.services.ffmpeg_service.find_font_path",
        lambda name: "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    job = {
        "episode": {"content_profile": "tagesschau_tr"},
        "expected_font_file": "RobotoCondensed-Bold.ttf",
    }
    with pytest.raises(SystemExit, match="Font mismatch"):
        render_job._verify_font(Settings(), job)


def test_font_match_passes(monkeypatch):
    monkeypatch.setattr(
        "btcedu.services.ffmpeg_service.find_font_path",
        lambda name: "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf",
    )
    job = {
        "episode": {"content_profile": "tagesschau_tr"},
        "expected_font_file": "RobotoCondensed-Bold.ttf",
    }
    render_job._verify_font(Settings(), job)


def test_a_pi_on_the_fallback_font_is_not_a_mismatch(monkeypatch):
    """The runner must mirror the Pi, including when the Pi has no such font."""
    monkeypatch.setattr(
        "btcedu.services.ffmpeg_service.find_font_path",
        lambda name: "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    job = {
        "episode": {"content_profile": "tagesschau_tr"},
        "expected_font_file": "DejaVuSans-Bold.ttf",
    }
    render_job._verify_font(Settings(), job)


# ---------------------------------------------------------------------------
# what travels back to the Pi
# ---------------------------------------------------------------------------


def _episode_with_a_weather_render(tmp_path):
    ep = tmp_path / "ep"
    (ep / "render" / "segments" / "beats").mkdir(parents=True)
    (ep / "provenance").mkdir(parents=True)
    (ep / "images").mkdir(parents=True)
    (ep / "render" / "draft.mp4").write_bytes(b"draft")
    (ep / "render" / "segments" / "ch07.mp4").write_bytes(b"segment")
    (ep / "render" / "segments" / "beats" / "b0.mp4").write_bytes(b"scratch")
    (ep / "provenance" / "render_provenance.json").write_text("{}", encoding="utf-8")
    (ep / "images" / "ch07_weather.mp4").write_bytes(b"weather-video")
    (ep / "images" / "ch07_weather.mp4.provenance.json").write_text("{}", encoding="utf-8")
    (ep / "images" / "ch07_weather_scenes.json").write_text("{}", encoding="utf-8")
    (ep / "images" / "ch07_weather.png").write_bytes(b"card")
    (ep / "images" / "ch01.png").write_bytes(b"story")
    return ep


def _packed_names(tmp_path, episode_dir):
    out = tmp_path / "result.tar.gz"
    render_job._pack_result(episode_dir, out)
    with tarfile.open(out, "r:gz") as tar:
        return set(tar.getnames())


def test_the_weather_video_travels_back(tmp_path):
    """review_gate_3 checks images/chXX_weather.mp4 on the Pi.

    The renderer writes it outside render/, so it needs its own rule or the
    gate blocks every episode whose weather chapter became a video.
    """
    names = _packed_names(tmp_path, _episode_with_a_weather_render(tmp_path))

    assert "images/ch07_weather.mp4" in names
    assert "images/ch07_weather.mp4.provenance.json" in names
    assert "images/ch07_weather_scenes.json" in names


def test_images_the_pi_already_has_stay_on_the_runner(tmp_path):
    """Only render's own output comes back; re-uploading the rest is waste."""
    names = _packed_names(tmp_path, _episode_with_a_weather_render(tmp_path))

    assert "images/ch01.png" not in names
    assert "images/ch07_weather.png" not in names


def test_beat_scratch_still_stays_behind(tmp_path):
    names = _packed_names(tmp_path, _episode_with_a_weather_render(tmp_path))

    assert "render/draft.mp4" in names
    assert not any(name.startswith("render/segments/beats/") for name in names)
