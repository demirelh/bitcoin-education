"""The newsroom name is a second door to the same room (N9).

The risk in renaming a running system is not the name, it is ending up with two
of everything: two entry points, two databases, two locks, two timers. These
tests assert the opposite — that ``almanya24`` and ``btcedu`` are literally the
same callable, and that nothing about storage or locking depends on which name
was typed.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

from btcedu.core.runlock import _lock_path as real_lock_path

PYPROJECT = Path("pyproject.toml")


def _scripts() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_both_names_are_installed():
    scripts = {
        row.name: row.value
        for row in distribution("bitcoin-education").entry_points
        if row.group == "console_scripts"
    }

    assert "btcedu" in scripts
    assert "almanya24" in scripts


def test_both_names_point_at_the_same_entry_point():
    """Not an equivalent command — the same one."""
    scripts = _scripts()

    assert scripts["almanya24"] == scripts["btcedu"]


def test_the_entry_point_resolves_to_one_click_group():
    from btcedu.cli import cli

    assert _scripts()["almanya24"] == "btcedu.cli:cli"
    assert cli.name == "cli"


def test_the_database_and_lock_do_not_depend_on_the_name(tmp_path, monkeypatch):
    """One lock file and one database, whichever name invoked the run."""
    from btcedu.config import Settings
    from btcedu.core.runlock import pipeline_lock

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'one.db'}")

    monkeypatch.setattr("btcedu.core.runlock._lock_path", real_lock_path)
    assert real_lock_path(settings) == tmp_path / "pipeline.lock"
    child = """
import sys
from importlib.metadata import distribution
from btcedu.config import Settings
from btcedu.cli import cli
from btcedu.core.runlock import pipeline_lock, PipelineBusyError
entry = next(e for e in distribution("bitcoin-education").entry_points if e.name == sys.argv[1])
assert entry.load() is cli
settings = Settings(_env_file=None, database_url=sys.argv[2])
try:
    with pipeline_lock(settings):
        raise AssertionError("Second entrypoint acquired an already held database lock")
except PipelineBusyError:
    print("blocked")
"""
    with pipeline_lock(settings):
        for name in ("almanya24", "btcedu"):
            result = subprocess.run(
                [sys.executable, "-c", child, name, settings.database_url],
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
            )
            assert result.stdout.strip() == "blocked"


def test_no_second_command_group_was_registered():
    """A duplicate group would be the start of two of everything."""
    from btcedu.cli import cli

    names = list(cli.commands)

    assert len(names) == len(set(names))
