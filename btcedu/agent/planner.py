"""AI planner — uses Claude API to generate improvement suggestions from analysis."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from btcedu.config import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior Python developer reviewing the "btcedu" project — an automated pipeline \
that converts German Bitcoin podcast episodes into Turkish YouTube videos. \
Stack: Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite, \
deployed on Raspberry Pi.

Based on the project analysis below, suggest up to {max_issues} concrete, actionable improvements.

Rules:
- Each suggestion must reference specific evidence from the analysis (file paths, error codes, etc.)
- Focus on high-impact improvements: failing tests, lint errors, code debt, maintainability
- Do NOT suggest trivial changes (formatting, adding docstrings everywhere, renaming)
- Do NOT suggest changes outside the btcedu/ and tests/ directories
- Each suggestion should be independently implementable
- Include a clear test command to verify the fix

Return ONLY a JSON array (no markdown fences). Each item:
{{"title": "short issue title", "body": "detailed description with evidence and test command", \
"labels": ["bug"|"enhancement"|"tech-debt"], "priority": "high"|"medium"|"low"}}
"""


@dataclass
class Suggestion:
    title: str
    body: str
    labels: list[str]
    priority: str


def generate_suggestions(
    analysis_summary: str,
    settings: Settings,
    max_issues: int = 3,
) -> list[Suggestion]:
    """Send analysis to Claude and return structured improvement suggestions."""
    if not settings.anthropic_api_key or settings.anthropic_api_key == "dummy":
        logger.warning("No Anthropic API key configured — skipping planner")
        return []

    try:
        import anthropic
    except ImportError:
        logger.error("anthropic package not installed")
        return []

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    system = SYSTEM_PROMPT.format(max_issues=max_issues)
    user_msg = f"## Project Analysis\n\n{analysis_summary}"

    try:
        response = client.messages.create(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        return []

    raw = response.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return _parse_suggestions(raw, max_issues)


def _parse_suggestions(raw_json: str, max_issues: int) -> list[Suggestion]:
    """Parse and validate LLM response into Suggestion objects."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse planner JSON: %s", e)
        return []

    if not isinstance(data, list):
        logger.error("Planner returned non-list: %s", type(data))
        return []

    ALLOWED_LABELS = {"bug", "enhancement", "tech-debt"}
    ALLOWED_PRIORITIES = {"high", "medium", "low"}

    suggestions: list[Suggestion] = []
    for item in data[:max_issues]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))[:200]
        body = str(item.get("body", ""))[:3000]
        if not title or not body:
            continue

        labels = [lb for lb in item.get("labels", []) if lb in ALLOWED_LABELS]
        priority = item.get("priority", "medium")
        if priority not in ALLOWED_PRIORITIES:
            priority = "medium"

        suggestions.append(Suggestion(title=title, body=body, labels=labels, priority=priority))

    return suggestions
