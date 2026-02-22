# Sprint 11 — Planning Prompt (YouTube Publishing + Safety Checks)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–10 completed codebase (especially `btcedu/core/reviewer.py` for review gate patterns, `btcedu/core/pipeline.py`, `btcedu/models/review.py` for `artifact_hash`, `btcedu/config.py`, `btcedu/services/`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the YouTube service, publisher module, OAuth2 flow, `publish_jobs` table, pre-publish safety checks, CLI command, pipeline integration, dashboard publish UI, and tests.

---

## Context

You are planning **Sprint 11 (Phase 6: YouTube Publishing)** of the btcedu video production pipeline extension. This is the **final sprint** of the 11-sprint implementation program.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–10 are complete:
- Foundation, Correction (RG1), Review System, Translation, Adaptation (RG2), Chapterization, Image Generation, TTS, Render Pipeline, Review Gate 3 — all functional.
- The pipeline can take an episode from NEW through APPROVED: detect → download → transcribe → correct → (RG1) → translate → adapt → (RG2) → chapterize → imagegen → tts → render → (RG3) → APPROVED.
- `artifact_hash` is recorded on RG3 approval for tamper-evident publishing.

Sprint 11 implements the **PUBLISH** stage — uploading the approved video to YouTube with proper metadata. This is the final stage of the v2 pipeline and the culmination of the entire project.

### Sprint 11 Focus (from MASTERPLAN.md §4 Phase 6, §5I)

1. Create YouTube service (`btcedu/services/youtube_service.py`) using YouTube Data API v3.
2. Implement OAuth2 flow for YouTube authentication (stored credentials).
3. Implement `publish_video()` in `btcedu/core/publisher.py`.
4. Create `publish_jobs` table (Migration N+5 per §7.4).
5. Implement **pre-publish safety checks** (§5I):
   - Approval gate: episode is APPROVED (RG3 approved)
   - Artifact integrity: SHA-256 of `draft.mp4` matches `artifact_hash` from RG3 approval
   - Metadata completeness: title, description, tags are non-empty
   - Cost sanity: total episode cost within budget
6. Resumable upload for large video files.
7. YouTube metadata from chapters/publishing context: title, description, tags, category, language, chapter timestamps.
8. Record YouTube video ID in database (Episode and PublishJob).
9. Add `publish` CLI command with `--force`, `--dry-run`.
10. Integrate into pipeline — PUBLISH after APPROVED, final status → PUBLISHED → COMPLETED.
11. Add dashboard publish button and status display.
12. Write tests.

### Relevant Subplans

- **Subplan 5I** (YouTube Publishing Integration) — all slices: YouTube service with OAuth2, video upload with resumable upload, metadata setting, CLI command, dashboard publish button.
- **§5I Pre-Publish Safety Checks** — approval gate, artifact integrity, metadata completeness, cost sanity.
- **§7.3** (New Tables) — `publish_jobs` table schema.
- **§7.4** (Migration Sequencing) — Migration N+5 for `publish_jobs`.
- **§8** (Idempotency) — PUBLISH stage: already done = youtube_video_id is set. Invalidated by manual decision only.
- **§3.7** (Failure Handling) — retry with exponential backoff for API failures.
- **§7.2** (Episode Model Extension) — `youtube_video_id`, `published_at_youtube` fields.
- **§11** (Decision Matrix) — YouTube API quotas: 10,000 units/day, upload = 1600 units.

---

## Your Task

Produce a detailed implementation plan for Sprint 11. The plan must include:

1. **Sprint Scope Summary** — one paragraph. This is the final sprint: YouTube publishing with safety checks.
2. **File-Level Plan** — for every file that will be created or modified.
3. **YouTube Service Design** — `btcedu/services/youtube_service.py`:
   - YouTube Data API v3 client setup
   - OAuth2 authentication:
     - Initial setup flow: `btcedu youtube-auth` CLI command opens browser for Google OAuth consent
     - Token storage: `data/.youtube_credentials.json` (refresh token for subsequent use)
     - Token refresh: automatic refresh on expiry
     - Client secrets: from config / environment variables
   - Upload method:
     - Resumable upload for large files (YouTube API supports this)
     - Progress callback (for CLI output)
     - Retry on network errors (exponential backoff, up to 3 retries)
   - Metadata setting:
     - Title, description, tags, category (Education = 27)
     - Language: Turkish (`tr`)
     - Chapter timestamps in description (from chapters.json)
     - Thumbnail upload (optional: from first chapter image or dedicated thumbnail)
     - Privacy: initially set to `unlisted` or `private` (configurable), then publish
   - Return: YouTube video ID and URL
4. **Publisher Module Design** — `btcedu/core/publisher.py`:
   - `publish_video()` function signature and return type (`PublishResult` dataclass)
   - Processing flow:
     a. Pre-publish safety checks (4 checks per §5I)
     b. Prepare metadata from chapter JSON / episode data
     c. Upload video via YouTube service
     d. Set metadata (title, description, tags, chapters)
     e. Record PublishJob in database
     f. Update Episode with youtube_video_id
     g. Update episode status: APPROVED → PUBLISHED → COMPLETED
   - Dry-run support: validate everything without actually uploading
5. **Pre-Publish Safety Checks** (critical — from §5I):
   - **Check 1 — Approval gate**: Episode status is APPROVED. A ReviewTask (stage="render") with status=APPROVED exists.
   - **Check 2 — Artifact integrity**: SHA-256 of current `draft.mp4` matches `ReviewTask.artifact_hash`. This verifies the video hasn't been modified since approval.
   - **Check 3 — Metadata completeness**: Title, description, and tags are non-empty strings.
   - **Check 4 — Cost sanity**: Total episode cost (`sum of PipelineRun.estimated_cost_usd`) is within `max_episode_cost_usd`.
   - If ANY check fails: abort with descriptive error. Never silently proceed.
   - Log all check results for audit trail.
6. **`publish_jobs` Table** — migration and SQLAlchemy model per §7.3:
   - Fields: id, episode_id, status (pending/uploading/published/failed), youtube_video_id, youtube_url, metadata_snapshot (JSON), published_at, error_message
   - Index: episode_id, status
7. **YouTube Metadata Construction**:
   - Title: from chapter JSON title or episode title
   - Description: auto-generated from chapter summaries + chapter timestamps
   - Tags: from episode content (Bitcoin, crypto, Turkish education, etc.)
   - Chapter timestamps: formatted as `HH:MM:SS Chapter Title` in description (YouTube auto-detects)
   - Category: 27 (Education)
   - Language: tr (Turkish)
8. **CLI Commands**:
   - `btcedu publish <episode_id>` with `--force`, `--dry-run`, `--privacy unlisted|private|public`
   - `btcedu youtube-auth` — interactive OAuth2 setup (opens browser)
   - `btcedu youtube-status` — check OAuth token validity
9. **Pipeline Integration** — APPROVED → PUBLISH → PUBLISHED → COMPLETED.
10. **Dashboard Publish UI**:
    - Publish button on episode detail (only visible for APPROVED episodes)
    - Publish status display (uploading progress, published, failed)
    - YouTube link displayed after publishing
    - Publish history (timestamps, metadata snapshot)
11. **Provenance, Idempotency**:
    - Idempotency: if `youtube_video_id` exists, skip (already published). Publishing is effectively irreversible.
    - Provenance: record publishing metadata, safety check results, YouTube response.
12. **Safety & Security**:
    - OAuth credentials never committed to git (stored in data/ directory, in .gitignore)
    - API quotas: check remaining quota before upload (or handle 403 gracefully)
    - Privacy default: `unlisted` (safe default, change to `public` explicitly)
    - No publishing without all 4 safety checks passing
13. **Test Plan** — list each test.
14. **Implementation Order** — numbered sequence.
15. **Definition of Done** — checklist.
16. **Non-Goals** — explicit list.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected.
- **Safety first**: The pre-publish safety checks are non-negotiable. All 4 must pass before upload. This is the last gate before public content.
- **Follow existing patterns**: Publisher should mirror existing stage patterns.
- **OAuth2 complexity**: OAuth2 for YouTube is inherently interactive (browser consent). Plan for a one-time setup flow via CLI.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections. Include the YouTube service interface, PublishJob schema, safety check implementation, metadata construction logic, OAuth2 flow, and dashboard UI description.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- `[ASSUMPTION]`: The Google API client libraries (`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`) will be added as dependencies.
- `[ASSUMPTION]`: YouTube API quota limits are checked before upload. If quota is insufficient, the publish stage aborts with a clear error rather than failing mid-upload.
- `[ASSUMPTION]`: Initial privacy is `unlisted`. A separate CLI command or dashboard button can change to `public` after manual verification on YouTube.
- `[ASSUMPTION]`: The `youtube_video_id` and `published_at_youtube` fields on Episode were already added in Sprint 1's migration. If not, add them in this sprint's migration.
- Consider what happens if publishing fails mid-upload (network error): the PublishJob should track partial upload state and support resumption.
- Consider YouTube chapter timestamps format: `0:00 Intro\n1:05 Chapter 2\n...` in the description. First timestamp must be `0:00`.
- This is the final sprint. The Definition of Done should include a **full end-to-end pipeline test**: episode goes from NEW through all stages to PUBLISHED, passing all three review gates.
- Document the complete v2 pipeline flow in a summary comment or docstring as a capstone deliverable.
