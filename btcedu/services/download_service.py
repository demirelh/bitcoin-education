import logging
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from btcedu.services.retry import retry_on_transient

logger = logging.getLogger(__name__)

_ALLOWED_AUDIO_FORMATS = frozenset({"m4a", "mp3", "wav", "opus", "flac", "aac", "ogg"})
_URL_PATTERN = re.compile(r"^https?://[^\s]+$")


def _validate_url(url: str) -> None:
    """Validate that *url* looks like an HTTP(S) URL."""
    if not isinstance(url, str) or not _URL_PATTERN.match(url):
        raise ValueError(f"Invalid URL: {url!r}")


def _find_ytdlp() -> str:
    """Locate the yt-dlp binary or raise a clear error."""
    path = shutil.which("yt-dlp")
    if path:
        return path
    # Fall back to the venv bin directory (useful under gunicorn/systemd).
    venv_path = Path(sys.executable).parent / "yt-dlp"
    if venv_path.is_file():
        return str(venv_path)
    raise RuntimeError(
        "yt-dlp is not installed or not on PATH. Install it with: pip install yt-dlp"
    )


@retry_on_transient(max_retries=2, base_delay=2.0)
def download_audio(
    url: str,
    output_dir: str,
    audio_format: str = "m4a",
) -> str:
    """Download audio from a URL using yt-dlp.

    Args:
        url: Video/podcast URL to download from.
        output_dir: Directory to save the audio file into.
        audio_format: Audio format to extract (default: m4a).

    Returns:
        Path to the downloaded audio file.

    Raises:
        RuntimeError: If yt-dlp fails.
    """
    _validate_url(url)
    if audio_format not in _ALLOWED_AUDIO_FORMATS:
        raise ValueError(f"Unsupported audio format: {audio_format!r}")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    output_template = str(out_path / "audio.%(ext)s")

    ytdlp = _find_ytdlp()

    cmd = [
        ytdlp,
        "--extract-audio",
        "--audio-format",
        shlex.quote(audio_format),
        "--output",
        shlex.quote(output_template),
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        shlex.quote(url),
    ]

    logger.info("Downloading audio: %s -> %s", url, output_dir)

    result = subprocess.run(  # noqa: S603 — inputs validated above
        cmd, capture_output=True, text=True, timeout=1800
    )

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed (exit {result.returncode}): {result.stderr.strip()}")

    # Find the actual output file
    audio_file = out_path / f"audio.{audio_format}"
    if not audio_file.exists():
        # yt-dlp may have used a different extension
        candidates = list(out_path.glob("audio.*"))
        if candidates:
            audio_file = candidates[0]
        else:
            raise RuntimeError(f"No audio file found in {output_dir} after download")

    logger.info("Downloaded: %s", audio_file)
    return str(audio_file)


def download_video(
    url: str,
    output_dir: str,
    max_height: int = 720,
) -> str:
    """Download video from a URL using yt-dlp.

    Downloads the best video+audio stream up to *max_height* resolution
    and merges into an MP4 container.

    Args:
        url: Video URL to download from.
        output_dir: Directory to save the video file into.
        max_height: Maximum video height in pixels (default: 720).

    Returns:
        Path to the downloaded video file.

    Raises:
        RuntimeError: If yt-dlp fails.
    """
    _validate_url(url)
    if not isinstance(max_height, int) or max_height <= 0 or max_height > 4320:
        raise ValueError(f"Invalid max_height: {max_height!r}")

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    output_template = str(out_path / "video.%(ext)s")

    ytdlp = _find_ytdlp()

    format_spec = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]"
    cmd = [
        ytdlp,
        "--format",
        shlex.quote(format_spec),
        "--merge-output-format",
        "mp4",
        "--output",
        shlex.quote(output_template),
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        shlex.quote(url),
    ]

    logger.info("Downloading video: %s -> %s", url, output_dir)

    result = subprocess.run(  # noqa: S603 — inputs validated above
        cmd, capture_output=True, text=True, timeout=1800
    )

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed (exit {result.returncode}): {result.stderr.strip()}")

    # Find the actual output file
    video_file = out_path / "video.mp4"
    if not video_file.exists():
        candidates = list(out_path.glob("video.*"))
        if candidates:
            video_file = candidates[0]
        else:
            raise RuntimeError(f"No video file found in {output_dir} after download")

    logger.info("Downloaded video: %s", video_file)
    return str(video_file)
