"""Deterministic, conservative analysis of structured transcript segments."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

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
    SuspiciousTranscriptSegment,
    TranscriptAnalysisDocument,
    TranscriptAnalysisSummary,
    TranscriptDocument,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

ANALYSIS_RULESET_VERSION = "1.3"

_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+(?:[-'][A-Za-zÄÖÜäöüß]+)?")
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")
_TERMINAL_RE = re.compile(r"""[.!?…]["')\]]*$""")
_DANGLING_WORDS = {
    "aber",
    "als",
    "auf",
    "bei",
    "dass",
    "der",
    "die",
    "ein",
    "eine",
    "für",
    "gegen",
    "im",
    "in",
    "mit",
    "oder",
    "und",
    "von",
    "weil",
    "wenn",
    "zu",
}
_GERMAN_STOPWORDS = {
    "aber",
    "als",
    "auch",
    "auf",
    "bei",
    "das",
    "der",
    "die",
    "ein",
    "eine",
    "für",
    "hat",
    "ist",
    "mit",
    "nicht",
    "sich",
    "und",
    "von",
    "werden",
    "zu",
}
_FOREIGN_STOPWORDS = {
    "and",
    "are",
    "but",
    "for",
    "from",
    "have",
    "is",
    "of",
    "that",
    "the",
    "this",
    "to",
    "ve",
    "bir",
    "bu",
    "için",
    "ile",
    "olan",
}
_CONTENT_STOPWORDS = _GERMAN_STOPWORDS | {
    "dem",
    "den",
    "des",
    "es",
    "im",
    "in",
    "oder",
    "war",
    "wie",
}
_REASON_ORDER = [
    "incomplete_sentence",
    "possible_missing_words",
    "unusually_short_segment",
    "unusually_long_segment",
    "strong_repetition",
    "many_non_alphabetic_characters",
    "asr_fragment",
    "missing_sentence_end",
    "possible_language_mixing",
    "numbers_without_context",
    "inconsistent_proper_name_spelling",
    "abrupt_context_break",
    "low_confidence",
]
_SEVERITY_ORDER = {"minor": 0, "major": 1, "critical": 2}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TranscriptAnalysisResult:
    """Summary of one deterministic transcript analysis run."""

    episode_id: str
    analysis_path: str
    provenance_path: str
    segment_count: int = 0
    suspicious_count: int = 0
    critical_count: int = 0
    cost_usd: float = 0.0
    skipped: bool = False
    reason: str = ""


def analyze_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> TranscriptAnalysisResult:
    """Flag suspicious transcript candidates without asserting corrections."""
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Stage 'transcript_analyze' requires v2 pipeline but episode "
            f"{episode_id} has pipeline_version={episode.pipeline_version}."
        )
    if episode.status != EpisodeStatus.TRANSCRIBED and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'transcribed'. Use --force to override."
        )

    analyze_config = _load_analysis_config(settings, episode)
    analysis_path = (
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json"
    )
    provenance_path = (
        Path(settings.outputs_dir)
        / episode_id
        / "provenance"
        / "transcript_analyze_provenance.json"
    )
    if not analyze_config.get("enabled", True):
        return TranscriptAnalysisResult(
            episode_id=episode_id,
            analysis_path=str(analysis_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="disabled by profile",
        )

    document = load_transcript_document(settings, episode_id)
    input_hash = _document_hash(document)
    ruleset_hash = _ruleset_hash(analyze_config)
    if not force and _is_analysis_current(
        analysis_path,
        provenance_path,
        input_hash,
        ruleset_hash,
    ):
        existing = TranscriptAnalysisDocument.model_validate_json(
            analysis_path.read_text(encoding="utf-8")
        )
        return TranscriptAnalysisResult(
            episode_id=episode_id,
            analysis_path=str(analysis_path),
            provenance_path=str(provenance_path),
            segment_count=existing.summary.segment_count,
            suspicious_count=existing.summary.suspicious_count,
            critical_count=existing.summary.critical_count,
            skipped=True,
            reason="already current",
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSCRIPT_ANALYZE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()
    started = time.monotonic()

    try:
        analysis = _analyze_document(document, analyze_config)
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text(
            json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _mark_verification_stale(settings, episode_id)

        elapsed = time.monotonic() - started
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "transcript_analyze",
                    "episode_id": episode_id,
                    "timestamp": _utcnow().isoformat(),
                    "model": f"deterministic/{ANALYSIS_RULESET_VERSION}",
                    "ruleset_version": ANALYSIS_RULESET_VERSION,
                    "ruleset_hash": ruleset_hash,
                    "input_files": [_transcript_input_path(settings, episode_id)],
                    "input_content_hash": input_hash,
                    "output_files": [str(analysis_path)],
                    "segment_count": analysis.summary.segment_count,
                    "suspicious_count": analysis.summary.suspicious_count,
                    "critical_count": analysis.summary.critical_count,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
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
                artifact_type="transcript_analysis",
                file_path=str(analysis_path),
                model=f"deterministic/{ANALYSIS_RULESET_VERSION}",
                prompt_hash=ruleset_hash,
                retrieval_snapshot_path=None,
            )
        )
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = 0.0
        episode.error_message = None
        session.commit()

        return TranscriptAnalysisResult(
            episode_id=episode_id,
            analysis_path=str(analysis_path),
            provenance_path=str(provenance_path),
            segment_count=analysis.summary.segment_count,
            suspicious_count=analysis.summary.suspicious_count,
            critical_count=analysis.summary.critical_count,
        )
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        episode.error_message = str(exc)
        session.commit()
        raise


def _analyze_document(
    document: TranscriptDocument,
    config: dict,
) -> TranscriptAnalysisDocument:
    flagged: list[SuspiciousTranscriptSegment] = []
    name_variants = _proper_name_variant_segments(document.segments)

    for index, segment in enumerate(document.segments):
        reasons: dict[str, str] = {}
        words = _words(segment.text)
        duration = segment.end_seconds - segment.start_seconds
        normalized = [word.lower() for word in words]
        last_word = normalized[-1] if normalized else ""

        next_segment = document.segments[index + 1] if index + 1 < len(document.segments) else None
        gap_seconds = (
            next_segment.start_seconds - segment.end_seconds if next_segment is not None else None
        )
        min_gap = float(config.get("incomplete_sentence_min_gap_seconds", 0.75))
        # A segment ending in a dangling connector (e.g. "und", "der") is only
        # genuine evidence of missing/truncated words when nothing follows it,
        # or when a real audio gap separates it from the next segment. ASR
        # segmenters routinely split continuous speech mid-clause with zero
        # gap; that is a chunking artifact, not lost content, since the
        # sentence is completed verbatim by the immediately following
        # segment. Flagging every such boundary as "major" was the dominant
        # source of suspicious-segment false positives.
        if last_word in _DANGLING_WORDS and (
            next_segment is None or (gap_seconds is not None and gap_seconds >= min_gap)
        ):
            reasons["incomplete_sentence"] = "major"
            reasons["possible_missing_words"] = "major"

        if len(words) <= 2 and duration >= float(config.get("short_duration_seconds", 4)):
            reasons["unusually_short_segment"] = "minor"

        if len(words) > int(config.get("long_segment_words", 80)) or duration > float(
            config.get("long_duration_seconds", 45)
        ):
            reasons["unusually_long_segment"] = "major"

        if _has_strong_repetition(normalized):
            reasons["strong_repetition"] = "critical"

        visible_chars = [char for char in segment.text if not char.isspace()]
        non_alpha = sum(not char.isalpha() for char in visible_chars)
        if len(visible_chars) >= 12 and non_alpha / len(visible_chars) > float(
            config.get("non_alpha_ratio", 0.35)
        ):
            reasons["many_non_alphabetic_characters"] = "major"

        if _looks_like_asr_fragment(segment.text, words):
            reasons["asr_fragment"] = "major"

        if (
            len(words) >= int(config.get("missing_sentence_end_min_words", 12))
            and not _TERMINAL_RE.search(segment.text.strip())
            and next_segment is not None
            and next_segment.text[:1].isupper()
        ):
            reasons["missing_sentence_end"] = "minor"

        if _has_language_mixing(normalized):
            reasons["possible_language_mixing"] = "minor"

        if len(_NUMBER_RE.findall(segment.text)) >= 4 and len(words) <= 6:
            reasons["numbers_without_context"] = "major"

        if segment.segment_id in name_variants:
            reasons["inconsistent_proper_name_spelling"] = "minor"

        if _is_abrupt_context_break(document.segments, index):
            reasons["abrupt_context_break"] = "minor"

        confidence_threshold = float(config.get("confidence_threshold", 0.45))
        if segment.confidence is not None and segment.confidence < confidence_threshold:
            reasons["low_confidence"] = (
                "critical" if segment.confidence < confidence_threshold / 3 else "major"
            )

        if reasons:
            severity = max(reasons.values(), key=_SEVERITY_ORDER.__getitem__)
            ordered_reasons = [reason for reason in _REASON_ORDER if reason in reasons]
            flagged.append(
                SuspiciousTranscriptSegment(
                    segment_id=segment.segment_id,
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    severity=severity,
                    reasons=ordered_reasons,
                )
            )

    critical_count = sum(item.severity == "critical" for item in flagged)
    return TranscriptAnalysisDocument(
        episode_id=document.episode_id,
        suspicious_segments=flagged,
        summary=TranscriptAnalysisSummary(
            segment_count=len(document.segments),
            suspicious_count=len(flagged),
            critical_count=critical_count,
        ),
    )


def _load_analysis_config(settings: Settings, episode: Episode) -> dict:
    from btcedu.profiles import get_registry

    profile = get_registry(settings).get(episode.content_profile)
    return profile.stage_config.get("transcript_analyze", {}) or {}


def _document_hash(document: TranscriptDocument) -> str:
    payload = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ruleset_hash(config: dict) -> str:
    payload = json.dumps(
        {
            "version": ANALYSIS_RULESET_VERSION,
            "config": config,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_analysis_current(
    analysis_path: Path,
    provenance_path: Path,
    input_hash: str,
    ruleset_hash: str,
) -> bool:
    if not analysis_path.exists() or not provenance_path.exists():
        return False

    stale_path = analysis_path.with_name(analysis_path.name + ".stale")
    if stale_path.exists():
        stale_path.unlink()
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        provenance.get("input_content_hash") == input_hash
        and provenance.get("ruleset_hash") == ruleset_hash
    )


def _mark_verification_stale(settings: Settings, episode_id: str) -> None:
    verification_path = (
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json"
    )
    if not verification_path.exists():
        return
    stale_path = verification_path.with_name(verification_path.name + ".stale")
    stale_path.write_text(
        json.dumps(
            {
                "stale": True,
                "reason": "transcript analysis changed",
                "invalidated_by": "transcript_analyze",
                "at": _utcnow().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _has_strong_repetition(words: list[str]) -> bool:
    if any(words[index : index + 4] == [words[index]] * 4 for index in range(len(words) - 3)):
        return True
    for phrase_size in range(2, 6):
        repeated_size = phrase_size * 3
        for index in range(len(words) - repeated_size + 1):
            phrase = words[index : index + phrase_size]
            if (
                words[index + phrase_size : index + 2 * phrase_size] == phrase
                and words[index + 2 * phrase_size : index + repeated_size] == phrase
            ):
                return True
    return False


def _looks_like_asr_fragment(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    if any(lowered.count(marker) >= 3 for marker in ("[musik]", "[applaus]", "(unverständlich)")):
        return True
    if len(words) < 6:
        return False
    vowel_less = sum(not re.search(r"[aeiouyäöü]", word, re.IGNORECASE) for word in words)
    return vowel_less >= 3 and vowel_less / len(words) > 0.3


def _has_language_mixing(words: list[str]) -> bool:
    if len(words) < 12:
        return False
    german = sum(word in _GERMAN_STOPWORDS for word in words)
    foreign = sum(word in _FOREIGN_STOPWORDS for word in words)
    return foreign >= 4 and foreign > german


def _proper_name_variant_segments(segments: list[TranscriptSegment]) -> set[str]:
    occurrences: dict[str, set[str]] = {}
    for segment in segments:
        words = _words(segment.text)
        for index, word in enumerate(words[1:], start=1):
            if len(word) >= 6 and word[:1].isupper() and _is_likely_proper_name(words, index):
                occurrences.setdefault(word, set()).add(segment.segment_id)

    flagged: set[str] = set()
    names = sorted(occurrences)
    counts = Counter(
        word
        for segment in segments
        for index, word in enumerate(_words(segment.text)[1:], start=1)
        if len(word) >= 6
        and word[:1].isupper()
        and _is_likely_proper_name(_words(segment.text), index)
    )
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if left[:3].lower() != right[:3].lower():
                continue
            if max(counts[left], counts[right]) < 2:
                continue
            ratio = SequenceMatcher(None, left.lower(), right.lower()).ratio()
            if 0.82 <= ratio < 1 and _german_variant_stem(left) != _german_variant_stem(right):
                flagged.update(occurrences[left])
                flagged.update(occurrences[right])
    return flagged


def _is_likely_proper_name(words: list[str], index: int) -> bool:
    """Reject ordinary German nouns introduced by determiners or adjectives."""
    previous = words[index - 1]
    if previous.casefold() in {
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "einem",
        "einen",
        "diese",
        "dieser",
        "diesem",
        "diesen",
    }:
        return False
    return previous[:1].isupper() or not previous.casefold().endswith(("e", "en", "er", "es"))


def _german_variant_stem(word: str) -> str:
    stem = word.casefold()
    suffixes = ("innen", "ern", "en", "er", "in", "amt", "e", "n", "r", "s")
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if stem.endswith(suffix) and len(stem) - len(suffix) >= 5:
                stem = stem[: -len(suffix)]
                changed = True
                break
    return stem


def _is_abrupt_context_break(segments: list[TranscriptSegment], index: int) -> bool:
    if index == 0 or index >= len(segments) - 1:
        return False
    current_words = _words(segments[index].text)
    if not 3 <= len(current_words) <= 8:
        return False
    if _TERMINAL_RE.search(segments[index].text.strip()):
        return False

    current = _content_words(segments[index].text)
    previous = _content_words(segments[index - 1].text)
    following = _content_words(segments[index + 1].text)
    if len(previous) < 5 or len(following) < 5 or not current:
        return False
    return not (current & previous) and not (current & following)


def _content_words(text: str) -> set[str]:
    return {
        word.lower()
        for word in _words(text)
        if len(word) >= 4 and word.lower() not in _CONTENT_STOPWORDS
    }


def _transcript_input_path(settings: Settings, episode_id: str) -> str:
    structured = Path(settings.transcripts_dir) / episode_id / "transcript.structured.de.json"
    if structured.exists():
        return str(structured)
    return str(Path(settings.transcripts_dir) / episode_id / "transcript.clean.de.txt")
