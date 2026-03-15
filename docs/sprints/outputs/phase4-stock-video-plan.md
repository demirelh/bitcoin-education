# Phase 4: Stock Video Clips / B-Roll Support — Implementation Plan

**Date:** 2026-03-15
**Depends on:** Phase 3 (Smart Stock-Image Ranking) — complete
**Goal:** Allow each chapter to use either a stock photo or a short stock video clip (B-roll), with human pinning and existing review flow preserved.

---

## 1. Problem Statement

Every chapter in the rendered video currently uses a single still image looped for the chapter's duration (typically 30–90 seconds). This produces a static, slideshow-like viewing experience. Chapters with `visual_type: b_roll` are naturally suited to short motion clips — the chapterizer already labels them as "B-roll" but the pipeline serves them the same static image treatment as diagrams.

Pexels provides a free Video API alongside Photos. The existing pipeline can be extended to fetch short video clips as B-roll candidates, let the human reviewer choose between photo and video per chapter, and render video clips natively instead of looping a static frame.

---

## 2. Current State

### What exists

| Component | Status | Notes |
|-----------|--------|-------|
| `PexelsService` | Photos only | `search()` calls `/v1/search`; no video support |
| `candidates_manifest.json` | Images only | All candidates are photos with `pexels_id`, `alt_text`, `local_path` (`.jpg`) |
| `finalize_selections()` | Copies images | Copies `{ch_id}_selected.jpg`; writes `manifest.json` with `generation_method: "pexels"` |
| `create_segment()` in `ffmpeg_service.py` | Image-only | Takes `image_path` + `audio_path`; uses `-loop 1` to repeat still frame |
| `_resolve_chapter_media()` in `renderer.py` | Returns `(image_path, audio_path, duration)` | Expects image file; no concept of video source |
| `MediaAssetType` enum | Has `VIDEO` | `IMAGE`, `AUDIO`, `VIDEO` — VIDEO is used for the draft render, not source clips |
| `VisualType` enum | Has `B_ROLL` | Already distinguishes `b_roll` from `diagram`, `screen_share`, `title_card`, `talking_head` |
| Chapter intent extraction | Phase 3 | Produces `intents`, `allowed_motifs`, `disallowed_motifs` per chapter |
| Stock review UI | Photos only | Shows thumbnail grid with pin buttons; serves images via `/stock/candidate-image` |

### What's missing

1. **No Pexels Video API integration** — Different endpoint (`/videos/search`), different response schema.
2. **No video candidates in manifest** — Candidates are all photos.
3. **No video-vs-photo choice logic** — Ranking doesn't consider motion vs. still.
4. **No video segment rendering** — `create_segment()` assumes `-loop 1 -i image.png`.
5. **No video preview in dashboard** — Review UI only shows image thumbnails.
6. **No video normalization** — Pexels videos come in various codecs/resolutions/lengths; need transcode before render.

---

## 3. Design Decisions

### 3.1 Unified candidate pool per chapter

Image and video candidates are stored in the **same candidate list** per chapter. Each candidate has an `asset_type` field (`"photo"` or `"video"`). Ranking considers them together — the LLM sees both types and picks the best visual for the chapter's intent, regardless of medium.

**Why unified, not separate?**
- Simpler manifest schema (one list, one ranking pass).
- The human reviewer sees all options in one grid.
- A video is not automatically better than a photo — a sharp, relevant photo beats a blurry generic video.

### 3.2 Video preference by visual type

The ranking prompt gets a hint:
- `visual_type: b_roll` → "Video clips are preferred if semantically relevant."
- `visual_type: diagram` → "Static images are preferred for data graphics."
- `visual_type: screen_share` → "Static images are preferred for screen captures."

This is a soft signal, not a hard rule. The LLM (and human) make the final call.

### 3.3 Pexels Video API integration

Pexels Videos API: `https://api.pexels.com/videos/search`

**Response schema** (different from Photos):
```json
{
  "total_results": 100,
  "page": 1,
  "per_page": 5,
  "videos": [
    {
      "id": 856789,
      "width": 1920,
      "height": 1080,
      "url": "https://www.pexels.com/video/...",
      "duration": 15,
      "image": "https://images.pexels.com/videos/...preview.jpg",
      "video_files": [
        {
          "id": 12345,
          "quality": "hd",
          "file_type": "video/mp4",
          "width": 1920,
          "height": 1080,
          "fps": 29.97,
          "link": "https://videos.pexels.com/..."
        },
        {
          "id": 12346,
          "quality": "sd",
          "file_type": "video/mp4",
          "width": 960,
          "height": 540,
          "link": "..."
        }
      ],
      "video_pictures": [
        {"id": 1, "picture": "https://...", "nr": 0}
      ],
      "user": {
        "id": 100,
        "name": "John Doe",
        "url": "https://www.pexels.com/@johndoe"
      }
    }
  ]
}
```

Key differences from Photo API:
- Endpoint: `/videos/search` (not `/v1/search`)
- Results key: `videos` (not `photos`)
- No `alt` text (videos lack alt descriptions)
- Has `duration`, `video_files[]` (multiple quality variants), `video_pictures[]` (thumbnail stills)
- User object structured differently (`user.name`, not `photographer`)

### 3.4 Video candidate count

Per chapter: **2 video candidates** alongside the existing **5 photo candidates**.

**Why only 2?**
- Video files are 5–50 MB each — downloading 5 per chapter × 14 chapters = 70 files = 1–5 GB of bandwidth per episode. On a Raspberry Pi with limited disk and bandwidth, this is impractical.
- 2 video candidates give the human a choice without overwhelming disk/network.
- Rate limit: 180 req/hr shared between photo and video searches. 14 chapters × 1 video search = 14 extra requests (acceptable).

**Assumption:** Pexels Videos API shares the same rate limit with Photos (same API key, documented as 200/hr total across all endpoints).

### 3.5 Video download strategy

**Download only the HD variant** (closest to 1920×1080). Skip 4K/original to save bandwidth and disk.

**Selection priority for `video_files[]`:**
1. `quality: "hd"` with `width >= 1280`
2. Highest resolution available if no HD

**Max duration filter:** Skip videos longer than **30 seconds** at search time. Most B-roll clips are 5–20s. Longer clips waste disk and render time (they'll be trimmed anyway).

**Assumption:** Pexels Videos API does not support a duration filter in search params. Filtering happens client-side after the API response.

### 3.6 Video normalization (pre-render)

Before a video clip can be used in `create_segment()`, it must be normalized to match the render pipeline's codec parameters:

| Parameter | Required value | Reason |
|-----------|---------------|--------|
| Resolution | 1920×1080 | Match render target; scale+pad with black bars |
| Codec | libx264 | Concat demuxer requires identical codec across segments |
| Pixel format | yuv420p | H.264 compatibility |
| FPS | 30 | Match render target |
| Audio | **stripped** | TTS narration replaces any ambient audio |
| Duration | Trimmed to chapter audio duration | Chapter segment = TTS duration, not clip duration |

**Normalization happens during finalization** (`finalize_selections()`), not during download. This way:
- Downloaded candidates stay as-is (original quality preserved for review).
- Only the selected video gets normalized (saves compute).
- Normalization command is deterministic and logged.

### 3.7 Rendering with video clips

A new function `create_video_segment()` in `ffmpeg_service.py` handles the video case:

```
Input:  video_path (normalized) + audio_path (TTS) + overlays + duration
Output: segment MP4 (same format as image-based segments)
```

**Key differences from `create_segment()`:**
- No `-loop 1` — video clip plays natively.
- If clip is shorter than TTS duration → **loop the clip** (`-stream_loop -1`).
- If clip is longer → trim to duration (`-t`).
- Audio from clip is **muted** — only TTS audio track is used.
- Overlays and fades applied identically to the image path.

**Concat compatibility:** Since both image-based and video-based segments output the same codec/resolution/fps/pixel-format, `concatenate_segments()` with `-c copy` works unchanged.

### 3.8 Manifest schema evolution

**`candidates_manifest.json` schema 3.1** (extends 3.0 from Phase 3):

Per-candidate additions:
```json
{
  "pexels_id": 856789,
  "asset_type": "video",
  "photographer": "John Doe",
  "photographer_url": "https://www.pexels.com/@johndoe",
  "source_url": "https://www.pexels.com/video/...",
  "download_url": "https://videos.pexels.com/...",
  "local_path": "images/candidates/ch01/pexels_v_856789.mp4",
  "preview_url": "https://images.pexels.com/videos/.../preview.jpg",
  "preview_path": "images/candidates/ch01/pexels_v_856789_preview.jpg",
  "alt_text": "",
  "width": 1920,
  "height": 1080,
  "duration_seconds": 12.5,
  "fps": 29.97,
  "size_bytes": 8500000,
  "downloaded_at": "2026-03-15T...",
  "selected": false,
  "locked": false,
  "rank": null,
  "rank_reason": null,
  "trap_flag": false
}
```

New fields vs photo candidates:
- `asset_type`: `"photo"` (default, backward-compatible) or `"video"`
- `duration_seconds`: Video clip duration (null for photos)
- `fps`: Frame rate (null for photos)
- `preview_url`: Thumbnail still from Pexels (for dashboard display)
- `preview_path`: Local path to downloaded preview thumbnail

**Backward compatibility:** Existing candidates without `asset_type` are treated as `"photo"`. Schema version bumps to `"3.1"`.

**`images/manifest.json`** (final manifest) additions:
```json
{
  "chapter_id": "ch01",
  "visual_type": "b_roll",
  "asset_type": "video",
  "file_path": "images/ch01_selected.mp4",
  "generation_method": "pexels_video",
  "mime_type": "video/mp4",
  "duration_seconds": 12.5,
  "metadata": {
    "pexels_id": 856789,
    "source_url": "...",
    "license": "Pexels License (free for commercial use)",
    "normalized": true,
    "original_resolution": "1920x1080",
    "original_duration": 15.0
  }
}
```

New/changed fields:
- `asset_type`: `"photo"` or `"video"` (default `"photo"` for backward compat)
- `generation_method`: `"pexels_video"` (vs existing `"pexels"` for photos, `"template"` for placeholders)
- `duration_seconds`: Clip duration (for video; null for photo)
- `metadata.normalized`: Whether the video was transcoded
- `metadata.original_resolution`, `metadata.original_duration`: Pre-normalization values

### 3.9 Rollout strategy

**Opt-in by config:**
```python
# In Settings (config.py)
pexels_video_enabled: bool = False  # Phase 4: enable video candidates
pexels_video_per_chapter: int = 2   # Video candidates per chapter
pexels_video_max_duration: int = 30 # Max clip duration in seconds
```

When `pexels_video_enabled = False` (default):
- `search_stock_images()` searches photos only (existing behavior).
- Manifests and render pipeline unchanged.
- No video downloads, no extra API calls.

When `pexels_video_enabled = True`:
- `search_stock_images()` also searches videos per chapter.
- Video candidates appear in manifest alongside photos.
- Ranking considers both types.
- Dashboard shows video previews.
- Render handles both image and video segments.

**Assumption:** Existing episodes with photo-only manifests continue to work unchanged. The renderer reads `asset_type` from the final manifest and falls back to image-based rendering if absent.

---

## 4. Detailed Changes

### 4.1 Pexels Service: `pexels_service.py`

**New dataclass:**
```python
@dataclass
class PexelsVideo:
    id: int
    width: int
    height: int
    url: str  # Pexels page URL
    duration: int  # seconds
    image: str  # Preview thumbnail URL
    user_name: str
    user_url: str
    video_files: list[dict]  # [{"id", "quality", "file_type", "width", "height", "fps", "link"}]
    video_pictures: list[dict]  # [{"id", "picture", "nr"}]

@dataclass
class PexelsVideoSearchResult:
    query: str
    total_results: int
    videos: list[PexelsVideo]
    page: int
    per_page: int
```

**New method:**
```python
def search_videos(
    self,
    query: str,
    per_page: int = 5,
    page: int = 1,
    orientation: str = "landscape",
    size: str = "large",
) -> PexelsVideoSearchResult:
    """Search Pexels for videos matching query."""
```

Uses `https://api.pexels.com/videos/search` with same auth header and rate limiting.

**New method:**
```python
def download_video(
    self,
    video: PexelsVideo,
    target_path: Path,
    preferred_quality: str = "hd",
) -> Path:
    """Download video file (HD variant preferred)."""
```

Selects best `video_files[]` entry matching preferred quality and resolution.

**New method:**
```python
def download_video_preview(
    self,
    video: PexelsVideo,
    target_path: Path,
) -> Path:
    """Download video preview thumbnail."""
```

Downloads `video.image` (the preview thumbnail URL).

**Updated `StockPhotoService` protocol:** Renamed to `StockMediaService` (or add `StockVideoService` protocol separately).

**Assumption:** `search_videos()` and `download_video()` share the same `_rate_limit_wait()` and `_request_with_retry()` as photos — the rate limit is per-API-key, not per-endpoint.

### 4.2 Stock Images: `stock_images.py`

**`search_stock_images()` — updated:**

After the photo search loop, if `settings.pexels_video_enabled`:
1. For each chapter with `visual_type == "b_roll"`: also call `service.search_videos()`.
2. Filter results: skip videos longer than `settings.pexels_video_max_duration`.
3. Download top N video files + preview thumbnails.
4. Add to same chapter's `candidates` list with `asset_type: "video"`.

**Naming convention:**
- Photo candidates: `images/candidates/{ch_id}/pexels_{id}.jpg`
- Video candidates: `images/candidates/{ch_id}/pexels_v_{id}.mp4`
- Video previews: `images/candidates/{ch_id}/pexels_v_{id}_preview.jpg`

The `v_` prefix prevents ID collisions (Pexels photo IDs and video IDs are in different namespaces).

**`rank_candidates()` — updated:**

The ranking prompt already receives chapter context and intents. Additions:
1. Include `asset_type` in candidate info sent to LLM.
2. Add visual-type hint to prompt: "For b_roll chapters, motion video is preferred if relevant."
3. The LLM ranks photos and videos together; the `trap_flag` and dedup logic apply to both.

**`_derive_search_query()` — unchanged** for videos. Same query used for both photo and video search (Pexels API accepts the same keywords).

**`finalize_selections()` — updated:**

For selected candidates with `asset_type: "video"`:
1. **Normalize the video** → call new `normalize_video_clip()` (see §4.3).
2. Copy normalized video to `images/{ch_id}_selected.mp4`.
3. Set `generation_method: "pexels_video"`, `mime_type: "video/mp4"`, `duration_seconds`.
4. Create `MediaAsset` with `asset_type=MediaAssetType.VIDEO` (not `IMAGE`).

For selected candidates with `asset_type: "photo"` (or no `asset_type`): existing behavior unchanged.

**`_validate_and_adjust_selection()` — unchanged.** Trap checks and dedup logic are asset-type agnostic (they operate on `alt_text` and `pexels_id`).

### 4.3 ffmpeg Service: `ffmpeg_service.py`

**New function: `normalize_video_clip()`**

```python
def normalize_video_clip(
    input_path: str,
    output_path: str,
    target_duration: float | None = None,
    resolution: str = "1920x1080",
    fps: int = 30,
    crf: int = 23,
    preset: str = "medium",
    timeout_seconds: int = 300,
    dry_run: bool = False,
) -> SegmentResult:
    """Normalize a video clip for render pipeline compatibility.

    - Scale and pad to target resolution (preserving aspect ratio)
    - Transcode to H.264/yuv420p
    - Match target FPS
    - Strip audio track
    - Optionally trim to target_duration
    """
```

**ffmpeg command:**
```bash
ffmpeg -y -i input.mp4 \
  -filter_complex "
    [0:v]scale=1920:1080:force_original_aspect_ratio=decrease,
         pad=1920:1080:(ow-iw)/2:(oh-ih)/2,
         format=yuv420p,
         fps=30[v]
  " \
  -map "[v]" \
  -an \
  -c:v libx264 -preset medium -crf 23 \
  -t {target_duration} \
  output.mp4
```

Key flags:
- `-an` — strip all audio (TTS replaces it)
- `fps=30` — normalize frame rate
- `-t` — trim to target duration (if provided)
- Same scale+pad+format as `create_segment()` for consistency

**Performance note (Raspberry Pi 4, 4 cores, ARM64):** Transcoding a 15s 1080p clip at `preset=medium` takes ~30–60s on the Pi. With 14 chapters × maybe 3–5 video selections, that's 1.5–5 minutes of normalization. Acceptable within the existing `render_timeout_segment` (300s per segment). The `preset` can be set to `"fast"` or `"veryfast"` in config to speed up at quality cost.

**New function: `create_video_segment()`**

```python
def create_video_segment(
    video_path: str,
    audio_path: str,
    output_path: str,
    duration: float,
    overlays: list[OverlaySpec],
    resolution: str = "1920x1080",
    fps: int = 30,
    crf: int = 23,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    font: str = "NotoSans-Bold",
    fade_in_duration: float = 0.0,
    fade_out_duration: float = 0.0,
    timeout_seconds: int = 300,
    dry_run: bool = False,
) -> SegmentResult:
    """Create a video segment from video clip + TTS audio + overlays.

    Similar to create_segment() but input is a video file, not a still image.
    """
```

**ffmpeg command:**
```bash
ffmpeg -y \
  -stream_loop -1 -i normalized_clip.mp4 \
  -i audio.mp3 \
  -filter_complex "
    [0:v]scale=1920:1080:force_original_aspect_ratio=decrease,
         pad=1920:1080:(ow-iw)/2:(oh-ih)/2,
         format=yuv420p[scaled];
    [scaled]drawtext=...:[overlay_chain];
    [pre_fade]fade=t=in:st=0:d=0.5,fade=t=out:st=59.5:d=0.5[v]
  " \
  -map "[v]" \
  -map "1:a" \
  -af "afade=t=in:...,afade=t=out:..." \
  -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -t 60.0 -shortest \
  output.mp4
```

Key differences from `create_segment()`:
- `-stream_loop -1` instead of `-loop 1` — loops the video clip if shorter than TTS duration
- No `-loop 1` — video plays naturally
- Input is `.mp4`, not `.png`/`.jpg`
- Audio from the video input is ignored; only TTS audio (`-map "1:a"`) is used
- Scale+pad filter still applied (normalized clips should already be 1920×1080, but belt-and-suspenders)

**Why `-stream_loop -1`?** A 12s video clip with 60s of TTS narration needs to loop ~5 times. `-stream_loop -1` loops the input infinitely; `-t 60.0` and `-shortest` stop at the right time.

**Performance note:** Re-encoding a 60s segment from a video source takes slightly longer than from a still image (more data to process), but the same timeout (300s) is sufficient.

### 4.4 Renderer: `renderer.py`

**`_resolve_chapter_media()` — updated:**

Returns a 4-tuple instead of 3-tuple:
```python
def _resolve_chapter_media(
    chapter_id: str,
    image_manifest: dict,
    tts_manifest: dict,
    base_dir: Path,
) -> tuple[Path, Path, float, str]:
    """Returns (media_path, audio_path, duration, asset_type)."""
```

The new `asset_type` return value is `"photo"` or `"video"`, read from the image manifest entry's `asset_type` field (default `"photo"` if absent for backward compat).

**`render_video()` — updated:**

In the per-chapter loop, branch on `asset_type`:
```python
media_path, audio_path, duration, asset_type = _resolve_chapter_media(...)

if asset_type == "video":
    segment_result = create_video_segment(
        video_path=str(media_path),
        audio_path=str(audio_path),
        output_path=str(segment_path),
        duration=duration,
        overlays=overlay_specs,
        ...
    )
else:
    segment_result = create_segment(
        image_path=str(media_path),
        audio_path=str(audio_path),
        ...
    )
```

**`RenderSegmentEntry` — updated:**

Add `asset_type` field:
```python
@dataclass
class RenderSegmentEntry:
    chapter_id: str
    image: str  # Relative path (image or video)
    audio: str
    duration_seconds: float
    segment_path: str
    overlays: list[dict]
    transition_in: str
    transition_out: str
    size_bytes: int
    asset_type: str = "photo"  # "photo" or "video"
```

**`_compute_render_content_hash()` — updated:**

Include `asset_type` in the hash input so that switching a chapter from photo to video triggers re-render.

### 4.5 Config: `config.py`

New settings:
```python
# Stock Video / Phase 4
pexels_video_enabled: bool = False         # Enable video candidate search
pexels_video_per_chapter: int = 2          # Video candidates per b_roll chapter
pexels_video_max_duration: int = 30        # Max clip duration to download (seconds)
pexels_video_preferred_quality: str = "hd" # "hd" or "sd"
```

### 4.6 Web API: `api.py`

**Updated endpoint: `GET /episodes/{id}/stock/candidates`**

No route change needed. The response already serializes the full `candidates_manifest.json`. Video candidates appear naturally with their `asset_type`, `preview_path`, `duration_seconds` fields. The frontend handles display.

**New endpoint: `GET /episodes/{id}/stock/candidate-video`**

Serves video candidate files from disk:
```
GET /api/episodes/{id}/stock/candidate-video?chapter=ch01&filename=pexels_v_856789.mp4
```

Same pattern as existing `candidate-image` endpoint:
- Path traversal guard
- Validates path within `data/outputs/{ep_id}/images/candidates/`
- Serves `.mp4` files with `Content-Type: video/mp4`
- Supports `Range` header for seeking (HTTP 206 Partial Content)

**Updated endpoint: `POST /episodes/{id}/stock/pin`**

No change needed. `select_stock_image()` already works by `pexels_id` match within the chapter's candidate list. Video candidates have different `pexels_id` values, so pinning works identically.

### 4.7 Dashboard UI: `app.js`

**Stock review panel — per chapter:**

For each candidate, check `asset_type`:

```javascript
if (cand.asset_type === "video") {
    // Video candidate: show preview thumbnail + play button + duration badge
    const previewSrc = `/api/episodes/${epId}/stock/candidate-image?chapter=${chId}&filename=${encodeURIComponent(cand.preview_path.split('/').pop())}`;
    const videoSrc = `/api/episodes/${epId}/stock/candidate-video?chapter=${chId}&filename=${encodeURIComponent(cand.local_path.split('/').pop())}`;
    html += `
        <div class="stock-thumb ${selectedCls} ${pinnedCls}" data-pexels-id="${cand.pexels_id}">
            <div class="stock-thumb-media">
                <img src="${previewSrc}" alt="Video preview" loading="lazy">
                <span class="stock-video-badge">▶ ${cand.duration_seconds}s</span>
            </div>
            ...
        </div>`;
} else {
    // Photo candidate: existing thumbnail rendering
}
```

**Video preview modal:** When a video thumbnail is clicked, show a modal/inline `<video>` element:
```html
<video controls preload="none" style="max-width:100%">
    <source src="/api/episodes/{id}/stock/candidate-video?chapter=ch01&filename=..." type="video/mp4">
</video>
```

**Visual distinction:** Video thumbnails have a `▶` play icon overlay and a duration badge (e.g., "12s").

### 4.8 Dashboard CSS: `styles.css`

```css
/* Phase 4: Video candidate badges */
.stock-video-badge {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: rgba(0, 0, 0, 0.75);
    color: #fff;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 0.75em;
    font-weight: 700;
}

.stock-thumb-media {
    position: relative;
    display: inline-block;
    width: 100%;
}

.stock-thumb-media img {
    width: 100%;
    height: 120px;
    object-fit: cover;
}

/* Video preview modal */
.stock-video-preview {
    margin-top: 0.5em;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
}

.stock-video-preview video {
    width: 100%;
    max-height: 300px;
}
```

---

## 5. Files to Modify / Create

### New files

| File | Purpose |
|------|---------|
| `tests/test_stock_video.py` | Phase 4 tests |

### Modified files

| File | Changes |
|------|---------|
| `btcedu/services/pexels_service.py` | Add `PexelsVideo`, `PexelsVideoSearchResult`, `search_videos()`, `download_video()`, `download_video_preview()` |
| `btcedu/services/ffmpeg_service.py` | Add `normalize_video_clip()`, `create_video_segment()` |
| `btcedu/core/stock_images.py` | Update `search_stock_images()` for video candidates; update `finalize_selections()` for video normalization; update manifest writes for schema 3.1 |
| `btcedu/core/renderer.py` | Update `_resolve_chapter_media()` to return `asset_type`; update `render_video()` to branch on video vs. photo; update `RenderSegmentEntry` |
| `btcedu/config.py` | Add `pexels_video_enabled`, `pexels_video_per_chapter`, `pexels_video_max_duration`, `pexels_video_preferred_quality` |
| `btcedu/web/api.py` | Add `candidate-video` endpoint |
| `btcedu/web/static/app.js` | Video thumbnails, preview modal, duration badges |
| `btcedu/web/static/styles.css` | Video badge and preview styles (~25 lines) |
| `btcedu/prompts/templates/stock_rank.md` | Add `asset_type` to candidate list; add visual-type motion preference hint |

### Unchanged (preserved)

| File | Why |
|------|-----|
| `btcedu/core/pipeline.py` | `imagegen` stage calls `search + rank` as before; `review_gate_stock` unchanged |
| `btcedu/core/reviewer.py` | Review logic unchanged |
| `btcedu/models/media_asset.py` | `VIDEO` type already exists |
| `btcedu/models/chapter_schema.py` | `B_ROLL` visual type already exists |

---

## 6. ffmpeg Strategy — Raspberry Pi Considerations

### 6.1 Performance constraints

| Resource | RPi 4 spec | Impact |
|----------|-----------|--------|
| CPU | Cortex-A72, 4 cores, no hardware H.264 encode | Software encoding only (libx264) |
| RAM | ~4 GB | Sufficient for 1080p transcode |
| Disk | SD card / USB SSD | I/O bottleneck for large video files |
| Network | Ethernet/WiFi | Video downloads are 5–50 MB each |

### 6.2 Encoding preset strategy

For **normalization** (one-time per selected video):
- Use `settings.render_preset` (default `"medium"`).
- A 15s 1080p clip takes ~30–60s to normalize at `medium`.
- If speed is a concern, user can set `render_preset = "fast"` in `.env`.

For **segment creation** (per-chapter render):
- Same preset as image-based segments (no change).
- Video-sourced segments take slightly longer because the input must be decoded (vs. a still image which is trivial to decode).

### 6.3 Disk budget

Worst case (14 b_roll chapters, 2 video candidates each):
- 28 video files × ~10 MB average = ~280 MB candidates
- 28 preview thumbnails × ~50 KB = ~1.4 MB
- 14 normalized clips × ~5 MB = ~70 MB
- Total: ~350 MB per episode

This is on top of existing ~100 MB for photo candidates + render segments. Total ~450 MB per episode. Manageable on a USB SSD (typical 64+ GB), but tight on a 32 GB SD card.

**Mitigation:** Document that video mode is recommended with SSD storage. The `pexels_video_enabled = False` default prevents accidental disk usage.

### 6.4 Audio handling

All video clips are **muted** during normalization (`-an` flag). The TTS narration from ElevenLabs is the sole audio track in every segment.

**Why?** B-roll ambient audio would compete with the narrator. The pipeline is designed as narration-over-visuals, not as a soundtrack mix. This matches the existing image-based approach where there's no ambient audio at all.

### 6.5 Loop strategy for short clips

If a video clip (12s) is shorter than the chapter's TTS duration (60s):
- `-stream_loop -1` loops the clip indefinitely.
- `-t 60.0 -shortest` stops at the TTS duration.
- Result: the clip plays ~5 times. This creates visible looping.

**Assumption:** Visible looping is acceptable for Phase 4. A future enhancement could add slow-motion, reverse playback, or freeze-frame at the end to disguise the loop. This is explicitly out of scope.

### 6.6 Concat compatibility

Both image-based and video-based segment MP4s output with identical:
- Resolution: 1920×1080
- Codec: libx264 / aac
- Pixel format: yuv420p
- FPS: 30

This guarantees that `concatenate_segments()` with `-c copy` (stream copy, no re-encode) works for mixed sequences of photo and video segments.

---

## 7. Test Plan

### 7.1 Pexels Video Service tests

| Test | What it verifies |
|------|-----------------|
| `test_search_videos_parses_response` | Mocked Pexels Video API response correctly parsed into `PexelsVideoSearchResult` |
| `test_search_videos_rate_limited` | Rate limiter applies to video searches |
| `test_download_video_selects_hd` | `download_video()` picks HD variant from `video_files[]` |
| `test_download_video_fallback_to_best` | Falls back to highest resolution if no HD |
| `test_download_video_preview` | Preview thumbnail downloaded correctly |

### 7.2 Stock image search with videos

| Test | What it verifies |
|------|-----------------|
| `test_search_includes_video_candidates_when_enabled` | `search_stock_images()` adds video candidates for b_roll chapters when `pexels_video_enabled=True` |
| `test_search_skips_video_when_disabled` | Default config → no video candidates |
| `test_search_skips_long_videos` | Videos over `pexels_video_max_duration` not downloaded |
| `test_video_candidates_have_asset_type` | Each video candidate has `asset_type: "video"` |
| `test_photo_candidates_have_asset_type` | Existing photo candidates have `asset_type: "photo"` |
| `test_video_candidates_for_diagram_skipped` | `diagram` visual type gets no video candidates |

### 7.3 Ranking with mixed candidates

| Test | What it verifies |
|------|-----------------|
| `test_ranking_includes_asset_type_in_prompt` | LLM prompt includes `asset_type` per candidate |
| `test_ranking_video_preferred_for_b_roll` | Prompt includes motion preference hint for b_roll |
| `test_ranking_photo_preferred_for_diagram` | No motion hint for diagram chapters |

### 7.4 Manifest / schema tests

| Test | What it verifies |
|------|-----------------|
| `test_manifest_schema_3_1_with_videos` | After search+rank with videos, schema_version is "3.1" |
| `test_manifest_backward_compat_no_asset_type` | Manifest without `asset_type` treated as photo |
| `test_finalized_manifest_includes_video_entry` | Video selection produces `generation_method: "pexels_video"` |

### 7.5 ffmpeg normalization tests

| Test | What it verifies |
|------|-----------------|
| `test_normalize_video_clip_command` | Generated ffmpeg command has correct flags (`-an`, scale, fps, crf) |
| `test_normalize_video_clip_dry_run` | Dry-run creates placeholder without executing |
| `test_create_video_segment_command` | Generated command uses `-stream_loop -1`, `-map "1:a"`, no `-loop 1` |
| `test_create_video_segment_with_overlays` | Drawtext filters applied to video segment same as image segment |
| `test_create_video_segment_dry_run` | Placeholder file created in dry-run |

### 7.6 Renderer integration tests

| Test | What it verifies |
|------|-----------------|
| `test_resolve_chapter_media_returns_asset_type` | Photo entry → `"photo"`, video entry → `"video"` |
| `test_resolve_chapter_media_default_photo` | Entry without `asset_type` → `"photo"` |
| `test_render_video_mixed_segments` | Episode with some photo and some video chapters renders all segments |
| `test_render_segment_entry_has_asset_type` | Render manifest includes `asset_type` per segment |

### 7.7 API / UI tests

| Test | What it verifies |
|------|-----------------|
| `test_candidate_video_endpoint_serves_mp4` | `GET /stock/candidate-video` returns video/mp4 |
| `test_candidate_video_endpoint_path_traversal_guard` | Rejects `../../../etc/passwd` |
| `test_candidates_api_includes_asset_type` | JSON response includes `asset_type` per candidate |

### 7.8 Integration smoke path

| Test | What it verifies |
|------|-----------------|
| `test_full_video_smoke_path` | Mock Pexels photo+video search → mock LLM rank → finalize (mock normalize) → mock render → verify manifest chain is consistent |

**Total: ~30 tests**

---

## 8. Definition of Done

1. `PexelsService` supports `search_videos()` and `download_video()`.
2. `search_stock_images()` fetches video candidates for b_roll chapters when enabled.
3. `rank_candidates()` ranks mixed photo/video candidates with intent context.
4. `finalize_selections()` normalizes selected video clips via ffmpeg.
5. `create_video_segment()` renders video-based chapter segments with TTS audio and overlays.
6. `renderer.py` handles mixed photo/video chapters in a single episode.
7. `candidates_manifest.json` schema 3.1 with `asset_type`, `preview_path`, `duration_seconds`.
8. `images/manifest.json` includes `asset_type` and `generation_method: "pexels_video"`.
9. Dashboard shows video thumbnails with play icon, duration badge, and inline preview.
10. `GET /stock/candidate-video` serves video files with range support.
11. Feature gated by `pexels_video_enabled = False` default.
12. All tests pass (~30 new + existing ~782 unbroken).
13. Ruff lint clean.
14. Manual smoke test: enable video mode, re-search + re-rank real episode, verify video candidates appear and can be pinned.

---

## 9. Non-Goals

- **No Ken Burns / pan-and-zoom for photos.** Phase 4 adds video clips; photo rendering stays static.
- **No audio mixing.** B-roll ambient sound is always stripped. TTS is the sole audio.
- **No automatic video preference.** The LLM and human decide; no hard auto-select for video over photo.
- **No slow-motion or loop disguise.** If a clip is shorter than TTS, it loops visibly. Future work.
- **No video editing (trim points, in/out selection).** The full clip plays (up to TTS duration). Human can't choose a sub-clip.
- **No new pipeline stages.** Video normalization happens inside `finalize_selections()`, not as a separate pipeline stage.
- **No new DB tables or migrations.** `MediaAssetType.VIDEO` already exists. All state lives in JSON manifests.
- **No publish flow changes.** The rendered `draft.mp4` is the same format regardless of source media.
- **No v1 pipeline changes.**

---

## 10. Implementation Order

1. **Config** — Add 4 new settings.
2. **Pexels Service** — `PexelsVideo`, `search_videos()`, `download_video()`, `download_video_preview()`.
3. **Tests: Pexels** — Mock video API responses.
4. **Stock Images: search** — Update `search_stock_images()` for video candidates.
5. **Tests: search** — Verify video candidates appear/don't appear by config.
6. **Stock Images: finalize** — Update `finalize_selections()` for video normalization.
7. **ffmpeg Service** — `normalize_video_clip()`, `create_video_segment()`.
8. **Tests: ffmpeg** — Verify command generation.
9. **Renderer** — Update `_resolve_chapter_media()` and `render_video()`.
10. **Tests: renderer** — Mixed photo/video segment rendering.
11. **Ranking prompt** — Update `stock_rank.md` with `asset_type` and motion hint.
12. **API** — `candidate-video` endpoint.
13. **UI** — Video thumbnails, preview modal, duration badge.
14. **CSS** — Video-specific styles.
15. **Integration test** — Full smoke path.
16. **Manual smoke test** with real episode.
17. **Output doc**.

Estimated scope: ~300 lines Pexels service, ~150 lines ffmpeg service, ~150 lines stock_images, ~50 lines renderer, ~30 lines config+API, ~50 lines JS/CSS, ~500 lines tests.

---

## 11. Open Questions (resolved with assumptions)

| Question | Assumption | Rationale |
|----------|-----------|-----------|
| Do Pexels photo and video IDs collide? | No, different namespaces | Pexels docs show separate ID spaces; we prefix video files with `pexels_v_` for safety |
| Does Pexels Video API share the photo rate limit? | Yes, 200/hr total | Same API key; conservative 180/hr budget shared |
| Should videos be searched for all visual types? | No, b_roll only | Diagrams and screen shares are better as static graphics; title cards and talking heads are placeholders |
| What if a video candidate has no preview thumbnail? | Use first frame via ffprobe/ffmpeg | Fallback: `ffmpeg -i clip.mp4 -vframes 1 -f image2 preview.jpg` |
| Should normalization happen during search or finalize? | Finalize | Normalize only the selected clip to save compute; candidates stay as downloaded |
| Should we add a "prefer video" checkbox to the UI? | No | The visual_type hint in the ranking prompt and human pinning are sufficient |
