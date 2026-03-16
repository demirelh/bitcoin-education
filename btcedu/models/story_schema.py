"""Pydantic models for story JSON schema validation (Tagesschau news broadcast)."""

from enum import Enum

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
    text_tr: str | None = Field(
        None, description="Turkish translation (filled during translation)"
    )


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
        # Check total_stories matches list length
        if self.total_stories != len(self.stories):
            raise ValueError(
                f"total_stories ({self.total_stories}) != len(stories) ({len(self.stories)})"
            )

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
                    f"Story order must be sequential 1..{len(self.stories)}, "
                    f"got {actual_order}"
                )

        return self
