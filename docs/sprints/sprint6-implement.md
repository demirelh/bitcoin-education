# Sprint 6 — Implementation Prompt (Chapterized Production JSON)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 6 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–5 completed codebase
> - **Expected output**: All code changes (new files, modified files), chapter JSON schema definition, chapterization prompt template, dashboard chapter viewer, tests — committed and passing.

---

## Context

You are implementing **Sprint 6 (Phase 3, Part 1: Chapterized Production JSON)** of the btcedu video production pipeline.

Sprints 1–5 (Phases 0–2) are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: corrector module, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: reviewer module, dashboard review queue + diff viewer, approve/reject/request-changes.
- Translation: translator module, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: adapter module, Turkey-context adaptation with tiered rules, adaptation diff, ADAPT stage, Review Gate 2.

Sprint 6 adds the **CHAPTERIZE** stage — decomposing the adapted script into structured production chapters. The chapter JSON becomes the contract for all downstream stages (IMAGE_GEN, TTS, RENDER). This sprint also adds a chapter viewer to the dashboard.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 6 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/adapter.py` (stage pattern to follow), `btcedu/core/pipeline.py`, `btcedu/core/reviewer.py`, `btcedu/cli.py`, `btcedu/config.py` (Pydantic patterns), `btcedu/web/` (template patterns), `btcedu/models/episode.py`.

2. **Define the chapter JSON schema** — create Pydantic models (or a JSON schema definition) for chapter validation. Create `btcedu/core/chapter_schema.py` (or similar) with:
   - `ChapterOverlay` model: type (lower_third | full_screen | bullet_list | highlight), text, start_offset_seconds, duration_seconds
   - `ChapterTransitions` model: in (fade | cut | slide), out (fade | cut | slide)
   - `ChapterNarration` model: text, word_count, estimated_duration_seconds
   - `ChapterVisual` model: type (title_card | diagram | b_roll | talking_head | screen_share), description, image_prompt (optional/nullable)
   - `Chapter` model: chapter_id, title, order, narration (ChapterNarration), visual (ChapterVisual), overlays (list of ChapterOverlay), transitions (ChapterTransitions), notes (optional)
   - `ChapterDocument` model: schema_version (default "1.0"), episode_id, title, total_chapters, estimated_duration_seconds, chapters (list of Chapter)
   - Validation: chapter_id uniqueness, sequential order, word_count matches actual text word count (±5%), schema_version is recognized
   - Use Pydantic v2 if the project uses it (check `btcedu/config.py`), otherwise match existing patterns

3. **Create the chapterization prompt template** — `btcedu/prompts/templates/chapterize.md` with:
   - YAML frontmatter: name (`chapterize`), model, temperature (0.4), max_tokens (16384), description, author
   - System section: video production editor specializing in Turkish Bitcoin educational content
   - Instructions:
     - Decompose the adapted script into 6-10 chapters for a ~15 minute video
     - Each chapter must have: narration text, visual type, overlays, transitions
     - Duration guidance: Turkish speech at ~150 words/min
     - Chapter structure: start with an engaging intro/hook, end with a clear conclusion/CTA
     - Balance chapter durations (avoid very short or very long chapters)
     - Select appropriate visual types per chapter content
     - Generate `image_prompt` only for chapters that need custom images (diagrams, b_roll); set to null for title_card, talking_head, screen_share
   - Constraints:
     - Output MUST be valid JSON matching the specified schema exactly
     - Do NOT add content not in the adapted script
     - Do NOT add financial advice or opinions
     - Preserve all adapted content — do NOT remove or summarize sections
   - Input variable: `{{ adapted_script }}`
   - Output: complete JSON document (no markdown code fences, pure JSON)

4. **Create ChapterizationResult dataclass** — include: chapter_document (validated), raw_json, provenance, cost, token counts, chapter_count, total_duration_estimate.

5. **Implement `chapterize_script()`** in `btcedu/core/chapterizer.py`:
   - Load prompt template via PromptRegistry
   - Read adapted script from `data/outputs/{ep_id}/script.adapted.tr.md`
   - **Pre-condition check**: Episode status is ADAPTED and Review Gate 2 (stage="adapt") is APPROVED
   - Check idempotency (output exists + prompt hash match + input hash match)
   - Call Claude via existing service/pattern
   - Parse the response as JSON:
     - Strip any markdown code fences if present (```json ... ```)
     - Attempt `json.loads()`
     - On JSON parse failure: attempt one retry with a corrective prompt asking for valid JSON
   - Validate parsed JSON against the Pydantic schema:
     - On validation failure: log the specific validation errors and fail with descriptive message
     - Do NOT silently accept invalid JSON
   - Compute/validate duration estimates:
     - Calculate `word_count` from actual narration text for each chapter
     - Calculate `estimated_duration_seconds` from word count at ~150 words/min
     - Override LLM estimates if they deviate significantly (>20% from word-count-based estimate)
     - Calculate total `estimated_duration_seconds` as sum of chapter durations
   - Save validated JSON to `data/outputs/{ep_id}/chapters.json`
   - Save provenance to `data/outputs/{ep_id}/provenance/chapterize_provenance.json`
   - Register/record prompt version
   - Return ChapterizationResult

6. **Implement JSON retry logic**:
   - If initial Claude response is not valid JSON, send a follow-up prompt:
     "Your previous response was not valid JSON. Please return ONLY a valid JSON document matching the schema, with no additional text or markdown formatting."
   - Include the original response and the parse error in the follow-up
   - If second attempt also fails, abort with a clear error
   - Track retries in provenance (retry_count field)

7. **Add `chapterize` CLI command** to `btcedu/cli.py`:
   - `btcedu chapterize <episode_id>` with `--force` and `--dry-run`
   - Validate episode exists, is at ADAPTED status, and Review Gate 2 is approved
   - On success: update episode status to CHAPTERIZED
   - On failure (including JSON validation failure): log error, leave status unchanged
   - Output chapter summary on success: number of chapters, total duration, visual type distribution

8. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
   - Ensure CHAPTERIZE is in `PipelineStage` enum
   - Update `resolve_pipeline_plan()` to include CHAPTERIZE for v2 episodes after Review Gate 2 approval
   - Position: ADAPTED + RG2 → CHAPTERIZE → IMAGE_GEN (Sprint 7)
   - Pipeline must check for approved ReviewTask (stage="adapt") before executing CHAPTERIZE
   - No review gate after CHAPTERIZE (the chapter viewer is informational)

9. **Create dashboard chapter viewer** — add chapter display to the episode detail view or a dedicated route:
   - New route: `GET /episodes/<ep_id>/chapters` (or extend existing episode detail)
   - Template showing:
     - Episode title and total estimated duration
     - Ordered list of chapters, each showing:
       - Chapter title and order number
       - Visual type as a colored badge (title_card=gray, diagram=blue, b_roll=green, talking_head=purple, screen_share=orange)
       - Narration preview (first 100 characters, truncated with "...")
       - Estimated duration (formatted as mm:ss)
       - Overlay count
       - Notes (if present)
     - Total statistics: chapter count, total duration, visual type distribution
   - Link from episode detail page to chapter view
   - This is read-only — no editing capability

10. **Implement cascade invalidation**:
    - Adaptation re-run → chapterization marked stale
    - Chain: correction → translation → adaptation → chapterization
    - Hook into `invalidate_downstream()` or the adapter's re-run path

11. **Write tests**:
    - `tests/test_chapter_schema.py`:
      - Schema validation: valid JSON passes, invalid JSON fails with descriptive error
      - Required fields: missing field fails validation
      - Type validation: wrong type fails
      - Chapter_id uniqueness: duplicate IDs fail
      - Order sequencing: non-sequential order fails (or is corrected)
      - Word count validation: matches actual text ±5%
      - Schema version: recognized version passes, unknown version fails
    - `tests/test_chapterizer.py`:
      - Unit: duration estimation from word count
      - Unit: JSON parsing with markdown fence stripping
      - Unit: pre-condition check (fails if RG2 not approved)
      - Integration: chapterization with dry-run
      - Idempotency: second run skips
      - Force: `--force` re-runs
      - JSON retry: malformed response triggers retry
    - CLI test: `btcedu chapterize --help` works
    - Pipeline test: CHAPTERIZE included in v2 plan after RG2 approval
    - Dashboard test: chapter viewer renders correctly

12. **Verify**:
    - Run `pytest tests/`
    - Pick an adapted + RG2-approved episode
    - Run `btcedu chapterize <ep_id> --dry-run`
    - Run `btcedu chapterize <ep_id>`
    - Verify `chapters.json` at `data/outputs/{ep_id}/chapters.json`
    - Verify JSON is valid and matches schema (use `python -m json.tool` or Pydantic validation)
    - Verify chapter count, duration estimates, visual types are reasonable
    - Verify provenance at `data/outputs/{ep_id}/provenance/chapterize_provenance.json`
    - Open dashboard → navigate to chapter view → verify chapter list renders
    - Run again → verify skipped (idempotent)
    - Run with `--force` → verify re-runs
    - Run `btcedu status` → verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement image generation (that's Sprint 7).
- **Do NOT** implement the `image_gen_service` or call any image generation API.
- **Do NOT** implement TTS (Sprint 8).
- **Do NOT** implement video rendering (Sprint 9-10).
- **Do NOT** add a review gate after chapterization (there is none per the master plan).
- **Do NOT** implement chapter editing in the dashboard (read-only view only).
- **Do NOT** implement the `media_assets` table (that's Sprint 7 when images are generated).
- **Do NOT** modify existing stages (correct, translate, adapt) or review gates.
- **Do NOT** over-engineer the schema — implement exactly what §5D specifies, no additional fields.
- **Do NOT** add new external dependencies for JSON schema validation — use Pydantic (already in the project).

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/adapter.py` closely — same file I/O, Claude API, provenance, idempotency patterns.
- **Pydantic models**: Follow `btcedu/config.py` for Pydantic patterns. Use the same Pydantic version.
- **Dashboard**: Follow existing template and route patterns in `btcedu/web/`.
- **CLI commands**: Follow existing Click command patterns.
- **JSON handling**: Use `json.loads()`/`json.dumps()` with `indent=2` and `ensure_ascii=False`.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred
- Manual verification steps:
  - Pick an adapted, RG2-approved episode
  - Run `btcedu chapterize <ep_id>`
  - Verify chapters.json at expected path
  - Validate JSON (schema, duration, visual types)
  - View chapters in dashboard
  - Test idempotency and --force
  - Verify v1 pipeline unaffected

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- The chapter JSON schema is critical — it is the contract for IMAGE_GEN, TTS, and RENDER. Validate strictly.
- Use `ensure_ascii=False` for Turkish characters in JSON output.
- The chapterizer MUST strip markdown code fences from the LLM response before JSON parsing. LLMs commonly wrap JSON in ```json ``` blocks.
- Duration estimates should be realistic: ~150 words/min for Turkish speech.

---

## Definition of Done

- [ ] Chapter JSON schema defined as Pydantic models in `btcedu/core/chapter_schema.py` (or equivalent)
- [ ] Schema matches MASTERPLAN.md §5D exactly (all fields, types, nesting)
- [ ] `schema_version` field present and validated
- [ ] `btcedu/prompts/templates/chapterize.md` exists with valid YAML frontmatter and structured chapterization instructions
- [ ] `btcedu/core/chapterizer.py` exists with `chapterize_script()` function
- [ ] Chapterizer produces validated `chapters.json` at `data/outputs/{ep_id}/chapters.json`
- [ ] Chapterizer validates JSON against schema before saving (rejects invalid output)
- [ ] JSON retry logic handles malformed LLM responses (one retry)
- [ ] Duration estimates computed from word count at ~150 words/min
- [ ] Visual types from the 5-type classification are used
- [ ] `btcedu chapterize <episode_id>` CLI works with `--force` and `--dry-run`
- [ ] Pipeline plan includes CHAPTERIZE for v2 episodes after RG2 approval
- [ ] Episode status updated to CHAPTERIZED on success
- [ ] Pre-condition check: fails if RG2 not approved
- [ ] Idempotency works: second run skips, `--force` re-runs
- [ ] Cascade invalidation: adaptation re-run marks chapterization stale
- [ ] Provenance JSON stored
- [ ] Dashboard chapter viewer shows chapter list with visual types, durations, narration previews
- [ ] All tests pass (schema validation, chapterizer, CLI, pipeline, dashboard)
- [ ] v1 pipeline unaffected

## Non-Goals

- Image generation / IMAGE_GEN stage (Sprint 7)
- `media_assets` table (Sprint 7)
- `image_gen_service` (Sprint 7)
- TTS integration (Sprint 8)
- Video rendering (Sprint 9-10)
- Review gate after chapterization
- Chapter editing in dashboard
- Image prompt generation for chapters (the `image_prompt` field is populated by the LLM during chapterization, but actual image generation is Sprint 7)
- Thumbnail generation (deferred)
- Background music integration (deferred)
