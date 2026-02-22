# Sprint 4 — Validation Prompt (Turkish Translation Stage)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 4 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–3 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 4 (Phase 2, Part 1: Turkish Translation Stage)** implementation of the btcedu video production pipeline.

Sprint 4 was scoped to:
- Create the translation prompt template (`translate.md`)
- Implement `translate_transcript()` in `btcedu/core/translator.py`
- Implement segment-by-segment processing for long texts
- Add `translate` CLI command with `--force` and `--dry-run`
- Integrate TRANSLATE stage into v2 pipeline after Review Gate 1 approval
- Register prompt version via PromptRegistry
- Store provenance JSON
- Implement idempotency checks
- Implement cascade invalidation (correction re-run → translation marked stale)
- Write tests

Sprint 4 was NOT scoped to include: ADAPT stage, Review Gate 2, cultural adaptation, adaptation diff/review UI, new dashboard pages.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Translation Prompt Template

- [ ] **1.1** `btcedu/prompts/templates/translate.md` exists
- [ ] **1.2** Has valid YAML frontmatter with: name (`translate`), model, temperature, max_tokens, description, author
- [ ] **1.3** System section instructs LLM to act as a professional German-to-Turkish translator for Bitcoin/crypto content
- [ ] **1.4** Instructions specify faithful translation — no cultural adaptation, no content changes
- [ ] **1.5** Technical term handling: keep originals in parentheses (e.g., "madencilik (Mining)")
- [ ] **1.6** Code/URLs/speaker names passed through unchanged
- [ ] **1.7** Input variable `{{ transcript }}` is used
- [ ] **1.8** Output format is specified (translated Turkish plain text)
- [ ] **1.9** Prompt does NOT contain adaptation or cultural neutralization instructions (that is Sprint 5)
- [ ] **1.10** Prompt does NOT contain financial advice or hallucination-prone instructions

### 2. Translator Module

- [ ] **2.1** `btcedu/core/translator.py` exists
- [ ] **2.2** `translate_transcript()` function has correct signature: `session`, `episode_id`, `settings`, `force=False` (or similar matching corrector pattern)
- [ ] **2.3** Function returns a structured result (TranslationResult or similar) with translated_text, provenance, cost info
- [ ] **2.4** Reads input from `data/transcripts/{ep_id}/transcript.corrected.de.txt`
- [ ] **2.5** Writes output to `data/transcripts/{ep_id}/transcript.tr.txt`
- [ ] **2.6** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **2.7** Uses existing Claude API calling pattern (matches corrector style)
- [ ] **2.8** Loads prompt via PromptRegistry (not hardcoded)
- [ ] **2.9** Handles empty input gracefully (error, not crash)
- [ ] **2.10** Pre-condition check: verifies Review Gate 1 (stage="correct") is APPROVED before proceeding
- [ ] **2.11** Pre-condition check: fails with clear, descriptive error if Review Gate 1 not approved

### 3. Segmentation

- [ ] **3.1** Long text segmentation is implemented (for transcripts exceeding threshold)
- [ ] **3.2** Segments split at paragraph boundaries (`\n\n`) as primary strategy
- [ ] **3.3** Fallback splitting at sentence boundaries when paragraphs are too long
- [ ] **3.4** Segments are reassembled correctly after translation
- [ ] **3.5** Context overlap or continuity mechanism exists between segments (to maintain translation consistency)
- [ ] **3.6** Segmentation handles edge cases: single paragraph text, text with no paragraph breaks, very short text
- [ ] **3.7** Number of segments processed is tracked (for provenance/reporting)

### 4. Provenance

- [ ] **4.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/translate_provenance.json`
- [ ] **4.2** Provenance format matches MASTERPLAN.md §3.6: stage, episode_id, timestamp, prompt_name, prompt_version, prompt_hash, model, model_params, input_files, output_files, input_tokens, output_tokens, cost_usd, duration_seconds
- [ ] **4.3** Provenance is written with `indent=2` and `ensure_ascii=False`
- [ ] **4.4** Prompt hash in provenance matches the hash stored in PromptVersion record

### 5. Idempotency

- [ ] **5.1** Second run without `--force` skips translation (does not call API)
- [ ] **5.2** Idempotency check verifies: output file exists AND prompt hash matches current default AND input content hash matches stored hash
- [ ] **5.3** `--force` flag bypasses idempotency check and re-runs
- [ ] **5.4** `.stale` marker is respected if present (skips idempotency, forces re-run)
- [ ] **5.5** Content hashes use SHA-256 (not mtime)

### 6. Cascade Invalidation

- [ ] **6.1** When correction is re-run, translation output is marked as stale (or equivalent mechanism)
- [ ] **6.2** `.stale` marker file is created with invalidation metadata (invalidated_by, invalidated_at, reason) per §8
- [ ] **6.3** Stale translation does not block pipeline — it triggers re-translation on next run
- [ ] **6.4** Cascade invalidation propagates downstream: re-correction → translation stale → adaptation stale (when adaptation exists in Sprint 5)
- [ ] **6.5** `invalidate_downstream()` utility function exists or is integrated into the corrector's re-run path

### 7. CLI Command

- [ ] **7.1** `btcedu translate <episode_id>` command exists and is registered
- [ ] **7.2** `--force` flag works
- [ ] **7.3** `--dry-run` flag works (no API call, no file writes, but shows what would happen)
- [ ] **7.4** `btcedu translate --help` shows useful help text
- [ ] **7.5** Command validates episode exists
- [ ] **7.6** Command validates episode is at CORRECTED status with approved Review Gate 1
- [ ] **7.7** On success: episode status updated to TRANSLATED
- [ ] **7.8** On failure: episode status unchanged, error logged
- [ ] **7.9** Command follows existing CLI patterns in `btcedu/cli.py` (matches `correct` command style)

### 8. Pipeline Integration

- [ ] **8.1** TRANSLATE is in `PipelineStage` enum
- [ ] **8.2** `resolve_pipeline_plan()` includes TRANSLATE for `pipeline_version=2` episodes
- [ ] **8.3** TRANSLATE is positioned after CORRECTED + Review Gate 1 approval
- [ ] **8.4** TRANSLATE is positioned before ADAPT (when ADAPT is added in Sprint 5)
- [ ] **8.5** Pipeline checks for approved ReviewTask (stage="correct") before executing TRANSLATE
- [ ] **8.6** v1 pipeline (`pipeline_version=1`) is completely unaffected
- [ ] **8.7** No review gate is added after TRANSLATE (per §5B — no review gate for translation)

### 9. V1 Pipeline Compatibility (Regression)

- [ ] **9.1** `btcedu status` still works for existing episodes
- [ ] **9.2** v1 pipeline stages (chunk, generate, refine) are unmodified
- [ ] **9.3** Correction stage and Review Gate 1 still work correctly
- [ ] **9.4** Existing tests still pass
- [ ] **9.5** No existing CLI commands are broken
- [ ] **9.6** No existing models or schemas are modified (beyond adding to PipelineStage enum if needed)
- [ ] **9.7** Dashboard review system still functions correctly

### 10. Test Coverage

- [ ] **10.1** Unit test for segmentation logic (split at paragraphs, fallback to sentences, reassembly)
- [ ] **10.2** Unit test for pre-condition check (fails if Review Gate 1 not approved)
- [ ] **10.3** Test that translation with dry-run does not write files or call API
- [ ] **10.4** Test that idempotency check works (second call skips)
- [ ] **10.5** Test that `--force` overrides idempotency
- [ ] **10.6** Test for CLI command registration (`btcedu translate --help`)
- [ ] **10.7** Test that episode status is updated to TRANSLATED on success
- [ ] **10.8** Test for cascade invalidation (correction re-run marks translation stale)
- [ ] **10.9** Tests use mocked Claude API calls (not real API)
- [ ] **10.10** All tests pass with `pytest tests/`

### 11. Scope Creep Detection

- [ ] **11.1** No ADAPT stage was implemented
- [ ] **11.2** No Review Gate 2 was added
- [ ] **11.3** No cultural adaptation or content neutralization logic was implemented
- [ ] **11.4** No adaptation diff computation was added
- [ ] **11.5** No new dashboard pages were created (minor additions to existing pages are acceptable)
- [ ] **11.6** No existing pipeline stages were modified
- [ ] **11.7** No existing review system was modified
- [ ] **11.8** No unnecessary dependencies were added

### 12. Prompt Governance

- [ ] **12.1** Translation prompt is registered as a PromptVersion in DB on first use
- [ ] **12.2** Prompt version is linked to translation output via provenance
- [ ] **12.3** Prompt content hash is computed correctly (SHA-256 of body, excluding frontmatter)
- [ ] **12.4** If prompt file changes between runs, a new version is registered

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 4 is complete and ready for Sprint 5 (Adaptation). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 5 after fixes. |
| **FAIL** | Critical issues found. Sprint 4 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Deferred Items Acknowledged:

- ADAPT stage with cultural neutralization (Sprint 5)
- Review Gate 2 after adaptation (Sprint 5)
- Adaptation diff and review UI (Sprint 5)
- Dashboard adaptation view (Sprint 5)
- Alternative translation providers (later if needed)
- Translation A/B testing (later)
- CHAPTERIZE stage (Sprint 6)

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- Verify that the translation prompt produces faithful translations — it must NOT contain any cultural adaptation instructions.
- Check that the pre-condition check (Review Gate 1 approved) is enforced both in the translator module and in the CLI command.
- Check that provenance tracking creates an auditable chain from corrected transcript → prompt version → translated output.
- Verify that cascade invalidation works: if correction is re-run, translation output should be marked as stale.
