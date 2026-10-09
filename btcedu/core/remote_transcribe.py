"""Transcription on a GitHub Actions runner.

The Pi packs one audio file, the runner transcribes it with the very same
:class:`~btcedu.services.transcription_service.FasterWhisperTranscriptionProvider`
the Pi would have used, and the timestamped segments come back as a run
artifact. There is no second transcription implementation; the runner merely
has the CPU to afford ``large-v3-turbo``, which takes the Pi hours.

Same plumbing and guards as :mod:`btcedu.core.remote_render`: a temporary
draft release carries the payload, the runner must check out the commit the Pi
is on, and release and artifact are deleted afterwards.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

from btcedu.config import Settings
from btcedu.core.remote_render import (
    _safe_extract,
    current_branch,
    current_git_commit,
    remote_branch_head,
    resolve_repo,
)
from btcedu.services.transcription_service import (
    ProviderTranscript,
    ProviderTranscriptSegment,
)

logger = logging.getLogger(__name__)

JOB_ARCHIVE_NAME = "transcribe-job.tar.gz"
RESULT_ARTIFACT_NAME = "transcribe-result"
RESULT_FILE_NAME = "transcript.json"
WORKDIR_PREFIX = "btcedu-remote-transcribe-"
WORKDIR_PARENT_NAME = ".transcribe-jobs"


def transcript_to_dict(transcript: ProviderTranscript) -> dict:
    return {
        "text": transcript.text,
        "audio_seconds": transcript.audio_seconds,
        "cost_usd": transcript.cost_usd,
        "segments": [
            {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "text": segment.text,
                "confidence": segment.confidence,
            }
            for segment in transcript.segments
        ],
    }


def transcript_from_dict(data: dict) -> ProviderTranscript:
    return ProviderTranscript(
        text=str(data["text"]),
        audio_seconds=float(data["audio_seconds"]),
        cost_usd=float(data.get("cost_usd", 0.0)),
        segments=[
            ProviderTranscriptSegment(
                start_seconds=float(item["start_seconds"]),
                end_seconds=float(item["end_seconds"]),
                text=str(item["text"]),
                confidence=item.get("confidence"),
            )
            for item in data["segments"]
        ],
    )


def build_job_package(
    audio_path: Path, *, model: str, language: str, commit: str, workdir: Path
) -> Path:
    """``job.json`` plus the audio file; nothing else leaves the Pi."""
    job = {
        "model": model,
        "language": language,
        "commit": commit,
        "audio": f"audio/{audio_path.name}",
    }
    archive = workdir / JOB_ARCHIVE_NAME
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(job, indent=2).encode("utf-8")
        info = tarfile.TarInfo("job.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        tar.add(audio_path, arcname=job["audio"])
    return archive


def read_job_package(archive: Path, dest: Path) -> tuple[dict, Path]:
    """Runner side: unpack safely and return the job and the audio path."""
    with tarfile.open(archive, "r:gz") as tar:
        _safe_extract(tar, dest)
    job = json.loads((dest / "job.json").read_text(encoding="utf-8"))
    audio = (dest / job["audio"]).resolve()
    if dest.resolve() not in audio.parents or not audio.is_file():
        raise ValueError(f"Job audio missing or outside the job: {job['audio']}")
    return job, audio


class GitHubFasterWhisperProvider:
    """faster-whisper on a GitHub runner; raises on any failure so the caller can fall back."""

    name = "faster_whisper_github"
    needs_chunking = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def transcribe(self, audio_path: str, *, model: str, language: str) -> ProviderTranscript:
        from btcedu.services.github_actions_service import (
            GitHubActionsClient,
            GitHubActionsError,
        )

        settings = self.settings
        if settings.dry_run:
            raise RuntimeError("Remote transcription is not available in dry-run mode")
        audio = Path(audio_path)
        repo = resolve_repo(settings)
        commit = current_git_commit()
        branch = current_branch()
        if remote_branch_head(branch) != commit:
            raise RuntimeError(
                f"Local HEAD {commit[:8]} is not origin/{branch}; the runner would "
                "transcribe with different code -- push first"
            )
        token = getattr(settings, "github_token", "") or os.environ.get("GITHUB_TOKEN", "")
        client = GitHubActionsClient(token, repo)
        workflow = settings.github_transcribe_workflow
        job_id = f"{audio.parent.name}-{uuid.uuid4().hex[:8]}"

        parent = Path(settings.outputs_dir).resolve() / WORKDIR_PARENT_NAME
        parent.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=parent))
        release: dict = {}
        run = None
        try:
            archive = build_job_package(
                audio, model=model, language=language, commit=commit, workdir=workdir
            )
            release = client.create_draft_release(
                tag=f"transcribe-job-{job_id}",
                body=f"Transcription input for {audio.parent.name}. Deleted automatically.",
            )
            client.upload_release_asset(release["id"], archive, JOB_ARCHIVE_NAME)
            client.dispatch_workflow(
                workflow,
                ref=branch,
                inputs={
                    "job_id": job_id,
                    "release_id": str(release["id"]),
                    "commit": commit,
                    "model": model,
                },
            )
            run = client.find_run(workflow, marker=job_id)
            logger.info("Remote transcription run for %s: %s", audio.parent.name, run.html_url)
            run = client.wait_for_run(
                run.id,
                timeout=settings.github_transcribe_timeout,
                poll_interval=settings.github_transcribe_poll_interval,
            )
            if not run.succeeded:
                detail = client.get_run_logs_summary(run.id)
                raise GitHubActionsError(
                    f"Remote transcription finished as '{run.conclusion}' ({run.html_url})"
                    + (f": {detail}" if detail else "")
                )
            extracted = client.download_artifact(run.id, RESULT_ARTIFACT_NAME, workdir / "result")
            result = json.loads((extracted / RESULT_FILE_NAME).read_text(encoding="utf-8"))
            if result.get("model") != model:
                raise RuntimeError(f"Runner used {result.get('model')!r}, expected {model!r}")
            transcript = transcript_from_dict(result["transcript"])
            logger.info(
                "Remote transcription of %s: %d segments, %.0fs audio in %.0fs on the runner",
                audio.parent.name,
                len(transcript.segments),
                transcript.audio_seconds,
                float(result.get("elapsed_seconds", 0.0)),
            )
            return transcript
        except Exception:
            if run is not None and not run.finished:
                client.cancel_run(run.id)
            raise
        finally:
            if release.get("id"):
                client.delete_release(release["id"])
            shutil.rmtree(workdir, ignore_errors=True)
