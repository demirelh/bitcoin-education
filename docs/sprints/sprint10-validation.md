# Sprint 10 — Validation Prompt (Render Polish + Review Gate 3 + Video Preview)

> **Usage**
> - **Model**: Claude Opus or Sonnet
> - **Mode**: Validation / Review / Regression Check
> - **Inputs required**: The Sprint 10 plan, the implementation diff (all files changed/created), `MASTERPLAN.md`, test results, Sprint 1–9 validation status
> - **Expected output**: A structured checklist with PASS/FAIL per item and a final verdict.

---

## Context

You are reviewing the **Sprint 10 (Phase 5, Part 2: Render Polish + Review Gate 3 + Video Preview)** implementation of the btcedu video production pipeline.

Sprint 10 was scoped to:
- Add fade transition support to ffmpeg service and renderer
- Implement Review Gate 3 (video review after RENDERED)
- Create dashboard video review page (video player + chapter script + review actions)
- Compute and store `artifact_hash` (SHA-256 of draft.mp4) on RG3 approval
- Add/extend CLI review commands for all gates
- Harden the full review workflow (RG1 + RG2 + RG3)
- Video file serving with byte-range support
- Write tests

Sprint 10 was NOT scoped to include: YouTube publishing, publish_jobs table, YouTube service/OAuth2, background music, video editing, complex transitions (slide/crossfade), auto-approve for video.

---

## Review Checklist

Evaluate each item as **PASS**, **FAIL**, or **N/A**. Provide a brief note for any FAIL.

### 1. Transition Support

- [ ] **1.1** Fade transition: video `fade` filter applied (fade in at start, fade out at end)
- [ ] **1.2** Audio fade: `afade` filter applied to match video fades
- [ ] **1.3** Cut transition: no filter applied (default behavior, backward compatible)
- [ ] **1.4** Transition types read from chapter JSON (`transitions.in`, `transitions.out`)
- [ ] **1.5** Renderer passes transition types to ffmpeg service correctly
- [ ] **1.6** Fade duration is reasonable (~0.5 seconds)
- [ ] **1.7** Render manifest records which transitions were applied
- [ ] **1.8** Transitions don't break concatenation (segments still join correctly)

### 2. Review Gate 3 — Core Logic

- [ ] **2.1** After RENDER completes, a ReviewTask is created with `stage="render"`
- [ ] **2.2** Episode status stays at RENDERED until review is completed
- [ ] **2.3** Pipeline does NOT advance past RENDERED without RG3 approval
- [ ] **2.4** ReviewTask.artifact_paths includes draft.mp4 and chapters.json paths
- [ ] **2.5** ReviewTask uses the same model/schema as RG1 and RG2

### 3. Review Gate 3 — Approval Flow

- [ ] **3.1** On approval: SHA-256 of `draft.mp4` is computed
- [ ] **3.2** Hash is stored in `ReviewTask.artifact_hash` field
- [ ] **3.3** Episode status updated to APPROVED
- [ ] **3.4** ReviewDecision record created (decision="approved")
- [ ] **3.5** Hash computation is from the actual file on disk (not a cached value)
- [ ] **3.6** Hash is a valid SHA-256 hex string

### 4. Review Gate 3 — Rejection / Changes Flow

- [ ] **4.1** On rejection: episode status remains RENDERED
- [ ] **4.2** ReviewDecision record created (decision="rejected", with notes)
- [ ] **4.3** On request changes: ReviewDecision created with notes
- [ ] **4.4** Reviewer notes are stored and accessible for re-generation
- [ ] **4.5** Episode can be re-rendered after rejection/changes request

### 5. Dashboard Video Review Page

- [ ] **5.1** Video review page renders for ReviewTask with stage="render"
- [ ] **5.2** HTML5 `<video>` element with playback controls
- [ ] **5.3** Video loads and plays the draft.mp4 file
- [ ] **5.4** Chapter script displayed alongside video (scrollable)
- [ ] **5.5** Review actions: Approve (green), Reject (red), Request Changes (yellow)
- [ ] **5.6** Reject and Request Changes require notes (form validation)
- [ ] **5.7** Review history shown (previous decisions for this episode/stage)
- [ ] **5.8** File metadata displayed: duration, resolution, file size
- [ ] **5.9** Follows existing dashboard template and styling patterns
- [ ] **5.10** Turkish text properly escaped (XSS prevention)

### 6. Video File Serving

- [ ] **6.1** Route exists to serve draft.mp4 (e.g., `/episodes/<ep_id>/video`)
- [ ] **6.2** Uses `send_file()` with `conditional=True` for byte-range support
- [ ] **6.3** Correct MIME type: `video/mp4`
- [ ] **6.4** Missing file returns 404 (not 500)
- [ ] **6.5** Video seeking works in the browser (byte-range requests honored)

### 7. Artifact Hash (Tamper-Evident Chain)

- [ ] **7.1** `compute_artifact_hash()` utility exists
- [ ] **7.2** Uses SHA-256 algorithm
- [ ] **7.3** Reads the complete file (not a partial read)
- [ ] **7.4** Hash stored in `ReviewTask.artifact_hash` on approval
- [ ] **7.5** Hash is persistent (survives server restart)
- [ ] **7.6** Sprint 11 can retrieve this hash for pre-publish verification (documented)

### 8. Review Gate Hardening

- [ ] **8.1** RG1 (after CORRECT): blocks pipeline, resumes on approval
- [ ] **8.2** RG2 (after ADAPT): blocks pipeline, resumes on approval
- [ ] **8.3** RG3 (after RENDER): blocks pipeline, resumes on approval
- [ ] **8.4** Rejection at RG1: episode goes back to TRANSCRIBED
- [ ] **8.5** Rejection at RG2: episode goes back to TRANSLATED (or ADAPTED for re-generation)
- [ ] **8.6** Rejection at RG3: episode stays at RENDERED for re-render
- [ ] **8.7** Request changes at any gate: notes stored and available
- [ ] **8.8** Pipeline resumes automatically or via trigger after approval
- [ ] **8.9** Multiple rejection/approval cycles work (not just first attempt)

### 9. CLI Review Commands

- [ ] **9.1** `btcedu review list` shows pending reviews (optionally filterable by status/stage)
- [ ] **9.2** `btcedu review approve <id>` works for all gates (RG1, RG2, RG3)
- [ ] **9.3** `btcedu review reject <id> --notes "..."` works for all gates
- [ ] **9.4** `btcedu review request-changes <id> --notes "..."` works
- [ ] **9.5** CLI approval for RG3 computes and stores artifact_hash
- [ ] **9.6** `btcedu review --help` shows useful help text

### 10. Pipeline Integration

- [ ] **10.1** RENDERED → ReviewTask (stage="render") → blocks → APPROVED on approval
- [ ] **10.2** APPROVED is a terminal pre-publish state
- [ ] **10.3** Pipeline does not advance past APPROVED (PUBLISH is Sprint 11)
- [ ] **10.4** v1 pipeline is completely unaffected

### 11. V1 Pipeline + Previous Sprint Compatibility (Regression)

- [ ] **11.1** `btcedu status` still works for all episodes
- [ ] **11.2** v1 pipeline stages unmodified
- [ ] **11.3** RG1 (correction review) still works correctly
- [ ] **11.4** RG2 (adaptation review) still works correctly
- [ ] **11.5** All previous stages (correct through render) still work
- [ ] **11.6** Existing dashboard pages still function (review queue, diff viewers)
- [ ] **11.7** Existing tests still pass
- [ ] **11.8** No existing CLI commands broken
- [ ] **11.9** Render pipeline (Sprint 9) still produces correct output
- [ ] **11.10** Sprint 9 cut-only transitions still work as default

### 12. Test Coverage

- [ ] **12.1** Transition tests: fade filters generated correctly, cuts unchanged
- [ ] **12.2** RG3 tests: ReviewTask created, approval flow, rejection flow, changes flow
- [ ] **12.3** Artifact hash tests: SHA-256 computation, storage in ReviewTask
- [ ] **12.4** Video review UI tests: template renders, video route serves MP4
- [ ] **12.5** Review endpoint tests: POST approve/reject/request-changes
- [ ] **12.6** CLI review tests: list, approve, reject, request-changes
- [ ] **12.7** Review hardening tests: full pipeline flow through all three gates
- [ ] **12.8** Rejection cycle tests: reject → re-process → new review → approve
- [ ] **12.9** Existing Sprint 3 review tests still pass
- [ ] **12.10** All tests pass with `pytest tests/`

### 13. Scope Creep Detection

- [ ] **13.1** No YouTube publishing was implemented
- [ ] **13.2** No `publish_jobs` table was created
- [ ] **13.3** No YouTube service / OAuth2 was implemented
- [ ] **13.4** No background music was implemented
- [ ] **13.5** No video editing / trim tools were implemented
- [ ] **13.6** No complex transitions (slide, crossfade, wipe) were implemented
- [ ] **13.7** No auto-approve for video review was implemented
- [ ] **13.8** No existing stages were modified beyond review gate integration
- [ ] **13.9** No existing manifest formats were broken

### 14. Security Review

- [ ] **14.1** Video serving route does not expose files outside the expected directory (path traversal prevention)
- [ ] **14.2** Review approval/rejection endpoints validate the review_id
- [ ] **14.3** Reviewer notes are sanitized/escaped in the dashboard (XSS prevention)
- [ ] **14.4** Artifact hash computation is resistant to TOCTOU (time-of-check/time-of-use): hash is computed at approval time from the file on disk

---

## Verdict

Based on the checklist above, provide one of:

| Verdict | Meaning |
|---------|---------|
| **PASS** | All items pass. Sprint 10 is complete and ready for Sprint 11 (YouTube Publishing). |
| **PASS WITH FIXES** | Minor issues found. List specific items and fixes. Can proceed to Sprint 11 after fixes. |
| **FAIL** | Critical issues found. Sprint 10 must be reworked. |

### Verdict: **[PASS / PASS WITH FIXES / FAIL]**

### Issues Found (if any):

1. [Item X.Y] — description of issue and recommended fix
2. ...

### Review Workflow Integrity Assessment:

The review workflow is the safety system for the entire pipeline. Verify the complete chain:
- [ ] Episode flows through all stages: NEW → ... → CORRECTED → (RG1) → ... → ADAPTED → (RG2) → ... → RENDERED → (RG3) → APPROVED
- [ ] Every review gate blocks pipeline progression
- [ ] Every gate supports: approve, reject, request changes
- [ ] Rejection flows correctly reset status and allow re-processing
- [ ] The artifact_hash chain is intact: RG3 approval records hash → Sprint 11 can verify before publish
- [ ] No bypass path exists (cannot reach APPROVED without all three reviews)

### Publish-Readiness Assessment:

Sprint 11 (YouTube Publishing) requires:
- [ ] Episode status is APPROVED
- [ ] `artifact_hash` of `draft.mp4` is stored in ReviewTask (for pre-publish integrity check)
- [ ] `draft.mp4` exists at expected path
- [ ] Metadata for YouTube (title, description, tags) is available from chapter JSON / publishing context
- [ ] All review gates are passed

### Deferred Items Acknowledged:

- YouTube publishing / PUBLISH stage (Sprint 11)
- `publish_jobs` table (Sprint 11)
- YouTube service / OAuth2 (Sprint 11)
- Pre-publish safety checks (Sprint 11)
- Background music / intro / outro
- Video editing / trim / re-cut tools
- Complex transitions (slide, crossfade, wipe)
- Auto-approve for video review

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps when recommending fixes.
- **Pay special attention to Section 7 (Artifact Hash)** — this is the critical link between approval and publishing. A broken hash chain means Sprint 11 can't safely publish.
- **Pay special attention to Section 8 (Review Gate Hardening)** — all three gates must work correctly. A broken review gate could allow unapproved content through.
- **Pay attention to Section 14 (Security)** — video serving and review endpoints are web-facing. Ensure path traversal prevention and XSS protection.
- Verify that transitions don't break the existing cut-only render (backward compatibility within the sprint).
