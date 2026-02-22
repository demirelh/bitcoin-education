# Draft: OpenCode/Oh My OpenCode – Claude-Rollen (Opus=Planner, Sonnet=Implementer)

## Requirements (confirmed)
- Ziel: OpenCode so konfigurieren, dass es wie bisheriges Claude-Setup arbeitet.
- Rollen:
  - „Opus 4.6“ = Planner/Architect (Planung/Architektur, Debug-Strategie, präzise Step-by-step-Pläne, Reviews)
  - „Sonnet“ = Implementierer (setzt Plan um, schreibt Code, führt Tests/Lint/Build aus)
- Nutzt: OpenCode + Oh My OpenCode (oh-my-opencode). Presets/Agents/Workflows berücksichtigen.
- Aufgaben:
  1) Verfügbare Provider + Modellnamen in dieser Installation prüfen (insb. OpenAI/Codex).
  2) Kosteneffiziente Modell-Kombi wählen (Planner=Top-Qualität, Implementer=günstig/schnell).
  3) Config so setzen: `model`=Planner, `small_model`=Implementer; wenn möglich Safety Defaults.
  4) Config an korrektem Pfad schreiben: bevorzugt `<repo>/opencode.json`, sonst globale Config.
  5) Kurz prüfen, dass Oh My OpenCode neue Modelle übernimmt (Agent/Variant Auswahl).
  6) Am Ende ausgeben: Pfad, Modellnamen, komplettes JSON, kurze Bedien-Anleitung.
- Randbedingungen/Fallbacks:
  - Wenn `gpt-5-mini` nicht verfügbar → `gpt-5.1-codex-mini` oder beste Mini-Alternative.
  - Wenn `gpt-5.2/5.3` nicht verfügbar → `gpt-5.1-codex` oder beste High-End-Alternative.
- Guardrail: Nichts am Codebase ändern außer Konfiguration.

## Technical Decisions (tentative)
- **Scope choice**: Global OMO overrides (User entschied: global statt projekt-local).
- **Planner model**: `openai/gpt-5.2-codex`.
- **Implementer model**: `openai/gpt-5.1-codex-mini`.

## Requirements (confirmed) — Decisions
- Rollen-Trennung soll über OMO-Agenten erfolgen:
  - Prometheus (Planner) → `openai/gpt-5.2-codex`
  - Sisyphus (Implementer) → `openai/gpt-5.1-codex-mini`

## Research Findings
- Repo hat Projekt-Config: `<repo>/opencode.json` (aktuell: `model=openai/gpt-5.2`, `small_model=openai/gpt-5-mini`).
- OpenCode CLI ist installiert: `opencode 1.2.6`.
- OpenCode kennt eine Modell-Liste per `opencode models`.
- Globaler OpenCode-Config-Pfad (opencode debug paths):
  - Config: `/home/pi/.config/opencode`
  - Data: `/home/pi/.local/share/opencode`
- Globaler OpenCode-Plugin-Load ist aktiv:
  - `/home/pi/.config/opencode/opencode.json` lädt `oh-my-opencode@latest`.
  - `/home/pi/.config/opencode/oh-my-opencode.json` setzt OMO-Agent-Modelle (u.a. `prometheus`, `metis`, `momus`, `oracle`) derzeit auf `openai/gpt-5.2` und `hephaestus` auf `openai/gpt-5.3-codex`.
- Verfügbare OpenAI-Modelle (Auszug aus `opencode models openai`):
  - `openai/gpt-5.2`, `openai/gpt-5.2-codex`, `openai/gpt-5-mini`, `openai/gpt-5.1-codex-mini`, `openai/gpt-5.3-codex`, `openai/o3(-pro/-mini)`, `openai/o4-mini`, ...

## Open Questions
- Existiert bereits `<repo>/opencode.json` und soll diese angepasst werden (oder nur global)?
- Welche Provider sind aktuell eingerichtet (API keys vorhanden)?
- Welche oh-my-opencode Presets/Agents nutzt du konkret (Namen/Dateien), falls mehrere?

## Notes
- OMO Schema (GitHub raw) zeigt, dass `oh-my-opencode.json` explizit `agents.sisyphus` und `agents.prometheus` konfigurieren kann (Model/Variant/etc.).

## Scope Boundaries
- INCLUDE: Nur OpenCode/oh-my-opencode Konfiguration.
- EXCLUDE: Code-Änderungen außerhalb von Config.
