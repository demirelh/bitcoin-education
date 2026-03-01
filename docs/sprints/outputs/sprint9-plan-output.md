# Sprint 9: Video Assembly / Render Pipeline (Part 1) — Implementation Plan

**Sprint Number:** 9
**Phase:** 5 (Video Assembly), Part 1
**Status:** Planning
**Dependencies:** Sprint 8 (TTS) complete
**Created:** 2026-03-01

---

## 1. Sprint Scope Summary

Sprint 9 builds the **foundational render pipeline** that combines per-chapter images (Sprint 7) + TTS audio (Sprint 8) + text overlays into a draft MP4 video via ffmpeg. This is the stage between `TTS_DONE` and `REVIEW GATE 3` in the v2 pipeline.

Sprint 9 is Part 1 of Phase 5. It delivers a working end-to-end render: image + audio → per-chapter segment → concatenated `draft.mp4`. Sprint 10 will add transitions, Review Gate 3, and dashboard video preview.

**Key difference from previous stages**: Render uses **local ffmpeg** (no external API), so there's no per-call cost tracking. The service layer wraps `subprocess.run()` instead of HTTP requests.

**In Scope:**
1. `ffmpeg_service.py` — wrapper around ffmpeg CLI for video composition
2. Render manifest format (`render/render_manifest.json`)
3. `renderer.py` — orchestration: `render_video()`
4. Per-chapter segment rendering (image + audio + overlays → video segment)
5. Text overlay support (ffmpeg drawtext filter)
6. Chapter concatenation (ffmpeg concat demuxer)
7. Output: H.264 MP4, AAC audio at `data/outputs/{ep_id}/render/draft.mp4`
8. CLI command: `btcedu render` with `--force`, `--dry-run`
9. Pipeline integration: RENDER after TTS_DONE
10. Provenance, idempotency
11. Tests (~32 new)

**Not in Scope (Sprint 10):**
- Fancy transitions (fade, dissolve) — Sprint 9 uses "cut" only
- Review Gate 3 (video review + approval)
- Dashboard video preview / player
- Dashboard render trigger button
- API endpoints for render manifest / video serving
- Background music or audio mixing
- Intro/outro templates
- Cloud storage upload
- Parallel segment rendering

---

## 2. New Files

### 2.1 `btcedu/services/ffmpeg_service.py`

ffmpeg CLI wrapper. No database logic — pure subprocess calls.

**Dataclasses:**
- `OverlaySpec` — text, overlay_type, fontsize, fontcolor, font, position, start, end, box, boxcolor, boxborderw
- `SegmentResult` — segment_path, duration_seconds, size_bytes, ffmpeg_command, returncode, stderr
- `ConcatResult` — output_path, duration_seconds, size_bytes, segment_count, ffmpeg_command, returncode, stderr
- `MediaInfo` — duration_seconds, width, height, codec_video, codec_audio, size_bytes, format_name

**Functions:**
- `get_ffmpeg_version() -> str` — parse `ffmpeg -version` output
- `find_font_path(font_name) -> str` — search `/usr/share/fonts/truetype/noto/`, fallback to DejaVu, then fontconfig name
- `_escape_drawtext(text) -> str` — escape `:`, `'`, `\` for ffmpeg drawtext; Turkish chars (ş,ç,ğ,ı,ö,ü,İ) pass through
- `_build_drawtext_filter(overlay, font_path) -> str` — build `drawtext=fontfile=...:text=...:enable='between(t,start,end)'`
- `create_segment(image_path, audio_path, output_path, duration, overlays, resolution, fps, crf, preset, audio_bitrate, timeout_seconds, dry_run) -> SegmentResult`
- `concatenate_segments(segment_paths, output_path, timeout_seconds, dry_run) -> ConcatResult`
- `probe_media(file_path) -> MediaInfo` — via `ffprobe -v quiet -print_format json -show_format -show_streams`
- `_run_ffmpeg(cmd, timeout) -> tuple[int, str]` — subprocess.run wrapper with timeout

**ffmpeg command for segment creation:**
```
ffmpeg -y -loop 1 -i {image} -i {audio} \
  -filter_complex "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,
    pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p[scaled];
    [scaled]drawtext=...[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -t {duration} -shortest {output}
```

**ffmpeg command for concatenation:**
```
# concat_list.txt written with: file '/abs/path/segments/ch01.mp4'
ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy {output}
```

**Overlay position map:**
```python
OVERLAY_POSITIONS = {
    "bottom_center": "x=(w-text_w)/2:y=h-th-60",
    "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    "top_center": "x=(w-text_w)/2:y=60",
}
```

### 2.2 `btcedu/core/renderer.py`

Render orchestration following the exact same structure as `tts.py`.

**Dataclasses:**
- `RenderSegmentEntry` — chapter_id, image, audio, duration_seconds, segment_path, overlays (list[dict]), transition_in, transition_out, size_bytes
- `RenderResult` — episode_id, render_path, manifest_path, provenance_path, draft_path, segment_count, total_duration_seconds, total_size_bytes, skipped

**Overlay style mapping (overlay_type → visual defaults):**
```python
OVERLAY_STYLES = {
    "lower_third": {"fontsize": 48, "fontcolor": "white", "position": "bottom_center"},
    "title":       {"fontsize": 72, "fontcolor": "white", "position": "center"},
    "quote":       {"fontsize": 42, "fontcolor": "white", "position": "center"},
    "statistic":   {"fontsize": 56, "fontcolor": "#F7931A", "position": "center"},
}
```

**Main function:**
```python
def render_video(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> RenderResult:
```

**Orchestration steps:**
1. Fetch episode, validate `pipeline_version == 2`
2. Validate status in `(TTS_DONE, RENDERED)` — raise if wrong (unless force)
3. Resolve paths: `chapters.json`, `images/manifest.json`, `tts/manifest.json`, `render/`
4. Load all three inputs via `_load_chapters()`, `_load_image_manifest()`, `_load_tts_manifest()`
5. Compute combined content hash via `_compute_render_content_hash()` — hashes overlay text/timing + image paths + TTS paths/durations
6. Idempotency: if not force and `_is_render_current()` → return `RenderResult(skipped=True)`
7. Create `PipelineRun(stage="render", status=RUNNING)`
8. Create `render/segments/` directory
9. For each chapter:
   - `_resolve_chapter_media()` → get absolute image_path, audio_path, duration from manifests
   - `_chapter_to_overlay_specs()` → convert `chapter.overlays` to `OverlaySpec` list
   - Call `ffmpeg_service.create_segment()` → segment MP4
   - Record `RenderSegmentEntry`
10. Call `ffmpeg_service.concatenate_segments()` → `render/draft.mp4`
11. Write `render/render_manifest.json`
12. Write `provenance/render_provenance.json`
13. Create `ContentArtifact(artifact_type="render", model="ffmpeg")`
14. Create `MediaAsset(asset_type=VIDEO, duration_seconds, size_bytes)`
15. Set `episode.status = RENDERED`
16. Update PipelineRun to SUCCESS
17. On exception: PipelineRun FAILED, episode.error_message, re-raise

**Private helpers:**
- `_load_chapters(path) -> ChapterDocument`
- `_load_image_manifest(path) -> dict`
- `_load_tts_manifest(path) -> dict`
- `_compute_render_content_hash(chapters_doc, image_manifest, tts_manifest) -> str`
- `_is_render_current(manifest_path, provenance_path, draft_path, content_hash) -> bool`
- `_chapter_to_overlay_specs(chapter) -> list[OverlaySpec]`
- `_resolve_chapter_media(chapter_id, image_manifest, tts_manifest, base_dir) -> tuple[Path, Path, float]`
- `_create_media_asset_record(session, episode_id, draft_path, duration, size_bytes)`

**Dry-run behavior**: `create_segment` and `concatenate_segments` get `dry_run=True`, which builds the ffmpeg command but skips execution. The manifest is still written (with `size_bytes=0` for segments). Episode status is still set to RENDERED.

### 2.3 `tests/test_ffmpeg_service.py` (~16 tests)

| Test | Asserts |
|------|---------|
| `test_get_ffmpeg_version_success` | Mock subprocess → version string parsed |
| `test_get_ffmpeg_version_not_found` | FileNotFoundError → returns "unknown" |
| `test_find_font_path_noto_exists` | NotoSans path returned when file exists |
| `test_find_font_path_fallback_dejavu` | DejaVu path when Noto missing |
| `test_find_font_path_fallback_fontconfig` | Font name string when neither found |
| `test_escape_drawtext_plain_text` | No special chars → unchanged |
| `test_escape_drawtext_colon_and_quotes` | `:` and `'` escaped correctly |
| `test_escape_drawtext_turkish_chars` | ş,ç,ğ,ı,ö,ü pass through |
| `test_build_drawtext_filter_lower_third` | Correct filter string with position/timing |
| `test_build_drawtext_filter_title` | Center position, larger fontsize |
| `test_create_segment_no_overlays` | Mock subprocess, verify command has scale+pad but no drawtext |
| `test_create_segment_with_overlays` | Verify drawtext filter in command |
| `test_create_segment_dry_run` | subprocess NOT called, command returned |
| `test_concatenate_segments_command` | Verify concat demuxer command + list file |
| `test_concatenate_segments_dry_run` | subprocess NOT called |
| `test_probe_media_success` | Mock ffprobe JSON → MediaInfo parsed |

### 2.4 `tests/test_renderer.py` (~19 tests)

Uses custom `db_engine`/`db_session` fixtures (same as `test_tts.py`) with `prompt_versions` table and `MediaBase.metadata.create_all()`.

| Test | Asserts |
|------|---------|
| `test_render_episode_not_found` | ValueError("Episode not found") |
| `test_render_v1_rejected` | ValueError("v1 pipeline") |
| `test_render_wrong_status` | ValueError("expected 'tts_done'") |
| `test_render_missing_chapters` | FileNotFoundError |
| `test_render_missing_image_manifest` | FileNotFoundError |
| `test_render_missing_tts_manifest` | FileNotFoundError |
| `test_compute_render_hash_stable` | Same inputs → same hash |
| `test_compute_render_hash_changes` | Changed overlay text → different hash |
| `test_is_render_current_missing_manifest` | Returns False |
| `test_is_render_current_stale_marker` | Returns False |
| `test_is_render_current_hash_mismatch` | Returns False |
| `test_is_render_current_all_good` | Returns True |
| `test_chapter_to_overlay_specs` | Correct OverlaySpec from chapter overlay |
| `test_chapter_to_overlay_specs_empty` | Empty overlays → empty list |
| `test_render_video_dry_run` | Manifest written, ffmpeg NOT called, status → RENDERED |
| `test_render_video_happy_path` | Mock ffmpeg service → manifest + provenance + ContentArtifact + MediaAsset + status RENDERED + PipelineRun SUCCESS |
| `test_render_video_idempotency` | Second run → skipped=True |
| `test_render_video_ffmpeg_failure` | PipelineRun FAILED, error_message set, re-raised |
| `test_v2_stages_include_render` | `("render", TTS_DONE)` in `_V2_STAGES` |

---

## 3. Modified Files

### 3.1 `btcedu/config.py`

Add after the `elevenlabs_*` block:

```python
# Render / ffmpeg (Sprint 9)
render_resolution: str = "1920x1080"
render_fps: int = 30
render_crf: int = 23
render_preset: str = "medium"
render_audio_bitrate: str = "192k"
render_font: str = "NotoSans-Bold"
render_timeout_segment: int = 300
render_timeout_concat: int = 600
```

### 3.2 `btcedu/core/pipeline.py`

**a)** `_V2_STAGES`: Add `("render", EpisodeStatus.TTS_DONE)` after the tts entry

**b)** `_run_stage()`: Add `elif stage_name == "render":` branch — lazy import `render_video`, call it, return StageResult with `f"{result.segment_count} segments, {result.total_duration_seconds:.1f}s, {result.total_size_bytes / 1024 / 1024:.1f}MB"`

**c)** `run_pending()`: Add `EpisodeStatus.TTS_DONE` to status filter list

**d)** `run_latest()`: Add `EpisodeStatus.TTS_DONE` to status filter list

**Note**: Cost extraction tuple does NOT need `"render"` — render has no API cost, detail string uses MB not `$`.

### 3.3 `btcedu/cli.py`

Add `render` command after `tts` command. Pattern: `--episode-id` (multiple), `--force`, `--dry-run`. Sets `settings.dry_run = True` if `--dry-run`. Loops over episode_ids, prints `[OK]`/`[SKIP]`/`[FAIL]` with segment count, duration, size in MB.

### 3.4 `btcedu/web/jobs.py`

Add `elif job.action == "render": self._do_render(job, session, settings)` in `_execute()`.
Add `_do_render()` method — imports `render_video`, calls it, updates job result.

### 3.5 `.env.example`

Add render config entries:
```
# Render / ffmpeg (Sprint 9)
RENDER_RESOLUTION=1920x1080
RENDER_FPS=30
RENDER_CRF=23
RENDER_PRESET=medium
RENDER_AUDIO_BITRATE=192k
RENDER_FONT=NotoSans-Bold
RENDER_TIMEOUT_SEGMENT=300
RENDER_TIMEOUT_CONCAT=600
```

---

## 4. Render Manifest Schema

Written to `data/outputs/{ep_id}/render/render_manifest.json`:

```json
{
  "episode_id": "abc123",
  "schema_version": "1.0",
  "resolution": "1920x1080",
  "fps": 30,
  "generated_at": "2026-03-01T12:00:00+00:00",
  "total_duration_seconds": 847.3,
  "total_size_bytes": 524288000,
  "segments": [
    {
      "chapter_id": "ch01",
      "image": "images/ch01_intro.png",
      "audio": "tts/ch01.mp3",
      "duration_seconds": 58.3,
      "segment_path": "render/segments/ch01.mp4",
      "overlays": [
        {
          "type": "lower_third",
          "text": "Bitcoin Nedir?",
          "font": "NotoSans-Bold",
          "fontsize": 48,
          "fontcolor": "white",
          "position": "bottom_center",
          "start": 2.0,
          "end": 7.0
        }
      ],
      "transition_in": "cut",
      "transition_out": "cut",
      "size_bytes": 12345678
    }
  ],
  "output_path": "render/draft.mp4",
  "ffmpeg_version": "6.1.1",
  "codec": {
    "video": "libx264",
    "audio": "aac",
    "preset": "medium",
    "crf": 23,
    "audio_bitrate": "192k"
  }
}
```

Provenance written to `data/outputs/{ep_id}/provenance/render_provenance.json`:

```json
{
  "stage": "render",
  "episode_id": "abc123",
  "timestamp": "2026-03-01T12:00:00+00:00",
  "model": "ffmpeg",
  "ffmpeg_version": "6.1.1",
  "input_files": [
    "data/outputs/abc123/chapters.json",
    "data/outputs/abc123/images/manifest.json",
    "data/outputs/abc123/tts/manifest.json"
  ],
  "input_content_hash": "sha256:abcdef...",
  "output_files": [
    "data/outputs/abc123/render/render_manifest.json",
    "data/outputs/abc123/render/draft.mp4"
  ],
  "segment_count": 8,
  "total_duration_seconds": 847.3,
  "total_size_bytes": 524288000,
  "cost_usd": 0.0
}
```

---

## 5. Key Implementation Details

### 5.1 ffmpeg Filter Complex (per segment)

**With overlays:**
```bash
ffmpeg -y \
  -loop 1 -i /path/images/ch01.png \
  -i /path/tts/ch01.mp3 \
  -filter_complex "\
    [0:v]scale=1920:1080:force_original_aspect_ratio=decrease,\
    pad=1920:1080:(ow-iw)/2:(oh-ih)/2,\
    format=yuv420p[scaled];\
    [scaled]drawtext=fontfile=/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf:\
      text='Bitcoin Nedir\?':fontsize=48:fontcolor=white:\
      box=1:boxcolor=black@0.6:boxborderw=10:\
      x=(w-text_w)/2:y=h-th-60:\
      enable='between(t\,2.0\,7.0)'[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -t 58.3 -shortest \
  /path/render/segments/ch01.mp4
```

**Without overlays:**
```bash
ffmpeg -y \
  -loop 1 -i /path/images/ch01.png \
  -i /path/tts/ch01.mp3 \
  -filter_complex "\
    [0:v]scale=1920:1080:force_original_aspect_ratio=decrease,\
    pad=1920:1080:(ow-iw)/2:(oh-ih)/2,\
    format=yuv420p[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -t 58.3 -shortest \
  /path/render/segments/ch01.mp4
```

### 5.2 Concatenation

```bash
# concat_list.txt:
file '/abs/path/render/segments/ch01.mp4'
file '/abs/path/render/segments/ch02.mp4'

ffmpeg -y -f concat -safe 0 \
  -i /path/render/concat_list.txt \
  -c copy \
  /path/render/draft.mp4
```

### 5.3 ffprobe

```bash
ffprobe -v quiet -print_format json \
  -show_format -show_streams \
  /path/render/draft.mp4
```

---

## 6. Implementation Order

1. `btcedu/config.py` — add 8 render config fields
2. `btcedu/services/ffmpeg_service.py` — ffmpeg wrapper service
3. `tests/test_ffmpeg_service.py` — service unit tests
4. `btcedu/core/renderer.py` — render orchestration module
5. `tests/test_renderer.py` — orchestration tests
6. `btcedu/core/pipeline.py` — wire render into pipeline
7. `btcedu/cli.py` — add render CLI command
8. `btcedu/web/jobs.py` — add render job handler
9. `.env.example` — add render config entries

---

## 7. Definition of Done

- [ ] All existing tests pass (476+)
- [ ] ~35 new tests pass (16 service + 19 renderer)
- [ ] `btcedu render --episode-id EP --dry-run` produces render manifest without running ffmpeg
- [ ] `btcedu render --episode-id EP` creates per-chapter segments + concatenated `draft.mp4`
- [ ] `_V2_STAGES` includes `("render", EpisodeStatus.TTS_DONE)`
- [ ] `btcedu run --episode-id EP` (v2) advances through RENDER automatically
- [ ] `media_assets` table gets rows with `asset_type="video"`, `duration_seconds`, `size_bytes`
- [ ] Render manifest + provenance JSON written correctly
- [ ] Idempotency: re-run with unchanged inputs is skipped
- [ ] `.stale` marker triggers re-render
- [ ] `--force` flag forces re-render
- [ ] Text overlays support Turkish characters (ş,ç,ğ,ı,ö,ü,İ)
- [ ] `run_pending()` and `run_latest()` pick up `TTS_DONE` episodes
- [ ] Web job handler `_do_render` works
- [ ] v1 pipeline completely unaffected

---

## 8. Assumptions

- `[ASSUMPTION]` ffmpeg is installed at system PATH (`sudo apt install ffmpeg` on Raspberry Pi)
- `[ASSUMPTION]` Font: NotoSans-Bold for Turkish text. Fallback to DejaVu-Bold. Install: `sudo apt install fonts-noto`
- `[ASSUMPTION]` Transitions are all "cut" in Sprint 9 (ignored in ffmpeg, just concatenated)
- `[ASSUMPTION]` Render has no API cost — cost_usd is always 0.0 in provenance
- `[ASSUMPTION]` Segment rendering is sequential (no parallelism needed for Raspberry Pi)
- `[ASSUMPTION]` `-preset medium` is acceptable on Raspberry Pi (~10-30 min for 15-min video)

---

## 9. Performance Considerations

- **Render time**: ~10-30 min for a 15-min video on Raspberry Pi with `-preset medium`
- **Storage**: ~500MB per draft video. Segment files add ~500MB more during render (can be cleaned after concat)
- **Sequential processing**: Chapters rendered one at a time. Progress visible via per-segment logging
- **Timeout**: 300s per segment (5 min), 600s for concatenation (10 min)
- **Preset tuning**: Can switch to `-preset fast` on Pi for 2x speed at slight quality cost

---

## 10. Key Reference Files

- `btcedu/core/tts.py` — primary pattern to follow for orchestration
- `btcedu/core/image_generator.py` — secondary pattern reference
- `btcedu/models/chapter_schema.py` — input schema (Chapter.overlays, Overlay, OverlayType)
- `btcedu/models/media_asset.py` — uses own Base (MediaBase), has VIDEO type
- `btcedu/core/pipeline.py` — pipeline integration (_V2_STAGES, _run_stage, run_pending, run_latest)
- `tests/test_tts.py` — test fixture pattern (db_engine with MediaBase tables)
