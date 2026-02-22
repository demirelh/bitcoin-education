# Sprint 2 — Planning Prompt (Transcript Correction Stage)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1 implementation (completed), current codebase (especially `btcedu/core/generator.py` for `call_claude()` patterns, `btcedu/core/pipeline.py`, `btcedu/prompts/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the correction module, prompt template, diff computation, CLI command, pipeline integration, provenance, idempotency, and tests.

---

## Context

You are planning **Sprint 2 (Phase 1, Part 1: Transcript Correction Stage)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprint 1 (Foundation) is complete — the `EpisodeStatus` enum has new values, `prompt_versions` and `review_tasks` tables exist, `PromptRegistry` is functional, and `pipeline_version` is in config.

Sprint 2 implements the **CORRECT** stage — the first new pipeline stage in the v2 pipeline. It does NOT include the review UI (that's Sprint 3). It focuses on the backend: correction logic, diff generation, CLI command, pipeline integration, and provenance tracking.

### Sprint 2 Focus (from MASTERPLAN.md §4 Phase 1 and §12 Sprint 2)

1. Create the correction prompt template (`btcedu/prompts/templates/correct_transcript.md`) with YAML frontmatter and Jinja2 variables.
2. Implement `correct_transcript()` in `btcedu/core/corrector.py` — call Claude with the correction prompt, receive corrected text, save to file.
3. Implement structured diff computation — compare original and corrected transcripts, produce `correction_diff.json` (format in §5A).
4. Add `correct` CLI command to `btcedu/cli.py` — `btcedu correct <episode_id>` with `--force` and `--dry-run` flags.
5. Integrate into pipeline — extend `PipelineStage` enum, update `resolve_pipeline_plan()` so the v2 pipeline includes CORRECT after TRANSCRIBED.
6. Register the correction prompt version via `PromptRegistry` on first use.
7. Store provenance JSON (format in §3.6) after each correction run.
8. Implement idempotency checks (§8, CORRECT Stage): skip if output exists AND prompt hash matches AND input content hash matches.
9. Write tests: unit (diff computation), integration (correction with dry-run), CLI (`--help`), idempotency.

### Relevant Subplans

- **Subplan 5A** (Transcript Correction + Diff Review) — slices 1–3 (core corrector, diff computation, CLI + pipeline integration). Slices 4–5 (dashboard diff viewer, review gate) are deferred to Sprint 3.
- **§8** (Idempotency) — CORRECT stage specifics.
- **§3.6** (Provenance Model) — provenance JSON format.

---

## Your Task

Produce a detailed implementation plan for Sprint 2. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **Correction Prompt Template** — full draft of `correct_transcript.md` including YAML frontmatter, system instructions, input variable (`{{ transcript }}`), and output format specification. Follow the existing system prompt constraints from `btcedu/prompts/system.py` for the German Bitcoin/crypto context.
4. **Corrector Module Design** — `correct_transcript()` function signature, return type (`CorrectionResult` dataclass), error handling, long-transcript segmentation strategy (>30min transcripts split at paragraph breaks).
5. **Diff Computation** — algorithm for producing the structured diff JSON (§5A format). Specify whether to use Python's `difflib` or a custom approach. The diff must include: change type (replace/insert/delete), original text, corrected text, context, position, and category (spelling/punctuation/grammar).
6. **CLI Command Design** — Click command signature, options (`--force`, `--dry-run`), output messages, error handling.
7. **Pipeline Integration** — how `resolve_pipeline_plan()` changes to include CORRECT for v2 episodes. How the pipeline checks `pipeline_version` to decide v1 vs v2 flow.
8. **Provenance and Idempotency** — exact provenance JSON fields, idempotency check logic (file exists + prompt hash + input hash), `.stale` marker handling.
9. **Test Plan** — list each test function, what it asserts, and which file it belongs to.
10. **Implementation Order** — numbered sequence of steps.
11. **Definition of Done** — checklist.
12. **Non-Goals** — explicit list of what Sprint 2 does NOT include.

---

## Constraints

- **Backward compatibility**: v1 pipeline (`pipeline_version=1`) must not be affected. The CORRECT stage only runs for v2 episodes.
- **Additive changes only**: No modification to existing pipeline stages.
- **Follow existing patterns**: Study `btcedu/core/generator.py` for `call_claude()` usage, cost tracking, and dry-run patterns. Study `btcedu/core/transcriber.py` for file I/O patterns.
- **Use existing services**: Use `btcedu/services/claude_service.py` (or the existing `call_claude()` function) for Claude API calls.
- **Use the PromptRegistry** built in Sprint 1 to load and register the correction prompt.
- **No UI changes**: Dashboard diff viewer and review gate are deferred to Sprint 3.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include code snippets for the prompt template, function signatures, diff format, and provenance format.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The correction prompt should instruct the LLM to correct Whisper transcription errors in German Bitcoin/crypto content without adding or removing information.
- For diff computation, prefer a solution that produces human-readable diffs with category classification. If automatic category classification is complex, start with a simpler "change type" classification and label it as `[SIMPLIFICATION]`.
- Long transcripts (>30 minutes / >15,000 characters) should be processed in segments aligned with paragraph breaks, with results reassembled.
