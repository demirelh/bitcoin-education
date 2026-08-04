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

- Full suite: **2001 passed** (baseline 1955; 46 new tests).
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

## Deliberately not done

- **Dashboard UI for the script stage.** The artifacts
  (`script_broadcast.json`, `script_omissions.json`, `script_qa.json`) are
  written and readable, but no endpoints or views were added.
- **Reusable intro master asset.** The profile's existing `intro_*` render
  settings are used; no separate pre-rendered intro clip was introduced.
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
