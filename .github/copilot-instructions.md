# btcedu — Copilot Instructions

Condensed companion to `CLAUDE.md`. The per-directory `CLAUDE.md` files
(`btcedu/core/`, `btcedu/models/`, `btcedu/services/`, `btcedu/web/`,
`btcedu/prompts/`, `tests/`) remain authoritative for details — do not duplicate
them here.

## Purpose

Automated pipeline that turns German podcast/news episodes (currently ARD
tagesschau) into Turkish YouTube videos. Python 3.12, Click CLI, Flask
dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings. Runs on a
Raspberry Pi via systemd timers behind a Caddy reverse proxy.
Entry point: `btcedu = "btcedu.cli:cli"`.

## Repository Structure

- `btcedu/core/` — pipeline stages and orchestration (`pipeline.py`), plus
  `weather/` (deterministic weather visuals), `final_review.py`,
  `narration_lock.py`, `retention.py`, `regression_runner.py`
- `btcedu/services/` — external API wrappers behind Protocols
- `btcedu/models/` — SQLAlchemy models and Pydantic schemas
- `btcedu/profiles/` — profile YAML: stage routing, providers, gates
- `btcedu/prompts/` — versioned templates (YAML frontmatter + Jinja2)
- `btcedu/web/` — Flask dashboard (`api.py`, `static/app.js`)
- `tests/` — pytest suite (~1955 tests)
- `docs/implementation/` — implementation notes and audits

## Pipeline (v2)

```
download → transcribe → transcript_analyze → transcript_verify → correct →
transcript_qa → review_gate_transcript_qa → review_gate_1 → segment →
translate → adapt → review_gate_2 → chapterize → frameextract → imagegen →
review_gate_stock → tts → anchorgen → render → review_gate_3 → publish
```

v1 is legacy: stored v1 episodes keep a compatibility path, new v1 profiles are
not supported. All v2-only stages are guarded in `_run_stage()`.

## Coding Rules

- ruff-enforced: line length 100, select E/W/F/I/UP, ignore UP042
- SQLAlchemy 2.0 `Mapped[]` / `mapped_column()` with `Base` from `btcedu/db.py`
  (**exception**: `MediaAsset` uses its own `declarative_base()`)
- Timestamps via `_utcnow()` → `datetime.now(UTC)`
- Stage signature: `generate_X(session, episode_id, settings, force=False)` →
  result dataclass
- Stage functions are lazy-imported in `_run_stage()` to avoid circular imports
- Migrations: abstract `Migration` class, `MIGRATIONS` list, check-before-act
- Comment only what needs clarification; keep changes surgical

## Testing

- `pytest` (full), `pytest tests/test_pipeline.py -x -q` (targeted)
- **Never** call a real external API in a test — mock every provider
- `MediaAsset` tests must `create_all()` both metadata sets
- Lazy-imported functions must be patched at their **source** module
- `btcedu regression-run` replays recent episodes against a cloned DB and
  copied outputs; it never touches production data

## Models & Providers

Provider choice is profile-owned, not hardcoded. LLM calls go through
`services/claude_service.call_claude()` (providers: `anthropic`, `openai`,
`github_models`, `copilot_cli`). Images may route among Flux, Ideogram,
DALL-E, Gemini or Pexels; TTS uses ElevenLabs; weather cards are rendered
deterministically (HTML/SVG + headless Chromium), never by an image model.

## Configuration

`.env` → `btcedu/config.py` (`Settings`, pydantic-settings). Profile YAML owns
stage routing and may override applicable `.env` values. Never invent settings —
verify against `btcedu/config.py` and the profile file.

## Idempotency

Every stage: SHA-256 content hash + provenance file, `.stale` markers for
downstream invalidation, partial recovery (skip unchanged chapters),
`PipelineRun` / `ContentArtifact` / `MediaAsset` records.
`render --force` still skips segments whose picture and audio are unchanged;
changed render settings invalidate them automatically via
`render/segments/.render_settings`.

## Review Gates

`has_approved_review()` / `has_pending_review()` pause the pipeline; gates
resume only when the **current** artifacts are approved, superseded reviews get
`ReviewStatus.SUPERSEDED`. `review_gate_transcript_qa` and `review_gate_2` can
be adjudicated automatically by an independent model when the profile enables
it. `review_gate_3` runs deterministic weather video checks and is fail-closed.

## Cost Guard

Cumulative episode cost is checked against `max_episode_cost_usd` before paid
calls. `settings.dry_run` must produce placeholders instead of API calls.

## External APIs & Secrets

Credentials come from `.env` / environment only. **Never** commit secrets, keys,
tokens or `auth/` session data, and never print them in logs or output.
Two safeguards back this up: `Settings` masks credential fields in its own
`repr`, and `utils/secrets.install_log_redaction()` strikes the configured
values out of every log record — needed because a third-party library may
quote a rejected key back in its own error text.
External data that is not part of the approved narration (e.g. Open-Meteo
temperatures) must be attributed and excluded from claim validation.

## Ground Rules

- Do not hallucinate modules, CLI commands, settings or endpoints — verify them
  in the code before writing about them.
- Respect existing files and conventions; prefer minimal, targeted edits over
  rewrites or repository-wide modernization.
- Do not create parallel documentation or new agent-instruction systems.

<!--
Documentation sync
Baseline: 1d7291b
Synced through: HEAD
Date: 2026-08-04
-->
