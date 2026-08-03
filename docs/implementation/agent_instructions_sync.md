# Agent Instructions Sync

Synchronisation der Claude-/Copilot-Anweisungsdateien mit dem tatsächlichen
Code-Stand. Dieser Auftrag hat **ausschließlich Dokumentation** geändert —
kein Produktcode, keine Pipeline-Stages, keine Refactorings.

## Gefundene Instruktionsdateien

| Datei | Letzte inhaltliche Änderung | Datum |
|---|---|---|
| `CLAUDE.md` (Root) | `1d7291b` (davor `ae6d30e`) | 2026-07-19 |
| `btcedu/core/CLAUDE.md` | `8ffa377` | 2026-04-15 |
| `btcedu/models/CLAUDE.md` | `8ffa377` | 2026-04-15 |
| `btcedu/services/CLAUDE.md` | `8ffa377` | 2026-04-15 |
| `btcedu/prompts/CLAUDE.md` | `8ffa377` | 2026-04-15 |
| `tests/CLAUDE.md` | `8ffa377` | 2026-04-15 |
| `btcedu/web/CLAUDE.md` | `226040e` (davor `ded819c`, 2026-07-18) | 2026-08-03 |

Nicht vorhanden vor diesem Auftrag: `AGENTS.md`,
`.github/copilot-instructions.md`, `.github/instructions/**`,
`*.instructions.md`.

Arbeitsverzeichnis vor Beginn: Branch `main`, keine uncommitted Änderungen an
getrackten Dateien, ein einziger Worktree, kein Stash.

## Analysierte Commitspanne

`1d7291b..HEAD` — 46 Commits (2026-07-19 bis 2026-08-03).

Als Baseline wurde die letzte inhaltliche Änderung der Root-`CLAUDE.md` gewählt.
Die Commits `fdf52a8` und `226040e` (beide 2026-08-03) haben `CLAUDE.md` bzw.
`btcedu/web/CLAUDE.md` nur um den WhatsApp-Abschnitt ergänzt und liegen
innerhalb der Spanne.

Analysiert wurden die Diffs (nicht die Commitmessages) unter `btcedu/`,
`tests/`, `pyproject.toml` und `run.sh`. Ignoriert wurden Episodendaten,
Datenbanken, Logs, generierte Artefakte und reine Formatierung.

## Erkannte Änderungen mit Dokumentationsrelevanz

**Neue Core-Module**
- `btcedu/core/weather/` — vollständiges deterministisches Wetter-Subsystem
  (`detector`, `extractor`, `scene_planner`, `renderer`, `validator`, `models`,
  `lexicon`, `dates`, `cities`, `templates/`, `assets/`). Rendering über
  HTML/SVG + headless Chromium, nicht über ein Bildmodell.
- `final_review.py` — deterministische Wetter-Videoprüfungen an
  `review_gate_3` (Blank-/Freeze-/Stale-Frames, Auflösung, Narrationsabdeckung),
  fail-closed.
- `narration_lock.py` — Invariante der freigegebenen Narration inklusive
  deterministischer Reparatur kleiner Abweichungen.
- `retention.py` — `prune_expired_episodes()`, aufgerufen aus `detector.py`.
- `regression_runner.py` — Replay der letzten Episoden gegen geklonte DB und
  kopierte Outputs.

**Neue Services**
- `meteo_service.py` — Open-Meteo / DWD ICON, wirft nie, degradiert auf leere
  Liste.
- `notify_service.py` — WhatsApp-Push bei Stage-Fehlern über die lokale
  whatsapp-service-REST-API.
- `errors.py` — neue Kategorie `PERMANENT_QUOTA` (erschöpftes Provider-Guthaben).
- `claude_service.py` — Provider `copilot_cli` zusätzlich zu `anthropic`,
  `openai`, `github_models`.

**Pipeline / Review Gates**
- Automatische Adjudikation von `review_gate_transcript_qa` und `review_gate_2`
  durch ein unabhängiges Modell; mit `block_on_hold: false` läuft die Pipeline
  unbeaufsichtigt weiter, Entscheidungen landen in `gate_adjudication.json`.
- Wiederaufnahme von Episoden, die an einem offenen Gate parken.
- `ReviewStatus.SUPERSEDED` für Reviews, die ein erneut laufendes Gate entwertet.
- Fehlerbenachrichtigung im Fehlerpfad von `_run_stage()`.
- Die Stage-Liste `_V2_STAGES` selbst ist **unverändert** — das Diagramm in
  `CLAUDE.md` blieb korrekt.

**Modelle / Schemas**
- `Chapter`: neue optionale Felder `story_type`, `source_text`, `metadata`.
- `QAFinding`: `structural_invariant`, `source_unreliable`, `review_required`.

**Konfiguration**
- Neu in `Settings`: `transcription_secondary_minimum_severity`,
  `episode_retention_days`, `notify_whatsapp_*`.
- Profil `tagesschau_tr`: `weather`-Block inkl. `city_temperatures`,
  `gate_adjudication`, `validation_adjudication`, Intro-/Topic-Intro-Optionen,
  neue TTS-Stimme, `ingest.retention_days`.

**CLI**
- Neue Kommandos: `regression-run`, `weather-render`, `notify-test`.

**Web-Dashboard**
- Wetter-Endpunkte (`summary`, `detail`, `image`, `video`, `rerender`,
  `override`, `overrides`), Intro-Audio-Endpunkte, WhatsApp-Pairing-Seite.

**Tests**
- Testanzahl von ~1189 auf 1955 gestiegen (via `pytest --collect-only -q`
  verifiziert).

## Angepasste Dokumente

| Datei | Änderung |
|---|---|
| `CLAUDE.md` | Gate-Adjudikation, `review_gate_3`-Videochecks, `SUPERSEDED`, Wetter-Subsystem, Retention, `regression-run`, neue Config-Felder |
| `btcedu/core/CLAUDE.md` | neue Module dokumentiert, doppelte `frame_editor.py`-Zeile entfernt, Hinweis auf Fehlerbenachrichtigung |
| `btcedu/services/CLAUDE.md` | `meteo_service`, `notify_service`, `errors`, Provider-Liste in `claude_service`, doppelte `gemini_image_service`-Zeile entfernt |
| `btcedu/models/CLAUDE.md` | `ReviewStatus.SUPERSEDED`, neue `Chapter`-Felder, Abschnitt zum QA-Schema |
| `btcedu/prompts/CLAUDE.md` | tatsächliche Templateliste, Profil-Overrides, Glossare, Adjudikationshinweis |
| `tests/CLAUDE.md` | Testanzahl korrigiert |
| `btcedu/web/CLAUDE.md` | Wetter- und Intro-Audio-Endpunkte, Hinweis auf relative URLs |
| `.github/copilot-instructions.md` | **neu erstellt** |

Alle aktualisierten Dateien tragen am Ende einen Sync-Marker mit Baseline,
Zielstand und Datum.

## Copilot-Status

Die verwendete Copilot CLI (Version 1.0.78-2) liest Instruktionen offiziell aus:

```
CLAUDE.md (git root & cwd)
GEMINI.md (git root & cwd)
AGENTS.md (git root & cwd)
.github/instructions/**/*.instructions.md
.github/copilot-instructions.md
$HOME/.copilot/copilot-instructions.md
$HOME/.copilot/instructions/**/*.instructions.md
COPILOT_CUSTOM_INSTRUCTIONS_DIRS
```

`.github/copilot-instructions.md` ist damit das offiziell unterstützte Format
und wurde angelegt. Die Meldung
`No copilot-instructions.md found. Run /init to generate.` entfällt dadurch.

Die Datei ist bewusst eine komprimierte Fassung der `CLAUDE.md` und **keine**
zweite Projektdokumentation. Es wurden keine Cursor-, Windsurf- oder
Gemini-Dateien und keine neuen Agentensysteme erzeugt.

## Init-Status

Im Repository existiert **kein** eigener Initialisierungs- oder
Bootstrap-Mechanismus für Agenten-Dokumentation:

- `/init` ist ein eingebauter Befehl der Copilot CLI
  („Initialize Copilot instructions for this repository"), kein Repo-Artefakt.
- `btcedu init-db` initialisiert die Datenbank und hat mit Agenten-Instruktionen
  nichts zu tun.
- `run.sh` ist ein Deployment-Skript (git pull → pip → migrate → restart).

Es wurde daher bewusst **kein** Generator gebaut; der Sachverhalt ist hier
dokumentiert. Nach diesem Commit muss `/init` nicht mehr ausgeführt werden.

## `/allow-all`

**Ergebnis: nicht dauerhaft aktivierbar.**

`/allow-all` ist in dieser CLI-Version ein Sitzungsbefehl ohne `on`/`off`-Argument
(„Enable all permissions (tools, paths, and URLs)"). Es existiert kein
offizieller Konfigurationsschlüssel, der ihn dauerhaft setzt;
`~/.copilot/settings.json` kennt keinen entsprechenden Eintrag und
`~/.copilot/permissions-config.json` wird automatisch verwaltet und speichert
nur einzelne, interaktiv erteilte Freigaben pro Verzeichnis.

Offiziell vorgesehen und dauerhaft nutzbar sind ausschließlich die
Start-Optionen:

```bash
copilot --allow-all          # entspricht --allow-all-tools --allow-all-paths --allow-all-urls
copilot --allow-all-tools    # env: COPILOT_ALLOW_ALL
```

Wer das dauerhaft möchte, setzt die Umgebungsvariable in der eigenen
Shell-Konfiguration oder nutzt einen Alias — beides sind unterstützte
Mechanismen. Es wurden keine Hacks, Workarounds oder
Sicherheitsumgehungen implementiert und keine Berechtigungsdateien manipuliert.

## Bewusst NICHT angepasst

- Produktcode, Pipeline-Stages, Modelle, Provider, Tests — dieser Auftrag ist
  rein dokumentarisch.
- `docs/implementation/transcript_and_qa_upgrade_*.md` — historische
  Audit-Dokumente, die den Stand ihres Zeitpunkts festhalten sollen.
- `README.md` und weitere Nutzerdokumentation — nicht Teil des Auftrags.
- `btcedu/web/CLAUDE.md`-Struktur — nur ergänzt, nicht umgebaut.
- Der externe `whatsapp-service` unter `/home/pi/services/whatsapp-service`
  liegt außerhalb dieses Repositories und hat eine eigene `README.md`.
- Keine Bereinigung der 17 vorbestehenden ruff-Fehler in unberührten Dateien.

<!--
Documentation sync
Baseline: 1d7291b
Synced through: HEAD
Date: 2026-08-04
-->
