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

**Unabhängiger Abgleich:** Die früheren Vollständigkeitsbehauptungen wurden
nicht übernommen. Insbesondere N7 war nur ein isolierter Adapter.
Die [Code-/Abnahmematrix](../../review/almanya24-news-platform/implementation-audit.md)
unterscheidet Anfangsstand, Korrekturen und externe Voraussetzungen.
Aktueller Prüfcommit: `e10767bc64e40d36f9434048bee5955e24887ce8`
(mit den Integrationskorrekturen aus `f615960` und `1cdf948`).
Die abschließende isolierte Vollsuite ist grün: **4229 bestanden,
0 fehlgeschlagen, 0 übersprungen**, 60 Deprecation-Warnungen in
**3972,90 s (1:06:12)**. Der Code blieb während des gesamten Laufs unverändert.
Der reale synthetische ffmpeg-Durchlauf ist ausgeführt; externe Antworten
und Sprache sind Fixtures, der manuelle Publishnachweis ist ausdrücklich Dry-Run.

| Paket | Status | Commit | Nachweis / nächster Schritt |
| --- | --- | --- | --- |
| Planübernahme | Erhalten und unabhängig abgeglichen | `6458e61`, Audit | Originalbasis und tatsächlicher Diff statt Übernahme des Abschlussberichts |
| N0a Importidentität | Lokal umgesetzt, gezielt bestätigt | `9c89cd2`, `f615960`, `e10767b` | Explizit fremde Kanäle nicht durch Titelähnlichkeit dedupliziert; historischer Profildefault darf Sendungs-/Editionsidentität nicht verwerfen |
| N0b Retention-Holds | Lokal umgesetzt | `de85f64`, `f615960` | Pipeline-/Avatar-/Uploadledger sowie gebundene Videoeditionen geschützt |
| N1 Redaktionelle Basis | Lokal verbunden | `bba044e`, `f615960` | Revisions-/Spanmodelle und atomare Reservation im echten Workflow |
| N2 Automatische Belege | Lokal verbunden, Anbieterantworten simuliert | `97052f8`, `f615960` | Toolfreier Caller, automatische Queries, echte Dokumentpassagen, semantischer Prüfauftrag und Budgetresume |
| N3 Commons und Rechte | Lokal verbunden, Livebildabnahme extern | `8ca3146`, `f615960` | Katalog-/Asset-/Rechteprüfung; zusätzliche Video-/Profilentscheidung |
| N4 Interner Artikel-MVP | Lokal verbunden, Livequalität extern | `5a48f8d`, `f615960` | TR-Entwurf/Konsistenzauftrag; tatsächlicher Text und vollständige fachliche Zustände hashgebunden |
| N5 Öffentliche Website | Lokal gebaut und geprüft, nicht öffentlich | `80ccc0c`, `f615960` | Suche/Related, Public-only DTO, byte- und freigabefrischer Releaseswitch |
| N6 Themenlebenszyklus | Lokal verbunden | `814f918`, `f615960` | Updateentscheidung vor Doppelentwurf; Änderungsnachweis für Entwurf/Website/Edition, dauerhafter Fanoutfehler |
| N7 Gemeinsamer Videopfad | Tatsächliche Pipelineanbindung | `f615960`, `1cdf948` | Episodebindung, echte TTS-/Renderfunktionen, sichtbare Credits, Bytefreigabe und separater manueller Dry-Run-Publisher |
| N8 Bedarfsgerechter Ausbau | Freigegebener lokaler Umfang umgesetzt | `16224c2`, `f615960` | Profilfehler vor Stufenstart, Report ohne Volltextladen, Ist-/Schätzkosten; weitere Adapter nur bei belegtem Bedarf |
| N9 Namensmigration | Lokaler Kompatibilitätsschritt umgesetzt | `d7147f9`, `f615960` | Installierte Aliases, echter Prozesslock, Repo-/Hostinventar; kein beauftragter Infrastrukturumzug |

## Verifikationsjournal

**Unabhängiger Auditlauf (abgeschlossen):** 318/331 im ersten verbreiterten Lauf;
danach 147/152 und 45/46 in gezielten Nachläufen. Die dabei gefundenen
Regressionen betrafen falsche Modellfeldannahmen im neuen Report,
veraltete Fixture-Freigaben, ungenaue Katalogdatierungen und veraltete lokale
Paketmetadaten. Sie wurden korrigiert, nicht als erfolgreiche Abnahme gezählt.
Der letzte verbleibende Fehler war die nur im Test fehlende
`schema_migrations`-Tabelle. Danach **582 Tests bestanden** (393,87 s),
anschließend **77 Tests bestanden** (118,47 s) für die letzten
Änderungshooks/Videoverträge. Zwei neue Tests für Wiederholungsmeldung
und echten Operatoreinstieg mit automatisch gebildeten Suchqueries bestanden
ebenfalls.
Der erste Vollsuite-Anlauf auf `f615960` wurde bei der synthetischen
Bestands-Bulletinprüfung (2 %) gezielt beendet, weil beim weiteren
Schnittstellenabgleich noch die Zuordnung der neuen Finalfreigabe zum
manuellen Publisher korrigiert werden musste. Das war kein Ressourcenfehler.
Danach bestanden **93 Integration-/Publisher-/Bestandsgatetests** (95,52 s).
Die vollständigen Läufe benötigten jeweils etwa eine Stunde. Die ersten
rund 30 Minuten entfielen weitgehend auf `test_almanya24_e2e.py`, dessen
Bestandsfälle wiederholt echte synthetische ffmpeg-Welten aufbauen.
Die geringe anfängliche Prozentzahl war kein Stillstand oder Heap-/OOM-Fehler.
Die neue `test_editorial_integration.py` führt den realen Renderer aus,
prüft sichtbare Pixel der eingebrannten Credits, unveränderte TTS,
bytegebundene Finalfreigabe sowie öffentliche und Remote-Allowlists.

Der erste vollständige Lauf auf `1cdf948` endete nach **1:08:56**:
**4226 bestanden**, drei Fehler in
`TestLocalRecordingIsSupersededByFeed`, 60 Deprecation-Warnungen.
Die neue unbedingte Profil-Mismatch-Sperre ignorierte das historische
Standardprofil alter, kanalunzugeordneter Tagesschau-Folgen.
Die Profil-Mismatch-Sperre wurde entfernt: ein bekannter abweichender Kanal
bleibt ausschließend, bei historischem Profildefault zählt weiterhin die
explizite Sendung/Edition mit Sendedatum. Keine Bestandsdaten werden verändert.

Der korrigierte Commit `e10767b` bestand zuerst alle **134 gezielten
Recorder-/Detector-/Kanaltests** (42,98 s), danach **alle 4229 Tests**
(3972,90 s). Keine Fehler, keine übersprungenen Fälle, kein Ressourcenabbruch.
Die 60 Warnungen betreffen Pillow-Pixelzugriff, SQLite-Datetimeadapter und
zwei bestehende Fork-Locktests. Alle Testprozesse dieses Auditlaufs sind beendet.
Temporäre Konsolenlogs werden nach Übernahme der Ergebnisse entfernt.

Reproduktionsbefehl, ausschließlich in der isolierten Entwicklungsumgebung:

```bash
nice -n 10 /home/pi/.venvs/almanya24-newsroom-dev/bin/python \
  -m pytest -v -p no:cacheprovider -o faulthandler_timeout=180 --tb=short
```

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
6. Die frühe Annahme, die Vollsuite sei auf dem Pi nicht praktikabel,
   wurde durch den Audit widerlegt. Sequenzielle Läufe brauchen rund eine
   Stunde; der abschließende Lauf auf `e10767b` besteht vollständig.
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
34. Skriptfreigabe und Freigabe der fertigen Datei sind getrennte, benannte
    Entscheidungen. Keine davon lädt etwas hoch; ein Test liest das
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
41. Zusätzliche N8-Adapter benötigen laut Plan einen konkreten ungelösten
    Fall und einen entsprechend geschnittenen Folgeauftrag. Openverse/EU AV
    verlangen nicht pauschal einen bezahlten Vertrag; Implementierung und
    Fixturetests wären ohne Beschaffung möglich. Ein solcher konkreter
    zusätzlicher Bedarf wurde hier nicht festgestellt. Reale Pressebildrechte
    und Liveanbietertarife bleiben gesondert freizugeben.

42. `almanya24` und `btcedu` sind derselbe Einstiegspunkt, nicht zwei
    gleichwertige Kommandos: eine Datenbank, ein Lock, eine Timerfamilie.
    Paket-, Pfad- und Dienstnamen bleiben unverändert, bis ein einzelner
    Schritt gesondert beauftragt ist — ein Pfadumzug entwertet gespeicherte
    Artefakte still.

## Abschluss und Wiederaufnahmegrenze

Der freigegebene, lokal ausführbare Implementierungsumfang ist abgeschlossen
und gegen den tatsächlichen Code geprüft. Episodebindung und synthetischer
Video-Smoke waren fehlende Pflichtintegration, keine optionalen Liveblocker;
beides ist jetzt umgesetzt und durch die bestehende Pipeline nachgewiesen.
Auch türkische Suche, Related-Links und Release-Freshness sind implementiert.
Themen-/Neuprüfungsoperationen bleiben über die vorhandene CLI bedienbar.

**Für einen anschließenden kontrollierten Entwicklungs-/Staging-Rollout
vorbereitet, nicht für ungeprüften öffentlichen Livebetrieb freigegeben.**
Tatsächlich offen bleiben die im Audit einzeln beschriebenen externen
Voraussetzungen: genehmigte Liveanbieter und Nutzungsbedingungen, reale
Redaktions-/Bildrechteabnahme, Domain/TLS/Public-Root und Betreibertexte.
YouTube-Zielkanal/OAuth und explizite Publishfreigabe werden nur für eine später
beauftragte echte Veröffentlichung benötigt. Weitere N8-Adapter oder ein
produktiver N9-Namensumzug sind bedarfsgebundene Folgeaufträge.

Kein Push, Deployment, produktiver DB-Eingriff, Dienstwechsel, bezahlter
Provideraufruf oder echte Veröffentlichung. Avatarerzeugung und automatisches
Publishing bleiben deaktiviert. Nach dem Prüfcommit folgen nur Dokumentation
und die Bereinigung eigener temporärer Logs, keine weiteren Codeänderungen.
