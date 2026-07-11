"""Glossary loader for per-domain translation term consistency.

Loads YAML glossaries from btcedu/prompts/glossaries/ and injects
them into the translator's system prompt as a term-consistency block.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GLOSSARY_DIR = Path(__file__).parent / "glossaries"

# Map content_profile identifiers → glossary filename
_PROFILE_TO_GLOSSARY = {
    "bitcoin_tr": "glossary_bitcoin_tr.yaml",
    "bitcoin_podcast": "glossary_bitcoin_tr.yaml",
    "tagesschau_tr": "glossary_news_tr.yaml",
    "news_tr": "glossary_news_tr.yaml",
}


def load_glossary(profile_id: str) -> dict[str, Any] | None:
    """Load a YAML glossary for a given content profile. Returns None if missing."""
    filename = _PROFILE_TO_GLOSSARY.get(profile_id)
    if not filename:
        return None
    path = _GLOSSARY_DIR / filename
    if not path.exists():
        logger.debug("Glossary not found for profile %s: %s", profile_id, path)
        return None
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to load glossary %s: %s", path, e)
        return None


def build_glossary_prompt_block(glossary: dict[str, Any]) -> str:
    """Render the glossary as an appendix that can be appended to a system prompt."""
    if not glossary:
        return ""

    lines: list[str] = []
    lines.append("\n\n=== TERM CONSISTENCY GLOSSARY (STRICTLY FOLLOW) ===")

    tone = glossary.get("tone", {})
    if tone:
        lines.append("\n**Style / Register:**")
        for k, v in tone.items():
            if isinstance(v, str):
                v = v.strip()
                if v:
                    lines.append(f"- {k}: {v}")

    terms = glossary.get("terms", {})
    if terms:
        lines.append("\n**Fixed term translations (DE → TR):**")
        for de, tr in terms.items():
            lines.append(f"- {de} → {tr}")

    phrases = glossary.get("phrases", {})
    if phrases:
        lines.append("\n**Fixed phrase translations:**")
        for de, tr in phrases.items():
            lines.append(f'- "{de}" → "{tr}"')

    lines.append("\n=== END GLOSSARY ===\n")
    return "\n".join(lines)


def inject_glossary_into_prompt(system_prompt: str, profile_id: str | None) -> str:
    """Append the glossary appendix to a system prompt if a glossary exists for the profile."""
    if not profile_id:
        return system_prompt
    glossary = load_glossary(profile_id)
    if not glossary:
        return system_prompt
    return system_prompt + build_glossary_prompt_block(glossary)
