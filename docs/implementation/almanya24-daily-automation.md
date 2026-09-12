# ALMANYA24 — tägliche Transkript→News-Automation

Betrifft ausschließlich den Entwicklungs-Worktree
`/home/pi/AI-Startup-Lab/almanya24-newsroom-dev` und die geschützte Vorschau
unter `https://sahimi.app/almanya24-dev/`. Produktion, YouTube, TTS und Avatar
sind nicht beteiligt.

## Was der Lauf tut

```
stories.json  →  Themenauswahl  →  Recherche/Belege  →  türkischer Artikel
              →  Medienauswahl  →  Entwurf gespeichert  →  Entwicklungsseite
```

Ein Lauf ist `btcedu.core.editorial.daily.run_daily()`. Alles Externe — Modell,
Suche, Bilder, Uhr — wird hineingereicht, damit der Offline-Test denselben
Kontrollfluss fährt wie der Dienst. Der produktive Zusammenbau steht in
`scripts/almanya24_preview/daily_run.py`.

## Transkriptquelle

Gelesen wird `data/outputs/tagesschau_<datum>_2000/stories.json` aus dem
**Produktions**-Worktree, ausschließlich lesend (`ReadOnlyPaths` in der Unit).

Das ist bewusst nicht das Rohtranskript. Die Videopipeline segmentiert die
Sendung bereits in genau das `Story`-Schema, das die Redaktionskomponenten
erwarten. Diese Segmentierung ist also schon bezahlt; ein zweiter Modelllauf
über dasselbe Transkript wäre eine zweite Rechnung für ein bereits
vorliegendes Ergebnis.

`transcript_source.select_stories()` verwirft Meta-, Intro-, Outro- und
Wetterbeiträge sowie Texte unter 40 Wörtern und bildet die Kategorie auf eine
Sektion der Website ab. Die Auswahl ist deterministisch und kostenlos.

## Zeitplan

`stories.json` erschien über sechs geprüfte Sendetage konstant zwischen
**20:31 und 20:37** Europe/Berlin. Der Timer läuft deshalb:

| Zeit (Europe/Berlin) | Zweck |
| --- | --- |
| 21:15 | Hauptlauf, rund 40 Minuten Puffer nach der beobachteten Spätgrenze |
| 22:30 | Nachholung, falls die Pipeline an diesem Abend später fertig wurde |
| 06:45 | Nachholung, falls der Abend ganz ausfiel |

Zusätzlich sucht `--lookback-days 3` ältere, noch unverarbeitete Sendungen.
Die Systemzeitzone des Pi ist Europe/Berlin, `OnCalendar` ist damit lokal
korrekt.

Ein Nachhollauf ist billig, weil bereits verarbeitete Geschichten in
`daily-processed.sqlite` stehen und übersprungen werden.

## Verbrauchsgrenzen

`btcedu/core/editorial/limits.py`. Drei Grenzen, alle ohne Vorgabewert — ein
Standardbudget, das niemand bewusst gesetzt hat, ist genau der Fehler, der Geld
kostet:

- `--budget-usd` Tagesbudget
- `--max-calls` Anbieteraufrufe pro Tag
- `--max-stories` Geschichten pro Tag

Der Tag ist ein Kalendertag in Europe/Berlin.

**Reserviert wird vor dem Aufruf, nicht nach der Antwort.** `DailyLedger`
schätzt konservativ (inklusive `_HIDDEN_PROMPT_CHARS`, weil System- und
Werkzeugtext in der Rechnung landet, aber nicht im übergebenen Prompt sichtbar
ist), bucht die Reservierung, ruft dann auf und trägt anschließend die
tatsächlichen Kosten nach. Verbraucht ist `COALESCE(actual_usd, reserved_usd)`.

Daraus folgen zwei Eigenschaften, die beide beabsichtigt sind:

- Eine **verworfene** Antwort bleibt gebucht. Sie wurde bezahlt.
- Ein **Absturz** mitten im Aufruf bleibt gebucht. Sonst wäre eine
  Absturzschleife gratis, und genau die würde das Budget aushebeln.

Der Ledger liegt in SQLite, nicht im Prozess. Ein Neustart setzt die Zähler
deshalb nicht zurück.

Ein Task ohne Obergrenze in `MAX_TOKENS_BY_TASK` wird abgelehnt statt geschätzt:
eine unbegrenzte Antwort ist eine unbegrenzte Rechnung.

### Budget freigeben

Die Unit liefert `ALMANYA24_DAILY_BUDGET_USD=0` und `ALMANYA24_DAILY_MAX_CALLS=0`
aus, also gesperrt. Die Freigabe geschieht über die nicht versionierte Datei

```
data/almanya24-preview/daily-budget.env
```

nach dem Muster in `deploy/daily-budget.env.example`. Sie wird per
`EnvironmentFile=-` eingebunden; fehlt sie, bleibt der Lauf gesperrt und meldet
`budget_not_approved`, statt etwas auszugeben.

## Betriebszustände

`RunOutcome`, sichtbar auf `/_durum/`:

| Zustand | Bedeutung |
| --- | --- |
| `success` | Mindestens ein Entwurf erzeugt und angezeigt |
| `no_new_transcript` | Keine unverarbeitete Sendung gefunden |
| `no_suitable_stories` | Sendung vorhanden, aber kein verwertbares Thema |
| `budget_not_approved` | Grenzen stehen auf 0 |
| `budget_exhausted` | Tagesgrenze im Lauf erreicht |
| `failed` | Technischer Fehler |
| `already_running` | Ein anderer Lauf hält den Lock |

## Nebenläufigkeit, Wiederaufnahme, Teilfehler

`RunLock` ist eine Sperrdatei mit `O_EXCL` plus PID-Prüfung, damit ein nach
einem Absturz liegengebliebener Lock den nächsten Lauf nicht dauerhaft
blockiert.

`ProcessedStories` merkt sich jede Geschichte samt Ergebnis. Ein Neustart
bezahlt abgeschlossene Schritte nicht erneut. Fehlversuche werden gezählt und
nach `MAX_STORY_ATTEMPTS = 2` nicht mehr wiederholt — dieselbe Eingabe
unverändert erneut zu senden, erzeugt dieselbe Rechnung und denselben Fehler.

Eine gescheiterte Geschichte beendet den Lauf nicht; die übrigen laufen weiter.

## Belegrecherche ohne Anbieterschlüssel

Für die Websuche ist kein Schlüssel konfiguriert. `btcedu/services/free_search.py`
recherchiert deshalb über zwei schlüsselfreie Endpunkte: die tagesschau-API und
die MediaWiki-API. `_deduplicate` verschränkt die Treffer herausgeberweise,
damit nicht ein Herausgeber die Liste füllt.

Das ist **kein Ersatz für einen allgemeinen Webindex**. Wenn zu einem Claim nur
ein Herausgeber etwas liefert, fällt die Bewertung der Quellenunabhängigkeit
entsprechend schwach aus, und der Claim wird nicht als solide belegt geführt.
Das ist gewollt sichtbar und wird nicht kaschiert.

## Entwicklungsfreigabe

`NEWSROOM_DEV_AUTO_RELEASE=true` wird ausschließlich in
`deploy/almanya24-daily.service` gesetzt; ein Test hält fest, dass keine andere
Unit den Schalter setzt. Die inhaltlichen Prüfungen laufen unverändert weiter
und ihre Ergebnisse stehen in der Reviewansicht. Im Entwicklungsmodus blockiert
ein negatives redaktionelles Urteil die Anzeige nicht, sondern erscheint als
Banner auf dem Artikel und als Marke auf der Karte. Die technischen Prüfungen
bleiben fatal. Ohne den Schalter ist das Freigabeverhalten unverändert.

Eine menschliche Freigabe wird dabei nicht vorgetäuscht: die Version trägt
`decision_id="dev-auto-release"`, und es existiert keine `EditorialDecision`.

## Installation

```
sudo /home/pi/AI-Startup-Lab/almanya24-newsroom-dev/deploy/activate-almanya24-dev.sh
```

installiert Vorschaudienst und Timer, prüft danach den Zugriffsschutz und nennt
den freigegebenen Budgetstand.

Status: `systemctl status almanya24-daily.timer`,
Logs: `journalctl -u almanya24-daily.service`.
