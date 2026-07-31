"""Weather visual renderer: HTML/SVG via headless Chromium with Pillow fallback.

Renders branded 1920x1080 weather visuals suitable for the tagesschau_tr
video pipeline. Supports Raspberry Pi (ARM64 Chromium).

Fallback chain:
1. Full HTML/SVG render via headless Chromium
2. Reduced render with confirmed data only
3. Generic branded weather card (Pillow)
4. Pillow branded fallback (never blank)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from btcedu.core.weather.models import (
    RENDERER_VERSION,
    SCHEMA_VERSION,
    FindingSeverity,
    FindingType,
    ValidationFinding,
    WeatherData,
    WeatherRenderResult,
    WeatherScene,
    WeatherScenePlan,
    WeatherSceneType,
)

logger = logging.getLogger(__name__)

# Template directory relative to this file
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ASSETS_DIR = Path(__file__).parent / "assets"


def weather_provenance_path(output_path: Path) -> Path:
    """Return a sidecar path that keeps PNG and MP4 provenance separate."""
    return output_path.with_name(f"{output_path.name}.provenance.json")


_CONDITION_LABELS_TR = {
    "sunny": "Güneşli",
    "mostly_sunny": "Çoğunlukla güneşli",
    "partly_cloudy": "Parçalı bulutlu",
    "cloudy": "Bulutlu",
    "overcast": "Kapalı",
    "rain": "Yağmurlu",
    "showers": "Sağanak yağışlı",
    "heavy_rain": "Şiddetli yağışlı",
    "thunderstorms": "Gök gürültülü",
    "snow": "Karlı",
    "fog": "Sisli",
    "windy": "Rüzgarlı",
    "storm": "Fırtınalı",
    "hot": "Sıcak",
    "cold": "Soğuk",
    "mixed": "Değişken",
}


def _compute_cache_key(
    weather_data: WeatherData,
    scene_plan: WeatherScenePlan | None,
    *,
    profile: str = "",
    resolution: str = "1920x1080",
    accent_color: str = "#004B87",
    title: str = "Hava Durumu",
) -> str:
    """Compute deterministic cache key for weather render."""
    parts = [
        f"schema:{SCHEMA_VERSION}",
        f"renderer:{RENDERER_VERSION}",
        f"profile:{profile}",
        f"story:{weather_data.story_id or ''}",
        f"narration_hash:{weather_data.source_text_hash}",
        f"weather_json:{hashlib.sha256(weather_data.model_dump_json().encode()).hexdigest()[:12]}",
        f"resolution:{resolution}",
        f"accent:{accent_color}",
        f"title:{title}",
        f"assets:{_renderer_assets_hash()}",
    ]
    if scene_plan:
        plan_hash = hashlib.sha256(scene_plan.model_dump_json().encode()).hexdigest()[:12]
        parts.append(f"plan:{plan_hash}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:24]


def _renderer_assets_hash() -> str:
    """Hash the HTML template and bundled SVG assets used by the renderer."""
    digest = hashlib.sha256()
    paths = [_TEMPLATES_DIR / "weather_card.html"]
    paths.extend(sorted(_ASSETS_DIR.rglob("*.svg")))
    for path in paths:
        digest.update(str(path.relative_to(Path(__file__).parent)).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _find_chromium() -> str | None:
    """Find a usable headless Chromium/Chrome binary."""
    candidates = [
        "chromium-browser",
        "chromium",
        "google-chrome",
        "google-chrome-stable",
    ]
    for cmd in candidates:
        path = shutil.which(cmd)
        if path:
            return path
    return None


def _render_html_to_png(
    html_content: str,
    output_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
) -> bool:
    """Render HTML to PNG using headless Chromium. Returns True on success."""
    chromium = _find_chromium()
    if not chromium:
        logger.warning("No Chromium binary found, falling back to Pillow")
        return False

    # Write HTML to temp file next to output
    html_path = output_path.with_suffix(".html")
    html_path.write_text(html_content, encoding="utf-8")

    try:
        cmd = [
            chromium,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-software-rasterizer",
            f"--window-size={width},{height}",
            "--hide-scrollbars",
            f"--screenshot={output_path}",
            str(html_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and output_path.exists():
            size = output_path.stat().st_size
            if size > 5000:  # >5KB means likely non-blank
                return True
            logger.warning("Chromium produced suspiciously small file (%d bytes)", size)
            return False
        logger.warning("Chromium render failed: %s", result.stderr[:200])
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Chromium render error: %s", e)
        return False
    finally:
        # Clean up temporary HTML unless debug mode is active
        import os

        if not os.environ.get("BTCEDU_DEBUG_WEATHER"):
            html_path.unlink(missing_ok=True)


def _build_weather_html(
    weather_data: WeatherData,
    scene_plan: WeatherScenePlan | None,
    *,
    accent_color: str = "#004B87",
    title: str = "Hava Durumu",
) -> str:
    """Build the weather HTML/SVG page from Jinja2 template."""
    try:
        import jinja2
    except ImportError:
        logger.warning("Jinja2 not available, using Pillow fallback")
        return ""

    template_path = _TEMPLATES_DIR / "weather_card.html"
    if not template_path.exists():
        logger.warning("Weather template not found at %s", template_path)
        return ""

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("weather_card.html")

    # Load SVG icons inline
    icons = _load_weather_icons()

    # Build Germany map SVG
    map_svg = _get_germany_map_svg()

    return template.render(
        weather=weather_data,
        scene_plan=scene_plan,
        accent_color=accent_color,
        icons=icons,
        condition_labels=_CONDITION_LABELS_TR,
        germany_map=map_svg,
        renderer_version=RENDERER_VERSION,
        title=title,
    )


def _load_weather_icons() -> dict[str, str]:
    """Load SVG weather icons from assets directory."""
    icons = {}
    icons_dir = _ASSETS_DIR / "icons"
    if not icons_dir.exists():
        return icons
    for svg_file in icons_dir.glob("*.svg"):
        icons[svg_file.stem] = svg_file.read_text(encoding="utf-8")
    return icons


def _get_germany_map_svg() -> str:
    """Load the Germany map SVG."""
    map_path = _ASSETS_DIR / "germany_map.svg"
    if map_path.exists():
        return map_path.read_text(encoding="utf-8")
    return ""


def _render_pillow_fallback(
    weather_data: WeatherData,
    output_path: Path,
    *,
    accent_color: str = "#004B87",
    fallback_level: str = "generic",
    width: int = 1920,
    height: int = 1080,
    title: str = "Hava Durumu",
) -> bool:
    """Render a branded weather card using Pillow. Never produces blank output."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("Pillow not available — cannot render weather fallback")
        return False

    def _hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    try:
        accent_rgb = _hex_to_rgb(accent_color)
    except Exception:
        accent_rgb = (0, 75, 135)

    img = Image.new("RGB", (width, height), color=(20, 30, 50))
    draw = ImageDraw.Draw(img)

    # Header band
    draw.rectangle([(0, 0), (width, 180)], fill=accent_rgb)

    def _font(size: int):
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    # Title
    draw.text((60, 50), title, fill=(255, 255, 255), font=_font(72))

    y = 250

    if fallback_level == "title":
        draw.text(
            (80, y),
            "Hava Durumu",
            fill=(210, 220, 235),
            font=_font(52),
        )
        draw.rectangle(
            [(width // 2 - 240, y + 120), (width // 2 + 240, y + 360)],
            outline=(110, 150, 190),
            width=6,
        )
    elif fallback_level == "full" and weather_data.regions:
        # Full data render
        for region in weather_data.regions[:6]:
            conditions_str = ", ".join(
                _CONDITION_LABELS_TR.get(condition.value, condition.value)
                for condition in region.conditions
            )
            line = f"{region.label_tr}: {conditions_str}"
            draw.text((80, y), line, fill=(240, 240, 240), font=_font(48))
            y += 80

        # Temperature bar
        if weather_data.overview.temperature_min_c is not None:
            y += 40
            temp_text = (
                f"{weather_data.overview.temperature_min_c}°C – "
                f"{weather_data.overview.temperature_max_c}°C"
            )
            draw.text((80, y), temp_text, fill=(255, 200, 50), font=_font(64))

    elif fallback_level == "reduced" and weather_data.regions:
        # Reduced: only confirmed regions
        for region in weather_data.regions[:4]:
            conditions_str = ", ".join(
                _CONDITION_LABELS_TR.get(condition.value, condition.value)
                for condition in region.conditions
            )
            line = f"{region.label_tr}: {conditions_str}"
            draw.text((80, y), line, fill=(240, 240, 240), font=_font(48))
            y += 80

    else:
        # Generic branded fallback
        draw.text(
            (80, y),
            "Almanya Geneli Hava Durumu",
            fill=(200, 200, 200),
            font=_font(52),
        )
        y += 100
        # Neutral branded mark: do not imply sun, rain, or any other condition.
        mark_left = width // 2 - 150
        draw.rounded_rectangle(
            [(mark_left, y), (mark_left + 300, y + 170)],
            radius=28,
            fill=(35, 50, 72),
            outline=accent_rgb,
            width=6,
        )
        draw.text(
            (mark_left + 67, y + 48),
            "HAVA",
            fill=(235, 235, 235),
            font=_font(52),
        )
        y += 220
        excerpt = weather_data.unresolved_claims[0] if weather_data.unresolved_claims else ""
        if excerpt:
            draw.text(
                (80, y),
                excerpt[:90],
                fill=(230, 230, 230),
                font=_font(34),
            )
            y += 70
        draw.text(
            (80, y),
            "Detaylar için videoyu izleyin",
            fill=(180, 180, 180),
            font=_font(36),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG")

    size = output_path.stat().st_size
    if size < 3000:
        logger.error("Pillow fallback produced suspiciously small file (%d bytes)", size)
        return False
    return True


def _validate_output(output_path: Path) -> list[ValidationFinding]:
    """Validate rendered output is not blank/transparent/near-uniform."""
    findings: list[ValidationFinding] = []

    if not output_path.exists():
        findings.append(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message="Weather visual file does not exist",
                publish_blocked=True,
            )
        )
        return findings

    size = output_path.stat().st_size
    if size < 5000:
        findings.append(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=f"Weather visual too small ({size} bytes), likely blank",
                publish_blocked=True,
            )
        )
        return findings

    # Check for near-uniform images using Pillow
    try:
        import statistics

        from PIL import Image

        img = Image.open(output_path).convert("RGB")
        # Sample pixels across the image
        w, h = img.size
        pixels = []
        for x in range(0, w, w // 20):
            for y in range(0, h, h // 20):
                pixels.append(img.getpixel((x, y)))

        # Check if all pixels are nearly the same (blank)
        r_vals = [p[0] for p in pixels]
        g_vals = [p[1] for p in pixels]
        b_vals = [p[2] for p in pixels]

        r_std = statistics.stdev(r_vals) if len(r_vals) > 1 else 0
        g_std = statistics.stdev(g_vals) if len(g_vals) > 1 else 0
        b_std = statistics.stdev(b_vals) if len(b_vals) > 1 else 0

        avg_std = (r_std + g_std + b_std) / 3
        if avg_std < 5:
            findings.append(
                ValidationFinding(
                    type=FindingType.TRANSPARENT_WEATHER_FRAME,
                    severity=FindingSeverity.CRITICAL,
                    message=f"Weather visual appears near-uniform (std={avg_std:.1f})",
                    publish_blocked=True,
                )
            )
    except Exception as e:
        logger.warning("Could not validate weather visual pixel content: %s", e)

    return findings


def render_weather_visual(
    weather_data: WeatherData,
    scene_plan: WeatherScenePlan | None,
    output_path: Path,
    *,
    accent_color: str = "#004B87",
    profile: str = "",
    force: bool = False,
    width: int = 1920,
    height: int = 1080,
    title: str = "Hava Durumu",
    render_empty_template: bool = False,
) -> WeatherRenderResult:
    """Render weather visual with full fallback chain.

    Fallback order:
    1. Full HTML/SVG via headless Chromium (best quality)
    2. Reduced HTML with confirmed-only data
    3. Generic branded weather card (Pillow)
    4. Branded Pillow fallback (never blank)
    """
    cache_key = _compute_cache_key(
        weather_data,
        scene_plan,
        profile=profile,
        resolution=f"{width}x{height}",
        accent_color=accent_color,
        title=title,
    )

    # Check if output already exists and is valid (idempotency)
    if not force and output_path.exists():
        provenance_path = weather_provenance_path(output_path)
        if provenance_path.exists():
            try:
                prov = json.loads(provenance_path.read_text())
                if prov.get("cache_key") == cache_key:
                    findings = _validate_output(output_path)
                    if not any(f.publish_blocked for f in findings):
                        logger.info("Weather visual current (cache hit)")
                        return WeatherRenderResult(
                            success=True,
                            output_path=str(output_path),
                            fallback_level="full",
                            cache_key=cache_key,
                            metadata={"cached": True},
                        )
            except (json.JSONDecodeError, KeyError):
                pass

    output_path.parent.mkdir(parents=True, exist_ok=True)
    findings: list[ValidationFinding] = []
    fallback_level = "full"

    # Attempt 1: Full HTML/SVG render
    has_data = (
        bool(weather_data.regions)
        or weather_data.overview.temperature_min_c is not None
        or bool(weather_data.warnings)
        or bool(weather_data.outlook)
    )
    if has_data or render_empty_template:
        html = _build_weather_html(
            weather_data,
            scene_plan,
            accent_color=accent_color,
            title=title,
        )
        if html and _render_html_to_png(html, output_path, width=width, height=height):
            output_findings = _validate_output(output_path)
            if not any(f.publish_blocked for f in output_findings):
                _write_provenance(output_path, cache_key, "chromium_html", weather_data)
                return WeatherRenderResult(
                    success=True,
                    output_path=str(output_path),
                    fallback_level="full",
                    cache_key=cache_key,
                    findings=output_findings,
                    metadata={"method": "chromium_html"},
                )
            findings.extend(output_findings)

    if render_empty_template:
        if _render_pillow_fallback(
            weather_data,
            output_path,
            accent_color=accent_color,
            fallback_level="title",
            width=width,
            height=height,
            title=title,
        ):
            output_findings = _validate_output(output_path)
            if not any(f.publish_blocked for f in output_findings):
                _write_provenance(output_path, cache_key, "pillow_title", weather_data)
                return WeatherRenderResult(
                    success=True,
                    output_path=str(output_path),
                    fallback_level="full",
                    cache_key=cache_key,
                    findings=output_findings,
                    metadata={"method": "pillow_title"},
                )
            findings.extend(output_findings)

    # Attempt 2: Pillow full data render
    if has_data:
        if _render_pillow_fallback(
            weather_data,
            output_path,
            accent_color=accent_color,
            fallback_level="full",
            width=width,
            height=height,
            title=title,
        ):
            output_findings = _validate_output(output_path)
            if not any(f.publish_blocked for f in output_findings):
                fallback_level = "full"
                _write_provenance(output_path, cache_key, "pillow_full", weather_data)
                return WeatherRenderResult(
                    success=True,
                    output_path=str(output_path),
                    fallback_level=fallback_level,
                    cache_key=cache_key,
                    findings=output_findings,
                    metadata={"method": "pillow_full"},
                )
            findings.extend(output_findings)

    # Attempt 3: Reduced Pillow render
    if weather_data.regions:
        if _render_pillow_fallback(
            weather_data,
            output_path,
            accent_color=accent_color,
            fallback_level="reduced",
            width=width,
            height=height,
            title=title,
        ):
            output_findings = _validate_output(output_path)
            if not any(f.publish_blocked for f in output_findings):
                fallback_level = "reduced"
                _write_provenance(output_path, cache_key, "pillow_reduced", weather_data)
                return WeatherRenderResult(
                    success=True,
                    output_path=str(output_path),
                    fallback_level="reduced",
                    cache_key=cache_key,
                    findings=output_findings,
                    metadata={"method": "pillow_reduced"},
                )
            findings.extend(output_findings)

    # Attempt 4: Generic branded fallback (always works)
    if _render_pillow_fallback(
        weather_data,
        output_path,
        accent_color=accent_color,
        fallback_level="generic",
        width=width,
        height=height,
        title=title,
    ):
        output_findings = _validate_output(output_path)
        _write_provenance(output_path, cache_key, "pillow_generic", weather_data)
        findings.extend(output_findings)
        blocked = any(f.publish_blocked for f in output_findings)

        if blocked:
            findings.append(
                ValidationFinding(
                    type=FindingType.WEATHER_RENDER_FAILED,
                    severity=FindingSeverity.CRITICAL,
                    message="All render attempts produced invalid output",
                    publish_blocked=True,
                )
            )

        return WeatherRenderResult(
            success=not blocked,
            output_path=str(output_path),
            fallback_level="pillow_fallback",
            cache_key=cache_key,
            findings=findings,
            metadata={"method": "pillow_generic"},
        )

    # Complete failure
    findings.append(
        ValidationFinding(
            type=FindingType.WEATHER_RENDER_FAILED,
            severity=FindingSeverity.CRITICAL,
            message="All render methods failed",
            publish_blocked=True,
        )
    )
    return WeatherRenderResult(
        success=False,
        fallback_level="pillow_fallback",
        cache_key=cache_key,
        findings=findings,
    )


def _weather_data_for_scene(weather_data: WeatherData, scene: WeatherScene) -> WeatherData:
    """Return a claim-preserving weather subset for one planned scene."""
    selected = weather_data.model_copy(deep=True)
    if scene.type == WeatherSceneType.TITLE:
        selected.regions = []
        selected.overview.temperature_min_c = None
        selected.overview.temperature_max_c = None
        selected.warnings = []
        selected.outlook = []
    elif scene.type == WeatherSceneType.REGIONS:
        region_ids = set(scene.regions)
        selected.regions = [region for region in selected.regions if region.region_id in region_ids]
        selected.overview.temperature_min_c = None
        selected.overview.temperature_max_c = None
        selected.warnings = []
        selected.outlook = []
    elif scene.type == WeatherSceneType.TEMPERATURE:
        selected.regions = []
        selected.warnings = []
        selected.outlook = []
    elif scene.type == WeatherSceneType.WARNING:
        selected.regions = []
        selected.outlook = []
    elif scene.type == WeatherSceneType.OUTLOOK:
        selected.regions = []
        selected.warnings = []
    return selected


_REGION_ANCHORS = {
    "north": (0.29, 0.25),
    "northeast": (0.39, 0.28),
    "northwest": (0.18, 0.28),
    "central": (0.28, 0.48),
    "east": (0.40, 0.46),
    "west": (0.16, 0.49),
    "south": (0.28, 0.72),
    "southeast": (0.36, 0.68),
    "southwest": (0.20, 0.70),
    "coast": (0.28, 0.21),
    "alps": (0.28, 0.77),
}


def _render_animated_scene_frames(
    source_path: Path,
    output_dir: Path,
    weather_data: WeatherData,
    *,
    frame_count: int = 24,
) -> str | None:
    """Create a short loop of grounded sun, wind, rain, or storm motion."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    conditions = {
        condition.value for region in weather_data.regions for condition in region.conditions
    }
    warning_text = " ".join(warning.text.casefold() for warning in weather_data.warnings)
    has_wind = "windy" in conditions or "rüzgar" in warning_text or "rüzgâr" in warning_text
    has_sun = bool({"sunny", "mostly_sunny"} & conditions)
    has_rain = bool({"rain", "showers", "heavy_rain"} & conditions)
    has_storm = bool({"storm", "thunderstorms"} & conditions)
    if not any((has_wind, has_sun, has_rain, has_storm)):
        return None

    try:
        base = Image.open(source_path).convert("RGBA")
    except (OSError, ValueError):
        return None
    width, height = base.size
    pattern = output_dir / "anim_%03d.png"
    wind_anchors = [
        _REGION_ANCHORS.get(region.region_id.value, (0.28, 0.48))
        for region in weather_data.regions
        if any(condition.value == "windy" for condition in region.conditions)
    ]
    if has_wind and not wind_anchors:
        wind_anchors = [(0.28, 0.48)]
    rain_anchors = [
        _REGION_ANCHORS.get(region.region_id.value, (0.28, 0.48))
        for region in weather_data.regions
        if any(
            condition.value in {"rain", "showers", "heavy_rain"} for condition in region.conditions
        )
    ]
    storm_anchors = [
        _REGION_ANCHORS.get(region.region_id.value, (0.28, 0.48))
        for region in weather_data.regions
        if any(condition.value in {"storm", "thunderstorms"} for condition in region.conditions)
    ]
    for frame_index in range(frame_count):
        phase = frame_index / frame_count
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        if has_sun:
            pulse = 24 + int(10 * abs(0.5 - phase) * 2)
            for region in weather_data.regions:
                if not any(c.value in {"sunny", "mostly_sunny"} for c in region.conditions):
                    continue
                x_ratio, y_ratio = _REGION_ANCHORS.get(region.region_id.value, (0.28, 0.48))
                x, y = int(width * x_ratio), int(height * y_ratio)
                draw.ellipse(
                    (x - pulse, y - pulse, x + pulse, y + pulse),
                    fill=(255, 205, 55, 115),
                    outline=(255, 235, 130, 230),
                    width=5,
                )
        if has_wind:
            shift = int(phase * 220)
            for x_ratio, y_ratio in wind_anchors:
                center_x, center_y = int(width * x_ratio), int(height * y_ratio)
                for row in range(3):
                    x = center_x - 100 + ((shift + row * 65) % 190)
                    y = center_y - 70 + row * 45
                    draw.arc(
                        (x, y, x + 120, y + 45),
                        190,
                        350,
                        fill=(130, 220, 255, 210),
                        width=6,
                    )
        if has_rain:
            shift = int(phase * 34)
            for x_ratio, y_ratio in rain_anchors:
                center_x, center_y = int(width * x_ratio), int(height * y_ratio)
                for column in range(5):
                    x = center_x - 80 + column * 35
                    y = center_y - 55 + ((column * 23 + shift) % 100)
                    draw.line((x, y, x - 12, y + 32), fill=(90, 180, 255, 190), width=5)
        if has_storm and frame_index % 12 < 2:
            for x_ratio, y_ratio in storm_anchors:
                x, y = int(width * x_ratio), int(height * y_ratio)
                draw.line(
                    ((x + 20, y - 70), (x - 10, y), (x + 25, y), (x - 20, y + 90)),
                    fill=(255, 240, 120, 230),
                    width=9,
                )
        frame.convert("RGB").save(output_dir / f"anim_{frame_index:03d}.png")
    return str(pattern)


def render_weather_scene_video(
    weather_data: WeatherData,
    scene_plan: WeatherScenePlan,
    output_path: Path,
    *,
    accent_color: str = "#004B87",
    profile: str = "",
    width: int = 1920,
    height: int = 1080,
    fps: int = 25,
    title: str = "Hava Durumu",
) -> WeatherRenderResult:
    """Render the deterministic scene plan to a silent H.264 weather clip."""
    if not scene_plan.scenes:
        return WeatherRenderResult(
            success=False,
            output_path=str(output_path),
            fallback_level="pillow_fallback",
            findings=[
                ValidationFinding(
                    type=FindingType.WEATHER_SCENE_EMPTY,
                    severity=FindingSeverity.CRITICAL,
                    message="Weather scene plan is empty",
                    publish_blocked=True,
                )
            ],
        )

    cache_key = _compute_cache_key(
        weather_data,
        scene_plan,
        profile=f"{profile}:fps={fps}",
        resolution=f"{width}x{height}",
        accent_color=accent_color,
        title=title,
    )
    provenance_path = weather_provenance_path(output_path)
    if output_path.exists() and provenance_path.exists() and output_path.stat().st_size > 5000:
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            provenance = {}
        if provenance.get("cache_key") == cache_key:
            return WeatherRenderResult(
                success=True,
                output_path=str(output_path),
                fallback_level="full",
                cache_key=cache_key,
                metadata={
                    "method": "ffmpeg_scene_video",
                    "cached": True,
                    "duration_seconds": scene_plan.duration_seconds,
                    "fps": fps,
                },
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene_metadata: list[dict] = []
    with tempfile.TemporaryDirectory(
        prefix=f"{output_path.stem}_scenes_", dir=output_path.parent
    ) as temp_dir:
        scene_inputs: list[tuple[Path, str | None]] = []
        for index, scene in enumerate(scene_plan.scenes):
            scene_path = Path(temp_dir) / f"scene_{index:02d}.png"
            scene_weather = _weather_data_for_scene(weather_data, scene)
            scene_result = render_weather_visual(
                scene_weather,
                None,
                scene_path,
                accent_color=accent_color,
                profile=f"{profile}:scene:{scene.type.value}:{index}",
                force=True,
                width=width,
                height=height,
                title=scene.headline or title,
                render_empty_template=scene.type == WeatherSceneType.TITLE,
            )
            if not scene_result.success:
                return scene_result
            animation_dir = Path(temp_dir) / f"animation_{index:02d}"
            animation_dir.mkdir()
            animation_pattern = _render_animated_scene_frames(
                scene_path,
                animation_dir,
                scene_weather,
            )
            scene_inputs.append((scene_path, animation_pattern))
            scene_metadata.append(
                {
                    "type": scene.type.value,
                    "start": scene.start,
                    "end": scene.end,
                    "duration": scene.end - scene.start,
                }
            )

        command = ["ffmpeg", "-y"]
        for (scene_path, animation_pattern), metadata in zip(
            scene_inputs, scene_metadata, strict=True
        ):
            if animation_pattern:
                command.extend(
                    [
                        "-stream_loop",
                        "-1",
                        "-framerate",
                        "12",
                        "-t",
                        f"{metadata['duration']:.3f}",
                        "-i",
                        animation_pattern,
                    ]
                )
            else:
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-t",
                        f"{metadata['duration']:.3f}",
                        "-i",
                        str(scene_path),
                    ]
                )

        filters: list[str] = []
        labels: list[str] = []
        for index, metadata in enumerate(scene_metadata):
            duration = float(metadata["duration"])
            fade_out = max(0.0, duration - 0.25)
            label = f"v{index}"
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,"
                f"fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out:.3f}:d=0.25,"
                f"fps={fps}[{label}]"
            )
            labels.append(f"[{label}]")
        filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[v]")
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(60, int(scene_plan.duration_seconds * 4)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return WeatherRenderResult(
                success=False,
                output_path=str(output_path),
                fallback_level="pillow_fallback",
                findings=[
                    ValidationFinding(
                        type=FindingType.WEATHER_RENDER_FAILED,
                        severity=FindingSeverity.MAJOR,
                        message=f"Weather scene video failed: {error}",
                    )
                ],
            )
        if completed.returncode != 0 or not output_path.exists():
            return WeatherRenderResult(
                success=False,
                output_path=str(output_path),
                fallback_level="pillow_fallback",
                findings=[
                    ValidationFinding(
                        type=FindingType.WEATHER_RENDER_FAILED,
                        severity=FindingSeverity.MAJOR,
                        message=f"Weather scene video failed: {completed.stderr[-300:]}",
                    )
                ],
            )

    _write_provenance(output_path, cache_key, "ffmpeg_scene_video", weather_data)
    return WeatherRenderResult(
        success=True,
        output_path=str(output_path),
        fallback_level="full",
        cache_key=cache_key,
        metadata={
            "method": "ffmpeg_scene_video",
            "scene_assets": scene_metadata,
            "duration_seconds": scene_plan.duration_seconds,
            "fps": fps,
        },
    )


def _write_provenance(
    output_path: Path,
    cache_key: str,
    method: str,
    weather_data: WeatherData,
) -> None:
    """Write provenance sidecar for idempotency tracking."""
    from datetime import UTC, datetime

    provenance = {
        "cache_key": cache_key,
        "renderer_version": RENDERER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "story_id": weather_data.story_id,
        "source_text_hash": weather_data.source_text_hash,
        "region_count": len(weather_data.regions),
        "rendered_at": datetime.now(UTC).isoformat(),
    }
    prov_path = weather_provenance_path(output_path)
    prov_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
