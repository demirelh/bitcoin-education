# Dual-Presenter News Format — Implementierungsplan

Ziel: Aus der bestehenden v2-Pipeline eine eigenständige türkischsprachige
Nachrichtensendung für Menschen in Deutschland machen — mit weiblicher
Hauptmoderatorin, Cavit als Reporter, eigener Marke, 8–10 Minuten Laufzeit,
Story-Ranking, informativen Overlays und ohne sichtbare Fremdquellenzeile.

Profil: `tagesschau_tr`. Alle Änderungen sind profilgesteuert; andere Profile und
`pipeline_version=1` bleiben unverändert.

---

## 1. Aktuelle relevante Architektur

### Stage-Kette (tagesschau_tr, v2)

```
download → transcribe → transcript_analyze → transcript_verify → correct →
transcript_qa → review_gate_transcript_qa → review_gate_1 → segment →
translate → adapt → review_gate_2 → chapterize → frameextract → imagegen →
review_gate_stock → tts → anchorgen → render → review_gate_3 → publish
```

`_V2_STAGES` in `core/pipeline.py` ist statisch, `_get_stages()` modifiziert sie
profilabhängig (fügt `segment` ein, entfernt `adapt`, benennt Gates um). Neue
Stages lassen sich dort sauber einhängen.

### Narrationskette (Invariante)

`qa_reviewer._downstream_narration()` bestimmt, welche Datei die *freigegebene
Narration* ist. Reihenfolge heute:

1. `stories_translated.json` (nur wenn `script.adapted.tr.md` fehlt)
2. `script.adapted.tr.md` bzw. `review/script.adapted.reviewed.tr.md`
3. `transcript.tr.txt`

Der Translation-Quality-Gate hasht genau diesen Text (`narration_sha256`).
`chapterizer._enforce_narration_lock()` verlangt, dass die aneinandergehängten
Kapitelnarrationen exakt diesem Text entsprechen (modulo technischer
Normalisierung in `core/narration_lock.py`). Für `tagesschau_tr` läuft `adapt`,
also ist `script.adapted.tr.md` die kanonische Narration.

### Datenmodelle

- `models/story_schema.py` — `Story` mit `category`, `story_type`,
  `is_lead_story`, `word_count`, `text_tr`, `text_adapted_tr`,
  `source_segment_ids`, `narration_sha256`.
- `models/chapter_schema.py` — `Chapter` mit `narration`, `visual`, `overlays`,
  `transitions`, plus optionale `story_type`, `source_text`, `metadata`.
- `Overlay` kennt heute nur `type`, `text`, `start_offset_seconds`,
  `duration_seconds` — also **eine** Textzeile.

### TTS

`core/tts.py` synthetisiert **eine** MP3 pro Kapitel mit **einer** Stimme aus
`stage_config.tts.voice_id`. Idempotenz über
`_chapter_tts_hash(chapter_id, narration, synthesis_text, voice_sig)`.

### Render

`core/renderer.py` baut pro Kapitel ein Segment aus Bild/Video + MP3 +
`OverlaySpec`s (`_chapter_to_overlay_specs`). Intro/Topic-Intro/Outro-Karten
werden über `stage_config.render.*` gesteuert. Wetterkapitel werden im Render aus
`images/chXX_weather.json` als Szenenvideo gebaut.

### Wetter

`core/weather/` funktioniert bereits deterministisch: Detection → Extraktion mit
`source_span` → Validierung → Scene-Plan → HTML/SVG+Chromium-Render →
Städte-Temperaturen von Open-Meteo als *attribuierte Kontextdaten*.
`core/final_review.py` prüft an `review_gate_3` auf Blank-/Freeze-Frames.

---

## 2. Gefundene Root Causes

| # | Symptom | Root Cause |
|---|---|---|
| 1 | `Kaynak: ARD tagesschau — btcedu Türkçe` im Video sichtbar | `prompts/templates/tagesschau_tr/chapterize.md` Regel 3 schreibt ein Attributions-Overlay für erstes und letztes Kapitel **vor**; zusätzlich `stage_config.render.outro_text` im Profil. |
| 2 | Overlays zeigen nur den Themennamen | Es gibt gar keine Story-Overlays — nur das Attributions-Overlay. Der sichtbare Themenname kommt aus der Topic-Intro-Karte (`chapter.title`). `Overlay` hat kein Feld für eine zweite Zeile. |
| 3 | Nur Cavits Stimme | `stage_config.tts.voice_id` ist ein einzelner Wert; `tts.py` kennt genau eine Stimme pro Episode. |
| 4 | Sendung zu lang (11–12 min) | Es existiert **kein** Story-Ranking und keine Kürzungslogik. `chapterize` übernimmt jede Story 1:1 („1 Beitrag = 1 Kapitel"), die Narrationssperre verbietet Kürzen. |
| 5 | Wirkt wie vorgelesene Übersetzung | Es gibt keine redaktionelle Stufe zwischen Übersetzung und Kapitelbildung. `adapt` darf ausdrücklich nur konservativ polieren. |
| 6 | Wetterteil „leer" | Der Renderer funktioniert (verifiziert an `IuNt7iyNtkI/images/ch10_weather.png`). Schwäche: mehrere Tage werden in eine Karte gemischt (z. B. „Güneybatı: Sağanak yağışlı, Güneşli") und der Szenen-Fallback ist nicht explizit gestuft. |
| 7 | Intro pro Episode neu gebaut | `render.intro_*` erzeugt die Intro-Karte bei jedem Render neu; es gibt kein wiederverwendbares Master-Asset. |

---

## 3. Geplante Änderungen

### 3.1 Branding und Quellenentfernung

Neuer Profilblock:

```yaml
branding:
  show_name: "Almanya'nın Nabzı"
  slogan: "Almanya'nın nabzı burada atıyor."
  visible_source_attribution: false
  internal_source_provenance: true
  source_in_description: true
  forbidden_visible_terms: [...]
```

- Attributionsregel aus `chapterize.md` entfernen.
- `render.outro_text` auf den Slogan umstellen.
- Neues Modul `core/branding_guard.py`:
  - `scan_visible_texts()` sammelt Kapiteltitel, Overlay-Texte, Narration,
    Intro-/Outro-Texte, Ticker.
  - `assert_no_forbidden_visible_text()` wird **vor Render** und **vor Publish**
    aufgerufen und wirft `PipelineError(PERMANENT_VALIDATION)`.
  - Zusätzlich ein Sanitizer in `chapterizer`, der Attributions-Overlays still
    entfernt, falls das Modell sie doch erzeugt (Rückwärtskompatibilität mit
    bereits gespeicherten Episoden).
- Interne Provenienz bleibt unverändert: `stories*.json.source_attribution`,
  QA-Artefakte, `provenance/`, und `publisher.py` schreibt die Quelle weiterhin
  in die YouTube-Beschreibung (`source_in_description: true`).

### 3.2 Neue Stage `script` (redaktionelles Sendungsskript)

Position: nach `review_gate_2`, vor `chapterize`. Aktivierung über
`stage_config.script.enabled`.

**Input:** `stories_adapted.json` (freigegebene Übersetzung), QA-Findings,
Zielgesamtdauer, Sprecherrollen, Branding.

**Ablauf:**

1. **Deterministisches Vor-Ranking** (`core/story_ranking.py`) aus
   `category`, `story_type`, `is_lead_story`, `word_count` und einer
   profilierbaren Themen-Relevanzmatrix (Rente, Steuern, Migration, Miete,
   Energie, … = hoch; EU/International = mittel; Promi/Sport/Kultur = niedrig).
2. **LLM-Redaktion** (`prompts/templates/tagesschau_tr/script_broadcast.md`):
   erzeugt pro Story `priority`, `display_headline`, `display_summary`,
   `viewer_relevance` und die `speaker_sequence` (Anchor-Intro,
   Reporter-Bericht, Anchor-Einordnung …) — grounded auf `text_adapted_tr`.
3. **Deterministische Nachbearbeitung:** Begrüßung, Schlagzeilenblock,
   Kurzmeldungsbündel, Wetterüberleitung, Outro werden aus profilierten
   Varianten gesetzt, nicht vom Modell erfunden.

**Output:**

- `script_broadcast.json` (neues Schema `models/script_schema.py`)
- `script.broadcast.tr.md` — reine gesprochene Narration in Sendereihenfolge;
  wird die **neue kanonische Narration** für Downstream.
- `script_omissions.json` — Auslassungsprotokoll (keine Story verschwindet
  stillschweigend).

### 3.3 Narrationskette

`_downstream_narration()` bekommt eine neue höchste Priorität:

```
review/script.broadcast.reviewed.tr.md  →  script.broadcast.tr.md  →  (bisherige Kette)
```

Damit gilt:

- Translation-QA hasht weiterhin die **Übersetzung**, bevor `script` läuft.
- Script-QA erdet das **Sendungsskript** gegen die freigegebene Übersetzung
  (keine neuen Zahlen/Namen/Zitate).
- Der Narration-Lock in `chapterize` sperrt gegen das Sendungsskript — also
  gegen genau das, was gesprochen wird. Die bestehende Invariante bleibt
  strukturell erhalten, nur ihr Anker wandert eine Stufe nach hinten.

### 3.4 Chapterize deterministisch bei vorhandenem Skript

Existiert `script_broadcast.json`, baut `chapterizer` die Kapitel **ohne
LLM-Aufruf** direkt aus dem Skript:

- 1 Skript-Story = 1 Kapitel (Reihenfolge = Sendereihenfolge)
- `narration.text` = konkatenierte Segmenttexte
- `metadata.speaker_segments` = Liste `{role, purpose, text, ...}` für TTS
- `overlays` = ein `title`-Overlay aus `display_headline` + `display_summary`
- `visual` aus der bestehenden Heuristik (Wetter → `diagram`, sonst `b_roll`)

Vorteile: Narration-Lock ist per Konstruktion erfüllt, keine LLM-Kosten,
Sprechersegmente überleben die Kapitelbildung.

### 3.5 Overlays mit Titel und Kurztext

`models/chapter_schema.py`:

```python
class Overlay(BaseModel):
    ...
    subtext: str | None            # display_summary
    priority: int = 0              # overlay_priority
```

`Chapter` bekommt `display_headline` und `display_summary`.
`renderer._chapter_to_overlay_specs()` erzeugt für `subtext` einen zweiten
`OverlaySpec` mit kleinerer Schrift, direkt unter der Überschrift, innerhalb der
mobilen Safe Area und oberhalb der Untertitelzone.

### 3.6 Multi-Voice-TTS

Profil:

```yaml
tts:
  voices:
    anchor_female: {provider: elevenlabs, voice_id: "...", model: eleven_turbo_v2_5, ...}
    reporter_male: {provider: elevenlabs, voice_id: "Q2IX97JeHBY3vNGzgM5s", ...}
  default_role: reporter_male
```

`core/tts.py`:

- Kennt das Kapitel `metadata.speaker_segments`. Ohne Segmente → bisheriges
  Verhalten (eine Stimme, ein File) — vollständig rückwärtskompatibel.
- Mit Segmenten: pro Segment eine MP3 unter `tts/<chapter>/seg<NN>_<role>.mp3`,
  danach Konkatenation mit kurzer Pause beim Sprecherwechsel zur
  Kapitel-MP3 (bestehender Pfad, damit Render unverändert bleibt).
- Manifest speichert pro Segment: `story_id`, `role`, `purpose`, `voice_id`,
  `model`, `text_hash`, `file_path`, `duration_seconds`, `cost_usd`.
- Idempotenz-Hash pro Segment enthält nur die Stimme **seiner** Rolle → eine
  geänderte Anchor-Stimme invalidiert ausschließlich Anchor-Segmente.

### 3.7 Wiederverwendbares Intro-Master-Asset

`core/intro_asset.py`:

- Rendert einmalig ein 1920×1080-Intro (Logo, Programmtitel, Slogan) aus einem
  deterministischen HTML/SVG-Template über den bereits vorhandenen
  Chromium-Renderer + ffmpeg, gemischt mit `intro.mp3`.
- Cache-Key aus Branding + Auflösung + FPS + Templateversion; liegt unter
  `data/assets/<profil>/intro_master_<key>.mp4` und wird von allen Episoden
  wiederverwendet.
- Existiert bereits ein vom Nutzer geliefertes Intro-Video, wird es validiert
  (Auflösung, FPS, Dauer) und bevorzugt.
- Keine generative KI pro Episode.

### 3.8 Script-QA und Dauer-Gate

`core/script_qa.py`, läuft am Ende der `script`-Stage:

Deterministische Prüfungen (kostenlos):

- jede nicht-`omit` Story ist im Skript enthalten
- angeteaserte Schlagzeilen erscheinen später tatsächlich
- Zahlen und Eigennamen des Skripts sind Teilmenge der Quelle
- Anchor-Anteil (Wörter) 40–50 %, `< 35 %` = Major, `< 25 %` = Revision
- geschätzte Gesamtdauer 8:00–10:30, sonst Revision
- Overlay-Texte sind aus dem Story-Text ableitbar
- verbotene Meinungsmuster (`Bence`, `kesinlikle adaletsiz`, `destekliyoruz`, …)

Nach TTS wird die **reale** Dauer gemessen und in
`script_qa.json` nachgetragen; Überschreitung > 10:30 erzeugt ein Major-Finding.
Maximal zwei automatische Revisionen (`max_automatic_revisions: 2`).

### 3.9 Betroffene Dateien

**Neu**

- `btcedu/models/script_schema.py`
- `btcedu/core/story_ranking.py`
- `btcedu/core/scripter.py`
- `btcedu/core/script_qa.py`
- `btcedu/core/branding_guard.py`
- `btcedu/core/intro_asset.py`
- `btcedu/prompts/templates/tagesschau_tr/script_broadcast.md`
- `tests/test_story_ranking.py`, `tests/test_scripter.py`,
  `tests/test_script_qa.py`, `tests/test_branding_guard.py`,
  `tests/test_multivoice_tts.py`, `tests/test_dual_presenter_flow.py`

**Geändert**

- `btcedu/core/pipeline.py` — Stage `script` einhängen, Stale-Kaskade
- `btcedu/core/chapterizer.py` — deterministischer Skript-Pfad, Sanitizer
- `btcedu/core/qa_reviewer.py` — `_downstream_narration` Priorität
- `btcedu/core/tts.py` — Multi-Voice pro Sprechersegment
- `btcedu/core/renderer.py` — zweizeilige Overlays, Intro-Master, Branding-Check
- `btcedu/core/publisher.py` — Branding-Check vor Publish
- `btcedu/models/chapter_schema.py` — `Overlay.subtext`, Chapter-Displayfelder
- `btcedu/models/episode.py` — Status `SCRIPTED`
- `btcedu/config.py` — Defaults für Skript/Voices
- `btcedu/profiles/tagesschau_tr.yaml` — Branding, Skript, Voices
- `btcedu/prompts/templates/tagesschau_tr/chapterize.md` — Attribution raus
- `btcedu/cli.py` — `btcedu script`, `btcedu intro-build`
- `btcedu/web/api.py`, `static/app.js` — Dashboard

### 3.10 Datenmodelländerungen

- `EpisodeStatus.SCRIPTED` (zwischen `ADAPTED` und `CHAPTERIZED`)
- `Overlay.subtext`, `Overlay.priority`
- `Chapter.display_headline`, `Chapter.display_summary`
- `Chapter.metadata["speaker_segments"]` (bestehendes Feld, kein Schemabruch)
- neues Artefakt-Schema `BroadcastScript` in `models/script_schema.py`

Keine SQL-Migration nötig: Alle neuen Felder sind Artefakt-Felder, der neue
Status ist ein String-Wert in der bestehenden Spalte.

### 3.11 Profiländerungen (tagesschau_tr)

```yaml
branding: {...}
stage_config:
  script:
    enabled: true
    target_total_seconds: 540
    min_total_seconds: 480
    max_total_seconds: 630
    anchor_share_target: [0.40, 0.50]
    max_automatic_revisions: 2
    provider: copilot_cli
    model: gpt-5.6-sol
  tts:
    voices: {anchor_female: {...}, reporter_male: {...}}
  render:
    outro_text: "Almanya'nın nabzı burada atıyor."
```

### 3.12 Rendering

- Zweizeiliges Story-Overlay (Headline + Summary), Safe-Area 8 %, Umbruch nach
  ~42 Zeichen, Untertitelzone bleibt frei.
- Intro-Master wird vorangestellt statt pro Episode gebaut.
- Outro-Karte zeigt Programmtitel + Slogan, keine Quelle.

### 3.13 Wetter

Bestehende Kette bleibt. Änderungen:

- Szenenplanung gruppiert Regionen **pro Tag**, damit nicht „Sağanak yağışlı,
  Güneşli" für dieselbe Region entsteht.
- Fallback-Stufen explizit: volle Karte → reduzierte Karte → gebrandete
  Hava-Durumu-Seite mit bestätigten Textausschnitten → generisches Branding.
- Moderatorin leitet zum Wetter über (Teil der `speaker_sequence`).

### 3.14 QA-Integration

Script-QA-Findings verwenden das bestehende `QAFinding`-Schema und landen in
`script_qa.json`. Critical-Findings blockieren `publish` über den bestehenden
`final_review`-Pfad an `review_gate_3`.

### 3.15 Tests

Alle in Abschnitt 20 des Auftrags genannten Punkte werden abgedeckt; sämtliche
externen Provider (ElevenLabs, LLM, Open-Meteo, ffmpeg/Chromium) werden gemockt.

### 3.16 Kostenwirkung

- `script`: ein LLM-Aufruf pro Episode (~10–15k Input-Token) ≈ $0.02–0.05.
- `chapterize`: entfällt als LLM-Aufruf, wenn ein Skript vorliegt → **Ersparnis**
  in ähnlicher Größenordnung.
- TTS: gleiche Zeichenmenge, aber kürzere Sendung → tendenziell günstiger.
- Intro: einmalig statt pro Episode.
- Netto: annähernd kostenneutral bis leicht günstiger. Der bestehende Cost Guard
  greift unverändert vor jedem bezahlten Aufruf.

### 3.17 Rückwärtskompatibilität

- `pipeline_version=1` unberührt (`_get_stages` filtert v2-Stages weiterhin).
- Profile ohne `script.enabled` behalten exakt die bisherige Kette.
- Ohne `tts.voices` bleibt TTS einstimmig.
- Kapitel ohne `speaker_segments` werden wie bisher verarbeitet.
- Bereits erzeugte Episoden bleiben lesbar; alte Attributions-Overlays werden
  beim Rendern still gefiltert statt zu einem harten Fehler zu führen.

### 3.18 Deployment und Rollback

- Deployment über `./run.sh` (git pull → pip → migrate → restart).
- Rollback: `stage_config.script.enabled: false` und `tts.voices` entfernen —
  die Pipeline fällt ohne Codeänderung auf das alte Verhalten zurück.
- Kein Datenverlust: alle neuen Artefakte sind zusätzliche Dateien.

---

## 4. Reihenfolge der Umsetzung

1. Branding + Quellenentfernung + Guard (kleinster Nutzen/Risiko-Quotient)
2. Schemas (`script_schema`, Overlay-Felder, Status)
3. Story-Ranking (deterministisch, testbar)
4. Script-Stage + Prompt
5. Script-QA + Dauer-Gate
6. Narrationskette + deterministisches Chapterize
7. Multi-Voice-TTS
8. Overlays + Intro-Master im Renderer
9. Pipeline-Integration + Stale-Kaskade
10. Dashboard
11. Tests, Preview, Dokumentation
