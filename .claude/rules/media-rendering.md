---
paths:
  - btcedu/core/renderer.py
  - btcedu/core/frame_editor.py
  - btcedu/core/image_generator.py
  - btcedu/core/tts.py
  - btcedu/core/stock_images.py
  - btcedu/services/ffmpeg_service.py
  - btcedu/services/gemini_image_service.py
  - btcedu/services/elevenlabs_service.py
---

# Media & Rendering Rules

- imagegen dispatch: tagesschau_tr episodes → Gemini 2.0 Flash frame editing (~$0.003/image); all others → Pexels stock images
- Gemini frame editor: translates German text overlays to Turkish on extracted video frames
- ElevenLabs TTS: raw HTTP (not SDK), retry logic, text chunking for long narrations
- ffmpeg rendering: per-chapter segments → concat → draft.mp4
- Video normalization: `normalize_video_clip()` for resolution/fps/codec standardization
- Pi performance: software encoding is slow. Increase `RENDER_TIMEOUT_SEGMENT` for long videos
- Normalization failures gracefully fall back to placeholder images
