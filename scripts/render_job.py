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
