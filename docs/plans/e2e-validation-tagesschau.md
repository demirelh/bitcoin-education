# E2E Validation: Tagesschau-Episode durch die Pipeline

Lies die Datei `docs/plans/final-e2e-validation-prompt.md` und führe **alle Schritte** darin aus — aber mit folgenden Anpassungen für **Tagesschau**-Inhalte statt Bitcoin-Podcast:

## Abweichungen vom Standard-Prompt

### 1. Episode erkennen/anlegen

Statt einer Bitcoin-Podcast-Episode muss eine **Tagesschau**-Episode verwendet werden.
Entweder existiert bereits eine im System:
```bash
btcedu detect --channel tagesschau_tr
```

Oder manuell anlegen (aktuelle tagesschau 20:00 Uhr von YouTube):
```bash
btcedu add-episode \
  --url "https://www.youtube.com/watch?v=<AKTUELLE_TAGESSCHAU_VIDEO_ID>" \
  --title "tagesschau 20:00 Uhr $(date +%d.%m.%Y)" \
  --profile tagesschau_tr
```

### 2. Content-Profil = `tagesschau_tr`

Das Profil `tagesschau_tr` aktiviert folgende Besonderheiten:
- **Segment-Stage** ist aktiviert (`segment.enabled: true`) — die Sendung wird in einzelne Nachrichtenbeiträge zerlegt
- **Adapt-Stage wird übersprungen** (`adapt.skip: true`) — keine kulturelle Adaption nötig
- **Übersetzung per Story** (`translate.mode: per_story`) im formellen Nachrichtenregister
- **Moderator-Bereinigung** (`clean_moderator: true`) — Moderatoren-Intros/Outros werden entfernt
- **Akzentfarbe**: ARD-Blau `#004B87`
- **YouTube-Kategorie**: 25 (News)
- **Prompt-Namespace**: `tagesschau_tr` (eigene Prompt-Templates)

### 3. Pipeline-Stages (v2, Tagesschau-Profil)

Die Stage-Reihenfolge ist leicht anders als beim Bitcoin-Podcast:
```
download → transcribe → correct → [review_gate_1] →
segment → translate → [review_gate_editorial] → chapterize →
frameextract → imagegen → [review_gate_stock] → tts →
anchorgen → render → [review_gate_3] → publish
```

Beachte:
- **`segment`** kommt nach review_gate_1 (zerlegt die Sendung in Stories)
- **`adapt`** fehlt (übersprungen für News-Profile)
- **`review_gate_2`** wird durch **`review_gate_editorial`** ersetzt (Nachrichtenredaktions-Checkliste)

### 4. Pipeline starten

```bash
btcedu run --episode <EPISODE_ID> --verbose
```

Oder mit Profil-Filter:
```bash
btcedu run --profile tagesschau_tr --verbose
```

### 5. Zusätzliche Prüfpunkte (Tagesschau-spezifisch)

Bei der Output-Verifizierung (Schritt 5 im Haupt-Prompt) zusätzlich prüfen:

```bash
EP_ID="<EPISODE_ID>"
BASE="data/outputs/$EP_ID"

# Stories/Segmente vorhanden?
ls -la "$BASE/stories.json" 2>/dev/null || echo "FEHLT: stories.json"

# Stories-Schema prüfen
python -c "
import json
from btcedu.models.story_schema import StoryDocument
doc = StoryDocument(**json.load(open('$BASE/stories.json')))
print(f'Stories: {len(doc.stories)}')
for s in doc.stories:
    print(f'  {s.story_id}: {s.headline} ({s.duration_seconds}s)')
"

# Disclaimer vorhanden in chapters.json?
grep -c "tagesschau" "$BASE/chapters.json" && echo "OK: tagesschau-Attribution vorhanden"

# Moderator-Intros entfernt?
# (sollte keine "Willkommen zur tagesschau" o.ä. im türkischen Output sein)
grep -i "tagesschau\|tagesthemen\|Das Erste" "$BASE/tts/"*.txt 2>/dev/null \
  && echo "WARNUNG: Sendungsnamen im TTS-Output gefunden!" \
  || echo "OK: Keine Sendungsnamen im TTS-Output"
```

### 6. Erfolgskriterien (zusätzlich)

Neben den Kriterien aus dem Haupt-Prompt:
- [ ] `stories.json` existiert und ist schema-valide
- [ ] Jede Story hat eine eigene Übersetzung
- [ ] Moderator-Intros/-Outros sind bereinigt
- [ ] ARD/tagesschau-Attribution ist im Output vorhanden
- [ ] Keine Sendungsnamen (tagesschau, tagesthemen, Das Erste) im türkischen TTS-Output
- [ ] YouTube-Metadata: Kategorie 25 (News), Tags enthalten "haberler"

## Zusammenfassung

Führe den vollständigen E2E-Validierungs-Prompt (`docs/plans/final-e2e-validation-prompt.md`) aus,
aber verwende das Profil `tagesschau_tr` statt `bitcoin_podcast`. Beachte die oben genannten
Abweichungen bei Stages, Review Gates und Prüfpunkten. Alle Code-Fixes committen und pushen.
