# Sprint 5 Implementation Plan: Turkey-Context Adaptation Stage

**Generated**: 2026-02-24
**Sprint**: 5 (Phase 2, Part 2)
**Status**: Implementation-Ready
**Dependencies**: Sprint 1-4 (Foundation + Correction + Review System + Translation) completed

---

## 1. Sprint Scope Summary

**In Scope**:

Sprint 5 implements the **ADAPT** stage for the btcedu v2 pipeline, providing Turkey-context cultural adaptation of Turkish translations with a tiered rule system. This is the most editorially sensitive stage in the pipeline. The adapter will:

- Accept faithful Turkish translations and original German corrected transcripts as input
- Apply tiered adaptation rules (T1: mechanical, low-risk; T2: editorial, requires review)
- Replace German institutions with Turkish equivalents or generic references
- Convert currency references appropriately (Euro → Turkish Lira or USD)
- Adjust tone to Turkish influencer style (conversational, "siz" formal)
- Remove Germany-specific legal/tax advice with markers
- Preserve ALL Bitcoin/crypto technical facts unchanged
- Enforce hard constraints: no invented Turkish regulations, no financial advice, no political commentary
- Tag all adaptations with `[T1]` or `[T2]` markers
- Generate adaptation diff JSON comparing literal translation vs adapted version
- Integrate **Review Gate 2** after adaptation for human approval
- Support reviewer feedback injection for iterative refinement
- Track provenance, implement idempotency, support cascade invalidation
- Provide CLI command (`btcedu adapt`) for manual execution

**Explicitly NOT In Scope**:

- Chapterization, image generation, TTS, rendering, or publishing (future sprints)
- Auto-approve rules for adaptations
- Database lookup tables for German↔Turkish institution mappings (use prompt)
- Modification of existing correction/translation/review systems
- Redesign of review architecture (extend existing)
- Dashboard UI enhancements beyond adaptation diff viewer
- Adaptation quality metrics or evaluation framework
- Multi-language support beyond German→Turkish→Adapted Turkish
- Translation memory or glossary management
- Video review (Review Gate 3 is Sprint 9)

---

## 2. File-Level Plan

### Files to CREATE:

#### 2.1 `btcedu/core/adapter.py`

**Purpose**: Core adaptation logic with tiered rule application
**Key Contents**:

- `adapt_script()` — main entry point
- `AdaptationResult` — result dataclass
- `_is_adaptation_current()` — idempotency check (input hash + prompt hash + stale marker)
- `_split_prompt()` — split template at marker into system + user parts
- `_segment_text()` — paragraph-aware text splitting (reuse translator pattern)
- `compute_adaptation_diff()` — parse `[T1]`/`[T2]` tags, classify changes
- `_write_provenance()` — provenance JSON writer
- Error handling with PipelineRun tracking
- Integration with Review Gate 1 approval check (must be approved before adapting)

**Pattern to follow**: Clone `btcedu/core/translator.py` structure exactly, with these differences:
- Takes TWO inputs: `transcript.tr.txt` (Turkish translation) AND `transcript.corrected.de.txt` (German original for reference)
- Checks Review Gate 1 approval before proceeding
- Computes adaptation diff instead of simple diff
- Creates Review Gate 2 (ReviewTask with stage="adapt") after successful adaptation

#### 2.2 `btcedu/prompts/templates/adapt.md`

**Purpose**: Adaptation prompt template with YAML frontmatter and complete tiered rule system
**Key Contents**:

- YAML metadata (name, model, temperature, max_tokens, description, author)
- System instructions for Turkey-context cultural adaptation
- Complete tiered adaptation rules from MASTERPLAN §5C
- Input variables: `{{ translation }}`, `{{ original_german }}`, `{{ reviewer_feedback }}`
- Output format specification with `[T1]`/`[T2]` tagging requirement
- Hard constraints enforcement (FORBIDDEN actions)
- Editorial neutrality guidelines

#### 2.3 `tests/test_adapter.py`

**Purpose**: Unit and integration tests for adapter module
**Key Contents**:

- Fixtures: `translated_episode()` with translation + corrected transcript files, ReviewTask approved
- `test_adapt_script_basic()` — successful adaptation with T1/T2 tags
- `test_adapt_script_idempotent()` — skip on second run
- `test_adapt_script_force()` — reprocess with --force
- `test_adapt_script_blocks_without_review_approval()` — raises error if Review Gate 1 not approved
- `test_adapt_script_creates_provenance()` — provenance file validation
- `test_adapt_script_updates_episode_status()` — status TRANSLATED → ADAPTED
- `test_adapt_script_creates_review_gate_2()` — ReviewTask with stage="adapt" created
- `test_adaptation_diff_parsing()` — diff correctly identifies T1/T2 changes
- `test_adaptation_diff_categories()` — classification (institution, currency, tone, legal, cultural, regulatory)
- `test_cascade_invalidation()` — .stale marker created when translation changes
- `test_reviewer_feedback_injection()` — request_changes notes injected into re-adaptation

### Files to MODIFY:

#### 2.4 `btcedu/models/episode.py`

**Changes**: None required (EpisodeStatus.ADAPTED already added in Sprint 1)
**Verification**: Confirm `ADAPTED = "adapted"` exists in enum

#### 2.5 `btcedu/core/pipeline.py`

**Changes**:

1. Add `("adapt", EpisodeStatus.TRANSLATED)` to `_V2_STAGES` list (after translate)
2. Add `("review_gate_2", EpisodeStatus.ADAPTED)` to `_V2_STAGES` list (after adapt)
3. Add `elif stage_name == "adapt":` branch to `_run_stage()` function:

```python
elif stage_name == "adapt":
    from btcedu.core.adapter import adapt_script

    result = adapt_script(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0
    return StageResult(
        "adapt",
        "success",
        elapsed,
        detail=f"Adapted for Turkey context ({result.adaptation_count} adaptations, ${result.cost_usd:.4f})",
    )
```

4. Add `elif stage_name == "review_gate_2":` branch (mirror review_gate_1 pattern):

```python
elif stage_name == "review_gate_2":
    from btcedu.core.reviewer import (
        has_approved_review,
        has_pending_review,
        create_review_task,
    )

    # Check if already approved
    if has_approved_review(session, episode.episode_id, "adapt"):
        return StageResult(
            "review_gate_2",
            "success",
            elapsed,
            detail="adaptation review approved",
        )

    # Check if review task already pending
    if has_pending_review(session, episode.episode_id):
        return StageResult(
            "review_gate_2",
            "review_pending",
            elapsed,
            detail="awaiting adaptation review",
        )

    # Create new review task
    adapted_path = Path(settings.outputs_dir) / episode.episode_id / "script.adapted.tr.md"
    diff_path = Path(settings.outputs_dir) / episode.episode_id / "review" / "adaptation_diff.json"

    create_review_task(
        session,
        episode.episode_id,
        stage="adapt",
        artifact_paths=[str(adapted_path)],
        diff_path=str(diff_path) if diff_path.exists() else None,
    )
    session.commit()

    return StageResult(
        "review_gate_2",
        "review_pending",
        elapsed,
        detail="adaptation review task created",
    )
```

5. Update `STAGE_DEPENDENCIES` dict (if it exists) to include `"adapt": ["translate"]`
6. Update `_STATUS_ORDER` to ensure `EpisodeStatus.ADAPTED: 12` is present

#### 2.6 `btcedu/cli.py`

**Changes**: Add new `adapt` command

```python
@cli.command()
@click.option(
    "--episode-id",
    "episode_ids",
    multiple=True,
    required=True,
    help="Episode ID(s) to adapt (repeatable).",
)
@click.option("--force", is_flag=True, default=False, help="Re-adapt even if output exists.")
@click.option("--dry-run", is_flag=True, default=False, help="Write request JSON instead of calling API.")
@click.pass_context
def adapt(ctx: click.Context, episode_ids: tuple[str, ...], force: bool, dry_run: bool) -> None:
    """Adapt Turkish translation for Turkey context."""
    from btcedu.core.adapter import adapt_script

    settings = ctx.obj["settings"]
    if dry_run:
        settings.dry_run = True

    session = ctx.obj["session_factory"]()
    try:
        for eid in episode_ids:
            try:
                result = adapt_script(session, eid, settings, force=force)
                click.echo(
                    f"[OK] {eid} -> {result.adapted_path} "
                    f"({result.adaptation_count} adaptations, ${result.cost_usd:.4f})"
                )
            except Exception as e:
                click.echo(f"[FAIL] {eid}: {e}", err=True)
    finally:
        session.close()
```

#### 2.7 `btcedu/core/reviewer.py`

**Changes**: Verify existing functions support stage="adapt" (likely already generic)

**Verification**:
- `create_review_task()` accepts any stage string ✓
- `approve_review()` works for any stage ✓
- `reject_review()` works for any stage ✓
- `request_changes()` works for any stage ✓
- `get_review_detail()` loads diff_data generically ✓

**Potential addition** (if not already present):

```python
def get_adaptation_review_data(session: Session, review_id: int) -> dict:
    """Load adaptation-specific review data with literal translation for comparison."""
    review = session.query(ReviewTask).filter_by(id=review_id).one_or_none()
    if not review or review.stage != "adapt":
        raise ValueError(f"Review {review_id} is not an adaptation review")

    # Load adapted script
    adapted_path = Path(json.loads(review.artifact_paths)[0])
    adapted_text = adapted_path.read_text(encoding="utf-8") if adapted_path.exists() else ""

    # Load literal translation for comparison
    episode_id = review.episode_id
    translation_path = Path(f"data/transcripts/{episode_id}/transcript.tr.txt")
    translation_text = translation_path.read_text(encoding="utf-8") if translation_path.exists() else ""

    # Load diff JSON
    diff_data = {}
    if review.diff_path:
        diff_path = Path(review.diff_path)
        if diff_path.exists():
            diff_data = json.loads(diff_path.read_text(encoding="utf-8"))

    return {
        "adapted_text": adapted_text,
        "translation_text": translation_text,
        "diff_data": diff_data,
        "review": review,
    }
```

#### 2.8 `btcedu/web/api.py`

**Changes**: Add endpoint for adaptation review data (if generic review endpoint doesn't already serve this)

```python
@api_bp.route("/reviews/<int:review_id>/adaptation")
def get_adaptation_review_data_route(review_id: int):
    """Get adaptation-specific review data with literal translation."""
    from btcedu.core.reviewer import get_adaptation_review_data

    session = get_session()
    try:
        data = get_adaptation_review_data(session, review_id)
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    finally:
        session.close()
```

**Note**: If the existing `/reviews/<id>` endpoint already generically loads diff_data + artifact text, this may not be needed. Verify during implementation.

#### 2.9 Web Dashboard Review Templates (conditional extension)

**File**: `btcedu/web/templates/review_detail.html` (or equivalent)

**Changes**: Extend existing review detail template to detect stage type and render adaptation diff view

**Pattern**:

```html
{% if review.stage == 'correct' %}
  <!-- Existing correction diff viewer (side-by-side, highlighted changes) -->
  <div class="diff-viewer correction">
    <!-- ... existing correction diff rendering ... -->
  </div>

{% elif review.stage == 'adapt' %}
  <!-- New adaptation diff viewer (literal translation vs adapted, T1/T2 highlighting) -->
  <div class="diff-viewer adaptation">
    <div class="diff-header">
      <h3>Adaptation Review</h3>
      <p>{{ review_data.diff_data.summary.total_adaptations }} adaptations
         ({{ review_data.diff_data.summary.tier1_count }} mechanical,
          {{ review_data.diff_data.summary.tier2_count }} editorial)</p>
    </div>

    <div class="diff-columns">
      <div class="diff-column original">
        <h4>Literal Translation</h4>
        <pre>{{ review_data.translation_text }}</pre>
      </div>
      <div class="diff-column adapted">
        <h4>Adapted Version</h4>
        <pre class="adapted-text">{{ review_data.adapted_text }}</pre>
      </div>
    </div>

    <div class="adaptations-list">
      <h4>Adaptations</h4>
      {% for adaptation in review_data.diff_data.adaptations %}
        <div class="adaptation-item tier-{{ adaptation.tier }}">
          <span class="tier-badge">{{ adaptation.tier }}</span>
          <span class="category">{{ adaptation.category }}</span>
          <div class="change">
            <span class="original">{{ adaptation.original }}</span>
            <span class="arrow">→</span>
            <span class="adapted">{{ adaptation.adapted }}</span>
          </div>
          <div class="context">{{ adaptation.context }}</div>
        </div>
      {% endfor %}
    </div>
  </div>

{% endif %}

<!-- Existing approve/reject/request-changes buttons (work for any stage) -->
<div class="review-actions">
  <button onclick="approveReview({{ review.id }})">✓ Approve</button>
  <button onclick="rejectReview({{ review.id }})">✗ Reject</button>
  <button onclick="requestChanges({{ review.id }})">✎ Request Changes</button>
</div>
```

**CSS additions** (for T1/T2 color-coding):

```css
.adaptation-item.tier-T1 {
  background-color: #e8f5e9; /* Light green: low-risk mechanical */
  border-left: 4px solid #4caf50;
}

.adaptation-item.tier-T2 {
  background-color: #fff3e0; /* Light orange: editorial, needs attention */
  border-left: 4px solid #ff9800;
}

.tier-badge {
  font-weight: bold;
  padding: 2px 6px;
  border-radius: 3px;
}

.tier-T1 .tier-badge {
  background-color: #4caf50;
  color: white;
}

.tier-T2 .tier-badge {
  background-color: #ff9800;
  color: white;
}
```

**[ASSUMPTION]**: The existing review template is modular enough to add conditional rendering based on `review.stage`. If the current template is monolithic, may need minor refactoring.

---

## 3. Adaptation Prompt Template

Full draft of `btcedu/prompts/templates/adapt.md`:

```markdown
---
name: adapt
model: claude-sonnet-4-20250514
temperature: 0.3
max_tokens: 12000
description: Turkey-context cultural adaptation of Turkish Bitcoin/crypto content with tiered neutralization rules
author: content_owner
---

# System

You are a specialized content adapter for Turkish audiences. Your task is to take a faithful Turkish translation of German Bitcoin/cryptocurrency content and adapt it for a Turkish audience by neutralizing Germany-specific references while preserving ALL technical accuracy and editorial objectivity.

You will apply a **tiered adaptation system**:
- **Tier 1 (T1)**: Mechanical, low-risk adaptations (institutions, currency, tone)
- **Tier 2 (T2)**: Editorial adaptations requiring human review (cultural references, regulatory context)

Every adaptation MUST be tagged with `[T1]` or `[T2]` in your output.

---

# Adaptation Rules

## Tier 1 — Mechanical (Low Risk, Auto-Applicable)

These are safe, consistent replacements. Tag each with `[T1]`:

### 1. German Institutions → Turkish Equivalents or Generic References

Replace German-specific institutions with Turkish equivalents OR generic descriptions:

- **BaFin** (German financial regulator) → `[T1: SPK (Sermaye Piyasası Kurulu)]` OR `[T1: Türkiye'deki düzenleyici kurum]`
- **Sparkasse** (German savings bank) → `[T1: yerel banka]` OR `[T1: tasarruf bankası]`
- **Bundesbank** → `[T1: Merkez Bankası]` (generic central bank)
- **Finanzamt** (tax office) → `[T1: vergi dairesi]`
- **Bundestag** → `[T1: Meclis]` OR remove if not critical

**Examples**:
- Original: "BaFin hat neue Regeln erlassen"
- Translation: "BaFin yeni kurallar yayınladı"
- Adapted: "`[T1: Türkiye'deki finansal düzenleyici (SPK)]` yeni kurallar yayınladı"

### 2. Currency Conversions

Convert Euro amounts to Turkish Lira or USD, context-appropriate:

- **Small amounts** (< €100): Convert to TRY with approximate equivalent: "€50" → `[T1: ~2.000 TL (yaklaşık 50 EUR)]`
- **Large amounts** (> €1000): Keep in EUR or convert to USD: "€10.000" → `[T1: 10.000 EUR (~11.000 USD)]`
- **Bitcoin prices**: ALWAYS use USD: "€30.000" → `[T1: 30.000 USD]`
- **Keep currency symbols**: ₿, $, €, ₺

### 3. Tone Adjustment to Turkish Influencer Style

Adjust formality and address:

- Use **"siz"** (formal you) for direct address
- Conversational, engaging tone (not stiff translation)
- Turkish idioms where appropriate (but don't force)
- Paragraph-level tone smoothing (remove German-style formality)

Tag tone adjustments: `[T1: ton düzeltmesi]`

### 4. Remove Germany-Specific Legal/Tax Advice

If the content provides Germany-specific legal or tax guidance:

- **Remove** the specific advice
- **Replace** with: `[T1: [kaldırıldı: ülkeye özgü yasal bilgi — Türkiye'de farklı düzenlemeler geçerlidir]]`
- **Do NOT** invent Turkish legal advice to replace it

**Example**:
- Original: "In Deutschland sind Bitcoin-Gewinne nach einem Jahr steuerfrei"
- Translation: "Almanya'da Bitcoin kazançları bir yıl sonra vergiden muaftır"
- Adapted: "`[T1: [kaldırıldı: Almanya'ya özgü vergi bilgisi]]` — Not: Bitcoin vergilendirmesi ülkelere göre farklılık gösterir, Türkiye için güncel mevzuata başvurun."

---

## Tier 2 — Editorial (Flagged for Review)

These require human judgment. Tag each with `[T2]`:

### 5. German Cultural References → Turkish Equivalents

Replace Germany-specific cultural examples with Turkish equivalents ONLY when:
- The example is illustrative (not factual reporting)
- A clear Turkish equivalent exists
- The adaptation doesn't change the underlying point

**Tag each substitution**: `[T2: kültürel uyarlama: "X" → "Y"]`

**Examples**:
- "Oktoberfest" → `[T2: kültürel uyarlama: "Oktoberfest" → "Ramazan festivalleri"]` (only if the point is "large public festival")
- "Autobahn" → `[T2: kültürel uyarlama: "Autobahn" → "otoyol"]` (if illustrating "fast highway")
- "Deutsche Telekom" → `[T2: kültürel uyarlama: "Deutsche Telekom" → "Turkcell"]` (only if generic telco example)

**If uncertain**, do NOT adapt — leave original and tag `[T2: kültürel referans korundu]`

### 6. Regulatory/Legal Context Beyond Simple Removal

If the content discusses regulatory frameworks beyond a single law reference:
- Summarize the German regulatory position neutrally
- Add a disclaimer: `[T2: Türkiye'de bu konu farklı düzenlenmiştir, yerel mevzuata başvurun]`
- **Do NOT invent Turkish regulatory details**

**Example**:
- Original: "Die MiCA-Verordnung der EU reguliert Krypto-Assets in Deutschland"
- Adapted: "AB'nin MiCA düzenlemesi Almanya'da kripto varlıkları düzenler. `[T2: Türkiye'nin kripto düzenlemeleri farklıdır; güncel bilgi için yerel kaynaklara başvurun.]`"

---

## Hard Constraints (FORBIDDEN)

These actions are STRICTLY PROHIBITED. Violation is a critical error:

### 7. Preserve ALL Bitcoin/Crypto Technical Facts

- **NO simplification** of technical explanations (mining, consensus, cryptography)
- **NO reinterpretation** of Bitcoin protocol details
- **NO changes** to technical terminology beyond localization

### 8. NEVER Invent Turkish Regulatory Details

- **DO NOT** cite Turkish laws, regulations, or legal precedents unless they were in the German original
- **DO NOT** fabricate Turkish regulatory positions
- If uncertain: use `[T2: Türkiye'de bu konu farklı düzenlenmiştir]` (no specifics)

### 9. NO Financial Advice, Investment Recommendations, or Price Predictions

- If the German source avoids financial advice, YOU MUST TOO
- **Do NOT add**: "Bu bir yatırım tavsiyesi değildir" unless it was in the original
- Keep factual reporting factual; keep opinion as opinion

### 10. NO Political Commentary or Partisan Framing

- Remain politically neutral
- **Do NOT** add commentary on Turkish politics, government, or parties
- If the German source criticizes German policy, adapt neutrally (e.g., "government policy" not "Erdoğan's policy")

### 11. DO NOT Present Adaptations as Original Source Claims

- Adaptations are YOUR editorial changes, not the source's claims
- Use markers (`[T1]`, `[T2]`) to distinguish adaptations from original content
- In the final output, these markers are preserved for review transparency

### 12. Editorial Neutrality

- Adaptations change **framing**, NOT **facts**
- Cultural adaptation ≠ content creation
- When in doubt, adapt LESS rather than MORE

---

# Input

You will receive:

1. **Turkish Translation** (literal, faithful translation from German)
2. **Original German Corrected Transcript** (for reference, to understand source context)

{{ reviewer_feedback }}

## Turkish Translation

{{ translation }}

## Original German (for reference)

{{ original_german }}

---

# Output Format

Return the **adapted Turkish script** as Markdown.

**Requirements**:
1. All adaptations MUST be tagged inline with `[T1]` or `[T2]`
2. Include all `[T1]`/`[T2]` markers in the output (they will be parsed for review)
3. Use Markdown formatting (headings, lists, emphasis) to structure the content
4. NO preamble, NO metadata header, NO explanations — JUST the adapted script

**Example Output**:

```markdown
# Bitcoin'in Tarihi

Bitcoin, 2008 yılında Satoshi Nakamoto tarafından yaratıldı. `[T1: [kaldırıldı: Almanya'ya özgü erken benimseme bilgisi]]` Dünya çapında hızla yayıldı.

Bitcoin'in fiyatı `[T1: 2023'te 30.000 USD]` seviyelerine ulaştı. `[T2: Türkiye'de kripto varlık düzenlemeleri farklıdır]`.

## Madencilik (Mining)

Madencilik, işlem doğrulama sürecidir. `[T1: ton düzeltmesi]` Bu süreç, Proof of Work mekanizması ile çalışır...
```

---

# Final Checklist Before Output

- [ ] All T1/T2 rules applied correctly?
- [ ] No invented Turkish laws or regulations?
- [ ] All Bitcoin technical facts preserved?
- [ ] No financial advice added?
- [ ] No political commentary added?
- [ ] Adaptations clearly tagged?
- [ ] Editorial neutrality maintained?

Proceed with the adaptation now.
```

---

## 4. Adapter Module Design

### 4.1 Function Signature

```python
def adapt_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> AdaptationResult:
    """
    Adapt a Turkish translation for Turkey context using tiered rules.

    Args:
        session: SQLAlchemy session
        episode_id: Episode identifier
        settings: Application settings
        force: If True, re-adapt even if output exists

    Returns:
        AdaptationResult with paths, metrics, and cost

    Raises:
        ValueError: If episode not found, status invalid, or Review Gate 1 not approved
        FileNotFoundError: If translation or corrected transcript missing
        RuntimeError: If adaptation fails
    """
```

### 4.2 Result Dataclass

```python
from dataclasses import dataclass

@dataclass
class AdaptationResult:
    """Summary of adaptation operation for one episode."""

    episode_id: str
    adapted_path: str               # Path to script.adapted.tr.md
    diff_path: str                  # Path to adaptation_diff.json
    provenance_path: str            # Path to adapt_provenance.json
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0       # Turkish translation length
    output_char_count: int = 0      # Adapted script length
    adaptation_count: int = 0       # Total T1 + T2 adaptations
    tier1_count: int = 0            # Mechanical adaptations
    tier2_count: int = 0            # Editorial adaptations
    segments_processed: int = 1     # How many segments (1 if no segmentation)
    skipped: bool = False           # True if idempotent skip
```

### 4.3 Core Logic Flow

```python
def adapt_script(...) -> AdaptationResult:
    # 1. Validate episode exists and is TRANSLATED
    episode = session.query(Episode).filter_by(episode_id=episode_id).one_or_none()
    if not episode:
        raise ValueError(f"Episode {episode_id} not found")

    if episode.status != EpisodeStatus.TRANSLATED:
        raise ValueError(
            f"Episode {episode_id} is not TRANSLATED (current: {episode.status}). "
            f"Run 'btcedu translate --episode-id {episode_id}' first."
        )

    # 2. Check Review Gate 1 approval (correction must be approved)
    from btcedu.core.reviewer import has_approved_review

    if not has_approved_review(session, episode_id, "correct"):
        raise ValueError(
            f"Episode {episode_id} correction not approved. "
            f"Review and approve correction before adapting."
        )

    # 3. Define paths
    translation_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
    if not translation_path.exists():
        raise FileNotFoundError(f"Turkish translation not found: {translation_path}")

    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(f"Corrected German transcript not found: {corrected_path}")

    adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    diff_path = Path(settings.outputs_dir) / episode_id / "review" / "adaptation_diff.json"
    provenance_path = Path(settings.outputs_dir) / episode_id / "provenance" / "adapt_provenance.json"

    # Ensure directories exist
    adapted_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)

    # 4. Check idempotency (skip if already done and not forced)
    if not force:
        skip_result = _is_adaptation_current(
            adapted_path, provenance_path, translation_path, corrected_path, settings, session
        )
        if skip_result:
            logger.info(f"Adaptation is current for {episode_id} (use --force to re-adapt)")
            return skip_result

    # 5. Load and validate prompt
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "adapt.md"
    prompt_version = registry.register_version("adapt", template_file, set_default=True)
    metadata, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # 6. Inject reviewer feedback if present (from previous request_changes)
    from btcedu.core.reviewer import get_latest_reviewer_feedback

    reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "adapt")
    if reviewer_feedback:
        feedback_block = (
            "## Revisor Geri Bildirimi (lütfen bu düzeltmeleri uygulayın)\n\n"
            f"{reviewer_feedback}\n\n"
            "Önemli: Bu geri bildirimi çıktıda aynen aktarmayın, yalnızca düzeltme kılavuzu olarak kullanın."
        )
        template_body = template_body.replace("{{ reviewer_feedback }}", feedback_block)
    else:
        template_body = template_body.replace("{{ reviewer_feedback }}", "")

    # 7. Read inputs
    translation_text = translation_path.read_text(encoding="utf-8")
    german_text = corrected_path.read_text(encoding="utf-8")

    input_char_count = len(translation_text)

    # Compute input content hashes for idempotency
    translation_hash = hashlib.sha256(translation_text.encode("utf-8")).hexdigest()
    german_hash = hashlib.sha256(german_text.encode("utf-8")).hexdigest()

    # 8. Split prompt into system + user parts
    system_prompt, user_template = _split_prompt(template_body)

    # 9. Segment if needed (long texts)
    MAX_SEGMENT_CHARS = 15000
    segments = _segment_text(translation_text, max_chars=MAX_SEGMENT_CHARS)

    logger.info(f"Adapting {len(segments)} segment(s) for {episode_id}")

    # 10. Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.ADAPT,
        status=RunStatus.RUNNING,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.flush()

    try:
        # 11. Process segments via Claude
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        adapted_segments = []

        for i, segment in enumerate(segments):
            # For multi-segment: include corresponding German segment (approximate alignment)
            german_segment = german_text  # Simplification: use full German for reference
            # [ASSUMPTION]: For v1, we pass full German text as reference for all segments.
            # A more sophisticated implementation would align German segments with Turkish segments.

            user_message = (
                user_template
                .replace("{{ translation }}", segment)
                .replace("{{ original_german }}", german_segment)
            )

            dry_run_path = adapted_path.parent / f"adapt_request_seg{i+1}.json" if settings.dry_run else None

            response = call_claude(system_prompt, user_message, settings, dry_run_path=dry_run_path)
            adapted_segments.append(response.text)

            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

            logger.info(
                f"Segment {i+1}/{len(segments)}: "
                f"{response.input_tokens} in, {response.output_tokens} out, ${response.cost_usd:.4f}"
            )

        adapted_text = "\n\n".join(adapted_segments)
        output_char_count = len(adapted_text)

        # 12. Compute adaptation diff
        diff_data = compute_adaptation_diff(
            translation_text, adapted_text, episode_id
        )

        adaptation_count = diff_data["summary"]["total_adaptations"]
        tier1_count = diff_data["summary"]["tier1_count"]
        tier2_count = diff_data["summary"]["tier2_count"]

        logger.info(
            f"Adaptation complete: {adaptation_count} adaptations "
            f"(T1: {tier1_count}, T2: {tier2_count})"
        )

        # 13. Write outputs
        adapted_path.write_text(adapted_text, encoding="utf-8")
        diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 14. Write provenance JSON
        elapsed = (datetime.utcnow() - pipeline_run.started_at).total_seconds()
        provenance = {
            "stage": "adapt",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "adapt",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(translation_path), str(corrected_path)],
            "input_content_hashes": {
                "translation": translation_hash,
                "german": german_hash,
            },
            "output_files": [str(adapted_path), str(diff_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(segments),
            "adaptation_summary": {
                "total_adaptations": adaptation_count,
                "tier1_count": tier1_count,
                "tier2_count": tier2_count,
            },
        }
        provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")

        # 15. Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="adapt",
            file_path=str(adapted_path),
            model=settings.claude_model,
            prompt_hash=prompt_content_hash,
            retrieval_snapshot_path=None,
        )
        session.add(artifact)

        # 16. Update PipelineRun + Episode status
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost

        episode.status = EpisodeStatus.ADAPTED
        episode.error_message = None
        session.commit()

        logger.info(f"✓ Adapted {episode_id}: {adapted_path}")

        return AdaptationResult(
            episode_id=episode_id,
            adapted_path=str(adapted_path),
            diff_path=str(diff_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=input_char_count,
            output_char_count=output_char_count,
            adaptation_count=adaptation_count,
            tier1_count=tier1_count,
            tier2_count=tier2_count,
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = f"Adaptation failed: {e}"
        session.commit()
        logger.error(f"✗ Adaptation failed for {episode_id}: {e}")
        raise
```

### 4.4 Helper Functions

```python
def _is_adaptation_current(
    adapted_path: Path,
    provenance_path: Path,
    translation_path: Path,
    corrected_path: Path,
    settings: Settings,
    session: Session,
) -> AdaptationResult | None:
    """
    Check if adaptation is current (idempotency).

    Returns AdaptationResult with skipped=True if current, else None.
    """
    # Check if output exists
    if not adapted_path.exists() or not provenance_path.exists():
        return None

    # Check for .stale marker (cascade invalidation)
    stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
    if stale_marker.exists():
        logger.info(f"Adaptation marked stale (upstream change), will reprocess")
        stale_marker.unlink()  # Consume marker
        return None

    # Load provenance
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        logger.warning("Provenance file corrupt or missing, will reprocess")
        return None

    # Check input content hashes
    translation_text = translation_path.read_text(encoding="utf-8")
    german_text = corrected_path.read_text(encoding="utf-8")

    translation_hash = hashlib.sha256(translation_text.encode("utf-8")).hexdigest()
    german_hash = hashlib.sha256(german_text.encode("utf-8")).hexdigest()

    stored_translation_hash = provenance.get("input_content_hashes", {}).get("translation")
    stored_german_hash = provenance.get("input_content_hashes", {}).get("german")

    if translation_hash != stored_translation_hash or german_hash != stored_german_hash:
        logger.info("Input content changed, will reprocess")
        return None

    # Check prompt hash (has prompt changed?)
    registry = PromptRegistry(session)
    template_file = TEMPLATES_DIR / "adapt.md"
    _, template_body = registry.load_template(template_file)
    current_prompt_hash = registry.compute_hash(template_body)

    stored_prompt_hash = provenance.get("prompt_hash")
    if current_prompt_hash != stored_prompt_hash:
        logger.info("Prompt version changed, will reprocess")
        return None

    # All checks passed: adaptation is current
    logger.info("Adaptation is current (inputs + prompt unchanged)")

    # Return cached result
    diff_path = adapted_path.parent.parent / "review" / "adaptation_diff.json"
    diff_data = {}
    if diff_path.exists():
        diff_data = json.loads(diff_path.read_text(encoding="utf-8"))

    return AdaptationResult(
        episode_id=provenance["episode_id"],
        adapted_path=str(adapted_path),
        diff_path=str(diff_path) if diff_path.exists() else "",
        provenance_path=str(provenance_path),
        input_tokens=provenance.get("input_tokens", 0),
        output_tokens=provenance.get("output_tokens", 0),
        cost_usd=provenance.get("cost_usd", 0.0),
        input_char_count=len(translation_text),
        output_char_count=len(adapted_path.read_text(encoding="utf-8")),
        adaptation_count=diff_data.get("summary", {}).get("total_adaptations", 0),
        tier1_count=diff_data.get("summary", {}).get("tier1_count", 0),
        tier2_count=diff_data.get("summary", {}).get("tier2_count", 0),
        segments_processed=provenance.get("segments_processed", 1),
        skipped=True,
    )


def _split_prompt(template_body: str) -> tuple[str, str]:
    """
    Split prompt template into system prompt and user template.

    Splits at '# Input' marker (adapt stage) or '# Turkish Translation' marker.
    """
    markers = ["# Input", "# Turkish Translation"]
    for marker in markers:
        if marker in template_body:
            parts = template_body.split(marker, 1)
            system_prompt = parts[0].strip()
            user_template = marker + parts[1]
            return system_prompt, user_template

    # Fallback: no marker found, entire template is user message
    return "", template_body


def _segment_text(text: str, max_chars: int = 15000) -> list[str]:
    """
    Segment text at paragraph boundaries, respecting max_chars.

    If a single paragraph exceeds max_chars, split at sentence boundaries.
    """
    # [ASSUMPTION]: Reuse translator's _segment_text() logic exactly.
    # Implementation: Split on "\n\n" (paragraph breaks), combine until max_chars.
    # If paragraph > max_chars, split on ". " (sentence breaks).

    paragraphs = text.split("\n\n")
    segments = []
    current_segment = []
    current_length = 0

    for para in paragraphs:
        para_len = len(para)

        if current_length + para_len > max_chars and current_segment:
            # Flush current segment
            segments.append("\n\n".join(current_segment))
            current_segment = []
            current_length = 0

        if para_len > max_chars:
            # Single paragraph too long: split at sentences
            sentences = para.split(". ")
            for sent in sentences:
                sent_len = len(sent)
                if current_length + sent_len > max_chars and current_segment:
                    segments.append("\n\n".join(current_segment))
                    current_segment = []
                    current_length = 0
                current_segment.append(sent)
                current_length += sent_len
        else:
            current_segment.append(para)
            current_length += para_len

    if current_segment:
        segments.append("\n\n".join(current_segment))

    return segments
```

---

## 5. Adaptation Diff Computation

The adaptation diff must parse `[T1]` and `[T2]` tags from the adapted text and classify changes.

### 5.1 Diff Algorithm

```python
import re
from typing import List, Dict, Any

def compute_adaptation_diff(
    translation: str,
    adapted: str,
    episode_id: str,
) -> dict:
    """
    Compute adaptation diff by parsing [T1]/[T2] tags in adapted text.

    Returns:
        {
            "episode_id": str,
            "adaptations": [
                {
                    "tier": "T1" | "T2",
                    "category": str,
                    "original": str,
                    "adapted": str,
                    "context": str,
                    "position": {"start": int, "end": int}
                },
                ...
            ],
            "summary": {
                "total_adaptations": int,
                "tier1_count": int,
                "tier2_count": int,
                "by_category": {"institution_replacement": int, ...}
            }
        }
    """
    adaptations = []

    # Regex to match [T1: ...] or [T2: ...]
    pattern = r"\[(T1|T2):\s*([^\]]+)\]"

    for match in re.finditer(pattern, adapted):
        tier = match.group(1)
        content = match.group(2).strip()
        start = match.start()
        end = match.end()

        # Extract context (50 chars before/after)
        context_start = max(0, start - 50)
        context_end = min(len(adapted), end + 50)
        context = adapted[context_start:context_end]

        # Classify category from content
        category = _classify_adaptation(content)

        # Extract original vs adapted (if format is "original → adapted")
        if "→" in content:
            parts = content.split("→", 1)
            original_text = parts[0].strip().strip('"')
            adapted_text = parts[1].strip().strip('"')
        else:
            # No arrow: content is the adapted replacement
            original_text = ""
            adapted_text = content

        adaptations.append({
            "tier": tier,
            "category": category,
            "original": original_text,
            "adapted": adapted_text,
            "context": context,
            "position": {"start": start, "end": end},
        })

    # Summarize
    tier1_count = sum(1 for a in adaptations if a["tier"] == "T1")
    tier2_count = sum(1 for a in adaptations if a["tier"] == "T2")

    by_category = {}
    for a in adaptations:
        cat = a["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    return {
        "episode_id": episode_id,
        "original_length": len(translation),
        "adapted_length": len(adapted),
        "adaptations": adaptations,
        "summary": {
            "total_adaptations": len(adaptations),
            "tier1_count": tier1_count,
            "tier2_count": tier2_count,
            "by_category": by_category,
        },
    }


def _classify_adaptation(content: str) -> str:
    """
    Classify adaptation by analyzing the tag content.

    Categories:
    - institution_replacement: BaFin, Sparkasse, etc.
    - currency_conversion: EUR → TRY/USD
    - tone_adjustment: "ton düzeltmesi"
    - legal_removal: "[kaldırıldı: ..."
    - cultural_reference: "kültürel uyarlama", "kültürel referans"
    - regulatory_context: "düzenleme", "mevzuat"
    - other: fallback
    """
    content_lower = content.lower()

    if "kaldırıldı" in content_lower or "[removed" in content_lower:
        return "legal_removal"
    elif "ton düzeltmesi" in content_lower or "tone" in content_lower:
        return "tone_adjustment"
    elif "kültürel" in content_lower or "cultural" in content_lower:
        return "cultural_reference"
    elif any(inst in content_lower for inst in ["bafin", "sparkasse", "bundesbank", "spk", "merkez bankası"]):
        return "institution_replacement"
    elif any(curr in content_lower for curr in ["eur", "usd", "tl", "€", "$", "₺"]):
        return "currency_conversion"
    elif "düzenleme" in content_lower or "mevzuat" in content_lower or "regulat" in content_lower:
        return "regulatory_context"
    else:
        return "other"
```

### 5.2 Diff JSON Format

```json
{
  "episode_id": "abc123",
  "original_length": 15000,
  "adapted_length": 14900,
  "adaptations": [
    {
      "tier": "T1",
      "category": "institution_replacement",
      "original": "BaFin",
      "adapted": "Türkiye'deki finansal düzenleyici (SPK)",
      "context": "...BaFin yeni kurallar yayınladı...",
      "position": {"start": 1234, "end": 1289}
    },
    {
      "tier": "T1",
      "category": "currency_conversion",
      "original": "50 EUR",
      "adapted": "~2.000 TL (yaklaşık 50 EUR)",
      "context": "...fiyat 50 EUR idi...",
      "position": {"start": 2456, "end": 2489}
    },
    {
      "tier": "T2",
      "category": "cultural_reference",
      "original": "Oktoberfest",
      "adapted": "büyük halk festivali",
      "context": "...Oktoberfest gibi etkinliklerde...",
      "position": {"start": 5678, "end": 5732}
    },
    {
      "tier": "T1",
      "category": "legal_removal",
      "original": "Almanya'da Bitcoin kazançları bir yıl sonra vergiden muaftır",
      "adapted": "[kaldırıldı: Almanya'ya özgü vergi bilgisi]",
      "context": "...[kaldırıldı: Almanya'ya özgü vergi bilgisi]...",
      "position": {"start": 7890, "end": 7945}
    }
  ],
  "summary": {
    "total_adaptations": 42,
    "tier1_count": 35,
    "tier2_count": 7,
    "by_category": {
      "institution_replacement": 8,
      "currency_conversion": 12,
      "tone_adjustment": 10,
      "legal_removal": 5,
      "cultural_reference": 5,
      "regulatory_context": 2
    }
  }
}
```

---

## 6. Review Gate 2 Design

### 6.1 Integration Points

**Review Gate 2** is created AFTER `adapt_script()` succeeds:

1. `adapt_script()` completes → Episode status = ADAPTED
2. Pipeline reaches `review_gate_2` stage (in `pipeline.py`)
3. Check if ReviewTask already exists for this episode + stage="adapt":
   - If APPROVED → proceed to next stage (CHAPTERIZE in future)
   - If PENDING/IN_REVIEW → pause pipeline (return `review_pending`)
   - If none exists → create new ReviewTask
4. ReviewTask created with:
   - `stage="adapt"`
   - `artifact_paths=[adapted_path]`
   - `diff_path=adaptation_diff.json`
   - `status=PENDING`

### 6.2 Approval Flow

**Approve**:
- Call `approve_review(session, review_id, notes)`
- ReviewTask status → APPROVED
- Episode remains ADAPTED (pipeline can proceed)
- ReviewDecision created with decision="approved"

**Reject**:
- Call `reject_review(session, review_id, notes)`
- ReviewTask status → REJECTED
- Episode status reverted to TRANSLATED
- Adapted output marked stale (`.stale` marker created)
- ReviewDecision created with decision="rejected"

**Request Changes**:
- Call `request_changes(session, review_id, notes)`
- ReviewTask status → CHANGES_REQUESTED
- Episode status reverted to TRANSLATED
- Reviewer notes stored in ReviewTask.reviewer_notes
- Adapted output marked stale
- On next `adapt_script()` run, notes injected into prompt via `{{ reviewer_feedback }}`

### 6.3 Pipeline Pause Logic

The v2 pipeline orchestration already handles review gates generically:

- `run_episode_pipeline()` calls `_run_stage()` for each stage in sequence
- If `_run_stage()` returns `StageResult(status="review_pending")`, pipeline stops
- Episode status remains at previous successful stage (ADAPTED)
- Dashboard shows "Pending Review" badge
- On approval, next `run_episode_pipeline()` call resumes from ADAPTED → CHAPTERIZE

No new code needed in `run_episode_pipeline()`; Review Gate 2 follows Review Gate 1 pattern exactly.

---

## 7. Adaptation Review UI

### 7.1 Conditional Rendering

Extend `btcedu/web/templates/review_detail.html` to detect `review.stage == "adapt"`:

```html
{% if review.stage == 'correct' %}
  <!-- Existing correction diff viewer -->
  <div class="diff-viewer correction">
    <!-- ... existing code ... -->
  </div>

{% elif review.stage == 'adapt' %}
  <!-- New adaptation diff viewer -->
  <div class="diff-viewer adaptation">
    <!-- Adaptation-specific rendering (see section 7.2) -->
  </div>

{% else %}
  <p>Unknown review stage: {{ review.stage }}</p>
{% endif %}
```

### 7.2 Adaptation Diff View Components

**Header**:
- Summary: "42 adaptations (35 mechanical, 7 editorial)"
- Legend: T1 = green (mechanical), T2 = orange (editorial, needs attention)

**Side-by-side comparison**:
- Left column: Literal Turkish translation (from `transcript.tr.txt`)
- Right column: Adapted script (from `script.adapted.tr.md`)

**Adaptations list** (below columns):
- Each adaptation displayed as a card
- Color-coded by tier (T1 green, T2 orange)
- Shows: tier badge, category, original → adapted, context snippet

**Actions** (same as correction review):
- ✓ Approve (green button)
- ✗ Reject (red button)
- ✎ Request Changes (yellow button, opens textarea for notes)

### 7.3 CSS Styling

```css
/* Tier color-coding */
.adaptation-item {
  margin-bottom: 1rem;
  padding: 0.75rem;
  border-radius: 4px;
  border-left: 4px solid;
}

.adaptation-item.tier-T1 {
  background-color: #e8f5e9;
  border-left-color: #4caf50;
}

.adaptation-item.tier-T2 {
  background-color: #fff3e0;
  border-left-color: #ff9800;
}

.tier-badge {
  display: inline-block;
  font-weight: bold;
  padding: 2px 8px;
  border-radius: 3px;
  margin-right: 8px;
  color: white;
  font-size: 0.85rem;
}

.tier-T1 .tier-badge {
  background-color: #4caf50;
}

.tier-T2 .tier-badge {
  background-color: #ff9800;
}

.adaptation-item .category {
  font-size: 0.9rem;
  color: #666;
  text-transform: uppercase;
  margin-left: 8px;
}

.adaptation-item .change {
  margin: 0.5rem 0;
  font-family: monospace;
}

.adaptation-item .original {
  color: #d32f2f;
  text-decoration: line-through;
}

.adaptation-item .arrow {
  margin: 0 8px;
  color: #666;
}

.adaptation-item .adapted {
  color: #388e3c;
  font-weight: 500;
}

.adaptation-item .context {
  margin-top: 0.5rem;
  font-size: 0.85rem;
  color: #666;
  font-style: italic;
}

/* Diff columns */
.diff-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 2rem;
}

.diff-column {
  border: 1px solid #ddd;
  border-radius: 4px;
  padding: 1rem;
  background-color: #f9f9f9;
}

.diff-column h4 {
  margin-top: 0;
}

.diff-column pre {
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 500px;
  overflow-y: auto;
}
```

### 7.4 JavaScript (if needed)

If the existing review page uses JavaScript for approve/reject/request-changes, no changes needed. The buttons call the same API endpoints (`/reviews/<id>/approve`, etc.) which are already generic.

If adaptation review needs special handling (e.g., filtering by tier), add:

```javascript
function filterAdaptationsByTier(tier) {
  const items = document.querySelectorAll('.adaptation-item');
  items.forEach(item => {
    if (tier === 'all' || item.classList.contains(`tier-${tier}`)) {
      item.style.display = 'block';
    } else {
      item.style.display = 'none';
    }
  });
}
```

And add filter buttons:
```html
<div class="filter-controls">
  <button onclick="filterAdaptationsByTier('all')">All</button>
  <button onclick="filterAdaptationsByTier('T1')">T1 (Mechanical)</button>
  <button onclick="filterAdaptationsByTier('T2')">T2 (Editorial)</button>
</div>
```

---

## 8. Provenance, Idempotency, Cascade Invalidation

### 8.1 Provenance JSON

Written to `data/outputs/{episode_id}/provenance/adapt_provenance.json`:

```json
{
  "stage": "adapt",
  "episode_id": "abc123",
  "timestamp": "2026-02-24T10:30:00Z",
  "prompt_name": "adapt",
  "prompt_version": 1,
  "prompt_hash": "sha256:def456...",
  "model": "claude-sonnet-4-20250514",
  "model_params": {"temperature": 0.3, "max_tokens": 12000},
  "input_files": [
    "data/transcripts/abc123/transcript.tr.txt",
    "data/transcripts/abc123/transcript.corrected.de.txt"
  ],
  "input_content_hashes": {
    "translation": "sha256:abc123...",
    "german": "sha256:def456..."
  },
  "output_files": [
    "data/outputs/abc123/script.adapted.tr.md",
    "data/outputs/abc123/review/adaptation_diff.json"
  ],
  "input_tokens": 8500,
  "output_tokens": 9200,
  "cost_usd": 0.163,
  "duration_seconds": 18.7,
  "segments_processed": 1,
  "adaptation_summary": {
    "total_adaptations": 42,
    "tier1_count": 35,
    "tier2_count": 7
  }
}
```

### 8.2 Idempotency Check

Implemented in `_is_adaptation_current()`:

1. **Output exists**: `script.adapted.tr.md` + `adapt_provenance.json` must exist
2. **No stale marker**: `script.adapted.tr.md.stale` must NOT exist
3. **Input hashes match**: SHA-256 of `transcript.tr.txt` and `transcript.corrected.de.txt` match stored hashes in provenance
4. **Prompt hash matches**: SHA-256 of current `adapt.md` template matches stored prompt_hash

If all checks pass → return cached `AdaptationResult` with `skipped=True`

If any check fails → return `None` (will reprocess)

### 8.3 Cascade Invalidation

**Trigger**: When `translator.py` re-runs (due to correction change or request_changes)

**Action in translator**:
```python
# In translator.py, after writing new translation:
from btcedu.core.reviewer import _mark_output_stale

# Mark downstream outputs stale
adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
if adapted_path.exists():
    _mark_output_stale(adapted_path, reason="translation_changed")
```

**Helper function** (add to `reviewer.py` if not already present):
```python
def _mark_output_stale(output_path: Path, reason: str = "upstream_change"):
    """Create .stale marker file for cascade invalidation."""
    stale_marker = output_path.parent / (output_path.name + ".stale")
    stale_data = {
        "invalidated_at": datetime.utcnow().isoformat(),
        "reason": reason,
    }
    stale_marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
    logger.info(f"Marked stale: {output_path.name}")
```

**Detection in adapter**:
```python
# In _is_adaptation_current():
stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
if stale_marker.exists():
    logger.info("Adaptation marked stale (upstream change), will reprocess")
    stale_marker.unlink()  # Consume marker
    return None  # Not current
```

---

## 9. Test Plan

### 9.1 Unit Tests

File: `tests/test_adapter.py`

```python
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json

from btcedu.core.adapter import (
    adapt_script,
    compute_adaptation_diff,
    _is_adaptation_current,
    _split_prompt,
    _segment_text,
    _classify_adaptation,
)
from btcedu.models import Episode, EpisodeStatus, ReviewTask, ReviewStatus
from btcedu.services.claude_service import ClaudeResponse


# Fixtures

@pytest.fixture
def translated_episode(db_session, tmp_path):
    """Episode at TRANSLATED status with translation + German corrected files."""
    episode_id = "ep_test_adapt"

    # Create transcript files
    transcripts_dir = tmp_path / "transcripts" / episode_id
    transcripts_dir.mkdir(parents=True)

    translation_path = transcripts_dir / "transcript.tr.txt"
    translation_path.write_text(
        "Bitcoin, 2008 yılında Satoshi Nakamoto tarafından yaratıldı. "
        "BaFin yeni düzenlemeler yayınladı. Fiyat 50 EUR idi.",
        encoding="utf-8"
    )

    corrected_path = transcripts_dir / "transcript.corrected.de.txt"
    corrected_path.write_text(
        "Bitcoin wurde 2008 von Satoshi Nakamoto erschaffen. "
        "Die BaFin hat neue Regelungen veröffentlicht. Der Preis betrug 50 EUR.",
        encoding="utf-8"
    )

    # Create episode
    episode = Episode(
        episode_id=episode_id,
        title="Test Episode",
        url="https://example.com",
        status=EpisodeStatus.TRANSLATED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    return episode, translation_path, corrected_path


@pytest.fixture
def approved_correction_review(db_session, translated_episode):
    """Create approved Review Gate 1 (correction) for the episode."""
    episode, _, _ = translated_episode

    review = ReviewTask(
        episode_id=episode.episode_id,
        stage="correct",
        status=ReviewStatus.APPROVED,
        artifact_paths=json.dumps(["/path/to/corrected.txt"]),
    )
    db_session.add(review)
    db_session.commit()

    return review


# Tests

def test_adapt_script_basic(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test successful adaptation with T1/T2 tags."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    adapted_text = (
        "Bitcoin, 2008 yılında Satoshi Nakamoto tarafından yaratıldı. "
        "[T1: SPK (Sermaye Piyasası Kurulu)] yeni düzenlemeler yayınladı. "
        "Fiyat [T1: ~2.000 TL (yaklaşık 50 EUR)] idi."
    )

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text=adapted_text,
            input_tokens=200,
            output_tokens=150,
            cost_usd=0.03,
            model="claude-sonnet-4-20250514"
        )

        result = adapt_script(db_session, episode.episode_id, settings, force=False)

        assert result.episode_id == episode.episode_id
        assert Path(result.adapted_path).exists()
        assert Path(result.diff_path).exists()
        assert result.adaptation_count == 2
        assert result.tier1_count == 2
        assert result.tier2_count == 0
        assert result.cost_usd == 0.03
        assert not result.skipped

        # Verify episode status updated
        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.ADAPTED


def test_adapt_script_idempotent(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that second run skips (idempotent)."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    adapted_text = "Adapted text with [T1: test] tag."

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text=adapted_text, input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        # First run
        result1 = adapt_script(db_session, episode.episode_id, settings, force=False)
        assert not result1.skipped
        assert result1.cost_usd == 0.02

        # Second run (should skip)
        result2 = adapt_script(db_session, episode.episode_id, settings, force=False)
        assert result2.skipped
        assert result2.cost_usd == 0.02  # Cached cost
        assert mock_claude.call_count == 1  # Not called again


def test_adapt_script_force(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that --force re-processes."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Adapted", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        # First run
        result1 = adapt_script(db_session, episode.episode_id, settings, force=False)
        assert not result1.skipped

        # Second run with force=True
        result2 = adapt_script(db_session, episode.episode_id, settings, force=True)
        assert not result2.skipped
        assert mock_claude.call_count == 2  # Called twice


def test_adapt_script_blocks_without_review_approval(db_session, settings, translated_episode, tmp_path):
    """Test that adaptation raises error if Review Gate 1 not approved."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    # No approved review exists (fixture not used)

    with pytest.raises(ValueError, match="correction not approved"):
        adapt_script(db_session, episode.episode_id, settings, force=False)


def test_adapt_script_creates_provenance(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that provenance JSON is written."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Adapted [T1: test]", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        result = adapt_script(db_session, episode.episode_id, settings, force=False)

        provenance_path = Path(result.provenance_path)
        assert provenance_path.exists()

        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        assert provenance["stage"] == "adapt"
        assert provenance["episode_id"] == episode.episode_id
        assert "input_content_hashes" in provenance
        assert provenance["input_content_hashes"]["translation"]
        assert provenance["input_content_hashes"]["german"]
        assert provenance["cost_usd"] == 0.02


def test_adapt_script_updates_episode_status(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that episode status transitions TRANSLATED → ADAPTED."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    assert episode.status == EpisodeStatus.TRANSLATED

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Adapted", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        adapt_script(db_session, episode.episode_id, settings, force=False)

        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.ADAPTED


def test_adaptation_diff_parsing(db_session):
    """Test that diff correctly identifies T1/T2 adaptations."""
    translation = "BaFin yeni kurallar yayınladı. Fiyat 50 EUR idi."
    adapted = (
        "[T1: SPK (Sermaye Piyasası Kurulu)] yeni kurallar yayınladı. "
        "Fiyat [T1: ~2.000 TL (yaklaşık 50 EUR)] idi. "
        "[T2: kültürel uyarlama: 'Oktoberfest' → 'Ramazan festivali']"
    )

    diff = compute_adaptation_diff(translation, adapted, "ep_test")

    assert diff["summary"]["total_adaptations"] == 3
    assert diff["summary"]["tier1_count"] == 2
    assert diff["summary"]["tier2_count"] == 1

    # Check adaptations
    assert len(diff["adaptations"]) == 3
    assert diff["adaptations"][0]["tier"] == "T1"
    assert diff["adaptations"][2]["tier"] == "T2"


def test_adaptation_diff_categories(db_session):
    """Test that adaptations are correctly classified by category."""
    adapted = (
        "[T1: SPK (Sermaye Piyasası Kurulu)] "
        "[T1: ~2.000 TL] "
        "[T1: ton düzeltmesi] "
        "[T1: [kaldırıldı: Almanya'ya özgü yasal bilgi]] "
        "[T2: kültürel uyarlama: 'X' → 'Y']"
    )

    diff = compute_adaptation_diff("", adapted, "ep_test")

    by_cat = diff["summary"]["by_category"]
    assert by_cat.get("institution_replacement", 0) >= 1
    assert by_cat.get("currency_conversion", 0) >= 1
    assert by_cat.get("tone_adjustment", 0) >= 1
    assert by_cat.get("legal_removal", 0) >= 1
    assert by_cat.get("cultural_reference", 0) >= 1


def test_cascade_invalidation(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that .stale marker causes reprocessing."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Adapted", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        # First run
        result1 = adapt_script(db_session, episode.episode_id, settings, force=False)
        adapted_path = Path(result1.adapted_path)

        # Create stale marker (simulating upstream change)
        stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
        stale_marker.write_text(json.dumps({"reason": "translation_changed"}), encoding="utf-8")

        # Second run (should reprocess due to stale marker)
        result2 = adapt_script(db_session, episode.episode_id, settings, force=False)
        assert not result2.skipped  # Reprocessed
        assert not stale_marker.exists()  # Marker consumed
        assert mock_claude.call_count == 2


def test_reviewer_feedback_injection(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test that request_changes notes are injected into re-adaptation prompt."""
    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    # Create initial adapted output
    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Initial adaptation", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )
        result1 = adapt_script(db_session, episode.episode_id, settings, force=False)

    # Create Review Gate 2 and request changes
    review = ReviewTask(
        episode_id=episode.episode_id,
        stage="adapt",
        status=ReviewStatus.CHANGES_REQUESTED,
        artifact_paths=json.dumps([result1.adapted_path]),
        reviewer_notes="Lütfen 'BaFin' yerine 'SPK' kullanın.",
    )
    db_session.add(review)
    db_session.commit()

    # Revert episode status (simulate request_changes)
    episode.status = EpisodeStatus.TRANSLATED
    db_session.commit()

    # Re-run adaptation
    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Revised adaptation with SPK", input_tokens=120, output_tokens=90, cost_usd=0.025, model="claude-sonnet-4-20250514"
        )

        result2 = adapt_script(db_session, episode.episode_id, settings, force=False)

        # Verify reviewer notes were injected into prompt
        call_args = mock_claude.call_args
        user_message = call_args[0][1]  # Second arg to call_claude
        assert "Lütfen 'BaFin' yerine 'SPK' kullanın" in user_message


def test_split_prompt():
    """Test prompt splitting at '# Input' marker."""
    template = """
# System

System instructions here.

# Input

{{ translation }}

{{ original_german }}
"""
    system, user = _split_prompt(template)

    assert "System instructions" in system
    assert "# Input" in user
    assert "{{ translation }}" in user


def test_segment_text():
    """Test text segmentation at paragraph boundaries."""
    text = "Para 1.\n\nPara 2.\n\nPara 3."
    segments = _segment_text(text, max_chars=15)

    # Each paragraph becomes a segment (< 15 chars each)
    assert len(segments) >= 3


def test_classify_adaptation():
    """Test adaptation category classification."""
    from btcedu.core.adapter import _classify_adaptation

    assert _classify_adaptation("BaFin → SPK") == "institution_replacement"
    assert _classify_adaptation("50 EUR → 2000 TL") == "currency_conversion"
    assert _classify_adaptation("ton düzeltmesi") == "tone_adjustment"
    assert _classify_adaptation("[kaldırıldı: yasal bilgi]") == "legal_removal"
    assert _classify_adaptation("kültürel uyarlama: X → Y") == "cultural_reference"
    assert _classify_adaptation("düzenleme bilgisi") == "regulatory_context"
    assert _classify_adaptation("something else") == "other"
```

### 9.2 Integration Tests

These test the full pipeline flow (mock Claude API):

```python
def test_full_adapt_pipeline(db_session, settings, translated_episode, approved_correction_review, tmp_path):
    """Test complete adaptation pipeline: adapt → create Review Gate 2 → approve → proceed."""
    from btcedu.core.pipeline import run_episode_pipeline, _run_stage
    from btcedu.core.reviewer import approve_review, has_approved_review

    episode, translation_path, corrected_path = translated_episode
    settings.transcripts_dir = translation_path.parent.parent
    settings.outputs_dir = tmp_path / "outputs"

    with patch("btcedu.services.claude_service.call_claude") as mock_claude:
        mock_claude.return_value = ClaudeResponse(
            text="Adapted [T1: test]", input_tokens=100, output_tokens=80, cost_usd=0.02, model="claude-sonnet-4-20250514"
        )

        # Run adaptation stage
        stage_result = _run_stage(db_session, episode, "adapt", settings, force=False)
        assert stage_result.status == "success"

        # Episode now ADAPTED
        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.ADAPTED

        # Run Review Gate 2 stage (should create ReviewTask)
        stage_result = _run_stage(db_session, episode, "review_gate_2", settings, force=False)
        assert stage_result.status == "review_pending"

        # Verify ReviewTask created
        review = db_session.query(ReviewTask).filter_by(
            episode_id=episode.episode_id, stage="adapt"
        ).first()
        assert review is not None
        assert review.status == ReviewStatus.PENDING

        # Approve the review
        approve_review(db_session, review.id, notes="Looks good!")

        # Verify approval
        assert has_approved_review(db_session, episode.episode_id, "adapt")

        # Re-run Review Gate 2 (should now pass)
        stage_result = _run_stage(db_session, episode, "review_gate_2", settings, force=False)
        assert stage_result.status == "success"
```

---

## 10. Implementation Order

Execute in this sequence:

1. **Create prompt template** (`btcedu/prompts/templates/adapt.md`)
   - Write complete YAML frontmatter + tiered rules + hard constraints
   - Verify syntax (no unclosed braces, valid YAML)

2. **Implement core adapter module** (`btcedu/core/adapter.py`)
   - Start with `adapt_script()` main function (copy translator pattern)
   - Add helper functions: `_is_adaptation_current()`, `_split_prompt()`, `_segment_text()`
   - Implement `compute_adaptation_diff()` with tag parsing + classification
   - Add `AdaptationResult` dataclass

3. **Add CLI command** (`btcedu/cli.py`)
   - Add `adapt` command with --force, --dry-run
   - Test: `btcedu adapt --help`

4. **Integrate into pipeline** (`btcedu/core/pipeline.py`)
   - Add `("adapt", EpisodeStatus.TRANSLATED)` to `_V2_STAGES`
   - Add `("review_gate_2", EpisodeStatus.ADAPTED)` to `_V2_STAGES`
   - Add `adapt` branch to `_run_stage()`
   - Add `review_gate_2` branch to `_run_stage()` (mirror review_gate_1)

5. **Extend review system** (`btcedu/core/reviewer.py`, if needed)
   - Add `get_adaptation_review_data()` if not already generic
   - Add `_mark_output_stale()` if not already present
   - Verify existing functions support stage="adapt"

6. **Update web API** (`btcedu/web/api.py`, if needed)
   - Add `/reviews/<id>/adaptation` endpoint (if generic endpoint insufficient)

7. **Extend review UI templates** (`btcedu/web/templates/review_detail.html`)
   - Add `{% elif review.stage == 'adapt' %}` branch
   - Implement adaptation diff viewer (side-by-side, adaptations list)
   - Add CSS for T1/T2 color-coding

8. **Write unit tests** (`tests/test_adapter.py`)
   - Create fixtures (translated_episode, approved_correction_review)
   - Test: basic adaptation, idempotency, force, blocks without approval, provenance, status update, diff parsing, categories, cascade, feedback injection

9. **Write integration tests** (`tests/test_adapter.py`)
   - Test full pipeline flow: adapt → Review Gate 2 → approve → proceed

10. **Manual verification**:
    - Create test episode at TRANSLATED status
    - Run `btcedu adapt --episode-id <id>`
    - Verify adapted output, diff JSON, provenance
    - Check dashboard review queue (pending adaptation review)
    - Open review detail, verify diff display
    - Approve review
    - Verify pipeline can proceed

11. **Cascade invalidation test**:
    - Re-run translation stage (simulate correction change)
    - Verify .stale marker created on adapted output
    - Re-run adaptation, verify reprocessing

12. **Reviewer feedback test**:
    - Request changes on adaptation review with notes
    - Re-run adaptation
    - Verify notes injected into prompt (check dry-run JSON)

---

## 11. Definition of Done

Sprint 5 is complete when ALL of the following are true:

- [ ] `btcedu/prompts/templates/adapt.md` created with complete tiered rules + hard constraints
- [ ] `btcedu/core/adapter.py` implemented with all functions
- [ ] `adapt_script()` successfully adapts Turkish translation with T1/T2 tagging
- [ ] Adaptation diff JSON correctly parsed and classified
- [ ] CLI command `btcedu adapt --episode-id <id>` works
- [ ] CLI command `btcedu adapt --episode-id <id> --force` re-processes
- [ ] CLI command `btcedu adapt --episode-id <id> --dry-run` writes request JSON
- [ ] Pipeline integration: ADAPT stage added to `_V2_STAGES`
- [ ] Pipeline integration: Review Gate 2 added to `_V2_STAGES`
- [ ] Review Gate 2 creates ReviewTask with stage="adapt"
- [ ] Pipeline pauses at Review Gate 2 until approval
- [ ] Episode status transitions: TRANSLATED → ADAPTED
- [ ] Provenance JSON written with input hashes, prompt hash, cost, adaptation summary
- [ ] Idempotency: second run without --force skips (checks input + prompt hashes + stale marker)
- [ ] Cascade invalidation: .stale marker created when translation changes
- [ ] Reviewer feedback injection: request_changes notes injected into re-adaptation prompt
- [ ] Dashboard review queue shows adaptation reviews
- [ ] Dashboard review detail page renders adaptation diff (side-by-side, T1/T2 highlighting)
- [ ] Approve/reject/request-changes buttons work for adaptation reviews
- [ ] All unit tests pass (`pytest tests/test_adapter.py -v`)
- [ ] All integration tests pass
- [ ] Manual test: adapt real episode, review in dashboard, approve, pipeline proceeds
- [ ] Existing tests still pass (no regressions in correction/translation/review)
- [ ] `btcedu status` shows episodes at ADAPTED status
- [ ] v1 pipeline unaffected (episodes with pipeline_version=1 skip ADAPT stage)
- [ ] No errors in logs during adaptation
- [ ] Cost tracking accurate (adaptation cost recorded in PipelineRun)

---

## 12. Non-Goals

Explicitly OUT OF SCOPE for Sprint 5:

- **Chapterization** (Sprint 6) — do NOT implement `chapterize_script()` or `chapters.json` generation
- **Image generation** (Sprint 6-7) — do NOT implement image prompt generation or DALL-E integration
- **TTS** (Sprint 8) — do NOT implement ElevenLabs integration or audio generation
- **Video rendering** (Sprint 9-10) — do NOT implement ffmpeg integration or video assembly
- **YouTube publishing** (Sprint 11) — do NOT implement YouTube API integration
- **Review Gate 3** (Sprint 9) — do NOT implement video review workflow
- **Auto-approve rules** — ALL adaptations require human review (no auto-approval based on T1/T2 tier)
- **Institution mapping database** — use prompt-based replacements (no lookup tables)
- **Adaptation quality metrics** — no automated quality scoring (rely on human review)
- **Multi-language support** — only German→Turkish→Adapted Turkish
- **Translation memory** — no glossary or translation memory system
- **Dashboard enhancements beyond adaptation diff viewer** — no new navigation, no redesign
- **Modification of existing stages** — correction/translation/review systems remain unchanged
- **Prompt A/B testing UI** — prompt versioning exists, but no comparison UI (future sprint)
- **Cost optimization** — no segment caching, no prompt compression (future optimization)
- **Performance optimization** — single-threaded, sequential processing (sufficient for v1)

---

## 13. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Adaptation prompt too permissive** → Hallucinated Turkish laws | Medium | High | Hard constraints in prompt + Tier 2 review gate + manual review of first 10 episodes |
| **T1/T2 tagging inconsistent** → Diff parsing fails | Medium | Medium | Strict output format + regex validation + fallback to "other" category |
| **Cultural adaptations offensive or inaccurate** | Low | High | Tier 2 review gate + editorial neutrality constraints + content owner approval |
| **Adaptation too aggressive** → Loses original meaning | Medium | High | "Adapt LESS rather than MORE" principle in prompt + side-by-side review UI |
| **Cascade invalidation breaks** → Stale outputs used | Low | Medium | .stale marker tests + manual verification + provenance hash checks |
| **Review UI doesn't distinguish T1/T2** → Editorial adaptations missed | Medium | High | Color-coded T1 (green) vs T2 (orange) + filter buttons + summary stats |
| **Reviewer feedback not injected** → Re-adaptation ignores notes | Low | High | Unit test for feedback injection + manual verification |
| **Pipeline blocks forever on pending review** → Bottleneck | Low | Medium | Review queue badge + email notifications (future) + manual monitoring |
| **Prompt version change invalidates all episodes** → Mass reprocessing | Low | Medium | Controlled prompt updates + cost cap enforcement + batch reprocessing script |

---

## 14. Assumptions

**[ASSUMPTION 1]**: The existing review detail template (`review_detail.html`) is modular enough to add conditional rendering for `review.stage == "adapt"`. If the template is monolithic, minor refactoring may be needed (out of scope, will be addressed if encountered).

**[ASSUMPTION 2]**: For multi-segment adaptations, we pass the full German corrected transcript as reference for ALL segments (vs. aligned German segments). This simplification is acceptable for v1. A more sophisticated segment alignment can be added in future if needed.

**[ASSUMPTION 3]**: The existing `/reviews/<id>` API endpoint generically loads diff_data + artifact text. If it doesn't, we'll add a new `/reviews/<id>/adaptation` endpoint (minimal work).

**[ASSUMPTION 4]**: The `call_claude()` function in `claude_service.py` supports `max_tokens=12000` (adapt prompt may need more tokens than correction/translation). Verify Claude Sonnet 4's token limit (likely 200k input, 4k-8k output). If needed, increase `max_tokens` in adapt.md frontmatter.

**[ASSUMPTION 5]**: The existing `_segment_text()` logic from translator can be reused as-is for adapter. Turkish text segmentation follows same paragraph/sentence boundaries.

**[ASSUMPTION 6]**: The adaptation diff viewer doesn't need to show WHICH PART of the script was adapted (just the list of adaptations). Full text comparison is side-by-side. If reviewers need inline highlighting (like GitHub PR diffs), that's a future enhancement.

**[ASSUMPTION 7]**: The `pipeline_version=2` check is already implemented in `run_episode_pipeline()`. All v2-specific stages (CORRECT, TRANSLATE, ADAPT) only run for episodes with `pipeline_version=2`.

**[ASSUMPTION 8]**: Adaptation reviews can be batched (reviewer can approve multiple episodes in one session). No special "batch approve" UI needed for Sprint 5; reviewers process one at a time.

**[ASSUMPTION 9]**: The adaptation prompt's tiered rules (T1/T2) are sufficient for the initial implementation. Prompt iteration will happen post-Sprint 5 based on real episode feedback.

**[ASSUMPTION 10]**: The `get_latest_reviewer_feedback()` function in `reviewer.py` already exists and works generically for any stage. If not, it will be added as part of Sprint 5.

---

## 15. Success Metrics

Sprint 5 is successful if:

1. **Functional**: At least 3 real episodes successfully adapted with T1/T2 tags, reviewed, and approved
2. **Quality**: Manual review of first 5 adapted episodes shows:
   - No hallucinated Turkish laws or regulations
   - All Bitcoin technical facts preserved
   - Appropriate tone (conversational, Turkish influencer style)
   - Cultural adaptations are reasonable and tagged T2
3. **Cost**: Adaptation cost per episode < $0.25 (Claude Sonnet 4 @ 12k chars input, 12k output ≈ $0.18)
4. **Performance**: Adaptation completes in < 30 seconds per episode (15k char average)
5. **Reliability**: Idempotency works (second run skips), cascade invalidation works (stale marker consumed)
6. **Usability**: Content owner can review adaptation diff in dashboard and approve/reject/request-changes without confusion

---

## 16. Next Steps (Post-Sprint 5)

After Sprint 5 completion:

1. **Collect real-world feedback**: Review first 10-20 adapted episodes, identify prompt improvements
2. **Iterate on adaptation prompt**: Based on review feedback, tune T1/T2 rules, add examples
3. **Sprint 6**: Implement CHAPTERIZE stage (production JSON with script + visuals + timing)
4. **Monitor adaptation quality**: Track tier2_count per episode, identify patterns in editorial adaptations
5. **Optimize prompt if needed**: If too many T2 adaptations (> 30% of total), tighten rules
6. **Document adaptation guidelines**: Create internal guide for reviewers on what to approve/reject

---

**End of Sprint 5 Implementation Plan**

This plan is implementation-ready. All file-level changes, function signatures, data structures, and test plans are specified. Follow the implementation order strictly for smooth execution.
