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

_FOLLOWUP_PROGRAM_NAME_RE = re.compile(
    r"\b(?:tagesthemen|nachtmagazin)\b",
    re.IGNORECASE,
)
_FOLLOWUP_PROGRAM_SCHEDULE_RE = re.compile(
    r"\b(?:"
    r"melden\s+sich|"
    r"sehen\s+sie|"
    r"folgen?|"
    r"gegen\s+\d{1,2}[.:]\d{2}\s*uhr|"
    r"um\s+\d{1,2}[.:]\d{2}\s*uhr|"
    r"halbzeitpause"
    r")\b",
    re.IGNORECASE,
)


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


def is_followup_program_preview(text_de: str) -> bool:
    """Detect a schedule/teaser for a later news programme in German source text."""
    if not text_de:
        return False
    return bool(
        _FOLLOWUP_PROGRAM_NAME_RE.search(text_de)
        and _FOLLOWUP_PROGRAM_SCHEDULE_RE.search(text_de)
    )


# --- Neutral broadcast flow: remove anchor transitions, program hints, sign-offs ---

# Program/Tagesthemen preview hint (Turkish), spanning to the end of the text —
# includes any teaser list and trailing good-bye. Matches e.g.
# "Saat 21.45'te güncel haberlerle devam edeceğiz: … İyi akşamlar dileriz."
_PROGRAM_HINT_RE = re.compile(
    r"\s*Saat\s+\d{1,2}[.:]\d{2}['’´`]?\w*\s+güncel\s+haberlerle.*$",
    re.IGNORECASE | re.DOTALL,
)

# Leading topic transition into sport: "Şimdi spora geçiyoruz."
_SPORT_TRANSITION_RE = re.compile(
    r"^\s*Şimdi\s+spor[a-zçğıöşü]*\s+geçiyoruz[.!]?\s*",
    re.IGNORECASE,
)

# Trailing good-bye: "İyi akşamlar dileriz." / "İyi akşamlar."
_GOODBYE_RE = re.compile(
    r"\s*İyi\s+akşamlar(?:\s+dileriz)?[.!]?\s*$",
    re.IGNORECASE,
)

# Weather transition opener -> neutral opener. Captures the date part so
# "Şimdi yarınki hava durumu tahmini, 12 Temmuz Pazar günü." becomes
# "12 Temmuz Pazar günü için hava tahmini şöyle: …".
_WEATHER_OPENER_RE = re.compile(
    r"^\s*Şimdi\s+(?:yarınki\s+)?hava\s+durumu(?:\s+tahmini)?\s*,?\s*(.+?)\s*[.:]\s*",
    re.IGNORECASE,
)


def strip_broadcast_transitions(text: str) -> str:
    """Remove anchor moderation transitions, program hints and sign-offs.

    Used for fully neutral output: strips leading topic transitions, the
    Tagesthemen program hint (with teasers) and the closing good-bye, and
    rewrites the weather transition into a neutral opener. Only matches highly
    stereotyped broadcast phrases, so it is safe to apply to any story body.
    """
    if not text:
        return text

    result = text

    # Rewrite the weather transition into a neutral opener (if present at start).
    m = _WEATHER_OPENER_RE.match(result)
    if m:
        date_part = m.group(1).strip()
        result = f"{date_part} için hava tahmini şöyle: " + result[m.end() :]

    result = _SPORT_TRANSITION_RE.sub("", result)
    result = _PROGRAM_HINT_RE.sub("", result)
    result = _GOODBYE_RE.sub("", result)

    result = re.sub(r"  +", " ", result).strip()
    return result
