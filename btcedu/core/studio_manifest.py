"""The ALMANYA24 studio, described as data instead of as code.

A bulletin has to look the same every evening. The way to get that is not to
teach the renderer what the studio looks like, but to write the studio down
once and let the renderer read it: where the wall is, where the monitor sits in
it, where the presenter stands, and which strips of the frame belong to the
lower third, the ticker and the subtitles and must therefore stay clear.

Everything here is deliberately strict. A studio manifest that is merely
*plausible* produces a bulletin that is subtly wrong in a way nobody notices
until it is published -- a monitor over the presenter's face, a ticker behind
the desk, subtitles off the bottom of the screen. So every rule that can be
checked is checked, and anything unclear is refused rather than guessed.

Two rules deserve their own sentence.

Asset paths are resolved strictly inside the studio directory. A manifest is an
input file, and an input file that can name ``/etc/passwd`` or ``../../.env`` is
a way to read the disk, not a way to describe a studio.

A manifest may declare itself a placeholder. Phase 1 of the ALMANYA24 asset work
has not happened, so the only manifest in the repository describes assets that
do not exist. Readiness fails loudly on such a manifest instead of letting a
render run into missing files halfway through.
"""

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
EXAMPLE_MANIFEST_FILENAME = "manifest.example.json"

ALPHA_MODE_WEBM = "alpha_webm"
ALPHA_MODE_OPAQUE = "opaque_mp4"
ALPHA_MODES = (ALPHA_MODE_WEBM, ALPHA_MODE_OPAQUE)

FIT_COVER = "cover"
FIT_CONTAIN = "contain"
FIT_MODES = (FIT_COVER, FIT_CONTAIN)

ASSET_KIND_IMAGE = "image"
ASSET_KIND_VIDEO = "video"
ASSET_KINDS = (ASSET_KIND_IMAGE, ASSET_KIND_VIDEO)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}

# Relative POSIX paths only: letters, digits, dot, dash, underscore, slash. This
# also keeps every value out of shell and ffmpeg-filter metacharacter territory.
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class StudioManifestError(ValueError):
    """The studio description is unusable. Never raised as a warning."""


class StudioNotReadyError(RuntimeError):
    """The studio is described but its assets are not on disk (or are fakes)."""


@dataclass(frozen=True)
class Rect:
    """An axis-aligned area of the frame, in pixels."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def overlaps(self, other: "Rect") -> bool:
        return not (
            self.right <= other.x
            or other.right <= self.x
            or self.bottom <= other.y
            or other.bottom <= self.y
        )


@dataclass(frozen=True)
class StudioAsset:
    """One file belonging to the studio, named relative to the studio directory."""

    path: str
    kind: str
    sha256: str = ""
    placeholder: bool = False


@dataclass(frozen=True)
class DisplayZone:
    """The monitor in the studio wall that carries the evening's topic media.

    ``corners`` is optional. When present it holds the four image-space corners
    of the monitor in top-left, top-right, bottom-right, bottom-left order, so a
    wall that is not shot head-on still gets a picture that sits *in* it rather
    than pasted flat on top of it.
    """

    zone_id: str
    rect: Rect
    fit_mode: str = FIT_COVER
    focus_point: tuple[float, float] = (0.5, 0.5)
    corners: tuple[tuple[int, int], ...] = ()
    presenter_free: bool = False
    z_order: int = 10


@dataclass(frozen=True)
class PresenterPlacement:
    """Where the avatar stands and how large it is drawn.

    ``anchor_x``/``anchor_y`` name the point in the frame that the clip's
    bottom-centre is placed on, because that is the part of a presenter shot
    that must stay put: the feet or the desk edge, not the top of the head.
    """

    anchor_x: int
    anchor_y: int
    scale: float = 1.0
    z_order: int = 20


@dataclass(frozen=True)
class SafeAreas:
    """Strips of the frame that belong to text and must not be covered."""

    lower_third: Rect
    ticker: Rect
    subtitle: Rect


@dataclass(frozen=True)
class StudioManifest:
    """A complete, validated description of one studio version."""

    schema_version: int
    studio_version: str
    asset_version: str
    name: str
    width: int
    height: int
    fps: int
    alpha_mode: str
    background: StudioAsset
    display_zone: DisplayZone
    presenter: PresenterPlacement
    logo_zone: Rect
    safe_areas: SafeAreas
    intro_asset: StudioAsset | None = None
    loop_asset: StudioAsset | None = None
    shadow_layer: StudioAsset | None = None
    foreground_layer: StudioAsset | None = None
    occlusion_mask: StudioAsset | None = None
    fallback_display_media: StudioAsset | None = None
    placeholder: bool = False
    provenance: dict = field(default_factory=dict)
    base_dir: Path = field(default=Path("."), compare=False)

    @property
    def frame(self) -> Rect:
        return Rect(0, 0, self.width, self.height)

    def assets(self) -> list[StudioAsset]:
        """Every declared file, in a stable order."""
        candidates = [
            self.background,
            self.intro_asset,
            self.loop_asset,
            self.shadow_layer,
            self.foreground_layer,
            self.occlusion_mask,
            self.fallback_display_media,
        ]
        return [asset for asset in candidates if asset is not None]

    def asset_path(self, asset: StudioAsset) -> Path:
        return resolve_studio_asset(self.base_dir, asset.path)


def resolve_studio_asset(base_dir: str | Path, relative: str) -> Path:
    """Resolve a manifest path, refusing anything that leaves the studio.

    Symlinks are resolved before the containment check, so a link inside the
    studio directory cannot be used as a door out of it.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise StudioManifestError("Asset path must be a non-empty string")
    if not _SAFE_RELATIVE_PATH.match(relative):
        raise StudioManifestError(
            f"Unsafe asset path {relative!r}: only relative paths made of "
            "letters, digits, '.', '_', '-' and '/' are allowed"
        )
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise StudioManifestError(f"Asset path escapes the studio directory: {relative!r}")

    root = Path(base_dir).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise StudioManifestError(f"Asset path escapes the studio directory: {relative!r}")
    return candidate


def _require(data: dict, key: str, context: str):
    if key not in data:
        raise StudioManifestError(f"{context} is missing required field {key!r}")
    return data[key]


def _as_int(value, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudioManifestError(f"{context} must be a number, got {value!r}")
    if float(value) != int(value):
        raise StudioManifestError(f"{context} must be a whole number of pixels, got {value!r}")
    return int(value)


def _as_float(value, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StudioManifestError(f"{context} must be a number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise StudioManifestError(f"{context} must be a finite number, got {value!r}")
    return number


def _parse_rect(data, context: str) -> Rect:
    if not isinstance(data, dict):
        raise StudioManifestError(f"{context} must be an object with x, y, width, height")
    rect = Rect(
        x=_as_int(_require(data, "x", context), f"{context}.x"),
        y=_as_int(_require(data, "y", context), f"{context}.y"),
        width=_as_int(_require(data, "width", context), f"{context}.width"),
        height=_as_int(_require(data, "height", context), f"{context}.height"),
    )
    if rect.width <= 0 or rect.height <= 0:
        raise StudioManifestError(f"{context} must have a positive width and height")
    if rect.x < 0 or rect.y < 0:
        raise StudioManifestError(f"{context} must not start outside the frame")
    return rect


def _guess_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return ASSET_KIND_IMAGE
    if suffix in VIDEO_SUFFIXES:
        return ASSET_KIND_VIDEO
    raise StudioManifestError(f"Cannot tell whether {path!r} is an image or a video")


def _parse_asset(data, context: str, *, allowed_kinds=ASSET_KINDS) -> StudioAsset:
    if isinstance(data, str):
        data = {"path": data}
    if not isinstance(data, dict):
        raise StudioManifestError(f"{context} must be a path or an object with a 'path'")

    path = _require(data, "path", context)
    if not isinstance(path, str):
        raise StudioManifestError(f"{context}.path must be a string")

    kind = data.get("kind") or _guess_kind(path)
    if kind not in allowed_kinds:
        raise StudioManifestError(
            f"{context}.kind must be one of {allowed_kinds}, got {kind!r}"
        )

    sha256 = data.get("sha256", "")
    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
        raise StudioManifestError(f"{context}.sha256 must be a 64-character hex digest")

    return StudioAsset(
        path=path,
        kind=kind,
        sha256=str(sha256),
        placeholder=bool(data.get("placeholder", False)),
    )


def _parse_display_zone(data, frame: Rect) -> DisplayZone:
    context = "display_zone"
    if not isinstance(data, dict):
        raise StudioManifestError("display_zone must be an object")

    zone_id = str(_require(data, "zone_id", context))
    if not zone_id.strip():
        raise StudioManifestError("display_zone.zone_id must not be empty")

    rect = _parse_rect(_require(data, "rect", context), f"{context}.rect")
    if rect.right > frame.width or rect.bottom > frame.height:
        raise StudioManifestError("display_zone.rect must lie inside the frame")

    fit_mode = str(data.get("fit_mode", FIT_COVER))
    if fit_mode not in FIT_MODES:
        raise StudioManifestError(f"display_zone.fit_mode must be one of {FIT_MODES}")

    focus = data.get("focus_point", [0.5, 0.5])
    if not isinstance(focus, (list, tuple)) or len(focus) != 2:
        raise StudioManifestError("display_zone.focus_point must be a pair [x, y]")
    focus_point = (
        _as_float(focus[0], "display_zone.focus_point.x"),
        _as_float(focus[1], "display_zone.focus_point.y"),
    )
    if not all(0.0 <= value <= 1.0 for value in focus_point):
        raise StudioManifestError("display_zone.focus_point values must be within 0.0..1.0")

    corners_raw = data.get("corners") or []
    if corners_raw and (not isinstance(corners_raw, list) or len(corners_raw) != 4):
        raise StudioManifestError(
            "display_zone.corners must hold exactly four points "
            "(top-left, top-right, bottom-right, bottom-left)"
        )
    corners: list[tuple[int, int]] = []
    for index, point in enumerate(corners_raw):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise StudioManifestError(f"display_zone.corners[{index}] must be a pair [x, y]")
        px = _as_int(point[0], f"display_zone.corners[{index}].x")
        py = _as_int(point[1], f"display_zone.corners[{index}].y")
        if not (0 <= px <= frame.width and 0 <= py <= frame.height):
            raise StudioManifestError(f"display_zone.corners[{index}] lies outside the frame")
        corners.append((px, py))

    return DisplayZone(
        zone_id=zone_id,
        rect=rect,
        fit_mode=fit_mode,
        focus_point=focus_point,
        corners=tuple(corners),
        presenter_free=bool(data.get("presenter_free", False)),
        z_order=_as_int(data.get("z_order", 10), "display_zone.z_order"),
    )


def _parse_presenter(data, frame: Rect) -> PresenterPlacement:
    context = "presenter"
    if not isinstance(data, dict):
        raise StudioManifestError("presenter must be an object")

    anchor_x = _as_int(_require(data, "anchor_x", context), "presenter.anchor_x")
    anchor_y = _as_int(_require(data, "anchor_y", context), "presenter.anchor_y")
    if not (0 <= anchor_x <= frame.width and 0 <= anchor_y <= frame.height):
        raise StudioManifestError("presenter anchor point lies outside the frame")

    scale = _as_float(data.get("scale", 1.0), "presenter.scale")
    if not 0.05 <= scale <= 4.0:
        raise StudioManifestError("presenter.scale must be within 0.05..4.0")

    return PresenterPlacement(
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        scale=scale,
        z_order=_as_int(data.get("z_order", 20), "presenter.z_order"),
    )


def _parse_safe_areas(data, frame: Rect) -> SafeAreas:
    if not isinstance(data, dict):
        raise StudioManifestError("safe_areas must be an object")
    areas = {}
    for name in ("lower_third", "ticker", "subtitle"):
        rect = _parse_rect(_require(data, name, "safe_areas"), f"safe_areas.{name}")
        if rect.right > frame.width or rect.bottom > frame.height:
            raise StudioManifestError(f"safe_areas.{name} must lie inside the frame")
        areas[name] = rect
    return SafeAreas(**areas)


def parse_studio_manifest(data: dict, base_dir: str | Path) -> StudioManifest:
    """Turn a decoded manifest into a validated object, or refuse it."""
    if not isinstance(data, dict):
        raise StudioManifestError("Studio manifest must be a JSON object")

    schema_version = _as_int(_require(data, "schema_version", "manifest"), "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise StudioManifestError(
            f"Unsupported studio manifest schema_version {schema_version} "
            f"(this build understands {SCHEMA_VERSION})"
        )

    width = _as_int(_require(data, "width", "manifest"), "width")
    height = _as_int(_require(data, "height", "manifest"), "height")
    if width <= 0 or height <= 0:
        raise StudioManifestError("width and height must be positive")
    fps = _as_int(_require(data, "fps", "manifest"), "fps")
    if not 1 <= fps <= 120:
        raise StudioManifestError("fps must be within 1..120")
    frame = Rect(0, 0, width, height)

    alpha_mode = str(_require(data, "alpha_mode", "manifest"))
    if alpha_mode not in ALPHA_MODES:
        raise StudioManifestError(f"alpha_mode must be one of {ALPHA_MODES}, got {alpha_mode!r}")

    display_zone = _parse_display_zone(_require(data, "display_zone", "manifest"), frame)
    presenter = _parse_presenter(_require(data, "presenter", "manifest"), frame)
    safe_areas = _parse_safe_areas(_require(data, "safe_areas", "manifest"), frame)
    logo_zone = _parse_rect(_require(data, "logo_zone", "manifest"), "logo_zone")
    if logo_zone.right > width or logo_zone.bottom > height:
        raise StudioManifestError("logo_zone must lie inside the frame")

    optional = {}
    for key, allowed in (
        ("intro_asset", ASSET_KINDS),
        ("loop_asset", ASSET_KINDS),
        ("shadow_layer", (ASSET_KIND_IMAGE, ASSET_KIND_VIDEO)),
        ("foreground_layer", (ASSET_KIND_IMAGE, ASSET_KIND_VIDEO)),
        ("occlusion_mask", (ASSET_KIND_IMAGE,)),
        ("fallback_display_media", ASSET_KINDS),
    ):
        raw = data.get(key)
        optional[key] = _parse_asset(raw, key, allowed_kinds=allowed) if raw else None

    manifest = StudioManifest(
        schema_version=schema_version,
        studio_version=str(_require(data, "studio_version", "manifest")),
        asset_version=str(_require(data, "asset_version", "manifest")),
        name=str(data.get("name", "")),
        width=width,
        height=height,
        fps=fps,
        alpha_mode=alpha_mode,
        background=_parse_asset(_require(data, "background", "manifest"), "background"),
        display_zone=display_zone,
        presenter=presenter,
        logo_zone=logo_zone,
        safe_areas=safe_areas,
        placeholder=bool(data.get("placeholder", False)),
        provenance=dict(data.get("provenance") or {}),
        base_dir=Path(base_dir),
        **optional,
    )

    _validate_geometry(manifest)
    # Path safety is part of validation, not of use: a manifest that names an
    # unusable path must be refused before anything reads it.
    for asset in manifest.assets():
        resolve_studio_asset(base_dir, asset.path)
    return manifest


def _validate_geometry(manifest: StudioManifest) -> None:
    """Refuse layouts that would hide text or the presenter."""
    zone = manifest.display_zone
    for name, area in (
        ("lower_third", manifest.safe_areas.lower_third),
        ("ticker", manifest.safe_areas.ticker),
        ("subtitle", manifest.safe_areas.subtitle),
    ):
        if zone.rect.overlaps(area):
            raise StudioManifestError(
                f"display_zone.rect overlaps the {name} safe area; the topic monitor "
                "would cover text the viewer has to read"
            )

    if manifest.alpha_mode == ALPHA_MODE_OPAQUE:
        # The opaque clip *is* the studio camera: the presenter is already
        # inside the picture and we cannot know where. A monitor may only be
        # laid on top where we are told the presenter never appears, or where a
        # mask says which pixels belong to her.
        if not zone.presenter_free and manifest.occlusion_mask is None:
            raise StudioManifestError(
                "opaque_mp4 mode needs either display_zone.presenter_free=true or an "
                "occlusion_mask; otherwise the topic monitor may be drawn over the presenter"
            )


def load_studio_manifest(path: str | Path) -> StudioManifest:
    """Read and validate a studio manifest from disk."""
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise StudioManifestError(f"Studio manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StudioManifestError(f"Studio manifest is not valid JSON: {manifest_path}") from exc
    return parse_studio_manifest(data, manifest_path.parent)


def studio_manifest_path(asset_dir: str | Path, filename: str = MANIFEST_FILENAME) -> Path:
    return Path(asset_dir) / filename


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def studio_readiness_problems(manifest: StudioManifest) -> list[str]:
    """Everything that stands between this manifest and a real render.

    Returned rather than raised so a readiness command can print the whole list
    instead of one problem per run.
    """
    problems: list[str] = []

    if manifest.placeholder:
        problems.append(
            "Studio manifest is marked placeholder=true: it describes assets that "
            "do not exist yet (ALMANYA24 asset phase 1 is outstanding)"
        )

    for asset in manifest.assets():
        if asset.placeholder:
            problems.append(f"Asset {asset.path!r} is marked as a placeholder")
        try:
            path = manifest.asset_path(asset)
        except StudioManifestError as exc:
            problems.append(str(exc))
            continue
        if not path.exists():
            problems.append(f"Asset file missing: {asset.path}")
            continue
        if path.stat().st_size == 0:
            problems.append(f"Asset file is empty: {asset.path}")
            continue
        if asset.sha256 and file_sha256(path) != asset.sha256:
            problems.append(f"Asset {asset.path!r} does not match its recorded sha256")

    if manifest.fallback_display_media is None:
        # A scene whose topic medium is missing must still show something
        # deliberate. Without that graphic there is no safe answer, so the
        # studio is not ready.
        problems.append(
            "No fallback_display_media declared: a scene without topic media would "
            "have nothing defined to show in the monitor"
        )

    if manifest.studio_version.startswith("0.0.0"):
        problems.append(
            f"studio_version {manifest.studio_version!r} is a placeholder version"
        )

    return problems


def require_studio_ready(manifest: StudioManifest) -> None:
    """Fail closed unless every declared asset really exists."""
    problems = studio_readiness_problems(manifest)
    if problems:
        raise StudioNotReadyError(
            "ALMANYA24 studio is not ready:\n  - " + "\n  - ".join(problems)
        )


def studio_content_hash(manifest: StudioManifest) -> str:
    """Fingerprint of the studio for downstream invalidation.

    Geometry and versions are included because changing either changes every
    composited frame. Asset digests are included where the manifest records
    them, so replacing the wall artwork invalidates compositing even though the
    numbers stayed the same. What is *not* included is anything about a scene:
    changing the studio must never look like a reason to re-buy an avatar clip.
    """
    relevant = {
        "schema_version": manifest.schema_version,
        "studio_version": manifest.studio_version,
        "asset_version": manifest.asset_version,
        "width": manifest.width,
        "height": manifest.height,
        "fps": manifest.fps,
        "alpha_mode": manifest.alpha_mode,
        "display_zone": asdict(manifest.display_zone),
        "presenter": asdict(manifest.presenter),
        "logo_zone": asdict(manifest.logo_zone),
        "safe_areas": asdict(manifest.safe_areas),
        "assets": sorted((a.path, a.kind, a.sha256) for a in manifest.assets()),
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
