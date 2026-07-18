# btcedu — Bitcoin Education Video Pipeline

Automated pipeline: German Bitcoin podcast episodes → Turkish YouTube videos.
Stack: Python 3.12, Click CLI, Flask web dashboard, SQLAlchemy 2.0 + SQLite + FTS5, Pydantic settings.
Deployed on Raspberry Pi via systemd timers + Caddy reverse proxy.
Entry point: `btcedu = "btcedu.cli:cli"` (pyproject.toml). All 11 sprints complete.

## Build & Test

```bash
pip install -e ".[dev,web]"          # install with dev + web deps
pip install -e ".[youtube]"          # optional YouTube upload deps
pytest                               # run full test suite
pytest tests/test_pipeline.py -x -q  # run specific test file
ruff check btcedu/ tests/            # lint (py312, line-length 100, E/W/F/I/UP)
./run.sh                             # production deploy: git pull → pip → migrate → restart
```

## Pipeline Architecture

New profiles use v2. Existing stored v1 episodes retain a compatibility path;
new v1 profile definitions are not supported.

**v1 (legacy):** NEW → DOWNLOADED → TRANSCRIBED → CHUNKED → GENERATED → REFINED → COMPLETED
**v2 (current):**
```
download → transcribe → transcript_analyze → transcript_verify → correct →
transcript_qa → review_gate_transcript_qa → review_gate_1 → segment →
translate → adapt → review_gate_2/translation quality gate → chapterize →
frameextract → imagegen → review_gate_stock → tts → anchorgen → render →
review_gate_3 → publish
```

Translation QA runs deterministic checks before an independent LLM cascade.
GREEN stores the approved narration hash; RED blocks chapterize and creates an
artifact-bound review. Automatic targeted translate/adapt retries are bounded.
Chapterize must preserve the approved narration modulo explicit technical
normalization. Review gates resume only when their current artifacts are approved.
All v2-only stages are guarded in `_run_stage()` — v1 episodes cannot enter v2 stages.

**imagegen dispatch**: provider and fallback are profile-owned.
`tagesschau_tr` uses generative routing with deterministic rendering for exact
data; other profiles may route among Flux, Ideogram, DALL-E, Gemini, or Pexels.

## Coding Conventions

- Python 3.12, ruff-enforced (line-length 100, select E/W/F/I/UP, ignore UP042)
- SQLAlchemy 2.0: `Mapped[]` / `mapped_column()` with `Base` from `btcedu/db.py`
- Datetime: `datetime.now(UTC)` via `_utcnow()` helper in each model file
- Stage functions: `generate_X(session, episode_id, settings, force=False)` → result dataclass
- All external APIs mocked in tests (no real API calls)
- Migrations: abstract `Migration` class, `MIGRATIONS` list, check-before-act idempotency

## Key Design Patterns

1. **Stage pattern**: service layer (Protocol) → core module (orchestration) → idempotency (SHA-256 hash + provenance) → cost guard → dry-run → PipelineRun record
2. **Cascade invalidation**: upstream changes write selective `.stale` markers downstream
3. **Review gates**: `has_approved_review()` / `has_pending_review()` → pause pipeline
4. **Prompt versioning**: YAML frontmatter + Jinja2 templates, PromptRegistry tracks SHA-256 hashes in DB
5. **Cost guard**: cumulative episode cost vs `max_episode_cost_usd`

## Critical Gotchas

- **MediaAsset uses own Base**: `declarative_base()` in `media_asset.py`, NOT `btcedu.db.Base`. Tests must `create_all()` both metadata sets separately.
- **pydub + Python 3.13**: `audioop` removed. Tests mock via `sys.modules`. Production needs `pyaudioop`.
- **Chapter.visual is singular** (`Visual`), not a list. Narration has `.text`, `.word_count`, `.estimated_duration_seconds`.
- **Lazy imports**: stage functions lazy-imported in `_run_stage()` to avoid circular deps.
- **YouTube deps are optional**: `pip install -e ".[youtube]"`. `run.sh` auto-installs if `data/client_secret.json` exists.
- **SQLAlchemy string relationships** (e.g. `"ReviewItemDecision"`) require the target module to be imported at runtime, not just under `TYPE_CHECKING`.
- **Raspberry Pi**: ffmpeg uses software encoding (slow). Config: `RENDER_PRESET=ultrafast`, `RENDER_TIMEOUT_SEGMENT=900`.

## Config (.env)

Key settings: transcription primary/secondary providers, transcript QA
thresholds, `qa_review_enabled`, `qa_model`, LLM/provider credentials,
`default_content_profile`, `dry_run`, `max_episode_cost_usd`, image/TTS/render
providers, and YouTube OAuth paths. Profile YAML owns stage routing and may
override applicable `.env` values. Full list: `btcedu/config.py`.
