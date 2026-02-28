"""Image generation service abstraction with DALL-E 3 implementation."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

# DALL-E 3 pricing (as of 2025)
DALLE3_COST_STANDARD_1024 = 0.040  # $0.040 per image (1024x1024)
DALLE3_COST_STANDARD_1792 = 0.080  # $0.080 per image (1792x1024 or 1024x1792)
DALLE3_COST_HD_1024 = 0.080  # $0.080 per image (1024x1024 HD)
DALLE3_COST_HD_1792 = 0.120  # $0.120 per image (1792x1024 or 1024x1792 HD)


@dataclass
class ImageGenRequest:
    """Request for image generation."""

    prompt: str
    model: str = "dall-e-3"
    size: str = "1792x1024"  # DALL-E 3 landscape (closest to 1920x1080)
    quality: str = "standard"  # "standard" or "hd"
    style_prefix: str = ""  # Brand guidelines prefix


@dataclass
class ImageGenResponse:
    """Response from image generation."""

    image_url: str  # Original URL from API
    revised_prompt: str  # DALL-E's revised prompt (if applicable)
    file_path: Path | None  # Local file path after download (set by caller)
    cost_usd: float  # Estimated cost
    model: str  # Model used


class ImageGenService(Protocol):
    """Protocol for image generation services."""

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        """Generate an image from a prompt."""
        ...


class DallE3ImageService:
    """DALL-E 3 image generation service."""

    def __init__(
        self,
        api_key: str,
        default_size: str = "1792x1024",
        default_quality: str = "standard",
        style_prefix: str = "",
    ):
        """Initialize DALL-E 3 service.

        Args:
            api_key: OpenAI API key
            default_size: Default image size ("1792x1024", "1024x1792", or "1024x1024")
            default_quality: Default quality ("standard" or "hd")
            style_prefix: Optional prefix to prepend to all prompts for style consistency
        """
        self.api_key = api_key
        self.default_size = default_size
        self.default_quality = default_quality
        self.style_prefix = style_prefix

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        """Generate image using DALL-E 3.

        Args:
            request: Image generation request

        Returns:
            ImageGenResponse with image URL and metadata

        Raises:
            RuntimeError: If API call fails after retries
        """
        # Prepend style prefix if configured
        full_prompt = (
            self.style_prefix + request.prompt if self.style_prefix else request.prompt
        )

        # Use request params or defaults
        size = request.size or self.default_size
        quality = request.quality or self.default_quality

        # Call DALL-E 3 with retry logic
        response_data = self._call_dalle3_with_retry(
            prompt=full_prompt,
            size=size,
            quality=quality,
        )

        # Extract response data
        image_url = response_data["data"][0]["url"]
        revised_prompt = response_data["data"][0].get("revised_prompt", request.prompt)

        # Compute cost
        cost = self._compute_cost(size, quality)

        logger.info(
            f"DALL-E 3 generated image: size={size}, quality={quality}, cost=${cost:.3f}"
        )

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=revised_prompt,
            file_path=None,  # Set by caller after download
            cost_usd=cost,
            model=request.model,
        )

    def _call_dalle3_with_retry(
        self, prompt: str, size: str, quality: str, max_retries: int = 3
    ) -> dict:
        """Call DALL-E 3 API with exponential backoff retry.

        Args:
            prompt: Image generation prompt
            size: Image size
            quality: Image quality
            max_retries: Maximum number of retry attempts

        Returns:
            API response dict

        Raises:
            RuntimeError: If all retries fail
        """
        from openai import APIError, OpenAI, RateLimitError

        client = OpenAI(api_key=self.api_key)

        for attempt in range(max_retries):
            try:
                response = client.images.generate(
                    model="dall-e-3",
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    n=1,
                )
                return response.model_dump()
            except RateLimitError as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(
                        f"DALL-E 3 rate limit hit (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(
                        f"DALL-E 3 rate limit exceeded after {max_retries} retries"
                    ) from e
            except APIError as e:
                error_msg = str(e).lower()
                if "content_policy_violation" in error_msg or "safety system" in error_msg:
                    raise RuntimeError(
                        f"DALL-E 3 rejected prompt due to content policy: {prompt[:100]}..."
                    ) from e
                raise RuntimeError(f"DALL-E 3 API error: {e}") from e

        raise RuntimeError(f"DALL-E 3 call failed after {max_retries} attempts")

    def _compute_cost(self, size: str, quality: str) -> float:
        """Compute cost based on size and quality.

        Args:
            size: Image size (e.g., "1792x1024")
            quality: Image quality ("standard" or "hd")

        Returns:
            Cost in USD
        """
        if quality == "hd":
            return DALLE3_COST_HD_1792 if "1792" in size else DALLE3_COST_HD_1024
        else:
            return DALLE3_COST_STANDARD_1792 if "1792" in size else DALLE3_COST_STANDARD_1024

    @staticmethod
    def download_image(url: str, target_path: Path) -> Path:
        """Download image from URL to local file.

        Args:
            url: Image URL from API
            target_path: Local file path to save image

        Returns:
            Path to saved image file

        Raises:
            requests.HTTPError: If download fails
        """
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(response.content)
        logger.info(f"Downloaded image to {target_path} ({len(response.content)} bytes)")
        return target_path
