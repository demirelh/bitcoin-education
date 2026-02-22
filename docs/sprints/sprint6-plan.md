# Sprint 6 — Planning Prompt (Chapterized Production JSON)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–5 completed codebase (especially `btcedu/core/adapter.py` for stage pattern, `btcedu/core/pipeline.py`, `btcedu/core/reviewer.py`, `btcedu/prompts/templates/`, `btcedu/models/`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the chapterizer module, chapter JSON schema definition, chapterization prompt template, duration estimation, visual type classification, CLI command, pipeline integration, dashboard chapter viewer, and tests.

---

## Context

You are planning **Sprint 6 (Phase 3, Part 1: Chapterized Production JSON)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–5 (Phases 0–2) are complete:
- Foundation: `EpisodeStatus` enum, `PromptVersion`/`ReviewTask`/`ReviewDecision` models, `PromptRegistry`, `pipeline_version`.
- Correction: `btcedu/core/corrector.py`, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: `btcedu/core/reviewer.py`, dashboard review queue + diff viewer + approve/reject/request-changes.
- Translation: `btcedu/core/translator.py`, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: `btcedu/core/adapter.py`, Turkey-context adaptation with tiered rules, adaptation diff, ADAPT stage, Review Gate 2.

Sprint 6 implements the **CHAPTERIZE** stage — breaking the adapted script into structured production-ready chapters. Each chapter defines narration (what is said), visuals (what is shown), overlays (text/graphics), and timing guidance. The output is a strict JSON document that drives all subsequent stages (image generation, TTS, rendering).

This is a structurally critical sprint: the chapter JSON schema becomes the contract between CHAPTERIZE and all downstream stages (IMAGE_GEN, TTS, RENDER). Getting the schema right is essential.

### Sprint 6 Focus (from MASTERPLAN.md §4 Phase 3, §5D)

1. Define and document the chapter JSON schema (§5D) with `schema_version` field and strict validation.
2. Create the chapterization prompt template (`btcedu/prompts/templates/chapterize.md`) with YAML frontmatter and instructions to produce structured chapter JSON.
3. Implement `chapterize_script()` in `btcedu/core/chapterizer.py` — call Claude with the chapterization prompt, receive chapter JSON, validate against schema, save to file.
4. Implement duration estimation from word count (Turkish ~150 words/min per §5D).
5. Implement visual type classification system: `title_card`, `diagram`, `b_roll`, `talking_head`, `screen_share`.
6. Add `chapterize` CLI command to `btcedu/cli.py` with `--force` and `--dry-run`.
7. Integrate into pipeline — CHAPTERIZE after ADAPTED + Review Gate 2 approval.
8. Create a chapter viewer in the dashboard (timeline view showing chapter structure, narration previews, visual types).
9. Implement JSON schema validation for the chapter output (to catch malformed LLM output).
10. Provenance, idempotency, cascade invalidation.
11. Write tests.

### Relevant Subplans

- **Subplan 5D** (Chapterized Production JSON) — all slices: core chapterizer, duration estimation, visual type classification, CLI + pipeline integration, chapter viewer in dashboard.
- **§8** (Idempotency) — CHAPTERIZE stage specifics: already done = `chapters.json` exists AND input hash matches AND prompt hash matches.
- **§3.6** (Provenance Model) — provenance JSON format.
- **§5D Schema Versioning Rule** — `schema_version` field required; minor versions additive, major versions breaking.

---

## Your Task

Produce a detailed implementation plan for Sprint 6. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **Chapter JSON Schema Definition** — the complete schema from MASTERPLAN.md §5D, including:
   - Top-level fields: `schema_version`, `episode_id`, `title`, `total_chapters`, `estimated_duration_seconds`, `chapters` array
   - Per-chapter fields: `chapter_id`, `title`, `order`, `narration` (text + word_count + estimated_duration_seconds), `visual` (type + description + image_prompt), `overlays` array (type + text + start_offset + duration), `transitions` (in + out), `notes`
   - Schema versioning: how `schema_version` works, compatibility rules
   - Propose implementing schema validation using either Pydantic models or JSON Schema — recommend the approach that fits existing codebase patterns
4. **Chapterization Prompt Template** — full draft of `chapterize.md` including:
   - YAML frontmatter: name, model, temperature, max_tokens, description
   - System section: video production editor for Turkish Bitcoin educational content
   - Instructions to decompose the adapted script into chapters
   - Expected output: valid JSON matching the schema
   - Guidance on chapter count (6-10 per ~15 min episode), duration balance, visual type selection
   - Constraints: no hallucinated content, no added financial advice, preserve all adapted content
5. **Chapterizer Module Design** — `chapterize_script()` function signature, return type (`ChapterizationResult` dataclass), processing logic:
   - Load adapted script from `data/outputs/{ep_id}/script.adapted.tr.md`
   - Call Claude to produce chapter JSON
   - Validate JSON against schema (fail if invalid)
   - Compute duration estimates from word counts
   - Save validated JSON to `data/outputs/{ep_id}/chapters.json`
6. **Duration Estimation** — algorithm for estimating chapter and total duration from Turkish word count (~150 words/min). Where this is applied: by the LLM during generation, validated/overridden by the chapterizer module.
7. **Visual Type Classification** — the 5 visual types (`title_card`, `diagram`, `b_roll`, `talking_head`, `screen_share`) and guidance on when each is used. How the LLM selects visual types.
8. **Schema Validation** — how to validate the LLM's JSON output:
   - Check all required fields present
   - Check types correct
   - Check chapter_id uniqueness
   - Check order is sequential
   - Check schema_version is recognized
   - Handle validation failures (retry once? fail with descriptive error?)
9. **CLI Command Design** — `btcedu chapterize <episode_id>` with `--force` and `--dry-run`.
10. **Pipeline Integration** — CHAPTERIZE after ADAPTED + Review Gate 2 approval, before IMAGE_GEN.
11. **Dashboard Chapter Viewer** — design for displaying chapters in the episode detail or a dedicated view:
    - Timeline-like layout showing chapters in order
    - Per chapter: title, narration preview (first N words), visual type badge, estimated duration, overlay count
    - Total duration shown
    - This is a read-only view (no editing)
12. **Provenance, Idempotency, Cascade Invalidation** — provenance JSON, idempotency checks, cascade from adaptation re-run.
13. **Test Plan** — list each test, what it asserts, file it belongs to.
14. **Implementation Order** — numbered sequence.
15. **Definition of Done** — checklist.
16. **Non-Goals** — explicit list.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected. CHAPTERIZE only runs for v2 episodes.
- **Schema is the contract**: The chapter JSON schema defined in this sprint will be consumed by IMAGE_GEN (Sprint 7), TTS (Sprint 8), and RENDER (Sprint 9-10). Design it carefully. Changes to the schema after this sprint are costly.
- **Follow existing patterns**: The chapterizer should mirror the adapter/translator/corrector module pattern.
- **Strict JSON output**: The LLM must produce valid JSON matching the schema. The chapterizer MUST validate before accepting.
- **No image generation**: Sprint 6 defines the schema and visual types but does NOT generate images. IMAGE_GEN is Sprint 7.
- **No review gate after chapterization**: Per the master plan, there is no explicit review gate between CHAPTERIZE and IMAGE_GEN. The chapter viewer is informational, not a blocking gate. [ASSUMPTION: No review gate here unless the master plan explicitly requires one. The three review gates are after CORRECT, ADAPT, and RENDER.]
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include the full chapter JSON schema, prompt template draft, function signatures, validation logic, and dashboard UI description.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The chapter JSON schema is the most important deliverable of this sprint. Take extra care to define it completely and correctly, matching §5D exactly.
- Consider using Pydantic models for schema validation if the existing codebase already uses Pydantic (it does — Pydantic Settings are used for config). This would give type-safe validation and good error messages.
- For the dashboard chapter viewer, a simple ordered list/table showing chapter info is sufficient. No timeline visualization framework needed.
- The `image_prompt` field in the visual section should be `null` for visual types that don't need generated images (e.g., `title_card` can use a template). The IMAGE_GEN stage (Sprint 7) will populate image prompts where needed.
- Consider what happens when the LLM produces malformed JSON — the chapterizer should either retry once with a corrective prompt, or fail with a clear validation error. Recommend one approach and justify it.
