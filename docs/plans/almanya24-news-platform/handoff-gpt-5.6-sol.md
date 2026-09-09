# ALMANYA24 – Übergabe für GPT 5.6 Sol

[Gesamtplan](../almanya24-news-platform.md) · [Arbeitspakete](work-packages.md) · [Codebefunde](../../review/almanya24-news-platform/findings.md) · [Architektur](architecture.md) · [Fortschritt](progress.md)

## Freigabestatus

**Der gesamte Pflichtplan wurde am 09.09.2026 zur sequenziellen
Implementierung freigegeben.** Die frühere Anweisung, nach N0a zu stoppen,
ist aufgehoben. Paketgrenzen, Prüfungen und lokale Commits bleiben Pflicht.
Keine Subagents, Background Agents oder `/fleet`.

## Ausgangsbasis und Aufbewahrungsort

Repository: `/home/pi/AI-Startup-Lab/bitcoin-education`

Untersuchter Commit: `1c229ccb083a444c9b6920e3a5cd95dd0095a110`,
Branch `main`, bei Abschlusskontrolle sauber.
`origin/main` ist nur die lokal vorhandene Trackingreferenz;
in diesem Planauftrag wurde nicht gefetcht.

Der vollständige Plan wurde zu Beginn der Umsetzung in den isolierten
Implementierungsbranch übernommen. Die folgenden alten Sitzungsquellen
bleiben als unveränderter Herkunftsnachweis bestehen.

Sitzungswurzel:
`/home/pi/.copilot/session-state/c377342c-5d0b-4b01-8312-7000d1b610d6/`

Vollständige Liste der ausschließlich dort vorhandenen Dokumente:

| Relativ zur Sitzungswurzel | Inhalt |
| --- | --- |
| `plan.md` | Gemeinsamer Einstieg, Entscheidungen und Reihenfolge |
| `files/news-platform/docs/review/current-system.md` | Ist-Abläufe und getrennte Code-/Laufzeitnachweise |
| `files/news-platform/docs/review/findings.md` | Priorisierte Befunde, konkrete Codebelege, Verdachtsgrenzen |
| `files/news-platform/docs/review/research.md` | Offizielle Quellen-/Medien-/Lizenzrecherche |
| `files/news-platform/docs/plans/architecture.md` | Zielverträge, Konfigurationssemantik, Diagramme, Namensmigration |
| `files/news-platform/docs/plans/work-packages.md` | Pakete N0a–N9, Schnittstellen, Abnahme, Tests und Rückfall |
| `files/news-platform/docs/plans/handoff-gpt-5.6-sol.md` | Diese Übergabe und der exakt begrenzte erste Auftrag |

Repositoryablage dieses Branches:
`docs/plans/almanya24-news-platform.md`,
`docs/review/almanya24-news-platform/` und
`docs/plans/almanya24-news-platform/`.
Kein Push, Merge oder Deployment ist damit freigegeben.

## Gesicherte Grenzen

### Anforderungsabdeckung im vollständigen Plan

| Vereinbarter Bereich | Dokumentierter Ort / Grenze |
| --- | --- |
| Tatsächliche Einstiege, Feed/Recorder, Fehler/Retry/Änderung/Cleanup | `review/current-system.md`, Abschnitte 3–5; Implementierung und Laufzeit getrennt |
| Priorisierte Codebefunde und Testlücken | `review/findings.md`, R1–R4 und A1–A8; Verdachtsfälle separat |
| Öffentliche Marke und technische Migration | `plans/architecture.md`, Abschnitt 10; keine sofortige Umbenennung |
| Claimtypen, stabile Originalbezüge, Belege/Gegenbelege, Quellenabhängigkeit | Architektur Abschnitt 4; N1/N2 |
| DE/TR/EN, Aktualität, verschwundene Quellen, Zuschreibung und Unsicherheit | Architektur Abschnitt 4; keine Wahrheitsprozente oder Echtheitsbehauptung |
| Konkrete Medienanbieter, Lizenz/Attribution/Archiv/Illustration | `review/research.md`, Abschnitte 3/5; Architektur Abschnitt 5; N3 |
| Automatische Suche und Commons bereits im ersten MVP | N1–N4; manuelle Redaktion bleibt Freigabeinstanz |
| Eigenständige Artikel, gemeinsame Kernaussagen und getrennte Ausgabe-Gates | Architektur Abschnitte 2/6; N4/N7 |
| Website, mobile Ansicht, Quellen, Suche, SEO, RSS, stabile URLs | Architektur Abschnitt 8; N5 unmittelbar nach MVP |
| Mehrere Artikel/Sendung, mehrere Quellen/Thema, Updates/Dubletten/Korrekturen | N6; grundlegende Einzelkorrektur/Rücknahme bereits N5 |
| Alle sieben geforderten Diagrammkategorien | Diagrammregister im Gesamtplan |
| Ressourcen, Budgets, Cache, Resume, Untrusted Input | Architektur Abschnitt 9 und paketübergreifende Abnahmematrix |
| Voraussetzungen, Module, Modelle, Migrationen, Tests, Kosten und Rückfall | `plans/work-packages.md` pro Paket plus Schnittstellentabelle |
| Sichere Übergabe, Freigabesperre und Dokumentationsorte | Dieses Dokument; keine Repoänderung im Plan Mode |

Automatische **Belegrecherche und Commons-Medienauswahl sind Bestandteil
des ersten funktionalen MVP N1–N4**, nicht erst eines späteren Ausbaus.
N0a/N0b sind vorbereitende Bestandssicherungen, selbst kein Ersatz-MVP.
Ein Fixturelauf beweist den technischen Ablauf, nicht reale journalistische
Qualität. Onlinepilot nur nach Konto-/Budgetfreigabe; manuelle Belege sind
Fallback und dürfen fehlende automatische Recherche nicht kaschieren.

N4 endet mit internem, manuell freizugebendem türkischem Artikelentwurf.
N5 liefert unmittelbar anschließend die öffentliche Website.
Kein automatisches Freigeben ungeklärter Kernaussagen oder Rechte.
Eine belegte Zuschreibung kann einen engeren Claim tragen, aber nicht die
behauptete Wahrheit des zugeschriebenen Inhalts ersetzen.

`anchor_enabled` ist ein **Settings-/Env-Aktivierungsschalter für
Avatarerzeugung**, kein Schalter für Moderatorinnentext oder die gesamte
Pipeline. Profilrouting auf HeyGen/Avatar III aktiviert ihn nicht.
`auto_publish` ist dagegen ein **ContentProfile-Feld**, kein belegter
gleichnamiger `.env`-Schalter. Details in der Konfigurationstabelle der
Architektur.

Alle bestehenden ALMANYA24-Verträge bleiben: ein persistenter Look pro
Episode, Avatar III, wiederaufnehmbare Jobs ohne blinden Neukauf,
Studio mit Themenmonitor, unsichtbarer Reporter, bestehende Medienpipeline,
WebM bevorzugt/MP4-Fallback, Original-TTS als finales Audio.
`anchor_enabled=false` und Profil `auto_publish=false` bleiben unverändert.

## Konkreter Auftrag für das erste Arbeitspaket

Der folgende Auftrag ist der erste Schritt der freigegebenen Gesamtausführung:

> Implementiere ausschließlich **N0a – Importidentität und Zuordnung**.
> Lies zuerst die aktuellen Repository- und Bereichsinstruktionen sowie
> Gesamtplan, Befunde R1–R3 und N0a. Arbeite sequenziell, ohne Subagents,
> Background Agents oder /fleet.
>
> 1. Prüfe Branch, HEAD und lokale Änderungen. Bei Abweichung von der
>    dokumentierten Basis Umfang neu bewerten; nichts zurücksetzen oder
>    fremde Änderungen verwerfen. Keine automatische Git-Reparatur.
> 2. Ergänze gezielte Regressionstests für R1–R3 in den bestehenden Tests.
>    Die bisherigen In-Memory-Reproduktionen sind beschrieben, aber noch
>    keine neuen eingecheckten Regressionstests.
> 3. Entferne die herkunftslose NULL-Kanal-Massenzuweisung aus normalem
>    `detect_episodes()`. Bestehende unbekannte Herkunft nicht erraten.
> 4. Ersetze die zu grobe Tages-Deduplizierung durch einen nachvollziehbaren
>    Vergleich innerhalb derselben Quellserie und Edition. Feed und
>    Recorder derselben Ausgabe deduplizieren weiterhin in beiden
>    Reihenfolgen; verschiedene Kanäle und 100-Sekunden-/Hauptausgabe
>    dürfen sich nicht unterdrücken.
> 5. Prüfe vorher bestehende Parser in `failover/coordination.py`.
>    Keine zweite widersprüchliche Broadcastidentität einführen,
>    aber bestehende persistente Lease-/Publish-Schlüssel nicht verändern.
>    Ein Profil ist keine Quellserie. Uploadzeit ist keine sichere Sendezeit.
> 6. Sorge im Backfill für explizit korrektes validiertes Profil und
>    Pipelineversion; gemeinsame Erzeugungslogik nur soweit für die
>    Importpfade nötig. Historische v1-Episoden nicht umschreiben.
> 7. Prüfe zielgerichtet `tests/test_detector.py`,
>    `tests/test_channel_assignment.py`, `tests/test_local_recorder.py`
>    und bei gemeinsam verwendeten Parsern `tests/test_failover_pipeline.py`.
>    Ergänze Ruff und die im Paketplan verlangte Integrations-/Vollsuite-
>    Absicherung für die tatsächlich geänderten gemeinsamen Grenzen.
>    Alle Feeds/Provider mocken. Keine Tests abschwächen.
> 8. Aktualisiere nur den Fortschrittsnachweis dieses Pakets; dokumentiere
>    geänderte Dateien, Ergebnisse, bekannte Baselineprobleme und Grenzen.
>    Erstelle nach N0a einen lokalen Checkpoint-Commit, aktualisiere den
>    Fortschrittsbericht und fahre bei erfolgreicher Prüfung mit N0b fort.
>
> Keine Produktionsdatenreparatur, keine Migration/Neuinitialisierung,
> keine `.env`-/Flagänderung, keine realen HeyGen-/TTS-/Medien-/YouTube-
> oder anderen kostenpflichtigen Aufrufe; kein Deployment, Release-Tag,
> Commit oder Push ohne gesonderten Auftrag. Keine neue Artikel-/Claim-
> Funktion, kein Retention-Fix und keine technische Umbenennung in N0a.

### N0a ist erst abgeschlossen, wenn

| Fall | Erwartung |
| --- | --- |
| Kanalloser Podcast + leerer fremder Feed | Vorhandener Podcast unverändert |
| Podcast und Tagesschau mit gleichem Titeldatum | Beide vorhanden |
| Hauptausgabe und Kurzformat desselben Tages | Beide vorhanden, unabhängig von Reihenfolge |
| Feed/Recorder derselben Ausgabe | Ein logischer Import, beide Reihenfolgen |
| Reupload nach Mitternacht | Sendedatum/Edition statt Uploaddatum maßgeblich |
| Nachrichten-Backfill | Richtiges Profil und v2 ausdrücklich gesetzt |
| Historische v1-Episode | Nicht verändert |
| Unbekannte Edition | Kein unbegründetes Zusammenlegen |
| Wiederholter Import | Kein unbeabsichtigter zusätzlicher Datensatz |
| Bestehender Failover-Schlüssel | Unverändert; Tests kontaktieren keinen Control Plane |

## Aufgabenbestand: keine alten Arbeiten automatisch fortsetzen

Es gibt genau elf Nachrichten-Implementierungspakete:
`news-n0a-import`, `news-n0b-retention`, `news-n1-domain`,
`news-n2-evidence`, `news-n3-media`, `news-n4-draft`, `news-n5-site`,
`news-n6-lifecycle`, `news-n7-video`, `news-n8-expansion`, `news-n9-naming`.
N8/N9 sind Planungsblöcke für spätere einzeln freizugebende Änderungen,
keine pauschale Ausführungsfreigabe.

Die ersten zwölf Todo-Einträge der Sitzung stammen aus einem älteren
Release-/Avatar-Audit. Ihre offenen Status sind **kein Auftrag** dieser
Nachrichtenplanung. Nicht neu anlegen, nicht als erledigt behaupten und
nicht automatisch fortsetzen. Offene Altaufgaben werden als historisch
blockiert abgegrenzt; ursprüngliche Beschreibungen bleiben erhalten.
N0a ist freigegeben. N9 bleibt auf die im Paketplan festgelegte kompatible
Alias-/Inventararbeit begrenzt; keine produktive Dienstmigration.
Der schriftliche Paketplan ist die Übergabequelle; SQL ist nur Sitzungs-
Tracking und darf nicht Voraussetzung für das Verständnis sein.

## Offene Nachweise

Keine aktuelle Vollsuite und kein heutiger Onlinepilot in diesem
Planabschluss. Laufzeit-/DB-Schnappschuss stammt aus der vorherigen
lesenden Analyse; historische Testergebnisse nicht auf einen anderen
HEAD übertragen. Herkunft der 184 verwaisten PipelineRuns, vollständiges
Hostinventar und rechtliche Einzelfallprüfungen bleiben offen.
Keine Secrets benötigt oder ausgegeben.
