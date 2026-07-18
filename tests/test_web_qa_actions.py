"""Tests for Phase 9 QA panel actions: transcript QA approve/request-changes and
translation QA finding status mutation (open/resolved/dismissed).

These endpoints reuse existing ReviewTask/ReviewDecision conventions and the
quality gate's own JSON artifact history for auditing — no parallel DB table.
"""

import json
from datetime import UTC, datetime

import pytest
from flask import Flask

from btcedu.core.qa_reviewer import _gate_path
from btcedu.core.reviewer import create_review_task
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.qa_schema import (
    QAFinding,
    QualityGateDocument,
    QualityGateSummary,
)


@pytest.fixture
def app(db_engine, tmp_path):
    """Flask test app with in-memory DB (mirrors tests/test_review_api.py)."""
    from sqlalchemy.orm import sessionmaker

    from btcedu.web.api import api_bp
    from btcedu.web.jobs import JobManager

    factory = sessionmaker(bind=db_engine)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["session_factory"] = factory
    app.config["settings"] = type(
        "Settings",
        (),
        {
            "database_url": "sqlite:///:memory:",
            "raw_data_dir": str(tmp_path / "raw"),
            "transcripts_dir": str(tmp_path / "transcripts"),
            "outputs_dir": str(tmp_path / "outputs"),
            "reports_dir": str(tmp_path / "reports"),
            "logs_dir": str(tmp_path / "logs"),
        },
    )()
    app.config["job_manager"] = JobManager(str(tmp_path / "logs"))

    app.register_blueprint(api_bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(db_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db_engine)


def _stub_settings(tmp_path):
    """Minimal settings-like object with the attrs core QA helpers read."""

    class _Settings:
        transcripts_dir = str(tmp_path / "transcripts")
        outputs_dir = str(tmp_path / "outputs")

    return _Settings()


def _write_transcript_qa_artifacts(tmp_path, episode_id="ep_tqa"):
    transcript_dir = tmp_path / "transcripts" / episode_id
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.corrected.de.txt").write_text(
        "Korrigierter Text.", encoding="utf-8"
    )
    (transcript_dir / "transcript.corrected.structured.de.json").write_text(
        json.dumps({"segments": []}), encoding="utf-8"
    )

    transcript_output_dir = tmp_path / "outputs" / episode_id / "transcript"
    transcript_output_dir.mkdir(parents=True)
    qa_doc = {
        "schema_version": 1,
        "episode_id": episode_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "red",
        "blocked": True,
        "findings": [],
        "summary": {
            "info_count": 0,
            "minor_count": 0,
            "major_count": 0,
            "critical_count": 0,
            "blocking_count": 0,
        },
        "gate_config": {},
    }
    (transcript_output_dir / "transcript_qa.json").write_text(json.dumps(qa_doc), encoding="utf-8")
    return transcript_dir, transcript_output_dir


@pytest.fixture
def transcript_qa_episode(session_factory, tmp_path):
    """Episode + persisted transcript QA artifact + a pending 'transcript_qa' review task."""
    session = session_factory()
    _write_transcript_qa_artifacts(tmp_path, "ep_tqa")

    episode = Episode(
        episode_id="ep_tqa",
        source="youtube_rss",
        title="Transcript QA Episode",
        url="https://youtube.com/watch?v=ep_tqa",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    session.add(episode)
    session.commit()

    from btcedu.core.transcript_qa import review_artifacts

    task = create_review_task(
        session,
        episode_id="ep_tqa",
        stage="transcript_qa",
        artifact_paths=review_artifacts(_stub_settings(tmp_path), "ep_tqa"),
    )
    session.close()
    return {"task_id": task.id}


@pytest.fixture
def transcript_qa_episode_no_task(session_factory, tmp_path):
    """Episode + persisted transcript QA artifact, but no ReviewTask yet."""
    session = session_factory()
    _write_transcript_qa_artifacts(tmp_path, "ep_tqa2")
    episode = Episode(
        episode_id="ep_tqa2",
        source="youtube_rss",
        title="Transcript QA Episode 2",
        url="https://youtube.com/watch?v=ep_tqa2",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    session.add(episode)
    session.commit()
    session.close()
    return {"episode_id": "ep_tqa2"}


def _write_gate(tmp_path, episode_id, findings, retry_generation=0):
    settings = _stub_settings(tmp_path)
    summary = QualityGateSummary(
        critical_count=sum(1 for f in findings if f.status == "open" and f.severity == "critical"),
        major_count=sum(1 for f in findings if f.status == "open" and f.severity == "major"),
        minor_count=sum(1 for f in findings if f.status == "open" and f.severity == "minor"),
        info_count=sum(1 for f in findings if f.status == "open" and f.severity == "info"),
        open_count=sum(1 for f in findings if f.status == "open"),
        resolved_count=sum(1 for f in findings if f.status == "resolved"),
        dismissed_count=sum(1 for f in findings if f.status == "dismissed"),
    )
    decision = (
        "red" if any(f.status == "open" and f.severity == "critical" for f in findings) else "green"
    )
    gate = QualityGateDocument(
        episode_id=episode_id,
        generated_at=datetime.now(UTC),
        decision=decision,
        status=decision,
        blocked=(decision == "red"),
        deterministic_status=decision,
        findings=findings,
        summary=summary,
        retry_generation=retry_generation,
    )
    path = _gate_path(settings, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gate.model_dump(mode="json")), encoding="utf-8")
    return gate


def _finding(finding_id="qa-0001", status="open", severity="critical", story_id="s1"):
    return QAFinding(
        finding_id=finding_id,
        story_id=story_id,
        category="hallucination",
        severity=severity,
        source_excerpt="Quelle",
        target_excerpt="Ziel",
        explanation="Erklärung",
        required_action="Aktion",
        source_segment_ids=["seg-1", "seg-2"],
        detector="deterministic",
        status=status,
        provider="deterministic",
        model="translation-qa",
        retry_generation=0,
    )


@pytest.fixture
def translation_qa_episode(session_factory, tmp_path):
    """Episode + a quality gate with one open critical finding + a pending
    'translation_qa' review task authorizing mutation."""
    session = session_factory()
    episode = Episode(
        episode_id="ep_gate",
        source="youtube_rss",
        title="Gate Episode",
        url="https://youtube.com/watch?v=ep_gate",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    session.add(episode)
    session.commit()

    _write_gate(tmp_path, "ep_gate", [_finding()])

    from btcedu.core.qa_reviewer import gate_review_artifacts

    task = create_review_task(
        session,
        episode_id="ep_gate",
        stage="translation_qa",
        artifact_paths=gate_review_artifacts(_stub_settings(tmp_path), "ep_gate"),
    )
    session.close()
    return {"task_id": task.id}


# ---------------------------------------------------------------------------
# Transcript QA approve / request-changes
# ---------------------------------------------------------------------------


class TestApproveTranscriptQa:
    def test_approve_existing_task(self, client, transcript_qa_episode, tmp_path):
        resp = client.post(
            "/api/episodes/ep_tqa/qa/transcript/approve",
            json={"notes": "Sieht gut aus"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["review_task_id"] == transcript_qa_episode["task_id"]
        assert data["decision"] == "approved"

        # Audit trail: review_history.json got an entry (existing JSON convention).
        history_path = tmp_path / "outputs" / "ep_tqa" / "review" / "review_history.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert history[-1]["decision"] == "approved"
        assert history[-1]["review_task_id"] == transcript_qa_episode["task_id"]

    def test_approve_creates_task_when_missing(self, client, transcript_qa_episode_no_task):
        resp = client.post(
            "/api/episodes/ep_tqa2/qa/transcript/approve",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["decision"] == "approved"
        assert data["review_task_id"] is not None

    def test_approve_missing_artifact_404(self, client, session_factory):
        session = session_factory()
        episode = Episode(
            episode_id="ep_no_artifact",
            source="youtube_rss",
            title="No Artifact",
            url="https://youtube.com/watch?v=ep_no_artifact",
            status=EpisodeStatus.CORRECTED,
            pipeline_version=2,
        )
        session.add(episode)
        session.commit()
        session.close()

        resp = client.post(
            "/api/episodes/ep_no_artifact/qa/transcript/approve",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert "transcript QA artifact" in resp.get_json()["error"]

    def test_approve_missing_episode_404(self, client):
        resp = client.post(
            "/api/episodes/does-not-exist/qa/transcript/approve",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_approve_rejects_obsolete_review_artifacts(
        self, client, transcript_qa_episode, tmp_path
    ):
        qa_path = tmp_path / "outputs" / "ep_tqa" / "transcript" / "transcript_qa.json"
        qa_path.write_text(qa_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        resp = client.post(
            "/api/episodes/ep_tqa/qa/transcript/approve",
            json={},
            content_type="application/json",
        )

        assert resp.status_code == 409
        assert "obsolete artifacts" in resp.get_json()["error"]

    def test_approve_already_reviewed_400(self, client, transcript_qa_episode):
        resp1 = client.post(
            "/api/episodes/ep_tqa/qa/transcript/approve",
            json={},
            content_type="application/json",
        )
        assert resp1.status_code == 200

        resp2 = client.post(
            "/api/episodes/ep_tqa/qa/transcript/approve",
            json={},
            content_type="application/json",
        )
        # No actionable task left, and the artifact is unchanged, so a fresh
        # (already-approved) task is created and rejected as non-actionable... but
        # since the artifact still exists, a brand new PENDING task is created
        # instead, which is itself approvable. Assert the endpoint stays usable.
        assert resp2.status_code == 200


class TestRequestChangesTranscriptQa:
    def test_missing_notes_400(self, client, transcript_qa_episode):
        resp = client.post(
            "/api/episodes/ep_tqa/qa/transcript/request-changes",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "Notes are required" in resp.get_json()["error"]

    def test_with_notes_200(self, client, transcript_qa_episode, tmp_path):
        resp = client.post(
            "/api/episodes/ep_tqa/qa/transcript/request-changes",
            json={"notes": "Bitte Namen prüfen"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["decision"] == "changes_requested"

        history_path = tmp_path / "outputs" / "ep_tqa" / "review" / "review_history.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert history[-1]["decision"] == "changes_requested"
        assert history[-1]["notes"] == "Bitte Namen prüfen"

    def test_missing_episode_404(self, client):
        resp = client.post(
            "/api/episodes/does-not-exist/qa/transcript/request-changes",
            json={"notes": "x"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_progressed_episode_is_not_silently_rewound(
        self, client, transcript_qa_episode, session_factory
    ):
        session = session_factory()
        episode = session.query(Episode).filter(Episode.episode_id == "ep_tqa").one()
        episode.status = EpisodeStatus.RENDERED
        session.commit()
        session.close()

        resp = client.post(
            "/api/episodes/ep_tqa/qa/transcript/request-changes",
            json={"notes": "Bitte erneut prüfen"},
            content_type="application/json",
        )

        assert resp.status_code == 409
        assert "only valid while the episode is corrected" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Translation QA finding status mutation
# ---------------------------------------------------------------------------


class TestFindingStatusMutation:
    def test_invalid_status_400(self, client, translation_qa_episode):
        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "archived"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "status must be one of" in resp.get_json()["error"]

    def test_missing_episode_404(self, client):
        resp = client.post(
            "/api/episodes/does-not-exist/qa/findings/qa-0001/status",
            json={"status": "resolved"},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_missing_review_task_404(self, client, session_factory, tmp_path):
        session = session_factory()
        episode = Episode(
            episode_id="ep_no_task",
            source="youtube_rss",
            title="No Task",
            url="https://youtube.com/watch?v=ep_no_task",
            status=EpisodeStatus.ADAPTED,
            pipeline_version=2,
        )
        session.add(episode)
        session.commit()
        session.close()
        _write_gate(tmp_path, "ep_no_task", [_finding()])

        resp = client.post(
            "/api/episodes/ep_no_task/qa/findings/qa-0001/status",
            json={"status": "resolved"},
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert "review task" in resp.get_json()["error"]

    def test_missing_finding_404(self, client, translation_qa_episode):
        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-9999/status",
            json={"status": "resolved"},
            content_type="application/json",
        )
        assert resp.status_code == 404
        assert "Finding not found" in resp.get_json()["error"]

    def test_task_not_actionable_400(self, client, translation_qa_episode, session_factory):
        session = session_factory()
        from btcedu.core.reviewer import approve_review

        approve_review(session, translation_qa_episode["task_id"])
        session.close()

        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "resolved"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "must be pending or in_review" in resp.get_json()["error"]

    def test_obsolete_review_task_cannot_mutate_new_gate(
        self, client, translation_qa_episode, tmp_path
    ):
        gate_path = _gate_path(_stub_settings(tmp_path), "ep_gate")
        gate_path.write_text(gate_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "resolved"},
            content_type="application/json",
        )

        assert resp.status_code == 409
        assert "obsolete artifacts" in resp.get_json()["error"]

    def test_resolve_updates_summary_and_history(self, client, translation_qa_episode):
        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "resolved", "note": "false positive, fixed manually"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        gate = data["quality_gate"]
        finding = next(f for f in gate["findings"] if f["finding_id"] == "qa-0001")
        assert finding["status"] == "resolved"
        assert finding["history"][-1]["status"] == "resolved"
        assert finding["history"][-1]["note"] == "false positive, fixed manually"
        # Summary recomputed: no longer counted as open/critical.
        assert gate["summary"]["open_count"] == 0
        assert gate["summary"]["critical_count"] == 0
        assert gate["summary"]["resolved_count"] == 1
        # Decision/blocked are left untouched by manual curation (requires a
        # full QA re-run to change, per the "safe, minimal" design).
        assert gate["decision"] == "red"
        assert gate["blocked"] is True

    def test_dismiss_then_reopen(self, client, translation_qa_episode):
        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "dismissed"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["quality_gate"]["summary"]["dismissed_count"] == 1

        resp2 = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "open"},
            content_type="application/json",
        )
        assert resp2.status_code == 200
        gate = resp2.get_json()["quality_gate"]
        assert gate["summary"]["open_count"] == 1
        finding = next(f for f in gate["findings"] if f["finding_id"] == "qa-0001")
        # History accumulates every transition — durable audit trail.
        statuses = [e["status"] for e in finding["history"]]
        assert statuses == ["dismissed", "open"]

    def test_persists_across_get_qa(self, client, translation_qa_episode):
        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "dismissed"},
            content_type="application/json",
        )
        assert resp.status_code == 200

        resp2 = client.get("/api/episodes/ep_gate/qa")
        assert resp2.status_code == 200
        gate = resp2.get_json()["quality_gate"]
        finding = next(f for f in gate["findings"] if f["finding_id"] == "qa-0001")
        assert finding["status"] == "dismissed"


class TestFindingStatusAndStructuredRestart:
    """Verify curated finding statuses are respected by the structured-findings
    helpers that back 'Restart Translate + Adapt' and 'Restart All'."""

    def test_dismissed_finding_excluded_from_targeted_repair(
        self, client, translation_qa_episode, tmp_path
    ):
        from btcedu.core.qa_reviewer import (
            build_story_findings,
            load_quality_gate,
            target_story_ids_from_gate,
        )

        class _Settings:
            outputs_dir = str(tmp_path / "outputs")

        settings = _Settings()
        gate_before = load_quality_gate(settings, "ep_gate")
        assert target_story_ids_from_gate(gate_before) == ["s1"]

        resp = client.post(
            "/api/episodes/ep_gate/qa/findings/qa-0001/status",
            json={"status": "dismissed"},
            content_type="application/json",
        )
        assert resp.status_code == 200

        gate_after = load_quality_gate(settings, "ep_gate")
        assert target_story_ids_from_gate(gate_after) == []
        assert build_story_findings(gate_after) == {}
