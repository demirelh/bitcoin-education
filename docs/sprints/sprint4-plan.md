# Sprint 4 — Planning Prompt (Turkish Translation Stage)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–3 completed codebase (especially `btcedu/core/corrector.py` for stage implementation patterns, `btcedu/core/pipeline.py`, `btcedu/core/prompt_registry.py`, `btcedu/prompts/templates/`, `btcedu/cli.py`, `btcedu/models/episode.py`)
> - **Expected output**: A file-level implementation plan covering the translator module, translation prompt template, CLI command, pipeline integration, provenance, idempotency, cascade invalidation, and tests.

---

## Context

You are planning **Sprint 4 (Phase 2, Part 1: Turkish Translation Stage)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–3 (Phase 0 + Phase 1) are complete:
- Foundation: `EpisodeStatus` enum has v2 values, `PromptVersion`/`ReviewTask`/`ReviewDecision` models exist, `PromptRegistry` works, `pipeline_version` is in config.
- Correction: `btcedu/core/corrector.py` with `correct_transcript()` exists, `btcedu correct <episode_id>` CLI works, diff JSON + provenance generated, CORRECT stage integrated into v2 pipeline.
- Review System: `btcedu/core/reviewer.py` exists, Review Gate 1 (after CORRECT) works, dashboard review queue + diff viewer functional, approve/reject/request-changes flow works end-to-end, CLI review commands work.

Sprint 4 implements the **TRANSLATE** stage — German-to-Turkish faithful translation of the corrected transcript. This is the first stage after Review Gate 1 approval. Per MASTERPLAN.md §5B, there is **no review gate** after translation (the editorial review happens after adaptation in Sprint 5). Translation is treated as a mechanical, high-fidelity rendering.

### Sprint 4 Focus (from MASTERPLAN.md §4 Phase 2 and §5B)

1. Create the translation prompt template (`btcedu/prompts/templates/translate.md`) with YAML frontmatter and Jinja2 variables.
2. Implement `translate_transcript()` in `btcedu/core/translator.py` — call Claude with the translation prompt, receive Turkish translation, save to file.
3. Implement segment-by-segment processing for long texts (aligned with paragraph breaks).
4. Add `translate` CLI command to `btcedu/cli.py` — `btcedu translate <episode_id>` with `--force` and `--dry-run` flags.
5. Integrate into pipeline — update `resolve_pipeline_plan()` so the v2 pipeline includes TRANSLATE after CORRECTED (and after Review Gate 1 approval).
6. Register the translation prompt version via `PromptRegistry` on first use.
7. Store provenance JSON (format in §3.6) after each translation run.
8. Implement idempotency checks (§8, TRANSLATE Stage): skip if output exists AND prompt hash matches AND input content hash matches.
9. Implement cascade invalidation: if correction changes, mark translation as stale (downstream of CORRECT).
10. Write tests: unit (segmentation), integration (translation with dry-run), CLI (`--help`), idempotency, cascade invalidation.

### Relevant Subplans

- **Subplan 5B** (Turkish Translation) — all slices: core translator with segmented processing, CLI command and pipeline integration, dashboard view of translated text.
- **§8** (Idempotency) — TRANSLATE stage specifics.
- **§3.6** (Provenance Model) — provenance JSON format.
- **§8 Cascade Invalidation** — TRANSLATE is invalidated by correction changes or prompt version changes.

---

## Your Task

Produce a detailed implementation plan for Sprint 4. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **Translation Prompt Template** — full draft of `translate.md` including YAML frontmatter, system instructions, input variables (`{{ transcript }}`, `{{ source_language }}`, `{{ target_language }}`), output format specification. Key constraints:
   - Faithful rendering — no adaptation, no cultural changes
   - Technical terms kept with original in parentheses: "madencilik (Mining)"
   - Code/URLs passed through unchanged
   - Speaker names kept as-is
4. **Translator Module Design** — `translate_transcript()` function signature, return type (`TranslationResult` dataclass), error handling, segmentation strategy for long texts (split at paragraph breaks, reassemble).
5. **Segmentation Strategy** — how to split long corrected transcripts into segments for translation. Define:
   - Maximum segment size (aligned with model context limits)
   - Splitting algorithm (paragraph breaks, fallback to sentence boundaries)
   - Reassembly and continuity (overlapping context for segment consistency)
6. **CLI Command Design** — Click command signature, options (`--force`, `--dry-run`), output messages, error handling.
7. **Pipeline Integration** — how `resolve_pipeline_plan()` changes to include TRANSLATE for v2 episodes after Review Gate 1 approval. The pipeline must verify that a CORRECTED episode has an approved ReviewTask (stage="correct") before proceeding to TRANSLATE.
8. **Cascade Invalidation** — how re-correction invalidates the translation output (`.stale` marker pattern from §8). How `invalidate_downstream()` is implemented or extended.
9. **Provenance and Idempotency** — exact provenance JSON fields, idempotency check logic (file exists + prompt hash + input hash).
10. **Test Plan** — list each test function, what it asserts, and which file it belongs to.
11. **Implementation Order** — numbered sequence of steps.
12. **Definition of Done** — checklist.
13. **Non-Goals** — explicit list of what Sprint 4 does NOT include.

---

## Constraints

- **Backward compatibility**: v1 pipeline (`pipeline_version=1`) must not be affected. The TRANSLATE stage only runs for v2 episodes that have passed Review Gate 1.
- **Additive changes only**: No modification to existing pipeline stages or the correction/review system.
- **Follow existing patterns**: Study `btcedu/core/corrector.py` for the stage implementation pattern (file I/O, Claude API calls, provenance, idempotency). The translator should follow the same pattern closely.
- **Use existing services**: Use the same Claude API calling pattern established in the corrector.
- **Use the PromptRegistry** to load and register the translation prompt.
- **No review gate after translation**: Per §5B, translation does not have a review gate. The review happens after adaptation (Sprint 5).
- **No cultural adaptation**: Translation must be faithful. Adaptation is a separate stage (Sprint 5).
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include code snippets for the prompt template, function signatures, provenance format, and idempotency logic.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The translation prompt must instruct the LLM to produce a faithful German-to-Turkish translation of Bitcoin/crypto content without cultural adaptation.
- For segmentation, prefer paragraph-level splitting. If no paragraph breaks exist, fall back to sentence boundaries around the 4000-character mark. Label segmentation thresholds as `[ASSUMPTION]` if not specified in the master plan.
- The translator must verify that Review Gate 1 has been approved before running. If the correction has not been approved, the translator should fail with a clear error message.
- Consider how `invalidate_downstream()` from §8 should work — at minimum, describe how to mark translation output as stale when correction is re-run.
