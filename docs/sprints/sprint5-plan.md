# Sprint 5 — Planning Prompt (Turkey-Context Adaptation Stage)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–4 completed codebase (especially `btcedu/core/translator.py` for stage pattern, `btcedu/core/corrector.py`, `btcedu/core/reviewer.py`, `btcedu/core/pipeline.py`, `btcedu/prompts/templates/`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the adapter module, adaptation prompt template with tiered neutralization rules, adaptation diff computation, Review Gate 2, adaptation review UI in dashboard, CLI command, pipeline integration, and tests.

---

## Context

You are planning **Sprint 5 (Phase 2, Part 2: Turkey-Context Adaptation Stage)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–4 are complete:
- Foundation: `EpisodeStatus` enum, `PromptVersion`/`ReviewTask`/`ReviewDecision` models, `PromptRegistry`, `pipeline_version` in config.
- Correction: `btcedu/core/corrector.py`, correction diff, provenance, CORRECT stage in pipeline.
- Review System: `btcedu/core/reviewer.py`, Review Gate 1 (after CORRECT) with dashboard queue + diff viewer + approve/reject/request-changes.
- Translation: `btcedu/core/translator.py`, faithful German-to-Turkish translation, segmentation, TRANSLATE stage in pipeline.

Sprint 5 implements the **ADAPT** stage — Turkey-context cultural adaptation of the Turkish translation with a tiered rule system, and **Review Gate 2** for human review of adaptations. This is the most editorially sensitive stage in the pipeline, requiring careful prompt engineering and robust review tooling.

### Sprint 5 Focus (from MASTERPLAN.md §4 Phase 2, §5C, §5H)

1. Create the adaptation prompt template (`btcedu/prompts/templates/adapt.md`) with YAML frontmatter, Jinja2 variables, and the full tiered adaptation rule system.
2. Implement `adapt_script()` in `btcedu/core/adapter.py` — call Claude with the adaptation prompt, receive adapted script, save to file.
3. Implement adaptation diff computation — compare literal translation vs adapted version, produce `adaptation_diff.json`.
4. Add Review Gate 2 after ADAPT — create ReviewTask, pause pipeline, require approval.
5. Extend the dashboard review system to handle adaptation reviews:
   - Review detail page for adaptation: side-by-side literal translation vs adapted version
   - Highlighted adaptations (color-coded by tier: T1 mechanical, T2 editorial)
   - Approve/reject/request-changes for adaptation
6. Implement reviewer feedback injection for adaptation re-runs.
7. Add `adapt` CLI command to `btcedu/cli.py` with `--force` and `--dry-run`.
8. Integrate into pipeline — ADAPT after TRANSLATED, with Review Gate 2 before proceeding.
9. Provenance, idempotency, cascade invalidation.
10. Write tests.

### Relevant Subplans

- **Subplan 5C** (Turkey-Context Adaptation) — all slices: core adapter, adaptation diff, CLI + pipeline integration, Review Gate 2, prompt iteration.
- **Subplan 5H** (Human Review & Approval Workflow) — slice 6 (adaptation review view). Extend the existing review system for adaptation-specific rendering.
- **§8** (Idempotency) — ADAPT stage specifics.
- **§3.6** (Provenance Model) — provenance JSON format.
- **§9** (Quality Assurance) — adaptation review methods.

---

## Your Task

Produce a detailed implementation plan for Sprint 5. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **Adaptation Prompt Template** — full draft of `adapt.md` including YAML frontmatter, the complete tiered adaptation rule system from §5C:
   - *Tier 1 — Mechanical (low risk, auto-applicable)*:
     1. Replace German institutions with Turkish equivalents (BaFin → SPK, Sparkasse → generic)
     2. Replace Euro amounts with context-appropriate Turkish Lira or USD
     3. Adjust tone to Turkish influencer style (conversational, "siz" formal)
     4. Remove Germany-specific legal/tax advice with marker `[kaldırıldı: ülkeye özgü]`
   - *Tier 2 — Editorial (flagged for review, highlighted in diff)*:
     5. Replace German cultural references with Turkish equivalents (each tagged for reviewer)
     6. Any regulatory/legal context change beyond simple removal
   - *Hard constraints (FORBIDDEN)*:
     7. Keep ALL Bitcoin/crypto technical facts unchanged
     8. Do NOT invent Turkish regulatory details — never fabricate Turkish law references
     9. Do NOT add financial advice, investment recommendations, or price predictions
     10. Do NOT add political commentary or partisan framing
     11. Do NOT present adaptation choices as claims from the original source
     12. Editorial neutrality: adaptation changes framing, not facts
   - Each adaptation tagged with `[T1]` or `[T2]` in output
   - Input variables: `{{ translation }}`, `{{ original_german }}` (for reference), `{{ reviewer_feedback }}` (optional)
4. **Adapter Module Design** — `adapt_script()` function signature, return type (`AdaptationResult` dataclass), processing logic. The adapter receives both the Turkish translation and the original German corrected transcript (as reference).
5. **Adaptation Diff Computation** — algorithm for comparing literal Turkish translation vs adapted Turkish script. The diff must:
   - Identify all adaptations made
   - Tag each with `[T1]` or `[T2]` based on the adaptation type
   - Include the original text and the adapted replacement
   - Highlight Tier 2 changes distinctly for review
6. **Review Gate 2 Design** — how Review Gate 2 integrates:
   - After `adapt_script()` succeeds, create ReviewTask with stage="adapt"
   - Pipeline pauses at ADAPTED until ReviewTask is APPROVED
   - Rejection reverts to TRANSLATED
   - Request-changes injects notes into re-adaptation prompt
7. **Adaptation Review UI** — extend the existing review detail page to handle adaptation reviews:
   - Side-by-side: literal translation vs adapted version
   - Adaptations highlighted by tier (T1 in one color, T2 in a different, more prominent color)
   - Same approve/reject/request-changes buttons as correction review
   - How to detect stage type and render the appropriate diff view
8. **CLI Command Design** — `btcedu adapt <episode_id>` with `--force` and `--dry-run`.
9. **Pipeline Integration** — ADAPT after TRANSLATED, Review Gate 2 before CHAPTERIZE.
10. **Provenance, Idempotency, Cascade Invalidation** — provenance JSON, idempotency checks, cascade from translation re-run.
11. **Test Plan** — list each test function, what it asserts, and which file it belongs to.
12. **Implementation Order** — numbered sequence of steps.
13. **Definition of Done** — checklist.
14. **Non-Goals** — explicit list of what Sprint 5 does NOT include.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected. ADAPT only runs for v2 episodes.
- **Reuse the review system**: Do NOT create a separate review framework. Extend the existing `btcedu/core/reviewer.py` and review routes/templates to support adaptation reviews alongside correction reviews.
- **Follow existing patterns**: The adapter should mirror the translator/corrector module pattern.
- **Safety-critical prompt design**: The adaptation prompt MUST enforce the hard constraints from §5C. No hallucinated regulatory content. No financial advice. No political commentary. Editorial neutrality. This is the highest-risk prompt in the pipeline — plan accordingly.
- **Additive changes only to review UI**: Extend the existing review detail template to detect the stage type and render the appropriate diff view. Do NOT redesign the review system.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include code snippets for the prompt template, function signatures, diff format, and review UI logic.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The adaptation prompt is the most safety-critical prompt in the pipeline. Take extra care to include all hard constraints from §5C. If in doubt, err on the side of more restrictions rather than fewer.
- The adaptation diff should clearly distinguish Tier 1 (mechanical, low-risk) from Tier 2 (editorial, flagged for human review) adaptations.
- Consider how the existing review detail template can be extended to show adaptation diffs — the current template handles correction diffs. You likely need conditional rendering based on the review stage.
- The adapter takes TWO inputs: the Turkish translation AND the original German corrected transcript (for reference). This is explicitly stated in §5C.
