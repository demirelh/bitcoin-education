"""Ideogram v2 image generation — specializes in text-in-image (thumbnails, quote cards)."""

import logging
import time
from pathlib import Path

import requests

from btcedu.services.image_gen_service import (
    ImageEditRequest,
    ImageGenRequest,
    ImageGenResponse,
)

logger = logging.getLogger(__name__)

# Ideogram pricing (as of 2025)
IDEOGRAM_V2_COST = 0.08     # $0.08 per image (standard)
IDEOGRAM_V2_TURBO_COST = 0.05


class IdeogramImageService:
    """Ideogram v2 image generation.

    Excels at:
    - Text inside images (readable, correctly spelled)
    - YouTube thumbnails with bold captions
    - Quote cards, meme templates
    - Multi-language text rendering
    """

    def __init__(
        self,
        api_key: str,
        default_size: str = "ASPECT_16_9",
        default_model: str = "V_2",
        style_prefix: str = "",
    ):
        self.api_key = api_key
        self.default_size = default_size
        self.default_model = default_model  # "V_2" | "V_2_TURBO"
        self.style_prefix = style_prefix
        self.base_url = "https://api.ideogram.ai"

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        if not self.api_key:
            raise RuntimeError("IDEOGRAM_API_KEY not configured")

        full_prompt = (self.style_prefix + request.prompt) if self.style_prefix else request.prompt
        model = self._normalize_model(request.model or self.default_model)
        aspect = self._resolve_aspect(request.size or self.default_size)

        endpoint = f"{self.base_url}/generate"
        headers = {
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "image_request": {
                "prompt": full_prompt,
                "aspect_ratio": aspect,
                "model": model,
                "magic_prompt_option": "AUTO",
                "style_type": "GENERAL",
            }
        }

        response_data = self._call_with_retry(endpoint, headers, payload)
        data = response_data["data"][0]
        image_url = data["url"]
        revised = data.get("prompt", full_prompt)
        cost = self._compute_cost(model)

        logger.info(f"Ideogram generated: model={model}, aspect={aspect}, cost=${cost:.3f}")

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=revised,
            file_path=None,
            cost_usd=cost,
            model=f"ideogram-{model.lower()}",
        )

    def edit_image(self, request: ImageEditRequest) -> ImageGenResponse:
        raise NotImplementedError("Ideogram edit not implemented; use Gemini for frame editing")

    def _normalize_model(self, model: str) -> str:
        m = model.upper()
        if "TURBO" in m:
            return "V_2_TURBO"
        return "V_2"

    def _resolve_aspect(self, size: str) -> str:
        if size.upper().startswith("ASPECT_"):
            return size.upper()
        s = size.lower()
        if s in ("1920x1080", "1792x1024", "16x9"):
            return "ASPECT_16_9"
        if s in ("1080x1920", "9x16"):
            return "ASPECT_9_16"
        if s in ("1024x1024", "1x1"):
            return "ASPECT_1_1"
        if s in ("1024x1792", "4x3"):
            return "ASPECT_4_3"
        return "ASPECT_16_9"

    def _compute_cost(self, model: str) -> float:
        return IDEOGRAM_V2_TURBO_COST if "TURBO" in model else IDEOGRAM_V2_COST

    def _call_with_retry(self, endpoint: str, headers: dict, payload: dict, max_retries: int = 3) -> dict:
        last_exc = None
        for attempt in range(max_retries):
            try:
                r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
                if r.status_code == 429:
                    time.sleep(2 ** attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    logger.warning(f"Ideogram call failed (attempt {attempt+1}): {e}, retrying...")
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"Ideogram API failed after {max_retries} retries: {last_exc}")

    @staticmethod
    def download_image(image_url: str, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(image_url, timeout=60)
        r.raise_for_status()
        target_path.write_bytes(r.content)
        return target_path
