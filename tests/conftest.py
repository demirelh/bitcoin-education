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
