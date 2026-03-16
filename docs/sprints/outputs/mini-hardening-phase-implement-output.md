# Mini Hardening Phase — Implementation Output

**Date:** 2026-03-16
**Implementor:** Claude Sonnet 4.6 (automated)
**Plan source:** `docs/sprints/outputs/mini-hardening-phase-plan.md`
**Tests baseline:** 853 passing → **867 passing** (14 new tests, 0 regressions)

---

## Summary Verdict

**PASS — all three work items implemented and tested.**

All new tests pass. The full 867-test suite runs clean with 0 failures.

---

## H-1: Phase 4 Video Smoke-Test Support

### What was implemented

**`btcedu/services/ffmpeg_service.py`** — two new helper functions:

- `generate_test_video(output_path, duration, resolution, fps, dry_run)` — generates a synthetic H.264/yuv420p MP4 via `ffmpeg -f lavfi -i testsrc2` (no external input files needed)
- `generate_silent_audio(output_path, duration, sample_rate, dry_run)` — generates a silent AAC audio file via `ffmpeg -f lavfi -i anullsrc` (no external input files needed)

Both follow the existing `dry_run` pattern: build the command but skip execution, touch an empty output file.

**`btcedu/cli.py`** — new `smoke-test-video` command:

```
btcedu smoke-test-video [--resolution WxH] [--keep]
```

Runs 4 sequential steps:
1. `generate_test_video()` — synthetic test video via testsrc2
2. `normalize_video_clip()` — scale/pad/yuv420p normalization
3. `generate_silent_audio()` — silent AAC audio via anullsrc
4. `create_video_segment()` — stream_loop + TTS audio + overlay pipeline

Validates output with `probe_media()` (codec, duration, stream count). Prints per-step PASS/FAIL. Exit code 0 on success, 1 on any failure. `--keep` retains temp files for manual inspection.

**`tests/test_ffmpeg_smoke.py`** — 7 new dry-run tests in 3 classes:

| Class | Tests |
|-------|-------|
| `TestGenerateTestVideoCommand` | testsrc2 in command, libx264+yuv420p in command, dry_run touches file |
| `TestGenerateSilentAudioCommand` | anullsrc in command, aac in command, dry_run touches file |
| `TestSmokeTestIntegrationDryRun` | full 4-step sequence with dry_run=True, stream_loop + 1:a verified |

### Manual verification steps (Raspberry Pi)

```bash
# Run real ffmpeg execution (requires working Pi ffmpeg install)
btcedu smoke-test-video --keep

# Expected: 4 PASS lines, "PASS All smoke-test steps passed."
# Check /tmp/btcedu_smoke_* directory for output files

# Inspect output segment
ffprobe -v quiet -print_format json -show_streams /tmp/btcedu_smoke_*/segment.mp4 \
  | python -c "import sys,json; d=json.load(sys.stdin); [print(s['codec_name'],s.get('pix_fmt','')) for s in d['streams']]"
# Expected: h264 yuv420p, aac
```

---

## H-2: PromptRegistry / LLM-Report Tracking for Intent Extraction

### What was implemented

**`btcedu/core/stock_images.py`** — added PromptRegistry registration at the start of `extract_chapter_intents()`:

```python
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry

# In extract_chapter_intents():
try:
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "intent_extract.md"
    if template_file.exists():
        registry.register_version("intent_extract", template_file, set_default=True)
except Exception as _reg_err:
    logger.debug("PromptRegistry registration skipped for intent_extract: %s", _reg_err)
```

Effect: every real (non-dry-run) call to `extract_chapter_intents()` now creates/updates a `PromptVersion` record for `"intent_extract"` in the DB. The prompt is now visible via `btcedu prompt list`.

The registration is wrapped in `try/except` so failures (e.g., missing table in legacy DB) are non-fatal.

**`btcedu/prompts/templates/intent_extract.md`** — no changes needed; the template already had proper YAML frontmatter and correct content.

**`tests/test_intent_extract_registry.py`** — 2 new tests (separate file to avoid the `autouse mock_extract_intents` fixture in `test_stock_ranking.py`):

| Test | Asserts |
|------|---------|
| `test_intent_extract_registers_prompt_version` | `PromptVersion(name="intent_extract")` exists in DB after call, `is_default=True` |
| `test_intent_extract_returns_cost` | `IntentResult.cost_usd` matches mock `call_claude` cost_usd |

### Deviations from plan

1. **`call_claude()` prompt_hash parameter**: The plan stated "pass `prompt_hash` to `call_claude()` (already supported)". The actual `call_claude()` signature does not accept a `prompt_hash` parameter (it's not in the function signature). The registration creates the `PromptVersion` record (the primary tracking value) but does not wire the hash into individual LLM calls. ContentArtifact linkage is deferred to P2.

2. **`ImageGenResult.intent_extraction_cost_usd` field**: The plan proposed adding this field to `ImageGenResult` in `image_generator.py`. However, `extract_chapter_intents()` is called from `stock_images.py` (Pexels pipeline), not from `image_generator.py` (DALL-E pipeline) — these are different code paths. Adding the field to `ImageGenResult` would not reflect intent extraction cost. The existing `IntentResult.cost_usd` already captures the cost; it is included in the total cost reported by `rank_candidates()`. The `ImageGenResult` is left unchanged.

### Manual verification steps

```bash
# Run imagegen with a v2 episode to trigger intent extraction
btcedu imagegen --episode-id YOUR_EP_ID --dry-run

# Check that prompt version was registered
btcedu prompt list
# Should show: intent_extract  1  ✓  ...  claude-sonnet-4-20250514
```

---

## H-3: Two Missing Integration Tests

### H-3a: Phase 5 Adaptation Apply API

**`tests/test_review_item_api.py`** — added:

1. `review_setup_adaptation` fixture: ADAPTED episode + ReviewTask(stage="adapt") + adaptation_diff.json (2 items: adap-0000 at chars [2:5], adap-0001 at chars [8:11]) + adapted script (`"A XXX B YYY C"`)

2. `TestApplyAdaptationAPI` class with 3 tests:

| Test | Coverage |
|------|----------|
| `test_apply_adaptation_creates_sidecar` | POST /apply returns 200; sidecar content correctly reflects REJECT (adap-0000 → "OOO") + ACCEPT (adap-0001 → "YYY"); expected sidecar = "A OOO B YYY C" |
| `test_apply_adaptation_sidecar_path` | Sidecar written to `outputs/ep002/review/script.adapted.reviewed.tr.md` |
| `test_apply_on_non_actionable_review_returns_400` | Approved task returns 400 |

### Deviation from plan

The plan stated `test_apply_on_non_actionable_review_returns_409`. The actual API returns **HTTP 400** (not 409) for non-actionable reviews — `_check_review_actionable()` returns a 400 response tuple. The test asserts `resp.status_code == 400`.

### H-3b: Phase 4 Video Normalization Failure Fallback

**`tests/test_finalize_video.py`** — new file, 2 tests:

| Test | Coverage |
|------|----------|
| `test_normalization_failure_creates_placeholder` | Mocks `btcedu.services.ffmpeg_service.normalize_video_clip` to raise `RuntimeError`; verifies `finalize_selections()` doesn't raise, `result.placeholder_count == 1`, `result.selected_count == 0`, warning is logged |
| `test_normalization_failure_manifest_written` | Same setup; verifies `manifest.json` is still written with `asset_type: "photo"` (placeholder, not video) |

**Note on mock target**: `normalize_video_clip` is lazily imported inside `finalize_selections()` (`from btcedu.services.ffmpeg_service import normalize_video_clip`). Patching `btcedu.core.stock_images.normalize_video_clip` fails (no module-level attribute). Correct target is `btcedu.services.ffmpeg_service.normalize_video_clip`.

---

## Files Changed

| File | Type | Change |
|------|------|--------|
| `btcedu/services/ffmpeg_service.py` | Modified | Added `generate_test_video()` and `generate_silent_audio()` |
| `btcedu/cli.py` | Modified | Added `smoke-test-video` command |
| `btcedu/core/stock_images.py` | Modified | Added PromptRegistry import + `register_version()` call in `extract_chapter_intents()` |
| `tests/test_ffmpeg_smoke.py` | New | 7 dry-run tests for H-1 helpers |
| `tests/test_finalize_video.py` | New | 2 tests for H-3b normalization failure fallback |
| `tests/test_intent_extract_registry.py` | New | 2 tests for H-2 PromptRegistry registration |
| `tests/test_review_item_api.py` | Modified | Added `review_setup_adaptation` fixture + `TestApplyAdaptationAPI` class (3 tests) |
| `tests/test_stock_ranking.py` | Modified | Removed H-2 tests (relocated to separate file); comment added |

---

## Test Count

| Metric | Value |
|--------|-------|
| Tests before | 853 |
| New tests added | 14 |
| Tests after | **867** |
| Failures | **0** |
| Regressions | **0** |

New test breakdown:
- H-1 (ffmpeg smoke): 7 tests in `test_ffmpeg_smoke.py`
- H-2 (PromptRegistry): 2 tests in `test_intent_extract_registry.py`
- H-3a (adaptation apply API): 3 tests in `test_review_item_api.py`
- H-3b (normalization fallback): 2 tests in `test_finalize_video.py`

---

## Acceptance Criteria Status

### H-1
- [x] `btcedu smoke-test-video` implemented with `--keep` and `--resolution` flags
- [x] Steps: generate_test_video → normalize_video_clip → generate_silent_audio → create_video_segment
- [x] ffprobe validation of output (codec, duration, streams)
- [x] 7 dry-run tests pass in CI (no ffmpeg required)
- [ ] **Manual**: Run on Raspberry Pi to validate ARM64 ffmpeg build (requires physical Pi execution)

### H-2
- [x] `extract_chapter_intents()` calls `PromptRegistry.register_version("intent_extract", ...)`
- [x] `PromptVersion` record created in DB on first call (verified by test)
- [x] `IntentResult.cost_usd` correctly captured from LLM response
- [x] 2 new registry integration tests pass
- [ ] **Deviation**: `prompt_hash` not wired into `call_claude()` (function doesn't accept that param)
- [ ] **Deviation**: `ImageGenResult.intent_extraction_cost_usd` field not added (wrong code path)

### H-3a
- [x] `test_apply_adaptation_creates_sidecar` — sidecar content verified character-precisely
- [x] `test_apply_adaptation_sidecar_path` — sidecar written to correct review/ path
- [x] `test_apply_on_non_actionable_review_returns_400` — non-actionable guard works (HTTP 400, not 409)

### H-3b
- [x] `test_normalization_failure_creates_placeholder` — no crash, placeholder created, warning logged
- [x] `test_normalization_failure_manifest_written` — manifest.json written with asset_type:photo
