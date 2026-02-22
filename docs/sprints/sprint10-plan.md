# Sprint 10 — Planning Prompt (Render Polish + Review Gate 3 + Video Preview)

> **Usage**
> - **Model**: Claude Opus
> - **Mode**: Plan Mode
> - **Inputs required**: `MASTERPLAN.md`, Sprint 1–9 completed codebase (especially `btcedu/core/renderer.py` for the existing render pipeline, `btcedu/services/ffmpeg_service.py`, `btcedu/core/reviewer.py` for review gate patterns, `btcedu/core/pipeline.py`, `btcedu/web/`, `btcedu/cli.py`)
> - **Expected output**: A file-level implementation plan covering transitions support, render polish, Review Gate 3 (video review), dashboard video preview, review gate hardening, and tests.

---

## Context

You are planning **Sprint 10 (Phase 5, Part 2: Render Polish + Review Gate 3)** of the btcedu video production pipeline extension.

Read `MASTERPLAN.md` (the source of truth) and the current codebase before producing the plan. Sprints 1–9 are complete:
- Foundation, Correction, Review System (RG1), Translation, Adaptation (RG2), Chapterization, Image Generation, TTS — all functional.
- Render Pipeline (Sprint 9): ffmpeg service, renderer module, per-chapter segment rendering with text overlays, basic concatenation into draft.mp4, `render` CLI command.

Sprint 10 completes the RENDER phase by adding:
1. **Transitions** between chapter segments (fade, cut, slide) — upgrading from Sprint 9's cut-only.
2. **Review Gate 3** — the final human review gate before publishing. The content owner watches the draft video and approves/rejects.
3. **Dashboard video preview** — embedded video player for reviewing draft videos.
4. **Review gate hardening** — ensure the full review workflow (RG1 + RG2 + RG3) is solid and the pipeline correctly blocks at each gate.
5. **Artifact hash recording** — when RG3 approves, record the SHA-256 of `draft.mp4` in the ReviewTask for tamper-evident publishing (per §5I pre-publish safety checks).

### Sprint 10 Focus (from MASTERPLAN.md §4 Phase 5, §5G, §5H, §3.3)

1. Add transition support to ffmpeg service (fade_in/out between segments).
2. Implement Review Gate 3 in pipeline — after RENDERED, creates a ReviewTask for video review.
3. Create dashboard video review page:
   - Embedded video player for `draft.mp4`
   - Chapter script displayed alongside for reference
   - Approve / Reject / Request Changes buttons
   - Review history timeline
4. When video is approved: record `artifact_hash` (SHA-256 of `draft.mp4`) in ReviewTask, update status to APPROVED.
5. When video is rejected/changes requested: reset to appropriate stage for re-generation.
6. Harden the full review workflow: ensure RG1, RG2, RG3 all block correctly and pipeline resumes on approval.
7. CLI commands for review management: `btcedu review list`, `btcedu review approve <id>`, `btcedu review reject <id>`.
8. Tests.

### Relevant Subplans

- **Subplan 5G** (Video Assembly) — slice 5: transitions between chapters. Slice 6: video preview in dashboard.
- **Subplan 5H** (Human Review & Approval Workflow) — slice 7: video review view. Slice 2: core reviewer logic. ReviewTask state machine.
- **§3.3** (Human Review Gates) — Review Gate 3 after RENDER: approve/reject/request changes.
- **§5I** (Pre-Publish Safety Checks) — `artifact_hash` computed at RG3 approval for tamper-evident publishing chain.
- **§9.3** (Review Status State Machine) — PENDING → IN_REVIEW → APPROVED / REJECTED / CHANGES_REQUESTED.

---

## Your Task

Produce a detailed implementation plan for Sprint 10. The plan must include:

1. **Sprint Scope Summary** — one paragraph. This sprint completes the render phase and adds the final review gate before publishing.
2. **File-Level Plan** — for every file that will be created or modified.
3. **Transition Support** — extend ffmpeg service:
   - Fade transitions: ffmpeg `fade` filter (fade in at segment start, fade out at end)
   - Cut transitions: no filter (current behavior, default)
   - Slide transitions: optional, simpler variant (crossfade between segments)
   - Map transition types from chapter JSON (`transitions.in`, `transitions.out`) to ffmpeg filters
   - Update `create_segment()` to apply transition filters
   - Update `concatenate_segments()` to handle transition overlaps (if using crossfade)
4. **Review Gate 3** — video review after RENDERED:
   - Pipeline reaches RENDERED → creates ReviewTask (stage="render")
   - Episode status stays at RENDERED until approved
   - On approval: `artifact_hash` = SHA-256 of `draft.mp4`, status → APPROVED
   - On rejection: status → RENDERED (or back to earlier stage for re-generation)
   - On request changes: reviewer notes injected into re-render context
   - Integration with existing `btcedu/core/reviewer.py` patterns from Sprint 3
5. **Dashboard Video Review Page** — `/reviews/<id>` for video review type:
   - HTML5 video player (`<video>` element) with playback controls
   - Serve `draft.mp4` from local filesystem
   - Chapter script displayed alongside (scrollable, synced with video if feasible)
   - Review actions: Approve (green), Reject (red), Request Changes (yellow + notes)
   - Review history: timeline of previous decisions for this episode
   - File metadata: duration, resolution, file size, generation date
6. **Artifact Hash for Tamper-Evident Publishing**:
   - When RG3 approves: compute SHA-256 of `draft.mp4`
   - Store hash in `ReviewTask.artifact_hash` field
   - Sprint 11 (Publish) will verify this hash before uploading to YouTube
   - This creates a chain: approval → hash → publish verification
7. **Review Gate Hardening**:
   - Verify all three gates (RG1, RG2, RG3) block pipeline correctly
   - Verify rejection/request-changes flows work for all three gates
   - Verify reviewer feedback injection works (from §5H)
   - Verify pipeline resumes automatically after approval
   - Test the full pipeline flow: CORRECTED → RG1 → ... → RENDERED → RG3 → APPROVED
8. **CLI Review Commands** — extend or add:
   - `btcedu review list [--status pending]` — list review tasks
   - `btcedu review approve <review_id> [--notes "..."]` — approve a review
   - `btcedu review reject <review_id> --notes "..."` — reject with notes
   - These may already exist from Sprint 3; extend if needed for RG3
9. **Pipeline Integration** — RENDERED → Review Gate 3 → APPROVED.
10. **Test Plan** — list each test.
11. **Implementation Order** — numbered sequence.
12. **Definition of Done** — checklist.
13. **Non-Goals** — explicit list.

---

## Constraints

- **Backward compatibility**: v1 pipeline unaffected. Previous review gates (RG1, RG2) unaffected.
- **Follow existing patterns**: Review Gate 3 should use the same `ReviewTask`/`ReviewDecision` models and `btcedu/core/reviewer.py` patterns from Sprint 3.
- **Transitions are optional polish**: The draft video already works with cuts (Sprint 9). Transitions are an enhancement. If transitions add significant complexity, plan a minimal version (fade only) and defer slide/crossfade.
- **No YouTube publishing**: Sprint 10 stops at APPROVED. Publishing is Sprint 11.
- **No rewrites**: Do not refactor existing code.
- **Preserve compatibility with the existing pipeline and patterns.**
- **Use small, safe, incremental steps.**

---

## Output Format

Write the plan as a structured Markdown document with clear sections. Include ffmpeg transition filter designs, video review UI wireframe, review gate flow diagram, and test plan.

---

## Additional Instructions

- Do not ask clarifying questions; make reasonable assumptions and label them clearly as `[ASSUMPTION]`.
- `[ASSUMPTION]`: The dashboard already has patterns for serving static files (images from Sprint 7). Serving video follows the same pattern using Flask's `send_file()` or `send_from_directory()`.
- `[ASSUMPTION]`: The video player is a standard HTML5 `<video>` element. No custom video player library needed.
- `[ASSUMPTION]`: Review Gate 3 does not have auto-approve rules (unlike RG1 for minor corrections). All videos must be manually reviewed.
- Consider video file size: `draft.mp4` can be hundreds of MB. Ensure the dashboard can stream it (byte-range requests) rather than requiring full download before playback.
- The `artifact_hash` is critical for Sprint 11's pre-publish safety check. Plan this carefully — it must be computed from the exact file that was reviewed, and the hash must be stored persistently.
