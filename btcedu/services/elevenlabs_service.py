"""ElevenLabs TTS service abstraction."""

import logging
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

# ElevenLabs Starter pricing (per 1000 characters)
ELEVENLABS_COST_PER_1K_CHARS = 0.30

# Maximum characters per API request
MAX_CHARS_PER_REQUEST = 5000

# ElevenLabs API base URL
API_BASE = "https://api.elevenlabs.io/v1"


@dataclass
class TTSRequest:
    """Request for text-to-speech synthesis."""

    text: str
    voice_id: str
    model: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True


@dataclass
class TTSResponse:
    """Response from text-to-speech synthesis."""

    audio_bytes: bytes
    duration_seconds: float
    sample_rate: int
    model: str
    voice_id: str
    character_count: int
    cost_usd: float


class TTSService(Protocol):
    """Protocol for TTS services."""

    def synthesize(self, request: TTSRequest) -> TTSResponse: ...


class ElevenLabsService:
    """ElevenLabs TTS service using REST API."""

    def __init__(
        self,
        api_key: str,
        default_voice_id: str = "",
        default_model: str = "eleven_multilingual_v2",
    ):
        self.api_key = api_key
        self.default_voice_id = default_voice_id
        self.default_model = default_model

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        """Synthesize text to speech.

        Chunks text if >5000 chars, calls API per chunk,
        concatenates if multi-chunk, measures duration via pydub.
        """
        voice_id = request.voice_id or self.default_voice_id
        model = request.model or self.default_model

        if not voice_id:
            raise ValueError("No voice_id provided and no default configured")

        voice_settings = {
            "stability": request.stability,
            "similarity_boost": request.similarity_boost,
            "style": request.style,
            "use_speaker_boost": request.use_speaker_boost,
        }

        char_count = len(request.text)

        # Chunk if necessary
        if char_count > MAX_CHARS_PER_REQUEST:
            chunks = _chunk_text(request.text, MAX_CHARS_PER_REQUEST)
            logger.info(
                "Text length %d exceeds limit, split into %d chunks",
                char_count,
                len(chunks),
            )
        else:
            chunks = [request.text]

        # Synthesize each chunk
        audio_parts = []
        for i, chunk in enumerate(chunks):
            logger.info("Synthesizing chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
            audio_data = self._call_with_retry(chunk, voice_id, model, voice_settings)
            audio_parts.append(audio_data)

        # Concatenate if multi-chunk
        if len(audio_parts) == 1:
            audio_bytes = audio_parts[0]
        else:
            audio_bytes = _concatenate_audio(audio_parts)

        # Measure duration
        duration_seconds, sample_rate = _measure_duration(audio_bytes)

        # Compute cost
        cost_usd = _compute_cost(char_count)

        logger.info(
            "TTS complete: %d chars, %.1fs, %d Hz, $%.3f",
            char_count,
            duration_seconds,
            sample_rate,
            cost_usd,
        )

        return TTSResponse(
            audio_bytes=audio_bytes,
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
            model=model,
            voice_id=voice_id,
            character_count=char_count,
            cost_usd=cost_usd,
        )

    def _call_with_retry(
        self,
        text: str,
        voice_id: str,
        model: str,
        voice_settings: dict,
        max_retries: int = 3,
    ) -> bytes:
        """Call ElevenLabs API with exponential backoff on rate limits."""
        url = f"{API_BASE}/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": model,
            "voice_settings": voice_settings,
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=120)

                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            "ElevenLabs rate limit (attempt %d/%d), retrying in %ds...",
                            attempt + 1,
                            max_retries,
                            wait_time,
                        )
                        time.sleep(wait_time)
                        continue
                    else:
                        raise RuntimeError(
                            f"ElevenLabs rate limit exceeded after {max_retries} retries"
                        )

                if response.status_code != 200:
                    error_detail = response.text[:200]
                    raise RuntimeError(
                        f"ElevenLabs API error {response.status_code}: {error_detail}"
                    )

                return response.content

            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "ElevenLabs request error (attempt %d/%d): %s, retrying in %ds...",
                        attempt + 1,
                        max_retries,
                        e,
                        wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(
                        f"ElevenLabs request failed after {max_retries} retries: {e}"
                    ) from e

        raise RuntimeError(f"ElevenLabs call failed after {max_retries} attempts")


def _chunk_text(text: str, limit: int = MAX_CHARS_PER_REQUEST) -> list[str]:
    """Split text at sentence boundaries, never exceeding limit per chunk."""
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Find the best split point within the limit
        split_pos = limit
        # Try sentence-ending punctuation
        for sep in [". ", "! ", "? "]:
            pos = remaining.rfind(sep, 0, limit)
            if pos != -1:
                split_pos = pos + len(sep)
                break
        else:
            # Fallback: split at last space
            pos = remaining.rfind(" ", 0, limit)
            if pos != -1:
                split_pos = pos + 1

        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:]

    return chunks


def _concatenate_audio(chunks: list[bytes]) -> bytes:
    """Join multiple MP3 audio chunks using pydub."""
    from pydub import AudioSegment

    combined = AudioSegment.empty()
    for chunk_bytes in chunks:
        segment = AudioSegment.from_mp3(BytesIO(chunk_bytes))
        combined += segment

    buffer = BytesIO()
    combined.export(buffer, format="mp3")
    return buffer.getvalue()


def _measure_duration(audio_bytes: bytes) -> tuple[float, int]:
    """Measure duration and sample rate of MP3 audio bytes."""
    from pydub import AudioSegment

    segment = AudioSegment.from_mp3(BytesIO(audio_bytes))
    duration_seconds = len(segment) / 1000.0
    sample_rate = segment.frame_rate
    return duration_seconds, sample_rate


def _compute_cost(char_count: int) -> float:
    """Compute cost based on character count (ElevenLabs Starter pricing)."""
    return char_count / 1000 * ELEVENLABS_COST_PER_1K_CHARS
