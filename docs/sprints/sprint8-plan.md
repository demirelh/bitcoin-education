# Sprint 8 — Planning Prompt (TTS Integration)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–7 completed codebase (especially `btcedu/core/image_generator.py` for stage pattern, `btcedu/core/chapter_schema.py` for chapter JSON schema, `btcedu/models/media_asset.py`, `btcedu/services/image_gen_service.py` for service abstraction pattern, `btcedu/core/pipeline.py`, `btcedu/config.py`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the TTS module, ElevenLabs service, per-chapter audio generation, TTS manifest, audio preview in dashboard, CLI command, pipeline integration, and tests.

---

## Context

You are planning **Sprint 8 (Phase 4: TTS Integration)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–7 are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: corrector module, correction diff, provenance, CORRECT stage, Review Gate 1.
- Review System: reviewer module, dashboard review queue + diff viewer + approve/reject/request-changes.
- Translation: translator module, faithful German→Turkish translation, TRANSLATE stage.
- Adaptation: adapter module, Turkey-context adaptation with tiered rules, adaptation diff, ADAPT stage, Review Gate 2.
- Chapterization: chapterizer module, chapter JSON schema (Pydantic), validated chapters.json, chapter viewer in dashboard.
- Image Generation: image generator module, ImageGenService (DALL-E 3), media_assets table, image manifest, dashboard image gallery.

Sprint 8 implements the **TTS** stage — converting each chapter's narration text to spoken audio using ElevenLabs (or a configurable provider). This produces per-chapter MP3 audio segments that will be combined with images in the RENDER stage (Sprint 9-10).

### Sprint 8 Focus (from MASTERPLAN.md §4 Phase 4, §5F)

1. Implement `generate_tts()` in `btcedu/core/tts.py` — iterate over chapters, extract narration text, call TTS API, save per-chapter audio.
2. Create ElevenLabs service wrapper (`btcedu/services/elevenlabs_service.py`) abstracted behind a `TTSService` interface for future provider swaps.
3. Generate per-chapter audio segments as MP3 files at `data/outputs/{ep_id}/tts/{chapter_id}.mp3`.
4. Create TTS manifest (`data/outputs/{ep_id}/tts/manifest.json`) tracking generated audio with duration and metadata.
5. Add new config values: `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`.
6. Add `tts` CLI command to `btcedu/cli.py` with `--force`, `--dry-run`, `--chapter`.
7. Integrate into pipeline — TTS after IMAGES_GENERATED (or CHAPTERIZED if images are optional), before RENDER.
8. Add audio preview to dashboard (play per-chapter audio, show duration).
9. Record generated audio in `media_assets` table (asset_type = "audio").
10. Partial recovery: only regenerate audio for chapters with changed narration text.
11. Provenance, idempotency, cascade invalidation.
12. Write tests.

### Relevant Subplans

- **Subplan 5F** (TTS Integration) — all slices: ElevenLabs service wrapper, per-chapter TTS generation, audio manifest + duration tracking, CLI + pipeline integration, audio player in dashboard.
- **§8** (Idempotency) — TTS stage specifics: already done = all chapter audio files exist AND narration text matches. Partial recovery for changed chapters.
- **§3.6** (Provenance Model) — provenance JSON format.
- **§11** (Decision Matrix) — TTS provider: ElevenLabs recommended (better Turkish voice quality, simpler API).
- **§13** (Assumptions) — Turkish voice quality assumption; voice selection is an open question.

---

## Your Task

Produce a detailed implementation plan for Sprint 8. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating what is in scope and what is explicitly not.
2. **File-Level Plan** — for every file that will be created or modified, list:
   - File path
   - What changes are made (create / modify)
   - Key contents (class names, function signatures, data structures)
3. **ElevenLabs Service Design** — `TTSService` abstraction and ElevenLabs implementation:
   - Interface/protocol: `synthesize(text: str, voice_id: str, model: str, **kwargs) -> TTSResponse`
   - `TTSResponse` dataclass: audio_bytes, duration_seconds, sample_rate, model, voice_id, character_count, cost_usd
   - `ElevenLabsService` implementation:
     - Uses ElevenLabs API (text-to-speech endpoint)
     - Configurable: voice_id, model (eleven_multilingual_v2), stability, similarity_boost
     - Handles rate limits and errors gracefully
     - Returns audio bytes (MP3 format)
   - Config: API key, voice_id, model, optional voice settings
4. **TTS Module Design** — `generate_tts()` function:
   - Function signature and return type (`TTSResult` dataclass)
   - Processing logic: iterate chapters → extract narration text → call TTS → save audio
   - Per-chapter processing with partial recovery
   - Duration tracking from actual audio file (not just estimate)
   - Audio file naming: `{chapter_id}.mp3`
5. **TTS Manifest** — `data/outputs/{ep_id}/tts/manifest.json` format per §5F:
   - Per-segment entry: chapter_id, text_length, duration_seconds, file_path, sample_rate
   - Voice and model metadata
6. **Configuration** — new `.env` values:
   - `ELEVENLABS_API_KEY`
   - `ELEVENLABS_VOICE_ID`
   - `ELEVENLABS_MODEL` (default: `eleven_multilingual_v2`)
   - Optional: stability, similarity_boost, style settings
7. **CLI Command Design** — `btcedu tts <episode_id>` with `--force`, `--dry-run`, `--chapter <chapter_id>`.
8. **Pipeline Integration** — TTS after IMAGES_GENERATED, before RENDER.
9. **Dashboard Audio Preview** — audio player in episode detail / chapter view:
   - Per-chapter play button with HTML5 audio element
   - Duration display
   - Chapter title and narration text alongside
   - Serve audio files from local filesystem
10. **`media_assets` Integration** — record audio segments in existing `media_assets` table with `asset_type = "audio"`.
11. **Provenance, Idempotency, Cascade Invalidation**:
    - Idempotency: audio file exists AND narration text hash matches
    - Cascade: chapterization re-run (narration change) → audio marked stale
    - Partial recovery: only regenerate audio for changed chapters
12. **Cost Tracking** — ElevenLabs costs per character, cumulative check against `max_episode_cost_usd`.
13. **Test Plan** — list each test, what it asserts, file it belongs to.
14. **Implementation Order** — numbered sequence.
15. **Definition of Done** — checklist.
16. **Non-Goals** — explicit list.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected. TTS only runs for v2 episodes.
- **Chapter JSON is the input**: Consume chapter JSON schema from Sprint 6. Extract narration text per chapter.
- **Follow existing patterns**: The TTS module should mirror the image generator/chapterizer module pattern.
- **ElevenLabs first, abstracted for swap**: Start with ElevenLabs. Abstract behind a `TTSService` interface so the provider can be swapped (Google Cloud TTS, Azure, etc.).
- **No video rendering**: Sprint 8 handles audio only. Rendering is Sprint 9-10.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections matching the items above. Include the full service interface, TTS manifest schema, function signatures, configuration values, and dashboard UI description.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- `[ASSUMPTION]`: The ElevenLabs Python SDK (`elevenlabs` package) will be added as a dependency. If the project prefers to use raw HTTP requests to avoid a heavy dependency, note both options and recommend one.
- `[ASSUMPTION]`: Audio output format is MP3 at 44100 Hz sample rate (standard for ElevenLabs).
- Consider character limits: ElevenLabs has per-request character limits (typically 5000 chars). Long chapter narrations may need to be split and concatenated.
- Consider pronunciation of technical terms: Bitcoin/crypto jargon in Turkish may need pronunciation hints. Document this as a known issue for prompt tuning.
- The TTS stage should be parallelizable across chapters in the future, but for now sequential processing is fine (matching the single-threaded pipeline pattern).
- Audio duration should be measured from the actual generated file (using `pydub` or similar, which is already a project dependency), not just estimated from word count.
