"""Typed contracts for editorial imports and data-only research."""

from typing import Literal

from pydantic import BaseModel, Field

ClaimType = Literal[
    "transcription_error",
    "fact",
    "quote",
    "opinion",
    "prediction",
    "unverifiable",
]


class ClaimDraft(BaseModel):
    claim_key: str = Field(..., min_length=1, max_length=128)
    statement: str = Field(..., min_length=1)
    claim_type: ClaimType
    subject: str | None = None
    location: str | None = None
    event_date: str | None = None
    numeric_value: str | None = None
    unit: str | None = None
    attribution: str | None = None
    modality: str | None = None
    material: bool = True


class ResearchQueryPlan(BaseModel):
    query_key: str = Field(..., min_length=1, max_length=128)
    query_text: str = Field(..., min_length=1, max_length=1000)
    language: Literal["de", "tr", "en"]
    purpose: Literal["support", "counter", "context"]
    estimated_cost_usd: float = Field(default=0.0, ge=0)
    max_results: int = Field(default=5, ge=1, le=20)


class SearchHitData(BaseModel):
    title: str = ""
    url: str
    snippet: str = ""
    publisher: str | None = None
    published_at: str | None = None


class EvidenceDraft(BaseModel):
    canonical_url: str
    relation: Literal["supports", "contradicts", "context", "inconclusive"]
    passage: str = Field(..., min_length=1)
    translated_passage: str | None = None
    rationale: str = Field(..., min_length=1)
    provenance_family: str | None = None
