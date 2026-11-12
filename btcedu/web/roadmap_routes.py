"""Read-only dashboard view of ``docs/ROADMAP.md``, the project's single plan.

The Markdown file stays the source of truth: agents and the operator edit it,
this module only parses it on every request. Registered after ``init_auth``,
so the global login gate applies.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template

ROADMAP_PATH = Path(__file__).resolve().parents[2] / "docs" / "ROADMAP.md"

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


@roadmap_bp.route("/roadmap")
def roadmap_page():
    return render_template("roadmap.html")


@roadmap_api_bp.route("")
def roadmap_data():
    if not ROADMAP_PATH.is_file():
        return jsonify({"error": "docs/ROADMAP.md fehlt"}), 404
    data = parse_roadmap(ROADMAP_PATH.read_text(encoding="utf-8"))
    data["updated_at"] = datetime.fromtimestamp(ROADMAP_PATH.stat().st_mtime, UTC).isoformat()
    return jsonify(data)
