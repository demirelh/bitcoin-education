# Runbook: Video Smoke Test

## Purpose

Verify that the ffmpeg video pipeline works correctly on this machine (especially Raspberry Pi ARM64).

## Quick test (dry-run, no ffmpeg needed)

```bash
pytest tests/test_ffmpeg_smoke.py -v
```

Runs 7 dry-run tests that verify command construction without executing ffmpeg.

## Full test (requires ffmpeg)

```bash
btcedu smoke-test-video --keep
```

Runs 4 sequential steps:
1. `generate_test_video()` — synthetic H.264 video via `testsrc2`
2. `normalize_video_clip()` — scale/pad/yuv420p normalization
3. `generate_silent_audio()` — silent AAC audio via `anullsrc`
4. `create_video_segment()` — combine video + audio + overlay

Expected output: 4 PASS lines, then "PASS All smoke-test steps passed."

## Inspect output

```bash
# Find temp directory (printed by --keep)
ls /tmp/btcedu_smoke_*/

# Check output video
ffprobe -v quiet -print_format json -show_streams /tmp/btcedu_smoke_*/segment.mp4 \
  | python -c "import sys,json; d=json.load(sys.stdin); [print(s['codec_name'],s.get('pix_fmt','')) for s in d['streams']]"
# Expected: h264 yuv420p, aac
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| ffmpeg not found | `sudo apt install ffmpeg` |
| Timeout during normalize | Increase `RENDER_TIMEOUT_SEGMENT` in `.env` (900+ for Pi) |
| Slow encoding | Set `RENDER_PRESET=ultrafast` in `.env` |
| codec not supported | Check `ffmpeg -codecs` for libx264 and aac support |

## Custom resolution

```bash
btcedu smoke-test-video --resolution 1280x720 --keep
```
