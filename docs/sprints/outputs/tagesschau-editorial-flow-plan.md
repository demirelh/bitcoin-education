# Tagesschau Editorial Transformation & Review Model — Phase 3 Plan

**Date:** 2026-03-16
**Status:** Planned
**Depends on:** Phase 1 (multi-profile foundation), Phase 2 (tagesschau ingestion/segmentation)
**Scope:** News-specific editorial constraints, translation review gate, per-story review, editorial policy

---

## Problem Statement

Phase 2 built the tagesschau ingestion and segmentation pipeline. However, the news profile currently has a critical review gap: because `adapt` is skipped, the Turkish translation goes **directly** to chapterization with no human review of translation quality. For a Bitcoin podcast, this is caught by review_gate_2 (adaptation review). For news content — where factual accuracy is paramount — the translated text must be human-verified before it becomes video narration.

Additionally, the existing review UI shows generic labels. News reviewers need profile-specific checklists, bilingual diff views, and per-story granular review.

---

## Assumptions (labeled)

1. **[SCOPE]** Translation review is the highest-priority gap. Segmentation review is designed but deferred (auto-approved for Phase 3).
2. **[STYLE]** The translation stage produces faithful text, not condensed presenter copy. Tagesschau anchors already speak concisely; we translate their words. No new condensation stage.
3. **[REVIEW-UI]** The web dashboard is a JavaScript SPA (vanilla JS). Changes to review UI are data-driven (profile metadata in API responses), not new HTML pages.
4. **[DIFF]** Translation diffs show bilingual pairs (German source ↔ Turkish translation) per story, not word-level substitution diffs. This is a different review modality than correction/adaptation diffs.
5. **[SIDECAR]** A reviewed translation sidecar (`stories_translated.reviewed.json`) follows the same pattern as `transcript.reviewed.de.txt` — consumed by chapterizer if present.

---

## 1. Translation Review Gate

### 1.1 Pipeline Insertion

For profiles where `adapt` is skipped, insert a **translation review gate** after TRANSLATED. The pipeline already has `review_gate_2` but it guards the `adapt` stage. For news profiles, we repurpose the gate position — but with stage="translate" instead of stage="adapt".

Updated tagesschau pipeline flow:
```
CORRECTED → [review_gate_1] → SEGMENTED → TRANSLATED →
[review_gate_translate] → CHAPTERIZED → ... → [review_gate_3] → PUBLISHED
```

### 1.2 Stage Configuration

In `_get_stages()`, when `adapt.skip=True`:
- Currently: removes `adapt` and `review_gate_2`, chapterize requires `TRANSLATED`
- Phase 3: replaces `review_gate_2` with `review_gate_translate` instead of removing it

```python
# When adapt.skip=True in profile:
# Replace review_gate_2 with review_gate_translate
stages = [
    ("review_gate_translate", EpisodeStatus.TRANSLATED)
    if n == "review_gate_2" else (n, s)
    for n, s in stages
]
# Then remove adapt (but keep the gate)
stages = [(n, s) for n, s in stages if n != "adapt"]
# chapterize requires TRANSLATED (no ADAPTED status)
stages = [
    ("chapterize", EpisodeStatus.TRANSLATED) if n == "chapterize" else (n, s)
    for n, s in stages
]
```

This means:
- Bitcoin podcast: `... → adapt → review_gate_2 → chapterize(ADAPTED) → ...` (unchanged)
- Tagesschau: `... → review_gate_translate → chapterize(TRANSLATED) → ...` (new)

### 1.3 Gate Implementation in `_run_stage()`

New case in `_run_stage()`:

```python
elif stage_name == "review_gate_translate":
    # Check if already approved
    if has_approved_review(session, episode.episode_id, "translate"):
        return StageResult("review_gate_translate", "success", elapsed,
                          detail="translation review approved")

    # Check if pending review exists
    if has_pending_review(session, episode.episode_id):
        return StageResult("review_gate_translate", "review_pending", elapsed,
                          detail="awaiting translation review")

    # Create review task with bilingual diff
    _create_translation_review_task(session, episode, settings)
    return StageResult("review_gate_translate", "review_pending", elapsed,
                      detail="translation review task created")
```

### 1.4 Translation Diff Generation

A new function `compute_translation_diff()` produces a bilingual diff suitable for per-story review:

```python
def compute_translation_diff(stories_translated_path: str | Path) -> dict:
    """Generate a bilingual story-level diff for translation review.

    Unlike correction diffs (word-level substitutions) or adaptation diffs
    (character-level splices), translation diffs show parallel German/Turkish
    text per story. Each story is one reviewable item.
    """
```

Output structure:

```json
{
  "episode_id": "EP001",
  "diff_type": "translation",
  "source_language": "de",
  "target_language": "tr",
  "stories": [
    {
      "item_id": "trans-s01",
      "story_id": "s01",
      "headline_de": "Bundestag debattiert Haushaltsentwurf",
      "headline_tr": "Bundestag (Almanya Federal Meclisi) bütçe taslağını tartışıyor",
      "text_de": "...(German source)...",
      "text_tr": "...(Turkish translation)...",
      "word_count_de": 120,
      "word_count_tr": 105,
      "category": "politik",
      "story_type": "bericht"
    }
  ],
  "summary": {
    "total_stories": 10,
    "total_words_de": 1500,
    "total_words_tr": 1350,
    "compression_ratio": 0.90
  }
}
```

Written to: `{outputs_dir}/{episode_id}/review/translation_diff.json`

### 1.5 Review Task Creation

```python
def _create_translation_review_task(session, episode, settings):
    """Create a ReviewTask for translation review."""
    stories_path = Path(settings.outputs_dir) / episode.episode_id / "stories_translated.json"
    transcript_path = Path(settings.transcripts_dir) / episode.episode_id / "transcript.tr.txt"

    # Generate bilingual diff
    diff_path = Path(settings.outputs_dir) / episode.episode_id / "review" / "translation_diff.json"
    diff_data = compute_translation_diff(stories_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2))

    create_review_task(
        session,
        episode.episode_id,
        stage="translate",
        artifact_paths=[str(stories_path), str(transcript_path)],
        diff_path=str(diff_path),
    )
```

---

## 2. Per-Story Granular Review

### 2.1 Item Decision Model

The existing `ReviewItemDecision` model works for per-story review with no schema changes:

| Field | Translation Review Usage |
|-------|------------------------|
| `item_id` | `"trans-s01"`, `"trans-s02"`, ... |
| `operation_type` | Story category: `"politik"`, `"wirtschaft"`, etc. |
| `original_text` | German source (`text_de`) |
| `proposed_text` | Turkish translation (`text_tr`) |
| `action` | ACCEPTED / REJECTED / EDITED / PENDING |
| `edited_text` | Reviewer's corrected Turkish text |

### 2.2 Assembly Algorithm

New case in `apply_item_decisions()` for stage="translate":

```python
def _assemble_translation_review(stories_data, item_decisions) -> dict:
    """Reconstruct stories_translated.json from per-story review decisions.

    - ACCEPTED / PENDING: keep translated text (text_tr, headline_tr)
    - REJECTED: revert to literal re-translation marker (flags for re-work)
    - EDITED: use reviewer's custom text
    """
```

**Assembly logic:**
1. Load `stories_translated.json` as dict
2. For each story in `stories[].item_id`:
   - ACCEPTED/PENDING → keep `text_tr` and `headline_tr` as-is
   - EDITED → replace `text_tr` with `decision.edited_text`
   - REJECTED → set `text_tr` to `"[ÇEVİRİ REDDEDİLDİ — yeniden çeviri gerekli]"` (flags for attention)
3. Write sidecar: `{outputs_dir}/{episode_id}/review/stories_translated.reviewed.json`
4. Return path to sidecar

### 2.3 Sidecar Consumption

**Chapterizer** already checks for reviewed sidecar:
```python
reviewed_path = Path(settings.outputs_dir) / episode_id / "review" / "script.adapted.reviewed.tr.md"
```

Add parallel check for translation review sidecar:
```python
# In story mode, check for reviewed stories sidecar
stories_reviewed_path = (
    Path(settings.outputs_dir) / episode_id / "review" / "stories_translated.reviewed.json"
)
if stories_reviewed_path.exists():
    stories_path = stories_reviewed_path
    logger.info("Using reviewed translation sidecar for episode %s", episode_id)
```

---

## 3. Prompt Strategy

### 3.1 Existing Prompts (Phase 2, already implemented)

All prompts already exist under `btcedu/prompts/templates/tagesschau_tr/`. Phase 3 makes targeted refinements:

| Prompt | Phase 2 Status | Phase 3 Changes |
|--------|---------------|-----------------|
| `system.md` | Complete | Add explicit review context section |
| `correct_transcript.md` | Complete | No changes |
| `translate.md` | Complete | Add reviewer feedback for re-translation |
| `chapterize.md` | Complete | No changes |
| `segment_broadcast.md` | Complete | No changes |

### 3.2 Prompt Refinements

**`tagesschau_tr/translate.md`** — Add re-translation guidance:

After `{{ reviewer_feedback }}` section, add:

```markdown
## BEI NACHARBEIT (Wenn Reviewer-Feedback vorliegt)

Wenn oben Reviewer-Feedback aufgeführt ist:
1. Konzentriere dich auf die genannten Probleme
2. Korrigiere NUR die beanstandeten Passagen
3. Ändere NICHT Passagen, die nicht im Feedback erwähnt werden
4. Beachte insbesondere: Eigennamen, Institutionszuordnungen, Neutralitätsverstöße
```

### 3.3 Anti-Hallucination Enforcement

Already present in all tagesschau prompts. Phase 3 adds a **validation check** at the pipeline level:

**Post-translation sanity check** (in translator.py, after Claude call):
- Compare `word_count_tr` to `word_count_de` per story
- Flag if ratio is <0.5 or >1.5 (±50% — suggests summarization or hallucination)
- Log warning; do NOT block (human reviewer catches it in review gate)

This is a **soft guard** — it doesn't reject output, but logs anomalies for reviewer attention. Added to `SegmentationResult` / `TranslationResult` as `warnings: list[str]`.

---

## 4. Review Model

### 4.1 Review Gate Labels (Profile-Aware)

Currently `_REVIEW_GATE_LABELS` is a static dict in `api.py`:

```python
_REVIEW_GATE_LABELS = {
    "correct": ("review_gate_1", "Transcript Correction Review"),
    "adapt": ("review_gate_2", "Adaptation Review"),
    "stock_images": ("review_gate_stock", "Stock Image Review"),
    "render": ("review_gate_3", "Video Review"),
}
```

Phase 3 adds profile-aware label override and a new entry:

```python
_REVIEW_GATE_LABELS = {
    "correct": ("review_gate_1", "Transcript Correction Review"),
    "translate": ("review_gate_translate", "Translation Review"),  # NEW
    "adapt": ("review_gate_2", "Adaptation Review"),
    "stock_images": ("review_gate_stock", "Stock Image Review"),
    "render": ("review_gate_3", "Video Review"),
}
```

### 4.2 News Review Checklist

When the review detail API returns data for a news episode (profile=tagesschau_tr), include a **review checklist** in the response:

```json
{
  "review_checklist": [
    {"id": "factual_accuracy", "label": "Factual accuracy verified", "checked": false},
    {"id": "political_neutrality", "label": "No editorialization or political spin", "checked": false},
    {"id": "attribution_present", "label": "Source attribution included", "checked": false},
    {"id": "proper_nouns_correct", "label": "Names, places, institutions correct", "checked": false},
    {"id": "no_hallucination", "label": "No invented facts or figures", "checked": false},
    {"id": "register_correct", "label": "Formal news register (not conversational)", "checked": false}
  ]
}
```

**Implementation:** The checklist is not stored in DB (it's a hint, not enforced). It's returned based on `episode.content_profile`:
- `bitcoin_podcast` → no checklist (existing review flow)
- `tagesschau_tr` → news checklist (above)

Future: checklist state could be stored in `reviewer_notes` JSON or a new field.

### 4.3 Review Detail Enrichment for Translation Reviews

When `get_review_detail()` returns data for stage="translate":

```python
# Translation-specific review detail
if task.stage == "translate" and diff_data and diff_data.get("diff_type") == "translation":
    detail["review_mode"] = "bilingual"
    detail["stories"] = diff_data["stories"]  # bilingual pairs for UI
    detail["compression_ratio"] = diff_data["summary"]["compression_ratio"]
    detail["warnings"] = diff_data.get("warnings", [])
```

This gives the frontend enough data to render a bilingual side-by-side view.

### 4.4 Approval Criteria

**Translation review** (`stage="translate"`):
- Reviewer sees bilingual story pairs (DE ↔ TR)
- Can accept/reject/edit each story individually
- Can approve the whole review task once satisfied
- **Rejection reverts to SEGMENTED** (episode re-enters SEGMENTED state, translation re-runs with feedback)
- **Changes requested** → feedback injected via `{{ reviewer_feedback }}` on re-translation

**Reversion map update** in `_revert_episode()`:

```python
_REVERT_MAP = {
    EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,   # RG1 (existing)
    EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,      # RG2 (existing)
    EpisodeStatus.TRANSLATED: EpisodeStatus.SEGMENTED,    # RG_translate (NEW)
}
```

### 4.5 Auto-Approval for Translation

Unlike corrections (which have `_is_minor_correction`), translations have no auto-approval. Every news translation must be human-reviewed. This is intentional — factual accuracy in news requires human judgment.

**[ASSUMPTION]** No auto-approval for news translation reviews. This is a design choice for news content safety, not a limitation.

---

## 5. Sidecar & Reviewed-Output Flow

### 5.1 File Layout After Translation Review

```
data/outputs/{episode_id}/
├── stories.json                              # German stories (from segment)
├── stories_translated.json                   # Turkish stories (from translate)
├── review/
│   ├── correction_diff.json                  # Correction diff (existing)
│   ├── translation_diff.json                 # NEW: bilingual story pairs
│   ├── stories_translated.reviewed.json      # NEW: reviewed translation sidecar
│   ├── transcript.reviewed.de.txt            # Correction sidecar (existing)
│   └── review_history.json                   # Audit trail (existing)
└── chapters.json                             # Chapters (from chapterize)
```

### 5.2 Sidecar Priority in Chapterizer

The chapterizer's input resolution order (story mode):

1. `review/stories_translated.reviewed.json` (if exists — reviewed translation)
2. `stories_translated.json` (default — unreviewed translation)
3. Fall through to adapted script path (non-story mode)

### 5.3 Cascade Invalidation

When translation review requests changes:
- `stories_translated.json` → `.stale` marker
- Downstream: chapterize, imagegen, tts, render all invalidated
- Translator re-runs with `{{ reviewer_feedback }}`

When translation review approves:
- No stale markers (output is accepted)
- If per-story edits applied → sidecar written → chapterizer uses sidecar
- Pipeline advances to chapterize

---

## 6. Exact File Changes

### New Files (3)

| # | File | Purpose |
|---|------|---------|
| 1 | `btcedu/core/translation_diff.py` | `compute_translation_diff()` function |
| 2 | `docs/runbooks/news-editorial-policy.md` | Operator review guide for news content |
| 3 | `tests/test_translation_review.py` | Translation review gate + sidecar tests |

### Modified Files (7)

| # | File | Change |
|---|------|--------|
| 1 | `btcedu/core/pipeline.py` | `review_gate_translate` in `_get_stages()` + `_run_stage()` |
| 2 | `btcedu/core/reviewer.py` | `_REVERT_MAP` for TRANSLATED→SEGMENTED, `apply_item_decisions()` for stage="translate", `_assemble_translation_review()` |
| 3 | `btcedu/core/translator.py` | Word-count ratio warning after per-story translation |
| 4 | `btcedu/core/chapterizer.py` | Check for `stories_translated.reviewed.json` sidecar |
| 5 | `btcedu/web/api.py` | `"translate"` in `_REVIEW_GATE_LABELS`, news checklist in review detail, bilingual review data |
| 6 | `btcedu/prompts/templates/tagesschau_tr/translate.md` | Add re-translation guidance section |
| 7 | `tests/test_tagesschau_flow.py` | Add review gate tests for news translation |

---

## 7. Detailed Change Specifications

### 7.1 `btcedu/core/pipeline.py`

**`_get_stages()`** — Replace the `adapt.skip` block:

```python
# Current (Phase 2):
if stage_config.get("adapt", {}).get("skip"):
    stages = [(n, s) for n, s in stages if n not in ("adapt", "review_gate_2")]
    stages = [
        ("chapterize", EpisodeStatus.TRANSLATED) if n == "chapterize" else (n, s)
        for n, s in stages
    ]

# Phase 3:
if stage_config.get("adapt", {}).get("skip"):
    # Replace review_gate_2 with review_gate_translate (don't remove it)
    stages = [
        ("review_gate_translate", EpisodeStatus.TRANSLATED)
        if n == "review_gate_2" else (n, s)
        for n, s in stages
    ]
    # Remove adapt only (keep the renamed gate)
    stages = [(n, s) for n, s in stages if n != "adapt"]
    # Chapterize requires TRANSLATED (gate doesn't change status)
    stages = [
        ("chapterize", EpisodeStatus.TRANSLATED) if n == "chapterize" else (n, s)
        for n, s in stages
    ]
```

**`_run_stage()`** — Add `review_gate_translate` case:

```python
elif stage_name == "review_gate_translate":
    from btcedu.core.reviewer import (
        create_review_task, has_approved_review, has_pending_review,
    )
    from btcedu.core.translation_diff import compute_translation_diff

    if has_approved_review(session, episode.episode_id, "translate"):
        elapsed = time.monotonic() - t0
        return StageResult("review_gate_translate", "success", elapsed,
                          detail="translation review approved")

    if has_pending_review(session, episode.episode_id):
        elapsed = time.monotonic() - t0
        return StageResult("review_gate_translate", "review_pending", elapsed,
                          detail="awaiting translation review")

    # Generate bilingual diff and create review task
    stories_path = (
        Path(settings.outputs_dir) / episode.episode_id / "stories_translated.json"
    )
    diff_path = (
        Path(settings.outputs_dir) / episode.episode_id
        / "review" / "translation_diff.json"
    )
    if stories_path.exists():
        diff_data = compute_translation_diff(stories_path)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(
            json.dumps(diff_data, ensure_ascii=False, indent=2)
        )

    transcript_path = (
        Path(settings.transcripts_dir) / episode.episode_id / "transcript.tr.txt"
    )
    create_review_task(
        session, episode.episode_id, stage="translate",
        artifact_paths=[str(stories_path), str(transcript_path)],
        diff_path=str(diff_path) if diff_path.exists() else None,
    )
    elapsed = time.monotonic() - t0
    return StageResult("review_gate_translate", "review_pending", elapsed,
                      detail="translation review task created")
```

Also add `"review_gate_translate"` to `_V2_ONLY_STAGES` set.

### 7.2 `btcedu/core/reviewer.py`

**`_revert_episode()`** — Add TRANSLATED → SEGMENTED:

```python
def _revert_episode(session, episode_id):
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        return

    revert_map = {
        EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,
        EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,
        EpisodeStatus.TRANSLATED: EpisodeStatus.SEGMENTED,  # NEW
    }

    target = revert_map.get(episode.status)
    if target:
        logger.info("Reverting %s from %s to %s", episode_id, episode.status.value, target.value)
        episode.status = target
        session.commit()
    else:
        logger.warning("No reversion rule for status %s", episode.status.value)
```

**`apply_item_decisions()`** — Add stage="translate" case:

```python
elif task.stage == "translate":
    # Load stories_translated.json
    stories_path = _find_artifact_by_pattern(task, "stories_translated.json")
    stories_data = json.loads(Path(stories_path).read_text(encoding="utf-8"))

    reviewed = _assemble_translation_review(stories_data, diff_data, item_decisions)

    reviewed_path = (
        Path(settings.outputs_dir) / task.episode_id
        / "review" / "stories_translated.reviewed.json"
    )
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    reviewed_path.write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(reviewed_path.resolve())
```

**`_assemble_translation_review()`** — New function:

```python
def _assemble_translation_review(
    stories_data: dict,
    diff_data: dict,
    item_decisions: dict[str, "ReviewItemDecision"],
) -> dict:
    """Reconstruct stories_translated.json from per-story review decisions."""
    result = dict(stories_data)  # shallow copy top level
    result["stories"] = []

    for story in stories_data["stories"]:
        story_copy = dict(story)
        item_id = f"trans-{story['story_id']}"
        decision = item_decisions.get(item_id)

        if decision and decision.action == ReviewItemAction.EDITED.value:
            story_copy["text_tr"] = decision.edited_text
        elif decision and decision.action in (
            ReviewItemAction.REJECTED.value,
            ReviewItemAction.UNCHANGED.value,
        ):
            # Mark for re-translation
            story_copy["text_tr"] = (
                "[ÇEVİRİ REDDEDİLDİ — yeniden çeviri gerekli] "
                + story_copy.get("text_tr", "")
            )
        # ACCEPTED / PENDING: keep text_tr as-is

        result["stories"].append(story_copy)

    return result
```

### 7.3 `btcedu/core/translation_diff.py` — New file

```python
def compute_translation_diff(stories_translated_path: str | Path) -> dict:
    """Generate a bilingual story-level diff for translation review."""
    data = json.loads(Path(stories_translated_path).read_text(encoding="utf-8"))

    stories_diff = []
    total_words_de = 0
    total_words_tr = 0
    warnings = []

    for story in data.get("stories", []):
        words_de = len(story.get("text_de", "").split())
        words_tr = len(story.get("text_tr", "").split())
        total_words_de += words_de
        total_words_tr += words_tr

        # Flag anomalous compression/expansion
        if words_de > 0:
            ratio = words_tr / words_de
            if ratio < 0.5 or ratio > 1.5:
                warnings.append(
                    f"Story {story['story_id']}: word ratio {ratio:.2f} "
                    f"(DE:{words_de} → TR:{words_tr}) — possible "
                    f"{'summarization' if ratio < 0.5 else 'hallucination'}"
                )

        stories_diff.append({
            "item_id": f"trans-{story['story_id']}",
            "story_id": story["story_id"],
            "headline_de": story.get("headline_de", ""),
            "headline_tr": story.get("headline_tr", ""),
            "text_de": story.get("text_de", ""),
            "text_tr": story.get("text_tr", ""),
            "word_count_de": words_de,
            "word_count_tr": words_tr,
            "category": story.get("category", ""),
            "story_type": story.get("story_type", ""),
        })

    compression = total_words_tr / total_words_de if total_words_de > 0 else 1.0

    return {
        "episode_id": data.get("episode_id", ""),
        "diff_type": "translation",
        "source_language": "de",
        "target_language": "tr",
        "stories": stories_diff,
        "summary": {
            "total_stories": len(stories_diff),
            "total_words_de": total_words_de,
            "total_words_tr": total_words_tr,
            "compression_ratio": round(compression, 3),
        },
        "warnings": warnings,
    }
```

### 7.4 `btcedu/core/chapterizer.py`

In story mode input resolution, add reviewed sidecar check:

```python
# Story mode: check for reviewed translation sidecar first
stories_reviewed_path = (
    Path(settings.outputs_dir) / episode_id / "review"
    / "stories_translated.reviewed.json"
)
stories_default_path = (
    Path(settings.outputs_dir) / episode_id / "stories_translated.json"
)

if stories_reviewed_path.exists():
    stories_path = stories_reviewed_path
    logger.info("Using reviewed translation sidecar for episode %s", episode_id)
elif stories_default_path.exists():
    stories_path = stories_default_path
```

### 7.5 `btcedu/web/api.py`

**Review gate labels:**
```python
_REVIEW_GATE_LABELS = {
    "correct": ("review_gate_1", "Transcript Correction Review"),
    "translate": ("review_gate_translate", "Translation Review"),
    "adapt": ("review_gate_2", "Adaptation Review"),
    "stock_images": ("review_gate_stock", "Stock Image Review"),
    "render": ("review_gate_3", "Video Review"),
}
```

**Review detail enrichment** — in `get_review_detail()`:

```python
# Profile-aware review checklist
episode = session.query(Episode).filter(
    Episode.episode_id == task.episode_id
).first()
content_profile = getattr(episode, "content_profile", "bitcoin_podcast")

if content_profile == "tagesschau_tr":
    detail["review_checklist"] = _NEWS_REVIEW_CHECKLIST

if task.stage == "translate" and diff_data and diff_data.get("diff_type") == "translation":
    detail["review_mode"] = "bilingual"
    detail["stories"] = diff_data["stories"]
    detail["compression_ratio"] = diff_data["summary"].get("compression_ratio")
    detail["warnings"] = diff_data.get("warnings", [])
```

```python
_NEWS_REVIEW_CHECKLIST = [
    {"id": "factual_accuracy", "label": "Factual accuracy verified"},
    {"id": "political_neutrality", "label": "No editorialization or political spin"},
    {"id": "attribution_present", "label": "Source attribution included"},
    {"id": "proper_nouns_correct", "label": "Names, places, institutions correct"},
    {"id": "no_hallucination", "label": "No invented facts or figures"},
    {"id": "register_correct", "label": "Formal news register (not conversational)"},
]
```

### 7.6 `btcedu/prompts/templates/tagesschau_tr/translate.md`

Add before `# Input`:

```markdown
## BEI NACHARBEIT (Wenn Reviewer-Feedback vorliegt)

Wenn oben Reviewer-Feedback aufgeführt ist:
1. Konzentriere dich auf die genannten Probleme
2. Korrigiere NUR die beanstandeten Passagen
3. Ändere NICHT Passagen, die nicht im Feedback erwähnt werden
4. Beachte insbesondere: Eigennamen, Institutionszuordnungen, Neutralitätsverstöße
```

### 7.7 `docs/runbooks/news-editorial-policy.md`

Operator guide covering:
- **Purpose**: Define review standards for tagesschau-derived content
- **When to approve**: Translation is factually accurate, politically neutral, formally registered, properly attributed
- **When to request changes**: Minor issues (wrong institution name, informal register, missing parenthetical explanation)
- **When to reject**: Factual errors, editorialization, hallucinated content, missing stories
- **Checklist items**: Matches the 6-item API checklist
- **Escalation**: If source material is disputed/retracted, mark episode as FAILED with notes
- **Per-story review**: How to use accept/reject/edit on individual stories
- **Sidecar workflow**: Edited stories flow into chapterizer automatically

---

## 8. Test Strategy

### 8.1 New Tests (`tests/test_translation_review.py`)

```
test_review_gate_translate_creates_task
    - Episode at TRANSLATED with tagesschau_tr profile
    - Pipeline runs review_gate_translate
    - ReviewTask created with stage="translate"
    - Story-level translation_diff.json written

test_review_gate_translate_approved
    - ReviewTask approved for stage="translate"
    - Pipeline skips gate, advances to chapterize

test_review_gate_translate_changes_requested
    - ReviewTask gets request_changes
    - Episode reverts to SEGMENTED
    - Translator re-runs with feedback

test_translation_diff_structure
    - compute_translation_diff() produces correct bilingual pairs
    - item_ids follow "trans-s01" pattern
    - compression_ratio calculated correctly
    - Warnings generated for anomalous ratios

test_translation_diff_warnings
    - Story with TR text much shorter than DE → warning about summarization
    - Story with TR text much longer than DE → warning about hallucination

test_per_story_item_decisions
    - Reviewer accepts some stories, edits others, rejects one
    - apply_item_decisions() produces correct sidecar
    - Edited stories have reviewer text
    - Rejected stories have marker prefix

test_translation_sidecar_consumed_by_chapterizer
    - stories_translated.reviewed.json exists
    - Chapterizer uses it instead of stories_translated.json

test_translation_review_not_for_bitcoin
    - Episode with bitcoin_podcast profile
    - _get_stages() does NOT include review_gate_translate
    - Still includes review_gate_2 (adaptation review)

test_news_review_checklist_in_api
    - GET /api/reviews/<id> for tagesschau episode
    - Response includes review_checklist with 6 items
    - Bitcoin podcast review does NOT include checklist

test_review_gate_labels_include_translate
    - _REVIEW_GATE_LABELS has "translate" entry
```

### 8.2 Modified Tests (`tests/test_tagesschau_flow.py`)

```
test_get_stages_tagesschau_tr
    - Verify review_gate_translate is present (not review_gate_2)
    - Verify adapt is absent
    - Verify chapterize requires TRANSLATED

test_full_news_pipeline_flow
    - Mock all Claude calls
    - Episode goes: NEW → ... → TRANSLATED → review_gate_translate(pending) → STOP
    - Approve review → pipeline resumes → CHAPTERIZED → ...
```

### 8.3 Regression Tests

```
test_bitcoin_pipeline_unchanged
    - Bitcoin podcast episode through full pipeline
    - review_gate_2 still present (not review_gate_translate)
    - adapt stage still runs
    - All existing behavior preserved
```

---

## 9. Definition of Done

1. **All existing tests pass** (910+ baseline, zero regressions)
2. **New tests pass** (~10 tests in `test_translation_review.py`)
3. **Pipeline flow verified:**
   - Tagesschau episode hits `review_gate_translate` after TRANSLATED
   - Review task created with bilingual diff
   - Approve → pipeline advances to chapterize
   - Request changes → episode reverts to SEGMENTED, feedback injected
   - Per-story edit → sidecar written → chapterizer uses sidecar
4. **Bitcoin pipeline unchanged:** review_gate_2 still works for adaptation review
5. **API verified:**
   - Review detail for translation includes bilingual stories + checklist
   - Review gate label shows "Translation Review"
6. **Editorial policy doc exists:** `docs/runbooks/news-editorial-policy.md`
7. **Prompt updated:** tagesschau_tr translate.md includes re-translation guidance

---

## 10. Non-Goals (Phase 3)

- **No condensation/rewriting stage** — translation stays faithful; tagesschau anchors already speak concisely
- **No segmentation review gate** — segmentation is auto-trusted in Phase 3 (structural, not content)
- **No dashboard UI changes** — frontend will render bilingual review using existing component patterns + new data fields; actual UI work is a separate sprint
- **No auto-approval for news translations** — intentional omission for safety
- **No multi-reviewer workflow** — single reviewer per task, same as Bitcoin
- **No real-time fact-checking** — no external API calls to verify claims
- **No per-headline review** — headlines are reviewed as part of their story's item; no separate headline review items
- **No checklist persistence in DB** — checklist is a UI hint, not enforced workflow state
