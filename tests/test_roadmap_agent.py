"""Tests for the roadmap coordinator launcher (btcedu.core.roadmap_agent)."""

import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


def test_launcher_blocks_sudo_but_not_push_or_deploy(tmp_path):
    """Pushing and deploying are allowed now; root access is not."""
    shell = _launched_shell(tmp_path)
    assert f"--deny-tool {shlex.quote('shell(sudo)')}" in shell
    assert shell.count("--deny-tool") == 1
    assert "git push" not in shell and "run.sh" not in shell


# ---------------------------------------------------------------------------
# _finish: documentation-only fast-forward into the production checkout
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(repo: Path, relative: str, text: str, message: str):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", message, cwd=repo)


@pytest.fixture
def repos(tmp_path):
    """A real checkout on ``main`` plus its ``roadmap`` worktree — never the real repository."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-b", "main", cwd=checkout)
    _git("config", "user.email", "test@example.invalid", cwd=checkout)
    _git("config", "user.name", "Test", cwd=checkout)
    _commit(checkout, "docs/ROADMAP.md", "# Fahrplan\n- [ ] A\n", "init")
    worktree = tmp_path / "checkout-roadmap"
    _git("worktree", "add", "-b", roadmap_agent.BRANCH, str(worktree), cwd=checkout)
    return checkout, worktree


def _finish_run(tmp_path, checkout: Path, exit_code: int = 0) -> dict:
    """Run the finish hook against the temporary repos and return the run entry."""
    state_dir = tmp_path / "chat"
    state = {
        "session_id": "sess-1",
        "queue": [],
        "runs": [{"id": "r1", "channel": "dashboard", "started_at": "x", "finished_at": None}],
    }
    roadmap_agent._save(state_dir, state)
    settings = SimpleNamespace(notify_whatsapp_enabled=False)
    with (
        patch.object(roadmap_agent, "REPOSITORY", checkout),
        patch("btcedu.config.Settings", return_value=settings),
        patch("btcedu.services.notify_service.send_notification") as notify,
    ):
        roadmap_agent._finish(state_dir, "r1", exit_code)
    notify.assert_not_called()
    return roadmap_agent._load(state_dir)["runs"][0]


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_finish_merges_a_documentation_only_branch(tmp_path, repos):
    """A ticked-off roadmap item must reach the checkout the dashboard reads."""
    checkout, worktree = repos
    _commit(worktree, "docs/ROADMAP.md", "# Fahrplan\n- [x] A\n", "Punkt erledigt")
    run = _finish_run(tmp_path, checkout)
    assert run["merged_to_main"] == 1
    assert _head(checkout) == _head(worktree)
    assert "- [x] A" in (checkout / "docs" / "ROADMAP.md").read_text(encoding="utf-8")


def test_finish_does_not_merge_code_changes(tmp_path, repos):
    """Code needs pip install, migration and restart — it may not slip in unnoticed."""
    checkout, worktree = repos
    _commit(worktree, "docs/ROADMAP.md", "# Fahrplan\n- [x] A\n", "Punkt erledigt")
    _commit(worktree, "btcedu/core/thing.py", "x = 1\n", "Code")
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout)
    assert "merged_to_main" not in run
    assert run["rollout_pending"]["commits"] == 2
    assert run["rollout_pending"]["reason"] == "code"
    assert _head(checkout) == before


def test_finish_does_not_merge_after_a_failed_run(tmp_path, repos):
    checkout, worktree = repos
    _commit(worktree, "docs/ROADMAP.md", "# Fahrplan\n- [x] A\n", "Punkt erledigt")
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout, exit_code=1)
    assert "merged_to_main" not in run and "rollout_pending" not in run
    assert _head(checkout) == before


def test_finish_does_not_merge_into_a_dirty_checkout(tmp_path, repos):
    """The operator may be editing in the production tree; never touch that."""
    checkout, worktree = repos
    _commit(worktree, "docs/ROADMAP.md", "# Fahrplan\n- [x] A\n", "Punkt erledigt")
    (checkout / "docs" / "ROADMAP.md").write_text("lokal bearbeitet\n", encoding="utf-8")
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout)
    assert "merged_to_main" not in run
    assert _head(checkout) == before


def test_finish_does_not_merge_without_a_fast_forward(tmp_path, repos):
    """main moved on its own: a merge commit is never created here."""
    checkout, worktree = repos
    _commit(worktree, "docs/ROADMAP.md", "# Fahrplan\n- [x] A\n", "Punkt erledigt")
    _commit(checkout, "docs/NOTES.md", "lokal\n", "Betreiber-Commit")
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout)
    assert "merged_to_main" not in run and "rollout_pending" not in run
    assert _head(checkout) == before


def test_finish_survives_a_broken_git(tmp_path, repos):
    """A git failure is logged; the run still counts as finished."""
    checkout, _ = repos
    with patch.object(roadmap_agent, "_git", side_effect=OSError("git weg")):
        run = _finish_run(tmp_path, checkout)
    assert run["finished_at"] is not None and run["exit_code"] == 0
    assert "merged_to_main" not in run


def test_finish_does_not_merge_code_renamed_into_docs(tmp_path, repos):
    """Rename detection would hide a module leaving the running checkout.

    ``git mv btcedu/live.py docs/live.py`` shows up as ``docs/live.py`` alone
    unless ``--no-renames`` is used, and the fast-forward would then delete the
    module from production without any deploy.
    """
    checkout, worktree = repos
    _commit(worktree, "btcedu/live.py", "x = 1\n", "Modul")
    _git("merge", "--ff-only", roadmap_agent.BRANCH, cwd=checkout)
    subprocess.run(
        ["git", "mv", "btcedu/live.py", "docs/live.py"], cwd=worktree, check=True,
        capture_output=True, text=True,
    )
    _git("commit", "-m", "verschoben", cwd=worktree)
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout)
    assert "merged_to_main" not in run
    assert run["rollout_pending"]["reason"] == "code"
    assert _head(checkout) == before
    assert (checkout / "btcedu" / "live.py").is_file()


def test_finish_does_not_merge_a_symlink_below_docs(tmp_path, repos):
    """A docs/ path may still point somewhere else — only plain files are merged."""
    checkout, worktree = repos
    (worktree / "docs").mkdir(exist_ok=True)
    (worktree / "docs" / "quelle").symlink_to("../btcedu")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "Verweis", cwd=worktree)
    before = _head(checkout)
    run = _finish_run(tmp_path, checkout)
    assert "merged_to_main" not in run
    assert run["rollout_pending"]["reason"] == "code"
    assert _head(checkout) == before


def test_plain_docs_change_rejects_anything_unparseable():
    assert roadmap_agent._is_plain_docs_change(":100644 100644 aaa bbb M\tdocs/ROADMAP.md")
    assert roadmap_agent._is_plain_docs_change(":000000 100644 aaa bbb A\tdocs/neu.md")
    assert not roadmap_agent._is_plain_docs_change(":100644 120000 aaa bbb T\tdocs/quelle")
    assert not roadmap_agent._is_plain_docs_change(":000000 160000 aaa bbb A\tdocs/modul")
    assert not roadmap_agent._is_plain_docs_change(":100644 100644 aaa bbb M\tbtcedu/x.py")
    assert not roadmap_agent._is_plain_docs_change('docs/"seltsam".md')
    assert not roadmap_agent._is_plain_docs_change("")
