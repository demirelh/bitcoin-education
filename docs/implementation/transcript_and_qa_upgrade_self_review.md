# Adversarial Self-Review: Transcript and QA Upgrade

Scope: complete diff from `8809796^` through `ae6d30e`.

The review assumed that the implementation contained subtle failures. It
covered pipeline reachability and order, stored v1 compatibility, profiles,
configuration precedence, migrations/SQLite, idempotency, stale propagation,
cost/retry behavior, reviews/findings, transcript and translation QA, narration
locking, publishing, CLI/web operations, tests, and documentation.

## critical

No verified critical finding was found. In particular:

- critical deterministic findings are retained during LLM merging;
- retry generation/history survives forced reruns;
- `tagesschau_tr` cannot auto-publish;
- chapterize is called only after the translation quality-gate check;
- stored `pipeline_version=1` episodes remain loadable in the tested
  compatibility path.

## major

### SR-001: Review artifact hashing fails open on missing files

- **File:** `btcedu/core/reviewer.py`
- **Function/class:** `_compute_artifact_hash`, `approve_review`,
  `review_task_matches_artifacts`, `refresh_review_task_artifacts`
- **Problem:** `_compute_artifact_hash()` silently skips paths that no longer
  exist. A task can therefore be created or approved against a hash of only a
  subset of its declared artifacts.
- **Impact:** A transcript or translation review can remain current after a
  required corrected transcript, structured document, gate, or render binding
  disappears.
- **Concrete correction:** Require every declared artifact to be a regular
  file and raise a clear validation error when any path is absent.
- **Required test:** Task creation and approval with one missing artifact must
  fail; current-artifact matching must return false when a bound path is
  removed.

### SR-002: Transcription idempotency accepts an incomplete artifact set

- **File:** `btcedu/core/transcriber.py`
- **Function/class:** `transcribe_episode`
- **Problem:** The non-force skip path checks only
  `transcript.clean.de.txt`. It can skip while
  `transcript.structured.de.json` or transcribe provenance is missing.
- **Impact:** Downstream stages silently fall back to zero-timestamp legacy
  segmentation and lose provider/model/cost traceability.
- **Concrete correction:** Skip only when clean, structured, and provenance
  artifacts all exist and the structured document validates. Otherwise rerun
  transcription or fail explicitly when audio is unavailable.
- **Required test:** Delete the structured artifact or provenance after a
  successful transcription and verify that the stage does not report the
  incomplete cache as current.

### SR-003: Existing but corrupt quality gate can be treated as absent

- **File:** `btcedu/core/chapterizer.py`
- **Function/class:** `_enforce_translation_quality_gate`,
  `_enforce_narration_lock`
- **Problem:** `load_quality_gate(..., strict=True)` returns `None` for both a
  missing and an invalid gate. When effective QA is not required, an existing
  corrupt gate is treated like no gate and chapterize can continue.
- **Impact:** A previously gated episode can bypass its narration approval
  after gate corruption or a partial JSON write.
- **Concrete correction:** Distinguish “file absent” from “file exists but is
  invalid”; always fail closed for an existing invalid gate.
- **Required test:** Write malformed `translation_quality_gate.json` for a
  profile with QA currently disabled and assert chapterize fails before
  writing chapters or advancing status.

### SR-004: QA web requests can mutate review state before validation

- **File:** `btcedu/web/api.py`
- **Function/class:** `approve_transcript_qa`,
  `request_changes_transcript_qa`,
  `_get_or_create_transcript_qa_task`
- **Problem:** Approval creates/commits a pending `ReviewTask` before parsing
  and validating `quality_rating`. Nonnumeric values can raise an uncaught
  exception; invalid ratings can leave a task behind despite a failed request.
  Non-object JSON also causes attribute errors.
- **Impact:** Malformed requests produce persistent review state and possible
  HTTP 500 responses.
- **Concrete correction:** Require a JSON object, parse and validate all fields
  before task creation, then perform task creation and decision as one
  operation.
- **Required test:** Invalid content type, JSON arrays, nonnumeric ratings, and
  out-of-range ratings return 4xx and create no `ReviewTask`.

### SR-005: New QA mutation endpoints lack a cross-site request barrier

- **File:** `btcedu/web/api.py`, `btcedu/web/static/app.js`
- **Function/class:** transcript QA approve/request-changes and finding-status
  routes
- **Problem:** The endpoints accept form-compatible POST requests without
  requiring JSON. With browser-cached HTTP Basic credentials, a cross-origin
  form can attempt a state-changing request.
- **Impact:** An operator review decision could be triggered from another
  origin.
- **Concrete correction:** Require `application/json` and a JSON object for
  the new QA mutation endpoints. This follows the dashboard's existing JSON
  client behavior and blocks simple cross-origin form submissions without
  introducing a new framework.
- **Required test:** Form-encoded and empty-body POST requests return 415/400
  and do not change review/finding state.

### SR-006: Finding updates are non-atomic and lose concurrent history

- **File:** `btcedu/core/qa_reviewer.py`, `btcedu/web/api.py`
- **Function/class:** `update_finding_status`,
  `update_translation_finding_status`
- **Problem:** Concurrent requests read, modify, and overwrite the complete
  `translation_quality_gate.json` without serialization. JSON is written
  directly to the authoritative path.
- **Impact:** Last-writer-wins can discard another finding status and its audit
  event; interruption can leave truncated JSON and block or bypass later work.
- **Concrete correction:** Serialize per-episode finding mutations with a file
  lock and persist the authoritative gate through temp-file + `os.replace`.
  Use the same atomic writer for generated gates.
- **Required test:** Two synchronized finding mutations both survive with both
  history events; simulated write replacement leaves either the old or new
  valid document, never partial JSON.

### SR-013: Malformed transcription provenance can crash cache validation

- **File:** `btcedu/core/transcriber.py`
- **Function/class:** `transcribe_episode`
- **Problem:** Valid JSON with an unexpected shape, such as an array or a
  non-list `output_files` value, reaches dictionary/list operations that raise
  uncaught `AttributeError` or `TypeError`.
- **Impact:** A damaged cache causes the transcribe stage to crash instead of
  being rejected and regenerated from the source audio.
- **Concrete correction:** Validate that provenance is an object and that
  `output_files` is a list containing only strings before constructing paths.
- **Required test:** Array provenance, scalar `output_files`, and non-string
  path entries must all trigger regeneration rather than an exception.

### SR-014: Derived QA write failure can leave the review task stale

- **File:** `btcedu/core/qa_reviewer.py`, `btcedu/web/api.py`
- **Function/class:** `update_finding_status`,
  `update_translation_finding_status`
- **Problem:** The authoritative gate is replaced before legacy JSON/Markdown
  projections. If a projection write raises, the request aborts before the
  review task is rebound to the new authoritative gate hash.
- **Impact:** The finding mutation is durable but subsequent authorized
  mutations are rejected as obsolete.
- **Concrete correction:** Keep the quality gate authoritative, log individual
  projection refresh failures, and allow the endpoint to refresh the review
  task binding after the durable mutation.
- **Required test:** Simulate failure of `qa_review.json`, assert the request
  still succeeds, and verify a subsequent mutation remains authorized.

## minor

### SR-007: Contradiction summary is not schema-validated

- **File:** `btcedu/models/qa_schema.py`
- **Function/class:** `QualityGateDocument.validate_summary`
- **Problem:** `summary.contradiction_count` is not checked against finding
  flags.
- **Impact:** Corrupt or stale display/audit metrics can load as valid.
- **Concrete correction:** Validate the count against all findings.
- **Required test:** A mismatched contradiction count must fail model
  validation.

### SR-008: `run-latest` conflates lock contention with no work

- **File:** `btcedu/core/pipeline.py`, `btcedu/cli.py`
- **Function/class:** `run_latest`, `run_latest_cmd`
- **Problem:** Both lock contention and “no pending episode” return `None`.
- **Impact:** A systemd invocation exits successfully even though pending work
  may have been skipped until the next timer.
- **Concrete correction:** Propagate or return a distinct busy result and make
  CLI output/exit status explicit.
- **Required test:** Hold the pipeline lock, invoke `run-latest`, and assert a
  nonzero busy result distinct from an empty queue.

### SR-009: Operator stage names are abstracted in the README

- **File:** `README.md`
- **Function/class:** pipeline diagram
- **Problem:** Generic labels such as `[quality gate]` do not map directly to
  logged stage names.
- **Impact:** Minor operational confusion during incident diagnosis.
- **Concrete correction:** Include the actual review-gate stage names.
- **Required test:** Documentation-only; no automated test required.

### SR-010: Raspberry Pi values are recommendations, not defaults

- **File:** `CLAUDE.md`, `.env.example`, `README.md`
- **Function/class:** render configuration documentation
- **Problem:** Pi production tuning (`ultrafast`, longer timeout) is adjacent to
  generic defaults (`medium`, 300 seconds) without a consistent label.
- **Impact:** Operators may mistake recommendations for effective defaults.
- **Concrete correction:** Label deployment tuning explicitly.
- **Required test:** Documentation-only.

### SR-011: Fractional review ratings are truncated

- **File:** `btcedu/web/api.py`
- **Function/class:** `_qa_quality_rating`
- **Problem:** Converting arbitrary numeric JSON with `int()` accepts `1.5` as
  rating `1`.
- **Impact:** Incorrect audit ratings can be persisted.
- **Concrete correction:** Accept only integer JSON values or strict
  single-digit integer strings from 1 through 5.
- **Required test:** Fractional ratings on approve and request-changes return
  400 and create no task.

### SR-012: Derived QA files can race with finding updates

- **File:** `btcedu/core/qa_reviewer.py`
- **Function/class:** `update_finding_status`
- **Problem:** The authoritative gate was locked, but legacy JSON/Markdown was
  refreshed after releasing the lock.
- **Impact:** Concurrent regeneration could leave display artifacts describing
  a different generation than the authoritative gate.
- **Concrete correction:** Refresh authoritative and derived gate outputs
  atomically while holding the same per-episode lock.
- **Required test:** Concurrent generation/mutation must not overwrite newer
  derived status output.

## accepted trade-offs

- External APIs remain mocked in automated tests. Provider adapters are tested
  at their boundaries; no paid network call is part of CI.
- Secondary ASR intentionally covers suspicious regions rather than a full
  second transcription.
- Conservative entity, negation, and Turkish suffix heuristics prefer findings
  over automatic semantic claims; some false positives require review.
- Existing stored v1 episodes have a compatibility path, but new v1 profile
  definitions are intentionally unsupported.
- A host-wide `flock` serializes pipeline batch entrypoints. This is suitable
  for the single-host Raspberry Pi deployment, not a distributed worker fleet.
- The application currently relies on reverse-proxy authentication and does
  not implement a project-wide CSRF token framework. The self-review limits
  the correction to the newly added QA mutation routes.
- Many older JSON sidecars use direct writes. The authoritative translation
  quality gate receives atomic writes in the recommended fix; converting every
  historical artifact is outside this upgrade.
- A transcription cache hit now resolves the effective profile/configuration
  before accepting provenance. If profile resolution is broken, the stage
  fails rather than trusting an unverifiable cache.

## missing requirements

No agreed transcript/QA feature is completely absent. Partial operational
gaps are:

- no project-wide CSRF mechanism;
- no configured static type checker;
- not every historical JSON artifact uses atomic replacement.

## insufficient tests

### IT-001: Complete v2 orchestration test patches `_run_stage`

- **File:** `tests/test_phase9_pipeline_integration.py`,
  `tests/test_pipeline.py`
- **Problem:** The complete dry-run test simulates status advancement by
  replacing `_run_stage`.
- **Impact:** A stage-name/import/status wiring regression can evade the
  “complete pipeline” test even though each stage has focused tests.
- **Concrete correction:** Add a wiring smoke that keeps the real
  `run_episode_pipeline()` and `_run_stage()` dispatch while stubbing only
  provider/stage boundaries.
- **Required test:** Assert the real dispatcher reaches transcript analysis,
  verification, transcript QA, segment, translation QA gate, narration lock,
  and downstream stages in profile order.

### IT-002: No concurrency regression for finding lifecycle writes

- **File:** `tests/test_quality_gate.py`, `tests/test_web_qa_actions.py`
- **Problem:** Finding history tests are sequential.
- **Impact:** Lost updates in threaded gunicorn execution were not detected.
- **Concrete correction:** Add a barrier-based concurrent mutation test.
- **Required test:** Both independent finding updates and history entries
  persist.

### IT-003: Corruption recovery is not tested at every authoritative boundary

- **File:** transcript, quality-gate, and chapterize tests
- **Problem:** Some corrupt-cache tests exist, but partial transcript caches
  and corrupt gate fail-closed behavior were missing.
- **Impact:** Cache checks can silently degrade to legacy behavior.
- **Concrete correction:** Add the tests required by SR-002 and SR-003.

## recommended fixes

Apply in this order:

1. Make artifact hashing fail closed and update review/web tests.
2. Validate QA request bodies before creating tasks and require JSON.
3. Make quality-gate writes atomic and serialize finding mutations.
4. Fail closed on an existing invalid gate.
5. Strengthen transcription cache completeness.
6. Validate contradiction counts.
7. Distinguish pipeline busy from no work.
8. Add focused corruption, concurrency, request-validation, and dispatch tests.
9. Align the minor documentation labels.
10. Validate transcription provenance shapes and make legacy QA projections
    best-effort after authoritative gate writes.

## resolution status

Fixed in the self-review correction:

- SR-001 through SR-014
- IT-002 concurrent finding/history coverage
- IT-003 incomplete transcript cache and corrupt-gate coverage

Intentionally remaining:

- IT-001: the complete v1/v2 dry-run still replaces `_run_stage`. Real stage
  functions and dispatcher branches are covered separately, but there is no
  single provider-boundary-only end-to-end v2 test. Adding one would require a
  large synthetic media/LLM fixture and is retained as a test-infrastructure
  improvement rather than a production correctness blocker.

## final validation

- `pytest -x -q`: 1,522 passed in 110.97 seconds.
- Changed Python files: Ruff check and format check passed.
- `ruff check .`: unchanged unrelated baseline of 18 findings in older frame,
  thumbnail, credits, image-provider, and migration code.
- `ruff format --check .`: unchanged unrelated baseline of 31 older files.
- `git diff --check`: passed.
