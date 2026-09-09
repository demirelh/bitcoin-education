# Architekturreview: priorisierte Befunde

[Ist-Zustand](current-system.md) | [Arbeitspakete](../../plans/almanya24-news-platform/work-packages.md)

Alle Codepositionen beziehen sich auf `1c229cc`. P1 = vor Ausbau mit realen
Inhalten beheben/absichern; P2 = begrenztes Risiko oder relevante
Migrations-/Wartbarkeitsluecke. Kein P0-Ausfall festgestellt.
Die Prioritaet ist keine Aussage ueber eine ausnutzbare Sicherheitsluecke.

## A. Bestaetigte Fehler

### Abschließender Codeabgleich

Die folgenden ursächlichen Ausschnitte wurden am unveränderten
`1c229ccb083a444c9b6920e3a5cd95dd0095a110` erneut gelesen.
Die zuvor beschriebenen In-Memory-Reproduktionen wurden für diesen
Dokumentationsnachtrag nicht erneut ausgeführt.

| Befund | Konkreter Codebeleg |
| --- | --- |
| R1 | `btcedu/core/detector.py`, `detect_episodes`: `.filter(Episode.channel_id.is_(None)).update({Episode.channel_id: resolved_channel_id})` ohne Herkunftsbedingung |
| R2 | `detect_episodes`: `known_days = _titled_broadcast_days(session)`; der Helfer iteriert `session.query(Episode.episode_id, Episode.title).all()`. Der vorgelagerte Titelregex prüft neue Feedinhalte, grenzt diese globale Bestandsabfrage aber nicht ein. |
| R3 | `backfill_episodes`: `Episode(...)` setzt Kanal/Quelle/Titel/URL/Datum/Status, lässt aber `content_profile` und `pipeline_version` weg. Der normale Feed-Konstruktor setzt beide ausdrücklich. |
| R4 | `prune_expired_episodes`: einziger Hold ist `PipelineRun.status == RunStatus.RUNNING`; danach `_delete_episode_files` vor `_delete_episode_records`. Kein Avatarjob-Hold in diesem Pfad. |

Präzisierung zu R2: `_episode_days_by_source()` filtert bereits nach
Recorderquelle und optional Titelregex, aber nicht nach expliziter
Kanal-/Quellserie oder Editionsidentität. Nicht behaupten, **jeder**
Deduplizierungshelfer sei völlig ungefiltert.

### R1 / P1: Feedimport beansprucht alle bisher kanallosen Episoden

- **Ort:** `core/detector.py:225-235`, `detect_episodes`.
- **Ausloeser:** Mindestens eine Episode hat `channel_id=NULL`, waehrend
  irgendein aufloesbarer Kanal importiert wird.
- **Auswirkung:** Fremde Episoden erhalten den gerade importierten Kanal,
  unabhaengig von Quelle/Profil. Dashboardfilter und spaetere Verarbeitung
  arbeiten dann mit falscher Herkunft.
- **Beleg:** Das UPDATE filtert nur `Episode.channel_id.is_(None)`.
  Isolierte Reproduktion: kanalloser Bitcoin-Podcast wird durch leeren
  Tagesschau-Feed auf `tagesschau` gesetzt. Keine Produktionsmutation.
- **Testluecke:** Die gelesenen Kanaltests behandeln explizite Zuweisung,
  Profilprioritaet und Migration 021, nicht diesen fremden NULL-Datensatz.
- **Kleinste Loesung:** Massen-Backfill aus normalem Detect entfernen;
  Reparatur nur anhand eindeutiger Herkunft und explizitem Dry-Run-Mapping.
  Neue Episoden erhalten weiterhin ihren explizit bestimmten Kanal.

### R2 / P1: Tagesdatum verwechselt andere Quellen und andere Editionen

- **Ort:** `core/detector.py:263-295,335-351`, `_titled_broadcast_days`;
  ferner `_episode_days_by_source:354-392`; Profil
  `tagesschau_tr.yaml:ingest.title_include`.
- **Ausloeser:** Andere Sendung/Kanal mit demselben Titeldatum oder 100-Sekunden-
  und 20-Uhr-Ausgabe am selben Tag. Das Profil erlaubt beide Editionen.
- **Auswirkung:** Echte neue Nachrichtensendung wird als Duplikat verworfen;
  Reihenfolge eines Feedbatches entscheidet, welche Edition uebrig bleibt.
- **Beleg:** `_titled_broadcast_days` liest ALLE Episoden ohne Kanal-/Profilfilter.
  In-Memory: datierter fremder Podcast -> neue Hauptausgabe `new=0`;
  Batch aus zwei erlaubten Editionen -> `new=1` von 2.
- **Testluecke:** Bestehende Tests schuetzen Reuploads derselben Ausgabe,
  nicht dieses zusammengesetzte Herkunfts-/Editionsproblem.
- **Kleinste Loesung:** Kanonischen Schluessel aus Quellserie,
  Editionskennung, Sendezeit/Datum und Zeitzone bilden. Feed/Recorder derselben
  Hauptausgabe teilen ihn; andere Kanaele/Editionen nicht. Kein automatisches
  Loeschen oder Zusammenfuehren historischer Episoden.

### R3 / P1: Backfill legt Nachrichten als falsches Profil und v1 an

- **Ort:** `core/detector.py:719-731`, `backfill_episodes`;
  `models/episode.py:95-100`.
- **Ausloeser:** Backfill fuer Nachrichtenprofil mit v2-Konfiguration.
- **Auswirkung:** Kanal kann richtig sein, aber `content_profile` wird
  `bitcoin_podcast`, `pipeline_version` wird 1. Verarbeitung und Filter sind
  inkonsistent; v2-only Stufen verweigern die Episode.
- **Beleg:** Konstruktor laesst beide Felder weg.
  Gemockter Backfill mit Tagesschau/v2 ergab exakt `bitcoin_podcast, 1`.
- **Testluecke:** Bestehende Backfilltests pruefen Anzahl, Zeitfilter,
  Duplikate und Bestandsschutz, nicht Profil-/Versionskonsistenz.
- **Kleinste Loesung:** Gemeinsamer, expliziter Episode-Erzeugungsvertrag
  fuer echte Importpfade; validiertes Profil liefert Version/Profil.
  Alte v1-Daten nicht pauschal umdeklarieren.

### R4 / P1: Retention loescht die Episode eines ungeklaerten Avatarjobs

- **Ort:** `core/retention.py:79-125,156-179`;
  `models/avatar_job.py:59-115`.
- **Ausloeser:** Episode abgelaufen, kein RUNNING-PipelineRun, aber
  `reserved`, `submitted` oder `reconcile_required` im Avatarledger.
- **Auswirkung:** Episode/Dateien verschwinden, Job/Kostenreferenz bleiben
  ohne passende Produktionsbasis; Reconciliation und Belegbarkeit werden
  erschwert. Noch kein entsprechender Produktionsfall belegt: Ledger heute leer.
- **Beleg:** Schutz prueft nur PipelineRun.RUNNING, DB-Cleanup kennt die
  Avatartabellen nicht. In-Memory-Reproduktion mit Dateiloeschen als Mock:
  0 Episoden, 1 reconcile_required-Job, Dateiloescher waere einmal aufgerufen
  worden, `protected=0`.
- **Testluecke:** `tests/test_retention.py` hat drei betrachtete Faelle
  (abgelaufen, RUNNING, PermissionError), keinen ungeklaerten Providerjob.
- **Kleinste Loesung:** Retention-Hold fuer ungeklaerte Provider-/Uploadjobs;
  Aufbewahrung fuer Kosten, Audit und Publikationsbelege gesondert regeln.
  Vorhandene Datensaetze nicht blind kaskadierend entfernen.

## B. Nachgewiesene Architektur-/Policy-Luecken, nicht pauschal Bugs

| ID / Prio | Beleg und Ausloeser | Auswirkung / Testluecke | Kleinster Ausbau |
| --- | --- | --- | --- |
| A1 / P1 | `transcript_verifier.py`, `translation_qa.py:650`, `qa_reviewer.py:703`: Originalaudio/Originaltext als Bezug | Gruene QA belegt Quelltreue, nicht Ereigniswahrheit. Kein Claim-/Evidence-/Article-Modell in untersuchten Python-Modulen | Eigenstaendiger Behauptungs-/Belegpfad nach Story-Segmentierung, vor neuer redaktioneller Freigabe |
| A2 / P1 | Profil: `auto_approve_reviews=true`, Gate-Adjudikation `block_on_hold=false`; `pipeline.py:1611` | APPROVED darf oeffentlich nicht als menschlicher Faktencheck bezeichnet werden. Neue Gates duerfen keine Alt-Automatik erben | Separate manuelle EditorialDecision mit Subject-Digest und Decision-Origin; bestehende Automatik nicht global abschalten |
| A3 / P1 | `stock_images.py:1370ff,1483,1535`: Frames zuerst, statischer Pexels-Lizenztext | Keine asset-/verwendungsspezifische Rechtepruefung, kein belastbarer Lizenzbeleg. Echte Frameherkunft ist keine Nutzungsfreigabe | MediaSource/LicenseEvidence/MediaUseDecision mit Website-/Video-Kontext; unklar bleibt gesperrt |
| A4 / P1 | Profil-Bildprompt fordert photorealistische dokumentarische Nachrichtenaesthetik; keine per-Asset-Archiv-/KI-Kennzeichnung in untersuchten Renderstellen gefunden | Verwechslungsrisiko von Illustration mit Ereignisbild; keine Behauptung, jedes ausgegebene Video sei unmarkiert | Verwendungsrolle und sichtbare Kennzeichnung je Szene/Artikel; generierte Ereignisdarstellung standardmaessig ausschliessen |
| A5 / P1 | `retention.py:115-121,156ff` entfernt auch Reviews/PublishJobs/Medien, Profilfrist 10 Tage | Fuer dauerhafte Website-Artikel gehen Quellen-/Rechte-/Korrekturbelege verloren. Dateiloeschen vor DB-Commit ist nicht atomar | Dauerhafte redaktionelle Revisionen ausserhalb der Episode-Ablage; zweiphasiger Cleanup mit Schutzreferenzen |
| A6 / P1 | `copilot_fix.py:248-274` baut aus Fehlertext autonomen Reparaturauftrag mit Commit/Push/Fortsetzen und `--allow-all`; Laufzeitflag true | Untrusted Webinhalte duerfen nicht ueber Recherchefehler im Code-Reparaturpfad landen. Kein Exploitnachweis in diesem Audit | Recherchejobs explizit ohne Auto-Fix-Hook; begrenzte, typisierte Fehler. LLMs erhalten keine Ausfuehrungsrechte/Produktionsumgebung |
| A7 / P2 | `web/jobs.py:90-140` Queue/Status in RAM; DB-Stufenruns separat | Prozessrestart verliert Queue/Job-ID; neue Recherche kann teilweise bezahlt sein | Persistenter ResearchRun/ProviderOperation, Worker nimmt DB-Job wieder auf; keine Redis-/Celery-Pflicht |
| A8 / P2 | `pipeline.py:116-123` faellt bei beliebigem Profilfehler auf Default-Stufen zurueck | Unbekanntes Profil kann falschen Plan anzeigen; weitere Stufen koennen spaeter dennoch stoppen | Vor Ausfuehrung vollstaendig validierter Profilplan, sichtbarer Fehler statt stiller Default; keine neuen News-Gates im Fallback auslassen |

A6 ist eine Integrationsgrenze, kein Auftrag, in dieser Planung die
bestehende Auto-Reparatur umzustellen. Der normale LLM-Adapter ist davon
verschieden: `claude_service.py:322-337` erlaubt derzeit `view`, nicht
`--allow-all`. Fuer fremde Webseiten ist auch Lesen aus breiten Verzeichnissen
kein geeigneter neuer Analysevertrag: Eingaben explizit bereitstellen,
keine Umgebungs-/Dateizugriffe durch das Modell.

## C. Laufzeitbefunde und offene Verdachtsfaelle

| Befund | Gesichert | Nicht gesichert / naechste Untersuchung |
| --- | --- | --- |
| 184 verwaiste PipelineRuns | Read-only LEFT JOIN auf Produktions-DB | Entstehungszeit/Ursache unbekannt; aktuelle ORM-Episodebeziehung besitzt delete-orphan. Deshalb NICHT behaupten, heutige Retention habe genau diese Zeilen erzeugt |
| Transkriptcache | `transcriber.py:71-105` prueft Config-Hash und Dateien, nicht Audio-Bytes | Audioaustausch unter gleichem Pfad koennte alte ASR wiederverwenden. End-to-End-Regression mit Austausch/Force/Ruecksetzung fehlt in dieser Untersuchung |
| Review-Frische | `reviewer.py:788-894`: manche Gates pruefen nur juengsten Status; `auto_approve_stage` no-op bei bereits approved | Nach Artefaktaenderung koennen Anzeige und Freigabe auseinanderlaufen; Publisher/Render-Hashes koennen spaeter sicher stoppen. Kein bestaetigter Publish-Bypass |
| Historische Sidecars | `reviewer.py:1092-1184` schreibt reviewed-Dateien; Bestand hat mehrere Formate | Alle moeglichen manuell editierten Sidecars und invalidierenden Pfade nicht vollstaendig getestet. Im neuen Pfad nur immutable Revisionen |
| Swap nahe voll | Eine Momentaufnahme | Keine Trendmessung; keine Ursache fuer aktuelle Performance abgeleitet |

## D. Dokumentation weicht vom aktuellen Code ab

1. Root-/Testinstruktionen nennen ca. 2.655 Tests; historische Abschlusszahlen
   nennen spaetere Bestaende. Keine dieser Zahlen wurde hier als heutiger
   Suite-Nachweis verwendet.
2. `services/CLAUDE.md` und Medien-Roadmap behaupten ausstehendes
   Alpha-Studio-Compositing. `scene_renderer.py`, Studiovertraege und Profil
   haben es bereits implementiert; Aktivierung bleibt separat blockiert.
3. `docs/runbooks/news-editorial-policy.md` verlangt sichtbare ARD-/btcedu-
   Attribution und beschreibt u.a. einen reinen Translate-Gate-Pfad.
   Aktuelles Profil setzt `visible_source_attribution=false`, ALMANYA24,
   Adaptierung und Zwei-Sprecher-Skript ein.
4. Dieselbe Policy bezeichnet die Nutzung als transformative Bildungsableitung.
   Das ist kein nachgewiesener Lizenzvertrag und keine pauschale
   Rechtsgrundlage fuer Artikel oder monetarisierte Videos.
5. `_profile_pipeline_flags`-Docstring nennt Fallback `(False, True)`,
   tatsaechlicher Code liefert `(False, False)`.
6. Releasebericht formuliert breit, dass Webrequests keine Provideraktion
   ausloesen koennen. Das Dashboard kann nach Authentifizierung Jobs fuer
   Providerstufen einreihen. Richtig ist die engere Trennung:
   HTTP-Request/Renderer sollen keinen ungeprueften spontanen Providerkauf
   durchfuehren; Operatoraktionen koennen sehr wohl kostenpflichtige Jobs starten.

Kleinste Loesung: kanonische aktuelle Editorial- und Betriebsregeln mit
expliziten Overrides; Altplaene als historisch markieren. Kein globales
Dokumentations-Rewrite vor den ursachennahen Arbeitspaketen.

## E. Was erhalten werden sollte

- Profilrouting statt hartcodierter Anbieter.
- Source-Segment-IDs und Zeitbereiche als Startpunkt fuer Nachvollziehbarkeit.
- Separate Artefakt-/Reviewhashes und strikte Renderinputpruefungen.
- Avatarreservation, persistente Job-ID, Reconciliation und Budgetmodell.
- Test-/Produktionszieltrennung und manuelle Publishpflicht.
- Bestehendes Flask-Login/CSRF-Modell fuer Operatorfunktionen.
- Kleine SQLite-/Jinja-/FFmpeg-Basis statt zusaetzlicher schwerer Infrastruktur.

**Erstes empfohlenes Paket:** N0a, Importidentitaet und Zuordnung absichern
(R1-R3). Direkt danach N0b als Voraussetzung fuer den recherchierenden MVP.
Neue Inhalte auf falscher Herkunft und verlierbaren Belegen aufzubauen waere
keine risikoarme Abkuerzung.
