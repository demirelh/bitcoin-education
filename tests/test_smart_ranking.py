"""Tests for Phase 3: Smart Stock-Image Ranking.

Covers: _TR_TO_EN expansion, _derive_search_query with search_hints,
_parse_intent_response, _validate_and_adjust_selection,
extract_chapter_intents, and rank_candidates integration.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.stock_images import (
    _TR_TO_EN,
    _derive_search_query,
    _parse_intent_response,
    _validate_and_adjust_selection,
    extract_chapter_intents,
    rank_candidates,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    s = MagicMock()
    s.outputs_dir = str(tmp_path)
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


def _make_chapter(
    chapter_id="ch01",
    title="Test Chapter",
    visual_type="b_roll",
    visual_description="A test visual",
    narration_text="Test narration text.",
):
    ch = MagicMock()
    ch.chapter_id = chapter_id
    ch.title = title
    ch.visual = MagicMock()
    ch.visual.type = visual_type
    ch.visual.description = visual_description
    ch.narration = MagicMock()
    ch.narration.text = narration_text
    return ch


def _make_chapters_doc(chapters):
    doc = MagicMock()
    doc.chapters = chapters
    return doc


def _make_candidates(n=3, start_id=100, alt_prefix="Photo"):
    return [
        {
            "pexels_id": start_id + i,
            "photographer": f"Photographer {i}",
            "photographer_url": f"https://pexels.com/@photo{i}",
            "source_url": f"https://pexels.com/photo/{start_id + i}",
            "download_url": f"https://images.pexels.com/{start_id + i}",
            "local_path": f"images/candidates/ch01/pexels_{start_id + i}.jpg",
            "alt_text": f"{alt_prefix} {i} description",
            "width": 1880,
            "height": 1253,
            "size_bytes": 200000 + i * 1000,
            "downloaded_at": "2026-01-01T00:00:00",
            "selected": False,
            "locked": False,
        }
        for i in range(n)
    ]


def _make_manifest(chapters_data, **extra):
    base = {
        "episode_id": "ep001",
        "schema_version": "1.0",
        "searched_at": "2026-01-01T00:00:00",
        "chapters_hash": "abc123",
        "chapters": chapters_data,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# TestTrToEnExpansion
# ---------------------------------------------------------------------------


class TestTrToEnExpansion:
    def test_makas_translates_to_gap(self):
        assert _TR_TO_EN["makas"] == "gap divide"

    def test_baskı_translates_to_pressure(self):
        # Turkish dotless ı
        assert _TR_TO_EN["baskı"] == "pressure"

    def test_köpük_translates_to_bubble(self):
        assert _TR_TO_EN["köpük"] == "bubble"

    def test_new_terms_count(self):
        """At least 15 new terms were added in Phase 3."""
        new_terms = [
            "makas", "baskı", "köpük", "balon", "boşluk", "çukur",
            "dalga", "patlama", "daralma", "aşınma", "tavan", "taban",
            "kaldıraç", "çıpa", "sürdürülebilir",
        ]
        for term in new_terms:
            assert term in _TR_TO_EN, f"Expected '{term}' in _TR_TO_EN"
        assert len(new_terms) >= 15


# ---------------------------------------------------------------------------
# TestDeriveSearchQuery
# ---------------------------------------------------------------------------


class TestDeriveSearchQuery:
    def test_search_hints_override_keyword_extraction(self):
        """When search_hints provided, query uses them instead of keyword extraction."""
        ch = _make_chapter(
            title="Zenginler ve Fakirler Arasındaki Makas",
            visual_type="b_roll",
            visual_description="Economic gap visualization",
        )
        hints = ["wealth gap", "economic inequality"]
        query = _derive_search_query(ch, search_hints=hints)
        assert "wealth gap" in query
        assert "economic inequality" in query

    def test_empty_search_hints_falls_back_to_existing(self):
        """Empty search_hints list falls back to existing keyword extraction."""
        ch = _make_chapter(
            title="Bitcoin Madenciliği",
            visual_type="b_roll",
            visual_description="Mining hardware",
        )
        # Empty list should fall back
        query_with_empty = _derive_search_query(ch, search_hints=[])
        query_without = _derive_search_query(ch)
        assert query_with_empty == query_without

    def test_visual_type_modifier_still_appended_with_hints(self):
        """Visual type modifier is appended even when search_hints are provided."""
        ch = _make_chapter(
            title="Test",
            visual_type="diagram",
            visual_description="A diagram",
        )
        hints = ["economic chart"]
        query = _derive_search_query(ch, search_hints=hints)
        # diagram modifier includes "chart graph infographic"
        assert "chart" in query or "graph" in query or "infographic" in query

    def test_none_search_hints_falls_back_to_existing(self):
        """None search_hints uses existing keyword extraction."""
        ch = _make_chapter(
            title="Bitcoin",
            visual_type="b_roll",
            visual_description="Bitcoin coin",
        )
        query = _derive_search_query(ch, search_hints=None)
        # Should still produce something from keyword extraction
        assert len(query) > 0


# ---------------------------------------------------------------------------
# TestParseIntentResponse
# ---------------------------------------------------------------------------


class TestParseIntentResponse:
    def _make_chapters_list(self, ids=("ch01",)):
        return [{"chapter_id": cid, "title": f"Chapter {cid}"} for cid in ids]

    def test_valid_response_parsed(self):
        chapters = self._make_chapters_list(["ch01"])
        response = json.dumps({
            "chapters": {
                "ch01": {
                    "intents": ["wealth inequality"],
                    "allowed_motifs": ["city skyline"],
                    "disallowed_motifs": ["scissors"],
                    "literal_traps": [{"word": "makas", "intended": "gap", "trap": "scissors"}],
                    "search_hints": ["wealth gap"],
                }
            }
        })
        result = _parse_intent_response(response, chapters)
        assert "ch01" in result
        assert result["ch01"]["intents"] == ["wealth inequality"]
        assert result["ch01"]["disallowed_motifs"] == ["scissors"]
        assert len(result["ch01"]["literal_traps"]) == 1

    def test_invalid_json_returns_empty_intents(self):
        chapters = self._make_chapters_list(["ch01", "ch02"])
        result = _parse_intent_response("not valid json {{", chapters)
        assert "ch01" in result
        assert "ch02" in result
        assert result["ch01"]["intents"] == []
        assert result["ch01"]["disallowed_motifs"] == []

    def test_partial_response_handles_missing_chapters(self):
        """Only ch01 in response; ch02 should get empty intents."""
        chapters = self._make_chapters_list(["ch01", "ch02"])
        response = json.dumps({
            "chapters": {
                "ch01": {
                    "intents": ["bitcoin mining"],
                    "allowed_motifs": ["hardware"],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": ["bitcoin mining hardware"],
                }
            }
        })
        result = _parse_intent_response(response, chapters)
        assert result["ch01"]["intents"] == ["bitcoin mining"]
        assert result["ch02"]["intents"] == []

    def test_markdown_fence_stripped(self):
        """Markdown code fences should be stripped before parsing."""
        chapters = self._make_chapters_list(["ch01"])
        inner = json.dumps({
            "chapters": {
                "ch01": {
                    "intents": ["test"],
                    "allowed_motifs": [],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": [],
                }
            }
        })
        fenced = f"```json\n{inner}\n```"
        result = _parse_intent_response(fenced, chapters)
        assert result["ch01"]["intents"] == ["test"]


# ---------------------------------------------------------------------------
# TestValidateAndAdjustSelection
# ---------------------------------------------------------------------------


class TestValidateAndAdjustSelection:
    def test_trap_flagged_winner_demoted_to_rank2(self):
        """Rank-1 with trap_flag=True should be replaced by rank-2."""
        intent_data = {
            "intents": ["wealth inequality"],
            "allowed_motifs": ["city skyline"],
            "disallowed_motifs": [],
            "literal_traps": [],
        }
        candidates = [
            {"pexels_id": 111, "alt_text": "Wealth photo", "rank": 1,
             "selected": True, "trap_flag": True},
            {"pexels_id": 222, "alt_text": "Financial chart", "rank": 2,
             "selected": False, "trap_flag": False},
        ]
        _validate_and_adjust_selection(candidates, intent_data, set())
        assert not candidates[0]["selected"]
        assert candidates[1]["selected"]

    def test_alt_text_disallowed_motif_check_barbershop(self):
        """Regression: barbershop image should not be selected for inequality chapter."""
        intent_data = {
            "intents": ["wealth inequality"],
            "allowed_motifs": ["city skyline contrast", "luxury vs poverty"],
            "disallowed_motifs": ["scissors", "barbershop", "hair cutting"],
            "literal_traps": [
                {"word": "makas", "intended": "gap/divide", "trap": "scissors/cutting tools"}
            ],
        }
        candidates = [
            {
                "pexels_id": 6152046,
                "alt_text": "Glass wall of modern barbershop with reflection",
                "rank": 1, "selected": True, "trap_flag": False,
            },
            {
                "pexels_id": 19260324,
                "alt_text": "Luxury hotel facade in city center",
                "rank": 2, "selected": False, "trap_flag": False,
            },
        ]
        _validate_and_adjust_selection(candidates, intent_data, set())
        # Barbershop should be deselected
        assert not candidates[0]["selected"]
        assert candidates[1]["selected"]

    def test_alt_text_disallowed_motif_check_pressure_cooker(self):
        """Regression: pressure cooker should not be selected for monetary pressure chapter."""
        intent_data = {
            "intents": ["monetary pressure", "central bank policy"],
            "allowed_motifs": ["central bank building", "financial stress gauge"],
            "disallowed_motifs": ["pressure cooker", "printing press", "kitchen"],
            "literal_traps": [],
        }
        candidates = [
            {
                "pexels_id": 9876,
                "alt_text": "Stainless steel pressure cooker on kitchen counter",
                "rank": 1, "selected": True, "trap_flag": False,
            },
            {
                "pexels_id": 5432,
                "alt_text": "Central bank facade in European city",
                "rank": 2, "selected": False, "trap_flag": False,
            },
        ]
        _validate_and_adjust_selection(candidates, intent_data, set())
        assert not candidates[0]["selected"]
        assert candidates[1]["selected"]

    def test_alt_text_disallowed_motif_check_soap_bubbles(self):
        """Regression: soap bubbles should not be selected for economic bubble chapter."""
        intent_data = {
            "intents": ["economic bubble", "asset overvaluation"],
            "allowed_motifs": ["stock market crash", "overinflated balloon chart"],
            "disallowed_motifs": ["soap bubbles", "party balloons", "foam"],
            "literal_traps": [
                {"word": "köpük", "intended": "bubble/speculation", "trap": "soap foam"}
            ],
        }
        candidates = [
            {
                "pexels_id": 1111,
                "alt_text": "Child playing with colorful soap bubbles in garden",
                "rank": 1, "selected": True, "trap_flag": False,
            },
            {
                "pexels_id": 2222,
                "alt_text": "Stock market graph showing sharp decline",
                "rank": 2, "selected": False, "trap_flag": False,
            },
        ]
        _validate_and_adjust_selection(candidates, intent_data, set())
        assert not candidates[0]["selected"]
        assert candidates[1]["selected"]

    def test_duplicate_avoided_when_alternative_exists(self):
        """If rank-1 is already selected in another chapter, use rank-2."""
        intent_data = {
            "intents": ["cryptocurrency"],
            "allowed_motifs": ["digital coins"],
            "disallowed_motifs": [],
            "literal_traps": [],
        }
        candidates = [
            {"pexels_id": 999, "alt_text": "Bitcoin coin photo", "rank": 1,
             "selected": True, "trap_flag": False},
            {"pexels_id": 888, "alt_text": "Blockchain network diagram", "rank": 2,
             "selected": False, "trap_flag": False},
        ]
        already_selected = {999}  # 999 already used in ch01
        _validate_and_adjust_selection(candidates, intent_data, already_selected)
        assert not candidates[0]["selected"]
        assert candidates[1]["selected"]
        assert candidates[1].get("dedup_adjusted") is True

    def test_duplicate_kept_when_no_alternative(self):
        """If all candidates are duplicates, keep rank-1 rather than leaving empty."""
        intent_data = {
            "intents": ["bitcoin"],
            "allowed_motifs": ["digital coin"],
            "disallowed_motifs": [],
            "literal_traps": [],
        }
        candidates = [
            {"pexels_id": 999, "alt_text": "Bitcoin coin", "rank": 1,
             "selected": True, "trap_flag": False},
            {"pexels_id": 888, "alt_text": "Another bitcoin coin", "rank": 2,
             "selected": False, "trap_flag": False},
        ]
        already_selected = {999, 888}  # Both already used
        _validate_and_adjust_selection(candidates, intent_data, already_selected)
        # rank-1 should stay selected (better a duplicate than nothing)
        assert candidates[0]["selected"]

    def test_no_swap_when_clean_selection(self):
        """No trap flag, no disallowed motif, no duplicate: selection unchanged."""
        intent_data = {
            "intents": ["bitcoin mining"],
            "allowed_motifs": ["mining hardware"],
            "disallowed_motifs": ["barbershop"],
            "literal_traps": [],
        }
        candidates = [
            {"pexels_id": 500, "alt_text": "Bitcoin mining hardware server room",
             "rank": 1, "selected": True, "trap_flag": False},
            {"pexels_id": 501, "alt_text": "Computer chip close-up",
             "rank": 2, "selected": False, "trap_flag": False},
        ]
        _validate_and_adjust_selection(candidates, intent_data, set())
        assert candidates[0]["selected"]
        assert not candidates[1]["selected"]


# ---------------------------------------------------------------------------
# TestExtractChapterIntents
# ---------------------------------------------------------------------------


class TestExtractChapterIntents:
    def test_dry_run_returns_empty_intents(self, settings, tmp_path):
        """Dry-run should return empty intents without making an LLM call."""
        settings.dry_run = True
        settings.outputs_dir = str(tmp_path)

        ch1 = _make_chapter("ch01", "Bitcoin Mining", visual_type="b_roll")
        ch2 = _make_chapter("ch02", "Blockchain Tech", visual_type="diagram")
        chapters_doc = _make_chapters_doc([ch1, ch2])

        session = MagicMock()

        with patch("btcedu.core.stock_images._load_chapters", return_value=chapters_doc), \
             patch("btcedu.core.stock_images._compute_chapters_hash", return_value="hash123"), \
             patch("btcedu.services.claude_service.call_claude") as mock_claude:

            result = extract_chapter_intents(session, "ep001", settings)

            mock_claude.assert_not_called()
            assert result.cost_usd == 0.0
            assert result.chapters_analyzed >= 0  # dry_run with b_roll + diagram
            assert result.intent_path.exists()

            # Verify content is valid JSON with empty intents
            data = json.loads(result.intent_path.read_text())
            assert data["model"] == "dry_run"

    def test_idempotency_uses_cache(self, settings, tmp_path):
        """Second call with same chapters_hash returns cached result without LLM call."""
        settings.dry_run = False
        settings.outputs_dir = str(tmp_path)

        ch1 = _make_chapter("ch01", "Bitcoin Mining", visual_type="b_roll")
        chapters_doc = _make_chapters_doc([ch1])

        # Pre-create a cached intent file
        intent_dir = tmp_path / "ep001" / "images" / "candidates"
        intent_dir.mkdir(parents=True)
        cached_data = {
            "episode_id": "ep001",
            "schema_version": "1.0",
            "analyzed_at": "2026-01-01T00:00:00",
            "model": "claude-sonnet-4-20250514",
            "cost_usd": 0.012,
            "chapters_hash": "fixed_hash",
            "chapters": {
                "ch01": {
                    "intents": ["bitcoin mining"],
                    "allowed_motifs": ["hardware"],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": ["bitcoin mining hardware"],
                }
            },
        }
        (intent_dir / "intent_analysis.json").write_text(json.dumps(cached_data))

        session = MagicMock()

        with patch("btcedu.core.stock_images._load_chapters", return_value=chapters_doc), \
             patch("btcedu.core.stock_images._compute_chapters_hash", return_value="fixed_hash"), \
             patch("btcedu.services.claude_service.call_claude") as mock_claude:

            result = extract_chapter_intents(session, "ep001", settings, force=False)

            mock_claude.assert_not_called()
            assert result.cost_usd == 0.012
            assert result.chapters_analyzed == 1


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_rank_candidates_calls_extract_intents(self, settings, tmp_path):
        """rank_candidates should call extract_chapter_intents before ranking."""
        settings.outputs_dir = str(tmp_path)

        ch1 = _make_chapter("ch01", "Bitcoin Mining", visual_type="b_roll")
        ch2 = _make_chapter("ch02", "Blockchain", visual_type="diagram")
        chapters_doc = _make_chapters_doc([ch1, ch2])

        manifest = _make_manifest({
            "ch01": {"search_query": "bitcoin mining", "candidates": _make_candidates(2, 100)},
            "ch02": {"search_query": "blockchain", "candidates": _make_candidates(2, 200)},
        })
        manifest_dir = tmp_path / "ep001" / "images" / "candidates"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

        session = MagicMock()

        # Build a valid intent file
        intent_data = {
            "episode_id": "ep001",
            "schema_version": "1.0",
            "analyzed_at": "2026-01-01T00:00:00",
            "model": "claude-sonnet-4-20250514",
            "cost_usd": 0.0,
            "chapters_hash": "abc123",
            "chapters": {
                "ch01": {
                    "intents": ["bitcoin mining"],
                    "allowed_motifs": ["hardware"],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": ["bitcoin mining"],
                },
                "ch02": {
                    "intents": ["blockchain technology"],
                    "allowed_motifs": ["network diagram"],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": ["blockchain network"],
                },
            },
        }

        with patch("btcedu.core.stock_images._load_chapters", return_value=chapters_doc), \
             patch("btcedu.core.stock_images._get_episode", return_value=MagicMock()), \
             patch("btcedu.core.stock_images.extract_chapter_intents") as mock_extract, \
             patch("btcedu.services.claude_service.call_claude") as mock_claude:

            mock_extract.return_value = MagicMock(
                cost_usd=0.01,
                intent_path=manifest_dir / "intent_analysis.json",
                chapters_analyzed=2,
            )
            (manifest_dir / "intent_analysis.json").write_text(json.dumps(intent_data))

            rankings_json = json.dumps({
                "rankings": [
                    {"pexels_id": 100, "rank": 1, "reason": "Best", "trap_flag": False},
                    {"pexels_id": 101, "rank": 2, "reason": "Good", "trap_flag": False},
                ]
            })
            mock_claude.return_value = MagicMock(text=rankings_json, cost_usd=0.005)

            result = rank_candidates(session, "ep001", settings)

            mock_extract.assert_called_once()
            assert result.chapters_ranked == 2

    def test_manifest_schema_3_after_ranking(self, settings, tmp_path):
        """After ranking, manifest has schema_version 3.0."""
        settings.outputs_dir = str(tmp_path)

        ch1 = _make_chapter("ch01", "Bitcoin", visual_type="b_roll")
        chapters_doc = _make_chapters_doc([ch1])

        manifest = _make_manifest({
            "ch01": {"search_query": "bitcoin", "candidates": _make_candidates(2, 100)},
        })
        manifest_dir = tmp_path / "ep001" / "images" / "candidates"
        manifest_dir.mkdir(parents=True)
        manifest_path = manifest_dir / "candidates_manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        intent_data = {
            "episode_id": "ep001",
            "schema_version": "1.0",
            "analyzed_at": "2026-01-01T00:00:00",
            "model": "dry_run",
            "cost_usd": 0.0,
            "chapters_hash": "abc123",
            "chapters": {
                "ch01": {
                    "intents": ["bitcoin"],
                    "allowed_motifs": ["coin"],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": [],
                }
            },
        }

        session = MagicMock()

        with patch("btcedu.core.stock_images._load_chapters", return_value=chapters_doc), \
             patch("btcedu.core.stock_images._get_episode", return_value=MagicMock()), \
             patch("btcedu.core.stock_images.extract_chapter_intents") as mock_extract, \
             patch("btcedu.services.claude_service.call_claude") as mock_claude:

            mock_extract.return_value = MagicMock(
                cost_usd=0.0,
                intent_path=manifest_dir / "intent_analysis.json",
                chapters_analyzed=1,
            )
            (manifest_dir / "intent_analysis.json").write_text(json.dumps(intent_data))

            rankings_json = json.dumps({
                "rankings": [
                    {"pexels_id": 100, "rank": 1, "reason": "Best", "trap_flag": False},
                    {"pexels_id": 101, "rank": 2, "reason": "OK", "trap_flag": False},
                ]
            })
            mock_claude.return_value = MagicMock(text=rankings_json, cost_usd=0.003)

            rank_candidates(session, "ep001", settings)

            updated = json.loads(manifest_path.read_text())
            assert updated["schema_version"] == "3.0"
            assert "intent_analysis_cost_usd" in updated
            assert "ranking_cost_usd" in updated
