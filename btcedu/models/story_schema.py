"""Pydantic models for story JSON schema validation (Tagesschau news broadcast)."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class StoryCategory(str, Enum):
    """News story category."""

    POLITIK = "politik"
    INTERNATIONAL = "international"
    WIRTSCHAFT = "wirtschaft"
    GESELLSCHAFT = "gesellschaft"
    KULTUR = "kultur"
    SPORT = "sport"
    WETTER = "wetter"
    META = "meta"


class StoryType(str, Enum):
    """News story type."""

    MELDUNG = "meldung"
    BERICHT = "bericht"
    INTERVIEW = "interview"
    KURZMELDUNG = "kurzmeldung"
    WETTER = "wetter"
    INTRO = "intro"
    OUTRO = "outro"


class Story(BaseModel):
    """A single news story extracted from a broadcast transcript."""

    story_id: str = Field(..., min_length=1, description="Unique story ID (e.g., 's01')")
    order: int = Field(..., ge=1, description="1-based sequential order")
    headline_de: str = Field(..., min_length=1, description="German headline")
    category: StoryCategory = Field(..., description="Story category")
    story_type: StoryType = Field(..., description="Story type")
    text_de: str = Field(..., min_length=1, description="German transcript segment (exact text)")
    word_count: int = Field(..., ge=0, description="Word count of text_de")
    estimated_duration_seconds: int = Field(..., ge=0, description="Estimated duration in seconds")
    reporter: str | None = Field(None, description="Reporter name if mentioned")
    location: str | None = Field(None, description="Location if mentioned")
    is_lead_story: bool = Field(False, description="Whether this is the lead story")
    headline_tr: str | None = Field(
        None, description="Turkish headline (filled during translation)"
    )
    text_tr: str | None = Field(None, description="Turkish translation (filled during translation)")
    source_segment_ids: list[str] = Field(
        default_factory=list,
        description="Corrected transcript segments that make up this story",
    )
    source_text: str | None = Field(
        None,
        description="Exact corrected source text assembled from source_segment_ids",
    )
    source_start_seconds: float | None = Field(None, ge=0)
    source_end_seconds: float | None = Field(None, ge=0)
    source_confidence: Literal["high", "medium", "low"] = "high"
    source_flags: list[str] = Field(default_factory=list)
    translator_flags: list[str] = Field(default_factory=list)
    omitted_uncertain_details: list[str] = Field(default_factory=list)
    glossary_terms_used: list[str] = Field(default_factory=list)
    text_adapted_tr: str | None = None
    adaptation_operations: list[str] = Field(default_factory=list)
    narration_sha256: str | None = Field(
        None,
        pattern=r"^[0-9a-f]{64}$",
        description="Hash reserved for a later narration lock gate",
    )

    @model_validator(mode="after")
    def populate_source_defaults(self) -> "Story":
        """Keep legacy story files readable while exposing the new source contract."""
        if self.source_text is None:
            self.source_text = self.text_de
        if (
            self.source_start_seconds is not None
            and self.source_end_seconds is not None
            and self.source_end_seconds < self.source_start_seconds
        ):
            raise ValueError("source_end_seconds must be >= source_start_seconds")
        return self


class StoryDocument(BaseModel):
    """Complete story manifest for a news broadcast episode."""

    schema_version: str = Field(
        "1.0", pattern=r"^\d+\.\d+$", description="Schema version (e.g., '1.0')"
    )
    episode_id: str = Field(..., min_length=1, description="Episode identifier")
    broadcast_date: str = Field(..., description="Broadcast date (ISO format)")
    source_attribution: dict = Field(..., description="Source attribution block")
    total_stories: int = Field(..., ge=0, description="Total number of stories")
    total_duration_seconds: int = Field(..., ge=0, description="Total estimated duration")
    stories: list[Story] = Field(default_factory=list, description="List of stories")

    @model_validator(mode="after")
    def validate_document(self) -> "StoryDocument":
        """Validate document-level constraints."""
        # Auto-correct total_stories to match actual list length (LLM may miscount)
        if self.total_stories != len(self.stories):
            self.total_stories = len(self.stories)

        # Check story_id uniqueness
        story_ids = [s.story_id for s in self.stories]
        if len(story_ids) != len(set(story_ids)):
            duplicates = [sid for sid in story_ids if story_ids.count(sid) > 1]
            raise ValueError(f"Duplicate story_id values: {duplicates}")

        # Check sequential order (1, 2, 3, ...)
        if self.stories:
            actual_order = [s.order for s in self.stories]
            expected_order = list(range(1, len(self.stories) + 1))
            if actual_order != expected_order:
                raise ValueError(
                    f"Story order must be sequential 1..{len(self.stories)}, got {actual_order}"
                )

        return self


class StoryTranslationOutput(BaseModel):
    """Strict per-story output returned by the translation model."""

    story_id: str = Field(..., min_length=1)
    source_segment_ids: list[str] = Field(default_factory=list)
    translated_headline: str = ""
    translated_text: str
    translator_flags: list[str] = Field(default_factory=list)
    omitted_uncertain_details: list[str] = Field(default_factory=list)
    glossary_terms_used: list[str] = Field(default_factory=list)


class StoryAdaptationOutput(BaseModel):
    """Strict per-story output returned by the adaptation model."""

    story_id: str = Field(..., min_length=1)
    adapted_text: str
    operations_applied: list[str] = Field(default_factory=list)
