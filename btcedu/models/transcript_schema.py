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


class TranscriptVerificationRegion(BaseModel):
    """One primary/secondary comparison for a bounded audio region."""

    verification_id: str = Field(..., pattern=r"^verify-\d{4,}$")
    source_segment_ids: list[str] = Field(..., min_length=1)
    original_start_seconds: float = Field(..., ge=0)
    original_end_seconds: float = Field(..., ge=0)
    clip_start_seconds: float = Field(..., ge=0)
    clip_end_seconds: float = Field(..., ge=0)
    primary_text: str
    secondary_text: str
    agreement: Literal["high", "medium", "low"]
    risk_types: list[str]
    severity: Literal["none", "minor", "major", "critical"]
    resolution: str | None = None
    cost_usd: float = Field(default=0, ge=0)
    status: Literal["success", "failed"] = "success"
    error: str | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> TranscriptVerificationRegion:
        if self.original_end_seconds < self.original_start_seconds:
            raise ValueError("original_end_seconds must be >= original_start_seconds")
        if self.clip_end_seconds <= self.clip_start_seconds:
            raise ValueError("clip_end_seconds must be greater than clip_start_seconds")
        if self.clip_start_seconds > self.original_start_seconds:
            raise ValueError("clip must start at or before the original region")
        if self.clip_end_seconds < self.original_end_seconds:
            raise ValueError("clip must end at or after the original region")
        return self


class TranscriptVerificationSummary(BaseModel):
    """Aggregate counters and cost for transcript verification."""

    regions_checked: int = Field(..., ge=0)
    critical_count: int = Field(..., ge=0)
    cost_usd: float = Field(default=0, ge=0)


class TranscriptVerificationDocument(BaseModel):
    """Versioned result of selective secondary transcription."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    mode: Literal["full", "suspicious_segments_only"]
    verified_regions: list[TranscriptVerificationRegion]
    summary: TranscriptVerificationSummary

    @model_validator(mode="after")
    def validate_verification_summary(self) -> TranscriptVerificationDocument:
        successful = sum(region.status == "success" for region in self.verified_regions)
        if self.summary.regions_checked != successful:
            raise ValueError("regions_checked must match successful verified regions")
        critical = sum(
            region.status == "success" and region.severity == "critical"
            for region in self.verified_regions
        )
        if self.summary.critical_count != critical:
            raise ValueError("critical_count must match successful critical regions")
        region_cost = round(sum(region.cost_usd for region in self.verified_regions), 6)
        if round(self.summary.cost_usd, 6) != region_cost:
            raise ValueError("summary cost_usd must match verified region costs")
        return self
