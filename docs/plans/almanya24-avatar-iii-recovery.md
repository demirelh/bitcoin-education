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
- [x] **Renderer-Statuskette**: `renderer.py` und `remote_render.py` konsumieren
  `scene_plan.json` jetzt szenenweise (WP-4). Ohne Plan bleibt der Kapitelpfad
  unverändert.
- [~] **Anchor-Stage**: Der Scene-Pfad ist umgesetzt (WP-2). Offen bleiben
  Parallelität/Backoff und das Operator-Kommando `btcedu avatar-reconcile`.
- [~] **Kostenprüfung Avatar**: Der Scene-Pfad prüft szenenweise gegen
  `max_cost_usd` und `episode_avatar_cost()`. Der Kapitelpfad kennt weiterhin
  nur Kapitel — bewusst unverändert, damit D-ID nicht betroffen ist.
- [~] **WebM**: Der Compositor legt Alpha-WebM über das Studio (WP-3) und der
  Renderer ruft ihn szenenweise auf (WP-4). Offen bleibt allein das echte
  Studio-Artwork aus Phase 1 — bis dahin ist die Bereitschaftsprüfung bewusst
  rot. Ungeprüft ist außerdem, ob das ffmpeg des Pi Alpha aus einem echten
  HeyGen-VP9-WebM *dekodieren* kann; das ist eine Betriebsfrage, kein Blocker.

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

### WP-4 — Renderer und Remote-Render auf Szenenebene (6. September 2026)

Neu: `btcedu/core/scene_renderer.py`. Geändert: `btcedu/core/renderer.py`,
`btcedu/core/remote_render.py`, `scripts/render_job.py`.

**Timingvertrag** (im Modul-Docstring von `scene_renderer.py` dokumentiert):

1. Die Kapitel-MP3 ist die alleinige Autorität für die Kapitellänge.
2. Szenengewichte stammen aus den gemessenen `speaker_parts`; geplante Dauern
   sind nur der Rückfall für ältere Episoden.
3. Die Gewichte werden auf die gemessene Kapitellänge skaliert; die
   Rundungsdifferenz landet deterministisch **ausschließlich** auf der letzten
   Szene.
4. Sprecherpausen stecken bereits in der Kapitel-MP3 und werden nie erneut
   eingefügt — geschnitten wird nur das Bild.
5. Szenen werden **stumm** gerendert, verbunden, danach legt
   `replace_audio_track()` die unveränderte Kapitel-MP3 als einzige Tonspur
   auf. Ein stummes Zwischenprodukt kann kein HeyGen-Audio in den Master lassen.
6. Die letzte Szene erhält `SCENE_TAIL_HEADROOM_SECONDS = 0.15`, damit kein
   letztes Wort abgeschnitten wird.
7. `_verify_chapter_output()` prüft per ffprobe die Dauer gegen
   `DURATION_TOLERANCE_SECONDS = 0.35` und genau eine Audiospur.

- [x] `render_video()` schneidet nach `scene_plan.json`; der `use_scenes`-Zweig
      steht vor `use_beats`. Ohne Plan ist der Kapitelpfad Byte-für-Byte
      derselbe, weil `scene_hash_inputs()` ohne Plan `None` liefert und der
      Schlüssel `"scenes"` dann gar nicht erst in den Content-Hash kommt.
- [x] Enthält ein gespeicherter Plan Studio-Szenen, während
      `anchor_enabled=false` ist, ignoriert der Renderer den Plan einschließlich
      seines Hash-Beitrags und verwendet den bestehenden Kapitelpfad. Dadurch
      benötigen deaktivierte Anchor weder Studio-Manifest noch Avatar-Clips;
      reine Reporter-Pläne bleiben weiterhin nutzbar.
- [x] Anchor-Szenen → `composite_studio_scene()` in zwei Durchgängen: erst das
      Studio (Alpha, Monitor, Desk), dann der bestehende `create_video_segment()`
      für Overlays, Ticker und Fades. Das verhindert eine zweite Renderpipeline.
- [x] Reporter-Szenen → bestehender Vollbild-/B-Roll-Pfad (`create_segment()`
      beziehungsweise `create_video_segment()`), kein Avatar, kein Studio, keine
      zusätzliche Mediengenerierung.
- [x] Wetterkapitel werden über `_weather_chapter_ids(image_manifest)` vom
      Szenenpfad ausgenommen; der deterministische Wetterrenderer bleibt
      unberührt. `studio_weather_handover` ist weiterhin eine Anchor-Szene
      *innerhalb* eines Nachrichtenkapitels und wird normal komponiert.
- [x] Template → Platte: `studio_opening_wide` → `intro_asset`;
      `studio_closing` und `studio_weather_handover` → `loop_asset`;
      `studio_anchor_medium`/`studio_anchor_return` → Standardhintergrund.
- [x] Fail-closed in `resolve_avatar_clip()`: fehlender Manifesteintrag, Status
      in `{failed, reserved, reconcile_required, ""}`, abweichende Look-ID,
      Pfad außerhalb des Episodenverzeichnisses, fehlende Datei. Der Renderer
      beauftragt **niemals** einen Clip.
- [x] Wiederverwendung pro Szene über eine `<shot>.hash`-Datei neben
      `<shot>.mp4`; verlangt wird passender Hash **und** erfolgreiches
      `probe_media`, eine beschädigte Szene wird einzeln neu gerendert.
- [x] Kapitel-, Overlay-, Timeline-, Untertitel- und YouTube-Kapitelmarken
      unverändert: pro Kapitel entsteht weiterhin genau ein Segment und genau
      eine Kapitelmarke — Sprecherwechsel erzeugen keine neuen Marken.
- [x] `render_settings`-Hash um Plan-, Anchor-, Studio-, Themenmedium- und
      Compositing-Hash erweitert. Beide Aufrufstellen benutzen denselben Helfer
      `_scene_hash_block()`, damit Pi und GitHub-Runner nicht auseinanderlaufen.
- [x] Remote-Render: Studio-Assets werden deklariert und wie Profilaudio unter
      `assets/<rel>` mitgeschickt; `scene_job_requirements()` und
      `verify_job_completeness()` prüfen vor dem Rendern auf Vollständigkeit.
      Secrets und Symlinks werden gefiltert (`is_secret_name()`), unreferenzierte
      Assets nicht mitgesendet. Ohne Szenenplan ist das Paket unverändert.
- [ ] `segments: []` im Anchor-Manifest bleibt bewusst leer; der Vertrag ist
      jetzt dokumentiert, die Ablösung gehört aber nicht mehr in dieses Paket.

Tests: `tests/test_scene_renderer.py` (56), `tests/test_scene_remote_render.py`
(27), `tests/test_renderer_scene_integration.py` (13). Die Bestandssuiten
`test_renderer.py`, `test_remote_render.py`, `test_render_job_script.py` und
`test_render_subtitles.py` laufen unverändert (102 Tests).

Fixture-Hinweis: `speaker_parts` benutzen den Schlüssel `"file"` mit bloßem
Dateinamen unter `tts/parts/`, nicht `file_path`. `OverlaySpec` kennt kein
`duration`-Argument. Beides hat beim Schreiben der Fixtures Zeit gekostet.

### WP-5A — Readiness, Rechte-Gate und Avatar-Reconciliation (6. September 2026)

Bedienbar gemacht, was bisher nur im Code galt: die Frage *darf und kann diese
Episode überhaupt moderiert werden*, und der Weg, wie ein Mensch einen Job
schließt, dessen Ausgang die Pipeline sich bewusst weigert zu raten.

**`btcedu anchor-readiness --profile tagesschau_tr`** (`core/anchor_readiness.py`)

- [x] Standardlauf vollständig offline und kostenfrei; 35 Checks in den
      Bereichen `configuration`, `studio`, `ffmpeg`, `pipeline`, `ledger`,
      `rights`.
- [x] Konfiguration: Provider `heygen`, Engine exakt `avatar_iii`, positive
      Kostenrate, Anchor-Limit 7 USD und nicht größer als das Episodenlimit,
      Look-Pool mit eindeutigen, aktiven, nicht-Platzhalter-IDs, kein Rückfall
      auf eine erfundene globale Avatar-ID.
- [x] Studio: Manifest vorhanden und gültig, alle Assets vorhanden, lesbar und
      innerhalb des Asset-Verzeichnisses (Symlinkschutz), Auflösung/FPS passend
      zum Renderer, Display-Zone und Safe Areas gültig, im opaken Modus eine
      presenterfreie Zone oder eine Occlusion-Maske.
- [x] FFmpeg: Binaries vorhanden, benötigte Filter je Modus verfügbar, im
      Alpha-Modus Alpha-**Dekodierung**. Bewusst keine Encoding-Anforderung
      erfunden: produktiv liefert HeyGen das WebM, das Ergebnis ist H.264.
- [x] Pipeline: `sceneplan` vor `anchorgen` vor `render`, Presenter-Zuweisung
      konfigurierbar, Job-Tabelle und Migration vorhanden, privates Testziel,
      `auto_publish=false`, Remote-Render kann die Studioassets übertragen.
- [x] Exitcodes 0/1/2/3, abgeleitet statt gepflegt. Ein falsch geschriebenes
      `--profile` ist kein Aufruffehler, sondern ein blockierender Befund mit
      Remedy — Skripte bekommen so in beiden Fällen dieselbe Reportform.
- [x] `--json` mit versioniertem Schema; jeder blockierende Befund trägt eine
      `remedy`, und ein Test erzwingt das. Keine Secrets, keine vollständigen
      API-Schlüssel, keine absoluten Deployment-Pfade in beiden Ausgaben.
- [x] Der reale Lauf gegen `tagesschau_tr` ist heute **30 pass, 1 warning,
      4 blocked, Exitcode 2** — die ehrliche Antwort, solange Phase 1 fehlt.

**Rechte- und Transparenzvertrag** (`core/anchor_rights.py`)

- [x] Maschinenlesbarer Freigabesatz mit Zustimmung, Voice-/Likeness- und
      Synthesebestätigung, Kanälen, Gebieten, Gültigkeit, Widerruf, interner
      Vertragsreferenz, Betreiberfreigabe und KI-Kennzeichnungstext.
- [x] Datensparsamkeit ist *mechanisch* erzwungen, nicht nur dokumentiert:
      `parse_rights_record()` weist jeden Schlüssel außerhalb der Allow-List
      zurück, und `presenter_rights_id`/`approved_by_ref` müssen einem Muster
      genügen, das ein Name oder eine E-Mail-Adresse nicht erfüllen kann.
- [x] Produktiver Satz liegt außerhalb des Repos und ist gitignored; committet
      ist nur `assets/almanya24/rights/anchor-rights.example.json` samt README.
- [x] Fail-closed bei fehlender Zustimmung, Widerruf, Ablauf, nicht abgedecktem
      Kanal oder Gebiet, fehlender Betreiberfreigabe und fehlender
      KI-Kennzeichnung. Alle Probleme werden gesammelt gemeldet, nicht das
      erste — wer eine Freigabe nachzieht, will die ganze Liste.

**Optionaler Online-Check** (`services/heygen_readonly.py`)

- [x] Nur GET: Authentifizierung und Look-Abfrage. Die Klasse besitzt keine
      Methode, die etwas erzeugen oder hochladen könnte; ein Test prüft das.
- [x] Das Versprechen wird vor der ersten Anfrage gedruckt, nicht nur
      dokumentiert.
- [x] Fehlerarten getrennt: 401/403 blockieren (das ist eine Antwort),
      Netzwerkfehler und Timeout warnen nur — ein nicht erreichbarer Provider
      sagt nichts über die Konfiguration, und eine schlechte Minute Netz darf
      das Offline-Urteil nicht vergiften. 429 respektiert `Retry-After`.
- [x] Ein **fehlendes** `supported_api_engines` bleibt *unbekannt*, nicht leer.
      Schweigen als "unterstützt kein Avatar III" zu lesen, würde einen
      funktionierenden Look an einem Feld scheitern lassen, das der Provider nie
      zugesagt hat.

**`btcedu avatar-reconcile`** (`core/avatar_reconcile.py`, Migration 017)

- [x] `list`, `inspect`, `attach`, `resolve`; Text- und JSON-Ausgabe, keine
      Secrets, keine absoluten Pfade.
- [x] Fünf modellierte Betreiberentscheidungen: `running`, `delivered`,
      `not-billed`, `unresolved`, `abandon`. Jede verlangt Begründung und
      Operatorreferenz und schreibt eine Auditzeile mit Status und Kosten davor
      und danach.
- [x] **Ein 404 ist kein Beweis, dass nichts berechnet wurde.** HeyGen hält
      fertige Videos nur begrenzt vor; ein fehlender Job nach Wochen beweist
      allein den Ablauf der Aufbewahrungsfrist. `not-billed` wird ohne gebundene
      Provider-Job-ID verweigert — sicher sind `unresolved` oder `abandon`.
- [x] `abandon` nullt die Kosten **nicht**. Ein aufgegebener Clip ist keine
      Behauptung, er sei gratis gewesen.
- [x] Keine automatische Neubeauftragung: `reserve_scene()` liefert für
      `reserved`, `reconcile_required` und den neuen Status `ABANDONED`
      `ACTION_RECONCILE`.
- [x] Erledigte Jobs werden nie wieder geöffnet, eine Aufgabe ist endgültig.
- [x] `attach` ist ein eigener Schritt und verlangt `--confirm`: eine von Hand
      gefundene Job-ID zu binden ist eine Behauptung, die jede spätere
      Entscheidung erbt.
- [x] Konkurrierende Auflösung ist ein einziges bedingtes UPDATE mit
      Rowcount-Prüfung; der Verlierer wird informiert, nicht überschrieben.

**Ausfallpolitik** (`core/anchor_fallback.py`)

- [x] Bei aktivem Avatarbetrieb stoppt ein fehlender, ungültiger oder
      ungeklärter Moderatorinnenclip die Episode. Kein stiller Rückfall auf den
      alten Vollbild-Voice-over: eine Episode, die die Moderatorin heimlich
      weglässt, rendert, besteht die technischen Prüfungen und veröffentlicht in
      einem Format, das der Kanal aufgegeben hat.
- [x] Der Voice-over-Fallback existiert nur als ausdrückliche Betreiber-
      entscheidung für **genau eine** Episode, mit Grund, Auditeintrag und
      erzwungener neuer Endabnahme (`requires_final_review` ist immer wahr).
- [x] Der Renderer wählt nie: `require_anchor_usable()` ist Betreiberwerkzeug.
      Ein zweites, breiteres Veto im Renderer würde so aussehen, als entschiede
      er über die Darstellung.
- [x] Der Anchor-disabled-Pfad anderer Profile bleibt unberührt.

**Tests**: `tests/test_anchor_readiness.py` (59), `test_anchor_readiness_online.py`
(21), `test_anchor_rights.py` (37), `test_avatar_reconcile.py` (42),
`test_anchor_fallback.py` (22) — 181 Tests, kein einziger Provideraufruf.

**Dokumentation**: `docs/anchor-readiness.md` (CLI-Hilfe, Bereichstabelle,
Exitcodes, JSON-Beispiel, Entscheidungstabelle und die Regeln, die Geld kosten).

### WP-5B — Dashboard, Vorschau und Avatar-Review-Gate (6. September 2026)

Der Betreiber kann die Moderatorinnenclips jetzt *vor* dem teuren Render sehen
und freigeben — und zwar gebunden an genau die Artefakte, die er gesehen hat.

**Pipeline-Gate** (`core/anchor_review.py`, `core/pipeline.py`)

- [x] Neue Stage `review_gate_anchor` zwischen `anchorgen` und `render`, in
      `_V2_STAGES` und `_V2_ONLY_STAGES`; Stagezahl 21 → 22.
- [x] `gate_applies(config, settings)` — aktiv nur bei `anchor_enabled` **und**
      `anchor.review_required`. Kein Gate für `bitcoin_podcast`, für den
      D-ID-Pfad, ohne Szenenplan oder ohne Anchor-Szenen.
- [x] `auto_approve_reviews` überspringt dieses Gate bewusst **nicht**; die
      Freigabe hängt an der eigenen Profiloption `anchor.review_required`
      (ALMANYA24: `true`).
- [x] Ablehnung stoppt fail-closed; `renderer._require_anchor_approval()`
      schließt zusätzlich den direkten `btcedu render`-Aufruf.
- [x] `review_gate_3` bleibt unverändert zusätzlich erforderlich.

**Artefaktbindung über ein Digest-Sidecar** (`anchor/review_digest.json`)

`reviewer.py` bindet Freigaben an Datei-*Bytes*; ein Job, der nach
`reconcile_required` wandert, ändert aber nur eine DB-Zeile. Deshalb schreibt
`collect_state()` ein Sidecar, das genau das enthält, worüber entschieden wird:
geplante Anchor-Szenen, Presenter-Zuweisung, Manifestidentität, Clip-Hashes,
TTS-Speaker-Part-Hashes, Provider/Engine, **Ledger-Jobstatus** und **offene
Regenerationsrevisionen**.

- [x] Nur Anchor-Szenen fließen ein, nicht der gesamte Sceneplan-Hash: ein
      getauschtes Reporterbild darf eine Freigabe nicht entwerten, die nie über
      den Reporter getroffen wurde.
- [x] `build_digest()` löst die Konfiguration selbst auf (`_config_for`). Ein
      optionales `config`-Argument hatte denselben Zustand zweimal verschieden
      gehasht und Freigaben sofort veralten lassen — der teuerste Fehler dieses
      Pakets.
- [x] Das Sidecar wird aus `collect_state()` geschrieben (und nur bei
      Byteänderung), damit nie gegen einen veralteten Stand entschieden wird.
- [x] `collect_state()` kehrt früh zurück, wenn das Gate nicht gilt — eine
      Installation ohne Avatar braucht die Tabellen gar nicht erst.
- [x] Look-, Audio-, Sceneplan-, Jobstatus- oder Clipänderung macht die Freigabe
      automatisch ungültig; eine alte Freigabe wandert nie auf neue Clips.

**Bewusste Neugenerierung** (`models/avatar_regeneration.py`,
`core/avatar_regeneration.py`, Migration 018)

- [x] Eigenes `AvatarRegenerationRequest`-Modell statt einer Spalte am Job: der
      Vorgang hat einen eigenen Lebenszyklus, eigene Audit-Referenzen und darf
      den abgerechneten Altjob nicht überschreiben. Begründung im Docstring.
- [x] `prepare()` liefert ein Kostenangebot (bisherige Kosten, geschätzte
      Zusatzkosten, Grund) und **kauft nichts**; erst `confirm()` mit der
      angezeigten Revision gibt frei. Getrennte Felder `requested_by_ref` und
      `confirmed_by_ref`.
- [x] Stabiler Idempotency-Key je Attempt, Unique Constraints auf
      `(episode_id, scene_id, revision)` und den Key: Doppelklick oder
      HTTP-Retry erzeugt genau einen Attempt.
- [x] `generation_revision` fließt in den **Content-Hash**, nicht in den Unique
      Constraint — der neue Versuch *ist* nach der Regel des Ledgers andere
      Arbeit. Revision 0 wird aus der Payload ausgelassen, alle bestehenden
      Hashes bleiben byteidentisch.
- [x] „Retry" (Transport/Polling) und „Regenerate" (neuer bezahlter Clip)
      bleiben begrifflich und technisch getrennt.
- [x] Alter Job und Clip bleiben erhalten; Avatarfreigabe und Render werden
      stale, andere Szenen, TTS und Reportermedien nicht.

**Dashboard-API** (`web/api.py`, bestehende Konventionen)

- [x] `GET /api/episodes/<id>/avatar` — Readiness-Zusammenfassung, Provider,
      Engine, Avatar-Typ, Studio-Modus, Look-Name und gekürzte Look-ID,
      Presenter-/Sceneplan-/Manifest-/Review-Hash, Reviewstatus, erwartete,
      fertige und blockierte Szenen, reservierte, tatsächliche und geschätzte
      Restkosten sowie die Szenenliste. Alle Bezeichner über `_short()` gekürzt,
      keine Schlüssel, keine vertraulichen Vertragsdaten.
- [x] `POST .../avatar/approve`, `/reject`, `/scenes/<id>/flag`,
      `/scenes/<id>/regenerate`, `/look`; 409 bei veraltetem Review-Hash oder
      konkurrierender Änderung, kein Provideraufruf in der HTTP-Anfrage.
- [x] Lookwechsel vor dem ersten Job frei, danach nur als ausdrücklich
      bestätigter Neuaufbau; `NoActiveLookError` wird 400, nicht 500.
- [x] `GET .../avatar/scenes/<id>/preview` — nur registrierte, validierte Clips
      dieser Episode, Pfadeinhausung und Symlinkschutz, MP4 und WebM,
      Range-Unterstützung über `send_file(..., conditional=True)`, keine
      Provider-URL an den Browser.

**Oberfläche** (`web/static/app.js`, Bereich „Sendung")

- [x] Readiness (bereit/Warnung/Blocker mit kurzer Empfehlung), Presenterblock,
      Szenentabelle mit Vorschau, Beanstandung und bestätigungspflichtiger
      Regeneration, Freigabe/Ablehnung mit Notiz.
- [x] Rohe Vorschau eindeutig als „Avatarclip – Studio wird im finalen Render
      ergänzt" gekennzeichnet, mit sichtbarem Hinweis auf die danach noch
      folgende finale Sendungsfreigabe.
- [x] Korrekte Zustände ohne Phase-1-Assets, bei deaktiviertem Anchor, bei
      teilweise fertiger Episode und bei Reconciliation.

**Tests**: `tests/test_anchor_review.py` (35), `test_avatar_regeneration.py`
(21), `test_web_avatar.py` (33), `test_anchor_review_gate.py` (15) — 104 neue
Tests, kein Provideraufruf.

**Dokumentation**: `docs/avatar-review.md` (Gate, Digest-Bindung, API, Vorschau,
Regeneration, Lookwechsel, Audit).

### WP-5C — Begrenzte Parallelität, Backoff und Betriebshärtung (6. September 2026)

**Parallelität**

- [x] Profilgesteuert: `anchor.max_concurrent_jobs`, `poll_interval_seconds`,
      `request_timeout_seconds`, `poll_timeout_seconds`.
- [x] Strikte Validierung: Minimum 1, hartes Maximum
      `HEYGEN_MAX_CONCURRENT_JOBS = 5`. Bewusst konservativ gesenkt (vorher 10),
      weil für dieses Konto **kein** Providerlimit bestätigt ist. ALMANYA24
      steht auf 3.
- [x] `AvatarCoordinator` in drei Phasen: planen (Hauptthread, mit Session),
      ausführen (Threadpool, **ohne** Session), persistieren (Hauptthread).
      Worker erhalten reine Dataclasses und können die Datenbank nicht
      erreichen; Rückmeldung über `queue.Queue`.
- [x] Keine SQLAlchemy-Session zwischen Threads, keine offene Schreibtransaktion
      während eines Netzwerkaufrufs.
- [x] Bereits eingereichte Jobs belegen einen Slot (Resume zählt mit).
- [x] Budget: Reservierung beim Planen, erneute Prüfung unmittelbar vor jedem
      einzelnen Submit.
- [x] Reporter-, Wetter- und Nicht-Anchor-Szenen werden gar nicht erst geplant
      (unverändert aus WP-2).

**Audioassets**

- [x] Neues Modell `AvatarAudioAsset`, Identität `(provider, audio_hash)` —
      ausdrücklich kein Pfad und keine Provider-URL.
- [x] Unverändertes Audio wird wiederverwendet, auch szenen- und laufübergreifend.
- [x] Abgelaufene Assets werden **markiert**, nicht gelöscht.
- [x] Ein erneuter Audio-Upload bedeutet nie einen neuen Videoauftrag.

**Retry-Matrix** (`btcedu/core/avatar_retry.py`)

- [x] `retryable` und `ambiguous` sind getrennte Fragen; nur `ambiguous`
      entscheidet über Geld.
- [x] 401/403 permanent, 400/422 nachweislich unbezahlt, 404 kontextabhängig,
      429 mit `Retry-After`, 5xx je nach Methode, Netzwerkabbruch nach
      Sendezeitpunkt unterschieden.
- [x] `Retry-After` als Sekundenwert **und** als HTTP-Datum.
- [x] Exponentielles Backoff mit Jitter, Obergrenze, keine Endlosschleife.
- [x] Injizierbare `sleep`/`now`/`rand` — Tests warten nie wirklich.
- [x] Keine Secrets in Logs (Fehlerklasse statt Payload).

**Idempotency-Key**

- [x] `sha256(episode:scene:content_hash)`, stabil pro Generation-Attempt,
      vor dem Aufruf persistiert, Ablaufzeit 24 h gespeichert.
- [x] Jeder Retry — auch nach Prozessneustart — nutzt denselben Key.
- [x] Eine bewusste Regeneration ändert `content_hash` und damit den Key.
- [x] Nach Ablauf des Fensters keine automatische POST-Wiederholung, sondern
      `reconcile_required`.

**Polling, Download, Validierung**

- [x] Mehrere Jobs abwechselnd gepollt, kein Busy-Wait; Status persistiert,
      nach Neustart fortsetzbar.
- [x] Unbekannter Providerstatus ist fail-closed.
- [x] Poll-Timeout vertagt (`deferred`), Zeile bleibt `submitted`, kein Neukauf.
- [x] Streamingdownload in `.part` + `os.replace`, Größenober- und -untergrenze,
      SHA-256 beim Schreiben.
- [x] FFprobe-Validierung: Container, Auflösung, Alpha hart; FPS und Dauer als
      Warnung.
- [x] Beschädigte Datei wird quarantänisiert, nicht gelöscht — sie wurde bezahlt.
- [x] Abgelaufene Download-URL wird über **eine** read-only Statusabfrage
      erneuert, niemals durch eine neue Generierung.

**Circuit Breaker und Betriebsschutz**

- [x] Persistenter Breaker je Provider (`AvatarProviderBreaker`), Cooldown und
      Grund sichtbar.
- [x] Offen blockiert nur neue Submits; laufende Jobs werden weiter gepollt und
      heruntergeladen.
- [x] Reine Ablehnungen (400/422) zählen nicht mit.
- [x] Reset nur manuell, mit Operatorreferenz und Notiz, als
      `AvatarAuditAction.BREAKER_RESET` auditiert.
- [x] SIGTERM zwischen sicheren Zustandsübergängen; Ledger wird vor Prozessende
      committet, keine verwaisten Threads.
- [x] Pipeline-Lock unverändert respektiert.

**Beobachtbarkeit**

- [x] `btcedu/core/avatar_runtime.py`: aktive Jobs, freie Slots, wartende
      Submits, nächster Pollzeitpunkt, Retryanzahl, letzter HTTP-Fehlertyp,
      `Retry-After`, Breaker-Zustand, reservierte / tatsächliche / **ungeklärte**
      Kosten, Validierungsstatus.
- [x] Sichtbar unter `runtime` in `GET /api/episodes/<id>/avatar` und über
      `btcedu avatar-status` sowie `btcedu avatar-breaker status|reset`.
- [x] Keine Schlüssel, Payloads oder signierten URLs.

**Fail-closed-Politik unverändert**

- [x] Kein automatischer Voice-over-Fallback; eine fehlende Szene blockiert.
- [x] Kein Provideraufruf aus Renderer oder Webrequest.
- [x] D-ID-Pfad unverändert (Ledger und Parallelität ja, granulare Matrix nein —
      bewusste Rückwärtskompatibilität).

**Migration**: 019 `AddAvatarConcurrencyStateMigration` — elf Spalten auf
`avatar_jobs`, zwei neue Tabellen, vier Indizes. Bereits abgeschlossene Zeilen
werden `validation_status='legacy'` gesetzt, nicht fälschlich `validated`.

**Tests**: `tests/test_avatar_concurrency.py`, 66 Tests.

**Gesamtsuite nach WP-5C**: 3466 grün, **genau die zwei dokumentierten
Baselinefehler**, kein dritter. Die in WP-5B nachgezogene Zählprüfung
`TestReviewGateLabels` (6 → 7 Gate-Labels wegen `review_gate_anchor`) ist
gezielt erneut geprüft und grün; sie wurde erweitert, nicht abgeschwächt.
Kein Test wurde zur Erzeugung eines grünen Ergebnisses gelockert.

**Dokumentation**: `docs/avatar-operations.md` (Konfiguration, Matrix,
Idempotenz, Polling, Breaker, Beobachtbarkeit, Störungstabelle).

### WP-6 — Vollständiger Ende-zu-Ende-Trockenlauf (7. September 2026)

**Neu**: `btcedu/core/almanya24_smoke.py` (synthetische Welt und Treiber),
`tests/test_almanya24_e2e.py` (56 Tests), CLI-Befehl
`btcedu smoke-test-almanya24 [--keep] [--dir]`, `docs/almanya24-e2e-dry-run.md`.

- [x] Deterministische, vollständig lokale Testepisode mit 11 Abschnitten:
      Opening, Schlagzeilenblock, mehrere Meldungen, `anchor → reporter`,
      `anchor → reporter → anchor`, reine Reporterfortsetzung, Wetterübergabe,
      deterministischer Wetterblock, Closing.
- [x] Synthetische Phase-1-Fixtures: Studiohintergrund, Displayzone,
      Themenbilder und -video, opaker Presenterclip, Desk-Layer, Logo,
      TTS-Parts, Kapitel-MP3s, Intro/Outro, Studio-Manifest, Rechtefixture,
      Look-Pool. Alle als `SMOKE` markiert und vom echten Readiness-Befehl
      nicht akzeptiert.
- [x] Happy Path über die echte Verdrahtung: 16 Szenen, 10 Anchorclips,
      ein Look, $0.2505 simuliert, beide Gates halten an, Render 11 Segmente
      / 43,1 s, genau ein simulierter privater Upload.
- [x] Fail-closed: fehlende Phase-1-Assets, ungültiges Studio-Manifest,
      fehlende Rechtefreigabe, Platzhalter-Look, abweichender Look, fehlender
      und beschädigter Clip, opaker Clip im Alphamodus, MP4-Fallback ohne
      sichere Displayzone, `reserved`, `reconcile_required`, Budget über 7 USD,
      fehlendes und veraltetes Anchor-Review, fehlendes Finalreview,
      `auto_publish=false`, falsches Ziel, Reporter, Rendereraufruf.
- [x] Restart/Recovery: nach Look-Zuweisung, nach Reservierung, nach
      persistierter Job-ID, nach teilweise fertigen Clips, nach Anchor-Freigabe,
      nach Render vor Finalfreigabe, nach simulierter Uploadannahme.
- [x] Wiederholbarkeit: zweiter Lauf wählt keinen Look neu, beauftragt nichts,
      erzeugt keine Kosten und keinen zweiten Upload; Content-Hashes identisch.
- [x] Remote-Render: Paketinhalt geprüft (nur referenzierte Artefakte, keine
      `.env`, keine Schlüssel, keine signierten URLs, keine Symlinks),
      Vollständigkeitsprüfung und Fail-closed bei fehlendem Asset.
- [x] Medienprüfung per FFprobe: 320×180 (bewusst verkleinerte Fixture),
      25 fps, h264, genau eine Audiospur, keine HeyGen-Tonspur, Dauer in
      Toleranz, keine Nullbyte-Datei, Presenter sichtbar, Themenmonitor nicht
      leer, Reportermedium vollflächig, Schlussframe nicht leer.
- [x] Artefakt- und Provenienzprüfung über alle 13 geforderten Objekte.

**Zwei echte Produktfehler, die erst der E2E-Lauf sichtbar gemacht hat**

1. **Wetterübergabe war unrenderbar** (behoben). Der Szenenplaner markiert die
   Wetterübergabe der Moderatorin als `needs_avatar`, Review-Gate und Renderer
   verlangen dafür einen Clip — `_billable_units` schloss sie aber über
   `template_id == TEMPLATE_WEATHER` aus. Jede Sendung mit Wetter war damit
   dauerhaft blockiert. Ausschluss erfolgt jetzt über
   `visual_mode == VISUAL_MODE_WEATHER`, also über die Wetterkarte selbst.
   `test_weather_handover_is_never_animated` wurde in zwei Tests aufgeteilt,
   die die korrigierte Absicht abbilden.
2. **Kein Rechte-/Studio-Preflight vor der ersten Bezahlung** (behoben).
   `anchor_generator` bestellte Clips auch ohne ladbares Studio-Manifest und
   ohne gültigen Consent-Datensatz; erst das Review-Gate verweigerte — nach der
   Abrechnung und nach der Synthese einer Likeness, für die keine Freigabe
   vorlag. `_refuse_unless_permitted()` prüft jetzt vor jedem Auftrag die
   Readiness-Bereiche `configuration`, `rights` und `studio`.

**Baselinefehler nach WP-6 (unverändert, nicht abgeschwächt)**

1. `tests/test_cross_profile.py::test_tts_profile_config_values` —
   `AssertionError: assert 0.0 > 0.0` auf `tts_cfg.get("style", 0.0) > 0.0`.
   Deterministisch, auf jedem Stand reproduzierbar, CI wäre dadurch rot.
   Ursache ist eine **absichtliche** Profiländerung: `tagesschau_tr` setzt seit
   `d1b4676` für beide Stimmen `style: 0.0` mit ausführlicher Begründung im
   Profil (bei `style: 0.3` betonten die ElevenLabs-Varianten unterschiedliche
   Wörter, was einzelne Sendungen falsch klingen ließ). Der Test bildet die
   frühere, verworfene Abstimmung ab.
   **Empfehlung**: die Zusicherung in einem eigenen, ausschließlich damit
   befassten Commit auf `style == 0.0` umstellen und den Grund im Test
   festhalten — nicht nebenbei in einem Avatar-Arbeitspaket, weil es eine
   Aussage über die Tonabstimmung ist und nicht über den Avatarpfad.
2. `tests/test_speech_normalize.py::TestTheLastTenEpisodes::test_no_markers_or_orphaned_apostrophes_are_left_behind`
   — findet ein verwaistes Apostroph in `Himalayalar'daki`. Liest die real
   gerenderten Episoden *dieser* Maschine, ist also daten- und
   maschinenabhängig; auf einem CI-Runner ohne diese Episoden läuft er nicht in
   denselben Fehler. Kein Zusammenhang mit dem Avatarpfad.
3. Bekannter Flake, keine Regression:
   `tests/test_web.py::TestJobsAndLogs::test_run_all_nothing_to_do_on_published`
   (Timing des JobManager-Threads, einzeln grün).

**Suite nach WP-6**: 3522 grün, die zwei dokumentierten Baselinefehler und der
bekannte Flake. Ruff über `btcedu/` und `tests/` grün.

**Restlücke (bewusst dokumentiert)**: Eine Anchor-Freigabe hängt am Digest, der
das Manifest hasht. Werden Clipbytes *hinter* dem Manifest her verändert, ohne
das Manifest anzufassen, bleibt die Freigabe gültig. Realistische Änderungen
laufen über eine bestätigte Regeneration und invalidieren korrekt.

**Grenze dieser Maschine**: ffmpeg kann hier kein WebM mit von ffprobe
gemeldetem Alphakanal erzeugen. Die synthetische Welt läuft daher opak
(`studio_mode: baked`, `output_format: mp4`); der transparente Pfad ist nur über
seine Verweigerungen und strukturelle Tests abgedeckt. Dass die WebM-Route Ende
zu Ende trägt, kann erst der echte Pilot zeigen.

### WP-6A — Bytegebundene Integrität von Avatarclips (7. September 2026)

**Neu**: `btcedu/core/avatar_integrity.py`, Migration `020_avatar_clip_hash`,
`tests/test_avatar_integrity.py` (14 Tests), Testklasse
`TestTheApprovedBytesAreTheRenderedBytes` in `tests/test_almanya24_e2e.py`
(14 Tests), Abschnitt 9 in `docs/avatar-operations.md`.

- [x] Kanonischer SHA-256 der tatsächlichen Clipbytes, streamingbasiert beim
      Download berechnet, in `avatar_jobs.file_sha256` und im Manifestfeld
      `file_sha256` persistiert. Größe, mtime und Pfad sind nirgends Ersatz.
- [x] Der Anchor-Review-Digest enthält den **gemessenen**, nicht den
      erinnerten Hash. Damit fällt eine Freigabe, sobald sich die Bytes ändern,
      ohne dass jemand das Manifest anfassen müsste.
- [x] Sieben Vertrauensgrenzen prüfen neu: Download, Wiederverwendung eines
      bezahlten Clips, Digestbildung, Freigabe, lokaler Render, Remote-Paket,
      Remote-Runner (`clip_digests` im Job).
- [x] Bei Abweichung: Freigabe ungültig, Render/Remote/Publish blockiert,
      Integritätsstatus mit Handlungsempfehlung, **kein** automatischer neuer
      HeyGen-Auftrag, Kosten bleiben im Ledger.
- [x] Beide Manipulationsfälle abgedeckt — Bytes allein geändert und Bytes plus
      Manifest gemeinsam geändert; im zweiten Fall ist alles intern konsistent
      und `has_current_approval` trotzdem falsch.
- [x] Pfadtraversal und Symlinks aus dem Episodenverzeichnis heraus werden als
      `unsafe` abgelehnt.
- [x] Rückwärtskompatibel: Clips ohne Hash sind `unrecorded` — weder
      vertrauenswürdig noch verurteilt; sie blockieren die Freigabe, bis der
      Digest nachgetragen und erneut freigegeben wurde. Migration 020 füllt
      Altzeilen bewusst **nicht** zurück. Der D-ID-Kapitelpfad bleibt unberührt.
- [x] Streaming ist getestet, nicht behauptet: der Test zählt die Lesegrößen
      und schlägt fehl, sobald jemand auf `read_bytes()` umstellt.

**Angepasste Bestandsfixtures** (Vertrag erweitert, keine Abschwächung):
`tests/test_anchor_review.py`, `tests/test_anchor_review_gate.py` schreiben nun
`file_sha256` zu den Clips, die sie selbst erzeugen; das Download-Double in
`tests/test_avatar_concurrency.py` meldet den Hash der Bytes, die es schreibt,
statt `"deadbeef"` — ein Double, das über den eigenen Inhalt falsch aussagt,
hätte jede Integritätsprüfung auf einer Lüge bestehen lassen.

**Verbleibendes Zeitfenster (dokumentiert, nicht geschlossen)**: Zwischen
Messung und Verwendung liegt ein kurzes TOCTOU-Fenster. Es wird klein gehalten
(atomischer Download, Neumessung an jeder Grenze statt Vertrauen auf die
vorige), nicht durch Locking beseitigt: Wer während eines Renders in das
Episodenverzeichnis schreiben kann, kann auch das fertige Video austauschen.

**Bewusst offen**: Studio-Plates und Themenmedien haben keine Bytehashes. Das
Studio ist über `studio_hash` und die Paket-Vollständigkeitsprüfung abgesichert,
Themenbilder über den Render-Content-Hash; ein getauschtes Studio-PNG bei
gleichem Manifest fällt erst im finalen Review auf.

**Weiterhin offen für WP-8**: Diese Lücke bleibt nach WP-7 unverändert
bestehen. Bytehashes der übrigen Renderinputs — Studio-Plates, Themenmedien,
Overlays — sind ausdrücklich **nicht** erledigt.

### WP-7 — Dashboard-Bedienung, Reconciliation und Voice-over-Override (7. September 2026)

**Neu**: `tests/test_web_avatar_operations.py` (54 Tests), Testklasse
`TestTheBulletinWithoutThePresenter` in `tests/test_almanya24_e2e.py` (6 Tests),
`docs/avatar-dashboard.md`.

- [x] Runtimeanzeige: Provider, Engine, Studio-Modus, Readiness,
      Circuit-Breaker-Status mit Grund, letztem Fehler, Cooldown und letztem
      Erfolg; Parallelitätsgrenze, laufende Jobs, freie Slots, wartende
      Submits, Polling/Download/Validierung/Retry/Klärungsfälle, nächster
      Pollzeitpunkt, Retryanzahl, Fehlerkategorie.
- [x] Kostenblock: gebucht, reserviert, ungeklärt, gebunden, Stufenlimit,
      Rest und Warnung vor erwarteter Überschreitung. Ungeklärte Beträge
      mindern das Restbudget — ein Job mit unbekanntem Ausgang kann berechnet
      worden sein.
- [x] Reconciliation im Dashboard bildet exakt die fünf CLI-Entscheidungen ab
      und ruft dieselbe `resolve_job()`. Der Weblayer hat **keine** eigene
      Zustandslogik; er sammelt Begründung und Digest.
- [x] Zweistufig: `prepare` verändert nichts, `confirm` ist an Status, Kosten,
      Versuchszähler **und die letzte Auditzeile** gebunden. Ohne die
      Auditbindung ließ sich `unresolved` — eine Entscheidung, die keine Spalte
      ändert — konkurrierend doppelt anwenden; genau das deckte
      `test_a_competing_decision_on_another_state_is_refused` auf.
- [x] Doppelklick liefert `already_applied` (200) statt einer zweiten
      Entscheidung, veralteter Digest 409, abgeschlossener Job 422.
- [x] Circuit-Breaker-Reset auditiert; Provider-Job-ID anhängen; optionaler
      Online-Inspect ausschließlich lesend. Kein Submit aus einem Webrequest.
- [x] Voice-over-Override als Prepare/Confirm/Revoke in
      `core/anchor_fallback.py`: Digest über Szenenplan, Anchor-Manifest
      (inklusive Bytehashes) und Jobzustand, Pflicht zu Begründung und
      Kenntnisnahme, idempotente Bestätigung, Auditzeile, Invalidierung der
      Anchor-/Renderfreigaben, `render/.stale`.
- [x] Nach bestätigtem Override lehnt `anchor_generator` jeden Submit dieser
      Episode ab. Kosten werden nicht storniert, laufende Jobs nicht
      abgebrochen, nichts neu synthetisiert.
- [x] Widerruf vor der Veröffentlichung möglich und auditiert; danach 422. Die
      Historie bleibt in jedem Fall erhalten.
- [x] Fallback-Render: `shows_presenter()` entscheidet je Szene, Anchor-Szenen
      laufen unter Override wie Reporterszenen mit dem bereits zugeordneten
      Themenmedium, TTS, Kapitelreihenfolge und Wetterpfad unverändert,
      fehlendes Fallbackasset blockiert fail-closed.
- [x] `presentation_mode` (`avatar` / `voice_over_override`) steckt im
      Szenen-Content-Hash, in `scene_hash_inputs`, im Render-Manifest und im
      Remote-Render-Job; der Runner zieht die Entscheidung in seiner
      Wegwerfdatenbank nach. Ein Moduswechsel schneidet jeden Shot neu — sonst
      würde ein alter Studio-Shot wiederverwendet und die Moderatorin doch
      gesendet.
- [x] UI-Kosmetik im vorhandenen Bereich: deutsche Statuslabels, getrennte
      Blöcke, Status mit Text **und** Farbe, gekürzte IDs, Leer-/Lade-/
      Fehlerzustände, Bestätigungsdialoge, kein Autoplay, keine automatischen
      Videodownloads, `sr-only`-Labels, mobiles Tabellenscrolling.
- [x] Zustandsändernde Avatarrouten verlangen `application/json` und lehnen
      fremde `Origin`-Header ab. Das Dashboard hat keinen Login; das ist die
      Absicherung, die zur vorhandenen Architektur passt, kein Ersatz für
      Authentifizierung.

**Zwei echte Fehler gefunden und behoben** (keine Testkosmetik):
`avatar_runtime.runtime_snapshot()` griff auf ein `AvatarJob.updated_at` zu, das
es nicht gibt — sobald zwei Jobs einen `last_error_type` trugen, wäre die
Runtimeanzeige in Produktion mit `AttributeError` abgestürzt. Und der Renderer
verlangte auch unter Override eine gültige Anchor-Freigabe, womit der
Notfallpfad genau dann unbenutzbar war, wenn die Clips kaputt sind.

**Bewusst nicht getan**: kein allgemeines Dashboard-Redesign, keine
Kostenstornierung, keine automatische Wahl des Fallbacks, keine öffentliche
Kennzeichnung erfunden — der Produktionsmodus steht in Manifest und Provenienz,
die redaktionelle Policy bleibt unverändert.

### Offene Kosmetik

- [ ] `sceneplan` im `_STAGE_WORKFLOW_KEY`-Kommentar und in `STAGE_PROVIDER_MAP`
      nachziehen.
- [x] Sichtbare Bedienung des Voice-over-Overrides — mit WP-7 erledigt.

### Extern blockiert

- [!] HeyGen-PAYG-Zugang, echte Look-IDs, Studio-Artwork, Rechteentscheidung,
      ALMANYA24-Testkanal. Details in `almanya24-media-roadmap.md`.

## Gap-Analyse nach WP-5C (6. September 2026)

Klassifikation aller Anforderungen der Recovery-Checkliste. Bewusst getrennt
nach dem, was Code leisten kann, und dem, was ohne echte Phase-1-Assets oder
ohne den Betreiber schlicht nicht abschließbar ist.

### Implementiert und automatisiert geprüft

- Scene-Plan als einzige Quelle der Anchor-Aufträge (WP-0/WP-2).
- Persistente Look-/Presenter-Zuweisung, ein Outfit pro Episode (WP-1).
- Dauerhaftes Avatar-Job-Register mit Reservierung, Resume, Reuse und
  Reconciliation (WP-1/WP-2).
- Restart-fähige Aufträge ohne Doppelabrechnung, Idempotency-Key mit
  24-Stunden-Fenster (WP-2/WP-5C).
- Studio-Manifest und FFmpeg-Compositing, Themenbild im Studiomonitor,
  Reporter unsichtbar mit vollflächigen Medien (WP-3).
- Szenenbasierter Renderer inklusive Remote-Render-Vertrag (WP-4).
- Transparentes WebM bevorzugt, opaker MP4-Fallback erhalten (WP-3/WP-4).
- Original-TTS bleibt finale Audioquelle (WP-2/WP-4).
- Readiness, Rechte-/Transparenz-Gate, Reconciliations-CLI (WP-5A).
- Artefaktgebundenes Avatar-Review-Gate, Dashboard, Vorschau, bewusste
  Regeneration mit getrenntem „Retry“ und „Regenerate“ (WP-5B).
- Begrenzte Parallelität, providerbewusste Retry-Matrix, Circuit Breaker,
  Streamingdownload mit FFprobe-Validierung, Beobachtbarkeit (WP-5C).
- Keine echten Provideraufrufe, keine externen Kosten in irgendeinem Test.
- Andere Profile und der D-ID-Pfad unverändert.

### Implementiert, aber erst mit echten Phase-1-Assets real prüfbar

- Tatsächliche Bildwirkung des Studios, des Monitorinhalts und der
  Moderatorinnen-Freistellung — bisher nur gegen synthetische Clips geprüft.
- Reale Alpha-Qualität eines HeyGen-WebM (Kantensaum, Kompression).
- Echte Auftragsdauer, damit `poll_timeout_seconds` belastbar wird.
- Reales Providerlimit für `max_concurrent_jobs`; der Deckel von 5 ist eine
  konservative Annahme, keine bestätigte Zahl.
- Reale Kosten pro Sekunde und damit die Schärfe des Budget-Preflights.
- Verhalten des echten `Retry-After`-Headers und der echten Fehlercodes.
- Lebensdauer eines hochgeladenen Audioassets beim Provider.

### Noch offen (technisch, konkret benennbar)

- **WP-6 — Ende-zu-Ende-Trockenlauf**: eine vollständige Episode von `sceneplan`
  bis `publish` gegen Fakes, mit Review-Gates und Remote-Render, als ein Test.
- **Kosmetik**: `sceneplan` im `_STAGE_WORKFLOW_KEY`-Kommentar und in
  `STAGE_PROVIDER_MAP`.
- **Bedienoberfläche für den Voice-over-Override**: Policy, Datenmodell und Gate
  stehen seit WP-5A; es fehlt der auditierte Knopf.
- **Dashboard-Anzeige der WP-5C-Laufzeitdaten**: die API liefert `runtime`
  bereits, die Oberfläche zeigt es noch nicht an.
- **Reconciliation aus dem Dashboard heraus**: bisher nur CLI.

### Bewusst nicht erforderlich

- Automatischer Voice-over-Fallback — widerspricht der Fail-closed-Politik.
- Granulare Retry-Matrix für D-ID — der Altpfad bleibt eingefroren.
- Externe Infrastruktur für den Circuit Breaker; eine Tabelle genügt.
- Wiederverwendbarer TTS-Take-Cache — unverändert nicht vorgesehen.

### Betreiberaktion

- HeyGen-PAYG-Zugang, echte Avatar-III-Look-IDs und Rechteentscheidung.
- ALMANYA24-Studio-Artwork und Testkanal.
- Entscheidung über die Rotationsregel der Outfits.
- Erste überwachte Pilotepisode mit aktivem Avatar-Review.

## Konsistenzprüfung der geänderten Dateien

Durchgeführt am 6. September 2026:

- Volle Suite im Arbeitsverzeichnis nach WP-1: **2865 passed, 2 failed**.
- Volle Suite im Arbeitsverzeichnis nach WP-2: **2886 passed, 3 failed**
  (2 vorbestehende Fehlschläge + 1 bekannter Timing-Flake, siehe unten).
- Volle Suite im Arbeitsverzeichnis nach WP-3: **3017 passed, 2 failed** —
  ausschließlich die beiden unten dokumentierten Baselineprobleme.
- Volle Suite im Arbeitsverzeichnis nach WP-5A: **3293 passed, 2 failed** —
  ausschließlich die beiden unten dokumentierten Baselineprobleme, der
  `test_web`-Flake blieb diesmal grün.
- Volle Suite im Arbeitsverzeichnis nach WP-5B: **3398 passed, 3 failed** —
  die beiden Baselineprobleme plus `test_web_review_ux.py::TestReviewGateLabels`
  `::test_all_review_stages_present`, der die Zahl der Review-Gate-Labels fest
  gegen 6 prüfte. Das neue `review_gate_anchor` macht daraus 7; der Test wurde
  um die Erwartung `"anchor" in _REVIEW_GATE_LABELS` **erweitert**, nicht
  aufgeweicht. Ebenso wurden die Stagezahl 21 → 22 und die Reihenfolgelisten in
  `test_pipeline.py` und `test_web_progress.py` nachgezogen. Nach der Korrektur
  laufen alle drei Dateien grün.
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
| 2026-09-06 | Checkpoint | Lokaler Commit `e9efd04` (WP-3), nicht gepusht |
| 2026-09-06 | WP-4 | Szenenrenderer, Remote-Vertrag, 96 neue Tests, Ruff grün; Suite 3114 grün / 2 Baselinefehler |
| 2026-09-06 | Checkpoint | Lokaler Commit `fc468bc` (WP-4), nicht gepusht |
| 2026-09-06 | WP-5A | Readiness-CLI, Rechtevertrag, Reconciliation, Ausfallpolitik, 181 Tests, Ruff grün; Suite 3293 grün / 2 Baselinefehler |
| 2026-09-06 | Checkpoint | Lokaler Commit `e1bd0bb` (WP-5A), nicht gepusht |
| 2026-09-06 | WP-5B | Avatar-Review-Gate, Digest-Bindung, Regenerationsmodell, Dashboard-API, Vorschau und Oberfläche, 104 neue Tests, Ruff grün; Suite 3398 grün / 2 Baselinefehler (Labelzähler nachgezogen) |
| 2026-09-06 | Checkpoint | Lokaler Commit `f177b88` (WP-5B), nicht gepusht |
| 2026-09-06 | WP-5C | Coordinator mit begrenzter Parallelität, Retry-Matrix, Idempotenzfenster, Audioasset-Register, Circuit Breaker, Streamingdownload mit FFprobe, Beobachtbarkeit und CLI, Migration 019, 66 neue Tests, Ruff grün; Suite 3466 grün / 2 Baselinefehler |
| 2026-09-06 | Checkpoint | Lokaler Commit `17c06d5` (WP-5C), nicht gepusht |
| 2026-09-07 | WP-6 | Synthetischer E2E-Trockenlauf, CLI `smoke-test-almanya24`, 56 neue Tests, zwei echte Produktfehler gefunden und behoben, Ruff grün |
| 2026-09-07 | Checkpoint | Lokaler Commit `dda8972` (WP-6), nicht gepusht |
| 2026-09-07 | WP-6A | Bytegebundene Clipintegrität über sieben Vertrauensgrenzen, Migration 020, 28 neue Tests, Ruff grün |
| 2026-09-07 | WP-7 | Dashboard-Runtimeanzeige, Reconciliation-Bedienung, Voice-over-Override mit Fallback-Render, 60 neue Tests, `docs/avatar-dashboard.md`, Ruff grün |
