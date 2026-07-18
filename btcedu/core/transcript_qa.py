"""Deterministic transcript QA evaluation and blocking gate data."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
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
    TranscriptQADocument,
    TranscriptQAFinding,
    TranscriptQASummary,
    TranscriptVerificationDocument,
)

_FLAG_TO_CATEGORY = {
    "possible_name_disagreement": "unresolved_name",
    "inconsistent_proper_name_spelling": "unresolved_name",
    "number_disagreement": "unresolved_number",
    "numbers_without_context": "unresolved_number",
    "date_disagreement": "unresolved_date",
    "negation_disagreement": "unresolved_negation",
    "incomplete_sentence": "incomplete_sentence",
    "missing_sentence_end": "incomplete_sentence",
    "possible_missing_words": "incomplete_sentence",
    "asr_fragment": "incomplete_sentence",
    "conflicting_transcriptions": "conflicting_transcriptions",
    "casualty_disagreement": "casualty_uncertainty",
    "score_disagreement": "result_uncertainty",
    "semantic_role_disagreement": "semantic_role_uncertainty",
}
_INHERENTLY_CRITICAL = {
    "unresolved_negation",
    "casualty_uncertainty",
    "result_uncertainty",
    "semantic_role_uncertainty",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TranscriptQAResult:
    episode_id: str
    qa_path: str
    provenance_path: str
    status: str = "green"
    blocked: bool = False
    finding_count: int = 0
    blocking_count: int = 0
    skipped: bool = False
    reason: str = ""


def evaluate_transcript_qa(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> TranscriptQAResult:
    """Evaluate corrected transcript uncertainty against configured thresholds."""
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Stage 'transcript_qa' requires v2 pipeline but episode "
            f"{episode_id} has pipeline_version={episode.pipeline_version}."
        )
    if episode.status != EpisodeStatus.CORRECTED and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'corrected'. Use --force to override."
        )

    config = _load_config(settings, episode)
    qa_path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_qa.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "transcript_qa_provenance.json"
    )
    if not config["enabled"]:
        return TranscriptQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="transcript QA disabled by profile",
        )

    corrected_path = (
        Path(settings.transcripts_dir) / episode_id / "transcript.corrected.structured.de.json"
    )
    if not corrected_path.exists():
        raise FileNotFoundError(f"Structured corrected transcript not found: {corrected_path}")
    corrected = CorrectedTranscriptDocument.model_validate_json(
        corrected_path.read_text(encoding="utf-8")
    )
    verification = _load_verification(settings, episode_id)
    input_hash = _input_hash(corrected, verification)
    config_hash = _config_hash(config)
    if not force and _is_current(
        qa_path,
        provenance_path,
        input_hash,
        config_hash,
    ):
        existing = TranscriptQADocument.model_validate_json(qa_path.read_text(encoding="utf-8"))
        return TranscriptQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            status=existing.status,
            blocked=existing.blocked,
            finding_count=len(existing.findings),
            blocking_count=existing.summary.blocking_count,
            skipped=True,
            reason="already current",
        )

    run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSCRIPT_QA,
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    started = time.monotonic()
    try:
        document = _evaluate(corrected, verification, config)
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        qa_path.write_text(
            json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "transcript_qa",
                    "episode_id": episode_id,
                    "timestamp": _utcnow().isoformat(),
                    "model": "deterministic/transcript-qa-v1",
                    "config_hash": config_hash,
                    "input_content_hash": input_hash,
                    "input_files": _input_files(settings, episode_id),
                    "output_files": [str(qa_path)],
                    "finding_count": len(document.findings),
                    "blocking_count": document.summary.blocking_count,
                    "status": document.status,
                    "cost_usd": 0.0,
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        session.add(
            ContentArtifact(
                episode_id=episode_id,
                artifact_type="transcript_qa",
                file_path=str(qa_path),
                model="deterministic/transcript-qa-v1",
                prompt_hash=config_hash,
                retrieval_snapshot_path=None,
            )
        )
        run.status = RunStatus.SUCCESS
        run.completed_at = _utcnow()
        run.estimated_cost_usd = 0.0
        episode.error_message = None
        session.commit()
        return TranscriptQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            status=document.status,
            blocked=document.blocked,
            finding_count=len(document.findings),
            blocking_count=document.summary.blocking_count,
        )
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.completed_at = _utcnow()
        run.error_message = str(exc)[:1000]
        episode.error_message = str(exc)
        session.commit()
        raise


def load_transcript_qa(settings: Settings, episode_id: str) -> dict | None:
    path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_qa.json"
    if not path.exists():
        return None
    try:
        document = TranscriptQADocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document.model_dump(mode="json")


def review_artifacts(settings: Settings, episode_id: str) -> list[str]:
    """Artifact paths that bind a ``transcript_qa`` review task to current content.

    Mirrors the artifact list ``review_gate_transcript_qa`` uses in the pipeline so
    a manual approval/request-changes action (e.g. from the dashboard) is bound to
    the exact same files the pipeline checks via ``has_approved_review_for_artifacts``.
    """
    transcript_dir = Path(settings.transcripts_dir) / episode_id
    transcript_output_dir = Path(settings.outputs_dir) / episode_id / "transcript"
    artifacts = [
        str(transcript_dir / "transcript.corrected.de.txt"),
        str(transcript_dir / "transcript.corrected.structured.de.json"),
        str(transcript_output_dir / "transcript_qa.json"),
    ]
    verification_path = transcript_output_dir / "transcript_verification.json"
    if verification_path.exists():
        artifacts.append(str(verification_path))
    return artifacts


def _evaluate(
    corrected: CorrectedTranscriptDocument,
    verification: TranscriptVerificationDocument | None,
    config: dict,
) -> TranscriptQADocument:
    verification_by_id = {
        region.verification_id: region
        for region in (verification.verified_regions if verification else [])
    }
    findings: list[TranscriptQAFinding] = []
    for segment in corrected.segments:
        categories = list(
            dict.fromkeys(
                _FLAG_TO_CATEGORY[flag] for flag in segment.flags if flag in _FLAG_TO_CATEGORY
            )
        )
        for category in categories:
            severity = _finding_severity(segment, category)
            verification_ids = [
                verification_id
                for verification_id in segment.verification_ids
                if verification_id in verification_by_id
            ]
            secondary_text = " ".join(
                verification_by_id[verification_id].secondary_text
                for verification_id in verification_ids
                if verification_by_id[verification_id].secondary_text
            ).strip()
            findings.append(
                TranscriptQAFinding(
                    finding_id=f"transcript-qa-{len(findings) + 1:04d}",
                    category=category,
                    severity=severity,
                    blocking=severity == "critical" and config["block_on_critical"],
                    segment_ids=[segment.segment_id],
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    message=segment.reason or _finding_message(category),
                    primary_text=segment.original_text,
                    secondary_text=secondary_text or None,
                    corrected_text=segment.corrected_text,
                    verification_ids=verification_ids,
                )
            )

    major_findings = [finding for finding in findings if finding.severity == "major"]
    if len(major_findings) > config["max_major_findings"]:
        for finding in major_findings:
            finding.blocking = True

    direct_block = any(finding.blocking for finding in findings)
    below_threshold = bool(findings) and not direct_block
    blocked = direct_block or (below_threshold and not config["auto_continue_below_threshold"])
    status = "red" if blocked else ("yellow" if findings else "green")
    summary = TranscriptQASummary(
        info_count=sum(finding.severity == "info" for finding in findings),
        minor_count=sum(finding.severity == "minor" for finding in findings),
        major_count=sum(finding.severity == "major" for finding in findings),
        critical_count=sum(finding.severity == "critical" for finding in findings),
        blocking_count=sum(finding.blocking for finding in findings),
    )
    return TranscriptQADocument(
        episode_id=corrected.episode_id,
        generated_at=_utcnow(),
        status=status,
        blocked=blocked,
        findings=findings,
        summary=summary,
        gate_config=config,
    )


def _finding_severity(segment: CorrectedTranscriptSegment, category: str) -> str:
    if category in _INHERENTLY_CRITICAL and segment.status == "unresolved":
        return "critical"
    if category in {"unresolved_date", "incomplete_sentence"} and (
        segment.status == "unresolved" and segment.severity == "critical"
    ):
        return "critical"
    if category == "unresolved_name" and segment.severity == "critical":
        return "critical"
    if segment.severity == "critical":
        return "critical"
    if segment.severity == "major" or segment.status == "unresolved":
        return "major"
    if segment.severity == "minor" or segment.status == "uncertain":
        return "minor"
    return "info"


def _finding_message(category: str) -> str:
    return {
        "unresolved_name": "Ein Personen- oder Eigenname ist nicht eindeutig geklärt.",
        "unresolved_number": "Eine Zahl ist nicht eindeutig geklärt.",
        "unresolved_date": "Ein Datum ist nicht eindeutig geklärt.",
        "unresolved_negation": "Eine Negation ist nicht eindeutig geklärt.",
        "incomplete_sentence": "Ein unvollständiger Satz kann die Bedeutung verändern.",
        "conflicting_transcriptions": "Primär- und Sekundärtranskription widersprechen sich.",
        "casualty_uncertainty": "Eine Opferzahl ist nicht eindeutig geklärt.",
        "result_uncertainty": "Ein Ergebnis ist widersprüchlich.",
        "semantic_role_uncertainty": "Täter-, Opfer- oder andere Rollen sind ungeklärt.",
    }[category]


def _load_config(settings: Settings, episode: Episode) -> dict:
    from btcedu.profiles import get_registry

    profile = get_registry(settings).get(episode.content_profile)
    configured = profile.stage_config.get("transcript_qa", {}) or {}
    return {
        "enabled": bool(
            configured.get("enabled", getattr(settings, "transcript_qa_enabled", True))
        ),
        "block_on_critical": bool(
            configured.get(
                "block_on_critical",
                getattr(settings, "transcript_qa_block_on_critical", True),
            )
        ),
        "max_major_findings": int(
            configured.get(
                "max_major_findings",
                getattr(settings, "transcript_qa_max_major_findings", 3),
            )
        ),
        "auto_continue_below_threshold": bool(
            configured.get(
                "auto_continue_below_threshold",
                getattr(settings, "transcript_qa_auto_continue_below_threshold", True),
            )
        ),
    }


def _load_verification(
    settings: Settings,
    episode_id: str,
) -> TranscriptVerificationDocument | None:
    path = Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json"
    if not path.exists():
        return None
    return TranscriptVerificationDocument.model_validate_json(path.read_text(encoding="utf-8"))


def _input_hash(
    corrected: CorrectedTranscriptDocument,
    verification: TranscriptVerificationDocument | None,
) -> str:
    payload = {
        "corrected": corrected.model_dump(mode="json"),
        "verification": verification.model_dump(mode="json") if verification else None,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _config_hash(config: dict) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _input_files(settings: Settings, episode_id: str) -> list[str]:
    candidates = [
        Path(settings.transcripts_dir) / episode_id / "transcript.corrected.structured.de.json",
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_verification.json",
    ]
    return [str(path) for path in candidates if path.exists()]


def _is_current(
    qa_path: Path,
    provenance_path: Path,
    input_hash: str,
    config_hash: str,
) -> bool:
    if not qa_path.exists() or not provenance_path.exists():
        return False
    stale = qa_path.with_name(qa_path.name + ".stale")
    if stale.exists():
        stale.unlink()
        return False
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        provenance.get("input_content_hash") == input_hash
        and provenance.get("config_hash") == config_hash
    )
