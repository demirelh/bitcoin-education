# Sprint 10 — Implementation Prompt (Render Polish + Review Gate 3 + Video Preview)

> **Usage**
> - **Model**: Claude Sonnet
> - **Mode**: Implementation
> - **Inputs required**: The Opus planning output for Sprint 10 (paste below or provide as context), `MASTERPLAN.md`, Sprint 1–9 completed codebase
> - **Expected output**: All code changes (modified files for transitions, new review gate integration, dashboard video review page), tests — committed and passing.

---

## Context

You are implementing **Sprint 10 (Phase 5, Part 2: Render Polish + Review Gate 3)** of the btcedu video production pipeline.

Sprints 1–9 are complete:
- All stages through RENDER are functional.
- Review Gates 1 (correction) and 2 (adaptation) are working.
- The render pipeline produces `draft.mp4` from images + audio + text overlays.

Sprint 10 completes the render phase and adds the critical **Review Gate 3** — the final approval before YouTube publishing. This sprint also adds transitions, a dashboard video player, and hardens the full review workflow.

The Opus planning output for this sprint is provided below. Follow it precisely.

---

## Opus Planning Output

> **[PASTE THE OPUS SPRINT 10 PLAN HERE]**

---

## Implementation Instructions

### Step-by-step implementation order

1. **Read existing files first** — read `btcedu/core/renderer.py`, `btcedu/services/ffmpeg_service.py`, `btcedu/core/reviewer.py`, `btcedu/core/pipeline.py`, `btcedu/web/` (routes, templates), `btcedu/models/review.py`, `btcedu/cli.py`.

2. **Add fade transitions to ffmpeg service** — modify `btcedu/services/ffmpeg_service.py`:
   - Add a `fade` filter to `create_segment()`:
     - If `transition_in == "fade"`: add `fade=t=in:st=0:d=0.5` to video filter chain
     - If `transition_out == "fade"`: add `fade=t=out:st={duration-0.5}:d=0.5` to video filter chain
     - `"cut"` transitions: no filter change (existing behavior)
   - `[ASSUMPTION]`: Slide transitions are deferred — implement fade and cut only. Document slide as a future enhancement.
   - Audio fade: add `afade=t=in:st=0:d=0.5` / `afade=t=out:st={duration-0.5}:d=0.5` to match video fades
   - Update `create_segment()` signature to accept `transition_in` and `transition_out` parameters
   - Update the renderer module to pass transition types from chapter JSON to the ffmpeg service

3. **Update renderer to use transitions** — modify `btcedu/core/renderer.py`:
   - When building per-chapter segments, read `chapter.transitions.in` and `chapter.transitions.out`
   - Pass to `ffmpeg_service.create_segment()`
   - Update render manifest to record which transitions were applied

4. **Implement Review Gate 3** — modify `btcedu/core/pipeline.py` and `btcedu/core/reviewer.py`:
   - After RENDER stage completes successfully (status = RENDERED):
     - Create a ReviewTask with `stage="render"`
     - Set `artifact_paths` to include `draft.mp4` path and `chapters.json` path
     - Pipeline pauses — does not advance to APPROVED
   - On approval:
     - Compute SHA-256 of `draft.mp4` file
     - Store hash in `ReviewTask.artifact_hash`
     - Update episode status to APPROVED
     - Record ReviewDecision (decision="approved")
   - On rejection:
     - Record ReviewDecision (decision="rejected", notes from reviewer)
     - Episode status remains RENDERED (or reset to earlier stage if reviewer specifies)
     - ReviewTask status → REJECTED
   - On request changes:
     - Record ReviewDecision (decision="changes_requested", notes from reviewer)
     - Episode can be re-rendered with notes as context
     - ReviewTask status → CHANGES_REQUESTED
   - Use existing ReviewTask model and patterns from Sprint 3

5. **Artifact hash computation** — add utility function:
   - `compute_artifact_hash(file_path: str) -> str` — computes SHA-256 of a file
   - Called when RG3 approves: `hash = compute_artifact_hash("data/outputs/{ep_id}/render/draft.mp4")`
   - Stored in `ReviewTask.artifact_hash`
   - Sprint 11 publish stage will read this hash and verify before upload

6. **Create dashboard video review page** — extend `btcedu/web/`:
   - **Route**: `GET /reviews/<review_id>` — if review is for stage="render", render the video review template
   - **Video player**:
     - HTML5 `<video>` element with `controls` attribute
     - Source: serve `draft.mp4` via a new route `GET /episodes/<ep_id>/video` that uses `send_file()` with `conditional=True` for byte-range support (enables seeking)
     - Display: duration, resolution, file size
   - **Chapter script sidebar**:
     - Load chapter JSON and display chapter titles + narration text
     - Scrollable list alongside the video
   - **Review actions**:
     - Approve button (green) — POST to approve endpoint
     - Reject button (red) — POST to reject endpoint, requires notes
     - Request Changes button (yellow) — POST to request-changes endpoint, requires notes
   - **Review history**:
     - Show previous ReviewDecisions for this episode's render stage
     - Timeline format: date, decision, notes
   - Follow existing review page patterns from Sprint 3 (correction diff viewer)

7. **Video file serving** — new route `GET /episodes/<ep_id>/video`:
   - Use `flask.send_file()` with `conditional=True` for HTTP range requests
   - This enables video seeking without downloading the entire file
   - Set correct MIME type: `video/mp4`
   - Handle missing file gracefully (404)

8. **Extend CLI review commands** (if not already comprehensive):
   - `btcedu review list [--status pending|approved|rejected] [--stage correct|adapt|render]`
   - `btcedu review approve <review_id> [--notes "..."]`
   - `btcedu review reject <review_id> --notes "..."`
   - `btcedu review request-changes <review_id> --notes "..."`
   - These should work for all review gates (RG1, RG2, RG3)
   - For RG3 approval: CLI should also compute and store artifact_hash

9. **Review gate hardening** — verify and fix the full review workflow:
   - RG1 (after CORRECT): pipeline creates ReviewTask, blocks, resumes on approval
   - RG2 (after ADAPT): pipeline creates ReviewTask, blocks, resumes on approval
   - RG3 (after RENDER): pipeline creates ReviewTask, blocks, records artifact_hash on approval, status → APPROVED
   - Rejection at any gate: episode goes back, can be re-processed
   - Request changes at any gate: notes stored, available for re-generation
   - Pipeline auto-resume: after approval via dashboard or CLI, pipeline can be triggered to continue

10. **Integrate into pipeline** — update `btcedu/core/pipeline.py`:
    - After RENDER: create ReviewTask (stage="render"), set episode to RENDERED
    - Pipeline checks for approved RG3 before advancing to APPROVED
    - APPROVED is a terminal state before PUBLISH (Sprint 11)

11. **Write tests**:
    - `tests/test_transitions.py`:
      - Fade transition: correct ffmpeg filters generated
      - Cut transition: no extra filters
      - Audio fade applied with video fade
    - `tests/test_review_gate_3.py`:
      - ReviewTask created after RENDER
      - Approval: artifact_hash computed, status → APPROVED
      - Rejection: status remains RENDERED, notes stored
      - Request changes: notes stored, re-render possible
    - `tests/test_video_review_ui.py`:
      - Video review route returns correct template
      - Video serving route returns MP4 with correct headers
      - Approve/reject/request-changes POST endpoints work
    - `tests/test_artifact_hash.py`:
      - SHA-256 computation produces correct hash
      - Hash stored in ReviewTask on approval
    - `tests/test_review_hardening.py`:
      - Full pipeline flow: all three review gates block and resume correctly
      - Rejection flow works for each gate
      - Pipeline auto-resume after approval
    - Existing review tests (Sprint 3) still pass

12. **Verify**:
    - Run `pytest tests/`
    - Pick a RENDERED episode
    - Verify ReviewTask (stage="render") was created
    - Open dashboard → review queue → see pending video review
    - Click review → video player loads, plays draft.mp4
    - Chapter script visible alongside
    - Click Approve → verify artifact_hash stored, status → APPROVED
    - Test Reject flow: reject → status stays RENDERED, notes stored
    - Test Request Changes: notes stored, re-render available
    - Verify transitions: re-render an episode with fade transitions, verify smooth fades
    - CLI: `btcedu review list --status pending` shows video review
    - CLI: `btcedu review approve <id>` works
    - Run `btcedu status` → verify v1 pipeline unaffected
    - Run full v2 pipeline on a new episode → verify all three review gates trigger and block

### Anti-scope-creep guardrails

- **Do NOT** implement YouTube publishing (Sprint 11).
- **Do NOT** implement the `publish_jobs` table (Sprint 11).
- **Do NOT** implement the YouTube service / OAuth2 (Sprint 11).
- **Do NOT** implement background music or intro/outro.
- **Do NOT** implement video editing / trim / re-cut tools.
- **Do NOT** implement complex transitions beyond fade and cut (slide/crossfade deferred).
- **Do NOT** modify existing stages (correct, translate, adapt, chapterize, imagegen, tts).
- **Do NOT** modify the render manifest format defined in Sprint 9 (extend if needed, don't break).
- **Do NOT** implement auto-approve for video review (all videos require manual review per master plan).

### Code patterns to follow

- **Review gate**: Follow existing RG1/RG2 patterns in `btcedu/core/reviewer.py` and pipeline integration.
- **Dashboard**: Follow existing review page patterns (correction diff viewer, adaptation diff viewer).
- **Video serving**: Use `flask.send_file()` with `conditional=True` for streaming support.
- **ffmpeg filters**: Follow existing filter construction patterns in `btcedu/services/ffmpeg_service.py`.
- **CLI commands**: Follow existing Click command patterns.

### What to output

For each file changed or created:
1. The full file path
2. The complete code change

At the end, provide:
- A summary of all files created and modified
- A list of what was intentionally deferred to Sprint 11
- Manual verification steps (including watching the draft video with transitions and testing all review flows)

---

## Constraints

- Preserve compatibility with the existing pipeline and patterns.
- Use small, safe, incremental steps.
- Do not ask clarifying questions; make reasonable assumptions and label them as `[ASSUMPTION]`.
- Review Gate 3 must use the same `ReviewTask`/`ReviewDecision` models as RG1 and RG2.
- The `artifact_hash` is critical for Sprint 11's pre-publish safety check. Ensure it is computed from the exact `draft.mp4` file and stored persistently.
- Video streaming: use byte-range support for large files. Do not require full download before playback.
- Transitions: keep it simple. Fade in/out with a 0.5-second duration is sufficient. Do not implement complex transition effects.

---

## Definition of Done

- [ ] Fade transitions work (fade in/out on chapter segments)
- [ ] Cut transitions work (existing behavior preserved)
- [ ] Renderer passes transition types from chapter JSON to ffmpeg service
- [ ] Review Gate 3 created: ReviewTask with stage="render" after RENDERED
- [ ] Pipeline blocks at RG3 until approved
- [ ] RG3 approval: `artifact_hash` (SHA-256 of draft.mp4) computed and stored
- [ ] RG3 approval: episode status → APPROVED
- [ ] RG3 rejection: status stays RENDERED, reviewer notes stored
- [ ] RG3 request changes: notes stored, re-render available
- [ ] Dashboard video review page: video player, chapter script, review actions
- [ ] Video streaming: byte-range support for large MP4 files
- [ ] Video review history shown (previous decisions)
- [ ] CLI review commands work for RG3 (list, approve, reject, request-changes)
- [ ] CLI approval computes artifact_hash
- [ ] All three review gates (RG1 + RG2 + RG3) block and resume correctly
- [ ] Rejection/request-changes flows work for all gates
- [ ] Pipeline resumes after approval
- [ ] All tests pass
- [ ] v1 pipeline unaffected

## Non-Goals

- YouTube publishing / PUBLISH stage (Sprint 11)
- `publish_jobs` table (Sprint 11)
- YouTube service / OAuth2 (Sprint 11)
- Background music / intro / outro
- Video editing / trim / re-cut tools
- Complex transitions (slide, crossfade, wipe)
- Auto-approve for video review
- Parallel rendering
- 4K resolution support
