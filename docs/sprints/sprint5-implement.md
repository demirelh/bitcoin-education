# Sprint 5 — Implementation Prompt (Turkey-Context Adaptation Stage)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 5 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–4 completed codebase
> - **Expected output**: All code changes (new files, modified files), adaptation prompt template, adaptation diff logic, review UI extensions, tests — committed and passing.

---

## Context

You are implementing **Sprint 5 (Phase 2, Part 2: Turkey-Context Adaptation Stage)** of the btcedu video production pipeline.

Sprints 1–4 are complete:
- Foundation: EpisodeStatus enum, PromptVersion/ReviewTask/ReviewDecision models, PromptRegistry, pipeline_version.
- Correction: `btcedu/core/corrector.py`, correction diff, provenance, CORRECT stage.
- Review System: `btcedu/core/reviewer.py`, Review Gate 1 (after CORRECT), dashboard review queue + diff viewer + approve/reject.
- Translation: `btcedu/core/translator.py`, faithful German→Turkish translation, segmentation, TRANSLATE stage.

Sprint 5 adds the **ADAPT** stage — Turkey-context cultural adaptation with tiered rules — and **Review Gate 2** for human review of adaptations. This is the most editorially sensitive stage in the pipeline.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 5 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/translator.py` (pattern to follow), `btcedu/core/corrector.py`, `btcedu/core/reviewer.py`, `btcedu/core/pipeline.py`, `btcedu/web/review_routes.py` (or wherever review routes are), `btcedu/web/templates/review_detail.html`, `btcedu/cli.py`, `btcedu/prompts/templates/correct_transcript.md`.

2. **Create the adaptation prompt template** — `btcedu/prompts/templates/adapt.md` with:
   - YAML frontmatter: name (`adapt`), model, temperature (0.3), max_tokens (8192), description, author
   - System section: Turkish content adaptation specialist for Bitcoin/crypto educational content
   - Full tiered adaptation rules (from MASTERPLAN.md §5C):
     - **Tier 1 — Mechanical** (tag with `[T1]`):
       1. Replace German institutions → Turkish equivalents (BaFin → SPK, Sparkasse → generic bank)
       2. Replace Euro amounts → Turkish Lira or USD as appropriate
       3. Adjust tone to Turkish influencer style (conversational, engaging, formal "siz")
       4. Remove Germany-specific legal/tax advice with marker `[kaldırıldı: ülkeye özgü]`
     - **Tier 2 — Editorial** (tag with `[T2]`):
       5. Replace German cultural references with Turkish equivalents (each tagged for reviewer)
       6. Any regulatory/legal context change beyond simple removal
     - **Hard constraints (FORBIDDEN)**:
       7. Keep ALL Bitcoin/crypto technical facts unchanged — no simplification, no reinterpretation
       8. Do NOT invent Turkish regulatory details — never fabricate Turkish law references
       9. Do NOT add financial advice, investment recommendations, or price predictions
       10. Do NOT add political commentary or partisan framing
       11. Do NOT present adaptation choices as claims from the original source
       12. Editorial neutrality: adaptation changes framing, not facts
   - Input variables: `{{ translation }}` (Turkish), `{{ original_german }}` (corrected German for reference)
   - Optional: `{% if reviewer_feedback %}` block for re-adaptation after request-changes
   - Output format: adapted Turkish script in Markdown, each adaptation tagged `[T1]` or `[T2]`

3. **Create AdaptationResult dataclass** — include: adapted_text, adaptation_diff, provenance, cost, token counts, t1_count, t2_count.

4. **Implement `adapt_script()`** in `btcedu/core/adapter.py`:
   - Load prompt template via PromptRegistry
   - Read Turkish translation from `data/transcripts/{ep_id}/transcript.tr.txt`
   - Read German corrected transcript from `data/transcripts/{ep_id}/transcript.corrected.de.txt` (reference input)
   - **Pre-condition check**: Episode status is TRANSLATED (and CORRECT review gate was approved)
   - Check idempotency (output exists + prompt hash match + input hash match)
   - Call Claude via existing service/pattern
   - Save adapted script to `data/outputs/{ep_id}/script.adapted.tr.md`
   - Compute adaptation diff and save to `data/outputs/{ep_id}/review/adaptation_diff.json`
   - Save provenance to `data/outputs/{ep_id}/provenance/adapt_provenance.json`
   - Check for reviewer feedback (from previous request-changes) and pass as template variable
   - Register/record prompt version
   - Return AdaptationResult

5. **Implement adaptation diff computation**:
   - Compare the literal Turkish translation (`transcript.tr.txt`) against the adapted script (`script.adapted.tr.md`)
   - Identify all adaptations and classify by tier:
     - Parse `[T1]` and `[T2]` tags from the adapted output
     - For each adaptation, extract: original text from translation, adapted text, tier, context
   - Produce `adaptation_diff.json` with format:
     ```json
     {
       "episode_id": "...",
       "original_length": ...,
       "adapted_length": ...,
       "adaptations": [
         {
           "tier": "T1",
           "type": "institution_replacement",
           "original": "BaFin tarafından",
           "adapted": "SPK tarafından",
           "context": "...para birimi düzenleyicisi olan SPK tarafından...",
           "position": {"start": ..., "end": ...}
         }
       ],
       "summary": {
         "total_adaptations": ...,
         "t1_count": ...,
         "t2_count": ...,
         "by_type": {"institution_replacement": ..., "currency": ..., "tone": ..., "legal_removal": ..., "cultural_reference": ..., "regulatory": ...}
       }
     }
     ```
   - If the LLM does not consistently tag adaptations, fall back to a difflib-based comparison and classify manually. Label this as `[SIMPLIFICATION]` if needed.

6. **Implement Review Gate 2** — extend `btcedu/core/reviewer.py` (or the pipeline integration):
   - After `adapt_script()` succeeds, call `create_review_task()` with stage="adapt"
   - Include `adaptation_diff.json` as the diff_path
   - Include `script.adapted.tr.md` and `transcript.tr.txt` as artifact_paths
   - Pipeline pauses at ADAPTED until ReviewTask (stage="adapt") is APPROVED
   - On rejection: episode status reverts to TRANSLATED
   - On request-changes: store reviewer notes, revert to TRANSLATED, inject into re-adaptation prompt

7. **Extend adaptation review UI** — modify the existing review detail template:
   - Detect review stage: if stage=="correct" → show correction diff; if stage=="adapt" → show adaptation diff
   - For adaptation review:
     - Left column: literal Turkish translation (from `transcript.tr.txt`)
     - Right column: adapted script (from `script.adapted.tr.md`)
     - Highlight Tier 1 adaptations in one color (e.g., blue/light — mechanical, low risk)
     - Highlight Tier 2 adaptations in a more prominent color (e.g., orange/amber — editorial, needs attention)
     - Show adaptation summary: total adaptations, T1 count, T2 count, by type
   - Same approve/reject/request-changes buttons
   - Add CSS for the new tier color coding

8. **Add `adapt` CLI command** to `btcedu/cli.py`:
   - `btcedu adapt <episode_id>` with `--force` and `--dry-run`
   - Validate episode exists and is at TRANSLATED status
   - On success: update episode status to ADAPTED, create ReviewTask
   - On failure: log error, leave status unchanged

9. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
   - Ensure ADAPT is in `PipelineStage` enum
   - Update `resolve_pipeline_plan()` to include ADAPT for v2 episodes after TRANSLATED
   - Add Review Gate 2 check: pipeline checks for approved ReviewTask (stage="adapt") before proceeding past ADAPTED
   - Position: TRANSLATED → ADAPT → Review Gate 2 → CHAPTERIZE (Sprint 6)

10. **Implement cascade invalidation**:
    - Translation re-run → adaptation marked stale
    - Correction re-run → translation stale → adaptation stale (chain)
    - Hook into `invalidate_downstream()` or the translator's re-run path

11. **Write tests**:
    - `tests/test_adapter.py`:
      - Unit: adaptation diff computation from known inputs
      - Unit: tier classification (T1 vs T2)
      - Unit: pre-condition check (fails if not TRANSLATED)
      - Integration: adaptation with dry-run
      - Idempotency: second run skips
      - Force: `--force` re-runs
      - Reviewer feedback injection: re-adaptation uses notes
    - `tests/test_review_adaptation.py` (or extend existing review tests):
      - Review Gate 2 creation after adapt
      - Approve → episode status advances
      - Reject → episode reverts to TRANSLATED
      - Request-changes → notes stored, feedback injected on re-run
    - Flask test: adaptation review endpoint renders correctly
    - Pipeline test: pipeline pauses at Review Gate 2, resumes on approval

12. **Verify**:
    - Run `pytest tests/`
    - Pick a translated episode
    - Run `btcedu adapt <ep_id> --dry-run`
    - Run `btcedu adapt <ep_id>`
    - Verify `script.adapted.tr.md` at expected path
    - Verify `adaptation_diff.json` generated
    - Verify ReviewTask created (stage="adapt", status=PENDING)
    - Open dashboard → review queue shows adaptation review
    - Click into review → see adaptation diff with tier highlighting
    - Approve → episode status advances to ADAPTED (approved)
    - Test reject flow
    - Test request-changes flow
    - Verify v1 pipeline unaffected

### Anti-scope-creep guardrails

- **Do NOT** implement the CHAPTERIZE stage (that's Sprint 6).
- **Do NOT** implement image generation (Sprint 6-7).
- **Do NOT** implement auto-approve rules for adaptations.
- **Do NOT** create a lookup table of German→Turkish institution mappings as a database feature. The mapping is handled within the prompt. A hardcoded reference list in the prompt template is fine.
- **Do NOT** implement video review (Review Gate 3) — Sprint 9-10.
- **Do NOT** redesign the review system architecture — extend the existing system.
- **Do NOT** modify the translation stage or correction stage.
- **Do NOT** add new external dependencies for diff computation (use difflib or simple comparison).
- **Do NOT** refactor existing code for style or cleanup.

### Code patterns to follow

- **Stage implementation**: Follow `btcedu/core/translator.py` and `btcedu/core/corrector.py` closely — same file I/O, Claude API, provenance, idempotency patterns.
- **Review integration**: Follow the same pattern as Review Gate 1 (correction review) — study how the corrector creates a ReviewTask and how the pipeline checks for approval.
- **Review UI**: Follow the existing review detail template pattern. Add conditional rendering for stage-specific diff views.
- **CLI commands**: Follow existing Click command patterns.
- **Diff format**: Consistent with `correction_diff.json` but adapted for tier-based classification.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred
- Manual verification steps:
  - Pick a translated episode
  - Run `btcedu adapt <ep_id>`
  - Verify adapted script at expected path
  - Verify adaptation diff with tier tags
  - Verify ReviewTask created
  - Test full review flow in dashboard (approve, reject, request-changes)
  - Verify v1 pipeline unaffected

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- The adaptation prompt is safety-critical. Double-check all hard constraints from §5C are present.
- Adaptation diff JSON should use `ensure_ascii=False` for Turkish characters.
- The review UI for adaptations must properly escape user-supplied text (XSS prevention).
- Tier 2 adaptations must be visually prominent in the review UI — they require human attention.

---

## Definition of Done

- [ ] `btcedu/prompts/templates/adapt.md` exists with valid YAML frontmatter and full tiered adaptation rules
- [ ] Adaptation prompt includes ALL hard constraints from MASTERPLAN.md §5C
- [ ] `btcedu/core/adapter.py` exists with `adapt_script()` function
- [ ] Adapter takes both Turkish translation AND German original as inputs
- [ ] Adapter produces `script.adapted.tr.md` at `data/outputs/{ep_id}/script.adapted.tr.md`
- [ ] Adapter produces `adaptation_diff.json` at `data/outputs/{ep_id}/review/adaptation_diff.json`
- [ ] Adaptation diff classifies changes by tier (T1/T2) and type
- [ ] Review Gate 2 works: ReviewTask created after adapt, pipeline pauses
- [ ] Approval advances episode status
- [ ] Rejection reverts to TRANSLATED
- [ ] Request-changes injects feedback into re-adaptation prompt
- [ ] Dashboard shows adaptation review with tier-highlighted diff
- [ ] `btcedu adapt <episode_id>` CLI works with `--force` and `--dry-run`
- [ ] Pipeline plan includes ADAPT and Review Gate 2 for v2 episodes
- [ ] Idempotency works: second run skips, `--force` re-runs
- [ ] Cascade invalidation: translation re-run marks adaptation stale
- [ ] All tests pass
- [ ] v1 pipeline unaffected
- [ ] Phase 2 complete: full flow from correction → translation → adaptation → review works end-to-end

## Non-Goals

- CHAPTERIZE stage (Sprint 6)
- Image generation (Sprint 6-7)
- TTS integration (Sprint 8)
- Video review / Review Gate 3 (Sprint 9-10)
- Auto-approve rules for adaptations
- German→Turkish institution lookup table as a database feature
- Alternative adaptation strategies (multiple passes, human-in-the-loop editing)
- Batch adaptation of multiple episodes
- A/B testing of adaptation prompts (later sprint)
