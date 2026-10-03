"""Image generation service abstraction. Primary generation uses gpt-image-1
(OpenAI retired dall-e-3 for image generation; see class docstring below).
"""

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from btcedu.services.retry import retry_on_transient

logger = logging.getLogger(__name__)

# gpt-image-1 is billed by token, not by a flat per-image price. These
# per-token rates come straight from the "usage" block OpenAI returns with
# every response, so cost tracking survives future per-token price changes
# without a code edit (only these three constants would need updating).
GPT_IMAGE_OUTPUT_COST_PER_TOKEN = 40.0 / 1_000_000  # image (output) tokens
GPT_IMAGE_TEXT_INPUT_COST_PER_TOKEN = 5.0 / 1_000_000  # text prompt tokens
GPT_IMAGE_IMAGE_INPUT_COST_PER_TOKEN = 10.0 / 1_000_000  # reference image tokens

# gpt-image-1 quality values, keyed by the legacy dall-e-3 "standard"/"hd"
# vocabulary still used throughout the profile configs and Settings defaults.
_GPT_IMAGE_QUALITY_MAP = {"standard": "medium", "hd": "high"}
_GPT_IMAGE_VALID_QUALITIES = {"low", "medium", "high", "auto"}

# gpt-image-1 only accepts these three sizes (plus "auto"); anything else
# (e.g. the legacy dall-e-3 "1792x1024") is mapped to the closest orientation.
_GPT_IMAGE_VALID_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}

# Approximate flat-rate fallback (USD/image) if a response ever lacks a usage
# block; the token-based _compute_cost_from_usage() above is preferred.
_GPT_IMAGE_FALLBACK_COST_BY_QUALITY = {
    "low": 0.011,
    "medium": 0.042,
    "high": 0.167,
    "auto": 0.042,
}

# DALL-E 2 edit pricing (as of 2025). The edit endpoint (frame_editor.py,
# frame_extractor.py) is unaffected by the gpt-image-1 migration above.
DALLE2_EDIT_COST_256 = 0.016  # $0.016 per image (256x256)
DALLE2_EDIT_COST_512 = 0.018  # $0.018 per image (512x512)
DALLE2_EDIT_COST_1024 = 0.020  # $0.020 per image (1024x1024)


def _gpt_image_quality(quality: str) -> str:
    """Map a "standard"/"hd" (or already-valid) quality value to gpt-image-1's."""
    quality = (quality or "").lower()
    if quality in _GPT_IMAGE_VALID_QUALITIES:
        return quality
    return _GPT_IMAGE_QUALITY_MAP.get(quality, "medium")


def _gpt_image_size(size: str) -> str:
    """Map a requested size to one gpt-image-1 actually supports."""
    if size in _GPT_IMAGE_VALID_SIZES:
        return size
    try:
        width, height = (int(part) for part in size.lower().split("x"))
    except (ValueError, AttributeError):
        return "auto"
    if width == height:
        return "1024x1024"
    return "1536x1024" if width > height else "1024x1536"


@dataclass
class ImageEditRequest:
    """Request for DALL-E image editing (inpainting/variation)."""

    image_path: Path  # Source image to edit
    prompt: str  # Edit instructions
    model: str = "dall-e-2"  # edit API only supports dall-e-2
    size: str = "1024x1024"  # dall-e-2 sizes: 256x256, 512x512, 1024x1024
    mask_path: Path | None = None  # Optional mask for inpainting


@dataclass
class ImageGenRequest:
    """Request for image generation."""

    prompt: str
    model: str = "gpt-image-1"
    size: str = "1536x1024"  # gpt-image-1 landscape (closest to 1920x1080)
    quality: str = "standard"  # legacy vocabulary; mapped to gpt-image-1's own
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

    def edit_image(self, request: ImageEditRequest) -> ImageGenResponse:
        """Edit an existing image using DALL-E."""
        ...


class DallE3ImageService:
    """Primary generative image service (gpt-image-1). Kept as
    ``DallE3ImageService`` — the pre-existing class name callers, the image
    provider factory and tests all reference — even though OpenAI retired the
    dall-e-3 model this class originally wrapped; only the API model id,
    quality/size vocabulary and response decoding changed.
    """

    def __init__(
        self,
        api_key: str,
        default_size: str = "1536x1024",
        default_quality: str = "standard",
        style_prefix: str = "",
    ):
        """Initialize the image generation service.

        Args:
            api_key: OpenAI API key
            default_size: Default image size ("1536x1024", "1024x1536", or "1024x1024")
            default_quality: Default quality ("standard" or "hd"; mapped to gpt-image-1's
                own "medium"/"high" vocabulary)
            style_prefix: Optional prefix to prepend to all prompts for style consistency
        """
        self.api_key = api_key
        self.default_size = default_size
        self.default_quality = default_quality
        self.style_prefix = style_prefix

    def generate_image(self, request: ImageGenRequest) -> ImageGenResponse:
        """Generate an image using gpt-image-1.

        Args:
            request: Image generation request

        Returns:
            ImageGenResponse with image URL and metadata

        Raises:
            RuntimeError: If API call fails after retries
        """
        # Prepend style prefix if configured
        full_prompt = self.style_prefix + request.prompt if self.style_prefix else request.prompt

        # Use request params or defaults, translated to gpt-image-1's vocabulary
        size = _gpt_image_size(request.size or self.default_size)
        quality = _gpt_image_quality(request.quality or self.default_quality)

        # Call gpt-image-1 with retry logic
        response_data = self._call_dalle3_with_retry(
            prompt=full_prompt,
            size=size,
            quality=quality,
        )

        # Extract response data. gpt-image-1 only ever returns b64_json, never
        # a hosted url; encode it as a data: URI so download_image() (which
        # every provider shares) can write it out without a network request.
        entry = response_data["data"][0]
        image_url = entry.get("url")
        if not image_url:
            image_url = f"data:image/png;base64,{entry['b64_json']}"
        revised_prompt = entry.get("revised_prompt", request.prompt)

        # Compute cost from the token usage OpenAI actually billed, when
        # present (gpt-image-1); otherwise fall back to the flat per-image
        # table (older dall-e-3-style responses).
        usage = response_data.get("usage")
        cost = self._compute_cost_from_usage(usage) if usage else self._compute_cost(size, quality)

        logger.info(f"gpt-image-1 generated image: size={size}, quality={quality}, cost=${cost:.3f}")

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=revised_prompt,
            file_path=None,  # Set by caller after download
            cost_usd=cost,
            model=request.model,
        )

    def edit_image(self, request: ImageEditRequest) -> ImageGenResponse:
        """Edit an existing image using DALL-E 2 edit API.

        Args:
            request: Image edit request with source image path and prompt

        Returns:
            ImageGenResponse with edited image URL and metadata

        Raises:
            RuntimeError: If API call fails after retries
        """
        response_data = self._call_dalle2_edit_with_retry(
            image_path=request.image_path,
            prompt=request.prompt,
            size=request.size,
            mask_path=request.mask_path,
        )

        image_url = response_data["data"][0]["url"]
        revised_prompt = response_data["data"][0].get("revised_prompt", request.prompt)

        cost = self._compute_edit_cost(request.size)

        logger.info(f"DALL-E 2 edited image: size={request.size}, cost=${cost:.3f}")

        return ImageGenResponse(
            image_url=image_url,
            revised_prompt=revised_prompt,
            file_path=None,
            cost_usd=cost,
            model=request.model,
        )

    def _call_dalle2_edit_with_retry(
        self,
        image_path: Path,
        prompt: str,
        size: str,
        mask_path: Path | None = None,
        max_retries: int = 3,
    ) -> dict:
        """Call DALL-E 2 edit API with exponential backoff retry.

        Args:
            image_path: Path to source image
            prompt: Edit instructions
            size: Output image size
            mask_path: Optional mask image path
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
                kwargs = {
                    "model": "dall-e-2",
                    "image": open(image_path, "rb"),
                    "prompt": prompt,
                    "size": size,
                    "n": 1,
                }
                if mask_path is not None:
                    kwargs["mask"] = open(mask_path, "rb")

                response = client.images.edit(**kwargs)
                return response.model_dump()
            except RateLimitError as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        f"DALL-E 2 edit rate limit hit (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(
                        f"DALL-E 2 edit rate limit exceeded after {max_retries} retries"
                    ) from e
            except APIError as e:
                error_msg = str(e).lower()
                if "content_policy_violation" in error_msg or "safety system" in error_msg:
                    raise RuntimeError(
                        f"DALL-E 2 edit rejected prompt due to content policy: {prompt[:100]}..."
                    ) from e
                raise RuntimeError(f"DALL-E 2 edit API error: {e}") from e

        raise RuntimeError(f"DALL-E 2 edit call failed after {max_retries} attempts")

    def _compute_edit_cost(self, size: str) -> float:
        """Compute cost for DALL-E 2 edit based on size.

        Args:
            size: Image size (e.g., "1024x1024")

        Returns:
            Cost in USD
        """
        if "256" in size:
            return DALLE2_EDIT_COST_256
        elif "512" in size:
            return DALLE2_EDIT_COST_512
        else:
            return DALLE2_EDIT_COST_1024

    def _call_dalle3_with_retry(
        self, prompt: str, size: str, quality: str, max_retries: int = 3
    ) -> dict:
        """Call gpt-image-1 with exponential backoff retry.

        Args:
            prompt: Image generation prompt
            size: Image size (already translated to a gpt-image-1 size)
            quality: Image quality (already translated to a gpt-image-1 quality)
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
                    model="gpt-image-1",
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
                        f"gpt-image-1 rate limit hit (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(
                        f"gpt-image-1 rate limit exceeded after {max_retries} retries"
                    ) from e
            except APIError as e:
                error_msg = str(e).lower()
                if "content_policy_violation" in error_msg or "safety system" in error_msg:
                    raise RuntimeError(
                        f"gpt-image-1 rejected prompt due to content policy: {prompt[:100]}..."
                    ) from e
                raise RuntimeError(f"gpt-image-1 API error: {e}") from e

        raise RuntimeError(f"gpt-image-1 call failed after {max_retries} attempts")

    def _compute_cost_from_usage(self, usage: dict) -> float:
        """Compute the actually-billed cost from gpt-image-1's usage block.

        Preferred over a flat per-image table: it reflects exactly what
        OpenAI charged for this call and survives future per-token price
        changes without a code edit (only the three GPT_IMAGE_*_COST_PER_TOKEN
        constants would need updating).
        """
        output_tokens = usage.get("output_tokens", 0) or 0
        input_details = usage.get("input_tokens_details") or {}
        text_tokens = input_details.get("text_tokens", 0) or 0
        image_tokens = input_details.get("image_tokens", 0) or 0
        return (
            output_tokens * GPT_IMAGE_OUTPUT_COST_PER_TOKEN
            + text_tokens * GPT_IMAGE_TEXT_INPUT_COST_PER_TOKEN
            + image_tokens * GPT_IMAGE_IMAGE_INPUT_COST_PER_TOKEN
        )

    def _compute_cost(self, size: str, quality: str) -> float:
        """Fallback flat-rate estimate, used only if a response has no usage
        block (should not happen for gpt-image-1, kept defensively).

        Args:
            size: Image size (e.g., "1536x1024")
            quality: Image quality ("low", "medium", "high", or "auto")

        Returns:
            Cost in USD
        """
        return _GPT_IMAGE_FALLBACK_COST_BY_QUALITY.get(quality, 0.042)

    @staticmethod
    @retry_on_transient(max_retries=3, base_delay=1.0)
    def download_image(url: str, target_path: Path) -> Path:
        """Write an image to local disk from either a hosted URL or a
        ``data:`` URI (gpt-image-1 only ever returns inline base64 data).

        Args:
            url: Image URL or ``data:image/...;base64,...`` URI from the API
            target_path: Local file path to save image

        Returns:
            Path to saved image file

        Raises:
            requests.HTTPError: If a network download fails
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if url.startswith("data:"):
            _, _, encoded = url.partition(",")
            raw_bytes = base64.b64decode(encoded)
            target_path.write_bytes(raw_bytes)
            logger.info(f"Wrote inline image to {target_path} ({len(raw_bytes)} bytes)")
            return target_path

        response = requests.get(url, timeout=30)
        response.raise_for_status()
        target_path.write_bytes(response.content)
        logger.info(f"Downloaded image to {target_path} ({len(response.content)} bytes)")
        return target_path
