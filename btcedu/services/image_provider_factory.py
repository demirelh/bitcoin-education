"""Factory to select image generation provider by name.

Supports:
- "dalle3" (default, OpenAI DALL-E 3)
- "flux"   (fal.ai Flux.1 dev — best photoreal quality/cost)
- "ideogram" (Ideogram v2 — text-in-image, thumbnails, quote cards)
"""

import logging

from btcedu.services.image_gen_service import DallE3ImageService

logger = logging.getLogger(__name__)


def get_image_service(settings, provider: str | None = None):
    """Return an ImageGenService instance for the requested provider.

    Falls back to DALL-E 3 if the requested provider's API key is missing.
    """
    provider = (provider or getattr(settings, "image_gen_provider", "dalle3") or "dalle3").lower()

    if provider in ("flux", "flux_dev", "flux-dev", "fal"):
        from btcedu.services.flux_service import FluxImageService

        if not getattr(settings, "fal_api_key", ""):
            logger.warning("FAL_API_KEY not set — falling back to DALL-E 3")
            return _dalle3(settings)
        return FluxImageService(
            api_key=settings.fal_api_key,
            default_size=getattr(settings, "image_gen_size", "1920x1080"),
            style_prefix=getattr(settings, "image_gen_style_prefix", ""),
        )

    if provider in ("ideogram", "ideogram_v2"):
        from btcedu.services.ideogram_service import IdeogramImageService

        if not getattr(settings, "ideogram_api_key", ""):
            logger.warning("IDEOGRAM_API_KEY not set — falling back to DALL-E 3")
            return _dalle3(settings)
        return IdeogramImageService(
            api_key=settings.ideogram_api_key,
            default_size="ASPECT_16_9",
            style_prefix=getattr(settings, "image_gen_style_prefix", ""),
        )

    return _dalle3(settings)


def _dalle3(settings):
    return DallE3ImageService(
        api_key=settings.openai_api_key,
        default_size=getattr(settings, "image_gen_size", "1792x1024"),
        default_quality=getattr(settings, "image_gen_quality", "standard"),
        style_prefix=getattr(settings, "image_gen_style_prefix", ""),
    )


def select_provider_for_chapter(chapter, profile_config: dict | None = None) -> str:
    """Choose image provider based on chapter type.

    Heuristics:
    - Text-heavy overlays or quote / chart chapters → Ideogram (text renders correctly)
    - Regular hero/photoreal / lifestyle chapters → Flux
    - B-roll / stock-preferred → dalle3 (until pexels fallback kicks in)
    """
    # Profile-level override
    if profile_config:
        img_cfg = profile_config.get("imagegen", {}) if isinstance(profile_config, dict) else {}
        forced = img_cfg.get("provider")
        if forced:
            return forced

    visual_type = getattr(getattr(chapter, "visual", None), "type", "") or ""
    visual_type = str(visual_type).lower()
    overlays = getattr(chapter, "overlays", None) or []
    has_text_overlay = any(
        getattr(o, "text", None) and len(getattr(o, "text", "")) > 8 for o in overlays
    )

    if visual_type in ("quote", "chart", "text_heavy", "title_card", "thumbnail") or has_text_overlay:
        return "ideogram"
    if visual_type in ("stock", "b_roll", "broll"):
        return "dalle3"
    return "flux"
