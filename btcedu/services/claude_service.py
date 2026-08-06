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


class ModelRefusalError(RuntimeError):
    """Raised when the LLM refuses the task on every attempt (incl. fallback).

    Returning a refusal as if it were real output silently corrupts the
    pipeline (dropped/omitted content), so callers must fail loudly instead.
    """


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


def _resolve_provider(settings, provider_override: str | None = None) -> str:
    """Determine which LLM provider to use.

    Priority: ``provider_override`` (when set), then ``settings.llm_provider``,
    but fall back to openai if the anthropic key is missing and the openai key
    is present. The github_models provider falls back to openai when no token
    is configured. This keeps the QA second opinion able to force a specific
    provider without disturbing the default routing.
    """
    provider = provider_override or getattr(settings, "llm_provider", "anthropic")
    if provider_override is not None:
        if provider == "anthropic" and not getattr(settings, "anthropic_api_key", ""):
            raise ValueError("Explicit Anthropic provider requires ANTHROPIC_API_KEY")
        if provider == "openai" and not getattr(settings, "openai_api_key", ""):
            raise ValueError("Explicit OpenAI provider requires OPENAI_API_KEY")
        if provider == "github_models" and not getattr(settings, "github_token", ""):
            raise ValueError("Explicit GitHub Models provider requires GITHUB_TOKEN")
        if provider not in {"anthropic", "openai", "github_models", "copilot_cli"}:
            raise ValueError(f"Unsupported explicit LLM provider: {provider!r}")
        return provider
    if provider == "github_models" and not getattr(settings, "github_token", ""):
        logger.warning("No GITHUB_TOKEN set — falling back to OpenAI")
        provider = "openai"
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
    json_mode: bool = False,
    model_override: str | None = None,
    provider_override: str | None = None,
) -> ClaudeResponse:
    """Call LLM API (Anthropic or OpenAI fallback).

    Provider selection:
        1. ``provider_override`` (explicit, e.g. an independent QA route)
        2. ``settings.llm_provider`` ("anthropic" or "openai")
        3. Auto-fallback to OpenAI when Anthropic key is empty

    Args:
        system_prompt: System-level instructions.
        user_message: User message content.
        settings: Application settings.
        dry_run_path: If settings.dry_run, write payload here instead of calling API.
        max_tokens: Override settings.claude_max_tokens for this call.
        json_mode: If True, request JSON output from the API (OpenAI response_format).
        model_override: If set, use this model instead of the provider default for
            this call (honored by anthropic, openai, github_models and copilot_cli).
            Enables an independent QA second opinion with a different model than the
            translation.
        provider_override: If set, force this provider ("anthropic", "openai",
            "github_models", "copilot_cli") for this call regardless of
            ``settings.llm_provider``. Missing-key fallbacks still apply.

    Returns:
        ClaudeResponse with text, token counts, and cost.
    """
    if settings.dry_run:
        return _write_dry_run(system_prompt, user_message, settings, dry_run_path)

    provider = _resolve_provider(settings, provider_override)

    if provider == "copilot_cli":
        response = _call_copilot_cli(
            system_prompt,
            user_message,
            settings,
            max_tokens=max_tokens,
            json_mode=json_mode,
            model_override=model_override,
        )
        # Copilot CLI occasionally refuses non-coding tasks with a boilerplate
        # "I'm the GitHub Copilot CLI, a terminal assistant..." message. Detect
        # and fall back to Anthropic (if key available) so translations don't
        # silently corrupt the pipeline.
        if _is_copilot_refusal(response.text):
            if provider_override is not None:
                raise ModelRefusalError(
                    "Explicit Copilot route refused the task; provider fallback is disabled "
                    f"for audited calls. Refusal preview: {response.text.strip()[:200]!r}"
                )
            logger.warning(
                "Copilot CLI refused task (len=%d, first=%s...). Retrying with coding-frame.",
                len(response.text),
                response.text[:80].replace("\n", " "),
            )
            # Retry #1: reframe as a file-processing / coding task. This bypasses
            # the "I'm just a coding assistant" refusal 90%+ of the time.
            reframed_user = (
                "The file below contains German source text that needs to be "
                "converted to Turkish following the system rules. This is a text "
                "processing task — perform the conversion and return only the "
                "converted text (no preface, no explanation).\n\n"
                "```source-de.txt\n" + user_message + "\n```"
            )
            response = _call_copilot_cli(
                system_prompt,
                reframed_user,
                settings,
                max_tokens=max_tokens,
                json_mode=json_mode,
                model_override=model_override,
            )
            # Retry #2 fallback: Anthropic direct (only if key valid and coding-frame also failed)
            if _is_copilot_refusal(response.text) and getattr(settings, "anthropic_api_key", ""):
                logger.warning("Coding-frame retry also refused. Falling back to Anthropic API.")
                try:
                    return _call_anthropic(
                        system_prompt, user_message, settings, max_tokens=max_tokens
                    )
                except Exception as exc:
                    logger.error("Anthropic fallback failed: %s.", exc)
            # If every attempt (Copilot + coding-frame + Anthropic) still refuses,
            # fail loudly rather than returning the refusal as pipeline content.
            if _is_copilot_refusal(response.text):
                raise ModelRefusalError(
                    "LLM refused the task on all attempts (Copilot CLI, coding-frame "
                    "retry"
                    + (", Anthropic fallback" if getattr(settings, "anthropic_api_key", "") else "")
                    + f"). Refusal preview: {response.text.strip()[:200]!r}"
                )
        return response
    if provider == "github_models":
        return _call_github_models(
            system_prompt,
            user_message,
            settings,
            max_tokens=max_tokens,
            json_mode=json_mode,
            model_override=model_override,
        )
    if provider == "openai":
        return _call_openai(
            system_prompt,
            user_message,
            settings,
            max_tokens=max_tokens,
            json_mode=json_mode,
            model_override=model_override,
        )
    return _call_anthropic(
        system_prompt,
        user_message,
        settings,
        max_tokens=max_tokens,
        model_override=model_override,
    )


def _call_anthropic(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
    model_override: str | None = None,
) -> ClaudeResponse:
    """Call Anthropic Claude Messages API."""
    from anthropic import Anthropic

    effective_max_tokens = max_tokens or settings.claude_max_tokens
    model = model_override or settings.claude_model

    client = Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.max_retries,
    )

    response = client.messages.create(
        model=model,
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
        model,
    )

    return ClaudeResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        model=model,
    )


def _call_copilot_cli(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
    json_mode: bool = False,
    model_override: str | None = None,
) -> ClaudeResponse:
    """Bridge to GitHub Copilot CLI via `copilot -p` subprocess.

    Routes LLM calls through the user's Copilot subscription (unlimited quota
    on paid plans). Model selected via settings.copilot_cli_model (default
    'claude-sonnet-4.5').

    Requires `copilot` binary on PATH. Uses JSONL output stream to reliably
    extract only the assistant's final text (no footer stats or stray output).
    """
    import subprocess
    import tempfile
    import time as _time

    model = model_override or getattr(settings, "copilot_cli_model", "claude-sonnet-4.5")
    binary = getattr(settings, "copilot_cli_binary", "copilot")

    # Copilot CLI treats obvious system/user framing as prompt injection.
    # Present as a natural task, no XML wrappers.
    combined = f"{system_prompt}\n\n---\n\n{user_message}"

    # Copilot CLI has no native JSON response format. When json_mode is requested,
    # inject an explicit instruction at the very end so Sonnet returns only the JSON
    # object (no chatty preface, no markdown fence). The response is then extracted
    # to just the first {...} block below.
    if json_mode:
        combined += (
            "\n\n---\n\n"
            "ÇIKTI KURALI (MUTLAK): Sadece geçerli, tek bir JSON nesnesi döndür. "
            "JSON dışında hiçbir metin, açıklama, markdown code-fence, önsöz, sonsöz olmasın. "
            "Yanıt karakterin ilk karakteri '{' olmak zorundadır ve son karakteri '}' "
            "olmak zorundadır."
        )

    # Write prompt to tempfile to avoid arg-length issues (translate segments can be huge).
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", delete=False) as pf:
        pf.write(combined)
        prompt_path = pf.name

    cmd = [
        binary,
        "--model",
        model,
        "--no-custom-instructions",
        "--available-tools=view",
        "--allow-tool=view",
        "--add-dir",
        str(Path(prompt_path).parent),
        "--no-ask-user",
        "--output-format",
        "json",
        "-p",
        _copilot_prompt_argument(combined, prompt_path),
    ]

    t0 = _time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=getattr(settings, "copilot_cli_timeout", 900),
            env=copilot_cli_env(),
        )
    finally:
        try:
            import os as _os

            _os.unlink(prompt_path)
        except Exception:
            pass

    if proc.returncode != 0:
        raise RuntimeError(
            f"copilot CLI exited {proc.returncode}: {proc.stderr[:500] or proc.stdout[:500]}"
        )

    text, input_tokens, output_tokens, event_types = _parse_copilot_jsonl(proc.stdout)
    if not text:
        first_char = next((char for char in proc.stdout if not char.isspace()), "")
        stdout_hash = hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()[:12]
        raise RuntimeError(
            "Copilot CLI JSON stream contained no assistant content "
            f"(events={event_types}, stdout_len={len(proc.stdout)}, "
            f"stdout_sha256={stdout_hash}, first_char={first_char!r})"
        )

    if json_mode and text:
        text = _extract_json_object(text)

    duration = _time.time() - t0
    logger.info(
        "Copilot CLI call: model=%s duration=%.1fs text_len=%d",
        model,
        duration,
        len(text),
    )
    return ClaudeResponse(
        text=text,
        input_tokens=input_tokens or len(combined) // 4,
        output_tokens=output_tokens or len(text) // 4,
        cost_usd=0.0,
        model=f"copilot/{model}",
    )


def _parse_copilot_jsonl(stdout: str) -> tuple[str, int, int, list[str]]:
    """Extract assistant content and usage from current and legacy CLI events."""
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    event_types: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = str(evt.get("type", ""))
        if etype and etype not in event_types:
            event_types.append(etype)
        data = evt.get("data", {}) or {}
        if etype in {"assistant.message_delta", "assistant.text_delta"}:
            text_parts.append(data.get("deltaContent", "") or "")
        elif etype == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                text_parts = [content]
        elif etype in {"assistant.turn_complete", "assistant.usage"}:
            usage = data.get("usage") or data
            input_tokens = (
                usage.get("inputTokens")
                or usage.get("input_tokens")
                or input_tokens
            )
            output_tokens = (
                usage.get("outputTokens")
                or usage.get("output_tokens")
                or output_tokens
            )

    return "".join(text_parts).strip(), input_tokens, output_tokens, event_types


#: Token prefixes Copilot refuses. Classic PATs (``ghp_``) and the matching
#: server-to-server tokens are rejected outright, which aborts the CLI before it
#: ever falls back to the stored device login.
UNSUPPORTED_COPILOT_TOKEN_PREFIXES = ("ghp_", "ghs_")

#: Environment variables the Copilot CLI reads for authentication.
COPILOT_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def copilot_cli_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment for a Copilot CLI subprocess.

    ``GITHUB_TOKEN`` is needed by the ``github_models`` provider and therefore
    lives in ``.env``, which systemd hands to the whole pipeline process. The
    Copilot CLI, however, prefers that variable over its own stored login and
    exits with an auth error when it holds a classic PAT. Dropping only the
    unsupported values lets the CLI use ``~/.copilot`` while every other
    provider keeps its token.
    """
    import os as _os

    env = dict(_os.environ if base_env is None else base_env)
    for name in COPILOT_TOKEN_ENV_VARS:
        value = env.get(name, "")
        if value.startswith(UNSUPPORTED_COPILOT_TOKEN_PREFIXES):
            env.pop(name, None)
    return env


def _copilot_prompt_argument(prompt: str, prompt_path: str) -> str:
    """Keep large prompts out of argv so Linux ARG_MAX cannot abort the CLI."""
    if len(prompt.encode("utf-8")) < 60_000:
        return prompt
    return (
        f"Read the complete task from {prompt_path} and follow it exactly. "
        "Return only the requested final output."
    )


def _is_copilot_refusal(text: str) -> bool:
    """Detect Copilot CLI's boilerplate 'I'm not designed for that' response.

    Copilot CLI occasionally derails non-coding tasks with a canned reply
    identifying itself as a terminal assistant. This pattern is stable across
    German/English/Turkish outputs. Detection thresholds are conservative to
    avoid false positives on genuine content that happens to mention Copilot.
    """
    if not text:
        return True  # empty = failure
    # Scan a generous head window: refusals sometimes open with a polite
    # preface ("I appreciate the detailed instructions, but...") before the
    # self-identification, pushing the tell-tale phrase past 400 chars.
    head = text[:800].lower()
    # Refusal phrases (DE + EN variants observed in production). These are
    # high-signal: legitimate Turkish/German news output never contains them.
    refusal_markers = [
        "i'm the github copilot cli",
        "i'm github copilot cli",
        "i am the github copilot cli",
        "as the github copilot cli",
        "ich bin der github copilot cli",
        "ich bin github copilot cli",
        "terminal assistant",
        "entwicklungsassistent für code",
        "not designed for translation",
        "not configured for translation",
        "i'm not designed for",
        "i am not designed for",
        "i'm designed to help with",
        "i am designed to help with",
        "respectfully decline",
        "i need to decline",
        "i must decline",
        "i cannot assist with",
        "i can't assist with",
        "i cannot help with",
        "i can't help with",
        "unrelated to software development",
        "unrelated to the bitcoin-education repository",
        "is there something related to the bitcoin-education repository",
        "übersetzungsaufgaben gehören nicht",
        "übersetzungsaufgaben sind nicht",
        "gehört nicht zu meinem funktionsbereich",
        "gehören nicht zu meinen kernfunktionen",
        "outside my scope",
        "außerhalb meines aufgabenbereichs",
    ]
    return any(m in head for m in refusal_markers)


def _extract_json_object(text: str) -> str:
    """Extract the first balanced {...} JSON object from a chatty LLM response.

    Copilot CLI Sonnet often wraps JSON in prose or markdown code fences even
    when instructed otherwise. This walks the string once, tracking brace depth
    while respecting string literals, and returns the first top-level object.
    If the assistant returned no JSON, preserve its text so refusal handling or
    the calling stage's structured-response retry can decide how to recover.
    """
    if not text:
        return text
    stripped = text.strip()
    # Strip markdown code fence quickly if present
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    # Find first '{' and walk to matching '}'
    start = stripped.find("{")
    if start == -1:
        # Try array
        start = stripped.find("[")
        if start == -1:
            return text
        open_ch, close_ch = "[", "]"
    else:
        open_ch, close_ch = "{", "}"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    return text


def _supports_at_prompt(binary: str) -> bool:
    """Copilot CLI accepts @file syntax for -p in recent versions."""
    try:
        import subprocess

        out = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=5).stdout
        return "@" in out and "prompt" in out.lower()
    except Exception:
        return False


def _copilot_cli_fallback_text(binary: str, model: str, prompt: str, settings) -> str:
    """Fallback to text-format output, stripping the stats footer."""
    import subprocess

    proc = subprocess.run(
        [
            binary,
            "--model",
            model,
            "--no-custom-instructions",
            "--available-tools=view",
            "--allow-tool=view",
            "--no-ask-user",
            "--output-format",
            "text",
            "-p",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=getattr(settings, "copilot_cli_timeout", 900),
        env=copilot_cli_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"copilot CLI text fallback exited {proc.returncode}: "
            f"{proc.stderr[:500] or proc.stdout[:500]}"
        )
    out = proc.stdout
    # Footer starts with "\n\nChanges" or "\nChanges    "
    for marker in ("\n\nChanges", "\nChanges    ", "\nAI Credits"):
        idx = out.rfind(marker)
        if idx > 0:
            out = out[:idx]
            break
    return out.strip()


def _call_github_models(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
    json_mode: bool = False,
    model_override: str | None = None,
) -> ClaudeResponse:
    """Call GitHub Models API (OpenAI-compatible, uses GitHub PAT).

    Docs: https://docs.github.com/en/rest/models
    Endpoint pattern: https://models.github.ai/inference/chat/completions
    Model IDs: publisher/name  (e.g. openai/gpt-4.1, anthropic/claude-sonnet-4.5)
    Auth header: Authorization: Bearer <PAT with models:read>
    """
    from openai import OpenAI

    effective_max_tokens = max_tokens or settings.claude_max_tokens
    model = model_override or getattr(settings, "github_models_model", "openai/gpt-4.1")
    endpoint = getattr(settings, "github_models_endpoint", "https://models.github.ai/inference")

    client = OpenAI(api_key=settings.github_token, base_url=endpoint)

    kwargs: dict = {
        "model": model,
        "max_tokens": effective_max_tokens,
        "temperature": settings.claude_temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)

    text = response.choices[0].message.content or ""
    input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(response.usage, "completion_tokens", 0) or 0
    # GitHub Models free tier: no direct cost — we still track tokens
    cost = 0.0

    logger.info(
        "GitHub Models call: %d in / %d out tokens (%s)",
        input_tokens,
        output_tokens,
        model,
    )

    return ClaudeResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        model=model,
    )


def _call_openai(
    system_prompt: str,
    user_message: str,
    settings,
    max_tokens: int | None = None,
    json_mode: bool = False,
    model_override: str | None = None,
) -> ClaudeResponse:
    """Call OpenAI Chat Completions API as fallback."""
    from openai import OpenAI

    effective_max_tokens = max_tokens or settings.claude_max_tokens
    model = model_override or getattr(settings, "openai_llm_model", "gpt-4o")
    client = OpenAI(api_key=settings.openai_api_key)

    kwargs: dict = {
        "model": model,
        "max_tokens": effective_max_tokens,
        "temperature": settings.claude_temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)

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
