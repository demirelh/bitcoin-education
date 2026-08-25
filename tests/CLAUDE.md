# tests/ — Test Suite (~2655 tests)

## Running Tests

```bash
pytest                                # full suite
pytest tests/test_pipeline.py -x -q   # specific file, stop on first failure
pytest -k "test_render" -x            # match pattern
pytest --collect-only -q              # refresh the collection baseline
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

6. **Mock all external APIs**: never make real API calls. Mock `call_claude`, `DallE3ImageService`, `ElevenLabsService`, `GeminiImageService`, `YouTubeDataAPIService`, etc.

7. **Failover tests are local only**: use the Flask control-plane test client,
fake HTTP responses and temporary SQLite files. Never use production node or
operator tokens and never contact the live control plane.

8. **TTS has no reusable take cache**: regression tests must expect identical
text (including intro/outro) to trigger fresh provider calls. Keep provider
chunks at or below 750 characters and charge retries per successful call.

9. **Full-suite shared state**: background web jobs are asynchronous. Wait for
terminal job state and cleanly isolate DB/app fixtures; a failure seen only
after thousands of tests but passing alone may be leaked suite state, not a
reason to weaken the production behavior.

10. **Endpoint acceptance tests**: tests that only assert a background action
returns `202` must stub `JobManager.submit()` so synthetic episode URLs cannot
reach yt-dlp or any other external provider. Exercise real submission only in
job lifecycle tests, with every stage dependency mocked and the job awaited.

## Test File Organization

- `test_<module>.py` — maps to `btcedu/core/<module>.py` or `btcedu/services/<module>.py`
- `test_web*.py` / `test_*_api.py` — Flask test client tests for web endpoints
- `test_failover_*.py` — control-plane auth, mode/health policy, lease CAS,
  fencing, reconciliation, pipeline gating and dashboard proxy tests
- `test_tts_no_cache.py` — fresh synthesis regression coverage
- `conftest.py` — shared fixtures (db, episodes, transcripts)
- `fixtures/` — static test data files

<!--
Documentation sync
Baseline: d1b4676
Synced through: current working tree
Date: 2026-08-22
-->
