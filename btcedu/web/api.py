"""API blueprint for the btcedu web dashboard."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@api_bp.route("/health")
def health():
    """Health check for monitoring and proxy verification."""
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
    })


def _get_session():
    return current_app.config["session_factory"]()


def _get_settings():
    return current_app.config["settings"]


def _get_job_manager():
    return current_app.config["job_manager"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_presence(episode_id: str, settings) -> dict[str, bool]:
    """Check which output files exist for an episode."""
    raw = Path(settings.raw_data_dir) / episode_id
    trans = Path(settings.transcripts_dir) / episode_id
    chunks = Path(settings.chunks_dir) / episode_id
    out = Path(settings.outputs_dir) / episode_id

    return {
        "audio": any(raw.glob("audio.*")) if raw.exists() else False,
        "transcript_raw": (trans / "transcript.de.txt").exists(),
        "transcript_clean": (trans / "transcript.clean.de.txt").exists(),
        "chunks": (chunks / "chunks.jsonl").exists(),
        "outline": (out / "outline.tr.md").exists(),
        "script": (out / "script.long.tr.md").exists(),
        "shorts": (out / "shorts.tr.json").exists(),
        "visuals": (out / "visuals.json").exists(),
        "qa": (out / "qa.json").exists(),
        "publishing": (out / "publishing_pack.json").exists(),
    }


def _episode_to_dict(ep: Episode, settings) -> dict:
    """Serialize an Episode ORM object to a JSON-safe dict."""
    return {
        "episode_id": ep.episode_id,
        "title": ep.title,
        "status": ep.status.value,
        "source": ep.source,
        "url": ep.url,
        "published_at": ep.published_at.isoformat() if ep.published_at else None,
        "detected_at": ep.detected_at.isoformat() if ep.detected_at else None,
        "completed_at": ep.completed_at.isoformat() if ep.completed_at else None,
        "error_message": ep.error_message,
        "retry_count": ep.retry_count,
        "files": _file_presence(ep.episode_id, settings),
    }


def _submit_job(action, episode_id, **kwargs):
    """Submit a background job, return (response, status_code)."""
    mgr = _get_job_manager()
    active = mgr.active_for_episode(episode_id)
    if active:
        return jsonify({
            "error": "Job already active",
            "job_id": active.job_id,
        }), 409

    job = mgr.submit(
        action=action,
        episode_id=episode_id,
        app=current_app._get_current_object(),
        **kwargs,
    )
    return jsonify({"job_id": job.job_id, "state": job.state}), 202


# ---------------------------------------------------------------------------
# Episode list + detail
# ---------------------------------------------------------------------------

@api_bp.route("/episodes")
def list_episodes():
    session = _get_session()
    settings = _get_settings()
    try:
        episodes = (
            session.query(Episode)
            .order_by(Episode.published_at.desc().nullslast())
            .all()
        )
        return jsonify([_episode_to_dict(ep, settings) for ep in episodes])
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

        data = _episode_to_dict(ep, settings)

        # Add cost info from pipeline runs
        runs = (
            session.query(PipelineRun)
            .filter(PipelineRun.episode_id == ep.id)
            .all()
        )
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
    """Detect new episodes — synchronous (fast network I/O)."""
    from btcedu.core.detector import detect_episodes

    session = _get_session()
    settings = _get_settings()
    try:
        result = detect_episodes(session, settings)
        return jsonify({
            "success": True,
            "found": result.found,
            "new": result.new,
            "total": result.total,
        })
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


@api_bp.route("/episodes/<episode_id>/chunk", methods=["POST"])
def chunk_episode(episode_id: str):
    body = request.get_json(silent=True) or {}
    return _submit_job("chunk", episode_id, force=body.get("force", False))


@api_bp.route("/episodes/<episode_id>/generate", methods=["POST"])
def generate_episode(episode_id: str):
    body = request.get_json(silent=True) or {}
    return _submit_job(
        "generate",
        episode_id,
        force=body.get("force", False),
        dry_run=body.get("dry_run", False),
        top_k=body.get("top_k", 16),
    )


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
        ep = session.query(Episode).filter(
            Episode.episode_id == job.episode_id,
        ).first()
        data["episode_status"] = ep.status.value if ep else None
    finally:
        session.close()

    return jsonify(data)


@api_bp.route("/episodes/<episode_id>/action-log")
def episode_action_log(episode_id: str):
    settings = _get_settings()
    tail = request.args.get("tail", 200, type=int)
    log_path = Path(settings.logs_dir) / "episodes" / f"{episode_id}.log"

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
    "outline": ("outputs_dir", "{eid}/outline.tr.md"),
    "script": ("outputs_dir", "{eid}/script.long.tr.md"),
    "shorts": ("outputs_dir", "{eid}/shorts.tr.json"),
    "visuals": ("outputs_dir", "{eid}/visuals.json"),
    "qa": ("outputs_dir", "{eid}/qa.json"),
    "publishing": ("outputs_dir", "{eid}/publishing_pack.json"),
}


@api_bp.route("/episodes/<episode_id>/files/<file_type>")
def get_file(episode_id: str, file_type: str):
    settings = _get_settings()

    # Handle report separately (find latest)
    if file_type == "report":
        report_dir = Path(settings.reports_dir) / episode_id
        if not report_dir.exists():
            return jsonify({"error": "No reports found"}), 404
        reports = sorted(report_dir.glob("report_*.json"), reverse=True)
        if not reports:
            return jsonify({"error": "No reports found"}), 404
        path = reports[0]
    elif file_type in _FILE_MAP:
        dir_attr, pattern = _FILE_MAP[file_type]
        base = getattr(settings, dir_attr)
        path = Path(base) / pattern.format(eid=episode_id)
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
            stages.append({
                "stage": row.stage.value,
                "runs": row.runs,
                "input_tokens": row.input_tokens or 0,
                "output_tokens": row.output_tokens or 0,
                "cost_usd": round(cost_val, 6),
            })

        ep_count = session.query(
            func.count(func.distinct(PipelineRun.episode_id))
        ).scalar() or 0

        return jsonify({
            "stages": stages,
            "total_usd": round(grand_total, 6),
            "episodes_processed": ep_count,
            "avg_per_episode": round(grand_total / ep_count, 4) if ep_count else 0,
        })
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
        all_eps = session.query(Episode).filter(
            Episode.status.notin_([EpisodeStatus.NEW, EpisodeStatus.GENERATED, EpisodeStatus.COMPLETED])
        ).all()
        for ep in all_eps:
            files = _file_presence(ep.episode_id, settings)
            if ep.status == EpisodeStatus.DOWNLOADED and not files.get("transcript_raw"):
                incomplete.append({
                    "episode_id": ep.episode_id,
                    "title": ep.title,
                    "status": ep.status.value,
                    "missing": "transcript",
                })
            elif ep.status == EpisodeStatus.TRANSCRIBED and not files.get("chunks"):
                incomplete.append({
                    "episode_id": ep.episode_id,
                    "title": ep.title,
                    "status": ep.status.value,
                    "missing": "chunks",
                })
            elif ep.status == EpisodeStatus.CHUNKED and not files.get("outline"):
                incomplete.append({
                    "episode_id": ep.episode_id,
                    "title": ep.title,
                    "status": ep.status.value,
                    "missing": "generated content",
                })

        return jsonify({
            "new_episodes": [
                {"episode_id": ep.episode_id, "title": ep.title}
                for ep in new_eps
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
        })
    finally:
        session.close()
