#!/usr/bin/env python3
"""Run one render job on a GitHub Actions runner.

The Pi packs an episode's render inputs (see ``btcedu/core/remote_render.py``),
this script unpacks them, rebuilds just enough database state for the real
renderer to run, and calls it. There is deliberately no second render
implementation: ``btcedu.core.renderer.render_video`` is the same code the Pi
would have executed.

Usage:
    python scripts/render_job.py --job render-job.tar.gz --out render-result.tar.gz
"""

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btcedu.config import Settings  # noqa: E402
from btcedu.core.remote_render import _safe_extract  # noqa: E402
from btcedu.core.renderer import render_video  # noqa: E402
from btcedu.db import Base, get_engine, get_session_factory  # noqa: E402
from btcedu.models.episode import Episode, EpisodeStatus  # noqa: E402
from btcedu.models.media_asset import Base as MediaBase  # noqa: E402

# Written back to the Pi. Mirrors _RESULT_PATHS in remote_render.
RESULT_PATHS = ("render", "provenance/render_provenance.json")

# The renderer builds the timed weather video into images/, not into render/.
# review_gate_3 checks that source asset on the Pi, so it has to travel back
# with the segments it was rendered into. Mirrors _RESULT_GLOBS.
RESULT_GLOBS = (
    "images/*_weather.mp4",
    "images/*_weather.mp4.provenance.json",
    "images/*_weather_scenes.json",
)

# Per-beat intermediates are ~370 MB of scratch data that get concatenated
# into the chapter segments. The Pi never reads them back, so they stay here.
RESULT_EXCLUDED = ("render/segments/beats",)


def _build_settings(job: dict, outputs_dir: Path, db_path: Path) -> Settings:
    """Reconstruct the Pi's render configuration on the runner.

    The runner has no ``.env``; without this the code defaults (preset=medium)
    would silently produce a different video than the Pi does.
    """
    overrides = dict(job.get("settings", {}))
    overrides["outputs_dir"] = str(outputs_dir)
    overrides["database_url"] = f"sqlite:///{db_path}"
    overrides["dry_run"] = False
    return Settings(**overrides)


def _effective_font(settings: Settings, profile_name: str) -> str:
    """The font the renderer will actually use (a profile override wins)."""
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(profile_name)
        cfg = (profile.stage_config.get("render", {}) if profile else {}) or {}
    except Exception:
        cfg = {}
    return str(cfg.get("font") or settings.render_font or "")


def _verify_font(settings: Settings, job: dict) -> None:
    """Refuse to render with a different typeface than the Pi would use.

    ``find_font_path`` falls back to DejaVuSans-Bold and only logs a warning.
    On the Pi that is a reasonable last resort, but here it would quietly
    return a video whose overlays are set in a different font than every
    episode before it -- and nothing downstream would notice. The Pi ships the
    font *file* it resolved, so this compares like for like: a Pi that is
    itself on the fallback expects the fallback here too.
    """
    expected = str(job.get("expected_font_file") or "")
    if not expected:
        return

    from btcedu.services.ffmpeg_service import find_font_path

    profile_name = job["episode"].get("content_profile") or "bitcoin_podcast"
    wanted = _effective_font(settings, profile_name)
    resolved = Path(find_font_path(wanted)).name if wanted else ""
    print(f"Font {wanted} -> {resolved} (Pi used {expected})", flush=True)
    if resolved != expected:
        raise SystemExit(
            f"Font mismatch: the Pi renders {wanted!r} with {expected!r}, this runner would "
            f"use {resolved!r}. Install the package providing {expected!r} before rendering."
        )


def _place_assets(workdir: Path, job: dict) -> None:
    """Put the profile's audio where the profile's relative paths expect it.

    Profiles reference files like ``data/assets/<profile>/intro.mp3``, which
    are git-ignored and therefore absent on a runner. They resolve against the
    working directory, so that is where they are restored.
    """
    source_root = workdir / "assets"
    declared = list(job.get("assets") or [])
    # The studio package travels the same way: relative paths that resolve
    # against the working directory, restored before anything is encoded.
    declared += list((job.get("scene_render") or {}).get("studio_assets") or [])
    for rel in dict.fromkeys(declared):
        source = source_root / rel
        if not source.is_file():
            raise SystemExit(f"Job package is missing the asset it declared: {rel}")
        target = Path.cwd() / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"Asset {rel} -> {target}", flush=True)


def _verify_scene_inputs(episode_dir: Path, job: dict) -> None:
    """Refuse a scene render whose inputs did not all arrive.

    A missing avatar clip does not make the render fail; it makes it produce a
    different, wrong video. Checking the declared list first is the only way to
    tell those two apart. The runner renders and nothing else -- it never
    orders an avatar clip, synthesises speech or generates an image.
    """
    from btcedu.core.remote_render import verify_job_completeness

    verify_job_completeness(episode_dir, job)
    scene_job = (job or {}).get("scene_render") or {}
    if scene_job:
        print(
            f"Scene render: {scene_job.get('scene_count', 0)} scenes, "
            f"plan {str(scene_job.get('scene_plan_hash') or '')[:12]}..., "
            f"{len(scene_job.get('studio_assets') or [])} studio assets",
            flush=True,
        )


def _verify_content_hash(session, settings: Settings, job: dict) -> None:
    """Refuse to render something the Pi would reject anyway.

    The Pi accepts a render only if it can recompute the same input hash. If
    the runner's view of the settings, profile, assets or episode differs, the
    render is wasted work and the Pi would immediately queue another one --
    an endless loop that is far cheaper to catch here.
    """
    expected = str(job.get("expected_content_hash") or "")
    if not expected:
        return

    from btcedu.core.renderer import _current_render_content_hash

    actual = _current_render_content_hash(session, job["episode"]["episode_id"], settings) or ""
    if actual != expected:
        raise SystemExit(
            f"Render input hash mismatch: the Pi expects {expected[:12]}..., this runner "
            f"computes {actual[:12]}.... The result would be rejected as out of date."
        )
    print(f"Render input hash matches the Pi: {expected[:12]}...", flush=True)


def _seed_episode(session, job: dict) -> str:
    """Insert the single episode row the renderer reads."""
    info = job["episode"]
    published_at = info.get("published_at")
    episode = Episode(
        episode_id=info["episode_id"],
        title=info.get("title") or info["episode_id"],
        url=info.get("url") or "",
        pipeline_version=int(info.get("pipeline_version") or 2),
        status=EpisodeStatus(info.get("status") or EpisodeStatus.TTS_DONE.value),
        published_at=datetime.fromisoformat(published_at) if published_at else None,
    )
    if info.get("content_profile"):
        episode.content_profile = info["content_profile"]
    if info.get("source"):
        episode.source = info["source"]
    session.add(episode)
    session.commit()
    return episode.episode_id


def _pack_result(episode_dir: Path, out_path: Path) -> None:
    excluded = {(episode_dir / rel).resolve() for rel in RESULT_EXCLUDED}

    def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        candidate = (episode_dir / info.name).resolve()
        if candidate in excluded or any(parent in excluded for parent in candidate.parents):
            return None
        return info

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz", compresslevel=1) as tar:
        for rel in RESULT_PATHS:
            source = episode_dir / rel
            if source.exists():
                tar.add(source, arcname=rel, filter=_keep)
        for pattern in RESULT_GLOBS:
            for source in sorted(episode_dir.glob(pattern)):
                tar.add(source, arcname=str(source.relative_to(episode_dir)), filter=_keep)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="Path to the render-job tar.gz")
    parser.add_argument("--out", required=True, help="Where to write the result tar.gz")
    parser.add_argument("--workdir", default="", help="Scratch directory")
    args = parser.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp())
    workdir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(args.job, "r:*") as tar:
        _safe_extract(tar, workdir)

    job = json.loads((workdir / "job.json").read_text(encoding="utf-8"))
    episode_id = job["episode"]["episode_id"]

    outputs_dir = workdir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    episode_dir = outputs_dir / episode_id
    shutil.move(str(workdir / "episode"), str(episode_dir))

    db_path = workdir / "render.db"
    settings = _build_settings(job, outputs_dir, db_path)

    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)  # MediaAsset uses its own declarative base
    session = get_session_factory(settings.database_url)()

    try:
        _seed_episode(session, job)
        _place_assets(workdir, job)
        _verify_scene_inputs(episode_dir, job)
        _verify_font(settings, job)
        _verify_content_hash(session, settings, job)
        print(
            f"Rendering {episode_id} at {settings.render_resolution}@{settings.render_fps} "
            f"(preset={settings.render_preset}, crf={settings.render_crf})",
            flush=True,
        )
        result = render_video(session, episode_id, settings, force=True)
        print(
            f"Rendered {result.segment_count} segments, "
            f"{result.total_duration_seconds:.1f}s, "
            f"{result.total_size_bytes / 1_048_576:.1f} MB",
            flush=True,
        )
    finally:
        session.close()

    _pack_result(episode_dir, Path(args.out))
    size_mb = Path(args.out).stat().st_size / 1_048_576
    print(f"Result package: {args.out} ({size_mb:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
