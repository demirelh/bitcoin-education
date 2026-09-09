"""The private editorial review surface (N4 web layer).

These tests ask what someone who has reached the port can do with an article
that nobody approved yet, and whether the audit row afterwards names a real
operator. No provider is contacted; the article state is built by the same
fixture pipeline the domain tests use.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from werkzeug.security import generate_password_hash

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.article import ArticleRevision, ArticleStatus, EditorialDecision
from btcedu.web.app import create_app
from tests.test_editorial_article import _draft, _pipeline

USERNAME = "almanya-ops"
PASSWORD = "correct horse battery staple"
SECRET = "s" * 48


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
        web_cookie_secure=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def engine():
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
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def app(tmp_path, factory):
    application = create_app(settings=_settings(tmp_path))
    application.config["TESTING"] = True
    application.config["session_factory"] = factory
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf(client) -> str:
    page = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert match, "the login form must carry a token"
    return match.group(1).decode()


@pytest.fixture
def operator(client):
    response = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    return client


def _page_csrf(client, url) -> str:
    page = client.get(url)
    match = re.search(rb'name="csrf-token" content="([^"]+)"', page.data)
    assert match, "the review page must publish a token for its own actions"
    return match.group(1).decode()


def _article(factory, tmp_path, drafter=None, **kwargs):
    """Build a complete reviewable article and return its public id."""
    from btcedu.core.editorial.article import generate_article_revision

    session = factory()
    revision, run, _ = _pipeline(session, tmp_path, **kwargs)
    article = generate_article_revision(
        session,
        editorial_revision=revision,
        research_run=run,
        drafter=drafter or (lambda payload: _draft()),
    )
    article_id = article.article_revision_id
    session.close()
    return article_id


# ---------------------------------------------------------------------------
# Reaching the surface at all
# ---------------------------------------------------------------------------


def test_anonymous_cannot_read_a_draft_article(client, factory, tmp_path):
    article_id = _article(factory, tmp_path)

    assert client.get(f"/editorial/articles/{article_id}").status_code in (302, 401)
    assert client.get(f"/api/editorial/articles/{article_id}").status_code == 401


def test_anonymous_cannot_approve(client, factory, tmp_path):
    article_id = _article(factory, tmp_path)

    response = client.post(
        f"/api/editorial/articles/{article_id}/approve",
        json={"content_hash": "x", "evidence_hash": "y", "media_hash": "z"},
    )

    assert response.status_code == 401


def test_the_editorial_routes_are_not_public_endpoints():
    """A newsroom route must never be on the login allowlist."""
    from btcedu.web.auth import PUBLIC_ENDPOINTS

    assert not any(
        endpoint.startswith(("editorial.", "editorial_api."))
        for endpoint in PUBLIC_ENDPOINTS
    )


def test_a_logged_in_operator_sees_the_private_preview(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)

    response = operator.get(f"/editorial/articles/{article_id}")

    assert response.status_code == 200
    assert b"Berlin" in response.data


def test_an_unknown_article_is_not_found(operator):
    assert operator.get("/api/editorial/articles/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Acting on it
# ---------------------------------------------------------------------------


def test_approval_without_a_csrf_token_is_refused(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)
    state = operator.get(f"/api/editorial/articles/{article_id}").get_json()

    response = operator.post(
        f"/api/editorial/articles/{article_id}/approve",
        json={
            "content_hash": state["content_hash"],
            "evidence_hash": state["evidence_hash"],
            "media_hash": state["media_hash"],
        },
    )

    assert response.status_code == 403
    session = factory()
    assert session.query(EditorialDecision).count() == 0
    session.close()


def test_an_operator_approves_the_state_they_reviewed(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)
    token = _page_csrf(operator, f"/editorial/articles/{article_id}")
    state = operator.get(f"/api/editorial/articles/{article_id}").get_json()

    response = operator.post(
        f"/api/editorial/articles/{article_id}/approve",
        json={
            "content_hash": state["content_hash"],
            "evidence_hash": state["evidence_hash"],
            "media_hash": state["media_hash"],
            "rationale": "Belege geprüft",
        },
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 200
    session = factory()
    article = (
        session.query(ArticleRevision).filter_by(article_revision_id=article_id).one()
    )
    decision = session.query(EditorialDecision).one()
    assert article.status == ArticleStatus.APPROVED.value
    assert USERNAME in decision.operator_ref
    assert decision.reviewed_content_hash == state["content_hash"]
    session.close()


def test_a_click_on_a_stale_preview_does_not_release(operator, factory, tmp_path):
    """The picture is withdrawn between reading and clicking."""
    from btcedu.core.editorial.media import revoke_media_decision
    from btcedu.models.media_rights import MediaUseDecision

    article_id = _article(factory, tmp_path)
    token = _page_csrf(operator, f"/editorial/articles/{article_id}")
    state = operator.get(f"/api/editorial/articles/{article_id}").get_json()

    session = factory()
    decision = session.query(MediaUseDecision).one()
    revoke_media_decision(session, decision, reason="Rechte unklar")
    session.close()

    response = operator.post(
        f"/api/editorial/articles/{article_id}/approve",
        json={
            "content_hash": state["content_hash"],
            "evidence_hash": state["evidence_hash"],
            "media_hash": state["media_hash"],
        },
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "stale_review"
    session = factory()
    article = (
        session.query(ArticleRevision).filter_by(article_revision_id=article_id).one()
    )
    assert article.status != ArticleStatus.APPROVED.value
    assert session.query(EditorialDecision).count() == 0
    session.close()


def _weaken_evidence(factory):
    """The evidence turns out not to carry the claim after all."""
    from btcedu.models.editorial import ClaimAssessment

    session = factory()
    assessment = session.query(ClaimAssessment).one()
    assessment.verdict = "insufficient"
    session.commit()
    session.close()


def test_an_open_core_claim_blocks_the_button(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)
    token = _page_csrf(operator, f"/editorial/articles/{article_id}")
    _weaken_evidence(factory)
    state = operator.get(f"/api/editorial/articles/{article_id}").get_json()

    assert state["open_issues"]

    response = operator.post(
        f"/api/editorial/articles/{article_id}/approve",
        json={
            "content_hash": state["content_hash"],
            "evidence_hash": state["evidence_hash"],
            "media_hash": state["media_hash"],
        },
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 422
    assert response.get_json()["reasons"]


def test_a_rejection_needs_a_reason(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)
    token = _page_csrf(operator, f"/editorial/articles/{article_id}")

    response = operator.post(
        f"/api/editorial/articles/{article_id}/reject",
        json={"rationale": "   "},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 400
    session = factory()
    assert session.query(EditorialDecision).count() == 0
    session.close()


def test_a_rejection_is_recorded_with_its_reason(operator, factory, tmp_path):
    article_id = _article(factory, tmp_path)
    token = _page_csrf(operator, f"/editorial/articles/{article_id}")

    response = operator.post(
        f"/api/editorial/articles/{article_id}/reject",
        json={"rationale": "Quelle zu dünn"},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 200
    session = factory()
    decision = session.query(EditorialDecision).one()
    assert decision.decision == "reject"
    assert decision.rationale == "Quelle zu dünn"
    session.close()


# ---------------------------------------------------------------------------
# What the page renders
# ---------------------------------------------------------------------------


def test_generated_text_cannot_inject_markup_into_the_review_page(
    operator, factory, tmp_path
):
    """A drafter is an LLM; its output is data, never page structure."""
    from btcedu.core.editorial.article import ArticleContentRejected

    def hostile(payload):
        return _draft(title="<script>alert(1)</script>")

    with pytest.raises(ArticleContentRejected):
        _article(factory, tmp_path, drafter=hostile)


def test_a_caption_is_escaped_in_the_review_page(operator, factory, tmp_path):
    from btcedu.models.media_rights import MediaUseDecision

    article_id = _article(factory, tmp_path)
    session = factory()
    decision = session.query(MediaUseDecision).one()
    decision.attribution_text = '<img src=x onerror="alert(1)">'
    session.commit()
    session.close()

    page = operator.get(f"/editorial/articles/{article_id}")

    assert b'<img src=x onerror' not in page.data
    assert b"&lt;img src=x onerror" in page.data


def test_the_review_page_shows_the_open_issues_next_to_the_prose(
    operator, factory, tmp_path
):
    article_id = _article(factory, tmp_path)
    _weaken_evidence(factory)

    page = operator.get(f"/editorial/articles/{article_id}")

    assert page.status_code == 200
    assert b"Freigabe blockiert" in page.data
