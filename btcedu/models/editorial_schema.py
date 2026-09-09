"""Typed contracts for importing stories into the editorial domain."""

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
