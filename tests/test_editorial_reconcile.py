"""Reconciling blocked provider operations, and refusing to reconcile the rest.

The guard that blocks a retry after an uncertain outcome is correct. What was
missing is any way out of it, which turned one failed free search into a
permanently dead story. These tests pin down both halves: the effect-free case
is cleared, and everything else stays exactly as blocked as it was.
"""

from __future__ import annotations

import pytest

from btcedu.core.editorial.reconcile import reconcile_operations
from btcedu.models.editorial import (
    ProviderOperation,
    ProviderOperationStatus,
    ResearchQuery,
    ResearchQueryStatus,
    ResearchRun,
    Topic,
)


def _research_run(db_session, *, run_id: str) -> ResearchRun:
    topic = Topic(topic_id=run_id, topic_key=f"key-{run_id}", title="Thema")
    db_session.add(topic)
    db_session.flush()
    research_run = ResearchRun(
        run_id=run_id,
        topic_id=topic.id,
        input_hash="i" * 64,
        policy_version="v1",
        status="running",
        max_queries=4,
        max_cost_usd=0.0,
    )
    db_session.add(research_run)
    db_session.commit()
    return research_run


@pytest.fixture
def run(db_session):
    return _research_run(db_session, run_id="run-1")


def _operation(db_session, run, *, op_id, op_type="search", estimated=0.0, actual=None,
               status=ProviderOperationStatus.RECONCILE_REQUIRED.value):
    operation = ProviderOperation(
        operation_id=op_id,
        research_run_id=run.id,
        operation_key=op_id,
        operation_type=op_type,
        provider="free-news",
        model_name="",
        input_hash="h" * 64,
        status=status,
        estimated_cost_usd=estimated,
        actual_cost_usd=actual,
    )
    db_session.add(operation)
    db_session.commit()
    return operation


def test_a_free_search_that_delivered_nothing_is_cleared(db_session, run):
    operation = _operation(db_session, run, op_id="op-free")

    report = reconcile_operations(db_session)

    assert report.resolved == ["op-free"]
    assert operation.status == ProviderOperationStatus.FAILED.value


def test_a_model_call_is_never_cleared_automatically(db_session, run):
    """Its outcome is unknown and it was billed. Only an operator may judge it."""
    operation = _operation(db_session, run, op_id="op-model", op_type="model", estimated=0.02)

    report = reconcile_operations(db_session)

    assert report.resolved == []
    assert report.requires_operator == ["op-model"]
    assert operation.status == ProviderOperationStatus.RECONCILE_REQUIRED.value


def test_a_search_that_cost_money_is_not_cleared(db_session, run):
    """A paid search provider is a bill, so the zero-cost argument does not hold."""
    operation = _operation(db_session, run, op_id="op-paid", estimated=0.004)

    report = reconcile_operations(db_session)

    assert report.requires_operator == ["op-paid"]
    assert operation.status == ProviderOperationStatus.RECONCILE_REQUIRED.value


def test_a_submitted_operation_is_left_untouched(db_session, run):
    """Submitted means the outcome is genuinely in doubt -- the guard's whole point."""
    operation = _operation(
        db_session, run, op_id="op-submitted", status=ProviderOperationStatus.SUBMITTED.value
    )

    report = reconcile_operations(db_session)

    assert report.resolved == []
    assert report.requires_operator == []
    assert operation.status == ProviderOperationStatus.SUBMITTED.value


def test_a_blocked_query_is_released_with_its_operation(db_session, run):
    operation = _operation(db_session, run, op_id="op-q")
    query = ResearchQuery(
        query_id="q-1",
        research_run_id=run.id,
        claim_revision_id=1,
        provider_operation_id=operation.id,
        query_key="k",
        query_text="Thema",
        language="de",
        purpose="evidence",
        status=ResearchQueryStatus.BLOCKED.value,
    )
    db_session.add(query)
    db_session.commit()

    reconcile_operations(db_session)

    assert query.status == ResearchQueryStatus.FAILED.value


def test_reconciliation_can_be_scoped_to_one_run(db_session, run):
    other = _research_run(db_session, run_id="run-2")
    _operation(db_session, run, op_id="op-a")
    elsewhere = _operation(db_session, other, op_id="op-b")

    report = reconcile_operations(db_session, research_run_id=run.id)

    assert report.resolved == ["op-a"]
    assert elsewhere.status == ProviderOperationStatus.RECONCILE_REQUIRED.value


# ---------------------------------------------------------------------------
# The evidence passage check, which rejected passages that were in the document
# ---------------------------------------------------------------------------


def test_a_passage_is_found_across_typographic_differences():
    """The extracted body and the model differ in typography, not in wording."""
    from btcedu.core.editorial.research import _anchor_text

    document = "Die Ministerin ( SPD ) sagte:\n\u201eWir prüfen das\u201c – so wört\u00adlich."
    passage = 'Die Ministerin (SPD) sagte: "Wir prüfen das" - so wörtlich.'

    assert passage not in document, "the raw comparison is what failed in production"
    assert _anchor_text(passage) in _anchor_text(document)


def test_an_invented_passage_is_still_rejected():
    from btcedu.core.editorial.research import _anchor_text

    document = "Die Ministerin kündigte eine Prüfung an."
    assert _anchor_text("Die Ministerin trat zurück.") not in _anchor_text(document)


def test_a_quote_across_a_headline_boundary_is_accepted():
    """Extraction does not punctuate where a headline meets its paragraph.

    Taken verbatim from a rejected production passage: the document runs
    "...Versprechen den Republikanern...", the model wrote the full stop that
    German orthography demands. The words are identical.
    """
    from btcedu.core.editorial.research import _anchor_text, _word_sequence

    document = (
        "Trump lockt Wähler mit 5.000 -Dollar-Versprechen "
        "Den Republikanern droht eine Schlappe."
    )
    passage = (
        "Trump lockt Wähler mit 5.000 -Dollar-Versprechen. "
        "Den Republikanern droht eine Schlappe."
    )

    assert _anchor_text(passage) not in _anchor_text(document)
    assert _word_sequence(passage) in _word_sequence(document)


def test_the_word_sequence_fallback_still_rejects_a_passage_that_is_absent():
    """The second production case: quoted from a search snippet, not the page."""
    from btcedu.core.editorial.research import _word_sequence

    document = "Das Bundesamt veröffentlichte die Zahlen für das vergangene Jahr."
    passage = "Die Zahl der Empfänger ist auf 390.000 gesunken."

    assert _word_sequence(passage) not in _word_sequence(document)


def test_a_changed_number_is_not_absorbed_by_the_fallback():
    from btcedu.core.editorial.research import _word_sequence

    document = "Die Zahl sank auf 390.000 Menschen."
    assert _word_sequence("Die Zahl sank auf 490.000 Menschen.") not in _word_sequence(document)


def test_an_added_negation_is_not_absorbed_by_the_fallback():
    from btcedu.core.editorial.research import _word_sequence

    document = "Die Ministerin hat den Vorschlag unterstützt."
    assert _word_sequence("Die Ministerin hat den Vorschlag nicht unterstützt.") not in (
        _word_sequence(document)
    )


def test_the_fallback_cannot_match_inside_a_longer_word():
    from btcedu.core.editorial.research import _word_sequence

    assert _word_sequence("rat") not in _word_sequence("Der Vorrat ist erschöpft.")
