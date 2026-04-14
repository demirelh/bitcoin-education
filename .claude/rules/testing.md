---
paths:
  - tests/**
  - btcedu/**
---

# Testing Rules

- Run: `pytest` (full suite ~1189 tests), `pytest tests/test_X.py -x -q` (specific file)
- All external APIs must be mocked — no real API calls in tests
- Mock at the **source module** for lazy imports (e.g., `btcedu.services.ffmpeg_service.normalize_video_clip`), not at the calling module
- MediaAsset uses its own `declarative_base()` — tests must call `MediaBase.metadata.create_all(engine)` separately from `btcedu.db.Base.metadata.create_all(engine)`
- If a test uses PromptRegistry, import `from btcedu.models.prompt_version import PromptVersion  # noqa: F401` at module level so the table is created
- pydub + Python 3.13: `audioop` removed. Mock with `patch.dict(sys.modules, {"pydub": mock_pydub})`
- Some test files have `autouse=True` fixtures that patch globally — if you need the real function, put your test in a separate file
