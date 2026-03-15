"""Tests for LLM-based stock photo candidate ranking (Phase 2)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.stock_images import (
    RankResult,
    _apply_rankings,
    _parse_ranking_response,
    rank_candidates,
)


@pytest.fixture(autouse=True)
def mock_extract_intents(tmp_path):
    """Phase 3: patch extract_chapter_intents so ranking tests stay isolated."""
    from btcedu.core.stock_images import IntentResult

    fake_result = IntentResult(
        episode_id="ep001",
        chapters_analyzed=0,
        cost_usd=0.0,
        intent_path=tmp_path / "ep001" / "images" / "candidates" / "intent_analysis.json",
    )
    with patch("btcedu.core.stock_images.extract_chapter_intents", return_value=fake_result):
        yield


@pytest.fixture
def settings():
    s = MagicMock()
    s.outputs_dir = ""
    s.pexels_api_key = "test-key"
    s.pexels_results_per_chapter = 3
    s.pexels_orientation = "landscape"
    s.pexels_download_size = "large2x"
    s.claude_model = "claude-sonnet-4-20250514"
    s.claude_max_tokens = 4096
    s.claude_temperature = 0.1
    s.max_episode_cost_usd = 10.0
    s.dry_run = False
    s.llm_provider = "anthropic"
    s.anthropic_api_key = "test-anthropic-key"
    return s


@pytest.fixture
def mock_chapters_doc():
    """ChapterDocument mock with 2 chapters."""
    ch1 = MagicMock()
    ch1.chapter_id = "ch01"
    ch1.title = "Bitcoin Madenciliği"
    ch1.visual = MagicMock()
    ch1.visual.type = "b_roll"
    ch1.visual.description = "Mining hardware close-up"
    ch1.narration = MagicMock()
    ch1.narration.text = "Bu bölümde madencilik konusu ele alınacak."

    ch2 = MagicMock()
    ch2.chapter_id = "ch02"
    ch2.title = "Blockchain Teknolojisi"
    ch2.visual = MagicMock()
    ch2.visual.type = "diagram"
    ch2.visual.description = "Blockchain diagram"
    ch2.narration = MagicMock()
    ch2.narration.text = "Blockchain teknolojisinin temelleri."

    doc = MagicMock()
    doc.chapters = [ch1, ch2]
    return doc


def _make_manifest(chapters_data, **extra):
    """Helper to build a candidates manifest dict."""
    base = {
        "episode_id": "ep001",
        "schema_version": "1.0",
        "searched_at": "2026-01-01T00:00:00",
        "chapters_hash": "abc123",
        "chapters": chapters_data,
    }
    base.update(extra)
    return base


def _make_candidates(n=3, start_id=100):
    """Generate n candidate entries."""
    return [
        {
            "pexels_id": start_id + i,
            "photographer": f"Photographer {i}",
            "photographer_url": f"https://pexels.com/@photo{i}",
            "source_url": f"https://pexels.com/photo/{start_id + i}",
            "download_url": f"https://images.pexels.com/{start_id + i}",
            "local_path": f"images/candidates/ch01/pexels_{start_id + i}.jpg",
            "alt_text": f"Photo {i} alt text",
            "width": 1880,
            "height": 1253,
            "size_bytes": 200000 + i * 1000,
            "downloaded_at": "2026-01-01T00:00:00",
            "selected": False,
            "locked": False,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# rank_candidates tests
# ---------------------------------------------------------------------------


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_calls_llm_per_chapter(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """One LLM call per chapter with candidates."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    # Create manifest with 2 chapters
    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin mining", "candidates": _make_candidates(3, 100)},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    result = rank_candidates(session, "ep001", settings)

    assert mock_claude.call_count == 2  # One per chapter
    assert result.chapters_ranked == 2


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_skips_locked(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """Locked chapters are not re-ranked."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    candidates = _make_candidates(2, 100)
    candidates[0]["locked"] = True
    candidates[0]["selected"] = True

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": candidates},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 200, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    result = rank_candidates(session, "ep001", settings)

    assert mock_claude.call_count == 1  # Only ch02
    assert result.chapters_skipped == 1
    assert result.chapters_ranked == 1


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_force_overrides_locked(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """force=True re-ranks locked chapters."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    candidates = _make_candidates(2, 100)
    candidates[0]["locked"] = True

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": candidates},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    result = rank_candidates(session, "ep001", settings, force=True)

    assert mock_claude.call_count == 2  # Both chapters
    assert result.chapters_ranked == 2


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
def test_rank_single_candidate_no_llm(
    mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """Single candidate auto-ranked as 1, no LLM call."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(1, 100)},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(1, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    session = MagicMock()

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        result = rank_candidates(session, "ep001", settings)
        mock_claude.assert_not_called()

    assert result.chapters_ranked == 2
    assert result.total_cost_usd == 0.0


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
def test_rank_no_candidates_skipped(
    mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """Chapters with 0 candidates are skipped."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": []},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = MagicMock(
            text='{"rankings": [{"pexels_id": 200, "rank": 1, "reason": "Good"}]}',
            cost_usd=0.005,
        )
        session = MagicMock()
        result = rank_candidates(session, "ep001", settings)

    assert result.chapters_skipped == 1
    assert result.chapters_ranked == 1


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_writes_rank_fields(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """rank and rank_reason written to manifest."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text=json.dumps({
            "rankings": [
                {"pexels_id": 100, "rank": 1, "reason": "Best match"},
                {"pexels_id": 101, "rank": 2, "reason": "Second best"},
            ]
        }),
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    ch01_cands = updated["chapters"]["ch01"]["candidates"]
    assert ch01_cands[0]["rank"] == 1
    assert ch01_cands[0]["rank_reason"] == "Best match"
    assert ch01_cands[1]["rank"] == 2


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_selects_top_ranked(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """selected=True on rank=1 candidate."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(3, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text=json.dumps({
            "rankings": [
                {"pexels_id": 102, "rank": 1, "reason": "Best"},
                {"pexels_id": 100, "rank": 2, "reason": "OK"},
                {"pexels_id": 101, "rank": 3, "reason": "Worst"},
            ]
        }),
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    ch01_cands = updated["chapters"]["ch01"]["candidates"]
    # Candidates should be sorted by rank
    selected = [c for c in ch01_cands if c["selected"]]
    assert len(selected) == 1
    assert selected[0]["pexels_id"] == 102


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_sets_pinned_by_llm(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """pinned_by='llm_rank' on chapter."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    assert updated["chapters"]["ch01"]["pinned_by"] == "llm_rank"


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_updates_manifest_metadata(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """ranked_at, ranking_model, ranking_cost_usd set."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    assert "ranked_at" in updated
    assert updated["ranking_model"] == "claude-sonnet-4-20250514"
    assert updated["ranking_cost_usd"] == 0.005


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_bumps_schema_version(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """schema_version set to '2.0'."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    assert updated["schema_version"] == "3.0"


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_invalid_llm_response_fallback(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """Malformed JSON → fall back to order-based ranking."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(3, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text="This is not valid JSON at all",
        cost_usd=0.005,
    )

    session = MagicMock()
    result = rank_candidates(session, "ep001", settings)

    # Should still succeed via fallback
    assert result.chapters_ranked == 1

    updated = json.loads(manifest_path.read_text())
    ch01_cands = updated["chapters"]["ch01"]["candidates"]
    # Fallback assigns ranks 1, 2, 3
    assert ch01_cands[0]["rank"] == 1
    assert ch01_cands[0]["selected"] is True


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_unknown_pexels_id_ignored(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """LLM returns unknown ID → ignored, others still ranked."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text=json.dumps({
            "rankings": [
                {"pexels_id": 999, "rank": 1, "reason": "Unknown"},
                {"pexels_id": 100, "rank": 2, "reason": "Known"},
            ]
        }),
        cost_usd=0.005,
    )

    session = MagicMock()
    rank_candidates(session, "ep001", settings)

    updated = json.loads(manifest_path.read_text())
    ch01_cands = updated["chapters"]["ch01"]["candidates"]
    # 100 should be ranked, 999 ignored
    ranked = [c for c in ch01_cands if c.get("rank")]
    assert any(c["pexels_id"] == 100 for c in ranked)


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
@patch("btcedu.services.claude_service.call_claude")
def test_rank_cost_accumulated(
    mock_claude, mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """Total cost = sum of per-chapter LLM costs."""
    settings.outputs_dir = str(tmp_path)
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
        "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

    mock_claude.return_value = MagicMock(
        text='{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Best"}]}',
        cost_usd=0.003,
    )

    session = MagicMock()
    result = rank_candidates(session, "ep001", settings)

    assert result.total_cost_usd == pytest.approx(0.006, abs=0.001)


@patch("btcedu.core.stock_images._load_chapters")
@patch("btcedu.core.stock_images._get_episode")
def test_rank_dry_run_no_llm(
    mock_get_ep, mock_load_ch, tmp_path, settings, mock_chapters_doc
):
    """dry_run=True → no LLM call, ranks by order."""
    settings.outputs_dir = str(tmp_path)
    settings.dry_run = True
    mock_get_ep.return_value = MagicMock()
    mock_load_ch.return_value = mock_chapters_doc

    manifest = _make_manifest({
        "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(3, 100)},
    })
    manifest_dir = tmp_path / "ep001" / "images" / "candidates"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        session = MagicMock()
        result = rank_candidates(session, "ep001", settings)
        mock_claude.assert_not_called()

    assert result.chapters_ranked == 1
    assert result.total_cost_usd == 0.0

    updated = json.loads(manifest_path.read_text())
    cands = updated["chapters"]["ch01"]["candidates"]
    assert cands[0]["rank"] == 1
    assert cands[1]["rank"] == 2
    assert cands[2]["rank"] == 3


def test_rank_result_dataclass():
    """RankResult fields populated correctly."""
    r = RankResult(
        episode_id="ep001",
        chapters_ranked=5,
        chapters_skipped=2,
        total_cost_usd=0.015,
    )
    assert r.episode_id == "ep001"
    assert r.chapters_ranked == 5
    assert r.chapters_skipped == 2
    assert r.total_cost_usd == 0.015


# ---------------------------------------------------------------------------
# _parse_ranking_response tests
# ---------------------------------------------------------------------------


def test_parse_ranking_valid():
    text = '{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Good"}]}'
    result = _parse_ranking_response(text, [{"pexels_id": 100}])
    assert len(result) == 1
    assert result[0]["rank"] == 1


def test_parse_ranking_with_markdown_fences():
    text = '```json\n{"rankings": [{"pexels_id": 100, "rank": 1, "reason": "Good"}]}\n```'
    result = _parse_ranking_response(text, [{"pexels_id": 100}])
    assert len(result) == 1


def test_parse_ranking_invalid_json():
    result = _parse_ranking_response("not json", [])
    assert result == []


# ---------------------------------------------------------------------------
# _apply_rankings tests
# ---------------------------------------------------------------------------


def test_apply_rankings_valid():
    candidates = _make_candidates(3, 100)
    rankings = [
        {"pexels_id": 102, "rank": 1, "reason": "Best"},
        {"pexels_id": 100, "rank": 2, "reason": "OK"},
        {"pexels_id": 101, "rank": 3, "reason": "Worst"},
    ]
    _apply_rankings(candidates, rankings)

    assert candidates[0]["rank"] == 1
    assert candidates[0]["pexels_id"] == 102
    assert candidates[0]["selected"] is True
    assert candidates[1]["selected"] is False


def test_apply_rankings_empty_fallback():
    candidates = _make_candidates(2, 100)
    _apply_rankings(candidates, [])

    assert candidates[0]["rank"] == 1
    assert candidates[0]["selected"] is True
    assert candidates[1]["rank"] == 2
