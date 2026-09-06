"""Profile-owned avatar configuration, parsed once and validated eagerly.

This sits below the anchor stage on purpose. The readiness command, the
dashboard and the look rotation all need the resolved configuration without
dragging in the pipeline stage, which pulls SQLAlchemy models and provider
clients behind it.
"""

import logging
from dataclasses import dataclass, field

from btcedu.config import Settings

logger = logging.getLogger(__name__)

# What the profile ships before phase 1 produced the real looks. An active look
# still carrying this value is a misconfiguration, not a usable avatar.
PLACEHOLDER_LOOK_ID = "REPLACE_WITH_HEYGEN_LOOK_ID"

# HeyGen counts every asynchronous generation on the account against this, not
# just ours, so staying below it is the caller's job.
HEYGEN_MAX_CONCURRENT_JOBS = 10

VALID_ENGINES = {"avatar_iii", "avatar_iv", "avatar_v"}
VALID_STUDIO_MODES = {"composite", "baked"}
VALID_ROTATION_STRATEGIES = {"least_recently_used", "fixed"}


@dataclass(frozen=True)
class PresenterLook:
    """One outfit the presenter can wear, as configured in the profile."""

    name: str
    avatar_look_id: str
    active: bool = False

    @property
    def is_placeholder(self) -> bool:
        return self.avatar_look_id.strip() == PLACEHOLDER_LOOK_ID


@dataclass(frozen=True)
class StudioConfig:
    """Where the versioned studio plate and its display zones live."""

    asset_dir: str = ""
    manifest: str = "manifest.json"
    required_version: int = 1


@dataclass(frozen=True)
class RightsConfig:
    """Records whether the consent paperwork exists; never interprets it."""

    consent_documented: bool = False
    consent_reference: str = ""
    permitted_channels: tuple[str, ...] = ()
    permitted_territories: tuple[str, ...] = ()
    revoked: bool = False
    ai_disclosure_required: bool = True


@dataclass(frozen=True)
class AnchorConfig:
    """Resolved provider configuration for one episode."""

    provider: str
    engine: str
    source_image: str
    source_image_url: str
    avatar_id: str
    avatar_type: str
    expression: str
    output_format: str
    resolution: str
    aspect_ratio: str
    cost_per_second_usd: float
    max_cost_usd: float
    studio_mode: str = "baked"
    max_concurrent_jobs: int = 1
    rotation_strategy: str = "least_recently_used"
    cost_source: str = ""
    cost_checked_on: str = ""
    looks: tuple[PresenterLook, ...] = ()
    studio: StudioConfig = field(default_factory=StudioConfig)
    rights: RightsConfig = field(default_factory=RightsConfig)

    @property
    def active_looks(self) -> tuple[PresenterLook, ...]:
        """Looks the rotation may choose from, in profile order."""
        return tuple(look for look in self.looks if look.active)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _as_str_tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value.strip(),)
    return tuple(str(item).strip() for item in value if str(item).strip())


def parse_looks(raw) -> tuple[PresenterLook, ...]:
    """Read the outfit pool, rejecting the mistakes that cost money later.

    A duplicate ID would silently break rotation into a single repeated outfit,
    and an active placeholder would be sent to the provider as a literal avatar
    ID. Both are caught here rather than at the first paid call.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("Profile anchor.looks must be a list")

    looks: list[PresenterLook] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"Profile anchor.looks[{index}] must be a mapping")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Profile anchor.looks[{index}] is missing a name")
        if name in seen_names:
            raise ValueError(f"Duplicate anchor look name: {name!r}")
        seen_names.add(name)
        look_id = str(entry.get("avatar_look_id") or "").strip()
        if not look_id:
            raise ValueError(f"Anchor look {name!r} is missing avatar_look_id")
        looks.append(
            PresenterLook(
                name=name,
                avatar_look_id=look_id,
                active=_as_bool(entry.get("active"), default=False),
            )
        )

    active = [look for look in looks if look.active]
    active_ids = [look.avatar_look_id for look in active]
    duplicates = {value for value in active_ids if active_ids.count(value) > 1}
    if duplicates:
        raise ValueError(
            "Active anchor looks must have unique avatar_look_id; "
            f"duplicated: {sorted(duplicates)}"
        )
    placeholders = [look.name for look in active if look.is_placeholder]
    if placeholders:
        raise ValueError(
            "Active anchor looks still carry the phase-1 placeholder ID: "
            f"{placeholders}. Paste the real HeyGen look IDs before activating."
        )
    return tuple(looks)


def parse_studio(raw) -> StudioConfig:
    if raw is None:
        return StudioConfig()
    if not isinstance(raw, dict):
        raise ValueError("Profile anchor.studio must be a mapping")
    required_version = int(raw.get("required_version", 1))
    if required_version < 1:
        raise ValueError("Profile anchor.studio.required_version must be >= 1")
    return StudioConfig(
        asset_dir=str(raw.get("asset_dir") or "").strip(),
        manifest=str(raw.get("manifest") or "manifest.json").strip(),
        required_version=required_version,
    )


def parse_rights(raw) -> RightsConfig:
    if raw is None:
        return RightsConfig()
    if not isinstance(raw, dict):
        raise ValueError("Profile anchor.rights must be a mapping")
    return RightsConfig(
        consent_documented=_as_bool(raw.get("consent_documented"), default=False),
        consent_reference=str(raw.get("consent_reference") or "").strip(),
        permitted_channels=_as_str_tuple(raw.get("permitted_channels")),
        permitted_territories=_as_str_tuple(raw.get("permitted_territories")),
        revoked=_as_bool(raw.get("revoked"), default=False),
        ai_disclosure_required=_as_bool(raw.get("ai_disclosure_required"), default=True),
    )


def load_profile_anchor_config(profile_name: str, settings: Settings) -> dict:
    """Read stage_config.anchor for a profile, tolerating a missing profile."""
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        profile = _get_profile_registry(settings).get(profile_name or "bitcoin_podcast")
        profile_config = (profile.stage_config.get("anchor", {}) if profile else {}) or {}
    except Exception as exc:  # noqa: BLE001 - missing profile keeps global defaults
        logger.debug("Could not resolve anchor profile config; using settings: %s", exc)
        return {}
    if not isinstance(profile_config, dict):
        raise ValueError("Profile stage_config.anchor must be a mapping")
    return profile_config


def resolve_anchor_config(profile_name: str, settings: Settings) -> AnchorConfig:
    """Merge profile-owned avatar choices over backward-compatible settings."""
    profile_config = load_profile_anchor_config(profile_name, settings)

    provider = str(profile_config.get("provider") or settings.anchor_provider).strip().lower()
    provider = {"did": "d-id", "d_id": "d-id"}.get(provider, provider)
    max_cost_usd = float(profile_config.get("max_cost_usd", settings.anchor_max_cost_usd))
    if max_cost_usd < 0:
        raise ValueError("Anchor max_cost_usd must be non-negative")

    if provider == "d-id":
        return _resolve_did(profile_config, settings, max_cost_usd)
    if provider == "heygen":
        return _resolve_heygen(profile_config, settings, max_cost_usd)
    raise ValueError(f"Unsupported anchor provider: {provider!r}")


def _resolve_did(profile_config: dict, settings: Settings, max_cost_usd: float) -> AnchorConfig:
    engine = str(profile_config.get("engine") or "talks").strip().lower()
    if engine != "talks":
        raise ValueError(f"Unsupported D-ID anchor engine: {engine!r}")
    output_format = str(profile_config.get("output_format") or "mp4").strip().lower()
    if output_format != "mp4":
        raise ValueError("D-ID anchor output_format must be 'mp4'")
    cost_per_second = float(
        profile_config.get("cost_per_second_usd", settings.did_cost_per_second_usd)
    )
    if cost_per_second < 0:
        raise ValueError("D-ID cost_per_second_usd must be non-negative")
    return AnchorConfig(
        provider="d-id",
        engine=engine,
        source_image=str(profile_config.get("source_image") or settings.did_source_image_path),
        source_image_url=str(
            profile_config.get("source_image_url") or settings.did_source_image_url
        ),
        avatar_id="",
        avatar_type="photo_avatar",
        expression=str(profile_config.get("expression") or "serious"),
        output_format=output_format,
        resolution="",
        aspect_ratio="",
        cost_per_second_usd=cost_per_second,
        max_cost_usd=max_cost_usd,
    )


def _resolve_heygen(profile_config: dict, settings: Settings, max_cost_usd: float) -> AnchorConfig:
    from btcedu.services.anchor_service import heygen_cost_per_second

    engine = str(profile_config.get("engine") or settings.heygen_engine).strip().lower()
    if engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported HeyGen engine: {engine!r}")
    avatar_type = (
        str(profile_config.get("avatar_type") or settings.heygen_avatar_type).strip().lower()
    )

    configured_cost = profile_config.get("cost_per_second_usd")
    cost_per_second = (
        float(configured_cost)
        if configured_cost is not None
        else heygen_cost_per_second(engine, avatar_type)
    )
    # A zero rate is worse than a wrong one: it makes every budget projection
    # come out at zero, so the stage limit would never stop anything.
    if cost_per_second <= 0:
        raise ValueError(
            "HeyGen cost_per_second_usd must be greater than zero — a missing or "
            "zero rate would disable the budget preflight"
        )

    output_format = (
        str(profile_config.get("output_format") or settings.heygen_output_format).strip().lower()
    )
    if output_format not in {"mp4", "webm"}:
        raise ValueError("HeyGen output_format must be 'mp4' or 'webm'")

    studio_mode = str(profile_config.get("studio_mode") or "baked").strip().lower()
    if studio_mode not in VALID_STUDIO_MODES:
        raise ValueError(f"Unsupported anchor studio_mode: {studio_mode!r}")
    # Compositing needs the alpha channel that only the WebM container carries.
    if studio_mode == "composite" and output_format != "webm":
        raise ValueError(
            "studio_mode 'composite' requires output_format 'webm'; "
            "the presenter cannot be keyed out of an opaque MP4"
        )

    resolution = str(profile_config.get("resolution") or settings.heygen_resolution).strip().lower()
    if resolution not in {"720p", "1080p", "4k"}:
        raise ValueError(f"Unsupported HeyGen resolution: {resolution!r}")
    aspect_ratio = (
        str(profile_config.get("aspect_ratio") or settings.heygen_aspect_ratio).strip().lower()
    )
    if aspect_ratio not in {"16:9", "9:16", "4:5", "5:4", "1:1", "auto"}:
        raise ValueError(f"Unsupported HeyGen aspect_ratio: {aspect_ratio!r}")

    max_concurrent_jobs = int(profile_config.get("max_concurrent_jobs", 1))
    if max_concurrent_jobs < 1:
        raise ValueError("Anchor max_concurrent_jobs must be at least 1")
    if max_concurrent_jobs > HEYGEN_MAX_CONCURRENT_JOBS:
        raise ValueError(
            "Anchor max_concurrent_jobs exceeds HeyGen's documented ceiling of "
            f"{HEYGEN_MAX_CONCURRENT_JOBS} concurrent jobs"
        )

    rotation_strategy = (
        str(profile_config.get("rotation_strategy") or "least_recently_used").strip().lower()
    )
    if rotation_strategy not in VALID_ROTATION_STRATEGIES:
        raise ValueError(f"Unsupported anchor rotation_strategy: {rotation_strategy!r}")

    return AnchorConfig(
        provider="heygen",
        engine=engine,
        source_image="",
        source_image_url="",
        avatar_id=str(profile_config.get("avatar_id") or settings.heygen_avatar_id),
        avatar_type=avatar_type,
        expression="",
        output_format=output_format,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        cost_per_second_usd=cost_per_second,
        max_cost_usd=max_cost_usd,
        studio_mode=studio_mode,
        max_concurrent_jobs=max_concurrent_jobs,
        rotation_strategy=rotation_strategy,
        cost_source=str(profile_config.get("cost_source") or "").strip(),
        cost_checked_on=str(profile_config.get("cost_checked_on") or "").strip(),
        looks=parse_looks(profile_config.get("looks")),
        studio=parse_studio(profile_config.get("studio")),
        rights=parse_rights(profile_config.get("rights")),
    )
