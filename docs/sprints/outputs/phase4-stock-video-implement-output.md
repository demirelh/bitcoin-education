# Phase 4: Stock Video Clips / B-Roll Support — Implementation Output

**Date:** 2026-03-15
**Status:** Complete
**Tests:** 32 new tests passing / 814 total passing

---

## Summary

Phase 4 adds Pexels Video API support to the stock image pipeline, allowing b_roll chapters to use short MP4 video clips instead of static images. The feature is fully opt-in via `pexels_video_enabled = False` (default).

---

## Files Changed

### New Files

| File | Description |
|------|-------------|
| `tests/test_stock_video.py` | 32 new Phase 4 tests (8 test classes) |
| `docs/sprints/outputs/phase4-stock-video-implement-output.md` | This document |

### Modified Files

| File | Changes |
|------|---------|
| `btcedu/config.py` | Added 4 settings: `pexels_video_enabled`, `pexels_video_per_chapter`, `pexels_video_max_duration`, `pexels_video_preferred_quality` |
| `btcedu/services/pexels_service.py` | Added `PexelsVideoFile`, `PexelsVideo`, `PexelsVideoSearchResult` dataclasses; added `search_videos()`, `download_video()`, `download_video_preview()`, `_select_video_file()` methods to `PexelsService`; added `VIDEO_API_BASE` constant |
| `btcedu/services/ffmpeg_service.py` | Added `normalize_video_clip()` (scale+pad+transcode, strip audio, optional duration trim); added `create_video_segment()` (like `create_segment()` but uses `-stream_loop -1` for video input) |
| `btcedu/core/stock_images.py` | Updated `search_stock_images()` to add video candidates for b_roll chapters when enabled; updated `rank_candidates()` to include `asset_type` and `duration_seconds` in candidate list + motion preference hint for b_roll/diagram; updated `finalize_selections()` to normalize and finalize video selections with `generate_method: "pexels_video"`; updated `_create_media_asset()` to accept `asset_type_override`; schema_version bumped to "3.1" when video candidates present |
| `btcedu/core/renderer.py` | Updated `_resolve_chapter_media()` to return 4-tuple `(media_path, audio_path, duration, asset_type)`; updated `render_video()` to call `create_video_segment()` for video assets and `create_segment()` for photos; updated `RenderSegmentEntry` with `asset_type` field; updated `_compute_render_content_hash()` to include `asset_type` |
| `btcedu/web/api.py` | Added `GET /episodes/<id>/stock/candidate-video` endpoint serving MP4 files with range support |
| `btcedu/web/static/app.js` | Updated stock candidates rendering to show video thumbnails with preview badge and duration; added `toggleVideoPreview()` for inline video preview |
| `btcedu/web/static/styles.css` | Added `.stock-video-badge`, `.stock-thumb-media`, `.stock-video-preview` CSS classes |
| `btcedu/prompts/templates/stock_rank.md` | Updated candidate listing to include `asset_type` and `duration_seconds`; added motion preference hint for b_roll and diagram visual types |
| `tests/test_renderer.py` | Updated `test_resolve_chapter_media` to unpack 4-tuple (backward compat assertion added) |

---

## Test Results

```
32 new tests in tests/test_stock_video.py — all passing
814 total tests — all passing
Ruff lint clean on all modified files
```

### Test Classes

| Class | Tests | Description |
|-------|-------|-------------|
| `TestPexelsVideoService` | 5 | Video API response parsing, HD file selection, fallback, preview download |
| `TestStockSearchWithVideos` | 6 | Enabled/disabled flags, duration filter, b_roll-only, asset_type fields, schema 3.1 |
| `TestMixedCandidateRanking` | 3 | asset_type in prompt, motion hint for b_roll, no hint for diagram |
| `TestManifestSchemaVideo` | 4 | Backward compat (no asset_type), video asset_type detection, RenderSegmentEntry field, hash changes |
| `TestNormalizeVideoClip` | 3 | Command flags (-an, fps, crf), target_duration -t flag, dry_run |
| `TestCreateVideoSegment` | 4 | -stream_loop -1 flag, 1:a audio map, overlays, dry_run |
| `TestRendererWithVideos` | 4 | resolve returns photo/video/default, full render with video segment produces correct manifest |
| `TestStockVideoAPI` | 3 | MP4 served correctly, non-MP4 rejected, 404 for missing file |

---

## Architecture Decisions

1. **Unified candidate pool** — photos and videos in the same `candidates` list per chapter. `asset_type` field distinguishes them.

2. **b_roll only** — video search runs only for chapters with `visual.type == "b_roll"`. Diagrams and screen_shares always use static images.

3. **Opt-in by default** — `pexels_video_enabled = False` means zero behavior change for existing episodes.

4. **Normalization at finalize time** — only the selected video is normalized (not all candidates). Saves compute on Pi hardware.

5. **No target_duration at finalize** — audio duration isn't available at finalize time; trimming happens at render time via `-t duration -shortest` in `create_video_segment()`.

6. **Backward compatibility** — `_resolve_chapter_media()` defaults `asset_type` to `"photo"` when the manifest entry has no `asset_type` field. All existing episodes continue to work.

7. **Schema versioning** — candidates manifest bumps to "3.1" when video candidates are present; "1.0" otherwise.

---

## Manual Smoke Test Steps

To test Phase 4 with a real episode:

```bash
# 1. Enable video mode
echo "PEXELS_VIDEO_ENABLED=true" >> .env
echo "PEXELS_VIDEO_PER_CHAPTER=2" >> .env
echo "PEXELS_VIDEO_MAX_DURATION=30" >> .env

# 2. Re-run stock image search for an episode at CHAPTERIZED status
btcedu stock search --episode-id <ep_id> --force

# 3. Check that video candidates appear in candidates_manifest.json
cat data/outputs/<ep_id>/images/candidates/candidates_manifest.json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); \
   [print(c.get('asset_type'), c.get('pexels_id'), c.get('duration_seconds')) \
    for ch in d['chapters'].values() for c in ch['candidates']]"

# 4. Open dashboard, navigate to stock images panel, verify:
#    - Video thumbnails show preview image + duration badge
#    - Clicking thumbnail shows inline video player
#    - Pin button works for video candidates

# 5. Rank candidates
btcedu stock rank --episode-id <ep_id> --force

# 6. Approve and finalize
btcedu stock finalize --episode-id <ep_id>

# 7. Verify final manifest has asset_type: "video" for selected videos
cat data/outputs/<ep_id>/images/manifest.json | python3 -c \
  "import json,sys; [print(i['chapter_id'], i.get('asset_type'), i.get('generation_method')) \
   for i in json.load(sys.stdin)['images']]"

# 8. Render
btcedu render --episode-id <ep_id> --force
```

---

## Deviations from Plan

None. All items from the implementation plan were implemented as specified.

### Items Deferred (out of scope per plan §9)

- Ken Burns / pan-and-zoom for photos
- Audio mixing (B-roll ambient sound always stripped)
- Slow-motion or loop disguise for short clips
- Video sub-clip selection (in/out points)
- New pipeline stages or DB tables
