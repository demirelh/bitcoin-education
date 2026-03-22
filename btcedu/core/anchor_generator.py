"""Anchor video generation: create talking-head videos for TALKING_HEAD chapters."""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.chapter_schema import ChapterDocument, VisualType
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnchorEntry:
    """Metadata for a single anchor video."""

    chapter_id: str
    chapter_title: str
    audio_path: str
    video_path: str
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    did_talk_id: str


@dataclass
class AnchorResult:
    """Summary of anchor generation for one episode."""

    episode_id: str
    anchor_dir: Path
    manifest_path: Path
    provenance_path: Path
    segment_count: int = 0
    total_duration_seconds: float = 0.0
    cost_usd: float = 0.0
    skipped: bool = False


def generate_anchors(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> AnchorResult:
    """Generate anchor videos for TALKING_HEAD chapters in an episode.

    If anchor_enabled is False, this is a no-op that advances the status.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, regenerate all anchor videos

    Returns:
        AnchorResult with paths, counts, duration, cost, and skip status
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline. Anchor generation requires v2."
        )

    # Allow TTS_DONE or ANCHOR_GENERATED for idempotency
    if (
        episode.status not in (EpisodeStatus.TTS_DONE, EpisodeStatus.ANCHOR_GENERATED)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'anchor_generated'. Use --force to override."
        )

    # Resolve paths
    outputs_dir = Path(settings.outputs_dir) / episode_id
    chapters_path = outputs_dir / "chapters.json"
    anchor_dir = outputs_dir / "anchor"
    manifest_path = anchor_dir / "manifest.json"
    provenance_path = outputs_dir / "provenance" / "anchor_provenance.json"

    # If anchor is disabled, just advance status
    if not settings.anchor_enabled:
        logger.info("Anchor generation disabled, advancing status for %s", episode_id)
        if episode.status == EpisodeStatus.TTS_DONE:
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # Load chapters
    if not chapters_path.exists():
        raise FileNotFoundError(f"Chapters file not found: {chapters_path}")

    chapters_doc = _load_chapters(chapters_path)

    # Find TALKING_HEAD chapters
    talking_head_chapters = [
        ch for ch in chapters_doc.chapters if ch.visual.type == VisualType.TALKING_HEAD
    ]

    if not talking_head_chapters:
        logger.info("No TALKING_HEAD chapters for %s, advancing status", episode_id)
        if episode.status == EpisodeStatus.TTS_DONE:
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # Idempotency check
    content_hash = _compute_anchor_hash(chapters_doc, settings)
    if not force and _is_anchor_current(manifest_path, provenance_path, content_hash):
        logger.info("Anchor videos current for %s (use --force to regenerate)", episode_id)
        if episode.status == EpisodeStatus.TTS_DONE:
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="anchorgen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Load profile for anchor config
        source_image = settings.did_source_image_path
        source_image_url = settings.did_source_image_url
        expression = "serious"
        try:
            from btcedu.profiles import get_registry as _get_profile_registry

            _profile_name = (
                getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
            )
            _profile = _get_profile_registry(settings).get(_profile_name)
            if _profile:
                _anchor_cfg = _profile.stage_config.get("anchor", {})
                source_image = _anchor_cfg.get("source_image", source_image)
                source_image_url = _anchor_cfg.get("source_image_url", source_image_url)
                expression = _anchor_cfg.get("expression", expression)
        except Exception:
            pass

        # Load TTS manifest to find audio files
        tts_manifest_path = outputs_dir / "tts" / "manifest.json"
        if not tts_manifest_path.exists():
            raise FileNotFoundError(f"TTS manifest not found: {tts_manifest_path}")
        tts_manifest = json.loads(tts_manifest_path.read_text(encoding="utf-8"))
        tts_segments = {s["chapter_id"]: s for s in tts_manifest.get("segments", [])}

        # Create anchor service
        if settings.dry_run:
            from btcedu.services.anchor_service import DryRunAnchorService

            anchor_service = DryRunAnchorService(output_dir=str(anchor_dir))
        else:
            from btcedu.services.anchor_service import DIDService

            anchor_service = DIDService(
                api_key=settings.did_api_key, output_dir=str(anchor_dir)
            )

        anchor_dir.mkdir(parents=True, exist_ok=True)

        # Generate anchor videos
        anchor_entries: list[AnchorEntry] = []
        total_cost = 0.0
        total_duration = 0.0

        for chapter in talking_head_chapters:
            # Cost guard
            episode_total_cost = _get_episode_total_cost(session, episode_id)
            if episode_total_cost + total_cost > settings.max_episode_cost_usd:
                raise RuntimeError(
                    f"Episode cost limit exceeded: "
                    f"{episode_total_cost + total_cost:.2f} > "
                    f"{settings.max_episode_cost_usd}. Stopping anchor generation."
                )

            tts_segment = tts_segments.get(chapter.chapter_id)
            if not tts_segment:
                logger.warning(
                    "No TTS segment for chapter %s, skipping anchor", chapter.chapter_id
                )
                continue

            audio_path = str(outputs_dir / tts_segment["file_path"])
            if not Path(audio_path).exists():
                logger.warning("TTS audio not found: %s, skipping", audio_path)
                continue

            from btcedu.services.anchor_service import AnchorRequest

            request = AnchorRequest(
                source_image_path=source_image,
                source_image_url=source_image_url,
                audio_path=audio_path,
                chapter_id=chapter.chapter_id,
                expression=expression,
            )

            response = anchor_service.generate_anchor_video(request)

            entry = AnchorEntry(
                chapter_id=chapter.chapter_id,
                chapter_title=chapter.title,
                audio_path=tts_segment["file_path"],
                video_path=f"anchor/{chapter.chapter_id}.mp4",
                duration_seconds=response.duration_seconds,
                size_bytes=response.size_bytes,
                cost_usd=response.cost_usd,
                did_talk_id=response.did_talk_id,
            )
            anchor_entries.append(entry)
            total_cost += response.cost_usd
            total_duration += response.duration_seconds

            # Create MediaAsset record
            asset = MediaAsset(
                episode_id=episode_id,
                asset_type=MediaAssetType.VIDEO,
                chapter_id=chapter.chapter_id,
                file_path=entry.video_path,
                mime_type="video/mp4",
                size_bytes=entry.size_bytes,
                duration_seconds=response.duration_seconds,
                meta={
                    "did_talk_id": response.did_talk_id,
                    "source": "d-id",
                },
            )
            session.add(asset)

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "anchor_provider": settings.anchor_provider,
            "source_image": source_image,
            "generated_at": _utcnow().isoformat(),
            "total_duration_seconds": total_duration,
            "total_cost_usd": total_cost,
            "segments": [asdict(entry) for entry in anchor_entries],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "anchorgen",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "model": f"d-id ({settings.anchor_provider})",
            "input_files": [str(chapters_path), str(tts_manifest_path)],
            "input_content_hash": content_hash,
            "output_files": [str(manifest_path)]
            + [str(anchor_dir / f"{e.chapter_id}.mp4") for e in anchor_entries],
            "segment_count": len(anchor_entries),
            "total_duration_seconds": total_duration,
            "cost_usd": total_cost,
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="anchor_video",
            file_path="anchor/manifest.json",
            prompt_hash=content_hash,
            model="d-id",
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Mark render as stale
        render_manifest = outputs_dir / "render" / "render_manifest.json"
        if render_manifest.exists():
            render_manifest.with_suffix(".json.stale").touch()

        # Update episode status
        episode.status = EpisodeStatus.ANCHOR_GENERATED
        episode.error_message = None
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = total_cost
        session.commit()

        logger.info(
            "Anchor generation complete for %s: %d segments, %.1fs total, $%.3f",
            episode_id,
            len(anchor_entries),
            total_duration,
            total_cost,
        )

        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            segment_count=len(anchor_entries),
            total_duration_seconds=total_duration,
            cost_usd=total_cost,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        logger.error("Anchor generation failed for %s: %s", episode_id, e)
        raise


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _compute_anchor_hash(chapters_doc: ChapterDocument, settings: Settings) -> str:
    """Compute content hash for idempotency."""
    relevant = [
        {
            "chapter_id": ch.chapter_id,
            "visual_type": ch.visual.type.value,
        }
        for ch in chapters_doc.chapters
        if ch.visual.type == VisualType.TALKING_HEAD
    ]
    content = json.dumps(relevant, sort_keys=True) + settings.did_source_image_path
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_anchor_current(
    manifest_path: Path, provenance_path: Path, content_hash: str
) -> bool:
    """Check if anchor output is current (idempotency)."""
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        logger.info("Anchor manifest marked as stale")
        stale_marker.unlink()
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        return provenance.get("input_content_hash") == content_hash
    except (json.JSONDecodeError, KeyError):
        return False


def _get_episode_total_cost(session: Session, episode_id: str) -> float:
    """Get total cost across all pipeline runs for an episode."""
    from sqlalchemy import func

    result = (
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode_id)
        .scalar()
    )
    return float(result)
