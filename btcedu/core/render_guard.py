"""WP-8C: nothing gets into a protected render that was never measured.

:mod:`btcedu.core.render_inputs` binds the files the pipeline *knows* about.
That inventory is built from the manifests, which is the right source — but it
is still an inventory of what the code believes it will use, and the belief and
the behaviour can drift. A stage that learns to draw a new watermark, a filter
that grows a ``movie=`` source, an operator who points a config value at a file
outside the episode: each of those puts bytes into the finished video that no
digest covers, and nothing would say so.

So this module asks the other question. Every ffmpeg invocation in the
protected path goes through one function, :func:`btcedu.services.ffmpeg_service.
_run_ffmpeg`, and that function is where the command can be read back and every
file it opens compared against the inventory. An input nobody recorded stops
the render.

Four things are allowed, and only four:

* a file in the measured inventory;
* a file under the render's own working directory — an intermediate this render
  produced a moment ago, whose provenance is the render itself;
* an exact path explicitly admitted after this render generated it outside the
  working directory, such as a timed weather video beside its static card;
* a file under a declared system root, which is the documented machine-local
  class (fonts, codec data) that cannot be shared across machines and is
  recorded rather than digested.

Everything else is refused. The guard is armed explicitly and only around the
protected render, so the smoke test, the weather stage, TTS levelling and every
other ffmpeg user are untouched by it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories whose contents are machine-local by nature. A font or a codec
# data file cannot be part of a cross-machine digest (see the ROOT_SYSTEM note
# in render_inputs), so it is classified here instead of being refused.
DEFAULT_SYSTEM_ROOTS: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts-droid-fallback",
    "/usr/lib",
    "/usr/share/ffmpeg",
    "/etc/fonts",
)


class UnknownRenderInputError(RuntimeError):
    """ffmpeg was asked to open a file that no one measured."""


@dataclass
class _Armed:
    """The inventory a protected render is allowed to draw on."""

    inventory: set[str] = field(default_factory=set)
    work_roots: list[str] = field(default_factory=list)
    system_roots: list[str] = field(default_factory=list)
    seen_system: set[str] = field(default_factory=set)
    episode_id: str = ""


_state = threading.local()


def _current() -> _Armed | None:
    return getattr(_state, "armed", None)


def is_armed() -> bool:
    return _current() is not None


def _resolve(value: str | Path) -> str:
    try:
        return str(Path(value).resolve())
    except (OSError, ValueError):  # pragma: no cover - a path the OS rejects
        return str(value)


def _under(path: str, roots: Iterable[str]) -> bool:
    candidate = Path(path)
    for root in roots:
        try:
            candidate.relative_to(Path(root))
        except ValueError:
            continue
        return True
    return False


@contextmanager
def arm(
    *,
    inventory: Iterable[str | Path],
    work_roots: Iterable[str | Path],
    system_roots: Iterable[str | Path] | None = None,
    episode_id: str = "",
) -> Iterator[_Armed]:
    """Guard every ffmpeg call on this thread until the block exits.

    Nested arming is not supported and not needed: a render is one operation.
    Re-arming inside an armed block replaces the inventory and restores the
    outer one on exit, which keeps the contract simple if a caller ever does it
    by accident.
    """
    previous = _current()
    armed = _Armed(
        inventory={_resolve(item) for item in inventory},
        work_roots=[_resolve(item) for item in work_roots],
        system_roots=[_resolve(item) for item in (system_roots or DEFAULT_SYSTEM_ROOTS)],
        episode_id=episode_id,
    )
    _state.armed = armed
    try:
        yield armed
    finally:
        if previous is None:
            _state.armed = None
        else:
            _state.armed = previous


def arm_now(
    *,
    inventory: Iterable[str | Path],
    work_roots: Iterable[str | Path],
    system_roots: Iterable[str | Path] | None = None,
    episode_id: str = "",
) -> None:
    """Arm the guard without a ``with`` block.

    :func:`arm` is the shape a caller wants; this exists for the one caller
    whose protected region is a several-hundred-line ``try`` body, where
    wrapping would mean reindenting the render rather than guarding it. Pair it
    with :func:`disarm_now` in a ``finally``.
    """
    _state.armed = _Armed(
        inventory={_resolve(item) for item in inventory},
        work_roots=[_resolve(item) for item in work_roots],
        system_roots=[_resolve(item) for item in (system_roots or DEFAULT_SYSTEM_ROOTS)],
        episode_id=episode_id,
    )


def disarm_now() -> None:
    _state.armed = None


def admit_generated_input(path: str | Path) -> None:
    """Admit one file that the active render just generated.

    This is intentionally exact-path admission rather than another work root:
    reviewed inputs beside the generated file must remain inventory-bound.
    """
    armed = _current()
    if armed is not None:
        armed.inventory.add(_resolve(path))


@contextmanager
def disarmed() -> Iterator[None]:
    """Run something inside a protected render without the guard.

    For measurement and probing — ffprobe on a file the render just wrote, a
    duration read — where the command opens an output rather than an input.
    """
    previous = _current()
    _state.armed = None
    try:
        yield
    finally:
        _state.armed = previous


def _unescape(value: str) -> str:
    # ffmpeg filter syntax escapes the colon and the backslash inside options.
    return value.replace("\\:", ":").replace("\\\\", "\\").strip("'\"")


def _concat_entries(list_file: Path) -> list[str]:
    """The files a concat demuxer list points at, resolved against the list."""
    entries: list[str] = []
    try:
        for line in list_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("file "):
                continue
            target = _unescape(line[len("file ") :].strip())
            path = Path(target)
            if not path.is_absolute():
                path = list_file.parent / path
            entries.append(str(path))
    except OSError as exc:  # pragma: no cover - unreadable list is ffmpeg's problem
        logger.debug("Could not read a concat list while guarding a render: %s", exc)
    return entries


def command_file_inputs(cmd: list[str]) -> list[str]:
    """Every existing file the command will open for reading.

    Existence on disk is the test rather than a syntax rule: ``-i`` also takes
    lavfi descriptors, device names and ``concat:`` pseudo-paths, and the only
    reliable way to tell a file from one of those is to ask the filesystem.
    A file that does not exist is ffmpeg's error to report, not this guard's.
    """
    found: list[str] = []

    def _consider(value: str) -> None:
        if not value:
            return
        try:
            path = Path(value)
            if not path.is_file():
                return
        except (OSError, ValueError):
            return
        found.append(value)
        if path.suffix.lower() in {".txt", ".ffconcat"}:
            found.extend(_concat_entries(path))

    index = 0
    while index < len(cmd):
        argument = str(cmd[index])
        if argument == "-i" and index + 1 < len(cmd):
            _consider(str(cmd[index + 1]))
            index += 2
            continue
        # Filters can name their own sources and their own typeface.
        for token in ("fontfile=", "movie=", "amovie=", "textfile="):
            position = argument.find(token)
            while position != -1:
                tail = argument[position + len(token) :]
                # The value ends at an unescaped separator.
                value = ""
                skip = False
                for character in tail:
                    if skip:
                        value += character
                        skip = False
                        continue
                    if character == "\\":
                        value += character
                        skip = True
                        continue
                    if character in ":,[]'\"":
                        break
                    value += character
                _consider(_unescape(value))
                position = argument.find(token, position + 1)
        index += 1
    return found


def inspect(cmd: list[str]) -> None:
    """Refuse a command that reads a file the inventory does not know.

    A no-op unless a protected render armed the guard on this thread.
    """
    armed = _current()
    if armed is None:
        return
    unknown: list[str] = []
    for raw in command_file_inputs(cmd):
        resolved = _resolve(raw)
        if resolved in armed.inventory:
            continue
        if _under(resolved, armed.work_roots):
            continue
        if _under(resolved, armed.system_roots):
            armed.seen_system.add(resolved)
            continue
        unknown.append(resolved)
    if unknown:
        raise UnknownRenderInputError(
            "Refusing to render "
            + (f"{armed.episode_id}: " if armed.episode_id else ": ")
            + "ffmpeg was asked to read "
            + ", ".join(sorted(set(unknown))[:5])
            + ", which is not in the measured input set, not an intermediate of "
            "this render and not a declared system input"
        )


def system_inputs_seen() -> list[str]:
    """The machine-local files the guarded render actually opened."""
    armed = _current()
    return sorted(armed.seen_system) if armed else []
