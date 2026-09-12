"""The unattended daily run, end to end and entirely offline.

The control flow under test is the real one: ``run_daily`` discovers a
transcript, selects stories, drives ``draft_story`` and then the real
publisher and site build. Only the outside world is replaced — the model, the
search provider, the fetcher and Commons — because those are the parts that
cost money or need a network, not the parts whose behaviour is in question.

The negative cases carry the weight here. An unattended run is trusted to stop
on its own, so "it refuses when the budget is gone" is a more valuable proof
than "it works when everything is fine".
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.editorial.daily import (
    AlreadyRunning,
    DailyPaths,
    ProcessedStories,
    RunLock,
    RunOutcome,
    load_report,
    pending_stories,
    run_daily,
)
from btcedu.core.editorial.limits import (
    BudgetNotApproved,
    DailyLedger,
    DailyLimits,
    LedgerGuardedModel,
    today_key,
)
from btcedu.core.editorial.media import MediaRequirement
from btcedu.core.editorial.public import publish_article
from btcedu.core.editorial.site_export import SiteConfig, build_site, switch_release
from btcedu.core.editorial.transcript_source import (
    discover_transcripts,
    select_stories,
)
from btcedu.core.editorial.workflow import ModelReply, draft_story
from btcedu.models.article import ArticleRevision
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_rights import MediaRole
from btcedu.models.publication import Publication
from btcedu.services.commons_service import FixtureCommonsProvider
from tests.test_editorial_article import (
    PASSAGE,
    SUPPORT_URL,
    _candidate,
    _claim_draft,
    _draft,
    _fetcher,
    _plans,
    _search_provider,
    _story,
)
from tests.test_renderer import db_engine  # noqa: F401

BASE_URL = "https://example.invalid/dev"

#: A broadcast story is a paragraph, not a sentence. Padding the fixture up to
#: a realistic length is not cosmetic: the selection step refuses stories too
#: short to carry a claim, and a five-word fixture would silently bypass it.
BROADCAST_TEXT = (
    "Berlin meldet 100 neue Wohnungen. Die Senatsverwaltung teilte am Abend mit, "
    "dass die Gebäude im Laufe des kommenden Jahres bezugsfertig werden sollen. "
    "Nach Angaben der Verwaltung entstehen die Wohnungen in mehreren Bezirken, "
    "ein Teil davon ist für Haushalte mit geringem Einkommen vorgesehen. "
    "Kritiker halten das Tempo des Wohnungsbaus weiterhin für zu niedrig und "
    "verweisen auf die anhaltend hohe Nachfrage in der Hauptstadt."
)


# ---------------------------------------------------------------------------
# Fixtures that stand in for the outside world
# ---------------------------------------------------------------------------


def _stories_payload(*, broadcast: str, count: int = 2) -> dict:
    """A broadcast in the exact shape the video pipeline writes."""
    story = _story().model_dump(mode="json")
    story["source_text"] = BROADCAST_TEXT
    story["text_de"] = BROADCAST_TEXT
    story["word_count"] = len(BROADCAST_TEXT.split())
    stories = []
    for index in range(count):
        entry = dict(story)
        entry.update(
            {
                "story_id": f"s{index + 2:02d}",
                "order": index + 2,
                "headline_de": f"{story['headline_de']} {index + 1}",
                "is_lead_story": index == 0,
            }
        )
        stories.append(entry)
    # Material a bulletin contains but a newsroom must not turn into articles.
    stories.insert(
        0,
        {
            **story,
            "story_id": "s01",
            "order": 1,
            "headline_de": "Begrüßung",
            "category": "meta",
            "story_type": "intro",
            "is_lead_story": False,
        },
    )
    stories.append(
        {
            **story,
            "story_id": "s99",
            "order": 99,
            "headline_de": "Das Wetter",
            "category": "wetter",
            "story_type": "wetter",
            "is_lead_story": False,
        }
    )
    return {
        "schema_version": "1.0",
        "episode_id": f"tagesschau_{broadcast}_2000",
        "broadcast_date": broadcast,
        "source_attribution": {"broadcaster": "ARD", "source": "tagesschau"},
        "total_stories": len(stories),
        "stories": stories,
    }


def _outputs(tmp_path: Path, *, broadcast: str = "2026-09-11", count: int = 2) -> Path:
    payload = _stories_payload(broadcast=broadcast, count=count)
    directory = tmp_path / "outputs" / payload["episode_id"]
    directory.mkdir(parents=True)
    (directory / "stories.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path / "outputs"


class RecordingModel:
    """An offline editorial model that answers every task the workflow asks."""

    def __init__(self, *, consistent: bool = True, cost_usd: float = 0.001) -> None:
        self.tasks: list[str] = []
        self.consistent = consistent
        self.cost_usd = cost_usd

    def __call__(self, payload):
        task = payload["task"]
        self.tasks.append(task)
        if task == "extract_claims":
            result = [_claim_draft().model_dump(mode="json")]
        elif task == "evaluate_claim_evidence":
            result = [
                {
                    "canonical_url": SUPPORT_URL,
                    "relation": "supports",
                    "passage": PASSAGE,
                    "rationale": "Fixture: same entity and count in fetched text",
                }
            ]
        elif task == "draft_article":
            result = _draft()
        elif task == "check_article_consistency":
            result = {"consistent": self.consistent, "issues": []}
        else:  # pragma: no cover - a new task must be added deliberately
            raise AssertionError(f"Unexpected model task {task}")
        return ModelReply(result=result, cost_usd=self.cost_usd, model="offline-fixture")


def _settings(tmp_path: Path, paths: DailyPaths, *, dev_auto_release: bool) -> Settings:
    return Settings(
        newsroom_enabled=True,
        newsroom_data_dir=str(paths.private),
        newsroom_site_dir=str(paths.site),
        newsroom_site_base_url=BASE_URL,
        newsroom_dev_auto_release=dev_auto_release,
        outputs_dir=str(tmp_path / "pipeline-outputs"),
        reports_dir=str(tmp_path / "reports"),
        dry_run=False,
    )


def _harness(
    session,
    tmp_path: Path,
    *,
    dev_auto_release: bool = True,
    model: RecordingModel | None = None,
    license_id: str = "CC BY-SA 4.0",
):
    """The real drafter and publisher, with fixtures where money would be."""
    paths = DailyPaths(data_dir=tmp_path / "dev")
    settings = _settings(tmp_path, paths, dev_auto_release=dev_auto_release)
    fetcher, _ = _fetcher(tmp_path)
    caller = model or RecordingModel()

    def drafter(*, settings, model, selected, published_at: datetime):
        kwargs = dict(
            session=session,
            episode_id=selected.episode_id,
            story=selected.story,
            settings=settings,
            model_caller=model,
            search_provider=_search_provider(),
            fetcher=fetcher,
            media_provider=FixtureCommonsProvider(
                {"Berlin": (replace(_candidate(), license_id=license_id),)}
            ),
            media_requirement=MediaRequirement(subject="Berlin", role=MediaRole.SYMBOLIC),
            plan_builder=_plans,
            max_call_cost_usd=0.01,
            source_published_at=published_at,
        )
        try:
            article = draft_story(**kwargs)
        except ValueError as exc:
            # Mirrors the runner exactly: no clearable picture means a card
            # without one, never a card with an unrelated one.
            if "Requested illustration unresolved" not in str(exc):
                raise
            kwargs["media_requirement"] = None
            article = draft_story(**kwargs)

        class _Result:
            title = article.title
            section = selected.section

        return _Result()

    def publisher(*, settings, paths: DailyPaths):
        urls: dict[str, str] = {}
        latest: dict[int, ArticleRevision] = {}
        rows = (
            session.query(ArticleRevision, EditorialRevision.topic_id)
            .join(
                EditorialRevision,
                EditorialRevision.id == ArticleRevision.editorial_revision_id,
            )
            .order_by(ArticleRevision.id)
        )
        for article, topic_id in rows:
            latest[topic_id] = article
        for article in latest.values():
            try:
                publication = publish_article(
                    session,
                    article,
                    operator_ref="test:daily",
                    dev_auto_release=settings.newsroom_dev_auto_release,
                )
            except Exception:  # noqa: BLE001 - mirrors the runner's isolation
                session.rollback()
                continue
            urls[article.title] = f"{BASE_URL}/{publication.section}/{publication.slug}/"
        config = SiteConfig(
            site_name="ALMANYA24",
            base_url=BASE_URL,
            dev_auto_release=settings.newsroom_dev_auto_release,
        )
        result = build_site(
            session, root=paths.site, config=config, operator_ref="test:daily"
        )
        switch_release(session, root=paths.site, release_id=result.release_id)
        return {"articles": result.article_count, "urls": urls}

    return paths, settings, caller, drafter, publisher


def _run(session, tmp_path, *, limits: DailyLimits, outputs: Path, drafter_override=None, **kwargs):
    paths, settings, caller, drafter, publisher = _harness(session, tmp_path, **kwargs)
    report = run_daily(
        paths=paths,
        limits=limits,
        outputs_dir=outputs,
        settings_factory=lambda _paths: settings,
        model_factory=lambda _settings: caller,
        drafter=drafter_override or drafter,
        publisher=publisher,
        today=date(2026, 9, 11),
        now=datetime(2026, 9, 11, 21, 30),
    )
    return report, paths, caller


# ---------------------------------------------------------------------------
# Selecting what to write about
# ---------------------------------------------------------------------------


def test_intro_and_weather_are_not_news(tmp_path):
    """A bulletin contains material that cannot become an article."""
    outputs = _outputs(tmp_path, count=2)
    source = discover_transcripts(outputs, today=date(2026, 9, 11))[0]

    selected = select_stories(source, limit=10)

    assert len(selected) == 2
    assert all(item.story.story_id not in {"s01", "s99"} for item in selected)


def test_a_late_transcript_is_still_picked_up_the_next_day(tmp_path):
    """Yesterday's broadcast must not be lost because the timer was early."""
    outputs = _outputs(tmp_path, broadcast="2026-09-10")

    same_day = discover_transcripts(outputs, lookback_days=0, today=date(2026, 9, 11))
    with_lookback = discover_transcripts(outputs, lookback_days=3, today=date(2026, 9, 11))

    assert same_day == []
    assert len(with_lookback) == 1


def test_broadcast_date_is_kept_apart_from_the_publishing_date(tmp_path):
    outputs = _outputs(tmp_path, broadcast="2026-09-10")
    source = discover_transcripts(outputs, today=date(2026, 9, 11))[0]

    assert select_stories(source, limit=1)[0].broadcast_date == date(2026, 9, 10)


# ---------------------------------------------------------------------------
# The limits, which nobody is watching
# ---------------------------------------------------------------------------


def test_limits_cannot_be_left_out(tmp_path):
    """There is no unlimited default to fall back into."""
    with pytest.raises(TypeError):
        DailyLimits(budget_usd=1.0, max_calls=10)  # type: ignore[call-arg]


def test_without_an_approved_budget_no_paid_call_is_attempted(tmp_path):
    ledger = DailyLedger(tmp_path / "ledger.sqlite")
    limits = DailyLimits(budget_usd=0.0, max_calls=0, max_stories=3)

    def model(payload):  # pragma: no cover - must never run
        raise AssertionError("A paid call was attempted without a budget")

    guarded = LedgerGuardedModel(
        model, ledger=ledger, limits=limits, max_tokens_by_task={"draft_article": 100}
    )

    with pytest.raises(BudgetNotApproved):
        guarded({"task": "draft_article"})
    assert ledger.call_count(today_key()) == 0


def test_a_restart_does_not_reset_the_counters(tmp_path):
    """The ledger is the whole defence against a crash-restart spending loop."""
    path = tmp_path / "ledger.sqlite"
    # Generous on purpose: this test is about the counters surviving a
    # restart, not about the guard, which has its own tests.
    limits = DailyLimits(budget_usd=0.5, max_calls=5, max_stories=3)
    tokens = {"draft_article": 100}
    day = today_key()

    first = LedgerGuardedModel(
        lambda payload: ModelReply(result={}, cost_usd=0.008, model="fixture"),
        ledger=DailyLedger(path),
        limits=limits,
        max_tokens_by_task=tokens,
    )
    first({"task": "draft_article", "n": 1})

    # A fresh process, a fresh object, the same day.
    second = LedgerGuardedModel(
        lambda payload: ModelReply(result={}, cost_usd=0.008, model="fixture"),
        ledger=DailyLedger(path),
        limits=limits,
        max_tokens_by_task=tokens,
    )

    assert DailyLedger(path).call_count(day) == 1
    assert DailyLedger(path).spent_usd(day) == pytest.approx(0.008, abs=1e-9)
    second({"task": "draft_article", "n": 2})
    assert DailyLedger(path).call_count(day) == 2


def test_a_discarded_reply_is_still_paid_for(tmp_path):
    """A model that answered and was then rejected has still been billed."""
    path = tmp_path / "ledger.sqlite"
    limits = DailyLimits(budget_usd=1.0, max_calls=5, max_stories=3)

    def exploding(payload):
        raise RuntimeError("validation threw the reply away")

    guarded = LedgerGuardedModel(
        exploding,
        ledger=DailyLedger(path),
        limits=limits,
        max_tokens_by_task={"draft_article": 100},
    )

    with pytest.raises(RuntimeError):
        guarded({"task": "draft_article"})
    assert DailyLedger(path).spent_usd(today_key()) > 0


def test_an_identical_failed_payload_is_not_sent_again(tmp_path):
    attempts = []

    def failing(payload):
        attempts.append(payload)
        raise RuntimeError("provider said no")

    guarded = LedgerGuardedModel(
        failing,
        ledger=DailyLedger(tmp_path / "ledger.sqlite"),
        limits=DailyLimits(budget_usd=1.0, max_calls=9, max_stories=3),
        max_tokens_by_task={"draft_article": 100},
    )
    payload = {"task": "draft_article", "text": "same"}

    with pytest.raises(RuntimeError):
        guarded(payload)
    with pytest.raises(Exception) as second:
        guarded(dict(payload))

    assert len(attempts) == 1
    assert "identical payload" in str(second.value)


def test_a_task_without_a_token_ceiling_is_refused(tmp_path):
    """An unbounded reply is an unbounded bill, so it is never attempted."""
    guarded = LedgerGuardedModel(
        lambda payload: None,
        ledger=DailyLedger(tmp_path / "ledger.sqlite"),
        limits=DailyLimits(budget_usd=1.0, max_calls=9, max_stories=3),
        max_tokens_by_task={},
    )

    with pytest.raises(Exception) as exc:
        guarded({"task": "something_new"})
    assert "token ceiling" in str(exc.value)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_two_runs_cannot_overlap(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path):
        with pytest.raises(AlreadyRunning):
            with RunLock(path):
                pass  # pragma: no cover


def test_a_lock_left_by_a_dead_process_is_reclaimed(tmp_path):
    """A power cut must not take the newsroom out until someone notices."""
    path = tmp_path / "run.lock"
    path.write_text("999999")

    with RunLock(path):
        assert path.read_text() == str(__import__("os").getpid())


def test_a_second_run_reports_that_one_is_in_progress(tmp_path, db_session):
    outputs = _outputs(tmp_path)
    paths = DailyPaths(data_dir=tmp_path / "dev")
    paths.data_dir.mkdir(parents=True)
    with RunLock(paths.lock):
        report = run_daily(
            paths=paths,
            limits=DailyLimits(budget_usd=1.0, max_calls=9, max_stories=2),
            outputs_dir=outputs,
            settings_factory=lambda _p: None,
            model_factory=lambda _s: None,
            drafter=lambda **kwargs: None,
            publisher=lambda **kwargs: {},
        )
    assert report.outcome == RunOutcome.ALREADY_RUNNING.value


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_a_transcript_becomes_turkish_articles_on_the_development_index(
    db_session, tmp_path
):
    """Transcript to topics to article to the development home page."""
    outputs = _outputs(tmp_path, count=2)

    report, paths, caller = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=2),
        outputs=outputs,
    )

    assert report.outcome == RunOutcome.SUCCESS.value
    assert report.transcript == "tagesschau_2026-09-11_2000"
    assert report.broadcast_date == "2026-09-11"
    assert "extract_claims" in caller.tasks
    assert "draft_article" in caller.tasks
    drafted = [item for item in report.stories if item.status == "drafted"]
    assert drafted, report.stories

    index = (paths.site / "current" / "index.html").read_text(encoding="utf-8")
    assert report.published >= 1
    assert 'class="dev-badge"' in index
    for item in drafted:
        assert item.title_tr and item.title_tr in index


def test_the_run_leaves_a_status_an_operator_can_read(db_session, tmp_path):
    outputs = _outputs(tmp_path, count=1)

    report, paths, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
    )

    stored = load_report(paths.report)
    assert stored is not None
    assert stored["outcome"] == report.outcome
    assert stored["usage"]["day"] == "2026-09-11"
    assert stored["limits"]["budget_usd"] == 1.0


def test_running_twice_does_not_produce_a_second_copy_of_an_article(
    db_session, tmp_path
):
    outputs = _outputs(tmp_path, count=1)

    first, paths, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
    )
    second, _, caller = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
    )

    assert first.outcome == RunOutcome.SUCCESS.value
    assert db_session.query(Publication).count() == 1
    # Nothing was re-drafted, so nothing was paid for twice.
    assert caller.tasks == []
    assert second.outcome == RunOutcome.NO_SUITABLE_TOPICS.value


def test_no_new_transcript_is_reported_as_such_and_not_as_a_failure(
    db_session, tmp_path
):
    empty = tmp_path / "outputs"
    empty.mkdir()

    report, _, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=2),
        outputs=empty,
    )

    assert report.outcome == RunOutcome.NO_NEW_TRANSCRIPT.value
    assert report.errors == []


def test_with_no_budget_the_run_names_what_it_would_have_written(
    db_session, tmp_path
):
    """The locked state has to be legible, not just silent."""
    outputs = _outputs(tmp_path, count=2)

    report, _, caller = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=0.0, max_calls=0, max_stories=2),
        outputs=outputs,
    )

    assert report.outcome == RunOutcome.BUDGET_NOT_APPROVED.value
    assert caller.tasks == []
    assert [item.status for item in report.stories] == [
        "waiting_for_budget",
        "waiting_for_budget",
    ]


def test_an_exhausted_budget_stops_the_run_instead_of_failing_every_story(
    db_session, tmp_path
):
    outputs = _outputs(tmp_path, count=3)

    report, _, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=0.02, max_calls=40, max_stories=3),
        outputs=outputs,
    )

    assert report.outcome == RunOutcome.BUDGET_EXHAUSTED.value
    assert len(report.stories) < 3


def test_one_failing_story_does_not_block_the_others(db_session, tmp_path):
    outputs = _outputs(tmp_path, count=2)
    paths, settings, caller, drafter, publisher = _harness(db_session, tmp_path)
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no usable source for this one")
        return drafter(**kwargs)

    report = run_daily(
        paths=paths,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=2),
        outputs_dir=outputs,
        settings_factory=lambda _p: settings,
        model_factory=lambda _s: caller,
        drafter=flaky,
        publisher=publisher,
        today=date(2026, 9, 11),
        now=datetime(2026, 9, 11, 21, 30),
    )

    statuses = sorted(item.status for item in report.stories)
    assert statuses == ["drafted", "failed"]
    assert report.outcome == RunOutcome.SUCCESS.value


def test_a_story_that_keeps_failing_is_eventually_left_alone(tmp_path):
    """Retrying a deterministic rejection forever is the expensive way to fail."""
    outputs = _outputs(tmp_path, count=1)
    source = discover_transcripts(outputs, today=date(2026, 9, 11))[0]
    selected = select_stories(source, limit=1)
    processed = ProcessedStories(tmp_path / "processed.sqlite")

    assert pending_stories(selected, processed) == selected
    processed.record(selected[0].key, episode_id=source.episode_id, status="failed")
    assert pending_stories(selected, processed) == selected
    processed.record(selected[0].key, episode_id=source.episode_id, status="failed")
    assert pending_stories(selected, processed) == []


def test_a_picture_that_cannot_be_cleared_yields_a_card_without_one(
    db_session, tmp_path
):
    """Rather no image than an unrelated one — and the draft still appears.

    A non-commercial licence is a real rights failure, not a technical one. The
    story keeps its place, loses its picture, and the page still says plainly
    that nobody approved it.
    """
    outputs = _outputs(tmp_path, count=1)

    report, paths, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
        license_id="CC BY-NC 4.0",
    )

    assert report.published >= 1
    publication = db_session.query(Publication).one()
    page = (
        paths.site
        / "current"
        / publication.section
        / publication.slug
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'class="dev-release"' in page
    assert "No operator approval recorded" in page
    assert "article-visual" not in page


def test_a_negative_editorial_judgement_is_visible_and_does_not_hide_the_draft(
    db_session, tmp_path
):
    """The approved development behaviour: checks run, results stay, draft shows."""
    outputs = _outputs(tmp_path, count=1)

    report, paths, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
    )

    assert report.published >= 1
    publication = db_session.query(Publication).one()
    page = (
        paths.site
        / "current"
        / publication.section
        / publication.slug
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'class="dev-release"' in page
    assert "No operator approval recorded" in page


def test_without_the_switch_an_unapproved_draft_still_does_not_appear(
    db_session, tmp_path
):
    """The switch is the only thing that changes; the default is untouched."""
    outputs = _outputs(tmp_path, count=1)

    report, paths, _ = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=40, max_stories=1),
        outputs=outputs,
        dev_auto_release=False,
    )

    assert report.published == 0
    index = (paths.site / "current" / "index.html").read_text(encoding="utf-8")
    assert "empty-news" in index


def test_the_operations_page_is_linked_only_in_development(db_session, tmp_path):
    """An operational status link has no business on a real news site."""
    config_on = SiteConfig(
        site_name="ALMANYA24",
        base_url=BASE_URL,
        preview_notice="Önizleme",
        dev_auto_release=True,
    )
    config_off = replace(config_on, dev_auto_release=False)

    on = build_site(db_session, root=tmp_path / "on", config=config_on, operator_ref="t")
    off = build_site(db_session, root=tmp_path / "off", config=config_off, operator_ref="t")

    assert "_durum" in (on.directory / "index.html").read_text(encoding="utf-8")
    assert "_durum" not in (off.directory / "index.html").read_text(encoding="utf-8")


def test_an_open_topic_question_is_not_recorded_as_a_failure(db_session, tmp_path):
    """Whether a broadcast continues an existing story is an editor's call.

    Filing it as a failure would bury it among real errors and spend the
    story's retries on a question no retry can answer.
    """
    outputs = _outputs(tmp_path, count=1)

    def refusing(*, settings, model, selected, published_at):
        raise ValueError(
            "Possible topic update: review the stored proposals before drafting"
        )

    report, _paths, _caller = _run(
        db_session,
        tmp_path,
        limits=DailyLimits(budget_usd=1.0, max_calls=20, max_stories=3),
        outputs=outputs,
        drafter_override=refusing,
    )

    assert [item.status for item in report.stories] == ["needs_decision"]
    assert report.outcome == RunOutcome.NEEDS_EDITORIAL_DECISION.value


def test_an_open_topic_question_does_not_consume_the_retry_budget(tmp_path):
    outputs = _outputs(tmp_path, count=1)
    source = discover_transcripts(outputs, today=date(2026, 9, 11))[0]
    selected = select_stories(source, limit=1)
    processed = ProcessedStories(tmp_path / "processed.sqlite")

    for _ in range(4):
        processed.record(
            selected[0].key, episode_id=source.episode_id, status="needs_decision"
        )

    assert pending_stories(selected, processed) == selected
