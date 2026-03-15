# Pexels Stock Images — Phase 2 Implementation Output

**Date:** 2026-03-15
**Phase:** 2 of 2 — LLM Ranking, Pipeline Integration, Review Gate, Dashboard
**Status:** Complete

## Summary

Phase 2 completes the replacement of DALL-E AI image generation with Pexels stock photos in the v2 pipeline. LLM-based ranking selects the best candidate per chapter, a new `review_gate_stock` pipeline stage enables human review before finalization, and the web dashboard provides a Stock Images tab for browsing candidates, pinning selections, and approving reviews.

**Absolute goal achieved:** No OpenAI image generation calls remain in the v2 pipeline. v1 pipeline behavior is preserved unchanged.

## Changes by Component

### 1. LLM Ranking (`btcedu/core/stock_images.py`)

- Added `RankResult` dataclass
- Added `rank_candidates(session, episode_id, settings, force=False)` — one LLM call per chapter with >1 unlocked candidates
- Added `_parse_ranking_response(response_text)` — strips markdown fences, parses JSON rankings
- Added `_apply_rankings(candidates, rankings)` — applies LLM ranks, handles unknown IDs, fallback ranking for errors
- Locked/pinned chapters are skipped (unless `force=True`)
- Single-candidate chapters auto-ranked (rank=1, no LLM call)
- Dry-run mode: order-based ranking, no API calls

### 2. Prompt Template (`btcedu/prompts/templates/stock_rank.md`)

- YAML frontmatter: name=stock_rank, model=claude-sonnet-4-20250514, temperature=0.1, max_tokens=4096
- Instructs LLM to rank candidates by relevance to chapter context (narration, visual type, image prompt)
- Output: JSON `{"rankings": [{"pexels_id": ..., "rank": ..., "reason": ...}]}`

### 3. Pipeline Integration (`btcedu/core/pipeline.py`)

- `_V2_STAGES` expanded from 13 → 14 stages with `("review_gate_stock", EpisodeStatus.CHAPTERIZED)` between `imagegen` and `tts`
- `imagegen` handler: removed DALL-E branch entirely; now calls `search_stock_images()` + `rank_candidates()` only
- `review_gate_stock` handler: checks `has_approved_review("stock_images")`, calls `finalize_selections()` on approval, creates `ReviewTask` if none exists

### 4. Review System (`btcedu/core/reviewer.py`)

- `reject_review()` and `request_changes()`: stock_images stage excluded from status revert (same as render), allowing re-ranking without data loss

### 5. API Endpoints (`btcedu/web/api.py`)

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/episodes/<id>/stock/candidates` | Returns candidates manifest + review info |
| POST | `/api/episodes/<id>/stock/pin` | Pins specific candidate (sets lock + pinned_by=human) |
| POST | `/api/episodes/<id>/stock/rank` | Triggers LLM re-ranking |
| GET | `/api/episodes/<id>/stock/candidate-image` | Serves candidate image file (with path traversal guard) |

### 6. Dashboard (`btcedu/web/static/app.js`, `styles.css`)

- New "Stock Images" tab in episode detail view
- Per-chapter sections showing: query used, candidate thumbnail grid, rank badges, pin buttons
- Locked/pinned indicators with photographer attribution
- Review status display with Approve button for pending reviews
- Re-rank action button

### 7. CLI (`btcedu/cli.py`)

- Added `stock rank` command: `btcedu stock rank --episode-id EP [--force] [--dry-run]`
- Updated `stock list` output to show Rank column
- Updated `stock auto-select` docstring to clarify dev/test-only purpose

## Test Results

**38 new tests, all passing:**

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `tests/test_stock_ranking.py` | 20 | rank_candidates, _parse_ranking_response, _apply_rankings, dry-run, fallback, cost, pinned_by |
| `tests/test_stock_review_gate.py` | 8 | v2 stage list, gate creation, pending/approved flow, rejection behavior, finalization |
| `tests/test_stock_api.py` | 10 | GET/POST endpoints, pin/rank/serve, path traversal guard, 404 handling |

**Full suite:** 705 passed, 5 failed (all pre-existing — env contamination in pipeline tests + pydantic in chapterizer)

**Ruff lint:** 0 errors in all modified files

## Pipeline Flow (v2, updated)

```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review_gate_1] →
TRANSLATED → ADAPTED → [review_gate_2] → CHAPTERIZED →
IMAGES_GENERATED → [review_gate_stock] → TTS_DONE → RENDERED →
[review_gate_3] → APPROVED → PUBLISHED
```

The `imagegen` stage now exclusively uses Pexels stock photo search + LLM ranking. The `review_gate_stock` stage pauses the pipeline for human review of stock image selections before TTS generation proceeds.

## candidates_manifest.json Schema (v2.0)

```json
{
  "schema_version": "2.0",
  "episode_id": "...",
  "chapters": {
    "ch_01": {
      "query": "bitcoin mining hardware",
      "candidates": [
        {
          "pexels_id": 12345,
          "photographer": "Jane Doe",
          "width": 1920,
          "height": 1080,
          "local_path": "candidates/ch_01_12345.jpg",
          "rank": 1,
          "rank_reason": "Best match for mining hardware context",
          "pinned_by": null
        }
      ],
      "selected": "candidates/ch_01_12345.jpg",
      "locked": false
    }
  }
}
```

New fields per candidate: `rank` (int), `rank_reason` (str), `pinned_by` (null | "human" | "llm")

## Files Modified

| File | Change |
|------|--------|
| `btcedu/core/stock_images.py` | +RankResult, +rank_candidates, +_parse_ranking_response, +_apply_rankings |
| `btcedu/core/pipeline.py` | +review_gate_stock stage, removed DALL-E branch from imagegen |
| `btcedu/core/reviewer.py` | stock_images exemption in reject/request_changes |
| `btcedu/web/api.py` | +4 stock endpoints |
| `btcedu/web/static/app.js` | +Stock Images tab, +pin/rank/approve functions |
| `btcedu/web/static/styles.css` | +stock panel styles |
| `btcedu/cli.py` | +stock rank command, updated stock list |
| `btcedu/prompts/templates/stock_rank.md` | New LLM ranking prompt template |
| `tests/test_stock_ranking.py` | New — 20 tests |
| `tests/test_stock_review_gate.py` | New — 8 tests |
| `tests/test_stock_api.py` | New — 10 tests |
| `tests/test_pipeline.py` | Updated stage count test (13→14) |
