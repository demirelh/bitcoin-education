from datetime import datetime

from pydantic import BaseModel


class EpisodeInfo(BaseModel):
    """Parsed episode info from RSS/YouTube feed."""

    episode_id: str
    title: str
    published_at: datetime | None = None
    url: str
    source: str = "youtube_rss"


class PipelineStatus(BaseModel):
    """Status summary for an episode's pipeline progress."""

    episode_id: int
    video_id: str
    title: str
    status: str
    detected_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    total_cost_usd: float = 0.0
