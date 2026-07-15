"""Deterministic post-translation glossary fixes.

The translation LLM occasionally produces German→Turkish hybrid words (a German
noun with a Turkish case suffix, e.g. "tankstasyona") or a small set of
consistently mistranslated terms. These are cheap, unambiguous, context-free
substitutions that we apply deterministically after the LLM step so they never
reach downstream stages regardless of LLM variability.

Only add entries here for fixes that are ALWAYS correct without context. Anything
that depends on sentence context belongs in the translate.md prompt rules instead.
"""

import re

# (compiled pattern, replacement). Patterns keep any Turkish case suffix that
# follows the stem via a backreference so "tankstasyona"/"tankstasyonda" etc.
# all map to the correct Turkish base word plus the same suffix.
_GLOSSARY_RULES: list[tuple[re.Pattern[str], str]] = [
    # "Tankstelle" leaked as the hybrid "tankstasyon(+suffix)" → "benzin istasyonu(+suffix)"
    (
        re.compile(r"\btankstasyon(a|da|dan|un|una|unda|undan|u)?\b", re.IGNORECASE),
        lambda m: "benzin istasyonu" + _tank_suffix(m.group(1)),
    ),
]


def _tank_suffix(suffix: str | None) -> str:
    """Map the suffix captured on the German hybrid to the correct suffix on
    "benzin istasyonu" (which already ends in the possessive -u)."""
    mapping = {
        None: "",
        "": "",
        "u": "",
        "a": "na",
        "una": "na",
        "da": "nda",
        "unda": "nda",
        "dan": "ndan",
        "undan": "ndan",
        "un": "nun",
    }
    return mapping.get((suffix or "").lower(), "")


def fix_translation_glossary(text: str) -> str:
    """Apply deterministic glossary substitutions to translated Turkish text."""
    if not text:
        return text
    for pattern, replacement in _GLOSSARY_RULES:
        text = pattern.sub(replacement, text)
    return text
