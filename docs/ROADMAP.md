# ALMANYA24 – Fahrplan zum profitablen Nachrichtenkanal

**Dies ist die einzige verbindliche Planungsdatei.** Ältere Pläne (Abschnitt
„Einordnung älterer Pläne") sind Hintergrund, nicht Arbeitsauftrag.

## So arbeitest du mit dieser Datei

- Frage den Agenten: *„Wo stehen wir mit der Planung?"* oder *„Wie machen wir
  weiter?"* – der Agent liest diese Datei, nennt den Abschnitt
  **Nächste Schritte** und arbeitet den obersten offenen Punkt ab.
- Nach jedem erledigten Punkt: Kästchen abhaken, **Statuslog** ergänzen
  (Datum, was, Beleg), **Nächste Schritte** neu setzen. Zahlen nur mit Quelle
  (Datenbank, Log, Befehl) eintragen.
- Entscheidungen, die nur du treffen kannst, stehen unter
  **Offene Entscheidungen**. Ein Agent trifft sie nicht stillschweigend.

## Ziel

Täglich nach der 20-Uhr-tagesschau eine professionelle türkischsprachige
Nachrichtensendung mit KI-Moderatorin auf YouTube veröffentlichen, ergänzt um
Shorts und eine Nachrichten-Website aus denselben Daten – mit Einnahmen über
den laufenden Kosten.

| Kennzahl | Ziel |
|---|---|
| Video öffentlich | täglich, spätestens 21:30 Uhr |
| Zuschauer nach 30 Sekunden | ≥ 70 % |
| Klickrate Vorschaubild | ≥ 5 % |
| Laufende Kosten | ≤ 300 USD/Monat bis zur Monetarisierung |
| YouTube-Partnerprogramm | Schwellen erreicht und Antrag gestellt |
| Website | öffentlich, indexiert, mit Impressum |

## Ist-Stand (geprüft am 2026-11-12)

| Bereich | Befund | Beleg |
|---|---|---|
| Videopipeline | Läuft täglich und stabil: 10 von 10 Folgen seit 2026-11-02 fertig, keine offenen Fehler | `pipeline_runs`, `episodes` |
| Fertigstellung | 21:37–22:42 Uhr (Ziel 21:30 nie erreicht). Größte Posten: TTS ⌀ 28 min, Render ⌀ 25 min | `pipeline_runs` |
| Kosten pro Folge | ≈ 3,30 USD erfasst; davon TTS ≈ 3,00, Bilder ≈ 0,16. Textmodelle über Copilot CLI: 0 USD **erfasst**, nicht kostenlos | `pipeline_runs.estimated_cost_usd` |
| **YouTube** | **Noch kein einziges Video hochgeladen.** 10 Folgen warten auf die Freigabe `publish`; `publish_jobs` ist leer | `review_tasks`, `publish_jobs` |
| YouTube-Zugang | Ziel `test` ohne Zugangsdaten (`data/youtube/test/credentials.json` fehlt), Kanal-ID leer, `auto_publish: false` | `btcedu youtube-status` |
| Avatar | Code fertig, `anchor-readiness`: **BLOCKED** (4 Befunde: keine echten HeyGen-Look-IDs, kein Studio-Manifest, keine Rechte-Datei, Hinweistext) | `btcedu anchor-readiness --profile tagesschau_tr` |
| Avatar-Typ | Profil steht auf `digital_twin`, Entscheidung vom 2026-10-12 war Stock-Avatar. Die Unterscheidung ist im Code noch nicht umgesetzt | `profiles/tagesschau_tr.yaml`, `growth-plan` |
| Newsroom | Läuft täglich (22:30) nur in der Entwicklungsvorschau: 200 Artikel in `data/almanya24-preview/current-news.sqlite`, Produktions-DB leer | `almanya24-daily.service` |
| Website | Nur `https://sahimi.app/almanya24-dev/` (noindex). Keine Produktionsdomain, kein Impressum, keine Werbung | `ABNAHME.md` |
| Remote-Render | 11 Fehlschläge in 30 Tagen („Dispatched run … did not appear within 120s"); der lokale Ersatz greift, kostet aber Zeit | `pipeline_runs` |
| Textmodelle | Nahezu alles über `copilot_cli` (gpt-6.1-sol, claude-opus-5.5) | Profil, `almanya24_editorial.yaml` |
| Shorts/Reels, Vorschaubilder, YouTube-Statistik | nicht vorhanden bzw. nicht in der Pipeline | Code-Suche |

## Bewertung

### Was gut ist

- Die Pipeline ist technisch weit: Ingest aus dem lokalen Rekorder,
  mehrstufige Transkript- und Übersetzungsprüfung, deterministische Wetter-
  und Kartengrafiken, Lautheitsnormalisierung, Idempotenz, Kostenbremse.
- Bild- und Grafikpaket (Rundown, Themenleiste, Ortskarte, Ken Burns, freie
  Fotos) ist für einen Nachrichtensender angemessen.
- Der Newsroom arbeitet mit Belegen, Quellenprüfung und Lizenzprüfung – das ist
  journalistisch und rechtlich das wertvollste Bauteil des Projekts.

### Probleme (nach Wirkung sortiert)

1. **Es wird nichts veröffentlicht.** Ohne Uploads gibt es weder Zuschauer
   noch Daten noch Einnahmen. Das ist der eigentliche Engpass, nicht die
   Technik.
2. **Rechte- und Monetarisierungsrisiko der ARD-Übersetzung.** Die Fakten einer
   Meldung sind frei, der Sendetext der tagesschau ist geschützt. Eine
   KI-gesprochene Übersetzung fremder Sendungen ist außerdem ein typischer
   Ablehnungsgrund im YouTube-Partnerprogramm (Richtlinien zu
   wiederverwendeten bzw. nicht authentischen, massenhaft erzeugten Inhalten).
   Das Risiko wurde am 2026-10-12 bewusst akzeptiert. Für die Monetarisierung
   empfehle ich, die Entscheidung neu zu treffen (siehe E1).
3. **Abhängigkeit von der Copilot CLI im Produktionsbetrieb.** Copilot ist als
   Entwicklerwerkzeug lizenziert. Der Dauerbetrieb eines kommerziellen
   Medienprodukts darüber ist vertraglich unsicher. Dazu kommen technische
   Mängel: Verweigerungen („I'm GitHub Copilot CLI"), Zensurmarker `******`,
   Heap-Abstürze, keine Kostentransparenz und bis 2026-11-12 über 9.000
   gespeicherte Sessions.
4. **Zu spät.** Fertig ab 21:37 Uhr, oft erst gegen 22:30 Uhr. Die Zielgruppe
   schaut am Abend, Konkurrenz (z. B. DW Türkçe) ist schneller.
5. **Avatar blockiert** durch Dinge, die nur du liefern kannst (HeyGen-Zugang,
   Look-Auswahl, Studio-Freigabe, Rechte-Datei).
6. **Kein Wachstumskreislauf:** keine Shorts, keine Vorschaubilder in der
   Pipeline, keine YouTube-Statistik.
7. **Website nur als Vorschau**, keine Rechtstexte, keine Verlinkung mit dem
   Kanal.

## Modelle und Anbieter

| Aufgabe | Heute | Bewertung | Empfehlung |
|---|---|---|---|
| Transkription | OpenAI `whisper-1`, Fallback faster-whisper; Gegenprobe `gpt-4o-mini-transcribe` | gut, günstig | Mit `btcedu regression-run` 3 Folgen gegen `gpt-4o-transcribe` vergleichen; nur wechseln, wenn die Korrekturstufe messbar weniger ändern muss |
| Übersetzung, Adaption, QA | `copilot_cli` | qualitativ stark, betrieblich riskant (Problem 3) | Produktionspfad auf direkte API (Anthropic oder OpenAI, Schlüssel vorhanden) mit Kostenerfassung umstellen; Copilot nur noch als unabhängige Zweitmeinung oder in der Entwicklung. Erwartung: ca. 1–2 USD pro Folge, per Messlauf bestätigen |
| Bilder | Flux/Ideogram, freie Fotos, Pexels | gut, 0,16 USD/Folge | so lassen |
| Sprachausgabe | ElevenLabs `eleven_multilingual_v2` | beste Qualität, aber 90 % der Kosten und 28 min Laufzeit | Zuerst parallelisieren (Zeit). Dann Hörprobe gegen ElevenLabs Flash/Turbo (günstiger, schneller); Stimme ist Markenbestandteil, daher nur bei gleicher Wirkung wechseln |
| Avatar | HeyGen Avatar III, als `digital_twin` konfiguriert | Code fertig; HeyGen bleibt die richtige Wahl, weil die Integration darauf gebaut ist | siehe unten |

### Avatar mit HeyGen – Optionen

| Option | Vorteil | Nachteil |
|---|---|---|
| A. Stock-Avatar | sofort verfügbar, keine Einwilligung | nicht exklusiv: dasselbe Gesicht nutzen andere Kanäle; schwächt Marke und Vertrauen |
| B. Eigene, frei erfundene Moderatorin (Bild selbst erzeugen, in HeyGen als Photo-Avatar) | exklusives Markengesicht, keine reale Person | Lizenz- und Nutzungsbedingungen von HeyGen und Bildmodell für kommerzielle Nutzung prüfen; Qualität vorab testen |
| C. Digitaler Zwilling einer echten Sprecherin | höchste Glaubwürdigkeit | Vertrag, Einwilligung, laufende Kosten |

**Empfehlung:** Mit A einen Pilot über 3 Folgen fahren und messen, ob die
Zuschauerbindung steigt. Gleichzeitig B als dauerhaftes Markengesicht
vorbereiten. Lippensynchron zur vorhandenen ElevenLabs-Stimme bleibt der Weg
(HeyGen animiert nur). Kosten laut Profil 1 USD pro Avatarminute, bei
35–45 % Bildanteil etwa 3–5 USD pro Sendung.

## Wirtschaftlichkeit (Annahmen, mit echten Daten ersetzen)

- Laufende Kosten heute ≈ 100 USD/Monat; mit Avatar und direkter API
  ≈ 250–320 USD/Monat.
- Bei einem angenommenen Erlös von 2–4 USD je 1.000 Aufrufe (türkischsprachig,
  Zuschauer überwiegend in Deutschland) braucht die Kostendeckung grob
  75.000–150.000 Aufrufe im Monat, also 2.500–5.000 pro Tag.
- Werbeeinnahmen setzen die Aufnahme ins Partnerprogramm voraus (aktuelle
  Schwellen vor dem Antrag prüfen; bisher u. a. 1.000 Abonnenten plus
  4.000 Wiedergabestunden in 12 Monaten).
- Schneller erreichbar: **Sponsoring** durch Unternehmen mit türkischsprachiger
  Kundschaft in Deutschland, ein lokaler Sponsor kann die Kosten allein decken.
  Danach Kanalmitgliedschaften und Werbung auf der Website.
- **Inhaltliche Positionierung:** Nicht nur übersetzen, sondern einordnen –
  „Almanya'daki Türkler için ne anlama geliyor?" (Aufenthalt, Rente,
  Kindergeld, Steuern, Behörden). Das unterscheidet von DW Türkçe und stärkt
  Eigenständigkeit gegenüber der Quelle.

## Meilensteine und Aufgaben

Verantwortlich: **Du** = Betreiber, **Agent** = Umsetzung im Repository.

### M0 – Fundament (Agent)

- [x] Copilot-CLI-Sessions der Pipeline nicht mehr speichern (Commit `999f4fd`)
- [x] Anleitungsdateien der Agents geprüft und korrigiert (2026-11-12)
- [x] Dieser Fahrplan angelegt
- [x] Agententeam unter `.github/agents/` und Fahrplanseite `/roadmap` im Dashboard angelegt
- [x] **Agent + Du:** Commits pushen und mit `./run.sh` ausrollen (nach Freigabe)
- [x] **Agent:** Chat mit dem Koordinator im Dashboard (Seite „Fahrplan“)
- [x] **Agent:** Koordinator über WhatsApp (Selbst-Chat), mit Gedächtnis, Warteschlange und Live-Anzeige der Agenten; automatische Fehlermeldungen per WhatsApp abgeschaltet
- [x] **Du:** WhatsApp neu verknüpfen (Dashboard → WhatsApp → QR-Code scannen) und den WhatsApp-Dienst neu starten: `sudo systemctl restart whatsapp`
- [x] **Agent:** Remote-Render-Fehler „did not appear within 120s" beheben (Workflow auf dem Ziel-Ref, Wartezeit, Fehlerbild)

### M1 – Erste Veröffentlichung (größter Hebel)

- [ ] **Du:** YouTube-Kanal als Brand-Konto anlegen (Name, Logo, Banner, Beschreibung, Impressum-Link) – Runbook `docs/runbooks/youtube-test-environment.md`
- [ ] **Du:** Google-Cloud-Projekt, YouTube Data API v3, OAuth-Zustimmungsbildschirm. Im Status „Testing" laufen Freigaben nach 7 Tagen ab – für den Dauerbetrieb auf „In production" stellen
- [ ] **Du:** `btcedu youtube-auth --target production` ausführen; Kanal-ID in `.env` eintragen
- [ ] **Du:** YouTube-API-Prüfung („Audit and Quota Extension") beantragen. Uploads über die API aus ungeprüften Projekten bleiben sonst privat
- [ ] **Agent:** Upload kennzeichnet KI-Inhalte (`status.containsSyntheticMedia`) und schreibt den KI-Hinweis in die Beschreibung
- [ ] **Agent + Du:** Eine Folge mit `btcedu publish --episode-id … --target production --privacy unlisted` hochladen und prüfen
- [ ] **Du:** Danach `auto_publish: true` und Ziel `production` freigeben (zunächst `unlisted`, nach einer Woche `public`)

### M2 – Rechtliche und strategische Absicherung

- [ ] **Du:** Einmalige Beratung Medien-/Urheberrecht zu Übersetzung der tagesschau, Impressum, KI-Kennzeichnung (EU AI Act Art. 50) – vor der Monetarisierung
- [ ] **Du:** Entscheidung E1 treffen
- [ ] **Agent:** Bei „Newsroom zuerst": Videoskript aus der geprüften Newsroom-Artikelrevision statt aus der Übersetzung erzeugen (Arbeitspaket N7 in `docs/plans/almanya24-news-platform/work-packages.md`); tagesschau bleibt Themen- und Faktensignal

### M3 – Avatar-Pilot

- [ ] **Du:** HeyGen-API-Zugang, Avatar wählen (E2), Look-IDs liefern
- [ ] **Agent:** Bereitschaftsprüfung und Rechte-Datei unterscheiden Stock-, Photo-Avatar und digitalen Zwilling (Growth-Plan Punkt 5); Profil `avatar_type` passend setzen
- [ ] **Agent:** Studio-Hintergrund im ALMANYA24-Design entwerfen, `assets/almanya24/studio/manifest.json` erzeugen → **Du:** freigeben
- [ ] **Du:** Rechte-Datei außerhalb des Repos anlegen, KI-Hinweistext freigeben
- [ ] **Agent:** 3 Folgen mit Avatar `unlisted`, Vergleich der Zuschauerbindung mit/ohne Avatar

### M4 – Pünktlichkeit und Kosten

- [ ] **Agent:** TTS-Kapitel parallel synthetisieren (Ziel < 10 min statt 28 min, innerhalb der ElevenLabs-Gleichzeitigkeitsgrenze)
- [ ] **Agent:** Avatar-Erzeugung parallel zur übrigen Renderer-Vorbereitung
- [ ] **Agent:** Textmodelle des Produktionspfads auf direkte API mit Kostenerfassung (E3)
- [ ] **Agent:** Hörprobe ElevenLabs-Modelle; Transkriptionsvergleich per `regression-run`
- [ ] Ziel erreicht: 7 Tage in Folge vor 21:30 Uhr online

### M5 – Wachstum

- [ ] **Agent:** `thumbnail_generator` in die Pipeline (Gesicht/Foto + 2–4 große türkische Wörter, Varianten)
- [ ] **Agent:** Titel nach Neugier-Muster, Playlisten nach Thema, angehefteter Kommentar mit Website-Link
- [ ] **Agent:** Shorts 9:16 je Meldung aus vorhandenen Bausteinen (keine zweite Render-Logik)
- [ ] **Agent:** YouTube-Statistik (Aufrufe, Klickrate, Zuschauerkurve) ins Dashboard
- [ ] **Du:** Instagram-Business-Konto und Meta-App, falls Reels gewünscht

### M6 – Website live

- [ ] **Du:** Domain festlegen (E4)
- [ ] **Agent:** Newsroom vom Vorschau- in den Produktionsbetrieb (eigene DB, `btcedu site build/publish/switch`), noindex entfernen
- [ ] **Agent + Du:** Impressum (verantwortliche Person für journalistische Inhalte), Datenschutz, Korrekturhinweise
- [ ] **Agent:** Sitemap, strukturierte Daten `NewsArticle`, Verlinkung Video ↔ Artikel
- [ ] **Du:** Google Search Console, später Google News und AdSense

### M7 – Monetarisierung

- [ ] **Du:** Partnerprogramm beantragen, sobald die Schwellen erreicht sind
- [ ] **Agent + Du:** Mediadaten für Sponsoren (Reichweite, Zielgruppe), erste Sponsoranfragen
- [ ] **Agent:** Monatlicher Abgleich Einnahmen gegen Kosten im Statuslog

## Arbeitsweise mit Agenten

### Team

| Rolle | Agent (`.github/agents/`) | Ersetzt im Unternehmen |
|---|---|---|
| Koordination | `roadmap-lead` | Projektleitung |
| Pipeline, Laufzeit, Kosten | `pipeline-engineer` | Backend-Entwickler |
| Veröffentlichung und Wachstum | `youtube-growth` | YouTube-/Growth-Manager |
| Avatar und Studio | `avatar-studio` | Motion-/Video-Techniker |
| Newsroom und Website | `newsroom-web` | Web-Entwickler, SEO |
| Prüfung | `qa-reviewer` | Code-Review, QA |

**Nicht durch Agenten ersetzbar:** du als Betreiber (Konten, Verträge, Geld,
Entscheidungen E1–E5), eine Person mit redaktioneller Verantwortung
(Impressum, Korrekturen), einmalig eine Medienrechtsberatung. Optional extern:
Designer für Logo und Studio, später Vertrieb für Sponsoren.

### Autopilot

Ein Lauf erledigt **einen** Fahrplanpunkt im Worktree
`../bitcoin-education-roadmap` (Branch `roadmap`). Er ruft keine
kostenpflichtigen Dienste auf. Seit 2026-11-12 darf er den geprüften Stand auch
selbst übernehmen, pushen und ausrollen; `run.sh` verweigert den Start zwischen
19:30 und 23:30 Uhr von sich aus. Start in `tmux`, damit der Lauf eine
getrennte SSH-Verbindung übersteht:

```bash
tmux new -s roadmap
cd ~/AI-Startup-Lab/bitcoin-education
copilot --agent roadmap-lead --autopilot --max-autopilot-continues 15 \
  --deny-tool 'shell(sudo)' \
  -p "Arbeite den nächsten Agent-Punkt aus docs/ROADMAP.md ab."
```

Bequemer: im Dashboard unter **Fahrplan** im Feld „Koordinator" schreiben
(Haken „Autopilot" für selbstständiges Arbeiten). Das startet denselben Aufruf
in der tmux-Sitzung `roadmap-lead`; alle Nachrichten gehören zu einem Gespräch.

Soll ein Lauf nichts übernehmen, die Sperren wieder mitgeben:
`--deny-tool 'shell(git push)' --deny-tool 'shell(./run.sh)'`. Übernehmen von
Hand geht weiterhin: `git merge --ff-only roadmap`, dann `git push` und
`./run.sh` (nicht zwischen 19:30 und 23:30 Uhr).

### Prompts

- Normalfall: *„Arbeite den nächsten Agent-Punkt aus docs/ROADMAP.md ab."*
- Gezielt: *„Erledige in docs/ROADMAP.md den Punkt ‚TTS-Kapitel parallel
  synthetisieren' (M4). Messe die Laufzeit vorher und nachher mit
  `btcedu regression-run`."*
- Mit Budget: *„… Du darfst dafür bis zu 5 USD bei ElevenLabs ausgeben."*
- Nur Stand: *„Wo stehen wir? Fasse docs/ROADMAP.md zusammen und nenne meine
  offenen Aufgaben."*

## Offene Entscheidungen

| Nr. | Frage | Empfehlung |
|---|---|---|
| E1 | Bleibt die Sendung eine Übersetzung der tagesschau oder wird sie aus eigenen, belegten Newsroom-Texten erzeugt? | Newsroom zuerst – senkt Rechte- und Ablehnungsrisiko, nutzt vorhandene Arbeit |
| E2 | Avatar-Option A, B oder C? | A als Pilot, B als Ziel |
| E3 | Textmodelle im Produktionspfad: Copilot CLI oder direkte API? | direkte API mit Budget |
| E4 | Domain und Markenname der Website | – |
| E5 | Ab wann `public` statt `unlisted`? | nach 7 fehlerfreien Tagen und erteilter API-Prüfung |

## Nächste Schritte

1. **Du:** YouTube-Kanal, Google-Cloud-Projekt und OAuth einrichten (M1).
2. **Du:** Entscheidungen E1–E3 treffen.
3. **Du:** einmalig `sudo -n systemctl is-active btcedu-web; echo $?` ausführen.
   Kommt nicht `0`, greift die NOPASSWD-Regel nicht und mein Deploy bricht beim
   Dienstneustart ab (statt auf eine Passwortabfrage zu warten).
4. **Du:** diesen einen Rollout noch selbst anstoßen – `git merge --ff-only roadmap`,
   `git push`, `./run.sh` im Produktions-Checkout. Erst danach starten neue
   Koordinator-Läufe mit den freigegebenen Rechten; die laufende Sitzung hat sie
   noch nicht. Beim nächsten Remote-Render prüfen, ob der Lauf gefunden wird.
5. **Agent:** KI-Kennzeichnung beim Upload umsetzen (M1), danach TTS parallelisieren (M4).
6. **Agent:** `deploy/setup-web.sh` für den unbeaufsichtigten Lauf absichern
   (`sudo -n`, `DEBIAN_FRONTEND=noninteractive`) und einen `pre-push`-Hook im
   Produktions-Checkout einrichten, der nur Commits mit maschinenlesbarer
   QA-Freigabe durchlässt. Beides vor dem ersten regelmäßigen Autopilot-Rollout.

## Einordnung älterer Pläne

| Datei | Status |
|---|---|
| `docs/plans/almanya24-growth-plan.md` (2026-10-12) | Grundlage für M3–M5; Phase 1 (Grafiken, Bewegung, Rundown, Karten) weitgehend umgesetzt |
| `docs/plans/almanya24-news-platform/` | Detailarchitektur und Arbeitspakete des Newsrooms, gilt für M2/M6 |
| `docs/plans/almanya24-media-roadmap.md`, `ai-news-anchor.md`, `almanya24-avatar-iii-recovery.md` | Avatar-Historie, umgesetzt bis auf externe Blocker |
| `PLAN.md`, `IMPROVEMENTS.md`, `TAGESSCHAU_IMPROVEMENTS.md`, `docs/plans/status-report-and-next-steps.md` | veraltet (Stand Juli/August 2026), nur Historie |

## Statuslog

| Datum | Eintrag |
|---|---|
| 2026-11-12 | Push- und Deploy-Rechte für den Koordinator freigegeben: `git push` und `./run.sh` sind erlaubt, `sudo` bleibt gesperrt (`DENIED_TOOLS = ("shell(sudo)",)`). Als Ersatz für die menschliche Vorsicht schützt sich `run.sh` jetzt selbst — Abbruch zwischen 19:30 und 23:30 Uhr (Sendefenster, Override `ALLOW_BROADCAST_WINDOW=1`) und Abbruch, wenn es aus einem verlinkten Worktree gestartet wird (dort fehlen `.venv`/`.env`, der Lauf hätte die Erstinstallation ausgelöst). Alle ausführenden `sudo systemctl`-Aufrufe laufen mit `-n`, scheitern also sofort statt auf eine Passworteingabe zu warten; die Statusabfrage braucht gar kein `sudo` mehr. 14 Tests grün (`tests/test_deploy_window.py`, `tests/test_roadmap_agent.py`), `ruff check` sauber, QA-Prüfung in zweiter Runde freigegeben. |
| 2026-11-12 | Koordinator-Testläufe im Produktions-venv wieder möglich: Die Sperre kam nicht von fehlenden Rechten, sondern von der Pfadprüfung der Copilot CLI, die `.venv/bin/python` auf sein Symlink-Ziel unter `/usr` auflöst; der Starter übergibt jetzt `--allow-all-paths` (`67268fb`). |
| 2026-11-12 | Remote-Render „did not appear within 120s" behoben (M0): Ursache war das starre Suchbudget 20×6 s in `GitHubActionsClient.find_run()` — GitHub legt den Lauf nach `workflow_dispatch` asynchron an und brauchte unter Last länger, der Pi gab zu früh auf und renderte lokal (⌀ 25 min). Jetzt Poll-Schleife mit Deadline (Default 420 s, `GITHUB_RENDER_RUN_LOOKUP_TIMEOUT`) und Backoff 3→30 s, Zuordnung über Marker plus `created_after`, transiente Listing-Fehler toleriert, Fehlermeldung nennt Repo, Workflow, Ref, Wartezeit und ob die Workflow-Datei auf dem Ref existiert. Lokaler Rückfall unverändert. 91 Tests grün (`tests/test_github_actions_service.py`, `test_remote_render.py`, `test_config.py`; 4 Flask-Importfehler umgebungsbedingt), `ruff check` sauber, QA-Prüfung freigegeben. |
| 2026-11-12 | WhatsApp wieder gekoppelt (seit 19.10. getrennt): Ursache veraltete WhatsApp-Web-Version im Dienst, jetzt live von web.whatsapp.com; abgelaufene QR-Codes werden nicht mehr angezeigt; Signal-Schlüssel nicht mehr im Systemprotokoll. Vorgemerkte Antwort zugestellt. |
| 2026-11-12 | Koordinator über WhatsApp: Selbst-Chat → `btcedu roadmap-chat inbound`, Antwort per WhatsApp, Gedächtnis `data/roadmap-chat/memory.md` (Test: Begriff „die Sendung“ nach neuem Gespräch korrekt aufgelöst), Live-Anzeige je Agent auf der Fahrplanseite. Fehlermeldungen aus (`NOTIFY_WHATSAPP_FAILURES=false`). Zustellung wartet auf neue WhatsApp-Verknüpfung. |
| 2026-11-12 | Ausgerollt: Fahrplanseite und Koordinator-Chat im Dashboard (`/dashboard/roadmap`), Agententeam unter `.github/agents/`. Koordinator-Testlauf über denselben Codepfad wie das Dashboard: Antwort nach 17 s, Exit 0, keine Dateiänderung. |
| 2026-11-12 | Bestandsaufnahme: Pipeline stabil, ≈ 3,30 USD/Folge, fertig 21:37–22:42; kein YouTube-Upload bisher; Avatar blockiert; Newsroom nur Vorschau (200 Artikel). Fahrplan angelegt, Agent-Anleitungen korrigiert, Copilot-Session-Flut behoben (`999f4fd`), 8.948 alte Pipeline-Sessions gelöscht. |
