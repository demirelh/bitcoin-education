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

- imagegen dispatch is profile-owned: tagesschau_tr uses Flux/Ideogram with Pexels fallback, not Gemini frame editing. Pexels search retries transport failures within three attempts; terminal fallback errors must not be masked by the earlier generative-provider error.
- Gemini frame editor: translates German text overlays to Turkish on extracted video frames
- ElevenLabs TTS: raw HTTP (not SDK), retry logic, text chunking for long narrations
- ffmpeg rendering: per-chapter segments → concat → draft.mp4
- Video normalization: `normalize_video_clip()` for resolution/fps/codec standardization
- Pi performance: software encoding is slow. Increase `RENDER_TIMEOUT_SEGMENT` for long videos
- Normalization failures gracefully fall back to placeholder images
