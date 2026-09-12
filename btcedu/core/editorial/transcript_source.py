"""Finding today's transcripts and choosing what is worth an article.

The newsroom does not need to segment a broadcast again. The video pipeline
already turns each transcript into ``stories.json``, and that file is exactly
the shape the editorial workflow wants. Reading it is free, deterministic and
repeatable, where a second model pass over the transcript would be none of the
three.

Production is read and never written: the files are opened read-only and the
newsroom's own state lives entirely in the development data directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from btcedu.models.story_schema import Story, StoryCategory, StoryType

#: Categories and types that are part of a broadcast but not a news story.
#: Weather is excluded on purpose as well: the video pipeline renders it from
#: measured data, and there is nothing here to research or source.
SKIPPED_CATEGORIES = frozenset({StoryCategory.META.value, StoryCategory.WETTER.value})
SKIPPED_TYPES = frozenset(
    {StoryType.INTRO.value, StoryType.OUTRO.value, StoryType.WETTER.value}
)

#: A story shorter than this has no reportable substance — a one-line teaser
#: cannot carry a claim, let alone evidence for it.
MIN_WORDS = 40

#: Where a Turkish article ends up on the site, per German story category.
SECTIONS = {
    StoryCategory.POLITIK.value: "almanya",
    StoryCategory.INTERNATIONAL.value: "dunya",
    StoryCategory.WIRTSCHAFT.value: "ekonomi",
    StoryCategory.GESELLSCHAFT.value: "toplum",
    StoryCategory.KULTUR.value: "kultur",
    StoryCategory.SPORT.value: "spor",
}
DEFAULT_SECTION = "haber"


@dataclass(frozen=True)
class TranscriptSource:
    """One broadcast whose stories are available for the newsroom."""

    episode_id: str
    broadcast_date: date
    stories_path: Path
    attribution: str

    @property
    def key(self) -> str:
        return self.episode_id


@dataclass(frozen=True)
class SelectedStory:
    """A broadcast story chosen for an article, with its routing decided."""

    story: Story
    section: str
    episode_id: str
    broadcast_date: date
    attribution: str

    @property
    def key(self) -> str:
        """Stable across reruns, because the article identity depends on it."""
        return f"{self.episode_id}:{self.story.story_id}"


def _attribution(raw: object) -> str:
    """The pipeline stores this as a mapping; readers need one line of credit."""
    if isinstance(raw, dict):
        parts = [
            str(raw[key])
            for key in ("broadcaster", "source", "programme", "program")
            if raw.get(key)
        ]
        if parts:
            return " · ".join(dict.fromkeys(parts))
    return str(raw) if raw else "ARD tagesschau"


def discover_transcripts(
    outputs_dir: Path,
    *,
    prefix: str = "tagesschau_",
    lookback_days: int = 3,
    today: date | None = None,
) -> list[TranscriptSource]:
    """Broadcasts from the last few days that have been segmented already.

    The lookback is what covers a late transcript. A run that finds nothing new
    today will pick up yesterday's broadcast tomorrow if it only finished after
    the timer fired, without anyone having to notice.
    """
    outputs = Path(outputs_dir)
    if not outputs.is_dir():
        return []
    cutoff = (today or date.today()).toordinal() - max(0, lookback_days)
    found: list[TranscriptSource] = []
    for episode_dir in sorted(outputs.iterdir()):
        if not episode_dir.is_dir() or not episode_dir.name.startswith(prefix):
            continue
        stories_path = episode_dir / "stories.json"
        if not stories_path.is_file():
            continue
        try:
            payload = json.loads(stories_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_date = payload.get("broadcast_date")
        try:
            broadcast = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            continue
        if broadcast.toordinal() < cutoff:
            continue
        found.append(
            TranscriptSource(
                episode_id=str(payload.get("episode_id") or episode_dir.name),
                broadcast_date=broadcast,
                stories_path=stories_path,
                attribution=_attribution(payload.get("source_attribution")),
            )
        )
    found.sort(key=lambda source: source.broadcast_date, reverse=True)
    return found


def select_stories(source: TranscriptSource, *, limit: int) -> list[SelectedStory]:
    """The reportable stories of one broadcast, best first.

    Ordering is the broadcast's own: a bulletin leads with what its editors
    considered most important, and inventing a different ranking here would
    mean claiming an editorial judgement the newsroom has not made.
    """
    payload = json.loads(source.stories_path.read_text(encoding="utf-8"))
    selected: list[SelectedStory] = []
    for raw in payload.get("stories", []):
        if raw.get("category") in SKIPPED_CATEGORIES:
            continue
        if raw.get("story_type") in SKIPPED_TYPES:
            continue
        text = str(raw.get("source_text") or raw.get("text_de") or "")
        if len(text.split()) < MIN_WORDS:
            continue
        try:
            story = Story.model_validate(raw)
        except Exception:  # noqa: BLE001 - a malformed story is skipped, not fatal
            continue
        selected.append(
            SelectedStory(
                story=story,
                section=SECTIONS.get(story.category.value, DEFAULT_SECTION),
                episode_id=source.episode_id,
                broadcast_date=source.broadcast_date,
                attribution=source.attribution,
            )
        )
    selected.sort(key=lambda item: (not item.story.is_lead_story, item.story.order))
    return selected[: max(0, limit)]


def broadcast_datetime(source: TranscriptSource) -> datetime:
    """When the material was broadcast, which is not when it is published.

    Keeping these apart is the whole reason the date travels with the story:
    an article written tomorrow from tonight's bulletin must not present the
    events as having happened tomorrow.
    """
    return datetime.combine(source.broadcast_date, datetime.min.time()).replace(hour=20)
