# OpenCode + Oh My OpenCode: Planner (Opus-like) vs Implementer (Sonnet-like)

## TL;DR
Configure OpenCode/Oh-My-OpenCode so **planning/review** uses a large Codex model and **implementation** uses a cheaper mini Codex model. Apply changes **globally** (user config) while keeping repo-level `opencode.json` aligned to avoid precedence surprises.

**Chosen models (confirmed available via `opencode models openai`)**
- **Planner (Prometheus / Opus-like)**: `openai/gpt-5.2-codex`
- **Implementer (Sisyphus / Sonnet-like)**: `openai/gpt-5.1-codex-mini`

**Primary files to change**
- Global OMO config: `/home/pi/.config/opencode/oh-my-opencode.json`
- Project OpenCode config: `/home/pi/AI-Startup-Lab/bitcoin-education/opencode.json`

---

## Context

### Original Request (DE)
- Rollen-Trennung wie im Claude-Setup:
  - Opus 4.6 = Planner/Architect (Architektur, Debug-Strategie, präzise Schritt-für-Schritt-Pläne, Reviews)
  - Sonnet = Implementer (setzt Schritte um, schreibt Code, führt Tests/Lint/Build aus, hält sich strikt an Plan)
- OpenCode + Oh My OpenCode (oh-my-opencode) soll diese Rollen sauber abbilden.

### Observations (local)
- `opencode 1.2.6` ist installiert.
- `opencode models` listet verfügbare Provider/Modelle; `openai/gpt-5.2-codex` und `openai/gpt-5.1-codex-mini` sind verfügbar.
- Plugin ist aktiv über globales `/home/pi/.config/opencode/opencode.json` (`oh-my-opencode@latest`).
- OMO config liegt global unter `/home/pi/.config/opencode/oh-my-opencode.json`.
- Repo hat bereits `opencode.json` (project precedence!).

### Key Decisions
- Scope: **Global** (user config), nicht projekt-local.
- Planner model: `openai/gpt-5.2-codex`.
- Implementer model: `openai/gpt-5.1-codex-mini`.

---

## Scope Boundaries

### IN
- Änderungen **nur** an OpenCode/OMO Konfigurationsdateien.
- Verifikation per `opencode debug ...` und minimalen, nicht-destruktiven Checks.

### OUT
- Keine Änderungen am Anwendungscode.
- Keine Secrets ausgeben oder in Logs drucken.
- Keine destruktiven Shell-Kommandos ohne explizite Rückfrage.

---

## Verification Strategy

### Evidence / Commands (agent-executable)
- `opencode debug paths`
- `opencode debug config`
- `opencode debug agent "Prometheus (Plan Builder)"`
- `opencode debug agent "Sisyphus (Ultraworker)"`

### Success Signals
- Prometheus resolved model shows `openai/gpt-5.2-codex`.
- Sisyphus resolved model shows `openai/gpt-5.1-codex-mini`.
- `opencode debug config` shows plugin loaded and no unexpected overrides.

---

## Execution Strategy

### Wave 1 (parallel — config edits)
- Task 1: Align repo `opencode.json` defaults (model + small_model)
- Task 2: Apply global OMO role overrides (Prometheus + Sisyphus models)
- Task 3: Precedence sanity check (ensure no project-level `.opencode/oh-my-opencode.*` override)

### Wave 2 (parallel — verification)
- Task 4: Verify resolved config + agent models via `opencode debug ...`
- Task 5: Smoke-check agent selection workflow (non-destructive)

---

## TODOs

- [ ] 1. Align repo OpenCode defaults (`opencode.json`)

  **What to do**:
  - Edit `/home/pi/AI-Startup-Lab/bitcoin-education/opencode.json`:
    - Set `model` → `openai/gpt-5.2-codex`
    - Set `small_model` → `openai/gpt-5.1-codex-mini`
    - Keep `$schema` unchanged

  **Intended final file (exact)**:
  ```json
  {
    "$schema": "https://opencode.ai/config.json",
    "model": "openai/gpt-5.2-codex",
    "small_model": "openai/gpt-5.1-codex-mini"
  }
  ```

  **References**:
  - `/home/pi/AI-Startup-Lab/bitcoin-education/opencode.json` (current defaults)
  - `opencode models openai` confirms both models exist in this installation

  **Acceptance Criteria (agent-executable)**:
  - [ ] File content matches the intended JSON (see Task 1 QA evidence)
  - [ ] From repo root: `opencode debug config` shows:
    - top-level `model` resolves to `openai/gpt-5.2-codex`
    - top-level `small_model` resolves to `openai/gpt-5.1-codex-mini`

  **QA Scenarios**:
  ```
  Scenario: Repo defaults resolve correctly
    Tool: Bash
    Steps:
      1. Run: opencode debug config
      2. Assert output contains: "model": "openai/gpt-5.2-codex"
      3. Assert output contains: "small_model": "openai/gpt-5.1-codex-mini"
    Evidence: .sisyphus/evidence/task-1-debug-config.txt
  ```

- [ ] 2. Apply global Oh-My-OpenCode role overrides (Prometheus + Sisyphus)

  **What to do**:
  - Edit `/home/pi/.config/opencode/oh-my-opencode.json`:
    - Update `agents.prometheus.model` → `openai/gpt-5.2-codex`
    - Add `agents.sisyphus.model` → `openai/gpt-5.1-codex-mini`
    - Preserve all other existing agent/category settings unless explicitly desired

  **Patch intent (minimal diff)**:
  - Change:
    - `agents.prometheus.model`: `openai/gpt-5.2` → `openai/gpt-5.2-codex`
  - Add:
    - `agents.sisyphus`: `{ "model": "openai/gpt-5.1-codex-mini" }`

  **Guardrails**:
  - Do NOT remove existing agents/categories.
  - Do NOT add new providers.
  - Do NOT print or paste any secrets.

  **References**:
  - `/home/pi/.config/opencode/oh-my-opencode.json` (current global OMO config)
  - OMO schema (agent keys include `sisyphus` + `prometheus`):
    - https://raw.githubusercontent.com/code-yeongyu/oh-my-opencode/master/assets/oh-my-opencode.schema.json

  **Acceptance Criteria (agent-executable)**:
  - [ ] `opencode debug agent "Prometheus (Plan Builder)"` resolves model providerID/modelID to `openai` / `gpt-5.2-codex`
  - [ ] `opencode debug agent "Sisyphus (Ultraworker)"` resolves model providerID/modelID to `openai` / `gpt-5.1-codex-mini`

  **QA Scenarios**:
  ```
  Scenario: Prometheus uses planner model
    Tool: Bash
    Steps:
      1. Run: opencode debug agent "Prometheus (Plan Builder)"
      2. Assert JSON includes providerID "openai" and modelID "gpt-5.2-codex"
    Evidence: .sisyphus/evidence/task-2-prometheus-agent.json

  Scenario: Sisyphus uses implementer model
    Tool: Bash
    Steps:
      1. Run: opencode debug agent "Sisyphus (Ultraworker)"
      2. Assert JSON includes providerID "openai" and modelID "gpt-5.1-codex-mini"
    Evidence: .sisyphus/evidence/task-2-sisyphus-agent.json
  ```

- [ ] 3. Precedence sanity check (ensure no project-level OMO override)

  **What to do**:
  - From repo root, confirm there is no:
    - `/home/pi/AI-Startup-Lab/bitcoin-education/.opencode/oh-my-opencode.json`
    - `/home/pi/AI-Startup-Lab/bitcoin-education/.opencode/oh-my-opencode.jsonc`
  - If either exists, it will override global settings; align it to the same model choices or remove it (only after explicit user approval).

  **Acceptance Criteria (agent-executable)**:
  - [ ] No project-level `.opencode/oh-my-opencode.*` override exists OR it is aligned to the same model choices.

  **QA Scenarios**:
  ```
  Scenario: No project-level OMO override
    Tool: Bash
    Steps:
      1. Run: ls -la .opencode  (if directory exists)
      2. Assert: oh-my-opencode.json/jsonc not present OR contents align
    Evidence: .sisyphus/evidence/task-3-opencode-dir.txt
  ```

- [ ] 4. Verify resolved configuration end-to-end (no hidden overrides)

  **What to do**:
  - From repo root, capture resolved configuration and confirm the following:
    - Plugin list contains `oh-my-opencode@latest`
    - `model` resolves to `openai/gpt-5.2-codex`
    - `small_model` resolves to `openai/gpt-5.1-codex-mini`
    - Prometheus + Sisyphus agent models resolve as intended (Tasks 2 criteria)
  - Capture provider/model availability evidence (OpenAI list):
    - `opencode models openai`

  **Acceptance Criteria (agent-executable)**:
  - [ ] `opencode debug config` contains the expected plugin + model values.
  - [ ] Agent debug outputs match expected providerID/modelID pairs.

  **QA Scenarios**:
  ```
  Scenario: Resolved config matches expectations
    Tool: Bash
    Steps:
      1. Run: opencode debug config
      2. Assert: plugin includes "oh-my-opencode@latest"
      3. Assert: model == "openai/gpt-5.2-codex"
      4. Assert: small_model == "openai/gpt-5.1-codex-mini"
    Evidence: .sisyphus/evidence/task-4-debug-config.json

  Scenario: Chosen models are listed as available
    Tool: Bash
    Steps:
      1. Run: opencode models openai
      2. Assert output contains: openai/gpt-5.2-codex
      3. Assert output contains: openai/gpt-5.1-codex-mini
    Evidence: .sisyphus/evidence/task-4-models-openai.txt
  ```

- [ ] 5. Verify quick Planner↔Implementer workflow (no TUI required)

  **What to do**:
  - Confirm the user can invoke roles explicitly using `--agent` and that the resolved model matches:
    - Planner: `--agent "Prometheus (Plan Builder)"`
    - Implementer: `--agent "Sisyphus (Ultraworker)"`
  - Use non-destructive prompts that do not require repo writes.

  **Acceptance Criteria (agent-executable)**:
  - [ ] `opencode debug agent "Prometheus (Plan Builder)"` shows planner model.
  - [ ] `opencode debug agent "Sisyphus (Ultraworker)"` shows implementer model.

  **QA Scenarios**:
  ```
  Scenario: Planner agent is selectable and mapped
    Tool: Bash
    Steps:
      1. Run: opencode debug agent "Prometheus (Plan Builder)"
      2. Assert: modelID == "gpt-5.2-codex"
    Evidence: .sisyphus/evidence/task-5-planner-mapped.json

  Scenario: Implementer agent is selectable and mapped
    Tool: Bash
    Steps:
      1. Run: opencode debug agent "Sisyphus (Ultraworker)"
      2. Assert: modelID == "gpt-5.1-codex-mini"
    Evidence: .sisyphus/evidence/task-5-implementer-mapped.json
  ```

---

## Final Verification Wave
- Re-run all verification commands from a clean shell in repo root.

---

## Commit Strategy
- No commits unless explicitly requested (config-only change).

---

## Success Criteria
- Planner vs Implementer are cleanly separated by model.
- Oh My OpenCode agents use the intended models.
- No code changes outside configuration.
