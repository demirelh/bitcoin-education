"""Operational and editorial figures counted from the newsroom's own rows (N8).

The report exists to make two things visible that prose hides: how much of the
evidence really comes from one place, and what is waiting for a person. The
tests below therefore care less about formatting than about whether a
syndicated story can look like agreement between independent sources.
"""

from __future__ import annotations

import json

from btcedu.core.editorial.article import (
    approve_article_revision,
    article_preview,
    generate_article_revision,
)
from btcedu.core.editorial.recheck import open_issue
from btcedu.core.editorial.report import (
    build_report,
    format_report,
    report_warnings,
)
from btcedu.models.editorial import (
    EvidenceLink,
    ProviderOperation,
    ProviderOperationStatus,
    SourceObservation,
)
from tests.test_editorial_article import _draft, _pipeline


def _run_cli(db_session, args: list[str]):
    from click.testing import CliRunner

    from btcedu.cli import cli

    return CliRunner().invoke(cli, args, obj={"session_factory": lambda: db_session})


def test_an_empty_newsroom_reports_nothing_rather_than_failing(db_session):
    report = build_report(db_session)

    assert report.sources.evidence_links == 0
    assert report.cost.total_cost_usd == 0.0
    assert report_warnings(report) == ()
    assert "(none)" in format_report(report)


def test_the_report_counts_verdicts_evidence_and_cost(db_session, tmp_path):
    _pipeline(db_session, tmp_path)

    report = build_report(db_session)

    assert report.claims_by_verdict
    assert report.sources.evidence_links > 0
    assert report.cost.research_runs == 1
    assert report.fetches.observations > 0


def test_evidence_from_one_family_is_counted_as_one_source(db_session, tmp_path):
    """Three URLs from the same wire are one witness, not three."""
    _pipeline(db_session, tmp_path)
    link = db_session.query(EvidenceLink).first()
    observation = db_session.get(SourceObservation, link.source_observation_id)
    observation.provenance_family = "dpa"
    link.provenance_family = "dpa"
    db_session.commit()

    report = build_report(db_session)

    families = dict(report.sources.top_families)
    assert "dpa" in families
    assert report.sources.distinct_families <= report.sources.evidence_links


def test_a_claim_resting_on_a_single_family_is_named(db_session, tmp_path):
    _pipeline(db_session, tmp_path)
    for link in db_session.query(EvidenceLink).all():
        link.provenance_family = "dpa"
    db_session.commit()

    report = build_report(db_session)
    warnings = report_warnings(report)

    assert report.sources.single_family_claims
    assert any("single provenance family" in warning for warning in warnings)


def test_a_claim_with_no_supporting_evidence_is_named(db_session, tmp_path):
    _pipeline(db_session, tmp_path, relation="contradicts")

    report = build_report(db_session)
    warnings = report_warnings(report)

    assert report.sources.unsupported_claims
    assert any("supporting evidence" in warning for warning in warnings)


def test_a_dominant_source_is_called_what_it_is(db_session, tmp_path):
    _pipeline(db_session, tmp_path)
    for link in db_session.query(EvidenceLink).all():
        link.provenance_family = "dpa"
    db_session.commit()

    warnings = report_warnings(build_report(db_session))

    assert any("not a consensus" in warning for warning in warnings)


def test_failed_provider_operations_are_reported(db_session, tmp_path):
    _pipeline(db_session, tmp_path)
    operation = db_session.query(ProviderOperation).first()
    operation.status = ProviderOperationStatus.FAILED.value
    db_session.commit()

    report = build_report(db_session)

    assert report.cost.failed_operations == 1
    assert any("failed" in warning for warning in report_warnings(report))


def test_rate_limited_fetches_are_reported(db_session, tmp_path):
    _pipeline(db_session, tmp_path)
    observation = db_session.query(SourceObservation).first()
    observation.http_status = 429
    db_session.commit()

    report = build_report(db_session)

    assert report.fetches.rate_limited == 1
    assert any("rate limited" in warning for warning in report_warnings(report))


def test_open_issues_and_pending_rechecks_are_surfaced(db_session, tmp_path):
    open_issue(
        db_session,
        kind="licence_changed",
        ref="commons:File:Beispiel.jpg",
        detail="Lizenz unklar",
    )

    report = build_report(db_session)

    assert report.open_issues == 1
    assert any("waiting for a person" in warning for warning in report_warnings(report))


def test_an_approved_article_that_never_reached_readers_is_flagged(db_session, tmp_path):
    revision, run, _ = _pipeline(db_session, tmp_path)
    article = generate_article_revision(
        db_session,
        editorial_revision=revision,
        research_run=run,
        drafter=lambda payload: _draft(),
    )
    preview = article_preview(db_session, article, research_run=run)
    approve_article_revision(
        db_session,
        article,
        research_run=run,
        operator_ref="web:editor",
        reviewed_content_hash=preview["content_hash"],
        reviewed_evidence_hash=preview["evidence_hash"],
        reviewed_media_hash=preview["media_hash"],
    )

    warnings = report_warnings(build_report(db_session))

    assert any("not published" in warning for warning in warnings)


def test_the_report_never_reaches_the_network(db_session):
    """Every figure is a count over stored rows."""
    import ast
    import inspect

    from btcedu.core.editorial import report as report_module

    tree = ast.parse(inspect.getsource(report_module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")

    assert not any(
        module.split(".")[0] in {"requests", "httpx", "urllib3", "socket"}
        for module in names
    )


def test_the_cli_prints_a_report_and_can_emit_json(db_session, tmp_path):
    _pipeline(db_session, tmp_path)

    text = _run_cli(db_session, ["newsroom-report"])
    assert text.exit_code == 0, text.output
    assert "claims by verdict" in text.output

    machine = _run_cli(db_session, ["newsroom-report", "--json"])
    assert machine.exit_code == 0
    payload = json.loads(machine.output)
    assert payload["cost"]["research_runs"] == 1
    assert "warnings" in payload
