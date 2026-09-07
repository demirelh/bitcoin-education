# ALMANYA24 — synthetischer Ende-zu-Ende-Trockenlauf

Der Trockenlauf führt eine vollständige ALMANYA24-Sendung durch die echte
Produktionsverdrahtung — `sceneplan → anchorgen → review_gate_anchor → render →
review_gate_3 → publish` — und ersetzt dabei ausschließlich die *externen*
Systeme durch kontrollierte Fakes. Er kostet nichts, benötigt kein Netz und
veröffentlicht nichts.

```bash
btcedu smoke-test-almanya24            # temporäres Verzeichnis, wird aufgeräumt
btcedu smoke-test-almanya24 --keep     # Artefakte bleiben liegen, Pfad wird gemeldet
btcedu smoke-test-almanya24 --dir /tmp/x --keep
```

Exitcode `0` heißt: alle acht Phasen gemeldet `[OK]`, genau ein simulierter
privater Upload. Exitcode `1` heißt: eine Phase hat abgebrochen; die Zeile
`[FAIL] <phase>: <grund>` nennt die Ursache. Mit `--keep` liegt daneben das
gerenderte Testvideo, dessen Pfad am Ende ausgegeben wird.

## Wozu er dient — und wozu nicht

Der Trockenlauf beweist **Verdrahtung, Reihenfolge, Buchführung und
Verweigerungen**: dass nur Moderatorinnenszenen beauftragt werden, dass ein
einziger Look für die ganze Episode gilt, dass beide Review-Gates wirklich
anhalten, dass die Freigabe an den Digest gebunden ist, dass Kapitel-MP3s die
einzige Tonquelle bleiben und dass ein zweiter Lauf nichts neu kauft.

Er beweist **keine Bildqualität**. Alle Medien sind synthetische Farbflächen und
Tongeneratoren; die Presenterclips stammen von einem Fake-Provider. Aussagen wie
„die Moderatorin sitzt sauber im Studio“ kann erst der echte HeyGen-Pilot mit
Phase-1-Assets treffen. Der Smoke-Test prüft nur, dass die Regionen im Bild
nicht leer sind und die technischen Kennwerte stimmen.

| | synthetischer Smoke-Test | echter HeyGen-Pilot |
|---|---|---|
| Provider | Fake, keine Anfrage | HeyGen Avatar III |
| Kosten | keine | reale Abrechnung |
| Assets | erzeugte Fixtures | Phase-1-Studio, echter Look |
| Aussage | Logik, Buchführung, Fail-closed | Aussehen, Lippensynchronität, Freistellung |

## Verwendete Fakes

* `FakeHeyGenService` — nimmt Aufträge an, zählt sie, liefert vorbereitete
  Clips. Jeder Auftrag wird mit Szenen-ID, Look und Audio-Hash protokolliert,
  damit Tests prüfen können, *was* beauftragt wurde.
* `install_fake_downloader` — beantwortet Ergebnis-URLs aus dem Dateisystem.
* `FakeYouTubeService` — simuliert genau einen Upload gegen ein Testziel mit
  Sichtbarkeit `private`; es gibt keine OAuth-Datei und keinen Netzzugriff.
* Bild-/Videoanbieter werden nicht erreicht: Themenmedien liegen bereits als
  Fixtures im Episodenverzeichnis.
* Der Remote-Renderer wird über den lokalen Paketvertrag geprüft, nicht über
  GitHub Actions.

## Erwartete Haltepunkte

1. Nach `anchorgen` hält `review_gate_anchor`. Die Freigabe wird an den Digest
   der Anchor-Artefakte gebunden.
2. Nach `render` hält `review_gate_3`. Erst die finale Freigabe setzt die
   Episode auf `APPROVED`.
3. `publish` läuft ausschließlich gegen das Testziel. Da es kein
   Produktionsziel ist, bleibt die Episode absichtlich `APPROVED`; der
   `PublishJob`-Datensatz ist der Nachweis.

## Erzeugte Artefakte

`presenter_assignment`, `scene_plan.json`, `avatar_jobs`-Ledger,
`anchor/manifest.json`, Anchor-Review-Digest, Studio-Hash,
`render_manifest.json` mit Timeline, Untertitel, `youtube_metadata.json`,
`PublishJob`, Kostenaufstellung und Reviewhistorie. Der E2E-Test prüft, dass
alle Querverweise, IDs und Hashes zueinander passen.

## Grenzen dieser Maschine

Das hier installierte ffmpeg kann **kein** WebM erzeugen, dessen Alphakanal von
ffprobe gemeldet wird (`libvpx` und `libvpx-vp9` mit `-pix_fmt yuva420p` melden
beide `yuv420p`). Deshalb läuft die synthetische Welt standardmäßig im
opaken Pfad (`alpha_mode="opaque_mp4"`, `studio_mode: baked`,
`output_format: mp4`). Der transparente Compositing-Pfad ist nur über seine
*Verweigerungen* abgedeckt — ein opaker Clip, der einem Alpha-Auftrag angeboten
wird, wird abgelehnt — sowie über strukturelle Tests, die ffmpeg nie erreichen.
Dass die WebM-Route Ende zu Ende funktioniert, kann erst der echte Pilot zeigen.

## Fehlerdiagnose

* `[FAIL] anchorgen: … refused before any provider call` — Preflight hat
  angehalten: Studio-Manifest, Rechteeintrag oder Anchor-Konfiguration. Genau
  dieses Verhalten ist gewollt; `btcedu anchor-readiness` nennt dieselben Gründe.
* `[FAIL] render` — meist eine fehlende oder zu kleine Fixture; das
  Finalreview verlangt Mindestgrößen.
* `[FAIL] publish` — fehlende Translation-QA-Freigabe oder falsches Ziel.
* Mit `--keep` lässt sich das Verzeichnis vollständig inspizieren; die Struktur
  entspricht einem echten Episodenverzeichnis.

Kein Testasset ist als produktives Phase-1-Asset verwendbar: Profil, Episode und
Look tragen die Markierung `SMOKE` und der echte Readiness-Befehl akzeptiert
sie nicht.
