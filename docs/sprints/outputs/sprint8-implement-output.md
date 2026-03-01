# Sprint 8 — Implementation Output (TTS Stage)

**Sprint Number:** 8
**Phase:** 4 (Audio + Video Production), Part 1
**Status:** Implemented
**Date:** 2026-03-01

---

## 1. Implementation Summary

Sprint 8 implements the **TTS** (Text-to-Speech) pipeline stage, converting each chapter's narration text into per-chapter MP3 audio files via the ElevenLabs REST API.

**Core Functionality:**
1. **ElevenLabs service**: Raw HTTP API integration with retry, text chunking at sentence boundaries, audio concatenation via pydub
2. **TTS orchestration**: Full idempotency via narration hash, partial recovery (skip unchanged chapters), per-episode cost guard, dry-run mode
3. **Pipeline integration**: Runs after `IMAGES_GENERATED`, sets status to `TTS_DONE`
4. **Dashboard**: TTS Audio tab with per-chapter `<audio>` players, duration/character badges
5. **Provenance**: Full traceability with content hashes, costs, and segment metadata

---

## 2. Files Created

| File | Purpose |
|------|---------|
| `btcedu/services/elevenlabs_service.py` | ElevenLabs TTS service (TTSRequest, TTSResponse, ElevenLabsService, text chunking, audio concatenation) |
| `btcedu/core/tts.py` | TTS orchestration (generate_tts, AudioEntry, TTSResult, idempotency, partial recovery, cost guard) |
| `tests/test_elevenlabs_service.py` | 13 service tests |
| `tests/test_tts.py` | 22 core orchestration tests |

## 3. Files Modified

| File | Changes |
|------|---------|
| `btcedu/config.py` | Added 7 `elevenlabs_*` config fields (api_key, voice_id, model, stability, similarity_boost, style, use_speaker_boost) |
| `btcedu/core/pipeline.py` | Added `("tts", IMAGES_GENERATED)` to `_V2_STAGES`; TTS branch in `_run_stage()`; expanded status filters in `run_pending()`/`run_latest()` to include TRANSLATED, ADAPTED, CHAPTERIZED, IMAGES_GENERATED; expanded cost extraction to include all v2 stages |
| `btcedu/cli.py` | Added `btcedu tts` command with `--episode-id`, `--force`, `--dry-run`, `--chapter` options |
| `btcedu/web/jobs.py` | Added `_do_tts()` job action handler |
| `btcedu/web/api.py` | Added 3 TTS endpoints: GET manifest, GET MP3, POST trigger |
| `btcedu/web/static/app.js` | Added TTS Audio tab, `loadTTSPanel()` function, `actions.tts()` handler |
| `btcedu/web/static/styles.css` | Added TTS panel styles (`.tts-panel`, `.tts-chapter-row`, `.tts-duration`, `.tts-chars`, `.tts-player`) |

---

## 4. Key Implementation Details

### 4.1 ElevenLabs Service (`elevenlabs_service.py`)

- **Raw HTTP** via `requests.post()` to `POST /v1/text-to-speech/{voice_id}` — no `elevenlabs` SDK dependency
- **Retry**: Exponential backoff (1s, 2s, 4s) on HTTP 429 rate limit, max 3 retries
- **Text chunking**: Splits at sentence boundaries (`. `, `! `, `? `) when text exceeds 5000 chars; falls back to word boundaries
- **Audio concatenation**: Multi-chunk audio joined via `pydub.AudioSegment`
- **Duration measurement**: Via `pydub.AudioSegment.from_mp3()` for accurate MP3 duration
- **Cost computation**: `chars / 1000 * $0.30` (ElevenLabs Starter pricing)

### 4.2 TTS Orchestration (`tts.py`)

- **Idempotency**: SHA-256 hash of all `{chapter_id, narration_text}` pairs; skips if manifest + provenance exist with matching hash and all MP3 files present
- **Partial recovery**: On re-run, chapters with matching `text_hash` and existing MP3 are skipped; only changed chapters are regenerated
- **Cost guard**: Checks cumulative episode cost from PipelineRun records before each chapter; raises RuntimeError if `max_episode_cost_usd` exceeded
- **Dry-run**: Writes minimal silent MP3 placeholders (single MPEG frame) instead of calling API; cost = $0
- **Downstream invalidation**: Writes `.stale` marker on `render/draft.mp4` if it exists
- **Database records**: Creates `MediaAsset` (type=AUDIO) and `ContentArtifact` (type=tts_audio, model=elevenlabs) records

### 4.3 Pipeline Integration

```
_V2_STAGES = [
    ...
    ("imagegen", EpisodeStatus.CHAPTERIZED),   # Sprint 7
    ("tts",      EpisodeStatus.IMAGES_GENERATED),  # Sprint 8
]
```

- `run_pending()` and `run_latest()` now pick up episodes at TRANSLATED, ADAPTED, CHAPTERIZED, and IMAGES_GENERATED statuses
- Cost extraction in `run_episode_pipeline()` expanded to include translate, adapt, chapterize, imagegen, and tts stages

### 4.4 TTS Manifest Schema

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
      "text_hash": "sha256:abc123...",
      "duration_seconds": 95.4,
      "file_path": "tts/ch01.mp3",
      "sample_rate": 44100,
      "size_bytes": 1523600,
      "cost_usd": 0.375,
      "metadata": {"generated_at": "..."}
    }
  ]
}
```

### 4.5 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/episodes/<id>/tts` | Return TTS manifest JSON |
| GET | `/api/episodes/<id>/tts/<chapter>.mp3` | Serve per-chapter MP3 via `send_file` |
| POST | `/api/episodes/<id>/tts` | Trigger TTS job via JobManager |

### 4.6 CLI Command

```
btcedu tts --episode-id EP [--force] [--dry-run] [--chapter CH]
```

- `--force`: Regenerate all audio even if current
- `--dry-run`: Write silent MP3 placeholders, skip API calls
- `--chapter CH`: Regenerate only the specified chapter

---

## 5. Test Results

```
476 passed, 33 warnings in 45.71s
```

### New Tests: 35

**Service tests (`test_elevenlabs_service.py`): 13**
- TTSRequest defaults
- Cost computation (basic, large)
- Text chunking (under limit, over limit with sentence boundary, no sentence boundary, exact limit)
- Duration measurement (mocked pydub)
- Audio concatenation (mocked pydub)
- Synthesis success (mocked HTTP)
- Synthesis rate limit retry
- Synthesis API error
- Missing voice_id validation

**Core tests (`test_tts.py`): 22**
- Narration hash stability and change detection
- Chapters narration hash: stable, changes on narration change, ignores visual changes
- `_is_tts_current`: missing manifest, stale marker, hash mismatch, missing MP3, all good
- `_mark_downstream_stale`: no render file, with render file
- Silent MP3 creation
- `generate_tts`: episode not found, v1 rejected, wrong status
- `generate_tts`: dry-run (manifest + silent placeholders, no API calls)
- `generate_tts`: happy path (manifest + provenance + MediaAsset + status update)
- `generate_tts`: idempotency (second run skipped)
- `generate_tts`: single chapter regeneration
- `generate_tts`: cost limit enforcement
- Pipeline `_V2_STAGES` includes TTS entry

---

## 6. Definition of Done Checklist

- [x] All existing tests pass (441 existing)
- [x] 35 new tests pass (13 service + 22 core)
- [x] `btcedu tts --episode-id EP --dry-run` produces manifest + silent MP3 placeholders
- [x] `_V2_STAGES` includes `("tts", EpisodeStatus.IMAGES_GENERATED)`
- [x] `btcedu run --episode-id EP` (v2) advances through TTS automatically
- [x] `media_assets` table gets rows with `asset_type="audio"`, `duration_seconds` populated
- [x] Manifest + provenance JSON files written correctly
- [x] Partial recovery: unchanged chapters skipped on re-run
- [x] Dashboard TTS tab shows audio players per chapter
- [x] `GET/POST /api/episodes/{id}/tts` endpoints work
- [x] `GET /api/episodes/{id}/tts/{chapter}.mp3` serves audio files
- [x] `run_pending()` and `run_latest()` pick up `IMAGES_GENERATED` episodes

---

## 7. Configuration

Add to `.env`:
```env
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=your_voice_id_here
ELEVENLABS_MODEL=eleven_multilingual_v2
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.0
ELEVENLABS_USE_SPEAKER_BOOST=true
```

---

## 8. Known Limitations

1. **pydub dependency**: Requires `ffmpeg` installed on the system for MP3 handling. On Raspberry Pi: `sudo apt install ffmpeg`
2. **Python 3.13**: `pydub` has an `audioop` import issue on Python 3.13 (module removed). Tests work around this with mock injection. Production may need `pyaudioop` package or pydub update.
3. **No parallel processing**: Chapters are processed sequentially. For long episodes with many chapters, this could be slow.
4. **MediaAsset model**: Uses its own `declarative_base()`, not `btcedu.db.Base`. Tests must create `prompt_versions` table and call `MediaBase.metadata.create_all()` separately.
