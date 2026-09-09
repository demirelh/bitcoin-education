# Quellen, Medien und Website: Recherchegrundlage

[Einstieg](../../plans/almanya24-news-platform.md) | [Architektur](../../plans/almanya24-news-platform/architecture.md)

Oeffentliche Dokumentation gelesen am **09.09.2026**. Keine API-Schluessel
beantragt, keine Abos abgeschlossen, keine Suchabfragen ueber kostenpflichtige
Produkt-APIs, keine Medien heruntergeladen oder lizenziert. Preise in USD,
ohne verbindliche Aussage zu Steuern/Wechselkurs; vor Beschaffung erneut
pruefen. Plattformdokumentation ersetzt keine Einzelfall-Rechtepruefung.

## 1. Automatische Websuche fuer Belege

| Option | Verifizierte Funktionen / Kosten / Limits | Entscheidung |
| --- | --- | --- |
| Brave Search API | Offizielle Produktseite: Search 5 USD/1.000 Requests, 5 USD monatlicher Freikredit, 50 Requests/s. Websuche mit Land, Sprache, Freshness, `site:`, max. 20 Resultate/Seite, offset max. 9 [S1,S2] | Bevorzugter zu evaluierender Web-Discovery-Adapter: transparente URL-Ergebnisse statt fertiger Wahrheitsantwort |
| Tavily Search | 1.000 Credits/Monat kostenlos ohne Kreditkarte laut Doku; Basic 1 Credit, Advanced 2; PAYG 0.008 USD/Credit; kleinster Monatsplan 30 USD/4.000 Credits [S3] | Realistische alternative MVP-Anbindung; kostenloses Kontingent attraktiv, aber erst separat Kontozugang freigeben |
| Tavily Limits | Development 100 RPM, Production 1.000 RPM; Production-Key erfordert Paid/PAYG; 429 mit Retry-After [S4] | Development-Kontingent fuer kleinen Pilot ausreichend; kein Auto-Upgrade/PAYG |
| Institutionelle Feeds/Open Data | Bundestag offene Protokolle/Drucksachen XML/JSON, Abstimmungslisten; ECB SDMX Daten+Metadaten [S5,S6] | Kostenguenstige gezielte Ergaenzung und primaere Belege. Keine vollstaendige allgemeine Nachrichten-Websuche |
| Selbst betriebene Metasuche / Suchmaschinen-HTML | Kein belastbarer pauschaler Nutzungs-/Verfuegbarkeitsvertrag in dieser Untersuchung | Nicht als zuverlaessigen kostenlosen Standard versprechen; kein Umgehen von Zugangsbeschraenkungen |

Brave/Tavily liefern **Fundstellen**, keine Tatsachenfreigabe. Auch ein
providerseitiger Antworttext oder `answer` mit Quellen wird nicht als Beleg
gespeichert, solange die jeweilige Originalfundstelle nicht separat gelesen
und die Passage zugeordnet wurde.

### Konkrete Kostenrechnung und Beschaffungsgrenze

Vorschlag fuer einen Thema-Pilot: hoechstens 5 wesentliche Claims, zuerst
2 gezielte Queries je Claim, dann Gegenbeleg-/Sprachsuche nur bei Bedarf.
**Maximal 20 Suchrequests pro Themenrevision inklusive Retries und
Rechecks**, danach `budget_blocked`/manuelle Bearbeitung. Keine unbegrenzte
Agentenschleife.

- Brave: 20 x 0.005 = **0.10 USD** Brutto-Suchkosten vor Freikredit.
- Tavily Basic: 20 x 0.008 = **0.16 USD**, Advanced **0.32 USD**.
- Beispiel 30 solcher Revisionen: 600 Requests -> Brave **3 USD**,
  Tavily Basic **4.80 USD** vor Freikontingent; 600 Basic-Credits liegen
  unter dem dokumentierten 1.000er Kontingent. Das ist keine Zusage
  kostenloser LLM-Verarbeitung.
- Tavily Extract kostet zusaetzlich: Basic 1 Credit pro 5 erfolgreiche URLs,
  Advanced 2. Im MVP kein automatisches Crawl/Research-Produkt:
  Tavily Research kann bis 250 Credits/Request kosten [S3].
- LLM-Extraktion, Passagebewertung und Artikelerzeugung getrennt messen:
  Modellrate, Tokenlimits, Cachehits, akzeptierte/ungeklaerte Operationen.
  Vorlaeufige harte Obergrenzen im Ziel: **0.50 USD LLM pro Revision**,
  **0.75 USD gesamt**, **3 USD/Tag**, **20 USD/Monat**.
  Das sind Planwerte, keine existierenden Settings oder freigegebene Ausgaben.
  Bei unklarer Tarif-/Quotaabbildung stoppt der Job vor dem Aufruf.

Empfehlung: Vertrag fuer beide Adapter vorsehen, **einen** im MVP
implementieren. Brave zuerst anhand fester DE/TR/EN-Faelle evaluieren;
Tavily Basic als Alternative bei einfacherem kostenfreiem Zugang.
Die endgueltige Konto-/Tarifwahl ist eine getrennte Betreiberentscheidung.
Ohne freigegebenen Zugang laeuft der automatische Ablauf nur mit Fixtures,
nicht heimlich ausschliesslich mit manuellen Belegen.

Such-API-AGB, Rechte an Suchauszuegen, Speicherung und Weitergabe sind vor
Kontoaktivierung gesondert zu pruefen. Ein Preisblatt ist keine
Speicher-/Publikationslizenz. Oeffentlich erscheinen eigene Quellenangaben
und kurze erforderliche Zitate, nicht Suchresultatdatenbanken.

## 2. Redaktionelle Belegquellen

| Themen | Bevorzugte Fundstellen | Grenzen |
| --- | --- | --- |
| Deutsche Politik/Recht | Bundestag, Bundesrat, Gerichte, Gesetzblaetter, zustaendige Ministerien; konkretes Dokument/Abstimmung statt Pressezusammenfassung | Amtliche Position ist Beleg fuer die Position, nicht automatisch fuer jede darin behauptete Wirkung |
| Wirtschaft/Zahlen | Destatis/GENESIS, ECB, Eurostat, Bundesbank; in TR TUIK/TCMB | Zeitraum, Einheit, saisonbereinigt/unbereinigt, Revision und Bezugsbevoelkerung muessen passen |
| Tuerkei/International | Zustaendige Institutionen, internationale Organisationen, offizielle Originaldokumente; DE/TR/EN-Berichte zum Abgleich | Politische Abhaengigkeit und Ursprung markieren; primaer bedeutet nicht neutral |
| Sport | Offizieller Wettbewerb/Verein/Verband fuer Ergebnis oder Termin, unabh. Bericht fuer strittige Ereignisse | Spielstaende als Daten sind nicht dasselbe Recht wie Pressefotos, Wappen oder TV-Ausschnitte |
| Eilmeldungen/Konflikte | Benannte verantwortliche Erstquelle plus unabhaengige Berichterstattung; explizite Zuschreibung/Unsicherheit | Agenturabschriften zaehlen als eine Herkunftsfamilie; ungepruefte Opferzahlen nicht automatisch verfestigen |

Automatische Discovery kann diese Domains priorisieren, aber darf nicht
nur zustimmende Fundstellen suchen. Gegensuche und Herkunftscluster sind
Pflicht. Ein primaeres Wahlergebnis kann einen engen Zahlenclaim allein
tragen; ein schwerwiegender umstrittener Vorwurf braucht andere Massstaebe
als die starre Regel "immer zwei URLs".

Heute direkt lesbar bestaetigt: Bundestag Open Data und ECB API [S5,S6].
Destatis-Seite lieferte 403; TUIK leitet auf eine JavaScript-Anwendung um.
Fuer diese Quellen werden **keine erfundenen API-Endpunkte oder
pauschalen Automationsrechte** zugesagt. Gegebenenfalls oeffentliche
Pressemitteilung direkt abrufen oder manuelle Fundstelle aufnehmen.
Keine Login-/Paywall-/Bot-Schutz-Umgehung.

## 3. Echte Medien: Quellenvergleich

| Quelle | Abdeckung/Aktualitaet | API und Kosten | Rechte / MVP-Eignung |
| --- | --- | --- | --- |
| Wikimedia Commons | Gute Portraets, Orte, Gebaeude, Institutionen; Ereignis- und Sportaktualitaet lueckenhaft | Action API: Search im Datei-Namensraum 6, Imageinfo fuer URL/Groesse/MIME/SHA1/extmetadata; kleine Batches, User-Agent, maxlag, Cache [M1-M4] | **MVP zuerst.** Pro Datei Lizenz/Urheber/Herkunft pruefen; Commons garantiert Lizenzrichtigkeit nicht |
| Openverse | Aggregiert offene Bilder/Audio vieler Quellen | Dokumentierte API, Metadatenkatalog; keine hier verifizierte stabile allgemeine Gratisquote [M5] | Spaeter Discovery, kein zweiter Rechtsgarant. Nutzungsbedingungen verlangen eigene Lizenzpruefung; keine Katalogscrapes |
| EU Audiovisual Service | EU-Politik, Amtstraeger, Treffen, Presseevents; aktuell und Archiv | Oeffentliches Portal; keine stabile oeffentliche Such-API hier verifiziert | EU-eigene Inhalte grundsaetzlich CC BY 4.0, sofern nichts anderes pro Datei; Drittrechte, Personen, Gebaeude und Musik beachten [M6]. Spaeter gezielt/manuell, nicht blind scrapen |
| Smithsonian Open Access | Geschichte, Kultur, Natur, Objekte; kaum aktuelle Politiker-/Sportevents | Open-Access-Bestand, API-Anbindung vor Implementierung separat dokumentieren | Nur explizit CC0-markierte Assets: kommerziell nutzbar, Drittrechte/Marken bleiben; andere Objekte ausgeschlossen [M7] |
| NASA | Raumfahrt, Forschung, Erde; thematisch begrenzt | Image/Video-Portal, verlinkte API-PDF nicht inhaltlich ausgewertet | Offizielle Nutzungsrichtlinie hat Drittmaterial-, Personen-, Marken- und spezielle AI-Abschnitte. Keine pauschale weltweite Gemeinfreiheit behaupten; **nicht MVP-Standard** [M8] |
| Europeana | Kultur-/Archivmaterial vieler Einrichtungen | Rechte-/API-Seiten waren teilweise nur Shell/Cookie-Ausgabe bzw. 403 | Aufnahme pro Objekt/Rechtestatement noetig; API-Limits und Lizenzdetails hier U. Spaeter nach eigener Vertragspruefung |
| Pexels, bereits im Projekt | Allgemeine Stockmotive, Symbolbilder, B-Roll; keine verlässliche Ereignisfotoquelle | Dokumentiert 200 Requests/Stunde, 20.000/Monat; API verlangt prominenten Pexels-Link, moeglichst Fotografencredit [M9] | Kommerzielle Lizenz mit erheblichen Verboten: irrefuehrender/politischer Kontext, Rechte Dritter etc. [M10]. Fuer Politikportraets kein pauschal sicherer Fallback |
| Reuters Connect / AP / dpa-Picture-Angebote | Professionelle aktuelle Politik-/Sport-/Ereignisbilder | Reuters-Portal und AP Media API 2.2 Help Center erreichbar; keine verbindlichen Angebotspreise abgerufen; versuchter dpa-Bild-Link 404 | Individueller Vertrag/Assetlizenz, redaktionelle Nutzungsrechte pro Medium, Gebiet, Dauer und Kanal pruefen. **Kein Pressebildabo als MVP-Voraussetzung** |

Beispiel Putin/Erdogan/Merz/Trump: Commons kann ein korrekt identifiziertes,
lizenziertes Portraet liefern. Es beweist nicht Anwesenheit am aktuellen
Ereignis. Alte Portraets sichtbar als Archiv/Portraet kennzeichnen.
Fussball: Vereins-/Stadionmotiv kann ein zulässiges Symbolbild sein,
belegt aber weder Spielszene noch Ergebnis; aktuelle Spielbilder brauchen
konkrete Rechte. Wappen/Logos sind keine automatisch freien Fotos.

### Rechteklassen sind nicht austauschbar

| Klasse | Entscheidung im Ziel |
| --- | --- |
| Frei erreichbar | Nur Zugang; weder Lizenz noch Echtheitsbeweis |
| Gemeinfrei / CC0 | Grund/Hoheitsgebiet bzw. CC0-Erklaerung dokumentieren; Persoenlichkeits-/Markenrechte bleiben |
| CC BY | Version, Urheber, Quelle, Lizenzlink und Bearbeitungen dokumentieren/anzeigen; Nutzungskontext pruefen |
| CC BY-SA | Nicht generell ungeeignet, aber ShareAlike-Auswirkung der Bearbeitung/Videoeinbindung pruefen; MVP manuelle Einzelfallfreigabe |
| NC / ND | Fuer monetarisierte oder bearbeitete Nutzung regelmaessig ungeeignet; MVP standardmaessig ausschliessen |
| Editorial only / bezahlt | Monetarisierte redaktionelle Nutzung kann erlaubt sein, Werbung/Endorsement nicht automatisch; Vertragsumfang entscheidet |
| Unklar/widerspruechlich | Gesperrt; alternative Datei oder bewusst kein Bild |

CC BY-SA erlaubt kommerzielle Nutzung und Bearbeitung unter Bedingungen.
Die Lizenz klaert nicht alle Persoenlichkeits-, Datenschutz- oder sonstigen
Rechte [M11]. Mehrfachlizenzen: gewaehlte Lizenz dauerhaft dokumentieren,
nicht die bequemste spaetere Metadatenanzeige stillschweigend uebernehmen.

## 4. Referenzwebsite ensonhaber.com

Die oeffentliche Startseite wurde als HTML gelesen, keine Artikel/Bilder
uebernommen. Erkennbar: dichte Themenpraesentation, Rubriken fuer Welt,
Wirtschaft, Sport und Technik, Eilmeldungen, Galerien; mehrere
Navigationsbereiche und JSON-LD. JavaScript-/CSS-Marker sind kein getesteter
mobiler Nutzungsablauf. Kein visueller Pixelvergleich vorgenommen.

Eigenstaendiger Vorschlag: ALMANYA24-Startseite mit einer Hauptmeldung,
daneben kurzen aktuellen Meldungen, darunter Deutschland/Tuerkei/Welt/
Wirtschaft/Gesellschaft/Sport als konfigurierbare Rubriken; deutlich
weniger Ablenkung, keine kopierte Anordnung, Farben oder fremden Module.
Artikelseiten priorisieren Quellen, Aktualisierungsstand und Medienhinweise.
Ein Faktenpruefstatus wird verstaendlich beschrieben, nicht als
"100 Prozent wahr"-Badge vermarktet.

## 5. Nachschlageregister

### Suche und Primaerquellen

- [S1 Brave Search API: Preise und Kapazitaet](https://brave.com/search/api/)
- [S2 Brave Web Search: Sprache, Datum, Parameter](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)
- [S3 Tavily Credits und Preise](https://docs.tavily.com/documentation/api-credits)
- [S4 Tavily Rate Limits](https://docs.tavily.com/documentation/rate-limits)
- [S5 Bundestag Open Data](https://www.bundestag.de/services/opendata)
- [S6 ECB Data Portal API](https://data.ecb.europa.eu/help/api/overview)
- [Destatis, Abruf hier 403](https://www.destatis.de/EN/Service/OpenData/_node.html)
- [TUIK, Weiterleitung auf JS-Portal](https://data.tuik.gov.tr/)

### Medien und Rechte

- [M1 Commons Wiederverwendung](https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia)
- [M2 MediaWiki API Search](https://www.mediawiki.org/wiki/API:Search)
- [M3 MediaWiki API Imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo)
- [M4 API Etiquette](https://www.mediawiki.org/wiki/API:Etiquette)
- [M5 Openverse Terms](https://docs.openverse.org/terms_of_service.html) und [API](https://api.openverse.org/v1/)
- [M6 EU AV Conditions of use](https://audiovisual.ec.europa.eu/en/conditions-of-use)
- [M7 Smithsonian Open Access FAQ](https://www.si.edu/openaccess/faq)
- [M8 NASA Images and Media Guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)
- [M9 Pexels API Guidelines](https://www.pexels.com/api/documentation/)
- [M10 Pexels Terms, angezeigter Stand 15.11.2024](https://www.pexels.com/terms-of-service/)
- [M11 CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- [Europeana Terms, hier nur Shell gelesen](https://www.europeana.eu/en/rights/terms-of-use)
- [Reuters Connect](https://www.reutersconnect.com/)
- [AP Media API Help](https://api.ap.org/media/v/docs/)
- [Designreferenz, nicht Inhaltsquelle](https://www.ensonhaber.com/)

Die Recherche ist ein belastbarer MVP-Quellenvergleich, keine vollstaendige
Rechtspruefung oder Garantie einzelner Assets. Die bewusst als U markierten
Anbieter sind keine stillen Implementierungsvoraussetzungen.
