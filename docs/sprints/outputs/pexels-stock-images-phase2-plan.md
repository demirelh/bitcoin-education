# Phase 2: Stock Image Pinning, LLM Ranking & Review Integration

**Status**: Plan
**Depends on**: Phase 1 (Pexels search + candidate download + auto-select, merged)
**Goal**: Replace AI image generation entirely. The production v2 pipeline must never call OpenAI image generation. Stock images become mandatory for the v2 flow.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Data Model: Pinning Persistence](#2-data-model-pinning-persistence)
3. [LLM-Based Candidate Ranking](#3-llm-based-candidate-ranking)
4. [Review Task Integration](#4-review-task-integration)
5. [Dashboard UI](#5-dashboard-ui)
6. [API Endpoints](#6-api-endpoints)
7. [Pipeline Integration](#7-pipeline-integration)
8. [CLI Commands](#8-cli-commands)
9. [Test Plan](#9-test-plan)
10. [Implementation Order & DoD](#10-implementation-order--dod)

---

## 1. Architecture Overview

### End-to-End Flow After Phase 2

```
CHAPTERIZED
  ↓
[imagegen stage — stock images ONLY]
  ├── 1. search_stock_images()        ← Phase 1 (exists)
  ├── 2. rank_candidates()            ← Phase 2 NEW: LLM ranks candidates per chapter
  ├── 3. → Pipeline PAUSES →         ← Phase 2 NEW: ReviewTask stage="stock_images" created
  │       Human reviews in dashboard
  │       Pins/swaps images per chapter
  │       Approves ReviewTask
  ├── 4. finalize_selections()        ← Phase 1 (exists), called after approval
  ↓
IMAGES_GENERATED
  ↓
[tts, render, review_gate_3, publish — unchanged]
```

### What Changes From Phase 1

| Component | Phase 1 (current) | Phase 2 (target) |
|-----------|-------------------|-------------------|
| Selection method | `auto_select_best()` picks first candidate | LLM ranks, human pins final choice |
| Pipeline flow | imagegen = search + auto-select + finalize (no pause) | imagegen = search + rank → PAUSE → human pins → approve → finalize |
| Review gate | None | New `review_gate_stock` after imagegen, before TTS |
| AI image gen | Still available via `image_gen_provider=dalle3` | **Removed from v2 pipeline entirely** |
| Dashboard | No stock image UI | Candidate browser + pin/swap + approve |

### Files Modified

| File | Change |
|------|--------|
| `btcedu/core/stock_images.py` | Add `rank_candidates()`, remove `auto_select_best()` from pipeline path |
| `btcedu/core/pipeline.py` | Rewrite imagegen stage, add `review_gate_stock`, remove DALL-E branch |
| `btcedu/core/reviewer.py` | Add `"stock_images"` to `_REVERT_MAP` |
| `btcedu/web/api.py` | Add 4 stock image endpoints |
| `btcedu/web/static/app.js` | Add Stock Images tab/panel |
| `btcedu/web/static/styles.css` | Styles for candidate grid |
| `btcedu/cli.py` | Add `stock rank` command, update `stock auto-select` |
| `btcedu/config.py` | Add `stock_image_ranking_enabled` setting |

### Files Created

| File | Purpose |
|------|---------|
| `btcedu/prompts/templates/stock_rank.md` | LLM ranking prompt template |

### Files NOT Modified (downstream compatibility)

- `btcedu/core/tts.py` — reads `images/manifest.json`, format unchanged
- `btcedu/core/renderer.py` — reads `images/manifest.json`, format unchanged
- `btcedu/models/media_asset.py` — already used by Phase 1
- `btcedu/services/pexels_service.py` — Phase 1 service, unchanged

---

## 2. Data Model: Pinning Persistence

### Decision: JSON-file persistence (Option B)

Pinning state lives in `candidates_manifest.json`, not in the database. Rationale:
- Phase 1 already stores `selected` and `locked` flags per candidate in this file
- No schema migration needed
- File-level idempotency pattern consistent with other stages (chapters.json, tts/manifest.json)
- ReviewTask in DB provides the blocking gate; pinning is just selection state

### candidates_manifest.json — Phase 2 Extensions

Each candidate entry gains a `rank` field (integer, 1 = best). Each chapter entry gains a `pinned_by` field.

```json
{
  "episode_id": "SJFLLZxlWqk",
  "schema_version": "2.0",
  "searched_at": "2026-03-15T10:00:00+00:00",
  "ranked_at": "2026-03-15T10:01:00+00:00",
  "ranking_model": "claude-sonnet-4-20250514",
  "ranking_cost_usd": 0.012,
  "chapters_hash": "abc123...",
  "chapters": {
    "ch_01": {
      "search_query": "bitcoin mining hardware finance",
      "candidates": [
        {
          "pexels_id": 12345,
          "photographer": "John Doe",
          "photographer_url": "https://pexels.com/@johndoe",
          "source_url": "https://pexels.com/photo/12345",
          "download_url": "https://images.pexels.com/...",
          "local_path": "images/candidates/ch_01/pexels_12345.jpg",
          "alt_text": "Bitcoin mining farm",
          "width": 1880,
          "height": 1253,
          "size_bytes": 245000,
          "downloaded_at": "2026-03-15T10:00:00+00:00",
          "selected": true,
          "locked": true,
          "rank": 1,
          "rank_reason": "Directly shows mining hardware, matches chapter topic"
        },
        {
          "pexels_id": 67890,
          "...": "...",
          "selected": false,
          "locked": false,
          "rank": 2,
          "rank_reason": "Related but shows general server room"
        }
      ],
      "pinned_by": "llm_rank"
    }
  }
}
```

### Field Semantics

| Field | Type | Set By | Meaning |
|-------|------|--------|---------|
| `rank` | int \| null | `rank_candidates()` | 1 = best match. null = unranked |
| `rank_reason` | str \| null | `rank_candidates()` | One-line LLM justification |
| `selected` | bool | `rank_candidates()` or human pin | This candidate is the current pick |
| `locked` | bool | Human pin (via API/CLI) | Prevents re-ranking from overwriting |
| `pinned_by` | str \| null | System | `"llm_rank"`, `"human"`, or null |
| `ranked_at` | str (ISO) | `rank_candidates()` | Timestamp of last ranking run |
| `ranking_model` | str | `rank_candidates()` | Model used for ranking |
| `ranking_cost_usd` | float | `rank_candidates()` | Total LLM cost for ranking |
| `schema_version` | str | Always | Bumped to `"2.0"` when ranking fields present |

---

## 3. LLM-Based Candidate Ranking

### 3.1 Prompt Template: `btcedu/prompts/templates/stock_rank.md`

```yaml
---
name: stock_rank
version: 1
model: claude-sonnet-4-20250514
temperature: 0.1
max_tokens: 4096
description: Rank stock photo candidates for a video chapter
author: btcedu
---
```

**System section:**
```
You are an editorial assistant selecting the best stock photo for a YouTube video chapter.
The video covers Bitcoin and cryptocurrency education, targeting a Turkish audience.
```

**Instructions section (Jinja2):**
```
## Chapter Context
- **Title**: {{ chapter_title }}
- **Visual type**: {{ visual_type }}
- **Visual description**: {{ visual_description }}
- **Narration excerpt** (first 200 chars): {{ narration_excerpt }}
- **Search query used**: {{ search_query }}

## Candidates
{% for c in candidates %}
### Candidate {{ loop.index }}
- **Pexels ID**: {{ c.pexels_id }}
- **Alt text**: {{ c.alt_text }}
- **Dimensions**: {{ c.width }}x{{ c.height }}
- **Photographer**: {{ c.photographer }}
{% endfor %}

## Task
Rank ALL candidates from best (1) to worst. For each, provide a one-line reason.

Ranking criteria (in priority order):
1. **Relevance**: How well does the image match the visual description and chapter topic?
2. **Composition**: Is it landscape-oriented, uncluttered, suitable as a video background?
3. **Professionalism**: Does it look like educational/financial content, not casual/amateur?
4. **Text overlay compatibility**: Will subtitle text be readable over this image?

## Output Format (JSON)
```json
{
  "rankings": [
    {"pexels_id": 12345, "rank": 1, "reason": "..."},
    {"pexels_id": 67890, "rank": 2, "reason": "..."}
  ]
}
```
```

### 3.2 Function: `rank_candidates()` in `btcedu/core/stock_images.py`

```python
def rank_candidates(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> RankResult:
    """Use LLM to rank stock photo candidates per chapter.

    Reads candidates_manifest.json, calls LLM for each chapter with candidates,
    writes rank + rank_reason back to manifest, selects rank=1 as default pick.

    Skips chapters with locked selections unless force=True.
    """
```

**Signature of result dataclass:**
```python
@dataclass
class RankResult:
    episode_id: str
    chapters_ranked: int
    chapters_skipped: int  # locked or no candidates
    total_cost_usd: float
```

**Algorithm:**

1. Load `candidates_manifest.json`
2. Load `chapters.json` for chapter context
3. For each chapter with candidates:
   a. Skip if locked and not force
   b. Skip if 0 or 1 candidate (auto-rank the single one as rank=1)
   c. Render `stock_rank.md` template with chapter context + candidate metadata
   d. Call `call_claude(system_prompt, user_message, settings, json_mode=True)`
   e. Parse response JSON: `{"rankings": [{"pexels_id": int, "rank": int, "reason": str}]}`
   f. Write `rank` and `rank_reason` to each candidate in manifest
   g. Set `selected=True` on rank=1 candidate, `selected=False` on others
   h. Set `pinned_by="llm_rank"` on chapter
   i. Accumulate cost
4. Update manifest: set `ranked_at`, `ranking_model`, `ranking_cost_usd`, bump `schema_version` to `"2.0"`
5. Write manifest back
6. Record PipelineRun (stage=imagegen, cost)
7. Return RankResult

**Safety constraints:**
- Max 1 LLM call per chapter (no retry loops for ranking — if parse fails, fall back to rank by candidate order)
- `json_mode=True` for structured output
- If LLM returns pexels_ids not in candidates, ignore them and fall back to order-based ranking
- Cost guard: ranking is part of imagegen stage cost, checked against `max_episode_cost_usd`
- Dry-run support: if `settings.dry_run`, assign ranks 1..N by candidate order without LLM call

### 3.3 PromptRegistry Integration

`rank_candidates()` loads the template via `PromptRegistry`, same as other stages:

```python
from btcedu.core.prompt_registry import PromptRegistry

registry = PromptRegistry(session)
prompt_version = registry.load("stock_rank")
system_prompt = prompt_version.rendered_system  # from system.md
user_message = prompt_version.render(context_vars)
```

If `PromptRegistry` is not used by Phase 1 image code, follow the pattern from `chapterizer.py` or `adapter.py` for template loading.

---

## 4. Review Task Integration

### 4.1 New Review Stage: `"stock_images"`

The review gate sits between ranking and finalization. The pipeline creates a `ReviewTask` with `stage="stock_images"` after ranking completes.

### 4.2 Pipeline Stage: `review_gate_stock`

Inserted into `_V2_STAGES` between `imagegen` and `tts`:

```python
# In _V2_STAGES (pipeline.py):
("imagegen",          EpisodeStatus.CHAPTERIZED),     # search + rank (no finalize)
("review_gate_stock", EpisodeStatus.CHAPTERIZED),     # NEW: blocks until human approves
("tts",               EpisodeStatus.IMAGES_GENERATED), # unchanged
```

**Important**: The `imagegen` stage no longer sets `IMAGES_GENERATED` status. It leaves the episode at `CHAPTERIZED`. The `review_gate_stock` stage:
1. Checks `has_approved_review(session, episode_id, "stock_images")`
2. If approved: calls `finalize_selections()` → sets `IMAGES_GENERATED` → returns success
3. If pending: returns `"review_pending"`
4. If no task exists: creates `ReviewTask(stage="stock_images", ...)` → returns `"review_pending"`

### 4.3 ReviewTask Artifacts

```python
create_review_task(
    session,
    episode.episode_id,
    stage="stock_images",
    artifact_paths=[
        str(candidates_manifest_path),  # candidates + rankings
        str(chapters_json_path),        # chapter context
    ],
    diff_path=None,  # no diff for image selection
)
```

### 4.4 Reviewer Changes

In `btcedu/core/reviewer.py`, add to `_REVERT_MAP`:

```python
_REVERT_MAP = {
    EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,   # RG1
    EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,      # RG2
    # No revert for stock_images — rejection keeps CHAPTERIZED status,
    # user re-pins and re-approves. No stage re-run needed.
}
```

On rejection of `stage="stock_images"`: the episode stays at `CHAPTERIZED`. The user re-pins images in the dashboard and creates a new approval. No automatic revert needed because the candidates are already downloaded.

### 4.5 Approval Flow

1. Pipeline runs `imagegen` → search + rank → episode stays `CHAPTERIZED`
2. Pipeline runs `review_gate_stock` → creates ReviewTask → returns `"review_pending"`
3. Pipeline pauses (standard review gate behavior)
4. Human opens dashboard → Stock Images tab → browses ranked candidates → pins final choices → clicks "Approve"
5. API: `POST /api/reviews/{id}/approve` (existing endpoint)
6. Next pipeline run: `review_gate_stock` sees approval → `finalize_selections()` → `IMAGES_GENERATED`
7. Pipeline continues to TTS, render, etc.

---

## 5. Dashboard UI

### 5.1 Stock Images Tab

Add a "Stock Images" tab to the episode detail view in `app.js`, visible when episode status is `CHAPTERIZED` or later and a `candidates_manifest.json` exists.

### 5.2 UI Layout

```
┌─────────────────────────────────────────────────────┐
│ Stock Images — Episode: SJFLLZxlWqk                 │
│                                                      │
│ Chapter 1: Bitcoin Madenciliği                       │
│ Visual: B_ROLL — "Mining farm with rows of ASICs"   │
│ Search query: "bitcoin mining hardware finance"      │
│                                                      │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│ │ [img 1]  │ │ [img 2]  │ │ [img 3]  │  ...        │
│ │ ★ Rank 1 │ │   Rank 2 │ │   Rank 3 │              │
│ │ PINNED   │ │          │ │          │              │
│ │ [Pin]    │ │ [Pin]    │ │ [Pin]    │              │
│ └──────────┘ └──────────┘ └──────────┘              │
│ Photographer: John Doe | Pexels License             │
│ Reason: "Directly shows mining hardware..."         │
│                                                      │
│ Chapter 2: Blockchain Teknolojisi                    │
│ ...                                                  │
│                                                      │
│ [Approve All Selections]  [Re-rank with LLM]        │
└─────────────────────────────────────────────────────┘
```

### 5.3 UI Components

**Chapter section** (one per chapter needing images):
- Chapter title + visual type badge + visual description
- Search query shown as muted text
- Horizontal scrollable row of candidate thumbnails (max 200px wide)
- Each thumbnail shows: rank badge (top-left), "PINNED" label if selected+locked, photographer credit
- Click thumbnail → shows larger preview + alt text + rank reason
- "Pin" button on each thumbnail → calls `POST /api/episodes/{id}/stock/pin`

**Action bar** (bottom of tab):
- "Approve All Selections" button → calls `POST /api/reviews/{review_id}/approve`
  - Disabled if any chapter has 0 selections
  - Shows confirmation dialog: "Approve N images for N chapters?"
- "Re-rank with LLM" button → calls `POST /api/episodes/{id}/stock/rank`
  - Disabled if all chapters are locked

### 5.4 Implementation in `app.js`

Add these functions following existing patterns (reference: `showReviews`, `loadReviewList`):

```javascript
// New functions:
async function loadStockImages(episodeId) { ... }
function renderStockImagesTab(data) { ... }
async function pinStockImage(episodeId, chapterId, pexelsId) { ... }
async function rerankStock(episodeId) { ... }
```

Thumbnail images served via existing file endpoint: `GET /api/episodes/{id}/files/stock_candidate?chapter={ch_id}&filename={filename}`

### 5.5 Styles in `styles.css`

Minimal additions:
- `.stock-chapter` — chapter section container
- `.stock-grid` — horizontal flex row of thumbnails
- `.stock-thumb` — thumbnail card (200px max-width, border, hover shadow)
- `.stock-thumb.pinned` — green border for pinned image
- `.stock-rank-badge` — rank number overlay (top-left)
- `.stock-pin-btn` — pin button

---

## 6. API Endpoints

All endpoints added to `btcedu/web/api.py` under the `api_bp` blueprint.

### 6.1 GET `/api/episodes/<id>/stock/candidates`

Returns the full candidates manifest for the episode.

**Response** (200):
```json
{
  "episode_id": "SJFLLZxlWqk",
  "schema_version": "2.0",
  "ranked_at": "...",
  "chapters": { ... },
  "review_task_id": 42,
  "review_status": "pending"
}
```

**Response** (404): `{"error": "No candidates manifest found"}`

**Implementation**: Read `candidates_manifest.json`, look up ReviewTask for `stage="stock_images"`, merge review info.

### 6.2 POST `/api/episodes/<id>/stock/pin`

Pin a specific candidate for a chapter.

**Request body**:
```json
{
  "chapter_id": "ch_01",
  "pexels_id": 12345,
  "lock": true
}
```

**Response** (200): `{"status": "pinned", "chapter_id": "ch_01", "pexels_id": 12345}`

**Implementation**: Calls `select_stock_image()` (Phase 1 function) with `lock=True`. Updates `pinned_by` to `"human"` in manifest.

### 6.3 POST `/api/episodes/<id>/stock/rank`

Trigger LLM re-ranking for all unlocked chapters.

**Response** (200):
```json
{
  "status": "ranked",
  "chapters_ranked": 12,
  "chapters_skipped": 3,
  "cost_usd": 0.012
}
```

**Implementation**: Calls `rank_candidates()`. Returns result.

### 6.4 GET `/api/episodes/<id>/files/stock_candidate`

Serve a candidate image file. Query params: `chapter` (chapter_id), `filename` (e.g., `pexels_12345.jpg`).

**Response**: Raw image file with `Content-Type: image/jpeg`.

**Implementation**: Resolve path as `data/outputs/{ep_id}/images/candidates/{chapter}/{filename}`. Validate path is within outputs dir (path traversal guard). Use `send_file()`.

---

## 7. Pipeline Integration

### 7.1 Remove AI Image Generation from v2

**In `btcedu/core/pipeline.py`**, the `imagegen` stage handler is rewritten:

```python
elif stage_name == "imagegen":
    from btcedu.core.stock_images import rank_candidates, search_stock_images

    # Step 1: Search Pexels for candidates
    search_stock_images(session, episode.episode_id, settings, force=force)

    # Step 2: LLM-rank candidates
    rank_result = rank_candidates(session, episode.episode_id, settings, force=force)

    elapsed = time.monotonic() - t0
    return StageResult(
        "imagegen",
        "success",
        elapsed,
        detail=(
            f"{rank_result.chapters_ranked} chapters ranked, "
            f"{rank_result.chapters_skipped} skipped, "
            f"${rank_result.total_cost_usd:.4f}"
        ),
    )
```

The DALL-E branch (`else: from btcedu.core.image_generator import generate_images`) is **deleted entirely**. The `image_gen_provider` config field is no longer checked in the pipeline. (The field itself remains in config for backward compatibility but is unused by v2.)

**Note**: `auto_select_best()` is NOT called in the pipeline anymore. It remains available as a CLI convenience command (`btcedu stock auto-select`) for testing/development, but the production pipeline uses `rank_candidates()` + human pinning.

### 7.2 New Stage in `_V2_STAGES`

```python
_V2_STAGES = [
    ("download",          EpisodeStatus.NEW),
    ("transcribe",        EpisodeStatus.DOWNLOADED),
    ("correct",           EpisodeStatus.TRANSCRIBED),
    ("review_gate_1",     EpisodeStatus.CORRECTED),
    ("translate",         EpisodeStatus.CORRECTED),
    ("adapt",             EpisodeStatus.TRANSLATED),
    ("review_gate_2",     EpisodeStatus.ADAPTED),
    ("chapterize",        EpisodeStatus.ADAPTED),
    ("imagegen",          EpisodeStatus.CHAPTERIZED),       # search + rank (no finalize)
    ("review_gate_stock", EpisodeStatus.CHAPTERIZED),       # NEW
    ("tts",               EpisodeStatus.IMAGES_GENERATED),
    ("render",            EpisodeStatus.TTS_DONE),
    ("review_gate_3",     EpisodeStatus.RENDERED),
    ("publish",           EpisodeStatus.APPROVED),
]
```

### 7.3 `review_gate_stock` Stage Handler

```python
elif stage_name == "review_gate_stock":
    from btcedu.core.reviewer import (
        create_review_task,
        has_approved_review,
        has_pending_review,
    )
    from btcedu.core.stock_images import finalize_selections

    # Check if already approved
    if has_approved_review(session, episode.episode_id, "stock_images"):
        # Finalize selections into images/manifest.json
        select_result = finalize_selections(session, episode.episode_id, settings)
        elapsed = time.monotonic() - t0
        return StageResult(
            "review_gate_stock",
            "success",
            elapsed,
            detail=(
                f"stock images approved, {select_result.selected_count} finalized, "
                f"{select_result.placeholder_count} placeholders"
            ),
        )

    # Check if pending
    if has_pending_review(session, episode.episode_id):
        elapsed = time.monotonic() - t0
        return StageResult(
            "review_gate_stock",
            "review_pending",
            elapsed,
            detail="awaiting stock image review",
        )

    # Create review task
    candidates_manifest_path = (
        Path(settings.outputs_dir) / episode.episode_id
        / "images" / "candidates" / "candidates_manifest.json"
    )
    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"

    create_review_task(
        session,
        episode.episode_id,
        stage="stock_images",
        artifact_paths=[str(candidates_manifest_path), str(chapters_path)],
        diff_path=None,
    )
    elapsed = time.monotonic() - t0
    return StageResult(
        "review_gate_stock",
        "review_pending",
        elapsed,
        detail="stock image review task created",
    )
```

### 7.4 Episode Status Flow

The `imagegen` stage **no longer** sets `IMAGES_GENERATED`. It leaves the episode at `CHAPTERIZED` (the status it was already at). The `review_gate_stock` handler calls `finalize_selections()` on approval, which sets `IMAGES_GENERATED`.

This means:
- `run_pending()` picks up `CHAPTERIZED` episodes and runs `imagegen` + `review_gate_stock`
- `review_gate_stock` pauses with `"review_pending"`
- After human approval, next `run_pending()` call sees `CHAPTERIZED` + approved review → `finalize_selections()` → `IMAGES_GENERATED`
- Pipeline continues to TTS

### 7.5 `_STATUS_ORDER` — No Changes Needed

`CHAPTERIZED` (13) and `IMAGES_GENERATED` (14) are already in `_STATUS_ORDER`. The new `review_gate_stock` uses `CHAPTERIZED` as its required status, which is already defined.

### 7.6 v1 Pipeline — Unchanged

v1 does not use imagegen. No changes to `_V1_STAGES`.

### 7.7 Backward Compatibility for `image_gen_provider`

The `image_gen_provider` config field (`"dalle3"` or `"pexels"`) is no longer checked in the pipeline. However:
- The field remains in `Settings` to avoid breaking `.env` files
- `image_gen_service.py` and `image_generator.py` are NOT deleted (they still work for v1 or manual use)
- A deprecation warning is logged if `image_gen_provider == "dalle3"` and `pipeline_version >= 2`

### 7.8 Pexels API Key Validation

The pipeline must fail early if `pexels_api_key` is empty and `pipeline_version >= 2`. Add a check at the start of `run_episode_pipeline()`:

```python
if settings.pipeline_version >= 2 and not settings.pexels_api_key:
    raise ValueError(
        "PEXELS_API_KEY is required for v2 pipeline. "
        "Set it in .env or environment."
    )
```

---

## 8. CLI Commands

### 8.1 New: `btcedu stock rank`

```
btcedu stock rank --episode-id EP [--force] [--dry-run]
```

Runs `rank_candidates()` for the given episode. Prints ranked results per chapter.

### 8.2 Updated: `btcedu stock auto-select`

Remains available but prints a warning:
```
⚠ auto-select is for development/testing only.
  The production pipeline uses LLM ranking + human pinning.
  Use 'btcedu stock rank' instead.
```

### 8.3 Updated: `btcedu stock search`

No changes needed. Works as-is.

### 8.4 Updated: `btcedu stock select`

No changes needed. The `--lock` flag already exists. In Phase 2 context, this is the CLI equivalent of the dashboard "Pin" button.

### 8.5 Updated: `btcedu stock list`

Add rank and rank_reason to the output table:

```
Chapter   Rank  Selected  Locked  Pexels ID  Alt Text
ch_01     1     ✓         ✓       12345      Bitcoin mining farm
ch_01     2                       67890      Server room
ch_02     1     ✓                 11111      Blockchain diagram
```

---

## 9. Test Plan

### 9.1 Unit Tests: LLM Ranking (`tests/test_stock_ranking.py`) — ~18 tests

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_rank_candidates_calls_llm_per_chapter` | One LLM call per chapter with candidates |
| 2 | `test_rank_candidates_skips_locked` | Locked chapters not re-ranked |
| 3 | `test_rank_candidates_force_overrides_locked` | force=True re-ranks locked chapters |
| 4 | `test_rank_candidates_single_candidate_no_llm` | Single candidate auto-ranked as 1, no LLM call |
| 5 | `test_rank_candidates_no_candidates_skipped` | Chapters with 0 candidates are skipped |
| 6 | `test_rank_writes_rank_fields` | `rank` and `rank_reason` written to manifest |
| 7 | `test_rank_selects_top_ranked` | `selected=True` on rank=1 candidate |
| 8 | `test_rank_sets_pinned_by_llm` | `pinned_by="llm_rank"` on chapter |
| 9 | `test_rank_updates_manifest_metadata` | `ranked_at`, `ranking_model`, `ranking_cost_usd` set |
| 10 | `test_rank_bumps_schema_version` | `schema_version` set to `"2.0"` |
| 11 | `test_rank_invalid_llm_response_fallback` | Malformed JSON → fall back to order-based ranking |
| 12 | `test_rank_unknown_pexels_id_ignored` | LLM returns unknown ID → ignored, others still ranked |
| 13 | `test_rank_cost_accumulated` | Total cost = sum of per-chapter LLM costs |
| 14 | `test_rank_dry_run_no_llm` | dry_run=True → no LLM call, ranks by order |
| 15 | `test_rank_creates_pipeline_run` | PipelineRun record created with cost |
| 16 | `test_rank_result_dataclass` | RankResult fields populated correctly |
| 17 | `test_rank_prompt_template_rendered` | Template includes chapter context and candidate info |
| 18 | `test_rank_respects_cost_guard` | Stops ranking if cumulative cost exceeds max_episode_cost_usd |

### 9.2 Unit Tests: Review Gate (`tests/test_stock_review_gate.py`) — ~10 tests

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_review_gate_stock_creates_task` | ReviewTask created with stage="stock_images" |
| 2 | `test_review_gate_stock_pending` | Returns "review_pending" when task exists |
| 3 | `test_review_gate_stock_approved_finalizes` | Calls finalize_selections() on approval |
| 4 | `test_review_gate_stock_sets_images_generated` | Episode status = IMAGES_GENERATED after approval |
| 5 | `test_review_gate_stock_artifact_paths` | ReviewTask has candidates_manifest + chapters.json paths |
| 6 | `test_review_gate_stock_rejection_keeps_status` | Rejection keeps episode at CHAPTERIZED |
| 7 | `test_pipeline_pauses_at_stock_review` | Full pipeline run pauses at review_gate_stock |
| 8 | `test_pipeline_resumes_after_stock_approval` | After approval, pipeline continues to TTS |
| 9 | `test_v2_stages_includes_review_gate_stock` | _V2_STAGES contains review_gate_stock entry |
| 10 | `test_pexels_key_required_for_v2` | ValueError if pexels_api_key empty + v2 |

### 9.3 Unit Tests: API Endpoints (`tests/test_stock_api.py`) — ~10 tests

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_get_candidates_returns_manifest` | GET /stock/candidates returns full manifest |
| 2 | `test_get_candidates_includes_review_info` | Response includes review_task_id and review_status |
| 3 | `test_get_candidates_404_no_manifest` | 404 when no candidates_manifest.json |
| 4 | `test_pin_image_updates_manifest` | POST /stock/pin updates selected+locked in manifest |
| 5 | `test_pin_image_sets_pinned_by_human` | pinned_by="human" after pin |
| 6 | `test_pin_image_invalid_chapter` | 400 for unknown chapter_id |
| 7 | `test_pin_image_invalid_pexels_id` | 400 for unknown pexels_id |
| 8 | `test_rank_endpoint_triggers_ranking` | POST /stock/rank calls rank_candidates() |
| 9 | `test_serve_candidate_image` | GET /files/stock_candidate serves image file |
| 10 | `test_serve_candidate_path_traversal` | Path traversal attempt → 400/403 |

### 9.4 Integration Tests — ~5 tests

| # | Test | What it verifies |
|---|------|------------------|
| 1 | `test_full_stock_flow_search_rank_pin_approve` | End-to-end: search → rank → pin → approve → finalize |
| 2 | `test_pipeline_imagegen_no_dalle` | v2 imagegen never imports image_generator module |
| 3 | `test_rerank_after_pin_preserves_locked` | Re-rank skips locked, updates unlocked |
| 4 | `test_manifest_backward_compat_v1_schema` | Phase 1 manifest (no rank fields) still works |
| 5 | `test_stock_images_mandatory_for_v2` | Pipeline refuses to run v2 without pexels_api_key |

**Total: ~43 tests**

### 9.5 Test Patterns

- Mock `call_claude()` for LLM ranking tests (return pre-built JSON response)
- Mock `PexelsService` for search tests (as in Phase 1 tests)
- Use in-memory SQLite via `db_session` fixture
- Use `tmp_path` for file system operations
- Phase 1 test fixtures (`pexels_search_response.json`) reused where applicable

---

## 10. Implementation Order & DoD

### Step 1: Prompt Template + Ranking Function

**Files**: `btcedu/prompts/templates/stock_rank.md`, `btcedu/core/stock_images.py`
**Tests**: `tests/test_stock_ranking.py` (18 tests)
**DoD**: `rank_candidates()` works standalone, writes rank fields to manifest, all 18 tests pass

### Step 2: Pipeline Integration — Remove DALL-E, Add Review Gate

**Files**: `btcedu/core/pipeline.py`, `btcedu/core/reviewer.py`
**Tests**: `tests/test_stock_review_gate.py` (10 tests)
**DoD**:
- `_V2_STAGES` includes `review_gate_stock`
- `imagegen` handler calls search + rank only (no finalize, no DALL-E)
- `review_gate_stock` handler creates ReviewTask and finalizes on approval
- DALL-E branch deleted from pipeline
- Pexels API key validation added
- All 10 tests pass

### Step 3: API Endpoints

**Files**: `btcedu/web/api.py`
**Tests**: `tests/test_stock_api.py` (10 tests)
**DoD**: All 4 endpoints work, all 10 tests pass

### Step 4: Dashboard UI

**Files**: `btcedu/web/static/app.js`, `btcedu/web/static/styles.css`
**Tests**: Manual testing (UI is JS, no pytest)
**DoD**: Stock Images tab renders, thumbnails display, Pin button works, Approve button calls review API

### Step 5: CLI Updates

**Files**: `btcedu/cli.py`
**Tests**: Part of integration tests
**DoD**: `btcedu stock rank` works, `btcedu stock list` shows rank column

### Step 6: Integration Tests + Config Cleanup

**Files**: `btcedu/config.py` (deprecation warning), integration test file
**Tests**: 5 integration tests
**DoD**: All ~43 new tests pass, full test suite has no regressions, `btcedu run --episode-id EP` completes v2 pipeline with stock images only

### Non-Goals

- Unsplash/Pixabay provider support (future Phase 3)
- Image editing/cropping in dashboard
- Multi-select (multiple images per chapter)
- A/B testing different stock photos
- Caching Pexels search results across episodes
- Deleting `image_generator.py` or `image_gen_service.py` (kept for v1 compat and manual use)
- Changing the `images/manifest.json` output schema (must remain DALL-E-compatible for TTS/render)

---

## Appendix A: Candidate Manifest Migration

Phase 1 manifests (schema_version `"1.0"`) lack `rank`, `rank_reason`, and `pinned_by` fields. Phase 2 code must handle both:

```python
# In rank_candidates():
rank = candidate.get("rank")  # None for Phase 1 manifests
pinned_by = ch_data.get("pinned_by")  # None for Phase 1 manifests
```

No migration script needed. Phase 2 code treats missing fields as unranked/unpinned.

## Appendix B: Cost Estimate

| Operation | Cost per episode |
|-----------|-----------------|
| Pexels search (Phase 1) | $0.00 |
| LLM ranking (~12 chapters × ~500 input tokens) | ~$0.01–0.03 |
| Human review | $0.00 |
| **Total** | **~$0.01–0.03** |

vs. DALL-E 3: ~$0.48–0.72 per episode (12 chapters × $0.04–0.06 each)

**Savings**: ~95% reduction in image generation cost.
