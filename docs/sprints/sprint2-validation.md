# Sprint 2 — Validation Prompt (Transcript Correction Stage)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 2 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 2 (Phase 1, Part 1: Transcript Correction Stage)** implementation of the btcedu video production pipeline.

Sprint 2 was scoped to:
- Create the correction prompt template (`correct_transcript.md`)
- Implement `correct_transcript()` in `btcedu/core/corrector.py`
- Implement structured diff computation (JSON format per §5A)
- Add `correct` CLI command with `--force` and `--dry-run`
- Integrate CORRECT stage into v2 pipeline plan
- Register prompt version via PromptRegistry
- Store provenance JSON
- Implement idempotency checks
- Write tests

Sprint 2 was NOT scoped to include: dashboard diff viewer, review gate, review queue UI, TRANSLATE/ADAPT stages.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Correction Prompt Template

- [ ] **1.1** `btcedu/prompts/templates/correct_transcript.md` exists
- [ ] **1.2** Has valid YAML frontmatter with: name (`correct_transcript`), model, temperature, max_tokens, description, author
- [ ] **1.3** System section instructs LLM to act as German transcript editor for Bitcoin/crypto content
- [ ] **1.4** Instructions cover: spelling correction, punctuation, sentence boundaries, speaker attribution
- [ ] **1.5** Hard constraints are present: do NOT add information, do NOT change meaning, do NOT translate, do NOT remove content
- [ ] **1.6** Input variable `{{ transcript }}` is used
- [ ] **1.7** Output format is specified (corrected plain text)
- [ ] **1.8** Prompt does NOT contain financial advice, political content, or hallucination-prone instructions

### 2. Corrector Module

- [ ] **2.1** `btcedu/core/corrector.py` exists
- [ ] **2.2** `correct_transcript()` function has correct signature: `session`, `episode_id`, `settings`, `force=False`
- [ ] **2.3** Function returns a structured result (CorrectionResult or similar) with corrected_text, diff, provenance, cost info
- [ ] **2.4** Reads input from `data/transcripts/{ep_id}/transcript.de.txt`
- [ ] **2.5** Writes corrected output to `data/transcripts/{ep_id}/transcript.corrected.de.txt`
- [ ] **2.6** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **2.7** Uses existing Claude API calling pattern (matches `btcedu/core/generator.py` style)
- [ ] **2.8** Loads prompt via PromptRegistry (not hardcoded)
- [ ] **2.9** Handles empty transcript gracefully (error, not crash)
- [ ] **2.10** Long transcript handling exists (segmentation or documented limitation)

### 3. Diff Computation

- [ ] **3.1** Structured diff JSON written to `data/outputs/{ep_id}/review/correction_diff.json`
- [ ] **3.2** Diff format matches MASTERPLAN.md §5A: `episode_id`, `original_length`, `corrected_length`, `changes` array, `summary`
- [ ] **3.3** Each change has: `type` (replace/insert/delete), `original`, `corrected`, `context`, `position`, `category`
- [ ] **3.4** Summary includes `total_changes` and breakdown (at minimum by change type; by category if implemented)
- [ ] **3.5** Diff is deterministic — same inputs always produce same diff
- [ ] **3.6** Diff handles edge cases: no changes (empty changes array), entire text replaced, empty input

### 4. Provenance

- [ ] **4.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/correct_provenance.json`
- [ ] **4.2** Provenance format matches MASTERPLAN.md §3.6: stage, episode_id, timestamp, prompt_name, prompt_version, prompt_hash, model, model_params, input_files, output_files, input_tokens, output_tokens, cost_usd, duration_seconds
- [ ] **4.3** Provenance is written with `indent=2` and `ensure_ascii=False`
- [ ] **4.4** Prompt hash in provenance matches the hash stored in PromptVersion record

### 5. Idempotency

- [ ] **5.1** Second run without `--force` skips correction (does not call API)
- [ ] **5.2** Idempotency check verifies: output file exists AND prompt hash matches current default AND input content hash matches stored hash
- [ ] **5.3** `--force` flag bypasses idempotency check and re-runs
- [ ] **5.4** `.stale` marker is respected if present (or documented as future work)
- [ ] **5.5** Content hashes use SHA-256 (not mtime)

### 6. CLI Command

- [ ] **6.1** `btcedu correct <episode_id>` command exists and is registered
- [ ] **6.2** `--force` flag works
- [ ] **6.3** `--dry-run` flag works (no API call, no file writes, but shows what would happen)
- [ ] **6.4** `btcedu correct --help` shows useful help text
- [ ] **6.5** Command validates episode exists
- [ ] **6.6** Command validates episode is at TRANSCRIBED status (or later for --force)
- [ ] **6.7** On success: episode status updated to CORRECTED
- [ ] **6.8** On failure: episode status unchanged, error logged
- [ ] **6.9** Command follows existing CLI patterns in `btcedu/cli.py`

### 7. Pipeline Integration

- [ ] **7.1** CORRECT is in `PipelineStage` enum
- [ ] **7.2** `resolve_pipeline_plan()` includes CORRECT for `pipeline_version=2` episodes
- [ ] **7.3** CORRECT is positioned after TRANSCRIBED, before any translation stage
- [ ] **7.4** v1 pipeline (`pipeline_version=1`) is completely unaffected
- [ ] **7.5** Pipeline does NOT include review gate logic yet (that's Sprint 3)

### 8. V1 Pipeline Compatibility (Regression)

- [ ] **8.1** `btcedu status` still works for existing episodes
- [ ] **8.2** v1 pipeline stages (chunk, generate, refine) are unmodified
- [ ] **8.3** Existing tests still pass
- [ ] **8.4** No existing CLI commands are broken
- [ ] **8.5** No existing models or schemas are modified

### 9. Test Coverage

- [ ] **9.1** Unit test for diff computation with known input pairs
- [ ] **9.2** Test that correction with dry-run does not write files
- [ ] **9.3** Test that idempotency check works (second call skips)
- [ ] **9.4** Test that `--force` overrides idempotency
- [ ] **9.5** Test for CLI command registration (`btcedu correct --help`)
- [ ] **9.6** Test that episode status is updated to CORRECTED on success
- [ ] **9.7** Tests use mocked Claude API calls (not real API)
- [ ] **9.8** All tests pass with `pytest tests/`

### 10. Scope Creep Detection

- [ ] **10.1** No dashboard/UI changes were made
- [ ] **10.2** No review gate or approval flow was implemented
- [ ] **10.3** No review queue API endpoints were added
- [ ] **10.4** No TRANSLATE or ADAPT stages were implemented
- [ ] **10.5** No existing pipeline stages were modified
- [ ] **10.6** No existing prompt Python modules were modified
- [ ] **10.7** No unnecessary dependencies added

### 11. Prompt Governance

- [ ] **11.1** Correction prompt is registered as a PromptVersion in DB on first use
- [ ] **11.2** Prompt version is linked to correction output via provenance
- [ ] **11.3** Prompt content hash is computed correctly (SHA-256 of body, excluding frontmatter)
- [ ] **11.4** If prompt file changes between runs, a new version is registered

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 2 is complete and ready for Sprint 3. |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 3 after fixes. |
| **FAIL** | Critical issues found. Sprint 2 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Deferred Items Acknowledged:

- Dashboard diff viewer (Sprint 3)
- Review gate / approval-rejection flow (Sprint 3)
- Review queue UI and API endpoints (Sprint 3)
- Cascade invalidation of downstream stages (Sprint 2+ as stages are added)
- Auto-approve rules for trivial corrections (later sprint)
- TRANSLATE stage (Sprint 4)
- ADAPT stage (Sprint 4-5)

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- Verify that the correction prompt does not violate any safety constraints from MASTERPLAN.md §5C (no financial advice, no hallucination, editorial neutrality).
- Check that provenance tracking creates an auditable chain from input → prompt version → output.
