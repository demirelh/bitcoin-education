"""Running stories, fan-out and rechecks (N6).

The questions worth asking here are about restraint: what does the system
refuse to link, what does it refuse to decide, and what does a change to one
document reach. A recheck that could approve something would defeat every gate
in front of it, so one test reads the source to make sure none exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from btcedu.core.editorial.article import (
    approve_article_revision,
    article_preview,
    generate_article_revision,
)
from btcedu.core.editorial.public import publish_article, withdraw_publication
from btcedu.core.editorial.recheck import (
    affected_publications,
    open_issue,
    pending_rechecks,
    purge_evidence_cache,
    queue_rechecks,
    record_dependencies,
    resolve_issue,
    run_recheck,
)
from btcedu.core.editorial.topics import (
    MIN_KEYWORD_OVERLAP,
    TopicGraphError,
    accept_proposal,
    add_alias,
    canonical_topic,
    match_signals,
    merge_topics,
    normalize_terms,
    propose_updates,
    reject_proposal,
    revert_merge,
)
from btcedu.models.editorial import (
    ClaimAssessment,
    SourceItem,
    SourceRevision,
    Topic,
    TopicSource,
)
from btcedu.models.topic_graph import (
    AliasKind,
    DependencyKind,
    IssueStatus,
    MergeStatus,
    ProposalStatus,
    PublicationDependency,
    RecheckJob,
    RecheckStatus,
    SourceIssue,
    UpdateProposal,
)
from tests.test_editorial_article import _draft, _pipeline


def _topic(db_session, title: str) -> Topic:
    topic = Topic(
        topic_id=str(uuid.uuid4()),
        topic_key=f"key-{uuid.uuid4()}",
        title=title,
    )
    db_session.add(topic)
    db_session.commit()
    return topic


def _revision(db_session, *, title: str, text: str = "", published=None) -> SourceRevision:
    item = SourceItem(
        source_id=str(uuid.uuid4()),
        source_key=f"src-{uuid.uuid4()}",
        source_type="transcript",
        episode_id="episode-1",
    )
    db_session.add(item)
    db_session.flush()
    revision = SourceRevision(
        revision_id=str(uuid.uuid4()),
        source_item_id=item.id,
        content_hash=uuid.uuid4().hex * 2,
        language="de",
        title=title,
        source_text=text or title,
        published_at=published,
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(revision)
    db_session.commit()
    return revision


# ---------------------------------------------------------------------------
# What counts as the same story
# ---------------------------------------------------------------------------


def test_stopwords_never_carry_topic_identity():
    assert "und" not in normalize_terms("Berlin und Wohnungsbau")
    assert set(normalize_terms("Berlin und Wohnungsbau")) == {"berlin", "wohnungsbau"}


def test_a_shared_date_alone_does_not_join_two_stories(db_session):
    """Everything in one bulletin shares a date. That is not a relationship."""
    topic = _topic(db_session, "Wohnungsbau in Berlin")
    add_alias(
        db_session, topic, kind=AliasKind.EVENT_DATE, value="2026-09-09"
    )
    revision = _revision(
        db_session,
        title="Bundesliga Ergebnisse",
        text="Der Spieltag endete mit drei Unentschieden.",
        published=datetime(2026, 9, 9, tzinfo=UTC),
    )

    signals = match_signals(db_session, topic, revision)

    assert signals.date_distance_days == 0
    assert not signals.sufficient()
    assert propose_updates(db_session, revision, topics=[topic]) == []


def test_the_same_person_in_a_different_event_does_not_join(db_session):
    topic = _topic(db_session, "Ruecktritt der Ministerin Meier")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="meier")
    revision = _revision(
        db_session,
        title="Meier eroeffnet Bruecke",
        text="Die Ministerin Meier eroeffnete eine Bruecke.",
    )

    signals = match_signals(db_session, topic, revision)

    assert signals.entities == ("meier",)
    assert len(signals.keywords) < MIN_KEYWORD_OVERLAP or not signals.sufficient()
    assert not signals.sufficient()


def test_a_continued_story_becomes_a_proposal_not_a_second_article(db_session):
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: neue Foerderung beschlossen",
        text="Der Senat beschloss die Foerderung fuer den Wohnungsbau in Berlin.",
    )

    proposals = propose_updates(db_session, revision, topics=[topic])

    assert len(proposals) == 1
    assert proposals[0].status == ProposalStatus.OPEN.value
    assert proposals[0].score > 0
    # Nothing was attached and nothing was published.
    assert db_session.query(TopicSource).count() == 0


def test_proposing_twice_does_not_duplicate_the_suggestion(db_session):
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: neue Foerderung",
        text="Foerderung fuer den Wohnungsbau in Berlin beschlossen.",
    )
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")

    propose_updates(db_session, revision, topics=[topic])
    propose_updates(db_session, revision, topics=[topic])

    assert db_session.query(UpdateProposal).count() == 1


def test_accepting_a_proposal_attaches_the_source_and_names_the_operator(db_session):
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: neue Foerderung",
        text="Foerderung fuer den Wohnungsbau in Berlin beschlossen.",
    )
    proposal = propose_updates(db_session, revision, topics=[topic])[0]

    accept_proposal(db_session, proposal, operator_ref="web:editor")

    assert proposal.status == ProposalStatus.ACCEPTED.value
    assert proposal.operator_ref == "web:editor"
    assert db_session.query(TopicSource).count() == 1


def test_a_proposal_cannot_be_accepted_anonymously(db_session):
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: neue Foerderung",
        text="Foerderung fuer den Wohnungsbau in Berlin beschlossen.",
    )
    proposal = propose_updates(db_session, revision, topics=[topic])[0]

    with pytest.raises(ValueError):
        accept_proposal(db_session, proposal, operator_ref=" ")
    assert db_session.query(TopicSource).count() == 0


def test_a_rejected_proposal_cannot_be_accepted_later(db_session):
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: neue Foerderung",
        text="Foerderung fuer den Wohnungsbau in Berlin beschlossen.",
    )
    proposal = propose_updates(db_session, revision, topics=[topic])[0]
    reject_proposal(db_session, proposal, operator_ref="web:editor")

    with pytest.raises(TopicGraphError):
        accept_proposal(db_session, proposal, operator_ref="web:editor")


def test_one_broadcast_can_feed_several_topics(db_session):
    housing = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    senate = _topic(db_session, "Senat Berlin Haushalt Foerderung")
    for topic in (housing, senate):
        add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    revision = _revision(
        db_session,
        title="Senat in Berlin beschliesst Foerderung fuer Wohnungsbau",
        text="Der Senat in Berlin beschloss die Foerderung fuer den Wohnungsbau.",
    )

    proposals = propose_updates(db_session, revision, topics=[housing, senate])

    assert len(proposals) == 2


# ---------------------------------------------------------------------------
# Merge and split
# ---------------------------------------------------------------------------


def _merged_pair(db_session):
    primary = _topic(db_session, "Wohnungsbau Berlin")
    other = _topic(db_session, "Berliner Wohnungsbau")
    own = _revision(db_session, title="Erste Meldung")
    shared = _revision(db_session, title="Zweite Meldung")
    db_session.add(TopicSource(topic_id=primary.id, source_revision_id=own.id))
    db_session.add(TopicSource(topic_id=other.id, source_revision_id=shared.id))
    db_session.commit()
    return primary, other, own, shared


def test_a_merge_needs_an_operator_and_a_readable_reason(db_session):
    primary, other, _, _ = _merged_pair(db_session)

    with pytest.raises(ValueError):
        merge_topics(
            db_session,
            primary=primary,
            merged=other,
            operator_ref="",
            rationale="Gleiche Sache",
        )
    with pytest.raises(ValueError):
        merge_topics(
            db_session,
            primary=primary,
            merged=other,
            operator_ref="web:editor",
            rationale="   ",
        )
    assert db_session.query(TopicSource).filter_by(topic_id=primary.id).count() == 1


def test_a_merge_carries_the_sources_over_and_survives_a_revert(db_session):
    primary, other, own, shared = _merged_pair(db_session)

    merge = merge_topics(
        db_session,
        primary=primary,
        merged=other,
        operator_ref="web:editor",
        rationale="Dieselbe Sendungssache an zwei Abenden",
    )
    assert db_session.query(TopicSource).filter_by(topic_id=primary.id).count() == 2
    assert canonical_topic(db_session, other).id == primary.id

    revert_merge(db_session, merge, operator_ref="web:chief")

    assert merge.status == MergeStatus.REVERTED.value
    assert merge.reverted_by == "web:chief"
    remaining = {
        link.source_revision_id
        for link in db_session.query(TopicSource).filter_by(topic_id=primary.id)
    }
    assert remaining == {own.id}
    assert db_session.query(TopicSource).filter_by(topic_id=other.id).count() == 1
    assert canonical_topic(db_session, other).id == other.id


def test_a_revert_keeps_a_source_the_primary_topic_already_had(db_session):
    """The merged topic repeated a source. Reverting must not steal it."""
    primary, other, own, shared = _merged_pair(db_session)
    db_session.add(TopicSource(topic_id=other.id, source_revision_id=own.id))
    db_session.commit()

    merge = merge_topics(
        db_session,
        primary=primary,
        merged=other,
        operator_ref="web:editor",
        rationale="Gleiche Sache",
    )
    revert_merge(db_session, merge, operator_ref="web:editor")

    remaining = {
        link.source_revision_id
        for link in db_session.query(TopicSource).filter_by(topic_id=primary.id)
    }
    assert remaining == {own.id}


def test_a_topic_cannot_be_merged_into_itself(db_session):
    topic = _topic(db_session, "Wohnungsbau")

    with pytest.raises(TopicGraphError):
        merge_topics(
            db_session,
            primary=topic,
            merged=topic,
            operator_ref="web:editor",
            rationale="Unsinn",
        )


def test_a_topic_merged_away_stops_receiving_proposals(db_session):
    primary, other, _, _ = _merged_pair(db_session)
    for topic in (primary, other):
        add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
        add_alias(db_session, topic, kind=AliasKind.KEYWORD, value="foerderung")
    merge_topics(
        db_session,
        primary=primary,
        merged=other,
        operator_ref="web:editor",
        rationale="Gleiche Sache",
    )
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: Foerderung geht weiter",
        text="Die Foerderung fuer den Wohnungsbau in Berlin geht weiter.",
    )

    proposals = propose_updates(db_session, revision, topics=[primary, other])

    assert [p.topic_id for p in proposals] == [primary.id]


# ---------------------------------------------------------------------------
# Dependencies and fan-out
# ---------------------------------------------------------------------------


def _published(db_session, tmp_path):
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
    publication = publish_article(db_session, article, operator_ref="web:editor")
    return publication, article, run


def test_publishing_records_what_the_article_stands_on(db_session, tmp_path):
    publication, _, _ = _published(db_session, tmp_path)

    kinds = {
        row.kind
        for row in db_session.query(PublicationDependency).filter_by(
            publication_id=publication.id
        )
    }

    assert DependencyKind.SOURCE_OBSERVATION.value in kinds
    assert DependencyKind.MEDIA_ASSET.value in kinds


def test_recording_dependencies_twice_is_harmless(db_session, tmp_path):
    publication, _, _ = _published(db_session, tmp_path)
    before = db_session.query(PublicationDependency).count()

    record_dependencies(db_session, publication)

    assert db_session.query(PublicationDependency).count() == before


def test_a_changed_asset_finds_every_page_that_uses_it(db_session, tmp_path):
    from btcedu.models.media_rights import NewsroomMediaAsset

    publication, _, _ = _published(db_session, tmp_path)
    asset = db_session.query(NewsroomMediaAsset).one()

    found = affected_publications(
        db_session, kind=DependencyKind.MEDIA_ASSET, ref=asset.asset_id
    )

    assert [row.id for row in found] == [publication.id]


def test_a_licence_change_queues_one_bounded_recheck_per_page(db_session, tmp_path):
    from btcedu.models.media_rights import NewsroomMediaAsset

    publication, _, _ = _published(db_session, tmp_path)
    asset = db_session.query(NewsroomMediaAsset).one()

    first = queue_rechecks(
        db_session,
        kind=DependencyKind.MEDIA_ASSET,
        ref=asset.asset_id,
        reason="license_changed",
    )
    second = queue_rechecks(
        db_session,
        kind=DependencyKind.MEDIA_ASSET,
        ref=asset.asset_id,
        reason="license_changed",
    )

    assert len(first) == 1
    assert [job.job_id for job in second] == [job.job_id for job in first]
    assert db_session.query(RecheckJob).count() == 1
    assert len(pending_rechecks(db_session)) == 1


def test_the_fanout_is_capped(db_session, tmp_path):
    publication, _, _ = _published(db_session, tmp_path)
    for index in range(4):
        db_session.add(
            PublicationDependency(
                publication_id=publication.id,
                kind=DependencyKind.SOURCE_REVISION.value,
                ref=f"extra-{index}",
            )
        )
    db_session.commit()

    jobs = queue_rechecks(
        db_session,
        kind=DependencyKind.SOURCE_REVISION,
        ref="extra-0",
        reason="source_changed",
        max_fanout=1,
    )

    assert len(jobs) == 1


def test_a_recheck_can_block_but_never_approve(db_session, tmp_path):
    publication, article, _ = _published(db_session, tmp_path)
    assessment = db_session.query(ClaimAssessment).one()
    assessment.verdict = "insufficient"
    db_session.commit()
    job = queue_rechecks(
        db_session,
        kind=DependencyKind.SOURCE_OBSERVATION,
        ref=db_session.query(PublicationDependency)
        .filter_by(kind=DependencyKind.SOURCE_OBSERVATION.value)
        .first()
        .ref,
        reason="source_changed",
    )[0]

    outcome = run_recheck(db_session, job)

    assert outcome.status == RecheckStatus.BLOCKED.value
    assert "insufficient" in outcome.detail
    # The publication itself is untouched: withdrawing is an operator's word.
    assert publication.status != "withdrawn"


def test_a_clean_recheck_says_so_without_changing_anything(db_session, tmp_path):
    publication, _, _ = _published(db_session, tmp_path)
    job = queue_rechecks(
        db_session,
        kind=DependencyKind.MEDIA_ASSET,
        ref=db_session.query(PublicationDependency)
        .filter_by(kind=DependencyKind.MEDIA_ASSET.value)
        .first()
        .ref,
        reason="license_changed",
    )[0]

    outcome = run_recheck(db_session, job)

    assert outcome.status == RecheckStatus.CLEAR.value
    assert publication.status == "published"


def test_a_review_reason_asks_for_a_person(db_session, tmp_path):
    _published(db_session, tmp_path)
    job = queue_rechecks(
        db_session,
        kind=DependencyKind.MEDIA_ASSET,
        ref=db_session.query(PublicationDependency)
        .filter_by(kind=DependencyKind.MEDIA_ASSET.value)
        .first()
        .ref,
        reason="review:metadata_changed",
    )[0]

    outcome = run_recheck(db_session, job)

    assert outcome.status == RecheckStatus.REVIEW_REQUESTED.value


def test_a_withdrawn_page_does_not_keep_raising_alarms(db_session, tmp_path):
    publication, _, _ = _published(db_session, tmp_path)
    withdraw_publication(
        db_session,
        publication,
        operator_ref="web:editor",
        summary="Kaynak iddiayı geri çekti.",
    )
    job = queue_rechecks(
        db_session,
        kind=DependencyKind.MEDIA_ASSET,
        ref=db_session.query(PublicationDependency)
        .filter_by(kind=DependencyKind.MEDIA_ASSET.value)
        .first()
        .ref,
        reason="license_changed",
    )[0]

    outcome = run_recheck(db_session, job)

    assert outcome.status == RecheckStatus.CLEAR.value


def test_no_recheck_path_can_publish_or_approve():
    """Read the module: there must be no way out of it into a release."""
    import inspect

    from btcedu.core.editorial import recheck as module

    source = inspect.getsource(module)

    assert "publish_article" not in source
    assert "approve_article_revision" not in source
    assert "switch_release" not in source


# ---------------------------------------------------------------------------
# The inbox
# ---------------------------------------------------------------------------


def test_the_same_problem_is_reported_once(db_session):
    open_issue(db_session, kind="source_gone", ref="obs-1", detail="404")
    open_issue(db_session, kind="source_gone", ref="obs-1", detail="404 again")

    assert db_session.query(SourceIssue).count() == 1


def test_a_resolved_problem_can_reappear(db_session):
    issue = open_issue(db_session, kind="source_gone", ref="obs-1")
    resolve_issue(db_session, issue)

    reopened = open_issue(db_session, kind="source_gone", ref="obs-1")

    assert reopened.id != issue.id
    assert issue.status == IssueStatus.RESOLVED.value
    assert reopened.status == IssueStatus.OPEN.value


# ---------------------------------------------------------------------------
# Retention: proof is permanent, bytes are not
# ---------------------------------------------------------------------------


def test_cached_bytes_expire_but_the_publication_record_does_not(
    db_session, tmp_path
):
    from btcedu.models.editorial import SourceObservation

    publication, _, _ = _published(db_session, tmp_path)
    observation = db_session.query(SourceObservation).first()
    body = tmp_path / "cached.html"
    body.write_text("<html></html>", encoding="utf-8")
    observation.body_path = str(body)
    observation.retrieved_at = datetime.now(UTC) - timedelta(days=400)
    db_session.commit()

    removed, kept = purge_evidence_cache(
        db_session, older_than=datetime.now(UTC) - timedelta(days=30)
    )

    assert removed == 0
    assert kept >= 1
    assert body.is_file()
    assert (
        db_session.query(PublicationDependency)
        .filter_by(publication_id=publication.id)
        .count()
        > 0
    )


def test_an_unused_old_document_loses_its_bytes_only(db_session, tmp_path):
    from btcedu.models.editorial import SourceObservation

    _published(db_session, tmp_path)
    body = tmp_path / "orphan.html"
    body.write_text("<html></html>", encoding="utf-8")
    orphan = SourceObservation(
        observation_id=str(uuid.uuid4()),
        observation_key=uuid.uuid4().hex,
        research_run_id=db_session.query(SourceObservation).first().research_run_id,
        requested_url="https://old.example/x",
        canonical_url="https://old.example/x",
        url_digest=uuid.uuid4().hex,
        retrieved_at=datetime.now(UTC) - timedelta(days=400),
        body_path=str(body),
    )
    db_session.add(orphan)
    db_session.commit()

    removed, _ = purge_evidence_cache(
        db_session, older_than=datetime.now(UTC) - timedelta(days=30)
    )

    assert removed == 1
    assert not body.is_file()
    assert db_session.get(SourceObservation, orphan.id) is not None
    assert db_session.get(SourceObservation, orphan.id).body_path is None


def test_a_dry_run_deletes_nothing(db_session, tmp_path):
    from btcedu.models.editorial import SourceObservation

    _published(db_session, tmp_path)
    body = tmp_path / "orphan2.html"
    body.write_text("<html></html>", encoding="utf-8")
    orphan = SourceObservation(
        observation_id=str(uuid.uuid4()),
        observation_key=uuid.uuid4().hex,
        research_run_id=db_session.query(SourceObservation).first().research_run_id,
        requested_url="https://old.example/y",
        canonical_url="https://old.example/y",
        url_digest=uuid.uuid4().hex,
        retrieved_at=datetime.now(UTC) - timedelta(days=400),
        body_path=str(body),
    )
    db_session.add(orphan)
    db_session.commit()

    removed, _ = purge_evidence_cache(
        db_session, older_than=datetime.now(UTC) - timedelta(days=30), dry_run=True
    )

    assert removed == 1
    assert body.is_file()
    assert db_session.get(SourceObservation, orphan.id).body_path == str(body)


def test_a_crash_between_file_and_row_leaves_a_cache_miss_not_a_lie(
    db_session, tmp_path
):
    """The file is removed first, so a row can never promise bytes that are gone."""
    import inspect

    from btcedu.core.editorial import recheck as module

    source = inspect.getsource(module.purge_evidence_cache)
    unlink_at = source.index("path.unlink()")
    clear_at = source.index("observation.body_path = None")

    assert unlink_at < clear_at


def _run_cli(db_session, args: list[str]):
    from click.testing import CliRunner

    from btcedu.cli import cli

    return CliRunner().invoke(cli, args, obj={"session_factory": lambda: db_session})


def test_the_cli_lists_and_accepts_a_proposal_under_a_named_operator(db_session):
    """Every graph change through the CLI carries the person who made it."""
    topic = _topic(db_session, "Wohnungsbau Berlin Foerderung")
    add_alias(db_session, topic, kind=AliasKind.ENTITY, value="berlin")
    add_alias(db_session, topic, kind=AliasKind.KEYWORD, value="foerderung")
    revision = _revision(
        db_session,
        title="Wohnungsbau in Berlin: Foerderung geht weiter",
        text="Die Foerderung fuer den Wohnungsbau in Berlin geht weiter.",
    )
    [proposal] = propose_updates(db_session, revision, topics=[topic])
    proposal_id = proposal.proposal_id

    listing = _run_cli(db_session, ["topics", "proposals"])
    assert listing.exit_code == 0
    assert proposal_id in listing.output

    accepted = _run_cli(
        db_session,
        ["topics", "accept", proposal_id, "--operator", "nadine"],
    )
    assert accepted.exit_code == 0
    stored = db_session.query(UpdateProposal).filter_by(proposal_id=proposal_id).one()
    assert stored.status == ProposalStatus.ACCEPTED.value
    assert stored.operator_ref == "cli:nadine"
    assert (
        db_session.query(TopicSource)
        .filter_by(topic_id=stored.topic_id, source_revision_id=stored.source_revision_id)
        .one_or_none()
        is not None
    )


def test_the_cli_refuses_an_unknown_proposal_instead_of_guessing(db_session):
    result = _run_cli(
        db_session, ["topics", "accept", "does-not-exist", "--operator", "nadine"]
    )
    assert result.exit_code != 0
    assert "Unknown proposal" in result.output


def test_the_cli_shows_open_issues_and_marks_them_resolved(db_session):
    issue = open_issue(
        db_session,
        kind="licence_changed",
        ref="commons:File:Beispiel.jpg",
        detail="Lizenz von CC BY-SA auf unklar gewechselt",
    )

    issue_id = issue.issue_id

    listing = _run_cli(db_session, ["recheck", "issues"])
    assert listing.exit_code == 0
    assert issue_id in listing.output

    resolved = _run_cli(db_session, ["recheck", "resolve", issue_id])
    assert resolved.exit_code == 0
    stored = db_session.query(SourceIssue).filter_by(issue_id=issue_id).one()
    assert stored.status == IssueStatus.RESOLVED.value

    assert "no open issues" in _run_cli(db_session, ["recheck", "issues"]).output
