"""Transcript correction: LLM-based ASR error correction with diff tracking."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
class CorrectionResult:
    """Summary of transcript correction for one episode."""

    episode_id: str
    corrected_path: str
    diff_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    change_count: int = 0
    input_char_count: int = 0
    output_char_count: int = 0


def correct_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> CorrectionResult:
    """Correct a Whisper transcript using Claude.

    Reads the cleaned German transcript, sends it to Claude for ASR
    error correction, writes the corrected transcript and a structured
    diff JSON.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-correct even if output exists.

    Returns:
        CorrectionResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.TRANSCRIBED, EpisodeStatus.CORRECTED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'transcribed'. Use --force to override."
        )

    # Resolve paths
    transcript_path = Path(episode.transcript_path) if episode.transcript_path else None
    if not transcript_path or not transcript_path.exists():
        raise ValueError(
            f"Transcript not found for episode {episode_id}: {episode.transcript_path}"
        )

    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    diff_path = Path(settings.outputs_dir) / episode_id / "review" / "correction_diff.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "correct_provenance.json"
    )

    # Load and register prompt via PromptRegistry
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "correct_transcript.md"
    prompt_version = registry.register_version(
        "correct_transcript", template_file, set_default=True
    )
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Compute input content hash for idempotency
    original_text = transcript_path.read_text(encoding="utf-8")
    input_content_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_correction_current(
        corrected_path, provenance_path, input_content_hash, prompt_content_hash
    ):
        logger.info("Correction is current for %s (use --force to re-correct)", episode_id)
        existing_corrected = corrected_path.read_text(encoding="utf-8")
        existing_diff = json.loads(diff_path.read_text(encoding="utf-8"))
        return CorrectionResult(
            episode_id=episode_id,
            corrected_path=str(corrected_path),
            diff_path=str(diff_path),
            provenance_path=str(provenance_path),
            change_count=existing_diff.get("summary", {}).get("total_changes", 0),
            input_char_count=len(original_text),
            output_char_count=len(existing_corrected),
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.CORRECT,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "correct")
        if reviewer_feedback:
            feedback_block = (
                "## Reviewer-Korrekturen "
                "(bitte diese Anmerkungen bei der Korrektur berücksichtigen)\n\n"
                f"{reviewer_feedback}"
            )
            template_body = template_body.replace("{{ reviewer_feedback }}", feedback_block)
        else:
            template_body = template_body.replace("{{ reviewer_feedback }}", "")

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Segment transcript if needed
        segments = _segment_transcript(original_text)

        # Process each segment
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        corrected_segments: list[str] = []

        for i, segment in enumerate(segments):
            user_message = user_template.replace("{{ transcript }}", segment)

            # Dry-run path
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_correct_{i}.json"
                if settings.dry_run
                else None
            )

            response: ClaudeResponse = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
            )

            corrected_segments.append(response.text)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

        # Reassemble corrected text
        corrected_text = "\n\n".join(corrected_segments)

        # Compute diff
        diff_data = compute_correction_diff(original_text, corrected_text, episode_id)

        # Write output files
        corrected_path.parent.mkdir(parents=True, exist_ok=True)
        corrected_path.write_text(corrected_text, encoding="utf-8")

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "correct",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "correct_transcript",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(transcript_path)],
            "input_content_hash": input_content_hash,
            "output_files": [str(corrected_path), str(diff_path)],
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
            artifact_type="correct",
            file_path=str(corrected_path),
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
        episode.status = EpisodeStatus.CORRECTED
        session.commit()

        logger.info(
            "Corrected transcript for %s (%d changes, $%.4f)",
            episode_id,
            diff_data["summary"]["total_changes"],
            total_cost,
        )

        return CorrectionResult(
            episode_id=episode_id,
            corrected_path=str(corrected_path),
            diff_path=str(diff_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            change_count=diff_data["summary"]["total_changes"],
            input_char_count=len(original_text),
            output_char_count=len(corrected_text),
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise


def _is_correction_current(
    corrected_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing correction is still valid.

    Returns True (skip) if ALL of:
    1. corrected_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not corrected_path.exists():
        return False

    # Check for stale marker
    stale_marker = corrected_path.parent / (corrected_path.name + ".stale")
    if stale_marker.exists():
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if provenance.get("prompt_hash") != prompt_content_hash:
        return False

    if provenance.get("input_content_hash") != input_content_hash:
        return False

    return True


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split rendered template into system prompt and user message.

    The template is split at the '# Transkript' header.
    Everything before it becomes the system prompt.
    Everything from '# Transkript' onward becomes the user message.
    """
    marker = "# Transkript"
    idx = template_body.find(marker)
    if idx == -1:
        # Fallback: use empty system prompt, entire body is user message
        return ("", template_body)
    system = template_body[:idx].strip()
    user = template_body[idx:].strip()
    return (system, user)


def _segment_transcript(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split transcript into segments at paragraph breaks.

    If the text is shorter than the limit, returns a single-element list.
    Splits at double-newline paragraph boundaries to preserve context.
    If no paragraph breaks exist, splits at the limit boundary.

    Args:
        text: The full transcript text.
        limit: Maximum characters per segment.

    Returns:
        List of text segments.
    """
    if len(text) <= limit:
        return [text]

    paragraphs = text.split("\n\n")
    segments: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        # Account for the \n\n separator
        sep_len = 2 if current else 0

        if current_len + sep_len + para_len > limit and current:
            segments.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += sep_len + para_len

    if current:
        segments.append("\n\n".join(current))

    # Handle edge case: a single paragraph longer than the limit
    # Split it at the character limit
    final_segments: list[str] = []
    for seg in segments:
        if len(seg) <= limit:
            final_segments.append(seg)
        else:
            # Force-split at limit boundaries
            for start in range(0, len(seg), limit):
                final_segments.append(seg[start : start + limit])

    return final_segments


def compute_correction_diff(
    original: str,
    corrected: str,
    episode_id: str,
    context_words: int = 5,
) -> dict:
    """Compute structured diff between original and corrected transcript.

    Uses difflib.SequenceMatcher on word-level tokens.

    Args:
        original: The original transcript text.
        corrected: The corrected transcript text.
        episode_id: Episode identifier for the output.
        context_words: Number of surrounding words for context.

    Returns:
        Dict matching the correction_diff.json format from MASTERPLAN §5A.
    """
    orig_words = original.split()
    corr_words = corrected.split()

    matcher = SequenceMatcher(None, orig_words, corr_words)
    changes: list[dict] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        orig_span = " ".join(orig_words[i1:i2])
        corr_span = " ".join(corr_words[j1:j2])

        # Build context from corrected words (or original for deletes)
        if tag == "delete":
            ctx_start = max(0, i1 - context_words)
            ctx_end = min(len(orig_words), i2 + context_words)
            context = " ".join(orig_words[ctx_start:ctx_end])
        else:
            ctx_start = max(0, j1 - context_words)
            ctx_end = min(len(corr_words), j2 + context_words)
            context = " ".join(corr_words[ctx_start:ctx_end])

        change = {
            "type": tag,  # "replace", "insert", "delete"
            "original": orig_span,
            "corrected": corr_span,
            "context": f"...{context}...",
            "position": {"start_word": i1, "end_word": i2},
            "category": "auto",
        }
        changes.append(change)

    # Summary
    by_type: dict[str, int] = {}
    for c in changes:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1

    return {
        "episode_id": episode_id,
        "original_length": len(original),
        "corrected_length": len(corrected),
        "changes": changes,
        "summary": {
            "total_changes": len(changes),
            "by_type": by_type,
        },
    }
