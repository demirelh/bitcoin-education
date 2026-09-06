"""What may be retried against an avatar provider, and what must never be.

A retry is a cheap word for an expensive decision. HeyGen bills a generation
from the moment it is accepted, so "try again" is only safe when we can show
that the previous attempt was *not* accepted. Everything in this module exists
to make that distinction explicit instead of leaving it to whoever writes the
next ``except`` block.

Two properties are tracked separately for every failure, because they are not
the same question:

``retryable``
    May the very same call be repeated automatically right now? A 429 is
    retryable, a 401 never is.

``ambiguous``
    Might the provider have accepted — and therefore billed — the request even
    though we saw an error? A read timeout on a create call is ambiguous; the
    identical timeout on a status poll is not. An ambiguous outcome that
    survives all retries must end in ``reconcile_required``, never in a second
    purchase.

The waiting is driven by injected ``sleep`` and ``now`` callables so the tests
can exercise a full exponential backoff, including ``Retry-After``, without
spending a single real second.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# The five things we ever ask an avatar provider to do. They are listed
# separately because their risk profiles differ: repeating an upload wastes
# bandwidth, repeating a create may buy a second video.
OP_UPLOAD_ASSET = "upload_asset"
OP_CREATE_VIDEO = "create_video"
OP_POLL_STATUS = "poll_status"
OP_DOWNLOAD_RESULT = "download_result"
OP_CAPABILITY = "capability"

OPERATIONS = (
    OP_UPLOAD_ASSET,
    OP_CREATE_VIDEO,
    OP_POLL_STATUS,
    OP_DOWNLOAD_RESULT,
    OP_CAPABILITY,
)

#: Operations that create or may create billable state at the provider.
MUTATING_OPERATIONS = frozenset({OP_CREATE_VIDEO})

#: How long a provider honours a repeated Idempotency-Key. After this window a
#: repeated POST is a new order, so an unclear outcome stops being retryable and
#: becomes an operator's problem.
IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


class FailureClass(str, Enum):
    """Why a provider call failed, in the terms the ledger cares about."""

    AUTH = "auth"  # 401/403 — credentials or plan, never a transient blip
    REQUEST_REJECTED = "request_rejected"  # 400/422 — answered with a refusal
    NOT_FOUND = "not_found"  # 404 — meaning depends entirely on the operation
    RATE_LIMITED = "rate_limited"  # 429
    SERVER_ERROR = "server_error"  # 5xx
    NETWORK = "network"  # connection refused, timeout, reset
    UNKNOWN_STATUS = "unknown_status"  # provider said something we cannot map
    UNEXPECTED = "unexpected"  # a bug on our side, not a provider condition


@dataclass(frozen=True)
class RetryPolicy:
    """How hard one operation may try before giving up."""

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    max_total_wait_seconds: float
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("RetryPolicy delays must be non-negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("RetryPolicy.max_delay_seconds must not be below the base delay")
        if self.max_total_wait_seconds < 0:
            raise ValueError("RetryPolicy.max_total_wait_seconds must be non-negative")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("RetryPolicy.jitter_ratio must be between 0 and 1")


#: The matrix. Uploads and reads may try harder than a create, because a create
#: is the only one of them that can be answered with an invoice.
DEFAULT_POLICIES: dict[str, RetryPolicy] = {
    OP_UPLOAD_ASSET: RetryPolicy(
        max_attempts=4,
        base_delay_seconds=2.0,
        max_delay_seconds=30.0,
        max_total_wait_seconds=120.0,
    ),
    OP_CREATE_VIDEO: RetryPolicy(
        max_attempts=3,
        base_delay_seconds=3.0,
        max_delay_seconds=30.0,
        max_total_wait_seconds=90.0,
    ),
    OP_POLL_STATUS: RetryPolicy(
        max_attempts=5,
        base_delay_seconds=2.0,
        max_delay_seconds=60.0,
        max_total_wait_seconds=180.0,
    ),
    OP_DOWNLOAD_RESULT: RetryPolicy(
        max_attempts=4,
        base_delay_seconds=2.0,
        max_delay_seconds=30.0,
        max_total_wait_seconds=120.0,
    ),
    OP_CAPABILITY: RetryPolicy(
        max_attempts=3,
        base_delay_seconds=1.0,
        max_delay_seconds=10.0,
        max_total_wait_seconds=30.0,
    ),
}


@dataclass(frozen=True)
class FailureVerdict:
    """The classification of one failed provider call."""

    operation: str
    failure_class: FailureClass
    retryable: bool
    #: True when the provider may have accepted and billed the request anyway.
    ambiguous: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None
    reason: str = ""

    @property
    def blocks_provider(self) -> bool:
        """Should this failure count towards opening the circuit breaker?"""
        return self.failure_class in {
            FailureClass.AUTH,
            FailureClass.RATE_LIMITED,
            FailureClass.SERVER_ERROR,
            FailureClass.NETWORK,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "failure_class": self.failure_class.value,
            "retryable": self.retryable,
            "ambiguous": self.ambiguous,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after_seconds,
            "reason": self.reason,
        }


class RetryExhausted(RuntimeError):
    """Every permitted attempt was made and the operation still failed."""

    def __init__(self, verdict: FailureVerdict, cause: BaseException, attempts: int):
        self.verdict = verdict
        self.cause = cause
        self.attempts = attempts
        super().__init__(
            f"{verdict.operation} failed after {attempts} attempt(s): "
            f"{verdict.failure_class.value}"
        )


class PermanentFailure(RuntimeError):
    """The failure is final; repeating the call cannot change the answer."""

    def __init__(self, verdict: FailureVerdict, cause: BaseException):
        self.verdict = verdict
        self.cause = cause
        super().__init__(f"{verdict.operation} failed permanently: {verdict.failure_class.value}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_retry_after(value: Any, *, now: Callable[[], datetime] = _utcnow) -> float | None:
    """Turn a ``Retry-After`` header into seconds.

    RFC 9110 allows both a delay in seconds and an HTTP date, and providers use
    both. A date in the past yields ``0.0`` rather than a negative wait, and
    anything unparseable yields ``None`` so the caller falls back to its own
    backoff instead of trusting a malformed header.
    """
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return max(0.0, float(value))

    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


def _status_and_retry_after(exc: BaseException) -> tuple[int | None, Any]:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    return status, getattr(exc, "retry_after", None)


def _network_verdict(operation: str, exc: BaseException) -> FailureVerdict:
    """Decide whether a transport failure could have reached the provider.

    This is the one place where the distinction the operator cares about is
    technically available: a connection that was never established cannot have
    been accepted, while a request that was sent and never answered may have
    been. Anything we cannot tell apart is treated as ambiguous for a mutating
    call, which is the expensive-but-safe direction.
    """
    name = type(exc).__name__
    never_sent = name in {"ConnectTimeout", "ConnectionRefusedError"}
    mutating = operation in MUTATING_OPERATIONS

    if never_sent:
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.NETWORK,
            retryable=True,
            ambiguous=False,
            reason=f"connection never established ({name})",
        )
    if mutating:
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.NETWORK,
            retryable=True,
            ambiguous=True,
            reason=f"request may have reached the provider ({name})",
        )
    return FailureVerdict(
        operation=operation,
        failure_class=FailureClass.NETWORK,
        retryable=True,
        ambiguous=False,
        reason=f"transport failure on a read-only call ({name})",
    )


def classify(
    operation: str,
    exc: BaseException,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> FailureVerdict:
    """Map one exception onto the retry matrix."""
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown avatar provider operation: {operation!r}")

    status, retry_after_raw = _status_and_retry_after(exc)
    retry_after = parse_retry_after(retry_after_raw, now=now)

    if status is None:
        if _is_transport_error(exc):
            return _network_verdict(operation, exc)
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.UNEXPECTED,
            retryable=False,
            # A bug on our side never buys anything, but on a mutating call we
            # cannot prove where in the call it happened.
            ambiguous=operation in MUTATING_OPERATIONS,
            reason=f"{type(exc).__name__}: {str(exc)[:200]}",
        )

    if status in (401, 403):
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.AUTH,
            retryable=False,
            ambiguous=False,
            status_code=status,
            reason="provider rejected the credentials or the plan",
        )

    if status == 429:
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.RATE_LIMITED,
            retryable=True,
            # A throttled request was refused, not queued. Repeating it with the
            # same idempotency key is safe.
            ambiguous=False,
            status_code=status,
            retry_after_seconds=retry_after,
            reason="provider rate limit",
        )

    if status == 404:
        return _not_found_verdict(operation, status)

    if status in (400, 422):
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.REQUEST_REJECTED,
            retryable=False,
            # The provider answered with a refusal, so it did not start work.
            # This is the only 4xx that lets the ledger free the reservation.
            ambiguous=False,
            status_code=status,
            reason="provider refused the request without starting work",
        )

    if 500 <= status < 600:
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.SERVER_ERROR,
            retryable=True,
            # A 5xx on a create tells us nothing about whether the job was
            # queued before the failure. On a read it tells us nothing at all.
            ambiguous=operation in MUTATING_OPERATIONS,
            status_code=status,
            retry_after_seconds=retry_after,
            reason="provider server error",
        )

    return FailureVerdict(
        operation=operation,
        failure_class=FailureClass.UNKNOWN_STATUS,
        retryable=False,
        ambiguous=operation in MUTATING_OPERATIONS,
        status_code=status,
        reason=f"unmapped provider status {status}",
    )


def _not_found_verdict(operation: str, status: int) -> FailureVerdict:
    """404 means two completely different things depending on what we asked.

    Asking whether a look exists and being told "no" is an answer. Asking after
    a video we know we submitted and being told "no" is not: providers drop
    finished jobs out of their API after a retention period, so a 404 there is
    perfectly compatible with a generation that ran and was billed.
    """
    if operation in {OP_POLL_STATUS, OP_DOWNLOAD_RESULT}:
        return FailureVerdict(
            operation=operation,
            failure_class=FailureClass.NOT_FOUND,
            retryable=False,
            ambiguous=True,
            status_code=status,
            reason=(
                "provider does not know this job id; after its retention window "
                "that is not proof the generation was never billed"
            ),
        )
    return FailureVerdict(
        operation=operation,
        failure_class=FailureClass.NOT_FOUND,
        retryable=False,
        ambiguous=False,
        status_code=status,
        reason="provider reports the resource does not exist",
    )


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return False
    return isinstance(exc, requests.exceptions.RequestException)


def backoff_delay(
    policy: RetryPolicy,
    attempt: int,
    *,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with jitter, unless the provider named a delay.

    ``Retry-After`` wins outright. Guessing a shorter wait than the provider
    asked for is how a 429 storm turns into a ban, and guessing a longer one
    only wastes the run's time budget.
    """
    if retry_after is not None:
        return max(0.0, float(retry_after))
    raw = policy.base_delay_seconds * (2 ** max(0, attempt - 1))
    capped = min(raw, policy.max_delay_seconds)
    if policy.jitter_ratio <= 0:
        return capped
    # Full-width jitter around the capped value, never negative.
    spread = capped * policy.jitter_ratio
    return max(0.0, capped - spread + (2 * spread * rand()))


def run_with_retry(
    operation: str,
    func: Callable[[], Any],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] = _utcnow,
    rand: Callable[[], float] = random.random,
    on_attempt: Callable[[int, FailureVerdict, float], None] | None = None,
) -> Any:
    """Call ``func`` under this operation's retry policy.

    Raises :class:`PermanentFailure` when the first answer is final, and
    :class:`RetryExhausted` when the attempts or the total wait budget run out.
    Both carry the verdict, so the caller can decide between "free to retry
    later" and "an operator has to look at this".
    """
    if operation not in OPERATIONS:
        raise ValueError(f"Unknown avatar provider operation: {operation!r}")
    resolved = policy or DEFAULT_POLICIES[operation]
    if sleep is None:
        import time as _time

        sleep = _time.sleep

    waited = 0.0
    for attempt in range(1, resolved.max_attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            verdict = classify(operation, exc, now=now)
            if not verdict.retryable:
                if on_attempt is not None:
                    on_attempt(attempt, verdict, 0.0)
                raise PermanentFailure(verdict, exc) from exc
            if attempt >= resolved.max_attempts:
                if on_attempt is not None:
                    on_attempt(attempt, verdict, 0.0)
                raise RetryExhausted(verdict, exc, attempt) from exc

            delay = backoff_delay(
                resolved,
                attempt,
                retry_after=verdict.retry_after_seconds,
                rand=rand,
            )
            if waited + delay > resolved.max_total_wait_seconds:
                # Stopping here rather than sleeping past the budget: a caller
                # that allotted 90 seconds meant it, and an over-long wait is
                # indistinguishable from a hang to whoever is watching.
                if on_attempt is not None:
                    on_attempt(attempt, verdict, 0.0)
                raise RetryExhausted(verdict, exc, attempt) from exc

            if on_attempt is not None:
                on_attempt(attempt, verdict, delay)
            logger.warning(
                "Avatar %s attempt %d/%d failed (%s), retrying in %.1fs",
                operation,
                attempt,
                resolved.max_attempts,
                verdict.failure_class.value,
                delay,
            )
            waited += delay
            sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise RuntimeError(f"retry loop for {operation} ended without a result")


def idempotency_expiry(issued_at: datetime) -> datetime:
    """When a persisted idempotency key stops protecting a repeated POST."""
    from datetime import timedelta

    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    return issued_at + timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS)


def idempotency_key_is_live(
    expires_at: datetime | None, *, now: Callable[[], datetime] = _utcnow
) -> bool:
    """Is a repeated create still guaranteed to return the original job?"""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now()
