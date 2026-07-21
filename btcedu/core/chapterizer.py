"""Chapterization: Transform adapted script into structured production JSON."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import json_repair
from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.services.claude_service import ClaudeResponse, call_claude

logger = logging.getLogger(__name__)

# Texts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 15_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NarrationLockError(ValueError):
    """Raised when chapterization alters the approved narration content."""


def _narration_lock_diagnostic_path(settings: Settings, episode_id: str) -> Path:
    return Path(settings.outputs_dir) / episode_id / "provenance" / "chapterize_narration_lock.json"


def _write_narration_lock_diagnostic(
    settings: Settings,
    episode_id: str,
    result,
    approved_sha: str,
    composed_sha: str,
) -> None:
    """Persist a RED diagnostic describing how narration diverged."""
    path = _narration_lock_diagnostic_path(settings, episode_id)
    payload = {
        "stage": "chapterize",
        "episode_id": episode_id,
        "status": "red",
        "check": "narration_lock",
        "timestamp": _utcnow().isoformat(),
        "matches": False,
        "approved_narration_sha256": approved_sha,
        "composed_narration_sha256": composed_sha,
        "summary": result.summary(),
        "first_divergence_index": result.first_divergence_index,
        "approved_excerpt": result.approved_excerpt,
        "composed_excerpt": result.composed_excerpt,
        "added_numbers": result.added_numbers or [],
        "removed_numbers": result.removed_numbers or [],
        "added_names": result.added_names or [],
        "removed_names": result.removed_names or [],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Could not write narration lock diagnostic for %s", episode_id)


def _enforce_narration_lock(
    settings: Settings,
    episode_id: str,
    final_chapter_doc: ChapterDocument,
) -> tuple[str | None, str | None]:
    """Fail closed when chapters are not a faithful partition of approved narration.

    Chapterization may create chapter boundaries, visuals, overlays, prompts and
    metadata (titles/notes), but the concatenation of chapter narration — in
    chapter order — must equal the approved narrative after the narrow technical
    normalizations defined in :mod:`btcedu.core.narration_lock`.

    The lock is only enforced for profiles that carry a translation quality gate
    (news / story mode). Legacy profiles without a gate (e.g. the Bitcoin podcast
    hook/intro/outro flow) have no approved-narration artifact and are unaffected.

    Returns ``(approved_sha, composed_sha)`` for provenance. Raises
    :class:`NarrationLockError` on an unauthorized change; a RED diagnostic is
    written and the caller must not write chapters or advance status.
    """
    import hashlib

    from btcedu.core.narration_lock import (
        check_narration_lock,
        compose_chapter_narration,
        normalize_narration_text,
        restore_minor_narration_drift,
        restore_truncated_narration_suffix,
    )
    from btcedu.core.qa_reviewer import canonical_narration, load_quality_gate

    gate = load_quality_gate(settings, episode_id)
    if gate is None:
        # No approved-narration lock exists for this profile (legacy). Skip.
        return None, None

    _, approved_text = canonical_narration(settings, episode_id)
    if not approved_text.strip():
        return None, None

    composed_text = compose_chapter_narration(final_chapter_doc.chapters)
    result = check_narration_lock(approved_text, composed_text)
    if not result.matches:
        repair = None
        if restore_minor_narration_drift(approved_text, final_chapter_doc.chapters):
            repair = "minor character drift"
        elif restore_truncated_narration_suffix(
            approved_text,
            final_chapter_doc.chapters,
        ):
            repair = "truncated final suffix"
        if repair:
            logger.warning(
                "Restored %s from approved narration for %s",
                repair,
                episode_id,
            )
            for chapter in final_chapter_doc.chapters:
                word_count = len(chapter.narration.text.split())
                chapter.narration.word_count = word_count
                chapter.narration.estimated_duration_seconds = _compute_duration_estimate(
                    word_count
                )
            final_chapter_doc.estimated_duration_seconds = sum(
                chapter.narration.estimated_duration_seconds
                for chapter in final_chapter_doc.chapters
            )
            composed_text = compose_chapter_narration(final_chapter_doc.chapters)
            result = check_narration_lock(approved_text, composed_text)

    approved_sha = hashlib.sha256(
        normalize_narration_text(approved_text).encode("utf-8")
    ).hexdigest()
    composed_sha = hashlib.sha256(
        normalize_narration_text(composed_text).encode("utf-8")
    ).hexdigest()

    if not result.matches:
        _write_narration_lock_diagnostic(settings, episode_id, result, approved_sha, composed_sha)
        logger.error("Narration lock FAILED for %s: %s", episode_id, result.summary())
        raise NarrationLockError(
            f"Episode {episode_id} chapterization altered the approved narration "
            f"({result.summary()}). No chapters were written."
        )

    logger.info("Narration lock passed for %s (%s)", episode_id, approved_sha[:12])
    return approved_sha, composed_sha


def _enforce_translation_quality_gate(
    session: Session,
    episode_id: str,
    settings: Settings,
) -> None:
    """Block chapterization when a non-green / stale translation quality gate exists.

    Backward-compatible: episodes without a ``translation_quality_gate.json``
    (legacy / podcast) are unaffected. When a gate exists it must be GREEN for the
    exact current narration hash, or carry an artifact-bound approved
    ``translation_qa`` review. Force does not bypass this factual gate.
    """
    from btcedu.core.qa_reviewer import (
        gate_review_artifacts,
        load_quality_gate,
        narration_sha256,
    )

    gate_required = False
    try:
        from btcedu.profiles import get_registry

        episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        profile_name = getattr(episode, "content_profile", None) if episode else None
        if profile_name:
            profile = get_registry(settings).get(profile_name)
            gate_required = bool(getattr(settings, "qa_review_enabled", False)) and isinstance(
                profile.stage_config.get("qa"), dict
            )
    except Exception:  # noqa: BLE001
        gate_required = False

    gate = load_quality_gate(settings, episode_id, strict=True)
    if gate is None:
        gate_path = Path(settings.outputs_dir) / episode_id / "translation_quality_gate.json"
        if not gate_path.exists() and not gate_required:
            return
        raise ValueError(
            f"Episode {episode_id} requires a valid translation quality gate before chapterization."
        )

    from btcedu.core.reviewer import has_approved_review_for_artifacts

    artifacts = gate_review_artifacts(settings, episode_id)
    if has_approved_review_for_artifacts(session, episode_id, "translation_qa", artifacts):
        return

    current_hash = narration_sha256(settings, episode_id)
    approved_hash = gate.get("narration_sha256")
    if gate.get("decision") == "green" and approved_hash and approved_hash == current_hash:
        return

    hash_matches = bool(approved_hash) and approved_hash == current_hash
    raise ValueError(
        f"Episode {episode_id} translation quality gate is "
        f"'{gate.get('decision', 'unknown')}' (narration_match={hash_matches}). "
        "Chapterization is blocked until the gate is GREEN for the current narration "
        "or an artifact-bound translation_qa review is approved."
    )


@dataclass
class ChapterizationResult:
    """Summary of chapterization operation for one episode."""

    episode_id: str
    chapters_path: str
    provenance_path: str
    chapter_count: int = 0
    estimated_duration_seconds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    segments_processed: int = 1
    skipped: bool = False


def chapterize_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> ChapterizationResult:
    """Chapterize adapted script into structured production JSON.

    Reads the adapted Turkish script and transforms it into a structured
    chapter document with narration, visuals, overlays, and timing guidance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-chapterize even if output exists.

    Returns:
        ChapterizationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
        FileNotFoundError: If adapted script missing.
        ValidationError: If LLM produces invalid JSON.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Phase 7 factual gate: when a translation quality gate exists, chapterization
    # is only allowed if it is GREEN for the current narration, or an artifact-bound
    # translation_qa review is approved. This runs even with force so a stale or
    # non-green factual gate cannot be silently bypassed.
    _enforce_translation_quality_gate(session, episode_id, settings)

    # Determine if this is a story-mode episode (tagesschau news profiles)
    # Story mode: stories_translated.json exists AND adapt was skipped (no adapted script).
    # When adapt ran (script.adapted.tr.md exists), prefer that clean tr-only narrative:
    # feeding the raw 30k+ char stories JSON (which also carries text_de) forces
    # char-based segmentation that cuts across stories and produces duplicated chapters.
    stories_translated_path = Path(settings.outputs_dir) / episode_id / "stories_translated.json"
    adapted_script_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    use_story_mode = stories_translated_path.exists() and not adapted_script_path.exists()

    # Allow both ADAPTED, TRANSLATED (story mode), and CHAPTERIZED status
    allowed_statuses = {EpisodeStatus.ADAPTED, EpisodeStatus.CHAPTERIZED}
    if use_story_mode:
        allowed_statuses.add(EpisodeStatus.TRANSLATED)
    if episode.status not in allowed_statuses and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'adapted', 'translated' (story mode), or 'chapterized'. "
            "Use --force to override."
        )

    # Check Review Gate 2 approval (adaptation must be approved) — only for adapted path
    if episode.status == EpisodeStatus.ADAPTED and not force and not use_story_mode:
        from btcedu.core.reviewer import has_pending_review, profile_auto_approves

        # Profiles with auto_approve_reviews run fully automatically — skip the gate.
        if not profile_auto_approves(settings, episode):
            # Check if there's a pending review for adaptation
            if has_pending_review(session, episode_id):
                raise ValueError(
                    f"Episode {episode_id} has pending review. "
                    "Chapterization cannot proceed until reviews are resolved."
                )

            # Verify adaptation was approved
            approved_adapt = (
                session.query(ReviewTask)
                .filter(
                    ReviewTask.episode_id == episode_id,
                    ReviewTask.stage == "adapt",
                    ReviewTask.status == ReviewStatus.APPROVED.value,
                )
                .first()
            )

            if not approved_adapt:
                raise ValueError(
                    f"Episode {episode_id} adaptation has not been approved. "
                    "Chapterization cannot proceed until Review Gate 2 is approved."
                )

    # Resolve input path: stories mode vs adapted script mode
    if use_story_mode:
        # Story mode: check for reviewed translation sidecar first
        stories_reviewed_path = (
            Path(settings.outputs_dir) / episode_id / "review" / "stories_translated.reviewed.json"
        )
        if stories_reviewed_path.exists():
            adapted_path = stories_reviewed_path
            logger.info("Using reviewed translation sidecar for episode %s", episode_id)
        else:
            adapted_path = stories_translated_path
            logger.info(
                "Using stories_translated.json (story mode) for chapterization of %s", episode_id
            )
    else:
        adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
        if not adapted_path.exists():
            raise FileNotFoundError(
                f"Adapted script not found for episode {episode_id}: {adapted_path}"
            )

        # Check for reviewed sidecar (Phase 5 granular review output)
        reviewed_path = (
            Path(settings.outputs_dir) / episode_id / "review" / "script.adapted.reviewed.tr.md"
        )
        if reviewed_path.exists():
            adapted_path = reviewed_path
            logger.info("Using reviewed adaptation sidecar for episode %s", episode_id)

    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "chapterize_provenance.json"
    )

    # Resolve profile namespace for prompt namespacing
    content_profile = getattr(episode, "content_profile", None)
    profile_obj = None
    try:
        from btcedu.profiles import get_registry as get_profile_registry

        pr = get_profile_registry(settings)
        profile_obj = pr.get(content_profile) if content_profile else None
        profile_namespace = getattr(profile_obj, "prompt_namespace", None) if profile_obj else None
    except Exception:
        profile_namespace = None

    # Load and register prompt via PromptRegistry (with profile-namespaced fallback)
    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("chapterize.md", profile=profile_namespace)
    prompt_name = "chapterize"
    if profile_namespace and (TEMPLATES_DIR / profile_namespace / "chapterize.md").exists():
        prompt_name = f"{profile_namespace}/chapterize"
    prompt_version = registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Compute input content hash for idempotency
    adapted_text = adapted_path.read_text(encoding="utf-8")
    adapted_hash = hashlib.sha256(adapted_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_chapterization_current(
        chapters_path, provenance_path, adapted_hash, prompt_content_hash
    ):
        logger.info("Chapterization is current for %s (use --force to re-chapterize)", episode_id)
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

        return ChapterizationResult(
            episode_id=episode_id,
            chapters_path=str(chapters_path),
            provenance_path=str(provenance_path),
            chapter_count=existing_provenance.get("chapter_count", 0),
            estimated_duration_seconds=existing_provenance.get("estimated_duration_seconds", 0),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.CHAPTERIZE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject chapterize stage_config into template (hook, chapter length rules)
        chapterize_cfg = (
            profile_obj.stage_config.get("chapterize", {}) if profile_obj else {}
        ) or {}
        hook_max_seconds = chapterize_cfg.get("hook_max_seconds", 15)
        min_chapter_seconds = chapterize_cfg.get("min_chapter_seconds", 30)
        max_chapter_seconds = chapterize_cfg.get("max_chapter_seconds", 120)
        template_body = (
            template_body.replace("{{ hook_max_seconds }}", str(hook_max_seconds))
            .replace("{{ min_chapter_seconds }}", str(min_chapter_seconds))
            .replace("{{ max_chapter_seconds }}", str(max_chapter_seconds))
        )

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Segment text if needed (unlikely for adapted scripts, but handle it)
        segments = _segment_script(adapted_text)

        # Process each segment
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        all_chapters = []

        for i, segment in enumerate(segments):
            user_message = user_template.replace("{{episode_id}}", episode_id).replace(
                "{{adapted_script}}", segment
            )

            # Dry-run path
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_chapterize_{i}.json"
                if settings.dry_run
                else None
            )

            response: ClaudeResponse = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                dry_run_path=dry_run_path,
                max_tokens=16384,  # chapter JSON needs much more than default 4096
                json_mode=True,
            )

            # Parse JSON response (retry once if the model emitted invalid JSON)
            try:
                chapter_data = _parse_json_response(response.text, episode_id, segment, settings)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Chapterization output was not valid JSON (%s) — retrying with correction", e
                )
                chapter_data = _retry_with_json_correction(
                    e, response.text, episode_id, segment, system_prompt, settings
                )

            # Fix common LLM output issues before validation
            chapter_data = _fix_chapter_data(chapter_data, episode_id)

            # Validate with Pydantic
            try:
                chapter_doc = ChapterDocument.model_validate(chapter_data)
            except ValidationError as e:
                logger.warning("Chapterization output failed validation: %s", e)
                # Retry once with corrective prompt
                chapter_data = _retry_with_correction(
                    e, episode_id, segment, system_prompt, settings
                )
                chapter_data = _fix_chapter_data(chapter_data, episode_id)
                # Validate retry
                chapter_doc = ChapterDocument.model_validate(chapter_data)

            # For multi-segment: merge chapters
            if len(segments) > 1:
                all_chapters.extend(chapter_doc.chapters)
            else:
                all_chapters = chapter_doc.chapters

            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd

            logger.info(
                "Segment %d/%d: %d chapters, %d in, %d out, $%.4f",
                i + 1,
                len(segments),
                len(chapter_doc.chapters),
                response.input_tokens,
                response.output_tokens,
                response.cost_usd,
            )

        # Reassemble if multi-segment: re-number chapters sequentially
        if len(segments) > 1:
            for idx, ch in enumerate(all_chapters):
                ch.order = idx + 1
                ch.chapter_id = f"ch{ch.order:02d}"

            # Build final document
            total_duration = sum(ch.narration.estimated_duration_seconds for ch in all_chapters)
            final_chapter_doc = ChapterDocument(
                schema_version="1.0",
                episode_id=episode_id,
                title=chapter_doc.title,  # Use title from first segment
                total_chapters=len(all_chapters),
                estimated_duration_seconds=total_duration,
                chapters=all_chapters,
            )
        else:
            final_chapter_doc = chapter_doc

        # Merge chapters shorter than min_chapter_seconds (LLM constraint is unreliable).
        merged_chapters = _merge_short_chapters(
            list(final_chapter_doc.chapters), min_chapter_seconds
        )
        if len(merged_chapters) != len(final_chapter_doc.chapters):
            logger.info(
                "Merged %d short chapters (<%ds) into neighbors",
                len(final_chapter_doc.chapters) - len(merged_chapters),
                min_chapter_seconds,
            )
            for idx, ch in enumerate(merged_chapters):
                ch.order = idx + 1
                ch.chapter_id = f"ch{ch.order:02d}"
            final_chapter_doc.chapters = merged_chapters
            final_chapter_doc.total_chapters = len(merged_chapters)
            final_chapter_doc.estimated_duration_seconds = sum(
                c.narration.estimated_duration_seconds for c in merged_chapters
            )

        # Validate duration estimates (recalculate from word count)
        for ch in final_chapter_doc.chapters:
            actual_word_count = len(ch.narration.text.split())
            expected_duration = _compute_duration_estimate(actual_word_count)
            # If LLM's estimate is off by >20%, log warning
            if abs(ch.narration.estimated_duration_seconds - expected_duration) > (
                expected_duration * 0.2
            ):
                logger.warning(
                    "Chapter %s duration mismatch: LLM=%ds, expected=%ds (word_count=%d)",
                    ch.chapter_id,
                    ch.narration.estimated_duration_seconds,
                    expected_duration,
                    actual_word_count,
                )

        logger.info(
            "Chapterization complete: %d chapters, ~%ds",
            final_chapter_doc.total_chapters,
            final_chapter_doc.estimated_duration_seconds,
        )

        # Phase 8 requirement A: prove the chapters are a faithful partition of
        # the approved narration BEFORE anything is written or status advances.
        # Raises NarrationLockError (handled by the except below) on any change.
        approved_narration_sha, composed_narration_sha = _enforce_narration_lock(
            settings, episode_id, final_chapter_doc
        )

        # Capture the previous per-component hashes (before overwriting provenance)
        # so downstream invalidation can be selective (Phase 8 requirement D).
        previous_components = _load_previous_component_hashes(provenance_path)
        component_hashes = _chapter_component_hashes(final_chapter_doc)

        # Write chapters.json
        chapters_path.parent.mkdir(parents=True, exist_ok=True)
        chapters_json = final_chapter_doc.model_dump(mode="json", by_alias=True)
        chapters_path.write_text(
            json.dumps(chapters_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "chapterize",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "chapterize",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(adapted_path)],
            "input_content_hash": adapted_hash,
            "output_files": [str(chapters_path)],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": len(segments),
            "chapter_count": final_chapter_doc.total_chapters,
            "estimated_duration_seconds": final_chapter_doc.estimated_duration_seconds,
            "approved_narration_sha256": approved_narration_sha,
            "composed_narration_sha256": composed_narration_sha,
            "narration_locked": approved_narration_sha is not None,
            "component_hashes": component_hashes,
            "schema_version": "1.0",
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="chapterize",
            file_path=str(chapters_path),
            model=settings.claude_model,
            prompt_hash=prompt_content_hash,
            retrieval_snapshot_path=None,
        )
        session.add(artifact)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost

        # Update Episode
        episode.status = EpisodeStatus.CHAPTERIZED
        episode.error_message = None
        session.commit()

        # Selectively mark downstream stages stale based on WHAT changed
        # (Phase 8 requirement D): narration -> TTS+render, visual/prompt ->
        # images+render, overlay -> render, structure -> images+TTS+render.
        _mark_downstream_stale(episode_id, settings, previous_components, component_hashes)

        logger.info(
            "Chapterized %s (%d chapters, ~%ds, $%.4f)",
            episode_id,
            final_chapter_doc.total_chapters,
            final_chapter_doc.estimated_duration_seconds,
            total_cost,
        )

        return ChapterizationResult(
            episode_id=episode_id,
            chapters_path=str(chapters_path),
            provenance_path=str(provenance_path),
            chapter_count=final_chapter_doc.total_chapters,
            estimated_duration_seconds=final_chapter_doc.estimated_duration_seconds,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            segments_processed=len(segments),
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = f"Chapterization failed: {e}"
        session.commit()
        logger.error("Chapterization failed for %s: %s", episode_id, e)
        raise


def _is_chapterization_current(
    chapters_path: Path,
    provenance_path: Path,
    adapted_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing chapterization is still valid.

    Returns True (skip) if ALL of:
    1. chapters_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not chapters_path.exists():
        return False

    # Check for stale marker
    stale_marker = chapters_path.parent / (chapters_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Chapterization marked stale (upstream change), will reprocess")
        stale_marker.unlink()  # Consume marker
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Provenance file corrupt or missing, will reprocess")
        return False

    if provenance.get("prompt_hash") != prompt_content_hash:
        logger.info("Prompt hash mismatch (prompt was updated)")
        return False

    if provenance.get("input_content_hash") != adapted_hash:
        logger.info("Adapted script content hash mismatch (adapted script was updated)")
        return False

    return True


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split rendered template into system prompt and user message.

    The template is split at the '# Input' header.
    Everything before it becomes the system prompt.
    Everything from '# Input' onward becomes the user message.
    """
    marker = "# Input"
    idx = template_body.find(marker)
    if idx == -1:
        # Fallback: use empty system prompt, entire body is user message
        return ("", template_body)
    system = template_body[:idx].strip()
    user = template_body[idx:].strip()
    return (system, user)


def _segment_script(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split script into segments at paragraph breaks.

    If the text is shorter than the limit, returns a single-element list.
    Splits at double-newline paragraph boundaries to preserve context.
    If a single paragraph exceeds the limit, splits at sentence boundaries.

    Args:
        text: The full text.
        limit: Maximum characters per segment.

    Returns:
        List of text segments (non-empty).
    """
    # If text is short enough, return as single segment
    if len(text) <= limit:
        return [text]

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

        # If single paragraph exceeds limit, split by sentences
        if para_len > limit:
            # Flush current segment
            if current_segment:
                segments.append("\n\n".join(current_segment))
                current_segment = []
                current_length = 0

            # Split long paragraph by sentences (approximate with ". ")
            sentences = para.split(". ")

            # If no sentence breaks found, fall back to character-based splitting
            if len(sentences) == 1:
                # No sentence breaks, split by chunks
                for i in range(0, para_len, limit):
                    segments.append(para[i : i + limit])
            else:
                # Has sentence breaks, split by sentences
                sent_segment = []
                sent_len = 0
                for sent in sentences:
                    if sent_len + len(sent) + 2 > limit and sent_segment:
                        # Flush sentence segment
                        segments.append(". ".join(sent_segment) + ".")
                        sent_segment = [sent]
                        sent_len = len(sent)
                    else:
                        sent_segment.append(sent)
                        sent_len += len(sent) + 2
                if sent_segment:
                    segments.append(". ".join(sent_segment) + ".")

        # Normal paragraph fits in current segment
        elif current_length + para_len + 2 <= limit:
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


def _merge_short_chapters(chapters: list, min_seconds: int) -> list:
    """Merge chapters shorter than min_seconds into a neighbor (skips Hook ch01).

    Greedy bucket pack: once a chapter reaches min_seconds it is "closed" and
    subsequent short chapters start a new bucket with the next chapter.
    """
    if not chapters:
        return chapters

    merged: list = [chapters[0]]  # keep hook as-is

    def _dur(ch):
        return getattr(ch.narration, "estimated_duration_seconds", 0) or 0

    def _is_weather(ch) -> bool:
        title = str(getattr(ch, "title", "") or "").casefold()
        narration = str(getattr(ch.narration, "text", "") or "").casefold()
        return any(
            marker in title or marker in narration
            for marker in ("hava durumu", "hava tahmini", "wettervorhersage", "weather forecast")
        )

    def _merge_tail_into_previous() -> None:
        last = merged[-1]
        prev = merged[-2]
        prev_text = prev.narration.text.rstrip()
        new_text = f"{prev_text} {last.narration.text.lstrip()}".strip()
        new_wc = len(new_text.split())
        prev.narration.text = new_text
        prev.narration.word_count = new_wc
        prev.narration.estimated_duration_seconds = _compute_duration_estimate(new_wc)
        try:
            prev.overlays = list(getattr(prev, "overlays", []) or []) + list(
                getattr(last, "overlays", []) or []
            )
        except Exception:
            pass
        merged.pop()

    for ch in chapters[1:]:
        if _is_weather(ch):
            # Close a preceding short bulletin before appending weather. Weather
            # is a recurring, semantically distinct final Tagesschau chapter.
            if (
                len(merged) >= 2
                and _dur(merged[-1]) < min_seconds
                and merged[-2].chapter_id != "ch01"
                and not _is_weather(merged[-1])
            ):
                _merge_tail_into_previous()
            merged.append(ch)
            continue
        prev = merged[-1] if merged else None
        prev_is_short = prev is not None and prev.chapter_id != "ch01" and _dur(prev) < min_seconds
        if _dur(ch) < min_seconds and prev_is_short:
            # merge into prev
            prev_text = prev.narration.text.rstrip()
            new_text = f"{prev_text} {ch.narration.text.lstrip()}".strip()
            new_wc = len(new_text.split())
            prev.narration.text = new_text
            prev.narration.word_count = new_wc
            prev.narration.estimated_duration_seconds = _compute_duration_estimate(new_wc)
            try:
                prev.overlays = list(getattr(prev, "overlays", []) or []) + list(
                    getattr(ch, "overlays", []) or []
                )
            except Exception:
                pass
        else:
            merged.append(ch)

    # tail: if last chapter is still short, merge it into predecessor
    if (
        len(merged) >= 2
        and _dur(merged[-1]) < min_seconds
        and merged[-2].chapter_id != "ch01"
        and not _is_weather(merged[-1])
    ):
        _merge_tail_into_previous()

    return merged


def _compute_duration_estimate(word_count: int) -> int:
    """Estimate narration duration in seconds from Turkish word count.

    Turkish speech rate: ~150 words/minute.

    Args:
        word_count: Number of words in narration text

    Returns:
        Estimated duration in seconds (rounded to nearest integer)
    """
    WORDS_PER_MINUTE = 150
    duration_minutes = word_count / WORDS_PER_MINUTE
    duration_seconds = duration_minutes * 60
    return round(duration_seconds)


def _parse_json_response(
    response_text: str, episode_id: str, segment: str, settings: Settings
) -> dict:
    """Parse JSON from LLM response, stripping markdown code fences if present.

    Handles common LLM quirks: markdown fences, trailing commas, JS-style
    comments, and single-line // comments.

    Args:
        response_text: Raw LLM response
        episode_id: Episode ID for error messages
        segment: Input segment for error messages
        settings: Settings (unused, for future use)

    Returns:
        Parsed JSON dict

    Raises:
        json.JSONDecodeError: If JSON is malformed after cleanup
    """
    # Strip markdown code fences
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    elif text.startswith("```"):
        text = text[len("```") :].strip()

    if text.endswith("```"):
        text = text[: -len("```")].strip()

    # If response starts with non-JSON text, try to extract the JSON object
    if text and not text.startswith("{") and not text.startswith("["):
        # Find the first { and last }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            logger.warning(
                "Response for %s starts with non-JSON text, extracting JSON object "
                "(chars %d-%d of %d)",
                episode_id,
                first_brace,
                last_brace,
                len(text),
            )
            text = text[first_brace : last_brace + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to fix common LLM JSON quirks (comments, trailing commas)
        cleaned = _clean_json(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            # Last resort: structural repair. The dominant failure mode is an
            # unescaped double-quote inside a narration string (the tagesschau
            # text is full of German „…" quotes and embedded speech), which the
            # model copies verbatim and breaks JSON at the same char every time.
            # json_repair reliably re-escapes these while preserving content.
            logger.warning(
                "Standard JSON parse failed for %s (%s) — attempting structural repair",
                episode_id,
                e,
            )
            try:
                repaired = json_repair.loads(cleaned)
                if isinstance(repaired, dict) and repaired:
                    logger.info("json_repair recovered a valid document for %s", episode_id)
                    return repaired
                logger.error(
                    "json_repair returned an unusable result for %s: %r", episode_id, repaired
                )
            except Exception as repair_err:  # noqa: BLE001 - repair is best-effort
                logger.error("json_repair also failed for %s: %s", episode_id, repair_err)
            logger.error("Failed to parse JSON response for %s: %s", episode_id, e)
            logger.error("Response text (first 500 chars): %s", response_text[:500])
            raise


def _clean_json(text: str) -> str:
    """Best-effort cleanup of LLM-generated JSON.

    Handles:
    - Single-line // comments
    - Trailing commas before } or ]
    """
    import re

    # Remove single-line // comments (but not inside strings)
    # Simple heuristic: remove // comments only when preceded by whitespace or start of line
    lines = []
    for line in text.split("\n"):
        # Skip full-line comments
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        # Remove inline // comments (rough: only if not inside a string value)
        # Find // that's not inside quotes
        in_string = False
        escape_next = False
        cut_pos = None
        for i, ch in enumerate(line):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
            elif ch == "/" and not in_string and i + 1 < len(line) and line[i + 1] == "/":
                cut_pos = i
                break
        if cut_pos is not None:
            line = line[:cut_pos].rstrip()
        lines.append(line)
    text = "\n".join(lines)

    # Remove trailing commas: ,  followed by } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text


_VALID_VISUAL_TYPES = {"title_card", "diagram", "b_roll", "talking_head", "screen_share"}

_VISUAL_TYPE_MAP = {
    "historical_image": "b_roll",
    "illustration": "diagram",
    "image": "b_roll",
    "photo": "b_roll",
    "animation": "diagram",
    "chart": "diagram",
    "graph": "diagram",
    "infographic": "diagram",
    "video": "b_roll",
    "text": "title_card",
}


def _backfill_chapter_structure(ch: dict, is_last: bool = False) -> None:
    """Backfill required structural fields the LLM occasionally omits.

    The chapterize LLM sometimes drops ``visual`` or ``transitions`` from a
    chapter (typically the last/weather one), which raises a Pydantic
    ValidationError and hard-fails the stage. Rather than lose the entire run,
    fill sensible defaults so downstream stages (imagegen/render) can proceed;
    the specific chapter can still be regenerated by the user afterwards.
    """
    if not isinstance(ch, dict):
        return

    title = ch.get("title") or "Haber"
    narration = ch.get("narration") or {}
    narration_text = narration.get("text", "") if isinstance(narration, dict) else ""
    haystack = f"{title} {narration_text}".lower()
    is_weather = any(
        kw in haystack for kw in ("hava durumu", "wetter", "weather", "sıcaklık", "derece")
    )

    # Backfill missing/invalid visual
    visual = ch.get("visual")
    if not isinstance(visual, dict) or not visual.get("type"):
        if is_weather:
            vtype, prompt = "diagram", "Almanya hava durumu haritası, sıcaklık ve rüzgar gösterimi"
        elif ch.get("order") == 1:
            vtype, prompt = "title_card", None
        elif is_last:
            vtype, prompt = "title_card", None
        else:
            vtype, prompt = "b_roll", title
        logger.warning(
            "Backfilling missing visual for chapter %s -> type=%s",
            ch.get("chapter_id", "?"),
            vtype,
        )
        ch["visual"] = {"type": vtype, "description": title, "image_prompt": prompt}

    # Backfill missing/invalid transitions
    transitions = ch.get("transitions")
    if not isinstance(transitions, dict) or "in" not in transitions or "out" not in transitions:
        logger.warning(
            "Backfilling missing transitions for chapter %s",
            ch.get("chapter_id", "?"),
        )
        ch["transitions"] = {"in": "fade", "out": "fade"}


def _fix_chapter_data(data: dict, episode_id: str) -> dict:
    """Fix common LLM output issues before Pydantic validation.

    - Maps invalid visual types to valid enum values
    - Recalculates estimated_duration_seconds from word counts
    - Ensures episode_id is present
    """
    if "episode_id" not in data or not data["episode_id"]:
        data["episode_id"] = episode_id

    chapters = data.get("chapters", [])
    total_duration = 0

    for ch in chapters:
        # Backfill missing required structural fields so a single incomplete
        # chapter from the LLM cannot hard-fail the whole pipeline.
        _backfill_chapter_structure(ch, is_last=(ch is chapters[-1] if chapters else False))

        # Fix visual type
        visual = ch.get("visual")
        if isinstance(visual, dict):
            vtype = visual.get("type", "")
            if vtype not in _VALID_VISUAL_TYPES:
                mapped = _VISUAL_TYPE_MAP.get(vtype.lower(), "b_roll")
                logger.info(
                    "Fixing visual type '%s' -> '%s' in chapter %s",
                    vtype,
                    mapped,
                    ch.get("chapter_id", "?"),
                )
                visual["type"] = mapped
            # b_roll/diagram require a non-empty image_prompt
            if visual.get("type") in ("b_roll", "diagram") and not visual.get("image_prompt"):
                fallback = visual.get("description") or ch.get("title") or "Haber görseli"
                logger.info(
                    "Backfilling missing image_prompt for chapter %s (%s)",
                    ch.get("chapter_id", "?"),
                    visual.get("type"),
                )
                visual["image_prompt"] = fallback

        # Recalculate duration from word count if narration is a dict
        narration = ch.get("narration")
        if isinstance(narration, dict):
            text = narration.get("text", "")
            word_count = len(text.split())
            narration["word_count"] = word_count
            duration = _compute_duration_estimate(word_count)
            narration["estimated_duration_seconds"] = duration
            total_duration += duration

    # Fix total duration to match sum of chapter durations
    if chapters and total_duration > 0:
        data["estimated_duration_seconds"] = total_duration
        data["total_chapters"] = len(chapters)

    return data


def _retry_with_json_correction(
    decode_error: json.JSONDecodeError,
    bad_output: str,
    episode_id: str,
    segment: str,
    system_prompt: str,
    settings: Settings,
) -> dict:
    """Retry chapterization after the model emitted syntactically invalid JSON.

    json_mode normally prevents this, but flaky providers occasionally drop a comma
    or otherwise break the structure. Ask the model to return the SAME content as
    strictly valid JSON.

    Args:
        decode_error: The JSONDecodeError raised while parsing the first attempt.
        bad_output: The raw (invalid) model output, for context.
        episode_id: Episode ID.
        segment: The adapted script segment.
        system_prompt: The system prompt.
        settings: Application settings.

    Returns:
        Parsed JSON dict (unvalidated; caller validates).

    Raises:
        json.JSONDecodeError: If the retry is also invalid JSON.
    """
    corrective_prompt = f"""Your previous response was NOT valid JSON.

Parser error: {decode_error}

Return the SAME chapter document, but as strictly valid JSON only. Rules:
- Output ONLY the JSON object, no prose, no markdown code fences.
- Every array/object element must be separated by a comma.
- No trailing commas, no comments.
- Keep all chapters and their full narration text unchanged.

Re-emit valid JSON for this input:
{segment}
"""

    logger.info("Retrying chapterization after JSON decode error for %s", episode_id)

    response: ClaudeResponse = call_claude(
        system_prompt=system_prompt,
        user_message=corrective_prompt,
        settings=settings,
        max_tokens=16384,
        json_mode=True,
    )

    return _parse_json_response(response.text, episode_id, segment, settings)


def _retry_with_correction(
    validation_error: ValidationError,
    episode_id: str,
    segment: str,
    system_prompt: str,
    settings: Settings,
) -> dict:
    """Retry chapterization with corrective prompt after validation failure.

    Args:
        validation_error: The Pydantic validation error
        episode_id: Episode ID
        segment: The adapted script segment
        system_prompt: The system prompt
        settings: Application settings

    Returns:
        Parsed JSON dict (validated)

    Raises:
        ValidationError: If retry also fails
    """
    corrective_prompt = f"""
The previous JSON output had validation errors:

{validation_error}

Please correct the JSON and return a valid document matching this EXACT schema:

{{
  "schema_version": "1.0",
  "episode_id": "{episode_id}",
  "title": "...",
  "total_chapters": N,
  "estimated_duration_seconds": N,
  "chapters": [
    {{
      "chapter_id": "ch01",
      "title": "...",
      "order": 1,
      "narration": {{
        "text": "...",
        "word_count": N,
        "estimated_duration_seconds": N
      }},
      "visual": {{
        "type": "diagram",
        "description": "...",
        "image_prompt": "..."
      }},
      "overlays": [
        {{
          "type": "lower_third",
          "text": "...",
          "start_offset_seconds": 0.0,
          "duration_seconds": 5.0
        }}
      ],
      "transitions": {{
        "in": "fade",
        "out": "cut"
      }},
      "notes": "..."
    }}
  ]
}}

CRITICAL: narration, visual, overlays, transitions MUST be objects/arrays, NOT strings.
- narration must be an object with text, word_count, estimated_duration_seconds
- visual must be an object with type, description, image_prompt
- overlays must be an array of objects with type, text, start_offset_seconds, duration_seconds
- transitions must be an object with "in" and "out" keys
- episode_id field is REQUIRED at the top level
- total_chapters must equal len(chapters)
- estimated_duration_seconds must equal sum of chapter durations

Original input:
{segment}
"""

    logger.info("Retrying chapterization with corrective prompt for %s", episode_id)

    response: ClaudeResponse = call_claude(
        system_prompt=system_prompt,
        user_message=corrective_prompt,
        settings=settings,
        max_tokens=16384,
        json_mode=True,
    )

    # Parse and return (let caller validate)
    return _parse_json_response(response.text, episode_id, segment, settings)


def _chapter_component_hashes(doc: ChapterDocument) -> dict:
    """Per-component SHA-256 hashes of a chapter document.

    Splitting the document into narration / visuals / overlays / structure lets
    chapterization invalidate only the downstream stages actually affected by a
    change, instead of always regenerating both images and TTS.
    """

    def _h(obj) -> str:
        return hashlib.sha256(
            json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _vtype(visual) -> str:
        t = getattr(visual, "type", "")
        return str(getattr(t, "value", t))

    narration = [{"id": ch.chapter_id, "text": ch.narration.text} for ch in doc.chapters]
    visuals = [
        {
            "id": ch.chapter_id,
            "title": ch.title,
            "type": _vtype(ch.visual),
            "description": ch.visual.description,
            "image_prompt": ch.visual.image_prompt,
            "deterministic": ch.visual.deterministic,
        }
        for ch in doc.chapters
    ]
    overlays = [
        {"id": ch.chapter_id, "overlays": [o.model_dump(mode="json") for o in ch.overlays]}
        for ch in doc.chapters
    ]
    structure = [{"id": ch.chapter_id, "order": ch.order} for ch in doc.chapters]
    return {
        "narration": _h(narration),
        "visuals": _h(visuals),
        "overlays": _h(overlays),
        "structure": _h(structure),
    }


def _load_previous_component_hashes(provenance_path: Path) -> dict | None:
    """Load the previous run's ``component_hashes`` from provenance, or None."""
    if not provenance_path.exists():
        return None
    try:
        prov = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    components = prov.get("component_hashes")
    return components if isinstance(components, dict) else None


def _mark_downstream_stale(
    episode_id: str,
    settings: Settings,
    previous: dict | None = None,
    current: dict | None = None,
) -> None:
    """Selectively mark downstream stages stale based on what changed.

    Mapping (Phase 8 requirement D):
      * structure (chapter set/order) -> images + TTS + render
      * narration text                -> TTS + render (never images)
      * visual type/description/prompt -> images + render (never TTS)
      * overlays only                 -> render only

    When no previous component hashes exist (first run / legacy provenance) the
    behaviour is conservative and marks everything. Markers are written at the
    exact paths the consuming stages check.
    """
    base = Path(settings.outputs_dir) / episode_id
    images_dir = base / "images"
    tts_dir = base / "tts"
    render_draft = base / "render" / "draft.mp4"

    if not previous or not current:
        invalidate = {"images", "tts", "render"}
        reason = "chapters_changed"
    else:
        invalidate: set[str] = set()
        if current.get("structure") != previous.get("structure"):
            invalidate |= {"images", "tts", "render"}
        if current.get("narration") != previous.get("narration"):
            invalidate |= {"tts", "render"}
        if current.get("visuals") != previous.get("visuals"):
            invalidate |= {"images", "render"}
        if current.get("overlays") != previous.get("overlays"):
            invalidate |= {"render"}
        reason = "chapters_changed_selective"

    stale_data = {
        "invalidated_at": _utcnow().isoformat(),
        "invalidated_by": "chapterize",
        "reason": reason,
    }

    def _write(marker: Path) -> None:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            logger.warning("Could not write stale marker %s", marker)

    if "images" in invalidate and images_dir.exists():
        # Cover both image paths: imagegen (manifest.json.stale) and the
        # tagesschau frame-edit stage (.stale).
        _write(images_dir / "manifest.json.stale")
        _write(images_dir / ".stale")
        logger.info("Marked images stale for %s (%s)", episode_id, reason)
    if "tts" in invalidate and tts_dir.exists():
        _write(tts_dir / "manifest.json.stale")
        logger.info("Marked tts stale for %s (%s)", episode_id, reason)
    if "render" in invalidate and render_draft.exists():
        _write(render_draft.with_suffix(".mp4.stale"))
        logger.info("Marked render stale for %s (%s)", episode_id, reason)
