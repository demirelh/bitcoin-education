# Sprint 6 — Implementation Plan (Chapterized Production JSON)

**Sprint Number:** 6
**Phase:** 3 (Chapterization + Image Generation), Part 1
**Status:** Planning
**Dependencies:** Sprint 5 (Adaptation) complete
**Created:** 2026-02-25

---

## 1. Sprint Scope Summary

**In Scope:**

Sprint 6 implements the **CHAPTERIZE** stage, which transforms the Turkey-adapted Turkish script into a structured JSON document that defines production-ready chapters for video assembly. Each chapter specifies narration text, visual type, overlay elements, timing guidance, and transitions.

This sprint delivers:
1. The **chapter JSON schema** (version 1.0) — the contract between CHAPTERIZE and all downstream stages (IMAGE_GEN, TTS, RENDER)
2. A **chapterization prompt template** with instructions to decompose scripts into chapters
3. The **chapterizer module** (`btcedu/core/chapterizer.py`) with full idempotency, provenance, validation
4. **JSON schema validation** using Pydantic models
5. A **CLI command** (`btcedu chapterize <episode_id>`)
6. **Pipeline integration** after ADAPT + Review Gate 2
7. A **dashboard chapter viewer** (read-only timeline view)
8. **Tests** for chapterizer logic, validation, and integration

**Not In Scope (Deferred to Later Sprints):**

- Image generation (Sprint 7)
- TTS audio generation (Sprint 8)
- Video rendering (Sprint 9-10)
- Review Gate 3 after chapterization (no gate specified in MASTERPLAN for this stage)
- Editing chapters in the dashboard (read-only view only)
- Chapter re-ordering or manual chapter creation UI
- Background music, intro/outro overlays (deferred per MASTERPLAN §13)

---

## 2. File-Level Plan

### 2.1 Files to Create

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/core/chapterizer.py`
**Purpose:** Core chapterization logic
**Contents:**
- `SEGMENT_CHAR_LIMIT = 15_000` (constant for large scripts)
- `@dataclass ChapterizationResult` — return type with `episode_id`, `chapters_path`, `provenance_path`, `chapter_count`, `estimated_duration_seconds`, `input_tokens`, `output_tokens`, `cost_usd`, `skipped`
- `chapterize_script(session, episode_id, settings, force=False) -> ChapterizationResult` — main function
- `_is_chapterization_current(output_path, provenance_path, input_content_hash, prompt_content_hash) -> bool` — idempotency check
- `_split_prompt(template_body: str) -> tuple[str, str]` — split at "# Input" marker
- `_segment_script(text: str, limit: int) -> list[str]` — segment large scripts if needed
- `_validate_chapter_json(data: dict) -> bool` — validate JSON structure against Pydantic schema
- `_compute_duration_estimate(word_count: int) -> float` — Turkish: 150 words/min

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/models/chapter_schema.py`
**Purpose:** Pydantic models for chapter JSON schema validation
**Contents:**
- `class VisualType(str, Enum)` — title_card, diagram, b_roll, talking_head, screen_share
- `class TransitionType(str, Enum)` — fade, cut, dissolve
- `class OverlayType(str, Enum)` — lower_third, title, quote, statistic
- `class Narration(BaseModel)` — text, word_count, estimated_duration_seconds
- `class Visual(BaseModel)` — type: VisualType, description, image_prompt (optional)
- `class Overlay(BaseModel)` — type: OverlayType, text, start_offset_seconds, duration_seconds
- `class Transitions(BaseModel)` — in: TransitionType, out: TransitionType
- `class Chapter(BaseModel)` — chapter_id, title, order, narration, visual, overlays, transitions, notes
- `class ChapterDocument(BaseModel)` — schema_version, episode_id, title, total_chapters, estimated_duration_seconds, chapters
- Validation methods: check chapter_id uniqueness, sequential order, duration consistency

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/prompts/templates/chapterize.md`
**Purpose:** Prompt template for chapterization
**Contents:** Full template with YAML frontmatter and instructions (see section 4 below)

#### `/home/runner/work/bitcoin-education/bitcoin-education/tests/test_chapterizer.py`
**Purpose:** Unit and integration tests
**Contents:**
- `test_chapterization_result_dataclass()` — validates result structure
- `test_segment_script()` — tests large script splitting
- `test_compute_duration_estimate()` — verifies 150 words/min calculation
- `test_validate_chapter_json_valid()` — Pydantic validation with valid JSON
- `test_validate_chapter_json_invalid_schema()` — catches schema errors
- `test_validate_chapter_json_duplicate_chapter_ids()` — uniqueness check
- `test_chapterize_script_dry_run()` — end-to-end with dry-run
- `test_chapterize_script_idempotency()` — runs twice, second skips
- `test_chapterize_script_force()` — force re-run works
- `test_chapterize_script_missing_adapted_script()` — error handling
- `test_chapter_schema_validation_pydantic()` — Pydantic model validation

### 2.2 Files to Modify

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/models/episode.py`
**Changes:**
- Add `CHAPTERIZED = "chapterized"` to `EpisodeStatus` enum (line ~22)
- Add `CHAPTERIZE = "chapterize"` to `PipelineStage` enum (line ~41)

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/core/pipeline.py`
**Changes:**
- Add `EpisodeStatus.CHAPTERIZED: 13` to `_STATUS_ORDER` dict (line ~35)
- Add `("chapterize", EpisodeStatus.ADAPTED)` to `_V2_STAGES` list after adaptation entry (line ~64)
- Add chapterize stage handler to `_run_stage()` function (line ~240):
  ```python
  elif stage_name == "chapterize":
      from btcedu.core.chapterizer import chapterize_script
      result = chapterize_script(session, episode.episode_id, settings, force=force)
      elapsed = time.monotonic() - t0
      if result.skipped:
          return StageResult("chapterize", "skipped", elapsed, detail="already up-to-date")
      else:
          return StageResult(
              "chapterize",
              "success",
              elapsed,
              detail=f"{result.chapter_count} chapters, ~{result.estimated_duration_seconds}s (${result.cost_usd:.4f})",
          )
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/cli.py`
**Changes:**
- Add `@cli.command()` function `chapterize()` after the `adapt` command (line ~656):
  ```python
  @cli.command()
  @click.option(
      "--episode-id",
      "episode_ids",
      multiple=True,
      required=True,
      help="Episode ID(s) to chapterize (repeatable).",
  )
  @click.option("--force", is_flag=True, default=False, help="Re-chapterize even if output exists.")
  @click.option("--dry-run", is_flag=True, default=False, help="Write request JSON instead of calling Claude API.")
  @click.pass_context
  def chapterize(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
      """Chapterize adapted script into production JSON (v2 pipeline)."""
      from btcedu.core.chapterizer import chapterize_script

      settings = ctx.obj["settings"]
      if dry_run:
          settings.dry_run = True

      session = ctx.obj["session_factory"]()
      try:
          for eid in episode_ids:
              try:
                  result = chapterize_script(session, eid, settings, force=force)
                  if result.skipped:
                      click.echo(f"[SKIP] {eid} -> already up-to-date")
                  else:
                      click.echo(
                          f"[OK] {eid} -> {result.chapter_count} chapters, "
                          f"~{result.estimated_duration_seconds}s (${result.cost_usd:.4f})"
                      )
              except Exception as e:
                  click.echo(f"[FAIL] {eid}: {e}", err=True)
      finally:
          session.close()
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/web/api.py`
**Changes:**
- Add file mapping to `_FILE_MAP` dict (line ~350):
  ```python
  "chapters": ("outputs_dir", "{eid}/chapters.json"),
  ```
- Add chapter viewer route or extend episode detail API to include chapter data (line ~450+):
  ```python
  @app.route("/api/episodes/<episode_id>/chapters", methods=["GET"])
  def get_episode_chapters(episode_id: str):
      """Get chapter structure for episode."""
      settings = get_settings()
      chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"

      if not chapters_path.exists():
          return jsonify({"error": "Chapters not found"}), 404

      try:
          chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
          return jsonify(chapters_data)
      except (json.JSONDecodeError, OSError) as e:
          return jsonify({"error": f"Failed to load chapters: {e}"}), 500
  ```

#### `/home/runner/work/bitcoin-education/bitcoin-education/btcedu/web/templates/episode_detail.html`
**Changes:**
- Add "Chapters" section after "Adaptation" section
- Display chapter count, estimated duration, and link to chapter viewer
- Show chapter timeline with cards for each chapter (title, narration preview, visual type badge, duration)

---

## 3. Chapter JSON Schema Definition (Version 1.0)

### 3.1 Full Schema (from MASTERPLAN §5D)

```json
{
  "schema_version": "1.0",
  "episode_id": "abc123",
  "title": "Bitcoin Nedir?",
  "total_chapters": 8,
  "estimated_duration_seconds": 720,
  "chapters": [
    {
      "chapter_id": "ch01",
      "title": "Giriş",
      "order": 1,
      "narration": {
        "text": "Merhaba arkadaşlar, bugün Bitcoin'in temel prensiplerine bakacağız...",
        "word_count": 150,
        "estimated_duration_seconds": 60
      },
      "visual": {
        "type": "title_card",
        "description": "Channel logo with episode title overlay",
        "image_prompt": null
      },
      "overlays": [
        {
          "type": "lower_third",
          "text": "Bitcoin Nedir?",
          "start_offset_seconds": 2,
          "duration_seconds": 5
        }
      ],
      "transitions": {
        "in": "fade",
        "out": "cut"
      },
      "notes": "Hook the viewer in first 5 seconds"
    },
    {
      "chapter_id": "ch02",
      "title": "Bitcoin'in Tarihi",
      "order": 2,
      "narration": {
        "text": "Bitcoin 2008 yılında Satoshi Nakamoto tarafından...",
        "word_count": 200,
        "estimated_duration_seconds": 80
      },
      "visual": {
        "type": "diagram",
        "description": "Timeline showing Bitcoin's key milestones from 2008 to present",
        "image_prompt": "Clean minimalist timeline diagram showing Bitcoin history from 2008 to 2024"
      },
      "overlays": [
        {
          "type": "title",
          "text": "2008: Bitcoin Whitepaper",
          "start_offset_seconds": 10,
          "duration_seconds": 3
        }
      ],
      "transitions": {
        "in": "dissolve",
        "out": "cut"
      },
      "notes": "Use visual timeline to anchor key dates"
    }
  ]
}
```

### 3.2 Schema Field Definitions

**Top-Level Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | Semantic version (e.g., "1.0"). Minor increments (1.0 → 1.1) are additive/backward-compatible. Major increments (1.x → 2.0) are breaking. |
| `episode_id` | string | Yes | Episode identifier (YouTube video ID) |
| `title` | string | Yes | Episode title in Turkish |
| `total_chapters` | integer | Yes | Count of chapters in `chapters` array |
| `estimated_duration_seconds` | integer | Yes | Sum of all chapter durations |
| `chapters` | array | Yes | Array of Chapter objects (must be non-empty) |

**Chapter Object Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `chapter_id` | string | Yes | Unique ID (e.g., "ch01", "ch02"). Must be unique within episode. |
| `title` | string | Yes | Chapter title (short, descriptive) |
| `order` | integer | Yes | Sequential order starting at 1. Must be strictly sequential. |
| `narration` | Narration object | Yes | What is said in this chapter |
| `visual` | Visual object | Yes | What is shown in this chapter |
| `overlays` | array of Overlay | Yes | Text/graphic overlays (can be empty array) |
| `transitions` | Transitions object | Yes | Transition effects |
| `notes` | string | No | Production notes (optional, for human editors) |

**Narration Object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | Yes | Full narration text in Turkish |
| `word_count` | integer | Yes | Word count of text |
| `estimated_duration_seconds` | integer | Yes | Computed from word_count (~150 words/min for Turkish) |

**Visual Object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | VisualType enum | Yes | One of: `title_card`, `diagram`, `b_roll`, `talking_head`, `screen_share` |
| `description` | string | Yes | Human-readable description of visual |
| `image_prompt` | string or null | No | Prompt for image generation (null for types that use templates like `title_card`) |

**VisualType Enum:**
- `title_card` — Branded intro/outro/section divider (uses template, no image generation)
- `diagram` — Illustration, infographic, chart (generated)
- `b_roll` — Contextual imagery, photos, stock-style visuals (generated)
- `talking_head` — On-camera presenter or avatar (future: not generated in Sprint 7)
- `screen_share` — Screen recording, code, app demo (future: not generated in Sprint 7)

**Overlay Object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | OverlayType enum | Yes | One of: `lower_third`, `title`, `quote`, `statistic` |
| `text` | string | Yes | Text to display |
| `start_offset_seconds` | number | Yes | Start time relative to chapter start (0 = chapter start) |
| `duration_seconds` | number | Yes | How long overlay is shown |

**OverlayType Enum:**
- `lower_third` — Name/title bar at bottom (common for intros)
- `title` — Large centered title text
- `quote` — Highlighted quote or key point
- `statistic` — Data point or number highlight

**Transitions Object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `in` | TransitionType enum | Yes | Transition into this chapter |
| `out` | TransitionType enum | Yes | Transition out of this chapter |

**TransitionType Enum:**
- `fade` — Gradual fade in/out
- `cut` — Hard cut (instant)
- `dissolve` — Cross-dissolve blend

### 3.3 Schema Versioning & Compatibility

**Schema Version Field (`schema_version`):**
- **Format:** `"MAJOR.MINOR"` (e.g., "1.0", "1.1", "2.0")
- **Minor Version Increment (1.0 → 1.1):** Additive changes only. New optional fields, new enum values. Old chapters.json files are valid.
- **Major Version Increment (1.x → 2.0):** Breaking changes. Field renames, required field additions, enum value removals. Old chapters.json files require regeneration.

**Validation Rules:**
- IMAGE_GEN, TTS, and RENDER stages check `schema_version` before processing
- If major version mismatch, stage fails with clear error: "Chapter schema v2.0 required, found v1.0. Re-run chapterize."
- If minor version older than expected, stage proceeds (backward compatible)

**Schema Evolution Example:**
- v1.0 (Sprint 6): Initial schema
- v1.1 (future): Add optional `background_music` field to Chapter
- v2.0 (future): Restructure overlays to support animations (breaking change)

### 3.4 Validation Approach: Pydantic Models

**Recommendation:** Use **Pydantic v2 BaseModel** for validation.

**Rationale:**
1. Codebase already uses Pydantic for `Settings` (config.py)
2. Provides type-safe validation with excellent error messages
3. Easy to serialize/deserialize JSON
4. Built-in enum support
5. Can add custom validators for business logic (e.g., sequential order check)

**Alternative Considered:** JSON Schema + `jsonschema` library
- Pro: Language-agnostic schema definition
- Con: Less type-safe, more verbose, codebase doesn't use it elsewhere
- **Decision:** Reject. Pydantic is better integrated.

---

## 4. Chapterization Prompt Template

### Full Template: `btcedu/prompts/templates/chapterize.md`

```markdown
---
name: chapterize
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 8192
description: Decomposes adapted Turkish script into structured production chapters
author: content_owner
---

# System

You are a video production editor specializing in educational Bitcoin content for Turkish audiences. Your task is to transform a finished Turkish script (already adapted for Turkey context) into a structured **production-ready chapter JSON** for video assembly.

Each chapter defines:
1. **Narration** (what is said)
2. **Visual** (what is shown)
3. **Overlays** (text/graphics)
4. **Timing** (duration estimates)
5. **Transitions** (fade, cut, dissolve)

Your output will drive image generation, text-to-speech, and video rendering stages. Precision and completeness are critical.

---

# Instructions

## Input

You will receive:
- **Adapted Turkish script** — a finished, Turkey-contextualized educational script about Bitcoin

## Output

Produce a **valid JSON document** matching this schema exactly:

```json
{
  "schema_version": "1.0",
  "episode_id": "{{episode_id}}",
  "title": "[Extract from script or generate appropriate title]",
  "total_chapters": 0,
  "estimated_duration_seconds": 0,
  "chapters": [
    {
      "chapter_id": "ch01",
      "title": "[Short descriptive title]",
      "order": 1,
      "narration": {
        "text": "[Full narration text for this chapter]",
        "word_count": 0,
        "estimated_duration_seconds": 0
      },
      "visual": {
        "type": "[title_card|diagram|b_roll|talking_head|screen_share]",
        "description": "[Human-readable description of what is shown]",
        "image_prompt": "[Prompt for image generation API, or null]"
      },
      "overlays": [
        {
          "type": "[lower_third|title|quote|statistic]",
          "text": "[Text to display]",
          "start_offset_seconds": 0.0,
          "duration_seconds": 5.0
        }
      ],
      "transitions": {
        "in": "[fade|cut|dissolve]",
        "out": "[cut|fade|dissolve]"
      },
      "notes": "[Optional production note]"
    }
  ]
}
```

---

## Chapterization Guidelines

### Chapter Count & Structure

- **Target:** 6-10 chapters for a ~15-minute episode (900 seconds)
- **Shorter episodes:** Fewer chapters (minimum 3)
- **Longer episodes:** More chapters (maximum 15)
- **Chapter length:** Aim for 60-120 seconds per chapter (1-2 minutes)
- **Balance:** Distribute content evenly. Avoid one very long chapter and several very short ones.

### Narration

- **Decompose script:** Break the adapted script into logical chapter segments
- **Preserve all content:** Do NOT skip, summarize, or paraphrase. Copy the adapted script text verbatim into narration fields.
- **Natural breaks:** Chapter boundaries should align with topic shifts, pauses, or section headers in the script
- **Word count:** Count words accurately (split on whitespace)
- **Duration estimate:** Use **150 words per minute** for Turkish:
  - `estimated_duration_seconds = (word_count / 150) * 60`
  - Round to nearest integer

### Visual Type Selection

Choose the most appropriate visual type for each chapter:

1. **`title_card`**
   - Use for: Intro, outro, major section dividers
   - Description: Branded template with channel logo, episode title, or section title
   - `image_prompt`: Set to `null` (uses template, not generated)

2. **`diagram`**
   - Use for: Explanations, processes, comparisons, technical concepts
   - Description: What the diagram shows (e.g., "Bitcoin transaction flow diagram")
   - `image_prompt`: Clear, detailed prompt for image generation API (e.g., "Clean minimalist diagram showing Bitcoin transaction flow with nodes, arrows, and labeled components")

3. **`b_roll`**
   - Use for: Contextual visuals, establishing shots, metaphors
   - Description: What is shown (e.g., "Busy city street representing economic activity")
   - `image_prompt`: Detailed prompt (e.g., "Professional photo of a busy city street at night with neon lights, representing economic activity and commerce")

4. **`talking_head`**
   - Use for: Direct address, personal stories, opinion segments
   - Description: Presenter or avatar on-camera
   - `image_prompt`: `null` (future: will use avatar or video, not generated in Sprint 7)

5. **`screen_share`**
   - Use for: Demos, code walkthroughs, app tutorials
   - Description: What application/screen is shown
   - `image_prompt`: `null` (future: will use screen recording, not generated in Sprint 7)

**Default:** If unsure, use `diagram` for technical content or `b_roll` for general content.

### Overlays

Add overlays to emphasize key points:

- **`lower_third`:** Name/title bar at bottom (use in intro chapters)
  - Example: `{"type": "lower_third", "text": "Bitcoin Nedir?", "start_offset_seconds": 2, "duration_seconds": 5}`

- **`title`:** Large centered title (use for section breaks)
  - Example: `{"type": "title", "text": "Bölüm 1: Temeller", "start_offset_seconds": 0, "duration_seconds": 3}`

- **`quote`:** Highlighted quote or key takeaway
  - Example: `{"type": "quote", "text": "Bitcoin güveni matematiğe dayandırır", "start_offset_seconds": 15, "duration_seconds": 7}`

- **`statistic`:** Data point or number
  - Example: `{"type": "statistic", "text": "21 Milyon BTC", "start_offset_seconds": 30, "duration_seconds": 5}`

**Guidelines:**
- Use overlays sparingly (0-3 per chapter)
- Timing: `start_offset_seconds` is relative to chapter start (0 = chapter begins)
- Duration: Keep overlays on-screen 3-7 seconds
- Overlays array can be empty `[]` if no overlays needed

### Transitions

- **`in`:** Transition into this chapter from the previous one
  - `fade`: Gradual fade-in (use for gentle topic shifts, intros)
  - `cut`: Instant cut (use for fast pacing, same topic continuation)
  - `dissolve`: Cross-dissolve blend (use for smooth visual changes)

- **`out`:** Transition out of this chapter to the next one
  - Same options as `in`
  - Default: `cut` for most transitions, `fade` for outro

**First chapter:** `in` should be `fade` (fade in from black)
**Last chapter:** `out` should be `fade` (fade to black)

### Notes Field (Optional)

Add production notes to help human editors:
- Pacing guidance: "Speak slowly to emphasize"
- Visual cues: "Show diagram before explaining"
- Editing tips: "Add pause after this point"

---

## Constraints (CRITICAL)

1. **NO hallucination:** All narration text MUST come directly from the provided adapted script. Do NOT invent, summarize, or paraphrase content.

2. **NO financial advice:** Do NOT add investment recommendations, price predictions, or trading advice. The adapted script is already sanitized; preserve it exactly.

3. **NO content alteration:** You are restructuring, not rewriting. The script is final. Your job is to divide it into chapters and assign visuals.

4. **Valid JSON only:** Output MUST be parseable JSON. No markdown code fences, no explanatory text before/after. Just the JSON object.

5. **Schema compliance:** Every field must match the schema exactly. Required fields cannot be null or omitted.

6. **Unique chapter IDs:** `chapter_id` must be unique (e.g., "ch01", "ch02", ..., "ch10"). Use zero-padded numbers.

7. **Sequential order:** `order` must be 1, 2, 3, ... with no gaps.

8. **Accurate counts:** `total_chapters` must equal `len(chapters)`. `estimated_duration_seconds` must equal sum of all chapter durations.

9. **Duration realism:** Total duration should match script length. For a typical 15-min script (~2250 words Turkish), expect ~900 seconds total.

---

# Input

## Episode ID
```
{{episode_id}}
```

## Adapted Turkish Script
```
{{adapted_script}}
```

---

# Output

Return only the JSON object (no markdown, no explanations):

```json
{
  "schema_version": "1.0",
  ...
}
```
```

---

## 5. Chapterizer Module Design

### 5.1 Function Signature

```python
def chapterize_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> ChapterizationResult:
    """
    Chapterize adapted script into structured production JSON.

    Args:
        session: SQLAlchemy session
        episode_id: Episode identifier (YouTube video ID)
        settings: Application settings
        force: If True, re-chapterize even if output exists

    Returns:
        ChapterizationResult with paths, counts, tokens, cost

    Raises:
        ValueError: If episode not found or not in ADAPTED status
        FileNotFoundError: If adapted script missing
        ValidationError: If LLM produces invalid JSON
    """
```

### 5.2 Return Type

```python
@dataclass
class ChapterizationResult:
    """Result of chapterization operation."""
    episode_id: str
    chapters_path: str              # Path to chapters.json
    provenance_path: str            # Path to provenance JSON
    chapter_count: int              # Number of chapters generated
    estimated_duration_seconds: int # Total estimated video duration
    input_tokens: int               # Claude input tokens
    output_tokens: int              # Claude output tokens
    cost_usd: float                 # API cost
    skipped: bool = False           # True if idempotent skip
```

### 5.3 Processing Logic

**High-Level Flow:**

1. **Lookup episode** (validate exists, status is ADAPTED or later)
2. **Check Review Gate 2** (adapted script must be approved)
3. **Resolve file paths:**
   - Input: `data/outputs/{episode_id}/script.adapted.tr.md`
   - Output: `data/outputs/{episode_id}/chapters.json`
   - Provenance: `data/outputs/{episode_id}/provenance/chapterize_provenance.json`
4. **Load and register prompt** via `PromptRegistry`
5. **Compute input content hash** (SHA-256 of adapted script)
6. **Check idempotency** (if not force and output is current, skip)
7. **Create PipelineRun** record (stage=CHAPTERIZE, status=RUNNING)
8. **Try-except block:**
   - Load adapted script
   - Inject reviewer feedback (if any)
   - Split prompt into system/user
   - Segment script if > 15,000 chars (unlikely for adapted scripts)
   - Call Claude API
   - Parse JSON response
   - Validate JSON with Pydantic model
   - If validation fails: retry once with corrective prompt
   - If still fails: raise ValidationError
   - Write chapters.json
   - Write provenance JSON
   - Persist ContentArtifact
   - Update PipelineRun (success)
   - Update Episode status to CHAPTERIZED
   - **Mark downstream as stale** (if IMAGE_GEN or TTS outputs exist)
9. **Except block:** Mark PipelineRun as FAILED, set error message
10. **Return result**

### 5.4 Segmentation (if needed)

**Scenario:** Adapted scripts are typically 2000-5000 words (~12,000-30,000 chars). Most fit in one Claude call. But for very long episodes (>30 min), scripts may exceed 15,000 chars.

**Strategy:**
- If `len(adapted_script) <= 15_000`: Process as single segment
- If longer: Split at paragraph boundaries (`\n\n`), process each segment, merge chapter arrays in final JSON

**Implementation:**
```python
def _segment_script(text: str, limit: int = 15_000) -> list[str]:
    """Split script into segments at paragraph boundaries."""
    if len(text) <= limit:
        return [text]

    paragraphs = text.split("\n\n")
    segments = []
    current = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for \n\n
        if current_len + para_len > limit and current:
            segments.append("\n\n".join(current))
            current = [para]
            current_len = para_len
        else:
            current.append(para)
            current_len += para_len

    if current:
        segments.append("\n\n".join(current))

    return segments
```

**Merging segments:**
```python
# After processing all segments
all_chapters = []
for idx, segment_json in enumerate(segment_responses):
    data = json.loads(segment_json)
    chapters = data["chapters"]

    # Re-number chapters sequentially
    for ch in chapters:
        ch["order"] = len(all_chapters) + 1
        ch["chapter_id"] = f"ch{ch['order']:02d}"

    all_chapters.extend(chapters)

# Build final JSON
final_json = {
    "schema_version": "1.0",
    "episode_id": episode_id,
    "title": segment_responses[0]["title"],  # Use title from first segment
    "total_chapters": len(all_chapters),
    "estimated_duration_seconds": sum(ch["narration"]["estimated_duration_seconds"] for ch in all_chapters),
    "chapters": all_chapters,
}
```

**[ASSUMPTION]:** Multi-segment processing is rare. Test with single-segment first. Implement multi-segment if real episodes require it.

---

## 6. Duration Estimation

### 6.1 Algorithm

**Formula:** Turkish narration speed is approximately **150 words per minute**.

```python
def _compute_duration_estimate(word_count: int) -> float:
    """
    Estimate narration duration in seconds from Turkish word count.

    Turkish speech rate: ~150 words/minute.

    Args:
        word_count: Number of words in narration text

    Returns:
        Estimated duration in seconds (rounded to nearest integer)
    """
    WORDS_PER_MINUTE = 150
    duration_minutes = word_count / WORDS_PER_MINUTE
    duration_seconds = duration_minutes * 60
    return round(duration_seconds)
```

**Example:**
- 150 words → 60 seconds (1 minute)
- 75 words → 30 seconds
- 300 words → 120 seconds (2 minutes)

### 6.2 Application in Pipeline

**Two places duration is computed:**

1. **In the prompt (by LLM):** The chapterization prompt instructs Claude to compute `estimated_duration_seconds` for each chapter's narration using 150 words/min formula.

2. **In the chapterizer module (validation):** After parsing LLM output, the module validates that durations are reasonable:
   - Recalculate expected duration from word_count
   - If LLM's duration is off by >20%, log warning but accept (LLM may account for pauses)
   - Sum chapter durations to get total episode duration
   - Store in `ChapterizationResult.estimated_duration_seconds`

**[ASSUMPTION]:** Trust LLM's duration estimates if within 20% of formula. Don't override unless validation fails.

---

## 7. Visual Type Classification

### 7.1 Visual Types

| Type | When to Use | `image_prompt` | Generated? |
|------|-------------|----------------|------------|
| `title_card` | Intro, outro, section dividers | `null` | No (uses template) |
| `diagram` | Technical explanations, processes, charts | Required | Yes (Sprint 7) |
| `b_roll` | Contextual visuals, establishing shots | Required | Yes (Sprint 7) |
| `talking_head` | Direct address, personal segments | `null` | No (future: avatar/video) |
| `screen_share` | Demos, code, app tutorials | `null` | No (future: screen recording) |

### 7.2 Selection Guidance (in Prompt)

The chapterization prompt provides these rules:

- **Default for technical content:** `diagram`
- **Default for general content:** `b_roll`
- **Intro/outro:** `title_card`
- **Direct address to viewer:** `talking_head` (rare in current episodes)
- **App demo:** `screen_share` (rare in current episodes)

**[ASSUMPTION]:** Most chapters will be `diagram` or `b_roll`. Validation should warn if >50% of chapters are `title_card` (indicates over-use).

### 7.3 Image Prompt Requirements

- **If visual type is `diagram` or `b_roll`:** `image_prompt` must be a non-empty string
- **If visual type is `title_card`, `talking_head`, or `screen_share`:** `image_prompt` should be `null`

**Validation:** Pydantic model validates this constraint with custom validator.

---

## 8. Schema Validation

### 8.1 Validation Strategy

**Use Pydantic v2 BaseModel** for JSON validation.

**Validation Flow:**
1. LLM returns JSON string
2. Parse JSON with `json.loads()` (raises JSONDecodeError if malformed)
3. Validate with Pydantic: `ChapterDocument.model_validate(data)`
4. Pydantic checks:
   - All required fields present
   - Types correct (int, str, enum values)
   - Enum values are valid
   - Custom validators: chapter_id uniqueness, sequential order, duration consistency
5. If validation passes: proceed
6. If validation fails: **retry once** with corrective prompt
7. If retry fails: raise ValidationError with detailed message

### 8.2 Pydantic Models (Full Implementation)

**File:** `btcedu/models/chapter_schema.py`

```python
"""Pydantic models for chapter JSON schema validation."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class VisualType(str, Enum):
    """Visual type for chapter."""
    TITLE_CARD = "title_card"
    DIAGRAM = "diagram"
    B_ROLL = "b_roll"
    TALKING_HEAD = "talking_head"
    SCREEN_SHARE = "screen_share"


class TransitionType(str, Enum):
    """Transition effect type."""
    FADE = "fade"
    CUT = "cut"
    DISSOLVE = "dissolve"


class OverlayType(str, Enum):
    """Overlay type."""
    LOWER_THIRD = "lower_third"
    TITLE = "title"
    QUOTE = "quote"
    STATISTIC = "statistic"


class Narration(BaseModel):
    """Narration data for a chapter."""
    text: str = Field(..., min_length=1, description="Full narration text")
    word_count: int = Field(..., ge=1, description="Word count of text")
    estimated_duration_seconds: int = Field(..., ge=1, description="Estimated duration")

    @field_validator("estimated_duration_seconds")
    @classmethod
    def validate_duration(cls, v: int, info) -> int:
        """Validate duration is reasonable for word count."""
        word_count = info.data.get("word_count", 0)
        if word_count > 0:
            expected_duration = round((word_count / 150) * 60)
            # Allow 20% variance
            if abs(v - expected_duration) > (expected_duration * 0.2):
                # Log warning but don't fail (LLM may account for pauses)
                pass
        return v


class Visual(BaseModel):
    """Visual data for a chapter."""
    type: VisualType = Field(..., description="Visual type")
    description: str = Field(..., min_length=1, description="Description of visual")
    image_prompt: Optional[str] = Field(None, description="Image generation prompt (null for non-generated types)")

    @model_validator(mode="after")
    def validate_image_prompt(self) -> "Visual":
        """Validate image_prompt based on visual type."""
        needs_prompt = self.type in (VisualType.DIAGRAM, VisualType.B_ROLL)
        has_prompt = self.image_prompt is not None and len(self.image_prompt) > 0

        if needs_prompt and not has_prompt:
            raise ValueError(
                f"Visual type '{self.type}' requires image_prompt, got null/empty"
            )

        if not needs_prompt and has_prompt:
            # Warning only, not error (LLM may provide prompt for future use)
            pass

        return self


class Overlay(BaseModel):
    """Overlay data for a chapter."""
    type: OverlayType = Field(..., description="Overlay type")
    text: str = Field(..., min_length=1, description="Text to display")
    start_offset_seconds: float = Field(..., ge=0.0, description="Start time relative to chapter start")
    duration_seconds: float = Field(..., gt=0.0, description="Duration of overlay")


class Transitions(BaseModel):
    """Transition effects for a chapter."""
    in_transition: TransitionType = Field(..., alias="in", description="Transition in")
    out_transition: TransitionType = Field(..., alias="out", description="Transition out")


class Chapter(BaseModel):
    """Chapter data."""
    chapter_id: str = Field(..., min_length=1, description="Unique chapter ID")
    title: str = Field(..., min_length=1, description="Chapter title")
    order: int = Field(..., ge=1, description="Sequential order")
    narration: Narration = Field(..., description="Narration data")
    visual: Visual = Field(..., description="Visual data")
    overlays: list[Overlay] = Field(default_factory=list, description="Overlays (can be empty)")
    transitions: Transitions = Field(..., description="Transitions")
    notes: Optional[str] = Field(None, description="Production notes (optional)")


class ChapterDocument(BaseModel):
    """Complete chapter document."""
    schema_version: str = Field(..., pattern=r"^\d+\.\d+$", description="Schema version (e.g., '1.0')")
    episode_id: str = Field(..., min_length=1, description="Episode identifier")
    title: str = Field(..., min_length=1, description="Episode title")
    total_chapters: int = Field(..., ge=1, description="Total number of chapters")
    estimated_duration_seconds: int = Field(..., ge=1, description="Total estimated duration")
    chapters: list[Chapter] = Field(..., min_items=1, description="Array of chapters")

    @model_validator(mode="after")
    def validate_document(self) -> "ChapterDocument":
        """Validate document-level constraints."""
        # Check total_chapters matches array length
        if self.total_chapters != len(self.chapters):
            raise ValueError(
                f"total_chapters ({self.total_chapters}) != len(chapters) ({len(self.chapters)})"
            )

        # Check chapter_id uniqueness
        chapter_ids = [ch.chapter_id for ch in self.chapters]
        if len(chapter_ids) != len(set(chapter_ids)):
            duplicates = [cid for cid in chapter_ids if chapter_ids.count(cid) > 1]
            raise ValueError(f"Duplicate chapter_id values: {duplicates}")

        # Check sequential order (1, 2, 3, ...)
        expected_order = list(range(1, len(self.chapters) + 1))
        actual_order = [ch.order for ch in self.chapters]
        if actual_order != expected_order:
            raise ValueError(
                f"Chapter order must be sequential 1..{len(self.chapters)}, got {actual_order}"
            )

        # Check estimated_duration_seconds is sum of chapter durations
        total_duration = sum(ch.narration.estimated_duration_seconds for ch in self.chapters)
        if abs(self.estimated_duration_seconds - total_duration) > 5:
            # Allow 5-second variance for rounding
            raise ValueError(
                f"estimated_duration_seconds ({self.estimated_duration_seconds}) != "
                f"sum of chapter durations ({total_duration})"
            )

        return self
```

### 8.3 Validation Failure Handling

**Strategy:** Retry once with corrective prompt.

```python
try:
    chapter_doc = ChapterDocument.model_validate(data)
except ValidationError as e:
    logger.warning("Chapterization output failed validation: %s", e)

    # Retry with corrective prompt
    corrective_prompt = f"""
The previous JSON output had validation errors:

{e}

Please correct the JSON and return a valid document matching the schema exactly.
Pay special attention to:
- All required fields present
- chapter_id values unique
- order values sequential (1, 2, 3, ...)
- total_chapters = len(chapters)
- estimated_duration_seconds = sum of chapter durations

Original input:
{adapted_script}
"""

    retry_response = call_claude(
        system_prompt=system_prompt,
        user_message=corrective_prompt,
        settings=settings,
    )

    # Parse and validate retry
    retry_data = json.loads(retry_response.text)
    chapter_doc = ChapterDocument.model_validate(retry_data)  # If this fails, raise
```

**If retry also fails:** Raise `ValidationError` with both error messages. The pipeline will mark the run as FAILED.

---

## 9. CLI Command Design

### 9.1 Command Implementation

**Location:** Add after `adapt` command in `btcedu/cli.py` (line ~656)

```python
@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to chapterize (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-chapterize even if output exists.")
@click.option("--dry-run", is_flag=True, default=False, help="Write request JSON instead of calling Claude API.")
@click.pass_context
def chapterize(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Chapterize adapted script into production JSON (v2 pipeline).

    Transforms the Turkey-adapted Turkish script into a structured chapter document
    with narration, visuals, overlays, and timing guidance for video assembly.

    Examples:

        btcedu chapterize --episode-id abc123

        btcedu chapterize --episode-id abc123 --force

        btcedu chapterize --episode-id abc123 def456 --dry-run
    """
    from btcedu.core.chapterizer import chapterize_script

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = chapterize_script(session, eid, settings, force=force)
                if result.skipped:
                    click.echo(f"[SKIP] {eid} -> already up-to-date")
                else:
                    click.echo(
                        f"[OK] {eid} -> {result.chapter_count} chapters, "
                        f"~{result.estimated_duration_seconds}s total, "
                        f"{result.input_tokens} in / {result.output_tokens} out "
                        f"(${result.cost_usd:.4f})"
                    )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()
```

### 9.2 Help Output

```
$ btcedu chapterize --help

Usage: btcedu chapterize [OPTIONS]

  Chapterize adapted script into production JSON (v2 pipeline).

  Transforms the Turkey-adapted Turkish script into a structured chapter
  document with narration, visuals, overlays, and timing guidance for video
  assembly.

Options:
  --episode-id TEXT  Episode ID(s) to chapterize (repeatable).  [required]
  --force            Re-chapterize even if output exists.
  --dry-run          Write request JSON instead of calling Claude API.
  --help             Show this message and exit.
```

---

## 10. Pipeline Integration

### 10.1 Pipeline Stage Addition

**File:** `btcedu/core/pipeline.py`

**Changes:**

1. **Add to _STATUS_ORDER** (line ~35):
```python
_STATUS_ORDER = {
    # ... existing statuses ...
    EpisodeStatus.ADAPTED: 12,
    EpisodeStatus.CHAPTERIZED: 13,  # NEW
    EpisodeStatus.IMAGES_GENERATED: 14,
    # ...
}
```

2. **Add to _V2_STAGES** (line ~64):
```python
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate", EpisodeStatus.CORRECTED),
    ("adapt", EpisodeStatus.TRANSLATED),
    ("review_gate_2", EpisodeStatus.ADAPTED),
    ("chapterize", EpisodeStatus.ADAPTED),  # NEW
    # Future: ("imagegen", EpisodeStatus.CHAPTERIZED),
    # Future: ("tts", EpisodeStatus.CHAPTERIZED),
]
```

3. **Add stage handler to _run_stage()** (line ~240):
```python
elif stage_name == "chapterize":
    from btcedu.core.chapterizer import chapterize_script

    result = chapterize_script(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0

    if result.skipped:
        return StageResult("chapterize", "skipped", elapsed, detail="already up-to-date")
    else:
        return StageResult(
            "chapterize",
            "success",
            elapsed,
            detail=(
                f"{result.chapter_count} chapters, "
                f"~{result.estimated_duration_seconds}s, "
                f"${result.cost_usd:.4f}"
            ),
        )
```

### 10.2 Review Gate Dependency

**Chapterize stage runs after Review Gate 2 (ADAPT review).**

The pipeline checks:
1. Episode status is ADAPTED
2. ReviewTask for "adapt" stage is APPROVED
3. If not approved, stage skips with status "review_pending"

**[ASSUMPTION]:** No review gate after CHAPTERIZE (per MASTERPLAN). Chapters are informational, not requiring approval before IMAGE_GEN.

---

## 11. Dashboard Chapter Viewer

### 11.1 API Endpoint

**File:** `btcedu/web/api.py`

**Add route** (line ~450):

```python
@app.route("/api/episodes/<episode_id>/chapters", methods=["GET"])
def get_episode_chapters(episode_id: str):
    """Get chapter structure for episode.

    Returns:
        200: Chapter JSON document
        404: Chapters not found
        500: Failed to load chapters
    """
    settings = get_settings()
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"

    if not chapters_path.exists():
        return jsonify({"error": "Chapters not found"}), 404

    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return jsonify(chapters_data)
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": f"Failed to load chapters: {e}"}), 500
```

### 11.2 Dashboard UI

**File:** `btcedu/web/templates/episode_detail.html`

**Add section after "Adaptation":**

```html
{% if episode.status in ["chapterized", "images_generated", "tts_done", "rendered", "approved", "published", "completed"] %}
<div class="section">
    <h3>Chapters</h3>
    <div id="chapters-viewer">
        <p class="loading">Loading chapters...</p>
    </div>
</div>

<script>
// Fetch and display chapters
fetch(`/api/episodes/{{ episode.episode_id }}/chapters`)
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            document.getElementById('chapters-viewer').innerHTML =
                `<p class="error">${data.error}</p>`;
            return;
        }

        renderChapters(data);
    })
    .catch(err => {
        document.getElementById('chapters-viewer').innerHTML =
            `<p class="error">Failed to load chapters: ${err}</p>`;
    });

function renderChapters(data) {
    const container = document.getElementById('chapters-viewer');

    // Summary
    let html = `
        <div class="chapters-summary">
            <strong>Episode:</strong> ${data.title}<br>
            <strong>Chapters:</strong> ${data.total_chapters}<br>
            <strong>Estimated Duration:</strong> ${formatDuration(data.estimated_duration_seconds)}<br>
            <strong>Schema Version:</strong> ${data.schema_version}
        </div>
        <div class="chapters-timeline">
    `;

    // Chapter cards
    data.chapters.forEach(ch => {
        const narrationPreview = ch.narration.text.substring(0, 100) +
            (ch.narration.text.length > 100 ? '...' : '');

        html += `
            <div class="chapter-card">
                <div class="chapter-header">
                    <span class="chapter-order">${ch.order}</span>
                    <span class="chapter-title">${ch.title}</span>
                    <span class="chapter-duration">${formatDuration(ch.narration.estimated_duration_seconds)}</span>
                </div>
                <div class="chapter-body">
                    <div class="chapter-narration">
                        <strong>Narration:</strong> ${narrationPreview}
                        <br><em>${ch.narration.word_count} words</em>
                    </div>
                    <div class="chapter-visual">
                        <span class="visual-badge visual-${ch.visual.type}">${ch.visual.type}</span>
                        ${ch.visual.description}
                    </div>
                    ${ch.overlays.length > 0 ? renderOverlays(ch.overlays) : ''}
                    ${ch.notes ? `<div class="chapter-notes"><em>Note: ${ch.notes}</em></div>` : ''}
                </div>
            </div>
        `;
    });

    html += '</div>';
    container.innerHTML = html;
}

function renderOverlays(overlays) {
    let html = '<div class="chapter-overlays"><strong>Overlays:</strong><ul>';
    overlays.forEach(ov => {
        html += `<li>[${ov.type}] "${ov.text}" at ${ov.start_offset_seconds}s for ${ov.duration_seconds}s</li>`;
    });
    html += '</ul></div>';
    return html;
}

function formatDuration(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}
</script>

<style>
.chapters-summary {
    background: #f5f5f5;
    padding: 10px;
    margin-bottom: 15px;
    border-radius: 4px;
}

.chapters-timeline {
    display: flex;
    flex-direction: column;
    gap: 15px;
}

.chapter-card {
    border: 1px solid #ddd;
    border-radius: 6px;
    padding: 12px;
    background: #fff;
}

.chapter-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid #eee;
}

.chapter-order {
    background: #007bff;
    color: white;
    padding: 4px 10px;
    border-radius: 4px;
    font-weight: bold;
    font-size: 14px;
}

.chapter-title {
    flex: 1;
    font-weight: bold;
    font-size: 16px;
}

.chapter-duration {
    color: #666;
    font-size: 14px;
}

.chapter-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.chapter-narration {
    color: #333;
    font-size: 14px;
}

.chapter-visual {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
}

.visual-badge {
    padding: 3px 8px;
    border-radius: 3px;
    font-size: 12px;
    font-weight: bold;
    text-transform: uppercase;
}

.visual-title_card { background: #6c757d; color: white; }
.visual-diagram { background: #28a745; color: white; }
.visual-b_roll { background: #17a2b8; color: white; }
.visual-talking_head { background: #ffc107; color: black; }
.visual-screen_share { background: #dc3545; color: white; }

.chapter-overlays {
    font-size: 13px;
    color: #555;
}

.chapter-overlays ul {
    margin: 5px 0 0 20px;
    padding: 0;
}

.chapter-notes {
    color: #777;
    font-size: 13px;
    font-style: italic;
}
</style>
{% endif %}
```

### 11.3 File Mapping

**Add to `_FILE_MAP`** in `btcedu/web/api.py` (line ~350):

```python
_FILE_MAP = {
    # ... existing mappings ...
    "chapters": ("outputs_dir", "{eid}/chapters.json"),
}
```

---

## 12. Provenance, Idempotency, Cascade Invalidation

### 12.1 Provenance JSON

**File:** `data/outputs/{episode_id}/provenance/chapterize_provenance.json`

**Format:**
```json
{
  "stage": "chapterize",
  "episode_id": "abc123",
  "timestamp": "2026-02-25T12:00:00Z",
  "prompt_name": "chapterize",
  "prompt_version": 1,
  "prompt_hash": "sha256:abcdef...",
  "model": "claude-sonnet-4-20250514",
  "model_params": {
    "temperature": 0.3,
    "max_tokens": 8192
  },
  "input_files": ["data/outputs/abc123/script.adapted.tr.md"],
  "input_content_hash": "sha256:123456...",
  "output_files": ["data/outputs/abc123/chapters.json"],
  "input_tokens": 3500,
  "output_tokens": 2200,
  "cost_usd": 0.082,
  "duration_seconds": 15.3,
  "segments_processed": 1,
  "chapter_count": 8,
  "estimated_duration_seconds": 720,
  "schema_version": "1.0"
}
```

### 12.2 Idempotency Check

**Function:** `_is_chapterization_current()`

```python
def _is_chapterization_current(
    output_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if chapterization output is still valid.

    Returns True if:
    - chapters.json exists
    - No .stale marker
    - Provenance exists
    - Input hash matches (adapted script unchanged)
    - Prompt hash matches (prompt unchanged)

    Returns False otherwise (triggers re-chapterization).
    """
    if not output_path.exists():
        return False

    # Check for .stale marker (set by downstream rejection or upstream change)
    stale_marker = output_path.parent / (output_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Chapterization marked as stale, removing marker")
        stale_marker.unlink()
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    # Check hashes
    if provenance.get("prompt_hash") != prompt_content_hash:
        logger.info("Chapterization prompt changed, re-running")
        return False

    if provenance.get("input_content_hash") != input_content_hash:
        logger.info("Adapted script changed, re-running chapterization")
        return False

    return True
```

### 12.3 Cascade Invalidation

**When adapted script changes, mark chapters as stale:**

In `btcedu/core/adapter.py`, after writing adapted script:

```python
# Mark downstream chapterization as stale if it exists
chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
if chapters_path.exists():
    stale_marker = chapters_path.parent / (chapters_path.name + ".stale")
    stale_data = {
        "invalidated_at": _utcnow().isoformat(),
        "invalidated_by": "adapt",
        "reason": "adapted_script_changed",
    }
    stale_marker.parent.mkdir(parents=True, exist_ok=True)
    stale_marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
    logger.info("Marked downstream chapterization as stale: %s", chapters_path.name)
```

**When chapterization produces new output, mark IMAGE_GEN and TTS as stale:**

In `btcedu/core/chapterizer.py`, after writing chapters.json:

```python
# Mark downstream stages as stale (IMAGE_GEN, TTS will be implemented in Sprint 7-8)
# For now, this is a no-op, but structure is in place
imagegen_marker_path = Path(settings.outputs_dir) / episode_id / "images" / ".stale"
tts_marker_path = Path(settings.outputs_dir) / episode_id / "tts" / ".stale"

for marker_path in [imagegen_marker_path, tts_marker_path]:
    if marker_path.parent.exists():
        stale_data = {
            "invalidated_at": _utcnow().isoformat(),
            "invalidated_by": "chapterize",
            "reason": "chapters_changed",
        }
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
        logger.info("Marked downstream stage as stale: %s", marker_path.parent.name)
```

---

## 13. Test Plan

### 13.1 Unit Tests

**File:** `tests/test_chapterizer.py`

| Test Function | What It Tests | Assertions |
|---------------|---------------|------------|
| `test_chapterization_result_dataclass()` | ChapterizationResult structure | All fields present, types correct |
| `test_segment_script_short()` | Segmentation with short script | Returns single segment |
| `test_segment_script_long()` | Segmentation with >15k char script | Returns multiple segments, split at paragraphs |
| `test_compute_duration_estimate()` | Duration calculation | 150 words = 60s, 300 words = 120s |
| `test_validate_chapter_json_valid()` | Pydantic validation with valid JSON | No errors |
| `test_validate_chapter_json_missing_field()` | Missing required field | Raises ValidationError |
| `test_validate_chapter_json_invalid_enum()` | Invalid enum value | Raises ValidationError |
| `test_validate_chapter_json_duplicate_chapter_ids()` | Duplicate chapter_id | Raises ValidationError |
| `test_validate_chapter_json_non_sequential_order()` | Non-sequential order (1,3,2) | Raises ValidationError |
| `test_validate_chapter_json_duration_mismatch()` | total_duration != sum of chapters | Raises ValidationError |
| `test_chapterize_script_dry_run()` | End-to-end with dry-run | Writes request JSON, no API call, returns result |
| `test_chapterize_script_idempotency()` | Run twice without force | Second run skips, returns skipped=True |
| `test_chapterize_script_force()` | Force re-chapterization | Re-runs even if output exists |
| `test_chapterize_script_missing_adapted_script()` | Adapted script not found | Raises FileNotFoundError |
| `test_chapterize_script_invalid_status()` | Episode not ADAPTED | Raises ValueError |
| `test_chapterize_script_review_not_approved()` | Review Gate 2 not approved | Raises ValueError or skips |
| `test_chapter_schema_validation_pydantic()` | Pydantic model validation | Valid data passes, invalid fails |

### 13.2 Integration Tests

| Test Function | What It Tests | Setup Required |
|---------------|---------------|----------------|
| `test_chapterize_full_pipeline()` | Full pipeline from ADAPTED to CHAPTERIZED | Episode in ADAPTED status, adapted script exists |
| `test_chapterize_cascade_invalidation()` | Changing adapted script marks chapters stale | Run adapter, then chapterizer, then adapter again with force, verify stale marker |
| `test_chapterize_cli_command()` | CLI command works | Run `btcedu chapterize --episode-id X` via subprocess |

### 13.3 Manual Verification

After implementation:

1. **Setup:** Pick an episode that completed ADAPT stage (e.g., episode from Sprint 5 testing)
2. **Run:** `btcedu chapterize --episode-id <eid>`
3. **Verify output:**
   - `data/outputs/<eid>/chapters.json` exists
   - JSON is valid (use `jq . < chapters.json`)
   - All required fields present
   - Chapter count is reasonable (6-10 for 15-min episode)
   - Duration estimate matches script length
4. **Check idempotency:** Run again → should skip
5. **Force re-run:** Run with `--force` → should regenerate
6. **Dashboard:** Open episode detail page, verify chapter viewer shows chapters
7. **Provenance:** Check `provenance/chapterize_provenance.json` exists with correct metadata

---

## 14. Implementation Order

**Recommended sequence (numbered steps):**

1. **Create Pydantic models** (`btcedu/models/chapter_schema.py`)
   - Define all enums, BaseModel classes, validators
   - Write unit tests for schema validation
   - Verify validation catches errors

2. **Create chapterization prompt template** (`btcedu/prompts/templates/chapterize.md`)
   - Write YAML frontmatter
   - Write system prompt
   - Write instructions with schema, guidelines, constraints
   - Include example JSON in comments

3. **Implement chapterizer module** (`btcedu/core/chapterizer.py`)
   - Define `ChapterizationResult` dataclass
   - Implement helper functions: `_segment_script`, `_compute_duration_estimate`, `_split_prompt`, `_is_chapterization_current`
   - Implement `chapterize_script()` main function
   - Add JSON validation with Pydantic
   - Add retry logic for validation failures
   - Add provenance writing
   - Add cascade invalidation

4. **Update Episode models** (`btcedu/models/episode.py`)
   - Add `EpisodeStatus.CHAPTERIZED`
   - Add `PipelineStage.CHAPTERIZE`

5. **Integrate into pipeline** (`btcedu/core/pipeline.py`)
   - Add to `_STATUS_ORDER`
   - Add to `_V2_STAGES`
   - Add stage handler to `_run_stage()`

6. **Add CLI command** (`btcedu/cli.py`)
   - Implement `chapterize` command
   - Test `--help`, `--force`, `--dry-run`

7. **Write unit tests** (`tests/test_chapterizer.py`)
   - Test all helper functions
   - Test schema validation
   - Test idempotency
   - Run tests: `pytest tests/test_chapterizer.py -v`

8. **Add cascade invalidation to adapter** (`btcedu/core/adapter.py`)
   - Mark chapters.json as stale when adapted script changes

9. **Add web API endpoint** (`btcedu/web/api.py`)
   - Implement `/api/episodes/<id>/chapters` route
   - Add file mapping to `_FILE_MAP`

10. **Add dashboard chapter viewer** (`btcedu/web/templates/episode_detail.html`)
    - Add chapters section with JavaScript to fetch and render
    - Add CSS for chapter cards
    - Test in browser

11. **Manual end-to-end verification**
    - Run full pipeline on test episode: `detect → download → transcribe → correct → approve → translate → adapt → approve → chapterize`
    - Verify all outputs exist
    - Verify dashboard shows chapters
    - Verify idempotency works

12. **Documentation updates** (if needed)
    - Update `ARCHITECTURE.md` with chapter schema
    - Add chapter JSON example to docs

---

## 15. Definition of Done

**Sprint 6 is complete when all of these are true:**

- [ ] `btcedu/models/chapter_schema.py` created with all Pydantic models and validators
- [ ] `btcedu/prompts/templates/chapterize.md` created with complete prompt
- [ ] `btcedu/core/chapterizer.py` created with `chapterize_script()` function
- [ ] `EpisodeStatus.CHAPTERIZED` added to enum
- [ ] `PipelineStage.CHAPTERIZE` added to enum
- [ ] `_STATUS_ORDER` and `_V2_STAGES` updated in pipeline.py
- [ ] Chapterize stage handler added to `_run_stage()`
- [ ] `btcedu chapterize` CLI command works
- [ ] `btcedu chapterize --help` shows correct help text
- [ ] `btcedu chapterize --episode-id X` generates valid chapters.json
- [ ] `btcedu chapterize --episode-id X --force` re-runs even if output exists
- [ ] `btcedu chapterize --episode-id X --dry-run` writes request JSON without API call
- [ ] Running chapterize twice (no force) skips on second run (idempotency)
- [ ] Provenance JSON is written with correct metadata
- [ ] Cascade invalidation: changing adapted script marks chapters.json as stale
- [ ] Cascade invalidation: generating new chapters marks IMAGE_GEN/TTS as stale (structure in place)
- [ ] Web API endpoint `/api/episodes/<id>/chapters` returns chapter JSON
- [ ] Dashboard episode detail page shows chapter viewer with timeline
- [ ] Chapter viewer displays: title, narration preview, visual type badge, duration, overlays
- [ ] All unit tests in `tests/test_chapterizer.py` pass
- [ ] Manual verification: test episode goes through chapterize stage successfully
- [ ] JSON schema validation catches malformed LLM output (tested with intentionally bad JSON)
- [ ] Retry logic works when validation fails (tested by mocking validation error)
- [ ] Existing v1 pipeline still works (no regressions)
- [ ] `btcedu status` shows episode in CHAPTERIZED status after successful run
- [ ] No lint errors: `ruff check btcedu/`
- [ ] No type errors: `mypy btcedu/` (if project uses mypy)

---

## 16. Non-Goals

**Explicitly NOT in scope for Sprint 6:**

- **Image generation:** Sprint 7 will implement IMAGE_GEN stage
- **TTS audio generation:** Sprint 8 will implement TTS stage
- **Video rendering:** Sprint 9-10 will implement RENDER stage
- **Review gate after chapterization:** No review gate specified in MASTERPLAN for this stage
- **Editing chapters in dashboard:** Read-only view only. Manual editing is future work.
- **Chapter re-ordering UI:** Chapters are defined by LLM order. Manual re-ordering is future work.
- **Background music, intro/outro templates:** Deferred per MASTERPLAN §13
- **Multi-language support:** Turkish only for now
- **Schema migration from v1.0 to v2.0:** Not needed (this is v1.0 initial release)
- **A/B testing of chapterization prompts:** Can be added later using existing PromptRegistry
- **Thumbnail generation:** Deferred to IMAGE_GEN stage or later
- **Auto-approval of trivial chapterization changes:** No review gate here
- **Chapter preview video:** No video preview until RENDER stage

---

## Appendix A: Key Assumptions

**[ASSUMPTION 1]:** Most adapted scripts will be under 15,000 chars and fit in a single Claude call. Multi-segment processing is implemented but rarely used.

**[ASSUMPTION 2]:** No review gate after CHAPTERIZE. The MASTERPLAN specifies three gates: after CORRECT, ADAPT, and RENDER. Chapterization is informational.

**[ASSUMPTION 3]:** Trust LLM's duration estimates if within 20% of formula. Don't override unless validation fails.

**[ASSUMPTION 4]:** Most chapters will use `diagram` or `b_roll` visual types. `title_card` is for intros/outros, `talking_head` and `screen_share` are rare.

**[ASSUMPTION 5]:** If Pydantic validation fails, retry once with corrective prompt. If retry fails, raise ValidationError and mark pipeline run as FAILED.

**[ASSUMPTION 6]:** Chapter JSON schema v1.0 is stable for Sprints 6-10. Schema changes are not expected until after MVP.

**[ASSUMPTION 7]:** Existing prompts (correct_transcript.md, translate.md, adapt.md) follow the established YAML frontmatter + markdown template pattern. Chapterize prompt follows same pattern.

**[ASSUMPTION 8]:** Turkish word count is approximately 150 words/minute for narration. This is consistent with conversational speech rates in educational content.

**[ASSUMPTION 9]:** The dashboard chapter viewer is read-only. Editing chapters via UI is deferred to future work.

**[ASSUMPTION 10]:** IMAGE_GEN and TTS stages (Sprints 7-8) will consume the chapter JSON produced by this sprint. The schema must be stable and complete.

---

## Appendix B: Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|-----------|
| LLM produces invalid JSON | Medium | High | Retry once with corrective prompt; validation catches errors |
| Schema changes needed mid-implementation | Low | High | Design schema carefully; review with content owner before implementation |
| Duration estimates inaccurate | Low | Medium | Use conservative 150 words/min; can tune after manual review |
| Chapterization quality poor (too many/few chapters) | Medium | Medium | Prompt provides clear guidelines; can iterate on prompt after testing |
| Long scripts exceed Claude context | Low | Medium | Segmentation handles long scripts; test with longest episode |
| Dashboard viewer performance slow | Low | Low | Chapter JSON is small (~10-50KB); no performance issue expected |
| Pydantic validation too strict | Low | Medium | Add tolerance for minor variance (20% duration variance allowed) |
| Cascade invalidation not triggered | Low | High | Test cascade explicitly in integration tests |
| Provenance file corruption | Very Low | Medium | Use safe write pattern (write to temp, then rename) |

---

## Appendix C: File Paths Reference

**Input:**
- Adapted script: `data/outputs/{episode_id}/script.adapted.tr.md`

**Output:**
- Chapters JSON: `data/outputs/{episode_id}/chapters.json`
- Provenance: `data/outputs/{episode_id}/provenance/chapterize_provenance.json`

**Stale Markers:**
- Chapters stale: `data/outputs/{episode_id}/chapters.json.stale`
- IMAGE_GEN stale: `data/outputs/{episode_id}/images/.stale` (future)
- TTS stale: `data/outputs/{episode_id}/tts/.stale` (future)

---

**End of Sprint 6 Implementation Plan**
