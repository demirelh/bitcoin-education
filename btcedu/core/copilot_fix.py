"""Launch and track one-shot Copilot repairs for pipeline failures."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_NAME = "copilotfix"
DEFAULT_MODEL = "gpt-5.6-sol"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_FILENAME = "copilot_fixes.json"


@dataclass(frozen=True)
class CopilotFixLaunch:
    started: bool
    already_running: bool = False
    already_attempted: bool = False
    session: str = SESSION_NAME
    model: str = DEFAULT_MODEL
    stage: str = "unknown"


def marker_path(outputs_dir: str | Path, episode_id: str) -> Path:
    if not _SAFE_ID.fullmatch(episode_id):
        raise ValueError(f"Invalid episode ID: {episode_id}")
    return Path(outputs_dir) / episode_id / "provenance" / _FILENAME


def load_entries(outputs_dir: str | Path, episode_id: str) -> list[dict]:
    path = marker_path(outputs_dir, episode_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Copilot fix marker: {path}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Copilot fix marker must contain a list: {path}")
    return [entry for entry in data if isinstance(entry, dict)]


def stage_attribution(outputs_dir: str | Path, episode_id: str) -> dict[str, dict]:
    attribution = {}
    for entry in load_entries(outputs_dir, episode_id):
        stage = entry.get("stage")
        if isinstance(stage, str):
            attribution[stage] = entry
    return attribution


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{_FILENAME}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            json.dump(entries, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _with_locked_entries(outputs_dir: str | Path, episode_id: str, callback):
    path = marker_path(outputs_dir, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        entries = load_entries(outputs_dir, episode_id)
        result = callback(entries)
        _write_entries(path, entries)
        return result


def _claim_attempt(
    outputs_dir: str | Path,
    episode_id: str,
    stage: str,
    model: str,
    error_message: str,
    automatic: bool,
) -> str | None:
    error_hash = hashlib.sha256(error_message.encode("utf-8")).hexdigest()

    def claim(entries: list[dict]) -> str | None:
        if automatic and any(
            entry.get("automatic") is True and entry.get("error_hash") == error_hash
            for entry in entries
        ):
            return None
        attempt_id = uuid.uuid4().hex
        entries.append(
            {
                "attempt_id": attempt_id,
                "episode_id": episode_id,
                "stage": stage,
                "automatic": automatic,
                "error_hash": error_hash,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "model": model,
            }
        )
        return attempt_id

    return _with_locked_entries(outputs_dir, episode_id, claim)


def complete_attempt(
    outputs_dir: str | Path,
    episode_id: str,
    attempt_id: str,
    exit_code: int,
    repository: str | Path,
) -> None:
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )

    def complete(entries: list[dict]) -> None:
        entry = next(
            (item for item in entries if item.get("attempt_id") == attempt_id),
            None,
        )
        if entry is None:
            raise ValueError(f"Unknown Copilot fix attempt: {attempt_id}")
        entry["status"] = "success" if exit_code == 0 else "failed"
        entry["completed_at"] = datetime.now(UTC).isoformat()
        entry["git_commit"] = commit.stdout.strip() if commit.returncode == 0 else None

    _with_locked_entries(outputs_dir, episode_id, complete)


def record_success(
    outputs_dir: str | Path,
    episode_id: str,
    stage: str,
    model: str,
    repository: str | Path,
) -> Path:
    """Record a completed manual fix, including legacy/current-session fixes."""
    attempt_id = _claim_attempt(
        outputs_dir,
        episode_id,
        stage,
        model,
        f"legacy-manual-fix:{uuid.uuid4().hex}",
        automatic=False,
    )
    complete_attempt(outputs_dir, episode_id, attempt_id or "", 0, repository)
    return marker_path(outputs_dir, episode_id)


def start_copilot_fix(
    settings,
    episode_id: str,
    title: str,
    stage: str,
    error_message: str,
    *,
    automatic: bool,
    profile: str = "tagesschau_tr",
) -> CopilotFixLaunch:
    """Start Copilot in a detached tmux session, at most once per automatic error."""
    model = getattr(settings, "copilot_auto_fix_model", DEFAULT_MODEL)
    if automatic and (
        not getattr(settings, "copilot_auto_fix_enabled", True)
        or getattr(settings, "dry_run", False)
    ):
        return CopilotFixLaunch(started=False, model=model, stage=stage)

    tmux_binary = shutil.which("tmux")
    copilot_binary = shutil.which(getattr(settings, "copilot_cli_binary", "copilot"))
    if not tmux_binary or not copilot_binary:
        missing = "tmux" if not tmux_binary else "copilot"
        raise RuntimeError(f"Required command is not installed: {missing}")

    existing = subprocess.run(
        [tmux_binary, "has-session", "-t", f"={SESSION_NAME}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        return CopilotFixLaunch(
            started=False,
            already_running=True,
            model=model,
            stage=stage,
        )

    attempt_id = _claim_attempt(
        settings.outputs_dir,
        episode_id,
        stage,
        model,
        error_message,
        automatic,
    )
    if attempt_id is None:
        return CopilotFixLaunch(
            started=False,
            already_attempted=True,
            model=model,
            stage=stage,
        )

    repository = Path(__file__).resolve().parents[2]
    regression_command = shlex.join(
        [
            "btcedu",
            "regression-run",
            "--from-stage",
            stage,
            "--only-stage",
            "--profile",
            profile,
            "--count",
            "3",
        ]
    )
    regression_requirement = (
        " Before committing, pushing, or resuming the failed episode, you MUST run "
        f"`{regression_command}`. This isolated regression must execute the affected "
        "stage successfully for the three most recent episodes of the same profile. "
        "The Copilot fix is not successful if this command is skipped or fails; in "
        "that case do not commit, do not push, and do not resume the episode."
    )
    prompt = (
        "Fix only the root cause of the exact btcedu pipeline error shown below for the "
        "selected episode. Reproduce and trace this specific failure using the episode's "
        "current artifacts and logs; do not broaden the task to unrelated cleanup. Work "
        "autonomously, make complete surgical code changes, and run the smallest relevant "
        "tests. Update every Markdown file whose documentation or instructions are affected "
        "by the fix, including applicable CLAUDE.md files."
        f"{regression_requirement} After validation, definitively "
        "commit all files belonging to this fix with a clear commit message and push the "
        "commit to the current remote branch. Then resume this exact episode through the "
        "repository's supported pipeline command so processing continues from the stage "
        "where it failed; do not restart completed stages unnecessarily. Do not deploy. "
        f"Selected episode: {episode_id} ({title}). Failed stage: {stage}. "
        f"Exact current pipeline error: {error_message[:4000]}"
    )
    copilot_command = shlex.join(
        [
            copilot_binary,
            "--model",
            model,
            "--allow-all",
            "--autopilot",
            "--no-ask-user",
            "-p",
            prompt,
        ]
    )
    completion_command = shlex.join(
        [
            sys.executable,
            "-m",
            "btcedu.core.copilot_fix",
            "complete",
            "--outputs-dir",
            str(settings.outputs_dir),
            "--episode-id",
            episode_id,
            "--attempt-id",
            attempt_id,
            "--repository",
            str(repository),
        ]
    )
    tmux_command = (
        f"{copilot_command}; status=$?; "
        f"{completion_command} --exit-code \"$status\"; exit \"$status\""
    )
    started = subprocess.run(
        [
            tmux_binary,
            "new-session",
            "-d",
            "-s",
            SESSION_NAME,
            "-c",
            str(repository),
            tmux_command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        complete_attempt(
            settings.outputs_dir,
            episode_id,
            attempt_id,
            started.returncode,
            repository,
        )
        detail = (started.stderr or started.stdout).strip()
        raise RuntimeError(f"Could not start Copilot fix session: {detail}")

    return CopilotFixLaunch(started=True, model=model, stage=stage)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    complete = subparsers.add_parser("complete")
    complete.add_argument("--outputs-dir", required=True)
    complete.add_argument("--episode-id", required=True)
    complete.add_argument("--attempt-id", required=True)
    complete.add_argument("--exit-code", required=True, type=int)
    complete.add_argument("--repository", required=True)
    args = parser.parse_args()
    complete_attempt(
        args.outputs_dir,
        args.episode_id,
        args.attempt_id,
        args.exit_code,
        args.repository,
    )


if __name__ == "__main__":
    main()
