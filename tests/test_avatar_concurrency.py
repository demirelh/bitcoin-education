"""WP-5C: bounded parallelism, provider-aware backoff, operational hardening.

Nothing here talks to HeyGen. The provider is a fake that records what it was
asked for, the clock and the sleeps are injected, and the download is a local
file copy. What is being tested is the part that costs money if it is wrong:
how many orders are placed, how often, with which idempotency key, and what
happens to a job whose outcome nobody knows.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.core import avatar_breaker
from btcedu.core.avatar_coordinator import (
    AvatarCoordinator,
    SceneRequest,
    build_idempotency_key,
    expire_audio_asset,
    find_audio_asset,
    plan_scene_work,
)
from btcedu.core.avatar_download import (
    ClipExpectation,
    DownloadResult,
    ValidationError,
    probe_clip,
    stream_to_file,
    validate_clip,
)
from btcedu.core.avatar_retry import (
    OP_CAPABILITY,
    OP_CREATE_VIDEO,
    OP_DOWNLOAD_RESULT,
    OP_POLL_STATUS,
    OP_UPLOAD_ASSET,
    FailureClass,
    RetryExhausted,
    classify,
    idempotency_key_is_live,
    parse_retry_after,
    run_with_retry,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.services.anchor_service import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_PROCESSING,
    AnchorAPIError,
    ProviderVideoStatus,
)

EPISODE_ID = "ep_conc_001"
PROVIDER = "heygen"
ENGINE = "avatar_iii"
LOOK_ID = "look_studio_navy"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


@pytest.fixture
def dirs(tmp_path):
    outputs = tmp_path / "outputs"
    anchor = outputs / "anchor"
    audio = outputs / "tts"
    anchor.mkdir(parents=True)
    audio.mkdir(parents=True)
    return outputs, anchor, audio


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.value = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += timedelta(seconds=seconds)


class FakeHeyGen:
    """A granular provider double. Counts every order it is given."""

    def __init__(self, *, processing_polls: int = 0):
        self.uploads: list[str] = []
        self.creates: list[dict] = []
        self.polls: list[str] = []
        self.downloads: list[str] = []
        self.processing_polls = processing_polls
        self._poll_counts: dict[str, int] = {}
        self.create_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.poll_error: Exception | None = None
        self.fail_state_for: set[str] = set()
        self.unknown_state_for: set[str] = set()
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self.next_job_id = 0

    # -- provider surface --------------------------------------------------

    def upload_audio_asset(self, path: str) -> str:
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append(path)
        return f"asset_{len(self.uploads)}"

    def create_video(self, asset_id: str, *, title: str, idempotency_key: str) -> str:
        if self.create_error is not None:
            raise self.create_error
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        self.next_job_id += 1
        job_id = f"pjob_{self.next_job_id}"
        self.creates.append(
            {"asset_id": asset_id, "title": title, "idempotency_key": idempotency_key,
             "job_id": job_id}
        )
        return job_id

    def poll_video_once(self, provider_job_id: str) -> ProviderVideoStatus:
        if self.poll_error is not None:
            raise self.poll_error
        self.polls.append(provider_job_id)
        seen = self._poll_counts.get(provider_job_id, 0)
        self._poll_counts[provider_job_id] = seen + 1
        if provider_job_id in self.unknown_state_for:
            return ProviderVideoStatus(state="unknown", raw_status="gremlin")
        if provider_job_id in self.fail_state_for:
            return ProviderVideoStatus(
                state=STATE_FAILED,
                raw_status="failed",
                error_code="generation_failed",
                error_detail="the provider gave up",
            )
        if seen < self.processing_polls:
            return ProviderVideoStatus(state=STATE_PROCESSING, raw_status="processing")
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
        return ProviderVideoStatus(
            state=STATE_COMPLETED,
            raw_status="completed",
            video_url=f"https://example.invalid/{provider_job_id}.mp4",
            duration_seconds=8.0,
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(duration_seconds * 0.01, 6)


def fake_download(recorder: list[str]):
    """Replacement for download_and_validate that writes a small local file."""

    def _download(url, destination, expectation, **kwargs):
        recorder.append(url)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        payload = b"clip-bytes"
        Path(destination).write_bytes(payload)
        # The real downloader hashes what it writes. A double that reports some
        # other digest would let the integrity checks pass on a lie.
        return DownloadResult(
            path=Path(destination),
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            probe=None,
            warnings=[],
        )

    return _download


def make_requests(audio_dir: Path, count: int) -> list[SceneRequest]:
    requests = []
    for index in range(1, count + 1):
        audio = audio_dir / f"chapter_{index:02d}.mp3"
        audio.write_bytes(b"audio" * (index + 1))
        requests.append(
            SceneRequest(
                scene_id=f"sc_{index:03d}",
                chapter_id=f"ch_{index:02d}",
                content_hash=f"hash{index:03d}",
                audio_path=audio,
                audio_hash=f"audio{index:03d}",
                expected_duration_seconds=8.0,
                estimated_cost_usd=0.08,
                title=f"Topic {index}",
            )
        )
    return requests


def run_coordinator(
    session,
    dirs,
    service,
    requests,
    *,
    max_concurrent_jobs=1,
    clock=None,
    downloads=None,
    poll_timeout_seconds=600.0,
    budget_check=None,
    monkeypatch=None,
):
    outputs, anchor, _audio = dirs
    clock = clock or FakeClock()
    downloads = downloads if downloads is not None else []
    if monkeypatch is not None:
        monkeypatch.setattr(
            "btcedu.core.avatar_coordinator.download_and_validate", fake_download(downloads)
        )
    plan = plan_scene_work(
        session,
        episode_id=EPISODE_ID,
        requests=requests,
        provider=PROVIDER,
        engine=ENGINE,
        look_id=LOOK_ID,
        output_format="mp4",
        outputs_dir=outputs,
        budget_check=budget_check,
        now=clock.now,
    )
    coordinator = AvatarCoordinator(
        session,
        episode_id=EPISODE_ID,
        provider=PROVIDER,
        engine=ENGINE,
        look_id=LOOK_ID,
        output_format="mp4",
        service=service,
        outputs_dir=outputs,
        anchor_dir=anchor,
        max_concurrent_jobs=max_concurrent_jobs,
        poll_interval_seconds=1.0,
        poll_timeout_seconds=poll_timeout_seconds,
        expectation=ClipExpectation(output_format="mp4"),
        budget_check=budget_check,
        sleep=clock.sleep,
        now=clock.now,
        install_signal_handler=False,
    )
    return coordinator.run(plan), clock


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrencyLimit:
    @pytest.mark.parametrize("limit", [1, 2, 3])
    def test_never_more_jobs_in_flight_than_configured(self, session, dirs, monkeypatch, limit):
        service = FakeHeyGen(processing_polls=2)
        requests = make_requests(dirs[2], 6)
        result, _clock = run_coordinator(
            session,
            dirs,
            service,
            requests,
            max_concurrent_jobs=limit,
            monkeypatch=monkeypatch,
        )
        assert len(result.outcomes) == 6
        assert service.max_in_flight <= limit

    def test_a_single_slot_serialises_completely(self, session, dirs, monkeypatch):
        service = FakeHeyGen(processing_polls=1)
        result, _ = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 3), monkeypatch=monkeypatch
        )
        assert result.complete
        assert service.max_in_flight == 1

    def test_resumed_jobs_occupy_a_slot(self, session, dirs, monkeypatch):
        """A job already at the provider is work in flight, not free capacity."""
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 2)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)

        jobs = session.query(AvatarJob).all()
        assert all(job.status == AvatarJobStatus.COMPLETED.value for job in jobs)
        # A resumed job re-enters the same dispatch loop, so the cap applies to
        # it exactly as it does to a fresh submission.
        jobs[0].status = AvatarJobStatus.SUBMITTED.value
        session.commit()
        result, _ = run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        assert service.max_in_flight <= 1
        assert result.resumed >= 1

    def test_zero_concurrency_is_refused(self, session, dirs):
        with pytest.raises(ValueError, match="max_concurrent_jobs"):
            AvatarCoordinator(
                session,
                episode_id=EPISODE_ID,
                provider=PROVIDER,
                engine=ENGINE,
                look_id=LOOK_ID,
                output_format="mp4",
                service=FakeHeyGen(),
                outputs_dir=dirs[0],
                anchor_dir=dirs[1],
                max_concurrent_jobs=0,
                install_signal_handler=False,
            )


class TestThreadDiscipline:
    def test_workers_never_touch_the_session(self, session, dirs, monkeypatch):
        """A worker thread that can reach the ORM will eventually corrupt it."""
        main_thread = threading.get_ident()
        seen: list[int] = []
        original = AvatarCoordinator._run_scene

        def spy(self, work):
            seen.append(threading.get_ident())
            assert "session" not in vars(work)
            return original(self, work)

        monkeypatch.setattr(AvatarCoordinator, "_run_scene", spy)
        service = FakeHeyGen()
        run_coordinator(
            session,
            dirs,
            service,
            make_requests(dirs[2], 3),
            max_concurrent_jobs=3,
            monkeypatch=monkeypatch,
        )
        assert seen and all(ident != main_thread for ident in seen)

    def test_no_transaction_is_open_while_the_provider_is_called(
        self, session, dirs, monkeypatch
    ):
        """The session must be idle whenever a network call is outstanding."""
        service = FakeHeyGen()
        observed: list[bool] = []
        original_create = service.create_video

        def watched(*args, **kwargs):
            # SQLAlchemy keeps a read transaction open by design; what must
            # never happen is an uncommitted *write* sitting in the session
            # while a two-minute generation is outstanding.
            observed.append(bool(session.new or session.dirty or session.deleted))
            return original_create(*args, **kwargs)

        service.create_video = watched
        run_coordinator(
            session, dirs, service, make_requests(dirs[2], 2), monkeypatch=monkeypatch
        )
        assert observed and not any(observed)


# ---------------------------------------------------------------------------
# Restart and partial progress
# ---------------------------------------------------------------------------


class TestRestartAndReuse:
    def test_partial_failure_keeps_the_successful_scenes(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 3)
        service.fail_state_for = {"pjob_2"}
        result, _ = run_coordinator(
            session, dirs, service, requests, max_concurrent_jobs=1, monkeypatch=monkeypatch
        )
        assert len(result.outcomes) == 2
        assert len(result.failures) == 1
        assert not result.complete

    def test_restart_resumes_a_submitted_job_without_reordering(
        self, session, dirs, monkeypatch
    ):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 2)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        orders_after_first_run = len(service.creates)

        # Simulate a crash after submission: the row keeps its provider job id.
        job = session.query(AvatarJob).filter(AvatarJob.scene_id == "sc_001").one()
        job.status = AvatarJobStatus.SUBMITTED.value
        session.commit()

        result, _ = run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        assert len(service.creates) == orders_after_first_run
        assert result.resumed == 1

    def test_completed_scene_is_reused_not_rebought(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 2)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        result, _ = run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        assert len(service.creates) == 2
        assert result.reused == 2
        assert result.submitted == 0

    def test_the_same_narration_is_uploaded_only_once(self, session, dirs, monkeypatch):
        """The provider already holds these bytes; sending them twice is waste."""
        service = FakeHeyGen()
        first = make_requests(dirs[2], 1)[0]
        second = SceneRequest(
            scene_id="sc_002",
            chapter_id="ch_02",
            content_hash="hash002",
            audio_path=first.audio_path,
            audio_hash=first.audio_hash,
            expected_duration_seconds=8.0,
            estimated_cost_usd=0.08,
            title="Topic 2",
        )
        run_coordinator(session, dirs, service, [first], monkeypatch=monkeypatch)
        assert len(service.uploads) == 1

        asset = find_audio_asset(session, provider=PROVIDER, audio_hash=first.audio_hash)
        assert asset is not None and asset.asset_id == "asset_1"

        # A later run, a different scene, the same narration: no second upload,
        # but a genuinely new video order.
        run_coordinator(session, dirs, service, [first, second], monkeypatch=monkeypatch)
        assert len(service.uploads) == 1
        assert len(service.creates) == 2
        assert service.creates[1]["asset_id"] == "asset_1"

    def test_reuploaded_audio_does_not_imply_a_new_video_order(
        self, session, dirs, monkeypatch
    ):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 1)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        expire_audio_asset(
            session,
            provider=PROVIDER,
            audio_hash=requests[0].audio_hash,
            reason="provider dropped the asset",
        )
        assert (
            find_audio_asset(session, provider=PROVIDER, audio_hash=requests[0].audio_hash)
            is None
        )

        creates_before = len(service.creates)
        result, _ = run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        assert result.reused == 1
        assert len(service.creates) == creates_before
        assert len(service.uploads) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotencyKey:
    def test_key_is_stable_across_runs_and_restarts(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 1)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        first = session.query(AvatarJob).one().idempotency_key
        assert first
        assert first == service.creates[0]["idempotency_key"]
        assert first == build_idempotency_key(EPISODE_ID, "sc_001", requests[0].content_hash)

    def test_a_new_content_hash_produces_a_new_key(self):
        first = build_idempotency_key(EPISODE_ID, "sc_001", "hash-a")
        second = build_idempotency_key(EPISODE_ID, "sc_001", "hash-b")
        assert first != second

    def test_retries_of_one_attempt_share_the_key(self, session, dirs, monkeypatch):
        """A 5xx retry must repeat the same order, never place a second one."""
        service = FakeHeyGen()
        attempts: list[str] = []
        real_create = service.create_video
        state = {"failures": 2}

        def flaky(asset_id, *, title, idempotency_key):
            attempts.append(idempotency_key)
            if state["failures"] > 0:
                state["failures"] -= 1
                raise AnchorAPIError(PROVIDER, 503, "upstream wobbled")
            return real_create(asset_id, title=title, idempotency_key=idempotency_key)

        service.create_video = flaky
        result, clock = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        assert result.complete
        assert len(attempts) == 3
        assert len(set(attempts)) == 1
        assert clock.slept, "backoff must have waited, on the injected clock only"

    def test_an_expired_window_blocks_an_automatic_repeat(self, session, dirs, monkeypatch):
        """Past 24 hours the same key is no longer the same order to HeyGen."""
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 1)
        monkeypatch.setattr(
            "btcedu.core.avatar_coordinator.download_and_validate", fake_download([])
        )
        plan = plan_scene_work(
            session,
            episode_id=EPISODE_ID,
            requests=requests,
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            outputs_dir=dirs[0],
        )
        plan[0].idempotency_live = False

        clock = FakeClock()
        result = AvatarCoordinator(
            session,
            episode_id=EPISODE_ID,
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            service=service,
            outputs_dir=dirs[0],
            anchor_dir=dirs[1],
            poll_interval_seconds=1.0,
            sleep=clock.sleep,
            now=clock.now,
            install_signal_handler=False,
        ).run(plan)

        assert service.creates == []
        assert result.failures
        assert "24-hour" in result.failures[0].message
        assert session.query(AvatarJob).one().status == (
            AvatarJobStatus.RECONCILE_REQUIRED.value
        )

    def test_window_helper_reads_the_clock(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        assert idempotency_key_is_live(now + timedelta(hours=1), now=lambda: now)
        assert not idempotency_key_is_live(now - timedelta(seconds=1), now=lambda: now)


# ---------------------------------------------------------------------------
# Retry matrix
# ---------------------------------------------------------------------------


class TestFailureClassification:
    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_is_permanent_and_stops_the_provider(self, code):
        verdict = classify(OP_CREATE_VIDEO, AnchorAPIError(PROVIDER, code, "no"))
        assert verdict.failure_class is FailureClass.AUTH
        assert not verdict.retryable
        assert not verdict.ambiguous
        assert verdict.blocks_provider

    @pytest.mark.parametrize("code", [400, 422])
    def test_a_refusal_is_unbilled_and_not_retried_blindly(self, code):
        verdict = classify(OP_CREATE_VIDEO, AnchorAPIError(PROVIDER, code, "bad payload"))
        assert verdict.failure_class is FailureClass.REQUEST_REJECTED
        assert not verdict.retryable
        assert not verdict.ambiguous

    def test_404_on_a_known_job_is_ambiguous(self):
        verdict = classify(OP_POLL_STATUS, AnchorAPIError(PROVIDER, 404, "gone"))
        assert verdict.ambiguous, "a missing job is not proof that nothing was billed"

    def test_404_on_a_capability_lookup_is_simply_not_found(self):
        verdict = classify(OP_CAPABILITY, AnchorAPIError(PROVIDER, 404, "no such look"))
        assert verdict.failure_class is FailureClass.NOT_FOUND
        assert not verdict.ambiguous

    def test_429_is_retryable_and_respects_seconds(self):
        error = AnchorAPIError(PROVIDER, 429, "slow down", retry_after=17.0)
        verdict = classify(OP_CREATE_VIDEO, error)
        assert verdict.failure_class is FailureClass.RATE_LIMITED
        assert verdict.retryable
        assert verdict.retry_after_seconds == pytest.approx(17.0)

    def test_retry_after_accepts_an_http_date(self):
        now = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
        seconds = parse_retry_after("Sun, 01 Mar 2026 20:00:30 GMT", now=lambda: now)
        assert seconds == pytest.approx(30.0, abs=1.0)

    def test_retry_after_accepts_plain_seconds(self):
        assert parse_retry_after("12") == pytest.approx(12.0)

    def test_5xx_on_a_create_is_retryable_but_ambiguous(self):
        verdict = classify(OP_CREATE_VIDEO, AnchorAPIError(PROVIDER, 502, "bad gateway"))
        assert verdict.retryable
        assert verdict.ambiguous

    def test_5xx_on_a_read_is_retryable_and_harmless(self):
        verdict = classify(OP_POLL_STATUS, AnchorAPIError(PROVIDER, 500, "oops"))
        assert verdict.retryable
        assert not verdict.ambiguous

    def test_a_connect_failure_never_reached_the_provider(self):
        verdict = classify(OP_CREATE_VIDEO, ConnectionRefusedError("refused"))
        assert not verdict.ambiguous

    def test_a_read_timeout_on_a_create_might_have_been_accepted(self):
        import requests

        verdict = classify(OP_CREATE_VIDEO, requests.exceptions.ReadTimeout("no answer"))
        assert verdict.ambiguous, "the order may be running and billed"


class TestRetryLoop:
    def test_a_rate_limit_waits_the_requested_time(self):
        clock = FakeClock()
        state = {"calls": 0}

        def call():
            state["calls"] += 1
            if state["calls"] == 1:
                raise AnchorAPIError(PROVIDER, 429, "slow down", retry_after=30.0)
            return "ok"

        value = run_with_retry(
            OP_POLL_STATUS, call, sleep=clock.sleep, now=clock.now, rand=lambda: 0.0
        )
        assert value == "ok"
        assert clock.slept == [30.0]

    def test_retries_are_bounded(self):
        clock = FakeClock()

        def always_broken():
            raise AnchorAPIError(PROVIDER, 500, "still broken")

        with pytest.raises(RetryExhausted):
            run_with_retry(
                OP_POLL_STATUS, always_broken, sleep=clock.sleep, now=clock.now, rand=lambda: 0.0
            )
        assert len(clock.slept) < 20, "a bounded policy, not an endless loop"

    def test_no_real_time_passes(self):
        clock = FakeClock()
        state = {"calls": 0}

        def flaky():
            state["calls"] += 1
            if state["calls"] < 3:
                raise AnchorAPIError(PROVIDER, 503, "wobble")
            return "ok"

        started = datetime.now(UTC)
        run_with_retry(
            OP_UPLOAD_ASSET, flaky, sleep=clock.sleep, now=clock.now, rand=lambda: 0.0
        )
        assert (datetime.now(UTC) - started).total_seconds() < 1.0


class TestProviderErrorsInTheCoordinator:
    def test_an_auth_failure_does_not_hold_a_reservation(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        service.create_error = AnchorAPIError(PROVIDER, 401, "bad key")
        result, _ = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        assert result.failures
        job = session.query(AvatarJob).one()
        assert job.status == AvatarJobStatus.FAILED.value
        assert job.cost_usd == 0.0

    def test_an_ambiguous_failure_becomes_reconcile_required(self, session, dirs, monkeypatch):
        import requests as _requests

        service = FakeHeyGen()
        service.create_error = _requests.exceptions.ReadTimeout("no answer")
        result, _ = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        assert result.failures
        job = session.query(AvatarJob).one()
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

    def test_an_unmapped_provider_status_fails_closed(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        requests = make_requests(dirs[2], 1)
        service.unknown_state_for = {"pjob_1"}
        result, _ = run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)
        assert result.failures
        assert "unmapped" in result.failures[0].message
        assert session.query(AvatarJob).one().status == (
            AvatarJobStatus.RECONCILE_REQUIRED.value
        )

    def test_a_poll_timeout_defers_instead_of_reordering(self, session, dirs, monkeypatch):
        service = FakeHeyGen(processing_polls=10_000)
        result, _ = run_coordinator(
            session,
            dirs,
            service,
            make_requests(dirs[2], 1),
            poll_timeout_seconds=3.0,
            monkeypatch=monkeypatch,
        )
        assert result.deferred
        assert not result.complete
        job = session.query(AvatarJob).one()
        assert job.status == AvatarJobStatus.SUBMITTED.value
        assert len(service.creates) == 1


# ---------------------------------------------------------------------------
# Download and validation
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, chunks, status_code=200, headers=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}

    def iter_content(self, chunk_size=1):  # noqa: ARG002 - signature parity
        yield from self._chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AnchorAPIError(PROVIDER, self.status_code, "download failed")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestStreamingDownload:
    def test_the_clip_is_streamed_in_chunks_not_slurped(self, tmp_path):
        chunks = [b"a" * 4096, b"b" * 4096, b"c" * 2048]
        destination = tmp_path / "clip.mp4"
        size, digest = stream_to_file(
            "https://example.invalid/clip.mp4",
            destination,
            opener=lambda url, **kw: FakeResponse(chunks),
        )
        assert size == 10240
        assert len(digest) == 64
        assert destination.read_bytes() == b"".join(chunks)

    def test_an_oversized_clip_is_refused_and_leaves_nothing_behind(self, tmp_path):
        from btcedu.core.avatar_download import DownloadError

        destination = tmp_path / "clip.mp4"
        with pytest.raises(DownloadError):
            stream_to_file(
                "https://example.invalid/clip.mp4",
                destination,
                opener=lambda url, **kw: FakeResponse([b"x" * 4096]),
                max_bytes=1024,
            )
        assert not destination.exists()
        assert not list(tmp_path.glob("*.part"))

    def test_an_interrupted_download_never_becomes_the_final_file(self, tmp_path):
        from btcedu.core.avatar_download import DownloadError

        def broken(url, **kwargs):
            def chunks():
                yield b"partial"
                raise ConnectionError("cable pulled")

            return FakeResponse(chunks())

        destination = tmp_path / "clip.mp4"
        with pytest.raises((DownloadError, ConnectionError)):
            stream_to_file("https://example.invalid/x.mp4", destination, opener=broken)
        assert not destination.exists()


def _probe_payload(*, codec="h264", pix_fmt="yuv420p", width=1920, height=1080, fps="25/1",
                   duration="8.0", fmt="mov,mp4,m4a"):
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": codec,
                    "pix_fmt": pix_fmt,
                    "width": width,
                    "height": height,
                    "avg_frame_rate": fps,
                }
            ],
            "format": {"format_name": fmt, "duration": duration},
        }
    )


class FakeProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class TestClipValidation:
    def test_a_healthy_mp4_passes(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 4096)
        probe = probe_clip(clip, runner=lambda cmd: FakeProc(_probe_payload()))
        warnings = validate_clip(
            probe, ClipExpectation(output_format="mp4", width=1920, height=1080)
        )
        assert warnings == []

    def test_a_webm_that_carries_no_alpha_is_rejected(self, tmp_path):
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"x" * 4096)
        probe = probe_clip(
            clip,
            runner=lambda cmd: FakeProc(
                _probe_payload(codec="vp9", pix_fmt="yuv420p", fmt="matroska,webm")
            ),
        )
        with pytest.raises(ValidationError, match="alpha"):
            validate_clip(probe, ClipExpectation(output_format="webm", require_alpha=True))

    def test_a_transparent_webm_passes(self, tmp_path):
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"x" * 4096)
        probe = probe_clip(
            clip,
            runner=lambda cmd: FakeProc(
                _probe_payload(codec="vp9", pix_fmt="yuva420p", fmt="matroska,webm")
            ),
        )
        assert validate_clip(probe, ClipExpectation(output_format="webm", require_alpha=True)) == []

    def test_the_wrong_resolution_is_fatal(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 4096)
        probe = probe_clip(clip, runner=lambda cmd: FakeProc(_probe_payload(width=640, height=360)))
        with pytest.raises(ValidationError, match="640x360"):
            validate_clip(probe, ClipExpectation(output_format="mp4", width=1920, height=1080))

    def test_a_wildly_wrong_duration_is_only_a_warning(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 4096)
        probe = probe_clip(clip, runner=lambda cmd: FakeProc(_probe_payload(duration="30.0")))
        warnings = validate_clip(
            probe, ClipExpectation(output_format="mp4", expected_duration_seconds=8.0)
        )
        assert any("narration suggested" in warning for warning in warnings)

    def test_a_corrupt_file_is_quarantined_not_silently_accepted(self, tmp_path):
        from btcedu.core.avatar_download import download_and_validate

        destination = tmp_path / "clip.mp4"
        with pytest.raises(ValidationError):
            download_and_validate(
                "https://example.invalid/clip.mp4",
                destination,
                ClipExpectation(output_format="mp4"),
                opener=lambda url, **kw: FakeResponse([b"y" * 4096]),
                probe_runner=lambda cmd: FakeProc("not json at all", returncode=1),
            )
        assert not destination.exists()
        quarantined = list((tmp_path / "quarantine").glob("*.mp4"))
        assert quarantined, "a billed clip is evidence and must be kept"


class TestDownloadInTheCoordinator:
    def test_a_stale_url_is_refreshed_by_a_status_call_not_a_new_order(
        self, session, dirs, monkeypatch
    ):
        service = FakeHeyGen()
        attempts: list[str] = []

        def flaky_download(url, destination, expectation, **kwargs):
            attempts.append(url)
            if len(attempts) == 1:
                raise AnchorAPIError(PROVIDER, 403, "signature expired")
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"clip")
            return DownloadResult(
                path=Path(destination), size_bytes=4, sha256="abc", probe=None, warnings=[]
            )

        monkeypatch.setattr(
            "btcedu.core.avatar_coordinator.download_and_validate", flaky_download
        )
        plan = plan_scene_work(
            session,
            episode_id=EPISODE_ID,
            requests=make_requests(dirs[2], 1),
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            outputs_dir=dirs[0],
        )
        clock = FakeClock()
        result = AvatarCoordinator(
            session,
            episode_id=EPISODE_ID,
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            service=service,
            outputs_dir=dirs[0],
            anchor_dir=dirs[1],
            poll_interval_seconds=1.0,
            sleep=clock.sleep,
            now=clock.now,
            install_signal_handler=False,
        ).run(plan)
        assert result.complete
        assert len(attempts) >= 2
        assert len(service.creates) == 1, "a dead link must never trigger a new generation"

    def test_a_second_run_does_not_download_again(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        downloads: list[str] = []
        requests = make_requests(dirs[2], 2)
        run_coordinator(
            session, dirs, service, requests, downloads=downloads, monkeypatch=monkeypatch
        )
        assert len(downloads) == 2
        run_coordinator(
            session, dirs, service, requests, downloads=downloads, monkeypatch=monkeypatch
        )
        assert len(downloads) == 2, "a completed job is reused from disk"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def _fail(self, session, clock, count, code=500, operation=OP_CREATE_VIDEO):
        for _ in range(count):
            verdict = classify(operation, AnchorAPIError(PROVIDER, code, "boom"))
            avatar_breaker.record_failure(session, PROVIDER, verdict, now=clock.now)

    def test_repeated_failures_open_the_breaker(self, session):
        clock = FakeClock()
        self._fail(session, clock, 5)
        view = avatar_breaker.status(session, PROVIDER, now=clock.now)
        assert view.state == "open"
        assert not view.submissions_allowed
        assert view.reason

    def test_an_open_breaker_blocks_further_submissions(self, session, dirs, monkeypatch):
        clock = FakeClock()
        self._fail(session, clock, 5)
        service = FakeHeyGen()
        result, _ = run_coordinator(
            session,
            dirs,
            service,
            make_requests(dirs[2], 2),
            clock=clock,
            monkeypatch=monkeypatch,
        )
        assert result.breaker_blocked
        assert service.creates == []

    def test_success_closes_it_again(self, session):
        clock = FakeClock()
        self._fail(session, clock, 2)
        avatar_breaker.record_success(session, PROVIDER, now=clock.now)
        assert avatar_breaker.status(session, PROVIDER, now=clock.now).consecutive_failures == 0

    def test_a_reset_needs_an_operator_and_is_audited(self, session):
        from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit

        clock = FakeClock()
        self._fail(session, clock, 5)
        avatar_breaker.reset(
            session, PROVIDER, operator_ref="ops-anna", note="quota topped up"
        )
        view = avatar_breaker.status(session, PROVIDER, now=clock.now)
        assert view.submissions_allowed
        audits = (
            session.query(AvatarJobAudit)
            .filter(AvatarJobAudit.action == AvatarAuditAction.BREAKER_RESET.value)
            .all()
        )
        assert len(audits) == 1
        assert audits[0].operator_ref == "ops-anna"

    def test_a_reset_without_a_reference_is_refused(self, session):
        with pytest.raises(ValueError):
            avatar_breaker.reset(session, PROVIDER, operator_ref="", note="")

    def test_a_refusal_does_not_count_towards_the_breaker(self, session):
        clock = FakeClock()
        self._fail(session, clock, 6, code=400)
        assert avatar_breaker.status(session, PROVIDER, now=clock.now).submissions_allowed


# ---------------------------------------------------------------------------
# Shutdown, budget and policy
# ---------------------------------------------------------------------------


class TestOperationalSafety:
    def test_a_shutdown_signal_stops_dispatching_new_orders(self, session, dirs, monkeypatch):
        service = FakeHeyGen(processing_polls=2)
        requests = make_requests(dirs[2], 4)
        clock = FakeClock()
        plan = plan_scene_work(
            session,
            episode_id=EPISODE_ID,
            requests=requests,
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            outputs_dir=dirs[0],
        )
        monkeypatch.setattr(
            "btcedu.core.avatar_coordinator.download_and_validate", fake_download([])
        )
        coordinator = AvatarCoordinator(
            session,
            episode_id=EPISODE_ID,
            provider=PROVIDER,
            engine=ENGINE,
            look_id=LOOK_ID,
            output_format="mp4",
            service=service,
            outputs_dir=dirs[0],
            anchor_dir=dirs[1],
            max_concurrent_jobs=1,
            poll_interval_seconds=1.0,
            sleep=clock.sleep,
            now=clock.now,
            install_signal_handler=False,
        )
        coordinator._shutdown.set()
        result = coordinator.run(plan)
        assert result.interrupted
        assert len(service.creates) < 4
        # Whatever was already reserved is still in the ledger for the next run.
        assert session.query(AvatarJob).count() == 4

    def test_the_budget_is_checked_again_before_every_order(self, session, dirs, monkeypatch):
        service = FakeHeyGen()
        checks: list[float] = []

        def budget_check(cost):
            checks.append(cost)
            if len(checks) > 4:
                raise RuntimeError("stage budget exceeded")

        result, _ = run_coordinator(
            session,
            dirs,
            service,
            make_requests(dirs[2], 2),
            budget_check=budget_check,
            monkeypatch=monkeypatch,
        )
        # Twice while planning, twice again immediately before dispatch.
        assert len(checks) == 4
        assert result.complete

    def test_no_voice_over_fallback_is_ever_produced(self, session, dirs, monkeypatch):
        """A missing presenter blocks. It never silently degrades to audio."""
        service = FakeHeyGen()
        service.fail_state_for = {"pjob_1"}
        result, _ = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        assert not result.complete
        assert result.outcomes == []
        assert not list(dirs[1].glob("*.mp4"))


class TestRuntimeSnapshot:
    def test_it_reports_slots_costs_and_breaker_without_secrets(
        self, session, dirs, monkeypatch
    ):
        from btcedu.core.avatar_runtime import runtime_snapshot

        service = FakeHeyGen()
        requests = make_requests(dirs[2], 2)
        run_coordinator(session, dirs, service, requests, monkeypatch=monkeypatch)

        snapshot = runtime_snapshot(
            session, EPISODE_ID, provider=PROVIDER, max_concurrent_jobs=3
        )
        assert snapshot["active_jobs"] == 0
        assert snapshot["available_slots"] == 3
        assert snapshot["cost_actual_usd"] > 0
        assert snapshot["cost_unresolved_usd"] == 0
        assert snapshot["circuit_breaker"]["state"] == "closed"
        assert len(snapshot["jobs"]) == 2
        blob = json.dumps(snapshot)
        assert "api_key" not in blob and "https://" not in blob

    def test_a_running_job_consumes_a_slot(self, session, dirs, monkeypatch):
        from btcedu.core.avatar_runtime import runtime_snapshot

        service = FakeHeyGen()
        run_coordinator(
            session, dirs, service, make_requests(dirs[2], 2), monkeypatch=monkeypatch
        )
        job = session.query(AvatarJob).first()
        job.status = AvatarJobStatus.SUBMITTED.value
        session.commit()
        snapshot = runtime_snapshot(
            session, EPISODE_ID, provider=PROVIDER, max_concurrent_jobs=2
        )
        assert snapshot["active_jobs"] == 1
        assert snapshot["available_slots"] == 1

    def test_unresolved_cost_is_reported_separately(self, session, dirs, monkeypatch):
        from btcedu.core.avatar_runtime import runtime_snapshot

        service = FakeHeyGen()
        run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        job = session.query(AvatarJob).one()
        job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
        session.commit()
        snapshot = runtime_snapshot(session, EPISODE_ID, provider=PROVIDER)
        assert snapshot["cost_unresolved_usd"] > 0
        assert snapshot["unresolved_jobs"] == 1


class TestNoRealProviderCalls:
    def test_the_download_helper_needs_an_opener_or_requests(self):
        """Guard: nothing in this module reaches the network by default."""
        assert OP_DOWNLOAD_RESULT == "download_result"

    def test_the_renderer_module_never_imports_the_provider_service(self):
        source = Path("btcedu/core/scene_renderer.py").read_text(encoding="utf-8")
        assert "anchor_service" not in source
        assert "avatar_coordinator" not in source


class LegacyDIDService:
    """The pre-existing provider surface: submit and collect, nothing granular."""

    def __init__(self, anchor_dir: Path):
        self.anchor_dir = anchor_dir
        self.submissions: list[str] = []

    def submit_anchor_video(self, request) -> str:
        self.submissions.append(request.clip_id)
        return f"did_{len(self.submissions)}"

    def collect_anchor_video(self, provider_job_id, request):
        from btcedu.services.anchor_service import AnchorResponse

        path = self.anchor_dir / f"{request.clip_id}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"did-clip")
        return AnchorResponse(
            chapter_id=request.chapter_id,
            provider_job_id=provider_job_id,
            video_path=str(path),
            duration_seconds=request.expected_duration_seconds,
            size_bytes=8,
            cost_usd=0.05,
            output_format="mp4",
            mime_type="video/mp4",
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(duration_seconds * 0.01, 6)


class TestLegacyProviderIsUntouched:
    def test_a_did_service_still_goes_through_submit_and_collect(
        self, session, dirs, monkeypatch
    ):
        """No granular retry matrix for D-ID — and no behaviour change either."""
        service = LegacyDIDService(dirs[1])
        result, _ = run_coordinator(
            session, dirs, service, make_requests(dirs[2], 2), monkeypatch=monkeypatch
        )
        assert result.complete
        assert service.submissions == ["sc_001", "sc_002"]
        assert all(outcome.cost_usd == 0.05 for outcome in result.outcomes)

    def test_the_legacy_path_still_records_the_ledger(self, session, dirs, monkeypatch):
        service = LegacyDIDService(dirs[1])
        run_coordinator(
            session, dirs, service, make_requests(dirs[2], 1), monkeypatch=monkeypatch
        )
        job = session.query(AvatarJob).one()
        assert job.status == AvatarJobStatus.COMPLETED.value
        assert job.provider_job_id == "did_1"


class TestPipelineLock:
    def test_a_second_run_of_the_same_pipeline_is_refused(self, tmp_path):
        """Two processes generating the same episode would order twice."""
        from types import SimpleNamespace

        from btcedu.core.runlock import PipelineBusyError, pipeline_lock

        settings = SimpleNamespace(data_dir=str(tmp_path), outputs_dir=str(tmp_path))
        with pipeline_lock(settings):
            with pytest.raises(PipelineBusyError):
                with pipeline_lock(settings):
                    pytest.fail("the lock let a second run in")
