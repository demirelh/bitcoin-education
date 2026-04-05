"""Frame editing stage: translate text overlays in extracted frames via Gemini."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FrameEditEntry:
    """One edited frame."""

    chapter_id: str
    source_path: str
    output_path: str
    cost_usd: float = 0.0
    skipped: bool = False


@dataclass
class FrameEditResult:
    """Summary of frame editing for one episode."""

    episode_id: str
    entries: list[FrameEditEntry] = field(default_factory=list)
    total_cost_usd: float = 0.0
    chapters_edited: int = 0
    chapters_skipped: int = 0
    skipped: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_content_hash(*parts: str) -> str:
    """SHA-256 hash of concatenated parts."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def _build_edit_prompt(
    chapter_title: str,
    narration_text: str,
    visual_description: str = "",
) -> str:
    """Render the Gemini frame edit prompt from the Jinja2 template."""
    template_path = (
        Path(__file__).parent.parent / "prompts" / "templates" / "gemini_frame_edit.md"
    )
    if template_path.exists():
        import jinja2

        raw = template_path.read_text(encoding="utf-8")
        # Strip YAML frontmatter
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                raw = raw[end + 3:].strip()
        tmpl = jinja2.Template(raw)
        return tmpl.render(
            chapter_title=chapter_title,
            narration_text=narration_text,
            visual_description=visual_description,
        )

    # Fallback if template missing
    return (
        f"Edit this German news frame for a Turkish version. "
        f"Translate all German text overlays to Turkish. "
        f"Chapter: {chapter_title}. "
        f"Keep all visual elements unchanged except the text."
    )


def _get_episode_total_cost(session: Session, episode_id: int) -> float:
    """Sum of all successful PipelineRun costs for an episode."""
    from sqlalchemy import func

    result = (
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(
            PipelineRun.episode_id == episode_id,
            PipelineRun.status == RunStatus.SUCCESS,
        )
        .scalar()
    )
    return float(result)


# ---------------------------------------------------------------------------
# Main stage function
# ---------------------------------------------------------------------------


def edit_frames(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> FrameEditResult:
    """Edit extracted frames using Gemini to translate DE text to TR.

    Stage flow
    ----------
    1. Load frames manifest (from frameextract stage).
    2. Load chapters (for context per frame).
    3. For each chapter assignment, send frame + prompt to Gemini.
    4. Save edited frame, write images manifest + provenance.
    5. Advance episode to IMAGES_GENERATED.

    Falls back to copying the original frame if Gemini is unavailable
    or the chapter has no assigned frame.
    """
    from btcedu.services.gemini_image_service import GeminiEditRequest, GeminiImageService

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(f"Episode {episode_id} is v1 pipeline")

    allowed = (EpisodeStatus.FRAMES_EXTRACTED, EpisodeStatus.IMAGES_GENERATED)
    if episode.status not in allowed and not force:
        raise ValueError(
            f"Episode {episode_id} status '{episode.status.value}' not in "
            f"{[s.value for s in allowed]}. Use --force to override."
        )

    # --- paths ---
    outputs = Path(settings.outputs_dir)
    ep_dir = outputs / episode_id
    frames_manifest_path = ep_dir / "frames" / "manifest.json"
    chapters_path = ep_dir / "chapters.json"
    images_dir = ep_dir / "images"
    images_manifest_path = images_dir / "manifest.json"
    provenance_path = ep_dir / "provenance" / "images_provenance.json"

    # --- load frames manifest ---
    if not frames_manifest_path.exists():
        logger.warning("No frames manifest for %s; nothing to edit", episode_id)
        if episode.status == EpisodeStatus.FRAMES_EXTRACTED:
            episode.status = EpisodeStatus.IMAGES_GENERATED
            session.commit()
        return FrameEditResult(episode_id=episode_id, skipped=True)

    frames_manifest = json.loads(frames_manifest_path.read_text(encoding="utf-8"))
    chapter_assignments = frames_manifest.get("chapter_assignments", [])

    # --- load chapters ---
    if not chapters_path.exists():
        raise ValueError(f"chapters.json not found for {episode_id}")

    from btcedu.models.chapter_schema import ChapterDocument

    chapters_doc = ChapterDocument.model_validate_json(
        chapters_path.read_text(encoding="utf-8")
    )
    chapters_by_id = {ch.chapter_id: ch for ch in chapters_doc.chapters}

    # --- idempotency check ---
    content_hash = _compute_content_hash(
        frames_manifest_path.read_text(encoding="utf-8"),
        chapters_path.read_text(encoding="utf-8"),
        settings.gemini_image_model,
    )

    if (
        not force
        and provenance_path.exists()
        and not (images_dir / ".stale").exists()
    ):
        prov = json.loads(provenance_path.read_text(encoding="utf-8"))
        if prov.get("content_hash") == content_hash:
            logger.info("Frame edits current for %s (use --force)", episode_id)
            if episode.status == EpisodeStatus.FRAMES_EXTRACTED:
                episode.status = EpisodeStatus.IMAGES_GENERATED
                session.commit()
            return FrameEditResult(episode_id=episode_id, skipped=True)

    # --- cost guard ---
    total_cost = _get_episode_total_cost(session, episode.id)
    if total_cost >= settings.max_episode_cost_usd:
        raise ValueError(
            f"Episode {episode_id} cost ${total_cost:.2f} exceeds "
            f"limit ${settings.max_episode_cost_usd:.2f}"
        )

    # --- pipeline run record ---
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.IMAGEGEN,
        status=RunStatus.RUNNING,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Init Gemini service
        service = GeminiImageService(
            api_key=settings.gemini_api_key,
            model=settings.gemini_image_model,
        )

        images_dir.mkdir(parents=True, exist_ok=True)
        # Clear stale marker
        stale_marker = images_dir / ".stale"
        if stale_marker.exists():
            stale_marker.unlink()

        result = FrameEditResult(episode_id=episode_id)
        image_entries: list[dict] = []

        for assignment in chapter_assignments:
            cid = assignment["chapter_id"]
            frame_path = Path(assignment["assigned_frame"])
            chapter = chapters_by_id.get(cid)

            if not chapter:
                logger.warning("Chapter %s not found in chapters.json, skipping", cid)
                result.chapters_skipped += 1
                continue

            if not frame_path.exists():
                logger.warning("Frame not found: %s, skipping chapter %s", frame_path, cid)
                result.chapters_skipped += 1
                continue

            output_path = images_dir / f"{cid}_edited.png"

            # Build prompt with chapter context
            prompt = _build_edit_prompt(
                chapter_title=chapter.title,
                narration_text=chapter.narration.text,
                visual_description=chapter.visual.description,
            )

            if settings.dry_run:
                # Dry run: copy original frame
                import shutil

                shutil.copy2(str(frame_path), str(output_path))
                entry = FrameEditEntry(
                    chapter_id=cid,
                    source_path=str(frame_path),
                    output_path=str(output_path),
                    skipped=True,
                )
                result.entries.append(entry)
                result.chapters_skipped += 1
                image_entries.append({
                    "chapter_id": cid,
                    "source_frame": str(frame_path),
                    "edited_frame": str(output_path),
                    "method": "dry_run",
                })
                continue

            # Call Gemini
            try:
                edit_resp = service.edit_image(
                    GeminiEditRequest(
                        image_path=frame_path,
                        prompt=prompt,
                        model=settings.gemini_image_model,
                    ),
                    output_path=output_path,
                )

                entry = FrameEditEntry(
                    chapter_id=cid,
                    source_path=str(frame_path),
                    output_path=str(output_path),
                    cost_usd=edit_resp.cost_usd,
                )
                result.entries.append(entry)
                result.total_cost_usd += edit_resp.cost_usd
                result.chapters_edited += 1

                image_entries.append({
                    "chapter_id": cid,
                    "source_frame": str(frame_path),
                    "edited_frame": str(output_path),
                    "method": f"gemini:{settings.gemini_image_model}",
                    "cost_usd": edit_resp.cost_usd,
                    "prompt_tokens": edit_resp.prompt_tokens,
                    "completion_tokens": edit_resp.completion_tokens,
                })

            except Exception as e:
                logger.warning(
                    "Gemini edit failed for chapter %s: %s — using original frame", cid, e
                )
                # Fallback: copy original frame
                import shutil

                shutil.copy2(str(frame_path), str(output_path))
                entry = FrameEditEntry(
                    chapter_id=cid,
                    source_path=str(frame_path),
                    output_path=str(output_path),
                    skipped=True,
                )
                result.entries.append(entry)
                result.chapters_skipped += 1
                image_entries.append({
                    "chapter_id": cid,
                    "source_frame": str(frame_path),
                    "edited_frame": str(output_path),
                    "method": "fallback_copy",
                    "error": str(e),
                })

        # --- write images manifest ---
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "method": "gemini_frame_edit",
            "model": settings.gemini_image_model,
            "total_edited": result.chapters_edited,
            "total_skipped": result.chapters_skipped,
            "total_cost_usd": result.total_cost_usd,
            "entries": image_entries,
        }
        images_manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # --- write provenance ---
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_data = {
            "stage": "imagegen",
            "method": "gemini_frame_edit",
            "model": settings.gemini_image_model,
            "content_hash": content_hash,
            "chapters_edited": result.chapters_edited,
            "chapters_skipped": result.chapters_skipped,
            "total_cost_usd": result.total_cost_usd,
            "created_at": _utcnow().isoformat(),
        }
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2), encoding="utf-8"
        )

        # --- update pipeline run ---
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = result.total_cost_usd

        # --- advance episode status ---
        if episode.status == EpisodeStatus.FRAMES_EXTRACTED:
            episode.status = EpisodeStatus.IMAGES_GENERATED
        episode.error_message = None
        session.commit()

        logger.info(
            "Frame editing complete for %s: %d edited, %d skipped, $%.4f",
            episode_id,
            result.chapters_edited,
            result.chapters_skipped,
            result.total_cost_usd,
        )

        return result

    except Exception:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(Exception)
        session.commit()
        raise
