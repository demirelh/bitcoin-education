"""Is ALMANYA24 actually allowed and able to generate a presenter today?

Everything the avatar path needs is checked here *before* anything is bought:
the profile's provider settings, the studio package, the pipeline wiring, and
the presenter's release. The default run touches no network and costs nothing,
which is what makes it usable as a pre-flight in a cron job rather than a thing
one runs when already suspicious.

The report is deliberately a list of independent results instead of the first
exception. Phase 1 of the ALMANYA24 assets is outstanding, so a real run today
is *expected* to end blocked — and the useful output in that situation is the
complete list of what is still missing, each with the action that clears it.

Severity is separate from status. A check is either informational, a warning or
blocking by nature; its result is PASS, WARNING or BLOCKED. That separation is
what lets ``--json`` consumers decide for themselves, and it keeps the exit code
derivable from the results rather than hand-maintained.
"""

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from btcedu.config import Settings

logger = logging.getLogger(__name__)

#: Bumped whenever the JSON shape changes in a way a consumer could notice.
REPORT_SCHEMA_VERSION = 1

STATUS_PASS = "PASS"
STATUS_WARNING = "WARNING"
STATUS_BLOCKED = "BLOCKED"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_BLOCKING = "blocking"

AREA_CONFIGURATION = "configuration"
AREA_STUDIO = "studio"
AREA_PIPELINE = "pipeline"
AREA_RIGHTS = "rights"
AREA_PROVIDER = "provider"

AREAS = (AREA_CONFIGURATION, AREA_STUDIO, AREA_PIPELINE, AREA_RIGHTS, AREA_PROVIDER)

#: Exit codes. Stable: scripts depend on them.
EXIT_READY = 0
EXIT_WARNINGS = 1
EXIT_BLOCKED = 2
EXIT_USAGE = 3

#: The hard stage budget the ALMANYA24 profile is expected to carry.
EXPECTED_ANCHOR_MAX_COST_USD = 7.0

#: A rate outside this range is almost certainly a typo — a decimal point in the
#: wrong place turns a seven-dollar ceiling into a seventy-cent or a
#: seven-hundred-dollar one, and neither is noticed until the invoice.
PLAUSIBLE_COST_PER_SECOND = (0.001, 0.5)


@dataclass(frozen=True)
class CheckResult:
    """One question asked of the configuration, and its answer."""

    check_id: str
    area: str
    severity: str
    status: str
    detail: str
    remedy: str = ""

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "area": self.area,
            "severity": self.severity,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class ReadinessReport:
    """Everything one readiness run found."""

    profile: str
    studio_mode: str
    online: bool
    results: list[CheckResult] = field(default_factory=list)

    def add(
        self,
        check_id: str,
        area: str,
        *,
        ok: bool,
        detail: str,
        remedy: str = "",
        severity: str = SEVERITY_BLOCKING,
    ) -> CheckResult:
        if ok:
            status = STATUS_PASS
        elif severity == SEVERITY_BLOCKING:
            status = STATUS_BLOCKED
        else:
            status = STATUS_WARNING
        result = CheckResult(
            check_id=check_id,
            area=area,
            severity=severity,
            status=status,
            detail=detail,
            remedy="" if ok else remedy,
        )
        self.results.append(result)
        return result

    @property
    def blocked(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == STATUS_BLOCKED]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == STATUS_WARNING]

    @property
    def passed(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == STATUS_PASS]

    @property
    def exit_code(self) -> int:
        if self.blocked:
            return EXIT_BLOCKED
        if self.warnings:
            return EXIT_WARNINGS
        return EXIT_READY

    def by_area(self, area: str) -> list[CheckResult]:
        return [r for r in self.results if r.area == area]

    def to_dict(self) -> dict:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "profile": self.profile,
            "studio_mode": self.studio_mode,
            "online": self.online,
            "exit_code": self.exit_code,
            "summary": {
                "pass": len(self.passed),
                "warning": len(self.warnings),
                "blocked": len(self.blocked),
            },
            "checks": [r.to_dict() for r in self.results],
        }


def _check_configuration(report: ReadinessReport, profile: str, settings: Settings):
    """Provider, engine, budget and the outfit pool. Returns the config or None."""
    from btcedu.core.anchor_config import PLACEHOLDER_LOOK_ID, resolve_anchor_config

    try:
        from btcedu.profiles import get_registry

        found = get_registry(settings).get(profile) is not None
    except Exception as exc:  # noqa: BLE001 - a broken registry is a config error
        found = False
        logger.debug("Profile registry failed: %s", exc)
    report.add(
        "config.profile_exists",
        AREA_CONFIGURATION,
        ok=found,
        detail=f"Profile {profile!r} {'found' if found else 'not found'}",
        remedy=f"Add btcedu/profiles/{profile}.yaml or check the --profile argument",
    )
    if not found:
        return None

    try:
        config = resolve_anchor_config(profile, settings)
    except ValueError as exc:
        report.add(
            "config.parses",
            AREA_CONFIGURATION,
            ok=False,
            detail=f"stage_config.anchor is invalid: {exc}",
            remedy="Fix the anchor block in the profile YAML",
        )
        return None
    report.add(
        "config.parses",
        AREA_CONFIGURATION,
        ok=True,
        detail="stage_config.anchor parses and validates",
    )

    report.add(
        "config.provider",
        AREA_CONFIGURATION,
        ok=config.provider == "heygen",
        detail=f"Anchor provider is {config.provider!r}",
        remedy="ALMANYA24 requires provider: heygen",
    )
    report.add(
        "config.engine",
        AREA_CONFIGURATION,
        ok=config.engine == "avatar_iii",
        detail=f"Engine is {config.engine!r}",
        remedy=(
            "Set engine: avatar_iii explicitly. Omitting it makes HeyGen default "
            "to Avatar IV, which is a different product at a different price."
        ),
    )
    report.add(
        "config.avatar_type",
        AREA_CONFIGURATION,
        ok=config.avatar_type in {"digital_twin", "photo_avatar", "studio_avatar"},
        detail=f"Avatar type is {config.avatar_type!r}",
        remedy=f"Unsupported avatar_type {config.avatar_type!r}",
        severity=SEVERITY_WARNING,
    )
    report.add(
        "config.anchor_enabled",
        AREA_CONFIGURATION,
        ok=bool(settings.anchor_enabled),
        detail=f"ANCHOR_ENABLED={settings.anchor_enabled}",
        remedy=(
            "Set ANCHOR_ENABLED=true when the presenter should actually be "
            "generated. Readiness reports this as a warning, not a fault: a "
            "deliberately disabled avatar path is a valid deployment."
        ),
        severity=SEVERITY_WARNING,
    )

    rate = config.cost_per_second_usd
    low, high = PLAUSIBLE_COST_PER_SECOND
    report.add(
        "config.cost_rate_positive",
        AREA_CONFIGURATION,
        ok=rate > 0,
        detail=f"cost_per_second_usd={rate}",
        remedy=(
            "A zero or missing rate makes every budget projection come out at "
            "zero, so the stage limit would never stop anything."
        ),
    )
    report.add(
        "config.cost_rate_plausible",
        AREA_CONFIGURATION,
        ok=low <= rate <= high,
        detail=f"cost_per_second_usd={rate} (plausible range {low}-{high})",
        remedy="Check the decimal point against the provider's published price list",
        severity=SEVERITY_WARNING,
    )
    report.add(
        "config.cost_documented",
        AREA_CONFIGURATION,
        ok=bool(config.cost_source and config.cost_checked_on),
        detail=(
            f"cost_source={'set' if config.cost_source else 'missing'}, "
            f"cost_checked_on={config.cost_checked_on or 'missing'}"
        ),
        remedy="Record where the rate came from and when it was last verified",
        severity=SEVERITY_WARNING,
    )

    report.add(
        "config.stage_budget_positive",
        AREA_CONFIGURATION,
        ok=config.max_cost_usd > 0,
        detail=f"anchor max_cost_usd={config.max_cost_usd}",
        remedy="A stage budget of zero blocks every clip before the first call",
    )
    report.add(
        "config.stage_budget_expected",
        AREA_CONFIGURATION,
        ok=abs(config.max_cost_usd - EXPECTED_ANCHOR_MAX_COST_USD) < 1e-9,
        detail=(
            f"anchor max_cost_usd={config.max_cost_usd}, "
            f"expected {EXPECTED_ANCHOR_MAX_COST_USD}"
        ),
        remedy=(
            f"ALMANYA24 is budgeted at {EXPECTED_ANCHOR_MAX_COST_USD} USD per episode "
            "for the presenter; change it deliberately or restore it"
        ),
        severity=SEVERITY_WARNING,
    )
    report.add(
        "config.episode_budget_covers_stage",
        AREA_CONFIGURATION,
        ok=settings.max_episode_cost_usd >= config.max_cost_usd,
        detail=(
            f"max_episode_cost_usd={settings.max_episode_cost_usd} vs "
            f"anchor max_cost_usd={config.max_cost_usd}"
        ),
        remedy=(
            "The episode ceiling is below the anchor stage budget, so the stage "
            "would be cut off mid-episode by the global guard instead"
        ),
    )

    looks = config.looks
    active = config.active_looks
    report.add(
        "config.look_pool_present",
        AREA_CONFIGURATION,
        ok=bool(looks),
        detail=f"{len(looks)} looks configured",
        remedy="Define anchor.looks in the profile",
    )
    report.add(
        "config.active_look",
        AREA_CONFIGURATION,
        ok=bool(active),
        detail=f"{len(active)} active looks",
        remedy=(
            "Paste the real HeyGen look IDs produced by asset phase 1 and set "
            "active: true on at least one of them"
        ),
    )

    all_ids = [look.avatar_look_id for look in looks]
    duplicate_ids = sorted(
        {i for i in all_ids if all_ids.count(i) > 1 and i != PLACEHOLDER_LOOK_ID}
    )
    report.add(
        "config.look_ids_unique",
        AREA_CONFIGURATION,
        ok=not duplicate_ids,
        detail=(
            f"duplicated look IDs: {duplicate_ids}" if duplicate_ids else "all look IDs are unique"
        ),
        remedy="Two looks with the same ID silently collapse the rotation into one outfit",
    )
    all_names = [look.name for look in looks]
    duplicate_names = sorted({n for n in all_names if all_names.count(n) > 1})
    report.add(
        "config.look_names_unique",
        AREA_CONFIGURATION,
        ok=not duplicate_names,
        detail=(
            f"duplicated look names: {duplicate_names}"
            if duplicate_names
            else "all look names are unique"
        ),
        remedy="Look names identify the assignment in the ledger and must be distinct",
    )
    placeholders = sorted(
        look.name for look in looks if look.is_placeholder or not look.avatar_look_id.strip()
    )
    report.add(
        "config.no_placeholder_looks",
        AREA_CONFIGURATION,
        ok=not placeholders,
        detail=(
            f"placeholder look IDs still present: {placeholders}"
            if placeholders
            else "no placeholder look IDs"
        ),
        remedy=(
            "Replace REPLACE_WITH_HEYGEN_LOOK_ID with the real IDs from asset "
            "phase 1. Until then the look pool is not usable."
        ),
    )
    # A global avatar_id would be used as the look for every scene, which is
    # exactly the "one invented avatar for everything" outcome the outfit
    # rotation exists to prevent.
    global_avatar = str(settings.heygen_avatar_id or "").strip()
    report.add(
        "config.no_global_avatar_fallback",
        AREA_CONFIGURATION,
        ok=bool(active) or not global_avatar,
        detail=(
            "look rotation drives the avatar ID"
            if active
            else (
                "HEYGEN_AVATAR_ID is set while no look is active"
                if global_avatar
                else "no active look and no global HEYGEN_AVATAR_ID to fall back to"
            )
        ),
        remedy=(
            "Without an active look the stage would fall back to the global "
            "HEYGEN_AVATAR_ID, defeating the one-outfit-per-episode rule"
        ),
    )
    report.add(
        "config.rotation_strategy",
        AREA_CONFIGURATION,
        ok=config.rotation_strategy in {"least_recently_used", "fixed"},
        detail=f"rotation_strategy={config.rotation_strategy!r}",
        remedy="Supported strategies are least_recently_used and fixed",
    )
    return config


def _check_studio(report: ReadinessReport, settings: Settings, config, studio_mode: str) -> None:
    from btcedu.core.studio_manifest import (
        ALPHA_MODES,
        StudioManifestError,
        load_studio_manifest,
        studio_manifest_path,
        studio_readiness_problems,
    )

    if studio_mode not in ALPHA_MODES:
        report.add(
            "studio.mode_valid",
            AREA_STUDIO,
            ok=False,
            detail=f"Unknown studio mode {studio_mode!r}",
            remedy=f"Choose one of {', '.join(ALPHA_MODES)}",
        )
        return
    report.add(
        "studio.mode_valid",
        AREA_STUDIO,
        ok=True,
        detail=f"Checking against studio mode {studio_mode!r}",
    )

    asset_dir = (config.studio.asset_dir if config else "").strip()
    if not asset_dir:
        report.add(
            "studio.configured",
            AREA_STUDIO,
            ok=False,
            detail="No anchor.studio.asset_dir configured",
            remedy="Point anchor.studio.asset_dir at the versioned studio package",
        )
        return
    report.add(
        "studio.configured",
        AREA_STUDIO,
        ok=True,
        detail=f"Studio package: {asset_dir}",
    )

    manifest_path = studio_manifest_path(asset_dir, config.studio.manifest)
    if not manifest_path.is_file():
        report.add(
            "studio.manifest_present",
            AREA_STUDIO,
            ok=False,
            detail=f"No production studio manifest at {asset_dir}/{config.studio.manifest}",
            remedy=(
                "Asset phase 1 produces the studio. Until it lands only "
                "manifest.example.json exists, and that is on purpose: a "
                "production manifest must never describe files nobody made."
            ),
        )
        return
    report.add(
        "studio.manifest_present",
        AREA_STUDIO,
        ok=True,
        detail=f"Studio manifest found at {asset_dir}/{config.studio.manifest}",
    )

    try:
        manifest = load_studio_manifest(manifest_path)
    except StudioManifestError as exc:
        report.add(
            "studio.manifest_valid",
            AREA_STUDIO,
            ok=False,
            detail=f"Studio manifest is invalid: {exc}",
            remedy="Fix the manifest against docs and the schema in core/studio_manifest.py",
        )
        return
    report.add(
        "studio.manifest_valid",
        AREA_STUDIO,
        ok=True,
        detail=(
            f"Studio {manifest.name!r} version {manifest.studio_version} "
            f"(assets {manifest.asset_version})"
        ),
    )

    problems = studio_readiness_problems(manifest)
    report.add(
        "studio.assets_present",
        AREA_STUDIO,
        ok=not problems,
        detail=(
            "; ".join(problems[:6]) if problems else "every declared asset exists and matches"
        ),
        remedy="Complete the studio package before enabling the avatar path",
    )

    width, height = _render_frame(settings)
    report.add(
        "studio.frame_matches_renderer",
        AREA_STUDIO,
        ok=(manifest.width, manifest.height) == (width, height),
        detail=(
            f"studio {manifest.width}x{manifest.height} vs renderer {width}x{height}"
        ),
        remedy="The studio plate and RENDER_RESOLUTION must describe the same frame",
    )
    report.add(
        "studio.fps_matches_renderer",
        AREA_STUDIO,
        ok=manifest.fps == int(settings.render_fps),
        detail=f"studio {manifest.fps} fps vs renderer {settings.render_fps} fps",
        remedy="Mismatched frame rates make the composited presenter drift against the audio",
    )

    zone = manifest.display_zone
    report.add(
        "studio.display_zone",
        AREA_STUDIO,
        ok=zone.rect.width > 0 and zone.rect.height > 0,
        detail=f"display zone {zone.rect.width}x{zone.rect.height} at "
        f"({zone.rect.x},{zone.rect.y})",
        remedy="The monitor needs a positive rectangle to receive the topic medium",
    )
    safe = manifest.safe_areas
    safe_ok = all(
        rect.width > 0 and rect.height > 0
        for rect in (safe.lower_third, safe.ticker, safe.subtitle)
    )
    report.add(
        "studio.safe_areas",
        AREA_STUDIO,
        ok=safe_ok,
        detail="lower third, ticker and subtitle safe areas are declared"
        if safe_ok
        else "at least one safe area is empty",
        remedy="Text areas need real rectangles or the monitor may cover the captions",
    )
    report.add(
        "studio.fallback_graphic",
        AREA_STUDIO,
        ok=manifest.fallback_display_media is not None,
        detail=(
            "neutral fallback graphic declared"
            if manifest.fallback_display_media is not None
            else "no fallback_display_media"
        ),
        remedy="A scene whose topic medium is missing must still show something deliberate",
    )

    if studio_mode == "opaque_mp4":
        # In the opaque fallback the presenter is baked into the clip, so a
        # monitor overlay can only be placed where the presenter provably is
        # not. Without that proof there is no safe placement at all.
        has_mask = manifest.occlusion_mask is not None
        report.add(
            "studio.opaque_safe_zone",
            AREA_STUDIO,
            ok=bool(zone.presenter_free or has_mask),
            detail=(
                f"presenter_free={zone.presenter_free}, "
                f"occlusion_mask={'present' if has_mask else 'absent'}"
            ),
            remedy=(
                "Mark the display zone presenter_free, or supply an occlusion "
                "mask. Otherwise the topic monitor may land on the presenter's face."
            ),
        )

    _check_ffmpeg(report, studio_mode)


def _render_frame(settings: Settings) -> tuple[int, int]:
    raw = str(settings.render_resolution or "1920x1080").lower()
    try:
        width, height = (int(part) for part in raw.split("x", 1))
    except (TypeError, ValueError):
        return (0, 0)
    return width, height


def _check_ffmpeg(report: ReadinessReport, studio_mode: str) -> None:
    from btcedu.services.studio_compositor import has_perspective_filter

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    report.add(
        "ffmpeg.present",
        AREA_STUDIO,
        ok=bool(ffmpeg and ffprobe),
        detail=f"ffmpeg={'yes' if ffmpeg else 'no'}, ffprobe={'yes' if ffprobe else 'no'}",
        remedy="Install ffmpeg; the compositor and every probe depend on it",
    )
    if not ffmpeg:
        return

    filters = _ffmpeg_filters()
    required = {"overlay", "scale", "crop", "colorkey" if studio_mode == "opaque_mp4" else "format"}
    missing = sorted(name for name in required if name not in filters)
    report.add(
        "ffmpeg.filters",
        AREA_STUDIO,
        ok=not missing,
        detail=(
            f"missing filters for {studio_mode}: {missing}"
            if missing
            else "required filters present"
        ),
        remedy="Rebuild or reinstall ffmpeg with the standard filter set",
    )
    report.add(
        "ffmpeg.perspective",
        AREA_STUDIO,
        ok=has_perspective_filter(),
        detail=(
            "perspective filter available"
            if has_perspective_filter()
            else "no perspective filter: a tilted monitor falls back to a plain rectangle"
        ),
        remedy="Cosmetic only. The monitor is filled as a rectangle without it.",
        severity=SEVERITY_WARNING,
    )
    if studio_mode == "alpha_webm":
        # Only *decoding* matters. Production never encodes alpha: HeyGen
        # delivers it and the finished bulletin is opaque H.264.
        decoders = _ffmpeg_decoders()
        can_decode = bool({"vp8", "vp9", "libvpx", "libvpx-vp9"} & decoders)
        report.add(
            "ffmpeg.alpha_decode",
            AREA_STUDIO,
            ok=can_decode,
            detail=(
                "VP8/VP9 decoding available for transparent WebM"
                if can_decode
                else "no VP8/VP9 decoder: a transparent HeyGen WebM cannot be read"
            ),
            remedy="Install an ffmpeg build with libvpx decoding",
        )


def _ffmpeg_filters() -> set[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(" -"):
            names.add(parts[1])
    return names


def _ffmpeg_decoders() -> set[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def _check_pipeline(report: ReadinessReport, profile: str, settings: Settings) -> None:
    from btcedu.core.pipeline import _V2_STAGES

    names = [name for name, _ in _V2_STAGES]
    for stage in ("sceneplan", "anchorgen", "render"):
        report.add(
            f"pipeline.{stage}_present",
            AREA_PIPELINE,
            ok=stage in names,
            detail=f"stage {stage!r} {'is' if stage in names else 'is not'} in the v2 pipeline",
            remedy=f"Restore the {stage} stage in core/pipeline.py",
        )
    ordered = all(stage in names for stage in ("sceneplan", "anchorgen", "render"))
    if ordered:
        report.add(
            "pipeline.stage_order",
            AREA_PIPELINE,
            ok=names.index("sceneplan") < names.index("anchorgen") < names.index("render"),
            detail=" -> ".join(["sceneplan", "anchorgen", "render"]),
            remedy=(
                "The shot list must exist before clips are ordered, and the clips "
                "before the render that composites them"
            ),
        )

    try:
        from btcedu.core.presenter_assignment import ASSIGNMENT_FILENAME

        report.add(
            "pipeline.presenter_assignment",
            AREA_PIPELINE,
            ok=bool(ASSIGNMENT_FILENAME),
            detail=f"presenter assignment artifact: {ASSIGNMENT_FILENAME}",
        )
    except Exception as exc:  # noqa: BLE001
        report.add(
            "pipeline.presenter_assignment",
            AREA_PIPELINE,
            ok=False,
            detail=f"presenter assignment unavailable: {exc}",
            remedy="core/presenter_assignment.py must be importable",
        )

    report.add(
        "pipeline.remote_render_ships_studio",
        AREA_PIPELINE,
        ok=_remote_render_can_ship_studio(),
        detail="remote render declares studio assets"
        if _remote_render_can_ship_studio()
        else "remote render cannot declare studio assets",
        remedy="core/remote_render.py must expose scene_job_requirements()",
    )

    profile_data = _profile_dict(profile, settings)
    auto_publish = bool(profile_data.get("auto_publish", False))
    report.add(
        "pipeline.auto_publish_off",
        AREA_PIPELINE,
        ok=not auto_publish,
        detail=f"auto_publish={auto_publish}",
        remedy=(
            "A synthetic presenter must not reach a channel without a human "
            "pressing publish. Set auto_publish: false."
        ),
    )
    youtube = profile_data.get("youtube") or {}
    target = str(youtube.get("publish_target") or "").strip()
    privacy = str(
        ((youtube.get("targets") or {}).get(target) or {}).get("default_privacy") or ""
    ).strip()
    report.add(
        "pipeline.private_test_target",
        AREA_PIPELINE,
        ok=target == "test" and privacy == "private",
        detail=f"publish_target={target!r}, default_privacy={privacy!r}",
        remedy=(
            "Point youtube.publish_target at a test target whose default_privacy "
            "is private until the presenter has been reviewed on a real channel"
        ),
        severity=SEVERITY_WARNING,
    )
    report.add(
        "pipeline.final_review_active",
        AREA_PIPELINE,
        ok="review_gate_3" in [name for name, _ in _V2_STAGES],
        detail="review_gate_3 runs before publish",
        remedy="The final review gate must stay in the pipeline",
    )


def _profile_dict(profile: str, settings: Settings) -> dict:
    try:
        import yaml

        path = Path(__file__).resolve().parent.parent / "profiles" / f"{profile}.yaml"
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - readiness must not die on YAML
        logger.debug("Could not read profile YAML for %s: %s", profile, exc)
        return {}


def _remote_render_can_ship_studio() -> bool:
    try:
        from btcedu.core.remote_render import scene_job_requirements

        return callable(scene_job_requirements)
    except Exception:  # noqa: BLE001
        return False


def _check_ledger(report: ReadinessReport, settings: Settings, session) -> None:
    from btcedu.core.avatar_jobs import unresolved_jobs

    if session is None:
        report.add(
            "ledger.table_present",
            AREA_PIPELINE,
            ok=False,
            detail="No database session available",
            remedy="Run from an initialised deployment so the ledger can be inspected",
            severity=SEVERITY_WARNING,
        )
        return
    try:
        pending = unresolved_jobs(session)
    except Exception as exc:  # noqa: BLE001 - a missing table is the finding
        report.add(
            "ledger.table_present",
            AREA_PIPELINE,
            ok=False,
            detail=f"avatar_jobs is not queryable: {exc}",
            remedy="Run `btcedu migrate` to create the avatar job ledger",
        )
        return
    report.add(
        "ledger.table_present",
        AREA_PIPELINE,
        ok=True,
        detail="avatar_jobs ledger is present",
    )
    report.add(
        "ledger.no_unresolved_jobs",
        AREA_PIPELINE,
        ok=not pending,
        detail=(
            f"{len(pending)} jobs awaiting reconciliation: "
            + ", ".join(f"{j.episode_id}/{j.scene_id}" for j in pending[:5])
            if pending
            else "no reserved or reconcile_required jobs"
        ),
        remedy="Resolve them with `btcedu avatar-reconcile list` before generating more",
        severity=SEVERITY_WARNING,
    )


def _check_rights(report: ReadinessReport, config, today: date | None = None) -> None:
    from btcedu.core.anchor_rights import (
        RightsRecordError,
        load_rights_record,
        rights_problems,
    )

    rights = config.rights if config else None
    if rights is None:
        return

    if rights.revoked:
        report.add(
            "rights.profile_not_revoked",
            AREA_RIGHTS,
            ok=False,
            detail="anchor.rights.revoked is true in the profile",
            remedy="Consent has been withdrawn; no further generation is permitted",
        )
        return
    report.add(
        "rights.profile_not_revoked",
        AREA_RIGHTS,
        ok=True,
        detail="profile does not mark the release as revoked",
    )

    record_file = (rights.record_file or "").strip()
    if not record_file:
        report.add(
            "rights.record_configured",
            AREA_RIGHTS,
            ok=False,
            detail="No anchor.rights.record_file configured",
            remedy=(
                "Point anchor.rights.record_file at the release record. It is kept "
                "outside this repository; see assets/almanya24/rights/README.md."
            ),
        )
        return
    report.add(
        "rights.record_configured",
        AREA_RIGHTS,
        ok=True,
        detail="release record location is configured",
    )

    try:
        record = load_rights_record(record_file)
    except RightsRecordError as exc:
        report.add(
            "rights.record_valid",
            AREA_RIGHTS,
            ok=False,
            # The message may name the file; the file name is operational, its
            # contents are not, and nothing from inside it is echoed here.
            detail=f"release record unusable: {exc}",
            remedy="Create or repair the record from anchor-rights.example.json",
        )
        return
    report.add(
        "rights.record_valid",
        AREA_RIGHTS,
        ok=True,
        detail=(
            f"release record v{record.record_version} for presenter "
            f"{record.presenter_rights_id!r}"
        ),
    )

    problems = rights_problems(
        record,
        channel=rights.channel,
        territory=rights.territory,
        today=today,
    )
    report.add(
        "rights.permits_generation",
        AREA_RIGHTS,
        ok=not problems,
        detail="; ".join(problems) if problems else "consent, scope and validity all check out",
        remedy="Every one of these must be cleared before a presenter may be generated",
    )
    report.add(
        "rights.ai_disclosure",
        AREA_RIGHTS,
        ok=bool(record.ai_disclosure_text) or not rights.ai_disclosure_required,
        detail=(
            "AI disclosure text configured"
            if record.ai_disclosure_text
            else "no AI disclosure text"
        ),
        remedy="A synthetic presenter must be declared as synthetic to the audience",
    )


def _check_provider_online(report: ReadinessReport, settings: Settings, config) -> None:
    from btcedu.services.heygen_readonly import (
        HeyGenReadOnlyClient,
        HeyGenReadOnlyError,
    )

    api_key = str(settings.heygen_api_key or "").strip()
    if not api_key:
        report.add(
            "provider.api_key",
            AREA_PROVIDER,
            ok=False,
            detail="HEYGEN_API_KEY is not configured",
            remedy="Set HEYGEN_API_KEY in .env to use --online",
        )
        return

    client = HeyGenReadOnlyClient(api_key)
    try:
        client.authenticate()
    except HeyGenReadOnlyError as exc:
        report.add(
            "provider.api_key",
            AREA_PROVIDER,
            ok=False,
            detail=f"{exc.kind}: {exc}",
            remedy=exc.remedy,
            severity=SEVERITY_BLOCKING if exc.kind == "auth" else SEVERITY_WARNING,
        )
        return
    report.add(
        "provider.api_key",
        AREA_PROVIDER,
        ok=True,
        detail="API key authenticated against a read-only endpoint",
    )

    for look in config.active_looks:
        try:
            avatar = client.look(look.avatar_look_id)
        except HeyGenReadOnlyError as exc:
            report.add(
                f"provider.look.{look.name}",
                AREA_PROVIDER,
                ok=False,
                detail=f"look {look.name!r}: {exc.kind}: {exc}",
                remedy=exc.remedy,
                severity=SEVERITY_BLOCKING if exc.kind in {"auth", "missing"} else SEVERITY_WARNING,
            )
            continue

        report.add(
            f"provider.look.{look.name}",
            AREA_PROVIDER,
            ok=True,
            detail=f"look {look.name!r} exists at the provider",
        )
        expected_avatar = str(config.avatar_id or "").strip()
        if expected_avatar:
            report.add(
                f"provider.look_owner.{look.name}",
                AREA_PROVIDER,
                ok=avatar.avatar_id in ("", expected_avatar),
                detail=(
                    f"look {look.name!r} belongs to avatar {avatar.avatar_id or 'unreported'!r}, "
                    f"expected {expected_avatar!r}"
                ),
                remedy="The look must belong to the configured presenter avatar",
            )
        # The provider may or may not report its engines. Absent is reported as
        # unknown; inventing a field would turn a silence into a false pass.
        if avatar.supported_api_engines is None:
            report.add(
                f"provider.look_engine.{look.name}",
                AREA_PROVIDER,
                ok=True,
                detail=f"look {look.name!r}: provider did not report supported_api_engines",
                severity=SEVERITY_INFO,
            )
        else:
            report.add(
                f"provider.look_engine.{look.name}",
                AREA_PROVIDER,
                ok=config.engine in avatar.supported_api_engines,
                detail=(
                    f"look {look.name!r} supports {sorted(avatar.supported_api_engines)}, "
                    f"profile requests {config.engine!r}"
                ),
                remedy="Retrain or reselect the look for Avatar III",
            )


def evaluate_readiness(
    profile: str,
    settings: Settings,
    *,
    studio_mode: str = "alpha_webm",
    online: bool = False,
    session=None,
    today: date | None = None,
) -> ReadinessReport:
    """Run every check. Offline by default and free in every mode."""
    report = ReadinessReport(profile=profile, studio_mode=studio_mode, online=online)
    config = _check_configuration(report, profile, settings)
    _check_studio(report, settings, config, studio_mode)
    _check_pipeline(report, profile, settings)
    _check_ledger(report, settings, session)
    _check_rights(report, config, today=today)
    if online and config is not None:
        _check_provider_online(report, settings, config)
    return report


def format_report(report: ReadinessReport) -> str:
    """Human-readable output, grouped by area and never quoting a secret."""
    lines: list[str] = []
    header = f"ALMANYA24 anchor readiness — profile {report.profile}, studio {report.studio_mode}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append(
        "Mode: online read-only checks included"
        if report.online
        else "Mode: offline only (no network, no cost)"
    )
    lines.append("")

    for area in AREAS:
        results = report.by_area(area)
        if not results:
            continue
        lines.append(f"[{area.upper()}]")
        for result in results:
            lines.append(f"  {result.status:<8} {result.check_id}: {result.detail}")
            if result.remedy and result.status != STATUS_PASS:
                lines.append(f"           -> {result.remedy}")
        lines.append("")

    lines.append(
        f"Summary: {len(report.passed)} pass, {len(report.warnings)} warning, "
        f"{len(report.blocked)} blocked"
    )
    if report.blocked:
        lines.append("Result: BLOCKED — the avatar path must not be enabled yet.")
    elif report.warnings:
        lines.append("Result: WARNINGS — technically able to start, but read them first.")
    else:
        lines.append("Result: READY.")
    return "\n".join(lines)


def online_notice() -> str:
    """Printed before any network access, so the user sees the promise first."""
    return (
        "--online performs read-only HeyGen requests only: authentication and "
        "look lookup. Nothing is uploaded, no video is generated, and no billable "
        "operation is issued."
    )


def resolve_studio_mode(config, requested: str | None) -> str:
    """Explicit choice wins; otherwise derive it from the profile."""
    if requested:
        return requested.strip().lower()
    if config is None:
        return "alpha_webm"
    if getattr(config, "studio_mode", "") == "composite":
        return "alpha_webm"
    return "opaque_mp4"
