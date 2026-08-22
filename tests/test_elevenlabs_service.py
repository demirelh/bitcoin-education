"""Tests for ElevenLabs TTS service."""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.elevenlabs_service import (
    ELEVENLABS_COST_PER_1K_CHARS,
    MAX_CHARS_PER_REQUEST,
    ElevenLabsAPIError,
    ElevenLabsService,
    TTSRequest,
    TTSResponse,
    _chunk_text,
    _compute_cost,
    _decode_audio_response,
    _words_from_alignment,
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


def test_later_chunk_failure_reports_already_incurred_cost():
    service = ElevenLabsService(api_key="key", default_voice_id="voice")
    text = ("Ein vollständiger Satz. " * 400).strip()
    chunks = _chunk_text(text, MAX_CHARS_PER_REQUEST)
    assert len(chunks) > 1
    service._call_with_retry = MagicMock(
        side_effect=[(b"first chunk audio", None), RuntimeError("second chunk failed")]
    )

    with pytest.raises(RuntimeError) as exc_info:
        service.synthesize(TTSRequest(text=text, voice_id=None))

    assert exc_info.value.cost_usd == pytest.approx(_compute_cost(len(chunks[0])))


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


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_successful_call_reports_billed_characters(mock_post, mock_measure):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"audio"
    mock_post.return_value = mock_response
    mock_measure.return_value = (1.0, 44100)
    billed = []
    service = ElevenLabsService(
        api_key="key",
        default_voice_id="voice",
        after_api_call=billed.append,
    )

    service.synthesize(TTSRequest(text="Merhaba", voice_id="voice"))

    assert billed == [7]


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_synthesize_sends_speed_in_voice_settings(mock_post, mock_measure):
    """The speed voice setting is forwarded to the ElevenLabs payload."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"audio"
    mock_post.return_value = mock_response
    mock_measure.return_value = (5.0, 44100)

    service = ElevenLabsService(api_key="k", default_voice_id="v")
    req = TTSRequest(text="Merhaba", voice_id="v", speed=1.08, style=0.35, stability=0.45)
    service.synthesize(req)

    payload = mock_post.call_args[1]["json"]
    vs = payload["voice_settings"]
    assert vs["speed"] == pytest.approx(1.08)
    assert vs["style"] == pytest.approx(0.35)
    assert vs["stability"] == pytest.approx(0.45)


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


@patch("btcedu.services.elevenlabs_service.requests.post")
def test_synthesize_quota_error_exposes_provider_code(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = (
        '{"detail":{"type":"invalid_request","code":"quota_exceeded",'
        '"message":"Not enough credits"}}'
    )
    mock_response.json.return_value = {
        "detail": {
            "type": "invalid_request",
            "code": "quota_exceeded",
            "message": "Not enough credits",
        }
    }
    mock_post.return_value = mock_response

    service = ElevenLabsService(api_key="key", default_voice_id="v1")

    with pytest.raises(ElevenLabsAPIError) as exc_info:
        service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_code == "quota_exceeded"
    assert mock_post.call_count == 1


def test_synthesize_no_voice_id():
    """Missing voice_id raises ValueError."""
    service = ElevenLabsService(api_key="key")
    req = TTSRequest(text="Test", voice_id="")

    with pytest.raises(ValueError, match="No voice_id"):
        service.synthesize(req)


# ---------------------------------------------------------------------------
# Reserve accounts: a spent monthly plan must not end the broadcast
# ---------------------------------------------------------------------------


def _quota_exhausted_response(field: str = "code"):
    """The real 401 body ElevenLabs returns when the plan is spent."""
    detail = {
        "type": "invalid_request",
        field: "quota_exceeded",
        "message": (
            "This request exceeds your quota of 100360. You have 17 credits "
            "remaining, while 77 credits are required for this request."
        ),
    }
    response = MagicMock()
    response.status_code = 401
    response.text = json.dumps({"detail": detail})
    response.json.return_value = {"detail": detail}
    return response


def _rejected_key_response():
    """A revoked or mistyped key — same status, different cause."""
    detail = {"status": "invalid_api_key", "message": "Invalid API key"}
    response = MagicMock()
    response.status_code = 401
    response.text = json.dumps({"detail": detail})
    response.json.return_value = {"detail": detail}
    return response


def _ok_response(content: bytes = b"audio"):
    response = MagicMock()
    response.status_code = 200
    response.content = content
    return response


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_exhausted_quota_continues_on_the_reserve_account(mock_post, mock_measure):
    mock_post.side_effect = [_quota_exhausted_response(), _ok_response(b"reserve_audio")]
    mock_measure.return_value = (5.0, 44100)

    service = ElevenLabsService(
        api_key="spent", default_voice_id="v1", fallback_api_keys=["reserve"]
    )
    result = service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert result.audio_bytes == b"reserve_audio"
    assert mock_post.call_count == 2
    # The retry must actually carry the second account, not the spent one.
    assert mock_post.call_args_list[0].kwargs["headers"]["xi-api-key"] == "spent"
    assert mock_post.call_args_list[1].kwargs["headers"]["xi-api-key"] == "reserve"
    assert service.api_key == "reserve"


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_quota_reported_only_in_the_status_field_still_switches(mock_post, mock_measure):
    """Some endpoints fill ``status`` instead of ``code``."""
    mock_post.side_effect = [_quota_exhausted_response(field="status"), _ok_response()]
    mock_measure.return_value = (5.0, 44100)

    service = ElevenLabsService(
        api_key="spent", default_voice_id="v1", fallback_api_keys=["reserve"]
    )
    service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert service.api_key == "reserve"


@patch("btcedu.services.elevenlabs_service.requests.post")
def test_a_rejected_key_never_spends_the_reserve(mock_post):
    """A bad key is a misconfiguration; draining the spare would mask it."""
    mock_post.return_value = _rejected_key_response()

    service = ElevenLabsService(
        api_key="broken", default_voice_id="v1", fallback_api_keys=["reserve"]
    )

    with pytest.raises(ElevenLabsAPIError):
        service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert mock_post.call_count == 1
    assert service.api_key == "broken"
    assert service.fallback_api_keys == ["reserve"]


@patch("btcedu.services.elevenlabs_service.requests.post")
def test_quota_without_a_reserve_still_fails(mock_post):
    mock_post.return_value = _quota_exhausted_response()

    service = ElevenLabsService(api_key="spent", default_voice_id="v1")

    with pytest.raises(ElevenLabsAPIError) as exc_info:
        service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert exc_info.value.error_code == "quota_exceeded"
    assert mock_post.call_count == 1


@patch("btcedu.services.elevenlabs_service.requests.post")
def test_every_account_exhausted_raises_after_trying_all(mock_post):
    mock_post.side_effect = [
        _quota_exhausted_response(),
        _quota_exhausted_response(),
        _quota_exhausted_response(),
    ]

    service = ElevenLabsService(
        api_key="one", default_voice_id="v1", fallback_api_keys=["two", "three"]
    )

    with pytest.raises(ElevenLabsAPIError):
        service.synthesize(TTSRequest(text="Test", voice_id="v1"))

    assert mock_post.call_count == 3
    assert service.fallback_api_keys == []


@patch("btcedu.services.elevenlabs_service._measure_duration")
@patch("btcedu.services.elevenlabs_service.requests.post")
def test_the_switch_holds_for_the_rest_of_the_episode(mock_post, mock_measure):
    """Going back to the spent account for each chunk would waste a call."""
    mock_post.side_effect = [
        _quota_exhausted_response(),
        _ok_response(b"a"),
        _ok_response(b"b"),
    ]
    mock_measure.return_value = (5.0, 44100)

    service = ElevenLabsService(
        api_key="spent", default_voice_id="v1", fallback_api_keys=["reserve"]
    )
    with patch("btcedu.services.elevenlabs_service._concatenate_audio", return_value=b"ab"):
        service.synthesize(TTSRequest(text="x" * (MAX_CHARS_PER_REQUEST + 10), voice_id="v1"))

    used = [c.kwargs["headers"]["xi-api-key"] for c in mock_post.call_args_list]
    assert used == ["spent", "reserve", "reserve"]


def test_a_blank_or_duplicate_reserve_is_ignored():
    """Retrying the very same spent key would only waste a call."""
    service = ElevenLabsService(api_key="one", fallback_api_keys=["", "  ", "one", " two "])
    assert service.fallback_api_keys == ["two"]


# ---------------------------------------------------------------------------
# Word timings for subtitles
# ---------------------------------------------------------------------------


def _alignment(characters, starts, ends):
    return {
        "characters": characters,
        "character_start_times_seconds": starts,
        "character_end_times_seconds": ends,
    }


def test_words_from_alignment_collapses_characters_into_words():
    words = _words_from_alignment(
        _alignment(
            list("ab cd"),
            [0.0, 0.1, 0.2, 0.3, 0.4],
            [0.1, 0.2, 0.3, 0.4, 0.5],
        )
    )
    assert [(w.word, w.start, w.end) for w in words] == [
        ("ab", 0.0, 0.2),
        ("cd", 0.3, 0.5),
    ]


def test_words_from_alignment_rejects_mismatched_lengths():
    assert _words_from_alignment(_alignment(list("ab"), [0.0], [0.1, 0.2])) is None


def test_words_from_alignment_rejects_non_numeric_times():
    assert _words_from_alignment(_alignment(list("ab"), [0.0, "x"], [0.1, 0.2])) is None


def test_words_from_alignment_rejects_a_non_dict():
    assert _words_from_alignment(["a"]) is None


def test_words_from_alignment_of_whitespace_only_is_none():
    assert _words_from_alignment(_alignment([" "], [0.0], [0.1])) is None


def test_decode_audio_response_reads_base64_json():
    import base64

    response = MagicMock()
    response.json.return_value = {
        "audio_base64": base64.b64encode(b"audio").decode(),
        "alignment": _alignment(list("ab"), [0.0, 0.1], [0.1, 0.2]),
    }
    audio, timings = _decode_audio_response(response)
    assert audio == b"audio"
    assert [w.word for w in timings] == ["ab"]


def test_decode_audio_response_falls_back_to_raw_bytes():
    """A plain MP3 body has no JSON at all - the audio must still come out."""
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    response.content = b"mp3-bytes"
    assert _decode_audio_response(response) == (b"mp3-bytes", None)


def test_decode_audio_response_ignores_json_without_audio():
    response = MagicMock()
    response.json.return_value = {"detail": "something else"}
    response.content = b"mp3-bytes"
    assert _decode_audio_response(response) == (b"mp3-bytes", None)


def test_disable_timestamps_only_for_endpoint_refusals():
    service = ElevenLabsService(api_key="k", default_voice_id="v")
    assert service._disable_timestamps(ElevenLabsAPIError(422, "nope")) is True
    assert service.request_timestamps is False


def test_disable_timestamps_not_for_quota_or_rate_limit():
    """A spent plan or a rate limit must stay a loud failure, not silently
    cost us the subtitles for every future run."""
    service = ElevenLabsService(api_key="k", default_voice_id="v")
    assert service._disable_timestamps(ElevenLabsAPIError(401, "quota", "quota_exceeded")) is False
    assert service._disable_timestamps(ElevenLabsAPIError(429, "slow down")) is False
    assert service.request_timestamps is True


def test_synthesize_offsets_timings_of_the_second_chunk():
    """Chunk two is spoken after chunk one, so its times must move by the
    measured length of chunk one - otherwise every subtitle after the first
    5000 characters sits on top of the beginning."""
    from btcedu.services.elevenlabs_service import WordTiming

    service = ElevenLabsService(api_key="k", default_voice_id="v")
    chunks = [
        (b"one", [WordTiming("bir", 0.0, 1.0)]),
        (b"two", [WordTiming("iki", 0.0, 1.0)]),
    ]
    with (
        patch.object(service, "_call_with_retry", side_effect=chunks),
        patch(
            "btcedu.services.elevenlabs_service._chunk_text",
            return_value=["a", "b"],
        ),
        patch("btcedu.services.elevenlabs_service._concatenate_audio", return_value=b"onetwo"),
        patch(
            "btcedu.services.elevenlabs_service._measure_duration",
            return_value=(4.0, 44100),
        ),
    ):
        long_text = "x" * (MAX_CHARS_PER_REQUEST + 1)
        result = service.synthesize(TTSRequest(text=long_text, voice_id="v"))

    assert [(w.word, w.start) for w in result.word_timings] == [("bir", 0.0), ("iki", 4.0)]


def test_synthesize_drops_all_timings_when_one_chunk_has_none():
    """Half a subtitle track is worse than none: the missing stretch would
    silently shift everything after it."""
    from btcedu.services.elevenlabs_service import WordTiming

    service = ElevenLabsService(api_key="k", default_voice_id="v")
    chunks = [(b"one", [WordTiming("bir", 0.0, 1.0)]), (b"two", None)]
    with (
        patch.object(service, "_call_with_retry", side_effect=chunks),
        patch(
            "btcedu.services.elevenlabs_service._chunk_text",
            return_value=["a", "b"],
        ),
        patch("btcedu.services.elevenlabs_service._concatenate_audio", return_value=b"onetwo"),
        patch(
            "btcedu.services.elevenlabs_service._measure_duration",
            return_value=(4.0, 44100),
        ),
    ):
        long_text = "x" * (MAX_CHARS_PER_REQUEST + 1)
        result = service.synthesize(TTSRequest(text=long_text, voice_id="v"))

    assert result.word_timings is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
