# Render Flow

## Overview

The render stage combines chapter images/videos with TTS audio to produce a draft MP4 video.

## Prerequisites

Episode must be at `TTS_DONE` status with:
- `images/manifest.json` — finalized visual assets
- `tts/manifest.json` — per-chapter audio files
- `chapters.json` — chapter definitions

## Flow

```
render_video() in renderer.py
  1. Load chapter data + image manifest + TTS manifest
  2. For each chapter:
     a. If video asset: normalize_video_clip() (if not already normalized)
     b. create_video_segment() — combine visual + audio via ffmpeg
        - Photos: static image + TTS audio
        - Videos: stream_loop video + TTS audio overlay
        - Text overlays: chapter titles via drawtext filter
  3. concat_segments() — concatenate all chapter segments
  4. Write render_manifest.json (provenance)
  5. Set episode status to RENDERED
```

## ffmpeg operations

All via `btcedu/services/ffmpeg_service.py`:

| Function | Purpose |
|----------|---------|
| `normalize_video_clip()` | Scale/pad to target resolution, H.264 + yuv420p |
| `create_video_segment()` | Per-chapter: visual + audio + overlays |
| `concat_segments()` | Join all chapter segments into draft.mp4 |
| `probe_media()` | ffprobe wrapper for validation |
| `generate_test_video()` | Synthetic test video (smoke test) |
| `generate_silent_audio()` | Silent audio track (smoke test) |

## Raspberry Pi considerations

- **Software encoding only**: no hardware H.264 encoder on Pi
- **Config tuning**: `RENDER_PRESET=ultrafast` and `RENDER_TIMEOUT_SEGMENT=900` in `.env`
- **Smoke test**: `btcedu smoke-test-video --keep` validates the full pipeline without real content

## Output

- `data/outputs/{ep_id}/render/draft.mp4` — rendered video
- `data/outputs/{ep_id}/render/render_manifest.json` — provenance metadata

## Error handling

- Segment creation failure: logged, episode marked FAILED
- Normalization failure: falls back to placeholder (photo), warning logged
- Timeout: configurable via `render_timeout_segment` (default 300s, Pi needs 900s)
