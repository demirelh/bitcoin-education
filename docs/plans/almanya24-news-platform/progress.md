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
| N1 Redaktionelle Basis | In Arbeit | – | Modelle, Migration und persistente Jobreservation |
| N2 Automatische Belege | Offen | – | Fixtures vollständig; Onlinepilot ohne Zugangsdaten offen |
| N3 Commons und Rechte | Offen | – | Vertragstests ohne echte Käufe |
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

## Wiederaufnahme

Nächster konkreter Schritt:

1. N1: vorhandene Migrations-/Modellkonventionen und Storyartefakte erfassen.
2. Minimalen redaktionellen Datenvertrag und atomare Research-Reservation umsetzen.
3. Fresh-/Upgrade-/Idempotenztests auf isolierten Datenbanken ausführen.

Abgeschlossene Pakete werden nicht ohne neuen konkreten Befund wieder geöffnet.
