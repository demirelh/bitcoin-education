import logging
import math
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)


def transcribe_audio(
    audio_path: str,
    api_key: str,
    model: str = "whisper-1",
    language: str = "de",
    max_chunk_mb: int = 24,
) -> str:
    """Transcribe an audio file using OpenAI Whisper API.

    If the file exceeds max_chunk_mb, it is split into segments first.

    Returns:
        Full transcript text.
    """
    file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)

    if file_size_mb <= max_chunk_mb:
        return _transcribe_single(audio_path, api_key, model, language)

    logger.info("Audio %.1f MB > %d MB limit, splitting...", file_size_mb, max_chunk_mb)
    return _transcribe_chunked(audio_path, api_key, model, language, max_chunk_mb)


def _transcribe_single(
    audio_path: str,
    api_key: str,
    model: str,
    language: str,
) -> str:
    """Transcribe a single audio file."""
    client = OpenAI(api_key=api_key)
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model=model,
            file=f,
            language=language,
            response_format="text",
        )
    return response.strip()


def _transcribe_chunked(
    audio_path: str,
    api_key: str,
    model: str,
    language: str,
    max_chunk_mb: int,
) -> str:
    """Split audio and transcribe each segment, then concatenate."""
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    duration_ms = len(audio)
    file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)

    # Calculate segment duration to stay under the size limit
    num_segments = math.ceil(file_size_mb / max_chunk_mb)
    segment_ms = duration_ms // num_segments

    segments = []
    for i in range(num_segments):
        start = i * segment_ms
        end = min((i + 1) * segment_ms, duration_ms)
        segments.append(audio[start:end])

    logger.info("Split into %d segments of ~%ds each", len(segments), segment_ms // 1000)

    tmp_dir = Path(audio_path).parent / "_whisper_tmp"
    tmp_dir.mkdir(exist_ok=True)

    parts = []
    try:
        for i, segment in enumerate(segments):
            tmp_path = tmp_dir / f"segment_{i:03d}.mp3"
            segment.export(str(tmp_path), format="mp3")
            logger.info("Transcribing segment %d/%d...", i + 1, len(segments))
            text = _transcribe_single(str(tmp_path), api_key, model, language)
            parts.append(text)
    finally:
        # Clean up temp files
        for f in tmp_dir.glob("*"):
            f.unlink()
        tmp_dir.rmdir()

    return "\n\n".join(parts)


def clean_transcript(raw_text: str) -> str:
    """Basic transcript cleanup: normalize whitespace, strip artifacts."""
    import re

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()
