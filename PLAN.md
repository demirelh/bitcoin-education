# 🎯 Plan: Publish-Ready Video-Pipeline für Monetarisierung

**Ziel:** Vollautomatische Pipeline, die aus deutschen YouTube-Podcasts/News türkische
YouTube-Videos für in Deutschland lebende Türken produziert — publish-ready,
monetarisierbar, cross-platform.

**Pilot-Kanäle:**
- `bitcoin_podcast` — Blocktrainer/BTC-Content, DE→TR, Ziel: Krypto-affine Community
- `tagesschau_tr` — Deutsche Nachrichten auf Türkisch, Ziel: Diaspora Informationsversorgung

---

## 1. Stage-by-Stage Verbesserungen

### 🎤 Transcribe
| Aktuell | Verbesserung | Warum |
|---|---|---|
| Whisper-1 API | **faster-whisper large-v3-turbo** (lokal) | 8× günstiger, bessere DE |
| — | pyannote-audio Diarization | Sprecherwechsel → Chapter-Grenzen |
| — | VAD-Prefilter | Werbung/Pausen weg |

### 🔧 Correct
- Claude Sonnet 4.5 (Update von 4)
- **Domain-Glossare** injecten: `glossary_bitcoin_tr.yaml`, `glossary_news_tr.yaml`

### 🌐 Translate (DE→TR)
- **Zwei-Pass**: Sonnet 4.5 Draft → Opus 4.7 Polish (nur Hook + CTAs)
- **Register per Profil**:
  - Bitcoin: `casual_educational`, du-Form, Anglizismen erklären
  - Tagesschau: `formal_news`, sie-Form, TRT-Standard

### 🇹🇷 Adapt
- Bitcoin: bestehende Tiers + `examples_turkey` + `local_regulation` (BDDK/SPK)
- **Tagesschau: `adapt.skip=false` machen!** → tier `local_relevance` (kurze DE-TR-Brücke)

### 📖 Chapterize
- Model auf **Claude Haiku 4.5** (spart 70%)
- Optimale Chapter-Länge: 45-90s
- Erstes Chapter = `hook`, ≤15s, mit Cliffhanger

### 🎨 Bilder (größter Qualitätshebel)
| Provider | Nutzung | Preis |
|---|---|---|
| **Flux.1 [dev]** via fal.ai/Replicate | Hero-Shots photoreal | ~$0.025 |
| **Ideogram v2** | Bilder MIT Text (Overlays, Zitate, Thumbnails) | ~$0.08 |
| **Gemini 2.5 Flash Image** | Frame-Edit Tagesschau (DE→TR Overlays) | ~$0.03 |
| Unsplash API | Free-Stock statt Pexels | Free |
| Pexels | B-Roll Fallback | Free |

❌ X-Bilder direkt = Copyright-Risiko → Monetarisierung gesperrt.
✅ X-Bilder als Vision-Input für Claude → dann selbst rendern.
✅ Tagesschau-Frames: Fair Use ok bei starker Transformation (TR-Voice + Overlay-Edits).

### 🎙️ TTS
- **Bitcoin (POLAT bleibt)**: `stability=0.4, similarity=0.85, style=0.3`
- **Tagesschau**: eigene TR-News-Voice, `stability=0.7, style=0.0`
- Model: `eleven_turbo_v2_5` (50% günstiger als multilingual_v2)
- SSML-Tags: `<break>`, `<emphasis>` fürs Pacing

### 🎬 Render
- `crf=18` (statt 20 — YouTube-1080p sweet spot)
- **Ken Burns, Lower Thirds animated, Ticker (nur Tagesschau), Intro/Outro** aktivieren
- **BG-Music**: Bitcoin=Electronic-Bed, Tagesschau=News-Bed
- **B-Roll-Cuts** alle 8-12s aus Pexels-Video → gegen Slideshow-Look
- Auto-Captions burned-in (Retention +15%)
- Fonts: Bitcoin=Inter Black + `#F7931A`, Tagesschau=Roboto Condensed + `#004B87`

### 📤 Publish
1. **Thumbnails** (fehlt komplett): Ideogram, 3 Varianten/Ep, A/B nach 48h
2. **Titel-Optimizer**: 5 Varianten, ≤60 Zeichen, Emotion/Number/Question, Keyword vorn
3. **Description**: Hook first 3 Zeilen, Chapters (schon da), Affiliate-Links, Newsletter
4. **Tags dynamisch** aus Chapter-Titeln + TR-YouTube-Trending
5. **Publish-Timing**: 19-21h CET (Feierabend Deutschland), Timer-basiert

### 🔍 QA/Review
- Copyright Music Detection
- Profanity Filter TR-spezifisch
- Political-Bias-Check (neutrale Zone)
- Länge: 8-15min für Mid-Roll Ads
- Audio auf **-14 LUFS** normalisieren
- Manual Gate bei Rating < 4/5

---

## 2. Model-Selection-Matrix

| Stage | Aktuell | Empfohlen |
|---|---|---|
| Transcribe | whisper-1 API | faster-whisper large-v3-turbo (lokal) |
| Correct | Sonnet 4 | Sonnet 4.5 |
| Translate Draft | Sonnet 4 | Sonnet 4.5 |
| Translate Polish | — | Opus 4.7 (Hook+CTA only) |
| Adapt | Sonnet 4 | Sonnet 4.5 |
| Chapterize | Sonnet 4 | **Haiku 4.5** |
| Image Prompts | Sonnet 4 | Haiku 4.5 |
| Image Gen Hero | DALL-E 3 | **Flux.1 dev** |
| Image Gen Text | DALL-E 3 | **Ideogram v2** |
| TTS | eleven_multilingual_v2 | eleven_turbo_v2_5 |
| Thumbnail | — | **Ideogram v2** |
| QA Check | — | Haiku 4.5 |

**Kosten/10-Min-Episode:** aktuell ~$4-6 → optimiert **~$1.50-2.50**

---

## 3. Monetarisierungs-Erweiterungen

### A. Multi-Format aus einer Episode
- Long-Form 10-15min (YouTube monetize)
- 3× Shorts (60s) → YouTube Shorts + TikTok
- 3× Instagram Reels (9:16)
- Podcast-only (Spotify RSS)
- Blog-Post (SEO auf eigenem Host)

### B. Cross-Platform Publish
- YouTube (primär)
- TikTok
- Instagram Reels
- Spotify Podcast
- Automation via n8n / direct APIs

### C. Analytics-Loop
- YouTube Data API v3 → nach 7d CTR/Retention/AVD → DB
- Wöchentlicher Auto-Report
- Top-10% Videos → Prompts als Beispiele (RLHF-light)

### D. Multi-Source-Ausbau
- Bloomberg TV DE / Wirtschaft
- DW News
- Kurzgesagt DE
- ARD/ZDF Doku-Ausschnitte (fair use)

---

## 4. Ausführungs-Reihenfolge (Impact/Aufwand-optimiert)

### Wave 1: Sofortige Video-Qualität (~2h)
1. **Render-Features aktivieren**: Ken Burns, Lower Thirds, Ticker, Intro/Outro, Color-Correction, CRF=18
2. **Assets vorbereiten**: Intro/Outro-MP4, BG-Music-Files, Fonts

### Wave 2: Bild-Qualität (~4h)
3. **Flux.1 dev Provider** via fal.ai integrieren
4. **Ideogram v2 Provider** für Text-Bilder
5. `image_gen_provider` per Chapter-Type auswählen (hero=flux, text=ideogram, broll=pexels)

### Wave 3: Thumbnails + Titel (~3h)
6. **Thumbnail-Generator-Stage** — 3 Varianten, mit Ideogram
7. **Title-Optimizer** — 5 Varianten, LLM-Scoring

### Wave 4: Prompts + Glossare (~2h)
8. Domain-Glossare erstellen und im Translator injecten
9. Register-System pro Profil

### Wave 5: Tagesschau-Anpassungen (~2h)
10. `adapt.skip=false`, `local_relevance` Tier
11. Eigene TR-News-Voice-ID auswählen

### Wave 6: Analytics + Auto-Publish-Timing (~4h)
12. YouTube Data API Integration
13. Scheduled-Publish 19-21h CET
14. Wöchentlicher Report

### Wave 7: Cross-Format (~6h)
15. Auto-Shorts aus Long-Form
16. Reels-Export 9:16
17. Podcast RSS für Spotify
18. Blog-Post-Export

### Wave 8: Cross-Platform (~4h)
19. TikTok Auto-Upload
20. Instagram Reels API

---

## 5. Monetarisierungs-Setup

- YouTube Partner Program: Aktivieren sobald 1000 Subs + 4000h Watch (Long-Form) ODER 10M Shorts Views
- Affiliate-Links:
  - Bitpanda / Binance / Bitget Referral (Bitcoin-Content)
  - Amazon Associates DE (News-Books)
- Sponsoring: Türkisch-Deutsche Firmen (Anwälte, Steuerberater, Immobilien)
- Newsletter → E-Mail-Liste Monetarisierung
- Discord/Telegram-Community → Merch/Premium

---

**Status:** Wave 1 startet jetzt.
