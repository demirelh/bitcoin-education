"""Pydantic models for the editorial broadcast script (dual-presenter format).

The broadcast script sits between the approved translation and chapterization.
It decides *what* is broadcast (story ranking), *who* says it (anchor vs.
reporter) and *how it is labelled on screen* (overlay headline + summary).

It never invents facts: every spoken segment must be grounded in the approved
translation of its source story, which :mod:`btcedu.core.script_qa` verifies.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

# Words per minute used to estimate spoken duration for Turkish news delivery.
# Measured on synthesized audio of a full episode (1135 words, 556.5 s): the
# per-chapter rate sits between 121 and 128 wpm, averaging 122. The previous
# value of 150 underestimated every programme by about a fifth.
WORDS_PER_MINUTE = 122


def estimate_duration_seconds(text: str) -> float:
    """Estimate spoken duration of a text in seconds."""
    words = len([w for w in text.split() if w.strip()])
    if not words:
        return 0.0
    return round(words / WORDS_PER_MINUTE * 60.0, 2)


class SpeakerRole(str, Enum):
    """Who speaks a segment."""

    ANCHOR = "anchor_female"
    REPORTER = "reporter_male"


class SegmentPurpose(str, Enum):
    """Dramaturgical purpose of a speaker segment."""

    OPENING = "opening"
    HEADLINES = "headlines"
    INTRODUCTION = "introduction"
    REPORT = "report"
    ANALYSIS = "analysis"
    TRANSITION = "transition"
    BRIEF = "brief"
    WEATHER_HANDOVER = "weather_handover"
    WEATHER = "weather"
    #: Forecast read from an attributed external source because the broadcast
    #: itself carried none — see ``scripter._external_weather_segment``.
    WEATHER_EXTERNAL = "weather_external"
    CLOSING = "closing"


class StoryPriority(str, Enum):
    """Editorial priority assigned to a source story."""

    TOP = "top"
    NORMAL = "normal"
    BRIEF = "brief"
    OMIT = "omit"


class StoryRanking(BaseModel):
    """Relevance scoring for one source story (0-10 per dimension)."""

    story_id: str = Field(..., min_length=1)
    germany_relevance: int = Field(0, ge=0, le=10)
    diaspora_relevance: int = Field(0, ge=0, le=10)
    daily_life_impact: int = Field(0, ge=0, le=10)
    economic_impact: int = Field(0, ge=0, le=10)
    urgency: int = Field(0, ge=0, le=10)
    public_interest: int = Field(0, ge=0, le=10)
    total_score: float = Field(0.0, ge=0.0)
    priority: StoryPriority = StoryPriority.NORMAL
    reasoning_summary: str = ""
    estimated_duration_seconds: float = Field(0.0, ge=0.0)
    manual_override: bool = False


class SpeakerSegment(BaseModel):
    """One continuous piece of speech by a single voice."""

    role: SpeakerRole
    purpose: SegmentPurpose
    text: str = Field(..., min_length=1)
    estimated_duration_seconds: float = Field(0.0, ge=0.0)

    @model_validator(mode="after")
    def _fill_duration(self) -> SpeakerSegment:
        if not self.estimated_duration_seconds:
            self.estimated_duration_seconds = estimate_duration_seconds(self.text)
        return self

    @property
    def word_count(self) -> int:
        return len([w for w in self.text.split() if w.strip()])


class ScriptStory(BaseModel):
    """A source story as it appears in the broadcast."""

    story_id: str = Field(..., min_length=1)
    source_story_id: str = Field(..., min_length=1)
    order: int = Field(..., ge=1)
    priority: StoryPriority = StoryPriority.NORMAL
    category: str = ""
    display_headline: str = Field(..., min_length=1, description="Overlay headline, 2-6 words")
    display_summary: str = Field("", description="Overlay summary line, ~8-15 words")
    viewer_relevance: str = ""
    speaker_sequence: list[SpeakerSegment] = Field(default_factory=list)
    source_segment_ids: list[str] = Field(default_factory=list)
    grounding_references: list[str] = Field(default_factory=list)
    is_weather: bool = False
    overlay_duration_seconds: float = Field(6.0, gt=0.0)
    overlay_priority: int = Field(0, ge=0)

    @property
    def narration(self) -> str:
        return " ".join(seg.text.strip() for seg in self.speaker_sequence if seg.text.strip())

    @property
    def estimated_duration_seconds(self) -> float:
        return round(sum(seg.estimated_duration_seconds for seg in self.speaker_sequence), 2)

    def words_by_role(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for seg in self.speaker_sequence:
            counts[seg.role.value] = counts.get(seg.role.value, 0) + seg.word_count
        return counts


class OmittedStory(BaseModel):
    """Audit record for a source story that is not broadcast."""

    story_id: str
    headline: str = ""
    total_score: float = 0.0
    reason: str = ""
    decision: str = "omitted"
    manual_override: bool = False


class BroadcastScript(BaseModel):
    """Complete editorial script for one episode."""

    schema_version: str = Field("1.0", pattern=r"^\d+\.\d+$")
    episode_id: str = Field(..., min_length=1)
    profile: str = ""
    show_name: str = ""
    slogan: str = ""
    broadcast_date: str = ""
    stories: list[ScriptStory] = Field(default_factory=list)
    rankings: list[StoryRanking] = Field(default_factory=list)
    omissions: list[OmittedStory] = Field(default_factory=list)
    revision: int = Field(0, ge=0)
    generated_by: str = "deterministic"

    @model_validator(mode="after")
    def _validate(self) -> BroadcastScript:
        ids = [s.story_id for s in self.stories]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Duplicate story_id values: {duplicates}")
        expected = list(range(1, len(self.stories) + 1))
        actual = [s.order for s in self.stories]
        if actual != expected:
            raise ValueError(f"Story order must be sequential 1..{len(self.stories)}, got {actual}")
        return self

    @property
    def narration(self) -> str:
        """Spoken narration of the whole broadcast, in order."""
        return "\n\n".join(story.narration for story in self.stories if story.narration.strip())

    @property
    def estimated_duration_seconds(self) -> float:
        return round(sum(s.estimated_duration_seconds for s in self.stories), 2)

    def words_by_role(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for story in self.stories:
            for role, words in story.words_by_role().items():
                counts[role] = counts.get(role, 0) + words
        return counts

    def anchor_share(self) -> float:
        """Fraction of spoken words delivered by the anchor (0.0-1.0)."""
        counts = self.words_by_role()
        total = sum(counts.values())
        if not total:
            return 0.0
        return round(counts.get(SpeakerRole.ANCHOR.value, 0) / total, 4)
