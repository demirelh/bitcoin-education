"""ElevenLabs TTS service abstraction."""

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

# ElevenLabs Starter pricing (per 1000 characters)
ELEVENLABS_COST_PER_1K_CHARS = 0.30


class ElevenLabsAPIError(RuntimeError):
    """Structured ElevenLabs API failure for reliable retry classification."""

    def __init__(self, status_code: int, detail: str, error_code: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        code_suffix = f" ({error_code})" if error_code else ""
        super().__init__(f"ElevenLabs API error {status_code}{code_suffix}: {detail}")


# Maximum characters per API request
MAX_CHARS_PER_REQUEST = 5000

# A spent monthly plan comes back as HTTP 401 — the same status as a bad key —
# so the body is what tells the two apart. ElevenLabs fills ``code`` and
# ``status`` with ``quota_exceeded``; the message is matched as well for the
# endpoints that only phrase it in prose.
_QUOTA_EXHAUSTED = re.compile(
    r"quota[_\s-]?exceeded|not enough credits|insufficient credits|"
    r"credits remaining",
    re.IGNORECASE,
)


def _is_quota_exhausted(error: ElevenLabsAPIError) -> bool:
    """Whether the account behind the request has no credits left."""
    if str(getattr(error, "error_code", "") or "").lower() == "quota_exceeded":
        return True
    return bool(_QUOTA_EXHAUSTED.search(str(getattr(error, "detail", "") or "")))


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
    speed: float = 1.0


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
        before_api_call: Callable[[int, int], None] | None = None,
        fallback_api_keys: Sequence[str] = (),
    ):
        self.api_key = api_key
        self.default_voice_id = default_voice_id
        self.default_model = default_model
        self.before_api_call = before_api_call
        # Reserve accounts, tried in order and only after the one in use has
        # reported its quota spent.
        self.fallback_api_keys = [
            key.strip()
            for key in fallback_api_keys
            if key and key.strip() and key.strip() != api_key
        ]

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
            "speed": request.speed,
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
        sent_chars = 0
        for i, chunk in enumerate(chunks):
            logger.info("Synthesizing chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
            try:
                audio_data = self._call_with_retry(
                    chunk,
                    voice_id,
                    model,
                    voice_settings,
                    sent_chars=sent_chars,
                )
            except Exception as exc:
                incurred = _compute_cost(sent_chars)
                try:
                    exc.cost_usd = max(float(getattr(exc, "cost_usd", 0.0)), incurred)
                except (TypeError, ValueError):
                    exc.cost_usd = incurred
                raise
            audio_parts.append(audio_data)
            sent_chars += len(chunk)

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

    def _switch_to_reserve_account(self, error: ElevenLabsAPIError) -> bool:
        """Move to the next configured account after a quota rejection.

        Deliberately narrow. Only an exhausted plan justifies spending a
        reserve; a rejected or revoked key must stay a loud failure, because
        silently draining the spare account would hide the misconfiguration
        until there is nothing left to fall back on.

        A rejected request is not billed, so nothing is lost by re-sending the
        same chunk on the new account.
        """
        if not self.fallback_api_keys:
            return False
        if not _is_quota_exhausted(error):
            return False

        self.api_key = self.fallback_api_keys.pop(0)
        logger.warning(
            "ElevenLabs quota exhausted (%s); switching to the next configured "
            "account, %d reserve(s) left after this one",
            error.detail[:120],
            len(self.fallback_api_keys),
        )
        return True

    def _call_with_retry(
        self,
        text: str,
        voice_id: str,
        model: str,
        voice_settings: dict,
        max_retries: int = 3,
        sent_chars: int = 0,
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
                if self.before_api_call is not None:
                    self.before_api_call(sent_chars, len(text))
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
                        raise ElevenLabsAPIError(
                            429,
                            f"rate limit exceeded after {max_retries} retries",
                            "rate_limit_exceeded",
                        )

                if response.status_code != 200:
                    error_detail = response.text[:200]
                    error_code = None
                    try:
                        body = response.json()
                        detail = body.get("detail") if isinstance(body, dict) else None
                        if isinstance(detail, dict):
                            # ElevenLabs reports quota exhaustion in both
                            # fields; reading only one has already been enough
                            # to misclassify a spent plan as a bad key.
                            error_code = detail.get("code") or detail.get("status")
                    except (TypeError, ValueError):
                        pass
                    api_error = ElevenLabsAPIError(
                        response.status_code,
                        error_detail,
                        error_code,
                    )
                    if self._switch_to_reserve_account(api_error):
                        # Same chunk, fresh account. Not counted as a retry:
                        # the first account will never answer differently.
                        return self._call_with_retry(
                            text,
                            voice_id,
                            model,
                            voice_settings,
                            max_retries=max_retries,
                            sent_chars=sent_chars,
                        )
                    raise api_error

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
