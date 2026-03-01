# Sprint 9 Validation Output — Video Render Pipeline

**Sprint**: 9 (Phase 5, Part 1)
**Validated against**: `MASTERPLAN.md`, `sprint9-validation.md`, `sprint9-plan-output.md`, `sprint9-implement-output.md`
**Date**: 2026-03-01
**Validator**: Claude Opus 4.6

---

## 1) Verdict

**PASS WITH FIXES**

The Sprint 9 implementation delivers a working render pipeline that follows established project patterns. The core architecture (ffmpeg service layer, renderer orchestration, idempotency, dry-run, CLI, pipeline integration) is sound and consistent with prior sprints. However, one functional bug (`run_pending()` missing `TTS_DONE`) prevents automatic pipeline execution of the render stage, and there are several test coverage gaps that should be addressed.

---

## 2) Scope Check

### In-scope items implemented

| Item | Status | Location |
|------|--------|----------|
| `ffmpeg_service.py` with `create_segment`, `concatenate_segments`, `probe_media` | Done | `btcedu/services/ffmpeg_service.py` |
| `OverlaySpec`, `SegmentResult`, `ConcatResult`, `MediaInfo` dataclasses | Done | `btcedu/services/ffmpeg_service.py` |
| Font resolution with fallback (`find_font_path`) | Done | `btcedu/services/ffmpeg_service.py` |
| Drawtext escaping for Turkish characters | Done | `btcedu/services/ffmpeg_service.py` |
| `renderer.py` with `render_video()` orchestration | Done | `btcedu/core/renderer.py` |
| `RenderSegmentEntry`, `RenderResult` dataclasses | Done | `btcedu/core/renderer.py` |
| `OVERLAY_STYLES` default configuration | Done | `btcedu/core/renderer.py` |
| Render manifest + provenance JSON output | Done | `data/outputs/{ep}/render/` |
| Content hash idempotency (SHA-256) | Done | `btcedu/core/renderer.py` |
| Stale marker detection | Done | `_is_render_current()` checks `.stale` |
| H.264/AAC output, 1920x1080, yuv420p | Done | ffmpeg command in `create_segment()` |
| Scale+pad filter (preserves aspect ratio, black bars) | Done | `_build_scale_pad_filter()` |
| Dry-run support (placeholder files) | Done | Both service and renderer |
| `PipelineRun` + `ContentArtifact` records | Done | `render_video()` |
| `MediaAsset` record for draft video | Done | `_create_media_asset_record()` |
| Config: 8 render fields with defaults | Done | `btcedu/config.py` |
| CLI: `btcedu render --episode-id --force --dry-run` | Done | `btcedu/cli.py` |
| Pipeline: `("render", TTS_DONE)` in `_V2_STAGES` | Done | `btcedu/core/pipeline.py` |
| `_run_stage()` render branch | Done | `btcedu/core/pipeline.py` |
| Web jobs: `_do_render()` handler | Done | `btcedu/web/jobs.py` |
| `.env.example` render fields | Done | `.env.example` |
| No new migrations required | Correct | Uses existing tables |

### Out-of-scope items correctly deferred

| Item | Confirmed deferred |
|------|--------------------|
| Transitions (fade, dissolve) | Yes — cut-only |
| Review Gate 3 | Yes — not implemented |
| Dashboard video preview/player | Yes — no web API endpoints |
| Dashboard render trigger button | Yes — no API route |
| Background music / audio mixing | Yes |
| Intro/outro templates | Yes |
| Thumbnail generation | Yes |
| Dynamic font sizing | Yes |
| Watermark/branding overlays | Yes |

### Out-of-scope changes detected

**None.** No scope creep detected. The implementation stays tightly within Sprint 9 boundaries. No v1 pipeline code was modified. No unrelated refactors were introduced.

---

## 3) Correctness Review

### Key components reviewed

**`btcedu/services/ffmpeg_service.py`** — Well-structured service layer:
- Filter chain correctly scales/pads images to target resolution before overlays
- Drawtext filter handles Turkish characters (pass-through) and escapes colons, quotes, backslashes
- Concat demuxer with `-c copy` avoids re-encoding
- `_run_ffmpeg()` properly captures stderr and handles timeout/missing-binary
- `probe_media()` extracts duration, resolution, and codec info from ffprobe JSON

**`btcedu/core/renderer.py`** — Follows established stage pattern (consistent with `tts.py`, `image_generator.py`):
- Correct `render_video()` signature: `(session, episode_id, settings, force)`
- Pre-condition checks: missing episode, v1 pipeline rejection, wrong status
- Idempotency via content hash + provenance file + stale marker detection
- Per-chapter segment rendering with graceful skip on missing media (log warning + continue)
- Concatenation into `draft.mp4`
- PipelineRun lifecycle (RUNNING → SUCCESS/FAILED)
- ContentArtifact and MediaAsset records created
- Status transition: `TTS_DONE` → `RENDERED`

**`btcedu/core/pipeline.py`** — Render stage integration:
- `_V2_STAGES` includes `("render", EpisodeStatus.TTS_DONE)` — correct
- `_STATUS_ORDER` has `RENDERED: 16` between `TTS_DONE: 15` and `APPROVED: 17` — correct
- `_run_stage()` render branch returns proper `StageResult` with segment/duration/size detail
- Cost aggregation correctly excludes `"render"` (no API cost)

**`btcedu/cli.py`** — Standard CLI command with `--episode-id`, `--force`, `--dry-run`

**`btcedu/web/jobs.py`** — `_do_render()` dispatches correctly

### Risks / Defects

| ID | Severity | Description |
|----|----------|-------------|
| D1 | **HIGH** | `run_pending()` status filter (line 587-600) does not include `EpisodeStatus.TTS_DONE`. Episodes at `TTS_DONE` status will **not** be picked up for automatic pipeline execution. The render stage works via CLI and direct `run_episode_pipeline()` calls, but `run_pending()` and `run_latest()` (which calls `run_pending`) will skip them. This is a functional bug. |
| D2 | **LOW** | `_do_render()` in `jobs.py` does not forward `job.dry_run` to `settings.dry_run`, unlike `_do_generate()`. This means web-triggered render jobs always use the global setting. Currently low-impact since no web API endpoint triggers render jobs, but will matter in Sprint 10 when dashboard integration is added. |
| D3 | **LOW** | No input validation on `render_resolution` config format. `create_segment()` does `width, height = resolution.split("x")` which produces an unhelpful error on malformed input. Low-risk since the default is correct and only advanced users would change it. |
| D4 | **INFO** | No per-chapter partial recovery — every non-skipped run re-renders all chapters. This is acceptable for Sprint 9 (ffmpeg is fast for individual segments), but may be worth adding in Sprint 10 for long episodes. |
| D5 | **INFO** | No `--chapter` CLI option for targeted re-rendering. Acceptable omission; the plan did not specify single-chapter re-render support. |

---

## 4) Test Review

### Coverage present

**`tests/test_ffmpeg_service.py`** — 18 tests:
- Utility functions: `find_font_path`, `_escape_drawtext` (plain, special chars, Turkish), `_build_drawtext_filter` (2 positions)
- `get_ffmpeg_version`: success + not-found
- `create_segment`: missing image, missing audio, dry-run, with overlays (mocked `_run_ffmpeg`)
- `concatenate_segments`: empty list, missing segment, dry-run
- `probe_media`: success, video-only, missing file

**`tests/test_renderer.py`** — 15 tests:
- Guard checks: missing episode, v1 pipeline, wrong status, missing inputs
- Dry-run render (2 chapters, verifies segment count and status transition)
- Idempotency: 4 tests for `_is_render_current()` (no files, stale marker, hash mismatch, all good)
- Content hash: determinism + sensitivity to changes
- Overlay conversion: `_chapter_to_overlay_specs`
- Media resolution: `_resolve_chapter_media` (happy path + missing image)
- Force re-render: starts at `RENDERED` status, verifies force bypass
- **Total: 33 tests across both files**

### Missing or weak tests

| Gap | Severity | Description |
|-----|----------|-------------|
| T1 | **MEDIUM** | No non-dry-run `render_video()` test with mocked ffmpeg service. All integration tests use `dry_run=True`. The actual render loop calling `create_segment()` + `concatenate_segments()` is never exercised, even with mocks. |
| T2 | **MEDIUM** | No test for `render_video()` error/rollback path. When all chapters are skipped (all media missing) → `RuntimeError("No segments were rendered")`, the except block should set `PipelineRun.status = FAILED` and `episode.error_message`. Untested. |
| T3 | **LOW** | No test for ffmpeg failure (non-zero returncode → RuntimeError) in `create_segment()` or `concatenate_segments()`. |
| T4 | **LOW** | No test verifying `PipelineRun`, `ContentArtifact`, or `MediaAsset` DB records are created with correct values after a render. |
| T5 | **LOW** | No test for render manifest / provenance JSON content. `test_render_video_dry_run` checks files exist but doesn't validate their JSON structure. |
| T6 | **LOW** | `test_compute_render_content_hash` doesn't verify sensitivity to image manifest or TTS manifest changes (only overlay text changes). |
| T7 | **LOW** | `_resolve_chapter_media` missing audio case is untested (only missing image is covered). |

### Suggested additions

1. **Non-dry-run integration test** (addresses T1, T4): Mock `btcedu.services.ffmpeg_service.create_segment` and `concatenate_segments` at module level in renderer, verify `SegmentResult`/`ConcatResult` flow, check `PipelineRun.status == SUCCESS`, `ContentArtifact` exists, `MediaAsset` with `asset_type="video"`.

2. **Error rollback test** (addresses T2): Create a scenario where all chapters have missing media → expect `RuntimeError`, verify `PipelineRun.status == "failed"` and `episode.error_message` is set.

3. **ffmpeg failure test** (addresses T3): Mock `_run_ffmpeg` to return `(1, "error message")` → verify `RuntimeError` is raised from `create_segment()`.

---

## 5) Backward Compatibility Check

| Check | Result |
|-------|--------|
| v1 pipeline code untouched | PASS — no changes to `_V1_STAGES`, `_STAGES` alias, or v1 stage functions |
| v1 episodes rejected by renderer | PASS — `render_video()` raises `ValueError` for `pipeline_version != 2`, covered by test |
| `run_pending()` still picks up v1 statuses | PASS — `NEW`, `DOWNLOADED`, `TRANSCRIBED`, `CHUNKED`, `GENERATED` still in filter |
| `_STATUS_ORDER` v1 values unchanged | PASS — values 0-7 remain the same |
| No model schema changes | PASS — no migrations, no column changes |
| Existing tests pass | PASS — 509 tests passing |
| `EpisodeStatus` enum unchanged | PASS — `RENDERED` was already defined in Sprint 1 |

**v1 pipeline risk: NONE.** Sprint 9 is additive only.

---

## 6) Required Fixes Before Commit

### Fix 1: Add `TTS_DONE` to `run_pending()` episode filter

**File**: `btcedu/core/pipeline.py`, line ~599
**Issue**: D1 — Episodes at `TTS_DONE` are not picked up for automatic pipeline execution
**Fix**: Add `EpisodeStatus.TTS_DONE` to the `Episode.status.in_()` filter list in `run_pending()`:

```python
                    EpisodeStatus.IMAGES_GENERATED,
                    EpisodeStatus.TTS_DONE,  # Sprint 9: render stage
                ]
```

Also update the docstring (line 572-573) to include `TTS_DONE` in the listed statuses.

### Fix 2: Add non-dry-run integration test for `render_video()`

**File**: `tests/test_renderer.py`
**Issue**: T1 — Core render path is untested
**Fix**: Add a test that mocks `btcedu.services.ffmpeg_service.create_segment` and `concatenate_segments` (patched in `btcedu.core.renderer`), runs `render_video()` with `dry_run=False`, and verifies:
- `result.segment_count` matches chapter count
- `result.skipped` is `False`
- `episode.status == RENDERED`
- A `PipelineRun` record exists with `status="success"`
- A `ContentArtifact` record exists with `artifact_type="render_manifest"`
- A `MediaAsset` record exists with `asset_type="video"`

### Fix 3: Add error rollback test

**File**: `tests/test_renderer.py`
**Issue**: T2 — Error recovery path is untested
**Fix**: Add a test where chapters.json references chapters but all images/audio are missing. Verify `RuntimeError("No segments were rendered")` is raised, `PipelineRun.status == "failed"`, and `episode.error_message` is set.

---

## 7) Nice-to-Have Improvements (optional)

1. **Forward `job.dry_run` in `_do_render()`** (D2): Add `settings.dry_run = job.dry_run` before calling `render_video()` (restore afterward). Low-priority since no web endpoint exists yet.

2. **Validate `render_resolution` format** (D3): Add a Pydantic validator on `render_resolution` in `config.py` to ensure it matches `\d+x\d+`.

3. **Test content hash sensitivity to manifest changes** (T6): Extend `test_compute_render_content_hash` to verify that changing an image file path or TTS duration changes the hash.

4. **Test `_resolve_chapter_media` missing audio** (T7): Add a test case where a chapter's audio entry is missing from the TTS manifest.

5. **Test ffmpeg non-zero exit** (T3): Add `test_create_segment_ffmpeg_failure` and `test_concatenate_segments_ffmpeg_failure` to `test_ffmpeg_service.py`.

6. **Test manifest/provenance JSON content** (T5): Verify the JSON structure of `render_manifest.json` and `render_provenance.json` in a dry-run test (check `segment_count`, `total_duration_seconds`, `content_hash`, `ffmpeg_version`).
