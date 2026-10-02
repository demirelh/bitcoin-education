"""Known provider outcomes are not uncertain ones.

The daily run lost sixteen stories to "Uncertain model operation requires
reconciliation". Each had begun with a call the provider plainly refused --
an empty OpenAI balance answered with HTTP 429 -- which was nevertheless filed
as an outcome nobody could know. The retry the next evening then hit the guard
and the story was dead. These tests pin down the split: a refusal and a paid
but unusable reply are decided; only a call whose fate is unknown stays
blocked.
"""

from __future__ import annotations

import openai
import pytest

from btcedu.core.editorial.jobs import (
    ModelReplyUnusable,
    provider_account_unusable,
    provider_rejection_status,
    reserved_cost_usd,
)
from btcedu.core.editorial.limits import DailyLedger, DailyLimits, LedgerGuardedModel, today_key
from btcedu.core.editorial.workflow import BudgetedCaller, ModelReply
from btcedu.models.editorial import ProviderOperation, ProviderOperationStatus, ResearchRun, Topic

try:  # the openai SDK ships its own fork of httpx in recent releases
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
NO_CREDITS = {
    "message": "You have no credits remaining.",
    "type": "insufficient_quota",
    "code": "credit_balance_exhausted",
}


def _status_error(cls, status: int, error: dict):
    body = {"error": error}
    return cls(
        f"Error code: {status} - {body}",
        response=httpx.Response(status, request=_REQUEST, json=body),
        body=error,
    )


def _no_credits():
    return _status_error(openai.RateLimitError, 429, NO_CREDITS)


def _rate_limited():
    return _status_error(
        openai.RateLimitError, 429, {"message": "Rate limit reached", "code": "rate_limit"}
    )


@pytest.fixture
def run(db_session):
    topic = Topic(topic_id="t-1", topic_key="key-1", title="Thema")
    db_session.add(topic)
    db_session.flush()
    research_run = ResearchRun(
        run_id="run-1",
        topic_id=topic.id,
        input_hash="i" * 64,
        policy_version="v1",
        status="running",
        max_queries=4,
        max_cost_usd=1.0,
    )
    db_session.add(research_run)
    db_session.commit()
    return research_run


def _caller(db_session, run, model):
    return BudgetedCaller(
        db_session, run, model, provider="openai", model="m", max_call_cost_usd=0.12
    )


def _operation(db_session) -> ProviderOperation:
    return db_session.query(ProviderOperation).filter_by(operation_type="llm").one()


PAYLOAD = {"task": "extract_claims", "text": "Berlin meldet 100 neue Wohnungen."}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_refusal_carries_its_status_and_an_unknown_outcome_does_not():
    assert provider_rejection_status(_no_credits()) == 429
    assert provider_rejection_status(openai.APITimeoutError(request=_REQUEST)) is None
    server = _status_error(openai.InternalServerError, 500, {"message": "boom"})
    assert provider_rejection_status(server) is None
    assert provider_rejection_status(RuntimeError("anything")) is None


def test_only_an_account_refusal_stops_the_whole_run():
    assert provider_account_unusable(_no_credits())
    assert provider_account_unusable(
        _status_error(openai.AuthenticationError, 401, {"message": "Incorrect API key"})
    )
    # Plain throttling and a bad request concern this call, not the account.
    assert not provider_account_unusable(_rate_limited())
    assert not provider_account_unusable(
        _status_error(openai.BadRequestError, 400, {"message": "bad schema"})
    )
    # Words alone never stop a run: the provider must actually have answered.
    assert not provider_account_unusable(RuntimeError("no credits remaining"))


# ---------------------------------------------------------------------------
# The per-story operation record
# ---------------------------------------------------------------------------


def test_a_refused_call_is_closed_and_the_next_attempt_goes_through(db_session, run):
    def refusing(payload):
        raise _no_credits()

    with pytest.raises(openai.RateLimitError):
        _caller(db_session, run, refusing)(PAYLOAD)

    operation = _operation(db_session)
    assert operation.status == ProviderOperationStatus.FAILED.value
    assert operation.actual_cost_usd == 0.0
    assert "HTTP 429" in operation.error_message

    result = _caller(db_session, run, lambda payload: ModelReply(result=["ok"], cost_usd=0.01))(
        PAYLOAD
    )
    assert result == ["ok"]
    assert _operation(db_session).status == ProviderOperationStatus.COMPLETED.value


def test_a_call_whose_fate_is_unknown_stays_blocked(db_session, run):
    """The guard this fix must not soften."""

    def timing_out(payload):
        raise openai.APITimeoutError(request=_REQUEST)

    with pytest.raises(openai.APITimeoutError):
        _caller(db_session, run, timing_out)(PAYLOAD)
    assert _operation(db_session).status == ProviderOperationStatus.RECONCILE_REQUIRED.value

    sent = []
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        _caller(db_session, run, lambda payload: sent.append(payload))(PAYLOAD)
    assert sent == []


def test_a_paid_reply_that_cannot_be_used_keeps_its_bill(db_session, run):
    def garbled(payload):
        raise ModelReplyUnusable("reply is not JSON", cost_usd=0.03)

    with pytest.raises(ModelReplyUnusable):
        _caller(db_session, run, garbled)(PAYLOAD)

    operation = _operation(db_session)
    assert operation.status == ProviderOperationStatus.FAILED.value
    assert operation.actual_cost_usd == pytest.approx(0.03)
    assert reserved_cost_usd(db_session, run.id) == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# The daily ledger
# ---------------------------------------------------------------------------


def _guarded(tmp_path, model):
    return LedgerGuardedModel(
        model,
        ledger=DailyLedger(tmp_path / "ledger.sqlite"),
        limits=DailyLimits(budget_usd=1.0, max_calls=9, max_stories=3),
        max_tokens_by_task={"extract_claims": 100},
    )


def test_a_refused_call_costs_the_day_nothing(tmp_path):
    def refusing(payload):
        raise _no_credits()

    with pytest.raises(openai.RateLimitError):
        _guarded(tmp_path, refusing)(PAYLOAD)

    ledger = DailyLedger(tmp_path / "ledger.sqlite")
    assert ledger.spent_usd(today_key()) == 0.0
    assert ledger.call_count(today_key()) == 1


def test_an_unusable_reply_is_booked_at_its_actual_price(tmp_path):
    def garbled(payload):
        raise ModelReplyUnusable("reply is not JSON", cost_usd=0.004)

    with pytest.raises(ModelReplyUnusable):
        _guarded(tmp_path, garbled)(PAYLOAD)

    assert DailyLedger(tmp_path / "ledger.sqlite").spent_usd(today_key()) == pytest.approx(0.004)


def test_an_unknown_failure_still_books_the_whole_reservation(tmp_path):
    def timing_out(payload):
        raise openai.APITimeoutError(request=_REQUEST)

    guarded = _guarded(tmp_path, timing_out)
    with pytest.raises(openai.APITimeoutError):
        guarded(PAYLOAD)

    assert DailyLedger(tmp_path / "ledger.sqlite").spent_usd(today_key()) == pytest.approx(
        guarded.maximum_cost_usd(PAYLOAD)
    )


# ---------------------------------------------------------------------------
# The model adapter
# ---------------------------------------------------------------------------


def test_a_reply_that_is_not_json_is_reported_with_its_cost(monkeypatch):
    from btcedu.services import claude_service
    from btcedu.services.editorial_model import EditorialModel

    class _Response:
        text = "Sorry, I cannot help with that."
        cost_usd = 0.002
        input_tokens = 10
        output_tokens = 5
        model = "m"

    monkeypatch.setattr(claude_service, "call_claude", lambda *a, **k: _Response())
    model = EditorialModel(object(), provider="openai", model="m")

    with pytest.raises(ModelReplyUnusable) as raised:
        model(PAYLOAD)
    assert raised.value.cost_usd == pytest.approx(0.002)
