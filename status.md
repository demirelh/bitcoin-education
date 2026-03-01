# Pipeline Status — Episode SJFLLZxlWqk

**Episode:** Wir rennen schneller, arbeiten mehr, sparen härter, und kommen trotzdem nicht voran.
**Datum:** 2026-03-02

## Pipeline-Fortschritt

| Stage | Status | Details |
|-------|--------|---------|
| download | done | |
| transcribe | done | |
| correct | done | |
| review_gate_1 | done | auto-approved |
| translate | done | |
| adapt | done | |
| review_gate_2 | done | auto-approved |
| chapterize | done | 15 Chapters, ~614s, $0.09 |
| imagegen | teilweise | 9/15 Bilder generiert, Rest failed wegen Billing-Limit |
| tts | blockiert | ElevenLabs 402: bezahlter Plan erforderlich |
| render | ausstehend | |
| review_gate_3 | ausstehend | |
| publish | ausstehend | |

## Blockierende Billing-Probleme

| Service | Problem | Lösung |
|---------|---------|--------|
| **OpenAI DALL-E** | `Billing hard limit has been reached` | Limit erhöhen auf platform.openai.com → Billing → Usage limits |
| **ElevenLabs** | `Free users cannot use library voices via the API` | Upgrade auf bezahlten Plan auf elevenlabs.io, oder eigene Voice-ID erstellen |

## Behobene Code-Bugs (diese Session)

1. **Chapterizer `_retry_with_correction`** — Schema-Beispiel im Corrective Prompt hinzugefügt, damit GPT-4o korrekte verschachtelte Objekte zurückgibt
2. **Chapterizer `_fix_chapter_data`** — Neue Post-Processing-Funktion: fixiert ungültige visual types (z.B. `historical_image` → `b_roll`), berechnet Durations neu aus Word Counts, setzt fehlende `episode_id`
3. **`image_generator.py` `chapter.visuals[0]`** → `chapter.visual` (Singular, wie im Pydantic-Schema definiert)
4. **`MediaAsset` FK-Problem** — `ForeignKey("prompt_versions.id")` aus ORM-Model entfernt (eigenes `declarative_base()` kann cross-Base FK nicht auflösen)
5. **`ContentArtifact` ungültige Felder** — `prompt_version_id`, `input_tokens`, `output_tokens`, `cost_usd` entfernt, `model` Pflichtfeld hinzugefügt

## Nächste Schritte

1. OpenAI Billing-Limit erhöhen → `btcedu imagegen --episode-id SJFLLZxlWqk --force`
2. ElevenLabs auf bezahlten Plan upgraden → `btcedu tts --episode-id SJFLLZxlWqk`
3. `btcedu run --episode-id SJFLLZxlWqk` bis RENDERED/PUBLISHED
