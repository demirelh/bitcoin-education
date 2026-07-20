"""Selective secondary transcription and deterministic comparison."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.transcriber import load_transcript_document
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.transcript_schema import (
    TranscriptAnalysisDocument,
    TranscriptDocument,
    TranscriptVerificationDocument,
    TranscriptVerificationRegion,
    TranscriptVerificationSummary,
)
from btcedu.services.errors import ErrorCategory, PipelineError

logger = logging.getLogger(__name__)

VERIFICATION_COMPARATOR_VERSION = "1.2"
_VALID_MODES = {"disabled", "full", "suspicious_segments_only"}
_SEVERITY_ORDER = {"minor": 0, "major": 1, "critical": 2}
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*|\d+(?:[.,:]\d+)*%?", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")
_PERCENT_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*%")
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?"
    r"|\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)(?:\s+\d{2,4})?)\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d|\s*Uhr)\b", re.IGNORECASE)
_SCORE_RE = re.compile(r"\b\d{1,2}\s*(?::|-|zu)\s*\d{1,2}\b", re.IGNORECASE)
_NAME_RE = re.compile(
    r"\b[A-ZÄÖÜ][a-zäöüß]+(?:[-'][A-ZÄÖÜ]?[a-zäöüß]+)?"
    r"(?:\s+[A-ZÄÖÜ][a-zäöüß]+(?:[-'][A-ZÄÖÜ]?[a-zäöüß]+)?)+\b"
)
_NEGATIONS = {
    "nicht",
    "nichts",
    "nie",
    "niemals",
    "kein",
    "keine",
    "keinen",
    "keinem",
    "keiner",
    "ohne",
    "weder",
}
_CASUALTY_TERMS = {
    "tot",
    "tote",
    "toten",
    "getötet",
    "starb",
    "starben",
    "opfer",
    "verletzt",
    "verletzte",
    "verletzten",
}
_SCORE_TERMS = {
    "spiel",
    "sieg",
    "gewann",
    "verlor",
    "tor",
    "tore",
    "ergebnis",
    "halbzeit",
    "satz",
}
_ROLE_VERBS = {
    "tötete",
    "töteten",
    "verletzte",
    "verletzten",
    "verhaftete",
    "verhafteten",
    "beschuldigte",
    "beschuldigten",
    "besiegte",
    "besiegten",
    "ernannte",
    "ernannten",
    "kritisierte",
    "kritisierten",
}
_OPPOSING_CLAIM_GROUPS = (
    ({"angenommen", "akzeptiert", "beschlossen"}, {"abgelehnt", "verworfen"}),
    ({"gestiegen", "gewachsen", "erhöht"}, {"gesunken", "gefallen", "verringert"}),
    ({"gewonnen", "besiegt"}, {"verloren", "unterlegen"}),
    ({"erlaubt", "genehmigt"}, {"verboten", "untersagt"}),
    ({"bestätigt", "zugestimmt"}, {"widersprochen", "dementiert"}),
)
_DANGLING_WORDS = {
    "aber",
    "als",
    "auch",
    "dass",
    "denn",
    "doch",
    "für",
    "mit",
    "oder",
    "sondern",
    "und",
    "weil",
    "wenn",
    "während",
    "zu",
}
_RISK_ORDER = [
    "number_disagreement",
    "date_disagreement",
    "time_disagreement",
    "casualty_disagreement",
    "score_disagreement",
    "negation_disagreement",
    "possible_name_disagreement",
    "incomplete_sentence",
    "semantic_role_disagreement",
]
_GERMAN_NUMBER_WORDS = {
    "null": "0",
    "eins": "1",
    "ein": "1",
    "eine": "1",
    "zwei": "2",
    "drei": "3",
    "vier": "4",
    "fünf": "5",
    "sechs": "6",
    "sieben": "7",
    "acht": "8",
    "neun": "9",
    "zehn": "10",
}
_CRITICAL_RISKS = {
    "casualty_disagreement",
    "negation_disagreement",
    "semantic_role_disagreement",
}
_MAJOR_RISKS = {
    "number_disagreement",
    "date_disagreement",
    "time_disagreement",
    "score_disagreement",
    "possible_name_disagreement",
    "incomplete_sentence",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class VerificationRegionPlan:
    """One merged audio region selected for verification."""

    source_segment_ids: tuple[str, ...]
    original_start_seconds: float
    original_end_seconds: float
    clip_start_seconds: float
    clip_end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.clip_end_seconds - self.clip_start_seconds


@dataclass(frozen=True)
class TranscriptComparison:
    """Deterministic comparison output for one transcript pair."""

    token_difference: float
    agreement: Literal["high", "medium", "low"]
    risk_types: tuple[str, ...]
    severity: Literal["none", "minor", "major", "critical"]


@dataclass
class TranscriptVerificationResult:
    """Summary returned by the transcript verification stage."""

    episode_id: str
    verification_path: str
    provenance_path: str
    regions_checked: int = 0
    critical_count: int = 0
    cost_usd: float = 0.0
    skipped: bool = False
    reason: str = ""
    dry_run: bool = False


def compare_transcripts(primary_text: str, secondary_text: str) -> TranscriptComparison:
    """Compare two transcripts while ignoring punctuation and case-only differences."""
    primary_tokens = _normalized_tokens(primary_text)
    secondary_tokens = _normalized_tokens(secondary_text)
    if _is_contiguous_subsequence(primary_tokens, secondary_tokens):
        return TranscriptComparison(
            token_difference=0.0,
            agreement="high",
            risk_types=(),
            severity="none",
        )
    context_passage = _best_context_passage(primary_text, secondary_text)
    if context_passage is not None:
        secondary_text = context_passage
        secondary_tokens = _normalized_tokens(context_passage)
    similarity = SequenceMatcher(None, primary_tokens, secondary_tokens).ratio()
    token_difference = round(1 - similarity, 4)
    if primary_tokens == secondary_tokens:
        return TranscriptComparison(
            token_difference=0.0,
            agreement="high",
            risk_types=(),
            severity="none",
        )
    risks: set[str] = set()

    primary_numbers = _normalized_matches(_NUMBER_RE, primary_text)
    secondary_numbers = _normalized_matches(_NUMBER_RE, secondary_text)
    if primary_numbers != secondary_numbers or _normalized_matches(
        _PERCENT_RE, primary_text
    ) != _normalized_matches(_PERCENT_RE, secondary_text):
        risks.add("number_disagreement")

    if _normalized_matches(_DATE_RE, primary_text) != _normalized_matches(_DATE_RE, secondary_text):
        if _DATE_RE.search(primary_text) or _DATE_RE.search(secondary_text):
            risks.add("date_disagreement")

    if _normalized_matches(_TIME_RE, primary_text) != _normalized_matches(_TIME_RE, secondary_text):
        if _TIME_RE.search(primary_text) or _TIME_RE.search(secondary_text):
            risks.add("time_disagreement")

    primary_words = set(primary_tokens)
    secondary_words = set(secondary_tokens)
    if (primary_words | secondary_words) & _CASUALTY_TERMS:
        if primary_numbers != secondary_numbers:
            risks.add("casualty_disagreement")

    primary_scores = _normalized_matches(_SCORE_RE, primary_text)
    secondary_scores = _normalized_matches(_SCORE_RE, secondary_text)
    if primary_scores != secondary_scores and (
        primary_scores or secondary_scores or (primary_words | secondary_words) & _SCORE_TERMS
    ):
        risks.add("score_disagreement")

    primary_negations = primary_words & _NEGATIONS
    secondary_negations = secondary_words & _NEGATIONS
    if bool(primary_negations) != bool(secondary_negations):
        risks.add("negation_disagreement")

    primary_names = _name_candidates(primary_text)
    secondary_names = _name_candidates(secondary_text)
    if primary_names != secondary_names and (primary_names or secondary_names):
        risks.add("possible_name_disagreement")

    if _is_incomplete(primary_tokens, secondary_tokens):
        risks.add("incomplete_sentence")

    if _semantic_roles_differ(primary_text, secondary_text):
        risks.add("semantic_role_disagreement")
    if _opposing_claims_differ(primary_words, secondary_words):
        risks.add("semantic_role_disagreement")

    ordered_risks = tuple(risk for risk in _RISK_ORDER if risk in risks)
    if risks & _CRITICAL_RISKS:
        severity: Literal["none", "minor", "major", "critical"] = "critical"
    elif risks & _MAJOR_RISKS:
        severity = "major"
    elif token_difference >= 0.4:
        severity = "minor"
    else:
        severity = "none"

    if severity == "critical" or similarity < 0.5:
        agreement: Literal["high", "medium", "low"] = "low"
    elif severity == "major" or similarity < 0.8:
        agreement = "medium"
    else:
        agreement = "high"

    return TranscriptComparison(
        token_difference=token_difference,
        agreement=agreement,
        risk_types=ordered_risks,
        severity=severity,
    )


def _is_contiguous_subsequence(primary_tokens: list[str], secondary_tokens: list[str]) -> bool:
    """Return true when a context transcript contains the complete target passage."""
    if not primary_tokens or len(primary_tokens) > len(secondary_tokens):
        return False
    width = len(primary_tokens)
    return any(
        secondary_tokens[index : index + width] == primary_tokens
        for index in range(len(secondary_tokens) - width + 1)
    )


def _best_context_passage(primary_text: str, secondary_text: str) -> str | None:
    """Return the raw local secondary passage best aligned to a short source."""
    primary_tokens = _normalized_tokens(primary_text)
    secondary_spans = _normalized_token_spans(secondary_text)
    secondary_tokens = [token for token, _, _ in secondary_spans]
    if not primary_tokens or len(secondary_tokens) < len(primary_tokens) * 2:
        return None
    width = len(primary_tokens)
    best_bounds: tuple[int, int] | None = None
    best_ratio = 0.0
    best_boundary_score = -1
    for candidate_width in range(width, min(len(secondary_tokens), width + 3) + 1):
        for index in range(len(secondary_tokens) - candidate_width + 1):
            window = secondary_tokens[index : index + candidate_width]
            ratio = SequenceMatcher(None, primary_tokens, window).ratio()
            boundary_score = int(window[0] == primary_tokens[0]) + int(
                window[-1] == primary_tokens[-1]
            )
            if (ratio, boundary_score) > (best_ratio, best_boundary_score):
                best_ratio = ratio
                best_boundary_score = boundary_score
                best_bounds = (index, index + candidate_width)
    if best_ratio < 0.82:
        return None
    assert best_bounds is not None
    start_index, end_index = best_bounds
    return secondary_text[secondary_spans[start_index][1] : secondary_spans[end_index - 1][2]]


def verify_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    dry_run: bool | None = None,
) -> TranscriptVerificationResult:
    """Verify selected transcript regions with the configured secondary provider."""
    from btcedu.services.ffmpeg_service import extract_audio_clip, probe_media
    from btcedu.services.transcription_service import (
        get_transcription_provider,
        resolve_transcription_config,
    )

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Stage 'transcript_verify' requires v2 pipeline but episode "
            f"{episode_id} has pipeline_version={episode.pipeline_version}."
        )
    if episode.status != EpisodeStatus.TRANSCRIBED and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'transcribed'. Use --force to override."
        )

    verification_path = (
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json"
    )
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "transcript_verify_provenance.json"
    )
    profile_config = _load_transcription_config(settings, episode)
    resolved = resolve_transcription_config(settings, profile_config)
    mode = resolved.secondary.mode.strip().lower()
    if not resolved.secondary.enabled:
        mode = "disabled"
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Unsupported secondary transcription mode '{mode}'. "
            f"Expected one of: {', '.join(sorted(_VALID_MODES))}."
        )
    if mode == "disabled":
        return TranscriptVerificationResult(
            episode_id=episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="secondary transcription disabled",
        )
    if not resolved.secondary.provider.strip() or not resolved.secondary.model.strip():
        raise ValueError("Secondary transcription provider and model must be configured.")
    if not episode.audio_path:
        raise ValueError(f"No audio file for episode {episode_id}")

    document = load_transcript_document(settings, episode_id)
    analysis = _load_analysis(settings, episode_id) if mode == "suspicious_segments_only" else None
    if analysis is not None:
        analysis = _filter_analysis_by_severity(
            analysis,
            resolved.secondary_minimum_severity,
        )
    effective_dry_run = settings.dry_run if dry_run is None else dry_run
    if analysis is not None and not analysis.suspicious_segments:
        return _write_empty_verification(
            session,
            episode,
            settings,
            document,
            analysis,
            resolved,
            verification_path,
            provenance_path,
            mode,
            force=force,
            dry_run=effective_dry_run,
        )

    duration_seconds = probe_media(episode.audio_path).duration_seconds
    if duration_seconds <= 0:
        raise ValueError(f"Could not determine audio duration for episode {episode_id}")
    plans = build_verification_regions(
        document,
        analysis,
        mode=mode,
        audio_duration_seconds=duration_seconds,
        context_seconds=resolved.suspicious_segment_context_seconds,
    )
    input_hash = _verification_input_hash(document, analysis)
    config_hash = _verification_config_hash(resolved)
    if not force and _is_verification_current(
        verification_path,
        provenance_path,
        input_hash,
        config_hash,
    ):
        existing = TranscriptVerificationDocument.model_validate_json(
            verification_path.read_text(encoding="utf-8")
        )
        return TranscriptVerificationResult(
            episode_id=episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            regions_checked=existing.summary.regions_checked,
            critical_count=existing.summary.critical_count,
            cost_usd=existing.summary.cost_usd,
            skipped=True,
            reason="already current",
        )

    estimated_total_cost = _estimated_cost(
        sum(plan.duration_seconds for plan in plans),
        settings.transcription_openai_cost_per_minute_usd,
    )
    if effective_dry_run:
        _validate_plan_limits(
            plans,
            max_audio_seconds=resolved.max_secondary_audio_seconds,
            max_clips=resolved.max_secondary_clips,
        )
        _ensure_cost_budget(session, episode, settings, estimated_total_cost)
        return TranscriptVerificationResult(
            episode_id=episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            regions_checked=len(plans),
            cost_usd=estimated_total_cost,
            skipped=True,
            reason=f"dry-run: would verify {len(plans)} region(s)",
            dry_run=True,
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSCRIPT_VERIFY,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()
    started = time.monotonic()
    total_cost = 0.0
    verified_regions: list[TranscriptVerificationRegion] = []

    try:
        _validate_plan_limits(
            plans,
            max_audio_seconds=resolved.max_secondary_audio_seconds,
            max_clips=resolved.max_secondary_clips,
        )
        _ensure_cost_budget(session, episode, settings, estimated_total_cost)

        api_key = _provider_api_key(settings, resolved.secondary.provider)
        provider = get_transcription_provider(
            resolved.secondary.provider,
            api_key=api_key,
            openai_cost_per_minute_usd=settings.transcription_openai_cost_per_minute_usd,
        )

        with tempfile.TemporaryDirectory(prefix="btcedu-transcript-verify-") as temp_dir:
            for index, plan in enumerate(plans):
                estimated_region_cost = _estimated_cost(
                    plan.duration_seconds,
                    settings.transcription_openai_cost_per_minute_usd,
                )
                _ensure_cost_budget(
                    session,
                    episode,
                    settings,
                    total_cost + estimated_region_cost,
                )
                clip_path = Path(temp_dir) / f"verify-{index + 1:04d}.mp3"
                extract_audio_clip(
                    episode.audio_path,
                    str(clip_path),
                    start_seconds=plan.clip_start_seconds,
                    end_seconds=plan.clip_end_seconds,
                )
                primary_text = _primary_text_for_clip(document, plan)
                try:
                    response = provider.transcribe(
                        str(clip_path),
                        model=resolved.secondary.model,
                        language=document.language,
                    )
                except Exception as exc:
                    verified_regions.append(_failed_region(index, plan, primary_text, str(exc)))
                    _write_verification_artifact(
                        verification_path,
                        episode_id,
                        resolved.secondary.provider,
                        resolved.secondary.model,
                        mode,
                        verified_regions,
                    )
                    raise

                comparison = _compare_region_segments(
                    document,
                    plan.source_segment_ids,
                    response.text,
                )
                total_cost += response.cost_usd
                verified_regions.append(
                    TranscriptVerificationRegion(
                        verification_id=f"verify-{index + 1:04d}",
                        source_segment_ids=list(plan.source_segment_ids),
                        original_start_seconds=round(plan.original_start_seconds, 3),
                        original_end_seconds=round(plan.original_end_seconds, 3),
                        clip_start_seconds=round(plan.clip_start_seconds, 3),
                        clip_end_seconds=round(plan.clip_end_seconds, 3),
                        primary_text=primary_text,
                        secondary_text=response.text.strip(),
                        agreement=comparison.agreement,
                        risk_types=list(comparison.risk_types),
                        severity=comparison.severity,
                        cost_usd=round(response.cost_usd, 6),
                    )
                )
                _ensure_cost_budget(session, episode, settings, total_cost)

        verification = _write_verification_artifact(
            verification_path,
            episode_id,
            resolved.secondary.provider,
            resolved.secondary.model,
            mode,
            verified_regions,
        )
        elapsed = time.monotonic() - started
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "transcript_verify",
                    "episode_id": episode_id,
                    "timestamp": _utcnow().isoformat(),
                    "provider": resolved.secondary.provider,
                    "model": resolved.secondary.model,
                    "mode": mode,
                    "minimum_analysis_severity": resolved.secondary_minimum_severity,
                    "comparator_version": VERIFICATION_COMPARATOR_VERSION,
                    "config_hash": config_hash,
                    "input_files": _verification_input_files(settings, episode_id, mode),
                    "input_content_hash": input_hash,
                    "output_files": [str(verification_path)],
                    "regions_checked": verification.summary.regions_checked,
                    "critical_count": verification.summary.critical_count,
                    "verified_audio_seconds": round(
                        sum(plan.duration_seconds for plan in plans), 3
                    ),
                    "cost_usd": verification.summary.cost_usd,
                    "duration_seconds": round(elapsed, 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        session.add(
            ContentArtifact(
                episode_id=episode_id,
                artifact_type="transcript_verification",
                file_path=str(verification_path),
                model=f"{resolved.secondary.provider}/{resolved.secondary.model}",
                prompt_hash=config_hash,
                retrieval_snapshot_path=None,
            )
        )
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = verification.summary.cost_usd
        episode.error_message = None
        session.commit()
        return TranscriptVerificationResult(
            episode_id=episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            regions_checked=verification.summary.regions_checked,
            critical_count=verification.summary.critical_count,
            cost_usd=verification.summary.cost_usd,
        )
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = round(total_cost, 6)
        pipeline_run.error_message = str(exc)[:1000]
        episode.error_message = str(exc)
        if isinstance(exc, PipelineError) and exc.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
        session.commit()
        raise


def build_verification_regions(
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument | None,
    *,
    mode: str,
    audio_duration_seconds: float,
    context_seconds: float,
) -> list[VerificationRegionPlan]:
    """Build clamped, merged verification regions without duplicate audio."""
    if mode == "full":
        if not document.segments:
            raise ValueError("Full verification requires at least one transcript segment.")
        return [
            VerificationRegionPlan(
                source_segment_ids=tuple(segment.segment_id for segment in document.segments),
                original_start_seconds=0.0,
                original_end_seconds=audio_duration_seconds,
                clip_start_seconds=0.0,
                clip_end_seconds=audio_duration_seconds,
            )
        ]
    if mode != "suspicious_segments_only":
        raise ValueError(f"Cannot build verification regions for mode: {mode}")
    if analysis is None:
        raise ValueError("Suspicious-segment verification requires transcript analysis.")

    segment_by_id = {segment.segment_id: segment for segment in document.segments}
    regions: list[VerificationRegionPlan] = []
    for finding in analysis.suspicious_segments:
        segment = segment_by_id.get(finding.segment_id)
        if segment is None:
            raise ValueError(
                f"Analysis references unknown transcript segment: {finding.segment_id}"
            )
        original_start = segment.start_seconds
        original_end = min(segment.end_seconds, audio_duration_seconds)
        clip_start = max(0.0, original_start - max(0.0, context_seconds))
        clip_end = min(
            audio_duration_seconds,
            original_end + max(0.0, context_seconds),
        )
        if clip_end <= clip_start:
            raise ValueError(f"Transcript segment {segment.segment_id} has no usable audio range.")
        regions.append(
            VerificationRegionPlan(
                source_segment_ids=(segment.segment_id,),
                original_start_seconds=original_start,
                original_end_seconds=original_end,
                clip_start_seconds=clip_start,
                clip_end_seconds=clip_end,
            )
        )

    regions.sort(key=lambda region: (region.clip_start_seconds, region.clip_end_seconds))
    merged: list[VerificationRegionPlan] = []
    for region in regions:
        if not merged or region.clip_start_seconds > merged[-1].clip_end_seconds:
            merged.append(region)
            continue
        previous = merged[-1]
        merged[-1] = VerificationRegionPlan(
            source_segment_ids=tuple(
                dict.fromkeys((*previous.source_segment_ids, *region.source_segment_ids))
            ),
            original_start_seconds=min(
                previous.original_start_seconds, region.original_start_seconds
            ),
            original_end_seconds=max(previous.original_end_seconds, region.original_end_seconds),
            clip_start_seconds=previous.clip_start_seconds,
            clip_end_seconds=max(previous.clip_end_seconds, region.clip_end_seconds),
        )
    return [
        VerificationRegionPlan(
            source_segment_ids=tuple(
                segment.segment_id
                for segment in document.segments
                if segment.start_seconds >= region.original_start_seconds
                and segment.end_seconds <= region.original_end_seconds
            ),
            original_start_seconds=region.original_start_seconds,
            original_end_seconds=region.original_end_seconds,
            clip_start_seconds=region.clip_start_seconds,
            clip_end_seconds=region.clip_end_seconds,
        )
        for region in merged
    ]


def _filter_analysis_by_severity(
    analysis: TranscriptAnalysisDocument,
    minimum_severity: str,
) -> TranscriptAnalysisDocument:
    minimum = minimum_severity.strip().lower()
    if minimum not in _SEVERITY_ORDER:
        raise ValueError(
            f"Unsupported secondary minimum severity '{minimum_severity}'. "
            f"Expected one of: {', '.join(_SEVERITY_ORDER)}."
        )
    selected = [
        finding
        for finding in analysis.suspicious_segments
        if _SEVERITY_ORDER[finding.severity] >= _SEVERITY_ORDER[minimum]
    ]
    return TranscriptAnalysisDocument(
        episode_id=analysis.episode_id,
        suspicious_segments=selected,
        summary={
            "segment_count": analysis.summary.segment_count,
            "suspicious_count": len(selected),
            "critical_count": sum(finding.severity == "critical" for finding in selected),
        },
    )


def _write_empty_verification(
    session: Session,
    episode: Episode,
    settings: Settings,
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument,
    resolved,
    verification_path: Path,
    provenance_path: Path,
    mode: str,
    *,
    force: bool,
    dry_run: bool,
) -> TranscriptVerificationResult:
    input_hash = _verification_input_hash(document, analysis)
    config_hash = _verification_config_hash(resolved)
    if not force and _is_verification_current(
        verification_path,
        provenance_path,
        input_hash,
        config_hash,
    ):
        return TranscriptVerificationResult(
            episode_id=episode.episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="already current",
        )
    if dry_run:
        return TranscriptVerificationResult(
            episode_id=episode.episode_id,
            verification_path=str(verification_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="dry-run: no suspicious regions",
            dry_run=True,
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSCRIPT_VERIFY,
        status=RunStatus.SUCCESS,
        completed_at=_utcnow(),
        estimated_cost_usd=0.0,
    )
    verification = _write_verification_artifact(
        verification_path,
        episode.episode_id,
        resolved.secondary.provider,
        resolved.secondary.model,
        mode,
        [],
    )
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(
            {
                "stage": "transcript_verify",
                "episode_id": episode.episode_id,
                "timestamp": _utcnow().isoformat(),
                "provider": resolved.secondary.provider,
                "model": resolved.secondary.model,
                "mode": mode,
                "comparator_version": VERIFICATION_COMPARATOR_VERSION,
                "config_hash": config_hash,
                "input_files": _verification_input_files(
                    settings,
                    episode.episode_id,
                    mode,
                ),
                "input_content_hash": input_hash,
                "output_files": [str(verification_path)],
                "regions_checked": 0,
                "critical_count": 0,
                "verified_audio_seconds": 0,
                "cost_usd": 0.0,
                "duration_seconds": 0.0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    session.add(pipeline_run)
    session.add(
        ContentArtifact(
            episode_id=episode.episode_id,
            artifact_type="transcript_verification",
            file_path=str(verification_path),
            model=f"{resolved.secondary.provider}/{resolved.secondary.model}",
            prompt_hash=config_hash,
            retrieval_snapshot_path=None,
        )
    )
    episode.error_message = None
    session.commit()
    return TranscriptVerificationResult(
        episode_id=episode.episode_id,
        verification_path=str(verification_path),
        provenance_path=str(provenance_path),
        regions_checked=verification.summary.regions_checked,
    )


def _write_verification_artifact(
    path: Path,
    episode_id: str,
    provider: str,
    model: str,
    mode: str,
    regions: list[TranscriptVerificationRegion],
) -> TranscriptVerificationDocument:
    document = TranscriptVerificationDocument(
        episode_id=episode_id,
        provider=provider,
        model=model,
        mode=mode,
        verified_regions=regions,
        summary=TranscriptVerificationSummary(
            regions_checked=sum(region.status == "success" for region in regions),
            critical_count=sum(
                region.status == "success" and region.severity == "critical" for region in regions
            ),
            cost_usd=round(sum(region.cost_usd for region in regions), 6),
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return document


def _failed_region(
    index: int,
    plan: VerificationRegionPlan,
    primary_text: str,
    error: str,
) -> TranscriptVerificationRegion:
    return TranscriptVerificationRegion(
        verification_id=f"verify-{index + 1:04d}",
        source_segment_ids=list(plan.source_segment_ids),
        original_start_seconds=round(plan.original_start_seconds, 3),
        original_end_seconds=round(plan.original_end_seconds, 3),
        clip_start_seconds=round(plan.clip_start_seconds, 3),
        clip_end_seconds=round(plan.clip_end_seconds, 3),
        primary_text=primary_text,
        secondary_text="",
        agreement="low",
        risk_types=[],
        severity="major",
        cost_usd=0.0,
        status="failed",
        error=error[:1000],
    )


def _validate_plan_limits(
    plans: list[VerificationRegionPlan],
    *,
    max_audio_seconds: float,
    max_clips: int,
) -> None:
    if max_clips < 1:
        raise ValueError("max_secondary_clips must be at least 1")
    if len(plans) > max_clips:
        _raise_cost_limit(f"Secondary clip limit exceeded: {len(plans)} > {max_clips}.")
    total_seconds = sum(plan.duration_seconds for plan in plans)
    if total_seconds > max_audio_seconds:
        _raise_cost_limit(
            f"Secondary audio limit exceeded: {total_seconds:.1f}s > {max_audio_seconds:.1f}s."
        )


def _ensure_cost_budget(
    session: Session,
    episode: Episode,
    settings: Settings,
    additional_cost: float,
) -> None:
    current_cost = (
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
    )
    projected = float(current_cost or 0.0) + additional_cost
    if projected > settings.max_episode_cost_usd:
        _raise_cost_limit(
            f"Episode cost limit exceeded: ${projected:.4f} > ${settings.max_episode_cost_usd:.4f}."
        )


def _raise_cost_limit(message: str) -> None:
    raise PipelineError(message, ErrorCategory.PERMANENT_COST_LIMIT)


def _estimated_cost(audio_seconds: float, cost_per_minute: float) -> float:
    return max(0.0, audio_seconds) / 60 * max(0.0, cost_per_minute)


def _load_transcription_config(settings: Settings, episode: Episode) -> dict:
    from btcedu.profiles import get_registry

    profile = get_registry(settings).get(episode.content_profile)
    return profile.stage_config.get("transcription", {}) or {}


def _provider_api_key(settings: Settings, provider: str) -> str:
    if provider.strip().lower() == "openai":
        if not settings.effective_whisper_api_key:
            raise ValueError(
                "No OpenAI key for secondary transcription. Set WHISPER_API_KEY or OPENAI_API_KEY."
            )
        return settings.effective_whisper_api_key
    raise ValueError(f"Unsupported secondary transcription provider: {provider}")


def _load_analysis(settings: Settings, episode_id: str) -> TranscriptAnalysisDocument:
    path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json"
    if not path.exists():
        raise FileNotFoundError(f"Transcript analysis not found for episode {episode_id}: {path}")
    return _load_analysis_from_path(path)


def _load_analysis_from_path(path: Path) -> TranscriptAnalysisDocument:
    try:
        return TranscriptAnalysisDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid transcript analysis: {path}") from exc


def _verification_input_hash(
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument | None,
) -> str:
    payload = {
        "transcript": document.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json") if analysis else None,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verification_config_hash(resolved) -> str:
    payload = {
        "provider": resolved.secondary.provider,
        "model": resolved.secondary.model,
        "mode": resolved.secondary.mode,
        "enabled": resolved.secondary.enabled,
        "minimum_severity": resolved.secondary_minimum_severity,
        "context_seconds": resolved.suspicious_segment_context_seconds,
        "max_audio_seconds": resolved.max_secondary_audio_seconds,
        "max_clips": resolved.max_secondary_clips,
        "comparator_version": VERIFICATION_COMPARATOR_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_verification_current(
    verification_path: Path,
    provenance_path: Path,
    input_hash: str,
    config_hash: str,
) -> bool:
    if not verification_path.exists() or not provenance_path.exists():
        return False
    stale_path = verification_path.with_name(verification_path.name + ".stale")
    if stale_path.exists():
        stale_path.unlink()
        return False
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        provenance.get("input_content_hash") == input_hash
        and provenance.get("config_hash") == config_hash
    )


def _verification_input_files(
    settings: Settings,
    episode_id: str,
    mode: str,
) -> list[str]:
    files = [str(Path(settings.transcripts_dir) / episode_id / "transcript.structured.de.json")]
    if mode == "suspicious_segments_only":
        files.append(
            str(Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json")
        )
    return files


def _primary_text_for_clip(
    document: TranscriptDocument,
    plan: VerificationRegionPlan,
) -> str:
    """Build primary text from the same context interval sent to secondary ASR."""
    return " ".join(
        segment.text
        for segment in document.segments
        if segment.start_seconds >= plan.clip_start_seconds
        and segment.end_seconds <= plan.clip_end_seconds
    ).strip()


def _compare_region_segments(
    document: TranscriptDocument,
    source_segment_ids: tuple[str, ...],
    secondary_text: str,
) -> TranscriptComparison:
    wanted = set(source_segment_ids)
    comparisons = [
        compare_transcripts(segment.text, secondary_text)
        for segment in document.segments
        if segment.segment_id in wanted
    ]
    if not comparisons:
        return compare_transcripts("", secondary_text)
    severity_order = {"none": 0, "minor": 1, "major": 2, "critical": 3}
    agreement_order = {"high": 0, "medium": 1, "low": 2}
    risks = {risk for comparison in comparisons for risk in comparison.risk_types}
    return TranscriptComparison(
        token_difference=max(comparison.token_difference for comparison in comparisons),
        agreement=max(comparisons, key=lambda item: agreement_order[item.agreement]).agreement,
        risk_types=tuple(risk for risk in _RISK_ORDER if risk in risks),
        severity=max(comparisons, key=lambda item: severity_order[item.severity]).severity,
    )


def _normalized_tokens(text: str) -> list[str]:
    text = _SCORE_RE.sub(
        lambda match: re.sub(r"\s*(?::|-|zu)\s*", " score ", match.group(0), flags=re.I),
        text,
    )
    return [
        _GERMAN_NUMBER_WORDS.get(normalized, normalized)
        for token in _TOKEN_RE.findall(text)
        if (normalized := token.casefold().replace(",", "."))
    ]


def _normalized_matches(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).casefold().replace(" ", "").replace(",", ".")
        for match in pattern.finditer(text)
    )


def _normalized_token_spans(text: str) -> list[tuple[str, int, int]]:
    """Tokenize like ``_normalized_tokens`` while retaining raw text offsets."""
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for score_match in _SCORE_RE.finditer(text):
        for token_match in _TOKEN_RE.finditer(text, cursor, score_match.start()):
            normalized = token_match.group(0).casefold().replace(",", ".")
            spans.append(
                (
                    _GERMAN_NUMBER_WORDS.get(normalized, normalized),
                    token_match.start(),
                    token_match.end(),
                )
            )
        for token in _normalized_tokens(score_match.group(0)):
            spans.append((token, score_match.start(), score_match.end()))
        cursor = score_match.end()
    for token_match in _TOKEN_RE.finditer(text, cursor):
        normalized = token_match.group(0).casefold().replace(",", ".")
        spans.append(
            (
                _GERMAN_NUMBER_WORDS.get(normalized, normalized),
                token_match.start(),
                token_match.end(),
            )
        )
    return spans


def _name_candidates(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _NAME_RE.finditer(text)}


def _is_incomplete(
    primary_tokens: list[str],
    secondary_tokens: list[str],
) -> bool:
    if not secondary_tokens:
        return True
    last_secondary = secondary_tokens[-1]
    if last_secondary in _DANGLING_WORDS:
        return True
    if len(primary_tokens) >= 8 and len(secondary_tokens) < len(primary_tokens) * 0.55:
        return True
    return False


def _semantic_roles_differ(primary_text: str, secondary_text: str) -> bool:
    primary_names = [match.group(0).casefold() for match in _NAME_RE.finditer(primary_text)]
    secondary_names = [match.group(0).casefold() for match in _NAME_RE.finditer(secondary_text)]
    if len(primary_names) != 2 or set(primary_names) != set(secondary_names):
        return False
    if primary_names != list(reversed(secondary_names)):
        return False
    primary_words = set(_normalized_tokens(primary_text))
    secondary_words = set(_normalized_tokens(secondary_text))
    return bool(primary_words & _ROLE_VERBS) and bool(secondary_words & _ROLE_VERBS)


def _opposing_claims_differ(primary_words: set[str], secondary_words: set[str]) -> bool:
    for positive, negative in _OPPOSING_CLAIM_GROUPS:
        if (primary_words & positive and secondary_words & negative) or (
            primary_words & negative and secondary_words & positive
        ):
            return True
    return False
