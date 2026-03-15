# Phase 3: Smart Stock-Image Ranking — Implementation Plan

**Date:** 2026-03-15
**Depends on:** Pexels Stock Images Phase 2 (complete)
**Goal:** Prevent literal-trap mismatches and improve semantic relevance of stock image selections.

---

## 1. Problem Statement

The current stock image system searches Pexels using keyword queries derived from Turkish chapter metadata, then ranks candidates with an LLM. Both steps are vulnerable to **literal traps** — cases where a word has multiple meanings and the wrong one dominates.

### Real failure: "makas" (gap vs. scissors)

Chapter: *"Giriş: Zengin ve Fakir Arasındaki Makas"* (The Gap Between Rich and Poor)

- **Search query**: `"toplumun zengin fakir kesimlerini temsil eden makas photo"` — "makas" (gap/scissors) was not in `_TR_TO_EN`, so it passed through untranslated to Pexels.
- **Pexels returned**: A barbershop (because "makas" = scissors in Turkish, and Pexels matched literally).
- **LLM ranked it #1**: The ranking prompt only sees `alt_text` ("Glass wall of modern barbershop…") and rationalized it as "symbolizing contrast between wealth and poverty."

### Additional observed issues

| Problem | Evidence |
|---------|----------|
| **Cross-chapter duplicates** | Same Pexels photo selected for ch02+ch09 (crypto coins) and ch10+ch11 (financial report chart) |
| **No semantic intent in ranking** | The LLM has no explicit concept of *what the chapter is about* — only title + visual description + alt text |
| **No trap detection** | No mechanism to flag that "makas"→barbershop or "pressure"→pressure cooker is a wrong-domain match |
| **Turkish leaking into queries** | Words not in `_TR_TO_EN` pass through as Turkish, producing irrelevant Pexels results |

---

## 2. Current State

### What exists

| Component | Location | Relevant to Phase 3 |
|-----------|----------|---------------------|
| `_derive_search_query()` | `stock_images.py:850` | Keyword extraction + `_TR_TO_EN` translation → Pexels query |
| `_TR_TO_EN` dict | `stock_images.py:26` | 50+ Turkish→English domain mappings |
| `rank_candidates()` | `stock_images.py:346` | Per-chapter LLM ranking call |
| `stock_rank.md` template | `prompts/templates/` | Ranking prompt with 4 criteria: Relevance, Composition, Professionalism, Text Overlay |
| `_apply_rankings()` | `stock_images.py:532` | Applies LLM ranking to candidates |
| Human pinning | `select_stock_image()` | Final override via dashboard |
| `review_gate_stock` | `pipeline.py` | Human approval gate |

### What's missing

1. **No intent extraction**: The system never articulates *what the chapter is about* before searching or ranking.
2. **No disallowed-motif awareness**: Nothing tells the LLM "do NOT choose a barbershop for a chapter about inequality."
3. **No literal-trap detection**: No guard against polysemous words pulling in wrong-domain images.
4. **No cross-chapter dedup**: Each chapter is ranked independently; the same Pexels photo can be selected for multiple chapters.
5. **`_TR_TO_EN` gaps**: "makas", "baskı" (pressure), "köpük" (bubble), etc. are missing.

---

## 3. Design

### 3.1 Architecture overview

```
chapters.json
     │
     ▼
[Intent Extraction]  ← NEW: single LLM call for entire episode
     │
     ▼
intent_analysis.json  ← NEW artifact
     │
     ├──────────────────────────┐
     ▼                          ▼
[Search Query Enrichment]   [Ranking Prompt Enrichment]
  (negative keywords,         (intents, disallowed motifs,
   improved translation)       literal traps, dedup hints)
     │                          │
     ▼                          ▼
Pexels API search           LLM ranking (per chapter)
     │                          │
     ▼                          ▼
candidates_manifest.json    candidates_manifest.json (ranked)
                                │
                                ▼
                          [Post-rank validation]  ← NEW
                                │
                                ▼
                          Human review (existing)
```

### 3.2 Intent extraction — single LLM call per episode

A new **`extract_chapter_intents()`** function makes one LLM call for the entire episode, producing structured intent metadata per chapter. This is done once before ranking and cached as `intent_analysis.json`.

**Why one call, not per-chapter?**
- Cross-chapter context lets the LLM assign differentiated intents (ch02 is about monetary policy, ch03 is about gold standard → different motifs).
- One call is cheaper (~$0.02) than 12 separate calls.
- The LLM can detect duplicate-prone chapters and suggest differentiation.

**Output schema per chapter:**

```json
{
  "ch01": {
    "intents": ["wealth inequality", "class divide", "economic disparity"],
    "allowed_motifs": ["city skyline contrast", "luxury vs poverty", "scales of justice", "wealth distribution chart"],
    "disallowed_motifs": ["scissors", "barbershop", "hair cutting", "sewing"],
    "literal_traps": [
      {"word": "makas", "intended": "gap/divide", "trap": "scissors/cutting tools"}
    ],
    "search_hints": ["wealth gap", "rich poor divide", "economic inequality"]
  }
}
```

**Fields:**
- `intents`: 1–3 semantic intents describing the chapter's core meaning.
- `allowed_motifs`: 3–6 visual motifs that would fit the chapter's meaning.
- `disallowed_motifs`: 2–4 visual motifs that a naive keyword search might return but would be wrong.
- `literal_traps`: Explicit polysemous words with intended vs. trap meanings. Helps both search and ranking.
- `search_hints`: 2–4 English search terms the LLM recommends for Pexels (supplements `_derive_search_query()`).

### 3.3 Search query enrichment

`_derive_search_query()` is updated to:

1. **Use `search_hints`** from intent analysis as primary query terms (if available), falling back to existing keyword extraction.
2. **Expand `_TR_TO_EN`** with missing entries for known-problematic words.
3. **Drop untranslated Turkish words** more aggressively — currently words that are ASCII pass through, but non-ASCII untranslated words are dropped. The problem is "makas" is ASCII-compatible in the query context.

**Assumption:** We do NOT add Pexels negative-keyword support (Pexels API doesn't support it). Instead, the LLM-generated `search_hints` are the primary lever for better search queries.

### 3.4 Ranking prompt overhaul

The `stock_rank.md` template is upgraded to version 2 with:

1. **Intent context block**: Injects `intents`, `allowed_motifs`, `disallowed_motifs`, and `literal_traps` into the prompt.
2. **New ranking criterion**: "Semantic fit" is promoted to criterion #1, above relevance. It asks: "Does this image match the *intended meaning* of the chapter, not just keyword overlap?"
3. **Literal-trap warning**: Explicit instruction to check each candidate against `disallowed_motifs` and `literal_traps`.
4. **Cross-chapter dedup hint**: The prompt receives a list of Pexels IDs already selected in previous chapters, with instruction to prefer variety.
5. **Expanded output**: Each ranking entry gets a `"trap_flag"` boolean indicating whether the candidate triggered a literal-trap concern.

**Updated prompt structure:**

```markdown
# System
You are an editorial assistant selecting the best stock photo for a YouTube video chapter.
...

# Chapter Context
- **Title**: {{ chapter_title }}
- **Visual type**: {{ visual_type }}
- **Visual description**: {{ visual_description }}
- **Narration excerpt**: {{ narration_excerpt }}
- **Search query used**: {{ search_query }}

# Semantic Intent (IMPORTANT — use this to judge relevance)
- **Intents**: {{ intents | join(", ") }}
- **Allowed motifs**: {{ allowed_motifs | join(", ") }}
- **Disallowed motifs (DO NOT select)**: {{ disallowed_motifs | join(", ") }}
{% if literal_traps %}
- **Literal traps to avoid**:
{% for trap in literal_traps %}
  - "{{ trap.word }}" means "{{ trap.intended }}" here, NOT "{{ trap.trap }}"
{% endfor %}
{% endif %}

{% if already_selected_ids %}
# Variety Preference
These Pexels IDs are already selected for other chapters. Prefer different images unless this candidate is clearly the best fit:
{{ already_selected_ids | join(", ") }}
{% endif %}

# Candidates
...

# Task
Rank ALL candidates. For each candidate:
1. Check if it matches any DISALLOWED motif or literal trap → if yes, rank it last and set trap_flag=true
2. Judge semantic fit with the chapter's INTENTS, not just keyword overlap
3. Consider composition, professionalism, and text overlay compatibility

# Output Format (JSON)
{
  "rankings": [
    {"pexels_id": 12345, "rank": 1, "reason": "...", "trap_flag": false},
    ...
  ]
}
```

### 3.5 Post-rank validation pass

After the LLM ranks candidates, a lightweight **validation pass** in Python checks for obvious problems before writing the manifest:

1. **Trap-flagged winner**: If the rank-1 candidate has `trap_flag: true`, promote rank-2 to the selection. Log a warning.
2. **Cross-chapter duplicate**: If the rank-1 candidate's Pexels ID is already selected in another chapter, check if rank-2 is within reasonable quality (rank ≤ 3). If so, prefer rank-2 to avoid duplication. If all top candidates are duplicates, keep rank-1 (better a duplicate than a bad image).
3. **Alt-text keyword check**: A simple string match against `disallowed_motifs` on the selected candidate's `alt_text`. If any disallowed motif substring appears, downrank and log.

**Location:** New function `_validate_and_adjust_selection()` in `stock_images.py`, called after `_apply_rankings()` inside `rank_candidates()`.

**Why hybrid (LLM + rules)?**
- The LLM catches nuanced traps ("barbershop symbolizes class divide" — rationalization).
- The rule layer catches cases where the LLM rationalizes anyway. Belt and suspenders.
- Rules are free (no API cost) and deterministic.

### 3.6 Cross-chapter duplicate reduction

**Strategy:** Process chapters sequentially during ranking. Maintain a `selected_so_far` set of Pexels IDs. Pass this set to both the ranking prompt (as `already_selected_ids`) and the post-rank validation.

**Implementation:**
1. In `rank_candidates()`, iterate chapters in order.
2. After ranking each chapter, add the selected Pexels ID to `selected_so_far`.
3. Pass `selected_so_far` as a template variable to the ranking prompt for subsequent chapters.
4. In `_validate_and_adjust_selection()`, prefer non-duplicate rank-2 over duplicate rank-1 when quality difference is small.

**Edge case:** If the same image genuinely is the best for two chapters (e.g., both about Bitcoin → Bitcoin coins photo), allow it. The LLM is told to *prefer* variety, not enforce uniqueness.

### 3.7 UI enhancement (minimal)

Add two small additions to the stock image review panel:

1. **Intent summary line** per chapter: Below the chapter header, show the `intents` as small tags (e.g., `[wealth inequality] [class divide]`). This helps the human reviewer understand what the system thinks the chapter is about and judge selections accordingly.
2. **Trap flag indicator**: If a candidate has `trap_flag: true`, show a small warning icon (⚠) next to its rank badge. Tooltip: "Possible literal-trap mismatch — check carefully."

**No layout redesign.** These are additive to the existing stock review panel.

---

## 4. Data Contract Changes

### 4.1 New artifact: `intent_analysis.json`

**Path:** `data/outputs/{ep_id}/images/candidates/intent_analysis.json`

```json
{
  "episode_id": "SJFLLZxlWqk",
  "schema_version": "1.0",
  "analyzed_at": "2026-03-15T12:00:00+00:00",
  "model": "claude-sonnet-4-20250514",
  "cost_usd": 0.018,
  "chapters_hash": "9b45...",
  "chapters": {
    "ch01": {
      "intents": ["wealth inequality", "class divide", "economic disparity"],
      "allowed_motifs": ["city skyline contrast", "luxury vs poverty", "scales of justice"],
      "disallowed_motifs": ["scissors", "barbershop", "hair cutting", "sewing"],
      "literal_traps": [
        {"word": "makas", "intended": "gap/divide", "trap": "scissors/cutting tools"}
      ],
      "search_hints": ["wealth gap", "rich poor divide", "economic inequality"]
    }
  }
}
```

### 4.2 Updated `candidates_manifest.json` (schema 3.0)

Per-candidate additions:
- `"trap_flag": bool` — set by ranking LLM or post-rank validation
- `"dedup_adjusted": bool` — true if selection was changed by duplicate avoidance

Per-chapter additions:
- `"intents": [str]` — copied from intent_analysis.json for display convenience

Top-level additions:
- `"intent_analysis_hash": str` — hash of intent_analysis.json used during ranking
- `"schema_version": "3.0"`

### 4.3 Stock candidates API response

The existing `GET /api/episodes/{id}/stock/candidates` response gains:
- `intents` array per chapter (from manifest)
- `trap_flag` per candidate (from manifest)

No new endpoints needed.

---

## 5. Files to Modify / Create

### New files

| File | Purpose |
|------|---------|
| `btcedu/prompts/templates/intent_extract.md` | Prompt template for episode-level intent extraction |
| `btcedu/prompts/templates/stock_rank.md` (v2) | Updated ranking prompt with intent context |
| `data/outputs/{ep_id}/images/candidates/intent_analysis.json` | Per-episode intent artifact (runtime) |
| `tests/test_smart_ranking.py` | Phase 3 tests |

### Modified files

| File | Changes |
|------|---------|
| `btcedu/core/stock_images.py` | Add `extract_chapter_intents()`, `_validate_and_adjust_selection()`, `_check_alt_text_traps()`; update `rank_candidates()` to use intents + dedup + validation; update `_derive_search_query()` to use `search_hints`; expand `_TR_TO_EN` |
| `btcedu/web/static/app.js` | Add intent tags and trap-flag indicator to stock review panel |
| `btcedu/web/static/styles.css` | Add `.stock-intent-tag` and `.stock-trap-warning` styles (~15 lines) |

### Unchanged (preserved)

| File | Why |
|------|-----|
| `btcedu/services/pexels_service.py` | Search API interface unchanged |
| `btcedu/core/pipeline.py` | `imagegen` stage handler calls search + rank as before |
| `btcedu/web/api.py` | Existing stock endpoints unchanged; intent data flows through manifest |
| `btcedu/core/reviewer.py` | Review gate logic unchanged |

---

## 6. Detailed Function Changes

### 6.1 `extract_chapter_intents(session, episode_id, settings, force=False)`

**New function** in `stock_images.py`.

```
Input:  episode_id, chapters.json
Output: intent_analysis.json
Cost:   ~$0.01–0.03 (single LLM call)
```

**Logic:**
1. Load `chapters.json`.
2. Compute `chapters_hash`. Check idempotency: if `intent_analysis.json` exists with matching hash and not stale → return cached.
3. Build prompt from `intent_extract.md` template with all chapters' title + visual description + narration excerpt (first 200 chars each).
4. Call `call_claude(system_prompt, user_message, settings, json_mode=True)`.
5. Parse JSON response. Validate schema (must have all required fields per chapter).
6. Write `intent_analysis.json`.
7. Return `IntentResult(episode_id, chapters_analyzed, cost_usd)`.

**Dry-run behavior:** Return empty intents (no LLM call). Ranking falls back to existing behavior.

### 6.2 `rank_candidates()` — updated

**Changes:**
1. **Before the per-chapter loop:** Call `extract_chapter_intents()` to ensure `intent_analysis.json` exists. Load it.
2. **Per-chapter iteration order:** Chapters processed in order (ch01, ch02, …).
3. **Per-chapter prompt:** Pass `intents`, `allowed_motifs`, `disallowed_motifs`, `literal_traps`, and `already_selected_ids` to the template.
4. **After `_apply_rankings()`:** Call `_validate_and_adjust_selection(candidates, intents_data, selected_so_far)`.
5. **After selection:** Add selected Pexels ID to `selected_so_far`.
6. **Write manifest:** Bump to schema 3.0; include `intents` and `trap_flag`/`dedup_adjusted` fields.

### 6.3 `_validate_and_adjust_selection(candidates, intent_data, selected_so_far)`

**New function** in `stock_images.py`.

```
Input:  ranked candidate list, intent data for this chapter, set of already-selected IDs
Output: mutates candidates in-place (may swap selection)
```

**Logic:**
1. Find current selection (rank-1, `selected=True`).
2. **Trap check:** If `trap_flag=True` on selection, or if any `disallowed_motifs` substring found in `alt_text` (case-insensitive):
   - Find next-best candidate without trap issues.
   - If found: swap selection, set `dedup_adjusted=False` on the swapped-in candidate.
   - Log warning.
3. **Duplicate check:** If selection's Pexels ID is in `selected_so_far`:
   - Find next-best candidate (rank ≤ 3) not in `selected_so_far` and not trap-flagged.
   - If found: swap selection, set `dedup_adjusted=True`.
   - If not found: keep duplicate (better than bad image).
4. Return.

### 6.4 `_derive_search_query()` — updated

**Changes:**
1. Accept optional `search_hints: list[str]` parameter.
2. If `search_hints` provided and non-empty: use them as the primary terms, append visual-type modifiers and "finance", skip keyword extraction.
3. If not provided: fall back to existing keyword extraction logic.
4. Expand `_TR_TO_EN` with ~15 new entries (see §6.5).

### 6.5 `_TR_TO_EN` expansions

```python
# Phase 3 additions — polysemous / frequently-missed terms
"makas": "gap divide",          # NOT scissors
"baskı": "pressure",            # NOT printing press
"köpük": "bubble",              # NOT soap foam
"balon": "bubble",              # NOT party balloon
"boşluk": "gap void",           # NOT empty space
"çukur": "pit downturn",        # NOT physical hole
"dalga": "wave cycle",          # NOT ocean wave
"patlama": "boom explosion",    # economic boom, NOT literal explosion
"daralma": "contraction",       # economic, NOT physical
"aşınma": "erosion decline",    # NOT physical erosion
"tavan": "ceiling cap",         # price ceiling, NOT room ceiling
"taban": "floor base",          # price floor, NOT room floor
"kaldıraç": "leverage",        # financial, NOT physical lever
"çıpa": "anchor peg",          # currency peg, NOT boat anchor
"sürdürülebilir": "sustainable",
```

---

## 7. Prompt Templates

### 7.1 `intent_extract.md` (new)

```yaml
---
name: intent_extract
version: 1
model: claude-sonnet-4-20250514
temperature: 0.1
max_tokens: 4096
description: Extract semantic intents per chapter for stock image selection
author: btcedu
---
```

**System prompt:**
> You are a visual editor for an educational YouTube channel about Bitcoin and cryptocurrency, targeting a Turkish audience. Your task is to analyze video chapters and extract semantic intents for stock photo selection.

**User prompt structure:**
> For each chapter below, extract:
> 1. `intents` (1–3): The core concepts/themes the chapter communicates.
> 2. `allowed_motifs` (3–6): Visual motifs that would appropriately illustrate these intents in a stock photo.
> 3. `disallowed_motifs` (2–4): Visual motifs that a naive keyword search might return but would be WRONG for this chapter. Think about polysemous words (words with multiple meanings).
> 4. `literal_traps`: Any words in the chapter title or description that have a non-obvious alternate meaning which could mislead image search. Format: {"word": "...", "intended": "...", "trap": "..."}.
> 5. `search_hints` (2–4): English search terms you would use to find the RIGHT stock photo on Pexels.
>
> ## Chapters
> {% for ch in chapters %}
> ### {{ ch.chapter_id }}: {{ ch.title }}
> - Visual type: {{ ch.visual_type }}
> - Visual description: {{ ch.visual_description }}
> - Narration excerpt: {{ ch.narration_excerpt }}
> {% endfor %}
>
> Return ONLY valid JSON: {"chapters": {"ch01": {...}, "ch02": {...}, ...}}

### 7.2 `stock_rank.md` v2 (updated)

Key additions to the existing template (see §3.4 for full structure):

- Semantic Intent block with intents, allowed/disallowed motifs, literal traps
- Variety Preference block with already-selected IDs
- Updated ranking criteria: Semantic Fit > Relevance > Composition > Professionalism > Text Overlay
- Updated output format: adds `trap_flag` per ranking entry
- Version bumped to 2 in frontmatter

---

## 8. Test Plan

### 8.1 Unit tests: Intent extraction

| Test | What it verifies |
|------|-----------------|
| `test_extract_intents_returns_all_chapters` | All chapters from chapters.json appear in output |
| `test_intent_schema_per_chapter` | Each chapter has `intents`, `allowed_motifs`, `disallowed_motifs`, `literal_traps`, `search_hints` |
| `test_intents_not_empty` | Every chapter has at least 1 intent and 1 allowed motif |
| `test_intent_idempotency` | Second call with same chapters_hash returns cached result |
| `test_intent_dry_run` | Dry-run returns empty intents without LLM call |

### 8.2 Regression tests: Literal traps

| Test | Setup | Expected |
|------|-------|----------|
| `test_makas_barbershop_trap` | ch01 about "Zengin ve Fakir Arasındaki Makas"; candidates include a barbershop image (alt_text contains "barbershop"). Intent analysis produces `disallowed_motifs: ["scissors", "barbershop", ...]`. | Post-rank validation detects "barbershop" in alt_text → barbershop candidate NOT selected; a different candidate wins. |
| `test_pressure_cooker_trap` | Chapter about "monetary pressure" ("parasal baskı"); one candidate alt_text contains "pressure cooker". Intent analysis produces `disallowed_motifs: ["pressure cooker", "printing press"]`. | "pressure cooker" candidate downranked; NOT selected. |
| `test_bubble_soap_trap` | Chapter about "economic bubble" ("ekonomik balon"); one candidate alt_text contains "soap bubbles" or "party balloons". | Soap/balloon candidate downranked; NOT selected. |

**Implementation approach for regression tests:** These are unit tests that mock the LLM calls. The mock intent extraction returns known intents/traps. The mock ranking returns a ranking with the trap candidate as rank-1 with `trap_flag: true`. The test verifies that `_validate_and_adjust_selection()` swaps the selection.

### 8.3 Unit tests: Validation pass

| Test | What it verifies |
|------|-----------------|
| `test_trap_flagged_winner_demoted` | Rank-1 with `trap_flag=True` replaced by rank-2 |
| `test_alt_text_disallowed_motif_check` | Rank-1 alt_text contains disallowed motif → swapped |
| `test_duplicate_avoided_when_alternative_exists` | Rank-1 already in `selected_so_far` + rank-2 not → rank-2 promoted |
| `test_duplicate_kept_when_no_alternative` | All candidates in `selected_so_far` → rank-1 kept (no crash) |
| `test_no_swap_when_clean` | Rank-1 has no trap flag, no disallowed motif, no duplicate → selection unchanged |

### 8.4 Unit tests: Search query enrichment

| Test | What it verifies |
|------|-----------------|
| `test_search_hints_override_keyword_extraction` | When `search_hints` provided, query uses those instead of `_derive_search_query()` keyword logic |
| `test_tr_to_en_makas_translated` | "makas" now translates to "gap divide" |
| `test_tr_to_en_baski_translated` | "baskı" now translates to "pressure" |
| `test_search_hints_fallback` | Empty search_hints → falls back to existing behavior |

### 8.5 Unit tests: Cross-chapter dedup

| Test | What it verifies |
|------|-----------------|
| `test_dedup_prefers_rank2_over_duplicate` | Ch02 selects image X; ch09 has X as rank-1 but Y as rank-2 → ch09 selects Y |
| `test_dedup_passes_selected_ids_to_prompt` | Template receives `already_selected_ids` with IDs from prior chapters |

### 8.6 Integration tests

| Test | What it verifies |
|------|-----------------|
| `test_rank_candidates_calls_extract_intents` | `rank_candidates()` calls `extract_chapter_intents()` before ranking |
| `test_manifest_schema_3_after_ranking` | After ranking, manifest has schema_version "3.0", intents, trap_flag |
| `test_stock_candidates_api_includes_intents` | `GET /stock/candidates` response includes `intents` per chapter |

**Total: ~20 tests** (5 intent + 3 regression + 5 validation + 4 search + 2 dedup + 3 integration)

---

## 9. Implementation Order

1. **Expand `_TR_TO_EN`** — immediate, zero-risk improvement.
2. **Create `intent_extract.md` template** — prompt authoring.
3. **Implement `extract_chapter_intents()`** — core new function.
4. **Write intent extraction tests** (mock LLM).
5. **Update `stock_rank.md` to v2** — prompt enrichment.
6. **Implement `_validate_and_adjust_selection()`** — post-rank guard.
7. **Write validation + regression tests** (mock LLM + mock intents).
8. **Update `rank_candidates()`** — wire intent extraction, dedup tracking, validation.
9. **Update `_derive_search_query()`** — accept `search_hints`.
10. **Write search + dedup tests**.
11. **CSS + JS** — intent tags + trap warning in stock review panel.
12. **Integration tests**.
13. **Manual smoke test** with real episode.
14. **Write output doc**.

Estimated scope: ~200 lines `stock_images.py`, ~40 lines prompt templates, ~30 lines JS/CSS, ~400 lines tests.

---

## 10. Definition of Done

1. `extract_chapter_intents()` produces `intent_analysis.json` with per-chapter intents, motifs, traps, and search hints.
2. `rank_candidates()` uses intents to inform LLM ranking and passes dedup hints.
3. `_validate_and_adjust_selection()` catches trap-flagged winners and cross-chapter duplicates.
4. `_derive_search_query()` uses LLM-suggested `search_hints` when available.
5. `_TR_TO_EN` expanded with ~15 polysemous/missing terms.
6. The barbershop-for-inequality failure case is caught by both LLM (via `disallowed_motifs`) and rule layer (via alt_text check).
7. Cross-chapter duplicate selections reduced (ch02/ch09, ch10/ch11 cases).
8. Stock review UI shows intent tags and trap-flag warnings.
9. `candidates_manifest.json` schema 3.0 with intents, trap_flag, dedup_adjusted.
10. All tests pass (~20 new + existing ~759 unbroken).
11. Ruff lint clean.
12. Manual smoke test: re-rank the real episode and verify barbershop is no longer selected.

---

## 11. Non-Goals

- **No Pexels API changes.** Pexels doesn't support negative keywords or semantic search. We work around this with better query terms.
- **No stock video/B-roll search.** Phase 3 is images only.
- **No dashboard redesign.** Two small additive UI elements (intent tags, trap warning).
- **No new DB tables or migrations.** All state is in JSON artifacts.
- **No DALL-E fallback.** DALL-E was removed in Phase 2. If no good stock match exists, human reviewer pins the best available or requests re-search.
- **No automatic re-search.** If intent analysis reveals bad queries, the human can trigger re-search from the dashboard. Phase 3 does not auto-loop search→rank→validate→re-search.
- **No embedding-based similarity.** The validation uses substring matching on alt_text, not vector similarity. This is sufficient for the literal-trap pattern and avoids adding an embedding dependency.
- **No changes to `review_gate_stock` or pipeline flow.** The gate still pauses for human approval. Phase 3 improves what the human sees, not the approval process.

---

## 12. Cost Impact

| Component | Cost per episode | Notes |
|-----------|-----------------|-------|
| Intent extraction | ~$0.01–0.03 | Single LLM call, all chapters |
| Ranking (existing) | ~$0.01–0.03 | Per-chapter LLM calls (unchanged) |
| Ranking (with intent context) | ~$0.02–0.04 | Slightly longer prompts due to intent block |
| **Total delta** | ~$0.02–0.05 more | Intent extraction is new; ranking cost increases slightly |

Total imagegen+ranking cost: ~$0.04–0.08 per episode (up from ~$0.01–0.03). Still negligible compared to TTS (~$1–2) and well within `max_episode_cost_usd` ($10).

---

## 13. Open Questions (resolved with assumptions)

| Question | Assumption | Rationale |
|----------|-----------|-----------|
| Should intent extraction be a separate CLI command? | No, called automatically within `rank_candidates()` | Keeps workflow simple; idempotency prevents redundant calls |
| Should we re-search with better queries automatically? | No | Re-search is expensive (API rate limits) and the existing candidates may still contain a good match — just ranked wrong. Fixing ranking is higher leverage. |
| What if the LLM returns bad intent data? | Fall back gracefully | If `intent_analysis.json` fails to parse or is missing fields, ranking proceeds without intent context (existing behavior). Logged as warning. |
| Should `trap_flag` block selection entirely? | No, soft downrank | Hard blocking could leave a chapter with no selection. Soft swap to rank-2 is safer. |
| Template version: bump stock_rank to v2 or create new template? | Bump to v2 | Same template file, version 2 in frontmatter. Prompt registry tracks versions via content hash. |
