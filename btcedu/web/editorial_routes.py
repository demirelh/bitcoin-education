"""Private editorial review surface for newsroom article revisions (N4).

Everything here is internal. There is no export, no upload and no generation
job behind any of these routes — the only state they change is an editorial
decision written by a named, logged-in operator.

The blueprint is registered after ``init_auth``, so the global login gate and
CSRF protection apply automatically. No endpoint of this module may ever be
added to ``PUBLIC_ENDPOINTS``.
"""

import logging

from flask import Blueprint, current_app, jsonify, render_template, request

from btcedu.core.editorial.article import (
    ArticleApprovalBlocked,
    StaleArticleApproval,
    approve_article_revision,
    article_preview,
    reject_article_revision,
)
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import EditorialRevision, ResearchRun
from btcedu.web.auth import operator_ref

logger = logging.getLogger(__name__)

# The JSON routes are mounted under ``/api`` by the application factory. That
# prefix is what makes an unauthenticated call answer 401 instead of redirecting
# a programmatic caller into the login page, which it would parse as data.
editorial_bp = Blueprint("editorial", __name__)
editorial_api_bp = Blueprint("editorial_api", __name__)


def _session():
    return current_app.config["session_factory"]()


def _load(session, article_revision_id: str):
    """Return the article and the research run its evidence belongs to."""
    article = (
        session.query(ArticleRevision)
        .filter_by(article_revision_id=article_revision_id)
        .one_or_none()
    )
    if article is None:
        return None, None
    revision = session.get(EditorialRevision, article.editorial_revision_id)
    run = (
        session.query(ResearchRun)
        .filter_by(topic_id=revision.topic_id)
        .order_by(ResearchRun.id.desc())
        .first()
    )
    return article, run


def _payload(session, article, run) -> dict:
    return article_preview(session, article, research_run=run)


@editorial_bp.get("/articles/<article_revision_id>")
def article_review_page(article_revision_id: str):
    """The private review view: prose, its evidence and its credits together."""
    session = _session()
    try:
        article, run = _load(session, article_revision_id)
        if article is None or run is None:
            return render_template("editorial_article.html", article=None), 404
        return render_template(
            "editorial_article.html", article=_payload(session, article, run)
        )
    finally:
        session.close()


@editorial_api_bp.get("/articles/<article_revision_id>")
def article_review_json(article_revision_id: str):
    session = _session()
    try:
        article, run = _load(session, article_revision_id)
        if article is None or run is None:
            return jsonify({"error": "unknown_article"}), 404
        return jsonify(_payload(session, article, run))
    finally:
        session.close()


@editorial_api_bp.post("/articles/<article_revision_id>/approve")
def article_approve(article_revision_id: str):
    """Release an article — only for exactly the state the operator reviewed."""
    body = request.get_json(silent=True) or {}
    session = _session()
    try:
        article, run = _load(session, article_revision_id)
        if article is None or run is None:
            return jsonify({"error": "unknown_article"}), 404
        try:
            decision = approve_article_revision(
                session,
                article,
                research_run=run,
                operator_ref=operator_ref(),
                reviewed_content_hash=str(body.get("content_hash", "")),
                reviewed_evidence_hash=str(body.get("evidence_hash", "")),
                reviewed_media_hash=str(body.get("media_hash", "")),
                rationale=str(body.get("rationale", "")),
            )
        except StaleArticleApproval as exc:
            return (
                jsonify(
                    {
                        "error": "stale_review",
                        "message": str(exc),
                        "current": _payload(session, article, run),
                    }
                ),
                409,
            )
        except ArticleApprovalBlocked as exc:
            return jsonify({"error": "blocked", "reasons": list(exc.reasons)}), 422
        return jsonify(
            {
                "decision_id": decision.decision_id,
                "status": article.status,
                "operator_ref": decision.operator_ref,
            }
        )
    finally:
        session.close()


@editorial_api_bp.post("/articles/<article_revision_id>/reject")
def article_reject(article_revision_id: str):
    body = request.get_json(silent=True) or {}
    rationale = str(body.get("rationale", "")).strip()
    if not rationale:
        return jsonify({"error": "rationale_required"}), 400
    session = _session()
    try:
        article, run = _load(session, article_revision_id)
        if article is None or run is None:
            return jsonify({"error": "unknown_article"}), 404
        decision = reject_article_revision(
            session,
            article,
            research_run=run,
            operator_ref=operator_ref(),
            rationale=rationale,
        )
        return jsonify(
            {
                "decision_id": decision.decision_id,
                "status": article.status,
                "operator_ref": decision.operator_ref,
            }
        )
    finally:
        session.close()
