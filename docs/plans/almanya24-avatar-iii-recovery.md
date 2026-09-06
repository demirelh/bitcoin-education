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
- [~] **Anchor-Stage**: `generate_anchors()` filtert weiterhin auf
  `visual.type == TALKING_HEAD`. Für die Dual-Presenter-Kapitel von ALMANYA24
  ist das faktisch ein No-op — der Plan ist genau die Ersetzung dafür, aber die
  Stage liest ihn noch nicht.
- [~] **Kostenprüfung Avatar**: Vor-/Nach-Prüfung gegen `max_cost_usd` existiert
  in `anchor_generator.py`, kennt aber nur Kapitel, nicht Szenen.
- [~] **WebM**: Der Service kann WebM anfordern und herunterladen; es gibt noch
  keinen Compositor, der Alpha über ein Studio legt. Aktiv nutzbar ist damit nur
  der MP4-Zweig (Anforderung 8 halb erfüllt).

## Noch offen

Grob in der Reihenfolge, in der die Pakete aufeinander aufbauen.

### WP-1 — Dauerhaftes Avatar-Job-Register — **erledigt**, siehe oben

Offen bleibt nur die Anbindung: das Register wird noch von keiner Stage benutzt.

### WP-2 — Anchor-Stage auf Scene-Plan und Job-Register umstellen *(nächstes Paket)*

- [ ] `generate_anchors()` liest `scene_plan.json` statt `TALKING_HEAD`.
- [ ] Nur `anchor_scenes()` wird beauftragt; Mehrteil-Blöcke werden vorher
      lokal zu einer Audiodatei verbunden (der Planer erfindet dafür bewusst
      keinen Pfad).
- [ ] Budget-Preflight über die geplante Anchor-Gesamtdauer, gespeist aus
      `episode_avatar_cost()` statt aus dem Kapitel-Hash.
- [ ] Jede Szene läuft durch `reserve_scene()`; `resume` pollt den bestehenden
      HeyGen-Auftrag, `reconcile` bricht die Stage fail-closed ab.
- [ ] Manifest je Szene statt je Kapitel, mit Job-ID und tatsächlichen Kosten.
- [ ] Parallelität `max_concurrent_jobs`, 429-/5xx-Backoff.
- [ ] `btcedu avatar-reconcile` als Operator-Kommando für `resolve_job()`.

### WP-3 — Studio-Manifest und Compositor

- [ ] `assets/almanya24/studio/manifest.json` (Schema-Version 1): Studioplatte,
      Display-Zone `main_wall`, Vordergrundebenen, Safe Areas.
- [ ] Loader + Validator, fail-closed bei fehlender oder falscher Version.
- [ ] ffmpeg-Compositing: Alpha-WebM über Studioplatte, Themenbild in die
      Display-Zone (Anforderungen 4, 5, 8).
- [ ] MP4-Fallback: Provider liefert das Studio bereits gebacken.
- [ ] Reporter-Szenen bleiben vollflächige Medien aus der bestehenden Pipeline.

### WP-4 — Renderer auf den Scene-Plan umstellen

- [ ] `render_video()` schneidet nach `scene_plan.json` statt nach eigener
      Blockaufteilung; Original-TTS bleibt die Audioquelle (Anforderung 9).
- [ ] `render_settings`-Hash um den Plan-Hash erweitern.
- [ ] Remote-Render-Paket: Plan als Pflichteingabe prüfen.

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

- Volle Suite im Arbeitsverzeichnis: **2865 passed, 2 failed**.
- Volle Suite gegen einen sauberen `HEAD`-Worktree (nur zum Vergleich angelegt
  und wieder entfernt, das Arbeitsverzeichnis blieb unangetastet):
  **2788 passed, 1 failed**.
- `.venv/bin/ruff check btcedu/ tests/` → All checks passed.
- Die beiden verbleibenden Fehlschläge sind **vorbestehend und ohne Bezug** zur
  Avatar-Arbeit:
  - `test_cross_profile.py::test_tts_profile_config_values` — schlägt auch auf
    `HEAD` fehl; der Test erwartet `style > 0`, das Profil steht committet auf
    `style: 0.0`. Eigenes Ticket, nicht Teil dieser Arbeit.
  - `test_speech_normalize.py::TestTheLastTenEpisodes` — liest echte gerenderte
    Episoden dieser Maschine; im sauberen Worktree wird er übersprungen. Kein
    Codepfad dieser Arbeit berührt die Narration.
- `test_web.py::TestJobsAndLogs::test_run_all_nothing_to_do_on_published` fiel
  in einem Lauf aus und war im Wiederholungslauf sowie einzeln grün — ein
  Timing-Flake des JobManager-Threads, keine Regression.
- `sceneplan` ist in allen gefundenen Stage-Registern eingetragen
  (`_V2_STAGES`, `_STATUS_ORDER`, `_V2_ONLY_STAGES`, `_STAGE_NAME_TO_PIPELINE_STAGE`,
  `_RESUMABLE_EPISODE_STATUSES`, `_STAGE_LABELS`, `_STAGE_TO_PIPELINE_STAGE`,
  `_ALLOWED_STAGE_ACTIONS`, JobManager-Dispatch und Reset-Matrix).
- `SCENE_PLANNED` ist in jeder Statusprüfung ergänzt, die vorher `TTS_DONE`
  akzeptierte (`renderer`, `remote_render`, `anchor_generator`, CLI, JobManager).
- Kein Codepfad ruft HeyGen ohne `anchor_enabled` **und** Provider `heygen` auf.
- Die vier `report.*.json` sind Absturzartefakte und gehören nicht zum Commit.
- Nichts ist committet oder gepusht; alles liegt im Arbeitsverzeichnis.

## Arbeitsprotokoll

| Datum | Paket | Ergebnis |
|---|---|---|
| 2026-09-06 | Recovery | Stand rekonstruiert, Checkliste angelegt, Bestandstests grün |
| 2026-09-06 | Konsistenz | 12 durch `sceneplan` veraltete Bestandstests nachgezogen |
| 2026-09-06 | WP-1 | Avatar-Job-Register, Migration 016, 35 Tests, Ruff grün |
