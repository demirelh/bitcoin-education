# Sprint 11 — Implementation Prompt (YouTube Publishing + Safety Checks)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 11 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–10 completed codebase
> - **Expected output**: All code changes (new files, modified files), YouTube service, publisher module, publish_jobs migration, pre-publish safety checks, dashboard publish UI, tests — committed and passing.

---

## Context

You are implementing **Sprint 11 (Phase 6: YouTube Publishing)** — the **final sprint** of the btcedu video production pipeline.

Sprints 1–10 are complete:
- The full pipeline from detect through render + review is functional.
- Three review gates (RG1, RG2, RG3) are working.
- `artifact_hash` is recorded on RG3 approval.
- Episodes can reach APPROVED status.

Sprint 11 adds the **PUBLISH** stage — uploading approved videos to YouTube. This includes pre-publish safety checks, OAuth2 authentication, resumable upload, metadata setting, and the `publish_jobs` table.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 11 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/reviewer.py` (review gate patterns), `btcedu/core/pipeline.py`, `btcedu/models/review.py` (ReviewTask with artifact_hash), `btcedu/models/episode.py` (youtube_video_id field), `btcedu/config.py`, `btcedu/cli.py`, `btcedu/web/`.

2. **Add Google API dependencies** — add to `pyproject.toml`:
   - `google-api-python-client` (YouTube Data API)
   - `google-auth-httplib2` (auth transport)
   - `google-auth-oauthlib` (OAuth2 flow)
   - Keep versions pinned or with minimum version constraints

3. **Create `publish_jobs` migration** — Migration N+5 per §7.4:
   ```sql
   CREATE TABLE publish_jobs (
     id INTEGER PRIMARY KEY AUTOINCREMENT,
     episode_id TEXT NOT NULL,
     status TEXT NOT NULL DEFAULT 'pending',
     youtube_video_id TEXT,
     youtube_url TEXT,
     metadata_snapshot TEXT,
     published_at DATETIME,
     error_message TEXT
   );
   CREATE INDEX ix_publish_jobs_episode ON publish_jobs(episode_id);
   CREATE INDEX ix_publish_jobs_status ON publish_jobs(status);
   ```
   - Create `btcedu/models/publish_job.py` with SQLAlchemy model
   - Register model in `btcedu/models/__init__.py`
   - Append Migration subclass to `btcedu/migrations/__init__.py`
   - Verify `youtube_video_id` and `published_at_youtube` fields exist on Episode model (added in Sprint 1 migration). If missing, add them in this migration.

4. **Extend configuration** — add to `btcedu/config.py`:
   - `YOUTUBE_CLIENT_SECRETS_FILE: str = "data/.youtube_client_secrets.json"` (Google OAuth client ID/secret)
   - `YOUTUBE_CREDENTIALS_FILE: str = "data/.youtube_credentials.json"` (stored OAuth tokens)
   - `YOUTUBE_DEFAULT_PRIVACY: str = "unlisted"` (default upload privacy)
   - `YOUTUBE_DEFAULT_CATEGORY: str = "27"` (Education category)
   - `YOUTUBE_UPLOAD_CHUNK_SIZE: int = -1` (-1 = single request; set to e.g., 10*1024*1024 for chunked)
   - Update `.env.example` with new variables

5. **Create YouTube service** — `btcedu/services/youtube_service.py`:

   - **OAuth2 setup** — `authenticate()`:
     - Check for existing credentials at `YOUTUBE_CREDENTIALS_FILE`
     - If valid: load and refresh if expired
     - If missing/invalid: run `InstalledAppFlow` from client secrets → opens browser → saves credentials
     - Return authenticated `googleapiclient.discovery.build("youtube", "v3", credentials=creds)` client

   - **Upload method** — `upload_video(file_path, metadata) -> UploadResult`:
     - Use `MediaFileUpload` with `resumable=True` for large files
     - Set chunk size from config
     - Call `youtube.videos().insert(...)` with:
       - `part="snippet,status"`
       - `snippet`: title, description, tags, categoryId, defaultLanguage
       - `status`: privacyStatus (from config, default "unlisted")
     - Handle upload progress: `response = None; while response is None: status, response = request.next_chunk()`
     - Log progress: "Uploading... X% complete"
     - Return `UploadResult`: video_id, url (`https://youtu.be/{video_id}`), status
     - Handle errors:
       - `HttpError` 403 (quota exceeded): abort with descriptive message
       - `HttpError` 400 (bad request): abort with YouTube error details
       - Network errors: retry up to 3 times with exponential backoff
       - Resumable upload: on network failure, the upload can be resumed from last chunk

   - **Metadata update** — `update_video_metadata(video_id, metadata) -> bool`:
     - For post-upload metadata updates (e.g., changing privacy to public)
     - `youtube.videos().update(...)`

   - **Thumbnail upload** — `set_thumbnail(video_id, image_path) -> bool`:
     - `youtube.thumbnails().set(videoId=video_id, media_body=...)`
     - Optional — only if a thumbnail image is available

6. **Implement pre-publish safety checks** — in `btcedu/core/publisher.py`:

   ```python
   def run_safety_checks(session, episode, settings) -> SafetyCheckResult:
       checks = []

       # Check 1: Approval gate
       rg3 = get_approved_review_task(session, episode.id, stage="render")
       checks.append(SafetyCheck(
           name="approval_gate",
           passed=rg3 is not None,
           message="Episode is APPROVED with RG3" if rg3 else "No approved RG3 ReviewTask found"
       ))

       # Check 2: Artifact integrity
       if rg3 and rg3.artifact_hash:
           current_hash = compute_file_hash(draft_video_path)
           integrity_ok = current_hash == rg3.artifact_hash
           checks.append(SafetyCheck(
               name="artifact_integrity",
               passed=integrity_ok,
               message=f"Hash match: {integrity_ok}" if integrity_ok
                       else f"MISMATCH: approved={rg3.artifact_hash[:16]}... current={current_hash[:16]}..."
           ))
       else:
           checks.append(SafetyCheck(
               name="artifact_integrity",
               passed=False,
               message="No artifact_hash found on approved ReviewTask"
           ))

       # Check 3: Metadata completeness
       metadata = build_youtube_metadata(episode)
       meta_ok = all([metadata.title, metadata.description, metadata.tags])
       checks.append(SafetyCheck(
               name="metadata_completeness",
               passed=meta_ok,
               message="Title, description, and tags are non-empty" if meta_ok
                       else "Missing required metadata fields"
       ))

       # Check 4: Cost sanity
       total_cost = get_episode_total_cost(session, episode.id)
       cost_ok = total_cost <= settings.max_episode_cost_usd
       checks.append(SafetyCheck(
           name="cost_sanity",
           passed=cost_ok,
           message=f"Total cost ${total_cost:.2f} within budget ${settings.max_episode_cost_usd:.2f}"
                   if cost_ok else f"Total cost ${total_cost:.2f} EXCEEDS budget ${settings.max_episode_cost_usd:.2f}"
       ))

       all_passed = all(c.passed for c in checks)
       return SafetyCheckResult(checks=checks, all_passed=all_passed)
   ```

   - If any check fails: abort with descriptive error listing all failed checks
   - Log all results for audit trail
   - In `--dry-run` mode: run checks and report but don't upload

7. **Implement `publish_video()`** in `btcedu/core/publisher.py`:
   - **Pre-condition check**: Episode status is APPROVED
   - Run all 4 safety checks → abort if any fail
   - Check idempotency: if `episode.youtube_video_id` is set, skip (already published)
   - Build YouTube metadata:
     - Title: from episode title or chapter document title
     - Description: summary + chapter timestamps
     - Tags: from episode content context
     - Category: 27 (Education)
     - Language: tr
   - Create PublishJob record (status = "pending")
   - Update PublishJob status to "uploading"
   - Call `youtube_service.upload_video()`
   - On success:
     - Update PublishJob: status="published", youtube_video_id, youtube_url, published_at
     - Update Episode: youtube_video_id, published_at_youtube
     - Update episode status: PUBLISHED
     - Optionally set thumbnail (first chapter image or generated thumbnail)
   - On failure:
     - Update PublishJob: status="failed", error_message
     - Episode status stays APPROVED (can retry)
   - Return PublishResult

8. **Build YouTube metadata** — `build_youtube_metadata()`:
   - Load chapter JSON from `data/outputs/{ep_id}/chapters.json`
   - Title: `chapters.title` (the episode title from chapter document)
   - Description template:
     ```
     {Episode summary / first chapter narration excerpt}

     Bölümler:
     0:00 {Chapter 1 title}
     {cumulative_time} {Chapter 2 title}
     ...

     #Bitcoin #Kripto #Türkçe #Eğitim
     ```
   - Chapter timestamps: computed from cumulative chapter durations (from TTS manifest actual durations)
   - Tags: `["Bitcoin", "Kripto", "Blockchain", "Türkçe", "Eğitim"]` + episode-specific tags
   - Store constructed metadata in `PublishJob.metadata_snapshot` (JSON)

9. **Add CLI commands** to `btcedu/cli.py`:

   - **`btcedu publish <episode_id>`** with `--force`, `--dry-run`, `--privacy unlisted|private|public`:
     - Validate episode is APPROVED
     - Run safety checks (display results)
     - Upload to YouTube (show progress)
     - Display YouTube URL on success

   - **`btcedu youtube-auth`**:
     - Run OAuth2 flow (opens browser for Google consent)
     - Save credentials to `YOUTUBE_CREDENTIALS_FILE`
     - Display success message with authenticated channel name

   - **`btcedu youtube-status`**:
     - Check if credentials exist and are valid
     - Display: authenticated (yes/no), channel name, token expiry

10. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
    - Ensure PUBLISHED is in `PipelineStage` enum (should exist from Sprint 1)
    - Update `resolve_pipeline_plan()` to include PUBLISH for v2 episodes after APPROVED
    - Position: APPROVED → PUBLISH → PUBLISHED → COMPLETED
    - PUBLISHED → COMPLETED transition (mark episode as fully processed)
    - Set `pipeline_version = 2` as operational: the full v2 pipeline is now complete

11. **Create dashboard publish UI** — extend episode detail view:
    - **Publish button**: visible only for APPROVED episodes
      - Confirmation dialog: "Publish to YouTube as unlisted?"
      - POST to publish endpoint
      - Show progress (uploading... → published)
    - **Published status**: show YouTube URL, video ID, published date
      - Direct link to YouTube video
    - **Publish history**: PublishJob records for this episode
      - Status, timestamps, error messages (if any)
    - **Safety check display**: show results of pre-publish checks before publishing
    - Link to YouTube from episode list (icon/badge for published episodes)

12. **Write tests**:
    - `tests/test_youtube_service.py`:
      - Mock Google API: upload success, quota error, network error
      - OAuth2 credential loading (mocked file system)
      - Resumable upload progress
      - Metadata update
      - Thumbnail upload
    - `tests/test_publisher.py`:
      - Pre-publish safety checks: all pass, each fails individually
      - Artifact integrity check: matching hash, mismatched hash, missing hash
      - Metadata construction: title, description with chapter timestamps, tags
      - Full publish flow with mocked YouTube service
      - Idempotency: already published → skip
      - Dry-run: checks run but no upload
      - Error handling: upload failure → PublishJob marked failed
    - `tests/test_publish_job_model.py`: CRUD for publish_jobs table
    - CLI tests: `btcedu publish --help`, `btcedu youtube-auth --help`
    - Pipeline tests: PUBLISH in v2 plan after APPROVED
    - **End-to-end pipeline test**: mock all external APIs, run episode through complete v2 pipeline from NEW to PUBLISHED, verify all stages execute and all review gates trigger

13. **Verify**:
    - Run `pytest tests/`
    - Run `btcedu migrate` (verify publish_jobs table created)
    - Set up YouTube OAuth: `btcedu youtube-auth`
    - Verify: `btcedu youtube-status` shows authenticated
    - Pick an APPROVED episode
    - Run `btcedu publish <ep_id> --dry-run`:
      - Verify all 4 safety checks pass
      - Verify metadata construction (title, description with chapter timestamps, tags)
      - No actual upload in dry-run
    - Run `btcedu publish <ep_id>`:
      - Verify upload progress shown
      - Verify YouTube URL returned
      - Verify PublishJob record created (status="published")
      - Verify Episode.youtube_video_id set
      - Verify episode status is PUBLISHED
      - Open the YouTube URL and verify the video is accessible (unlisted)
    - Run `btcedu publish <ep_id>` again → skipped (idempotent)
    - Run `btcedu status` → verify v1 pipeline unaffected
    - Run full v2 pipeline on a fresh episode → verify complete flow from NEW to PUBLISHED

### Anti-scope-creep guardrails

- **Do NOT** implement live streaming support.
- **Do NOT** implement YouTube analytics or comment management.
- **Do NOT** implement multi-channel publishing (single channel is sufficient).
- **Do NOT** implement scheduled publishing (YouTube supports this natively; configure manually).
- **Do NOT** implement video re-upload or update (publish is one-shot; re-publish requires manual intervention).
- **Do NOT** modify existing stages or review gates.
- **Do NOT** implement automatic privacy changes (unlisted → public). This should be a manual step.
- **Do NOT** over-engineer the OAuth flow — a simple CLI-based setup is sufficient.

### Code patterns to follow

- **Stage implementation**: Follow existing stage patterns (corrector, translator, etc.).
- **Service layer**: Follow `btcedu/services/image_gen_service.py` or `btcedu/services/elevenlabs_service.py` for API wrapper patterns.
- **Models**: Follow existing SQLAlchemy model patterns.
- **Migrations**: Follow existing migration patterns.
- **CLI commands**: Follow existing Click command patterns.
- **Dashboard**: Follow existing template and route patterns.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred (post-v1 features)
- Manual verification steps (including actual YouTube upload if credentials are available)
- **Final summary**: The complete v2 pipeline is now operational. Document the full flow:
  `NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → (RG1) → TRANSLATED → ADAPTED → (RG2) → CHAPTERIZED → IMAGES_GENERATED → TTS_DONE → RENDERED → (RG3) → APPROVED → PUBLISHED → COMPLETED`

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- **Safety checks are non-negotiable**: All 4 pre-publish checks must pass. Never upload without verification.
- **OAuth credentials must never be committed to git**: Store in `data/` directory, ensure it's in `.gitignore`.
- **Default privacy is `unlisted`**: Safe default. Only change to `public` explicitly.
- **Resumable upload is required**: Video files can be hundreds of MB. Use Google API's resumable upload.
- Chapter timestamps in YouTube description must start with `0:00` and use cumulative durations from TTS manifest.

---

## Definition of Done

- [ ] `publish_jobs` table created via migration
- [ ] `PublishJob` SQLAlchemy model exists and is registered
- [ ] `btcedu/services/youtube_service.py` exists with OAuth2 + upload + metadata methods
- [ ] OAuth2 flow works: `btcedu youtube-auth` authenticates with Google
- [ ] Credentials stored securely (not in git)
- [ ] Resumable upload works for large video files
- [ ] `btcedu/core/publisher.py` exists with `publish_video()` function
- [ ] Pre-publish safety checks implemented (all 4 per §5I):
  - [ ] Approval gate check (APPROVED status + RG3)
  - [ ] Artifact integrity check (SHA-256 match)
  - [ ] Metadata completeness check
  - [ ] Cost sanity check
- [ ] Safety checks abort on any failure with descriptive error
- [ ] YouTube metadata constructed: title, description with chapter timestamps, tags
- [ ] Chapter timestamps computed from cumulative TTS durations
- [ ] `btcedu publish <episode_id>` CLI works with `--force`, `--dry-run`, `--privacy`
- [ ] `btcedu youtube-auth` and `btcedu youtube-status` CLI commands work
- [ ] Pipeline plan includes PUBLISH for v2 episodes after APPROVED
- [ ] Episode status updated: APPROVED → PUBLISHED → COMPLETED
- [ ] Episode.youtube_video_id and PublishJob records stored
- [ ] Dashboard publish button visible for APPROVED episodes
- [ ] Dashboard shows YouTube URL after publishing
- [ ] Idempotency: already published → skip
- [ ] All tests pass (including end-to-end pipeline test)
- [ ] v1 pipeline unaffected
- [ ] **Full v2 pipeline is operational**: NEW → ... → PUBLISHED → COMPLETED

## Non-Goals

- Live streaming support
- YouTube analytics / comment management
- Multi-channel publishing
- Scheduled publishing automation
- Video re-upload / update mechanism
- Automatic privacy changes (unlisted → public)
- YouTube Shorts generation (separate content format)
- Monetization / ad settings management
- Playlist management
