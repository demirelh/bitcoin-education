# btcedu Pipeline — Aufbau & KI-Einsatz

## Projektbeschreibung

**btcedu** ist eine vollautomatische Content-Pipeline, die fremdsprachige
Nachrichten- und Bildungsinhalte in fertige, muttersprachliche YouTube-Videos
umwandelt. Aus einer Quell-Sendung (Video + Audio) entsteht ohne manuelle
Zwischenschritte ein neues Video mit übersetztem, neutralisiertem Skript,
KI-generierten Bildern, synthetischer Sprachausgabe und fertigem Schnitt.

Der Betrieb läuft auf einem **Raspberry Pi** (Python 3.12, Click-CLI +
Flask-Web-Dashboard, SQLite) und wird über **systemd-Timer** getaktet; die
Bild-, Sprach- und Text-KI wird über externe APIs bzw. die Copilot CLI
angebunden. Die Architektur ist **profilbasiert**: ein YAML-Profil beschreibt
Quelle, Sprachen, Register, Bild-/Stimmen-Einstellungen und Veröffentlichungs­
regeln, sodass sich neue Kanäle/Formate ohne Codeänderung ergänzen lassen.

Das derzeit aktive Profil ist **`tagesschau_tr`**: deutsche *tagesschau*-
Sendungen → neutrale türkische Nachrichten-Videos.

## Ziel

- **Automatisierung:** Aus einer Quell-Sendung ohne Handarbeit ein
  veröffentlichungsreifes Video erzeugen — von Transkription bis Schnitt.
- **Sprachliche & inhaltliche Qualität:** Fachgerechte DE→TR-Übersetzung im
  formalen Nachrichten-Register, mit kultureller Adaption, Neutralisierung
  (Entfernen von Moderator/Intro/Outro) und einer **unabhängigen KI-QA**
  (anderes Modell als der Produzent) als Cross-Check gegen systematische Fehler.
- **Sicherheit & Kontrolle:** Kostenobergrenze pro Episode, idempotente Stages,
  robuste Retries — und ein bewusster **Stopp vor dem YouTube-Upload** für die
  finale menschliche Endkontrolle.
- **Skalierbarkeit:** Über zusätzliche Profile auf weitere Sprachen, Quellen und
  Themen (z. B. Bitcoin-Bildung, der ursprüngliche Anwendungsfall) erweiterbar.

---

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
| 2  | **transcribe**     | ✅   | **OpenAI Whisper `whisper-1`** (DE, konfigurierbar) | Audio → strukturiertes Transkript mit Segmenten, Zeitstempeln und Legacy-Textdateien |
| 3  | **transcript_analyze** | – | Deterministische Heuristiken              | Markiert konservativ verdächtige ASR-Segmente; keine externe API |
| 4  | **transcript_verify** | ✅ | **OpenAI `gpt-4o-mini-transcribe`** (konfigurierbar) | Transkribiert nur verdächtige, zusammengeführte Audioausschnitte erneut und vergleicht kritische Fakten deterministisch |
| 5  | **correct**        | ✅   | **Claude Sonnet 4.5** (via Copilot CLI)  | Sichere segmentweise ASR-Korrektur als validiertes JSON; keine freie Rekonstruktion. Riskante Faktenänderungen werden deterministisch verworfen |
| 6  | **transcript_qa**  | –    | Deterministische Regeln                  | Bewertet dauerhaft Namen-, Zahlen-, Datums-, Negations-, Opferzahl-, Ergebnis- und Rollenunsicherheiten als GREEN/YELLOW/RED |
| 7  | review_gate_transcript_qa | – | –                                     | Setzt bei kritischen oder zu vielen schweren Findings einen blockierenden ReviewTask; Freigabe oder Änderungen anfordern |
| 8  | review_gate_1      | –    | –                                        | Korrektur-Review; für das Profil regulär Auto-approve |
| 9  | **segment**        | ✅   | **Claude Sonnet 4.5**                    | Zerlegt die Sendung in einzelne News-Beiträge (StoryDocument) |
| 10 | **translate**      | ✅   | **Claude Sonnet 4.5**                    | Pro-Beitrag DE→TR, `formal_news`-Register, entfernt Moderator/Intro/Outro, Glossar |
| 11 | **adapt**          | ✅   | **Claude Sonnet 4.5**                    | Kulturelle Adaption (Tiers `anchor_unify`, `local_relevance`), Neutralisierung |
| 12 | review_gate_2      | ✅   | **GPT-5.6 Sol** (via Copilot CLI)        | **QA-Zweitmeinung** — unabhängige Qualitätsprüfung TR vs. DE-Quelle (beratend). Findings fließen beim Re-Run in translate/adapt zurück |
| 13 | **chapterize**     | ✅   | **Claude Sonnet 4.5**                    | Erzeugt ChapterDocument: Kapitel, Narrationstext, Visual-Vorgaben, Overlays |
| 14 | **frameextract**   | –    | ffmpeg                                   | Keyframes aus dem Quellvideo |
| 15 | **imagegen**       | ✅   | **Ideogram + Flux + DALL-E 3** (Routing) | Pro Kapitel ein Bild: Ideogram (Text/Karten/Infografik), Flux (photoreal b_roll), DALL-E 3 (Fallback) |
| 16 | review_gate_stock  | –    | –                                        | Auto-approve |
| 17 | **tts**            | ✅   | **ElevenLabs `eleven_turbo_v2_5`**, Stimme „Irem" | Türkische Sprachsynthese pro Kapitel |
| 18 | **anchorgen**      | (✅) | **D-ID** (Talking-Head)                  | Avatar-Video — **aktuell No-Op** (kein D-ID-Key gesetzt) |
| 19 | **render**         | –    | ffmpeg                                   | Video-Zusammenbau (Bilder + TTS + Ticker + Musik). Pi = Software-Encoding |
| 20 | review_gate_3      | –    | –                                        | Auto-approve |
| 21 | **publish**        | –    | YouTube API                              | `auto_publish: false` → **stoppt vor dem Upload** (manuelle Endkontrolle) |

---

## Modell-Zusammenfassung

- **OpenAI Whisper `whisper-1`** → Transkription (Stage 2; Provider und Modell
  sind konfigurierbar)
- **Deterministische Heuristiken** → Transkriptanalyse (Stage 3, keine KI/API)
- **OpenAI `gpt-4o-mini-transcribe`** → selektive Zweittranskription
  verdächtiger Audioausschnitte (Stage 4)
- **Claude Sonnet 4.5** (über Copilot CLI, `llm_provider=copilot_cli`) →
  correct, segment, translate, adapt, chapterize (Stages 5, 9, 10, 11, 13) —
  das **inhaltliche Kernmodell**
- **Deterministische Transcript-QA** → blockierender Faktenrisiko-Check
  (Stages 6–7, keine externe API)
- **GPT-5.6 Sol** (über Copilot CLI) → QA-Zweitmeinung (Stage 12) — bewusst ein
  **anderes** Modell als der Produzent, damit es dessen systematische Fehler
  unabhängig erkennt (Cross-Check)
- **Ideogram / Flux / DALL-E 3** → Bildgenerierung (Stage 13)
- **ElevenLabs turbo v2.5** (Stimme „Irem") → TTS (Stage 15)
- **D-ID** → Avatar (Stage 16, deaktiviert)

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
- **Sichere Korrektur:** Neben `transcript.corrected.de.txt` entsteht
  `transcript.corrected.structured.de.json` mit Segment-IDs, Zeitstempeln,
  Original-/Korrekturtext, Status, Severity, Flags und Verification-IDs.
  Ungültige JSON-Ausgabe wird einmal erneut angefordert; ein Freitext-Fallback
  existiert nicht.
- **Transcript-QA-Gate:** `transcript_qa.json` bleibt als Audit-Artefakt erhalten.
  Kritische ungeklärte Fakten blockieren vor Segmentierung/Übersetzung und sind
  im CLI, QA-Reiter und bestehenden Review-Workflow prüfbar.
- **QA-Feedback-Loop:** Die QA-Findings von Stage 12 werden beim nächsten Lauf in
  translate/adapt als Korrekturvorgabe injiziert → iterative Selbstkorrektur.
  Auslösbar über zwei Buttons im QA-Reiter der Web-UI
  („Restart Translate + Adapt" bzw. „Restart All").
- **Vollautomatik mit Sicherheitsstopp:** Reguläre Profil-Reviews sind
  auto-approved. Ein blockierendes Transcript-QA-Finding wird jedoch nicht
  automatisch freigegeben. Zusätzlich stoppt die Pipeline bewusst **vor dem
  YouTube-Upload**.

---

## Bekannte Limitierung

Die Zweittranskription prüft nur markierte Ausschnitte und entscheidet
deterministisch über klar messbare Abweichungen. Freie semantische
Umformulierungen, einzelne Eigennamen und komplexe Rollenwechsel können deshalb
weiterhin eine menschliche Prüfung benötigen. Die Übersetzungs-QA (Stage 12) prüft das
türkische Skript weiterhin gegen das **korrigierte deutsche Transkript**.
