"""Turkey-context adaptation: Cultural adaptation of Turkish translations with tiered rules."""

import hashlib
import json
import logging
import re
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

# Texts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 15_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AdaptationResult:
    """Summary of adaptation operation for one episode."""

    episode_id: str
    adapted_path: str
    diff_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0
    output_char_count: int = 0
    adaptation_count: int = 0
    tier1_count: int = 0
    tier2_count: int = 0
    segments_processed: int = 1
    skipped: bool = False


def adapt_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> AdaptationResult:
    """Adapt a Turkish translation for Turkey context using tiered rules.

    Reads the Turkish translation and German corrected transcript, applies
    tiered cultural adaptation rules, writes the adapted script and diff.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-adapt even if output exists.

    Returns:
        AdaptationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
        FileNotFoundError: If translation or corrected transcript missing.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Allow both TRANSLATED and ADAPTED status
    if episode.status not in (EpisodeStatus.TRANSLATED, EpisodeStatus.ADAPTED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'translated' or 'adapted'. Use --force to override."
        )

    # Check Review Gate 1 approval (correction must be approved)
    if episode.status == EpisodeStatus.TRANSLATED and not force:
        from btcedu.core.reviewer import has_pending_review
        from btcedu.models.review import ReviewStatus, ReviewTask

        # Check if there's a pending review for correction
        if has_pending_review(session, episode_id):
            raise ValueError(
                f"Episode {episode_id} has pending review. "
                "Adaptation cannot proceed until reviews are resolved."
            )

        # Verify correction was approved
        approved_correct = (
            session.query(ReviewTask)
            .filter(
                ReviewTask.episode_id == episode_id,
                ReviewTask.stage == "correct",
                ReviewTask.status == ReviewStatus.APPROVED.value,
            )
            .first()
        )

        if not approved_correct:
            raise ValueError(
                f"Episode {episode_id} correction has not been approved. "
                "Adaptation cannot proceed until Review Gate 1 is approved."
            )

    # Resolve paths
    translation_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
    if not translation_path.exists():
        raise FileNotFoundError(
            f"Turkish translation not found for episode {episode_id}: {translation_path}"
        )

    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(
            f"Corrected German transcript not found for episode {episode_id}: {corrected_path}"
        )

    adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    diff_path = Path(settings.outputs_dir) / episode_id / "review" / "adaptation_diff.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "adapt_provenance.json"
    )

    # Load and register prompt via PromptRegistry
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "adapt.md"
    prompt_version = registry.register_version("adapt", template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Compute input content hashes for idempotency
    translation_text = translation_path.read_text(encoding="utf-8")
    german_text = corrected_path.read_text(encoding="utf-8")

    translation_hash = hashlib.sha256(translation_text.encode("utf-8")).hexdigest()
    german_hash = hashlib.sha256(german_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_adaptation_current(
        adapted_path,
        provenance_path,
        translation_hash,
        german_hash,
        prompt_content_hash,
    ):
        logger.info("Adaptation is current for %s (use --force to re-adapt)", episode_id)
        existing_adapted = adapted_path.read_text(encoding="utf-8")
        existing_diff = {}
        if diff_path.exists():
            existing_diff = json.loads(diff_path.read_text(encoding="utf-8"))
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

        return AdaptationResult(
            episode_id=episode_id,
            adapted_path=str(adapted_path),
            diff_path=str(diff_path) if diff_path.exists() else "",
            provenance_path=str(provenance_path),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            input_char_count=len(translation_text),
            output_char_count=len(existing_adapted),
            adaptation_count=existing_diff.get("summary", {}).get("total_adaptations", 0),
            tier1_count=existing_diff.get("summary", {}).get("tier1_count", 0),
            tier2_count=existing_diff.get("summary", {}).get("tier2_count", 0),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.ADAPT,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "adapt")
        if reviewer_feedback:
            feedback_block = (
                "## Revisor Geri Bildirimi (lütfen bu düzeltmeleri uygulayın)\n\n"
                f"{reviewer_feedback}\n\n"
                "Önemli: Bu geri bildirimi çıktıda aynen aktarmayın, "
                "yalnızca düzeltme kılavuzu olarak kullanın."
            )
            template_body = template_body.replace("{{ reviewer_feedback }}", feedback_block)
        else:
            template_body = template_body.replace("{{ reviewer_feedback }}", "")

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Segment text if needed
        segments = _segment_text(translation_text)

        # Process each segment
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        adapted_segments: list[str] = []

        for i, segment in enumerate(segments):
            # For multi-segment: include full German text as reference (simplification)
            # A more sophisticated implementation would align German segments with Turkish segments
            user_message = user_template.replace("{{ translation }}", segment).replace(
                "{{ original_german }}", german_text
            )

            # Dry-run path
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_adapt_{i}.json"
                if settings.dry_run
                else None
            )

            response: ClaudeResponse = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
            )

            adapted_segments.append(response.text)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

            logger.info(
                "Segment %d/%d: %d in, %d out, $%.4f",
                i + 1,
                len(segments),
                response.input_tokens,
                response.output_tokens,
                response.cost_usd,
            )

        # Reassemble adapted text
        adapted_text = "\n\n".join(adapted_segments)

        # Compute adaptation diff
        diff_data = compute_adaptation_diff(translation_text, adapted_text, episode_id)

        adaptation_count = diff_data["summary"]["total_adaptations"]
        tier1_count = diff_data["summary"]["tier1_count"]
        tier2_count = diff_data["summary"]["tier2_count"]

        logger.info(
            "Adaptation complete: %d adaptations (T1: %d, T2: %d)",
            adaptation_count,
            tier1_count,
            tier2_count,
        )

        # Write output files
        adapted_path.parent.mkdir(parents=True, exist_ok=True)
        adapted_path.write_text(adapted_text, encoding="utf-8")

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "adapt",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "adapt",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(translation_path), str(corrected_path)],
            "input_content_hashes": {
                "translation": translation_hash,
                "german": german_hash,
            },
            "output_files": [str(adapted_path), str(diff_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(segments),
            "adaptation_summary": {
                "total_adaptations": adaptation_count,
                "tier1_count": tier1_count,
                "tier2_count": tier2_count,
            },
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="adapt",
            file_path=str(adapted_path),
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
        episode.status = EpisodeStatus.ADAPTED
        episode.error_message = None
        session.commit()

        logger.info(
            "Adapted %s (%d→%d chars, %d adaptations, $%.4f)",
            episode_id,
            len(translation_text),
            len(adapted_text),
            adaptation_count,
            total_cost,
        )

        return AdaptationResult(
            episode_id=episode_id,
            adapted_path=str(adapted_path),
            diff_path=str(diff_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=len(translation_text),
            output_char_count=len(adapted_text),
            adaptation_count=adaptation_count,
            tier1_count=tier1_count,
            tier2_count=tier2_count,
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = f"Adaptation failed: {e}"
        session.commit()
        logger.error("Adaptation failed for %s: %s", episode_id, e)
        raise


def _is_adaptation_current(
    adapted_path: Path,
    provenance_path: Path,
    translation_hash: str,
    german_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing adaptation is still valid.

    Returns True (skip) if ALL of:
    1. adapted_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hashes match
    """
    if not adapted_path.exists():
        return False

    # Check for stale marker
    stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Adaptation marked stale (upstream change), will reprocess")
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

    stored_hashes = provenance.get("input_content_hashes", {})
    if stored_hashes.get("translation") != translation_hash:
        logger.info("Translation content hash mismatch (translation was updated)")
        return False

    if stored_hashes.get("german") != german_hash:
        logger.info("German content hash mismatch (corrected transcript was updated)")
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


def compute_adaptation_diff(
    translation: str,
    adapted: str,
    episode_id: str,
) -> dict:
    """Compute adaptation diff by parsing [T1]/[T2] tags in adapted text.

    Returns:
        {
            "episode_id": str,
            "original_length": int,
            "adapted_length": int,
            "adaptations": [
                {
                    "tier": "T1" | "T2",
                    "category": str,
                    "original": str,
                    "adapted": str,
                    "context": str,
                    "position": {"start": int, "end": int}
                },
                ...
            ],
            "summary": {
                "total_adaptations": int,
                "tier1_count": int,
                "tier2_count": int,
                "by_category": {...}
            }
        }
    """
    adaptations = []

    # Regex to match [T1: ...] or [T2: ...]
    pattern = r"\[(T1|T2):\s*([^\]]+)\]"

    for match in re.finditer(pattern, adapted):
        tier = match.group(1)
        content = match.group(2).strip()
        start = match.start()
        end = match.end()

        # Extract context (50 chars before/after)
        context_start = max(0, start - 50)
        context_end = min(len(adapted), end + 50)
        context = adapted[context_start:context_end]

        # Classify category from content
        category = _classify_adaptation(content)

        # Extract original vs adapted (if format is "original → adapted")
        if "→" in content:
            parts = content.split("→", 1)
            original_text = parts[0].strip().strip('"').strip("'")
            adapted_text = parts[1].strip().strip('"').strip("'")
        else:
            # No arrow: content is the adapted replacement
            original_text = ""
            adapted_text = content

        adaptations.append(
            {
                "tier": tier,
                "category": category,
                "original": original_text,
                "adapted": adapted_text,
                "context": context,
                "position": {"start": start, "end": end},
            }
        )

    # Summarize
    tier1_count = sum(1 for a in adaptations if a["tier"] == "T1")
    tier2_count = sum(1 for a in adaptations if a["tier"] == "T2")

    by_category: dict[str, int] = {}
    for a in adaptations:
        cat = a["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    return {
        "episode_id": episode_id,
        "original_length": len(translation),
        "adapted_length": len(adapted),
        "adaptations": adaptations,
        "summary": {
            "total_adaptations": len(adaptations),
            "tier1_count": tier1_count,
            "tier2_count": tier2_count,
            "by_category": by_category,
        },
    }


def _classify_adaptation(content: str) -> str:
    """Classify adaptation by analyzing the tag content.

    Categories:
    - institution_replacement: BaFin, Sparkasse, etc.
    - currency_conversion: EUR → TRY/USD
    - tone_adjustment: "ton düzeltmesi"
    - legal_removal: "[kaldırıldı: ..."
    - cultural_reference: "kültürel uyarlama", "kültürel referans"
    - regulatory_context: "düzenleme", "mevzuat"
    - other: fallback
    """
    content_lower = content.lower()

    if "kaldırıldı" in content_lower or "[removed" in content_lower:
        return "legal_removal"
    elif "ton düzeltmesi" in content_lower or "tone" in content_lower:
        return "tone_adjustment"
    elif "kültürel" in content_lower or "cultural" in content_lower:
        return "cultural_reference"
    elif any(
        inst in content_lower
        for inst in ["bafin", "sparkasse", "bundesbank", "spk", "merkez bankası"]
    ):
        return "institution_replacement"
    elif any(curr in content_lower for curr in ["eur", "usd", "tl", "€", "$", "₺"]):
        return "currency_conversion"
    elif "düzenleme" in content_lower or "mevzuat" in content_lower or "regulat" in content_lower:
        return "regulatory_context"
    else:
        return "other"
