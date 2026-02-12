"""Flask application factory for the btcedu web dashboard."""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, render_template, request

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

    @app.before_request
    def _start_timer():
        g.start_time = time.monotonic()

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
