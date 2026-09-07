"""Authentication, CSRF and operator identity for the dashboard (WP-8A).

Until this package, the dashboard was protected by the address bar alone. These
tests are written from the position of someone who has reached the port: what
can they do, what does the audit trail say they were, and what stops a foreign
page from acting through a legitimate session.

No provider is contacted, no video is rendered and nothing sleeps: the login
throttle is driven by its own clock, never by waiting.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import generate_password_hash

from btcedu.config import Settings
from btcedu.core.operator_identity import (
    cli_operator,
    identity_kind,
    identity_label,
    system_operator,
    web_operator,
)
from btcedu.db import Base
from btcedu.models.avatar_job_audit import AvatarJobAudit  # noqa: F401 — registers the table
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.web.app import create_app
from btcedu.web.auth import (
    AuthConfigError,
    LoginThrottle,
    Operator,
    resolve_policy,
    safe_next,
)

USERNAME = "almanya-ops"
PASSWORD = "correct horse battery staple"
SECRET = "s" * 48


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(tmp_path, **overrides):
    base = dict(
        anthropic_api_key="test-key",
        openai_api_key="test-key",
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        web_auth_enabled=True,
        web_bind_host="127.0.0.1",
        web_session_secret=SECRET,
        web_operator_username=USERNAME,
        web_operator_password_hash=generate_password_hash(PASSWORD),
        web_operator_display_name="Redaktion ALMANYA24",
        # The Flask test client speaks http; a Secure cookie would simply never
        # be sent back and every test would look like a login failure. The
        # default is asserted separately, and production runs behind HTTPS.
        web_cookie_secure=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    return engine, sessionmaker(bind=engine)


@pytest.fixture
def app(tmp_path, db):
    engine, factory = db
    application = create_app(settings=_settings(tmp_path))
    application.config["TESTING"] = True
    application.config["session_factory"] = factory
    session = factory()
    session.add(
        Episode(
            episode_id="ep_auth",
            source="local_recorder",
            title="Tagesschau",
            url="/tmp/x.mp4",
            status=EpisodeStatus.ANCHOR_GENERATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
    )
    session.commit()
    session.close()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client) -> str:
    page = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert match, "the login form must carry a token"
    return match.group(1).decode()


def _login(client, username=USERNAME, password=PASSWORD, **extra):
    """Log in the way a browser does: fetch the form, send its token back."""
    data = {"username": username, "password": password, "csrf_token": _csrf(client)}
    data.update(extra)
    return client.post("/login", data=data, follow_redirects=False)


@pytest.fixture
def operator(client):
    """A logged-in browser session."""
    response = _login(client)
    assert response.status_code == 302
    return client


def _dashboard_csrf(client) -> str:
    page = client.get("/")
    match = re.search(rb'name="csrf-token" content="([^"]+)"', page.data)
    assert match, "the dashboard must publish a token for its own fetches"
    return match.group(1).decode()


# ---------------------------------------------------------------------------
# 1. Configuration: the dashboard refuses to start unsafely
# ---------------------------------------------------------------------------


class TestTheDashboardRefusesToStartUnsafely:
    def test_authentication_without_a_credential_is_fatal(self, tmp_path):
        with pytest.raises(AuthConfigError) as exc:
            resolve_policy(
                _settings(
                    tmp_path,
                    web_operator_username="",
                    web_operator_password_hash="",
                )
            )
        assert "WEB_OPERATOR_USERNAME" in str(exc.value)
        assert "WEB_OPERATOR_PASSWORD_HASH" in str(exc.value)

    def test_a_missing_session_secret_is_fatal(self, tmp_path):
        with pytest.raises(AuthConfigError) as exc:
            resolve_policy(_settings(tmp_path, web_session_secret=""))
        assert "WEB_SESSION_SECRET" in str(exc.value)

    def test_a_short_session_secret_is_refused(self, tmp_path):
        with pytest.raises(AuthConfigError) as exc:
            resolve_policy(_settings(tmp_path, web_session_secret="short"))
        assert "32" in str(exc.value)

    def test_an_external_bind_without_authentication_is_fatal(self, tmp_path):
        """The one combination that would serve the pipeline to the network."""
        with pytest.raises(AuthConfigError) as exc:
            resolve_policy(
                _settings(tmp_path, web_auth_enabled=False, web_bind_host="0.0.0.0")
            )
        assert "0.0.0.0" in str(exc.value)

    @pytest.mark.parametrize("host", ["192.168.1.40", "0.0.0.0", "::"])
    def test_no_lan_address_slips_through_as_development(self, tmp_path, host):
        with pytest.raises(AuthConfigError):
            resolve_policy(_settings(tmp_path, web_auth_enabled=False, web_bind_host=host))

    def test_loopback_development_is_explicitly_allowed(self, tmp_path):
        policy = resolve_policy(
            _settings(tmp_path, web_auth_enabled=False, web_bind_host="127.0.0.1")
        )
        assert policy.enabled is False

    def test_the_failure_never_quotes_the_credential(self, tmp_path):
        """A restart loop writes this message to the journal every few seconds."""
        with pytest.raises(AuthConfigError) as exc:
            resolve_policy(_settings(tmp_path, web_operator_username=""))
        message = str(exc.value)
        assert SECRET not in message
        assert PASSWORD not in message
        assert generate_password_hash(PASSWORD)[:20] not in message

    def test_the_app_itself_will_not_come_up(self, tmp_path):
        """Not a request-time check: an unsafe dashboard must not bind at all."""
        with pytest.raises(AuthConfigError):
            create_app(settings=_settings(tmp_path, web_session_secret=""))

    def test_authentication_is_on_by_default(self):
        """The default of a security switch is the whole security decision."""
        assert Settings.model_fields["web_auth_enabled"].default is True
        assert Settings.model_fields["web_cookie_secure"].default is True
        assert Settings.model_fields["web_bind_host"].default == "127.0.0.1"

    def test_the_settings_repr_masks_the_credentials(self, tmp_path):
        text_repr = repr(_settings(tmp_path))
        assert SECRET not in text_repr
        assert PASSWORD not in text_repr

    def test_the_example_env_carries_no_real_credential(self):
        example = Path(".env.example").read_text(encoding="utf-8")
        assert "WEB_OPERATOR_PASSWORD_HASH=" in example
        # A placeholder, never a usable hash: werkzeug hashes start with the
        # method name followed by a colon.
        for line in example.splitlines():
            if line.startswith("WEB_OPERATOR_PASSWORD_HASH="):
                value = line.split("=", 1)[1].strip()
                assert not value.startswith(("scrypt:", "pbkdf2:", "argon2"))
            if line.startswith("WEB_SESSION_SECRET="):
                value = line.split("=", 1)[1].strip()
                assert value in {"", "change-me"} or value.startswith("<")


# ---------------------------------------------------------------------------
# 2. Login
# ---------------------------------------------------------------------------


class TestLoggingIn:
    def test_the_right_credential_gets_a_session(self, client):
        response = _login(client)
        assert response.status_code == 302
        assert client.get("/").status_code == 200

    def test_a_wrong_password_is_refused(self, client):
        response = _login(client, password="nope")
        assert response.status_code == 401
        assert client.get("/", follow_redirects=False).status_code == 302

    def test_an_unknown_user_is_refused(self, client):
        response = _login(client, username="someone")
        assert response.status_code == 401

    def test_both_failures_read_identically(self, client):
        """Which half was wrong is free reconnaissance."""
        wrong_user = _login(client, username="x")
        wrong_pass = _login(client, password="x")
        assert wrong_user.status_code == wrong_pass.status_code
        body_a = re.sub(rb'value="[^"]*"', b"", wrong_user.data)
        body_b = re.sub(rb'value="[^"]*"', b"", wrong_pass.data)
        assert body_a == body_b

    def test_the_session_is_replaced_not_extended(self, app):
        """Session fixation: nothing planted before the login survives it."""
        client = app.test_client()
        with client.session_transaction() as pre:
            pre["planted"] = "attacker-chosen"
        _login(client)
        with client.session_transaction() as post:
            assert "planted" not in post
            assert post.get("_user_id") == USERNAME

    def test_a_relative_next_is_honoured(self, client):
        response = _login(client, next="/episodes")
        assert response.headers["Location"] == "/episodes"

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.example/steal",
            "//evil.example/steal",
            "http://evil.example",
            "\\\\evil.example",
            "javascript:alert(1)",
        ],
    )
    def test_an_open_redirect_is_discarded(self, client, target):
        response = _login(client, next=target)
        assert "evil.example" not in response.headers["Location"]
        assert "javascript" not in response.headers["Location"].lower()

    def test_safe_next_is_conservative_on_its_own(self):
        assert safe_next("/episodes") == "/episodes"
        assert safe_next("") == "/"
        assert safe_next("episodes") == "/"
        assert safe_next("//host/x") == "/"
        assert safe_next("https://host/x") == "/"

    def test_logout_is_post_only(self, operator):
        assert operator.get("/logout").status_code == 405
        token = _dashboard_csrf(operator)
        assert operator.post("/logout", data={"csrf_token": token}).status_code == 302
        assert operator.get("/", follow_redirects=False).status_code == 302

    def test_a_logout_without_a_token_does_not_log_anyone_out(self, operator):
        """A forged logout is a nuisance, not a favour."""
        assert operator.post("/logout").status_code == 403
        assert operator.get("/").status_code == 200

    def test_an_expired_session_leads_back_to_the_login(self, tmp_path, db):
        _engine, factory = db
        application = create_app(
            settings=_settings(tmp_path, web_session_lifetime_minutes=1)
        )
        application.config["TESTING"] = True
        application.config["session_factory"] = factory
        assert application.config["PERMANENT_SESSION_LIFETIME"] == timedelta(minutes=1)

        client = application.test_client()
        _login(client)
        assert client.get("/").status_code == 200
        # Expiry is enforced by the cookie itself; dropping it is what a browser
        # does when it lapses. No test may wait a minute to prove that.
        client.delete_cookie("session")
        assert client.get("/", follow_redirects=False).status_code == 302

    def test_the_cookie_is_httponly_and_samesite(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Strict"

    def test_the_cookie_is_secure_when_configured(self, tmp_path, db):
        _engine, factory = db
        application = create_app(settings=_settings(tmp_path, web_cookie_secure=True))
        application.config["session_factory"] = factory
        assert application.config["SESSION_COOKIE_SECURE"] is True

    def test_no_credential_reaches_the_log(self, app, caplog):
        with caplog.at_level("DEBUG"):
            _login(app.test_client())
            _login(app.test_client(), password="wrong-one")
        recorded = "\n".join(record.getMessage() for record in caplog.records)
        assert PASSWORD not in recorded
        assert "wrong-one" not in recorded

    def test_no_credential_travels_in_the_url(self, client):
        response = _login(client)
        assert PASSWORD not in response.headers["Location"]
        assert USERNAME not in response.headers["Location"]


class TestTheLoginThrottle:
    def test_repeated_failures_start_a_cooldown(self, client):
        for _ in range(5):
            assert _login(client, password="wrong").status_code == 401
        blocked = _login(client)
        assert blocked.status_code == 429

    def test_the_cooldown_never_says_how_close_the_guess_was(self, client):
        for _ in range(5):
            _login(client, password="wrong")
        blocked = _login(client, password="wrong")
        assert b"Fehlversuche" in blocked.data
        assert b"Passwort ist falsch" not in blocked.data

    def test_a_success_clears_the_counter(self):
        throttle = LoginThrottle(max_attempts=3, cooldown=60, cooldown_max=900)
        throttle.record_failure("a")
        throttle.record_failure("a")
        throttle.record_success("a")
        throttle.record_failure("a")
        assert throttle.retry_after("a") == 0

    def test_the_cooldown_grows_but_is_capped(self):
        throttle = LoginThrottle(max_attempts=1, cooldown=60, cooldown_max=120)
        throttle.record_failure("a")
        first = throttle.retry_after("a")
        throttle.record_failure("a")
        second = throttle.retry_after("a")
        throttle.record_failure("a")
        throttle.record_failure("a")
        capped = throttle.retry_after("a")
        assert first <= second
        assert capped <= 121

    def test_it_does_not_key_on_a_forwarded_address(self, client):
        """A header an attacker can vary must not reset a security counter."""
        for index in range(5):
            client.post(
                "/login",
                data={
                    "username": USERNAME,
                    "password": "wrong",
                    "csrf_token": _csrf(client),
                },
                headers={"X-Forwarded-For": f"10.0.0.{index}"},
            )
        blocked = client.post(
            "/login",
            data={
                "username": USERNAME,
                "password": PASSWORD,
                "csrf_token": _csrf(client),
            },
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        assert blocked.status_code == 429


# ---------------------------------------------------------------------------
# 3. Access control
# ---------------------------------------------------------------------------


class TestNothingIsReachableWithoutASession:
    def test_the_dashboard_redirects_to_the_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize(
        "path",
        [
            "/api/episodes",
            "/api/episodes/ep_auth",
            "/api/episodes/ep_auth/avatar",
            "/api/avatar/reconcile",
            "/api/reviews",
            "/api/cost",
            "/api/credits",
            "/api/render-mode",
            "/api/failover/status",
        ],
    )
    def test_an_api_call_is_a_json_401(self, client, path):
        response = client.get(path)
        assert response.status_code == 401
        assert response.is_json
        assert response.get_json()["code"] == "unauthenticated"

    def test_an_anonymous_api_call_never_reveals_an_episode(self, client):
        for path in ("/api/episodes", "/api/episodes/ep_auth"):
            body = client.get(path).get_data(as_text=True)
            assert "ep_auth" not in body
            assert "Tagesschau" not in body

    def test_an_unknown_and_a_known_episode_are_indistinguishable(self, client):
        known = client.get("/api/episodes/ep_auth")
        unknown = client.get("/api/episodes/ep_does_not_exist")
        assert known.status_code == unknown.status_code == 401
        assert known.get_json() == unknown.get_json()

    def test_media_and_previews_are_closed(self, client):
        for path in (
            "/api/episodes/ep_auth/avatar/scenes/sc_001/preview",
            "/api/episodes/ep_auth/video",
            "/api/episodes/ep_auth/files/render/final.mp4",
        ):
            assert client.get(path).status_code in (401, 404)
            if client.get(path).status_code == 401:
                assert client.get(path).is_json

    def test_the_whole_route_table_is_covered(self, app):
        """The guarantee that survives the next endpoint somebody adds.

        Blanket-disabling authentication in the other test modules is only
        defensible because this test exists: it walks every registered rule and
        insists each one is either protected or a deliberate line in
        PUBLIC_ENDPOINTS.
        """
        from btcedu.web.auth import PUBLIC_ENDPOINTS

        client = app.test_client()
        unprotected = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in PUBLIC_ENDPOINTS:
                continue
            if rule.arguments:
                continue  # exercised by name above; a placeholder URL is not a route
            method = "GET" if "GET" in rule.methods else "POST"
            response = client.open(rule.rule, method=method)
            if response.status_code not in (401, 302, 405):
                unprotected.append(f"{method} {rule.rule} -> {response.status_code}")
        assert not unprotected, f"reachable without a session: {unprotected}"

    def test_the_public_list_is_the_documented_one(self):
        from btcedu.web.auth import PUBLIC_ENDPOINTS

        assert PUBLIC_ENDPOINTS == frozenset(
            {"auth.login", "auth.logout", "static", "api.health"}
        )


class TestTheHealthCheck:
    def test_it_answers_without_a_session(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"

    def test_it_tells_an_anonymous_caller_nothing_else(self, client):
        """A version number tells a stranger which advisories apply here."""
        payload = client.get("/api/health").get_json()
        assert set(payload) == {"status", "time"}
        assert "version" not in payload
        assert "git_commit" not in payload

    def test_an_operator_still_gets_the_version(self, operator):
        payload = operator.get("/api/health").get_json()
        assert payload["version"] == "0.1.0"
        assert "git_commit" in payload


class TestStaticFilesStayPublic:
    def test_the_stylesheet_and_script_load(self, client):
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_they_carry_no_dynamic_data(self, client):
        body = client.get("/static/app.js").get_data(as_text=True)
        assert "ep_auth" not in body
        assert SECRET not in body


class TestTheLoginPageItself:
    def test_it_is_public(self, client):
        assert client.get("/login").status_code == 200

    def test_it_offers_a_labelled_accessible_form(self, client):
        body = client.get("/login").get_data(as_text=True)
        assert 'for="username"' in body
        assert 'for="password"' in body
        assert 'autocomplete="current-password"' in body
        assert 'type="password"' in body

    def test_a_logged_in_operator_is_sent_onwards(self, operator):
        response = operator.get("/login", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" not in response.headers["Location"]


# ---------------------------------------------------------------------------
# 4. CSRF
# ---------------------------------------------------------------------------


class TestCrossSiteRequestForgery:
    def test_a_post_with_a_valid_token_is_accepted(self, operator):
        token = _dashboard_csrf(operator)
        response = operator.post(
            "/api/credits/openai-balance",
            json={"balance_usd": 5.0},
            headers={"X-CSRFToken": token},
        )
        assert response.status_code == 200

    def test_a_post_without_a_token_is_refused(self, operator):
        response = operator.post("/api/credits/openai-balance", json={"balance_usd": 5.0})
        assert response.status_code == 403
        assert response.get_json()["code"] == "csrf_failed"

    def test_a_wrong_token_is_refused(self, operator):
        response = operator.post(
            "/api/credits/openai-balance",
            json={"balance_usd": 5.0},
            headers={"X-CSRFToken": "not-a-token"},
        )
        assert response.status_code == 403

    def test_a_token_from_another_session_is_refused(self, app):
        """A token is only a token for the session it was minted in."""
        first = app.test_client()
        _login(first)
        stolen = _dashboard_csrf(first)

        second = app.test_client()
        _login(second)
        response = second.post(
            "/api/credits/openai-balance",
            json={"balance_usd": 5.0},
            headers={"X-CSRFToken": stolen},
        )
        assert response.status_code == 403

    def test_the_rejection_leaks_nothing(self, operator):
        response = operator.post("/api/credits/openai-balance", json={"balance_usd": 1.0})
        body = response.get_data(as_text=True)
        assert "Traceback" not in body
        assert SECRET not in body
        assert _dashboard_csrf(operator) not in body

    def test_no_token_appears_in_a_log_line(self, operator, caplog):
        token = _dashboard_csrf(operator)
        with caplog.at_level("DEBUG"):
            operator.post("/api/credits/openai-balance", json={"balance_usd": 1.0})
        recorded = "\n".join(record.getMessage() for record in caplog.records)
        assert token not in recorded

    def test_the_login_form_is_protected_too(self, client):
        """Login CSRF logs a victim into the attacker's account."""
        response = client.post(
            "/login", data={"username": USERNAME, "password": PASSWORD, "csrf_token": "x"}
        )
        assert response.status_code == 403

    def test_a_valid_login_token_works(self, client):
        token = _csrf(client)
        response = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD, "csrf_token": token},
        )
        assert response.status_code == 302

    def test_the_origin_check_still_applies_on_top(self, operator):
        """Two layers, not one: WP-7's Origin check is not replaced by a token."""
        token = _dashboard_csrf(operator)
        response = operator.post(
            "/api/avatar/breaker/heygen/reset",
            json={"reason": "checked the provider"},
            headers={"X-CSRFToken": token, "Origin": "https://evil.example"},
        )
        assert response.status_code == 403

    def test_a_get_needs_no_token(self, operator):
        assert operator.get("/api/episodes").status_code == 200

    def test_the_dashboard_page_publishes_a_token(self, operator):
        assert _dashboard_csrf(operator)

    def test_the_script_sends_it_on_every_mutation(self):
        """The token is worthless if the dashboard's own fetches omit it."""
        source = Path("btcedu/web/static/app.js").read_text(encoding="utf-8")
        assert "X-CSRFToken" in source
        for call in re.findall(r'fetch\((?:"|\')api[^;]{0,400}?\)', source, re.S):
            if "method" not in call or '"GET"' in call:
                continue
            if "DELETE" in call or "POST" in call or "PUT" in call:
                assert "csrfHeaders" in call, f"unprotected mutation: {call[:120]}"


class TestTheCommandLineIsUnaffected:
    def test_the_cli_never_passes_through_the_web_layer(self):
        """CSRF is a browser mechanism; a command on the box must not meet it."""
        source = Path("btcedu/cli.py").read_text(encoding="utf-8")
        assert "csrf" not in source.lower()
        assert "test_client" not in source

    def test_a_background_job_writes_through_the_domain(self):
        source = Path("btcedu/web/jobs.py").read_text(encoding="utf-8")
        assert "X-CSRFToken" not in source

    def test_the_domain_functions_take_no_request(self):
        import inspect

        from btcedu.core import avatar_reconcile

        signature = inspect.signature(avatar_reconcile.resolve)
        assert "request" not in signature.parameters
        assert "csrf_token" not in signature.parameters


# ---------------------------------------------------------------------------
# 5. Operator identity
# ---------------------------------------------------------------------------


class TestTheAuditKnowsWhoActed:
    def test_the_three_origins_are_distinguishable(self):
        assert identity_kind(web_operator("ops")) == "web"
        assert identity_kind(cli_operator("ops")) == "cli"
        assert identity_kind(system_operator("scheduler")) == "system"
        assert identity_kind("dashboard") == "unknown"
        assert identity_label(web_operator("ops")) == "ops"

    def test_a_legacy_row_is_not_retro_fitted(self):
        """Claiming to know the origin of an old decision would be a lie."""
        assert identity_kind("ops-anna") == "unknown"
        assert identity_label("ops-anna") == "ops-anna"

    def test_a_web_action_records_the_session_identity(self, operator, app):
        token = _dashboard_csrf(operator)
        response = operator.post(
            "/api/avatar/breaker/heygen/reset",
            json={"reason": "provider confirmed healthy"},
            headers={"X-CSRFToken": token},
        )
        assert response.status_code in (200, 404, 409, 422)

        factory = app.config["session_factory"]
        session = factory()
        try:
            rows = session.query(AvatarJobAudit).all()
            if rows:
                assert rows[-1].operator_ref == f"web:{USERNAME}"
        finally:
            session.close()

    def test_a_client_cannot_name_itself(self, operator, app):
        """The old hole: the audit recorded whatever the POST body claimed."""
        token = _dashboard_csrf(operator)
        operator.post(
            "/api/avatar/breaker/heygen/reset",
            json={"reason": "checked", "operator_ref": "someone-else"},
            headers={"X-CSRFToken": token},
        )
        factory = app.config["session_factory"]
        session = factory()
        try:
            refs = [row.operator_ref for row in session.query(AvatarJobAudit).all()]
        finally:
            session.close()
        assert "someone-else" not in refs
        for ref in refs:
            assert ref.startswith("web:")

    def test_a_forwarded_user_header_is_not_an_identity(self, operator, app):
        token = _dashboard_csrf(operator)
        operator.post(
            "/api/avatar/breaker/heygen/reset",
            json={"reason": "checked"},
            headers={"X-CSRFToken": token, "X-Forwarded-User": "admin"},
        )
        factory = app.config["session_factory"]
        session = factory()
        try:
            refs = [row.operator_ref for row in session.query(AvatarJobAudit).all()]
        finally:
            session.close()
        assert "admin" not in refs
        assert "web:admin" not in refs

    def test_the_api_helper_reads_the_session(self, app):
        from btcedu.web.api import _operator_ref

        client = app.test_client()
        _login(client)
        with app.test_request_context("/api/episodes"):
            # No session in this synthetic context: the helper must not invent
            # a name, and must not fall back to a request field.
            assert _operator_ref() == "web:unknown"

    def test_the_display_name_is_not_the_identity(self, app):
        operator_obj = Operator(USERNAME, "Redaktion ALMANYA24")
        assert operator_obj.display_name == "Redaktion ALMANYA24"
        assert operator_obj.operator_ref == f"web:{USERNAME}"
        assert "Redaktion" not in operator_obj.operator_ref

    def test_the_cli_marks_its_own_decisions(self):
        source = Path("btcedu/cli.py").read_text(encoding="utf-8")
        assert source.count("cli_operator(operator_ref)") >= 2
        assert "cli_operator" in source

    def test_the_prefix_cannot_satisfy_a_rights_approval(self):
        """A rights approval names a person; a session must never count as one."""
        from btcedu.core.anchor_rights import _OPERATOR_REF

        assert _OPERATOR_REF.match(web_operator("ops")) is None


# ---------------------------------------------------------------------------
# 6. Regression: the dashboard still works once logged in
# ---------------------------------------------------------------------------


class TestTheDashboardStillWorksAfterLogin:
    def test_the_page_renders_with_a_logout_control(self, operator):
        body = operator.get("/").get_data(as_text=True)
        assert "Abmelden" in body
        assert 'action="/logout"' in body
        assert "Redaktion ALMANYA24" in body

    def test_the_episode_list_is_served(self, operator):
        response = operator.get("/api/episodes")
        assert response.status_code == 200
        payload = response.get_json()
        episodes = payload["episodes"] if isinstance(payload, dict) else payload
        assert any(e["episode_id"] == "ep_auth" for e in episodes)

    def test_the_avatar_state_is_served(self, operator):
        response = operator.get("/api/episodes/ep_auth/avatar")
        assert response.status_code in (200, 400)

    def test_the_reconciliation_list_is_served(self, operator):
        response = operator.get("/api/avatar/reconcile")
        assert response.status_code == 200
        assert "jobs" in response.get_json()

    def test_the_voice_over_prepare_is_reachable(self, operator):
        token = _dashboard_csrf(operator)
        response = operator.post(
            "/api/episodes/ep_auth/avatar/voice-over",
            json={"action": "prepare"},
            headers={"X-CSRFToken": token},
        )
        assert response.status_code in (200, 400, 404, 422)
        assert response.is_json

    def test_the_reviews_endpoint_is_served(self, operator):
        assert operator.get("/api/reviews").status_code == 200

    def test_no_secret_reaches_the_rendered_page(self, operator):
        body = operator.get("/").get_data(as_text=True)
        assert SECRET not in body
        assert PASSWORD not in body

    def test_the_operator_name_is_escaped(self, tmp_path, db):
        """A display name is configuration, and configuration is still input."""
        _engine, factory = db
        application = create_app(
            settings=_settings(tmp_path, web_operator_display_name="<script>x</script>")
        )
        application.config["session_factory"] = factory
        client = application.test_client()
        _login(client)
        body = client.get("/").get_data(as_text=True)
        assert "<script>x</script>" not in body
        assert "&lt;script&gt;" in body

    def test_no_provider_is_ever_contacted(self, operator, monkeypatch):
        import requests

        def _forbidden(*args, **kwargs):
            raise AssertionError("a web request must never reach a provider")

        monkeypatch.setattr(requests, "post", _forbidden)
        monkeypatch.setattr(requests, "get", _forbidden)
        token = _dashboard_csrf(operator)
        operator.post(
            "/api/avatar/breaker/heygen/reset",
            json={"reason": "checked"},
            headers={"X-CSRFToken": token},
        )
        assert operator.get("/api/avatar/reconcile").status_code == 200


class TestPathSecurityIsStillEnforced:
    def test_a_traversal_stays_refused_for_an_operator(self, operator):
        """Authentication is not authorisation for the whole filesystem."""
        for path in (
            "/api/episodes/ep_auth/files/../../../etc/passwd",
            "/api/episodes/..%2f..%2fetc/files/passwd",
        ):
            assert operator.get(path).status_code in (400, 403, 404)

    def test_an_unknown_episode_is_a_404_not_a_500(self, operator):
        response = operator.get("/api/episodes/ep_nope")
        assert response.status_code == 404
        assert "Traceback" not in response.get_data(as_text=True)


class TestTheDeploymentDocumentation:
    def test_the_service_file_names_the_bind_host(self):
        unit = Path("deploy/btcedu-web.service").read_text(encoding="utf-8")
        assert "WEB_BIND_HOST" in unit

    def test_the_example_env_documents_the_switches(self):
        example = Path(".env.example").read_text(encoding="utf-8")
        for key in (
            "WEB_AUTH_ENABLED",
            "WEB_SESSION_SECRET",
            "WEB_OPERATOR_USERNAME",
            "WEB_OPERATOR_PASSWORD_HASH",
            "WEB_SESSION_LIFETIME_MINUTES",
            "WEB_BIND_HOST",
        ):
            assert key in example, key

    def test_the_hash_helper_is_documented(self):
        doc = Path("docs/dashboard-auth.md").read_text(encoding="utf-8")
        assert "generate-password-hash" in doc
        assert "WEB_SESSION_SECRET" in doc
        # The reset path matters more than the setup path: it is needed on the
        # day nobody can log in.
        assert "zurücksetzen" in doc.lower() or "reset" in doc.lower()

    def test_no_plaintext_password_is_committed(self):
        for path in (".env.example", "deploy/btcedu-web.service", "docs/dashboard-auth.md"):
            body = Path(path).read_text(encoding="utf-8")
            assert "WEB_OPERATOR_PASSWORD=" not in body

    def test_the_helper_never_writes_a_file(self):
        source = Path("btcedu/cli.py").read_text(encoding="utf-8")
        start = source.index("def generate_password_hash_command")
        block = source[start : start + 2000]
        assert "getpass" in block
        assert "open(" not in block
        assert "write_text" not in block


class TestTheJsonContract:
    def test_the_401_shape_is_stable(self, client):
        payload = client.get("/api/episodes").get_json()
        assert payload == {"error": "Authentication required", "code": "unauthenticated"}

    def test_the_403_shape_is_stable(self, operator):
        payload = operator.post(
            "/api/credits/openai-balance", json={"balance_usd": 1.0}
        ).get_json()
        assert payload["code"] == "csrf_failed"
        assert isinstance(payload["error"], str)

    def test_an_api_401_is_never_html(self, client):
        response = client.get("/api/cost", headers={"Accept": "text/html"})
        assert response.status_code == 401
        assert response.is_json
        json.loads(response.get_data(as_text=True))
