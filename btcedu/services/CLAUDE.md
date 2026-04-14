# btcedu/services/ — External API Wrappers

## Pattern

Each service uses a Protocol for swappable implementations:
- `ImageGenService` protocol -> `DallE3ImageService`
- `TTSService` protocol -> `ElevenLabsService`
- `YouTubeService` protocol -> `YouTubeDataAPIService` / `DryRunYouTubeService`

## Services

- `claude_service.py` — `call_claude()` -> `ClaudeResponse(text, input_tokens, output_tokens, cost_usd)`. Uses anthropic SDK. Also: `compute_prompt_hash()`, `calculate_cost()`
- `elevenlabs_service.py` — raw HTTP (not SDK). Retry logic, text chunking for long narrations.
- `image_gen_service.py` — DALL-E 3 via openai SDK
- `pexels_service.py` — Pexels stock photo/video search via raw HTTP
- `ffmpeg_service.py` — ffmpeg subprocess wrapper: `normalize_video_clip()`, `create_video_segment()`, `concat_segments()`, `probe_media()`, `generate_test_video()`, `generate_silent_audio()`
- `youtube_service.py` — YouTube Data API upload + OAuth. `authenticate()`, `check_token_status()` -> `{valid, expired, expiry, can_refresh, error}`
- `feed_service.py` — RSS/YouTube feed parsing -> `list[EpisodeInfo]`
- `download_service.py` — yt-dlp audio download
- `transcription_service.py` — OpenAI Whisper API, auto-chunks large audio files
- `gemini_image_service.py` — Gemini 2.0 Flash image editing via raw HTTP REST API. `edit_image()` -> `GeminiEditResult(image_path, cost_usd)`
- `gemini_image_service.py` — Gemini 2.0 Flash image editing via raw HTTP REST API. `edit_image()` -> `GeminiEditResult(image_path, cost_usd)`

## Conventions

- Raw HTTP (`requests`) for ElevenLabs, Pexels, and Gemini (no SDKs)
- All services are stateless (instantiated per-call or with minimal config)
- Tests mock all external APIs — no real API calls ever
