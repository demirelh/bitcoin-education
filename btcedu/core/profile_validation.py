"""Refuse to run on a profile nobody can read (N8, A8).

A pipeline that silently falls back to a default plan when a profile is
misspelled, missing or malformed is the worst of both worlds: it produces
output, and the output is not the one anyone asked for. Everything here exists
to turn that into a stop before the first stage runs.

Validation is structural only. It never guesses what a profile meant, and it
never repairs one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from btcedu.profiles import ContentProfile, ProfileNotFoundError, get_registry

#: Sections of ``stage_config`` this repository actually reads. An unknown
#: section is reported rather than ignored, because a typo in a key is exactly
#: how a profile ends up doing nothing it was written to do.
KNOWN_STAGE_CONFIG_SECTIONS = frozenset(
    {
        "adapt",
        "anchor",
        "chapterize",
        "editorial_video",
        "imagegen",
        "qa",
        "render",
        "script",
        "segment",
        "transcript_analyze",
        "transcript_qa",
        "transcript_verify",
        "transcription",
        "translate",
        "translation_qa",
        "tts",
        "weather",
    }
)


class ProfileConfigurationError(RuntimeError):
    """A profile cannot be used as written."""

    def __init__(self, name: str, reasons: Iterable[str]) -> None:
        self.profile_name = name
        self.reasons = tuple(reasons)
        joined = "; ".join(self.reasons)
        super().__init__(f"Profile {name!r} is not usable: {joined}")


def known_stage_names() -> frozenset[str]:
    from btcedu.core.pipeline import _V2_STAGES

    return frozenset(name for name, _ in _V2_STAGES)


def profile_problems(profile: ContentProfile) -> tuple[str, ...]:
    """Everything structurally wrong with this profile, in reading order."""
    problems: list[str] = []

    stages = profile.stages_enabled
    if stages != "all":
        if not isinstance(stages, list):
            problems.append("stages_enabled must be 'all' or a list of stage names")
        else:
            unknown = sorted(set(stages) - known_stage_names())
            if unknown:
                problems.append(f"stages_enabled names unknown stages: {', '.join(unknown)}")

    if not isinstance(profile.stage_config, Mapping):
        problems.append("stage_config must be a mapping")
    else:
        unknown_sections = sorted(set(profile.stage_config) - KNOWN_STAGE_CONFIG_SECTIONS)
        if unknown_sections:
            problems.append(
                "stage_config has sections nothing reads: " + ", ".join(unknown_sections)
            )
        for key, value in profile.stage_config.items():
            if not isinstance(value, Mapping):
                problems.append(f"stage_config.{key} must be a mapping")
            elif "enabled" in value and not isinstance(value["enabled"], bool):
                problems.append(f"stage_config.{key}.enabled must be a boolean")

    if not isinstance(profile.review_gates, Mapping):
        problems.append("review_gates must be a mapping")

    if not profile.target_language.strip():
        problems.append("target_language is empty")

    return tuple(problems)


def validate_profile(profile: ContentProfile) -> ContentProfile:
    problems = profile_problems(profile)
    if problems:
        raise ProfileConfigurationError(profile.name, problems)
    return profile


def resolve_profile(settings, name: str) -> ContentProfile:
    """Look a profile up and validate it, or say precisely what is wrong.

    An unknown name is reported with the names that do exist, because the
    realistic cause is a typo and the realistic fix is reading the list.
    """
    registry = get_registry(settings)
    try:
        profile = registry.get(name)
    except ProfileNotFoundError as exc:
        raise ProfileConfigurationError(name, [str(exc)]) from exc
    return validate_profile(profile)


def assert_episode_profile_valid(settings, episode) -> None:
    """Stop a v2 episode before the first stage if its profile is unusable.

    Legacy v1 episodes are left exactly as they were: they predate profile
    routing, and failing them here would break stored work to enforce a rule
    that never applied to them.
    """
    if getattr(episode, "pipeline_version", 2) != 2:
        return
    name = getattr(episode, "content_profile", None)
    if not name:
        return
    resolve_profile(settings, name)


def registry_load_errors(settings) -> tuple[tuple[str, str], ...]:
    """Profile files that could not be read at all, as (path, error) pairs."""
    registry = get_registry(settings)
    return tuple(getattr(registry, "load_errors", {}).items())
