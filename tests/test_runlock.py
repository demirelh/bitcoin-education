"""Tests for the cross-process pipeline run lock."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from btcedu.core.runlock import PipelineBusyError, _lock_path, pipeline_lock


def _settings(tmp_path):
    return SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'btcedu.db'}",
        logs_dir=str(tmp_path / "logs"),
    )


def test_lock_path_next_to_sqlite_db(tmp_path):
    settings = _settings(tmp_path)
    path = _lock_path(settings)
    assert path.parent == (tmp_path).resolve()
    assert path.name == "pipeline.lock"


def test_lock_path_fallback_to_logs_dir(tmp_path):
    settings = SimpleNamespace(
        database_url="postgresql://localhost/db",
        logs_dir=str(tmp_path / "logs"),
    )
    path = _lock_path(settings)
    assert path.parent == (tmp_path / "logs")
    assert path.name == "pipeline.lock"


def test_lock_acquire_and_release(tmp_path):
    settings = _settings(tmp_path)
    with pipeline_lock(settings):
        pass
    # Re-acquirable after release
    with pipeline_lock(settings):
        pass


def test_second_acquire_in_child_raises(tmp_path):
    """A second process cannot acquire the lock while the parent holds it."""
    settings = _settings(tmp_path)
    with pipeline_lock(settings):
        pid = os.fork()
        if pid == 0:
            # Child process: attempt non-blocking acquire, should fail.
            code = 0
            try:
                with pipeline_lock(settings):
                    code = 2  # unexpectedly acquired
            except PipelineBusyError:
                code = 0
            except Exception:
                code = 3
            os._exit(code)
        else:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0


def test_blocking_false_is_default(tmp_path):
    """When held by another process, non-blocking raises promptly."""
    settings = _settings(tmp_path)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        with pipeline_lock(settings):
            os.write(w, b"x")  # signal held
            import time

            time.sleep(1.0)
        os._exit(0)
    else:
        os.close(w)
        assert os.read(r, 1) == b"x"  # wait until child holds the lock
        with pytest.raises(PipelineBusyError):
            with pipeline_lock(settings):
                pass
        os.close(r)
        os.waitpid(pid, 0)
