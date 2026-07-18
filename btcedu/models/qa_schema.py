"""Shared schemas for deterministic and model-assisted quality findings."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class QAFindingEvent(BaseModel):
    """One durable status transition in a finding's lifetime."""

    generation: int = Field(0, ge=0)
    status: Literal["open", "resolved", "dismissed"] = "open"
    note: str | None = None


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
    detector: Literal["deterministic", "llm", "llm_escalation"] = "deterministic"
    status: Literal["open", "resolved", "dismissed"] = "open"
    # Provenance of the detector that raised this finding.
    provider: str | None = None
    model: str | None = None
    # Retry generation at which this finding was last observed/updated. 0 is the
    # first (pre-retry) evaluation.
    retry_generation: int = Field(0, ge=0)
    # True when a model finding conflicts with a deterministic finding for the
    # same story/category (both are retained; the deterministic one wins).
    contradiction: bool = False
    # Durable status history across retry generations.
    history: list[QAFindingEvent] = Field(default_factory=list)


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


class QualityGateSummary(BaseModel):
    """Severity + lifecycle totals for the merged quality gate document."""

    info_count: int = Field(0, ge=0)
    minor_count: int = Field(0, ge=0)
    major_count: int = Field(0, ge=0)
    critical_count: int = Field(0, ge=0)
    open_count: int = Field(0, ge=0)
    resolved_count: int = Field(0, ge=0)
    dismissed_count: int = Field(0, ge=0)
    contradiction_count: int = Field(0, ge=0)


class QAModelCall(BaseModel):
    """One LLM QA call (standard or escalation) with its cost accounting."""

    kind: Literal["standard", "escalation"]
    provider: str
    model: str
    generation: int = Field(0, ge=0)
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    cost_usd: float = Field(0.0, ge=0)
    triggered_by: list[str] = Field(default_factory=list)
    ok: bool = True
    error: str | None = None


class QARetryRecord(BaseModel):
    """One targeted repair generation applied between QA evaluations."""

    generation: int = Field(..., ge=1)
    action: Literal["translate", "adapt", "translate_adapt"]
    target_story_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    cost_usd: float = Field(0.0, ge=0)
    resulting_status: Literal["green", "yellow", "red"] | None = None
    note: str | None = None


class QualityGateDocument(BaseModel):
    """Merged deterministic + model QA gate artifact and audit trail."""

    schema_version: Literal[1] = 1
    episode_id: str = Field(..., min_length=1)
    generated_at: datetime
    decision: Literal["green", "yellow", "red"]
    status: Literal["green", "yellow", "red"]
    blocked: bool
    deterministic_status: Literal["green", "yellow", "red"]
    reasons: list[str] = Field(default_factory=list)
    findings: list[QAFinding] = Field(default_factory=list)
    summary: QualityGateSummary
    model_calls: list[QAModelCall] = Field(default_factory=list)
    retry_history: list[QARetryRecord] = Field(default_factory=list)
    retry_generation: int = Field(0, ge=0)
    standard_cost_usd: float = Field(0.0, ge=0)
    escalation_cost_usd: float = Field(0.0, ge=0)
    total_cost_usd: float = Field(0.0, ge=0)
    narration_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    narration_approved: bool = False
    gate_config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_summary(self) -> "QualityGateDocument":
        open_findings = [f for f in self.findings if f.status == "open"]
        counts = {
            severity: sum(f.severity == severity for f in open_findings)
            for severity in ("info", "minor", "major", "critical")
        }
        if self.summary.info_count != counts["info"]:
            raise ValueError("info_count must match open findings")
        if self.summary.minor_count != counts["minor"]:
            raise ValueError("minor_count must match open findings")
        if self.summary.major_count != counts["major"]:
            raise ValueError("major_count must match open findings")
        if self.summary.critical_count != counts["critical"]:
            raise ValueError("critical_count must match open findings")
        if self.summary.open_count != len(open_findings):
            raise ValueError("open_count must match open findings")
        if self.summary.resolved_count != sum(f.status == "resolved" for f in self.findings):
            raise ValueError("resolved_count must match findings")
        if self.summary.dismissed_count != sum(f.status == "dismissed" for f in self.findings):
            raise ValueError("dismissed_count must match findings")
        if self.summary.contradiction_count != sum(f.contradiction for f in self.findings):
            raise ValueError("contradiction_count must match findings")
        if self.status != self.decision:
            raise ValueError("status must mirror decision")
        if self.blocked != (self.decision == "red"):
            raise ValueError("blocked must be True iff decision is red")
        if self.narration_approved and self.decision != "green":
            raise ValueError("narration may only be approved on a green gate")
        return self
