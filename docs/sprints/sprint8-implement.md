# Sprint 8 — Implementation Prompt (TTS Integration)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 8 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–7 completed codebase
> - **Expected output**: All code changes (new files, modified files), ElevenLabs service, TTS module, dashboard audio preview, tests — committed and passing.

---

## Context

You are implementing **Sprint 8 (Phase 4: TTS Integration)** of the btcedu video production pipeline.

Sprints 1–7 are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: corrector module, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: reviewer module, dashboard review queue + diff viewer, approve/reject/request-changes.
- Translation: translator module, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: adapter module, Turkey-context adaptation, ADAPT stage, Review Gate 2.
- Chapterization: chapterizer module, chapter JSON schema (Pydantic), chapters.json, chapter viewer.
- Image Generation: image generator, ImageGenService (DALL-E 3), media_assets table, image manifest, image gallery.

Sprint 8 adds the **TTS** stage — converting chapter narration text to spoken audio using ElevenLabs. This produces per-chapter MP3 audio segments needed for the RENDER stage (Sprint 9-10).

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 8 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/image_generator.py` (stage pattern to follow), `btcedu/services/image_gen_service.py` (service abstraction pattern), `btcedu/core/chapter_schema.py` (Pydantic models), `btcedu/core/pipeline.py`, `btcedu/models/media_asset.py`, `btcedu/config.py`, `btcedu/cli.py`, `btcedu/web/`.

2. **Add ElevenLabs dependency** — add `elevenlabs` (or `httpx`/`requests` for raw API calls) to `pyproject.toml` dependencies. Check existing dependencies to choose the lightest approach.

3. **Extend configuration** — add to `btcedu/config.py`:
   - `ELEVENLABS_API_KEY: str = ""` (required for TTS)
   - `ELEVENLABS_VOICE_ID: str = ""` (required — specific Turkish voice)
   - `ELEVENLABS_MODEL: str = "eleven_multilingual_v2"` (default)
   - `ELEVENLABS_STABILITY: float = 0.5` (optional, voice stability)
   - `ELEVENLABS_SIMILARITY_BOOST: float = 0.75` (optional, voice clarity)
   - Update `.env.example` with new variables

4. **Create `TTSService` abstraction** — `btcedu/services/elevenlabs_service.py` (or `tts_service.py`):
   - Define `TTSService` Protocol or ABC:
     - `synthesize(text: str, voice_id: str, model: str, **kwargs) -> TTSResponse`
     - `TTSResponse` dataclass: audio_bytes (bytes), duration_seconds (float), sample_rate (int), model (str), voice_id (str), character_count (int), cost_usd (float | None)
   - Implement `ElevenLabsService`:
     - Uses ElevenLabs text-to-speech API
     - Sends narration text → receives MP3 audio bytes
     - Handles character limits: if text > 5000 chars, split at sentence boundaries, synthesize each segment, concatenate audio
     - Handles rate limits (429) with exponential backoff (3 retries)
     - Handles API errors gracefully (returns error result or raises with descriptive message)
     - Computes audio duration from returned bytes using `pydub` (already a dependency)
     - Estimates cost from character count (ElevenLabs charges per character)

5. **Create TTSResult dataclass** — include: episode_id, segments_generated (count), segments_skipped (count), segments_failed (count), manifest, provenance, total_cost, total_duration_seconds.

6. **Implement `generate_tts()`** in `btcedu/core/tts.py`:
   - Load chapter JSON from `data/outputs/{ep_id}/chapters.json` using Sprint 6 Pydantic models
   - **Pre-condition check**: Episode status is IMAGES_GENERATED (or CHAPTERIZED if images are optional for testing)
   - Check idempotency per chapter: audio file exists AND narration text hash matches stored hash
   - For each chapter:
     - Extract narration text from `chapter.narration.text`
     - Check if audio already exists and text is unchanged → skip
     - Call `TTSService.synthesize()` with narration text
     - Save audio to `data/outputs/{ep_id}/tts/{chapter_id}.mp3`
     - Measure actual duration from audio file using `pydub.AudioSegment`
     - Record in `media_assets` table (asset_type = "audio")
   - Save manifest to `data/outputs/{ep_id}/tts/manifest.json`
   - Save provenance to `data/outputs/{ep_id}/provenance/tts_provenance.json`
   - Check cumulative episode cost against `max_episode_cost_usd` after each chapter
   - Return TTSResult

7. **TTS Manifest format** — `data/outputs/{ep_id}/tts/manifest.json`:
   ```json
   {
     "episode_id": "abc123",
     "voice_id": "turkish_male_01",
     "model": "eleven_multilingual_v2",
     "segments": [
       {
         "chapter_id": "ch01",
         "text_length": 150,
         "duration_seconds": 58.3,
         "file_path": "tts/ch01.mp3",
         "sample_rate": 44100,
         "status": "generated"
       }
     ],
     "total_duration_seconds": 720.0,
     "total_characters": 12000,
     "estimated_cost_usd": 0.36
   }
   ```

8. **Handle long narration text** — ElevenLabs has per-request character limits:
   - If `chapter.narration.text` > 5000 characters:
     - Split at sentence boundaries (split on `. ` or `\n`)
     - Synthesize each segment separately
     - Concatenate audio segments using `pydub`
     - Save concatenated result as the chapter audio
   - Track the split in provenance (number of segments per chapter)

9. **Add `tts` CLI command** to `btcedu/cli.py`:
   - `btcedu tts <episode_id>` with `--force`, `--dry-run`, `--chapter <chapter_id>`
   - Validate episode exists and is at IMAGES_GENERATED status
   - On success: update episode status to TTS_DONE
   - On failure: log error, leave status unchanged
   - Output summary: segments generated, skipped, failed, total duration, total cost

10. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
    - Ensure TTS_DONE is in `PipelineStage` enum (should exist from Sprint 1)
    - Update `resolve_pipeline_plan()` to include TTS for v2 episodes after IMAGES_GENERATED
    - Position: IMAGES_GENERATED → TTS → RENDER (Sprint 9-10)
    - Cost cap check after TTS stage

11. **Create dashboard audio preview** — add audio playback to chapter viewer or episode detail:
    - For each chapter, show:
      - Play button (HTML5 `<audio>` element with controls)
      - Duration (formatted as mm:ss)
      - Character count and narration preview
      - Generation status (generated / skipped / failed)
    - Serve audio files from local filesystem via Flask
    - Integrate into existing chapter viewer (add audio column/section) or create a dedicated TTS view
    - Link from episode detail to TTS view

12. **Write tests**:
    - `tests/test_elevenlabs_service.py`:
      - Mock ElevenLabs API: successful synthesis, rate limit, API error
      - Long text splitting and concatenation
      - Duration measurement from audio bytes
      - Cost estimation from character count
    - `tests/test_tts.py`:
      - Unit: narration text extraction from chapter JSON
      - Unit: per-chapter idempotency (skip unchanged)
      - Unit: partial regeneration (only changed chapters)
      - Integration: full TTS generation with mocked API
      - Force: `--force` regenerates all
      - Chapter flag: `--chapter ch01` regenerates only ch01
      - Cost cap: stops if episode cost exceeds limit
      - Error handling: one chapter failure doesn't fail episode
    - CLI test: `btcedu tts --help` works
    - Pipeline test: TTS included in v2 plan after IMAGES_GENERATED

13. **Verify**:
    - Run `pytest tests/`
    - Pick an episode with IMAGES_GENERATED status
    - Run `btcedu tts <ep_id> --dry-run`
    - Run `btcedu tts <ep_id>`
    - Verify audio files at `data/outputs/{ep_id}/tts/`
    - Verify manifest at `data/outputs/{ep_id}/tts/manifest.json`
    - Verify `media_assets` records (asset_type = "audio") in database
    - Verify provenance at `data/outputs/{ep_id}/provenance/tts_provenance.json`
    - Play audio files manually to verify Turkish speech quality
    - Open dashboard → navigate to audio preview → verify playback works
    - Run again → verify unchanged chapters skipped (idempotent)
    - Run with `--force` → verify all regenerated
    - Run `btcedu status` → verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement video rendering (that's Sprint 9-10).
- **Do NOT** implement video review or Review Gate 3 (Sprint 9-10).
- **Do NOT** implement YouTube publishing (Sprint 11).
- **Do NOT** implement background music mixing (deferred feature).
- **Do NOT** implement audio editing or waveform visualization in dashboard.
- **Do NOT** implement voice cloning or custom voice training.
- **Do NOT** add multiple TTS providers — ElevenLabs only. The interface is for future use.
- **Do NOT** modify existing stages (correct, translate, adapt, chapterize, imagegen).
- **Do NOT** modify the chapter JSON schema.
- **Do NOT** modify the `media_assets` table schema (use it as-is from Sprint 7).

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/image_generator.py` closely — same file I/O, API calling, provenance, idempotency, manifest patterns.
- **Service layer**: Follow `btcedu/services/image_gen_service.py` for API wrapper/abstraction pattern.
- **Dashboard**: Follow existing template and route patterns in `btcedu/web/`.
- **CLI commands**: Follow existing Click command patterns.
- **Audio processing**: Use `pydub` (already a dependency) for duration measurement and audio concatenation.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred
- Manual verification steps

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Use `pydub` for audio duration measurement (it's already a project dependency).
- Handle ElevenLabs API errors gracefully — a single chapter failure should not break the entire stage.
- Track costs: ElevenLabs charges ~$0.30 per 1000 characters for the default plan.
- Audio files should be MP3 format at 44100 Hz for compatibility with ffmpeg in Sprint 9-10.

---

## Definition of Done

- [ ] `btcedu/services/elevenlabs_service.py` exists with `TTSService` interface and `ElevenLabsService` implementation
- [ ] ElevenLabs service handles rate limits and API errors gracefully
- [ ] Long text (>5000 chars) is split at sentence boundaries and audio is concatenated
- [ ] Config extended with `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`
- [ ] `.env.example` updated with new config values
- [ ] `btcedu/core/tts.py` exists with `generate_tts()` function
- [ ] Per-chapter audio saved to `data/outputs/{ep_id}/tts/{chapter_id}.mp3`
- [ ] Audio duration measured from actual file (not estimated)
- [ ] TTS manifest saved to `data/outputs/{ep_id}/tts/manifest.json`
- [ ] Manifest format matches §5F
- [ ] `media_assets` records created for each audio segment (asset_type = "audio")
- [ ] Provenance JSON stored
- [ ] Partial regeneration works (only changed narration regenerated)
- [ ] `btcedu tts <episode_id>` CLI works with `--force`, `--dry-run`, `--chapter`
- [ ] Pipeline plan includes TTS for v2 episodes after IMAGES_GENERATED
- [ ] Episode status updated to TTS_DONE on success
- [ ] Cost tracking: cumulative cost checked against `max_episode_cost_usd`
- [ ] Dashboard audio preview shows play button, duration, and status per chapter
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- Video rendering / RENDER stage (Sprint 9-10)
- Review Gate 3 / video review (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Background music mixing
- Audio editing / waveform UI
- Voice cloning or custom voice training
- Multiple TTS providers (only ElevenLabs now; interface is for future)
- Intro/outro audio generation
