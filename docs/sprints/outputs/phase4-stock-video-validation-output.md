# Phase 4 Validation Report: Stock Video Clips / B-Roll Support

**Date:** 2026-03-15
**Validator:** Claude Sonnet 4.6 (automated)
**Sprint output reviewed:** `phase4-stock-video-implement-output.md`
**Tests baseline:** 814 passing (782 prior + 32 new)

---

## 1. Verdict

**PASS**

All plan items are implemented correctly. The feature is properly opt-in, the video search/download/normalize/render path is complete and safe, backward compatibility is fully preserved, and the test suite covers the major failure modes. One minor observation (no test for the `finalize_selections` video normalization failure path) is noted but non-blocking.

---

## 2. Scope Check

| Plan Item | Implemented | Notes |
|-----------|-------------|-------|
| `pexels_video_enabled` config, default False | ✅ | Confirmed in `config.py` |
| `pexels_video_per_chapter`, `pexels_video_max_duration`, `pexels_video_preferred_quality` | ✅ | All 4 config fields present with correct defaults |
| `PexelsVideoFile`, `PexelsVideo`, `PexelsVideoSearchResult` dataclasses | ✅ | All in `pexels_service.py` |
| `search_videos()`, `download_video()`, `download_video_preview()`, `_select_video_file()` | ✅ | All implemented |
| `VIDEO_API_BASE = "https://api.pexels.com/videos"` | ✅ | Correct endpoint (separate from `/v1`) |
| `search_stock_images()` adds video candidates for b_roll only when enabled | ✅ | Confirmed with guard at lines 327-333 |
| Duration filter, quality filter | ✅ | `video.duration > max_duration` skip; `_select_video_file()` with quality |
| `asset_type` field on all candidates | ✅ | Photos get `"photo"`, videos get `"video"` |
| `candidates_manifest.json` schema 3.1 when videos present | ✅ | Correct version bump logic |
| `finalize_selections()` normalizes selected video via `normalize_video_clip()` | ✅ | Lines 1264-1296 |
| Normalization: scale/pad/yuv420p, strip audio, optional trim | ✅ | `-an` confirmed in ffmpeg command |
| `normalize_video_clip()` in `ffmpeg_service.py` | ✅ | Full implementation |
| `create_video_segment()` in `ffmpeg_service.py` | ✅ | `-stream_loop -1`, `1:a` audio map |
| `_resolve_chapter_media()` returns 4-tuple `(path, audio, duration, asset_type)` | ✅ | Lines 555-609 |
| `render_video()` branches on `asset_type` | ✅ | Lines 226-261 |
| `RenderSegmentEntry.asset_type` field | ✅ | With default `"photo"` |
| `_compute_render_content_hash()` includes `asset_type` | ✅ | Line 477 |
| `GET /episodes/<id>/stock/candidate-video` endpoint | ✅ | With range support, .mp4 guard, path validation |
| UI: video thumbnails with preview badge, `toggleVideoPreview()` | ✅ | Confirmed in `app.js` |
| UI: duration badge, inline video player | ✅ | Confirmed in `app.js` |
| CSS: `.stock-video-badge`, `.stock-thumb-media`, `.stock-video-preview` | ✅ | |
| `stock_rank.md` updated with `asset_type` and motion preference | ✅ | Motion preference block for b_roll |
| `_create_media_asset()` accepts `asset_type_override` | ✅ | `MediaAssetType.VIDEO` for video entries |
| Schema version 3.1 in candidates manifest when videos present | ✅ | Confirmed |
| 32 new tests | ✅ | All 8 test classes as planned |

No scope gaps identified.

---

## 3. Correctness Review

### 3.1 Config safety

`pexels_video_enabled: bool = False` is a proper Pydantic field with a safe default. All read sites in `stock_images.py` use `getattr(settings, "pexels_video_enabled", False)` — this double-safety pattern (Pydantic default + getattr fallback) means old code paths that receive a settings object without the field (e.g., mock objects in pre-Phase-4 tests) will not break. Correct.

### 3.2 Video candidate search

The guard condition at line 327 is tight and correct:
```python
if (
    getattr(settings, "pexels_video_enabled", False)
    and visual.type == "b_roll"
    and not _has_locked_selection(...)
):
```

- Only b_roll chapters get video candidates — diagrams and screen_shares remain photo-only. This is correct per the architecture decision.
- Locked chapters skip the video search, which is correct — no point re-downloading if a human already locked a pick.
- Duration filter (`video.duration > max_duration`) is applied before file download, saving bandwidth. Correct.
- `_select_video_file()` falls back to highest-resolution if preferred quality not available. Correct and safe.
- `pexels_v_` filename prefix for video files prevents Pexels ID collisions with photo files in the same chapter directory. Important correctness detail.
- Preview download failure is non-fatal (logs warning, sets `preview_rel_path = ""`). Correct — no candidate should be lost due to a preview JPEG failure.

### 3.3 Video API endpoint

`VIDEO_API_BASE = "https://api.pexels.com/videos"` — correct. The Pexels video search API is at `/videos/search`, not `/v1/videos/search`. This distinction matters.

Rate limiting: `_rate_limit_wait()` and `_record_request()` are shared between photo and video API calls. This is correct — Pexels applies a single rate limit across both APIs per API key.

### 3.4 Normalization at finalize time

**Critical design point confirmed correct:** `normalize_video_clip()` is called only on the *selected* candidate, not all downloads. `target_duration=None` at finalize time is intentional and correct — the audio duration isn't known until render time.

The normalized clip is written to `candidates/{chapter_id}_normalized.mp4` then copied to `images/{chapter_id}_selected.mp4`. Two-step process (normalize → copy) is slightly redundant but not harmful.

**Normalization failure path:** If normalization fails, a placeholder entry is created instead of crashing. This is the correct graceful degradation — a broken video clip shouldn't block the entire render.

### 3.5 Render pipeline

`create_video_segment()` key properties verified:

- `-stream_loop -1` appears **before** the `-i video_path` argument (lines 545–548). This is the correct ffmpeg ordering — the loop flag must precede the input it applies to.
- `-map 1:a` maps the TTS audio (second input) not the video's audio track. Correct.
- `-t {duration} -shortest` together ensure the output is trimmed to the TTS duration regardless of how long the looped video runs. Correct and safe — can't produce an oversized segment.
- `-an` is absent from `create_video_segment()` (audio stripping is not done here), which is correct — the video input's audio is simply never mapped (only `1:a` is mapped). Equivalent result, cleaner approach.
- Filter chain (scale → pad → drawtext overlays → fade) is identical to `create_segment()`, which means overlays and fades work identically for video and photo segments. Correct.

**Double transcoding concern:** Normalized clips are H.264/yuv420p already; `create_video_segment()` then re-encodes to H.264 again. This is a generation loss but is acceptable — the normalization step ensures consistent input to the render, and ffmpeg produces deterministic output from normalized input. The alternative (using `-c:v copy` to skip re-encoding) would risk compatibility issues if the normalized clip has timing mismatches with the overlay filter chain. Current approach is safe.

### 3.6 `_compute_render_content_hash` with `asset_type`

`asset_type` is included in the hash input (line 477). This means switching a chapter from photo to video (or back) will invalidate the render cache and trigger a re-render. Correct behavior.

### 3.7 Candidate-video API endpoint

Security properties confirmed:
- `secure_filename()` applied to all three path components (episode_id, chapter, filename)
- `_validate_episode_path()` constructs the path as `base_dir / episode_id / "images" / "candidates" / chapter / filename` and verifies the resolved path stays within `base_dir` — prevents path traversal
- `.mp4` extension check before path construction — non-MP4 requests rejected with 400
- `send_file(..., conditional=True)` enables HTTP range requests (byte-range seek for the browser video player). Correct.
- No episode DB existence check before serving the file — this matches the pattern used by the candidate image endpoint (`get_stock_candidate_image`) which also skips the DB check in favor of the path validation. Consistent behavior.

### 3.8 `RenderSegmentEntry` backward compatibility

`asset_type: str = "photo"` as a dataclass field default ensures deserialization of old render manifests (which won't have `asset_type`) still works. Correct.

---

## 4. Test Review

### 4.1 Test classes and coverage

| Class | Tests | Quality |
|-------|-------|---------|
| `TestPexelsVideoService` | 5 | Covers API response parsing, HD file selection, fallback, preview download — solid |
| `TestStockSearchWithVideos` | 6 | Enabled/disabled flags, duration filter, b_roll-only restriction, asset_type field, schema 3.1 — comprehensive |
| `TestMixedCandidateRanking` | 3 | asset_type in prompt, motion hint for b_roll, no hint for diagram — correct scope |
| `TestManifestSchemaVideo` | 4 | Backward compat (no asset_type), video asset_type detection, RenderSegmentEntry field, hash changes — all important |
| `TestNormalizeVideoClip` | 3 | `-an` flag, fps, crf presence, target_duration `-t` flag, dry_run — direct command verification |
| `TestCreateVideoSegment` | 4 | `-stream_loop -1` flag, `1:a` audio map, overlays present, dry_run — key correctness tests |
| `TestRendererWithVideos` | 4 | resolve returns photo/video/default, full render with video produces correct manifest — integration-level |
| `TestStockVideoAPI` | 3 | MP4 served correctly, non-MP4 rejected (400), 404 for missing file — security + functionality |

### 4.2 Key correctness tests

`TestCreateVideoSegment.test_stream_loop_flag` directly verifies that `-stream_loop` appears in the ffmpeg command and precedes the video input path. This is the most important correctness test for video rendering.

`TestCreateVideoSegment.test_audio_map_1a` verifies `"1:a"` appears in the command — ensures TTS audio is used, not video audio.

`TestNormalizeVideoClip.test_strips_audio` verifies `-an` appears in the command.

`TestManifestSchemaVideo.test_hash_changes_with_asset_type` verifies the render content hash differs when `asset_type` changes — prevents stale renders with mismatched asset types.

### 4.3 Gaps (non-blocking)

1. **`finalize_selections()` normalization failure path not tested.** The code falls back to a placeholder if `normalize_video_clip()` raises; no test exercises this path. Low probability in production, but worth noting.

2. **Duration filter boundary not tested.** `test_video_duration_filtered` tests clips longer than max duration being excluded. No test verifies a clip exactly at the max duration (`video.duration == max_duration`) is included (boundary condition: `>` not `>=`). Minor.

3. **`_select_video_file()` with empty `video_files` list not tested.** The fallback to `max(...)` or `None` when no files are available should be tested. The code handles it correctly (`if not video.video_files: return None`), but there's no test.

4. **Locked chapter + video enabled interaction not tested.** When a chapter has a locked selection and `pexels_video_enabled=True`, video search should be skipped. The code does this correctly (second `_has_locked_selection` check), but the test in `TestStockSearchWithVideos` only tests the disabled flag, not the locked guard.

None of these are blocking — the happy path and the main failure modes are covered.

### 4.4 Existing tests updated

`test_renderer.py::test_resolve_chapter_media` correctly updated to unpack 4-tuple. The test includes a backward compatibility assertion (default `asset_type="photo"` when field missing). Correct.

---

## 5. Backward Compatibility Check

| Concern | Status |
|---------|--------|
| Episodes with no `asset_type` in image manifest | ✅ `image_entry.get("asset_type", "photo")` defaults to photo |
| Old render manifests without `asset_type` in segment entries | ✅ `RenderSegmentEntry.asset_type = "photo"` default |
| Photo-only candidates manifest (schema 1.0 or 2.0) | ✅ Video search only added when enabled; schema 3.1 only written when videos present |
| `pexels_video_enabled = False` (default) | ✅ Zero code path change — video search block never entered |
| `finalize_selections()` with photo-only manifest | ✅ `asset_type = candidate.get("asset_type", "photo")` → takes the `else` branch |
| `_compute_render_content_hash()` with old manifests | ✅ `img.get("asset_type", "photo")` defaults gracefully |
| v1 pipeline episodes | ✅ Renderer raises `ValueError` for v1 episodes before reaching asset_type logic |
| Phase 2/3 tests | ✅ `mock_extract_intents` fixture in `test_stock_ranking.py` continues to isolate Phase 2/3; no fixture changes needed for Phase 4 |
| `select_stock_image()` (human pinning) | ✅ Operates on `pexels_id` matching — works identically for video and photo candidates |
| `auto_select_and_finalize()` | ✅ Uses `candidates[0]["selected"] = True` agnostically — works for any candidate type |

No backward compatibility issues found.

---

## 6. Required Fixes Before Commit

**None.** The implementation is correct, tests pass (814 total), and no blocking issues were identified.

---

## 7. Nice-to-Have Improvements

1. **Test `finalize_selections()` video normalization failure path.** A test where `normalize_video_clip()` raises an exception would verify the placeholder fallback is triggered. Adds confidence for production failure handling.

2. **Test locked chapter + video enabled guard.** A test case where a b_roll chapter has a locked selection and `pexels_video_enabled=True` verifying video search is skipped would close the coverage gap.

3. **Eliminate the double-copy in `finalize_selections()`.** `normalize_video_clip()` writes to `candidates/{chapter_id}_normalized.mp4`, then `shutil.copy2()` copies it to `images/{chapter_id}_selected.mp4`. Writing directly to the destination or using `shutil.move()` would be cleaner. Low priority — no correctness impact.

4. **Consider `original_resolution` in finalize manifest metadata.** The metadata block sets `"original_resolution": "1920x1080"` hardcoded (from `candidate['width']x{candidate['height']}` via the `"size"` field). If the Pexels video file is SD (e.g., 1280×720), the recorded value will be accurate since it comes from `PexelsVideoFile.width/height`. This is correct, but the field name `original_resolution` could be confused with the normalized output resolution. No fix needed, just a documentation note.

5. **Rate limit is shared between photo and video searches.** If an episode has many b_roll chapters and video is enabled, the combined search call count could approach the 180 req/hr limit faster. The existing `_rate_limit_wait()` handles this transparently, but it could be worth a note in `README` / smoke test instructions.

---

## 8. Summary

Phase 4 is a clean, well-scoped implementation. The opt-in design is correctly enforced at three levels (config default, search guard, `getattr` fallback), ensuring zero behavior change for existing episodes. The key correctness properties — `-stream_loop -1` ordering, TTS-only audio mapping, audio stripping during normalization, render cache invalidation on asset type change — are all verified by dedicated unit tests. The security properties of the new API endpoint mirror the existing endpoints exactly. Backward compatibility is fully preserved across all manifest schemas, the renderer, and the DB layer.

**Verdict: PASS — ready to ship.**
