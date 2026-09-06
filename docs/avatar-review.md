# Die Avatarprüfung im Dashboard

Zwischen `anchorgen` und `render` steht ein Tor: `review_gate_anchor`. Es
zeigt einem Menschen die rohen Moderatorinnenclips, bevor irgendetwas
zusammengesetzt wird, und lässt den Render erst zu, wenn genau diese Clips
freigegeben wurden.

Das ist bewusst der frühe Zeitpunkt. Ein misslungener Take ist hier ein
Klick; nach dem Render ist er eine halbe Stunde Rechenzeit auf dem Pi.

Die abschließende Sendungsfreigabe (`review_gate_3`) bleibt unverändert
bestehen und prüft, was diese Stufe nicht prüfen kann: Studio, Themenmonitor,
Reporterbilder, Ton, Untertitel und die Sendung als Ganzes.

## Wann das Tor überhaupt gilt

Alle vier Bedingungen müssen zutreffen:

| Bedingung | Sonst |
| --- | --- |
| `ANCHOR_ENABLED=true` | übersprungen |
| Profil setzt `anchor.review_required: true` | übersprungen |
| Provider ist `heygen` | übersprungen (der D-ID-Pfad hat keinen Szenenplan) |
| Die Episode hat Anchor-Szenen | übersprungen |

`bitcoin_podcast`, alte D-ID-Episoden und jede Installation ohne Avatar
laufen dadurch unverändert durch. Für `tagesschau_tr` ist
`review_required: true` gesetzt — bewusst als eigener Schalter und nicht als
Teil von `auto_approve_reviews`: dieses Profil erledigt seine redaktionellen
Tore unbeaufsichtigt, aber das Gesicht auf dem Bildschirm sieht sich ein
Mensch an.

## Woran eine Freigabe hängt

Eine Freigabe gilt für *diese* Clips, nicht für die Episode. Gebunden wird
sie an eine Datei, die die Stufe selbst schreibt:

```
outputs/<episode>/anchor/review_digest.json
```

Der Digest enthält geordnet und gerundet:

* die geplanten Moderatorinnen-Szenen (ID, Kapitel, Reihenfolge, Text-Hash,
  Audiodatei, Dauer)
* die Presenter-Zuweisung (Look-ID, Look-Name, Engine, Content-Hash)
* Provider, Engine und die Identität des Anchor-Manifests
* pro Clip: Look-ID, Audio-Hash, Content-Hash, Status, Dauer
* die **Jobstatus aus dem Ledger**
* offene, bestätigte Regenerationen

Der Umweg über eine eigene Datei hat einen Grund. Die vorhandene
Artefaktbindung hasht Dateibytes, aber ein Job, der in der Reconciliation
landet, ändert nur eine Datenbankzeile — das Manifest auf der Platte bleibt
gleich. Ohne den Digest überlebte eine Freigabe genau den Fall, für den sie
nicht gelten darf.

Umgekehrt enthält der Digest bewusst **nicht** den Hash des gesamten
Szenenplans. Ein ausgetauschtes Reporterbild würde sonst eine Freigabe
entwerten, die nie vom Reporter handelte.

Der Digest wird nur geschrieben, wenn sich sein Inhalt ändert. Ein
unveränderter Lauf lässt die Datei in Ruhe, sonst sähe jedes Artefakt jünger
aus als die Entscheidung darüber.

## Was eine Freigabe verhindert

Der Freigabeknopf bleibt gesperrt, solange eines davon zutrifft:

* nicht alle erwarteten Szenen sind fertig
* eine Szene ist `reserved`, `failed`, `abandoned` oder `reconcile_required`
* ein Job gilt als fertig, aber die Datei fehlt
* eine Szene verwendet ein anderes Outfit als die Episodenzuweisung
* eine Szene ist beanstandet
* `anchor-readiness` meldet Blocker in Konfiguration oder Rechten

Studio-Blocker sperren diese Stufe absichtlich nicht: sie halten den Render
auf und werden am finalen Tor geprüft. Ein Tor, das niemanden auf die Clips
schauen lässt, bevor die Kulisse fertig ist, verhindert genau die frühe
Prüfung, für die es existiert.

## Fail-closed

`render_video()` weigert sich, eine Episode mit unfreigegebenen
Moderatorinnenclips zu rendern — auch über `btcedu render`, nicht nur über
die Pipeline. Es gibt keinen stillen Rückfall auf den alten Vollbild-Modus;
der Renderer wählt niemals selbst einen Fallback (siehe
`core/anchor_fallback.py`).

## HTTP-Schnittstelle

Alle Pfade liegen unter `/api/episodes/<episode_id>/avatar`.

| Methode | Pfad | Zweck |
| --- | --- | --- |
| GET | `/avatar` | Zusammenfassung, Szenenliste, Kosten, Reviewstatus |
| GET | `/avatar/scenes/<scene_id>/preview` | den rohen Clip streamen |
| POST | `/avatar/approve` | Avatarstufe freigeben |
| POST | `/avatar/reject` | Avatarstufe ablehnen (Begründung nötig) |
| POST | `/avatar/scenes/<scene_id>/flag` | eine Szene beanstanden oder freigeben |
| POST | `/avatar/scenes/<scene_id>/regenerate` | `prepare` / `confirm` |
| POST | `/avatar/look` | Outfit wechseln |

Beispielantwort (gekürzt):

```json
{
  "schema_version": 1,
  "episode_id": "20260822_tagesschau",
  "anchor_enabled": true,
  "provider": "heygen",
  "engine": "avatar_iii",
  "avatar_type": "digital_twin",
  "studio_mode": "composite",
  "look_name": "look_02",
  "look_id": "heygen_l…0001",
  "review_status": "pending",
  "review_hash": "9f2c1ab4…7e10",
  "expected_scene_count": 9,
  "completed_scene_count": 9,
  "blocked_scene_count": 0,
  "cost_per_second_usd": 0.0167,
  "actual_cost_usd": 2.6721,
  "estimated_remaining_usd": 0.0,
  "approvable": true,
  "blockers": [],
  "scenes": [
    {
      "scene_id": "sc_001",
      "chapter_id": "ch_01",
      "order": 1,
      "duration_seconds": 17.4,
      "look_id": "heygen_l…0001",
      "job_status": "completed",
      "provider_job_id": "b31f0c…9a2",
      "cost_usd": 0.29058,
      "preview_available": true,
      "revision": 0,
      "flagged": false
    }
  ]
}
```

Bezeichner werden gekürzt ausgegeben (`abcdefgh…wxyz`): erkennbar für einen
Betreiber, unbrauchbar als Zugangsdaten. API-Schlüssel, absolute Pfade und
Rechtevertragsdaten erscheinen nirgends.

Wer freigibt, schickt den `review_hash` mit, den die Seite angezeigt hat.
Passt er nicht mehr, antwortet der Server mit **409** statt die Entscheidung
auf Clips zu buchen, die der Betreiber nie gesehen hat.

## Vorschau

Ausgeliefert wird nur, was im Anchor-Manifest steht, und nur über die
gemeinsame Pfadprüfung (`_validate_episode_path`): kein Requestparameter
benennt jemals eine Datei. Symlinks aus dem Episodenverzeichnis heraus werden
abgewiesen, Byte-Ranges werden unterstützt, und die Download-URL des
Providers erreicht den Browser nicht.

Die Vorschau ist im Dashboard eindeutig als **„Avatarclip — Studio wird im
finalen Render ergänzt"** beschriftet.

## Erneut kaufen: `retry` ist nicht `regenerate`

Ein Transport- oder Pollingfehler wird wiederholt und kostet nichts. Ein
neuer Clip ist eine neue Rechnung. Beide dürfen nicht ineinander übergehen,
deshalb sind sie getrennt.

Eine Regeneration läuft in zwei Schritten:

1. **`prepare`** eröffnet einen `AvatarRegenerationRequest` und nennt die
   bisherigen und die zusätzlich erwarteten Kosten. Autorisiert ist damit
   nichts.
2. **`confirm`** bestätigt genau die Revision, die im Dialog stand.

Warum eine eigene Tabelle und keine Spalte am Job: die Absicht existiert
früher als die Arbeit. Der Unique-Constraint auf
`(episode_id, scene_id, revision)` macht aus einem Doppelklick, einem
wiederholten POST und einem ungeduldigen Betreiber *eine* Zeile statt zweier
Rechnungen — ohne Read-Modify-Write-Rennen.

Die Revision fließt in den Content-Hash des Jobs ein. Der neue Auftrag ist
damit nach der Regel des Ledgers selbst eine andere Arbeit, statt dieselbe
Arbeit an einer Eindeutigkeitsprüfung vorbeizuschieben. Revision 0 wird aus
dem Hash-Payload weggelassen, sodass jeder vor dieser Änderung berechnete
Hash byteweise gleich bleibt.

Der alte Job und der alte Clip bleiben erhalten. Bestätigen entwertet die
Avatarfreigabe und die Renderartefakte; andere Szenen, TTS und
Reportermedien bleiben unberührt. Aus einer Webanfrage heraus wird kein
Provider kontaktiert — den bestätigten Auftrag holt sich der nächste
`anchorgen`-Lauf.

Eine Szene mit `reconcile_required` kann nicht regeneriert werden. Für einen
Auftrag mit unbekanntem Ausgang einen Ersatz zu kaufen, zahlt zweimal; erst
`btcedu avatar-reconcile`.

## Outfitwechsel

Vor dem ersten HeyGen-Auftrag ist der Wechsel kostenlos und ändert nur die
Presenter-Zuweisung. Danach antwortet die API mit **409**, nennt die bereits
ausgegebenen Kosten und verlangt `confirm_rebuild` — denn das zugewiesene
Outfit ist das, wofür bezahlt wurde.

## Audit

Freigabe, Ablehnung, Beanstandung, Vorbereitung und Bestätigung einer
Regeneration werden mit Zeitpunkt, Grund und einer nicht vertraulichen
Betreiberreferenz festgehalten. `requested_by_ref` und `confirmed_by_ref`
bleiben getrennt, damit sichtbar ist, dass zwei Handlungen stattfanden —
auch wenn dieselbe Person beide vorgenommen hat.
