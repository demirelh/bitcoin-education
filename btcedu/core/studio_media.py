"""Which picture belongs in the studio monitor -- and never a new one.

The evening's topic media already exist. The image stage produced one picture
per presenter block, the weather subsystem rendered its own card, and the
reporter's blocks show those same files full-frame. The studio monitor is a
second *presentation* of that material, not a second production of it, so this
module only ever resolves and classifies files that are already on disk.

When a scene has no topic medium at all, the answer is a deliberate neutral
card from the studio package rather than an improvised one. If the studio does
not declare such a card, there is no defined answer and the studio counts as not
ready -- that is checked in ``studio_manifest.studio_readiness_problems``.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from btcedu.core.studio_manifest import (
    ASSET_KIND_IMAGE,
    ASSET_KIND_VIDEO,
    FIT_COVER,
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    StudioManifest,
)
from btcedu.services.studio_compositor import DisplayMedia

logger = logging.getLogger(__name__)


class DisplayMediaUnavailableError(RuntimeError):
    """No topic medium and no declared fallback. There is nothing safe to show."""


@dataclass(frozen=True)
class ResolvedMedia:
    """One editorial medium, plus how each presentation should frame it."""

    path: Path
    kind: str
    relative_path: str
    is_fallback: bool = False

    def for_monitor(self, manifest: StudioManifest, scene_fit: str = "") -> DisplayMedia:
        """The same file, framed for the studio monitor."""
        zone = manifest.display_zone
        return DisplayMedia(
            path=str(self.path),
            kind=self.kind,
            fit_mode=scene_fit or zone.fit_mode,
            focus_point=zone.focus_point,
            is_fallback=self.is_fallback,
        )

    def for_fullscreen(self, scene_fit: str = "") -> DisplayMedia:
        """The same file, framed for a reporter block that fills the frame."""
        return DisplayMedia(
            path=str(self.path),
            kind=self.kind,
            fit_mode=scene_fit or FIT_COVER,
            focus_point=(0.5, 0.5),
            is_fallback=self.is_fallback,
        )


def classify_media(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return ASSET_KIND_IMAGE
    if suffix in VIDEO_SUFFIXES:
        return ASSET_KIND_VIDEO
    raise DisplayMediaUnavailableError(f"Unsupported topic medium type: {path}")


def _safe_episode_path(outputs_dir: Path, relative: str) -> Path:
    """Resolve an episode-relative path, refusing anything outside the episode.

    Manifests are files, and a file can name any path. The episode directory is
    the boundary; a topic medium from outside it is a bug at best.
    """
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise DisplayMediaUnavailableError(f"Unsafe topic medium path: {relative!r}")
    root = Path(outputs_dir).resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents and candidate != root:
        raise DisplayMediaUnavailableError(f"Topic medium escapes the episode: {relative!r}")
    return candidate


def _chapter_media_from_manifests(
    chapter_id: str,
    beat_index: int,
    outputs_dir: Path,
    manifests: dict,
) -> str | None:
    """Fall back to the chapter's own media when the scene names none.

    Video wins over a still for the same beat: a chapter that has a clip was
    given one on purpose, and showing its thumbnail instead would quietly
    discard editorial work.
    """
    for key in ("video_manifest", "weather_manifest", "image_manifest"):
        manifest = manifests.get(key) or {}
        entries = manifest.get("videos") or manifest.get("images") or manifest.get("cards") or []
        candidates = [
            entry
            for entry in entries
            if str(entry.get("chapter_id")) == chapter_id
            and entry.get("generation_method") != "failed"
            and entry.get("file_path")
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda e: int((e.get("metadata") or {}).get("beat_index") or 0))
        pick = candidates[min(beat_index, len(candidates) - 1)]
        relative = str(pick["file_path"])
        if _safe_episode_path(outputs_dir, relative).exists():
            return relative
    return None


def resolve_scene_media(
    scene,
    outputs_dir: str | Path,
    studio: StudioManifest,
    *,
    manifests: dict | None = None,
) -> ResolvedMedia:
    """Find the editorial medium for one scene, or the declared fallback."""
    outputs = Path(outputs_dir)
    manifests = manifests or {}

    relative = str(getattr(scene, "background_asset", "") or "")
    if relative:
        try:
            path = _safe_episode_path(outputs, relative)
        except DisplayMediaUnavailableError:
            path = None
        if path is not None and path.exists():
            return ResolvedMedia(path=path, kind=classify_media(path), relative_path=relative)
        logger.warning(
            "Scene %s names %s, which is not on disk; looking for the chapter's media",
            getattr(scene, "scene_id", "?"),
            relative,
        )

    discovered = _chapter_media_from_manifests(
        str(getattr(scene, "chapter_id", "")),
        int(getattr(scene, "beat_index", 0) or 0),
        outputs,
        manifests,
    )
    if discovered:
        path = _safe_episode_path(outputs, discovered)
        return ResolvedMedia(path=path, kind=classify_media(path), relative_path=discovered)

    fallback = studio.fallback_display_media
    if fallback is None:
        raise DisplayMediaUnavailableError(
            f"Scene {getattr(scene, 'scene_id', '?')} has no topic medium and the studio "
            "declares no fallback_display_media. There is no defined picture to show."
        )
    fallback_path = studio.asset_path(fallback)
    if not fallback_path.exists():
        raise DisplayMediaUnavailableError(
            f"Studio fallback graphic is declared but missing: {fallback.path}"
        )
    return ResolvedMedia(
        path=fallback_path,
        kind=fallback.kind,
        relative_path=fallback.path,
        is_fallback=True,
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def composite_content_hash(
    *,
    studio_hash: str,
    scene_id: str,
    scene_plan_hash: str,
    display_media_hash: str,
    avatar_clip_hash: str,
    avatar_look_id: str,
    audio_hash: str,
    compositing_params: dict,
    renderer_version: str,
) -> str:
    """Fingerprint of one composited shot.

    The separation of concerns is the whole point of the argument list. Changing
    the studio changes this hash and therefore the composite and the final
    render -- but it does not touch the avatar job hash, so a repainted wall
    never re-buys a HeyGen clip. Changing the narration audio changes both,
    because a different take is a different performance to lip-sync.
    """
    relevant = {
        "studio": studio_hash,
        "scene_id": scene_id,
        "scene_plan": scene_plan_hash,
        "display_media": display_media_hash,
        "avatar_clip": avatar_clip_hash,
        "avatar_look_id": avatar_look_id,
        "audio": audio_hash,
        "params": compositing_params,
        "renderer_version": renderer_version,
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def media_digest(path: str | Path) -> str:
    """Content digest of a topic medium, empty when there is none."""
    candidate = Path(path)
    return _file_digest(candidate) if candidate.exists() else ""
