"""Tests for Phase 4 content generation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.generator import (
    ARTIFACT_FILENAMES,
    build_query_terms,
    format_chunks_for_prompt,
    generate_content,
    refine_content,
    retrieve_chunks,
    save_retrieval_snapshot,
)
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.services.claude_service import (
    ClaudeResponse,
    calculate_cost,
    compute_prompt_hash,
)


def _mock_claude_response(text="Mock Turkish content [ep001_C0001]"):
    """Create a mock ClaudeResponse for testing."""
    return ClaudeResponse(
        text=text,
        input_tokens=5000,
        output_tokens=1500,
        cost_usd=0.0375,
        model="claude-sonnet-4-20250514",
    )


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        outputs_dir=str(tmp_path / "outputs"),
        claude_model="claude-sonnet-4-20250514",
        claude_max_tokens=4096,
        claude_temperature=0.3,
        dry_run=False,
    )


# ── Query Term Extraction ────────────────────────────────────────


class TestBuildQueryTerms:
    def test_extracts_content_words(self):
        terms = build_query_terms("Bitcoin und die Zukunft des Geldes")
        assert '"Bitcoin"' in terms
        assert '"Zukunft"' in terms
        assert '"Geldes"' in terms

    def test_filters_stopwords(self):
        terms = build_query_terms("Bitcoin und die Zukunft des Geldes")
        # Stopwords should not appear (neither quoted nor unquoted)
        joined = " ".join(terms)
        assert "und" not in joined.replace('"', '').split()
        assert "die" not in joined.replace('"', '').split()
        assert "des" not in joined.replace('"', '').split()

    def test_keeps_bitcoin_terms(self):
        terms = build_query_terms("Blockchain und Lightning Network")
        assert '"Blockchain"' in terms
        assert '"Lightning"' in terms
        assert '"Network"' in terms

    def test_splits_hyphenated_words(self):
        terms = build_query_terms("Das Saylor-Kalkül: Was hat Strategy vor?")
        assert '"Saylor"' in terms
        assert '"Kalkül"' in terms
        assert '"Strategy"' in terms

    def test_strips_brackets(self):
        terms = build_query_terms("Bitcoin Prognosen [2026]")
        assert '"Bitcoin"' in terms
        assert '"Prognosen"' in terms
        assert '"2026"' in terms


# ── Chunk Retrieval ───────────────────────────────────────────────


class TestRetrieveChunks:
    def test_retrieves_by_fts_query(self, db_session, chunked_episode):
        chunks = retrieve_chunks(db_session, "ep001", ["Bitcoin"], top_k=16)
        assert len(chunks) > 0
        assert all("text" in c for c in chunks)
        assert all("chunk_id" in c for c in chunks)

    def test_falls_back_to_ordinal(self, db_session, chunked_episode):
        chunks = retrieve_chunks(
            db_session, "ep001", ["xyzzythisnotexist"], top_k=16
        )
        assert len(chunks) > 0  # Should fall back to ordinal

    def test_respects_top_k(self, db_session, chunked_episode):
        chunks = retrieve_chunks(db_session, "ep001", ["Bitcoin"], top_k=3)
        assert len(chunks) <= 3

    def test_returns_full_text(self, db_session, chunked_episode):
        chunks = retrieve_chunks(db_session, "ep001", ["Bitcoin"], top_k=5)
        for c in chunks:
            assert len(c["text"]) > 10  # Not just a snippet


# ── Retrieval Snapshot ────────────────────────────────────────────


class TestSaveRetrievalSnapshot:
    def test_creates_snapshot_file(self, tmp_path, db_session, chunked_episode):
        chunks = retrieve_chunks(db_session, "ep001", ["Bitcoin"], top_k=5)
        path = save_retrieval_snapshot(
            chunks, "outline", tmp_path, ["Bitcoin"], top_k=5
        )
        assert Path(path).exists()
        data = json.loads(Path(path).read_text())
        assert data["artifact_type"] == "outline"
        assert data["top_k"] == 5
        assert len(data["chunks"]) > 0


# ── Content Generation (mocked Claude) ───────────────────────────


class TestGenerateContent:
    @patch("btcedu.core.generator.call_claude")
    def test_creates_all_6_artifacts(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        result = generate_content(db_session, "ep001", settings)

        assert len(result.artifacts) == 6
        for path in result.artifacts:
            assert Path(path).exists()

    @patch("btcedu.core.generator.call_claude")
    def test_creates_retrieval_snapshots(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        generate_content(db_session, "ep001", settings)

        retrieval_dir = tmp_path / "outputs" / "ep001" / "retrieval"
        assert retrieval_dir.exists()
        snapshots = list(retrieval_dir.glob("*_snapshot.json"))
        assert len(snapshots) == 6

    @patch("btcedu.core.generator.call_claude")
    def test_updates_status_to_generated(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        generate_content(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert ep.status == EpisodeStatus.GENERATED

    @patch("btcedu.core.generator.call_claude")
    def test_creates_pipeline_run(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        result = generate_content(db_session, "ep001", settings)

        run = (
            db_session.query(PipelineRun)
            .filter_by(stage=PipelineStage.GENERATE)
            .first()
        )
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        assert run.input_tokens == result.total_input_tokens
        assert run.estimated_cost_usd > 0

    @patch("btcedu.core.generator.call_claude")
    def test_persists_content_artifacts(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        generate_content(db_session, "ep001", settings)

        artifacts = db_session.query(ContentArtifact).filter_by(episode_id="ep001").all()
        assert len(artifacts) == 6
        types = {a.artifact_type for a in artifacts}
        assert types == {"outline", "script", "shorts", "visuals", "qa", "publishing"}

    @patch("btcedu.core.generator.call_claude")
    def test_skips_existing_artifacts(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        # First run creates all
        generate_content(db_session, "ep001", settings)
        call_count_first = mock_claude.call_count

        # Reset episode status for second run
        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        ep.status = EpisodeStatus.CHUNKED
        db_session.commit()

        # Second run should skip (files exist)
        mock_claude.reset_mock()
        generate_content(db_session, "ep001", settings)
        assert mock_claude.call_count == 0  # All skipped

    @patch("btcedu.core.generator.call_claude")
    def test_force_regenerates(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        generate_content(db_session, "ep001", settings)

        # Reset status
        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        ep.status = EpisodeStatus.CHUNKED
        db_session.commit()

        mock_claude.reset_mock()
        generate_content(db_session, "ep001", settings, force=True)
        assert mock_claude.call_count == 6  # All regenerated

    def test_rejects_wrong_status(self, db_session):
        ep = Episode(
            episode_id="ep002",
            source="youtube_rss",
            title="Test",
            url="https://youtube.com/watch?v=ep002",
            status=EpisodeStatus.NEW,
        )
        db_session.add(ep)
        db_session.commit()

        settings = Settings(outputs_dir="/tmp/test")
        with pytest.raises(ValueError, match="expected 'chunked'"):
            generate_content(db_session, "ep002", settings)

    def test_rejects_unknown_episode(self, db_session):
        settings = Settings(outputs_dir="/tmp/test")
        with pytest.raises(ValueError, match="Episode not found"):
            generate_content(db_session, "nonexistent", settings)

    @patch("btcedu.core.generator.call_claude")
    def test_output_filenames(self, mock_claude, db_session, chunked_episode, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        settings = _make_settings(tmp_path)

        generate_content(db_session, "ep001", settings)

        output_dir = tmp_path / "outputs" / "ep001"
        assert (output_dir / "outline.tr.md").exists()
        assert (output_dir / "script.long.tr.md").exists()
        assert (output_dir / "shorts.tr.json").exists()
        assert (output_dir / "visuals.json").exists()
        assert (output_dir / "qa.json").exists()
        assert (output_dir / "publishing_pack.json").exists()


# ── Dry Run ───────────────────────────────────────────────────────


class TestDryRun:
    def test_writes_payload_no_api_call(self, db_session, chunked_episode, tmp_path):
        settings = Settings(
            anthropic_api_key="sk-ant-test",
            outputs_dir=str(tmp_path / "outputs"),
            dry_run=True,
        )

        result = generate_content(db_session, "ep001", settings)

        # Dry-run payload files should exist
        output_dir = tmp_path / "outputs" / "ep001"
        dry_run_files = list(output_dir.glob("dry_run_*.json"))
        assert len(dry_run_files) == 6

        # Output artifacts should also exist (with dry run text)
        assert len(result.artifacts) == 6
        assert result.total_cost_usd == 0.0


# ── Prompt Constraints ────────────────────────────────────────────


class TestPromptConstraints:
    def test_system_prompt_has_citation_format(self):
        from btcedu.prompts.system import SYSTEM_PROMPT

        assert "EPISODEID_C####" in SYSTEM_PROMPT or "_C####" in SYSTEM_PROMPT

    def test_system_prompt_has_sources_only_rule(self):
        from btcedu.prompts.system import SYSTEM_PROMPT

        assert "YALNIZCA" in SYSTEM_PROMPT
        assert "KAYNAK" in SYSTEM_PROMPT

    def test_system_prompt_has_no_financial_advice(self):
        from btcedu.prompts.system import SYSTEM_PROMPT

        assert "FINANSAL" in SYSTEM_PROMPT or "finansal" in SYSTEM_PROMPT

    def test_system_prompt_has_kaynaklarda_yok(self):
        from btcedu.prompts.system import SYSTEM_PROMPT

        assert "Kaynaklarda yok" in SYSTEM_PROMPT

    def test_system_prompt_has_disclaimer(self):
        from btcedu.prompts.system import SYSTEM_PROMPT

        assert "egitim amaclidir" in SYSTEM_PROMPT

    def test_all_prompts_mention_citation_format(self):
        from btcedu.prompts import outline, qa, script, shorts, visuals

        # outline takes 3 args
        assert "_C" in outline.build_user_prompt("Test", "ep001", "chunks")
        # script, shorts, visuals take 4 args (chunks_text + outline_text)
        for mod in [script, shorts, visuals]:
            prompt = mod.build_user_prompt("Test", "ep001", "chunks", "outline")
            assert "_C" in prompt, f"{mod.__name__} missing citation format"
        # qa takes chunks_text + script_text
        assert "_C" in qa.build_user_prompt("Test", "ep001", "chunks", "script")


# ── Claude Service ────────────────────────────────────────────────


class TestClaudeService:
    def test_cost_calculation(self):
        # 1M input = $3, 1M output = $15
        cost = calculate_cost(1_000_000, 1_000_000)
        assert cost == 18.0

    def test_cost_small(self):
        cost = calculate_cost(5000, 1500)
        assert cost == pytest.approx(0.0375, abs=0.001)

    def test_prompt_hash_deterministic(self):
        h1 = compute_prompt_hash("template", "model", 0.3, ["a", "b"])
        h2 = compute_prompt_hash("template", "model", 0.3, ["a", "b"])
        assert h1 == h2

    def test_prompt_hash_changes_on_different_input(self):
        h1 = compute_prompt_hash("template", "model", 0.3, ["a", "b"])
        h2 = compute_prompt_hash("template", "model", 0.3, ["a", "c"])
        assert h1 != h2

    def test_prompt_hash_order_independent(self):
        h1 = compute_prompt_hash("template", "model", 0.3, ["b", "a"])
        h2 = compute_prompt_hash("template", "model", 0.3, ["a", "b"])
        assert h1 == h2  # sorted internally


# ── Format Chunks ─────────────────────────────────────────────────


class TestFormatChunks:
    def test_includes_citation_ids(self):
        chunks = [
            {"chunk_id": "ep001_001", "episode_id": "ep001", "ordinal": 1,
             "text": "Test text", "rank": 0},
        ]
        formatted = format_chunks_for_prompt(chunks, "ep001")
        assert "[ep001_C0001]" in formatted
        assert "Test text" in formatted


# ── Refine Content (mocked Claude) ──────────────────────────────


def _create_generated_episode(db_session, tmp_path):
    """Create a GENERATED episode with v1 artifacts on disk."""
    ep = Episode(
        episode_id="ep_gen",
        source="youtube_rss",
        title="Bitcoin und die Zukunft des Geldes",
        url="https://youtube.com/watch?v=ep_gen",
        status=EpisodeStatus.GENERATED,
    )
    db_session.add(ep)
    db_session.commit()

    output_dir = tmp_path / "outputs" / "ep_gen"
    output_dir.mkdir(parents=True)
    (output_dir / "outline.tr.md").write_text("# Outline v1\n- Punkt 1 [ep_gen_C0001]", encoding="utf-8")
    (output_dir / "script.long.tr.md").write_text("# Script v1\nBitcoin bir [ep_gen_C0001]...", encoding="utf-8")
    (output_dir / "qa.json").write_text('{"claims": [{"status": "supported"}]}', encoding="utf-8")

    return ep


class TestRefineContent:
    @patch("btcedu.core.generator.call_claude")
    def test_creates_3_v2_artifacts(self, mock_claude, db_session, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        _create_generated_episode(db_session, tmp_path)
        settings = _make_settings(tmp_path)

        result = refine_content(db_session, "ep_gen", settings)

        assert len(result.artifacts) == 3
        for path in result.artifacts:
            assert Path(path).exists()

    @patch("btcedu.core.generator.call_claude")
    def test_updates_status_to_refined(self, mock_claude, db_session, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        _create_generated_episode(db_session, tmp_path)
        settings = _make_settings(tmp_path)

        refine_content(db_session, "ep_gen", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep_gen").first()
        assert ep.status == EpisodeStatus.REFINED

    @patch("btcedu.core.generator.call_claude")
    def test_creates_pipeline_run_with_refine_stage(self, mock_claude, db_session, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        _create_generated_episode(db_session, tmp_path)
        settings = _make_settings(tmp_path)

        refine_content(db_session, "ep_gen", settings)

        run = (
            db_session.query(PipelineRun)
            .filter_by(stage=PipelineStage.REFINE)
            .first()
        )
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        assert run.estimated_cost_usd > 0

    @patch("btcedu.core.generator.call_claude")
    def test_output_v2_filenames(self, mock_claude, db_session, tmp_path):
        mock_claude.return_value = _mock_claude_response()
        _create_generated_episode(db_session, tmp_path)
        settings = _make_settings(tmp_path)

        refine_content(db_session, "ep_gen", settings)

        output_dir = tmp_path / "outputs" / "ep_gen"
        assert (output_dir / "outline.tr.v2.md").exists()
        assert (output_dir / "script.long.tr.v2.md").exists()
        assert (output_dir / "publishing_pack.v2.json").exists()

    def test_rejects_wrong_status(self, db_session):
        ep = Episode(
            episode_id="ep_new",
            source="youtube_rss",
            title="Test",
            url="https://youtube.com/watch?v=ep_new",
            status=EpisodeStatus.NEW,
        )
        db_session.add(ep)
        db_session.commit()

        settings = Settings(outputs_dir="/tmp/test")
        with pytest.raises(ValueError, match="expected 'generated'"):
            refine_content(db_session, "ep_new", settings)

    def test_rejects_missing_v1_artifacts(self, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_no_files",
            source="youtube_rss",
            title="Test",
            url="https://youtube.com/watch?v=ep_no_files",
            status=EpisodeStatus.GENERATED,
        )
        db_session.add(ep)
        db_session.commit()

        settings = _make_settings(tmp_path)
        with pytest.raises(ValueError, match="Missing required input"):
            refine_content(db_session, "ep_no_files", settings)
