"""Tests for Sprint 1 new ORM models and enum extensions."""

import pytest
from sqlalchemy.exc import IntegrityError

from btcedu.models.episode import Episode, EpisodeStatus, PipelineStage
from btcedu.models.prompt_version import PromptVersion
from btcedu.models.review import ReviewDecision, ReviewStatus, ReviewTask


class TestPromptVersionORM:
    def test_create_prompt_version(self, db_session):
        pv = PromptVersion(
            name="correct_transcript",
            version=1,
            content_hash="sha256_abc123",
            template_path="btcedu/prompts/templates/correct_transcript.md",
            model="claude-sonnet-4-20250514",
            temperature=0.2,
            max_tokens=8192,
            is_default=True,
            notes="Initial version",
        )
        db_session.add(pv)
        db_session.commit()

        result = db_session.query(PromptVersion).first()
        assert result is not None
        assert result.name == "correct_transcript"
        assert result.version == 1
        assert result.content_hash == "sha256_abc123"
        assert result.model == "claude-sonnet-4-20250514"
        assert result.temperature == 0.2
        assert result.max_tokens == 8192
        assert result.is_default is True
        assert result.notes == "Initial version"
        assert result.created_at is not None

    def test_unique_name_version(self, db_session):
        pv1 = PromptVersion(name="system", version=1, content_hash="hash_a")
        pv2 = PromptVersion(name="system", version=1, content_hash="hash_b")
        db_session.add(pv1)
        db_session.commit()
        db_session.add(pv2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_unique_name_hash(self, db_session):
        pv1 = PromptVersion(name="system", version=1, content_hash="hash_same")
        pv2 = PromptVersion(name="system", version=2, content_hash="hash_same")
        db_session.add(pv1)
        db_session.commit()
        db_session.add(pv2)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_default_flag(self, db_session):
        pv = PromptVersion(name="system", version=1, content_hash="hash_a", is_default=True)
        db_session.add(pv)
        db_session.commit()

        result = db_session.query(PromptVersion).filter(PromptVersion.is_default.is_(True)).first()
        assert result is not None
        assert result.name == "system"


class TestReviewTaskORM:
    def test_create_review_task(self, db_session):
        task = ReviewTask(
            episode_id="ep001",
            stage="correct",
            artifact_paths='["path/to/file.txt"]',
            diff_path="path/to/diff.json",
            artifact_hash="sha256_xyz",
        )
        db_session.add(task)
        db_session.commit()

        result = db_session.query(ReviewTask).first()
        assert result is not None
        assert result.episode_id == "ep001"
        assert result.stage == "correct"
        assert result.status == "pending"
        assert result.artifact_paths == '["path/to/file.txt"]'
        assert result.diff_path == "path/to/diff.json"
        assert result.artifact_hash == "sha256_xyz"
        assert result.created_at is not None

    def test_review_task_defaults(self, db_session):
        task = ReviewTask(episode_id="ep001", stage="adapt")
        db_session.add(task)
        db_session.commit()

        assert task.status == ReviewStatus.PENDING.value
        assert task.reviewed_at is None
        assert task.reviewer_notes is None
        assert task.prompt_version_id is None

    def test_review_decision_relationship(self, db_session):
        task = ReviewTask(episode_id="ep001", stage="correct")
        db_session.add(task)
        db_session.commit()

        decision = ReviewDecision(
            review_task_id=task.id,
            decision="approved",
            notes="Looks good",
        )
        db_session.add(decision)
        db_session.commit()

        assert len(task.decisions) == 1
        assert task.decisions[0].decision == "approved"
        assert task.decisions[0].notes == "Looks good"

        # Test cascade delete
        db_session.delete(task)
        db_session.commit()
        assert db_session.query(ReviewDecision).count() == 0


class TestReviewDecisionORM:
    def test_create_review_decision(self, db_session):
        task = ReviewTask(episode_id="ep001", stage="render")
        db_session.add(task)
        db_session.commit()

        decision = ReviewDecision(
            review_task_id=task.id,
            decision="rejected",
            notes="Audio out of sync",
        )
        db_session.add(decision)
        db_session.commit()

        result = db_session.query(ReviewDecision).first()
        assert result is not None
        assert result.decision == "rejected"
        assert result.notes == "Audio out of sync"
        assert result.decided_at is not None
        assert result.review_task.episode_id == "ep001"


class TestEpisodeV2Fields:
    def test_pipeline_version_default(self, db_session):
        episode = Episode(
            episode_id="ep_v2_test",
            source="youtube_rss",
            title="V2 Test",
            url="https://youtube.com/watch?v=test",
        )
        db_session.add(episode)
        db_session.commit()

        assert episode.pipeline_version == 1
        assert episode.review_status is None
        assert episode.youtube_video_id is None
        assert episode.published_at_youtube is None

    def test_new_status_values(self, db_session):
        new_statuses = [
            EpisodeStatus.CORRECTED,
            EpisodeStatus.TRANSLATED,
            EpisodeStatus.ADAPTED,
            EpisodeStatus.CHAPTERIZED,
            EpisodeStatus.IMAGES_GENERATED,
            EpisodeStatus.TTS_DONE,
            EpisodeStatus.RENDERED,
            EpisodeStatus.APPROVED,
            EpisodeStatus.PUBLISHED,
            EpisodeStatus.COST_LIMIT,
        ]
        for i, status in enumerate(new_statuses):
            ep = Episode(
                episode_id=f"ep_status_{i}",
                title=f"Status test {status.value}",
                url=f"https://youtube.com/watch?v=st{i}",
                status=status,
            )
            db_session.add(ep)

        db_session.commit()

        for i, status in enumerate(new_statuses):
            ep = db_session.query(Episode).filter(Episode.episode_id == f"ep_status_{i}").first()
            assert ep.status == status

    def test_pipeline_stage_new_values(self):
        new_stages = [
            PipelineStage.CORRECT,
            PipelineStage.TRANSLATE,
            PipelineStage.ADAPT,
            PipelineStage.CHAPTERIZE,
            PipelineStage.IMAGEGEN,
            PipelineStage.TTS,
            PipelineStage.RENDER,
            PipelineStage.REVIEW,
            PipelineStage.PUBLISH,
        ]
        for stage in new_stages:
            assert stage.value  # exists and has a string value

        # Total enum count: 7 original + 9 new = 16
        assert len(PipelineStage) == 16

    def test_episode_status_total_count(self):
        # 8 original + 10 new = 18
        assert len(EpisodeStatus) == 18
