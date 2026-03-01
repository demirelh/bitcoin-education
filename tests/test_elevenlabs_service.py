"""Tests for ElevenLabs TTS service."""

from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.elevenlabs_service import (
    ELEVENLABS_COST_PER_1K_CHARS,
    MAX_CHARS_PER_REQUEST,
    ElevenLabsService,
    TTSRequest,
    TTSResponse,
    _chunk_text,
    _compute_cost,
)

# ---------------------------------------------------------------------------
# TTSRequest defaults
# ---------------------------------------------------------------------------


def test_tts_request_defaults():
    """TTSRequest has sensible defaults."""
    req = TTSRequest(text="Hello", voice_id="v1")
    assert req.text == "Hello"
    assert req.voice_id == "v1"
    assert req.model == "eleven_multilingual_v2"
    assert req.stability == 0.5
    assert req.similarity_boost == 0.75
    assert req.style == 0.0
    assert req.use_speaker_boost is True


# ---------------------------------------------------------------------------
# _compute_cost
# ---------------------------------------------------------------------------


def test_compute_cost_basic():
    """Cost = chars / 1000 * rate."""
    assert _compute_cost(1000) == pytest.approx(ELEVENLABS_COST_PER_1K_CHARS)
    assert _compute_cost(500) == pytest.approx(ELEVENLABS_COST_PER_1K_CHARS / 2)
    assert _compute_cost(0) == 0.0


def test_compute_cost_large():
    """Cost scales linearly."""
    assert _compute_cost(10000) == pytest.approx(ELEVENLABS_COST_PER_1K_CHARS * 10)


# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_under_limit():
    """Text under limit returns single chunk."""
    text = "Short text."
    result = _chunk_text(text, 100)
    assert result == [text]


def test_chunk_text_over_limit_sentence_boundary():
    """Long text splits at sentence boundary."""
    text = "First sentence. Second sentence. Third sentence."
    chunks = _chunk_text(text, 30)
    assert len(chunks) >= 2
    # All chunks join back to the original
    assert "".join(chunks) == text
    # No chunk exceeds the limit
    for chunk in chunks:
        assert len(chunk) <= 30


def test_chunk_text_no_sentence_boundary():
    """Falls back to space splitting when no sentence boundary."""
    text = "word " * 20  # 100 chars of "word word word..."
    chunks = _chunk_text(text.strip(), 30)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 30


def test_chunk_text_exact_limit():
    """Text exactly at limit returns single chunk."""
    text = "a" * MAX_CHARS_PER_REQUEST
    result = _chunk_text(text, MAX_CHARS_PER_REQUEST)
    assert result == [text]


# ---------------------------------------------------------------------------
# _measure_duration (mocked pydub)
# ---------------------------------------------------------------------------


def test_measure_duration_mocked():
    """Duration measurement with mocked pydub."""
    import sys
    import types

    # Create mock pydub module to avoid import issues on Python 3.13
    mock_pydub = types.ModuleType("pydub")
    mock_audio_segment_cls = MagicMock()
    mock_pydub.AudioSegment = mock_audio_segment_cls

    mock_segment = MagicMock()
    mock_segment.__len__ = MagicMock(return_value=5000)  # 5 seconds in ms
    mock_segment.frame_rate = 44100
    mock_audio_segment_cls.from_mp3.return_value = mock_segment

    with patch.dict(sys.modules, {"pydub": mock_pydub}):
        from btcedu.services.elevenlabs_service import _measure_duration

        duration, sample_rate = _measure_duration(b"fake_mp3_data")

    assert duration == pytest.approx(5.0)
    assert sample_rate == 44100


# ---------------------------------------------------------------------------
# _concatenate_audio (mocked pydub)
# ---------------------------------------------------------------------------


def test_concatenate_audio_single_chunk():
    """Single chunk concatenation."""
    import sys
    import types

    mock_pydub = types.ModuleType("pydub")
    mock_audio_segment_cls = MagicMock()
    mock_pydub.AudioSegment = mock_audio_segment_cls

    mock_empty = MagicMock()
    mock_segment = MagicMock()
    mock_audio_segment_cls.empty.return_value = mock_empty
    mock_audio_segment_cls.from_mp3.return_value = mock_segment

    mock_combined = MagicMock()
    mock_empty.__iadd__ = MagicMock(return_value=mock_combined)

    mock_buffer_content = b"combined_mp3"
    mock_combined.export = MagicMock(side_effect=lambda buf, format: buf.write(mock_buffer_content))

    with patch.dict(sys.modules, {"pydub": mock_pydub}):
        from btcedu.services.elevenlabs_service import _concatenate_audio

        result = _concatenate_audio([b"chunk1"])
    assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# synthesize (mocked HTTP)
# ---------------------------------------------------------------------------


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_synthesize_success(mock_post, mock_measure):
    """Successful synthesis with mocked HTTP."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"fake_mp3_audio_data"
    mock_post.return_value = mock_response

    mock_measure.return_value = (10.5, 44100)

    service = ElevenLabsService(api_key="test_key", default_voice_id="voice1")
    req = TTSRequest(text="Merhaba dünya", voice_id="voice1")

    result = service.synthesize(req)

    assert isinstance(result, TTSResponse)
    assert result.audio_bytes == b"fake_mp3_audio_data"
    assert result.duration_seconds == pytest.approx(10.5)
    assert result.sample_rate == 44100
    assert result.voice_id == "voice1"
    assert result.character_count == len("Merhaba dünya")
    assert result.cost_usd == pytest.approx(_compute_cost(len("Merhaba dünya")))

    # Verify API was called
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "voice1" in call_args[0][0]  # URL contains voice_id
    assert call_args[1]["headers"]["xi-api-key"] == "test_key"


@patch("btcedu.services.elevenlabs_service.time.sleep")
@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_synthesize_rate_limit_retry(mock_post, mock_measure, mock_sleep):
    """Rate limit triggers retry with backoff."""
    rate_limited = MagicMock()
    rate_limited.status_code = 429

    success = MagicMock()
    success.status_code = 200
    success.content = b"audio_after_retry"

    mock_post.side_effect = [rate_limited, success]
    mock_measure.return_value = (5.0, 44100)

    service = ElevenLabsService(api_key="key", default_voice_id="v1")
    req = TTSRequest(text="Test", voice_id="v1")

    result = service.synthesize(req)

    assert result.audio_bytes == b"audio_after_retry"
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once_with(1)  # 2^0 = 1


@patch("btcedu.services.elevenlabs_service.requests.post")
def test_synthesize_api_error(mock_post):
    """Non-200 non-429 raises RuntimeError."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal server error"
    mock_post.return_value = mock_response

    service = ElevenLabsService(api_key="key", default_voice_id="v1")
    req = TTSRequest(text="Test", voice_id="v1")

    with pytest.raises(RuntimeError, match="ElevenLabs API error 500"):
        service.synthesize(req)


def test_synthesize_no_voice_id():
    """Missing voice_id raises ValueError."""
    service = ElevenLabsService(api_key="key")
    req = TTSRequest(text="Test", voice_id="")

    with pytest.raises(ValueError, match="No voice_id"):
        service.synthesize(req)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
