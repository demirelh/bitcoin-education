# btcedu — Bitcoin Education Video Pipeline

Automated pipeline: German Bitcoin podcast episodes -> Turkish YouTube videos.
Stack: Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings.
Deployed on Raspberry Pi via systemd timers + Caddy reverse proxy.
Entry point: `btcedu = "btcedu.cli:cli"` (pyproject.toml). All 11 sprints complete.

## Build & Test

```bash
pip install -e ".[dev,web]"          # install with dev + web deps
pip install -e ".[youtube]"          # optional YouTube upload deps
pytest                               # run full test suite (~867 tests)
pytest tests/test_pipeline.py -x -q  # run specific test file
ruff check btcedu/ tests/            # lint (py312, line-length 100, E/W/F/I/UP)
./run.sh                             # production deploy: git pull → pip → migrate → restart
btcedu smoke-test-video --keep       # verify ffmpeg on Pi
```

## Pipeline Architecture

Two pipeline versions coexist. `pipeline_version` config (1=v1, 2=v2) controls which runs.

**v1 (legacy):** NEW -> DOWNLOADED -> TRANSCRIBED -> CHUNKED -> GENERATED -> REFINED -> COMPLETED
**v2 (current):**
```
NEW -> DOWNLOADED -> TRANSCRIBED -> CORRECTED -> [review_gate_1] ->
TRANSLATED -> ADAPTED -> [review_gate_2] -> CHAPTERIZED ->
IMAGES_GENERATED -> TTS_DONE -> RENDERED -> [review_gate_3] -> APPROVED -> PUBLISHED
```

Review gates block the pipeline, create ReviewTask records, and resume on approval.
All v2-only stages are guarded in `_run_stage()` — v1 episodes cannot enter v2 stages.

## Directory Layout

- `btcedu/core/` — pipeline orchestration + all v2 stage modules (see `core/CLAUDE.md`)
- `btcedu/models/` — SQLAlchemy 2.0 models + Pydantic schemas (see `models/CLAUDE.md`)
- `btcedu/services/` — external API wrappers with Protocols (see `services/CLAUDE.md`)
- `btcedu/web/` — Flask SPA dashboard + REST API (see `web/CLAUDE.md`)
- `btcedu/prompts/` — v1 Python builders + v2 Jinja2 templates (see `prompts/CLAUDE.md`)
- `btcedu/migrations/` — 7 migrations in `__init__.py`, check-before-act idempotency
- `tests/` — 48 test files, in-memory SQLite fixtures (see `tests/CLAUDE.md`)
- `deploy/` — systemd units, Caddy config, `setup-web.sh`
- `data/` — runtime data (.gitignored): audio, transcripts, outputs, DB

## Coding Conventions

- Python 3.12, ruff-enforced (line-length 100, select E/W/F/I/UP, ignore UP042)
- SQLAlchemy 2.0: `Mapped[]` / `mapped_column()` with `Base` from `btcedu/db.py`
- Datetime: `datetime.now(UTC)` via `_utcnow()` helper in each model file
- Stage functions: `generate_X(session, episode_id, settings, force=False)` -> result dataclass
- All external APIs mocked in tests (no real API calls)
- Migrations: abstract `Migration` class, `MIGRATIONS` list, check-before-act idempotency

## Key Design Patterns

1. **Stage pattern**: service layer (Protocol) -> core module (orchestration) -> idempotency (SHA-256 hash + provenance) -> cost guard -> dry-run -> PipelineRun record
2. **Cascade invalidation**: upstream changes write `.stale` markers downstream
3. **Review gates**: `has_approved_review()` / `has_pending_review()` -> pause pipeline
4. **Prompt versioning**: YAML frontmatter + Jinja2 templates, PromptRegistry tracks SHA-256 hashes in DB
5. **Cost guard**: cumulative episode cost vs `max_episode_cost_usd` ($10 default)

## Critical Gotchas

- **MediaAsset uses own Base**: `declarative_base()` in `media_asset.py`, NOT `btcedu.db.Base`. Tests must `create_all()` both metadata sets separately.
- **pydub + Python 3.13**: `audioop` removed. Tests mock via `sys.modules`. Production needs `pyaudioop`.
- **Chapter.visual is singular** (`Visual`), not a list. Narration has `.text`, `.word_count`, `.estimated_duration_seconds`.
- **Lazy imports**: stage functions lazy-imported in `_run_stage()` to avoid circular deps.
- **YouTube deps are optional**: `pip install -e ".[youtube]"`. `run.sh` auto-installs if `data/client_secret.json` exists.
- **SQLAlchemy string relationships** (e.g. `"ReviewItemDecision"`) require the target module to be imported at runtime, not just under `TYPE_CHECKING`.
- **Raspberry Pi**: ffmpeg uses software encoding (slow). Config: `RENDER_PRESET=ultrafast`, `RENDER_TIMEOUT_SEGMENT=900`.

## Config (.env)

Key settings: `anthropic_api_key`, `openai_api_key`, `elevenlabs_api_key`, `database_url` (default: sqlite:///data/btcedu.db), `pipeline_version` (1 or 2), `dry_run`, `max_episode_cost_usd` ($10), `render_preset`, `render_timeout_segment`. Full list in `btcedu/config.py`.

## Reference Documents

- `MASTERPLAN.md` — full architecture, schemas, phase roadmap (66KB)
- `docs/architecture/` — pipeline, review gate, visual asset, render flow docs
- `docs/decisions/` — ADRs for key design choices
- `docs/runbooks/` — operator guides for common tasks
- `docs/sprints/` — per-sprint plan/implement/validate docs (all 11 sprints)
