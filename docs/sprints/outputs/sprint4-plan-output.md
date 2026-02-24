# Sprint 4 Implementation Plan: Turkish Translation Stage

**Generated**: 2026-02-24
**Sprint**: 4 (Phase 2, Part 1)
**Status**: Implementation-Ready
**Dependencies**: Sprint 1-3 (Foundation + Correction + Review System) completed

---

## 1. Sprint Scope Summary

**In Scope**:
Sprint 4 implements the **TRANSLATE** stage for the btcedu v2 pipeline, providing faithful German-to-Turkish translation of corrected transcripts. This stage follows Review Gate 1 (transcript correction approval) and precedes the ADAPT stage (Sprint 5). The translator will:
- Accept corrected German transcripts as input
- Perform faithful, high-fidelity translation to Turkish using Claude
- Preserve technical terminology with original terms in parentheses
- Handle long transcripts via paragraph-level segmentation
- Track provenance, implement idempotency, and support cascade invalidation
- Integrate into the v2 pipeline orchestration
- Provide CLI command (`btcedu translate`) for manual execution

**Explicitly NOT In Scope**:
- Cultural adaptation or localization (that's Sprint 5's ADAPT stage)
- Review gate after translation (review happens after adaptation)
- Dashboard UI changes (deferred to later dashboard enhancement sprint)
- Modification of existing v1 pipeline or correction/review systems
- Translation quality metrics or evaluation framework
- Multi-language support beyond German→Turkish
- Translation memory or glossary management

---

## 2. File-Level Plan

### Files to CREATE:

#### 2.1 `btcedu/core/translator.py`
**Purpose**: Core translation logic
**Key Contents**:
- `translate_transcript()` — main entry point
- `TranslationResult` — result dataclass
- `_segment_text()` — paragraph-aware text splitting
- `_translate_segment()` — single segment translation
- `_check_idempotency()` — skip if already done
- `_write_provenance()` — provenance JSON writer
- Error handling with PipelineRun tracking

#### 2.2 `btcedu/prompts/templates/translate.md`
**Purpose**: Translation prompt template with YAML frontmatter
**Key Contents**:
- YAML metadata (name, model, temperature, max_tokens, description, author)
- System instructions for faithful German→Turkish translation
- Constraints (preserve technical terms, handle code/URLs, keep speaker names)
- Input variable: `{{ transcript }}`
- Optional reviewer feedback injection: `{{ reviewer_feedback }}`
- Output format specification

#### 2.3 `tests/test_translator.py`
**Purpose**: Unit and integration tests for translator module
**Key Contents**:
- `test_segment_text_basic()` — paragraph splitting
- `test_segment_text_long_paragraph()` — fallback to sentence splitting
- `test_translate_transcript_idempotent()` — skip on second run
- `test_translate_transcript_force()` — reprocess with --force
- `test_translate_transcript_dry_run()` — no API call in dry-run mode
- `test_translate_transcript_creates_provenance()` — provenance file validation
- `test_translate_transcript_updates_episode_status()` — status transition
- `test_cascade_invalidation()` — .stale marker created when correction changes

### Files to MODIFY:

#### 2.4 `btcedu/models/episode.py`
**Changes**: None required (EpisodeStatus.TRANSLATED already added in Sprint 1)
**Verification**: Confirm `TRANSLATED = "translated"` exists in enum

#### 2.5 `btcedu/core/pipeline.py`
**Changes**:
1. Add `("translate", EpisodeStatus.CORRECTED)` to `_V2_STAGES` list (after review_gate_1)
2. Add `elif stage_name == "translate":` branch to `_run_stage()` function:
   ```python
   elif stage_name == "translate":
       from btcedu.core.translator import translate_transcript

       result = translate_transcript(session, episode.episode_id, settings, force=force)
       elapsed = time.monotonic() - t0
       return StageResult(
           "translate",
           "success",
           elapsed,
           detail=f"Translated to Turkish ({result.output_char_count} chars, ${result.cost_usd:.4f})",
       )
   ```
3. Update `STAGE_DEPENDENCIES` dict (if it exists) to include `"translate": ["correct"]`

#### 2.6 `btcedu/cli.py`
**Changes**: Add new `translate` command
```python
@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to translate (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-translate even if output exists.")
@click.option("--dry-run", is_flag=True, default=False, help="Write request JSON instead of calling API.")
@click.pass_context
def translate(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Translate corrected German transcripts to Turkish."""
    from btcedu.core.translator import translate_transcript

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = translate_transcript(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {result.translated_path} "
                    f"({result.input_char_count}→{result.output_char_count} chars, ${result.cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()
```

---

## 3. Translation Prompt Template

Full draft of `btcedu/prompts/templates/translate.md`:

```markdown
---
name: translate
model: claude-sonnet-4-20250514
temperature: 0.2
max_tokens: 8192
description: Faithful German→Turkish translation of Bitcoin/crypto content
author: content_owner
---

# System

You are a professional German-to-Turkish translator specializing in Bitcoin, cryptocurrency, and financial technology content. Your task is to produce a faithful, high-quality translation that preserves the exact meaning, tone, and technical accuracy of the original German text.

# Instructions

Translate the following German transcript to Turkish. Follow these rules strictly:

## Translation Requirements

1. **Faithful Rendering**: Translate the meaning precisely. Do not add, remove, or reinterpret information.
2. **Technical Terminology**: Keep Bitcoin/crypto technical terms in their original form with Turkish equivalent in parentheses on first use. Examples:
   - "Mining" → "madencilik (Mining)"
   - "Proof of Work" → "İş İspatı (Proof of Work)"
   - "Lightning Network" → "Lightning Network"
   - "Halving" → "yarılanma (Halving)"
3. **Tone**: Maintain the original tone (formal, casual, technical, conversational, etc.)
4. **Speaker Names**: Keep speaker names unchanged. If attributions like "Sprecher A:" exist, preserve them.
5. **Code/URLs**: Pass through code snippets, URLs, email addresses, and technical identifiers unchanged.
6. **Numbers**: Preserve numeric values exactly. Keep currency symbols (€, $, ₿) as-is.
7. **Paragraph Structure**: Maintain the original paragraph breaks and structure.
8. **German Cultural References**: Translate literally without adaptation. (Adaptation happens in the next stage.)
9. **Quotes**: Preserve quoted text as quotes. Use Turkish quotation conventions (tırnak işaretleri).

## Forbidden Actions

- Do NOT add explanations, footnotes, or commentary
- Do NOT adapt cultural references or examples (that's a separate stage)
- Do NOT change or simplify technical explanations
- Do NOT invent information not in the source
- Do NOT translate proper names (people, organizations, brands) unless commonly translated
- Do NOT add financial advice, investment recommendations, or price predictions

{{ reviewer_feedback }}

# Input

{{ transcript }}

# Output Format

Return ONLY the translated Turkish text. No preamble, no metadata, no explanations. Just the translation.
```

**Key Design Decisions**:
- **Faithful, not adaptive**: This stage is mechanical translation; cultural adaptation is Sprint 5
- **Technical term handling**: Preserve original with Turkish equivalent in parentheses (first occurrence)
- **Reviewer feedback injection point**: `{{ reviewer_feedback }}` allows iterative improvement after human review rejects a translation
- **Paragraph-level fidelity**: Maintains structure for downstream adapter stage
- **Strict output format**: No wrapper, just the translation (makes parsing trivial)

---

## 4. Translator Module Design

### 4.1 Function Signature

```python
def translate_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> TranslationResult:
    """
    Translate a corrected German transcript to Turkish.

    Args:
        session: SQLAlchemy session
        episode_id: Episode identifier
        settings: Application settings
        force: If True, re-translate even if output exists

    Returns:
        TranslationResult with paths, metrics, and cost

    Raises:
        ValueError: If episode not found or status invalid
        FileNotFoundError: If corrected transcript missing
        RuntimeError: If translation fails
    """
```

### 4.2 Result Dataclass

```python
from dataclasses import dataclass

@dataclass
class TranslationResult:
    """Summary of translation operation for one episode."""

    episode_id: str
    translated_path: str          # Path to transcript.tr.txt
    provenance_path: str          # Path to translate_provenance.json
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0     # German text length
    output_char_count: int = 0    # Turkish text length
    segments_processed: int = 1   # How many segments (1 if no segmentation)
    skipped: bool = False         # True if idempotent skip
```

### 4.3 Core Logic Flow

```python
def translate_transcript(...) -> TranslationResult:
    # 1. Validate episode exists and is in correct status
    episode = session.query(Episode).filter_by(episode_id=episode_id).one_or_none()
    if not episode:
        raise ValueError(f"Episode {episode_id} not found")

    if episode.status != EpisodeStatus.CORRECTED:
        raise ValueError(f"Episode {episode_id} is not CORRECTED (current: {episode.status})")

    # 2. Define paths
    corrected_path = Path(f"data/transcripts/{episode_id}/transcript.corrected.de.txt")
    if not corrected_path.exists():
        raise FileNotFoundError(f"Corrected transcript not found: {corrected_path}")

    translated_path = Path(f"data/transcripts/{episode_id}/transcript.tr.txt")
    provenance_path = Path(f"data/outputs/{episode_id}/provenance/translate_provenance.json")
    provenance_path.parent.mkdir(parents=True, exist_ok=True)

    # 3. Check idempotency (skip if already done and not forced)
    if not force:
        skip_result = _check_idempotency(
            session, episode_id, translated_path, provenance_path, corrected_path, settings
        )
        if skip_result:
            return skip_result

    # 4. Load and validate prompt
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "translate.md"
    prompt_version = registry.register_version("translate", template_file, set_default=True)
    metadata, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # 5. Inject reviewer feedback if present
    reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "translate")
    if reviewer_feedback:
        feedback_block = (
            "## Revisor Geri Bildirimi (lütfen bu düzeltmeleri uygulayın)\n\n"
            f"{reviewer_feedback}\n\n"
            "Önemli: Bu geri bildirimi çıktıda aynen aktarmayın, yalnızca düzeltme kılavuzu olarak kullanın."
        )
        template_body = template_body.replace("{{ reviewer_feedback }}", feedback_block)
    else:
        template_body = template_body.replace("{{ reviewer_feedback }}", "")

    # 6. Read input
    corrected_text = corrected_path.read_text(encoding="utf-8")
    input_char_count = len(corrected_text)
    input_content_hash = hashlib.sha256(corrected_text.encode("utf-8")).hexdigest()

    # 7. Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSLATE,
        status=RunStatus.RUNNING,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.flush()  # Get ID

    try:
        # 8. Segment and translate
        segments = _segment_text(corrected_text, max_chars=15000)
        translated_segments = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0

        for i, segment in enumerate(segments):
            user_message = template_body.replace("{{ transcript }}", segment)
            system_prompt = metadata.get("system_prompt", "You are a professional translator.")

            dry_run_path = None
            if settings.dry_run:
                dry_run_path = Path(f"data/outputs/{episode_id}/dry_run_translate_seg{i}.json")

            response = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
            )

            translated_segments.append(response.text)
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

        # 9. Rejoin segments
        translated_text = "\n\n".join(translated_segments)
        output_char_count = len(translated_text)

        # 10. Write output
        translated_path.write_text(translated_text, encoding="utf-8")

        # 11. Write provenance
        _write_provenance(
            provenance_path,
            episode_id,
            prompt_version,
            prompt_content_hash,
            metadata,
            corrected_path,
            input_content_hash,
            translated_path,
            total_input_tokens,
            total_output_tokens,
            total_cost,
            len(segments),
        )

        # 12. Create ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="translate",
            file_path=str(translated_path),
            model=settings.claude_model,
            prompt_hash=prompt_content_hash,
            retrieval_snapshot_path=None,
        )
        session.add(artifact)

        # 13. Update PipelineRun and Episode status
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost

        episode.status = EpisodeStatus.TRANSLATED
        episode.error_message = None

        session.commit()

        return TranslationResult(
            episode_id=episode_id,
            translated_path=str(translated_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=input_char_count,
            output_char_count=output_char_count,
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise
```

### 4.4 Helper Functions

```python
def _check_idempotency(
    session: Session,
    episode_id: str,
    translated_path: Path,
    provenance_path: Path,
    corrected_path: Path,
    settings: Settings,
) -> TranslationResult | None:
    """
    Check if translation is already done and up-to-date.

    Returns TranslationResult if skipping, None if need to process.
    """
    # Check output exists
    if not translated_path.exists():
        return None

    # Check for stale marker (created by reviewer on rejection or upstream change)
    stale_marker = Path(str(translated_path) + ".stale")
    if stale_marker.exists():
        logger.info(f"Translation marked stale (reason: {stale_marker.read_text()})")
        stale_marker.unlink()
        return None

    # Load provenance
    if not provenance_path.exists():
        logger.info("Provenance missing, re-translating")
        return None

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

    # Check input hash
    current_input_hash = hashlib.sha256(
        corrected_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    if provenance.get("input_content_hash") != current_input_hash:
        logger.info("Input changed, re-translating")
        return None

    # Check prompt hash
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "translate.md"
    _, template_body = registry.load_template(template_file)
    current_prompt_hash = registry.compute_hash(template_body)
    if provenance.get("prompt_hash") != current_prompt_hash:
        logger.info("Prompt changed, re-translating")
        return None

    # All checks passed — skip
    logger.info(f"Translation already up-to-date for {episode_id}, skipping")
    return TranslationResult(
        episode_id=episode_id,
        translated_path=str(translated_path),
        provenance_path=str(provenance_path),
        input_tokens=provenance.get("input_tokens", 0),
        output_tokens=provenance.get("output_tokens", 0),
        cost_usd=provenance.get("cost_usd", 0.0),
        input_char_count=len(corrected_path.read_text(encoding="utf-8")),
        output_char_count=len(translated_path.read_text(encoding="utf-8")),
        segments_processed=provenance.get("segments_processed", 1),
        skipped=True,
    )


def _segment_text(text: str, max_chars: int = 15000) -> list[str]:
    """
    Split text into segments by paragraph breaks, respecting max_chars limit.

    Strategy:
    1. Split on double newlines (paragraph boundaries)
    2. Group paragraphs into segments <= max_chars
    3. If a single paragraph exceeds max_chars, split on sentence boundaries

    Args:
        text: Input text to segment
        max_chars: Maximum characters per segment (default: 15000)

    Returns:
        List of text segments (non-empty)
    """
    # Split into paragraphs
    paragraphs = text.split("\n\n")

    segments = []
    current_segment = []
    current_length = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # If single paragraph exceeds max_chars, split by sentences
        if para_len > max_chars:
            # Flush current segment
            if current_segment:
                segments.append("\n\n".join(current_segment))
                current_segment = []
                current_length = 0

            # Split long paragraph by sentences (approximate with ". ")
            sentences = para.split(". ")
            sent_segment = []
            sent_len = 0
            for sent in sentences:
                if sent_len + len(sent) + 2 > max_chars:
                    if sent_segment:
                        segments.append(". ".join(sent_segment) + ".")
                    sent_segment = [sent]
                    sent_len = len(sent)
                else:
                    sent_segment.append(sent)
                    sent_len += len(sent) + 2
            if sent_segment:
                segments.append(". ".join(sent_segment) + ".")

        # Normal paragraph fits in segment
        elif current_length + para_len + 2 <= max_chars:
            current_segment.append(para)
            current_length += para_len + 2

        # Start new segment
        else:
            if current_segment:
                segments.append("\n\n".join(current_segment))
            current_segment = [para]
            current_length = para_len

    # Flush remaining
    if current_segment:
        segments.append("\n\n".join(current_segment))

    return segments if segments else [text]  # Fallback: return original as single segment


def _write_provenance(
    provenance_path: Path,
    episode_id: str,
    prompt_version: PromptVersion,
    prompt_hash: str,
    metadata: dict,
    input_file: Path,
    input_content_hash: str,
    output_file: Path,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    segments_processed: int,
) -> None:
    """Write translation provenance JSON."""
    provenance = {
        "stage": "translate",
        "episode_id": episode_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "prompt_name": "translate",
        "prompt_version": prompt_version.version,
        "prompt_hash": prompt_hash,
        "model": metadata.get("model", "claude-sonnet-4-20250514"),
        "model_params": {
            "temperature": metadata.get("temperature", 0.2),
            "max_tokens": metadata.get("max_tokens", 8192),
        },
        "input_files": [str(input_file)],
        "input_content_hash": input_content_hash,
        "output_files": [str(output_file)],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 6),
        "duration_seconds": 0.0,  # Can add timing if needed
        "segments_processed": segments_processed,
    }
    provenance_path.write_text(json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8")
```

---

## 5. Segmentation Strategy

### 5.1 Rationale

Long transcripts (>15,000 characters) exceed practical context limits for translation quality. Segmentation ensures:
- Consistent translation quality across entire transcript
- Better handling of context boundaries
- Reduced API timeout risk
- Modular retry on segment failure

### 5.2 Algorithm

**[ASSUMPTION]** Max segment size: 15,000 characters (matches corrector pattern, well within Claude's 200K context window, leaves room for prompt overhead)

```
1. Split input on paragraph breaks ("\n\n")
2. For each paragraph:
   a. If paragraph alone > max_chars:
      - Split by sentence boundaries (". ")
      - Create sub-segments <= max_chars
   b. Otherwise:
      - Accumulate paragraphs into segments until max_chars reached
      - Start new segment when next paragraph would exceed limit
3. Return list of segments (each is valid standalone text)
```

### 5.3 Reassembly

```python
translated_text = "\n\n".join(translated_segments)
```

Segments are rejoined with double newlines to preserve paragraph structure. No overlap or stitching logic needed because Claude understands paragraph context.

**[ASSUMPTION]** No cross-segment context window needed. German paragraph structure is preserved, so Turkish translation naturally flows when paragraphs are reassembled.

### 5.4 Edge Cases

| Case | Handling |
|------|----------|
| Empty input | Return empty string (no segments) |
| Single short paragraph | Single segment (no splitting) |
| No paragraph breaks | Split on sentence boundaries at ~15K chars |
| Paragraph with no sentences | Hard-split at character limit (rare) |
| Non-UTF8 input | Raise encoding error (corrector guarantees UTF-8) |

---

## 6. CLI Command Design

### 6.1 Command Signature

```python
@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to translate (repeatable).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-translate even if output exists and is up-to-date.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Write request JSON to file instead of calling Claude API.",
)
@click.pass_context
def translate(ctx, episode_ids, force, dry_run):
    """Translate corrected German transcripts to Turkish."""
```

### 6.2 Usage Examples

```bash
# Translate single episode
btcedu translate --episode-id abc123

# Translate multiple episodes
btcedu translate --episode-id abc123 --episode-id def456

# Force re-translation
btcedu translate --episode-id abc123 --force

# Dry-run (no API call)
btcedu translate --episode-id abc123 --dry-run

# Check help
btcedu translate --help
```

### 6.3 Output Messages

```
[OK] abc123 -> data/transcripts/abc123/transcript.tr.txt (14523→14891 chars, $0.0234)
[SKIP] abc123 -> already up-to-date (idempotent)
[FAIL] abc123: Episode not found
[FAIL] def456: Episode status is 'transcribed', expected 'corrected'
```

### 6.4 Error Handling

```python
try:
    result = translate_transcript(session, eid, settings, force=force)
    if result.skipped:
        click.echo(f"[SKIP] {eid} -> already up-to-date (idempotent)")
    else:
        click.echo(
            f"[OK] {eid} -> {result.translated_path} "
            f"({result.input_char_count}→{result.output_char_count} chars, "
            f"${result.cost_usd:.4f})"
        )
except ValueError as e:
    click.echo(f"[FAIL] {eid}: {e}", err=True)
    # Continue to next episode (don't fail entire batch)
except Exception as e:
    click.echo(f"[FAIL] {eid}: Unexpected error: {e}", err=True)
    # Continue to next episode
```

---

## 7. Pipeline Integration

### 7.1 Changes to `pipeline.py`

#### Add to `_V2_STAGES` List

```python
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate", EpisodeStatus.CORRECTED),  # NEW
    ("adapt", EpisodeStatus.TRANSLATED),     # Placeholder for Sprint 5
]
```

**[ASSUMPTION]** Review Gate 1 sets episode status to CORRECTED *after* approval. The translate stage checks that status == CORRECTED, implicitly verifying approval.

#### Add to `_run_stage()` Function

```python
elif stage_name == "translate":
    from btcedu.core.translator import translate_transcript

    result = translate_transcript(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0

    if result.skipped:
        return StageResult("translate", "skipped", elapsed, detail="Already up-to-date")
    else:
        return StageResult(
            "translate",
            "success",
            elapsed,
            detail=f"Translated to Turkish ({result.output_char_count} chars, ${result.cost_usd:.4f})",
        )
```

#### Update `STAGE_DEPENDENCIES` (if exists)

```python
STAGE_DEPENDENCIES = {
    "download": [],
    "transcribe": ["download"],
    "correct": ["transcribe"],
    "translate": ["correct"],  # NEW
    "adapt": ["translate"],    # Placeholder for Sprint 5
    # ...
}
```

### 7.2 Pipeline Flow Verification

**Before Translation**:
- Episode must be in `CORRECTED` status
- ReviewTask with stage="correct" must have status="approved" (verified implicitly by status)
- File `data/transcripts/{ep_id}/transcript.corrected.de.txt` must exist

**After Translation**:
- Episode status → `TRANSLATED`
- File `data/transcripts/{ep_id}/transcript.tr.txt` created
- File `data/outputs/{ep_id}/provenance/translate_provenance.json` created
- ContentArtifact record created
- PipelineRun record marked SUCCESS

**Failure Handling**:
- Episode status remains `CORRECTED`
- PipelineRun marked FAILED with error_message
- Episode.error_message set
- Pipeline halts (does not proceed to ADAPT stage)

---

## 8. Cascade Invalidation

### 8.1 Invalidation Logic

**Trigger**: When CORRECT stage re-runs (e.g., after Review Gate 1 rejection and regeneration), downstream outputs must be invalidated.

**Affected Stages**:
- TRANSLATE (direct dependency)
- ADAPT (indirect: depends on TRANSLATE)
- All subsequent stages (CHAPTERIZE, IMAGE_GEN, TTS, RENDER, PUBLISH)

### 8.2 Implementation

**Option A: Extend `invalidate_downstream()` in `pipeline.py`** (if it exists from Sprint 3)

```python
def invalidate_downstream(session, episode_id, from_stage):
    """
    Mark all downstream stages as needing re-run.

    Creates .stale marker files and resets episode status to the changed stage.
    """
    stage_order = ["correct", "translate", "adapt", "chapterize", "imagegen", "tts", "render", "publish"]

    try:
        from_index = stage_order.index(from_stage)
    except ValueError:
        logger.warning(f"Unknown stage {from_stage}, skipping invalidation")
        return

    downstream_stages = stage_order[from_index + 1:]

    for stage in downstream_stages:
        output_paths = _get_stage_outputs(episode_id, stage)
        for output_path in output_paths:
            stale_marker = Path(str(output_path) + ".stale")
            stale_marker.write_text(
                json.dumps({
                    "invalidated_by": from_stage,
                    "invalidated_at": datetime.utcnow().isoformat() + "Z",
                    "reason": "upstream_change",
                })
            )

    # Reset episode status to the changed stage's completed status
    status_map = {
        "correct": EpisodeStatus.CORRECTED,
        "translate": EpisodeStatus.TRANSLATED,
        "adapt": EpisodeStatus.ADAPTED,
        # ...
    }
    episode = session.query(Episode).filter_by(episode_id=episode_id).one()
    episode.status = status_map.get(from_stage, episode.status)
    session.commit()


def _get_stage_outputs(episode_id: str, stage: str) -> list[Path]:
    """Return list of output file paths for a given stage."""
    outputs_map = {
        "translate": [Path(f"data/transcripts/{episode_id}/transcript.tr.txt")],
        "adapt": [Path(f"data/outputs/{episode_id}/script.adapted.tr.md")],
        # ... add more as stages are implemented
    }
    return outputs_map.get(stage, [])
```

**Option B: Call `invalidate_downstream()` in `corrector.py`**

At the end of `correct_transcript()`, after successful correction:

```python
# In corrector.py, after successful correction
if not result.skipped:
    invalidate_downstream(session, episode_id, "correct")
```

**[ASSUMPTION]** We'll implement Option A (extend pipeline.py) since invalidation is a pipeline-wide concern, not corrector-specific.

### 8.3 Detection in Translator

The translator's `_check_idempotency()` function already checks for `.stale` markers:

```python
stale_marker = Path(str(translated_path) + ".stale")
if stale_marker.exists():
    logger.info(f"Translation marked stale (reason: {stale_marker.read_text()})")
    stale_marker.unlink()  # Remove marker
    return None  # Trigger re-translation
```

---

## 9. Provenance and Idempotency

### 9.1 Provenance JSON Format

**Path**: `data/outputs/{episode_id}/provenance/translate_provenance.json`

**Schema**:
```json
{
  "stage": "translate",
  "episode_id": "abc123",
  "timestamp": "2026-02-24T10:30:00Z",
  "prompt_name": "translate",
  "prompt_version": 1,
  "prompt_hash": "sha256:def456...",
  "model": "claude-sonnet-4-20250514",
  "model_params": {
    "temperature": 0.2,
    "max_tokens": 8192
  },
  "input_files": ["data/transcripts/abc123/transcript.corrected.de.txt"],
  "input_content_hash": "sha256:abc123...",
  "output_files": ["data/transcripts/abc123/transcript.tr.txt"],
  "input_tokens": 4500,
  "output_tokens": 5200,
  "cost_usd": 0.0234,
  "duration_seconds": 12.5,
  "segments_processed": 1
}
```

**Fields**:
- `stage`: Always "translate"
- `episode_id`: Episode identifier
- `timestamp`: ISO 8601 datetime (UTC)
- `prompt_name`: Always "translate"
- `prompt_version`: Integer version from PromptVersion table
- `prompt_hash`: SHA-256 of prompt template body (after stripping YAML frontmatter)
- `model`: Model identifier used
- `model_params`: Temperature, max_tokens
- `input_files`: List of input file paths (corrected transcript)
- `input_content_hash`: SHA-256 of corrected transcript content
- `output_files`: List of output file paths (Turkish transcript)
- `input_tokens`, `output_tokens`: Token counts from Claude API
- `cost_usd`: Calculated cost
- `duration_seconds`: Elapsed time (optional)
- `segments_processed`: Number of segments translated (>1 if segmented)

### 9.2 Idempotency Check Logic

**Conditions for skipping re-translation**:
1. Output file `transcript.tr.txt` exists
2. No `.stale` marker present
3. Provenance file exists and is valid JSON
4. `input_content_hash` in provenance matches current input file hash
5. `prompt_hash` in provenance matches current prompt hash

**If ANY condition fails** → re-translate

**Implementation**: See `_check_idempotency()` in §4.4

### 9.3 Force Flag Behavior

`--force` bypasses all idempotency checks:
- Ignores existing output
- Ignores provenance file
- Ignores stale markers
- Always calls Claude API

Use cases:
- Testing prompt changes
- Recovering from partial failure
- Manual override when quality is poor

---

## 10. Test Plan

### 10.1 Unit Tests

**File**: `tests/test_translator.py`

```python
import pytest
from btcedu.core.translator import _segment_text, translate_transcript, TranslationResult
from btcedu.models.episode import Episode, EpisodeStatus

def test_segment_text_basic():
    """Test paragraph-based segmentation."""
    text = "Para 1.\n\nPara 2.\n\nPara 3."
    segments = _segment_text(text, max_chars=20)
    assert len(segments) > 1
    assert all(len(s) <= 20 or "\n\n" in s for s in segments)

def test_segment_text_long_paragraph():
    """Test sentence-based fallback for long paragraphs."""
    text = "A" * 20000  # Single paragraph exceeding limit
    segments = _segment_text(text, max_chars=15000)
    assert len(segments) > 1
    assert all(len(s) <= 15000 for s in segments)

def test_segment_text_no_split_needed():
    """Test single segment for short text."""
    text = "Short text."
    segments = _segment_text(text, max_chars=15000)
    assert len(segments) == 1
    assert segments[0] == text

def test_segment_text_empty_input():
    """Test empty input handling."""
    segments = _segment_text("", max_chars=15000)
    assert len(segments) == 1
    assert segments[0] == ""
```

### 10.2 Integration Tests

```python
def test_translate_transcript_idempotent(tmp_path, db_session, settings):
    """Test that running translate twice skips on second run."""
    # Setup: create episode, corrected transcript
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    # First run
    result1 = translate_transcript(db_session, "test123", settings, force=False)
    assert not result1.skipped
    assert result1.cost_usd > 0

    # Second run (idempotent)
    result2 = translate_transcript(db_session, "test123", settings, force=False)
    assert result2.skipped
    assert result2.cost_usd == result1.cost_usd  # No new API call

def test_translate_transcript_force(tmp_path, db_session, settings):
    """Test that --force re-translates."""
    # Setup: same as above
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    # First run
    result1 = translate_transcript(db_session, "test123", settings, force=False)

    # Force re-run
    result2 = translate_transcript(db_session, "test123", settings, force=True)
    assert not result2.skipped
    # New API call made (cost may differ due to non-determinism)

def test_translate_transcript_dry_run(tmp_path, db_session, settings):
    """Test dry-run writes JSON instead of calling API."""
    settings.dry_run = True
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    result = translate_transcript(db_session, "test123", settings, force=False)

    # Check dry-run file created
    dry_run_file = tmp_path / "dry_run_translate_seg0.json"
    assert dry_run_file.exists()
    assert result.cost_usd == 0.0  # No actual API call

def test_translate_transcript_creates_provenance(tmp_path, db_session, settings):
    """Test provenance JSON is created with correct fields."""
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    result = translate_transcript(db_session, "test123", settings, force=False)

    provenance_path = Path(result.provenance_path)
    assert provenance_path.exists()

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["stage"] == "translate"
    assert provenance["episode_id"] == "test123"
    assert "prompt_hash" in provenance
    assert "input_content_hash" in provenance
    assert provenance["input_tokens"] > 0
    assert provenance["output_tokens"] > 0

def test_translate_transcript_updates_episode_status(tmp_path, db_session, settings):
    """Test episode status transitions to TRANSLATED."""
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    result = translate_transcript(db_session, "test123", settings, force=False)

    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.TRANSLATED
    assert episode.error_message is None

def test_cascade_invalidation(tmp_path, db_session, settings):
    """Test .stale marker invalidates translation."""
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text v1.", encoding="utf-8")

    # First translation
    result1 = translate_transcript(db_session, "test123", settings, force=False)
    translated_path = Path(result1.translated_path)

    # Simulate correction re-run creating stale marker
    stale_marker = Path(str(translated_path) + ".stale")
    stale_marker.write_text(json.dumps({"invalidated_by": "correct"}))

    # Update corrected text
    corrected_path.write_text("German text v2.", encoding="utf-8")

    # Second translation (should NOT skip due to stale marker)
    result2 = translate_transcript(db_session, "test123", settings, force=False)
    assert not result2.skipped
    assert not stale_marker.exists()  # Marker removed after re-translation
```

### 10.3 CLI Tests

```python
def test_translate_cli_help(cli_runner):
    """Test CLI help message."""
    result = cli_runner.invoke(cli, ["translate", "--help"])
    assert result.exit_code == 0
    assert "Translate corrected German transcripts to Turkish" in result.output

def test_translate_cli_success(cli_runner, db_session, tmp_path):
    """Test successful translation via CLI."""
    # Setup
    episode = Episode(episode_id="test123", status=EpisodeStatus.CORRECTED)
    db_session.add(episode)
    db_session.commit()

    corrected_path = tmp_path / "transcript.corrected.de.txt"
    corrected_path.write_text("German text here.", encoding="utf-8")

    # Run CLI
    result = cli_runner.invoke(cli, ["translate", "--episode-id", "test123"])
    assert result.exit_code == 0
    assert "[OK] test123" in result.output
    assert "chars" in result.output
    assert "$" in result.output  # Cost displayed

def test_translate_cli_invalid_status(cli_runner, db_session):
    """Test CLI fails gracefully for wrong episode status."""
    episode = Episode(episode_id="test123", status=EpisodeStatus.TRANSCRIBED)
    db_session.add(episode)
    db_session.commit()

    result = cli_runner.invoke(cli, ["translate", "--episode-id", "test123"])
    assert result.exit_code == 0  # Don't crash
    assert "[FAIL]" in result.output
    assert "not CORRECTED" in result.output
```

---

## 11. Implementation Order

### Phase 1: Core Module (Sessions 1-2)

1. **Create `btcedu/core/translator.py`**
   - Empty file with imports
   - `TranslationResult` dataclass
   - `_segment_text()` function with unit tests
   - Verify tests pass

2. **Implement `_check_idempotency()`**
   - Hash computation
   - Provenance loading
   - Stale marker detection
   - Return early result or None

3. **Implement `_write_provenance()`**
   - JSON serialization
   - All required fields
   - UTF-8 encoding

4. **Implement `translate_transcript()` core logic**
   - Episode validation
   - Path setup
   - Idempotency check
   - Prompt loading via PromptRegistry
   - Segmentation
   - Claude API call(s)
   - Output writing
   - Provenance recording
   - Episode status update
   - Error handling

### Phase 2: Prompt Template (Session 2)

5. **Create `btcedu/prompts/templates/translate.md`**
   - YAML frontmatter
   - System instructions
   - Constraints
   - Input variables
   - Output format

6. **Test prompt loading**
   - Verify PromptRegistry can load template
   - Verify hash computation works
   - Verify prompt_version record created

### Phase 3: CLI Integration (Session 3)

7. **Add `translate` command to `btcedu/cli.py`**
   - Command decorator
   - Options (episode-id, force, dry-run)
   - Import translator module
   - Call `translate_transcript()`
   - Output formatting
   - Error handling

8. **Test CLI manually**
   - `btcedu translate --help`
   - `btcedu translate --episode-id <id>`
   - `btcedu translate --episode-id <id> --force`
   - `btcedu translate --episode-id <id> --dry-run`

### Phase 4: Pipeline Integration (Session 3)

9. **Modify `btcedu/core/pipeline.py`**
   - Add to `_V2_STAGES`
   - Add to `_run_stage()`
   - Update `STAGE_DEPENDENCIES` (if exists)

10. **Test pipeline integration**
    - Run `btcedu run-latest` on a CORRECTED episode
    - Verify translate stage executes
    - Verify episode status transitions to TRANSLATED
    - Verify pipeline continues to next stage (if ADAPT exists)

### Phase 5: Cascade Invalidation (Session 4)

11. **Extend `invalidate_downstream()` in `pipeline.py`**
    - Add "translate" to stage order
    - Implement `_get_stage_outputs("translate")`
    - Test stale marker creation

12. **Verify invalidation flow**
    - Translate episode
    - Re-correct episode (or manually create stale marker)
    - Re-translate → should not skip

### Phase 6: Testing (Session 4-5)

13. **Write unit tests** (`tests/test_translator.py`)
    - Segmentation tests
    - Idempotency tests
    - Provenance tests

14. **Write integration tests**
    - Full translation pipeline with DB
    - Status transitions
    - Cost tracking

15. **Write CLI tests**
    - Help message
    - Success cases
    - Error cases

16. **Run full test suite**
    - `pytest tests/test_translator.py -v`
    - Fix any failures

### Phase 7: Manual Verification (Session 5)

17. **End-to-end test with real episode**
    - Create test episode with German transcript
    - Run correction stage
    - Approve review
    - Run translation stage
    - Verify output quality
    - Check costs in dashboard

18. **Verify backward compatibility**
    - Run `btcedu status` → should show v1 and v2 episodes
    - Run existing v1 pipeline → should not be affected
    - Check that no existing tests break

---

## 12. Definition of Done

**Sprint 4 is complete when**:

- [ ] `btcedu/core/translator.py` exists with full implementation
- [ ] `btcedu/prompts/templates/translate.md` exists with complete prompt
- [ ] `tests/test_translator.py` exists with all unit/integration tests passing
- [ ] `btcedu translate --help` works and shows correct options
- [ ] `btcedu translate --episode-id <id>` successfully translates a corrected transcript
- [ ] Output file `data/transcripts/{ep_id}/transcript.tr.txt` created with valid Turkish text
- [ ] Provenance file `data/outputs/{ep_id}/provenance/translate_provenance.json` created with all required fields
- [ ] ContentArtifact record created in database
- [ ] PipelineRun record created with SUCCESS status, tokens, and cost
- [ ] Episode status transitions from CORRECTED to TRANSLATED
- [ ] Idempotency works: running translate twice skips on second run
- [ ] Force flag works: `--force` re-translates despite existing output
- [ ] Dry-run flag works: `--dry-run` writes JSON instead of calling API
- [ ] Pipeline integration works: `btcedu run-latest` executes translate stage for v2 episodes
- [ ] Cascade invalidation works: stale marker triggers re-translation
- [ ] Existing v1 pipeline unaffected: v1 episodes still work
- [ ] All existing tests still pass (no regressions)
- [ ] Code follows existing patterns (matches corrector.py structure)
- [ ] UTF-8 encoding handled correctly for German→Turkish
- [ ] No review gate created (translation goes directly to next stage)
- [ ] Documentation: docstrings for all public functions
- [ ] Manual verification: real German transcript translates to readable Turkish

---

## 13. Non-Goals (Explicitly Out of Scope)

**Not included in Sprint 4**:

1. **Dashboard UI changes** — translation viewing in web interface deferred to later dashboard enhancement sprint
2. **Review gate after translation** — per MASTERPLAN, review happens after adaptation (Sprint 5), not after translation
3. **Cultural adaptation** — translation is faithful; adaptation is a separate stage (Sprint 5)
4. **Translation quality metrics** — no automated quality scoring or evaluation framework
5. **Translation memory** — no caching of previously translated phrases or terms
6. **Glossary management** — no term database or terminology consistency enforcement (beyond prompt instructions)
7. **Multi-language support** — only German→Turkish, no other language pairs
8. **Parallel translation** — segments translated sequentially, not in parallel
9. **Translation diff view** — no before/after comparison (unlike correction stage which has diff JSON)
10. **Cost optimization** — no prompt compression, caching, or model switching based on segment complexity
11. **Speaker diarization preservation** — speaker labels translated literally, not intelligently preserved
12. **Timestamp alignment** — no time-code tracking (transcripts are text-only)
13. **API rate limiting** — no exponential backoff or retry logic (assumes Claude API is reliable)
14. **Translation validation** — no post-hoc checks for Turkish grammar, completeness, or coherence
15. **Reviewer feedback UI** — feedback injection exists in code, but no dashboard interface to provide feedback
16. **Internationalization** — CLI messages and logs remain in English, not localized
17. **Performance benchmarking** — no metrics on translation speed, throughput, or resource usage
18. **A/B testing of prompts** — prompt versioning exists, but no automated comparison framework
19. **Historical data migration** — existing v1 episodes not migrated to v2 pipeline

---

## 14. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Translation quality poor** | Medium | High | Start with conservative prompt; iterate based on first 3-5 manual reviews; prompt versioning allows easy rollback |
| **Segmentation breaks context** | Low | Medium | Test with long transcripts; adjust segment size if needed; paragraph-level splitting preserves semantic boundaries |
| **Technical terms mistranslated** | Medium | High | Explicit prompt constraint to preserve terms with Turkish in parentheses; manual spot-check first 5 translations |
| **API cost higher than expected** | Low | Medium | Track costs in dashboard; set `max_episode_cost_usd` limit (already in MASTERPLAN); dry-run mode for testing |
| **Pipeline integration breaks v1** | Very Low | Critical | Thorough testing of v1 episodes before merge; v2 stages isolated by `pipeline_version` check |
| **UTF-8 encoding issues** | Low | Medium | Explicit `encoding="utf-8"` in all file I/O; test with Turkish characters (ğ, ı, ş, etc.) |
| **Stale marker logic fails** | Low | Medium | Comprehensive cascade invalidation tests; manual verification with correction re-run |
| **Long translation time** | Low | Low | Monitor duration in provenance; segment size already tuned for reasonable API latency |

---

## 15. Success Metrics

**How to measure Sprint 4 success**:

1. **Functional Completeness**:
   - All checklist items in Definition of Done completed
   - Zero critical bugs in translation pipeline
   - All tests pass (unit + integration + CLI)

2. **Translation Quality** (manual evaluation):
   - First 5 translated episodes reviewed by Turkish speaker
   - Technical accuracy: 100% (all Bitcoin terms preserved correctly)
   - Readability: 4/5 or better (subjective Turkish fluency)
   - Faithfulness: No added/removed information

3. **Performance**:
   - Translation time: <2 min per 15-min episode (10K-20K chars)
   - Cost: $0.02-0.05 per episode (within budget)
   - Idempotency: 100% (second run always skips if inputs unchanged)

4. **Integration**:
   - v2 pipeline runs end-to-end from DETECT to TRANSLATED without manual intervention
   - v1 pipeline unaffected (existing episodes still work)
   - Dashboard shows correct episode status transitions

5. **Developer Experience**:
   - Code review: follows existing patterns, clear naming, adequate comments
   - Tests: 80%+ code coverage for translator.py
   - Documentation: all public functions have docstrings

---

## 16. Follow-Up Work (Out of Scope, but Noted for Future)

**Deferred to later sprints**:

1. **Dashboard Translation Viewer** — show German vs Turkish side-by-side
2. **Translation Review Gate** — if quality issues emerge, add optional review gate
3. **Translation Glossary** — maintain term database for consistency across episodes
4. **Translation Caching** — cache common phrases to reduce API costs
5. **Multi-Model Translation** — test GPT-4 vs Claude for Turkish quality comparison
6. **Automated Quality Checks** — detect untranslated German words in Turkish output
7. **Translation Metrics Dashboard** — track accuracy, cost, and duration over time

---

## 17. Assumptions

**Key assumptions made during planning**:

1. **[ASSUMPTION]** Review Gate 1 approval implicitly verified by episode status == CORRECTED (no explicit approval check needed)
2. **[ASSUMPTION]** Max segment size: 15,000 characters (matches corrector, sufficient for quality)
3. **[ASSUMPTION]** No overlap between segments (paragraph boundaries provide sufficient context)
4. **[ASSUMPTION]** Turkish technical term format: "Turkish (Original)" on first use, then just Turkish
5. **[ASSUMPTION]** Translation is faithful enough that no review gate is needed (per MASTERPLAN §5B)
6. **[ASSUMPTION]** Corrected transcript is always UTF-8 (guaranteed by corrector stage)
7. **[ASSUMPTION]** Segmentation fallback (sentence splitting) is rare (most transcripts have paragraph breaks)
8. **[ASSUMPTION]** Claude Sonnet 4 is sufficient for German→Turkish translation (no need for Opus)
9. **[ASSUMPTION]** Translation cost is linear with input length (~$0.015 per 10K chars)
10. **[ASSUMPTION]** No speaker diarization metadata to preserve (transcripts are plain text)
11. **[ASSUMPTION]** Translation errors are caught by human review in ADAPT stage (Sprint 5)
12. **[ASSUMPTION]** API rate limits not an issue for daily pipeline (1-2 episodes/day)

**If any assumption proves false, plan will need revision.**

---

## Appendix A: File Paths Reference

```
btcedu/
├── core/
│   ├── translator.py                          [CREATE]
│   ├── pipeline.py                            [MODIFY]
│   └── prompt_registry.py                     [USE]
├── prompts/
│   └── templates/
│       └── translate.md                       [CREATE]
├── cli.py                                      [MODIFY]
└── models/
    └── episode.py                              [VERIFY]

tests/
└── test_translator.py                          [CREATE]

data/
├── transcripts/
│   └── {episode_id}/
│       ├── transcript.de.txt                   [EXISTING]
│       ├── transcript.corrected.de.txt         [INPUT]
│       └── transcript.tr.txt                   [OUTPUT]
└── outputs/
    └── {episode_id}/
        └── provenance/
            └── translate_provenance.json       [OUTPUT]
```

---

## Appendix B: Command Reference

```bash
# Development workflow
btcedu translate --help                         # Check CLI
btcedu translate --episode-id abc123           # Translate one
btcedu translate --episode-id abc123 --force   # Force re-translate
btcedu translate --episode-id abc123 --dry-run # Test without API call

# Pipeline workflow
btcedu status                                   # Check episode statuses
btcedu run-latest                               # Run full v2 pipeline
btcedu run-latest --force                       # Force re-run all stages

# Testing
pytest tests/test_translator.py -v              # Run translator tests
pytest tests/ -v                                # Run all tests
pytest tests/test_translator.py::test_segment_text_basic -v  # Run specific test

# Manual verification
cat data/transcripts/abc123/transcript.corrected.de.txt
cat data/transcripts/abc123/transcript.tr.txt
cat data/outputs/abc123/provenance/translate_provenance.json

# Database inspection
sqlite3 btcedu.db "SELECT episode_id, status FROM episodes WHERE pipeline_version=2"
sqlite3 btcedu.db "SELECT * FROM pipeline_runs WHERE stage='translate'"
```

---

**End of Sprint 4 Implementation Plan**

This plan is implementation-ready. All design decisions are documented. All file changes are specified. All tests are outlined. All edge cases are considered. The plan can be handed to Sonnet for implementation execution.
