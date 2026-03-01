"""Chapterization: Transform adapted script into structured production JSON."""

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
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.services.claude_service import ClaudeResponse, call_claude

logger = logging.getLogger(__name__)

# Texts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 15_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ChapterizationResult:
    """Summary of chapterization operation for one episode."""

    episode_id: str
    chapters_path: str
    provenance_path: str
    chapter_count: int = 0
    estimated_duration_seconds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    segments_processed: int = 1
    skipped: bool = False


def chapterize_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> ChapterizationResult:
    """Chapterize adapted script into structured production JSON.

    Reads the adapted Turkish script and transforms it into a structured
    chapter document with narration, visuals, overlays, and timing guidance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-chapterize even if output exists.

    Returns:
        ChapterizationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
        FileNotFoundError: If adapted script missing.
        ValidationError: If LLM produces invalid JSON.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Allow both ADAPTED and CHAPTERIZED status
    if episode.status not in (EpisodeStatus.ADAPTED, EpisodeStatus.CHAPTERIZED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'adapted' or 'chapterized'. Use --force to override."
        )

    # Check Review Gate 2 approval (adaptation must be approved)
    if episode.status == EpisodeStatus.ADAPTED and not force:
        from btcedu.core.reviewer import has_pending_review

        # Check if there's a pending review for adaptation
        if has_pending_review(session, episode_id):
            raise ValueError(
                f"Episode {episode_id} has pending review. "
                "Chapterization cannot proceed until reviews are resolved."
            )

        # Verify adaptation was approved
        approved_adapt = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == "adapt",
                ReviewTask.status == ReviewStatus.APPROVED.value,
            )
            .first()
        )

        if not approved_adapt:
            raise ValueError(
                f"Episode {episode_id} adaptation has not been approved. "
                "Chapterization cannot proceed until Review Gate 2 is approved."
            )

    # Resolve paths
    adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    if not adapted_path.exists():
        raise FileNotFoundError(
            f"Adapted script not found for episode {episode_id}: {adapted_path}"
        )

    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "chapterize_provenance.json"
    )

    # Load and register prompt via PromptRegistry
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "chapterize.md"
    prompt_version = registry.register_version("chapterize", template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Compute input content hash for idempotency
    adapted_text = adapted_path.read_text(encoding="utf-8")
    adapted_hash = hashlib.sha256(adapted_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_chapterization_current(
        chapters_path, provenance_path, adapted_hash, prompt_content_hash
    ):
        logger.info("Chapterization is current for %s (use --force to re-chapterize)", episode_id)
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

        return ChapterizationResult(
            episode_id=episode_id,
            chapters_path=str(chapters_path),
            provenance_path=str(provenance_path),
            chapter_count=existing_provenance.get("chapter_count", 0),
            estimated_duration_seconds=existing_provenance.get("estimated_duration_seconds", 0),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.CHAPTERIZE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Segment text if needed (unlikely for adapted scripts, but handle it)
        segments = _segment_script(adapted_text)

        # Process each segment
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        all_chapters = []

        for i, segment in enumerate(segments):
            user_message = user_template.replace("{{episode_id}}", episode_id).replace(
                "{{adapted_script}}", segment
            )

            # Dry-run path
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_chapterize_{i}.json"
                if settings.dry_run
                else None
            )

            response: ClaudeResponse = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
                max_tokens=16384,  # chapter JSON needs much more than default 4096
            )

            # Parse JSON response
            chapter_data = _parse_json_response(response.text, episode_id, segment, settings)

            # Validate with Pydantic
            try:
                chapter_doc = ChapterDocument.model_validate(chapter_data)
            except ValidationError as e:
                logger.warning("Chapterization output failed validation: %s", e)
                # Retry once with corrective prompt
                chapter_data = _retry_with_correction(
                    e, episode_id, segment, system_prompt, settings
                )
                # Validate retry
                chapter_doc = ChapterDocument.model_validate(chapter_data)

            # For multi-segment: merge chapters
            if len(segments) > 1:
                all_chapters.extend(chapter_doc.chapters)
            else:
                all_chapters = chapter_doc.chapters

            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

            logger.info(
                "Segment %d/%d: %d chapters, %d in, %d out, $%.4f",
                i + 1,
                len(segments),
                len(chapter_doc.chapters),
                response.input_tokens,
                response.output_tokens,
                response.cost_usd,
            )

        # Reassemble if multi-segment: re-number chapters sequentially
        if len(segments) > 1:
            for idx, ch in enumerate(all_chapters):
                ch.order = idx + 1
                ch.chapter_id = f"ch{ch.order:02d}"

            # Build final document
            total_duration = sum(ch.narration.estimated_duration_seconds for ch in all_chapters)
            final_chapter_doc = ChapterDocument(
                schema_version="1.0",
                episode_id=episode_id,
                title=chapter_doc.title,  # Use title from first segment
                total_chapters=len(all_chapters),
                estimated_duration_seconds=total_duration,
                chapters=all_chapters,
            )
        else:
            final_chapter_doc = chapter_doc

        # Validate duration estimates (recalculate from word count)
        for ch in final_chapter_doc.chapters:
            actual_word_count = len(ch.narration.text.split())
            expected_duration = _compute_duration_estimate(actual_word_count)
            # If LLM's estimate is off by >20%, log warning
            if abs(ch.narration.estimated_duration_seconds - expected_duration) > (
                expected_duration * 0.2
            ):
                logger.warning(
                    "Chapter %s duration mismatch: LLM=%ds, expected=%ds (word_count=%d)",
                    ch.chapter_id,
                    ch.narration.estimated_duration_seconds,
                    expected_duration,
                    actual_word_count,
                )

        logger.info(
            "Chapterization complete: %d chapters, ~%ds",
            final_chapter_doc.total_chapters,
            final_chapter_doc.estimated_duration_seconds,
        )

        # Write chapters.json
        chapters_path.parent.mkdir(parents=True, exist_ok=True)
        chapters_json = final_chapter_doc.model_dump(mode="json", by_alias=True)
        chapters_path.write_text(
            json.dumps(chapters_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "chapterize",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "chapterize",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(adapted_path)],
            "input_content_hash": adapted_hash,
            "output_files": [str(chapters_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(segments),
            "chapter_count": final_chapter_doc.total_chapters,
            "estimated_duration_seconds": final_chapter_doc.estimated_duration_seconds,
            "schema_version": "1.0",
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="chapterize",
            file_path=str(chapters_path),
            model=settings.claude_model,
            prompt_hash=prompt_content_hash,
            retrieval_snapshot_path=None,
        )
        session.add(artifact)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost

        # Update Episode
        episode.status = EpisodeStatus.CHAPTERIZED
        episode.error_message = None
        session.commit()

        # Mark downstream stages as stale (IMAGE_GEN, TTS if they exist)
        _mark_downstream_stale(episode_id, settings)

        logger.info(
            "Chapterized %s (%d chapters, ~%ds, $%.4f)",
            episode_id,
            final_chapter_doc.total_chapters,
            final_chapter_doc.estimated_duration_seconds,
            total_cost,
        )

        return ChapterizationResult(
            episode_id=episode_id,
            chapters_path=str(chapters_path),
            provenance_path=str(provenance_path),
            chapter_count=final_chapter_doc.total_chapters,
            estimated_duration_seconds=final_chapter_doc.estimated_duration_seconds,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = f"Chapterization failed: {e}"
        session.commit()
        logger.error("Chapterization failed for %s: %s", episode_id, e)
        raise


def _is_chapterization_current(
    chapters_path: Path,
    provenance_path: Path,
    adapted_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing chapterization is still valid.

    Returns True (skip) if ALL of:
    1. chapters_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not chapters_path.exists():
        return False

    # Check for stale marker
    stale_marker = chapters_path.parent / (chapters_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Chapterization marked stale (upstream change), will reprocess")
        stale_marker.unlink()  # Consume marker
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Provenance file corrupt or missing, will reprocess")
        return False

    if provenance.get("prompt_hash") != prompt_content_hash:
        logger.info("Prompt hash mismatch (prompt was updated)")
        return False

    if provenance.get("input_content_hash") != adapted_hash:
        logger.info("Adapted script content hash mismatch (adapted script was updated)")
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
        # Fallback: use empty system prompt, entire body is user message
        return ("", template_body)
    system = template_body[:idx].strip()
    user = template_body[idx:].strip()
    return (system, user)


def _segment_script(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split script into segments at paragraph breaks.

    If the text is shorter than the limit, returns a single-element list.
    Splits at double-newline paragraph boundaries to preserve context.
    If a single paragraph exceeds the limit, splits at sentence boundaries.

    Args:
        text: The full text.
        limit: Maximum characters per segment.

    Returns:
        List of text segments (non-empty).
    """
    # If text is short enough, return as single segment
    if len(text) <= limit:
        return [text]

    # Split into paragraphs
    paragraphs = text.split("\n\n")

    segments = []
    current_segment = []
    current_length = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # If single paragraph exceeds limit, split by sentences
        if para_len > limit:
            # Flush current segment
            if current_segment:
                segments.append("\n\n".join(current_segment))
                current_segment = []
                current_length = 0

            # Split long paragraph by sentences (approximate with ". ")
            sentences = para.split(". ")

            # If no sentence breaks found, fall back to character-based splitting
            if len(sentences) == 1:
                # No sentence breaks, split by chunks
                for i in range(0, para_len, limit):
                    segments.append(para[i : i + limit])
            else:
                # Has sentence breaks, split by sentences
                sent_segment = []
                sent_len = 0
                for sent in sentences:
                    if sent_len + len(sent) + 2 > limit and sent_segment:
                        # Flush sentence segment
                        segments.append(". ".join(sent_segment) + ".")
                        sent_segment = [sent]
                        sent_len = len(sent)
                    else:
                        sent_segment.append(sent)
                        sent_len += len(sent) + 2
                if sent_segment:
                    segments.append(". ".join(sent_segment) + ".")

        # Normal paragraph fits in current segment
        elif current_length + para_len + 2 <= limit:
            current_segment.append(para)
            current_length += para_len + 2

        # Start new segment
        else:
            if current_segment:
                segments.append("\n\n".join(current_segment))
            current_segment = [para]
            current_length = para_len

    # Flush remaining
    if current_segment:
        segments.append("\n\n".join(current_segment))

    return segments if segments else [text]  # Fallback: return original as single segment


def _compute_duration_estimate(word_count: int) -> int:
    """Estimate narration duration in seconds from Turkish word count.

    Turkish speech rate: ~150 words/minute.

    Args:
        word_count: Number of words in narration text

    Returns:
        Estimated duration in seconds (rounded to nearest integer)
    """
    WORDS_PER_MINUTE = 150
    duration_minutes = word_count / WORDS_PER_MINUTE
    duration_seconds = duration_minutes * 60
    return round(duration_seconds)


def _parse_json_response(
    response_text: str, episode_id: str, segment: str, settings: Settings
) -> dict:
    """Parse JSON from LLM response, stripping markdown code fences if present.

    Handles common LLM quirks: markdown fences, trailing commas, JS-style
    comments, and single-line // comments.

    Args:
        response_text: Raw LLM response
        episode_id: Episode ID for error messages
        segment: Input segment for error messages
        settings: Settings (unused, for future use)

    Returns:
        Parsed JSON dict

    Raises:
        json.JSONDecodeError: If JSON is malformed after cleanup
    """
    # Strip markdown code fences
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    elif text.startswith("```"):
        text = text[len("```") :].strip()

    if text.endswith("```"):
        text = text[: -len("```")].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to fix common LLM JSON quirks
        cleaned = _clean_json(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON response for %s: %s", episode_id, e)
            logger.error("Response text (first 500 chars): %s", response_text[:500])
            raise


def _clean_json(text: str) -> str:
    """Best-effort cleanup of LLM-generated JSON.

    Handles:
    - Single-line // comments
    - Trailing commas before } or ]
    """
    import re

    # Remove single-line // comments (but not inside strings)
    # Simple heuristic: remove // comments only when preceded by whitespace or start of line
    lines = []
    for line in text.split("\n"):
        # Skip full-line comments
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        # Remove inline // comments (rough: only if not inside a string value)
        # Find // that's not inside quotes
        in_string = False
        escape_next = False
        cut_pos = None
        for i, ch in enumerate(line):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
            elif ch == "/" and not in_string and i + 1 < len(line) and line[i + 1] == "/":
                cut_pos = i
                break
        if cut_pos is not None:
            line = line[:cut_pos].rstrip()
        lines.append(line)
    text = "\n".join(lines)

    # Remove trailing commas: ,  followed by } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text


def _retry_with_correction(
    validation_error: ValidationError,
    episode_id: str,
    segment: str,
    system_prompt: str,
    settings: Settings,
) -> dict:
    """Retry chapterization with corrective prompt after validation failure.

    Args:
        validation_error: The Pydantic validation error
        episode_id: Episode ID
        segment: The adapted script segment
        system_prompt: The system prompt
        settings: Application settings

    Returns:
        Parsed JSON dict (validated)

    Raises:
        ValidationError: If retry also fails
    """
    corrective_prompt = f"""
The previous JSON output had validation errors:

{validation_error}

Please correct the JSON and return a valid document matching the schema exactly.
Pay special attention to:
- All required fields present
- chapter_id values unique
- order values sequential (1, 2, 3, ...)
- total_chapters = len(chapters)
- estimated_duration_seconds = sum of chapter durations

Original input:
{segment}
"""

    logger.info("Retrying chapterization with corrective prompt for %s", episode_id)

    response: ClaudeResponse = call_claude(
        system_prompt=system_prompt,
        user_message=corrective_prompt,
        settings=settings,
        max_tokens=16384,
    )

    # Parse and return (let caller validate)
    return _parse_json_response(response.text, episode_id, segment, settings)


def _mark_downstream_stale(episode_id: str, settings: Settings) -> None:
    """Mark downstream stages as stale (IMAGE_GEN, TTS).

    Creates .stale marker files in images/ and tts/ directories if they exist.

    Args:
        episode_id: Episode ID
        settings: Application settings
    """
    imagegen_marker_path = Path(settings.outputs_dir) / episode_id / "images" / ".stale"
    tts_marker_path = Path(settings.outputs_dir) / episode_id / "tts" / ".stale"

    for marker_path in [imagegen_marker_path, tts_marker_path]:
        if marker_path.parent.exists():
            stale_data = {
                "invalidated_at": _utcnow().isoformat(),
                "invalidated_by": "chapterize",
                "reason": "chapters_changed",
            }
            marker_path.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
            logger.info("Marked downstream stage as stale: %s", marker_path.parent.name)
