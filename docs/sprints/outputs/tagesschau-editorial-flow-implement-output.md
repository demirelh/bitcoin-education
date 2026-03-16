# Tagesschau Editorial Transformation & Review Model — Phase 3 Implementation Output

**Date:** 2026-03-16
**Status:** Complete
**Tests:** 944 passing (34 new in `tests/test_translation_review.py`; 0 regressions)

---

## What Was Implemented

### New Files

1. **`btcedu/core/translation_diff.py`**
   - `compute_translation_diff(stories_translated_path)` — generates bilingual story-level diff
   - Each story becomes a reviewable item with `item_id = "trans-s01"` pattern
   - Flags per-story word ratio anomalies: <0.5 = summarization warning, >1.5 = hallucination warning
   - Compression ratio = total_words_tr / total_words_de (defaults to 1.0 when DE=0)
   - Writes `diff_type: "translation"` for downstream routing in reviewer

2. **`tests/test_translation_review.py`** — 34 new tests covering:
   - `compute_translation_diff` structure, summary, warnings (low/high/normal ratio, zero-word guard)
   - `_get_stages` confirms tagesschau has `review_gate_translate` (not `review_gate_2`, not `adapt`)
   - `_run_stage` for `review_gate_translate`: creates task, skips when approved, pending when already pending, rejects v1 episodes
   - `_assemble_translation_review`: EDITED replaces text_tr, REJECTED/UNCHANGED prepends marker, PENDING/ACCEPTED keep text_tr
   - `apply_item_decisions` for stage="translate": writes sidecar JSON
   - Chapterizer uses reviewed sidecar; falls back to stories_translated.json
   - Revert: TRANSLATED → SEGMENTED on reject/request_changes; CORRECTED → TRANSCRIBED regression check
   - `get_review_detail`: tagesschau has 6-item checklist, bitcoin_podcast has no checklist
   - Bilingual review mode in `get_review_detail` for translate stage
   - `_REVIEW_GATE_LABELS` has "translate" entry
   - `_load_item_texts_from_diff` for translation diffs

### Modified Files

3. **`btcedu/core/pipeline.py`**
   - `_get_stages()`: when `adapt.skip=True`, replaces `review_gate_2` with `review_gate_translate` (status=TRANSLATED), removes `adapt`, adjusts chapterize to accept TRANSLATED
   - `_run_stage()`: added `"review_gate_translate"` to `_V2_ONLY_STAGES`, added handler that checks approved/pending/creates review task with bilingual diff

4. **`btcedu/core/reviewer.py`**
   - `_revert_episode()`: added `EpisodeStatus.TRANSLATED → EpisodeStatus.SEGMENTED` mapping
   - `apply_item_decisions()`: added `stage="translate"` branch — finds `stories_translated.json` in artifact_paths, calls `_assemble_translation_review`, writes sidecar to `{outputs_dir}/{ep_id}/review/stories_translated.reviewed.json`
   - `_assemble_translation_review()`: new function — EDITED replaces text_tr, REJECTED/UNCHANGED prepends Turkish marker, ACCEPTED/PENDING keeps text_tr
   - `_load_item_texts_from_diff()`: handles `diff_type="translation"` — extracts (text_de, text_tr, category) from stories list
   - `get_review_detail()`: added `review_checklist` for `content_profile="tagesschau_tr"` episodes, `review_mode="bilingual"` + stories/compression_ratio/translation_warnings for translate stage
   - `_NEWS_REVIEW_CHECKLIST`: 6-item constant (factual_accuracy, political_neutrality, attribution_present, proper_nouns_correct, no_hallucination, register_correct)
   - `get_review_detail()`: extended item_decisions_map loading to include stage="translate"

5. **`btcedu/core/chapterizer.py`**
   - Story mode now checks for `review/stories_translated.reviewed.json` sidecar before falling back to `stories_translated.json`

6. **`btcedu/web/api.py`**
   - `_REVIEW_GATE_LABELS`: added `"translate": ("review_gate_translate", "Translation Review")`
   - `_STAGE_LABELS`: added `"review_gate_translate": "Review Translate"`

7. **`btcedu/prompts/templates/tagesschau_tr/translate.md`**
   - Added `## BEI NACHARBEIT` section before `# Input` — instructs Claude to focus on reviewer feedback, correct only flagged passages, preserve unflagged content

8. **`docs/runbooks/news-editorial-policy.md`**
   - Added `## Translation Review Gate (review_gate_translate)` section with: 6-item checklist table, approve/request changes/reject criteria, per-story review workflow, sidecar workflow explanation, reversion behavior

### Test Infrastructure Fixes

9. **`tests/test_web_progress.py`** — updated `expected_keys` to include `"review_gate_translate"`
10. **`tests/test_web_review_ux.py`** — updated label count from 4 to 5, added `"translate"` assertion

---

## Pipeline Flow After Phase 3

**tagesschau_tr (new):**
```
CORRECTED → [review_gate_1] → SEGMENTED → TRANSLATED →
[review_gate_translate] → CHAPTERIZED → IMAGES_GENERATED → TTS_DONE →
RENDERED → [review_gate_3] → APPROVED → PUBLISHED
```

**bitcoin_podcast (unchanged):**
```
CORRECTED → [review_gate_1] → TRANSLATED → ADAPTED →
[review_gate_2] → CHAPTERIZED → ...
```

---

## Key Design Decisions

1. **review_gate_translate replaces review_gate_2** for news profiles — the gate is repurposed rather than added, so the pipeline has the same number of review checkpoints for both profiles.

2. **No auto-approval** — translation reviews are never auto-approved (unlike minor punctuation corrections). Every news translation requires human review.

3. **Sidecar pattern** — `stories_translated.reviewed.json` follows the same pattern as `transcript.reviewed.de.txt` and `script.adapted.reviewed.tr.md`, consumed by downstream stage automatically.

4. **Review enrichment in reviewer.py, not api.py** — `get_review_detail()` in `reviewer.py` handles both the checklist and bilingual data injection, so the API endpoint (`api.py`) remains a thin passthrough.

5. **_revert_episode TRANSLATED→SEGMENTED** — when translation review is rejected or changes requested, the episode reverts to SEGMENTED (not CORRECTED), because the segmentation is still valid; only the translation needs re-running.

---

## Assumptions

1. The `_assemble_translation_review` function takes `diff_data` as a parameter for API consistency with other assembly functions, but does not use it (translation decisions are indexed by story_id, not diff position).

2. The `apply_item_decisions` translate branch returns early (before the common `out_path.write_text()` at end of function) because it handles JSON serialization internally. This matches the existing function structure where each branch sets `out_path` and `reviewed` then falls through to the common writer — except translate writes JSON while correct/adapt write plain text.

3. The `_NEWS_REVIEW_CHECKLIST` constant was placed in `reviewer.py` rather than `api.py` to keep it with the `get_review_detail` function that uses it, and to make it testable without Flask context.

---

## Verification Steps

```bash
# Run new tests
pytest tests/test_translation_review.py -v -q

# Run existing tagesschau tests (no regressions)
pytest tests/test_tagesschau_flow.py -v -q

# Full suite
pytest -x -q

# Lint
ruff check btcedu/ tests/
```

**Results:** 944 tests pass, 0 failures, 0 ruff errors (new files clean).

---

## Limitations

- **No UI changes**: the bilingual review data is returned by the API but the JavaScript SPA does not yet render it as a side-by-side view. The data is there; the rendering is a separate UI sprint.
- **Checklist not persisted**: the 6-item checklist is a hint in the API response, not stored in DB. Reviewer cannot mark individual checklist items as checked.
- **No per-headline review**: headlines are reviewed as part of their story item; no separate headline-level review items.
- **Warning thresholds are fixed**: the ±50% word ratio warning threshold is hardcoded. A future improvement could make this configurable per profile.
