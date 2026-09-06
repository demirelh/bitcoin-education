# ALMANYA24 Avatar III — Recovery-Checkliste

Stand: 6. September 2026

Diese Datei ist die dauerhafte Fortschrittsspur für die ALMANYA24-Avatar-III-Arbeit.
Sie wurde nach einem Absturz der Copilot-CLI-Sitzung aus dem Arbeitsverzeichnis
rekonstruiert, damit der Stand nicht erneut nur in einer Sitzung existiert.

Ergänzt (nicht ersetzt) `docs/plans/almanya24-media-roadmap.md`. Der ältere
`docs/plans/ai-news-anchor.md` beschreibt den D-ID-MVP und ist historisch.

Legende: `[x]` fertig · `[~]` teilweise · `[ ]` offen · `[!]` extern blockiert

## Unveränderliche Anforderungen

Jedes Arbeitspaket wird gegen diese Liste geprüft:

1. HeyGen **Avatar III** (nicht IV/V) — Profil setzt `engine: avatar_iii` explizit.
2. Ein Outfit pro Episode, persistent zugewiesen, nie mitten in der Episode gewechselt.
3. Restart-fähige HeyGen-Jobs ohne Doppelabrechnung.
4. Moderatorin im standardisierten ALMANYA24-Studio.
5. Aktuelles Themenbild professionell im Studiomonitor.
6. Reporter bleibt unsichtbar; seine Blöcke sind vollflächige Medien.
7. Vorhandene Medienpipeline (imagegen, Wetter, Render) weiterverwenden.
8. Transparentes WebM bevorzugt, opaker MP4-Fallback bleibt erhalten.
9. Original-TTS (ElevenLabs) bleibt die finale Audioquelle.
10. Keine echten HeyGen-Aufrufe, keine externen Kosten in Entwicklung und Tests.
11. Andere Profile (bitcoin_podcast, D-ID) dürfen nicht beschädigt werden.

## Rekonstruierter Repository-Stand

Letzter Commit: `cd8b8e0` — *Give ALMANYA24 an outfit that cannot change mid-episode*.

Uncommitted im Arbeitsverzeichnis (nichts davon wurde verworfen):

| Datei | Art | Inhalt |
|---|---|---|
| `btcedu/core/scene_planner.py` | neu | Stage `sceneplan`, deterministische Shot-Liste |
| `tests/test_scene_planner.py` | neu | 46 Tests für den Planer |
| `btcedu/models/episode.py` | geändert | `EpisodeStatus.SCENE_PLANNED`, `PipelineStage.SCENEPLAN` |
| `btcedu/core/pipeline.py` | geändert | Stage-Registrierung, Status-Ordnung 15.3, Dispatch, Resume |
| `btcedu/cli.py` | geändert | `btcedu sceneplan`, Resume-Statusliste |
| `btcedu/core/anchor_generator.py` | geändert | akzeptiert `SCENE_PLANNED` |
| `btcedu/core/renderer.py` | geändert | akzeptiert `SCENE_PLANNED` |
| `btcedu/core/remote_render.py` | geändert | akzeptiert `SCENE_PLANNED` |
| `btcedu/web/api.py` | geändert | Stage-Label, Stage-Mapping, erlaubte Aktion |
| `btcedu/web/jobs.py` | geändert | `_do_sceneplan`, Reset-Matrix, Resume-Liste |
| `report.*.json` (4 Dateien) | untracked | Heap-Dumps des Absturzes, **nicht** Teil der Arbeit |

Am 6. September 2026 in dieser Wiederherstellungssitzung ergänzt (WP-1):

| Datei | Art | Inhalt |
|---|---|---|
| `btcedu/models/avatar_job.py` | neu | `AvatarJob`, `AvatarJobStatus` |
| `btcedu/core/avatar_jobs.py` | neu | Reservierungsprotokoll des Job-Registers |
| `tests/test_avatar_jobs.py` | neu | 35 Tests, keine Provideraufrufe |
| `btcedu/migrations/__init__.py` | geändert | Migration 016 `avatar_jobs` |
| `btcedu/models/__init__.py` | geändert | `AvatarJob` exportiert |
| `tests/test_pipeline.py` u. a. | geändert | Stage-Erwartungen um `sceneplan` ergänzt |

## Vollständig umgesetzt

### Avatar-Grundlage (committet)

- [x] `AnchorService`-Protokoll ist providerneutral; D-ID bleibt unverändert nutzbar.
- [x] `HeyGenService`: Audio-Upload, Videoauftrag, Polling, Download, MP4 **und** WebM.
- [x] `heygen_cost_per_second()` mit Engine-/Avatar-Typ-Tabelle; Avatar III explizit.
- [x] Enge Fehlerklassen (`AnchorAPIError`) für Auth, Quota, Providerfehler, Timeout.
- [x] `DryRunAnchorService` — Entwicklung und Tests ohne einen einzigen echten Aufruf.
- [x] `core/anchor_config.py`: `AnchorConfig`, `PresenterLook`, `StudioConfig`,
      `RightsConfig`; Provider/Engine/Format/Studio-Modus sind profilbesessen.
- [x] `core/presenter_assignment.py`: `ensure_assignment()` / `select_look()` /
      `reassign_look()`, Least-recently-used-Rotation, Artefakt + Provenienz.
- [x] `models/presenter_assignment.py` + Migration 015 mit `UNIQUE(episode_id)` —
      Anforderung 2 wird von der Datenbank durchgesetzt, nicht von Anwendungscode.
- [x] Profil `tagesschau_tr`: `engine: avatar_iii`, `output_format: webm`,
      `studio_mode: composite`, `cost_per_second_usd` mit Quelle und Prüfdatum,
      `max_cost_usd: 7.0`, `max_concurrent_jobs: 4`, zehn Look-Platzhalter.
- [x] `anchor_enabled=false` bleibt global Standard; ohne Aktivierung kein Aufruf.

### Scene-Plan-Stage (uncommitted, aber vollständig und grün)

- [x] `plan_scenes()` folgt dem Stage-Muster: Guards, Idempotenz-Hash,
      Provenienz, `PipelineRun`, `.stale`-Invalidierung, `error_message`-Pflege.
- [x] Sprecherblöcke aus `chapters.json`; Blocklängen exakt wie im Renderer
      gewichtet, damit Avatar-Clip und Bild nicht auseinanderlaufen.
- [x] Templates: Opening, Anchor, Anchor-Return, Reporter, Wetter, Closing.
- [x] `anchor_scenes()` ist die **einzige** Stelle, die entscheidet, was zum
      Provider geht — der Reporter erreicht HeyGen nie (Anforderung 6).
- [x] Wetterkapitel bleiben `weather_renderer`, werden nie generiert (Anforderung 7).
- [x] Plan-Hash enthält Look-ID, Texthash, Dauer und Hintergrundbild.
- [x] Look-Zuweisung wird nur bei `anchor_enabled` **und** Provider `heygen`
      angefordert; andere Profile bleiben unberührt (Anforderung 11).
- [x] Kostenfrei: kein Provideraufruf, kein Kosteneintrag.
- [x] `scene_plan.json` liegt im Episodenverzeichnis und wird vom
      Remote-Render-Paket automatisch mitgeliefert (nicht in `_JOB_EXCLUDED`).
- [x] Stage in Pipeline, CLI, Web-API und JobManager registriert.
- [x] `tests/test_scene_planner.py` — 46 Tests, grün.
- [x] Bestandstests an die neue Stufe angepasst: Stage-Anzahl 20 → 21,
      `EpisodeStatus` 18 → 19, `PipelineStage` 21 → 22, Stage-Reihenfolge und
      Label-Menge in `test_pipeline.py`, `test_phase9_pipeline_integration.py`,
      `test_sprint1_models.py`, `test_web_progress.py`.

### WP-1 — Dauerhaftes Avatar-Job-Register (6. September 2026)

- [x] `models/avatar_job.py`: `AvatarJob` mit
      `UNIQUE(episode_id, scene_id, content_hash)` — ein Clip wird einmal
      gekauft, und die Datenbank setzt das durch, nicht der Anwendungscode.
- [x] `AvatarJobStatus`: `reserved`, `submitted`, `completed`, `failed`,
      `reconcile_required`.
- [x] Migration 016 `CreateAvatarJobsTableMigration`, prüf-vor-Handlung,
      mehrfach ausführbar, mit Unique- und Statusindex.
- [x] `core/avatar_jobs.py`: `reserve_scene()` schreibt die Zeile **vor** dem
      Aufruf und antwortet mit `submit` / `reuse` / `resume` / `reconcile`.
- [x] `record_submission()` persistiert die Provider-Job-ID sofort; ein Neustart
      danach pollt den bestehenden Auftrag statt einen zweiten zu kaufen.
- [x] Eine `reserved` gebliebene Zeile wird beim nächsten Lauf **nicht**
      wiederholt, sondern `reconcile_required` — der Ausgang ist unbekannt, und
      genau dort entstünde die Doppelabrechnung (Anforderung 3).
- [x] `record_refusal()` nur für nachweislich nicht berechnete Ablehnungen
      (401, Quota, Validierung); nur diese Zeilen dürfen erneut versuchen.
- [x] `hold_for_reconciliation()` für Timeouts und abgebrochene Downloads.
- [x] `resolve_job()` als Operator-Entscheidung mit Pflichtnotiz, Ausgang
      `delivered` oder `not_billed`.
- [x] `episode_avatar_cost()` zählt reservierte und ungeklärte Zeilen mit — ein
      Budget, das Unbekanntes als kostenlos behandelt, gibt zweimal frei.
- [x] `compute_job_hash()`: Look, Text, **Audio**, Engine, Format, Auflösung und
      Seitenverhältnis. Avatar III und Avatar IV sind verschiedene Arbeit.
- [x] Providerneutral; D-ID nutzt dasselbe Register (Anforderung 11).
- [x] `tests/test_avatar_jobs.py` — 35 Tests, kein einziger Provideraufruf.

## Teilweise umgesetzt

- [~] **Pipeline-Verdrahtung `sceneplan`**: Stage läuft, aber
  `_STAGE_WORKFLOW_KEY`-Kommentar in `web/api.py:826` nennt die Hilfsstufen noch
  ohne `sceneplan`, und `STAGE_PROVIDER_MAP` kennt die kostenfreie Stufe nicht.
  Kosmetisch, kein Funktionsfehler.
- [~] **Renderer-Statuskette**: `renderer.py` und `remote_render.py` akzeptieren
  `SCENE_PLANNED`, konsumieren `scene_plan.json` aber noch nicht. Der Renderer
  baut seine Blöcke weiterhin selbst.
- [~] **Anchor-Stage**: Der Scene-Pfad ist umgesetzt (WP-2). Offen bleiben
  Parallelität/Backoff und das Operator-Kommando `btcedu avatar-reconcile`.
- [~] **Kostenprüfung Avatar**: Der Scene-Pfad prüft szenenweise gegen
  `max_cost_usd` und `episode_avatar_cost()`. Der Kapitelpfad kennt weiterhin
  nur Kapitel — bewusst unverändert, damit D-ID nicht betroffen ist.
- [~] **WebM**: Der Compositor legt jetzt Alpha-WebM über das Studio (WP-3).
  Offen bleibt die Renderer-Integration (WP-4) und das echte Studio-Artwork aus
  Phase 1 — bis dahin ist die Bereitschaftsprüfung bewusst rot.

## Noch offen

Grob in der Reihenfolge, in der die Pakete aufeinander aufbauen.

### WP-1 — Dauerhaftes Avatar-Job-Register — **erledigt**, siehe oben

Offen bleibt nur die Anbindung: das Register wird noch von keiner Stage benutzt.

### WP-2 — Anchor-Stage auf Scene-Plan und Job-Register umgestellt (6. September 2026)

- [x] `generate_anchors()` wählt den Pfad allein anhand der Existenz von
      `scene_plan.json`. Ohne Plan läuft der Kapitelpfad unverändert weiter —
      gespeicherte D-ID-Episoden und alles vor dem Plan bleiben lauffähig
      (Anforderung 11).
- [x] Nur `anchor_scenes()` wird beauftragt. Der Reporter erzeugt keine Zeile im
      Register und keinen Auftrag (Anforderung 6).
- [x] Der Wetter-Übergabeblock ist zwar `needs_avatar`, wird aber im
      Anchor-Generator zusätzlich ausgeschlossen und mit Grund im Manifest
      vermerkt. Das Wetterbild bleibt deterministisch gerendert (Anforderung 7).
- [x] Audioquelle ist immer eine vorhandene TTS-`speaker_part`-Datei. Ein Block
      aus mehreren Takes wird **nicht** zusammengemischt, sondern als ein Clip je
      Take beauftragt (`<scene_id>_p00`, `_p01`, …). Damit ist die an HeyGen
      übergebene Audiodatei stets ein Artefakt, das die TTS-Stufe wirklich
      erzeugt hat, und das Original-TTS bleibt die spätere Render-Audioquelle
      (Anforderung 9).
- [x] Alle Anchor-Szenen einer Episode verwenden die persistierte Look-ID aus
      `PresenterAssignment`; `_create_anchor_service()` nimmt sie als Override
      entgegen. Weicht `presenter_look_id` im Plan davon ab, bricht die Stage
      fail-closed ab, statt zwei Outfits in einer Sendung zu mischen
      (Anforderung 2).
- [x] Budget-Preflight über die geplante Moderatorinnendauer: `episode_avatar_cost()`
      plus Schätzung genau der Clips, die tatsächlich beauftragt würden.
      Reservierte, eingereichte und ungeklärte Zeilen zählen bereits im
      Register mit und werden nicht doppelt veranschlagt. Vor jedem einzelnen
      bezahlten Aufruf wird erneut geprüft.
- [x] Jede Einheit läuft durch `reserve_scene()`: `submit` kauft,
      `resume` pollt ausschließlich, `reuse` nimmt die vorhandene Datei,
      `reconcile` bricht mit `AnchorReconciliationRequired` fail-closed ab
      (Anforderung 3).
- [x] `record_submission()` läuft unmittelbar nach der Providerannahme und vor
      dem langen Polling — das ist die Zeile, die aus einem Absturz eine
      Wiederaufnahme statt einer zweiten Rechnung macht.
- [x] Ein Fehler beim Einreichen wird nur dann als „nicht berechnet“ gewertet,
      wenn der Provider nachweislich abgelehnt hat (400/401/403/404/422, Quota).
      Alles andere — Timeout, abgerissene Verbindung — geht nach
      `reconcile_required`.
- [x] `AnchorRequest.idempotency_key` trägt den Content-Hash als zusätzliche
      Absicherung. Ob HeyGen den Header ehrt, ist nicht verifiziert; die Garantie
      liegt im Register, nicht im Header.
- [x] Manifest v2 (`schema_version: "2.0"`) mit `scenes[]`: Scene-ID, Clip-ID,
      Kapitel-ID, Sprecherrolle, Template, Look-ID, Audio-Pfad, Audio-Hash,
      Content-Hash, Provider-Job-ID, Status, Dauer, Größe, Kosten, Format.
      `segments` bleibt bewusst leer, damit der Renderer sich exakt wie bisher
      verhält, bis WP-3 ihn auf Szenen umstellt.
- [x] Provenienz enthält dieselben Felder je Clip plus Plan- und TTS-Manifest als
      Eingaben; `MediaAsset`- und `ContentArtifact`-Zeilen wie bisher.
- [x] Dry-Run schreibt **keine** Registerzeile. Eine „erledigte“ Dry-Run-Zeile
      würde später wiederverwendet und den echten Kauf stillschweigend
      überspringen (Anforderung 10).
- [x] Teilweise erfolgreiche Episoden werden nach Neustart fortgesetzt: bereits
      fertige Clips werden wiederverwendet, nur der Rest wird gekauft.
- [x] `tests/test_anchor_generator_scenes.py` — 22 Tests, kein Provideraufruf.

Innerhalb von WP-2 bewusst **nicht** erledigt (verschoben):

- [ ] Parallelität `max_concurrent_jobs`, 429-/5xx-Backoff (eigene Einheit).
- [ ] `btcedu avatar-reconcile` als Operator-Kommando für `resolve_job()`.

### WP-3 — Studio-Manifest, Validierung und FFmpeg-Compositing (6. September 2026)

Umgesetzt in `btcedu/core/studio_manifest.py`, `btcedu/services/studio_compositor.py`,
`btcedu/core/studio_media.py`, `assets/almanya24/studio/`.

- [x] Kanonisches JSON-Schema (Schema-Version 1): Schema-/Studio-/Asset-Version,
      Auflösung, FPS, Studiohintergrund, Intro-/Einschwenkasset, Studioloop,
      Display-Zone mit optionalen vier Eckpunkten, Fit-Modus, Fokuspunkt,
      Presenter-Position und -Skalierung, Shadow-Layer, Vordergrund/Desk-Layer,
      Logo-Zone, Lower-Third-/Ticker-/Untertitel-Safe-Areas, Alpha-Modus,
      opaker MP4-Fallback, Hashes und Provenienz.
- [x] Strikter Loader mit Pfadeinhausung: Assetpfade müssen relativ sein, dem
      Muster `^[A-Za-z0-9][A-Za-z0-9._/-]*$` genügen und **nach**
      Symlinkauflösung im Studioverzeichnis liegen. Ein Manifest ist eine
      Datei, also ein nicht vertrauenswürdiger Eingabewert.
- [x] Geometrievalidierung: die Display-Zone darf Lower Third, Ticker und
      Untertitel nicht überlappen — sonst verdeckt das Themenbild die Schrift.
- [x] Fail-closed im `opaque_mp4`-Modus: ohne `presenter_free`-Zone **und** ohne
      `occlusion_mask` lädt das Manifest nicht. Dieselbe Prüfung wiederholt der
      Compositor, damit ein im Code gebautes Manifest sie nicht umgeht.
- [x] `assets/almanya24/studio/manifest.example.json` mit Platzhalterpfaden und
      `placeholder: true`. Ein produktives `manifest.json` existiert bewusst
      **nicht**; ein Test hält das fest.
- [x] `studio_readiness_problems()` / `require_studio_ready()`: fehlende, leere
      oder digest-abweichende Assets und Platzhalterflags blockieren.
- [x] `studio_content_hash()` ohne szenenbezogene Eingaben — ein neu gestrichenes
      Studio darf niemals wie ein Grund aussehen, einen HeyGen-Clip neu zu kaufen.
- [x] Alpha-Compositor `composite_studio_scene()`: transparentes WebM über
      Studiohintergrund oder -loop, Themenmedium in den Monitor (Maskierung,
      optional Perspektivtransformation), Avatar-Skalierung und -Positionierung,
      optionaler Schatten, optionaler Desk-Layer, Ausgabe 1920×1080, 25 fps,
      H.264/yuv420p.
- [x] Alphaprüfung vor dem Rendern: `probe_has_alpha()` akzeptiert ein
      Alpha-Pixelformat **oder** das Stream-Tag `alpha_mode=1` — echtes
      HeyGen-VP9-WebM trägt Alpha außerhalb des Pixelformats, eine reine
      pix_fmt-Prüfung würde jede echte Lieferung ablehnen. Ein opakes Video wird
      im Alpha-Modus abgelehnt, nie stillschweigend übernommen.
- [x] Avatar-Tonspur wird verworfen: nur die Narration wird `-map`ped; die
      Ausgabeprüfung besteht auf genau einer Audiospur.
- [x] Dauer exakt an der TTS-Dauer: `-t <Narrationsdauer>`, kein `-shortest`;
      ein zu kurzer Avatarclip wird per `tpad=stop_mode=clone` gehalten, damit
      das letzte Wort nicht abgeschnitten wird.
- [x] Atomische Ausgabe über `.<stem>.part<suffix>` — der echte Suffix muss
      bleiben, weil ffmpeg den Muxer daraus wählt; der führende Punkt hält die
      Teildatei aus Medien-Globs heraus. Beschädigte, leere oder in Auflösung
      beziehungsweise Dauer abweichende Ergebnisse werden verworfen.
- [x] Keine Shell-Injection: `subprocess.run` bekommt immer eine Liste, nie
      `shell=True`; in den Filtergraph gelangen ausschließlich Zahlen, jede über
      `_num()`, das nicht-endliche Werte ablehnt. Pfade erscheinen nur als
      `-i`-Argumente.
- [x] Perspektivtransformation nur, wenn das lokale ffmpeg den Filter kennt
      (`has_perspective_filter()`); sonst Rechteck-Overlay. `used_perspective`
      protokolliert, was tatsächlich passiert ist.
- [x] Dynamischer Studiomonitor ohne zweite Mediengenerierung:
      `resolve_scene_media()` nimmt zuerst `scene.background_asset` (der Planer
      setzt es bereits aus dem Image-Manifest), dann das Kapitelmedium aus
      Video-, Wetter- und Image-Manifest, dann die neutrale Fallbackgrafik des
      Studios. Fehlt auch diese, ist es ein `DisplayMediaUnavailableError` und
      das Studio gilt als nicht bereit.
- [x] Dasselbe redaktionelle Medium in beiden Darstellungsarten:
      `ResolvedMedia.for_monitor()` und `.for_fullscreen()` liefern dieselbe
      Datei mit unterschiedlicher Rahmung (`cover`/`contain`/Fokuspunkt).
- [x] `composite_content_hash()` trennt Studio, Szenenplan, Themenmedium,
      Avatarclip, Look, Audio, Compositingparameter und Rendererversion, damit
      die Invalidierungsregeln gelten: Studioänderung invalidiert Compositing
      und Render, nicht HeyGen; Themenmediumänderung nur die Szene und den
      Render; reine Overlayänderung nur den Render; eine Reporteränderung
      erzeugt gar keinen HeyGen-Auftrag.
- [x] `tests/test_studio_manifest.py` (57), `tests/test_studio_compositor.py`
      (42, mit synthetischen Fixtures und echtem ffmpeg), `tests/test_studio_media.py`
      (31). Kein Provideraufruf, keine externen Kosten.

Bewusst **nicht** in WP-3 vorgezogen (gehört zu WP-4): die Renderer- und
Remote-Render-Integration auf Szenenebene. Der Compositor ist als isolierter,
für sich testbarer Dienst gebaut; ihn und den Renderer im selben Paket
umzustellen hätte zwei große Änderungen ununterscheidbar vermischt.

**Betriebshinweis:** Das ffmpeg dieser Maschine (7.1.4) kann Alpha-VP9 zwar
dekodieren, aber nicht *enkodieren* — `-pix_fmt yuva420p` fällt still auf
`yuv420p` zurück. Für Alpha-Fixtures nutzen die Tests deshalb `qtrle/argb` in
einer MOV-Datei. Für die Produktion ist das unkritisch, weil Alpha von HeyGen
kommt und nur gelesen wird.

### WP-4 — Renderer auf den Scene-Plan umstellen *(nächstes Paket)*

- [ ] `render_video()` schneidet nach `scene_plan.json` statt nach eigener
      Blockaufteilung; Original-TTS bleibt die Audioquelle (Anforderung 9).
- [ ] Anchor-Szenen → `composite_studio_scene()`; Reporter-Szenen → bestehender
      Vollbild-/B-Roll-Pfad; Wetter → bestehender Wetterrenderer;
      Opening/Closing → Studiotemplates.
- [ ] Kapitel-, Overlay-, Timeline-, Untertitel- und YouTube-Kapitelmechanismen
      unverändert erhalten; der Kapitelpfad ohne `scene_plan.json` bleibt, wie
      er ist, ebenso D-ID und `bitcoin_podcast`.
- [ ] `segments: []` im Anchor-Manifest erst ablösen, wenn der neue Renderer
      einen dokumentierten Vertrag hat.
- [ ] `render_settings`-Hash um Plan-, Studio- und Compositing-Hash erweitern.
- [ ] Remote-Render-Paket: Plan, Studio-Assets und Szenenartefakte übertragen
      oder deterministisch referenzieren, ohne große Medien zu duplizieren.

### WP-5 — Betriebswerkzeuge und Gates

- [ ] `btcedu anchor-readiness` (im Profil bereits erwähnt, existiert nicht):
      lehnt Platzhalter-Look-IDs, doppelte IDs, fehlende Rechte und
      nicht-positive Kostenrate ab.
- [ ] Dashboard-Vorschau und Review-Gate für Avatar-Szenen.
- [ ] Fail-closed-Verhalten beziehungsweise sichtbarer Voice-over-Fallback.

### Extern blockiert

- [!] HeyGen-PAYG-Zugang, echte Look-IDs, Studio-Artwork, Rechteentscheidung,
      ALMANYA24-Testkanal. Details in `almanya24-media-roadmap.md`.

## Konsistenzprüfung der geänderten Dateien

Durchgeführt am 6. September 2026:

- Volle Suite im Arbeitsverzeichnis nach WP-1: **2865 passed, 2 failed**.
- Volle Suite im Arbeitsverzeichnis nach WP-2: **2886 passed, 3 failed**
  (2 vorbestehende Fehlschläge + 1 bekannter Timing-Flake, siehe unten).
- Volle Suite im Arbeitsverzeichnis nach WP-3: **3017 passed, 2 failed** —
  ausschließlich die beiden unten dokumentierten Baselineprobleme.
- Volle Suite gegen einen sauberen `HEAD`-Worktree (nur zum Vergleich angelegt
  und wieder entfernt, das Arbeitsverzeichnis blieb unangetastet):
  **2788 passed, 1 failed**.
- `.venv/bin/ruff check btcedu/ tests/` → All checks passed.

### Vorbestehende Baselineprobleme (getrennt dokumentiert, nicht abgeschwächt)

Keiner dieser Tests wurde angefasst, entschärft oder übersprungen.

1. `tests/test_cross_profile.py::test_tts_profile_config_values`
   Der Test erwartet `style > 0`; das committete Profil steht auf `style: 0.0`.
   Schlägt auf einem sauberen `HEAD`-Worktree genauso fehl. Entweder ist der
   Profilwert falsch oder die Erwartung veraltet — das ist eine TTS-Frage und
   gehört in ein eigenes Ticket, nicht in die Avatar-Arbeit.
2. `tests/test_speech_normalize.py::TestTheLastTenEpisodes::test_no_markers_or_orphaned_apostrophes_are_left_behind`
   Liest echte gerenderte Episoden aus `data/outputs` dieser Maschine und ist
   damit maschinenabhängig; im sauberen Worktree wird er übersprungen. Kein
   Codepfad dieser Arbeit berührt die Narration.

### Bekannter Flake (keine Regression)

- `tests/test_web.py::TestJobsAndLogs::test_run_all_nothing_to_do_on_published`
  fällt gelegentlich im Gesamtlauf aus und ist einzeln reproduzierbar grün
  (`tests/test_web.py` allein: 77 passed). Ein Timing-Effekt des
  JobManager-Threads.

### Weitere Prüfungen

- `sceneplan` ist in allen gefundenen Stage-Registern eingetragen
  (`_V2_STAGES`, `_STATUS_ORDER`, `_V2_ONLY_STAGES`, `_STAGE_NAME_TO_PIPELINE_STAGE`,
  `_RESUMABLE_EPISODE_STATUSES`, `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`,
  `_ALLOWED_STAGE_ACTIONS`, JobManager-Dispatch und Reset-Matrix).
- `SCENE_PLANNED` ist in jeder Statusprüfung ergänzt, die vorher `TTS_DONE`
  akzeptierte (`renderer`, `remote_render`, `anchor_generator`, CLI, JobManager).
- Kein Codepfad ruft HeyGen ohne `anchor_enabled` **und** Provider `heygen` auf.
- Die bestehenden 120 Tests aus `test_anchor_generator.py`,
  `test_anchor_service.py`, `test_scene_planner.py` und `test_avatar_jobs.py`
  bleiben grün; der Kapitelpfad ist unverändert.
- Die vier `report.*.json` sind Absturzartefakte und werden von `.gitignore`
  ausgeschlossen.
- Commits liegen ausschließlich lokal; es wurde nichts gepusht.

## Arbeitsprotokoll

| Datum | Paket | Ergebnis |
|---|---|---|
| 2026-09-06 | Recovery | Stand rekonstruiert, Checkliste angelegt, Bestandstests grün |
| 2026-09-06 | Konsistenz | 12 durch `sceneplan` veraltete Bestandstests nachgezogen |
| 2026-09-06 | WP-1 | Avatar-Job-Register, Migration 016, 35 Tests, Ruff grün |
| 2026-09-06 | Checkpoint | Lokaler Commit `0ec0d5b` (Sceneplan + Register), nicht gepusht |
| 2026-09-06 | WP-2 | Anchor-Stage auf Scene-Plan und Register, 22 Tests, Ruff grün |
| 2026-09-06 | Checkpoint | Lokaler Commit `684d639` (WP-2), nicht gepusht |
| 2026-09-06 | WP-3 | Studio-Manifest, Compositor, Medienauflösung, 130 Tests, Ruff grün; Suite 3017 grün / 2 Baselinefehler |
