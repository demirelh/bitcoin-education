# Sprint 8 — Implementation Plan (TTS Stage)

**Sprint Number:** 8
**Phase:** 4 (Audio + Video Production), Part 1
**Status:** Planning
**Dependencies:** Sprint 7 (Image Generation) complete
**Created:** 2026-03-01

---

## 1. Sprint Scope Summary

**In Scope:**

Sprint 8 implements the **TTS** (Text-to-Speech) stage, which converts each chapter's narration text (from `chapters.json`) into per-chapter MP3 audio files via the ElevenLabs API. This is the stage between `IMAGES_GENERATED` and `RENDER` in the v2 pipeline.

This sprint delivers:
1. The **ElevenLabs TTS service abstraction** (`TTSService` protocol) with `ElevenLabsService` as the initial provider, using raw HTTP via `requests` (no SDK dependency)
2. The **TTS orchestration module** (`btcedu/core/tts.py`) with full idempotency, provenance, partial recovery, and cost guard support
3. A **TTS manifest** (`tts/manifest.json`) per episode tracking all generated audio segments
4. A **CLI command** (`btcedu tts --episode-id EP`) with `--force`, `--dry-run`, and `--chapter` options
5. **Pipeline integration** after `IMAGES_GENERATED`, before `RENDER`
6. A **dashboard TTS Audio tab** with per-chapter `<audio>` players
7. **3 new API endpoints** for TTS manifest, audio serving, and job triggering
8. **35 tests** covering service layer and core orchestration

**Not in Scope:**
- Video rendering or combining audio with images (Sprint 9-10)
- Multiple TTS providers (only ElevenLabs; Protocol allows future additions)
- Parallel/concurrent chapter processing
- Voice selection UI in dashboard
- Turkish pronunciation dictionary or SSML phoneme hints
- Audio normalization or post-processing
- Streaming audio delivery

---

## 2. Architecture Overview

### Pipeline Flow (v2)

```
DETECT → DOWNLOAD → TRANSCRIBE → CORRECT → [review_gate_1] →
TRANSLATE → ADAPT → [review_gate_2] → CHAPTERIZE →
IMAGEGEN → TTS (Sprint 8) → RENDER → REVIEW → PUBLISH
```

### Key Design Decisions

1. **Raw HTTP via `requests`** for ElevenLabs API — `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` with `xi-api-key` header. Keeps dependencies light.
2. **MP3 format, 44100 Hz** — ElevenLabs default. Duration measured from actual file via pydub.
3. **5000 char limit** per API request — long narrations split at sentence boundaries. Typical chapter narration is 800-2000 chars, so chunking is a safety net.
4. **TTS does NOT depend on images** — only on `narration.text`. Regenerating images does NOT invalidate TTS. Both TTS and images independently invalidate RENDER.
5. **`ContentArtifact.model`** field set to `"elevenlabs"` (no LLM used in TTS stage).
6. **Status check**: Episodes must be at `IMAGES_GENERATED` or `TTS_DONE` (for re-runs). `CHAPTERIZED` episodes are rejected — they must complete imagegen first.

---

## 3. New Files

### 3.1 `btcedu/services/elevenlabs_service.py`
- `TTSRequest` dataclass: text, voice_id, model, stability, similarity_boost, style, use_speaker_boost
- `TTSResponse` dataclass: audio_bytes, duration_seconds, sample_rate, model, voice_id, character_count, cost_usd
- `TTSService` Protocol with `synthesize()` method
- `ElevenLabsService` class: REST API integration, retry with exponential backoff on 429, text chunking at sentence boundaries (5000 char limit), audio concatenation via pydub

### 3.2 `btcedu/core/tts.py`
- `AudioEntry` dataclass: per-chapter metadata
- `TTSResult` dataclass: episode-level summary
- `generate_tts()`: main orchestration function with idempotency, partial recovery, cost guard, dry-run support
- Private helpers: hash computation, currentness check, single audio generation, media asset records, downstream stale marking

### 3.3 `tests/test_elevenlabs_service.py`
~13 tests covering request defaults, cost computation, text chunking, duration measurement, synthesis with mocked HTTP, rate limit retry, error handling

### 3.4 `tests/test_tts.py`
~22 tests covering hash computation, idempotency, partial recovery, status checks, dry-run, happy path with mocked service, cost limit enforcement, pipeline integration

---

## 4. Modified Files

### 4.1 `btcedu/config.py`
7 new ElevenLabs config fields: `elevenlabs_api_key`, `elevenlabs_voice_id`, `elevenlabs_model`, `elevenlabs_stability`, `elevenlabs_similarity_boost`, `elevenlabs_style`, `elevenlabs_use_speaker_boost`

### 4.2 `btcedu/core/pipeline.py`
- `_V2_STAGES`: Add `("tts", EpisodeStatus.IMAGES_GENERATED)`
- `_run_stage()`: Add TTS branch
- `run_pending()` / `run_latest()`: Add TRANSLATED, ADAPTED, CHAPTERIZED, IMAGES_GENERATED to status filters
- Cost extraction: Add translate, adapt, chapterize, imagegen, tts to success_stages

### 4.3 `btcedu/cli.py`
New `btcedu tts` command with `--episode-id`, `--force`, `--dry-run`, `--chapter` options

### 4.4 `btcedu/web/jobs.py`
New `_do_tts()` job action handler

### 4.5 `btcedu/web/api.py`
3 new endpoints:
- `GET /api/episodes/<id>/tts` — TTS manifest
- `GET /api/episodes/<id>/tts/<chapter>.mp3` — serve MP3
- `POST /api/episodes/<id>/tts` — trigger TTS job

### 4.6 `btcedu/web/static/app.js`
TTS Audio tab with per-chapter audio players, generate button

### 4.7 `btcedu/web/static/styles.css`
TTS panel styles

---

## 5. TTS Manifest Schema

`data/outputs/{ep_id}/tts/manifest.json`:
```json
{
  "episode_id": "ep_xyz",
  "schema_version": "1.0",
  "voice_id": "...",
  "model": "eleven_multilingual_v2",
  "generated_at": "2026-03-01T12:00:00+00:00",
  "total_duration_seconds": 847.3,
  "total_characters": 12450,
  "total_cost_usd": 3.735,
  "segments": [
    {
      "chapter_id": "ch01",
      "chapter_title": "Giris",
      "text_length": 1250,
      "text_hash": "sha256:...",
      "duration_seconds": 95.4,
      "file_path": "tts/ch01.mp3",
      "sample_rate": 44100,
      "size_bytes": 1523600,
      "cost_usd": 0.375
    }
  ]
}
```

---

## 6. Implementation Order

1. `btcedu/config.py` — add 7 ElevenLabs config fields
2. `btcedu/services/elevenlabs_service.py` — service layer
3. `tests/test_elevenlabs_service.py` — service unit tests
4. `btcedu/core/tts.py` — core orchestration module
5. `tests/test_tts.py` — core module tests
6. `btcedu/core/pipeline.py` — wire TTS into pipeline
7. `btcedu/cli.py` — add TTS CLI command
8. `btcedu/web/jobs.py` — add TTS job action
9. `btcedu/web/api.py` — add 3 TTS endpoints
10. `btcedu/web/static/app.js` — TTS tab + audio player
11. `btcedu/web/static/styles.css` — TTS styles

---

## 7. Definition of Done

- [ ] All existing tests pass (441+)
- [ ] 35+ new tests pass (service + core)
- [ ] `btcedu tts --episode-id EP --dry-run` produces manifest + silent MP3 placeholders
- [ ] `_V2_STAGES` includes `("tts", EpisodeStatus.IMAGES_GENERATED)`
- [ ] `btcedu run --episode-id EP` (v2) advances through TTS automatically
- [ ] `media_assets` table gets rows with `asset_type="audio"`, `duration_seconds` populated
- [ ] Manifest + provenance JSON files written correctly
- [ ] Partial recovery: unchanged chapters skipped on re-run
- [ ] Dashboard TTS tab shows audio players per chapter
- [ ] `GET/POST /api/episodes/{id}/tts` endpoints work
- [ ] `GET /api/episodes/{id}/tts/{chapter}.mp3` serves audio files
- [ ] `run_pending()` and `run_latest()` pick up `IMAGES_GENERATED` episodes
