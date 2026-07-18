"""Background job manager for long-running pipeline tasks.

Uses a single-thread ThreadPoolExecutor so jobs queue up and execute
one at a time — safe for SQLite's single-writer constraint.
Jobs are stored in-memory; on process restart they are lost,
but the episode DB status is always the source of truth.
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask

logger = logging.getLogger(__name__)

# Stage weights for progress calculation (percentages)
# These weights represent the relative time/effort for each stage
STAGE_WEIGHTS = {
    "download": 3,
    "transcribe": 19,
    "transcript_analyze": 1,
    "correct": 7,
    "translate": 20,
    "adapt": 10,
    "chapterize": 5,
    "frameextract": 3,
    "imagegen": 7,
    "tts": 10,
    "render": 15,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    job_id: str
    episode_id: str
    action: str  # download|transcribe|chunk|generate|run|retry
    state: str = "queued"  # queued|running|success|error
    stage: str = ""
    message: str = ""
    force: bool = False
    dry_run: bool = False
    top_k: int = 16
    privacy: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    result: dict | None = None


@dataclass
class BatchJob:
    """Batch job for processing all pending episodes."""

    batch_id: str
    state: str = "queued"  # queued|running|stopped|success|error
    current_episode_id: str | None = None
    current_episode_title: str = ""
    current_stage: str = ""
    total_episodes: int = 0
    completed_episodes: int = 0
    failed_episodes: int = 0
    total_cost_usd: float = 0.0
    episode_ids: list[str] = field(default_factory=list)
    force: bool = False
    channel_id: str | None = None
    profile: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    message: str = ""
    stop_requested: bool = False
    # Progress tracking fields
    progress_pct: int = 0
    total_work: float = 0.0
    completed_work: float = 0.0


class JobManager:
    def __init__(self, logs_dir: str):
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="btcedu-job",
        )
        self._jobs: dict[str, Job] = {}
        self._batch_jobs: dict[str, BatchJob] = {}
        self._lock = threading.Lock()
        self._logs_dir = logs_dir
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
        (Path(logs_dir) / "episodes").mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        action: str,
        episode_id: str,
        app: Flask,
        force: bool = False,
        dry_run: bool = False,
        top_k: int = 16,
        privacy: str | None = None,
    ) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id,
            episode_id=episode_id,
            action=action,
            force=force,
            dry_run=dry_run,
            top_k=top_k,
            privacy=privacy,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._execute, job, app)
        logger.info("Job %s submitted: %s %s", job_id, action, episode_id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_episode(self, episode_id: str) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.episode_id == episode_id and job.state in ("queued", "running"):
                    return job
        return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Batch Job Public API
    # ------------------------------------------------------------------

    def submit_batch(
        self,
        app: Flask,
        force: bool = False,
        channel_id: str | None = None,
        profile: str | None = None,
    ) -> BatchJob:
        """Submit a batch job to process all pending episodes."""
        batch_id = uuid.uuid4().hex[:12]
        batch_job = BatchJob(batch_id=batch_id, force=force, channel_id=channel_id, profile=profile)
        with self._lock:
            self._batch_jobs[batch_id] = batch_job
        self._executor.submit(self._execute_batch, batch_job, app)
        logger.info(
            "Batch job %s submitted (channel_id=%s, profile=%s)", batch_id, channel_id, profile
        )
        return batch_job

    def get_batch(self, batch_id: str) -> BatchJob | None:
        """Get batch job status."""
        with self._lock:
            return self._batch_jobs.get(batch_id)

    def stop_batch(self, batch_id: str) -> bool:
        """Request graceful stop of a batch job."""
        with self._lock:
            batch_job = self._batch_jobs.get(batch_id)
            if batch_job and batch_job.state == "running":
                batch_job.stop_requested = True
                batch_job.updated_at = _utcnow()
                logger.info("Batch job %s stop requested", batch_id)
                return True
        return False

    def active_batch(self) -> BatchJob | None:
        """Check if there's an active batch job."""
        with self._lock:
            for batch_job in self._batch_jobs.values():
                if batch_job.state in ("queued", "running"):
                    return batch_job
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update(self, job: Job, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = _utcnow()

    def _update_batch(self, batch_job: BatchJob, **kwargs) -> None:
        """Update batch job fields thread-safely."""
        with self._lock:
            for key, value in kwargs.items():
                setattr(batch_job, key, value)
            batch_job.updated_at = _utcnow()

    def _broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast an SSE event. Silently ignores errors so pipeline never breaks."""
        try:
            from btcedu.web.api import broadcast_sse

            broadcast_sse(event_type, data)
        except Exception:
            pass

    def _log(self, job: Job, msg: str) -> None:
        ts = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{job.action}] {msg}\n"
        log_path = Path(self._logs_dir) / "episodes" / f"{job.episode_id}.log"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            logger.warning("Failed to write episode log: %s", log_path)

    # ------------------------------------------------------------------
    # Executor entry point
    # ------------------------------------------------------------------

    def _execute(self, job: Job, app: Flask) -> None:
        with app.app_context():
            session_factory = app.config["session_factory"]
            settings = app.config["settings"]
            session = session_factory()

            self._update(job, state="running", stage="starting")
            self._log(job, f"Starting {job.action} for {job.episode_id}")
            self._broadcast(
                "job_update",
                {
                    "job_id": job.job_id,
                    "episode_id": job.episode_id,
                    "state": "running",
                    "stage": "starting",
                    "action": job.action,
                },
            )

            try:
                _action_map = {
                    "download": self._do_download,
                    "transcribe": self._do_transcribe,
                    "transcript_analyze": self._do_transcript_analyze,
                    "correct": self._do_correct,
                    "segment": self._do_segment,
                    "translate": self._do_translate,
                    "adapt": self._do_adapt,
                    "chapterize": self._do_chapterize,
                    "frameextract": self._do_frameextract,
                    "imagegen": self._do_imagegen,
                    "anchorgen": self._do_anchorgen,
                    "tts": self._do_tts,
                    "render": self._do_render,
                    "publish": self._do_publish,
                    "run": self._do_full_pipeline,
                    "retry": self._do_retry,
                    "qa_rerun": self._do_qa_rerun,
                    "qa_rerun_all": self._do_qa_rerun_all,
                }
                handler = _action_map.get(job.action)
                if handler is None:
                    raise ValueError(f"Unknown action: {job.action}")

                # Serialize with any other pipeline run (e.g. the autostart
                # timer) to avoid concurrent SQLite writers deadlocking on the
                # database write lock during long stages like translate/render.
                from btcedu.core.runlock import pipeline_lock

                with pipeline_lock(settings):
                    handler(job, session, settings)

                self._update(job, state="success", stage="done")
                self._log(job, "Job completed successfully")
                self._broadcast(
                    "job_update",
                    {
                        "job_id": job.job_id,
                        "episode_id": job.episode_id,
                        "state": "success",
                        "stage": "done",
                        "action": job.action,
                        "result": job.result,
                    },
                )

            except Exception as e:
                logger.exception("Job %s failed", job.job_id)
                self._update(job, state="error", message=str(e))
                self._log(job, f"ERROR: {e}")
                self._broadcast(
                    "job_update",
                    {
                        "job_id": job.job_id,
                        "episode_id": job.episode_id,
                        "state": "error",
                        "action": job.action,
                        "message": str(e),
                    },
                )
            finally:
                session.close()

    # ------------------------------------------------------------------
    # Action runners — update stage/result but never set state
    # ------------------------------------------------------------------

    def _do_download(self, job, session, settings):
        from btcedu.core.detector import download_episode

        self._update(job, stage="downloading")
        self._log(job, "Downloading audio...")
        path = download_episode(session, job.episode_id, settings, force=job.force)
        self._update(job, result={"success": True, "path": path})
        self._log(job, f"Download complete: {path}")

    def _do_transcribe(self, job, session, settings):
        from btcedu.core.transcriber import transcribe_episode

        self._update(job, stage="transcribing")
        self._log(job, "Transcribing audio...")
        path = transcribe_episode(session, job.episode_id, settings, force=job.force)
        self._update(job, result={"success": True, "path": path})
        self._log(job, f"Transcription complete: {path}")

    def _do_transcript_analyze(self, job, session, settings):
        from btcedu.core.transcript_analyzer import analyze_transcript

        self._update(job, stage="transcript_analyze")
        self._log(job, "Analyzing transcript segments...")
        result = analyze_transcript(
            session,
            job.episode_id,
            settings,
            force=job.force,
        )
        self._update(
            job,
            result={
                "success": True,
                "suspicious_count": result.suspicious_count,
                "critical_count": result.critical_count,
            },
        )
        self._log(
            job,
            f"Transcript analysis complete: {result.suspicious_count} suspicious",
        )

    def _do_correct(self, job, session, settings):
        from btcedu.core.corrector import correct_transcript

        self._update(job, stage="correcting")
        self._log(job, "Correcting transcript...")
        result = correct_transcript(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "cost_usd", 0),
            },
        )
        self._log(job, "Correction complete")

    def _do_segment(self, job, session, settings):
        from btcedu.core.segmenter import segment_broadcast

        self._update(job, stage="segmenting")
        self._log(job, "Segmenting stories...")
        result = segment_broadcast(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "cost_usd", 0),
            },
        )
        self._log(job, "Segmentation complete")

    def _do_translate(self, job, session, settings):
        from btcedu.core.translator import translate_transcript

        self._update(job, stage="translating")
        self._log(job, "Translating content...")
        result = translate_transcript(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "total_cost_usd", 0),
            },
        )
        self._log(
            job,
            f"Translation complete: ${getattr(result, 'total_cost_usd', 0):.4f}",
        )

    def _do_adapt(self, job, session, settings):
        from btcedu.core.adapter import adapt_script

        self._update(job, stage="adapting")
        self._log(job, "Adapting content...")
        result = adapt_script(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "total_cost_usd", 0),
            },
        )
        self._log(job, "Adaptation complete")

    def _do_chapterize(self, job, session, settings):
        from btcedu.core.chapterizer import chapterize_script

        self._update(job, stage="chapterizing")
        self._log(job, "Creating chapters...")
        result = chapterize_script(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "total_cost_usd", 0),
            },
        )
        self._log(job, "Chapterization complete")

    def _do_frameextract(self, job, session, settings):
        from btcedu.core.frame_extractor import extract_frames

        self._update(job, stage="extracting_frames")
        self._log(job, "Extracting frames from source video...")
        extract_frames(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={"success": True},
        )
        self._log(job, "Frame extraction complete")

    def _do_imagegen(self, job, session, settings):
        from btcedu.models.episode import Episode

        # Dispatch on the profile's imagegen.provider, consistent with the full
        # pipeline (_run_stage): gemini_frame_edit → Gemini frame editing,
        # anything else → generative/stock image generation.
        ep = session.query(Episode).filter(Episode.episode_id == job.episode_id).first()

        from btcedu.core.pipeline import _imagegen_provider

        provider = _imagegen_provider(settings, ep) if ep else ""
        use_gemini = (
            provider == "gemini_frame_edit"
            and settings.gemini_image_edit_enabled
            and settings.gemini_api_key
        )

        if use_gemini:
            from btcedu.core.frame_editor import edit_frames

            self._update(job, stage="editing_frames")
            self._log(job, "Editing frames via Gemini...")
            result = edit_frames(session, job.episode_id, settings, force=job.force)
            self._update(
                job,
                result={
                    "success": True,
                    "edited": result.chapters_edited,
                    "skipped": result.chapters_skipped,
                    "cost_usd": result.total_cost_usd,
                },
            )
            self._log(
                job,
                f"Frame editing complete: {result.chapters_edited} edited, "
                f"${result.total_cost_usd:.4f}",
            )
        else:
            from btcedu.core.image_generator import generate_images

            self._update(job, stage="generating_images")
            self._log(job, "Generating images...")
            result = generate_images(session, job.episode_id, settings, force=job.force)
            self._update(
                job,
                result={
                    "success": True,
                    "generated": result.generated_count,
                    "placeholders": result.template_count,
                    "failed": result.failed_count,
                    "cost_usd": result.cost_usd,
                },
            )
            self._log(
                job,
                f"Image generation complete: {result.generated_count} generated, "
                f"{result.failed_count} failed (${result.cost_usd:.4f})",
            )

    def _do_anchorgen(self, job, session, settings):
        from btcedu.core.anchor_generator import generate_anchors

        self._update(job, stage="generating_anchor")
        self._log(job, "Generating D-ID anchor video...")
        result = generate_anchors(session, job.episode_id, settings, force=job.force)
        self._update(
            job,
            result={
                "success": True,
                "cost_usd": getattr(result, "cost_usd", 0),
            },
        )
        self._log(job, "Anchor video generation complete")

    def _do_tts(self, job, session, settings):
        from btcedu.core.tts import generate_tts

        self._update(job, stage="generating_tts")
        self._log(job, "Generating TTS audio...")
        result = generate_tts(
            session,
            job.episode_id,
            settings,
            force=job.force,
        )
        if result.skipped:
            self._update(
                job,
                result={"success": True, "skipped": True, "message": "Already up-to-date"},
            )
            self._log(job, "TTS already up-to-date (skipped)")
        else:
            self._update(
                job,
                result={
                    "success": True,
                    "segments": result.segment_count,
                    "duration_seconds": result.total_duration_seconds,
                    "characters": result.total_characters,
                    "cost_usd": result.cost_usd,
                },
            )
            self._log(
                job,
                f"TTS complete: {result.segment_count} segments, "
                f"{result.total_duration_seconds:.1f}s, ${result.cost_usd:.4f}",
            )

    def _do_render(self, job, session, settings):
        from btcedu.core.renderer import render_video

        self._update(job, stage="rendering_video")
        self._log(job, "Rendering draft video...")

        def _on_progress(evt: dict) -> None:
            stage = evt.get("stage", "")
            current = evt.get("current", 0)
            total = evt.get("total", 0)
            title = (evt.get("chapter_title") or evt.get("chapter_id") or "").strip()
            pct = int(evt.get("progress_pct", 0))
            if stage == "concat":
                label = f"concat {total} segments"
            else:
                short = title if len(title) <= 40 else title[:37] + "..."
                label = f"ch {current}/{total}: {short}" if short else f"ch {current}/{total}"
            self._update(job, stage=label, progress_pct=pct)
            self._broadcast(
                "job_update",
                {
                    "job_id": job.job_id,
                    "episode_id": job.episode_id,
                    "state": "running",
                    "stage": label,
                    "action": job.action,
                    "progress_pct": pct,
                    "render_current": current,
                    "render_total": total,
                    "render_chapter_id": evt.get("chapter_id"),
                    "render_chapter_title": title,
                    "render_stage": stage,
                },
            )
            if stage == "segment_done":
                self._log(job, f"Rendered {current}/{total}: {title}")

        result = render_video(
            session,
            job.episode_id,
            settings,
            force=job.force,
            progress_callback=_on_progress,
        )
        if result.skipped:
            self._update(
                job,
                result={"success": True, "skipped": True, "message": "Already up-to-date"},
            )
            self._log(job, "Render already up-to-date (skipped)")
        else:
            self._update(
                job,
                result={
                    "success": True,
                    "segments": result.segment_count,
                    "duration_seconds": result.total_duration_seconds,
                    "size_bytes": result.total_size_bytes,
                    "size_mb": result.total_size_bytes / 1024 / 1024,
                },
            )
            self._log(
                job,
                f"Render complete: {result.segment_count} segments, "
                f"{result.total_duration_seconds:.1f}s, "
                f"{result.total_size_bytes / 1024 / 1024:.1f}MB",
            )

    def _do_publish(self, job, session, settings):
        from btcedu.core.publisher import publish_video

        self._update(job, stage="publishing")
        self._log(job, "Publishing video to YouTube...")
        result = publish_video(
            session,
            job.episode_id,
            settings,
            force=job.force,
            privacy=job.privacy,
        )
        if result.skipped:
            self._update(
                job,
                result={
                    "success": True,
                    "skipped": True,
                    "youtube_video_id": result.youtube_video_id,
                    "youtube_url": result.youtube_url,
                },
            )
            self._log(job, f"Already published: {result.youtube_url}")
        else:
            self._update(
                job,
                result={
                    "success": True,
                    "dry_run": result.dry_run,
                    "youtube_video_id": result.youtube_video_id,
                    "youtube_url": result.youtube_url,
                    "publish_job_id": result.publish_job_id,
                },
            )
            status = "(dry-run)" if result.dry_run else ""
            self._log(job, f"Published {status}: {result.youtube_url}")

    def _do_full_pipeline(self, job, session, settings):
        from btcedu.core.pipeline import (
            resolve_pipeline_plan,
            run_episode_pipeline,
            write_report,
        )
        from btcedu.models.episode import Episode

        episode = (
            session.query(Episode)
            .filter(
                Episode.episode_id == job.episode_id,
            )
            .first()
        )
        if not episode:
            raise ValueError(f"Episode not found: {job.episode_id}")

        # Clear stale error (same as retry behavior)
        if episode.error_message:
            self._log(job, f"Clearing previous error: {episode.error_message}")
            episode.error_message = None
            session.commit()

        # Log the pipeline plan
        plan = resolve_pipeline_plan(session, episode, force=job.force, settings=settings)
        for p in plan:
            self._log(job, f"Plan: {p.stage} \u2192 {p.decision} ({p.reason})")

        run_stages = [p for p in plan if p.decision in ("run", "pending")]
        if not run_stages:
            self._log(job, "Nothing to do \u2014 all stages already completed")
            self._update(job, result={"success": True, "message": "Nothing to do"})
            return

        def on_stage(stage_name):
            self._update(job, stage=stage_name)
            self._log(job, f"Running: {stage_name}")

        # Execute via the same function CLI uses
        self._update(job, stage=run_stages[0].stage)
        report = run_episode_pipeline(
            session,
            episode,
            settings,
            force=job.force,
            stage_callback=on_stage,
        )
        write_report(report, settings.reports_dir)

        if report.success:
            self._update(
                job,
                result={
                    "success": True,
                    "cost_usd": report.total_cost_usd,
                    "stages_run": [sr.stage for sr in report.stages if sr.status == "success"],
                    "stages_skipped": [sr.stage for sr in report.stages if sr.status == "skipped"],
                },
            )
            self._log(job, f"Pipeline complete: ${report.total_cost_usd:.4f}")
        else:
            raise RuntimeError(report.error or "Pipeline failed")

    def _do_qa_rerun(self, job, session, settings):
        """Re-run translate + adapt so the QA second opinion feeds back into
        the prompts, then regenerate a fresh QA critique. Stops after QA.

        The translate and adapt stages read the previous ``qa_review.json`` and
        inject its findings via the ``{{ reviewer_feedback }}`` placeholder, so
        this closes the QA correction loop.
        """
        from btcedu.core.adapter import adapt_script
        from btcedu.core.qa_reviewer import generate_qa_review
        from btcedu.core.translator import translate_transcript

        self._update(job, stage="translate")
        self._log(job, "QA-Re-Run: Übersetzung (QA-Feedback wird angewendet)...")
        translate_transcript(session, job.episode_id, settings, force=True)

        self._update(job, stage="adapt")
        self._log(job, "QA-Re-Run: Adaption (QA-Feedback wird angewendet)...")
        adapt_script(session, job.episode_id, settings, force=True)

        self._update(job, stage="qa")
        self._log(job, "QA-Re-Run: neue QA-Zweitmeinung...")
        qa = generate_qa_review(session, job.episode_id, settings, force=True)
        score = getattr(qa, "overall_score", None)
        self._update(job, result={"success": True, "qa_score": score})
        self._log(job, f"QA-Re-Run abgeschlossen (Score={score})")

    def _do_qa_rerun_all(self, job, session, settings):
        """Re-run translate + adapt + QA, then continue the pipeline through
        the remaining stages (chapterize -> images -> tts -> render ...).

        First applies the QA correction loop (translate+adapt+QA), then resumes
        the pipeline from the adapt point so downstream artifacts are rebuilt
        from the corrected script.
        """
        from btcedu.core.pipeline import run_episode_pipeline, write_report
        from btcedu.models.episode import Episode

        # Step 1: translate + adapt + fresh QA (QA feedback applied).
        self._do_qa_rerun(job, session, settings)

        # Step 2: resume the pipeline (chapterize onward) with the corrected
        # script. Episode status is ADAPTED after adapt, so this rebuilds the
        # downstream stages via cascade invalidation.
        episode = session.query(Episode).filter(Episode.episode_id == job.episode_id).first()
        if not episode:
            raise ValueError(f"Episode not found: {job.episode_id}")

        def on_stage(stage_name):
            self._update(job, stage=stage_name)
            self._log(job, f"Running: {stage_name}")

        self._log(job, "QA-Re-Run: Pipeline wird bis Render fortgesetzt...")
        report = run_episode_pipeline(
            session,
            episode,
            settings,
            force=False,
            stage_callback=on_stage,
        )
        write_report(report, settings.reports_dir)

        if report.success:
            self._update(
                job,
                result={
                    "success": True,
                    "cost_usd": report.total_cost_usd,
                    "stages_run": [sr.stage for sr in report.stages if sr.status == "success"],
                },
            )
            self._log(job, f"QA-Re-Run (alles) abgeschlossen: ${report.total_cost_usd:.4f}")
        else:
            raise RuntimeError(report.error or "Pipeline failed")

    def _do_retry(self, job, session, settings):
        from btcedu.core.pipeline import (
            resolve_pipeline_plan,
            retry_episode,
            write_report,
        )
        from btcedu.models.episode import Episode, EpisodeStatus

        episode = (
            session.query(Episode)
            .filter(
                Episode.episode_id == job.episode_id,
            )
            .first()
        )
        if not episode:
            raise ValueError(f"Episode not found: {job.episode_id}")

        if not episode.error_message and episode.status != EpisodeStatus.FAILED:
            raise ValueError(f"Nothing to retry (status={episode.status.value}, no error)")

        self._update(job, stage="planning")
        self._log(job, f"Retrying from status: {episode.status.value}")
        self._log(job, f"Last error: {episode.error_message}")

        # Show what will happen after error is cleared
        plan = resolve_pipeline_plan(session, episode, force=False, settings=settings)
        for p in plan:
            self._log(job, f"Plan: {p.stage} \u2192 {p.decision} ({p.reason})")

        def on_stage(stage_name):
            self._update(job, stage=stage_name)
            self._log(job, f"Running: {stage_name}")

        self._update(job, stage="retrying")
        report = retry_episode(
            session,
            job.episode_id,
            settings,
            stage_callback=on_stage,
        )
        write_report(report, settings.reports_dir)

        if report.success:
            self._update(
                job,
                result={
                    "success": True,
                    "cost_usd": report.total_cost_usd,
                    "stages_run": [sr.stage for sr in report.stages if sr.status == "success"],
                },
            )
            self._log(job, f"Retry succeeded: ${report.total_cost_usd:.4f}")
        else:
            raise RuntimeError(report.error or "Retry failed")

    # ------------------------------------------------------------------
    # Batch Job Executor
    # ------------------------------------------------------------------

    def _calculate_episode_work(self, episode_status: str, force: bool = False) -> float:
        """Calculate the total work (in percentage points) for an episode.

        Returns the sum of weights for stages that need to run based on the
        episode's current status. If force=True, all stages run.
        """
        from btcedu.models.episode import EpisodeStatus

        if force:
            # All stages will run
            return sum(STAGE_WEIGHTS.values())

        # Map status to completed stages
        status_to_stages = {
            EpisodeStatus.NEW: [],
            EpisodeStatus.DOWNLOADED: ["download"],
            EpisodeStatus.TRANSCRIBED: ["download", "transcribe"],
            EpisodeStatus.CORRECTED: ["download", "transcribe", "correct"],
            EpisodeStatus.SEGMENTED: ["download", "transcribe", "correct"],
            EpisodeStatus.TRANSLATED: ["download", "transcribe", "correct", "translate"],
            EpisodeStatus.ADAPTED: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
            ],
            EpisodeStatus.CHAPTERIZED: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
                "chapterize",
            ],
            EpisodeStatus.FRAMES_EXTRACTED: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
                "chapterize",
                "frameextract",
            ],
            EpisodeStatus.IMAGES_GENERATED: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
                "chapterize",
                "frameextract",
                "imagegen",
            ],
            EpisodeStatus.TTS_DONE: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
                "chapterize",
                "frameextract",
                "imagegen",
                "tts",
            ],
            EpisodeStatus.ANCHOR_GENERATED: [
                "download",
                "transcribe",
                "correct",
                "translate",
                "adapt",
                "chapterize",
                "frameextract",
                "imagegen",
                "tts",
            ],
            EpisodeStatus.RENDERED: list(STAGE_WEIGHTS.keys()),
            EpisodeStatus.APPROVED: list(STAGE_WEIGHTS.keys()),
            EpisodeStatus.PUBLISHED: list(STAGE_WEIGHTS.keys()),
        }

        completed_stages = status_to_stages.get(episode_status, [])
        if episode_status not in (
            EpisodeStatus.NEW,
            EpisodeStatus.DOWNLOADED,
            EpisodeStatus.TRANSCRIBED,
        ):
            completed_stages = [*completed_stages, "transcript_analyze"]
        remaining_work = 0.0

        for stage, weight in STAGE_WEIGHTS.items():
            if stage not in completed_stages:
                remaining_work += weight

        return remaining_work

    def _execute_batch(self, batch_job: BatchJob, app: Flask) -> None:
        """Execute batch job to process all pending episodes."""
        with app.app_context():
            session_factory = app.config["session_factory"]
            settings = app.config["settings"]
            session = session_factory()

            # Serialize the whole batch with any other pipeline run (autostart
            # timer, single web jobs) so concurrent SQLite writers don't
            # deadlock on the database write lock.
            from btcedu.core.runlock import PipelineBusyError, pipeline_lock

            try:
                _batch_lock = pipeline_lock(settings)
                _batch_lock.__enter__()
            except PipelineBusyError:
                self._update_batch(
                    batch_job,
                    state="error",
                    message="Another pipeline run is active. Batch skipped; try again later.",
                )
                logger.warning(
                    "Batch job %s skipped: another pipeline run is active", batch_job.batch_id
                )
                session.close()
                return

            self._update_batch(batch_job, state="running")
            logger.info("Batch job %s started", batch_job.batch_id)

            try:
                from btcedu.models.episode import Episode, EpisodeStatus

                # Query pending episodes (oldest first)
                query = session.query(Episode).filter(
                    Episode.status.in_(
                        [
                            EpisodeStatus.NEW,
                            EpisodeStatus.DOWNLOADED,
                            EpisodeStatus.TRANSCRIBED,
                            EpisodeStatus.CORRECTED,
                            EpisodeStatus.SEGMENTED,
                            EpisodeStatus.TRANSLATED,
                            EpisodeStatus.ADAPTED,
                            EpisodeStatus.CHAPTERIZED,
                            EpisodeStatus.IMAGES_GENERATED,
                            EpisodeStatus.TTS_DONE,
                            EpisodeStatus.RENDERED,
                            EpisodeStatus.APPROVED,
                        ]
                    )
                )

                # Filter by channel if specified
                if batch_job.channel_id:
                    query = query.filter(Episode.channel_id == batch_job.channel_id)

                # Filter by profile if specified
                if batch_job.profile:
                    query = query.filter(Episode.content_profile == batch_job.profile)

                pending_episodes = query.order_by(Episode.published_at.asc()).all()

                episode_ids = [ep.episode_id for ep in pending_episodes]
                total = len(episode_ids)

                if total == 0:
                    self._update_batch(
                        batch_job,
                        state="success",
                        message="No pending episodes to process",
                        total_episodes=0,
                        progress_pct=100,
                    )
                    logger.info("Batch job %s: no pending episodes", batch_job.batch_id)
                    return

                # Calculate total work based on each episode's status
                total_work = 0.0
                for ep in pending_episodes:
                    total_work += self._calculate_episode_work(ep.status, batch_job.force)

                self._update_batch(
                    batch_job,
                    total_episodes=total,
                    episode_ids=episode_ids,
                    total_work=total_work,
                    completed_work=0.0,
                    progress_pct=0,
                )
                logger.info(
                    "Batch job %s: processing %d episodes (total work: %.1f)",
                    batch_job.batch_id,
                    total,
                    total_work,
                )

                # Process each episode
                for idx, episode_id in enumerate(episode_ids):
                    # Check if stop was requested
                    with self._lock:
                        if batch_job.stop_requested:
                            self._update_batch(
                                batch_job,
                                state="stopped",
                                message=f"Stopped after {idx} of {total} episodes",
                            )
                            logger.info("Batch job %s stopped by request", batch_job.batch_id)
                            return

                    # Update progress
                    self._update_batch(
                        batch_job,
                        current_episode_id=episode_id,
                        current_episode_title="",
                        current_stage="starting",
                    )

                    # Process the episode
                    try:
                        from btcedu.core.pipeline import (
                            run_episode_pipeline,
                            write_report,
                        )

                        episode = (
                            session.query(Episode)
                            .filter(
                                Episode.episode_id == episode_id,
                            )
                            .first()
                        )

                        if not episode:
                            logger.warning("Episode %s not found, skipping", episode_id)
                            continue

                        # Update with episode title
                        self._update_batch(
                            batch_job,
                            current_episode_title=episode.title,
                        )

                        # Clear error if present
                        if episode.error_message:
                            episode.error_message = None
                            session.commit()

                        def on_stage(stage_name):
                            # Check for stop during stage execution
                            with self._lock:
                                if batch_job.stop_requested:
                                    raise InterruptedError("Batch job stopped")

                            # Update current stage (stage is about to start)
                            self._update_batch(
                                batch_job,
                                current_stage=stage_name,
                            )

                        report = run_episode_pipeline(
                            session,
                            episode,
                            settings,
                            force=batch_job.force,
                            stage_callback=on_stage,
                        )
                        write_report(report, settings.reports_dir)

                        if report.success:
                            # Calculate work done for this episode
                            # Count stages that actually ran (not skipped)
                            work_done = sum(
                                STAGE_WEIGHTS.get(sr.stage, 0)
                                for sr in report.stages
                                if sr.status == "success"
                            )
                            new_completed = batch_job.completed_work + work_done
                            progress = 0
                            if batch_job.total_work > 0:
                                progress = min(100, int(100 * new_completed / batch_job.total_work))

                            self._update_batch(
                                batch_job,
                                completed_episodes=batch_job.completed_episodes + 1,
                                total_cost_usd=batch_job.total_cost_usd + report.total_cost_usd,
                                completed_work=new_completed,
                                progress_pct=progress,
                            )
                            self._broadcast(
                                "batch_update",
                                {
                                    "batch_id": batch_job.batch_id,
                                    "state": "running",
                                    "progress_pct": progress,
                                    "current_episode_id": episode_id,
                                    "completed_episodes": batch_job.completed_episodes + 1,
                                    "total_cost_usd": batch_job.total_cost_usd
                                    + report.total_cost_usd,
                                },
                            )
                            logger.info(
                                "Batch job %s: completed episode %s (%d/%d) - progress: %d%%",
                                batch_job.batch_id,
                                episode_id,
                                idx + 1,
                                total,
                                progress,
                            )
                        else:
                            # Episode failed - still count partial work if any stages succeeded
                            work_done = sum(
                                STAGE_WEIGHTS.get(sr.stage, 0)
                                for sr in report.stages
                                if sr.status == "success"
                            )
                            new_completed = batch_job.completed_work + work_done
                            progress = 0
                            if batch_job.total_work > 0:
                                progress = min(100, int(100 * new_completed / batch_job.total_work))

                            self._update_batch(
                                batch_job,
                                failed_episodes=batch_job.failed_episodes + 1,
                                completed_work=new_completed,
                                progress_pct=progress,
                            )
                            logger.warning(
                                "Batch job %s: episode %s failed: %s",
                                batch_job.batch_id,
                                episode_id,
                                report.error,
                            )

                    except InterruptedError:
                        # Stop was requested during stage execution
                        self._update_batch(
                            batch_job,
                            state="stopped",
                            message=f"Stopped during episode {episode_id} ({idx + 1}/{total})",
                        )
                        logger.info("Batch job %s interrupted", batch_job.batch_id)
                        return

                    except Exception:
                        logger.exception(
                            "Batch job %s: episode %s failed with exception",
                            batch_job.batch_id,
                            episode_id,
                        )
                        self._update_batch(
                            batch_job,
                            failed_episodes=batch_job.failed_episodes + 1,
                        )

                # All episodes processed
                self._update_batch(
                    batch_job,
                    state="success",
                    current_episode_id=None,
                    current_episode_title="",
                    current_stage="",
                    progress_pct=100,
                    message=f"Completed {batch_job.completed_episodes}/{total} episodes",
                )
                logger.info(
                    "Batch job %s completed: %d succeeded, %d failed, $%.4f total cost",
                    batch_job.batch_id,
                    batch_job.completed_episodes,
                    batch_job.failed_episodes,
                    batch_job.total_cost_usd,
                )
                self._broadcast(
                    "batch_update",
                    {
                        "batch_id": batch_job.batch_id,
                        "state": "success",
                        "progress_pct": 100,
                        "completed_episodes": batch_job.completed_episodes,
                        "failed_episodes": batch_job.failed_episodes,
                        "total_cost_usd": batch_job.total_cost_usd,
                    },
                )

            except Exception as e:
                logger.exception("Batch job %s failed", batch_job.batch_id)
                self._update_batch(
                    batch_job,
                    state="error",
                    message=str(e),
                )
                self._broadcast(
                    "batch_update",
                    {
                        "batch_id": batch_job.batch_id,
                        "state": "error",
                        "message": str(e),
                    },
                )
            finally:
                try:
                    _batch_lock.__exit__(None, None, None)
                finally:
                    session.close()
