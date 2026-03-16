# Visual Asset Flow (Photo & Video)

## Overview

After chapterization, each chapter needs a visual asset (photo or video). The pipeline searches for candidates, uses LLM-based ranking to select the best match, allows human review, then finalizes and normalizes the selections.

The current implementation uses Pexels as the image/video provider, but the flow is provider-agnostic — search, rank, review, finalize.

## Flow

```
chapterize (chapters.json)
  -> search for candidates per chapter (currently via Pexels API)
  -> rank_candidates() — LLM intent extraction + smart ranking
  -> [review gate] — human reviews selections in web dashboard
  -> finalize_selections() — download, normalize videos, write manifest
```

## Search phase

`search_stock_images()` in `stock_images.py`:
1. Load chapters from `chapters.json`
2. For each chapter with DIAGRAM or B_ROLL visual type, search for matching images/videos
3. Download candidate thumbnails/previews
4. Write `candidates_manifest.json` with all candidates per chapter

## Ranking phase

`rank_candidates()` in `stock_images.py`:
1. `extract_chapter_intents()` — LLM extracts semantic intent from chapter content
2. Registers prompt template via `PromptRegistry` for version tracking
3. Ranks candidates by relevance to extracted intents
4. Marks best candidate as `selected: true` in manifest

## Asset types

| Type | Description | Normalization |
|------|------------|--------------|
| photo | JPEG/PNG image | None (used as-is) |
| video | MP4 video clip | `normalize_video_clip()` — resolution, fps, codec, yuv420p |
| placeholder | Template fallback | Used when no candidate selected or normalization fails |

## Video normalization

Videos are normalized via `ffmpeg_service.normalize_video_clip()`:
- Scale + pad to target resolution (default 1920x1080)
- Convert to H.264 + yuv420p
- If normalization fails: graceful fallback to placeholder

## Finalization

`finalize_selections()`:
1. Read approved selections from candidates_manifest
2. For videos: run normalization pipeline
3. Write final `manifest.json` with selected assets
4. Set episode status to IMAGES_GENERATED

## Key files

- `btcedu/core/stock_images.py` — orchestration
- `btcedu/services/pexels_service.py` — current provider (Pexels API)
- `btcedu/services/ffmpeg_service.py` — video normalization
- `btcedu/prompts/templates/intent_extract.md` — intent extraction prompt
- `btcedu/prompts/templates/stock_rank.md` — ranking prompt
