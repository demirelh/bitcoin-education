# Sprint 7 — Implementation Prompt (Image/Video Prompt Generation)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 7 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–6 completed codebase
> - **Expected output**: All code changes (new files, modified files), image generation service, image generator module, `media_assets` migration, dashboard image gallery, tests — committed and passing.

---

## Context

You are implementing **Sprint 7 (Phase 3, Part 2: Image/Video Prompt Generation)** of the btcedu video production pipeline.

Sprints 1–6 (Phases 0–2 + Phase 3 Part 1) are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: corrector module, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: reviewer module, dashboard review queue + diff viewer, approve/reject/request-changes.
- Translation: translator module, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: adapter module, Turkey-context adaptation with tiered rules, adaptation diff, ADAPT stage, Review Gate 2.
- Chapterization: chapterizer module, chapter JSON schema (Pydantic), validated chapters.json, chapter viewer in dashboard.

Sprint 7 adds the **IMAGE_GEN** stage — generating image prompts via LLM and producing images via DALL-E 3 for each chapter. This sprint also introduces the `media_assets` table and a service abstraction (`ImageGenService`) for image generation.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 7 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/chapterizer.py` (stage pattern to follow), `btcedu/core/chapter_schema.py` (Pydantic models to import), `btcedu/core/pipeline.py`, `btcedu/services/claude_service.py` (API calling patterns), `btcedu/config.py`, `btcedu/cli.py`, `btcedu/web/` (template patterns), `btcedu/models/`.

2. **Create `media_assets` migration** — Migration N+4 per §7.4:
   - Create the `media_assets` table:
     ```sql
     CREATE TABLE media_assets (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       episode_id TEXT NOT NULL,
       asset_type TEXT NOT NULL,
       chapter_id TEXT,
       file_path TEXT,
       mime_type TEXT,
       size_bytes INTEGER,
       duration_seconds REAL,
       metadata TEXT,
       prompt_version_id INTEGER REFERENCES prompt_versions(id),
       created_at DATETIME DEFAULT CURRENT_TIMESTAMP
     );
     CREATE INDEX ix_media_assets_episode ON media_assets(episode_id, asset_type, chapter_id);
     ```
   - Create `btcedu/models/media_asset.py` with SQLAlchemy model.
   - Register the model in `btcedu/models/__init__.py`.
   - Append the Migration subclass to `btcedu/migrations/__init__.py`.

3. **Create `ImageGenService` abstraction** — `btcedu/services/image_gen_service.py`:
   - Define `ImageGenService` Protocol or ABC:
     - `generate_image(prompt: str, size: str, style: str | None) -> ImageGenResponse`
     - `ImageGenResponse` dataclass: image_bytes, file_path (optional), model, size, prompt, generated_at, cost_usd (optional)
   - Implement `DallE3ImageService`:
     - Uses OpenAI API (key from existing config)
     - Model: `dall-e-3`
     - Default size: `1792x1024` (closest DALL-E 3 landscape option to 1080p)
     - Quality: `standard` (configurable to `hd`)
     - Handles rate limits (429) with exponential backoff (3 retries)
     - Handles content policy rejections: returns a result with error flag, does not raise
     - Downloads the generated image from URL and returns bytes
   - Add config to `btcedu/config.py`:
     - `IMAGE_GEN_MODEL` (default: `dall-e-3`)
     - `IMAGE_GEN_SIZE` (default: `1792x1024`)
     - `IMAGE_GEN_QUALITY` (default: `standard`)
     - `IMAGE_GEN_STYLE_PREFIX` (default: empty string, configurable brand prefix)

4. **Create imagegen prompt template** — `btcedu/prompts/templates/imagegen.md`:
   - YAML frontmatter: name (`imagegen`), model, temperature (0.7), max_tokens (1024), description, author
   - Instructions to generate a detailed DALL-E image prompt from a chapter's visual description and context
   - Include style consistency guidance
   - Input variables: `{{ visual_description }}`, `{{ visual_type }}`, `{{ chapter_title }}`, `{{ narration_context }}`, `{{ style_prefix }}`
   - Output: a single image prompt string (plain text, no JSON)
   - Constraints: no text in images (DALL-E text rendering is unreliable), clean visual style, educational tone

5. **Create ImageGenResult dataclass** — include: episode_id, images_generated (count), images_skipped (count), images_failed (count), manifest, provenance, total_cost, token_counts.

6. **Implement `generate_images()`** in `btcedu/core/image_generator.py`:
   - Load chapter JSON from `data/outputs/{ep_id}/chapters.json` using Sprint 6 Pydantic models
   - **Pre-condition check**: Episode status is CHAPTERIZED
   - Check idempotency per chapter (image exists + chapter hash matches)
   - For each chapter:
     - Determine if image generation is needed based on `visual.type`:
       - `title_card`: skip (use template/brand asset)
       - `talking_head`: skip (use stock/template image)
       - `diagram`, `b_roll`, `screen_share`: generate
     - If `visual.image_prompt` is populated in chapter JSON: use it as the base prompt
     - If `visual.image_prompt` is null: call Claude via imagegen prompt template to generate a DALL-E prompt
     - Prepend style prefix from config
     - Call `ImageGenService.generate_image()`
     - Save image to `data/outputs/{ep_id}/images/{chapter_id}.png`
     - Record in `media_assets` table
   - Save manifest to `data/outputs/{ep_id}/images/manifest.json`
   - Save provenance to `data/outputs/{ep_id}/provenance/imagegen_provenance.json`
   - Check cumulative episode cost against `max_episode_cost_usd` after each image
   - Return ImageGenResult

7. **Handle partial regeneration**:
   - Compare current chapter hashes against stored manifest
   - Only regenerate images for chapters that changed
   - Keep existing images for unchanged chapters
   - `--force` regenerates all images
   - `--chapter <chapter_id>` regenerates a single chapter's image

8. **Handle errors gracefully**:
   - Content policy rejection: log the rejection, record in manifest as `status: "rejected"`, continue to next chapter. Do not fail the entire episode.
   - API timeout/rate limit: exponential backoff with 3 retries. After 3 failures, log and continue.
   - Record failed/skipped images in the manifest and in ImageGenResult.

9. **Add `imagegen` CLI command** to `btcedu/cli.py`:
   - `btcedu imagegen <episode_id>` with `--force`, `--dry-run`, `--chapter <chapter_id>`
   - Validate episode exists and is at CHAPTERIZED status
   - On success: update episode status to IMAGES_GENERATED
   - On partial success (some images failed): update status to IMAGES_GENERATED with warning, record failures in manifest
   - On complete failure: log error, leave status unchanged
   - Output summary: images generated, skipped, failed, total cost

10. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
    - Ensure IMAGES_GENERATED is in `PipelineStage` enum (should exist from Sprint 1)
    - Update `resolve_pipeline_plan()` to include IMAGE_GEN for v2 episodes after CHAPTERIZED
    - Position: CHAPTERIZED → IMAGE_GEN → TTS (Sprint 8)
    - No review gate between CHAPTERIZE and IMAGE_GEN
    - Cost cap check after IMAGE_GEN stage

11. **Create dashboard image gallery** — add image display to episode detail or dedicated route:
    - New route: `GET /episodes/<ep_id>/images` (or extend episode detail)
    - Template showing:
      - Grid layout of chapter images
      - Each image: thumbnail, chapter title, visual type badge, generation status
      - Click to view full-size image
      - Failed/skipped images shown with placeholder and status message
      - Manifest metadata: model, size, cost per image
    - Link from episode detail and chapter viewer to image gallery
    - Serve images from local filesystem via Flask static/send_file

12. **Write tests**:
    - `tests/test_image_gen_service.py`:
      - Mock OpenAI API: successful generation, rate limit, content rejection, timeout
      - Service returns correct ImageGenResponse
      - Retry logic on 429
    - `tests/test_image_generator.py`:
      - Unit: chapter type filtering (which chapters get images)
      - Unit: prompt generation (LLM call mocked)
      - Unit: partial regeneration (only changed chapters)
      - Integration: full image generation with mocked API
      - Idempotency: second run skips unchanged chapters
      - Force: `--force` regenerates all
      - Chapter flag: `--chapter ch01` regenerates only ch01
      - Error handling: content rejection continues to next chapter
      - Cost cap: stops if episode cost exceeds limit
    - `tests/test_media_asset_model.py`: CRUD for media_assets table
    - CLI test: `btcedu imagegen --help` works
    - Pipeline test: IMAGE_GEN included in v2 plan after CHAPTERIZED

13. **Verify**:
    - Run `pytest tests/`
    - Pick a chapterized episode
    - Run `btcedu imagegen <ep_id> --dry-run`
    - Run `btcedu imagegen <ep_id>`
    - Verify images at `data/outputs/{ep_id}/images/`
    - Verify manifest at `data/outputs/{ep_id}/images/manifest.json`
    - Verify `media_assets` records in database
    - Verify provenance at `data/outputs/{ep_id}/provenance/imagegen_provenance.json`
    - Open dashboard → navigate to image gallery → verify images render
    - Run again → verify unchanged chapters skipped (idempotent)
    - Run with `--force` → verify all regenerated
    - Run `btcedu status` → verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement TTS (that's Sprint 8).
- **Do NOT** implement video rendering (Sprint 9-10).
- **Do NOT** implement video review or Review Gate 3 (Sprint 9-10).
- **Do NOT** implement YouTube publishing (Sprint 11).
- **Do NOT** implement thumbnail generation as a separate stage (can be part of imagegen if a chapter is designated as thumbnail, but no separate flow).
- **Do NOT** implement image editing or regeneration UI beyond basic gallery view.
- **Do NOT** add a review gate after image generation (there is none per the master plan).
- **Do NOT** modify the chapter JSON schema (consume it as-is from Sprint 6).
- **Do NOT** modify existing stages (correct, translate, adapt, chapterize) or review gates.
- **Do NOT** implement multiple image generation providers — DALL-E 3 only. The interface is for future use.

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/chapterizer.py` closely — same file I/O, Claude API, provenance, idempotency patterns.
- **Service layer**: Follow `btcedu/services/claude_service.py` for API wrapper patterns.
- **Dashboard**: Follow existing template and route patterns in `btcedu/web/`.
- **CLI commands**: Follow existing Click command patterns.
- **Models**: Follow existing SQLAlchemy model patterns in `btcedu/models/`.
- **Migrations**: Follow existing migration patterns in `btcedu/migrations/`.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred
- Manual verification steps

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Import and use the chapter JSON Pydantic models from Sprint 6 — do not redefine the schema.
- Use `ensure_ascii=False` for Turkish characters in manifest JSON.
- Handle DALL-E 3 API errors gracefully — do not let a single image failure break the entire stage.
- Track costs meticulously — DALL-E 3 images are ~$0.04-0.08 each (standard quality).

---

## Definition of Done

- [ ] `media_assets` table created via migration
- [ ] `MediaAsset` SQLAlchemy model exists and is registered
- [ ] `ImageGenService` Protocol/ABC defined with `generate_image()` method
- [ ] `DallE3ImageService` implementation works with OpenAI API
- [ ] Image generation service handles rate limits and content policy rejections gracefully
- [ ] `btcedu/prompts/templates/imagegen.md` exists with valid YAML frontmatter
- [ ] `btcedu/core/image_generator.py` exists with `generate_images()` function
- [ ] Image generator correctly filters chapters by visual type (only diagram, b_roll, screen_share)
- [ ] Images saved to `data/outputs/{ep_id}/images/{chapter_id}.png`
- [ ] Image manifest saved to `data/outputs/{ep_id}/images/manifest.json`
- [ ] Manifest format matches §5E
- [ ] `media_assets` records created for each generated image
- [ ] Provenance JSON stored at expected path
- [ ] Partial regeneration works (only changed chapters regenerated)
- [ ] `btcedu imagegen <episode_id>` CLI works with `--force`, `--dry-run`, `--chapter`
- [ ] Pipeline plan includes IMAGE_GEN for v2 episodes after CHAPTERIZED
- [ ] Episode status updated to IMAGES_GENERATED on success
- [ ] Cost tracking: cumulative cost checked against `max_episode_cost_usd`
- [ ] Dashboard image gallery shows images per chapter with metadata
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- TTS integration (Sprint 8)
- Video rendering / RENDER stage (Sprint 9-10)
- Review Gate 3 / video review (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Multiple image generation providers (only DALL-E 3 now; interface is for future)
- Image editing or in-dashboard regeneration controls
- Review gate after image generation
- Thumbnail generation as a separate workflow
- Background music integration
