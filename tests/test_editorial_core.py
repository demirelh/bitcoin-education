import threading
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.editorial import EditorialOperationConflict
from btcedu.core.editorial.ingest import import_story
from btcedu.core.editorial.jobs import (
    EditorialBudgetExceeded,
    reserve_provider_operation,
    reserve_research_run,
    reserved_cost_usd,
)
from btcedu.core.retention import prune_expired_episodes
from btcedu.models.editorial import (
    Claim,
    ClaimRevision,
    ProviderOperation,
    ProviderOperationStatus,
    ResearchRun,
    SourceItem,
    SourceRevision,
    SourceSpan,
    Topic,
)
from btcedu.models.editorial_schema import ClaimDraft
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.story_schema import Story, StoryCategory, StoryType


def _story(text: str = "Berlin meldet 100 neue Wohnungen.") -> Story:
    return Story(
        story_id="s01",
        order=1,
        headline_de="Neue Wohnungen in Berlin",
        category=StoryCategory.GESELLSCHAFT,
        story_type=StoryType.MELDUNG,
        text_de=text,
        source_text=text,
        source_segment_ids=["seg-1", "seg-2"],
        source_start_seconds=12.5,
        source_end_seconds=22.0,
        word_count=len(text.split()),
        estimated_duration_seconds=10,
    )


def _claim(statement: str = "Berlin meldet 100 neue Wohnungen.") -> ClaimDraft:
    return ClaimDraft(
        claim_key="housing-count",
        statement=statement,
        claim_type="fact",
        subject="Berlin",
        numeric_value="100",
        unit="Wohnungen",
    )


def test_importing_the_same_story_twice_reuses_every_revision(db_session):
    first = import_story(
        db_session,
        episode_id="episode-1",
        story=_story(),
        claims=[_claim()],
    )
    second = import_story(
        db_session,
        episode_id="episode-1",
        story=_story(),
        claims=[_claim()],
    )

    assert first.source_item.id == second.source_item.id
    assert first.source_revision.id == second.source_revision.id
    assert first.source_span.id == second.source_span.id
    assert first.topic.id == second.topic.id
    assert first.claim_revisions[0].id == second.claim_revisions[0].id
    assert db_session.query(SourceItem).count() == 1
    assert db_session.query(SourceRevision).count() == 1
    assert db_session.query(ClaimRevision).count() == 1


def test_changed_story_and_claim_create_new_revisions_without_overwriting(db_session):
    first = import_story(
        db_session,
        episode_id="episode-1",
        story=_story(),
        claims=[_claim()],
    )
    changed = import_story(
        db_session,
        episode_id="episode-1",
        story=_story("Berlin meldet 120 neue Wohnungen."),
        claims=[_claim("Berlin meldet 120 neue Wohnungen.")],
    )

    assert first.source_revision.id != changed.source_revision.id
    assert first.claim_revisions[0].id != changed.claim_revisions[0].id
    assert first.claim_revisions[0].revision_number == 1
    assert changed.claim_revisions[0].revision_number == 2
    assert (
        db_session.get(SourceRevision, first.source_revision.id).source_text
        == "Berlin meldet 100 neue Wohnungen."
    )
    assert (
        db_session.get(ClaimRevision, first.claim_revisions[0].id).statement
        == "Berlin meldet 100 neue Wohnungen."
    )
    assert db_session.get(SourceSpan, first.source_span.id).start_seconds == 12.5


def test_missing_source_times_stay_unknown(db_session):
    story = _story()
    story.source_start_seconds = None
    story.source_end_seconds = None

    imported = import_story(db_session, episode_id="episode-1", story=story)

    assert imported.source_span.start_seconds is None
    assert imported.source_span.end_seconds is None


def test_research_and_operation_reservations_are_idempotent_and_budgeted(db_session):
    imported = import_story(db_session, episode_id="episode-1", story=_story())
    first = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=20,
        max_cost_usd=0.75,
    )
    second = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=20,
        max_cost_usd=0.75,
    )
    operation = reserve_provider_operation(
        db_session,
        research_run=first,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.4,
    )
    same_operation = reserve_provider_operation(
        db_session,
        research_run=first,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.4,
    )

    assert first.id == second.id
    assert operation.id == same_operation.id
    assert reserved_cost_usd(db_session, first.id) == pytest.approx(0.4)
    with pytest.raises(EditorialBudgetExceeded):
        reserve_provider_operation(
            db_session,
            research_run=first,
            operation_key="search:2",
            operation_type="search",
            provider="fixture",
            model_name="",
            input_hash="c" * 64,
            estimated_cost_usd=0.4,
        )


def test_negative_research_or_operation_budgets_are_rejected(db_session):
    imported = import_story(db_session, episode_id="episode-1", story=_story())

    with pytest.raises(ValueError, match="max_queries"):
        reserve_research_run(
            db_session,
            topic_id=imported.topic.id,
            input_hash="a" * 64,
            policy_version="1",
            model_name="fixture",
            max_queries=-1,
            max_cost_usd=1.0,
        )

    run = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=1,
        max_cost_usd=1.0,
    )
    with pytest.raises(ValueError, match="estimated_cost_usd"):
        reserve_provider_operation(
            db_session,
            research_run=run,
            operation_key="search:1",
            operation_type="search",
            provider="fixture",
            model_name="",
            input_hash="b" * 64,
            estimated_cost_usd=-0.1,
        )


def test_operation_key_cannot_be_reused_for_different_input(db_session):
    imported = import_story(db_session, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=2,
        max_cost_usd=1.0,
    )
    reserve_provider_operation(
        db_session,
        research_run=run,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.1,
    )

    with pytest.raises(EditorialOperationConflict):
        reserve_provider_operation(
            db_session,
            research_run=run,
            operation_key="search:1",
            operation_type="search",
            provider="fixture",
            model_name="",
            input_hash="c" * 64,
            estimated_cost_usd=0.1,
        )


def test_query_budget_counts_uncertain_operations(db_session):
    imported = import_story(db_session, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=1,
        max_cost_usd=5.0,
    )
    reserve_provider_operation(
        db_session,
        research_run=run,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.1,
    )

    with pytest.raises(EditorialBudgetExceeded, match="query budget"):
        reserve_provider_operation(
            db_session,
            research_run=run,
            operation_key="search:2",
            operation_type="search",
            provider="fixture",
            model_name="",
            input_hash="c" * 64,
            estimated_cost_usd=0.1,
        )


@pytest.mark.parametrize(
    "status",
    [
        ProviderOperationStatus.RESERVED,
        ProviderOperationStatus.SUBMITTED,
        ProviderOperationStatus.RECONCILE_REQUIRED,
    ],
)
def test_uncertain_provider_operations_count_against_budget(db_session, status):
    imported = import_story(db_session, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        db_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=20,
        max_cost_usd=0.75,
    )
    operation = reserve_provider_operation(
        db_session,
        research_run=run,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.5,
    )
    operation.status = status.value
    db_session.commit()

    assert reserved_cost_usd(db_session, run.id) == pytest.approx(0.5)


def test_restart_finds_reserved_run_and_provider_operation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    from btcedu.db import Base

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    first_session = factory()
    imported = import_story(first_session, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        first_session,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=2,
        max_cost_usd=1.0,
    )
    operation = reserve_provider_operation(
        first_session,
        research_run=run,
        operation_key="search:1",
        operation_type="search",
        provider="fixture",
        model_name="",
        input_hash="b" * 64,
        estimated_cost_usd=0.1,
    )
    run_id = run.run_id
    operation_id = operation.operation_id
    first_session.close()

    restarted = factory()
    resumed_run = restarted.query(ResearchRun).filter_by(run_id=run_id).one()
    resumed_operation = (
        restarted.query(ProviderOperation).filter_by(operation_id=operation_id).one()
    )

    assert resumed_run.status == "reserved"
    assert resumed_operation.status == "reserved"
    assert resumed_operation.research_run_id == resumed_run.id
    restarted.close()


def test_concurrent_reservations_cannot_overspend_budget(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"timeout": 5},
    )
    from btcedu.db import Base

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    setup = factory()
    imported = import_story(setup, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        setup,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=2,
        max_cost_usd=0.75,
    )
    run_id = run.id
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def reserve(index: int) -> None:
        session = factory()
        try:
            current_run = session.get(ResearchRun, run_id)
            assert current_run is not None
            barrier.wait()
            reserve_provider_operation(
                session,
                research_run=current_run,
                operation_key=f"search:{index}",
                operation_type="search",
                provider="fixture",
                model_name="",
                input_hash=str(index) * 64,
                estimated_cost_usd=0.5,
            )
        except EditorialBudgetExceeded:
            outcome = "blocked"
        else:
            outcome = "reserved"
        finally:
            session.close()
        with outcome_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["blocked", "reserved"]
    verify = factory()
    assert verify.query(ProviderOperation).count() == 1
    assert reserved_cost_usd(verify, run_id) == pytest.approx(0.5)
    verify.close()


def test_concurrent_duplicate_start_reuses_one_logical_operation(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'duplicate.db'}",
        connect_args={"timeout": 5},
    )
    from btcedu.db import Base

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    setup = factory()
    imported = import_story(setup, episode_id="episode-1", story=_story())
    run = reserve_research_run(
        setup,
        topic_id=imported.topic.id,
        input_hash="a" * 64,
        policy_version="1",
        model_name="fixture",
        max_queries=2,
        max_cost_usd=1.0,
    )
    run_id = run.id
    setup.close()

    barrier = threading.Barrier(2)
    operation_ids: list[str] = []
    result_lock = threading.Lock()

    def reserve() -> None:
        session = factory()
        current_run = session.get(ResearchRun, run_id)
        assert current_run is not None
        barrier.wait()
        operation = reserve_provider_operation(
            session,
            research_run=current_run,
            operation_key="search:shared",
            operation_type="search",
            provider="fixture",
            model_name="",
            input_hash="b" * 64,
            estimated_cost_usd=0.25,
        )
        with result_lock:
            operation_ids.append(operation.operation_id)
        session.close()

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(operation_ids) == 2
    assert len(set(operation_ids)) == 1
    verify = factory()
    assert verify.query(ProviderOperation).count() == 1
    assert verify.get(ResearchRun, run_id).used_queries == 1
    verify.close()


def test_editorial_records_survive_episode_retention(db_session, tmp_path):
    MediaBase.metadata.create_all(db_session.bind)
    now = datetime(2026, 9, 9, tzinfo=UTC)
    episode = Episode(
        episode_id="episode-1",
        source="youtube_rss",
        title="Source",
        url="https://example.invalid/source",
        published_at=now - timedelta(days=11),
        status=EpisodeStatus.NEW,
        content_profile="tagesschau_tr",
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()
    import_story(db_session, episode_id=episode.episode_id, story=_story(), claims=[_claim()])
    settings = Settings(
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        episode_retention_days=10,
    )

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 1
    assert db_session.query(Episode).count() == 0
    assert db_session.query(SourceRevision).count() == 1
    assert db_session.query(SourceSpan).count() == 1
    assert db_session.query(Topic).count() == 1
    assert db_session.query(Claim).count() == 1


def test_newsroom_is_disabled_by_default():
    settings = Settings()

    assert settings.newsroom_enabled is False
    assert settings.newsroom_data_dir == "data/newsroom"


def test_newsroom_data_root_cannot_overlap_episode_outputs(tmp_path):
    outputs = tmp_path / "outputs"

    with pytest.raises(ValidationError, match="must not overlap"):
        Settings(outputs_dir=str(outputs), newsroom_data_dir=str(outputs / "newsroom"))
