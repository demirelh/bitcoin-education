# Sprint 11 — Validation Prompt (YouTube Publishing + Safety Checks)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 11 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–10 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict. This is the **final validation** — includes a comprehensive end-to-end pipeline assessment.

---

## Context

You are reviewing the **Sprint 11 (Phase 6: YouTube Publishing)** implementation of the btcedu video production pipeline. This is the **final sprint** of the 11-sprint program.

Sprint 11 was scoped to:
- Create YouTube service with OAuth2 authentication and resumable upload
- Create `publish_jobs` table + SQLAlchemy model
- Implement `publish_video()` in `btcedu/core/publisher.py`
- Implement 4 pre-publish safety checks (approval gate, artifact integrity, metadata completeness, cost sanity)
- Build YouTube metadata with chapter timestamps from TTS durations
- Add `publish`, `youtube-auth`, `youtube-status` CLI commands
- Integrate PUBLISH stage into v2 pipeline (APPROVED → PUBLISHED → COMPLETED)
- Add dashboard publish button and status display
- Write tests (including end-to-end pipeline test)

Sprint 11 was NOT scoped to include: live streaming, YouTube analytics, multi-channel, scheduled publishing, video re-upload, automatic privacy changes.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. `publish_jobs` Table & Model

- [ ] **1.1** Migration creates `publish_jobs` table with correct schema per §7.3
- [ ] **1.2** Table has fields: id, episode_id, status, youtube_video_id, youtube_url, metadata_snapshot (JSON), published_at, error_message
- [ ] **1.3** Indexes on episode_id and status
- [ ] **1.4** `PublishJob` SQLAlchemy model exists and is registered
- [ ] **1.5** Migration runs cleanly on existing database (additive)

### 2. YouTube Service

- [ ] **2.1** `btcedu/services/youtube_service.py` exists
- [ ] **2.2** OAuth2 authentication implemented:
  - [ ] **2.2a** Loads credentials from stored file
  - [ ] **2.2b** Refreshes expired tokens automatically
  - [ ] **2.2c** Falls back to interactive flow if no credentials
- [ ] **2.3** Upload method uses resumable upload (`MediaFileUpload(resumable=True)`)
- [ ] **2.4** Upload handles large files (100MB+)
- [ ] **2.5** Upload shows progress logging
- [ ] **2.6** Metadata setting: title, description, tags, category, language, privacy
- [ ] **2.7** Handles API errors: quota (403), bad request (400), network errors
- [ ] **2.8** Retry logic for network errors (exponential backoff, up to 3 retries)
- [ ] **2.9** Returns video ID and YouTube URL on success

### 3. Pre-Publish Safety Checks (CRITICAL)

- [ ] **3.1** **Check 1 — Approval gate**: Verifies episode is APPROVED and RG3 ReviewTask exists with status=APPROVED
- [ ] **3.2** **Check 2 — Artifact integrity**: Computes SHA-256 of current `draft.mp4` and compares to `ReviewTask.artifact_hash`
- [ ] **3.3** **Check 3 — Metadata completeness**: Verifies title, description, and tags are non-empty
- [ ] **3.4** **Check 4 — Cost sanity**: Verifies total episode cost ≤ `max_episode_cost_usd`
- [ ] **3.5** All checks run before any upload attempt
- [ ] **3.6** If ANY check fails: publish aborts with descriptive error listing all failures
- [ ] **3.7** Check results logged for audit trail
- [ ] **3.8** `--dry-run` runs all checks without uploading
- [ ] **3.9** No bypass path — cannot publish without all 4 checks passing
- [ ] **3.10** Hash comparison uses constant-time comparison (or acceptable alternative)

### 4. Publisher Module

- [ ] **4.1** `btcedu/core/publisher.py` exists
- [ ] **4.2** `publish_video()` function has correct signature
- [ ] **4.3** Returns structured result (PublishResult or similar)
- [ ] **4.4** Pre-condition check: episode is APPROVED
- [ ] **4.5** Runs safety checks before upload
- [ ] **4.6** Builds YouTube metadata correctly
- [ ] **4.7** Creates PublishJob record with status tracking
- [ ] **4.8** On success: updates Episode (youtube_video_id, published_at), PublishJob (published)
- [ ] **4.9** On failure: PublishJob marked failed with error_message, episode stays APPROVED
- [ ] **4.10** Idempotency: skips if youtube_video_id already set

### 5. YouTube Metadata Construction

- [ ] **5.1** Title from episode/chapter data
- [ ] **5.2** Description includes episode summary
- [ ] **5.3** Chapter timestamps in description (YouTube format):
  - [ ] **5.3a** Starts with `0:00` for first chapter
  - [ ] **5.3b** Uses cumulative durations from TTS manifest (actual audio durations)
  - [ ] **5.3c** Format: `H:MM:SS Title` or `M:SS Title`
- [ ] **5.4** Tags include relevant keywords (Bitcoin, Kripto, Türkçe, Eğitim, etc.)
- [ ] **5.5** Category set to 27 (Education)
- [ ] **5.6** Language set to Turkish (tr)
- [ ] **5.7** Privacy set to `unlisted` by default (configurable)
- [ ] **5.8** Metadata snapshot stored in PublishJob record (JSON)
- [ ] **5.9** Turkish characters handled correctly in metadata

### 6. OAuth2 Authentication

- [ ] **6.1** `btcedu youtube-auth` command exists and works
- [ ] **6.2** Opens browser for Google OAuth consent
- [ ] **6.3** Credentials stored at configured path (data/.youtube_credentials.json)
- [ ] **6.4** Credentials file is in .gitignore (not committed)
- [ ] **6.5** Client secrets file is in .gitignore
- [ ] **6.6** Token refresh works for expired tokens
- [ ] **6.7** `btcedu youtube-status` shows authentication status

### 7. CLI Commands

- [ ] **7.1** `btcedu publish <episode_id>` exists with `--force`, `--dry-run`, `--privacy`
- [ ] **7.2** `--dry-run`: runs safety checks, shows metadata, no upload
- [ ] **7.3** `--privacy unlisted|private|public`: overrides default privacy
- [ ] **7.4** Shows upload progress during publishing
- [ ] **7.5** Displays YouTube URL on success
- [ ] **7.6** `btcedu youtube-auth` sets up OAuth2 credentials
- [ ] **7.7** `btcedu youtube-status` shows credential validity
- [ ] **7.8** All commands have useful `--help` text

### 8. Pipeline Integration

- [ ] **8.1** PUBLISH / PUBLISHED is properly wired in pipeline
- [ ] **8.2** `resolve_pipeline_plan()` includes PUBLISH for v2 episodes after APPROVED
- [ ] **8.3** PUBLISHED → COMPLETED transition exists
- [ ] **8.4** Full v2 pipeline flow: NEW → ... → APPROVED → PUBLISHED → COMPLETED
- [ ] **8.5** v1 pipeline is completely unaffected

### 9. Dashboard Publish UI

- [ ] **9.1** Publish button visible for APPROVED episodes only
- [ ] **9.2** Confirmation before publishing
- [ ] **9.3** Progress/status display during upload
- [ ] **9.4** YouTube URL displayed after publishing (clickable link)
- [ ] **9.5** Publish history shown (PublishJob records)
- [ ] **9.6** Safety check results displayed before publish
- [ ] **9.7** Error messages displayed for failed publishes
- [ ] **9.8** Follows existing dashboard template and styling patterns

### 10. V1 Pipeline + All Previous Sprint Compatibility (Full Regression)

- [ ] **10.1** `btcedu status` still works for all episodes (v1 and v2)
- [ ] **10.2** v1 pipeline stages completely unmodified and functional
- [ ] **10.3** Correction + Review Gate 1 still work
- [ ] **10.4** Translation stage still works
- [ ] **10.5** Adaptation + Review Gate 2 still work
- [ ] **10.6** Chapterization still works
- [ ] **10.7** Image generation still works
- [ ] **10.8** TTS still works
- [ ] **10.9** Render pipeline still works
- [ ] **10.10** Review Gate 3 still works (artifact_hash recorded)
- [ ] **10.11** All existing dashboard pages still function
- [ ] **10.12** All existing tests still pass
- [ ] **10.13** No existing CLI commands broken
- [ ] **10.14** All existing models and migrations are intact

### 11. Test Coverage

- [ ] **11.1** YouTube service tests: mock upload success, quota error, network error, retry
- [ ] **11.2** OAuth2 tests: credential loading, refresh, missing credentials
- [ ] **11.3** Safety check tests: all pass, each fails individually
- [ ] **11.4** Artifact integrity tests: hash match, hash mismatch, missing hash
- [ ] **11.5** Metadata construction tests: title, description with timestamps, tags
- [ ] **11.6** Chapter timestamp tests: correct cumulative calculation, format
- [ ] **11.7** Publisher tests: full flow, idempotency, dry-run, error handling
- [ ] **11.8** PublishJob model tests: CRUD
- [ ] **11.9** CLI tests: publish, youtube-auth, youtube-status
- [ ] **11.10** Pipeline tests: PUBLISH in v2 plan after APPROVED
- [ ] **11.11** **End-to-end pipeline test**: full v2 flow from NEW to PUBLISHED (mocked APIs)
- [ ] **11.12** All tests use mocked external API calls
- [ ] **11.13** All tests pass with `pytest tests/`

### 12. Scope Creep Detection

- [ ] **12.1** No live streaming support implemented
- [ ] **12.2** No YouTube analytics/comment management implemented
- [ ] **12.3** No multi-channel support implemented
- [ ] **12.4** No scheduled publishing implemented
- [ ] **12.5** No video re-upload/update mechanism implemented
- [ ] **12.6** No automatic privacy changes implemented
- [ ] **12.7** No existing stages modified beyond pipeline integration
- [ ] **12.8** No unnecessary dependencies added

### 13. Security & Safety Review (CRITICAL for Publishing Sprint)

- [ ] **13.1** OAuth credentials not committed to git (.gitignore verified)
- [ ] **13.2** Client secrets not committed to git
- [ ] **13.3** No API keys hardcoded in source
- [ ] **13.4** Pre-publish safety checks cannot be bypassed (no code path skips them)
- [ ] **13.5** Artifact hash verification prevents publishing tampered videos
- [ ] **13.6** Cost cap prevents runaway spending
- [ ] **13.7** Default privacy is `unlisted` (not `public`) — safe default
- [ ] **13.8** Upload errors don't leak sensitive information (API keys, tokens)
- [ ] **13.9** Dashboard publish endpoint validates authorization
- [ ] **13.10** No SQL injection vectors in PublishJob queries
- [ ] **13.11** Turkish text in metadata properly encoded (no encoding issues on YouTube)

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 11 is complete. **The full v2 pipeline is operational.** |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Pipeline is nearly complete. |
| **FAIL** | Critical issues found. Sprint 11 must be reworked before the pipeline is production-ready. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

---

## Final Pipeline Assessment (Sprint 11 = Final Sprint)

This section is unique to Sprint 11 — it assesses the **complete v2 pipeline** as a whole.

### Complete V2 Pipeline Flow Verification

Verify the following end-to-end flow is functional:

```
NEW
 → DOWNLOADED          (audio.m4a via yt-dlp)
 → TRANSCRIBED         (transcript.de.txt via Whisper)
 → CORRECTED           (transcript.corrected.de.txt via Claude)
   ◆ REVIEW GATE 1     (approve/reject transcript correction)
 → TRANSLATED          (transcript.tr.txt via Claude)
 → ADAPTED             (script.adapted.tr.md via Claude)
   ◆ REVIEW GATE 2     (approve/reject adaptation)
 → CHAPTERIZED         (chapters.json via Claude)
 → IMAGES_GENERATED    (images/ via DALL-E 3)
 → TTS_DONE            (tts/ via ElevenLabs)
 → RENDERED            (draft.mp4 via ffmpeg)
   ◆ REVIEW GATE 3     (approve/reject video)
 → APPROVED            (human approved, artifact_hash recorded)
 → PUBLISHED           (uploaded to YouTube)
 → COMPLETED           (archived)
```

- [ ] **E2E.1** Each stage transitions to the next correctly
- [ ] **E2E.2** All three review gates block pipeline progression
- [ ] **E2E.3** Rejection at any gate allows re-processing
- [ ] **E2E.4** Cascade invalidation works: upstream change → downstream marked stale
- [ ] **E2E.5** Idempotency works: re-running any stage skips if output is current
- [ ] **E2E.6** Force flag works: re-runs any stage regardless
- [ ] **E2E.7** Cost tracking is cumulative across all stages
- [ ] **E2E.8** Provenance is recorded for every LLM/API call
- [ ] **E2E.9** v1 pipeline coexists without interference
- [ ] **E2E.10** `pipeline_version` correctly distinguishes v1 and v2 episodes

### Architecture Compliance Check

Verify alignment with MASTERPLAN.md guiding principles:

- [ ] **ARCH.1** Idempotency: every stage checks before processing (§3.8)
- [ ] **ARCH.2** Observability: every output traceable to input + prompt version + model (§3.6)
- [ ] **ARCH.3** Human Review: three mandatory gates at correct positions (§3.3)
- [ ] **ARCH.4** Prompt Versioning: prompts are files with metadata, tracked by PromptRegistry (§3.4)
- [ ] **ARCH.5** Incremental Extension: no big rewrites, new stages alongside existing (§principle 5)
- [ ] **ARCH.6** Cascade Invalidation: upstream changes invalidate downstream (§8)
- [ ] **ARCH.7** Safety: no hallucination, no financial advice, editorial neutrality (§principle 7)

### Subplan Completion Check

Verify all subplans from the master plan have been addressed:

- [ ] **SUB.A** Transcript Correction + Diff Review (Sprint 2-3)
- [ ] **SUB.B** Turkish Translation (Sprint 4)
- [ ] **SUB.C** Turkey-Context Adaptation (Sprint 5)
- [ ] **SUB.D** Chapterized Production JSON (Sprint 6)
- [ ] **SUB.E** Image/Video Prompt Generation (Sprint 7)
- [ ] **SUB.F** TTS Integration (Sprint 8)
- [ ] **SUB.G** Video Assembly / Render Pipeline (Sprint 9-10)
- [ ] **SUB.H** Human Review & Approval Workflow (Sprint 3, 5, 10)
- [ ] **SUB.I** YouTube Publishing Integration (Sprint 11)
- [ ] **SUB.J** Prompt Management / Versioning Framework (Sprint 1, ongoing)

### Production Readiness

- [ ] **PROD.1** All stages are functional and tested
- [ ] **PROD.2** Review gates prevent unapproved content from publishing
- [ ] **PROD.3** Safety checks prevent tampered content from publishing
- [ ] **PROD.4** Cost controls prevent runaway spending
- [ ] **PROD.5** Error handling is resilient (no silent failures)
- [ ] **PROD.6** Logging and provenance support debugging
- [ ] **PROD.7** OAuth credentials are managed securely
- [ ] **PROD.8** Dashboard provides sufficient visibility for content owner
- [ ] **PROD.9** CLI provides sufficient control for developer operations
- [ ] **PROD.10** Deployment on Raspberry Pi is viable (reasonable resource usage)

---

## Post-Implementation Recommendations

After Sprint 11, the following enhancements can be considered (not in scope for this sprint):

1. **Monitoring & Alerts**: Webhook/email notifications for new review tasks, publish failures
2. **Auto-approve rules**: Configurable auto-approval for minor corrections (RG1)
3. **Batch processing**: Process multiple episodes through the v2 pipeline
4. **A/B testing**: Compare prompt versions systematically
5. **Performance optimization**: Parallel image/TTS generation, render optimization
6. **Cleanup policies**: Auto-delete old rendered segments, manage disk usage
7. **Multi-channel**: Support different YouTube channels with different settings
8. **Playlist management**: Auto-add published videos to playlists
9. **Analytics dashboard**: Track publishing metrics, viewer engagement
10. **Prompt tuning**: Iterative improvement of correction/adaptation/chapterization prompts

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **This is the final sprint validation. Pay extra attention to**:
  - **Section 3 (Safety Checks)**: The 4 pre-publish checks are the last defense before public content. They must be airtight.
  - **Section 13 (Security)**: OAuth credentials, API keys, and content integrity are critical for a publishing system.
  - **Final Pipeline Assessment**: The end-to-end flow must work. This is the culmination of 11 sprints.
- If the end-to-end pipeline test doesn't exist or doesn't cover the full flow, this is a **blocking issue**.
