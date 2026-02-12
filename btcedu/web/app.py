"""Flask application factory for the btcedu web dashboard."""

from flask import Flask, render_template

from btcedu.config import get_settings
from btcedu.db import get_session_factory, init_db
from btcedu.web.api import api_bp


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

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.route("/")
    def index():
        return render_template("index.html")

    return app
