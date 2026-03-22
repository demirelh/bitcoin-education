# Final E2E Validation: Complete Pipeline Run

Du bist ein Senior DevOps/Pipeline Engineer. Führe einen vollständigen End-to-End
Pipeline-Test für das btcedu-Projekt durch. Alle 6 Phasen sind implementiert
(1172 Tests, lint-clean). Deine Aufgabe: Eine echte Episode durch die komplette
v2-Pipeline laufen lassen und dabei auftretende Fehler sofort fixen.

## Vorbereitung

1. Prüfe die aktuelle Konfiguration:
```bash
# API Keys prüfen
grep -c "api_key" .env 2>/dev/null || echo "Keine .env gefunden"

# Projekt installieren
pip install -e ".[dev,web]"

# Tests bestätigen
pytest -x -q

# Dashboard prüfen (optional)
curl -s localhost:5000/api/health 2>/dev/null || echo "Dashboard nicht aktiv"
```

2. Falls keine .env existiert oder Keys fehlen — erstelle eine minimale .env:
```bash
cat > .env << 'ENVEOF'
DRY_RUN=true
PIPELINE_VERSION=2
DATABASE_URL=sqlite:///data/btcedu.db
ENVEOF
```

## Durchführung

### Schritt 1: Datenbank migrieren + Episode erkennen
```bash
btcedu migrate
btcedu detect
```
Wähle die neueste Episode-ID aus der Ausgabe. Falls keine gefunden:
```bash
# Manuell eine Test-Episode anlegen
btcedu add-episode --url "https://www.youtube.com/watch?v=EXAMPLE" --title "Test Episode"
```

### Schritt 2: Pipeline starten
```bash
btcedu run --episode <EPISODE_ID> --verbose
```

Die v2-Pipeline durchläuft 16 Stages:
```
download → transcribe → correct → [review_gate_1] →
translate → adapt → [review_gate_2] → chapterize →
frameextract → imagegen → [review_gate_stock] → tts →
anchorgen → render → [review_gate_3] → publish
```

### Schritt 3: Bei Fehler — Diagnose und Fix

Wenn die Pipeline bei einem Stage stoppt:

1. **Status prüfen:**
```bash
btcedu status <EPISODE_ID>
```

2. **Häufige Fehler und Fixes:**

| Fehler | Ursache | Fix |
|--------|---------|-----|
| `AuthenticationError` | API Key ungültig/fehlend | `.env` prüfen, Key setzen |
| `RateLimitError` | API Limit erreicht | 60s warten, dann: `btcedu run --episode <ID> --force` |
| `CostLimitExceeded` | Episode über $15 Budget | `MAX_EPISODE_COST_USD=25 btcedu run --episode <ID>` |
| `FileNotFoundError: chapters.json` | Upstream-Stage fehlgeschlagen | Vorheriges Stage mit `--force` wiederholen |
| `ValidationError` in chapters | Schema-Fehler im generierten JSON | chapters.json manuell prüfen, ggf. chapterize `--force` |
| `ffmpeg: command not found` | ffmpeg nicht installiert | `apt install ffmpeg` |
| `ffmpeg` Render-Timeout | Segment zu groß | `RENDER_PRESET=ultrafast RENDER_TIMEOUT_SEGMENT=900 btcedu run ...` |
| `ReviewTask pending` | Review Gate blockiert | `btcedu review approve <REVIEW_ID>` (siehe Schritt 4) |
| `pydub` / `audioop` Fehler | Python 3.13 Kompatibilität | `pip install pyaudioop` |
| `sqlite3.OperationalError: database is locked` | Paralleler Zugriff | Anderen Prozess beenden, retry |
| `ConnectionError` (D-ID/ElevenLabs) | Netzwerk-Problem | Retry nach 10s |

3. **Stage einzeln wiederholen:**
```bash
btcedu run --episode <ID> --stage <STAGE_NAME> --force
```
Verfügbare Stage-Namen: download, transcribe, correct, translate, adapt,
chapterize, frameextract, imagegen, tts, anchorgen, render, publish

4. **Wenn ein Code-Bug gefunden wird:**
   - Fix implementieren im entsprechenden Modul
   - `pytest -x -q` ausführen — alle Tests müssen passen
   - `ruff check btcedu/ tests/` — lint-clean
   - Stage mit `--force` wiederholen
   - Weiter mit der Pipeline

### Schritt 4: Review Gates durchlassen

An 3 Stellen (+ optional stock review) wartet die Pipeline auf Approval:
```bash
# Alle offenen Reviews anzeigen:
btcedu review list

# Review Gate 1 (nach correct — Transkript-Korrektur prüfen):
btcedu review approve <REVIEW_ID> --comment "E2E validation"

# Pipeline fortsetzen (läuft bis zum nächsten Gate):
btcedu run --episode <EPISODE_ID>

# Review Gate 2 (nach adapt — Türkische Adaptation prüfen):
btcedu review approve <REVIEW_ID> --comment "E2E validation"

# Pipeline fortsetzen:
btcedu run --episode <EPISODE_ID>

# Review Gate 3 (nach render — Video prüfen):
btcedu review approve <REVIEW_ID> --comment "E2E validation"

# Letzter Durchlauf (publish):
btcedu run --episode <EPISODE_ID>
```

**Alternativ im Dashboard:** http://localhost:5000 → Reviews-Tab → Approve-Buttons

### Schritt 5: Output verifizieren

Nach erfolgreichem Durchlauf alle Artefakte prüfen:
```bash
EP_ID="<EPISODE_ID>"
BASE="data/outputs/$EP_ID"

# Alle Artefakte vorhanden?
echo "=== Artefakte ==="
ls -la "$BASE/"
ls -la "$BASE/images/" 2>/dev/null
ls -la "$BASE/tts/" 2>/dev/null
ls -la "$BASE/render/" 2>/dev/null
ls -la "$BASE/provenance/" 2>/dev/null

# Chapters-Schema valide?
python -c "
import json
from btcedu.models.chapter_schema import ChapterDocument
doc = ChapterDocument(**json.load(open('$BASE/chapters.json')))
print(f'Chapters: {doc.total_chapters}, Duration: {doc.estimated_duration_seconds}s')
for ch in doc.chapters:
    print(f'  {ch.chapter_id}: {ch.title} ({ch.visual.type})')
"

# Video-Metadata
ffprobe -v quiet -print_format json -show_format "$BASE/render/draft.mp4" 2>/dev/null \
  | python -c "import sys,json; d=json.load(sys.stdin); print(f'Duration: {d[\"format\"][\"duration\"]}s, Size: {int(d[\"format\"][\"size\"])/1024/1024:.1f}MB')"

# Pipeline-Kosten (aus PipelineRun-Tabelle)
python -c "
from btcedu.db import get_session_factory
from btcedu.models.episode import PipelineRun
from btcedu.config import get_settings
factory = get_session_factory(get_settings().database_url)
session = factory()
runs = session.query(PipelineRun).filter(PipelineRun.episode_id == '$EP_ID').all()
total = sum(r.estimated_cost_usd or 0 for r in runs)
for r in runs:
    print(f'  {r.stage.value:15s} {r.status.value:8s} \${r.estimated_cost_usd or 0:.4f}')
print(f'Total: \${total:.4f}')
session.close()
"
```

### Schritt 6: Code-Fixes committen

Falls du Code-Fixes während des Runs machen musstest:
```bash
# Tests + Lint
pytest -x -q
ruff check btcedu/ tests/

# Commit
git add <geänderte-dateien>
git commit -m "fix: <beschreibung der fixes aus E2E validation>"

# Push
git push origin main
```

## Erfolgskriterien

- [ ] Episode durchläuft alle 16 v2-Stages (disabled Stages werden übersprungen)
- [ ] Keine unbehandelten Fehler — alle Fehler wurden gefixt
- [ ] `data/outputs/<EP_ID>/render/draft.mp4` existiert und ist abspielbar
- [ ] `data/outputs/<EP_ID>/chapters.json` ist schema-valide
- [ ] Image-Manifest vorhanden: `data/outputs/<EP_ID>/images/manifest.json`
- [ ] TTS-Manifest vorhanden: `data/outputs/<EP_ID>/tts/manifest.json`
- [ ] Provenance-Dateien für jedes Stage vorhanden
- [ ] Alle Tests passieren nach eventuellen Code-Fixes (1172+)
- [ ] Lint-clean: `ruff check btcedu/ tests/`
- [ ] Pipeline-Kosten unter $15 pro Episode

## Bei dry_run=true (ohne API-Keys)

```bash
DRY_RUN=true btcedu run --episode <ID>
```
Dies erstellt Placeholder-Outputs für alle API-abhängigen Stages. Verifiziere:
- Pipeline durchläuft alle Stages ohne Fehler
- Placeholder-Dateien werden korrekt erstellt
- Review Gates funktionieren korrekt (create + approve)
- Render-Stage erstellt ein Test-Video (wenn ffmpeg vorhanden)
- Kosten sind $0.00 (dry run)

## Wichtige Dateien (Referenz)

| Datei | Zweck |
|-------|-------|
| `btcedu/core/pipeline.py` | Stage-Orchestrierung, `_V2_STAGES`, `_run_stage()` |
| `btcedu/core/reviewer.py` | Review CRUD, `approve_review()`, `has_pending_review()` |
| `btcedu/config.py` | Alle Settings (API Keys, Limits, Feature Flags) |
| `btcedu/cli.py` | CLI Commands (`detect`, `run`, `review`, `status`) |
| `btcedu/models/episode.py` | Episode + PipelineRun + EpisodeStatus Enums |
| `btcedu/core/renderer.py` | Video-Assembly (segments → concat → draft.mp4) |
| `btcedu/services/` | Alle API-Wrapper (Claude, OpenAI, ElevenLabs, D-ID, Pexels) |
| `CLAUDE.md` | Projekt-Übersicht und Coding Conventions |
