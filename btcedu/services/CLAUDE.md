# btcedu/services/ — External API Wrappers

## Pattern

Each service uses a Protocol for swappable implementations:
- `ImageGenService` protocol -> `DallE3ImageService`
- `TTSService` protocol -> `ElevenLabsService`
- `YouTubeService` protocol -> `YouTubeDataAPIService` / `DryRunYouTubeService`

## Services

- `claude_service.py` — `call_claude()` -> `ClaudeResponse(text, input_tokens, output_tokens, cost_usd)`. Providers: `anthropic`, `openai`, `github_models`, `copilot_cli`; the current Copilot CLI producer default is `claude-sonnet-5`. Also: `compute_prompt_hash()`, `calculate_cost()`
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
- `anchor_service.py` — provider-neutral D-ID/HeyGen talking-avatar adapters.
  HeyGen can request alpha-capable WebM, but active profiles remain on opaque
  MP4 until the renderer gains alpha-aware studio compositing.
- `image_provider_factory.py` plus `flux_service.py` / `ideogram_service.py` —
  profile-owned generative image routing. Flux and Ideogram retry transient CDN
  failures while downloading an already-generated (and already-billed) image,
  as the `dalle3` provider has always done. The `dalle3` provider name is a
  routing key, not a live model id: OpenAI retired dall-e-3 for image
  generation, so `image_gen_service.py` now calls `gpt-image-1` underneath it.
- `image_gen_service.py` — `DallE3ImageService` (name kept for compatibility)
  calls `gpt-image-1` via the openai SDK. gpt-image-1 only returns inline
  base64 (`b64_json`), never a hosted url, so `generate_image()` encodes it as
  a `data:` URI and the shared `download_image()` decodes/writes it directly
  instead of making an HTTP request. Cost is computed from the response's
  `usage` token counts (`GPT_IMAGE_*_COST_PER_TOKEN`), not a flat per-image
  table. `quality`/`size` accept the legacy dall-e-3 vocabulary
  (`"standard"`/`"hd"`, `"1792x1024"`, ...) and are translated to gpt-image-1's
  own (`"medium"`/`"high"`, `"1536x1024"`, ...) before the API call. The
  `dall-e-2` edit path (`edit_image()`, used by `frame_editor.py` /
  `frame_extractor.py`) is unrelated and untouched by this migration.
- `pexels_service.py` — Pexels stock photo/video search via raw HTTP
- `meteo_service.py` — Open-Meteo / DWD ICON forecasts via urllib (`OpenMeteoService`, `MeteoService` Protocol). One multi-location request, never raises: failures degrade to an empty list. `_request()` is the seam patched in tests.
- `notify_service.py` — WhatsApp push for pipeline failures via the local whatsapp-service REST API. Never raises, skipped when disabled or in dry-run.
- `credits_service.py` — live provider balances plus locally tracked spend. OpenAI
  runway uses an operator-recorded balance snapshot and warns when only two
  average transcription episodes remain; recent quota failures force critical.
- `errors.py` — `ErrorCategory` (incl. `PERMANENT_QUOTA` for exhausted provider credits),
  `is_transient()`, `ERROR_SUGGESTIONS`. Provider quota codes take precedence over
  generic HTTP 429 rate-limit classification.
- `ffmpeg_service.py` — ffmpeg subprocess wrapper: `normalize_video_clip()`, `create_video_segment()`, `concat_segments()`, `probe_media()`, `generate_test_video()`, `generate_silent_audio()`
- `youtube_service.py` — target-separated YouTube Data API upload + OAuth.
  Test/production credentials and expected channel IDs are resolved explicitly;
  uploads verify `channels.list(mine=true)` before `videos.insert`. Current
  granular quota estimates are recorded by bucket. OAuth tokens are atomically
  written, and both token/client files are maintained at mode 0600.
- `feed_service.py` — RSS/YouTube feed parsing -> `list[EpisodeInfo]`
- `download_service.py` — yt-dlp audio download
- `transcription_service.py` — provider-neutral OpenAI and local faster-whisper
  transcription, with automatic chunking for large OpenAI inputs. The shared retry
  decorator owns OpenAI retries; SDK retries are disabled to avoid multiplying paid
  or quota-rejected requests.
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
