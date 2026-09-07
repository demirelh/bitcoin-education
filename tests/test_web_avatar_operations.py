"""Operating the avatar pipeline from the dashboard.

Three questions decide whether this code is safe. Can a click cost money that
nobody agreed to? Can a decision land on a state the operator never saw? And
can an episode lose its presenter without somebody having said so, in writing,
for that one episode?

Everything here is about those three. The routes themselves hold no logic worth
testing: they collect a reason, bind a confirmation to a digest and call the
very same domain functions the CLI calls. What is tested is that they refuse
everything else.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.core import anchor_review, avatar_breaker, avatar_reconcile
from btcedu.core.anchor_fallback import (
    PRESENTATION_AVATAR,
    PRESENTATION_VOICE_OVER,
    active_voice_over_override,
    presentation_mode,
)
from btcedu.core.avatar_jobs import reserve_scene
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    ROLE_REPORTER,
    TEMPLATE_ANCHOR,
    TEMPLATE_REPORTER,
    VISUAL_MODE_FULLSCREEN,
    VISUAL_MODE_STUDIO,
    Scene,
    write_scene_plan,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.presenter_assignment import PresenterAssignment
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.profiles import reset_registry

EPISODE_ID = "ep_web_ops"
LOOK_ID = "heygen_look_alpha_0001"
CLIP_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 512


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def no_readiness_blockers():
    with patch.object(anchor_review, "_rights_and_readiness_blockers", return_value=[]):
        yield


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        anchor_enabled=True,
        heygen_api_key="super-secret-key",
        pipeline_version=2,
    )


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def session(factory):
    sess = factory()
    yield sess
    sess.close()


@pytest.fixture
def episode(session):
    ep = Episode(
        episode_id=EPISODE_ID,
        source="local_recorder",
        title="Tagesschau",
        url="/mnt/rec/x.mp4",
        status=EpisodeStatus.ANCHOR_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    session.add(ep)
    session.add(
        PresenterAssignment(
            episode_id=EPISODE_ID,
            provider="heygen",
            engine="avatar_iii",
            avatar_type="digital_twin",
            avatar_look_id=LOOK_ID,
            look_name="look_02",
            strategy="least_recently_used",
            config_version=1,
            status="assigned",
            content_hash="assignment-hash",
            provenance_path="",
            cost_per_second_usd=0.0167,
        )
    )
    session.commit()
    return ep


@pytest.fixture
def client(settings, factory, episode):
    from btcedu.web.app import create_app

    app = create_app(settings=settings)
    app.config["session_factory"] = factory
    app.config["TESTING"] = True
    return app.test_client()


def _scene(scene_id, chapter_id, order, role):
    studio = role == ROLE_ANCHOR
    return Scene(
        scene_id=scene_id,
        chapter_id=chapter_id,
        order=order,
        beat_index=0,
        speaker_role=role,
        purpose="report",
        segment_indices=[0],
        text_hash=f"text-{scene_id}",
        audio_file=None,
        expected_duration_seconds=6.0,
        visual_mode=VISUAL_MODE_STUDIO if studio else VISUAL_MODE_FULLSCREEN,
        template_id=TEMPLATE_ANCHOR if studio else TEMPLATE_REPORTER,
        background_asset=None if studio else "images/ch_01.jpg",
        background_asset_type="none" if studio else "image",
        display_zone_id="monitor" if studio else None,
        display_fit_mode="contain",
        focus_point=None,
        transition_in="cut",
        overlays={},
        needs_avatar=studio,
    )


def _plan(settings):
    outputs = Path(settings.outputs_dir) / EPISODE_ID
    outputs.mkdir(parents=True, exist_ok=True)
    write_scene_plan(
        outputs / "scene_plan.json",
        EPISODE_ID,
        [
            _scene("sc_001", "ch_01", 1, ROLE_ANCHOR),
            _scene("sc_002", "ch_01", 2, ROLE_REPORTER),
            _scene("sc_003", "ch_02", 3, ROLE_ANCHOR),
        ],
        presenter_look_id=LOOK_ID,
        presenter_assignment_id=1,
    )
    return outputs


def _reserve(session, scene_id, chapter_id, *, status, cost=0.1002):
    job = reserve_scene(
        session,
        episode_id=EPISODE_ID,
        scene_id=scene_id,
        chapter_id=chapter_id,
        content_hash=f"hash-{scene_id}",
        provider="heygen",
        engine="avatar_iii",
        avatar_look_id=LOOK_ID,
        output_format="mp4",
        estimated_cost_usd=cost,
    ).job
    job.status = status
    job.cost_usd = cost
    job.duration_seconds = 6.0
    session.commit()
    return job


def _json(response):
    return response.get_json()


def _open_breaker(session, provider="heygen"):
    """Trip the provider-wide breaker the way a bad afternoon would."""
    from btcedu.core.avatar_retry import FailureClass, FailureVerdict

    for _ in range(10):
        avatar_breaker.record_failure(
            session,
            provider,
            FailureVerdict(
                operation="submit",
                failure_class=FailureClass.SERVER_ERROR,
                retryable=True,
                ambiguous=False,
                status_code=500,
                reason="provider unavailable",
            ),
        )


# ---------------------------------------------------------------------------
# Runtime display
# ---------------------------------------------------------------------------


class TestTheRuntimeDisplay:
    def test_it_shows_concurrency_and_the_jobs_in_flight(self, client, session, settings):
        _plan(settings)
        _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.SUBMITTED.value)
        _reserve(session, "sc_003", "ch_02", status=AvatarJobStatus.RESERVED.value)

        rt = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]

        assert rt["max_concurrent_jobs"] >= 1
        assert rt["active_jobs"] == 1
        assert rt["polling_jobs"] == 1
        assert rt["waiting_submits"] == 1
        assert rt["available_slots"] == max(0, rt["max_concurrent_jobs"] - 1)

    def test_it_names_the_engine_and_the_studio_mode(self, client, session, settings):
        _plan(settings)
        rt = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]
        assert rt["engine"]
        assert rt["studio_mode"] in {"alpha_webm", "opaque_mp4", "baked", "composite"}

    def test_it_reports_retries_and_the_next_poll(self, client, session, settings):
        _plan(settings)
        job = _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.SUBMITTED.value)
        job.retry_count = 2
        job.last_error_type = "rate_limit"
        job.last_status_code = 429
        job.retry_after_seconds = 30
        from datetime import UTC, datetime, timedelta

        job.next_poll_at = datetime.now(UTC) + timedelta(seconds=45)
        session.commit()

        rt = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]

        assert rt["retry_count"] == 2
        assert rt["retrying_jobs"] == 1
        assert rt["last_error_type"] == "rate_limit"
        assert rt["last_status_code"] == 429
        assert rt["retry_after_seconds"] == 30
        assert rt["next_poll_at"]

    def test_a_closed_breaker_looks_different_from_an_open_one(
        self, client, session, settings
    ):
        _plan(settings)
        closed = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]
        assert closed["circuit_breaker"]["state"] == "closed"
        assert closed["circuit_breaker"]["submissions_allowed"] is True

        _open_breaker(session)
        opened = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]
        assert opened["circuit_breaker"]["state"] == "open"
        assert opened["circuit_breaker"]["submissions_allowed"] is False
        assert opened["circuit_breaker"]["reason"]
        assert opened["circuit_breaker"]["cooldown_until"]

    def test_costs_separate_spent_reserved_and_unresolved(self, client, session, settings):
        _plan(settings)
        _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.COMPLETED.value, cost=0.5)
        _reserve(session, "sc_003", "ch_02", status=AvatarJobStatus.RESERVED.value, cost=0.25)
        _reserve(
            session,
            "sc_004",
            "ch_03",
            status=AvatarJobStatus.RECONCILE_REQUIRED.value,
            cost=0.125,
        )

        rt = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]

        assert rt["cost_actual_usd"] == pytest.approx(0.5)
        assert rt["cost_reserved_usd"] == pytest.approx(0.25)
        assert rt["cost_unresolved_usd"] == pytest.approx(0.125)
        assert rt["cost_committed_usd"] == pytest.approx(0.875)

    def test_the_remaining_budget_counts_unresolved_money_as_spent(
        self, client, session, settings
    ):
        """An unresolved job may well have been billed.

        Treating it as free would authorise a second purchase with money that
        is possibly already gone — the exact mistake the ledger exists to stop.
        """
        _plan(settings)
        _reserve(
            session,
            "sc_001",
            "ch_01",
            status=AvatarJobStatus.RECONCILE_REQUIRED.value,
            cost=1.0,
        )

        rt = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))["runtime"]

        assert rt["max_cost_usd"] is not None
        assert rt["budget_remaining_usd"] == pytest.approx(rt["max_cost_usd"] - 1.0)

    def test_it_never_leaks_a_key_or_a_signed_url(self, client, session, settings):
        _plan(settings)
        job = _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.SUBMITTED.value)
        job.provider_job_id = "heygen_job_42"
        session.commit()

        body = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_data(as_text=True)

        assert "super-secret-key" not in body
        assert "https://" not in body
        assert "idempotency" not in body.lower()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliationFromTheDashboard:
    def test_it_lists_only_the_jobs_that_need_a_human(self, client, session, settings):
        _plan(settings)
        _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.COMPLETED.value)
        _reserve(
            session, "sc_003", "ch_02", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )

        data = _json(client.get(f"/api/avatar/reconcile?episode_id={EPISODE_ID}"))

        assert data["count"] == 1
        assert data["jobs"][0]["scene_id"] == "sc_003"
        assert {d["value"] for d in data["decisions"]} == set(avatar_reconcile.DECISIONS)

    def test_the_detail_carries_the_audit_trail_and_a_recommendation(
        self, client, session, settings
    ):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )

        data = _json(client.get(f"/api/avatar/reconcile/{job.id}"))

        assert data["job"]["scene_id"] == "sc_001"
        assert data["next_safe_action"]
        assert data["digest"]
        assert isinstance(data["audit"], list)
        assert data["integrity"]["status"] in {"ok", "missing", "unrecorded", "mismatch"}

    def test_an_unknown_job_is_a_404(self, client):
        assert client.get("/api/avatar/reconcile/9999").status_code == 404

    def test_a_decision_without_a_reason_is_refused(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": "unresolved"},
            )
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": "unresolved",
                "digest": prepared["digest"],
                "reason": "   ",
            },
        )
        assert response.status_code == 400
        assert not session.query(AvatarJobAudit).count()

    def test_preparing_changes_nothing(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={"action": "prepare", "decision": "abandon"},
        )
        session.expire_all()
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert session.query(AvatarJobAudit).count() == 0

    @pytest.mark.parametrize("decision", ["running", "unresolved", "abandon"])
    def test_each_allowed_decision_is_recorded(self, client, session, settings, decision):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        job.provider_job_id = "heygen_job_1"
        session.commit()

        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": decision},
            )
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": decision,
                "digest": prepared["digest"],
                "reason": "checked in the provider dashboard",
            },
        )
        assert response.status_code == 200, response.get_json()
        assert session.query(AvatarJobAudit).count() == 1

    def test_a_stale_digest_is_a_409(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": "abandon",
                "digest": "0" * 64,
                "reason": "gone",
            },
        )
        assert response.status_code == 409
        assert response.get_json()["digest"]

    def test_a_repeated_confirmation_is_one_decision(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": "abandon"},
            )
        )
        body = {
            "action": "confirm",
            "decision": "abandon",
            "digest": prepared["digest"],
            "reason": "the provider never generated it",
        }
        first = client.post(f"/api/avatar/reconcile/{job.id}/resolve", json=body)
        second = client.post(f"/api/avatar/reconcile/{job.id}/resolve", json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.get_json()["status"] == "already_applied"
        assert (
            session.query(AvatarJobAudit)
            .filter(AvatarJobAudit.action == AvatarAuditAction.ABANDONED.value)
            .count()
            == 1
        )

    def test_a_competing_decision_on_another_state_is_refused(
        self, client, session, settings
    ):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": "abandon"},
            )
        )
        # Somebody else resolves it differently in the meantime.
        avatar_reconcile.resolve(
            session,
            job.id,
            decision="unresolved",
            note="still searching",
            operator_ref="cli",
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": "abandon",
                "digest": prepared["digest"],
                "reason": "give up",
            },
        )
        assert response.status_code == 409

    def test_a_settled_job_is_not_reopened(self, client, session, settings):
        _plan(settings)
        job = _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.COMPLETED.value)
        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": "abandon"},
            )
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": "abandon",
                "digest": prepared["digest"],
                "reason": "changed my mind",
            },
        )
        assert response.status_code == 422

    def test_giving_up_keeps_the_cost_on_the_episode(self, client, session, settings):
        """Abandoning is about the clip, never about the invoice."""
        _plan(settings)
        job = _reserve(
            session,
            "sc_001",
            "ch_01",
            status=AvatarJobStatus.RECONCILE_REQUIRED.value,
            cost=0.42,
        )
        prepared = _json(
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={"action": "prepare", "decision": "abandon"},
            )
        )
        client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={
                "action": "confirm",
                "decision": "abandon",
                "digest": prepared["digest"],
                "reason": "provider cannot identify it",
            },
        )
        session.expire_all()
        assert float(job.cost_usd) == pytest.approx(0.42)

    def test_a_missing_provider_job_is_not_evidence_of_a_free_failure(
        self, client, session, settings
    ):
        """'not-billed' needs a provider statement, not the absence of one.

        The dashboard offers the decision, because sometimes the provider does
        say so — but it has to be typed as a reason, and nothing in the UI
        derives it from a 404.
        """
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        detail = _json(client.get(f"/api/avatar/reconcile/{job.id}"))
        assert "never 'not-billed'" in detail["next_safe_action"]

    def test_attaching_a_job_id_needs_confirmation_and_a_reason(
        self, client, session, settings
    ):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        refused = client.post(
            f"/api/avatar/reconcile/{job.id}/attach",
            json={"provider_job_id": "heygen_x", "reason": "found it"},
        )
        assert refused.status_code == 422

        accepted = client.post(
            f"/api/avatar/reconcile/{job.id}/attach",
            json={"provider_job_id": "heygen_x", "reason": "found it", "confirm": True},
        )
        assert accepted.status_code == 200
        session.expire_all()
        assert job.provider_job_id == "heygen_x"

    def test_the_breaker_can_be_closed_with_a_reason_only(self, client, session, settings):
        _plan(settings)
        _open_breaker(session)
        assert client.post("/api/avatar/breaker/heygen/reset", json={}).status_code == 400

        response = client.post(
            "/api/avatar/breaker/heygen/reset", json={"reason": "provider status page green"}
        )
        assert response.status_code == 200
        assert response.get_json()["circuit_breaker"]["state"] == "closed"

    def test_no_provider_is_contacted_from_a_web_request(self, client, session, settings):
        """The routes import no provider service at all.

        Checked by patching the ledger's submission entry point: if any route
        below reached it, the pipeline would be ordering clips out of a browser
        request, which is exactly what the two-step design exists to prevent.
        """
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        with patch("btcedu.core.avatar_jobs.record_submission") as submitted:
            prepared = _json(
                client.post(
                    f"/api/avatar/reconcile/{job.id}/resolve",
                    json={"action": "prepare", "decision": "unresolved"},
                )
            )
            client.post(
                f"/api/avatar/reconcile/{job.id}/resolve",
                json={
                    "action": "confirm",
                    "decision": "unresolved",
                    "digest": prepared["digest"],
                    "reason": "provider cannot find it",
                },
            )
        submitted.assert_not_called()

    def test_a_cross_origin_post_is_refused(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            json={"action": "prepare", "decision": "abandon"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403

    def test_a_form_post_is_refused(self, client, session, settings):
        """A form can be submitted cross-site; JSON cannot, without a preflight."""
        _plan(settings)
        job = _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        response = client.post(
            f"/api/avatar/reconcile/{job.id}/resolve",
            data={"action": "confirm", "decision": "abandon", "reason": "x"},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# The voice-over emergency path
# ---------------------------------------------------------------------------


class TestTheVoiceOverOverride:
    def test_it_is_never_chosen_automatically(self, client, session, settings):
        _plan(settings)
        _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        data = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))
        assert data["presentation"]["mode"] == PRESENTATION_AVATAR
        assert data["presentation"]["voice_over_override"] is None

    def test_preparing_shows_the_consequences_and_changes_nothing(
        self, client, session, settings
    ):
        _plan(settings)
        _reserve(
            session,
            "sc_001",
            "ch_01",
            status=AvatarJobStatus.RECONCILE_REQUIRED.value,
            cost=0.3,
        )
        prep = _json(
            client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json={"action": "prepare"}
            )
        )["override"]

        assert [s["scene_id"] for s in prep["anchor_scenes"]] == ["sc_001", "sc_003"]
        assert prep["blockers"]
        assert prep["cost_unresolved_usd"] == pytest.approx(0.3)
        assert any("Moderatorin" in line for line in prep["consequences"])
        assert prep["digest"]
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_AVATAR
        assert session.query(AvatarJobAudit).count() == 0

    def test_confirming_needs_a_reason_and_an_acknowledgement(
        self, client, session, settings
    ):
        _plan(settings)
        prep = _json(
            client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json={"action": "prepare"}
            )
        )["override"]

        no_reason = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "confirm", "digest": prep["digest"], "acknowledged": True},
        )
        assert no_reason.status_code == 400

        no_ack = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "confirm", "digest": prep["digest"], "reason": "provider down"},
        )
        assert no_ack.status_code == 422
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_AVATAR

    def test_a_stale_digest_is_a_409(self, client, session, settings):
        _plan(settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={
                "action": "confirm",
                "digest": "0" * 64,
                "reason": "provider down",
                "acknowledged": True,
            },
        )
        assert response.status_code == 409
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_AVATAR

    def test_confirming_is_idempotent(self, client, session, settings):
        _plan(settings)
        prep = _json(
            client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json={"action": "prepare"}
            )
        )["override"]
        body = {
            "action": "confirm",
            "digest": prep["digest"],
            "reason": "provider outage before the broadcast",
            "acknowledged": True,
        }
        first = client.post(f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json=body)
        second = client.post(f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        assert (
            session.query(AvatarJobAudit)
            .filter(AvatarJobAudit.action == AvatarAuditAction.VOICE_OVER_OVERRIDE.value)
            .count()
            == 1
        )

    def _confirm(self, client, settings):
        prep = _json(
            client.post(
                f"/api/episodes/{EPISODE_ID}/avatar/voice-over", json={"action": "prepare"}
            )
        )["override"]
        return client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={
                "action": "confirm",
                "digest": prep["digest"],
                "reason": "provider outage before the broadcast",
                "acknowledged": True,
            },
        )

    def test_it_applies_to_one_episode_only(self, client, session, settings):
        _plan(settings)
        session.add(
            Episode(
                episode_id="ep_other",
                source="local_recorder",
                title="Other",
                url="/mnt/rec/y.mp4",
                status=EpisodeStatus.ANCHOR_GENERATED,
                pipeline_version=2,
                content_profile="tagesschau_tr",
            )
        )
        session.commit()

        self._confirm(client, settings)

        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_VOICE_OVER
        assert presentation_mode(session, "ep_other") == PRESENTATION_AVATAR

    def test_it_invalidates_the_anchor_approval(self, client, session, settings):
        _plan(settings)
        session.add(
            ReviewTask(
                episode_id=EPISODE_ID,
                stage="anchor",
                status=ReviewStatus.APPROVED.value,
                artifact_hash="digest-1",
            )
        )
        session.commit()

        self._confirm(client, settings)

        session.expire_all()
        task = session.query(ReviewTask).filter(ReviewTask.stage == "anchor").one()
        assert task.status == ReviewStatus.SUPERSEDED.value

    def test_it_marks_the_render_stale_without_touching_the_speech(
        self, client, session, settings
    ):
        outputs = _plan(settings)
        (outputs / "render").mkdir(parents=True, exist_ok=True)
        (outputs / "render" / "draft.mp4").write_bytes(b"old")
        tts = outputs / "tts" / "parts"
        tts.mkdir(parents=True, exist_ok=True)
        (tts / "ch_01_p00.mp3").write_bytes(b"speech")

        self._confirm(client, settings)

        assert (outputs / "render" / ".stale").exists()
        assert (tts / "ch_01_p00.mp3").read_bytes() == b"speech"

    def test_unresolved_costs_survive_the_override(self, client, session, settings):
        _plan(settings)
        job = _reserve(
            session,
            "sc_001",
            "ch_01",
            status=AvatarJobStatus.RECONCILE_REQUIRED.value,
            cost=0.75,
        )
        self._confirm(client, settings)
        session.expire_all()
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert float(job.cost_usd) == pytest.approx(0.75)

    def test_no_provider_job_is_cancelled_or_started(self, client, session, settings):
        _plan(settings)
        _reserve(session, "sc_001", "ch_01", status=AvatarJobStatus.SUBMITTED.value)
        before = [(j.id, j.status) for j in session.query(AvatarJob).all()]

        self._confirm(client, settings)

        session.expire_all()
        assert [(j.id, j.status) for j in session.query(AvatarJob).all()] == before

    def test_further_anchor_submits_are_blocked(self, client, session, settings):
        from btcedu.core.anchor_generator import _refuse_if_overridden
        from btcedu.services.errors import PipelineError

        _plan(settings)
        self._confirm(client, settings)

        with pytest.raises(PipelineError, match="voice-over override"):
            _refuse_if_overridden(session, EPISODE_ID)

    def test_the_dashboard_says_so_permanently(self, client, session, settings):
        _plan(settings)
        self._confirm(client, settings)
        data = _json(client.get(f"/api/episodes/{EPISODE_ID}/avatar"))
        assert data["presentation"]["mode"] == PRESENTATION_VOICE_OVER
        assert data["presentation"]["voice_over_override"]["reason"]
        assert "Voice-over" in data["presentation"]["label"]

    def test_it_can_be_withdrawn_before_publication(self, client, session, settings):
        _plan(settings)
        self._confirm(client, settings)

        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "revoke", "reason": "the provider recovered"},
        )
        assert response.status_code == 200
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_AVATAR
        # The decision itself is still in the history.
        assert (
            session.query(AvatarJobAudit)
            .filter(AvatarJobAudit.action == AvatarAuditAction.VOICE_OVER_OVERRIDE.value)
            .count()
            == 1
        )

    def test_withdrawing_needs_a_reason(self, client, session, settings):
        _plan(settings)
        self._confirm(client, settings)
        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "revoke", "reason": ""},
        )
        assert response.status_code == 400
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_VOICE_OVER

    def test_a_published_override_stays_in_the_history(
        self, client, session, settings, episode
    ):
        _plan(settings)
        self._confirm(client, settings)
        episode.status = EpisodeStatus.PUBLISHED
        session.commit()

        response = client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "revoke", "reason": "too late"},
        )
        assert response.status_code == 422
        assert presentation_mode(session, EPISODE_ID) == PRESENTATION_VOICE_OVER

    def test_withdrawing_starts_no_new_provider_job(self, client, session, settings):
        _plan(settings)
        _reserve(
            session, "sc_001", "ch_01", status=AvatarJobStatus.RECONCILE_REQUIRED.value
        )
        self._confirm(client, settings)
        before = [(j.id, j.status) for j in session.query(AvatarJob).all()]

        client.post(
            f"/api/episodes/{EPISODE_ID}/avatar/voice-over",
            json={"action": "revoke", "reason": "the provider recovered"},
        )

        session.expire_all()
        assert [(j.id, j.status) for j in session.query(AvatarJob).all()] == before

    def test_an_unknown_episode_is_a_404(self, client):
        assert (
            client.post(
                "/api/episodes/nope/avatar/voice-over", json={"action": "prepare"}
            ).status_code
            == 404
        )

    def test_the_override_record_carries_no_secret(self, client, session, settings):
        _plan(settings)
        self._confirm(client, settings)
        body = client.get(f"/api/episodes/{EPISODE_ID}/avatar").get_data(as_text=True)
        assert "super-secret-key" not in body


# ---------------------------------------------------------------------------
# The renderer's half of the override
# ---------------------------------------------------------------------------


class TestTheFallbackRender:
    def test_the_presenter_is_not_composited_under_an_override(
        self, session, settings, episode
    ):
        from btcedu.core.scene_renderer import (
            is_studio_scene,
            load_scene_context,
            shows_presenter,
        )

        outputs = _plan(settings)
        (outputs / "anchor").mkdir(parents=True, exist_ok=True)
        (outputs / "anchor" / "manifest.json").write_text(
            json.dumps({"schema_version": "2.0", "scenes": []}), encoding="utf-8"
        )

        from btcedu.core.anchor_fallback import record_voice_over_override

        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="provider outage"
        )

        ctx = load_scene_context(outputs, settings, episode, session=session)
        assert ctx is not None
        assert ctx.presentation_mode == PRESENTATION_VOICE_OVER
        anchor_scene = next(s for s in ctx.scenes if s.speaker_role == ROLE_ANCHOR)
        # The scene is still a studio scene in the plan; it is simply not shot
        # as one. The plan is never rewritten by an operational decision.
        assert is_studio_scene(anchor_scene) is True
        assert shows_presenter(ctx, anchor_scene) is False

    def test_the_normal_path_still_shows_her(self, session, settings, episode):
        from btcedu.core.scene_renderer import load_scene_context, shows_presenter

        outputs = _plan(settings)
        ctx = load_scene_context(outputs, settings, episode, session=session)
        assert ctx.presentation_mode == PRESENTATION_AVATAR
        anchor_scene = next(s for s in ctx.scenes if s.speaker_role == ROLE_ANCHOR)
        assert shows_presenter(ctx, anchor_scene) is True

    def test_the_manifest_names_the_production_mode(self, session, settings, episode):
        from btcedu.core.anchor_fallback import record_voice_over_override
        from btcedu.core.scene_renderer import load_scene_context, scene_manifest_block

        outputs = _plan(settings)
        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="provider outage"
        )
        ctx = load_scene_context(outputs, settings, episode, session=session)
        block = scene_manifest_block(ctx, [])
        assert block["presentation_mode"] == PRESENTATION_VOICE_OVER

    def test_the_scene_hash_changes_with_the_presentation(
        self, session, settings, episode
    ):
        """A change of presentation must re-cut every shot.

        Without the mode in the hash, an episode that already rendered with the
        presenter would reuse those shots and broadcast her anyway.
        """
        from btcedu.core.anchor_fallback import record_voice_over_override
        from btcedu.core.scene_renderer import load_scene_context, scene_content_hash

        outputs = _plan(settings)
        ctx = load_scene_context(outputs, settings, episode, session=session)
        scene = ctx.scenes[0]
        kwargs = {
            "avatar_clip_hash": "clip",
            "display_media_hash": "media",
            "audio_hash": "audio",
            "duration": 6.0,
            "overlay_fingerprint": {},
        }
        before = scene_content_hash(ctx, scene, **kwargs)

        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="provider outage"
        )
        after_ctx = load_scene_context(outputs, settings, episode, session=session)
        after = scene_content_hash(after_ctx, scene, **kwargs)

        assert before != after

    def test_a_broken_clip_does_not_block_the_fallback(self, session, settings, episode):
        """The clips are unusable — that is why the override exists."""
        from btcedu.core.anchor_fallback import record_voice_over_override
        from btcedu.core.scene_renderer import load_scene_context

        outputs = _plan(settings)
        anchor = outputs / "anchor"
        anchor.mkdir(parents=True, exist_ok=True)
        (anchor / "sc_001.mp4").write_bytes(b"corrupted")
        (anchor / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "scenes": [
                        {
                            "scene_id": "sc_001",
                            "video_path": "anchor/sc_001.mp4",
                            "file_sha256": "0" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="clips are corrupt"
        )
        ctx = load_scene_context(outputs, settings, episode, session=session)
        assert ctx is not None
        assert ctx.anchor_clips == {}

    def test_the_same_broken_clip_still_blocks_the_avatar_path(
        self, session, settings, episode
    ):
        from btcedu.core.avatar_integrity import ClipIntegrityError
        from btcedu.core.scene_renderer import load_scene_context

        outputs = _plan(settings)
        anchor = outputs / "anchor"
        anchor.mkdir(parents=True, exist_ok=True)
        (anchor / "sc_001.mp4").write_bytes(b"corrupted")
        (anchor / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "scenes": [
                        {
                            "scene_id": "sc_001",
                            "video_path": "anchor/sc_001.mp4",
                            "file_sha256": "0" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ClipIntegrityError):
            load_scene_context(outputs, settings, episode, session=session)

    def test_the_remote_job_carries_the_decision(self, session, settings, episode):
        from btcedu.core.anchor_fallback import record_voice_over_override
        from btcedu.core.remote_render import scene_job_requirements

        outputs = _plan(settings)
        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="provider outage"
        )
        job = scene_job_requirements(outputs, settings, episode, session=session)

        assert job["presentation_mode"] == PRESENTATION_VOICE_OVER
        assert job["clip_digests"] == {}
        assert not [rel for rel in job["episode_files"] if rel.startswith("anchor/")]

    def test_without_an_override_the_remote_job_is_unchanged(
        self, session, settings, episode
    ):
        from btcedu.core.remote_render import scene_job_requirements

        outputs = _plan(settings)
        job = scene_job_requirements(outputs, settings, episode, session=session)
        assert job["presentation_mode"] == PRESENTATION_AVATAR

    def test_a_session_less_caller_never_assumes_an_override(self, settings, episode):
        """Fail closed: an unknown decision is the normal avatar path."""
        from btcedu.core.scene_renderer import _presentation_mode

        assert _presentation_mode(None, None) == PRESENTATION_AVATAR

    def test_the_override_is_readable_after_a_restart(self, factory, settings):
        """Persisted, not held in a request. A crash must not undo it."""
        from btcedu.core.anchor_fallback import record_voice_over_override

        first = factory()
        first.add(
            Episode(
                episode_id="ep_restart",
                source="local_recorder",
                title="T",
                url="/x",
                status=EpisodeStatus.ANCHOR_GENERATED,
                pipeline_version=2,
                content_profile="tagesschau_tr",
            )
        )
        first.commit()
        record_voice_over_override(
            first, "ep_restart", operator_ref="dashboard", reason="outage"
        )
        first.close()

        second = factory()
        assert active_voice_over_override(second, "ep_restart") is not None
        second.close()

    def test_a_missing_fallback_medium_blocks_the_render(self, session, settings, episode):
        """Fail closed: no medium to show means no shot, not a black frame."""
        from btcedu.core.anchor_fallback import record_voice_over_override
        from btcedu.core.scene_renderer import (
            SceneRenderError,
            _resolve_display_media,
            load_scene_context,
        )

        outputs = _plan(settings)
        record_voice_over_override(
            session, EPISODE_ID, operator_ref="dashboard", reason="provider outage"
        )
        ctx = load_scene_context(outputs, settings, episode, session=session)
        anchor_scene = next(s for s in ctx.scenes if s.speaker_role == ROLE_ANCHOR)

        # No chapter image, no studio fallback plate: nothing can fill the frame.
        assert ctx.studio is None
        with pytest.raises(SceneRenderError):
            _resolve_display_media(ctx, anchor_scene)

    def test_the_remote_runner_reproduces_the_decision(self, tmp_path):
        """The runner's database is seconds old and knows nothing by itself."""
        import importlib.util

        from btcedu.core.anchor_fallback import active_voice_over_override
        from btcedu.models.avatar_job_audit import AvatarJobAudit  # noqa: F401

        spec = importlib.util.spec_from_file_location(
            "render_job_under_test", Path("scripts/render_job.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        runner_session = sessionmaker(bind=engine)()

        module._seed_presentation_mode(
            runner_session,
            {"scene_render": {"presentation_mode": PRESENTATION_VOICE_OVER}},
            "ep_remote",
        )
        assert active_voice_over_override(runner_session, "ep_remote") is not None

        module._seed_presentation_mode(
            runner_session,
            {"scene_render": {"presentation_mode": PRESENTATION_AVATAR}},
            "ep_avatar",
        )
        assert active_voice_over_override(runner_session, "ep_avatar") is None
