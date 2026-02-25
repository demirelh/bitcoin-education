"""Turkish translation: Faithful German→Turkish translation of corrected transcripts."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
from btcedu.services.claude_service import ClaudeResponse, call_claude

logger = logging.getLogger(__name__)

# Transcripts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 15_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TranslationResult:
    """Summary of translation operation for one episode."""

    episode_id: str
    translated_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0
    output_char_count: int = 0
    segments_processed: int = 1
    skipped: bool = False


def translate_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> TranslationResult:
    """Translate a corrected German transcript to Turkish.

    Reads the corrected German transcript, sends it to Claude for faithful
    translation to Turkish, writes the translated transcript and provenance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-translate even if output exists.

    Returns:
        TranslationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Allow both CORRECTED and TRANSLATED status:
    # - CORRECTED: Normal first-time translation (after Review Gate 1 approval)
    # - TRANSLATED: Allow idempotent re-runs (useful for testing, manual re-translation)
    # The _is_translation_current() check will skip if output is already current.
    if episode.status not in (EpisodeStatus.CORRECTED, EpisodeStatus.TRANSLATED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'corrected' or 'translated'. Use --force to override."
        )

    # Check Review Gate 1 approval (unless episode already translated or force flag)
    # Per MASTERPLAN §3.1, translation must not proceed until Review Gate 1 is approved.
    if episode.status == EpisodeStatus.CORRECTED and not force:
        from btcedu.core.reviewer import has_pending_review

        # First check if there's a pending review (not yet approved/rejected)
        if has_pending_review(session, episode_id):
            raise ValueError(
                f"Episode {episode_id} has pending review for correction stage. "
                "Translation cannot proceed until Review Gate 1 is approved."
            )

        # Verify at least one approved review exists for the correct stage
        from btcedu.models.review import ReviewTask, ReviewStatus  # noqa: I001

        approved_review = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == "correct",
                ReviewTask.status == ReviewStatus.APPROVED.value,
            )
            .first()
        )

        if not approved_review:
            raise ValueError(
                f"Episode {episode_id} correction has not been approved. "
                "Translation cannot proceed until Review Gate 1 is approved."
            )

    # Resolve paths
    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(
            f"Corrected transcript not found for episode {episode_id}: {corrected_path}"
        )

    translated_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "translate_provenance.json"
    )

    # Load and register prompt via PromptRegistry
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "translate.md"
    prompt_version = registry.register_version("translate", template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Compute input content hash for idempotency
    corrected_text = corrected_path.read_text(encoding="utf-8")
    input_content_hash = hashlib.sha256(corrected_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_translation_current(
        translated_path, provenance_path, input_content_hash, prompt_content_hash
    ):
        logger.info("Translation is current for %s (use --force to re-translate)", episode_id)
        existing_translated = translated_path.read_text(encoding="utf-8")
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        return TranslationResult(
            episode_id=episode_id,
            translated_path=str(translated_path),
            provenance_path=str(provenance_path),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            input_char_count=len(corrected_text),
            output_char_count=len(existing_translated),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSLATE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "translate")
        if reviewer_feedback:
            feedback_block = (
                "## Reviewer Feedback (please apply these corrections)\n\n"
                f"{reviewer_feedback}\n\n"
                "Important: Treat this feedback as correction guidance. "
                "Do not include the feedback text verbatim in your output."
            )
            template_body = template_body.replace("{{ reviewer_feedback }}", feedback_block)
        else:
            template_body = template_body.replace("{{ reviewer_feedback }}", "")

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Segment transcript if needed
        segments = _segment_text(corrected_text)

        # Process each segment
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        translated_segments: list[str] = []

        for i, segment in enumerate(segments):
            user_message = user_template.replace("{{ transcript }}", segment)

            # Dry-run path
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_translate_{i}.json"
                if settings.dry_run
                else None
            )

            response: ClaudeResponse = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
            )

            translated_segments.append(response.text)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

        # Reassemble translated text
        translated_text = "\n\n".join(translated_segments)

        # Write output files
        translated_path.parent.mkdir(parents=True, exist_ok=True)
        translated_path.write_text(translated_text, encoding="utf-8")

        # Mark downstream adaptation as stale if it exists (cascade invalidation)
        adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
        if adapted_path.exists():
            stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
            stale_data = {
                "invalidated_at": _utcnow().isoformat(),
                "invalidated_by": "translate",
                "reason": "translation_changed",
            }
            stale_marker.parent.mkdir(parents=True, exist_ok=True)
            stale_marker.write_text(
                json.dumps(stale_data, indent=2), encoding="utf-8"
            )
            logger.info("Marked downstream adaptation as stale: %s", adapted_path.name)

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "translate",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "translate",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(corrected_path)],
            "input_content_hash": input_content_hash,
            "output_files": [str(translated_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(segments),
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="translate",
            file_path=str(translated_path),
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
        episode.status = EpisodeStatus.TRANSLATED
        episode.error_message = None
        session.commit()

        logger.info(
            "Translated transcript for %s (%d→%d chars, $%.4f)",
            episode_id,
            len(corrected_text),
            len(translated_text),
            total_cost,
        )

        return TranslationResult(
            episode_id=episode_id,
            translated_path=str(translated_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=len(corrected_text),
            output_char_count=len(translated_text),
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise


def _is_translation_current(
    translated_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing translation is still valid.

    Returns True (skip) if ALL of:
    1. translated_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not translated_path.exists():
        return False

    # Check for stale marker
    stale_marker = translated_path.parent / (translated_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Translation marked stale: %s", stale_marker.read_text(encoding="utf-8"))
        stale_marker.unlink()  # Remove marker after detection
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
        # Fallback: use empty system prompt, entire body is user message
        return ("", template_body)
    system = template_body[:idx].strip()
    user = template_body[idx:].strip()
    return (system, user)


def _segment_text(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split text into segments at paragraph breaks.

    If the text is shorter than the limit, returns a single-element list.
    Splits at double-newline paragraph boundaries to preserve context.
    If a single paragraph exceeds the limit, splits at sentence boundaries.

    Args:
        text: The full transcript text.
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
