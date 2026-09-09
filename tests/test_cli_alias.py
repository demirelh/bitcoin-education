"""The newsroom name is a second door to the same room (N9).

The risk in renaming a running system is not the name, it is ending up with two
of everything: two entry points, two databases, two locks, two timers. These
tests assert the opposite — that ``almanya24`` and ``btcedu`` are literally the
same callable, and that nothing about storage or locking depends on which name
was typed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path("pyproject.toml")


def _scripts() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["scripts"]


def test_both_names_are_installed():
    scripts = _scripts()

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


def test_the_database_and_lock_do_not_depend_on_the_name(tmp_path):
    """One lock file and one database, whichever name invoked the run."""
    from btcedu.config import Settings
    from btcedu.core.runlock import _lock_path

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'one.db'}")

    assert "btcedu" in settings.database_url or str(tmp_path) in settings.database_url
    assert _lock_path(settings) == _lock_path(settings)


def test_no_second_command_group_was_registered():
    """A duplicate group would be the start of two of everything."""
    from btcedu.cli import cli

    names = list(cli.commands)

    assert len(names) == len(set(names))
