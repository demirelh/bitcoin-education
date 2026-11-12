"""Chat with the ``roadmap-lead`` agent from the dashboard and from WhatsApp.

Every operator message becomes one Copilot CLI run in its own detached tmux
session. All runs share one Copilot session ID, so the conversation continues
across messages, and a memory file outside the repository carries what the
operator decided and prefers beyond a reset. The CLI's own ``events.jsonl`` is
the transcript and the source of the live activity view. The agent works in a
separate ``roadmap`` worktree, never in the production checkout.
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

SESSION_PREFIX = "roadmap-lead"
AGENT_NAME = "roadmap-lead"
BRANCH = "roadmap"
MAX_MESSAGE_CHARS = 8000
MAX_QUEUED = 5
MAX_REPLY_CHARS = 3000
MAX_AUTOPILOT_CONTINUES = 15
# The coordinator may push and deploy since 2026-11-12 (operator's decision).
# sudo stays blocked: nothing in the roadmap work needs root. run.sh calls
# `sudo -n systemctl` itself, which fails immediately when the NOPASSWD rule
# is missing instead of waiting for a password nobody can type.
DENIED_TOOLS = ("shell(sudo)",)
REPOSITORY = Path(__file__).resolve().parents[2]

TEAM = (
    ("roadmap-lead", "Koordinator"),
    ("pipeline-engineer", "Pipeline"),
    ("youtube-growth", "YouTube"),
    ("avatar-studio", "Avatar"),
    ("newsroom-web", "Newsroom/Web"),
    ("qa-reviewer", "Prüfung"),
)

MEMORY_TEMPLATE = """# Gedächtnis des Koordinators

Dauerhafte Fakten aus Gesprächen mit dem Betreiber: Entscheidungen, Vorlieben,
Begriffe („wenn ich X sage, meine ich Y"), Zusagen. Kurz halten, Veraltetes
löschen. Keine Geheimnisse.

## Entscheidungen

## Vorlieben und Begriffe

## Offene Zusagen
"""


class ChatError(RuntimeError):
    """A request the operator can fix (empty message, full queue, ...)."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def chat_dir(settings) -> Path:
    return Path(settings.outputs_dir).resolve().parent / "roadmap-chat"


def memory_path(settings) -> Path:
    return chat_dir(settings) / "memory.md"


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


def _state_file(state_dir: Path) -> Path:
    return state_dir / "conversation.json"


def _load(state_dir: Path) -> dict:
    path = _state_file(state_dir)
    state = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {"session_id": str(uuid.uuid4()), "runs": []}
    )
    state.setdefault("queue", [])
    return state


def _save(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "conversation.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_state_file(state_dir))


def _tmux() -> str | None:
    return shutil.which("tmux")


def _session_exists(name: str) -> bool:
    tmux = _tmux()
    if not tmux:
        return False
    probe = [tmux, "has-session", "-t", f"={name}"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def _active_run(state: dict) -> dict | None:
    for run in reversed(state["runs"]):
        # Runs started before per-run sessions used the bare prefix as their session name.
        if run.get("finished_at") is None and _session_exists(run.get("tmux", SESSION_PREFIX)):
            return run
    return None


def is_running(settings) -> bool:
    return _active_run(_load(chat_dir(settings))) is not None


def _copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def _events(session_id: str) -> list[dict]:
    path = _copilot_home() / "session-state" / session_id / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # the CLI may be mid-write on the last line
    return events


def _tool_label(data: dict) -> str:
    args = data.get("arguments") or {}
    detail = ""
    if isinstance(args, dict):
        for key in ("command", "path", "pattern", "description", "prompt"):
            if args.get(key):
                detail = str(args[key])
                break
    detail = " ".join(detail.split())
    return f"{data.get('toolName', '?')} {detail[:120]}".strip()


def transcript(session_id: str, limit: int = 300) -> list[dict]:
    """Messages, tool calls and summaries of the Copilot session, oldest first."""
    items: list[dict] = []
    for event in _events(session_id):
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "user.message":
            items.append({"role": "user", "text": data.get("content", "")})
        elif kind == "assistant.message" and data.get("content"):
            if not data.get("parentToolCallId"):
                items.append({"role": "assistant", "text": data["content"]})
        elif kind == "tool.execution_start":
            items.append({"role": "tool", "text": _tool_label(data)})
        elif kind == "subagent.started":
            items.append({"role": "tool", "text": f"Agent {data.get('agentName', '?')} gestartet"})
        elif kind == "session.task_complete" and data.get("summary"):
            items.append({"role": "summary", "text": data["summary"]})
    return items[-limit:]


def activity(settings) -> dict:
    """Who works on what right now, derived from the live Copilot event stream."""
    state = _load(chat_dir(settings))
    run = _active_run(state)
    last = state["runs"][-1] if state["runs"] else None
    agents = {
        name: {"name": name, "label": label, "state": "idle", "action": "", "since": None}
        for name, label in TEAM
    }
    if run is None:
        return {
            "running": False,
            "agents": list(agents.values()),
            "run": None,
            "last_run": last,
            "queued": len(state["queue"]),
        }

    lead = agents[AGENT_NAME]
    lead.update(state="working", since=run["started_at"], action="denkt nach …")
    subagents: dict[str, str] = {}  # toolCallId -> agent name
    for event in _events(state["session_id"])[run.get("events_before", 0) :]:
        kind, data = event.get("type"), event.get("data") or {}
        stamp = event.get("timestamp")
        if kind == "subagent.started":
            name = data.get("agentName", "?")
            subagents[data.get("toolCallId", "")] = name
            entry = agents.setdefault(
                name, {"name": name, "label": name, "state": "idle", "action": "", "since": None}
            )
            entry.update(state="working", since=stamp, action="übernimmt den Auftrag")
            lead["action"] = f"hat {name} beauftragt"
        elif kind == "subagent.completed":
            name = subagents.get(data.get("toolCallId", ""), data.get("agentName", "?"))
            if name in agents:
                agents[name].update(state="done", action="fertig")
        elif kind == "tool.execution_start":
            owner = subagents.get(data.get("parentToolCallId") or "", AGENT_NAME)
            if owner in agents and data.get("toolName") not in ("task", "report_intent"):
                agents[owner]["action"] = _tool_label(data)
        elif kind == "assistant.message" and data.get("content"):
            owner = subagents.get(data.get("parentToolCallId") or "", AGENT_NAME)
            if owner in agents:
                agents[owner]["action"] = " ".join(data["content"].split())[:160]
    return {
        "running": True,
        "agents": list(agents.values()),
        "run": run,
        "last_run": last,
        "queued": len(state["queue"]),
    }


def chat_status(settings) -> dict:
    state = _load(chat_dir(settings))
    memory = memory_path(settings)
    return {
        "running": _active_run(state) is not None,
        "session_id": state["session_id"],
        "worktree": str(worktree_path()),
        "runs": state["runs"][-10:],
        "queued": len(state["queue"]),
        "items": transcript(state["session_id"]),
        "memory": memory.read_text(encoding="utf-8") if memory.is_file() else "",
    }


def _prompt(settings, message: str, channel: str) -> str:
    """Wrap the operator text with the channel and the memory contract."""
    via = "WhatsApp (kurz antworten, ohne Tabellen)" if channel == "whatsapp" else "Dashboard"
    return (
        f"[Kanal: {via}. Gedächtnis: {memory_path(settings)} – zuerst lesen; am Ende "
        "neue dauerhafte Entscheidungen, Vorlieben und Zusagen dort eintragen.]\n\n"
        f"{message}"
    )


def _launch(settings, state: dict, message: str, operator: str, autopilot: bool, channel: str):
    tmux = _tmux()
    copilot = shutil.which(getattr(settings, "copilot_cli_binary", "copilot"))
    if not tmux or not copilot:
        raise ChatError(f"Nicht installiert: {'tmux' if not tmux else 'copilot'}")
    worktree = ensure_worktree()
    directory = chat_dir(settings)
    memory = memory_path(settings)
    if not memory.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        memory.write_text(MEMORY_TEMPLATE, encoding="utf-8")

    run_id = uuid.uuid4().hex[:12]
    session = f"{SESSION_PREFIX}-{run_id}"
    run = {
        "id": run_id,
        "tmux": session,
        "operator": operator,
        "channel": channel,
        "autopilot": autopilot,
        "message": message[:200],
        "events_before": len(_events(state["session_id"])),
        "started_at": _utcnow(),
        "finished_at": None,
        "exit_code": None,
    }
    state["runs"].append(run)
    _save(directory, state)

    copilot_cmd = [copilot, "--agent", AGENT_NAME, "--session-id", state["session_id"]]
    copilot_cmd += ["--allow-all-tools", "--no-ask-user", "--add-dir", str(directory)]
    # Tests need the production checkout's .venv; the agent only reads there.
    copilot_cmd += ["--add-dir", str(REPOSITORY)]
    # --add-dir alone is not enough for the checkout's virtualenv: .venv/bin/python
    # is a symlink to the interpreter in /usr, and path verification rejects the
    # resolved target. Without this the agent cannot run the test suite at all;
    # the dangerous commands stay blocked by DENIED_TOOLS.
    copilot_cmd += ["--allow-all-paths"]
    for tool in DENIED_TOOLS:
        copilot_cmd += ["--deny-tool", tool]
    if autopilot:
        copilot_cmd += ["--autopilot", "--max-autopilot-continues", str(MAX_AUTOPILOT_CONTINUES)]
    copilot_cmd += ["-p", _prompt(settings, message, channel)]
    # Same rule as copilot_cli_env(): a classic PAT in .env would override the CLI login.
    unset = [
        f"-u{name}"
        for name in COPILOT_TOKEN_ENV_VARS
        if os.environ.get(name, "").startswith(UNSUPPORTED_COPILOT_TOKEN_PREFIXES)
    ]
    finish_cmd = [sys.executable, "-m", "btcedu.core.roadmap_agent", "finish"]
    finish_cmd += ["--state-dir", str(directory), "--run-id", run_id]
    log_path = shlex.quote(str(directory / f"{run_id}.log"))
    shell = (
        f"{shlex.join(['env', *unset, *copilot_cmd])} > {log_path} 2>&1; "
        # The hook must import btcedu and read .env from the main checkout, not the worktree.
        f"status=$?; cd {shlex.quote(str(REPOSITORY))} && "
        f'{shlex.join(finish_cmd)} --exit-code "$status"'
    )
    started = subprocess.run(
        [tmux, "new-session", "-d", "-s", session, "-c", str(worktree), shell],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        run.update(finished_at=_utcnow(), exit_code=-1)
        _save(directory, state)
        raise ChatError(f"tmux-Start fehlgeschlagen: {(started.stderr or started.stdout).strip()}")
    return run


def start_message(
    settings, message: str, operator: str, *, autopilot: bool, channel: str = "dashboard"
) -> dict:
    """Start a run now, or queue the message behind the one that is running."""
    message = (message or "").strip()
    if not message:
        raise ChatError("Leere Nachricht.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ChatError(f"Nachricht ist länger als {MAX_MESSAGE_CHARS} Zeichen.")
    directory = chat_dir(settings)
    state = _load(directory)
    if _active_run(state) is not None:
        if len(state["queue"]) >= MAX_QUEUED:
            raise ChatError(f"Warteschlange voll ({MAX_QUEUED}). Bitte später erneut.")
        state["queue"].append(
            {"message": message, "operator": operator, "autopilot": autopilot, "channel": channel}
        )
        _save(directory, state)
        return {"queued": len(state["queue"]), "session_id": state["session_id"]}
    run = _launch(settings, state, message, operator, autopilot, channel)
    return {"run_id": run["id"], "session_id": state["session_id"]}


def stop(settings) -> bool:
    directory = chat_dir(settings)
    state = _load(directory)
    run = _active_run(state)
    tmux = _tmux()
    if run is None or not tmux:
        return False
    subprocess.run([tmux, "kill-session", "-t", f"={run['tmux']}"], check=False)
    run.update(finished_at=_utcnow(), exit_code=-1)
    state["queue"] = []
    _save(directory, state)
    return True


def reset(settings) -> str:
    """Start a fresh conversation; the memory file is kept on purpose."""
    directory = chat_dir(settings)
    state = _load(directory)
    if _active_run(state) is not None:
        raise ChatError("Der Koordinator arbeitet noch. Erst stoppen.")
    state["session_id"] = str(uuid.uuid4())
    state["queue"] = []
    _save(directory, state)
    return state["session_id"]


def run_reply(state: dict, run: dict) -> str:
    """The final answer of one run: its task summary, else its last top-level message."""
    answer = ""
    for event in _events(state["session_id"])[run.get("events_before", 0) :]:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "session.task_complete" and data.get("summary"):
            answer = data["summary"]
        elif kind == "assistant.message" and data.get("content"):
            if not data.get("parentToolCallId"):
                answer = data["content"]
    answer = answer.strip() or "(keine Antwort)"
    if len(answer) > MAX_REPLY_CHARS:
        answer = "…" + answer[-MAX_REPLY_CHARS:]
    return answer


def status_text(settings) -> str:
    live = activity(settings)
    if not live["running"]:
        return "Koordinator: bereit." + (f" Wartend: {live['queued']}." if live["queued"] else "")
    lines = [f"Koordinator arbeitet seit {live['run']['started_at'][11:16]} UTC:"]
    lines += [
        f"• {agent['label']}: {agent['action'] or 'arbeitet'}"
        for agent in live["agents"]
        if agent["state"] == "working"
    ]
    if live["queued"]:
        lines.append(f"Wartend: {live['queued']}")
    return "\n".join(lines)


def handle_inbound(settings, text: str) -> str:
    """WhatsApp entry point. Returns the immediate acknowledgement to send back."""
    command = text.strip().lower().rstrip(".!")
    if command in {"status", "?"}:
        return status_text(settings)
    if command in {"stopp", "stop", "abbrechen"}:
        return "Abgebrochen." if stop(settings) else "Es läuft gerade nichts."
    if command in {"neu", "neues gespräch"}:
        reset(settings)
        return "Neues Gespräch begonnen. Das Gedächtnis bleibt erhalten."
    if command in {"hilfe", "help"}:
        return (
            "Schreib einfach, was du willst. Der Koordinator antwortet hier.\n"
            "„auto: …“ = selbstständig arbeiten (Autopilot)\n"
            "status · stopp · neu · hilfe"
        )
    autopilot = command.startswith("auto:")
    message = text.strip()[5:].strip() if autopilot else text
    started = start_message(
        settings, message, "whatsapp:self", autopilot=autopilot, channel="whatsapp"
    )
    if "queued" in started:
        return f"Vorgemerkt ({started['queued']}. in der Warteschlange)."
    return "Koordinator arbeitet …" + (" (Autopilot)" if autopilot else "")


def _finish(state_dir: Path, run_id: str, exit_code: int) -> None:
    from btcedu.config import Settings
    from btcedu.services.notify_service import send_notification

    state = _load(state_dir)
    run = next((r for r in state["runs"] if r["id"] == run_id), None)
    if run is None or run.get("finished_at") is not None:
        return
    run.update(finished_at=_utcnow(), exit_code=exit_code)
    _save(state_dir, state)

    settings = Settings()
    if not settings.notify_whatsapp_enabled and run.get("channel") == "whatsapp":
        print(f"WhatsApp disabled in {Path.cwd()}/.env, reply not sent", file=sys.stderr)
    if run.get("channel") == "whatsapp":
        prefix = "" if exit_code == 0 else f"(Exit {exit_code}) "
        send_notification(settings, f"🧭 {prefix}{run_reply(state, run)}")
    if state["queue"]:
        nxt = state["queue"].pop(0)
        _save(state_dir, state)
        try:
            _launch(
                settings, state, nxt["message"], nxt["operator"], nxt["autopilot"], nxt["channel"]
            )
        except ChatError as exc:
            if nxt["channel"] == "whatsapp":
                send_notification(settings, f"🧭 Konnte nicht starten: {exc}")


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
