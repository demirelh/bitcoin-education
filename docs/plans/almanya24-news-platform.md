# ALMANYA24 Newsroom: Bestandsaufnahme und Weiterentwicklungsplan

**Status: Gesamtplan am 09.09.2026 zur sequenziellen Implementierung freigegeben.**
Stand der Untersuchung: 09.09.2026.

## Abschließendes Planreview und Übergabe

Der Plan wurde nochmals am unveränderten HEAD geprüft und präzisiert:
`anchor_enabled` als Settings-/Env-Schalter von profilgesteuertem
HeyGen-Routing und `auto_publish` getrennt; vorhandene Failover-
Broadcastidentität in N0a berücksichtigt; konkrete Paketübergaben ergänzt;
statischen Suchindex statt zusätzlicher MVP-FTS klargestellt; kein
Takecache durch die spätere Videoanbindung eingeführt.

Die frühere Beschränkung auf N0a wurde am 09.09.2026 aufgehoben.
Alle Pflichtpakete werden sequenziell umgesetzt; als optional bezeichnete
Erweiterungen bleiben bedarfs- oder freigabeabhängig.
Die [vollständige Übergabe](almanya24-news-platform/handoff-gpt-5.6-sol.md)
enthält Ausgangsbasis, Grenzen und ersten Arbeitsschritt.
Der aktuelle Umsetzungsstand steht im
[Wiederaufnahmebericht](almanya24-news-platform/progress.md).

## Ergebnis in Kuerze

**Das vorhandene Projekt ist als Grundlage geeignet; ein kompletter Neubau
ist nicht begruendet.** Es ist heute eine profilgesteuerte Videopipeline
mit brauchbaren Story-, Medien-, Review- und Rendervertraegen, aber noch
keine allgemeine Faktenpruefungs- oder Artikelplattform.

Empfehlung: **ALMANYA24** als Marke behalten, die technische Zielplattform
**ALMANYA24 Newsroom** nennen und eine versionierte redaktionelle Domaene
neben dem vorhandenen Episodenmodell ergaenzen. Automatische Recherche
liefert dokumentgebundene Belege, keine Wahrheitsgarantie. Commons liefert
Bildkandidaten, keine pauschale Rechtefreigabe. Die manuelle Redaktion
entscheidet auf der konkreten Themen-/Claim-/Medienrevision.

Zuerst ein Thema als internen tuerkischen Artikelentwurf durchgaengig
bearbeiten; direkt danach eine schlanke **statische Nachrichtenwebsite
aus Jinja2-Exporten hinter Caddy**, strikt getrennt vom Operator-Dashboard.
Gemeinsame Videoausgabe aus derselben redaktionellen Basis folgt spaeter.
Kein neues schweres Webframework, CMS oder Queuecluster im MVP.

## Vollstaendige Dokumentation

Die geprüften Sitzungsdokumente wurden zu Beginn der Umsetzung in diesen
Branch übernommen. Die ursprünglichen Sitzungsdateien bleiben unverändert
als Herkunftsnachweis erhalten.

| Dokument | Inhalt |
| --- | --- |
| [1. Ist-Zustand](../review/almanya24-news-platform/current-system.md) | Einstiegspunkte, Laufzeitbefund, Kanal/Profil, Feed und Recorder bis Publish, Fehler/Retry/Eingriffe/Cleanup, DB/Artefakte/Kosten |
| [2. Priorisiertes Review](../review/almanya24-news-platform/findings.md) | Vier reproduzierte Fehler mit Ort/Ausloeser/Wirkung/Testluecke/Fix, acht Architektur-/Policy-Luecken, getrennte Verdachtsfaelle und Dokumentationsdrift |
| [3. Quellen- und Medienrecherche](../review/almanya24-news-platform/research.md) | Offizielle Brave-/Tavily-/Commons-/Institutionen-/Pexels-Dokumentation, Preise/Limits/Rechte und Grenzen; ensonhaber als Strukturreferenz |
| [4. Zielarchitektur](almanya24-news-platform/architecture.md) | Claims/Evidenz/Medien/Artikel/Video, Revisionen/Freigaben/Korrekturen, Publictrennung, Kosten/Untrusted Input, Namensmigration |
| [5. Arbeitspakete](almanya24-news-platform/work-packages.md) | Reihenfolge, Voraussetzungen, Module/Modelle/Migrationen, Abnahme/Tests/Kosten/Risiken/Rueckfall je Paket |
| [6. Übergabe für GPT 5.6 Sol](almanya24-news-platform/handoff-gpt-5.6-sol.md) | Ausgangsbasis, unveränderliche Grenzen und erster konkreter Arbeitsschritt |
| [7. Fortschritt und Wiederaufnahme](almanya24-news-platform/progress.md) | Paketstatus, Commits, Tests, Entscheidungen, Blocker und nächster Schritt |

### Diagrammregister

| Geforderte Darstellung | Fundstelle |
| --- | --- |
| 1. Bestehende Systemarchitektur | Ist-Zustand, Abschnitt 3 |
| 2. Bestehender fachlicher Pipelinefluss | Ist-Zustand, Abschnitt 4, zwei lesbare Teilgraphen |
| 3. Vorgeschlagener Gesamtfluss | Zielarchitektur, Abschnitt 3 |
| 4. Faktenpruefungsentscheidung | Zielarchitektur, Abschnitt 4 |
| 5. Medienauswahlentscheidung | Zielarchitektur, Abschnitt 5 |
| 6. Zustaende, Freigaben, Korrekturen | Zielarchitektur, Abschnitt 6 |
| 7. Bestehende und vorgeschlagene Datenmodelle | Zielarchitektur, Abschnitt 7, getrennte ER-Diagramme |

Unter den Diagrammen stehen bestehende Datei-/Funktionszuordnungen
beziehungsweise neue Komponenten und Reviewfragen.

## Belastbarer aktueller Stand

Untersuchungsbasis: Repository `~/AI-Startup-Lab/bitcoin-education`,
Branch `main`, sauberer Worktree,
**`1c229ccb083a444c9b6920e3a5cd95dd0095a110`**.
Lokaler Remote-Tracking-Vergleich `0 0`; kein Fetch im Planauftrag,
also keine frische Aussage zum GitHub-Serverstand.

Die Berichte unterscheiden:

- **I:** im aktuellen Code untersucht;
- **R:** aktuell lesend zur Laufzeit festgestellt;
- **D:** implementiert, aber deaktiviert;
- **Z:** vorgeschlagener Zielzustand;
- **U:** unbekannt oder nicht erneut geprueft.

Read-only festgestellt: DB `quick_check=ok`, 21 Migrationen,
23 Podcast- und 10 Tagesschau-Episoden; Web/Caddy aktiv, Health/Login 200,
anonyme Episoden-API 401. Die vorhandenen 184 verwaisten PipelineRuns
sind ein gesicherter Datenbefund, ihre Entstehungsursache ist ungeprueft.

`anchor_enabled=false` und `auto_publish=false` sind erhalten.
Die bestehende Reviewautomatik und automatische Code-Reparatur sind
dagegen aktiviert; **der neue redaktionelle Pfad darf beides nicht erben**.
Ein heutiges APPROVED bedeutet nicht automatisch menschlicher Faktencheck.

Keine heutigen Vollsuite-/Ruff-/Visual-/Authentifizierungsnachweise aus
historischen Abschlussberichten abgeleitet. Kein Deployment, keine
Migration, keine Konfigurations-/Dienst-/Produktionsdatenaenderung,
kein Commit/Push, keine kostenpflichtige API und keine Veroeffentlichung
fuer diese Planung ausgefuehrt. Bestehende Timer wurden nicht angehalten;
unabhaengige automatische Betriebsvorgaenge sind damit nicht ausgeschlossen.

## Wichtigste Befunde

| Prioritaet | Befund | Konsequenz |
| --- | --- | --- |
| P1 / R1 | Feed-Detect weist alle NULL-Kanal-Episoden dem importierten Kanal zu | Herkunft kann fremd ueberschrieben werden |
| P1 / R2 | Globale Datums-Deduplizierung verwechselt Kanaele und erlaubte Editionen | Legitime Sendungen werden ausgelassen |
| P1 / R3 | Backfill laesst Profil/Version weg | Nachrichten werden als Bitcoin/v1 angelegt |
| P1 / R4 | Retention schuetzt ungeklaerte Avatarjobs nicht | Episode/Dateien koennen vor Reconciliation verschwinden |

Alle vier wurden isoliert mit gemockten Quellen und In-Memory-DB
reproduziert, nicht an Produktionsdaten veraendert.
Detailbelege und bestehende Testluecken:
[Review](../review/almanya24-news-platform/findings.md).

Daneben fehlen heute allgemeine Webevidenz, dauerhafte Medienrechte und
Artikel-/Korrekturrevisionen. Das sind Ausbauanforderungen, nicht
nachtraeglich als vorhandene Funktionen zu verkaufen.
Ein pauschaler Publish-Bypass oder die Ursache der 184 Waisen wurde
nicht bewiesen und wird deshalb nicht behauptet.

## Produkt- und Architekturentscheidungen

Bereits mit dem Betreiber geklaert:

1. Marke ALMANYA24; Deutschland und relevante Weltmeldungen fuer
   Tuerkischsprachige, ohne technische Themenbeschraenkung.
2. MVP recherchiert **automatisch** Belege und Commons-Medien.
   Manuelle Fundstellen bleiben Fallback, nicht Ersatz fuer diese Funktion.
3. MVP endet bei interner Vorschau/manueller Freigabe.
   Oeffentliche Website ist das unmittelbare Folgepaket.
4. Keine Pressebildabos als Voraussetzung; bei ungeeigneten Bildern
   gekennzeichnete passende Alternative oder bewusst kein Bild.

Vorgeschlagene technische Entscheidungen:

- Immutable SourceSpan-/Claim-/Evidence-/MediaUse-/Editorialrevisionen.
  Identitaet und Content-Hash getrennt, Freigaben immer revisionsgebunden.
- Gesicherte Kernaussagen und passgenaue Rechte vor Darstellung;
  Quellenbeleg nicht mit Echtheit des Originalmaterials verwechseln.
- Eigene TR-Artikel statt Volluebersetzung der Sendung.
- Publicexport nur aus allowlist-basierten freigegebenen DTOs;
  keine private Datenbank oder API oeffentlich.
- Episodeunabhaengige Beleg-/Rechteablage, begrenzter persistenter
  Rechercheworker, reservierte/unklare Calls im Budget.
- Daten-only-Verarbeitung fremder Dokumente, kein Toolzugriff/Auto-Fix
  aus Webseiteninhalten, kontrollierter Fetcher.
- Spaetere technische Namen: Repo/Distribution `almanya24-newsroom`,
  Paket/CLI `almanya24`; bestehende Namen/Pfade vorerst behalten.
  Keine Domain-/Markenverfuegbarkeit behauptet.

## Geplanter Ablauf

| Paket | Abgeschlossenes Ergebnis |
| --- | --- |
| **N0a** | Sichere Kanal-/Profil-/Editionszuordnung, Regressionen R1-R3 |
| N0b | Retention-Holds fuer ungeklaerte Provider-/Uploadjobs |
| N1 | Versionierte redaktionelle Datenbasis und persistente Recherchejobs |
| N2 | Automatische Belegsuche, Passagevergleich, Gegenbelege und Claimstatus |
| N3 | Commons-Suche, assetbezogene Rechte und nachvollziehbare Auswahl |
| **N4** | **Durchgaengiger interner Artikel-MVP mit manueller Freigabe** |
| **N5** | **Direkt anschliessend oeffentliche statische Website** |
| N6 | Mehrquellenthemen, Update-/Deduplogik und skalierte Korrekturen |
| N7 | Videoskript/Medien aus derselben redaktionellen Freigabe |
| N8 | Bedarfsgerechter Quellen-/Betriebsausbau, in einzelne Folgepakete zerlegen |
| N9 | Unabhaengige schrittweise technische Namensmigration mit Kompatibilitaet |

Abnahmekriterien und Grenzen je Paket stehen im
[ausfuehrbaren Detailplan](almanya24-news-platform/work-packages.md).
Keine Einfuehrung aller Pakete in einer einzigen Implementierungssitzung.

## Verbleibende Unsicherheit und Entscheidungen

Fuer die Planung ist keine weitere Zustimmung zur erlaubten Analyse noetig.
Vor spaeterem Onlinepilot: Suchkonto/Tarif und Budget bestaetigen
(Brave bevorzugte Evaluation, Tavily Basic Alternative).
Vor Publicbetrieb: Domain/Host, redaktionelle Verantwortlichkeit,
rechtliche Seiten und konkreter Nutzungsumfang.
Sonderlizenzen/Pressebilder nur entscheiden, wenn tatsaechlich benoetigt.

Offen bleiben u.a. vollstaendiges Hostinventar fuer Umbenennung,
rechtliche Einzelfallpruefung, visuelle Website-/Videoabnahme,
aktuelle Vollsuite, Ursache historischer DB-Waisen und die ausdruecklich
als Verdacht markierten Cache-/Reviewfaelle. Diese Punkte verhindern
keine ehrliche Architekturplanung, aber auch keine stillen Produktions-
oder Rechtssicherheitszusagen.

**Ausführung:** N0a zuerst vollständig implementieren und prüfen; danach
ohne erneute Freigabefrage in der dokumentierten Abhängigkeitsreihenfolge
fortfahren. Paketgrenzen und lokale Checkpoint-Commits bleiben erhalten.
