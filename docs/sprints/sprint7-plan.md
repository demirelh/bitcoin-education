# Sprint 7 — Planning Prompt (Image/Video Prompt Generation)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–6 completed codebase (especially `btcedu/core/chapterizer.py` for stage pattern, `btcedu/core/chapter_schema.py` for the chapter JSON schema/Pydantic models, `btcedu/core/pipeline.py`, `btcedu/prompts/templates/`, `btcedu/models/`, `btcedu/services/`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the image generator module, image generation service abstraction, image prompt generation via LLM, image manifest, `media_assets` table + migration, CLI command, pipeline integration, dashboard image gallery, and tests.

---

## Context

You are planning **Sprint 7 (Phase 3, Part 2: Image/Video Prompt Generation)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–6 (Phases 0–2 + Phase 3 Part 1) are complete:
- Foundation: `EpisodeStatus` enum, `PromptVersion`/`ReviewTask`/`ReviewDecision` models, `PromptRegistry`, `pipeline_version`.
- Correction: `btcedu/core/corrector.py`, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: `btcedu/core/reviewer.py`, dashboard review queue + diff viewer + approve/reject/request-changes.
- Translation: `btcedu/core/translator.py`, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: `btcedu/core/adapter.py`, Turkey-context adaptation with tiered rules, adaptation diff, ADAPT stage, Review Gate 2.
- Chapterization: `btcedu/core/chapterizer.py`, chapter JSON schema (Pydantic models), validated `chapters.json`, chapter viewer in dashboard.

Sprint 7 implements the **IMAGE_GEN** stage — generating visual assets for each chapter based on the chapter JSON. This involves two sub-steps: (1) generating image prompts from the chapter JSON via an LLM call, and (2) calling an image generation API (DALL-E 3) to produce the actual images.

This sprint also introduces the `media_assets` table to track generated media and a service abstraction layer (`ImageGenService`) for future provider swaps.

### Sprint 7 Focus (from MASTERPLAN.md §4 Phase 3, §5E)

1. Generate image prompts from chapter JSON (LLM call using Claude to create detailed DALL-E prompts from chapter visual descriptions).
2. Create image generation service (`btcedu/services/image_gen_service.py`) with DALL-E 3 as the initial provider, abstracted behind an interface.
3. Implement `generate_images()` in `btcedu/core/image_generator.py` — iterate over chapters, generate prompts, call image API, save images.
4. Create image manifest (`data/outputs/{ep_id}/images/manifest.json`) tracking generated images.
5. Create `media_assets` table (Migration N+4 per §7.4) for tracking all generated media.
6. Add `imagegen` CLI command to `btcedu/cli.py` with `--force`, `--dry-run`, `--chapter` (single chapter regeneration).
7. Integrate into pipeline — IMAGE_GEN after CHAPTERIZED, before TTS.
8. Add image gallery view to dashboard (per-episode, per-chapter thumbnails).
9. Style consistency: brand guideline prefix for image prompts.
10. Partial recovery: only regenerate images for changed chapters.
11. Provenance, idempotency, cascade invalidation.
12. Write tests.

### Relevant Subplans

- **Subplan 5E** (Image/Video Prompt Generation) — all slices: image prompt generation from chapter JSON, image generation service (DALL-E 3), image storage + manifest, CLI + pipeline integration, image gallery in dashboard.
- **§7.3** (New Tables) — `media_assets` table schema.
- **§7.4** (Migration Sequencing) — Migration N+4 for `media_assets`.
- **§8** (Idempotency) — IMAGE_GEN stage specifics: already done = all chapter images exist AND chapter count matches. Partial recovery for changed chapters.
- **§3.6** (Provenance Model) — provenance JSON format.
- **§11** (Decision Matrix) — Image generation: DALL-E 3 recommended (RPi can't run SD; API is simpler).

---

## Your Task

Produce a detailed implementation plan for Sprint 7. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **Image Generation Service Design** — `ImageGenService` abstraction:
   - Interface/protocol definition (abstract base or Protocol class)
   - DALL-E 3 implementation (`DallE3ImageService`)
   - Config: API key (OpenAI key already exists), model name, default size (1920x1080), style prefix
   - Error handling: API rate limits, content policy rejections, timeout
   - Propose using the existing OpenAI client or a new lightweight wrapper
4. **Image Prompt Generation** — LLM step to produce detailed DALL-E prompts:
   - New prompt template: `btcedu/prompts/templates/imagegen.md`
   - Input: chapter's `visual.description` and `visual.type`
   - Output: a detailed, DALL-E-optimized image prompt string
   - Style consistency prefix for brand guidelines
   - Which chapters get images generated vs. which use templates/placeholders (based on `visual.type`)
5. **Image Generator Module Design** — `generate_images()` function:
   - Function signature and return type (`ImageGenResult` dataclass)
   - Processing logic: iterate chapters → generate prompt → call API → save image
   - Per-chapter and batch processing
   - Partial regeneration: only regenerate images for changed/new chapters
   - `image_prompt` field from chapter JSON used as input (or LLM generates if null)
6. **Image Manifest** — `data/outputs/{ep_id}/images/manifest.json` format (per §5E):
   - Per-image entry: chapter_id, prompt, model, size, file_path, generated_at
7. **`media_assets` Table** — migration and SQLAlchemy model per §7.3:
   - Fields: id, episode_id, asset_type, chapter_id, file_path, mime_type, size_bytes, duration_seconds, metadata (JSON), prompt_version_id (FK), created_at
   - Index: (episode_id, asset_type, chapter_id)
8. **CLI Command Design** — `btcedu imagegen <episode_id>` with `--force`, `--dry-run`, `--chapter <chapter_id>`.
9. **Pipeline Integration** — IMAGE_GEN after CHAPTERIZED, before TTS. Check idempotency per chapter.
10. **Dashboard Image Gallery** — design for displaying generated images per episode:
    - Thumbnail grid or list per chapter
    - Image preview on click/hover
    - Chapter context (title, visual type) shown alongside each image
    - Regeneration status indicators
11. **Provenance, Idempotency, Cascade Invalidation**:
    - Provenance: per-image provenance or consolidated `imagegen_provenance.json`
    - Idempotency: all chapter images exist AND chapter count matches AND chapter hashes match
    - Cascade: chapterization re-run → images marked stale; partial recovery supported
12. **Cost Tracking** — DALL-E 3 costs per image, cumulative episode cost check against `max_episode_cost_usd`.
13. **Test Plan** — list each test, what it asserts, file it belongs to.
14. **Implementation Order** — numbered sequence.
15. **Definition of Done** — checklist.
16. **Non-Goals** — explicit list.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected. IMAGE_GEN only runs for v2 episodes.
- **Chapter JSON is the input**: Consume the chapter JSON schema defined in Sprint 6. Import and use the same Pydantic models for parsing.
- **Follow existing patterns**: The image generator should mirror the chapterizer/adapter/translator/corrector module pattern.
- **DALL-E 3 first, abstracted for swap**: Start with DALL-E 3 via OpenAI API (already have API key). Abstract behind a service interface so the provider can be swapped (Flux, Midjourney, etc.) without changing the core module.
- **No TTS generation**: Sprint 7 handles images only. TTS is Sprint 8.
- **No video rendering**: Sprint 7 generates image assets. Rendering is Sprint 9-10.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include the full service interface, image manifest schema, `media_assets` migration SQL, function signatures, and dashboard UI description.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- The `ImageGenService` interface is important for future flexibility. Design it cleanly but do not over-engineer — DALL-E 3 is the only implementation needed now.
- Consider rate limiting: DALL-E 3 has API rate limits. The image generator should handle 429 responses with exponential backoff.
- Image size should be 1920x1080 for video production (landscape format).
- Consider content policy rejections from DALL-E 3 — the system should gracefully handle cases where an image prompt is rejected and log the issue without failing the entire episode.
- The style prefix (brand guidelines) should be configurable, not hardcoded. Store in settings or a config file.
- `[ASSUMPTION]`: Chapters with `visual.type = "title_card"` use a template/branded background rather than a generated image. Chapters with `visual.type = "talking_head"` use a stock/template image. Only `diagram`, `b_roll`, and `screen_share` types need API-generated images.
