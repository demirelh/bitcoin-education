"""Dashboard chat with the ``roadmap-lead`` agent.

Every operator message starts one Copilot CLI run in a detached tmux session.
All runs share one Copilot session ID, so the conversation continues across
messages; the CLI's own ``events.jsonl`` is the transcript shown in the
dashboard. The agent works in a separate ``roadmap`` worktree, never in the
production checkout.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from btcedu.services.claude_service import (
    COPILOT_TOKEN_ENV_VARS,
    UNSUPPORTED_COPILOT_TOKEN_PREFIXES,
)

SESSION_NAME = "roadmap-lead"
AGENT_NAME = "roadmap-lead"
BRANCH = "roadmap"
MAX_MESSAGE_CHARS = 8000
MAX_AUTOPILOT_CONTINUES = 15
DENIED_TOOLS = ("shell(git push)", "shell(./run.sh)", "shell(bash run.sh)", "shell(sudo)")
REPOSITORY = Path(__file__).resolve().parents[2]


class ChatError(RuntimeError):
    """A request the operator can fix (busy agent, empty message, ...)."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def chat_dir(settings) -> Path:
    return Path(settings.outputs_dir).resolve().parent / "roadmap-chat"


def worktree_path() -> Path:
    return REPOSITORY.parent / f"{REPOSITORY.name}-roadmap"


def _git(*args: str, cwd: Path = REPOSITORY) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def ensure_worktree() -> Path:
    """Create the roadmap worktree, or fast-forward it to main when it is behind and clean."""
    path = worktree_path()
    if not (path / ".git").exists():
        has_branch = _git("rev-parse", "--verify", "--quiet", BRANCH).returncode == 0
        args = (
            ["worktree", "add", str(path), BRANCH]
            if has_branch
            else ["worktree", "add", "-b", BRANCH, str(path)]
        )
        created = _git(*args)
        if created.returncode != 0:
            raise ChatError(f"Worktree konnte nicht angelegt werden: {created.stderr.strip()}")
        return path
    clean = _git("status", "--porcelain", cwd=path).stdout.strip() == ""
    behind = _git("merge-base", "--is-ancestor", BRANCH, "main").returncode == 0
    if clean and behind:
        _git("merge", "--ff-only", "main", cwd=path)
    return path


def _load_state(settings) -> dict:
    path = chat_dir(settings) / "conversation.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"session_id": str(uuid.uuid4()), "runs": []}


def _save_state(settings, state: dict) -> None:
    directory = chat_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "conversation.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(directory / "conversation.json")


def is_running() -> bool:
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    probe = [tmux, "has-session", "-t", f"={SESSION_NAME}"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def _copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def transcript(session_id: str, limit: int = 300) -> list[dict]:
    """Messages, tool calls and summaries of the Copilot session, oldest first."""
    events = _copilot_home() / "session-state" / session_id / "events.jsonl"
    if not events.is_file():
        return []
    items: list[dict] = []
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # the CLI may be mid-write on the last line
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "user.message":
            items.append({"role": "user", "text": data.get("content", "")})
        elif kind == "assistant.message" and data.get("content"):
            items.append({"role": "assistant", "text": data["content"]})
        elif kind == "tool.execution_start":
            args = json.dumps(data.get("arguments") or {}, ensure_ascii=False)
            items.append({"role": "tool", "text": f"{data.get('toolName', '?')} {args[:160]}"})
        elif kind == "subagent.started":
            items.append({"role": "tool", "text": f"Agent {data.get('agentName', '?')} gestartet"})
        elif kind == "session.task_complete" and data.get("summary"):
            items.append({"role": "summary", "text": data["summary"]})
    return items[-limit:]


def chat_status(settings) -> dict:
    state = _load_state(settings)
    return {
        "running": is_running(),
        "session_id": state["session_id"],
        "worktree": str(worktree_path()),
        "runs": state["runs"][-10:],
        "items": transcript(state["session_id"]),
    }


def start_message(settings, message: str, operator: str, *, autopilot: bool) -> dict:
    message = (message or "").strip()
    if not message:
        raise ChatError("Leere Nachricht.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ChatError(f"Nachricht ist länger als {MAX_MESSAGE_CHARS} Zeichen.")
    tmux = shutil.which("tmux")
    copilot = shutil.which(getattr(settings, "copilot_cli_binary", "copilot"))
    if not tmux or not copilot:
        raise ChatError(f"Nicht installiert: {'tmux' if not tmux else 'copilot'}")
    if is_running():
        raise ChatError("Der Koordinator arbeitet noch. Warten oder stoppen.")

    worktree = ensure_worktree()
    state = _load_state(settings)
    run_id = uuid.uuid4().hex[:12]
    state["runs"].append(
        {
            "id": run_id,
            "operator": operator,
            "autopilot": autopilot,
            "started_at": _utcnow(),
            "finished_at": None,
            "exit_code": None,
        }
    )
    _save_state(settings, state)

    copilot_cmd = [copilot, "--agent", AGENT_NAME, "--session-id", state["session_id"]]
    copilot_cmd += ["--allow-all-tools", "--no-ask-user"]
    for tool in DENIED_TOOLS:
        copilot_cmd += ["--deny-tool", tool]
    if autopilot:
        copilot_cmd += ["--autopilot", "--max-autopilot-continues", str(MAX_AUTOPILOT_CONTINUES)]
    copilot_cmd += ["-p", message]
    # Same rule as copilot_cli_env(): a classic PAT in .env would override the CLI login.
    unset = [
        f"-u{name}"
        for name in COPILOT_TOKEN_ENV_VARS
        if os.environ.get(name, "").startswith(UNSUPPORTED_COPILOT_TOKEN_PREFIXES)
    ]
    finish_cmd = [
        sys.executable,
        "-m",
        "btcedu.core.roadmap_agent",
        "finish",
        "--state-dir",
        str(chat_dir(settings)),
        "--run-id",
        run_id,
    ]
    log_path = shlex.quote(str(chat_dir(settings) / f"{run_id}.log"))
    shell = (
        f"{shlex.join(['env', *unset, *copilot_cmd])} > {log_path} 2>&1; "
        f'status=$?; {shlex.join(finish_cmd)} --exit-code "$status"'
    )
    started = subprocess.run(
        [tmux, "new-session", "-d", "-s", SESSION_NAME, "-c", str(worktree), shell],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        raise ChatError(f"tmux-Start fehlgeschlagen: {(started.stderr or started.stdout).strip()}")
    return {"run_id": run_id, "session_id": state["session_id"]}


def stop(settings) -> bool:
    tmux = shutil.which("tmux")
    if not tmux or not is_running():
        return False
    subprocess.run([tmux, "kill-session", "-t", f"={SESSION_NAME}"], check=False)
    return True


def reset(settings) -> str:
    if is_running():
        raise ChatError("Der Koordinator arbeitet noch. Erst stoppen.")
    state = _load_state(settings)
    state["session_id"] = str(uuid.uuid4())
    _save_state(settings, state)
    return state["session_id"]


def _finish(state_dir: Path, run_id: str, exit_code: int) -> None:
    path = state_dir / "conversation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for run in state["runs"]:
        if run["id"] == run_id:
            run["finished_at"] = _utcnow()
            run["exit_code"] = exit_code
    tmp = state_dir / "conversation.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    finish = sub.add_parser("finish")
    finish.add_argument("--state-dir", required=True, type=Path)
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--exit-code", required=True, type=int)
    args = parser.parse_args()
    _finish(args.state_dir, args.run_id, args.exit_code)


if __name__ == "__main__":
    main()
