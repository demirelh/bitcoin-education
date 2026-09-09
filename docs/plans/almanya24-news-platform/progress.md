# ALMANYA24 Newsroom – Fortschritt und Wiederaufnahme

[Gesamtplan](../almanya24-news-platform.md) | [Arbeitspakete](work-packages.md) | [Architektur](architecture.md)

## Arbeitsumgebung

- Branch: `feat/almanya24-newsroom`
- Isolierter Worktree: `/home/pi/AI-Startup-Lab/almanya24-newsroom-dev`
- Entwicklungsumgebung: `/home/pi/.venvs/almanya24-newsroom-dev`
- Ausgangscommit: `1c229ccb083a444c9b6920e3a5cd95dd0095a110`
- Produktionscheckout: `/home/pi/AI-Startup-Lab/bitcoin-education`
- Der laufende Dienst lädt aus dem Produktionscheckout und dessen `.venv`.
  Dieser Branch wird weder deployt noch vom Dienst verwendet.

## Freigabe und Grenzen

Der Betreiber hat am 09.09.2026 den gesamten Pflichtplan zur sequenziellen
Umsetzung freigegeben. Keine Zwischenfreigaben notwendig.
Erlaubt: Code, Dokumentation, isolierte Tests/Builds und lokale Commits.
Nicht erlaubt: Push, Merge, Deployment, produktive Migrationen oder
historische Datenänderungen, reale Veröffentlichungen, kostenpflichtige
Provideraufrufe, Secrets, `anchor_enabled=true` oder automatisches Publishing.

## Paketstatus

| Paket | Status | Commit | Nachweis / nächster Schritt |
| --- | --- | --- | --- |
| Planübernahme | Erledigt | `6458e61` | Acht Repositorydokumente, Links und UTF-8/NFC geprüft |
| N0a Importidentität | Erledigt | `9c89cd2` | Stream-/Editionsschlüssel, kein NULL-Kanal-Backfill, korrektes Backfillprofil |
| N0b Retention-Holds | Erledigt | `de85f64` | Gründe für Pipeline/Avatar/Upload, Dry-Run und dauerhafte Providerledger |
| N1 Redaktionelle Basis | Erledigt | `bba044e` | Immutable Quellen-/Claimrevisionen, Migration 022, atomare Budgetreservation |
| N2 Automatische Belege | Erledigt | `97052f8` | Dokumentgebundene Belege, sechs Status, Provenienzfamilien, kontrollierter Fetcher, Budget/Deadline-Stopp |
| N3 Commons und Rechte | In Arbeit | – | Vertragstests ohne echte Käufe |
| N4 Interner Artikel-MVP | Offen | – | Abschluss des funktionalen MVP |
| N5 Öffentliche Website | Offen | – | Lokal bauen; nicht veröffentlichen |
| N6 Themenlebenszyklus | Offen | – | Nach N5 |
| N7 Gemeinsamer Videopfad | Offen | – | Keine Avataraktivierung |
| N8 Bedarfsgerechter Ausbau | Offen | – | Nur konkret durch Befunde begründete Teilpakete |
| N9 Technischer Namensalias | Offen | – | Optionaler kompatibler CLI-/Metadatenumfang, keine Dienstmigration |

## Verifikationsjournal

| Zeitpunkt | Umfang | Ergebnis |
| --- | --- | --- |
| 2026-09-09 | Produktionsisolierung | `btcedu-web` arbeitet aus `/home/pi/AI-Startup-Lab/bitcoin-education`; separater Worktree und separate venv eingerichtet |
| 2026-09-09 | Planübernahme | Acht Dokumente mit gültigem UTF-8/NFC und vollständigen lokalen Links; Commit `6458e61` |
| 2026-09-09 | N0a gezielt | 139 Tests in Detector/Channel/Recorder/Failover bestanden; Ruff für betroffene Dateien sauber |
| 2026-09-09 | N0b gezielt | 12 Retentiontests sowie 128 Retention-/Importintegrationstests bestanden; Ruff sauber |
| 2026-09-09 | N1 gezielt/kompatibel | 233 Editorial-/Migration-/Config-/Retention-/Remote-Render-/Pipeline-/Webtests bestanden; Ruff sauber |
| 2026-09-09 | Vollsuite nach N1 | Nach mehr als 20 Minuten bei 1 % kontrolliert beendet; bis dahin kein Fehler, aber kein vollständiger Nachweis |
| 2026-09-09 | N2 gezielt | 88 Recherche-/Fetcher-/Such-/Editorial-/Migrations-/Configtests bestanden |
| 2026-09-09 | N2 gemeinsame Grenzen | 68 Migrations-, Retention- und Remote-Render-Tests bestanden; Ruff für alle berührten Dateien sauber |

## Entscheidungen und Plananpassungen

1. Die versionierte `.venv` im Repository ist ein Symlink auf eine alte
   Umgebung. Sie bleibt unberührt; Entwicklung nutzt `/home/pi/.venvs/...`.
2. Die ursprünglich nur im Sitzungsverzeichnis vorhandenen Planartefakte
   werden im Implementierungsbranch dokumentiert. Die Sitzungsoriginale
   werden nicht gelöscht.
3. N9 ist durch die Gesamtfreigabe nicht zu einer produktiven Umbenennung
   von systemd, Datenpfaden oder GitHub-Repository geworden. Implementierbar
   sind nur sichere Kompatibilitätsaliases und dokumentiertes Inventar,
   soweit der Paketplan dies ohne Deployment erlaubt.
4. N1 führt `newsroom_enabled=false` und den privaten Standardroot
   `data/newsroom` ein. Newsroom- und Episodenausgabe-Roots dürfen sich nicht
   überlappen; dadurch bleiben Belege außerhalb von Retention und
   Remote-Renderpaketen.
5. Providerarbeit wird vor einem Aufruf unter einem stabilen Operationsschlüssel
   reserviert. SQLite-Reservationen serialisieren Budget- und Queryprüfung mit
   einer kurzen `BEGIN IMMEDIATE`-Transaktion; es findet dabei kein Netzwerkzugriff
   statt. Reservierte, eingereichte, abgeschlossene und ungeklärte Operationen
   zählen konservativ gegen das Budget.
6. Die vollständige Suite ist auf dem Pi in diesem Lauf nicht als Paketgate
   praktikabel gewesen. Sie wird am nächsten größeren Integrationsmeilenstein
   erneut gestartet; gezielte gemeinsame Grenzen werden weiterhin pro Paket
   vollständig geprüft.
7. N2 trennt Fund und Beleg. Ein Suchtreffer ist reine Entdeckung; belegend
   ist ausschließlich eine Passage aus einer selbst geladenen Seite, die die
   Anker des Claims unverändert enthält. Damit kann kein Snippet allein zu
   `supported` führen.
8. Syndizierte Kopien teilen eine Provenienzfamilie. Mehrere Abdrucke
   derselben Agenturmeldung bleiben ein Beleg.
9. Nicht wahrheitsprüfbare Claimtypen (Zitat als Wortlaut, Meinung, Prognose,
   ASR-Verdacht) lösen keine Suche aus und werden als `unverifiable`
   festgehalten, statt Budget für scheinbare Prüfungen auszugeben.
10. Ein Budget- oder Deadlinestopp beendet den Lauf mit Status `blocked`.
    Bereits dauerhafte Bewertungen bleiben erhalten, die übrigen Claims
    bleiben unbewertet und gelten ausdrücklich nicht als geprüft.

## Wiederaufnahme

Nächster konkreter Schritt:

1. N3: assetbezogene Rechteentscheidung für Commons-Medien beginnen —
   Lizenz-/Attributionsmodell und dauerhafte Rechteentscheidung je Asset.
2. Vertragstests gegen lokale Fixtures; keine echten Käufe und keine
   Providerzugänge.
3. Fortschritt nach Abschluss von N3 hier eintragen.

Abgeschlossene Pakete werden nicht ohne neuen konkreten Befund wieder geöffnet.
