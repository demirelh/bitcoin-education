"""Serving the dashboard under a stripped reverse-proxy sub-path.

Caddy mounts this app with ``handle_path /dashboard/*``, which removes the
prefix before the request arrives. The app therefore cannot see its own mount
point, and every URL it builds -- the login form's action, the stylesheet, the
post-login redirect -- came out rooted at ``/``. On the production host that
meant the login form posted to a path Caddy guards with its own basic auth, so
the dashboard was unreachable through the proxy while working perfectly on the
loopback port.

The header that carries the mount is also the obvious thing to forge, so the
second half of this file is about what happens when a client sends it itself.

Nothing here contacts a provider, renders a video or touches a real database.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import generate_password_hash

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.avatar_job_audit import AvatarJobAudit  # noqa: F401 — registers the table
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.web.app import _normalize_prefix, create_app

USERNAME = "almanya-ops"
PASSWORD = "correct horse battery staple"
SECRET = "s" * 48
PREFIX = "/dashboard"


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


def _app(tmp_path, db, **overrides):
    _engine, factory = db
    application = create_app(settings=_settings(tmp_path, **overrides))
    application.config["TESTING"] = True
    application.config["session_factory"] = factory
    session = factory()
    session.add(
        Episode(
            episode_id="ep_prefix",
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
def mounted(tmp_path, db):
    """The production shape: proxy strips the prefix and announces it."""
    return _app(tmp_path, db, web_forwarded_prefix=PREFIX)


@pytest.fixture
def unmounted(tmp_path, db):
    """The loopback shape: no prefix configured, no header expected."""
    return _app(tmp_path, db)


# The proxy strips the prefix, so the path on the wire never contains it; the
# header is the only thing that carries the mount.
PROXY = {"X-Forwarded-Prefix": PREFIX}


def _csrf(client, **kwargs) -> str:
    import re

    page = client.get("/login", **kwargs).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    assert match, "login page carries no CSRF token"
    return match.group(1)


def _dashboard_csrf(client, **kwargs) -> str:
    """The token the authenticated dashboard publishes for its own fetches."""
    import re

    page = client.get("/", **kwargs).get_data(as_text=True)
    match = re.search(r'name="csrf-token" content="([^"]+)"', page)
    assert match, "the dashboard must publish a token for its own fetches"
    return match.group(1)


# ---------------------------------------------------------------------------
# URLs the app hands out
# ---------------------------------------------------------------------------


def test_login_form_posts_back_to_its_own_mount(mounted):
    """The regression: the form used to post to `/login`, outside the mount."""
    page = mounted.test_client().get("/login", headers=PROXY).get_data(as_text=True)
    assert 'action="/dashboard/login"' in page


def test_stylesheet_is_requested_from_the_mount(mounted):
    page = mounted.test_client().get("/login", headers=PROXY).get_data(as_text=True)
    assert "/dashboard/static/" in page
    assert 'href="/static/' not in page


def test_unauthenticated_redirect_stays_inside_the_mount(mounted):
    resp = mounted.test_client().get("/", headers=PROXY)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/dashboard/login")


def test_prefix_is_never_doubled(mounted):
    """Caddy already stripped it; adding it back must happen exactly once."""
    client = mounted.test_client()
    page = client.get("/login", headers=PROXY).get_data(as_text=True)
    assert "/dashboard/dashboard" not in page
    location = client.get("/", headers=PROXY).headers["Location"]
    assert "/dashboard/dashboard" not in location


def test_index_is_reachable_at_the_stripped_path(mounted):
    """Routing still matches the stripped path, not the public one."""
    client = mounted.test_client()
    _login(client, headers=PROXY)
    assert client.get("/", headers=PROXY).status_code == 200
    # The public path is not what reaches the app, and must not resolve.
    assert client.get("/dashboard/", headers=PROXY).status_code == 404


# ---------------------------------------------------------------------------
# The full operator round trip through the proxy
# ---------------------------------------------------------------------------


def _login(client, headers=None):
    headers = dict(headers or {})
    token = _csrf(client, headers=headers)
    return client.post(
        "/login",
        data={"csrf_token": token, "username": USERNAME, "password": PASSWORD},
        headers=headers,
        follow_redirects=False,
    )


def test_login_logout_and_api_work_through_the_proxy(mounted):
    client = mounted.test_client()

    resp = _login(client, headers=PROXY)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/dashboard")
    assert "/dashboard/dashboard" not in resp.headers["Location"]

    assert client.get("/api/episodes", headers=PROXY).status_code == 200

    token = _dashboard_csrf(client, headers=PROXY)
    out = client.post("/logout", data={"csrf_token": token}, headers=PROXY)
    assert out.status_code == 302
    assert out.headers["Location"].startswith("/dashboard/login")
    # The session is really gone, not merely redirected away from.
    assert client.get("/api/episodes", headers=PROXY).status_code == 401


def test_csrf_is_still_enforced_behind_the_proxy(mounted):
    """A mount point must not become a way around the CSRF check."""
    client = mounted.test_client()
    _login(client, headers=PROXY)
    resp = client.post("/api/render-mode", json={"mode": "local"}, headers=PROXY)
    assert resp.status_code == 403


def test_anonymous_api_is_still_rejected_behind_the_proxy(mounted):
    assert mounted.test_client().get("/api/episodes", headers=PROXY).status_code == 401


def test_video_preview_route_resolves_behind_the_proxy(mounted):
    """The two preview URLs the frontend builds relative to the mount."""
    client = mounted.test_client()
    _login(client, headers=PROXY)
    for path in (
        "/api/episodes/ep_prefix/source.mp4",
        "/api/episodes/ep_prefix/avatar/scenes/scene_1/preview",
    ):
        resp = client.get(path, headers=PROXY)
        # The file does not exist in this fixture; what matters is that the
        # route is reached and authorized rather than answered with a redirect
        # to a login page outside the mount.
        assert resp.status_code != 401
        assert resp.status_code != 302


# ---------------------------------------------------------------------------
# Loopback: unchanged for every deployment that is not behind a sub-path
# ---------------------------------------------------------------------------


def test_without_configuration_urls_stay_at_the_root(unmounted):
    page = unmounted.test_client().get("/login").get_data(as_text=True)
    assert 'action="/login"' in page
    assert "/dashboard" not in page


def test_direct_access_still_works_when_a_prefix_is_configured(mounted):
    """Reaching the loopback port directly must not require the header."""
    client = mounted.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert 'action="/login"' in page
    _login(client)
    assert client.get("/api/episodes").status_code == 200


# ---------------------------------------------------------------------------
# The header is only ever a confirmation, never an instruction
# ---------------------------------------------------------------------------


def test_client_supplied_prefix_is_ignored_when_none_is_configured(unmounted):
    """Nothing in front rewrites the header here, so it must not be believed."""
    page = unmounted.test_client().get(
        "/login", headers={"X-Forwarded-Prefix": "/evil"}
    ).get_data(as_text=True)
    assert 'action="/login"' in page
    assert "/evil" not in page


def test_a_foreign_prefix_cannot_relocate_the_app(mounted):
    """Only the configured mount is honoured, even when one is configured."""
    page = mounted.test_client().get(
        "/login", headers={"X-Forwarded-Prefix": "https://attacker.example"}
    ).get_data(as_text=True)
    assert "attacker.example" not in page
    assert 'action="/login"' in page


def test_a_different_mount_point_is_rejected(mounted):
    page = mounted.test_client().get(
        "/login", headers={"X-Forwarded-Prefix": "/dashboard/../admin"}
    ).get_data(as_text=True)
    assert "admin" not in page
    assert 'action="/login"' in page


def test_no_other_forwarded_header_is_trusted(mounted):
    """Only x_prefix is enabled; scheme and host must come from the server."""
    page = mounted.test_client().get(
        "/login",
        headers={
            **PROXY,
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "https",
        },
    ).get_data(as_text=True)
    assert "attacker.example" not in page


# ---------------------------------------------------------------------------
# Prefix normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/dashboard", "/dashboard"),
        ("dashboard", "/dashboard"),
        ("/dashboard/", "/dashboard"),
        ("  /dashboard  ", "/dashboard"),
        ("", ""),
        ("/", ""),
        (None, ""),
    ],
)
def test_prefix_normalization(raw, expected):
    assert _normalize_prefix(raw) == expected
