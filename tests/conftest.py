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
def _isolate_tts_cache(tmp_path_factory, monkeypatch):
    """Keep the reusable-take store out of the working tree.

    The default location is ``data/tts_cache`` relative to the CWD, so without
    this a test run would both litter the real cache and read from it — a
    synthesis test would silently be served an earlier test's audio and stop
    testing synthesis at all.
    """
    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path_factory.mktemp("tts-cache")))
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
