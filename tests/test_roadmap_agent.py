"""Tests for the roadmap coordinator launcher (btcedu.core.roadmap_agent)."""

import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from btcedu.core import roadmap_agent


def _settings(tmp_path):
    return SimpleNamespace(outputs_dir=str(tmp_path / "outputs"), copilot_cli_binary="copilot")


def _launched_shell(tmp_path):
    state = {"session_id": "sess-1", "runs": []}
    started = MagicMock(returncode=0, stderr="", stdout="")
    with (
        patch.object(roadmap_agent, "_tmux", return_value="/usr/bin/tmux"),
        patch.object(roadmap_agent.shutil, "which", return_value="/usr/bin/copilot"),
        patch.object(roadmap_agent, "ensure_worktree", return_value=tmp_path / "worktree"),
        patch.object(roadmap_agent.subprocess, "run", return_value=started) as run,
    ):
        roadmap_agent._launch(
            _settings(tmp_path),
            state,
            "Wo stehen wir?",
            "operator",
            autopilot=False,
            channel="dashboard",
        )
    return run.call_args[0][0][-1]


def test_launcher_allows_the_checkout_venv_interpreter(tmp_path):
    """.venv/bin/python links into /usr; --add-dir alone leaves it unreachable."""
    shell = _launched_shell(tmp_path)
    assert "--allow-all-paths" in shell


def test_launcher_still_blocks_push_deploy_and_sudo(tmp_path):
    shell = _launched_shell(tmp_path)
    for tool in roadmap_agent.DENIED_TOOLS:
        assert f"--deny-tool {shlex.quote(tool)}" in shell
