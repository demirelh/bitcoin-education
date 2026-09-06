# Avatar-Betrieb: Parallelität, Retries, Recovery

Betriebsdokumentation für die HeyGen-Avatarstufe von ALMANYA24 (WP-5C).
Ergänzt `docs/plans/almanya24-avatar-iii-recovery.md` (Fortschritt) und
`docs/runbooks/` (Failover). Zielgruppe ist der Betreiber, nicht der Compiler.

Der Leitsatz der gesamten Stufe: **ein Clip wird höchstens einmal gekauft.**
Alles Weitere — Parallelität, Backoff, Circuit Breaker — existiert nur, damit
dieser Satz auch dann gilt, wenn das Netz, der Provider oder der Strom
ausfällt.

## 1. Konfiguration

Profil `btcedu/profiles/tagesschau_tr.yaml`, Abschnitt `anchor`:

```yaml
anchor:
  max_concurrent_jobs: 3      # gleichzeitige HeyGen-Aufträge
  poll_interval_seconds: 5    # Abstand zwischen zwei Statusabfragen
  request_timeout_seconds: 120
  poll_timeout_seconds: 1800  # danach wird vertagt, nicht neu gekauft
```

Validiert in `btcedu/core/anchor_config.py`:

* Minimum 1 paralleler Job.
* Hartes Maximum `HEYGEN_MAX_CONCURRENT_JOBS = 5`. Für dieses Konto ist **kein**
  offizielles Providerlimit bestätigt, deshalb ein bewusst konservativer Deckel
  statt einer geratenen Zahl. Ein höherer Wert im Profil ist ein Konfigurationsfehler
  und bricht den Start der Stufe ab.
* `poll_interval_seconds` und `request_timeout_seconds` sind nach oben und unten
  begrenzt; ein Intervall von 0 wäre eine Busy-Wait-Schleife gegen den Provider.

## 2. Ablauf einer Anchor-Stufe

`btcedu/core/avatar_coordinator.py` arbeitet in drei sauber getrennten Phasen:

1. **Planen** (`plan_scene_work`, Hauptthread, mit Session)
   Ledgerentscheidung je Szene (`reserve_scene`), Budgetprüfung,
   Idempotency-Key, Suche nach einem bereits hochgeladenen Audioasset.
2. **Ausführen** (`AvatarCoordinator.run`, Threadpool, **ohne** Session)
   Worker bekommen reine Dataclasses. Sie können die Datenbank nicht erreichen,
   also können sie auch keine Schreibtransaktion über einen zwei Minuten langen
   Providervorgang offen halten.
3. **Persistieren** (Hauptthread)
   Worker melden über eine `queue.Queue` zurück; der Hauptthread schreibt und
   committet. Die Provider-Job-ID wird sofort nach Annahme geschrieben.

Vor jedem einzelnen Auftrag wird erneut geprüft: Circuit Breaker offen? Budget
noch gedeckt? Erst dann geht ein Submit hinaus.

Zur Parallelität zählen auch **bereits eingereichte** Jobs aus einem früheren
Lauf. Ein Resume belegt einen Slot wie eine Neubestellung.

## 3. Retry- und Backoff-Matrix

`btcedu/core/avatar_retry.py`. Zwei Fragen werden strikt getrennt:

* **retryable** — darf genau dieser Aufruf jetzt wiederholt werden?
* **ambiguous** — könnte der Provider ihn trotzdem angenommen und abgerechnet
  haben?

Nur die zweite Frage entscheidet über Geld. Ein mehrdeutiger Fehler, der alle
Retries überlebt, endet in `reconcile_required` — niemals in einem zweiten Kauf.

| HTTP / Fehler | retryable | ambiguous | Folge |
| --- | --- | --- | --- |
| 401 / 403 | nein | nein | permanent, Reservierung frei, Breaker zählt |
| 400 / 422 | nein | nein | Provider hat abgelehnt, nichts begonnen, Reservierung frei |
| 404 auf Poll/Download | begrenzt | **ja** | kein Beweis, dass nichts berechnet wurde → Reconciliation |
| 404 auf Capability/Look | nein | nein | schlicht „nicht gefunden“ |
| 429 | ja | nein | `Retry-After` wird respektiert, gleicher Idempotency-Key |
| 5xx auf POST/Create | ja | **ja** | Retry mit exakt demselben Key; danach Reconciliation |
| 5xx auf GET/Status/Download | ja | nein | begrenztes Backoff |
| Connect-Timeout / Refused | ja | nein | Request hat den Provider nie erreicht |
| Read-Timeout auf Create | ja | **ja** | Auftrag läuft möglicherweise bereits |

Backoff ist exponentiell mit Jitter und gedeckelt; ein `Retry-After` schlägt die
Kurve. `Retry-After` wird sowohl als Sekundenwert als auch als HTTP-Datum
verstanden. Wartezeiten laufen über eine injizierbare `sleep`/`now`-Funktion —
die Tests warten nie wirklich.

## 4. Idempotency-Key

* `sha256(episode:scene:content_hash)`, stabil pro Generation-Attempt.
* Wird **vor** dem Provideraufruf persistiert, samt Ablaufzeit (24 h).
* Jeder Retry desselben Attempts nutzt denselben Key, auch nach Prozessneustart.
* Eine bewusste Regeneration (WP-5B) ändert `content_hash` und damit
  automatisch den Key.
* Nach Ablauf des 24-Stunden-Fensters wird ein unklarer Auftrag **nicht**
  automatisch erneut abgeschickt; er wird `reconcile_required`.
* Der Key ersetzt nicht die dauerhafte Job-ID im Ledger — er ist die zweite
  Absicherung, nicht die erste.

## 5. Polling und Download

* Mehrere Jobs werden abwechselnd gepollt, mit `poll_interval_seconds` Abstand.
* Providerstatus wird auf interne Zustände abgebildet; ein **unbekannter**
  Status ist fail-closed (`reconcile_required`), weil Warten hängt und
  „vermutlich fehlgeschlagen“ doppelt zahlt.
* `poll_timeout_seconds` bedeutet nicht „fehlgeschlagen und kostenlos“, sondern
  *vertagt*: die Zeile bleibt `submitted`, der nächste Lauf nimmt sie wieder auf.
* Download läuft gestreamt in eine `.part`-Datei und wird erst nach dem letzten
  Chunk umbenannt; Größenober- und -untergrenze, SHA-256 beim Schreiben.
* Danach ffprobe: Container, Auflösung und Alpha sind hart, FPS und Dauer sind
  Warnungen. Eine abgelehnte Datei wird **quarantänisiert, nicht gelöscht** —
  sie wurde bezahlt und ist Beweismittel.
* Eine abgelaufene Download-URL wird durch **eine** zusätzliche read-only
  Statusabfrage erneuert. Niemals durch eine neue Generierung.

## 6. Circuit Breaker

`btcedu/core/avatar_breaker.py`, Zustand in der Datenbank, pro Provider.

* Mehrere aufeinanderfolgende Auth-, Quota-, 429- oder 5xx-Fehler öffnen ihn.
* Offen heißt: **keine neuen Submits**. Laufende Jobs dürfen weiterhin
  read-only gepollt und heruntergeladen werden — sie sind bereits bezahlt.
* Eine reine Ablehnung (400/422) zählt nicht mit; sie sagt etwas über die
  Anfrage aus, nicht über den Provider.
* Cooldown und Grund sind sichtbar in CLI und Dashboard.
* Zurücksetzen ist ausdrücklich manuell und auditiert:

```bash
btcedu avatar-breaker status
btcedu avatar-breaker reset --operator-ref ops-anna --note "Kontingent aufgeladen"
```

Automatisches Vergessen wäre bequem und genau der Weg, auf dem ein Konto
gesperrt oder ein Kontingent zweimal verbrannt wird.

## 7. Beobachtbarkeit

```bash
btcedu avatar-status <episode_id>          # Menschliche Übersicht
btcedu avatar-status <episode_id> --json   # Maschinenlesbar
```

Dieselben Daten liegen unter `runtime` in
`GET /api/episodes/<episode_id>/avatar`:

aktive Jobs, freie Slots, wartende Submits, nächster Pollzeitpunkt,
Retryanzahl, letzter HTTP-Fehlertyp, `Retry-After`, Breaker-Zustand,
reservierte / tatsächliche / **ungeklärte** Kosten und der Validierungsstatus
jedes Clips.

Nicht enthalten und niemals auszugeben: API-Schlüssel, vollständige
Providerpayloads, signierte Download-URLs.

## 8. Wenn etwas schiefgeht

| Symptom | Bedeutung | Nächster Schritt |
| --- | --- | --- |
| Stufe bricht mit „still generating“ ab | Poll-Timeout, Jobs laufen weiter | Stufe später erneut starten; **nicht** neu beauftragen |
| Job steht auf `reconcile_required` | Ausgang unbekannt, evtl. bezahlt | `btcedu avatar-reconcile inspect <id>`, dann `attach` oder `resolve` |
| Breaker `open` | Provider antwortet dauerhaft schlecht | Ursache klären, dann `avatar-breaker reset` mit Notiz |
| Clip `quarantined` | heruntergeladen, aber technisch unbrauchbar | Datei in `anchor/quarantine/` prüfen, Szene ggf. bewusst regenerieren |
| `avatar-status` zeigt ungeklärte Kosten | Geld ist nicht zugeordnet | Reconciliation, bevor die Episode veröffentlicht wird |

## 9. Was bewusst *nicht* passiert

* **Kein automatischer Voice-over-Fallback.** Eine fehlende Moderatorinnen-Szene
  blockiert. Ein manueller, episodenbezogener Override bleibt auditiert und
  erzwingt ein erneutes finales Review.
* **Kein Provideraufruf aus dem Renderer oder aus einem Webrequest.** Das
  Dashboard bereitet vor und bestätigt; gekauft wird ausschließlich im
  Pipelinepfad.
* **Keine automatische Neubeauftragung** nach einem mehrdeutigen Fehler.
* **Keine Änderung am D-ID-Pfad.** Der Altprovider läuft weiter über
  `submit_anchor_video` / `collect_anchor_video`, mit Ledger und Parallelität,
  aber ohne die granulare Retry-Matrix — bewusste Rückwärtskompatibilität.
