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
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    result: dict | None = None


@dataclass
class BatchJob:
    """Batch job for processing all pending episodes."""

    batch_id: str
    state: str = "queued"  # queued|running|stopped|success|error
    current_episode_id: str | None = None
    current_stage: str = ""
    total_episodes: int = 0
    completed_episodes: int = 0
    failed_episodes: int = 0
    total_cost_usd: float = 0.0
    episode_ids: list[str] = field(default_factory=list)
    force: bool = False
    channel_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    message: str = ""
    stop_requested: bool = False


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
    ) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id,
            episode_id=episode_id,
            action=action,
            force=force,
            dry_run=dry_run,
            top_k=top_k,
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
        self, app: Flask, force: bool = False, channel_id: str | None = None
    ) -> BatchJob:
        """Submit a batch job to process all pending episodes."""
        batch_id = uuid.uuid4().hex[:12]
        batch_job = BatchJob(batch_id=batch_id, force=force, channel_id=channel_id)
        with self._lock:
            self._batch_jobs[batch_id] = batch_job
        self._executor.submit(self._execute_batch, batch_job, app)
        logger.info("Batch job %s submitted (channel_id=%s)", batch_id, channel_id)
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

            try:
                if job.action == "download":
                    self._do_download(job, session, settings)
                elif job.action == "transcribe":
                    self._do_transcribe(job, session, settings)
                elif job.action == "chunk":
                    self._do_chunk(job, session, settings)
                elif job.action == "generate":
                    self._do_generate(job, session, settings)
                elif job.action == "refine":
                    self._do_refine(job, session, settings)
                elif job.action == "run":
                    self._do_full_pipeline(job, session, settings)
                elif job.action == "retry":
                    self._do_retry(job, session, settings)
                else:
                    raise ValueError(f"Unknown action: {job.action}")

                self._update(job, state="success", stage="done")
                self._log(job, "Job completed successfully")

            except Exception as e:
                logger.exception("Job %s failed", job.job_id)
                self._update(job, state="error", message=str(e))
                self._log(job, f"ERROR: {e}")
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

    def _do_chunk(self, job, session, settings):
        from btcedu.core.transcriber import chunk_episode

        self._update(job, stage="chunking")
        self._log(job, "Chunking transcript...")
        count = chunk_episode(session, job.episode_id, settings, force=job.force)
        self._update(job, result={"success": True, "count": count})
        self._log(job, f"Chunking complete: {count} chunks")

    def _do_generate(self, job, session, settings):
        from btcedu.core.generator import generate_content

        self._update(job, stage="generating")
        self._log(job, "Generating content...")
        original_dry_run = settings.dry_run
        settings.dry_run = job.dry_run
        try:
            result = generate_content(
                session,
                job.episode_id,
                settings,
                force=job.force,
                top_k=job.top_k,
            )
        finally:
            settings.dry_run = original_dry_run
        self._update(
            job,
            result={
                "success": True,
                "artifacts": len(result.artifacts),
                "cost_usd": result.total_cost_usd,
                "input_tokens": result.total_input_tokens,
                "output_tokens": result.total_output_tokens,
            },
        )
        self._log(
            job,
            f"Generation complete: {len(result.artifacts)} artifacts, ${result.total_cost_usd:.4f}",
        )

    def _do_refine(self, job, session, settings):
        from btcedu.core.generator import refine_content

        self._update(job, stage="refining")
        self._log(job, "Refining content (v1 -> v2)...")
        result = refine_content(
            session,
            job.episode_id,
            settings,
            force=job.force,
        )
        self._update(
            job,
            result={
                "success": True,
                "artifacts": len(result.artifacts),
                "cost_usd": result.total_cost_usd,
                "input_tokens": result.total_input_tokens,
                "output_tokens": result.total_output_tokens,
            },
        )
        self._log(
            job,
            f"Refinement complete: {len(result.artifacts)} artifacts, ${result.total_cost_usd:.4f}",
        )

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
        plan = resolve_pipeline_plan(session, episode, force=job.force)
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
        plan = resolve_pipeline_plan(session, episode, force=False)
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

    def _execute_batch(self, batch_job: BatchJob, app: Flask) -> None:
        """Execute batch job to process all pending episodes."""
        with app.app_context():
            session_factory = app.config["session_factory"]
            settings = app.config["settings"]
            session = session_factory()

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
                            EpisodeStatus.CHUNKED,
                            EpisodeStatus.GENERATED,
                        ]
                    )
                )

                # Filter by channel if specified
                if batch_job.channel_id:
                    query = query.filter(Episode.channel_id == batch_job.channel_id)

                pending_episodes = query.order_by(Episode.published_at.asc()).all()

                episode_ids = [ep.episode_id for ep in pending_episodes]
                total = len(episode_ids)

                if total == 0:
                    self._update_batch(
                        batch_job,
                        state="success",
                        message="No pending episodes to process",
                        total_episodes=0,
                    )
                    logger.info("Batch job %s: no pending episodes", batch_job.batch_id)
                    return

                self._update_batch(
                    batch_job,
                    total_episodes=total,
                    episode_ids=episode_ids,
                )
                logger.info("Batch job %s: processing %d episodes", batch_job.batch_id, total)

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

                        # Clear error if present
                        if episode.error_message:
                            episode.error_message = None
                            session.commit()

                        def on_stage(stage_name):
                            # Check for stop during stage execution
                            with self._lock:
                                if batch_job.stop_requested:
                                    raise InterruptedError("Batch job stopped")
                            self._update_batch(batch_job, current_stage=stage_name)

                        report = run_episode_pipeline(
                            session,
                            episode,
                            settings,
                            force=batch_job.force,
                            stage_callback=on_stage,
                        )
                        write_report(report, settings.reports_dir)

                        if report.success:
                            self._update_batch(
                                batch_job,
                                completed_episodes=batch_job.completed_episodes + 1,
                                total_cost_usd=batch_job.total_cost_usd + report.total_cost_usd,
                            )
                            logger.info(
                                "Batch job %s: completed episode %s (%d/%d)",
                                batch_job.batch_id,
                                episode_id,
                                idx + 1,
                                total,
                            )
                        else:
                            self._update_batch(
                                batch_job,
                                failed_episodes=batch_job.failed_episodes + 1,
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
                    current_stage="",
                    message=f"Completed {batch_job.completed_episodes}/{total} episodes",
                )
                logger.info(
                    "Batch job %s completed: %d succeeded, %d failed, $%.4f total cost",
                    batch_job.batch_id,
                    batch_job.completed_episodes,
                    batch_job.failed_episodes,
                    batch_job.total_cost_usd,
                )

            except Exception as e:
                logger.exception("Batch job %s failed", batch_job.batch_id)
                self._update_batch(
                    batch_job,
                    state="error",
                    message=str(e),
                )
            finally:
                session.close()
