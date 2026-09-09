"""A profile that cannot be read stops the run (N8, A8).

The failure this guards against is quiet: a misspelled profile name, an
unreadable YAML file or a section nothing consumes used to leave the pipeline
running a default plan. It produced a finished video, and nothing in the output
said it was not the one the profile described.
"""

from __future__ import annotations

import pytest

from btcedu.core.profile_validation import (
    KNOWN_STAGE_CONFIG_SECTIONS,
    ProfileConfigurationError,
    assert_episode_profile_valid,
    known_stage_names,
    profile_problems,
    resolve_profile,
    validate_profile,
)
from btcedu.profiles import ContentProfile, ProfileRegistry, get_registry, reset_registry


def _Settings(profiles_dir: str):
    from btcedu.config import Settings

    return Settings(profiles_dir=profiles_dir)


class _Episode:
    def __init__(self, *, content_profile: str, pipeline_version: int = 2) -> None:
        self.content_profile = content_profile
        self.pipeline_version = pipeline_version


def _profile(**overrides) -> ContentProfile:
    payload = {
        "name": "almanya24_tr",
        "display_name": "ALMANYA24",
        "source_language": "de",
        "target_language": "tr",
        "domain": "news",
    }
    payload.update(overrides)
    return ContentProfile(**payload)


@pytest.fixture
def isolated_registry(tmp_path):
    reset_registry()
    yield tmp_path
    reset_registry()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_a_plain_profile_is_valid():
    assert profile_problems(_profile()) == ()
    assert validate_profile(_profile()).name == "almanya24_tr"


def test_the_shipped_profiles_are_valid():
    """The repository's own profiles must pass the check they are subject to."""
    reset_registry()
    registry = get_registry(_Settings("btcedu/profiles"))

    names = {profile.name for profile in registry.list_profiles()}
    assert {"bitcoin_podcast", "tagesschau_tr"} <= names
    for profile in registry.list_profiles():
        assert profile_problems(profile) == (), profile.name
    reset_registry()


def test_a_stage_name_that_does_not_exist_is_named():
    problems = profile_problems(_profile(stages_enabled=["download", "transkribe"]))

    assert any("transkribe" in problem for problem in problems)


def test_a_stage_config_section_nothing_reads_is_reported():
    """A typo in a section key is how a profile silently stops taking effect."""
    problems = profile_problems(_profile(stage_config={"imagegn": {"provider": "flux"}}))

    assert any("imagegn" in problem for problem in problems)


def test_a_stage_config_section_must_be_a_mapping():
    problems = profile_problems(_profile(stage_config={"imagegen": "flux"}))

    assert any("mapping" in problem for problem in problems)


def test_a_new_v1_profile_is_refused():
    """The model itself rejects it, so no v1 profile can reach validation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _profile(pipeline_version=1)


def test_an_empty_target_language_is_refused():
    problems = profile_problems(_profile(target_language="  "))

    assert any("target_language" in problem for problem in problems)


def test_every_known_section_is_a_plain_lowercase_key():
    assert all(key == key.lower() for key in KNOWN_STAGE_CONFIG_SECTIONS)
    assert "download" in known_stage_names()
    assert "render" in known_stage_names()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_an_unknown_profile_name_lists_the_names_that_exist(isolated_registry):
    (isolated_registry / "one.yaml").write_text(
        "name: one\ndisplay_name: One\nsource_language: de\n"
        "target_language: tr\ndomain: news\n",
        encoding="utf-8",
    )
    settings = _Settings(str(isolated_registry))
    get_registry(settings)

    with pytest.raises(ProfileConfigurationError) as exc:
        resolve_profile(settings, "onee")

    assert "one" in str(exc.value)


def test_a_broken_profile_file_is_remembered_rather_than_forgotten(isolated_registry):
    """A file that fails to load must not look like a file that never existed."""
    (isolated_registry / "broken.yaml").write_text("name: [\n", encoding="utf-8")
    registry = ProfileRegistry()

    registry.load_all(isolated_registry)

    assert registry.load_errors
    assert any("broken.yaml" in path for path in registry.load_errors)


def test_a_yaml_file_that_is_not_a_mapping_is_recorded(isolated_registry):
    (isolated_registry / "list.yaml").write_text("- one\n- two\n", encoding="utf-8")
    registry = ProfileRegistry()

    registry.load_all(isolated_registry)

    assert any("list.yaml" in path for path in registry.load_errors)


# ---------------------------------------------------------------------------
# The pipeline stops before the first stage
# ---------------------------------------------------------------------------


def test_a_v2_episode_with_an_unknown_profile_is_stopped(isolated_registry):
    (isolated_registry / "one.yaml").write_text(
        "name: one\ndisplay_name: One\nsource_language: de\n"
        "target_language: tr\ndomain: news\n",
        encoding="utf-8",
    )
    settings = _Settings(str(isolated_registry))
    get_registry(settings)

    with pytest.raises(ProfileConfigurationError):
        assert_episode_profile_valid(settings, _Episode(content_profile="typo"))


def test_a_legacy_v1_episode_is_left_alone(isolated_registry):
    """Stored v1 work predates profile routing and must not fail here."""
    settings = _Settings(str(isolated_registry))
    get_registry(settings)

    assert_episode_profile_valid(
        settings, _Episode(content_profile="whatever", pipeline_version=1)
    )


def test_an_episode_without_a_profile_is_left_alone(isolated_registry):
    settings = _Settings(str(isolated_registry))
    get_registry(settings)

    assert_episode_profile_valid(settings, _Episode(content_profile=""))


def test_the_pipeline_validates_before_it_plans_anything():
    """The check has to sit ahead of the stage loop, not inside it."""
    import inspect

    from btcedu.core import pipeline

    source = inspect.getsource(pipeline.run_episode_pipeline)
    validation_at = source.index("assert_episode_profile_valid(settings, episode)")
    loop_at = source.index("for stage_name, required_status in stages:")

    assert validation_at < loop_at


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def _run_cli(settings, args: list[str]):
    from click.testing import CliRunner

    from btcedu.cli import cli

    return CliRunner().invoke(cli, args, obj={"settings": settings})


def test_the_cli_reports_a_broken_profile_and_exits_non_zero(isolated_registry):
    (isolated_registry / "good.yaml").write_text(
        "name: good\ndisplay_name: Good\nsource_language: de\n"
        "target_language: tr\ndomain: news\n",
        encoding="utf-8",
    )
    (isolated_registry / "bad.yaml").write_text(
        "name: bad\ndisplay_name: Bad\nsource_language: de\n"
        "target_language: tr\ndomain: news\nstage_config:\n  imagegn: {}\n",
        encoding="utf-8",
    )
    settings = _Settings(str(isolated_registry))
    get_registry(settings)

    result = _run_cli(settings, ["profiles-validate"])

    assert result.exit_code != 0
    assert "[OK]   good" in result.output
    assert "imagegn" in result.output


def test_the_cli_accepts_the_shipped_profiles():
    reset_registry()
    settings = _Settings("btcedu/profiles")
    get_registry(settings)

    result = _run_cli(settings, ["profiles-validate"])

    assert result.exit_code == 0, result.output
    assert "[OK]   tagesschau_tr" in result.output
    reset_registry()
