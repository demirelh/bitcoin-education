# Phase 3 Validation Report: Smart Stock-Image Ranking

**Date:** 2026-03-15
**Validator:** Claude Sonnet 4.6 (automated)
**Sprint output reviewed:** `phase3-smart-ranking-implement-output.md`
**Tests baseline:** 782 passing (759 prior + 23 new)

---

## 1. Verdict

**PASS**

All plan items were implemented correctly. Root-cause fixes are in place, intent extraction is functional and idempotent, post-rank validation is belt-and-suspenders, manifest schema is versioned and backward compatible, and the test suite covers the failure modes that motivated the phase.

---

## 2. Scope Check

| Plan Item | Implemented | Notes |
|-----------|-------------|-------|
| `_TR_TO_EN` expansion (≥15 terms) | ✅ | 15 entries added, "makas"→"gap divide" confirmed |
| `extract_chapter_intents()` | ✅ | Single LLM call, idempotency via `chapters_hash` |
| `intent_analysis.json` artifact | ✅ | Written under `images/candidates/`, schema 1.0 |
| Per-chapter: intents, allowed/disallowed motifs, literal_traps, search_hints | ✅ | All 5 fields present in schema and parsing |
| `_validate_and_adjust_selection()` post-rank rule layer | ✅ | Traps + alt_text substring check + dedup |
| `_derive_search_query` with `search_hints` | ✅ | Hints override keyword extraction when non-empty |
| `stock_rank.md` v2 with semantic context | ✅ | Semantic Intent block, Variety Preference, `trap_flag` output |
| `candidates_manifest.json` schema 3.0 | ✅ | `trap_flag`, `dedup_adjusted`, `intents`, cost fields |
| UI: intent tags + trap warning `⚠` | ✅ | Both rendered in stock review panel |
| Optional: pass `search_hints` at search time | ✅ deferred | Correctly deferred — circular dependency if called pre-search |
| 23 new tests | ✅ | Exactly 23, all classes match plan |

No scope gaps. The one documented deviation (search-time hints optimization deferred due to circular dependency) is valid and properly noted.

---

## 3. Correctness Review

### 3.1 Root-cause fix: makas / barbershop failure

Three independent defenses now guard against the original failure case:

1. **Translation layer** (`_TR_TO_EN`): `"makas"` → `"gap divide"` produces a Pexels query for "wealth gap" imagery rather than scissors/cutting tools. This is the earliest intervention — reduces the chance of barbershop photos entering the candidate pool at all.

2. **LLM ranking layer** (`stock_rank.md` v2): The `disallowed_motifs` block explicitly instructs the LLM to rank barbershop/scissors candidates last and set `trap_flag=true`.

3. **Python rule layer** (`_validate_and_adjust_selection`): Even if the LLM fails to flag the trap, the `alt_text` substring check against `disallowed_motifs` will catch "Glass wall of modern barbershop with reflection" and swap to rank-2. This layer is independent of the LLM.

**Assessment:** The three-layer defense is correct and robust. The key regression test (`test_alt_text_disallowed_motif_check_barbershop`) exercises exactly the original failure scenario with realistic alt_text.

### 3.2 Intent extraction

- `extract_chapter_intents()` correctly filters to `VISUAL_TYPES_NEEDING_IMAGES` only (diagram, b_roll, screen_share), matching the downstream candidate selection scope.
- Idempotency compares `chapters_hash` (SHA-256 of serialized chapter data) — cache hits skip the LLM call entirely.
- Failure path falls back to empty intents without raising, keeping the rest of the pipeline functional.
- Dry-run produces a valid `intent_analysis.json` with `model: "dry_run"` and empty arrays.
- The prompt template (`intent_extract.md`) is correctly injected as `user_message` inline in the code rather than via `PromptRegistry` — this is consistent with how `stock_rank.md` is used and is not a correctness issue.

One minor observation: `extract_chapter_intents()` builds its own system/user prompt inline (lines 819–844) rather than rendering `intent_extract.md` via `PromptRegistry`. The template file exists but is not loaded. This means `PromptRegistry` doesn't track a `PromptVersion` for intent extraction, so cost/hash provenance isn't recorded in the DB. The plan does not explicitly require DB provenance for intent extraction (it's a candidates artifact, not a `ContentArtifact`), and the template file is still useful as documentation. Not a blocking issue.

### 3.3 Post-rank validation

`_validate_and_adjust_selection()` is correct:

- **Trap check order is right**: trap flag checked first, then disallowed motif substring, both in `_is_trap()`. The check is case-insensitive.
- **Swap logic is correct**: rank-1 deselected, first clean alternative selected. If no alternative, rank-1 is kept with a warning (better a trap than no image).
- **Dedup logic is correct**: rank ≤ 3 guard on alternative prevents replacing with a much worse image. `dedup_adjusted` flag is set on the new selection.
- **No global forbidden-word list**: disallowed_motifs come from per-chapter intent data, not a global blocklist. This is the correct approach.
- **Mutation**: function mutates `candidates` list in-place, which is consistent with `_apply_rankings()` — no issue.

One subtle edge case: if `selected_so_far` contains the trap-replaced candidate's id (i.e., the post-trap replacement is also a duplicate), the dedup check runs on the _new_ selected candidate, not the original. This is correct behavior — the two checks run sequentially, so the second check sees the updated selection.

### 3.4 Search query with hints

`_derive_search_query(ch, search_hints=hints)` correctly uses hints as primary query when the list is non-empty, and appends the visual type modifier regardless. Fallback to existing keyword extraction when `search_hints` is `None` or `[]` is correct.

### 3.5 Ranking prompt (stock_rank.md v2)

The Jinja2 template correctly:
- Conditionally renders the `literal_traps` block (only when `literal_traps` is non-empty)
- Conditionally renders the `Variety Preference` block (only when `already_selected_ids` is non-empty)
- Promotes Semantic Fit to ranking criterion #1
- Requests `trap_flag` in JSON output

The inline `user_message` built in `rank_candidates()` (lines 604–651) mirrors the template structure but is used directly (template is not rendered). This duplication between `stock_rank.md` and inline code is cosmetically awkward but functionally correct — both paths produce equivalent prompts.

---

## 4. Test Review

### 4.1 Coverage

| Class | Tests | Quality |
|-------|-------|---------|
| `TestTrToEnExpansion` | 4 | Solid — specific term assertions, count guard |
| `TestDeriveSearchQuery` | 4 | Good — covers hints override, empty/None fallback, modifier still appended |
| `TestParseIntentResponse` | 4 | Good — covers valid, invalid JSON, partial, markdown fence |
| `TestValidateAndAdjustSelection` | 7 | Excellent — 3 named regression tests with realistic alt_text |
| `TestExtractChapterIntents` | 2 | Good — dry_run no LLM, idempotency cache |
| `TestIntegration` | 2 | Good — extract called by rank, schema_version 3.0 written |

### 4.2 Regression tests

The three `test_alt_text_disallowed_motif_check_*` tests are the most valuable:
- `test_alt_text_disallowed_motif_check_barbershop`: direct reproduction of the original failure case with realistic alt_text "Glass wall of modern barbershop with reflection"
- `test_alt_text_disallowed_motif_check_pressure_cooker`: "Stainless steel pressure cooker on kitchen counter" for monetary pressure chapter
- `test_alt_text_disallowed_motif_check_soap_bubbles`: "Child playing with colorful soap bubbles in garden" for economic bubble chapter

These are not overfitted to a single example — all three exercise the same mechanism (alt_text substring check) with independently constructed scenarios. **This is exactly the right test pattern for literal-trap protection.**

### 4.3 Existing test isolation

`tests/test_stock_ranking.py` correctly adds an autouse `mock_extract_intents` fixture that patches `extract_chapter_intents` to return a mock `IntentResult`. This prevents Phase 2 tests from breaking when `rank_candidates()` now calls intent extraction. The schema_version assertion update from `"2.0"` to `"3.0"` is correct.

### 4.4 Missing tests (non-blocking)

- No test exercises the `_validate_and_adjust_selection` → dedup → replacement → also-a-trap scenario (triple conflict edge case). Low probability, acceptable gap.
- No test verifies that `intent_analysis.json` is _not_ regenerated on `rank --force` (force flag behavior for intent extraction vs. ranking is not tested separately). The `force` parameter flows through correctly in the code but is an untested path.
- No test for the fallback behavior when `intent_analysis.json` exists but has a stale `chapters_hash` (force=False, hash mismatch). Code path exists and is correct but untested.

None of these are blocking — the happy path and the main failure modes are fully covered.

---

## 5. Backward Compatibility Check

| Concern | Status |
|---------|--------|
| Episodes with no `intent_analysis.json` | ✅ — `intent_map.get(ch_id, {})` returns `{}`, all fields default to empty lists |
| Manifests with `schema_version` < 3.0 | ✅ — `trap_flag` and `dedup_adjusted` only checked when present; missing fields return `False` |
| Locked/pinned candidates | ✅ — `locked` check runs before `_validate_and_adjust_selection`; locked candidates are never swapped |
| v1 pipeline episodes | ✅ — Phase 3 only touches `stock_images.py`; v1 pipeline is unaffected |
| Existing `manifest.json` (finalize output) | ✅ — no changes to finalize schema in this phase |
| `test_stock_ranking.py` Phase 2 tests | ✅ — autouse fixture prevents regression |
| `_derive_search_query` signature | ✅ — `search_hints` is optional with default `None`; all callers not passing hints continue to work |

No backward compatibility issues found.

---

## 6. Required Fixes Before Commit

**None.** The implementation is correct, tests pass, and no blocking issues were identified.

---

## 7. Nice-to-Have Improvements

1. **Register intent extraction in PromptRegistry**: `extract_chapter_intents()` builds its prompt inline and doesn't create a `PromptVersion` DB record. This means intent extraction cost isn't tracked in the LLM report. Low priority since the cost is captured in `intent_analysis.json`, but it would improve observability parity with other stages.

2. **Deduplicate template vs. inline prompt**: `stock_rank.md` and the inline `user_message` in `rank_candidates()` describe the same prompt structure. If the template is authoritative, rendering it via `PromptRegistry` would eliminate the duplication. Currently neither template (`intent_extract.md`, `stock_rank.md`) is rendered via Jinja2 at runtime — both are bypassed by inline string construction.

3. **Test force=False / stale hash path**: Add a test for `extract_chapter_intents(force=False)` when the cached file has a mismatched `chapters_hash` — ensures the LLM is re-called and the cache is refreshed. Currently untested.

4. **Log dedup_adjusted chapters**: Currently `dedup_adjusted` is set silently on the candidate dict. A `logger.info` call at manifest write time listing which chapters had dedup adjustments would help in debugging.

---

## 8. Summary

Phase 3 is a clean, well-implemented sprint. The original barbershop/literal-trap failure mode is addressed at three independent levels (translation → LLM prompt → Python rule layer), and the regression tests directly reproduce the failure scenario. The intent extraction pipeline is idempotent, failure-safe, and correctly integrated into `rank_candidates()`. Cross-chapter dedup tracking is simple and effective. The manifest schema bump to 3.0 is correct. Backward compatibility is fully preserved. The 23 new tests are non-trivial and meaningful.

**Verdict: PASS — ready to ship.**
