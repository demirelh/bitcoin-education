# Dual-presenter news format — implementation result

Companion to `dual_presenter_news_plan.md`. Records what was actually built,
what was deliberately left out, and how it was verified.

## What changed

### 1. Own identity instead of a read-out translation

The programme now presents itself as **ALMANYA24**, an independent
Turkish-language news show.

- `btcedu/core/branding_guard.py` scans everything that becomes visible or
  audible (document title, chapter titles, overlay headline and summary,
  narration, render intro/outro/ticker config) and fails the stage when a
  forbidden term appears.
- The guard runs twice: before `render` and as a pre-publish safety check.
- Source provenance is **not** removed. It stays in `stories_*.json`, the QA
  artifacts, the provenance files and the video description — only the visible
  layer is guarded.
- Profiles opt in via `branding.visible_source_attribution: false`. Every other
  profile is untouched (`forbidden_terms()` returns an empty list).
- Backward compatibility: episodes chapterized before this change carry a
  mandatory attribution lower third and a broadcaster document title. Both are
  cleaned in memory by `sanitize_overlays()` so an already-approved episode
  still renders instead of failing.

### 2. Two presenters

- `btcedu/models/script_schema.py` introduces `SpeakerRole`
  (`anchor_female` / `reporter_male`), `SpeakerSegment`, `ScriptStory` and
  `BroadcastScript`.
- The female anchor is Nazlı Yeni (`o9DOmAyPjfFu8AfoFAnM`), the male reporter
  is Cavit (`Q2IX97JeHBY3vNGzgM5s`).
- `btcedu/core/tts.py` synthesizes each speaker segment with its role's voice
  and joins the parts into the single chapter MP3 the renderer already expects,
  with a 0.35 s pause between presenters.

### 3. A shorter, ranked programme

- `btcedu/core/story_ranking.py` scores every source story deterministically
  (topic relevance for a Turkish audience in Germany, category baseline, lead
  flag, length) and fills an airtime budget of ~9 minutes.
- Source intro/outro stories are always dropped — the show uses its own opening
  and closing. The weather block is always kept.
- Stories become `top`, `normal`, `brief` or `omit`. Omissions are written to
  `script_omissions.json` with score and reason, so an editorial decision is
  always auditable.

### 4. Editorial script stage

- `btcedu/core/scripter.py` adds the `script` stage between the translation
  review gate and `chapterize`.
- It follows the existing stage pattern exactly: `PipelineRun`, idempotency via
  `input_hash`, cost guard, dry-run, provenance, downstream stale markers.
- When the editorial model is unavailable the stage falls back to a fully
  deterministic assembly that **only re-splits the approved translation** across
  the two presenters. It never invents wording.
- `btcedu/core/script_qa.py` gates the result deterministically: grounding of
  numbers and proper names against the source story, overlay well-formedness,
  no opinion or advocacy, completeness, no repetition, speaker balance and
  duration.

### 5. Chapter overlays with headline and summary

- `Overlay` gained `subtext` and `priority`; `Chapter` gained
  `display_headline` and `display_summary`.
- The animated lower third draws both lines inside its own bar, which now grows
  with the second line. Previously a two-line overlay overlapped itself and ran
  past the bar — that was a latent bug, now fixed.
- The static renderer places the two lines at dedicated safe-area positions
  above the subtitle band.
- Summaries are shortened at a word boundary so they never run off frame.

### 6. Chapterize became deterministic

When `script_broadcast.json` exists, `chapterize` maps one chapter per script
story and takes the narration verbatim instead of calling the LLM.

Consequences:

- the narration lock holds **by construction** — no drift repair, no retries
- one LLM call per episode is saved
- speaker segments and overlay texts survive into `chapters.json` for TTS and
  the renderer

## Narration lock: why the translation gate is untouched

The translation quality gate stores the SHA-256 of the approved *translation*.
The script re-splits that translation and adds the show's own opening and
closing, so the spoken text is no longer byte-identical to it.

Rather than rewriting the gate hash — which would destroy the guarantee it
exists for — the chain is layered:

| Gate | Guards | Compares |
| --- | --- | --- |
| translation quality gate | translation vs source | unchanged translation hash |
| script QA | script vs translation | grounding, completeness, no opinion |
| narration lock | chapters vs script | exact match, satisfied by construction |

`canonical_narration()` and `narration_sha256()` keep their original meaning, so
`publisher._check_narration_current` stays valid.

## Backward compatibility

- `pipeline_version=1` episodes are unaffected: `script` is in
  `_V2_ONLY_STAGES` and `_get_stages()` returns early for v1.
- Profiles without `stage_config.script.enabled` never see the stage.
- Profiles without `stage_config.tts.voices` keep single-voice synthesis.
- Chapters without `metadata.speaker_segments` are synthesized as before.
- Overlays without `subtext` render exactly as before.
- A role without a configured voice falls back to the profile's main voice and
  logs a warning instead of failing.
- Speaker segments are only used when they reconstruct the chapter narration
  exactly, so stale or hand-edited metadata can never change what is spoken.

## Verification

- Full suite: **2012 passed** (baseline 1955; 57 new tests).
- `ruff check` and `ruff format --check` clean on every touched file. 18
  pre-existing errors remain in files this work did not touch
  (`credits_service.py`, `flux_service.py`, `ideogram_service.py`,
  `image_provider_factory.py`, `frame_extractor.py`, `thumbnail_generator.py`,
  `tests/test_web_whatsapp.py`).
- `git diff --check` clean.
- Ranking, script assembly, script QA, the deterministic chapter mapping and the
  branding guard were run against three real episodes (`IuNt7iyNtkI`,
  `pA6-ifh-wpA`, `LH4ehxBtkbw`): narration lock passes, branding clean, weather
  chapter still detected, anchor share 38–56 %.
- ffmpeg overlay rendering verified by rendering real frames for both the static
  and the animated path.
- Multi-voice TTS verified once against the real provider: both voices used,
  parts joined into one 7.36 s file, $0.029.
- All tests mock external providers; no test makes a paid call.

## Corrections found by the first real run

The format was afterwards run end to end on the real episode `IuNt7iyNtkI`.
That run found four genuine faults, all fixed:

1. **Grounding heuristic produced false alarms.** `_proper_names()` treated every
   capitalised token as a name. Turkish headlines are set in capitals and Turkish
   capitalises the first word of a sentence, so ordinary nouns were reported as
   invented names — four `major` findings on one episode. Names are now only taken
   from mixed-case tokens that are not sentence-initial, the source side is a full
   word vocabulary, and agglutinated forms are matched on a four-character stem.
   A genuinely invented name is still caught.
2. **The duration gate was one-sided.** Overlong programmes triggered a revision,
   short ones only produced advice. `duration_revision_below` (0.9 × minimum) now
   makes a substantial shortfall a `major` finding with a revision. The
   deterministic fallback is exempt — it can only re-split approved text.
3. **The editorial model delivers less than it is allocated.** It writes roughly
   88 % of the words it is given. `RankingBudget.delivery_factor` (0.88) inflates
   the allocation accordingly; validated offline against four episodes.
4. **`_merge_short_chapters()` mutates chapters in place.** On the script path its
   return value was discarded, but the mutation persisted and appended the closing
   to the weather chapter, so the narration lock failed. The merge is now skipped
   entirely when a broadcast script drives the mapping. Regression test added.

### Speech rate calibration

The assumed 150 words per minute was wrong. A fully synthesized episode measured
**1135 words in 556.5 s = 122 wpm** (121–128 per chapter). `WORDS_PER_MINUTE` is
now 122 and the chapterizer imports the same constant instead of duplicating the
literal. The duration estimate for `IuNt7iyNtkI` moved from 454 s to 556 s against
556.5 s measured. Much of the apparent "too short" problem was an estimation
artefact, not a content problem.

### The generated images carried the source brand

A frame extracted from the finished render showed the upstream broadcaster's
name printed across the top of the picture. The cause was the profile's own
`imagegen.style_prefix`, which described the desired look as *"in the visual
style of a European public-broadcaster newscast (ARD/Tagesschau)"*. Generative
image models draw the words they are given.

The visible-text guard could not see this: it inspects overlay, title and
narration metadata, not pixels.

Two changes:

- The style prefix no longer names any broadcaster and now forbids readable
  text, captions, logos, channel names, watermarks and signage outright.
- `branding_guard.sanitize_image_prompt()` strips forbidden terms from both the
  style prefix and the per-chapter prompt before the request leaves the process,
  and logs every removal. A prompt carries no editorial content worth
  preserving, so removal is safe — unlike narration, which is locked. Profiles
  that permit attribution are unaffected.

Images generated before this fix still contain the printed name and must be
regenerated.

### Renderer robustness

A full render takes about an hour on the Pi, and the segment directory was
created once at the start and then assumed to survive. Two renders failed
because the directory disappeared mid-run. The cause was not the pipeline: it
was a manual `rm -rf render/segments` issued while a scheduled run was already
rendering. The renderer nevertheless now re-asserts the directory before every
segment, topic intro, intro and outro, because an hour of work should not be
lost to a directory that can be recreated for free.

Operational rule that follows from this: never touch `data/outputs/<id>/render/`
by hand. Check `ps` and the pipeline lock first — a scheduled `run-latest` fires
every five minutes.

### The summary line was cut off

The two-line lower third trimmed the summary at 70 characters, so a normal
Turkish sentence ended in an ellipsis with a third of the bar still empty. The
limit is now 88, measured rather than estimated: at the 34 px summary size the
rendered line ends at 1723 px of 1920, just inside the 5 % title-safe margin,
while 95 characters already reach the frame edge.

## Dashboard

- `GET /api/episodes/<id>/broadcast` returns the editorial view: show, date,
  running order with priority, duration and speaker pattern, headline and
  summary, omissions with score and reason, QA findings, configured voices,
  anchor share.
- The episode detail view has a **Sendung** tab (tagesschau profile only) that
  renders this view.
- `script_broadcast.json`, `script.broadcast.tr.md`, `script_omissions.json` and
  `script_qa.json` are downloadable through the existing file endpoint.

## Per-speaker visuals and the anchor-first rule

Requested after the first watched episode: a change of voice should also be a
change of picture, and the anchor should open, announce and close everything.

**Anchor announces every story.** The prompt (`script_broadcast.md`) now states
it as an absolute rule, the deterministic fallback in `scripter.py` splits even
short briefs so the anchor announces the first sentence and the reporter
delivers the rest, and `check_presentation_order()` in `script_qa.py` reports
`reporter_opens_story` (severity `major`, structural invariant) if a story still
starts with the reporter. In the first real episode two of nine stories
(`n03`, `n06`) started with the reporter, so the rule was only half kept before.

**One picture per presenter block.** The chapterizer groups consecutive segments
of the same speaker into *visual beats* (`metadata["visual_beats"]`, written only
for chapters that are neither frame nor weather and that really change speaker).
The image generator produces one additional picture per beat
(`<chapter>_beatNN.png`, `beat_index` in the metadata), the renderer builds the
chapter from one silent shot per beat and then lays the untouched chapter MP3
over the joined picture with the new `replace_audio_track()`.

Why the audio is laid over instead of cut: TTS already inserts a 0.35 s pause
between speakers, so cutting the chapter audio per speaker would be fragile.
Copying the video stream and re-attaching the original narration keeps the sound
exactly what TTS produced; only the picture cuts. Beat durations are weights
(measured per-part durations from `speaker_parts`, word counts otherwise) scaled
onto the chapter duration, so they always add up to the chapter length, and the
last shot gets 0.2 s of headroom so `-shortest` can never clip the final words.

The lower third belongs to the story, so it is drawn on the first shot only; the
fades stay at the outer edges of the chapter; the Ken Burns direction varies per
shot.

**Cost.** 16 pictures for an eight-chapter episode instead of 8. Measured on
episode `hrzrn0Wutak`: **$0.485**, less than the $0.64 the pipeline spent before
this change, because the provider fault described below was fixed at the same
time. A real ffmpeg run before release exposed a wrong `SegmentResult`
construction in `replace_audio_track()` that would have failed the live render.

### Three picture faults found by watching a real episode

Verifying against the manifest would have missed all three; they were only
visible in the frames themselves.

1. **Every chapter went to the text-rendering provider.** `_route_provider_for_chapter()`
   treated any lower third as "this picture must contain text". Every chapter has
   a lower third, so all 15 pictures came from Ideogram — which wrote a speech
   bubble full of nonsense words into the EU map. Overlays are drawn by the
   renderer with `drawtext`, never by the image model, so they no longer
   influence the routing. Flux now handles the photographic chapters: better
   pictures at $0.025 instead of $0.080 each.
2. **The style prefix asked for a television studio.** *"in the visual style of a
   European public-service television newscast"* produced a studio with an
   invented male presenter — contradicting the female anchor the viewer hears.
   The prefix now asks for editorial news photography and rules out studios,
   desks, presenters and flat vector illustration; `imagegen_news.md` says the
   same, and the beat hints only describe framing.
3. **The shots of one story looked unrelated.** The first shot was built from the
   chapter's one-line `image_prompt`, the following shots from a two-hundred-word
   generated prompt. Every shot of a chapter with beats now goes through
   `_generate_beat_prompt()`, with the one-line prompt folded into the
   description.

**Invalidation.** `visual_beats` is part of the `visuals` component hash, so a
changed presenter block invalidates images and render and leaves the narration
audio — and its cost — untouched. Verified on `hrzrn0Wutak`: re-chapterizing
marked images and render stale and wrote no TTS marker.

**End-to-end verification** on `hrzrn0Wutak` (04.08.2026): 8 chapters, 16
pictures, 18 rendered shots, draft 527.7 s. The audio track measures 527.70 s
against 527.68 s of picture, so nothing is clipped. Frames at 4 s, 20 s and 37 s
of `ch03` show three different pictures of one story (Reichstag, the meeting,
the ministry) with the lower third on the first shot only.

## Deliberately not done

- **Reusable intro master asset.** The intro is generated procedurally by
  `create_intro_segment()` from `intro_show_name`, `intro_slogan`,
  `intro_episode_title`, the episode date and `intro.mp3`. It is branded and it
  works. A hand-made intro clip would need artwork that does not exist in the
  repository and a new config field, so it is left as an explicit decision for
  the operator rather than invented here.
- **Weather refinements** (grouping regions per forecast day). The weather
  renderer already works — map, region cards, per-city temperatures from
  Open-Meteo — and rebuilding it was out of scope for this change.
- **Automatic YouTube publishing** remains off, as required.

## Operational notes

- The first episode through the new path will produce a noticeably shorter video
  (~7–9 minutes instead of ~11–12).
- Existing episodes are not migrated. They keep their chapters and render via
  the legacy sanitize path.
- To roll back, set `stage_config.script.enabled: false` and
  `branding.visible_source_attribution: true` in `tagesschau_tr.yaml`; the
  pipeline then behaves exactly as before.
