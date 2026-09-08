"""Login and logout routes.

Separate from ``auth.py`` so the policy (what protection exists) and the pages
(how an operator uses it) stay legible on their own.
"""

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from btcedu.web.auth import authenticate, end_session, safe_next, start_session

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

# One message for every rejection. "Unknown user" and "wrong password" are two
# different pieces of information, and the first one is free reconnaissance.
_GENERIC_ERROR = "Benutzername oder Passwort ist falsch."


def _within_mount(path: str) -> str:
    """Point a validated internal path at this app's mount, not the site root.

    ``safe_next`` guarantees the target is app-internal, but "internal" is
    expressed without the sub-path a reverse proxy strips. Redirecting to it
    verbatim from behind ``/dashboard`` sends the browser to the site root
    instead -- which on the production host is a different application.
    """
    if not path:
        return ""
    root = request.script_root or ""
    if not root or path == root or path.startswith(root + "/"):
        return path
    return root + path


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    policy = current_app.config["auth_policy"]
    throttle = current_app.config["login_throttle"]

    if not policy.enabled:
        return redirect(url_for("index"))
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    target = safe_next(request.args.get("next") or request.form.get("next") or "")

    if request.method == "GET":
        return render_template("login.html", next=target, error=None), 200

    username = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")

    wait = throttle.retry_after(username)
    if wait:
        # Never say which of the two halves was wrong, and never say how many
        # attempts remain: both would tell an attacker how close they are.
        logger.warning("Login refused during cooldown (%ss remaining)", wait)
        return render_template(
            "login.html",
            next=target,
            error=f"Zu viele Fehlversuche. Bitte {wait} Sekunden warten.",
        ), 429

    operator = authenticate(policy, username, password)
    if operator is None:
        throttle.record_failure(username)
        # The username is not logged either. It is frequently a password typed
        # into the wrong field, and this log is written to disk.
        logger.warning("Failed dashboard login attempt")
        return render_template("login.html", next=target, error=_GENERIC_ERROR), 401

    throttle.record_success(username)
    start_session(operator)
    logger.info("Dashboard login: %s", operator.operator_ref)
    return redirect(_within_mount(target) or url_for("index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """POST only, session only, token only.

    A logout reachable by GET is a link anyone can plant; a logout reachable
    without a session or a token is a request an attacker can aim at an operator
    in the middle of a review. It is a state change like any other and is
    treated like one.
    """
    if current_user.is_authenticated:
        logger.info("Dashboard logout: %s", current_user.operator_ref)
    end_session()
    flash("Abgemeldet.", "ok")
    return redirect(url_for("auth.login"))
