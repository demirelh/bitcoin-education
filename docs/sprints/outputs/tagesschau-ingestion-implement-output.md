# Tagesschau Ingestion — Phase 2 Implementation Output

**Date:** 2026-03-16
**Status:** Complete
**Tests:** 20 new tests, 910 total (zero regressions)

---

## What Was Implemented

### New Files (10)

| File | Purpose |
|------|---------|
| `btcedu/models/story_schema.py` | Pydantic `StoryDocument`, `Story`, `StoryCategory`, `StoryType` schemas |
| `btcedu/core/segmenter.py` | `segment_broadcast()` stage — extracts stories from corrected transcript via Claude |
| `btcedu/prompts/templates/segment_broadcast.md` | Story segmentation prompt (temperature=0.1, max_tokens=16384) |
| `btcedu/prompts/templates/tagesschau_tr/system.md` | News-specific system prompt (ARD attribution, neutrality rules) |
| `btcedu/prompts/templates/tagesschau_tr/correct_transcript.md` | News correction prompt (political/institutional terms) |
| `btcedu/prompts/templates/tagesschau_tr/translate.md` | Formal news register translation prompt |
| `btcedu/prompts/templates/tagesschau_tr/chapterize.md` | Story-to-chapter mapping prompt (1 story = 1 chapter) |
| `docs/runbooks/news-editorial-policy.md` | Operator editorial review guide |
| `tests/test_segmenter.py` | 13 segmenter unit tests |
| `tests/test_tagesschau_flow.py` | 7 tagesschau flow integration tests |

### Modified Files (8)

| File | Change |
|------|--------|
| `btcedu/models/episode.py` | Added `EpisodeStatus.SEGMENTED` and `PipelineStage.SEGMENT` enums |
| `btcedu/core/pipeline.py` | `_STATUS_ORDER[SEGMENTED]=10.5`, profile-aware `_get_stages()`, `segment` in `_run_stage()` + `_V2_ONLY_STAGES`, `SEGMENTED` in `run_pending()`/`run_latest()` |
| `btcedu/core/corrector.py` | Passes `profile_namespace` to `PromptRegistry.resolve_template_path()` for profile-namespaced prompt resolution |
| `btcedu/core/translator.py` | Added `SEGMENTED` to allowed statuses; per-story translation mode via `_translate_per_story()` helper; writes `stories_translated.json`; profile-namespaced prompt |
| `btcedu/core/chapterizer.py` | Story-mode chapterization (uses `stories_translated.json` when present); profile-namespaced prompt; allows `TRANSLATED` status for tagesschau episodes |
| `btcedu/profiles/tagesschau_tr.yaml` | Added `segment.enabled`, `translate.mode=per_story`, `prompt_namespace=tagesschau_tr`, tags |
| `btcedu/cli.py` | Added `btcedu segment` command |
| `btcedu/web/api.py` | Added `story_count` field to `_episode_to_dict()` via `_get_story_count()` helper |
| `tests/test_sprint1_models.py` | Updated enum count assertions (SEGMENTED + SEGMENT) |

---

## Test Counts

| Category | Count |
|----------|-------|
| New tests: `test_segmenter.py` | 13 |
| New tests: `test_tagesschau_flow.py` | 7 |
| **Total new tests** | **20** |
| Total passing (full suite) | **910** |
| Regressions | 0 |

---

## Assumptions Made During Implementation

1. **`_STATUS_ORDER` float values:** Used `10.5` for `SEGMENTED` between `CORRECTED (10)` and `TRANSLATED (11)`. Python dict values support floats and all comparisons (`>`, `<`) work correctly. No renumbering was required.

2. **Translator status check:** Extended the allowed statuses in `translate_transcript()` to include `EpisodeStatus.SEGMENTED` (news path enters translation from SEGMENTED, not CORRECTED). The Review Gate 1 check applies equally to both `CORRECTED` and `SEGMENTED` status.

3. **Chapterizer story mode:** The chapterizer detects story mode by checking if `stories_translated.json` exists in the outputs directory. This is a file-system check rather than a profile check, allowing backward compatibility: if `stories_translated.json` doesn't exist, the old adapted-script path is used.

4. **Corrector profile namespace lookup:** Uses `settings.profiles_dir` to load the profile registry and resolve `prompt_namespace`. If the profile lookup fails (e.g., profile not found), falls back to the base `correct_transcript.md` template gracefully.

5. **`_translate_per_story` helper:** Makes 2 API calls per story (headline + body). For a typical 10-story broadcast this is 20 calls vs. the standard 1-2 for full-transcript mode. This matches the plan's note that "per-story = shorter texts = similar total cost".

6. **`EPISODE_ID_PLACEHOLDER` in segment_broadcast.md:** The LLM is instructed to use the episode_id from the template. The segmenter code post-processes the response to replace `EPISODE_ID_PLACEHOLDER` with the actual episode_id, and also handles the case where the LLM left the field empty.

---

## Manual Verification Steps

```bash
# 1. Verify enum values
python -c "from btcedu.models.episode import EpisodeStatus, PipelineStage; \
  print(EpisodeStatus.SEGMENTED, PipelineStage.SEGMENT)"

# 2. Verify profile loads correctly with new fields
python -c "
from btcedu.profiles import get_registry, reset_registry
from btcedu.config import get_settings
reset_registry()
s = get_settings()
r = get_registry(s)
p = r.get('tagesschau_tr')
print('segment enabled:', p.stage_config.get('segment', {}).get('enabled'))
print('adapt skip:', p.stage_config.get('adapt', {}).get('skip'))
print('prompt_namespace:', p.prompt_namespace)
"

# 3. Verify profile-aware _get_stages for tagesschau
python -c "
from btcedu.profiles import get_registry, reset_registry
from btcedu.config import get_settings
from btcedu.core.pipeline import _get_stages
from btcedu.models.episode import Episode, EpisodeStatus
reset_registry()
s = get_settings()
ep = Episode(episode_id='x', title='x', url='x', source='x',
             pipeline_version=2, content_profile='tagesschau_tr')
stages = _get_stages(s, ep)
print([n for n, _ in stages])
"

# 4. Verify segment prompt templates exist
ls btcedu/prompts/templates/tagesschau_tr/
ls btcedu/prompts/templates/segment_broadcast.md

# 5. Run CLI help
btcedu segment --help

# 6. Run full test suite
pytest -q
```

---

## Known Limitations / Non-Goals

Per the plan's Section 12 (Non-Goals for Phase 2):

- **No tagesschau.de article scraping** — audio-only via YouTube yt-dlp
- **No subtitle/teletext extraction** — Whisper transcription only
- **No review gate after segmentation** — deferred to Phase 2+; segmentation is auto-approved
- **No TTS voice differentiation** — single voice for anchor and all reporters
- **No per-story image style** — same visual pipeline as bitcoin_podcast
- **No dashboard UI for story editing** — review via existing artifact viewer
- **No automatic weather graphics** — weather is a story like any other
- **No multi-language news** — German source only

---

## Pipeline Flow for tagesschau_tr

```
NEW → DOWNLOADED → TRANSCRIBED → CORRECTED → [review_gate_1] →
SEGMENTED → TRANSLATED → [adapt SKIPPED] → CHAPTERIZED →
IMAGES_GENERATED → TTS_DONE → RENDERED → [review_gate_3] → APPROVED → PUBLISHED
```

Key data artifacts at each stage:
- `transcript.corrected.de.txt` → `stories.json` (segment)
- `stories.json` → `stories_translated.json` + `transcript.tr.txt` (translate)
- `stories_translated.json` → `chapters.json` (chapterize)
