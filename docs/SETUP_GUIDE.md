# Setup Guide — v2 Pipeline vollständig einrichten

Neue Installationen verwenden standardmäßig Pipeline v2. Bestehende
`pipeline_version=1`-Episoden bleiben aus Kompatibilitätsgründen lesbar, sollen
aber nicht per SQL in v2 umgeschrieben werden. Die aktuelle Pipeline stoppt für
`tagesschau_tr` vor dem YouTube-Upload, bis eine finale manuelle Freigabe
vorliegt.

---

## Übersicht: Was fehlt

| Was | Status | Aktion |
|-----|--------|--------|
| `.env` Datei | ❌ fehlt | Erstellen aus `.env.example` |
| `PIPELINE_VERSION=2` | Standard | In `.env` explizit belassen |
| Anthropic API Key | ✅ läuft schon | In `.env` übernehmen |
| OpenAI API Key | ✅ läuft schon | In `.env` übernehmen |
| ElevenLabs API Key + Voice | ❌ fehlt | Account erstellen, Key holen |
| YouTube OAuth | ❌ fehlt | Google Cloud Projekt + OAuth |
| YouTube Python Packages | ❌ nicht installiert | `pip install -e ".[youtube]"` |
| Noto Font für Rendering | ❌ fehlt | Font installieren |
| ffmpeg | ✅ installiert | — |

---

## Schritt 1: `.env` Datei erstellen

```bash
cp .env.example .env
```

Dann `.env` bearbeiten — **Datei: `.env`** (Projektroot):

---

## Schritt 2: Pipeline auf v2 umschalten

In `.env`:
```env
PIPELINE_VERSION=2
```

> **Wichtig:** v2 ergänzt strukturierte Transkripte, selektive
> Zweittranskription, Transcript-QA, Story-Inventar, Translation-QA,
> Narration-Lock und manuelles Publishing.

---

## Schritt 3: API Keys eintragen

### 3a) Anthropic (hast du schon — übernehmen)

In `.env`:
```env
ANTHROPIC_API_KEY=sk-ant-dein-key-hier
```
Wird für Anthropic-basierte LLM-Stages oder als konfigurierter Fallback genutzt.
Bei `LLM_PROVIDER=copilot_cli` muss zusätzlich die Copilot CLI installiert und
authentifiziert sein.

### 3b) OpenAI (hast du schon — übernehmen)

In `.env`:
```env
OPENAI_API_KEY=sk-dein-key-hier
```
Wird für primäre/sekundäre Transkription und optional OpenAI-Bildgenerierung genutzt.

### 3c) ElevenLabs — NEU einrichten

1. Account erstellen: https://elevenlabs.io
2. API Key holen: https://elevenlabs.io/app/settings/api-keys
3. Voice auswählen: https://elevenlabs.io/app/voice-library
   - Empfehlung: Eine türkischsprachige Stimme wählen (da Output auf Türkisch ist)
   - Die Voice-ID findest du in der URL oder über die API

In `.env`:
```env
ELEVENLABS_API_KEY=dein-elevenlabs-key
ELEVENLABS_VOICE_ID=die-voice-id-hier
```

Optional (Defaults sind ok):
```env
ELEVENLABS_MODEL=eleven_multilingual_v2
ELEVENLABS_STABILITY=0.5
ELEVENLABS_SIMILARITY_BOOST=0.75
```

---

## Schritt 4: YouTube Upload einrichten (optional)

Falls du Videos automatisch auf YouTube hochladen willst:

### 4a) Google Cloud Projekt erstellen

1. https://console.cloud.google.com → Neues Projekt erstellen
2. **YouTube Data API v3** aktivieren:
   - APIs & Services → Library → "YouTube Data API v3" suchen → Enable
3. **OAuth 2.0 Credentials** erstellen:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - JSON herunterladen

### 4b) Client Secret platzieren

```bash
# Die heruntergeladene JSON-Datei hierhin kopieren:
cp ~/Downloads/client_secret_xxxxx.json data/client_secret.json
```

### 4c) YouTube Python Packages installieren

```bash
pip install -e ".[youtube]"
```

### 4d) OAuth Token erstellen (interaktiv)

```bash
btcedu youtube-auth
```
→ Öffnet Browser, du meldest dich mit deinem YouTube-Kanal an.
→ Erstellt `data/.youtube_credentials.json` automatisch.

### 4e) `.env` Einstellungen (Defaults sind meistens ok)

```env
YOUTUBE_CLIENT_SECRETS_PATH=data/client_secret.json
YOUTUBE_CREDENTIALS_PATH=data/.youtube_credentials.json
YOUTUBE_DEFAULT_PRIVACY=unlisted
YOUTUBE_DEFAULT_LANGUAGE=tr
YOUTUBE_CATEGORY_ID=27
```

---

## Schritt 5: System-Dependencies

### Font für Video-Rendering

```bash
# Ubuntu/Debian:
sudo apt install fonts-noto-core

# Prüfen:
fc-list | grep -i "NotoSans-Bold"
```

Falls der Font einen anderen Namen hat, in `.env` anpassen:
```env
RENDER_FONT=NotoSans-Bold
```

### ffmpeg (✅ bereits installiert)

Ist schon da. Wenn nicht: `sudo apt install ffmpeg`

---

## Schritt 6: Datenbank und bestehende Episoden

```bash
btcedu init-db
btcedu migrate
btcedu migrate-status
```

Migrationen sind idempotent und löschen keine Episodendaten. Bestehende
v1-Episoden behalten ihren gespeicherten Pfad. Für eine v2-Neuverarbeitung eine
neue Episode erkennen oder die vorhandenen Restart-/Review-Aktionen verwenden;
Status und `pipeline_version` nicht direkt per SQL manipulieren.

---

## Checkliste `.env` — Minimal für v2

```env
# ── Pflicht ──────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...          # LLM stages
OPENAI_API_KEY=sk-...                 # Whisper + DALL-E
ELEVENLABS_API_KEY=...                # TTS
ELEVENLABS_VOICE_ID=...               # TTS Voice
PIPELINE_VERSION=2                    # v2 Pipeline aktivieren
DEFAULT_CONTENT_PROFILE=tagesschau_tr # gewünschtes Profil
PODCAST_YOUTUBE_CHANNEL_ID=UC...      # Quell-Channel

# ── Optional (Defaults ok) ──────────────────────────
# IMAGE_GEN_QUALITY=standard          # oder "hd" (teurer)
# RENDER_FONT=NotoSans-Bold
# YOUTUBE_DEFAULT_PRIVACY=unlisted
# MAX_EPISODE_COST_USD=10.0           # Kostenlimit pro Episode
# DRY_RUN=false                       # true = keine API-Calls
```

---

## Kosten-Überblick pro Episode (ca.)

| Stage | Service | ~Kosten |
|-------|---------|---------|
| Transcribe | OpenAI Whisper | ~$0.10 |
| Selective verification | OpenAI ASR | nur verdächtige Ausschnitte |
| Correct | konfigurierter LLM-Provider | abhängig vom Modell |
| Deterministic QA | Python | $0 |
| Translate | Claude | ~$0.15 |
| Adapt | Claude | ~$0.10 |
| Independent QA | konfigurierter QA-Provider | nur Standard/Eskalation |
| Chapterize | Claude | ~$0.10 |
| Image Gen | DALL-E 3 | ~$0.40/Bild × ~6 Kapitel = ~$2.40 |
| TTS | ElevenLabs | ~$0.30 (je nach Plan) |
| **Gesamt** | | **~$3–4 pro Episode** |

> Der Cost Guard prüft vor jedem kostenpflichtigen Aufruf. Wird die Grenze
> innerhalb einer Stage erreicht, gilt die Stage nicht als erfolgreich.

---

## Reihenfolge der Schritte

1. `cp .env.example .env`
2. API Keys in `.env` eintragen (Anthropic, OpenAI, ElevenLabs)
3. `PIPELINE_VERSION=2` setzen
4. Font installieren: `sudo apt install fonts-noto-core`
5. Datenbank: `btcedu init-db && btcedu migrate`
6. (Optional) YouTube: `pip install -e ".[youtube]"` + `btcedu youtube-auth`
7. Mit einer Testepisode oder `DRY_RUN=true` prüfen
8. Vor echtem Publish finale manuelle Freigabe im Dashboard erteilen
