"""Cross-process pipeline run lock.

Only one pipeline run may execute at a time on a host. Because the pipeline
uses a single SQLite database, two concurrent runs (e.g. the every-10-minutes
autostart timer overlapping with a manual or web-triggered run) serialize on
the database write lock: a long-running stage such as ``translate`` holds an
open write transaction for several minutes, so the second run blocks and then
fails with ``sqlite3.OperationalError: database is locked`` once the busy
timeout expires.

This module provides an advisory ``flock``-based lock so batch entrypoints
(``run_latest`` / ``run_pending``) skip cleanly when another run is already
active, instead of piling up and colliding on the database.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "pipeline.lock"


class PipelineBusyError(RuntimeError):
    """Raised when another pipeline run already holds the lock."""


def _lock_path(settings) -> Path:
    """Return the lock file path, co-located with the SQLite database.

    Falls back to ``logs_dir`` (and finally ``data/``) when the database is not
    a local SQLite file.
    """
    url = getattr(settings, "database_url", "") or ""
    lock_dir: Path | None = None
    if url.startswith("sqlite:///"):
        db_path = url.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            lock_dir = Path(db_path).resolve().parent
    if lock_dir is None:
        lock_dir = Path(getattr(settings, "logs_dir", "data") or "data")
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / _LOCK_FILENAME


@contextlib.contextmanager
def pipeline_lock(settings, *, blocking: bool = False) -> Iterator[None]:
    """Acquire the exclusive pipeline run lock.

    Args:
        settings: Application settings (used to locate the lock file).
        blocking: If True, wait until the lock is available. If False
            (default), raise :class:`PipelineBusyError` immediately when
            another run holds the lock.

    Raises:
        PipelineBusyError: When ``blocking`` is False and the lock is held.
    """
    path = _lock_path(settings)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            raise PipelineBusyError("Another pipeline run is already active (lock held).") from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
