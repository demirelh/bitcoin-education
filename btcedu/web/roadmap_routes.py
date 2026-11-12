"""Read-only dashboard view of ``docs/ROADMAP.md``, the project's single plan.

The Markdown file stays the source of truth: agents and the operator edit it,
this module only parses it on every request. Registered after ``init_auth``,
so the global login gate applies.
"""

import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

from btcedu.core import roadmap_agent
from btcedu.web.auth import operator_ref

ROADMAP_PATH = Path(__file__).resolve().parents[2] / "docs" / "ROADMAP.md"

REPO_ROOT = ROADMAP_PATH.parents[1]

logger = logging.getLogger(__name__)

roadmap_bp = Blueprint("roadmap", __name__)
roadmap_api_bp = Blueprint("roadmap_api", __name__)

_SECTION = re.compile(r"^## (.+?)\s*$")
_MILESTONE = re.compile(r"^### (M\d+)\s*[–-]\s*(.+?)\s*$")
_TASK = re.compile(r"^- \[([ xX])\] (.+)$")
_OWNER = re.compile(r"^(Du|Agent(?: \+ Du)?):\s*")
_NUMBERED = re.compile(r"^\d+\.\s+(.+)$")


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = [
        [_plain(cell) for cell in line.strip().strip("|").split("|")]
        for line in lines
        if line.lstrip().startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", line.strip())
    ]
    return rows[1:]  # first row is the header


def parse_roadmap(text: str) -> dict:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    def section(prefix: str) -> list[str]:
        return next((body for name, body in sections.items() if name.startswith(prefix)), [])

    milestones = []
    for line in section("Meilensteine"):
        heading = _MILESTONE.match(line)
        task = _TASK.match(line)
        if heading:
            milestones.append({"id": heading.group(1), "title": heading.group(2), "tasks": []})
        elif task and milestones:
            body = _plain(task.group(2))
            owner = _OWNER.match(body)
            milestones[-1]["tasks"].append(
                {
                    "done": task.group(1).lower() == "x",
                    "owner": owner.group(1) if owner else "",
                    "text": body[owner.end() :] if owner else body,
                }
            )
    for milestone in milestones:
        milestone["total"] = len(milestone["tasks"])
        milestone["done"] = sum(task["done"] for task in milestone["tasks"])

    return {
        "goals": [
            {"metric": r[0], "target": r[1]} for r in _table_rows(section("Ziel")) if len(r) >= 2
        ],
        "status": [
            {"area": r[0], "finding": r[1], "source": r[2] if len(r) > 2 else ""}
            for r in _table_rows(section("Ist-Stand"))
            if len(r) >= 2
        ],
        "milestones": milestones,
        "progress": {
            "done": sum(m["done"] for m in milestones),
            "total": sum(m["total"] for m in milestones),
        },
        "next_steps": [
            _plain(m.group(1))
            for line in section("Nächste Schritte")
            if (m := _NUMBERED.match(line))
        ],
        "decisions": [
            {"id": r[0], "question": r[1], "recommendation": r[2] if len(r) > 2 else ""}
            for r in _table_rows(section("Offene Entscheidungen"))
            if len(r) >= 2
        ],
        "log": [
            {"date": r[0], "entry": r[1]} for r in _table_rows(section("Statuslog")) if len(r) >= 2
        ],
    }


def _git(*args: str) -> subprocess.CompletedProcess | None:
    """Run a read-only git command in the checkout; ``None`` when git is unusable."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # git missing, repo broken, hang
        logger.warning("Fahrplan: git nicht verfügbar (%s)", exc)
        return None


def _pending_commits() -> int:
    """Commits on ``roadmap`` that the checkout's ``main`` does not have yet."""
    proc = _git("rev-list", "--count", f"main..{roadmap_agent.BRANCH}")
    if proc is None or proc.returncode != 0:
        return 0
    count = proc.stdout.strip()
    return int(count) if count.isdigit() else 0


def roadmap_source() -> tuple[str | None, str, int, str | None]:
    """Which plan actually applies: the committed ``roadmap`` branch or the checkout file.

    The coordinator commits to the ``roadmap`` branch in its own worktree. Until
    that branch reaches ``main``, the checkout's file is stale and a ticked-off
    item would look open to the operator. Only the *committed* branch state is
    used, and only while the branch is genuinely ahead: a worktree that merely
    sits behind ``main``, or a half-finished edit during a run, must never
    replace the checkout. Read-only; any git problem falls back to the checkout.
    """
    checkout_text = ROADMAP_PATH.read_text(encoding="utf-8") if ROADMAP_PATH.is_file() else None
    pending = _pending_commits()
    if pending > 0:
        shown = _git("show", f"{roadmap_agent.BRANCH}:docs/ROADMAP.md")
        if shown is not None and shown.returncode == 0 and shown.stdout.strip():
            if shown.stdout != checkout_text:
                stamp = _git(
                    "log", "-1", "--format=%cI", roadmap_agent.BRANCH, "--", "docs/ROADMAP.md"
                )
                ok = stamp is not None and stamp.returncode == 0
                updated = stamp.stdout.strip() if ok else None
                return shown.stdout, "branch", pending, updated
    return checkout_text, "checkout", pending, None


@roadmap_bp.route("/roadmap")
def roadmap_page():
    return render_template("roadmap.html")


@roadmap_api_bp.route("")
def roadmap_data():
    text, source, pending, updated = roadmap_source()
    if text is None:
        return jsonify({"error": "docs/ROADMAP.md fehlt"}), 404
    data = parse_roadmap(text)
    if updated is None and ROADMAP_PATH.is_file():
        updated = datetime.fromtimestamp(ROADMAP_PATH.stat().st_mtime, UTC).isoformat()
    data["updated_at"] = updated
    data["source"] = source
    data["pending_commits"] = pending
    return jsonify(data)


@roadmap_api_bp.route("/chat")
def chat_status():
    return jsonify(roadmap_agent.chat_status(current_app.config["settings"]))


@roadmap_api_bp.route("/activity")
def chat_activity():
    return jsonify(roadmap_agent.activity(current_app.config["settings"]))


@roadmap_api_bp.route("/chat", methods=["POST"])
def chat_send():
    body = request.get_json(silent=True) or {}
    try:
        started = roadmap_agent.start_message(
            current_app.config["settings"],
            str(body.get("message", "")),
            operator_ref(),
            autopilot=bool(body.get("autopilot")),
        )
    except roadmap_agent.ChatError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(started), 202


@roadmap_api_bp.route("/chat/stop", methods=["POST"])
def chat_stop():
    return jsonify({"stopped": roadmap_agent.stop(current_app.config["settings"])})


@roadmap_api_bp.route("/chat/reset", methods=["POST"])
def chat_reset():
    try:
        session_id = roadmap_agent.reset(current_app.config["settings"])
    except roadmap_agent.ChatError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"session_id": session_id})
