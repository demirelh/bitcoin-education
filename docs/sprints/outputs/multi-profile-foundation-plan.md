# Multi-Profile Content Pipeline — Phase 1 Foundation Plan

**Date:** 2026-03-16
**Status:** Planned
**Scope:** Foundational architecture for multi-profile support

---

## Goal

Extend btcedu from a single hardcoded Bitcoin podcast pipeline into a multi-profile content pipeline that can process different content types (podcasts, news, etc.) with different language pairs and stage configurations — without forking the codebase.

## Architecture

### Profile = YAML config file, not DB table

Profiles are configuration, not data. Each profile is a YAML file in `btcedu/profiles/`:

```
btcedu/profiles/
├── __init__.py              # ContentProfile model + ProfileRegistry
├── bitcoin_podcast.yaml     # existing workflow (DE→TR Bitcoin podcast)
└── tagesschau_tr.yaml       # new workflow (DE→TR news)
```

### ContentProfile Model (Pydantic)

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Slug identifier (e.g. `bitcoin_podcast`) |
| `display_name` | str | Human-readable name |
| `source_language` | str | ISO 639-1 (e.g. `de`) |
| `target_language` | str | ISO 639-1 (e.g. `tr`) |
| `domain` | str | Content domain (e.g. `cryptocurrency`, `news`) |
| `pipeline_version` | int | Pipeline version (1 or 2) |
| `stages_enabled` | list[str] | Which stages to run (or `"all"`) |
| `stage_config` | dict | Per-stage overrides (e.g. `adapt: {skip: true}`) |
| `review_gates` | dict | Per-gate config (auto-approve thresholds) |
| `youtube` | dict | Upload config (category, language, privacy, tags) |
| `prompt_namespace` | str? | Template directory (defaults to profile name) |

### ProfileRegistry

Singleton that loads, validates, and caches profiles from YAML files:
- `load_all(profiles_dir)` → `dict[str, ContentProfile]`
- `get(name)` → `ContentProfile` (raises `ProfileNotFoundError`)
- `list_profiles()` → `list[ContentProfile]`

### Episode Linkage

- New column: `episodes.content_profile VARCHAR(64) DEFAULT 'bitcoin_podcast' NOT NULL`
- All existing episodes get `bitcoin_podcast` via migration default
- Simple string field, not FK — profiles are config files, not DB rows

### Prompt Template Resolution

Profile-namespaced with fallback:
1. Check `templates/{profile}/{name}.md`
2. Fall back to `templates/{name}.md`

This allows profiles to share templates by default and override only when needed.

---

## Changes Summary

### New Files (5)

| File | Purpose |
|------|---------|
| `btcedu/profiles/__init__.py` | ContentProfile + ProfileRegistry |
| `btcedu/profiles/bitcoin_podcast.yaml` | Bitcoin podcast profile definition |
| `btcedu/profiles/tagesschau_tr.yaml` | Tagesschau news profile (skeleton) |
| `btcedu/migrations/m008_add_content_profile.py` | Migration: add content_profile to episodes |
| `tests/test_profiles.py` | Profile tests |

### Modified Files (6)

| File | Change |
|------|--------|
| `btcedu/models/episode.py` | Add `content_profile` field |
| `btcedu/core/prompt_registry.py` | Profile-namespaced template resolution |
| `btcedu/cli.py` | `--profile` option + `btcedu profile list/show` |
| `btcedu/config.py` | `profiles_dir` + `default_content_profile` settings |
| `btcedu/web/api.py` | Expose `content_profile` in episode JSON + filter |
| `btcedu/core/pipeline.py` | Load profile, wire skip-stage logic |

---

## What Phase 1 Does NOT Do

- Rewrite any stage logic (stages ignore profile config until Phase 2)
- Add per-profile prompt templates (just the resolution mechanism)
- Add tagesschau-specific RSS/download logic
- Change dashboard UI (beyond showing profile field)
- Add profile switching in the dashboard

---

## Profile Examples

### bitcoin_podcast.yaml
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
youtube:
  category_id: "27"
  default_language: tr
  default_privacy: unlisted
  tags: [bitcoin, kripto, podcast]
review_gates:
  review_gate_1:
    auto_approve_threshold: 5
```

### tagesschau_tr.yaml
```yaml
name: tagesschau_tr
display_name: "Tagesschau News (DE→TR)"
source_language: de
target_language: tr
domain: news
pipeline_version: 2
stages_enabled: all
stage_config:
  adapt:
    skip: true  # news doesn't need cultural adaptation
youtube:
  category_id: "25"
  default_language: tr
  default_privacy: unlisted
  tags: [haberler, almanya, türkçe]
```

---

## Verification

1. All existing 853+ tests pass (no regressions)
2. New `tests/test_profiles.py` passes
3. `btcedu profile list` shows both profiles
4. `btcedu profile show bitcoin_podcast` displays config
5. `btcedu migrate` applies migration 008
6. Existing episodes show `content_profile=bitcoin_podcast`
7. New episodes with `--profile tagesschau_tr` store correct profile
