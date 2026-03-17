"""Deterministic moderator content cleaning patterns.

Provides regex-based post-processing to remove German broadcast moderator
names and show references from translated Turkish text. Used as a second
layer after the LLM-based intro/outro translation prompt.
"""

import re

# Known tagesschau moderators (Nachrichtensprecher + Moderatoren)
MODERATOR_NAMES: list[str] = [
    "Jens Riewa",
    "Susanne Daubner",
    "Judith Rakers",
    "Jan Hofer",
    "Linda Zervakis",
    "Constantin Schreiber",
    "Thorsten Schröder",
    "Ingo Zamperoni",
    "Caren Miosga",
    "Julia-Niharika Sen",
    "Aline Abboud",
    "Helge Fuhst",
    "Michail Paweletz",
    "Ellen Ehni",
    "Jessy Wellmer",
    "Mark Bator",
    "Karsten Arndt",
    "Sandrine Harder",
    "Susanne Holst",
    "Claus-Erich Boetzkes",
]

# German broadcast show names that must not appear in Turkish output
BROADCAST_NAMES_TR: list[str] = [
    "tagesschau",
    "Tagesschau",
    "tagesthemen",
    "Tagesthemen",
    "tagesschau24",
    "Nachtmagazin",
]

# Greeting patterns (German) — used for detection/validation
GREETING_PATTERNS_DE: list[re.Pattern[str]] = [
    re.compile(r"[Gg]uten\s+[Aa]bend"),
    re.compile(r"[Ww]illkommen\s+(?:bei|zur|zum)"),
    re.compile(r"[Hh]ier\s+ist\s+\w+\s+\w+\s+mit\s+der"),
    re.compile(r"[Mm]eine\s+[Dd]amen\s+und\s+[Hh]erren"),
    re.compile(r"[Ii]ch\s+begrüße\s+[Ss]ie"),
    re.compile(r"[Dd]as\s+war(?:'s|\s+es)\s+(?:von|aus)\s+der"),
    re.compile(r"[Ii]ch\s+wünsche\s+[Ii]hnen"),
    re.compile(r"[Mm]orgen\s+begrüßt\s+[Ss]ie\s+dann"),
    re.compile(r"[Ss]chönen\s+(?:Abend|Tag|Feierabend)"),
]


def clean_moderator_names(text: str) -> str:
    """Remove moderator names and broadcast references from translated text.

    Handles patterns like:
    - "Ben Jens Riewa" (Turkish: I am Jens Riewa)
    - "Jens Riewa ile" (with Jens Riewa)
    - "Jens Riewa burada" (Jens Riewa here)
    - Standalone name references
    """
    if not text:
        return text

    result = text
    for name in MODERATOR_NAMES:
        escaped = re.escape(name)
        # Remove "Ben [Name]" pattern (Turkish "I am [Name]")
        result = re.sub(rf"\bBen\s+{escaped}\b[,.]?\s*", "", result)
        # Remove "[Name] ile" pattern (Turkish "with [Name]")
        result = re.sub(rf"\b{escaped}\s+ile\b[,.]?\s*", "", result)
        # Remove "[Name] burada" pattern
        result = re.sub(rf"\b{escaped}\s+burada\b[,.]?\s*", "", result)
        # Remove standalone name with optional trailing punctuation
        result = re.sub(rf"\b{escaped}\b[,.]?\s*", "", result)

    # Clean broadcast names in Turkish output
    for name in BROADCAST_NAMES_TR:
        result = re.sub(rf"\b{re.escape(name)}\b", "", result)

    # Normalize whitespace
    result = re.sub(r"  +", " ", result).strip()
    # Fix orphaned leading punctuation
    result = re.sub(r"^\s*[,.:]\s*", "", result)
    # Fix double punctuation from removed content
    result = re.sub(r"\s+([,.])", r"\1", result)
    # Fix multiple consecutive punctuation
    result = re.sub(r"([,.])\s*[,.]+", r"\1", result)

    return result


def has_moderator_content(text_de: str) -> bool:
    """Check if German text contains moderator greeting/farewell patterns."""
    for pattern in GREETING_PATTERNS_DE:
        if pattern.search(text_de):
            return True
    return any(name in text_de for name in MODERATOR_NAMES)
