# Stock Asset Flow (Photo & Video)

## Overview

After chapterization, each chapter needs a visual asset. The stock asset pipeline searches Pexels, uses LLM-based ranking, allows human review, then finalizes selections.

## Flow

```
chapterize (chapters.json)
  -> search_stock_images() — Pexels API search per chapter
  -> rank_candidates() — LLM intent extraction + smart ranking
  -> [review_gate_stock] — human reviews selections in web dashboard
  -> finalize_selections() — download, normalize videos, write manifest
```

## Search phase

`search_stock_images()` in `stock_images.py`:
1. Load chapters from `chapters.json`
2. For each chapter with DIAGRAM or B_ROLL visual type, search Pexels
3. Downloads candidate thumbnails/previews
4. Writes `candidates_manifest.json` with all candidates per chapter

## Ranking phase

`rank_candidates()` in `stock_images.py`:
1. `extract_chapter_intents()` — LLM extracts semantic intent from chapter content
2. Registers prompt template via `PromptRegistry` for version tracking
3. Ranks candidates by relevance to extracted intents
4. Marks best candidate as `selected: true` in manifest

## Asset types

| Type | Source | Normalization |
|------|--------|--------------|
| photo | Pexels JPEG | None (used as-is) |
| video | Pexels MP4 | `normalize_video_clip()` — resolution, fps, codec, yuv420p |
| placeholder | Generated | Template-based fallback when no candidate works |

## Video normalization

Videos are normalized via `ffmpeg_service.normalize_video_clip()`:
- Scale + pad to target resolution (default 1920x1080)
- Convert to H.264 + yuv420p
- If normalization fails: graceful fallback to placeholder (photo asset_type)

## Finalization

`finalize_selections()`:
1. Read approved selections from candidates_manifest
2. For videos: run normalization pipeline
3. Write final `manifest.json` with selected assets
4. Set episode status to IMAGES_GENERATED

## Key files

- `btcedu/core/stock_images.py` — orchestration (60KB)
- `btcedu/services/pexels_service.py` — Pexels API wrapper
- `btcedu/services/ffmpeg_service.py` — video normalization
- `btcedu/prompts/templates/intent_extract.md` — intent extraction prompt
- `btcedu/prompts/templates/stock_rank.md` — ranking prompt
