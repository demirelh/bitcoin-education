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
    llm_provider: str = "anthropic"  # "anthropic" or "openai"
    claude_model: str = "claude-sonnet-4-20250514"
    openai_llm_model: str = "gpt-4o"  # fallback model when using openai provider
    claude_max_tokens: int = 4096
    claude_temperature: float = 0.3
    max_retries: int = 3
    retry_base_delay: float = 1.0  # seconds, for retry decorator
    retry_max_delay: float = 60.0  # seconds, max backoff cap
    retry_jitter: bool = True  # add random jitter to prevent thundering herd
    max_stage_retries: int = 3  # max retries per stage on transient errors
    dry_run: bool = False

    # Pipeline Version Control
    pipeline_version: int = 2  # 1 = legacy (chunk->generate->refine), 2 = v2 pipeline
    profiles_dir: str = "btcedu/profiles"
    default_content_profile: str = "bitcoin_podcast"
    max_episode_cost_usd: float = 10.0  # per-episode cost safety cap

    # Image Generation (Sprint 7)
    image_gen_provider: str = "dalle3"  # "dalle3" or "pexels"
    image_gen_model: str = "dall-e-3"
    image_gen_size: str = "1792x1024"  # DALL-E 3 landscape (closest to 1920x1080)
    image_gen_quality: str = "standard"  # "standard" or "hd"
    image_gen_style_prefix: str = (
        "Professional educational content illustration for Bitcoin/cryptocurrency video. "
        "Clean, modern, minimalist design. "
    )

    # Frame Extraction
    frame_extraction_enabled: bool = False
    frame_extract_video_height: int = 720
    frame_extract_scene_threshold: float = 0.3
    frame_extract_min_interval: float = 2.0
    frame_extract_max_frames: int = 100
    frame_extract_style_preset: str = "news_recolor"
    frame_extract_style_provider: str = "ffmpeg"  # "ffmpeg" or "dalle_edit"
    frame_extract_anchor_detection: bool = True
    frame_extract_crop_anchor: bool = True

    # Stock Images / Pexels
    pexels_api_key: str = ""
    pexels_results_per_chapter: int = 5  # Candidates to fetch per chapter (3-8)
    pexels_orientation: str = "landscape"  # "landscape" | "portrait" | "square"
    pexels_download_size: str = "large2x"  # "original" | "large2x" | "landscape"

    # Stock Video / Phase 4
    pexels_video_enabled: bool = False  # Enable video candidate search for b_roll chapters
    pexels_video_per_chapter: int = 2  # Video candidates per b_roll chapter
    pexels_video_max_duration: int = 30  # Max clip duration to download (seconds)
    pexels_video_preferred_quality: str = "hd"  # "hd" or "sd"

    # TTS / ElevenLabs (Sprint 8)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    elevenlabs_style: float = 0.0
    elevenlabs_use_speaker_boost: bool = True

    # Render / ffmpeg (Sprint 9-10)
    render_resolution: str = "1920x1080"
    render_fps: int = 30
    render_crf: int = 23
    render_preset: str = "medium"
    render_audio_bitrate: str = "192k"
    render_font: str = "NotoSans-Bold"
    render_timeout_segment: int = 300  # 5 minutes
    render_timeout_concat: int = 600  # 10 minutes
    render_transition_duration: float = 0.5  # seconds for fade in/out (Sprint 10)

    # Video quality enhancements (each individually toggleable)
    render_ken_burns_enabled: bool = False
    render_ken_burns_zoom_ratio: float = 0.04  # total zoom range (1.0 -> 1.04)

    render_lower_thirds_animated: bool = False
    render_lower_thirds_slide_duration: float = 0.4  # slide-in seconds
    render_lower_thirds_gradient: bool = True  # gradient vs solid box

    render_ticker_enabled: bool = False
    render_ticker_speed: int = 80  # px/sec scroll speed
    render_ticker_height: int = 50  # ticker bar height px
    render_ticker_fontsize: int = 28

    render_intro_enabled: bool = False
    render_intro_duration: float = 4.0
    render_intro_bg_color: str = "#004B87"
    render_intro_show_name: str = "Bitcoin Haberleri"
    render_outro_enabled: bool = False
    render_outro_duration: float = 3.0
    render_outro_bg_color: str = "#004B87"
    render_outro_text: str = "Kaynak: Einundzwanzig Podcast"

    render_color_correction_enabled: bool = False
    render_color_saturation: float = 0.85  # <1.0 = desaturated
    render_color_brightness: float = 0.02
    render_color_blue_shift: float = 0.05  # cool tint

    # YouTube Publishing (Sprint 11)
    youtube_client_secrets_path: str = "data/client_secret.json"
    youtube_credentials_path: str = "data/.youtube_credentials.json"
    youtube_default_privacy: str = "unlisted"  # "unlisted", "private", or "public"
    youtube_upload_chunk_size_mb: int = 10
    youtube_category_id: str = "27"  # Education
    youtube_default_language: str = "tr"

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
