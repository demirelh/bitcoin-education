"""Run the render stage on a GitHub Actions runner instead of the Pi.

The Pi needs roughly 47 minutes for a ten-minute bulletin and saturates all
four cores while doing it (which is what tripped the hardware watchdog). A
hosted runner does the same work about 2.5x faster and leaves the Pi
responsive.

Only the ffmpeg work moves. Everything that needs the database stays here:
this module packs the episode's render inputs, hands them to the runner,
waits, unpacks the result and writes the same records the local renderer
would have written. ``btcedu/core/renderer.py`` remains the single render
implementation -- the runner imports and calls it too.

Transport: the input package goes up as an asset on a temporary draft release
(artifacts cannot be uploaded before a run exists) and the finished ``render/``
directory comes back as a run artifact.
"""

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.app_setting import get_setting, set_setting
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus

logger = logging.getLogger(__name__)

RENDER_MODE_KEY = "render_execution_mode"
VALID_RENDER_MODES = ("github", "local")

JOB_ARCHIVE_NAME = "render-job.tar.gz"
RESULT_ARTIFACT_NAME = "render-result"
RESULT_ARCHIVE_NAME = "render-result.tar.gz"

WORKDIR_PREFIX = "btcedu-remote-render-"

# Work directories live beside the episode outputs, not in the system temp
# directory: on the Pi /tmp is a RAM-backed tmpfs of 3.9 GB, while a rendered
# episode is ~800 MB and passes through here twice (downloaded archive, then
# extracted tree). Doing that in memory competed with ffmpeg for the same
# 7.6 GB and pushed the machine into swap. The outputs directory sits on the
# real disk, which has room to spare — and being on the same filesystem as the
# episode, the final handover becomes a rename instead of a copy.
WORKDIR_PARENT_NAME = ".render-jobs"

# How long a leftover work directory may survive before the next remote render
# removes it. Comfortably longer than a render (~25 min) so a directory in use
# by a concurrent run is never touched, short enough that the disk is not held
# by the debris of a killed run.
_WORKDIR_MAX_AGE_SECONDS = 6 * 3600

# Episode sub-paths the runner must not receive: they are render *outputs*.
# Shipping them would waste ~850 MB of upload for no benefit.
_JOB_EXCLUDED = ("render/segments", "render/draft.mp4", "render/draft_subtitled.mp4")

# What the runner sends back.
_RESULT_PATHS = ("render", "provenance/render_provenance.json")

# The timed weather video is built into images/ during render, and
# review_gate_3 validates it there. Without this it stays on the runner and
# the gate blocks every episode whose weather chapter became a video.
# Mirrors RESULT_GLOBS in scripts/render_job.py.
_RESULT_GLOBS = (
    "images/*_weather.mp4",
    "images/*_weather.mp4.provenance.json",
    "images/*_weather_scenes.json",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# mode selection
# ---------------------------------------------------------------------------


def get_render_mode(session: Session, settings: Settings) -> str:
    """Effective render mode: dashboard override wins over ``.env``."""
    configured = getattr(settings, "render_execution_mode", "github")
    try:
        mode = get_setting(session, RENDER_MODE_KEY, configured)
    except Exception:  # table missing (pre-migration) must not break rendering
        mode = configured
    return mode if mode in VALID_RENDER_MODES else "local"


def set_render_mode(session: Session, mode: str) -> str:
    """Persist the operator's render mode choice."""
    if mode not in VALID_RENDER_MODES:
        raise ValueError(f"Invalid render mode {mode!r}, expected one of {VALID_RENDER_MODES}")
    set_setting(session, RENDER_MODE_KEY, mode)
    return mode


# ---------------------------------------------------------------------------
# job package
# ---------------------------------------------------------------------------


def render_settings_snapshot(settings: Settings) -> dict:
    """The render-relevant settings, so the runner produces identical output.

    Deliberately a prefix whitelist: the runner must never see API keys. The
    ``github_render_*`` values steer the offload itself and are irrelevant on
    the far side, so they are excluded as well.
    """
    snapshot = {}
    for field in sorted(type(settings).model_fields):
        if not field.startswith("render_"):
            continue
        if field == "render_execution_mode":
            continue
        snapshot[field] = getattr(settings, field)
    return snapshot


def current_git_commit(repo_root: Path | None = None) -> str:
    """HEAD of the working tree, so the runner uses the very same code."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not determine git commit: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(repo_root: Path | None = None) -> str:
    """Branch name to dispatch against.

    ``workflow_dispatch`` only accepts branch or tag names, not commit SHAs,
    so the branch is the ref and the commit is verified on the runner.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not determine git branch: {exc}") from exc
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch or branch == "HEAD":
        raise RuntimeError("Not on a named branch; cannot dispatch a render run")
    return branch


def remote_branch_head(branch: str, repo_root: Path | None = None) -> str:
    """The commit the runner would check out for ``branch`` right now."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not query the remote: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Branch {branch!r} does not exist on origin")
    return result.stdout.split()[0]


def resolve_repo(settings: Settings, repo_root: Path | None = None) -> str:
    """Configured repo, else derived from the ``origin`` remote."""
    configured = getattr(settings, "github_render_repo", "") or ""
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not read git remote: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError("No github_render_repo configured and no origin remote")
    url = result.stdout.strip()
    # git@github.com:owner/repo.git | https://github.com/owner/repo.git
    slug = url.split("github.com", 1)[-1].lstrip(":/").removesuffix(".git")
    if "/" not in slug:
        raise RuntimeError(f"Could not derive owner/repo from remote {url!r}")
    return slug


def _job_filter(episode_dir: Path):
    excluded = {(episode_dir / rel).resolve() for rel in _JOB_EXCLUDED}
    root = episode_dir.resolve()

    def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # info.name is "episode/<relative path>"
        rel = info.name.split("/", 1)[1] if "/" in info.name else ""
        candidate = (root / rel).resolve() if rel else root
        if candidate in excluded or any(parent in excluded for parent in candidate.parents):
            return None
        return info

    return _keep


_PROFILE_ASSET_KEYS = ("intro_audio", "topic_intro_audio", "outro_audio", "music_bed")


def _profile_render_config(settings: Settings, episode) -> dict:
    """The profile's ``render`` block, or an empty one."""
    try:
        from btcedu.profiles import get_registry

        profile_name = getattr(episode, "content_profile", "") or "bitcoin_podcast"
        profile = get_registry(settings).get(profile_name)
        return (profile.stage_config.get("render", {}) if profile else {}) or {}
    except Exception:
        return {}


def _profile_assets(settings: Settings, episode) -> list[str]:
    """Audio the profile pulls in from outside the episode directory.

    These live under ``data/assets/``, which is git-ignored, so a runner that
    only has the repository has no copy of them. Without shipping them the
    intro jingle silently disappears from the video *and* the idempotency hash
    stops matching, which would leave the Pi re-rendering the same episode for
    ever.
    """
    cfg = _profile_render_config(settings, episode)
    assets: list[str] = []
    for key in _PROFILE_ASSET_KEYS:
        value = str(cfg.get(key) or "")
        if not value or value in assets:
            continue
        if Path(value).is_absolute():
            logger.warning("Profile asset %s=%r is absolute and cannot be shipped", key, value)
            continue
        if Path(value).is_file():
            assets.append(value)
    return assets


def _expected_font_file(settings: Settings, episode) -> str:
    """The font file the Pi would use, so the runner can prove it matches.

    ``find_font_path`` silently falls back to DejaVuSans-Bold when a font is
    missing. Comparing resolved *files* rather than font names is what makes a
    remote render provably identical: it catches a font missing on the runner
    without failing when the Pi itself is using the fallback.
    """
    try:
        cfg = _profile_render_config(settings, episode)
        wanted = str(cfg.get("font") or settings.render_font or "")
        if not wanted:
            return ""
        from btcedu.services.ffmpeg_service import find_font_path

        return Path(find_font_path(wanted)).name
    except Exception:  # a font hint must never break packing
        return ""


def _expected_content_hash(session: Session, episode_id: str, settings: Settings) -> str:
    """The idempotency hash the Pi will check the returned render against."""
    try:
        from btcedu.core.renderer import _current_render_content_hash

        return _current_render_content_hash(session, episode_id, settings) or ""
    except Exception:  # a hint must never break packing
        return ""


def build_job_package(
    session: Session,
    episode_id: str,
    settings: Settings,
    dest_dir: Path,
    force: bool = False,
) -> Path:
    """Pack render inputs plus a job descriptor into a tar.gz."""
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    episode_dir = Path(settings.outputs_dir) / episode_id
    if not episode_dir.is_dir():
        raise ValueError(f"No output directory for episode {episode_id}: {episode_dir}")

    job = {
        "schema_version": "1.0",
        "created_at": _utcnow().isoformat(),
        "episode": {
            "episode_id": episode.episode_id,
            "title": episode.title or "",
            "published_at": episode.published_at.isoformat() if episode.published_at else None,
            "status": episode.status.value,
            "pipeline_version": episode.pipeline_version,
            "content_profile": getattr(episode, "content_profile", "") or "",
            "source": getattr(episode, "source", "") or "",
            "url": "",  # never ship source URLs; the runner does not need them
        },
        "settings": render_settings_snapshot(settings),
        "expected_font_file": _expected_font_file(settings, episode),
        "assets": _profile_assets(settings, episode),
        # The runner recomputes this. Any drift in settings, profile, assets or
        # episode metadata changes it, and a mismatch means the result would be
        # rejected by render_is_current -- i.e. the Pi would re-render for ever.
        "expected_content_hash": _expected_content_hash(session, episode_id, settings),
        "force": bool(force),
    }

    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / JOB_ARCHIVE_NAME
    job_json = dest_dir / "job.json"
    job_json.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")

    # compresslevel=1: the payload is mostly PNG/MP3, already compressed, and
    # the Pi's CPU is the scarce resource here.
    with tarfile.open(archive_path, "w:gz", compresslevel=1) as tar:
        tar.add(job_json, arcname="job.json")
        tar.add(episode_dir, arcname="episode", filter=_job_filter(episode_dir))
        for rel in job["assets"]:
            tar.add(rel, arcname=f"assets/{rel}")
    job_json.unlink(missing_ok=True)

    logger.info(
        "Render job package for %s: %.1f MB",
        episode_id,
        archive_path.stat().st_size / 1_048_576,
    )
    return archive_path


def unpack_result(archive_path: Path, episode_dir: Path) -> None:
    """Replace the local render outputs with the runner's result.

    Staging happens next to the episode rather than in the system temp
    directory. Two reasons, both of which bit this machine: /tmp is a RAM-backed
    tmpfs here and the extracted tree is ~800 MB, and staging on the episode's
    own filesystem turns every ``shutil.move`` below into a rename instead of a
    byte-for-byte copy.
    """
    episode_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=episode_dir, prefix=".unpack-") as tmp:
        staging = Path(tmp)
        with tarfile.open(archive_path, "r:*") as tar:
            _safe_extract(tar, staging)

        for rel in _RESULT_PATHS:
            source = staging / rel
            if not source.exists():
                continue
            target = episode_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(source), str(target))
            else:
                shutil.move(str(source), str(target))

        for pattern in _RESULT_GLOBS:
            for source in sorted(staging.glob(pattern)):
                target = episode_dir / source.relative_to(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract while refusing paths that escape ``dest``."""
    root = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"Refusing to extract {member.name!r} outside {dest}")
    # filter="data" additionally strips absolute paths, links and odd metadata.
    tar.extractall(dest, filter="data")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _finalize(
    session: Session,
    episode: Episode,
    settings: Settings,
    episode_dir: Path,
    pipeline_run: PipelineRun,
) -> "object":
    """Write the records the local renderer would have written."""
    from btcedu.core.renderer import RenderResult, _create_media_asset_record

    render_dir = episode_dir / "render"
    manifest_path = render_dir / "render_manifest.json"
    draft_path = render_dir / "draft.mp4"
    provenance_path = episode_dir / "provenance" / "render_provenance.json"

    if not draft_path.exists():
        raise RuntimeError(f"Remote render returned no draft video at {draft_path}")
    if not manifest_path.exists():
        raise RuntimeError(f"Remote render returned no manifest at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    segments = manifest.get("segments", [])
    total_duration = float(manifest.get("total_duration_seconds", 0.0))
    total_size = int(manifest.get("total_size_bytes", 0))

    content_hash = ""
    if provenance_path.exists():
        try:
            content_hash = json.loads(provenance_path.read_text(encoding="utf-8")).get(
                "input_content_hash", ""
            )
        except (OSError, json.JSONDecodeError):
            content_hash = ""

    session.add(
        ContentArtifact(
            episode_id=episode.episode_id,
            artifact_type="render",
            file_path=str(manifest_path.relative_to(episode_dir)),
            prompt_hash=content_hash,
            model="ffmpeg",
            created_at=_utcnow(),
        )
    )
    if not settings.dry_run:
        _create_media_asset_record(
            session, episode.episode_id, draft_path, total_duration, total_size
        )

    episode.status = EpisodeStatus.RENDERED
    episode.error_message = None
    session.commit()

    pipeline_run.status = RunStatus.SUCCESS.value
    pipeline_run.completed_at = _utcnow()
    pipeline_run.estimated_cost_usd = 0.0
    session.commit()

    return RenderResult(
        episode_id=episode.episode_id,
        render_path=render_dir,
        manifest_path=manifest_path,
        provenance_path=provenance_path,
        draft_path=draft_path,
        segment_count=len(segments),
        total_duration_seconds=total_duration,
        total_size_bytes=total_size,
        skipped=False,
    )


def workdir_parent(settings: Settings) -> Path:
    """Where remote-render work directories are created."""
    return Path(settings.outputs_dir) / WORKDIR_PARENT_NAME


def sweep_stale_workdirs(
    settings: Settings, max_age_seconds: int = _WORKDIR_MAX_AGE_SECONDS
) -> int:
    """Delete work directories that an earlier render left behind.

    The normal path removes its directory in a ``finally``, but that clause
    cannot run when the process is killed — Ctrl-C on a manual run, a systemd
    stop, or the OOM killer. What stays behind is up to ~1.6 GB of archive and
    extracted video, which nothing else will ever clean up.

    Returns the number of directories removed.
    """
    removed = 0
    cutoff = time.time() - max_age_seconds
    try:
        candidates = list(workdir_parent(settings).glob(f"{WORKDIR_PREFIX}*"))
    except OSError:  # an unreadable directory must not break the render
        return 0

    for path in candidates:
        try:
            if not path.is_dir() or path.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            logger.info("Removed stale remote-render work directory %s", path)
        except OSError:
            logger.debug("Could not inspect %s", path, exc_info=True)
    return removed


def render_video_remote(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    progress_callback=None,
):
    """Render an episode on a GitHub Actions runner.

    Mirrors :func:`btcedu.core.renderer.render_video` in signature, guards and
    database effects. Raises on any failure so the caller can decide whether to
    fall back to a local render.
    """
    from btcedu.core.renderer import RenderResult, _write_render_progress, render_is_current
    from btcedu.services.github_actions_service import GitHubActionsClient, GitHubActionsError

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline "
            f"(pipeline_version={episode.pipeline_version}). Render is v2 only."
        )
    if (
        episode.status
        not in (
            EpisodeStatus.TTS_DONE,
            EpisodeStatus.SCENE_PLANNED,
            EpisodeStatus.ANCHOR_GENERATED,
            EpisodeStatus.RENDERED,
        )
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'rendered'. Use --force to override."
        )

    episode_dir = Path(settings.outputs_dir) / episode_id
    render_dir = episode_dir / "render"

    # Same idempotency contract as the local renderer: never ship 30 MB to a
    # runner for work that is already done.
    if not force:
        is_current, reason = render_is_current(session, episode_id, settings)
        if is_current:
            logger.info("Render already current for %s (%s), skipping", episode_id, reason)
            if episode.status in (
                EpisodeStatus.TTS_DONE,
                EpisodeStatus.SCENE_PLANNED,
                EpisodeStatus.ANCHOR_GENERATED,
            ):
                episode.status = EpisodeStatus.RENDERED
                session.commit()
            manifest_path = render_dir / "render_manifest.json"
            return RenderResult(
                episode_id=episode_id,
                render_path=render_dir,
                manifest_path=manifest_path,
                provenance_path=episode_dir / "provenance" / "render_provenance.json",
                draft_path=render_dir / "draft.mp4",
                skipped=True,
            )

    if settings.dry_run:
        raise RuntimeError("Remote render is not available in dry-run mode")

    repo = resolve_repo(settings)
    commit = current_git_commit()
    branch = current_branch()
    remote_head = remote_branch_head(branch)
    if remote_head != commit:
        raise RuntimeError(
            f"Local HEAD {commit[:8]} differs from origin/{branch} {remote_head[:8]}. "
            "The runner would render different code -- push first, or switch the "
            "render mode to local."
        )

    workflow = getattr(settings, "github_render_workflow", "render.yml")
    token = getattr(settings, "github_token", "") or os.environ.get("GITHUB_TOKEN", "")
    client = GitHubActionsClient(token, repo)

    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage=PipelineStage.RENDER,
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
        git_commit=commit,
    )
    session.add(pipeline_run)
    session.commit()

    job_id = f"{episode_id}-{uuid.uuid4().hex[:8]}"
    release: dict = {}
    run = None
    sweep_stale_workdirs(settings)
    parent = workdir_parent(settings)
    parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=parent))

    def _emit(payload: dict) -> None:
        try:
            _write_render_progress(render_dir, payload)
        except Exception:  # progress display must never break the render
            logger.debug("Could not write render progress", exc_info=True)
        if progress_callback is not None:
            try:
                progress_callback(payload)
            except Exception:
                logger.debug("progress_callback failed", exc_info=True)

    try:
        _emit(
            {
                "stage": "remote_upload",
                "chapter_title": "Uploading render job to GitHub",
                "progress_pct": 2,
            }
        )
        archive = build_job_package(session, episode_id, settings, workdir, force=force)

        release = client.create_draft_release(
            tag=f"render-job-{job_id}",
            body=f"Render inputs for {episode_id}. Deleted automatically.",
        )
        client.upload_release_asset(release["id"], archive, JOB_ARCHIVE_NAME)

        _emit(
            {
                "stage": "remote_dispatch",
                "chapter_title": "Starting GitHub runner",
                "progress_pct": 8,
            }
        )
        client.dispatch_workflow(
            workflow,
            ref=branch,
            inputs={
                "job_id": job_id,
                "episode_id": episode_id,
                "release_id": str(release["id"]),
                "commit": commit,
            },
        )
        run = client.find_run(workflow, marker=job_id)
        logger.info("Remote render run for %s: %s", episode_id, run.html_url)
        _emit(
            {
                "stage": "remote_running",
                "chapter_title": "Rendering on GitHub runner",
                "progress_pct": 15,
                "run_url": run.html_url,
            }
        )

        def _on_poll(current) -> None:
            _emit(
                {
                    "stage": "remote_running",
                    "chapter_title": f"GitHub runner: {current.status}",
                    "progress_pct": 40,
                    "run_url": current.html_url,
                }
            )

        run = client.wait_for_run(
            run.id,
            timeout=getattr(settings, "github_render_timeout", 5400),
            poll_interval=getattr(settings, "github_render_poll_interval", 20),
            on_poll=_on_poll,
        )
        if not run.succeeded:
            detail = client.get_run_logs_summary(run.id)
            raise GitHubActionsError(
                f"Remote render finished as '{run.conclusion}' ({run.html_url})"
                + (f": {detail}" if detail else "")
            )

        _emit(
            {
                "stage": "remote_download",
                "chapter_title": "Downloading rendered video",
                "progress_pct": 85,
                "run_url": run.html_url,
            }
        )
        extracted = client.download_artifact(run.id, RESULT_ARTIFACT_NAME, workdir / "result")
        result_archive = extracted / RESULT_ARCHIVE_NAME
        if not result_archive.exists():
            raise RuntimeError(f"Artifact did not contain {RESULT_ARCHIVE_NAME}")
        unpack_result(result_archive, episode_dir)

        result = _finalize(session, episode, settings, episode_dir, pipeline_run)
        logger.info(
            "Remote render complete for %s: %d segments, %.1fs",
            episode_id,
            result.segment_count,
            result.total_duration_seconds,
        )
        _emit(
            {
                "stage": "done",
                "chapter_title": "Render complete (GitHub)",
                "progress_pct": 100,
                "run_url": run.html_url,
            }
        )
        return result

    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)
        episode.error_message = str(exc)
        session.commit()
        if run is not None and not run.finished:
            client.cancel_run(run.id)
        _emit({"stage": "failed", "error": str(exc), "chapter_title": "Remote render failed"})
        logger.error("Remote render failed for %s: %s", episode_id, exc)
        raise
    finally:
        if release.get("id"):
            client.delete_release(release["id"])
        shutil.rmtree(workdir, ignore_errors=True)
