# Pipeline Status — Episode SJFLLZxlWqk

**Episode:** Wir rennen schneller, arbeiten mehr, sparen härter, und kommen trotzdem nicht voran.
**Datum:** 2026-03-02
**Status:** APPROVED (bereit für Publish)

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
| imagegen | done | 15/15 Bilder (14 DALL-E + 1 Placeholder), $1.12 |
| tts | done | 15/15 Chapters, 823.4s Audio, $3.57 |
| render | done | 15 Segmente, 823.4s, 103.7MB draft.mp4 |
| review_gate_3 | done | auto-approved |
| publish | blockiert | YouTube OAuth nicht eingerichtet |

## Kosten

| Posten | Betrag |
|--------|--------|
| LLM (correct, translate, adapt, chapterize) | $1.32 |
| DALL-E 3 (15 Bilder) | $1.12 |
| ElevenLabs TTS (15 Chapters) | $3.57 |
| Render (lokal, ffmpeg) | $0.00 |
| **Gesamt** | **~$6.01** |

## Output-Dateien

- **Draft-Video:** `data/outputs/SJFLLZxlWqk/render/draft.mp4` (103.7MB, ~13:43 Min)
- **Chapters JSON:** `data/outputs/SJFLLZxlWqk/chapters.json`
- **Bilder:** `data/outputs/SJFLLZxlWqk/images/` (15 PNGs)
- **TTS Audio:** `data/outputs/SJFLLZxlWqk/tts/` (15 MP3s)

## Behobene Code-Bugs (Session 2026-03-02)

1. **Chapterizer `_retry_with_correction`** — Schema-Beispiel im Corrective Prompt hinzugefügt, damit GPT-4o korrekte verschachtelte Objekte zurückgibt
2. **Chapterizer `_fix_chapter_data`** — Neue Post-Processing-Funktion: fixiert ungültige visual types (z.B. `historical_image` → `b_roll`), berechnet Durations neu aus Word Counts, setzt fehlende `episode_id`
3. **`image_generator.py` `chapter.visuals[0]`** → `chapter.visual` (Singular, wie im Pydantic-Schema definiert)
4. **`MediaAsset` FK-Problem** — `ForeignKey("prompt_versions.id")` aus ORM-Model entfernt (eigenes `declarative_base()` kann cross-Base FK nicht auflösen)
5. **`ContentArtifact` ungültige Felder** — `prompt_version_id`, `input_tokens`, `output_tokens`, `cost_usd` entfernt, `model` Pflichtfeld hinzugefügt
6. **ffmpeg `drawtext` Filter-Syntax** — `drawtext:` → `drawtext=` (Filtername muss mit `=` statt `:` an Optionen angehängt werden)

## Nächster Schritt

1. `btcedu youtube-auth` ausführen (interaktiver OAuth-Flow, braucht Browser)
2. `btcedu run --episode-id SJFLLZxlWqk` → Publish to YouTube
