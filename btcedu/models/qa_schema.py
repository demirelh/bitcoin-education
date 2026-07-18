"""Shared schemas for deterministic and model-assisted quality findings."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class QAFinding(BaseModel):
    """One persistent, auditable quality finding."""

    finding_id: str = Field(..., pattern=r"^qa-\d{4,}$")
    story_id: str | None = None
    category: str = Field(..., min_length=1)
    severity: Literal["info", "minor", "major", "critical"]
    source_excerpt: str
    target_excerpt: str
    explanation: str = Field(..., min_length=1)
    required_action: str = Field(..., min_length=1)
    source_segment_ids: list[str] = Field(default_factory=list)
    detector: Literal["deterministic", "llm"] = "deterministic"
    status: Literal["open", "resolved", "dismissed"] = "open"


class QASummary(BaseModel):
    """Severity totals shared by QA documents."""

    info_count: int = Field(0, ge=0)
    minor_count: int = Field(0, ge=0)
    major_count: int = Field(0, ge=0)
    critical_count: int = Field(0, ge=0)
    open_count: int = Field(0, ge=0)


class TranslationQADocument(BaseModel):
    """Zero-cost deterministic translation QA artifact."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(..., min_length=1)
    generated_at: datetime
    mode: Literal["deterministic"] = "deterministic"
    status: Literal["green", "yellow", "red"]
    findings: list[QAFinding] = Field(default_factory=list)
    summary: QASummary
    checker_results: dict[str, dict]
    story_coverage: dict
    number_coverage: dict
    glossary_coverage: dict
    cost_usd: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> "TranslationQADocument":
        counts = {
            severity: sum(finding.severity == severity for finding in self.findings)
            for severity in ("info", "minor", "major", "critical")
        }
        if self.summary.info_count != counts["info"]:
            raise ValueError("info_count must match findings")
        if self.summary.minor_count != counts["minor"]:
            raise ValueError("minor_count must match findings")
        if self.summary.major_count != counts["major"]:
            raise ValueError("major_count must match findings")
        if self.summary.critical_count != counts["critical"]:
            raise ValueError("critical_count must match findings")
        if self.summary.open_count != sum(f.status == "open" for f in self.findings):
            raise ValueError("open_count must match findings")
        if self.cost_usd != 0:
            raise ValueError("deterministic translation QA must have zero cost")
        return self
