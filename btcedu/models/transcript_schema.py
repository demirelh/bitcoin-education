"""Pydantic schemas for structured transcription and deterministic analysis."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_SEGMENT_ID_RE = re.compile(r"^seg-\d{4,}$")


def make_segment_id(index: int) -> str:
    """Return the stable, one-based ID for a transcript segment."""
    if index < 0:
        raise ValueError("segment index must be non-negative")
    return f"seg-{index + 1:04d}"


class TranscriptSegment(BaseModel):
    """One timestamped transcription segment."""

    segment_id: str = Field(..., pattern=_SEGMENT_ID_RE.pattern)
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., ge=0)
    text: str = Field(..., min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_timing(self) -> TranscriptSegment:
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class TranscriptUsage(BaseModel):
    """Provider usage and estimated transcription cost."""

    audio_seconds: float = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)


class TranscriptDocument(BaseModel):
    """Versioned structured transcript persisted alongside legacy text files."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    language: str = Field(..., min_length=2)
    text: str
    segments: list[TranscriptSegment]
    usage: TranscriptUsage = Field(default_factory=TranscriptUsage)

    @model_validator(mode="after")
    def validate_segments(self) -> TranscriptDocument:
        expected_ids = [make_segment_id(i) for i in range(len(self.segments))]
        actual_ids = [segment.segment_id for segment in self.segments]
        if actual_ids != expected_ids:
            raise ValueError(
                "segment IDs must be unique, ordered, and sequential "
                f"({expected_ids!r} expected, got {actual_ids!r})"
            )
        return self


class SuspiciousTranscriptSegment(BaseModel):
    """A conservatively flagged candidate that may need verification."""

    segment_id: str = Field(..., pattern=_SEGMENT_ID_RE.pattern)
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., ge=0)
    severity: Literal["minor", "major", "critical"]
    reasons: list[str] = Field(..., min_length=1)


class TranscriptAnalysisSummary(BaseModel):
    """Aggregate counters for a transcript analysis."""

    segment_count: int = Field(..., ge=0)
    suspicious_count: int = Field(..., ge=0)
    critical_count: int = Field(..., ge=0)


class TranscriptAnalysisDocument(BaseModel):
    """Versioned output of the deterministic transcript analysis stage."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(..., min_length=1)
    suspicious_segments: list[SuspiciousTranscriptSegment]
    summary: TranscriptAnalysisSummary

    @model_validator(mode="after")
    def validate_summary(self) -> TranscriptAnalysisDocument:
        if self.summary.suspicious_count != len(self.suspicious_segments):
            raise ValueError("suspicious_count must match suspicious_segments")
        critical_count = sum(segment.severity == "critical" for segment in self.suspicious_segments)
        if self.summary.critical_count != critical_count:
            raise ValueError("critical_count must match critical suspicious segments")
        if self.summary.suspicious_count > self.summary.segment_count:
            raise ValueError("suspicious_count cannot exceed segment_count")
        return self
