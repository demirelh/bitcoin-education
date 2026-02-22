# Sprint 2 — Implementation Prompt (Transcript Correction Stage)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 2 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1 completed codebase
> - **Expected output**: All code changes (new files, modified files), correction prompt template, tests — committed and passing.

---

## Context

You are implementing **Sprint 2 (Phase 1, Part 1: Transcript Correction Stage)** of the btcedu video production pipeline.

Sprint 1 (Foundation) is complete: new `EpisodeStatus` values exist, `PromptVersion`/`ReviewTask`/`ReviewDecision` models exist, `PromptRegistry` works, `pipeline_version` is in config.

Sprint 2 adds the first new pipeline stage — **CORRECT** — which takes a raw Whisper transcript and produces a corrected version with a structured diff.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 2 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/generator.py` (for `call_claude()` pattern), `btcedu/core/pipeline.py` (for stage enum and plan resolution), `btcedu/cli.py` (for CLI command patterns), `btcedu/services/claude_service.py`, `btcedu/core/prompt_registry.py`.
2. **Create the correction prompt template** — `btcedu/prompts/templates/correct_transcript.md` with:
   - YAML frontmatter: name, model, temperature (0.2), max_tokens (8192), description, author
   - System section: German transcript editor specializing in Bitcoin/crypto content
   - Instructions: correct spelling (especially technical terms), punctuation, sentence boundaries, speaker attribution
   - Hard constraints: do NOT add information, do NOT change meaning, do NOT translate
   - Input variable: `{{ transcript }}`
   - Output format: corrected plain text
3. **Implement CorrectionResult dataclass** — include: corrected_text, diff_data, provenance, cost, token counts.
4. **Implement `correct_transcript()`** in `btcedu/core/corrector.py`:
   - Load prompt template via PromptRegistry
   - Read input transcript file
   - Check idempotency (output exists + prompt hash match + input hash match)
   - For long transcripts: split at paragraph breaks, process segments, reassemble
   - Call Claude via existing service/pattern
   - Save corrected text to `data/transcripts/{ep_id}/transcript.corrected.de.txt`
   - Compute diff and save to `data/outputs/{ep_id}/review/correction_diff.json`
   - Save provenance to `data/outputs/{ep_id}/provenance/correct_provenance.json`
   - Register/record prompt version
   - Return CorrectionResult
5. **Implement diff computation** — produce structured JSON diff (§5A format from MASTERPLAN.md):
   - Use `difflib.SequenceMatcher` or similar for change detection
   - Classify changes by type: replace, insert, delete
   - Include context around each change
   - Produce summary with total changes and breakdown by category
6. **Add `correct` CLI command** to `btcedu/cli.py`:
   - `btcedu correct <episode_id>` with `--force` and `--dry-run` flags
   - Validate episode exists and is at TRANSCRIBED status (or later, for --force)
   - On success: update episode status to CORRECTED
   - On failure: log error, leave status unchanged
7. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
   - Add CORRECT to `PipelineStage` enum (if not already present from Sprint 1)
   - Update `resolve_pipeline_plan()` to include CORRECT for `pipeline_version=2` episodes
   - Position CORRECT after TRANSCRIBED, before TRANSLATED
8. **Write tests**:
   - `tests/test_corrector.py`: unit tests for diff computation, integration test for correction with dry-run, idempotency test
   - CLI test: `btcedu correct --help` works
9. **Verify** — run `pytest tests/` to confirm all tests pass. Confirm `btcedu status` still works for v1 episodes.

### Anti-scope-creep guardrails

- **Do NOT** implement the dashboard diff viewer (Sprint 3).
- **Do NOT** implement the review gate / approval flow (Sprint 3).
- **Do NOT** implement the TRANSLATE or ADAPT stages.
- **Do NOT** add review queue UI or API endpoints.
- **Do NOT** modify existing prompt Python modules.
- **Do NOT** modify existing pipeline stages (detect, download, transcribe, chunk, generate, refine).
- **Do NOT** add unnecessary error handling beyond what the existing patterns use.
- **Do NOT** add new dependencies unless strictly required.

### Code patterns to follow

- **Claude API calls**: Follow the pattern in `btcedu/core/generator.py` — look for `call_claude()` or the Anthropic client usage pattern. Reuse the same cost tracking, token counting, and error handling.
- **File I/O**: Follow the pattern in `btcedu/core/transcriber.py` for reading/writing transcript files. Use `Path` objects, create directories with `mkdir(parents=True, exist_ok=True)`.
- **CLI commands**: Follow the Click command pattern in `btcedu/cli.py` — context passing, session management, settings access.
- **Provenance**: Follow the format in MASTERPLAN.md §3.6 exactly.
- **Idempotency**: Follow the strategy in MASTERPLAN.md §8 — check file existence, prompt hash, input content hash.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred to Sprint 3 (diff viewer, review gate, dashboard)
- Manual verification steps:
  - Pick an existing transcribed episode
  - Run `btcedu correct <ep_id> --dry-run`
  - Run `btcedu correct <ep_id>`
  - Verify output files exist at expected paths
  - Run again → verify skipped (idempotent)
  - Run with `--force` → verify re-runs
  - Run `btcedu status` → verify v1 pipeline unaffected

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- SHA-256 hashes for idempotency should use `hashlib.sha256` on file content bytes.
- Provenance JSON should be written with `json.dump()` using `indent=2` and `ensure_ascii=False` (Turkish/German characters).

---

## Definition of Done

- [ ] `btcedu/prompts/templates/correct_transcript.md` exists with valid YAML frontmatter and correction instructions
- [ ] `btcedu/core/corrector.py` exists with `correct_transcript()` function
- [ ] Corrector produces `transcript.corrected.de.txt` at the correct path
- [ ] Corrector produces `correction_diff.json` at the correct path
- [ ] Corrector produces `correct_provenance.json` at the correct path
- [ ] Diff JSON matches the format in MASTERPLAN.md §5A (changes array with type/original/corrected/context/position/category + summary)
- [ ] `btcedu correct <episode_id>` CLI command works with `--force` and `--dry-run`
- [ ] Pipeline plan includes CORRECT for v2 episodes
- [ ] Episode status updated to CORRECTED on success
- [ ] Idempotency works: second run skips, `--force` re-runs
- [ ] Prompt version registered in DB via PromptRegistry
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- Dashboard diff viewer UI (Sprint 3)
- Review gate / approval-rejection flow (Sprint 3)
- Review queue API endpoints (Sprint 3)
- TRANSLATE stage (Sprint 4)
- ADAPT stage (Sprint 4-5)
- Auto-approve rules (later sprint)
- Long transcript segmentation optimization (can be improved later)
