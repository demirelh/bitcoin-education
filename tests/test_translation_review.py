"""Tests for Phase 3: translation review gate, bilingual diff, and per-story assembly."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.prompt_version import PromptVersion  # noqa: F401 — ensure table created
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_profile_registry():
    """Reset profile registry singleton between tests."""
    from btcedu.profiles import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def settings_with_profiles(tmp_path):
    """Settings pointing to real profiles dir."""
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        anthropic_api_key="test-key",
        pipeline_version=2,
        profiles_dir="btcedu/profiles",
    )


@pytest.fixture
def tagesschau_episode(db_session):
    """Episode with tagesschau_tr profile at TRANSLATED status."""
    episode = Episode(
        episode_id="ep_ts_review",
        source="youtube_rss",
        title="tagesschau 20:00 Uhr",
        url="https://youtube.com/watch?v=ep_ts_review",
        status=EpisodeStatus.TRANSLATED,
        transcript_path="/tmp/transcript.txt",
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


@pytest.fixture
def bitcoin_episode(db_session):
    """Episode with bitcoin_podcast profile at ADAPTED status."""
    episode = Episode(
        episode_id="ep_btc_review",
        source="youtube_rss",
        title="Bitcoin Episode",
        url="https://youtube.com/watch?v=ep_btc_review",
        status=EpisodeStatus.ADAPTED,
        transcript_path="/tmp/transcript.txt",
        pipeline_version=2,
        content_profile="bitcoin_podcast",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def _make_stories_translated(episode_id: str, stories: list | None = None) -> dict:
    """Helper to build a minimal stories_translated.json structure."""
    if stories is None:
        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "Bundestag stimmt ab",
                "category": "politik",
                "story_type": "meldung",
                "text_de": "Der Bundestag hat heute abgestimmt und ein neues Gesetz beschlossen.",
                "word_count": 10,
                "estimated_duration_seconds": 5,
                "reporter": None,
                "location": None,
                "is_lead_story": True,
                "headline_tr": "Bundestag oylama yaptı",
                "text_tr": (
                    "Bundestag (Almanya Federal Meclisi) bugün oylama yaparak"
                    " yeni bir yasa kabul etti."
                ),
            },
            {
                "story_id": "s02",
                "order": 2,
                "headline_de": "Wirtschaftsnachrichten",
                "category": "wirtschaft",
                "story_type": "bericht",
                "text_de": "Die Wirtschaft wächst um zwei Prozent.",
                "word_count": 6,
                "estimated_duration_seconds": 3,
                "reporter": None,
                "location": None,
                "is_lead_story": False,
                "headline_tr": "Ekonomi haberleri",
                "text_tr": "Ekonomi yüzde iki büyüdü.",
            },
        ]
    return {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "broadcast_date": "2026-03-16",
        "source_attribution": {"source": "tagesschau"},
        "total_stories": len(stories),
        "total_duration_seconds": 60,
        "stories": stories,
    }


# ---------------------------------------------------------------------------
# 1. compute_translation_diff produces correct structure
# ---------------------------------------------------------------------------


class TestComputeTranslationDiff:
    def test_structure(self, tmp_path):
        """compute_translation_diff produces correct bilingual structure."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review")), encoding="utf-8"
        )

        result = compute_translation_diff(stories_path)

        assert result["diff_type"] == "translation"
        assert result["source_language"] == "de"
        assert result["target_language"] == "tr"
        assert result["episode_id"] == "ep_ts_review"
        assert len(result["stories"]) == 2
        assert result["summary"]["total_stories"] == 2

        # item_ids follow "trans-s01" pattern
        item_ids = [s["item_id"] for s in result["stories"]]
        assert "trans-s01" in item_ids
        assert "trans-s02" in item_ids

        # Check story structure
        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        assert s01["item_id"] == "trans-s01"
        assert s01["headline_de"] == "Bundestag stimmt ab"
        assert s01["headline_tr"] == "Bundestag oylama yaptı"
        assert "word_count_de" in s01
        assert "word_count_tr" in s01
        assert s01["category"] == "politik"

    def test_summary_fields(self, tmp_path):
        """Summary has total_stories, total_words, compression_ratio."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review")), encoding="utf-8"
        )

        result = compute_translation_diff(stories_path)

        summary = result["summary"]
        assert summary["total_stories"] == 2
        assert summary["total_words_de"] > 0
        assert summary["total_words_tr"] > 0
        assert "compression_ratio" in summary
        assert isinstance(summary["compression_ratio"], float)

    def test_warnings_low_ratio(self, tmp_path):
        """Story where TR has far fewer words than DE triggers summarization warning."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "Lange Meldung",
                "category": "politik",
                "story_type": "bericht",
                # DE: 20 words
                "text_de": (
                    "Der Bundestag hat heute eine wichtige Entscheidung getroffen"
                    " und viel debattiert über wichtige Themen der Haushaltspolitik."
                ),
                "word_count": 20,
                "estimated_duration_seconds": 10,
                "reporter": None,
                "location": None,
                "is_lead_story": True,
                "headline_tr": "Kısa",
                # TR: only 3 words — ratio = 3/20 = 0.15 (< 0.5)
                "text_tr": "Karar alındı.",
            }
        ]
        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review", stories=stories)),
            encoding="utf-8",
        )

        result = compute_translation_diff(stories_path)

        assert len(result["warnings"]) > 0
        warning_text = result["warnings"][0]
        assert "s01" in warning_text
        assert "summarization" in warning_text

    def test_warnings_high_ratio(self, tmp_path):
        """Story where TR has far more words than DE triggers hallucination warning."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "Kurze Meldung",
                "category": "wirtschaft",
                "story_type": "meldung",
                # DE: 3 words
                "text_de": "Kurs stieg.",
                "word_count": 3,
                "estimated_duration_seconds": 2,
                "reporter": None,
                "location": None,
                "is_lead_story": True,
                "headline_tr": "Uzun",
                # TR: 20 words — ratio = 20/3 > 1.5
                "text_tr": (
                    "Almanya borsasında bu hafta büyük bir artış yaşandı ve yatırımcılar "
                    "büyük kazanımlar elde etti ve piyasalar iyimser bitti."
                ),
            }
        ]
        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review", stories=stories)),
            encoding="utf-8",
        )

        result = compute_translation_diff(stories_path)

        assert len(result["warnings"]) > 0
        warning_text = result["warnings"][0]
        assert "s01" in warning_text
        assert "hallucination" in warning_text

    def test_no_warnings_normal_ratio(self, tmp_path):
        """Story with ratio between 0.5 and 1.5 produces no warnings."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review")), encoding="utf-8"
        )

        result = compute_translation_diff(stories_path)

        # The default fixture stories have roughly balanced text
        # s01: DE ~10 words, TR ~12 words (ratio ~1.2) — no warning
        # s02: DE 6 words, TR 4 words, ratio ~0.67 — no warning
        assert len(result["warnings"]) == 0

    def test_zero_de_words_no_crash(self, tmp_path):
        """Empty DE text doesn't crash (division by zero guard)."""
        from btcedu.core.translation_diff import compute_translation_diff

        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "",
                "category": "politik",
                "story_type": "meldung",
                "text_de": "",  # zero words
                "word_count": 0,
                "estimated_duration_seconds": 0,
                "reporter": None,
                "location": None,
                "is_lead_story": True,
                "headline_tr": "",
                "text_tr": "Bir şeyler.",
            }
        ]
        stories_path = tmp_path / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review", stories=stories)),
            encoding="utf-8",
        )

        result = compute_translation_diff(stories_path)
        # compression_ratio defaults to 1.0 when total_words_de == 0
        assert result["summary"]["compression_ratio"] == 1.0
        # No warnings for zero-word stories (no ratio computed)
        assert len(result["warnings"]) == 0


# ---------------------------------------------------------------------------
# 2. _get_stages for tagesschau includes review_gate_translate
# ---------------------------------------------------------------------------


class TestGetStagesTagesschau:
    def test_tagesschau_has_review_gate_translate(
        self, db_session, tagesschau_episode, settings_with_profiles
    ):
        """tagesschau_tr episode: has review_gate_translate, not review_gate_2, not adapt."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, tagesschau_episode)
        stage_names = [n for n, _ in stages]

        assert "review_gate_translate" in stage_names
        assert "review_gate_2" not in stage_names
        assert "adapt" not in stage_names

    def test_tagesschau_review_gate_translate_requires_translated(
        self, db_session, tagesschau_episode, settings_with_profiles
    ):
        """review_gate_translate gate requires TRANSLATED status."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, tagesschau_episode)
        rgt_stage = next(
            ((n, s) for n, s in stages if n == "review_gate_translate"), None
        )

        assert rgt_stage is not None
        assert rgt_stage[1] == EpisodeStatus.TRANSLATED

    def test_tagesschau_chapterize_requires_translated(
        self, db_session, tagesschau_episode, settings_with_profiles
    ):
        """chapterize requires TRANSLATED (not ADAPTED) for tagesschau."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, tagesschau_episode)
        chap_stage = next(((n, s) for n, s in stages if n == "chapterize"), None)

        assert chap_stage is not None
        assert chap_stage[1] == EpisodeStatus.TRANSLATED

    def test_bitcoin_podcast_unchanged(
        self, db_session, bitcoin_episode, settings_with_profiles
    ):
        """bitcoin_podcast still has review_gate_2, adapt, not review_gate_translate."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, bitcoin_episode)
        stage_names = [n for n, _ in stages]

        assert "review_gate_2" in stage_names
        assert "adapt" in stage_names
        assert "review_gate_translate" not in stage_names


# ---------------------------------------------------------------------------
# 3. review_gate_translate in _run_stage
# ---------------------------------------------------------------------------


class TestReviewGateTranslateRunStage:
    def test_creates_review_task(
        self, db_session, tagesschau_episode, settings_with_profiles, tmp_path
    ):
        """review_gate_translate creates ReviewTask with stage='translate'."""
        from btcedu.core.pipeline import _run_stage

        # Write stories_translated.json
        outputs_dir = tmp_path / "outputs" / "ep_ts_review"
        outputs_dir.mkdir(parents=True)
        stories_path = outputs_dir / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_ts_review")), encoding="utf-8"
        )

        result = _run_stage(
            db_session, tagesschau_episode, settings_with_profiles,
            "review_gate_translate", force=False,
        )

        assert result.status == "review_pending"
        assert "translation review task created" in result.detail

        # ReviewTask created with stage="translate"
        task = (
            db_session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == "ep_ts_review",
                ReviewTask.stage == "translate",
            )
            .first()
        )
        assert task is not None
        assert task.status == ReviewStatus.PENDING.value

        # translation_diff.json written
        diff_path = outputs_dir / "review" / "translation_diff.json"
        assert diff_path.exists()
        diff_data = json.loads(diff_path.read_text(encoding="utf-8"))
        assert diff_data["diff_type"] == "translation"

    def test_skips_when_approved(
        self, db_session, tagesschau_episode, settings_with_profiles, tmp_path
    ):
        """review_gate_translate returns success when translation review is approved."""
        from btcedu.core.pipeline import _run_stage

        # Create approved review task
        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.APPROVED.value,
        )
        db_session.add(task)
        db_session.commit()

        result = _run_stage(
            db_session, tagesschau_episode, settings_with_profiles,
            "review_gate_translate", force=False,
        )

        assert result.status == "success"
        assert "translation review approved" in result.detail

    def test_pending_when_review_exists(
        self, db_session, tagesschau_episode, settings_with_profiles, tmp_path
    ):
        """review_gate_translate returns review_pending when existing pending task."""
        from btcedu.core.pipeline import _run_stage

        # Create pending review task
        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        result = _run_stage(
            db_session, tagesschau_episode, settings_with_profiles,
            "review_gate_translate", force=False,
        )

        assert result.status == "review_pending"
        assert "awaiting translation review" in result.detail

    def test_requires_v2_pipeline(self, db_session, settings_with_profiles, tmp_path):
        """review_gate_translate raises ValueError for v1 episodes."""
        from btcedu.core.pipeline import _run_stage

        v1_episode = Episode(
            episode_id="ep_v1",
            source="youtube_rss",
            title="V1 Episode",
            url="https://youtube.com/watch?v=ep_v1",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=1,
            content_profile="tagesschau_tr",
        )
        db_session.add(v1_episode)
        db_session.commit()

        with pytest.raises(ValueError, match="requires v2 pipeline"):
            _run_stage(
                db_session, v1_episode, settings_with_profiles,
                "review_gate_translate", force=False,
            )


# ---------------------------------------------------------------------------
# 4. _assemble_translation_review
# ---------------------------------------------------------------------------


class TestAssembleTranslationReview:
    def _make_item_decision(
        self, review_task_id: int, item_id: str, action: str, edited_text: str | None = None
    ) -> ReviewItemDecision:
        return ReviewItemDecision(
            review_task_id=review_task_id,
            item_id=item_id,
            operation_type="story",
            original_text="original",
            proposed_text="proposed",
            action=action,
            edited_text=edited_text,
        )

    def test_edited_action_replaces_text_tr(self, db_session):
        """EDITED decision replaces text_tr with edited_text."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")
        diff_data = {}  # not used by _assemble_translation_review

        # Build item_decisions dict — s01 is EDITED
        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.IN_REVIEW.value,
        )
        db_session.add(task)
        db_session.commit()

        d_s01 = self._make_item_decision(task.id, "trans-s01", ReviewItemAction.EDITED.value,
                                          edited_text="Düzeltilmiş Türkçe metin.")
        db_session.add(d_s01)
        db_session.commit()

        item_decisions = {"trans-s01": d_s01}

        result = _assemble_translation_review(stories_data, diff_data, item_decisions)

        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        s02 = next(s for s in result["stories"] if s["story_id"] == "s02")

        assert s01["text_tr"] == "Düzeltilmiş Türkçe metin."
        # s02 has no decision, so text_tr unchanged
        assert s02["text_tr"] == "Ekonomi yüzde iki büyüdü."

    def test_rejected_action_prepends_marker(self, db_session):
        """REJECTED decision prepends [ÇEVİRİ REDDEDİLDİ] marker."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")

        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.IN_REVIEW.value,
        )
        db_session.add(task)
        db_session.commit()

        d_s01 = self._make_item_decision(
            task.id, "trans-s01", ReviewItemAction.REJECTED.value
        )
        db_session.add(d_s01)
        db_session.commit()

        item_decisions = {"trans-s01": d_s01}

        result = _assemble_translation_review(stories_data, {}, item_decisions)

        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        assert s01["text_tr"].startswith("[ÇEVİRİ REDDEDİLDİ")

    def test_unchanged_action_prepends_marker(self, db_session):
        """UNCHANGED decision (same as REJECTED) prepends marker."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")

        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.IN_REVIEW.value,
        )
        db_session.add(task)
        db_session.commit()

        d_s01 = self._make_item_decision(
            task.id, "trans-s01", ReviewItemAction.UNCHANGED.value
        )
        db_session.add(d_s01)
        db_session.commit()

        item_decisions = {"trans-s01": d_s01}

        result = _assemble_translation_review(stories_data, {}, item_decisions)

        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        assert s01["text_tr"].startswith("[ÇEVİRİ REDDEDİLDİ")

    def test_pending_keeps_translation(self, db_session):
        """PENDING decision (no explicit decision) keeps text_tr unchanged."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")
        original_s01_tr = stories_data["stories"][0]["text_tr"]

        result = _assemble_translation_review(stories_data, {}, {})  # no decisions

        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        assert s01["text_tr"] == original_s01_tr

    def test_accepted_keeps_translation(self, db_session):
        """ACCEPTED decision keeps text_tr unchanged."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")
        original_s01_tr = stories_data["stories"][0]["text_tr"]

        task = ReviewTask(
            episode_id="ep_ts_review",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.IN_REVIEW.value,
        )
        db_session.add(task)
        db_session.commit()

        d_s01 = self._make_item_decision(
            task.id, "trans-s01", ReviewItemAction.ACCEPTED.value
        )
        db_session.add(d_s01)
        db_session.commit()

        item_decisions = {"trans-s01": d_s01}

        result = _assemble_translation_review(stories_data, {}, item_decisions)

        s01 = next(s for s in result["stories"] if s["story_id"] == "s01")
        assert s01["text_tr"] == original_s01_tr

    def test_preserves_non_story_fields(self, db_session):
        """Assembly preserves top-level metadata fields from stories_data."""
        from btcedu.core.reviewer import _assemble_translation_review

        stories_data = _make_stories_translated("ep_ts_review")

        result = _assemble_translation_review(stories_data, {}, {})

        assert result["episode_id"] == "ep_ts_review"
        assert result["schema_version"] == "1.0"
        assert result["total_stories"] == 2


# ---------------------------------------------------------------------------
# 5. apply_item_decisions for stage="translate"
# ---------------------------------------------------------------------------


class TestApplyItemDecisionsTranslate:
    def test_writes_sidecar(self, db_session, tmp_path):
        """apply_item_decisions for stage='translate' writes reviewed sidecar."""
        from btcedu.core.reviewer import apply_item_decisions

        episode = Episode(
            episode_id="ep_apply_tr",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        # Write stories_translated.json
        outputs_dir = tmp_path / "outputs" / "ep_apply_tr"
        outputs_dir.mkdir(parents=True)
        stories_path = outputs_dir / "stories_translated.json"
        stories_path.write_text(
            json.dumps(_make_stories_translated("ep_apply_tr")), encoding="utf-8"
        )

        # Write translation_diff.json
        review_dir = outputs_dir / "review"
        review_dir.mkdir(parents=True)
        diff_path = review_dir / "translation_diff.json"
        diff_data = {
            "diff_type": "translation",
            "episode_id": "ep_apply_tr",
            "stories": [],
            "summary": {},
            "warnings": [],
        }
        diff_path.write_text(json.dumps(diff_data), encoding="utf-8")

        # Create review task
        task = ReviewTask(
            episode_id="ep_apply_tr",
            stage="translate",
            artifact_paths=json.dumps([str(stories_path)]),
            diff_path=str(diff_path),
            status=ReviewStatus.IN_REVIEW.value,
        )
        db_session.add(task)
        db_session.commit()

        # Patch _get_runtime_settings to return settings with our tmp_path
        from btcedu.config import Settings
        mock_settings = Settings(
            outputs_dir=str(tmp_path / "outputs"),
            transcripts_dir=str(tmp_path / "transcripts"),
        )

        with patch("btcedu.core.reviewer._get_runtime_settings", return_value=mock_settings):
            sidecar_path = apply_item_decisions(db_session, task.id)

        assert sidecar_path is not None
        sidecar = Path(sidecar_path)
        assert sidecar.exists()
        sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert "stories" in sidecar_data
        assert len(sidecar_data["stories"]) == 2


# ---------------------------------------------------------------------------
# 6. Chapterizer uses reviewed sidecar
# ---------------------------------------------------------------------------


class TestChapterizerUsesSidecar:
    def _make_chapter_response(self, episode_id: str) -> str:
        return json.dumps({
            "schema_version": "1.0",
            "episode_id": episode_id,
            "title": "tagesschau 20:00 Uhr",
            "total_chapters": 1,
            "estimated_duration_seconds": 4,
            "chapters": [
                {
                    "chapter_id": "ch01",
                    "title": "Test",
                    "order": 1,
                    "narration": {
                        "text": "Reviewed Turkish text.",
                        "word_count": 3,
                        "estimated_duration_seconds": 4,
                    },
                    "visual": {
                        "type": "b_roll",
                        "description": "Test",
                        "image_prompt": "Test image",
                    },
                    "overlays": [],
                    "transitions": {"in": "fade", "out": "cut"},
                    "notes": None,
                }
            ],
        })

    def test_chapterizer_prefers_reviewed_sidecar(
        self, db_session, settings_with_profiles, tmp_path
    ):
        """Chapterizer uses stories_translated.reviewed.json when it exists."""
        episode = Episode(
            episode_id="ep_sidecar",
            source="youtube_rss",
            title="tagesschau Test",
            url="https://youtube.com/watch?v=ep_sidecar",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        outputs_dir = tmp_path / "outputs" / "ep_sidecar"
        review_dir = outputs_dir / "review"
        review_dir.mkdir(parents=True)

        # Write both files
        stories_translated_path = outputs_dir / "stories_translated.json"
        stories_translated_path.write_text(
            json.dumps(_make_stories_translated("ep_sidecar")), encoding="utf-8"
        )

        reviewed_sidecar_path = review_dir / "stories_translated.reviewed.json"
        reviewed_sidecar_path.write_text(
            json.dumps(_make_stories_translated("ep_sidecar")), encoding="utf-8"
        )

        mock_resp = MagicMock()
        mock_resp.text = self._make_chapter_response("ep_sidecar")
        mock_resp.input_tokens = 100
        mock_resp.output_tokens = 300
        mock_resp.cost_usd = 0.01

        from btcedu.core.chapterizer import chapterize_script

        with patch("btcedu.core.chapterizer.call_claude", return_value=mock_resp):
            result = chapterize_script(db_session, "ep_sidecar", settings_with_profiles)

        assert result.skipped is False
        assert result.chapter_count == 1

        # Verify episode advanced to CHAPTERIZED
        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.CHAPTERIZED

    def test_chapterizer_falls_back_to_stories_translated(
        self, db_session, settings_with_profiles, tmp_path
    ):
        """Chapterizer uses stories_translated.json when no reviewed sidecar exists."""
        episode = Episode(
            episode_id="ep_nosidecar",
            source="youtube_rss",
            title="tagesschau Test",
            url="https://youtube.com/watch?v=ep_nosidecar",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        outputs_dir = tmp_path / "outputs" / "ep_nosidecar"
        outputs_dir.mkdir(parents=True)

        # Write only stories_translated.json (no reviewed sidecar)
        stories_translated_path = outputs_dir / "stories_translated.json"
        stories_translated_path.write_text(
            json.dumps(_make_stories_translated("ep_nosidecar")), encoding="utf-8"
        )

        mock_resp = MagicMock()
        mock_resp.text = self._make_chapter_response("ep_nosidecar")
        mock_resp.input_tokens = 100
        mock_resp.output_tokens = 300
        mock_resp.cost_usd = 0.01

        from btcedu.core.chapterizer import chapterize_script

        with patch("btcedu.core.chapterizer.call_claude", return_value=mock_resp):
            result = chapterize_script(db_session, "ep_nosidecar", settings_with_profiles)

        assert result.skipped is False
        assert result.chapter_count == 1


# ---------------------------------------------------------------------------
# 7. Revert episode TRANSLATED -> SEGMENTED
# ---------------------------------------------------------------------------


class TestRevertEpisodeTranslated:
    def test_reject_reverts_translated_to_segmented(self, db_session):
        """Rejecting a translate review reverts episode from TRANSLATED to SEGMENTED."""
        from btcedu.core.reviewer import reject_review

        episode = Episode(
            episode_id="ep_revert_tr",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_revert_tr",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        with patch("btcedu.core.reviewer._write_review_history"):
            reject_review(db_session, task.id, notes="Factual error in s01")

        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.SEGMENTED

    def test_request_changes_reverts_translated_to_segmented(self, db_session):
        """Requesting changes on a translate review reverts episode to SEGMENTED."""
        from btcedu.core.reviewer import request_changes

        episode = Episode(
            episode_id="ep_revert_rc",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_revert_rc",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        with patch("btcedu.core.reviewer._write_review_history"):
            request_changes(db_session, task.id, notes="Institution name wrong in s02")

        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.SEGMENTED

    def test_corrected_reverts_to_transcribed(self, db_session):
        """Existing revert: CORRECTED still reverts to TRANSCRIBED (no regression)."""
        from btcedu.core.reviewer import reject_review

        episode = Episode(
            episode_id="ep_rg1",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.CORRECTED,
            pipeline_version=2,
            content_profile="bitcoin_podcast",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_rg1",
            stage="correct",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        with patch("btcedu.core.reviewer._write_review_history"):
            reject_review(db_session, task.id)

        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.TRANSCRIBED


# ---------------------------------------------------------------------------
# 8. News checklist in get_review_detail
# ---------------------------------------------------------------------------


class TestReviewDetailChecklist:
    def test_tagesschau_review_detail_has_checklist(self, db_session):
        """get_review_detail for tagesschau episode includes 6-item news checklist."""
        from btcedu.core.reviewer import get_review_detail

        episode = Episode(
            episode_id="ep_checklist",
            source="youtube_rss",
            title="tagesschau Test",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            transcript_path=None,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_checklist",
            stage="translate",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        detail = get_review_detail(db_session, task.id)

        assert "review_checklist" in detail
        assert detail["review_checklist"] is not None
        assert len(detail["review_checklist"]) == 6

        checklist_ids = [item["id"] for item in detail["review_checklist"]]
        assert "factual_accuracy" in checklist_ids
        assert "political_neutrality" in checklist_ids
        assert "attribution_present" in checklist_ids
        assert "proper_nouns_correct" in checklist_ids
        assert "no_hallucination" in checklist_ids
        assert "register_correct" in checklist_ids

    def test_bitcoin_review_detail_no_checklist(self, db_session):
        """get_review_detail for bitcoin episode returns no checklist."""
        from btcedu.core.reviewer import get_review_detail

        episode = Episode(
            episode_id="ep_btc_chk",
            source="youtube_rss",
            title="Bitcoin Episode",
            url="https://example.com",
            status=EpisodeStatus.CORRECTED,
            transcript_path=None,
            pipeline_version=2,
            content_profile="bitcoin_podcast",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_btc_chk",
            stage="correct",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        detail = get_review_detail(db_session, task.id)

        # Bitcoin podcast has no news checklist
        assert detail.get("review_checklist") is None


# ---------------------------------------------------------------------------
# 9. Bilingual review mode in get_review_detail
# ---------------------------------------------------------------------------


class TestReviewDetailBilingual:
    def test_translate_task_has_bilingual_mode(self, db_session, tmp_path):
        """get_review_detail for translate stage returns bilingual review data."""
        from btcedu.core.reviewer import get_review_detail

        episode = Episode(
            episode_id="ep_bilingual",
            source="youtube_rss",
            title="tagesschau Test",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            transcript_path=None,
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        # Write translation_diff.json
        diff_dir = tmp_path / "ep_bilingual" / "review"
        diff_dir.mkdir(parents=True)
        diff_path = diff_dir / "translation_diff.json"
        diff_data = {
            "diff_type": "translation",
            "episode_id": "ep_bilingual",
            "source_language": "de",
            "target_language": "tr",
            "stories": [
                {
                    "item_id": "trans-s01",
                    "story_id": "s01",
                    "headline_de": "Test",
                    "headline_tr": "Test TR",
                    "text_de": "German text.",
                    "text_tr": "Turkish text.",
                    "word_count_de": 2,
                    "word_count_tr": 2,
                    "category": "politik",
                    "story_type": "meldung",
                }
            ],
            "summary": {
                "total_stories": 1,
                "total_words_de": 2,
                "total_words_tr": 2,
                "compression_ratio": 1.0,
            },
            "warnings": [],
        }
        diff_path.write_text(json.dumps(diff_data), encoding="utf-8")

        task = ReviewTask(
            episode_id="ep_bilingual",
            stage="translate",
            artifact_paths=json.dumps([]),
            diff_path=str(diff_path),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        detail = get_review_detail(db_session, task.id)

        assert detail["review_mode"] == "bilingual"
        assert detail["stories"] is not None
        assert len(detail["stories"]) == 1
        assert detail["compression_ratio"] == 1.0
        assert detail["translation_warnings"] == []

    def test_non_translate_task_no_bilingual_mode(self, db_session):
        """get_review_detail for non-translate stage has no bilingual mode."""
        from btcedu.core.reviewer import get_review_detail

        episode = Episode(
            episode_id="ep_no_bilingual",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.CORRECTED,
            transcript_path=None,
            pipeline_version=2,
            content_profile="bitcoin_podcast",
        )
        db_session.add(episode)
        db_session.commit()

        task = ReviewTask(
            episode_id="ep_no_bilingual",
            stage="correct",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        detail = get_review_detail(db_session, task.id)

        assert detail["review_mode"] is None
        assert detail["stories"] is None


# ---------------------------------------------------------------------------
# 10. _REVIEW_GATE_LABELS includes translate entry
# ---------------------------------------------------------------------------


class TestReviewGateLabels:
    def test_review_gate_labels_has_translate(self):
        """_REVIEW_GATE_LABELS contains 'translate' entry."""
        from btcedu.web.api import _REVIEW_GATE_LABELS

        assert "translate" in _REVIEW_GATE_LABELS
        gate_name, label = _REVIEW_GATE_LABELS["translate"]
        assert gate_name == "review_gate_translate"
        assert "Translation" in label

    def test_review_gate_labels_preserves_existing(self):
        """_REVIEW_GATE_LABELS still has correct, adapt, render, stock_images."""
        from btcedu.web.api import _REVIEW_GATE_LABELS

        assert "correct" in _REVIEW_GATE_LABELS
        assert "adapt" in _REVIEW_GATE_LABELS
        assert "render" in _REVIEW_GATE_LABELS
        assert "stock_images" in _REVIEW_GATE_LABELS


# ---------------------------------------------------------------------------
# 11. _load_item_texts_from_diff for translation diffs
# ---------------------------------------------------------------------------


class TestLoadItemTextsFromDiffTranslation:
    def test_loads_translation_diff_items(self, db_session, tmp_path):
        """_load_item_texts_from_diff returns bilingual text for translation diffs."""
        from btcedu.core.reviewer import _load_item_texts_from_diff

        diff_data = {
            "diff_type": "translation",
            "stories": [
                {
                    "item_id": "trans-s01",
                    "story_id": "s01",
                    "text_de": "German source.",
                    "text_tr": "Turkish translation.",
                    "category": "politik",
                }
            ],
        }
        diff_path = tmp_path / "translation_diff.json"
        diff_path.write_text(json.dumps(diff_data), encoding="utf-8")

        task = ReviewTask(
            episode_id="ep_load_test",
            stage="translate",
            artifact_paths=json.dumps([]),
            diff_path=str(diff_path),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        original, proposed, op_type = _load_item_texts_from_diff(task, "trans-s01")

        assert original == "German source."
        assert proposed == "Turkish translation."
        assert op_type == "politik"

    def test_returns_unknown_for_missing_item(self, db_session, tmp_path):
        """_load_item_texts_from_diff returns (None, None, 'unknown') for missing item."""
        from btcedu.core.reviewer import _load_item_texts_from_diff

        diff_data = {
            "diff_type": "translation",
            "stories": [],
        }
        diff_path = tmp_path / "translation_diff_empty.json"
        diff_path.write_text(json.dumps(diff_data), encoding="utf-8")

        task = ReviewTask(
            episode_id="ep_load_missing",
            stage="translate",
            artifact_paths=json.dumps([]),
            diff_path=str(diff_path),
            status=ReviewStatus.PENDING.value,
        )
        db_session.add(task)
        db_session.commit()

        original, proposed, op_type = _load_item_texts_from_diff(task, "trans-s99")

        assert original is None
        assert proposed is None
        assert op_type == "unknown"
