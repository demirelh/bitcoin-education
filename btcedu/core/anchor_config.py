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

# HeyGen does not publish a concurrency figure that applies to every plan, and
# the account this profile will run on has not been measured. Absent a
# confirmed provider limit the cap is deliberately conservative: exceeding an
# unknown ceiling shows up as 429s on paid calls, which is the one class of
# mistake this whole subsystem exists to avoid. Raise it only against a figure
# the provider actually stated for the account in use.
HEYGEN_MAX_CONCURRENT_JOBS = 5

# Bounds for the polling and timeout knobs. A one-second poll would hammer the
# API for no benefit; a ten-minute one would idle away the concurrency gain.
MIN_POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_INTERVAL_SECONDS = 120.0
MIN_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_REQUEST_TIMEOUT_SECONDS = 600.0
MIN_POLL_TIMEOUT_SECONDS = 60.0
MAX_POLL_TIMEOUT_SECONDS = 3 * 60 * 60

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
    # Path to the machine-readable release record. It lives *outside* the
    # repository: the profile only says where to look, never what it says.
    record_file: str = ""
    # What this profile actually publishes to, so the release can be checked
    # against a concrete channel and territory rather than in the abstract.
    channel: str = ""
    territory: str = ""


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
    #: How often a running generation is asked whether it is done.
    poll_interval_seconds: float = 5.0
    #: Per-HTTP-call timeout for every provider request.
    request_timeout_seconds: float = 120.0
    #: How long one clip may stay in "processing" before the run stops waiting
    #: and leaves it to be resumed. Not a failure — see the coordinator.
    poll_timeout_seconds: float = 1800.0
    rotation_strategy: str = "least_recently_used"
    cost_source: str = ""
    cost_checked_on: str = ""
    looks: tuple[PresenterLook, ...] = ()
    studio: StudioConfig = field(default_factory=StudioConfig)
    rights: RightsConfig = field(default_factory=RightsConfig)
    #: Whether the presenter clips need an explicit human approval before the
    #: renderer may use them. Its own switch rather than a reuse of
    #: ``auto_approve_reviews``: a profile that runs unattended for editorial
    #: gates may still want a person to look at the face on screen, and the two
    #: decisions have nothing to do with each other.
    review_required: bool = False

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


def _positive_int(value, field_name: str) -> int:
    """Reject a value that is not a whole number of at least one.

    Strict on purpose: ``max_concurrent_jobs: "3 "`` from a hand-edited profile
    should fail loudly at load time, not silently become something else on the
    evening a bulletin has to go out.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Anchor {field_name} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"Anchor {field_name} must be at least 1")
    return parsed


def _bounded_float(value, field_name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Anchor {field_name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(
            f"Anchor {field_name} must be between {minimum} and {maximum} (got {parsed})"
        )
    return parsed


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
        record_file=str(raw.get("record_file") or "").strip(),
        channel=str(raw.get("channel") or "").strip(),
        territory=str(raw.get("territory") or "").strip(),
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

    max_concurrent_jobs = _positive_int(
        profile_config.get("max_concurrent_jobs", 1), "max_concurrent_jobs"
    )
    if max_concurrent_jobs > HEYGEN_MAX_CONCURRENT_JOBS:
        raise ValueError(
            f"Anchor max_concurrent_jobs of {max_concurrent_jobs} exceeds the "
            f"conservative ceiling of {HEYGEN_MAX_CONCURRENT_JOBS} concurrent jobs "
            "for HeyGen; no higher provider limit has been confirmed for this account"
        )

    poll_interval_seconds = _bounded_float(
        profile_config.get("poll_interval_seconds", 5.0),
        "poll_interval_seconds",
        MIN_POLL_INTERVAL_SECONDS,
        MAX_POLL_INTERVAL_SECONDS,
    )
    request_timeout_seconds = _bounded_float(
        profile_config.get("request_timeout_seconds", 120.0),
        "request_timeout_seconds",
        MIN_REQUEST_TIMEOUT_SECONDS,
        MAX_REQUEST_TIMEOUT_SECONDS,
    )
    poll_timeout_seconds = _bounded_float(
        profile_config.get("poll_timeout_seconds", 1800.0),
        "poll_timeout_seconds",
        MIN_POLL_TIMEOUT_SECONDS,
        MAX_POLL_TIMEOUT_SECONDS,
    )
    if poll_timeout_seconds <= poll_interval_seconds:
        raise ValueError(
            "Anchor poll_timeout_seconds must be larger than poll_interval_seconds"
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
        poll_interval_seconds=poll_interval_seconds,
        request_timeout_seconds=request_timeout_seconds,
        poll_timeout_seconds=poll_timeout_seconds,
        rotation_strategy=rotation_strategy,
        cost_source=str(profile_config.get("cost_source") or "").strip(),
        cost_checked_on=str(profile_config.get("cost_checked_on") or "").strip(),
        looks=parse_looks(profile_config.get("looks")),
        studio=parse_studio(profile_config.get("studio")),
        rights=parse_rights(profile_config.get("rights")),
        review_required=_as_bool(profile_config.get("review_required"), False),
    )
