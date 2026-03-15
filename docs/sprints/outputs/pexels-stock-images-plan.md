# Pexels Stock Images — Implementation Plan

> **Goal:** Replace AI-generated images (DALL-E 3) with curated Pexels stock photos.
> AI images often don't match content; stock photos are cheaper, more predictable, and properly licensed.

---

## 1. Architecture Overview

### Design Principle: Drop-in Replacement

The existing `imagegen` pipeline stage produces an `images/manifest.json` consumed by the renderer. The stock-image flow will produce the **same manifest format**, making it transparent to downstream stages (TTS, RENDER). A config flag (`image_gen_provider`) switches between `dalle3` and `pexels`.

### Data Flow

```
chapters.json
    |
    v
[STOCK SEARCH] -- Pexels API --> candidates/ (3-8 per chapter)
    |                              + candidates_manifest.json
    v
  REVIEW (image selection per chapter)
    |
    v
[STOCK SELECT] --> images/ + manifest.json   (same format as DALL-E output)
    |
    v
  TTS / RENDER (unchanged)
```

### Key Decisions

1. **Reuse `imagegen` stage slot** — no new pipeline status. The stage name stays `"imagegen"` and status progresses `CHAPTERIZED → IMAGES_GENERATED` as before.
2. **Two-phase workflow** — `stock search` fetches candidates, `stock select` (or review approval) finalizes selection. This enables human curation without blocking the automated flow.
3. **Pexels API** — free with attribution, 200 req/hr, no cost per image. Only `PEXELS_API_KEY` required.

---

## 2. New Files

| File | Purpose |
|------|---------|
| `btcedu/services/pexels_service.py` | Pexels API client (search, download, rate limiting) |
| `btcedu/core/stock_images.py` | Orchestration: search candidates, select, build manifest |
| `tests/test_pexels_service.py` | Service-level tests (mocked HTTP) |
| `tests/test_stock_images.py` | Core orchestration tests |
| `tests/fixtures/pexels_search_response.json` | Deterministic API fixture |

### Modified Files

| File | Changes |
|------|---------|
| `btcedu/config.py` | Add `pexels_api_key`, `pexels_results_per_chapter`, `pexels_orientation` |
| `btcedu/cli.py` | Add `stock` command group with `search`, `select`, `list` subcommands |
| `btcedu/core/pipeline.py` | Branch `imagegen` stage to call `stock_images.search_and_select()` when `image_gen_provider == "pexels"` |
| `btcedu/core/image_generator.py` | No changes (preserved for `dalle3` provider) |
| `btcedu/web/api.py` | Add stock candidate endpoints |

---

## 3. Service Layer: `btcedu/services/pexels_service.py`

### Data Structures

```python
@dataclass
class PexelsPhoto:
    """Single photo result from Pexels API."""
    id: int
    width: int
    height: int
    url: str                    # Pexels page URL
    photographer: str
    photographer_url: str
    src_original: str           # Full-res download URL
    src_landscape: str          # 1200x627 landscape crop
    src_large2x: str            # 1880px wide
    alt: str                    # Alt text / description
    avg_color: str              # Hex color


@dataclass
class PexelsSearchResult:
    """Response from a Pexels search."""
    query: str
    total_results: int
    photos: list[PexelsPhoto]
    page: int
    per_page: int
```

### Class: `PexelsService`

```python
class PexelsService:
    """Pexels stock photo API client with rate limiting."""

    def __init__(self, api_key: str, requests_per_hour: int = 180):
        ...

    def search(
        self,
        query: str,
        per_page: int = 8,
        page: int = 1,
        orientation: str = "landscape",  # "landscape" | "portrait" | "square"
        size: str = "large",             # "large" | "medium" | "small"
    ) -> PexelsSearchResult:
        """Search Pexels for photos matching query.

        Raises:
            RuntimeError: On API error or rate limit exceeded.
        """
        ...

    def download_photo(
        self,
        photo: PexelsPhoto,
        target_path: Path,
        size: str = "large2x",  # "original" | "large2x" | "landscape"
    ) -> Path:
        """Download photo to local file. Returns path."""
        ...

    def _rate_limit_wait(self) -> None:
        """Block if approaching rate limit (200/hr). Uses sliding window."""
        ...
```

### Implementation Notes

- **Endpoint**: `GET https://api.pexels.com/v1/search`
- **Auth**: `Authorization: {api_key}` header
- **Rate limit**: 200 requests/hour. Track via response headers `X-Ratelimit-Remaining`.
- **Retry**: Exponential backoff on 429 (up to 3 retries, max wait 60s).
- **Download**: Use `src.large2x` (1880px wide) by default — closest to 1920x1080. Downloaded as JPEG.
- **No SDK**: Raw `requests` calls, consistent with project pattern (ElevenLabs, etc.).

### Protocol

```python
class StockPhotoService(Protocol):
    """Protocol for stock photo services (future: Unsplash, Pixabay)."""

    def search(self, query: str, per_page: int, orientation: str) -> PexelsSearchResult: ...
    def download_photo(self, photo: PexelsPhoto, target_path: Path) -> Path: ...
```

---

## 4. Core Module: `btcedu/core/stock_images.py`

### Data Structures

```python
@dataclass
class StockCandidate:
    """A candidate photo for a chapter."""
    chapter_id: str
    pexels_id: int
    photographer: str
    photographer_url: str
    source_url: str             # Pexels page URL
    download_url: str           # Direct image URL used
    local_path: str             # Relative path from episode outputs dir
    alt_text: str
    search_query: str
    downloaded_at: str          # ISO timestamp
    size_bytes: int
    width: int
    height: int
    selected: bool = False      # True if user selected this candidate
    locked: bool = False        # True if selection is locked (won't change on re-search)


@dataclass
class StockSearchResult:
    """Summary of stock image search for one episode."""
    episode_id: str
    candidates_dir: Path
    candidates_manifest_path: Path
    chapters_searched: int
    total_candidates: int
    skipped_chapters: int       # Chapters with locked selections
    cost_usd: float = 0.0      # Always 0 for Pexels


@dataclass
class StockSelectResult:
    """Summary after finalizing selections."""
    episode_id: str
    images_path: Path
    manifest_path: Path         # Same format as DALL-E manifest
    selected_count: int
    placeholder_count: int      # title_card etc.
```

### Functions

```python
def search_stock_images(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> StockSearchResult:
    """Search Pexels for candidate images per chapter.

    For each chapter with visual type in {diagram, b_roll, screen_share}:
    1. Derive search query from chapter title + visual description + narration keywords
    2. Search Pexels API (3-8 results per chapter)
    3. Download candidates to data/outputs/{ep_id}/images/candidates/{ch_id}/
    4. Write candidates_manifest.json

    Respects locked selections: chapters with locked=True are skipped.
    """
    ...


def _derive_search_query(chapter: Chapter) -> str:
    """Extract search keywords from chapter metadata.

    Strategy:
    1. Start with visual.description (most specific)
    2. Add chapter title keywords
    3. Filter Turkish stop-words
    4. Translate key terms to English (Pexels works best with English queries)
    5. Append domain tags: "bitcoin cryptocurrency finance" for topic relevance
    6. Cap at ~8 keywords

    Returns:
        English search query string
    """
    ...


def select_stock_image(
    session: Session,
    episode_id: str,
    chapter_id: str,
    pexels_id: int,
    settings: Settings,
    lock: bool = False,
) -> None:
    """Select a specific candidate photo for a chapter.

    1. Find candidate in candidates_manifest.json by pexels_id
    2. Copy/symlink to images/{chapter_id}.jpg
    3. Mark as selected (and optionally locked) in manifest
    4. If all chapters have selections, build final images/manifest.json
    """
    ...


def auto_select_best(
    session: Session,
    episode_id: str,
    settings: Settings,
) -> StockSelectResult:
    """Auto-select the first candidate for each chapter (for pipeline automation).

    Does NOT mark as locked. Creates review task for human verification.
    """
    ...


def finalize_selections(
    session: Session,
    episode_id: str,
    settings: Settings,
) -> StockSelectResult:
    """Build final images/manifest.json from selected candidates.

    Produces the same manifest format as DALL-E image_generator:
    {
        "episode_id": "...",
        "schema_version": "1.0",
        "generated_at": "...",
        "images": [
            {
                "chapter_id": "ch01",
                "chapter_title": "...",
                "visual_type": "b_roll",
                "file_path": "images/ch01_selected.jpg",
                "prompt": null,
                "generation_method": "pexels",
                "model": null,
                "size": "1880x1253",
                "mime_type": "image/jpeg",
                "size_bytes": 234567,
                "metadata": {
                    "pexels_id": 12345,
                    "photographer": "John Doe",
                    "photographer_url": "https://www.pexels.com/@johndoe",
                    "source_url": "https://www.pexels.com/photo/12345",
                    "license": "Pexels License (free for commercial use)",
                    "search_query": "bitcoin cryptocurrency digital",
                    "alt_text": "Close-up of golden Bitcoin coin",
                    "downloaded_at": "2026-03-15T12:00:00Z"
                }
            },
            ...
        ]
    }

    Also writes:
    - provenance/imagegen_provenance.json (same schema)
    - ContentArtifact record
    - MediaAsset records
    - Updates episode status to IMAGES_GENERATED
    """
    ...
```

### Candidate Manifest Schema

Stored at `data/outputs/{ep_id}/images/candidates_manifest.json`:

```json
{
    "episode_id": "SJFLLZxlWqk",
    "schema_version": "1.0",
    "searched_at": "2026-03-15T12:00:00Z",
    "chapters_hash": "abc123...",
    "chapters": {
        "ch01": {
            "search_query": "wealth inequality economics graph",
            "candidates": [
                {
                    "pexels_id": 12345,
                    "photographer": "John Doe",
                    "photographer_url": "https://www.pexels.com/@johndoe",
                    "source_url": "https://www.pexels.com/photo/12345",
                    "download_url": "https://images.pexels.com/photos/12345/...",
                    "local_path": "images/candidates/ch01/pexels_12345.jpg",
                    "alt_text": "Graph showing wealth distribution",
                    "width": 1880,
                    "height": 1253,
                    "size_bytes": 234567,
                    "downloaded_at": "2026-03-15T12:00:00Z",
                    "selected": true,
                    "locked": false
                },
                ...
            ]
        },
        "ch02": { ... },
        ...
    }
}
```

### Search Query Derivation

The `_derive_search_query` function is critical for quality results. Strategy:

```python
def _derive_search_query(chapter: Chapter) -> str:
    """
    Input chapter example:
        title: "1971: Para Sistemindeki Değişiklik"
        visual.description: "1971 öncesi ve sonrası ekonomik grafikler"
        visual.type: "diagram"
        narration.text: "1971'de para sistemi büyük bir değişim geçirdi..."

    Processing:
    1. Extract nouns/keywords from visual.description
    2. Map Turkish domain terms to English:
       - "para" -> "money"
       - "ekonomik" -> "economic"
       - "Bitcoin" -> "Bitcoin" (unchanged)
       - "enflasyon" -> "inflation"
       - "zenginlik" -> "wealth"
    3. Add visual-type modifiers:
       - diagram -> "chart graph infographic"
       - b_roll -> "photo"
    4. Always append: "finance"
    5. Deduplicate and cap at 8 terms

    Output: "1971 money system economic chart graph finance"
    """
```

A static translation table (~50 domain-specific Turkish→English terms) is sufficient.
No LLM call needed — keeps cost at $0.

---

## 5. Config Changes: `btcedu/config.py`

```python
# Stock Images / Pexels
pexels_api_key: str = ""
pexels_results_per_chapter: int = 5        # Candidates to fetch per chapter (3-8)
pexels_orientation: str = "landscape"       # "landscape" | "portrait" | "square"
pexels_download_size: str = "large2x"       # "original" | "large2x" | "landscape"
```

The existing `image_gen_provider` field controls the switch:

```python
image_gen_provider: str = "dalle3"  # "dalle3" or "pexels"
```

---

## 6. CLI Commands: `btcedu/cli.py`

### New `stock` Command Group

```python
@cli.group()
def stock():
    """Stock image management (Pexels)."""
    pass


@stock.command()
@click.option("--episode-id", "episode_ids", multiple=True, required=True)
@click.option("--force", is_flag=True, default=False,
              help="Re-search even if candidates exist.")
@click.option("--per-page", default=None, type=int,
              help="Override candidates per chapter.")
@click.pass_context
def search(ctx, episode_ids, force, per_page):
    """Search Pexels for candidate images per chapter.

    Downloads 3-8 candidate photos per chapter into
    data/outputs/{ep_id}/images/candidates/.
    """
    ...


@stock.command()
@click.option("--episode-id", required=True)
@click.option("--chapter", "chapter_id", required=True,
              help="Chapter to select image for (e.g. ch03).")
@click.option("--photo-id", "pexels_id", required=True, type=int,
              help="Pexels photo ID to select.")
@click.option("--lock", is_flag=True, default=False,
              help="Lock this selection (won't change on re-search).")
@click.pass_context
def select(ctx, episode_id, chapter_id, pexels_id, lock):
    """Select a specific Pexels photo for a chapter."""
    ...


@stock.command(name="list")
@click.option("--episode-id", required=True)
@click.pass_context
def list_status(ctx, episode_id):
    """Show stock image selection status per chapter.

    Output:
      ch01  [selected]  pexels:12345  "wealth inequality graph"
      ch02  [pending]   5 candidates  "political perspectives"
      ch03  [locked]    pexels:67890  "1971 economic change"
      ...
      ch15  [template]  title_card placeholder
    """
    ...


@stock.command(name="auto-select")
@click.option("--episode-id", required=True)
@click.pass_context
def auto_select(ctx, episode_id):
    """Auto-select first candidate per chapter and create review task."""
    ...
```

### Modified `imagegen` Command

No changes needed. When `image_gen_provider == "pexels"`, the pipeline's `_run_stage("imagegen")` dispatches to `stock_images` instead of `image_generator`.

---

## 7. Pipeline Integration: `btcedu/core/pipeline.py`

### Modified `_run_stage` for `imagegen`

```python
elif stage_name == "imagegen":
    if settings.image_gen_provider == "pexels":
        from btcedu.core.stock_images import search_stock_images, auto_select_best, finalize_selections

        # Phase 1: Search candidates (if not already done)
        search_result = search_stock_images(session, episode.episode_id, settings, force=force)

        # Phase 2: Auto-select best candidates
        auto_select_best(session, episode.episode_id, settings)

        # Phase 3: Finalize into images/manifest.json
        result = finalize_selections(session, episode.episode_id, settings)

        elapsed = time.monotonic() - t0
        return StageResult(
            "imagegen", "success", elapsed,
            detail=f"{result.selected_count} stock images selected, "
                   f"{result.placeholder_count} placeholders, $0.00"
        )
    else:
        from btcedu.core.image_generator import generate_images
        # ... existing DALL-E flow unchanged ...
```

### No New Pipeline Status

The `imagegen` stage still transitions `CHAPTERIZED → IMAGES_GENERATED`. The downstream stages (TTS, RENDER) are unaffected because they consume `images/manifest.json` which has the same schema regardless of provider.

---

## 8. Review Flow

### Automatic Flow (Pipeline)

When running via `btcedu run`, the pipeline:
1. Searches Pexels candidates
2. Auto-selects the first candidate per chapter
3. Creates a review task (stage=`"stock_images"`) with artifact_paths pointing to `candidates_manifest.json`
4. Finalizes selections into `images/manifest.json`
5. Advances to `IMAGES_GENERATED`

> Note: Unlike DALL-E, stock images are cheap to replace. The review task is created for quality assurance but does **not block** the pipeline. The user can later re-select better images and re-render.

### Manual Flow (CLI)

For careful curation:

```bash
# 1. Search candidates
btcedu stock search --episode-id SJFLLZxlWqk

# 2. Review candidates (see what's available)
btcedu stock list --episode-id SJFLLZxlWqk

# 3. Select specific photos per chapter
btcedu stock select --episode-id SJFLLZxlWqk --chapter ch03 --photo-id 67890 --lock

# 4. Auto-select remaining chapters
btcedu stock auto-select --episode-id SJFLLZxlWqk

# 5. Continue pipeline
btcedu run --episode-id SJFLLZxlWqk
```

### Review Task Details

The review task for stock images includes:
- Thumbnail grid of selected images per chapter
- Chapter title + visual description alongside each image
- Pexels source link for each photo
- "Replace" action per chapter (opens candidate list)

---

## 9. Idempotency and Invalidation

### Idempotency

`search_stock_images()` checks:
1. `candidates_manifest.json` exists
2. `chapters_hash` in manifest matches current `chapters.json` hash
3. No `.stale` marker exists

If all pass → skip (return existing results).

`finalize_selections()` checks:
1. `images/manifest.json` exists
2. Provenance hash matches
3. No `.stale` marker

### Invalidation Cascade

**If `chapters.json` changes** (re-chapterize):
- The chapterizer writes `.stale` markers for `images/` (existing behavior)
- `search_stock_images()` detects stale marker → re-searches
- Locked selections are **preserved** unless the chapter was removed/renumbered
- Unlocked selections are cleared

**If stock selections change** (user re-selects):
- `select_stock_image()` writes `.stale` markers for `render/draft.mp4`
- Next `btcedu run` will re-render with new images

---

## 10. Directory Layout

```
data/outputs/{ep_id}/
├── images/
│   ├── manifest.json                     # Final manifest (same format as DALL-E)
│   ├── ch01_selected.jpg                 # Selected images
│   ├── ch02_selected.jpg
│   ├── ...
│   ├── ch15_placeholder.png              # Title card placeholder
│   └── candidates/                       # Search candidates
│       ├── candidates_manifest.json
│       ├── ch01/
│       │   ├── pexels_12345.jpg
│       │   ├── pexels_12346.jpg
│       │   └── pexels_12347.jpg
│       ├── ch02/
│       │   └── ...
│       └── ...
├── provenance/
│   └── imagegen_provenance.json          # Same schema as DALL-E provenance
└── ...
```

---

## 11. Search Query Translation Table

Static mapping of ~50 Turkish domain terms used in Bitcoin education content:

```python
_TR_TO_EN = {
    # Economics
    "para": "money", "ekonomi": "economy", "ekonomik": "economic",
    "enflasyon": "inflation", "deflasyon": "deflation",
    "zenginlik": "wealth", "gelir": "income", "dağılım": "distribution",
    "faiz": "interest rate", "borç": "debt", "vergi": "tax",
    "bütçe": "budget", "tasarruf": "savings", "yatırım": "investment",
    "piyasa": "market", "ticaret": "trade", "ihracat": "export",
    # Bitcoin / crypto
    "bitcoin": "bitcoin", "kripto": "cryptocurrency",
    "blokzincir": "blockchain", "madencilik": "mining",
    "cüzdan": "wallet", "işlem": "transaction",
    # Finance
    "banka": "bank", "merkez bankası": "central bank",
    "dolar": "dollar", "altın": "gold", "hisse": "stock",
    # Visuals
    "grafik": "chart graph", "tablo": "table",
    "karşılaştırma": "comparison", "diyagram": "diagram",
    # General
    "sistem": "system", "değişim": "change", "tarih": "history",
    "toplum": "society", "eşitsizlik": "inequality",
    "üretim": "production", "tüketim": "consumption",
    "teknoloji": "technology", "dijital": "digital",
    "güvenlik": "security", "özgürlük": "freedom",
}
```

---

## 12. Tests

### `tests/test_pexels_service.py` (~15 tests)

```python
# Fixtures
SAMPLE_SEARCH_RESPONSE = {
    "total_results": 42,
    "page": 1,
    "per_page": 5,
    "photos": [
        {
            "id": 12345,
            "width": 5000,
            "height": 3333,
            "url": "https://www.pexels.com/photo/12345/",
            "photographer": "Test Photographer",
            "photographer_url": "https://www.pexels.com/@test",
            "src": {
                "original": "https://images.pexels.com/photos/12345/original.jpeg",
                "large2x": "https://images.pexels.com/photos/12345/large2x.jpeg",
                "landscape": "https://images.pexels.com/photos/12345/landscape.jpeg",
            },
            "alt": "Golden Bitcoin coin on dark background",
            "avg_color": "#2D2D2D",
        },
        # ... more photos
    ],
}

class TestPexelsService:
    def test_search_returns_photos(self, mock_requests): ...
    def test_search_passes_orientation(self, mock_requests): ...
    def test_search_auth_header(self, mock_requests): ...
    def test_search_rate_limit_retry(self, mock_requests): ...
    def test_search_api_error_raises(self, mock_requests): ...
    def test_download_photo_saves_file(self, mock_requests, tmp_path): ...
    def test_download_photo_creates_parent_dirs(self, mock_requests, tmp_path): ...
    def test_rate_limit_tracking(self): ...
```

### `tests/test_stock_images.py` (~25 tests)

```python
class TestSearchStockImages:
    def test_search_creates_candidates_manifest(self, db_session, mock_pexels): ...
    def test_search_downloads_candidates(self, db_session, mock_pexels): ...
    def test_search_skips_locked_chapters(self, db_session, mock_pexels): ...
    def test_search_skips_title_card_chapters(self, db_session, mock_pexels): ...
    def test_search_idempotent_when_current(self, db_session, mock_pexels): ...
    def test_search_re_searches_on_stale_marker(self, db_session, mock_pexels): ...
    def test_search_re_searches_on_chapters_change(self, db_session, mock_pexels): ...

class TestDeriveSearchQuery:
    def test_translates_turkish_terms(self): ...
    def test_adds_visual_type_modifiers(self): ...
    def test_caps_at_8_keywords(self): ...
    def test_deduplicates(self): ...
    def test_appends_finance_domain(self): ...

class TestSelectStockImage:
    def test_select_marks_candidate_selected(self, db_session): ...
    def test_select_copies_to_images_dir(self, db_session): ...
    def test_select_with_lock(self, db_session): ...
    def test_select_invalid_photo_id_raises(self, db_session): ...

class TestAutoSelectBest:
    def test_selects_first_candidate_per_chapter(self, db_session): ...
    def test_skips_already_selected(self, db_session): ...

class TestFinalizeSelections:
    def test_produces_dalle_compatible_manifest(self, db_session): ...
    def test_creates_provenance(self, db_session): ...
    def test_creates_content_artifact(self, db_session): ...
    def test_creates_media_assets(self, db_session): ...
    def test_updates_episode_status(self, db_session): ...
    def test_marks_render_stale(self, db_session): ...
```

### Test Fixture: `tests/fixtures/pexels_search_response.json`

A static JSON file matching the Pexels API response format, used by all tests to avoid network calls.

---

## 13. Migration Strategy

### Backward Compatibility

- `image_gen_provider = "dalle3"` (default): Existing DALL-E flow is untouched.
- `image_gen_provider = "pexels"`: New stock image flow.
- The config default stays `"dalle3"` — opt-in to Pexels.
- Both providers produce the same `images/manifest.json` schema.
- No database migration needed (reuses existing `media_assets`, `content_artifacts` tables).

### Switching Mid-Episode

If an episode already has DALL-E images and you switch to Pexels:
- Running `btcedu stock search --episode-id X --force` overwrites the candidates
- Running `btcedu stock auto-select --episode-id X` + `btcedu run` overwrites `images/manifest.json`
- The render will automatically re-render with new images (stale marker)

### Pexels License Attribution

Pexels License requires no attribution but recommends it. The metadata in `manifest.json` stores photographer name and URL. A future enhancement could add a credits roll to the video outro.

---

## 14. Implementation Order

| Step | Task | Files | Est. Tests |
|------|------|-------|------------|
| 1 | Pexels service + tests | `services/pexels_service.py`, `tests/test_pexels_service.py`, `tests/fixtures/pexels_search_response.json` | 15 |
| 2 | Config additions | `config.py` | 0 |
| 3 | Core stock_images module + tests | `core/stock_images.py`, `tests/test_stock_images.py` | 25 |
| 4 | CLI stock command group | `cli.py` | 0 (manual testing) |
| 5 | Pipeline integration | `core/pipeline.py` | 2-3 |
| 6 | Web API endpoints (optional) | `web/api.py` | 0 |

**Total: ~40-43 new tests**

### Step 1: Pexels Service

Build and test the HTTP client in isolation. Mock all network calls. Verify auth headers, rate limiting, retry logic, download to file.

### Step 2: Config

Add 4 new fields to `Settings`. No migration needed (Pydantic defaults).

### Step 3: Core Module

Build search, select, auto-select, finalize. This is the bulk of the work. Key focus:
- `_derive_search_query()` with Turkish→English translation table
- Candidate manifest read/write
- Final manifest in DALL-E-compatible format
- Idempotency checks
- Locked selection preservation

### Step 4: CLI

Wire up `stock` group with `search`, `select`, `list`, `auto-select` subcommands. Follow existing CLI patterns (session management, error handling, click.echo output).

### Step 5: Pipeline

Single branch in `_run_stage("imagegen")` to dispatch based on `settings.image_gen_provider`. Minimal change — ~20 lines.

### Step 6: Web API (Optional / Follow-up)

Endpoints for browsing candidates, selecting photos via dashboard. Not required for CLI-first workflow.

---

## 15. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Pexels search returns irrelevant results for Bitcoin topics | Turkish→English translation table + domain keyword appending. Manual `select` override. |
| Rate limit (200/hr) hit with many chapters | Track `X-Ratelimit-Remaining` header, preemptive sleep. 15 chapters * 5 candidates = 15 API calls (well within limit). |
| Pexels images are landscape but not exactly 1920x1080 | Renderer already scales+pads via ffmpeg `scale=1920:1080:force_original_aspect_ratio=decrease,pad=...`. No change needed. |
| JPEG artifacts vs PNG quality | JPEG quality is fine for video frames at 1080p. File size is ~5x smaller than DALL-E PNGs. |
| User forgets to select images before running pipeline | `auto_select_best()` picks first candidate automatically. Review task created for later curation. |
