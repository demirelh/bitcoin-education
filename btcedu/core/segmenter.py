"""Story segmentation: Extract discrete news stories from broadcast transcripts."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.story_schema import Story, StoryDocument
from btcedu.models.transcript_schema import CorrectedTranscriptDocument
from btcedu.services.claude_service import ClaudeResponse, call_claude

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SegmentationResult:
    """Summary of broadcast segmentation for one episode."""

    episode_id: str
    stories_path: str
    provenance_path: str
    story_count: int = 0
    total_duration_seconds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: bool = False


def segment_broadcast(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> SegmentationResult:
    """Segment a corrected broadcast transcript into discrete news stories.

    Reads the corrected German transcript, sends it to Claude for story
    segmentation, validates the result with Pydantic, and writes the
    story manifest JSON and provenance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-segment even if output exists.

    Returns:
        SegmentationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found, not in correct status, or profile
            does not have segment enabled.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.CORRECTED, EpisodeStatus.SEGMENTED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'corrected' or 'segmented'. Use --force to override."
        )

    # Load profile and check segment is enabled
    try:
        from btcedu.profiles import get_registry

        registry_profiles = get_registry(settings)
        profile = registry_profiles.get(episode.content_profile)
        segment_config = profile.stage_config.get("segment", {})
        if not segment_config.get("enabled", False):
            logger.info(
                "Segment stage not enabled for profile '%s' on episode %s — skipping",
                episode.content_profile,
                episode_id,
            )
            return SegmentationResult(
                episode_id=episode_id,
                stories_path="",
                provenance_path="",
                skipped=True,
            )
        profile_namespace = profile.prompt_namespace
    except Exception as exc:
        # If profile not found, treat as not enabled
        logger.warning(
            "Could not load profile '%s' for episode %s: %s — skipping segment",
            getattr(episode, "content_profile", "unknown"),
            episode_id,
            exc,
        )
        return SegmentationResult(
            episode_id=episode_id,
            stories_path="",
            provenance_path="",
            skipped=True,
        )

    # Resolve paths
    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(
            f"Corrected transcript not found for episode {episode_id}: {corrected_path}"
        )

    stories_path = Path(settings.outputs_dir) / episode_id / "stories.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "segment_provenance.json"
    )

    # Load and register prompt via PromptRegistry
    prompt_registry = PromptRegistry(session)
    template_file = prompt_registry.resolve_template_path(
        "segment_broadcast.md", profile=profile_namespace
    )
    # Always fall back to base template (segment_broadcast is in templates root)
    if not template_file.exists():
        template_file = TEMPLATES_DIR / "segment_broadcast.md"
    prompt_version = prompt_registry.register_version(
        "segment_broadcast", template_file, set_default=True
    )
    _, template_body = prompt_registry.load_template(template_file)
    prompt_content_hash = prompt_registry.compute_hash(template_body)

    structured_corrected_path = (
        Path(settings.transcripts_dir) / episode_id / "transcript.corrected.structured.de.json"
    )

    # Compute input content hash for idempotency
    corrected_text = corrected_path.read_text(encoding="utf-8")
    structured_bytes = (
        structured_corrected_path.read_bytes() if structured_corrected_path.exists() else b""
    )
    input_content_hash = hashlib.sha256(
        corrected_text.encode("utf-8") + b"\0" + structured_bytes
    ).hexdigest()

    # Idempotency check
    if not force and _is_segmentation_current(
        stories_path, provenance_path, input_content_hash, prompt_content_hash
    ):
        logger.info("Segmentation is current for %s (use --force to re-segment)", episode_id)
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        return SegmentationResult(
            episode_id=episode_id,
            stories_path=str(stories_path),
            provenance_path=str(provenance_path),
            story_count=existing_provenance.get("story_count", 0),
            total_duration_seconds=existing_provenance.get("total_duration_seconds", 0),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.SEGMENT,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Split template body into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        corrected_doc = _load_corrected_document(structured_corrected_path, episode_id)
        prompt_transcript = _render_segment_input(corrected_text, corrected_doc)
        user_message = user_template.replace("{{ transcript }}", prompt_transcript)

        # Dry-run path
        dry_run_path = (
            Path(settings.outputs_dir) / episode_id / "dry_run_segment.json"
            if settings.dry_run
            else None
        )

        response: ClaudeResponse = call_claude(
            system_prompt=system_prompt,
            user_message=user_message,
            settings=settings,
            dry_run_path=dry_run_path,
            max_tokens=16384,
            json_mode=True,
        )

        # Parse and validate JSON response
        story_data = _parse_json_response(response.text, episode_id)

        # Inject episode_id if not present
        if "episode_id" not in story_data or not story_data["episode_id"]:
            story_data["episode_id"] = episode_id
        elif story_data["episode_id"] == "EPISODE_ID_PLACEHOLDER":
            story_data["episode_id"] = episode_id

        # Validate with Pydantic
        try:
            story_doc = StoryDocument.model_validate(story_data)
            story_doc = _normalize_story_inventory(story_doc, corrected_doc)
        except ValidationError as e:
            logger.warning("Segmentation output failed validation: %s", e)
            raise ValueError(f"Story segmentation output failed Pydantic validation: {e}") from e

        # Write stories.json
        stories_path.parent.mkdir(parents=True, exist_ok=True)
        stories_json = story_doc.model_dump(mode="json")
        stories_path.write_text(
            json.dumps(stories_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Cascade: mark translation as stale
        translation_stale_path = (
            Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt.stale"
        )
        stale_data = {
            "stale": True,
            "reason": "upstream changed",
            "at": _utcnow().isoformat(),
            "invalidated_by": "segment",
        }
        translation_stale_path.parent.mkdir(parents=True, exist_ok=True)
        translation_stale_path.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
        logger.info("Marked downstream translation as stale: %s", translation_stale_path.name)

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "segment",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "segment_broadcast",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": 16384,
            },
            "input_files": [
                str(path) for path in (corrected_path, structured_corrected_path) if path.exists()
            ],
            "input_content_hash": input_content_hash,
            "output_files": [str(stories_path)],
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
            "duration_seconds": round(elapsed, 2),
            "story_count": story_doc.total_stories,
            "total_duration_seconds": story_doc.total_duration_seconds,
            "broadcast_date": story_doc.broadcast_date,
            "source_attribution": story_doc.source_attribution,
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="segment",
            file_path=str(stories_path),
            model=settings.claude_model,
            prompt_hash=prompt_content_hash,
            retrieval_snapshot_path=None,
        )
        session.add(artifact)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = response.input_tokens
        pipeline_run.output_tokens = response.output_tokens
        pipeline_run.estimated_cost_usd = response.cost_usd

        # Update Episode
        episode.status = EpisodeStatus.SEGMENTED
        episode.error_message = None
        session.commit()

        logger.info(
            "Segmented broadcast for %s (%d stories, ~%ds, $%.4f)",
            episode_id,
            story_doc.total_stories,
            story_doc.total_duration_seconds,
            response.cost_usd,
        )

        return SegmentationResult(
            episode_id=episode_id,
            stories_path=str(stories_path),
            provenance_path=str(provenance_path),
            story_count=story_doc.total_stories,
            total_duration_seconds=story_doc.total_duration_seconds,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise


def _is_segmentation_current(
    stories_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing segmentation is still valid.

    Returns True (skip) if ALL of:
    1. stories_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not stories_path.exists():
        return False

    # Check for stale marker
    stale_marker = stories_path.parent / (stories_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Segmentation marked stale (upstream change), will reprocess")
        stale_marker.unlink()
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if provenance.get("prompt_hash") != prompt_content_hash:
        logger.info("Prompt hash mismatch (prompt was updated)")
        return False

    if provenance.get("input_content_hash") != input_content_hash:
        logger.info("Input content hash mismatch (corrected transcript was updated)")
        return False

    return True


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split rendered template into system prompt and user message.

    The template is split at the '# Input' header.
    Everything before it becomes the system prompt.
    Everything from '# Input' onward becomes the user message.
    """
    marker = "# Input"
    idx = template_body.find(marker)
    if idx == -1:
        return ("", template_body)
    system = template_body[:idx].strip()
    user = template_body[idx:].strip()
    return (system, user)


def _parse_json_response(response_text: str, episode_id: str) -> dict:
    """Parse JSON from LLM response, stripping markdown code fences if present."""
    import re as _re

    text = response_text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    elif text.startswith("```"):
        text = text[len("```") :].strip()

    if text.endswith("```"):
        text = text[: -len("```")].strip()

    if text and not text.startswith("{") and not text.startswith("["):
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            text = text[first_brace : last_brace + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try cleaning trailing commas
        cleaned = _re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON response for %s: %s", episode_id, e)
            logger.error("Response text (first 500 chars): %s", response_text[:500])
            raise


def _load_corrected_document(
    structured_path: Path,
    episode_id: str,
) -> CorrectedTranscriptDocument | None:
    """Load the structured correction when available; legacy text remains supported."""
    if not structured_path.exists():
        return None
    document = CorrectedTranscriptDocument.model_validate_json(
        structured_path.read_text(encoding="utf-8")
    )
    if document.episode_id != episode_id:
        raise ValueError(
            f"Structured corrected transcript belongs to {document.episode_id}, "
            f"expected {episode_id}"
        )
    return document


def _render_segment_input(
    corrected_text: str,
    corrected_doc: CorrectedTranscriptDocument | None,
) -> str:
    """Give the model exact segment boundaries instead of asking it to reconstruct them."""
    if corrected_doc is None:
        return corrected_text
    payload = {
        "schema_version": 1,
        "episode_id": corrected_doc.episode_id,
        "segments": [
            {
                "segment_id": segment.segment_id,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "text": segment.corrected_text,
                "status": segment.status,
                "severity": segment.severity,
                "flags": segment.flags,
            }
            for segment in corrected_doc.segments
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_story_inventory(
    story_doc: StoryDocument,
    corrected_doc: CorrectedTranscriptDocument | None,
) -> StoryDocument:
    """Assign stable IDs and derive source fields from authoritative transcript segments."""
    for index, story in enumerate(story_doc.stories, start=1):
        story.story_id = f"s{index:02d}"
        story.order = index

    if corrected_doc is None:
        return story_doc

    segment_by_id = {segment.segment_id: segment for segment in corrected_doc.segments}
    referenced_ids: list[str] = []
    normalized_stories: list[Story] = []
    for story in story_doc.stories:
        if not story.source_segment_ids:
            raise ValueError(
                f"Story {story.story_id} must include source_segment_ids when a structured "
                "corrected transcript is available"
            )
        unknown = [sid for sid in story.source_segment_ids if sid not in segment_by_id]
        if unknown:
            raise ValueError(f"Story {story.story_id} references unknown segments: {unknown}")

        segments = [segment_by_id[sid] for sid in story.source_segment_ids]
        referenced_ids.extend(story.source_segment_ids)
        story.source_text = " ".join(segment.corrected_text.strip() for segment in segments)
        story.text_de = story.source_text
        story.source_start_seconds = segments[0].start_seconds
        story.source_end_seconds = segments[-1].end_seconds
        story.source_flags = sorted(
            {
                flag
                for segment in segments
                for flag in (
                    [segment.status, segment.severity, *segment.flags]
                    if segment.status in {"uncertain", "unresolved"}
                    else segment.flags
                )
            }
        )
        if any(segment.status == "unresolved" for segment in segments):
            story.source_confidence = "low"
        elif any(segment.status == "uncertain" for segment in segments):
            story.source_confidence = "medium"
        else:
            story.source_confidence = "high"
        story.word_count = len(story.source_text.split())
        normalized_stories.append(story)

    expected_ids = [segment.segment_id for segment in corrected_doc.segments]
    if referenced_ids != expected_ids:
        missing = [sid for sid in expected_ids if sid not in referenced_ids]
        duplicates = sorted({sid for sid in referenced_ids if referenced_ids.count(sid) > 1})
        raise ValueError(
            "Story coverage must preserve every transcript segment exactly once "
            f"in source order; missing={missing}, duplicates={duplicates}"
        )

    story_doc.stories = normalized_stories
    story_doc.total_stories = len(normalized_stories)
    return story_doc
