# Sprint 6 — Validation Prompt (Chapterized Production JSON)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 6 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–5 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 6 (Phase 3, Part 1: Chapterized Production JSON)** implementation of the btcedu video production pipeline.

Sprint 6 was scoped to:
- Define the chapter JSON schema (Pydantic models matching §5D)
- Create the chapterization prompt template (`chapterize.md`)
- Implement `chapterize_script()` in `btcedu/core/chapterizer.py`
- Implement JSON schema validation with retry on malformed output
- Implement duration estimation from word count (~150 words/min Turkish)
- Implement visual type classification (5 types)
- Add `chapterize` CLI command with `--force` and `--dry-run`
- Integrate CHAPTERIZE stage into v2 pipeline after RG2 approval
- Create dashboard chapter viewer (read-only timeline/list view)
- Provenance, idempotency, cascade invalidation
- Write tests

Sprint 6 was NOT scoped to include: image generation, TTS, rendering, review gate after chapterization, chapter editing, media_assets table.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Chapter JSON Schema

- [ ] **1.1** Schema definition exists (Pydantic models or equivalent) in a dedicated module
- [ ] **1.2** Top-level `ChapterDocument` model has: `schema_version`, `episode_id`, `title`, `total_chapters`, `estimated_duration_seconds`, `chapters` (list)
- [ ] **1.3** `schema_version` field exists and defaults to "1.0"
- [ ] **1.4** Per-chapter model has: `chapter_id`, `title`, `order`, `narration`, `visual`, `overlays`, `transitions`, `notes`
- [ ] **1.5** `narration` sub-model has: `text`, `word_count`, `estimated_duration_seconds`
- [ ] **1.6** `visual` sub-model has: `type` (enum of 5 types), `description`, `image_prompt` (nullable)
- [ ] **1.7** Visual types include all 5: `title_card`, `diagram`, `b_roll`, `talking_head`, `screen_share`
- [ ] **1.8** `overlays` is a list with entries containing: `type` (lower_third | full_screen | bullet_list | highlight), `text`, `start_offset_seconds`, `duration_seconds`
- [ ] **1.9** `transitions` sub-model has: `in` (fade | cut | slide), `out` (fade | cut | slide)
- [ ] **1.10** Schema matches MASTERPLAN.md §5D — no missing fields, no extra fields beyond what is specified
- [ ] **1.11** Schema validation rejects invalid JSON with descriptive error messages
- [ ] **1.12** Schema validates chapter_id uniqueness
- [ ] **1.13** Schema validates sequential order

### 2. Chapterization Prompt Template

- [ ] **2.1** `btcedu/prompts/templates/chapterize.md` exists
- [ ] **2.2** Has valid YAML frontmatter with: name (`chapterize`), model, temperature, max_tokens, description, author
- [ ] **2.3** System section instructs LLM as a video production editor for Turkish Bitcoin educational content
- [ ] **2.4** Instructions specify chapter count guidance (6-10 per ~15 min episode)
- [ ] **2.5** Instructions include duration guidance (~150 words/min Turkish)
- [ ] **2.6** Instructions include visual type selection guidance
- [ ] **2.7** Instructions require output as valid JSON matching the schema
- [ ] **2.8** Instructions require engaging intro/hook and clear conclusion/CTA
- [ ] **2.9** Constraints: no hallucinated content, no financial advice, preserve all adapted content
- [ ] **2.10** Input variable `{{ adapted_script }}` is used
- [ ] **2.11** Output format clearly specifies pure JSON (no markdown fences)

### 3. Chapterizer Module

- [ ] **3.1** `btcedu/core/chapterizer.py` exists
- [ ] **3.2** `chapterize_script()` function has correct signature matching existing stage patterns
- [ ] **3.3** Function returns a structured result with: chapter_document, raw_json, provenance, cost, chapter_count, total_duration
- [ ] **3.4** Reads adapted script from `data/outputs/{ep_id}/script.adapted.tr.md`
- [ ] **3.5** Writes validated chapters to `data/outputs/{ep_id}/chapters.json`
- [ ] **3.6** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **3.7** Uses existing Claude API calling pattern
- [ ] **3.8** Loads prompt via PromptRegistry (not hardcoded)
- [ ] **3.9** Pre-condition check: verifies ADAPTED status and RG2 approval
- [ ] **3.10** JSON parsing: strips markdown code fences before `json.loads()`
- [ ] **3.11** Schema validation: validates parsed JSON against Pydantic models before saving
- [ ] **3.12** Rejects invalid JSON with descriptive validation errors (does not silently accept)

### 4. JSON Retry Logic

- [ ] **4.1** On JSON parse failure: attempts one retry with corrective prompt
- [ ] **4.2** Retry prompt clearly requests valid JSON only
- [ ] **4.3** On second failure: aborts with clear error (does not infinite-retry)
- [ ] **4.4** Retry count tracked in provenance or logging
- [ ] **4.5** Both API calls (initial + retry) have costs tracked

### 5. Duration Estimation

- [ ] **5.1** Word count computed from actual narration text for each chapter
- [ ] **5.2** Duration estimated at ~150 words/min for Turkish speech
- [ ] **5.3** LLM-provided estimates are validated against word-count-based calculation
- [ ] **5.4** Significant deviations (>20%) are overridden with computed values (or flagged)
- [ ] **5.5** Total `estimated_duration_seconds` is the sum of chapter durations
- [ ] **5.6** Duration values are realistic (not 0, not negative, not unreasonably large)

### 6. Visual Type Classification

- [ ] **6.1** All 5 visual types are defined: `title_card`, `diagram`, `b_roll`, `talking_head`, `screen_share`
- [ ] **6.2** Visual type is validated against the enum (rejects unknown types)
- [ ] **6.3** `image_prompt` is nullable and null for types that don't need custom images (title_card, talking_head)
- [ ] **6.4** `image_prompt` is populated for types that need generation (diagram, b_roll, screen_share)

### 7. Provenance

- [ ] **7.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/chapterize_provenance.json`
- [ ] **7.2** Provenance format matches MASTERPLAN.md §3.6
- [ ] **7.3** Provenance records input file (adapted script path)
- [ ] **7.4** Prompt hash matches PromptVersion record

### 8. Idempotency

- [ ] **8.1** Second run without `--force` skips chapterization
- [ ] **8.2** Idempotency check verifies: output file exists AND prompt hash matches AND input content hash matches
- [ ] **8.3** `--force` flag bypasses idempotency check
- [ ] **8.4** `.stale` marker is respected
- [ ] **8.5** Content hashes use SHA-256

### 9. Cascade Invalidation

- [ ] **9.1** Adaptation re-run marks chapterization output as stale
- [ ] **9.2** Chain propagation works: correction → translation → adaptation → chapterization
- [ ] **9.3** `.stale` marker includes invalidation metadata
- [ ] **9.4** Future stages (IMAGE_GEN, TTS) will be invalidated by chapterization re-run (documented, even if not yet implemented)

### 10. CLI Command

- [ ] **10.1** `btcedu chapterize <episode_id>` command exists and is registered
- [ ] **10.2** `--force` flag works
- [ ] **10.3** `--dry-run` flag works
- [ ] **10.4** `btcedu chapterize --help` shows useful help text
- [ ] **10.5** Command validates episode exists and is ADAPTED with RG2 approved
- [ ] **10.6** On success: episode status updated to CHAPTERIZED
- [ ] **10.7** On success: outputs chapter summary (count, total duration, visual types)
- [ ] **10.8** On failure (including JSON validation): episode status unchanged, error logged
- [ ] **10.9** On JSON validation failure: specific validation errors shown to user

### 11. Pipeline Integration

- [ ] **11.1** CHAPTERIZE is in `PipelineStage` enum
- [ ] **11.2** `resolve_pipeline_plan()` includes CHAPTERIZE for v2 episodes
- [ ] **11.3** CHAPTERIZE positioned after ADAPTED + RG2 approval
- [ ] **11.4** No review gate added after CHAPTERIZE
- [ ] **11.5** Pipeline checks for approved ReviewTask (stage="adapt") before executing CHAPTERIZE
- [ ] **11.6** v1 pipeline is completely unaffected

### 12. Dashboard Chapter Viewer

- [ ] **12.1** Chapter viewer route exists (dedicated route or integrated into episode detail)
- [ ] **12.2** Shows episode title and total estimated duration
- [ ] **12.3** Lists chapters in order with: title, order number, visual type badge, narration preview, duration, overlay count
- [ ] **12.4** Visual types displayed with distinguishing visual treatment (badges, colors, or icons)
- [ ] **12.5** Duration formatted readably (mm:ss or similar)
- [ ] **12.6** Narration text properly truncated with "..." for long text
- [ ] **12.7** Chapter statistics shown (total count, duration, visual type distribution)
- [ ] **12.8** Handles edge case: episode with no chapters.json (shows appropriate message)
- [ ] **12.9** Link from episode detail page to chapter view
- [ ] **12.10** Read-only view (no editing capability)
- [ ] **12.11** Turkish text properly escaped (XSS prevention)
- [ ] **12.12** Follows existing dashboard template and styling patterns

### 13. V1 Pipeline + Phase 1-2 Compatibility (Regression)

- [ ] **13.1** `btcedu status` still works for existing episodes
- [ ] **13.2** v1 pipeline stages are unmodified
- [ ] **13.3** Correction stage and Review Gate 1 still work correctly
- [ ] **13.4** Translation stage still works correctly
- [ ] **13.5** Adaptation stage and Review Gate 2 still work correctly
- [ ] **13.6** Existing dashboard pages still function
- [ ] **13.7** Existing tests still pass
- [ ] **13.8** No existing CLI commands are broken

### 14. Test Coverage

- [ ] **14.1** Schema validation tests: valid JSON passes, invalid JSON fails with descriptive errors
- [ ] **14.2** Schema validation tests: missing required fields, wrong types, duplicate chapter_ids, non-sequential order
- [ ] **14.3** Duration estimation tests: word count → duration calculation
- [ ] **14.4** JSON parsing tests: markdown fence stripping, plain JSON, invalid JSON
- [ ] **14.5** Retry logic tests: first attempt fails, second succeeds; both attempts fail
- [ ] **14.6** Pre-condition check tests: fails if RG2 not approved
- [ ] **14.7** Idempotency tests: second run skips
- [ ] **14.8** Force tests: `--force` re-runs
- [ ] **14.9** CLI tests: command registration, help text
- [ ] **14.10** Pipeline tests: CHAPTERIZE in v2 plan after RG2 approval
- [ ] **14.11** Dashboard tests: chapter viewer renders correctly
- [ ] **14.12** Tests use mocked Claude API calls
- [ ] **14.13** All tests pass with `pytest tests/`

### 15. Scope Creep Detection

- [ ] **15.1** No image generation was implemented
- [ ] **15.2** No `media_assets` table was created
- [ ] **15.3** No `image_gen_service` was created
- [ ] **15.4** No TTS integration was implemented
- [ ] **15.5** No video rendering was implemented
- [ ] **15.6** No review gate was added after chapterization
- [ ] **15.7** No chapter editing was implemented in the dashboard
- [ ] **15.8** No existing stages were modified beyond pipeline integration
- [ ] **15.9** No unnecessary dependencies were added
- [ ] **15.10** Schema has no extraneous fields beyond MASTERPLAN.md §5D

### 16. Schema Contract Validation (Critical for Downstream Stages)

- [ ] **16.1** `chapters.json` output can be parsed by a downstream consumer using the same Pydantic models
- [ ] **16.2** `schema_version` is present and set to "1.0"
- [ ] **16.3** `image_prompt` field correctly nullable — null for title_card/talking_head, populated for diagram/b_roll/screen_share
- [ ] **16.4** `estimated_duration_seconds` values are consistent (chapter durations sum ≈ total duration)
- [ ] **16.5** JSON is written with `indent=2` and `ensure_ascii=False`
- [ ] **16.6** Schema is documented (either in a README, inline docstrings, or the schema module itself)

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 6 is complete and ready for Sprint 7 (Image Generation). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 7 after fixes. |
| **FAIL** | Critical issues found. Sprint 6 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Schema Readiness Assessment:

The chapter JSON schema is the contract for all downstream stages. Verify:
- [ ] Schema is complete and matches §5D
- [ ] Schema validation is strict (rejects invalid output)
- [ ] Schema is reusable — IMAGE_GEN (Sprint 7), TTS (Sprint 8), and RENDER (Sprint 9-10) can import and use the same Pydantic models
- [ ] `schema_version` versioning rule is implemented (minor = additive, major = breaking)
- [ ] Schema documentation exists for downstream stage implementers

If the schema is not ready, this is a blocking issue for all subsequent sprints.

### Deferred Items Acknowledged:

- Image generation / IMAGE_GEN stage (Sprint 7)
- `media_assets` table (Sprint 7)
- `image_gen_service` (Sprint 7)
- TTS integration (Sprint 8)
- Video assembly / Render pipeline (Sprint 9-10)
- Review Gate 3 / video review (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Chapter editing in dashboard
- Thumbnail generation
- Background music integration

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 1 (Schema)** and **Section 16 (Schema Contract)** — the chapter JSON schema is the foundation for all remaining sprints. An incorrect or incomplete schema will cascade into problems in Sprints 7-11.
- **Pay attention to Section 4 (JSON Retry)** — LLMs frequently produce invalid JSON. The retry mechanism is important for robustness.
- Verify that duration estimates are realistic and consistent across chapters.
- Check that the dashboard chapter viewer provides enough information for the content owner to verify the chapter structure before proceeding to image generation.
