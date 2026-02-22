# Sprint 7 — Validation Prompt (Image/Video Prompt Generation)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 7 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–6 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 7 (Phase 3, Part 2: Image/Video Prompt Generation)** implementation of the btcedu video production pipeline.

Sprint 7 was scoped to:
- Create `ImageGenService` abstraction with DALL-E 3 implementation
- Create image prompt generation template (`imagegen.md`)
- Implement `generate_images()` in `btcedu/core/image_generator.py`
- Generate images per chapter, filtered by visual type
- Create image manifest at `data/outputs/{ep_id}/images/manifest.json`
- Create `media_assets` table + SQLAlchemy model (Migration N+4)
- Add `imagegen` CLI command with `--force`, `--dry-run`, `--chapter`
- Integrate IMAGE_GEN stage into v2 pipeline after CHAPTERIZED
- Create dashboard image gallery
- Partial regeneration for changed chapters only
- Provenance, idempotency, cascade invalidation, cost tracking
- Write tests

Sprint 7 was NOT scoped to include: TTS, video rendering, Review Gate 3, YouTube publishing, multiple image providers, image editing UI, review gate after image generation.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. `media_assets` Table & Model

- [ ] **1.1** Migration creates `media_assets` table with correct schema per §7.3
- [ ] **1.2** Table has fields: id, episode_id, asset_type, chapter_id, file_path, mime_type, size_bytes, duration_seconds, metadata (JSON), prompt_version_id (FK), created_at
- [ ] **1.3** Index on (episode_id, asset_type, chapter_id) exists
- [ ] **1.4** SQLAlchemy `MediaAsset` model exists and is registered in `__init__.py`
- [ ] **1.5** Migration runs cleanly on existing database (additive, no drops)
- [ ] **1.6** Migration is appended correctly to the migrations list

### 2. Image Generation Service

- [ ] **2.1** `btcedu/services/image_gen_service.py` exists
- [ ] **2.2** `ImageGenService` Protocol/ABC is defined with `generate_image()` method
- [ ] **2.3** `DallE3ImageService` implementation exists
- [ ] **2.4** Uses OpenAI API with existing API key configuration
- [ ] **2.5** Default size is landscape format suitable for video production
- [ ] **2.6** Handles 429 (rate limit) responses with exponential backoff (up to 3 retries)
- [ ] **2.7** Handles content policy rejections gracefully (returns error result, does not raise)
- [ ] **2.8** Handles timeouts gracefully
- [ ] **2.9** Downloads generated image from URL and returns bytes
- [ ] **2.10** Cost tracking per image generation call
- [ ] **2.11** Config values are used (not hardcoded): model, size, quality, style prefix

### 3. Image Prompt Template

- [ ] **3.1** `btcedu/prompts/templates/imagegen.md` exists
- [ ] **3.2** Has valid YAML frontmatter with: name (`imagegen`), model, temperature, max_tokens, description
- [ ] **3.3** Instructions generate a detailed image prompt from chapter visual context
- [ ] **3.4** Includes style consistency guidance
- [ ] **3.5** Input variables include visual description, visual type, chapter title, narration context
- [ ] **3.6** Output is a plain text prompt string (not JSON)
- [ ] **3.7** Constraints: no text in images (DALL-E text rendering unreliable), clean visual style

### 4. Image Generator Module

- [ ] **4.1** `btcedu/core/image_generator.py` exists
- [ ] **4.2** `generate_images()` function has correct signature matching existing stage patterns
- [ ] **4.3** Function returns a structured result (ImageGenResult or similar)
- [ ] **4.4** Reads chapter JSON from `data/outputs/{ep_id}/chapters.json` using Sprint 6 Pydantic models
- [ ] **4.5** **Pre-condition check**: verifies episode is CHAPTERIZED
- [ ] **4.6** Correctly filters chapters by visual type:
  - `title_card` → skipped (template/brand asset)
  - `talking_head` → skipped (template/stock)
  - `diagram` → generated
  - `b_roll` → generated
  - `screen_share` → generated
- [ ] **4.7** Uses `image_prompt` from chapter JSON if populated; generates via LLM if null
- [ ] **4.8** Prepends style prefix from config
- [ ] **4.9** Calls `ImageGenService.generate_image()` for each eligible chapter
- [ ] **4.10** Saves images to `data/outputs/{ep_id}/images/{chapter_id}.png`
- [ ] **4.11** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **4.12** Records each image in `media_assets` table

### 5. Image Manifest

- [ ] **5.1** Manifest saved to `data/outputs/{ep_id}/images/manifest.json`
- [ ] **5.2** Manifest format matches §5E: per-image entries with chapter_id, prompt, model, size, file_path, generated_at
- [ ] **5.3** Manifest includes status for failed/skipped images
- [ ] **5.4** JSON written with `indent=2` and `ensure_ascii=False`

### 6. Error Handling

- [ ] **6.1** Content policy rejection for one chapter does not fail the entire episode
- [ ] **6.2** Rejected images recorded in manifest with appropriate status
- [ ] **6.3** API timeouts handled with retry and logged
- [ ] **6.4** Rate limits handled with exponential backoff
- [ ] **6.5** Partial success is supported (some images generated, some failed)
- [ ] **6.6** Episode status still updated to IMAGES_GENERATED on partial success (with warnings logged)

### 7. Partial Regeneration

- [ ] **7.1** Unchanged chapters are skipped on re-run (images preserved)
- [ ] **7.2** Changed chapters (different hash) are regenerated
- [ ] **7.3** `--force` flag regenerates all images
- [ ] **7.4** `--chapter <chapter_id>` regenerates a single chapter's image
- [ ] **7.5** Manifest is updated correctly after partial regeneration

### 8. Provenance & Idempotency

- [ ] **8.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/imagegen_provenance.json`
- [ ] **8.2** Provenance format matches §3.6
- [ ] **8.3** Second run without `--force` skips chapters with existing unchanged images
- [ ] **8.4** Idempotency checks: image file exists AND chapter hash matches
- [ ] **8.5** `.stale` marker respected
- [ ] **8.6** Content hashes use SHA-256

### 9. Cascade Invalidation

- [ ] **9.1** Chapterization re-run marks image generation as stale
- [ ] **9.2** Chain: correction → translation → adaptation → chapterization → image generation
- [ ] **9.3** `.stale` markers created with invalidation metadata
- [ ] **9.4** RENDER stage (future) will be invalidated by image regeneration (documented)

### 10. Cost Tracking

- [ ] **10.1** Per-image cost tracked
- [ ] **10.2** Cumulative episode cost checked against `max_episode_cost_usd` after each image
- [ ] **10.3** If cost cap exceeded, stage stops with COST_LIMIT error
- [ ] **10.4** Cost recorded in provenance and/or PipelineRun

### 11. CLI Command

- [ ] **11.1** `btcedu imagegen <episode_id>` command exists and is registered
- [ ] **11.2** `--force` flag works
- [ ] **11.3** `--dry-run` flag works
- [ ] **11.4** `--chapter <chapter_id>` flag works (single chapter regeneration)
- [ ] **11.5** `btcedu imagegen --help` shows useful help text
- [ ] **11.6** Command validates episode exists and is CHAPTERIZED
- [ ] **11.7** On success: episode status updated to IMAGES_GENERATED
- [ ] **11.8** Outputs summary: images generated, skipped, failed, total cost

### 12. Pipeline Integration

- [ ] **12.1** IMAGE_GEN / IMAGES_GENERATED is properly wired in `PipelineStage` enum
- [ ] **12.2** `resolve_pipeline_plan()` includes IMAGE_GEN for v2 episodes after CHAPTERIZED
- [ ] **12.3** No review gate added between CHAPTERIZE and IMAGE_GEN
- [ ] **12.4** v1 pipeline is completely unaffected

### 13. Dashboard Image Gallery

- [ ] **13.1** Image gallery route exists
- [ ] **13.2** Shows images per chapter in a grid or list layout
- [ ] **13.3** Each image shows: thumbnail, chapter title, visual type
- [ ] **13.4** Failed/skipped images shown with placeholder and status
- [ ] **13.5** Full-size image viewable on click
- [ ] **13.6** Images served correctly from local filesystem
- [ ] **13.7** Link from episode detail / chapter viewer to image gallery
- [ ] **13.8** Follows existing dashboard template and styling patterns
- [ ] **13.9** Turkish text properly escaped (XSS prevention)

### 14. V1 Pipeline + Previous Sprint Compatibility (Regression)

- [ ] **14.1** `btcedu status` still works for all episodes
- [ ] **14.2** v1 pipeline stages are unmodified
- [ ] **14.3** Correction + Review Gate 1 still work
- [ ] **14.4** Translation stage still works
- [ ] **14.5** Adaptation + Review Gate 2 still work
- [ ] **14.6** Chapterization stage still works
- [ ] **14.7** Chapter JSON schema is consumed unmodified (not redefined)
- [ ] **14.8** Existing dashboard pages still function
- [ ] **14.9** Existing tests still pass
- [ ] **14.10** No existing CLI commands are broken

### 15. Test Coverage

- [ ] **15.1** Image generation service tests: mock API success, rate limit, content rejection, timeout
- [ ] **15.2** Image generator tests: chapter type filtering, prompt generation, partial regeneration
- [ ] **15.3** Idempotency tests: second run skips unchanged chapters
- [ ] **15.4** Force tests: `--force` regenerates all
- [ ] **15.5** Single chapter tests: `--chapter` regenerates only specified chapter
- [ ] **15.6** Error handling tests: rejection continues to next chapter
- [ ] **15.7** Cost cap tests: stops if limit exceeded
- [ ] **15.8** Media asset model tests: CRUD
- [ ] **15.9** CLI tests: command registration, help text
- [ ] **15.10** Pipeline tests: IMAGE_GEN in v2 plan
- [ ] **15.11** Dashboard tests: gallery renders
- [ ] **15.12** All tests use mocked API calls
- [ ] **15.13** All tests pass with `pytest tests/`

### 16. Scope Creep Detection

- [ ] **16.1** No TTS integration was implemented
- [ ] **16.2** No video rendering was implemented
- [ ] **16.3** No Review Gate 3 / video review was implemented
- [ ] **16.4** No YouTube publishing was implemented
- [ ] **16.5** No multiple image generation providers were implemented (only DALL-E 3)
- [ ] **16.6** No image editing UI was implemented
- [ ] **16.7** No review gate was added after image generation
- [ ] **16.8** Chapter JSON schema was not modified
- [ ] **16.9** No existing stages were modified beyond pipeline integration
- [ ] **16.10** No unnecessary dependencies were added

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 7 is complete and ready for Sprint 8 (TTS Integration). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 8 after fixes. |
| **FAIL** | Critical issues found. Sprint 7 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Image Generation Robustness Assessment:

The image generation pipeline must be robust against API failures and content policy rejections. Verify:
- [ ] A single image failure does not cascade to the entire episode
- [ ] Partial results are preserved and resumable
- [ ] Cost tracking prevents runaway API spending
- [ ] The `ImageGenService` interface is clean enough for future provider swaps
- [ ] Images are correctly sized for downstream video rendering (1080p landscape)

### Deferred Items Acknowledged:

- TTS integration / TTS_DONE stage (Sprint 8)
- Video assembly / Render pipeline (Sprint 9-10)
- Review Gate 3 / video review (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Multiple image generation providers
- Image editing / regeneration UI
- Thumbnail generation as separate workflow
- Background music integration

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 6 (Error Handling)** — image generation APIs are unreliable and the pipeline must be resilient.
- **Pay attention to Section 7 (Partial Regeneration)** — this is important for cost efficiency and iteration speed.
- **Pay attention to Section 10 (Cost Tracking)** — DALL-E 3 costs accumulate quickly with many chapters. The cost cap must work.
- Verify that the `ImageGenService` interface is clean and would allow a straightforward swap to Flux, Midjourney, or another provider.
