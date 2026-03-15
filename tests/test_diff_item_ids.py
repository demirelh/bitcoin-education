"""Tests for item_id presence and format in correction/adaptation diffs (Phase 5)."""

import re

from btcedu.core.adapter import compute_adaptation_diff
from btcedu.core.corrector import compute_correction_diff


def test_correction_diff_has_item_ids():
    original = "Das ist ein Bit Coin Test."
    corrected = "Das ist ein Bitcoin Test."
    result = compute_correction_diff(original, corrected, "ep1")
    changes = result["changes"]
    assert len(changes) > 0
    for c in changes:
        assert "item_id" in c, f"Missing item_id in change: {c}"


def test_correction_item_id_format():
    original = "a b c d e f g h i j k"
    corrected = "A b c D e f G h i J k"
    result = compute_correction_diff(original, corrected, "ep1")
    changes = result["changes"]
    assert len(changes) > 0
    pattern = re.compile(r"^corr-\d{4}$")
    for c in changes:
        assert pattern.match(c["item_id"]), f"Bad item_id format: {c['item_id']}"

    # Verify sequential: corr-0000, corr-0001, ...
    for i, c in enumerate(changes):
        assert c["item_id"] == f"corr-{i:04d}"


def test_adaptation_diff_has_item_ids():
    translation = "Bu bir test metnidir."
    adapted = "Bu bir [T1: test metni → test içeriği] metnidir. [T2: test → deneme]"
    result = compute_adaptation_diff(translation, adapted, "ep1")
    adaptations = result["adaptations"]
    assert len(adaptations) > 0
    for a in adaptations:
        assert "item_id" in a, f"Missing item_id in adaptation: {a}"


def test_adaptation_item_id_format():
    translation = "text"
    adapted = "[T1: a → b] [T2: c → d] [T1: e → f]"
    result = compute_adaptation_diff(translation, adapted, "ep1")
    adaptations = result["adaptations"]
    assert len(adaptations) == 3
    pattern = re.compile(r"^adap-\d{4}$")
    for a in adaptations:
        assert pattern.match(a["item_id"]), f"Bad item_id format: {a['item_id']}"

    for i, a in enumerate(adaptations):
        assert a["item_id"] == f"adap-{i:04d}"


def test_item_id_stable_across_reruns():
    original = "Das Bitcoin Protokoll ist gut."
    corrected = "Das Bitcoin-Protokoll ist gut."
    result1 = compute_correction_diff(original, corrected, "ep1")
    result2 = compute_correction_diff(original, corrected, "ep1")
    ids1 = [c["item_id"] for c in result1["changes"]]
    ids2 = [c["item_id"] for c in result2["changes"]]
    assert ids1 == ids2
