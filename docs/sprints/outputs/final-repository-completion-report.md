# Final Repository Completion Report

**Date**: 2025-07-16
**Scope**: Close all gaps identified in `docs/sprints/outputs/missing-implementations-plan.md`
**Baseline**: 17 gaps (1 P0, 6 P1, 10 P2) — all 11 sprints already merged to `main`

---

## Summary

All P0 and P1 items from the audit have been implemented. One P2 item (YouTube optional deps) was also completed because it directly impacts deployment reliability. The remaining 9 P2 items are deferred — they are polish/monitoring improvements that do not affect pipeline functionality.

**Test suite**: 629 tests, all passing
**CLI commands**: 34 (was 30)
**Files modified**: 11 source files, 4 test files, 1 new file

---

## Implemented Items

### P0-1: Corrector Cascade Invalidation ✅

**Problem**: Re-correction did not mark `transcript.tr.txt` as stale, so downstream translation would not re-run.

**Fix**: Added `_mark_downstream_stale()` in `btcedu/core/corrector.py` (~15 lines). After writing a corrected file, creates a `.stale` marker on `transcript.tr.txt` if it exists. Pattern matches `translator.py`'s cascade approach.

**Tests**: 2 new tests in `tests/test_corrector.py`:
- `test_marks_downstream_translation_stale`
- `test_no_stale_marker_when_no_translation`

---

### P1-1: Prompt CLI Commands ✅

**Problem**: `PromptRegistry` had `get_history()` and `promote_to_default()` but no CLI exposure.

**Fix**: Added `prompt` CLI group with two subcommands in `btcedu/cli.py`:
- `btcedu prompt list [--name NAME]` — tabular display of all prompt versions (ID, Name, Version, Default, Hash, Model, Created)
- `btcedu prompt promote VERSION_ID` — promote a prompt version to default

---

### P1-2: .env.example Completeness ✅

**Problem**: `.env.example` was missing 14 config fields from Sprint 7-11.

**Fix**: Added 3 new sections to `.env.example`:
- **Image Generation** (6 fields): IMAGE_GEN_PROVIDER, MODEL, SIZE, QUALITY, STYLE_PREFIX
- **YouTube Publishing** (6 fields): CLIENT_SECRETS_PATH, CREDENTIALS_PATH, DEFAULT_PRIVACY, UPLOAD_CHUNK_SIZE_MB, CATEGORY_ID, DEFAULT_LANGUAGE
- **Pipeline Settings** (3 fields): PIPELINE_VERSION, MAX_EPISODE_COST_USD, MAX_RETRIES

---

### P1-3: Dashboard Image Gallery ✅

**Problem**: No way to view generated images in the web dashboard.

**Fix**: Added 2 API endpoints in `btcedu/web/api.py`:
- `GET /api/episodes/<id>/images` — returns manifest JSON
- `GET /api/episodes/<id>/images/<filename>` — serves PNG files (with path security)

Added "Images" tab in `btcedu/web/static/app.js` with grid layout showing:
- Thumbnail per chapter
- Chapter ID and generation method badge
- Prompt preview (click to expand)
- Click-to-open full-size image

---

### P1-4: V2 Pipeline E2E Test ✅

**Problem**: No test exercised the full v2 pipeline flow through review gates.

**Fix**: Added `TestV2PipelineE2E` class in `tests/test_pipeline.py` with 5 tests:
- `test_pauses_at_review_gate_1` — verifies pipeline stops at first review gate
- `test_resumes_after_gate_1_approval` — approval lets pipeline continue to gate 2
- `test_full_pipeline_new_to_published` — full 4-run cycle (NEW → gate1 → gate2 → gate3 → PUBLISHED)
- `test_v2_plan_shows_all_13_stages` — plan resolution for v2 returns all 13 stages
- `test_v2_cost_accumulation` — cost from stage results is accumulated correctly

The E2E test uses a mock side-effect that advances episode status like real stages, with review gates delegating to actual `_run_stage()` for authentic gate behavior.

---

### P1-5: review_history.json ✅

**Problem**: Review decisions were only in the database, no file-level audit trail.

**Fix**: Added `_write_review_history()` helper in `btcedu/core/reviewer.py` (~40 lines). Called from `approve_review()`, `reject_review()`, and `request_changes()`. Each entry appended to `data/outputs/{ep_id}/review/review_history.json` with:
- review_task_id, episode_id, stage, decision, notes, decided_at, artifact_hash, task_status

---

### P1-6: Auto-Approve for Minor Corrections ✅

**Problem**: All corrections required manual approval, even trivial punctuation fixes.

**Fix**: Added in `btcedu/core/reviewer.py`:
- `_is_punctuation_only(original, corrected)` — strips punctuation and compares word tokens
- `_is_minor_correction(diff_path)` — returns True if <5 changes and all punctuation-only
- Auto-approve check at end of `create_review_task()` for stage="correct"

**Tests**: 15 new tests in `tests/test_reviewer.py`:
- `TestIsPunctuationOnly` (5 tests)
- `TestIsMinorCorrection` (7 tests)
- `TestAutoApproveMinorCorrections` (3 tests)

---

### P2-10: YouTube Optional Dependencies ✅

**Problem**: YouTube deps were commented out in `pyproject.toml`. Required manual pip install.

**Fix**: Created `[youtube]` optional extra in `pyproject.toml`:
```toml
youtube = [
    "google-api-python-client>=2.0.0",
    "google-auth-httplib2>=0.2.0",
    "google-auth-oauthlib>=1.0.0",
]
```
Install with: `pip install -e ".[youtube]"`

---

### Image Generator Test Expansion ✅

**Problem**: Only 7 tests, all for utility functions. No integration tests for `generate_images()`.

**Fix**: Expanded `tests/test_image_generator.py` from 7 to 33 tests:
- `TestNeedsGeneration` (6 tests)
- `TestSplitPrompt` (3 tests)
- `TestComputeChaptersContentHash` (3 tests)
- `TestIsImageGenCurrent` (7 tests) — idempotency, stale markers, missing files
- `TestMarkDownstreamStale` (2 tests)
- `TestDallE3Service` (4 tests)
- `TestImageGenRequest` (1 test)
- `TestImageEntry` (1 test)
- `TestImageGenResult` (2 tests)
- `TestGenerateImagesValidation` (4 tests) — missing episode, v1 rejection, wrong status, missing chapters.json

---

## Infrastructure Updates

### run.sh Updates ✅

Improvements to the deployment script:

1. **ffmpeg check** — warns if ffmpeg not installed (needed for renderer)
2. **YouTube deps auto-install** — installs `.[youtube]` when `data/client_secret.json` exists
3. **init-db before migrate** — ensures tables exist on fresh deploys
4. **Health check** — hits `/api/health` after service restart (requires curl)

### CLAUDE.md Updates ✅

- Test count updated: 629 (was 581)
- CLI commands: 34 with the new `prompt` group
- Added `prompt list` and `prompt promote` to CLI table
- YouTube deps: updated from "commented out" to proper `[youtube]` optional extra
- Added Key Design Decisions 8-10: auto-approve, review_history.json, corrector cascade
- Removed outdated gotcha about "YouTube deps commented out"

---

## Deferred P2 Items

These items are polish/monitoring improvements that do not affect pipeline correctness or deployment:

| ID | Item | Reason for Deferral |
|----|------|---------------------|
| P2-1 | Centralized `STAGE_DEPENDENCIES` | Works fine with per-module stale marking |
| P2-2 | `render/timeline.json` (EDL) | No consumer currently needs it |
| P2-3 | Dashboard prompt comparison view | ~200 lines; UX improvement only |
| P2-4 | `ContentArtifact.prompt_version_id` FK | Schema migration needed; linkage exists via hash |
| P2-5 | Dashboard TTS playback | Audio can be accessed via file endpoints |
| P2-6 | Dashboard video review page | Review works via CLI; dashboard has basic support |
| P2-7 | Review dashboard redesign | Functional as-is |
| P2-8 | Pipeline journal structured output | Human-readable format is sufficient |
| P2-9 | Cost dashboard graphs | Cost CLI command provides data |

---

## Verification Commands

```bash
# Run full test suite
.venv/bin/python -m pytest -q

# Count tests
.venv/bin/python -m pytest --co -q 2>&1 | tail -1

# List CLI commands
.venv/bin/btcedu --help

# Check prompt commands
.venv/bin/btcedu prompt --help

# Lint
.venv/bin/ruff check btcedu/ tests/

# Deploy
./run.sh
```

---

## Files Modified

### Source Code
- `btcedu/core/corrector.py` — cascade invalidation (~15 lines)
- `btcedu/core/reviewer.py` — auto-approve + review_history.json (~120 lines)
- `btcedu/core/pipeline.py` — no changes (test coverage added)
- `btcedu/cli.py` — prompt group + list/promote commands (~70 lines)
- `btcedu/web/api.py` — image gallery endpoints (~50 lines)
- `btcedu/web/static/app.js` — Images tab + gallery panel (~60 lines)
- `.env.example` — 3 new sections (~20 lines)
- `pyproject.toml` — YouTube optional extra
- `run.sh` — ffmpeg check, YouTube deps, init-db, health check
- `CLAUDE.md` — updated test count, CLI count, features, gotchas

### Test Files
- `tests/test_corrector.py` — +2 tests (cascade stale)
- `tests/test_reviewer.py` — +15 tests (auto-approve)
- `tests/test_pipeline.py` — +5 tests (v2 E2E)
- `tests/test_image_generator.py` — expanded 7→33 tests
