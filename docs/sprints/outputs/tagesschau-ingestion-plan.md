# Tagesschau Ingestion & News Pipeline — Phase 2 Plan

**Date:** 2026-03-16
**Status:** Planned
**Depends on:** Phase 1 (multi-profile foundation) — complete
**Scope:** Tagesschau-specific source ingestion, story segmentation, news-adapted text flow

---

## Goal

Enable btcedu to ingest the daily 20 Uhr Tagesschau broadcast (German TV news), segment it into discrete news stories, translate faithfully into Turkish, and produce a structured chapter document suitable for video rendering — all while enforcing strict editorial neutrality, factual preservation, and source attribution.

---

## Assumptions (labeled)

1. **[SOURCE]** The Tagesschau 20 Uhr broadcast is available on YouTube via the official tagesschau channel. We reuse the existing yt-dlp download infrastructure. No scraping of tagesschau.de articles is needed for Phase 2.
2. **[FORMAT]** Source is audio extracted from video (same as Bitcoin podcast). No subtitle/teletext extraction in Phase 2.
3. **[RIGHTS]** Tagesschau content is publicly broadcast and uploaded to YouTube by ARD. We assume fair-use for educational/transformative Turkish-language news summary. Attribution is mandatory. No verbatim redistribution of German audio.
4. **[STRUCTURE]** A typical 20 Uhr Tagesschau has: greeting (~15s), 8-12 Meldungen (1-4 min each), weather forecast (~1 min), closing (~15s). Total ~15-20 minutes.
5. **[ADAPTATION]** News content is NOT culturally adapted. The `adapt` stage is skipped (already configured in `tagesschau_tr.yaml`). German institutional references are preserved with Turkish explanation parentheticals during translation, not replaced.
6. **[TONE]** Output tone is formal Turkish newsreader (haber spikeri), not informal YouTube influencer. Uses formal "siz" address but with broadcast register, not conversational.
7. **[EDITORIAL]** Zero tolerance for editorialization. Facts only. No commentary, no opinion, no spin. Uncertainty must be explicit ("kaynağa göre" / "bildirildiğine göre").

---

## 1. Source Ingestion Model

### 1.1 Episode Representation

No new model needed. A Tagesschau broadcast is an `Episode` with:

```
episode_id:       YouTube video ID (e.g., "abc123xyz")
source:           "youtube_rss"
title:            "tagesschau 20:00 Uhr, 16.03.2026"
content_profile:  "tagesschau_tr"
pipeline_version: 2
channel_id:       <tagesschau YouTube channel>
```

The existing `Episode` model (with Phase 1 `content_profile` field) is sufficient.

### 1.2 Source Configuration

Add a dedicated channel entry for Tagesschau. The operator creates it via existing `btcedu detect --channel-id <tagesschau_yt_id>` or by configuring a second RSS source.

**Config additions** (`.env` or per-profile):

| Field | Value | Notes |
|-------|-------|-------|
| `podcast_youtube_channel_id` | (tagesschau YT channel) | Per-channel, not global |
| `whisper_language` | `de` | Same as Bitcoin podcast |
| `audio_format` | `m4a` | Same |

No new config fields needed. Channel-level config already exists via `channels` table.

### 1.3 Ingestion Metadata

The existing `Episode` fields cover broadcast metadata. Additional news-specific metadata is stored in the **story manifest** (see Section 3), not on the Episode model.

### 1.4 Source Attribution

A new **attribution block** is written to the story manifest and propagated to chapter output:

```json
{
  "source": "tagesschau",
  "broadcaster": "ARD/Das Erste",
  "broadcast_date": "2026-03-16",
  "broadcast_time": "20:00 CET",
  "original_language": "de",
  "original_url": "https://www.youtube.com/watch?v=...",
  "attribution_text_tr": "Kaynak: ARD tagesschau, 16.03.2026 — Türkçe çeviri btcedu tarafından hazırlanmıştır.",
  "attribution_text_de": "Quelle: ARD tagesschau, 16.03.2026"
}
```

This is generated during the **segment** stage and embedded in the final chapter document as a mandatory intro/outro overlay.

---

## 2. Pipeline Flow for `tagesschau_tr`

### 2.1 Modified v2 Pipeline

```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review_gate_1] →
SEGMENTED → TRANSLATED → [adapt SKIPPED] → CHAPTERIZED →
IMAGES_GENERATED → TTS_DONE → RENDERED → [review_gate_3] → APPROVED → PUBLISHED
```

Key differences from `bitcoin_podcast`:

| Stage | Bitcoin Podcast | Tagesschau News |
|-------|----------------|-----------------|
| correct | Crypto domain terms | Political/institutional domain terms |
| **segment** | **N/A (no stage)** | **NEW: Story extraction from broadcast** |
| translate | Faithful DE→TR | Faithful DE→TR, formal news register |
| adapt | Cultural adaptation (T1/T2 tiers) | **SKIPPED** |
| review_gate_2 | After adaptation | **SKIPPED** (no adaptation) |
| chapterize | Topic-based chapters from adapted script | **Story-based chapters from segmented transcript** |

### 2.2 New Pipeline Status: `SEGMENTED`

Add to `EpisodeStatus` enum:

```python
SEGMENTED = "segmented"  # After story segmentation (news profiles)
```

Add to `_STATUS_ORDER`:

```python
EpisodeStatus.SEGMENTED: 10.5,  # Between CORRECTED (10) and TRANSLATED (11)
```

**Implementation note:** Since `_STATUS_ORDER` uses integers, we need to renumber. Proposed:

```python
EpisodeStatus.CORRECTED: 10,
EpisodeStatus.SEGMENTED: 11,    # NEW
EpisodeStatus.TRANSLATED: 12,   # was 11
EpisodeStatus.ADAPTED: 13,      # was 12
EpisodeStatus.CHAPTERIZED: 14,  # was 13
# ... etc, shift all subsequent by +1
```

### 2.3 Stage Routing (Profile-Aware)

The `_get_stages()` function and `_run_stage()` need profile-aware routing. Two approaches, recommend **Option B**:

**Option A — Static stage lists per profile:** Maintain `_TAGESSCHAU_STAGES` alongside `_V2_STAGES`. Simple but doesn't scale.

**Option B — Dynamic stage filtering:** `_get_stages()` loads the episode's profile, then:
1. Inserts `segment` stage if profile has `segment` in `stages_enabled` or `stage_config`
2. Removes `adapt` + `review_gate_2` if `stage_config.adapt.skip == true`
3. Adjusts required-status tuples accordingly

This is the Phase 2 skip/insert logic deferred from Phase 1.

### 2.4 Stage Skip/Insert Logic (Pseudocode)

```python
def _get_stages(settings, episode=None):
    base = _V2_STAGES if version >= 2 else _V1_STAGES
    profile = _load_profile(episode)  # from Phase 1 registry

    stages = list(base)

    # Insert segment stage for news profiles
    if profile and profile.stage_config.get("segment", {}).get("enabled"):
        # Insert after review_gate_1, before translate
        idx = next(i for i, (name, _) in enumerate(stages) if name == "translate")
        stages.insert(idx, ("segment", EpisodeStatus.CORRECTED))
        # Adjust translate to require SEGMENTED
        stages[idx + 1] = ("translate", EpisodeStatus.SEGMENTED)

    # Skip adapt + review_gate_2 if configured
    if profile and profile.stage_config.get("adapt", {}).get("skip"):
        stages = [(n, s) for n, s in stages if n not in ("adapt", "review_gate_2")]
        # Adjust chapterize to accept TRANSLATED instead of ADAPTED
        stages = [
            ("chapterize", EpisodeStatus.TRANSLATED) if n == "chapterize" else (n, s)
            for n, s in stages
        ]

    return stages
```

---

## 3. Segment Model: Story Manifest

### 3.1 Story Structure

A 20 Uhr Tagesschau broadcast is decomposed into a **StoryManifest**:

```python
@dataclass
class StoryManifest:
    episode_id: str
    broadcast_date: str          # ISO date
    source_attribution: dict     # See Section 1.4
    total_stories: int
    total_duration_seconds: int
    stories: list[Story]
    meta: dict                   # e.g., {"has_weather": true, "anchor_name": "..."}

@dataclass
class Story:
    story_id: str                # "s01", "s02", ...
    order: int                   # 1-based sequential
    headline_de: str             # German headline (extracted)
    headline_tr: str | None      # Turkish headline (filled during translation)
    category: str                # "politik", "wirtschaft", "international", ...
    story_type: str              # "meldung", "bericht", "interview", "kurzmeldung", "wetter", "intro", "outro"
    text_de: str                 # German transcript segment
    text_tr: str | None          # Turkish translation (filled later)
    word_count: int
    estimated_duration_seconds: int
    reporter: str | None         # Reporter name if mentioned
    location: str | None         # Location if mentioned
    is_lead_story: bool          # First major story
```

### 3.2 Storage

```
data/outputs/{episode_id}/
├── stories.json                 # StoryManifest (written by segment stage)
├── stories_translated.json      # StoryManifest with text_tr filled (written by translate stage)
├── provenance/
│   └── segment_provenance.json  # Hash of inputs, prompt, model
└── review/
    └── segmentation_diff.json   # Optional: flagged segmentation decisions
```

### 3.3 Story Categories

| Category | Description | Examples |
|----------|-------------|---------|
| `politik` | German domestic politics | Bundestag, elections, parties |
| `international` | Foreign affairs | EU, NATO, conflicts |
| `wirtschaft` | Economy/business | Markets, trade, employment |
| `gesellschaft` | Society/social issues | Health, education, demographics |
| `kultur` | Culture/science | Research, arts, technology |
| `sport` | Sports | Bundesliga, Olympics |
| `wetter` | Weather forecast | Always last before outro |
| `meta` | Intro/outro/transition | Greeting, closing |

### 3.4 Pydantic Schema

New file: `btcedu/models/story_schema.py`

```python
class StoryDocument(BaseModel):
    schema_version: str = "1.0"
    episode_id: str
    broadcast_date: str
    source_attribution: dict
    total_stories: int
    total_duration_seconds: int
    stories: list[Story]

class Story(BaseModel):
    story_id: str
    order: int
    headline_de: str
    category: StoryCategory  # enum
    story_type: StoryType    # enum
    text_de: str
    word_count: int
    estimated_duration_seconds: int
    reporter: str | None = None
    location: str | None = None
    is_lead_story: bool = False
```

---

## 4. New and Modified Stages

### 4.1 NEW: `btcedu/core/segmenter.py` — Story Segmentation

**Function:** `segment_broadcast(session, episode_id, settings, force=False) -> SegmentationResult`

**Flow:**
1. Load corrected German transcript (`transcript.corrected.de.txt`)
2. Load profile → verify `segment` is enabled
3. Idempotency: check `stories.json` + provenance hash
4. Call Claude with `segment_broadcast.md` prompt:
   - Input: full corrected German transcript
   - Output: JSON `StoryDocument`
5. Validate with Pydantic schema
6. Write `stories.json` + provenance
7. Update episode status → `SEGMENTED`
8. Cascade: mark translation as stale

**SegmentationResult:**
```python
@dataclass
class SegmentationResult:
    episode_id: str
    stories_path: str
    provenance_path: str
    story_count: int = 0
    total_duration_seconds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: bool = False
```

### 4.2 MODIFIED: `btcedu/core/translator.py`

For news profiles, the translator operates **per-story** rather than on the full transcript:

1. Check profile → if `segment` was enabled, load `stories.json` instead of raw transcript
2. Translate each `story.text_de` → `story.text_tr` individually
3. Translate each `story.headline_de` → `story.headline_tr`
4. Write both `transcript.tr.txt` (concatenated, for compatibility) and `stories_translated.json` (structured)
5. Use news-specific translation prompt if profile override exists (`templates/tagesschau_tr/translate.md`)

**Backward compatibility:** If no `stories.json` exists (Bitcoin podcast path), translator falls back to existing full-transcript mode. Zero changes to Bitcoin flow.

### 4.3 MODIFIED: `btcedu/core/chapterizer.py`

For news profiles with segmented stories:

1. Check profile → if stories exist, load `stories_translated.json` instead of adapted script
2. Map stories → chapters (typically 1 story = 1 chapter, but short Kurzmeldungen may be grouped)
3. Use news-specific chapterize prompt (`templates/tagesschau_tr/chapterize.md`)
4. Inject source attribution as mandatory first overlay on ch01 and last overlay on final chapter

**Backward compatibility:** If no stories file exists, chapterizer uses existing adapted-script path.

### 4.4 MODIFIED: `btcedu/core/corrector.py`

No code changes. News-specific correction behavior is achieved purely through a **profile-namespaced prompt override**:
- `templates/tagesschau_tr/correct_transcript.md` — focuses on political/institutional terms instead of crypto terms

### 4.5 MODIFIED: `btcedu/core/pipeline.py`

- Add `segment` to `_V2_STAGES` (conditionally)
- Add `SEGMENTED` handling in `_run_stage()`
- Implement profile-aware `_get_stages()` (Section 2.4)
- Add `segment` to `_V2_ONLY_STAGES` guard set
- Add `SEGMENTED` to `run_pending()` and `run_latest()` status filters

---

## 5. Prompt Templates

### 5.1 NEW: `btcedu/prompts/templates/segment_broadcast.md`

**Purpose:** Extract discrete news stories from a full Tagesschau transcript.

```yaml
---
name: segment_broadcast
model: claude-sonnet-4-20250514
temperature: 0.1
max_tokens: 16384
---
```

**Prompt design principles:**
- Very low temperature (0.1) — factual extraction, not creative
- JSON output mode
- Identifies story boundaries by topic shifts, anchor transitions ("Und nun zu..."), reporter handoffs
- Extracts: headline, category, story type, reporter name, location
- Preserves exact transcript text per story (no summarization)
- Marks intro/outro as `meta` type
- Flags weather as `wetter` type
- Identifies lead story (`is_lead_story: true`)

**Key rules in prompt:**
1. **DO NOT summarize or paraphrase** — copy exact transcript text into each story
2. **DO NOT merge unrelated stories** — each discrete topic is a separate story
3. **DO NOT invent headlines** — derive from anchor's introduction of the topic
4. **Kurzmeldungen** (brief items read in sequence) may be grouped into one story if <30 seconds each
5. **Reporter segments** — include the reporter's full text within the parent story
6. **Category assignment** — use provided category taxonomy, default to `gesellschaft` if ambiguous

### 5.2 NEW: `btcedu/prompts/templates/tagesschau_tr/correct_transcript.md`

Profile-namespaced override of the correction prompt. Differences from base:
- Domain terms: political parties (CDU, SPD, Grüne, AfD), institutions (Bundestag, Bundesrat, EU-Kommission), places (Berlin, Brüssel)
- Removes crypto-specific term list
- Adds news-specific ASR error patterns: proper nouns of politicians, city names, parliamentary terms
- Same structure (YAML frontmatter + body), same model/temperature

### 5.3 NEW: `btcedu/prompts/templates/tagesschau_tr/translate.md`

Profile-namespaced translation prompt. Differences from base:
- **Register:** Formal news broadcast Turkish (haber dili), not conversational
- **Institutional terms:** Preserve German institution names with Turkish explanation in parentheses
  - "Bundestag" → "Bundestag (Almanya Federal Meclisi)"
  - "Bundesrat" → "Bundesrat (Almanya Federal Konseyi)"
  - Not replaced (unlike Bitcoin podcast adaptation)
- **Political neutrality:** Explicit instruction to preserve neutral reporting tone
- **No opinion markers:** Never add editorial qualifiers ("maalesef", "ne yazık ki", etc.)
- **Attribution language:** Preserve sourcing ("bildirildiğine göre", "kaynağa göre")
- **Numbers/statistics:** Preserve exactly, convert units only when unambiguous (°C stays °C, km stays km)
- **Names:** All proper nouns unchanged, add pronunciation hint only for very unusual names

### 5.4 NEW: `btcedu/prompts/templates/tagesschau_tr/chapterize.md`

Profile-namespaced chapterize prompt. Differences from base:
- **1 story ≈ 1 chapter** mapping (not topic-based grouping)
- **Mandatory attribution overlay** on first and last chapter
- **Visual types:** Prefer `b_roll` with news-appropriate image prompts (no Bitcoin branding)
- **Tone:** Narration text is the translated story text (no rewriting)
- **Duration:** Story duration from manifest, not re-estimated
- **Weather chapter:** May use `diagram` visual type for weather graphics
- **No entertainment overlays:** No quotes or statistics overlays unless directly from source

### 5.5 NEW: `btcedu/prompts/templates/tagesschau_tr/system.md`

Profile-namespaced system prompt. Replaces Bitcoin-specific system prompt:
- **Domain:** News / current affairs (not cryptocurrency)
- **Source:** ARD tagesschau (not Der Bitcoin Podcast)
- **Hard rules:**
  1. USE ONLY PROVIDED SOURCES (same)
  2. NO FABRICATION (same)
  3. NO EDITORIAL OPINION (stricter than Bitcoin version)
  4. NO POLITICAL COMMENTARY beyond what source states
  5. FACTUAL PRESERVATION — every claim must be traceable to source transcript
  6. ATTRIBUTION — use hedging language for unverified claims
  7. LANGUAGE: Turkish, formal news register
  8. NO FINANCIAL ADVICE (same, relevant for Wirtschaft stories)
  9. MANDATORY DISCLAIMER: "Bu içerik ARD tagesschau yayınından Türkçe'ye çevrilmiştir. Orijinal kaynak: tagesschau.de"

---

## 6. Review Implications

### 6.1 Review Gate 1 (Post-Correction)

**Stricter for news.** Correction errors in news content can change meaning of political statements.

- Existing `review_gate_1` applies (no code change)
- The news-specific correction prompt produces fewer false corrections (lower temperature, narrower domain)
- Reviewer focus: verify proper nouns, political party names, place names are correct

### 6.2 Review Gate 2 (Post-Adaptation)

**Skipped.** No adaptation stage for news.

### 6.3 NEW: Review Gate for Segmentation (Optional, Phase 2+)

Not implemented in Phase 2, but designed for:
- Verify story boundaries are correct
- Verify headlines accurately represent content
- Verify category assignments
- Flag if stories were incorrectly merged/split

**Recommendation:** Add as `review_gate_segment` in a future phase. For Phase 2, segmentation is auto-approved (low risk — it's structural, not content-altering).

### 6.4 Review Gate 3 (Post-Render)

**Same as Bitcoin podcast.** Human reviews final video before publish.

Additional news-specific review checklist (enforced in prompt/UI, not code):
- [ ] Attribution overlay visible in intro
- [ ] No editorialization in narration
- [ ] Proper nouns correctly pronounced in TTS
- [ ] Story order matches broadcast order
- [ ] No stories omitted without justification

### 6.5 Editorial Review Policy

Not a code change — a **documentation artifact** for operators:

New file: `docs/runbooks/news-editorial-policy.md`

Contents:
- Factual accuracy standards
- Political neutrality checklist
- Attribution requirements
- When to reject (editorialization, missing attribution, factual error)
- When to request changes (minor tone issues, pronunciation)
- Escalation path (if source material itself is disputed/retracted)

---

## 7. Storage & Provenance

### 7.1 File Layout (per episode)

```
data/
├── raw/{episode_id}/
│   └── audio.m4a                           # Downloaded audio
├── transcripts/{episode_id}/
│   ├── transcript.de.txt                    # Raw Whisper output
│   ├── transcript.clean.de.txt              # Cleaned
│   ├── transcript.corrected.de.txt          # ASR-corrected German
│   └── transcript.tr.txt                    # Full Turkish translation
└── outputs/{episode_id}/
    ├── stories.json                         # StoryManifest (German)
    ├── stories_translated.json              # StoryManifest (with Turkish)
    ├── chapters.json                        # ChapterDocument (production-ready)
    ├── provenance/
    │   ├── correct_provenance.json
    │   ├── segment_provenance.json          # NEW
    │   ├── translate_provenance.json
    │   └── chapterize_provenance.json
    └── review/
        ├── correction_diff.json
        └── segmentation_diff.json           # NEW (optional)
```

### 7.2 Provenance Records

Each stage writes a provenance JSON with:
- Input file hash (SHA-256)
- Prompt template hash
- Model name + parameters
- Timestamp
- Token counts + cost

The `segment_provenance.json` additionally records:
- `story_count`: number of stories extracted
- `broadcast_date`: extracted date
- `source_attribution`: full attribution block

### 7.3 Audit Trail

All provenance files + `ContentArtifact` DB records provide a complete audit trail:
- **What** was generated (artifact type, file path)
- **From what** (input hash)
- **By what** (prompt hash, model)
- **When** (timestamp)
- **At what cost** (token counts, USD estimate)

For news content, the audit trail is especially important because:
- Factual claims must be traceable to source transcript
- Translation fidelity must be verifiable
- No hallucinated content should exist in the chain

### 7.4 DB Records

No new tables. Existing models cover all needs:
- `Episode` — with `content_profile="tagesschau_tr"`
- `PipelineRun` — with new `PipelineStage.SEGMENT` enum value
- `ContentArtifact` — for `stories.json`, `stories_translated.json`
- `ReviewTask` — for review gates (unchanged)

---

## 8. Legal & Editorial Guardrails

### 8.1 Source Rights

- **Assumption:** Tagesschau content is publicly broadcast and uploaded to YouTube by ARD (publicly funded broadcaster)
- **Mitigation:** We do NOT redistribute German audio. We produce a **transformative Turkish-language derivative** (translated, restructured for video format)
- **Attribution:** Mandatory in every output (intro overlay, description, metadata)
- **Takedown compliance:** If ARD requests removal, the `content_profile` allows bulk identification and removal of all tagesschau-derived episodes

### 8.2 Non-Hallucination Requirements

Enforced at multiple levels:

| Level | Mechanism |
|-------|-----------|
| **System prompt** | "USE ONLY PROVIDED SOURCES" |
| **Segment prompt** | "DO NOT summarize — copy exact text" |
| **Translate prompt** | "Faithful translation — no additions" |
| **Chapterize prompt** | "All narration MUST come from translated text" |
| **Review gate** | Human verifies no invented content |
| **Provenance** | Input hash proves output derived from source |

### 8.3 Uncertainty Handling

When source material is incomplete or ambiguous:

- **Correction stage:** If a word is unclear in transcription, preserve as-is (don't guess)
- **Segmentation stage:** If story boundaries are ambiguous, prefer over-splitting (more stories, each clearly delineated) over under-splitting
- **Translation stage:** If a German phrase has no direct Turkish equivalent, provide literal translation with German original in parentheses
- **Chapterization stage:** If a story is very short (<20 seconds), it may be grouped with adjacent Kurzmeldungen — flagged in notes field

### 8.4 Content Filtering

No content filtering of news topics. Tagesschau covers:
- War/conflict (factual reporting)
- Political controversy (neutral reporting)
- Crime (factual reporting)
- Sensitive social issues

The system does NOT editorialize, censor, or selectively omit topics. If a story is in the broadcast, it appears in the output. The only exception: the operator may manually reject stories during review.

---

## 9. Implementation: Exact File Changes

### New Files (7)

| # | File | Purpose |
|---|------|---------|
| 1 | `btcedu/models/story_schema.py` | Pydantic `StoryDocument` + `Story` schema |
| 2 | `btcedu/core/segmenter.py` | `segment_broadcast()` stage module |
| 3 | `btcedu/prompts/templates/segment_broadcast.md` | Story segmentation prompt |
| 4 | `btcedu/prompts/templates/tagesschau_tr/system.md` | News system prompt |
| 5 | `btcedu/prompts/templates/tagesschau_tr/correct_transcript.md` | News correction prompt |
| 6 | `btcedu/prompts/templates/tagesschau_tr/translate.md` | News translation prompt |
| 7 | `btcedu/prompts/templates/tagesschau_tr/chapterize.md` | News chapterization prompt |
| 8 | `docs/runbooks/news-editorial-policy.md` | Operator editorial review guide |
| 9 | `tests/test_segmenter.py` | Segmenter unit tests |
| 10 | `tests/test_tagesschau_flow.py` | Integration tests for news pipeline flow |

### Modified Files (8)

| # | File | Change |
|---|------|--------|
| 1 | `btcedu/models/episode.py` | Add `SEGMENTED` to `EpisodeStatus`, `SEGMENT` to `PipelineStage` |
| 2 | `btcedu/core/pipeline.py` | Profile-aware `_get_stages()`, `segment` in `_run_stage()`, `SEGMENTED` in status filters |
| 3 | `btcedu/core/translator.py` | Per-story translation mode when `stories.json` exists, profile-namespaced prompt |
| 4 | `btcedu/core/chapterizer.py` | Story-to-chapter mapping when `stories_translated.json` exists, profile-namespaced prompt |
| 5 | `btcedu/core/corrector.py` | Pass profile name to `PromptRegistry.load_template()` for namespaced resolution |
| 6 | `btcedu/profiles/tagesschau_tr.yaml` | Add `segment: {enabled: true}` to `stage_config` |
| 7 | `btcedu/web/api.py` | Expose stories in episode detail, segment stage in progress |
| 8 | `btcedu/cli.py` | Add `btcedu segment --episode-id` command |

### Migration

| # | Migration | Change |
|---|-----------|--------|
| 1 | `009_add_segmented_status` | No DDL needed — `EpisodeStatus` is a Python enum stored as string. Just add to enum. But `PipelineStage` needs `SEGMENT` value for `PipelineRun` records. Enum is also Python-side. **No migration needed** — SQLAlchemy stores enum values as strings in SQLite. |

---

## 10. Profile Config Update

```yaml
# btcedu/profiles/tagesschau_tr.yaml
name: tagesschau_tr
display_name: "Tagesschau News (DE→TR)"
source_language: de
target_language: tr
domain: news
pipeline_version: 2
stages_enabled: all
stage_config:
  segment:
    enabled: true           # NEW: enables story segmentation
  adapt:
    skip: true              # news: no cultural adaptation
  translate:
    mode: per_story         # NEW: translate per-story from manifest
    register: formal_news   # NEW: hint for prompt selection
youtube:
  category_id: "25"         # News & Politics
  default_language: tr
  default_privacy: unlisted
  tags: [haberler, almanya, türkçe, tagesschau]
review_gates: {}
prompt_namespace: tagesschau_tr  # NEW: explicit prompt directory
```

---

## 11. Definition of Done

1. **All existing tests pass** (890+ baseline, zero regressions)
2. **New tests pass:**
   - `test_segmenter.py`: StoryDocument validation, segmentation with mocked Claude, idempotency, provenance
   - `test_tagesschau_flow.py`: Full pipeline flow with segment→translate→chapterize, adapt skipped, correct profile routing
   - Profile-aware `_get_stages()` returns correct stage list for each profile
   - Translator per-story mode produces both `transcript.tr.txt` and `stories_translated.json`
   - Chapterizer story-to-chapter mapping produces valid `ChapterDocument`
   - Profile-namespaced prompt templates resolve correctly
3. **CLI works:**
   - `btcedu segment --episode-id EP` processes a corrected news episode
   - `btcedu profile show tagesschau_tr` shows updated config with segment enabled
4. **Web API:**
   - Episode detail includes stories count for segmented episodes
   - Stage progress shows segment stage for news episodes
5. **Prompts:**
   - All 4 profile-namespaced prompts (`system`, `correct_transcript`, `translate`, `chapterize`) exist and parse correctly
   - `segment_broadcast.md` exists and parses correctly
6. **Attribution:**
   - `stories.json` includes `source_attribution` block
   - `chapters.json` includes attribution overlay on first/last chapter
7. **Editorial policy:**
   - `docs/runbooks/news-editorial-policy.md` exists

---

## 12. Non-Goals (Phase 2)

- **No tagesschau.de article scraping** — audio-only via YouTube
- **No subtitle/teletext extraction** — Whisper transcription only
- **No automatic weather graphics** — weather is a story like any other
- **No live/breaking news handling** — daily 20 Uhr only
- **No multi-language news** (English, French) — German source only
- **No automatic story deduplication** across broadcasts
- **No Tagesschau-specific RSS feed discovery** — operator configures channel manually
- **No review gate after segmentation** — deferred to Phase 2+
- **No TTS voice differentiation** (anchor vs reporter) — single voice for Phase 2
- **No per-story image style** — same visual pipeline as Bitcoin podcast
- **No dashboard UI for story editing** — review via existing artifact viewer

---

## 13. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Whisper struggles with multiple speakers (anchor + reporter) | Medium | Medium | Correction stage fixes; future: speaker diarization |
| Story boundaries ambiguous (smooth transitions) | Medium | Low | Over-split preference; human review catches errors |
| ARD objects to derivative use | Low | High | Attribution, transformative use, immediate takedown capability |
| News accuracy error in translation | Medium | High | Strict prompts, review gate 3, editorial policy |
| `_STATUS_ORDER` renumbering breaks existing episodes | Low | High | Migration backfills; existing episodes have statuses < CORRECTED so unaffected |
| Per-story translation increases API cost | Medium | Low | Stories are shorter → fewer tokens per call; total similar |

---

## 14. Sequencing Recommendation

Implement in this order to maintain a green test suite throughout:

1. **Models first:** `story_schema.py`, `SEGMENTED` status, `SEGMENT` stage enum
2. **Profile config:** Update `tagesschau_tr.yaml` with segment config
3. **Prompts:** All 5 new prompt templates (can be written/tested independently)
4. **Segmenter:** `core/segmenter.py` + tests (standalone, no pipeline integration yet)
5. **Pipeline wiring:** Profile-aware `_get_stages()`, skip logic, `_run_stage()` for segment
6. **Translator modification:** Per-story mode + profile prompt resolution
7. **Chapterizer modification:** Story-to-chapter mapping + profile prompt resolution
8. **Corrector modification:** Profile prompt resolution (smallest change)
9. **CLI + Web:** `btcedu segment` command, API exposure
10. **Integration tests:** Full flow test
11. **Editorial policy doc**
