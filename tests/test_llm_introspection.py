"""Tests for LLM introspection utility."""

import json

from btcedu.utils.llm_introspection import (
    format_full_report,
    generate_constraints_table,
    generate_json_summary,
    generate_llm_provider_report,
    generate_models_table,
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


def test_generate_models_table():
    """Test that the models table generates correctly."""
    table = generate_models_table()

    assert isinstance(table, str)
    assert len(table) > 0

    # Check table structure
    assert "# Available Models Table" in table
    assert "| Provider | Model Family | Versions | Status | Type |" in table
    assert "Anthropic | Claude" in table
    assert "OpenAI" in table

    # Check legends exist
    assert "## Status Legend" in table
    assert "## Type Legend" in table


def test_generate_constraints_table():
    """Test that the constraints table generates correctly."""
    table = generate_constraints_table()

    assert isinstance(table, str)
    assert len(table) > 0

    # Check table structure
    assert "# Constraints Table" in table
    assert "| Constraint Category | Explanation |" in table

    # Check that constraint categories are present
    assert "Dependency on System Context" in table
    assert "No Direct API Access" in table
    assert "Configuration vs Runtime Reality" in table
    assert "Code Inspection Limitations" in table
    assert "Model Routing Internals" in table
    assert "Temporal Limitations" in table

    # Check summary exists
    assert "## Summary" in table


def test_models_table_markdown_valid():
    """Test that models table is valid markdown."""
    table = generate_models_table()

    # Count table rows (should have header + separator + data rows)
    lines = table.split("\n")
    table_lines = [line for line in lines if line.startswith("|")]

    # Should have at least header row + separator + some data
    assert len(table_lines) >= 3

    # Verify header and separator format
    assert table_lines[0].count("|") >= 5  # At least 5 columns
    assert table_lines[1].count("-") > 0  # Separator row


def test_constraints_table_markdown_valid():
    """Test that constraints table is valid markdown."""
    table = generate_constraints_table()

    # Count table rows
    lines = table.split("\n")
    table_lines = [line for line in lines if line.startswith("|")]

    # Should have header + separator + 6 constraint rows
    assert len(table_lines) >= 8

    # Verify all 6 constraints are in the table
    constraint_count = sum(
        1
        for line in table_lines
        if "| " in line and line != table_lines[0] and line != table_lines[1]
    )
    assert constraint_count == 6
