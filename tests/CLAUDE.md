# tests/ — Test Suite (~867 tests)

## Running Tests

```bash
pytest                                # full suite
pytest tests/test_pipeline.py -x -q   # specific file, stop on first failure
pytest -k "test_render" -x            # match pattern
```

## Core Fixtures (conftest.py)

- `db_engine` — in-memory SQLite with FTS5, creates all tables from `btcedu.db.Base`
- `db_session` — scoped session from `db_engine`, auto-rolls back after each test
- `chunked_episode` — Episode at CHUNKED status with chunks + FTS entries
- `SAMPLE_TRANSCRIPT` — loaded from `tests/fixtures/sample_transcript_de.txt`

## Critical Test Gotchas

1. **MediaAsset separate Base**: tests for MediaAsset must also call `MediaBase.metadata.create_all(engine)` — it uses its own `declarative_base()`, not `btcedu.db.Base`

2. **Import PromptVersion at module level**: if a test uses PromptRegistry, import `from btcedu.models.prompt_version import PromptVersion  # noqa: F401` at the top so the table exists when `db_engine` calls `create_all()`

3. **pydub + Python 3.13**: `audioop` removed. Mock with `patch.dict(sys.modules, {"pydub": mock_pydub})`

4. **Lazy imports and mock targets**: functions lazy-imported inside other functions (e.g., `normalize_video_clip` inside `finalize_selections()`) must be patched at the **source module** (`btcedu.services.ffmpeg_service.normalize_video_clip`), not at the calling module

5. **autouse fixtures**: some test files have `autouse=True` fixtures that patch functions for all tests in the file. If you need the real function, put your test in a separate file (see `test_intent_extract_registry.py` — separated from `test_stock_ranking.py`)

6. **Mock all external APIs**: never make real API calls. Mock `call_claude`, `DallE3ImageService`, `ElevenLabsService`, `YouTubeDataAPIService`, etc.

## Test File Organization

- `test_<module>.py` — maps to `btcedu/core/<module>.py` or `btcedu/services/<module>.py`
- `test_web*.py` / `test_*_api.py` — Flask test client tests for web endpoints
- `conftest.py` — shared fixtures (db, episodes, transcripts)
- `fixtures/` — static test data files
