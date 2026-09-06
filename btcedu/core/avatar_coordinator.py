"""Buying several presenter clips at once, without ever buying one twice.

The sequential version of this stage was safe and slow: one clip at a time, each
waiting out a generation that HeyGen runs in a couple of minutes. Nine chapters
of a bulletin therefore took as long as nine generations in a row. Doing them
concurrently is worth real minutes on a Raspberry Pi — but only if concurrency
does not quietly dismantle the guarantees the ledger exists for.

The shape below is the whole argument:

1. **Plan** — in the main thread, with the session. Every scene is reserved,
   budget-checked and given a persisted idempotency key *before* anything is
   dispatched.
2. **Execute** — in a bounded pool. Worker threads see plain dataclasses. They
   never touch the ``Session``, never open a transaction, and never decide
   anything about money.
3. **Persist** — back in the main thread, driven by a queue the workers write
   to. A provider job id reaches the database within milliseconds of existing,
   which is the difference between a crash that resumes and a crash that
   re-orders.

Three limits are enforced rather than hoped for: at most ``max_concurrent_jobs``
generations are in flight, an open circuit breaker stops new submissions on the
spot, and a SIGTERM stops dispatching but lets the jobs already at the provider
be recorded before the process leaves.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from btcedu.core import avatar_breaker
from btcedu.core.avatar_download import (
    VALIDATION_QUARANTINED,
    VALIDATION_VALID,
    ClipExpectation,
    DownloadError,
    ValidationError,
    download_and_validate,
)
from btcedu.core.avatar_retry import (
    OP_CREATE_VIDEO,
    OP_DOWNLOAD_RESULT,
    OP_POLL_STATUS,
    OP_UPLOAD_ASSET,
    FailureClass,
    FailureVerdict,
    PermanentFailure,
    RetryExhausted,
    classify,
    idempotency_expiry,
    idempotency_key_is_live,
    run_with_retry,
)
from btcedu.models.avatar_audio_asset import AudioAssetStatus, AvatarAudioAsset
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus

logger = logging.getLogger(__name__)

#: How long one clip may stay in "processing" before the run gives up waiting.
#: Giving up is not the same as failing: see :meth:`_finish_deferred`.
DEFAULT_POLL_TIMEOUT_SECONDS = 30 * 60

ACTION_SUBMIT = "submit"
ACTION_RESUME = "resume"
ACTION_REUSE = "reuse"


class AvatarConcurrencyError(RuntimeError):
    """The run cannot proceed safely and stops rather than guess."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SceneRequest:
    """One presenter clip the stage would like to have."""

    scene_id: str
    chapter_id: str
    content_hash: str
    audio_path: Path
    audio_hash: str
    expected_duration_seconds: float
    estimated_cost_usd: float
    title: str = ""


@dataclass
class SceneWork:
    """A planned scene, carrying everything a worker needs and nothing more.

    Explicitly *not* carrying the session, the job object or the settings. A
    worker that cannot reach the database cannot accidentally hold a write
    transaction open across a two-minute generation.
    """

    request: SceneRequest
    job_id: int
    action: str
    provider_job_id: str = ""
    audio_asset_id: str = ""
    idempotency_key: str = ""
    idempotency_live: bool = True


@dataclass
class SceneOutcome:
    """A clip that exists, is validated and is recorded."""

    scene_id: str
    action: str
    job_id: int
    provider_job_id: str
    video_path: Path
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    output_format: str
    mime_type: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class SceneFailure:
    """A clip that did not happen, and what that means for the money."""

    scene_id: str
    job_id: int
    verdict: FailureVerdict | None
    message: str
    #: True when the provider may have accepted and billed the request.
    ambiguous: bool
    #: True when nothing was bought and the row may be tried again later.
    retryable_later: bool = False
    #: The original provider exception, kept so an unambiguous refusal can be
    #: re-raised unchanged instead of being flattened into a generic message.
    cause: Exception | None = None


@dataclass
class DeferredScene:
    """Still generating at the provider when this run ran out of patience."""

    scene_id: str
    job_id: int
    provider_job_id: str
    last_state: str


@dataclass
class CoordinatorResult:
    """What one bounded, concurrent pass over an episode achieved."""

    outcomes: list[SceneOutcome] = field(default_factory=list)
    failures: list[SceneFailure] = field(default_factory=list)
    deferred: list[DeferredScene] = field(default_factory=list)
    submitted: int = 0
    reused: int = 0
    resumed: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0
    interrupted: bool = False
    breaker_blocked: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures and not self.deferred and not self.breaker_blocked


# --------------------------------------------------------------------------
# Events: the only channel from a worker back to the database.
# --------------------------------------------------------------------------

EVENT_ASSET = "asset_uploaded"
EVENT_SUBMITTED = "submitted"
EVENT_RETRY = "retry"
EVENT_POLL = "poll"


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    job_id: int
    payload: dict[str, Any] = field(default_factory=dict)


class AvatarCoordinator:
    """Plans, dispatches and records one episode's presenter clips."""

    def __init__(
        self,
        session: Session,
        *,
        episode_id: str,
        provider: str,
        engine: str,
        look_id: str,
        output_format: str,
        service: Any,
        outputs_dir: Path,
        anchor_dir: Path,
        max_concurrent_jobs: int = 1,
        poll_interval_seconds: float = 5.0,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        expectation: ClipExpectation | None = None,
        budget_check: Callable[[float], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] = _utcnow,
        install_signal_handler: bool = True,
    ):
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        self.session = session
        self.episode_id = episode_id
        self.provider = provider
        self.engine = engine
        self.look_id = look_id
        self.output_format = output_format
        self.service = service
        self.outputs_dir = Path(outputs_dir)
        self.anchor_dir = Path(anchor_dir)
        self.max_concurrent_jobs = int(max_concurrent_jobs)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.poll_timeout_seconds = float(poll_timeout_seconds)
        self.expectation = expectation or ClipExpectation(output_format=output_format)
        self.budget_check = budget_check
        self.now = now
        self.install_signal_handler = install_signal_handler
        if sleep is None:
            import time as _time

            sleep = _time.sleep
        self.sleep = sleep

        self._events: queue.Queue[WorkerEvent] = queue.Queue()
        self._shutdown = threading.Event()

    # -- Public entry point -------------------------------------------------

    def run(self, plan: list[SceneWork]) -> CoordinatorResult:
        """Execute a planned set of scenes under the concurrency cap.

        Planning happens in :func:`plan_scene_work` because it is the part that
        needs the ledger, the budget and the reconciliation rules — none of
        which a worker thread may see.
        """
        result = CoordinatorResult()

        pending: deque[SceneWork] = deque()
        for work in plan:
            if work.action == ACTION_REUSE:
                outcome = self._reuse_outcome(work)
                if outcome is not None:
                    result.outcomes.append(outcome)
                    result.reused += 1
                    result.total_duration_seconds += outcome.duration_seconds
                continue
            pending.append(work)

        if not pending:
            return result

        with self._signal_guard():
            with ThreadPoolExecutor(
                max_workers=self.max_concurrent_jobs,
                thread_name_prefix="avatar",
            ) as pool:
                running: dict[Future, SceneWork] = {}
                while pending or running:
                    self._dispatch(pool, pending, running, result)
                    if not running:
                        # Nothing in flight and nothing dispatchable: either the
                        # breaker closed the door or a shutdown was requested.
                        break
                    done, _ = wait(
                        set(running),
                        timeout=0.05,
                        return_when=FIRST_COMPLETED,
                    )
                    self._drain_events()
                    for future in done:
                        work = running.pop(future)
                        self._record_result(future, work, result)
                self._drain_events()

        result.interrupted = self._shutdown.is_set()
        return result

    def _reuse_outcome(self, work: SceneWork) -> SceneOutcome | None:
        """Build an outcome from a clip this episode already paid for."""
        job = self.session.get(AvatarJob, work.job_id)
        if job is None or not job.output_path:
            return None
        path = self.outputs_dir / job.output_path
        if not path.exists():
            return None
        mime = "video/webm" if self.output_format == "webm" else "video/mp4"
        return SceneOutcome(
            scene_id=work.request.scene_id,
            action=ACTION_REUSE,
            job_id=work.job_id,
            provider_job_id=job.provider_job_id or "",
            video_path=path,
            duration_seconds=float(job.duration_seconds or 0.0),
            size_bytes=_safe_size(path),
            # Already on the episode's bill; counting it again would make the
            # stage look twice as expensive as it was.
            cost_usd=0.0,
            output_format=self.output_format,
            mime_type=mime,
        )

    def request_shutdown(self) -> None:
        """Stop dispatching new work; let in-flight jobs be recorded."""
        self._shutdown.set()

    # -- Dispatch -----------------------------------------------------------

    def _dispatch(
        self,
        pool: ThreadPoolExecutor,
        pending: deque[SceneWork],
        running: dict[Future, SceneWork],
        result: CoordinatorResult,
    ) -> None:
        while pending and len(running) < self.max_concurrent_jobs:
            if self._shutdown.is_set():
                logger.warning(
                    "Shutdown requested; %d avatar scene(s) left undispatched", len(pending)
                )
                pending.clear()
                return
            work = pending[0]
            if work.action == ACTION_SUBMIT:
                # Both gates are checked here, in the main thread, immediately
                # before the only call that can cost money.
                try:
                    avatar_breaker.ensure_submission_allowed(
                        self.session, self.provider, now=self.now
                    )
                except avatar_breaker.BreakerOpenError as exc:
                    logger.error("Avatar submissions blocked: %s", exc)
                    result.breaker_blocked = [w.request.scene_id for w in pending]
                    pending.clear()
                    return
                if self.budget_check is not None:
                    self.budget_check(work.request.estimated_cost_usd)
            pending.popleft()
            running[pool.submit(self._run_scene, work)] = work

    # -- Worker (no session, no transaction, no money decisions) ------------

    def _run_scene(self, work: SceneWork) -> SceneOutcome | SceneFailure | DeferredScene:
        try:
            if work.action == ACTION_SUBMIT:
                provider_job_id = self._submit(work)
            else:
                provider_job_id = work.provider_job_id
                if not provider_job_id:
                    return SceneFailure(
                        scene_id=work.request.scene_id,
                        job_id=work.job_id,
                        verdict=None,
                        message="resume requested without a provider job id",
                        ambiguous=True,
                    )
            return self._collect(work, provider_job_id)
        except _SceneAborted as exc:
            if exc.failure.cause is None:
                exc.failure.cause = exc.cause
            return exc.failure
        except Exception as exc:  # noqa: BLE001 - turned into a ledger decision
            # Reached by providers outside the granular retry matrix (D-ID) and
            # by genuine bugs. Classify anyway: a flat "assume billed" here would
            # send every plain refusal to reconciliation.
            verdict = classify(OP_CREATE_VIDEO, exc, now=self.now)
            return SceneFailure(
                scene_id=work.request.scene_id,
                job_id=work.job_id,
                verdict=verdict,
                message=str(exc)[:500],
                ambiguous=verdict.ambiguous,
                retryable_later=(
                    verdict.failure_class is FailureClass.REQUEST_REJECTED
                    and not verdict.ambiguous
                ),
                cause=exc,
            )

    def _submit(self, work: SceneWork) -> str:
        request = work.request
        granular = hasattr(self.service, "poll_video_once")

        if not granular:
            # Legacy provider (D-ID). Its submit/collect pair is left exactly as
            # it was; concurrency and the ledger still apply, the granular retry
            # matrix does not.
            from btcedu.services.anchor_service import AnchorRequest

            legacy = AnchorRequest(
                source_image_path="",
                source_image_url="",
                audio_path=str(request.audio_path),
                chapter_id=request.chapter_id,
                clip_id=request.scene_id,
                expected_duration_seconds=request.expected_duration_seconds,
                idempotency_key=work.idempotency_key,
            )
            provider_job_id = self.service.submit_anchor_video(legacy)
            self._emit(EVENT_SUBMITTED, work, provider_job_id=provider_job_id)
            return provider_job_id

        asset_id = work.audio_asset_id
        if not asset_id:
            asset_id = self._guarded(
                work,
                OP_UPLOAD_ASSET,
                lambda: self.service.upload_audio_asset(str(request.audio_path)),
            )
            self._emit(
                EVENT_ASSET,
                work,
                asset_id=asset_id,
                audio_hash=request.audio_hash,
                size_bytes=_safe_size(request.audio_path),
            )

        if not work.idempotency_live:
            # The persisted key is older than the provider's window, so a repeat
            # would be a fresh order rather than the same one. Refusing here is
            # the whole point of storing the expiry.
            raise _SceneAborted(
                SceneFailure(
                    scene_id=request.scene_id,
                    job_id=work.job_id,
                    verdict=None,
                    message=(
                        "the stored idempotency key is older than the provider's "
                        "24-hour window; a repeat submission would be a second order"
                    ),
                    ambiguous=True,
                )
            )

        provider_job_id = self._guarded(
            work,
            OP_CREATE_VIDEO,
            lambda: self.service.create_video(
                asset_id,
                title=request.title or request.chapter_id,
                idempotency_key=work.idempotency_key,
            ),
        )
        self._emit(EVENT_SUBMITTED, work, provider_job_id=provider_job_id, asset_id=asset_id)
        return provider_job_id

    def _collect(
        self, work: SceneWork, provider_job_id: str
    ) -> SceneOutcome | SceneFailure | DeferredScene:
        request = work.request
        granular = hasattr(self.service, "poll_video_once")

        if not granular:
            from btcedu.services.anchor_service import AnchorRequest

            legacy = AnchorRequest(
                source_image_path="",
                source_image_url="",
                audio_path=str(request.audio_path),
                chapter_id=request.chapter_id,
                clip_id=request.scene_id,
                expected_duration_seconds=request.expected_duration_seconds,
                idempotency_key=work.idempotency_key,
            )
            response = self.service.collect_anchor_video(provider_job_id, legacy)
            return SceneOutcome(
                scene_id=request.scene_id,
                action=work.action,
                job_id=work.job_id,
                provider_job_id=response.provider_job_id or provider_job_id,
                video_path=Path(response.video_path),
                duration_seconds=response.duration_seconds,
                size_bytes=response.size_bytes,
                cost_usd=response.cost_usd,
                output_format=response.output_format,
                mime_type=response.mime_type,
            )

        from btcedu.services.anchor_service import (
            STATE_COMPLETED,
            STATE_FAILED,
            STATE_PROCESSING,
        )

        waited = 0.0
        status = None
        while waited <= self.poll_timeout_seconds:
            if self._shutdown.is_set():
                return DeferredFailure(work, provider_job_id, "shutdown").as_deferred()
            status = self._guarded(
                work,
                OP_POLL_STATUS,
                lambda: self.service.poll_video_once(provider_job_id),
            )
            self._emit(EVENT_POLL, work, state=status.state, raw_status=status.raw_status)
            if status.state == STATE_COMPLETED:
                break
            if status.state == STATE_FAILED:
                # The provider says it failed. That is a definite statement and
                # HeyGen does not bill a failed generation, so the row may be
                # retried on a later run.
                return SceneFailure(
                    scene_id=request.scene_id,
                    job_id=work.job_id,
                    verdict=FailureVerdict(
                        operation=OP_POLL_STATUS,
                        failure_class=FailureClass.REQUEST_REJECTED,
                        retryable=False,
                        ambiguous=False,
                        reason=status.error_code or "generation_failed",
                    ),
                    message=status.error_detail or "provider reported a failed generation",
                    ambiguous=False,
                    retryable_later=True,
                )
            if status.state == STATE_PROCESSING:
                self.sleep(self.poll_interval_seconds)
                waited += self.poll_interval_seconds
                continue
            # Unknown status: fail closed. Waiting on a state we cannot read is
            # how a run hangs, and assuming it means "failed" is how it pays
            # twice.
            return SceneFailure(
                scene_id=request.scene_id,
                job_id=work.job_id,
                verdict=FailureVerdict(
                    operation=OP_POLL_STATUS,
                    failure_class=FailureClass.UNKNOWN_STATUS,
                    retryable=False,
                    ambiguous=True,
                    reason=f"unmapped provider status {status.raw_status!r}",
                ),
                message=f"provider reported an unmapped status {status.raw_status!r}",
                ambiguous=True,
            )
        else:
            status = None

        if status is None or status.state != STATE_COMPLETED:
            # Out of patience, not out of hope. The job is still the provider's
            # and still ours; the next run resumes it from the ledger.
            return DeferredFailure(
                work, provider_job_id, status.raw_status if status else "processing"
            ).as_deferred()

        destination = self.anchor_dir / f"{request.scene_id}{_extension(self.output_format)}"
        expectation = ClipExpectation(
            output_format=self.expectation.output_format,
            width=self.expectation.width,
            height=self.expectation.height,
            fps=self.expectation.fps,
            expected_duration_seconds=request.expected_duration_seconds,
            require_alpha=self.expectation.require_alpha,
        )
        downloaded = self._download(work, status, provider_job_id, destination, expectation)
        if isinstance(downloaded, SceneFailure):
            return downloaded

        cost = self.service.estimate_cost(status.duration_seconds)
        mime = "video/webm" if self.output_format == "webm" else "video/mp4"
        return SceneOutcome(
            scene_id=request.scene_id,
            action=work.action,
            job_id=work.job_id,
            provider_job_id=provider_job_id,
            video_path=downloaded.path,
            duration_seconds=status.duration_seconds,
            size_bytes=downloaded.size_bytes,
            cost_usd=cost,
            output_format=self.output_format,
            mime_type=mime,
            warnings=list(downloaded.warnings),
        )

    def _download(
        self,
        work: SceneWork,
        status: Any,
        provider_job_id: str,
        destination: Path,
        expectation: ClipExpectation,
    ) -> Any:
        """Fetch the finished clip, refreshing a stale URL exactly once.

        A signed download URL expires; the job behind it does not. So the only
        correct response to a dead link is another read-only status call, and
        under no circumstances another generation.
        """
        url = status.video_url
        for attempt in (1, 2):
            try:
                return self._guarded(
                    work,
                    OP_DOWNLOAD_RESULT,
                    lambda target=url: download_and_validate(target, destination, expectation),
                )
            except _SceneAborted as exc:
                cause = exc.cause
                if isinstance(cause, ValidationError):
                    # The clip was generated and billed; it is simply not
                    # usable. An operator's problem, not a reason to re-order.
                    return SceneFailure(
                        scene_id=work.request.scene_id,
                        job_id=work.job_id,
                        verdict=None,
                        message=f"downloaded clip failed validation: {cause}",
                        ambiguous=True,
                    )
                stale = exc.failure.verdict is not None and exc.failure.verdict.failure_class in {
                    FailureClass.NOT_FOUND,
                    FailureClass.AUTH,
                }
                if attempt == 1 and stale:
                    refreshed = self._guarded(
                        work,
                        OP_POLL_STATUS,
                        lambda: self.service.poll_video_once(provider_job_id),
                    )
                    if getattr(refreshed, "video_url", ""):
                        url = refreshed.video_url
                        continue
                if isinstance(cause, DownloadError):
                    return SceneFailure(
                        scene_id=work.request.scene_id,
                        job_id=work.job_id,
                        verdict=None,
                        message=f"clip download failed: {cause}",
                        ambiguous=True,
                    )
                return exc.failure
        return SceneFailure(  # pragma: no cover - the loop always returns first
            scene_id=work.request.scene_id,
            job_id=work.job_id,
            verdict=None,
            message="clip download did not complete",
            ambiguous=True,
        )

    def _guarded(self, work: SceneWork, operation: str, func: Callable[[], Any]) -> Any:
        """Run one provider call under its policy, reporting every retry."""

        def on_attempt(attempt: int, verdict: FailureVerdict, delay: float) -> None:
            self._emit(
                EVENT_RETRY,
                work,
                operation=operation,
                attempt=attempt,
                failure_class=verdict.failure_class.value,
                status_code=verdict.status_code,
                retry_after=verdict.retry_after_seconds,
                delay=delay,
            )

        try:
            return run_with_retry(
                operation,
                func,
                sleep=self.sleep,
                now=self.now,
                on_attempt=on_attempt,
            )
        except (PermanentFailure, RetryExhausted) as exc:
            verdict = exc.verdict
            raise _SceneAborted(
                SceneFailure(
                    scene_id=work.request.scene_id,
                    job_id=work.job_id,
                    verdict=verdict,
                    message=f"{operation}: {verdict.reason}",
                    ambiguous=verdict.ambiguous,
                    retryable_later=(
                        verdict.failure_class is FailureClass.REQUEST_REJECTED
                        and not verdict.ambiguous
                    ),
                    cause=exc.cause,
                ),
                cause=exc.cause,
            ) from exc

    def _emit(self, kind: str, work: SceneWork, **payload: Any) -> None:
        self._events.put(WorkerEvent(kind=kind, job_id=work.job_id, payload=payload))

    # -- Persistence (main thread only) ------------------------------------

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply_event(event)
            except Exception:  # noqa: BLE001 - telemetry must never kill a run
                logger.exception("Failed to persist avatar coordinator event %s", event.kind)
                self.session.rollback()

    def _apply_event(self, event: WorkerEvent) -> None:
        job = self.session.get(AvatarJob, event.job_id)
        if job is None:
            return
        moment = self.now()

        if event.kind == EVENT_ASSET:
            asset_id = str(event.payload.get("asset_id") or "")
            audio_hash = str(event.payload.get("audio_hash") or "")
            if asset_id and audio_hash:
                _upsert_audio_asset(
                    self.session,
                    provider=self.provider,
                    audio_hash=audio_hash,
                    asset_id=asset_id,
                    episode_id=self.episode_id,
                    scene_id=job.scene_id,
                    size_bytes=int(event.payload.get("size_bytes") or 0),
                    now=moment,
                )
                job.audio_asset_id = asset_id
                job.audio_hash = audio_hash
                self.session.commit()
            return

        if event.kind == EVENT_SUBMITTED:
            provider_job_id = str(event.payload.get("provider_job_id") or "")
            if not provider_job_id:
                return
            asset_id = str(event.payload.get("asset_id") or "")
            if asset_id:
                job.audio_asset_id = asset_id
            job.provider_job_id = provider_job_id
            job.status = AvatarJobStatus.SUBMITTED.value
            job.submitted_at = moment
            job.next_poll_at = moment + timedelta(seconds=self.poll_interval_seconds)
            self.session.commit()
            return

        if event.kind == EVENT_RETRY:
            job.retry_count = int(job.retry_count or 0) + 1
            job.last_error_type = str(event.payload.get("failure_class") or "")[:32]
            status_code = event.payload.get("status_code")
            job.last_status_code = int(status_code) if isinstance(status_code, int) else None
            retry_after = event.payload.get("retry_after")
            job.retry_after_seconds = float(retry_after) if retry_after else None
            delay = float(event.payload.get("delay") or 0.0)
            job.next_poll_at = moment + timedelta(seconds=delay)
            self.session.commit()
            return

        if event.kind == EVENT_POLL:
            job.next_poll_at = moment + timedelta(seconds=self.poll_interval_seconds)
            self.session.commit()

    def _record_result(
        self, future: Future, work: SceneWork, result: CoordinatorResult
    ) -> None:
        from btcedu.core.avatar_jobs import (
            hold_for_reconciliation,
            record_completion,
            record_refusal,
        )

        try:
            value = future.result()
        except Exception as exc:  # noqa: BLE001 - a worker bug is still ambiguous
            logger.exception("Avatar worker for %s crashed", work.request.scene_id)
            value = SceneFailure(
                scene_id=work.request.scene_id,
                job_id=work.job_id,
                verdict=None,
                message=str(exc)[:500],
                ambiguous=True,
            )

        job = self.session.get(AvatarJob, work.job_id)

        if isinstance(value, DeferredScene):
            result.deferred.append(value)
            if job is not None:
                job.next_poll_at = self.now() + timedelta(seconds=self.poll_interval_seconds)
                self.session.commit()
            return

        if isinstance(value, SceneFailure):
            if job is not None:
                # Ambiguity, not retryability, decides where the money lands.
                # An auth rejection is not retryable and is still unbilled; an
                # exhausted 5xx retry is retryable later and may already have
                # been charged. Only the second kind may hold a reservation.
                if value.ambiguous:
                    hold_for_reconciliation(self.session, job, value.message)
                else:
                    record_refusal(self.session, job, value.message)
                if value.message.startswith("downloaded clip failed validation"):
                    job.validation_status = VALIDATION_QUARANTINED
                    job.validation_error = value.message[:1000]
                    self.session.commit()
            if value.verdict is not None:
                avatar_breaker.record_failure(
                    self.session, self.provider, value.verdict, now=self.now
                )
            result.failures.append(value)
            return

        # Success.
        try:
            relative = value.video_path.relative_to(self.outputs_dir)
        except ValueError:
            result.failures.append(
                SceneFailure(
                    scene_id=value.scene_id,
                    job_id=value.job_id,
                    verdict=None,
                    message=(
                        "provider wrote outside the episode output directory: "
                        f"{value.video_path}"
                    ),
                    ambiguous=True,
                )
            )
            return

        if job is not None:
            record_completion(
                self.session,
                job,
                output_path=str(relative),
                duration_seconds=value.duration_seconds,
                cost_usd=value.cost_usd,
            )
            job.validation_status = VALIDATION_VALID
            job.validation_error = None
            job.next_poll_at = None
            self.session.commit()

        avatar_breaker.record_success(self.session, self.provider, now=self.now)
        result.outcomes.append(value)
        result.total_cost_usd += value.cost_usd
        result.total_duration_seconds += value.duration_seconds
        if work.action == ACTION_SUBMIT:
            result.submitted += 1
        else:
            result.resumed += 1

    # -- Shutdown -----------------------------------------------------------

    class _SignalGuard:
        def __init__(self, coordinator: AvatarCoordinator):
            self.coordinator = coordinator
            self.previous: Any = None
            self.installed = False

        def __enter__(self) -> AvatarCoordinator._SignalGuard:
            if not self.coordinator.install_signal_handler:
                return self
            try:
                self.previous = signal.getsignal(signal.SIGTERM)

                def handler(signum, frame):  # pragma: no cover - timing dependent
                    logger.warning("SIGTERM received; finishing in-flight avatar jobs")
                    self.coordinator.request_shutdown()

                signal.signal(signal.SIGTERM, handler)
                self.installed = True
            except ValueError:
                # Not the main thread. The coordinator still works; it simply
                # cannot hear the signal, which is the caller's problem to solve.
                self.installed = False
            return self

        def __exit__(self, *exc_info: object) -> None:
            if self.installed and self.previous is not None:
                try:
                    signal.signal(signal.SIGTERM, self.previous)
                except ValueError:  # pragma: no cover
                    pass

    def _signal_guard(self) -> AvatarCoordinator._SignalGuard:
        return AvatarCoordinator._SignalGuard(self)


class _SceneAborted(Exception):
    """Internal: carries a classified failure, and its cause, out of a worker."""

    def __init__(self, failure: SceneFailure, cause: BaseException | None = None):
        self.failure = failure
        self.cause = cause
        super().__init__(failure.message)


@dataclass
class DeferredFailure:
    """Helper turning an out-of-patience poll into a deferral."""

    work: SceneWork
    provider_job_id: str
    last_state: str

    def as_deferred(self) -> DeferredScene:
        return DeferredScene(
            scene_id=self.work.request.scene_id,
            job_id=self.work.job_id,
            provider_job_id=self.provider_job_id,
            last_state=self.last_state,
        )


def _extension(output_format: str) -> str:
    return ".webm" if output_format == "webm" else ".mp4"


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _upsert_audio_asset(
    session: Session,
    *,
    provider: str,
    audio_hash: str,
    asset_id: str,
    episode_id: str,
    scene_id: str,
    size_bytes: int,
    now: datetime,
) -> AvatarAudioAsset:
    row = (
        session.query(AvatarAudioAsset)
        .filter(
            AvatarAudioAsset.provider == provider,
            AvatarAudioAsset.audio_hash == audio_hash,
        )
        .first()
    )
    if row is None:
        row = AvatarAudioAsset(
            provider=provider,
            audio_hash=audio_hash,
            asset_id=asset_id,
            episode_id=episode_id,
            scene_id=scene_id,
            size_bytes=size_bytes,
            status=AudioAssetStatus.ACTIVE.value,
            created_at=now,
            last_used_at=now,
        )
        session.add(row)
    else:
        row.asset_id = asset_id
        row.status = AudioAssetStatus.ACTIVE.value
        row.expired_reason = None
        row.last_used_at = now
    return row


def find_audio_asset(
    session: Session, *, provider: str, audio_hash: str
) -> AvatarAudioAsset | None:
    """The provider's copy of this exact narration, if it still has one."""
    if not audio_hash:
        return None
    return (
        session.query(AvatarAudioAsset)
        .filter(
            AvatarAudioAsset.provider == provider,
            AvatarAudioAsset.audio_hash == audio_hash,
            AvatarAudioAsset.status == AudioAssetStatus.ACTIVE.value,
        )
        .first()
    )


def expire_audio_asset(
    session: Session, *, provider: str, audio_hash: str, reason: str
) -> None:
    """Record that the provider no longer knows an asset we uploaded.

    Marked rather than deleted, so the next run re-uploads deliberately instead
    of silently rediscovering the same dead id — and so a pattern of expiries
    is visible rather than invisible.
    """
    row = (
        session.query(AvatarAudioAsset)
        .filter(
            AvatarAudioAsset.provider == provider,
            AvatarAudioAsset.audio_hash == audio_hash,
        )
        .first()
    )
    if row is None:
        return
    row.status = AudioAssetStatus.EXPIRED.value
    row.expired_reason = str(reason)[:1000]
    session.commit()


def build_idempotency_key(episode_id: str, scene_id: str, content_hash: str) -> str:
    """Stable per generation attempt, and different for a new attempt.

    The content hash already carries the generation revision, so a deliberate
    re-purchase produces a different key while every retry of the same attempt —
    including one after a reboot — produces the identical one.
    """
    import hashlib

    payload = f"{episode_id}:{scene_id}:{content_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


def ensure_idempotency_key(
    session: Session,
    job: AvatarJob,
    *,
    episode_id: str,
    now: Callable[[], datetime] = _utcnow,
) -> tuple[str, bool]:
    """Persist the key before the first create call and reuse it afterwards.

    Returns ``(key, still_live)``. ``still_live`` is False once the provider's
    24-hour window has passed, which is the point at which repeating a POST
    stops being a retry and starts being a second order.
    """
    moment = now()
    if not job.idempotency_key:
        job.idempotency_key = build_idempotency_key(episode_id, job.scene_id, job.content_hash)
        job.idempotency_expires_at = idempotency_expiry(moment)
        session.commit()
        return job.idempotency_key, True

    live = idempotency_key_is_live(job.idempotency_expires_at, now=now)
    return job.idempotency_key, live


class SceneReconciliationRequired(AvatarConcurrencyError):
    """A scene's ledger row has an unknown outcome; nothing may be ordered."""


def plan_scene_work(
    session: Session,
    *,
    episode_id: str,
    requests: list[SceneRequest],
    provider: str,
    engine: str,
    look_id: str,
    output_format: str,
    outputs_dir: Path,
    budget_check: Callable[[float], None] | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> list[SceneWork]:
    """Turn scene requests into work the pool is allowed to execute.

    Everything that needs a decision happens here, sequentially, in the main
    thread: the ledger reservation, the per-job budget check, the idempotency
    key and the lookup of an audio asset the provider already holds. By the
    time a :class:`SceneWork` exists, the only remaining question is whether
    the network cooperates.
    """
    from btcedu.core.avatar_jobs import (
        ACTION_RECONCILE,
        reserve_scene,
    )
    from btcedu.core.avatar_jobs import (
        ACTION_RESUME as LEDGER_RESUME,
    )
    from btcedu.core.avatar_jobs import (
        ACTION_REUSE as LEDGER_REUSE,
    )

    plan: list[SceneWork] = []
    for request in requests:
        decision = reserve_scene(
            session,
            episode_id=episode_id,
            scene_id=request.scene_id,
            chapter_id=request.chapter_id,
            content_hash=request.content_hash,
            provider=provider,
            engine=engine,
            avatar_look_id=look_id,
            output_format=output_format,
            estimated_cost_usd=request.estimated_cost_usd,
        )
        job = decision.job

        if decision.action == ACTION_RECONCILE:
            raise SceneReconciliationRequired(
                f"Avatar job for scene {request.scene_id} of {episode_id} has an unknown "
                f"outcome ({decision.reason}). It may already have been billed; "
                "resolve it with an operator reconciliation before retrying."
            )

        action = ACTION_SUBMIT
        if decision.action == LEDGER_REUSE:
            existing = outputs_dir / (job.output_path or "")
            if job.output_path and existing.exists():
                action = ACTION_REUSE
            else:
                # Paid for, but the file is gone. Fetch it from the same job
                # rather than ordering a replacement.
                action = ACTION_RESUME
        elif decision.action == LEDGER_RESUME:
            action = ACTION_RESUME

        idempotency_key = ""
        idempotency_live = True
        audio_asset_id = ""
        if action == ACTION_SUBMIT:
            if budget_check is not None:
                budget_check(request.estimated_cost_usd)
            idempotency_key, idempotency_live = ensure_idempotency_key(
                session, job, episode_id=episode_id, now=now
            )
            asset = find_audio_asset(
                session, provider=provider, audio_hash=request.audio_hash
            )
            if asset is not None:
                audio_asset_id = asset.asset_id
            elif job.audio_asset_id and job.audio_hash == request.audio_hash:
                audio_asset_id = job.audio_asset_id

        plan.append(
            SceneWork(
                request=request,
                job_id=int(job.id),
                action=action,
                provider_job_id=job.provider_job_id or "",
                audio_asset_id=audio_asset_id,
                idempotency_key=idempotency_key,
                idempotency_live=idempotency_live,
            )
        )
    return plan
