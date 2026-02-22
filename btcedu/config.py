import logging
import warnings

from pydantic import model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    whisper_api_key: str = ""  # falls back to openai_api_key if empty
    claude_api_key: str = ""  # deprecated alias for anthropic_api_key

    @model_validator(mode="after")
    def _migrate_claude_api_key(self) -> "Settings":
        """Support CLAUDE_API_KEY as a deprecated alias for ANTHROPIC_API_KEY."""
        if self.claude_api_key:
            if not self.anthropic_api_key:
                self.anthropic_api_key = self.claude_api_key
                warnings.warn(
                    "CLAUDE_API_KEY is deprecated. Use ANTHROPIC_API_KEY instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            else:
                logger.debug(
                    "Both ANTHROPIC_API_KEY and CLAUDE_API_KEY set; using ANTHROPIC_API_KEY."
                )
            self.claude_api_key = ""  # clear after migration
        return self

    # Database
    database_url: str = "sqlite:///data/btcedu.db"

    # Podcast Source
    source_type: str = "youtube_rss"  # "youtube_rss" or "rss"
    podcast_youtube_channel_id: str = ""
    podcast_rss_url: str = ""

    # Audio / Raw Data
    raw_data_dir: str = "data/raw"
    audio_format: str = "m4a"
    max_audio_chunk_mb: int = 24

    # Transcription
    transcripts_dir: str = "data/transcripts"
    whisper_model: str = "whisper-1"
    whisper_language: str = "de"

    # Chunking
    chunks_dir: str = "data/chunks"
    chunk_size: int = 1500  # chars (~350 tokens)
    chunk_overlap: float = 0.15  # 15% overlap

    # Content Generation
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 4096
    claude_temperature: float = 0.3
    max_retries: int = 3
    dry_run: bool = False

    # Output
    outputs_dir: str = "data/outputs"

    # Reports & Logs
    reports_dir: str = "data/reports"
    logs_dir: str = "data/logs"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def rss_url(self) -> str:
        if self.podcast_rss_url:
            return self.podcast_rss_url
        if self.podcast_youtube_channel_id:
            return (
                f"https://www.youtube.com/feeds/videos.xml"
                f"?channel_id={self.podcast_youtube_channel_id}"
            )
        return ""

    @property
    def effective_whisper_api_key(self) -> str:
        return self.whisper_api_key or self.openai_api_key


def get_settings() -> Settings:
    return Settings()
