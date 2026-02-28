"""Image generation: Create visual assets from chapter JSON via DALL-E 3."""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType
from btcedu.services.claude_service import call_claude
from btcedu.services.image_gen_service import (
    DallE3ImageService,
    ImageGenRequest,
    ImageGenResponse,
)

logger = logging.getLogger(__name__)

# Visual types that need API generation vs. template/placeholder
VISUAL_TYPES_NEEDING_GENERATION = {"diagram", "b_roll", "screen_share"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ImageEntry:
    """Metadata for a single generated or placeholder image."""

    chapter_id: str
    chapter_title: str
    visual_type: str
    file_path: str  # Relative path from episode outputs dir
    prompt: str | None  # DALL-E prompt (null for template placeholders)
    generation_method: str  # "dalle3", "template", "failed", "skipped"
    model: str | None  # Model used (null for templates)
    size: str  # Image dimensions
    mime_type: str
    size_bytes: int
    metadata: dict  # Additional generation params, cost, etc.


@dataclass
class ImageGenResult:
    """Summary of image generation operation for one episode."""

    episode_id: str
    images_path: Path
    manifest_path: Path
    provenance_path: Path
    image_count: int = 0
    generated_count: int = 0  # Actually generated via API
    template_count: int = 0  # Placeholders created
    failed_count: int = 0  # Failed generations
    input_tokens: int = 0  # From LLM prompt generation
    output_tokens: int = 0
    cost_usd: float = 0.0  # Total: LLM + image generation
    skipped: bool = False


def generate_images(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    chapter_id: str | None = None,
) -> ImageGenResult:
    """Generate images for all chapters (or a specific chapter) in an episode.

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
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # V2 pipeline only
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "Image generation is only supported for v2 pipeline."
        )

    # Check episode status (allow CHAPTERIZED or IMAGES_GENERATED for idempotency)
    if (
        episode.status not in (EpisodeStatus.CHAPTERIZED, EpisodeStatus.IMAGES_GENERATED)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'chapterized' or 'images_generated'. Use --force to override."
        )

    # Resolve paths
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not chapters_path.exists():
        raise FileNotFoundError(
            f"Chapters file not found for episode {episode_id}: {chapters_path}"
        )

    output_dir = Path(settings.outputs_dir) / episode_id / "images"
    manifest_path = output_dir / "manifest.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "imagegen_provenance.json"
    )

    # Load chapters
    chapters_doc = _load_chapters(chapters_path)
    chapters_hash = _compute_chapters_content_hash(chapters_doc)

    # Load and register prompt
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "imagegen.md"
    prompt_version = registry.register_version("imagegen", template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Idempotency check (skip if not force and not chapter-specific and output is current)
    if not force and chapter_id is None:
        if _is_image_gen_current(
            manifest_path, provenance_path, chapters_hash, prompt_content_hash
        ):
            logger.info(
                "Image generation is current for %s (use --force to regenerate)", episode_id
            )
            existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            return ImageGenResult(
                episode_id=episode_id,
                images_path=output_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                image_count=existing_provenance.get("image_count", 0),
                generated_count=existing_provenance.get("generated_count", 0),
                template_count=existing_provenance.get("template_count", 0),
                failed_count=existing_provenance.get("failed_count", 0),
                input_tokens=existing_provenance.get("input_tokens", 0),
                output_tokens=existing_provenance.get("output_tokens", 0),
                cost_usd=existing_provenance.get("cost_usd", 0.0),
                skipped=True,
            )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="imagegen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Create image generation service
        image_service = DallE3ImageService(
            api_key=settings.openai_api_key,
            default_size=getattr(settings, "image_gen_size", "1792x1024"),
            default_quality=getattr(settings, "image_gen_quality", "standard"),
            style_prefix=getattr(settings, "image_gen_style_prefix", ""),
        )

        # Filter chapters to process
        chapters_to_process = chapters_doc.chapters
        if chapter_id:
            chapters_to_process = [c for c in chapters_doc.chapters if c.chapter_id == chapter_id]
            if not chapters_to_process:
                raise ValueError(f"Chapter {chapter_id} not found in chapters.json")

        # Load existing manifest if doing partial regeneration
        existing_entries = {}
        if chapter_id and manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_entries = {
                    entry["chapter_id"]: entry for entry in existing_manifest.get("images", [])
                }
            except (json.JSONDecodeError, KeyError):
                logger.warning(
                    f"Could not load existing manifest from {manifest_path}, will regenerate all"
                )

        # Process each chapter
        image_entries = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        generated_count = 0
        template_count = 0
        failed_count = 0

        output_dir.mkdir(parents=True, exist_ok=True)

        for chapter in chapters_to_process:
            # Skip if no visual or processing only a specific chapter
            visual = chapter.visuals[0] if chapter.visuals else None
            if not visual:
                logger.warning(f"Chapter {chapter.chapter_id} has no visuals, skipping")
                continue

            # If partial regeneration and this chapter isn't being regenerated, keep existing
            if (
                chapter_id
                and chapter.chapter_id != chapter_id
                and chapter.chapter_id in existing_entries
            ):
                existing_entry_dict = existing_entries[chapter.chapter_id]
                # Convert dict to ImageEntry
                image_entry = ImageEntry(**existing_entry_dict)
                image_entries.append(image_entry)
                continue

            # Check if generation is needed for this visual type
            if _needs_generation(visual.type):
                try:
                    # Check cost limit before generating
                    episode_total_cost = _get_episode_total_cost(session, episode_id)
                    if episode_total_cost + total_cost > settings.max_episode_cost_usd:
                        raise RuntimeError(
                            f"Episode cost limit exceeded: {episode_total_cost + total_cost:.2f} > "
                            f"{settings.max_episode_cost_usd}. Stopping image generation."
                        )

                    # Generate or use existing image prompt
                    if visual.image_prompt:
                        # Use prompt from chapter JSON if provided
                        image_prompt = visual.image_prompt
                        prompt_tokens, completion_tokens, prompt_cost = 0, 0, 0.0
                    else:
                        # Generate prompt via LLM
                        (
                            image_prompt,
                            prompt_tokens,
                            completion_tokens,
                            prompt_cost,
                        ) = _generate_image_prompt(chapter, template_body, settings)
                        total_input_tokens += prompt_tokens
                        total_output_tokens += completion_tokens
                        total_cost += prompt_cost

                    # Generate image
                    image_entry = _generate_single_image(
                        chapter, image_prompt, image_service, output_dir, settings
                    )
                    total_cost += image_entry.metadata.get("cost_usd", 0.0)
                    generated_count += 1

                except Exception as e:
                    logger.error(f"Failed to generate image for chapter {chapter.chapter_id}: {e}")
                    # Create failed entry
                    image_entry = ImageEntry(
                        chapter_id=chapter.chapter_id,
                        chapter_title=chapter.title,
                        visual_type=visual.type,
                        file_path=f"images/{chapter.chapter_id}_failed.png",
                        prompt=None,
                        generation_method="failed",
                        model=None,
                        size="0x0",
                        mime_type="image/png",
                        size_bytes=0,
                        metadata={"error": str(e)},
                    )
                    failed_count += 1

            else:
                # Create template placeholder for title_card/talking_head
                image_entry = _create_template_placeholder(chapter, output_dir)
                template_count += 1

            image_entries.append(image_entry)

            # Record MediaAsset in database
            if image_entry.generation_method != "failed":
                _create_media_asset_record(session, episode_id, image_entry, prompt_version.id)

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": chapters_doc.schema_version,
            "generated_at": _utcnow().isoformat(),
            "images": [asdict(entry) for entry in image_entries],
        }
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "imagegen",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "imagegen",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "image_gen_model": getattr(settings, "image_gen_model", "dall-e-3"),
            "input_files": [str(chapters_path)],
            "input_content_hash": chapters_hash,
            "output_files": [str(manifest_path)]
            + [str(output_dir / entry.file_path) for entry in image_entries],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "image_count": len(image_entries),
            "generated_count": generated_count,
            "template_count": template_count,
            "failed_count": failed_count,
            "cost_usd": total_cost,
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="images",
            file_path=str(manifest_path.relative_to(Path(settings.outputs_dir) / episode_id)),
            prompt_hash=prompt_content_hash,
            prompt_version_id=prompt_version.id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Mark downstream stages stale (TTS, RENDER)
        _mark_downstream_stale(episode_id, Path(settings.outputs_dir))

        # Update episode status
        episode.status = EpisodeStatus.IMAGES_GENERATED
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost
        session.commit()

        logger.info(
            f"Image generation complete for {episode_id}: {generated_count} generated, "
            f"{template_count} placeholders, {failed_count} failed (${total_cost:.3f})"
        )

        return ImageGenResult(
            episode_id=episode_id,
            images_path=output_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            image_count=len(image_entries),
            generated_count=generated_count,
            template_count=template_count,
            failed_count=failed_count,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        logger.error(f"Image generation failed for {episode_id}: {e}")
        raise


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _compute_chapters_content_hash(chapters_doc: ChapterDocument) -> str:
    """Compute SHA-256 hash of relevant chapter fields for change detection."""
    # Hash only fields that affect image generation
    relevant_data = {
        "schema_version": chapters_doc.schema_version,
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "title": ch.title,
                "visual": (
                    {"type": ch.visual.type, "description": ch.visual.description}
                    if ch.visual
                    else None
                ),
            }
            for ch in chapters_doc.chapters
        ],
    }
    content_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _is_image_gen_current(
    manifest_path: Path,
    provenance_path: Path,
    chapters_hash: str,
    prompt_hash: str,
) -> bool:
    """Check if image generation is current (idempotency)."""
    # Check files exist
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    # Check for .stale marker
    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        logger.info("Image manifest marked as stale")
        return False

    # Check provenance hashes
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("input_content_hash") != chapters_hash:
            logger.info("Chapters content has changed")
            return False
        if provenance.get("prompt_hash") != prompt_hash:
            logger.info("Image generation prompt has changed")
            return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not verify provenance: {e}")
        return False

    # Verify all images exist
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_dir = manifest_path.parent.parent  # outputs/{ep_id}/
        for img_entry in manifest.get("images", []):
            if img_entry.get("generation_method") == "failed":
                continue  # Skip failed entries
            img_path = base_dir / img_entry["file_path"]
            if not img_path.exists():
                logger.info(f"Image file missing: {img_path}")
                return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not verify manifest: {e}")
        return False

    return True


def _needs_generation(visual_type: str) -> bool:
    """Check if visual type needs API generation vs. template."""
    return visual_type in VISUAL_TYPES_NEEDING_GENERATION


def _generate_image_prompt(
    chapter,
    template_body: str,
    settings: Settings,
) -> tuple[str, int, int, float]:
    """Generate DALL-E image prompt from chapter using LLM.

    Args:
        chapter: Chapter object with visual description
        template_body: Prompt template text
        settings: Application settings

    Returns:
        Tuple of (image_prompt, input_tokens, output_tokens, cost_usd)
    """
    # Split template at "# Input" marker
    system_prompt, user_template = _split_prompt(template_body)

    # Build user message with chapter data
    visual = chapter.visuals[0]
    narration_context = (
        chapter.narration.text[:300] + "..."
        if len(chapter.narration.text) > 300
        else chapter.narration.text
    )

    user_message = user_template.replace("{{ chapter_title }}", chapter.title)
    user_message = user_message.replace("{{ visual_type }}", visual.type)
    user_message = user_message.replace("{{ visual_description }}", visual.description)
    user_message = user_message.replace("{{ narration_context }}", narration_context)

    # Call Claude
    dry_run_path = None
    if settings.dry_run:
        dry_run_path = (
            Path(settings.outputs_dir) / "dry_run" / f"imagegen_{chapter.chapter_id}.json"
        )

    response = call_claude(system_prompt, user_message, settings, dry_run_path)

    image_prompt = response.text.strip()
    logger.info(
        f"Generated image prompt for chapter {chapter.chapter_id} ({len(image_prompt)} chars)"
    )

    return image_prompt, response.input_tokens, response.output_tokens, response.cost_usd


def _generate_single_image(
    chapter,
    image_prompt: str,
    image_service: DallE3ImageService,
    output_dir: Path,
    settings: Settings,
) -> ImageEntry:
    """Generate a single image via API.

    Args:
        chapter: Chapter object
        image_prompt: DALL-E prompt
        image_service: Image generation service
        output_dir: Directory to save images
        settings: Application settings

    Returns:
        ImageEntry with metadata
    """
    # Generate image via API
    request = ImageGenRequest(
        prompt=image_prompt,
        model=getattr(settings, "image_gen_model", "dall-e-3"),
        size=getattr(settings, "image_gen_size", "1792x1024"),
        quality=getattr(settings, "image_gen_quality", "standard"),
        style_prefix=getattr(settings, "image_gen_style_prefix", ""),
    )

    response: ImageGenResponse = image_service.generate_image(request)

    # Download image
    filename = f"{chapter.chapter_id}_{chapter.title[:30].replace(' ', '_').lower()}.png"
    target_path = output_dir / filename
    DallE3ImageService.download_image(response.image_url, target_path)

    # Get file size
    file_size = target_path.stat().st_size

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visuals[0].type,
        file_path=f"images/{filename}",
        prompt=image_prompt,
        generation_method="dalle3",
        model=response.model,
        size=request.size,
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "revised_prompt": response.revised_prompt,
            "cost_usd": response.cost_usd,
            "generated_at": _utcnow().isoformat(),
        },
    )


def _create_template_placeholder(chapter, output_dir: Path) -> ImageEntry:
    """Create a placeholder image for template types (title_card, talking_head).

    Args:
        chapter: Chapter object
        output_dir: Directory to save placeholder

    Returns:
        ImageEntry with placeholder metadata
    """
    from PIL import Image, ImageDraw, ImageFont

    # Create simple placeholder (solid color with text)
    width, height = 1920, 1080
    bg_color = (
        (247, 147, 26)
        if chapter.visuals[0].type == "title_card"
        else (200, 200, 200)  # Bitcoin orange or gray
    )

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Draw chapter title
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except OSError:
        font = ImageDraw.getfont()  # Fallback to default

    text = chapter.title
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((width - text_width) // 2, (height - text_height) // 2)

    draw.text(position, text, fill=(255, 255, 255), font=font)

    # Save placeholder
    filename = f"{chapter.chapter_id}_placeholder.png"
    target_path = output_dir / filename
    img.save(target_path, "PNG")

    file_size = target_path.stat().st_size

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visuals[0].type,
        file_path=f"images/{filename}",
        prompt=None,
        generation_method="template",
        model=None,
        size="1920x1080",
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "template_name": f"{chapter.visuals[0].type}_placeholder",
            "background_color": f"rgb{bg_color}",
            "text_overlay": chapter.title,
        },
    )


def _create_media_asset_record(
    session: Session,
    episode_id: str,
    image_entry: ImageEntry,
    prompt_version_id: int,
) -> None:
    """Create MediaAsset database record for generated image."""
    media_asset = MediaAsset(
        episode_id=episode_id,
        asset_type=MediaAssetType.IMAGE,
        chapter_id=image_entry.chapter_id,
        file_path=image_entry.file_path,
        mime_type=image_entry.mime_type,
        size_bytes=image_entry.size_bytes,
        meta=json.dumps(image_entry.metadata, ensure_ascii=False),
        prompt_version_id=prompt_version_id,
        created_at=_utcnow(),
    )
    session.add(media_asset)


def _mark_downstream_stale(episode_id: str, outputs_dir: Path) -> None:
    """Mark TTS and RENDER stages as stale."""
    stale_data = {
        "invalidated_at": _utcnow().isoformat(),
        "invalidated_by": "imagegen",
        "reason": "images_changed",
    }

    # Mark render artifacts stale
    render_draft = outputs_dir / episode_id / "render" / "draft.mp4"
    if render_draft.exists():
        stale_marker = render_draft.with_suffix(".mp4.stale")
        stale_marker.write_text(json.dumps(stale_data, ensure_ascii=False))
        logger.info(f"Marked render draft as stale: {stale_marker}")


def _get_episode_total_cost(session: Session, episode_id: str) -> float:
    """Get cumulative cost for all pipeline runs for this episode."""
    total = (
        session.query(PipelineRun)
        .filter(PipelineRun.episode_id == episode_id)
        .filter(PipelineRun.estimated_cost_usd.isnot(None))
    )
    return sum(run.estimated_cost_usd for run in total if run.estimated_cost_usd)


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split template at '# Input' marker into system and user sections."""
    if "# Input" in template_body:
        system_part, user_part = template_body.split("# Input", 1)
        return system_part.strip(), user_part.strip()
    else:
        # If no marker, treat entire template as system prompt
        return template_body.strip(), ""
