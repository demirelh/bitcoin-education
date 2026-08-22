# btcedu/services/ — External API Wrappers

## Pattern

Each service uses a Protocol for swappable implementations:
- `ImageGenService` protocol -> `DallE3ImageService`
- `TTSService` protocol -> `ElevenLabsService`
- `YouTubeService` protocol -> `YouTubeDataAPIService` / `DryRunYouTubeService`

## Services

- `claude_service.py` — `call_claude()` -> `ClaudeResponse(text, input_tokens, output_tokens, cost_usd)`. Providers: `anthropic`, `openai`, `github_models`, `copilot_cli`. Also: `compute_prompt_hash()`, `calculate_cost()`
- `elevenlabs_service.py` — raw HTTP (not SDK). Provider retries, quota-only
  account fallback, timestamp/alignment responses and callbacks that charge
  every successful billed request. The 750-character splitting and quality
  retries live in `core/tts.py`.
- `failover_service.py` — typed node/operator client for the external control
  plane; reads bearer tokens from settings or credential files, sends
  heartbeats, manages leases/completions/reconciliation and fails closed when
  failover is enabled
- `local_recorder_service.py` — reads only atomically committed `.DONE`
  recordings, validates completion/provenance and adapts local MP4s to
  `EpisodeInfo`
- `github_actions_service.py` — GitHub Actions workflow dispatch/artifact client
  used by remote rendering
- `image_provider_factory.py` plus `flux_service.py` / `ideogram_service.py` —
  profile-owned generative image routing
- `image_gen_service.py` — DALL-E 3 via openai SDK
- `pexels_service.py` — Pexels stock photo/video search via raw HTTP
- `meteo_service.py` — Open-Meteo / DWD ICON forecasts via urllib (`OpenMeteoService`, `MeteoService` Protocol). One multi-location request, never raises: failures degrade to an empty list. `_request()` is the seam patched in tests.
- `notify_service.py` — WhatsApp push for pipeline failures via the local whatsapp-service REST API. Never raises, skipped when disabled or in dry-run.
- `errors.py` — `ErrorCategory` (incl. `PERMANENT_QUOTA` for exhausted provider credits), `is_transient()`, `ERROR_SUGGESTIONS`
- `ffmpeg_service.py` — ffmpeg subprocess wrapper: `normalize_video_clip()`, `create_video_segment()`, `concat_segments()`, `probe_media()`, `generate_test_video()`, `generate_silent_audio()`
- `youtube_service.py` — YouTube Data API upload + OAuth. `authenticate()`, `check_token_status()` -> `{valid, expired, expiry, can_refresh, error}`
- `feed_service.py` — RSS/YouTube feed parsing -> `list[EpisodeInfo]`
- `download_service.py` — yt-dlp audio download
- `transcription_service.py` — OpenAI Whisper API, auto-chunks large audio files
- `gemini_image_service.py` — Gemini 2.0 Flash image editing via raw HTTP REST API. `edit_image()` -> `GeminiEditResult(image_path, cost_usd)`

## Conventions

- Raw HTTP (`requests`) for ElevenLabs, Pexels, and Gemini (no SDKs); `urllib` for Open-Meteo and the WhatsApp notifier
- Services are stateless or hold only minimal per-run state. The ElevenLabs
  account switch is intentionally sticky for one service instance after proven
  quota exhaustion.
- Tests mock all external APIs — no real API calls ever
- Never log bearer tokens, API keys, rejected credentials or credential-file
  contents; use the repository redaction helpers

<!--
Documentation sync
Baseline: d1b4676
Synced through: current working tree
Date: 2026-08-22
-->
