# E2E Smoke Test Report — Pexels Stock Image Pipeline

**Date:** 2026-03-15
**Episode:** `SJFLLZxlWqk` — "Wir rennen schneller, arbeiten mehr, sparen harder, und kommen trotzdem nicht voran."
**Pipeline version:** 2
**Final verdict:** **PASS**

## Config Flags Used

| Setting | Value |
|---------|-------|
| `pipeline_version` | 2 |
| `image_gen_provider` | pexels |
| `render_resolution` | 1920x1080 |
| `render_fps` | 30 |
| `render_crf` | 23 |
| `render_preset` | medium |
| `dry_run` | False |
| `PEXELS_API_KEY` | (set) |
| `ANTHROPIC_API_KEY` | (set) |
| `OPENAI_API_KEY` | (set, used for ranking LLM calls via gpt-4o) |
| `ELEVENLABS_API_KEY` | (set) |

## Stage-by-Stage Results

| Stage | Status | Duration | Detail |
|-------|--------|----------|--------|
| download | skipped | — | Already completed |
| transcribe | skipped | — | Already completed |
| correct | skipped | — | Already completed |
| review_gate_1 | skipped | — | Already approved (RT#1) |
| translate | skipped | — | Already completed |
| adapt | skipped | — | Already completed |
| review_gate_2 | skipped | — | Already approved (RT#2) |
| chapterize | skipped | — | Already completed |
| **imagegen** | **success** | 39.4s | 14 chapters ranked, 0 skipped, $0.0502 (Pexels search + LLM ranking) |
| **review_gate_stock** | **success** | 0.8s | Stock images approved, 14 finalized, 1 placeholder |
| **tts** | **skipped** | — | Already up-to-date (cached from prior run) |
| **render** | **success** | 2359.5s (~39 min) | 15 segments, 823.4s total duration, 44.2MB draft |
| **review_gate_3** | **success** | 0.0s | Video review approved (pre-existing approval RT#3) |
| publish | failed (expected) | — | Artifact integrity mismatch (new render vs old approval hash) — **not a bug** |

## Auto-Approvals Performed

| Gate | Review Task ID | Stage | Timestamp | Notes |
|------|---------------|-------|-----------|-------|
| review_gate_1 | RT#1 | correct | pre-existing | Auto-approved previously |
| review_gate_2 | RT#2 | adapt | pre-existing | Auto-approved previously |
| **review_gate_stock** | **RT#6** | stock_images | 2026-03-15 04:32 | "Auto-approved during E2E smoke test" |
| review_gate_3 | RT#3 | render | pre-existing | Auto-approved by CLI in prior run |

## Pinned Stock Images Per Chapter

| Chapter | Pexels ID | Photographer | Source URL | Local Path |
|---------|-----------|-------------|------------|------------|
| ch01 | 6152046 | Meruyert Gonullu | https://www.pexels.com/photo/exterior-of-barbershop-with-signboard-6152046/ | images/ch01_selected.jpg (267KB) |
| ch02 | 19840560 | Marta Branco | https://www.pexels.com/photo/golden-and-silver-coins-19840560/ | images/ch02_selected.jpg (281KB) |
| ch03 | 7947635 | RDNE Stock project | https://www.pexels.com/photo/a-graph-in-close-up-photography-7947635/ | images/ch03_selected.jpg (218KB) |
| ch04 | 7947998 | RDNE Stock project | https://www.pexels.com/photo/black-remote-control-on-white-printer-paper-7947998/ | images/ch04_selected.jpg (249KB) |
| ch05 | 7267489 | DS stories | https://www.pexels.com/photo/close-up-photograph-of-gold-bitcoins-7267489/ | images/ch05_selected.jpg (147KB) |
| ch06 | 7947997 | RDNE Stock project | https://www.pexels.com/photo/a-chart-and-a-mobile-phone-7947997/ | images/ch06_selected.jpg (139KB) |
| ch07 | 31330742 | Marta Branco | https://www.pexels.com/photo/economic-struggle-coins-wallet-and-rice-scene-31330742/ | images/ch07_selected.jpg (336KB) |
| ch08 | 9531939 | Timur Weber | https://www.pexels.com/photo/man-in-gray-jacket-and-blue-pants-sitting-on-gray-concrete-bench-9531939/ | images/ch08_selected.jpg (256KB) |
| ch09 | 19840560 | Marta Branco | https://www.pexels.com/photo/golden-and-silver-coins-19840560/ | images/ch09_selected.jpg (281KB) |
| ch10 | 7948099 | RDNE Stock project | https://www.pexels.com/photo/white-printer-paper-with-blue-and-green-color-7948099/ | images/ch10_selected.jpg (69KB) |
| ch11 | 7948099 | RDNE Stock project | https://www.pexels.com/photo/white-printer-paper-with-blue-and-green-color-7948099/ | images/ch11_selected.jpg (69KB) |
| ch12 | 8369649 | RDNE Stock project | https://www.pexels.com/photo/close-up-shot-of-gold-bitcoin-on-wooden-surface-8369649/ | images/ch12_selected.jpg (101KB) |
| ch13 | 7947701 | RDNE Stock project | https://www.pexels.com/photo/silver-framed-eyeglasses-on-white-paper-7947701/ | images/ch13_selected.jpg (136KB) |
| ch14 | 7937717 | Pavel Danilyuk | https://www.pexels.com/photo/a-client-handing-payment-to-a-realtor-7937717/ | images/ch14_selected.jpg (143KB) |
| ch15 | — | — | — | images/ch15_placeholder.png (25KB, title_card) |

## Output Video

| Property | Value |
|----------|-------|
| Path | `data/outputs/SJFLLZxlWqk/render/draft.mp4` |
| Size | 44.2 MB (46,317,652 bytes) |
| Duration | 823.4 seconds (~13:43) |
| Segments | 15 |
| Resolution | 1920x1080 |
| Codec | H.264 (libx264, CRF 23, medium preset) |
| Audio | AAC 192kbps |

## Artifacts Summary

| Artifact | Path | Status |
|----------|------|--------|
| Candidates manifest | `images/candidates/candidates_manifest.json` | OK (14 chapters, schema v2.0) |
| Images manifest | `images/manifest.json` | OK (15 entries, all files exist) |
| Selected images | `images/ch{01-14}_selected.jpg` | OK (14 files, all non-zero) |
| TTS manifest | `tts/manifest.json` | OK (15 segments) |
| TTS audio | `tts/ch{01-15}.mp3` | OK (15 files, all non-zero, 12.6MB total) |
| Render manifest | `render/render_manifest.json` | OK (15 segments) |
| Draft video | `render/draft.mp4` | OK (44.2MB, non-zero) |
| Provenance: correct | `provenance/correct_provenance.json` | OK |
| Provenance: translate | `provenance/translate_provenance.json` | OK |
| Provenance: adapt | `provenance/adapt_provenance.json` | OK |
| Provenance: chapterize | `provenance/chapterize_provenance.json` | OK |
| Provenance: imagegen | `provenance/imagegen_provenance.json` | OK |
| Provenance: tts | `provenance/tts_provenance.json` | OK |
| Provenance: render | `provenance/render_provenance.json` | OK |

## Fixes Applied

### Fix 1: TTS skipped-but-not-advancing status
- **File:** `btcedu/core/tts.py`
- **Problem:** When TTS returns `skipped=True` (idempotent, already up-to-date), it did not advance the episode status from `IMAGES_GENERATED` to `TTS_DONE`. The pipeline's stage loop then skipped render as "not ready" because `current_order < required_order`.
- **Fix:** Added status advancement in the `skipped` branch: `if episode.status == EpisodeStatus.IMAGES_GENERATED: episode.status = EpisodeStatus.TTS_DONE`
- **Test:** `test_tts_skipped_advances_status` (1 new test)

### Fix 2: Renderer skipped-but-not-advancing status (preventive)
- **File:** `btcedu/core/renderer.py`
- **Problem:** Same pattern as TTS — if render is skipped (idempotent), episode status would not advance from `TTS_DONE` to `RENDERED`, blocking review_gate_3.
- **Fix:** Added status advancement in the `skipped` branch: `if episode.status == EpisodeStatus.TTS_DONE: episode.status = EpisodeStatus.RENDERED`

### Fix 3: IMAGE_GEN_PROVIDER .env change
- **File:** `.env`
- **Change:** `IMAGE_GEN_PROVIDER=dalle3` → `IMAGE_GEN_PROVIDER=pexels`
- **Reason:** Required to activate Pexels stock image pipeline instead of DALL-E

## Reproduction Steps

```bash
# 1. Ensure .env has IMAGE_GEN_PROVIDER=pexels and all API keys set
# 2. Reset episode to CHAPTERIZED (if already past imagegen)
python -c "
from btcedu.db import get_session_factory
from btcedu.models.episode import Episode, EpisodeStatus
session = get_session_factory()()
ep = session.query(Episode).filter(Episode.episode_id == 'SJFLLZxlWqk').first()
ep.status = EpisodeStatus.CHAPTERIZED
session.commit()
"

# 3. Run pipeline (will stop at review_gate_stock)
btcedu run --episode-id SJFLLZxlWqk

# 4. Approve stock image review
btcedu review list --status pending
btcedu review approve <REVIEW_ID> --notes "Auto-approved during E2E smoke test"

# 5. Continue pipeline (TTS → render → review_gate_3)
btcedu run --episode-id SJFLLZxlWqk

# 6. Verify output
ls -la data/outputs/SJFLLZxlWqk/render/draft.mp4
```

## Test Results

- **Full suite:** 706 passed, 5 failed (all pre-existing)
- **New test:** `test_tts_skipped_advances_status` — validates status advancement fix
- **Ruff lint:** 0 errors in all modified files

## Cost Breakdown

| Stage | Cost |
|-------|------|
| Pexels search | $0.00 (free API) |
| LLM ranking (14 calls, gpt-4o) | $0.0502 |
| TTS (cached) | $0.00 |
| Render (local ffmpeg) | $0.00 |
| **Total E2E run** | **$0.0502** |

## Follow-up Items

1. **Publish artifact integrity:** The publisher's safety check computes a hash of the render artifacts. After re-rendering with new images, the hash no longer matches the previously approved review. A fresh review_gate_3 approval with the new hash would be needed before publishing.
2. **ch15 placeholder:** Chapter 15 uses a `title_card` visual type which doesn't need a stock image. The placeholder is a simple colored PNG, which is correct behavior.
3. **Duplicate images:** ch09 and ch02 use the same Pexels photo (19840560), as do ch10 and ch11 (7948099). The LLM ranking independently selected these as best matches. A future dedup pass could improve variety.
