"""Tests for Copilot CLI transport, refusal detection, and fail-loud behavior.

Regression coverage for the bug where the LLM backend (Copilot CLI) refused a
translation/adaptation task, the refusal was not detected (marker list too
narrow), and the refusal text was returned as pipeline content — silently
dropping source stories from the published video.
"""

import json
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.services.claude_service import (
    ClaudeResponse,
    ModelRefusalError,
    _call_copilot_cli,
    _copilot_prompt_argument,
    _extract_json_object,
    _is_copilot_refusal,
    _parse_copilot_jsonl,
    call_claude,
)

# Real refusal variants captured from production outputs.
ADAPT_REFUSAL = (
    "I appreciate you providing the detailed instructions, but I need to "
    "respectfully decline this task.\n\nThis is a specialized news translation "
    "and localization request that is completely unrelated to software "
    "development or the bitcoin-education repository. As the GitHub Copilot CLI, "
    "my purpose is to assist with code development."
)
TRANSLATE_REFUSAL = (
    "I appreciate the detailed instructions, but this appears to be a text "
    "translation/adaptation task for German-to-Turkish news transcripts, which "
    "is unrelated to the bitcoin-education repository where I'm currently "
    "working.\n\nAs the GitHub Copilot CLI, I'm designed to help with code "
    "development."
)
LEGIT_TURKISH = (
    "Bugün İtalya'da Morandi Köprüsü davasında kararlar açıklandı. 43 kişi "
    "hayatını kaybetmişti. Baş sanık on iki yıl hapis cezasına çarptırıldı. "
    "Federal Hükümet vergi kaçakçılığıyla daha güçlü mücadele etmek istiyor."
)


@pytest.mark.parametrize("text", [ADAPT_REFUSAL, TRANSLATE_REFUSAL, ""])
def test_is_copilot_refusal_detects_production_variants(text):
    assert _is_copilot_refusal(text) is True


def test_is_copilot_refusal_passes_legit_turkish():
    assert _is_copilot_refusal(LEGIT_TURKISH) is False


def test_large_copilot_prompt_uses_tempfile_instruction():
    argument = _copilot_prompt_argument("x" * 60_000, "/tmp/prompt.txt")

    assert argument == (
        "Read the complete task from /tmp/prompt.txt and follow it exactly. "
        "Return only the requested final output."
    )


def test_copilot_jsonl_message_delta_extraction():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant.message_delta",
                    "data": {"messageId": "m1", "deltaContent": '{"status":'},
                }
            ),
            json.dumps(
                {
                    "type": "assistant.message_delta",
                    "data": {"messageId": "m1", "deltaContent": '"ok"}'},
                }
            ),
            json.dumps(
                {
                    "type": "assistant.usage",
                    "data": {"inputTokens": 123, "outputTokens": 45},
                }
            ),
        ]
    )

    text, input_tokens, output_tokens, event_types = _parse_copilot_jsonl(stdout)

    assert text == '{"status":"ok"}'
    assert input_tokens == 123
    assert output_tokens == 45
    assert event_types == ["assistant.message_delta", "assistant.usage"]


def test_copilot_jsonl_legacy_text_delta_remains_supported():
    stdout = json.dumps(
        {
            "type": "assistant.text_delta",
            "data": {"deltaContent": "legacy response"},
        }
    )

    text, _, _, _ = _parse_copilot_jsonl(stdout)

    assert text == "legacy response"


def test_copilot_json_extraction_preserves_non_json_for_stage_retry():
    text = "I could not produce the requested JSON."

    assert _extract_json_object(text) == text


@patch("btcedu.services.claude_service._copilot_cli_fallback_text")
@patch("subprocess.run")
def test_copilot_missing_assistant_event_fails_without_second_model_call(
    mock_run,
    mock_fallback,
):
    mock_run.return_value = CompletedProcess(
        args=["copilot"],
        returncode=0,
        stdout=json.dumps({"type": "session.idle", "data": {}}),
        stderr="",
    )
    settings = SimpleNamespace(
        copilot_cli_model="claude-sonnet-4.5",
        copilot_cli_binary="copilot",
        copilot_cli_timeout=30,
    )

    with pytest.raises(RuntimeError, match="no assistant content"):
        _call_copilot_cli("system", "user", settings, json_mode=True)

    mock_fallback.assert_not_called()
    cmd = mock_run.call_args.args[0]
    assert "--allow-all-tools" not in cmd
    assert "--available-tools=view" in cmd
    assert "--allow-tool=view" in cmd


def _refusal_response():
    return ClaudeResponse(
        text=ADAPT_REFUSAL,
        input_tokens=50,
        output_tokens=40,
        cost_usd=0.0,
        model="copilot/claude-sonnet-4.5",
    )


@patch("btcedu.services.claude_service._call_copilot_cli")
def test_call_claude_raises_on_persistent_refusal(mock_copilot):
    """When Copilot refuses on every attempt (no Anthropic key), fail loudly."""
    mock_copilot.return_value = _refusal_response()
    settings = Settings(
        llm_provider="copilot_cli",
        anthropic_api_key="",
        openai_api_key="",
        dry_run=False,
    )

    with pytest.raises(ModelRefusalError):
        call_claude("system", "translate this", settings)

    # Copilot was retried with the coding-frame reframing before giving up.
    assert mock_copilot.call_count >= 2


@patch("btcedu.services.claude_service._call_copilot_cli")
def test_call_claude_recovers_when_reframe_succeeds(mock_copilot):
    """A refusal on the first attempt is recovered by the coding-frame retry."""
    good = ClaudeResponse(
        text=LEGIT_TURKISH,
        input_tokens=100,
        output_tokens=80,
        cost_usd=0.0,
        model="copilot/claude-sonnet-4.5",
    )
    mock_copilot.side_effect = [_refusal_response(), good]
    settings = Settings(
        llm_provider="copilot_cli",
        anthropic_api_key="",
        openai_api_key="",
        dry_run=False,
    )

    result = call_claude("system", "translate this", settings)

    assert result.text == LEGIT_TURKISH
    assert mock_copilot.call_count == 2


@patch("btcedu.services.claude_service._call_copilot_cli")
def test_call_claude_json_mode_reframes_refusal_before_parsing(mock_copilot):
    good = ClaudeResponse(
        text='{"segments":[]}',
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.0,
        model="copilot/claude-sonnet-4.5",
    )
    mock_copilot.side_effect = [_refusal_response(), good]
    settings = Settings(
        llm_provider="copilot_cli",
        anthropic_api_key="",
        openai_api_key="",
        dry_run=False,
    )

    result = call_claude("system", "correct this", settings, json_mode=True)

    assert result.text == '{"segments":[]}'
    assert mock_copilot.call_count == 2


@patch("btcedu.services.claude_service._call_copilot_cli")
def test_call_claude_threads_model_override(mock_copilot):
    """model_override is forwarded to the Copilot CLI call (QA second opinion)."""
    mock_copilot.return_value = ClaudeResponse(
        text=LEGIT_TURKISH,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.0,
        model="copilot/gpt-5.6",
    )
    settings = Settings(
        llm_provider="copilot_cli",
        anthropic_api_key="",
        openai_api_key="",
        dry_run=False,
    )

    call_claude("system", "review this", settings, model_override="gpt-5.6")

    assert mock_copilot.call_args.kwargs["model_override"] == "gpt-5.6"
