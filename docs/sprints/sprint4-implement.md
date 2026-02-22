# Sprint 4 — Implementation Prompt (Turkish Translation Stage)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 4 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–3 completed codebase
> - **Expected output**: All code changes (new files, modified files), translation prompt template, tests — committed and passing.

---

## Context

You are implementing **Sprint 4 (Phase 2, Part 1: Turkish Translation Stage)** of the btcedu video production pipeline.

Sprints 1–3 (Phase 0 + Phase 1) are complete:
- Foundation: New `EpisodeStatus` values, `PromptVersion`/`ReviewTask`/`ReviewDecision` models, `PromptRegistry`, `pipeline_version` in config.
- Correction: `btcedu/core/corrector.py` produces corrected transcript + diff + provenance. CORRECT stage in pipeline.
- Review System: `btcedu/core/reviewer.py` handles create/approve/reject/request-changes. Review Gate 1 after CORRECT works. Dashboard review queue + diff viewer functional.

Sprint 4 adds the **TRANSLATE** stage — faithful German-to-Turkish translation of the corrected, review-approved transcript. There is **no review gate** after translation. The next review gate is after adaptation (Sprint 5).

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 4 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/corrector.py` (the pattern to follow), `btcedu/core/pipeline.py`, `btcedu/core/reviewer.py`, `btcedu/cli.py`, `btcedu/services/claude_service.py`, `btcedu/core/prompt_registry.py`, `btcedu/models/episode.py`.

2. **Create the translation prompt template** — `btcedu/prompts/templates/translate.md` with:
   - YAML frontmatter: name (`translate`), model, temperature (0.3), max_tokens (8192), description, author
   - System section: professional German-to-Turkish translator specializing in Bitcoin/crypto content
   - Instructions:
     - Produce a faithful, high-quality Turkish translation
     - Preserve the original meaning, tone, and structure
     - Keep technical terms with original in parentheses: "madencilik (Mining)"
     - Pass through code, URLs, and technical identifiers unchanged
     - Keep speaker names as-is
     - Do NOT adapt cultural references (that is a separate stage)
     - Do NOT add information not in the original
     - Do NOT simplify or reinterpret technical content
   - Input variable: `{{ transcript }}`
   - Output format: translated plain text in Turkish

3. **Create TranslationResult dataclass** — include: translated_text, provenance, cost, token counts, segments_processed.

4. **Implement `translate_transcript()`** in `btcedu/core/translator.py`:
   - Load prompt template via PromptRegistry
   - Read input from `data/transcripts/{ep_id}/transcript.corrected.de.txt`
   - **Pre-condition check**: Verify episode status is CORRECTED and Review Gate 1 (stage="correct") is APPROVED. Fail with clear error if not.
   - Check idempotency (output exists + prompt hash match + input hash match)
   - For long transcripts (>~4000 characters): split at paragraph breaks, process segments individually, reassemble
   - For each segment: call Claude via existing service/pattern
   - Save translated text to `data/transcripts/{ep_id}/transcript.tr.txt`
   - Save provenance to `data/outputs/{ep_id}/provenance/translate_provenance.json`
   - Register/record prompt version
   - Return TranslationResult

5. **Implement segmentation logic**:
   - Split input text at double-newline (`\n\n`) paragraph boundaries
   - If any single segment exceeds the threshold (~4000 chars), split further at sentence boundaries (`. `, `! `, `? `)
   - For each segment, include a small context overlap from the previous segment's last sentence (for translation continuity)
   - Reassemble by concatenating translated segments with paragraph separators
   - Track which segments were processed for provenance

6. **Add `translate` CLI command** to `btcedu/cli.py`:
   - `btcedu translate <episode_id>` with `--force` and `--dry-run` flags
   - Validate episode exists, is at CORRECTED status, and Review Gate 1 is approved
   - On success: update episode status to TRANSLATED
   - On failure: log error, leave status unchanged
   - Follow existing CLI patterns from `btcedu correct`

7. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
   - Ensure TRANSLATE is in `PipelineStage` enum (may already exist from Sprint 1)
   - Update `resolve_pipeline_plan()` to include TRANSLATE for `pipeline_version=2` episodes
   - Position TRANSLATE after CORRECTED + Review Gate 1 approval, before ADAPT
   - Pipeline must check for approved ReviewTask (stage="correct") before executing TRANSLATE

8. **Implement cascade invalidation hookup** (if not already present):
   - When correction is re-run (e.g., after reject + re-correct), mark translation output as stale
   - Use `.stale` marker pattern from MASTERPLAN.md §8
   - At minimum, create a utility function that writes `.stale` markers for downstream outputs
   - Hook this into the corrector's re-run path (or into `invalidate_downstream()` if it exists)

9. **Write tests**:
   - `tests/test_translator.py`:
     - Unit: segmentation logic (split, reassemble, boundary handling)
     - Unit: pre-condition check (fails if review gate not approved)
     - Integration: translation with dry-run (no API call, no file writes)
     - Idempotency: second run skips
     - Force: `--force` re-runs
   - CLI test: `btcedu translate --help` works
   - Pipeline test: TRANSLATE included in v2 plan after review gate approval

10. **Verify**:
    - Run `pytest tests/`
    - Pick an existing corrected + review-approved episode
    - Run `btcedu translate <ep_id> --dry-run`
    - Run `btcedu translate <ep_id>`
    - Verify output at `data/transcripts/{ep_id}/transcript.tr.txt`
    - Verify provenance at `data/outputs/{ep_id}/provenance/translate_provenance.json`
    - Run again → verify skipped (idempotent)
    - Run with `--force` → verify re-runs
    - Run `btcedu status` → verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement the ADAPT stage (that's Sprint 5).
- **Do NOT** add Review Gate 2 (that's Sprint 5).
- **Do NOT** implement cultural adaptation, tone changes, or content neutralization.
- **Do NOT** add adaptation diff computation or review UI for adaptation.
- **Do NOT** modify the existing correction or review system.
- **Do NOT** modify existing dashboard pages (a simple translated text view in the episode detail page is acceptable if it follows existing patterns, but do not build a new dedicated page).
- **Do NOT** refactor existing code for style or cleanup purposes.
- **Do NOT** add new dependencies unless strictly required.

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/corrector.py` closely — the translator module should mirror its structure: load prompt, check idempotency, call Claude, save output, write provenance.
- **Claude API calls**: Use the same calling pattern as the corrector.
- **File I/O**: Use `Path` objects, `mkdir(parents=True, exist_ok=True)`.
- **CLI commands**: Follow the same Click command pattern as `btcedu correct`.
- **Provenance**: Follow MASTERPLAN.md §3.6 format exactly.
- **Idempotency**: Follow MASTERPLAN.md §8 — check file existence, prompt hash, input content hash.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred to Sprint 5 (adaptation, Review Gate 2, adaptation diff/review)
- Manual verification steps:
  - Pick a corrected + review-approved episode
  - Run `btcedu translate <ep_id> --dry-run`
  - Run `btcedu translate <ep_id>`
  - Verify output files at expected paths
  - Run again → verify skipped (idempotent)
  - Run with `--force` → verify re-runs
  - Attempt on a non-approved episode → verify error
  - Run `btcedu status` → verify v1 unaffected

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- SHA-256 hashes for idempotency should use `hashlib.sha256` on file content bytes.
- Provenance JSON should be written with `json.dump()` using `indent=2` and `ensure_ascii=False` (Turkish/German characters).
- The translation prompt MUST NOT include any cultural adaptation instructions. It must produce a faithful translation only.

---

## Definition of Done

- [ ] `btcedu/prompts/templates/translate.md` exists with valid YAML frontmatter and translation instructions
- [ ] `btcedu/core/translator.py` exists with `translate_transcript()` function
- [ ] Translator produces `transcript.tr.txt` at `data/transcripts/{ep_id}/transcript.tr.txt`
- [ ] Translator produces `translate_provenance.json` at `data/outputs/{ep_id}/provenance/translate_provenance.json`
- [ ] Pre-condition check: translator fails clearly if Review Gate 1 not approved
- [ ] Long transcript segmentation works (paragraph splits, reassembly)
- [ ] `btcedu translate <episode_id>` CLI command works with `--force` and `--dry-run`
- [ ] Pipeline plan includes TRANSLATE for v2 episodes after Review Gate 1 approval
- [ ] Episode status updated to TRANSLATED on success
- [ ] Idempotency works: second run skips, `--force` re-runs
- [ ] Prompt version registered in DB via PromptRegistry
- [ ] Cascade invalidation: correction re-run marks translation as stale
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- ADAPT stage (Sprint 5)
- Review Gate 2 / adaptation review (Sprint 5)
- Cultural adaptation or content neutralization (Sprint 5)
- Adaptation diff computation (Sprint 5)
- New dashboard pages for translation (a view of translated text on existing episode detail page is acceptable)
- A/B testing of translation prompts (later sprint)
- Alternative translation providers (Google Translate, DeepL) — Claude only for now
