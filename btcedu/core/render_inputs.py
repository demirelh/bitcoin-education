"""Byte-level identity for everything that reaches the final video.

WP-6A bound the one artefact that is expensive and impossible to re-derive: the
presenter clip. Everything else the renderer consumes was still identified by
its *path* and, at best, by a hash of the text that produced it. That leaves a
gap wide enough to walk through:

* replacing ``images/ch01.png`` with different bytes leaves the render content
  hash untouched, so the renderer reports itself current and the episode is
  published with a picture nobody reviewed;
* replacing a TTS take changes no recorded value either — the manifest carries
  ``text_hash``, which is a hash of what was *asked for*, not of what came back;
* a system font upgrade silently restyles every overlay in an approved video;
* a studio plate is only compared against a digest the studio manifest itself
  declares, and that field is optional.

This module closes that by measuring the bytes of every local file that
influences the finished picture or sound, recording each one with its size,
kind and provenance, and re-measuring the set at every boundary where those
bytes are about to be trusted: the render itself, the remote render package and
its return, the review, the final approval and the publish.

Three deliberate choices are worth stating.

**Measured, not declared.** A digest that a manifest declares about itself is a
claim; this module always reads the file. The studio manifest's own ``sha256``
field remains useful as a cross-check, and a disagreement between the declared
and the measured value is reported rather than resolved.

**Refuse, do not repair.** A changed input may have been changed by a person
with a reason. Guessing that reason means either publishing something nobody
approved or discarding work; both are worse than stopping.

**Legacy is unrecorded, not condemned.** An episode rendered before this
contract existed has no recorded set. It is reported as such and allowed
through, because there is nothing to compare and refusing would strand every
finished episode on the machine. A set that exists but is *incomplete* is a
different matter and is refused: a half-written contract is a bug, not history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bumped when the recorded shape changes in a way a reader must notice.
RENDER_INPUTS_SCHEMA_VERSION = "1.0"

#: Manifest/provenance key holding the recorded set.
RENDER_INPUTS_KEY = "render_inputs"

CHUNK_BYTES = 1 << 20

# --- roots -----------------------------------------------------------------
#
# Every input is recorded relative to a named root rather than as a bare path.
# Two reasons: the same episode directory has different absolute paths on the
# Pi and on the GitHub runner, and a relative path plus a root is something a
# containment check can actually be performed against.

ROOT_EPISODE = "episode"
ROOT_STUDIO = "studio"
ROOT_ASSETS = "assets"
#: Files outside any managed tree — system fonts, chiefly. Recorded by their
#: resolved absolute path because there is no tree to be relative to.
ROOT_SYSTEM = "system"

# --- kinds -----------------------------------------------------------------

KIND_STUDIO_PLATE = "studio_plate"
KIND_TOPIC_MEDIA = "topic_media"
KIND_CARD = "card"
KIND_TTS_AUDIO = "tts_audio"
KIND_STING_AUDIO = "sting_audio"
KIND_MUSIC_BED = "music_bed"
KIND_FONT = "font"
KIND_AVATAR_CLIP = "avatar_clip"
KIND_TEMPLATE = "template"
KIND_SUBTITLE = "subtitle"

# --- statuses --------------------------------------------------------------

#: The recorded digest and the file agree.
INPUT_OK = "ok"
#: The set names a file that is not there.
INPUT_MISSING = "missing"
#: The file is there and is not the file that was recorded.
INPUT_MISMATCH = "mismatch"
#: An entry in a recorded set that carries no digest.
INPUT_UNRECORDED = "unrecorded"
#: The path escapes its root, is not a regular file, or names an unknown root.
INPUT_UNSAFE = "unsafe"

_PROBLEM_STATUSES = frozenset({INPUT_MISSING, INPUT_MISMATCH, INPUT_UNRECORDED, INPUT_UNSAFE})

_REMEDIES = {
    INPUT_MISSING: (
        "restore the file, or re-run the stage that produces it; do not approve "
        "or publish a video whose inputs are no longer on disk"
    ),
    INPUT_MISMATCH: (
        "the reviewed bytes are gone: restore them, or re-render and obtain a "
        "fresh approval for what the video now actually contains"
    ),
    INPUT_UNRECORDED: (
        "this entry was written without a digest, which should not happen; "
        "re-run the render so the set is recorded completely"
    ),
    INPUT_UNSAFE: (
        "the recorded path leaves its root or is not a regular file; do not "
        "render or publish, and inspect how the manifest was written"
    ),
}


class RenderInputError(Exception):
    """Raised at a trust boundary when the inputs are not what was recorded."""

    def __init__(self, message: str, problems: list[InputIntegrity] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


@dataclass(frozen=True)
class RenderInput:
    """One local file that reaches the finished video."""

    key: str
    """Stable identity of the *slot*, e.g. ``tts:ch01``. Survives a renamed file
    so a diff of two sets reads as "this input changed" rather than "one
    vanished and another appeared"."""

    kind: str
    root: str
    path: str
    sha256: str
    size_bytes: int
    media_type: str
    provenance: str
    """What declared this input: a manifest name, a setting, a profile key."""

    declared_sha256: str = ""
    """A digest the source claimed about itself, where it claims one. Recorded
    for the cross-check, never used in place of the measurement."""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InputIntegrity:
    """What one recorded input is now, measured rather than remembered."""

    key: str
    kind: str
    root: str
    path: str
    status: str
    expected: str = ""
    actual: str = ""

    @property
    def ok(self) -> bool:
        return self.status == INPUT_OK

    @property
    def remedy(self) -> str:
        return _REMEDIES.get(self.status, "")

    @property
    def detail(self) -> str:
        where = f"{self.root}:{self.path}"
        if self.status == INPUT_OK:
            return f"{self.key}: {where} matches the recorded digest"
        if self.status == INPUT_MISSING:
            return f"{self.key}: {where} is missing"
        if self.status == INPUT_UNSAFE:
            return f"{self.key}: {where} is not a safe, regular file inside its root"
        if self.status == INPUT_UNRECORDED:
            return f"{self.key}: {where} has no recorded byte digest"
        return (
            f"{self.key}: {where} changed on disk "
            f"(expected {short_digest(self.expected)}, found {short_digest(self.actual)})"
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "root": self.root,
            "path": self.path,
            "status": self.status,
            # Shortened on purpose: an operator compares two fingerprints, and a
            # full digest in a web page is noise, not evidence.
            "expected": short_digest(self.expected),
            "actual": short_digest(self.actual),
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class RootSet:
    """The named directories inputs may be recorded relative to."""

    roots: dict[str, Path] = field(default_factory=dict)

    def with_root(self, name: str, path: str | Path | None) -> RootSet:
        if path:
            self.roots[name] = Path(path)
        return self

    def get(self, name: str) -> Path | None:
        return self.roots.get(name)

    def resolve(self, root: str, relative: str) -> Path | None:
        """The absolute path of a recorded input, or ``None`` when unsafe."""
        return safe_resolve(self, root, relative)


def short_digest(value: str) -> str:
    text = str(value or "")
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"


def hash_file(path: str | Path, *, chunk_bytes: int = CHUNK_BYTES) -> str:
    """SHA-256 of a file, read in chunks.

    Streaming is not an optimisation. A bulletin's inputs run to hundreds of
    megabytes across a dozen files, and this runs on a Raspberry Pi that is
    encoding video at the same time; reading them whole would make the
    integrity check the largest allocation in the process.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_resolve(roots: RootSet, root: str, relative: str) -> Path | None:
    """Resolve a recorded path, refusing anything that leaves its root.

    Symlinks are resolved *before* the containment test, so a link inside the
    tree cannot be used as a door out of it. ``..`` is refused outright rather
    than normalised away, because a path that needed normalising was not the
    path anyone meant to write.
    """
    if not relative:
        return None
    if root == ROOT_SYSTEM:
        candidate = Path(relative)
        if not candidate.is_absolute():
            return None
        return Path(os.path.realpath(candidate))

    base = roots.get(root)
    if base is None:
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    target = (base / candidate).resolve()
    anchor = base.resolve()
    if anchor != target and anchor not in target.parents:
        return None
    return target


def _relative_to(base: Path | None, path: Path) -> str | None:
    """``path`` expressed inside ``base``, or ``None`` when it is not inside."""
    if base is None:
        return None
    try:
        anchor = base.resolve()
        target = path.resolve()
    except OSError:
        return None
    if anchor != target and anchor not in target.parents:
        return None
    return target.relative_to(anchor).as_posix()


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def describe_file(
    key: str,
    kind: str,
    path: str | Path,
    *,
    roots: RootSet,
    provenance: str,
    declared_sha256: str = "",
    preferred_root: str = "",
) -> RenderInput | None:
    """Measure one file and place it under the most specific root that holds it.

    Returns ``None`` for a file that is absent or is not a regular file. An
    input that cannot be measured is not recorded as measured — the absence is
    reported by the collector that asked for it, which knows whether the file
    was required.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        base = roots.get(preferred_root) if preferred_root else None
        if base is None:
            return None
        resolved = safe_resolve(roots, preferred_root, candidate.as_posix())
        if resolved is None:
            return None
        candidate = resolved
    if not candidate.is_file():
        return None

    order = [preferred_root] if preferred_root else []
    order += [ROOT_EPISODE, ROOT_STUDIO, ROOT_ASSETS]
    chosen_root = ROOT_SYSTEM
    chosen_path = str(Path(os.path.realpath(candidate)))
    for name in order:
        if not name or name == ROOT_SYSTEM:
            continue
        relative = _relative_to(roots.get(name), candidate)
        if relative is not None:
            chosen_root = name
            chosen_path = relative
            break

    try:
        size = candidate.stat().st_size
    except OSError:
        return None

    return RenderInput(
        key=key,
        kind=kind,
        root=chosen_root,
        path=chosen_path,
        sha256=hash_file(candidate),
        size_bytes=int(size),
        media_type=_media_type(candidate),
        provenance=provenance,
        declared_sha256=str(declared_sha256 or ""),
    )


def inputs_digest(inputs: list[RenderInput]) -> str:
    """One fingerprint over the shipped part of the set.

    Includes the key, the kind and the digest but not the path: moving a file
    between two roots that both hold the same bytes is not a change to the
    video, and a digest that says otherwise would re-render an episode for a
    directory rename.

    Excludes :data:`ROOT_SYSTEM` — in practice the overlay font. This number is
    also the number a GitHub runner recomputes to prove it rendered the same
    episode, and the runner's ``fonts-noto`` package is not the Pi's. Folding a
    system file into it would make every remote render disagree by design and
    send every episode back to the Pi. System files are still recorded and
    still verified; they are verified *on the machine that has them*, which is
    the only place the comparison means anything. The cross-machine guard for
    the font stays what it already was: ``remote_render`` compares the resolved
    font *file name* on both sides.
    """
    payload = sorted(
        (item.key, item.kind, item.sha256) for item in inputs if item.root != ROOT_SYSTEM
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def inputs_block(inputs: list[RenderInput]) -> dict:
    """The recorded set as it is written into a manifest or provenance file."""
    ordered = sorted(inputs, key=lambda item: (item.kind, item.key))
    return {
        "schema_version": RENDER_INPUTS_SCHEMA_VERSION,
        "digest": inputs_digest(ordered),
        "count": len(ordered),
        "entries": [item.to_dict() for item in ordered],
    }


def block_entries(block: dict | None) -> list[dict]:
    if not isinstance(block, dict):
        return []
    entries = block.get("entries")
    return (
        [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    )


def is_recorded(block: dict | None) -> bool:
    """Whether a manifest carries a byte-bound set at all.

    ``False`` means "rendered before this contract existed", which is a
    different thing from "recorded and wrong" and is treated differently at
    every boundary.
    """
    return bool(block_entries(block))


def verify_block(
    block: dict | None, *, roots: RootSet, skip_roots: frozenset[str] | set[str] = frozenset()
) -> list[InputIntegrity]:
    """Measure every recorded input, in recorded order.

    ``skip_roots`` exists for exactly one caller: taking back a remote render,
    where the runner's system font legitimately differs from the Pi's and
    comparing them would reject every remote result. Nothing else may use it —
    a boundary that skips a root is a boundary that does not check it.
    """
    results: list[InputIntegrity] = []
    for entry in block_entries(block):
        if str(entry.get("root") or "") in skip_roots:
            continue
        key = str(entry.get("key") or "")
        kind = str(entry.get("kind") or "")
        root = str(entry.get("root") or "")
        path = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")

        resolved = safe_resolve(roots, root, path)
        if resolved is None:
            results.append(InputIntegrity(key, kind, root, path, INPUT_UNSAFE, expected))
            continue
        if not resolved.is_file():
            results.append(InputIntegrity(key, kind, root, path, INPUT_MISSING, expected))
            continue
        if not expected:
            results.append(InputIntegrity(key, kind, root, path, INPUT_UNRECORDED))
            continue
        actual = hash_file(resolved)
        status = INPUT_OK if actual == expected else INPUT_MISMATCH
        results.append(InputIntegrity(key, kind, root, path, status, expected, actual))
    return results


def problems(results: list[InputIntegrity]) -> list[InputIntegrity]:
    return [item for item in results if item.status in _PROBLEM_STATUSES]


def require_inputs_intact(
    block: dict | None,
    *,
    roots: RootSet,
    context: str,
    skip_roots: frozenset[str] | set[str] = frozenset(),
) -> list[InputIntegrity]:
    """Verify at a trust boundary, or refuse to cross it.

    A manifest with no recorded set passes through: it predates this contract,
    there is nothing to compare, and refusing would strand every episode that
    was finished before today. The caller is told by :func:`is_recorded` and
    decides whether to say so out loud.
    """
    results = verify_block(block, roots=roots, skip_roots=skip_roots)
    broken = problems(results)
    if broken:
        raise RenderInputError(
            f"{context} refused: " + "; ".join(item.detail for item in broken),
            broken,
        )
    return results


def declared_mismatches(block: dict | None) -> list[str]:
    """Entries whose source claimed a digest that the measurement contradicted.

    Only the studio manifest claims one today. A disagreement means the studio
    package and its own inventory disagree, which is worth saying even when the
    bytes are otherwise consistent from render to publish.
    """
    notes: list[str] = []
    for entry in block_entries(block):
        declared = str(entry.get("declared_sha256") or "")
        measured = str(entry.get("sha256") or "")
        if declared and measured and declared != measured:
            notes.append(
                f"{entry.get('key')}: manifest declares {short_digest(declared)}, "
                f"file is {short_digest(measured)}"
            )
    return notes


def summarize(results: list[InputIntegrity]) -> dict:
    """A compact status for a dashboard or a CLI line."""
    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "total": len(results),
        "ok": counts.get(INPUT_OK, 0),
        "problems": [item.to_dict() for item in problems(results)],
        "counts": counts,
    }
