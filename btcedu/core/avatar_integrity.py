"""Byte-level identity for presenter clips.

An avatar clip is the one artefact in this pipeline that is expensive to make,
impossible to re-derive and shown to an audience as if a person had said it. So
it is not enough to know that *a* file sits at the path the manifest names: the
approval, the render and the remote runner all have to agree on the exact bytes
a human looked at.

Everything here is deliberately small. The hash is a plain streaming SHA-256 of
the file, stored on the job row and in the anchor manifest, and re-measured at
each boundary where the bytes are about to be trusted. There is no content
addressed store and no new place for a file to live; the recorded digest is
simply compared against what is on disk, and a disagreement stops the pipeline
instead of being repaired automatically — a mismatched clip may have been
replaced by a person who had a reason, and guessing that reason costs money.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

CHUNK_BYTES = 1 << 20

#: The recorded digest and the file agree.
INTEGRITY_OK = "ok"
#: The manifest names a clip that is not there.
INTEGRITY_MISSING = "missing"
#: The file is there and is not the file that was approved.
INTEGRITY_MISMATCH = "mismatch"
#: A clip from before this contract existed. Not trusted, not condemned.
INTEGRITY_UNRECORDED = "unrecorded"
#: The path escapes the episode directory or is not a regular file.
INTEGRITY_UNSAFE = "unsafe"

_PROBLEM_STATUSES = {
    INTEGRITY_MISSING,
    INTEGRITY_MISMATCH,
    INTEGRITY_UNRECORDED,
    INTEGRITY_UNSAFE,
}

_REMEDIES = {
    INTEGRITY_MISSING: (
        "restore the clip from the provider with `btcedu avatar-reconcile`, or "
        "request a confirmed regeneration; do not re-run the stage blindly"
    ),
    INTEGRITY_MISMATCH: (
        "the approved bytes are gone: restore them, or record a confirmed "
        "regeneration and obtain a fresh anchor approval"
    ),
    INTEGRITY_UNRECORDED: (
        "this clip predates byte-level provenance; re-run `btcedu anchorgen` "
        "so the digest is recorded, then approve the avatar review again"
    ),
    INTEGRITY_UNSAFE: (
        "the manifest points outside the episode directory; do not render, and "
        "inspect how the manifest was written"
    ),
}


class ClipIntegrityError(Exception):
    """Raised at a trust boundary when the clips are not what was approved."""

    def __init__(self, message: str, problems: list[ClipIntegrity] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


@dataclass(frozen=True)
class ClipIntegrity:
    """What one clip is, measured rather than remembered."""

    scene_id: str
    path: str
    status: str
    expected: str = ""
    actual: str = ""

    @property
    def ok(self) -> bool:
        return self.status == INTEGRITY_OK

    @property
    def remedy(self) -> str:
        return _REMEDIES.get(self.status, "")

    @property
    def detail(self) -> str:
        if self.status == INTEGRITY_OK:
            return f"{self.scene_id}: clip matches the recorded digest"
        if self.status == INTEGRITY_MISSING:
            return f"{self.scene_id}: clip {self.path} is missing"
        if self.status == INTEGRITY_UNSAFE:
            return f"{self.scene_id}: clip path {self.path} is not inside the episode output"
        if self.status == INTEGRITY_UNRECORDED:
            return f"{self.scene_id}: clip {self.path} has no recorded byte digest"
        return (
            f"{self.scene_id}: clip {self.path} changed on disk "
            f"(expected {_short(self.expected)}, found {_short(self.actual)})"
        )

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "path": self.path,
            "status": self.status,
            # Shortened on purpose: an operator compares two fingerprints, and
            # a full digest in a web page is noise, not evidence.
            "expected": _short(self.expected),
            "actual": _short(self.actual),
            "detail": self.detail,
            "remedy": self.remedy,
        }


def _short(value: str) -> str:
    text = str(value or "")
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"


def hash_file(path: str | Path, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """SHA-256 of a file, read in chunks.

    Streaming is not an optimisation here. A presenter clip is tens of
    megabytes and a bulletin has a dozen of them; reading them whole would make
    an integrity check the largest allocation in the process, on a Raspberry Pi
    that renders video at the same time.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_within(base: Path, relative: str) -> Path | None:
    """The absolute clip path, or None if it leaves the episode directory.

    Symlinks are resolved before the comparison, so a link pointing out of the
    tree is caught rather than followed.
    """
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    target = (base / candidate).resolve()
    root = base.resolve()
    if root != target and root not in target.parents:
        return None
    return target


def verify_clip(
    scene_id: str, relative_path: str, expected: str, *, base_dir: Path
) -> ClipIntegrity:
    """Measure one clip against the digest recorded when it was accepted."""
    if not relative_path:
        return ClipIntegrity(scene_id, relative_path, INTEGRITY_MISSING, expected)
    resolved = _resolved_within(base_dir, relative_path)
    if resolved is None:
        return ClipIntegrity(scene_id, relative_path, INTEGRITY_UNSAFE, expected)
    if not resolved.is_file():
        return ClipIntegrity(scene_id, relative_path, INTEGRITY_MISSING, expected)
    if not expected:
        return ClipIntegrity(scene_id, relative_path, INTEGRITY_UNRECORDED, "")
    actual = hash_file(resolved)
    if actual != expected:
        return ClipIntegrity(scene_id, relative_path, INTEGRITY_MISMATCH, expected, actual)
    return ClipIntegrity(scene_id, relative_path, INTEGRITY_OK, expected, actual)


def manifest_clip_entries(manifest: dict) -> list[tuple[str, str, str]]:
    """``(scene_id, relative_path, recorded_hash)`` for every presenter clip."""
    entries: list[tuple[str, str, str]] = []
    for entry in manifest.get("scenes", []) or []:
        entries.append(
            (
                str(entry.get("scene_id") or ""),
                str(entry.get("video_path") or ""),
                str(entry.get("file_sha256") or ""),
            )
        )
    return entries


def verify_manifest(manifest: dict, *, base_dir: Path) -> list[ClipIntegrity]:
    """Measure every clip the anchor manifest claims, in manifest order."""
    return [
        verify_clip(scene_id, path, recorded, base_dir=base_dir)
        for scene_id, path, recorded in manifest_clip_entries(manifest)
    ]


def problems(results: list[ClipIntegrity]) -> list[ClipIntegrity]:
    return [result for result in results if result.status in _PROBLEM_STATUSES]


def require_intact(manifest: dict, *, base_dir: Path, context: str) -> list[ClipIntegrity]:
    """Verify at a trust boundary, or refuse to cross it.

    The chapter-based D-ID manifest has no ``scenes`` and therefore no clips to
    check; it passes through untouched rather than being retrofitted with a
    contract that was never part of it.
    """
    results = verify_manifest(manifest, base_dir=base_dir)
    broken = problems(results)
    if broken:
        raise ClipIntegrityError(
            f"{context} refused: " + "; ".join(item.detail for item in broken),
            broken,
        )
    return results


def hash_tree_files(base_dir: Path, relative_paths: list[str]) -> dict[str, str]:
    """Digest a set of packaged files, skipping anything unsafe or absent."""
    digests: dict[str, str] = {}
    for relative in sorted(dict.fromkeys(relative_paths)):
        resolved = _resolved_within(base_dir, relative)
        if resolved is None or not resolved.is_file():
            continue
        digests[relative] = hash_file(resolved)
    return digests


def is_symlink(path: Path) -> bool:
    return os.path.islink(path)
