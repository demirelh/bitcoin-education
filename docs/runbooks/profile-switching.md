# Profile Switching Runbook

**Last updated:** 2026-03-16
**Applies to:** btcedu v2 pipeline with multi-profile support (Phase 4)

---

## Overview

btcedu supports multiple content profiles. Each profile configures the full pipeline for a specific source+target combination:

| Profile | Source | Target | Domain | Notes |
|---------|--------|--------|--------|-------|
| `bitcoin_podcast` | DE podcast | TR YouTube | cryptocurrency | Default; uses adapt stage |
| `tagesschau_tr` | DE TV news | TR YouTube | news | Skips adapt; uses segment + translation review |

---

## CLI Quick Reference

### Assign profile when detecting new episodes

```bash
# Detect Bitcoin podcast episodes (default)
btcedu detect

# Detect tagesschau episodes — assigns profile to new inserts
btcedu detect --profile tagesschau_tr
```

### Process only one profile

```bash
# Process all pending tagesschau episodes
btcedu run-pending --profile tagesschau_tr

# Process only Bitcoin podcast episodes
btcedu run-pending --profile bitcoin_podcast

# Process all profiles (no filter)
btcedu run-pending
```

### Run latest for a specific profile

```bash
btcedu run-latest --profile tagesschau_tr
```

### View current profiles

```bash
btcedu profile list
btcedu profile show tagesschau_tr
```

### Validate profile configuration (no API calls needed)

```bash
btcedu smoke-test-pipeline --profile bitcoin_podcast
btcedu smoke-test-pipeline --profile tagesschau_tr
btcedu smoke-test-pipeline  # validates all profiles
```

---

## Setting Up a New Tagesschau Channel

1. **Create the channel in DB**

   ```bash
   # Via web dashboard: Channels → + Add Channel
   # Or directly via API:
   curl -X POST /api/channels -d '{"name":"tagesschau","rss_url":"<RSS_URL>"}'
   ```

2. **Verify pipeline version**

   Ensure `.env` has:
   ```
   PIPELINE_VERSION=2
   ```

3. **Detect episodes and assign profile**

   ```bash
   btcedu detect --profile tagesschau_tr
   ```

4. **Run smoke test**

   ```bash
   btcedu smoke-test-pipeline --profile tagesschau_tr
   ```
   Expected output: `[PASS] All profile smoke tests passed.`

5. **Process first episode manually**

   ```bash
   btcedu run --episode-id <EPISODE_ID>
   ```

6. **Review at each gate**

   - Gate 1 (`review_gate_1`): Transcript correction review
   - Gate `review_gate_translate`: Translation review (bilingual, per-story diff)
   - Gate 3 (`review_gate_3`): Final video review

---

## Profile-Specific Pipeline Differences

### Bitcoin Podcast

```
NEW → DOWNLOAD → TRANSCRIBE → CORRECT → [RG1] →
TRANSLATE → ADAPT → [RG2] → CHAPTERIZE →
IMAGEGEN → [RG_STOCK] → TTS → RENDER → [RG3] → PUBLISH
```

- Adaptation stage rewrites Turkish content for cultural fit
- Bitcoin orange (#F7931A) statistic overlays
- YouTube category: 27 (Education)
- Tags: bitcoin, kripto, blockchain, türkçe, eğitim

### Tagesschau

```
NEW → DOWNLOAD → TRANSCRIBE → CORRECT → [RG1] →
SEGMENT → TRANSLATE → [RG_TRANSLATE] → CHAPTERIZE →
IMAGEGEN → [RG_STOCK] → TTS → RENDER → [RG3] → PUBLISH
```

- Segment stage splits broadcast into individual news stories
- Per-story translation with compression ratio warnings
- Bilingual review interface with story-level diff
- ARD tagesschau blue (#004B87) statistic overlays
- YouTube category: 25 (News & Politics)
- Tags: haberler, almanya, türkçe, tagesschau
- Attribution in video description: "Kaynak: ARD tagesschau"

---

## Updating Profile Configuration

Profile configuration lives in YAML files under `btcedu/profiles/`:

```bash
btcedu/profiles/
  bitcoin_podcast.yaml
  tagesschau_tr.yaml
```

After editing a profile YAML:
1. Restart the btcedu service to reload: `sudo systemctl restart btcedu`
2. Re-run the smoke test: `btcedu smoke-test-pipeline --profile <name>`

---

## Prompt Templates

Profile-specific templates override base templates:

```
btcedu/prompts/templates/
  system.md                       # base system prompt (bitcoin_podcast)
  correct_transcript.md           # base correction prompt
  translate.md                    # base translation prompt
  intent_extract.md               # base stock image intent prompt
  stock_rank.md                   # base stock ranking prompt
  tagesschau_tr/
    system.md                     # news system prompt (overrides base)
    correct_transcript.md         # news correction (institutional terms)
    translate.md                  # formal news translation
    chapterize.md                 # story-to-chapter mapping
    intent_extract.md             # news-specific image intent extraction
    stock_rank.md                 # news-appropriate stock ranking
```

---

## Troubleshooting

### Episode stuck at SEGMENTED

Tagesschau episodes can get stuck if the LLM returns invalid JSON for story segmentation.

```bash
btcedu investigate-failure --episode-id <ID>  # via web dashboard
# Or: check error_message in DB
btcedu run --episode-id <ID> --force
```

### Wrong profile assigned to episode

Profiles are assigned at detection time and stored on the Episode record. To reassign:

```bash
# Via web dashboard: Episode → edit content_profile field
# Or via SQL:
sqlite3 data/btcedu.db "UPDATE episodes SET content_profile='tagesschau_tr' WHERE episode_id='<ID>'"
```

### Smoke test FAIL: template not found

```
Template translate.md: not found in tagesschau_tr/ or base
```

Create the missing template file under `btcedu/prompts/templates/tagesschau_tr/`.

---

## Production Readiness Checklist

Before processing Tagesschau episodes in production:

- [ ] `btcedu smoke-test-pipeline --profile tagesschau_tr` → PASS
- [ ] `pytest tests/test_cross_profile.py -x -q` → all pass
- [ ] ElevenLabs voice tested with sample news text
- [ ] If different voice needed: set `tts.voice_id` in `tagesschau_tr.yaml`
- [ ] YouTube auth credentials present at `data/.youtube_credentials.json`
- [ ] Reviewed `docs/runbooks/news-editorial-policy.md`
- [ ] At least one episode fully processed manually before batch runs
