"""Runner-side guards of scripts/render_job.py.

These are the checks that stop a remote render from quietly producing a
different video than the Pi would have produced: a missing font, a missing
profile asset, or any other drift that shows up in the input hash.
"""

import sys
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
