"""LLM API service wrapper with Anthropic + OpenAI fallback support."""

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

# OpenAI GPT-4o pricing (per million tokens)
GPT4O_INPUT_PRICE_PER_M = 2.50
GPT4O_OUTPUT_PRICE_PER_M = 10.0


@dataclass
class ClaudeResponse:
    """Parsed response from LLM API (name kept for backward compat)."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    provider: str = "anthropic",
) -> float:
    """Calculate estimated cost in USD for an LLM API call."""
    if provider == "openai":
        input_cost = (input_tokens / 1_000_000) * GPT4O_INPUT_PRICE_PER_M
        output_cost = (output_tokens / 1_000_000) * GPT4O_OUTPUT_PRICE_PER_M
    else:
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


def _resolve_provider(settings) -> str:
    """Determine which LLM provider to use.

    Priority: settings.llm_provider, but fall back to openai if
    anthropic key is missing and openai key is present.
    """
    provider = getattr(settings, "llm_provider", "anthropic")
    if provider == "anthropic" and not settings.anthropic_api_key:
        if settings.openai_api_key:
            logger.warning(
                "No ANTHROPIC_API_KEY set — falling back to OpenAI (%s)",
                getattr(settings, "openai_llm_model", "gpt-4o"),
            )
            return "openai"
    return provider


def call_claude(
    system_prompt: str,
    user_message: str,
    settings,
    dry_run_path: Path | None = None,
    max_tokens: int | None = None,
) -> ClaudeResponse:
    """Call LLM API (Anthropic or OpenAI fallback).

    Provider selection:
        1. ``settings.llm_provider`` ("anthropic" or "openai")
        2. Auto-fallback to OpenAI when Anthropic key is empty

    Args:
        system_prompt: System-level instructions.
        user_message: User message content.
        settings: Application settings.
        dry_run_path: If settings.dry_run, write payload here instead of calling API.
        max_tokens: Override settings.claude_max_tokens for this call.

    Returns:
        ClaudeResponse with text, token counts, and cost.
    """
    if settings.dry_run:
        return _write_dry_run(system_prompt, user_message, settings, dry_run_path)

    provider = _resolve_provider(settings)

    if provider == "openai":
        return _call_openai(system_prompt, user_message, settings, max_tokens=max_tokens)
    return _call_anthropic(system_prompt, user_message, settings, max_tokens=max_tokens)


def _call_anthropic(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
) -> ClaudeResponse:
    """Call Anthropic Claude Messages API."""
    from anthropic import Anthropic

    effective_max_tokens = max_tokens or settings.claude_max_tokens

    client = Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.max_retries,
    )

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=effective_max_tokens,
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
    cost = calculate_cost(input_tokens, output_tokens, provider="anthropic")

    logger.info(
        "Anthropic call: %d in / %d out tokens, $%.4f (%s)",
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


def _call_openai(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
) -> ClaudeResponse:
    """Call OpenAI Chat Completions API as fallback."""
    from openai import OpenAI

    effective_max_tokens = max_tokens or settings.claude_max_tokens
    model = getattr(settings, "openai_llm_model", "gpt-4o")
    client = OpenAI(api_key=settings.openai_api_key)

    response = client.chat.completions.create(
        model=model,
        max_tokens=effective_max_tokens,
        temperature=settings.claude_temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )

    text = response.choices[0].message.content or ""
    input_tokens = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens
    cost = calculate_cost(input_tokens, output_tokens, provider="openai")

    logger.info(
        "OpenAI call: %d in / %d out tokens, $%.4f (%s)",
        input_tokens,
        output_tokens,
        cost,
        model,
    )

    return ClaudeResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        model=model,
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
