# Transcript and QA Upgrade: Repository Audit

Stand: 2026-07-18
Repository: `demirelh/bitcoin-education`
Branch/HEAD bei Auditbeginn: `main` / `f663623`

## Umfang und Ausgangslage

Dieses Dokument beschreibt den tatsächlich ausgecheckten Code. README, CLAUDE-Dateien
und bestehende Pläne wurden nur zum Abgleich verwendet.

Der Working Tree war bereits vor dem Audit nicht sauber:

- `.env.bak.v1removal` (untracked)
- `.venv` (untracked)
- `btcedu/web/static/samples/` (untracked)

Deshalb darf diese Phase nach der Aufgabenstellung keinen Commit erzeugen. Es wurden
keine funktionalen Produktionsänderungen vorgenommen.

Ein grundlegender Konflikt ist vor der Implementierung zu klären: Die neue Vorgabe
fordert weiterhin funktionsfähiges `pipeline_version=1`; der aktuelle Code hat v1
jedoch absichtlich entfernt, verbietet v1-Profile und Migration 013 löscht die
früheren v1-Tabellen. Die Zielarchitektur muss daher additiv auf v2 aufgebaut werden,
ohne die vorhandenen v1-Werte in Bestandsdaten weiter zu beschädigen. Eine
Wiederherstellung der alten v1-Pipeline ist ein separates Vorhaben und darf nicht
implizit im Transcript-/QA-Upgrade erfolgen.

## Ist-Architektur

### Pipeline-Orchestrierung

Die zentrale Orchestrierung liegt in `btcedu/core/pipeline.py`:

- `_STATUS_ORDER` ordnet `EpisodeStatus`.
- `_V2_STAGES` definiert die Basisreihenfolge.
- `_get_stages()` verändert sie anhand des persistierten Episode-Profils.
- `_run_stage()` dispatcht per Lazy Import in die Core-Module.
- `run_episode_pipeline()` führt die aufgelöste Liste aus.
- `_ensure_stage_pipeline_run()` ergänzt fehlende Zeitmessungen für Stages, die
  keinen eigenen erfolgreichen `PipelineRun` geschrieben haben.

Die Basis-v2-Reihenfolge ist:

```text
download
-> transcribe
-> correct
-> review_gate_1
-> translate
-> adapt
-> review_gate_2
-> chapterize
-> frameextract
-> imagegen
-> review_gate_stock
-> tts
-> anchorgen
-> render
-> review_gate_3
-> publish
```

Profilabhängige Änderungen:

- `stage_config.segment.enabled=true` fügt `segment` direkt vor `translate` ein.
- `stage_config.adapt.skip=true` entfernt `adapt`, benennt `review_gate_2` in
  `review_gate_translate` um und lässt `chapterize` bei `TRANSLATED` beginnen.
- `stages_enabled` wird geladen und in CLI/Web angezeigt, aber derzeit nicht zur
  Pipelineauflösung verwendet.
- `review_gates` wird ebenfalls geladen und angezeigt, beeinflusst die Gates aber
  nicht. Das konfigurierte `auto_approve_threshold` des Bitcoin-Profils ist damit
  keine aktive profilbasierte Schwelle.

`_get_stages()` fängt alle Fehler beim Profil-Lookup breit ab und fällt still auf die
Basisliste zurück. Das kann eine fehlerhafte Profilkonfiguration verdecken und ist
mit der neuen Vorgabe gegen stille Fehler nicht vereinbar.

### Tatsächliche Reihenfolge für `tagesschau_tr`

`btcedu/profiles/tagesschau_tr.yaml` setzt `stages_enabled: all`,
`segment.enabled: true`, `adapt.skip: false`, `auto_approve_reviews: true` und
`auto_publish: false`. Die effektive Reihenfolge ist daher:

```text
download
-> transcribe
-> correct
-> review_gate_1 (automatisch genehmigt)
-> segment
-> translate
-> adapt
-> review_gate_2 (QA-Zweitmeinung + automatisch genehmigt)
-> chapterize
-> frameextract
-> imagegen
-> review_gate_stock (automatisch genehmigt/finalisiert)
-> tts
-> anchorgen
-> render
-> review_gate_3 (automatisch genehmigt)
-> publish (übersprungen, weil auto_publish=false)
```

### Pipeline-Versionen

Der aktuelle Code besitzt nur eine ausführbare v2-Stage-Liste:

- `Settings.pipeline_version` ist `2`.
- `ContentProfile` lehnt jeden Wert ungleich `2` ab.
- `_run_stage()` verbietet für Episoden mit `pipeline_version != 2` alle Stages
  außer `download` und `transcribe`.
- Weitere v2-Guards stehen in Renderer, TTS, Image Generator, Publisher,
  Anchor Generator und Frame Extractor.
- Migration `013_drop_v1_chunks_table` löscht `chunks` und `chunks_fts`.

Gleichzeitig ist `Episode.pipeline_version` weiterhin mit Default `1` modelliert,
und Migration 002 fügte die Spalte mit Default `1` ein. Alte Episode-Zeilen können
deshalb einen Wert tragen, für den kein vollständiger Laufpfad mehr existiert.

**Folgerung:** Dokumentation und neue Anforderung sprechen von v1-Kompatibilität,
der aktuelle Code bietet sie nicht. Das Upgrade darf bestehende v1-Daten nicht
löschen oder automatisch umschreiben, kann v1 aber ohne gesonderte Wiederherstellung
nicht wieder funktionsfähig machen.

## Konfigurationspräzedenz

Die effektive Präzedenz ist nicht zentral implementiert, sondern ergibt sich aus
Pydantic Settings und den einzelnen Stage-Modulen:

1. Feld-Defaults in `btcedu/config.py`.
2. Werte aus `.env`.
3. Prozess-Umgebungsvariablen überschreiben `.env`.
4. CLI-Optionen ändern gezielt Laufzeitwerte, zum Beispiel `--profile`,
   `--force`, `--dry-run` oder einzelne Command-Parameter.
5. Bei neu erkannten Episoden werden `pipeline_version` und
   `default_content_profile` in der Episode persistiert.
6. Bei bestehenden Episoden bestimmt `Episode.content_profile`, welches YAML-Profil
   geladen wird.
7. Profilwerte überschreiben globale Settings nur dort, wo ein Stage-Modul sie
   ausdrücklich aus `profile.stage_config`, `profile.youtube` oder den
   Automatik-Flags liest.

Es gibt keinen allgemeinen Deep Merge zwischen Settings und Profilen. Daher ist die
Aussage "Profil überschreibt `.env`" nur für explizit ausgewertete Felder richtig.

Weitere Lücken:

- `stage_config.chapterize.model` ist im Bitcoin-Profil vorhanden, wird vom
  Chapterizer aber nicht als Modell-Override an `call_claude()` übergeben.
- Die LLM-Stage-Provenance und `ContentArtifact.model` speichern häufig
  `settings.claude_model`, obwohl tatsächlich Copilot CLI mit
  `settings.copilot_cli_model` oder ein anderer Provider ausgeführt wurde.
- `stages_enabled` und `review_gates` haben aktuell keine Laufzeitwirkung.

## Providerarchitektur

### Textmodelle

`btcedu/services/claude_service.py` bietet mit `call_claude()` einen gemeinsamen
Adapter und `ClaudeResponse` als einheitliches Ergebnisformat. Unterstützt werden:

- Anthropic
- OpenAI
- GitHub Models
- GitHub Copilot CLI

`_resolve_provider()` liest `settings.llm_provider` und enthält Key-abhängige
Fallbacks. Copilot CLI wird als Subprozess ausgeführt; das Modell kommt aus
`copilot_cli_model` oder `model_override`. Refusals werden erkannt, mit einer
Coding-Umrahmung erneut versucht und optional an Anthropic weitergereicht. Ein
vollständiger Refusal endet als `ModelRefusalError`.

Die aktuelle QA nutzt `model_override=settings.qa_model`. Damit ist das Modell
konfigurierbar, aber nicht unabhängig vom Provider: Es läuft über denselben global
aufgelösten Providerpfad. Copilot-CLI-Aufrufe melden Kosten von `0.0`, wodurch die
Kostenbilanz für die wichtigsten Textstages unvollständig ist.

### Transkription

`btcedu/services/transcription_service.py` kapselt OpenAI Whisper. Große Audiodateien
werden lokal mit pydub geteilt und nacheinander transkribiert. Der Request verwendet
`response_format="text"`. Der Service gibt ausschließlich einen String zurück.

### Sprache, Bilder, Anchor und Publishing

- ElevenLabs: `TTSService` Protocol plus `ElevenLabsService`.
- Anchor: `AnchorService` Protocol, `DIDService` und `DryRunAnchorService`.
- YouTube: `YouTubeService` Protocol, Dry-Run- und Data-API-Implementierung.
- Bilder: Factory für DALL-E 3, Flux/fal.ai und Ideogram mit DALL-E-Fallback;
  zusätzlich Pexels- und Gemini-Services.
- `tagesschau_tr` nutzt aktuell `imagegen.provider: generative`; das Core-Routing
  verteilt nach Visual-Typ auf Ideogram, Flux oder DALL-E. Gemini Frame Editing
  läuft nur bei explizitem Profilwert `gemini_frame_edit`.

Externe Provider sind in den Tests mockbar; Dry-Run-Implementierungen existieren für
kritische Dienste. Die Retry-Logik ist teilweise zentral
(`retry_on_transient`), teilweise providerspezifisch. `max_stage_retries` ist
konfiguriert, aber es wurde kein automatischer, generischer Whole-Stage-Retry-Loop
in der Pipeline gefunden; `retry_episode()` ist ein separater Wiederanlaufpfad.

## Daten- und Artefaktformate

### Transkription

`transcribe` schreibt:

- `data/transcripts/<episode>/transcript.de.txt`
- `data/transcripts/<episode>/transcript.clean.de.txt`

Nur der Pfad zum bereinigten Text wird in `Episode.transcript_path` gespeichert.
Es gibt kein Transcript- oder TranscriptSegment-Modell. Whisper liefert keine
Segment-, Wort- oder Zeitstempel, und die beim Größen-Splitting verwendeten lokalen
Chunk-Grenzen werden nicht persistiert. Zeitstempel und Segmente gehen daher
vollständig verloren.

### Inhaltliche Stages

| Stage | Haupteingang | Hauptausgang |
|---|---|---|
| correct | `transcript.clean.de.txt` | `transcript.corrected.de.txt`, `review/correction_diff.json` |
| segment | korrigiertes DE-Transkript | `stories.json` (`StoryDocument`) |
| translate | korrigiertes DE-Transkript, optional `stories.json` | `transcript.tr.txt`, bei Story-Modus zusätzlich strukturierte Story-Ausgaben |
| adapt | TR-Transkript + korrigiertes DE-Transkript | `script.adapted.tr.md`, `review/adaptation_diff.json` |
| chapterize | adaptiertes Skript oder übersetzte Stories | `chapters.json` (`ChapterDocument`) |
| QA | korrigiertes DE + adaptiertes TR | `qa_review.json`, `qa_review.md` |

`StoryDocument` und `ChapterDocument` sind Pydantic-v2-Schemas. ChapterDocument
validiert unter anderem Schema-Version, Kapitelzahl, Reihenfolge und Dauer.

Jede zentrale LLM-Stage schreibt zusätzlich JSON-Provenance unter
`data/outputs/<episode>/provenance/` und einen `ContentArtifact`-Datensatz.
`PipelineRun` speichert Stage, Status, Zeiten, Tokens, geschätzte Kosten, Fehler und
Git-Commit.

## Implementierung von correct, segment, translate, adapt und chapterize

Die fünf Module folgen weitgehend demselben Muster:

1. Episode und erlaubten Status prüfen.
2. Profil und Prompt über `PromptRegistry` auflösen.
3. Prompt-Version und SHA-256-Hash bestimmen.
4. Eingabedateien und Eingabehash berechnen.
5. Bei unverändertem Input/Prompt und ohne `.stale` überspringen.
6. `PipelineRun(RUNNING)` erzeugen.
7. LLM über `call_claude()` aufrufen.
8. Ausgabe validieren/schreiben.
9. Provenance, `ContentArtifact`, Kosten und Token persistieren.
10. Episode-Status aktualisieren und Folgeartefakt als stale markieren.
11. Bei Fehler `PipelineRun(FAILED)` setzen und erneut werfen.

Besonderheiten:

- Corrector schützt Zahlen/Datumswerte gegen unerwünschte LLM-Änderungen.
- Segmenter erzeugt ein validiertes `StoryDocument`.
- Translator unterstützt Whole-Transcript- und Per-Story-Modus.
- Translator und Adapter injizieren QA-Findings des vorherigen Laufs als
  `reviewer_feedback`.
- Chapterizer besitzt JSON-Reparatur und Korrektur-Retries sowie strukturelles
  Backfilling. Er markiert Images und TTS stale.

## Review- und QA-Architektur

### Human-Review

`ReviewTask` ist generisch nach Episode und Stage. Es speichert Artefaktpfade,
Diff-Pfad, Prompt-Version, Status, Notizen und den SHA-256-Artefakthash.
`ReviewDecision` bildet die Historie mit Entscheidung, Notizen und Bewertung 1-5 ab.
`ReviewItemDecision` unterstützt granulare Entscheidungen auf Diff-Positionen.

Die Gates verwenden `has_pending_review()` und `has_approved_review()`:

- Gate 1 prüft Correct.
- Gate 2 prüft Adapt beziehungsweise `review_gate_translate` die Translation.
- Stock-Gate finalisiert die Bildauswahl.
- Gate 3 prüft Render und setzt die Episode auf `APPROVED`.

Approve berechnet den Artefakthash erneut. Reject/Request Changes können den
Episode-Status zurücksetzen; Request Changes erzeugt `.stale`-Marker.
`auto_approve_reviews` erzeugt echte bereits genehmigte Tasks und Decisions, damit
Downstream-Prüfungen weiterhin funktionieren.

### Aktuelle QA-Zweitmeinung

`btcedu/core/qa_reviewer.py` ist keine Pipeline-Stage und kein Gate. Sie wird im
Handler von `review_gate_2` aufgerufen, verwendet `PipelineStage.REVIEW` und ist
ausdrücklich advisory-only. Fehler werden dort nicht blockierend behandelt.

Die QA-Dateien enthalten Gesamtscore, Zusammenfassung, Story-Bewertungen, fehlende
Inhalte, Halluzinationen, Neutralisierungslücken und Top-Fixes. Idempotenz basiert
auf einem Fingerprint aus DE-Hash, TR-Hash, QA-Modell und Prompt-Hash.

Beim nächsten manuellen QA-Re-Run formatiert `format_qa_feedback()` die Findings
als Promptblock. Translator und Adapter verwenden damit die Kritik des vorherigen
Laufs. Die aktuelle QA prüft jedoch nur gegen das bereits korrigierte deutsche
Transkript, nicht gegen Audio, Whisper-Segmente oder Zeitstempel.

## Idempotenz-, Stale-, Retry- und Kostenarchitektur

- SHA-256-Hashes für Eingaben und Prompts liegen in Stage-Provenance-Dateien.
- Ohne `force` wird bei passenden Hashes übersprungen.
- `force=True` umgeht Status- und Current-Prüfungen.
- Upstream-Stages schreiben Sidecar-`.stale`-Marker für direkte Downstream-Artefakte.
- Die Cascade ist eine lineare Ein-Schritt-Invalidierung, kein zentraler DAG.
- `PipelineRun` bildet Ausführung, Dauer, Tokens, Kosten und Fehler ab.
- `ContentArtifact` ergänzt Datei, Artefakttyp, Modell und Prompt-Hash.
- Provider-Retries klassifizieren transiente Fehler; ein zentraler Stage-Retry ist
  trotz `max_stage_retries` nicht nachgewiesen.
- Die Kostenobergrenze wird in Image Generation, Frame Editing, TTS, Anchor und
  vor Publish geprüft. Sie wird nicht zentral vor jeder Stage durchgesetzt.
- LLM-Kosten über Copilot CLI werden als null erfasst.

## Web-Restarts

`POST /episodes/<id>/qa-rerun`:

1. `translate_transcript(..., force=True)`
2. `adapt_script(..., force=True)`
3. `generate_qa_review(..., force=True)`
4. Stopp nach der neuen QA

`POST /episodes/<id>/qa-rerun-all` führt zunächst denselben Loop aus und startet
danach `run_episode_pipeline(..., force=False)`. Weil Adapt die vorhandene
`chapters.json` als stale markiert und den Status auf `ADAPTED` setzt, läuft die
Pipeline ab Chapterize weiter. Downstream-Neubau hängt damit von einer vollständigen
Stale-Kette ab; ein fehlender Marker kann alte Artefakte erhalten.

## Datenbankmigrationen

Vorhandene Reihenfolge:

1. `001_add_channels_support`
2. `002_add_v2_pipeline_columns`
3. `003_create_prompt_versions`
4. `004_create_review_tables`
5. `005_create_media_assets`
6. `006_create_publish_jobs`
7. `007_add_review_item_decisions`
8. `008_add_content_profile`
9. `009_create_dead_letter_queue`
10. `010_add_quality_rating`
11. `011_add_channel_content_profile`
12. `012_add_pipeline_run_git_commit`
13. `013_drop_v1_chunks_table`

Die Migrationen werden über `schema_migrations` versioniert und prüfen überwiegend
vor dem Ändern. Migration 013 ist destruktiv und praktisch nicht rückwärtsfähig.

### Voraussichtlich notwendige Migration

Für erhaltene Zeitstempel und Analyseergebnisse ist eine additive Transcript-Struktur
sinnvoll, zum Beispiel:

- `transcripts`: Episode, Sprache, Provider, Modell, Format-/Schema-Version,
  Audiohash, Roh-/Clean-Dateipfad, Dauer, erstellt/aktualisiert.
- `transcript_segments`: Transcript-FK, Index, Start/Ende, Text, optional
  Confidence und Wortdaten als JSON.

Alternativ kann zunächst ein versioniertes JSON-Artefakt verwendet werden. Das
vermeidet eine Migration, erschwert aber Abfragen, Reviews und langfristige
Schemaentwicklung. Für die geplanten verifizierbaren Transcript-Gates ist eine
additive DB-Persistenz vorzuziehen.

### Vermeidbare Migrationen

- Neue Review-/QA-Arten benötigen keine eigenen Tabellen; `ReviewTask.stage`,
  `ReviewDecision`, `ContentArtifact` und `PipelineRun` sind generisch genug.
- Ein separates `publish_gate` braucht keine neue Tabelle; vorhandene
  Publisher-Safety-Checks und Gate-Reviews können wiederverwendet werden.
- QA-Findings können weiter als strukturierte, versionierte Artefakte gespeichert
  werden, solange Such-/Reporting-Anforderungen keine Normalisierung verlangen.

Neue `EpisodeStatus`- und `PipelineStage`-Enumwerte können bei SQLite/SQLAlchemy
trotzdem eine Schema-/Datenkompatibilitätsprüfung erfordern. Vor einer Migration ist
zu testen, wie die aktuellen Enum-Spalten auf bestehenden Datenbanken angelegt sind.

## Abweichungen zwischen Code und Dokumentation

- README und CLAUDE behaupten, v1 und v2 existierten parallel. Aktiver v1-Code und
  v1-Tabellen wurden entfernt.
- README/CLAUDE beschreiben Gemini für `tagesschau_tr` und Pexels für alle anderen;
  das aktuelle Profil nutzt generatives Ideogram/Flux/DALL-E-Routing.
- Dokumentierte v2-Reihenfolgen lassen `segment`, `frameextract`, `anchorgen` und
  `review_gate_stock` teilweise aus.
- README nennt `PIPELINE_VERSION` als 1 oder 2; Profile akzeptieren nur 2.
- CLAUDE nennt ein Kostenlimit von 10 USD; Code-Default ist 15 USD,
  `.env.example` weiterhin 10 USD.
- `.env.example` enthält entfernte v1-Chunk-Einstellungen und dokumentiert weder
  `LLM_PROVIDER`, Copilot CLI, QA-Modell noch die aktuellen Bildprovider vollständig.
- `docs/pipeline.md` nennt Python 3.12; die vorhandene `.venv` läuft derzeit mit
  Python 3.13.5. `pyproject.toml` verlangt nur `>=3.12`, Ruff zielt auf py312.
- `docs/pipeline.md` beschreibt die Pipeline insgesamt genauer, vereinfacht aber
  die profilselektive Konfigurationspräzedenz und nennt QA als Teil von Gate 2,
  nicht als fehlende eigenständige Stage.
- Kommentare in `resolve_pipeline_plan()` erwähnen weiterhin v1-Auswahl, obwohl
  `_STAGES` nur auf `_V2_STAGES` zeigt.

## Wiederverwendbare Komponenten

- `PromptRegistry` mit Namespace, Versionierung und Prompt-Hash.
- `call_claude()`/`ClaudeResponse` mit Providerauflösung und Modell-Override.
- `retry_on_transient` und Fehlerklassifizierung.
- Stage-Signatur `(session, episode_id, settings, force=False)`.
- `PipelineRun`, `ContentArtifact` und Provenance-JSON.
- Current-/Skip-/Force- und `.stale`-Muster.
- `ReviewTask`, `ReviewDecision`, `ReviewItemDecision` und Artefakthash-Prüfung.
- `qa_reviewer` als Basis für `translation_qa`.
- Publisher-Safety-Checks als Basis für `publish_gate`.
- Story- und Chapter-Pydantic-Schemas.
- Web-Job-System, Stage-Fortschritt und vorhandene QA-Ansicht.

## Angepasste Zielarchitektur

Es soll keine zweite Pipeline entstehen. Die Zielarchitektur wird als additive
Erweiterung der bestehenden profilaufgelösten v2-Liste umgesetzt:

```text
download
-> transcribe
-> transcript_analyze
-> transcript_verify
-> correct
-> transcript_qa_gate
-> review_gate_1 (bestehend; je Profil weiterhin aktiv/auto-approved)
-> segment (profilabhängig wie bisher)
-> translate
-> adapt (profilabhängig wie bisher)
-> translation_qa
-> review_gate_2 oder review_gate_translate (bestehend)
-> chapterize
-> frameextract
-> imagegen
-> review_gate_stock
-> tts
-> anchorgen
-> render
-> review_gate_3
-> publish_gate
-> publish
```

Dabei darf `translation_qa` die vorhandene QA nicht duplizieren: Der bestehende
Aufruf wird aus `review_gate_2` in eine echte Stage extrahiert. `publish_gate`
extrahiert die vorhandenen Safety-Checks aus `publish`, statt sie neu zu erfinden.
Die bestehenden Review-Gates bleiben erhalten.

## Geplante Änderungen nach Datei/Modul

### Phase 2: Transcript-Grundlage

- `btcedu/config.py`: konfigurierbare Transcript-Formate, Analyse-/Verify-Provider,
  Modelle, Schwellen und Feature-Flags.
- `btcedu/services/transcription_service.py`: typisiertes Ergebnis mit Segmenten und
  Zeitstempeln; Whisper `verbose_json`; Chunk-Offsets korrekt addieren.
- `btcedu/core/transcriber.py`: Roh-JSON plus bestehende Textdateien schreiben;
  alte Text-Consumer unverändert versorgen.
- `btcedu/models/`: additive Transcript-/Segmentmodelle oder zunächst klar
  versioniertes Artefaktmodell nach finaler Persistenzentscheidung.
- `btcedu/migrations/`: idempotente additive Migration, keine Änderung an 001-013.
- Tests: Mock-Responses, Chunk-Offset, Idempotenz, alter Textpfad, keine API-Calls.

### Phase 3: Analyse und Verifikation

- Neue Core-Module `transcript_analyzer.py` und `transcript_verifier.py`.
- Neue Promptdateien mit Profil-Namespace und PromptRegistry.
- Strukturierte Pydantic-v2-Ergebnisse für Qualitätsprobleme, unsichere Passagen,
  Coverage und Zeitbezüge.
- Vollständige PipelineRun-/ContentArtifact-/Provenance-/Stale-/Force-/Retry- und
  Kostenintegration.

### Phase 4: Transcript-QA-Gate

- Vorhandenes Review-System um Stage-Namen und Artefakte erweitern.
- Policy konfigurierbar machen: blockierend, auto-approved oder advisory pro Profil.
- `review_gate_1` nicht entfernen; klar trennen zwischen automatischer Transcript-QA
  und menschlicher Korrekturfreigabe.

### Phase 5: Translation-QA extrahieren

- `qa_reviewer.py` in eine echte `translation_qa`-Stage überführen.
- Aufruf aus `review_gate_2` entfernen, nicht duplizieren.
- Bestehende `qa_review.json/.md`, Feedback-Loop und Web-Ansicht kompatibel halten.
- Konfigurierbare Pass-/Block-Schwelle; bestehendes advisory-Verhalten als
  kompatibler Default.

### Phase 6: Publish-Gate extrahieren

- Publisher-Safety-Checks in eine idempotente `publish_gate`-Stage extrahieren.
- `publish_video()` führt zur Sicherheit weiterhin eine finale Prüfung aus oder
  validiert die Gate-Provenance unmittelbar vor Upload.
- `review_gate_3` bleibt die menschliche Video-/Metadatenfreigabe.

### Phase 7: Orchestrierung und Web

- `EpisodeStatus`, `PipelineStage`, `_STATUS_ORDER`, `_get_stages()`,
  `_STAGE_NAME_TO_PIPELINE_STAGE` und `_run_stage()` additiv erweitern.
- Keine breite Profil-Fallback-Exception mehr; Fehler explizit protokollieren bzw.
  propagieren.
- `stages_enabled` entweder korrekt implementieren oder als rein informatives Feld
  markieren; keine halbaktive Semantik.
- QA-Restarts auf die neuen Stages abbilden und alte Endpunkte kompatibel lassen.
- Web-Fortschritt, Stage-Details und erlaubte Einzelstage-Aktionen ergänzen.

### Phase 8: Provenance und Kosten härten

- Tatsächlich verwendetes `ClaudeResponse.model` statt `settings.claude_model`
  persistieren.
- Stage-spezifische Modell-Overrides zentral und konfigurierbar auflösen.
- Zentralen Kosten-Check vor kostenpflichtigen Stages ergänzen.
- Copilot-Kosten als unbekannt/Subscription statt irreführend null kennzeichnen.
- Whole-Stage-Retry entweder sauber implementieren oder ungenutzte Konfiguration
  entfernen; keine doppelte Retry-Schicht ohne klare Verantwortung.

## Rückwärtskompatibilitätsstrategie

1. Bestehende Dateinamen bleiben erhalten; neue strukturierte Transcript-Artefakte
   werden zusätzlich geschrieben.
2. Bestehende Episoden ohne Segmentdaten werden bei Bedarf lazy backfilled oder
   laufen im Legacy-Textmodus; kein automatischer kostenpflichtiger Re-Transcribe.
3. Neue Stages werden nur für v2/Profile mit Feature-Flag aktiviert, bis Migration
   und Backfill validiert sind.
4. Bestehende QA-Dateien, URLs und Web-Buttons bleiben lesbar/funktionsfähig.
5. Bestehende Review-Gates werden nicht umbenannt oder gelöscht.
6. Migrationen sind ausschließlich additiv und check-before-act.
7. `pipeline_version=1`-Zeilen werden nicht verändert. Der bestehende Defekt wird
   dokumentiert; eine echte v1-Reaktivierung benötigt einen separaten Beschluss.
8. Profile ohne neue Konfiguration erhalten verhaltenssichere Defaults.

## Risiken vor der Implementierung

1. **Blocker: v1-Anforderung gegen Codezustand.** v1 ist nicht funktionsfähig und
   Migration 013 hat Datenstrukturen gelöscht.
2. **Status-/Enum-Migration.** Neue Zwischenstages können bestehende Statuslogik,
   Dashboard-Sortierung und SQLite-Enumwerte beeinflussen.
3. **Zeitstempel bei Audio-Chunks.** Segmentzeiten müssen um Chunk-Offsets verschoben
   und an Grenzen dedupliziert werden.
4. **Kostenanstieg.** Analyse, Verify und zwei QA-Gates erhöhen Modellaufrufe; Limits
   und Dry Run müssen vor Aktivierung greifen.
5. **Doppelte QA.** Der bestehende Gate-2-Aufruf muss extrahiert, nicht zusätzlich
   ausgeführt werden.
6. **Stale-Ketten.** Neue Zwischenartefakte vergrößern das Risiko unvollständiger
   Invalidierung; eine gemeinsame Helper-Schicht ist vorzuziehen.
7. **Provider-/Provenance-Drift.** Aktuell wird das tatsächlich verwendete Modell
   nicht überall korrekt aufgezeichnet.
8. **Profil-Fallbacks.** Breite Exceptions können falsche Stage-Listen aktivieren.
9. **Python-Version.** Ziel ist 3.12, die lokale Umgebung läuft auf 3.13.5; Tests
   können pydub/audioop-spezifisch abweichen.
10. **Logging und Datenschutz.** Vollständige Transkripte/Providerfehler dürfen nicht
    zusammen mit Secrets oder sensitiven API-Antworten protokolliert werden.

## Schrittweiser Implementierungsplan

1. Architekturentscheidung zu v1 und Transcript-Persistenz bestätigen.
2. Tests für aktuellen Stage-Resolver, Profilpräzedenz und Modell-Provenance ergänzen.
3. Strukturierte Whisper-Ergebnisse additiv implementieren und migrieren.
4. `transcript_analyze` mit vollständigem Stage-Vertrag implementieren.
5. `transcript_verify` implementieren und gegen Audio-/Segmentdaten testen.
6. `transcript_qa_gate` auf dem bestehenden Review-System aufbauen.
7. Bestehende QA als `translation_qa` extrahieren und Feedback-Loop erhalten.
8. Publisher-Safety-Checks als `publish_gate` extrahieren.
9. Pipeline, CLI, Web, Fortschritt und Restart-Flows ergänzen.
10. Profile über Feature-Flags schrittweise aktivieren; v1-Daten unangetastet lassen.
11. Nach jeder Phase vollständige Tests, Ruff Check, Ruff Format Check und
    `git diff --check`; exakte Phase jeweils separat committen.
