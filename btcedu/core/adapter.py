"""Turkey-context adaptation: Cultural adaptation of Turkish translations with tiered rules."""

import hashlib
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.core.translator import _format_story_findings_block
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.services.claude_service import ClaudeResponse, call_claude

logger = logging.getLogger(__name__)

# Texts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 8_000

# High-signal phrases that indicate the model refused the adaptation task
# instead of returning adapted Turkish text. A refused segment must never be
# accepted, otherwise the source content for that segment is silently dropped.
_REFUSAL_MARKERS = (
    "respectfully decline",
    "i need to decline",
    "i must decline",
    "i cannot assist with",
    "i can't assist with",
    "i cannot help with",
    "i can't help with",
    "as the github copilot cli",
    "i'm not designed for",
    "i am not designed for",
    "unrelated to software development",
    "is there something related to the bitcoin-education repository",
    "i appreciate you providing the detailed instructions",
)


class AdaptationRefusedError(RuntimeError):
    """Raised when the model refuses to adapt a segment instead of returning text."""


def _looks_like_refusal(text: str) -> bool:
    """Detect a model refusal so refused segments are never treated as output.

    Adapted output is Turkish news narration; the English meta-refusal phrases
    below do not occur in legitimate adaptations, so a single match is decisive.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AdaptationResult:
    """Summary of adaptation operation for one episode."""

    episode_id: str
    adapted_path: str
    diff_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0
    output_char_count: int = 0
    adaptation_count: int = 0
    tier1_count: int = 0
    tier2_count: int = 0
    segments_processed: int = 1
    skipped: bool = False


def adapt_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    *,
    target_story_ids: list[str] | None = None,
    structured_findings: dict[str, list[dict]] | None = None,
    budget_check: Callable[[float], bool] | None = None,
) -> AdaptationResult:
    """Adapt a Turkish translation for Turkey context using tiered rules.

    Reads the Turkish translation and German corrected transcript, applies
    tiered cultural adaptation rules, writes the adapted script and diff.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-adapt even if output exists.
        target_story_ids: When set (per-story mode), only these stories are
            re-adapted by the model; all other stories are preserved verbatim
            from the existing ``stories_adapted.json``.
        structured_findings: Optional mapping ``story_id -> [compact finding, ...]``
            injected as per-story correction guidance for the targeted stories.
        budget_check: Optional callback receiving in-flight stage cost before each
            external call. Returning False aborts with a cost-limit error.

    Returns:
        AdaptationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
        FileNotFoundError: If translation or corrected transcript missing.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Allow both TRANSLATED and ADAPTED status
    if episode.status not in (EpisodeStatus.TRANSLATED, EpisodeStatus.ADAPTED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'translated' or 'adapted'. Use --force to override."
        )

    # Check Review Gate 1 approval (correction must be approved)
    if episode.status == EpisodeStatus.TRANSLATED and not force:
        from btcedu.core.reviewer import has_pending_review, profile_auto_approves
        from btcedu.models.review import ReviewStatus, ReviewTask

        # Profiles with auto_approve_reviews run fully automatically — skip the gate.
        if not profile_auto_approves(settings, episode):
            # Check if there's a pending review for correction
            if has_pending_review(session, episode_id):
                raise ValueError(
                    f"Episode {episode_id} has pending review. "
                    "Adaptation cannot proceed until reviews are resolved."
                )

            # Verify correction was approved
            approved_correct = (
                session.query(ReviewTask)
                .filter(
                    ReviewTask.episode_id == episode_id,
                    ReviewTask.stage == "correct",
                    ReviewTask.status == ReviewStatus.APPROVED.value,
                )
                .first()
            )

            if not approved_correct:
                raise ValueError(
                    f"Episode {episode_id} correction has not been approved. "
                    "Adaptation cannot proceed until Review Gate 1 is approved."
                )

    # Resolve paths
    translation_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
    if not translation_path.exists():
        raise FileNotFoundError(
            f"Turkish translation not found for episode {episode_id}: {translation_path}"
        )

    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(
            f"Corrected German transcript not found for episode {episode_id}: {corrected_path}"
        )

    adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    diff_path = Path(settings.outputs_dir) / episode_id / "review" / "adaptation_diff.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "adapt_provenance.json"
    )
    stories_translated_path = Path(settings.outputs_dir) / episode_id / "stories_translated.json"
    stories_adapted_path = Path(settings.outputs_dir) / episode_id / "stories_adapted.json"

    # Resolve profile namespace so news/podcast pick up their own adapt prompt
    content_profile = getattr(episode, "content_profile", None)
    profile_obj = None
    profile_namespace: str | None = None
    adapt_cfg: dict = {}
    try:
        from btcedu.profiles import get_registry as get_profile_registry

        pr = get_profile_registry(settings)
        profile_obj = pr.get(content_profile) if content_profile else None
        if profile_obj is not None:
            profile_namespace = getattr(profile_obj, "prompt_namespace", None)
            adapt_cfg = profile_obj.stage_config.get("adapt", {}) or {}
    except Exception:  # noqa: BLE001
        profile_namespace = None
        adapt_cfg = {}

    adapt_mode = adapt_cfg.get("mode")
    if adapt_mode is None:
        adapt_mode = "disabled" if adapt_cfg.get("skip") else "full"
    if adapt_mode not in {"disabled", "conditional", "full"}:
        raise ValueError(f"Unsupported adapt.mode: {adapt_mode!r}")
    if adapt_mode == "disabled":
        logger.info("Adaptation disabled by profile for %s", episode_id)
        return AdaptationResult(
            episode_id=episode_id,
            adapted_path="",
            diff_path="",
            provenance_path="",
            skipped=True,
        )

    # Load and register prompt via PromptRegistry (profile-namespaced fallback)
    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("adapt.md", profile=profile_namespace)
    prompt_name = "adapt"
    if profile_namespace and (TEMPLATES_DIR / profile_namespace / "adapt.md").exists():
        prompt_name = f"{profile_namespace}/adapt"
    prompt_version = registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Inject configured tiers so prompt can react (news: local_relevance only)
    tiers_list = adapt_cfg.get("tiers") or []
    tiers_block = ", ".join(tiers_list) if tiers_list else "all"
    template_body = template_body.replace("{{ tiers }}", tiers_block)

    # Compute input content hashes for idempotency
    translation_text = translation_path.read_text(encoding="utf-8")
    german_text = corrected_path.read_text(encoding="utf-8")

    use_story_mode = stories_translated_path.exists()
    translation_input_bytes = (
        stories_translated_path.read_bytes() if use_story_mode else translation_text.encode("utf-8")
    )
    translation_hash = hashlib.sha256(translation_input_bytes).hexdigest()
    german_hash = hashlib.sha256(german_text.encode("utf-8")).hexdigest()

    # Idempotency check
    if not force and _is_adaptation_current(
        adapted_path,
        provenance_path,
        translation_hash,
        german_hash,
        prompt_content_hash,
    ):
        logger.info("Adaptation is current for %s (use --force to re-adapt)", episode_id)
        existing_adapted = adapted_path.read_text(encoding="utf-8")
        existing_diff = {}
        if diff_path.exists():
            existing_diff = json.loads(diff_path.read_text(encoding="utf-8"))
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

        return AdaptationResult(
            episode_id=episode_id,
            adapted_path=str(adapted_path),
            diff_path=str(diff_path) if diff_path.exists() else "",
            provenance_path=str(provenance_path),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            input_char_count=len(translation_text),
            output_char_count=len(existing_adapted),
            adaptation_count=existing_diff.get("summary", {}).get("total_adaptations", 0),
            tier1_count=existing_diff.get("summary", {}).get("tier1_count", 0),
            tier2_count=existing_diff.get("summary", {}).get("tier2_count", 0),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.ADAPT,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "adapt")
        feedback_parts: list[str] = []
        if reviewer_feedback:
            feedback_parts.append(
                "## Revisor Geri Bildirimi (lütfen bu düzeltmeleri uygulayın)\n\n"
                f"{reviewer_feedback}\n\n"
                "Önemli: Bu geri bildirimi çıktıda aynen aktarmayın, "
                "yalnızca düzeltme kılavuzu olarak kullanın."
            )
        # QA quality-gate findings are injected per story (structured, targeted)
        # in _adapt_per_story — never as a whole unstructured document here.
        template_body = template_body.replace(
            "{{ reviewer_feedback }}", "\n\n".join(feedback_parts)
        )

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        if use_story_mode:
            (
                adapted_text,
                diff_data,
                total_input_tokens,
                total_output_tokens,
                total_cost,
                segments_processed,
            ) = _adapt_per_story(
                stories_translated_path=stories_translated_path,
                stories_adapted_path=stories_adapted_path,
                episode_id=episode_id,
                system_prompt=system_prompt,
                user_template=user_template,
                settings=settings,
                mode=adapt_mode,
                allowed_operations=adapt_cfg.get("allowed_operations") or tiers_list,
                target_story_ids=target_story_ids,
                structured_findings=structured_findings,
                budget_check=budget_check,
            )
        else:
            (
                adapted_text,
                total_input_tokens,
                total_output_tokens,
                total_cost,
                segments_processed,
            ) = _adapt_legacy_text(
                translation_text=translation_text,
                german_text=german_text,
                episode_id=episode_id,
                system_prompt=system_prompt,
                user_template=user_template,
                prompt_version=prompt_version,
                settings=settings,
            )
            diff_data = compute_adaptation_diff(translation_text, adapted_text, episode_id)

        adaptation_count = diff_data["summary"]["total_adaptations"]
        tier1_count = diff_data["summary"]["tier1_count"]
        tier2_count = diff_data["summary"]["tier2_count"]

        logger.info(
            "Adaptation complete: %d adaptations (T1: %d, T2: %d)",
            adaptation_count,
            tier1_count,
            tier2_count,
        )

        # Persist raw tagged version for review, but clean tags before downstream stages
        # so TTS never speaks '[T1: ...]' aloud.
        raw_tagged_path = adapted_path.parent / (adapted_path.stem + ".raw.md")
        raw_tagged_path.parent.mkdir(parents=True, exist_ok=True)
        raw_tagged_path.write_text(adapted_text, encoding="utf-8")

        clean_adapted_text = strip_tier_markers(adapted_text)

        adapted_path.parent.mkdir(parents=True, exist_ok=True)
        adapted_path.write_text(clean_adapted_text, encoding="utf-8")

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Write provenance
        elapsed = time.monotonic() - t0
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
            "input_files": [
                str(stories_translated_path if use_story_mode else translation_path),
                str(corrected_path),
            ],
            "input_content_hashes": {
                "translation": translation_hash,
                "german": german_hash,
            },
            "output_files": [
                str(adapted_path),
                str(diff_path),
                *([str(stories_adapted_path)] if use_story_mode else []),
            ],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": segments_processed,
            "mode": adapt_mode,
            "per_story_mode": use_story_mode,
            "adaptation_summary": {
                "total_adaptations": adaptation_count,
                "tier1_count": tier1_count,
                "tier2_count": tier2_count,
            },
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="adapt",
            file_path=str(adapted_path),
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
        episode.status = EpisodeStatus.ADAPTED
        episode.error_message = None
        session.commit()

        # Mark downstream chapterization as stale if it exists
        chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
        if chapters_path.exists():
            stale_marker = chapters_path.parent / (chapters_path.name + ".stale")
            stale_data = {
                "invalidated_at": _utcnow().isoformat(),
                "invalidated_by": "adapt",
                "reason": "adapted_script_changed",
            }
            stale_marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
            logger.info("Marked downstream chapterization as stale: %s", chapters_path.name)

        logger.info(
            "Adapted %s (%d→%d chars, %d adaptations, $%.4f)",
            episode_id,
            len(translation_text),
            len(clean_adapted_text),
            adaptation_count,
            total_cost,
        )

        return AdaptationResult(
            episode_id=episode_id,
            adapted_path=str(adapted_path),
            diff_path=str(diff_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=len(translation_text),
            output_char_count=len(clean_adapted_text),
            adaptation_count=adaptation_count,
            tier1_count=tier1_count,
            tier2_count=tier2_count,
            segments_processed=segments_processed,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        pipeline_run.input_tokens = locals().get("total_input_tokens", 0) + int(
            getattr(e, "input_tokens", 0)
        )
        pipeline_run.output_tokens = locals().get("total_output_tokens", 0) + int(
            getattr(e, "output_tokens", 0)
        )
        pipeline_run.estimated_cost_usd = locals().get("total_cost", 0.0) + float(
            getattr(e, "cost_usd", 0.0)
        )
        episode.error_message = f"Adaptation failed: {e}"
        session.commit()
        logger.error("Adaptation failed for %s: %s", episode_id, e)
        raise


def _adapt_legacy_text(
    translation_text: str,
    german_text: str,
    episode_id: str,
    system_prompt: str,
    user_template: str,
    prompt_version,
    settings: Settings,
) -> tuple[str, int, int, float, int]:
    """Preserve the existing full-text adapter for profiles without stories."""
    segments = _segment_text(translation_text)
    german_slices = _slice_german_by_segments(german_text, translation_text, segments)
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    adapted_segments: list[str] = []

    for i, segment in enumerate(segments):
        german_ref = german_slices[i] if i < len(german_slices) else ""
        scoped_translation = (
            segment + "\n\n<!-- SEGMENT_SCOPE: Only adapt content that appears in "
            "THIS Turkish segment above. If the German reference mentions "
            "topics not present in this Turkish text, IGNORE them. -->"
        )
        user_message = user_template.replace("{{ translation }}", scoped_translation).replace(
            "{{ original_german }}", german_ref
        )
        dry_run_path = (
            Path(settings.outputs_dir) / episode_id / f"dry_run_adapt_{i}.json"
            if settings.dry_run
            else None
        )
        template_max = getattr(prompt_version, "max_tokens", None) or 0
        effective_max = max(int(template_max or 0), int(settings.claude_max_tokens or 0))
        response = _call_adaptation_with_refusal_retry(
            system_prompt,
            user_message,
            settings,
            dry_run_path,
            effective_max or None,
            f"segment {i + 1}/{len(segments)}",
            json_mode=False,
        )
        adapted_segments.append(response.text)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
        total_cost += response.cost_usd

    return (
        "\n\n".join(adapted_segments),
        total_input_tokens,
        total_output_tokens,
        total_cost,
        len(segments),
    )


def _adapt_per_story(
    stories_translated_path: Path,
    stories_adapted_path: Path,
    episode_id: str,
    system_prompt: str,
    user_template: str,
    settings: Settings,
    mode: str,
    allowed_operations: list[str],
    target_story_ids: list[str] | None = None,
    structured_findings: dict[str, list[dict]] | None = None,
    budget_check: Callable[[float], bool] | None = None,
) -> tuple[str, dict, int, int, float, int]:
    """Adapt only stories with a concrete need and preserve their identity.

    When ``target_story_ids`` is given, only those stories are re-adapted; every
    other story is preserved verbatim from the existing ``stories_adapted.json``.
    """
    from btcedu.models.story_schema import StoryAdaptationOutput, StoryDocument

    story_doc = StoryDocument.model_validate_json(
        stories_translated_path.read_text(encoding="utf-8")
    )
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    processed = 0
    adaptations: list[dict] = []
    adapted_stories: list[dict] = []

    target_set = set(target_story_ids) if target_story_ids is not None else None
    findings_by_story = structured_findings or {}

    existing_by_id: dict[str, dict] = {}
    if target_set is not None and stories_adapted_path.exists():
        try:
            existing_data = json.loads(stories_adapted_path.read_text(encoding="utf-8"))
            for entry in existing_data.get("stories", []):
                if entry.get("story_id"):
                    existing_by_id[entry["story_id"]] = entry
        except (json.JSONDecodeError, OSError):
            existing_by_id = {}

    for story in story_doc.stories:
        # Targeted rerun: preserve unaffected stories exactly as previously adapted.
        if (
            target_set is not None
            and story.story_id not in target_set
            and story.story_id in existing_by_id
        ):
            preserved = existing_by_id[story.story_id]
            adapted_stories.append(preserved)
            for operation in preserved.get("adaptation_operations", []):
                adaptations.append(
                    {
                        "item_id": f"adapt-{story.story_id}-{len(adaptations) + 1:03d}",
                        "story_id": story.story_id,
                        "tier": "T1",
                        "category": operation,
                        "original": story.text_tr or "",
                        "adapted": preserved.get("text_adapted_tr", ""),
                        "context": (preserved.get("text_adapted_tr", "") or "")[:200],
                        "position": {
                            "start": 0,
                            "end": len(preserved.get("text_adapted_tr", "") or ""),
                        },
                    }
                )
            logger.info("Story %s preserved (not targeted by QA repair)", story.story_id)
            continue

        source_text = story.text_tr or ""
        needed = _needed_adaptation_operations(story, source_text, allowed_operations)
        if mode == "conditional" and not needed:
            output = StoryAdaptationOutput(
                story_id=story.story_id,
                adapted_text=source_text,
                operations_applied=[],
            )
        else:
            requested = needed if mode == "conditional" else allowed_operations
            payload = {
                "story_id": story.story_id,
                "source_segment_ids": story.source_segment_ids,
                "translated_text": source_text,
                "original_german": story.source_text or story.text_de,
                "source_flags": story.source_flags,
                "allowed_operations": requested,
            }
            user_message = user_template.replace(
                "{{ translation }}",
                json.dumps(payload, ensure_ascii=False, indent=2),
            ).replace("{{ original_german }}", story.source_text or story.text_de)
            story_findings = findings_by_story.get(story.story_id)
            if story_findings:
                user_message += "\n\n" + _format_story_findings_block(story_findings)
            dry_run_path = (
                Path(settings.outputs_dir) / episode_id / f"dry_run_adapt_{story.story_id}.json"
                if settings.dry_run
                else None
            )
            response = _call_adaptation_with_refusal_retry(
                system_prompt,
                user_message,
                settings,
                dry_run_path,
                None,
                f"story {story.story_id}",
                json_mode=True,
                budget_check=(
                    (lambda pending, spent=total_cost: budget_check(spent + pending))
                    if budget_check is not None
                    else None
                ),
            )
            processed += 1
            total_input_tokens += response.input_tokens
            total_output_tokens += response.output_tokens
            total_cost += response.cost_usd
            output = StoryAdaptationOutput.model_validate(
                _parse_structured_adaptation(response.text)
            )
            if output.story_id != story.story_id:
                raise ValueError(
                    f"Adaptation returned story_id {output.story_id!r}, expected {story.story_id!r}"
                )
            disallowed = sorted(set(output.operations_applied) - set(requested))
            if disallowed:
                raise ValueError(
                    f"Story {story.story_id} used disallowed adaptation operations: {disallowed}"
                )
            risks = _adaptation_fidelity_risks(
                source_text,
                output.adapted_text,
                allow_anchor_unify="anchor_unify" in output.operations_applied,
                removable_names=[story.reporter] if story.reporter else [],
            )
            if risks:
                raise ValueError(
                    f"Story {story.story_id} adaptation changed protected facts: {risks}"
                )

        story_data = story.model_dump(mode="json")
        story_data["text_adapted_tr"] = output.adapted_text
        story_data["adaptation_operations"] = output.operations_applied
        story_data["narration_sha256"] = hashlib.sha256(
            output.adapted_text.encode("utf-8")
        ).hexdigest()
        adapted_stories.append(story_data)
        for operation in output.operations_applied:
            adaptations.append(
                {
                    "item_id": f"adapt-{story.story_id}-{len(adaptations) + 1:03d}",
                    "story_id": story.story_id,
                    "tier": "T1",
                    "category": operation,
                    "original": source_text,
                    "adapted": output.adapted_text,
                    "context": output.adapted_text[:200],
                    "position": {"start": 0, "end": len(output.adapted_text)},
                }
            )

    document_data = story_doc.model_dump(mode="json")
    document_data["stories"] = adapted_stories
    stories_adapted_path.write_text(
        json.dumps(document_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    adapted_text = "\n\n".join(
        story["text_adapted_tr"] for story in adapted_stories if story["text_adapted_tr"]
    )
    diff = {
        "episode_id": episode_id,
        "original_length": sum(len(story.text_tr or "") for story in story_doc.stories),
        "adapted_length": len(adapted_text),
        "adaptations": adaptations,
        "summary": {
            "total_adaptations": len(adaptations),
            "tier1_count": len(adaptations),
            "tier2_count": 0,
            "by_category": {
                operation: sum(item["category"] == operation for item in adaptations)
                for operation in sorted({item["category"] for item in adaptations})
            },
        },
    }
    return (
        adapted_text,
        diff,
        total_input_tokens,
        total_output_tokens,
        total_cost,
        processed,
    )


def _call_adaptation_with_refusal_retry(
    system_prompt: str,
    user_message: str,
    settings: Settings,
    dry_run_path: Path | None,
    max_tokens: int | None,
    label: str,
    json_mode: bool,
    budget_check: Callable[[float], bool] | None = None,
) -> ClaudeResponse:
    """Retry one refusal and never accept it as successful output."""
    if budget_check is not None and not budget_check(0.0):
        from btcedu.services.errors import ErrorCategory, PipelineError

        raise PipelineError(
            "Episode cost limit reached before adaptation call",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )
    response = call_claude(
        system_prompt=system_prompt,
        user_message=user_message,
        settings=settings,
        dry_run_path=dry_run_path,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    if not settings.dry_run and _looks_like_refusal(response.text):
        logger.warning("%s: model refused adaptation, retrying once", label)
        if budget_check is not None and not budget_check(response.cost_usd):
            from btcedu.services.errors import ErrorCategory, PipelineError

            error = PipelineError(
                "Episode cost limit reached before adaptation retry",
                ErrorCategory.PERMANENT_COST_LIMIT,
            )
            error.input_tokens = response.input_tokens
            error.output_tokens = response.output_tokens
            error.cost_usd = response.cost_usd
            raise error
        retry_response = call_claude(
            system_prompt=system_prompt,
            user_message=user_message,
            settings=settings,
            dry_run_path=dry_run_path,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        if _looks_like_refusal(retry_response.text):
            raise AdaptationRefusedError(
                f"Model refused to adapt {label} twice; aborting to avoid content loss. "
                f"Refusal preview: {retry_response.text.strip()[:200]!r}"
            )
        response = ClaudeResponse(
            text=retry_response.text,
            input_tokens=response.input_tokens + retry_response.input_tokens,
            output_tokens=response.output_tokens + retry_response.output_tokens,
            cost_usd=response.cost_usd + retry_response.cost_usd,
            model=getattr(retry_response, "model", getattr(response, "model", "")),
        )
    return response


def _parse_structured_adaptation(response_text: str) -> dict:
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Adaptation response did not contain a JSON object")
    return json.loads(text[start : end + 1])


def _needed_adaptation_operations(story, text: str, allowed_operations: list[str]) -> list[str]:
    """Return only operations justified by concrete markers in this story."""
    needed: list[str] = []
    lowered = text.casefold()
    if "anchor_unify" in allowed_operations and (
        story.story_type == "interview"
        or story.reporter
        or re.search(r"\b(ben|biz|bizim|bize)\b", lowered)
        or re.search(r"\b(muhabirimiz|meslektaşımız|teşekkürler)\b", lowered)
    ):
        needed.append("anchor_unify")
    if "institution_explanation" in allowed_operations and re.search(
        r"\b(Bundestag|Bundesrat|BaFin|AfD)\b(?!\s*\()", text
    ):
        needed.append("institution_explanation")
    if "local_relevance" in allowed_operations and re.search(
        r"\b(Türkiye|Türk|göç|çifte vatandaşlık)\b", text, re.IGNORECASE
    ):
        needed.append("local_relevance")
    return needed


_ADAPT_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,:%-]\d+)*(?!\w)")
_ADAPT_NAME_RE = re.compile(r"(?<![.!?]\s)\b[A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü'-]{2,}\b")


def _adaptation_fidelity_risks(
    source_text: str,
    adapted_text: str,
    *,
    allow_anchor_unify: bool = False,
    removable_names: list[str] | None = None,
) -> list[str]:
    """Protect exact factual tokens during same-language adaptation."""
    risks: list[str] = []
    if Counter(_ADAPT_NUMBER_RE.findall(source_text)) != Counter(
        _ADAPT_NUMBER_RE.findall(adapted_text)
    ):
        risks.append("numbers_dates_or_scores")
    name_source_text = (
        _strip_anchor_handoff_text(source_text) if allow_anchor_unify else source_text
    )
    source_names = set(_ADAPT_NAME_RE.findall(name_source_text))
    for removable_name in removable_names or []:
        source_names.difference_update(_ADAPT_NAME_RE.findall(removable_name))
    adapted_names = set(_ADAPT_NAME_RE.findall(adapted_text))
    missing_names = sorted(source_names - adapted_names)
    if missing_names:
        risks.append("names:" + ",".join(missing_names))
    return risks


def _strip_anchor_handoff_text(text: str) -> str:
    """Remove only sentences that anchor_unify is explicitly allowed to drop."""
    removable = re.compile(
        r"[^.!?]*(?:teşekkürler|muhabirimiz|meslektaşımız|"
        r"şimdi sözü|sözü .* bırakıyoruz)[^.!?]*[.!?]?",
        re.IGNORECASE,
    )
    return removable.sub("", text)


def _is_adaptation_current(
    adapted_path: Path,
    provenance_path: Path,
    translation_hash: str,
    german_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing adaptation is still valid.

    Returns True (skip) if ALL of:
    1. adapted_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hashes match
    """
    if not adapted_path.exists():
        return False

    # Check for stale marker
    stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Adaptation marked stale (upstream change), will reprocess")
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

    stored_hashes = provenance.get("input_content_hashes", {})
    if stored_hashes.get("translation") != translation_hash:
        logger.info("Translation content hash mismatch (translation was updated)")
        return False

    if stored_hashes.get("german") != german_hash:
        logger.info("German content hash mismatch (corrected transcript was updated)")
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


def _slice_german_by_segments(
    german_text: str, translation_text: str, tr_segments: list[str]
) -> list[str]:
    """Slice the German reference into portions matching each Turkish segment.

    Each Turkish segment is adapted with only the corresponding German portion
    as reference (plus small overlap for context). Without this, the LLM tends
    to re-adapt earlier German content in later segments, causing duplication.

    Args:
        german_text: Full German corrected transcript.
        translation_text: Full concatenated Turkish translation.
        tr_segments: The Turkish translation split into segments.

    Returns:
        List of German slices, one per Turkish segment. Single segment
        returns full German text.
    """
    if len(tr_segments) <= 1:
        return [german_text]

    total_tr = sum(len(s) for s in tr_segments) or 1
    total_de = len(german_text)
    slices: list[str] = []
    cursor_tr = 0
    for i, seg in enumerate(tr_segments):
        # Compute proportional German range for this segment
        frac_start = cursor_tr / total_tr
        cursor_tr += len(seg)
        frac_end = cursor_tr / total_tr
        de_start = int(frac_start * total_de)
        de_end = int(frac_end * total_de)
        # Small ~5 % overlap on either side for anaphora context
        pad = max(200, total_de // 20)
        de_start = max(0, de_start - pad)
        de_end = min(total_de, de_end + pad)
        # Snap to nearest paragraph boundary so we don't cut mid-sentence
        snap_start = german_text.rfind("\n\n", 0, de_start + 100)
        if snap_start > de_start - 500:
            de_start = snap_start if snap_start >= 0 else de_start
        snap_end = german_text.find("\n\n", max(0, de_end - 100))
        if 0 < snap_end < de_end + 500:
            de_end = snap_end
        slices.append(german_text[de_start:de_end].strip())
    return slices


def _segment_text(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split text into segments at paragraph breaks.

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


def strip_tier_markers(text: str) -> str:
    """Remove [T1:...]/[T2:...] adapter tags but keep their readable substitution content.

    Rules:
    - "[T1: Türkiye'deki SPK] yeni kurallar" → "Türkiye'deki SPK yeni kurallar"
    - "[T1: ton düzeltmesi]" (meta-only, no substantive content) → dropped
    - "[T1: [kaldırıldı: X]]" (removal marker) → dropped, replaced by short parenthetical
    - Also strips "<!-- localization_notes ... -->" HTML comments used by news adapt.
    """
    if not text:
        return text

    # First: drop meta-only markers (no substantive replacement content)
    meta_only = {"ton düzeltmesi", "tone adjustment", "kültürel referans korundu"}

    def _replace(match: "re.Match[str]") -> str:
        content = match.group(2).strip()
        # Removal case: [T1: [kaldırıldı: ...]]  or nested brackets meaning "removed"
        if content.lower().startswith("[kaldırıldı") or "kaldırıldı:" in content.lower():
            return ""
        # Pure meta-only tone markers
        if content.lower() in meta_only:
            return ""
        # Handle "original → adapted" — keep only adapted side
        if "→" in content:
            _, adapted_part = content.split("→", 1)
            return adapted_part.strip().strip('"').strip("'")
        # Everything else: keep raw content, drop the [Tx: ...] wrapper
        return content

    pattern = r"\[(T1|T2):\s*((?:[^\[\]]|\[[^\]]*\])+)\]"
    cleaned = re.sub(pattern, _replace, text)

    # Strip HTML localization_notes comments
    cleaned = re.sub(r"<!--\s*localization_notes.*?-->", "", cleaned, flags=re.DOTALL)

    # Collapse multiple spaces/newlines introduced by removals
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def compute_adaptation_diff(
    translation: str,
    adapted: str,
    episode_id: str,
) -> dict:
    """Compute adaptation diff by parsing [T1]/[T2] tags in adapted text.

    Returns:
        {
            "episode_id": str,
            "original_length": int,
            "adapted_length": int,
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
                "by_category": {...}
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
            original_text = parts[0].strip().strip('"').strip("'")
            adapted_text = parts[1].strip().strip('"').strip("'")
        else:
            # No arrow: content is the adapted replacement
            original_text = ""
            adapted_text = content

        adaptations.append(
            {
                "item_id": f"adap-{len(adaptations):04d}",
                "tier": tier,
                "category": category,
                "original": original_text,
                "adapted": adapted_text,
                "context": context,
                "position": {"start": start, "end": end},
            }
        )

    # Summarize
    tier1_count = sum(1 for a in adaptations if a["tier"] == "T1")
    tier2_count = sum(1 for a in adaptations if a["tier"] == "T2")

    by_category: dict[str, int] = {}
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
    """Classify adaptation by analyzing the tag content.

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
    elif any(
        inst in content_lower
        for inst in ["bafin", "sparkasse", "bundesbank", "spk", "merkez bankası"]
    ):
        return "institution_replacement"
    elif any(curr in content_lower for curr in ["eur", "usd", "tl", "€", "$", "₺"]):
        return "currency_conversion"
    elif "düzenleme" in content_lower or "mevzuat" in content_lower or "regulat" in content_lower:
        return "regulatory_context"
    else:
        return "other"
