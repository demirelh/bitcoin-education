from btcedu.core.tts import _synthesize_clean_take
from btcedu.services.elevenlabs_service import TTSRequest, TTSResponse


def test_identical_lines_are_synthesized_again(monkeypatch, tmp_path):
    import btcedu.core.tts as tts_module

    monkeypatch.setattr(tts_module, "_normalize_loudness", lambda _path: None)
    monkeypatch.setattr(tts_module, "_noise_floor_db", lambda _path: -70.0)
    monkeypatch.setattr(tts_module, "_take_runs_off", lambda *_args: False)
    monkeypatch.setattr(tts_module, "_take_stutters", lambda *_args: False)

    class CountingService:
        def __init__(self):
            self.calls = 0

        def synthesize(self, request):
            self.calls += 1
            return TTSResponse(
                audio_bytes=f"take-{self.calls}".encode(),
                duration_seconds=2.0,
                sample_rate=44100,
                model=request.model,
                voice_id=request.voice_id,
                character_count=len(request.text),
                cost_usd=0.01,
            )

    service = CountingService()
    request = TTSRequest(
        text="İyi akşamlar.",
        voice_id="anchor",
        model="eleven_multilingual_v2",
    )

    for name in ("first.mp3", "second.mp3"):
        _synthesize_clean_take(
            service,
            request,
            tmp_path / name,
            max_attempts=1,
            noise_floor_max_db=-60.0,
            label=name,
        )

    assert service.calls == 2
    assert (tmp_path / "first.mp3").read_bytes() == b"take-1"
    assert (tmp_path / "second.mp3").read_bytes() == b"take-2"
