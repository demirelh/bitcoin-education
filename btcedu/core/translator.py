"""Turkish translation: Faithful German→Turkish translation of corrected transcripts."""

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

import json_repair
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.core.translation_glossary import fix_translation_glossary
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

# Transcripts longer than this (in characters) are split into segments
SEGMENT_CHAR_LIMIT = 6_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _format_story_findings_block(findings: list[dict]) -> str:
    """Render compact per-story QA findings as targeted correction guidance.

    Only the structured fields are passed (never a whole QA document). The model
    must fix exactly these points and leave everything else unchanged.
    """
    lines = [
        "## QA-Korrekturen für DIESE Story (gezielt anwenden)",
        "Ein unabhängiges Qualitätsgate hat die folgenden Punkte beanstandet. "
        "Korrigiere ausschließlich diese Punkte und ändere sonst nichts. "
        "Erfinde keine Fakten und übernimm diesen Hinweistext nicht in die Ausgabe.",
    ]
    for finding in findings:
        parts = [
            f"- [{str(finding.get('severity', '')).upper()}]",
            str(finding.get("category", "")),
        ]
        if finding.get("explanation"):
            parts.append(f"— {finding['explanation']}")
        if finding.get("required_action"):
            parts.append(f"→ {finding['required_action']}")
        lines.append(" ".join(p for p in parts if p))
        if finding.get("source"):
            lines.append(f"    Quelle: {str(finding['source'])[:240]}")
        if finding.get("target"):
            lines.append(f"    Ziel:   {str(finding['target'])[:240]}")
    return "\n".join(lines)


@dataclass
class TranslationResult:
    """Summary of translation operation for one episode."""

    episode_id: str
    translated_path: str
    provenance_path: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    input_char_count: int = 0
    output_char_count: int = 0
    segments_processed: int = 1
    skipped: bool = False


def translate_transcript(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    *,
    target_story_ids: list[str] | None = None,
    structured_findings: dict[str, list[dict]] | None = None,
    budget_check: Callable[[float], bool] | None = None,
) -> TranslationResult:
    """Translate a corrected German transcript to Turkish.

    Reads the corrected German transcript, sends it to Claude for faithful
    translation to Turkish, writes the translated transcript and provenance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-translate even if output exists.
        target_story_ids: When set (per-story mode), only these stories are
            re-translated by the model; all other stories are preserved verbatim
            from the existing ``stories_translated.json``. Enables safe targeted
            QA repairs without touching unaffected stories.
        structured_findings: Optional mapping ``story_id -> [compact finding, ...]``
            (finding_id/category/severity/explanation/source/target/required_action)
            injected as per-story correction guidance for the targeted stories.
        budget_check: Optional callback receiving in-flight stage cost before each
            external call. Returning False aborts with a cost-limit error.

    Returns:
        TranslationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in correct status.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # Allow CORRECTED, SEGMENTED (news profile), and TRANSLATED status:
    # - CORRECTED: Normal first-time translation (after Review Gate 1 approval)
    # - SEGMENTED: News profile path — segment stage ran, ready for translation
    # - TRANSLATED: Allow idempotent re-runs (useful for testing, manual re-translation)
    # The _is_translation_current() check will skip if output is already current.
    if (
        episode.status
        not in (EpisodeStatus.CORRECTED, EpisodeStatus.SEGMENTED, EpisodeStatus.TRANSLATED)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'corrected', 'segmented', or 'translated'. Use --force to override."
        )

    # Check Review Gate 1 approval (unless episode already segmented/translated or force flag)
    # Per MASTERPLAN §3.1, translation must not proceed until Review Gate 1 is approved.
    if episode.status in (EpisodeStatus.CORRECTED, EpisodeStatus.SEGMENTED) and not force:
        from btcedu.core.reviewer import has_pending_review, profile_auto_approves

        # Profiles with auto_approve_reviews run fully automatically — skip the gate.
        if not profile_auto_approves(settings, episode):
            # First check if there's a pending review (not yet approved/rejected)
            if has_pending_review(session, episode_id):
                raise ValueError(
                    f"Episode {episode_id} has pending review for correction stage. "
                    "Translation cannot proceed until Review Gate 1 is approved."
                )

            # Verify at least one approved review exists for the correct stage
            from btcedu.models.review import ReviewStatus, ReviewTask  # noqa: I001

            approved_review = (
                session.query(ReviewTask)
                .filter(
                    ReviewTask.episode_id == episode_id,
                    ReviewTask.stage == "correct",
                    ReviewTask.status == ReviewStatus.APPROVED.value,
                )
                .first()
            )

            if not approved_review:
                raise ValueError(
                    f"Episode {episode_id} correction has not been approved. "
                    "Translation cannot proceed until Review Gate 1 is approved."
                )

    # Resolve paths
    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not corrected_path.exists():
        raise FileNotFoundError(
            f"Corrected transcript not found for episode {episode_id}: {corrected_path}"
        )

    # Check for reviewed sidecar (Phase 5 granular review output)
    reviewed_path = (
        Path(settings.outputs_dir) / episode_id / "review" / "transcript.reviewed.de.txt"
    )
    if reviewed_path.exists():
        corrected_path = reviewed_path
        logger.info("Using reviewed transcript sidecar for episode %s", episode_id)

    translated_path = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "translate_provenance.json"
    )

    # Resolve profile namespace for prompt namespacing
    content_profile = getattr(episode, "content_profile", None)
    try:
        from btcedu.profiles import get_registry as get_profile_registry

        pr = get_profile_registry(settings)
        profile_obj = pr.get(content_profile) if content_profile else None
        profile_namespace = getattr(profile_obj, "prompt_namespace", None) if profile_obj else None
        per_story_mode = (
            profile_obj is not None
            and profile_obj.stage_config.get("translate", {}).get("mode") == "per_story"
        )
    except Exception:
        profile_obj = None
        profile_namespace = None
        per_story_mode = False

    # Load and register prompt via PromptRegistry (with profile-namespaced fallback)
    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("translate.md", profile=profile_namespace)
    prompt_name = "translate"
    if profile_namespace and (TEMPLATES_DIR / profile_namespace / "translate.md").exists():
        prompt_name = f"{profile_namespace}/translate"
    prompt_version = registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Check if per-story mode: stories.json exists for this episode
    stories_path = Path(settings.outputs_dir) / episode_id / "stories.json"
    use_per_story = per_story_mode and stories_path.exists()

    # Compute input content hash for idempotency
    corrected_text = corrected_path.read_text(encoding="utf-8")
    story_bytes = stories_path.read_bytes() if use_per_story else b""
    input_content_hash = hashlib.sha256(
        corrected_text.encode("utf-8") + b"\0" + story_bytes
    ).hexdigest()

    # Idempotency check
    if not force and _is_translation_current(
        translated_path, provenance_path, input_content_hash, prompt_content_hash
    ):
        logger.info("Translation is current for %s (use --force to re-translate)", episode_id)
        existing_translated = translated_path.read_text(encoding="utf-8")
        existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        return TranslationResult(
            episode_id=episode_id,
            translated_path=str(translated_path),
            provenance_path=str(provenance_path),
            input_tokens=existing_provenance.get("input_tokens", 0),
            output_tokens=existing_provenance.get("output_tokens", 0),
            cost_usd=existing_provenance.get("cost_usd", 0.0),
            input_char_count=len(corrected_text),
            output_char_count=len(existing_translated),
            segments_processed=existing_provenance.get("segments_processed", 1),
            skipped=True,
        )

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSLATE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()

    try:
        # Inject reviewer feedback if available (from request_changes)
        from btcedu.core.reviewer import get_latest_reviewer_feedback

        reviewer_feedback = get_latest_reviewer_feedback(session, episode_id, "translate")
        feedback_parts: list[str] = []
        if reviewer_feedback:
            feedback_parts.append(
                "## Reviewer Feedback (please apply these corrections)\n\n"
                f"{reviewer_feedback}\n\n"
                "Important: Treat this feedback as correction guidance. "
                "Do not include the feedback text verbatim in your output."
            )
        # QA quality-gate findings are injected per story (structured, targeted)
        # in _translate_per_story — never as a whole unstructured document here.
        template_body = template_body.replace(
            "{{ reviewer_feedback }}", "\n\n".join(feedback_parts)
        )

        # Split prompt template into system and user parts
        system_prompt, user_template = _split_prompt(template_body)

        # Inject domain glossary (if profile has one) into the system prompt
        try:
            from btcedu.prompts.glossary_loader import inject_glossary_into_prompt

            profile_id = getattr(profile_obj, "profile_id", None) or profile_namespace
            system_prompt = inject_glossary_into_prompt(system_prompt, profile_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Glossary injection failed: %s", e)

        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0

        # Check if moderator cleaning is enabled for this profile
        clean_moderator = profile_obj is not None and profile_obj.stage_config.get(
            "translate", {}
        ).get("clean_moderator", False)

        if use_per_story:
            # Per-story mode: translate each story individually
            (
                translated_text,
                segments_processed,
                total_input_tokens,
                total_output_tokens,
                total_cost,
            ) = _translate_per_story(
                stories_path=stories_path,
                translated_path=translated_path,
                episode_id=episode_id,
                system_prompt=system_prompt,
                user_template=user_template,
                settings=settings,
                profile_namespace=profile_namespace,
                clean_moderator=clean_moderator,
                session=session,
                target_story_ids=target_story_ids,
                structured_findings=structured_findings,
                adjudication_route=_translation_adjudication_route(profile_obj),
                budget_check=budget_check,
            )
        else:
            # Standard full-transcript translation
            segments = _segment_text(corrected_text)
            segments_processed = len(segments)
            translated_segments: list[str] = []

            for i, segment in enumerate(segments):
                if budget_check is not None and not budget_check(total_cost):
                    from btcedu.services.errors import ErrorCategory, PipelineError

                    raise PipelineError(
                        "Episode cost limit reached before translation call",
                        ErrorCategory.PERMANENT_COST_LIMIT,
                    )
                user_message = user_template.replace("{{ transcript }}", segment)

                # Dry-run path
                dry_run_path = (
                    Path(settings.outputs_dir) / episode_id / f"dry_run_translate_{i}.json"
                    if settings.dry_run
                    else None
                )

                # Use template's max_tokens (from frontmatter) if higher than global setting.
                _template_max = getattr(prompt_version, "max_tokens", None) or 0
                _effective_max = max(int(_template_max or 0), int(settings.claude_max_tokens or 0))
                response: ClaudeResponse = call_claude(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    settings=settings,
                    dry_run_path=dry_run_path,
                    max_tokens=_effective_max or None,
                )

                translated_segments.append(fix_translation_glossary(response.text))
                total_input_tokens += response.input_tokens
                total_output_tokens += response.output_tokens
                total_cost += response.cost_usd

            translated_text = "\n\n".join(translated_segments)

            # Write translated transcript file
            translated_path.parent.mkdir(parents=True, exist_ok=True)
            translated_path.write_text(translated_text, encoding="utf-8")

        # Fidelity check: warn loudly if TR output is dramatically shorter than DE input.
        _ratio = len(translated_text) / max(len(corrected_text), 1)
        if _ratio < 0.75:
            logger.warning(
                "Translation fidelity LOW: %s TR/DE char ratio=%.2f "
                "(DE=%d chars → TR=%d chars). Model may be summarizing instead of translating. "
                "Consider smaller SEGMENT_CHAR_LIMIT, higher max_tokens, or stronger prompt.",
                episode_id,
                _ratio,
                len(corrected_text),
                len(translated_text),
            )

        # Mark downstream adaptation as stale if it exists (cascade invalidation)
        adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
        if adapted_path.exists():
            stale_marker = adapted_path.parent / (adapted_path.name + ".stale")
            stale_data = {
                "invalidated_at": _utcnow().isoformat(),
                "invalidated_by": "translate",
                "reason": "translation_changed",
            }
            stale_marker.parent.mkdir(parents=True, exist_ok=True)
            stale_marker.write_text(json.dumps(stale_data, indent=2), encoding="utf-8")
            logger.info("Marked downstream adaptation as stale: %s", adapted_path.name)

        # Write provenance
        elapsed = time.monotonic() - t0
        provenance = {
            "stage": "translate",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": prompt_version.name,
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "model_params": {
                "temperature": settings.claude_temperature,
                "max_tokens": settings.claude_max_tokens,
            },
            "input_files": [str(corrected_path)],
            "input_content_hash": input_content_hash,
            "output_files": [
                str(translated_path),
                *(
                    [str(Path(settings.outputs_dir) / episode_id / "stories_translated.json")]
                    if use_per_story
                    else []
                ),
            ],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cost_usd": total_cost,
            "duration_seconds": round(elapsed, 2),
            "segments_processed": segments_processed,
            "per_story_mode": use_per_story,
        }

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Persist ContentArtifact
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="translate",
            file_path=str(translated_path),
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
        episode.status = EpisodeStatus.TRANSLATED
        episode.error_message = None
        session.commit()

        logger.info(
            "Translated transcript for %s (%d→%d chars, $%.4f)",
            episode_id,
            len(corrected_text),
            len(translated_text),
            total_cost,
        )

        return TranslationResult(
            episode_id=episode_id,
            translated_path=str(translated_path),
            provenance_path=str(provenance_path),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            input_char_count=len(corrected_text),
            output_char_count=len(translated_text),
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
        episode.error_message = str(e)
        session.commit()
        raise


def _is_translation_current(
    translated_path: Path,
    provenance_path: Path,
    input_content_hash: str,
    prompt_content_hash: str,
) -> bool:
    """Check if existing translation is still valid.

    Returns True (skip) if ALL of:
    1. translated_path exists
    2. No .stale marker exists
    3. provenance_path exists and its prompt_hash matches
    4. provenance_path's input_content_hash matches
    """
    if not translated_path.exists():
        return False

    # Check for stale marker
    stale_marker = translated_path.parent / (translated_path.name + ".stale")
    if stale_marker.exists():
        logger.info("Translation marked stale: %s", stale_marker.read_text(encoding="utf-8"))
        stale_marker.unlink()  # Remove marker after detection
        return False

    if not provenance_path.exists():
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if provenance.get("prompt_hash") != prompt_content_hash:
        logger.info("Prompt hash mismatch (prompt was updated)")
        return False

    if provenance.get("input_content_hash") != input_content_hash:
        logger.info("Input content hash mismatch (corrected transcript was updated)")
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


def _segment_text(text: str, limit: int = SEGMENT_CHAR_LIMIT) -> list[str]:
    """Split text into segments at paragraph breaks.

    If the text is shorter than the limit, returns a single-element list.
    Splits at double-newline paragraph boundaries to preserve context.
    If a single paragraph exceeds the limit, splits at sentence boundaries.

    Args:
        text: The full transcript text.
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


def _translate_per_story(
    stories_path: "Path",
    translated_path: "Path",
    episode_id: str,
    system_prompt: str,
    user_template: str,
    settings: "Settings",
    profile_namespace: str | None = None,
    clean_moderator: bool = False,
    session: "Session | None" = None,
    target_story_ids: list[str] | None = None,
    structured_findings: dict[str, list[dict]] | None = None,
    adjudication_route: dict[str, str] | None = None,
    budget_check: Callable[[float], bool] | None = None,
) -> tuple[str, int, int, int, float]:
    """Translate each story in a StoryDocument individually.

    Translates story.text_de -> story.text_tr and story.headline_de -> story.headline_tr
    for each story in the StoryDocument. Writes stories_translated.json and the concatenated
    transcript.tr.txt.

    When clean_moderator is True and story_type is "intro" or "outro", uses a
    specialized prompt that neutralizes moderator greetings and broadcast names,
    followed by deterministic regex cleaning of moderator names.

    When ``target_story_ids`` is given, only those stories are re-translated by the
    model; every other story is preserved verbatim from the existing
    ``stories_translated.json`` so unaffected stories never change.

    Returns:
        Tuple of (translated_text, segments_processed, input_tokens, output_tokens, cost_usd)
    """
    from btcedu.models.story_schema import StoryDocument

    stories_data = json.loads(stories_path.read_text(encoding="utf-8"))
    story_doc = StoryDocument.model_validate(stories_data)

    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    segments_processed = 0

    target_set = set(target_story_ids) if target_story_ids is not None else None
    findings_by_story = structured_findings or {}

    # For a targeted rerun, preserve untargeted stories from the existing artifact.
    existing_by_id: dict[str, dict] = {}
    if target_set is not None:
        translated_artifact = stories_path.parent / "stories_translated.json"
        if translated_artifact.exists():
            try:
                existing_data = json.loads(translated_artifact.read_text(encoding="utf-8"))
                for entry in existing_data.get("stories", []):
                    if entry.get("story_id"):
                        existing_by_id[entry["story_id"]] = entry
            except (json.JSONDecodeError, OSError):
                existing_by_id = {}

    # Load intro/outro prompt if moderator cleaning is enabled
    intro_outro_prompt: tuple[str, str] | None = None
    if clean_moderator and session is not None and profile_namespace:
        intro_outro_prompt = _load_intro_outro_prompt(session, profile_namespace)
        if intro_outro_prompt:
            logger.info("Moderator cleaning enabled — using intro/outro prompt")

    translated_stories = []

    for i, story in enumerate(story_doc.stories):
        # Targeted rerun: preserve unaffected stories exactly as previously translated.
        if (
            target_set is not None
            and story.story_id not in target_set
            and story.story_id in existing_by_id
        ):
            translated_stories.append(existing_by_id[story.story_id])
            logger.info("Story %s preserved (not targeted by QA repair)", story.story_id)
            continue

        is_intro_outro = story.story_type in ("intro", "outro")

        # Select prompt: specialized for intro/outro, standard for everything else
        if is_intro_outro and intro_outro_prompt:
            active_system, active_user_tpl = intro_outro_prompt
            logger.info("Story %s (%s): using intro/outro prompt", story.story_id, story.story_type)
        else:
            active_system, active_user_tpl = system_prompt, user_template

        story_payload = {
            "story_id": story.story_id,
            "headline_de": story.headline_de,
            "source_segment_ids": story.source_segment_ids,
            "source_text": story.source_text or story.text_de,
            "source_confidence": story.source_confidence,
            "source_flags": story.source_flags,
        }
        body_user = active_user_tpl.replace(
            "{{ transcript }}",
            json.dumps(story_payload, ensure_ascii=False, indent=2),
        )
        story_findings = findings_by_story.get(story.story_id)
        if story_findings:
            body_user += "\n\n" + _format_story_findings_block(story_findings)
        dry_run_path = (
            Path(settings.outputs_dir) / episode_id / f"dry_run_translate_s{i:02d}_body.json"
            if settings.dry_run
            else None
        )
        translation, responses = _call_story_translation(
            story=story,
            system_prompt=active_system,
            user_message=body_user,
            settings=settings,
            dry_run_path=dry_run_path,
            adjudication_route=adjudication_route,
            adjudication_path=(
                Path(settings.outputs_dir)
                / episode_id
                / "provenance"
                / f"translate_adjudication_s{i:02d}.json"
            ),
            budget_check=(
                (lambda pending, spent=total_cost: budget_check(spent + pending))
                if budget_check is not None
                else None
            ),
        )
        total_input_tokens += sum(response.input_tokens for response in responses)
        total_output_tokens += sum(response.output_tokens for response in responses)
        total_cost += sum(response.cost_usd for response in responses)
        segments_processed += len(responses)

        # Build translated story dict
        story_dict = story.model_dump(mode="json")
        headline_tr = fix_translation_glossary(translation.translated_headline.strip())
        text_tr = fix_translation_glossary(translation.translated_text.strip())

        # Apply deterministic regex cleaning
        if clean_moderator:
            from btcedu.core.moderator_patterns import (
                clean_moderator_names,
                is_followup_program_preview,
                strip_broadcast_transitions,
            )

            if is_intro_outro:
                headline_tr = clean_moderator_names(headline_tr)
                text_tr = clean_moderator_names(text_tr)
            if is_followup_program_preview(story.source_text or story.text_de):
                headline_tr = ""
                text_tr = ""
                translation.translator_flags.append("broadcast_frame_removed")
            # Neutral flow: drop anchor transitions, program hints and sign-offs
            # from every story body (safe: only stereotyped phrases match).
            text_tr = strip_broadcast_transitions(text_tr)
            logger.info(
                "Applied moderator/transition cleaning to story %s (%s)",
                story.story_id,
                story.story_type,
            )

        story_dict["headline_tr"] = headline_tr
        story_dict["text_tr"] = text_tr
        story_dict["translator_flags"] = sorted(
            {*story.source_flags, *translation.translator_flags}
        )
        story_dict["omitted_uncertain_details"] = translation.omitted_uncertain_details
        story_dict["glossary_terms_used"] = translation.glossary_terms_used
        story_dict["narration_sha256"] = hashlib.sha256(text_tr.encode("utf-8")).hexdigest()
        translated_stories.append(story_dict)

        logger.info(
            "Translated story %s/%s: '%s' ($%.4f)",
            i + 1,
            len(story_doc.stories),
            story.story_id,
            sum(response.cost_usd for response in responses),
        )

    # Build translated StoryDocument
    translated_doc_data = story_doc.model_dump(mode="json")
    translated_doc_data["stories"] = translated_stories

    # Write stories_translated.json
    stories_translated_path = stories_path.parent / "stories_translated.json"
    stories_translated_path.write_text(
        json.dumps(translated_doc_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Wrote stories_translated.json for %s (%d stories)", episode_id, len(translated_stories)
    )

    # Build concatenated Turkish transcript (for compatibility)
    translated_parts = []
    for s in translated_stories:
        if s.get("text_tr"):
            translated_parts.append(s["text_tr"])
    translated_text = "\n\n".join(translated_parts)

    # Write transcript.tr.txt
    translated_path.parent.mkdir(parents=True, exist_ok=True)
    translated_path.write_text(translated_text, encoding="utf-8")

    return (
        translated_text,
        segments_processed,
        total_input_tokens,
        total_output_tokens,
        total_cost,
    )


def _call_story_translation(
    story,
    system_prompt: str,
    user_message: str,
    settings: "Settings",
    dry_run_path: "Path | None",
    adjudication_route: dict[str, str] | None = None,
    adjudication_path: "Path | None" = None,
    budget_check: Callable[[float], bool] | None = None,
):
    """Request and validate one structured story translation, retrying factual mismatches."""
    from btcedu.models.story_schema import StoryTranslationOutput

    responses: list[ClaudeResponse] = []
    active_message = user_message
    last_error: Exception | None = None
    last_translation = None
    last_risks: list[str] = []
    for attempt in range(2):
        last_translation = None
        last_risks = []
        if budget_check is not None and not budget_check(
            sum(response.cost_usd for response in responses)
        ):
            from btcedu.services.errors import ErrorCategory, PipelineError

            error = PipelineError(
                "Episode cost limit reached before story translation call",
                ErrorCategory.PERMANENT_COST_LIMIT,
            )
            error.input_tokens = sum(response.input_tokens for response in responses)
            error.output_tokens = sum(response.output_tokens for response in responses)
            error.cost_usd = sum(response.cost_usd for response in responses)
            raise error
        response: ClaudeResponse = call_claude(
            system_prompt=system_prompt,
            user_message=active_message,
            settings=settings,
            dry_run_path=dry_run_path,
            json_mode=True,
        )
        responses.append(response)
        try:
            data = _parse_structured_response(response.text)
            translation = StoryTranslationOutput.model_validate(data)
            _validate_story_translation_structure(story, translation)
            risks = (
                []
                if story.story_type in {"intro", "outro"}
                else _translation_fidelity_risks(
                    story.source_text or story.text_de,
                    translation.translated_text,
                )
            )
            if risks:
                last_translation = translation
                last_risks = risks
                detail = ""
                if "numbers" in risks:
                    from btcedu.core.translation_qa import extract_numeric_facts

                    source_values = [
                        fact.value
                        for fact in extract_numeric_facts(story.source_text or story.text_de)
                    ]
                    target_values = [
                        fact.value for fact in extract_numeric_facts(translation.translated_text)
                    ]
                    detail = f" (source={source_values}, target={target_values})"
                raise ValueError(f"Translation changed protected facts: {', '.join(risks)}{detail}")
            return translation, responses
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                active_message = (
                    user_message
                    + "\n\nThe previous output was invalid: "
                    + str(exc)
                    + "\nReturn corrected JSON only. Preserve every protected fact."
                )
    if last_translation is not None and last_risks and adjudication_route:
        translation, adjudication_response = _adjudicate_story_translation(
            story=story,
            candidate=last_translation,
            risks=last_risks,
            settings=settings,
            route=adjudication_route,
            adjudication_path=adjudication_path,
            budget_check=(
                (lambda cost: budget_check(sum(r.cost_usd for r in responses) + cost))
                if budget_check is not None
                else None
            ),
        )
        responses.append(adjudication_response)
        return translation, responses
    raise ValueError(f"Story translation failed validation after retry: {last_error}")


def _validate_story_translation_structure(story, translation) -> None:
    if translation.story_id != story.story_id:
        raise ValueError(
            f"Translation returned story_id {translation.story_id!r}, expected {story.story_id!r}"
        )
    if translation.source_segment_ids != story.source_segment_ids:
        raise ValueError("Translation changed source_segment_ids")
    if story.story_type not in {"intro", "outro"} and not (translation.translated_headline.strip()):
        raise ValueError("Translation omitted the story headline")


def _translation_adjudication_route(profile) -> dict[str, str] | None:
    if profile is None:
        return None
    config = profile.stage_config.get("translate", {}).get("validation_adjudication", {}) or {}
    if not config.get("enabled", False):
        return None
    provider = str(config.get("provider") or "").strip()
    model = str(config.get("model") or "").strip()
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def _adjudicate_story_translation(
    *,
    story,
    candidate,
    risks: list[str],
    settings: "Settings",
    route: dict[str, str],
    adjudication_path: "Path | None",
    budget_check: Callable[[float], bool] | None,
):
    """Independently accept a false positive or return a fact-safe replacement."""
    from btcedu.models.story_schema import StoryTranslationOutput
    from btcedu.services.errors import ErrorCategory, PipelineError

    if budget_check is not None and not budget_check(0.0):
        raise PipelineError(
            "Episode cost limit reached before translation validation adjudication",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )

    source_text = story.source_text or story.text_de
    system_prompt = (
        "You are an independent German-to-Turkish factual translation adjudicator. "
        "A producer translation was rejected by deterministic heuristics. Decide from the "
        "actual meaning, not string matching. Preserve every person, entity, number, unit, "
        "currency, date, score, casualty claim, negation and chronology statement. "
        "Return JSON only with decision accept_candidate, use_replacement, or reject; "
        "a concise reason; and replacement containing the complete StoryTranslationOutput "
        "when decision is use_replacement. Accept only when the candidate is factually "
        "equivalent. Replace when a safe correction is possible. Reject only when neither "
        "candidate nor a reliable correction can be justified."
    )
    user_message = json.dumps(
        {
            "source": {
                "story_id": story.story_id,
                "source_segment_ids": story.source_segment_ids,
                "headline_de": story.headline_de,
                "text_de": source_text,
            },
            "candidate": candidate.model_dump(mode="json"),
            "deterministic_risks": risks,
        },
        ensure_ascii=False,
        indent=2,
    )
    response = call_claude(
        system_prompt=system_prompt,
        user_message=user_message,
        settings=settings,
        json_mode=True,
        provider_override=route["provider"],
        model_override=route["model"],
    )
    data = _parse_structured_response(response.text)
    decision = str(data.get("decision") or "").strip()
    reason = str(data.get("reason") or "").strip()

    selected = None
    residual_risks: list[str] = []
    if decision == "accept_candidate":
        selected = candidate
    elif decision == "use_replacement":
        selected = StoryTranslationOutput.model_validate(data.get("replacement"))
        _validate_story_translation_structure(story, selected)
        residual_risks = _translation_fidelity_risks(
            source_text,
            selected.translated_text,
        )
    elif decision != "reject":
        raise ValueError(f"Invalid translation adjudication decision: {decision!r}")

    if adjudication_path is not None:
        adjudication_path.parent.mkdir(parents=True, exist_ok=True)
        adjudication_path.write_text(
            json.dumps(
                {
                    "story_id": story.story_id,
                    "timestamp": _utcnow().isoformat(),
                    "deterministic_risks": risks,
                    "candidate": candidate.model_dump(mode="json"),
                    "decision": decision,
                    "reason": reason,
                    "selected": selected.model_dump(mode="json") if selected is not None else None,
                    "residual_deterministic_risks": residual_risks,
                    "provider": route["provider"],
                    "model": response.model,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cost_usd": response.cost_usd,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if selected is None:
        raise ValueError(f"Independent translation adjudication rejected candidate: {reason}")
    logger.warning(
        "Independent translation adjudication %s story %s after risks: %s%s",
        decision,
        story.story_id,
        ", ".join(risks),
        (
            f"; replacement retains heuristic risks: {', '.join(residual_risks)}"
            if residual_risks
            else ""
        ),
    )
    return selected, response


def _parse_structured_response(response_text: str) -> dict:
    """Parse a JSON object while tolerating fences and malformed string quotes."""
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
        raise json.JSONDecodeError("No JSON object found", text, 0)
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Standard JSON parse failed for story translation (%s); "
            "attempting structural repair",
            exc,
        )
        try:
            repaired = json_repair.loads(candidate)
        except Exception as repair_error:  # noqa: BLE001 - preserve the original parse error
            raise exc from repair_error
        if isinstance(repaired, dict) and repaired:
            return repaired
        raise exc


_TRANSLATION_TERMS = {
    "fussball-wm": ("dünya kupası",),
    "weltmeisterschaft": ("dünya kupası",),
    "europameisterschaft": ("avrupa şampiyonası",),
    "titelverteidiger": ("son şampiyon",),
    "nachspielzeit": ("uzatma dakikaları", "hakemin eklediği dakikalar"),
    "tausend waffen": ("bin silah",),
    "juli": ("temmuz",),
}


def _translation_fidelity_risks(source_text: str, translated_text: str) -> list[str]:
    """Detect high-confidence factual regressions without judging semantic style."""
    risks: list[str] = []
    from btcedu.core.translation_qa import extract_numeric_facts

    source_numbers = Counter(fact.value for fact in extract_numeric_facts(source_text))
    translated_numbers = Counter(
        fact.value
        for fact in extract_numeric_facts(translated_text)
        if any(ch.isdigit() for ch in fact.raw)
    )
    if translated_numbers - source_numbers:
        risks.append("numbers")

    source_lower = source_text.casefold()
    translated_lower = translated_text.casefold()
    for source_term, allowed_targets in _TRANSLATION_TERMS.items():
        if re.search(rf"\b{re.escape(source_term)}\b", source_lower) and not any(
            target in translated_lower for target in allowed_targets
        ):
            risks.append(source_term)

    if "nachspielzeit" in source_lower and "uzatma devre" in translated_lower:
        risks.append("nachspielzeit_as_extra_time")
    if "tausend waffen" in source_lower and "binlerce silah" in translated_lower:
        risks.append("tausend_as_thousands")
    # Source signals that death, killing, or lethal violence is already present.
    # If any of these appear, a Turkish death/kill rendering is faithful rather
    # than invented — the German list is deliberately broad (kill/attack verbs,
    # not only literal deaths) so legitimate conflict reporting is not rejected.
    source_casualty_terms = (
        "tot",
        "töt",
        "getötet",
        "starb",
        "gestorb",
        "sterb",
        "ums leben",
        "umgekomm",
        "umbring",
        "umgebracht",
        "mord",
        "ermord",
        "opfer",
        "todes",
        "leiche",
        "erschoss",
        "gefallen",
        "vernicht",
        "attentat",
        "anschlag",
    )
    # Turkish patterns that denote an actual death in the target. Crucially,
    # "öldü" (died) must NOT match inside "öldür-" (to kill, active/hypothetical),
    # which is a faithful rendering of threats like "umbringen"/"vernichten".
    translated_casualty_patterns = (
        r"hayat[ıi]n[ıi] kaybet",
        r"hayat[ıi]n[ıi] yitir",
        r"yaşam[ıi]n[ıi] yitir",
        r"can kayb[ıi]",
        r"öldü(?!r)",
        r"öldürül",
        r"ölen\b",
        r"ölmüş",
        r"\bölüm\w*\b",
    )
    if not any(term in source_lower for term in source_casualty_terms) and any(
        re.search(pattern, translated_lower) for pattern in translated_casualty_patterns
    ):
        risks.append("invented_casualty")
    return sorted(set(risks))


def _load_intro_outro_prompt(
    session: "Session",
    profile_namespace: str,
) -> tuple[str, str] | None:
    """Load the specialized intro/outro translation prompt.

    Returns (system_prompt, user_template) tuple, or None if the template
    does not exist (falls back to standard translate prompt).
    """
    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path(
        "translate_intro_outro.md", profile=profile_namespace
    )
    if not template_file.exists():
        logger.info(
            "No intro/outro prompt at %s — using standard translate prompt",
            template_file,
        )
        return None

    _, body = registry.load_template(template_file)
    return _split_prompt(body)
