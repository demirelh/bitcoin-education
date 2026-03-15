# Test Failure Triage Report

**Date:** 2026-03-15
**Before:** 706 passed, 5 failed
**After:** 711 passed, 0 failed
**Net new tests:** +5 (from E2E smoke test phase)

## Failure Summary

| # | Test | File | Classification | Root Cause | Fix |
|---|------|------|---------------|------------|-----|
| 1 | `TestRunEpisodePipeline::test_skips_completed_stages` | `test_pipeline.py` | Environment-dependent | `.env` contamination | Add `pipeline_version=1` to `_make_settings()` |
| 2 | `TestRetryEpisode::test_retries_from_failed_stage` | `test_pipeline.py` | Environment-dependent | `.env` contamination | Same as #1 |
| 3 | `TestResolvePipelinePlan::test_stage_callback_invoked` | `test_pipeline.py` | Environment-dependent | `.env` contamination | Same as #1 |
| 4 | `test_chapterize_script_success` | `test_chapterizer.py` | Outdated test | Mock data incompatible with `_fix_chapter_data()` | Use realistic narration word counts |
| 5 | `test_chapterize_script_force` | `test_chapterizer.py` | Outdated test | Mock data incompatible with `_fix_chapter_data()` | Use realistic narration word counts |

## Detailed Analysis

### Failures 1-3: Pipeline Tests — `.env` Contamination

**Symptom:** Tests expecting v1 pipeline behavior (e.g., CHUNKED → generate stage) instead got v2 stages, causing assertion failures on stage names and status transitions.

**Root cause:** The `.env` file in the project root contains `PIPELINE_VERSION=2`. Pydantic `BaseSettings` reads `.env` automatically. The test helper `_make_settings()` in `test_pipeline.py` creates a `Settings()` object without explicitly setting `pipeline_version`, so it inherits `PIPELINE_VERSION=2` from `.env`. All three tests assume v1 pipeline stages.

**Fix:** Added `pipeline_version=1` to the `_make_settings()` constructor (`test_pipeline.py:36`). This is a one-line change that explicitly isolates the test from the environment.

**Classification: Environment-dependent.** These tests passed before `PIPELINE_VERSION=2` was set in `.env` (which happened as part of v2 pipeline activation). They are not flaky or obsolete — they correctly test v1 behavior but lacked environment isolation.

### Failures 4-5: Chapterizer Tests — Mock Data vs `_fix_chapter_data()`

**Symptom:**
- `test_chapterize_script_success`: `result.estimated_duration_seconds` was `2` instead of expected `120`.
- `test_chapterize_script_force`: Pydantic `ValidationError` — `estimated_duration_seconds` value `0` fails `ge=1` constraint.

**Root cause:** The `_fix_chapter_data()` function (chapterizer.py:662) recalculates `estimated_duration_seconds` from actual word counts in narration text, overriding whatever the LLM returned. The mock narration texts were unrealistically short:

| Test | Mock narration text | Words | Computed duration |
|------|-------------------|-------|-------------------|
| success (ch01) | "Merhaba arkadaşlar." | 2 | round(2/150×60) = 1s |
| success (ch02) | "Bitcoin nedir?" | 2 | round(2/150×60) = 1s |
| force (ch01) | "Test." | 1 | round(1/150×60) = 0s |

For `test_chapterize_script_success`, 1+1=2 replaced the expected 120. For `test_chapterize_script_force`, 0 violated Pydantic's `ge=1` constraint on `Narration.estimated_duration_seconds`.

**Fix:** Changed mock narration text to 150 words each (`" ".join(["kelime"] * 150)`), which produces exactly 60 seconds per chapter via `_compute_duration_estimate()`. This matches the mock's `word_count: 150` and `estimated_duration_seconds: 60` values, and gives `total = 120` for the success test (2 × 60) and `total = 60` for the force test (1 × 60).

**Classification: Outdated test.** These tests were written before `_fix_chapter_data()` was added (or enhanced to recalculate durations from word counts). The production code is correct — it defensively recalculates durations because LLMs often return inconsistent numbers. The mock data simply needed to be consistent with this recalculation.

## Files Modified

| File | Change |
|------|--------|
| `tests/test_pipeline.py` | Added `pipeline_version=1` to `_make_settings()` |
| `tests/test_chapterizer.py` | Updated mock narration text to 150 words in `test_chapterize_script_success` (2 chapters) and `test_chapterize_script_force` (1 chapter) |

## Verification

```
$ python -m pytest tests/ -q
711 passed, 33 warnings in 63.87s
```

All 711 tests pass. No regressions introduced.
