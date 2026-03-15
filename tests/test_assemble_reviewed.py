"""Tests for correction/adaptation assembly functions (Phase 5)."""

import pytest

from btcedu.core.reviewer import (
    _assemble_adaptation_review,
    _assemble_correction_review,
    _sidecar_path,
)
from btcedu.models.review_item import ReviewItemAction


class _FakeDecision:
    """Lightweight stand-in for ReviewItemDecision (avoids SQLAlchemy instrumentation)."""

    def __init__(self, item_id, action, edited_text=None):
        self.item_id = item_id
        self.action = action
        self.edited_text = edited_text


def _make_decision(item_id, action, edited_text=None):
    return _FakeDecision(item_id, action, edited_text)


# ── Correction assembly tests ─────────────────────────────────────────────


def test_assemble_correction_all_accepted():
    original = "Das ist ein Test."
    # One replacement: "Test" → "Prüfung"
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "Test.",
            "corrected": "Prüfung.",
            "position": {"start_word": 3, "end_word": 4},
        }
    ]
    decisions = {"corr-0000": _make_decision("corr-0000", ReviewItemAction.ACCEPTED.value)}
    result = _assemble_correction_review(original, changes, decisions)
    assert "Prüfung." in result
    assert "Test." not in result


def test_assemble_correction_all_rejected():
    original = "Das ist ein Test."
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "Test.",
            "corrected": "Prüfung.",
            "position": {"start_word": 3, "end_word": 4},
        }
    ]
    decisions = {"corr-0000": _make_decision("corr-0000", ReviewItemAction.REJECTED.value)}
    result = _assemble_correction_review(original, changes, decisions)
    assert "Test." in result
    assert "Prüfung." not in result


def test_assemble_correction_mixed():
    original = "a b c d e"
    # Three changes at positions 0-1, 2-3, 4-5
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "a",
            "corrected": "A",
            "position": {"start_word": 0, "end_word": 1},
        },
        {
            "item_id": "corr-0001",
            "type": "replace",
            "original": "c",
            "corrected": "C",
            "position": {"start_word": 2, "end_word": 3},
        },
        {
            "item_id": "corr-0002",
            "type": "replace",
            "original": "e",
            "corrected": "E",
            "position": {"start_word": 4, "end_word": 5},
        },
    ]
    decisions = {
        "corr-0000": _make_decision("corr-0000", ReviewItemAction.ACCEPTED.value),
        "corr-0001": _make_decision("corr-0001", ReviewItemAction.REJECTED.value),
        "corr-0002": _make_decision("corr-0002", ReviewItemAction.ACCEPTED.value),
    }
    result = _assemble_correction_review(original, changes, decisions)
    tokens = result.split()
    assert tokens[0] == "A"  # accepted
    assert tokens[1] == "b"  # gap word
    assert tokens[2] == "c"  # rejected → original
    assert tokens[3] == "d"  # gap word
    assert tokens[4] == "E"  # accepted


def test_assemble_correction_edited():
    original = "Das ist ein Test."
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "Test.",
            "corrected": "Prüfung.",
            "position": {"start_word": 3, "end_word": 4},
        }
    ]
    decisions = {
        "corr-0000": _make_decision(
            "corr-0000", ReviewItemAction.EDITED.value, edited_text="Experiment."
        )
    }
    result = _assemble_correction_review(original, changes, decisions)
    assert "Experiment." in result
    assert "Test." not in result
    assert "Prüfung." not in result


def test_assemble_correction_pending_defaults_to_proposed():
    """Pending (no decision) defaults to accepting the proposed change."""
    original = "Das ist ein Test."
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "Test.",
            "corrected": "Prüfung.",
            "position": {"start_word": 3, "end_word": 4},
        }
    ]
    # Empty decisions dict → pending → use proposed
    result = _assemble_correction_review(original, changes, {})
    assert "Prüfung." in result
    assert "Test." not in result


def test_assemble_correction_unchanged_keeps_original():
    """UNCHANGED action is equivalent to REJECTED: emit original words."""
    original = "Das ist ein Test."
    changes = [
        {
            "item_id": "corr-0000",
            "type": "replace",
            "original": "Test.",
            "corrected": "Prüfung.",
            "position": {"start_word": 3, "end_word": 4},
        }
    ]
    decisions = {"corr-0000": _make_decision("corr-0000", ReviewItemAction.UNCHANGED.value)}
    result = _assemble_correction_review(original, changes, decisions)
    assert "Test." in result
    assert "Prüfung." not in result


# ── Adaptation assembly tests ─────────────────────────────────────────────


def test_assemble_adaptation_accepted():
    """Accepted: keep the existing adapted text (marker tag remains)."""
    adapted = "Merhaba [T1: test → deneme] dünya."
    adaptations = [
        {
            "item_id": "adap-0000",
            "tier": "T1",
            "original": "test",
            "adapted": "deneme",
            "position": {"start": 8, "end": 27},  # covers "[T1: test → deneme]"
        }
    ]
    decisions = {"adap-0000": _make_decision("adap-0000", ReviewItemAction.ACCEPTED.value)}
    result = _assemble_adaptation_review(adapted, adaptations, decisions)
    assert "[T1: test → deneme]" in result


def test_assemble_adaptation_rejected():
    """Rejected: replace marker tag with original text."""
    adapted = "Merhaba [T1: test → deneme] dünya."
    tag = "[T1: test → deneme]"
    start = adapted.index(tag)
    end = start + len(tag)
    adaptations = [
        {
            "item_id": "adap-0000",
            "tier": "T1",
            "original": "test",
            "adapted": "deneme",
            "position": {"start": start, "end": end},
        }
    ]
    decisions = {"adap-0000": _make_decision("adap-0000", ReviewItemAction.REJECTED.value)}
    result = _assemble_adaptation_review(adapted, adaptations, decisions)
    assert "[T1:" not in result
    assert "test" in result


def test_assemble_adaptation_edited():
    """Edited: replace marker tag with edited_text."""
    adapted = "Merhaba [T1: test → deneme] dünya."
    tag = "[T1: test → deneme]"
    start = adapted.index(tag)
    end = start + len(tag)
    adaptations = [
        {
            "item_id": "adap-0000",
            "tier": "T1",
            "original": "test",
            "adapted": "deneme",
            "position": {"start": start, "end": end},
        }
    ]
    decisions = {
        "adap-0000": _make_decision(
            "adap-0000", ReviewItemAction.EDITED.value, edited_text="özel metin"
        )
    }
    result = _assemble_adaptation_review(adapted, adaptations, decisions)
    assert "özel metin" in result
    assert "[T1:" not in result


def test_assemble_adaptation_pending_defaults_to_accepted():
    """Pending adaptations keep the adapted text (accept proposed)."""
    adapted = "Merhaba [T1: test → deneme] dünya."
    tag = "[T1: test → deneme]"
    start = adapted.index(tag)
    end = start + len(tag)
    adaptations = [
        {
            "item_id": "adap-0000",
            "tier": "T1",
            "original": "test",
            "adapted": "deneme",
            "position": {"start": start, "end": end},
        }
    ]
    # Empty decisions → pending → keep adapted
    result = _assemble_adaptation_review(adapted, adaptations, {})
    assert "[T1: test → deneme]" in result


# ── Sidecar path tests ─────────────────────────────────────────────────────


def test_sidecar_path_correction(tmp_path):
    settings = type("S", (), {"outputs_dir": str(tmp_path)})()
    path = _sidecar_path("ep123", "correct", settings)
    assert str(path).endswith("ep123/review/transcript.reviewed.de.txt")


def test_sidecar_path_adaptation(tmp_path):
    settings = type("S", (), {"outputs_dir": str(tmp_path)})()
    path = _sidecar_path("ep123", "adapt", settings)
    assert str(path).endswith("ep123/review/script.adapted.reviewed.tr.md")


def test_sidecar_path_invalid_stage(tmp_path):
    settings = type("S", (), {"outputs_dir": str(tmp_path)})()
    with pytest.raises(ValueError, match="No sidecar path"):
        _sidecar_path("ep123", "render", settings)
