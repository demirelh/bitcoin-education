"""Claude API service wrapper with dry-run support."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Claude Sonnet 4 pricing (per million tokens)
SONNET_INPUT_PRICE_PER_M = 3.0
SONNET_OUTPUT_PRICE_PER_M = 15.0


@dataclass
class ClaudeResponse:
    """Parsed response from Claude API."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


def calculate_cost(input_tokens: int, output_tokens: int) -> float:
    """Calculate estimated cost in USD for a Claude API call."""
    input_cost = (input_tokens / 1_000_000) * SONNET_INPUT_PRICE_PER_M
    output_cost = (output_tokens / 1_000_000) * SONNET_OUTPUT_PRICE_PER_M
    return round(input_cost + output_cost, 6)


def compute_prompt_hash(
    template_text: str,
    model: str,
    temperature: float,
    chunk_ids: list[str],
) -> str:
    """SHA256 hash of prompt components for idempotency tracking."""
    payload = f"{template_text}|{model}|{temperature}|{','.join(sorted(chunk_ids))}"
    return hashlib.sha256(payload.encode()).hexdigest()


def call_claude(
    system_prompt: str,
    user_message: str,
    settings,
    dry_run_path: Path | None = None,
) -> ClaudeResponse:
    """Call Claude Messages API.

    Args:
        system_prompt: System-level instructions.
        user_message: User message content.
        settings: Application settings (needs anthropic_api_key, claude_model, etc.).
        dry_run_path: If settings.dry_run, write payload here instead of calling API.

    Returns:
        ClaudeResponse with text, token counts, and cost.
    """
    if settings.dry_run:
        return _write_dry_run(system_prompt, user_message, settings, dry_run_path)

    from anthropic import Anthropic

    client = Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.max_retries,
    )

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=settings.claude_max_tokens,
        temperature=settings.claude_temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = calculate_cost(input_tokens, output_tokens)

    logger.info(
        "Claude call: %d in / %d out tokens, $%.4f (%s)",
        input_tokens,
        output_tokens,
        cost,
        settings.claude_model,
    )

    return ClaudeResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        model=settings.claude_model,
    )


def _write_dry_run(
    system_prompt: str,
    user_message: str,
    settings,
    output_path: Path | None,
) -> ClaudeResponse:
    """Write request payload as JSON without calling API."""
    payload = {
        "model": settings.claude_model,
        "max_tokens": settings.claude_max_tokens,
        "temperature": settings.claude_temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
        "dry_run": True,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Dry-run payload written: %s", output_path)

    return ClaudeResponse(
        text="[DRY RUN - no API call made]",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        model=settings.claude_model,
    )
