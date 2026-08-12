"""Tests for the GitHub Actions client used by the render offload.

Every HTTP call is mocked; these tests never touch the network.
"""

import io
import zipfile
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from btcedu.services.github_actions_service import (
    GitHubActionsClient,
    GitHubActionsError,
    WorkflowRun,
)


def _response(status=200, payload=None, content=b""):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload if payload is not None else {}
    response.content = content
    response.text = "error body"
    response.iter_content.side_effect = lambda chunk_size=1: (
        content[i : i + chunk_size] for i in range(0, len(content), chunk_size)
    )
    return response


@pytest.fixture
def client():
    return GitHubActionsClient("ghp-secret-token", "acme/widgets")


def test_client_requires_token():
    with pytest.raises(GitHubActionsError, match="No GitHub token"):
        GitHubActionsClient("", "acme/widgets")


@pytest.mark.parametrize("repo", ["", "widgets", "acme widgets"])
def test_client_validates_repo_slug(repo):
    with pytest.raises(GitHubActionsError, match="Invalid repository"):
        GitHubActionsClient("ghp-token", repo)


def test_errors_never_contain_the_token(client):
    """Error strings end up in logs and the dashboard."""
    with patch("requests.request", return_value=_response(status=404)):
        with pytest.raises(GitHubActionsError) as excinfo:
            client.get_run(42)
    assert "ghp-secret-token" not in str(excinfo.value)


def test_token_is_sent_as_bearer(client):
    with patch("requests.request", return_value=_response(payload={"id": 1})) as request:
        client.get_run(1)
    headers = request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer ghp-secret-token"


def test_workflow_run_states():
    assert not WorkflowRun(1, "in_progress", None, "").finished
    assert WorkflowRun(1, "completed", "success", "").succeeded
    assert not WorkflowRun(1, "completed", "failure", "").succeeded


def test_find_run_matches_marker_in_run_name(client):
    payload = {
        "workflow_runs": [
            {"id": 1, "display_title": "render other (abc)", "status": "completed"},
            {
                "id": 2,
                "display_title": "render ep (job-xyz)",
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://example.invalid/2",
            },
        ]
    }
    with patch("requests.request", return_value=_response(payload=payload)):
        run = client.find_run("render.yml", marker="job-xyz")
    assert run.id == 2


def test_find_run_gives_up_with_a_useful_message(client):
    with (
        patch("requests.request", return_value=_response(payload={"workflow_runs": []})),
        patch("time.sleep"),
        pytest.raises(GitHubActionsError, match="did not appear"),
    ):
        client.find_run("render.yml", marker="nope", attempts=2, delay=0)


def test_wait_for_run_polls_until_finished(client):
    states = [
        _response(payload={"id": 5, "status": "queued"}),
        _response(payload={"id": 5, "status": "in_progress"}),
        _response(payload={"id": 5, "status": "completed", "conclusion": "success"}),
    ]
    with patch("requests.request", side_effect=states), patch("time.sleep"):
        run = client.wait_for_run(5, timeout=60, poll_interval=0)
    assert run.succeeded


def test_wait_for_run_times_out(client):
    running = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})
    with (
        patch("requests.request", return_value=running),
        patch("time.sleep"),
        patch("time.monotonic", side_effect=[0, 10_000, 20_000]),
        pytest.raises(GitHubActionsError, match="did not finish"),
    ):
        client.wait_for_run(5, timeout=1, poll_interval=0)


def test_download_artifact_unpacks_and_then_deletes(client, tmp_path):
    """The artifact must be removed again or it eats the storage quota."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("render-result.tar.gz", b"payload")

    listing = _response(
        payload={
            "artifacts": [
                {
                    "id": 77,
                    "name": "render-result",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/dl",
                }
            ]
        }
    )
    download = _response(content=buffer.getvalue())
    delete = _response(status=204)

    with patch("requests.request", side_effect=[listing, download, delete]) as request:
        client.download_artifact(1, "render-result", tmp_path / "out")

    assert (tmp_path / "out" / "render-result.tar.gz").read_bytes() == b"payload"
    assert request.call_args_list[-1].args[0] == "DELETE"
    # The downloaded zip is a working file, not an output.
    assert not (tmp_path / "out" / "render-result.zip").exists()


def test_download_artifact_streams_instead_of_buffering_in_memory(client, tmp_path):
    """A rendered episode is ~800 MB; two heap copies of it fell over on the Pi.

    ``response.content`` materialises the whole body, and the BytesIO wrapper
    around it made a second copy. Both are gone: the body is written to disk in
    chunks and zipfile reads it lazily from there.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("render-result.tar.gz", b"payload")

    listing = _response(
        payload={
            "artifacts": [
                {
                    "id": 77,
                    "name": "render-result",
                    "expired": False,
                    "archive_download_url": "https://example.invalid/dl",
                }
            ]
        }
    )
    download = _response(content=buffer.getvalue())
    type(download).content = PropertyMock(
        side_effect=AssertionError("the response body must not be materialised in memory")
    )

    with patch("requests.request", side_effect=[listing, download, _response(status=204)]) as req:
        client.download_artifact(1, "render-result", tmp_path / "out")

    assert (tmp_path / "out" / "render-result.tar.gz").read_bytes() == b"payload"
    assert req.call_args_list[1].kwargs["stream"] is True


def test_download_artifact_reports_missing_artifact(client, tmp_path):
    listing = _response(payload={"artifacts": [{"id": 1, "name": "something-else"}]})
    with (
        patch("requests.request", return_value=listing),
        pytest.raises(GitHubActionsError, match="no artifact named"),
    ):
        client.download_artifact(1, "render-result", tmp_path)


def test_delete_release_never_raises(client):
    """Cleanup runs in a finally block; it must not mask the real error."""
    with patch("requests.request", return_value=_response(status=500)):
        client.delete_release(1)  # must not raise
