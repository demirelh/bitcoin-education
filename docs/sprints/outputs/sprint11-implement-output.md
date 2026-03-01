# Sprint 11 — Implementation Output: YouTube Publishing + Safety Checks

> **Sprint**: 11 (Final)
> **Phase**: 6 — YouTube Publishing
> **Branch**: `claude/implement-sprint-10`
> **Date**: 2026-03-01

---

## Summary

Sprint 11 implements the **PUBLISH** stage — the final step of the v2 pipeline. An approved draft video is uploaded to YouTube with full metadata (title, description with chapter timestamps, tags), after passing 4 mandatory pre-publish safety checks. The implementation adds a YouTube service (OAuth2 + resumable upload), a publisher orchestration module, a `publish_jobs` database table, CLI commands, pipeline integration, and a dashboard publish UI.

All changes are strictly additive. The v1 pipeline and all previous v2 stages are untouched.

---

## Files Created (5)

### 1. `btcedu/core/publisher.py` (610 lines)

YouTube publishing orchestration module. Contains:

- **Data contracts**: `SafetyCheck` and `PublishResult` dataclasses
- **4 pre-publish safety checks**:
  1. `_check_approval_gate()` — Episode is APPROVED + render ReviewTask exists with status=APPROVED
  2. `_check_artifact_integrity()` — SHA-256 of artifact paths matches `artifact_hash` from approved ReviewTask
  3. `_check_metadata_completeness()` — Title, description, tags are non-empty
  4. `_check_cost_sanity()` — Total episode cost within `max_episode_cost_usd`
- `_run_all_safety_checks()` — Executes all 4 checks, logs results
- **Metadata construction**: `_build_youtube_metadata()` — loads chapters.json, builds title, description with YouTube chapter timestamps (starting at `0:00`), tags (base + episode-specific, enforcing 500-char limit)
- `_format_timestamp()` — Formats seconds as `M:SS` or `H:MM:SS`
- `_load_tts_durations()` — Loads actual TTS durations from manifest for accurate timestamps
- **Main function**: `publish_video(session, episode_id, settings, force, privacy)`:
  - Validates episode (v2, APPROVED)
  - Idempotency check (skip if `youtube_video_id` set, unless force)
  - Runs all 4 safety checks (aborts on failure)
  - Creates `PublishJob` record (pending → uploading → published/failed)
  - Uploads via YouTube service (or DryRunYouTubeService)
  - On success: updates Episode (`youtube_video_id`, `published_at_youtube`, status=PUBLISHED), records PipelineRun
  - On failure: PublishJob marked failed, episode stays APPROVED
  - Writes provenance JSON
- `_write_provenance()` — Writes `provenance/publish.json`
- `get_latest_publish_job()` — Query helper for web API

### 2. `btcedu/models/publish_job.py` (46 lines)

SQLAlchemy ORM model for the `publish_jobs` table:

- `PublishJobStatus` enum: `PENDING`, `UPLOADING`, `PUBLISHED`, `FAILED`
- `PublishJob(Base)` model with fields: `id`, `episode_id` (indexed), `status` (indexed), `youtube_video_id`, `youtube_url`, `metadata_snapshot` (JSON Text), `published_at`, `error_message`, `created_at`
- Uses `btcedu.db.Base` (consistent with other non-media models)

### 3. `btcedu/services/youtube_service.py` (437 lines)

YouTube Data API v3 wrapper:

- **Data contracts**: `YouTubeUploadRequest`, `YouTubeUploadResponse` dataclasses
- **Exception hierarchy**: `YouTubeAuthError`, `YouTubeUploadError`, `YouTubeQuotaError`
- **Protocol**: `YouTubeService` for dependency injection
- **`DryRunYouTubeService`**: Returns placeholder responses for dry-run and tests
- **`YouTubeDataAPIService`**: Real API implementation:
  - `_load_credentials()` — Loads OAuth2 credentials from file, auto-refreshes expired tokens
  - `_build_client()` — Returns authenticated YouTube API client
  - `upload_video()` — Resumable upload with `MediaFileUpload`, progress callbacks, chunk-based upload loop
  - `_execute_upload()` — Handles transient errors (500/502/503/504) with exponential backoff (up to 3 retries), quota errors (403 → `YouTubeQuotaError`), bad requests (400/401 → `YouTubeUploadError`)
  - `_upload_thumbnail()` — Optional thumbnail upload after video
- **OAuth2 helpers**:
  - `authenticate()` — Interactive flow via `InstalledAppFlow`, saves credentials
  - `check_token_status()` — Returns dict with `valid`, `expired`, `expiry`, `can_refresh`
- Constants: `YOUTUBE_UPLOAD_QUOTA_UNITS = 1600`, max title/description/tag limits

### 4. `tests/test_publisher.py` (445 lines)

33 tests covering:

- `TestFormatTimestamp` (6 tests): zero, seconds, minutes, hours, negative
- `TestCheckApprovalGate` (3 tests): pass, fail (wrong status), fail (no review task)
- `TestCheckArtifactIntegrity` (3 tests): pass (hash match), fail (hash mismatch/tampered), fail (no review task)
- `TestCheckMetadataCompleteness` (4 tests): pass, fail (empty title), fail (empty tags), fail (multiple missing)
- `TestCheckCostSanity` (3 tests): pass (within budget), fail (over budget), pass (no runs)
- `TestBuildYouTubeMetadata` (4 tests): chapters, timestamps start with 0:00, fallback title, base tags
- `TestPublishVideo` (8 tests): not found, v1 rejected, not approved, skip if published, dry-run, safety check failure, publish job on failure, provenance written
- `TestGetLatestPublishJob` (2 tests): none, returns most recent

### 5. `tests/test_youtube_service.py` (137 lines)

8 tests covering:

- `TestDryRunYouTubeService` (4 tests): response, progress callback, privacy preserved, long title
- `TestCheckTokenStatus` (3 tests): no file, valid JSON, corrupted file
- `TestYouTubeUploadRequest` (1 test): default privacy is unlisted

---

## Files Modified (12)

### 6. `btcedu/config.py` (+8 lines)

Added 6 YouTube configuration fields to `Settings`:
- `youtube_client_secrets_path` (default: `"data/client_secret.json"`)
- `youtube_credentials_path` (default: `"data/.youtube_credentials.json"`)
- `youtube_default_privacy` (default: `"unlisted"`)
- `youtube_upload_chunk_size_mb` (default: `10`)
- `youtube_category_id` (default: `"27"` — Education)
- `youtube_default_language` (default: `"tr"`)

### 7. `btcedu/migrations/__init__.py` (+49 lines)

Added `CreatePublishJobsTableMigration` (Migration 006):
- Creates `publish_jobs` table with all required columns
- Creates indexes on `episode_id` and `status`
- Check-before-act idempotency
- Appended to `MIGRATIONS` list

### 8. `btcedu/models/__init__.py` (+1 line)

Added import: `from btcedu.models.publish_job import PublishJob, PublishJobStatus`

### 9. `btcedu/core/pipeline.py` (+13 lines)

- Added `("publish", EpisodeStatus.APPROVED)` to `_V2_STAGES`
- Added `EpisodeStatus.APPROVED` to `run_pending()` and `run_latest()` status filters
- Added publish handler in `_run_stage()`: lazy-imports `publish_video`, returns `StageResult`

### 10. `btcedu/cli.py` (+102 lines)

Added 3 CLI commands:
- `btcedu publish <episode_ids>` — with `--force`, `--dry-run`, `--privacy` options
- `btcedu youtube-auth` — Interactive OAuth2 setup (opens browser)
- `btcedu youtube-status` — Check token validity, expiry, refresh capability

### 11. `btcedu/web/api.py` (+39 lines)

Added 2 API endpoints:
- `POST /episodes/<eid>/publish` — Trigger publish job via JobManager
- `GET /episodes/<eid>/publish-status` — Return latest PublishJob status + YouTube link
- Added `youtube_video_id` and `published_at_youtube` to `_episode_to_dict()`

### 12. `btcedu/web/jobs.py` (+42 lines)

- Added `Job.privacy` field
- Added `_do_publish()` method handling publish_video call with result tracking
- Added `"publish"` dispatch in `_execute()`

### 13. `btcedu/web/static/app.js` (+25 lines)

- Added `actions.publish()` function
- Added publish button in episode detail for APPROVED episodes
- Added publish status panel in video tab (YouTube link for published, upload button for unpublished)

### 14. `btcedu/web/static/styles.css` (+38 lines)

Added styles for: `.badge-approved`, `.badge-published`, `.btn-success`, `.publish-panel`, and all v2 status badge colors

### 15. `pyproject.toml` (+4 lines)

Added Google API dependencies as commented optional dependencies:
- `google-api-python-client`
- `google-auth-httplib2`
- `google-auth-oauthlib`

### 16. `tests/test_sprint1_migrations.py` (+2/-2 lines)

Fixed migration count assertion: `len(pending) == 4` → `len(pending) == 5` to account for Migration 006.

### 17. `uv.lock` (+891 lines)

Lock file updated with Google API dependency resolution.

---

## Test Results

```
581 passed, 33 warnings in 10.20s
```

- **Sprint 11 tests**: 41 (33 in test_publisher.py + 8 in test_youtube_service.py)
- **All existing tests**: Unbroken (540 pre-Sprint 11 → 581 total)
- **No failures, no errors**

---

## Bugs Found & Fixed During Validation

### Bug 1: `youtube-status` CLI KeyError

**File**: `btcedu/cli.py` (youtube-status command)
**Issue**: CLI referenced `status['exists']` but `check_token_status()` returns `valid`, `expired`, `expiry`, `can_refresh` — no `exists` key. Would crash with `KeyError('exists')` at runtime.
**Fix**: Replaced `status['exists']` with derived `has_creds` based on error message check. Added `expired` and `can_refresh` fields to output.

### Bug 2: Migration count test assertion

**File**: `tests/test_sprint1_migrations.py`
**Issue**: `test_all_migrations_run_sequentially` hardcoded `len(pending) == 4` but Migration 006 was added, making it 5.
**Fix**: Updated assertion to `len(pending) == 5`.

---

## Deferred / Not Implemented (by design)

1. Live streaming support
2. YouTube analytics / comment management
3. Multi-channel publishing
4. Scheduled publishing automation
5. Video re-upload / update mechanism
6. Automatic privacy changes (unlisted → public)
7. Subtitle/caption upload
8. Playlist management
9. End-to-end test with real YouTube API (tests use mocked service)

---

## Complete v2 Pipeline Flow (Capstone)

```
[DETECT]     → NEW
[DOWNLOAD]   → DOWNLOADED
[TRANSCRIBE] → TRANSCRIBED
[CORRECT]    → CORRECTED
[RG1]        → (review gate: approve/reject correction)
[TRANSLATE]  → TRANSLATED
[ADAPT]      → ADAPTED
[RG2]        → (review gate: approve/reject adaptation)
[CHAPTERIZE] → CHAPTERIZED
[IMAGEGEN]   → IMAGES_GENERATED
[TTS]        → TTS_DONE
[RENDER]     → RENDERED
[RG3]        → APPROVED (review gate: approve/reject video)
[PUBLISH]    → PUBLISHED (YouTube upload with safety checks)
```

**12 stages + 3 review gates = 15 pipeline steps.**
