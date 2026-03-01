# Sprint 11 — Validation Output: YouTube Publishing + Safety Checks

> **Sprint**: 11 (Final)
> **Phase**: 6 — YouTube Publishing
> **Validated on branch**: `main` (post-merge)
> **Sources**: `MASTERPLAN.md` §4-Phase6, §5I, §7.3–7.4; `docs/sprints/sprint11-validation.md`; `docs/sprints/outputs/sprint11-plan-output.md`
> **Date**: 2026-03-01
> **Test result**: 581 passed, 0 failed

---

# 1) Verdict

**PASS** (with 2 bugs found and fixed during validation)

---

# 2) Scope Check

- **In-scope items implemented**: YouTube service with OAuth2 + resumable upload, publisher module with 4 safety checks, `publish_jobs` table (Migration 006), 3 CLI commands (`publish`, `youtube-auth`, `youtube-status`), pipeline integration (APPROVED → PUBLISHED), dashboard publish button + status display, provenance, idempotency, 41 tests.
- **Out-of-scope changes detected**: None. All changes are strictly additive. No existing stages, models, or tests were modified beyond pipeline wiring and the migration count test fix.

---

# 3) Review Checklist

## 1. `publish_jobs` Table & Model

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1.1 | Migration creates `publish_jobs` table with correct schema per §7.3 | **PASS** | [Migration 006](btcedu/migrations/__init__.py#L408-L451) |
| 1.2 | Table has required fields | **PASS** | id, episode_id, status, youtube_video_id, youtube_url, metadata_snapshot (Text/JSON), published_at, error_message, created_at |
| 1.3 | Indexes on episode_id and status | **PASS** | `idx_publish_jobs_episode`, `idx_publish_jobs_status` |
| 1.4 | `PublishJob` SQLAlchemy model exists and is registered | **PASS** | [publish_job.py](btcedu/models/publish_job.py), imported in [models/__init__.py](btcedu/models/__init__.py#L6) |
| 1.5 | Migration runs cleanly (additive) | **PASS** | Check-before-act idempotency, tested |

## 2. YouTube Service

| # | Item | Status | Notes |
|---|------|--------|-------|
| 2.1 | `btcedu/services/youtube_service.py` exists | **PASS** | 437 lines |
| 2.2a | Loads credentials from stored file | **PASS** | [_load_credentials()](btcedu/services/youtube_service.py#L147-L182) |
| 2.2b | Refreshes expired tokens automatically | **PASS** | Uses `creds.refresh(Request())`, saves refreshed token |
| 2.2c | Falls back to interactive flow if no credentials | **PASS** | [authenticate()](btcedu/services/youtube_service.py#L360-L411) |
| 2.3 | Upload uses resumable upload | **PASS** | `MediaFileUpload(resumable=True)` |
| 2.4 | Handles large files (100MB+) | **PASS** | Configurable chunk size (default 10MB) |
| 2.5 | Upload shows progress logging | **PASS** | Progress callback + logger.info |
| 2.6 | Metadata: title, description, tags, category, language, privacy | **PASS** | Set in `videos().insert()` body |
| 2.7 | Handles API errors: quota (403), bad request (400), network | **PASS** | Separate exception classes: `YouTubeQuotaError`, `YouTubeUploadError` |
| 2.8 | Retry logic for network errors | **PASS** | Exponential backoff, up to 3 retries on 500/502/503/504 |
| 2.9 | Returns video ID and YouTube URL | **PASS** | `YouTubeUploadResponse` dataclass |

## 3. Pre-Publish Safety Checks (CRITICAL)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 3.1 | Check 1 — Approval gate | **PASS** | [_check_approval_gate()](btcedu/core/publisher.py#L78-L97) verifies APPROVED status + render ReviewTask |
| 3.2 | Check 2 — Artifact integrity | **PASS** | [_check_artifact_integrity()](btcedu/core/publisher.py#L100-L139) — SHA-256 over artifact_paths matches ReviewTask.artifact_hash |
| 3.3 | Check 3 — Metadata completeness | **PASS** | [_check_metadata_completeness()](btcedu/core/publisher.py#L142-L161) |
| 3.4 | Check 4 — Cost sanity | **PASS** | [_check_cost_sanity()](btcedu/core/publisher.py#L164-L183) |
| 3.5 | All checks run before any upload attempt | **PASS** | `_run_all_safety_checks()` called before `upload_video()` |
| 3.6 | Any check fails → abort with descriptive error | **PASS** | `ValueError` raised listing all failed checks |
| 3.7 | Check results logged for audit trail | **PASS** | `logger.info()` for each check result |
| 3.8 | `--dry-run` runs all checks without uploading | **PASS** | Uses `DryRunYouTubeService`, episode stays APPROVED |
| 3.9 | No bypass path | **PASS** | Even `--force` runs all safety checks |
| 3.10 | Hash comparison uses constant-time comparison | **MINOR** | Uses standard `!=` comparison (not `hmac.compare_digest`). Acceptable — this is not a cryptographic authentication scenario, just tamper detection. |

## 4. Publisher Module

| # | Item | Status | Notes |
|---|------|--------|-------|
| 4.1 | `btcedu/core/publisher.py` exists | **PASS** | 610 lines |
| 4.2 | `publish_video()` correct signature | **PASS** | `(session, episode_id, settings, force, privacy)` |
| 4.3 | Returns structured result | **PASS** | `PublishResult` dataclass |
| 4.4 | Pre-condition: episode is APPROVED | **PASS** | ValueError if not |
| 4.5 | Runs safety checks before upload | **PASS** | |
| 4.6 | Builds YouTube metadata correctly | **PASS** | `_build_youtube_metadata()` |
| 4.7 | Creates PublishJob record | **PASS** | Status tracking: pending → uploading → published/failed |
| 4.8 | On success: updates Episode + PublishJob | **PASS** | youtube_video_id, published_at_youtube, status=PUBLISHED |
| 4.9 | On failure: PublishJob=failed, episode stays APPROVED | **PASS** | Error message recorded |
| 4.10 | Idempotency: skips if youtube_video_id set | **PASS** | Returns `PublishResult(skipped=True)` |

## 5. YouTube Metadata Construction

| # | Item | Status | Notes |
|---|------|--------|-------|
| 5.1 | Title from chapter data | **PASS** | From `chapters.json` title, falls back to `episode.title` |
| 5.2 | Description includes episode summary | **PASS** | First chapter narration excerpt (up to 300 chars) |
| 5.3a | Starts with `0:00` | **PASS** | First chapter always gets `_format_timestamp(0)` = `0:00` |
| 5.3b | Uses cumulative durations from TTS manifest | **PASS** | `_load_tts_durations()` reads actual audio durations |
| 5.3c | Format: `M:SS Title` or `H:MM:SS Title` | **PASS** | `_format_timestamp()` handles both |
| 5.4 | Tags include relevant keywords | **PASS** | Base: Bitcoin, Kripto, Blockchain, Türkçe, Eğitim, Cryptocurrency + chapter titles |
| 5.5 | Category set to 27 (Education) | **PASS** | From config default |
| 5.6 | Language set to Turkish (tr) | **PASS** | From config default |
| 5.7 | Privacy: `unlisted` by default, configurable | **PASS** | `youtube_default_privacy` in config, `--privacy` CLI flag |
| 5.8 | Metadata snapshot stored in PublishJob | **PASS** | JSON-serialized |
| 5.9 | Turkish characters handled correctly | **PASS** | `ensure_ascii=False` in JSON serialization |

## 6. OAuth2 Authentication

| # | Item | Status | Notes |
|---|------|--------|-------|
| 6.1 | `btcedu youtube-auth` exists | **PASS** | [cli.py](btcedu/cli.py#L1232-L1249) |
| 6.2 | Opens browser for consent | **PASS** | `InstalledAppFlow.from_client_secrets_file()` + `run_local_server()` |
| 6.3 | Credentials stored at configured path | **PASS** | `youtube_credentials_path` setting |
| 6.4 | Credentials in .gitignore | **PASS** | `data/` directory is gitignored |
| 6.5 | Client secrets in .gitignore | **PASS** | Also in `data/` |
| 6.6 | Token refresh works | **PASS** | Auto-refresh in `_load_credentials()` |
| 6.7 | `btcedu youtube-status` shows status | **PASS** | Shows valid, expired, expiry, can_refresh |

## 7. CLI Commands

| # | Item | Status | Notes |
|---|------|--------|-------|
| 7.1 | `btcedu publish` with --force, --dry-run, --privacy | **PASS** | |
| 7.2 | --dry-run: checks + metadata, no upload | **PASS** | |
| 7.3 | --privacy unlisted/private/public | **PASS** | `click.Choice()` |
| 7.4 | Shows upload progress | **PASS** | Via progress_callback logging |
| 7.5 | Displays YouTube URL on success | **PASS** | `[OK] {eid} -> {url}` |
| 7.6 | youtube-auth sets up OAuth2 | **PASS** | |
| 7.7 | youtube-status shows credential validity | **PASS** | Fixed during validation (was using wrong key) |
| 7.8 | All commands have --help text | **PASS** | Click docstrings |

## 8. Pipeline Integration

| # | Item | Status | Notes |
|---|------|--------|-------|
| 8.1 | PUBLISH wired in pipeline | **PASS** | `("publish", EpisodeStatus.APPROVED)` in `_V2_STAGES` |
| 8.2 | Included for v2 after APPROVED | **PASS** | |
| 8.3 | PUBLISHED status exists | **PASS** | `EpisodeStatus.PUBLISHED` with ordinal 18 |
| 8.4 | Full v2 flow: NEW → ... → APPROVED → PUBLISHED | **PASS** | Note: PUBLISHED → COMPLETED transition is not explicitly wired (PUBLISHED is the terminal v2 state). The plan says PUBLISHED is the final state. |
| 8.5 | v1 pipeline unaffected | **PASS** | `_V1_STAGES` unchanged |

## 9. Dashboard Publish UI

| # | Item | Status | Notes |
|---|------|--------|-------|
| 9.1 | Publish button for APPROVED episodes | **PASS** | In episode detail actions |
| 9.2 | Confirmation before publishing | **PARTIAL** | Uses `submitJob()` which shows job progress but no explicit "Are you sure?" dialog |
| 9.3 | Progress/status during upload | **PASS** | JobManager polling |
| 9.4 | YouTube URL displayed after publishing | **PASS** | Publish panel in video tab |
| 9.5 | Publish history shown | **PASS** | Via `/publish-status` endpoint |
| 9.6 | Safety check results displayed | **N/A** | Plan says optional; checks run server-side, errors returned if failure |
| 9.7 | Error messages for failed publishes | **PASS** | `error_message` in publish-status response |
| 9.8 | Follows existing dashboard patterns | **PASS** | Same btn/badge/panel patterns |

## 10. V1 Pipeline + Backward Compatibility (Full Regression)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 10.1 | `btcedu status` works | **PASS** | Unchanged |
| 10.2 | v1 pipeline stages unmodified | **PASS** | `_V1_STAGES` untouched |
| 10.3 | Correction + RG1 | **PASS** | Tests pass |
| 10.4 | Translation | **PASS** | Tests pass |
| 10.5 | Adaptation + RG2 | **PASS** | Tests pass |
| 10.6 | Chapterization | **PASS** | Tests pass |
| 10.7 | Image generation | **PASS** | Tests pass |
| 10.8 | TTS | **PASS** | Tests pass |
| 10.9 | Render pipeline | **PASS** | Tests pass |
| 10.10 | Review Gate 3 | **PASS** | Tests pass |
| 10.11 | All dashboard pages | **PASS** | Tests pass |
| 10.12 | All existing tests pass | **PASS** | 540 pre-Sprint 11 tests still pass |
| 10.13 | No CLI commands broken | **PASS** | |
| 10.14 | All models/migrations intact | **PASS** | |

## 11. Test Coverage

| # | Item | Status | Notes |
|---|------|--------|-------|
| 11.1 | YouTube service tests: mock upload, quota, network | **PARTIAL** | DryRun tested; real API mock tests (quota, network error, retry) not present — acceptable since `google-api-python-client` is optional |
| 11.2 | OAuth2 tests: credential loading, refresh | **PARTIAL** | `check_token_status` tested (3 tests); load/refresh tested implicitly. No mock of real `Credentials` class |
| 11.3 | Safety checks: all pass, each fails individually | **PASS** | 13 tests covering all 4 checks in pass/fail scenarios |
| 11.4 | Artifact integrity: hash match/mismatch/missing | **PASS** | 3 tests |
| 11.5 | Metadata construction: title, description, timestamps, tags | **PASS** | 4 tests |
| 11.6 | Chapter timestamps: cumulative, format | **PASS** | 6 timestamp format tests + description test |
| 11.7 | Publisher: full flow, idempotency, dry-run, error | **PASS** | 8 integration tests |
| 11.8 | PublishJob model: CRUD | **PASS** | 2 tests for get_latest_publish_job |
| 11.9 | CLI tests | **PARTIAL** | CLI commands exist with --help; no dedicated CLI test file (consistent with other sprints) |
| 11.10 | Pipeline: PUBLISH in v2 after APPROVED | **PASS** | Verified via `_V2_STAGES` and `run_pending()` filter |
| 11.11 | End-to-end pipeline test | **N/A** | Not implemented as a test; would require mocking all 12 stages + 3 review gates. Acceptable for this sprint. |
| 11.12 | All tests use mocked external APIs | **PASS** | No real API calls in tests |
| 11.13 | All tests pass | **PASS** | 581 passed, 0 failed |

## 12. Scope Creep Detection

| # | Item | Status |
|---|------|--------|
| 12.1 | No live streaming | **PASS** |
| 12.2 | No YouTube analytics | **PASS** |
| 12.3 | No multi-channel | **PASS** |
| 12.4 | No scheduled publishing | **PASS** |
| 12.5 | No video re-upload | **PASS** |
| 12.6 | No automatic privacy changes | **PASS** |
| 12.7 | No existing stages modified | **PASS** |
| 12.8 | No unnecessary dependencies | **PASS** | Google API deps added as optional/commented |

## 13. Security & Safety Review (CRITICAL)

| # | Item | Status | Notes |
|---|------|--------|-------|
| 13.1 | OAuth credentials not in git | **PASS** | `data/` in `.gitignore` |
| 13.2 | Client secrets not in git | **PASS** | `data/` in `.gitignore` |
| 13.3 | No API keys hardcoded | **PASS** | All via config/files |
| 13.4 | Safety checks cannot be bypassed | **PASS** | No --skip-checks flag, even --force runs all checks |
| 13.5 | Artifact hash prevents tampered videos | **PASS** | SHA-256 comparison against RG3-approved hash |
| 13.6 | Cost cap prevents runaway spending | **PASS** | `max_episode_cost_usd` enforcement |
| 13.7 | Default privacy is `unlisted` | **PASS** | Safe default |
| 13.8 | Upload errors don't leak sensitive info | **PASS** | Error messages are descriptive but don't include tokens |
| 13.9 | Dashboard publish endpoint validates | **PASS** | Uses `_submit_job()` pattern with episode validation |
| 13.10 | No SQL injection | **PASS** | Uses SQLAlchemy ORM, parameterized queries |
| 13.11 | Turkish text properly encoded | **PASS** | `ensure_ascii=False`, UTF-8 encoding |

---

# 4) Bugs Found & Fixed

| # | Bug | Severity | Fix |
|---|-----|----------|-----|
| 1 | `youtube-status` CLI: `status['exists']` KeyError — `check_token_status()` returns `valid`, not `exists` | **High** (runtime crash) | Replaced with derived `has_creds` check; added expired/can_refresh output |
| 2 | `test_all_migrations_run_sequentially`: hardcoded `len(pending) == 4` but Migration 006 makes it 5 | **Medium** (test failure on main) | Updated assertion to `len(pending) == 5` |

Both bugs were fixed and committed.

---

# 5) Alignment with Plan Output

Comparing implementation against `docs/sprints/outputs/sprint11-plan-output.md`:

| Plan Item | Implemented | Deviation |
|-----------|-------------|-----------|
| 5 new files | ✅ 5 files created | None |
| 12 modified files | ✅ 12 files modified (+ test fix) | +1 test fix file |
| YouTubeDataAPIService | ✅ | None |
| DryRunYouTubeService | ✅ | None |
| 4 safety checks | ✅ All 4 | None |
| `publish_video()` flow | ✅ | None |
| PublishJob model | ✅ | None |
| Migration 006 | ✅ | None |
| 6 config fields | ✅ All 6 | None |
| 3 CLI commands | ✅ All 3 | None |
| Pipeline APPROVED → PUBLISHED | ✅ | PUBLISHED is terminal (no explicit → COMPLETED step, matches plan §19) |
| Dashboard UI | ✅ | No explicit confirmation dialog (minor) |
| 24 publisher tests planned | 33 implemented | More coverage than planned |
| 9 YouTube service tests planned | 8 implemented | Close match; thumbnail test omitted, acceptable |
| Provenance file | ✅ | None |
| Idempotency | ✅ | None |
| Cost $0 for publish stage | ✅ | `estimated_cost_usd=0.0` |

---

# 6) Backward Compatibility

- **v1 pipeline**: Completely untouched. `_V1_STAGES` unchanged, no v1 model modifications.
- **Pre-Sprint 11 tests**: All 540 existing tests pass unchanged (except 1 migration count fix).
- **Risk assessment**: **Low**. All changes are additive.

---

# 7) Nice-to-Have Improvements (optional, not blocking)

1. Add explicit confirmation dialog in dashboard before publishing (currently uses submitJob pattern only)
2. Add mock-based tests for `YouTubeDataAPIService.upload_video()` with simulated HttpError 403/503
3. Add `hmac.compare_digest()` for artifact hash comparison (defense in depth, though not security-critical)
4. Consider adding `PUBLISHED → COMPLETED` explicit transition in pipeline (currently PUBLISHED is terminal)
5. Add CLI tests for `btcedu publish --help`, `btcedu youtube-auth --help`
