"""Transcript correction: LLM-based ASR error correction with diff tracking."""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func
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
from btcedu.models.transcript_schema import (
    CorrectedTranscriptDocument,
    CorrectedTranscriptSegment,
    TranscriptAnalysisDocument,
    TranscriptDocument,
    TranscriptVerificationDocument,
)
from btcedu.services.claude_service import ClaudeResponse, call_claude
from btcedu.services.errors import ErrorCategory, PipelineError

logger = logging.getLogger(__name__)

# Transcripts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 15_000
STRUCTURED_CORRECTION_FILENAME = "transcript.corrected.structured.de.json"

# How often a batch the model keeps mishandling may be halved before its
# segments are kept unchanged. Each level doubles the number of paid calls in
# the worst case, so this trades a bounded amount of money against the chance
# that a smaller batch succeeds: three levels turns one batch into at most
# eight, which was enough for every failure observed so far.
_MAX_CORRECTION_SPLIT_DEPTH = 3

# Tokens the ASR corrector must NEVER alter: numbers/dates and sport-tournament
# names. The corrector fixes spelling/punctuation only; it is not a fact-checker.
# Guards against LLM over-correction (e.g. "Fußball-WM" -> "Fußball-EM",
# "12. Juli" -> "13. Juli") which would silently corrupt every downstream stage.
_PROTECTED_TOKEN_RE = re.compile(
    r"\d"  # any digit: dates, times, results, percentages, amounts
    r"|\b(?:WM|EM|Welt(?:meister\w*|meisterschaft)|Europa(?:meister\w*|meisterschaft))\b",
    re.IGNORECASE,
)
_TURKISH_TRANSLATION_MARKERS = {
    "ile",
    "için",
    "olarak",
    "değil",
    "bugün",
    "yarın",
    "kadar",
    "konut",
    "oldu",
}


def _has_protected_token(span: str) -> bool:
    return bool(_PROTECTED_TOKEN_RE.search(span))


def _protected_tokens(span: str) -> tuple[str, ...]:
    """Return factual tokens whose values must remain unchanged."""
    return tuple(match.group(0).casefold() for match in _PROTECTED_TOKEN_RE.finditer(span))


def _revert_protected_token_changes(original: str, corrected: str) -> str:
    """Restore original wording for any ASR-correction edit that touched a
    protected token (numbers/dates or sport-tournament names).

    The correction prompt forbids factual changes, but the LLM occasionally
    over-corrects (turning "Fußball-WM" into "Fußball-EM", or shifting a date).
    Such edits corrupt every downstream stage. This deterministic guard aligns
    the original and corrected text word-by-word and reverts only the
    ``replace`` spans that involve a protected token — all other corrections
    (spelling, punctuation, names) are preserved untouched.
    """
    orig_words = original.split()
    corr_words = corrected.split()
    if not orig_words or not corr_words:
        return corrected

    result = corrected
    matcher = SequenceMatcher(None, orig_words, corr_words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        orig_span = " ".join(orig_words[i1:i2])
        corr_span = " ".join(corr_words[j1:j2])
        if not corr_span or orig_span == corr_span:
            continue
        # A protected token that is present unchanged may share an opcode with
        # a legitimate surrounding repair (for example "das WM-Titel" ->
        # "der Stolz über den WM-Titel"). Only reject the edit when the
        # protected token values themselves changed.
        if _protected_tokens(orig_span) != _protected_tokens(corr_span):
            if corr_span in result:
                result = result.replace(corr_span, orig_span, 1)
                logger.info(
                    "Reverted protected-token over-correction: %r -> %r",
                    corr_span,
                    orig_span,
                )
    return result


def _contains_unexpected_turkish(original: str, corrected: str) -> bool:
    """Detect Turkish translation fragments newly introduced into German source text."""
    original_tokens = set(re.findall(r"[^\W\d_]+", original.casefold(), re.UNICODE))
    corrected_tokens = set(re.findall(r"[^\W\d_]+", corrected.casefold(), re.UNICODE))
    added_tokens = corrected_tokens - original_tokens
    return bool(added_tokens & _TURKISH_TRANSLATION_MARKERS) or bool(
        re.search(r"[ğıİşç]", corrected) and not re.search(r"[ğıİşç]", original)
    )


def _is_supported_by_verification(corrected: str, verifications: list[dict]) -> bool:
    """Return true when secondary ASR contains the corrected segment."""
    from btcedu.core.transcript_verifier import compare_transcripts

    return any(
        item.get("status") == "success"
        and compare_transcripts(corrected, str(item.get("secondary_text") or "")).severity == "none"
        for item in verifications
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CorrectionResult:
    """Summary of transcript correction for one episode."""

    episode_id: str
    corrected_path: str
    structured_path: str
    diff_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    change_count: int = 0
    input_char_count: int = 0
    output_char_count: int = 0


class _ModelCorrectionSegment(BaseModel):
    segment_id: str
    corrected_text: str = Field(..., min_length=1)
    status: Literal["verified", "corrected", "uncertain", "unresolved"]
    severity: Literal["none", "minor", "major", "critical"]
    flags: list[str] = Field(default_factory=list)
    reason: str | None = None
    verification_ids: list[str] = Field(default_factory=list)


class _ModelCorrectionResponse(BaseModel):
    segments: list[_ModelCorrectionSegment]


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
    structured_path = Path(settings.transcripts_dir) / episode_id / STRUCTURED_CORRECTION_FILENAME
    diff_path = Path(settings.outputs_dir) / episode_id / "review" / "correction_diff.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "correct_provenance.json"
    )

    # Load and register prompt via PromptRegistry (with profile-namespaced fallback)
    registry = PromptRegistry(session)
    profile = getattr(episode, "content_profile", None)
    # Resolve profile namespace from profile object if available
    try:
        from btcedu.profiles import get_registry as get_profile_registry

        pr = get_profile_registry(settings)
        profile_obj = pr.get(profile) if profile else None
        profile_namespace = getattr(profile_obj, "prompt_namespace", None) if profile_obj else None
    except Exception:
        profile_namespace = None

    template_file = registry.resolve_template_path(
        "correct_transcript.md", profile=profile_namespace
    )
    prompt_name = "correct_transcript"
    if profile_namespace and (TEMPLATES_DIR / profile_namespace / "correct_transcript.md").exists():
        prompt_name = f"{profile_namespace}/correct_transcript"
    prompt_version = registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    transcript_document = _load_transcript_document(settings, episode_id)
    analysis = _load_optional_analysis(settings, episode_id)
    verification = _load_optional_verification(settings, episode_id)
    original_text = transcript_document.text
    input_content_hash = _correction_input_hash(
        transcript_document,
        analysis,
        verification,
    )

    # Idempotency check
    if (
        not force
        and structured_path.exists()
        and _is_correction_current(
            corrected_path,
            provenance_path,
            input_content_hash,
            prompt_content_hash,
        )
    ):
        logger.info("Correction is current for %s (use --force to re-correct)", episode_id)
        existing_corrected = corrected_path.read_text(encoding="utf-8")
        existing_diff = json.loads(diff_path.read_text(encoding="utf-8"))
        episode.status = EpisodeStatus.CORRECTED
        episode.error_message = None
        session.commit()
        return CorrectionResult(
            episode_id=episode_id,
            corrected_path=str(corrected_path),
            structured_path=str(structured_path),
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
    all_responses: list[ClaudeResponse] = []

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        feedback_parts = [
            feedback
            for stage in ("transcript_qa", "correct")
            if (feedback := get_latest_reviewer_feedback(session, episode_id, stage))
        ]
        reviewer_feedback = "\n\n".join(feedback_parts)
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

        payloads = _build_correction_payloads(
            transcript_document,
            analysis,
            verification,
        )
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        corrected_segments: list[CorrectedTranscriptSegment] = []

        for index, payload in enumerate(payloads):
            user_message = _render_user_message(user_template, payload)

            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_correct_{index}.json"
                if settings.dry_run
                else None
            )
            if settings.dry_run:
                call_claude(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    settings=settings,
                    dry_run_path=dry_run_path,
                    json_mode=True,
                )
                model_response = _dry_run_response(payload)
                responses: list[ClaudeResponse] = []
            else:
                model_response, responses = _call_structured_correction(
                    system_prompt,
                    user_template,
                    payload,
                    settings,
                    episode_id,
                    budget_check=lambda pending, spent=total_cost: _ensure_cost_budget(
                        session,
                        episode,
                        settings,
                        spent + pending,
                    ),
                    response_sink=all_responses,
                )

            corrected_segments.extend(
                _finalize_correction_segments(
                    payload,
                    model_response,
                )
            )
            total_input_tokens += sum(response.input_tokens for response in responses)
            total_output_tokens += sum(response.output_tokens for response in responses)
            total_cost += sum(response.cost_usd for response in responses)

        corrected_text = "\n\n".join(segment.corrected_text for segment in corrected_segments)
        corrected_document = CorrectedTranscriptDocument(
            episode_id=episode_id,
            language=transcript_document.language,
            segments=corrected_segments,
            full_text=corrected_text,
        )

        # Compute diff
        diff_data = compute_correction_diff(original_text, corrected_text, episode_id)

        # Write output files
        corrected_path.parent.mkdir(parents=True, exist_ok=True)
        corrected_path.write_text(corrected_text, encoding="utf-8")
        structured_path.write_text(
            json.dumps(
                corrected_document.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Mark downstream translation as stale if it exists (cascade invalidation)
        translated_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
        if translated_path.exists():
            stale_marker = translated_path.parent / (translated_path.name + ".stale")
            stale_data = {
                "invalidated_at": _utcnow().isoformat(),
                "invalidated_by": "correct",
                "reason": "correction_changed",
            }
            stale_marker.parent.mkdir(parents=True, exist_ok=True)
            stale_marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
            logger.info("Marked downstream translation as stale: %s", translated_path.name)

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
            "input_files": _correction_input_files(settings, episode_id, transcript_path),
            "input_content_hash": input_content_hash,
            "output_files": [str(corrected_path), str(structured_path), str(diff_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(corrected_segments),
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
        session.add(
            ContentArtifact(
                episode_id=episode_id,
                artifact_type="corrected_transcript",
                file_path=str(structured_path),
                model=settings.claude_model,
                prompt_hash=prompt_content_hash,
                retrieval_snapshot_path=None,
            )
        )

        _mark_transcript_qa_stale(settings, episode_id)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost

        # Update Episode
        episode.status = EpisodeStatus.CORRECTED
        episode.error_message = None
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
            structured_path=str(structured_path),
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
        pipeline_run.input_tokens = sum(response.input_tokens for response in all_responses)
        pipeline_run.output_tokens = sum(response.output_tokens for response in all_responses)
        pipeline_run.estimated_cost_usd = sum(response.cost_usd for response in all_responses)
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        if isinstance(e, PipelineError) and e.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
        session.commit()
        raise


def _load_transcript_document(settings: Settings, episode_id: str) -> TranscriptDocument:
    from btcedu.core.transcriber import load_transcript_document

    return load_transcript_document(settings, episode_id)


def _load_optional_analysis(
    settings: Settings,
    episode_id: str,
) -> TranscriptAnalysisDocument | None:
    path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json"
    if not path.exists():
        return None
    return TranscriptAnalysisDocument.model_validate_json(path.read_text(encoding="utf-8"))


def _load_optional_verification(
    settings: Settings,
    episode_id: str,
) -> TranscriptVerificationDocument | None:
    path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json"
    if not path.exists():
        return None
    return TranscriptVerificationDocument.model_validate_json(path.read_text(encoding="utf-8"))


def _correction_input_hash(
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument | None,
    verification: TranscriptVerificationDocument | None,
) -> str:
    payload = {
        "transcript": document.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json") if analysis else None,
        "verification": verification.model_dump(mode="json") if verification else None,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _correction_input_files(
    settings: Settings,
    episode_id: str,
    transcript_path: Path,
) -> list[str]:
    candidates = [
        Path(settings.transcripts_dir) / episode_id / "transcript.structured.de.json",
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json",
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json",
    ]
    files = [str(path) for path in candidates if path.exists()]
    if not files:
        files.append(str(transcript_path))
    return files


def _build_correction_payloads(
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument | None,
    verification: TranscriptVerificationDocument | None,
) -> list[dict]:
    analysis_by_id = {
        finding.segment_id: finding.model_dump(mode="json")
        for finding in (analysis.suspicious_segments if analysis else [])
    }
    verification_by_id: dict[str, list[dict]] = {}
    if verification:
        for region in verification.verified_regions:
            item = {
                "verification_id": region.verification_id,
                "secondary_text": region.secondary_text,
                "agreement": region.agreement,
                "risk_types": region.risk_types,
                "severity": region.severity,
                "status": region.status,
                "error": region.error,
            }
            for segment_id in region.source_segment_ids:
                verification_by_id.setdefault(segment_id, []).append(item)

    items = [
        {
            "segment_id": segment.segment_id,
            "start_seconds": segment.start_seconds,
            "end_seconds": segment.end_seconds,
            "primary_text": segment.text,
            "analysis": analysis_by_id.get(segment.segment_id),
            "verifications": verification_by_id.get(segment.segment_id, []),
        }
        for segment in document.segments
    ]
    payloads: list[dict] = []
    current: list[dict] = []
    current_size = 0
    for item in items:
        item_size = len(json.dumps(item, ensure_ascii=False))
        if current and current_size + item_size > SEGMENT_CHAR_LIMIT:
            payloads.append(
                {
                    "episode_id": document.episode_id,
                    "language": document.language,
                    "segments": current,
                }
            )
            current = []
            current_size = 0
        current.append(item)
        current_size += item_size
    if current:
        payloads.append(
            {
                "episode_id": document.episode_id,
                "language": document.language,
                "segments": current,
            }
        )
    return payloads


def _render_user_message(user_template: str, payload: dict) -> str:
    return user_template.replace(
        "{{ transcript_payload }}", json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _sub_payload(payload: dict, segments: list[dict]) -> dict:
    return {**payload, "segments": segments}


def _passthrough_response(payload: dict, reason: str) -> _ModelCorrectionResponse:
    """The input, handed back unchanged, for segments the model would not do.

    Correction is a repair stage: every segment already carries usable text
    from the transcript. When the model cannot be made to answer properly for a
    segment, keeping that text is the conservative outcome — the sentence stays
    exactly as it was heard. Failing the whole stage instead throws away the
    other three hundred segments it did correct, and stops the bulletin over a
    malformed wrapper.

    Marked ``uncertain`` rather than ``unresolved`` on purpose. Both are honest
    about the model not having judged the text, but ``unresolved`` is read by
    transcript QA as a defect worth escalating, and there is no evidence of a
    defect here — only of an answer that never arrived.
    """
    return _ModelCorrectionResponse(
        segments=[
            _ModelCorrectionSegment(
                segment_id=item["segment_id"],
                corrected_text=item["primary_text"],
                status="uncertain",
                severity="none",
                reason=reason,
            )
            for item in payload["segments"]
        ]
    )


def _call_structured_correction(
    system_prompt: str,
    user_template: str,
    payload: dict,
    settings: Settings,
    episode_id: str,
    *,
    budget_check: Callable[[float], None],
    response_sink: list[ClaudeResponse],
    depth: int = 0,
) -> tuple[_ModelCorrectionResponse, list[ClaudeResponse]]:
    """Correct one batch of segments, splitting it if the model cannot cope.

    Two attempts are made at the batch as a whole. If both come back malformed,
    the batch is halved and each half asked for separately: a model that loses
    track of eighty segments usually manages forty, and the failure observed on
    2026-08-19 was exactly that shape — a single segment returned in place of
    eighty-one. Splitting is bounded, because each level doubles the number of
    paid calls; below the bound the segments are kept as they came in.
    """
    responses: list[ClaudeResponse] = []
    complaint = ""
    empty_attempts = 0

    for attempt in (1, 2):
        if attempt == 1:
            message = _render_user_message(user_template, payload)
        else:
            message = (
                "Your previous response was invalid: "
                f"{complaint}\n\n"
                "Return the correction for the SAME input as one strictly valid JSON object "
                "with a 'segments' array containing exactly one entry per input segment, in "
                "the same order. Do not add, remove, merge, or reorder segment IDs. Do not "
                "reconstruct missing facts. Output JSON only.\n\nInput:\n"
                + _render_user_message(user_template, payload)
            )

        budget_check(sum(item.cost_usd for item in responses))
        response = call_claude(
            system_prompt=system_prompt,
            user_message=message,
            settings=settings,
            json_mode=True,
        )
        responses.append(response)
        response_sink.append(response)
        budget_check(sum(item.cost_usd for item in responses))
        try:
            parsed = _parse_correction_response(response.text)
            _validate_response_segments(parsed, payload)
            return parsed, responses
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            complaint = str(exc)
            if isinstance(exc, _EmptyCorrectionResponse):
                empty_attempts += 1
            logger.warning(
                "Invalid structured correction for %s (%d segments, attempt %d): %s",
                episode_id,
                len(payload["segments"]),
                attempt,
                complaint,
            )

    if empty_attempts == 2:
        raise PipelineError(
            f"Correction model returned an invalid response after retry: {complaint}",
            category=ErrorCategory.TRANSIENT_SERVER,
        )

    segments = payload["segments"]
    if len(segments) > 1 and depth < _MAX_CORRECTION_SPLIT_DEPTH:
        middle = len(segments) // 2
        logger.warning(
            "Splitting correction batch for %s into %d + %d segments after two invalid responses",
            episode_id,
            middle,
            len(segments) - middle,
        )
        merged: list[_ModelCorrectionSegment] = []
        for half in (segments[:middle], segments[middle:]):
            part, part_responses = _call_structured_correction(
                system_prompt,
                user_template,
                _sub_payload(payload, half),
                settings,
                episode_id,
                budget_check=budget_check,
                response_sink=response_sink,
                depth=depth + 1,
            )
            merged.extend(part.segments)
            responses.extend(part_responses)
        return _ModelCorrectionResponse(segments=merged), responses

    logger.error(
        "Correction model would not answer properly for %d segment(s) of %s (%s); "
        "keeping the transcript text unchanged for them",
        len(segments),
        episode_id,
        complaint,
    )
    return (
        _passthrough_response(
            payload,
            "Modellantwort war wiederholt ungültig; Transkripttext unverändert übernommen.",
        ),
        responses,
    )


def _ensure_cost_budget(
    session: Session,
    episode: Episode,
    settings: Settings,
    in_flight_cost: float,
) -> None:
    current_cost = (
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
    )
    projected = float(current_cost or 0.0) + max(0.0, in_flight_cost)
    if projected >= settings.max_episode_cost_usd:
        raise PipelineError(
            (
                f"Episode cost limit reached before correction call: "
                f"${projected:.4f} >= ${settings.max_episode_cost_usd:.4f}"
            ),
            ErrorCategory.PERMANENT_COST_LIMIT,
        )


def _unwrap_segments(data: object) -> object:
    """Coerce the shapes a model reaches for into ``{"segments": [...]}``.

    The schema asks for an object with a ``segments`` array, and the model
    usually obliges. When it does not, it is almost always the wrapper that is
    wrong rather than the content: a bare array, a different key, or — as on
    2026-08-19, which cost an episode — a single segment object with no wrapper
    at all.

    Reshaping that is safe because nothing here trusts it. Whatever comes out
    still has to pass ``_validate_response_segments``, which demands exactly
    the input segment IDs and nothing else, so a reshape that guessed wrong is
    rejected a moment later. What this avoids is discarding a complete,
    correct answer over its punctuation.
    """
    if isinstance(data, list):
        return {"segments": data}
    if not isinstance(data, dict):
        return data
    if "segments" in data:
        return data
    # A lone segment, unwrapped.
    if "segment_id" in data and "corrected_text" in data:
        return {"segments": [data]}
    # The array under a name of the model's own choosing. Only accepted when
    # there is exactly one candidate, so nothing has to be guessed.
    candidates = [
        value
        for key, value in data.items()
        if isinstance(value, list)
        and value
        and all(isinstance(item, dict) and "segment_id" in item for item in value)
    ]
    if len(candidates) == 1:
        return {"segments": candidates[0]}
    return data


class _EmptyCorrectionResponse(ValueError):
    """The model returned nothing at all.

    Kept apart from a merely malformed answer: halving the batch is a cure for
    a model losing track of its own output, but an empty body means the call
    itself did not deliver, and retrying it in eight pieces only buys eight
    empty bodies. This one stays a loud, retryable failure.
    """


def _parse_correction_response(text: str) -> _ModelCorrectionResponse:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json") :].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned[len("```") :].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[: -len("```")].strip()
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            cleaned = cleaned[first : last + 1]
    if not cleaned:
        raise _EmptyCorrectionResponse("Correction model returned an empty response")
    data = json.loads(cleaned)
    return _ModelCorrectionResponse.model_validate(_unwrap_segments(data))


def _describe_segment_mismatch(expected: list[str], actual: list[str]) -> str:
    """Why a response was refused, in terms the model can act on.

    The retry used to be told only that its answer was "invalid", which leaves
    it guessing between a hundred possibilities. Naming the missing IDs turns
    the second attempt into a correction rather than a re-roll.
    """
    missing = [item for item in expected if item not in set(actual)]
    unknown = [item for item in actual if item not in set(expected)]
    parts = [f"expected {len(expected)} segments, received {len(actual)}"]
    if missing:
        parts.append(f"missing: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")
    if unknown:
        listed = ", ".join(unknown[:10])
        parts.append(f"not in the input: {listed}{' …' if len(unknown) > 10 else ''}")
    if not missing and not unknown:
        parts.append("same IDs but in a different order")
    return "; ".join(parts)


def _validate_response_segments(response: _ModelCorrectionResponse, payload: dict) -> None:
    """Refuse a response that does not cover exactly the segments asked about.

    Order is not part of the contract even though the prompt asks for it:
    ``_finalize_correction_segments`` matches by ID, so a reordered response
    produces byte-identical output. Rejecting it would buy a second paid call
    for nothing. A repeated ID is refused, because then the mapping is
    genuinely ambiguous.
    """
    expected = [item["segment_id"] for item in payload["segments"]]
    actual = [item.segment_id for item in response.segments]
    if len(set(actual)) != len(actual):
        raise ValueError("Correction response repeats a segment ID")
    if sorted(actual) != sorted(expected):
        raise ValueError(
            f"Correction response segment IDs must exactly match input "
            f"({_describe_segment_mismatch(expected, actual)})"
        )


def _dry_run_response(payload: dict) -> _ModelCorrectionResponse:
    return _ModelCorrectionResponse(
        segments=[
            _ModelCorrectionSegment(
                segment_id=item["segment_id"],
                corrected_text=item["primary_text"],
                status="verified",
                severity="none",
            )
            for item in payload["segments"]
        ]
    )


def _finalize_correction_segments(
    payload: dict,
    response: _ModelCorrectionResponse,
) -> list[CorrectedTranscriptSegment]:
    response_by_id = {item.segment_id: item for item in response.segments}
    finalized: list[CorrectedTranscriptSegment] = []
    for source in payload["segments"]:
        model = response_by_id[source["segment_id"]]
        original = source["primary_text"].strip()
        corrected = _revert_protected_token_changes(original, model.corrected_text.strip())
        status = model.status
        severity = model.severity
        flags = list(dict.fromkeys(model.flags))
        reason = model.reason
        verification_ids = [item["verification_id"] for item in source.get("verifications", [])]
        verifications = source.get("verifications", [])

        from btcedu.core.transcript_verifier import compare_transcripts

        if _contains_unexpected_turkish(original, corrected):
            logger.warning(
                "Rejected non-German correction for segment %s; original retained",
                source["segment_id"],
            )
            corrected = original
            status = "verified"
            severity = "none"
            flags = []
            reason = "Nicht-deutsche Modellantwort verworfen; Originaltext beibehalten."

        verification_supports_correction = _is_supported_by_verification(
            corrected,
            verifications,
        )
        original_words = original.split()
        corrected_words = corrected.split()
        if (
            original_words
            and len(corrected_words) < len(original_words) * 0.7
            and not verification_supports_correction
        ):
            corrected = original
            status = "unresolved"
            severity = "major"
            flags = list(dict.fromkeys([*flags, "possible_content_deletion"]))
            reason = "Starke unbelegte Kürzung wurde verworfen; Originaltext beibehalten."
        comparison = compare_transcripts(original, corrected)
        if verification_supports_correction:
            status = "corrected" if corrected != original else "verified"
            severity = "none"
            flags = []
            reason = (
                "Korrektur durch Sekundärtranskription bestätigt."
                if corrected != original
                else "Primärtext durch Sekundärtranskription bestätigt."
            )
        correction_risks = set(comparison.risk_types)
        dangerous_changes = correction_risks & {
            "number_disagreement",
            "date_disagreement",
            "time_disagreement",
            "casualty_disagreement",
            "score_disagreement",
            "negation_disagreement",
            "possible_name_disagreement",
            "semantic_role_disagreement",
        }
        if dangerous_changes and not verification_supports_correction:
            corrected = original
            status = "unresolved"
            severity = (
                "critical"
                if dangerous_changes
                & {
                    "casualty_disagreement",
                    "negation_disagreement",
                    "semantic_role_disagreement",
                }
                else "major"
            )
            flags = list(dict.fromkeys([*flags, *sorted(dangerous_changes)]))
            reason = "Riskante faktische Änderung wurde verworfen; Originaltext beibehalten."
        elif comparison.token_difference >= 0.5 and not verification_supports_correction:
            corrected = original
            status = "unresolved"
            severity = "major"
            flags = list(dict.fromkeys([*flags, "possible_free_reconstruction"]))
            reason = "Zu starke freie Rekonstruktion wurde verworfen; Originaltext beibehalten."

        verification_risks = {
            risk
            for item in verifications
            if item.get("status") == "success"
            for risk in compare_transcripts(
                original,
                str(item.get("secondary_text") or ""),
            ).risk_types
        }
        failed_verification = any(item.get("status") == "failed" for item in verifications)
        if failed_verification or (verification_risks and not verification_supports_correction):
            blocking = verification_risks & {
                "casualty_disagreement",
                "score_disagreement",
                "negation_disagreement",
                "possible_name_disagreement",
                "date_disagreement",
                "semantic_role_disagreement",
                "incomplete_sentence",
            }
            status = "unresolved" if failed_verification or blocking else "uncertain"
            severity = (
                "critical"
                if failed_verification
                or verification_risks
                & {
                    "casualty_disagreement",
                    "negation_disagreement",
                    "semantic_role_disagreement",
                }
                else "major"
            )
            flags = list(
                dict.fromkeys(
                    [
                        *flags,
                        *sorted(verification_risks),
                        "conflicting_transcriptions",
                    ]
                )
            )
            reason = (
                "Primär- und Sekundärtranskription widersprechen sich; "
                "keine künstliche Einigung vorgenommen."
            )

        analysis = source.get("analysis")
        analysis_reasons = set((analysis or {}).get("reasons", []))
        factual_flags = {
            "number_disagreement",
            "date_disagreement",
            "time_disagreement",
            "casualty_disagreement",
            "score_disagreement",
            "negation_disagreement",
            "possible_name_disagreement",
            "semantic_role_disagreement",
            "conflicting_transcriptions",
        }
        if corrected == original and not failed_verification:
            supported_flags = analysis_reasons | verification_risks
            flags = [flag for flag in flags if flag not in factual_flags or flag in supported_flags]
            if not supported_flags and not flags and status in {"unresolved", "uncertain"}:
                status = "verified"
                severity = "none"
                reason = None
        if analysis and not verifications:
            flags = list(dict.fromkeys([*flags, *sorted(analysis_reasons)]))
            if analysis.get("severity") == "critical" or "incomplete_sentence" in analysis_reasons:
                status = "unresolved"
                severity = "critical" if analysis.get("severity") == "critical" else "major"
            elif status not in {"unresolved", "uncertain"}:
                status = "uncertain"
                severity = "major" if analysis.get("severity") == "major" else "minor"
            reason = reason or "Verdächtiges ASR-Segment konnte nicht eindeutig verifiziert werden."

        if corrected == original and status == "corrected":
            status = "verified"
        elif corrected != original and status == "verified":
            status = "corrected"
        if status in {"verified", "corrected"}:
            reason = None
        elif not reason:
            reason = "Unsicherheit wurde vom Korrekturmodell markiert."
        if status == "unresolved" and severity in {"none", "minor"}:
            severity = "major"

        finalized.append(
            CorrectedTranscriptSegment(
                segment_id=source["segment_id"],
                start_seconds=source["start_seconds"],
                end_seconds=source["end_seconds"],
                original_text=original,
                corrected_text=corrected,
                status=status,
                severity=severity,
                flags=flags,
                reason=reason,
                verification_ids=verification_ids,
            )
        )
    return finalized


def _mark_transcript_qa_stale(settings: Settings, episode_id: str) -> None:
    qa_path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_qa.json"
    if not qa_path.exists():
        return
    qa_path.with_name(qa_path.name + ".stale").write_text(
        json.dumps(
            {
                "stale": True,
                "invalidated_by": "correct",
                "invalidated_at": _utcnow().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


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
            "item_id": f"corr-{len(changes):04d}",
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
