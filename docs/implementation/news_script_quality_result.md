# News script quality — result

Implements the plan in `news_script_quality_plan.md`.

## Root causes and fixes

| Symptom | Root cause | Fix |
| --- | --- | --- |
| "ALMANYA24'na hoş geldiniz" | `DEFAULT_OPENINGS` declined the on-screen logo | `branding.spoken_name` + `turkish_dative()`; screen keeps `display_name` |
| Opening read out screen titles | `_headline_block()` joined the all-caps `display_headline` | `_teaser_sentence()` uses `display_summary` / the first anchor sentence and refuses all-caps captions |
| "İşte ayrıntılar" promise | greeting variants ended in a filler phrase | greeting is only the greeting; the headline block closes with `Ayrıntılarla başlıyoruz.` |
| "Kısa haberlerle devam ediyoruz" for one story | the label was inserted before the *first* brief story | label only when at least two brief stories follow |
| Bulletin padded to hit a target | prompt rule 9 ordered the model to fill the airtime | rule rewritten: duration is a result, not a target; per-role word bands added |
| Length sent the script back | `duration_too_long` / `duration_too_short` were structural invariants | editorial band: long is only a defect together with redundancy, short is a review note that never asks for filler |
| Repetition survived QA | only verbatim anchor sentences were caught | sentence-level overlap: `cross_speaker_repetition`, `redundant_story_detail`, `headline_body_duplication` |
| Greeting/goodbye counted as stories | frame stories were counted like real ones | `broadcast_story_count()`, `non_story_counted_as_story`, dashboard counts without framing |
| Weather said "today/tomorrow" without a date | the prompt gave no structure | prompt structures tonight → tomorrow (with date) → outlook |

## New profile keys

`branding.display_name`, `branding.spoken_name`, `stage_config.script.editorial`
(`minimum_duration_seconds`, `preferred_duration_seconds`,
`soft_maximum_duration_seconds`, `hard_maximum_duration_seconds`,
`allow_longer_if_editorially_justified`).

Every key has a default that reproduces today's behaviour: without
`spoken_name` the display name is spoken, and without the `editorial` block the
historic hard duration limits stay in force. `pipeline_version=1` and
`bitcoin_podcast` are untouched.

## New QA findings

`invalid_spoken_brand_suffix`, `non_story_counted_as_story`,
`reporter_segment_too_verbose`, `anchor_analysis_too_abstract`,
`cross_speaker_repetition`, `redundant_story_detail`,
`headline_body_duplication`, `episode_below_editorial_minimum`,
`episode_longer_than_preferred`, `episode_overlong_due_to_redundancy`,
`duration_above_hard_maximum`.

Only `episode_overlong_due_to_redundancy` and `duration_above_hard_maximum`
force a revision.

## Dashboard

`/api/episodes/<id>/broadcast` now reports `frame_count`, `brief_block`, an
`editorial` block (display vs spoken name, duration band and verdict, the
broadcast greeting and goodbye) and per-segment word/second counts;
`story_count` excludes framing. The broadcast panel shows a "Redaktion"
section and per-speaker tooltips.

## Invalidation

Prompt and profile changes flow into the script provenance hash, so the next
run regenerates `script_broadcast.json` and everything downstream of it.
Stored artifacts of already rendered episodes are left as they are.

## Tests

16 new tests in `tests/test_broadcast_script.py` covering the dative helper,
spoken vs on-screen brand, the brand QA finding, sentence headlines, the
missing filler promise, the short-news label in both directions, all four
editorial duration cases plus the legacy hard-limit path, cross-speaker
repetition and its false-positive guard, reporter verbosity, and story
counting. Full suite: 2081 passed.

## Limitations

- `anchor_analysis_too_abstract` is a heuristic (no digit, no mid-sentence
  proper noun) and is deliberately `minor`.
- Transitions are steered by the prompt only; no deterministic transition text
  is injected, because a fixed list would reintroduce the generic wording the
  change set removes.
- The weather restructure depends on the model following the prompt; the
  deterministic weather renderer and claim extraction are untouched.
