"""API blueprint for the btcedu web dashboard."""

import fcntl
import json
import logging
import queue
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func
from werkzeug.utils import secure_filename

from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)

# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) infrastructure
# ---------------------------------------------------------------------------

_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()
_weather_overrides_lock = threading.Lock()
_SSE_MAX_CLIENTS = 10  # Limit for Raspberry Pi
_INTRO_AUDIO_MAX_BYTES = 20 * 1024 * 1024


def _tagesschau_intro_audio_path() -> Path:
    settings = current_app.config["settings"]
    data_root = Path(settings.raw_data_dir).resolve().parent
    return data_root / "assets" / "tagesschau_tr" / "intro.mp3"


def broadcast_sse(event_type: str, data: dict) -> None:
    """Broadcast an SSE event to all connected clients.

    Called from jobs.py on state changes. Thread-safe.
    """
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass


@api_bp.route("/stream")
def sse_stream():
    """Server-Sent Events endpoint for live dashboard updates.

    Clients connect here and receive push notifications for job/batch/episode
    state changes instead of polling. Heartbeat every 15 s keeps the connection
    alive through Caddy and mobile proxies.
    """
    q: queue.Queue = queue.Queue(maxsize=30)

    with _sse_lock:
        if len(_sse_clients) >= _SSE_MAX_CLIENTS:
            # Drop oldest client to make room
            try:
                _sse_clients.pop(0)
            except IndexError:
                pass
        _sse_clients.append(q)

    def generate():
        try:
            # Send initial connected event
            yield f"event: connected\ndata: {json.dumps({'ts': datetime.now(UTC).isoformat()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except queue.Empty:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx/Caddy buffering
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@api_bp.route("/health")
def health():
    """Health check for monitoring and proxy verification."""
    from btcedu.version import get_git_commit

    return jsonify(
        {
            "status": "ok",
            "time": datetime.now(UTC).isoformat(),
            "version": "0.1.0",
            "git_commit": get_git_commit(),
        }
    )


@api_bp.route("/intro-audio", methods=["GET"])
def intro_audio_status():
    """Return metadata for the Tagesschau intro MP3."""
    path = _tagesschau_intro_audio_path()
    if not path.exists():
        return jsonify({"exists": False, "max_size_mb": 20})

    from btcedu.services.ffmpeg_service import probe_media

    try:
        media = probe_media(str(path))
    except RuntimeError:
        logger.exception("Configured intro audio is unreadable: %s", path)
        return jsonify({"error": "The stored intro MP3 is unreadable."}), 500

    return jsonify(
        {
            "exists": True,
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "duration_seconds": media.duration_seconds,
            "codec": media.codec_audio,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            "url": "api/intro-audio/file",
            "max_size_mb": 20,
        }
    )


@api_bp.route("/intro-audio/file")
def intro_audio_file():
    """Stream the currently configured Tagesschau intro MP3."""
    from flask import send_file

    path = _tagesschau_intro_audio_path()
    if not path.exists():
        return jsonify({"error": "No intro MP3 has been uploaded."}), 404
    return send_file(str(path.resolve()), mimetype="audio/mpeg", conditional=True)


@api_bp.route("/intro-audio", methods=["POST"])
def upload_intro_audio():
    """Validate and atomically replace the Tagesschau intro MP3."""
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Select an MP3 file to upload."}), 400
    if Path(uploaded.filename).suffix.casefold() != ".mp3":
        return jsonify({"error": "Only .mp3 files are accepted."}), 400
    if request.content_length and request.content_length > _INTRO_AUDIO_MAX_BYTES:
        return jsonify({"error": "The MP3 must not exceed 20 MB."}), 413

    from btcedu.services.ffmpeg_service import probe_media

    target = _tagesschau_intro_audio_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="intro-",
            suffix=".mp3",
            dir=target.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            uploaded.save(temporary)

        if temporary_path.stat().st_size == 0:
            return jsonify({"error": "The uploaded MP3 is empty."}), 400
        if temporary_path.stat().st_size > _INTRO_AUDIO_MAX_BYTES:
            return jsonify({"error": "The MP3 must not exceed 20 MB."}), 413

        media = probe_media(str(temporary_path))
        if not media.codec_audio:
            return jsonify({"error": "The uploaded file contains no audio stream."}), 400
        if "mp3" not in media.format_name.casefold():
            return jsonify({"error": "The uploaded file is not a valid MP3."}), 400

        temporary_path.replace(target)
        temporary_path = None
        logger.info(
            "Tagesschau intro MP3 uploaded (%d bytes, %.2fs)",
            target.stat().st_size,
            media.duration_seconds,
        )
        return jsonify(
            {
                "ok": True,
                "filename": target.name,
                "size_bytes": target.stat().st_size,
                "duration_seconds": media.duration_seconds,
                "codec": media.codec_audio,
            }
        )
    except RuntimeError as exc:
        return jsonify({"error": f"Invalid MP3: {exc}"}), 400
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@api_bp.route("/intro-audio", methods=["DELETE"])
def delete_intro_audio():
    """Remove the configured Tagesschau intro MP3."""
    path = _tagesschau_intro_audio_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    return jsonify({"ok": True, "deleted": existed})


@api_bp.route("/pipeline-health")
def pipeline_health():
    """Pipeline health monitoring: stage success rates, error trends, DLQ status."""
    from datetime import timedelta

    from sqlalchemy import case

    session = _get_session()
    try:
        now = datetime.now(UTC)
        t_24h = now - timedelta(hours=24)
        t_7d = now - timedelta(days=7)

        # --- Per-stage metrics ---
        stages_data = {}
        stage_rows = (
            session.query(
                PipelineRun.stage,
                func.count().label("total"),
                func.sum(case((PipelineRun.status == RunStatus.SUCCESS, 1), else_=0)).label(
                    "successes"
                ),
                func.sum(case((PipelineRun.status == RunStatus.FAILED, 1), else_=0)).label(
                    "failures"
                ),
                func.avg(
                    func.julianday(PipelineRun.completed_at)
                    - func.julianday(PipelineRun.started_at)
                ).label("avg_duration_days"),
            )
            .filter(PipelineRun.started_at >= t_7d)
            .group_by(PipelineRun.stage)
            .all()
        )

        # Also get 24h breakdown
        stage_rows_24h = (
            session.query(
                PipelineRun.stage,
                func.count().label("total"),
                func.sum(case((PipelineRun.status == RunStatus.SUCCESS, 1), else_=0)).label(
                    "successes"
                ),
                func.sum(case((PipelineRun.status == RunStatus.FAILED, 1), else_=0)).label(
                    "failures"
                ),
            )
            .filter(PipelineRun.started_at >= t_24h)
            .group_by(PipelineRun.stage)
            .all()
        )
        stats_24h = {
            row.stage: {
                "total": row.total,
                "successes": row.successes or 0,
                "failures": row.failures or 0,
            }
            for row in stage_rows_24h
        }

        for row in stage_rows:
            stage_name = row.stage.value if hasattr(row.stage, "value") else str(row.stage)
            total_7d = row.total or 0
            successes_7d = row.successes or 0
            avg_days = row.avg_duration_days or 0
            avg_seconds = avg_days * 86400

            s24 = stats_24h.get(row.stage, {})
            total_24h = s24.get("total", 0)
            successes_24h = s24.get("successes", 0)
            failures_24h = s24.get("failures", 0)

            stages_data[stage_name] = {
                "success_rate_24h": (
                    round(successes_24h / total_24h, 3) if total_24h > 0 else None
                ),
                "success_rate_7d": (round(successes_7d / total_7d, 3) if total_7d > 0 else None),
                "avg_duration_seconds": round(avg_seconds, 1),
                "total_runs_24h": total_24h,
                "total_runs_7d": total_7d,
                "failures_24h": failures_24h,
            }

        # --- Error trends (last 7 days, grouped by date) ---
        error_trends = []
        failed_runs = (
            session.query(PipelineRun)
            .filter(
                PipelineRun.status == RunStatus.FAILED,
                PipelineRun.started_at >= t_7d,
            )
            .all()
        )
        # Group by date
        from collections import Counter

        trend_counter: Counter = Counter()
        for run in failed_runs:
            date_str = run.started_at.strftime("%Y-%m-%d") if run.started_at else "unknown"
            # Extract category from error_message if present
            err = run.error_message or ""
            category = "unknown"
            if err.startswith("[") and "]" in err:
                category = err[1 : err.index("]")]
            trend_counter[(date_str, category)] += 1

        for (date_str, category), count in sorted(trend_counter.items()):
            error_trends.append({"date": date_str, "category": category, "count": count})

        # --- Dead-letter queue ---
        dlq_data = {"pending": 0, "resolved_24h": 0, "entries": []}
        try:
            from btcedu.models.dead_letter import DeadLetterEntry

            pending_count = (
                session.query(func.count(DeadLetterEntry.id))
                .filter(DeadLetterEntry.resolved_at.is_(None))
                .scalar()
            ) or 0
            resolved_24h = (
                session.query(func.count(DeadLetterEntry.id))
                .filter(DeadLetterEntry.resolved_at >= t_24h)
                .scalar()
            ) or 0

            pending_entries = (
                session.query(DeadLetterEntry)
                .filter(DeadLetterEntry.resolved_at.is_(None))
                .order_by(DeadLetterEntry.created_at.desc())
                .limit(50)
                .all()
            )
            dlq_data = {
                "pending": pending_count,
                "resolved_24h": resolved_24h,
                "entries": [
                    {
                        "id": e.id,
                        "episode_id": e.episode_id,
                        "stage": e.stage,
                        "error_category": e.error_category,
                        "error_message": e.error_message[:200],
                        "suggestion": e.suggestion,
                        "created_at": e.created_at.isoformat() if e.created_at else None,
                        "retry_count": e.retry_count,
                    }
                    for e in pending_entries
                ],
            }
        except Exception:
            pass  # DLQ table may not exist yet pre-migration

        # --- Episode summary ---
        total_episodes = session.query(func.count(Episode.id)).scalar() or 0
        failed_episodes = (
            session.query(func.count(Episode.id)).filter(Episode.error_message.isnot(None)).scalar()
        ) or 0
        stuck_threshold = 3
        stuck_episodes_q = (
            session.query(Episode)
            .filter(
                Episode.error_message.isnot(None),
                Episode.retry_count > stuck_threshold,
            )
            .all()
        )

        episodes_data = {
            "total": total_episodes,
            "failed": failed_episodes,
            "stuck_episodes": [
                {
                    "episode_id": ep.episode_id,
                    "title": ep.title,
                    "status": ep.status.value if hasattr(ep.status, "value") else str(ep.status),
                    "retry_count": ep.retry_count,
                    "error_message": (ep.error_message or "")[:200],
                }
                for ep in stuck_episodes_q
            ],
        }

        return jsonify(
            {
                "generated_at": now.isoformat(),
                "stages": stages_data,
                "error_trends": error_trends,
                "dead_letter_queue": dlq_data,
                "episodes": episodes_data,
            }
        )
    finally:
        session.close()


@api_bp.route("/debug/db-schema")
def debug_db_schema():
    """Return current database schema for debugging."""
    session = _get_session()
    try:
        from sqlalchemy import text

        # Get all tables
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        )
        tables = [row[0] for row in result.fetchall()]

        schema = {}
        for table in tables:
            # Get columns for each table
            result = session.execute(text(f"PRAGMA table_info({table})"))
            columns = []
            for row in result.fetchall():
                columns.append(
                    {
                        "name": row[1],
                        "type": row[2],
                        "nullable": row[3] == 0,
                        "default": row[4],
                        "pk": row[5] == 1,
                    }
                )
            schema[table] = columns

        # Get indexes
        indexes = {}
        for table in tables:
            result = session.execute(text(f"PRAGMA index_list({table})"))
            table_indexes = [row[1] for row in result.fetchall()]
            if table_indexes:
                indexes[table] = table_indexes

        return jsonify({"tables": list(schema.keys()), "schema": schema, "indexes": indexes})
    except Exception as e:
        logger.exception("Failed to get database schema")
        return jsonify({"error": str(e)}), 500
    finally:
        session.close()


def _get_session():
    return current_app.config["session_factory"]()


def _get_settings():
    return current_app.config["settings"]


def _get_job_manager():
    return current_app.config["job_manager"]


# Allowlist: alphanumeric, hyphens, underscores, and dots (no leading dot).
# YouTube video IDs can start with '-' or '_', so allow those as first char.
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_-][a-zA-Z0-9._-]*$")


def _validate_episode_path(episode_id: str, base_dir: Path, *path_parts: str) -> Path | None:
    """Validate episode_id exists in DB and construct safe path within base_dir.

    Returns resolved path if valid, None if episode doesn't exist or path escapes base_dir.

    Args:
        episode_id: Episode identifier from URL parameter
        base_dir: Base directory (e.g., outputs_dir)
        *path_parts: Additional path components (e.g., "render", "draft.mp4")

    Returns:
        Resolved path if valid, None otherwise
    """
    # Reject empty or structurally unsafe episode_id / path parts.
    # secure_filename is a CodeQL-recognized sanitizer that strips path
    # separators, "..", leading dots, and other dangerous characters.
    # The allowlist regex provides additional defense-in-depth.
    episode_id = secure_filename(episode_id)
    if not episode_id or not _SAFE_PATH_COMPONENT_RE.match(episode_id):
        return None

    sanitized_parts = []
    for part in path_parts:
        part = secure_filename(part)
        if not part or not _SAFE_PATH_COMPONENT_RE.match(part):
            return None
        sanitized_parts.append(part)
    path_parts = tuple(sanitized_parts)

    session = _get_session()
    try:
        # Verify episode exists in database
        episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not episode:
            return None

        # Construct and resolve the full path using sanitized components
        full_path = base_dir / episode_id / Path(*path_parts)
        try:
            resolved_path = full_path.resolve()
        except (OSError, RuntimeError):
            # Handle path resolution errors
            return None

        # Verify resolved path is still within base_dir
        resolved_base = base_dir.resolve()
        try:
            resolved_path.relative_to(resolved_base)
        except ValueError:
            # Path escapes base directory
            return None

        return resolved_path
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_presence(episode_id: str, settings) -> dict[str, bool]:
    """Check which output files exist for an episode."""
    raw = Path(settings.raw_data_dir) / episode_id
    trans = Path(settings.transcripts_dir) / episode_id
    out = Path(settings.outputs_dir) / episode_id

    # v2 news-pipeline artifacts (Tagesschau etc.)
    script_adapted = (out / "script.adapted.tr.md").exists()
    chapters_json = (out / "chapters.json").exists()
    images_manifest = (out / "images" / "manifest.json").exists()
    tts_dir = out / "tts"
    tts_present = tts_dir.exists() and any(tts_dir.glob("ch*.mp3"))
    render_draft = (out / "render" / "draft.mp4").exists()

    return {
        "audio": any(raw.glob("audio.*")) if raw.exists() else False,
        "transcript_raw": (trans / "transcript.de.txt").exists(),
        "transcript_clean": (trans / "transcript.clean.de.txt").exists(),
        "stories": (out / "stories.json").exists(),
        "stories_translated": (out / "stories_translated.json").exists(),
        # v2 news-pipeline artifacts
        "script_adapted": script_adapted,
        "chapters": chapters_json,
        # Turkish spoken transcript is reconstructed from chapters.json narration
        "transcript_tr": chapters_json,
        "images": images_manifest,
        "tts": tts_present,
        "video": render_draft,
    }


# Maps each artifact-producing pipeline stage to a workflow-file key + label.
# Review gates and helper stages (frameextract, anchorgen) produce no primary
# artifact and are intentionally omitted.
_STAGE_WORKFLOW_KEY = {
    "download": "audio",
    "transcribe": "transcript_raw",
    "correct": "transcript_clean",
    "segment": "stories",
    "translate": "translation",
    "adapt": "script_adapted",
    "chapterize": "chapters",
    "imagegen": "images",
    "tts": "tts",
    "render": "video",
}

_WORKFLOW_LABELS = {
    "audio": "Audio",
    "transcript_raw": "Transcript DE",
    "transcript_clean": "Transcript Clean",
    "stories": "Stories DE",
    "translation": "Translation TR",
    "script_adapted": "Script (adapted)",
    "chapters": "Chapters",
    "images": "Images",
    "tts": "TTS Audio",
    "video": "Rendered Video",
}


def _workflow_files(ep, settings) -> list[dict] | None:
    """Return the ordered workflow-file dots relevant to *this* episode.

    Unlike ``_file_presence`` (which returns every possible v1+v2 artifact key),
    this derives the artifacts from the episode's actual pipeline stages
    (profile/version aware) so a fully completed episode shows every dot green
    and never carries irrelevant legacy dots.

    Returns a list of ``{"key", "label", "present"}`` in stage order, or
    ``None`` when the stage list cannot be resolved (frontend then falls back
    to the legacy full dot list).
    """
    from btcedu.core.pipeline import _get_stages

    try:
        stages = _get_stages(settings, ep)
    except Exception:
        return None

    presence = _file_presence(ep.episode_id, settings)
    trans = Path(settings.transcripts_dir) / ep.episode_id

    result: list[dict] = []
    seen: set[str] = set()
    for name, _required in stages:
        key = _STAGE_WORKFLOW_KEY.get(name)
        if not key or key in seen:
            continue
        seen.add(key)
        if key == "translation":
            # News uses per-story stories_translated.json; other profiles write
            # a plain transcript.tr.txt. Either counts as translated.
            present = bool(
                presence.get("stories_translated") or (trans / "transcript.tr.txt").exists()
            )
        else:
            present = bool(presence.get(key, False))
        result.append({"key": key, "label": _WORKFLOW_LABELS.get(key, key), "present": present})

    return result or None


def _build_tr_transcript(episode_id: str, settings) -> str | None:
    """Reconstruct the Turkish spoken transcript from chapters.json.

    Concatenates every chapter's narration text (the exact words spoken in the
    rendered video) into a readable document, prefixed with the episode title
    and per-chapter headings. Returns None when chapters.json is missing or
    unreadable.
    """
    path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    chapters = sorted(
        doc.get("chapters", []),
        key=lambda c: c.get("order", 0),
    )
    lines: list[str] = []
    title = doc.get("title")
    if title:
        lines.append(f"# {title}")
        lines.append("")

    for ch in chapters:
        heading = ch.get("title") or ch.get("chapter_id") or ""
        order = ch.get("order")
        prefix = f"{order}. " if order else ""
        lines.append(f"## {prefix}{heading}".rstrip())
        text = (ch.get("narration") or {}).get("text", "").strip()
        lines.append(text)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _get_story_count(episode_id: str, settings) -> int | None:
    """Return total_stories from stories.json if it exists, else None."""
    stories_path = Path(settings.outputs_dir) / episode_id / "stories.json"
    if not stories_path.exists():
        return None
    try:
        data = json.loads(stories_path.read_text(encoding="utf-8"))
        return data.get("total_stories")
    except Exception:
        return None


# Review gate labels: stage → (gate_name, human_readable_label)
_REVIEW_GATE_LABELS = {
    "transcript_qa": ("review_gate_transcript_qa", "Transcript QA Review"),
    "correct": ("review_gate_1", "Transcript Correction Review"),
    "translate": ("review_gate_translate", "Translation Review"),
    "adapt": ("review_gate_2", "Adaptation Review"),
    "stock_images": ("review_gate_stock", "Stock Image Review"),
    "render": ("review_gate_3", "Video Review"),
}

# Episode statuses that correspond to review gate pauses
_REVIEW_GATE_STATUS_MAP = {
    "corrected": "correct",
    "adapted": "adapt",
    "chapterized": "stock_images",
    "rendered": "render",
}


def _get_review_context(
    session, episode_id: str, status: str, pending_cache: dict | None = None
) -> dict | None:
    """Build review context dict for an episode.

    Args:
        session: DB session
        episode_id: Episode ID
        status: Episode status value string
        pending_cache: Optional pre-fetched dict of {episode_id: ReviewTask}
            for batch queries. If None, queries the DB directly.

    Returns:
        Review context dict or None if no review context applies.
    """
    from btcedu.models.review import ReviewStatus, ReviewTask

    # Check for pending/in_review task
    pending_task = None
    if pending_cache is not None:
        pending_task = pending_cache.get(episode_id)
    else:
        pending_task = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.status.in_(
                    [
                        ReviewStatus.PENDING.value,
                        ReviewStatus.IN_REVIEW.value,
                    ]
                ),
            )
            .order_by(ReviewTask.created_at.desc())
            .first()
        )

    if pending_task:
        stage = pending_task.stage
        gate_name, label = _REVIEW_GATE_LABELS.get(
            stage, (f"review_gate_{stage}", f"{stage.replace('_', ' ').title()} Review")
        )
        return {
            "state": "paused_for_review",
            "review_task_id": pending_task.id,
            "review_stage": stage,
            "review_stage_label": label,
            "review_status": pending_task.status,
            "review_gate": gate_name,
            "created_at": (
                pending_task.created_at.isoformat() if pending_task.created_at else None
            ),
            "next_action_text": (
                f"Pipeline paused \u2014 {gate_name.replace('_', ' ').title()} requires approval"
            ),
            "action_url": f"/api/reviews/{pending_task.id}",
        }

    # No pending task — check if status implies a review gate
    review_stage = _REVIEW_GATE_STATUS_MAP.get(status)
    if review_stage:
        # Check for approved review at this stage
        approved_task = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == review_stage,
                ReviewTask.status == ReviewStatus.APPROVED.value,
            )
            .order_by(ReviewTask.created_at.desc())
            .first()
        )
        if approved_task:
            _, label = _REVIEW_GATE_LABELS.get(
                review_stage,
                (None, f"{review_stage.replace('_', ' ').title()} Review"),
            )
            return {
                "state": "review_approved",
                "review_task_id": approved_task.id,
                "review_stage": review_stage,
                "review_stage_label": label,
                "review_status": approved_task.status,
                "review_gate": _REVIEW_GATE_LABELS.get(review_stage, (None,))[0],
                "created_at": (
                    approved_task.created_at.isoformat() if approved_task.created_at else None
                ),
                "next_action_text": f"{label} approved \u2014 run pipeline to continue",
                "action_url": f"/api/reviews/{approved_task.id}",
            }

    return None


def _compute_pipeline_state(status: str, review_context: dict | None) -> str:
    """Derive a high-level pipeline state string for UI consumption."""
    if review_context and review_context.get("state") == "paused_for_review":
        return "paused_for_review"
    if status in ("failed", "cost_limit"):
        return "failed"
    if status == "published":
        return "completed"
    if status == "approved":
        return "ready"
    return "ready"


# ---------------------------------------------------------------------------
# Phase 2: Pipeline stage progress
# ---------------------------------------------------------------------------

_STAGE_LABELS = {
    "download": "Download",
    "transcribe": "Transcribe",
    "transcript_analyze": "Transcript Analysis",
    "transcript_verify": "Transcript Verification",
    "correct": "Correct",
    "transcript_qa": "Transcript QA",
    "review_gate_transcript_qa": "Transcript QA Review",
    "review_gate_1": "Review 1",
    "segment": "Segment",
    "translate": "Translate",
    "adapt": "Adapt",
    "review_gate_translate": "Review Translate",
    "review_gate_2": "Review 2",
    "chapterize": "Chapterize",
    "frameextract": "Frames",
    "imagegen": "Images",
    "review_gate_stock": "Review Stock",
    "tts": "TTS",
    "anchorgen": "D-ID Anchor",
    "render": "Render",
    "review_gate_3": "Review 3",
    "publish": "Publish",
}

_STAGE_TO_PIPELINE_STAGE = {
    "download": PipelineStage.DOWNLOAD,
    "transcribe": PipelineStage.TRANSCRIBE,
    "transcript_analyze": PipelineStage.TRANSCRIPT_ANALYZE,
    "transcript_verify": PipelineStage.TRANSCRIPT_VERIFY,
    "correct": PipelineStage.CORRECT,
    "transcript_qa": PipelineStage.TRANSCRIPT_QA,
    "translate": PipelineStage.TRANSLATE,
    "adapt": PipelineStage.ADAPT,
    "chapterize": PipelineStage.CHAPTERIZE,
    "segment": PipelineStage.SEGMENT,
    "frameextract": PipelineStage.FRAMEEXTRACT,
    "imagegen": PipelineStage.IMAGEGEN,
    "tts": PipelineStage.TTS,
    "anchorgen": PipelineStage.ANCHORGEN,
    "render": PipelineStage.RENDER,
    "publish": PipelineStage.PUBLISH,
}


def _build_stage_progress(
    session,
    episode: Episode,
    settings,
    review_context: dict | None,
    duration_cache: dict | None = None,
) -> dict:
    """Build the stage_progress dict for an episode.

    Args:
        session: DB session
        episode: Episode ORM object
        settings: Application settings
        review_context: Pre-computed review context dict (or None)
        duration_cache: Optional pre-fetched {episode.id: {PipelineStage: (duration_s, cost_usd)}}

    Returns:
        Dict with pipeline_version, stages list, current_stage, completed_count, total_count.
    """
    from btcedu.core.pipeline import resolve_pipeline_plan

    plan = resolve_pipeline_plan(session, episode, force=False, settings=settings)

    # Map StagePlan decisions to UI states
    stages = []
    for sp in plan:
        is_gate = sp.stage.startswith("review_gate")
        if sp.decision == "skip" and "already completed" in sp.reason:
            state = "done"
        elif sp.decision == "run":
            state = "active"
        else:
            # "skip" with other reason, or "pending"
            state = "pending"

        stages.append(
            {
                "name": sp.stage,
                "label": _STAGE_LABELS.get(sp.stage, sp.stage),
                "state": state,
                "is_gate": is_gate,
                "duration_seconds": None,
                "cost_usd": None,
                "git_commit": None,
            }
        )

    # Override with review context
    if review_context:
        rc_state = review_context.get("state")
        rc_gate = review_context.get("review_gate")
        gate_index = next(
            (index for index, stage in enumerate(stages) if stage["name"] == rc_gate),
            None,
        )
        if gate_index is not None:
            for stage in stages[:gate_index]:
                stage["state"] = "done"
        for s in stages:
            if s["is_gate"] and s["name"] == rc_gate:
                if rc_state == "paused_for_review":
                    s["state"] = "paused"
                elif rc_state == "review_approved":
                    s["state"] = "done"

    # Additionally: review gates that were previously approved should show "done"
    # even without review_context (e.g. episode advanced past them)
    # This is already handled by "already completed" → "done" from resolve_pipeline_plan

    # Override with failure
    episode_status = episode.status.value
    if episode_status in ("failed", "cost_limit"):
        # Find the first "active" stage and mark it failed.
        # If none is active (FAILED has _STATUS_ORDER=-1, so all become pending),
        # find the first non-done stage instead.
        target = None
        for s in stages:
            if s["state"] == "active":
                target = s
                break
        if target is None:
            for s in stages:
                if s["state"] not in ("done", "skipped"):
                    target = s
                    break
        if target is not None:
            target["state"] = "failed"
            # All subsequent stages → pending
            found = False
            for s2 in stages:
                if found:
                    s2["state"] = "pending"
                if s2 is target:
                    found = True

    # Attach durations from duration_cache or query directly
    ep_duration_map: dict[PipelineStage, tuple[float, float, str | None]] = {}
    if duration_cache is not None:
        ep_duration_map = duration_cache.get(episode.id, {})
    else:
        # Single-episode query
        runs = (
            session.query(PipelineRun)
            .filter(
                PipelineRun.episode_id == episode.id,
                PipelineRun.status == RunStatus.SUCCESS,
            )
            .order_by(PipelineRun.completed_at.desc())
            .all()
        )
        seen: set[PipelineStage] = set()
        for run in runs:
            if run.stage not in seen:
                seen.add(run.stage)
                if run.completed_at and run.started_at:
                    dur = (run.completed_at - run.started_at).total_seconds()
                else:
                    dur = 0.0
                ep_duration_map[run.stage] = (
                    dur,
                    run.estimated_cost_usd,
                    getattr(run, "git_commit", None),
                )

    latest_commit = None
    for s in stages:
        if s["is_gate"]:
            continue
        ps = _STAGE_TO_PIPELINE_STAGE.get(s["name"])
        if ps and ps in ep_duration_map:
            dur, cost, commit = ep_duration_map[ps]
            s["duration_seconds"] = dur
            s["cost_usd"] = cost
            s["git_commit"] = commit
            if commit:
                latest_commit = commit

    # Compute summary fields
    current_stage = None
    for s in stages:
        if s["state"] in ("active", "paused", "failed"):
            current_stage = s["name"]
            break

    completed_count = sum(1 for s in stages if s["state"] in ("done", "skipped"))
    total_count = len(stages)

    return {
        "pipeline_version": getattr(episode, "pipeline_version", 1),
        "stages": stages,
        "current_stage": current_stage,
        "completed_count": completed_count,
        "total_count": total_count,
        "git_commit": latest_commit,
    }


def _episode_to_dict(
    ep: Episode,
    settings,
    session=None,
    pending_cache: dict | None = None,
    duration_cache: dict | None = None,
) -> dict:
    """Serialize an Episode ORM object to a JSON-safe dict."""
    status_val = ep.status.value
    review_context = None
    if session is not None:
        review_context = _get_review_context(
            session, ep.episode_id, status_val, pending_cache=pending_cache
        )

    stage_progress = None
    if session is not None:
        try:
            stage_progress = _build_stage_progress(
                session, ep, settings, review_context, duration_cache=duration_cache
            )
        except Exception:
            logger.exception("Failed to build stage_progress for %s", ep.episode_id)

    return {
        "episode_id": ep.episode_id,
        "title": ep.title,
        "status": status_val,
        "source": ep.source,
        "url": ep.url,
        "published_at": ep.published_at.isoformat() if ep.published_at else None,
        "detected_at": ep.detected_at.isoformat() if ep.detected_at else None,
        "completed_at": ep.completed_at.isoformat() if ep.completed_at else None,
        "error_message": ep.error_message,
        "retry_count": ep.retry_count,
        "content_profile": getattr(ep, "content_profile", "bitcoin_podcast"),
        "pipeline_version": getattr(ep, "pipeline_version", 1),
        "youtube_video_id": getattr(ep, "youtube_video_id", None),
        "published_at_youtube": (
            ep.published_at_youtube.isoformat()
            if getattr(ep, "published_at_youtube", None)
            else None
        ),
        "files": _file_presence(ep.episode_id, settings),
        "workflow_files": _workflow_files(ep, settings),
        "story_count": _get_story_count(ep.episode_id, settings),
        "review_context": review_context,
        "pipeline_state": _compute_pipeline_state(status_val, review_context),
        "stage_progress": stage_progress,
    }


def _submit_job(action, episode_id, **kwargs):
    """Submit a background job, return (response, status_code)."""
    mgr = _get_job_manager()
    active = mgr.active_for_episode(episode_id)
    if active:
        return jsonify(
            {
                "error": "Job already active",
                "job_id": active.job_id,
            }
        ), 409

    job = mgr.submit(
        action=action,
        episode_id=episode_id,
        app=current_app._get_current_object(),
        **kwargs,
    )
    return jsonify({"job_id": job.job_id, "state": job.state}), 202


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@api_bp.route("/profiles")
def list_profiles():
    """Return all content profiles."""
    try:
        from btcedu.profiles import get_registry, reset_registry

        settings = _get_settings()
        reset_registry()
        registry = get_registry(settings)
        profiles = registry.list_profiles()
        return jsonify(
            [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "source_language": p.source_language,
                    "target_language": p.target_language,
                    "domain": p.domain,
                    "pipeline_version": p.pipeline_version,
                    "stages_enabled": p.stages_enabled,
                }
                for p in profiles
            ]
        )
    except Exception as e:
        logger.exception("Failed to list profiles")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Episode list + detail
# ---------------------------------------------------------------------------


@api_bp.route("/episodes")
def list_episodes():
    session = _get_session()
    settings = _get_settings()
    try:
        from btcedu.core.retention import episode_is_expired
        from btcedu.models.review import ReviewStatus, ReviewTask

        # Support optional channel and profile filters
        channel_id = request.args.get("channel_id")
        profile_filter = request.args.get("profile")

        query = session.query(Episode)

        if channel_id:
            query = query.filter(Episode.channel_id == channel_id)
        if profile_filter:
            query = query.filter(Episode.content_profile == profile_filter)

        episodes = [
            episode
            for episode in query.order_by(Episode.published_at.desc().nullslast()).all()
            if not episode_is_expired(episode, settings)
        ]

        # Batch query: fetch all pending/in_review tasks in one query (avoids N+1)
        pending_tasks = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status.in_(
                    [
                        ReviewStatus.PENDING.value,
                        ReviewStatus.IN_REVIEW.value,
                    ]
                )
            )
            .order_by(ReviewTask.created_at.desc())
            .all()
        )
        # First match per episode_id wins (most recent pending task)
        pending_cache = {}
        for task in pending_tasks:
            if task.episode_id not in pending_cache:
                pending_cache[task.episode_id] = task

        # Batch duration query: most recent successful PipelineRun per (episode, stage)
        # PipelineRun.episode_id is an int FK to episodes.id
        # Build {episode.id: {PipelineStage: (duration_seconds, cost_usd)}}
        duration_cache: dict[int, dict[PipelineStage, tuple[float, float, str | None]]] = {}
        all_runs = (
            session.query(PipelineRun)
            .filter(PipelineRun.status == RunStatus.SUCCESS)
            .order_by(PipelineRun.episode_id, PipelineRun.stage, PipelineRun.completed_at.desc())
            .all()
        )
        seen_run_keys: set[tuple[int, PipelineStage]] = set()
        for run in all_runs:
            key = (run.episode_id, run.stage)
            if key not in seen_run_keys:
                seen_run_keys.add(key)
                if run.completed_at and run.started_at:
                    dur = (run.completed_at - run.started_at).total_seconds()
                else:
                    dur = 0.0
                if run.episode_id not in duration_cache:
                    duration_cache[run.episode_id] = {}
                duration_cache[run.episode_id][run.stage] = (
                    dur,
                    run.estimated_cost_usd,
                    getattr(run, "git_commit", None),
                )

        return jsonify(
            [
                _episode_to_dict(
                    ep,
                    settings,
                    session=session,
                    pending_cache=pending_cache,
                    duration_cache=duration_cache,
                )
                for ep in episodes
            ]
        )
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>")
def get_episode(episode_id: str):
    session = _get_session()
    settings = _get_settings()
    try:
        ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not ep:
            return jsonify({"error": f"Episode not found: {episode_id}"}), 404

        data = _episode_to_dict(ep, settings, session=session)

        # Add cost info from pipeline runs
        runs = session.query(PipelineRun).filter(PipelineRun.episode_id == ep.id).all()
        data["cost"] = {
            "total_usd": sum(r.estimated_cost_usd for r in runs),
            "input_tokens": sum(r.input_tokens for r in runs),
            "output_tokens": sum(r.output_tokens for r in runs),
        }

        return jsonify(data)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Pipeline actions (all async via JobManager)
# ---------------------------------------------------------------------------


@api_bp.route("/detect", methods=["POST"])
def detect():
    """Detect new episodes from all active channels."""
    from btcedu.core.detector import detect_all_active_channels

    session = _get_session()
    settings = _get_settings()
    try:
        result = detect_all_active_channels(session, settings)
        return jsonify(
            {
                "success": True,
                "found": result.found,
                "new": result.new,
                "total": result.total,
            }
        )
    except Exception as e:
        logger.exception("Detect failed")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/download", methods=["POST"])
def download_episode(episode_id: str):
    body = request.get_json(silent=True) or {}
    return _submit_job("download", episode_id, force=body.get("force", False))


@api_bp.route("/episodes/<episode_id>/transcribe", methods=["POST"])
def transcribe_episode(episode_id: str):
    body = request.get_json(silent=True) or {}
    return _submit_job("transcribe", episode_id, force=body.get("force", False))


@api_bp.route("/episodes/<episode_id>/run", methods=["POST"])
def run_episode(episode_id: str):
    body = request.get_json(silent=True) or {}
    return _submit_job("run", episode_id, force=body.get("force", False))


@api_bp.route("/episodes/<episode_id>/retry", methods=["POST"])
def retry_episode(episode_id: str):
    return _submit_job("retry", episode_id)


# ---------------------------------------------------------------------------
# Job status + logs
# ---------------------------------------------------------------------------


@api_bp.route("/jobs/<job_id>")
def get_job(job_id: str):
    mgr = _get_job_manager()
    job = mgr.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = {
        "job_id": job.job_id,
        "episode_id": job.episode_id,
        "action": job.action,
        "state": job.state,
        "stage": job.stage,
        "message": job.message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "result": job.result,
    }

    # Include current episode status from DB for real-time progress
    session = _get_session()
    try:
        ep = (
            session.query(Episode)
            .filter(
                Episode.episode_id == job.episode_id,
            )
            .first()
        )
        data["episode_status"] = ep.status.value if ep else None
    finally:
        session.close()

    return jsonify(data)


@api_bp.route("/episodes/<episode_id>/action-log")
def episode_action_log(episode_id: str):
    # Sanitize episode_id to prevent path traversal
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()
    tail = request.args.get("tail", 200, type=int)
    log_path = Path(settings.logs_dir) / "episodes" / f"{episode_id}.log"

    # Verify resolved path stays within logs_dir
    try:
        resolved = log_path.resolve()
        if not resolved.is_relative_to(Path(settings.logs_dir).resolve()):
            return jsonify({"error": "Invalid episode ID"}), 400
    except (OSError, RuntimeError):
        return jsonify({"error": "Invalid episode ID"}), 400

    if not log_path.exists():
        return jsonify({"lines": []})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    return jsonify({"lines": lines[-tail:]})


# ---------------------------------------------------------------------------
# File viewer
# ---------------------------------------------------------------------------

_FILE_MAP = {
    "transcript_raw": ("transcripts_dir", "{eid}/transcript.de.txt"),
    "transcript_clean": ("transcripts_dir", "{eid}/transcript.clean.de.txt"),
    "script_adapted": ("outputs_dir", "{eid}/script.adapted.tr.md"),
    "chapters": ("outputs_dir", "{eid}/chapters.json"),
    "stories": ("outputs_dir", "{eid}/stories.json"),
    "stories_translated": ("outputs_dir", "{eid}/stories_translated.json"),
}


@api_bp.route("/episodes/<episode_id>/files/<file_type>")
def get_file(episode_id: str, file_type: str):
    # Sanitize episode_id to prevent path traversal
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()

    # Handle report separately (find latest)
    if file_type == "report":
        report_dir = Path(settings.reports_dir) / episode_id
        # Verify resolved path stays within reports_dir
        try:
            if not report_dir.resolve().is_relative_to(Path(settings.reports_dir).resolve()):
                return jsonify({"error": "Invalid episode ID"}), 400
        except (OSError, RuntimeError):
            return jsonify({"error": "Invalid episode ID"}), 400
        if not report_dir.exists():
            return jsonify({"error": "No reports found"}), 404
        reports = sorted(report_dir.glob("report_*.json"), reverse=True)
        if not reports:
            return jsonify({"error": "No reports found"}), 404
        path = reports[0]
    elif file_type == "transcript_tr":
        # Synthesised from chapters.json narration (Turkish spoken transcript)
        transcript = _build_tr_transcript(episode_id, settings)
        if transcript is None:
            return jsonify({"error": "TR transcript not available (no chapters yet)"}), 404
        return jsonify(
            {
                "content": transcript,
                "path": str(Path(settings.outputs_dir) / episode_id / "chapters.json"),
            }
        )
    elif file_type in _FILE_MAP:
        dir_attr, pattern = _FILE_MAP[file_type]
        base = getattr(settings, dir_attr)
        path = Path(base) / pattern.format(eid=episode_id)
        # Verify resolved path stays within base directory
        try:
            if not path.resolve().is_relative_to(Path(base).resolve()):
                return jsonify({"error": "Invalid episode ID"}), 400
        except (OSError, RuntimeError):
            return jsonify({"error": "Invalid episode ID"}), 400
    else:
        return jsonify({"error": f"Unknown file type: {file_type}"}), 400

    if not path.exists():
        return jsonify({"error": f"File not found: {path}"}), 404

    content = path.read_text(encoding="utf-8")

    # Pretty-print JSON files
    if path.suffix == ".json":
        try:
            content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

    return jsonify({"content": content, "path": str(path)})


# ---------------------------------------------------------------------------
# Cost summary
# ---------------------------------------------------------------------------


@api_bp.route("/cost")
def cost_summary():
    session = _get_session()
    try:
        rows = (
            session.query(
                PipelineRun.stage,
                func.count().label("runs"),
                func.sum(PipelineRun.input_tokens).label("input_tokens"),
                func.sum(PipelineRun.output_tokens).label("output_tokens"),
                func.sum(PipelineRun.estimated_cost_usd).label("total_cost"),
            )
            .group_by(PipelineRun.stage)
            .all()
        )

        stages = []
        grand_total = 0.0
        for row in rows:
            cost_val = row.total_cost or 0.0
            grand_total += cost_val
            stages.append(
                {
                    "stage": row.stage.value,
                    "runs": row.runs,
                    "input_tokens": row.input_tokens or 0,
                    "output_tokens": row.output_tokens or 0,
                    "cost_usd": round(cost_val, 6),
                }
            )

        ep_count = session.query(func.count(func.distinct(PipelineRun.episode_id))).scalar() or 0

        return jsonify(
            {
                "stages": stages,
                "total_usd": round(grand_total, 6),
                "episodes_processed": ep_count,
                "avg_per_episode": round(grand_total / ep_count, 4) if ep_count else 0,
            }
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@api_bp.route("/analytics/throughput")
def analytics_throughput():
    """Return daily episode completion counts for throughput chart."""
    session = _get_session()
    try:
        date_col = func.date(PipelineRun.completed_at)
        rows = (
            session.query(
                date_col.label("day"),
                func.count(func.distinct(PipelineRun.episode_id)).label("episodes"),
                func.sum(PipelineRun.estimated_cost_usd).label("cost_usd"),
            )
            .filter(PipelineRun.status == "success")
            .filter(PipelineRun.completed_at.isnot(None))
            .group_by(date_col)
            .order_by(date_col)
            .all()
        )
        return jsonify(
            {
                "days": [
                    {
                        "date": str(row.day) if row.day else None,
                        "episodes": row.episodes,
                        "cost_usd": round(row.cost_usd or 0, 4),
                    }
                    for row in rows
                    if row.day is not None
                ]
            }
        )
    finally:
        session.close()


@api_bp.route("/analytics/error-rate")
def analytics_error_rate():
    """Return success/failure counts per stage for error-rate chart."""
    session = _get_session()
    try:
        rows = (
            session.query(
                PipelineRun.stage,
                PipelineRun.status,
                func.count().label("count"),
            )
            .group_by(PipelineRun.stage, PipelineRun.status)
            .all()
        )
        # Pivot into per-stage success/failure
        stage_map = {}
        for row in rows:
            stage_name = row.stage.value if hasattr(row.stage, "value") else str(row.stage)
            if stage_name not in stage_map:
                stage_map[stage_name] = {
                    "stage": stage_name,
                    "success": 0,
                    "failed": 0,
                    "running": 0,
                }
            status_val = row.status.value if hasattr(row.status, "value") else str(row.status)
            if status_val == "success":
                stage_map[stage_name]["success"] = row.count
            elif status_val == "failed":
                stage_map[stage_name]["failed"] = row.count
            else:
                stage_map[stage_name]["running"] = row.count

        stages = list(stage_map.values())
        for s in stages:
            total = s["success"] + s["failed"]
            s["error_rate"] = round(s["failed"] / total, 4) if total > 0 else 0

        return jsonify({"stages": stages})
    finally:
        session.close()


@api_bp.route("/analytics/provider-cost")
def analytics_provider_cost():
    """Return cost breakdown by provider (inferred from pipeline stage)."""
    session = _get_session()
    try:
        # Map stages to providers
        STAGE_PROVIDER_MAP = {
            "transcribe": "OpenAI (Whisper)",
            "correct": "Anthropic",
            "translate": "Anthropic",
            "adapt": "Anthropic",
            "chapterize": "Anthropic",
            "imagegen": "OpenAI (DALL-E)",
            "tts": "ElevenLabs",
            "anchorgen": "D-ID",
            "render": "Local (ffmpeg)",
            "publish": "YouTube",
        }

        rows = (
            session.query(
                PipelineRun.stage,
                func.count().label("runs"),
                func.sum(PipelineRun.estimated_cost_usd).label("cost_usd"),
            )
            .filter(PipelineRun.status == "success")
            .group_by(PipelineRun.stage)
            .all()
        )

        provider_map = {}
        for row in rows:
            stage_name = row.stage.value if hasattr(row.stage, "value") else str(row.stage)
            provider = STAGE_PROVIDER_MAP.get(stage_name, "Other")
            if provider not in provider_map:
                provider_map[provider] = {"provider": provider, "runs": 0, "cost_usd": 0.0}
            provider_map[provider]["runs"] += row.runs
            provider_map[provider]["cost_usd"] += row.cost_usd or 0

        providers = sorted(provider_map.values(), key=lambda p: -p["cost_usd"])
        for p in providers:
            p["cost_usd"] = round(p["cost_usd"], 4)

        return jsonify({"providers": providers})
    finally:
        session.close()


# ---------------------------------------------------------------------------
# What's new
# ---------------------------------------------------------------------------


@api_bp.route("/whats-new")
def whats_new():
    session = _get_session()
    settings = _get_settings()
    try:
        # New episodes
        new_eps = (
            session.query(Episode)
            .filter(Episode.status == EpisodeStatus.NEW)
            .order_by(Episode.detected_at.desc())
            .all()
        )

        # Failed episodes
        failed_eps = (
            session.query(Episode)
            .filter(Episode.error_message.isnot(None))
            .order_by(Episode.detected_at.desc())
            .all()
        )

        # Episodes missing a step (have audio but no transcript, etc.)
        incomplete = []
        all_eps = (
            session.query(Episode)
            .filter(
                Episode.status.notin_(
                    [
                        EpisodeStatus.NEW,
                        EpisodeStatus.PUBLISHED,
                    ]
                )
            )
            .all()
        )
        for ep in all_eps:
            files = _file_presence(ep.episode_id, settings)
            if ep.status == EpisodeStatus.DOWNLOADED and not files.get("transcript_raw"):
                incomplete.append(
                    {
                        "episode_id": ep.episode_id,
                        "title": ep.title,
                        "status": ep.status.value,
                        "missing": "transcript",
                    }
                )
            elif ep.status == EpisodeStatus.TRANSCRIBED and not files.get("transcript_clean"):
                incomplete.append(
                    {
                        "episode_id": ep.episode_id,
                        "title": ep.title,
                        "status": ep.status.value,
                        "missing": "corrected transcript",
                    }
                )

        return jsonify(
            {
                "new_episodes": [
                    {"episode_id": ep.episode_id, "title": ep.title} for ep in new_eps
                ],
                "failed": [
                    {
                        "episode_id": ep.episode_id,
                        "title": ep.title,
                        "error": ep.error_message[:200] if ep.error_message else None,
                        "retry_count": ep.retry_count,
                    }
                    for ep in failed_eps
                ],
                "incomplete": incomplete,
            }
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Batch Processing (Process All)
# ---------------------------------------------------------------------------


@api_bp.route("/batch/start", methods=["POST"])
def batch_start():
    """Start a batch job to process all pending episodes."""
    job_manager = _get_job_manager()

    # Check if there's already an active batch job
    active = job_manager.active_batch()
    if active:
        return jsonify(
            {
                "error": "A batch job is already running",
                "batch_id": active.batch_id,
            }
        ), 409

    data = request.get_json() or {}
    force = data.get("force", False)
    channel_id = data.get("channel_id")
    profile_filter = data.get("profile")

    batch_job = job_manager.submit_batch(
        current_app._get_current_object(),
        force=force,
        channel_id=channel_id,
        profile=profile_filter,
    )

    return jsonify(
        {
            "batch_id": batch_job.batch_id,
            "state": batch_job.state,
            "message": "Batch job started",
        }
    ), 202


@api_bp.route("/batch/<batch_id>", methods=["GET"])
def batch_status(batch_id):
    """Get batch job status and progress."""
    job_manager = _get_job_manager()
    batch_job = job_manager.get_batch(batch_id)

    if not batch_job:
        return jsonify({"error": "Batch job not found"}), 404

    return jsonify(
        {
            "batch_id": batch_job.batch_id,
            "state": batch_job.state,
            "current_episode_id": batch_job.current_episode_id,
            "current_episode_title": batch_job.current_episode_title,
            "current_stage": batch_job.current_stage,
            "total_episodes": batch_job.total_episodes,
            "completed_episodes": batch_job.completed_episodes,
            "failed_episodes": batch_job.failed_episodes,
            "remaining_episodes": batch_job.total_episodes
            - batch_job.completed_episodes
            - batch_job.failed_episodes,
            "total_cost_usd": batch_job.total_cost_usd,
            "message": batch_job.message,
            "created_at": batch_job.created_at.isoformat(),
            "updated_at": batch_job.updated_at.isoformat(),
            # Progress tracking fields
            "progress_pct": batch_job.progress_pct,
            "total_work": batch_job.total_work,
            "completed_work": batch_job.completed_work,
        }
    )


@api_bp.route("/batch/<batch_id>/stop", methods=["POST"])
def batch_stop(batch_id):
    """Request graceful stop of a batch job."""
    job_manager = _get_job_manager()

    success = job_manager.stop_batch(batch_id)

    if not success:
        batch_job = job_manager.get_batch(batch_id)
        if not batch_job:
            return jsonify({"error": "Batch job not found"}), 404
        return jsonify(
            {
                "error": f"Cannot stop batch job in state: {batch_job.state}",
            }
        ), 400

    return jsonify(
        {
            "batch_id": batch_id,
            "message": "Stop requested, will complete current episode",
        }
    )


@api_bp.route("/batch/active", methods=["GET"])
def batch_active():
    """Check if there's an active batch job."""
    job_manager = _get_job_manager()
    active = job_manager.active_batch()

    if not active:
        return jsonify({"active": False})

    return jsonify(
        {
            "active": True,
            "batch_id": active.batch_id,
            "state": active.state,
            "current_episode_id": active.current_episode_id,
            "current_episode_title": active.current_episode_title,
            "current_stage": active.current_stage,
            "total_episodes": active.total_episodes,
            "completed_episodes": active.completed_episodes,
            "failed_episodes": active.failed_episodes,
            "remaining_episodes": active.total_episodes
            - active.completed_episodes
            - active.failed_episodes,
            "total_cost_usd": active.total_cost_usd,
            "progress_pct": active.progress_pct,
            "total_work": active.total_work,
            "completed_work": active.completed_work,
        }
    )


# ---------------------------------------------------------------------------
# Channel Management
# ---------------------------------------------------------------------------


@api_bp.route("/channels", methods=["GET"])
def list_channels():
    """List all channels."""
    session = _get_session()
    try:
        from btcedu.models.channel import Channel

        channels = session.query(Channel).order_by(Channel.created_at.desc()).all()

        return jsonify(
            {
                "channels": [
                    {
                        "id": ch.id,
                        "channel_id": ch.channel_id,
                        "name": ch.name,
                        "youtube_channel_id": ch.youtube_channel_id,
                        "rss_url": ch.rss_url,
                        "content_profile": ch.content_profile,
                        "is_active": ch.is_active,
                        "created_at": ch.created_at.isoformat(),
                    }
                    for ch in channels
                ]
            }
        )
    finally:
        session.close()


@api_bp.route("/channels", methods=["POST"])
def create_channel():
    """Create a new channel."""
    data = request.get_json() or {}

    name = data.get("name", "").strip()
    youtube_channel_id = data.get("youtube_channel_id", "").strip()
    rss_url = data.get("rss_url", "").strip()
    content_profile = (data.get("content_profile") or "bitcoin_podcast").strip()

    if not name:
        return jsonify({"error": "Channel name is required"}), 400

    if not youtube_channel_id and not rss_url:
        return jsonify({"error": "Either youtube_channel_id or rss_url is required"}), 400

    session = _get_session()
    try:
        import uuid

        from btcedu.models.channel import Channel

        # Generate a unique channel_id
        channel_id = youtube_channel_id or f"channel_{uuid.uuid4().hex[:8]}"

        # Check if channel already exists
        existing = session.query(Channel).filter(Channel.channel_id == channel_id).first()

        if existing:
            return jsonify({"error": f"Channel with ID {channel_id} already exists"}), 409

        channel = Channel(
            channel_id=channel_id,
            name=name,
            youtube_channel_id=youtube_channel_id or None,
            rss_url=rss_url or None,
            content_profile=content_profile,
            is_active=True,
        )

        session.add(channel)
        session.commit()
        session.refresh(channel)

        return jsonify(
            {
                "channel": {
                    "id": channel.id,
                    "channel_id": channel.channel_id,
                    "name": channel.name,
                    "youtube_channel_id": channel.youtube_channel_id,
                    "rss_url": channel.rss_url,
                    "content_profile": channel.content_profile,
                    "is_active": channel.is_active,
                    "created_at": channel.created_at.isoformat(),
                }
            }
        ), 201
    finally:
        session.close()


@api_bp.route("/channels/<int:channel_id>", methods=["DELETE"])
def delete_channel(channel_id):
    """Delete a channel."""
    session = _get_session()
    try:
        from btcedu.models.channel import Channel

        channel = session.query(Channel).filter(Channel.id == channel_id).first()

        if not channel:
            return jsonify({"error": "Channel not found"}), 404

        # Check if there are episodes associated with this channel
        episode_count = (
            session.query(Episode).filter(Episode.channel_id == channel.channel_id).count()
        )

        if episode_count > 0:
            return jsonify(
                {"error": f"Cannot delete channel with {episode_count} associated episodes"}
            ), 400

        session.delete(channel)
        session.commit()

        return jsonify({"message": "Channel deleted"}), 200
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Review System
# ---------------------------------------------------------------------------


@api_bp.route("/reviews")
def list_reviews():
    """List review tasks: pending + recent resolved."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import get_pending_reviews, pending_review_count
        from btcedu.models.review import ReviewTask

        pending = get_pending_reviews(session)

        # Also get last 20 resolved tasks
        from btcedu.models.review import ReviewStatus

        resolved = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.status.in_(
                    [
                        ReviewStatus.APPROVED.value,
                        ReviewStatus.REJECTED.value,
                        ReviewStatus.CHANGES_REQUESTED.value,
                    ]
                )
            )
            .order_by(ReviewTask.reviewed_at.desc())
            .limit(20)
            .all()
        )

        def _task_to_dict(t):
            ep = session.query(Episode).filter(Episode.episode_id == t.episode_id).first()
            return {
                "id": t.id,
                "episode_id": t.episode_id,
                "episode_title": ep.title if ep else None,
                "stage": t.stage,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
            }

        return jsonify(
            {
                "pending_count": pending_review_count(session),
                "tasks": [_task_to_dict(t) for t in pending + resolved],
            }
        )
    finally:
        session.close()


@api_bp.route("/reviews/count")
def review_count():
    """Return count of pending reviews (for badge)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import pending_review_count

        return jsonify({"pending_count": pending_review_count(session)})
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>")
def get_review_detail(review_id: int):
    """Return full review detail including diff data."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import get_review_detail

        try:
            detail = get_review_detail(session, review_id)
        except ValueError:
            return jsonify({"error": f"Review not found: {review_id}"}), 404

        return jsonify(detail)
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/metadata", methods=["PUT"])
def update_review_metadata(review_id: int):
    """Save reviewer edits to the proposed YouTube metadata for a render review."""
    session = _get_session()
    try:
        from btcedu.core.publisher import save_metadata_edits
        from btcedu.models.review import ReviewTask

        task = session.query(ReviewTask).filter_by(id=review_id).first()
        if task is None:
            return jsonify({"error": f"Review not found: {review_id}"}), 404
        if task.stage != "render":
            return jsonify({"error": "Metadata editing only available for video reviews"}), 400

        body = request.get_json(silent=True) or {}
        settings = _get_settings()
        try:
            updated = save_metadata_edits(task.episode_id, settings, body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"success": True, "youtube_metadata": updated})
    finally:
        session.close()


@api_bp.route("/reviews/batch-approve", methods=["POST"])
def batch_approve_reviews():
    """Approve multiple review tasks in one request."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import approve_review

        body = request.get_json(silent=True) or {}
        review_ids = body.get("review_ids", [])
        if not isinstance(review_ids, list) or not review_ids:
            return jsonify({"error": "review_ids must be a non-empty list"}), 400

        approved = []
        errors = []
        for rid in review_ids:
            try:
                approve_review(session, int(rid), notes="Batch approved via dashboard")
                approved.append(rid)
            except Exception as exc:
                errors.append({"id": rid, "error": str(exc)})

        return jsonify({"success": True, "approved": approved, "errors": errors})
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/approve", methods=["POST"])
def approve_review_route(review_id: int):
    """Approve a review task."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import approve_review

        body = request.get_json(silent=True) or {}
        rating = body.get("quality_rating")
        if rating is not None:
            rating = int(rating)
        try:
            decision = approve_review(
                session, review_id, notes=body.get("notes"), quality_rating=rating
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(
            {
                "success": True,
                "decision_id": decision.id,
                "decision": decision.decision,
            }
        )
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/reject", methods=["POST"])
def reject_review_route(review_id: int):
    """Reject a review task."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import reject_review
        from btcedu.models.review import ReviewTask

        body = request.get_json(silent=True) or {}
        notes = body.get("notes", "").strip()

        task = session.query(ReviewTask).filter(ReviewTask.id == review_id).first()
        if not task:
            return jsonify({"error": f"Review not found: {review_id}"}), 404

        if task.stage == "render" and not notes:
            return jsonify({"error": "Notes are required when rejecting render review"}), 400
        rating = body.get("quality_rating")
        if rating is not None:
            rating = int(rating)
        try:
            decision = reject_review(session, review_id, notes=notes or None, quality_rating=rating)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(
            {
                "success": True,
                "decision_id": decision.id,
                "decision": decision.decision,
            }
        )
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/request-changes", methods=["POST"])
def request_changes_route(review_id: int):
    """Request changes on a review task (requires notes in JSON body)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import request_changes

        body = request.get_json(silent=True) or {}
        notes = body.get("notes", "").strip()
        if not notes:
            return jsonify({"error": "Notes are required when requesting changes"}), 400
        rating = body.get("quality_rating")
        if rating is not None:
            rating = int(rating)

        try:
            decision = request_changes(session, review_id, notes=notes, quality_rating=rating)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(
            {
                "success": True,
                "decision_id": decision.id,
                "decision": decision.decision,
            }
        )
    finally:
        session.close()


@api_bp.route("/feedback")
def get_feedback():
    """Export all review feedback (ratings + notes) for analysis."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import get_all_feedback

        stage = request.args.get("stage")
        profile = request.args.get("profile")
        feedback = get_all_feedback(session, stage=stage, profile=profile)
        return jsonify({"feedback": feedback, "total": len(feedback)})
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Granular review item actions (Phase 5)
# ---------------------------------------------------------------------------


def _get_review_task_or_404(session, review_id: int):
    """Helper: fetch ReviewTask or return 404 response tuple."""
    from btcedu.models.review import ReviewTask

    task = session.query(ReviewTask).filter(ReviewTask.id == review_id).first()
    if not task:
        return None, (jsonify({"error": f"Review not found: {review_id}"}), 404)
    return task, None


def _check_review_actionable(task):
    """Return (is_ok, error_response_or_None)."""
    from btcedu.models.review import ReviewStatus

    if task.status not in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value):
        return False, (
            jsonify(
                {"error": f"Review {task.id} is '{task.status}', must be pending or in_review"}
            ),
            400,
        )
    return True, None


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/accept", methods=["POST"])
def accept_review_item(review_id: int, item_id: str):
    """Accept a single diff item."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.ACCEPTED.value)
        return jsonify({"success": True, "item_id": item_id, "action": "accepted"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/reject", methods=["POST"])
def reject_review_item(review_id: int, item_id: str):
    """Reject a single diff item (revert to original)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.REJECTED.value)
        return jsonify({"success": True, "item_id": item_id, "action": "rejected"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/edit", methods=["POST"])
def edit_review_item(review_id: int, item_id: str):
    """Set reviewer-provided replacement text for a single diff item."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        body = request.get_json(silent=True) or {}
        text_value = body.get("text", "").strip()
        if not text_value:
            return jsonify({"error": "Request body must include non-empty 'text' field"}), 400

        upsert_item_decision(
            session, review_id, item_id, ReviewItemAction.EDITED.value, edited_text=text_value
        )
        return jsonify(
            {
                "success": True,
                "item_id": item_id,
                "action": "edited",
                "edited_text": text_value,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/items/<string:item_id>/reset", methods=["POST"])
def reset_review_item(review_id: int, item_id: str):
    """Reset a diff item back to pending (undo any action)."""
    session = _get_session()
    try:
        from btcedu.core.reviewer import upsert_item_decision
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        upsert_item_decision(session, review_id, item_id, ReviewItemAction.PENDING.value)
        return jsonify({"success": True, "item_id": item_id, "action": "pending"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/reviews/<int:review_id>/apply", methods=["POST"])
def apply_review_items(review_id: int):
    """Assemble and write the reviewed sidecar file from per-item decisions.

    Does NOT approve the review. Returns pending_count so UI can warn reviewer.
    Pending items (no decision recorded) are treated as accepted (proposed change wins).
    This behavior is explicit and visible in the API response via pending_count.
    """
    session = _get_session()
    try:
        from btcedu.core.reviewer import apply_item_decisions, get_item_decisions
        from btcedu.models.review_item import ReviewItemAction

        task, err = _get_review_task_or_404(session, review_id)
        if err:
            return err
        ok, err = _check_review_actionable(task)
        if not ok:
            return err

        decisions = get_item_decisions(session, review_id)
        if not decisions:
            return (
                jsonify({"error": "No item decisions found. Act on at least one item first."}),
                400,
            )

        try:
            reviewed_file = apply_item_decisions(session, review_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # Count pending items among all diff items
        total_items = 0
        if task.diff_path:
            import json as _json
            from pathlib import Path as _Path

            try:
                diff_data = _json.loads(_Path(task.diff_path).read_text(encoding="utf-8"))
                all_items = diff_data.get("changes", diff_data.get("adaptations", []))
                total_items = len(all_items)
            except (OSError, _json.JSONDecodeError):
                pass

        pending_count = sum(
            1 for d in decisions.values() if d.action == ReviewItemAction.PENDING.value
        )
        # Items with no decision record at all are also pending
        pending_count += max(0, total_items - len(decisions))

        return jsonify(
            {
                "success": True,
                "reviewed_file": reviewed_file,
                "pending_count": max(0, pending_count),
                "total_items": total_items,
            }
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# TTS Audio (Sprint 8)
# ---------------------------------------------------------------------------


@api_bp.route("/episodes/<episode_id>/tts")
def get_tts_manifest(episode_id: str):
    """Return TTS manifest JSON for an episode."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400
    settings = _get_settings()
    manifest_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "tts", "manifest.json"
    )

    if not manifest_path:
        return jsonify({"error": "Episode not found"}), 404

    if not manifest_path.exists():
        return jsonify({"error": "TTS manifest not found"}), 404

    content = json.loads(manifest_path.read_text(encoding="utf-8"))
    return jsonify(content)


@api_bp.route("/episodes/<episode_id>/tts/<chapter_id>.mp3")
def get_tts_audio(episode_id: str, chapter_id: str):
    """Serve per-chapter MP3 audio file."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400
    settings = _get_settings()
    # Validate episode exists and construct safe path
    mp3_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "tts", f"{chapter_id}.mp3"
    )

    if not mp3_path:
        return jsonify({"error": "Episode not found"}), 404

    if not mp3_path.exists():
        return jsonify({"error": f"Audio file not found: {chapter_id}.mp3"}), 404

    return send_file(str(mp3_path), mimetype="audio/mpeg")


@api_bp.route("/episodes/<episode_id>/tts", methods=["POST"])
def trigger_tts(episode_id: str):
    """Trigger TTS generation job."""
    body = request.get_json(silent=True) or {}
    return _submit_job("tts", episode_id, force=body.get("force", False))


# ---------------------------------------------------------------------------
# Stock image endpoints (Phase 2 — Pexels pinning + ranking)
# ---------------------------------------------------------------------------


@api_bp.route("/episodes/<episode_id>/stock/candidates")
def get_stock_candidates(episode_id: str):
    """Return full candidates manifest with review info."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()
    manifest_path = _validate_episode_path(
        episode_id,
        Path(settings.outputs_dir),
        "images",
        "candidates",
        "candidates_manifest.json",
    )

    if not manifest_path or not manifest_path.exists():
        return jsonify({"error": "No candidates manifest found"}), 404

    content = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Attach review info
    session = _get_session()
    try:
        from btcedu.models.review import ReviewTask

        task = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == "stock_images",
            )
            .order_by(ReviewTask.created_at.desc())
            .first()
        )
        if task:
            content["review_task_id"] = task.id
            content["review_status"] = task.status
        else:
            content["review_task_id"] = None
            content["review_status"] = None
    finally:
        session.close()

    return jsonify(content)


@api_bp.route("/episodes/<episode_id>/stock/pin", methods=["POST"])
def pin_stock_image(episode_id: str):
    """Pin a specific candidate for a chapter."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    data = request.get_json(silent=True) or {}
    chapter_id = data.get("chapter_id")
    pexels_id = data.get("pexels_id")
    lock = data.get("lock", True)

    if not chapter_id or pexels_id is None:
        return jsonify({"error": "chapter_id and pexels_id are required"}), 400

    try:
        pexels_id = int(pexels_id)
    except (TypeError, ValueError):
        return jsonify({"error": "pexels_id must be an integer"}), 400

    settings = _get_settings()
    session = _get_session()
    try:
        from btcedu.core.stock_images import select_stock_image

        select_stock_image(session, episode_id, chapter_id, pexels_id, settings, lock=lock)
        return jsonify(
            {
                "status": "pinned",
                "chapter_id": chapter_id,
                "pexels_id": pexels_id,
            }
        )
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/stock/rank", methods=["POST"])
def rank_stock_images(episode_id: str):
    """Trigger LLM re-ranking for all unlocked chapters."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()
    session = _get_session()
    try:
        from btcedu.core.stock_images import rank_candidates

        data = request.get_json(silent=True) or {}
        force = data.get("force", False)
        result = rank_candidates(session, episode_id, settings, force=force)
        return jsonify(
            {
                "status": "ranked",
                "chapters_ranked": result.chapters_ranked,
                "chapters_skipped": result.chapters_skipped,
                "cost_usd": result.total_cost_usd,
            }
        )
    except (ValueError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/stock/candidate-image")
def get_stock_candidate_image(episode_id: str):
    """Serve a candidate image file."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    chapter = request.args.get("chapter", "")
    filename = request.args.get("filename", "")

    chapter = secure_filename(chapter)
    filename = secure_filename(filename)

    if not chapter or not filename:
        return jsonify({"error": "chapter and filename params required"}), 400

    # Only serve image files
    if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
        return jsonify({"error": "Only image files are served"}), 400

    settings = _get_settings()
    img_path = _validate_episode_path(
        episode_id,
        Path(settings.outputs_dir),
        "images",
        "candidates",
        chapter,
        filename,
    )

    if not img_path or not img_path.exists():
        return jsonify({"error": "Image not found"}), 404

    mimetype = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return send_file(str(img_path), mimetype=mimetype)


@api_bp.route("/episodes/<episode_id>/stock/candidate-video")
def get_stock_candidate_video(episode_id: str):
    """Serve a video candidate file (Phase 4)."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    chapter = request.args.get("chapter", "")
    filename = request.args.get("filename", "")

    chapter = secure_filename(chapter)
    filename = secure_filename(filename)

    if not chapter or not filename:
        return jsonify({"error": "chapter and filename params required"}), 400

    # Only serve MP4 video files
    if not filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only MP4 video files are served"}), 400

    settings = _get_settings()
    video_path = _validate_episode_path(
        episode_id,
        Path(settings.outputs_dir),
        "images",
        "candidates",
        chapter,
        filename,
    )

    if not video_path or not video_path.exists():
        return jsonify({"error": "Video not found"}), 404

    # Serve with range support for video seeking (HTTP 206 Partial Content)
    return send_file(str(video_path), mimetype="video/mp4", conditional=True)


# ---------------------------------------------------------------------------
# Image gallery endpoints (Sprint 12 — post-audit)
# ---------------------------------------------------------------------------


@api_bp.route("/episodes/<episode_id>/images")
def get_images_manifest(episode_id: str):
    """Return image manifest JSON for an episode."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400
    settings = _get_settings()
    manifest_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "images", "manifest.json"
    )

    if not manifest_path:
        return jsonify({"error": "Episode not found"}), 404

    if not manifest_path.exists():
        return jsonify({"error": "Image manifest not found"}), 404

    content = json.loads(manifest_path.read_text(encoding="utf-8"))
    return jsonify(content)


@api_bp.route("/episodes/<episode_id>/images/<filename>")
def get_image_file(episode_id: str, filename: str):
    """Serve a generated chapter image file (PNG)."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    filename = secure_filename(filename)
    if not episode_id or not filename:
        return jsonify({"error": "Invalid parameters"}), 400

    # Only serve PNG files
    if not filename.lower().endswith(".png"):
        return jsonify({"error": "Only PNG files are served"}), 400

    settings = _get_settings()
    img_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "images", filename)

    if not img_path:
        return jsonify({"error": "Episode not found"}), 404

    if not img_path.exists():
        return jsonify({"error": f"Image not found: {filename}"}), 404

    return send_file(str(img_path), mimetype="image/png")


# ---------------------------------------------------------------------------
# Render endpoints (Sprint 10)
# ---------------------------------------------------------------------------


@api_bp.route("/episodes/<episode_id>/render")
def get_render_manifest(episode_id: str):
    """Return render manifest JSON for an episode."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400
    settings = _get_settings()
    manifest_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "render", "render_manifest.json"
    )

    if not manifest_path:
        return jsonify({"error": "Episode not found"}), 404

    if not manifest_path.exists():
        return jsonify({"error": "Render manifest not found"}), 404

    content = json.loads(manifest_path.read_text(encoding="utf-8"))
    return jsonify(content)


@api_bp.route("/episodes/<episode_id>/render/progress")
def get_render_progress(episode_id: str):
    """Return live render progress for an episode.

    Reads ``render/progress.json`` written by the renderer on every chapter/
    concat event. Works regardless of how the render was triggered (pipeline
    autostart or web job). Returns ``{"stage": "idle"}`` when no run has
    written progress yet.
    """
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400
    settings = _get_settings()
    progress_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "render", "progress.json"
    )

    if not progress_path:
        return jsonify({"error": "Episode not found"}), 404

    if not progress_path.exists():
        return jsonify({"stage": "idle"})

    try:
        content = json.loads(progress_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"stage": "idle"})
    return jsonify(content)


@api_bp.route("/episodes/<episode_id>/render/draft.mp4")
def get_render_video(episode_id: str):
    """Serve draft video MP4 file with byte-range support for HTML5 scrubbing."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400
    settings = _get_settings()
    video_path = _validate_episode_path(
        episode_id, Path(settings.outputs_dir), "render", "draft.mp4"
    )

    if not video_path:
        return jsonify({"error": "Episode not found"}), 404

    if not video_path.exists():
        return jsonify({"error": "Draft video not found"}), 404

    resp = send_file(str(video_path), mimetype="video/mp4", conditional=True)
    # Force browsers to revalidate — the video file changes when the episode
    # is re-rendered but the URL doesn't, so aggressive caching would show
    # stale content (e.g. old placeholder colors) after a fix.
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp
    """Trigger render job."""
    body = request.get_json(silent=True) or {}
    return _submit_job("render", episode_id, force=body.get("force", False))


@api_bp.route("/episodes/<episode_id>/publish", methods=["POST"])
def trigger_publish(episode_id: str):
    """Trigger YouTube publish job for an approved episode."""
    body = request.get_json(silent=True) or {}
    return _submit_job(
        "publish",
        episode_id,
        force=body.get("force", False),
        privacy=body.get("privacy"),
    )


@api_bp.route("/episodes/<episode_id>/publish-status")
def get_publish_status(episode_id: str):
    """Return the latest publish job status for an episode."""
    session = _get_session()
    try:
        from btcedu.core.publisher import get_latest_publish_job

        job = get_latest_publish_job(session, episode_id)
        if not job:
            return jsonify({"status": "not_started", "youtube_video_id": None, "youtube_url": None})
        return jsonify(
            {
                "status": job.status,
                "youtube_video_id": job.youtube_video_id,
                "youtube_url": job.youtube_url,
                "published_at": job.published_at.isoformat() if job.published_at else None,
                "error_message": job.error_message,
            }
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Per-stage detail + restart (Jenkins-style)
# ---------------------------------------------------------------------------

_ALLOWED_STAGE_ACTIONS = frozenset(
    {
        "download",
        "transcribe",
        "transcript_analyze",
        "transcript_verify",
        "correct",
        "transcript_qa",
        "segment",
        "translate",
        "adapt",
        "chapterize",
        "frameextract",
        "imagegen",
        "tts",
        "anchorgen",
        "render",
        "publish",
    }
)


@api_bp.route("/episodes/<episode_id>/stage-runs")
def get_stage_runs(episode_id: str):
    """Return all PipelineRun records for an episode, grouped by stage."""
    session = _get_session()
    try:
        ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not ep:
            return jsonify({"error": "Episode not found"}), 404

        runs = (
            session.query(PipelineRun)
            .filter(PipelineRun.episode_id == ep.id)
            .order_by(PipelineRun.started_at.desc())
            .all()
        )

        stages: dict[str, dict] = {}
        for run in runs:
            stage_name = run.stage.value
            dur = None
            if run.completed_at and run.started_at:
                dur = round((run.completed_at - run.started_at).total_seconds(), 1)

            entry = {
                "id": run.id,
                "status": run.status.value,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "duration_seconds": dur,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "estimated_cost_usd": run.estimated_cost_usd,
                "git_commit": getattr(run, "git_commit", None),
                "error_message": run.error_message,
            }

            if stage_name not in stages:
                stages[stage_name] = {
                    "latest": entry,
                    "history": [],
                    "run_count": 1,
                }
            else:
                stages[stage_name]["history"].append(entry)
                stages[stage_name]["run_count"] += 1

        # Read last N log lines for this episode (filtered later by frontend)
        log_lines = []
        settings = _get_settings()
        log_path = Path(settings.logs_dir) / "episodes" / f"{episode_id}.log"
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8")
                log_lines = text.strip().split("\n")[-200:]
            except OSError:
                pass

        # Independent QA second opinion (shown in the Adapt stage detail)
        qa_review = None
        quality_gate = None
        try:
            from btcedu.core.qa_reviewer import load_qa_review, load_quality_gate

            qa_review = load_qa_review(settings, episode_id)
            quality_gate = load_quality_gate(settings, episode_id)
        except Exception:
            qa_review = None
            quality_gate = None

        return jsonify(
            {
                "stages": stages,
                "log_lines": log_lines,
                "episode_id": episode_id,
                "status": ep.status.value,
                "error_message": ep.error_message,
                "qa_review": qa_review,
                "quality_gate": quality_gate,
            }
        )
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/qa")
def get_qa_review(episode_id: str):
    """Return transcript QA, the merged translation quality gate, and legacy QA."""
    settings = _get_settings()
    quality_gate = None
    try:
        from btcedu.core.qa_reviewer import load_qa_review, load_quality_gate

        qa_review = load_qa_review(settings, episode_id)
        quality_gate = load_quality_gate(settings, episode_id)
    except Exception:
        qa_review = None
        quality_gate = None
    try:
        from btcedu.core.transcript_qa import load_transcript_qa

        transcript_qa = load_transcript_qa(settings, episode_id)
    except Exception:
        transcript_qa = None
    if not qa_review and not transcript_qa and not quality_gate:
        return jsonify({"error": "Noch keine QA-Auswertung für diese Episode."}), 404
    return jsonify(
        {
            "episode_id": episode_id,
            "transcript_qa": transcript_qa,
            "qa_review": qa_review,
            "quality_gate": quality_gate,
        }
    )


@api_bp.route("/episodes/<episode_id>/qa-rerun", methods=["POST"])
def qa_rerun(episode_id: str):
    """Targeted translate + adapt repair using the gate's structured findings,
    then a fresh QA gate. Stops after QA."""
    return _submit_job("qa_rerun", episode_id)


@api_bp.route("/episodes/<episode_id>/qa-rerun-all", methods=["POST"])
def qa_rerun_all(episode_id: str):
    """Resolve the quality gate (bounded targeted retries), then continue the
    pipeline — only past Review Gate 2 when GREEN or approved."""
    return _submit_job("qa_rerun_all", episode_id)


# ---------------------------------------------------------------------------
# QA panel actions (Phase 9): transcript QA approve/request-changes, and
# translation QA finding status curation. Both reuse existing ReviewTask /
# ReviewDecision conventions and JSON artifact history — no parallel DB.
# ---------------------------------------------------------------------------


def _qa_json_body():
    """Return a JSON object for QA mutations or a Flask error tuple."""
    if not request.is_json:
        return None, (jsonify({"error": "Content-Type must be application/json"}), 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return body, None


def _qa_quality_rating(body: dict):
    """Parse and validate an optional review quality rating before DB mutation."""
    value = body.get("quality_rating")
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, (jsonify({"error": "quality_rating must be an integer from 1 to 5"}), 400)
    if isinstance(value, int):
        rating = value
    elif isinstance(value, str) and re.fullmatch(r"[1-5]", value.strip()):
        rating = int(value)
    else:
        return None, (jsonify({"error": "quality_rating must be an integer from 1 to 5"}), 400)
    if rating < 1 or rating > 5:
        return None, (jsonify({"error": f"quality_rating must be 1-5, got {rating}"}), 400)
    return rating, None


def _get_or_create_transcript_qa_task(session, episode_id: str):
    """Return the actionable 'transcript_qa' ReviewTask for an episode.

    Reuses the most recent pending/in_review task if one exists (created by the
    pipeline's ``review_gate_transcript_qa`` stage). If none exists, creates one
    bound to the persisted transcript QA artifact — mirroring the exact artifact
    list the pipeline itself uses — but only when that artifact is present, so we
    never fabricate a review against content that hasn't been evaluated.

    Returns (task, None) on success, or (None, (response, status_code)) on error.
    """
    from btcedu.models.episode import Episode, EpisodeStatus
    from btcedu.models.review import ReviewStatus, ReviewTask

    ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not ep:
        return None, (jsonify({"error": "Episode not found"}), 404)
    if ep.status != EpisodeStatus.CORRECTED:
        return None, (
            jsonify(
                {
                    "error": (
                        "Transcript QA review actions are only valid while the episode "
                        f"is corrected; current status is '{ep.status.value}'."
                    )
                }
            ),
            409,
        )

    from btcedu.core.reviewer import review_task_matches_artifacts
    from btcedu.core.transcript_qa import load_transcript_qa
    from btcedu.core.transcript_qa import review_artifacts as transcript_qa_artifacts

    settings = _get_settings()
    qa = load_transcript_qa(settings, episode_id)
    if not qa:
        return None, (
            jsonify(
                {
                    "error": (
                        "No transcript QA artifact for this episode; "
                        "run transcript QA before reviewing it."
                    )
                }
            ),
            404,
        )
    artifacts = transcript_qa_artifacts(settings, episode_id)

    task = (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == "transcript_qa",
            ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value]),
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )
    if task:
        if not review_task_matches_artifacts(task, artifacts):
            return None, (
                jsonify(
                    {
                        "error": (
                            "Transcript QA review task is bound to obsolete artifacts; "
                            "rerun the transcript QA gate to create a current review."
                        )
                    }
                ),
                409,
            )
        return task, None

    from btcedu.core.reviewer import create_review_task

    try:
        task = create_review_task(
            session,
            episode_id,
            stage="transcript_qa",
            artifact_paths=artifacts,
        )
    except ValueError as exc:
        return None, (jsonify({"error": str(exc)}), 409)
    return task, None


@api_bp.route("/episodes/<episode_id>/qa/transcript/approve", methods=["POST"])
def approve_transcript_qa(episode_id: str):
    """Approve the transcript QA review for an episode.

    Reuses an existing pending/in_review 'transcript_qa' ReviewTask if present,
    or creates one bound to the persisted transcript QA artifact. Delegates to
    the standard ``approve_review`` review-infrastructure function so the audit
    trail (ReviewDecision + review_history.json) matches every other review.
    """
    session = _get_session()
    try:
        from btcedu.core.reviewer import approve_review

        body, err = _qa_json_body()
        if err:
            return err
        rating, err = _qa_quality_rating(body)
        if err:
            return err

        task, err = _get_or_create_transcript_qa_task(session, episode_id)
        if err:
            return err

        try:
            decision = approve_review(
                session, task.id, notes=body.get("notes"), quality_rating=rating
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(
            {
                "success": True,
                "review_task_id": task.id,
                "decision_id": decision.id,
                "decision": decision.decision,
            }
        )
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/qa/transcript/request-changes", methods=["POST"])
def request_changes_transcript_qa(episode_id: str):
    """Request changes on the transcript QA review for an episode (notes required).

    Reuses/creates the 'transcript_qa' ReviewTask exactly like approve, and
    delegates to the standard ``request_changes`` function, which reverts the
    episode (RG1) and writes .stale markers per the existing convention.
    """
    session = _get_session()
    try:
        from btcedu.core.reviewer import request_changes

        body, err = _qa_json_body()
        if err:
            return err
        notes = (body.get("notes") or "").strip()
        if not notes:
            return jsonify({"error": "Notes are required when requesting changes"}), 400
        rating, err = _qa_quality_rating(body)
        if err:
            return err

        task, err = _get_or_create_transcript_qa_task(session, episode_id)
        if err:
            return err

        try:
            decision = request_changes(session, task.id, notes=notes, quality_rating=rating)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(
            {
                "success": True,
                "review_task_id": task.id,
                "decision_id": decision.id,
                "decision": decision.decision,
            }
        )
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/qa/findings/<finding_id>/status", methods=["POST"])
def update_translation_finding_status(episode_id: str, finding_id: str):
    """Mutate a translation QA finding's lifecycle status (open/resolved/dismissed).

    Authorization mirrors existing item-decision conventions: mutation requires
    an existing 'translation_qa' ReviewTask for the episode (proof the quality
    gate currently governs review gate 2) in a pending/in_review state. The
    audit trail is the finding's own durable ``history`` inside
    ``translation_quality_gate.json`` — no parallel DB table is created.
    """
    session = _get_session()
    try:
        from btcedu.models.episode import Episode
        from btcedu.models.review import ReviewStatus, ReviewTask

        ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not ep:
            return jsonify({"error": "Episode not found"}), 404

        body, err = _qa_json_body()
        if err:
            return err
        status = str(body.get("status") or "").strip()

        from btcedu.core.qa_reviewer import FINDING_STATUSES, gate_review_artifacts
        from btcedu.core.reviewer import (
            refresh_review_task_artifacts,
            review_task_matches_artifacts,
        )

        if status not in FINDING_STATUSES:
            return (
                jsonify(
                    {"error": (f"status must be one of {sorted(FINDING_STATUSES)}, got '{status}'")}
                ),
                400,
            )

        task = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == "translation_qa",
            )
            .order_by(ReviewTask.created_at.desc())
            .first()
        )
        if not task:
            return (
                jsonify(
                    {
                        "error": (
                            "No translation QA review task for this episode; "
                            "nothing authorizes this change."
                        )
                    }
                ),
                404,
            )
        if task.status not in (ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value):
            return (
                jsonify(
                    {
                        "error": (
                            f"Translation QA review {task.id} is '{task.status}', "
                            "must be pending or in_review"
                        )
                    }
                ),
                400,
            )
        settings = _get_settings()
        artifacts = gate_review_artifacts(settings, episode_id)
        if not review_task_matches_artifacts(task, artifacts):
            return (
                jsonify(
                    {
                        "error": (
                            "Translation QA review task is bound to obsolete artifacts; "
                            "rerun the quality gate before changing findings."
                        )
                    }
                ),
                409,
            )

        from btcedu.core.qa_reviewer import update_finding_status

        note = body.get("note")
        try:
            gate = update_finding_status(settings, episode_id, finding_id, status, note=note)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        refresh_review_task_artifacts(
            session,
            task,
            gate_review_artifacts(settings, episode_id),
        )

        return jsonify(
            {
                "success": True,
                "review_task_id": task.id,
                "finding_id": finding_id,
                "status": status,
                "quality_gate": gate,
            }
        )
    finally:
        session.close()


@api_bp.route("/episodes/<episode_id>/stage/<stage_name>", methods=["POST"])
def run_single_stage(episode_id: str, stage_name: str):
    """Restart a single pipeline stage for an episode."""
    if stage_name not in _ALLOWED_STAGE_ACTIONS:
        return jsonify({"error": f"Unknown stage: {stage_name}"}), 400

    body = request.get_json(silent=True) or {}
    return _submit_job(stage_name, episode_id, force=body.get("force", True))


# ---------------------------------------------------------------------------
# Channel endpoints
# ---------------------------------------------------------------------------


@api_bp.route("/channels/<int:channel_id>", methods=["PATCH"])
def update_channel(channel_id):
    """Update channel fields (name, content_profile)."""
    data = request.get_json() or {}
    session = _get_session()
    try:
        from btcedu.models.channel import Channel

        channel = session.query(Channel).filter(Channel.id == channel_id).first()
        if not channel:
            return jsonify({"error": "Channel not found"}), 404

        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Channel name cannot be empty"}), 400
            channel.name = name
        if "content_profile" in data:
            channel.content_profile = (data.get("content_profile") or "bitcoin_podcast").strip()

        session.commit()
        return jsonify(
            {
                "channel": {
                    "id": channel.id,
                    "channel_id": channel.channel_id,
                    "name": channel.name,
                    "content_profile": channel.content_profile,
                    "is_active": channel.is_active,
                }
            }
        )
    finally:
        session.close()


@api_bp.route("/channels/<int:channel_id>/toggle", methods=["POST"])
def toggle_channel(channel_id):
    """Toggle channel active status."""
    session = _get_session()
    try:
        from btcedu.models.channel import Channel

        channel = session.query(Channel).filter(Channel.id == channel_id).first()

        if not channel:
            return jsonify({"error": "Channel not found"}), 404

        channel.is_active = not channel.is_active
        session.commit()

        return jsonify(
            {
                "channel_id": channel.id,
                "is_active": channel.is_active,
            }
        )
    finally:
        session.close()


@api_bp.route("/credits", methods=["GET"])
def get_credits():
    """Live credit balances + usage tracking for all external APIs."""
    from btcedu.services.credits_service import get_all_credits, to_dict

    settings = current_app.config["settings"]
    session_factory = current_app.config["session_factory"]
    with session_factory() as session:
        statuses = get_all_credits(session, settings)
    return jsonify(
        {
            "credits": [to_dict(s) for s in statuses],
            "generated_at": statuses[0].fetched_at if statuses else None,
        }
    )


# ---------------------------------------------------------------------------
# Weather dashboard endpoints
# ---------------------------------------------------------------------------


def _read_json_artifact(path: Path) -> dict | None:
    """Read and parse a JSON artifact, returning None on error."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _weather_chapter_detail(
    chapter: dict,
    images_dir: Path,
    episode_id: str,
    overrides: dict | None = None,
) -> dict | None:
    """Build weather detail for a single chapter, or None if not a weather chapter.

    Prefers persisted JSON artifacts written by the core pipeline. Falls back to
    recomputation when artifacts are missing (e.g., before first imagegen run).
    """
    from btcedu.core.weather.detector import detect_weather_story

    chapter_id = chapter.get("chapter_id", "")
    title = chapter.get("title", "")
    narration = chapter.get("narration", {})
    narration_text = narration.get("text", "") if isinstance(narration, dict) else ""
    duration = float(
        narration.get("estimated_duration_seconds", 30) if isinstance(narration, dict) else 30
    )

    # Check override: "normal" means skip weather display, "weather" forces it
    _overrides = overrides or {}
    override_value = _overrides.get(chapter_id)

    if override_value == "normal":
        return None  # Forced as non-weather

    # Try persisted detection artifact first
    detection_artifact = _read_json_artifact(images_dir / f"{chapter_id}_weather_detection.json")
    if detection_artifact:
        is_weather = detection_artifact.get("is_weather_story", False)
        detection_dict = detection_artifact
    else:
        # Recompute detection
        detection = detect_weather_story(
            title=title,
            narration_text=narration_text,
        )
        is_weather = detection.is_weather_story
        detection_dict = detection.model_dump()

    # Override "weather" forces this chapter as weather regardless of detection
    if override_value == "weather":
        is_weather = True

    if not is_weather:
        return None

    # Try persisted weather data artifact
    weather_data_artifact = _read_json_artifact(images_dir / f"{chapter_id}_weather.json")
    if weather_data_artifact:
        weather_data_dict = weather_data_artifact
        # Extract source spans from persisted data
        regions = weather_data_artifact.get("regions", [])
        source_spans = [r.get("source_span") for r in regions]
    else:
        # Recompute extraction
        from btcedu.core.weather.extractor import extract_weather_data

        weather_data = extract_weather_data(narration_text, story_id=chapter_id)
        weather_data_dict = weather_data.model_dump(mode="json")
        source_spans = [
            r.source_span.model_dump() if r.source_span else None for r in weather_data.regions
        ]

    # Try persisted validation artifact
    validation_artifact = _read_json_artifact(images_dir / f"{chapter_id}_weather_validation.json")
    if validation_artifact:
        validation_dict = validation_artifact
    else:
        # Recompute validation
        from btcedu.core.weather.extractor import extract_weather_data
        from btcedu.core.weather.validator import validate_weather_data

        if weather_data_artifact:
            from btcedu.core.weather.models import WeatherData

            wd = WeatherData.model_validate(weather_data_artifact)
        else:
            wd = extract_weather_data(narration_text, story_id=chapter_id)
        validation = validate_weather_data(wd, narration_text)
        validation_dict = validation.model_dump(mode="json")

    # Try persisted scene plan artifact
    scene_plan_artifact = _read_json_artifact(images_dir / f"{chapter_id}_weather_scenes.json")
    if scene_plan_artifact:
        scene_plan_dict = scene_plan_artifact
    else:
        # Recompute scene plan
        from btcedu.core.weather.extractor import extract_weather_data
        from btcedu.core.weather.models import WeatherData
        from btcedu.core.weather.scene_planner import plan_weather_scenes

        if weather_data_artifact:
            wd = WeatherData.model_validate(weather_data_artifact)
        else:
            wd = extract_weather_data(narration_text, story_id=chapter_id)
        scene_plan = plan_weather_scenes(wd, duration, story_id=chapter_id)
        scene_plan_dict = scene_plan.model_dump(mode="json")

    # Check rendered assets (PNG and MP4)
    weather_png = images_dir / f"{chapter_id}_weather.png"
    weather_mp4 = images_dir / f"{chapter_id}_weather.mp4"
    provenance = _read_json_artifact(images_dir / f"{chapter_id}_weather.provenance.json")

    # Determine cache status
    cache_status = "miss"
    if provenance:
        cache_status = "hit" if provenance.get("cache_key") else "stale"

    # Determine asset type and preview URL (relative)
    image_url = None
    video_url = None
    asset_type = "none"
    if weather_mp4.exists():
        video_url = f"api/episodes/{episode_id}/weather/{chapter_id}/video"
        asset_type = "video"
    if weather_png.exists():
        image_url = f"api/episodes/{episode_id}/weather/{chapter_id}/image"
        if asset_type == "none":
            asset_type = "image"

    return {
        "chapter_id": chapter_id,
        "title": title,
        "detection": detection_dict,
        "narration_text": narration_text,
        "weather_data": weather_data_dict,
        "source_spans": source_spans,
        "validation": validation_dict,
        "scene_plan": scene_plan_dict,
        "image_url": image_url,
        "video_url": video_url,
        "asset_type": asset_type,
        "image_exists": weather_png.exists(),
        "video_exists": weather_mp4.exists(),
        "fallback_level": provenance.get("method", "unknown") if provenance else "none",
        "renderer_version": provenance.get("renderer_version", "unknown") if provenance else None,
        "cache_status": cache_status,
        "provenance": provenance,
        "override": override_value,
    }


@api_bp.route("/episodes/<episode_id>/weather")
def get_weather_summary(episode_id: str):
    """Return weather chapter summary for an episode.

    Lists all detected weather chapters with extraction results, validation
    findings, scene plans, rendered image/video status, fallback level, renderer
    version, and cache status. Prefers persisted artifacts, falls back to
    recomputation.
    """
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()

    # Load chapters.json
    chapters_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "chapters.json")
    if not chapters_path:
        return jsonify({"error": "Episode not found"}), 404
    if not chapters_path.exists():
        return jsonify({"error": "No chapters available (run chapterize first)"}), 404

    try:
        chapters_doc = json.loads(chapters_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": f"Cannot read chapters: {e}"}), 500

    if isinstance(chapters_doc, list):
        chapters = chapters_doc
    else:
        chapters = chapters_doc.get("chapters", [])

    images_dir = Path(settings.outputs_dir) / episode_id / "images"

    # Load overrides
    overrides_path = images_dir / "weather_overrides.json"
    overrides: dict = {}
    if overrides_path.exists():
        try:
            loaded = json.loads(overrides_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                overrides = {k: v for k, v in loaded.items() if v in ("weather", "normal")}
        except (json.JSONDecodeError, OSError):
            pass

    weather_chapters = []
    for ch in chapters:
        detail = _weather_chapter_detail(ch, images_dir, episode_id, overrides=overrides)
        if detail:
            weather_chapters.append(detail)

    return jsonify(
        {
            "episode_id": episode_id,
            "weather_chapters": weather_chapters,
            "total_chapters": len(chapters),
            "weather_count": len(weather_chapters),
            "overrides": overrides,
        }
    )


@api_bp.route("/episodes/<episode_id>/weather/<chapter_id>/image")
def get_weather_image(episode_id: str, chapter_id: str):
    """Serve a rendered weather chapter image (PNG)."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400

    if not _SAFE_PATH_COMPONENT_RE.match(chapter_id):
        return jsonify({"error": "Invalid chapter ID"}), 400

    settings = _get_settings()
    filename = f"{chapter_id}_weather.png"
    img_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "images", filename)
    if not img_path:
        return jsonify({"error": "Episode not found"}), 404
    if not img_path.exists():
        return jsonify({"error": "Weather image not rendered yet"}), 404

    return send_file(str(img_path), mimetype="image/png")


@api_bp.route("/episodes/<episode_id>/weather/<chapter_id>/video")
def get_weather_video(episode_id: str, chapter_id: str):
    """Serve a rendered weather chapter video (MP4)."""
    from flask import send_file

    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400

    if not _SAFE_PATH_COMPONENT_RE.match(chapter_id):
        return jsonify({"error": "Invalid chapter ID"}), 400

    settings = _get_settings()
    filename = f"{chapter_id}_weather.mp4"
    video_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "images", filename)
    if not video_path:
        return jsonify({"error": "Episode not found"}), 404
    if not video_path.exists():
        return jsonify({"error": "Weather video not rendered yet"}), 404

    return send_file(str(video_path), mimetype="video/mp4")


@api_bp.route("/episodes/<episode_id>/weather/<chapter_id>/rerender", methods=["POST"])
def rerender_weather(episode_id: str, chapter_id: str):
    """Re-render a specific weather chapter via the imagegen stage force path.

    Submits a background job to run imagegen with force=True targeting only the
    specified chapter_id. Does NOT publish.
    """
    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400
    if not _SAFE_PATH_COMPONENT_RE.match(chapter_id):
        return jsonify({"error": "Invalid chapter ID"}), 400

    # Verify episode exists
    session = _get_session()
    try:
        ep = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not ep:
            return jsonify({"error": "Episode not found"}), 404
    finally:
        session.close()

    return _submit_job("imagegen", episode_id, force=True, chapter_id=chapter_id)


@api_bp.route("/episodes/<episode_id>/weather/<chapter_id>/override", methods=["POST"])
def set_weather_override(episode_id: str, chapter_id: str):
    """Set or clear a weather override for a specific chapter.

    Body: {"value": "weather"|"normal"|null}
      - "weather": Force this chapter through the weather renderer
      - "normal": Force this chapter through the normal image pipeline
      - null (or absent): Clear the override (use automatic detection)

    Writes to outputs/{episode}/images/weather_overrides.json and marks
    manifest.json.stale so the next imagegen run picks up the change.
    Does NOT publish.
    """
    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400
    if not _SAFE_PATH_COMPONENT_RE.match(chapter_id):
        return jsonify({"error": "Invalid chapter ID"}), 400

    body = request.get_json(silent=True) or {}
    value = body.get("value")

    # Validate enum value
    if value is not None and value not in ("weather", "normal"):
        return jsonify({"error": "value must be 'weather', 'normal', or null"}), 400

    settings = _get_settings()

    # Verify episode exists and get images dir path
    images_dir = _validate_episode_path(episode_id, Path(settings.outputs_dir), "images")
    if not images_dir:
        return jsonify({"error": "Episode not found"}), 404

    # Verify chapter exists in chapters.json
    chapters_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "chapters.json")
    if not chapters_path or not chapters_path.exists():
        return jsonify({"error": "No chapters available"}), 404

    try:
        chapters_doc = json.loads(chapters_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "Cannot read chapters"}), 500

    if isinstance(chapters_doc, list):
        chapters = chapters_doc
    else:
        chapters = chapters_doc.get("chapters", [])

    chapter_ids = {ch.get("chapter_id") for ch in chapters if ch.get("chapter_id")}
    if chapter_id not in chapter_ids:
        return jsonify({"error": f"Chapter not found: {chapter_id}"}), 404

    overrides_path = images_dir / "weather_overrides.json"
    images_dir.mkdir(parents=True, exist_ok=True)
    lock_path = images_dir / ".weather_overrides.lock"
    with _weather_overrides_lock, lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        overrides: dict = {}
        if overrides_path.exists():
            try:
                loaded = json.loads(overrides_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                logger.error("Cannot read weather overrides for %s: %s", episode_id, error)
                return jsonify({"error": "Cannot read weather overrides"}), 500
            if not isinstance(loaded, dict):
                return jsonify({"error": "Invalid weather overrides artifact"}), 500
            overrides = {k: v for k, v in loaded.items() if v in ("weather", "normal")}

        if value is None:
            overrides.pop(chapter_id, None)
        else:
            overrides[chapter_id] = value

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix="weather-overrides-",
                suffix=".json",
                dir=images_dir,
                encoding="utf-8",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(overrides, temporary, indent=2, ensure_ascii=False)
            temporary_path.replace(overrides_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        # Keep invalidation in the same critical section as the override update.
        stale_path = images_dir / "manifest.json.stale"
        stale_path.write_text(
            json.dumps({"reason": f"weather_override_{chapter_id}", "value": value}),
            encoding="utf-8",
        )

    return jsonify(
        {
            "ok": True,
            "chapter_id": chapter_id,
            "value": value,
            "overrides": overrides,
        }
    )


@api_bp.route("/episodes/<episode_id>/weather/overrides")
def get_weather_overrides(episode_id: str):
    """Return the current weather_overrides.json for an episode."""
    episode_id = secure_filename(episode_id)
    if not episode_id:
        return jsonify({"error": "Invalid episode ID"}), 400

    settings = _get_settings()
    images_dir = _validate_episode_path(episode_id, Path(settings.outputs_dir), "images")
    if not images_dir:
        return jsonify({"error": "Episode not found"}), 404

    overrides_path = images_dir / "weather_overrides.json"
    overrides: dict = {}
    if overrides_path.exists():
        try:
            loaded = json.loads(overrides_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                overrides = {k: v for k, v in loaded.items() if v in ("weather", "normal")}
        except (json.JSONDecodeError, OSError):
            pass

    return jsonify({"overrides": overrides})


@api_bp.route("/episodes/<episode_id>/weather/<chapter_id>/detail")
def get_weather_chapter_detail(episode_id: str, chapter_id: str):
    """Return full weather detail for a single chapter."""
    episode_id = secure_filename(episode_id)
    chapter_id = secure_filename(chapter_id)
    if not episode_id or not chapter_id:
        return jsonify({"error": "Invalid parameters"}), 400
    if not _SAFE_PATH_COMPONENT_RE.match(chapter_id):
        return jsonify({"error": "Invalid chapter ID"}), 400

    settings = _get_settings()

    chapters_path = _validate_episode_path(episode_id, Path(settings.outputs_dir), "chapters.json")
    if not chapters_path:
        return jsonify({"error": "Episode not found"}), 404
    if not chapters_path.exists():
        return jsonify({"error": "No chapters available"}), 404

    try:
        chapters_doc = json.loads(chapters_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": f"Cannot read chapters: {e}"}), 500

    if isinstance(chapters_doc, list):
        chapters = chapters_doc
    else:
        chapters = chapters_doc.get("chapters", [])

    images_dir = Path(settings.outputs_dir) / episode_id / "images"

    # Load overrides
    overrides_path = images_dir / "weather_overrides.json"
    overrides: dict = {}
    if overrides_path.exists():
        try:
            loaded = json.loads(overrides_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                overrides = {k: v for k, v in loaded.items() if v in ("weather", "normal")}
        except (json.JSONDecodeError, OSError):
            pass

    # Find the specific chapter
    target_ch = None
    for ch in chapters:
        if ch.get("chapter_id") == chapter_id:
            target_ch = ch
            break

    if not target_ch:
        return jsonify({"error": f"Chapter not found: {chapter_id}"}), 404

    detail = _weather_chapter_detail(target_ch, images_dir, episode_id, overrides=overrides)
    if not detail:
        return jsonify({"error": "Chapter is not a weather story"}), 404

    return jsonify(detail)
