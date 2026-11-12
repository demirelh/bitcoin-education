"""Tests for the GitHub Actions client used by the render offload.

Every HTTP call is mocked; these tests never touch the network.
"""

import io
import zipfile
from datetime import UTC, datetime
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from btcedu.services.github_actions_service import (
    RUN_LOOKUP_TIMEOUT,
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


def _run_item(run_id, title, created="2026-01-01T12:00:00Z", status="in_progress"):
    return {
        "id": run_id,
        "display_title": title,
        "name": "Render Episode",
        "status": status,
        "conclusion": None,
        "created_at": created,
        "run_started_at": created,
        "html_url": f"https://example.invalid/{run_id}",
    }


def test_find_run_waits_far_beyond_two_minutes_for_a_late_run(client):
    """GitHub creates the run asynchronously; 120s was not enough under load."""
    late = {"workflow_runs": [_run_item(7, "render ep (job-xyz)")]}
    responses = [_response(payload={"workflow_runs": []}) for _ in range(16)]
    responses.append(_response(payload=late))
    delays = []
    with (
        patch("requests.request", side_effect=responses) as request,
        patch("time.sleep", side_effect=delays.append),
    ):
        run = client.find_run("render.yml", marker="job-xyz")

    assert run.id == 7
    assert request.call_count == 17
    # The old budget was 20 polls of a flat 6s: it gave up after 120s of waiting.
    assert sum(delays) > 120
    assert sum(delays) <= RUN_LOOKUP_TIMEOUT  # still inside the default budget
    assert delays == sorted(delays) and max(delays) <= 30  # backing off, but capped


def test_find_run_ignores_a_stale_run_with_the_same_marker(client):
    """A re-dispatch must bind to its own run, not to the previous attempt."""
    payload = {
        "workflow_runs": [
            _run_item(1, "render other (foreign-1)", created="2026-05-01T10:05:00Z"),
            _run_item(2, "render ep (job-xyz)", created="2026-05-01T09:00:00Z"),
            _run_item(3, "render other (foreign-2)", created="2026-05-01T10:06:00Z"),
            _run_item(4, "render ep (job-xyz)", created="2026-05-01T10:04:00Z"),
        ]
    }
    dispatched = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    with patch("requests.request", return_value=_response(payload=payload)):
        run = client.find_run(
            "render.yml", marker="job-xyz", ref="main", created_after=dispatched
        )
    assert run.id == 4


def test_find_run_tolerates_a_transient_listing_error(client):
    responses = [
        _response(status=503),
        _response(payload={"workflow_runs": [_run_item(9, "render ep (job-xyz)")]}),
    ]
    with patch("requests.request", side_effect=responses), patch("time.sleep"):
        run = client.find_run("render.yml", marker="job-xyz", ref="main")
    assert run.id == 9


def test_find_run_gives_up_with_a_diagnosable_message(client):
    """The failure must name ref, workflow file, criterion and waited time."""
    responses = [
        _response(payload={"workflow_runs": [_run_item(1, "render other (foreign)")]}),
        _response(status=404),  # workflow file lookup on the target ref
    ]
    with (
        patch("requests.request", side_effect=responses),
        patch("time.sleep"),
        pytest.raises(GitHubActionsError) as excinfo,
    ):
        client.find_run("render.yml", marker="nope", ref="roadmap", timeout=0)

    message = str(excinfo.value)
    assert "did not appear" in message
    assert "roadmap" in message and "render.yml" in message and "acme/widgets" in message
    assert "MISSING" in message  # workflow file absent on the target ref
    assert "run-name" in message
    assert "render other (foreign)" in message


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


# ---------------------------------------------------------------------------
# Losing the connection is not losing the render
# ---------------------------------------------------------------------------
"""The 2026-08-13 re-render died after 13 minutes on a single dropped TLS
handshake while polling. The runner carried on to completion; nobody collected
it. On a home connection that is an ordinary event, so the wait has to survive
it."""


def _dropped():
    import requests

    return requests.ConnectionError("Remote end closed connection without response")


def test_a_dropped_poll_does_not_end_the_wait(client):
    queued = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})
    done = _response(payload={"id": 5, "status": "completed", "conclusion": "success"})

    with patch("requests.request", side_effect=[queued, _dropped(), done]), patch("time.sleep"):
        run = client.wait_for_run(5, timeout=600, poll_interval=0)

    assert run.succeeded, "a poll that never arrived says nothing about the render"


def test_sustained_unreachability_is_still_a_failure(client):
    """Retrying forever would hide a genuinely broken connection."""
    queued = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})

    with (
        patch("requests.request", side_effect=[queued] + [_dropped()] * 20),
        patch("time.sleep"),
        pytest.raises(GitHubActionsError, match="Lost contact"),
    ):
        client.wait_for_run(5, timeout=600, poll_interval=0, unreachable_grace=0)


def test_a_deleted_run_is_not_retried(client):
    """A 404 is an answer, and waiting longer will not improve it."""
    queued = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})
    gone = _response(status=404)

    with (
        patch("requests.request", side_effect=[queued, gone]),
        patch("time.sleep"),
        pytest.raises(GitHubActionsError, match="404"),
    ):
        client.wait_for_run(5, timeout=600, poll_interval=0)


def test_a_server_error_is_worth_retrying(client):
    queued = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})
    outage = _response(status=502)
    done = _response(payload={"id": 5, "status": "completed", "conclusion": "success"})

    with patch("requests.request", side_effect=[queued, outage, done]), patch("time.sleep"):
        run = client.wait_for_run(5, timeout=600, poll_interval=0)

    assert run.succeeded


def test_the_timeout_still_applies_while_unreachable(client):
    """Grace must not be a way around the overall deadline."""
    queued = _response(payload={"id": 5, "status": "in_progress", "html_url": "u"})

    with (
        patch("requests.request", side_effect=[queued] + [_dropped()] * 20),
        patch("time.sleep"),
        patch("time.monotonic", side_effect=[0, 0, 10_000, 20_000, 30_000]),
        pytest.raises(GitHubActionsError, match="did not finish"),
    ):
        client.wait_for_run(5, timeout=1, poll_interval=0)


def test_an_error_without_an_answer_is_marked_transient():
    assert GitHubActionsError("boom").transient is True
    assert GitHubActionsError("boom", status_code=500).transient is True
    assert GitHubActionsError("boom", status_code=429).transient is True
    assert GitHubActionsError("boom", status_code=404).transient is False
    assert GitHubActionsError("boom", status_code=401).transient is False
