# Phase 3: Smart Stock-Image Ranking — Implementation Output

**Date:** 2026-03-15
**Status:** Complete
**Tests:** 782 passing (23 new), 0 failures

---

## What Was Implemented

Phase 3 adds semantic intent awareness to the stock image ranking pipeline to prevent literal-trap mismatches and cross-chapter duplicate selections.

### Core features delivered

1. **Intent extraction** (`extract_chapter_intents`): Single LLM call per episode produces `intent_analysis.json` with per-chapter intents, allowed/disallowed motifs, literal traps, and search hints.

2. **`_TR_TO_EN` expansion**: 15 new polysemous/frequently-missed Turkish→English mappings added, including the key "makas"→"gap divide" fix that caused the barbershop failure.

3. **Post-rank validation** (`_validate_and_adjust_selection`): Python rule layer that catches trap-flagged winners and cross-chapter duplicates after LLM ranking. Both LLM (via `disallowed_motifs`) and rule layer (via `alt_text` substring check) guard against literal traps — belt and suspenders.

4. **Cross-chapter dedup**: `selected_so_far` set tracks selected Pexels IDs across chapters; passed to ranking prompt as `already_selected_ids` and validated post-rank.

5. **Updated ranking prompt** (`stock_rank.md` v2): Includes semantic intent block, disallowed motifs, literal traps, variety preference, and `trap_flag` output field.

6. **`_derive_search_query` with `search_hints`**: When intent analysis provides search hints, they override keyword extraction as primary query terms.

7. **UI additions**: Intent tags rendered per chapter in stock review panel; `⚠` trap warning shown on flagged candidates.

8. **`candidates_manifest.json` schema 3.0**: Includes `intents` per chapter, `trap_flag`/`dedup_adjusted` per candidate, `intent_analysis_cost_usd`, `ranking_cost_usd`.

---

## Files Changed / Created

### New files

| File | Purpose |
|------|---------|
| `btcedu/prompts/templates/intent_extract.md` | Prompt template for episode-level intent extraction (v1) |
| `tests/test_smart_ranking.py` | 23 new Phase 3 tests |
| `docs/sprints/outputs/phase3-smart-ranking-implement-output.md` | This document |

### Modified files

| File | Changes |
|------|---------|
| `btcedu/core/stock_images.py` | +`IntentResult` dataclass; +`extract_chapter_intents()`; +`_parse_intent_response()`; +`_validate_and_adjust_selection()`; updated `rank_candidates()` to wire intents, dedup, validation; updated `_derive_search_query()` with `search_hints` param; updated `_apply_rankings()` to copy `trap_flag`; expanded `_TR_TO_EN` with 15 entries |
| `btcedu/prompts/templates/stock_rank.md` | Updated to version 2 with semantic intent block, trap awareness, variety preference, `trap_flag` output |
| `btcedu/web/static/app.js` | Intent tags rendered per chapter; trap warning `⚠` on flagged thumbnails |
| `btcedu/web/static/styles.css` | Added `.stock-intents`, `.stock-intent-tag`, `.stock-trap-warning` styles |
| `tests/test_stock_ranking.py` | Added `mock_extract_intents` autouse fixture to isolate Phase 2 tests; updated `schema_version` assertion to `"3.0"` |

---

## Test Results

```
782 passed, 33 warnings in 74s
```

**23 new tests** in `tests/test_smart_ranking.py`:

- `TestTrToEnExpansion` (4 tests): Verifies makas/baskı/köpük/all 15 new terms present
- `TestDeriveSearchQuery` (4 tests): search_hints override, fallback, visual type modifier
- `TestParseIntentResponse` (4 tests): Valid parse, invalid JSON, partial response, markdown fence strip
- `TestValidateAndAdjustSelection` (7 tests): Trap flag demotion, 3 regression tests (barbershop, pressure cooker, soap bubbles), duplicate avoidance, duplicate kept, clean selection
- `TestExtractChapterIntents` (2 tests): Dry-run no LLM call, idempotency cache
- `TestIntegration` (2 tests): `extract_chapter_intents` called by `rank_candidates`, manifest schema 3.0

**Ruff:** All checks passed.

---

## Manual Smoke Test Steps

To verify Phase 3 on a real episode:

```bash
# 1. Run stock image search (fetches candidates)
btcedu stock search --episode-id <EP_ID>

# 2. Run intent extraction + ranking
btcedu stock rank --episode-id <EP_ID> --force

# 3. Check intent_analysis.json was created
cat data/outputs/<EP_ID>/images/candidates/intent_analysis.json | jq '.chapters | keys'

# 4. Verify schema version in candidates_manifest.json
cat data/outputs/<EP_ID>/images/candidates/candidates_manifest.json | jq '.schema_version'
# Expected: "3.0"

# 5. Check trap_flag and intents fields in manifest
cat data/outputs/<EP_ID>/images/candidates/candidates_manifest.json | \
  jq '.chapters | to_entries[] | {chapter: .key, intents: .value.intents}'

# 6. Open web dashboard, go to Stock Images tab
# - Intent tags should appear below each chapter header
# - ⚠ icon should appear on any trap-flagged candidate rank badge
```

For the barbershop regression specifically: a chapter with "makas" in the title should now:
1. Use `_TR_TO_EN["makas"] = "gap divide"` → better Pexels query
2. Have `disallowed_motifs: ["scissors", "barbershop"]` in intent analysis
3. Have any barbershop candidate `trap_flag: true` from LLM ranking
4. Have `_validate_and_adjust_selection` demote it even if LLM still ranks it #1

---

## Deviations from Plan

- No deviations. All items in the plan specification were implemented as specified.
- The `_EMPTY_CHAPTER_INTENTS` constant in `_parse_intent_response` was defined as a local variable rather than a module-level constant (no functional difference).
- The `search_stock_images()` function's optional optimization (pass `search_hints` when intent file exists) was not implemented — the plan marked it as "optional optimization, only if clean to implement" and the intent extraction is called during `rank_candidates`, not `search_stock_images`. Adding it to search would require calling intent extraction before search has run, creating a circular dependency.
