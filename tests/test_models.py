from datetime import UTC, datetime

from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.schemas import (
    Citation,
    ContentPackage,
    EpisodeInfo,
    PipelineStatus,
    RetrievedChunk,
    TranscriptChunk,
)


class TestEpisodeORM:
    def test_create_episode(self, db_session):
        episode = Episode(
            episode_id="abc123",
            source="youtube_rss",
            title="Test Episode",
            url="https://youtube.com/watch?v=abc123",
            status=EpisodeStatus.NEW,
        )
        db_session.add(episode)
        db_session.commit()

        result = db_session.query(Episode).first()
        assert result is not None
        assert result.episode_id == "abc123"
        assert result.title == "Test Episode"
        assert result.status == EpisodeStatus.NEW
        assert result.source == "youtube_rss"
        assert result.detected_at is not None

    def test_episode_unique_episode_id(self, db_session):
        ep1 = Episode(
            episode_id="abc123",
            title="Episode 1",
            url="https://youtube.com/watch?v=abc123",
        )
        ep2 = Episode(
            episode_id="abc123",
            title="Episode 2",
            url="https://youtube.com/watch?v=abc123",
        )
        db_session.add(ep1)
        db_session.commit()
        db_session.add(ep2)
        try:
            db_session.commit()
            assert False, "Should have raised IntegrityError"
        except Exception:
            db_session.rollback()

    def test_episode_status_transitions(self, db_session):
        episode = Episode(
            episode_id="abc123",
            title="Test",
            url="https://youtube.com/watch?v=abc123",
            status=EpisodeStatus.NEW,
        )
        db_session.add(episode)
        db_session.commit()

        episode.status = EpisodeStatus.DOWNLOADED
        db_session.commit()
        assert episode.status == EpisodeStatus.DOWNLOADED

    def test_pipeline_run_relationship(self, db_session):
        episode = Episode(
            episode_id="abc123",
            title="Test",
            url="https://youtube.com/watch?v=abc123",
        )
        db_session.add(episode)
        db_session.commit()

        run = PipelineRun(
            episode_id=episode.id,
            stage=PipelineStage.DOWNLOAD,
            status=RunStatus.RUNNING,
        )
        db_session.add(run)
        db_session.commit()

        assert len(episode.pipeline_runs) == 1
        assert episode.pipeline_runs[0].stage == PipelineStage.DOWNLOAD

    def test_pipeline_run_defaults(self, db_session):
        episode = Episode(
            episode_id="abc123",
            title="Test",
            url="https://youtube.com/watch?v=abc123",
        )
        db_session.add(episode)
        db_session.commit()

        run = PipelineRun(
            episode_id=episode.id,
            stage=PipelineStage.TRANSCRIBE,
        )
        db_session.add(run)
        db_session.commit()

        assert run.status == RunStatus.RUNNING
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.estimated_cost_usd == 0.0
        assert run.started_at is not None


class TestPydanticSchemas:
    def test_episode_info(self):
        info = EpisodeInfo(
            episode_id="abc123",
            title="Test Episode",
            url="https://youtube.com/watch?v=abc123",
            source="youtube_rss",
        )
        assert info.episode_id == "abc123"
        assert info.source == "youtube_rss"

    def test_transcript_chunk(self):
        chunk = TranscriptChunk(
            chunk_index=0,
            text="Bitcoin ist eine dezentrale Waehrung.",
            word_count=5,
            start_sentence=0,
            end_sentence=1,
            episode_video_id="abc123",
        )
        assert chunk.chunk_index == 0
        assert chunk.word_count == 5

    def test_retrieved_chunk(self):
        chunk = RetrievedChunk(
            chunk_index=3,
            text="Some text",
            score=0.85,
        )
        assert chunk.score == 0.85
        assert chunk.metadata == {}

    def test_citation(self):
        citation = Citation(
            output_file="02_script_tr.md",
            section="Bolum 1",
            cited_text_de="Bitcoin Mining verbraucht...",
            chunk_index=5,
        )
        assert citation.chunk_index == 5

    def test_content_package_defaults(self):
        pkg = ContentPackage(episode_video_id="abc123")
        assert pkg.outline == ""
        assert pkg.citations == []

    def test_pipeline_status(self):
        now = datetime.now(UTC)
        status = PipelineStatus(
            episode_id=1,
            video_id="abc123",
            title="Test",
            status="new",
            detected_at=now,
        )
        assert status.total_cost_usd == 0.0
        assert status.completed_at is None
