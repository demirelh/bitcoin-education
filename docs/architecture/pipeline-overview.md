# Pipeline Architecture Overview

## Two-Version Coexistence

`pipeline_version` on the Episode model (1 or 2) controls which flow runs. v1 code is untouched.

## v1 Pipeline (legacy)

```
NEW -> DOWNLOADED -> TRANSCRIBED -> CHUNKED -> GENERATED -> REFINED -> COMPLETED
```

Stages: detect, download, transcribe, chunk, generate, refine. Uses legacy prompt builders in `btcedu/prompts/`.

## v2 Pipeline (current)

```
NEW -> DOWNLOADED -> TRANSCRIBED -> CORRECTED -> [review_gate_1] ->
TRANSLATED -> ADAPTED -> [review_gate_2] -> CHAPTERIZED ->
imagegen -> [review_gate_stock] ->
IMAGES_GENERATED -> TTS_DONE -> RENDERED -> [review_gate_3] -> APPROVED -> PUBLISHED
```

### Stage definitions (`_V2_STAGES` in pipeline.py)

| Stage | Entry Status | Module | Output |
|-------|-------------|--------|--------|
| download | NEW | detector.py | audio.m4a |
| transcribe | DOWNLOADED | transcriber.py | transcript.de.txt |
| correct | TRANSCRIBED | corrector.py | transcript.corrected.de.txt |
| review_gate_1 | CORRECTED | reviewer.py | ReviewTask |
| translate | CORRECTED (after approval) | translator.py | transcript.tr.txt |
| adapt | TRANSLATED | adapter.py | script.adapted.tr.md |
| review_gate_2 | ADAPTED | reviewer.py | ReviewTask |
| chapterize | ADAPTED (after approval) | chapterizer.py | chapters.json |
| imagegen | CHAPTERIZED | stock_images.py | candidates_manifest.json |
| review_gate_stock | CHAPTERIZED | reviewer.py | ReviewTask |
| tts | IMAGES_GENERATED | tts.py | {chapter_id}.mp3 + manifest |
| render | TTS_DONE | renderer.py | draft.mp4 |
| review_gate_3 | RENDERED | reviewer.py | ReviewTask |
| publish | APPROVED | publisher.py | YouTube upload |

### Orchestration

`run_episode_pipeline()` in `pipeline.py`:
1. Resolves plan: `_get_stages()` based on `pipeline_version`
2. Iterates `_V2_STAGES`, finds first matching stage for episode's status
3. Calls `_run_stage()` (lazy imports the stage function)
4. Records `StageResult(stage, status, duration, detail, error)` and `PipelineRun` in DB
5. Review gates return `review_pending` to pause pipeline
6. Accumulates cost from `StageResult.detail` (parsed via `$` split)

### Guards

- All v2-only stages guarded in `_run_stage()` against `pipeline_version != 2`
- Each stage function validates episode status before proceeding
- `force=True` bypasses status checks (not pipeline_version check)

### Automation

- `btcedu-detect.timer` — periodic RSS scan for new episodes
- `btcedu-run.timer` — periodic `run_pending()` for actionable episodes
- `run.sh` — deploy script (git pull, pip install, migrate, restart services)
