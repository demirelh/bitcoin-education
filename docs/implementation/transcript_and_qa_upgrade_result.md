# Transcript and QA Upgrade: Final Result

Status: completed and independently re-reviewed on 2026-07-19.

## Independent review resolution (2026-07-19)

Every finding in
`docs/implementation/transcript_and_qa_upgrade_independent_review.md` was
checked against commit `1d7291b` and the current implementation.

| Finding | Decision | Rationale and outcome |
| --- | --- | --- |
| F1: correction Cost Guard | **confirmed** | `correct_transcript()` called the paid LLM without checking cumulative episode cost. It now checks before every call, checks again immediately after every paid response, records partial spend on failure, marks the run failed, and sets the episode to `COST_LIMIT`. |
| F2: localized number parsing | **confirmed** | The previous regex/parser interpreted `1.000` as `1` and could not consume multiple thousands groups. Numeric tokens now support German/Turkish thousands separators, decimal commas, singular/plural million/milliard forms, and standalone scaled values. The detector version was bumped so old QA caches are invalidated. |
| F3: negation heuristic | **partially confirmed** | The claimed false match from an ordinary positive `-ma/-me` verb was not reproducible: the regex requires Turkish negative morphology and existing cases remained correct. However, the generic token `ne` did hide omissions in phrases such as `ne zaman`. Generic `ne` markers were removed and only a conservative `ne ... ne de ...` construction is accepted. |
| F4: real orchestration coverage | **partially confirmed** | Runtime wiring through `_get_stages()`, `_run_stage()`, and `run_episode_pipeline()` was present and correct; the defect was test coverage, not production orchestration. A new integration test runs the real v2 transcript chain through analysis, selective verification, correction, Transcript-QA, and the blocking review gate while mocking only the paid LLM boundary. |
| F5: shared Whisper temp directory | **confirmed (minor)** | Both chunked transcription paths use a deterministic `_whisper_tmp` directory. The host pipeline lock prevents normal timer overlap, but direct concurrent same-file calls can collide. This remains documented minor concurrency debt and was not required for the confirmed major corrections. |
| F6: non-atomic chapter writes | **confirmed (minor)** | `chapters.json` and its provenance still use direct writes. The authoritative translation gate is atomic and chapterization is idempotently regenerable, but interruption can leave a corrupt chapter sidecar. A shared atomic artifact writer remains recommended. |
| F7: broad profile fallbacks | **partially confirmed (minor)** | Broad exception fallbacks exist. Several are deliberately fail-safe, but some lack a warning and can obscure configuration errors. They do not bypass QA, narration, or publish gates. Narrower exceptions and consistent warning logs remain recommended. |
| F8: file-based finding history | **rejected as a defect** | The factual observation is correct, but the upgrade explicitly chose existing JSON provenance plus artifact-bound `ReviewTask`/`ReviewDecision` records instead of new QA tables. This satisfies the agreed SQLite-compatible artifact design; DB mirroring is an optional reporting feature, not a missing correctness requirement. |
| F9: model name in prompt frontmatter | **confirmed (minor/cosmetic)** | The templates contain a historical model label, but runtime routing and provenance use effective Settings/profile models. No business logic is hard-coded to that frontmatter value. Removing the label remains harmless cleanup rather than a production blocker. |

The review suggestions were also checked:

- **S1 partially confirmed:** apostrophe suffixes are supported; unrestricted
  Turkish stemming is intentionally not used because entity findings are
  conservative and minor.
- **S2 confirmed as intended behavior:** full secondary transcription is an
  explicit opt-in and remains bounded by clip, duration, and cost limits.
- **S3 confirmed as pre-existing debt:** report-level cost aggregation still
  parses stage detail strings; authoritative costs remain stored structurally
  in `PipelineRun`.

After these corrections there are no open critical or major findings from the
independent review. F5-F7 and F9 remain non-blocking technical debt for the
single-host, manually published production profile.

## Final architecture

The upgrade replaces free-form transcript and translation handling with a
versioned, traceable, gated pipeline:

```text
download
  -> transcribe
  -> transcript_analyze
  -> transcript_verify
  -> correct
  -> transcript_qa
  -> review_gate_transcript_qa
  -> review_gate_1
  -> segment
  -> translate
  -> adapt
  -> deterministic translation QA
  -> independent standard/escalation LLM QA
  -> GREEN/YELLOW/RED quality gate
  -> chapterize (approved narration locked)
  -> frameextract
  -> imagegen
  -> review_gate_stock
  -> tts
  -> anchorgen
  -> render
  -> review_gate_3
  -> manual publish
```

`segment`, secondary transcription, adaptation mode, QA providers, escalation,
retry limits, media routing, and publishing are profile-controlled. For
`tagesschau_tr`, secondary ASR is limited to suspicious regions, adaptation is
conditional, and `auto_publish` is false.

## Data models and artifact formats

### Structured transcript

`TranscriptDocument` stores provider/model/language, complete text, usage/cost,
and ordered `TranscriptSegment` entries with stable `seg-NNNN` IDs,
timestamps, text, and optional confidence.

Related versioned artifacts:

- `transcript.structured.de.json`
- `transcript_analysis.json`
- `transcript_verification.json`
- `transcript.corrected.structured.de.json`
- `transcript/transcript_qa.json`

The correction artifact retains original/corrected text, status, severity,
flags, reason, and verification IDs per segment. Uncertainty is never converted
into plausible precision.

### Story inventory and translation

`StoryDocument` preserves deterministic story order, stable story IDs, source
segment IDs, exact source text, time range, confidence, and uncertainty flags.
Intro/outro/meta stories are typed explicitly; neutralization may remove
moderator branding but not news facts.

Artifacts:

- `stories.json`
- `stories_translated.json`
- `stories_adapted.json`
- legacy `transcript.tr.txt` and `script.adapted.tr.md`

Each translated story keeps its ID and source segment IDs and records
translator flags, omitted uncertain details, glossary terms, and narration
hash. Conditional adaptation records only allowed operations.

### QA findings and gates

`QAFinding` is shared by deterministic and model QA:

- finding/story/segment identity
- category and severity
- source and target excerpts
- explanation and required action
- detector, provider, model, retry generation
- open/resolved/dismissed status and history
- contradiction marker

`translation_qa.json` contains zero-cost deterministic checker results, story,
number, and glossary coverage. `translation_quality_gate.json` merges
deterministic, standard-model, and optional escalation findings, model calls,
costs, retry history, decision, and approved narration hash.

## Deterministic checks

Transcript analysis flags suspicious ASR regions without external calls.
Selective verification merges nearby suspicious ranges, clamps clips to media
bounds, enforces clip/audio/cost limits, and calls secondary ASR only for those
ranges.

Translation QA checks, per story and local context:

- missing, duplicate, unknown, and reordered stories
- numbers, dates, times, percentages, money, temperatures, scores, and
  casualty/injury counts
- localized German/Turkish thousands and decimal formats, including scaled
  million/milliard expressions
- protected glossary targets and Turkish suffixes
- conservative entities, negation, chronology, and weather-region mapping

The regression suite covers WM/EM, 12/13 July, `tausend`/`binlerce`,
`Titelverteidiger`, `Nachspielzeit`, invented casualties, missing stories,
weather regions, and moderator/intro/outro neutralization.

## Independent QA and decision matrix

The independent QA receives only the corrected German source, segment/story
structure, Turkish output, glossary, deterministic findings, and rubric. It
does not receive producer reasoning or external facts and cannot silently edit
the translation.

- **GREEN:** no blocking facts; narration is approved and hashed.
- **YELLOW:** repairable findings; targeted affected-story translate/adapt
  retries may run.
- **RED:** critical factual risk, missing lead story, unresolved casualty/date/
  result/role/transcript issue; chapterize stops and review is created.

Retries are bounded by `quality_gate.max_automatic_retries`. Retry generation,
triggering finding IDs, model/provider, cost, result, and resolved history are
preserved across forced reruns.

## Narration lock and downstream safety

On GREEN, SHA-256 is calculated over the approved Turkish narration.
Chapterize may partition and technically normalize that text but cannot add,
remove, reorder, or rewrite facts. The composed chapter narration is checked
against the approved hash.

Selective stale propagation avoids unnecessary media spend:

- transcript/audio change: transcript analysis through media
- QA-rule change: QA first; media only if approved narration changes
- image prompt change: image generation and render
- TTS voice/model/lexicon change: TTS and render
- overlay change: render

Image, TTS, and render manifests include provider/model/input hashes and
provenance. TTS is chapter-retryable and reuses unchanged text/voice/model/
lexicon output.

## Provider routing and costs

Providers are selected by profile, not hard-coded business rules.
`tagesschau_tr` currently configures OpenAI primary/secondary transcription,
Copilot CLI producer/QA models, profile-routed generative images, and
ElevenLabs TTS.

Deterministic analysis and QA cost zero. Every paid call checks cumulative
episode cost before invocation and records structured `cost_usd`. Partial
provider spend is retained on failure; a stage is not marked successful after
the episode limit is reached.

The correction stage now enforces this invariant before and after each paid
response, including its structured-output retry.

Configuration precedence, from lowest to highest, is:

1. `Settings` class defaults
2. `.env` and environment variables
3. explicit `Settings(...)` constructor values
4. profile-owned stage/provider values
5. supported CLI overrides for the current invocation

## CLI operations

```bash
btcedu transcript-analyze --episode-id ID [--force]
btcedu transcript-verify --episode-id ID [--force] [--dry-run]
btcedu transcript-qa --episode-id ID [--force]
btcedu translation-qa --episode-id ID [--force]
btcedu retry --episode-id ID
btcedu run-latest [--profile NAME] [--all-channels]
```

QA exit codes are `0` for non-blocking completion, `1` for input/execution
failure, and `2` for a valid blocking RED result. Deterministic commands have
no dry-run flag because they make no external calls.

## Web functions

The dashboard exposes transcript and translation QA status, GREEN/YELLOW/RED,
findings by severity/story, timestamps, source/target text, required action,
finding status/history, provider/model, cost, and retry generation.

Mutating actions include transcript approval/change requests, finding
resolve/dismiss/reopen, Restart Translate + Adapt, and Restart All. Review
actions verify current artifact paths and hashes; stale tasks cannot approve or
modify new artifacts.

## Prompts

Correction, story segmentation, translation, adaptation, chapterize, and QA
prompts use structured outputs. They prioritize factual fidelity,
completeness, uncertainty preservation, natural Turkish, then style. They
forbid external facts, free reconstruction, summary, creative additions, and
changes to protected facts or story order. Profile-specific templates override
base templates through the prompt registry and remain hash-versioned.

## Files

New production modules introduced by the upgrade:

- `btcedu/models/transcript_schema.py`
- `btcedu/models/qa_schema.py`
- `btcedu/core/transcript_analyzer.py`
- `btcedu/core/transcript_verifier.py`
- `btcedu/core/transcript_qa.py`
- `btcedu/core/translation_qa.py`
- `btcedu/core/narration_lock.py`
- `docs/implementation/transcript_and_qa_upgrade_audit.md`
- `docs/implementation/transcript_and_qa_upgrade_result.md`

Major modified surfaces:

- pipeline orchestration, transcriber, corrector, segmenter, translator,
  adapter, QA reviewer, chapterizer, media stages, publisher, reviewer, CLI
- transcript/LLM/TTS/ffmpeg services
- `tagesschau_tr` and `bitcoin_podcast` profiles
- prompt templates and news glossary
- existing `story_schema.py` and story-segmentation prompt/model
- Flask API/jobs/dashboard assets
- configuration, runbooks, architecture, deployment, and environment examples
- focused unit, integration, CLI, web, migration, v1/v2, cost, and publish tests

Across commits `8809796^..ded819c`, 93 files changed before this final
documentation phase.

## Migrations and compatibility

No new database table or column was required for Phases 1-10. Existing
`PipelineRun`, `ContentArtifact`, `ReviewTask`, `ReviewDecision`, JSON metadata,
and provenance artifacts were reused. The existing migration chain remains
idempotent and is tested against old SQLite schemas and fresh installations.

Legacy text and story artifacts remain readable. Existing stored v1 episodes
omit the new v2-only transcript QA stages and retain the tested compatibility
path. New profile definitions require v2.

## Environment variables

Operators must provide only the credentials required by their selected profile:

- source: `PODCAST_YOUTUBE_CHANNEL_ID` or RSS configuration
- transcription/images: `OPENAI_API_KEY`
- Anthropic provider/fallback: `ANTHROPIC_API_KEY`
- GitHub Models, if selected: `GITHUB_TOKEN`
- Copilot CLI provider: installed/authenticated `copilot`, plus optional
  `COPILOT_CLI_*` overrides
- Flux/Ideogram/Gemini when selected: `FAL_API_KEY`, `IDEOGRAM_API_KEY`,
  `GEMINI_API_KEY`
- TTS: `ELEVENLABS_API_KEY` and profile/global voice configuration
- YouTube: client secret and OAuth credentials paths

Also review `DEFAULT_CONTENT_PROFILE`, primary/secondary transcription,
Transcript-QA thresholds, `QA_REVIEW_ENABLED`, `QA_MODEL`,
`MAX_EPISODE_COST_USD`, render timeouts, and `auto_publish` in the profile.

## Deployment

1. Back up `data/btcedu.db` and `data/outputs/`.
2. Pull the final commit.
3. Install `.[web]` and optional `.[youtube]` dependencies.
4. Run `btcedu init-db`, `btcedu migrate`, and `btcedu migrate-status`.
5. Verify `.env`, profile provider routing, Copilot/API authentication, ffmpeg,
   fonts, and YouTube OAuth.
6. Restart web/detect/run services through `run.sh`.
7. Check `/api/health`, CLI QA help, pending reviews, and one dry-run/test
   episode before enabling timers.
8. Keep final YouTube publishing manual for `tagesschau_tr`.

## Rollback

Stop processing timers, check out the previous known-good commit, reinstall,
and restart services. Migrations have no automatic down migration; restore the
pre-deployment database/output backup only if the older application cannot
read current artifacts. Do not delete migration history or manually rewrite
episode statuses. Structured artifacts coexist with legacy sidecars, so an
application-only rollback normally preserves data.

## Validation result

- Full pytest suite after the independent-review corrections:
  **1,533 passed** in 111.51 seconds
- Focused independent-review regressions: **85 passed**
- Migration suite: **15 passed**
- Migration + mocked v1/v2 integration selection: **17 passed**
- Focused final QA/pipeline/web regressions: **118 passed**
- CLI help smoke: **passed**
- Flask health/index/episodes/reviews/profiles smoke: **passed**
- Mocked complete v1/v2 dry-runs: covered by
  `test_complete_pipeline_dry_run_is_legacy_safe`
- Real v2 transcript orchestration: covered from `TRANSCRIBED` through
  `review_gate_transcript_qa` with real `_get_stages`, `_run_stage`, analysis,
  verification, correction, Transcript-QA, and artifact-bound review creation
- Dedicated type checker: **not configured**
- `ruff check .`: changed implementation files pass; the repository-wide
  command reports 18 unrelated baseline findings in frame extraction,
  thumbnails, credits, Flux, Ideogram, image-provider factory, and the legacy
  channel migration script
- `ruff format --check .`: changed implementation files pass; the
  repository-wide command reports 31 unrelated pre-existing files that would
  be reformatted
- `git diff --check`: passed

## Definition of Done

- [x] Structured transcripts
- [x] Deterministic transcript analysis
- [x] Selective secondary transcription
- [x] Structured uncertainty
- [x] Safe correction stage
- [x] Blocking Transcript-QA
- [x] Story completeness and traceability
- [x] Number/date/result checks
- [x] Glossary checks
- [x] Independent LLM QA
- [x] Critical findings block
- [x] Bounded targeted reruns
- [x] Narration lock
- [x] Hash-based idempotency and selective stale cascade
- [x] Episode Cost Guard before paid calls
- [x] CLI operations
- [x] Web QA/review operations
- [x] Stored v1 compatibility
- [x] Manual final publish gate

## Known limitations and plan deviations

- Secondary ASR intentionally checks suspicious regions rather than performing
  a second full transcription; this limits cost and preserves independent
  evidence where risk is highest.
- Conservative entity/negation/semantic checks produce findings rather than
  claiming every wording difference is wrong. Human review remains necessary
  for ambiguous names, roles, legal claims, and complex paraphrases.
- Chunked primary transcription still uses a shared per-audio temporary
  directory outside the normal host-wide pipeline lock, and chapter sidecars
  are not yet written through the shared atomic gate writer. These are
  confirmed minor hardening items, not open QA-gate or publishing blockers.
- Some profile-resolution paths retain broad fail-safe exception handling, and
  prompt frontmatter still contains a historical correction-model label.
  Runtime provider/model routing and provenance remain configuration-driven.
- No heavy NER dependency or new QA database tables were introduced. Existing
  JSON artifacts and review/audit models were sufficient and safer for SQLite
  compatibility.
- Story schema extends the existing story model instead of creating a parallel
  model; legacy fields therefore remain alongside the new traceability fields.
- Translation QA is integrated into the existing review-gate orchestration
  rather than exposed as a separate episode status, avoiding a risky status
  migration.
- Publishing remains deliberately manual even when intermediate reviews are
  auto-approved.
