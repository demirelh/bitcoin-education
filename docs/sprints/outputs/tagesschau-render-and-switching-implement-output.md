# Tagesschau Render, Visual Packaging & Operator Switching — Phase 4 Implementation Output

**Date:** 2026-03-16
**Status:** Complete
**Tests:** 956 passing (944 baseline + 12 new)
**Plan:** `docs/sprints/outputs/tagesschau-render-and-switching-plan.md`

---

## Summary

Phase 4 closes the gap between profile routing (Phases 1-3) and actual runtime behavior. Every downstream stage — stock images, TTS, renderer, publisher — is now profile-aware. Operators can filter, detect, and batch-process by profile from the CLI and dashboard.

---

## Changes Made

### New Files (7)

| File | Purpose |
|------|---------|
| `btcedu/prompts/templates/tagesschau_tr/intent_extract.md` | News-specific stock image intent extraction prompt |
| `btcedu/prompts/templates/tagesschau_tr/stock_rank.md` | News-appropriate stock ranking criteria |
| `tests/test_cross_profile.py` | 12 cross-profile isolation + metadata tests |
| `docs/runbooks/profile-switching.md` | Operator guide for switching between profiles |
| `docs/sprints/outputs/tagesschau-render-and-switching-implement-output.md` | This file |

Profile YAMLs updated (now complete):
- `btcedu/profiles/bitcoin_podcast.yaml` — added `render.accent_color: "#F7931A"`
- `btcedu/profiles/tagesschau_tr.yaml` — added `render.accent_color: "#004B87"`, `tts.voice_id/stability/style`

### Modified Files (10)

#### `btcedu/core/stock_images.py`
- Added 30+ news/political vocabulary terms to `_TR_TO_EN` (meclis, hükümet, savaş, deprem, etc.)
- Added `_load_episode_profile()` helper (safe profile loading, returns None on error)
- `search_stock_images()`: loads profile domain → passes to `_derive_search_query()` as `domain_tag`
- `_derive_search_query()`: accepts `domain_tag` parameter (default "finance" for backward compat)
- `extract_chapter_intents()`: resolves profile-namespaced `intent_extract.md` template
- `rank_candidates()`: resolves profile-namespaced `stock_rank.md` template

#### `btcedu/core/renderer.py`
- `render_video()`: loads profile, extracts `stage_config.render.accent_color` (fallback `#F7931A`)
- `_chapter_to_overlay_specs()`: accepts `accent_color` parameter; applies to `statistic` overlays

#### `btcedu/core/tts.py`
- `generate_tts()`: loads profile `stage_config.tts` → effective `voice_id`, `stability`, `style`
- `_generate_single_audio()`: accepts `voice_id`, `stability`, `style` overrides; uses effective values for both dry-run and live requests

#### `btcedu/core/publisher.py`
- `_build_youtube_metadata()`: accepts `session` parameter; loads profile YouTube config
  - Uses `profile.youtube.tags` as base tags (falls back to `_BASE_TAGS`)
  - Builds hashtags from profile tags
  - For `domain == "news"`: prepends ARD tagesschau attribution to description
- `publish_video()`: loads profile for `category_id`, `default_language`, `default_privacy` overrides

#### `btcedu/core/pipeline.py`
- `run_pending()`: added optional `profile: str | None = None` filter
- `run_latest()`: added optional `profile: str | None = None` filter

#### `btcedu/cli.py`
- `detect`: added `--profile` option (overrides `default_content_profile` on new detections)
- `run`: added `--profile` option (filters when no `--episode-id` given)
- `run-latest`: added `--profile` option
- `run-pending`: added `--profile` option
- Added `smoke-test-pipeline` command: validates profile YAML, stage list, prompt templates, YouTube metadata, TTS config, accent color, domain tag

#### `btcedu/web/api.py`
- `batch_start()`: reads `profile` from POST body; passes to `job_manager.submit_batch()`

#### `btcedu/web/jobs.py`
- `BatchJob`: added `profile: str | None = None` field
- `submit_batch()`: accepts `profile` parameter, logs it
- `_execute_batch()`: applies `profile` filter to episode query

#### `btcedu/web/static/app.js`
- Added `selectedProfile` state variable
- Added profile badge on non-default episodes (blue `badge-profile` badge)
- `refresh()`: includes `profile=` query param when `selectedProfile` is set
- `toggleBatch()`: includes `profile` in batch start body
- Added `onProfileChange()` function (exposed globally)

#### `btcedu/web/templates/index.html`
- Added profile filter `<select id="profile-select">` next to channel selector

#### `btcedu/web/static/styles.css`
- Added `.badge-profile` style (ARD blue background, tagesschau-themed)

---

## Profile State After Phase 4

### bitcoin_podcast.yaml
```yaml
name: bitcoin_podcast
domain: cryptocurrency
stage_config:
  adapt:
    skip: false
  render:
    accent_color: "#F7931A"        # Bitcoin orange
youtube:
  category_id: "27"               # Education
  tags: [bitcoin, kripto, blockchain, türkçe, eğitim, cryptocurrency]
```

### tagesschau_tr.yaml
```yaml
name: tagesschau_tr
domain: news
stage_config:
  segment: { enabled: true }
  adapt: { skip: true }
  translate: { mode: per_story, register: formal_news }
  tts:
    voice_id: ""                   # empty = settings default; set to override
    stability: 0.6                 # news anchor consistency
    style: 0.0
  render:
    accent_color: "#004B87"        # ARD/tagesschau blue
youtube:
  category_id: "25"               # News & Politics
  tags: [haberler, almanya, türkçe, tagesschau]
```

---

## New Tests (`tests/test_cross_profile.py` — 12 tests)

| Test | What it verifies |
|------|-----------------|
| `test_bitcoin_and_tagesschau_stages_are_different` | Stage lists differ between profiles |
| `test_segment_stage_position_before_translate` | `segment` precedes `translate` in tagesschau |
| `test_v1_episode_gets_v1_stages` | v1 episodes ignore profile stage overrides |
| `test_profile_episode_fields_are_independent` | Each episode stores its own `content_profile` |
| `test_run_pending_profile_filter` | `run_pending(profile=...)` only processes matching episodes |
| `test_run_pending_no_filter_processes_all` | `run_pending()` without filter processes all profiles |
| `test_youtube_metadata_differs_by_profile` | Tags differ between bitcoin and tagesschau |
| `test_news_description_includes_attribution` | Tagesschau descriptions include ARD attribution |
| `test_stock_domain_tag_differs_by_profile` | Domain tag is "cryptocurrency" vs "news" |
| `test_accent_color_from_profile` | Bitcoin=`#F7931A`, tagesschau=`#004B87` |
| `test_tts_profile_config_values` | Tagesschau TTS has stability ≥ 0.6 |
| `test_bitcoin_profile_has_no_tts_override` | Bitcoin profile has no voice_id override |

---

## Manual Verification Steps

```bash
# 1. Validate both profiles pass smoke test
btcedu smoke-test-pipeline --profile bitcoin_podcast
btcedu smoke-test-pipeline --profile tagesschau_tr

# 2. Confirm profile list
btcedu profile list

# 3. Test --profile flag on detect (assigns profile to new episodes)
btcedu detect --profile tagesschau_tr

# 4. Test run-pending profile filter
btcedu run-pending --profile bitcoin_podcast   # only Bitcoin episodes
btcedu run-pending --profile tagesschau_tr     # only tagesschau episodes
btcedu run-pending                             # all profiles

# 5. Run cross-profile tests
pytest tests/test_cross_profile.py -v -q

# 6. Run full suite (baseline: 956)
pytest -q
```

---

## Design Decisions

1. **Fallback chain**: All profile overrides fall back gracefully to settings defaults. A missing `stage_config.tts.voice_id` (empty string or absent key) uses `settings.elevenlabs_voice_id`. This ensures zero regression for existing Bitcoin episodes.

2. **`_load_episode_profile()` helper**: Centralizes profile loading in stock_images.py with a safe try/except wrapper. Returns `None` on any error (profile file missing, registry failure) so callers can fall back to defaults without crashing.

3. **`domain_tag` in `_derive_search_query()`**: Default is `"finance"` for backward compatibility. `search_stock_images()` passes the profile's `domain` field (e.g., `"news"` for tagesschau). LLM-driven intent search hints bypass this tag entirely.

4. **Profile-namespaced prompts for intent/rank**: When `prompt_namespace` is set, `resolve_template_path()` checks `templates/{namespace}/{name}` first. If not found, falls back to base template. This means the base intent/rank prompts (Bitcoin-focused) continue to work without any file changes.

5. **`style` in `_generate_single_audio`**: The signature uses `style: float | None = None` with explicit `if style is not None` check, so a profile value of `0.0` (which is falsy) is correctly applied rather than falling back to settings.

---

## Production Readiness

Before running tagesschau episodes in production, verify the checklist in `docs/runbooks/profile-switching.md`:
- `btcedu smoke-test-pipeline --profile tagesschau_tr` → PASS
- `pytest tests/test_cross_profile.py -x -q` → all pass
- ElevenLabs voice tested for news register
- YouTube credentials present
- News editorial policy reviewed (`docs/runbooks/news-editorial-policy.md`)
