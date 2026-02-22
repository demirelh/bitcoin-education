# Sprint 9 — Validation Prompt (Video Assembly / Render Pipeline — Part 1)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 9 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–8 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 9 (Phase 5, Part 1: Video Assembly — Foundation)** implementation of the btcedu video production pipeline.

Sprint 9 was scoped to:
- Create `ffmpeg_service.py` with `probe_media()`, `create_segment()`, `concatenate_segments()`
- Define and save render manifest format (`render_manifest.json`)
- Implement `render_video()` in `btcedu/core/renderer.py`
- Per-chapter segment rendering: image + audio + text overlays → video segment
- Chapter concatenation into `draft.mp4` (H.264 MP4, AAC audio, 1920x1080)
- Text overlay support via ffmpeg drawtext (Turkish character support)
- Missing asset fallbacks (placeholder images, skip missing audio)
- Add `render` CLI command with `--force`, `--dry-run`
- Integrate RENDER stage into v2 pipeline after TTS_DONE
- Record draft.mp4 in media_assets table
- Provenance, idempotency
- Write tests

Sprint 9 was NOT scoped to include: Review Gate 3, dashboard video preview, fancy transitions (fade/slide), YouTube publishing, background music, intro/outro.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. ffmpeg Service

- [ ] **1.1** `btcedu/services/ffmpeg_service.py` exists
- [ ] **1.2** `probe_media()` function: calls ffprobe, returns duration/resolution/codec info
- [ ] **1.3** `create_segment()` function: image + audio + overlays → video segment
- [ ] **1.4** `concatenate_segments()` function: multiple segments → single video
- [ ] **1.5** Uses `subprocess.run()` (not `shell=True`)
- [ ] **1.6** Commands built as lists (not strings) for security
- [ ] **1.7** Handles ffmpeg exit codes (non-zero = error)
- [ ] **1.8** Captures and parses stderr for error messages
- [ ] **1.9** Implements timeout per operation (prevents hanging)
- [ ] **1.10** `check_ffmpeg()` function verifies ffmpeg installation
- [ ] **1.11** Config-driven: ffmpeg path, resolution, fps, crf, preset, audio bitrate

### 2. Render Manifest

- [ ] **2.1** Render manifest saved to `data/outputs/{ep_id}/render/render_manifest.json`
- [ ] **2.2** Format matches §5G: episode_id, resolution, fps, codec, segments array
- [ ] **2.3** Per-segment: chapter_id, image path, audio path, duration, overlays, transitions
- [ ] **2.4** Manifest is self-contained (all info needed to re-run ffmpeg)
- [ ] **2.5** JSON written with `indent=2` and `ensure_ascii=False`
- [ ] **2.6** Overlay definitions include: type, text, font, position, timing (start/end)
- [ ] **2.7** Manifest generated from chapter JSON + image manifest + TTS manifest

### 3. Renderer Module

- [ ] **3.1** `btcedu/core/renderer.py` exists
- [ ] **3.2** `render_video()` function has correct signature matching existing stage patterns
- [ ] **3.3** Returns structured result (RenderResult or similar)
- [ ] **3.4** Loads chapter JSON, image manifest, and TTS manifest
- [ ] **3.5** **Pre-condition check**: verifies TTS_DONE status and ffmpeg availability
- [ ] **3.6** Maps chapters to image files and audio files correctly
- [ ] **3.7** Renders per-chapter segments in order
- [ ] **3.8** Concatenates all segments into `data/outputs/{ep_id}/render/draft.mp4`
- [ ] **3.9** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **3.10** Shows progress logging during render (segment X of Y)

### 4. Video Output Quality

- [ ] **4.1** Output format: H.264 MP4 with AAC audio
- [ ] **4.2** Resolution: 1920x1080 (or configured resolution)
- [ ] **4.3** Pixel format: yuv420p (required for broad compatibility)
- [ ] **4.4** Images scaled and padded correctly to target resolution (no stretching)
- [ ] **4.5** Audio syncs with image duration per segment
- [ ] **4.6** `-shortest` flag or equivalent prevents infinite loops from `-loop 1`

### 5. Text Overlays

- [ ] **5.1** Text overlays rendered via ffmpeg `drawtext` filter
- [ ] **5.2** Overlay types supported: lower_third, full_screen (at minimum)
- [ ] **5.3** Turkish characters render correctly (UTF-8, appropriate font)
- [ ] **5.4** Special characters escaped for ffmpeg: colons, single quotes, backslashes
- [ ] **5.5** Overlay timing: `enable='between(t,start,end)'` controls visibility
- [ ] **5.6** Font path configurable (or sensible fallback)

### 6. Missing Asset Handling

- [ ] **6.1** Chapters with no generated image use placeholder/default background
- [ ] **6.2** Chapters with no audio are skipped (logged as warning)
- [ ] **6.3** Partial render: some segments fail, rest still concatenated
- [ ] **6.4** Partial success recorded in RenderResult and provenance

### 7. Provenance & Idempotency

- [ ] **7.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/render_provenance.json`
- [ ] **7.2** Provenance format matches §3.6
- [ ] **7.3** Second run without `--force` skips rendering (draft.mp4 exists + inputs unchanged)
- [ ] **7.4** `--force` flag re-renders
- [ ] **7.5** Input hashes include: chapter JSON hash, image hashes, audio hashes
- [ ] **7.6** Content hashes use SHA-256

### 8. Cascade Invalidation

- [ ] **8.1** Image re-generation marks render as stale
- [ ] **8.2** TTS re-generation marks render as stale
- [ ] **8.3** Chapterization re-run marks render as stale (via cascading through images/TTS)
- [ ] **8.4** Chain: any upstream change → render marked stale

### 9. Storage & Media Assets

- [ ] **9.1** `draft.mp4` recorded in `media_assets` table (asset_type = "video")
- [ ] **9.2** Segment files preserved in `render/segments/` directory
- [ ] **9.3** File sizes tracked (important for storage management)
- [ ] **9.4** Render manifest preserved alongside draft video

### 10. CLI Command

- [ ] **10.1** `btcedu render <episode_id>` command exists and is registered
- [ ] **10.2** `--force` flag works
- [ ] **10.3** `--dry-run` flag works (generates manifest without running ffmpeg)
- [ ] **10.4** `btcedu render --help` shows useful help text
- [ ] **10.5** Validates episode exists and is TTS_DONE
- [ ] **10.6** Checks ffmpeg availability before starting
- [ ] **10.7** On success: episode status updated to RENDERED
- [ ] **10.8** Outputs summary: segments rendered, total duration, file size

### 11. Pipeline Integration

- [ ] **11.1** RENDER / RENDERED is properly wired in pipeline
- [ ] **11.2** `resolve_pipeline_plan()` includes RENDER for v2 episodes after TTS_DONE
- [ ] **11.3** No Review Gate 3 added (that's Sprint 10)
- [ ] **11.4** v1 pipeline is completely unaffected

### 12. V1 Pipeline + Previous Sprint Compatibility (Regression)

- [ ] **12.1** `btcedu status` still works for all episodes
- [ ] **12.2** v1 pipeline stages unmodified
- [ ] **12.3** All previous stages (correct, translate, adapt, chapterize, imagegen, tts) still work
- [ ] **12.4** All previous review gates still work
- [ ] **12.5** Chapter JSON schema consumed unmodified
- [ ] **12.6** Image manifest consumed unmodified
- [ ] **12.7** TTS manifest consumed unmodified
- [ ] **12.8** Existing dashboard pages still function
- [ ] **12.9** Existing tests still pass
- [ ] **12.10** No existing CLI commands broken

### 13. Test Coverage

- [ ] **13.1** ffmpeg service tests: probe_media, create_segment, concatenate_segments (mocked subprocess)
- [ ] **13.2** Text overlay escaping tests
- [ ] **13.3** ffmpeg availability check test
- [ ] **13.4** Timeout handling test
- [ ] **13.5** Renderer tests: manifest generation, chapter-to-asset mapping
- [ ] **13.6** Full render integration test (mocked ffmpeg)
- [ ] **13.7** Idempotency test
- [ ] **13.8** Force test
- [ ] **13.9** Missing asset fallback tests
- [ ] **13.10** CLI tests: command registration, help text
- [ ] **13.11** Pipeline tests: RENDER in v2 plan
- [ ] **13.12** All tests use mocked subprocess calls (no actual ffmpeg in CI)
- [ ] **13.13** All tests pass with `pytest tests/`

### 14. Scope Creep Detection

- [ ] **14.1** No Review Gate 3 / video review was implemented
- [ ] **14.2** No dashboard video preview was implemented
- [ ] **14.3** No fancy transitions (fade/slide) were implemented (cuts only)
- [ ] **14.4** No YouTube publishing was implemented
- [ ] **14.5** No background music or intro/outro was implemented
- [ ] **14.6** No video editing / trim controls were implemented
- [ ] **14.7** No existing stages were modified beyond pipeline integration
- [ ] **14.8** No existing manifest formats were modified
- [ ] **14.9** No unnecessary dependencies were added

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 9 is complete and ready for Sprint 10 (Render Polish + Review Gate 3). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 10 after fixes. |
| **FAIL** | Critical issues found. Sprint 9 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Video Output Quality Assessment:

If a real render was produced, verify:
- [ ] Video plays in VLC / mpv / browser without issues
- [ ] Images display at correct resolution (no stretching or cropping)
- [ ] Audio is in sync with visuals
- [ ] Text overlays are readable (correct font, size, position, timing)
- [ ] Turkish characters display correctly in overlays
- [ ] Chapter transitions are clean (no artifacts between segments)
- [ ] Total duration matches expected (sum of chapter audio durations)
- [ ] File size is reasonable (~30-50 MB per minute of video)

### Render Pipeline Robustness:

- [ ] ffmpeg errors produce actionable error messages (not just "exit code 1")
- [ ] Long renders don't time out unexpectedly
- [ ] Partial renders are recoverable (segment files preserved)
- [ ] Render is deterministic (same inputs → same output)
- [ ] draft.mp4 is suitable for YouTube upload (H.264, AAC, 1080p)

### Deferred Items Acknowledged:

- Review Gate 3 / video review workflow (Sprint 10)
- Dashboard video preview / player (Sprint 10)
- Fancy transitions (fade, slide, dissolve) (Sprint 10)
- YouTube publishing (Sprint 11)
- Background music / intro / outro
- Video editing / trim controls
- Parallel segment rendering
- 4K resolution support

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 5 (Text Overlays)** — Turkish character rendering in ffmpeg is a common failure point. Ensure proper UTF-8 handling and font selection.
- **Pay attention to Section 4 (Video Output Quality)** — the draft video must be suitable for YouTube upload. H.264 + AAC + yuv420p + 1080p is the standard.
- **Pay attention to Section 6 (Missing Asset Handling)** — the render pipeline should be resilient to missing images or audio. Not every chapter may have all assets.
- Verify that ffmpeg commands are built as lists (not shell strings) for security.
