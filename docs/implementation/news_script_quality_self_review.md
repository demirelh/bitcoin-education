# News script quality — self review

Checked against the brief point by point, using the reference episode
`o4Y4qK2OEk8` and a deterministic preview run (no model call, no paid API).

## Confirmed and fixed during the review

| Finding | Severity | Resolution |
| --- | --- | --- |
| The topic card really does burn `03 / 06` into the picture — an earlier note in this work stream claimed it did not | major | `create_topic_intro_segment(show_counter=...)`, profile sets `topic_intro_show_counter: false`, verified on a rendered frame |
| The closing card repeated the spoken thanks (`ALMANYA24 — Bizi izlediğiniz için teşekkürler`) | minor | card now carries the slogan; `redundant_closing_card_text` catches a relapse |
| `headline_read_as_script` and `false_short_news_transition` were named in the brief but missing | major | both implemented with tests |
| Reporter limit was a single number, so a legitimate 200-word top story was flagged while a 140-word brief was not | major | per-priority limits (top 220 / normal 150 / brief 70) matching the brief's word figures |
| Anchor commentary had no length rule, only an abstractness heuristic | minor | `MAX_ANCHOR_ANALYSIS_WORDS = 60` plus the existing heuristic |
| Neutral-language rule (`Tahran rejimi`) and the lower-third duplication rule were missing | major | `loaded_language_repeated`, `lower_third_duplicates_first_sentence`, `missing_lower_third_summary` |
| The duration verdict was recomputed in the web layer, so the dashboard could disagree with the gate | major | `describe_duration()` stores verdict and justification in `script_qa.json`; the dashboard reads it |
| `len(script.stories)` still counted the greeting and goodbye in logs and provenance | minor | replaced by `broadcast_story_count()` |
| The prompt word table was tighter than the brief's own figures | minor | table aligned to 130–220 / 80–150 / 30–60 |
| `topic_intro_show_counter` was missing from two of the three render fingerprints, so a change would not have re-rendered | major | added to all three |

## Checked and found correct

- **Invalidation**: `_invalidate_downstream()` marks `chapters.json`,
  `tts/manifest.json` and `render/manifest.json` stale and nothing upstream, so
  transcription and translation never re-run for these changes. Branding and the
  script config are part of the script provenance hash, so a `spoken_name` or
  opening/closing change does regenerate the script.
- **No artificial padding**: the preview episode lands at 8.5 min, below the
  9-minute floor, and the run produces `episode_below_editorial_minimum`
  (minor) with the stored reason "5 broadcast stories, 6 dropped for
  relevance — the runtime was not stretched with filler" and **no** revision.
- **No aggressive trimming**: ranking now works against the 720 s soft maximum
  instead of 630 s; the test asserts it keeps at least as many stories as the
  old box.
- **Backwards compatibility**: without a `script.editorial` block the historic
  hard limits and revision behaviour are unchanged; `bitcoin_podcast` has no
  broadcast script and no `spoken_name`. Both are asserted by tests.
- **`auto_publish` stays `false`** — asserted by a test.
- **Weather visuals untouched**: only the spoken structure changed. The
  deterministic renderer, the claim extraction and
  `allow_external_weather_data: false` are unmodified, and
  `tests/test_final_review_weather.py` passes.
- **Secrets**: no credential is read, logged or committed.

## Second pass — further gaps found and fixed

| Finding | Severity | Resolution |
| --- | --- | --- |
| The prompt still received the old 540 s target while ranking and QA worked to 600 s, so the model wrote to one number and was judged against another | major | `_preferred_seconds()`; the prompt now also states the length is not a quota |
| A very long episode was only "major" for repetition, but the brief also names over-long reporter blocks and rambling commentary | major | `PADDING_CATEGORIES` extends the redundancy set with the length findings |
| The brief asks to check contradictory hedges (`teorik olarak resmî biçimde`); only a prompt rule existed | minor | `contradictory_hedging` |
| No rule against an over-long strap | minor | `lower_third_too_long` (120 characters) |
| Tests 16, 20 and 22 of the brief's list were missing | minor | accepted 9–12 min range, weather determinism, `pipeline_version=1` |

## Verified by a real dry run

`btcedu script --episode-id o4Y4qK2OEk8 --force` against a **copied** database
and outputs directory, `DRY_RUN=true`: cost `$0.0000`, 5 broadcast stories,
8.5 min, anchor share 43 %, no forced revision. The stored assessment reads
`below_minimum` with the reason that six stories were dropped for relevance and
the runtime was not padded. Spoken opening:
`İyi akşamlar, Almanya Yirmi Dört'e hoş geldiniz.`
Spoken closing: `Bugünün gündemi bu kadar. Bizi izlediğiniz için teşekkür
ederiz. Yeniden görüşmek üzere, iyi akşamlar.`

Speaker shares in that run: Nazlı 443 words (43 %, ~3:38), Cavit 598 words
(57 %, ~4:54).

## Known limitations

- The weather structure (tonight → tomorrow with its date → outlook) and the
  transition wording are steered by the prompt. They cannot be asserted
  deterministically, so the tests assert the prompt contract, not the model
  output.
- `anchor_analysis_too_abstract` uses a heuristic (no digit, no mid-sentence
  proper noun) and is deliberately `minor`.
- `loaded_language_repeated` works on a fixed term list; it is a hint, not a
  full tone analysis.
- The preview uses the deterministic fallback splitter, which is a crude
  sentence splitter. Its findings (duplicated lower thirds, one 288-word
  reporter block) reflect that fallback, not the model path — but they do prove
  the new checks fire on real material.
- No `mypy`/`pyright` configuration exists in the repository, so no type check
  was run. `ruff format` is not enforced repo-wide (51 files were already
  unformatted); only the files touched here were formatted.

## Commands run

`pytest` (2119 passed), 46 dry-run tests, `ruff check` on all touched files,
`ruff format` on all touched files, `git diff --check`, the deterministic
preview, a real ffmpeg topic-card render with and without the counter, and the
final-review, publish-gate and pipeline test modules.
