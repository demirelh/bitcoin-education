# KI-Nachrichtensprecher Integration — Machbarkeitsplan

## Kontext

btcedu generiert türkische YouTube-Videos aus deutschen Bitcoin-Podcasts. Aktuell: Standbilder + Voice-Over (ElevenLabs TTS). Das `TALKING_HEAD` Visual-Type existiert bereits im Schema (`chapter_schema.py:14`), wird aber nur als grauer Platzhalter gerendert (`image_generator.py:286`). Ziel: Realistischer KI-Nachrichtensprecher mit Lippensynchronisation.

---

## 1. Empfohlener Ansatz: D-ID API

| Kriterium | D-ID | HeyGen | Open-Source (SadTalker/MuseTalk) |
|-----------|------|--------|----------------------------------|
| Kosten/Minute | ~$0.50–0.80 | ~$0.50–1.00 (Standard), $3 (Avatar IV) | Cloud-GPU: ~$0.30–0.50 + Engineering |
| API-Einfachheit | Foto + Audio → Video (REST) | Ähnlich, komplexere Tiers | GPU-Instanz managen, kein SaaS |
| Qualität (1080p) | Gut, natürliche Kopfbewegung | Vergleichbar | Wav2Lip unscharf, MuseTalk gut |
| Raspberry Pi | Cloud-only, kein GPU nötig | Cloud-only | CUDA-GPU erforderlich |
| Konsistenz | Gleiches Foto = gleicher Anchor | Ähnlich | Modellabhängig |
| Limit | 5 min pro Video | 30 min pro Video | Keins |

**Empfehlung: D-ID** — Bester Kompromiss aus Kosten, Qualität und Einfachheit. REST-API (Foto + Audio → MP4) passt perfekt zum bestehenden Stage-Pattern. Kein GPU nötig.

**5-Minuten-Limit-Mitigation:** D-ID erlaubt max. 5 min pro API-Call. Da Kapitel typisch 30–90 Sekunden sind, ist das kein Problem bei per-Kapitel-Generierung. Für Sonderfälle mit längeren Segmenten (>5 min) implementiert die `DIDService` automatisches Audio-Chunking: Audio an Satzgrenzen splitten, pro Chunk einen D-ID-Call, resultierende Videos per ffmpeg `concat` zusammenfügen (analoges Pattern wie ElevenLabs Text-Chunking in `elevenlabs_service.py`).

**Warum nicht HeyGen:** Avatar IV ($3/min) zu teuer, Standard-Qualität nicht besser als D-ID.
**Warum nicht Open-Source:** Braucht CUDA-GPU + Cloud-Infrastruktur (RunPod). Engineering-Overhead übersteigt D-ID-Kosten. Kann später als Fallback integriert werden (Protocol-Pattern macht das trivial).

---

## 2. Kostenvergleich — Typische 10-Minuten-Episode

~3 Minuten Anchor (Intro, Übergänge, Fazit), ~7 Minuten B-Roll/Diagramme.

| Komponente | Kosten/Episode | Notizen |
|-----------|---------------|---------|
| D-ID Anchor (3 min) | $1.50–2.40 | Pro-Plan, 15-Sek-Intervalle |
| ElevenLabs TTS (10 min) | ~$0.45 | Alle Kapitel |
| Stock Images (Pexels) | $0.00 | Gratis-API |
| Claude LLM (alle Stages) | ~$0.50 | Correct+Translate+Adapt+Chapterize |
| **Gesamt** | **~$2.45–3.35** | Unter $15 max_episode_cost_usd |

Anchor-Kosten allein: ~$0.50–0.80/Minute — **Budget <$1/min eingehalten**.
`max_episode_cost_usd` wird von $10 auf **$15** erhöht (Puffer für Anchor + Retries).

---

## 3. Architektur-Design

### 3.1 Neues Pipeline-Stage: `ANCHOR_GENERATED`

```
... → TTS_DONE → ANCHOR_GENERATED → RENDERED → [review_gate_3] → ...
```

Logik: Anchor braucht TTS-Audio als Input (Lippensync), produziert Videos die der Render-Stage konsumiert.

### 3.2 Änderungen an bestehenden Dateien

**`btcedu/models/episode.py`:**
- `EpisodeStatus.ANCHOR_GENERATED = "anchor_generated"` hinzufügen
- `PipelineStage.ANCHORGEN = "anchorgen"` hinzufügen

**`btcedu/core/pipeline.py`:**
- `_STATUS_ORDER`: `EpisodeStatus.ANCHOR_GENERATED: 15.5` (zwischen TTS_DONE=15, RENDERED=16)
- `_V2_STAGES`: `("anchorgen", EpisodeStatus.TTS_DONE)` zwischen `tts` und `render`
- `render` required status: `TTS_DONE` → `ANCHOR_GENERATED`
- `_V2_ONLY_STAGES`: `"anchorgen"` hinzufügen
- `_run_stage()`: neuer `elif stage_name == "anchorgen":` Block

**`btcedu/config.py`:**
```python
did_api_key: str = ""
did_source_image_path: str = "data/anchor/default.png"  # Fallback-Foto
did_source_image_url: str = ""  # Alternativ: Pre-uploaded URL
anchor_provider: str = "d-id"
anchor_enabled: bool = False  # Feature-Flag, default off
anchor_max_chunk_seconds: int = 270  # Max Audio-Chunk für D-ID (< 5 min)
max_episode_cost_usd: float = 15.0  # Erhöht von $10 auf $15
```

**Profil-spezifische Anchor-Fotos** (in `btcedu/profiles/{profile}/config.yaml`):
```yaml
# Beispiel: profiles/bitcoin_podcast/config.yaml
anchor:
  source_image: "data/anchor/bitcoin_anchor.png"
  source_image_url: ""  # Optional: Pre-uploaded D-ID URL
  expression: "serious"  # D-ID expression parameter
```
Die Stage-Funktion lädt das Profil via `get_registry(settings)` und verwendet das profilspezifische Foto. Fallback: `settings.did_source_image_path`.

**`btcedu/core/renderer.py`:**
- `render_video()`: Anchor-Manifest laden (neben Image- und TTS-Manifest)
- `_resolve_chapter_media()`: Wenn Anchor-Video existiert → `asset_type="video"` + Anchor-Pfad zurückgeben (nutzt bestehende `create_video_segment()` Logik, Zeile 283)

### 3.3 Neue Dateien

**`btcedu/services/anchor_service.py`** — Protocol + D-ID Implementation:
```python
class AnchorService(Protocol):
    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse: ...

class DIDService:
    # POST /talks (Foto + Audio) → Poll bis done → Download Video
    # Automatisches Audio-Chunking bei >anchor_max_chunk_seconds (270s)
    # Chunk-Videos per ffmpeg concat zusammenfügen
    ...

class DryRunAnchorService:
    # Platzhalter für dry_run Modus
    ...
```

Folgt dem Pattern von `elevenlabs_service.py`: Dataclass Request/Response, HTTP via `requests`, Retry mit Backoff.

**`btcedu/core/anchor_generator.py`** — Stage-Funktion:
```python
def generate_anchors(session, episode_id, settings, force=False) -> AnchorResult:
    # 1. Guard: pipeline_version == 2, status == TTS_DONE
    # 2. Wenn anchor_enabled == False → No-op, Status setzen, return
    # 3. chapters.json laden, nach visual.type == TALKING_HEAD filtern
    # 4. Wenn keine TALKING_HEAD-Kapitel → No-op, Status setzen, return
    # 5. Profil laden → anchor.source_image (Fallback: settings.did_source_image_path)
    # 6. TTS-Manifest laden für Audio-Dateien
    # 7. Content-Hash (Chapter-IDs + Audio-Hashes + Source-Image-Hash)
    # 8. Idempotenz-Check
    # 9. Cost Guard (max_episode_cost_usd = $15)
    # 10. Pro TALKING_HEAD-Kapitel: DIDService.generate_anchor_video()
    #     (mit Auto-Chunking bei Audio > 270s)
    # 11. anchor/manifest.json + provenance schreiben
    # 12. MediaAsset + ContentArtifact Records
    # 13. episode.status = ANCHOR_GENERATED
    # 14. render .stale Marker setzen
```

Output-Verzeichnis:
```
outputs/{episode_id}/
├── anchor/
│   ├── manifest.json          (AnchorEntry pro Kapitel)
│   ├── {chapter_id}.mp4       (Anchor-Video pro Kapitel)
│   └── anchor_provenance.json
```

### 3.4 TALKING_HEAD Verbindung

Bereits verdrahtet: Der Chapterizer (LLM) setzt `visual.type = "talking_head"` für Intro/Übergänge/Fazit. Der Image-Generator erstellt Platzhalter (Zeile 286). Die neue Anchor-Stage ersetzt diese Platzhalter durch echte Videos. Der Renderer erkennt `asset_type="video"` und nutzt `create_video_segment()` (bereits implementiert, Zeile 283).

---

## 4. MVP-Definition

**Im MVP:**
1. `btcedu/services/anchor_service.py` — Protocol + DIDService + DryRunAnchorService
2. `btcedu/core/anchor_generator.py` — Stage-Funktion nach `tts.py`-Muster
3. Config: `did_api_key`, `did_source_image_path`, `anchor_enabled`, `max_episode_cost_usd=15`
3b. Profil-Config: `anchor.source_image` pro Profil (Fallback auf globale Config)
4. Pipeline: `ANCHOR_GENERATED` Status, `anchorgen` Stage, `_run_stage()` Dispatch
5. Renderer: Anchor-Manifest laden, Video-Segments für TALKING_HEAD
6. Migration: Trivial (SQLite speichert Enums als Strings)
7. Tests: `test_anchor_service.py`, `test_anchor_generator.py`, Renderer-Update-Tests

**NICHT im MVP:**
- Keine Hybrid-Idle-Clips
- Kein HeyGen-Fallback
- Kein Open-Source-Fallback
- Kein Web-Dashboard für Anchor-Settings

---

## 5. Migrations-Pfad

**Phase 1: Feature-Flag (keine Breaking Changes)**
- `ANCHOR_GENERATED` Status + `anchorgen` Stage hinzufügen
- `anchor_enabled = False` (Default): Stage ist No-op, setzt nur Status weiter
- Renderer prüft Anchor-Manifest: wenn nicht vorhanden → Platzhalter wie bisher
- Alle bestehenden Episoden funktionieren unverändert

**Phase 2: Aktivierung**
- `.env`: `anchor_enabled=True`, `did_api_key=...`
- Anchor-Foto unter `data/anchor/source.png` platzieren
- Neue Episoden mit TALKING_HEAD-Kapiteln generieren Anchor-Videos

**Phase 3: Backfill (optional)**
- `btcedu render --force --episode <id>` für bestehende Episoden

---

## 6. Langfristige Vision

| Phase | Feature | Vorteil |
|-------|---------|---------|
| A | Hybrid: Pre-rendered Idle-Clips + API nur für Sprech-Segmente | 30-40% weniger API-Kosten |
| B | Open-Source Fallback (MuseTalk auf RunPod Serverless) | Kostenreduktion, Unabhängigkeit |
| C | Web-Dashboard: Anchor-Foto-Upload + Preview (5-Sek-Clips in Review Gates) | Bessere QA |
| D | Green-Screen Compositing (Anchor über B-Roll, PiP) | Professionelleres Format |

---

## 7. Implementierungsreihenfolge

1. `btcedu/models/episode.py` — Enums erweitern
2. `btcedu/config.py` — Config-Felder
3. `btcedu/services/anchor_service.py` — Protocol + D-ID + DryRun (NEU)
4. `btcedu/core/anchor_generator.py` — Stage-Funktion (NEU)
5. `btcedu/core/pipeline.py` — Stage-Registrierung
6. `btcedu/core/renderer.py` — Anchor-Manifest Integration
7. `btcedu/migrations/__init__.py` — Migration (trivial)
8. `tests/test_anchor_service.py` + `tests/test_anchor_generator.py` (NEU) + `tests/test_renderer.py` Update

## 8. Verifikation

- `pytest tests/test_anchor_service.py -x -q` — Service-Tests (D-ID API gemockt)
- `pytest tests/test_anchor_generator.py -x -q` — Stage-Tests
- `pytest tests/test_renderer.py -x -q` — Renderer mit Anchor-Videos
- `pytest` — Volle Suite (867+ Tests), kein Regression
- `ruff check btcedu/ tests/` — Lint-Check
- Manuell: `anchor_enabled=False` → Pipeline durchlaufen, kein Unterschied zu vorher
- Manuell: `anchor_enabled=True` + D-ID Key → TALKING_HEAD-Kapitel erhalten Video statt Platzhalter
