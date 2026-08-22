import warnings

from btcedu.config import Settings


class TestSettings:
    def test_default_values(self):
        settings = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
            podcast_youtube_channel_id="UCtest123",
        )
        assert settings.database_url == "sqlite:///data/btcedu.db"
        assert settings.audio_format == "m4a"
        assert settings.max_audio_chunk_mb == 24
        assert settings.claude_model == "claude-sonnet-4-20250514"
        assert settings.max_retries == 3
        assert settings.whisper_model == "whisper-1"
        assert settings.whisper_language == "de"
        assert settings.raw_data_dir == "data/raw"
        assert settings.transcripts_dir == "data/transcripts"

    def test_source_type_default(self):
        settings = Settings()
        assert settings.source_type == "youtube_rss"

    def test_source_type_override(self):
        settings = Settings(source_type="rss")
        assert settings.source_type == "rss"

    def test_effective_whisper_api_key_prefers_whisper(self):
        settings = Settings(whisper_api_key="whisper-key", openai_api_key="openai-key")
        assert settings.effective_whisper_api_key == "whisper-key"

    def test_effective_whisper_api_key_falls_back_to_openai(self):
        settings = Settings(whisper_api_key="", openai_api_key="openai-key")
        assert settings.effective_whisper_api_key == "openai-key"

    def test_elevenlabs_keys_are_ordered_primary_first(self):
        settings = Settings(
            elevenlabs_api_key="primary",
            elevenlabs_api_key_fallback="reserve_a,reserve_b",
        )
        assert settings.elevenlabs_api_keys == ["primary", "reserve_a", "reserve_b"]

    def test_elevenlabs_keys_without_a_reserve(self):
        settings = Settings(elevenlabs_api_key="primary")
        assert settings.elevenlabs_api_keys == ["primary"]

    def test_elevenlabs_keys_drop_blanks_and_repeats(self):
        """A repeated key would only buy a second rejection."""
        settings = Settings(
            elevenlabs_api_key="primary",
            elevenlabs_api_key_fallback=" reserve , , primary ,reserve",
        )
        assert settings.elevenlabs_api_keys == ["primary", "reserve"]

    def test_elevenlabs_keys_empty_when_nothing_configured(self):
        settings = Settings(elevenlabs_api_key="", elevenlabs_api_key_fallback="")
        assert settings.elevenlabs_api_keys == []

    def test_rss_url_from_channel_id(self):
        settings = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
            podcast_youtube_channel_id="UCtest123",
        )
        expected = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtest123"
        assert settings.rss_url == expected

    def test_rss_url_explicit_override(self):
        settings = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
            podcast_youtube_channel_id="UCtest123",
            podcast_rss_url="https://custom.feed/rss",
        )
        assert settings.rss_url == "https://custom.feed/rss"

    def test_rss_url_empty_when_no_channel(self):
        settings = Settings(
            anthropic_api_key="test-key",
            openai_api_key="test-key",
            podcast_youtube_channel_id="",
        )
        assert settings.rss_url == ""

    def test_claude_generation_defaults(self):
        # Explicitly set dry_run=False to test the default,
        # ignoring any DRY_RUN environment variable
        settings = Settings(dry_run=False)
        assert settings.claude_max_tokens == 16384
        assert settings.claude_temperature == 0.3
        assert settings.dry_run is False
        assert settings.outputs_dir == "data/outputs"

    def test_claude_generation_override(self):
        settings = Settings(claude_max_tokens=8192, claude_temperature=0.7, dry_run=True)
        assert settings.claude_max_tokens == 8192
        assert settings.claude_temperature == 0.7
        assert settings.dry_run is True

    def test_failover_defaults_preserve_backward_compatibility(self):
        settings = Settings()
        assert settings.failover_enabled is False
        assert settings.failover_node_role == "primary"
        assert settings.failover_pipeline_lease_ttl_seconds == 540
        assert settings.failover_publish_lease_ttl_seconds == 900

    def test_youtube_targets_are_separate_and_safe_by_default(self):
        settings = Settings()
        assert settings.youtube_default_target == "test"
        assert settings.youtube_test_default_privacy == "private"
        assert settings.youtube_production_default_privacy == "unlisted"
        assert (
            settings.youtube_test_credentials_path
            != settings.youtube_production_credentials_path
        )

    def test_legacy_youtube_paths_migrate_to_production_target(self):
        settings = Settings(
            youtube_client_secrets_path="legacy/client.json",
            youtube_credentials_path="legacy/token.json",
            youtube_default_privacy="private",
        )

        assert settings.youtube_production_client_secrets_path == "legacy/client.json"
        assert settings.youtube_production_credentials_path == "legacy/token.json"
        assert settings.youtube_production_default_privacy == "private"
        assert settings.youtube_test_credentials_path == "data/youtube/test/credentials.json"

    def test_explicit_production_youtube_paths_override_legacy_paths(self):
        settings = Settings(
            youtube_client_secrets_path="legacy/client.json",
            youtube_credentials_path="legacy/token.json",
            youtube_production_client_secrets_path="production/client.json",
            youtube_production_credentials_path="production/token.json",
        )

        assert settings.youtube_production_client_secrets_path == "production/client.json"
        assert settings.youtube_production_credentials_path == "production/token.json"

    def test_anthropic_api_key_loads(self):
        settings = Settings(anthropic_api_key="sk-ant-test")
        assert settings.anthropic_api_key == "sk-ant-test"
        assert settings.claude_api_key == ""

    def test_claude_api_key_alias_fallback(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            settings = Settings(anthropic_api_key="", claude_api_key="sk-ant-old")
            assert settings.anthropic_api_key == "sk-ant-old"
            assert settings.claude_api_key == ""  # cleared after migration
            assert len(w) == 1
            assert "deprecated" in str(w[0].message).lower()

    def test_anthropic_takes_precedence_over_claude(self):
        settings = Settings(anthropic_api_key="sk-ant-new", claude_api_key="sk-ant-old")
        assert settings.anthropic_api_key == "sk-ant-new"
        assert settings.claude_api_key == ""  # cleared after migration
