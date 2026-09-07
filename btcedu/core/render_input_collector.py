"""Gathering the byte-bound set of render inputs from an episode's manifests.

Separated from :mod:`btcedu.core.render_inputs` so the primitives (how a file
is measured, resolved and compared) stay independent of the pipeline's
knowledge of *which* files matter. This module is the one place that answers
that second question, and it answers it from the manifests the render actually
reads rather than from a list someone maintains by hand — a hand-maintained
list is a list that goes out of date the first time a stage learns a new
output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from btcedu.core.render_inputs import (
    KIND_AVATAR_CLIP,
    KIND_CARD,
    KIND_FONT,
    KIND_MUSIC_BED,
    KIND_STING_AUDIO,
    KIND_STUDIO_PLATE,
    KIND_TOPIC_MEDIA,
    KIND_TTS_AUDIO,
    ROOT_ASSETS,
    ROOT_EPISODE,
    ROOT_STUDIO,
    RenderInput,
    RootSet,
    describe_file,
)

logger = logging.getLogger(__name__)


def build_roots(
    episode_dir: str | Path,
    *,
    studio_dir: str | Path | None = None,
    assets_dir: str | Path | None = None,
) -> RootSet:
    """The named directories this episode's inputs may live in."""
    roots = RootSet()
    roots.with_root(ROOT_EPISODE, episode_dir)
    roots.with_root(ROOT_STUDIO, studio_dir)
    roots.with_root(ROOT_ASSETS, assets_dir or Path("data/assets"))
    return roots


def _add(
    collected: dict[str, RenderInput],
    missing: list[str],
    item: RenderInput | None,
    *,
    key: str,
    description: str,
    required: bool,
) -> None:
    if item is not None:
        # First writer wins. The same file can be reached through two
        # declarations (a fallback medium that is also a studio asset); the
        # first key is the more specific one and keeps the set stable.
        collected.setdefault(key, item)
        return
    if required:
        missing.append(description)


def collect_studio_inputs(
    studio, collected: dict[str, RenderInput], missing: list[str], *, roots: RootSet
) -> None:
    """Every plate, layer, mask and fallback graphic the studio declares."""
    if studio is None:
        return
    for asset in studio.assets():
        key = f"studio:{asset.path}"
        try:
            path = studio.asset_path(asset)
        except Exception as exc:  # noqa: BLE001 - a bad path is a missing asset
            missing.append(f"studio asset {asset.path!r} does not resolve: {exc}")
            continue
        kind = KIND_CARD if _looks_like_card(asset) else KIND_STUDIO_PLATE
        _add(
            collected,
            missing,
            describe_file(
                key,
                kind,
                path,
                roots=roots,
                provenance="studio manifest",
                declared_sha256=getattr(asset, "sha256", "") or "",
                preferred_root=ROOT_STUDIO,
            ),
            key=key,
            description=f"studio asset {asset.path!r} is missing",
            # A placeholder studio is a known, reported state; it must not turn
            # every unrelated render into a hard failure here.
            required=not getattr(asset, "placeholder", False),
        )


def _looks_like_card(asset) -> bool:
    name = str(getattr(asset, "path", "")).lower()
    return any(token in name for token in ("card", "intro", "outro", "opening", "closing"))


def collect_media_inputs(
    image_manifest: dict,
    collected: dict[str, RenderInput],
    missing: list[str],
    *,
    roots: RootSet,
    prefix: str = "media",
    source: str = "image manifest",
    required: bool = True,
) -> None:
    """Every topic medium: generated image, stock photo, clip or weather card.

    The three manifests that can supply a scene's picture — images, videos and
    weather cards — all use the same entry shape, so they are read by the same
    code under different key prefixes. ``studio_media`` consults all three when
    a scene names no background of its own; a set that only knew about
    ``images`` would leave every video-backed chapter unbound.
    """
    entries = (
        image_manifest.get("images")
        or image_manifest.get("videos")
        or image_manifest.get("cards")
        or []
    )
    for entry in entries:
        relative = str(entry.get("file_path") or "")
        if not relative:
            continue
        method = str(entry.get("generation_method") or "unknown")
        if method == "failed":
            # A recorded failure names no file the render will ever open.
            continue
        chapter = str(entry.get("chapter_id") or relative)
        beat = (entry.get("metadata") or {}).get("beat_index")
        key = f"{prefix}:{chapter}" if beat is None else f"{prefix}:{chapter}:{int(beat):02d}"
        _add(
            collected,
            missing,
            describe_file(
                key,
                KIND_TOPIC_MEDIA,
                relative,
                roots=roots,
                provenance=f"{source} ({method})",
                preferred_root=ROOT_EPISODE,
            ),
            key=key,
            description=f"topic medium for {chapter} is missing: {relative}",
            required=required,
        )


def collect_audio_inputs(
    tts_manifest: dict, collected: dict[str, RenderInput], missing: list[str], *, roots: RootSet
) -> None:
    """Every narration file, chapter mixes and per-speaker parts alike.

    The speaker parts matter separately from the chapter mix: a scene render
    uses the part, a chapter render uses the mix, and an episode can be
    published from either path.
    """
    for segment in tts_manifest.get("segments", []) or []:
        chapter = str(segment.get("chapter_id") or "")
        relative = str(segment.get("file_path") or "")
        if relative:
            key = f"tts:{chapter or relative}"
            _add(
                collected,
                missing,
                describe_file(
                    key,
                    KIND_TTS_AUDIO,
                    relative,
                    roots=roots,
                    provenance="tts manifest",
                    preferred_root=ROOT_EPISODE,
                ),
                key=key,
                description=f"narration audio for {chapter} is missing: {relative}",
                required=True,
            )
        metadata = segment.get("metadata") or {}
        for index, part in enumerate(metadata.get("speaker_parts", []) or []):
            part_file = str(part.get("file") or "")
            if not part_file:
                continue
            relative_part = part_file
            if "/" not in relative_part:
                relative_part = f"tts/{relative_part}"
            key = f"tts_part:{chapter}:{index:02d}"
            _add(
                collected,
                missing,
                describe_file(
                    key,
                    KIND_TTS_AUDIO,
                    relative_part,
                    roots=roots,
                    provenance="tts manifest (speaker part)",
                    preferred_root=ROOT_EPISODE,
                ),
                key=key,
                description=f"speaker part {relative_part} is missing",
                # A chapter-only render never writes parts; their absence is
                # not by itself a broken episode.
                required=False,
            )


def collect_avatar_inputs(
    anchor_manifest: dict, collected: dict[str, RenderInput], missing: list[str], *, roots: RootSet
) -> None:
    """Presenter clips, recorded here as well so one set describes the render.

    ``avatar_integrity`` remains the authority for the approval contract; this
    is the same measurement seen from the renderer's side, which is what makes
    the set complete rather than "everything except the part that matters".
    """
    for entry in anchor_manifest.get("scenes", []) or []:
        relative = str(entry.get("video_path") or "")
        if not relative:
            continue
        scene_id = str(entry.get("scene_id") or relative)
        key = f"avatar:{scene_id}"
        _add(
            collected,
            missing,
            describe_file(
                key,
                KIND_AVATAR_CLIP,
                relative,
                roots=roots,
                provenance="anchor manifest",
                declared_sha256=str(entry.get("file_sha256") or ""),
                preferred_root=ROOT_EPISODE,
            ),
            key=key,
            description=f"presenter clip for {scene_id} is missing: {relative}",
            required=True,
        )


def collect_branding_inputs(
    collected: dict[str, RenderInput],
    missing: list[str],
    *,
    roots: RootSet,
    intro_audio: str = "",
    topic_intro_audio: str = "",
    outro_audio: str = "",
    music_bed: str = "",
    font_name: str = "",
) -> None:
    """The stings, the music bed and the font.

    The font is here because a font is a render input like any other: an
    upgraded system package restyles every overlay in an already-approved
    video, and until now nothing recorded which bytes drew the text.
    """
    stings = (
        ("sting:intro", intro_audio, "render config intro_audio"),
        ("sting:topic_intro", topic_intro_audio, "render config topic_intro_audio"),
        ("sting:outro", outro_audio, "render config outro_audio"),
    )
    for key, value, provenance in stings:
        if not value:
            continue
        _add(
            collected,
            missing,
            describe_file(
                key,
                KIND_STING_AUDIO,
                value,
                roots=roots,
                provenance=provenance,
                preferred_root=ROOT_ASSETS,
            ),
            key=key,
            description=f"{provenance} names a file that is missing: {value}",
            # A configured but absent sting is already handled by the renderer,
            # which simply omits it. Recording it as fatal here would change
            # that behaviour for every profile.
            required=False,
        )

    if music_bed:
        _add(
            collected,
            missing,
            describe_file(
                "music:bed",
                KIND_MUSIC_BED,
                music_bed,
                roots=roots,
                provenance="render config music_bed",
                preferred_root=ROOT_ASSETS,
            ),
            key="music:bed",
            description=f"music bed is missing: {music_bed}",
            required=False,
        )

    if font_name:
        resolved = _resolve_font(font_name)
        if resolved is not None:
            _add(
                collected,
                missing,
                describe_file(
                    "font:overlay",
                    KIND_FONT,
                    resolved,
                    roots=roots,
                    provenance=f"render config font ({font_name})",
                ),
                key="font:overlay",
                description=f"overlay font is missing: {font_name}",
                required=False,
            )


def _resolve_font(font_name: str) -> Path | None:
    """The font file ffmpeg will actually use, when it is a file at all.

    ``find_font_path`` falls back to handing the name to fontconfig, which is
    not a path and cannot be hashed. That case is recorded by its absence
    rather than by a fabricated entry.
    """
    try:
        from btcedu.services.ffmpeg_service import find_font_path

        candidate = Path(find_font_path(font_name))
    except Exception as exc:  # noqa: BLE001 - a font lookup must not stop a render
        logger.debug("Could not resolve font %s: %s", font_name, exc)
        return None
    return candidate if candidate.is_absolute() and candidate.is_file() else None


def collect_extra_files(
    paths: dict[str, tuple[str, str]],
    collected: dict[str, RenderInput],
    missing: list[str],
    *,
    roots: RootSet,
) -> None:
    """``{key: (relative_path, kind)}`` for anything a caller knows about.

    The escape hatch for render templates, subtitle files and the fallback
    media a voice-over override substitutes — inputs whose presence depends on
    a mode rather than on a manifest.
    """
    for key, (relative, kind) in sorted(paths.items()):
        if not relative:
            continue
        _add(
            collected,
            missing,
            describe_file(
                key,
                kind,
                relative,
                roots=roots,
                provenance="render context",
                preferred_root=ROOT_EPISODE,
            ),
            key=key,
            description=f"{key} is missing: {relative}",
            required=False,
        )


def collect_render_inputs(
    *,
    roots: RootSet,
    image_manifest: dict | None = None,
    tts_manifest: dict | None = None,
    anchor_manifest: dict | None = None,
    video_manifest: dict | None = None,
    weather_manifest: dict | None = None,
    studio=None,
    intro_audio: str = "",
    topic_intro_audio: str = "",
    outro_audio: str = "",
    music_bed: str = "",
    font_name: str = "",
    extra: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[RenderInput], list[str]]:
    """Measure everything this episode's video is made of.

    Returns the recorded set and the list of inputs that were declared but are
    not on disk. The second list is what makes the set fail closed: a caller
    that receives a non-empty list must not treat the render as complete.
    """
    collected: dict[str, RenderInput] = {}
    missing: list[str] = []

    collect_studio_inputs(studio, collected, missing, roots=roots)
    collect_media_inputs(image_manifest or {}, collected, missing, roots=roots)
    # A clip or a weather card is only required if the scene that uses it says
    # so; the render falls back through the three manifests in order, so a
    # declared-but-absent video is a miss for that manifest, not for the video.
    collect_media_inputs(
        video_manifest or {},
        collected,
        missing,
        roots=roots,
        prefix="video",
        source="video manifest",
        required=False,
    )
    collect_media_inputs(
        weather_manifest or {},
        collected,
        missing,
        roots=roots,
        prefix="weather",
        source="weather manifest",
        required=False,
    )
    collect_audio_inputs(tts_manifest or {}, collected, missing, roots=roots)
    collect_avatar_inputs(anchor_manifest or {}, collected, missing, roots=roots)
    collect_branding_inputs(
        collected,
        missing,
        roots=roots,
        intro_audio=intro_audio,
        topic_intro_audio=topic_intro_audio,
        outro_audio=outro_audio,
        music_bed=music_bed,
        font_name=font_name,
    )
    collect_extra_files(extra or {}, collected, missing, roots=roots)

    return list(collected.values()), missing


# ---------------------------------------------------------------------------
# Boundary helpers
#
# One place that knows where an episode keeps its recorded set and which roots
# it lives in, so the review, the approval, the publish and the remote runner
# all ask the same question of the same files.
# ---------------------------------------------------------------------------


def _episode_dir(episode_id: str, settings) -> Path:
    return Path(settings.outputs_dir) / episode_id


def roots_for_episode(episode_id: str, settings, episode=None) -> RootSet:
    """The roots this episode's recorded inputs are relative to."""
    studio_dir = ""
    if episode is not None:
        try:
            from btcedu.core.scene_renderer import studio_directory

            studio_dir = studio_directory(settings, episode)
        except Exception as exc:  # noqa: BLE001 - a missing studio is not a root
            logger.debug("No studio root for %s: %s", episode_id, exc)
    return build_roots(_episode_dir(episode_id, settings), studio_dir=studio_dir or None)


def recorded_block(episode_id: str, settings) -> dict | None:
    """The set recorded by the last render, or ``None`` for a legacy render."""
    from btcedu.core.render_inputs import RENDER_INPUTS_KEY

    manifest = _episode_dir(episode_id, settings) / "render" / "render_manifest.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read the render manifest for %s: %s", episode_id, exc)
        return None
    block = data.get(RENDER_INPUTS_KEY)
    return block if isinstance(block, dict) else None


def verify_episode_inputs(episode_id: str, settings, episode=None):
    """``(block, results)`` for one episode, measuring what the manifest recorded."""
    from btcedu.core.render_inputs import verify_block

    block = recorded_block(episode_id, settings)
    roots = roots_for_episode(episode_id, settings, episode)
    return block, verify_block(block, roots=roots)


def require_episode_inputs_intact(episode_id: str, settings, episode=None, *, context: str):
    """Verify at a trust boundary, or refuse to cross it.

    A legacy render — one with no recorded set — passes through. There is
    nothing to compare and no evidence to act on, and refusing would strand
    every episode finished before WP-8B. The caller can ask
    :func:`btcedu.core.render_inputs.is_recorded` whether it just trusted
    something it could not check.
    """
    from btcedu.core.render_inputs import require_inputs_intact

    block = recorded_block(episode_id, settings)
    roots = roots_for_episode(episode_id, settings, episode)
    return block, require_inputs_intact(block, roots=roots, context=context)
