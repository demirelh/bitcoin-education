# ALMANYA24 Medien-Roadmap und TODOs

Stand: 22. August 2026

Dieses Dokument ist die zentrale TODO-Liste für den Ausbau von ALMANYA24 um
KI-Moderatorin, YouTube-Testbetrieb, On-Air-Design, Shorts und eine öffentliche
Website.

Status:

- `[x]` implementiert oder dokumentiert
- `[ ]` offen
- `[!]` durch eine externe Betreiberentscheidung oder einen externen Dienst blockiert

## Grundsatz: Bestehende Funktionen erhalten

Die neuen Funktionen sind so vorbereitet, dass der bisherige Betrieb unverändert
weiterlaufen kann:

- `anchor_enabled=false` bleibt der Standard. Ohne Aktivierung wird kein Avatar-API-Aufruf
  ausgeführt.
- `anchor_provider=d-id` bleibt der globale Standard. Bestehende D-ID-Konfigurationen
  funktionieren weiter.
- HeyGen ist nur für `tagesschau_tr` vorbereitet und wird erst bei aktivierter
  Anchor-Stufe verwendet.
- Der renderer-sichere Standard ist MP4. Optionales transparentes HeyGen-WebM wird
  noch nicht aktiv verwendet, weil das Studio-Compositing dafür erst gebaut werden muss.
- Die bisherigen Bitcoin-Podcast-Profile bleiben auf dem Produktionsziel und auf
  `unlisted`.
- Vorhandene `YOUTUBE_CLIENT_SECRETS_PATH`, `YOUTUBE_CREDENTIALS_PATH` und
  `YOUTUBE_DEFAULT_PRIVACY` werden automatisch in das neue Produktionsziel übernommen.
  Bestehende Installationen müssen ihre Produktions-Tokens daher nicht verschieben.
- Test- und Produktions-Uploads verwenden getrennte YouTube-Credentials. Es gibt
  keinen stillen Fallback von einem Ziel auf die Zugangsdaten des anderen.
- Bereits manuell bearbeitete `youtube_metadata.json` werden weiterhin nicht
  überschrieben.
- Alte abgeschlossene Publish-Jobs ohne Zielangabe werden als Produktions-Jobs
  interpretiert.
- Kapitelmarken kommen weiterhin aus der finalen Render-Timeline.
- Intro, Outro, TTS, Render, Wetter und die bestehenden Review-Gates werden durch
  die neuen, standardmäßig inaktiven Avatar-Funktionen nicht ersetzt.

## Bereits implementiert

### Avatar-Grundlage

- [x] `AnchorService` ist providerneutral.
- [x] Bestehende D-ID-Unterstützung und deren bisherige Defaults bleiben erhalten.
- [x] HeyGen-API-Adapter für Audio-Upload, Videoauftrag, Polling und Download.
- [x] HeyGen MP4 sowie optionales WebM auf Service-Ebene.
- [x] Profilgesteuerte Auswahl von Provider, Engine, Avatar-ID und Ausgabeformat.
- [x] Renderer-sicheres MP4 als aktives ALMANYA24-Ausgabeformat.
- [x] Idempotenz-Hash berücksichtigt Provider, Engine, Audio, Bild und Ausgabeparameter.
- [x] Avatar-Manifeste speichern Provider, Job-ID, Dauer, Größe, Kosten und Provenienz.
- [x] Harte Kostenprüfung vor und nach jedem Avatar-Auftrag.
- [x] Dry-run ohne externen API-Aufruf.
- [x] Enge Fehlerklassen für Authentifizierung, Quota, Providerfehler und Timeout.
- [x] Gemockte Tests für D-ID, HeyGen, Profilrouting, Formate und Budgets.

### YouTube-Grundlage

- [x] Explizite Ziele `test` und `production`.
- [x] Getrennte Client-Secret-, Token- und Kanal-ID-Konfiguration je Ziel.
- [x] ALMANYA24 verwendet bis zur Freigabe standardmäßig `test` und `private`.
- [x] Bestehende Profile verwenden weiterhin `production` und `unlisted`.
- [x] Authentifizierter Kanal wird vor einem Upload gegen die konfigurierte Kanal-ID
  geprüft.
- [x] OAuth-Dateien werden atomar und mit Dateirecht `0600` geschrieben.
- [x] CLI-Kommandos für Authentifizierung, Status, Review und Publish kennen das Ziel.
- [x] Quota-Schätzung unterscheidet Upload, Kanalprüfung, Thumbnail und Captions.
- [x] Uploadziel und Versuch werden vor dem externen Upload gespeichert.
- [x] Akzeptierte YouTube-Video-ID wird sofort persistiert.
- [x] Ein unklar abgebrochener `UPLOADING`-Job wird nicht automatisch erneut
  hochgeladen, auch nicht mit `--force`; er verlangt eine Reconciliation.
- [x] Test-Uploads setzen eine Episode nicht fälschlich auf den finalen
  Produktionsstatus.
- [x] Quellenattribution bleibt in der YouTube-Beschreibung, aber nicht im Bild.
- [x] Kapitelmarken, Untertitel und manuell bearbeitete Metadaten bleiben erhalten.
- [x] Operator-Runbook:
  `docs/runbooks/youtube-test-environment.md`.

### Marke und Rechte

- [x] Verbindlicher Markenname: `ALMANYA24`.
- [x] Gesprochener Name: `Almanya Yirmi Dört`.
- [x] Slogan: `Almanya'nın nabzı burada atıyor.`
- [x] Marken-, Avatar-Rechte- und Freigaberahmen dokumentiert:
  `docs/implementation/almanya24-brand-rights.md`.
- [x] Bestehende Avatar- und Dual-Presenter-Pläne auf den aktuellen Stand gebracht.

## P0 — externe Grundlagen abschließen

### Logo und Brand-Kit

- [!] Finales ALMANYA24-Logo erstellen und freigeben.
- [ ] Varianten liefern: horizontal, kompakt, rundes Kanal-Icon, hell, dunkel,
  monochrom und SVG.
- [ ] Lesbarkeit bei 48 px, 320 px, 1080p und als Thumbnail prüfen.
- [ ] Verbindliche Farbpalette, Typografie, Safe Areas und Motion-Regeln festlegen.
- [ ] Domain sowie YouTube- und Social-Handles prüfen und sichern.
- [ ] Sicherstellen, dass keine bestehende Sender- oder Medienmarke imitiert wird.

### Moderatorin und Rechte

- [!] Entscheiden: vollständig synthetische Figur oder lizenzierte reale Person.
- [!] Bei einer realen Person eine unterschriebene, widerrufbare Vereinbarung für
  Gesicht, Stimme, Training, Bearbeitung, kommerzielle Nutzung, Länder, Laufzeit und
  Datenlöschung abschließen.
- [ ] KI-Kennzeichnung für Video, Beschreibung, Website und maschinenlesbare
  Provenienz final freigeben.
- [ ] Ausspracheregeln für deutsche Namen, Orte und Institutionen definieren.
- [ ] Mimik, Gestik, Kleidung, Kameradistanz und sensible Nachrichtenthemen begrenzen.

### Separater YouTube-Testbetrieb

- [!] Separaten ALMANYA24-Testkanal beziehungsweise Brand Account erstellen.
- [!] Separates Google-Cloud-Projekt erstellen und YouTube Data API v3 aktivieren.
- [!] Test-OAuth-Client erzeugen und gemäß Runbook auf dem System hinterlegen.
- [!] Testkanal-ID konfigurieren und mit `youtube-status --target test` prüfen.
- [!] Ersten privaten manuellen beziehungsweise automatisierten Testupload ausführen.
- [ ] OAuth-App für dauerhaft unbeaufsichtigten Betrieb korrekt veröffentlichen oder
  verifizieren.

## P1 — Avatar produktionsreif machen

### Anbieterpilot

- [!] HeyGen-PAYG-Zugang und D-ID-Testzugang bereitstellen.
- [ ] Einen festen türkischen Referenztext von 60 bis 90 Sekunden mit schwierigen
  deutschen Namen definieren.
- [ ] Dasselbe ElevenLabs-Audio mit HeyGen Avatar III, HeyGen Avatar IV/V und D-ID
  rendern.
- [ ] Kosten, Renderzeit, Fehlversuche, Lippensynchronität und Natürlichkeit messen.
- [ ] Blindbewertung mit mindestens drei Personen durchführen.
- [ ] Produktionsprovider und Fallback erst anhand der Messwerte festlegen.
- [ ] Monatsrechnung für 22 und 30 Sendungen gegen das Budget von 100 bis 300 Euro
  prüfen.

### Studio und vollständige Integration

- [ ] Festes 16:9-ALMANYA24-Studio-Master erstellen.
- [ ] Alpha-WebM im Renderer über das Studio komponieren oder bei MP4 ein fertiges
  Studio bereits durch den Provider ausgeben.
- [ ] Bildregie so konfigurieren, dass die Moderatorin 55 bis 70 Prozent der
  Bildlaufzeit sichtbar ist.
- [ ] B-Roll, Dokumente, Karten und Wetter weiterhin als informative Schnitte nutzen.
- [ ] Szenen kurz und einzeln wiederholbar machen; nie die gesamte Sendung wegen eines
  einzelnen Providerfehlers erneut kaufen.
- [ ] Parallelität sowie 429-/5xx-Backoff festlegen.
- [ ] Dashboard-Vorschau und Review-Gate für Avatar-Szenen ergänzen.
- [ ] Fail-closed Verhalten oder sichtbar geprüften Voice-over-Fallback festlegen.

## P1 — YouTube Ende-zu-Ende abnehmen

- [ ] Drei private Uploads an verschiedenen Tagen durchführen.
- [ ] Titel, Beschreibung, Tags, Sprache, Kategorie, Thumbnail und Playlist prüfen.
- [ ] Kapitelmarken auf die richtigen Topic-Cards kontrollieren.
- [ ] Untertitelspur hochladen und Synchronität prüfen.
- [ ] Neustart direkt nach Uploadannahme simulieren und Reconciliation testen.
- [ ] Sicherstellen, dass kein Duplikat entsteht.
- [ ] Erst danach den Produktionskanal konfigurieren und freigeben.

## P2 — professionelles On-Air-Paket

- [ ] Logo, Studio, Lower Thirds, Topic-Cards, Wetter und Thumbnails als ein System
  gestalten.
- [ ] Intro von etwa vier bis sechs Sekunden produzieren.
- [ ] Outro von etwa drei bis fünf Sekunden produzieren.
- [ ] Jingle, kurzer Stinger und Outro-Variante mit kommerziellen Rechten erstellen.
- [ ] Musik-Stems, Projektdateien, Lizenz und Lautheitsziel archivieren.
- [ ] Intro und Outro als versionierte Master-Assets einbinden und nicht pro Episode
  neu generieren.
- [ ] Sprachverständlichkeit und Ducking gegen TTS prüfen.

## P2 — YouTube Shorts

- [ ] Neue V2-Stage nach finalem Render und Review implementieren.
- [ ] Kandidaten nur aus freigegebener Narration und finaler Timeline wählen.
- [ ] Höchstens ein bis drei Kandidaten von 20 bis 60 Sekunden je Episode erzeugen.
- [ ] 1080x1920 neu komponieren; nicht nur das 16:9-Bild mittig abschneiden.
- [ ] Avatar, Untertitel und Headline innerhalb der mobilen Safe Areas halten.
- [ ] Story-ID, Langvideo-Zeitbereich und verwendete Sätze im Manifest speichern.
- [ ] Zunächst nur Review-Artefakte erzeugen.
- [ ] Automatisches Publishing erst nach zehn manuell akzeptierten Shorts entscheiden.

## P3 — öffentliche Website

- [ ] Öffentliche Startseite mit den neuesten Sendungen erstellen.
- [ ] Eine Detailseite je Sendung mit Datum, freigegebener Zusammenfassung,
  Themenliste und YouTube-Link beziehungsweise Einbettung erstellen.
- [ ] Seiten statisch aus freigegebenen Pipeline-Artefakten generieren.
- [ ] Website unabhängig vom Produktions-Pi und von der Produktions-SQLite hosten.
- [ ] Veröffentlichung erst nach erfolgreichem YouTube-Publish.
- [ ] Sitemap, RSS/Atom, Open Graph und strukturierte Daten ergänzen.
- [ ] Impressum, Datenschutz, Kontakt, Korrekturen und KI-Transparenz bereitstellen.
- [ ] Mobil-, Accessibility-, Performance- und Pi-Ausfalltest durchführen.

## Abnahmekriterien

Der Ausbau ist abgeschlossen, wenn:

1. eine vollständige Sendung mit freigegebener Moderatorin und konsistentem Studio
   innerhalb des Monatsbudgets erzeugt wird,
2. die bisherigen nicht aktivierten Avatar- und bestehenden Publish-Pfade weiterhin
   funktionieren,
3. drei private YouTube-End-to-End-Läufe ohne Duplikat erfolgreich waren,
4. Kapitelmarken und Untertitel zur finalen Render-Timeline passen,
5. mindestens zehn Shorts manuell akzeptiert wurden,
6. die öffentliche Website ausschließlich freigegebene Inhalte zeigt und auch bei
   Ausfall des Produktions-Pi erreichbar bleibt.
