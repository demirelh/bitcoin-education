"""Content profile registry for multi-profile pipeline support."""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


class ProfileNotFoundError(KeyError):
    """Raised when a requested content profile does not exist."""


class ContentProfile(BaseModel):
    """Definition of a content pipeline profile."""

    name: str
    display_name: str
    source_language: str
    target_language: str
    domain: str
    pipeline_version: int = 2
    stages_enabled: list[str] | Literal["all"] = "all"
    stage_config: dict = {}
    review_gates: dict = {}
    youtube: dict = {}
    prompt_namespace: str | None = None

    @field_validator("pipeline_version")
    @classmethod
    def _validate_pipeline_version(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError(f"pipeline_version must be 1 or 2, got {v}")
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid profile name: {v!r}")
        return v


class ProfileRegistry:
    """Loads and caches content profiles from YAML files."""

    def __init__(self) -> None:
        self._profiles: dict[str, ContentProfile] = {}

    def load_all(self, profiles_dir: str | Path) -> dict[str, ContentProfile]:
        """Load all .yaml profile files from a directory."""
        profiles_dir = Path(profiles_dir)
        if not profiles_dir.is_dir():
            logger.warning("Profiles directory does not exist: %s", profiles_dir)
            return self._profiles

        for yaml_path in sorted(profiles_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    logger.warning("Skipping non-dict YAML: %s", yaml_path)
                    continue
                profile = ContentProfile(**data)
                self._profiles[profile.name] = profile
                logger.debug("Loaded profile: %s from %s", profile.name, yaml_path.name)
            except Exception:
                logger.exception("Failed to load profile from %s", yaml_path)

        return self._profiles

    def get(self, name: str) -> ContentProfile:
        """Get a profile by name. Raises ProfileNotFoundError if not found."""
        try:
            return self._profiles[name]
        except KeyError:
            raise ProfileNotFoundError(
                f"Profile {name!r} not found. "
                f"Available: {list(self._profiles.keys())}"
            )

    def list_profiles(self) -> list[ContentProfile]:
        """Return all loaded profiles."""
        return list(self._profiles.values())


_registry: ProfileRegistry | None = None


def get_registry(settings=None) -> ProfileRegistry:
    """Get or create the singleton ProfileRegistry.

    If settings is provided, loads profiles from settings.profiles_dir.
    """
    global _registry
    if _registry is None:
        _registry = ProfileRegistry()
        if settings is not None:
            profiles_dir = getattr(settings, "profiles_dir", "btcedu/profiles")
            _registry.load_all(profiles_dir)
    return _registry


def reset_registry() -> None:
    """Reset the singleton registry (for testing)."""
    global _registry
    _registry = None
