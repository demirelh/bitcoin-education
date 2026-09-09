# Unabhängiger Implementierungsabgleich, 09.09.2026

Basis: `1c229ccb083a444c9b6920e3a5cd95dd0095a110`.
Geprüfter Anfangsstand: `1e9bfb2`, sauberer Worktree,
`feat/almanya24-newsroom`. Keine Subagents, produktiven Änderungen oder Liveprovideraufrufe.
Die folgende Matrix beschreibt zunächst den **vorgefundenen**, nicht den
behaupteten Stand. Die Korrekturen und Nachweise werden darunter fortgeschrieben.

| Paket | Abnahme (kompakt) | Tatsächlicher Code / vorhandene Tests | Vorgefundener Stand / fehlende Integration | Externer Nachweis |
|---|---|---|---|---|
| N0a | Kanal-/Editionsidentität, keine globale Backfillmutation | `detector._stored_broadcast_keys`, `backfill_episodes`; `test_detector`, `test_channel_assignment` | Teilweise: Profil ODER Kanal ODER Titel genügte noch zur Quellgleichsetzung | Kein externer Blocker für Regressionen |
| N0b | Ungeklärte Jobs halten Dateien und Ledger | `retention.retention_hold_reasons`; `test_retention` | Bestands-Holds implementiert; neue Videoedition noch nicht geschützt | Kein externer Blocker |
| N1 | Revisions-/Spanidentität, atomare Reservation, Resume | `editorial.ingest`, `editorial.jobs`, Migration 022; `test_editorial_core` | Bausteine vorhanden, kein durchgängiger Aufrufer | Kein externer Blocker |
| N2 | Automatische Extraktion, Suche, Dokumentprüfung, Gegenbelege, Budget | `DataOnlyClaimExtractor`, `research_revision`, `BraveSearchProvider`, `DocumentFetcher`; `test_editorial_research` | Nur isoliert getestet: Caller nicht verdrahtet, Evaluator nicht budgetiert; Provenienz vom Caller übernehmbar | Suchkonto/Tarif und API-/Cachebedingungen nur für Livepilot |
| N3 | Relevanz, Rolle, konkrete Rechte und Attribution, kein stiller Ersatz | `WikimediaCommonsProvider`, `media.assess_candidate`, `select_media_for_revision`; `test_editorial_media` | Bausteine vorhanden; Video-Nutzungsfreigabe fehlte, Metadatenänderungen nicht vollständig gehasht | Konkrete Rechteentscheidung pro tatsächlichem Bild |
| N4 | TR-Entwurf vom Transkript, belegte Claims, manuelle hashgebundene Freigabe | `article`, `web/editorial_routes`; `test_editorial_article`, `test_editorial_web` | Teilweise: tatsächlicher Text nicht neu gehasht; Entwurf/Extraktion ohne Operator-Workflow | Redaktionelle Qualitätsabnahme realer Fälle |
| N5 | Public-only Website, Suche/Related, Freshness, atomarer Switch, Korrektur | `public`, `site_export`; `test_editorial_public` | Teilweise: Suchformular ohne Suchseite; Related leer; Release-Switch ohne erneute Prüfung; relativer Symlink; Medienbytes ungeprüft | Domain, rechtliche Betreibertexte, öffentliche Inbetriebnahme |
| N6 | Updates/Merge, reversible Entscheidungen, Fanout auch zu Video | `topics`, `recheck`; `test_editorial_topics` | Isolierte Operationen: keine automatische Änderungserfassung, Fanout nur Website, Recheck-Cap kann Rest verwerfen | Kein externer Blocker für Implementierung |
| N7 | Edition → bestehende Pipeline, Original-TTS, sichtbare Credits, getrennte Gates | `video`, Migration 028, CLI `edition`; `test_editorial_video` | **Nur Adaptertests:** `episode_id` nie gesetzt; Routing nie aufgerufen; keine Credits gerendert; Finaldatei nur Pfad, nicht Bytes; fehlende Dateien still akzeptiert | Kein externer Blocker für synthetischen Render |
| N8 | Explizite Profilfehler, messbare Qualität/Kosten, gezielte Adapter | `profile_validation`, `report`; zwei Testsuiten | Teilweise: Planauflösung fällt weiter still zurück, `.all()` lädt Volltexte; Adapterbedarf nicht belegt | Neue Anbieter nicht pauschal vertragspflichtig; Aktivierung fallabhängig |
| N9 | Derselbe CLI-Einstieg/Lock/DB, Inventar, keine doppelten Timer | `pyproject.toml`; `test_cli_alias` | Alias definiert, Installationsnachweis fehlt; Locktest tautologisch; Inventar fehlt | Produktiver Namens-/Dienstumzug nicht beauftragt |

## Priorisierte Befunde am Anfangsstand

1. **P1:** `article.current_article_state` gab `article.content_hash` zurück.
   Änderungen an Titel, Leitsatz oder Absatz konnten eine Freigabe überleben.
   Quellenpassagen, Claimtext und Lizenz-/Rollenmetadaten fehlten teilweise im Hash.
2. **P1:** N7 war kein Produktionspfad. Kein Aufrufer setzte `episode_id`;
   `edition_routing_enabled` war außerhalb von Tests/Exports unbenutzt.
   Der vorherige Bericht über eingebrannte Credits war unbelegt.
3. **P1:** Videoentscheidung prüfte nur Dateiexistenz; Sprechertext konnte
   nachträglich verändert werden. Medienexport akzeptierte fehlende Dateien.
4. **P1:** N5 aktivierte alte Releases ohne erneute Freigabeprüfung; Sections
   wurden ungeprüft als Pfad verwendet. Das relative Symlinkziel war falsch.
5. **P2:** N2/N4 besaßen keinen verbundenen automatischen Operatorablauf und
   keine Budgetreservation für Extraktion/Bewertung.
6. **P2:** N8-Planvalidierung geschah erst nach stiller Defaultplanung.
   N9-Nachweise behaupteten gemeinsame Locks, ohne Konkurrenz tatsächlich zu prüfen.
7. **P1, bei der letzten Schnittstellenprüfung behoben:** Der bestehende
   Publisher verlangte alte Render-/Translation-QA-Dateien, die eine
   redaktionelle Edition bewusst nicht erzeugt. Für Editionsfolgen ersetzt jetzt
   deren aktueller, bytegebundener Finalentscheid diese alten fachlichen Gates.
   Renderintegrität, Metadaten, Budget, Profilfreigabe und eine **zusätzliche**
   manuelle Publishentscheidung bleiben zwingend. Bestandsfolgen unverändert.

## Verlauf der Korrektur

Zu Beginn in Arbeit: tatsächliche Hashneuberechnung, Public-Suche/Releaseprüfung,
Video-/Profilrechteentscheidung, bytegebundene Finalentscheidung, Editionsbindung,
gemeinsame TTS-/Rendererroute und budgetierter Story-zu-Artikel-Ablauf.
Diese Änderungen waren zu diesem Zeitpunkt noch nicht abgenommen.
Erster gezielter Lauf: 159 bestanden, fünf erwartete Regressionen aus bisher
unzulässigen Freigabeannahmen; anschließend Anpassung mit zusätzlichen Negativfällen.

Der fehlende Smoke-Test hatte keinen dokumentierten technischen Blocker.
Er war schlicht nicht implementiert/ausgeführt. Die fehlende Episodebindung
war keine optionale Erweiterung, sondern verhinderte die vereinbarte Integration.

## Implementierte Korrekturen und Nachweisgrenzen

- `editorial.workflow.draft_story` verbindet Import, automatische Extraktion,
  Support-/Gegensuche, Dokumentbewertung, Commons-Auswahl und türkischen
  Entwurf. `services.editorial_model.EditorialModel` verdrahtet bestehende
  Anthropic-/OpenAI-APIs als toolfreie Datenaufrufe; `newsroom-draft` ist der
  explizite Operatoreinstieg. Kein Agent/Shellzugriff für Dokumente.
  Aus Claimsubjekt/-einheit entstehen konservative Themenaliases.
  Wiederholungsmeldungen halten vor einem neuen Entwurf an einem gespeicherten
  Updatevorschlag; erst eine benannte Annahme führt zum vorhandenen Thema.
  Broadcastdatum und konkrete Quellrevision bleiben am Recherchelauf gebunden.
- Modelloperationen werden vor dem Aufruf reserviert und danach mitsamt
  Ergebnissen/Kosten persistiert. Resume kauft abgeschlossene Arbeit nicht
  erneut. Ungewisse oder über Budget liegende Ergebnisse stoppen auch beim
  erneuten Aufruf. Unbekannte Suchkosten bleiben Schätzungen, nicht vermeintlich
  kostenlose Aufrufe.
- Bewertungsinputs binden Claim- und tatsächlich gelesene Dokumentzustände.
  Artikelhashes werden aus Titel, Leitsatz und Absätzen rekonstruiert; Quellen,
  Bewertungen, Rollen, Rechte, Angebote und Nutzung sind nicht nur als alte
  Digestzeichenfolge gebunden. Ein veränderter Beleg verlangt neue Bewertung.
- Kopierte Volltexte werden unabhängig von frei behaupteten Providerfamilien
  gruppiert; Agenturkennungen und ansonsten Publisherhosts bilden konservative
  Familien. Das ist **keine Garantie**, dass umgeschriebene Syndizierungen
  automatisch erkannt werden. Der Report zeigt Konzentration und Einquellenfälle.
- Unbekannte Rechte, zusammengesetzte NC/ND-Einschränkungen und unpräzise
  Ereignisdatierungen werden nicht als Nutzungsfreigabe interpretiert.
  Medienbytes werden gehasht; veränderte Katalogangebote dürfen keine alte
  Assetentscheidung übernehmen. Relevanz ist Metadatenprüfung, keine behauptete
  Bilderkennung; reale Freigaben benötigen weiterhin Bildsichtung.
- `editorial.production` bindet die Edition an eine echte v2-Episode.
  Die Pipeline benutzt unveränderte TTS-/Rendererfunktionen mit deterministisch
  erzeugtem Kapitel-/Bildmanifest, nicht einen parallel implementierten Renderer.
  Credits und Rollenhinweise sind echte Overlays, Metadaten enthalten sie ebenfalls.
  Editionsfolgen sind durch Retention-Holds geschützt.
- Der Smoke testet drei Sprecherabschnitte mit synthetischen 44,1-kHz-Tönen,
  tatsächlicher Lautheitsverarbeitung, ffmpeg-Rendering und ffprobe.
  Der Sinuston ist **kein** Beweis für Aussprache oder menschliche Videoqualität.
  Anbieter und Sprachqualitätsmessung werden simuliert; Datenbank, Orchestrierung,
  Dateimanifeste, Websiteausgabe, Rendern und Freigabegates laufen tatsächlich.
  Auch der bestehende manuelle Publishpfad wird inzwischen durchlaufen:
  zuerst sperren Profil und fehlende Publishentscheidung, danach ausschließlich
  `DryRunYouTubeService` mit einem nur im Test freigeschalteten Profil.
  Kein Upload, kein OAuthzugriff und keine tatsächliche Veröffentlichung.
- Quelle, Claim, Beleg, Lizenz, Rolle, Titel, Leitsatz und Absatz werden in
  Negativfällen verändert. Alte Website-Releases und Videoskripte blockieren.
  Veränderungen an Original-TTS oder Finalvideo entwerten die Finalentscheidung.
  Der lokale Änderungsscan erfasst Entwürfe, Websitepublikationen und Editionen;
  ein zu großer Fanout erzeugt ein dauerhaftes Issue statt Restarbeit zu verwerfen.

## N8: Was tatsächlich einen neuen Auftrag benötigt

N8 verlangt ausdrücklich **einen belegten Bedarf je zusätzlichem Adapter**,
nicht die pauschale Anbindung aller im Plan genannten Beispiele. Für
Openverse oder manuell dokumentierte EU-AV-Fundstellen ist kein allgemeiner
Pressebildvertrag nötig; Implementierung und Fixturetests wären ohne Kauf möglich.
Im vorliegenden Planlauf ist jedoch kein konkreter ungelöster Fall benannt,
der einen solchen zusätzlichen Adapter rechtfertigt. Daher sind diese Beispiele
keine offene Pflichtimplementierung und werden nicht als Vertragsblocker ausgegeben.
Kommerzielle aktuelle Presse-/Sportbilder benötigen tatsächlich eine konkrete
Nutzungsvereinbarung. Bestehender Brave-, Commons- und toolfreier Modellvertrag
sind lokal mit Fakes prüfbar; Livekonten, Tarif-/Cachebedingungen und echte
redaktionelle Qualitätsbeurteilung werden dadurch nicht ersetzt.

## N9: Lokales Installations- und Referenzinventar

`pyproject.toml` behält Paket-/Importnamen `bitcoin-education`/`btcedu`, beide
Console-Scripts verweisen auf `btcedu.cli:cli`. Installation erfolgte ausschließlich
in `/home/pi/.venvs/almanya24-newsroom-dev`, mit `--no-deps` und vorhandener
Buildkonfiguration. Fehlendes setuptools wurde dort nach dem konkreten
Buildfehler ergänzt. Veraltete ignorierte Worktree-`egg-info` wurden per
setuptools `egg_info` erneuert. Beide installierten Aliases werden aus
Paketmetadaten geladen; ein zweiter Prozess erhält am gemeinsamen DB-Lock
für beide Namen `PipelineBusyError`. Der alte tautologische Test ist ersetzt.

Unverändert kompatibel bleiben `run.sh`, `deploy/btcedu-*.service`,
`deploy/btcedu-*.timer`, Recorder-Trigger, Caddy-Dashboardkonfiguration,
`.github/workflows/{ci,deploy,render}.yml` und `core/remote_render.py`.
Keine Import-, DB-, Manifest-, Provider-ID- oder Artefaktpfade wurden umbenannt.
Es wurde kein konkurrierender Konfigurationsalias eingeführt.

Read-only Hostinventar am 09.09.2026: `systemctl list-timers` für `btcedu*`
und `almanya24*` zeigte `btcedu-run.timer` und `btcedu-detect.timer`,
keine `almanya24`-Timerfamilie. Kein Dienst wurde verändert. Produktive
Dienst-/Repository-/Datenpfadumbenennungen sind separate Rolloutentscheidungen,
nicht noch auszuführende lokale Pflichten dieses Auftrags.

## Paketzuordnung nach den Korrekturen

Die abschließende Codeprüfung bezieht sich auf
`e10767bc64e40d36f9434048bee5955e24887ce8` einschließlich `1cdf948` und `f615960`.
Der erste vollständige Lauf ergab 4226 bestandene Tests und drei Fehler
in der historischen Recorder-Deduplizierung (4136,75 s). Die Korrektur
entfernt den unbedingten Profil-Mismatch-Ausschluss, nicht den Schutz gegen
explizit fremde Kanäle: Legacyfolgen ohne Kanal tragen noch den alten
Profildefault, haben aber eine eindeutige Sendungs-/Editionsidentität.
Danach bestanden alle 134 gezielten Recorder-/Detector-/Kanaltests in 42,98 s,
einschließlich des expliziten Fremdkanalfalls. Die erneute Vollsuite auf dem
korrigierten Commit ist abgeschlossen: **4229 bestanden, keine Fehler,
keine übersprungenen Tests**, 60 Deprecation-Warnungen, **3972,90 s (1:06:12)**.
„Lokal umgesetzt“ bedeutet nicht Anbieter-Livebetrieb oder redaktionelle
Produktionsfreigabe. Die Einstufung „nur isolierter Adapter“ trifft auf den
früheren N7-Stand zu, nicht mehr auf den korrigierten Pfad.

| Paket | Stand des freigegebenen lokalen Umfangs | Codefundstellen | Nachweise / Grenze |
|---|---|---|---|
| N0a | Lokal umgesetzt | `detector._stored_broadcast_keys`, Backfill-/Channelrouting | Detector-/Channeltests; zusätzlicher Fremdkanalfall in `test_editorial_integration` |
| N0b | Lokal umgesetzt | `retention.retention_hold_reasons` | Retentionregressionen, unveränderte Ledger; neue Editionsbindung wird konservativ gehalten |
| N1 | Lokal umgesetzt und verbunden | `editorial.ingest.import_story`, `editorial.jobs`, `workflow.BudgetedCaller` | Basis-/Migrations-/Reservations-/Resumetests; Workflow nutzt dieselben persistenten Modelle |
| N2 | Lokal umgesetzt und verbunden; Livequalität nicht behauptet | `workflow.draft_story/query_plans`, `research`, `editorial_model`, `document_fetcher`, `search_service` | CLI mit automatischen Queries, gelesene Fixturepassagen, Gegenbelege, semantisches Negativurteil, Budgetresume; externe Modellantworten simuliert |
| N3 | Lokal umgesetzt und verbunden; konkrete Livebildrechte extern | `commons_service`, `media.evaluate_license/select_media_for_revision`, `video.approve_video_media` | Medien-/Lizenztests, ungeklärte Illustration blockiert, Bildbytes und Rollenänderungen, separate Video-/Profilfreigabe |
| N4 | Lokal umgesetzt und verbunden; redaktionelle Liveabnahme extern | `workflow`, `article`, `web/editorial_routes` | TR-Entwurf, Konsistenzauftrag, benannte hashgebundene Freigabe, private Webtests; keine automatische Freigabe |
| N5 | Lokal umgesetzt und tatsächlich gebaut | `public.build_public_article/export_blockers`, `site_export.build_site/switch_release` | Öffentliche DTOs, Suche/Related, Bildbytes, Korrektur/Rücknahme, atomare Aktivierung, stale Release verweigert; kein öffentlicher Serverbetrieb |
| N6 | Lokal umgesetzt und verbunden | `topics.propose_updates/accept_proposal/merge_topics/revert_merge`, `recheck.scan_changes/run_recheck` | Wiederholungsmeldung stoppt vor Doppelentwurf; Annahme nutzt altes Thema; Graph-/Reversal-/Fanout-/Cleanupregressionen, Änderungen zeigen Entwürfe/Website/Editionen |
| N7 | Lokal umgesetzt, bestehende Pipeline tatsächlich erreicht | `video`, `production`, `pipeline._get_stages/_run_stage`, `tts`, `renderer`, `remote_render.build_job_package`, `publisher._run_all_safety_checks` | Reales ffmpeg/ffprobe, sichtbare Credits, unveränderte TTS, drei getrennte redaktionelle Freigaben plus Video-Rechteentscheidung, separate simulierte manuelle Veröffentlichung |
| N8 | Freigegebener bedarfsunabhängiger Umfang lokal umgesetzt | `profile_validation`, `editorial.report`, Provider-Protokolle/Fixtures | Explizite Profilfehler, Schätz-/Istkosten, Quellenfamilien, kein Laden von Belegvolltexten im Report; weitere Adapter nur bei konkretem Bedarf, nicht pauschal blockiert |
| N9 | Freigegebener Kompatibilitätsschritt lokal umgesetzt | `pyproject.toml`, unveränderter `cli:cli`/`runlock`, obiges Inventar | Installierte Aliases, echter gemeinsamer Prozesslock, Referenz-/Remote-/Bestandsregressionen; keine produktiven Namens-/Dienständerungen |

## Abschließende Integration und Abnahme

`tests/test_editorial_integration.py` enthält 17 Testfälle einschließlich
Parametrisierungen. Der zentrale Durchlauf beginnt mit einer ausgewählten
Transkriptstory und endet über echte Episodebindung und Pipelineaufrufe bei
einem realen lokalen MP4. Modellurteile, externe Such-/Dokument-/Commonsantworten
und Sprachprovider sind Fixtures; sie beweisen keine Livequalität.
Tatsächlich ausgeführt werden Revisions-/Budgetpersistenz, Dokumentauswertung,
Freigaben, Public-only Websitebuild und Releaseswitch, Kapitel-/Bildmanifeste,
TTS-Lautheitsverarbeitung, ffmpeg und ffprobe. Sichtbare Creditpixel,
unveränderte TTS beim erneuten Rendern, private Remote-Ausschlüsse sowie
separate Skript-, Medien-, Final- und manuelle Publishgates sind geprüft.
Fehlende tragfähige Belege, ungeklärte angeforderte Bildrechte und veraltete
Freigaben blockieren. Die manuelle Veröffentlichungsstrecke verwendet nur
`DryRunYouTubeService`, keine tatsächliche Veröffentlichung.

Die komplette Suite wurde mit niedriger Priorität und ohne parallele schwere
Tests in `/home/pi/.venvs/almanya24-newsroom-dev` ausgeführt:

```bash
nice -n 10 /home/pi/.venvs/almanya24-newsroom-dev/bin/python \
  -m pytest -v -p no:cacheprovider -o faulthandler_timeout=180 --tb=short
```

Geprüfter Codecommit: **`e10767bc64e40d36f9434048bee5955e24887ce8`**.
Die nachfolgenden Änderungen betreffen nur Audit, Fortschritt und Flowcharts.
Warnungen: ein Pillow-Pixelzugriff, 57 SQLite-Datetimeadapterwarnungen und
zwei Fork-Warnungen bestehender Locktests; kein Fehler oder Ressourcenabbruch.

**Ergebnis:** Der freigegebene lokal ausführbare Umfang ist abgeschlossen.
Der Branch ist für einen separat freigegebenen kontrollierten
Entwicklungs-/Staging-Rollout vorbereitet. Das ist weder eine Freigabe realer
Inhalte noch ein Nachweis öffentlich betriebenen Systems. Für einen öffentlichen
Livebetrieb fehlen ausschließlich die nachfolgend benannten Voraussetzungen.

## Externe Voraussetzungen für einen späteren Livebetrieb

1. Such-/Modellkonto mit ausdrücklich freigegebenem Tarif und zulässiger
   Quellen-/Cacheverwendung; die Offline-Fixtures ersetzen diese Freigabe nicht.
2. Benannte Redaktion und konkrete Prüfentscheidung für reale Texte,
   Quellenabhängigkeiten und jedes verwendete Bild, einschließlich Videonutzung.
   Kein generelles Pressebildabo ist Voraussetzung für den Commons-MVP.
3. Domain/TLS, korrekter ausschließlich öffentlicher Static-Root sowie echte
   Betreiber-, Kontakt-, Datenschutz- und Nutzungshinweise. Die vorhandenen
   leeren/default Betreiberwerte dürfen nicht als produktive Rechtsseiten gelten.
4. Nur falls später eine echte Veröffentlichung gewünscht ist: explizite
   Profil-/Zielkanalfreigabe und passendes YouTube-OAuth. Der ausgelieferte
   Editionsprofilstand sperrt auch manuellen Upload weiterhin absichtlich.

Die Produktionsmigrationen, Dienständerungen und tatsächliche Aktivierung
sind **nicht ausgeführt**. Sie sind Arbeitsgrenzen dieses Auftrags, kein
Vorwand für fehlende lokale Adapter-/Integrationstests.
