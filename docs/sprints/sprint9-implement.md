# Sprint 9 — Implementation Prompt (Video Assembly / Render Pipeline — Part 1)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 9 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–8 completed codebase
> - **Expected output**: All code changes (new files, modified files), ffmpeg service, renderer module, render manifest, CLI command, tests — committed and passing.

---

## Context

You are implementing **Sprint 9 (Phase 5, Part 1: Video Assembly — Foundation)** of the btcedu video production pipeline.

Sprints 1–8 are complete:
- Foundation, Correction, Review System, Translation, Adaptation, Chapterization, Image Generation, TTS — all functional.

Sprint 9 builds the **core render pipeline** — assembling chapter images + TTS audio + text overlays into a draft video using ffmpeg. This is Part 1 of the RENDER phase; Sprint 10 adds transitions, Review Gate 3, and dashboard video preview.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 9 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/tts.py`, `btcedu/core/image_generator.py`, `btcedu/core/chapter_schema.py`, `btcedu/core/pipeline.py`, `btcedu/models/media_asset.py`, `btcedu/config.py`, `btcedu/cli.py`.

2. **Extend configuration** — add to `btcedu/config.py`:
   - `FFMPEG_PATH: str = "ffmpeg"` (system ffmpeg path)
   - `FFPROBE_PATH: str = "ffprobe"` (system ffprobe path)
   - `RENDER_RESOLUTION: str = "1920x1080"` (output video resolution)
   - `RENDER_FPS: int = 30` (output framerate)
   - `RENDER_CRF: int = 23` (H.264 quality, 0-51, lower = better)
   - `RENDER_PRESET: str = "medium"` (H.264 encoding speed; use "fast" on Pi)
   - `RENDER_AUDIO_BITRATE: str = "192k"` (AAC audio bitrate)
   - `RENDER_FONT_PATH: str = ""` (optional: path to NotoSans or similar for Turkish chars)

3. **Create ffmpeg service** — `btcedu/services/ffmpeg_service.py`:

   - **`probe_media(file_path: str) -> MediaInfo`**:
     - Calls `ffprobe -v quiet -print_format json -show_format -show_streams <file>`
     - Returns `MediaInfo` dataclass: duration_seconds, width, height, codec, sample_rate, file_size_bytes
     - Used to get actual audio duration and validate inputs

   - **`create_segment(image_path, audio_path, output_path, duration, overlays, settings) -> SegmentResult`**:
     - Builds ffmpeg command:
       ```
       ffmpeg -loop 1 -i {image_path} -i {audio_path} \
         -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,{drawtext_filters}" \
         -c:v libx264 -preset {preset} -crf {crf} \
         -c:a aac -b:a {bitrate} \
         -t {duration} -pix_fmt yuv420p -shortest \
         {output_path}
       ```
     - Handles text overlays via `drawtext` filter:
       - `lower_third`: text at bottom center with semi-transparent background
       - `full_screen`: centered large text
       - `bullet_list`: left-aligned text block
       - `highlight`: text at top with accent color
     - `drawtext` filter format: `drawtext=text='...':fontfile={font}:fontsize={size}:fontcolor={color}:x={x}:y={y}:enable='between(t,{start},{end})'`
     - Escape special characters in text for ffmpeg (colons, single quotes, backslashes)
     - Returns `SegmentResult`: success (bool), output_path, duration_seconds, file_size_bytes, command (for debugging)

   - **`concatenate_segments(segment_paths, output_path) -> ConcatResult`**:
     - Generates concat list file:
       ```
       file 'segment_ch01.mp4'
       file 'segment_ch02.mp4'
       ...
       ```
     - Calls: `ffmpeg -f concat -safe 0 -i list.txt -c copy {output_path}`
     - Returns `ConcatResult`: success, output_path, total_duration_seconds, file_size_bytes

   - **Error handling**:
     - Check ffmpeg exit code (0 = success)
     - Parse stderr for error messages
     - Timeout: 10 minutes per segment, 30 minutes for final concat (configurable)
     - Return descriptive error messages on failure

   - **ffmpeg availability check**:
     - `check_ffmpeg() -> bool` — run `ffmpeg -version` to verify installation
     - Called once before rendering starts

4. **Create RenderResult dataclass** — include: episode_id, draft_video_path, render_manifest_path, total_duration_seconds, total_size_bytes, segments_rendered (count), provenance, cost (render is local, so CPU time rather than API cost).

5. **Implement `render_video()`** in `btcedu/core/renderer.py`:
   - **Pre-condition check**: Episode status is TTS_DONE. Verify ffmpeg is available.
   - Check idempotency: `draft.mp4` exists AND all input hashes (chapters + images + audio) match
   - **Step 1: Load inputs**:
     - Chapter JSON from `data/outputs/{ep_id}/chapters.json`
     - Image manifest from `data/outputs/{ep_id}/images/manifest.json`
     - TTS manifest from `data/outputs/{ep_id}/tts/manifest.json`
   - **Step 2: Build render manifest**:
     - Map each chapter to its image file and audio file
     - Get actual audio duration from TTS manifest (or probe via ffprobe)
     - Include overlay definitions from chapter JSON
     - Save manifest to `data/outputs/{ep_id}/render/render_manifest.json`
   - **Step 3: Render segments**:
     - For each chapter in order:
       - Resolve image path (from image manifest or fallback placeholder for title_card/talking_head)
       - Resolve audio path (from TTS manifest)
       - Get duration from audio
       - Extract overlays from chapter definition
       - Call `ffmpeg_service.create_segment()`
       - Save segment to `data/outputs/{ep_id}/render/segments/segment_{chapter_id}.mp4`
       - Log progress (chapter X of Y)
     - If a segment fails: log error, continue to next, track failure
   - **Step 4: Concatenate**:
     - Collect all successful segment paths in order
     - Call `ffmpeg_service.concatenate_segments()`
     - Output: `data/outputs/{ep_id}/render/draft.mp4`
   - **Step 5: Cleanup and provenance**:
     - Save provenance to `data/outputs/{ep_id}/provenance/render_provenance.json`
     - Record `draft.mp4` in `media_assets` table (asset_type = "video")
     - Segment files can be kept for debugging or deleted to save space (configurable)
   - Return RenderResult

6. **Handle missing assets gracefully**:
   - If a chapter has no image (e.g., `title_card` that wasn't generated): use a solid color background or a default brand image
   - If a chapter has no audio: skip that chapter in the render (log warning)
   - If some segments fail: concatenate the successful ones, mark render as partial

7. **Render Manifest format** — `data/outputs/{ep_id}/render/render_manifest.json`:
   ```json
   {
     "episode_id": "abc123",
     "resolution": "1920x1080",
     "fps": 30,
     "codec": "libx264",
     "preset": "medium",
     "crf": 23,
     "audio_codec": "aac",
     "audio_bitrate": "192k",
     "segments": [
       {
         "chapter_id": "ch01",
         "image": "images/ch01_intro.png",
         "audio": "tts/ch01.mp3",
         "duration_seconds": 58.3,
         "overlays": [
           {
             "type": "lower_third",
             "text": "Bitcoin Nedir?",
             "font": "NotoSans-Bold",
             "position": "bottom_center",
             "start": 2.0,
             "end": 7.0
           }
         ],
         "transition_in": "cut",
         "transition_out": "cut"
       }
     ],
     "generated_at": "2026-02-22T10:00:00Z"
   }
   ```

8. **Add `render` CLI command** to `btcedu/cli.py`:
   - `btcedu render <episode_id>` with `--force`, `--dry-run`
   - Validate episode exists and is at TTS_DONE status
   - Check ffmpeg availability before starting
   - Show progress: "Rendering segment 3/8..."
   - On success: update episode status to RENDERED
   - On failure: log error with ffmpeg stderr, leave status unchanged
   - Output summary: segments rendered, total duration, file size, render time

9. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
   - Ensure RENDERED is in `PipelineStage` enum (should exist from Sprint 1)
   - Update `resolve_pipeline_plan()` to include RENDER for v2 episodes after TTS_DONE
   - Position: TTS_DONE → RENDER → (Review Gate 3 in Sprint 10)
   - No review gate in this sprint (added in Sprint 10)

10. **Write tests**:
    - `tests/test_ffmpeg_service.py`:
      - Mock subprocess: probe_media returns valid info
      - Mock subprocess: create_segment succeeds, fails
      - Mock subprocess: concatenate_segments succeeds, fails
      - Text overlay escaping: special characters handled
      - ffmpeg availability check
      - Timeout handling
    - `tests/test_renderer.py`:
      - Unit: render manifest generation from chapter + image + TTS inputs
      - Unit: chapter-to-asset mapping (resolving image and audio paths)
      - Unit: overlay filter generation
      - Integration: full render with mocked ffmpeg service
      - Idempotency: second run skips
      - Force: `--force` re-renders
      - Missing assets: graceful fallback
      - Partial render: some segments fail, rest concatenated
    - CLI test: `btcedu render --help` works
    - Pipeline test: RENDER included in v2 plan after TTS_DONE

11. **Verify**:
    - Run `pytest tests/`
    - Pick an episode with TTS_DONE status
    - Run `btcedu render <ep_id> --dry-run` (verify manifest generation without ffmpeg)
    - Run `btcedu render <ep_id>`
    - Verify `draft.mp4` at `data/outputs/{ep_id}/render/draft.mp4`
    - Verify render manifest at `data/outputs/{ep_id}/render/render_manifest.json`
    - Verify segments at `data/outputs/{ep_id}/render/segments/`
    - Play draft.mp4 to verify: images show, audio plays, overlays visible, chapters concatenated
    - Verify provenance at `data/outputs/{ep_id}/provenance/render_provenance.json`
    - Run again → verify skipped (idempotent)
    - Run with `--force` → verify re-renders
    - Run `btcedu status` → verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement Review Gate 3 / video review workflow (Sprint 10).
- **Do NOT** implement dashboard video preview (Sprint 10).
- **Do NOT** implement fancy transitions (fade, slide) — use cuts only. Transitions are Sprint 10.
- **Do NOT** implement YouTube publishing (Sprint 11).
- **Do NOT** implement background music or intro/outro.
- **Do NOT** implement video editing or trim controls.
- **Do NOT** modify existing stages (correct, translate, adapt, chapterize, imagegen, tts).
- **Do NOT** modify the chapter JSON schema, image manifest, or TTS manifest formats.

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/tts.py` and `btcedu/core/image_generator.py` for stage patterns.
- **Service layer**: Follow `btcedu/services/image_gen_service.py` or `btcedu/services/elevenlabs_service.py` for service wrapper patterns.
- **CLI commands**: Follow existing Click command patterns.
- **subprocess**: Use `subprocess.run()` with `capture_output=True`, `check=False` (handle exit codes manually for better error messages).

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred to Sprint 10
- Manual verification steps (including playing the draft video)

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- ffmpeg commands must handle Turkish characters in text overlays (use UTF-8, specify fontfile with Turkish character support).
- Escape special characters for ffmpeg drawtext filter: single quotes, colons, backslashes, semicolons.
- Use `subprocess.run()` — do not use shell=True (security risk). Build command as a list.
- Draft video output format: H.264 MP4 with AAC audio, suitable for YouTube upload.
- Keep rendered segments for debugging (don't auto-delete).

---

## Definition of Done

- [ ] `btcedu/services/ffmpeg_service.py` exists with `probe_media()`, `create_segment()`, `concatenate_segments()`
- [ ] ffmpeg service handles errors gracefully (exit codes, stderr, timeout)
- [ ] ffmpeg availability check works
- [ ] `btcedu/core/renderer.py` exists with `render_video()` function
- [ ] Render manifest generated and saved to `data/outputs/{ep_id}/render/render_manifest.json`
- [ ] Per-chapter segments rendered to `data/outputs/{ep_id}/render/segments/`
- [ ] Segments concatenated into `data/outputs/{ep_id}/render/draft.mp4`
- [ ] Output is H.264 MP4 with AAC audio at 1920x1080
- [ ] Text overlays rendered via ffmpeg drawtext (Turkish characters supported)
- [ ] Missing assets handled gracefully (placeholder images, warnings for missing audio)
- [ ] `draft.mp4` recorded in `media_assets` table (asset_type = "video")
- [ ] `btcedu render <episode_id>` CLI works with `--force`, `--dry-run`
- [ ] Pipeline plan includes RENDER for v2 episodes after TTS_DONE
- [ ] Episode status updated to RENDERED on success
- [ ] Idempotency: second run skips, `--force` re-renders
- [ ] Provenance JSON stored
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- Review Gate 3 / video review workflow (Sprint 10)
- Dashboard video preview / player (Sprint 10)
- Fancy transitions (fade, slide, dissolve) between segments (Sprint 10)
- YouTube publishing (Sprint 11)
- Background music or intro/outro
- Video editing / trim controls
- Parallel segment rendering
- Cloud rendering offload
- 4K resolution support (1080p only for now)
