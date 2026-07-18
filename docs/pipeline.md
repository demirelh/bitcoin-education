# btcedu Pipeline — Aufbau & KI-Einsatz

Vollständige, aus dem Code verifizierte Beschreibung der Verarbeitungspipeline
für das aktive Profil **`tagesschau_tr`** (Deutsch → Türkisch, `pipeline_version=2`,
vollautomatisch). Stand: 2026-07.

**Zweck:** Deutsche *tagesschau*-Nachrichtensendungen automatisch in neutrale
türkische YouTube-Videos umwandeln.

**Trigger:** systemd-Timer alle 10 Minuten →
`btcedu run-latest --profile tagesschau_tr --all-channels`. Greift die neueste
20:00-Uhr-Sendung (bzw. „100 Sekunden") anhand eines Titel-Regex
(`title_include` im Profil).

---

## Ablauf & KI-Einsatz pro Stage

| #  | Stage              | KI?  | Modell                                   | Aufgabe |
|----|--------------------|------|------------------------------------------|---------|
| 1  | **download**       | –    | –                                        | yt-dlp lädt Quellvideo + Audio |
| 2  | **transcribe**     | ✅   | **OpenAI Whisper `whisper-1`** (DE)      | Audio → deutsches Transkript |
| 3  | **correct**        | ✅   | **Claude Sonnet 4.5** (via Copilot CLI)  | ASR-/Tippfehler-Korrektur des Transkripts |
| 4  | review_gate_1      | –    | –                                        | Auto-approve (`auto_approve_reviews: true`) |
| 5  | **segment**        | ✅   | **Claude Sonnet 4.5**                    | Zerlegt die Sendung in einzelne News-Beiträge (StoryDocument) |
| 6  | **translate**      | ✅   | **Claude Sonnet 4.5**                    | Pro-Beitrag DE→TR, `formal_news`-Register, entfernt Moderator/Intro/Outro, Glossar |
| 7  | **adapt**          | ✅   | **Claude Sonnet 4.5**                    | Kulturelle Adaption (Tiers `anchor_unify`, `local_relevance`), Neutralisierung |
| 8  | review_gate_2      | ✅   | **GPT-5.6 Sol** (via Copilot CLI)        | **QA-Zweitmeinung** — unabhängige Qualitätsprüfung TR vs. DE-Quelle (beratend). Findings fließen beim Re-Run in translate/adapt zurück |
| 9  | **chapterize**     | ✅   | **Claude Sonnet 4.5**                    | Erzeugt ChapterDocument: Kapitel, Narrationstext, Visual-Vorgaben, Overlays |
| 10 | **frameextract**   | –    | ffmpeg                                   | Keyframes aus dem Quellvideo |
| 11 | **imagegen**       | ✅   | **Ideogram + Flux + DALL-E 3** (Routing) | Pro Kapitel ein Bild: Ideogram (Text/Karten/Infografik), Flux (photoreal b_roll), DALL-E 3 (Fallback) |
| 12 | review_gate_stock  | –    | –                                        | Auto-approve |
| 13 | **tts**            | ✅   | **ElevenLabs `eleven_turbo_v2_5`**, Stimme „Irem" | Türkische Sprachsynthese pro Kapitel |
| 14 | **anchorgen**      | (✅) | **D-ID** (Talking-Head)                  | Avatar-Video — **aktuell No-Op** (kein D-ID-Key gesetzt) |
| 15 | **render**         | –    | ffmpeg                                   | Video-Zusammenbau (Bilder + TTS + Ticker + Musik). Pi = Software-Encoding |
| 16 | review_gate_3      | –    | –                                        | Auto-approve |
| 17 | **publish**        | –    | YouTube API                              | `auto_publish: false` → **stoppt vor dem Upload** (manuelle Endkontrolle) |

---

## Modell-Zusammenfassung

- **OpenAI Whisper `whisper-1`** → Transkription (Stage 2)
- **Claude Sonnet 4.5** (über Copilot CLI, `llm_provider=copilot_cli`) →
  correct, segment, translate, adapt, chapterize (Stages 3, 5, 6, 7, 9) —
  das **inhaltliche Kernmodell**
- **GPT-5.6 Sol** (über Copilot CLI) → QA-Zweitmeinung (Stage 8) — bewusst ein
  **anderes** Modell als der Produzent, damit es dessen systematische Fehler
  unabhängig erkennt (Cross-Check)
- **Ideogram / Flux / DALL-E 3** → Bildgenerierung (Stage 11)
- **ElevenLabs turbo v2.5** (Stimme „Irem") → TTS (Stage 13)
- **D-ID** → Avatar (Stage 14, deaktiviert)

---

## Wichtige Design-Hinweise

- **Konfig-Präzedenz:** Das Profil (`btcedu/profiles/tagesschau_tr.yaml`)
  überschreibt die `.env`. Deshalb ist imagegen **generativ**
  (Ideogram/Flux/DALL-E), obwohl `.env` `pexels` sagt; Gemini-Frame-Editing ist
  konfiguriert, aber für dieses Profil **inaktiv** (nur bei
  `provider: gemini_frame_edit` aktiv).
- **Kostenschutz:** Guard pro Episode (`max_episode_cost_usd`, Default $10).
- **Robustheit:** Refusal-Retry (leerer/verweigerter LLM-Output → Reframe-Retry
  → Anthropic-Fallback), JSON-Korrektur-Retry (z. B. bei fehlerhaftem
  Chapterize-JSON), plus Pipeline-Level-Retry.
- **Idempotenz:** Jede Stage hat SHA-256-Provenance; unveränderte Inputs werden
  übersprungen. Upstream-Änderungen setzen `.stale`-Marker
  (Cascade-Invalidierung).
- **QA-Feedback-Loop:** Die QA-Findings von Stage 8 werden beim nächsten Lauf in
  translate/adapt als Korrekturvorgabe injiziert → iterative Selbstkorrektur.
  Auslösbar über zwei Buttons im QA-Reiter der Web-UI
  („Restart Translate + Adapt" bzw. „Restart All").
- **Vollautomatik mit Endkontrolle:** Alle Human-Review-Gates sind auto-approved;
  die Pipeline stoppt bewusst **vor dem YouTube-Upload**.

---

## Bekannte Limitierung

Die **Whisper-Transkription (DE)** und die **deutsche Korrektur** ganz am Anfang
sind die primäre Fehlerquelle — Fehler dort propagieren durch die gesamte Kette.
Die QA (Stage 8) prüft nur das türkische Skript gegen das **korrigierte deutsche
Transkript**, nicht gegen das Original-Audio. Eine Transkriptions-QA gegen die
Quelle wäre eine mögliche Erweiterung.
