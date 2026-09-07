"""Anchor video generation.

Two paths live here. The scene-based one is what ALMANYA24 uses: it takes the
shot list written by ``sceneplan``, sends only the presenter's blocks to the
avatar provider, and books every clip through the durable job ledger so a reboot
resumes rather than re-buys. The older chapter-based path, which orders one clip
per ``TALKING_HEAD`` chapter, is kept for episodes that have no scene plan --
stored D-ID episodes and anything produced before the plan existed.

Which path runs is decided by one thing only: whether ``scene_plan.json`` is on
disk. Nothing about the disabled-anchor no-op changes either way.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.anchor_config import AnchorConfig, resolve_anchor_config
from btcedu.models.chapter_schema import ChapterDocument, VisualType
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType
from btcedu.services.errors import ErrorCategory, PipelineError

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnchorEntry:
    """Metadata for a single anchor video."""

    chapter_id: str
    chapter_title: str
    audio_path: str
    video_path: str
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    provider: str
    provider_job_id: str
    output_format: str
    mime_type: str
    did_talk_id: str | None = None


@dataclass
class SceneAnchorEntry:
    """Metadata for one presenter clip of the scene-based path.

    Carries everything needed to audit a purchase without opening the database:
    which scene it belongs to, which outfit wore it, which take was lip-synced,
    what the provider called the job and what it cost.
    """

    scene_id: str
    clip_id: str
    chapter_id: str
    speaker_role: str
    template_id: str
    avatar_look_id: str
    part_index: int
    audio_path: str
    audio_hash: str
    content_hash: str
    provider: str
    provider_job_id: str
    status: str
    video_path: str
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    output_format: str
    mime_type: str
    #: SHA-256 of the clip's bytes. The manifest names a path; this says which
    #: file that path has to be, and every later gate compares against it.
    file_sha256: str = ""


@dataclass
class AnchorResult:
    """Summary of anchor generation for one episode."""

    episode_id: str
    anchor_dir: Path
    manifest_path: Path
    provenance_path: Path
    segment_count: int = 0
    total_duration_seconds: float = 0.0
    cost_usd: float = 0.0
    skipped: bool = False
    # Scene-based path only: how many clips were newly bought versus taken from
    # the ledger. A resumed run reports zero submitted.
    submitted_count: int = 0
    reused_count: int = 0
    resumed_count: int = 0
    scene_ids: list[str] = field(default_factory=list)



def generate_anchors(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> AnchorResult:
    """Generate anchor videos for TALKING_HEAD chapters in an episode.

    If anchor_enabled is False, this is a no-op that advances the status.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, regenerate all anchor videos

    Returns:
        AnchorResult with paths, counts, duration, cost, and skip status
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.pipeline_version != 2:
        raise ValueError(f"Episode {episode_id} is v1 pipeline. Anchor generation requires v2.")

    # Allow TTS_DONE or ANCHOR_GENERATED for idempotency
    if (
        episode.status
        not in (
            EpisodeStatus.TTS_DONE,
            EpisodeStatus.SCENE_PLANNED,
            EpisodeStatus.ANCHOR_GENERATED,
        )
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'anchor_generated'. Use --force to override."
        )

    # Resolve paths
    outputs_dir = Path(settings.outputs_dir) / episode_id
    chapters_path = outputs_dir / "chapters.json"
    anchor_dir = outputs_dir / "anchor"
    manifest_path = anchor_dir / "manifest.json"
    provenance_path = outputs_dir / "provenance" / "anchor_provenance.json"

    # If anchor is disabled, just advance status
    if not settings.anchor_enabled:
        logger.info("Anchor generation disabled, advancing status for %s", episode_id)
        if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED):
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # Load chapters
    if not chapters_path.exists():
        raise FileNotFoundError(f"Chapters file not found: {chapters_path}")

    # The shot list decides the path. Its presence means the episode was planned
    # as scenes, which is the only way the presenter's blocks are known.
    plan_path = outputs_dir / "scene_plan.json"
    if plan_path.exists():
        return _generate_anchors_from_plan(
            session,
            episode,
            settings,
            plan_path=plan_path,
            outputs_dir=outputs_dir,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            force=force,
        )

    chapters_doc = _load_chapters(chapters_path)

    # Find TALKING_HEAD chapters
    talking_head_chapters = [
        ch for ch in chapters_doc.chapters if ch.visual.type == VisualType.TALKING_HEAD
    ]

    if not talking_head_chapters:
        logger.info("No TALKING_HEAD chapters for %s, advancing status", episode_id)
        if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED):
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    anchor_config = _resolve_anchor_config(episode, settings)

    # Load TTS manifest before the idempotency check: changing the narration
    # audio must invalidate an otherwise-identical avatar render.
    tts_manifest_path = outputs_dir / "tts" / "manifest.json"
    if not tts_manifest_path.exists():
        raise FileNotFoundError(f"TTS manifest not found: {tts_manifest_path}")
    tts_manifest = json.loads(tts_manifest_path.read_text(encoding="utf-8"))
    tts_segments = {s["chapter_id"]: s for s in tts_manifest.get("segments", [])}

    # Idempotency check
    content_hash = _compute_anchor_hash(chapters_doc, anchor_config, tts_segments)
    if not force and _is_anchor_current(manifest_path, provenance_path, content_hash):
        logger.info("Anchor videos current for %s (use --force to regenerate)", episode_id)
        if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED):
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="anchorgen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        anchor_service = _create_anchor_service(anchor_config, settings, anchor_dir)

        anchor_dir.mkdir(parents=True, exist_ok=True)

        # Generate anchor videos
        anchor_entries: list[AnchorEntry] = []
        total_cost = 0.0
        total_duration = 0.0
        stage_budget = min(anchor_config.max_cost_usd, settings.max_episode_cost_usd)

        for chapter in talking_head_chapters:
            tts_segment = tts_segments.get(chapter.chapter_id)
            if not tts_segment:
                logger.warning("No TTS segment for chapter %s, skipping anchor", chapter.chapter_id)
                continue

            audio_path = str(outputs_dir / tts_segment["file_path"])
            if not Path(audio_path).exists():
                logger.warning("TTS audio not found: %s, skipping", audio_path)
                continue

            expected_duration = _positive_duration(
                tts_segment.get("duration_seconds"),
                f"TTS segment {chapter.chapter_id}",
            )
            estimated_cost = anchor_service.estimate_cost(expected_duration)
            _ensure_anchor_budget(
                session,
                episode_id,
                settings,
                stage_budget=stage_budget,
                spent_usd=total_cost,
                next_cost_usd=estimated_cost,
                provider=anchor_config.provider,
            )

            from btcedu.services.anchor_service import AnchorRequest

            request = AnchorRequest(
                source_image_path=anchor_config.source_image,
                source_image_url=anchor_config.source_image_url,
                audio_path=audio_path,
                chapter_id=chapter.chapter_id,
                expression=anchor_config.expression,
                expected_duration_seconds=expected_duration,
            )

            response = anchor_service.generate_anchor_video(request)
            total_cost += response.cost_usd
            total_duration += response.duration_seconds
            _ensure_anchor_budget(
                session,
                episode_id,
                settings,
                stage_budget=stage_budget,
                spent_usd=total_cost,
                next_cost_usd=0.0,
                provider=anchor_config.provider,
            )
            response_path = Path(response.video_path)
            try:
                relative_video_path = response_path.relative_to(outputs_dir)
            except ValueError as exc:
                raise ValueError(
                    f"Anchor provider wrote outside the episode output directory: {response_path}"
                ) from exc

            entry = AnchorEntry(
                chapter_id=chapter.chapter_id,
                chapter_title=chapter.title,
                audio_path=tts_segment["file_path"],
                video_path=str(relative_video_path),
                duration_seconds=response.duration_seconds,
                size_bytes=response.size_bytes,
                cost_usd=response.cost_usd,
                provider=response.provider,
                provider_job_id=response.provider_job_id,
                output_format=response.output_format,
                mime_type=response.mime_type,
                did_talk_id=response.did_talk_id or None,
            )
            anchor_entries.append(entry)

            # Create MediaAsset record
            asset = MediaAsset(
                episode_id=episode_id,
                asset_type=MediaAssetType.VIDEO,
                chapter_id=chapter.chapter_id,
                file_path=entry.video_path,
                mime_type=entry.mime_type,
                size_bytes=entry.size_bytes,
                duration_seconds=response.duration_seconds,
                meta={
                    "provider": response.provider,
                    "provider_job_id": response.provider_job_id,
                    "engine": anchor_config.engine,
                    **(
                        {
                            "did_talk_id": response.did_talk_id,
                            "source": "d-id",
                        }
                        if response.provider == "d-id"
                        else {}
                    ),
                },
            )
            session.add(asset)

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "anchor_provider": anchor_config.provider,
            "engine": anchor_config.engine,
            "output_format": anchor_config.output_format,
            "cost_per_second_usd": anchor_config.cost_per_second_usd,
            "max_cost_usd": stage_budget,
            "source_image": anchor_config.source_image,
            "avatar_id": anchor_config.avatar_id,
            "generated_at": _utcnow().isoformat(),
            "total_duration_seconds": total_duration,
            "total_cost_usd": total_cost,
            "segments": [asdict(entry) for entry in anchor_entries],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "anchorgen",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "model": f"{anchor_config.provider} ({anchor_config.engine})",
            "input_files": [str(chapters_path), str(tts_manifest_path)],
            "input_content_hash": content_hash,
            "output_files": [str(manifest_path)]
            + [str(outputs_dir / e.video_path) for e in anchor_entries],
            "segment_count": len(anchor_entries),
            "total_duration_seconds": total_duration,
            "cost_per_second_usd": anchor_config.cost_per_second_usd,
            "max_cost_usd": stage_budget,
            "cost_usd": total_cost,
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="anchor_video",
            file_path="anchor/manifest.json",
            prompt_hash=content_hash,
            model=anchor_config.provider,
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Mark render as stale
        render_manifest = outputs_dir / "render" / "render_manifest.json"
        if render_manifest.exists():
            render_manifest.with_suffix(".json.stale").touch()

        # Update episode status
        episode.status = EpisodeStatus.ANCHOR_GENERATED
        episode.error_message = None
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = total_cost
        session.commit()

        logger.info(
            "Anchor generation complete for %s: %d segments, %.1fs total, $%.3f",
            episode_id,
            len(anchor_entries),
            total_duration,
            total_cost,
        )

        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            segment_count=len(anchor_entries),
            total_duration_seconds=total_duration,
            cost_usd=total_cost,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        pipeline_run.estimated_cost_usd = locals().get("total_cost", 0.0)
        if isinstance(e, PipelineError) and e.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
        episode.error_message = str(e)
        session.commit()
        logger.error("Anchor generation failed for %s: %s", episode_id, e)
        raise


class AnchorReconciliationRequired(RuntimeError):
    """A prior avatar job may have been billed and must be resolved by hand."""


@dataclass(frozen=True)
class _AnchorUnit:
    """One presenter clip: exactly one existing TTS take, sent as it is."""

    scene_id: str
    clip_id: str
    chapter_id: str
    speaker_role: str
    template_id: str
    part_index: int
    audio_rel_path: str
    audio_abs_path: Path
    audio_hash: str
    text_hash: str
    expected_duration_seconds: float


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _refuse_unless_permitted(episode_id: str, settings: Settings, config: AnchorConfig) -> None:
    """Stop before the first paid call if this presenter may not be generated.

    The review gate already refuses to *show* clips made without a valid consent
    record or a loadable studio, but by then the provider has been paid for them
    and — worse — a likeness has been synthesised that nobody was permitted to
    synthesise. Consent is the one condition where a late refusal is worthless,
    so it is checked once more here, against the configuration this episode
    actually resolved.

    Deliberately narrow: only what the profile itself declares. A profile that
    names no release record and no studio directory is not silently blocked;
    that decision belongs to `btcedu anchor-readiness` and to the review gate,
    not to a stage that would otherwise refuse every existing installation.
    """
    from btcedu.core.anchor_rights import load_rights_record, rights_problems
    from btcedu.core.studio_manifest import load_studio_manifest

    problems: list[str] = []

    if config.rights.revoked:
        problems.append("the profile marks the presenter release as revoked")
    if config.rights.record_file:
        try:
            record = load_rights_record(config.rights.record_file)
        except Exception as exc:  # noqa: BLE001 - an unreadable release is a refusal
            problems.append(f"release record {config.rights.record_file!r} unusable: {exc}")
        else:
            problems.extend(
                rights_problems(
                    record,
                    channel=config.rights.channel,
                    territory=config.rights.territory,
                )
            )

    if config.studio.asset_dir:
        manifest_path = Path(config.studio.asset_dir) / config.studio.manifest
        try:
            load_studio_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001 - an unusable studio is a refusal
            problems.append(f"studio manifest {manifest_path} unusable: {exc}")

    if problems:
        raise PipelineError(
            f"Presenter generation for {episode_id} refused before any provider call: "
            + "; ".join(problems),
            category=ErrorCategory.PERMANENT_CONTENT,
        )


def _refuse_if_overridden(session, episode_id: str) -> None:
    """An episode an operator took off the avatar path buys no more clips.

    The override is what makes the bulletin renderable without a presenter; if
    the stage kept submitting afterwards it would spend money on footage the
    render is now guaranteed to ignore.
    """
    from btcedu.core.anchor_fallback import active_voice_over_override

    override = active_voice_over_override(session, episode_id)
    if override is None:
        return
    raise PipelineError(
        f"Episode {episode_id} carries a voice-over override recorded by "
        f"{override.operator_ref}; no further presenter clips are ordered. "
        "Withdraw the override to return to the avatar path.",
        category=ErrorCategory.PERMANENT_CONTENT,
    )


def _billable_units(
    scenes: list,
    tts_manifest: dict,
    outputs_dir: Path,
) -> tuple[list[_AnchorUnit], list[dict]]:
    """Split the presenter's scenes into the clips that may be bought.

    Everything the avatar must never animate is dropped here, with a reason, so
    the manifest can show what was left out instead of leaving it unexplained.
    The reporter is excluded by ``anchor_scenes`` already; what is excluded here
    is any scene whose picture is produced deterministically by the weather
    renderer, because a generated presenter drawn over a weather card is exactly
    what the weather subsystem exists to prevent.

    The test is the *visual mode*, not the template. The weather handover is a
    normal studio shot of the presenter announcing the forecast — she is
    composited into the studio like every other anchor scene, the shot list
    marks it ``needs_avatar``, and both the review gate and the renderer refuse
    to proceed without her clip. Excluding it by template left a bulletin with a
    weather block unrenderable and unapprovable; only the weather block itself,
    which the reporter speaks over a rendered card, must stay unanimated.
    """
    from btcedu.core.scene_planner import VISUAL_MODE_WEATHER, scene_audio_parts

    audio_entries = {
        str(entry.get("chapter_id")): entry for entry in tts_manifest.get("segments", [])
    }

    units: list[_AnchorUnit] = []
    excluded: list[dict] = []

    for scene in scenes:
        if scene.visual_mode == VISUAL_MODE_WEATHER:
            excluded.append({"scene_id": scene.scene_id, "reason": "weather_is_rendered"})
            continue

        entry = audio_entries.get(scene.chapter_id) or {}
        parts = ((entry.get("metadata") or {}).get("speaker_parts")) or []
        part_files = scene_audio_parts(scene, parts)
        # Same filter scene_audio_parts applies, so file i belongs to index i.
        indices = [
            i
            for i in scene.segment_indices
            if 0 <= i < len(parts) and str((parts[i] or {}).get("file") or "").strip()
        ]

        if not part_files:
            # A chapter the planner covered without speaker detail: its single
            # audio file is the take, and it is still a real TTS artefact.
            whole = str(entry.get("file_path") or "")
            if whole:
                part_files = [whole]
                indices = []
            else:
                excluded.append({"scene_id": scene.scene_id, "reason": "no_tts_audio"})
                continue

        for position, relative in enumerate(part_files):
            audio_path = outputs_dir / relative
            if not audio_path.exists():
                excluded.append(
                    {
                        "scene_id": scene.scene_id,
                        "reason": "tts_audio_missing",
                        "audio_path": relative,
                    }
                )
                continue

            if indices and position < len(indices):
                index = indices[position]
                measured = float((parts[index] or {}).get("duration_seconds") or 0.0)
            else:
                measured = float(entry.get("duration_seconds") or 0.0)
            if measured <= 0:
                measured = scene.expected_duration_seconds / max(1, len(part_files))
            if measured <= 0:
                excluded.append({"scene_id": scene.scene_id, "reason": "zero_duration"})
                continue

            clip_id = (
                scene.scene_id if len(part_files) == 1 else f"{scene.scene_id}_p{position:02d}"
            )
            units.append(
                _AnchorUnit(
                    scene_id=scene.scene_id,
                    clip_id=clip_id,
                    chapter_id=scene.chapter_id,
                    speaker_role=scene.speaker_role,
                    template_id=scene.template_id,
                    part_index=position,
                    audio_rel_path=relative,
                    audio_abs_path=audio_path,
                    audio_hash=_file_hash(audio_path),
                    text_hash=scene.text_hash,
                    expected_duration_seconds=round(measured, 3),
                )
            )

    return units, excluded


def _resolve_look(
    session: Session,
    episode_id: str,
    config: AnchorConfig,
    plan: dict,
    outputs_root: str | Path,
) -> str:
    """The one outfit this episode wears, taken from the durable assignment.

    The plan records the look it was built for. If the database now says
    something else, the plan describes a different presenter than the clips
    would show, and continuing would mix two outfits inside one bulletin.
    """
    if config.provider != "heygen":
        return config.avatar_id

    from btcedu.core.presenter_assignment import ensure_assignment

    assignment = ensure_assignment(session, episode_id, config, outputs_root)
    planned = str(plan.get("presenter_look_id") or "")
    if planned and planned != assignment.avatar_look_id:
        raise ValueError(
            f"Scene plan for {episode_id} was built for look {planned!r}, but the "
            f"episode is assigned {assignment.avatar_look_id!r}. Re-run sceneplan."
        )
    return assignment.avatar_look_id


def _generate_anchors_from_plan(
    session: Session,
    episode: Episode,
    settings: Settings,
    *,
    plan_path: Path,
    outputs_dir: Path,
    anchor_dir: Path,
    manifest_path: Path,
    provenance_path: Path,
    force: bool,
) -> AnchorResult:
    """Buy the presenter's clips, once each, and record what was bought."""
    from btcedu.core.avatar_coordinator import (
        AvatarCoordinator,
        SceneReconciliationRequired,
        SceneRequest,
        plan_scene_work,
    )
    from btcedu.core.avatar_download import ClipExpectation
    from btcedu.core.avatar_jobs import (
        compute_job_hash,
        episode_avatar_cost,
        get_job,
    )
    from btcedu.core.avatar_regeneration import episode_revisions, mark_consumed
    from btcedu.core.scene_planner import anchor_scenes, load_scene_plan, scenes_from_plan
    from btcedu.models.avatar_job import AvatarJobStatus

    episode_id = episode.episode_id
    config = _resolve_anchor_config(episode, settings)

    tts_manifest_path = outputs_dir / "tts" / "manifest.json"
    if not tts_manifest_path.exists():
        raise FileNotFoundError(f"TTS manifest not found: {tts_manifest_path}")
    tts_manifest = json.loads(tts_manifest_path.read_text(encoding="utf-8"))

    plan = load_scene_plan(plan_path)
    scenes = scenes_from_plan(plan)
    presenter_scenes = anchor_scenes(scenes)
    units, excluded = _billable_units(presenter_scenes, tts_manifest, outputs_dir)

    if not units:
        logger.info("No presenter clips to generate for %s, advancing status", episode_id)
        if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED):
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    _refuse_unless_permitted(episode_id, settings, config)
    _refuse_if_overridden(session, episode_id)

    look_id = _resolve_look(session, episode_id, config, plan, settings.outputs_dir)
    # A confirmed regeneration is the only thing that makes an already paid
    # scene different work; it changes the content hash rather than sneaking
    # a second job past the ledger's uniqueness rule.
    revisions = episode_revisions(session, episode_id)
    anchor_dir.mkdir(parents=True, exist_ok=True)
    service = _create_anchor_service(config, settings, anchor_dir, avatar_id=look_id)

    hashes = {
        unit.clip_id: compute_job_hash(
            scene_id=unit.clip_id,
            text_hash=unit.text_hash,
            audio_hash=unit.audio_hash,
            avatar_look_id=look_id,
            provider=config.provider,
            engine=config.engine,
            output_format=config.output_format,
            resolution=config.resolution,
            aspect_ratio=config.aspect_ratio,
            generation_revision=revisions.get(unit.clip_id, 0),
        )
        for unit in units
    }

    plan_hash = _compute_plan_anchor_hash(plan, config, look_id, hashes)
    if (
        not force
        and not settings.dry_run
        and _is_anchor_current(manifest_path, provenance_path, plan_hash)
    ):
        logger.info("Anchor clips current for %s (use --force to regenerate)", episode_id)
        if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED):
            episode.status = EpisodeStatus.ANCHOR_GENERATED
            session.commit()
        return AnchorResult(
            episode_id=episode_id,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="anchorgen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    total_cost = 0.0
    try:
        stage_budget = min(config.max_cost_usd, settings.max_episode_cost_usd)

        # Dry-run never touches the ledger. A placeholder row would later look
        # like a paid clip and suppress the real purchase.
        if settings.dry_run:
            entries, duration = _dry_run_clips(service, units, look_id, outputs_dir, config)
            return _finish_scene_anchors(
                session,
                episode,
                settings,
                pipeline_run=pipeline_run,
                config=config,
                look_id=look_id,
                entries=entries,
                excluded=excluded,
                outputs_dir=outputs_dir,
                anchor_dir=anchor_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                plan_path=plan_path,
                tts_manifest_path=tts_manifest_path,
                plan_hash=plan_hash,
                stage_budget=stage_budget,
                total_duration=duration,
                total_cost=0.0,
                counts=(0, 0, 0),
            )

        # Preflight over the whole planned presenter duration. Only clips that
        # would actually be submitted are projected; already-reserved and
        # completed ones are in episode_avatar_cost() and must not count twice.
        committed = episode_avatar_cost(session, episode_id)
        planned_new = 0.0
        for unit in units:
            existing = get_job(session, episode_id, unit.clip_id, hashes[unit.clip_id])
            if existing is None or existing.status == AvatarJobStatus.FAILED.value:
                planned_new += service.estimate_cost(unit.expected_duration_seconds)
        _ensure_anchor_budget(
            session,
            episode_id,
            settings,
            stage_budget=stage_budget,
            spent_usd=committed,
            next_cost_usd=planned_new,
            provider=config.provider,
        )

        by_scene = {unit.clip_id: unit for unit in units}
        scene_requests = [
            SceneRequest(
                scene_id=unit.clip_id,
                chapter_id=unit.chapter_id,
                content_hash=hashes[unit.clip_id],
                audio_path=unit.audio_abs_path,
                audio_hash=unit.audio_hash,
                expected_duration_seconds=unit.expected_duration_seconds,
                estimated_cost_usd=service.estimate_cost(unit.expected_duration_seconds),
                title=unit.chapter_id,
            )
            for unit in units
        ]

        # Two different budget questions. While planning, each new reservation
        # is checked against the running total it will add to. While
        # dispatching, the check is repeated against what the ledger now holds —
        # reservations from this run included — because minutes may have passed
        # and another process may have spent in the meantime.
        planned_spend = 0.0

        def plan_budget_check(next_cost: float) -> None:
            nonlocal planned_spend
            _ensure_anchor_budget(
                session,
                episode_id,
                settings,
                stage_budget=stage_budget,
                spent_usd=committed + planned_spend,
                next_cost_usd=next_cost,
                provider=config.provider,
            )
            planned_spend += next_cost

        def dispatch_budget_check(next_cost: float) -> None:
            del next_cost  # already reserved; the ledger total is the truth now
            _ensure_anchor_budget(
                session,
                episode_id,
                settings,
                stage_budget=stage_budget,
                spent_usd=episode_avatar_cost(session, episode_id),
                next_cost_usd=0.0,
                provider=config.provider,
            )

        try:
            work_plan = plan_scene_work(
                session,
                episode_id=episode_id,
                requests=scene_requests,
                provider=config.provider,
                engine=config.engine,
                look_id=look_id,
                output_format=config.output_format,
                outputs_dir=outputs_dir,
                budget_check=plan_budget_check,
            )
        except SceneReconciliationRequired as exc:
            raise AnchorReconciliationRequired(str(exc)) from exc

        coordinator = AvatarCoordinator(
            session,
            episode_id=episode_id,
            provider=config.provider,
            engine=config.engine,
            look_id=look_id,
            output_format=config.output_format,
            service=service,
            outputs_dir=outputs_dir,
            anchor_dir=anchor_dir,
            max_concurrent_jobs=config.max_concurrent_jobs,
            poll_interval_seconds=config.poll_interval_seconds,
            poll_timeout_seconds=config.poll_timeout_seconds,
            expectation=ClipExpectation(
                output_format=config.output_format,
                # A composited studio needs the alpha channel; an opaque clip
                # would be pasted over the set as a rectangle.
                require_alpha=config.studio_mode == "composite",
            ),
            budget_check=dispatch_budget_check,
        )
        run_result = coordinator.run(work_plan)

        entries: list[SceneAnchorEntry] = []
        total_duration = run_result.total_duration_seconds
        total_cost = run_result.total_cost_usd
        submitted = run_result.submitted
        reused = run_result.reused
        resumed = run_result.resumed

        for outcome in run_result.outcomes:
            unit = by_scene[outcome.scene_id]
            relative_video = outcome.video_path.relative_to(outputs_dir)
            entries.append(
                SceneAnchorEntry(
                    scene_id=unit.scene_id,
                    clip_id=unit.clip_id,
                    chapter_id=unit.chapter_id,
                    speaker_role=unit.speaker_role,
                    template_id=unit.template_id,
                    avatar_look_id=look_id,
                    part_index=unit.part_index,
                    audio_path=unit.audio_rel_path,
                    audio_hash=unit.audio_hash,
                    content_hash=hashes[unit.clip_id],
                    provider=config.provider,
                    provider_job_id=outcome.provider_job_id,
                    status=AvatarJobStatus.COMPLETED.value,
                    video_path=str(relative_video),
                    duration_seconds=outcome.duration_seconds,
                    size_bytes=outcome.size_bytes,
                    cost_usd=outcome.cost_usd,
                    output_format=outcome.output_format,
                    mime_type=outcome.mime_type,
                    file_sha256=outcome.file_sha256,
                )
            )
            if outcome.action == "reuse":
                continue
            session.add(
                MediaAsset(
                    episode_id=episode_id,
                    asset_type=MediaAssetType.VIDEO,
                    chapter_id=unit.chapter_id,
                    file_path=str(relative_video),
                    mime_type=outcome.mime_type,
                    size_bytes=outcome.size_bytes,
                    duration_seconds=outcome.duration_seconds,
                    meta={
                        "provider": config.provider,
                        "provider_job_id": outcome.provider_job_id,
                        "engine": config.engine,
                        "scene_id": unit.scene_id,
                        "avatar_look_id": look_id,
                    },
                )
            )

        _require_complete_run(run_result, episode_id)

        mark_consumed(session, episode_id, list(revisions))

        return _finish_scene_anchors(
            session,
            episode,
            settings,
            pipeline_run=pipeline_run,
            config=config,
            look_id=look_id,
            entries=entries,
            excluded=excluded,
            outputs_dir=outputs_dir,
            anchor_dir=anchor_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            plan_path=plan_path,
            tts_manifest_path=tts_manifest_path,
            plan_hash=plan_hash,
            stage_budget=stage_budget,
            total_duration=total_duration,
            total_cost=total_cost,
            counts=(submitted, reused, resumed),
        )

    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        pipeline_run.estimated_cost_usd = total_cost
        if isinstance(exc, PipelineError) and exc.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
        episode.error_message = str(exc)[:1000]
        session.commit()
        logger.error("Anchor generation failed for %s: %s", episode.episode_id, exc)
        raise


def _require_complete_run(run_result, episode_id: str) -> None:
    """Stop the stage unless every planned presenter clip actually exists.

    Fail-closed by policy: a bulletin missing its moderator is not a bulletin
    with a gap, it is a bulletin that must not be rendered. Each case is
    reported separately because the operator's next move differs — a deferred
    job wants another run, a blocked one wants reconciliation, and an open
    breaker wants somebody to look at the provider.
    """
    if run_result.breaker_blocked:
        raise AnchorReconciliationRequired(
            f"Avatar submissions for {episode_id} were stopped by the provider "
            f"circuit breaker before {len(run_result.breaker_blocked)} scene(s) were "
            "ordered. Resolve the provider condition and reset the breaker."
        )
    if run_result.failures:
        # An unambiguous refusal is not a reconciliation case: nothing was
        # bought, and the operator is better served by the provider's own error
        # than by a wrapper that hides it.
        unambiguous = [f for f in run_result.failures if not f.ambiguous and f.cause is not None]
        if len(unambiguous) == len(run_result.failures):
            raise unambiguous[0].cause
        details = "; ".join(
            f"{failure.scene_id}: {failure.message}" for failure in run_result.failures[:5]
        )
        raise AnchorReconciliationRequired(
            f"{len(run_result.failures)} presenter clip(s) of {episode_id} did not "
            f"complete: {details}"
        )
    if run_result.deferred:
        scenes = ", ".join(item.scene_id for item in run_result.deferred)
        raise TimeoutError(
            f"Presenter clips for {episode_id} are still generating at the provider "
            f"({scenes}). They are recorded and will be resumed; rerun the stage "
            "rather than reordering them."
        )
    if run_result.interrupted:
        raise AnchorReconciliationRequired(
            f"Avatar generation for {episode_id} was interrupted before every scene "
            "was ordered. The jobs already submitted are recorded; rerun to resume."
        )


def _is_unbilled_refusal(exc: Exception) -> bool:
    """Did the provider reject the request without starting a generation?

    Only a definite "no" may reset a job for retry. Anything else -- a timeout,
    a dropped connection, an unexpected status -- leaves the outcome open, and
    an open outcome must never authorise a second purchase.
    """
    from btcedu.services.anchor_service import AnchorAPIError

    if not isinstance(exc, AnchorAPIError):
        return False
    return exc.status_code in (400, 401, 403, 404, 422) or exc.error_code in (
        "quota_exceeded",
        "unexpected_output_format",
    )


def _entry_from_job(
    unit: _AnchorUnit,
    job,
    config: AnchorConfig,
    look_id: str,
    video_path: Path,
    outputs_dir: Path,
) -> SceneAnchorEntry:
    del outputs_dir
    return SceneAnchorEntry(
        scene_id=unit.scene_id,
        clip_id=unit.clip_id,
        chapter_id=unit.chapter_id,
        speaker_role=unit.speaker_role,
        template_id=unit.template_id,
        avatar_look_id=look_id,
        part_index=unit.part_index,
        audio_path=unit.audio_rel_path,
        audio_hash=unit.audio_hash,
        content_hash=job.content_hash,
        provider=job.provider,
        provider_job_id=job.provider_job_id or "",
        status=job.status,
        video_path=job.output_path or "",
        duration_seconds=job.duration_seconds,
        size_bytes=video_path.stat().st_size if video_path.exists() else 0,
        cost_usd=job.cost_usd,
        output_format=config.output_format,
        mime_type="video/webm" if config.output_format == "webm" else "video/mp4",
    )


def _dry_run_clips(
    service,
    units: list[_AnchorUnit],
    look_id: str,
    outputs_dir: Path,
    config: AnchorConfig,
) -> tuple[list[SceneAnchorEntry], float]:
    """Placeholders instead of API calls, and no ledger rows at all."""
    from btcedu.services.anchor_service import AnchorRequest

    entries: list[SceneAnchorEntry] = []
    total = 0.0
    for unit in units:
        request = AnchorRequest(
            source_image_path=config.source_image,
            source_image_url=config.source_image_url,
            audio_path=str(unit.audio_abs_path),
            chapter_id=unit.chapter_id,
            clip_id=unit.clip_id,
            expression=config.expression,
            expected_duration_seconds=unit.expected_duration_seconds,
        )
        response = service.generate_anchor_video(request)
        video_path = Path(response.video_path)
        entries.append(
            SceneAnchorEntry(
                scene_id=unit.scene_id,
                clip_id=unit.clip_id,
                chapter_id=unit.chapter_id,
                speaker_role=unit.speaker_role,
                template_id=unit.template_id,
                avatar_look_id=look_id,
                part_index=unit.part_index,
                audio_path=unit.audio_rel_path,
                audio_hash=unit.audio_hash,
                content_hash="",
                provider=response.provider,
                provider_job_id=response.provider_job_id,
                status="dry_run",
                video_path=str(video_path.relative_to(outputs_dir))
                if video_path.is_relative_to(outputs_dir)
                else str(video_path),
                duration_seconds=response.duration_seconds,
                size_bytes=response.size_bytes,
                cost_usd=0.0,
                output_format=response.output_format,
                mime_type=response.mime_type,
            )
        )
        total += response.duration_seconds
    return entries, total


def _finish_scene_anchors(
    session: Session,
    episode: Episode,
    settings: Settings,
    *,
    pipeline_run: PipelineRun,
    config: AnchorConfig,
    look_id: str,
    entries: list[SceneAnchorEntry],
    excluded: list[dict],
    outputs_dir: Path,
    anchor_dir: Path,
    manifest_path: Path,
    provenance_path: Path,
    plan_path: Path,
    tts_manifest_path: Path,
    plan_hash: str,
    stage_budget: float,
    total_duration: float,
    total_cost: float,
    counts: tuple[int, int, int],
) -> AnchorResult:
    """Write the manifest and provenance, then close the run."""
    del settings
    submitted, reused, resumed = counts
    episode_id = episode.episode_id

    manifest_data = {
        "episode_id": episode_id,
        # 2.0 is scene-based. ``segments`` stays empty on purpose: the renderer
        # still cuts by chapter, and it must not mistake one presenter clip for
        # a whole chapter's picture. WP-3 teaches it to read ``scenes``.
        "schema_version": "2.0",
        "anchor_provider": config.provider,
        "engine": config.engine,
        "avatar_look_id": look_id,
        "output_format": config.output_format,
        "cost_per_second_usd": config.cost_per_second_usd,
        "max_cost_usd": stage_budget,
        "generated_at": _utcnow().isoformat(),
        "total_duration_seconds": round(total_duration, 3),
        "total_cost_usd": round(total_cost, 6),
        "submitted_count": submitted,
        "reused_count": reused,
        "resumed_count": resumed,
        "excluded_scenes": excluded,
        "scenes": [asdict(entry) for entry in entries],
        "segments": [],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    provenance_data = {
        "stage": "anchorgen",
        "episode_id": episode_id,
        "timestamp": _utcnow().isoformat(),
        "model": f"{config.provider} ({config.engine})",
        "avatar_look_id": look_id,
        "input_files": [str(plan_path), str(tts_manifest_path)],
        "input_content_hash": plan_hash,
        "output_files": [str(manifest_path)]
        + [str(outputs_dir / e.video_path) for e in entries if e.video_path],
        "scene_count": len(entries),
        "excluded_scenes": excluded,
        "clips": [
            {
                "scene_id": e.scene_id,
                "clip_id": e.clip_id,
                "chapter_id": e.chapter_id,
                "speaker_role": e.speaker_role,
                "avatar_look_id": e.avatar_look_id,
                "audio_path": e.audio_path,
                "audio_hash": e.audio_hash,
                "content_hash": e.content_hash,
                "provider_job_id": e.provider_job_id,
                "status": e.status,
                "duration_seconds": e.duration_seconds,
                "cost_usd": e.cost_usd,
            }
            for e in entries
        ],
        "total_duration_seconds": round(total_duration, 3),
        "cost_per_second_usd": config.cost_per_second_usd,
        "max_cost_usd": stage_budget,
        "cost_usd": round(total_cost, 6),
    }
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    session.add(
        ContentArtifact(
            episode_id=episode_id,
            artifact_type="anchor_video",
            file_path="anchor/manifest.json",
            prompt_hash=plan_hash,
            model=config.provider,
            created_at=_utcnow(),
        )
    )

    render_manifest = outputs_dir / "render" / "render_manifest.json"
    if render_manifest.exists():
        render_manifest.with_suffix(".json.stale").touch()

    episode.status = EpisodeStatus.ANCHOR_GENERATED
    episode.error_message = None
    pipeline_run.status = RunStatus.SUCCESS.value
    pipeline_run.completed_at = _utcnow()
    pipeline_run.estimated_cost_usd = total_cost
    session.commit()

    logger.info(
        "Anchor generation complete for %s: %d clips (%d new, %d reused, %d resumed), "
        "%.1fs, $%.4f",
        episode_id,
        len(entries),
        submitted,
        reused,
        resumed,
        total_duration,
        total_cost,
    )

    return AnchorResult(
        episode_id=episode_id,
        anchor_dir=anchor_dir,
        manifest_path=manifest_path,
        provenance_path=provenance_path,
        segment_count=len(entries),
        total_duration_seconds=total_duration,
        cost_usd=total_cost,
        skipped=False,
        submitted_count=submitted,
        reused_count=reused,
        resumed_count=resumed,
        scene_ids=[e.scene_id for e in entries],
    )


def _compute_plan_anchor_hash(
    plan: dict,
    config: AnchorConfig,
    look_id: str,
    clip_hashes: dict[str, str],
) -> str:
    """Stage-level fingerprint: the outfit plus every clip's own identity."""
    relevant = {
        "plan_content_hash": plan.get("content_hash"),
        "provider": config.provider,
        "engine": config.engine,
        "output_format": config.output_format,
        "resolution": config.resolution,
        "aspect_ratio": config.aspect_ratio,
        "avatar_look_id": look_id,
        "clips": sorted(clip_hashes.items()),
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _resolve_anchor_config(episode: Episode, settings: Settings) -> AnchorConfig:
    """Merge profile-owned avatar choices over backward-compatible settings."""
    profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
    return resolve_anchor_config(profile_name, settings)


def _create_anchor_service(
    config: AnchorConfig,
    settings: Settings,
    anchor_dir: Path,
    avatar_id: str | None = None,
):
    """Instantiate the configured provider without leaking provider logic upward.

    ``avatar_id`` overrides the profile default with the look this episode was
    permanently assigned, so every clip of one bulletin wears one outfit.
    """
    if settings.dry_run:
        from btcedu.services.anchor_service import DryRunAnchorService

        return DryRunAnchorService(
            output_dir=str(anchor_dir),
            provider=config.provider,
            engine=config.engine,
            output_format=config.output_format,
        )

    if config.provider == "d-id":
        from btcedu.services.anchor_service import DIDService

        return DIDService(
            api_key=settings.did_api_key,
            output_dir=str(anchor_dir),
            cost_per_second_usd=config.cost_per_second_usd,
        )

    if config.provider == "heygen":
        from btcedu.services.anchor_service import HeyGenService

        return HeyGenService(
            api_key=settings.heygen_api_key,
            output_dir=str(anchor_dir),
            avatar_id=avatar_id or config.avatar_id,
            engine=config.engine,
            avatar_type=config.avatar_type,
            output_format=config.output_format,
            resolution=config.resolution,
            aspect_ratio=config.aspect_ratio,
            cost_per_second_usd=config.cost_per_second_usd,
            request_timeout_seconds=config.request_timeout_seconds,
            poll_interval_seconds=config.poll_interval_seconds,
        )

    raise ValueError(f"Unsupported anchor provider: {config.provider!r}")


def _compute_anchor_hash(
    chapters_doc: ChapterDocument,
    config: AnchorConfig,
    tts_segments: dict[str, dict],
) -> str:
    """Compute a provider- and audio-aware content hash for idempotency."""
    relevant = {
        "provider": config.provider,
        "engine": config.engine,
        "source_image": config.source_image,
        "source_image_url": config.source_image_url,
        "avatar_id": config.avatar_id,
        "avatar_type": config.avatar_type,
        "expression": config.expression,
        "output_format": config.output_format,
        "resolution": config.resolution,
        "aspect_ratio": config.aspect_ratio,
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "visual_type": chapter.visual.type.value,
                "audio_path": (tts_segments.get(chapter.chapter_id) or {}).get("file_path"),
                "audio_hash": (tts_segments.get(chapter.chapter_id) or {}).get("text_hash"),
                "duration_seconds": (tts_segments.get(chapter.chapter_id) or {}).get(
                    "duration_seconds"
                ),
            }
            for chapter in chapters_doc.chapters
            if chapter.visual.type == VisualType.TALKING_HEAD
        ],
    }
    content = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _positive_duration(value, context: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} has invalid duration_seconds") from exc
    if duration <= 0:
        raise ValueError(f"{context} has non-positive duration_seconds")
    return duration


def _ensure_anchor_budget(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    stage_budget: float,
    spent_usd: float,
    next_cost_usd: float,
    provider: str,
) -> None:
    """Reject a paid provider call before it can exceed either budget."""
    projected_stage = spent_usd + next_cost_usd
    if projected_stage > stage_budget:
        raise PipelineError(
            f"Anchor stage budget exceeded before {provider} API call: "
            f"${projected_stage:.4f} > ${stage_budget:.4f}",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )

    episode_total_cost = _get_episode_total_cost(session, episode_id)
    projected_episode = episode_total_cost + projected_stage
    if projected_episode > settings.max_episode_cost_usd:
        raise PipelineError(
            f"Episode cost limit exceeded before {provider} API call: "
            f"${projected_episode:.4f} > ${settings.max_episode_cost_usd:.4f}",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )


def _is_anchor_current(manifest_path: Path, provenance_path: Path, content_hash: str) -> bool:
    """Check if anchor output is current (idempotency)."""
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        logger.info("Anchor manifest marked as stale")
        stale_marker.unlink()
        return False

    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        return provenance.get("input_content_hash") == content_hash
    except (json.JSONDecodeError, KeyError):
        return False


def _get_episode_total_cost(session: Session, episode_id: str) -> float:
    """Get total cost across all pipeline runs for an episode.

    ``PipelineRun.episode_id`` is an integer FK to ``episodes.id``; resolve the
    string ``episode_id`` to that integer so the cost guard counts every prior
    stage rather than nothing.
    """
    from sqlalchemy import func

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is None:
        return 0.0
    result = (
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
    )
    return float(result)
