"""Gemini 2.0 Flash image editing service — raw HTTP, no SDK dependency."""

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Gemini 2.0 Flash pricing (approximate per-image edit cost)
# Input: ~280 tokens for 1024px image, output: ~280 tokens for response image
# At $0.10/1M input + $0.40/1M output tokens = ~$0.0001 per image
# We conservatively estimate higher due to text prompt tokens.
GEMINI_FLASH_COST_PER_EDIT = 0.003  # $0.003 per image (conservative)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class GeminiEditRequest:
    """Request to edit an image with Gemini."""

    image_path: Path
    prompt: str
    model: str = "gemini-2.0-flash-exp"


@dataclass
class GeminiEditResponse:
    """Response from Gemini image edit."""

    output_path: Path
    cost_usd: float
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    skipped: bool = False


class GeminiImageService:
    """Gemini 2.0 Flash image editing via REST API."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash-exp",
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries

    def edit_image(
        self,
        request: GeminiEditRequest,
        output_path: Path,
    ) -> GeminiEditResponse:
        """Edit an image using Gemini's multimodal capabilities.

        Sends the source image + text prompt to Gemini, which returns
        an edited image with German text overlays translated to Turkish.

        Args:
            request: Edit request with image path and prompt.
            output_path: Where to save the edited image.

        Returns:
            GeminiEditResponse with output path and cost.

        Raises:
            RuntimeError: If API call fails after retries.
        """
        # Read and encode source image
        image_bytes = request.image_path.read_bytes()
        mime_type = self._detect_mime(request.image_path)
        b64_image = base64.b64encode(image_bytes).decode("utf-8")

        # Build request payload
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": b64_image,
                            }
                        },
                        {"text": request.prompt},
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
            },
        }

        model = request.model or self.model
        url = f"{_API_BASE}/models/{model}:generateContent?key={self.api_key}"

        # Call with retry
        response_data = self._call_with_retry(url, payload)

        # Extract image from response
        output_bytes = self._extract_image(response_data)

        # Extract token counts
        usage = response_data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)

        # Save edited image
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(output_bytes)

        logger.info(
            "Gemini edited image: %s -> %s (%d bytes, %d+%d tokens)",
            request.image_path.name,
            output_path.name,
            len(output_bytes),
            prompt_tokens,
            completion_tokens,
        )

        return GeminiEditResponse(
            output_path=output_path,
            cost_usd=GEMINI_FLASH_COST_PER_EDIT,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _call_with_retry(self, url: str, payload: dict) -> dict:
        """Call Gemini API with exponential backoff retry."""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=120,
                )

                if resp.status_code == 429:
                    # Rate limited — retry
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Gemini rate limit (attempt %d/%d), retrying in %ds",
                        attempt + 1,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code >= 500:
                    # Server error — retry
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Gemini server error %d (attempt %d/%d), retrying in %ds",
                        resp.status_code,
                        attempt + 1,
                        self.max_retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if not resp.ok:
                    error_detail = resp.text[:500]
                    raise RuntimeError(
                        f"Gemini API error {resp.status_code}: {error_detail}"
                    )

                return resp.json()

            except requests.exceptions.Timeout:
                last_error = "timeout"
                if attempt < self.max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Gemini timeout (attempt %d), retrying in %ds", attempt + 1, wait
                    )
                    time.sleep(wait)
            except requests.exceptions.ConnectionError as e:
                last_error = str(e)
                if attempt < self.max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning("Gemini connection error, retrying in %ds", wait)
                    time.sleep(wait)

        raise RuntimeError(
            f"Gemini API failed after {self.max_retries} retries: {last_error}"
        )

    def _extract_image(self, response_data: dict) -> bytes:
        """Extract image bytes from Gemini response."""
        candidates = response_data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")

        parts = candidates[0].get("content", {}).get("parts", [])

        for part in parts:
            inline = part.get("inlineData")
            if inline and inline.get("mimeType", "").startswith("image/"):
                return base64.b64decode(inline["data"])

        # No image returned — Gemini may have refused or returned text only
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        text_summary = " ".join(text_parts)[:300]
        raise RuntimeError(
            f"Gemini did not return an image. Response text: {text_summary}"
        )

    @staticmethod
    def _detect_mime(path: Path) -> str:
        """Detect MIME type from file extension."""
        suffix = path.suffix.lower()
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")
