# Sprint 2 — Implementation Output: Transcript Correction Stage

**Sprint**: 2 (Phase 1, Part 1)
**Implemented**: 2026-02-23
**Test result**: 309 passed, 0 failed (31.36s)

---

## 1. Scope Summary

Sprint 2 implements the **CORRECT** pipeline stage for the v2 pipeline. This stage takes a Whisper-generated German transcript, sends it to Claude for ASR error correction, and produces:
- A corrected transcript file
- A structured JSON diff documenting all changes
- A provenance JSON for traceability

The pipeline now branches based on `pipeline_version`: v1 episodes continue through CHUNK -> GENERATE -> REFINE, while v2 episodes go through CORRECT (and will later continue to TRANSLATE -> ADAPT -> ...).

---

## 2. Files Created

| File | Description |
|------|-------------|
| `btcedu/prompts/templates/correct_transcript.md` | German correction prompt template with YAML frontmatter |
| `btcedu/core/corrector.py` | Corrector module: `correct_transcript()`, diff computation, segmentation, idempotency |
| `tests/test_corrector.py` | 20 tests: 8 diff unit, 4 segmentation, 2 prompt split, 6 idempotency, 5 integration, 1 CLI |

## 3. Files Modified

| File | Changes |
|------|---------|
| `btcedu/core/pipeline.py` | Added `_V2_STAGES`, `_get_stages()`, `correct` branch in `_run_stage()`, updated `resolve_pipeline_plan()` to accept `settings`, updated `run_pending()`/`run_latest()` to include `CORRECTED` status, updated cost extraction |
| `btcedu/cli.py` | Added `btcedu correct` CLI command with `--episode-id` and `--force` flags |

---

## 4. Key Design Decisions

### Pipeline branching
- `_V1_STAGES` and `_V2_STAGES` are separate lists selected by `_get_stages(settings)`
- `resolve_pipeline_plan()` now accepts an optional `settings` parameter (defaults to v1 for backward compat)
- `_STAGES` is kept as an alias for `_V1_STAGES` for any code referencing it directly

### Prompt template splitting
- The `correct_transcript.md` template contains both system and user sections
- At runtime, `_split_prompt()` splits at the `# Transkript` header
- System section: German editor instructions + rules
- User section: `# Transkript\n\n{{ transcript }}\n\n# Ausgabeformat\n\n...`

### Idempotency
- Four-condition check: file exists + no `.stale` marker + prompt hash matches + input content hash matches
- Provenance JSON stores both `prompt_hash` and `input_content_hash` for future comparison
- `.stale` markers are checked but not created (cascade invalidation is deferred)

### Long transcript segmentation
- Transcripts >15,000 characters are split at paragraph breaks (`\n\n`)
- Each segment is sent as a separate Claude call
- Results are reassembled with `\n\n` joins
- If a single paragraph exceeds the limit, it's force-split at character boundaries

### Diff computation
- Uses `difflib.SequenceMatcher` at word level
- Change types: `replace`, `insert`, `delete` (from difflib opcodes)
- Category field is `"auto"` for all changes (categorization deferred)
- Context: surrounding words included with `...` delimiters

---

## 5. Assumptions Made

- `[ASSUMPTION]` No new migration needed — Sprint 1 already created all required tables and enum values
- `[ASSUMPTION]` Transcript input is `episode.transcript_path` (the `transcript.clean.de.txt` file)
- `[ASSUMPTION]` No `corrected_transcript_path` column on Episode — uses convention-based path: `{transcripts_dir}/{episode_id}/transcript.corrected.de.txt`
- `[SIMPLIFICATION]` Diff categories are all `"auto"` — automatic spelling/punctuation/grammar classification deferred
- `[ASSUMPTION]` Dry-run is settings-level (`DRY_RUN=true` in `.env`), following existing pattern — not a CLI flag
- `[ASSUMPTION]` `resolve_pipeline_plan()` signature change (`settings` as optional kwarg) is backward-compatible — all callers already have `settings` available

---

## 6. Test Results

```
309 passed, 0 failed, 33 warnings in 31.36s
```

### New tests (20 total in `tests/test_corrector.py`):

**Diff computation (8 tests)**:
- `test_no_changes` — identical text produces empty diff
- `test_replace` — word replacement detected correctly
- `test_insert` — word insertion detected
- `test_delete` — word deletion detected
- `test_context_included` — surrounding words in context field
- `test_summary_counts` — summary matches individual changes
- `test_category_is_auto` — all categories are "auto"
- `test_position_fields` — start_word/end_word present

**Segmentation (4 tests)**:
- `test_short_text` — text under limit stays as one segment
- `test_long_text_splits_at_paragraphs` — splits at `\n\n` boundaries
- `test_no_paragraph_breaks` — force-splits long text without paragraphs
- `test_exact_limit` — text at exact limit stays as one segment

**Prompt splitting (2 tests)**:
- `test_splits_at_marker` — correctly splits at `# Transkript`
- `test_no_marker_fallback` — empty system prompt when no marker

**Idempotency (6 tests)**:
- `test_fresh_correction` — all conditions met returns True
- `test_missing_corrected_file` — missing file returns False
- `test_stale_marker` — stale marker returns False
- `test_prompt_hash_mismatch` — changed prompt returns False
- `test_input_hash_mismatch` — changed input returns False
- `test_missing_provenance` — missing provenance returns False

**Integration (5 tests)**:
- `test_success_dry_run` — full pipeline: PipelineRun, files, status update, ContentArtifact
- `test_wrong_status` — ValueError for non-TRANSCRIBED episodes
- `test_not_found` — ValueError for missing episodes
- `test_idempotent` — second call skips with zero cost
- `test_force_reruns` — force creates new PipelineRun

**CLI (1 test)**:
- `test_help` — `btcedu correct --help` exits 0 with expected text

---

## 7. Manual Verification Steps

```bash
# 1. Set pipeline version to 2
echo "PIPELINE_VERSION=2" >> .env

# 2. Pick a transcribed episode
btcedu status

# 3. Run correction (dry-run first)
DRY_RUN=true btcedu correct --episode-id <ep_id>

# 4. Run real correction
btcedu correct --episode-id <ep_id>

# 5. Verify output files
ls data/transcripts/<ep_id>/transcript.corrected.de.txt
cat data/outputs/<ep_id>/review/correction_diff.json | python3 -m json.tool
cat data/outputs/<ep_id>/provenance/correct_provenance.json | python3 -m json.tool

# 6. Verify idempotency (should skip)
btcedu correct --episode-id <ep_id>

# 7. Force re-run
btcedu correct --episode-id <ep_id> --force

# 8. Verify v1 pipeline unaffected
PIPELINE_VERSION=1 btcedu status
```

---

## 8. Intentionally Deferred (Sprint 3+)

- **Dashboard diff viewer UI** — side-by-side view of original vs corrected transcript
- **Review gate integration** — creating ReviewTask after correction, blocking pipeline until approved
- **Review queue API endpoints** — Flask routes for listing/approving/rejecting reviews
- **Cascade invalidation** — creating `.stale` markers when upstream changes
- **Diff category classification** — automatic spelling/punctuation/grammar labeling
- **TRANSLATE stage** — Sprint 4
- **ADAPT stage** — Sprint 4-5
- **Auto-approve rules** — later sprint

---

## 9. Rollback / Safe Revert Notes

All changes are additive. Safe revert strategy:

1. **Delete new files**: `btcedu/core/corrector.py`, `btcedu/prompts/templates/correct_transcript.md`, `tests/test_corrector.py`
2. **Revert `pipeline.py`**: Restore `_STAGES` list, remove `_V2_STAGES`/`_get_stages()`/`correct` branch, revert `resolve_pipeline_plan()` signature, revert pending status lists
3. **Revert `cli.py`**: Remove the `correct` command block

No database changes were made. No migrations were added. The `EpisodeStatus.CORRECTED` enum value and `PipelineStage.CORRECT` were already present from Sprint 1.

Episodes that were corrected during testing will have `status="corrected"` in the database. To revert them: `UPDATE episodes SET status='transcribed' WHERE status='corrected';`
