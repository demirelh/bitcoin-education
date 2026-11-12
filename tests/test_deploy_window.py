"""Deployment guards in run.sh.

The script is only *sourced* here, so a test can never start a deployment.
"""

import re
import subprocess
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parents[1] / "run.sh"


def _guard(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source {RUN_SH}; {snippet}"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("now", ["1930", "2000", "2330"])
def test_deploy_is_refused_during_the_broadcast(now):
    result = _guard(f"check_broadcast_window {now}")
    assert result.returncode != 0
    assert "19:30" in result.stderr


@pytest.mark.parametrize("now", ["0930", "1929", "2331", "0000"])
def test_deploy_is_allowed_outside_the_broadcast(now):
    """0930 also guards against the time being read as an octal number."""
    assert _guard(f"check_broadcast_window {now}").returncode == 0


def test_an_unreadable_time_refuses_instead_of_deploying():
    assert _guard('check_broadcast_window " 2000"').returncode != 0


def test_the_window_can_be_overridden_deliberately():
    assert _guard("ALLOW_BROADCAST_WINDOW=1 check_broadcast_window 2000").returncode == 0


def test_a_linked_worktree_must_not_deploy(tmp_path):
    """.git is a file in a worktree; deploying there would run the initial setup."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    result = _guard(f"PROJECT_ROOT={tmp_path} check_production_checkout")
    assert result.returncode != 0
    assert "worktree" in result.stderr


def test_the_production_checkout_may_deploy(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _guard(f"PROJECT_ROOT={tmp_path} check_production_checkout").returncode == 0


def test_service_restarts_never_wait_for_a_password():
    """sudo without -n blocks on the prompt and hangs an unattended deploy."""
    executed = re.findall(r"^\s*(?:if !\s*)?(sudo\s+\S+)", RUN_SH.read_text(), re.MULTILINE)
    assert executed  # the script does call sudo
    assert all(call.startswith("sudo -n") for call in executed), executed
