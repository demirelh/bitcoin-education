# MASTER PLAN: btcedu Video Production Pipeline

## 1) Executive Summary

### What Exists Today

btcedu is a working Python CLI + Flask web dashboard that automatically:
- Detects new German Bitcoin podcast episodes from YouTube RSS feeds
- Downloads audio via yt-dlp
- Transcribes via OpenAI Whisper API (German)
- Chunks transcripts into ~1500-char segments with FTS5 indexing
- Generates 6 Turkish content artifacts via Claude Sonnet (outline, script, shorts, visuals, QA, publishing pack)
- Refines artifacts using QA feedback (v2 versions)
- Tracks costs, tokens, provenance (prompt hashes, retrieval snapshots)
- Deploys on Raspberry Pi via systemd timers + Caddy reverse proxy

**Tech stack**: Python 3.12, Click CLI, Flask, SQLAlchemy + SQLite + FTS5, Pydantic settings, Anthropic/OpenAI APIs, yt-dlp, systemd.

### What Will Be Added

The pipeline will be extended from "content artifact generation" to "full video production automation" with these new stages:

| # | Stage | Status | Description |
|---|-------|--------|-------------|
| 1 | Detect | EXISTS | YouTube RSS feed scanning |
| 2 | Download | EXISTS | Audio via yt-dlp |
| 3 | Transcribe | EXISTS | Whisper API (German) |
| 4 | **Correct** | NEW | LLM-improved transcript with diff review |
| 5 | **Translate** | NEW | German → Turkish translation |
| 6 | **Adapt** | NEW | Turkey-context culturally neutralized influencer version |
| 7 | **Chapterize** | NEW | Production JSON (script + visuals + overlays + timing) |
| 8 | **Image Gen** | NEW | Image/video prompts from chapter JSON |
| 9 | **TTS** | NEW | Text-to-speech audio (ElevenLabs) |
| 10 | **Render** | NEW | Video assembly (ffmpeg) |
| 11 | **Review** | NEW | Human review + approval workflow |
| 12 | **Publish** | NEW | YouTube upload via API |

The existing GENERATE and REFINE stages will be **replaced** by the new stages 4-7, which decompose the monolithic generation into reviewable, resumable steps.

### Guiding Principles

1. **Idempotency**: Every stage checks if output exists before processing. `force=True` to reprocess.
2. **Observability**: Every output traceable to source input + prompt version + model + config.
3. **Human Review**: Required gates after transcript correction, adaptation, and final render.
4. **Prompt Versioning**: Prompts are files with metadata, hashed, tracked, versioned, A/B testable.
5. **Incremental Extension**: No big rewrites. New stages added alongside existing ones.
6. **Cascade Invalidation**: When upstream changes, downstream outputs are marked stale.
7. **Safety**: No hallucination, no financial advice, editorial neutrality in cultural adaptation.

---

## 2) Current-State Assessment

### CLI (`btcedu/cli.py`)
- **Exists**: 26 Click commands covering all pipeline operations, batch processing, cost reporting, status management
- **Reuse**: Command structure, context passing, session management patterns
- **Extend**: Add new commands for each new stage (correct, translate, adapt, chapterize, imagegen, tts, render, review, publish)

### Core Pipeline (`btcedu/core/pipeline.py`)
- **Exists**: `run_episode_pipeline()`, `run_pending()`, `run_latest()`, `retry_episode()`, PipelineReport/StageResult dataclasses, `resolve_pipeline_plan()`
- **Reuse**: Pipeline orchestration pattern, report writing, cost tracking
- **Extend**: Add new stages to `PipelineStage` enum, extend `resolve_pipeline_plan()`, add review gate logic

### Episode Status (`btcedu/models/episode.py`)
- **Exists**: `EpisodeStatus` enum: NEW → DOWNLOADED → TRANSCRIBED → CHUNKED → GENERATED → REFINED → COMPLETED | FAILED
- **Reuse**: Status progression pattern
- **Extend**: Insert new statuses: CORRECTED, TRANSLATED, ADAPTED, CHAPTERIZED, IMAGES_GENERATED, TTS_DONE, RENDERED, REVIEWED, PUBLISHED

### Generator (`btcedu/core/generator.py`)
- **Exists**: `generate_content()` producing 6 artifacts, `refine_content()` producing 3 v2 artifacts, retrieval snapshot system
- **Reuse**: `call_claude()` pattern, cost tracking, dry-run support, prompt hash computation
- **Refactor**: The monolithic generate stage will be decomposed. Existing artifacts (outline, script, shorts, visuals, qa, publishing) can be retained as optional/legacy or mapped to new stages.

### Prompt System (`btcedu/prompts/`)
- **Exists**: Python modules with `build_user_prompt()` functions, system prompt with Turkish constraints
- **Reuse**: Constraint framework, citation format, safety guardrails
- **Extend**: Move to file-based prompts (Jinja2 or plain text) with metadata headers for versioning. Keep Python builders as compatibility layer.

### Services (`btcedu/services/`)
- **Exists**: claude_service, transcription_service, feed_service, download_service
- **Reuse**: All services directly reusable
- **Add**: elevenlabs_service, image_gen_service, youtube_service, ffmpeg_service

### Web Dashboard (`btcedu/web/`)
- **Exists**: Flask SPA, episode list/detail, file viewer, job polling, batch processing
- **Reuse**: All existing UI patterns
- **Extend**: Add review queue, diff viewer, approval workflow, media preview

### Database (`btcedu/models/`)
- **Exists**: Episode, Chunk, PipelineRun, ContentArtifact, Channel, SchemaMigration
- **Reuse**: All existing models
- **Add**: PromptVersion, ReviewTask, ReviewDecision, MediaAsset, RenderJob, PublishJob

### Config (`btcedu/config.py`)
- **Exists**: Pydantic Settings from .env
- **Extend**: Add ElevenLabs API key, YouTube API credentials, image gen API key, ffmpeg path, review settings

### Deployment (`deploy/`)
- **Exists**: systemd services/timers for detect, run, web
- **Reuse**: Same deployment pattern
- **Extend**: Potentially add render worker service (long-running renders)

---

## 3) Target Architecture

### 3.1 Extended Pipeline Stages

```
NEW
 → DOWNLOADED          (audio.m4a)
 → TRANSCRIBED         (transcript.de.txt)
 → CORRECTED           (transcript.corrected.de.txt + diff)     ← REVIEW GATE 1
 → TRANSLATED          (transcript.tr.txt)
 → ADAPTED             (script.adapted.tr.txt)                  ← REVIEW GATE 2
 → CHAPTERIZED         (chapters.json)
 → IMAGES_GENERATED    (images/ folder)
 → TTS_DONE            (tts/ folder with chapter audio)
 → RENDERED            (draft_video.mp4)                        ← REVIEW GATE 3
 → APPROVED            (human approved)
 → PUBLISHED           (live on YouTube)
 → COMPLETED           (archived)
```

**Legacy compatibility**: The existing CHUNKED/GENERATED/REFINED statuses remain valid for episodes processed under the old pipeline. New episodes use the new status flow. The `pipeline_version` field on Episode distinguishes them.

### 3.2 Data Flow

```
Source Video (YouTube)
    │
    ▼
[DETECT] ──► Episode record (NEW)
    │
    ▼
[DOWNLOAD] ──► data/raw/{ep_id}/audio.m4a
    │
    ▼
[TRANSCRIBE] ──► data/transcripts/{ep_id}/transcript.de.txt
    │
    ▼
[CORRECT] ──► data/transcripts/{ep_id}/transcript.corrected.de.txt
    │           + data/transcripts/{ep_id}/correction_diff.json
    │
    ▼
  ◆ REVIEW GATE 1 (approve/reject correction)
    │
    ▼
[TRANSLATE] ──► data/transcripts/{ep_id}/transcript.tr.txt
    │
    ▼
[ADAPT] ──► data/outputs/{ep_id}/script.adapted.tr.md
    │
    ▼
  ◆ REVIEW GATE 2 (approve/reject adaptation)
    │
    ▼
[CHAPTERIZE] ──► data/outputs/{ep_id}/chapters.json
    │
    ▼
[IMAGE GEN] ──► data/outputs/{ep_id}/images/{chapter_id}.png
    │
    ▼
[TTS] ──► data/outputs/{ep_id}/tts/{chapter_id}.mp3
    │
    ▼
[RENDER] ──► data/outputs/{ep_id}/render/draft.mp4
    │
    ▼
  ◆ REVIEW GATE 3 (approve/reject video)
    │
    ▼
[PUBLISH] ──► YouTube video ID recorded
```

### 3.3 Human Review Gates

Three mandatory review gates:

| Gate | After Stage | What's Reviewed | Actions |
|------|-------------|-----------------|---------|
| RG1 | CORRECT | Transcript corrections (diff view) | Approve / Reject / Edit |
| RG2 | ADAPT | Turkey-adapted influencer script | Approve / Reject / Regenerate |
| RG3 | RENDER | Draft video (watch + read script) | Approve / Reject / Request Changes |

Review gates **block** pipeline progression. The pipeline pauses and creates a `ReviewTask`. The content owner reviews in the dashboard and approves/rejects. On approval, the pipeline resumes automatically (or waits for manual trigger).

### 3.4 Prompt Versioning Subsystem

```
btcedu/prompts/
├── templates/                    # Prompt template files
│   ├── correct_transcript.md     # Jinja2 template
│   ├── translate.md
│   ├── adapt.md
│   ├── chapterize.md
│   ├── imagegen.md
│   └── system.md
├── registry.py                   # PromptRegistry class
├── system.py                     # Legacy (kept for backward compat)
├── outline.py                    # Legacy
└── ...
```

**PromptRegistry** manages:
- Loading templates from files
- Computing content hashes (SHA-256 of template + variables)
- Recording prompt versions in DB
- Resolving "approved default" version per prompt name
- Comparing outputs across prompt versions

### 3.5 Artifact Storage Conventions

```
data/outputs/{episode_id}/
├── script.adapted.tr.md          # Adapted script
├── chapters.json                 # Production chapter JSON
├── images/                       # Generated images
│   ├── ch01_intro.png
│   ├── ch02_main.png
│   └── ...
├── tts/                          # TTS audio segments
│   ├── ch01.mp3
│   ├── ch02.mp3
│   └── ...
├── render/                       # Video render artifacts
│   ├── draft.mp4
│   ├── render_manifest.json      # ffmpeg instructions
│   └── timeline.json             # Edit decision list
├── review/                       # Review artifacts
│   ├── correction_diff.json
│   ├── adaptation_diff.json
│   └── review_history.json
├── provenance/                   # (replaces retrieval/)
│   ├── correct_provenance.json
│   ├── translate_provenance.json
│   ├── adapt_provenance.json
│   └── ...
└── retrieval/                    # Legacy (kept)
    └── ...
```

### 3.6 Provenance Model

Every stage execution records a provenance file:

```json
{
  "stage": "correct",
  "episode_id": "abc123",
  "timestamp": "2026-02-22T10:30:00Z",
  "prompt_name": "correct_transcript",
  "prompt_version": "v3",
  "prompt_hash": "sha256:abc...",
  "model": "claude-sonnet-4-20250514",
  "model_params": {"temperature": 0.2, "max_tokens": 8192},
  "input_files": ["data/transcripts/abc123/transcript.de.txt"],
  "output_files": ["data/transcripts/abc123/transcript.corrected.de.txt"],
  "input_tokens": 4500,
  "output_tokens": 5200,
  "cost_usd": 0.093,
  "duration_seconds": 12.5
}
```

### 3.7 Failure Handling & Retry

- Each stage wraps execution in try/except, records error in PipelineRun
- Episode status stays at previous successful stage on failure
- `retry_count` incremented on failure
- `btcedu retry <episode_id>` resumes from last successful stage
- API failures (rate limit, timeout): exponential backoff with 3 retries within stage
- Review rejections: not a "failure" — episode goes back to previous stage for regeneration

### 3.8 Idempotency Strategy

Per stage, "already done" = output file exists AND prompt hash matches AND input file unchanged. Details in Section 8.

---

## 4) Master Plan (Phased Roadmap)

### Phase 0: Foundation & Schema Evolution (Sprint 1)
**Objective**: Extend data model, status enum, and pipeline orchestration for new stages without breaking existing functionality.

**Scope**:
- Add new EpisodeStatus values
- Add `pipeline_version` field to Episode
- Create `prompt_versions` and `review_tasks` tables
- Extend `resolve_pipeline_plan()` for new stages
- Add new PipelineStage enum values

**Dependencies**: None (foundation)
**Deliverables**: Migration scripts, updated models, passing tests
**Success Criteria**: Existing pipeline still works unchanged. New statuses exist but aren't used yet.
**Risks**: Migration could break existing data → mitigated by backward-compatible additive changes only
**Opus Task**: Design exact schema changes and migration SQL
**Sonnet Task**: Implement migrations, update models, add tests

### Phase 1: Transcript Correction + Review System (Sprint 2-3)
**Objective**: Add CORRECT stage with diff-based human review.

**Scope**:
- Implement `correct_transcript()` in `btcedu/core/corrector.py`
- Create correction prompt template
- Build diff computation and storage
- Build review queue UI in dashboard
- Implement review approval/rejection flow

**Dependencies**: Phase 0
**Deliverables**: Working correction stage, review UI, diff viewer
**Success Criteria**: Can correct a transcript, view diff in dashboard, approve/reject, pipeline resumes
**Risks**: Correction quality depends on prompt → mitigated by prompt versioning and iterative improvement
**Opus Task**: Design correction prompt, diff format, review UI wireframe
**Sonnet Task**: Implement corrector module, review API endpoints, diff viewer component

### Phase 2: Translation + Adaptation (Sprint 4-5)
**Objective**: Add TRANSLATE and ADAPT stages with cultural neutralization.

**Scope**:
- Implement `translate_transcript()` in `btcedu/core/translator.py`
- Implement `adapt_script()` in `btcedu/core/adapter.py`
- Create translation and adaptation prompt templates
- Add Review Gate 2 for adaptation
- Extend dashboard with adaptation diff view

**Dependencies**: Phase 1
**Deliverables**: Working translation and adaptation, reviewable adaptation
**Success Criteria**: German transcript → Turkish translation → Turkey-adapted script, reviewable
**Risks**: Cultural adaptation quality needs careful prompt engineering
**Opus Task**: Design adaptation prompt with specific neutralization rules
**Sonnet Task**: Implement translator and adapter modules, extend review UI

### Phase 3: Chapterization + Image Generation (Sprint 6-7)
**Objective**: Add CHAPTERIZE and IMAGE_GEN stages.

**Scope**:
- Implement `chapterize_script()` in `btcedu/core/chapterizer.py`
- Define production JSON schema (what is said/shown/overlays/timing)
- Implement `generate_images()` in `btcedu/core/image_generator.py`
- Add image generation service (DALL-E, Flux, or similar)
- Display chapters and images in dashboard

**Dependencies**: Phase 2
**Deliverables**: Chapter JSON, generated images per chapter
**Success Criteria**: Adapted script → structured chapters → visual assets
**Risks**: Image generation API costs, quality consistency
**Opus Task**: Design chapter JSON schema, image prompt strategy
**Sonnet Task**: Implement chapterizer, image generator, dashboard preview

### Phase 4: TTS Integration (Sprint 8)
**Objective**: Add TTS stage with ElevenLabs (or configurable provider).

**Scope**:
- Implement `generate_tts()` in `btcedu/core/tts.py`
- Add `elevenlabs_service.py`
- Generate per-chapter audio segments
- Audio preview in dashboard

**Dependencies**: Phase 3
**Deliverables**: TTS audio per chapter, audio player in dashboard
**Success Criteria**: Chapter text → spoken audio segments, playable in dashboard
**Risks**: Voice quality, pronunciation of technical terms, API costs
**Opus Task**: Design TTS configuration (voice selection, speed, etc.)
**Sonnet Task**: Implement TTS module and service, audio preview UI

### Phase 5: Video Assembly (Sprint 9-10)
**Objective**: Add RENDER stage producing draft video.

**Scope**:
- Implement `render_video()` in `btcedu/core/renderer.py`
- Add `ffmpeg_service.py` for video composition
- Create render manifest format (timeline + assets)
- Generate draft video from images + TTS + overlays
- Video preview in dashboard
- Review Gate 3

**Dependencies**: Phase 4
**Deliverables**: Draft video from assets, video player in dashboard
**Success Criteria**: Images + audio + text overlays → watchable draft video
**Risks**: ffmpeg complexity, render time on Raspberry Pi, storage
**Opus Task**: Design render manifest, ffmpeg pipeline, overlay system
**Sonnet Task**: Implement renderer, ffmpeg service, video preview

### Phase 6: YouTube Publishing (Sprint 11)
**Objective**: Add PUBLISH stage for YouTube upload.

**Scope**:
- Implement `publish_video()` in `btcedu/core/publisher.py`
- Add `youtube_service.py` using YouTube Data API v3
- OAuth2 flow for YouTube authentication
- Publish with metadata from publishing_pack
- Record YouTube video ID in database

**Dependencies**: Phase 5
**Deliverables**: Automated YouTube publishing
**Success Criteria**: Approved video → published on YouTube with correct metadata
**Risks**: YouTube API quotas, OAuth token management
**Opus Task**: Design publishing workflow, OAuth strategy
**Sonnet Task**: Implement publisher, YouTube service, OAuth flow

### Phase 7: Prompt Management Framework (Ongoing, starts Phase 1)
**Objective**: Build prompt versioning, experimentation, and quality tracking.

**Scope**:
- PromptRegistry implementation
- Prompt metadata files
- Version comparison in dashboard
- A/B testing support
- Approved default management

**Dependencies**: Phase 0 (schema), then incremental
**Deliverables**: Full prompt governance system
**Success Criteria**: Can version prompts, compare outputs, promote versions
**Risks**: Over-engineering → mitigated by starting simple (file hash tracking) and expanding

---

## 5) Detailed Subplans

### 5A. Transcript Correction + Diff Review

**Problem**: Whisper transcripts contain errors — wrong words, missing punctuation, speaker misattribution, technical term misspellings (especially Bitcoin/crypto jargon in German).

**Architecture Changes**:
- New module: `btcedu/core/corrector.py`
- New prompt: `btcedu/prompts/templates/correct_transcript.md`
- New CLI command: `btcedu correct <episode_id>`

**Core Function**:
```python
def correct_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> CorrectionResult:
```

**Data Contract**:
- Input: `data/transcripts/{ep_id}/transcript.de.txt` (raw Whisper output)
- Output: `data/transcripts/{ep_id}/transcript.corrected.de.txt`
- Diff: `data/outputs/{ep_id}/review/correction_diff.json`

**Diff Format**:
```json
{
  "episode_id": "abc123",
  "original_length": 15000,
  "corrected_length": 14800,
  "changes": [
    {
      "type": "replace",
      "original": "Bit Coin",
      "corrected": "Bitcoin",
      "context": "...und dann hat er über Bit Coin gesprochen...",
      "position": {"start": 1234, "end": 1242},
      "category": "spelling"
    }
  ],
  "summary": {
    "total_changes": 42,
    "by_category": {"spelling": 15, "punctuation": 20, "grammar": 7}
  }
}
```

**Dashboard Implications**:
- Diff viewer component showing original vs corrected side-by-side
- Change categories highlighted in different colors
- Approve/reject buttons
- Inline editing capability (future)

**Edge Cases**:
- Very long transcripts (>30min): process in segments, reassemble
- Whisper produces empty transcript: fail with clear error
- Correction LLM hallucinating content: system prompt forbids adding information

**Tests**:
- Unit: diff computation from known inputs
- Integration: full correction pipeline with dry-run
- E2E: correction → review → approval flow

**Implementation Slices**:
1. Core corrector with basic prompt, file output
2. Diff computation and storage
3. CLI command and pipeline integration
4. Dashboard diff viewer
5. Review gate integration

### 5B. Turkish Translation

**Problem**: The corrected German transcript must be translated to Turkish as a faithful, high-quality translation before cultural adaptation.

**Architecture Changes**:
- New module: `btcedu/core/translator.py`
- New prompt: `btcedu/prompts/templates/translate.md`
- New CLI command: `btcedu translate <episode_id>`

**Data Contract**:
- Input: `data/transcripts/{ep_id}/transcript.corrected.de.txt`
- Output: `data/transcripts/{ep_id}/transcript.tr.txt`
- Provenance: `data/outputs/{ep_id}/provenance/translate_provenance.json`

**Key Design Decisions**:
- Translation is a **faithful** rendering — no adaptation yet
- Technical terms kept with original in parentheses: "madencilik (Mining)"
- Segment-by-segment processing for long texts (align with paragraph breaks)
- No review gate here (translation is mechanical; adaptation is where editorial judgment matters)

**Edge Cases**:
- Untranslatable cultural references: keep original with translator's note
- Code/URLs in transcript: pass through unchanged
- Speaker names: keep original

**Implementation Slices**:
1. Core translator with segmented processing
2. CLI command and pipeline integration
3. Dashboard view of translated text

### 5C. Turkey-Context Adaptation

**Problem**: The Turkish translation must be adapted for a Turkish audience. Germany-specific references, examples, regulatory context, banking systems, etc. need to be neutralized or adapted to Turkish equivalents — while remaining editorially neutral and factually accurate.

**Architecture Changes**:
- New module: `btcedu/core/adapter.py`
- New prompt: `btcedu/prompts/templates/adapt.md`
- New CLI command: `btcedu adapt <episode_id>`

**Data Contract**:
- Input: `data/transcripts/{ep_id}/transcript.tr.txt` + `data/transcripts/{ep_id}/transcript.corrected.de.txt` (German original for reference)
- Output: `data/outputs/{ep_id}/script.adapted.tr.md`
- Diff: `data/outputs/{ep_id}/review/adaptation_diff.json` (changes from literal translation)

**Adaptation Rules** (encoded in prompt):
1. Replace German institutions with Turkish equivalents (BaFin → SPK, Sparkasse → generic bank)
2. Replace Euro amounts with context-appropriate Turkish Lira or keep in USD
3. Replace German cultural references with Turkish ones
4. Remove Germany-specific legal/tax advice entirely (flag with "[kaldırıldı: ülkeye özgü]")
5. Keep all Bitcoin/crypto facts unchanged
6. Add Turkish influencer tone: conversational, engaging, use "siz" (formal you)
7. NO political commentary, NO financial advice, editorial neutrality

**Dashboard Implications**:
- Side-by-side: literal translation vs adapted version
- Highlighted adaptations (color-coded by type)
- Review Gate 2: approve/reject/regenerate

**Edge Cases**:
- Episode is entirely about German regulation: heavy adaptation needed, flag for manual review
- Technical deep-dive with no cultural references: minimal adaptation, mostly tone change
- Guest speakers mentioned by name: keep names, contextualize if needed

**Tests**:
- Unit: adaptation rules applied correctly to known inputs
- Integration: full adapt pipeline with known German→Turkish pairs
- Quality: manual review of first 5 episodes to calibrate prompt

**Implementation Slices**:
1. Core adapter with basic prompt
2. Adaptation diff computation
3. CLI command and pipeline integration
4. Review Gate 2 in dashboard
5. Prompt iteration based on review feedback

### 5D. Chapterized Production JSON

**Problem**: The adapted script must be broken into structured chapters for video production — each chapter defines what is said (narration), what is shown (visual), what overlays appear (text/graphics), and timing guidance.

**Architecture Changes**:
- New module: `btcedu/core/chapterizer.py`
- New prompt: `btcedu/prompts/templates/chapterize.md`
- New CLI command: `btcedu chapterize <episode_id>`

**Data Contract**:
- Input: `data/outputs/{ep_id}/script.adapted.tr.md`
- Output: `data/outputs/{ep_id}/chapters.json`

**Chapter JSON Schema**:
```json
{
  "episode_id": "abc123",
  "title": "Bitcoin Nedir?",
  "total_chapters": 8,
  "estimated_duration_seconds": 720,
  "chapters": [
    {
      "chapter_id": "ch01",
      "title": "Giriş",
      "order": 1,
      "narration": {
        "text": "Merhaba arkadaşlar, bugün Bitcoin'in...",
        "word_count": 150,
        "estimated_duration_seconds": 60
      },
      "visual": {
        "type": "title_card",
        "description": "Channel logo with episode title overlay",
        "image_prompt": null
      },
      "overlays": [
        {
          "type": "lower_third",
          "text": "Bitcoin Nedir?",
          "start_offset_seconds": 2,
          "duration_seconds": 5
        }
      ],
      "transitions": {
        "in": "fade",
        "out": "cut"
      },
      "notes": "Hook the viewer in first 5 seconds"
    }
  ]
}
```

**Implementation Slices**:
1. Core chapterizer with basic JSON output
2. Duration estimation from word count (Turkish ~150 words/min)
3. Visual type classification (title_card, diagram, b_roll, talking_head, screen_share)
4. CLI command and pipeline integration
5. Chapter viewer in dashboard (timeline view)

### 5E. Image/Video Prompt Generation

**Problem**: Each chapter needs visual assets. The system generates image generation prompts from the chapter JSON, then calls an image generation API.

**Architecture Changes**:
- New module: `btcedu/core/image_generator.py`
- New service: `btcedu/services/image_gen_service.py`
- New prompt: `btcedu/prompts/templates/imagegen.md`
- New CLI command: `btcedu imagegen <episode_id>`

**Data Contract**:
- Input: `data/outputs/{ep_id}/chapters.json`
- Output: `data/outputs/{ep_id}/images/{chapter_id}.png`
- Metadata: `data/outputs/{ep_id}/images/manifest.json`

**Image Manifest**:
```json
{
  "episode_id": "abc123",
  "images": [
    {
      "chapter_id": "ch01",
      "prompt": "Clean minimalist illustration of Bitcoin logo...",
      "model": "dall-e-3",
      "size": "1920x1080",
      "file_path": "images/ch01_intro.png",
      "generated_at": "2026-02-22T10:00:00Z"
    }
  ]
}
```

**API Strategy**:
- Start with DALL-E 3 (OpenAI, already have API key)
- Abstract behind `ImageGenService` interface for future swap (Flux, Midjourney, etc.)
- Style consistency prompt prefix: "In the style of [defined brand guidelines]..."

**Implementation Slices**:
1. Image prompt generation from chapter JSON (LLM call)
2. Image generation service (DALL-E 3)
3. Image storage and manifest
4. CLI command and pipeline integration
5. Image gallery in dashboard

### 5F. TTS Integration

**Problem**: Each chapter's narration text needs to be converted to spoken audio.

**Architecture Changes**:
- New module: `btcedu/core/tts.py`
- New service: `btcedu/services/elevenlabs_service.py`
- New CLI command: `btcedu tts <episode_id>`

**Data Contract**:
- Input: `data/outputs/{ep_id}/chapters.json` (narration texts)
- Output: `data/outputs/{ep_id}/tts/{chapter_id}.mp3`
- Metadata: `data/outputs/{ep_id}/tts/manifest.json`

**TTS Manifest**:
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
      "sample_rate": 44100
    }
  ]
}
```

**Config**:
```python
# .env additions
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ELEVENLABS_MODEL=eleven_multilingual_v2
```

**Implementation Slices**:
1. ElevenLabs service wrapper
2. Per-chapter TTS generation
3. Audio manifest and duration tracking
4. CLI command and pipeline integration
5. Audio player in dashboard

### 5G. Video Assembly / Render Pipeline

**Problem**: Combine images, TTS audio, text overlays, and transitions into a draft video.

**Architecture Changes**:
- New module: `btcedu/core/renderer.py`
- New service: `btcedu/services/ffmpeg_service.py`
- New CLI command: `btcedu render <episode_id>`

**Data Contract**:
- Input: `chapters.json` + `images/` + `tts/` + brand assets
- Output: `data/outputs/{ep_id}/render/draft.mp4`
- Manifest: `data/outputs/{ep_id}/render/render_manifest.json`

**Render Manifest** (ffmpeg instructions):
```json
{
  "episode_id": "abc123",
  "resolution": "1920x1080",
  "fps": 30,
  "segments": [
    {
      "chapter_id": "ch01",
      "image": "images/ch01_intro.png",
      "audio": "tts/ch01.mp3",
      "duration_seconds": 58.3,
      "overlays": [
        {
          "type": "lower_third",
          "text": "Bitcoin Nedir?",
          "font": "NotoSans-Bold",
          "position": "bottom_center",
          "start": 2.0,
          "end": 7.0
        }
      ],
      "transition_in": "fade",
      "transition_out": "cut"
    }
  ]
}
```

**ffmpeg Strategy**:
- Build ffmpeg filter complex from render manifest
- Per-chapter: image (scaled to 1080p) + audio + text overlays
- Concatenate chapters with transitions
- Output: H.264 MP4, AAC audio

**Risks**:
- Raspberry Pi render time: ~10-30 min for 15 min video → acceptable for daily pipeline
- Storage: ~500MB per draft video → manageable with periodic cleanup

**Implementation Slices**:
1. ffmpeg service: single image + audio → video segment
2. Text overlay support
3. Chapter concatenation
4. Full render pipeline from manifest
5. CLI command and pipeline integration
6. Video preview in dashboard

### 5H. Human Review & Approval Workflow

**Problem**: Content owner must review and approve key outputs before pipeline continues.

**Architecture Changes**:
- New module: `btcedu/core/reviewer.py`
- New routes: `btcedu/web/review_routes.py`
- New templates: review queue, diff viewer, approval form
- New models: `ReviewTask`, `ReviewDecision`

**Review Task State Machine**:
```
PENDING → IN_REVIEW → APPROVED
                    → REJECTED → (triggers re-generation)
                    → CHANGES_REQUESTED → (triggers re-generation with notes)
```

**ReviewTask Schema**:
```python
class ReviewTask(Base):
    id: int
    episode_id: str
    stage: str                    # "correct", "adapt", "render"
    status: ReviewStatus          # PENDING, IN_REVIEW, APPROVED, REJECTED
    artifact_paths: JSON          # list of files to review
    diff_path: str | None         # path to diff JSON
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_notes: str | None
    prompt_version_id: int | None # FK to PromptVersion
```

**Dashboard UI**:
- **Review Queue**: List of pending reviews, sorted by creation date
- **Review Detail**: Depends on stage:
  - Correction: side-by-side diff with highlighted changes
  - Adaptation: original translation vs adapted version
  - Render: embedded video player + chapter script
- **Actions**: Approve (green), Reject (red), Request Changes (yellow, with notes)
- **History**: Timeline of all reviews for an episode

**Workflow**:
1. Pipeline reaches review gate → creates ReviewTask (PENDING)
2. Dashboard shows badge count of pending reviews
3. Content owner opens review, status → IN_REVIEW
4. Owner approves → status → APPROVED, pipeline resumes
5. Owner rejects → status → REJECTED, episode goes back to previous stage
6. Owner requests changes → notes saved, episode goes back for regeneration with notes as additional prompt context

**Implementation Slices**:
1. ReviewTask model and migration
2. Core reviewer logic (create task, approve, reject)
3. Review queue API endpoints
4. Review queue UI (list view)
5. Diff viewer component (for correction review)
6. Adaptation review view
7. Video review view
8. Notification system (optional: email/webhook on new review)

### 5I. YouTube Publishing Integration

**Problem**: Approved videos need to be uploaded to YouTube with proper metadata.

**Architecture Changes**:
- New module: `btcedu/core/publisher.py`
- New service: `btcedu/services/youtube_service.py`
- New CLI command: `btcedu publish <episode_id>`

**YouTube Service**:
- Uses YouTube Data API v3
- OAuth2 for authentication (stored in `data/.youtube_credentials.json`)
- Upload resumable (large files)
- Set: title, description, tags, category, thumbnail, language, chapters

**Data from existing artifacts**:
- Title, description, tags, chapters: from `publishing_pack.v2.json` (or chapters.json timestamps)
- Thumbnail: from first chapter image or dedicated thumbnail generation
- Video file: from `render/draft.mp4`

**PublishJob Schema**:
```python
class PublishJob(Base):
    id: int
    episode_id: str
    status: str                   # PENDING, UPLOADING, PUBLISHED, FAILED
    youtube_video_id: str | None
    youtube_url: str | None
    published_at: datetime | None
    metadata_snapshot: JSON       # what was sent to YouTube
    error_message: str | None
```

**Implementation Slices**:
1. YouTube service with OAuth2 setup flow
2. Video upload with resumable upload
3. Metadata setting (title, description, tags)
4. CLI command
5. Dashboard publish button and status display

### 5J. Prompt Management / Versioning Framework

**Problem**: Prompts are the core product. They need to be versioned, tracked, compared, and improved over time.

**Architecture Changes**:
- New module: `btcedu/core/prompt_registry.py`
- New directory: `btcedu/prompts/templates/` (file-based prompts)
- New model: `PromptVersion`
- Dashboard: prompt comparison view

**PromptVersion Schema**:
```python
class PromptVersion(Base):
    id: int
    name: str                     # "correct_transcript", "adapt", etc.
    version: int                  # auto-incrementing per name
    content_hash: str             # SHA-256 of template content
    template_path: str            # relative path to template file
    model: str                    # intended model
    temperature: float
    max_tokens: int
    is_default: bool              # approved default for this prompt name
    created_at: datetime
    notes: str | None             # why this version was created
```

**PromptRegistry API**:
```python
class PromptRegistry:
    def get_default(self, name: str) -> PromptVersion
    def register_version(self, name: str, template_path: str, **params) -> PromptVersion
    def promote_to_default(self, version_id: int) -> None
    def get_history(self, name: str) -> list[PromptVersion]
    def compare_outputs(self, name: str, v1: int, v2: int) -> ComparisonReport
```

**Workflow**:
1. Developer edits a prompt template file
2. On next pipeline run, registry detects hash change → creates new PromptVersion
3. New version runs alongside (or replaces) default
4. Content owner reviews outputs from new version
5. If better: promote to default via CLI or dashboard
6. Old version retained for audit trail

**Implementation Slices**:
1. PromptVersion model and migration
2. PromptRegistry core (load, hash, register)
3. Integration with `call_claude()` to record prompt version
4. CLI commands: `btcedu prompt list`, `btcedu prompt promote <id>`
5. Dashboard prompt version viewer
6. Output comparison view (same input, different prompt versions)

---

## 6) Prompt Strategy

### 6.1 Where Prompts Live

```
btcedu/prompts/
├── templates/                    # File-based prompt templates (versioned)
│   ├── system.md                # Shared system prompt
│   ├── correct_transcript.md    # Transcript correction
│   ├── translate.md             # German → Turkish translation
│   ├── adapt.md                 # Cultural adaptation
│   ├── chapterize.md            # Production JSON generation
│   ├── imagegen.md              # Image generation prompts
│   └── README.md                # Prompt authoring guidelines
├── registry.py                  # PromptRegistry class
├── system.py                    # Legacy system prompt (kept for backward compat)
├── outline.py                   # Legacy (kept)
├── script.py                    # Legacy (kept)
└── ...                          # Legacy (kept)
```

### 6.2 Naming & Versioning Conventions

- **File name** = prompt name: `correct_transcript.md` → name = `correct_transcript`
- **Version**: auto-incrementing integer per name (v1, v2, v3...)
- **Hash**: SHA-256 of file content (after variable substitution template is stripped)
- **Approval**: one version per name is `is_default=True`

### 6.3 Prompt Template Format

```markdown
---
name: correct_transcript
model: claude-sonnet-4-20250514
temperature: 0.2
max_tokens: 8192
description: Corrects Whisper transcript errors while preserving content
author: content_owner
---

# System

You are a German-language transcript editor specializing in Bitcoin/crypto content...

# Instructions

Given the following raw Whisper transcript, correct:
1. Spelling errors (especially technical terms)
2. Punctuation and sentence boundaries
3. Speaker attribution markers
4. Obvious mistranscriptions

Do NOT:
- Add information not in the original
- Change the meaning or tone
- Remove content
- Translate anything

# Input

{{ transcript }}

# Output Format

Return the corrected transcript as plain text.
```

### 6.4 Prompt Metadata Schema

Stored in YAML frontmatter of each template file AND in `PromptVersion` DB record:
- `name`: identifier
- `model`: target model
- `temperature`: recommended temperature
- `max_tokens`: max output tokens
- `description`: what this prompt does
- `author`: who wrote/last edited
- `version`: auto from registry
- `content_hash`: computed

### 6.5 A/B Testing Approach

**Simple approach** (recommended for solo developer):
1. Create variant template: `correct_transcript_v2.md`
2. Run both on same episode: `btcedu correct <ep_id> --prompt-version 2`
3. Compare outputs in dashboard side-by-side
4. If v2 is better: `btcedu prompt promote correct_transcript 2`

**No infrastructure needed** — just manual comparison via CLI/dashboard.

### 6.6 Prompt Hash Storage

Every `ContentArtifact` already has `prompt_hash`. The PromptVersion table adds the reverse lookup: hash → version → template content. Combined, you can:
- Find which prompt version created any output
- Find all outputs created by a specific prompt version
- Detect when an output was created with a non-default prompt version

### 6.7 Review Methods for Prompt Quality

**Transcript Correction Prompt**:
- Metric: diff size (too many changes = overzealous, too few = missed errors)
- Manual spot check: 5 random changes per episode
- Regression test: known transcript with known corrections

**Adaptation Prompt**:
- Metric: number of cultural adaptations per episode
- Manual review: all adaptations shown in diff view
- Red flag: any factual claim changed (should only change framing, not facts)

**Chapterization Prompt**:
- Metric: chapter count, duration balance, visual type distribution
- Manual review: chapter structure makes sense for video
- Regression: known script → expected chapter structure

---

## 7) Data Model & Schema Evolution Plan

### 7.1 New Episode Statuses

```python
class EpisodeStatus(str, Enum):
    # Existing
    NEW = "new"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    CHUNKED = "chunked"              # Legacy (v1 pipeline)
    GENERATED = "generated"          # Legacy (v1 pipeline)
    REFINED = "refined"              # Legacy (v1 pipeline)
    # New (v2 pipeline)
    CORRECTED = "corrected"
    TRANSLATED = "translated"
    ADAPTED = "adapted"
    CHAPTERIZED = "chapterized"
    IMAGES_GENERATED = "images_generated"
    TTS_DONE = "tts_done"
    RENDERED = "rendered"
    APPROVED = "approved"
    PUBLISHED = "published"
    # Terminal
    COMPLETED = "completed"
    FAILED = "failed"
```

### 7.2 Episode Model Extension

```python
# New fields on Episode
pipeline_version: int = 1           # 1 = legacy, 2 = new pipeline
review_status: str | None = None    # current review gate status
youtube_video_id: str | None = None
published_at_youtube: datetime | None = None
```

**Migration**: `ALTER TABLE episodes ADD COLUMN pipeline_version INTEGER DEFAULT 1`
**Backward compat**: All existing episodes get `pipeline_version=1`, new ones get `2`.

### 7.3 New Tables

#### `prompt_versions`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| name | TEXT NOT NULL | prompt identifier |
| version | INTEGER NOT NULL | auto-increment per name |
| content_hash | TEXT NOT NULL | SHA-256 |
| template_path | TEXT | relative to repo root |
| model | TEXT | target model |
| temperature | REAL | |
| max_tokens | INTEGER | |
| is_default | BOOLEAN | one per name |
| created_at | DATETIME | |
| notes | TEXT | |

**Unique**: (name, version), (name, content_hash)
**Index**: name, is_default

#### `review_tasks`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| episode_id | TEXT NOT NULL | |
| stage | TEXT NOT NULL | correct/adapt/render |
| status | TEXT NOT NULL | pending/in_review/approved/rejected/changes_requested |
| artifact_paths | TEXT (JSON) | files to review |
| diff_path | TEXT | path to diff file |
| prompt_version_id | INTEGER FK | which prompt produced this |
| created_at | DATETIME | |
| reviewed_at | DATETIME | |
| reviewer_notes | TEXT | |

**Index**: (episode_id, stage), status

#### `review_decisions`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| review_task_id | INTEGER FK | |
| decision | TEXT | approved/rejected/changes_requested |
| notes | TEXT | |
| decided_at | DATETIME | |

**Purpose**: Audit trail — one ReviewTask can have multiple decisions (reject → regenerate → approve).

#### `media_assets`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| episode_id | TEXT NOT NULL | |
| asset_type | TEXT | image/audio/video |
| chapter_id | TEXT | |
| file_path | TEXT | |
| mime_type | TEXT | |
| size_bytes | INTEGER | |
| duration_seconds | REAL | for audio/video |
| metadata | TEXT (JSON) | generation params |
| prompt_version_id | INTEGER FK | |
| created_at | DATETIME | |

**Index**: (episode_id, asset_type, chapter_id)

#### `publish_jobs`
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| episode_id | TEXT NOT NULL | |
| status | TEXT | pending/uploading/published/failed |
| youtube_video_id | TEXT | |
| youtube_url | TEXT | |
| metadata_snapshot | TEXT (JSON) | what was sent |
| published_at | DATETIME | |
| error_message | TEXT | |

**Index**: episode_id, status

### 7.4 Migration Sequencing

1. **Migration N+1**: Add `pipeline_version`, `review_status`, `youtube_video_id`, `published_at_youtube` to `episodes`
2. **Migration N+2**: Create `prompt_versions` table
3. **Migration N+3**: Create `review_tasks` and `review_decisions` tables
4. **Migration N+4**: Create `media_assets` table
5. **Migration N+5**: Create `publish_jobs` table
6. **Migration N+6**: Add new EpisodeStatus values (SQLite stores as TEXT, so this is just updating the Python enum)

**All migrations are additive** — no column drops or renames. Full backward compatibility.

---

## 8) Stage-by-Stage Idempotency + Retry Design

### CORRECT Stage
- **Already done**: `transcript.corrected.de.txt` exists AND `prompt_hash` matches current default prompt version AND `transcript.de.txt` hasn't changed (compare mtime or hash)
- **Invalidated by**: transcript re-run, prompt version change
- **Force re-run**: `btcedu correct <ep_id> --force`
- **Cascade**: invalidates TRANSLATE, ADAPT, and all downstream

### TRANSLATE Stage
- **Already done**: `transcript.tr.txt` exists AND input hash matches AND prompt hash matches
- **Invalidated by**: correction change, prompt version change
- **Cascade**: invalidates ADAPT and all downstream

### ADAPT Stage
- **Already done**: `script.adapted.tr.md` exists AND input hash matches AND prompt hash matches
- **Invalidated by**: translation change, prompt version change
- **Cascade**: invalidates CHAPTERIZE and all downstream
- **Special**: review rejection also invalidates

### CHAPTERIZE Stage
- **Already done**: `chapters.json` exists AND input hash matches AND prompt hash matches
- **Invalidated by**: adaptation change, prompt version change
- **Cascade**: invalidates IMAGE_GEN, TTS, RENDER

### IMAGE_GEN Stage
- **Already done**: all chapter images exist in `images/` AND chapter count matches
- **Invalidated by**: chapters.json change
- **Partial recovery**: only regenerate images for changed chapters
- **Cascade**: invalidates RENDER

### TTS Stage
- **Already done**: all chapter audio files exist in `tts/` AND narration text matches
- **Invalidated by**: chapters.json narration change
- **Partial recovery**: only regenerate audio for changed chapters
- **Cascade**: invalidates RENDER

### RENDER Stage
- **Already done**: `draft.mp4` exists AND all inputs unchanged
- **Invalidated by**: any image, audio, or chapter change
- **Force re-run**: always possible (deterministic from inputs)
- **Cascade**: invalidates APPROVED status

### PUBLISH Stage
- **Already done**: `youtube_video_id` is set
- **Invalidated by**: manual decision only (can't un-publish easily)
- **Special**: publishing is irreversible — requires explicit approval

### Cascade Invalidation Implementation

```python
# In pipeline.py
STAGE_DEPENDENCIES = {
    "translate": ["correct"],
    "adapt": ["translate"],
    "chapterize": ["adapt"],
    "imagegen": ["chapterize"],
    "tts": ["chapterize"],
    "render": ["imagegen", "tts"],
    "publish": ["render"],
}

def invalidate_downstream(session, episode_id, from_stage):
    """Mark all downstream stages as needing re-run."""
    # Walk dependency graph, delete output files, reset status
```

---

## 9) Quality Assurance & Human Review Design

### 9.1 Review Queue UI

**Dashboard route**: `/reviews`

```
┌──────────────────────────────────────────────────────┐
│  Review Queue                              [3 pending]│
├──────────────────────────────────────────────────────┤
│  ⏳ EP-abc123 | Transcript Correction | 2h ago       │
│  ⏳ EP-def456 | Adaptation Review     | 1d ago       │
│  ⏳ EP-ghi789 | Video Review          | 3d ago       │
├──────────────────────────────────────────────────────┤
│  ✅ EP-xyz000 | Adaptation Review     | Approved 5d  │
│  ❌ EP-xyz000 | Adaptation Review     | Rejected 6d  │
└──────────────────────────────────────────────────────┘
```

### 9.2 Diff View (Correction Review)

```
┌─────────────────────┬─────────────────────┐
│  Original (Whisper)  │  Corrected           │
├─────────────────────┼─────────────────────┤
│  ...über Bit Coin   │  ...über Bitcoin     │
│  gesprochen und er  │  gesprochen, und er  │
│  hat gesagt das...  │  hat gesagt, dass... │
├─────────────────────┴─────────────────────┤
│  Summary: 42 changes (15 spelling,         │
│  20 punctuation, 7 grammar)                │
├────────────────────────────────────────────┤
│  [✅ Approve]  [❌ Reject]  [✏️ Edit]      │
└────────────────────────────────────────────┘
```

### 9.3 Review Status State Machine

```
                    ┌─────────────┐
        ┌──────────│   PENDING   │
        │          └──────┬──────┘
        │                 │ (user opens)
        │          ┌──────▼──────┐
        │          │  IN_REVIEW  │
        │          └──┬───┬───┬──┘
        │    approve  │   │   │  reject / request changes
        │       ┌─────▼┐  │  ┌▼──────────────┐
        │       │APPROV.│  │  │   REJECTED /   │
        │       └──────┘  │  │ CHANGES_REQ'D  │
        │                 │  └───────┬────────┘
        │                 │          │ (regenerate)
        │                 │   ┌──────▼──────┐
        └─────────────────┴───│ NEW PENDING  │
                              └─────────────┘
```

### 9.4 Keeping User Workload Low

1. **Auto-approve minor corrections**: If correction diff has <5 changes and all are punctuation → auto-approve (configurable)
2. **Batch review**: Review multiple episodes at once
3. **Priority sorting**: Newest episodes first
4. **Summary view**: Show statistics before full diff (so user can quickly approve trivial cases)
5. **One-click approve**: Big green button, no confirmation dialog needed
6. **Notifications**: Optional webhook/email when new review is pending

---

## 10) Implementation Sequencing (Opus + Sonnet Workflow)

### 10.1 Template: Opus Planning Prompt

```
You are in Plan Mode. Your task is to create a detailed implementation
plan for [SUBPLAN NAME] of the btcedu video production pipeline.

Context:
- Read MASTERPLAN.md for the overall architecture
- Read the current codebase to understand existing patterns
- This subplan covers: [BRIEF DESCRIPTION]

Your plan must include:
1. Exact files to create or modify (with paths)
2. Function signatures and class definitions
3. Database migration SQL (if applicable)
4. New CLI commands (Click decorators)
5. New API endpoints (Flask routes)
6. Test plan (what to test, how)
7. Implementation order (what to code first)

Constraints:
- Follow existing patterns in the codebase
- Use existing services (call_claude, etc.) where possible
- Keep backward compatibility with v1 pipeline
- Write the plan to [PLAN_FILE_PATH]
```

### 10.2 Template: Sonnet Implementation Prompt

```
Implement [SUBPLAN NAME] for btcedu based on the plan in [PLAN_FILE_PATH].

Rules:
1. Read the plan file first
2. Read existing files before modifying them
3. Follow existing code patterns (see btcedu/core/generator.py for reference)
4. Make small, focused commits
5. Run tests after each significant change
6. Do NOT modify files outside the plan scope
7. Do NOT break existing functionality

Implementation order:
1. Database migration (if any)
2. Core module implementation
3. Service layer (if any)
4. CLI command
5. Pipeline integration
6. API endpoints
7. Tests

After implementation, run:
- pytest tests/
- btcedu [new-command] --dry-run (if applicable)
- btcedu status (verify existing pipeline still works)
```

### 10.3 Template: Review/Checklist Prompt

```
Review the implementation of [SUBPLAN NAME] in btcedu.

Checklist:
- [ ] All files from the plan are created/modified
- [ ] No unplanned files were changed
- [ ] Database migration runs cleanly (forward only)
- [ ] CLI command works (--help shows correctly)
- [ ] Existing pipeline still works (run btcedu status)
- [ ] Tests pass (pytest tests/)
- [ ] Idempotency: running the stage twice produces same result
- [ ] Force flag works (--force re-runs the stage)
- [ ] Error handling: stage fails gracefully on API error
- [ ] Provenance: output file is traceable to prompt version + input
- [ ] Dashboard: new data shows correctly (if applicable)
```

### 10.4 Context Preservation Between Sessions

**Progress log**: `MASTERPLAN_PROGRESS.md`

```markdown
# Implementation Progress

## Phase 0: Foundation ✅
- [x] Schema migration (2026-02-25)
- [x] New status enum values
- [x] Tests passing

## Phase 1: Transcript Correction 🔄
- [x] Core corrector module (2026-03-01)
- [x] Correction prompt template
- [ ] Diff computation ← NEXT
- [ ] Review queue UI
- [ ] Pipeline integration

## Notes
- Correction prompt v1 tends to over-correct punctuation. Need to tune.
- Dashboard dark theme CSS needs update for diff view colors.
```

---

## 11) Risks, Tradeoffs, and Decision Matrix

| Decision | Option A | Option B | Recommended | Rationale |
|----------|----------|----------|-------------|-----------|
| Pipeline execution | Local sequential | Distributed workers | **Local sequential** | Solo dev, RPi, no need for parallelism yet |
| Video assembly | ffmpeg CLI | MoviePy/OpenCV | **ffmpeg CLI** | More capable, well-documented, already available |
| Image generation | DALL-E 3 | Local Stable Diffusion | **DALL-E 3** | RPi can't run SD; API is simpler |
| Prompt storage | Python modules (current) | File-based templates | **File-based templates** | Easier to version, diff, edit without code changes |
| Schema outputs | Strict JSON schema | Flexible JSON | **Strict JSON schema** | Reproducibility, validation, downstream parsing |
| Human review | Dashboard-only | Dashboard + CLI | **Dashboard + CLI** | CLI for quick approvals, dashboard for diffs |
| TTS provider | ElevenLabs | Google Cloud TTS | **ElevenLabs** | Better Turkish voice quality, simpler API |
| Video storage | Local filesystem | S3/object storage | **Local filesystem** | Sufficient for current scale, simpler |
| Multi-model | Single model (Claude) | Different models per stage | **Single model** for now | Simplicity; can swap later via config |
| Review automation | All manual | Auto-approve trivial | **Auto-approve trivial** | Reduces workload, configurable threshold |

---

## 12) First 3 Execution Sprints

### Sprint 1: Foundation (2-3 sessions)

**Scope**:
1. Add new `EpisodeStatus` values to enum
2. Add `pipeline_version` column to Episode
3. Create `prompt_versions` table + `PromptVersion` model
4. Create `review_tasks` + `review_decisions` tables + models
5. Create `btcedu/prompts/templates/` directory with first template (system.md)
6. Implement basic `PromptRegistry` (load, hash, register)
7. Tests for all new models and registry

**Files touched**:
- `btcedu/models/episode.py` (extend enum, add columns)
- `btcedu/models/__init__.py` (new model imports)
- `btcedu/models/prompt_version.py` (new)
- `btcedu/models/review.py` (new)
- `btcedu/core/prompt_registry.py` (new)
- `btcedu/prompts/templates/system.md` (new)
- `btcedu/db/migrations/` (new migration files)
- `tests/test_models.py` (new tests)
- `tests/test_prompt_registry.py` (new)

**Migrations**:
```sql
-- Migration N+1
ALTER TABLE episodes ADD COLUMN pipeline_version INTEGER DEFAULT 1;
ALTER TABLE episodes ADD COLUMN review_status TEXT;
ALTER TABLE episodes ADD COLUMN youtube_video_id TEXT;

-- Migration N+2
CREATE TABLE prompt_versions (...);

-- Migration N+3
CREATE TABLE review_tasks (...);
CREATE TABLE review_decisions (...);
```

**Manual Verification**:
- `btcedu migrate` runs cleanly
- `btcedu status` still works
- Existing episodes show `pipeline_version=1`
- `btcedu prompt list` shows system prompt

**Fallback**: Migrations are additive only — can be reverted by dropping new columns/tables.

**Defer**: No UI changes yet. No new pipeline stages yet.

### Sprint 2: Transcript Correction Stage (3-4 sessions)

**Scope**:
1. Create correction prompt template (`correct_transcript.md`)
2. Implement `btcedu/core/corrector.py` with `correct_transcript()`
3. Implement diff computation (structured JSON diff)
4. Add `correct` CLI command
5. Integrate into pipeline (after TRANSCRIBED, before TRANSLATED)
6. Register prompt version on first run
7. Store provenance
8. Tests

**Files touched**:
- `btcedu/core/corrector.py` (new)
- `btcedu/prompts/templates/correct_transcript.md` (new)
- `btcedu/cli.py` (add `correct` command)
- `btcedu/core/pipeline.py` (extend stage list)
- `tests/test_corrector.py` (new)

**Tests**:
- Unit: diff computation from known transcript pairs
- Integration: `correct_transcript()` with dry-run
- CLI: `btcedu correct --help` works
- Idempotency: running twice produces same output (skips on second run)

**Manual Verification**:
- Pick an existing transcribed episode
- Run `btcedu correct <ep_id>`
- Verify output file exists at expected path
- Verify diff JSON is generated
- Run again → skipped (idempotent)
- Run with `--force` → re-runs

**Defer**: Dashboard diff viewer (Sprint 3). Review gate integration (Sprint 3).

### Sprint 3: Review System + Dashboard (3-4 sessions)

**Scope**:
1. Implement `btcedu/core/reviewer.py` (create_review, approve, reject)
2. Add review gate to pipeline after CORRECT stage
3. Create review queue API endpoints in Flask
4. Build review queue UI (list of pending reviews)
5. Build diff viewer component for correction review
6. Implement approve/reject flow
7. Pipeline resumes on approval
8. Tests

**Files touched**:
- `btcedu/core/reviewer.py` (new)
- `btcedu/web/app.py` or `btcedu/web/routes.py` (new review endpoints)
- `btcedu/web/templates/` (review queue template, diff viewer)
- `btcedu/web/static/` (diff viewer CSS/JS)
- `btcedu/core/pipeline.py` (add review gate logic)
- `btcedu/cli.py` (add `review approve/reject` commands)
- `tests/test_reviewer.py` (new)
- `tests/test_review_api.py` (new)

**UI Changes**:
- New `/reviews` route showing pending review queue
- New `/reviews/<id>` route showing diff viewer for correction review
- Badge in nav bar showing pending review count
- Approve/reject buttons

**Manual Verification**:
- Run full pipeline on new episode → stops at CORRECTED, creates ReviewTask
- Open dashboard → review queue shows pending task
- Click review → see diff view
- Click approve → pipeline status advances
- Click reject on another → episode goes back to TRANSCRIBED

**Defer**: Adaptation review (Phase 2). Video review (Phase 5). Auto-approve rules.

---

## 13) Assumptions and Open Questions

### Assumptions (Non-Blocking)

1. **Turkish voice quality**: ElevenLabs has acceptable Turkish voices. If not, can swap to Google Cloud TTS or Azure.
2. **Image style consistency**: A well-crafted style prefix in image prompts will produce consistent visual branding. May need fine-tuning.
3. **Raspberry Pi render capacity**: ffmpeg on RPi can render a 15-min video in <30 minutes. If too slow, can offload to cloud or optimize settings.
4. **YouTube API quotas**: Default quota (10,000 units/day) is sufficient for 1 video/day. Upload = 1600 units.
5. **Single content owner**: One person reviews. If team review is needed later, add user/role model.
6. **Claude Sonnet is sufficient**: All LLM stages use Claude Sonnet 4. If quality needs Opus for adaptation, can configure per-stage.
7. **Legacy pipeline coexistence**: v1 episodes (CHUNKED/GENERATED/REFINED) remain valid and viewable. No migration of old episodes to v2 pipeline.
8. **Storage**: ~1GB per fully-rendered episode. RPi SD card has sufficient space for ~50+ episodes. Can add cleanup policy later.

### Open Questions (Validate Later)

1. **ElevenLabs voice selection**: Which Turkish voice to use? Test 2-3 options before committing.
2. **Image generation model**: DALL-E 3 vs alternatives? Cost vs quality tradeoff needs testing.
3. **Video resolution**: 1080p or 4K? Start with 1080p for speed.
4. **Chapter structure**: How many chapters per 15-min episode? 6-10 seems right, but validate with content owner.
5. **Thumbnail generation**: Separate stage or part of image generation? Defer to Phase 3.
6. **Background music**: Should draft video include background music? Defer — can add as overlay in render manifest later.
7. **Intro/outro templates**: Standard branded intro/outro? Defer — can add as static assets in render pipeline.
8. **Multi-channel support**: Current architecture supports multiple channels. Should v2 pipeline be per-channel configurable? Defer.
9. **Cost budgets**: Should there be per-episode cost limits? API costs for full pipeline (Whisper + Claude + DALL-E + ElevenLabs) estimated at $2-5/episode. Validate.
10. **Prompt language**: Should prompt templates be in English or Turkish? Recommend English (easier to maintain), with Turkish output instructions.

---

*This master plan is designed for incremental execution. Each phase builds on the previous one. The first 3 sprints can begin immediately. Future Opus sessions should reference this document and MASTERPLAN_PROGRESS.md for context.*
