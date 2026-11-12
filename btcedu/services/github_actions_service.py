"""Thin GitHub REST client for offloading the render stage to Actions.

Only the handful of endpoints the render offload needs are implemented:
a draft release is used to hand the input assets to the runner (artifacts
cannot be uploaded before a run exists), the workflow is dispatched, its run is
polled, and the resulting artifact is downloaded again.

The token is read from settings and never logged.
"""

import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
UPLOAD_ROOT = "https://uploads.github.com"

# Runs that have not finished yet report one of these.
_ACTIVE_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}

# How long a dispatched run may take to show up in the API before we give up.
# GitHub creates the run asynchronously; under load (or while a concurrency
# group is busy) it has repeatedly taken well over the two minutes this used to
# allow, which turned a healthy offload into a 25-minute local render. The
# budget is capped well below the remote render itself: a dispatch that really
# failed must still leave the evening enough time for the local fallback.
RUN_LOOKUP_TIMEOUT = 420
# Clock skew / "created_at" rounding tolerance when rejecting older runs.
_CREATED_AFTER_SLACK = 300.0
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class GitHubActionsError(RuntimeError):
    """Any failure while talking to the GitHub Actions API.

    ``status_code`` is the HTTP status when the API answered and ``None`` when
    the request never got an answer at all. Callers need the difference: a 404
    means the run is gone, a dropped connection means only that we did not
    hear back this time.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def transient(self) -> bool:
        """Is retrying the same request worth anything?"""
        if self.status_code is None:
            return True  # no answer: transport, DNS, TLS, or a dropped socket
        return self.status_code in (408, 429) or self.status_code >= 500


@dataclass
class WorkflowRun:
    """The subset of a workflow run this module cares about."""

    id: int
    status: str
    conclusion: str | None
    html_url: str

    @property
    def finished(self) -> bool:
        return self.status not in _ACTIVE_STATES

    @property
    def succeeded(self) -> bool:
        return self.finished and self.conclusion == "success"


class GitHubActionsClient:
    """Minimal client for the render offload."""

    def __init__(self, token: str, repo: str, request_timeout: int = 60) -> None:
        if not token:
            raise GitHubActionsError("No GitHub token configured (GITHUB_TOKEN)")
        if not repo or "/" not in repo:
            raise GitHubActionsError(f"Invalid repository {repo!r}, expected 'owner/repo'")
        self._token = token
        self.repo = repo
        self.request_timeout = request_timeout

    # -- plumbing ---------------------------------------------------------

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if url.startswith("/"):
            url = f"{API_ROOT}{url}"
        kwargs.setdefault("timeout", self.request_timeout)
        headers = self._headers(kwargs.pop("accept", "application/vnd.github+json"))
        headers.update(kwargs.pop("headers", {}))
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise GitHubActionsError(f"{method} {url} failed: {exc}") from exc
        if response.status_code >= 400:
            # Never echo the request headers -- they carry the token.
            raise GitHubActionsError(
                f"{method} {url} returned {response.status_code}: {response.text[:400]}",
                status_code=response.status_code,
            )
        return response

    # -- releases used as an input bucket ---------------------------------

    def create_draft_release(self, tag: str, body: str = "") -> dict:
        """Create a draft (unpublished) release to carry the job assets."""
        payload = {
            "tag_name": tag,
            "name": tag,
            "body": body or "Temporary render job payload. Safe to delete.",
            "draft": True,
            "prerelease": True,
        }
        return self._request("POST", f"/repos/{self.repo}/releases", json=payload).json()

    def upload_release_asset(self, release_id: int, path: Path, name: str = "") -> dict:
        """Attach a file to a release."""
        asset_name = name or path.name
        url = f"{UPLOAD_ROOT}/repos/{self.repo}/releases/{release_id}/assets?name={asset_name}"
        with path.open("rb") as handle:
            return self._request(
                "POST",
                url,
                data=handle,
                headers={"Content-Type": "application/octet-stream"},
                timeout=max(self.request_timeout, 900),
            ).json()

    def delete_release(self, release_id: int) -> None:
        """Remove the temporary release; failures are logged, not raised."""
        try:
            self._request("DELETE", f"/repos/{self.repo}/releases/{release_id}")
        except GitHubActionsError as exc:
            logger.warning("Could not delete render release %s: %s", release_id, exc)

    # -- workflow ---------------------------------------------------------

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict) -> None:
        """Trigger a workflow_dispatch run. GitHub returns no run id here."""
        self._request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
            json={"ref": ref, "inputs": inputs},
        )

    def workflow_file_on_ref(self, workflow: str, ref: str) -> bool | None:
        """Is ``.github/workflows/<workflow>`` present on ``ref``?

        Purely diagnostic: ``workflow_dispatch`` happily accepts a ref whose
        tree does not carry the workflow file, and the run then never appears.
        Returns ``None`` when the question could not be answered.
        """
        if not ref:
            return None
        try:
            self._request(
                "GET",
                f"/repos/{self.repo}/contents/.github/workflows/{workflow}",
                params={"ref": ref},
            )
            return True
        except GitHubActionsError as exc:
            if exc.status_code == 404:
                return False
            return None

    def _list_dispatch_runs(self, workflow: str) -> list[dict]:
        """Recent ``workflow_dispatch`` runs of one workflow, newest first.

        Deliberately not filtered by branch: ``workflow_dispatch`` may be
        answered on a ref the ``branch`` filter does not match (tags, or a ref
        GitHub reports under a different name), and an over-narrow query is
        indistinguishable from "the run does not exist yet".
        """
        payload = self._request(
            "GET",
            f"/repos/{self.repo}/actions/workflows/{workflow}/runs",
            params={"event": "workflow_dispatch", "per_page": 100},
        ).json()
        return payload.get("workflow_runs", []) or []

    @staticmethod
    def _run_title(item: dict) -> str:
        return f"{item.get('display_title', '') or ''} {item.get('name', '') or ''}"

    @staticmethod
    def _run_created_at(item: dict) -> datetime | None:
        raw = item.get("run_started_at") or item.get("created_at") or ""
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def find_run(
        self,
        workflow: str,
        marker: str,
        ref: str = "",
        timeout: int = RUN_LOOKUP_TIMEOUT,
        initial_delay: float = 3.0,
        max_delay: float = 30.0,
        created_after: datetime | None = None,
    ) -> WorkflowRun:
        """Locate the dispatched run by the marker carried in its run-name.

        ``workflow_dispatch`` does not return the run id, so the workflow puts a
        unique job id into ``run-name`` and we search for it. The marker is the
        only matching criterion -- never the branch or a timestamp -- so a
        foreign run dispatched at the same moment can never be mistaken for
        ours. ``created_after`` is an extra guard (with slack for clock skew)
        against matching a *previous* run that carried the same marker.

        Polling backs off from ``initial_delay`` to ``max_delay`` until
        ``timeout`` seconds have elapsed; a run that is merely slow to appear is
        not a failure.
        """
        deadline = time.monotonic() + max(timeout, 0)
        started = time.monotonic()
        delay = max(initial_delay, 0.0)
        attempts = 0
        stale_ids: set[object] = set()
        recent_titles: list[str] = []
        while True:
            attempts += 1
            try:
                items = self._list_dispatch_runs(workflow)
            except GitHubActionsError as exc:
                if not exc.transient or time.monotonic() + delay >= deadline:
                    raise
                logger.warning("Could not list runs of %s while searching: %s", workflow, exc)
                items = []
            recent_titles = [
                (item.get("display_title") or item.get("name") or "").strip()
                for item in items[:5]
            ]
            matches = [item for item in items if marker in self._run_title(item)]
            if created_after is not None:
                cutoff = created_after.timestamp() - _CREATED_AFTER_SLACK
                fresh = []
                for item in matches:
                    created = self._run_created_at(item)
                    if created is None or created.timestamp() >= cutoff:
                        fresh.append(item)
                    else:
                        if item.get("id") not in stale_ids:
                            logger.warning(
                                "Ignoring run %s for marker %s: created %s, before dispatch",
                                item.get("id"),
                                marker,
                                created,
                            )
                        stale_ids.add(item.get("id"))
                matches = fresh
            if matches:
                # Newest first is the documented order; a re-run of the same
                # job id must not resolve to the stale attempt.
                item = max(
                    matches,
                    key=lambda i: (self._run_created_at(i) or EPOCH, i.get("id", 0)),
                )
                if len(matches) > 1:
                    logger.warning(
                        "Marker %s matched %d runs; using the newest (%s)",
                        marker,
                        len(matches),
                        item.get("id"),
                    )
                return WorkflowRun(
                    id=item["id"],
                    status=item.get("status", ""),
                    conclusion=item.get("conclusion"),
                    html_url=item.get("html_url", ""),
                )
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
            delay = min(max_delay, delay * 1.5) if delay else max(initial_delay, 1.0)

        waited = time.monotonic() - started
        on_ref = self.workflow_file_on_ref(workflow, ref)
        stale_hint = (
            f"{len(stale_ids)} run(s) carried the marker but were created before the "
            "dispatch (check the clock of this machine); "
            if stale_ids
            else ""
        )
        ref_hint = {
            True: f".github/workflows/{workflow} exists on ref {ref!r}",
            False: (
                f".github/workflows/{workflow} is MISSING on ref {ref!r} -- "
                "the dispatch can never produce a run there"
            ),
            None: f"could not verify .github/workflows/{workflow} on ref {ref!r}",
        }[on_ref]
        raise GitHubActionsError(
            f"Dispatched run for marker {marker!r} did not appear within {waited:.0f}s "
            f"({attempts} polls). repo={self.repo} workflow={workflow} ref={ref or '?'}; "
            f"searched event=workflow_dispatch runs whose run-name contains the marker; "
            f"{stale_hint}{ref_hint}; most recent run names seen: {recent_titles or 'none'}. "
            "If the run exists on github.com, its run-name does not carry the job id."
        )

    def get_run(self, run_id: int) -> WorkflowRun:
        item = self._request("GET", f"/repos/{self.repo}/actions/runs/{run_id}").json()
        return WorkflowRun(
            id=item["id"],
            status=item.get("status", ""),
            conclusion=item.get("conclusion"),
            html_url=item.get("html_url", ""),
        )

    def wait_for_run(
        self,
        run_id: int,
        timeout: int,
        poll_interval: int,
        on_poll=None,
        unreachable_grace: int = 600,
    ) -> WorkflowRun:
        """Block until the run finishes or ``timeout`` seconds elapse.

        A poll that does not come back is not a failed render. The runner keeps
        working regardless of whether this machine can reach GitHub, and on a
        home connection a dropped TLS handshake is routine. Giving up on the
        first one threw away a quarter hour of finished work and left the run
        going with nobody to collect it, so unreachability is tolerated for
        ``unreachable_grace`` seconds before it counts as a real failure.
        """
        deadline = time.monotonic() + timeout
        run = self.get_run(run_id)
        last_contact = time.monotonic()
        while not run.finished:
            if time.monotonic() > deadline:
                raise GitHubActionsError(
                    f"Render run {run_id} did not finish within {timeout}s ({run.html_url})"
                )
            if on_poll is not None:
                on_poll(run)
            time.sleep(poll_interval)
            try:
                run = self.get_run(run_id)
            except GitHubActionsError as exc:
                if not exc.transient:
                    raise
                unreachable = time.monotonic() - last_contact
                if unreachable > unreachable_grace:
                    raise GitHubActionsError(
                        f"Lost contact with GitHub for {unreachable:.0f}s while waiting for "
                        f"run {run_id} ({run.html_url}): {exc}"
                    ) from exc
                logger.warning(
                    "Could not reach GitHub while waiting for run %s (%s); "
                    "retrying for up to %.0fs more",
                    run_id,
                    exc,
                    unreachable_grace - unreachable,
                )
                continue
            last_contact = time.monotonic()
        return run

    def cancel_run(self, run_id: int) -> None:
        try:
            self._request("POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel")
        except GitHubActionsError as exc:
            logger.warning("Could not cancel run %s: %s", run_id, exc)

    # -- artifacts --------------------------------------------------------

    def download_artifact(self, run_id: int, name: str, dest_dir: Path) -> Path:
        """Download and unpack a run artifact, returning the extraction dir.

        The artifact is deleted afterwards: a rendered episode is ~480 MB and
        would otherwise eat the account's artifact storage quota.
        """
        listing = self._request("GET", f"/repos/{self.repo}/actions/runs/{run_id}/artifacts").json()
        match = next(
            (a for a in listing.get("artifacts", []) if a.get("name") == name),
            None,
        )
        if match is None:
            available = [a.get("name") for a in listing.get("artifacts", [])]
            raise GitHubActionsError(
                f"Run {run_id} produced no artifact named {name!r} (found: {available})"
            )
        if match.get("expired"):
            raise GitHubActionsError(f"Artifact {name!r} of run {run_id} has expired")

        response = self._request(
            "GET",
            match["archive_download_url"],
            timeout=max(self.request_timeout, 1800),
            stream=True,
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Streamed to disk rather than held in memory: a rendered episode is
        # ~800 MB, and `response.content` plus the BytesIO copy around it used
        # to put roughly twice that on the heap of a Raspberry Pi with 7.6 GB
        # of RAM. zipfile reads the file lazily, so nothing is buffered whole.
        archive_path = dest_dir / f"{name}.zip"
        try:
            with archive_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    handle.write(chunk)
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(dest_dir)
        finally:
            archive_path.unlink(missing_ok=True)
        self.delete_artifact(match["id"])
        return dest_dir

    def delete_artifact(self, artifact_id: int) -> None:
        """Free the storage quota again; failures are logged, not raised."""
        try:
            self._request("DELETE", f"/repos/{self.repo}/actions/artifacts/{artifact_id}")
        except GitHubActionsError as exc:
            logger.warning("Could not delete artifact %s: %s", artifact_id, exc)

    def get_run_logs_summary(self, run_id: int, max_chars: int = 1200) -> str:
        """Best-effort tail of the failed jobs, for error messages."""
        try:
            jobs = self._request("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs").json()
        except GitHubActionsError:
            return ""
        failed = [
            f"{job.get('name')}: "
            + ", ".join(
                step.get("name", "")
                for step in job.get("steps", [])
                if step.get("conclusion") not in (None, "success", "skipped")
            )
            for job in jobs.get("jobs", [])
            if job.get("conclusion") not in (None, "success", "skipped")
        ]
        return "; ".join(filter(None, failed))[:max_chars]
