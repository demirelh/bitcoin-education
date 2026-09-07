from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.dead_letter import DeadLetterEntry  # noqa: F401 — register table

FIXTURES = Path(__file__).parent / "fixtures"

# Keep tests isolated from local .env files that may contain removed legacy
# settings keys.
Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _no_real_recogniser_in_tests(monkeypatch):
    """Keep the stutter check from loading a real Whisper model.

    Left on, tests synthesising fake audio load `small` and transcribe silence,
    which the recogniser answers with its stock hallucination ("Altyazı M.K.").
    That is not a verdict about anything the test wrote, but it is a rejection,
    so the take is retried three times and the assertions count six requests
    where they expect two — after several seconds of model loading per test.
    A test that means to exercise the check passes its own model in.
    """
    monkeypatch.setenv("TTS_STUTTER_CHECK_ENABLED", "false")
    yield


@pytest.fixture(autouse=True)
def _dashboard_auth_off_unless_a_test_asks_for_it(monkeypatch):
    """Dashboard authentication defaults to ON; tests opt out here, once.

    The alternative — logging in inside every one of the fourteen fixtures that
    build the app — would put a login into tests about render modes and QA
    findings, and the first awkward one would be "fixed" by disabling auth
    locally anyway. Switching it off in exactly one visible place is honest
    about what the other tests exercise.

    The guarantee that no route is left unprotected does not come from those
    tests: `tests/test_web_auth.py` enumerates every registered route and
    asserts each one is either protected or on the documented public list, so a
    new endpoint is covered the moment it is written.

    A test that wants authentication passes `web_auth_enabled=True` to
    `Settings` explicitly, which still wins.
    """
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    yield


@pytest.fixture(autouse=True)
def _dry_run_is_decided_by_the_test(monkeypatch):
    """Never let the surrounding machine decide whether a stage does its work.

    ``.env`` is already ignored above, but environment variables still reach
    ``Settings``. CI exported ``DRY_RUN=true`` as a safety net against real API
    calls, which meant six stage tests quietly asserted against placeholder
    paths instead of the logic they were written for — and passed on the Pi,
    where the variable is off, so nobody saw it. Providers are mocked
    everywhere regardless, so the net was protecting nothing.

    A test that wants dry-run behaviour passes ``dry_run=True`` to ``Settings``
    explicitly, which still wins.
    """
    monkeypatch.setenv("DRY_RUN", "false")
    yield


@pytest.fixture(autouse=True)
def _isolate_pipeline_lock(tmp_path_factory, monkeypatch):
    """Keep the run lock out of the production data directory.

    ``_lock_path`` co-locates the lock with the SQLite database, and a default
    ``Settings()`` points at the real ``data/btcedu.db``. Without this a CLI
    test would contend with a pipeline run actually happening on the machine:
    the test then fails with "Pipeline busy" for reasons that have nothing to
    do with the code under test — and, worse, a test could block the real run.
    """
    lock_dir = tmp_path_factory.mktemp("pipeline-lock")
    monkeypatch.setattr(
        "btcedu.core.runlock._lock_path",
        lambda settings: lock_dir / "pipeline.lock",
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_profile_registry():
    """Keep the profile registry singleton from leaking between tests.

    ``get_registry(settings)`` fills a module-level singleton on first use, so a
    test that loads profiles from a temporary directory would otherwise leave an
    empty registry behind and make later tests resolve the wrong stage list.
    """
    from btcedu.profiles import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def db_engine():
    """In-memory SQLite engine for tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Database session for tests."""
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()
