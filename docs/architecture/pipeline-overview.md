# Pipeline Architecture Overview

## Version compatibility

New profiles and new episodes use pipeline v2. Existing database rows with
`pipeline_version=1` remain supported by the legacy compatibility path. Profile
validation no longer accepts new v1 profiles.

## v1 Pipeline (legacy)

```
NEW -> DOWNLOADED -> TRANSCRIBED -> CHUNKED -> GENERATED -> REFINED -> COMPLETED
```

Stages: detect, download, transcribe, chunk, generate, refine. Uses legacy prompt builders in `btcedu/prompts/`.

## v2 Pipeline (current)

```
download
  -> transcribe
  -> transcript_analyze
  -> transcript_verify
  -> correct
  -> transcript_qa
  -> review_gate_transcript_qa
  -> review_gate_1
  -> segment (profile-enabled)
  -> translate
  -> adapt (disabled/conditional/full by profile)
  -> review_gate_2 + translation quality gate
  -> chapterize
  -> frameextract
  -> imagegen
  -> review_gate_stock
  -> tts
  -> anchorgen
  -> render
  -> review_gate_3
  -> publish
```

Deterministic translation QA and the independent model cascade execute inside
the translation quality-gate path before chapterize. A RED gate blocks; GREEN
stores the approved narration hash. The `tagesschau_tr` profile automatically
passes ordinary intermediate reviews but keeps publishing manual.

### Stage definitions (`_V2_STAGES` and profile routing in `pipeline.py`)

| Stage | Entry Status | Module | Output |
|-------|-------------|--------|--------|
| download | NEW | detector.py | audio.m4a |
| transcribe | DOWNLOADED | transcriber.py | legacy text + `transcript.structured.de.json` |
| transcript_analyze | TRANSCRIBED | transcript_analyzer.py | `transcript_analysis.json` |
| transcript_verify | TRANSCRIBED | transcript_verifier.py | `transcript_verification.json` |
| correct | TRANSCRIBED | corrector.py | corrected text + structured correction |
| transcript_qa | CORRECTED | transcript_qa.py | `transcript_qa.json` |
| review_gate_transcript_qa | CORRECTED | reviewer.py | artifact-bound `ReviewTask` when blocked |
| review_gate_1 | CORRECTED | reviewer.py | ReviewTask |
| segment | CORRECTED | segmenter.py | `stories.json` with segment traceability |
| translate | CORRECTED (after approval) | translator.py | transcript.tr.txt |
| adapt | TRANSLATED | adapter.py | constrained story output + legacy script |
| review_gate_2 | ADAPTED | qa_reviewer.py / reviewer.py | deterministic + LLM QA, retries, ReviewTask |
| chapterize | ADAPTED (after GREEN/approval) | chapterizer.py | narration-locked `chapters.json` |
| frameextract | CHAPTERIZED | frame_extractor.py | frame manifest |
| imagegen | FRAMES_EXTRACTED | image_generator.py | provider-routed media manifest |
| review_gate_stock | FRAMES_EXTRACTED | reviewer.py | ReviewTask |
| tts | IMAGES_GENERATED | tts.py | {chapter_id}.mp3 + manifest |
| anchorgen | TTS_DONE | anchor_generator.py | optional anchor assets or no-op status |
| render | ANCHOR_GENERATED | renderer.py | draft.mp4 + render provenance |
| review_gate_3 | RENDERED | reviewer.py | ReviewTask |
| publish | APPROVED | publisher.py | YouTube upload |

### Orchestration

`run_episode_pipeline()` in `pipeline.py`:
1. Resolves plan: `_get_stages()` based on `pipeline_version`
2. Iterates `_V2_STAGES`, finds first matching stage for episode's status
3. Calls `_run_stage()` (lazy imports the stage function)
4. Records `StageResult(stage, status, duration, detail, error)` and `PipelineRun` in DB
5. Review/QA gates return `review_pending` or a blocking result
6. Paid stages perform an episode Cost Guard before each API call and persist
   structured `cost_usd`

### Guards

- New transcript/QA stages are excluded for stored v1 episodes
- Each stage function validates episode status before proceeding
- `force=True` bypasses status checks (not pipeline_version check)
- Chapterize validates the approved narration hash
- Publish validates GREEN/manual override, current render and narration hashes,
  cost, profile permission, and final manual approval

### Selective invalidation

- Primary transcript/audio changes invalidate transcript analysis through media.
- QA rule changes rerun QA; media stays current unless approved narration changes.
- Image prompt changes invalidate image generation and render, not TTS.
- Voice/model/lexicon changes invalidate TTS and render.
- Overlay-only changes invalidate render.

Every stage combines input hashes, provider/model configuration, and provenance
to skip unchanged work. `.stale` markers communicate targeted downstream
invalidation.

### Automation

- `btcedu-detect.timer` — periodic RSS scan for new episodes
- `btcedu-run.timer` — periodic `run_pending()` for actionable episodes
- `run.sh` — deploy script (git pull, pip install, migrate, restart services)
