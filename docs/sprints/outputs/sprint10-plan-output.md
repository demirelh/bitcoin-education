# Sprint 10 Implementation Plan — Render Polish + Review Gate 3 + Video Preview

**Sprint**: 10 (Phase 5, Part 2)
**Planned by**: Claude Opus 4.6
**Date**: 2026-03-01
**Sources**: `MASTERPLAN.md` §3.3, §5G, §5H, §5I, §9.3; `docs/sprints/sprint10-plan.md`

---

## Context

Sprint 9 delivered the core render pipeline: per-chapter video segments with text overlays, concatenated into a `draft.mp4`. However, transitions between chapters are cut-only, there's no human review gate before publishing, and the dashboard can't preview videos. Sprint 10 completes Phase 5 by adding fade transitions, Review Gate 3 (the final approval before YouTube publishing), and an embedded video player in the dashboard.

## Sprint Scope Summary

Sprint 10 adds: (1) fade-in/fade-out transitions via ffmpeg `fade`/`afade` filters applied per-segment, controlled by chapter JSON's `TransitionType`; (2) Review Gate 3 in the pipeline after RENDERED, creating a ReviewTask for video review that blocks until approval; (3) dashboard video preview with HTML5 `<video>` player in both the episode detail panel and review detail view; (4) review gate hardening — fixing `_revert_episode()` to handle all three gates; (5) missing CLI `review request-changes` command; (6) artifact hash (SHA-256 of draft.mp4) on RG3 approval for Sprint 11's pre-publish safety check.

---

## 1. File-Level Plan

### 1A. `btcedu/config.py` — Add transition setting

Add after existing render fields (~line 98):
```python
render_transition_duration: float = 0.5  # seconds for fade in/out
```

Maps to env var `RENDER_TRANSITION_DURATION`.

---

### 1B. `btcedu/services/ffmpeg_service.py` — Fade transition filters

**Add parameters** to `create_segment()`:
```python
def create_segment(
    ...,
    fade_in_duration: float = 0.0,   # NEW
    fade_out_duration: float = 0.0,  # NEW
) -> SegmentResult:
```

**Inject video fade filters** into the `filter_complex` chain. After the overlay/copy chain produces the final `[v]` label:
- If fades are needed, rename final label from `[v]` to `[pre_fade]`
- Append: `[pre_fade]fade=t=in:st=0:d={fade_in_dur},fade=t=out:st={dur-fade_out_dur}:d={fade_out_dur}[v]`
- Only include the specific fade(s) requested (fade_in, fade_out, or both)

**Add audio fade** via `-af` parameter (after building cmd, before dry_run check):
```python
audio_filters = []
if fade_in_duration > 0:
    audio_filters.append(f"afade=t=in:st=0:d={fade_in_duration}")
if fade_out_duration > 0:
    audio_filters.append(f"afade=t=out:st={max(0, duration - fade_out_duration)}:d={fade_out_duration}")
# Insert -af into cmd before output_path
```

**Why this works**: Fade filters are applied during encoding of each segment (which already happens via libx264). The concat demuxer with `-c copy` stream-copies the pre-encoded segments unchanged.

**Transition type mapping**:
- `FADE` → apply fade with `render_transition_duration` seconds
- `CUT` → no fade (0.0 duration, current default behavior)
- `DISSOLVE` → treated same as `FADE` for Sprint 10 [ASSUMPTION]

**filter_complex chain modification**:
```
Current:  [0:v]scale+pad[scaled]; [scaled]drawtext...[v]
With fade: [0:v]scale+pad[scaled]; [scaled]drawtext...[pre_fade]; [pre_fade]fade=in...,fade=out...[v]
```

---

### 1C. `btcedu/core/renderer.py` — Wire transitions to ffmpeg

In the per-chapter loop (~line 207), after building `overlay_specs`:
```python
fade_in_dur = settings.render_transition_duration if chapter.transitions.in_transition.value in ("fade", "dissolve") else 0.0
fade_out_dur = settings.render_transition_duration if chapter.transitions.out_transition.value in ("fade", "dissolve") else 0.0
```

Pass to `create_segment(..., fade_in_duration=fade_in_dur, fade_out_duration=fade_out_dur)`.

Add `"transition_duration": settings.render_transition_duration` to the render manifest JSON (~line 292).

---

### 1D. `btcedu/core/reviewer.py` — Fix revert + video review detail

**Fix `_revert_episode()` (BUG FIX)** — currently only handles CORRECTED→TRANSCRIBED (line 39-58). Replace with a map covering all three gates:
```python
_REVERT_MAP = {
    EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,   # RG1
    EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,       # RG2
    EpisodeStatus.RENDERED: EpisodeStatus.TTS_DONE,        # RG3
}
target = _REVERT_MAP.get(episode.status)
if target:
    episode.status = target
```

**Extend `get_review_detail()`** — when `task.stage == "render"`, add to the returned dict:
- `video_url`: `/api/episodes/{episode_id}/render/draft.mp4` (if draft.mp4 exists)
- `render_manifest`: parsed render_manifest.json (if it exists)

---

### 1E. `btcedu/core/pipeline.py` — Review Gate 3 + filter updates

**Add to `_V2_STAGES`** (after render stage, ~line 65):
```python
("review_gate_3", EpisodeStatus.RENDERED),  # Sprint 10
```

**Add `review_gate_3` handler in `_run_stage()`** — follows exact pattern of review_gate_1/2:
1. `has_approved_review(session, episode_id, "render")` → set `episode.status = APPROVED`, return success
2. `has_pending_review()` → return `StageResult("review_gate_3", "review_pending", ...)`
3. Create `ReviewTask(stage="render", artifact_paths=[draft.mp4], diff_path=render_manifest.json)`

**Key difference from RG1/RG2**: On approval, RG3 sets `episode.status = APPROVED` directly since there is no subsequent stage to do this.

**Add `EpisodeStatus.RENDERED` to `run_pending()` filter** (~line 602):
```python
EpisodeStatus.TTS_DONE,    # Sprint 9: render stage
EpisodeStatus.RENDERED,     # Sprint 10: review gate 3
```

**Add `EpisodeStatus.TTS_DONE` and `EpisodeStatus.RENDERED` to `run_latest()` filter** (~line 675 — currently missing both):
```python
EpisodeStatus.IMAGES_GENERATED,
EpisodeStatus.TTS_DONE,    # Sprint 9
EpisodeStatus.RENDERED,     # Sprint 10
```

---

### 1F. `btcedu/web/api.py` — Video serving endpoints

Three new endpoints (after TTS endpoints):
```
GET  /episodes/<id>/render/draft.mp4  → send_file(conditional=True) for byte-range streaming
GET  /episodes/<id>/render            → render_manifest.json as JSON response
POST /episodes/<id>/render            → _submit_job("render", ...) with force param
```

`send_file(conditional=True)` enables HTTP Range headers for HTML5 video scrubbing without full download.

---

### 1G. `btcedu/web/static/app.js` — Video tab + review player

**Episode detail "Video" tab** — new `loadVideoPanel()` function (follows `loadTTSPanel` pattern):
- Fetch `GET /episodes/{id}/render` manifest
- Show summary: segment count, duration, size
- HTML5 `<video controls preload="metadata">` pointing to `/api/episodes/{id}/render/draft.mp4`
- Chapter timeline below player (chapter_id, duration, transition in/out)
- Render / Re-render button triggers `actions.render()`

**Review detail video player** — in `selectReview()`, when `data.stage === "render"`:
- Show `<video controls>` instead of diff viewer
- Show render manifest summary (segments, duration, size, resolution, fps)
- Approve / Reject / Request Changes buttons (existing pattern)

**Add `render` action** to `window.actions`:
```javascript
render() {
    if (!selected) return;
    submitJob("Render", `/episodes/${selected.episode_id}/render`, { force: isForce() });
}
```

---

### 1H. `btcedu/web/static/styles.css` — Video panel styles

```css
.video-panel, .video-summary, .video-player, .video-chapter-row,
.video-transition, .review-video-player, .review-render-info
```

---

### 1I. `btcedu/cli.py` — Add `review request-changes` command

```python
@review.command(name="request-changes")
@click.argument("review_id", type=int)
@click.option("--notes", required=True, help="Feedback describing changes needed.")
```

Calls `request_changes(session, review_id, notes)` from `reviewer.py`. Follows the pattern of existing `approve` and `reject` commands.

---

### 1J. `.env.example` — Add transition setting

```
RENDER_TRANSITION_DURATION=0.5
```

---

## 2. Migrations

**None required.** All features use existing tables (review_tasks, review_decisions, media_assets, pipeline_runs, content_artifacts). The `ReviewTask.artifact_hash` field already exists. The `_revert_episode()` fix is code-only.

---

## 3. Review Gate 3 Flow

```
Episode at TTS_DONE
  → render stage runs → Episode becomes RENDERED
  → review_gate_3 runs → creates ReviewTask(stage="render", artifact_paths=[draft.mp4])
  → pipeline returns "review_pending", stops

User opens dashboard Reviews tab:
  → sees pending render review
  → selectReview() shows HTML5 video player + render manifest info
  → user watches video

APPROVE:
  → approve_review() sets task APPROVED
  → _compute_artifact_hash([draft.mp4]) stores SHA-256 in ReviewTask.artifact_hash
  → pipeline picks up RENDERED episode (no pending review)
  → review_gate_3 sees has_approved_review("render") → True
  → sets episode.status = APPROVED → pipeline complete

REJECT:
  → reject_review() calls _revert_episode() → RENDERED → TTS_DONE
  → episode re-enters pipeline at TTS_DONE, render stage re-runs

REQUEST CHANGES:
  → request_changes() calls _revert_episode() → TTS_DONE + _mark_output_stale()
  → draft.mp4.stale marker invalidates idempotency check
  → render stage re-runs with fresh output
```

**Artifact hash chain**: `approve_review()` (reviewer.py:174-180) already calls `_compute_artifact_hash(paths)` which reads file bytes and computes SHA-256. For draft.mp4, this stores the video's content hash at approval time. Sprint 11 will verify this hash before YouTube upload.

---

## 4. Transition Filter Design

**Strategy**: Per-segment fade in/out. Each segment independently has fade filters baked into its video and audio streams. The concat demuxer with `-c copy` works because all segments share identical codec parameters.

**Video fade** (filter_complex chain):
- `fade=t=in:st=0:d=0.5` — fade from black at segment start
- `fade=t=out:st={duration-0.5}:d=0.5` — fade to black at segment end

**Audio fade** (`-af` parameter):
- `afade=t=in:st=0:d=0.5` — audio fade in
- `afade=t=out:st={duration-0.5}:d=0.5` — audio fade out

**TransitionType mapping**:
| Chapter JSON | fade_in_duration | fade_out_duration |
|---|---|---|
| `FADE` | `render_transition_duration` | `render_transition_duration` |
| `CUT` | 0.0 (no fade) | 0.0 (no fade) |
| `DISSOLVE` | `render_transition_duration` | `render_transition_duration` |

---

## 5. Dashboard Video Preview Design

### A. Episode Detail "Video" Tab (general browsing)

```
┌─────────────────────────────────────────────┐
│ [Transcript] [Correction] [TTS Audio] [Video] │  ← new tab
├─────────────────────────────────────────────┤
│ Render Summary: 10 segments · 458.3s · 45.2 MB │
│ [Re-render]                                   │
│                                               │
│ ┌─────────────────────────────────┐           │
│ │                                 │           │
│ │      HTML5 <video> player       │           │
│ │                                 │           │
│ └─────────────────────────────────┘           │
│                                               │
│ Chapter Timeline                              │
│ ─────────────────                             │
│ ch_001  58.3s  fade → cut                     │
│ ch_002  45.1s  cut → fade                     │
│ ch_003  62.0s  fade → fade                    │
└─────────────────────────────────────────────┘
```

### B. Review Detail View (stage="render")

```
┌─────────────────────────────────────────────┐
│ Review #12 — Episode: Bitcoin Basics          │
│ Stage: render  Status: PENDING                │
│                                               │
│ ┌─────────────────────────────────┐           │
│ │      HTML5 <video> player       │           │
│ └─────────────────────────────────┘           │
│                                               │
│ 10 segments · 458.3s · 45.2 MB                │
│ 1920x1080 @ 30fps                             │
│                                               │
│ [✓ Approve]  [✗ Reject]  [↻ Request Changes]  │
│                                               │
│ ┌─ Feedback ─────────────────────┐            │
│ │                                │            │
│ └────────────────────── [Submit] ┘            │
└─────────────────────────────────────────────┘
```

---

## 6. Test Plan

### `tests/test_ffmpeg_service.py` — Add 4 tests

| Test | Description |
|---|---|
| `test_create_segment_with_fade_in` | verify `fade=t=in` in ffmpeg command (dry-run mode) |
| `test_create_segment_with_fade_out` | verify `fade=t=out` with correct start time `duration - fade_out_duration` |
| `test_create_segment_with_both_fades` | both video fades in filter_complex + `afade` audio filters in `-af` |
| `test_create_segment_no_fades_default` | backward compat: no fade params → no fade filter in command |

### `tests/test_renderer.py` — Add 2 tests

| Test | Description |
|---|---|
| `test_render_video_with_transitions` | chapters with FADE transitions, mock `create_segment`, verify `fade_in_duration`/`fade_out_duration` kwargs passed |
| `test_render_manifest_includes_transition_duration` | dry-run render, verify render_manifest.json has `transition_duration` field |

### `tests/test_reviewer.py` — Add 6 tests

| Test | Description |
|---|---|
| `test_revert_episode_adapted_to_translated` | episode at ADAPTED → revert → TRANSLATED |
| `test_revert_episode_rendered_to_tts_done` | episode at RENDERED → revert → TTS_DONE |
| `test_revert_episode_unknown_status_noop` | episode at CHAPTERIZED → no change, warning logged |
| `test_reject_review_rendered_reverts_to_tts_done` | create render review task, reject → episode TTS_DONE |
| `test_request_changes_rendered_reverts_and_stale` | request changes → TTS_DONE + .stale marker |
| `test_get_review_detail_render_includes_video_url` | render review with draft.mp4 present → response has video_url + render_manifest |

### Pipeline integration tests — Add 3 tests

| Test | Description |
|---|---|
| `test_review_gate_3_creates_task` | RENDERED episode → ReviewTask created with stage="render" |
| `test_review_gate_3_approved_advances` | approved review → episode.status = APPROVED |
| `test_review_gate_3_pending_blocks` | pending review exists → returns "review_pending" |

### CLI test — Add 1 test

| Test | Description |
|---|---|
| `test_cli_review_request_changes` | invoke CLI command, verify `request_changes()` called with correct args |

### API tests — Add 4 tests

| Test | Description |
|---|---|
| `test_get_render_video_endpoint` | draft.mp4 exists → 200 with video/mp4 content type |
| `test_get_render_video_not_found` | no draft.mp4 → 404 |
| `test_get_render_manifest_endpoint` | render_manifest.json exists → 200 JSON |
| `test_trigger_render_endpoint` | POST → 202 with job_id |

**Total: ~20 new tests**

---

## 7. Implementation Order

1. `config.py` — add `render_transition_duration` (trivial, unblocks all)
2. `reviewer.py` `_revert_episode()` — fix bug (critical, affects RG1/RG2 too)
3. `ffmpeg_service.py` — add fade filter params + filter chain logic
4. `renderer.py` — wire chapter transitions to ffmpeg fade durations
5. `pipeline.py` — add review_gate_3 stage, handler, filter updates
6. `reviewer.py` `get_review_detail()` — add video_url/render_manifest
7. `api.py` — video serving, manifest, trigger endpoints
8. `cli.py` — `review request-changes` command
9. `app.js` + `styles.css` — Video tab, video panel, review video player
10. `.env.example` — add transition setting
11. Tests — all tests from plan above
12. Lint + format check

---

## 8. Definition of Done

- [ ] `_revert_episode()` handles all three gates: CORRECTED→TRANSCRIBED, ADAPTED→TRANSLATED, RENDERED→TTS_DONE
- [ ] Segments with `FADE` transitions have visible fade-in/fade-out (video + audio)
- [ ] `_V2_STAGES` includes `("review_gate_3", RENDERED)` and pipeline blocks/resumes correctly
- [ ] `run_pending()` and `run_latest()` include RENDERED (and TTS_DONE for run_latest)
- [ ] RG3 approval sets episode.status = APPROVED and stores SHA-256 of draft.mp4 in artifact_hash
- [ ] RG3 rejection/request-changes reverts episode to TTS_DONE
- [ ] Dashboard "Video" tab shows HTML5 video player with chapter timeline
- [ ] Review detail for stage="render" shows video player instead of diff viewer
- [ ] `GET /api/episodes/{id}/render/draft.mp4` serves video with byte-range support
- [ ] `btcedu review request-changes <id> --notes "..."` works from CLI
- [ ] All new code covered by tests (~20 new tests)
- [ ] All existing tests still pass (511+)
- [ ] ruff lint + format clean

---

## 9. Non-Goals

- xfade/crossfade between segments (requires full re-encode in concat step)
- Background music / audio mixing / loudness normalization
- Thumbnail generation
- YouTube Publishing (Sprint 11)
- New database migrations / schema changes
- Auto-approve rules for RG3
- Custom video player library (HTML5 `<video>` is sufficient)

---

## 10. Assumptions

- [ASSUMPTION] ffmpeg on Raspberry Pi supports `fade` and `afade` filters (standard in all builds)
- [ASSUMPTION] Flask `send_file(conditional=True)` provides adequate byte-range support for HTML5 video scrubbing
- [ASSUMPTION] Draft videos are < 200MB (podcast-length with static images), manageable for in-memory SHA-256 hashing
- [ASSUMPTION] `DISSOLVE` transition treated same as `FADE` for Sprint 10; true crossfade deferred
- [ASSUMPTION] `CUT` transition = no fade (0.0 duration), which is the existing default behavior
- [ASSUMPTION] The concat demuxer with `-c copy` works correctly when segments are independently encoded with matching codec parameters
- [ASSUMPTION] HTML5 `<video>` element is sufficient; no custom video player library needed
- [ASSUMPTION] RG3 has no auto-approve rules — all videos must be manually reviewed

---

## 11. Key Reference Files

| File | Role |
|---|---|
| `btcedu/services/ffmpeg_service.py` | Add fade filter parameters, modify filter_complex chain |
| `btcedu/core/renderer.py` | Wire chapter transitions to ffmpeg, update manifest |
| `btcedu/core/reviewer.py` | Fix `_revert_episode()`, extend `get_review_detail()` |
| `btcedu/core/pipeline.py` | Add review_gate_3, update status filters |
| `btcedu/web/api.py` | Video serving, manifest, trigger endpoints |
| `btcedu/web/static/app.js` | Video tab, video panel, review video player |
| `btcedu/web/static/styles.css` | Video panel CSS |
| `btcedu/cli.py` | `review request-changes` command |
| `btcedu/config.py` | `render_transition_duration` setting |
