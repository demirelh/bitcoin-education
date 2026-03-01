# Sprint 11 — Implementation Plan: YouTube Publishing + Safety Checks

> **Sprint**: 11 (Final)  
> **Phase**: 6 — YouTube Publishing  
> **Sources**: `MASTERPLAN.md` §4-Phase6, §5I, §7.3–7.4, §8-PUBLISH, §11; `docs/sprints/sprint11-plan.md`  
> **Date**: 2026-03-01

---

## 1. Sprint Scope Summary

Sprint 11 is the **final sprint** of the 11-sprint btcedu pipeline extension. It implements the **PUBLISH** stage — the last step of the v2 pipeline that uploads an approved draft video to YouTube with full metadata (title, description, tags, chapter timestamps), recording the YouTube video ID in the database. The sprint adds: a YouTube service layer with OAuth2 authentication, a publisher core module with four mandatory pre-publish safety checks, a `publish_jobs` database table (Migration 006), CLI commands for publishing and OAuth setup, pipeline integration (APPROVED → PUBLISHED), and a dashboard publish button with status display. All work is strictly additive; the v1 pipeline and all previous v2 stages remain untouched.

---

## 2. File-Level Plan

### New Files

| File | Purpose |
|------|---------|
| `btcedu/services/youtube_service.py` | YouTube Data API v3 wrapper: OAuth2, resumable upload, metadata |
| `btcedu/core/publisher.py` | Publish orchestration: safety checks, metadata prep, upload, DB records |
| `btcedu/models/publish_job.py` | `PublishJob` SQLAlchemy model |
| `tests/test_publisher.py` | Unit tests for publisher module |
| `tests/test_youtube_service.py` | Unit tests for YouTube service (mocked API) |

### Modified Files

| File | Changes |
|------|---------|
| `btcedu/migrations/__init__.py` | Add `CreatePublishJobsTableMigration` (006), append to `MIGRATIONS` |
| `btcedu/config.py` | Add YouTube-related settings fields |
| `btcedu/core/pipeline.py` | Add `("publish", EpisodeStatus.APPROVED)` to `_V2_STAGES`; add publish stage handler in `_run_stage()`; add `APPROVED` to `run_pending()` / `run_latest()` status filters |
| `btcedu/cli.py` | Add `publish`, `youtube-auth`, `youtube-status` CLI commands |
| `btcedu/web/api.py` | Add `/episodes/<eid>/publish` POST endpoint, `/episodes/<eid>/publish-status` GET endpoint |
| `btcedu/web/static/app.js` | Add publish button for APPROVED episodes, publish status display, YouTube link |
| `btcedu/web/static/styles.css` | Publish button + status styling |
| `btcedu/models/__init__.py` | Import `PublishJob` |
| `pyproject.toml` | Add `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib` to dependencies |

---

## 3. YouTube Service Design — `btcedu/services/youtube_service.py`

### Interface

```python
"""YouTube Data API v3 service: OAuth2, resumable upload, metadata."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class YouTubeUploadRequest:
    """Request for video upload to YouTube."""
    video_path: Path
    title: str
    description: str
    tags: list[str]
    category_id: str  # "27" = Education
    default_language: str  # "tr"
    privacy_status: str  # "unlisted" | "private" | "public"
    thumbnail_path: Path | None = None


@dataclass
class YouTubeUploadResponse:
    """Response from a successful YouTube upload."""
    video_id: str
    video_url: str  # https://youtu.be/{video_id}
    status: str  # "uploaded" | "processing"


class YouTubeService(Protocol):
    """Protocol for YouTube upload services."""
    def upload_video(
        self,
        request: YouTubeUploadRequest,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> YouTubeUploadResponse: ...

    def get_upload_status(self, video_id: str) -> dict: ...
```

### Implementation: `YouTubeDataAPIService`

- **OAuth2 setup flow**:
  - `authenticate(client_secrets_path, credentials_path)` — reads `client_secret.json` from config, opens browser for consent (via `google_auth_oauthlib.flow.InstalledAppFlow`), stores refresh token to `data/.youtube_credentials.json`.
  - `load_credentials(credentials_path)` — loads stored credentials, refreshes if expired via `google.auth.transport.requests.Request`.
  - Scopes: `["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube"]`.
  - `[ASSUMPTION]` Client secrets file path is configured via `youtube_client_secrets_path` setting.
  - `[ASSUMPTION]` Credentials file stored at `data/.youtube_credentials.json` (in `.gitignore`).

- **Resumable upload**:
  - Uses `googleapiclient.http.MediaFileUpload` with `resumable=True`, `chunksize=10*1024*1024` (10 MB chunks).
  - `_execute_upload(insert_request, progress_callback)` — loop calling `next_chunk()` until done, invoking progress_callback with `(bytes_sent, total_bytes)`.
  - Retry on `HttpError` with status 500/502/503/504 and on `ResumableUploadError` — exponential backoff up to 3 retries per chunk.
  - `[ASSUMPTION]` 10 MB chunk size is reasonable for Raspberry Pi memory constraints.

- **Metadata setting**:
  - Set via `videos().insert()` in the initial upload request body:
    - `snippet.title`, `snippet.description`, `snippet.tags`, `snippet.categoryId`, `snippet.defaultLanguage`
    - `status.privacyStatus` (configurable, defaults to `"unlisted"`)
  - Thumbnail upload via `thumbnails().set()` after video upload (optional).
  - `[ASSUMPTION]` Thumbnail is the first chapter image. If not present, skip thumbnail.

- **Error handling**:
  - `YouTubeUploadError(Exception)` — wraps API errors with descriptive messages.
  - `YouTubeAuthError(Exception)` — authentication/token errors.
  - `YouTubeQuotaError(Exception)` — 403 quotaExceeded handling.
  - Log all API interactions at DEBUG level.

- **Dry-run support**:
  - `DryRunYouTubeService` class implementing the same protocol, returns a placeholder `YouTubeUploadResponse` with `video_id="DRY_RUN"`.

---

## 4. Publisher Module Design — `btcedu/core/publisher.py`

### Function Signature

```python
@dataclass
class PublishResult:
    """Summary of publish operation for one episode."""
    episode_id: str
    youtube_video_id: str | None
    youtube_url: str | None
    publish_job_id: int | None
    safety_checks: dict[str, bool]  # check_name → passed
    skipped: bool = False
    dry_run: bool = False
    error: str | None = None


def publish_video(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    privacy: str = "unlisted",
) -> PublishResult:
    """Publish approved video to YouTube.

    Processing flow:
    1. Validate episode exists, is v2, status is APPROVED (or force)
    2. Idempotency check: if youtube_video_id is set, skip (already published)
    3. Run 4 pre-publish safety checks (abort if any fail)
    4. Prepare YouTube metadata from chapters.json + episode data
    5. Upload video via YouTube service (or dry-run placeholder)
    6. Record PublishJob in database
    7. Update Episode: youtube_video_id, published_at_youtube, status → PUBLISHED
    8. Write provenance file
    """
```

### Processing Flow Detail

1. **Episode validation**: look up Episode by `episode_id`, verify `pipeline_version == 2`, verify `status == APPROVED` (or force).
2. **Idempotency**: if `episode.youtube_video_id` is already set and not `force`, return `PublishResult(skipped=True)`.
3. **Pre-publish safety checks** (see §5 below). If any fail, raise `ValueError` with descriptive message.
4. **Metadata preparation**: call `_prepare_metadata(session, episode, settings)` to build `YouTubeUploadRequest`.
5. **Upload**: instantiate `YouTubeDataAPIService` (or `DryRunYouTubeService` if `settings.dry_run`), call `upload_video()`.
6. **Database records**:
   - Create `PublishJob` row (status=`"published"`, youtube_video_id, youtube_url, metadata_snapshot).
   - Update `Episode`: `youtube_video_id`, `published_at_youtube = utcnow()`, `status = PUBLISHED`.
7. **PipelineRun**: record PipelineRun for the publish stage (stage=`"publish"`, status=success, cost=0).
8. **Provenance**: write `data/outputs/{ep_id}/provenance/publish.json` with safety check results, YouTube response, metadata snapshot, timestamps.

### Error Recovery

- If upload fails mid-transfer: create `PublishJob` with status=`"failed"`, `error_message` set. Episode stays APPROVED.
- `[ASSUMPTION]` The Google API client handles resumable upload state internally. On retry (re-run `publish_video`), a new upload is started since we don't persist resumable upload URIs across process runs.
- `[ASSUMPTION]` Publishing cost is $0.00 (YouTube API is free to use within quota).

---

## 5. Pre-Publish Safety Checks

All 4 checks are implemented in `publisher.py` as `_run_safety_checks(session, episode, settings) -> dict[str, bool]`. A helper `_assert_all_checks_pass(results)` raises `ValueError` listing all failed checks.

### Check 1 — Approval Gate

```python
def _check_approval_gate(session: Session, episode: Episode) -> tuple[bool, str]:
    """Episode status is APPROVED and a ReviewTask(stage='render', status='approved') exists."""
    from btcedu.core.reviewer import has_approved_review
    if episode.status != EpisodeStatus.APPROVED:
        return False, f"Episode status is '{episode.status.value}', expected 'approved'"
    if not has_approved_review(session, episode.episode_id, "render"):
        return False, "No approved render review task found"
    return True, "Approval gate passed"
```

### Check 2 — Artifact Integrity

```python
def _check_artifact_integrity(session: Session, episode: Episode, settings: Settings) -> tuple[bool, str]:
    """SHA-256 of current draft.mp4 matches artifact_hash from approved ReviewTask."""
    from btcedu.models.review import ReviewTask, ReviewStatus
    task = (
        session.query(ReviewTask)
        .filter(ReviewTask.episode_id == episode.episode_id,
                ReviewTask.stage == "render",
                ReviewTask.status == ReviewStatus.APPROVED.value)
        .order_by(ReviewTask.created_at.desc())
        .first()
    )
    if not task or not task.artifact_hash:
        return False, "No artifact hash recorded on approved render review"

    draft_path = Path(settings.outputs_dir) / episode.episode_id / "render" / "draft.mp4"
    if not draft_path.exists():
        return False, f"Draft video not found: {draft_path}"

    current_hash = hashlib.sha256(draft_path.read_bytes()).hexdigest()
    if current_hash != task.artifact_hash:
        return False, (
            f"Artifact integrity check failed: "
            f"current={current_hash[:16]}... expected={task.artifact_hash[:16]}..."
        )
    return True, "Artifact integrity verified"
```

**Note**: `artifact_hash` is computed over all artifact paths (both `draft.mp4` and `chapters.json`) via `_compute_artifact_hash()` in `reviewer.py`. The integrity check here recomputes the same hash using the same sorted-path logic.

### Check 3 — Metadata Completeness

```python
def _check_metadata_completeness(
    title: str, description: str, tags: list[str]
) -> tuple[bool, str]:
    """Title, description, and tags are non-empty."""
    missing = []
    if not title or not title.strip():
        missing.append("title")
    if not description or not description.strip():
        missing.append("description")
    if not tags:
        missing.append("tags")
    if missing:
        return False, f"Missing metadata: {', '.join(missing)}"
    return True, "Metadata complete"
```

### Check 4 — Cost Sanity

```python
def _check_cost_sanity(session: Session, episode: Episode, settings: Settings) -> tuple[bool, str]:
    """Total episode cost is within max_episode_cost_usd."""
    from btcedu.models.episode import PipelineRun
    total_cost = (
        session.query(func.sum(PipelineRun.estimated_cost_usd))
        .filter(PipelineRun.episode_id == episode.episode_id)
        .scalar() or 0.0
    )
    if total_cost > settings.max_episode_cost_usd:
        return False, (
            f"Episode cost ${total_cost:.2f} exceeds "
            f"max_episode_cost_usd=${settings.max_episode_cost_usd:.2f}"
        )
    return True, f"Cost ${total_cost:.2f} within budget"
```

### Enforcement

```python
def _run_safety_checks(session, episode, settings, title, description, tags):
    results = {}
    results["approval_gate"] = _check_approval_gate(session, episode)
    results["artifact_integrity"] = _check_artifact_integrity(session, episode, settings)
    results["metadata_completeness"] = _check_metadata_completeness(title, description, tags)
    results["cost_sanity"] = _check_cost_sanity(session, episode, settings)

    failed = {k: v[1] for k, v in results.items() if not v[0]}
    if failed:
        msg = "Pre-publish safety checks failed:\n" + "\n".join(
            f"  ✗ {k}: {v}" for k, v in failed.items()
        )
        raise ValueError(msg)

    return {k: v[0] for k, v in results.items()}
```

All check results are logged and stored in the provenance file regardless of pass/fail.

---

## 6. `publish_jobs` Table — Migration 006

### Migration: `CreatePublishJobsTableMigration`

Added to `btcedu/migrations/__init__.py` as Migration 006.

```python
class CreatePublishJobsTableMigration(Migration):
    @property
    def version(self) -> str:
        return "006_create_publish_jobs"

    @property
    def description(self) -> str:
        return "Create publish_jobs table for YouTube publishing tracking"

    def up(self, session: Session) -> None:
        # Check-before-act idempotency
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='publish_jobs'")
        )
        if not result.fetchone():
            session.execute(text("""
                CREATE TABLE publish_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    youtube_video_id TEXT,
                    youtube_url TEXT,
                    metadata_snapshot TEXT,
                    published_at TIMESTAMP,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL
                )
            """))
            session.execute(text(
                "CREATE INDEX idx_publish_jobs_episode ON publish_jobs(episode_id)"
            ))
            session.execute(text(
                "CREATE INDEX idx_publish_jobs_status ON publish_jobs(status)"
            ))
            session.commit()
        self.mark_applied(session)
```

### SQLAlchemy Model: `btcedu/models/publish_job.py`

```python
"""PublishJob ORM model for tracking YouTube publishing."""

import enum
from datetime import UTC, datetime
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from btcedu.db import Base


class PublishJobStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=PublishJobStatus.PENDING.value)
    youtube_video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
```

**Design note**: `PublishJob` uses `btcedu.db.Base` (same as Episode, ReviewTask, etc.), NOT `MediaBase`. This is consistent with all non-media models.

---

## 7. YouTube Metadata Construction

Implemented as `_prepare_metadata()` in `publisher.py`:

```python
def _prepare_metadata(
    session: Session, episode: Episode, settings: Settings
) -> YouTubeUploadRequest:
    """Build YouTube upload metadata from chapters.json + episode data."""
```

### Title
- Read `chapters.json` → `ChapterDocument.title` if it exists.
- Fallback: `episode.title` (translated/adapted).
- `[ASSUMPTION]` The chapter document's top-level title (from chapterizer) is the video title.

### Description
Auto-generated, structured as:
```
{video_summary from chapters.json, or first chapter narration excerpt}

📋 Bölümler / Chapters:
0:00 {chapter_1_title}
{cumulative_timestamp} {chapter_2_title}
...

🔔 Abone ol / Subscribe
💬 Bitcoin eğitimi | Bitcoin Education

#Bitcoin #Kripto #Eğitim #Türkçe
```

- Chapter timestamps formatted as `M:SS` or `H:MM:SS` depending on total duration.
- First timestamp MUST be `0:00` for YouTube chapter auto-detection.
- `[ASSUMPTION]` Description is generated in Turkish to match video language.

### Tags
- Base tags: `["Bitcoin", "Kripto", "Eğitim", "Türkçe", "Cryptocurrency", "Education"]`
- Episode-specific: extracted from chapter titles (split on spaces, filter common words).
- Max 500 characters total (YouTube limit).
- `[ASSUMPTION]` A static base tag list is sufficient; no LLM-based tag generation.

### Category
- `"27"` (Education) — hardcoded.

### Language
- `"tr"` (Turkish) — hardcoded.

### Privacy
- Default: `"unlisted"` (safe default per sprint plan).
- Configurable via `--privacy` CLI flag and `youtube_default_privacy` config setting.

### Thumbnail
- Use first chapter image (`data/outputs/{ep_id}/images/{first_chapter_id}.png`) if it exists.
- `[ASSUMPTION]` No dedicated thumbnail generation in this sprint. Use chapter image as-is.

---

## 8. Config Changes — `btcedu/config.py`

Add the following fields to `Settings`:

```python
# YouTube Publishing (Sprint 11)
youtube_client_secrets_path: str = "data/client_secret.json"
youtube_credentials_path: str = "data/.youtube_credentials.json"
youtube_default_privacy: str = "unlisted"  # "unlisted" | "private" | "public"
youtube_upload_chunk_size_mb: int = 10
youtube_category_id: str = "27"  # Education
youtube_default_language: str = "tr"
```

**No new API key fields** — YouTube uses OAuth2 with client secrets file, not a simple API key.

---

## 9. CLI Commands — `btcedu/cli.py`

### `btcedu publish <episode_id>` 

```python
@cli.command()
@click.argument("episode_ids", nargs=-1, required=True)
@click.option("--force", is_flag=True, default=False, help="Re-publish even if already published.")
@click.option("--dry-run", is_flag=True, default=False, help="Validate and prepare without uploading.")
@click.option("--privacy", type=click.Choice(["unlisted", "private", "public"]),
              default=None, help="YouTube privacy status (default: from config).")
@click.pass_context
def publish(ctx, episode_ids, force, dry_run, privacy):
    """Publish approved video to YouTube (v2 pipeline, Sprint 11)."""
```

Follows the same pattern as `render`, `tts`, `correct`, etc. — iterates over `episode_ids`, calls `publish_video()`, prints results.

### `btcedu youtube-auth`

```python
@cli.command("youtube-auth")
@click.pass_context
def youtube_auth(ctx):
    """Set up YouTube OAuth2 credentials (interactive, opens browser)."""
```

- Reads `youtube_client_secrets_path` from settings.
- Runs the OAuth2 consent flow via `InstalledAppFlow.from_client_secrets_file()`.
- Saves credentials to `youtube_credentials_path`.
- Prints success message with token expiry info.

### `btcedu youtube-status`

```python
@cli.command("youtube-status")
@click.pass_context
def youtube_status(ctx):
    """Check YouTube OAuth2 token validity and channel info."""
```

- Loads credentials, checks if valid/expired.
- Attempts a lightweight API call (`channels.list(mine=True)`) to verify access.
- Prints: channel name, token expiry, quota usage if available.

---

## 10. Pipeline Integration — `btcedu/core/pipeline.py`

### `_V2_STAGES` Update

```python
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate", EpisodeStatus.CORRECTED),
    ("adapt", EpisodeStatus.TRANSLATED),
    ("review_gate_2", EpisodeStatus.ADAPTED),
    ("chapterize", EpisodeStatus.ADAPTED),
    ("imagegen", EpisodeStatus.CHAPTERIZED),
    ("tts", EpisodeStatus.IMAGES_GENERATED),
    ("render", EpisodeStatus.TTS_DONE),
    ("review_gate_3", EpisodeStatus.RENDERED),
    ("publish", EpisodeStatus.APPROVED),   # ← NEW (Sprint 11)
]
```

### `_run_stage()` — Publish Handler

```python
elif stage_name == "publish":
    from btcedu.core.publisher import publish_video

    result = publish_video(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0

    if result.skipped:
        return StageResult("publish", "skipped", elapsed, detail="already published")
    elif result.dry_run:
        return StageResult("publish", "success", elapsed, detail="dry-run: checks passed")
    else:
        return StageResult(
            "publish",
            "success",
            elapsed,
            detail=f"published as {result.youtube_video_id}",
        )
```

### `run_pending()` / `run_latest()` — Add APPROVED to Filter

Add `EpisodeStatus.APPROVED` to both status filter lists so that approved episodes are picked up for publishing.

### Cost Extraction

Add `"publish"` to the `success_stages` tuple in `run_episode_pipeline()`. (Publishing cost is $0, but the pattern should be consistent.)

---

## 11. Dashboard Publish UI

### API Endpoints — `btcedu/web/api.py`

**`POST /api/episodes/<eid>/publish`** — Trigger publish job via `JobManager`.

```python
@api_bp.route("/episodes/<episode_id>/publish", methods=["POST"])
def publish_episode_route(episode_id: str):
    """Trigger YouTube publish for an approved episode."""
    body = request.get_json(silent=True) or {}
    privacy = body.get("privacy", None)
    dry_run = body.get("dry_run", False)
    # Run via JobManager (background)
    ...
```

**`GET /api/episodes/<eid>/publish-status`** — Return publishing status and YouTube link.

```python
@api_bp.route("/episodes/<episode_id>/publish-status")
def publish_status_route(episode_id: str):
    """Return publish status for an episode."""
    # Query PublishJob for this episode
    # Return: status, youtube_video_id, youtube_url, published_at, error_message
```

### Frontend — `btcedu/web/static/app.js`

1. **Publish button**: Rendered on episode detail panel for episodes with `status === "approved"`. Button text: "📤 Publish to YouTube". Clicking triggers `POST /api/episodes/{eid}/publish` via JobManager.
2. **Publish status display**: For episodes with `status === "published"`, show:
   - YouTube video ID with link (`https://youtu.be/{id}`)
   - Published timestamp
   - Privacy status
3. **Publishing progress**: While job is running, show spinner + "Publishing to YouTube...".
4. **Error display**: If publish failed, show error message with retry button.

### Styling — `btcedu/web/static/styles.css`

- `.publish-btn` — primary action button style (Bitcoin orange `#F7931A`).
- `.publish-status` — status badge (green for published, yellow for uploading, red for failed).
- `.youtube-link` — styled external link to YouTube video.

---

## 12. Provenance & Idempotency

### Provenance File

Written to: `data/outputs/{ep_id}/provenance/publish.json`

```json
{
  "episode_id": "abc123",
  "published_at": "2026-03-01T12:00:00Z",
  "youtube_video_id": "dQw4w9WgXcQ",
  "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
  "privacy_status": "unlisted",
  "safety_checks": {
    "approval_gate": [true, "Approval gate passed"],
    "artifact_integrity": [true, "Artifact integrity verified"],
    "metadata_completeness": [true, "Metadata complete"],
    "cost_sanity": [true, "Cost $3.42 within budget"]
  },
  "metadata_snapshot": {
    "title": "...",
    "description": "...",
    "tags": ["..."],
    "category_id": "27",
    "language": "tr"
  },
  "dry_run": false
}
```

### Idempotency

- **Already done**: `episode.youtube_video_id` is not `None` → skip (return `PublishResult(skipped=True)`).
- **Force re-publish**: `--force` flag bypasses the skip check. `[ASSUMPTION]` Force re-publish creates a NEW YouTube video (the old one is not deleted — YouTube doesn't support in-place replacement). The new video ID overwrites the old in the database.
- Publishing is effectively **irreversible** — there is no un-publish stage. Manual deletion on YouTube is the only option.

### Cascade Invalidation

- Nothing depends on the PUBLISH stage, so no downstream invalidation needed.
- If upstream stages are re-run (e.g., render re-done), the episode status reverts, which means a new review gate 3 cycle is required before re-publishing.

---

## 13. Safety & Security

1. **OAuth credentials**: Stored in `data/` directory which is in `.gitignore`. Never logged or committed.
2. **Client secrets**: `client_secret.json` must be manually placed in `data/` directory. Not generated or downloaded by the code.
3. **API quotas**: YouTube Data API default quota is 10,000 units/day. Video upload costs 1,600 units. Handle `HttpError(403)` with `quotaExceeded` reason gracefully — log, set PublishJob to `failed`, keep episode at APPROVED.
4. **Privacy default**: `"unlisted"` — safe default. Must explicitly use `--privacy public` to make video publicly visible.
5. **All 4 safety checks are non-negotiable**: No `--skip-checks` flag. Even `--force` runs all checks.

---

## 14. Test Plan

### `tests/test_publisher.py`

| # | Test | Description |
|---|------|-------------|
| 1 | `test_publish_video_success` | Mock YouTube service, valid APPROVED episode → PublishResult with video_id |
| 2 | `test_publish_video_idempotent_skip` | Episode already has youtube_video_id → skipped=True |
| 3 | `test_publish_video_force_repub` | Episode has youtube_video_id + force=True → new publish |
| 4 | `test_publish_video_wrong_status` | Episode not APPROVED → ValueError |
| 5 | `test_publish_video_v1_rejected` | pipeline_version=1 → ValueError |
| 6 | `test_safety_check_approval_gate_pass` | APPROVED status + approved render review → pass |
| 7 | `test_safety_check_approval_gate_fail_status` | Status not APPROVED → fail |
| 8 | `test_safety_check_approval_gate_fail_no_review` | APPROVED but no approved review task → fail |
| 9 | `test_safety_check_artifact_integrity_pass` | Hash matches → pass |
| 10 | `test_safety_check_artifact_integrity_fail_mismatch` | File modified after approval → fail |
| 11 | `test_safety_check_artifact_integrity_fail_missing` | draft.mp4 missing → fail |
| 12 | `test_safety_check_metadata_completeness_pass` | All metadata present → pass |
| 13 | `test_safety_check_metadata_completeness_fail` | Missing title → fail |
| 14 | `test_safety_check_cost_sanity_pass` | Cost within budget → pass |
| 15 | `test_safety_check_cost_sanity_fail` | Cost exceeds budget → fail |
| 16 | `test_publish_video_dry_run` | dry_run=True → no upload, checks pass, result.dry_run=True |
| 17 | `test_publish_creates_pipeline_run` | Verify PipelineRun record created for publish stage |
| 18 | `test_publish_creates_publish_job` | Verify PublishJob row in DB with correct fields |
| 19 | `test_publish_updates_episode` | Episode.youtube_video_id, published_at_youtube, status=PUBLISHED set |
| 20 | `test_publish_writes_provenance` | Provenance JSON written with safety check results |
| 21 | `test_prepare_metadata_from_chapters` | Verify title/description/tags/timestamps built correctly |
| 22 | `test_prepare_metadata_chapter_timestamps` | Chapter timestamps formatted correctly (0:00 first) |
| 23 | `test_publish_upload_failure_records_error` | YouTube API error → PublishJob.status=failed, episode stays APPROVED |
| 24 | `test_all_checks_must_pass` | One failed check → ValueError with descriptive message |

### `tests/test_youtube_service.py`

| # | Test | Description |
|---|------|-------------|
| 1 | `test_load_credentials_valid` | Mock valid credentials file → credentials loaded |
| 2 | `test_load_credentials_expired_refresh` | Expired + refresh token → auto-refresh |
| 3 | `test_load_credentials_missing_file` | No credentials file → YouTubeAuthError |
| 4 | `test_upload_video_success` | Mock API → YouTubeUploadResponse with video_id |
| 5 | `test_upload_video_resumable_retry` | Transient 503 on chunk → retry succeeds |
| 6 | `test_upload_video_quota_exceeded` | 403 quotaExceeded → YouTubeQuotaError |
| 7 | `test_upload_video_progress_callback` | Verify progress_callback invoked |
| 8 | `test_dry_run_service` | DryRunYouTubeService returns placeholder |
| 9 | `test_thumbnail_upload` | Mock thumbnail upload after video |

### `tests/test_pipeline.py` (additions)

| # | Test | Description |
|---|------|-------------|
| 1 | `test_v2_stages_includes_publish` | Verify `_V2_STAGES` ends with `("publish", APPROVED)` |
| 2 | `test_run_stage_publish` | Mock publisher, APPROVED episode → success StageResult |
| 3 | `test_pipeline_approved_in_pending_filter` | APPROVED in `run_pending()` status filter |

### Migration Tests (additions to `tests/test_migrations.py`)

| # | Test | Description |
|---|------|-------------|
| 1 | `test_006_creates_publish_jobs_table` | Migration creates table with expected columns |
| 2 | `test_006_idempotent` | Running migration twice doesn't error |

---

## 15. Implementation Order

| Step | Task | Files | Depends On |
|------|------|-------|------------|
| 1 | Add YouTube config settings | `config.py` | — |
| 2 | Create `PublishJob` model | `models/publish_job.py`, `models/__init__.py` | — |
| 3 | Create Migration 006 | `migrations/__init__.py` | Step 2 |
| 4 | Create YouTube service | `services/youtube_service.py` | Step 1 |
| 5 | Create publisher module | `core/publisher.py` | Steps 1–4 |
| 6 | Write publisher tests | `tests/test_publisher.py` | Step 5 |
| 7 | Write YouTube service tests | `tests/test_youtube_service.py` | Step 4 |
| 8 | Integrate into pipeline | `core/pipeline.py` | Step 5 |
| 9 | Add CLI commands | `cli.py` | Steps 4–5 |
| 10 | Add API endpoints | `web/api.py` | Step 5 |
| 11 | Add dashboard UI | `web/static/app.js`, `web/static/styles.css` | Step 10 |
| 12 | Add migration tests | `tests/test_migrations.py` | Step 3 |
| 13 | Add pipeline tests | `tests/test_pipeline.py` | Step 8 |
| 14 | Add `pyproject.toml` deps | `pyproject.toml` | — |
| 15 | Validation & cleanup | All | Steps 1–14 |

---

## 16. Definition of Done

- [ ] `btcedu migrate` applies Migration 006 cleanly (creates `publish_jobs` table).
- [ ] `btcedu youtube-auth` completes OAuth2 flow and stores credentials.
- [ ] `btcedu youtube-status` reports token validity.
- [ ] `btcedu publish <eid>` uploads video to YouTube for an APPROVED episode.
- [ ] `btcedu publish <eid> --dry-run` validates all 4 safety checks without uploading.
- [ ] All 4 pre-publish safety checks enforced: approval gate, artifact integrity, metadata completeness, cost sanity.
- [ ] Any failed safety check aborts publish with descriptive error.
- [ ] Published episode has `youtube_video_id`, `published_at_youtube` set, status=PUBLISHED.
- [ ] `PublishJob` row created in database with metadata_snapshot.
- [ ] Provenance file written to `data/outputs/{ep_id}/provenance/publish.json`.
- [ ] Idempotency: re-running `publish` on a published episode skips (unless `--force`).
- [ ] Pipeline integration: `run_pending()` picks up APPROVED episodes and publishes them.
- [ ] Dashboard: Publish button visible for APPROVED episodes.
- [ ] Dashboard: YouTube link displayed for PUBLISHED episodes.
- [ ] Privacy default is `unlisted` (configurable).
- [ ] OAuth credentials never committed to git.
- [ ] v1 pipeline unaffected (backward compatibility).
- [ ] All tests pass: `pytest tests/test_publisher.py tests/test_youtube_service.py`.
- [ ] Existing tests unbroken: `pytest tests/` passes.
- [ ] **Capstone**: Full v2 pipeline flow documented in docstring: NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [RG1] → TRANSLATED → ADAPTED → [RG2] → CHAPTERIZED → IMAGES_GENERATED → TTS_DONE → RENDERED → [RG3] → APPROVED → PUBLISHED.

---

## 17. Non-Goals

1. **YouTube Analytics integration** — Not in scope. Only upload and record video ID.
2. **Scheduled publishing** — No cron-based auto-publish. Publish is triggered manually or by pipeline.
3. **Multi-account YouTube support** — One YouTube channel per deployment.
4. **Video deletion** — No API for deleting published videos. Manual-only.
5. **Live streaming** — Upload only, no live broadcast.
6. **Privacy status changes post-upload** — No API to change from `unlisted` to `public` after upload. Manual-only. `[ASSUMPTION]` This can be added later as a simple CLI command but is not Sprint 11 scope.
7. **Playlist management** — Not creating or managing YouTube playlists.
8. **Comment management** — No interaction with YouTube comments.
9. **v1 pipeline publishing** — Only v2 pipeline episodes can be published.
10. **Re-factoring existing stages** — All changes are additive.
11. **Subtitle/caption upload** — Not uploading `.srt` files to YouTube (can be added later).
12. **Background music mixing** — Not adding background music to the video before publish.
13. **End-to-end integration test with real YouTube API** — Tests use mocked YouTube service. Manual verification on staging YouTube account is advised but not automated.

---

## 18. Assumptions Summary

| ID | Assumption | Impact |
|----|------------|--------|
| A1 | Google API client libraries (`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`) added as dependencies | `pyproject.toml` change |
| A2 | Client secrets file manually placed at `data/client_secret.json` | Documentation needed |
| A3 | Credentials stored at `data/.youtube_credentials.json` (gitignored) | Security |
| A4 | 10 MB upload chunk size suitable for Raspberry Pi | Configurable via settings |
| A5 | Initial privacy is `unlisted` | Safe default |
| A6 | `youtube_video_id` and `published_at_youtube` columns already exist on Episode (Migration 002) | No new episode columns needed |
| A7 | Publishing cost is $0.00 (YouTube API is free within quota) | No cost tracking for this stage |
| A8 | Force re-publish creates a new video (no in-place update) | Old video stays on YouTube |
| A9 | First chapter image used as thumbnail (no dedicated thumbnail generation) | Simplicity |
| A10 | Static base tag list sufficient; no LLM-based tag generation | No API cost |
| A11 | Chapter document top-level title used as video title | From chapterizer output |
| A12 | Resumable upload state not persisted across processes | New upload on retry |
| A13 | `artifact_hash` recomputation uses `_compute_artifact_hash()` from reviewer with same sorted paths | Consistency |

---

## 19. Complete v2 Pipeline Flow (Capstone Reference)

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

**12 stages + 3 review gates = 15 pipeline steps, fully automated with human review at critical junctures.**
