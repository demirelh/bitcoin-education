"""Tests for multi-profile foundation (Phase 1)."""

import tempfile
from pathlib import Path

import pytest
import yaml

from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.profiles import (
    ContentProfile,
    ProfileNotFoundError,
    ProfileRegistry,
    get_registry,
    reset_registry,
)

# -----------------------------------------------------------------------
# ContentProfile validation
# -----------------------------------------------------------------------


class TestContentProfile:
    def test_valid_profile(self):
        p = ContentProfile(
            name="bitcoin_podcast",
            display_name="Bitcoin Podcast (DE→TR)",
            source_language="de",
            target_language="tr",
            domain="cryptocurrency",
        )
        assert p.name == "bitcoin_podcast"
        assert p.pipeline_version == 2
        assert p.stages_enabled == "all"
        assert p.stage_config == {}
        assert p.prompt_namespace is None

    def test_invalid_pipeline_version(self):
        with pytest.raises(ValueError, match="pipeline_version must be 1 or 2"):
            ContentProfile(
                name="test",
                display_name="Test",
                source_language="de",
                target_language="tr",
                domain="test",
                pipeline_version=3,
            )

    def test_invalid_name(self):
        with pytest.raises(ValueError, match="Invalid profile name"):
            ContentProfile(
                name="",
                display_name="Test",
                source_language="de",
                target_language="tr",
                domain="test",
            )

    def test_stages_enabled_list(self):
        p = ContentProfile(
            name="test_profile",
            display_name="Test",
            source_language="de",
            target_language="tr",
            domain="test",
            stages_enabled=["download", "transcribe", "correct"],
        )
        assert p.stages_enabled == ["download", "transcribe", "correct"]

    def test_stage_config_and_youtube(self):
        p = ContentProfile(
            name="test_profile",
            display_name="Test",
            source_language="de",
            target_language="tr",
            domain="test",
            stage_config={"adapt": {"skip": True}},
            youtube={"category_id": "25", "tags": ["news"]},
        )
        assert p.stage_config["adapt"]["skip"] is True
        assert p.youtube["tags"] == ["news"]


# -----------------------------------------------------------------------
# ProfileRegistry
# -----------------------------------------------------------------------


class TestProfileRegistry:
    def test_load_all_from_directory(self, tmp_path):
        # Write two profile YAMLs
        (tmp_path / "a.yaml").write_text(
            yaml.dump(
                {
                    "name": "profile_a",
                    "display_name": "A",
                    "source_language": "de",
                    "target_language": "tr",
                    "domain": "test",
                }
            )
        )
        (tmp_path / "b.yaml").write_text(
            yaml.dump(
                {
                    "name": "profile_b",
                    "display_name": "B",
                    "source_language": "en",
                    "target_language": "fr",
                    "domain": "news",
                }
            )
        )

        registry = ProfileRegistry()
        profiles = registry.load_all(tmp_path)
        assert len(profiles) == 2
        assert "profile_a" in profiles
        assert "profile_b" in profiles

    def test_get(self, tmp_path):
        (tmp_path / "x.yaml").write_text(
            yaml.dump(
                {
                    "name": "x",
                    "display_name": "X",
                    "source_language": "de",
                    "target_language": "tr",
                    "domain": "test",
                }
            )
        )
        registry = ProfileRegistry()
        registry.load_all(tmp_path)
        p = registry.get("x")
        assert p.name == "x"

    def test_get_not_found(self):
        registry = ProfileRegistry()
        with pytest.raises(ProfileNotFoundError, match="nope"):
            registry.get("nope")

    def test_list_profiles(self, tmp_path):
        (tmp_path / "one.yaml").write_text(
            yaml.dump(
                {
                    "name": "one",
                    "display_name": "One",
                    "source_language": "de",
                    "target_language": "tr",
                    "domain": "test",
                }
            )
        )
        registry = ProfileRegistry()
        registry.load_all(tmp_path)
        profiles = registry.list_profiles()
        assert len(profiles) == 1
        assert profiles[0].name == "one"

    def test_load_nonexistent_dir(self):
        registry = ProfileRegistry()
        profiles = registry.load_all("/nonexistent/path")
        assert profiles == {}

    def test_invalid_yaml_skipped(self, tmp_path):
        (tmp_path / "good.yaml").write_text(
            yaml.dump(
                {
                    "name": "good",
                    "display_name": "Good",
                    "source_language": "de",
                    "target_language": "tr",
                    "domain": "test",
                }
            )
        )
        (tmp_path / "bad.yaml").write_text("not a dict: [1, 2, 3]")

        registry = ProfileRegistry()
        profiles = registry.load_all(tmp_path)
        assert len(profiles) == 1
        assert "good" in profiles

    def test_load_bundled_profiles(self):
        """Load the actual profiles shipped with the project."""
        profiles_dir = Path(__file__).parent.parent / "btcedu" / "profiles"
        registry = ProfileRegistry()
        profiles = registry.load_all(profiles_dir)
        assert "bitcoin_podcast" in profiles
        assert "tagesschau_tr" in profiles

        bp = profiles["bitcoin_podcast"]
        assert bp.source_language == "de"
        assert bp.target_language == "tr"
        assert bp.domain == "cryptocurrency"
        assert bp.pipeline_version == 2


# -----------------------------------------------------------------------
# Singleton registry
# -----------------------------------------------------------------------


class TestGetRegistry:
    def teardown_method(self):
        reset_registry()

    def test_singleton(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_reset(self):
        r1 = get_registry()
        reset_registry()
        r2 = get_registry()
        assert r1 is not r2


# -----------------------------------------------------------------------
# Episode.content_profile
# -----------------------------------------------------------------------


class TestEpisodeContentProfile:
    def test_default_content_profile(self, db_session):
        ep = Episode(
            episode_id="ep_profile_test",
            source="youtube_rss",
            title="Test",
            url="https://example.com",
            status=EpisodeStatus.NEW,
        )
        db_session.add(ep)
        db_session.commit()

        loaded = db_session.query(Episode).filter_by(episode_id="ep_profile_test").one()
        assert loaded.content_profile == "bitcoin_podcast"

    def test_custom_content_profile(self, db_session):
        ep = Episode(
            episode_id="ep_profile_tagesschau",
            source="rss",
            title="Tagesschau",
            url="https://example.com",
            status=EpisodeStatus.NEW,
            content_profile="tagesschau_tr",
        )
        db_session.add(ep)
        db_session.commit()

        loaded = db_session.query(Episode).filter_by(episode_id="ep_profile_tagesschau").one()
        assert loaded.content_profile == "tagesschau_tr"


# -----------------------------------------------------------------------
# Profile-aware prompt template resolution
# -----------------------------------------------------------------------


class TestProfileAwareTemplateResolution:
    def test_resolve_default_template(self, db_session):
        """Without profile, resolve to base template."""
        from btcedu.core.prompt_registry import PromptRegistry
        from btcedu.models.prompt_version import PromptVersion  # noqa: F401

        with tempfile.TemporaryDirectory() as tmpdir:
            templates_dir = Path(tmpdir)
            (templates_dir / "test.md").write_text("---\nname: test\n---\nHello {{ name }}")

            registry = PromptRegistry(db_session, templates_dir=templates_dir)
            path = registry.resolve_template_path("test.md")
            assert path == templates_dir / "test.md"

    def test_resolve_profile_override(self, db_session):
        """With profile, prefer profile-specific template."""
        from btcedu.core.prompt_registry import PromptRegistry
        from btcedu.models.prompt_version import PromptVersion  # noqa: F401

        with tempfile.TemporaryDirectory() as tmpdir:
            templates_dir = Path(tmpdir)
            (templates_dir / "test.md").write_text("---\nname: test\n---\nDefault")
            (templates_dir / "tagesschau_tr").mkdir()
            (templates_dir / "tagesschau_tr" / "test.md").write_text(
                "---\nname: test\n---\nProfile override"
            )

            registry = PromptRegistry(db_session, templates_dir=templates_dir)
            path = registry.resolve_template_path("test.md", profile="tagesschau_tr")
            assert path == templates_dir / "tagesschau_tr" / "test.md"

    def test_resolve_profile_fallback(self, db_session):
        """With profile that has no override, fall back to base."""
        from btcedu.core.prompt_registry import PromptRegistry
        from btcedu.models.prompt_version import PromptVersion  # noqa: F401

        with tempfile.TemporaryDirectory() as tmpdir:
            templates_dir = Path(tmpdir)
            (templates_dir / "test.md").write_text("---\nname: test\n---\nDefault")

            registry = PromptRegistry(db_session, templates_dir=templates_dir)
            path = registry.resolve_template_path("test.md", profile="nonexistent")
            assert path == templates_dir / "test.md"

    def test_load_template_with_profile(self, db_session):
        """load_template() with profile param uses resolution."""
        from btcedu.core.prompt_registry import PromptRegistry
        from btcedu.models.prompt_version import PromptVersion  # noqa: F401

        with tempfile.TemporaryDirectory() as tmpdir:
            templates_dir = Path(tmpdir)
            (templates_dir / "greet.md").write_text("---\nname: greet\n---\nHello default")
            (templates_dir / "myprofile").mkdir()
            (templates_dir / "myprofile" / "greet.md").write_text(
                "---\nname: greet\nmodel: custom\n---\nHello profile"
            )

            registry = PromptRegistry(db_session, templates_dir=templates_dir)

            # Without profile -> default
            meta, body = registry.load_template("greet.md")
            assert "default" in body

            # With profile -> override
            meta, body = registry.load_template("greet.md", profile="myprofile")
            assert "profile" in body
            assert meta.get("model") == "custom"


# -----------------------------------------------------------------------
# Migration 008 idempotency
# -----------------------------------------------------------------------


class TestMigration008:
    def test_migration_idempotent(self, db_engine, db_session):
        """Migration 008 can be applied twice without error."""
        from btcedu.migrations import AddContentProfileMigration

        # Ensure schema_migrations table exists
        from btcedu.models.migration import SchemaMigration

        SchemaMigration.__table__.create(db_engine, checkfirst=True)

        m = AddContentProfileMigration()

        # Episode table already has content_profile from ORM create_all,
        # so migration should detect it and skip. Run twice for idempotency.
        m.up(db_session)
        assert m.is_applied(db_session)

        # Second run should not raise
        m.up(db_session)

    def test_migration_in_registry(self):
        """Migration 008 is in the MIGRATIONS list."""
        from btcedu.migrations import MIGRATIONS

        versions = [m.version for m in MIGRATIONS]
        assert "008_add_content_profile" in versions


# -----------------------------------------------------------------------
# Config additions
# -----------------------------------------------------------------------


class TestConfigProfileFields:
    def test_default_values(self):
        from btcedu.config import Settings

        s = Settings(anthropic_api_key="test")
        assert s.profiles_dir == "btcedu/profiles"
        assert s.default_content_profile == "bitcoin_podcast"
