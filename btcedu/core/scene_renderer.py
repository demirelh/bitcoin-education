"""Cutting a news chapter into scenes, without touching a single sample of audio.

The pieces this module joins already exist. ``scene_plan.json`` says who speaks
when, the anchor manifest says which avatar clip was bought for which scene, the
image and video manifests say which picture belongs to the story, and TTS has
already produced both the per-speaker parts and the finished chapter MP3. What
was missing was the renderer's ability to cut *inside* a chapter along those
lines instead of showing one still for the whole thing.

The timing contract
-------------------

There is exactly one authority for how long a chapter is: the chapter MP3 that
TTS produced. Every scene duration is a share of that number, so the shares
always add up to it. Concretely:

1. A scene's weight is the measured length of its own ``speaker_parts`` when TTS
   recorded them, and its planned duration otherwise.
2. The weights are scaled onto the chapter's measured audio duration.
3. Any rounding difference lands on the **last** scene of the chapter, and only
   there. That keeps the sum exact and the arithmetic deterministic.
4. The pauses between speakers are already inside the chapter MP3. They are
   therefore never inserted again -- the picture cuts, the sound runs on.

The pictures are rendered **silent**, one file per scene, joined in plan order,
and only then does the untouched chapter MP3 become the single audio track, in
exactly the way :func:`btcedu.core.renderer._render_beat_chapter` has always
done it. Doing it this way is what makes the audio guarantees cheap to keep:
an intermediate cannot leak a HeyGen voice into the master if the intermediate
has no sound at all, and the final track cannot drift from the approved
narration if it is copied rather than rebuilt.

The last shot gets a few frames of headroom so that laying the audio over the
joined picture can never clip the final word.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from btcedu.config import Settings
from btcedu.core.avatar_integrity import require_intact
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    SCENE_PLAN_FILENAME,
    TEMPLATE_ANCHOR,
    TEMPLATE_ANCHOR_RETURN,
    TEMPLATE_CLOSING,
    TEMPLATE_OPENING,
    TEMPLATE_WEATHER,
    VISUAL_MODE_STUDIO,
    Scene,
    scene_audio_parts,
    scenes_from_plan,
)
from btcedu.core.studio_manifest import (
    ALPHA_MODE_OPAQUE,
    ALPHA_MODE_WEBM,
    StudioManifest,
    StudioManifestError,
    load_studio_manifest,
    studio_content_hash,
    studio_readiness_problems,
)
from btcedu.core.studio_media import (
    DisplayMediaUnavailableError,
    composite_content_hash,
    media_digest,
    resolve_scene_media,
)

logger = logging.getLogger(__name__)

# Bumped whenever the way a scene is cut or composited changes, so an upgrade
# invalidates the cached scene segments rather than mixing two renderer
# generations inside one episode.
SCENE_RENDERER_VERSION = "1.0.0"

# Matches the headroom the beat path uses. The audio is laid over the joined
# picture, so the picture must never be the shorter of the two.
SCENE_TAIL_HEADROOM_SECONDS = 0.15

# The visual segment of a scene is silent by design; this is the tolerance the
# final chapter is checked against.
DURATION_TOLERANCE_SECONDS = 0.35

_STUDIO_TEMPLATES = (
    TEMPLATE_OPENING,
    TEMPLATE_ANCHOR,
    TEMPLATE_ANCHOR_RETURN,
    TEMPLATE_WEATHER,
    TEMPLATE_CLOSING,
)

# Which studio plate each template stands in front of. Opening and closing use
# the wide intro plate when the studio package ships one; everything else is the
# ordinary presenter background.
_TEMPLATE_BACKGROUND = {
    TEMPLATE_OPENING: "intro_asset",
    TEMPLATE_CLOSING: "loop_asset",
    TEMPLATE_WEATHER: "loop_asset",
}


class SceneRenderError(RuntimeError):
    """A scene cannot be rendered from what is on disk. Always fail closed."""


@dataclass
class SceneRenderEntry:
    """One rendered scene, as recorded in the render manifest."""

    scene_id: str
    chapter_id: str
    order: int
    speaker_role: str
    template_id: str
    visual_mode: str
    avatar_look_id: str
    avatar_clip: str
    display_media: str
    display_media_hash: str
    studio_version: str
    studio_content_hash: str
    segment_path: str
    start_seconds_in_chapter: float
    start_seconds_in_episode: float
    duration_seconds: float
    audio_source: str
    audio_parts: list[str]
    compositing_mode: str
    content_hash: str
    size_bytes: int
    reused: bool = False
    used_perspective: bool = False
    used_fallback_media: bool = False


@dataclass
class SceneRenderContext:
    """Everything the renderer needs to cut by scene, resolved once."""

    base_dir: Path
    plan: dict
    scenes: list[Scene]
    plan_hash: str
    presenter_look_id: str
    anchor_clips: dict = field(default_factory=dict)
    anchor_look_id: str = ""
    anchor_schema_version: str = ""
    studio: StudioManifest | None = None
    studio_hash: str = ""
    studio_problems: list = field(default_factory=list)
    manifests: dict = field(default_factory=dict)

    def scenes_for(self, chapter_id: str) -> list[Scene]:
        selected = [s for s in self.scenes if s.chapter_id == chapter_id]
        selected.sort(key=lambda s: (s.order, s.beat_index))
        return selected


def scene_plan_file(base_dir: Path) -> Path:
    return Path(base_dir) / SCENE_PLAN_FILENAME


def studio_directory(settings: Settings, episode) -> str:
    """Where the profile keeps its studio package, or ``""``.

    Read through the anchor configuration so that a profile without a studio --
    the Bitcoin podcast, or any D-ID setup -- resolves to nothing and never
    causes a studio to be looked for.
    """
    try:
        from btcedu.core.anchor_config import load_profile_anchor_config, parse_studio

        profile_name = getattr(episode, "content_profile", "") or "bitcoin_podcast"
        raw = load_profile_anchor_config(profile_name, settings)
        return parse_studio(raw.get("studio")).asset_dir
    except Exception as exc:  # noqa: BLE001 - a missing profile is not a studio
        logger.debug("No studio configured: %s", exc)
        return ""


def load_scene_context(
    base_dir: Path,
    settings: Settings,
    episode,
    *,
    manifests: dict | None = None,
) -> SceneRenderContext | None:
    """Assemble the scene context, or ``None`` when this episode has no plan.

    Returning ``None`` is the backward-compatible answer: every episode
    rendered before the scene planner existed, every D-ID episode and every
    Bitcoin-podcast episode takes that branch and the renderer behaves exactly
    as it always has.
    """
    base_dir = Path(base_dir)
    plan_path = scene_plan_file(base_dir)
    if not plan_path.exists():
        return None

    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        scenes = scenes_from_plan(plan)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Scene plan %s is unreadable, falling back to chapters: %s", plan_path, exc)
        return None
    if not scenes:
        return None

    anchor_manifest: dict = {}
    anchor_path = base_dir / "anchor" / "manifest.json"
    if anchor_path.exists():
        try:
            anchor_manifest = json.loads(anchor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Anchor manifest %s is unreadable", anchor_path)

    # Last check before the bytes become a broadcast. The review looked at
    # specific footage; between that signature and this encode the files sat on
    # a disk that other processes can write to, so they are measured once more
    # here — for the local render and, since the runner calls the same
    # function, for the remote one as well.
    require_intact(anchor_manifest, base_dir=base_dir, context="scene render")

    clips = {
        str(entry.get("scene_id")): entry
        for entry in anchor_manifest.get("scenes", [])
        if entry.get("scene_id")
    }

    studio, studio_hash, problems = _load_studio(settings, episode)

    return SceneRenderContext(
        base_dir=base_dir,
        plan=plan,
        scenes=scenes,
        plan_hash=str(plan.get("content_hash") or ""),
        presenter_look_id=str(plan.get("presenter_look_id") or ""),
        anchor_clips=clips,
        anchor_look_id=str(anchor_manifest.get("avatar_look_id") or ""),
        anchor_schema_version=str(anchor_manifest.get("schema_version") or ""),
        studio=studio,
        studio_hash=studio_hash,
        studio_problems=problems,
        manifests=manifests or {},
    )


def _load_studio(settings: Settings, episode) -> tuple[StudioManifest | None, str, list]:
    asset_dir = studio_directory(settings, episode)
    if not asset_dir:
        return None, "", []
    manifest_path = Path(asset_dir) / "manifest.json"
    if not manifest_path.exists():
        return None, "", [f"Studio manifest not found: {manifest_path}"]
    try:
        manifest = load_studio_manifest(manifest_path)
    except StudioManifestError as exc:
        return None, "", [f"Studio manifest is invalid: {exc}"]
    return manifest, studio_content_hash(manifest), list(studio_readiness_problems(manifest))


def scene_durations(
    scenes: list[Scene],
    parts: list[dict],
    total_duration: float,
) -> list[float]:
    """Share the chapter's measured audio duration across its scenes.

    The weights come from the per-speaker parts TTS measured, because those are
    the only numbers that describe the real recording. The planned durations are
    the fallback for an episode whose TTS manifest predates speaker parts.
    """
    if not scenes:
        return []
    weights: list[float] = []
    for scene in scenes:
        measured = 0.0
        for index in scene.segment_indices or []:
            if 0 <= int(index) < len(parts):
                measured += float(parts[int(index)].get("duration_seconds") or 0.0)
        if measured > 0:
            weights.append(measured)
        else:
            weights.append(max(float(scene.expected_duration_seconds or 0.0), 0.1))

    total_weight = sum(weights)
    if total_weight <= 0:
        share = total_duration / len(scenes)
        return [round(share, 3)] * len(scenes)

    durations = [round(total_duration * w / total_weight, 3) for w in weights]
    # The rounding difference is settled once, on the last scene, so the shares
    # add up to the chapter exactly and do so the same way on every machine.
    durations[-1] = round(total_duration - sum(durations[:-1]), 3)
    return durations


def template_background(manifest: StudioManifest, template_id: str) -> str | None:
    """Absolute path of the plate this template stands in front of, or None."""
    attribute = _TEMPLATE_BACKGROUND.get(template_id)
    if not attribute:
        return None
    asset = getattr(manifest, attribute, None)
    if asset is None:
        return None
    return str(manifest.asset_path(asset))


def is_studio_scene(scene: Scene) -> bool:
    return scene.visual_mode == VISUAL_MODE_STUDIO and scene.template_id in _STUDIO_TEMPLATES


def resolve_avatar_clip(ctx: SceneRenderContext, scene: Scene) -> tuple[Path, str]:
    """The already-bought clip for a scene, or a fail-closed error.

    Rendering must never be a reason to contact HeyGen. If the clip is not on
    disk the honest answer is to stop, not to order one, because a render is
    allowed to cost nothing.
    """
    entry = ctx.anchor_clips.get(scene.scene_id)
    if not entry:
        raise SceneRenderError(
            f"Scene {scene.scene_id} needs an avatar clip but the anchor manifest has "
            "no entry for it. Run the anchorgen stage; the renderer never orders one."
        )
    status = str(entry.get("status") or "")
    if status in {"failed", "reserved", "reconcile_required", ""}:
        raise SceneRenderError(
            f"Scene {scene.scene_id} has an avatar job in state {status!r}, which is not a "
            "finished clip. Resolve it before rendering."
        )
    look_id = str(entry.get("avatar_look_id") or "")
    expected = ctx.presenter_look_id or ctx.anchor_look_id
    if expected and look_id and look_id != expected:
        raise SceneRenderError(
            f"Scene {scene.scene_id} was generated with look {look_id!r} but this episode is "
            f"assigned look {expected!r}. One outfit per episode -- refusing to mix them."
        )
    relative = str(entry.get("video_path") or "")
    if not relative:
        raise SceneRenderError(f"Scene {scene.scene_id} has no avatar clip path")
    clip = (ctx.base_dir / relative).resolve()
    if not clip.is_relative_to(ctx.base_dir.resolve()):
        raise SceneRenderError(f"Avatar clip escapes the episode directory: {relative}")
    if not clip.exists():
        raise SceneRenderError(f"Avatar clip for scene {scene.scene_id} is missing: {relative}")
    return clip, look_id or expected


def require_studio(ctx: SceneRenderContext) -> StudioManifest:
    if ctx.studio is None:
        raise SceneRenderError(
            "This episode has studio scenes but no usable studio manifest: "
            + "; ".join(str(p) for p in ctx.studio_problems or ["none configured"])
        )
    if ctx.studio_problems:
        raise SceneRenderError(
            "The studio package is not ready: " + "; ".join(str(p) for p in ctx.studio_problems)
        )
    return ctx.studio


def scene_content_hash(
    ctx: SceneRenderContext,
    scene: Scene,
    *,
    avatar_clip_hash: str,
    display_media_hash: str,
    audio_hash: str,
    duration: float,
    overlay_fingerprint: dict,
) -> str:
    """Fingerprint of one rendered scene.

    Deliberately routed through :func:`composite_content_hash` so the studio,
    the topic medium, the avatar clip, the narration and the overlays each sit
    in their own slot. That separation is what lets a repainted studio
    invalidate the picture without ever looking like a reason to buy a clip.
    """
    return composite_content_hash(
        studio_hash=ctx.studio_hash,
        scene_id=scene.scene_id,
        scene_plan_hash=ctx.plan_hash,
        display_media_hash=display_media_hash,
        avatar_clip_hash=avatar_clip_hash,
        avatar_look_id=ctx.presenter_look_id,
        audio_hash=audio_hash,
        compositing_params={
            "template_id": scene.template_id,
            "visual_mode": scene.visual_mode,
            "speaker_role": scene.speaker_role,
            "duration": round(float(duration), 3),
            "fit_mode": scene.display_fit_mode,
            "focus_point": scene.focus_point,
            "overlays": overlay_fingerprint,
        },
        renderer_version=SCENE_RENDERER_VERSION,
    )


def scene_hash_inputs(ctx: SceneRenderContext | None) -> dict | None:
    """The scene-level part of the render content hash.

    Returned as its own block so an episode without a plan produces ``None``
    and therefore exactly the hash it produced before this module existed --
    which is what keeps every already-rendered episode from re-rendering.
    """
    if ctx is None:
        return None
    return {
        "renderer_version": SCENE_RENDERER_VERSION,
        "scene_plan_hash": ctx.plan_hash,
        "presenter_look_id": ctx.presenter_look_id,
        "anchor_look_id": ctx.anchor_look_id,
        "anchor_schema_version": ctx.anchor_schema_version,
        "studio_content_hash": ctx.studio_hash,
        "studio_version": ctx.studio.studio_version if ctx.studio else "",
        "clips": sorted(
            (
                str(entry.get("scene_id")),
                str(entry.get("content_hash") or ""),
                str(entry.get("video_path") or ""),
                str(entry.get("avatar_look_id") or ""),
            )
            for entry in ctx.anchor_clips.values()
        ),
        "scenes": [
            {
                "scene_id": s.scene_id,
                "chapter_id": s.chapter_id,
                "order": s.order,
                "role": s.speaker_role,
                "template_id": s.template_id,
                "visual_mode": s.visual_mode,
                "background_asset": s.background_asset,
                "text_hash": s.text_hash,
                "audio_file": s.audio_file,
            }
            for s in ctx.scenes
        ],
    }


def _file_digest(path: Path) -> str:
    return media_digest(path)


def _overlay_type(spec) -> str:
    raw = getattr(spec, "type", "")
    return str(getattr(raw, "value", raw))


def render_scene_chapter(
    *,
    ctx: SceneRenderContext,
    chapter,
    scenes: list[Scene],
    parts: list[dict],
    audio_path,
    output_path,
    duration: float,
    overlays,
    fade_in_duration: float,
    fade_out_duration: float,
    settings: Settings,
    font: str,
    enhancement_kwargs,
    episode_offset_seconds: float = 0.0,
) -> tuple[object, list[SceneRenderEntry]]:
    """Render one chapter scene by scene, then lay the original audio over it.

    Mirrors the beat path deliberately: silent shots, one concat, one
    ``replace_audio_track``. The only new thing is what a shot may contain --
    a presenter composited into the studio instead of only a still.
    """
    from btcedu.services.ffmpeg_service import (
        concatenate_segments,
        create_segment,
        create_video_segment,
        generate_silent_audio,
        probe_media,
        replace_audio_track,
    )

    output_path = Path(output_path)
    scenes_dir = output_path.parent / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    durations = scene_durations(scenes, parts, duration)
    durations[-1] = round(durations[-1] + SCENE_TAIL_HEADROOM_SECONDS, 3)

    audio_hash = _file_digest(Path(audio_path))
    overlay_fingerprint = {
        "count": len(overlays or []),
        "fade_in": round(float(fade_in_duration), 3),
        "fade_out": round(float(fade_out_duration), 3),
        "specs": [
            {
                "type": _overlay_type(spec),
                "text": getattr(spec, "text", ""),
                "start": getattr(spec, "start_offset_seconds", 0.0),
                "duration": getattr(spec, "duration_seconds", 0.0),
            }
            for spec in (overlays or [])
        ],
    }

    shot_paths: list[str] = []
    entries: list[SceneRenderEntry] = []
    cursor = 0.0

    for index, (scene, shot_duration) in enumerate(zip(scenes, durations, strict=True)):
        shot_path = scenes_dir / f"{chapter.chapter_id}_{scene.scene_id}.mp4"
        # The lower third belongs to the story, so it is drawn once, on the
        # chapter's first shot. A speaker change is a cut, not a new caption.
        shot_overlays = overlays if index == 0 else []
        fade_in = fade_in_duration if index == 0 else 0.0
        fade_out = fade_out_duration if index == len(scenes) - 1 else 0.0

        media = _resolve_display_media(ctx, scene)
        display_media_hash = _file_digest(media.path) if media else ""

        avatar_clip: Path | None = None
        look_id = ""
        avatar_clip_hash = ""
        studio: StudioManifest | None = None
        if is_studio_scene(scene):
            studio = require_studio(ctx)
            avatar_clip, look_id = resolve_avatar_clip(ctx, scene)
            avatar_clip_hash = str(
                (ctx.anchor_clips.get(scene.scene_id) or {}).get("content_hash") or ""
            ) or _file_digest(avatar_clip)

        content_hash = scene_content_hash(
            ctx,
            scene,
            avatar_clip_hash=avatar_clip_hash,
            display_media_hash=display_media_hash,
            audio_hash=audio_hash,
            duration=shot_duration,
            overlay_fingerprint=overlay_fingerprint,
        )

        reused = _reuse_shot(shot_path, content_hash, probe_media)
        used_perspective = False
        used_fallback = bool(media and media.is_fallback)

        if not reused:
            silent_audio = scenes_dir / f"{chapter.chapter_id}_{scene.scene_id}.m4a"
            generate_silent_audio(str(silent_audio), duration=shot_duration)
            if studio is not None:
                used_perspective = _render_studio_shot(
                    ctx=ctx,
                    studio=studio,
                    scene=scene,
                    avatar_clip=avatar_clip,
                    media=media,
                    silent_audio=silent_audio,
                    shot_path=shot_path,
                    shot_duration=shot_duration,
                    overlays=shot_overlays,
                    fade_in=fade_in,
                    fade_out=fade_out,
                    settings=settings,
                    font=font,
                    enhancement_kwargs=enhancement_kwargs,
                    chapter=chapter,
                    index=index,
                    create_segment=create_segment,
                    create_video_segment=create_video_segment,
                )
            else:
                _render_fullscreen_shot(
                    media=media,
                    silent_audio=silent_audio,
                    shot_path=shot_path,
                    shot_duration=shot_duration,
                    overlays=shot_overlays,
                    fade_in=fade_in,
                    fade_out=fade_out,
                    settings=settings,
                    font=font,
                    enhancement_kwargs=enhancement_kwargs,
                    chapter=chapter,
                    index=index,
                    create_segment=create_segment,
                    create_video_segment=create_video_segment,
                )
            _write_shot_marker(shot_path, content_hash)

        shot_paths.append(str(shot_path))
        entries.append(
            SceneRenderEntry(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                order=scene.order,
                speaker_role=scene.speaker_role,
                template_id=scene.template_id,
                visual_mode=scene.visual_mode,
                avatar_look_id=look_id,
                avatar_clip=_relative(ctx.base_dir, avatar_clip),
                display_media=_relative(ctx.base_dir, media.path if media else None),
                display_media_hash=display_media_hash,
                studio_version=studio.studio_version if studio else "",
                studio_content_hash=ctx.studio_hash if studio else "",
                segment_path=_relative(ctx.base_dir, shot_path),
                start_seconds_in_chapter=round(cursor, 3),
                start_seconds_in_episode=round(episode_offset_seconds + cursor, 3),
                duration_seconds=shot_duration,
                # Named, not embedded: the picture is silent and the chapter's
                # own MP3 becomes the one and only track further down.
                audio_source=_relative(ctx.base_dir, Path(audio_path)),
                audio_parts=scene_audio_parts(scene, parts),
                compositing_mode=(
                    studio.alpha_mode if studio else "fullscreen_media"
                ),
                content_hash=content_hash,
                size_bytes=shot_path.stat().st_size if shot_path.exists() else 0,
                reused=reused,
                used_perspective=used_perspective,
                used_fallback_media=used_fallback,
            )
        )
        cursor += shot_duration
        logger.info(
            "  scene %d/%d of %s: %s %s, %.2fs%s",
            index + 1,
            len(scenes),
            chapter.chapter_id,
            scene.speaker_role,
            scene.template_id,
            shot_duration,
            " (reused)" if reused else "",
        )

    silent_chapter = scenes_dir / f"{chapter.chapter_id}_silent.mp4"
    concatenate_segments(shot_paths, str(silent_chapter))
    result = replace_audio_track(
        video_path=str(silent_chapter),
        audio_path=str(audio_path),
        output_path=str(output_path),
        audio_bitrate=settings.render_audio_bitrate,
        timeout_seconds=settings.render_timeout_segment,
    )
    _verify_chapter_output(output_path, duration, probe_media)
    return result, entries


def _relative(base_dir: Path, path: Path | None) -> str:
    if path is None:
        return ""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(Path(base_dir).resolve()))
    except ValueError:
        return str(candidate)


def _resolve_display_media(ctx: SceneRenderContext, scene: Scene):
    """The editorial medium for a scene, or ``None`` when there cannot be one."""
    if ctx.studio is None and scene.speaker_role == ROLE_ANCHOR:
        return None
    try:
        return resolve_scene_media(
            scene,
            ctx.base_dir,
            ctx.studio,
            manifests=ctx.manifests,
        )
    except (DisplayMediaUnavailableError, AttributeError) as exc:
        raise SceneRenderError(
            f"Scene {scene.scene_id} has no topic medium to show: {exc}"
        ) from exc


def _marker_path(shot_path: Path) -> Path:
    return shot_path.with_suffix(".hash")


def _write_shot_marker(shot_path: Path, content_hash: str) -> None:
    _marker_path(shot_path).write_text(content_hash, encoding="utf-8")


def _reuse_shot(shot_path: Path, content_hash: str, probe) -> bool:
    """Is the shot on disk the shot these inputs describe, and is it playable?

    Both halves matter. The hash decides whether the inputs still agree; the
    probe decides whether the file survived the last interruption. A shot that
    fails either is re-rendered on its own, without disturbing its neighbours.
    """
    marker = _marker_path(shot_path)
    if not shot_path.exists() or not marker.exists():
        return False
    try:
        if marker.read_text(encoding="utf-8").strip() != content_hash:
            return False
    except OSError:
        return False
    if shot_path.stat().st_size == 0:
        return False
    try:
        probe(str(shot_path))
    except Exception:  # noqa: BLE001 - a broken shot is simply re-rendered
        logger.warning("Scene shot %s is unreadable, re-rendering it", shot_path.name)
        shot_path.unlink(missing_ok=True)
        return False
    return True


def _render_studio_shot(
    *,
    ctx: SceneRenderContext,
    studio: StudioManifest,
    scene: Scene,
    avatar_clip: Path | None,
    media,
    silent_audio: Path,
    shot_path: Path,
    shot_duration: float,
    overlays,
    fade_in: float,
    fade_out: float,
    settings: Settings,
    font: str,
    enhancement_kwargs,
    chapter,
    index: int,
    create_segment,
    create_video_segment,
) -> bool:
    """Composite the presenter into the studio, then apply chapter overlays.

    Two passes on purpose. The compositor owns the studio and knows nothing
    about lower thirds; the existing segment builders own the overlays, the
    ticker and the fades and know nothing about alpha. Keeping them apart is
    what stops this from becoming a second render pipeline.
    """
    from btcedu.services.studio_compositor import (
        StudioCompositeError,
        StudioCompositeRequest,
        composite_studio_scene,
    )

    raw_path = shot_path.with_name(f"{shot_path.stem}_studio.mp4")
    request = StudioCompositeRequest(
        manifest=studio,
        audio_path=str(silent_audio),
        output_path=str(raw_path),
        scene_id=scene.scene_id,
        avatar_clip=str(avatar_clip) if avatar_clip else None,
        display_media=media.for_monitor(studio, scene.display_fit_mode) if media else None,
        background_override=template_background(studio, scene.template_id),
        duration_seconds=shot_duration,
    )
    try:
        composite = composite_studio_scene(
            request,
            dry_run=settings.dry_run,
            timeout_seconds=settings.render_timeout_segment,
        )
    except StudioCompositeError as exc:
        raise SceneRenderError(f"Scene {scene.scene_id} could not be composited: {exc}") from exc

    if studio.alpha_mode == ALPHA_MODE_OPAQUE:
        logger.info("Scene %s used the opaque studio fallback", scene.scene_id)
    elif studio.alpha_mode == ALPHA_MODE_WEBM:
        logger.debug("Scene %s composited from an alpha clip", scene.scene_id)

    create_video_segment(
        video_path=str(raw_path),
        audio_path=str(silent_audio),
        output_path=str(shot_path),
        duration=shot_duration,
        overlays=overlays,
        resolution=settings.render_resolution,
        fps=settings.render_fps,
        crf=settings.render_crf,
        preset=settings.render_preset,
        audio_bitrate=settings.render_audio_bitrate,
        font=font,
        fade_in_duration=fade_in,
        fade_out_duration=fade_out,
        timeout_seconds=settings.render_timeout_segment,
        dry_run=settings.dry_run,
        **enhancement_kwargs("video", chapter.order - 1 + index),
    )
    del create_segment
    return bool(composite.used_perspective)


def _render_fullscreen_shot(
    *,
    media,
    silent_audio: Path,
    shot_path: Path,
    shot_duration: float,
    overlays,
    fade_in: float,
    fade_out: float,
    settings: Settings,
    font: str,
    enhancement_kwargs,
    chapter,
    index: int,
    create_segment,
    create_video_segment,
) -> None:
    """A reporter block: the story's own picture, full frame, no studio."""
    if media is None:
        raise SceneRenderError("A full-frame scene needs a topic medium")
    common = {
        "audio_path": str(silent_audio),
        "output_path": str(shot_path),
        "duration": shot_duration,
        "overlays": overlays,
        "resolution": settings.render_resolution,
        "fps": settings.render_fps,
        "crf": settings.render_crf,
        "preset": settings.render_preset,
        "audio_bitrate": settings.render_audio_bitrate,
        "font": font,
        "fade_in_duration": fade_in,
        "fade_out_duration": fade_out,
        "timeout_seconds": settings.render_timeout_segment,
        "dry_run": settings.dry_run,
    }
    if media.kind == "video":
        create_video_segment(
            video_path=str(media.path),
            **common,
            **enhancement_kwargs("video", chapter.order - 1 + index),
        )
    else:
        create_segment(
            image_path=str(media.path),
            **common,
            **enhancement_kwargs("photo", chapter.order - 1 + index),
        )


def _verify_chapter_output(output_path: Path, expected_duration: float, probe) -> None:
    """ffprobe has the last word on duration and on the number of audio tracks."""
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise SceneRenderError(f"Chapter render produced nothing usable: {output_path}")
    try:
        info = probe(str(output_path))
    except Exception as exc:  # noqa: BLE001 - an unreadable chapter is fatal
        raise SceneRenderError(f"Chapter render is unreadable: {output_path}: {exc}") from exc

    actual = float(getattr(info, "duration_seconds", 0.0) or 0.0)
    if actual > 0 and abs(actual - expected_duration) > DURATION_TOLERANCE_SECONDS:
        raise SceneRenderError(
            f"Chapter {output_path.name} is {actual:.2f}s but its narration is "
            f"{expected_duration:.2f}s. Refusing a render that would cut the audio."
        )
    if getattr(info, "codec_audio", "aac") is None:
        raise SceneRenderError(f"Chapter {output_path.name} has no audio track")


def scene_manifest_block(ctx: SceneRenderContext, entries: list[SceneRenderEntry]) -> dict:
    """The scene section of the render manifest.

    Added alongside the existing keys rather than in place of them, so a reader
    that only knows ``segments`` and ``timeline`` keeps working unchanged.
    """
    return {
        "scene_plan_hash": ctx.plan_hash,
        "presenter_look_id": ctx.presenter_look_id,
        "studio_version": ctx.studio.studio_version if ctx.studio else "",
        "studio_content_hash": ctx.studio_hash,
        "scene_renderer_version": SCENE_RENDERER_VERSION,
        "scenes": [asdict(entry) for entry in entries],
    }


def digest_of(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
