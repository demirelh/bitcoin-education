"""The Fahrplan page must show the roadmap that actually applies.

The coordinator commits to the ``roadmap`` branch in its own worktree, while
the dashboard runs from the production checkout on ``main``. Until a human
merges, a ticked-off item would look open. These tests use temporary git
repositories only — never the real repository, and never a push or a deploy.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core import roadmap_agent
from btcedu.web import roadmap_routes
from btcedu.web.app import create_app

CHECKOUT_ROADMAP = """# Fahrplan

## Meilensteine

### M1 – Start
- [ ] Agent: Dashboard zeigt den Stand
"""

WORKTREE_ROADMAP = CHECKOUT_ROADMAP.replace("- [ ]", "- [x]")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(repo: Path, relative: str, text: str, message: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", message, cwd=repo)


@pytest.fixture
def repos(tmp_path):
    """A checkout on ``main`` whose ``roadmap`` worktree is one commit ahead."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-b", "main", cwd=checkout)
    _git("config", "user.email", "test@example.invalid", cwd=checkout)
    _git("config", "user.name", "Test", cwd=checkout)
    _commit(checkout, "docs/ROADMAP.md", CHECKOUT_ROADMAP, "init")
    worktree = tmp_path / "checkout-roadmap"
    _git("worktree", "add", "-b", roadmap_agent.BRANCH, str(worktree), cwd=checkout)
    _commit(worktree, "docs/ROADMAP.md", WORKTREE_ROADMAP, "Punkt erledigt")
    return checkout, worktree


@pytest.fixture
def client(tmp_path, repos):
    checkout, _ = repos
    settings = Settings(
        anthropic_api_key="test-key",
        openai_api_key="test-key",
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        web_auth_enabled=False,
        web_bind_host="127.0.0.1",
    )
    app = create_app(settings=settings)
    app.config["TESTING"] = True
    with (
        patch.object(roadmap_routes, "ROADMAP_PATH", checkout / "docs" / "ROADMAP.md"),
        patch.object(roadmap_routes, "REPO_ROOT", checkout),
    ):
        yield app.test_client()


def assert_source(client, source: str, *, pending: int, done: int) -> dict:
    response = client.get("/api/roadmap")
    assert response.status_code == 200
    data = response.get_json()
    assert data["source"] == source
    assert data["pending_commits"] == pending
    assert data["progress"]["done"] == done
    return data


def test_api_shows_the_committed_branch_state(client, repos):
    """The branch is ahead and not merged yet, so its committed plan is the one that applies."""
    assert_source(client, "branch", pending=1, done=1)


def test_api_uses_the_checkout_without_pending_commits(client, repos):
    """Merged already: nothing is ahead, so the checkout is authoritative."""
    checkout, _ = repos
    _git("merge", "--ff-only", roadmap_agent.BRANCH, cwd=checkout)
    assert_source(client, "checkout", pending=0, done=1)


def test_api_reports_a_pending_code_rollout_without_switching_source(client, repos):
    """A code-only commit leaves the plan unchanged but must still be announced."""
    _, worktree = repos
    _commit(worktree, "btcedu/neu.py", "x = 1\n", "Code")
    (worktree / "docs" / "ROADMAP.md").write_text(CHECKOUT_ROADMAP, encoding="utf-8")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "Haken zurueck", cwd=worktree)
    data = assert_source(client, "checkout", pending=3, done=0)
    assert data["progress"] == {"done": 0, "total": 1}


def test_api_ignores_a_branch_that_is_behind_main(client, repos):
    """The old bug mirrored: a stale branch must never replace a newer checkout."""
    checkout, _ = repos
    _git("merge", "--ff-only", roadmap_agent.BRANCH, cwd=checkout)
    _commit(checkout, "docs/ROADMAP.md", CHECKOUT_ROADMAP, "Betreiber ergaenzt")
    data = assert_source(client, "checkout", pending=0, done=0)
    assert data["progress"] == {"done": 0, "total": 1}


def test_api_ignores_uncommitted_work_in_the_worktree(client, repos):
    """A half-finished edit during a run is not a state the operator should see."""
    checkout, worktree = repos
    _git("merge", "--ff-only", roadmap_agent.BRANCH, cwd=checkout)
    (worktree / "docs" / "ROADMAP.md").write_text("# kaputt, halb fertig\n", encoding="utf-8")
    assert_source(client, "checkout", pending=0, done=1)


def test_api_never_fails_when_git_is_unavailable(client, repos):
    """No git binary, no 500: the checkout is shown and the backlog stays unknown."""
    with patch.object(roadmap_routes.subprocess, "run", side_effect=OSError("kein git")):
        response = client.get("/api/roadmap")
    assert response.status_code == 200
    data = response.get_json()
    assert data["source"] == "checkout"
    assert data["pending_commits"] == 0


def test_page_carries_the_hint_element(client):
    page = client.get("/roadmap")
    assert page.status_code == 200
    assert b'id="rm-source"' in page.data
