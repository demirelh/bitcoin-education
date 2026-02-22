# Sprint 9 — Planning Prompt (Video Assembly / Render Pipeline — Part 1)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–8 completed codebase (especially `btcedu/core/tts.py` and `btcedu/core/image_generator.py` for input asset patterns, `btcedu/core/chapter_schema.py`, `btcedu/models/media_asset.py`, `btcedu/services/`, `btcedu/core/pipeline.py`, `btcedu/config.py`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering the ffmpeg service, render manifest format, renderer module (single-segment rendering + text overlays), CLI command, pipeline integration, and tests. Sprint 9 focuses on the foundational render pipeline; Sprint 10 completes it with full concatenation, Review Gate 3, and dashboard video preview.

---

## Context

You are planning **Sprint 9 (Phase 5, Part 1: Video Assembly — Foundation)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–8 are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: corrector module, CORRECT stage, Review Gate 1.
- Review System: reviewer module, dashboard review queue + diff viewer + approve/reject/request-changes.
- Translation: translator module, TRANSLATE stage.
- Adaptation: adapter module, ADAPT stage, Review Gate 2.
- Chapterization: chapterizer module, chapter JSON schema (Pydantic), chapters.json, chapter viewer.
- Image Generation: image generator, ImageGenService (DALL-E 3), media_assets table, image manifest, image gallery.
- TTS: TTS module, ElevenLabsService, per-chapter audio, TTS manifest, audio preview.

Sprint 9 is **Part 1 of the RENDER phase** (Phase 5). It focuses on building the core render infrastructure:
- ffmpeg service wrapper for video composition
- Render manifest format (timeline + assets → ffmpeg instructions)
- Per-chapter segment rendering (image + audio + text overlays → video segment)
- Basic concatenation of segments into a draft video

Sprint 10 will complete the render phase with: transitions, final polish, Review Gate 3 integration, and dashboard video preview.

### Sprint 9 Focus (from MASTERPLAN.md §4 Phase 5, §5G)

1. Create `ffmpeg_service.py` — wrapper around ffmpeg CLI for video composition operations.
2. Define render manifest format (`render_manifest.json`) — the specification that drives ffmpeg operations.
3. Implement `render_video()` in `btcedu/core/renderer.py` — generate render manifest from chapter JSON + images + audio, then execute ffmpeg.
4. Per-chapter rendering: image (scaled to 1080p) + audio → video segment.
5. Text overlay support using ffmpeg drawtext filter.
6. Chapter concatenation using ffmpeg concat demuxer.
7. Output: H.264 MP4, AAC audio at `data/outputs/{ep_id}/render/draft.mp4`.
8. Add `render` CLI command with `--force`, `--dry-run`.
9. Integrate into pipeline — RENDER after TTS_DONE, before REVIEW GATE 3.
10. Provenance, idempotency.
11. Write tests.

### Relevant Subplans

- **Subplan 5G** (Video Assembly / Render Pipeline) — slices 1–4: ffmpeg service (single image + audio → segment), text overlay support, chapter concatenation, full render pipeline from manifest.
- **§8** (Idempotency) — RENDER stage: already done = `draft.mp4` exists AND all inputs unchanged. Force re-run always possible (deterministic from inputs).
- **§3.6** (Provenance Model) — provenance JSON format.
- **§11** (Decision Matrix) — ffmpeg CLI recommended over MoviePy/OpenCV. H.264 MP4, AAC audio.
- **§3.5** (Artifact Storage) — render artifacts in `data/outputs/{ep_id}/render/`.

---

## Your Task

Produce a detailed implementation plan for Sprint 9. The plan must include:

1. **Sprint Scope Summary** — one paragraph restating scope. Emphasize this is Part 1: core render pipeline without Review Gate 3 or dashboard video preview (those are Sprint 10).
2. **File-Level Plan** — for every file that will be created or modified.
3. **ffmpeg Service Design** — `btcedu/services/ffmpeg_service.py`:
   - Wrapper around `subprocess.run()` calling ffmpeg
   - Key operations:
     - `create_segment(image_path, audio_path, output_path, duration, overlays) -> SegmentResult`
     - `concatenate_segments(segment_paths, output_path) -> ConcatResult`
     - `probe_media(file_path) -> MediaInfo` (get duration, resolution, etc. via ffprobe)
   - Error handling: ffmpeg exit codes, stderr parsing, timeout for long renders
   - Config: ffmpeg path (default: `ffmpeg`), ffprobe path, output resolution, fps, codec settings
4. **Render Manifest Format** — `data/outputs/{ep_id}/render/render_manifest.json` per §5G:
   - Top-level: episode_id, resolution, fps, segments array
   - Per-segment: chapter_id, image path, audio path, duration, overlays (type, text, font, position, timing), transition_in, transition_out
   - Manifest is generated from chapter JSON + image manifest + TTS manifest
5. **Renderer Module Design** — `render_video()` function:
   - Function signature and return type (`RenderResult` dataclass)
   - Processing flow:
     a. Load chapter JSON, image manifest, TTS manifest
     b. Build render manifest (map chapters to assets)
     c. For each chapter: create video segment (image + audio + overlays)
     d. Concatenate all segments into `draft.mp4`
     e. Save render manifest
     f. Save provenance
   - Segment rendering: image scaled/padded to 1920x1080, loop for audio duration, overlays composited
   - Text overlay: ffmpeg `drawtext` filter for lower_third, full_screen, bullet_list, highlight overlay types
   - Font handling: specify NotoSans or a safe fallback for Turkish characters
6. **ffmpeg Filter Complex Design** — per-segment:
   - Image input: `-loop 1 -i image.png`
   - Audio input: `-i chapter.mp3`
   - Scale/pad to 1920x1080: `scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2`
   - Duration: `-t {audio_duration}`
   - Text overlays: `drawtext=text='...':fontfile=...:fontsize=...:fontcolor=...:x=...:y=...:enable='between(t,start,end)'`
   - Output codec: `-c:v libx264 -preset medium -crf 23 -c:a aac -b:a 192k`
7. **Concatenation** — ffmpeg concat demuxer:
   - Generate concat list file
   - `ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4`
   - Handle chapters with different codecs (re-encode if needed)
8. **CLI Command Design** — `btcedu render <episode_id>` with `--force`, `--dry-run`.
9. **Pipeline Integration** — RENDER after TTS_DONE, before APPROVED (Review Gate 3 in Sprint 10).
10. **Provenance, Idempotency** — render is deterministic from inputs. Idempotency: draft.mp4 exists AND all input hashes match.
11. **Performance Considerations** — rendering on Raspberry Pi:
    - Estimated render time: ~10-30 min for 15-min video
    - Use `-preset medium` (balance quality vs speed; can switch to `fast` on Pi)
    - Consider segment-by-segment rendering to show progress
    - No parallelism (sequential render is fine)
12. **Test Plan** — list each test, what it asserts, file it belongs to.
13. **Implementation Order** — numbered sequence.
14. **Definition of Done** — checklist.
15. **Non-Goals** — explicit list (Review Gate 3, dashboard video preview, transitions — deferred to Sprint 10).

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected.
- **ffmpeg is required**: Assume ffmpeg is installed on the target system (it's listed as a system dependency). Do not bundle ffmpeg.
- **Follow existing patterns**: Renderer should mirror existing stage patterns.
- **Sprint 9 = foundation, Sprint 10 = polish**: Sprint 9 builds the working render pipeline. Sprint 10 adds transitions, Review Gate 3, and video preview. Keep Sprint 9 focused on getting a working draft video.
- **No Review Gate 3 in this sprint**: The pipeline sets status to RENDERED. Review Gate 3 integration is Sprint 10.
- **No dashboard video preview in this sprint**: Video preview is Sprint 10.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections. Include the ffmpeg service interface, render manifest schema, ffmpeg command templates, and function signatures.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- `[ASSUMPTION]`: ffmpeg is available at the system PATH. The config allows overriding the path for non-standard installations.
- `[ASSUMPTION]`: Font for Turkish text overlays is Noto Sans (NotoSans-Regular.ttf / NotoSans-Bold.ttf). If not available on the system, the plan should document how to install it or fall back to a default.
- `[ASSUMPTION]`: Transitions (fade, cut, slide) between segments will be basic in Sprint 9 (cut only). Fancy transitions are Sprint 10.
- Consider error recovery: if ffmpeg fails mid-render, the partially-rendered segments should be preserved so the render can be resumed.
- Storage: draft.mp4 will be ~500MB for a 15-min video. Document storage implications.
- The render manifest should be self-contained — all information needed to re-run ffmpeg should be in the manifest (for reproducibility and debugging).
