# Sprint 8 — Validation Prompt (TTS Integration)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 8 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–7 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 8 (Phase 4: TTS Integration)** implementation of the btcedu video production pipeline.

Sprint 8 was scoped to:
- Create `TTSService` abstraction with ElevenLabs implementation
- Implement `generate_tts()` in `btcedu/core/tts.py`
- Generate per-chapter audio segments as MP3 at `data/outputs/{ep_id}/tts/{chapter_id}.mp3`
- Create TTS manifest at `data/outputs/{ep_id}/tts/manifest.json`
- Handle long narration text (>5000 chars) by splitting and concatenating
- Extend config with ElevenLabs settings
- Add `tts` CLI command with `--force`, `--dry-run`, `--chapter`
- Integrate TTS stage into v2 pipeline after IMAGES_GENERATED
- Record audio in `media_assets` table (asset_type = "audio")
- Add audio preview to dashboard
- Provenance, idempotency, cascade invalidation, cost tracking
- Write tests

Sprint 8 was NOT scoped to include: video rendering, Review Gate 3, YouTube publishing, background music, audio editing, multiple TTS providers.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. ElevenLabs Service

- [ ] **1.1** `btcedu/services/elevenlabs_service.py` (or similar) exists
- [ ] **1.2** `TTSService` Protocol/ABC defined with `synthesize()` method
- [ ] **1.3** `ElevenLabsService` implementation exists
- [ ] **1.4** Uses ElevenLabs text-to-speech API correctly
- [ ] **1.5** Configurable: voice_id, model, stability, similarity_boost
- [ ] **1.6** Handles rate limits (429) with exponential backoff (up to 3 retries)
- [ ] **1.7** Handles API errors gracefully (descriptive error messages)
- [ ] **1.8** Returns audio bytes in MP3 format
- [ ] **1.9** Computes audio duration from actual audio data (using pydub or similar)
- [ ] **1.10** Estimates cost from character count
- [ ] **1.11** Handles long text (>5000 chars): splits at sentence boundaries, synthesizes segments, concatenates

### 2. Configuration

- [ ] **2.1** `ELEVENLABS_API_KEY` added to config
- [ ] **2.2** `ELEVENLABS_VOICE_ID` added to config
- [ ] **2.3** `ELEVENLABS_MODEL` added with default `eleven_multilingual_v2`
- [ ] **2.4** Optional voice settings (stability, similarity_boost) configurable
- [ ] **2.5** `.env.example` updated with new config values
- [ ] **2.6** Config values have sensible defaults where appropriate

### 3. TTS Module

- [ ] **3.1** `btcedu/core/tts.py` exists
- [ ] **3.2** `generate_tts()` function has correct signature matching existing stage patterns
- [ ] **3.3** Function returns a structured result (TTSResult or similar)
- [ ] **3.4** Reads chapter JSON from `data/outputs/{ep_id}/chapters.json` using Sprint 6 Pydantic models
- [ ] **3.5** **Pre-condition check**: verifies IMAGES_GENERATED status
- [ ] **3.6** Iterates over all chapters and extracts narration text
- [ ] **3.7** Calls `TTSService.synthesize()` for each chapter
- [ ] **3.8** Saves audio to `data/outputs/{ep_id}/tts/{chapter_id}.mp3`
- [ ] **3.9** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **3.10** Measures actual duration from generated audio file
- [ ] **3.11** Records each audio segment in `media_assets` table (asset_type = "audio")

### 4. TTS Manifest

- [ ] **4.1** Manifest saved to `data/outputs/{ep_id}/tts/manifest.json`
- [ ] **4.2** Manifest format matches §5F: voice_id, model, per-segment entries with chapter_id, text_length, duration_seconds, file_path, sample_rate
- [ ] **4.3** Manifest includes total_duration_seconds and total_characters
- [ ] **4.4** JSON written with `indent=2` and `ensure_ascii=False`

### 5. Long Text Handling

- [ ] **5.1** Text >5000 characters is detected
- [ ] **5.2** Splitting occurs at sentence boundaries (not mid-word)
- [ ] **5.3** Split segments are synthesized individually
- [ ] **5.4** Audio segments are concatenated correctly (no gaps, no overlaps)
- [ ] **5.5** Final concatenated audio is saved as a single MP3 per chapter
- [ ] **5.6** Duration of concatenated audio is correctly measured

### 6. Provenance & Idempotency

- [ ] **6.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/tts_provenance.json`
- [ ] **6.2** Provenance format matches §3.6
- [ ] **6.3** Second run without `--force` skips chapters with existing unchanged audio
- [ ] **6.4** Idempotency checks: audio file exists AND narration text hash matches
- [ ] **6.5** `.stale` marker respected
- [ ] **6.6** Content hashes use SHA-256

### 7. Cascade Invalidation

- [ ] **7.1** Chapterization re-run (narration change) marks TTS output as stale
- [ ] **7.2** Chain: correction → translation → adaptation → chapterization → TTS
- [ ] **7.3** Image re-generation does NOT invalidate TTS (images and TTS are parallel dependencies of RENDER)
- [ ] **7.4** `.stale` markers created with invalidation metadata
- [ ] **7.5** RENDER stage (future) will be invalidated by TTS re-run (documented)

### 8. Partial Regeneration

- [ ] **8.1** Unchanged chapters are skipped on re-run (audio preserved)
- [ ] **8.2** Changed narration text triggers re-generation
- [ ] **8.3** `--force` flag regenerates all audio
- [ ] **8.4** `--chapter <chapter_id>` regenerates a single chapter's audio
- [ ] **8.5** Manifest updated correctly after partial regeneration

### 9. Cost Tracking

- [ ] **9.1** Per-chapter cost estimated from character count
- [ ] **9.2** Cumulative episode cost checked against `max_episode_cost_usd` after each chapter
- [ ] **9.3** If cost cap exceeded, stage stops with descriptive error
- [ ] **9.4** Cost recorded in provenance, manifest, and/or PipelineRun

### 10. Error Handling

- [ ] **10.1** Single chapter TTS failure does not fail entire episode
- [ ] **10.2** Failed segments recorded in manifest with error status
- [ ] **10.3** Rate limits handled with exponential backoff
- [ ] **10.4** Partial success supported (some segments generated, some failed)

### 11. CLI Command

- [ ] **11.1** `btcedu tts <episode_id>` command exists and is registered
- [ ] **11.2** `--force` flag works
- [ ] **11.3** `--dry-run` flag works
- [ ] **11.4** `--chapter <chapter_id>` flag works
- [ ] **11.5** `btcedu tts --help` shows useful help text
- [ ] **11.6** Command validates episode exists and is IMAGES_GENERATED
- [ ] **11.7** On success: episode status updated to TTS_DONE
- [ ] **11.8** Outputs summary: segments generated/skipped/failed, total duration, total cost

### 12. Pipeline Integration

- [ ] **12.1** TTS / TTS_DONE is properly wired in pipeline
- [ ] **12.2** `resolve_pipeline_plan()` includes TTS for v2 episodes after IMAGES_GENERATED
- [ ] **12.3** Position: IMAGES_GENERATED → TTS → RENDER
- [ ] **12.4** v1 pipeline is completely unaffected

### 13. Dashboard Audio Preview

- [ ] **13.1** Audio preview added to chapter viewer or dedicated route
- [ ] **13.2** Per-chapter: play button (HTML5 audio element), duration, status
- [ ] **13.3** Audio files served correctly from local filesystem
- [ ] **13.4** Failed/skipped segments shown with appropriate status
- [ ] **13.5** Follows existing dashboard template and styling patterns

### 14. V1 Pipeline + Previous Sprint Compatibility (Regression)

- [ ] **14.1** `btcedu status` still works for all episodes
- [ ] **14.2** v1 pipeline stages unmodified
- [ ] **14.3** Correction + Review Gate 1 still work
- [ ] **14.4** Translation stage still works
- [ ] **14.5** Adaptation + Review Gate 2 still work
- [ ] **14.6** Chapterization still works
- [ ] **14.7** Image generation still works
- [ ] **14.8** Chapter JSON schema consumed unmodified
- [ ] **14.9** Existing dashboard pages still function
- [ ] **14.10** Existing tests still pass
- [ ] **14.11** No existing CLI commands broken

### 15. Test Coverage

- [ ] **15.1** ElevenLabs service tests: mock API success, rate limit, error
- [ ] **15.2** Long text handling tests: split, synthesize, concatenate
- [ ] **15.3** Duration measurement tests
- [ ] **15.4** TTS module tests: per-chapter generation, idempotency, partial regeneration
- [ ] **15.5** Force tests: `--force` regenerates all
- [ ] **15.6** Single chapter tests: `--chapter` flag
- [ ] **15.7** Cost cap tests: stops if limit exceeded
- [ ] **15.8** Error handling tests: one chapter failure continues
- [ ] **15.9** CLI tests: command registration, help text
- [ ] **15.10** Pipeline tests: TTS in v2 plan
- [ ] **15.11** Dashboard tests: audio preview renders
- [ ] **15.12** All tests use mocked API calls
- [ ] **15.13** All tests pass with `pytest tests/`

### 16. Scope Creep Detection

- [ ] **16.1** No video rendering was implemented
- [ ] **16.2** No Review Gate 3 was implemented
- [ ] **16.3** No YouTube publishing was implemented
- [ ] **16.4** No background music mixing was implemented
- [ ] **16.5** No audio editing UI was implemented
- [ ] **16.6** No multiple TTS providers were implemented
- [ ] **16.7** No existing stages were modified beyond pipeline integration
- [ ] **16.8** Chapter JSON schema was not modified
- [ ] **16.9** `media_assets` table schema was not modified
- [ ] **16.10** No unnecessary dependencies were added

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 8 is complete and ready for Sprint 9 (Render Pipeline). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 9 after fixes. |
| **FAIL** | Critical issues found. Sprint 8 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### TTS Quality Assessment:

Audio quality is critical for the final video. Verify:
- [ ] Audio is clear and natural-sounding (if real API was used for verification)
- [ ] Turkish pronunciation is acceptable for technical terms
- [ ] Audio segments have consistent volume levels
- [ ] No audio artifacts at segment boundaries (if text was split)
- [ ] Duration measurements are accurate
- [ ] MP3 files are compatible with ffmpeg (for Sprint 9-10 rendering)

### Render Pipeline Readiness:

Sprint 9-10 requires images + audio + chapters. Verify the following are in place:
- [ ] Images in `data/outputs/{ep_id}/images/` (from Sprint 7)
- [ ] Audio in `data/outputs/{ep_id}/tts/` (from Sprint 8)
- [ ] Chapters in `data/outputs/{ep_id}/chapters.json` (from Sprint 6)
- [ ] All three are accessible from the same base path
- [ ] Duration data (from TTS manifest) is available for render timeline

### Deferred Items Acknowledged:

- Video assembly / Render pipeline (Sprint 9-10)
- Review Gate 3 / video review (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Background music mixing
- Audio editing / waveform UI
- Multiple TTS providers
- Voice cloning or custom training
- Intro/outro audio

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 5 (Long Text Handling)** — this is a common edge case that can produce audio artifacts.
- **Pay attention to Section 7 (Cascade Invalidation)** — TTS and IMAGE_GEN are parallel dependencies of RENDER. Image changes should NOT invalidate TTS, and vice versa. Only chapterization changes (which can change narration text) should invalidate TTS.
- Verify audio file format compatibility with ffmpeg for the upcoming RENDER sprint.
