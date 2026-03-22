# btcedu — Status Report & Fehlende Implementierungen

**Stand: 2026-03-22** | **Tests auf main: 1028 bestanden** | **Baseline: 867**

---

## Phasen-Übersicht

| Phase | Status | Branch | Tests | In Main? |
|-------|--------|--------|-------|----------|
| 1: Content-Bereinigung | **Implementiert** | PR #32 gemerged | +161 Tests | Ja |
| 2: Frame-Extraktion | **~95% implementiert** | PR #32 gemerged | +42 Tests | Ja |
| 3: Web-UI Modernisierung | **Implementiert (3a-3d)** | PR #33 gemerged | +Tests | Ja |
| 4: Videoqualität | **Implementiert, nicht gemerged** | `claude/enhance-video-quality-nPSTa` | 1063 (alle grün) | **Nein** |
| 5: KI-Sprecher | **Nur Plan, keine Implementierung** | `claude/ai-news-anchor-plan-As4ov` | nur Plan-Dokument | **Nein** |
| 6: Pipeline-Stabilität | **Implementiert, nicht gemerged** | `claude/enhance-pipeline-stability-ZoQy4` | 1080 (alle grün) | **Nein** |

---

## Detailbewertung pro Phase

### Phase 1: Content-Bereinigung — ERLEDIGT
- `btcedu/core/moderator_patterns.py` — Regex-basierte Erkennung deutscher Moderationsfloskeln
- `btcedu/prompts/templates/tagesschau_tr/translate_intro_outro.md` — Dedizierter Prompt für Intro/Outro-Bereinigung
- `btcedu/profiles/tagesschau_tr.yaml` — `clean_moderator: true` Konfiguration
- Tests: `test_moderator_patterns.py`, `test_translator_intro_outro.py` (289 Zeilen)

**Fehlend:** Nichts — vollständig implementiert.

### Phase 2: Frame-Extraktion — ~95% ERLEDIGT
**Implementiert (vollständig):**
- `btcedu/core/frame_extractor.py` — Stage-Modul (476 Zeilen, vollständige Orchestrierung)
- `btcedu/services/frame_extraction_service.py` — OpenCV Haar-Cascade Sprecher-Erkennung + Positionsheuristik
- `btcedu/services/ffmpeg_service.py` — Keyframe-Extraktion (Scene-Detection), Crop, Style-Filter (3 Presets)
- `btcedu/services/download_service.py` — Erweitert um Video-Download (yt-dlp, bis 720p)
- `btcedu/core/pipeline.py` — `frameextract` Stage registriert (Position 13, nach chapterize, vor imagegen)
- `btcedu/core/stock_images.py` — Fallback-Logik: Extracted Frame > Stock > DALL-E > Placeholder
- `btcedu/models/episode.py` — `FRAMES_EXTRACTED` Status + `FRAMEEXTRACT` Stage
- `btcedu/config.py` — 9 neue Settings (feature-flagged, default: off)
- Tests: 42 neue Tests über 3 Dateien (907 Zeilen)
- `docs/plans/frame-extraction-pipeline.md` — Plan

**Fehlend (non-critical, ~5%):**
1. **DALL-E Edit API Style-Provider** — Config-Option existiert (`frame_extract_style_provider: "dalle_edit"`), aber Code-Pfad nicht implementiert (nur ffmpeg funktioniert)
2. **`sketch` Style-Preset** — Nur 3 von 4 geplanten Presets (news_recolor, warm_tint, cool_tint)
3. **Alternative-Frames im Manifest** — Werden berechnet aber nicht vollständig persistiert

### Phase 3: Web-UI Modernisierung — ERLEDIGT (3a-3d)
**Implementiert (Commit e43f213 + Fixes):**
- v2-Status-Filter im Dropdown
- Pipeline-Stepper für v2-Stages
- Inline Video-Vorschau im Detail-Panel
- Side-by-Side Diff für Reviews
- Audio-Preview neben Text
- Batch-Approve für Reviews
- Kosten-Trend-Chart (Canvas-basiert)
- Pipeline-Durchsatz-Metriken
- SSE (Server-Sent Events) für Live-Updates
- Fehlerrate pro Stage

**Fehlend:** Nichts Kritisches — alle 4 Sub-Phasen implementiert.

### Phase 4: Videoqualität — IMPLEMENTIERT, NICHT GEMERGED
**Branch:** `claude/enhance-video-quality-nPSTa` (1 Commit, 1361 Zeilen, 1063 Tests grün)

**Implementiert:**
- Ken Burns Effekt (zoompan filter, konfigurierbar)
- Professionelle Lower-Thirds (Gradient, animiert)
- Nachrichten-Ticker (scrollend)
- Intro/Outro-Generation
- Farbkorrektur (Entsättigung, Blau-Shift)
- Alles einzeln ein/ausschaltbar via `config.py`
- 669 Zeilen Tests (`test_video_enhancements.py`)

**Fehlend:** Nur PR erstellen und mergen.

### Phase 5: KI-Sprecher — NUR PLAN
**Branch:** `claude/ai-news-anchor-plan-As4ov` (1 Commit, 217 Zeilen Plan-Dokument)

**Plan vorhanden** (`docs/plans/ai-news-anchor.md`):
- Empfehlung: D-ID API
- Kostenanalyse: ~$2.45-3.35 pro Episode
- Architektur: Neues Stage `ANCHOR_GENERATED` nach TTS_DONE
- MVP-Definition: 7 Dateien
- Migrations-Pfad: Feature-Flag basiert

**KOMPLETT FEHLEND — Keine Implementierung:**
1. `btcedu/services/anchor_service.py` — Protocol + DIDService
2. `btcedu/core/anchor_generator.py` — Stage-Funktion
3. `btcedu/models/episode.py` — `ANCHOR_GENERATED` Status
4. `btcedu/core/pipeline.py` — Stage-Registrierung
5. `btcedu/core/renderer.py` — Anchor-Video Integration
6. `btcedu/config.py` — D-ID Config-Felder
7. Tests: `test_anchor_service.py`, `test_anchor_generator.py`

### Phase 6: Pipeline-Stabilität — IMPLEMENTIERT, NICHT GEMERGED
**Branch:** `claude/enhance-pipeline-stability-ZoQy4` (1 Commit, 1337 Zeilen, 1080 Tests grün)

**Implementiert:**
- `btcedu/services/retry.py` — Retry-Decorator mit Exponential Backoff
- `btcedu/services/errors.py` — Error-Klassifikation (transient vs permanent, 7 Kategorien)
- `btcedu/models/dead_letter.py` — Dead-Letter-Queue Model
- `btcedu/web/api.py` — `/api/pipeline-health` Endpoint (Stage-Metriken, DLQ, Error-Trends)
- `btcedu/core/pipeline.py` — DLQ-Integration + Retry-Wrapper
- `btcedu/migrations/__init__.py` — Migration für DLQ-Tabelle
- Tests: `test_retry.py` (312 Zeilen), `test_dead_letter.py` (138 Zeilen), `test_pipeline_health.py` (245 Zeilen)

**Fehlend:** Nur PR erstellen und mergen.

---

## Aktionsliste

### Sofort (PRs mergen):
1. **Phase 4 PR erstellen** — `claude/enhance-video-quality-nPSTa` → main
2. **Phase 6 PR erstellen** — `claude/enhance-pipeline-stability-ZoQy4` → main
3. **Phase 5 PR erstellen** — `claude/ai-news-anchor-plan-As4ov` → main (nur Plan-Dokument)

### Implementierung nötig:
4. **Phase 2 Feinschliff** — Frame-Extraktion: fehlende Style-Presets + DALL-E Edit Code-Pfad (optional, ~5%)
5. **Phase 5 implementieren** — KI-Sprecher (D-ID Service + Stage, 0% → 100%)
6. **Phase 3 Analytics** — Throughput-Chart, Error-Rate, Provider-Kosten-Breakdown (~15%)

---

## Prompts für fehlende Implementierungen

### Prompt A: Phase 2 Feinschliff — Frame-Extraktion (optional, niedrige Priorität)

**Verwendung:** Direkt Sonnet (kleiner Scope, kein Opus-Plan nötig)

**Hinweis:** Phase 2 ist ~95% fertig. Pipeline-Integration, Sprecher-Erkennung (OpenCV),
Chapter-Zuordnung, Fallback-Logik — alles implementiert und getestet (42 Tests).

#### Implementierungs-Prompt (Sonnet):

```
Vervollständige die letzten fehlenden Features der Frame-Extraktion in btcedu.

AKTUELLER STAND (~95% fertig):
- frameextract Stage ist registriert in pipeline.py (Position 13)
- OpenCV Haar-Cascade Sprecher-Erkennung funktioniert
- Smart-Crop der Sprecher-Region funktioniert
- 3 Style-Presets funktionieren (news_recolor, warm_tint, cool_tint)
- Fallback: Extracted Frame > Stock > DALL-E > Placeholder
- 42 Tests, alle grün

FEHLENDE FEATURES (3 kleine Aufgaben):

1. `sketch` Style-Preset hinzufügen:
   - In btcedu/services/ffmpeg_service.py _STYLE_FILTER_PRESETS dict
   - Edge-Detection + reduzierte Sättigung (edgedetect + eq Filter)
   - Test in tests/test_ffmpeg_frame_extraction.py ergänzen

2. DALL-E Edit API Code-Pfad (optional):
   - Config frame_extract_style_provider hat "dalle_edit" Option
   - Aber frame_extractor.py ruft nur apply_style_filter() (ffmpeg) auf
   - Wenn style_provider == "dalle_edit": DALL-E Edit API verwenden
   - Service: btcedu/services/image_gen_service.py hat bereits DALL-E Calls
   - Cost Guard beachten (max_episode_cost_usd)
   - Tests mit gemockter API

3. Alternative-Frames im Manifest persistieren:
   - frame_extractor.py berechnet alternatives = ranked[1:4]
   - Aber schreibt sie nicht ins manifest.json
   - Feld alternative_frames: [] in chapter_assignments ergänzen

REGELN:
- Bestehende 1028 Tests dürfen NICHT brechen
- Ruff-konform (line-length 100, select E/W/F/I/UP)
- Nach Implementierung: pytest -x -q ausführen
```

---

### Prompt B: Phase 5 implementieren — KI-Nachrichtensprecher (D-ID)

**Verwendung:** Direkt Sonnet zum Implementieren (Plan liegt bereits vor in docs/plans/ai-news-anchor.md)

#### Implementierungs-Prompt (Sonnet):

```
Implementiere den KI-Nachrichtensprecher für btcedu gemäß dem bestehenden Plan in docs/plans/ai-news-anchor.md.

ZUSAMMENFASSUNG DES PLANS:
- Neues Pipeline-Stage: ANCHOR_GENERATED (nach TTS_DONE, vor RENDERED)
- D-ID API: Foto + TTS-Audio → Talking-Head-Video
- Feature-Flag: anchor_enabled (default: false)
- Nur für Chapters mit visual.type == TALKING_HEAD
- Auto-Chunking bei Audio > 270s (D-ID Limit: 5 min)

ZU ERSTELLENDE DATEIEN:
1. btcedu/services/anchor_service.py — NEU
   - AnchorService Protocol
   - DIDService: POST /talks → Poll → Download
   - DryRunAnchorService: Platzhalter für Tests
   - AnchorRequest/AnchorResponse Dataclasses
   - Audio-Chunking bei > anchor_max_chunk_seconds
   - Folge Pattern von elevenlabs_service.py

2. btcedu/core/anchor_generator.py — NEU
   - generate_anchors(session, episode_id, settings, force=False) -> AnchorResult
   - Guard: pipeline_version == 2, status == TTS_DONE
   - No-op wenn anchor_enabled == False (Status trotzdem setzen)
   - No-op wenn keine TALKING_HEAD-Kapitel
   - Idempotenz: SHA-256 Hash (Chapter-IDs + Audio-Hashes + Image-Hash)
   - Cost Guard (max_episode_cost_usd)
   - Output: outputs/{episode_id}/anchor/manifest.json + {chapter_id}.mp4
   - Folge Pattern von btcedu/core/tts.py

ZU ÄNDERNDE DATEIEN:
3. btcedu/models/episode.py
   - EpisodeStatus.ANCHOR_GENERATED = "anchor_generated"
   - PipelineStage.ANCHORGEN = "anchorgen"

4. btcedu/core/pipeline.py
   - _STATUS_ORDER: ANCHOR_GENERATED zwischen TTS_DONE und RENDERED
   - _V2_STAGES: ("anchorgen", EpisodeStatus.TTS_DONE) einfügen
   - _V2_ONLY_STAGES: "anchorgen" hinzufügen
   - _run_stage(): elif stage_name == "anchorgen" Block
   - render required status: TTS_DONE → ANCHOR_GENERATED

5. btcedu/config.py
   - did_api_key: str = ""
   - did_source_image_path: str = "data/anchor/default.png"
   - did_source_image_url: str = ""
   - anchor_provider: str = "d-id"
   - anchor_enabled: bool = False
   - anchor_max_chunk_seconds: int = 270
   - max_episode_cost_usd: float = 15.0 (erhöht von 10)

6. btcedu/core/renderer.py
   - Anchor-Manifest laden (neben Image- und TTS-Manifest)
   - Wenn Anchor-Video für Chapter existiert → create_video_segment() verwenden
   - Fallback: Wie bisher (Standbild)

7. btcedu/migrations/__init__.py
   - Keine Schema-Änderung nötig (Enums als Strings in SQLite)

ZU ERSTELLENDE TESTS:
8. tests/test_anchor_service.py — D-ID API gemockt
   - Test: generate_anchor_video mit gemocktem HTTP
   - Test: Audio-Chunking
   - Test: Polling bis fertig
   - Test: DryRunAnchorService

9. tests/test_anchor_generator.py — Stage-Tests
   - Test: anchor_enabled=False → No-op
   - Test: Keine TALKING_HEAD-Kapitel → No-op
   - Test: Normale Generierung
   - Test: Idempotenz (zweiter Aufruf = skip)
   - Test: Cost Guard

REGELN:
- Folge dem bestehenden Stage-Pattern exakt (tts.py als Vorlage)
- D-ID API-Calls via requests (wie in anderen Services)
- Alle 1028+ bestehenden Tests müssen passen
- Feature-Flag: anchor_enabled=False darf NICHTS ändern am Verhalten
- Ruff-konform (line-length 100, select E/W/F/I/UP)
- Nach Implementierung: pytest -x -q ausführen und ruff check btcedu/ tests/
```

---

### Prompt C: Phase 4 & 6 PRs mergen

**Verwendung:** Direkt ausführen (keine Planung nötig)

```
Erstelle Pull Requests für die folgenden Branches und merge sie in main:

1. Branch: claude/enhance-video-quality-nPSTa
   - Titel: "feat: video quality enhancements (Ken Burns, lower thirds, ticker, intro/outro)"
   - 1063 Tests grün, 4 Dateien geändert (+1361 Zeilen)

2. Branch: claude/enhance-pipeline-stability-ZoQy4
   - Titel: "feat: pipeline stability (retry decorator, error classification, DLQ, health monitoring)"
   - 1080 Tests grün, 15 Dateien geändert (+1337 Zeilen)

3. Branch: claude/ai-news-anchor-plan-As4ov
   - Titel: "docs: AI news anchor (D-ID) integration plan"
   - Nur Plan-Dokument (217 Zeilen)

Für jeden Branch:
1. git checkout <branch>
2. git rebase main (falls nötig)
3. gh pr create --base main
4. Warte auf CI
5. gh pr merge (wenn CI grün)

REIHENFOLGE: Phase 6 zuerst (Pipeline-Stabilität), dann Phase 4, dann Phase 5 Plan.
```

---

### Prompt D: Web-UI erweitern für neue Features

**Verwendung:** Nach Phase 4+6 Merge und Phase 2+5 Implementierung

```
Erweitere die btcedu Web-UI um Unterstützung für die neuen Features:

1. Frame-Extraktion Tab:
   - Neuer Tab "Frames" im Episode-Detail
   - Grid-Ansicht der extrahierten Frames
   - Markierung: Sprecher-Frame vs. B-Roll-Frame
   - Chapter-Zuordnung anzeigen
   - Pin/Unpin für manuelle Frame-Auswahl

2. Anchor-Preview:
   - Neuer Tab "Anchor" im Episode-Detail (nur wenn anchor_enabled)
   - Video-Player für generierte Anchor-Videos
   - Pro-Chapter-Vorschau
   - Kostenanzeige pro Anchor-Generierung

3. Pipeline-Health Dashboard:
   - Widget auf der Hauptseite
   - Stage-Erfolgsrate (Balkendiagramm)
   - Error-Trend (7 Tage)
   - Dead-Letter-Queue Anzeige mit Retry-Button
   - Daten von /api/pipeline-health Endpoint

4. Video-Enhancement-Einstellungen:
   - Settings-Modal im Dashboard
   - Toggle für Ken Burns, Lower-Thirds, Ticker, Intro/Outro
   - Vorschau-Thumbnail mit/ohne Enhancement

BESTEHENDE DATEIEN (lies diese):
- btcedu/web/static/app.js — Bestehendes Dashboard
- btcedu/web/static/styles.css — Bestehendes Styling
- btcedu/web/templates/index.html — HTML Shell
- btcedu/web/api.py — API Endpoints

REGELN:
- Vanilla JS, kein Framework
- Dark Theme (bestehende Farbpalette)
- Mobile-Responsive
- SSE-Updates für neue Tabs nutzen
```

---

### Prompt E: Phase 3 Analytics vervollständigen (~15% fehlend)

**Verwendung:** Direkt Sonnet (kleiner Scope)

**Hinweis:** Phase 3 ist ~85% fertig. SSE, v2-Filter, Video-Preview, Batch-Approve,
Cost-Chart — alles implementiert. Fehlend sind erweiterte Analytics-Widgets.

#### Implementierungs-Prompt (Sonnet):

```
Ergänze die fehlenden Analytics-Widgets im btcedu Dashboard.

AKTUELLER STAND (~85% fertig):
- SSE Live-Updates funktionieren (/api/stream)
- Kosten-Chart (Canvas) im Cost-Modal vorhanden
- Status-Summary-Bar mit Episoden-Zählung vorhanden
- Batch-Approve für Reviews funktioniert

FEHLENDE ANALYTICS (3 Widgets):

1. Pipeline-Throughput-Chart:
   - Canvas-basiertes Liniendiagramm (7-Tage-Trend)
   - X-Achse: Tage, Y-Achse: Episoden abgeschlossen
   - Datenquelle: PipelineRun Records gruppiert nach Datum
   - Neuer API-Endpoint: GET /api/metrics/throughput?days=7
   - Widget unter dem Status-Summary-Bar

2. Error-Rate pro Stage:
   - Horizontales Balkendiagramm
   - Pro Stage: Erfolgsrate (grün) vs. Fehlerrate (rot)
   - Datenquelle: PipelineRun success/failed Counts (letzte 7 Tage)
   - Neuer API-Endpoint: GET /api/metrics/error-rates
   - Oder: Bestehender /api/pipeline-health Endpoint nutzen (falls Phase 6 gemerged)

3. API-Kosten-Breakdown nach Provider:
   - Donut/Pie-Chart oder gestapeltes Balkendiagramm
   - Aufschlüsselung: Anthropic (Claude) | OpenAI (DALL-E/Whisper) | ElevenLabs | Pexels (gratis)
   - Datenquelle: PipelineRun cost_usd gruppiert nach Stage → Provider-Mapping
   - Im bestehenden Cost-Modal ergänzen

BESTEHENDE DATEIEN (lies diese):
- btcedu/web/static/app.js — Bestehendes Dashboard (insb. renderCostChart Funktion)
- btcedu/web/api.py — Bestehende Endpoints
- btcedu/web/static/styles.css — Dark Theme Farben

REGELN:
- Canvas 2D API für Charts (kein Chart.js, kein externes Framework)
- HiDPI-aware (devicePixelRatio)
- Dark Theme Farben: #0d1117 bg, #161b22 surface, #58a6ff accent
- Mobile-Responsive (Charts skalieren)
- Bestehende Endpoints NICHT brechen
- Ruff-konform für Python-Dateien
```
