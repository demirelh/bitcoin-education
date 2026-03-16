# Tagesschau Render, Visual Packaging & Operator Switching — Phase 4 Plan

**Date:** 2026-03-16
**Status:** Planned
**Depends on:** Phase 1 (multi-profile), Phase 2 (tagesschau ingestion), Phase 3 (editorial review)
**Scope:** Profile-aware rendering, visual packaging, operator UX, smoke tests, production readiness

---

## Problem Statement

Phases 1-3 built profile-aware pipeline routing, story segmentation, translation, and review. But the **downstream stages** (stock images, TTS, rendering, publishing) remain hardcoded to Bitcoin/crypto assumptions:

- Stock image search uses a 100+ term Bitcoin/finance Turkish→English dictionary and Bitcoin-specific intent extraction prompts
- YouTube publisher hardcodes `#Bitcoin #Kripto` tags and category 27 (Education)
- Overlay colors use Bitcoin orange (#F7931A) for statistics
- There is no way for an operator to detect, filter, or process episodes by profile from the CLI
- The dashboard shows no profile indicator on episodes

Phase 4 closes these gaps with profile-aware visual packaging, publisher metadata, operator switching, and cross-profile smoke tests.

---

## Assumptions (labeled)

1. **[REUSE]** The renderer, TTS engine, and ffmpeg pipeline are structurally profile-agnostic. Only their *inputs* (prompts, config, overlays) need parameterization — no architectural rewrite.
2. **[TTS-VOICE]** A Turkish news-anchor ElevenLabs voice can be configured via profile-level `stage_config.tts.voice_id`. If unset, falls back to global `settings.elevenlabs_voice_id`. No multi-speaker (anchor + reporter) in Phase 4.
3. **[STOCK-FALLBACK]** The `_TR_TO_EN` vocabulary fallback in stock_images.py is used when intent analysis is skipped. For news profiles, intent analysis should always run — but the fallback dict needs news terms for robustness.
4. **[YOUTUBE-MULTI]** A single YouTube OAuth credential is used for all profiles. Content for different profiles goes to the same YouTube channel (different playlists possible but out of scope).
5. **[SYSTEMD]** Existing timers process all profiles in a single pass. No separate per-profile timers. Operators filter via CLI flags for manual runs.
6. **[CHANNEL-PROFILE]** Automatic channel→profile mapping is deferred. Phase 4 adds `--profile` to `btcedu detect` for manual profile assignment. A channel-profile mapping table is a future enhancement.

---

## 1. News-Specific Visual Packaging

### 1.1 Stock Image Search: Profile-Aware Intent Extraction

**Current state:** `extract_chapter_intents()` in `stock_images.py` uses a hardcoded system prompt mentioning "Bitcoin and cryptocurrency educational YouTube channel, Turkish audience."

**Change:** Load system prompt from profile-namespaced template.

New template: `btcedu/prompts/templates/tagesschau_tr/intent_extract.md`

Content: Replace Bitcoin/crypto domain context with news context:
- "Professional Turkish news video channel covering German/international news"
- Allowed motifs: government buildings, parliaments, EU institutions, press conferences, maps, city skylines, emergency services, weather graphics
- Disallowed motifs: cryptocurrency imagery, trading charts, blockchain diagrams
- Literal traps: "Bundestag" (not a random building), "Grüne" (political party, not color), "Bank" (financial institution, not river bank or bench)

**Code change in `stock_images.py`:** In `extract_chapter_intents()`, resolve the intent prompt via `PromptRegistry.resolve_template_path("intent_extract.md", profile=content_profile)` instead of loading the base template directly.

### 1.2 Stock Image Search: Profile-Aware Ranking

Same pattern: `rank_candidates()` loads `stock_rank.md` prompt. Add profile-namespaced override.

New template: `btcedu/prompts/templates/tagesschau_tr/stock_rank.md`

Content: Ranking criteria for news imagery:
- Semantic fit: Does image match the **news topic** (politics, economy, international)?
- Appropriateness: Is the image suitable for a **news broadcast** (not entertainment, not sensational)?
- Composition: Clean, professional, suitable as video background with text overlays
- Avoid: Cryptocurrency imagery, financial chart graphics, cartoon illustrations, stock photos of generic "business people"

### 1.3 Stock Image Search: News Domain Vocabulary

The `_TR_TO_EN` fallback dictionary is Bitcoin/finance-specific. Add a news-specific vocabulary supplement.

**Approach:** Instead of making the dict profile-aware (complex, brittle), extend the existing dict with news/political terms. Both profiles benefit from a larger vocabulary. This is additive and safe.

New terms to add to `_TR_TO_EN`:

```python
# Politics & Government
"meclis": "parliament", "hükümet": "government", "başbakan": "prime minister",
"cumhurbaşkanı": "president", "bakan": "minister", "seçim": "election",
"oy": "vote", "parti": "political party", "koalisyon": "coalition",
"muhalefet": "opposition", "yasa": "law", "anayasa": "constitution",

# International
"savaş": "war", "barış": "peace", "mülteci": "refugee", "göç": "migration",
"diplomatik": "diplomatic", "NATO": "NATO", "BM": "united nations",

# Society & Infrastructure
"hastane": "hospital", "okul": "school", "eğitim": "education",
"ulaşım": "transportation", "trafik": "traffic", "çevre": "environment",
"iklim": "climate", "deprem": "earthquake", "sel": "flood",

# Weather
"hava": "weather", "yağmur": "rain", "fırtına": "storm", "sıcaklık": "temperature",
"kar": "snow", "güneş": "sunshine", "bulut": "cloud",
```

Also: change the hardcoded domain tag from `"finance"` to a profile-derived domain. Use `profile.domain` if available (e.g., "news" for tagesschau, "cryptocurrency" for bitcoin_podcast), fall back to `"finance"` for backward compatibility.

### 1.4 Overlay Styling

**Current state:** `OVERLAY_STYLES` in renderer.py uses Bitcoin orange (#F7931A) for `statistic` overlays.

**Change:** Make statistic overlay color profile-aware.

Add to profile YAML:

```yaml
# bitcoin_podcast.yaml
render:
  accent_color: "#F7931A"  # Bitcoin orange

# tagesschau_tr.yaml
render:
  accent_color: "#004B87"  # ARD/tagesschau blue
```

In renderer.py, when building overlay specs, load accent color from profile `stage_config.render.accent_color`, fall back to `"#F7931A"`.

This is a minimal, targeted change — only the statistic overlay color changes. All other overlay styling (font size, position) remains fixed.

---

## 2. News-Specific Render Mode

### 2.1 Render Presets

No change needed. The renderer is codec/resolution-agnostic already. Settings (`render_resolution`, `render_crf`, `render_preset`) apply to all profiles equally. The Raspberry Pi's `render_preset=ultrafast` is a hardware constraint, not a profile constraint.

### 2.2 TTS Voice: Profile-Level Override

**Current state:** Single global `settings.elevenlabs_voice_id` used for all episodes.

**Change:** Allow per-profile voice ID override.

Add to profile YAML:

```yaml
# tagesschau_tr.yaml
stage_config:
  tts:
    voice_id: ""  # Empty = use settings default; set to override
    stability: 0.6  # Slightly higher for news anchor consistency
    style: 0.0
```

In `tts.py`, when building `TTSRequest`, check profile's `stage_config.tts` for overrides:

```python
# Load profile-specific TTS config
profile = _load_profile_for_episode(session, episode, settings)
tts_config = profile.stage_config.get("tts", {}) if profile else {}

voice_id = tts_config.get("voice_id") or settings.elevenlabs_voice_id
stability = tts_config.get("stability", settings.elevenlabs_stability)
style = tts_config.get("style", settings.elevenlabs_style)
```

**[ASSUMPTION]** No multi-speaker TTS (anchor voice vs. reporter voice). Single voice per episode. Reporter segments are narrated in the same voice. Multi-speaker is a future enhancement.

### 2.3 Intro/Outro Cards

**Current state:** Title cards are generated as placeholders by `finalize_selections()` — orange (#F7931A) or gray background with chapter title.

**Change for news:** The chapterize prompt for tagesschau already mandates:
- First chapter: `title_card` with broadcast name + date
- Last chapter: `title_card` with closing
- Attribution overlay on first and last chapters

No renderer change needed — the chapterizer and overlay system already handle this. The only change is the accent color (Section 1.4).

---

## 3. News-Specific YouTube Publishing

### 3.1 Profile-Aware YouTube Metadata

**Current state in `publisher.py`:**
- `_BASE_TAGS = ["Bitcoin", "Kripto", "Blockchain", "Türkçe", "Eğitim", "Cryptocurrency"]`
- Hashtags: `#Bitcoin #Kripto #Türkçe #Eğitim #Blockchain`
- Category: `settings.youtube_category_id` (default "27")
- Language: `settings.youtube_default_language` (default "tr")

**Change:** Load tags, category, language from profile YAML.

```python
def _build_youtube_metadata(episode, chapters_doc, tts_manifest, settings):
    profile = _load_profile_for_episode(session, episode, settings)
    yt_config = profile.youtube if profile else {}

    category_id = yt_config.get("category_id", settings.youtube_category_id)
    language = yt_config.get("default_language", settings.youtube_default_language)
    privacy = yt_config.get("default_privacy", settings.youtube_default_privacy)
    profile_tags = yt_config.get("tags", [])

    # Use profile tags or fall back to base tags
    if profile_tags:
        base_tags = profile_tags
        hashtags = " ".join(f"#{t.replace(' ', '')}" for t in profile_tags[:5])
    else:
        base_tags = _BASE_TAGS
        hashtags = "#Bitcoin #Kripto #Türkçe #Eğitim #Blockchain"
    ...
```

For tagesschau, this produces:
- Tags: `["haberler", "almanya", "türkçe", "tagesschau"]`
- Hashtags: `#haberler #almanya #türkçe #tagesschau`
- Category: 25 (News & Politics)

### 3.2 Attribution in Description

For news profiles, prepend source attribution to the video description:

```python
if profile and profile.domain == "news":
    attribution = (
        "Kaynak: ARD tagesschau — Türkçe çeviri btcedu tarafından hazırlanmıştır.\n"
        "Source: ARD tagesschau — Turkish translation by btcedu.\n\n"
    )
    description = attribution + description
```

---

## 4. Operator Switching UX

### 4.1 CLI: `--profile` Flag

Add `--profile` option to these commands:

| Command | Effect |
|---------|--------|
| `btcedu detect --profile tagesschau_tr` | Sets `content_profile` on newly detected episodes |
| `btcedu run-pending --profile tagesschau_tr` | Only processes episodes with matching profile |
| `btcedu run-latest --profile tagesschau_tr` | Only detects+processes episodes with matching profile |
| `btcedu run --profile tagesschau_tr` | Only processes matching episodes |

**Implementation:**

In `detect`:
```python
@click.option("--profile", default=None, help="Content profile to assign to new episodes.")
```
When creating Episode records, set `content_profile = profile or settings.default_content_profile`.

In `run-pending` / `run-latest`:
```python
@click.option("--profile", default=None, help="Only process episodes with this content profile.")
```
Pass to `run_pending()` / `run_latest()` as filter.

### 4.2 Pipeline Functions: Profile Filter

**`run_pending()`** — Add optional `profile` parameter:

```python
def run_pending(session, settings, max_episodes=None, since=None, profile=None):
    query = session.query(Episode).filter(...)
    if profile:
        query = query.filter(Episode.content_profile == profile)
    ...
```

**`run_latest()`** — Same pattern:

```python
def run_latest(session, settings, profile=None):
    ...
    candidates = session.query(Episode).filter(...)
    if profile:
        candidates = candidates.filter(Episode.content_profile == profile)
    ...
```

### 4.3 Dashboard: Profile Indicators

**Episode list:** Show profile badge next to each episode title.

In `_episode_to_dict()`, the `content_profile` field is already included (Phase 1). The frontend (`app.js`) needs to display it.

Add to episode row rendering in `app.js`:
```javascript
const profileBadge = ep.content_profile !== "bitcoin_podcast"
    ? `<span class="badge badge-profile">${ep.content_profile}</span>`
    : "";
```

This shows a badge only for non-default profiles (avoids visual noise for the common case).

**Profile filter dropdown** in the episode list header:
```javascript
// Add after existing channel filter
<select id="profile-filter" onchange="filterByProfile(this.value)">
    <option value="">All Profiles</option>
    <option value="bitcoin_podcast">Bitcoin Podcast</option>
    <option value="tagesschau_tr">Tagesschau</option>
</select>
```

Populate dynamically from `GET /api/profiles`. Filter calls existing `GET /api/episodes?profile=X`.

**Review pages:** Show profile name in review detail header.

In `get_review_detail()` response (already includes episode data), the frontend can display the profile. The news checklist (Phase 3) already provides profile-specific context.

### 4.4 Batch API: Profile Filter

Add optional `profile` field to `POST /api/batch/start`:

```json
{"force": false, "channel_id": null, "profile": "tagesschau_tr"}
```

Pass to `run_pending()` as filter.

---

## 5. Smoke Test Strategy

### 5.1 `btcedu smoke-test-pipeline` Command

New CLI command that validates a profile's pipeline configuration end-to-end in dry-run mode:

```bash
btcedu smoke-test-pipeline --profile bitcoin_podcast
btcedu smoke-test-pipeline --profile tagesschau_tr
```

**Flow:**
1. Load profile from registry; validate YAML parse + Pydantic
2. Compute `_get_stages()` for a dummy episode with this profile
3. Print stage list with required statuses
4. Verify all profile-namespaced prompt templates exist and parse (YAML frontmatter valid)
5. Verify stock image vocabulary has terms for the profile's domain
6. Print YouTube metadata that would be generated (tags, category, language)
7. Print TTS config (voice_id, stability, style)
8. Report: PASS / FAIL with details

This is a **config validation smoke test** — no DB, no API calls, no ffmpeg. Takes <1 second.

### 5.2 `btcedu smoke-test-video` Enhancement

The existing `smoke-test-video` tests ffmpeg rendering. Extend with optional `--profile` to verify profile-specific overlay styling (accent color).

Minimal change: pass accent color to the test segment render.

### 5.3 Automated Cross-Profile Test

New test file: `tests/test_cross_profile.py`

Tests:
```python
def test_bitcoin_and_tagesschau_stages_are_different():
    """Verify the two profiles produce distinct stage lists."""
    # Bitcoin: has adapt, review_gate_2, no segment
    # Tagesschau: has segment, review_gate_translate, no adapt

def test_both_profiles_have_valid_prompts():
    """Both profiles' prompt templates exist and parse."""

def test_profile_isolation():
    """Processing one profile's episode doesn't affect another's state."""
    # Create one bitcoin episode and one tagesschau episode
    # Run pipeline on bitcoin → verify tagesschau unaffected
    # Run pipeline on tagesschau → verify bitcoin unaffected

def test_youtube_metadata_differs_by_profile():
    """Publisher produces correct tags/category per profile."""

def test_stock_domain_tag_differs_by_profile():
    """Stock search uses profile.domain, not hardcoded "finance"."""
```

### 5.4 CI Integration (Future)

Not implemented in Phase 4, but designed for:
```bash
# In CI/CD or deploy check:
btcedu smoke-test-pipeline --profile bitcoin_podcast --exit-code
btcedu smoke-test-pipeline --profile tagesschau_tr --exit-code
pytest tests/test_cross_profile.py -x -q
```

---

## 6. Exact File Changes

### New Files (6)

| # | File | Purpose |
|---|------|---------|
| 1 | `btcedu/prompts/templates/tagesschau_tr/intent_extract.md` | News-specific intent extraction prompt |
| 2 | `btcedu/prompts/templates/tagesschau_tr/stock_rank.md` | News-specific stock ranking prompt |
| 3 | `tests/test_cross_profile.py` | Cross-profile isolation + metadata tests |
| 4 | `docs/runbooks/profile-switching.md` | Operator guide for switching between profiles |

### Modified Files (10)

| # | File | Change |
|---|------|--------|
| 1 | `btcedu/core/stock_images.py` | Profile-namespaced intent/rank prompt resolution; news terms in `_TR_TO_EN`; domain tag from `profile.domain` instead of hardcoded `"finance"` |
| 2 | `btcedu/core/renderer.py` | Load accent color from profile `stage_config.render.accent_color`; fall back to `"#F7931A"` |
| 3 | `btcedu/core/tts.py` | Load `voice_id`, `stability`, `style` from profile `stage_config.tts`; fall back to settings |
| 4 | `btcedu/core/publisher.py` | Load tags, category, language, privacy from `profile.youtube`; attribution prefix for news domain; hashtags from profile tags |
| 5 | `btcedu/core/pipeline.py` | `run_pending()` and `run_latest()` accept optional `profile` filter |
| 6 | `btcedu/cli.py` | `--profile` on `detect`, `run`, `run-pending`, `run-latest`; `btcedu smoke-test-pipeline --profile` command |
| 7 | `btcedu/web/api.py` | `profile` field in batch start; profile filter in batch processing |
| 8 | `btcedu/web/static/app.js` | Profile badge on episodes; profile filter dropdown |
| 9 | `btcedu/profiles/bitcoin_podcast.yaml` | Add `render.accent_color: "#F7931A"` |
| 10 | `btcedu/profiles/tagesschau_tr.yaml` | Add `render.accent_color: "#004B87"`, `tts.voice_id: ""`, `tts.stability: 0.6` |

---

## 7. Profile Config After Phase 4

### bitcoin_podcast.yaml (final)

```yaml
name: bitcoin_podcast
display_name: "Bitcoin Podcast (DE→TR)"
source_language: de
target_language: tr
domain: cryptocurrency
pipeline_version: 2
stages_enabled: all
stage_config:
  adapt:
    skip: false
    tiers: [cultural, technical]
  render:
    accent_color: "#F7931A"
youtube:
  category_id: "27"
  default_language: tr
  default_privacy: unlisted
  tags: [bitcoin, kripto, blockchain, türkçe, eğitim, cryptocurrency]
review_gates:
  review_gate_1:
    auto_approve_threshold: 5
```

### tagesschau_tr.yaml (final)

```yaml
name: tagesschau_tr
display_name: "Tagesschau News (DE→TR)"
source_language: de
target_language: tr
domain: news
pipeline_version: 2
stages_enabled: all
stage_config:
  segment:
    enabled: true
  adapt:
    skip: true
  translate:
    mode: per_story
    register: formal_news
  tts:
    voice_id: ""
    stability: 0.6
    style: 0.0
  render:
    accent_color: "#004B87"
youtube:
  category_id: "25"
  default_language: tr
  default_privacy: unlisted
  tags: [haberler, almanya, türkçe, tagesschau]
review_gates: {}
prompt_namespace: tagesschau_tr
```

---

## 8. Production Readiness Checklist

Before Tagesschau mode is used operationally, all of the following must be verified:

| # | Item | How to verify |
|---|------|---------------|
| 1 | Tagesschau YouTube channel ID known | Operator has ARD tagesschau YT channel ID |
| 2 | Channel created in DB | `btcedu detect --profile tagesschau_tr` with correct RSS/channel config |
| 3 | Pipeline version = 2 | `settings.pipeline_version = 2` in `.env` |
| 4 | ElevenLabs voice suitable for news | Test TTS with a sample news text; configure `stage_config.tts.voice_id` if different from podcast voice |
| 5 | Prompt templates pass smoke test | `btcedu smoke-test-pipeline --profile tagesschau_tr` → PASS |
| 6 | Review workflow tested | Process one real episode through full pipeline; approve at each review gate |
| 7 | News editorial policy reviewed | Operator has read `docs/runbooks/news-editorial-policy.md` |
| 8 | YouTube auth configured | `btcedu youtube-auth` completed; credentials at `data/.youtube_credentials.json` |
| 9 | Cross-profile tests pass | `pytest tests/test_cross_profile.py -x -q` → PASS |
| 10 | Full test suite green | `pytest -x -q` → all pass |
| 11 | Profile switching doc reviewed | Operator has read `docs/runbooks/profile-switching.md` |

---

## 9. Rollout Strategy

### Phase 4a (implement, ~1 session)
1. New prompt templates (intent_extract, stock_rank for tagesschau)
2. Profile-aware stock_images.py (prompt resolution, domain tag, vocabulary)
3. Profile-aware tts.py (voice_id, stability from stage_config)
4. Profile-aware renderer.py (accent color from stage_config)
5. Profile-aware publisher.py (tags, category, attribution from profile.youtube)
6. Profile YAML updates

### Phase 4b (operator UX, ~1 session)
1. CLI `--profile` on detect/run/run-pending/run-latest
2. Pipeline filter functions
3. `smoke-test-pipeline` command
4. Dashboard profile badge + filter
5. Batch API profile filter

### Phase 4c (testing + docs, ~1 session)
1. `test_cross_profile.py` tests
2. Profile-switching runbook
3. Production readiness verification
4. Update CLAUDE.md with multi-profile operations

---

## 10. Definition of Done

1. **All existing tests pass** (944+ baseline, zero regressions)
2. **New tests pass** (~8-12 tests in `test_cross_profile.py`)
3. **Smoke test works**: `btcedu smoke-test-pipeline --profile X` → PASS for both profiles
4. **CLI verified**:
   - `btcedu detect --profile tagesschau_tr` assigns profile to new episodes
   - `btcedu run-pending --profile tagesschau_tr` only processes tagesschau episodes
   - `btcedu run-pending` (no flag) processes all profiles
5. **Dashboard verified**:
   - Profile badge visible on non-default episodes
   - Profile filter dropdown works
6. **Publisher verified**:
   - Bitcoin episodes get Bitcoin tags/category
   - Tagesschau episodes get news tags/category 25
   - News descriptions include source attribution
7. **Stock images verified**:
   - Bitcoin episodes use Bitcoin/crypto intent extraction prompt
   - Tagesschau episodes use news intent extraction prompt
   - Domain tag is "news" for tagesschau, "cryptocurrency" for bitcoin_podcast
8. **TTS verified**:
   - Profile-level voice_id override works
   - Falls back to settings default when empty
9. **Renderer verified**:
   - Bitcoin episodes use orange (#F7931A) accent
   - Tagesschau episodes use blue (#004B87) accent
10. **Docs exist**:
    - `docs/runbooks/profile-switching.md`
    - Updated CLAUDE.md

---

## 11. Non-Goals (Phase 4)

- **No channel→profile auto-mapping** — operator assigns profile via `--profile` flag; auto-mapping from channel table is a future enhancement
- **No per-profile systemd timers** — single timer processes all profiles; `--profile` is for manual CLI use
- **No multi-speaker TTS** — single voice per episode; anchor+reporter differentiation is future
- **No per-profile YouTube playlists** — all content goes to same channel; playlist management is future
- **No profile-specific thumbnail generation** — same placeholder approach for both profiles
- **No profile-specific render resolution/codec** — same ffmpeg settings for all profiles
- **No content-aware image generation** — DALL-E image generation is not profile-specific; stock images are the primary visual source for both profiles
- **No per-profile cost limits** — `max_episode_cost_usd` applies globally; per-profile budgets are future
- **No profile CRUD API** — profiles are YAML files, not DB records; no REST API to create/edit profiles
- **No A/B testing between profiles** — single pipeline execution per episode
