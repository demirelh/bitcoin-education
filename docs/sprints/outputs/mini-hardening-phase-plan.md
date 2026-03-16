# Mini Hardening Phase — Plan

**Date:** 2026-03-16
**Source:** Combined Validation Report (Phases 1–5), P1 recommendations
**Scope:** 3 work items, ~2–3 hours estimated implementation
**Goal:** Close the highest-priority gaps identified in cross-phase validation before production use

---

## Work Items Overview

| ID | Title | Priority | Risk Addressed |
|----|-------|----------|----------------|
| H-1 | Phase 4 video smoke-test support on Raspberry Pi | P1 | Untested ffmpeg video path on target hardware |
| H-2 | Register Phase 3 intent extraction in PromptRegistry | P1 | LLM cost invisible to tracking/audit system |
| H-3 | Two missing integration tests | P1 | Untested failure/apply paths in Phase 4 + Phase 5 |

---

## H-1: Phase 4 Video Smoke-Test Support

### Problem

`normalize_video_clip()` and `create_video_segment()` produce ffmpeg commands with filters (`scale`, `pad`, `yuv420p`, `-stream_loop -1`) that have never been validated on the Raspberry Pi's ARM64 ffmpeg build. Unit tests verify command construction (string matching) but never execute ffmpeg. A format or filter incompatibility on Pi would surface only at render time in production.

### Approach

Add a CLI smoke-test command (`btcedu smoke-test-video`) that exercises the real ffmpeg path end-to-end with a minimal synthetic input. This command:

1. **Generates a 2-second test video** using ffmpeg's `testsrc2` filter (no external file needed)
2. **Runs `normalize_video_clip()`** on the test video with target resolution from settings
3. **Generates a 2-second silent audio file** using ffmpeg's `anullsrc` filter
4. **Runs `create_video_segment()`** with the normalized video + silent audio
5. **Validates output**: checks file exists, duration ≈ 2s (±0.5s), has video+audio streams
6. **Cleans up** temporary files (or keeps them with `--keep` flag)

### Files to Modify

| File | Change |
|------|--------|
| `btcedu/cli.py` | Add `smoke-test-video` command |
| `btcedu/services/ffmpeg_service.py` | Add `generate_test_video(output_path, duration, resolution)` and `generate_silent_audio(output_path, duration)` helper functions |
| `tests/test_ffmpeg_smoke.py` | New test file: 3 tests verifying the smoke-test helpers work with `dry_run=True` (command construction only — CI has no ffmpeg) |

### Implementation Details

**`generate_test_video()`** — produces a short H.264/yuv420p clip:
```
ffmpeg -f lavfi -i testsrc2=duration={duration}:size={resolution}:rate=30
       -c:v libx264 -pix_fmt yuv420p -crf 23 {output_path}
```

**`generate_silent_audio()`** — produces a silent AAC audio file:
```
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo
       -t {duration} -c:a aac -b:a 192k {output_path}
```

**CLI command** (`smoke-test-video`):
- Options: `--keep` (retain temp files), `--resolution` (override, default from settings)
- Exit code 0 on success, 1 on any failure
- Prints ffmpeg version, filter availability, and pass/fail for each step
- Uses `tempfile.mkdtemp()` for working directory

**Validation checks** (after `create_video_segment()` completes):
- Output file exists and size > 0
- `ffprobe` reports exactly 1 video stream + 1 audio stream
- Duration within ±0.5s of target
- Video codec is H.264, pixel format is yuv420p

### Test Strategy

Three dry-run tests in `tests/test_ffmpeg_smoke.py`:
1. `test_generate_test_video_command` — verify ffmpeg command includes `testsrc2`, `libx264`, `yuv420p`
2. `test_generate_silent_audio_command` — verify ffmpeg command includes `anullsrc`, `aac`
3. `test_smoke_test_video_integration_dry_run` — run the full smoke-test sequence with `dry_run=True`, verify no exceptions and all steps attempted

### Acceptance Criteria

- [ ] `btcedu smoke-test-video` runs successfully on Raspberry Pi (real ffmpeg execution)
- [ ] Output video has correct codec, resolution, pixel format, and duration
- [ ] `--keep` flag preserves temp files for manual inspection
- [ ] 3 new dry-run tests pass in CI (no ffmpeg required)

---

## H-2: Register Phase 3 Intent Extraction in PromptRegistry

### Problem

`extract_chapter_intents()` in `stock_images.py` (lines 737–905) constructs its LLM prompt inline — a hardcoded system message and user message assembled with f-strings. This bypasses the `PromptRegistry` entirely, meaning:

1. **No `PromptVersion` record** — the prompt's content hash is never registered in the DB
2. **No `ContentArtifact` record** — LLM calls have no audit trail linking prompt version to output
3. **No cost attribution** — `PipelineRun` for `imagegen` stage captures total cost but doesn't distinguish image generation vs. intent extraction LLM calls
4. **No `prompt_hash` tracking** — if the intent extraction prompt changes, there's no way to detect which episodes used which version

`rank_candidates()` (lines 476–734) has the same pattern but is a lower priority — it's a scoring/ranking call, not a content-generating call. This plan covers `extract_chapter_intents()` only; `rank_candidates()` can follow the same pattern later.

### Approach

1. **Create a prompt template** `btcedu/prompts/templates/intent_extract.md` — a file already exists with proper YAML frontmatter but is completely unused at runtime. Populate it with the current hardcoded prompt, parameterized with Jinja2 variables.

2. **Register on load** — call `PromptRegistry.register_version()` during `extract_chapter_intents()` (same pattern as `correct_transcript()`, `translate_transcript()`, etc.)

3. **Track prompt hash** — pass `prompt_hash` to `call_claude()` (already supported) so `ContentArtifact` records link to the registered version.

4. **Record cost separately** — the `call_claude()` return value (`ClaudeResponse`) already includes `cost_usd`. Accumulate intent extraction cost and include it in `ImageGenResult` as a new field `intent_extraction_cost_usd`.

### Files to Modify

| File | Change |
|------|--------|
| `btcedu/prompts/templates/intent_extract.md` | Replace stub content with actual prompt template (system + user sections, Jinja2 variables: `chapter_title`, `visual_description`, `image_prompt`, `visual_type`, `chapter_context`) |
| `btcedu/core/stock_images.py` | In `extract_chapter_intents()`: load template via `PromptRegistry`, register version, pass `prompt_hash` to `call_claude()`, return cost breakdown |
| `btcedu/core/image_generator.py` | Add `intent_extraction_cost_usd` field to `ImageGenResult`; include in total cost |
| `tests/test_stock_ranking.py` | Update `mock_extract_intents` fixture if return signature changes; add 2 tests for registry integration |

### Implementation Details

**Template structure** (`intent_extract.md`):

```yaml
---
name: intent_extract
version: 1
model: "{{ model }}"
temperature: 0.2
max_tokens: 1024
---
```

Body: the existing system message (lines 761–790 of `stock_images.py`) becomes the template body. Variables:
- `{{ visual_type }}` — e.g., "b_roll", "diagram"
- `{{ chapter_title }}` — chapter title for context
- `{{ visual_description }}` — the `visual.description` field
- `{{ image_prompt }}` — the `visual.image_prompt` field
- `{{ chapter_context }}` — optional surrounding chapter titles for continuity

**Registration call** (in `extract_chapter_intents()`):

```python
registry = PromptRegistry(session, settings)
tpl = registry.load_template("intent_extract")
prompt_hash = registry.compute_hash(tpl.render(chapter_title=..., ...))
pv = registry.register_version("intent_extract", prompt_hash, ...)
```

This mirrors the pattern in `corrector.py` (lines 68–85) exactly.

**Cost tracking**:

Currently `extract_chapter_intents()` returns `list[ChapterIntent]`. Change return to `tuple[list[ChapterIntent], float]` where the float is total LLM cost for all intent extraction calls in the batch. The caller in `search_stock_images()` accumulates this into the existing cost tracking.

### Test Strategy

2 new tests in `tests/test_stock_ranking.py`:
1. `test_intent_extract_registers_prompt_version` — mock `call_claude`, verify `PromptVersion` record created in DB with name `"intent_extract"`
2. `test_intent_extract_returns_cost` — mock `call_claude` with known `cost_usd`, verify returned cost matches

Update existing `mock_extract_intents` fixture to return the new `tuple` signature.

### Acceptance Criteria

- [ ] `extract_chapter_intents()` loads prompt from `intent_extract.md` template via `PromptRegistry`
- [ ] `PromptVersion` record created in DB on first call
- [ ] `prompt_hash` passed to `call_claude()` for `ContentArtifact` linkage
- [ ] Intent extraction cost tracked separately and included in `ImageGenResult`
- [ ] Existing `mock_extract_intents` tests still pass (fixture updated)
- [ ] 2 new registry integration tests pass
- [ ] No change to `rank_candidates()` in this phase (deferred to P2)

---

## H-3: Two Missing Integration Tests

### Problem

The combined validation report identified two untested paths:

1. **Phase 5 — Adaptation `apply` API endpoint** (`POST /api/reviews/<id>/items/apply`): The endpoint calls `apply_item_decisions()` which assembles a reviewed sidecar file from per-item decisions. The 17 tests in `test_review_item_api.py` cover individual item actions (accept/reject/edit/reset) but none exercise the `apply` endpoint that triggers the assembly + sidecar write.

2. **Phase 4 — `finalize_selections()` video normalization failure fallback**: When `normalize_video_clip()` raises an exception during finalization, the code (lines 1282–1296 of `stock_images.py`) falls back to a placeholder entry. No test exercises this path.

### H-3a: Adaptation Apply API Test

**File:** `tests/test_review_item_api.py`

**New test class:** `TestApplyItemDecisionsAPI` (3 tests)

#### Test 1: `test_apply_adaptation_creates_sidecar`

Setup:
- Create episode at `ADAPTED` status with `pipeline_version=2`
- Create `ReviewTask` for `review_gate_2` stage, status `IN_REVIEW`
- Write an adaptation diff JSON to the expected diff path (`data/outputs/{ep_id}/review/adaptation_diff.json`)
- Write the adapted script to `data/outputs/{ep_id}/script.adapted.tr.md`
- Create `ReviewItemDecision` records: 1 ACCEPTED, 1 REJECTED, 1 EDITED (with `edited_text`)

Action:
- `POST /api/reviews/{review_id}/items/apply`

Assertions:
- Response 200
- Sidecar file exists at `data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md`
- Sidecar content reflects the decisions: accepted items applied, rejected items use original text, edited items use `edited_text`
- Response JSON contains `applied_count` and `sidecar_path`

#### Test 2: `test_apply_correction_creates_sidecar`

Same pattern but for `review_gate_1` with correction diff format:
- `correction_diff.json` (word-level positions, `changes` key)
- `transcript.corrected.de.txt` as source
- Sidecar: `transcript.reviewed.de.txt`

#### Test 3: `test_apply_on_non_actionable_review_returns_409`

- Create `ReviewTask` with status `APPROVED` (not actionable)
- `POST /api/reviews/{review_id}/items/apply` → 409

#### Fixture Notes

The existing `review_setup` fixture in `test_review_item_api.py` creates an episode + review task. Extend or create a parallel fixture `review_setup_with_diff` that also writes the diff JSON and source artifact to `tmp_path`-based output directories. Use `monkeypatch` to override `settings.outputs_dir`.

The adaptation diff format (from `adapter.py` line 603):
```json
{
  "adaptations": [
    {
      "item_id": "adap-0001",
      "position": {"start": 42, "end": 67},
      "original": "original text",
      "adapted": "adapted text",
      "tier": "T1",
      "category": "cultural_reference"
    }
  ]
}
```

The correction diff format (from `corrector.py` line 450):
```json
{
  "changes": [
    {
      "item_id": "corr-0001",
      "start_word": 10,
      "end_word": 12,
      "type": "spelling",
      "original": "orignal",
      "corrected": "original"
    }
  ]
}
```

Assembly functions to exercise:
- `_assemble_adaptation_review()` — character-level reverse-order splicing
- `_assemble_correction_review()` — word-level reconstruction

### H-3b: Video Normalization Failure Fallback Test

**File:** `tests/test_stock_images.py` (or new `tests/test_finalize_video.py` if the existing file is too large)

**New test:** `test_finalize_video_normalization_failure_creates_placeholder`

Setup:
- Create episode at `IMAGES_GENERATED` status
- Write a `candidates_manifest.json` (schema 3.1) with one video candidate selected:
  ```json
  {
    "schema_version": "3.1",
    "chapters": {
      "ch-001": {
        "candidates": [{
          "pexels_id": "12345",
          "selected": true,
          "asset_type": "video",
          "file_path": "candidates/ch-001/pexels_v_12345.mp4",
          "width": 1920,
          "height": 1080
        }]
      }
    }
  }
  ```
- Create the candidate video file (can be empty/minimal — normalization will be mocked to fail)
- Mock `normalize_video_clip()` to raise `RuntimeError("ffmpeg failed")`

Action:
- Call `finalize_selections(session, episode_id, settings)`

Assertions:
- No exception raised (graceful degradation)
- Image manifest entry for `ch-001` exists with `asset_type: "photo"` (placeholder fallback)
- Warning logged (check via `caplog`)
- Other chapters (if any photo candidates) finalized normally

#### Implementation Notes

The failure path in `stock_images.py` (lines 1282–1296) creates a placeholder when normalization fails. The test needs to verify:
1. The `try/except` around `normalize_video_clip()` catches the error
2. A placeholder image entry is written (not a video entry)
3. The manifest is still valid and complete

Mock target: `btcedu.core.stock_images.normalize_video_clip` (the imported name in stock_images module).

### Test Count

- H-3a: 3 new tests
- H-3b: 1 new test
- **Total: 4 new tests** (857 total, up from 853)

### Acceptance Criteria

- [ ] `test_apply_adaptation_creates_sidecar` passes — sidecar written with correct content
- [ ] `test_apply_correction_creates_sidecar` passes — sidecar written with correct content
- [ ] `test_apply_on_non_actionable_review_returns_409` passes
- [ ] `test_finalize_video_normalization_failure_creates_placeholder` passes — placeholder created, no crash
- [ ] All 853 existing tests still pass (no regressions)
- [ ] Total test count: 857

---

## Validation Criteria (Post-Implementation)

After all three work items are implemented, validate:

1. **H-1 validation**: Run `btcedu smoke-test-video` on the Raspberry Pi. Verify exit code 0, output video properties correct. Run `pytest tests/test_ffmpeg_smoke.py` — 3 tests pass.

2. **H-2 validation**: Run `btcedu imagegen --episode-id <test-ep> --dry-run`. Verify:
   - `PromptVersion` record with name `"intent_extract"` exists in DB
   - `ContentArtifact` record links to the prompt version
   - `ImageGenResult` includes `intent_extraction_cost_usd` field
   - Run `pytest tests/test_stock_ranking.py` — all tests pass including 2 new ones.

3. **H-3 validation**: Run `pytest tests/test_review_item_api.py tests/test_finalize_video.py` (or equivalent). All 4 new tests pass. Run full suite — 857 tests, 0 failures.

4. **Regression check**: Full `pytest` run. No test count decrease. No new warnings.

---

## Implementation Order

1. **H-3** (missing tests) — fastest, lowest risk, immediately improves confidence
2. **H-1** (video smoke-test) — independent of other items, validates Pi hardware
3. **H-2** (PromptRegistry integration) — touches the most files, benefits from H-3's test coverage

Each item is independently committable and deployable.

---

## Out of Scope (Deferred to P2)

- `rank_candidates()` PromptRegistry integration (same pattern as H-2, lower priority)
- JS unit tests for `formatDuration()` and `renderPipelineStepper()` (Phase 2 nice-to-have)
- `COST_LIMIT` dedicated test (Phase 2 nice-to-have, covered by existing `or` branch)
- `_select_video_file()` empty list edge case test (Phase 4 nice-to-have)
- Duplicate index cleanup on `ReviewItemDecision.review_task_id` (cosmetic)
