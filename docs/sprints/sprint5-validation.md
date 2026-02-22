# Sprint 5 — Validation Prompt (Turkey-Context Adaptation Stage)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 5 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–4 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 5 (Phase 2, Part 2: Turkey-Context Adaptation Stage)** implementation of the btcedu video production pipeline.

Sprint 5 was scoped to:
- Create the adaptation prompt template (`adapt.md`) with tiered adaptation rules
- Implement `adapt_script()` in `btcedu/core/adapter.py`
- Implement adaptation diff computation (tier-classified)
- Add Review Gate 2 after ADAPT
- Extend dashboard review system with adaptation review view (tier-highlighted diff)
- Implement reviewer feedback injection for re-adaptation
- Add `adapt` CLI command with `--force` and `--dry-run`
- Integrate ADAPT stage and Review Gate 2 into v2 pipeline
- Provenance, idempotency, cascade invalidation
- Write tests

Sprint 5 was NOT scoped to include: CHAPTERIZE, image generation, TTS, video review (RG3), auto-approve, batch adaptation.

Sprint 5 completes Phase 2. After this sprint, the full cycle of correction → review → translation → adaptation → review should work end-to-end.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Adaptation Prompt Template — Content & Safety

- [ ] **1.1** `btcedu/prompts/templates/adapt.md` exists
- [ ] **1.2** Has valid YAML frontmatter with: name (`adapt`), model, temperature, max_tokens, description, author
- [ ] **1.3** System section instructs LLM to act as a Turkish content adaptation specialist for Bitcoin/crypto educational content
- [ ] **1.4** Tier 1 rules present: institution replacement (BaFin→SPK), currency conversion, tone adjustment, legal/tax removal with marker
- [ ] **1.5** Tier 2 rules present: cultural reference replacement (tagged for reviewer), regulatory context changes
- [ ] **1.6** **CRITICAL — Hard constraint 7**: Keep ALL Bitcoin/crypto technical facts unchanged — no simplification, no reinterpretation
- [ ] **1.7** **CRITICAL — Hard constraint 8**: Do NOT invent Turkish regulatory details — never fabricate Turkish law references
- [ ] **1.8** **CRITICAL — Hard constraint 9**: Do NOT add financial advice, investment recommendations, or price predictions
- [ ] **1.9** **CRITICAL — Hard constraint 10**: Do NOT add political commentary or partisan framing
- [ ] **1.10** **CRITICAL — Hard constraint 11**: Do NOT present adaptation choices as claims from the original source
- [ ] **1.11** **CRITICAL — Hard constraint 12**: Editorial neutrality: adaptation changes framing, not facts
- [ ] **1.12** Output specifies that each adaptation must be tagged `[T1]` or `[T2]`
- [ ] **1.13** Input variables include `{{ translation }}` and `{{ original_german }}`
- [ ] **1.14** Optional `{% if reviewer_feedback %}` block present for re-adaptation
- [ ] **1.15** Prompt does NOT contain instructions that could lead to hallucinated regulatory content
- [ ] **1.16** Prompt explicitly includes the neutral marker for removed content: `[kaldırıldı: ülkeye özgü]`

### 2. Adapter Module

- [ ] **2.1** `btcedu/core/adapter.py` exists
- [ ] **2.2** `adapt_script()` function has correct signature matching existing stage patterns
- [ ] **2.3** Function returns a structured result (AdaptationResult or similar) with adapted_text, diff, provenance, cost, tier counts
- [ ] **2.4** Reads Turkish translation from `data/transcripts/{ep_id}/transcript.tr.txt`
- [ ] **2.5** Reads German corrected transcript from `data/transcripts/{ep_id}/transcript.corrected.de.txt` (reference)
- [ ] **2.6** Writes adapted script to `data/outputs/{ep_id}/script.adapted.tr.md`
- [ ] **2.7** Creates necessary directories with `mkdir(parents=True, exist_ok=True)`
- [ ] **2.8** Uses existing Claude API calling pattern
- [ ] **2.9** Loads prompt via PromptRegistry (not hardcoded)
- [ ] **2.10** Pre-condition check: verifies episode is at TRANSLATED status
- [ ] **2.11** Handles reviewer feedback: reads notes from most recent CHANGES_REQUESTED ReviewDecision for stage="adapt" and passes as `reviewer_feedback` template variable

### 3. Adaptation Diff

- [ ] **3.1** Adaptation diff JSON written to `data/outputs/{ep_id}/review/adaptation_diff.json`
- [ ] **3.2** Diff includes `adaptations` array with entries containing: tier (T1/T2), type, original, adapted, context, position
- [ ] **3.3** Diff includes summary: total_adaptations, t1_count, t2_count, by_type breakdown
- [ ] **3.4** Tier classification is present (distinguishes T1 from T2)
- [ ] **3.5** Diff handles edge cases: no adaptations (empty array), many adaptations, text with no cultural references
- [ ] **3.6** Turkish characters handled correctly (`ensure_ascii=False`)

### 4. Review Gate 2

- [ ] **4.1** After `adapt_script()` succeeds, a ReviewTask is created with stage="adapt"
- [ ] **4.2** ReviewTask includes correct artifact_paths (adapted script + translation)
- [ ] **4.3** ReviewTask includes diff_path pointing to `adaptation_diff.json`
- [ ] **4.4** Pipeline pauses at ADAPTED status when no approved ReviewTask (stage="adapt") exists
- [ ] **4.5** Pipeline advances past ADAPTED when an approved ReviewTask exists for stage="adapt"
- [ ] **4.6** On rejection: episode status reverts to TRANSLATED
- [ ] **4.7** On request-changes: notes stored, episode reverts to TRANSLATED, re-adaptation uses feedback
- [ ] **4.8** Review Gate 2 uses the same ReviewTask/ReviewDecision models as Review Gate 1
- [ ] **4.9** Review Gate 2 does NOT interfere with Review Gate 1 (both can coexist for the same episode)

### 5. Adaptation Review UI

- [ ] **5.1** Review detail page detects stage type (correct vs adapt) and renders appropriate diff view
- [ ] **5.2** For adaptation review: shows side-by-side literal translation vs adapted version
- [ ] **5.3** Tier 1 adaptations highlighted in one color (e.g., blue/light for mechanical, low-risk)
- [ ] **5.4** Tier 2 adaptations highlighted in a more prominent color (e.g., orange/amber for editorial, needs attention)
- [ ] **5.5** Adaptation summary shown: total adaptations, T1 count, T2 count, by type
- [ ] **5.6** Approve/reject/request-changes buttons work for adaptation reviews
- [ ] **5.7** Request-changes textarea available for reviewer notes
- [ ] **5.8** Decision history shown (previous adaptations and reviews for this episode)
- [ ] **5.9** UI properly escapes Turkish text (XSS prevention)
- [ ] **5.10** Review queue correctly lists both correction and adaptation reviews with appropriate labels

### 6. Provenance

- [ ] **6.1** Provenance JSON written to `data/outputs/{ep_id}/provenance/adapt_provenance.json`
- [ ] **6.2** Provenance format matches MASTERPLAN.md §3.6
- [ ] **6.3** Provenance includes both input files (translation + German original)
- [ ] **6.4** Prompt hash in provenance matches the hash stored in PromptVersion record

### 7. Idempotency

- [ ] **7.1** Second run without `--force` skips adaptation (does not call API)
- [ ] **7.2** Idempotency check verifies: output file exists AND prompt hash matches AND input content hash matches
- [ ] **7.3** `--force` flag bypasses idempotency check
- [ ] **7.4** `.stale` marker is respected if present
- [ ] **7.5** Content hashes use SHA-256

### 8. Cascade Invalidation

- [ ] **8.1** Translation re-run marks adaptation output as stale
- [ ] **8.2** Correction re-run chains through: correction stale → translation stale → adaptation stale
- [ ] **8.3** `.stale` marker includes invalidation metadata (invalidated_by, invalidated_at, reason)
- [ ] **8.4** Review rejection also triggers re-adaptation on next pipeline run

### 9. CLI Command

- [ ] **9.1** `btcedu adapt <episode_id>` command exists and is registered
- [ ] **9.2** `--force` flag works
- [ ] **9.3** `--dry-run` flag works
- [ ] **9.4** `btcedu adapt --help` shows useful help text
- [ ] **9.5** Command validates episode exists and is TRANSLATED
- [ ] **9.6** On success: episode status updated to ADAPTED, ReviewTask created
- [ ] **9.7** On failure: episode status unchanged, error logged

### 10. Pipeline Integration

- [ ] **10.1** ADAPT is in `PipelineStage` enum
- [ ] **10.2** `resolve_pipeline_plan()` includes ADAPT for v2 episodes after TRANSLATED
- [ ] **10.3** Pipeline checks for approved ReviewTask (stage="adapt") before proceeding past ADAPTED
- [ ] **10.4** v1 pipeline is completely unaffected
- [ ] **10.5** Review Gate 2 positioned correctly: TRANSLATE → ADAPT → RG2 → (future CHAPTERIZE)

### 11. V1 Pipeline + Phase 1 Compatibility (Regression)

- [ ] **11.1** `btcedu status` still works for existing episodes
- [ ] **11.2** v1 pipeline stages are unmodified
- [ ] **11.3** Correction stage still works correctly
- [ ] **11.4** Review Gate 1 (correction review) still works correctly
- [ ] **11.5** Translation stage still works correctly
- [ ] **11.6** Existing dashboard pages still function
- [ ] **11.7** Existing tests still pass
- [ ] **11.8** No existing CLI commands are broken

### 12. Test Coverage

- [ ] **12.1** Unit test for adaptation diff computation from known inputs
- [ ] **12.2** Unit test for tier classification (T1 vs T2)
- [ ] **12.3** Test for pre-condition check (fails if not TRANSLATED)
- [ ] **12.4** Test that adaptation with dry-run does not write files or call API
- [ ] **12.5** Test that idempotency check works (second call skips)
- [ ] **12.6** Test that `--force` overrides idempotency
- [ ] **12.7** Test for Review Gate 2 creation and approval flow
- [ ] **12.8** Test for Review Gate 2 rejection flow (reverts to TRANSLATED)
- [ ] **12.9** Test for request-changes feedback injection
- [ ] **12.10** Test for cascade invalidation (translation re-run → adaptation stale)
- [ ] **12.11** Flask test: adaptation review endpoint renders correctly with tier-highlighted diff
- [ ] **12.12** Tests use mocked Claude API calls
- [ ] **12.13** All tests pass with `pytest tests/`

### 13. Scope Creep Detection

- [ ] **13.1** No CHAPTERIZE stage was implemented
- [ ] **13.2** No image generation was implemented
- [ ] **13.3** No TTS integration was implemented
- [ ] **13.4** No video review (Review Gate 3) was implemented
- [ ] **13.5** No auto-approve rules were added
- [ ] **13.6** No German→Turkish lookup table was created as a database feature
- [ ] **13.7** No existing stages (correct, translate) were modified beyond pipeline integration
- [ ] **13.8** No batch adaptation was implemented
- [ ] **13.9** No unnecessary dependencies were added
- [ ] **13.10** Review system was extended, not rewritten

### 14. Safety / Security

- [ ] **14.1** Adaptation prompt enforces ALL six hard constraints from §5C (items 7-12)
- [ ] **14.2** No prompt injection vulnerability via reviewer feedback (feedback is clearly delimited in template)
- [ ] **14.3** No XSS vulnerability in adaptation review UI (Turkish text properly escaped)
- [ ] **14.4** No SQL injection in review routes (using ORM / parameterized queries)
- [ ] **14.5** Adaptation output does not contain fabricated Turkish legal references (verify with sample output if possible)
- [ ] **14.6** Adaptation output does not contain financial advice (verify with sample output if possible)

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Phase 2 is complete. Ready for Phase 3 (Sprint 6: Chapterization). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 6 after fixes. |
| **FAIL** | Critical issues found. Sprint 5 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Phase 2 Completion Assessment:

After Sprint 5, Phase 2 (Translation + Adaptation) should be fully operational:
- [ ] A corrected, review-approved transcript can be translated via `btcedu translate <ep_id>`
- [ ] A translated transcript can be adapted via `btcedu adapt <ep_id>`
- [ ] Adaptation produces a tier-classified script with T1/T2 tags
- [ ] Review Gate 2 pauses pipeline for human review of adaptation
- [ ] Adaptation review shows tier-highlighted diff in dashboard
- [ ] Approve/reject/request-changes works for adaptation reviews
- [ ] Full chain works: correct → RG1 → translate → adapt → RG2 → ready for chapterization
- [ ] All stages are idempotent, provenance-tracked, and prompt-versioned
- [ ] Cascade invalidation chains correctly from correction through translation to adaptation
- [ ] v1 pipeline and Phase 1 are completely unaffected

If all of the above are true, Phase 2 is complete and Sprint 6 (Chapterization) can begin.

### Deferred Items Acknowledged:

- CHAPTERIZE stage (Sprint 6)
- Image generation (Sprint 6-7)
- TTS integration (Sprint 8)
- Video assembly / Render pipeline (Sprint 9-10)
- Video review / Review Gate 3 (Sprint 9-10)
- YouTube publishing (Sprint 11)
- Auto-approve rules for adaptations
- German→Turkish institution lookup as database
- Batch adaptation
- A/B testing of adaptation prompts

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 1 (Adaptation Prompt Safety)** — this is the highest-risk prompt in the pipeline. All six hard constraints from §5C MUST be present and correctly formulated. A missing hard constraint is a FAIL.
- **Pay special attention to Section 14 (Safety/Security)** — adaptation involves user-facing content that could cause harm if it contains fabricated legal information or financial advice.
- Verify that the review UI correctly distinguishes Tier 1 (mechanical, low-risk) from Tier 2 (editorial, needs attention) adaptations with visual differentiation.
- Check that reviewer feedback injection does not create prompt injection vulnerabilities.
