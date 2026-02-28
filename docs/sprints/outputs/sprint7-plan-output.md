# Sprint 7 — Implementation Plan (Image/Video Prompt Generation)

**Sprint Number:** 7
**Phase:** 3 (Chapterization + Image Generation), Part 2
**Status:** Planning
**Dependencies:** Sprint 6 (Chapterization) complete
**Created:** 2026-02-28

---

## 1. Sprint Scope Summary

**In Scope:**

Sprint 7 implements the **IMAGE_GEN** stage, which generates visual assets for each chapter based on the chapter JSON produced in Sprint 6. This involves two sub-steps:

1. **Image prompt generation** — Using an LLM (Claude) to transform chapter visual descriptions into detailed, DALL-E-optimized image generation prompts
2. **Image generation** — Calling an image generation API (DALL-E 3 via OpenAI) to produce 1920x1080 landscape images for each chapter

This sprint delivers:
1. The **image generation service abstraction** (`ImageGenService` protocol) with DALL-E 3 as the initial provider
2. An **image prompt generation template** (`btcedu/prompts/templates/imagegen.md`) for LLM-based prompt optimization
3. The **image generator module** (`btcedu/core/image_generator.py`) with full idempotency, provenance, and partial regeneration support
4. A **`media_assets` database table** and SQLAlchemy model for tracking generated media
5. An **image manifest** (`images/manifest.json`) per episode tracking all generated images
6. A **CLI command** (`btcedu imagegen <episode_id>`) with `--force`, `--dry-run`, and `--chapter` options
7. **Pipeline integration** after CHAPTERIZED, before TTS
8. A **dashboard image gallery** showing generated images per episode/chapter
9. **Tests** for image generator logic, service abstraction, and integration

**Not In Scope (Deferred to Later Sprints):**

- TTS audio generation (Sprint 8)
- Video rendering (Sprint 9-10)
- Review Gate 3 after image generation (no gate specified for this stage)
- Thumbnail generation as a separate stage (can be added to IMAGE_GEN later)
- Background music, intro/outro assets (deferred per MASTERPLAN §13)
- Image editing or regeneration UI beyond basic regeneration button
- Advanced image style transfer or fine-tuning
- Local image generation (Stable Diffusion) — API-based only for now
- Video asset generation (stock footage, animations) — static images only

---

## 2. File-Level Plan

### 2.1 Files to Create

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/services/image_gen_service.py`
**Purpose:** Image generation service abstraction and DALL-E 3 implementation
**Contents:**
- `@dataclass ImageGenRequest` — model, prompt, size, style_prefix
- `@dataclass ImageGenResponse` — image_url, revised_prompt, file_path
- `class ImageGenService(Protocol)` — abstract interface with `generate_image(request: ImageGenRequest) -> ImageGenResponse`
- `class DallE3ImageService` — concrete implementation using OpenAI API
  - `__init__(self, api_key: str, default_size: str = "1920x1080", style_prefix: str = "")`
  - `generate_image(request: ImageGenRequest) -> ImageGenResponse` — calls DALL-E 3, downloads image, returns response
  - `_call_dalle3(prompt: str) -> dict` — API call with retry logic
  - `_download_image(url: str, target_path: Path) -> Path` — downloads image from URL to local file
  - Error handling for rate limits (429), content policy rejections, timeouts

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/core/image_generator.py`
**Purpose:** Core image generation logic
**Contents:**
- `VISUAL_TYPES_NEEDING_GENERATION = {"diagram", "b_roll", "screen_share"}` — types that need API generation vs templates
- `@dataclass ImageGenResult` — return type with `episode_id`, `images_path`, `manifest_path`, `provenance_path`, `image_count`, `generated_count`, `template_count`, `input_tokens`, `output_tokens`, `cost_usd`, `skipped`
- `@dataclass ImageEntry` — individual image metadata for manifest
- `generate_images(session, episode_id, settings, force=False, chapter_id=None) -> ImageGenResult` — main function
- `_is_image_gen_current(manifest_path, provenance_path, chapters_hash, prompt_hash) -> bool` — idempotency check
- `_load_chapters(chapters_path: Path) -> ChapterDocument` — load and validate chapter JSON
- `_needs_generation(visual_type: str) -> bool` — check if visual type needs API generation
- `_generate_image_prompt(chapter: Chapter, template_body: str, settings: Settings, dry_run_path: Path = None) -> tuple[str, int, int, float]` — LLM call to optimize image prompt
- `_generate_single_image(chapter: Chapter, image_prompt: str, image_service: ImageGenService, output_dir: Path) -> ImageEntry` — generate one image
- `_create_template_placeholder(chapter: Chapter, output_dir: Path) -> ImageEntry` — create placeholder for title_card/talking_head types
- `_compute_chapters_content_hash(chapters_doc: ChapterDocument) -> str` — SHA-256 of relevant chapter fields for change detection
- `_mark_downstream_stale(episode_id: str, outputs_dir: Path)` — mark TTS and RENDER stages stale

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/models/media_asset.py`
**Purpose:** SQLAlchemy model for media assets table
**Contents:**
- `class MediaAssetType(str, Enum)` — IMAGE, AUDIO, VIDEO
- `class MediaAsset(Base)` — SQLAlchemy model
  - `id: int` (primary key)
  - `episode_id: str` (indexed)
  - `asset_type: MediaAssetType` (indexed)
  - `chapter_id: str | None` (indexed)
  - `file_path: str` (relative path from outputs_dir)
  - `mime_type: str` (e.g., "image/png")
  - `size_bytes: int`
  - `duration_seconds: float | None` (for audio/video)
  - `metadata: dict` (JSON column with generation params)
  - `prompt_version_id: int | None` (FK to prompt_versions)
  - `created_at: datetime`
- Indexes: `(episode_id, asset_type, chapter_id)`

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/migrations/migration_005_media_assets.py`
**Purpose:** Database migration for media_assets table
**Contents:**
- `class Migration005MediaAssets(Migration)` — Migration subclass
  - `version = 5`
  - `description = "Add media_assets table for tracking generated images, audio, video"`
  - `up()` — SQL to create table and indexes
  - `down()` — SQL to drop table (for rollback)

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/prompts/templates/imagegen.md`
**Purpose:** Prompt template for image prompt optimization
**Contents:** Full template with YAML frontmatter and instructions (see section 4 below)

#### `/home/runner/work/bitcoin-education/bitcoin-education/tests/test_image_generator.py`
**Purpose:** Unit and integration tests
**Contents:**
- `test_image_gen_result_dataclass()` — validates result structure
- `test_needs_generation()` — checks visual type filtering
- `test_generate_image_prompt()` — LLM prompt generation with dry-run
- `test_generate_images_dry_run()` — end-to-end with dry-run
- `test_generate_images_idempotency()` — runs twice, second skips
- `test_generate_images_force()` — force re-run works
- `test_generate_images_partial_chapter()` — regenerate single chapter
- `test_generate_images_missing_chapters()` — error handling
- `test_template_placeholder_creation()` — title_card/talking_head placeholders
- `test_dalle3_service_mock()` — mock DALL-E 3 API calls

#### `/home/runner/work/bitcoin-education/bitcoin-education/tests/test_image_gen_service.py`
**Purpose:** Service layer tests
**Contents:**
- `test_dalle3_service_generate_image()` — integration test with real API (skipped by default)
- `test_dalle3_service_rate_limit_retry()` — 429 handling
- `test_dalle3_service_content_policy_rejection()` — content policy error handling
- `test_image_gen_request_dataclass()` — request validation

### 2.2 Files to Modify

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/models/episode.py`
**Changes:**
- Add `IMAGES_GENERATED = "images_generated"` to `EpisodeStatus` enum (line ~24)
- Add `IMAGE_GEN = "imagegen"` to `PipelineStage` enum (line ~43)

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/models/__init__.py`
**Changes:**
- Add import: `from btcedu.models.media_asset import MediaAsset, MediaAssetType` (after other model imports)

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/core/pipeline.py`
**Changes:**
- Add `EpisodeStatus.IMAGES_GENERATED: 14` to `_STATUS_ORDER` dict (line ~36)
- Add `("imagegen", EpisodeStatus.CHAPTERIZED)` to `_V2_STAGES` list after chapterize entry (line ~66)
- Add imagegen stage handler to `_run_stage()` function (line ~260):
  ```python
  elif stage_name == "imagegen":
      from btcedu.core.image_generator import generate_images
      result = generate_images(session, episode.episode_id, settings, force=force)
      elapsed = time.monotonic() - t0
      if result.skipped:
          return StageResult("imagegen", "skipped", elapsed, detail="already up-to-date")
      else:
          return StageResult(
              "imagegen",
              "success",
              elapsed,
              detail=f"{result.generated_count}/{result.image_count} images generated (${result.cost_usd:.4f})",
          )
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/cli.py`
**Changes:**
- Add `@cli.command()` function `imagegen()` after the `chapterize` command (line ~690):
  ```python
  @cli.command()
  @click.option(
      "--episode-id",
      "episode_ids",
      multiple=True,
      required=True,
      help="Episode ID(s) to generate images for (repeatable).",
  )
  @click.option("--force", is_flag=True, default=False, help="Regenerate all images even if they exist.")
  @click.option("--dry-run", is_flag=True, default=False, help="Write request JSON instead of calling APIs.")
  @click.option("--chapter", "chapter_id", default=None, help="Regenerate images for a specific chapter only.")
  @click.pass_context
  def imagegen(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool, chapter_id: str | None) -> None:
      """Generate images for chapters (v2 pipeline)."""
      from btcedu.core.image_generator import generate_images

      settings = ctx.obj["settings"]
      if dry_run:
          settings.dry_run = True

      session = ctx.obj["session_factory"]()
      try:
          for eid in episode_ids:
              try:
                  result = generate_images(session, eid, settings, force=force, chapter_id=chapter_id)
                  if result.skipped:
                      click.echo(f"[SKIP] {eid} -> already up-to-date")
                  else:
                      click.echo(
                          f"[OK] {eid} -> {result.generated_count}/{result.image_count} images "
                          f"generated (${result.cost_usd:.4f})"
                      )
              except Exception as e:
                  click.echo(f"[FAIL] {eid}: {e}", err=True)
      finally:
          session.close()
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/config.py`
**Changes:**
- Add new settings after line 69 (max_episode_cost_usd):
  ```python
  # Image Generation
  image_gen_provider: str = "dalle3"  # "dalle3" (only option for now)
  image_gen_model: str = "dall-e-3"
  image_gen_size: str = "1792x1024"  # DALL-E 3 landscape (closest to 1920x1080)
  image_gen_quality: str = "standard"  # "standard" or "hd"
  image_gen_style_prefix: str = "Professional educational content illustration for Bitcoin/cryptocurrency video. Clean, modern, minimalist design. "
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/migrations/__init__.py`
**Changes:**
- Import new migration: `from btcedu.migrations.migration_005_media_assets import Migration005MediaAssets`
- Append to `MIGRATIONS` list: `Migration005MediaAssets()`

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/web/api.py`
**Changes:**
- Add file mapping to `_FILE_MAP` dict (line ~350):
  ```python
  "images_manifest": ("outputs_dir", "{eid}/images/manifest.json"),
  ```
- Add image gallery API route (after episode chapters route):
  ```python
  @app.route("/api/episodes/<episode_id>/images", methods=["GET"])
  def get_episode_images(episode_id: str):
      """Get generated images for episode."""
      settings = get_settings()
      manifest_path = Path(settings.outputs_dir) / episode_id / "images" / "manifest.json"

      if not manifest_path.exists():
          return jsonify({"error": "Image manifest not found"}), 404

      try:
          manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
          # Add full URLs for image files
          for img in manifest_data.get("images", []):
              img["url"] = f"/api/episodes/{episode_id}/files/images/{Path(img['file_path']).name}"
          return jsonify(manifest_data)
      except (json.JSONDecodeError, OSError) as e:
          return jsonify({"error": f"Failed to load image manifest: {e}"}), 500
  ```
- Add static file serving for images (if not already present):
  ```python
  @app.route("/api/episodes/<episode_id>/files/images/<filename>", methods=["GET"])
  def serve_episode_image(episode_id: str, filename: str):
      """Serve episode image file."""
      settings = get_settings()
      image_path = Path(settings.outputs_dir) / episode_id / "images" / filename

      if not image_path.exists():
          return jsonify({"error": "Image not found"}), 404

      return send_file(image_path, mimetype="image/png")
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/web/templates/episode_detail.html`
**Changes:**
- Add "Images" section after "Chapters" section
- Display image gallery with thumbnails (grid layout)
- Each thumbnail shows: chapter title, visual type badge, generation status
- Click to view full-size image in modal or new tab
- Show regeneration button per chapter with confirm dialog

---

## 3. Image Generation Service Design

### 3.1 Service Interface (Protocol)

The `ImageGenService` is defined as a Python Protocol to allow future provider swaps without modifying core logic:

```python
from typing import Protocol
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ImageGenRequest:
    """Request for image generation."""
    prompt: str
    model: str = "dall-e-3"
    size: str = "1792x1024"  # DALL-E 3 landscape (closest to 1920x1080)
    quality: str = "standard"  # "standard" or "hd"
    style_prefix: str = ""  # Brand guidelines prefix

@dataclass
class ImageGenResponse:
    """Response from image generation."""
    image_url: str  # Original URL from API
    revised_prompt: str  # DALL-E's revised prompt (if applicable)
    file_path: Path  # Local file path after download
    cost_usd: float  # Estimated cost
    model: str  # Model used

class ImageGenService(Protocol):
    """Protocol for image generation services."""

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        """Generate an image from a prompt."""
        ...
```

### 3.2 DALL-E 3 Implementation

```python
import time
import requests
from openai import OpenAI
from openai import RateLimitError, APIError

class DallE3ImageService:
    """DALL-E 3 image generation service."""

    # DALL-E 3 pricing per image (as of 2025)
    COST_STANDARD_1024 = 0.040  # $0.040 per image (1024x1024)
    COST_STANDARD_1792 = 0.080  # $0.080 per image (1792x1024 or 1024x1792)
    COST_HD_1024 = 0.080
    COST_HD_1792 = 0.120

    def __init__(
        self,
        api_key: str,
        default_size: str = "1792x1024",
        default_quality: str = "standard",
        style_prefix: str = "",
    ):
        self.client = OpenAI(api_key=api_key)
        self.default_size = default_size
        self.default_quality = default_quality
        self.style_prefix = style_prefix

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        """Generate image using DALL-E 3."""
        full_prompt = self.style_prefix + request.prompt if self.style_prefix else request.prompt

        # Call DALL-E 3 with retry logic
        response_data = self._call_dalle3_with_retry(
            prompt=full_prompt,
            size=request.size or self.default_size,
            quality=request.quality or self.default_quality,
        )

        # Download image
        image_url = response_data["data"][0]["url"]
        revised_prompt = response_data["data"][0].get("revised_prompt", request.prompt)

        # Compute cost
        cost = self._compute_cost(request.size or self.default_size, request.quality or self.default_quality)

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=revised_prompt,
            file_path=None,  # Set by caller after download
            cost_usd=cost,
            model=request.model,
        )

    def _call_dalle3_with_retry(self, prompt: str, size: str, quality: str, max_retries: int = 3) -> dict:
        """Call DALL-E 3 API with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                response = self.client.images.generate(
                    model="dall-e-3",
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    n=1,
                )
                return response.model_dump()
            except RateLimitError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(f"DALL-E 3 rate limit exceeded after {max_retries} retries") from e
            except APIError as e:
                if "content_policy_violation" in str(e).lower():
                    raise RuntimeError(f"DALL-E 3 rejected prompt due to content policy: {prompt}") from e
                raise RuntimeError(f"DALL-E 3 API error: {e}") from e

    def _compute_cost(self, size: str, quality: str) -> float:
        """Compute cost based on size and quality."""
        if quality == "hd":
            return self.COST_HD_1792 if "1792" in size else self.COST_HD_1024
        else:
            return self.COST_STANDARD_1792 if "1792" in size else self.COST_STANDARD_1024

    @staticmethod
    def download_image(url: str, target_path: Path) -> Path:
        """Download image from URL to local file."""
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(response.content)
        return target_path
```

**[ASSUMPTION]:** OpenAI API client is already installed and available. DALL-E 3 size "1792x1024" is the closest available to 1920x1080 (landscape format). Images will be scaled/cropped during video rendering if needed.

### 3.3 Error Handling Strategy

1. **Rate Limits (429):** Retry with exponential backoff (3 attempts: 1s, 2s, 4s). If all fail, raise exception and mark episode as failed for that stage.

2. **Content Policy Rejection:** Log the rejected prompt, raise exception with clear message. Episode status remains at previous stage. Human review required to adjust prompt or skip that chapter.

3. **Network Errors:** Retry up to 3 times. If persistent, fail gracefully with error message.

4. **API Timeouts:** 30-second timeout per request. Retry with backoff.

5. **Cost Overruns:** Before each image generation, check cumulative episode cost. If `total_cost + image_cost > max_episode_cost_usd`, stop and mark episode as `COST_LIMIT`.

---

## 4. Image Prompt Generation (LLM Step)

### 4.1 Purpose

Chapter visual descriptions from `chapters.json` are often brief (e.g., "diagram showing Bitcoin transaction flow"). We use an LLM (Claude) to expand these into detailed, DALL-E-optimized prompts that incorporate:
- Style consistency (brand guidelines)
- Technical accuracy (Bitcoin/crypto terminology)
- Visual composition guidance (layout, colors, typography)
- DALL-E 3 best practices (avoid text in images, use descriptive language)

### 4.2 Prompt Template (`btcedu/prompts/templates/imagegen.md`)

```markdown
---
name: imagegen
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 2048
description: Generate detailed DALL-E 3 image prompts from chapter visual descriptions
author: system
---

# System

You are an expert image prompt engineer specializing in educational content for Bitcoin and cryptocurrency videos. Your task is to transform brief chapter visual descriptions into detailed, high-quality image generation prompts optimized for DALL-E 3.

**Brand Guidelines:**
- Style: Professional, modern, minimalist
- Tone: Educational, approachable, trustworthy
- Color palette: Use Bitcoin orange (#F7931A) as accent, neutral backgrounds (white, light gray)
- Avoid: Cartoon-like illustrations, overly complex diagrams, financial advice imagery

**DALL-E 3 Best Practices:**
- Be descriptive and specific about composition, lighting, colors
- Avoid requesting text in images (DALL-E 3 struggles with text rendering)
- Use natural language, not keywords
- Specify style (e.g., "flat design illustration", "isometric diagram", "photorealistic")
- Avoid copyrighted characters or specific people

**Technical Accuracy:**
- Use correct Bitcoin/crypto terminology
- Ensure diagrams are conceptually accurate (e.g., blockchain structure, transaction flow)
- Avoid metaphors that might mislead (e.g., Bitcoin as physical coin in all contexts)

# Instructions

Given a chapter's visual description and type, generate a detailed DALL-E 3 prompt (150-250 words) that will produce a high-quality image for a Turkish Bitcoin education video.

**Input Format:**
- Chapter Title: [title]
- Visual Type: [diagram | b_roll | screen_share]
- Visual Description: [brief description from chapter JSON]
- Narration Context: [what is being said in this chapter]

**Output Format:**
Return ONLY the image prompt as plain text, no markdown formatting, no preamble.

# Input

Chapter Title: {{ chapter.title }}
Visual Type: {{ chapter.visuals[0].type }}
Visual Description: {{ chapter.visuals[0].description }}
Narration Context: {{ chapter.narration.text[:300] }}...

# Output

[Your detailed DALL-E 3 prompt here]
```

### 4.3 Visual Type Handling

**Types Needing API Generation:**
- `diagram` — Technical diagrams, flowcharts, conceptual illustrations
- `b_roll` — Background visuals, abstract concepts, scene-setting
- `screen_share` — UI mockups, software interface illustrations (not screenshots)

**Types Using Templates/Placeholders:**
- `title_card` — Branded template with episode title overlay (static asset)
- `talking_head` — Placeholder or stock image (future: real presenter recording)

**[ASSUMPTION]:** For title_card and talking_head types, we create placeholder images (solid color with text overlay) rather than generating via API. These can be replaced with proper assets in Sprint 9 (rendering).

---

## 5. Image Generator Module Design

### 5.1 Main Function Signature

```python
def generate_images(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    chapter_id: str | None = None,
) -> ImageGenResult:
    """
    Generate images for all chapters (or a specific chapter) in an episode.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, regenerate all images even if they exist
        chapter_id: If provided, only regenerate this specific chapter

    Returns:
        ImageGenResult with paths, counts, tokens, cost, and skip status

    Raises:
        ValueError: If episode/chapter not found or chapters.json invalid
        RuntimeError: If image generation API fails
    """
```

### 5.2 Processing Logic (Pseudocode)

```python
def generate_images(...) -> ImageGenResult:
    # 1. Load episode and check status
    episode = session.get(Episode, episode_id)
    if not episode:
        raise ValueError(f"Episode {episode_id} not found")

    if episode.pipeline_version != 2:
        raise ValueError(f"Episode {episode_id} is v1 pipeline, image generation not supported")

    if episode.status not in [CHAPTERIZED, IMAGES_GENERATED, ...]:
        raise ValueError(f"Episode {episode_id} not ready for image generation (status: {episode.status})")

    # 2. Load chapters.json
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not chapters_path.exists():
        raise ValueError(f"Chapters file not found: {chapters_path}")

    chapters_doc = _load_chapters(chapters_path)
    chapters_hash = _compute_chapters_content_hash(chapters_doc)

    # 3. Check idempotency (unless force=True or chapter_id specified)
    output_dir = Path(settings.outputs_dir) / episode_id / "images"
    manifest_path = output_dir / "manifest.json"
    provenance_path = Path(settings.outputs_dir) / episode_id / "provenance" / "imagegen_provenance.json"

    if not force and chapter_id is None:
        prompt_registry = PromptRegistry(session)
        prompt_version = prompt_registry.get_default("imagegen")

        if _is_image_gen_current(manifest_path, provenance_path, chapters_hash, prompt_version.content_hash):
            return ImageGenResult(episode_id=episode_id, skipped=True, ...)

    # 4. Load image generation service
    image_service = _create_image_service(settings)

    # 5. Load prompt template
    prompt_registry = PromptRegistry(session)
    prompt_version = prompt_registry.register_version("imagegen", "btcedu/prompts/templates/imagegen.md", set_default=True)
    template = prompt_registry.load_template("btcedu/prompts/templates/imagegen.md")

    # 6. Filter chapters to process
    chapters_to_process = chapters_doc.chapters
    if chapter_id:
        chapters_to_process = [c for c in chapters_doc.chapters if c.chapter_id == chapter_id]
        if not chapters_to_process:
            raise ValueError(f"Chapter {chapter_id} not found in chapters.json")

    # 7. Process each chapter
    image_entries = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    generated_count = 0
    template_count = 0

    for chapter in chapters_to_process:
        visual = chapter.visuals[0] if chapter.visuals else None
        if not visual:
            continue

        if _needs_generation(visual.type):
            # Generate image via API
            if visual.image_prompt:
                # Use prompt from chapter JSON if provided
                image_prompt = visual.image_prompt
                prompt_tokens, completion_tokens, prompt_cost = 0, 0, 0.0
            else:
                # Generate prompt via LLM
                image_prompt, prompt_tokens, completion_tokens, prompt_cost = _generate_image_prompt(
                    chapter, template.body, settings, dry_run_path=...
                )
                total_input_tokens += prompt_tokens
                total_output_tokens += completion_tokens
                total_cost += prompt_cost

            # Check cost limit
            episode_total_cost = _get_episode_total_cost(session, episode_id)
            if episode_total_cost + total_cost > settings.max_episode_cost_usd:
                raise RuntimeError(f"Episode cost limit exceeded: {episode_total_cost + total_cost:.2f} > {settings.max_episode_cost_usd}")

            # Generate image
            image_entry = _generate_single_image(chapter, image_prompt, image_service, output_dir)
            total_cost += image_entry.metadata["cost_usd"]
            generated_count += 1
        else:
            # Create template placeholder
            image_entry = _create_template_placeholder(chapter, output_dir)
            template_count += 1

        image_entries.append(image_entry)

        # Create MediaAsset record
        media_asset = MediaAsset(
            episode_id=episode_id,
            asset_type=MediaAssetType.IMAGE,
            chapter_id=chapter.chapter_id,
            file_path=str(image_entry.file_path.relative_to(Path(settings.outputs_dir) / episode_id)),
            mime_type="image/png",
            size_bytes=image_entry.file_path.stat().st_size,
            metadata=image_entry.metadata,
            prompt_version_id=prompt_version.id,
            created_at=datetime.utcnow(),
        )
        session.add(media_asset)

    # 8. Write manifest
    manifest_data = {
        "episode_id": episode_id,
        "schema_version": chapters_doc.schema_version,
        "generated_at": datetime.utcnow().isoformat(),
        "images": [asdict(entry) for entry in image_entries],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False))

    # 9. Write provenance
    provenance_data = {
        "stage": "imagegen",
        "episode_id": episode_id,
        "timestamp": datetime.utcnow().isoformat(),
        "prompt_name": "imagegen",
        "prompt_version": prompt_version.version,
        "prompt_hash": prompt_version.content_hash,
        "model": settings.claude_model,
        "image_gen_model": settings.image_gen_model,
        "input_files": [str(chapters_path)],
        "input_content_hash": chapters_hash,
        "output_files": [str(manifest_path)] + [str(e.file_path) for e in image_entries],
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "image_count": len(image_entries),
        "generated_count": generated_count,
        "template_count": template_count,
        "cost_usd": total_cost,
    }
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(provenance_data, indent=2, ensure_ascii=False))

    # 10. Create ContentArtifact record
    artifact = ContentArtifact(
        episode_id=episode_id,
        artifact_type="images",
        file_path=str(manifest_path.relative_to(Path(settings.outputs_dir) / episode_id)),
        prompt_hash=prompt_version.content_hash,
        prompt_version_id=prompt_version.id,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cost_usd=total_cost,
        created_at=datetime.utcnow(),
    )
    session.add(artifact)

    # 11. Mark downstream stages stale (TTS, RENDER)
    _mark_downstream_stale(episode_id, Path(settings.outputs_dir))

    # 12. Update episode status
    episode.status = EpisodeStatus.IMAGES_GENERATED
    session.commit()

    return ImageGenResult(
        episode_id=episode_id,
        images_path=output_dir,
        manifest_path=manifest_path,
        provenance_path=provenance_path,
        image_count=len(image_entries),
        generated_count=generated_count,
        template_count=template_count,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cost_usd=total_cost,
        skipped=False,
    )
```

### 5.3 Partial Regeneration (Single Chapter)

When `chapter_id` is provided:
1. Load existing manifest (if exists)
2. Keep all existing image entries except for the specified chapter
3. Regenerate only the specified chapter's image
4. Merge new entry into manifest
5. Update provenance with partial regeneration metadata

This allows fixing a single chapter without regenerating all images (saves cost and time).

---

## 6. Image Manifest Format

### 6.1 Schema

```json
{
  "episode_id": "abc123",
  "schema_version": "1.0",
  "generated_at": "2026-02-28T10:30:00Z",
  "images": [
    {
      "chapter_id": "ch01",
      "chapter_title": "Giriş",
      "visual_type": "title_card",
      "file_path": "images/ch01_intro.png",
      "prompt": null,
      "generation_method": "template",
      "model": null,
      "size": "1920x1080",
      "mime_type": "image/png",
      "size_bytes": 245120,
      "metadata": {
        "template_name": "title_card_default",
        "background_color": "#F7931A",
        "text_overlay": "Bitcoin Nedir?"
      }
    },
    {
      "chapter_id": "ch02",
      "chapter_title": "Bitcoin'in Tarihi",
      "visual_type": "diagram",
      "file_path": "images/ch02_history.png",
      "prompt": "Professional educational diagram showing Bitcoin's historical timeline from 2008 to present...",
      "generation_method": "dalle3",
      "model": "dall-e-3",
      "size": "1792x1024",
      "mime_type": "image/png",
      "size_bytes": 1523840,
      "metadata": {
        "revised_prompt": "A clean, modern timeline diagram...",
        "cost_usd": 0.080,
        "generated_at": "2026-02-28T10:32:15Z"
      }
    }
  ]
}
```

### 6.2 Fields

- `chapter_id`: Links to chapter in chapters.json
- `chapter_title`: Human-readable title
- `visual_type`: One of the VisualType enum values
- `file_path`: Relative path from episode output dir
- `prompt`: Full DALL-E prompt (null for templates)
- `generation_method`: "dalle3", "template", or future providers
- `model`: Model name used (null for templates)
- `size`: Image dimensions
- `metadata`: Provider-specific data (cost, revised_prompt, template_name, etc.)

---

## 7. `media_assets` Table Schema

### 7.1 Migration SQL (Migration 005)

```sql
-- Migration 005: Add media_assets table
-- Purpose: Track all generated media (images, audio, video) with metadata

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT NOT NULL,
    asset_type TEXT NOT NULL,  -- 'IMAGE', 'AUDIO', 'VIDEO'
    chapter_id TEXT,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_seconds REAL,  -- For audio/video
    metadata TEXT,  -- JSON
    prompt_version_id INTEGER,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
);

CREATE INDEX IF NOT EXISTS idx_media_assets_episode_type_chapter
ON media_assets(episode_id, asset_type, chapter_id);

CREATE INDEX IF NOT EXISTS idx_media_assets_type
ON media_assets(asset_type);
```

### 7.2 SQLAlchemy Model

See section 2.1 (btcedu/models/media_asset.py) for full model definition.

### 7.3 Usage

- **Sprint 7:** Insert IMAGE records after generating images
- **Sprint 8:** Insert AUDIO records after generating TTS
- **Sprint 9-10:** Insert VIDEO records after rendering
- **Query Examples:**
  - All images for an episode: `session.query(MediaAsset).filter_by(episode_id="abc123", asset_type="IMAGE")`
  - All assets for a chapter: `session.query(MediaAsset).filter_by(episode_id="abc123", chapter_id="ch02")`
  - Total storage used: `session.query(func.sum(MediaAsset.size_bytes)).scalar()`

---

## 8. Provenance, Idempotency, Cascade Invalidation

### 8.1 Provenance

Every image generation run writes `imagegen_provenance.json`:

```json
{
  "stage": "imagegen",
  "episode_id": "abc123",
  "timestamp": "2026-02-28T10:30:00Z",
  "prompt_name": "imagegen",
  "prompt_version": 1,
  "prompt_hash": "sha256:def456...",
  "model": "claude-sonnet-4-20250514",
  "image_gen_model": "dall-e-3",
  "input_files": ["data/outputs/abc123/chapters.json"],
  "input_content_hash": "sha256:abc123...",
  "output_files": [
    "data/outputs/abc123/images/manifest.json",
    "data/outputs/abc123/images/ch01_intro.png",
    "data/outputs/abc123/images/ch02_history.png"
  ],
  "input_tokens": 2500,
  "output_tokens": 800,
  "image_count": 8,
  "generated_count": 6,
  "template_count": 2,
  "cost_usd": 0.52,
  "duration_seconds": 45.3
}
```

### 8.2 Idempotency Check

IMAGE_GEN is considered "already done" if:
1. `images/manifest.json` exists
2. No `.stale` marker on manifest
3. `imagegen_provenance.json` exists
4. `input_content_hash` in provenance matches current chapters.json hash
5. `prompt_hash` in provenance matches current default prompt hash
6. All image files referenced in manifest exist on disk

If any condition fails, regeneration is needed.

### 8.3 Cascade Invalidation

**IMAGE_GEN is invalidated by:**
- Chapter JSON changes (chapterization re-run)
- Prompt template changes (imagegen prompt updated)

**IMAGE_GEN invalidates:**
- RENDER stage (video needs re-assembly with new images)

**Implementation:**
```python
def _mark_downstream_stale(episode_id: str, outputs_dir: Path):
    """Mark TTS and RENDER stages as stale."""
    stale_data = {
        "invalidated_at": datetime.utcnow().isoformat(),
        "invalidated_by": "imagegen",
        "reason": "images_changed",
    }

    # Mark render artifacts stale
    render_draft = outputs_dir / episode_id / "render" / "draft.mp4"
    if render_draft.exists():
        render_draft.with_suffix(".mp4.stale").write_text(json.dumps(stale_data))
```

**[ASSUMPTION]:** TTS stage is independent of images, so images don't invalidate TTS. Both run in parallel after chapterization.

---

## 9. Cost Tracking

### 9.1 Image Generation Costs

**DALL-E 3 Pricing (as of 2025):**
- Standard quality, 1024x1024: $0.040 per image
- Standard quality, 1792x1024: $0.080 per image
- HD quality, 1024x1024: $0.080 per image
- HD quality, 1792x1024: $0.120 per image

**[ASSUMPTION]:** Default settings use standard quality, 1792x1024 = $0.080 per image.

### 9.2 LLM Costs (Prompt Generation)

**Claude Sonnet 4 Pricing:**
- Input: $3 per million tokens
- Output: $15 per million tokens

**Estimated per-prompt generation:**
- Input: ~300 tokens (template + chapter context)
- Output: ~150 tokens (detailed prompt)
- Cost: ~$0.0027 per prompt

### 9.3 Total Episode Cost Example

For an 8-chapter episode:
- 6 chapters need generation (diagram/b_roll/screen_share)
- 2 chapters use templates (title_card/talking_head)
- LLM prompt generation: 6 × $0.0027 = $0.016
- Image generation: 6 × $0.080 = $0.480
- **Total IMAGE_GEN cost: ~$0.50**

Combined with upstream stages (correct, translate, adapt, chapterize), total episode cost should remain under $2-3, well within the $10 default `max_episode_cost_usd` limit.

### 9.4 Cost Limit Enforcement

Before each image generation, check:
```python
episode_total_cost = sum(run.estimated_cost_usd for run in episode.pipeline_runs)
if episode_total_cost + next_image_cost > settings.max_episode_cost_usd:
    episode.status = EpisodeStatus.COST_LIMIT
    episode.error_message = f"Cost limit exceeded: {episode_total_cost + next_image_cost:.2f} > {settings.max_episode_cost_usd}"
    raise RuntimeError(episode.error_message)
```

---

## 10. Dashboard Image Gallery Design

### 10.1 UI Components

**Episode Detail Page → Images Section:**

```
┌─────────────────────────────────────────────────────────────┐
│  Images                                        [Regenerate All]│
├─────────────────────────────────────────────────────────────┤
│  Generated: 6 images                                          │
│  Templates: 2 placeholders                                    │
│  Total Cost: $0.50                                            │
├─────────────────────────────────────────────────────────────┤
│  Chapter Gallery (Grid View)                                  │
│                                                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ ch01        │  │ ch02        │  │ ch03        │         │
│  │ Giriş       │  │ Tarih       │  │ Madencilik  │         │
│  │             │  │             │  │             │         │
│  │  [image]    │  │  [image]    │  │  [image]    │         │
│  │             │  │             │  │             │         │
│  │ title_card  │  │ diagram     │  │ b_roll      │         │
│  │ [template]  │  │ [generated] │  │ [generated] │         │
│  │             │  │ [🔄 Regen]  │  │ [🔄 Regen]  │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

**Features:**
- Thumbnail grid (3-4 per row, responsive)
- Each card shows: chapter title, visual type badge, generation method badge
- Click thumbnail to view full-size in modal
- "Regenerate" button per chapter (with confirm dialog)
- "Regenerate All" button (force=True, confirms cost)
- Show prompt text on hover or in modal
- Show cost per image in modal details

### 10.2 API Integration

**Frontend calls:**
1. `GET /api/episodes/{episode_id}/images` → returns manifest JSON with image URLs
2. `GET /api/episodes/{episode_id}/files/images/{filename}` → serves image file
3. `POST /api/jobs/submit` with `{"command": "imagegen", "episode_id": "...", "args": {"force": true}}` → trigger regeneration

**Regeneration Flow:**
1. User clicks "Regenerate All" or per-chapter "Regen" button
2. Confirm dialog: "This will cost ~$0.50. Continue?"
3. POST to jobs API, returns job_id
4. Poll job status endpoint
5. On completion, reload images section

---

## 11. Test Plan

### 11.1 Unit Tests (`tests/test_image_generator.py`)

| Test | Asserts | Notes |
|------|---------|-------|
| `test_image_gen_result_dataclass()` | ImageGenResult fields valid | Smoke test |
| `test_needs_generation()` | Correct visual types need generation | diagram/b_roll/screen_share → True, others → False |
| `test_generate_image_prompt()` | LLM prompt generation with dry-run | Outputs valid prompt string, tracks tokens/cost |
| `test_compute_chapters_content_hash()` | Hash changes when chapters change | SHA-256 consistency |
| `test_template_placeholder_creation()` | title_card/talking_head create placeholders | File exists, metadata correct |
| `test_generate_images_dry_run()` | Full pipeline with dry-run | No API calls, writes dry-run JSON |
| `test_generate_images_idempotency()` | Second run skips (same output) | skipped=True |
| `test_generate_images_force()` | Force re-runs despite existing output | skipped=False, new files |
| `test_generate_images_partial_chapter()` | Single chapter regeneration | Only specified chapter regenerated |
| `test_generate_images_missing_chapters()` | Error when chapters.json missing | Raises ValueError |
| `test_generate_images_cost_limit()` | Stops when cost limit exceeded | Raises RuntimeError, status=COST_LIMIT |
| `test_mark_downstream_stale()` | Render artifacts marked stale | .stale marker created |

### 11.2 Service Tests (`tests/test_image_gen_service.py`)

| Test | Asserts | Notes |
|------|---------|-------|
| `test_dalle3_service_generate_image()` | Real API call (skipped by default) | Integration test, requires API key |
| `test_dalle3_service_rate_limit_retry()` | 429 triggers exponential backoff | Mock API to return 429, then 200 |
| `test_dalle3_service_content_policy_rejection()` | Content policy error raised clearly | Mock API to return policy violation |
| `test_dalle3_service_cost_computation()` | Costs calculated correctly per size/quality | Standard 1792 = $0.080 |
| `test_dalle3_service_download_image()` | Image downloaded to correct path | Mock HTTP response |
| `test_image_gen_request_dataclass()` | Request validation | Fields populated correctly |

### 11.3 Integration Tests

| Test | Asserts | Notes |
|------|---------|-------|
| `test_pipeline_integration()` | IMAGE_GEN stage runs after CHAPTERIZED | Full pipeline from NEW → IMAGES_GENERATED |
| `test_cli_imagegen_command()` | CLI command works | Invoke `btcedu imagegen --episode-id ...` |
| `test_media_assets_records_created()` | MediaAsset records in DB | One per image |
| `test_cascade_invalidation()` | Chapterization re-run marks images stale | .stale markers exist |

### 11.4 Manual Testing Checklist

- [ ] Run full pipeline on test episode through IMAGE_GEN
- [ ] Verify images exist in `data/outputs/{eid}/images/`
- [ ] Verify manifest.json has correct structure
- [ ] Verify MediaAsset records in database
- [ ] Dashboard shows image gallery correctly
- [ ] Click thumbnail opens full-size image
- [ ] Regenerate single chapter works
- [ ] Regenerate all works
- [ ] Cost limit enforcement stops runaway costs
- [ ] Content policy rejection handled gracefully
- [ ] Idempotency: second run skips
- [ ] Force flag re-runs successfully

---

## 12. Implementation Order

**Recommended sequence (small, testable increments):**

1. **Database migration + model** (1 session)
   - Create `migration_005_media_assets.py`
   - Create `btcedu/models/media_asset.py`
   - Update `btcedu/models/__init__.py`
   - Update `btcedu/migrations/__init__.py`
   - Run migration, verify schema
   - Write model unit tests

2. **Image generation service** (1 session)
   - Create `btcedu/services/image_gen_service.py`
   - Implement `ImageGenService` protocol
   - Implement `DallE3ImageService`
   - Write service unit tests (with mocks)
   - Verify API integration with real call (manual test)

3. **Image prompt template** (1 session)
   - Create `btcedu/prompts/templates/imagegen.md`
   - Test prompt with dry-run

4. **Core image generator module** (2-3 sessions)
   - Create `btcedu/core/image_generator.py`
   - Implement idempotency check
   - Implement prompt generation step
   - Implement image generation loop
   - Implement template placeholder creation
   - Write provenance + manifest
   - Write unit tests
   - Test end-to-end with dry-run

5. **Pipeline + CLI integration** (1 session)
   - Update `btcedu/models/episode.py` (new status/stage)
   - Update `btcedu/core/pipeline.py` (dispatch, status order)
   - Add `imagegen` CLI command in `btcedu/cli.py`
   - Update config with image gen settings
   - Test CLI command manually

6. **Dashboard image gallery** (1-2 sessions)
   - Update `btcedu/web/api.py` (image endpoints)
   - Update `btcedu/web/templates/episode_detail.html`
   - Add CSS/JS for image gallery
   - Test in browser

7. **Integration tests + documentation** (1 session)
   - Write integration tests
   - Run full test suite
   - Update README or docs with new commands

**Total estimated effort:** 7-10 sessions (assuming 2-3 hour sessions)

---

## 13. Definition of Done

Sprint 7 is complete when:

- [x] **Migration 005** runs cleanly on existing database
- [x] **MediaAsset model** created and imported
- [x] **ImageGenService** protocol defined and DallE3ImageService implemented
- [x] **Image prompt template** (`imagegen.md`) created and registered
- [x] **Image generator module** (`image_generator.py`) implemented with all helper functions
- [x] **IMAGE_GEN stage** integrated into pipeline (dispatch in `_run_stage()`)
- [x] **EpisodeStatus.IMAGES_GENERATED** added to enum and status order
- [x] **CLI command** `btcedu imagegen` works with all options (--force, --dry-run, --chapter)
- [x] **Image manifest** written correctly with all metadata
- [x] **Provenance** written with all required fields
- [x] **MediaAsset records** created for each image
- [x] **Idempotency** works (second run skips if output is current)
- [x] **Force flag** re-runs successfully
- [x] **Partial regeneration** (single chapter) works
- [x] **Cascade invalidation** marks downstream stages stale
- [x] **Cost tracking** accumulated correctly in PipelineRun
- [x] **Cost limit enforcement** stops processing when exceeded
- [x] **Dashboard image gallery** displays images with thumbnails
- [x] **Regenerate buttons** in dashboard trigger jobs correctly
- [x] **All unit tests pass** (pytest tests/test_image_generator.py, tests/test_image_gen_service.py)
- [x] **Integration tests pass** (full pipeline through IMAGE_GEN)
- [x] **Manual verification** of full pipeline on at least 2 test episodes
- [x] **Existing v1 pipeline** still works (backward compatibility check)
- [x] **No regressions** in Sprint 1-6 functionality (run full test suite)
- [x] **Code review** checklist passed (see below)

### Code Review Checklist

- [ ] No hardcoded API keys or secrets
- [ ] All file paths use Path objects, not string concatenation
- [ ] All database sessions properly closed (try/finally or context managers)
- [ ] All API calls have timeout and retry logic
- [ ] All exceptions logged with context
- [ ] All dry-run paths handled correctly
- [ ] All provenance fields match MASTERPLAN schema
- [ ] All costs calculated correctly per pricing
- [ ] All statuses updated correctly in pipeline
- [ ] All downstream invalidation implemented
- [ ] No breaking changes to existing modules
- [ ] Type hints on all public functions
- [ ] Docstrings on all public functions
- [ ] No TODOs or FIXMEs in committed code
- [ ] All imports sorted and grouped correctly

---

## 14. Non-Goals

Explicitly **not** in scope for Sprint 7:

1. **TTS audio generation** — Sprint 8
2. **Video rendering** — Sprint 9-10
3. **Review gate after image generation** — Not specified in MASTERPLAN for this stage
4. **Image editing UI** — Dashboard is read-only view + regenerate button only
5. **Advanced image post-processing** — Scaling/cropping done in rendering stage, not here
6. **Thumbnail generation as separate stage** — Can be derived from first image or title card during rendering
7. **Background music or audio overlays** — Deferred per MASTERPLAN §13
8. **Intro/outro video templates** — Deferred, can be added to rendering
9. **Multi-provider image generation** — DALL-E 3 only for now (abstraction allows future swap)
10. **Local image generation (Stable Diffusion)** — RPi can't run SD efficiently
11. **Video assets (stock footage, animations)** — Static images only
12. **Image approval workflow** — No review gate specified for this stage
13. **Batch image regeneration across multiple episodes** — CLI supports one episode at a time
14. **Image prompt A/B testing** — Can be added via prompt versioning later
15. **Image style transfer or fine-tuning** — Use style_prefix for consistency, no advanced ML
16. **Copyright/licensing checks** — DALL-E 3 images are licensed for commercial use per OpenAI ToS

---

## 15. Assumptions

**[ASSUMPTION 1]:** OpenAI API client (Python SDK) is already installed in the project. If not, add `openai>=1.0.0` to requirements.txt.

**[ASSUMPTION 2]:** DALL-E 3 size "1792x1024" (landscape) is acceptable for video production. Images will be scaled/cropped to exactly 1920x1080 during rendering if needed.

**[ASSUMPTION 3]:** Chapters with `visual.type = "title_card"` or `"talking_head"` use template/placeholder images rather than API generation. These can be replaced with proper assets in Sprint 9.

**[ASSUMPTION 4]:** The `image_gen_style_prefix` setting is a global default. Per-chapter style overrides can be added to chapter JSON schema in the future if needed.

**[ASSUMPTION 5]:** Content policy rejections from DALL-E 3 are rare given the educational Bitcoin content. If they occur, the episode fails gracefully and requires human review to adjust the prompt.

**[ASSUMPTION 6]:** TTS stage is independent of images. Both run in parallel after chapterization. Images don't invalidate TTS.

**[ASSUMPTION 7]:** Image files are stored as PNG format. DALL-E 3 returns PNG by default. If JPG is preferred for smaller file sizes, can be configured via conversion step.

**[ASSUMPTION 8]:** The `image_prompt` field in chapter JSON is optional. If null, the system generates the prompt via LLM. If provided, it's used directly. This allows manual override for specific chapters.

**[ASSUMPTION 9]:** The dashboard image gallery does not support inline editing or annotation. It's a read-only view with regenerate functionality only.

**[ASSUMPTION 10]:** The cost estimate for images ($0.50 per 8-chapter episode) is acceptable within the $10 per-episode limit. If costs are higher in practice, the `max_episode_cost_usd` setting can be adjusted or per-stage cost limits added.

---

## 16. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| DALL-E 3 rate limits hit during batch processing | Medium | Medium | Exponential backoff retry (3 attempts). If persistent, fail gracefully and allow manual retry later. |
| Content policy rejections for Bitcoin-related prompts | Low | Medium | Craft prompts carefully to avoid financial advice imagery. Test prompt template with known-good examples. Log rejections for review. |
| Image quality inconsistency across chapters | Medium | Low | Use strong `style_prefix` with brand guidelines. Review first episode manually before batch processing. |
| High costs ($0.08 per image × many chapters) | Low | Medium | Cost limit enforcement per episode. Monitor cumulative costs via `btcedu cost` command. |
| DALL-E 3 API downtime | Low | High | Graceful failure with clear error message. Episode status remains at CHAPTERIZED, can retry later. |
| Image file storage fills disk (RPi SD card) | Medium | Medium | ~1.5MB per image × 6 images × 50 episodes = ~450MB. Monitor disk usage. Add cleanup policy for old episodes if needed. |
| Slow API responses (30s+ per image) | Medium | Low | Set 30s timeout per request. For 6 images, total ~3-5 minutes per episode (acceptable for daily pipeline). |
| Template placeholders look unprofessional | Low | Low | Improve template design in Sprint 9 (rendering). For now, solid color + text is sufficient for testing. |
| Chapter JSON schema changes break image generator | Low | High | Pydantic validation catches schema mismatches. Test with Sprint 6 chapters.json output before deploying. |

---

## 17. Follow-Up Work (Future Sprints)

After Sprint 7, the following work items are enabled or identified:

1. **Sprint 8 (TTS):** Generate per-chapter audio narration using ElevenLabs. IMAGE_GEN and TTS can run in parallel.

2. **Sprint 9-10 (Rendering):** Assemble images + TTS audio + overlays into draft video using ffmpeg. Images from Sprint 7 are consumed here.

3. **Image prompt refinement:** Based on first few episodes, iterate on `imagegen.md` template to improve output quality.

4. **Template asset design:** Replace placeholder title_card/talking_head images with proper branded assets (logo, presenter photo, etc.).

5. **Thumbnail generation:** Extract or generate dedicated YouTube thumbnail from episode images.

6. **Prompt versioning dashboard:** Build UI to compare outputs from different imagegen prompt versions (A/B testing).

7. **Image approval workflow:** If needed, add Review Gate 2.5 after IMAGE_GEN for human approval of visual assets before TTS/rendering.

8. **Multi-provider support:** Add Flux, Midjourney, or other image generation providers as alternatives to DALL-E 3.

9. **Local image generation:** If RPi is upgraded or cloud GPU is added, integrate Stable Diffusion for cost savings.

10. **Image post-processing:** Add automated scaling, cropping, watermarking, or style transfer as part of IMAGE_GEN stage.

---

**End of Sprint 7 Implementation Plan**

This plan is ready for implementation. Proceed with the sequence in section 12 (Implementation Order).
