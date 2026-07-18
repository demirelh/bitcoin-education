"""Turkish translation: Faithful German→Turkish translation of corrected transcripts."""

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
) -> TranslationResult:
    """Translate a corrected German transcript to Turkish.

    Reads the corrected German transcript, sends it to Claude for faithful
    translation to Turkish, writes the translated transcript and provenance.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: If True, re-translate even if output exists.

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
        if getattr(settings, "qa_review_enabled", False):
            from btcedu.core.qa_reviewer import format_qa_feedback

            qa_feedback = format_qa_feedback(settings, episode_id)
            if qa_feedback:
                feedback_parts.append(qa_feedback)
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
            )
        else:
            # Standard full-transcript translation
            segments = _segment_text(corrected_text)
            segments_processed = len(segments)
            translated_segments: list[str] = []

            for i, segment in enumerate(segments):
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
) -> tuple[str, int, int, int, float]:
    """Translate each story in a StoryDocument individually.

    Translates story.text_de -> story.text_tr and story.headline_de -> story.headline_tr
    for each story in the StoryDocument. Writes stories_translated.json and the concatenated
    transcript.tr.txt.

    When clean_moderator is True and story_type is "intro" or "outro", uses a
    specialized prompt that neutralizes moderator greetings and broadcast names,
    followed by deterministic regex cleaning of moderator names.

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

    # Load intro/outro prompt if moderator cleaning is enabled
    intro_outro_prompt: tuple[str, str] | None = None
    if clean_moderator and session is not None and profile_namespace:
        intro_outro_prompt = _load_intro_outro_prompt(session, profile_namespace)
        if intro_outro_prompt:
            logger.info("Moderator cleaning enabled — using intro/outro prompt")

    translated_stories = []

    for i, story in enumerate(story_doc.stories):
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
                strip_broadcast_transitions,
            )

            if is_intro_outro:
                headline_tr = clean_moderator_names(headline_tr)
                text_tr = clean_moderator_names(text_tr)
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
):
    """Request and validate one structured story translation, retrying factual mismatches."""
    from btcedu.models.story_schema import StoryTranslationOutput

    responses: list[ClaudeResponse] = []
    active_message = user_message
    last_error: Exception | None = None
    for attempt in range(2):
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
            if translation.story_id != story.story_id:
                raise ValueError(
                    f"Translation returned story_id {translation.story_id!r}, "
                    f"expected {story.story_id!r}"
                )
            if translation.source_segment_ids != story.source_segment_ids:
                raise ValueError("Translation changed source_segment_ids")
            if story.story_type not in {"intro", "outro"} and not (
                translation.translated_headline.strip()
            ):
                raise ValueError("Translation omitted the story headline")
            risks = _translation_fidelity_risks(
                story.source_text or story.text_de,
                translation.translated_text,
            )
            if risks:
                raise ValueError(f"Translation changed protected facts: {', '.join(risks)}")
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
    raise ValueError(f"Story translation failed validation after retry: {last_error}")


def _parse_structured_response(response_text: str) -> dict:
    """Parse a JSON object while tolerating a surrounding markdown fence."""
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
    return json.loads(text[start : end + 1])


_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,:]\d+)*(?!\w)")
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
    source_numbers = Counter(_NUMBER_RE.findall(source_text.casefold()))
    translated_numbers = Counter(_NUMBER_RE.findall(translated_text.casefold()))
    if source_numbers != translated_numbers:
        risks.append("numbers")

    source_lower = source_text.casefold()
    translated_lower = translated_text.casefold()
    for source_term, allowed_targets in _TRANSLATION_TERMS.items():
        if source_term in source_lower and not any(
            target in translated_lower for target in allowed_targets
        ):
            risks.append(source_term)

    if "nachspielzeit" in source_lower and "uzatma devre" in translated_lower:
        risks.append("nachspielzeit_as_extra_time")
    if "tausend waffen" in source_lower and "binlerce silah" in translated_lower:
        risks.append("tausend_as_thousands")
    source_casualty_terms = ("tot", "getötet", "starb", "opfer", "todes")
    translated_casualty_terms = (
        "hayatını kaybet",
        "öldü",
        "öldürüldü",
        "can kaybı",
    )
    if not any(term in source_lower for term in source_casualty_terms) and any(
        term in translated_lower for term in translated_casualty_terms
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
