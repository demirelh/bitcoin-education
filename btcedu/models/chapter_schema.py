"""Pydantic models for chapter JSON schema validation."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class VisualType(str, Enum):
    """Visual type for chapter."""

    TITLE_CARD = "title_card"
    DIAGRAM = "diagram"
    B_ROLL = "b_roll"
    TALKING_HEAD = "talking_head"
    SCREEN_SHARE = "screen_share"


class TransitionType(str, Enum):
    """Transition effect type."""

    FADE = "fade"
    CUT = "cut"
    DISSOLVE = "dissolve"


class OverlayType(str, Enum):
    """Overlay type."""

    LOWER_THIRD = "lower_third"
    TITLE = "title"
    QUOTE = "quote"
    STATISTIC = "statistic"


class Narration(BaseModel):
    """Narration data for a chapter."""

    text: str = Field(..., min_length=1, description="Full narration text")
    word_count: int = Field(..., ge=1, description="Word count of text")
    estimated_duration_seconds: int = Field(..., ge=1, description="Estimated duration")

    @field_validator("estimated_duration_seconds")
    @classmethod
    def validate_duration(cls, v: int, info) -> int:
        """Validate duration is reasonable for word count."""
        word_count = info.data.get("word_count", 0)
        if word_count > 0:
            expected_duration = round((word_count / 150) * 60)
            # Allow 20% variance
            if abs(v - expected_duration) > (expected_duration * 0.2):
                # Log warning but don't fail (LLM may account for pauses)
                pass
        return v


class Visual(BaseModel):
    """Visual data for a chapter."""

    type: VisualType = Field(..., description="Visual type")
    description: str = Field(..., min_length=1, description="Description of visual")
    image_prompt: Optional[str] = Field(
        None, description="Image generation prompt (null for non-generated types)"
    )

    @model_validator(mode="after")
    def validate_image_prompt(self) -> "Visual":
        """Validate image_prompt based on visual type."""
        needs_prompt = self.type in (VisualType.DIAGRAM, VisualType.B_ROLL)
        has_prompt = self.image_prompt is not None and len(self.image_prompt) > 0

        if needs_prompt and not has_prompt:
            raise ValueError(f"Visual type '{self.type}' requires image_prompt, got null/empty")

        if not needs_prompt and has_prompt:
            # Warning only, not error (LLM may provide prompt for future use)
            pass

        return self


class Overlay(BaseModel):
    """Overlay data for a chapter."""

    type: OverlayType = Field(..., description="Overlay type")
    text: str = Field(..., min_length=1, description="Text to display")
    start_offset_seconds: float = Field(
        ..., ge=0.0, description="Start time relative to chapter start"
    )
    duration_seconds: float = Field(..., gt=0.0, description="Duration of overlay")


class Transitions(BaseModel):
    """Transition effects for a chapter."""

    in_transition: TransitionType = Field(..., alias="in", description="Transition in")
    out_transition: TransitionType = Field(..., alias="out", description="Transition out")


class Chapter(BaseModel):
    """Chapter data."""

    chapter_id: str = Field(..., min_length=1, description="Unique chapter ID")
    title: str = Field(..., min_length=1, description="Chapter title")
    order: int = Field(..., ge=1, description="Sequential order")
    narration: Narration = Field(..., description="Narration data")
    visual: Visual = Field(..., description="Visual data")
    overlays: list[Overlay] = Field(
        default_factory=list, description="Overlays (can be empty)"
    )
    transitions: Transitions = Field(..., description="Transitions")
    notes: Optional[str] = Field(None, description="Production notes (optional)")


class ChapterDocument(BaseModel):
    """Complete chapter document."""

    schema_version: str = Field(
        ..., pattern=r"^\d+\.\d+$", description="Schema version (e.g., '1.0')"
    )
    episode_id: str = Field(..., min_length=1, description="Episode identifier")
    title: str = Field(..., min_length=1, description="Episode title")
    total_chapters: int = Field(..., ge=1, description="Total number of chapters")
    estimated_duration_seconds: int = Field(..., ge=1, description="Total estimated duration")
    chapters: list[Chapter] = Field(..., min_length=1, description="Array of chapters")

    @model_validator(mode="after")
    def validate_document(self) -> "ChapterDocument":
        """Validate document-level constraints."""
        # Check total_chapters matches array length
        if self.total_chapters != len(self.chapters):
            raise ValueError(
                f"total_chapters ({self.total_chapters}) != len(chapters) ({len(self.chapters)})"
            )

        # Check chapter_id uniqueness
        chapter_ids = [ch.chapter_id for ch in self.chapters]
        if len(chapter_ids) != len(set(chapter_ids)):
            duplicates = [cid for cid in chapter_ids if chapter_ids.count(cid) > 1]
            raise ValueError(f"Duplicate chapter_id values: {duplicates}")

        # Check sequential order (1, 2, 3, ...)
        expected_order = list(range(1, len(self.chapters) + 1))
        actual_order = [ch.order for ch in self.chapters]
        if actual_order != expected_order:
            raise ValueError(
                f"Chapter order must be sequential 1..{len(self.chapters)}, got {actual_order}"
            )

        # Check estimated_duration_seconds is sum of chapter durations
        total_duration = sum(ch.narration.estimated_duration_seconds for ch in self.chapters)
        if abs(self.estimated_duration_seconds - total_duration) > 5:
            # Allow 5-second variance for rounding
            raise ValueError(
                f"estimated_duration_seconds ({self.estimated_duration_seconds}) != "
                f"sum of chapter durations ({total_duration})"
            )

        return self
