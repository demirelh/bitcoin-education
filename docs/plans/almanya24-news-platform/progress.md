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
| N3 Commons und Rechte | Erledigt | `8ca3146` | Assetbezogene Rechteentscheidung, Lizenzbelege, Dublettenspeicher, Widerruf; keine Altassetübernahme |
| N4 Interner Artikel-MVP | Erledigt | `5a48f8d` | Dreifachhashbindung von Text, Belegen und Medien; private Reviewfläche, benannte Freigabe |
| N5 Öffentliche Website | Erledigt | `80ccc0c` | Allowlist-DTO, atomarer Releaseswitch, Korrektur/Rücknahme; lokal gebaut, nicht veröffentlicht |
| N6 Themenlebenszyklus | Erledigt | `814f918` | Vorschläge statt Verknüpfungen, revidierbare Zusammenführung, gebündelte Neuprüfung nach Quellenänderung |
| N7 Gemeinsamer Videopfad | Erledigt | `ee10d12` | Adapter ohne Modellaufruf, getrennte Skript-/Videofreigabe, rechtekonforme Renderpakete; Avatar unberührt |
| N8 Bedarfsgerechter Ausbau | Teilweise erledigt | `16224c2` | Profilvalidierung (A8) und Quellen-/Betriebsreport umgesetzt; Anbieteradapter warten auf Vertrag und Budget |
| N9 Namensmigration | Teilweise erledigt | `d7147f9` | CLI-Alias `almanya24` auf denselben Einstiegspunkt; weitere Umbenennungen nur mit eigenem Auftrag |

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
| 2026-09-09 | N3 gezielt/gemeinsam | 151 Medien-, Recherche-, Editorial-, Fetcher-, Migrations-, Config- und Retentiontests bestanden; Ruff über `btcedu/` und `tests/` sauber |
| 2026-09-09 | N4 gezielt | 152 Newsroomtests (Artikel-, Web-, Medien-, Recherche-, Migrations-, Configtests) bestanden |
| 2026-09-09 | N4 Grenzen | 191 Migrations-, Auth- und Dashboardtests sowie 83 Retention-, Remoterender- und Prefixtests bestanden; Ruff sauber |
| 2026-09-09 | N5 gezielt | 161 Newsroomtests inklusive 28 Public-/Release-Tests bestanden |
| 2026-09-09 | N5 Grenzen | 31 Migrations-, CLI- und Retentiontests bestanden; Ruff über `btcedu/` und `tests/` sauber |
| 2026-09-09 | N6 gezielt | 217 Newsroomtests bestanden, davon 33 zu Themengraph, Neuprüfung und Operator-CLI |
| 2026-09-09 | N9 Schritt 1 | 5 Alias-Tests bestanden; Ruff sauber |
| 2026-09-09 | N8 gezielt | 300 Newsroomtests bestanden, davon 17 zur Profilvalidierung und 12 zum Report |
| 2026-09-09 | N8 Grenzen | 327 Tests zu Migrationen, Retention, Remote-Render, Weblogin, Dashboard, Pipeline, Pipeline-CLI und tagesschau-Fluss bestanden; Ruff sauber |
| 2026-09-09 | N7 gezielt | 271 Newsroomtests bestanden, davon 29 zu Videoedition, Rechtehinweisen und Freigaben |
| 2026-09-09 | N7 Grenzen | 320 Tests zu Migrationen, Retention, Remote-Render, Weblogin, Dashboard, Pipeline und Pipeline-CLI bestanden; Ruff sauber |
| 2026-09-09 | N6 Grenzen | 264 Tests zu Migrationen, Retention, Remote-Render, Weblogin, Dashboard und Pipeline-CLI bestanden; Ruff sauber |

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
11. N3 trennt Bytes, Angebot, Lizenzbeleg und Verwendungsentscheidung. Ein
    Bild wird einmal gespeichert, mehrere Spiegel mit unterschiedlichen
    Lizenzangaben bleiben getrennt nachvollziehbar.
12. Rechte- und Kontextprüfung liegt vor jedem Ranking. Eine unklare oder
    nicht kommerzielle Lizenz und ein falscher Ereignisbezug sind
    Ausschlussgründe, die durch Platzierung nicht heilbar sind.
13. Mediendownloads nutzen denselben kontrollierten Fetcher wie Belegseiten.
    Ein zweiter Transportweg würde die Redirect-Neuprüfung erneut gefährden.
14. Bestehende Pexels-/Frameassets werden nicht in das Rechteledger
    übernommen; sie tragen keinen Beleg und dürfen nicht als freigegeben
    erscheinen.
15. Eine Artikelrevision ist über Text-, Beleg- und Medienhash zugleich
    eindeutig. Der Texthash allein hätte eine Neufassung nach entzogenem Bild
    mit der bereits geprüften Fassung kollidieren lassen.
16. Zahlen- und Zitatprüfung bezieht sich auf die gesamte Revision, nicht auf
    die Claims eines einzelnen Absatzes. Eine Überschrift darf eine Zahl
    wiederholen, die der Fließtext belegt.
17. Die Freigabe hat bewusst keinen automatischen Pfad. Die Autoapprove-
    Einstellung der Profile gilt nur für die Videopipeline; ein Test prüft die
    Abwesenheit dieses Pfades im Quelltext.
18. JSON-Routen der Redaktion liegen unter `/api/editorial`, die Seite unter
    `/editorial`. Nur der `/api/`-Pfad liefert einem maschinellen Aufruf 401
    statt einer Weiterleitung in das Loginformular.
19. Kein Redaktionsendpunkt steht auf der Loginfreiliste; es gibt hinter der
    Fläche keinen Export, keinen Upload und keinen Generierungsjob.
20. Öffentliche Ausgabe läuft ausschließlich über Allowlist-Dataclasses. Ein
    nicht dort benanntes Feld kann keine Seite, keinen Feed und keinen
    Suchindex erreichen, auch nicht nach einer späteren Spaltenerweiterung.
21. Belege und Rechte werden zum Buildzeitpunkt erneut geprüft. Ein Artikel mit
    entzogener Lizenz oder abgeschwächter Bewertung fällt aus dem Build heraus,
    statt veraltet ausgeliefert zu werden.
22. Ein Release wird in ein unreferenziertes Verzeichnis geschrieben; der
    Wechsel erfolgt über `os.replace` auf einen Symlink. `reconcile` glaubt dem
    Zeiger, nicht der Datenbank, weil Leser dem Zeiger folgen.
23. Ein Thema behält dauerhaft eine Adresse. Eine neue Fassung unter derselben
    URL verlangt eine lesbare Korrekturbegründung; eine Rücknahme liefert einen
    Tombstone und verschwindet aus Sitemap, Feed und Suchindex.
24. Bauen und Veröffentlichen sind manuelle CLI-Befehle. Standardbasis-URL ist
    ungültig, Impressums-, Datenschutz- und Rechtetexte bleiben leer statt
    erfunden.
25. Eine wiederkehrende Sendung erzeugt einen Vorschlag, keine Verknüpfung.
    Verlangt sind eine Stichwortüberschneidung und ein zweites unabhängiges
    Signal; Datum oder Beteiligte allein genügen nie.
26. Der Themenname selbst zählt nicht doppelt: Entitätsbegriffe werden aus der
    Stichwortmenge abgezogen, sonst bestätigte ein Ortsname sich selbst.
27. Eine Zusammenführung löscht kein Thema. Welche Quellenverknüpfungen sie
    angelegt hat, steht ausdrücklich im Merge-Datensatz, damit eine Rücknahme
    genau diese entfernt und später von Hand ergänzte stehen lässt.
28. Veröffentlichte Artikel führen ihre Abhängigkeiten mit. Eine geänderte
    Quelle, Bewertung oder Mediendatei stößt eine begrenzte Neuprüfung an;
    die Fan-out-Grenze verhindert, dass eine Massenänderung die Redaktion
    unter Aufgaben begräbt.
29. Eine Neuprüfung kann nur `clear`, `review_requested` oder `blocked`
    melden. Sie kann nichts freigeben und nichts zurückziehen: Freigabe und
    öffentliche Rücknahme bleiben benannte Entscheidungen eines Menschen.
    Ein Test liest den Quelltext, um die Abwesenheit eines solchen Pfades zu
    belegen.
30. Der Belegcache löst zuerst die Datei, dann den Datenbankverweis. Ein
    Absturz dazwischen hinterlässt einen Cachefehltreffer statt eines
    Datensatzes, der auf fehlende Bytes zeigt.

31. Der Videoadapter ruft kein Modell auf. Er ordnet freigegebenen Text
    Sprecherrollen zu; erfundene Übergangssätze gäbe es sonst ohne Prüfung.
    Dramaturgie darf abweichen, Substanz nicht.
32. Eine Edition trägt Text-, Beleg- und Medienhash des Artikels. Eine
    abgeschwächte Bewertung oder ein zurückgezogenes Bild nimmt die
    Skriptfreigabe wieder weg.
33. Nicht-Ereignisbilder tragen einen sichtbaren Hinweis (`ARŞİV`,
    `SEMBOL GÖRSEL`). Ein Archivbild, das wie das Ereignis aussieht, täuscht
    ohne einen einzigen falschen Satz.
34. Skriptfreigabe und Freigabe der fertigen Datei sind zwei Entscheidungen
    zweier benannter Personen. Keine davon lädt etwas hoch; ein Test liest das
    Modul als Syntaxbaum, damit kein Bezeichner zu einem Publisher führt.
35. Renderpakete enthalten Bilder und Ton, niemals Rechercheunterlagen oder
    Lizenzkorrespondenz. Der entfernte Renderer liegt außerhalb der
    Vertrauensgrenze der Redaktion.
36. Das neue Routing ist aus, solange ein Profil es nicht unter
    `stage_config.editorial_video.enabled` einschaltet. Bestehende
    Produktionsprofile adaptieren weiterhin die Originalsendung.

37. Ein v2-Lauf bricht vor der ersten Stufe ab, wenn sein Profil fehlt,
    falsch geschrieben oder strukturell unbrauchbar ist. Der bisherige stille
    Standardplan erzeugte ein fertiges Video, das niemand angefordert hatte.
    Legacy-v1-Folgen bleiben ausgenommen; sie sind älter als das Profilrouting.
38. Nicht ladbare Profildateien werden in `ProfileRegistry.load_errors`
    festgehalten. Eine still fehlschlagende Datei ist sonst nicht von einer
    nie geschriebenen zu unterscheiden.
39. Unbekannte `stage_config`-Abschnitte gelten als Fehler. Ein Tippfehler im
    Schlüssel ist der übliche Weg, auf dem ein Profil unbemerkt wirkungslos
    wird; ein Test hält die bekannte Liste an den ausgelieferten Profilen fest.
40. Der Report ruft nichts ab, er zählt vorhandene Zeilen. Seine einzige
    Wertung betrifft Unabhängigkeit: Belege aus derselben Provenienzfamilie
    sind eine Quelle, gleich wie viele URLs sie trug — sonst sieht eine
    Agenturmeldung wie Übereinstimmung mehrerer Zeugen aus.
41. Die in N8 ebenfalls genannten Anbieteradapter (z. B. Openverse, EU AV)
    bleiben offen. Sie verlangen Vertrag und Kostenfreigabe; ohne beides wäre
    jede Anbindung eine unbeauftragte Ausgabe.

42. `almanya24` und `btcedu` sind derselbe Einstiegspunkt, nicht zwei
    gleichwertige Kommandos: eine Datenbank, ein Lock, eine Timerfamilie.
    Paket-, Pfad- und Dienstnamen bleiben unverändert, bis ein einzelner
    Schritt gesondert beauftragt ist — ein Pfadumzug entwertet gespeicherte
    Artefakte still.

## Wiederaufnahme

Nächster konkreter Schritt:

1. Alle Pakete N0–N9 sind umgesetzt, soweit sie ohne Anbietervertrag,
   Deployment oder gesonderten Umbenennungsauftrag umsetzbar sind.
2. Offen aus N7, falls später gebraucht: ein synthetischer, kostenfreier
   Video-Smoke-Test über den Editionspfad und die Anbindung der Edition an
   eine konkrete Episode (`VideoEdition.episode_id` ist vorgesehen, wird
   bisher nicht gesetzt). Avatar-Aktivierung bleibt außerhalb des Umfangs.
3. Offen aus N5, falls später gebraucht: clientseitige TR-Suche über
   `arama/index.json`, Related-Links und responsive Bildvarianten. Die
   Datenfelder dafür bestehen bereits.
4. Offen aus N6, falls später gebraucht: eine Weboberfläche für Vorschläge,
   Zusammenführungen und die Problemliste. Bedienbar ist beides bereits über
   `btcedu topics` und `btcedu recheck`.
5. Offen aus N8: je ein Anbieteradapter pro Folgepaket, erst nach Vertrag
   und Budgetfreigabe; ausserdem Qualitätsauswertung anhand annotierter
   Fehlklassifikationen, sobald echte Betriebsfälle vorliegen.
6. Offen aus N9, jeweils gesondert zu beauftragen: Importkompatibilität,
   Dienste-/Timer-Alias und ein hash-sicherer Pfadumzug. Ohne Auftrag bleibt
   es beim CLI-Alias.
7. Nächster sinnvoller Schritt insgesamt: ein realer Trockenlauf des
   Redaktionspfades auf gespeicherten Daten (Report lesen, Vorschläge und
   Neuprüfungen bedienen), bevor weitere Funktionen ergänzt werden.

Abgeschlossene Pakete werden nicht ohne neuen konkreten Befund wieder geöffnet.
