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

    # Pipeline Version Control
    pipeline_version: int = 1  # 1 = legacy (chunk->generate->refine), 2 = v2 pipeline
    max_episode_cost_usd: float = 10.0  # per-episode cost safety cap

    # Image Generation (Sprint 7)
    image_gen_provider: str = "dalle3"  # "dalle3" (only option for now)
    image_gen_model: str = "dall-e-3"
    image_gen_size: str = "1792x1024"  # DALL-E 3 landscape (closest to 1920x1080)
    image_gen_quality: str = "standard"  # "standard" or "hd"
    image_gen_style_prefix: str = (
        "Professional educational content illustration for Bitcoin/cryptocurrency video. "
        "Clean, modern, minimalist design. "
    )

    # TTS / ElevenLabs (Sprint 8)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    elevenlabs_style: float = 0.0
    elevenlabs_use_speaker_boost: bool = True

    # Render / ffmpeg (Sprint 9)
    render_resolution: str = "1920x1080"
    render_fps: int = 30
    render_crf: int = 23
    render_preset: str = "medium"
    render_audio_bitrate: str = "192k"
    render_font: str = "NotoSans-Bold"
    render_timeout_segment: int = 300  # 5 minutes
    render_timeout_concat: int = 600  # 10 minutes

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
