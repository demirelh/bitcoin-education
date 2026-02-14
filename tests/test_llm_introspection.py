"""Tests for LLM introspection utility."""

import json

from btcedu.utils.llm_introspection import (
    format_full_report,
    generate_json_summary,
    generate_llm_provider_report,
)


def test_generate_llm_provider_report():
    """Test that the full report generates correctly."""
    report = generate_llm_provider_report()

    # Check metadata
    assert "metadata" in report
    assert "generated_at" in report["metadata"]
    assert "report_version" in report["metadata"]

    # Check all required sections exist
    assert "sections" in report
    sections = report["sections"]
    assert "current_runtime_model" in sections
    assert "model_routing_capability" in sections
    assert "known_llm_providers" in sections
    assert "claude_model_family" in sections
    assert "non_claude_models" in sections
    assert "limitations" in sections


def test_generate_json_summary():
    """Test that JSON summary follows the required schema."""
    summary = generate_json_summary()

    # Check all required keys exist
    required_keys = [
        "current_runtime_model",
        "model_routing_supported",
        "providers_known",
        "providers_likely_available",
        "claude_models_known",
        "claude_models_likely_accessible",
        "other_models_known",
        "other_models_likely_accessible",
        "notes",
    ]

    for key in required_keys:
        assert key in summary, f"Missing required key: {key}"

    # Check current_runtime_model structure
    assert "provider" in summary["current_runtime_model"]
    assert "model" in summary["current_runtime_model"]
    assert "confidence" in summary["current_runtime_model"]

    # Check model_routing_supported structure
    assert "value" in summary["model_routing_supported"]
    assert "confidence" in summary["model_routing_supported"]

    # Check that provider lists are actually lists
    assert isinstance(summary["providers_known"], list)
    assert isinstance(summary["providers_likely_available"], list)
    assert isinstance(summary["claude_models_known"], list)
    assert isinstance(summary["claude_models_likely_accessible"], list)
    assert isinstance(summary["other_models_known"], list)
    assert isinstance(summary["other_models_likely_accessible"], list)

    # Check that notes is a string
    assert isinstance(summary["notes"], str)
    assert len(summary["notes"]) > 0


def test_json_summary_is_valid_json():
    """Test that the JSON summary can be serialized and deserialized."""
    summary = generate_json_summary()

    # Should be able to serialize
    json_str = json.dumps(summary, indent=2, ensure_ascii=False)
    assert len(json_str) > 0

    # Should be able to deserialize
    parsed = json.loads(json_str)
    assert parsed == summary


def test_format_full_report():
    """Test that the full report generates a string."""
    report = format_full_report()

    assert isinstance(report, str)
    assert len(report) > 0

    # Check that all sections are present in the output
    assert "SECTION 1" in report
    assert "SECTION 2" in report
    assert "SECTION 3" in report
    assert "SECTION 4" in report
    assert "SECTION 5" in report
    assert "SECTION 6" in report
    assert "FINAL_JSON_SUMMARY" in report


def test_report_contains_current_model_info():
    """Test that the report contains information about the current model."""
    summary = generate_json_summary()

    # Should mention Claude/Anthropic
    assert summary["current_runtime_model"]["provider"] == "Anthropic"
    assert "claude" in summary["current_runtime_model"]["model"].lower()

    # Should list Anthropic in known providers
    assert "Anthropic" in summary["providers_known"]


def test_report_contains_provider_lists():
    """Test that the report contains expected LLM providers."""
    summary = generate_json_summary()

    # Should know about major providers
    providers = summary["providers_known"]
    assert "Anthropic" in providers
    assert "OpenAI" in providers
    assert "Google" in providers

    # Should indicate Anthropic is available
    available = summary["providers_likely_available"]
    assert any("Anthropic" in item for item in available)


def test_report_contains_claude_models():
    """Test that the report lists Claude model families."""
    summary = generate_json_summary()

    claude_models = summary["claude_models_known"]
    assert len(claude_models) > 0

    # Should contain Opus, Sonnet, and Haiku models
    models_str = " ".join(claude_models)
    assert "opus" in models_str.lower()
    assert "sonnet" in models_str.lower()
    assert "haiku" in models_str.lower()
