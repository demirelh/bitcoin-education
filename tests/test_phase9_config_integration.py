"""Phase 9 configuration, profile, and legacy compatibility integration tests."""

from pathlib import Path

import yaml

from btcedu.config import Settings
from btcedu.core.pipeline import _quality_gate_scoped, resolve_pipeline_plan
from btcedu.core.qa_reviewer import resolve_qa_config
from btcedu.core.tts import _resolve_tts_config
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.profiles import get_registry, reset_registry


def test_settings_precedence_constructor_over_environment_over_default(monkeypatch):
    monkeypatch.setenv("MAX_EPISODE_COST_USD", "7.5")
    from_environment = Settings(_env_file=None)
    from_constructor = Settings(max_episode_cost_usd=3.25, _env_file=None)

    assert Settings.model_fields["max_episode_cost_usd"].default == 15.0
    assert from_environment.max_episode_cost_usd == 7.5
    assert from_constructor.max_episode_cost_usd == 3.25


def test_profile_tts_values_override_global_settings():
    reset_registry()
    settings = Settings(
        elevenlabs_voice_id="global-voice",
        elevenlabs_model="global-model",
    )
    episode = Episode(
        episode_id="ep-profile-precedence",
        source="tagesschau_rss",
        title="News",
        url="https://example.com/news",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )

    resolved = _resolve_tts_config(episode, settings)

    assert resolved["voice_id"] == "NsFK0aDGLbVusA7tQfOB"
    assert resolved["model"] == "eleven_turbo_v2_5"
    reset_registry()


def test_profile_can_disable_each_new_qa_stage(db_session, tmp_path):
    profile = {
        "name": "qa_disabled",
        "display_name": "QA disabled",
        "source_language": "de",
        "target_language": "tr",
        "domain": "test",
        "pipeline_version": 2,
        "stages_enabled": "all",
        "stage_config": {
            "transcription": {"secondary": {"enabled": False, "mode": "disabled"}},
            "transcript_analyze": {"enabled": False},
            "transcript_verify": {"enabled": False},
            "transcript_qa": {"enabled": False},
            "translation_qa": {"enabled": False},
            "qa": {"enabled": False},
        },
    }
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "qa_disabled.yaml").write_text(
        yaml.safe_dump(profile),
        encoding="utf-8",
    )
    settings = Settings(
        profiles_dir=str(profiles_dir),
        outputs_dir=str(tmp_path / "outputs"),
    )
    episode = Episode(
        episode_id="ep-disabled",
        source="youtube_rss",
        title="Disabled QA",
        url="https://example.com/disabled",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=2,
        content_profile="qa_disabled",
    )
    db_session.add(episode)
    db_session.commit()

    reset_registry()
    get_registry(settings)
    stages = [item.stage for item in resolve_pipeline_plan(db_session, episode, settings=settings)]
    qa_config = resolve_qa_config(settings, episode)

    assert "transcript_analyze" not in stages
    assert "transcript_verify" not in stages
    assert "transcript_qa" not in stages
    assert "review_gate_transcript_qa" not in stages
    assert qa_config["enabled"] is False
    reset_registry()


def test_deterministic_translation_qa_can_be_disabled_without_disabling_llm_gate(
    db_session, tmp_path
):
    profile = {
        "name": "llm_qa_only",
        "display_name": "LLM QA only",
        "source_language": "de",
        "target_language": "tr",
        "domain": "test",
        "pipeline_version": 2,
        "stage_config": {
            "translation_qa": {"enabled": False},
            "qa": {"enabled": True},
        },
    }
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "llm_qa_only.yaml").write_text(
        yaml.safe_dump(profile),
        encoding="utf-8",
    )
    settings = Settings(
        profiles_dir=str(profiles_dir),
        outputs_dir=str(tmp_path / "outputs"),
        qa_review_enabled=False,
    )
    episode = Episode(
        episode_id="ep-llm-only",
        source="youtube_rss",
        title="LLM only",
        url="https://example.com/llm-only",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
        content_profile="llm_qa_only",
    )
    db_session.add(episode)
    db_session.commit()

    reset_registry()
    get_registry(settings)
    config = resolve_qa_config(settings, episode)

    assert config["enabled"] is True
    assert config["deterministic_checks"] is False
    assert _quality_gate_scoped(settings, episode) is True
    reset_registry()


def test_suspicious_only_verification_is_disabled_without_analysis(db_session, tmp_path):
    profile = {
        "name": "verify_without_analysis",
        "display_name": "Invalid dependency",
        "source_language": "de",
        "target_language": "tr",
        "domain": "test",
        "pipeline_version": 2,
        "stage_config": {
            "transcript_analyze": {"enabled": False},
            "transcript_verify": {"enabled": True},
            "transcription": {
                "secondary": {
                    "enabled": True,
                    "mode": "suspicious_segments_only",
                }
            },
        },
    }
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "verify_without_analysis.yaml").write_text(
        yaml.safe_dump(profile),
        encoding="utf-8",
    )
    settings = Settings(profiles_dir=str(profiles_dir))
    episode = Episode(
        episode_id="ep-invalid-dependency",
        source="youtube_rss",
        title="Dependency",
        url="https://example.com/dependency",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=2,
        content_profile="verify_without_analysis",
    )
    db_session.add(episode)
    db_session.commit()

    reset_registry()
    get_registry(settings)
    stages = [item.stage for item in resolve_pipeline_plan(db_session, episode, settings=settings)]

    assert "transcript_analyze" not in stages
    assert "transcript_verify" not in stages
    reset_registry()


def test_tagesschau_profile_operational_contract():
    reset_registry()
    settings = Settings(qa_review_enabled=False)
    profile = get_registry(settings).get("tagesschau_tr")
    config = profile.stage_config

    assert config["transcript_analyze"]["enabled"] is True
    assert config["transcript_verify"]["enabled"] is True
    assert config["transcription"]["secondary"]["mode"] == "suspicious_segments_only"
    assert config["transcript_qa"]["enabled"] is True
    assert config["translation_qa"]["enabled"] is True
    assert config["qa"]["enabled"] is True
    assert config["qa"]["quality_gate"]["max_automatic_retries"] == 2
    assert config["adapt"]["mode"] == "conditional"
    assert config["imagegen"]["provider"] == "generative"
    assert config["tts"]["voice_id"]
    assert config["tts"]["model"] == "eleven_turbo_v2_5"
    assert profile.auto_publish is False
    episode = Episode(
        episode_id="ep-tagesschau-config",
        source="tagesschau_rss",
        title="News",
        url="https://example.com/tagesschau",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    assert settings.qa_review_enabled is False
    assert resolve_qa_config(settings, episode)["enabled"] is True
    reset_registry()


def test_legacy_v1_episode_keeps_new_qa_stages_out_of_plan(db_session, tmp_path):
    settings = Settings(outputs_dir=str(tmp_path / "outputs"))
    episode = Episode(
        episode_id="ep-v1-phase9",
        source="youtube_rss",
        title="Legacy episode",
        url="https://example.com/legacy",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=1,
    )
    db_session.add(episode)
    db_session.commit()

    stages = [item.stage for item in resolve_pipeline_plan(db_session, episode, settings=settings)]

    assert not {
        "transcript_analyze",
        "transcript_verify",
        "transcript_qa",
        "review_gate_transcript_qa",
    }.intersection(stages)
    assert "correct" in stages


def test_configuration_precedence_is_documented():
    runbook = (Path(__file__).parents[1] / "docs" / "runbooks" / "profile-switching.md").read_text(
        encoding="utf-8"
    )

    assert "Configuration precedence" in runbook
    assert "`Settings` class defaults" in runbook
    assert "content profile" in runbook
    assert "Explicit CLI flags" in runbook
