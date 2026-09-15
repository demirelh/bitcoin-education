"""Image generation: Create visual assets from chapter JSON via gpt-image-1 (formerly DALL-E 3)."""

import fcntl
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.branding_guard import branding_config
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType
from btcedu.services.claude_service import call_claude
from btcedu.services.errors import ErrorCategory, PipelineError, classify_error, is_transient
from btcedu.services.image_gen_service import (
    ImageGenRequest,
    ImageGenResponse,
)

logger = logging.getLogger(__name__)

# Visual types that need API generation vs. template/placeholder
VISUAL_TYPES_NEEDING_GENERATION = {"diagram", "b_roll", "screen_share"}


def _read_weather_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Invalid weather overrides: {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid weather overrides: {path}: expected an object")
    return {str(key): str(value) for key, value in loaded.items() if value in {"weather", "normal"}}


# Transliteration map for characters that NFKD does not decompose to an ASCII
# base (Turkish dotless-i, German eszett, etc.). Applied before NFKD so chapter
# titles yield filesystem- and URL-safe ASCII filenames that survive
# werkzeug.secure_filename() unchanged when served back to the dashboard.
_FILENAME_TRANSLIT = str.maketrans(
    {
        "ı": "i",
        "İ": "I",
        "ş": "s",
        "Ş": "S",
        "ğ": "g",
        "Ğ": "G",
        "ç": "c",
        "Ç": "C",
        "ö": "o",
        "Ö": "O",
        "ü": "u",
        "Ü": "U",
        "ä": "a",
        "Ä": "A",
        "ß": "ss",
        "ø": "o",
        "å": "a",
    }
)


def _slugify_filename_part(text: str, max_len: int = 30) -> str:
    """Turn a chapter title into an ASCII, URL-safe filename fragment.

    Non-ASCII characters (Turkish/German diacritics) are transliterated so the
    resulting filename matches what ``secure_filename`` preserves, ensuring the
    image-serving route can locate the file on disk.
    """
    text = text.translate(_FILENAME_TRANSLIT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    text = text[:max_len].strip("_")
    return text or "chapter"


# Profile imagegen.provider values that select the generative (Flux/Ideogram/DALL-E)
# image path with per-chapter smart routing.
GENERATIVE_PROVIDERS = {"generative", "smart", "auto", "flux", "ideogram", "dalle3"}

# Extra visual types that should be generated (not template placeholders) when a
# profile uses the generative path — e.g. news title cards rendered by Ideogram.
GENERATIVE_EXTRA_TYPES = {"title_card"}

# Visual types best rendered by Ideogram (legible in-image text / infographics).
_TEXT_IN_IMAGE_TYPES = {
    "quote",
    "chart",
    "text_heavy",
    "title_card",
    "thumbnail",
    "diagram",
    "infographic",
    "map",
}


def _route_provider_for_chapter(chapter) -> str:
    """Pick the best image provider for a chapter's visual type.

    - Text-in-image / infographic chapters (title cards, charts, labelled weather
      maps) → Ideogram (renders legible text far better than Flux/DALL-E).
    - Lower thirds and other overlays are ignored here: the renderer draws them,
      so they say nothing about what the picture itself must contain.
    - Explicit stock placeholders → DALL-E 3 (until Pexels fallback kicks in).
    - Photoreal editorial footage (b_roll, hero, lifestyle) → Flux (quality/cost).
    """
    raw_type = getattr(getattr(chapter, "visual", None), "type", "") or ""
    # VisualType is a (str, Enum); str() yields "VisualType.X", so read .value.
    visual_type = str(getattr(raw_type, "value", raw_type)).lower()
    # Overlays deliberately do not influence the choice: they are drawn by the
    # renderer with drawtext, never by the image model. Routing on them sent
    # every chapter with a lower third to the text-rendering provider, which then
    # wrote nonsense words into the picture.
    if visual_type in _TEXT_IN_IMAGE_TYPES:
        return "ideogram"
    if visual_type == "stock":
        return "dalle3"
    return "flux"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Exact-data visual categories that must NOT be produced by a generative model
# (they carry precise numbers/labels/boundaries that a model would hallucinate).
_EXACT_DATA_KEYWORDS = {
    "weather": ("hava durumu", "hava tahmini", "wetter", "weather", "sıcaklık", "derece"),
    "chart": ("chart", "grafik", "diagram", "diyagram", "istatistik", "statistik"),
    "table": ("table", "tablo", "tabelle"),
    "map": ("harita", "karte", " map "),
    "election": ("seçim", "wahl", "election", "sandık", "oy oranı", "stimmen"),
    "timeline": ("timeline", "zaman çizelgesi", "zeitleiste", "kronoloji"),
}


def _visual_deterministic_spec(visual) -> dict | None:
    """Return the explicit deterministic asset spec on a visual, if any."""
    spec = getattr(visual, "deterministic", None)
    if isinstance(spec, dict) and spec:
        return spec
    return None


def _detect_exact_data_category(visual) -> str | None:
    """Best-effort exact-data category from an explicit spec or description keywords."""
    spec = _visual_deterministic_spec(visual)
    if spec:
        return str(spec.get("category") or "generic").strip().lower() or "generic"
    haystack = " ".join(
        str(getattr(visual, attr, "") or "") for attr in ("description", "image_prompt")
    ).lower()
    for category, keywords in _EXACT_DATA_KEYWORDS.items():
        if any(
            re.search(rf"(?<!\w){re.escape(keyword.strip())}(?!\w)", haystack)
            for keyword in keywords
        ):
            return category
    return None


def _should_render_deterministic(visual, imagegen_cfg: dict) -> bool:
    """Route exact-data visuals to the local deterministic renderer.

    Always when an explicit ``deterministic`` spec is attached (unambiguous
    opt-in). Otherwise only when the profile enables ``deterministic_exact_data``
    AND a known exact-data category is detected — so provider routing for every
    other chapter is untouched and there are no surprise provider switches.
    """
    if _visual_deterministic_spec(visual):
        return True
    if imagegen_cfg.get("deterministic_exact_data"):
        return _detect_exact_data_category(visual) is not None
    return False


def _weather_detection_for_chapter(chapter, weather_config: dict | None = None):
    """Run the canonical multi-signal weather detector for one chapter."""
    from btcedu.core.weather.detector import detect_weather_story

    narration_text = getattr(getattr(chapter, "narration", None), "text", "") or ""
    source_text = getattr(chapter, "source_text", None)
    story_type = getattr(chapter, "story_type", None)
    metadata = getattr(chapter, "metadata", None)
    return detect_weather_story(
        title=getattr(chapter, "title", "") or "",
        story_type=story_type if isinstance(story_type, str) else None,
        narration_text=narration_text,
        source_text=source_text if isinstance(source_text, str) else "",
        metadata=metadata if isinstance(metadata, dict) else {},
        min_confidence=((weather_config or {}).get("detection", {}) or {}).get(
            "min_confidence", 0.75
        ),
    )


def _load_existing_image_entries(manifest_path: Path) -> dict[str, list[dict]]:
    """Load {chapter_id: [entries]} from an existing image manifest, or {}.

    A chapter can hold more than one picture — one per presenter block — so the
    entries are grouped, ordered by beat index, with the chapter image first.
    """
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        grouped: dict[str, list[dict]] = {}
        for entry in manifest.get("images", []):
            grouped.setdefault(entry["chapter_id"], []).append(entry)
        for entries in grouped.values():
            entries.sort(key=lambda e: int((e.get("metadata") or {}).get("beat_index") or 0))
        return grouped
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def _image_inputs_unchanged(provenance_path: Path, chapters_hash: str, prompt_hash: str) -> bool:
    """True when the recorded imagegen inputs match the current chapters/prompt."""
    if not provenance_path.exists():
        return False
    try:
        prov = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (
        prov.get("input_content_hash") == chapters_hash and prov.get("prompt_hash") == prompt_hash
    )


def _chapters_needing_regen(
    existing_entries: dict[str, list[dict]], chapters_doc, output_dir: Path
) -> set[str]:
    """Chapter ids whose image entry is missing, failed, or has no file on disk."""
    base_dir = output_dir.parent  # outputs/{episode_id}/
    need: set[str] = set()
    for ch in chapters_doc.chapters:
        entries = existing_entries.get(ch.chapter_id)
        if not entries:
            need.add(ch.chapter_id)
            continue
        expected_beats = max(1, len(_visual_beats(ch)))
        if len(entries) < expected_beats:
            need.add(ch.chapter_id)
            continue
        for entry in entries:
            if entry.get("generation_method") == "failed":
                need.add(ch.chapter_id)
                break
            file_path = entry.get("file_path")
            if not file_path or not (base_dir / file_path).exists():
                need.add(ch.chapter_id)
                break
    return need


def _clear_stale_marker(marker: Path) -> None:
    """Remove a consumed .stale marker so it doesn't force endless regeneration."""
    try:
        if marker.exists():
            marker.unlink()
    except OSError as e:  # pragma: no cover - defensive
        logger.warning("Could not remove stale marker %s: %s", marker, e)


def _failed_image_entry(chapter, error: str) -> "ImageEntry":
    """A per-chapter failed entry. Image failures never block narration/pipeline."""
    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visual.type,
        file_path=f"images/{chapter.chapter_id}_failed.png",
        prompt=None,
        generation_method="failed",
        model=None,
        size="0x0",
        mime_type="image/png",
        size_bytes=0,
        metadata={"error": error},
    )


@dataclass
class ImageEntry:
    """Metadata for a single generated or placeholder image."""

    chapter_id: str
    chapter_title: str
    visual_type: str
    file_path: str  # Relative path from episode outputs dir
    prompt: str | None  # DALL-E prompt (null for template placeholders)
    generation_method: str  # "dalle3", "template", "failed", "skipped"
    model: str | None  # Model used (null for templates)
    size: str  # Image dimensions
    mime_type: str
    size_bytes: int
    metadata: dict  # Additional generation params, cost, etc.
    asset_type: str = "photo"


@dataclass
class ImageGenResult:
    """Summary of image generation operation for one episode."""

    episode_id: str
    images_path: Path
    manifest_path: Path
    provenance_path: Path
    image_count: int = 0
    generated_count: int = 0  # Actually generated via API
    template_count: int = 0  # Placeholders created
    deterministic_count: int = 0  # Rendered locally from exact data (no generative model)
    failed_count: int = 0  # Failed generations
    input_tokens: int = 0  # From LLM prompt generation
    output_tokens: int = 0
    cost_usd: float = 0.0  # Total: LLM + image generation
    skipped: bool = False


def generate_images(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    chapter_id: str | None = None,
) -> ImageGenResult:
    """Generate images for all chapters (or a specific chapter) in an episode.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, regenerate all images even if they exist
        chapter_id: If provided, only regenerate this specific chapter

    Returns:
        ImageGenResult with paths, counts, tokens, cost, and skip status

    Raises:
        ValueError: If episode/chapter not found or chapters.json invalid
        RuntimeError: If image generation API fails
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # V2 pipeline only
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "Image generation is only supported for v2 pipeline."
        )

    # Check episode status (allow CHAPTERIZED, FRAMES_EXTRACTED or IMAGES_GENERATED
    # for idempotency). In the v2 pipeline the frameextract stage runs immediately
    # before imagegen and advances status to FRAMES_EXTRACTED (a no-op for the
    # generative/stock profiles), so that is a valid precondition too.
    if (
        episode.status
        not in (
            EpisodeStatus.CHAPTERIZED,
            EpisodeStatus.FRAMES_EXTRACTED,
            EpisodeStatus.IMAGES_GENERATED,
        )
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'chapterized', 'frames_extracted' or 'images_generated'. "
            "Use --force to override."
        )

    # Resolve paths
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not chapters_path.exists():
        raise FileNotFoundError(
            f"Chapters file not found for episode {episode_id}: {chapters_path}"
        )

    # Load profile for profile-aware placeholder colors etc.
    _profile_accent = "#F7931A"
    _profile_style_prefix = None
    _imagegen_cfg: dict = {}
    _profile = None
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        _profile_name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
        _profile = _get_profile_registry(settings).get(_profile_name)
        _render_cfg = (_profile.stage_config.get("render", {}) if _profile else {}) or {}
        _profile_accent = _render_cfg.get("accent_color") or "#F7931A"
        _imagegen_cfg = (_profile.stage_config.get("imagegen", {}) if _profile else {}) or {}
        _branding_cfg = branding_config(settings, _profile_name)
        _profile_style_prefix = _imagegen_cfg.get("style_prefix", None)
    except Exception:
        pass

    _weather_cfg: dict = (_profile.stage_config.get("weather", {}) if _profile else {}) or {}

    output_dir = Path(settings.outputs_dir) / episode_id / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    weather_overrides_path = output_dir / "weather_overrides.json"
    weather_overrides_lock_path = output_dir / ".weather_overrides.lock"
    with weather_overrides_lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        weather_overrides = _read_weather_overrides(weather_overrides_path)
    manifest_path = output_dir / "manifest.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "imagegen_provenance.json"
    )

    # Load chapters
    chapters_doc = _load_chapters(chapters_path)
    chapters_hash = _compute_chapters_content_hash(
        chapters_doc,
        weather_config=_weather_cfg,
        weather_overrides=weather_overrides,
    )

    # Load and register prompt (profile can override template file)
    registry = PromptRegistry(session)
    _template_name = _imagegen_cfg.get("prompt_template", "imagegen.md") or "imagegen.md"
    template_file = TEMPLATES_DIR / _template_name
    if not template_file.exists():
        logger.warning(
            "Profile-requested imagegen template %s not found, falling back to imagegen.md",
            _template_name,
        )
        template_file = TEMPLATES_DIR / "imagegen.md"
    _prompt_key = template_file.stem  # 'imagegen' or 'imagegen_news'
    prompt_version = registry.register_version(_prompt_key, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Idempotency check (skip if not force and not chapter-specific and output is current)
    recover_ids: set[str] | None = None
    if not force and chapter_id is None:
        if _is_image_gen_current(
            manifest_path,
            provenance_path,
            chapters_hash,
            prompt_content_hash,
            {chapter.chapter_id for chapter in chapters_doc.chapters},
        ):
            logger.info(
                "Image generation is current for %s (use --force to regenerate)", episode_id
            )
            if episode.status != EpisodeStatus.IMAGES_GENERATED:
                episode.status = EpisodeStatus.IMAGES_GENERATED
                episode.error_message = None
                session.commit()
            existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            return ImageGenResult(
                episode_id=episode_id,
                images_path=output_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                image_count=existing_provenance.get("image_count", 0),
                generated_count=existing_provenance.get("generated_count", 0),
                template_count=existing_provenance.get("template_count", 0),
                failed_count=existing_provenance.get("failed_count", 0),
                input_tokens=existing_provenance.get("input_tokens", 0),
                output_tokens=existing_provenance.get("output_tokens", 0),
                cost_usd=existing_provenance.get("cost_usd", 0.0),
                skipped=True,
            )
        # Not fully current. When the inputs are unchanged and there is no stale
        # marker, the only reason is previously-failed or missing chapters — retry
        # just those (chapter recovery) instead of regenerating everything.
        stale_marker = manifest_path.with_suffix(".json.stale")
        if not stale_marker.exists() and _image_inputs_unchanged(
            provenance_path, chapters_hash, prompt_content_hash
        ):
            _existing = _load_existing_image_entries(manifest_path)
            if _existing:
                recover_ids = _chapters_needing_regen(_existing, chapters_doc, output_dir)
                if recover_ids:
                    logger.info(
                        "Recovering %d failed/missing image chapters for %s: %s",
                        len(recover_ids),
                        episode_id,
                        sorted(recover_ids),
                    )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="imagegen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Create image generation service via factory (supports dalle3/flux/ideogram)
        from btcedu.services.image_provider_factory import get_image_service

        image_service = get_image_service(settings)

        # Load existing manifest for partial regeneration / chapter recovery
        existing_entries = {}
        if (chapter_id or recover_ids is not None) and manifest_path.exists():
            existing_entries = _load_existing_image_entries(manifest_path)
            if not existing_entries:
                logger.warning(
                    f"Could not load existing manifest from {manifest_path}, will regenerate all"
                )

        # Filter chapters to process. A targeted rerender is safe only when the
        # existing manifest can supply every untouched chapter; otherwise rebuild
        # the complete manifest instead of silently dropping entries.
        chapters_to_process = chapters_doc.chapters
        if chapter_id:
            if not any(c.chapter_id == chapter_id for c in chapters_doc.chapters):
                raise ValueError(f"Chapter {chapter_id} not found in chapters.json")
            untouched_ids = {
                chapter.chapter_id
                for chapter in chapters_doc.chapters
                if chapter.chapter_id != chapter_id
            }
            if existing_entries and untouched_ids.issubset(existing_entries):
                chapters_to_process = [
                    c for c in chapters_doc.chapters if c.chapter_id == chapter_id
                ]
            elif existing_entries:
                logger.warning(
                    "Existing image manifest is incomplete; rebuilding all chapters for %s",
                    episode_id,
                )

        # Process each chapter
        image_entries = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        generated_count = 0
        template_count = 0
        failed_count = 0
        deterministic_count = 0

        def _check_cost_limit(*, before_call: bool) -> None:
            cumulative = _get_episode_total_cost(session, episode_id) + total_cost
            exceeded = (
                cumulative >= settings.max_episode_cost_usd
                if before_call
                else cumulative > settings.max_episode_cost_usd
            )
            if exceeded:
                relation = ">=" if before_call else ">"
                raise PipelineError(
                    f"Episode cost limit reached during image generation: "
                    f"${cumulative:.4f} {relation} ${settings.max_episode_cost_usd:.4f}",
                    ErrorCategory.PERMANENT_COST_LIMIT,
                )

        output_dir.mkdir(parents=True, exist_ok=True)

        # When the profile uses the generative path (Flux/Ideogram/DALL-E), route
        # each chapter to the best provider per visual type and also generate
        # title cards (via Ideogram) instead of flat template placeholders.
        _profile_provider = str(_imagegen_cfg.get("provider", "") or "").lower()
        _generative_profile = _profile_provider in GENERATIVE_PROVIDERS
        _smart_routing = _generative_profile or getattr(settings, "image_gen_smart_routing", False)
        # The profile names a second provider for exactly this case; without it
        # a single provider outage turned into a dangling manifest entry.
        _fallback_provider = str(_imagegen_cfg.get("fallback_provider", "") or "").lower()

        for chapter in chapters_to_process:
            # Skip if no visual or processing only a specific chapter
            visual = chapter.visual
            if not visual:
                logger.warning(f"Chapter {chapter.chapter_id} has no visual, skipping")
                continue

            weather_override = weather_overrides.get(chapter.chapter_id)
            weather_detection = _weather_detection_for_chapter(chapter, _weather_cfg)

            # Reuse the existing entry when this chapter is not the target of a
            # single-chapter regen, or (during recovery) is not one of the
            # failed/missing chapters being retried.
            _reuse_existing = chapter.chapter_id in existing_entries and (
                (chapter_id and chapter.chapter_id != chapter_id)
                or (recover_ids is not None and chapter.chapter_id not in recover_ids)
            )
            if _reuse_existing:
                for existing_entry_dict in existing_entries[chapter.chapter_id]:
                    image_entry = ImageEntry(**existing_entry_dict)
                    image_entries.append(image_entry)
                    if image_entry.generation_method == "deterministic":
                        deterministic_count += 1
                    elif image_entry.generation_method == "template":
                        template_count += 1
                    elif image_entry.generation_method != "failed":
                        generated_count += 1
                continue

            if weather_override == "weather" or (
                weather_override != "normal" and weather_detection.is_weather_story
            ):
                try:
                    image_entry = _render_weather_chapter(
                        chapter,
                        output_dir,
                        accent_color=_profile_accent,
                        weather_config=_weather_cfg,
                        broadcast_date=(
                            episode.published_at.date() if episode.published_at else None
                        ),
                    )
                    deterministic_count += 1
                except Exception as error:
                    raise RuntimeError(
                        f"Weather visual failed for chapter {chapter.chapter_id}: {error}"
                    ) from error
                image_entries.append(image_entry)
                _create_media_asset_record(session, episode_id, image_entry, prompt_version.id)
                continue

            # Exact-data visuals (maps/charts/tables/weather/election/timelines)
            # are rendered locally from their exact spec — never a generative
            # model — so numbers/labels/boundaries are never hallucinated. This
            # never affects narration and, on error, degrades to a failed entry.
            should_render_deterministic = _should_render_deterministic(visual, _imagegen_cfg)
            if weather_override == "normal" and _detect_exact_data_category(visual) == "weather":
                should_render_deterministic = False
            if should_render_deterministic:
                deterministic_category = _detect_exact_data_category(visual)
                try:
                    image_entry = _render_deterministic_visual(
                        chapter,
                        output_dir,
                        accent_color=_profile_accent,
                        weather_config=_weather_cfg,
                    )
                    deterministic_count += 1
                except Exception as e:  # noqa: BLE001 - image failure never blocks narration
                    logger.error(
                        f"Deterministic visual failed for chapter {chapter.chapter_id}: {e}"
                    )
                    if deterministic_category == "weather":
                        raise RuntimeError(
                            f"Weather visual failed for chapter {chapter.chapter_id}: {e}"
                        ) from e
                    image_entry = _failed_image_entry(chapter, str(e))
                    failed_count += 1
                image_entries.append(image_entry)
                if image_entry.generation_method != "failed":
                    _create_media_asset_record(session, episode_id, image_entry, prompt_version.id)
                continue

            # Check if generation is needed for this visual type
            if _needs_generation(visual.type) or (
                _generative_profile and visual.type in GENERATIVE_EXTRA_TYPES
            ):
                # Reset per chapter: the fallback below must never reuse the
                # prompt of the previous chapter when this one has no prompt yet.
                image_prompt = None
                try:
                    beats = _visual_beats(chapter)
                    if beats:
                        # A chapter with presenter blocks must have all of its
                        # shots written the same way, so the first shot goes
                        # through the beat prompt like the others.
                        _check_cost_limit(before_call=True)
                        (
                            image_prompt,
                            prompt_tokens,
                            completion_tokens,
                            prompt_cost,
                        ) = _generate_beat_prompt(chapter, beats[0], template_body, settings)
                        total_input_tokens += prompt_tokens
                        total_output_tokens += completion_tokens
                        total_cost += prompt_cost
                        _check_cost_limit(before_call=False)
                    elif visual.image_prompt:
                        # Use prompt from chapter JSON if provided
                        image_prompt = visual.image_prompt
                        prompt_tokens, completion_tokens, prompt_cost = 0, 0, 0.0
                    else:
                        _check_cost_limit(before_call=True)
                        # Generate prompt via LLM
                        (
                            image_prompt,
                            prompt_tokens,
                            completion_tokens,
                            prompt_cost,
                        ) = _generate_image_prompt(chapter, template_body, settings)
                        total_input_tokens += prompt_tokens
                        total_output_tokens += completion_tokens
                        total_cost += prompt_cost
                        _check_cost_limit(before_call=False)

                    # Generate image
                    _check_cost_limit(before_call=True)
                    image_entry = _generate_single_image(
                        chapter,
                        image_prompt,
                        image_service,
                        output_dir,
                        settings,
                        style_prefix_override=_profile_style_prefix,
                        smart_routing=_smart_routing,
                        branding=_branding_cfg,
                    )
                    total_cost += image_entry.metadata.get("cost_usd", 0.0)
                    _check_cost_limit(before_call=False)
                    generated_count += 1

                except PipelineError:
                    raise
                except Exception as e:
                    logger.error(f"Failed to generate image for chapter {chapter.chapter_id}: {e}")
                    image_entry = None
                    if _fallback_provider and image_prompt:
                        try:
                            from btcedu.services.image_provider_factory import (
                                get_image_service as _get_fallback_service,
                            )

                            logger.info(
                                "Retrying chapter %s with fallback provider %s",
                                chapter.chapter_id,
                                _fallback_provider,
                            )
                            _check_cost_limit(before_call=True)
                            image_entry = _generate_single_image(
                                chapter,
                                image_prompt,
                                _get_fallback_service(settings, provider=_fallback_provider),
                                output_dir,
                                settings,
                                style_prefix_override=_profile_style_prefix,
                                smart_routing=False,
                                branding=_branding_cfg,
                            )
                            total_cost += image_entry.metadata.get("cost_usd", 0.0)
                            _check_cost_limit(before_call=False)
                            generated_count += 1
                        except PipelineError:
                            raise
                        except Exception as fallback_error:
                            logger.error(
                                "Fallback provider %s also failed for chapter %s: %s",
                                _fallback_provider,
                                chapter.chapter_id,
                                fallback_error,
                            )
                            image_entry = None
                    if image_entry is None:
                        image_entry = _failed_image_entry(chapter, str(e))
                        failed_count += 1

            else:
                # Create template placeholder for title_card/talking_head
                image_entry = _create_template_placeholder(
                    chapter, output_dir, accent_color=_profile_accent
                )
                template_count += 1

            image_entries.append(image_entry)

            # Record MediaAsset in database
            if image_entry.generation_method != "failed":
                _create_media_asset_record(session, episode_id, image_entry, prompt_version.id)

            # One picture per presenter block. The first block already has the
            # chapter image above, so only the later blocks are generated here.
            if image_entry.generation_method != "failed":
                for beat in _visual_beats(chapter)[1:]:
                    beat_index = int(beat.get("beat_index") or 0)
                    try:
                        _check_cost_limit(before_call=True)
                        (
                            beat_prompt,
                            beat_in,
                            beat_out,
                            beat_prompt_cost,
                        ) = _generate_beat_prompt(chapter, beat, template_body, settings)
                        total_input_tokens += beat_in
                        total_output_tokens += beat_out
                        total_cost += beat_prompt_cost
                        _check_cost_limit(before_call=False)

                        _check_cost_limit(before_call=True)
                        beat_entry = _generate_single_image(
                            chapter,
                            beat_prompt,
                            image_service,
                            output_dir,
                            settings,
                            style_prefix_override=_profile_style_prefix,
                            smart_routing=_smart_routing,
                            branding=_branding_cfg,
                            beat_index=beat_index,
                        )
                        total_cost += beat_entry.metadata.get("cost_usd", 0.0)
                        _check_cost_limit(before_call=False)
                        generated_count += 1
                        image_entries.append(beat_entry)
                        _create_media_asset_record(
                            session, episode_id, beat_entry, prompt_version.id
                        )
                    except PipelineError:
                        raise
                    except Exception as e:
                        # A missing beat picture is not fatal: the renderer holds
                        # the previous shot for that block instead.
                        logger.warning(
                            "Beat %d of chapter %s has no picture (%s); the previous "
                            "shot will be held",
                            beat_index,
                            chapter.chapter_id,
                            e,
                        )

        if chapter_id and existing_entries:
            regenerated: dict[str, list[ImageEntry]] = {}
            for entry in image_entries:
                regenerated.setdefault(entry.chapter_id, []).append(entry)
            merged: list[ImageEntry] = []
            for chapter in chapters_doc.chapters:
                if chapter.chapter_id in regenerated:
                    merged.extend(regenerated[chapter.chapter_id])
                elif chapter.chapter_id in existing_entries:
                    merged.extend(ImageEntry(**raw) for raw in existing_entries[chapter.chapter_id])
            image_entries = merged

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": chapters_doc.schema_version,
            "generated_at": _utcnow().isoformat(),
            "images": [asdict(entry) for entry in image_entries],
        }
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "imagegen",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "prompt_name": "imagegen",
            "prompt_version": prompt_version.version,
            "prompt_hash": prompt_content_hash,
            "model": settings.claude_model,
            "image_gen_model": getattr(settings, "image_gen_model", "gpt-image-1"),
            "input_files": [str(chapters_path)],
            "input_content_hash": chapters_hash,
            "output_files": [str(manifest_path)]
            + [str(output_dir / entry.file_path) for entry in image_entries],
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "image_count": len(image_entries),
            "generated_count": generated_count,
            "template_count": template_count,
            "deterministic_count": deterministic_count,
            "failed_count": failed_count,
            "recovered": sorted(recover_ids) if recover_ids else [],
            "cost_usd": total_cost,
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Clear invalidation only if no override changed while image generation
        # was running. The API uses the same inter-process lock.
        with weather_overrides_lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            current_weather_overrides = _read_weather_overrides(weather_overrides_path)
            if current_weather_overrides != weather_overrides:
                raise RuntimeError(
                    "Weather overrides changed during image generation; rerun imagegen"
                )
            _clear_stale_marker(manifest_path.with_suffix(".json.stale"))

        # A failed chapter entry points at a file that was never written. The
        # renderer only discovers that after tts/anchorgen have run, and a retry
        # then resumes past imagegen and can never repair it. Fail here instead,
        # where a rerun regenerates exactly the missing chapters. The manifest
        # and provenance above are already written, so that recovery is partial.
        unresolved = [
            entry.chapter_id
            for entry in image_entries
            if entry.generation_method == "failed"
            or not (output_dir.parent / entry.file_path).exists()
        ]
        if unresolved:
            unresolved_ids = sorted(set(unresolved))
            # The per-chapter error (provider auth failure, quota exhaustion,
            # transient 5xx, ...) is recorded in the failed entry's metadata.
            # Re-classify it instead of always reporting a transient server
            # error: a 401/403 from every provider needs a credential fix, not
            # an automatic retry that will fail identically forever.
            failure_messages = {
                entry.chapter_id: entry.metadata.get("error")
                for entry in image_entries
                if entry.chapter_id in unresolved_ids and entry.generation_method == "failed"
            }
            category = ErrorCategory.TRANSIENT_SERVER
            for message in failure_messages.values():
                if not message:
                    continue
                chapter_category = classify_error(RuntimeError(message))
                if not is_transient(chapter_category):
                    category = chapter_category
                    break
            detail = "; ".join(
                f"{chapter_id}: {message}"
                for chapter_id, message in sorted(failure_messages.items())
                if message
            )
            message = (
                "Image generation left chapter(s) without a usable picture: "
                f"{', '.join(unresolved_ids)}. Rerun imagegen to regenerate them."
            )
            if detail:
                message += f" Details — {detail}"
            raise PipelineError(message, category)

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="images",
            file_path=str(manifest_path.relative_to(Path(settings.outputs_dir) / episode_id)),
            model=settings.image_gen_model,
            prompt_hash=prompt_content_hash,
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Mark downstream stages stale (RENDER only — images never affect TTS/narration)
        _mark_downstream_stale(episode_id, Path(settings.outputs_dir))

        # Update episode status
        episode.status = EpisodeStatus.IMAGES_GENERATED
        episode.error_message = None
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost
        session.commit()

        logger.info(
            f"Image generation complete for {episode_id}: {generated_count} generated, "
            f"{deterministic_count} deterministic, {template_count} placeholders, "
            f"{failed_count} failed (${total_cost:.3f})"
        )

        return ImageGenResult(
            episode_id=episode_id,
            images_path=output_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            image_count=len(image_entries),
            generated_count=generated_count,
            template_count=template_count,
            deterministic_count=deterministic_count,
            failed_count=failed_count,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        pipeline_run.input_tokens = locals().get("total_input_tokens", 0)
        pipeline_run.output_tokens = locals().get("total_output_tokens", 0)
        pipeline_run.estimated_cost_usd = locals().get("total_cost", 0.0)
        if isinstance(e, PipelineError) and e.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
        episode.error_message = str(e)
        session.commit()
        logger.error(f"Image generation failed for {episode_id}: {e}")
        raise


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _compute_chapters_content_hash(
    chapters_doc: ChapterDocument,
    *,
    weather_config: dict | None = None,
    weather_overrides: dict[str, str] | None = None,
) -> str:
    """Compute SHA-256 hash of relevant chapter fields for change detection.

    For weather chapters, includes the weather schema/renderer version so that
    renderer upgrades invalidate the cache and re-run imagegen.
    """
    from btcedu.core.weather.models import RENDERER_VERSION as WEATHER_RENDERER_VERSION
    from btcedu.core.weather.models import SCHEMA_VERSION as WEATHER_SCHEMA_VERSION

    # Hash only fields that affect image generation
    relevant_data = {
        "schema_version": chapters_doc.schema_version,
        "chapters": [],
    }
    for ch in chapters_doc.chapters:
        chapter_entry: dict = {
            "chapter_id": ch.chapter_id,
            "title": ch.title,
            "visual": (
                {
                    "type": ch.visual.type,
                    "description": ch.visual.description,
                    "image_prompt": ch.visual.image_prompt,
                    "deterministic": _visual_deterministic_spec(ch.visual),
                }
                if ch.visual
                else None
            ),
        }
        if weather_overrides and ch.chapter_id in weather_overrides:
            chapter_entry["_weather_override"] = weather_overrides[ch.chapter_id]
        # For weather chapters, include narration text and renderer version
        # so narration changes or renderer upgrades re-run imagegen.
        is_weather = bool(weather_overrides and weather_overrides.get(ch.chapter_id) == "weather")
        if not is_weather and not (
            weather_overrides and weather_overrides.get(ch.chapter_id) == "normal"
        ):
            is_weather = _weather_detection_for_chapter(ch, weather_config).is_weather_story
        if (
            not is_weather
            and ch.visual
            and not (weather_overrides and weather_overrides.get(ch.chapter_id) == "normal")
        ):
            cat = _detect_exact_data_category(ch.visual)
            if cat == "weather":
                is_weather = True
        if is_weather:
            narr_text = ""
            if hasattr(ch, "narration") and ch.narration:
                narr_text = getattr(ch.narration, "text", "") or ""
            chapter_entry["_weather_narration_hash"] = hashlib.sha256(
                narr_text.encode()
            ).hexdigest()[:16]
            chapter_entry["_weather_source_text_hash"] = hashlib.sha256(
                (getattr(ch, "source_text", None) or "").encode()
            ).hexdigest()[:16]
            chapter_entry["_weather_story_type"] = getattr(ch, "story_type", None)
            chapter_entry["_weather_metadata"] = getattr(ch, "metadata", None) or {}
            chapter_entry["_weather_renderer_version"] = WEATHER_RENDERER_VERSION
            chapter_entry["_weather_schema_version"] = WEATHER_SCHEMA_VERSION
            chapter_entry["_weather_renderer_fingerprint"] = _weather_renderer_fingerprint(
                weather_config
            )
        relevant_data["chapters"].append(chapter_entry)

    content_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _weather_renderer_fingerprint(weather_config: dict | None) -> str:
    """Hash weather config, template, and local SVG assets for imagegen invalidation."""
    weather_dir = Path(__file__).parent / "weather"
    paths = [weather_dir / "templates" / "weather_card.html"]
    paths.extend(sorted((weather_dir / "assets").rglob("*.svg")))

    digest = hashlib.sha256()
    digest.update(
        json.dumps(weather_config or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    for path in paths:
        digest.update(str(path.relative_to(weather_dir)).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _is_image_gen_current(
    manifest_path: Path,
    provenance_path: Path,
    chapters_hash: str,
    prompt_hash: str,
    expected_chapter_ids: set[str] | None = None,
) -> bool:
    """Check if image generation is current (idempotency)."""
    # Check files exist
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    # Check for .stale marker
    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        logger.info("Image manifest marked as stale")
        return False

    # Check provenance hashes
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("input_content_hash") != chapters_hash:
            logger.info("Chapters content has changed")
            return False
        if provenance.get("prompt_hash") != prompt_hash:
            logger.info("Image generation prompt has changed")
            return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not verify provenance: {e}")
        return False

    # Verify all images exist
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_dir = manifest_path.parent.parent  # outputs/{ep_id}/
        entries = manifest.get("images", [])
        actual_ids = {entry.get("chapter_id") for entry in entries}
        if expected_chapter_ids is not None and actual_ids != expected_chapter_ids:
            logger.info("Image manifest chapter coverage is incomplete or stale")
            return False
        for img_entry in entries:
            if img_entry.get("generation_method") == "failed":
                logger.info(
                    "Image generation previously failed for %s",
                    img_entry.get("chapter_id"),
                )
                return False
            img_path = base_dir / img_entry["file_path"]
            if not img_path.exists():
                logger.info(f"Image file missing: {img_path}")
                return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not verify manifest: {e}")
        return False

    return True


def _needs_generation(visual_type: str) -> bool:
    """Check if visual type needs API generation vs. template."""
    return visual_type in VISUAL_TYPES_NEEDING_GENERATION


def _generate_image_prompt(
    chapter,
    template_body: str,
    settings: Settings,
) -> tuple[str, int, int, float]:
    """Generate DALL-E image prompt from chapter using LLM.

    Args:
        chapter: Chapter object with visual description
        template_body: Prompt template text
        settings: Application settings

    Returns:
        Tuple of (image_prompt, input_tokens, output_tokens, cost_usd)
    """
    # Split template at "# Input" marker
    system_prompt, user_template = _split_prompt(template_body)

    # Build user message with chapter data
    visual = chapter.visual
    narration_context = (
        chapter.narration.text[:300] + "..."
        if len(chapter.narration.text) > 300
        else chapter.narration.text
    )

    user_message = user_template.replace("{{ chapter_title }}", chapter.title)
    user_message = user_message.replace("{{ visual_type }}", visual.type)
    user_message = user_message.replace("{{ visual_description }}", visual.description)
    user_message = user_message.replace("{{ narration_context }}", narration_context)

    # Call Claude
    dry_run_path = None
    if settings.dry_run:
        dry_run_path = (
            Path(settings.outputs_dir) / "dry_run" / f"imagegen_{chapter.chapter_id}.json"
        )

    response = call_claude(system_prompt, user_message, settings, dry_run_path)

    image_prompt = response.text.strip()
    logger.info(
        f"Generated image prompt for chapter {chapter.chapter_id} ({len(image_prompt)} chars)"
    )

    return image_prompt, response.input_tokens, response.output_tokens, response.cost_usd


def _visual_beats(chapter) -> list[dict]:
    """Return the presenter blocks of a chapter that each deserve their own picture."""
    metadata = getattr(chapter, "metadata", None) or {}
    beats = metadata.get("visual_beats") or []
    if not isinstance(beats, list):
        return []
    return [beat for beat in beats if isinstance(beat, dict)]


# The programme never shows its presenters, so a shot hint may only change the
# framing of the subject. Naming a studio or a presenter makes the image model
# invent an on-screen anchor, which is both wrong and, since the anchor is a
# woman, usually the wrong person as well.
_NO_STUDIO = "No news studio, no presenter, no person addressing the camera."

# Shot grammar of a television report: the sequence tells the story visually
# instead of showing the same subject three times. Each entry is a different
# distance, height or object, so two consecutive pictures can never be near
# duplicates of each other.
_SHOT_LADDER: tuple[str, ...] = (
    "Framing: a wide establishing shot that places the story in its location, "
    "eye level, showing the surroundings.",
    "Framing: a close detail shot of a single concrete object central to the "
    "story, shallow depth of field, filling the frame.",
    "Framing: a medium shot of people involved in the situation, seen from the "
    "side or from behind, going about the event.",
    "Framing: the exterior of the building or institution at the centre of the "
    "story, seen from a low angle.",
    "Framing: a top-down shot of documents, papers or equipment relevant to the "
    "story on a surface.",
    "Framing: a high aerial view of the area, showing the wider infrastructure and its context.",
    "Framing: a ground-level shot of the infrastructure itself — roads, rails, "
    "machinery, cables or terminals — without any overview.",
    "Framing: a quiet closing image of the scene in the same location, taken "
    "from a distance in soft light.",
)

# The role decides where in the ladder a shot starts: the anchor introduces and
# classifies (overview), the reporter is on the ground (detail).
_ROLE_LADDER_OFFSET = {"anchor_female": 0, "reporter_male": 1}


def _shot_hint(chapter, beat: dict) -> str:
    """Framing instruction for one shot, varied across the whole programme.

    Two neighbouring chapters that both open with an establishing shot look like
    the same picture twice, so the ladder is rotated per chapter as well as per
    shot within the chapter. The result is deterministic: the same chapter and
    the same shot always produce the same framing.
    """
    chapter_id = str(getattr(chapter, "chapter_id", "") or "")
    chapter_number = int(re.sub(r"\D", "", chapter_id) or 0)
    beat_index = int(beat.get("beat_index") or 0)
    offset = _ROLE_LADDER_OFFSET.get(str(beat.get("role") or ""), 0)
    # 3 is coprime with the ladder length, so consecutive chapters never reuse
    # the same starting rung.
    position = (chapter_number * 3 + beat_index * 2 + offset) % len(_SHOT_LADDER)
    return f"{_SHOT_LADDER[position]} {_NO_STUDIO}"


def _generate_beat_prompt(
    chapter,
    beat: dict,
    template_body: str,
    settings: Settings,
) -> tuple[str, int, int, float]:
    """Generate an image prompt for one presenter block of a chapter.

    The subject stays the same — it is the same story — but the shot must not be.
    A change of presenter is a change of scene, so the beat's own words and a
    shot-specific framing hint drive the prompt. The hints rotate through a
    television shot grammar (establishing, detail, people, building, documents,
    aerial, infrastructure, closing image), so no two neighbouring pictures show
    the same view.

    Every shot of a chapter is written by this function, including the first one.
    Building the first shot from the chapter's one-line ``image_prompt`` and the
    remaining shots from a detailed prompt made the shots of a single story look
    unrelated, so the short line is folded into the description instead.
    """
    system_prompt, user_template = _split_prompt(template_body)
    visual = chapter.visual
    beat_text = str(beat.get("text") or "")
    narration_context = beat_text[:300] + "..." if len(beat_text) > 300 else beat_text
    hint = _shot_hint(chapter, beat)

    user_message = user_template.replace("{{ chapter_title }}", chapter.title)
    user_message = user_message.replace("{{ visual_type }}", visual.type)
    description = " ".join(part for part in (visual.description, visual.image_prompt, hint) if part)
    user_message = user_message.replace("{{ visual_description }}", description.strip())
    user_message = user_message.replace("{{ narration_context }}", narration_context)

    dry_run_path = None
    if settings.dry_run:
        beat_index = int(beat.get("beat_index") or 0)
        dry_run_path = (
            Path(settings.outputs_dir)
            / "dry_run"
            / f"imagegen_{chapter.chapter_id}_beat{beat_index:02d}.json"
        )

    response = call_claude(system_prompt, user_message, settings, dry_run_path)
    return response.text.strip(), response.input_tokens, response.output_tokens, response.cost_usd


def _generate_single_image(
    chapter,
    image_prompt: str,
    image_service,
    output_dir: Path,
    settings: Settings,
    style_prefix_override: str | None = None,
    smart_routing: bool | None = None,
    branding: dict | None = None,
    beat_index: int = 0,
) -> ImageEntry:
    """Generate a single image via configured provider.

    image_service is provider-agnostic (DallE3/Flux/Ideogram) — all conform
    to the ImageGenService protocol.

    style_prefix_override: profile-supplied style prefix. If None, falls back
    to the settings default. If empty string, disables the prefix entirely
    (useful for news profiles where Bitcoin/crypto branding would leak in).

    smart_routing: when True, pick the best provider per chapter visual type
    (Flux for photoreal b-roll, Ideogram for title cards / labelled maps).
    Defaults to the settings.image_gen_smart_routing flag when None.
    """
    if style_prefix_override is not None:
        effective_prefix = style_prefix_override
    else:
        effective_prefix = getattr(settings, "image_gen_style_prefix", "")

    if branding:
        from btcedu.core.branding_guard import sanitize_image_prompt

        effective_prefix, removed_prefix = sanitize_image_prompt(effective_prefix, branding)
        image_prompt, removed_prompt = sanitize_image_prompt(image_prompt, branding)
        for term in removed_prefix + removed_prompt:
            logger.warning(
                "Removed forbidden term %r from the image prompt for chapter %s",
                term,
                getattr(chapter, "chapter_id", "?"),
            )

    request = ImageGenRequest(
        prompt=image_prompt,
        model=getattr(settings, "image_gen_model", "gpt-image-1"),
        size=getattr(settings, "image_gen_size", "1792x1024"),
        quality=getattr(settings, "image_gen_quality", "standard"),
        style_prefix=effective_prefix,
    )

    # Smart per-chapter routing: pick best provider for this chapter's visual type
    if smart_routing is None:
        smart_routing = getattr(settings, "image_gen_smart_routing", False)
    if smart_routing:
        from btcedu.services.image_provider_factory import get_image_service

        provider = _route_provider_for_chapter(chapter)
        image_service = get_image_service(settings, provider=provider)

    # Override service-level style_prefix so profile choice wins over factory default.
    # Services concatenate self.style_prefix with the prompt; leaving the Bitcoin
    # branding baked in there causes news profiles (Tagesschau) to bleed crypto
    # imagery into every generated frame.
    if hasattr(image_service, "style_prefix"):
        image_service.style_prefix = effective_prefix

    response: ImageGenResponse = image_service.generate_image(request)

    suffix = f"_beat{beat_index:02d}" if beat_index else ""
    filename = f"{chapter.chapter_id}{suffix}_{_slugify_filename_part(chapter.title)}.png"
    target_path = output_dir / filename
    # All service classes expose static download_image with same signature
    type(image_service).download_image(response.image_url, target_path)

    file_size = target_path.stat().st_size

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visual.type,
        file_path=f"images/{filename}",
        prompt=image_prompt,
        generation_method=response.model.split("-")[0] if response.model else "unknown",
        model=response.model,
        size=request.size,
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "revised_prompt": response.revised_prompt,
            "cost_usd": response.cost_usd,
            "generated_at": _utcnow().isoformat(),
            "beat_index": beat_index,
        },
    )


def _create_template_placeholder(
    chapter, output_dir: Path, accent_color: str = "#F7931A"
) -> ImageEntry:
    """Create a placeholder image for template types (title_card, talking_head).

    Args:
        chapter: Chapter object
        output_dir: Directory to save placeholder

    Returns:
        ImageEntry with placeholder metadata
    """
    from PIL import Image, ImageDraw, ImageFont

    def _hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    try:
        _accent_rgb = _hex_to_rgb(accent_color)
    except Exception:
        _accent_rgb = (247, 147, 26)

    # Create simple placeholder (solid color with text)
    width, height = 1920, 1080
    bg_color = (
        _accent_rgb
        if chapter.visual.type == "title_card"
        else (200, 200, 200)  # profile accent for title cards, gray otherwise
    )

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    # Draw chapter title
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except OSError:
        font = ImageDraw.getfont()  # Fallback to default

    text = chapter.title
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((width - text_width) // 2, (height - text_height) // 2)

    draw.text(position, text, fill=(255, 255, 255), font=font)

    # Save placeholder
    filename = f"{chapter.chapter_id}_placeholder.png"
    target_path = output_dir / filename
    img.save(target_path, "PNG")

    file_size = target_path.stat().st_size

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visual.type,
        file_path=f"images/{filename}",
        prompt=None,
        generation_method="template",
        model=None,
        size="1920x1080",
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "template_name": f"{chapter.visual.type}_placeholder",
            "background_color": f"rgb{bg_color}",
            "text_overlay": chapter.title,
        },
    )


def _render_deterministic_visual(
    chapter, output_dir: Path, accent_color: str = "#004B87", weather_config: dict | None = None
) -> ImageEntry:
    """Render an exact-data visual locally — never via a generative model.

    Draws a clean, broadcast-style information card using ONLY values/labels that
    are present in the chapter's deterministic spec (or, absent a spec, the
    chapter title/description). Numbers, names and boundaries are therefore never
    invented. When exact cartographic boundaries cannot be drawn safely, this
    labeled information card is the documented safe fallback (Phase 8 req B).

    Weather chapters are routed to the specialized weather renderer for
    branded map/region/temperature visuals.
    """
    category = _detect_exact_data_category(chapter.visual) or "generic"

    # Route weather chapters to the specialized weather renderer
    if category == "weather" and (weather_config or {}).get("enabled", True):
        from btcedu.core.weather.detector import detect_weather_story

        narration_text = getattr(getattr(chapter, "narration", None), "text", "") or ""
        source_text = getattr(chapter, "source_text", None)
        story_type = getattr(chapter, "story_type", None)
        metadata = getattr(chapter, "metadata", None)
        detection = detect_weather_story(
            title=chapter.title or "",
            story_type=story_type if isinstance(story_type, str) else None,
            narration_text=narration_text,
            source_text=source_text if isinstance(source_text, str) else "",
            metadata=metadata if isinstance(metadata, dict) else {},
            min_confidence=((weather_config or {}).get("detection", {}) or {}).get(
                "min_confidence", 0.75
            ),
        )
        if detection.is_weather_story:
            return _render_weather_chapter(chapter, output_dir, accent_color, weather_config)
        category = "generic"

    from PIL import Image, ImageDraw, ImageFont

    def _hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    try:
        accent_rgb = _hex_to_rgb(accent_color)
    except Exception:  # noqa: BLE001
        accent_rgb = (0, 75, 135)

    spec = _visual_deterministic_spec(chapter.visual) or {}
    category = _detect_exact_data_category(chapter.visual) or "generic"
    title = str(spec.get("title") or chapter.title or category.title())
    note = str(spec.get("note") or "")
    items = spec.get("items") if isinstance(spec.get("items"), list) else []

    width, height = 1920, 1080
    img = Image.new("RGB", (width, height), color=(245, 247, 250))
    draw = ImageDraw.Draw(img)
    # Header band in the profile accent colour.
    draw.rectangle([(0, 0), (width, 160)], fill=accent_rgb)

    def _font(size: int):
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()

    draw.text((60, 45), title[:60], fill=(255, 255, 255), font=_font(64))

    # Exact label:value rows straight from the spec — no invented data.
    y = 240
    row_font = _font(52)
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        line = f"{label}: {value}" if label and value else (label or value)
        if not line:
            continue
        draw.text((80, y), line[:70], fill=(20, 20, 20), font=row_font)
        y += 90

    if note:
        draw.text((80, height - 120), note[:90], fill=(90, 90, 90), font=_font(38))

    filename = f"{chapter.chapter_id}_deterministic.png"
    target_path = output_dir / filename
    img.save(target_path, "PNG")
    file_size = target_path.stat().st_size

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visual.type,
        file_path=f"images/{filename}",
        prompt=None,
        generation_method="deterministic",
        model=None,
        size="1920x1080",
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "provider": "local_deterministic",
            "category": category,
            "cost_usd": 0.0,
            "source": "deterministic_spec" if spec else "chapter_text",
            "item_count": len([i for i in items if isinstance(i, dict)]),
            "generated_at": _utcnow().isoformat(),
        },
    )


def _attach_city_forecasts(weather_data, weather_config: dict | None) -> None:
    """Attach external per-city temperatures to the extracted weather data.

    The data is attributed on the card and never enters claim validation. Any
    provider failure is non-fatal: the card simply renders without city temps.
    """
    from datetime import date as _date
    from datetime import timedelta

    config = (weather_config or {}).get("city_temperatures") or {}
    if not config.get("enabled", True):
        return
    reference = weather_data.forecast_reference
    if not reference.broadcast_date or not reference.anchor_date:
        return
    try:
        broadcast = _date.fromisoformat(reference.broadcast_date)
        anchor = _date.fromisoformat(reference.anchor_date)
    except ValueError:
        return

    from btcedu.core.weather.cities import MAP_CITIES
    from btcedu.services.meteo_service import OpenMeteoService

    selected_ids = config.get("cities")
    cities = tuple(city for city in MAP_CITIES if not selected_ids or city.city_id in selected_ids)
    if not cities:
        return

    # Cover the broadcast night through the multi-day outlook in one request.
    span_days = (anchor - broadcast).days + 4
    dates = [broadcast + timedelta(days=offset) for offset in range(max(span_days, 1))]

    service = OpenMeteoService(
        timeout=float(config.get("timeout_seconds", 20)),
        model=str(config.get("model", "icon_seamless")),
    )
    forecasts = service.fetch_city_forecasts(cities, dates)
    if forecasts:
        weather_data.city_forecasts = forecasts
        weather_data.temperature_source = service.source_label
    else:
        logger.warning("No external city temperatures available for weather card")


def _render_weather_chapter(
    chapter,
    output_dir: Path,
    accent_color: str = "#004B87",
    weather_config: dict | None = None,
    broadcast_date=None,
) -> ImageEntry:
    """Render a weather chapter using the specialized weather renderer.

    Detects weather content, extracts structured data from the chapter narration,
    validates claims, and renders a branded weather visual. Falls back through
    progressively simpler visuals — never produces blank output.

    Raises RuntimeError if:
    - validation has publish_blocked findings (unsupported/invented claims)
    - render result is not successful
    - render result has publish_blocked findings
    Generic grounded fallback is allowed when extraction produces no regions.
    """
    from btcedu.core.weather.detector import detect_weather_story
    from btcedu.core.weather.extractor import extract_weather_data
    from btcedu.core.weather.models import FindingType
    from btcedu.core.weather.renderer import render_weather_visual
    from btcedu.core.weather.scene_planner import plan_weather_scenes
    from btcedu.core.weather.validator import validate_weather_data

    narration_text = ""
    duration_seconds = 30.0
    if hasattr(chapter, "narration") and chapter.narration:
        narration_text = getattr(chapter.narration, "text", "") or ""
        duration_seconds = float(getattr(chapter.narration, "estimated_duration_seconds", 30) or 30)

    # Resolve config: use weather config branding accent if provided
    _wcfg = weather_config or {}
    _branding = _wcfg.get("branding", {}) or {}
    effective_accent = _branding.get("accent_color") or accent_color
    weather_title = _branding.get("title") or "Hava Durumu"
    _rendering = _wcfg.get("rendering", {}) or {}
    _resolution = _rendering.get("resolution", {}) or {}
    width = int(_resolution.get("width", 1920))
    height = int(_resolution.get("height", 1080))
    animation_level = str(_rendering.get("animation_level", "subtle"))

    # Detect (confirmation — we already routed here based on keywords)
    source_text = getattr(chapter, "source_text", None)
    story_type = getattr(chapter, "story_type", None)
    metadata = getattr(chapter, "metadata", None)
    detection = detect_weather_story(
        title=chapter.title or "",
        story_type=story_type if isinstance(story_type, str) else None,
        narration_text=narration_text,
        source_text=source_text if isinstance(source_text, str) else "",
        metadata=metadata if isinstance(metadata, dict) else {},
        min_confidence=(_wcfg.get("detection", {}) or {}).get("min_confidence", 0.75),
    )

    # Extract weather data
    weather_data = extract_weather_data(
        narration_text,
        story_id=chapter.chapter_id,
        source_text=source_text if isinstance(source_text, str) else "",
        broadcast_date=broadcast_date,
    )
    _attach_city_forecasts(weather_data, _wcfg)

    # Validate
    validation = validate_weather_data(weather_data, narration_text)
    weather_data_path = output_dir / f"{chapter.chapter_id}_weather.json"
    validation_path = output_dir / f"{chapter.chapter_id}_weather_validation.json"
    detection_path = output_dir / f"{chapter.chapter_id}_weather_detection.json"
    weather_data_path.write_text(weather_data.model_dump_json(indent=2), encoding="utf-8")
    validation_path.write_text(validation.model_dump_json(indent=2), encoding="utf-8")
    detection_path.write_text(detection.model_dump_json(indent=2), encoding="utf-8")

    # Reject if validation has publish-blocking findings (unsupported/invented claims)
    # BUT allow through if extraction simply found no regions (generic fallback OK)
    if validation.publish_blocked:
        blocking_findings = [f for f in validation.findings if f.publish_blocked]
        # Filter: if all blockers are just "low confidence" or "unresolved" that's
        # not an invented claim, it's absence — allow generic fallback.
        unsupported_blockers = [
            f
            for f in blocking_findings
            if f.type
            not in (
                FindingType.UNRESOLVED_WEATHER_CLAIM,
                FindingType.WEATHER_EXTRACTION_LOW_CONFIDENCE,
            )
        ]
        if unsupported_blockers:
            findings_detail = "; ".join(
                f"[{f.severity.value}] {f.type.value}: {f.message}" for f in unsupported_blockers
            )
            raise RuntimeError(
                f"Weather validation blocked publish for chapter "
                f"'{chapter.chapter_id}': {findings_detail}"
            )

    # Plan scenes
    scene_plan = plan_weather_scenes(weather_data, duration_seconds, story_id=chapter.chapter_id)

    scene_plan_path = output_dir / f"{chapter.chapter_id}_weather_scenes.json"
    scene_plan_path.write_text(scene_plan.model_dump_json(indent=2), encoding="utf-8")

    # Render
    filename = f"{chapter.chapter_id}_weather.png"
    target_path = output_dir / filename
    result = render_weather_visual(
        weather_data,
        scene_plan,
        target_path,
        accent_color=effective_accent,
        profile=json.dumps(_wcfg, sort_keys=True, ensure_ascii=False),
        width=width,
        height=height,
        title=weather_title,
    )

    # Reject if render failed or has publish-blocking findings
    if not result.success:
        findings_detail = (
            "; ".join(f"[{f.severity.value}] {f.message}" for f in result.findings)
            if result.findings
            else "unknown render failure"
        )
        raise RuntimeError(
            f"Weather render failed for chapter '{chapter.chapter_id}': {findings_detail}"
        )

    result_blockers = [f for f in result.findings if f.publish_blocked]
    if result_blockers:
        findings_detail = "; ".join(
            f"[{f.severity.value}] {f.type.value}: {f.message}" for f in result_blockers
        )
        raise RuntimeError(
            f"Weather render produced publish-blocked output for chapter "
            f"'{chapter.chapter_id}': {findings_detail}"
        )

    if not target_path.exists():
        raise RuntimeError(f"Weather render produced no output file: {target_path}")

    file_size = target_path.stat().st_size

    # Clean up temporary HTML file left by renderer (unless debug)
    html_path = target_path.with_suffix(".html")
    if html_path.exists():
        import os

        if not os.environ.get("BTCEDU_DEBUG_WEATHER"):
            html_path.unlink(missing_ok=True)

    return ImageEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        visual_type=chapter.visual.type if chapter.visual else "b_roll",
        file_path=f"images/{filename}",
        prompt=None,
        generation_method="deterministic",
        model=None,
        size=f"{width}x{height}",
        mime_type="image/png",
        size_bytes=file_size,
        metadata={
            "provider": "weather_renderer",
            "category": "weather",
            "cost_usd": 0.0,
            "source": "weather_narration",
            "fallback_level": result.fallback_level,
            "renderer_version": result.renderer_version,
            "cache_key": result.cache_key,
            "detection_confidence": detection.confidence,
            "region_count": len(weather_data.regions),
            "validation_valid": validation.valid,
            "weather_data_path": f"images/{weather_data_path.name}",
            "scene_plan_path": f"images/{scene_plan_path.name}",
            "validation_path": f"images/{validation_path.name}",
            "detection_path": f"images/{detection_path.name}",
            "scene_count": len(scene_plan.scenes),
            "animation_level": animation_level,
            "scene_video_deferred_until_render": animation_level != "none",
            "generated_at": _utcnow().isoformat(),
        },
        asset_type="photo",
    )


def _create_media_asset_record(
    session: Session,
    episode_id: str,
    image_entry: ImageEntry,
    prompt_version_id: int,
) -> None:
    """Create MediaAsset database record for generated image."""
    media_asset = MediaAsset(
        episode_id=episode_id,
        asset_type=(
            MediaAssetType.VIDEO if image_entry.asset_type == "video" else MediaAssetType.IMAGE
        ),
        chapter_id=image_entry.chapter_id,
        file_path=image_entry.file_path,
        mime_type=image_entry.mime_type,
        size_bytes=image_entry.size_bytes,
        meta=image_entry.metadata,
        prompt_version_id=prompt_version_id,
        created_at=_utcnow(),
    )
    session.add(media_asset)


def _mark_downstream_stale(episode_id: str, outputs_dir: Path) -> None:
    """Mark TTS and RENDER stages as stale."""
    stale_data = {
        "invalidated_at": _utcnow().isoformat(),
        "invalidated_by": "imagegen",
        "reason": "images_changed",
    }

    # Mark render artifacts stale
    render_draft = outputs_dir / episode_id / "render" / "draft.mp4"
    if render_draft.exists():
        stale_marker = render_draft.with_suffix(".mp4.stale")
        stale_marker.write_text(json.dumps(stale_data, ensure_ascii=False))
        logger.info(f"Marked render draft as stale: {stale_marker}")


def _get_episode_total_cost(session: Session, episode_id: str) -> float:
    """Get cumulative cost for all pipeline runs for this episode.

    ``PipelineRun.episode_id`` is an integer FK to ``episodes.id``; resolve the
    string ``episode_id`` to that integer so the cost guard counts every prior
    stage (transcribe/translate/adapt/chapterize/qa/...) rather than nothing.
    """
    from sqlalchemy import func

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is None:
        return 0.0
    return float(
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
        or 0.0
    )


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split template at '# Input' marker into system and user sections."""
    if "# Input" in template_body:
        system_part, user_part = template_body.split("# Input", 1)
        return system_part.strip(), user_part.strip()
    else:
        # If no marker, treat entire template as system prompt
        return template_body.strip(), ""
