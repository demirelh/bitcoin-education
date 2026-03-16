# ADR-002: Video Clips Are Opt-In

**Date:** 2026-03 (Phase 4)
**Status:** Accepted

## Context

After adopting stock photos (ADR-001), we added support for Pexels video clips as B-roll for chapters with `visual.type == "b_roll"`. Video clips require normalization (resolution, codec, fps) and are significantly slower to process on the Raspberry Pi.

## Decision

Video B-roll is opt-in, not default. Photos remain the default asset type for all chapters. Videos are only used when:
1. A video candidate is explicitly available from Pexels search
2. The candidate is marked as `asset_type: "video"` in the candidates manifest
3. The human reviewer selects/approves the video candidate

## Rationale

- **Pi performance**: video normalization takes 5-15 minutes per clip on ARM64 software encoding
- **Reliability**: photo-only pipeline is fast and reliable; video adds failure risk (ffmpeg timeout, codec issues)
- **Graceful degradation**: if video normalization fails, the system falls back to a placeholder photo

## Consequences

- `candidates_manifest.json` includes `asset_type` field (photo or video)
- `finalize_selections()` handles both asset types with video normalization pipeline
- Normalization failure creates placeholder (not a crash)
- `RENDER_TIMEOUT_SEGMENT` and `RENDER_PRESET` in `.env` are critical for Pi deployments
