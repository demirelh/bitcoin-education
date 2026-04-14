# Implementierungsplan: Moderator-Content-Bereinigung

## Analyse des Ist-Zustands

### Pipeline-Flow (tagesschau_tr Profil)
```
CORRECTED → segment_broadcast → SEGMENTED → translate (per_story) → TRANSLATED → [adapt SKIPPED] → chapterize → ...
```

### Problem
1. `_translate_per_story()` übersetzt ALLE Stories mit demselben Prompt — inklusive intro/outro
2. Moderator-Begrüßungen ("Guten Abend, hier ist Jens Riewa mit der tagesschau") werden 1:1 ins Türkische übersetzt
3. Der Adapt-Stage ist für `tagesschau_tr` **deaktiviert** (`adapt.skip: true`), d.h. es gibt keinen nachgelagerten Bereinigungsschritt
4. Der Chapterizer bekommt die unbereinigten intro/outro-Texte

### Wichtige Erkenntnisse
- `story.story_type` ist im per-story-Flow verfügbar (StoryType.INTRO / .OUTRO)
- `_translate_per_story()` iteriert bereits über jede Story — idealer Eingriffspunkt
- Die Lösung muss **im Translate-Schritt** greifen, weil adapt übersprungen wird

---

## Architektur-Entscheidung: Translate-Prompt erweitern (KEIN neues Stage)

**Begründung:**
- Ein neues Pipeline-Stage wäre Overkill (zusätzlicher Status, Migration, PipelineRun-Record)
- Der Translator hat bereits Zugriff auf `story_type` im per-story-Modus
- Intro/Outro-Stories sind kurz (10-30 Sekunden) — ein spezialisierter Prompt reicht
- Zusätzlich: deterministischer Regex-Postprocessing für Moderatornamen (kein LLM nötig)

**Ansatz: 2-Schichten-Bereinigung**
1. **Schicht 1 (LLM)**: Separater Translate-Prompt für intro/outro-Stories → neutralisiert Begrüßungen
2. **Schicht 2 (Regex)**: Deterministisches Postprocessing → entfernt Moderatornamen, Sendungsnamen

---

## Änderungsliste

### 1. Neues Prompt-Template: `tagesschau_tr/translate_intro_outro.md`

**Pfad:** `btcedu/prompts/templates/tagesschau_tr/translate_intro_outro.md`

**Inhalt:**
```markdown
---
name: tagesschau_tr/translate_intro_outro
model: claude-sonnet-4-20250514
temperature: 0.2
max_tokens: 2048
description: Translates and neutralizes German broadcast intro/outro for Turkish news video
---

# System

Du bist ein professioneller Nachrichtenredakteur, der deutsche Nachrichtensendungs-Intros
und -Outros für eine türkischsprachige Nachrichtensendung adaptiert.

## KERNAUFGABE

Transformiere den deutschen Intro/Outro-Text in einen **neutralen türkischen Nachrichtentext**.
Dies ist KEINE wörtliche Übersetzung — es ist eine Neuformulierung.

## REGELN

1. **MODERATORNAMEN ENTFERNEN**: Alle Moderatornamen (Jens Riewa, Susanne Daubner,
   Judith Rakers, Jan Hofer, Linda Zervakis, Constantin Schreiber, etc.) werden KOMPLETT entfernt.
2. **SENDUNGSNAMEN ENTFERNEN**: "tagesschau", "Tagesschau", "tagesthemen", "Das Erste", "ARD"
   dürfen NICHT im türkischen Output erscheinen.
3. **BEGRÜSSUNGSFORMELN ERSETZEN**: Deutsche Begrüßungen werden durch neutrale türkische
   Nachrichtenformeln ersetzt:
   - "Guten Abend, meine Damen und Herren" → "Günün önemli gelişmeleri"
   - "Willkommen zur tagesschau" → (weglassen)
   - "Hier sind die Nachrichten" → "İşte günün haberleri"
4. **THEMENVORSCHAU BEIBEHALTEN**: Wenn das Intro Themen aufzählt ("Heute mit folgenden Themen:
   Klimagipfel, Ukraine, Bundeshaushalt"), MÜSSEN diese Themen übersetzt und beibehalten werden.
5. **VERABSCHIEDUNGEN ERSETZEN**:
   - "Das war's von der tagesschau" → "Haberler sona erdi"
   - "Ich wünsche Ihnen einen schönen Abend" → (weglassen)
   - "Morgen begrüßt Sie dann..." → (weglassen)
6. **NACHRICHTENAKTEURE BEIBEHALTEN**: Politiker, Experten, Interviewpartner, die im Intro
   erwähnt werden, MÜSSEN erhalten bleiben (z.B. "Bundeskanzler Scholz" bleibt).

## INTRO-TEMPLATE (als Orientierung)

Für Intros:
"Günün öne çıkan gelişmeleri: [Thema 1], [Thema 2], [Thema 3]."

Für Outros:
"Haberlerin sonu. Bir sonraki bültenimizde görüşmek üzere."

## FORMAT

Gib NUR den türkischen Text zurück. Keine Erklärungen, keine Markierungen.
Wenn der Input nur eine Begrüßungsfloskel ohne Themenvorschau ist, gib einen
einzeiligen neutralen Opener zurück.

# Input

{{ transcript }}
```

### 2. Moderator-Pattern-Modul: `btcedu/core/moderator_patterns.py`

**Pfad:** `btcedu/core/moderator_patterns.py`

**Zweck:** Deterministisches Regex-Postprocessing für Moderatornamen und Sendungsnamen.

```python
"""Deterministic moderator content cleaning patterns."""

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

# German broadcast show names
BROADCAST_NAMES: list[str] = [
    "tagesschau",
    "Tagesschau",
    "tagesthemen",
    "Tagesthemen",
    "tagesschau24",
    "Das Erste",
    "ARD",
    "Nachtmagazin",
]

# Turkish equivalents that should also be cleaned (from literal translations)
BROADCAST_NAMES_TR: list[str] = [
    "tagesschau",
    "Tagesschau",
    "tagesthemen",
]

# Greeting patterns (German) - used for validation/testing
GREETING_PATTERNS_DE: list[str] = [
    r"[Gg]uten\s+[Aa]bend",
    r"[Ww]illkommen\s+(?:bei|zur|zum)",
    r"[Hh]ier\s+ist\s+\w+\s+\w+\s+mit\s+der",
    r"[Mm]eine\s+[Dd]amen\s+und\s+[Hh]erren",
    r"[Ii]ch\s+begrüße\s+[Ss]ie",
    r"[Dd]as\s+war(?:'s|\s+es)\s+(?:von|aus)\s+der",
    r"[Ii]ch\s+wünsche\s+[Ii]hnen",
    r"[Mm]orgen\s+begrüßt\s+[Ss]ie\s+dann",
    r"[Ss]chönen\s+(?:Abend|Tag|Feierabend)",
]


def clean_moderator_names(text: str) -> str:
    """Remove moderator names from translated text.

    Handles patterns like:
    - "Ben Jens Riewa" (Turkish: I am Jens Riewa)
    - "Jens Riewa ile" (with Jens Riewa)
    - "Jens Riewa burada" (Jens Riewa here)
    - Standalone name references
    """
    result = text
    for name in MODERATOR_NAMES:
        # Remove "Ben [Name]" pattern (Turkish "I am [Name]")
        result = re.sub(rf"\bBen\s+{re.escape(name)}\b[,.]?\s*", "", result)
        # Remove "[Name] ile" pattern (Turkish "with [Name]")
        result = re.sub(rf"\b{re.escape(name)}\s+ile\b[,.]?\s*", "", result)
        # Remove "[Name] burada" pattern
        result = re.sub(rf"\b{re.escape(name)}\s+burada\b[,.]?\s*", "", result)
        # Remove standalone name
        result = re.sub(rf"\b{re.escape(name)}\b[,.]?\s*", "", result)

    # Clean broadcast names in Turkish output
    for name in BROADCAST_NAMES_TR:
        result = re.sub(rf"\b{re.escape(name)}\b", "", result)

    # Normalize whitespace
    result = re.sub(r"  +", " ", result).strip()
    # Fix orphaned punctuation
    result = re.sub(r"^\s*[,.:]\s*", "", result)
    result = re.sub(r"\s+([,.])", r"\1", result)

    return result


def has_moderator_content(text_de: str) -> bool:
    """Check if German text contains moderator greeting/farewell patterns."""
    for pattern in GREETING_PATTERNS_DE:
        if re.search(pattern, text_de):
            return True
    return any(name in text_de for name in MODERATOR_NAMES)
```

### 3. Änderung: `btcedu/core/translator.py` — `_translate_per_story()`

**Was sich ändert:** Intro/Outro-Stories bekommen einen separaten Prompt + Regex-Postprocessing.

```python
# In _translate_per_story(), Zeile ~548, innerhalb der Story-Schleife:

for i, story in enumerate(story_doc.stories):
    is_intro_outro = story.story_type in ("intro", "outro")

    if is_intro_outro:
        # Use specialized intro/outro prompt
        active_system, active_user_tpl = _get_intro_outro_prompt(
            session, profile_namespace, settings
        )
    else:
        active_system, active_user_tpl = system_prompt, user_template

    # ... rest of translation logic uses active_system, active_user_tpl ...

    # After translation, apply regex cleaning for intro/outro
    if is_intro_outro:
        from btcedu.core.moderator_patterns import clean_moderator_names
        story_dict["text_tr"] = clean_moderator_names(story_dict["text_tr"])
        story_dict["headline_tr"] = clean_moderator_names(story_dict["headline_tr"])
```

**Neue Hilfsfunktion in translator.py:**
```python
def _get_intro_outro_prompt(
    session: Session,
    profile_namespace: str,
    settings: Settings,
) -> tuple[str, str]:
    """Load the intro/outro translation prompt for news broadcasts."""
    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path(
        "translate_intro_outro.md", profile=profile_namespace
    )
    if not template_file.exists():
        # Fallback: use regular translate prompt (no special handling)
        template_file = registry.resolve_template_path(
            "translate.md", profile=profile_namespace
        )
    _, body = registry.load_template(template_file)
    return _split_prompt(body)
```

### 4. Änderung: `btcedu/profiles/tagesschau_tr.yaml`

```yaml
stage_config:
  translate:
    mode: per_story
    register: formal_news
    clean_moderator: true    # NEU: aktiviert Moderator-Bereinigung
```

### 5. Tests: `tests/test_moderator_patterns.py`

**Testfälle:**

```python
class TestCleanModeratorNames:
    def test_removes_known_moderator(self):
        assert clean_moderator_names("Ben Jens Riewa, haberler...") == "haberler..."

    def test_preserves_politician_names(self):
        text = "Başbakan Scholz bugün açıkladı"
        assert clean_moderator_names(text) == text

    def test_removes_broadcast_name(self):
        assert "tagesschau" not in clean_moderator_names("tagesschau haberlerine hoş geldiniz")

    def test_handles_multiple_moderators(self):
        text = "Susanne Daubner burada. Yarın Jens Riewa ile görüşeceğiz."
        result = clean_moderator_names(text)
        assert "Susanne Daubner" not in result
        assert "Jens Riewa" not in result

    def test_empty_input(self):
        assert clean_moderator_names("") == ""


class TestHasModeratorContent:
    def test_detects_greeting(self):
        assert has_moderator_content("Guten Abend, meine Damen und Herren")

    def test_detects_moderator_name(self):
        assert has_moderator_content("Hier ist Jens Riewa mit der tagesschau")

    def test_no_false_positive_on_news(self):
        assert not has_moderator_content("Der Bundeskanzler hat heute erklärt")
```

### 6. Tests: `tests/test_translator_intro_outro.py`

**Testfälle für die Integration:**

```python
class TestTranslatePerStoryIntroOutro:
    """Test that intro/outro stories get special prompt + regex cleaning."""

    def test_intro_uses_specialized_prompt(self, ...):
        """Verify intro stories use translate_intro_outro.md prompt."""

    def test_outro_uses_specialized_prompt(self, ...):
        """Verify outro stories use translate_intro_outro.md prompt."""

    def test_regular_story_uses_standard_prompt(self, ...):
        """Verify regular stories still use translate.md prompt."""

    def test_moderator_name_removed_after_translation(self, ...):
        """Verify regex postprocessing removes moderator names."""

    def test_clean_moderator_disabled_by_profile(self, ...):
        """Verify clean_moderator=false skips special handling."""
```

---

## Dateiliste (Zusammenfassung)

| Datei | Aktion | Beschreibung |
|-------|--------|-------------|
| `btcedu/prompts/templates/tagesschau_tr/translate_intro_outro.md` | **NEU** | Spezialisierter Prompt für Intro/Outro-Neutralisierung |
| `btcedu/core/moderator_patterns.py` | **NEU** | Regex-Patterns + `clean_moderator_names()` + `has_moderator_content()` |
| `btcedu/core/translator.py` | **ÄNDERN** | `_translate_per_story()` erkennt intro/outro, verwendet separaten Prompt + Regex |
| `btcedu/profiles/tagesschau_tr.yaml` | **ÄNDERN** | `clean_moderator: true` Konfiguration |
| `tests/test_moderator_patterns.py` | **NEU** | Unit-Tests für Regex-Patterns |
| `tests/test_translator_intro_outro.py` | **NEU** | Integrationstests für Intro/Outro-Flow |

---

## Nicht betroffen / Keine Änderung nötig

- `btcedu/core/segmenter.py` — markiert bereits story_type korrekt
- `btcedu/prompts/templates/segment_broadcast.md` — intro/outro-Erkennung funktioniert
- `btcedu/prompts/templates/adapt.md` — adapt ist für tagesschau_tr deaktiviert
- `btcedu/core/adapter.py` — wird nicht aufgerufen
- `btcedu/core/chapterizer.py` — bekommt saubere intro/outro-Texte
- `btcedu/core/pipeline.py` — keine Änderung, kein neues Stage
- `bitcoin_podcast.yaml` — kein Segmenter, kein per-story → nicht betroffen

---

## Risiken und Mitigationen

1. **Regex entfernt zu viel**: Die Patterns sind auf bekannte Moderatornamen beschränkt. Unbekannte neue Moderatoren werden vom LLM-Prompt abgefangen (Schicht 1).
2. **LLM-Prompt entfernt Nachrichtenakteure**: Der Prompt hat explizite Regeln, Politiker/Experten zu erhalten. Tests validieren dies.
3. **bitcoin_podcast Profil**: Nicht betroffen, da kein Segmenter aktiviert und kein per-story-Modus.
4. **Idempotenz**: Der Prompt-Hash ändert sich → bestehende Übersetzungen werden automatisch neu generiert (durch vorhandene Idempotenzlogik).
