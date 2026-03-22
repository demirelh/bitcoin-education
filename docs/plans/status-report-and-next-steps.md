# btcedu — Status Report & Fehlende Implementierungen

**Stand: 2026-03-22** | **Tests auf main: 1028 bestanden** | **Baseline: 867**

---

## Phasen-Übersicht

| Phase | Status | Branch | Tests | In Main? |
|-------|--------|--------|-------|----------|
| 1: Content-Bereinigung | **Implementiert** | PR #32 gemerged | +161 Tests | Ja |
| 2: Frame-Extraktion | **Teilweise implementiert** | PR #32 gemerged | +Tests | Ja |
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

### Phase 2: Frame-Extraktion — TEILWEISE
**Implementiert:**
- `btcedu/core/frame_extractor.py` — Stage-Modul (Orchestrierung)
- `btcedu/services/frame_extraction_service.py` — ffmpeg Keyframe-Extraktion
- `btcedu/services/download_service.py` — Erweitert um Video-Download (nicht nur Audio)
- `docs/plans/frame-extraction-pipeline.md` — Plan
- Tests: `test_frame_extractor.py`, `test_frame_extraction_service.py`, `test_ffmpeg_frame_extraction.py`

**FEHLEND:**
1. **Sprecher-Erkennung/Entfernung** — Keine Face-Detection, kein Inpainting, kein Crop
2. **KI-Stil-Modifikation** — Keine Copyright-Schutz-Transformation
3. **Pipeline-Integration** — `extract_frames` ist NICHT als Stage in `pipeline.py` registriert
4. **Chapter-Zuordnung** — Frames werden extrahiert aber nicht automatisch Chapters zugeordnet
5. **Fallback-Logik** — Kein Fallback auf Stock-Images wenn Frame-Extraktion fehlschlägt
6. **Renderer-Integration** — Renderer nutzt die extrahierten Frames noch nicht

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
4. **Phase 2 vervollständigen** — Frame-Extraktion Pipeline-Integration + Sprecher-Erkennung
5. **Phase 5 implementieren** — KI-Sprecher (D-ID Service + Stage)

---

## Prompts für fehlende Implementierungen

### Prompt A: Phase 2 vervollständigen — Frame-Extraktion Pipeline-Integration

**Verwendung:** Opus zum Planen, dann Sonnet zum Implementieren

#### Planungs-Prompt (Opus):

```
Du bist ein Senior Software Engineer. Vervollständige die Frame-Extraktion für das btcedu-Projekt.

AKTUELLER STAND:
- btcedu/core/frame_extractor.py EXISTIERT — Stage-Orchestrierung für Keyframe-Extraktion
- btcedu/services/frame_extraction_service.py EXISTIERT — ffmpeg-basierte Scene-Detection
- btcedu/services/download_service.py ERWEITERT — kann jetzt auch Video herunterladen
- Tests existieren: test_frame_extractor.py, test_frame_extraction_service.py, test_ffmpeg_frame_extraction.py
- PROBLEM: Die Frame-Extraktion ist NICHT in die Pipeline integriert!

WAS FEHLT:
1. Pipeline-Integration: extract_frames ist KEIN registriertes Stage in pipeline.py
   - Neuer EpisodeStatus: FRAMES_EXTRACTED (zwischen IMAGES_GENERATED und TTS_DONE, oder als Alternative zu IMAGES_GENERATED)
   - Neuer PipelineStage in _V2_STAGES
   - _run_stage() Dispatch

2. Sprecher-Erkennung: Tagesschau-Frames mit Nachrichtensprecher identifizieren
   - Option A: Einfache Heuristik — Frames mit Gesicht im unteren Drittel = Sprecher
   - Option B: Face-Detection mit mediapipe (leichtgewichtig, läuft auf ARM)
   - Option C: LLM-basiert — Claude Vision API auf Keyframes anwenden ("Is this a news anchor shot?")
   EMPFEHLUNG: Option C (Claude Vision) — keine lokale Dependency, höchste Genauigkeit

3. Sprecher-Entfernung/Modifikation:
   - Sprecher-Frames: Oberen Teil croppen (Grafik/Karte hinter dem Sprecher)
   - Nicht-Sprecher-Frames: Leichte Modifikation für Copyright (Farbverschiebung + Crop)
   - KEIN Inpainting nötig — zu teuer und komplex für MVP

4. Chapter-Zuordnung:
   - Frames haben Zeitstempel (aus Scene-Detection)
   - Chapters haben estimated_duration_seconds und Story-Zuordnung
   - Matching: Frame-Zeitstempel → nächstes Chapter
   - Output: chapters.json erweitern mit frame_path pro Chapter

5. Renderer-Integration:
   - renderer.py: Wenn extracted_frame existiert → dieses verwenden statt DALL-E/Stock
   - Fallback: Wenn kein Frame → bestehende image_generator.py / stock_images.py
   - Priorität: extracted_frame > stock_image > generated_image

6. Profil-Konfiguration:
   - tagesschau_tr.yaml: frame_extraction: { enabled: true, prefer_over_stock: true }
   - bitcoin_podcast.yaml: frame_extraction: { enabled: false } (Podcast hat kein Video)

BESTEHENDE DATEIEN (lies diese):
- btcedu/core/frame_extractor.py — aktueller Stand
- btcedu/services/frame_extraction_service.py — aktueller Stand
- btcedu/core/pipeline.py — Stage-Registrierung verstehen
- btcedu/core/image_generator.py — Wie DALL-E Stage funktioniert
- btcedu/core/stock_images.py — Wie Stock-Image Stage funktioniert
- btcedu/core/renderer.py — Wie Renderer Bilder konsumiert
- btcedu/models/episode.py — EpisodeStatus Enum
- btcedu/models/media_asset.py — MediaAsset Model
- btcedu/profiles/tagesschau_tr.yaml — Profil-Konfiguration
- docs/plans/frame-extraction-pipeline.md — Ursprünglicher Plan

CONSTRAINTS:
- Raspberry Pi (ARM, kein CUDA) — keine schweren lokalen Modelle
- Claude Vision API für Sprecher-Erkennung ist OK (Budget: ~$0.01 pro Bild)
- Pipeline muss idempotent bleiben (SHA-256 Hash + Provenance)
- Bestehende Tests dürfen NICHT brechen (aktuell 1028 auf main)
- Frame-Extraktion soll OPTIONAL sein (Feature-Flag im Profil)

Erstelle einen detaillierten Implementierungsplan mit:
- Genaue Dateiliste (bestehend + neu)
- Pipeline-Stage-Design (wo einfügen, welche Reihenfolge)
- Sprecher-Erkennung via Claude Vision (Prompt-Design)
- Frame-Crop-Strategie (kein Inpainting)
- Chapter-Matching-Algorithmus
- Renderer-Fallback-Logik
- Test-Strategie
```

#### Implementierungs-Prompt (Sonnet):

```
Implementiere die Frame-Extraktion Pipeline-Integration für btcedu gemäß folgendem Plan:

[PLAN VON OPUS HIER EINFÜGEN]

REGELN:
- Folge dem bestehenden Stage-Pattern (siehe btcedu/core/CLAUDE.md)
- frame_extractor.py und frame_extraction_service.py ERWEITERN, nicht ersetzen
- Pipeline-Integration: Neues Stage registrieren in pipeline.py
- Renderer: Fallback-Logik (extracted_frame > stock > generated)
- Profil-basiert: tagesschau_tr aktiviert, bitcoin_podcast deaktiviert
- Alle bestehenden 1028 Tests müssen weiterhin passen
- Neue Tests für alle neuen Funktionen
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
