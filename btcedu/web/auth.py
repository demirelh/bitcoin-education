"""Authentication, CSRF and operator identity for the dashboard.

Until now the dashboard was protected by nothing but the address bar. Anyone who
reached ``127.0.0.1:8091`` — a second service on the Pi, a mis-scoped proxy
rule, a forwarded port during debugging — could approve a review, reset a
circuit breaker, spend the avatar budget or read every episode path on disk. The
documented deployment puts Caddy basicauth in front, but that is a property of a
file nobody in this repository controls, and it does not exist at all for the
port the WSGI server binds.

Three separate mechanisms, deliberately not collapsed into one:

* **Authentication** (Flask-Login) answers *who is this*. Everything except an
  explicit allowlist requires it.
* **CSRF** (Flask-WTF) answers *did this operator mean to send this request*.
  Being logged in is not consent: a foreign page can make an authenticated
  browser POST. The Origin check from WP-7 stays as a second layer, because a
  token that leaks is a token that works.
* **Operator identity** answers *whose name goes in the audit row*. It comes
  from the session and only from the session; a client-supplied ``operator_ref``
  is now ignored.

Password hashing uses ``werkzeug.security``, which ships with Flask and defaults
to scrypt with a per-password salt. No cryptography is written here.
"""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from flask import (
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,  # noqa: F401 — re-exported for route modules
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.security import check_password_hash

from btcedu.core.operator_identity import web_operator

logger = logging.getLogger(__name__)

csrf = CSRFProtect()

# Endpoints reachable without a session. Everything else is protected by
# default, so a new route is safe the moment it is written and a public one is a
# deliberate line in this list.
#
# * the login page and its POST, or nobody could ever log in
# * the static files: stylesheet and script, no dynamic data of any kind
# * a minimal health check for systemd and the proxy, stripped of everything
#   that would tell an anonymous caller what this machine does
#
# The logout POST is deliberately *not* here. It was, on the reasoning that a
# logout is harmless and should still work on an expired session, but that is
# wrong on both halves: a public state-changing endpoint is one an attacker can
# aim at the operator mid-task, and an expired session has nothing left to log
# out of. It now needs a session and a token like every other POST.
PUBLIC_ENDPOINTS = frozenset(
    {
        "auth.login",
        "static",
        "api.health",
    }
)

# Loopback addresses. Running without authentication is a development mode, and
# a development mode that is reachable from the network is a production hole.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "127.0.0.1:8091"})

_MIN_SECRET_LENGTH = 32


class AuthConfigError(RuntimeError):
    """The dashboard cannot be started safely with this configuration.

    Raised at start-up, never during a request: an unsafe dashboard must fail to
    come up rather than serve one request before anyone notices.
    """


@dataclass(frozen=True)
class AuthPolicy:
    """The decided, validated authentication configuration."""

    enabled: bool
    username: str
    password_hash: str
    display_name: str
    lifetime: timedelta
    cookie_secure: bool
    max_attempts: int
    cooldown_seconds: int
    cooldown_max_seconds: int


class Operator(UserMixin):
    """The single configured dashboard operator.

    Deliberately not a database row. A second operator is a real feature with
    real questions attached — per-user audit, revocation, password reset — and
    inventing half of it here would produce a user table nobody maintains. The
    interface (``id``, ``operator_ref``) is the one a user table would have, so
    the later change is local.
    """

    def __init__(self, username: str, display_name: str = ""):
        self.id = username
        self.username = username
        self.display_name = display_name or username

    @property
    def operator_ref(self) -> str:
        """The stable identity written to the audit trail."""
        return web_operator(self.username)


def _int_setting(settings, name: str, default: int) -> int:
    """Read a numeric setting from a real Settings or a lightweight test stub."""
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _is_loopback(host: str) -> bool:
    """Is this address reachable only from the machine itself?

    Anything not positively recognised is treated as external. An unparseable
    bind address is exactly the case where guessing "probably local" would be
    the expensive mistake.
    """
    raw = str(host or "").strip().lower()
    if not raw:
        return True  # unset means the default, which is loopback
    raw = raw.strip("[]")
    if raw in {"localhost", "::1"}:
        return True
    # Strip a trailing port, but only for IPv4/host forms: "::" is an address.
    if raw.count(":") == 1:
        raw = raw.split(":")[0]
    if raw in {"localhost", "::1"}:
        return True
    return raw.startswith("127.")


def resolve_policy(settings) -> AuthPolicy:
    """Validate the configuration, or refuse to run.

    The two failure modes are opposite and both fatal:

    * authentication on, credentials missing — nobody could log in, and a
      half-configured login is the classic way to end up switching it off "for
      now".
    * authentication off, bound to something other than loopback — the
      dashboard would be served to the network without a password.
    """
    enabled = bool(getattr(settings, "web_auth_enabled", True))
    bind = str(getattr(settings, "web_bind_host", "127.0.0.1") or "")

    if not enabled:
        if not _is_loopback(bind):
            raise AuthConfigError(
                "WEB_AUTH_ENABLED=false is only allowed for loopback development, "
                f"but WEB_BIND_HOST is {bind!r}. Set WEB_AUTH_ENABLED=true and "
                "configure an operator, or bind the server to 127.0.0.1."
            )
        logger.warning(
            "Dashboard authentication is DISABLED. This is only safe on %s.", bind or "127.0.0.1"
        )
        return AuthPolicy(
            enabled=False,
            username="",
            password_hash="",
            display_name="",
            lifetime=timedelta(minutes=_int_setting(settings, "web_session_lifetime_minutes", 720)),
            cookie_secure=False,
            max_attempts=_int_setting(settings, "web_login_max_attempts", 5),
            cooldown_seconds=_int_setting(settings, "web_login_cooldown_seconds", 60),
            cooldown_max_seconds=_int_setting(settings, "web_login_cooldown_max_seconds", 900),
        )

    missing = []
    if not str(getattr(settings, "web_operator_username", "") or "").strip():
        missing.append("WEB_OPERATOR_USERNAME")
    if not str(getattr(settings, "web_operator_password_hash", "") or "").strip():
        missing.append("WEB_OPERATOR_PASSWORD_HASH")
    secret = str(getattr(settings, "web_session_secret", "") or "")
    if not secret.strip():
        missing.append("WEB_SESSION_SECRET")
    if missing:
        # Names only. The whole point of this branch is a configuration that is
        # wrong, and echoing a partially-set credential back would put it in the
        # journal of every failed restart.
        raise AuthConfigError(
            "Dashboard authentication is enabled but not configured. Missing: "
            + ", ".join(missing)
            + ". Create the password hash with `btcedu generate-password-hash`."
        )
    if len(secret.strip()) < _MIN_SECRET_LENGTH:
        raise AuthConfigError(
            f"WEB_SESSION_SECRET must be at least {_MIN_SECRET_LENGTH} characters. "
            "Generate one with `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )

    lifetime = max(1, _int_setting(settings, "web_session_lifetime_minutes", 720))
    return AuthPolicy(
        enabled=True,
        username=str(settings.web_operator_username).strip(),
        password_hash=str(settings.web_operator_password_hash).strip(),
        display_name=str(getattr(settings, "web_operator_display_name", "") or "").strip(),
        lifetime=timedelta(minutes=lifetime),
        cookie_secure=bool(getattr(settings, "web_cookie_secure", True)),
        max_attempts=max(1, _int_setting(settings, "web_login_max_attempts", 5)),
        cooldown_seconds=max(1, _int_setting(settings, "web_login_cooldown_seconds", 60)),
        cooldown_max_seconds=max(1, _int_setting(settings, "web_login_cooldown_max_seconds", 900)),
    )


class LoginThrottle:
    """A cooldown after repeated failures, held in memory.

    Keyed by username rather than by client address, for one blunt reason: the
    address is a header the reverse proxy writes and an attacker can suggest.
    Trusting ``X-Forwarded-For`` for a security decision means the throttle can
    be bypassed by varying one header, which is worse than no throttle because
    it looks like one.

    The trade-off is stated plainly: on a single-operator deployment, throttling
    by username means an attacker who knows the username can lock the operator
    out for the cooldown. That is why the cooldown starts at a minute rather
    than an hour, and why the correct response to a lockout is a CLI, which this
    never touches.

    Memory rather than the database, because a single gunicorn process serves
    this dashboard and a lockout that survives a restart would be one more thing
    to clear by hand at exactly the wrong moment. The limit of the approach is
    therefore honest: restarting the service clears the counters.
    """

    def __init__(self, max_attempts: int, cooldown: int, cooldown_max: int):
        self._max = max_attempts
        self._cooldown = cooldown
        self._cooldown_max = cooldown_max
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def _key(self, username: str) -> str:
        return str(username or "").strip().lower()[:64]

    def retry_after(self, username: str) -> int:
        """Seconds the caller must wait, 0 when it may try now."""
        until = self._blocked_until.get(self._key(username))
        if until is None:
            return 0
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._blocked_until.pop(self._key(username), None)
            return 0
        return int(remaining) + 1

    def record_failure(self, username: str) -> None:
        key = self._key(username)
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self._max:
            over = count - self._max
            wait = min(self._cooldown * (2**over), self._cooldown_max)
            self._blocked_until[key] = time.monotonic() + wait

    def record_success(self, username: str) -> None:
        key = self._key(username)
        self._failures.pop(key, None)
        self._blocked_until.pop(key, None)


def _wants_json() -> bool:
    """Is this a programmatic caller rather than a browser navigation?

    An API caller must get a 401 it can act on, not a 302 to an HTML page it
    will parse as data. The path check comes first because the dashboard's own
    fetches do not always set an Accept header.
    """
    if request.path.startswith("/api/"):
        return True
    accept = request.accept_mimetypes
    return accept.accept_json and not accept.accept_html


def safe_next(target: str, fallback: str = "/") -> str:
    """Only ever redirect inside this application.

    ``?next=`` is the standard way to end up as somebody else's phishing hop.
    Anything with a scheme, a host, a backslash or a leading double slash is
    discarded without comment.
    """
    raw = str(target or "").strip()
    if not raw or "\\" in raw or raw.startswith("//"):
        return fallback
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not raw.startswith("/"):
        return fallback
    return raw


def operator_ref() -> str:
    """The identity for an audit row, taken from the session and nowhere else.

    A request body no longer has a say. Before this, ``operator_ref`` was read
    out of the POST payload, which meant the audit trail recorded whatever the
    caller typed — including a plausible-looking name that was never there.
    """
    policy: AuthPolicy | None = current_app.config.get("auth_policy")
    if policy is not None and not policy.enabled:
        # Development without authentication. Say so in the ledger rather than
        # forging a name, so a row written on a laptop is never mistaken for a
        # decision an operator made.
        return web_operator("unauthenticated-dev")
    if current_user.is_authenticated:
        return current_user.operator_ref
    return web_operator("unknown")


def operator_display_name() -> str:
    """The cosmetic name, kept apart from the identity used for the audit."""
    if current_user.is_authenticated:
        return getattr(current_user, "display_name", "") or current_user.username
    return ""


def init_auth(app, settings) -> AuthPolicy:
    """Wire authentication, CSRF and the login routes into the app."""
    policy = resolve_policy(settings)
    app.config["auth_policy"] = policy
    app.config["login_throttle"] = LoginThrottle(
        policy.max_attempts, policy.cooldown_seconds, policy.cooldown_max_seconds
    )

    # A secret is needed for the session cookie in both modes: even without
    # authentication the CSRF token is stored in the session. In the unprotected
    # development mode an ephemeral one is right — it invalidates on restart,
    # which is exactly what an unauthenticated session deserves.
    if policy.enabled:
        app.secret_key = str(settings.web_session_secret).strip()
    else:
        import secrets as _secrets

        app.secret_key = _secrets.token_urlsafe(48)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=policy.cookie_secure,
        PERMANENT_SESSION_LIFETIME=policy.lifetime,
        WTF_CSRF_TIME_LIMIT=None,  # the session lifetime already bounds it
        # CSRF defends an authenticated session against a request the operator
        # did not mean to send. Without authentication there is no session to
        # ride and no privilege to borrow, so the two switch together: it is
        # one decision ("this is unprotected loopback development"), not two
        # that could drift apart and leave production half covered.
        WTF_CSRF_ENABLED=policy.enabled,
    )

    login_manager = LoginManager()
    login_manager.session_protection = "strong"
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_operator(user_id: str):
        if not policy.enabled:
            return None
        if str(user_id) != policy.username:
            return None
        return Operator(policy.username, policy.display_name)

    @login_manager.unauthorized_handler
    def _unauthorized():
        if _wants_json():
            return jsonify(
                {"error": "Authentication required", "code": "unauthenticated"}
            ), 401
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    @app.errorhandler(CSRFError)
    def _csrf_failed(exc):
        # No token value, no session id, no hint about what a valid one looks
        # like. The operator needs to know to reload; an attacker needs nothing.
        #
        # Always 403, never a redirect: a redirect on a rejected POST looks like
        # a success to anything that follows it, and the login form's own
        # rejection would silently become "please log in again" rather than
        # "this request was refused".
        logger.warning("CSRF rejection on %s %s", request.method, request.path)
        message = "Die Sitzung ist abgelaufen oder das Sicherheits-Token fehlt. Bitte neu laden."
        if _wants_json():
            return jsonify({"error": message, "code": "csrf_failed"}), 403
        return render_template("login.html", next="", error=message), 403

    @app.before_request
    def _require_login():
        endpoint = request.endpoint
        if endpoint is None:
            return None  # a 404; the error handler answers it
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if not policy.enabled:
            return None
        if current_user.is_authenticated:
            session.permanent = True
            return None
        return login_manager.unauthorized()

    @app.context_processor
    def _inject_identity():
        return {
            "csrf_token_value": generate_csrf(),
            "operator_display_name": operator_display_name(),
            "auth_enabled": policy.enabled,
        }

    # CSRF is initialised last on purpose. Both checks install a
    # `before_request` hook and Flask runs them in registration order, so this
    # ordering is what makes an anonymous POST a 401 ("log in") rather than a
    # 403 ("your token is wrong") — the second answer is both less useful and a
    # small disclosure about how the door is built.
    csrf.init_app(app)

    from btcedu.web.auth_routes import auth_bp

    app.register_blueprint(auth_bp)
    return policy


def authenticate(policy: AuthPolicy, username: str, password: str) -> Operator | None:
    """Check a submitted credential.

    Both halves are always evaluated. Returning early on an unknown username
    would answer "does this account exist" with response time alone.
    """
    given = str(username or "").strip()
    candidate = str(password or "")
    reference = policy.password_hash
    ok_password = check_password_hash(reference, candidate)
    ok_username = given == policy.username
    if ok_username and ok_password:
        return Operator(policy.username, policy.display_name)
    return None


def start_session(operator: Operator) -> None:
    """Log in, discarding anything the pre-login session held.

    Session fixation: without the clear, a value planted in the anonymous
    session — including a CSRF token an attacker chose — would survive into the
    authenticated one.
    """
    session.clear()
    login_user(operator, remember=False)
    session.permanent = True
    g.operator = operator


def end_session() -> None:
    logout_user()
    session.clear()
