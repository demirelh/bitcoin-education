---
name: smoke-test-video
description: Run the btcedu video smoke test to verify ffmpeg works on this machine (Raspberry Pi)
allowed-tools: Bash
---

Run the video pipeline smoke test and report results.

```bash
.venv/bin/btcedu smoke-test-video --keep 2>&1
```

This runs 4 steps:
1. `generate_test_video()` — synthetic test video via testsrc2
2. `normalize_video_clip()` — scale/pad/yuv420p normalization
3. `generate_silent_audio()` — silent AAC audio via anullsrc
4. `create_video_segment()` — stream_loop + audio overlay

Report: which steps passed/failed. If `--keep` was used, note the temp directory path for manual inspection.

If it fails, check:
- Is ffmpeg installed? (`ffmpeg -version`)
- Is this a Raspberry Pi with software encoding? (expect slow performance)
- Check `RENDER_TIMEOUT_SEGMENT` and `RENDER_PRESET` in `.env`
