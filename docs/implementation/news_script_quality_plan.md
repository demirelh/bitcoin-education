# News script quality — plan

Reference episode: `o4Y4qK2OEk8` (tagesschau 20:00 Uhr, 05.08.2026), profile
`tagesschau_tr`. Every root cause below was read out of that episode's
`script_broadcast.json`, `chapters.json` and `script_qa.json`, not guessed.

## Root causes

1. **Opening and closing counted as stories.** `_frame_stories()` in
   `core/scripter.py` wraps the programme in two synthetic `ScriptStory`
   objects with `category="opening"` / `"closing"`. Everything downstream that
   asks "is this a real story" tested for `intro`/`outro`, the types used by the
   *segmented source*, not by the broadcast script. The renderer therefore made
   a topic card for the greeting and the goodbye (fixed in `33f8593`); the
   script QA and the dashboard still count them.

2. **`ALMANYA24'na`.** `DEFAULT_OPENINGS` hardcodes the Turkish dative as
   `{show_name}'na`. The suffix is written for a name ending in a vowel, but the
   brand is read aloud as *Almanya Yirmi Dört*, which ends in a hard consonant
   and takes `'e`. There is only one brand string in the profile
   (`branding.show_name`), used both on screen and in the mouth of the anchor.

3. **Screen titles are read out.** `_headline_block()` joins
   `story.display_headline` — the 2–6 word all-caps lower-third title — into the
   headline block. The anchor therefore reads "HAVALİMANINDA İHA ALARMI." as a
   sentence.

4. **`Kısa haberlerle devam ediyoruz` for a single item.** `_frame_stories()`
   inserts the label in front of the *first* `BRIEF` story without ever asking
   how many brief stories follow.

5. **Cavit's reports run long.** Rule 9 of `script_broadcast.md` tells the model
   that the per-story seconds are an airtime obligation ("süreyi doldur"), and
   `story_ranking.py` hands a top story `body * 1.35` seconds. The reporter
   carries the body, so all the slack lands in his segment. No rule bounds a
   single segment.

6. **Nazlı sounds academic.** The prompt's own list of permitted evaluations
   contains "Önümüzdeki dönemde belirleyici olacak soru…". The model copies the
   register it is shown.

7. **Repetition between the two.** `check_repetition()` only catches a *verbatim*
   anchor sentence inside the reporter text. Restating the same fact in
   different words passes, as does headline ↔ first sentence duplication.

8. **Duration is a hard box.** `script.target_seconds/min_seconds/max_seconds`
   (540/480/630) feed both `RankingBudget` and `ScriptQAConfig`. Above 630 s the
   QA raises a `major` with `structural_invariant=True` and forces a revision —
   regardless of whether the length is justified.

9. **Artificial filling and cutting.** The ranking's second pass re-admits
   omitted stories purely to reach the floor; the prompt tells the model to fill
   the airtime; the QA sends the script back when it is over the ceiling.

10. **Invalidation.** `scripter._invalidate_downstream()` already marks the
    downstream stages stale and leaves transcription/translation alone. New
    stale triggers are only needed where a *rendered* artifact depends on text
    that changes (weather scene video, overlays).

## Affected files

| Area | File |
| --- | --- |
| Duration bands, brand, opening/closing, transitions | `btcedu/core/scripter.py` |
| QA rules and findings | `btcedu/core/script_qa.py` |
| Ranking budget | `btcedu/core/story_ranking.py` |
| Editorial guidance | `btcedu/prompts/templates/tagesschau_tr/script_broadcast.md` |
| Profile config | `btcedu/profiles/tagesschau_tr.yaml` |
| Story counting, editorial panel | `btcedu/web/api.py` |
| Bumper types (already shared) | `btcedu/core/renderer.py` |

## Planned changes

- **Duration.** New profile block `script.editorial` with
  `minimum_duration_seconds: 540`, `preferred_duration_seconds: 600`,
  `soft_maximum_duration_seconds: 720`, `hard_maximum_duration_seconds: null`,
  `allow_longer_if_editorially_justified: true`. `ScriptQAConfig` grows the
  same fields; below the minimum → `episode_below_editorial_minimum` (review,
  never filler), between minimum and soft maximum → accepted, above the soft
  maximum → only `episode_overlong_due_to_redundancy` when redundancy or
  verbosity findings exist, otherwise informational. No forced revision on
  length alone unless a hard maximum is configured.
- **Brand.** `branding.spoken_name` next to `display_name`/`show_name`, plus a
  deterministic Turkish suffix helper. Opening/closing templates use the spoken
  name; screen texts keep `ALMANYA24`.
- **Opening.** Greeting + 2–4 headline *sentences* derived from
  `display_summary` (or the first sentence of the anchor's introduction), never
  from the all-caps title.
- **Closing.** New default wording; the closing card keeps name + slogan.
- **Transitions.** Brief label only with ≥2 brief stories; otherwise a
  deterministic, context-dependent transition sentence.
- **Roles and lengths.** Prompt gains per-role word bands and an explicit
  "the anchor explains, the reporter reports" contract; QA gains
  `reporter_segment_too_verbose` and `anchor_analysis_too_abstract`.
- **Repetition.** `cross_speaker_repetition`, `redundant_story_detail`,
  `headline_body_duplication` based on sentence-level token overlap.
- **Weather.** Prompt structures the forecast as tonight → tomorrow → outlook;
  the deterministic renderer and its claim extraction stay untouched.
- **Dashboard.** Editorial panel: display/spoken name, story count without
  bumpers, brief block yes/no, words and seconds per speaker segment, the new
  findings and the duration justification.

## Data model

`ScriptQAConfig` gains the editorial duration fields. No DB migration: all new
state lives in `script_qa.json` and the profile YAML.

## Stale invalidation

Unchanged path (`scripter._invalidate_downstream`): chapterize → imagegen →
tts → render → review. Transcription, translation and adaptation never re-run
for these changes.

## Backwards compatibility

- `pipeline_version=1` untouched.
- `bitcoin_podcast` keeps its behaviour: every new profile key has a default
  that reproduces today's numbers when the key is absent.
- Episodes without `spoken_name` fall back to `show_name`.
- `auto_publish` stays false.

## Tests

The 22 checks listed in the brief, added to `tests/test_scripter.py`,
`tests/test_script_qa.py`, `tests/test_story_ranking.py` and
`tests/test_profiles.py`. No provider is called: the script stage is driven
through a stubbed `call_claude`.
