"""A committed operational command must point at a committed program.

This exists because of a concrete failure: ``almanya24-dev-preview.service``
was committed with an ``ExecStart`` pointing into ``data/``, which the whole of
is git-ignored. The unit looked fine in review and worked on the one machine
where the file happened to exist, but a clean checkout of the branch could not
start the preview at all — the program simply was not there.

The rule this encodes is narrow on purpose: anything a committed unit or a
committed deploy script executes has to be in the git tree. Data paths are not
checked; runtime data belongs under ``data/`` and stays out of git.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"

#: Interpreters whose argument is a program file rather than data.
_INTERPRETERS = ("python", "python3", "bash", "sh")

#: Suffixes we treat as executable program files.
_PROGRAM_SUFFIXES = (".py", ".sh")


def _tracked_files() -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.splitlines())


def _repo_relative(token: str) -> str | None:
    """The repo-relative form of a token, or None if it is not a repo path."""
    token = token.strip().strip("'\"")
    if not token or not token.endswith(_PROGRAM_SUFFIXES):
        return None
    path = Path(token)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(REPO))
        except ValueError:
            # Outside the repository: not ours to guarantee.
            return None
    return str(path)


def _programs_in(text: str) -> set[str]:
    """Every program file an interpreter is asked to run in this text."""
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        tokens = line.replace("=", " ").split()
        for index, token in enumerate(tokens):
            name = Path(token.strip("'\"")).name
            if name not in _INTERPRETERS and not name.startswith("python"):
                continue
            for candidate in tokens[index + 1 :]:
                relative = _repo_relative(candidate)
                if relative is not None:
                    found.add(relative)
                    break
    return found


def _module_targets(text: str) -> set[str]:
    """Every ``python -m package.module`` an interpreter is asked to run.

    Without this the guard has a hole exactly the size of the mistake it
    exists to prevent: ``-m`` reaches a program just as well as a path does,
    and a unit invoking an untracked module fails in a clean checkout in
    precisely the same way.
    """
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        tokens = line.replace("=", " ").split()
        for index, token in enumerate(tokens):
            if token.strip("'\"") != "-m" or index + 1 >= len(tokens):
                continue
            dotted = tokens[index + 1].strip("'\"")
            if not re.fullmatch(r"[A-Za-z_][\w.]*", dotted):
                continue
            # Only modules that live in this repository. ``python -m venv``
            # is the standard library's problem, not a broken ExecStart.
            if not (REPO / dotted.split(".")[0]).is_dir():
                continue
            found.add(dotted.replace(".", "/") + ".py")
    return found


def _unit_files() -> list[Path]:
    return sorted(DEPLOY.glob("*.service")) + sorted(DEPLOY.glob("*.timer"))


def _script_files() -> list[Path]:
    return sorted(DEPLOY.glob("*.sh"))


@pytest.mark.parametrize("unit", _unit_files(), ids=lambda p: p.name)
def test_a_unit_only_executes_committed_programs(unit: Path):
    tracked = _tracked_files()
    text = unit.read_text()
    for program in _programs_in(text) | _module_targets(text):
        assert program in tracked, (
            f"{unit.name} executes {program}, which is not in the git tree. "
            "A clean checkout of this branch could not start it. Move the "
            "program into a tracked directory; runtime data stays under data/."
        )


@pytest.mark.parametrize("script", _script_files(), ids=lambda p: p.name)
def test_a_deploy_script_only_executes_committed_programs(script: Path):
    tracked = _tracked_files()
    text = script.read_text()
    for program in _programs_in(text) | _module_targets(text):
        assert program in tracked, (
            f"{script.name} executes {program}, which is not in the git tree."
        )


def test_the_preview_unit_and_the_activation_script_agree():
    # They are edited separately and drifted apart once already: the script
    # identifies the process it may stop by matching its command line, so a
    # unit that starts a different command turns the guard into a no-op that
    # silently leaves two preview processes fighting over the port.
    unit = (DEPLOY / "almanya24-dev-preview.service").read_text()
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    ).removeprefix("ExecStart=")
    command = re.sub(r"^\S*/bin/python3?\s+", "", exec_start).strip()
    assert command in (DEPLOY / "activate-almanya24-dev.sh").read_text(), (
        "The activation script no longer recognises the command the unit "
        f"starts ({command!r})."
    )


def test_runtime_data_is_not_committed():
    # The counterpart of the rule above: moving the programs out of data/ must
    # not tempt anyone into committing the database or the generated site.
    tracked = _tracked_files()
    offenders = [
        path
        for path in tracked
        if path.startswith("data/almanya24-preview/")
        or path.endswith((".sqlite", ".sqlite.bak"))
    ]
    assert offenders == [], f"Runtime data was committed: {offenders}"


# ---------------------------------------------------------------------------
# The daily unit's promises, which are only worth anything if they hold
# ---------------------------------------------------------------------------


def _daily_unit() -> str:
    return (DEPLOY / "almanya24-daily.service").read_text()


def test_paid_calls_are_locked_until_a_budget_is_granted():
    """The committed unit must not ship a spendable default.

    A budget that arrives by accident is the one activation mistake that
    costs money, and the file in version control is where it would arrive.
    """
    unit = _daily_unit()
    assert "Environment=ALMANYA24_DAILY_BUDGET_USD=0" in unit
    assert "Environment=ALMANYA24_DAILY_MAX_CALLS=0" in unit


def test_the_daily_unit_passes_every_limit_explicitly():
    """No limit may fall back to a default inside the program."""
    unit = _daily_unit()
    for flag in ("--budget-usd", "--max-calls", "--max-stories"):
        assert flag in unit, f"{flag} is not passed by the unit"


def test_only_the_daily_unit_turns_the_development_bypass_on():
    """The switch is off by default; exactly one unit is entitled to set it."""
    setters = [
        unit.name
        for unit in _unit_files()
        if "NEWSROOM_DEV_AUTO_RELEASE=true" in unit.read_text()
    ]
    assert setters == ["almanya24-daily.service"]


def test_production_data_is_mounted_read_only():
    """Transcripts are read from production; nothing may be written back."""
    unit = _daily_unit()
    assert "ReadOnlyPaths=/home/pi/AI-Startup-Lab/bitcoin-education" in unit
    assert "ReadWritePaths=" in unit
    write_targets = [
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.startswith("ReadWritePaths=")
    ]
    assert all("almanya24-newsroom-dev" in target for target in write_targets)
