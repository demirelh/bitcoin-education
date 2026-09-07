# Dashboard: Avatarbetrieb, Klärungsfälle und Voice-over-Notfallpfad

Bedienungsdokumentation zu WP-7. Ergänzt `docs/avatar-operations.md` (Betrieb
und Integritätsvertrag) und `docs/plans/almanya24-avatar-iii-recovery.md`
(Fortschritt).

Der Leitsatz: **das Dashboard entscheidet nichts.** Es zeigt persistierten
Zustand an und ruft für jede Aktion dieselbe Domainfunktion auf, die auch die
CLI benutzt. Es gibt keine zweite Zustandslogik im Weblayer, und aus einem
Browserrequest wird niemals ein Providerauftrag erzeugt.

## 1. Anzeige

Der Avatarbereich einer Episode zeigt sechs klar getrennte Blöcke:

| Block | Inhalt |
| --- | --- |
| Avatarstatus | Outfit, Szenen, Clips, Reviewstand, Freigabe |
| Providerbetrieb | Provider, Engine, Studio-Modus, Readiness, Circuit Breaker |
| Jobausführung | Parallelitätsgrenze, laufend, freie Slots, wartend, Polling, Download, Validierung, Retry, Klärungsfälle, nächster Poll |
| Kosten | gebucht, reserviert, ungeklärt, gebunden, Stufenlimit, Rest, Warnung |
| Klärungsfälle | offene Reconciliation mit Entscheidung je Job |
| Voice-over-Notfallpfad | Prepare/Confirm beziehungsweise Widerruf |

Alle Zahlen stammen aus `btcedu/core/avatar_runtime.py::runtime_snapshot()`
und damit aus derselben Quelle wie die CLI-Ausgabe. Ein Neustart ändert sie
nicht, weil sie aus dem Ledger und nicht aus einem Prozessspeicher kommen.

**Ungeklärte Kosten zählen als ausgegeben.** `budget_remaining_usd` zieht
gebuchte, reservierte *und* ungeklärte Beträge ab. Ein Job mit unbekanntem
Ausgang kann berechnet worden sein; ihn als kostenlos zu behandeln würde einen
zweiten Kauf mit bereits ausgegebenem Geld genehmigen.

Nicht ausgegeben werden: API-Schlüssel, Idempotency-Keys, signierte
Download-URLs, vollständige Providerpayloads und Rechteinformationen.

## 2. Klärungsfälle (Reconciliation)

Sichtbar sind ausschließlich Jobs in `reserved` oder `reconcile_required`.
Erlaubt sind exakt die Entscheidungen der CLI:

* `running` — Provider bestätigt, dass der Job noch erzeugt wird
* `delivered` — Provider bestätigt die Fertigstellung, Datei wurde geholt
* `not-billed` — Provider bestätigt eine Ablehnung ohne Abrechnung
* `unresolved` — Provider kann den Job nicht identifizieren, Zeile bleibt blockiert
* `abandon` — der Clip wird aufgegeben, die Kosten bleiben auf der Episode

Zusätzlich: Provider-Job-ID von Hand anhängen (`.../attach`) und den
Circuit Breaker auditiert schließen (`/api/avatar/breaker/<provider>/reset`).

**Ein 404 beim Provider ist kein Beweis für „nicht berechnet“.** Die
Detailansicht sagt das ausdrücklich; die sichere Entscheidung ist `unresolved`
oder `abandon`.

### Zweistufigkeit

1. `POST .../resolve` mit `action=prepare` — verändert **nichts** und liefert
   einen `digest` der Zeile.
2. `POST .../resolve` mit `action=confirm`, `decision`, `reason` und genau
   diesem `digest`.

Der Digest umfasst Status, Provider-Job-ID, Kosten, Versuchszähler, den
jüngsten Zeitstempel **und die letzte Auditzeile**. Letzteres ist kein Detail:
`unresolved` verändert keine einzige Spalte, ein Digest ohne Auditbindung würde
danach weiter passen und die widersprechende Entscheidung einer zweiten Person
unbemerkt darüberlegen.

* Digest veraltet → **409**, mit dem aktuellen Digest in der Antwort.
* Digest veraltet, aber die letzte Auditzeile ist genau diese Entscheidung →
  `already_applied` mit **200**. So bleibt ein Doppelklick eine Entscheidung.
* Bereits abgeschlossener Job → **422**. Ein erledigter Job wird nie
  wiedereröffnet.

## 3. Voice-over-Notfallpfad

Ein Ausnahmepfad für **eine** Episode. Er wird nie vorgeschlagen, nie
automatisch gewählt und nie auf andere Episoden übertragen.

### Ablauf

1. **Prepare** (`POST /api/episodes/<id>/avatar/voice-over`, `action=prepare`)
   zeigt betroffene Anchor-Szenen, Blocker, gebuchte/reservierte/ungeklärte
   Kosten, die stattdessen verwendeten Medien und die Folgen. Es wird nichts
   verändert und nichts protokolliert.
2. **Confirm** verlangt `reason`, `acknowledged: true` und den `digest` aus
   Schritt 1. Der Digest bindet Szenenplan, Anchor-Manifest (inklusive
   Bytehashes) und Jobzustand; ändert sich davon etwas, ist die Bestätigung
   ein **409**.
3. Bestätigt wird genau dreierlei: eine Auditzeile, die Ungültigkeit der
   Anchor- und Renderfreigaben und ein `.stale`-Marker im Renderverzeichnis.

Was **nicht** passiert: kein Providerauftrag, kein Abbruch laufender Jobs,
keine Kostenstornierung, keine neue Sprachsynthese, keine neue Mediengenerierung.

### Widerruf

Vor der Veröffentlichung darf der Override auditiert widerrufen werden. Die
ursprüngliche Entscheidung bleibt in der Historie — nur ihre Wirkung endet.
Danach gilt wieder der Avatarpfad und die Episode ist erneut blockiert, bis
alle Clips und Freigaben gültig sind; automatisch beauftragt wird nichts. Nach
der Veröffentlichung ist ein Widerruf **422**.

### Auswirkung auf den Render

Für eine Override-Episode gilt:

* Anchor-Szenen werden wie Reporterszenen vollflächig mit dem bereits
  zugeordneten Themenmedium gerendert (`shows_presenter()` in
  `scene_renderer.py`).
* Fehlt ein Medium, greift `fallback_display_media` des Studios; fehlt auch das,
  bleibt der Render fail-closed.
* Der Szenenplan wird **nicht** umgeschrieben: die Szene bleibt eine
  Studioszene, sie wird nur nicht als solche gedreht.
* Die Originalsprachspur, die Kapitelreihenfolge und der deterministische
  Wetterpfad bleiben unverändert.
* `presentation_mode` steht im Render-Manifest — `avatar` oder
  `voice_over_override` — und wird nie aus den Bildern erschlossen.
* Der Szenen-Content-Hash enthält den Modus, damit kein Shot aus einem
  früheren Avatar-Render wiederverwendet wird.
* Beim Remote-Render reist die Entscheidung im Job mit und wird auf dem Runner
  in dessen Wegwerfdatenbank nachgezogen; Anchor-Clips werden weder verlangt
  noch verschickt.
* `_require_anchor_approval()` verlangt für eine Override-Episode keine
  Anchor-Freigabe mehr — die Clips werden ja nicht gezeigt. Beurteilt wird die
  geänderte Präsentation im finalen `review_gate_3`, das erneut durchlaufen
  werden muss.

## 4. API-Verträge

Alle Antworten sind JSON, ohne Stacktrace und ohne Secret.

| Route | Methode | Zweck |
| --- | --- | --- |
| `/api/episodes/<id>/avatar` | GET | Reviewzustand + `runtime` + `presentation` |
| `/api/avatar/reconcile` | GET | offene Klärungsfälle (`?episode_id=` optional) |
| `/api/avatar/reconcile/<job_id>` | GET | Detail, Auditverlauf, Integritätsstatus, Digest |
| `/api/avatar/reconcile/<job_id>/resolve` | POST | `prepare` / `confirm` |
| `/api/avatar/reconcile/<job_id>/attach` | POST | Provider-Job-ID anhängen |
| `/api/avatar/breaker/<provider>/reset` | POST | Circuit Breaker schließen |
| `/api/episodes/<id>/avatar/voice-over` | GET/POST | `prepare` / `confirm` / `revoke` |

Statuscodes:

* **400** ungültige Eingabe, fehlende Begründung, kein JSON
* **403** Cross-Origin-Request
* **404** unbekannte Episode oder unbekannter Job
* **409** veralteter Digest oder konkurrierende Entscheidung
* **422** fachlich blockiert (abgeschlossener Job, fehlende Kenntnisnahme,
  Widerruf nach Veröffentlichung)

Bestehende Felder wurden ergänzt, nicht ersetzt: ältere Konsumenten von
`/api/episodes/<id>/avatar` sehen dieselben Schlüssel wie zuvor.

## 5. Sicherheit

Das Dashboard hat keinen eigenen Login; es steht hinter dem Reverse Proxy und
jeder Aufrufer ist bereits Betreiber. Was fehlt, ist Schutz davor, dass eine
fremde Seite im Nachbartab in seinem Namen postet. Die zustandsändernden
Avatarrouten verlangen deshalb `application/json` — das kann ein Browser ohne
Preflight nicht cross-origin senden — und lehnen einen fremden `Origin`-Header
ab. Das ist kein Ersatz für Authentifizierung, sondern die Absicherung, die zur
vorhandenen Architektur passt.

Bekannte, bewusst offene Punkte:

* Kein Benutzerkonto, daher kein personenbezogenes Audit — `operator_ref` ist
  ein vom Proxy weitergereichtes oder mitgesendetes Label.
* Studio-Plates und Themenmedien sind weiterhin nicht bytehashgebunden
  (siehe WP-8 in der Recovery-Checkliste).
