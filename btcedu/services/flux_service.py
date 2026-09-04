"""Flux.1 image generation via fal.ai for photoreal hero shots."""

import logging
import time
from pathlib import Path

import requests

from btcedu.services.image_gen_service import (
    ImageEditRequest,
    ImageGenRequest,
    ImageGenResponse,
)
from btcedu.services.retry import retry_on_transient

logger = logging.getLogger(__name__)

# fal.ai Flux pricing (as of 2025)
FLUX_DEV_COST = 0.025  # $0.025 per image (1024x1024)
FLUX_PRO_COST = 0.055  # $0.055 per image (1024x1024)
FLUX_SCHNELL_COST = 0.003  # $0.003 per image (fastest, lower quality)


class FluxImageService:
    """Flux.1 image generation via fal.ai API.

    Model variants:
    - flux-dev: Best quality/cost balance ($0.025)
    - flux-pro: Premium ($0.055)
    - flux-schnell: Fast + cheap ($0.003)
    """

    def __init__(
        self,
        api_key: str,
        default_size: str = "1920x1080",
        default_model: str = "flux/dev",
        style_prefix: str = "",
    ):
        self.api_key = api_key
        self.default_size = default_size
        self.default_model = default_model  # "flux/dev" | "flux-pro" | "flux/schnell"
        self.style_prefix = style_prefix
        self.base_url = "https://fal.run"

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        if not self.api_key:
            raise RuntimeError("FAL_API_KEY not configured")

        full_prompt = (self.style_prefix + request.prompt) if self.style_prefix else request.prompt
        model_slug = self._normalize_model(request.model or self.default_model)
        size = request.size or self.default_size
        width, height = self._parse_size(size)

        endpoint = f"{self.base_url}/{model_slug}"
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "prompt": full_prompt,
            "image_size": {"width": width, "height": height},
            "num_inference_steps": 28 if "schnell" not in model_slug else 4,
            "guidance_scale": 3.5,
            "num_images": 1,
            "enable_safety_checker": True,
        }

        response_data = self._call_with_retry(endpoint, headers, payload)
        image_url = response_data["images"][0]["url"]
        cost = self._compute_cost(model_slug)

        logger.info(f"Flux generated: model={model_slug}, size={width}x{height}, cost=${cost:.3f}")

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=full_prompt,
            file_path=None,
            cost_usd=cost,
            model=model_slug,
        )

    def edit_image(self, request: ImageEditRequest) -> ImageGenResponse:
        raise NotImplementedError(
            "Flux via fal.ai does not currently support image editing here; "
            "use Gemini or DALL-E 2 edit"
        )

    def _normalize_model(self, model: str) -> str:
        m = model.lower()
        if "pro" in m:
            return "fal-ai/flux-pro/v1.1"
        if "schnell" in m or "fast" in m:
            return "fal-ai/flux/schnell"
        return "fal-ai/flux/dev"

    def _parse_size(self, size: str) -> tuple[int, int]:
        try:
            w, h = size.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1920, 1080

    def _compute_cost(self, model_slug: str) -> float:
        if "pro" in model_slug:
            return FLUX_PRO_COST
        if "schnell" in model_slug:
            return FLUX_SCHNELL_COST
        return FLUX_DEV_COST

    def _call_with_retry(
        self, endpoint: str, headers: dict, payload: dict, max_retries: int = 3
    ) -> dict:
        last_exc = None
        for attempt in range(max_retries):
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=180)
                if r.status_code == 429:
                    time.sleep(2**attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    logger.warning(f"Flux call failed (attempt {attempt + 1}): {e}, retrying...")
                    time.sleep(2**attempt)
        raise RuntimeError(f"Flux API failed after {max_retries} retries: {last_exc}")

    @staticmethod
    @retry_on_transient(max_retries=3, base_delay=1.0)
    def download_image(image_url: str, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(image_url, timeout=60)
        r.raise_for_status()
        target_path.write_bytes(r.content)
        return target_path
