"""Project journal: append-only progress log with secret redaction."""

import re
from datetime import UTC, datetime
from pathlib import Path

JOURNAL_PATH = Path("docs/PROGRESS_LOG.md")

# Patterns whose *values* should be redacted
_SECRET_KEYS = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION|CREDENTIAL)",
    re.IGNORECASE,
)

# Matches: KEY=value, "key": "value", key: value (YAML-style)
_KV_PATTERNS = [
    # ENV style: SOME_API_KEY=sk-ant-abc123...
    re.compile(
        r"(" + _SECRET_KEYS.pattern + r"[A-Z_]*)\s*=\s*(\S+)",
        re.IGNORECASE,
    ),
    # JSON style: "some_api_key": "value"
    re.compile(
        r'("[^"]*' + _SECRET_KEYS.pattern + r'[^"]*")\s*:\s*"([^"]*)"',
        re.IGNORECASE,
    ),
    # Header style: Authorization: Bearer xxx
    re.compile(
        r"(Authorization)\s*:\s*(\S+.*?)(?:\s*$|\s{2,})",
        re.IGNORECASE | re.MULTILINE,
    ),
]


def redact(text: str) -> str:
    """Replace secret values with [REDACTED]. Keys are preserved."""
    result = text
    for pattern in _KV_PATTERNS:
        result = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", result)
    return result


def journal_append(section_title: str, body: str, journal_path: Path | None = None) -> Path:
    """Append a timestamped section to the progress log.

    Returns the path written to.
    """
    path = journal_path or JOURNAL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    safe_body = redact(body)

    entry = f"\n## {section_title}\n_{ts}_\n\n{safe_body}\n\n---\n"

    with path.open("a", encoding="utf-8") as f:
        # Add header on first write
        if path.stat().st_size == 0:
            f.write("# Bitcoin Education - Progress Log\n\n")
            f.write("Append-only project journal. Do NOT edit past entries.\n\n---\n")
        f.write(entry)

    return path


def journal_event(event_type: str, data: dict, journal_path: Path | None = None) -> Path:
    """Log a structured event as a journal entry."""
    lines = [f"**{event_type}**\n"]
    for key, value in data.items():
        if _SECRET_KEYS.search(str(key)):
            value = "[REDACTED]"
        lines.append(f"- **{key}**: {value}")
    body = "\n".join(lines)
    return journal_append(event_type, body, journal_path=journal_path)
