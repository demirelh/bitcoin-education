# Combined Validation Report (Phases 1–5)

**Reviewer:** Claude Opus (repository-wide validation pass)
**Date:** 2026-03-15
**Scope:** btcedu phases 1–5 combined — review UX, pipeline progress UI, smart stock ranking, stock video support, granular review actions
**Evidence basis:** codebase, test suite, all five phase implementation outputs, all five phase validation outputs, MASTERPLAN.md, CLAUDE.md

---

## 1. Executive Verdict

**PASS WITH MINOR CAVEATS**

All five phases are individually implemented correctly and verified by their per-phase validation outputs. The phases are coherent together: no later phase breaks an earlier one, and the cross-phase integration is sound. No blocking defects were found in code, tests, or data flows.

**Is the repository production-ready for current internal workflow?** Yes — with the caveats below.

The system is ready for internal use in the following workflows:
- Reviewing pipeline pauses and understanding episode state (Phases 1 + 2)
- Selecting better stock images with semantic intent awareness (Phase 3)
- Using video clips for b_roll chapters on opt-in (Phase 4, requires activation)
- Making granular per-item correction/adaptation decisions before whole-review approval (Phase 5)

The three caveats are: (1) Phase 4 video rendering path requires live Pi hardware smoke-test before trusting in production — ffmpeg pipeline is unit-tested but not integration-tested end-to-end; (2) one documentation error in the Phase 5 implement output (test count stated as "was 629" but actual baseline was 814); (3) intent extraction cost (Phase 3) is not tracked in the DB/LLM report. None are blocking.

---

## 2. Validation Method

### Sources compared

| Source | Weight |
|--------|--------|
| Actual code (read directly) | Highest |
| Actual tests (read directly) | Highest |
| Actual data flows (traced cross-module) | High |
| Per-phase validation outputs | High — used as checklist, not taken at face value |
| Per-phase implementation outputs | Medium — reference for intended behavior |
| MASTERPLAN.md / CLAUDE.md | Medium — architectural reference |

### How code vs. docs vs. tests were weighted

Code is authoritative. Where implementation outputs or validation outputs claim something is implemented, it was verified against the actual files. All cross-phase integration claims were traced through code, not inferred from documentation.

Documentation errors found: the Phase 5 implementation output states "was 629 before Phase 5" for the prior test count. The actual pre-Phase-5 baseline (Phase 4 exit count) was 814. This is a documentation error only; the test suite itself shows 853 passing after Phase 5.

### Assumptions

- Test suite passes as claimed (all phases exit with green suite; spot-checked assertions for key behaviors)
- No additional uncommitted changes exist beyond what git shows
- Pi hardware (Raspberry Pi 6.12 kernel) is the target deploy environment for the full pipeline including ffmpeg video encoding

---

## 3. Phase-by-Phase Summary

### Phase 1 — Review UX Improvements

**Expected scope:** `review_context` + `pipeline_state` on episode API, `⏸ review` badge on list, "Next Action" block on detail, `jumpToReview()` navigation, "Paused for review" filter, batch pending-task query.

**Actual implementation status:** Fully implemented. `_get_review_context()` returns structured review state. `_compute_pipeline_state()` derives high-level string. Batch query avoids N+1. UI components (badge, Next Action block, filter option, jump navigation) all present in `app.js`. 25 tests in `test_web_review_ux.py`.

**Validation status:** PASS (individual report: PASS, no required fixes)

**Confidence:** High. Code read directly. All 8 claimed deliverables verified in source.

**Residual observations:**
- `pipeline_state` does not distinguish idle from in-flight ("running" vs. "ready" both map to "ready") — documented non-goal, requires JobManager integration
- Approved-task cache in the list endpoint is absent — extra per-episode query on approved-at-gate-status episodes. Low risk at current scale.

---

### Phase 2 — Pipeline Progress UI

**Expected scope:** `stage_progress` field on episode API, `_build_stage_progress()` helper, batch duration query, `renderPipelineStepper()` JS, `formatDuration()` helper, stepper CSS, 23 new tests.

**Actual implementation status:** Fully implemented. `_STAGE_LABELS` (17 keys), `_STAGE_TO_PIPELINE_STAGE` (13 non-gate entries) in `api.py`. `_build_stage_progress()` handles all 5 UI states (done/active/pending/paused/failed) plus review context override. Batch duration query on `list_episodes()`. JS stepper renders connector lines, blob states, summary. 23 tests in `test_web_progress.py`.

**Validation status:** PASS (individual report: PASS, no required fixes)

**Confidence:** High. All stage labels and state-mapping logic read directly from `api.py`.

**Residual observations:**
- `COST_LIMIT` failure branch exercised by same code path as `FAILED` but not independently tested. Non-blocking.
- `"skipped"` state is counted in `completed_count` but never emitted — dead check, harmless.

---

### Phase 3 — Smart Stock-Image Ranking

**Expected scope:** `_TR_TO_EN` expansion (15 terms), `extract_chapter_intents()` with LLM + idempotency, `_validate_and_adjust_selection()` post-rank rule layer, cross-chapter dedup, updated `stock_rank.md` prompt, UI intent tags and trap warnings, manifest schema 3.0, 23 new tests.

**Actual implementation status:** Fully implemented. Three-layer defense against literal-trap matches (translation → LLM prompt → Python rule). `_validate_and_adjust_selection()` correct for trap demotion, dedup, and no-swap-needed cases. Intent extraction idempotent via `chapters_hash`. 23 tests in `test_smart_ranking.py` with three named regression tests reproducing real failure scenarios.

**Validation status:** PASS (individual report: PASS, no required fixes)

**Confidence:** High. Key functions traced in `stock_images.py`.

**Residual observations:**
- `extract_chapter_intents()` builds its LLM prompt inline rather than rendering `intent_extract.md` via `PromptRegistry`. This means intent extraction cost is not tracked in `PromptVersion` DB records or the `btcedu llm-report` output. The cost is captured in `intent_analysis.json` per-episode but not in the centralized DB cost report. A real cost gap for observability.
- `stock_rank.md` template also bypassed at runtime (inline prompt in `rank_candidates()`). Both template files exist as documentation but are not rendered. Cosmetically inconsistent with how `correct_transcript.md`, `translate.md`, etc. are used.

---

### Phase 4 — Stock Video B-Roll Support

**Expected scope:** `pexels_video_enabled` config (default False), `PexelsVideoFile/Video/SearchResult` dataclasses, `search_videos()`, `normalize_video_clip()`, `create_video_segment()`, `_resolve_chapter_media()` 4-tuple, renderer branching on `asset_type`, `/stock/candidate-video` API endpoint, UI video preview, schema 3.1, 32 new tests.

**Actual implementation status:** Fully implemented as opt-in feature. Config default safe. Search guard triple-checked (config + visual type + locked). ffmpeg commands verified by tests: `-stream_loop -1` before input path, `-map 1:a` for TTS audio, `-an` in normalize. Render cache invalidation on `asset_type` change. Security on the new API endpoint mirrors existing patterns. 32 tests in `test_stock_video.py`.

**Validation status:** PASS (individual report: PASS, no required fixes)

**Confidence:** High for code logic. **Medium for operational readiness**: ffmpeg normalization and render pipeline for video segments is unit-tested with mocked commands but NOT end-to-end integration tested on actual video files. Pi hardware performance for H.264 encoding is untested.

**Residual observations:**
- Double H.264 transcode (normalize → create_video_segment) is a known generation loss, accepted as safe. Worth revisiting if quality issues appear.
- `finalize_selections()` normalization failure path (placeholder fallback) is not tested.
- Duration filter boundary (`>` not `>=`) has no boundary test.

---

### Phase 5 — Granular Review Actions

**Expected scope:** `ReviewItemDecision` model, migration 007, `item_id` in correction and adaptation diffs, `_ensure_item_ids_*` backward compat, `upsert/get/apply_item_decisions()`, word-level correction assembly, char-level adaptation assembly, 5 new API endpoints, per-item UI actions, summary bar, inline edit, Apply button, 46 new tests.

**Actual implementation status:** Fully implemented. Pending-as-accepted behavior explicit at three layers: function docstring, API response (`pending_count`), UI toast. Whole-review flow completely untouched. Backward compat for old diffs and old tasks verified. 46 tests across 4 new test files + 3 modified existing files.

**Validation status:** PASS (individual report: PASS, no required fixes)

**Confidence:** High. All key functions read directly from `reviewer.py`, `api.py`, `app.js`.

**Residual observations:**
- `apply_item_decisions()` lacks `_validate_actionable()` at the function level (guard is at API layer only). Asymmetric with `upsert_item_decision()`. Non-blocking for web use, notable for direct Python caller.
- Duplicate `review_task_id` index (column-level `index=True` + named `Index` in `__table_args__`) creates one extra index in test path vs. production migration path. Harmless.
- No adaptation-stage `apply` end-to-end API test.
- Phase 5 implement output contains documentation error: "was 629 before Phase 5" — should be "was 814".

---

## 4. Cross-Phase Integration Review

### Phase 1 ↔ Phase 2 (Review UX ↔ Progress UI)

**Integration point:** Both phases add fields to `_episode_to_dict()`. Phase 1 adds `review_context` + `pipeline_state`; Phase 2 adds `stage_progress`. Phase 2 receives `review_context` as a pre-computed argument to `_build_stage_progress()` — the two features are composed cleanly without coupling.

**Review gate states in stepper:** The `stage_progress` correctly reflects review context via the override block in `_build_stage_progress()`. Gate stages become "paused" when `review_context.state == "paused_for_review"` and "done" when approved. The "Next Action" block (Phase 1) and the stage stepper (Phase 2) display complementary information at different granularities. **No conflict.** ✓

**Phase 1 Next Action + Phase 2 stepper placement:** In `selectEpisode()`, Phase 2 stepper is inserted between `detail-meta` and Phase 1's `renderNextAction()`. The layout is: metadata → stepper → next-action. This is the intended ordering from the Phase 2 plan. ✓

**Batch query stacking:** The `list_episodes()` endpoint now runs three batch queries: pending ReviewTasks (Phase 1), duration-cache PipelineRuns (Phase 2), and all successful runs for stage progress (Phase 2). Each is a single SELECT. The combined overhead is three additional queries per list request, which is acceptable. ✓

---

### Phase 2 ↔ Phase 3 (Progress UI ↔ Smart Ranking)

**Integration point:** Phase 2 shows the `imagegen` stage duration in the stepper. Phase 3 adds intent extraction as a sub-step within the imagegen stage. Since intent extraction runs inside the same `imagegen` pipeline stage, its duration is folded into the single `imagegen` PipelineRun record. The stepper shows total imagegen time including intent extraction — correct behavior. No separate stage is needed for intent extraction. ✓

**UI panels are independent:** Phase 2 adds the pipeline stepper on the episode detail panel. Phase 3 adds intent tags and trap warnings to the stock candidates panel. These are entirely separate parts of the SPA and do not interfere. ✓

**`review_gate_stock` handled correctly:** `_V2_STAGES` has `("review_gate_stock", EpisodeStatus.CHAPTERIZED)` as a named stage. `_STAGE_LABELS` in Phase 2's api.py includes `"review_gate_stock": "Review Stock"`. Phase 2 test `test_v2_stage_progress_all_stages_present` verifies `review_gate_stock` appears in the expected 14-stage order. The stock image review gate flows through the Phase 1 review context system (`_REVIEW_GATE_STATUS_MAP["chapterized"] = "stock_images"` → `_REVIEW_GATE_LABELS["stock_images"] = ("review_gate_stock", "Stock Image Review")`). All three UI layers (Phase 1 badge, Phase 1 Next Action, Phase 2 stepper) correctly represent the stock review gate. ✓

---

### Phase 3 ↔ Phase 4 (Smart Ranking ↔ Stock Video)

**Integration point:** Phase 4 adds video candidates to the same `candidates` list per chapter. Phase 3's `_validate_and_adjust_selection()` is applied to all candidates in this pool.

**Trap check on video candidates:** The trap check examines `candidate["alt_text"]`, which is set for video candidates from the Pexels video `description` field. The trap logic is asset-type-agnostic and works correctly for video candidates. ✓

**Cross-chapter dedup on video candidates:** The `selected_so_far` set tracks `pexels_id` values. Video candidate IDs use the `pexels_v_{id}` prefix, making them namespace-separated from photo IDs. Cross-chapter dedup works correctly for the mixed pool: the same video cannot be selected twice, and a video selection does not create a false-positive collision with a photo. ✓

**Manifest schema version:** Phase 3 writes `schema_version: "3.0"` for the candidates manifest. Phase 4 writes `schema_version: "3.1"` when video candidates are present. Both phases use the same manifest file; Phase 4 bumps the minor version correctly. The test in Phase 3 (`test_manifest_schema_version_3`) was updated to `"3.0"`, and Phase 4's `TestManifestSchemaVideo` tests the `"3.1"` bump separately. No regression. ✓

**Ranking prompt:** Phase 4 adds `asset_type` and `duration_seconds` to the candidate data and appends a motion preference hint for b_roll chapters to `stock_rank.md`. Phase 3's semantic intent block, disallowed_motifs, and literal_traps are preserved in the updated prompt. Phase 4 tests `TestMixedCandidateRanking` verify that `asset_type` appears in the prompt and that motion hints are present for b_roll but absent for diagrams. ✓

**Phase 3 tests with Phase 4 present:** `test_stock_ranking.py` has an autouse `mock_extract_intents` fixture (added in Phase 3) that patches intent extraction. Phase 4 tests (`test_stock_video.py`) mock the video search separately. The two test files do not interfere. ✓

**One gap to note:** Phase 3's `TestValidateAndAdjustSelection` tests do not include a video candidate in the test pool. The trap swap logic for a video candidate (i.e., the first-ranked video is a trap → demote to first clean candidate, which might be a photo) is not directly tested. Given the code is asset-type-agnostic (it only checks `alt_text` and `candidate["selected"]`), this gap is low risk. See Section 5.

---

### Phase 4 ↔ Phase 5 (Stock Video ↔ Granular Review)

**Integration point:** Phase 5 granular review applies only to `stage == "correct"` and `stage == "adapt"`. The stock video assets and the `render` review gate are explicitly excluded:
- `apply_item_decisions()` raises `ValueError` for any stage other than "correct" or "adapt"
- The "Apply Accepted Changes" button in JS is only shown for `data.stage === "correct" || data.stage === "adapt"`
- The `review_gate_stock` and `review_gate_3` tasks show the diff viewer path only when `data.diff` exists; stock and render reviews have no diff file

**No conflict.** Phase 5 granular review is orthogonal to Phase 4 video assets. ✓

**Sidecar pickup and video pipeline:** The Phase 5 correction sidecar (`transcript.reviewed.de.txt`) is consumed by `translator.py` (translation stage), which is well upstream of imagegen/video rendering. The Phase 5 adaptation sidecar (`script.adapted.reviewed.tr.md`) is consumed by `chapterizer.py`, also upstream of imagegen. Neither sidecar touches the render stage. The video rendering path (Phase 4) is not affected by Phase 5 sidecars. ✓

---

### End-to-End Coherence Across All Five Phases

A complete reviewer workflow from CORRECTED status through publication works as follows with all five phases active:

1. Episode paused at `review_gate_1` (CORRECTED status) →
   - **Phase 1:** `⏸ review` badge on list; "Pipeline paused — review gate 1 requires approval" Next Action block; `jumpToReview()` available
   - **Phase 2:** Stepper shows `correct=done`, `review_gate_1=paused`, rest pending; "1/14 stages complete"
   - **Phase 5:** Review detail page shows diff rows with Accept/Reject/Edit/Reset buttons; reviewer can apply granular decisions; then whole-approve

2. `review_gate_stock` (CHAPTERIZED status) →
   - **Phase 1:** Same badge and Next Action block for stock review context
   - **Phase 2:** `imagegen=done`, `review_gate_stock=paused`; Phase 3 intent tags visible in stock panel; Phase 4 video previews visible if enabled
   - **Phase 3:** Intent tags, trap warnings on candidate thumbnails
   - **Phase 4:** Video clip previews with duration badge if `pexels_video_enabled=True`
   - **Phase 5:** No granular review for stock gate (correct by design)

3. Approved review → pipeline resumes → renderer picks up correct `asset_type` from manifest → TTS audio maps correctly → render succeeds → `review_gate_3` pause →
   - **Phases 1+2** handle the render gate state display identically

This end-to-end flow is coherent. All state transitions, UI representations, and data flows are consistent across phases.

---

## 5. Remaining Risks or Inconsistencies

### Non-blocking risks

| # | Risk | Phases | Severity | Blocking? |
|---|------|--------|----------|-----------|
| 1 | Phase 4 video render path (normalization → `create_video_segment`) is unit-tested with mock ffmpeg but not end-to-end tested with real MP4 files on Pi hardware | 4 | Medium | No |
| 2 | Intent extraction cost (Phase 3) not tracked in `PromptVersion` DB / `btcedu llm-report` | 3 | Low | No |
| 3 | `stock_rank.md` and `intent_extract.md` templates exist but are bypassed at runtime (inline prompts) | 3 | Low | No |
| 4 | `pipeline_state` does not distinguish running from idle ("running" returns "ready") | 1 | Low | No |
| 5 | `apply_item_decisions()` lacks function-level `_validate_actionable()` guard (API layer guards it) | 5 | Negligible | No |
| 6 | Duplicate `review_task_id` index in `ReviewItemDecision` model (ORM + migration) | 5 | Negligible | No |
| 7 | Phase 3 trap-swap not tested with video candidate in the mixed pool | 3+4 | Low | No |
| 8 | `finalize_selections()` normalization failure path (Phase 4) not tested | 4 | Low | No |
| 9 | Phase 5 implement output documentation error: "was 629 before Phase 5" should be "was 814" | 5 | Negligible | No |
| 10 | No adaptation-stage `apply` end-to-end API test (Phase 5) | 5 | Low | No |

---

## 6. Test and Validation Quality Review

### Test count evolution

| Phase | Baseline | After | New tests |
|-------|----------|-------|-----------|
| Phase 1 | ~711 | 736 | ~25 |
| Phase 2 | 736 | 759 | 23 |
| Phase 3 | 759 | 782 | 23 |
| Phase 4 | 782 | 814 | 32 |
| Phase 5 | 814 | 853 | 39 |
| **Total** | | **853** | |

Note: Phase 1 baseline is inferred (Phase 2 opened at 736). Phase 5 implement output contains a documentation error claiming the Phase 5 baseline was 629; the actual baseline was 814.

### Coverage strengths

- **Phase 3 regression tests** are the standout quality win: three named tests (`barbershop`, `pressure_cooker`, `soap_bubbles`) directly reproduce real failure scenarios with realistic Pexels alt_text strings. This is the right test pattern for a literal-trap protection feature.
- **Phase 4 ffmpeg flag tests** directly assert critical command arguments (`-stream_loop -1` before video path, `-map 1:a`, `-an`) — high-value correctness coverage that would catch subtle ordering bugs.
- **Phase 5 assembly tests** use `_FakeDecision` plain objects to avoid SQLAlchemy instrumentation — pragmatic and correct. The full action matrix (accept/reject/edit/pending/unchanged) is covered for both correction and adaptation assembly.
- **Backward compatibility tests** are present in all five phases, covering old manifests, old diffs, v1 episodes, and missing optional fields.

### Coverage gaps (across phases)

| Gap | Phase | Priority |
|-----|-------|----------|
| Phase 4 video normalization failure path (placeholder fallback) | 4 | P1 |
| Phase 3 trap validation with a video candidate in pool | 3+4 | P2 |
| Phase 5 adaptation `apply` API end-to-end | 5 | P1 |
| Phase 5 `edit` → `accept` clears `edited_text` in DB | 5 | P2 |
| Phase 2 `COST_LIMIT` state in failure override | 2 | P2 |
| All phases: no JS unit tests | 1–5 | P2 |
| Phase 4 locked chapter + video enabled interaction | 4 | P2 |

### Are validation outputs reliable?

The five individual validation reports are **generally reliable** and evidence-based. They read actual code rather than documentation. All five correctly identified residual gaps as non-blocking and gave accurate verdicts.

One concern: all five validation reports were produced by the same automated reviewer (Claude Sonnet 4.6). They share the same validator blindspots — in particular, they do not run the code, they read it. The lack of live execution means subtle runtime behaviors (ffmpeg command construction with real files, SQLAlchemy mapper resolution edge cases, actual Flask response formatting) are assessed by inspection only.

The Phase 3 and Phase 4 validations are slightly optimistic about the `stock_rank.md` / `intent_extract.md` template situation (called "cosmetically awkward" / "not a correctness issue") — but for a system that prides itself on prompt versioning and LLM cost tracking, bypassing PromptRegistry for two production LLM calls is a meaningful observability gap worth treating as P1, not P2.

---

## 7. Production Readiness Assessment

### Ready now

- **Review UX (Phase 1):** Badge, Next Action block, filter, jump-to-review — all functional. Covers all four review gates (correct, adapt, stock, render). Zero risk.
- **Pipeline progress UI (Phase 2):** Stage stepper with all 14 v2 stages, correct state transitions, batch queries. Ready for daily use.
- **Smart stock ranking (Phase 3):** Three-layer trap defense, cross-chapter dedup, semantic intent extraction. The barbershop regression is fixed at all layers. Ready for production image selection.
- **Granular review actions (Phase 5):** Item-level accept/reject/edit/reset for correction and adaptation reviews, sidecar assembly, downstream sidecar detection. Ready for use in the correction review workflow.

### Ready with caution

- **Stock video B-roll (Phase 4, `pexels_video_enabled=True`):** Code is correct and unit-tested. However, the actual ffmpeg video normalization + render pipeline on a Raspberry Pi with real MP4 clips has not been end-to-end validated. Recommend running a full manual smoke test (one episode, one b_roll chapter, verify the final MP4 looks correct) before relying on it in regular production. The feature is safely off by default — turning it on requires explicit opt-in.

### Not ready yet

- **`pipeline_state: "running"` distinction:** Episodes that are actively processing and episodes that are idle-but-runnable both show `pipeline_state: "ready"`. This is a known non-goal, not a regression. The UI cannot distinguish "running now" from "ready to run."
- **Intent extraction LLM cost in `btcedu llm-report`:** Cost for intent extraction (`extract_chapter_intents()`) is captured in `intent_analysis.json` per-episode but not in the centralized DB LLM report. If LLM cost tracking matters for operations, this is an active gap.

---

## 8. Recommended Next Steps

### P0 — Must fix

None. No blocking defects found in any of the five phases.

---

### P1 — Should improve

**P1-1: Smoke-test Phase 4 video render end-to-end on Pi hardware**
Enable `pexels_video_enabled=True` for one episode, run the full pipeline (imagegen → stock select → finalize → render), verify the output MP4 plays correctly. This is the one path that is unit-tested but not live-validated.

**P1-2: Register intent extraction in PromptRegistry**
`extract_chapter_intents()` calls Claude but bypasses `PromptRegistry`, meaning cost is invisible to `btcedu llm-report`. Add a `PromptVersion` registration for `intent_extract.md` so the LLM cost report includes intent extraction. This is the same pattern used by all other Claude-calling stages.

**P1-3: Add Phase 5 adaptation `apply` API test**
`test_apply_corrections` only tests the `correct` stage. Add `test_apply_adaptation` to exercise the full `apply_item_decisions()` → `_assemble_adaptation_review()` → sidecar write path via the API endpoint.

**P1-4: Add Phase 4 normalization failure path test**
Add a test where `normalize_video_clip()` raises and verify the `finalize_selections()` placeholder fallback is triggered. This guards the graceful degradation path that would be hit if ffmpeg is absent or produces an error.

---

### P2 — Nice to have

**P2-1: Render `stock_rank.md` and `intent_extract.md` via PromptRegistry**
Both templates exist but are bypassed by inline prompt construction. Rendering them through Jinja2 + PromptRegistry would eliminate the documentation/runtime duplication, enable prompt version pinning, and make cost tracking uniform. A larger refactor but the right long-term direction.

**P2-2: Add `pipeline_state: "running"` with JobManager integration**
When an episode has an active background job in `JobManager`, `pipeline_state` should return `"running"` rather than `"ready"`. This requires checking `JobManager.active_for_episode()` in `_compute_pipeline_state()`. Noted as a non-goal in Phase 1; worth scheduling for the next UI pass.

**P2-3: Add approved-task cache in list endpoint**
Phase 1 noted that episodes at review-gate statuses with an approved task cause an extra DB query (approved-task cache is absent, only pending-task cache exists). For dashboards with many episodes at gate statuses simultaneously, pre-fetching approved tasks in the batch would remove the extra per-episode query.

**P2-4: Remove duplicate `review_task_id` index**
`ReviewItemDecision.review_task_id` has both `index=True` in `mapped_column()` and a named `Index` in `__table_args__`. Remove the `index=True` on the column definition; rely on the named index in `__table_args__`. One-line change, eliminates the ORM/migration path discrepancy.

**P2-5: Phase 3 trap test with mixed video/photo pool**
Add one test to `TestValidateAndAdjustSelection` that includes a video candidate as the trap-flagged winner, verifying the swap logic falls through to a photo candidate correctly.

**P2-6: Phase 5 `edit` → `accept` clears `edited_text` test**
Add `test_edit_then_accept_clears_edited_text` to verify that the upsert correctly clears `edited_text` when action changes from EDITED to ACCEPTED.

**P2-7: Phase 2 `COST_LIMIT` failure override test**
Add `test_cost_limit_episode_marks_failed_stage` mirroring `test_failed_episode_marks_failed_stage` to give direct coverage of the `"cost_limit"` branch.

---

## 9. Final Recommendation

The repository is in a strong state. All five phases are coherent, correct, and internally consistent. The codebase is ready for daily internal use in the review workflow and stock image selection.

**Immediate priorities for the repository owner:**

1. **Run Phase 4 video end-to-end smoke test** on a real episode with `pexels_video_enabled=True` before using video clips in production. This is the only path not validated under real conditions.

2. **Add PromptRegistry tracking for intent extraction** (P1-2) — this is a real observability gap that makes the `btcedu llm-report` incomplete for Phase 3 episodes.

3. **Apply Phase 5 adaptation apply test** (P1-3) and **Phase 4 normalization failure test** (P1-4) — both are short tests that close meaningful coverage gaps.

Beyond those three, the system is in a production-appropriate state for an internal tool. The architecture is clean, the phases compose correctly, backward compatibility is solid, and the test suite is meaningful (not just coverage-padded). The foundation is ready for the next phase of development.

---

*This report was produced by reading all source files, phase validation outputs, and phase implementation outputs directly. It does not rely on documentation claims for conclusions about code behavior.*
