"""Flask application factory for the btcedu web dashboard."""

import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

from btcedu.config import get_settings
from btcedu.db import get_session_factory, init_db
from btcedu.web.api import api_bp
from btcedu.web.jobs import JobManager

logger = logging.getLogger(__name__)


def create_app(settings=None) -> Flask:
    """Create and configure the Flask app.

    Args:
        settings: Optional Settings override (used in tests).
    """
    app = Flask(__name__)

    if settings is None:
        settings = get_settings()

    app.config["settings"] = settings
    init_db(settings.database_url)
    app.config["session_factory"] = get_session_factory(settings.database_url)

    # Initialize background job manager
    logs_dir = settings.logs_dir
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    app.config["job_manager"] = JobManager(logs_dir)

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.errorhandler(Exception)
    def handle_exception(e):
        """Global exception handler for unhandled errors."""
        # Log full stack trace to web_errors.log
        error_log = Path(logs_dir) / "web_errors.log"
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*80}\n")
                f.write(f"Timestamp: {ts}\n")
                f.write(f"Method: {request.method}\n")
                f.write(f"Path: {request.path}\n")
                f.write(f"Error: {str(e)}\n")
                f.write(f"Traceback:\n")
                f.write(traceback.format_exc())
                f.write(f"{'='*80}\n")
        except OSError:
            pass

        # Log to console as well
        logger.exception("Unhandled exception in request")

        # Check if it's a database schema error
        error_str = str(e).lower()
        if "no such column" in error_str or "no such table" in error_str:
            return jsonify({
                "error": "Database schema out of date",
                "hint": "Run `btcedu migrate` on the server to update the database schema.",
                "details": str(e)
            }), 500

        # Return generic error response
        return jsonify({
            "error": "Internal server error",
            "details": str(e)
        }), 500

    @app.before_request
    def _start_timer():
        g.start_time = time.monotonic()

    @app.before_request
    def _check_migrations():
        """Check if migrations are needed on startup (once)."""
        if not hasattr(app, "_migrations_checked"):
            from btcedu.migrations import get_pending_migrations
            session = app.config["session_factory"]()
            try:
                pending = get_pending_migrations(session)
                if pending:
                    migration_list = ", ".join(m.version for m in pending)
                    logger.warning(
                        f"Database migration required. Pending: {migration_list}. "
                        f"Run: btcedu migrate"
                    )
            except Exception as e:
                logger.warning(f"Could not check migration status: {e}")
            finally:
                session.close()
            app._migrations_checked = True

    @app.after_request
    def _log_request(response):
        duration_ms = (time.monotonic() - getattr(g, "start_time", time.monotonic())) * 1000
        logger.info(
            "%s %s %s %.0fms",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
        )
        try:
            web_log = Path(logs_dir) / "web.log"
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with open(web_log, "a", encoding="utf-8") as f:
                f.write(f"{ts} {request.method} {request.path} {response.status_code} {duration_ms:.0f}ms\n")
        except OSError:
            pass
        return response

    return app
