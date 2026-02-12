import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    output_template = str(out_path / f"audio.%(ext)s")

    # Prefer yt-dlp from the same venv as the running Python interpreter,
    # so it works even when PATH doesn't include the venv bin directory
    # (e.g. when running under gunicorn/systemd).
    ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")

    cmd = [
        ytdlp,
        "--extract-audio",
        "--audio-format", audio_format,
        "--output", output_template,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        url,
    ]

    logger.info("Downloading audio: %s -> %s", url, output_dir)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}): {result.stderr.strip()}"
        )

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
