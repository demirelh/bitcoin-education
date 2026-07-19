# Unabhängiges Code-Review: Transkript- & QA-Pipeline-Upgrade

**Reviewer:** Unabhängiger Senior-Code-Reviewer (nicht der Implementierungs-Agent)
**Datum:** 2026-07-19
**Umfang:** Commits `f663623..HEAD` (11 Commits, ~20.400 Zeilen +, ~1.350 −, 105 Dateien)
**Methodik:** Direkte Lektüre des Codes und des Git-Diffs. Dokumentation und
Abschlussberichte des vorherigen Agenten (`*_audit.md`, `*_result.md`,
`*_self_review.md`) wurden bewusst **nicht** als Wahrheitsquelle verwendet; jede
Aussage wurde am tatsächlichen Code verifiziert. Drei unabhängige
Verifikationsstränge (deterministische QA, Transcript-Verifier/Audio,
Testechtheit) wurden zusätzlich per Sub-Agent geprüft und deren Behauptungen
stichprobenartig am Code nachvollzogen.

---

## Executive Summary

Der Umbau ist architektonisch **sauber in die bestehende v2-Stage-Pipeline
integriert** (keine Parallelarchitektur), die neuen Stages laufen im echten
`run_latest`/`run_pending`, `pipeline_version=1` bleibt kompatibel, der
Narration-Lock und das GREEN/YELLOW/RED-Gate sind robust und korrekt
fail-closed. Die Kernanforderungen sind zu einem hohen Grad erfüllt.

Es gibt jedoch **keine blockierenden „critical"-Findings, aber mehrere „major"**
Findings, die vor einem produktiven Dauerbetrieb behoben werden sollten —
insbesondere eine **fehlende Cost-Guard im `correct`-Stage** (bezahlter
LLM-Aufruf ohne Budgetprüfung) und **lokalisierungs-blinde Zahlennormalisierung**
(deutsche/türkische Tausender-/Dezimaltrennung), die zu False-Positive-Findings
führen kann. Dazu kommt eine **Test-Lücke**: Die neuen Stages sind fast
ausschließlich isoliert bzw. auf Plan-Ebene getestet, aber nicht als echte
End-to-End-Orchestrierung.

**Produktionsreife:** Bedingt. Für den überwachten Betrieb (tagesschau_tr mit
`auto_publish=false`) einsetzbar; für vollautomatischen, unüberwachten Betrieb
erst nach Behebung der Major-Findings F1–F4 empfohlen.

---

## Findings

### CRITICAL
Keine.

---

### MAJOR

#### F1 — `correct`-Stage ruft bezahlten LLM ohne Cost-Guard auf
- **Datei/Symbol:** `btcedu/core/corrector.py` → `correct_transcript()` /
  `_call_structured_correction()` (Schleife ab ~Z. 279–314)
- **Problem:** Der `correct`-Stage iteriert über Payloads und ruft pro Payload
  `_call_structured_correction()` (→ `call_claude`) auf. In `corrector.py`
  existiert **kein** Aufruf eines Cost-Guards (`grep` nach
  `_ensure_cost_budget`/`max_episode_cost`/`_cumulative_cost` in der Datei:
  keine Treffer). Alle anderen bezahlten Stages (`adapter`, `translator`,
  `tts`, `image_generator`, `transcript_verifier`, `qa_reviewer`) prüfen das
  kumulative Episodenbudget vor dem bezahlten Aufruf.
- **Reproduzierbares Szenario:** Episode hat bereits Kosten nahe
  `max_episode_cost_usd`. Ein langes Transkript erzeugt viele Correction-Payloads
  → beliebig viele Claude-Aufrufe, ohne dass die Episode je auf `COST_LIMIT`
  gesetzt oder abgebrochen wird.
- **Auswirkung:** Verletzt Anforderung 7 („Cost Guard vor jedem
  kostenpflichtigen Aufruf"). Kostenüberschreitung möglich; inkonsistent zum
  dokumentierten Stage-Pattern in `btcedu/core/CLAUDE.md` (Punkt 5).
- **Korrektur:** Vor jedem `_call_structured_correction` (und vor der
  Payload-Schleife) das kumulative Budget prüfen — analog
  `transcript_verifier._ensure_cost_budget(session, episode, settings, est_cost)`
  bzw. `_budget_available` im QA-Reviewer. Bei Überschreitung `PipelineError`
  mit `PERMANENT_COST_LIMIT` werfen und Episode auf `COST_LIMIT` setzen.
- **Regressionstest:** Episode mit kumulativer Kostenschätzung > Limit → `correct`
  bricht **vor** dem ersten `call_claude` ab (Mock von `call_claude` darf nicht
  aufgerufen werden); Episode-Status wird `COST_LIMIT`.

#### F2 — Zahlennormalisierung ignoriert deutsche/türkische Tausender-/Dezimaltrennung
- **Datei/Symbol:** `btcedu/core/translation_qa.py` → `_NUMBER_RE` (Z. 80),
  `extract_numeric_facts()` (Z. 667–726), `_decimal()`/`_scaled_decimal()`
  (Z. 952–966)
- **Problem:** `_decimal()` macht lediglich `value.replace(",", ".")` und parst
  mit `float()`. `_NUMBER_RE = r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)"` erlaubt nur **eine**
  Trennzeichen-Gruppe. Deutsche/türkische Tausenderpunkte werden so als
  Dezimalpunkt interpretiert:
  - `1.000` (=eintausend) → `1.0`
  - `1.000 Euro` über `_MONEY_RE` → `_scaled_decimal("1.000", None)` → `1:EUR`
  - `1.000.000` → nur `1.000` erfasst → `1.0`
- **Reproduzierbares Szenario:** Quelle „1.000 Euro", Übersetzung „1000 avro"
  (ohne Tausenderpunkt): Quelle normalisiert zu `1:EUR`, Ziel zu `1000:EUR` →
  falscher `money_mismatch` (Severity **major**). Bei ≥ `max_major_findings`
  wird das Gate unnötig YELLOW/RED und löst einen automatischen Re-Run aus.
- **Auswirkung:** Verletzt Anforderungen 14/15. Symmetrisch identische
  Formatierung hebt sich zwar auf, aber jede legitime Formatabweichung erzeugt
  ein False-Positive; zusätzlich werden echte Millionen-/Tausenderabweichungen
  maskiert (False Negative).
- **Korrektur:** Locale-bewusste Parsing-Funktion: erst Tausenderpunkte/
  -leerzeichen entfernen (`de-DE`/`tr-TR`: `.` = Tausender, `,` = Dezimal),
  dann auf kanonischen numerischen Wert normalisieren. Ordinalpunkte (`3.`) als
  Ordinal statt Dezimal behandeln.
- **Regressionstest:** Parametrisierter Test mit `("1.000","1000")`,
  `("1.000,50","1000,50")`, `("1.000.000","1 milyon")` → jeweils **kein**
  `number_mismatch`/`money_mismatch`; und `("1.000","100")` → mismatch erkannt.

#### F3 — Negationsprüfung ist heuristisch/verrauscht
- **Datei/Symbol:** `btcedu/core/translation_qa.py` → `_check_marker_groups()`
  (Z. 846–880), `_NEGATION_GROUPS` (Z. 135–139), `_TR_VERBAL_NEGATION_RE`
  (Z. 141–147)
- **Problem:** Die türkische Verbalnegation wird über ein breites
  Suffix-Regex (`-ma/-me` + Tempus-/Personalendungen) erkannt und die Marker
  enthalten sehr generische Token (`"ne "`, `" ne"`). Dadurch kann ein Zieltext
  mit vielen regulären `-ma/-me`-Verben eine „vorhandene Negation" vortäuschen
  (False Negative) oder umgekehrt Rauschen erzeugen.
- **Reproduzierbares Szenario:** Quelle enthält „nicht", Ziel enthält zufällig
  ein unnegiertes Verb, dessen Stamm auf `-ma`/`-me` endet → `_TR_VERBAL_NEGATION_RE`
  matcht → Negation gilt fälschlich als erhalten.
- **Auswirkung:** Sinnverkehrende Negationsfehler (semantisch kritisch) können
  unentdeckt bleiben; Anforderung 7 (Robustheit) nur teilweise erfüllt.
- **Korrektur:** Satz-/Klausel-Ebenen-Alignment statt reiner Suffix-Heuristik;
  explizites Negations-Lexikon plus Zählung negierter Prädikate pro Story
  (Quelle vs. Ziel), nicht nur Vorkommen im Gesamttext.
- **Regressionstest:** Story mit Quell-Negation, deren Übersetzung die Negation
  weglässt, obwohl ein unbezogenes `-ma`-Verb vorhanden ist → `negation_mismatch`
  wird erzeugt.

#### F4 — Neue Stages sind nicht als echte End-to-End-Orchestrierung getestet
- **Datei/Symbol:** `tests/test_pipeline.py::TestV2PipelineE2E` (Z. 838–990),
  `tests/test_phase9_pipeline_integration.py`, diverse `*_cli.py`-Tests
- **Problem:** `run_episode_pipeline` wird in den E2E-Tests mit **gemocktem**
  `_run_stage` ausgeführt; der Fortschritt entsteht durch den Mock-Seiteneffekt.
  Es existiert **kein** Test, der `transcript_analyze` → `transcript_verify` →
  `transcript_qa` → `review_gate_transcript_qa` mit **echten** Implementierungen
  in Sequenz durch die Orchestrierung laufen lässt. Die CLI-Tests sind
  überwiegend tautologisch („Funktion mocken → CLI aufrufen → assert Mock mit
  denselben Args aufgerufen"), z. B. `tests/test_transcript_analyze_cli.py:12-83`,
  `tests/test_transcript_verify_cli.py:12-88`, `tests/test_transcript_qa_cli.py:12-106`,
  `tests/test_run_latest_and_retry_cli.py:12-161`.
- **Reproduzierbares Szenario:** Eine Regression in `_run_stage`-Verdrahtung
  (z. B. falscher Statusübergang zwischen `transcript_qa` und `review_gate_1`)
  würde von den vorhandenen Tests **nicht** erkannt.
- **Auswirkung:** Verletzt Anforderung 25 (Tests prüfen echte Orchestrierung).
  Falsche Sicherheit über die tatsächliche Stage-Verkettung.
- **Korrektur:** Mindestens ein Integrationstest, der mit gemockten
  **externen APIs** (Whisper/Claude/Copilot CLI), aber **echtem** `_run_stage`
  und `_get_stages` eine v2-Episode von `TRANSCRIBED` bis zum ersten Review-Gate
  durchfährt und die tatsächlich ausgeführte Stage-Reihenfolge sowie die
  Statusübergänge assertet.
- **Regressionstest:** Siehe Korrektur — plus ein echter v1-Durchlauf (F-Kompat)
  und ein „RED blockiert Folge-Stages im echten Run"-Test.

---

### MINOR

#### F5 — Geteiltes, statisches Temp-Verzeichnis in der Whisper-Chunk-Transkription
- **Datei/Symbol:** `btcedu/services/transcription_service.py` →
  `_transcribe_chunked()` (Z. 330–341) und `_transcribe_chunked_structured()`
  (Z. 359–384): `tmp_dir = Path(audio_path).parent / "_whisper_tmp"`.
- **Problem:** Deterministischer, nicht eindeutiger Pfad. Zwei Läufe auf
  **derselben** Audiodatei würden `segment_000.mp3` überschreiben und per
  `shutil.rmtree` gegenseitig Dateien löschen. Durch den `pipeline_lock`
  (`btcedu/core/runlock.py`) sind Batch-Läufe zwar serialisiert, aber ein direkt
  (z. B. über Web-Job) außerhalb des Locks angestoßener Transkriptionsaufruf ist
  nicht geschützt. Bei hartem Prozessabbruch bleibt das Verzeichnis liegen.
- **Auswirkung:** Anforderungen 10/29 (Temp-Bereinigung/Races) nur teilweise.
- **Korrektur:** `tempfile.TemporaryDirectory(prefix=...)` verwenden (wie im
  `transcript_verifier`, der es korrekt macht) oder eindeutigen Run-Suffix.
- **Regressionstest:** Zwei nebenläufige Transkriptionen derselben Audiodatei
  kollidieren nicht.

#### F6 — Nicht-atomare Schreibvorgänge kritischer Artefakte in `chapterize`
- **Datei/Symbol:** `btcedu/core/chapterizer.py` → `chapters_path.write_text(...)`
  (~Z. 563) und `provenance_path.write_text(...)` (~Z. 601).
- **Problem:** Das Quality-Gate (`qa_reviewer._atomic_write_text` → tempfile +
  `os.replace`) schreibt atomar, `chapters.json` jedoch direkt. Ein Absturz
  während des Schreibens hinterlässt eine korrupte `chapters.json`, die
  Folge-Stages (frameextract/imagegen/tts/render) als „vorhanden" konsumieren.
- **Auswirkung:** Anforderung 30 (atomare kritische Schreibvorgänge) teilweise.
- **Korrektur:** Denselben `_atomic_write_text`-Helfer (in ein gemeinsames
  Util anheben) auch für `chapters.json`/Provenienz verwenden.
- **Regressionstest:** Simulierter Fehler nach Teil-Write hinterlässt keine
  halbe `chapters.json` (alte Datei bleibt intakt).

#### F7 — Breite `except Exception`/`except:`-Blöcke degradieren still
- **Datei/Symbol:** u. a. `btcedu/core/pipeline.py` (`_get_stages` Z. ~120–127,
  `_quality_gate_scoped`, `_profile_pipeline_flags`, `_imagegen_provider`),
  `btcedu/core/corrector.py` (Z. 180–187, Profil-Namespace),
  `btcedu/core/translation_qa.py` (~Z. 286–292),
  `btcedu/core/qa_reviewer.py` (Profilauflösung).
- **Problem:** Fehler bei Profilauflösung werden verschluckt und auf Defaults
  zurückgefallen. `_profile_pipeline_flags` fällt bei Ausnahme auf
  `(False, False)` zurück — d. h. `auto_publish=False` (sicher), aber
  `auto_approve_reviews=False`; `_get_stages` fällt bei Profilfehler auf die
  **vollen** v2-Stages zurück. Meist sicher, aber lautlos.
- **Auswirkung:** Anforderung 27 (keine stillen Fehler) teilweise; Debugging
  erschwert.
- **Korrektur:** Ausnahmen eng fassen und mindestens auf `WARNING` loggen
  (welches Profil, welcher Fehler).
- **Regressionstest:** Kaputtes Profil-YAML → Warnung geloggt, Pipeline nutzt
  dokumentierten Fallback.

#### F8 — Finding-Audit-Trail nur dateibasiert, nicht in der DB
- **Datei/Symbol:** `qa_reviewer.py` (`retry_history`/`history` im
  `translation_quality_gate.json`), keine neuen Migrationen.
- **Problem:** Historie/Provenienz der Findings lebt ausschließlich im
  Outputs-JSON. Das erfüllt „Finding-Historie bleibt erhalten" (die Historie
  wird korrekt fortgeschrieben, nicht überschrieben — verifiziert in
  `_merge_findings`), ist aber nicht DB-abfragbar und geht mit dem Outputs-Ordner
  verloren.
- **Auswirkung:** Anforderung „SQLite-Migrationen" für QA-Daten bewusst **nicht**
  umgesetzt (Design-Entscheidung: Artefakte statt Tabellen). Kein Datenverlust
  im Normalbetrieb, aber eingeschränkte Nachvollziehbarkeit/Reporting.
- **Korrektur:** Optional QA-Verdikt + Findings in eine schlanke Tabelle
  spiegeln (idempotente Migration), falls Reporting/Audit über die DB gefordert
  ist.
- **Regressionstest:** N/A (Design), sonst Migrations-Idempotenztest.

#### F9 — Hartkodierter Modellname in Prompt-Frontmatter
- **Datei/Symbol:** `btcedu/prompts/templates/correct_transcript.md:2`,
  `btcedu/prompts/templates/tagesschau_tr/correct_transcript.md:2`.
- **Problem:** Prompt-Metadaten nennen ein festes Claude-Modell. Die Laufzeit
  liest Modelle korrekt aus `settings`/Profil (verifiziert in
  `transcription_service.resolve_transcription_config` und `qa_reviewer`), aber
  das Frontmatter kann in die Irre führen bzw. in Provenienz einfließen.
- **Auswirkung:** Anforderung 6 (keine fest verdrahteten Modellnamen) in der
  Laufzeit erfüllt, in Metadaten kosmetisch verletzt.
- **Korrektur:** Frontmatter-Modell entweder entfernen oder aus Settings
  templaten.
- **Regressionstest:** N/A (kosmetisch); ggf. Assert, dass Provenienz das
  effektive Settings-Modell nennt.

---

### SUGGESTION

#### S1 — Türkische Morphologie nur teilweise abgedeckt
`_contains_turkish_variant` (Z. 926–934) entfernt Apostroph-Suffixe
(`Bitcoin'i`, `Almanya'da`) und deckt damit die häufigsten Flexionsfälle ab
(Anforderung 16 im Kern erfüllt). Reichere Suffixketten ohne Apostroph in
Mehrwort-Entitäten können jedoch False-Positive-`entity_suspicion` (nur
**minor**, blockiert nicht) erzeugen. Empfehlung: leichtgewichtiges
TR-Stemming/Token-Matching. Kein Blocker, da Severity minor.

#### S2 — `full`-Sekundärtranskriptionsmodus
`transcript_verifier.build_verification_regions()` (Z. 576–587) erzeugt bei
`mode="full"` eine Region über die **gesamte** Audiodatei. Das ist ein
**expliziter Opt-in** (Default `suspicious_segments_only`), und
`_validate_plan_limits` (max_secondary_audio_seconds/max_secondary_clips)
greift weiterhin, sodass ein zu langes Full-Clip sicher scheitert. Anforderung 8
(selektiv) ist damit erfüllt; Empfehlung: im Doc klarstellen, dass `full` ein
bewusster, kosten-/limitgebundener Ausnahmemodus ist.

#### S3 — `run_episode_pipeline`-Kostenaggregation per String-Parsing
Die Gesamtkosten werden aus `StageResult.detail` per `split("$")` geparst
(pipeline.py). Fragil, aber vorbestehendes Muster (`btcedu/core/CLAUDE.md`).
Empfehlung: Kosten als typisiertes Feld auf `StageResult` führen.

---

### ACCEPTED TRADE-OFF

- **AT1 — Keine neuen DB-Tabellen für QA/Transkript-Artefakte.** Bewusst
  dateibasiert (JSON in `outputs/`), konsistent mit dem bestehenden
  Artefakt-/Provenienz-Muster. Vermeidet Migrationen, erschwert aber DB-Reporting
  (siehe F8).
- **AT2 — `pipeline_lock` (flock) statt DB-Transaktions-Serialisierung.**
  Pragmatisch und wirksam gegen überlappende systemd-Timer-Läufe. Deckt jedoch
  nur `run_latest`/`run_pending` ab, nicht Einzel-Stage-Aufrufe außerhalb des
  Locks (siehe F5).
- **AT3 — `auto_approve_reviews=true` für tagesschau_tr.** Menschliche
  Review-Gates werden übersprungen; die einzige inhaltliche Kontrolle ist das
  GREEN-Quality-Gate + Narration-Lock. Vertretbar, weil `auto_publish=false`
  einen manuellen Upload-Schritt erzwingt.

---

## Verifizierte Stärken (besonders gut gelöst)

1. **Narration-Lock (`btcedu/core/narration_lock.py` + `chapterizer._enforce_narration_lock`).**
   Streng, fail-closed, mit eng definierter, dokumentierter Normalisierung
   (nur Unicode-NFKC, Quote-/Dash-Folding, Whitespace, Interpunktions-Spacing).
   Ziffern/Wörter/Namen/Reihenfolge werden **nicht** normalisiert. Der Lock wird
   **vor** dem Schreiben von `chapters.json` erzwungen und schreibt eine
   RED-Diagnose. `force` umgeht ihn nicht. Anforderung 20 vollständig erfüllt.
2. **Deterministische Critical-Findings sind LLM-fest.** In `_merge_findings`
   werden deterministische Findings immer mit `status="open"` übernommen; die
   LLM-„disputed"-Menge setzt nur ein `contradiction`-Flag (→ Eskalation), löscht
   aber nie ein deterministisches Finding. `_evaluate_decision` erzwingt bei
   offenem Critical RED. Anforderungen 12/17 vollständig erfüllt.
3. **Bounded Re-Runs mit persistenter Generation.** `resolve_translation_quality_gate`
   iteriert nur bei YELLOW und nur `generation < max_automatic_retries` (Default 2);
   `retry_generation` persistiert im Gate über Pipeline-Läufe hinweg. RED wird
   nicht automatisch wiederholt, sondern erzeugt ein artefaktgebundenes Review.
   Anforderung 18 erfüllt.
4. **Chapterize-Lock gegen nicht-grünes Gate** (`_enforce_translation_quality_gate`):
   verlangt GREEN **für den exakten aktuellen Narration-Hash** oder ein
   artefaktgebundenes approved `translation_qa`-Review; `force` umgeht es nicht.
5. **Selektive Zweittranskription mit korrekter Begrenzung.** Clips werden auf
   `[0, duration]` geclamped, Kontext-Sekunden addiert, Regionen gemerged;
   `_validate_plan_limits` **und** Cost-Guard laufen **vor** dem Provider-Aufruf
   und zusätzlich pro Region; `tempfile.TemporaryDirectory` wird verwendet.
   Anforderungen 8/9/10 (im Verifier) erfüllt.
6. **v1-Kompatibilität sauber gekapselt.** `_get_stages` entfernt für
   `pipeline_version != 2` genau die neuen v2-Only-Seitenstages;
   `_run_stage` wirft zusätzlich einen expliziten Fehler, falls eine v2-Only-Stage
   je für eine v1-Episode angefragt wird. Getestet auf Plan-Ebene. Anforderung 4
   erfüllt (Ausführungs-E2E fehlt, siehe F4).
7. **`auto_publish=false` hart durchgesetzt.** Der `publish`-Stage lädt bei
   `auto_publish=false` **nie** hoch, selbst nach approved Review — er verlangt
   einen expliziten manuellen Publish. Anforderung 22 erfüllt.

---

## Antworten auf die Leitfragen

**1. Ist die Implementierung produktionsreif?**
Bedingt. Für den **überwachten** Betrieb des tagesschau_tr-Profils
(`auto_publish=false`) ja. Für **vollautomatischen, unbeaufsichtigten** Betrieb
erst nach Behebung von F1 (Cost-Guard im `correct`), F2 (Zahlen-Lokalisierung),
F3 (Negation) und Schließen der E2E-Test-Lücke F4.

**2. Welche critical/major Findings existieren?**
Keine critical. Vier major: F1 (fehlender Cost-Guard in `correct`), F2
(lokalisierungs-blinde Zahlennormalisierung → False Positives/Negatives), F3
(verrauschte Negationsheuristik), F4 (keine echte E2E-Orchestrierung getestet).

**3. Welche Anforderungen wurden nur teilweise umgesetzt?**
- Cost Guard vor jedem bezahlten Aufruf (7): `correct` fehlt (F1).
- Zahlen-/Datumsformate DE/TR (14/15): Datum via Monatsnamen-Map korrekt,
  Zahlen-Lokalisierung fehlerhaft (F2).
- Türkische Flexion (16): Apostroph-Suffixe abgedeckt, reichere Morphologie
  nicht (S1) — unkritisch (minor Severity).
- SQLite-Migrationen (23): bewusst dateibasiert statt Tabellen (F8/AT1).
- Atomare kritische Schreibvorgänge (30): Gate ja, `chapters.json` nein (F6).
- Races bei parallelen systemd-Läufen (29): durch `pipeline_lock` gelöst, außer
  geteiltem Whisper-Temp-Pfad (F5).
- Tests prüfen echte Orchestrierung (25): überwiegend isoliert/tautologisch (F4).

**4. Welche Tests fehlen?**
- Echter End-to-End-Lauf durch `run_episode_pipeline`/`_run_stage` mit den neuen
  Stages (nur externe APIs gemockt).
- Echter v1-End-to-End-Lauf (nicht nur Plan-Vergleich).
- „RED/`cost_limit` blockiert Folge-Stages im echten Run".
- Cost-Guard-Enforcement für `correct` (existiert nicht, weil Guard fehlt — F1).
- Zahlen-Lokalisierungs-Tabellentests (F2), Negations-Regressionen (F3).
- Nicht-atomarer-Write-Test für `chapters.json` (F6), Whisper-Temp-Race (F5).

**5. Welche Architekturteile sind unnötig komplex?**
- `qa_reviewer.py` (1.945 Z.) und `translation_qa.py` (1.027 Z.) bündeln sehr
  viel Verantwortung; `_evaluate_decision` mischt konfigurierbare Regeln mit
  hartkodierten Kategoriemengen (`factual_deviation`, `date_mismatch`, …) — die
  Kategorielisten sollten datengetrieben/konfigurierbar sein.
- Die Kostenaggregation per `StageResult.detail`-String-Parsing (S3) ist fragil.
- Insgesamt aber **keine Parallelarchitektur** (Anforderung 2 erfüllt): alles
  fügt sich in `_V2_STAGES`/`_run_stage` und das bestehende Artefakt-Muster ein.

**6. Welche Teile sind besonders gut gelöst?**
Narration-Lock, LLM-Festigkeit deterministischer Criticals, gebundene Re-Runs,
Chapterize-Gate-Lock, die selektive/gebündelte/limitierte Zweittranskription mit
korrekter Temp-/Cost-Behandlung sowie die harte `auto_publish=false`-Durchsetzung
(siehe „Verifizierte Stärken").

---

## Empfohlene Reihenfolge der Nacharbeit
1. F1 (Cost-Guard `correct`) — Sicherheits-/Kostenrisiko, kleiner Fix.
2. F2 (Zahlen-Lokalisierung) — verhindert False-Positive-Blocks/Re-Runs.
3. F4 (echter E2E-Test) — sichert alle obigen Fixes gegen Regression ab.
4. F3 (Negation), F5/F6 (Temp/Atomarität), dann Minor/Suggestions.

*Es wurden im Rahmen dieses Reviews bewusst keine Code-Änderungen vorgenommen.*
