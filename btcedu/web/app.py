"""Flask application factory for the btcedu web dashboard."""

import logging
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from btcedu.config import get_settings
from btcedu.db import get_session_factory, init_db
from btcedu.web.api import api_bp
from btcedu.web.jobs import JobManager

logger = logging.getLogger(__name__)


class _ConfiguredPrefixOnly:
    """Discard ``X-Forwarded-Prefix`` unless it is the mount the operator set.

    ``ProxyFix`` believes whichever prefix arrives, which is correct only as
    long as something in front rewrites the header on every request. That is
    one Caddy line away from not being true, and the failure is silent: a
    request carrying its own prefix would make the app hand out login and
    static URLs pointing somewhere else entirely. Comparing against the
    configured value first means the header can only ever confirm the mount
    that is already known, never introduce a new one.
    """

    def __init__(self, app, allowed_prefix: str):
        self.app = app
        self.allowed_prefix = allowed_prefix

    def __call__(self, environ, start_response):
        if environ.get("HTTP_X_FORWARDED_PREFIX") != self.allowed_prefix:
            environ.pop("HTTP_X_FORWARDED_PREFIX", None)
        return self.app(environ, start_response)


def _normalize_prefix(raw: str) -> str:
    """Return a mount point as ``/dashboard``: leading slash, no trailing one."""
    prefix = (raw or "").strip()
    if not prefix or prefix == "/":
        return ""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


def create_app(settings=None) -> Flask:
    """Create and configure the Flask app.

    Args:
        settings: Optional Settings override (used in tests).
    """
    app = Flask(__name__)
    # Reject oversized multipart bodies before Flask parses/spools request.files.
    # The intro MP3 itself is limited to 20 MB; 1 MB covers multipart overhead.
    app.config["MAX_CONTENT_LENGTH"] = 21 * 1024 * 1024

    if settings is None:
        settings = get_settings()

    app.config["settings"] = settings

    # Served under a stripped sub-path by the reverse proxy: teach the app its
    # mount so `url_for` builds `/dashboard/login` rather than `/login`. Only
    # the prefix is taken from the proxy — the client address, scheme, host and
    # port stay whatever the WSGI server reports, because nothing here needs
    # them and every trusted header is one more thing to get wrong.
    forwarded_prefix = _normalize_prefix(getattr(settings, "web_forwarded_prefix", ""))
    app.config["FORWARDED_PREFIX"] = forwarded_prefix
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=0, x_proto=0, x_host=0, x_port=0, x_prefix=1
    )
    app.wsgi_app = _ConfiguredPrefixOnly(app.wsgi_app, forwarded_prefix)

    # The dashboard runs as its own service and writes its own log; the same
    # third-party error texts reach it as reach the CLI.
    from btcedu.utils.secrets import install_log_redaction

    install_log_redaction(settings)

    init_db(settings.database_url)
    app.config["session_factory"] = get_session_factory(settings.database_url)

    # Initialize background job manager
    logs_dir = settings.logs_dir
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    app.config["job_manager"] = JobManager(logs_dir)

    # Authentication and CSRF are wired before any blueprint so a route cannot
    # be registered into an unprotected app by accident. This raises and stops
    # the service when the configuration is unsafe.
    from btcedu.web.auth import init_auth

    init_auth(app, settings)

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/whatsapp")
    def whatsapp_page():
        """Pairing page for the WhatsApp notification service (QR code)."""
        return render_template("whatsapp.html")

    @app.errorhandler(Exception)
    def handle_exception(e):
        """Global exception handler for unhandled errors."""
        # HTTPExceptions are deliberate outcomes, not failures: they already
        # carry the right status code. Falling through to the generic branch
        # below turned every unknown URL and every wrong HTTP method into a 500
        # with a full traceback in the log.
        if isinstance(e, HTTPException):
            return jsonify({"error": e.name, "details": e.description}), e.code

        # Log full stack trace to web_errors.log
        error_log = Path(logs_dir) / "web_errors.log"
        try:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 80}\n")
                f.write(f"Timestamp: {ts}\n")
                f.write(f"Method: {request.method}\n")
                f.write(f"Path: {request.path}\n")
                f.write(f"Error: {str(e)}\n")
                f.write("Traceback:\n")
                f.write(traceback.format_exc())
                f.write(f"{'=' * 80}\n")
        except OSError:
            pass

        # Log to console as well
        logger.exception("Unhandled exception in request")

        # Check if it's a database schema error
        error_str = str(e).lower()
        if "no such column" in error_str or "no such table" in error_str:
            return jsonify(
                {
                    "error": "Database schema out of date",
                    "hint": "Run `btcedu migrate` on the server to update the database schema.",
                    "details": str(e),
                }
            ), 500

        # Return generic error response
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

    @app.errorhandler(RequestEntityTooLarge)
    def handle_oversized_upload(_error):
        return jsonify({"error": "The upload must not exceed 20 MB."}), 413

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
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            with open(web_log, "a", encoding="utf-8") as f:
                log_line = (
                    f"{ts} {request.method} {request.path} "
                    f"{response.status_code} {duration_ms:.0f}ms\n"
                )
                f.write(log_line)
        except OSError:
            pass
        return response

    return app
