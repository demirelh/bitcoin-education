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

## Redaktionsmodell

Der Modellpfad läuft über das **Copilot-Abonnement des Betreibers**
(`copilot_cli`, Modell `claude-opus-5`), nicht über einen abgerechneten
OpenAI-Schlüssel. Festgelegt in `scripts/almanya24_preview/daily_run.py`
(`PROVIDER`, `MODEL`, `USD_METERED`).

`EditorialModel` lässt diesen Anbieter zu, weil
`claude_service._call_copilot_cli` den Teilprozess verschlossen hält:
`--no-custom-instructions`, `--no-ask-user`, `--available-tools=view`,
`--allow-tool=view` und ein `--add-dir`, das nur auf das Verzeichnis der
Prompt-Temporärdatei zeigt — von der Unit zusätzlich durch `PrivateTmp=true`
isoliert. Das eine verbliebene Werkzeug existiert ausschließlich, damit das
Modell den ihm übergebenen Prompt lesen kann. Keine Shell, kein Netzwerkwerkzeug,
keine Rückfrage an einen Operator. Ein Test in
`tests/test_editorial_provider_outcomes.py` liest diese Flags aus dem Quelltext,
damit eine spätere Bequemlichkeitsänderung ungeprüfte Nachrichtentexte nicht
still an einen handlungsfähigen Agenten übergibt.

## Verbrauchsgrenzen

`btcedu/core/editorial/limits.py`. Drei Grenzen, alle ohne Vorgabewert — ein
Standardbudget, das niemand bewusst gesetzt hat, ist genau der Fehler, der Geld
kostet:

- `--budget-usd` Tagesbudget **(bei Copilot wirkungslos, siehe unten)**
- `--max-calls` Anbieteraufrufe pro Tag
- `--max-stories` Geschichten pro Tag

Der Tag ist ein Kalendertag in Europe/Berlin.

**Ein Abonnement meldet keinen Preis pro Aufruf.** Deshalb gibt `run_daily()`
den Parameter `usd_metered` durch. Steht er auf `False`:

- Die Freigabe hängt allein an `--max-calls`; ein Nullbudget ist kein Stopp
  mehr. Ein verlangtes USD-Budget wäre eine Zahl, gegen die nie etwas
  verglichen wird, und die echte Grenze sähe daneben wie eine Formalie aus.
- Der Ledger bucht `0.00`. Eine geschätzte Zahl einzutragen hieße, die
  Tagesübersicht wie eine Rechnung aussehen zu lassen, die niemand bekommt.
- `--max-calls 0` bleibt ein vollständiger Stopp, und ein Task ohne Obergrenze
  in `MAX_TOKENS_BY_TASK` bleibt abgelehnt: eine unbegrenzte Antwort ist auch
  im Abonnement unbegrenzt.

**Bei einem abgerechneten Anbieter wird vor dem Aufruf reserviert, nicht nach
der Antwort.** `DailyLedger` schätzt konservativ (inklusive
`_HIDDEN_PROMPT_CHARS`, weil System- und Werkzeugtext in der Rechnung landet,
aber nicht im übergebenen Prompt sichtbar ist), bucht die Reservierung, ruft
dann auf und trägt anschließend die tatsächlichen Kosten nach. Verbraucht ist
`COALESCE(actual_usd, reserved_usd)`.

Daraus folgen zwei Eigenschaften, die beide beabsichtigt sind:

- Eine **verworfene** Antwort bleibt gebucht. Sie wurde bezahlt.
- Ein **Absturz** mitten im Aufruf bleibt gebucht. Sonst wäre eine
  Absturzschleife gratis, und genau die würde das Budget aushebeln.

Der Ledger liegt in SQLite, nicht im Prozess. Ein Neustart setzt die Zähler
deshalb nicht zurück.

### `0.00 USD` heißt „nicht erfasst", nicht „kostenlos"

Das ist der Punkt, an dem eine Übersicht am leichtesten lügt. Bei
`usd_metered=False` bucht der Ledger `0.00`, weil der Anbieter keinen Preis
meldet — nicht, weil der Aufruf nichts gekostet hätte. Er verbraucht weiterhin
Kontingent des Abonnements. Eine Anzeige „0,0000 / 2,0000 USD" neben einem
Fortschrittsbalken behauptet das Gegenteil.

Deshalb trägt der Bericht `usd_metered` direkt neben der Zahl, die es
qualifiziert (`RunReport.usd_metered`, gespiegelt in `usage`), und die
Statusseite ersetzt die USD-Zeile in diesem Fall durch „USD kaydedilmiyor"
mit der Begründung. Ein Nullbudget liest sich dort dann auch nicht mehr als
Sperre — gesperrt ist der Lauf im Abonnement erst bei `--max-calls 0`. Die
bindende Grenze, die Zahl der Anbieteraufrufe, steht unverändert daneben.

### Angefordertes und bestätigtes Modell

`LedgerGuardedModel.confirmed_models` sammelt die Modellkennung, mit der der
Anbieter tatsächlich geantwortet hat; `RunReport.model_requested` hält fest,
was verlangt wurde. Die beiden fallen auseinander (angefordert
`claude-opus-5`, geliefert `copilot/claude-opus-5`), und GitHub zieht
Modell-IDs ohne Vorwarnung zurück. Nur das Angeforderte zu berichten wäre eine
Behauptung, die niemand geprüft hat.

Eine leere Liste heißt „in diesem Lauf hat kein Anbieter etwas bestätigt" —
etwa weil alle Antworten aus `news_provider_operations` wiederverwendet wurden.
Das ist eine Aussage über den Lauf, keine über das Modell.

### Grenzen freigeben

Die Unit liefert `ALMANYA24_DAILY_MAX_CALLS=0` aus, also gesperrt. Die Freigabe
geschieht über die nicht versionierte Datei

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
| `provider_unavailable` | Modellkonto verweigert (kein Guthaben/Schlüssel ungültig); Lauf stoppt, Geschichte wartet ohne Fehlversuch |
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

### Auch der Entwurfsprüfung gegenüber (2026-10-03)

Der Schalter wirkte zunächst auf die Gates *um* die Texterzeugung herum
(Themenkontinuität, Belegstärke, semantische Prüfung), nicht auf die
deterministische Prüfung des Entwurfs gegen die geprüften Aussagen in
`generate_article_revision()`. Ein Entwurf, der eine Aussage mit dem Verdikt
`insufficient` behauptete, scheiterte deshalb mit `UnsupportedClaimReferenced`,
**bevor** die Revision gespeichert wurde — im Entwicklungsmodus verschwand die
Geschichte dadurch vollständig, statt ihre Ablehnung zu zeigen. Genau das sollte
die Vorschau sichtbar machen.

`generate_article_revision(..., dev_auto_release=True)` ändert daran genau eine
Sache: Ein Entwurf, der die Prüfung endgültig nicht besteht, wird nicht mehr
verworfen, sondern als `draft` gespeichert, dessen `block_reason` **jede**
Verletzung benennt (`_draft_violations()`, nicht nur die erste). Die Prüfung
selbst, das Reparaturbudget und das Verhalten ohne den Schalter sind unverändert.

Die Grenze verläuft bei der Speicherbarkeit, nicht beim Urteil: Nennt ein
Entwurf eine Aussage, die nicht zu dieser Revision gehört, gibt es keine Zeile,
auf die der Fremdschlüssel des Absatzes zeigen könnte. `UnknownClaimReferenced`
bleibt deshalb auch im Entwicklungsmodus fatal — das ist kein negatives
redaktionelles Urteil über speicherbaren Text, sondern nicht speicherbarer Text.

`workflow.py` führt die Befunde des Entwurfs und die der umgebenden Gates
zusammen, statt sie zu überschreiben.

### Gezielter Retry einer einzelnen Geschichte

`ALMANYA24_RETRY_STORY=<story_key>` löscht genau einen aufgezeichneten
Fehlschlag und protokolliert das. Gedacht für die Überprüfung einer Korrektur:
`ALMANYA24_RETRY_FAILED_BEFORE` gibt alle Fehlschläge vor einem Zeitpunkt frei
und verbraucht damit das Tagesbudget an denselben Sackgassen. Nur ein `failed`
wird entfernt; eine Geschichte mit `needs_decision` wartet auf einen Menschen und
wird nicht hinter dessen Rücken neu gestartet. Die Unit setzt den Schalter nicht.

## Installation

```
sudo /home/pi/AI-Startup-Lab/almanya24-newsroom-dev/deploy/activate-almanya24-dev.sh
```

installiert Vorschaudienst und Timer, prüft danach den Zugriffsschutz und nennt
den freigegebenen Budgetstand.

Status: `systemctl status almanya24-daily.timer`,
Logs: `journalctl -u almanya24-daily.service`.

## Befunde aus den ersten echten Läufen (2026-09-12)

Sechs Läufe gegen echte Transkripte, mit freigegebenem Budget (2,00 USD /
60 Aufrufe / 6 Geschichten). Verbraucht: 38 Aufrufe, 0,76 USD.
Drei Implementierungsfehler wurden dabei belegt und behoben, zwei Ablehnungen
als berechtigt bestätigt.

### Behoben

**1. Ganze Schlagzeilen als Suchanfrage.** Beide freien Endpunkte sind
Schlagwortsuchen und verlangen jeden Term. Eine vollständige Schlagzeile lieferte
reproduzierbar null Treffer — ohne Fehler, die APIs antworteten einwandfrei. Sieben
von acht Geschichten starben daran. `_query_variants` reduziert die Anfrage jetzt
schrittweise auf ihre tragenden Begriffe und lockert weiter, solange nur ein
Herausgeber antwortet (`tests/test_free_search.py`).

**2. `reconcile_required` war eine Sackgasse.** Eine einmal gescheiterte Suche
blockierte jeden weiteren Versuch, und nichts im Code konnte den Zustand je
auflösen — für einen unbeaufsichtigten Lauf ist die Geschichte damit endgültig tot.
`core/editorial/reconcile.py` löst ausschließlich nachweislich folgenlose
Operationen auf (Typ `search`, keine Kosten, kein gespeichertes Ergebnis). Ein
Modellaufruf oder eine bezahlte Suche bleibt blockiert, ein `submitted` ebenso —
genau die Fälle, für die die Sperre existiert.

**3. Die Belegprüfung verglich Bytes statt Wörter.** `_store_evidence_link`
prüfte die Passage als rohen Substring, obwohl `_anchor_text` im selben Modul
bereits für genau dieses Problem existiert. Zusätzlich punktiert die
Textextraktion die Grenze zwischen Überschrift und Folgeabsatz nicht, sodass ein
korrekt zitierender Entwurf einen Punkt schreibt, den das Dokument nicht trägt.
Der Vergleich läuft jetzt über `_anchor_text` und fällt auf die reine Wortfolge
zurück. Eine erfundene Passage, eine geänderte Zahl und eine eingefügte Negation
werden weiterhin abgelehnt; dafür gibt es je einen Test.

### Berechtigt abgelehnt

- Eine Passage, die aus dem **Suchsnippet** statt aus dem abgerufenen Dokument
  stammte. Sie kommt im Dokument nicht vor — die Ablehnung ist richtig.
- Eine Passage, die zwei getrennte Stellen des Dokuments **zusammensetzt**. Der
  erste Satz steht dort, die Fortsetzung an anderer Stelle; als eine Passage
  ausgegeben ist sie kein Zitat.

Die Fehlermeldung nennt jetzt den Anfang der abgelehnten Passage samt Längen, weil
sich diese beiden Fälle sonst nur durch einen weiteren bezahlten Lauf
unterscheiden lassen.

### Offener redaktioneller Zustand

`propose_updates` fragt, ob eine Sendung ein bestehendes Thema fortschreibt. Das
ist eine Entscheidung, die nur eine Redaktion treffen kann, und der Tageslauf
trifft sie ausdrücklich nicht. Solche Geschichten erhalten den Status
`needs_decision` und den Ausgang `needs_editorial_decision`; sie zählen nicht
gegen die Versuchsgrenze, weil die Frage vor dem ersten bezahlten Aufruf entsteht
und kein Retry sie beantwortet.

### Verbleibende Einschränkung

Nach diesen Korrekturen läuft der Ablauf technisch durch, aber es entstand noch
**kein neuer Artikel aus einem Transkript**. Die Gates arbeiten korrekt; was fehlt,
ist Belegtiefe. Zwei schlüsselfreie Herausgeber decken eine Abendsendung nicht ab,
und für viele Aussagen findet sich bei ihnen schlicht kein Dokument, das die
konkrete Zahl oder das konkrete Zitat trägt. Das ist eine Frage der Quellenbasis,
nicht der Implementierung, und sollte nicht durch Aufweichen der Prüfungen
beantwortet werden.

## What the first real timer runs exposed

Four defects, each found by a run rather than by reading the code:

1. **A free search reserved a fee.** The query plan carried a generic
   `0.005 USD` placeholder while the key-less endpoints charge nothing. That
   overstated the day's spend and, worse, made a failed search look like an
   operation with an uncertain bill, so `reconcile_operations` refused to
   release it and the story stayed dead. The provider now states its own price
   (`cost_per_search_usd`); only an unknown provider falls back to the plan.
2. **One unsupported claim discarded the whole story.** The agreed rule is that
   insufficiently supported statements are excluded. `research_revision` now
   drops the claim, records it in `ResearchOutcome.unsupported`, and continues
   with the claims that do hold up.
3. **An unusable claim was still offered to the drafter.** It appeared in the
   prompt with verdict `unassessed`, the model asserted it, and the draft was
   then rejected for doing so. It is now withheld, and a revision with no
   assessed claim at all is refused *before* the paid call.
4. **A failure was permanent.** `ALMANYA24_RETRY_FAILED_BEFORE=<timestamp>`
   forgets failures recorded before that moment, which is what makes a deployed
   fix retryable without turning into a standing licence to retry: failures
   after the stamp are untouched.

### Development switch, extended

With `NEWSROOM_DEV_AUTO_RELEASE=true` the switch additionally carries these
forward as findings instead of suppressing the story: an evidence-gate block,
each excluded claim, and an open topic-update proposal. The proposal case
drafts under the broadcast's own new topic — the proposals stay `OPEN`, so
nothing is merged on a reviewer's behalf. No `EditorialDecision` is ever
written; a warning is not a signature.

## Known provider outcomes are not uncertain ones (2026-10-01)

Sixteen stories died with "Uncertain model operation requires
reconciliation". The cause was a classification error, not the guard: on
2026-09-30 the OpenAI account answered HTTP 429 `credit_balance_exhausted`,
yet every exception from a model call was filed as `reconcile_required`. The
retry at 22:30 hit the guard, and after `MAX_STORY_ATTEMPTS` the story was
gone.

Exceptions are now split by what is actually known (`editorial/jobs.py`):

- **Refused (HTTP 4xx, `provider_rejection_status`)** — the provider answered
  before doing any work. The operation is closed as `failed` with cost 0 and
  can be retried; the daily ledger settles it at 0 (`outcome="rejected"`).
- **Paid but unusable (`ModelReplyUnusable`)** — e.g. a reply that is not
  JSON. Closed as `failed` at its real price; the price still counts against
  the run budget (`reserved_cost_usd`) and the daily ledger.
- **Unknown (timeout, connection loss, 5xx, anything else)** — unchanged:
  `reconcile_required`, no automatic retry.

An account-level refusal (`provider_account_unusable`: 4xx *and*
`classify_error` = quota/auth) ends the run with `provider_unavailable`. The
story is reported as `waiting_for_provider` and is not recorded in
`ProcessedStories`, so an empty balance no longer burns retry attempts.
Operations left in `reconcile_required` by earlier runs are not touched
automatically.

## Behobener Defekt: themenfremde Treffer aus der freien Quellensuche (2026-10-03)

Im Lauf vom 2026-10-03 (`research_run_id` 100) lieferte die Suche für den Claim
`filialen_bleiben_geoeffnet` der Galleria-Insolvenz zwei Wikipedia-Artikel über
eine Turiner Gemäldegalerie (*Hans Memling*) und ein Triester Museum (*Triest*).
Die Belegprüfung hat beide korrekt als `inconclusive` eingestuft — das Belegtor
arbeitete also richtig. Der Defekt saß davor, in
`btcedu/services/free_search.py`, und hatte zwei unabhängige Ursachen.

**1. Unabhängigkeit wurde vor Relevanz gezählt.** Die Lockerungsschleife in
`FreeNewsSearchProvider.search()` brach ab, sobald ein zweiter Herausgeber
*überhaupt etwas* geantwortet hatte. Die MediaWiki-Volltextsuche trifft auf das
beiläufige italienische Wort „Galleria", und dieser Treffer beendete die Suche,
bevor das Nachrichtenarchiv eine beantwortbare Abfrage gesehen hatte.

Jetzt wird jeder Treffer zuerst gegen die **Themenbegriffe der Abfrage**
geprüft (`_topic_terms` / `_is_on_topic`). Ein Treffer gilt nur dann als zur
Geschichte gehörig, wenn er mindestens zwei verschiedene Themenbegriffe
enthält. Erst danach werden unabhängige Herausgeber gezählt. Drei Details
machen das belastbar:

* Bindestrich-Komposita werden aufgetrennt, weil „Galleria-Filialen" Dokumente
  findet, die nur eine der beiden Hälften enthalten.
* Kalender-, Mengen- und Währungswörter (`_GENERIC`: Jahren, Mal, Prozent,
  Euro, Monatsnamen …) zählen nicht als Themenüberschneidung. Sie stehen in
  fast jedem deutschen Nachrichtensatz; ohne diese Liste genügte „Galleria" plus
  „Jahren", um einen Artikel über die Belle Époque relevant erscheinen zu lassen.
* Verglichen wird auf **Wortgrenzen** mit kompositatolerantem Präfix, nicht auf
  Teilzeichenketten — sonst wäre „Ketterer" ein Treffer für „Kette".

Bleibt nach allen Abfragevarianten kein themenbezogener Treffer übrig, liefert
der Adapter ein **leeres** Ergebnis und protokolliert, wie viele Treffer er als
themenfremd verworfen hat. Keine Belege sind das ehrlichere Ergebnis als
themenfremde Belege; die Belegprüfung selbst wurde nicht gelockert. Ein
`SearchProviderError` entsteht nur noch, wenn kein Kanal überhaupt geantwortet
hat. Ursprünglich warf der Adapter auch bei beantworteter, aber themenfremder
Suche — das brach ab 02.10. jede betroffene Meldung komplett ab (19 Meldungen),
statt sie ohne Beleg als Entwurf weiterzuführen. Zusätzlich zählen die von der
Gegensuche angehängten Wörter (`Widerspruch Korrektur Faktencheck`),
Fragewörter/Demonstrativa (`Wer`, `Diese` …) und Tageszeitwörter (`Abend`,
`morgen` …) nicht mehr als Themenwörter.

**2. Bearbeitungszeit wurde als Veröffentlichungszeit ausgegeben.**
`_wikipedia()` schrieb den MediaWiki-`timestamp` — den Zeitpunkt der letzten
**Bearbeitung** — in `published_at`, dasselbe Feld, in dem ein
Nachrichtenartikel sein Erscheinungsdatum führt. Ein Artikel über ein Museum
des 19. Jahrhunderts, zuletzt vor zehn Tagen bearbeitet, sah nachgelagert aus
wie zehn Tage alte Berichterstattung.

`SearchHit` hat jetzt ein eigenes Feld `modified_at`. Wikipedia-Treffer tragen
`published_at=None` und die Revisionszeit in `modified_at`; ein
tagesschau-Artikel trägt weiterhin sein echtes `published_at` und
`modified_at=None`. Eine unbekannte Veröffentlichungszeit bleibt damit
unbekannt, statt durch eine andere Größe ersetzt zu werden.

**Regressionsnachweis:** `tests/test_free_search.py` spielt die tatsächlich
aufgezeichneten Antworten aus `news_research_queries` 672 und 674 offline ab —
ohne Netzwerk und ohne Modellaufruf. Abgedeckt sind beide Fehlerbilder, die
Gegenprobe (ein *themenbezogener* Wikipedia-Treffer wird weiterhin
angenommen), das Weiterlockern trotz antwortendem Zweitherausgeber und beide
Datumsfälle.
