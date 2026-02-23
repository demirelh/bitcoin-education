# Sprint 2 — Implementation Plan: Transcript Correction Stage

**Sprint**: 2 (Phase 1, Part 1)
**Generated**: 2026-02-23
**Source of truth**: `MASTERPLAN.md` §4 Phase 1, §5A, §8; `docs/sprints/sprint2-plan.md`
**Prerequisite**: Sprint 1 (Foundation) — completed

---

## 1. Sprint Scope Summary

Sprint 2 implements the **CORRECT** pipeline stage — the first new v2 stage that calls Claude to fix Whisper ASR errors in German Bitcoin/crypto transcripts. It produces a corrected transcript file and a structured JSON diff summarizing changes. The sprint covers the corrector core module, a correction prompt template (registered via PromptRegistry), a `correct` CLI command, pipeline integration for v2 episodes, provenance tracking, idempotency checks, and tests. It does **not** include the dashboard diff viewer, review gate integration, or review queue UI (those are Sprint 3).

---

## 2. Non-Goals (Explicit)

- No dashboard/UI changes
- No review gate integration (ReviewTask creation after correction is Sprint 3)
- No review queue API endpoints or UI
- No diff viewer component
- No modification to existing v1 pipeline stages (CHUNK, GENERATE, REFINE)
- No new database migrations (all required tables exist from Sprint 1)
- No translation or adaptation stages
- No cascade invalidation logic (deferred)
- No auto-approve rules
- No refactoring of existing code

---

## 3. File-Level Plan

### NEW Files

| File | Description |
|------|-------------|
| `btcedu/core/corrector.py` | `correct_transcript()` orchestrator + `CorrectionResult` dataclass + diff computation |
| `btcedu/prompts/templates/correct_transcript.md` | Correction prompt template with YAML frontmatter |
| `tests/test_corrector.py` | Unit + integration tests for corrector module |

### MODIFIED Files

| File | Changes |
|------|---------|
| `btcedu/core/pipeline.py` | Add v2 stages list `_V2_STAGES`, add `elif stage_name == "correct"` branch in `_run_stage()`, update `resolve_pipeline_plan()` to select stages by `pipeline_version`, update `run_pending()`/`run_latest()` to include v2 statuses, update cost extraction for `correct` stage |
| `btcedu/cli.py` | Add `correct` CLI command |

---

## 4. Correction Prompt Template

### `btcedu/prompts/templates/correct_transcript.md`

```markdown
---
name: correct_transcript
model: claude-sonnet-4-20250514
temperature: 0.2
max_tokens: 8192
description: Corrects Whisper ASR transcript errors in German Bitcoin/crypto content
author: content_owner
---

# System

Du bist ein erfahrener deutscher Transkript-Editor, spezialisiert auf Bitcoin- und Kryptowährungsinhalte. Deine Aufgabe ist es, automatisch generierte Whisper-Transkripte zu korrigieren.

## REGELN

1. **NUR KORRIGIEREN, NICHT ÄNDERN**: Korrigiere Transkriptionsfehler. Ändere NICHT den Inhalt, die Bedeutung oder den Ton.
2. **KEINE INHALTE HINZUFÜGEN**: Füge keine neuen Informationen, Erklärungen oder Kommentare hinzu.
3. **KEINE INHALTE ENTFERNEN**: Lösche keine Passagen, auch wenn sie inhaltlich fragwürdig erscheinen.
4. **NICHT ÜBERSETZEN**: Das Transkript bleibt auf Deutsch. Übersetze nichts.

## WAS ZU KORRIGIEREN IST

1. **Rechtschreibung**: Besonders technische Begriffe — "Bit Coin" → "Bitcoin", "Blok Chain" → "Blockchain", "Leitning" → "Lightning", "Sattoshi" → "Satoshi", "Mainieng" → "Mining"
2. **Zeichensetzung**: Fehlende Punkte, Kommata, Satzgrenzen. Whisper lässt häufig Satzzeichen weg.
3. **Grammatik**: Offensichtliche grammatikalische Fehler, die durch ASR entstanden sind (z.B. falsche Kasusendungen, fehlende Artikel).
4. **Wortgrenzen**: Falsch getrennte oder zusammengeführte Wörter — "an dererseits" → "andererseits", "zusammen fassung" → "Zusammenfassung"
5. **Zahlen und Einheiten**: Falsch erkannte Zahlen, Währungen oder Einheiten — "21.000.000 Bit Coins" → "21.000.000 Bitcoins"

## WAS NICHT ZU KORRIGIEREN IST

- Stilistische Eigenheiten des Sprechers
- Umgangssprachliche Formulierungen
- Wiederholungen oder Füllwörter (sind Teil des natürlichen Sprechens)
- Inhaltliche Aussagen (auch wenn sie fachlich fragwürdig erscheinen)

# Transkript

{{ transcript }}

# Ausgabeformat

Gib das korrigierte Transkript als reinen Text zurück. Keine Erklärungen, keine Kommentare, keine Markierungen der Änderungen.
```

---

## 5. Corrector Module Design

### `btcedu/core/corrector.py`

```python
@dataclass
class CorrectionResult:
    """Summary of transcript correction for one episode."""
    episode_id: str
    corrected_path: str
    diff_path: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    change_count: int
    input_char_count: int
    output_char_count: int


def correct_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> CorrectionResult:
    """Correct a Whisper transcript using Claude.

    Reads the cleaned German transcript, sends it to Claude for ASR
    error correction, writes the corrected transcript and a structured
    diff JSON.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-correct even if output exists.

    Returns:
        CorrectionResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
    """
```

**Key design decisions:**

- **Status gate**: Episode must be `TRANSCRIBED` (or `CORRECTED` with `force=True`). `[ASSUMPTION]` Unlike v1 where CHUNKED follows TRANSCRIBED, v2's CORRECT stage also accepts `TRANSCRIBED`. The v2 pipeline branches from TRANSCRIBED — v1 goes to CHUNK, v2 goes to CORRECT.

- **Input**: Read from `episode.transcript_path` (points to `transcript.clean.de.txt`).

- **Output paths**:
  - Corrected transcript: `{settings.transcripts_dir}/{episode_id}/transcript.corrected.de.txt`
  - Diff JSON: `{settings.outputs_dir}/{episode_id}/review/correction_diff.json`
  - Provenance: `{settings.outputs_dir}/{episode_id}/provenance/correct_provenance.json`
  - Dry-run: `{settings.outputs_dir}/{episode_id}/dry_run_correct.json`

- **Long transcript segmentation**: `[ASSUMPTION]` Transcripts >15,000 characters are split at paragraph breaks (double newlines). Each segment is sent as a separate Claude call. Results are concatenated. Token/cost are summed across all segments. The diff is computed on the reassembled full text.

  ```python
  SEGMENT_CHAR_LIMIT = 15_000  # ~10K tokens, well within model context

  def _segment_transcript(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
      """Split transcript into segments at paragraph breaks."""
  ```

- **PipelineRun tracking**: Creates a `PipelineRun(stage=PipelineStage.CORRECT, status=RunStatus.RUNNING)` before the API call. Updates to `SUCCESS`/`FAILED` after.

- **ContentArtifact**: Persists a `ContentArtifact(artifact_type="correct", ...)` with prompt hash and file path.

- **PromptRegistry integration**: On each call, loads the `correct_transcript` template via `PromptRegistry`, registers it if new, and uses its content hash for idempotency.

---

## 6. Diff Computation

### Algorithm

Use Python's `difflib.SequenceMatcher` to produce a structured diff between the original and corrected transcripts. The diff operates at the **word level** (split on whitespace) for meaningful change detection, with surrounding context.

`[SIMPLIFICATION]` Automatic category classification (spelling vs punctuation vs grammar) is deferred. Each change is classified as `"replace"`, `"insert"`, or `"delete"` based on the `difflib` opcode type. The `category` field is set to `"auto"` for all changes. Manual or LLM-based categorization can be added in a future sprint.

### Diff Format (MASTERPLAN §5A)

```json
{
  "episode_id": "abc123",
  "original_length": 15000,
  "corrected_length": 14800,
  "changes": [
    {
      "type": "replace",
      "original": "Bit Coin",
      "corrected": "Bitcoin",
      "context": "...und dann hat er über Bitcoin gesprochen...",
      "position": {"start_word": 42, "end_word": 44},
      "category": "auto"
    }
  ],
  "summary": {
    "total_changes": 42,
    "by_type": {"replace": 30, "insert": 5, "delete": 7}
  }
}
```

### Implementation

```python
def compute_correction_diff(
    original: str,
    corrected: str,
    episode_id: str,
    context_words: int = 5,
) -> dict:
    """Compute structured diff between original and corrected transcript.

    Uses difflib.SequenceMatcher on word-level tokens.

    Args:
        original: The original transcript text.
        corrected: The corrected transcript text.
        episode_id: Episode identifier for the output.
        context_words: Number of surrounding words for context.

    Returns:
        Dict matching the correction_diff.json format.
    """
```

The function:
1. Tokenizes both texts into words (preserving whitespace context for position tracking).
2. Uses `difflib.SequenceMatcher(None, orig_words, corr_words)`.
3. Iterates `get_opcodes()`, collecting `replace`, `insert`, `delete` operations.
4. For each change, extracts surrounding words as context.
5. Computes summary counts by type.
6. Returns the structured dict.

---

## 7. CLI Command Design

### `btcedu correct`

```python
@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to correct (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-correct even if output exists.")
@click.pass_context
def correct(ctx: click.Context, episode_ids: tuple[str, ...], force: bool) -> None:
    """Correct Whisper transcripts for specified episodes (v2 pipeline)."""
    from btcedu.core.corrector import correct_transcript

    settings = ctx.obj["settings"]
    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = correct_transcript(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {result.corrected_path} "
                    f"({result.change_count} changes, ${result.cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()
```

**Behavior**:
- Processes multiple episodes in sequence
- On success: prints corrected file path, change count, and cost
- On failure: prints error to stderr, continues to next episode
- `--force`: re-runs correction even if `transcript.corrected.de.txt` exists
- Dry-run is controlled by `settings.dry_run` (from `.env`), not a CLI flag (follows existing pattern)

---

## 8. Pipeline Integration

### 8.1 Stages Lists

Add a v2 stages list alongside the existing v1 list. `[ASSUMPTION]` The pipeline selects which stages list to use based on `settings.pipeline_version`. For `pipeline_version=2`, the v2 list starts from TRANSCRIBED and branches to CORRECT. For `pipeline_version=1`, the existing `_STAGES` is used unchanged.

```python
# Existing v1 stages (unchanged)
_V1_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("chunk", EpisodeStatus.TRANSCRIBED),
    ("generate", EpisodeStatus.CHUNKED),
    ("refine", EpisodeStatus.GENERATED),
]

# v2 stages (extends after TRANSCRIBED)
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    # Future sprints will add:
    # ("translate", EpisodeStatus.CORRECTED),
    # ("adapt", EpisodeStatus.TRANSLATED),
    # ...
]

# Keep _STAGES as alias for backward compat
_STAGES = _V1_STAGES


def _get_stages(settings: Settings) -> list[tuple[str, EpisodeStatus]]:
    """Return the appropriate stages list based on pipeline version."""
    if settings.pipeline_version >= 2:
        return _V2_STAGES
    return _V1_STAGES
```

### 8.2 `resolve_pipeline_plan()` Change

Update to accept `settings` and use `_get_stages(settings)` instead of the hardcoded `_STAGES`:

```python
def resolve_pipeline_plan(
    session: Session,
    episode: Episode,
    settings: Settings,           # NEW parameter
    force: bool = False,
) -> list[StagePlan]:
```

`[ASSUMPTION]` Adding `settings` as a parameter to `resolve_pipeline_plan()` is a minor signature change. All callers (`run_episode_pipeline`, CLI `run` command) already have `settings` available. The `force` parameter's position does not change — `settings` is inserted before it.

### 8.3 `_run_stage()` Change

Add the `correct` branch:

```python
elif stage_name == "correct":
    from btcedu.core.corrector import correct_transcript

    result = correct_transcript(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0
    return StageResult(
        "correct",
        "success",
        elapsed,
        detail=f"{result.change_count} corrections (${result.cost_usd:.4f})",
    )
```

### 8.4 `run_episode_pipeline()` Change

Update the function to pass `settings` to `resolve_pipeline_plan()`, and iterate `_get_stages(settings)` instead of `_STAGES`.

### 8.5 `run_pending()` and `run_latest()` Changes

Add `EpisodeStatus.CORRECTED` to the list of "pending" statuses that are eligible for pipeline processing (so that v2 episodes in CORRECTED status are picked up for future stages):

```python
Episode.status.in_([
    EpisodeStatus.NEW,
    EpisodeStatus.DOWNLOADED,
    EpisodeStatus.TRANSCRIBED,
    EpisodeStatus.CHUNKED,
    EpisodeStatus.GENERATED,
    # v2 statuses
    EpisodeStatus.CORRECTED,
])
```

`[ASSUMPTION]` Adding CORRECTED here is safe since there's no TRANSLATE stage yet — CORRECTED episodes will simply be skipped by the pipeline until Sprint 4 adds the next v2 stage.

### 8.6 Cost Extraction

Update the cost extraction loop in `run_episode_pipeline()` to also recognize the `correct` stage:

```python
if sr.stage in ("generate", "refine", "correct") and sr.status == "success" and "$" in sr.detail:
```

---

## 9. Provenance and Idempotency

### 9.1 Provenance JSON

Written to `{settings.outputs_dir}/{episode_id}/provenance/correct_provenance.json`:

```json
{
  "stage": "correct",
  "episode_id": "abc123",
  "timestamp": "2026-02-23T10:30:00Z",
  "prompt_name": "correct_transcript",
  "prompt_version": 1,
  "prompt_hash": "sha256:abc123...",
  "model": "claude-sonnet-4-20250514",
  "model_params": {
    "temperature": 0.2,
    "max_tokens": 8192
  },
  "input_files": ["data/transcripts/abc123/transcript.clean.de.txt"],
  "input_content_hash": "sha256:def456...",
  "output_files": [
    "data/transcripts/abc123/transcript.corrected.de.txt",
    "data/outputs/abc123/review/correction_diff.json"
  ],
  "input_tokens": 4500,
  "output_tokens": 5200,
  "cost_usd": 0.093,
  "duration_seconds": 12.5,
  "segments_processed": 1
}
```

### 9.2 Idempotency Check Logic

Following MASTERPLAN §8 (CORRECT Stage):

```python
def _is_correction_current(
    corrected_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing correction is still valid.

    Returns True (skip) if ALL of:
    1. corrected_path exists
    2. No .stale marker exists (corrected_path.with_suffix('.stale'))
    3. provenance_path exists and its prompt_hash matches prompt_content_hash
    4. provenance_path's input_content_hash matches input_content_hash
    """
```

**Input content hash**: SHA-256 of the `transcript.clean.de.txt` file content. If the transcript is re-run, this hash changes, invalidating the correction.

**Prompt content hash**: From `PromptRegistry.compute_hash()` on the `correct_transcript` template body. If the prompt is edited, this hash changes, invalidating the correction.

### 9.3 `.stale` Marker

`[ASSUMPTION]` The `.stale` marker file is checked but not created in Sprint 2. Creation of `.stale` markers is part of the cascade invalidation logic (deferred). The idempotency check simply looks for `{corrected_path}.stale` and treats its existence as "needs re-run".

---

## 10. Test Plan

### `tests/test_corrector.py`

| # | Test Function | Asserts | Type |
|---|---|---|---|
| 1 | `test_compute_correction_diff_no_changes` | Empty input produces `change_count=0` | Unit |
| 2 | `test_compute_correction_diff_replace` | Known replacement ("Bit Coin" → "Bitcoin") produces correct diff entry with type="replace" | Unit |
| 3 | `test_compute_correction_diff_insert` | Inserted punctuation produces type="insert" entry | Unit |
| 4 | `test_compute_correction_diff_delete` | Deleted word produces type="delete" entry | Unit |
| 5 | `test_compute_correction_diff_context` | Context window around each change has correct surrounding words | Unit |
| 6 | `test_compute_correction_diff_summary` | Summary counts match individual changes | Unit |
| 7 | `test_segment_transcript_short` | Text under limit returns single segment | Unit |
| 8 | `test_segment_transcript_long` | Text over limit splits at paragraph breaks | Unit |
| 9 | `test_segment_transcript_no_paragraphs` | Long text without paragraph breaks still produces segments | Unit |
| 10 | `test_is_correction_current_fresh` | Returns True when all conditions met | Unit |
| 11 | `test_is_correction_current_missing_file` | Returns False when corrected file doesn't exist | Unit |
| 12 | `test_is_correction_current_stale_marker` | Returns False when .stale marker exists | Unit |
| 13 | `test_is_correction_current_prompt_hash_mismatch` | Returns False when prompt hash changed | Unit |
| 14 | `test_is_correction_current_input_hash_mismatch` | Returns False when input hash changed | Unit |
| 15 | `test_correct_transcript_success` | Full integration: creates PipelineRun, writes corrected file, writes diff JSON, writes provenance, updates episode status to CORRECTED, persists ContentArtifact. Uses `settings.dry_run=True` to avoid real API calls. | Integration |
| 16 | `test_correct_transcript_wrong_status` | Raises ValueError when episode is not TRANSCRIBED | Integration |
| 17 | `test_correct_transcript_not_found` | Raises ValueError when episode_id doesn't exist | Integration |
| 18 | `test_correct_transcript_idempotent` | Second call without force skips (returns same result, zero API tokens) | Integration |
| 19 | `test_correct_transcript_force` | With `force=True`, re-runs even if output exists | Integration |
| 20 | `test_correct_cli_help` | `btcedu correct --help` exits 0 and shows expected text | CLI |

### Test Fixtures (in `tests/test_corrector.py` or `tests/conftest.py`)

```python
@pytest.fixture
def transcribed_episode(db_session, tmp_path):
    """Episode at TRANSCRIBED status with a transcript file."""
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)
    transcript_path = transcript_dir / "transcript.clean.de.txt"
    transcript_path.write_text(
        "Heute sprechen wir über Bit Coin und die Blok Chain Technologie.\n\n"
        "Es ist eine dezentrale Währung die von Sattoshi Nakamoto erfunden wurde.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Grundlagen",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.TRANSCRIBED,
        transcript_path=str(transcript_path),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()
    return episode
```

---

## 11. Implementation Order

Execute in this order. Each step should be independently testable.

1. **Create correction prompt template** — `btcedu/prompts/templates/correct_transcript.md`
   - Write the full template with YAML frontmatter
   - Verify `PromptRegistry.load_template()` can parse it (manual check or add to existing registry tests)

2. **Implement diff computation** — bottom-up, pure function first
   - `compute_correction_diff()` in `btcedu/core/corrector.py`
   - Write unit tests 1-6 (`test_compute_correction_diff_*`)
   - Run tests

3. **Implement transcript segmentation**
   - `_segment_transcript()` in `btcedu/core/corrector.py`
   - Write unit tests 7-9 (`test_segment_transcript_*`)
   - Run tests

4. **Implement idempotency check**
   - `_is_correction_current()` in `btcedu/core/corrector.py`
   - Write unit tests 10-14 (`test_is_correction_current_*`)
   - Run tests

5. **Implement `correct_transcript()` core function**
   - `CorrectionResult` dataclass
   - Full function: status validation, PromptRegistry load, idempotency check, Claude call, file writes (corrected text, diff JSON, provenance JSON), PipelineRun tracking, ContentArtifact persistence, episode status update
   - Write integration tests 15-19
   - Run tests

6. **Add `correct` CLI command** to `btcedu/cli.py`
   - Follow existing Click command pattern
   - Write CLI test 20
   - Run `btcedu correct --help` to verify

7. **Integrate into pipeline** — `btcedu/core/pipeline.py`
   - Add `_V2_STAGES` list
   - Add `_get_stages()` helper
   - Update `resolve_pipeline_plan()` signature to accept `settings`
   - Update `_run_stage()` with `correct` branch
   - Update `run_episode_pipeline()` to use `_get_stages(settings)`
   - Update `run_pending()` and `run_latest()` pending status lists
   - Update cost extraction
   - Run ALL existing pipeline tests to confirm no regression

8. **Run full test suite** — `pytest tests/` — all tests must pass

9. **Manual verification**
   - Set `PIPELINE_VERSION=2` in `.env`
   - Pick an existing transcribed episode (or create one with `btcedu transcribe`)
   - Run `btcedu correct --episode-id <ep_id>`
   - Verify: corrected file exists, diff JSON exists, provenance JSON exists
   - Run again → skipped (idempotent)
   - Run with `--force` → re-runs
   - Run `btcedu status` → episode shows `corrected` status
   - Set `PIPELINE_VERSION=1` → existing v1 pipeline still works

---

## 12. Definition of Done

- [ ] All existing tests pass (`pytest tests/` — 0 failures, 0 regressions)
- [ ] `btcedu/prompts/templates/correct_transcript.md` exists with YAML frontmatter and German correction instructions
- [ ] `correct_transcript()` function processes a TRANSCRIBED episode and produces:
  - `transcript.corrected.de.txt` in the transcripts directory
  - `correction_diff.json` in the outputs/review directory
  - `correct_provenance.json` in the outputs/provenance directory
  - `ContentArtifact` record in the database
  - `PipelineRun` record in the database
  - Episode status updated to `CORRECTED`
- [ ] Diff JSON matches the format from MASTERPLAN §5A (type, original, corrected, context, position, summary)
- [ ] Idempotency: running `correct_transcript()` twice without `force` skips the second run (zero tokens, zero cost)
- [ ] Force: running with `force=True` re-corrects even if output exists
- [ ] Long transcripts (>15,000 chars) are segmented at paragraph breaks and reassembled
- [ ] Prompt is registered via `PromptRegistry` on first use; prompt hash stored in provenance
- [ ] Input content hash (SHA-256 of transcript) stored in provenance for idempotency
- [ ] `btcedu correct --help` shows correct usage
- [ ] `btcedu correct --episode-id <id>` works end-to-end (with dry-run)
- [ ] Pipeline integration: `pipeline_version=2` routes TRANSCRIBED episodes to CORRECT stage
- [ ] Pipeline integration: `pipeline_version=1` still routes TRANSCRIBED episodes to CHUNK stage (no regression)
- [ ] `btcedu status` still works
- [ ] `btcedu cost` reports CORRECT stage costs correctly
- [ ] 20 tests in `tests/test_corrector.py` pass
- [ ] No modifications to legacy prompt modules (`btcedu/prompts/system.py`, etc.)

---

## 13. Assumptions

- `[ASSUMPTION]` **No migration needed**: All required database tables (`prompt_versions`, `review_tasks`, etc.) and columns (`pipeline_version` on Episode) were created in Sprint 1. The CORRECT stage uses existing `PipelineRun` and `ContentArtifact` tables with `PipelineStage.CORRECT` enum value (already defined).

- `[ASSUMPTION]` **Transcript input is `transcript.clean.de.txt`**: The corrector reads from `episode.transcript_path`, which points to the clean (whitespace-normalized) Whisper output. This is the appropriate input since it preserves all content but removes trivial formatting issues.

- `[ASSUMPTION]` **No `corrected_transcript_path` column on Episode**: Instead of adding a new DB column, we use a convention-based path: `{transcripts_dir}/{episode_id}/transcript.corrected.de.txt`. Downstream stages (TRANSLATE in Sprint 4) will reconstruct this path from the episode_id. This avoids a migration and keeps the schema minimal.

- `[SIMPLIFICATION]` **Diff categories are `"auto"`**: Automatic classification of changes into spelling/punctuation/grammar categories is deferred. All changes get `category: "auto"`. This can be enhanced later with a second LLM pass or heuristics (e.g., punctuation-only changes detected by regex).

- `[ASSUMPTION]` **Segment limit is 15,000 characters**: This translates to roughly 10,000 tokens (at ~1.5 chars/token for German), well within Claude Sonnet's context window. Most podcast transcripts (15-30 min) will be 5,000-20,000 characters, so most will be processed in a single segment. Only transcripts from very long episodes (>45 min) would require segmentation.

- `[ASSUMPTION]` **Dry-run is settings-level**: Following the existing pattern in `generator.py`, dry-run is controlled by `settings.dry_run` (from `DRY_RUN=true` in `.env`), not by a `--dry-run` CLI flag on the `correct` command. This is consistent with how `generate` and `refine` commands work.

- `[ASSUMPTION]` **`resolve_pipeline_plan()` signature change is safe**: Adding `settings` as a parameter changes the function signature. The only callers are `run_episode_pipeline()` and the `run` CLI command (which calls `run_episode_pipeline`). Both already have `settings` in scope. This is a minimal, safe change.

- `[ASSUMPTION]` **v2 pipeline terminates after CORRECT in Sprint 2**: Since TRANSLATE doesn't exist yet, v2 episodes will reach CORRECTED status and stop. The pipeline reports this as successful (no error). Future sprints will extend `_V2_STAGES` with additional stages.

---

## 14. Detailed `correct_transcript()` Flow

```
1. Validate: episode exists, status is TRANSCRIBED (or CORRECTED+force)
2. Resolve paths:
   - input_path = episode.transcript_path
   - corrected_path = transcripts_dir / episode_id / "transcript.corrected.de.txt"
   - diff_path = outputs_dir / episode_id / "review" / "correction_diff.json"
   - provenance_path = outputs_dir / episode_id / "provenance" / "correct_provenance.json"
3. Load prompt template via PromptRegistry:
   - registry = PromptRegistry(session)
   - template_path = TEMPLATES_DIR / "correct_transcript.md"
   - pv = registry.register_version("correct_transcript", template_path, set_default=True)
   - _, body = registry.load_template(template_path)
   - prompt_hash = registry.compute_hash(body)
4. Compute input content hash: SHA-256 of input file content
5. Idempotency check: _is_correction_current(corrected_path, provenance_path, input_hash, prompt_hash)
   - If current and not force: return early with zero-cost CorrectionResult
6. Create PipelineRun(stage=CORRECT, status=RUNNING)
7. Read input transcript text
8. Segment if needed: _segment_transcript(text)
9. For each segment:
   - Render prompt: body.replace("{{ transcript }}", segment)
   - Build system prompt (extract from template or use body up to "# Transkript")
   - Call call_claude(system_prompt, user_message, settings, dry_run_path)
   - Accumulate tokens and cost
   - Collect corrected text
10. Reassemble corrected segments
11. Compute diff: compute_correction_diff(original, corrected, episode_id)
12. Write files:
    - corrected_path.write_text(corrected_text)
    - diff_path.write_text(json.dumps(diff))
    - provenance_path.write_text(json.dumps(provenance))
13. Persist ContentArtifact(artifact_type="correct", prompt_hash=prompt_hash, ...)
14. Update PipelineRun: status=SUCCESS, tokens, cost
15. Update Episode: status=CORRECTED
16. session.commit()
17. Return CorrectionResult
```

**Error handling**: Any exception after step 6 is caught in a try/except. The PipelineRun is set to FAILED, the episode's `error_message` is set, and the exception is re-raised. This matches the pattern in `generate_content()`.

---

## 15. Prompt System/User Split

`[ASSUMPTION]` The correction prompt template contains both system-level instructions and the user message with the `{{ transcript }}` variable. At runtime, the corrector splits the template:

- **System prompt**: Everything from the start of the body up to (but not including) `# Transkript`
- **User message**: `# Transkript\n\n{transcript_text}\n\n# Ausgabeformat\n\n...`

This approach keeps the prompt as a single template file (easy to version and edit) while properly separating system and user roles for the Claude API call. The split is done by finding the `# Transkript` header in the rendered body.

```python
def _split_prompt(rendered_body: str) -> tuple[str, str]:
    """Split rendered template into system prompt and user message.

    The template is split at the '# Transkript' header.
    Everything before it becomes the system prompt.
    Everything from '# Transkript' onward becomes the user message.
    """
    marker = "# Transkript"
    idx = rendered_body.find(marker)
    if idx == -1:
        # Fallback: entire body is user message, no system prompt
        return ("", rendered_body)
    system = rendered_body[:idx].strip()
    user = rendered_body[idx:].strip()
    return (system, user)
```
