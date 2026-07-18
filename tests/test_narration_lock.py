"""Phase 8 requirement A: deterministic narration-fidelity lock.

Covers the pure lock helpers, the shared canonical-narration source, and the
chapterization integration (unchanged passes / changed narration caught /
approved hash stable / RED diagnostic + fail-closed).
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.narration_lock import (
    check_narration_lock,
    compose_chapter_narration,
    normalize_narration_text,
)
from btcedu.models.prompt_version import PromptVersion  # noqa: F401 (table registration)

# ---------------------------------------------------------------------------
# Pure normalization / comparison
# ---------------------------------------------------------------------------


def test_normalize_collapses_whitespace_and_folds_typography():
    raw = "Merhaba\n\n  „dünya\u201d  —  test\u2019s"
    normalized = normalize_narration_text(raw)
    # newlines collapse to single spaces; curly quotes -> straight; em dash -> hyphen
    assert normalized == 'Merhaba "dünya" - test\'s'


def test_normalize_is_nfkc_but_preserves_digits_and_case():
    # full-width digits fold under NFKC; ASCII digits/letters/case are untouched
    assert normalize_narration_text("１２３") == "123"
    assert normalize_narration_text("Merkel 25") == "Merkel 25"
    # case is preserved (names are protected — not casefolded)
    assert normalize_narration_text("Scholz") != normalize_narration_text("scholz")


def test_lock_matches_after_technical_normalization_only():
    approved = "Berlin'de bugün 25 derece. „Hava güzel\u201d dedi."
    # same content, different whitespace + typographic quotes + em dash spacing
    composed = "Berlin'de bugün 25 derece.\n\n\"Hava güzel\" dedi."
    result = check_narration_lock(approved, composed)
    assert result.matches is True


def test_lock_detects_changed_number():
    result = check_narration_lock("Zam 25 derece oldu.", "Zam 35 derece oldu.")
    assert result.matches is False
    assert "25" in (result.removed_numbers or [])
    assert "35" in (result.added_numbers or [])


def test_lock_detects_dropped_name():
    result = check_narration_lock(
        "Merkel ve Scholz konuştu.", "Merkel konuştu."
    )
    assert result.matches is False
    assert "Scholz" in (result.removed_names or [])


def test_lock_detects_added_sentence():
    result = check_narration_lock(
        "Bitcoin bir para birimidir.",
        "Bitcoin bir para birimidir. Kanala abone olun!",
    )
    assert result.matches is False


def test_lock_detects_reordering():
    result = check_narration_lock("Bir. Iki. Uc.", "Iki. Bir. Uc.")
    assert result.matches is False


def test_compose_chapter_narration_orders_and_joins():
    ch1 = MagicMock()
    ch1.narration.text = "Birinci bolum."
    ch2 = MagicMock()
    ch2.narration.text = "Ikinci bolum."
    composed = compose_chapter_narration([ch1, ch2])
    assert composed == "Birinci bolum.\nIkinci bolum."


# ---------------------------------------------------------------------------
# Canonical narration source (shared by QA hash and chapterize lock)
# ---------------------------------------------------------------------------


def _settings(tmp_path):
    s = MagicMock()
    s.outputs_dir = str(tmp_path / "outputs")
    s.transcripts_dir = str(tmp_path / "transcripts")
    return s


def test_canonical_narration_adapted_mode_uses_file_text(tmp_path):
    from btcedu.core.qa_reviewer import canonical_narration

    settings = _settings(tmp_path)
    ep_dir = Path(settings.outputs_dir) / "ep1"
    ep_dir.mkdir(parents=True)
    (ep_dir / "script.adapted.tr.md").write_text("Merhaba dünya.", encoding="utf-8")

    path, text = canonical_narration(settings, "ep1")
    assert path.name == "script.adapted.tr.md"
    assert text == "Merhaba dünya."


def test_canonical_narration_story_mode_extracts_ordered_tr_not_raw_json(tmp_path):
    from btcedu.core.qa_reviewer import canonical_narration, narration_sha256

    settings = _settings(tmp_path)
    ep_dir = Path(settings.outputs_dir) / "ep2"
    ep_dir.mkdir(parents=True)
    stories = {
        "stories": [
            {"story_id": "s1", "text_de": "German one", "text_tr": "Birinci haber."},
            {"story_id": "s2", "text_de": "German two", "text_adapted_tr": "Ikinci haber."},
        ]
    }
    (ep_dir / "stories_translated.json").write_text(
        json.dumps(stories, ensure_ascii=False), encoding="utf-8"
    )

    path, text = canonical_narration(settings, "ep2")
    # Ordered TR narrative — NOT the raw JSON, and no German source text.
    assert text == "Birinci haber.\n\nIkinci haber."
    assert "German" not in text
    # narration_sha256 hashes this canonical text
    assert narration_sha256(settings, "ep2") == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_narration_sha256_is_stable(tmp_path):
    from btcedu.core.qa_reviewer import narration_sha256

    settings = _settings(tmp_path)
    ep_dir = Path(settings.outputs_dir) / "ep3"
    ep_dir.mkdir(parents=True)
    (ep_dir / "script.adapted.tr.md").write_text("Sabit metin.", encoding="utf-8")

    h1 = narration_sha256(settings, "ep3")
    h2 = narration_sha256(settings, "ep3")
    assert h1 == h2 and h1 is not None


# ---------------------------------------------------------------------------
# Chapterization integration
# ---------------------------------------------------------------------------

# Canonical narration is one sentence per chapter; a faithful partition splits it
# across chapters without changing any content.
_CANONICAL = (
    "Almanya'da bugün önemli gelişmeler yaşandı. "
    "Ekonomi bakanı 25 milyar euroluk paketi açıkladı. "
    "Hava durumu yarın için yağmur öngörüyor."
)


def _chapter(chapter_id, order, text, visual_type="b_roll", image_prompt="p", overlays=None):
    return {
        "chapter_id": chapter_id,
        "title": f"Bolum {order}",
        "order": order,
        "narration": {
            "text": text,
            "word_count": len(text.split()),
            "estimated_duration_seconds": max(1, round(len(text.split()) / 150 * 60)),
        },
        "visual": {"type": visual_type, "description": "d", "image_prompt": image_prompt},
        "overlays": overlays or [],
        "transitions": {"in": "fade", "out": "cut"},
    }


def _chapter_doc(chapters):
    total = sum(c["narration"]["estimated_duration_seconds"] for c in chapters)
    return {
        "schema_version": "1.0",
        "episode_id": "ep_lock",
        "title": "Test",
        "total_chapters": len(chapters),
        "estimated_duration_seconds": total,
        "chapters": chapters,
    }


def _setup_locked_episode(db_session, tmp_path, model_chapters):
    """Create an ADAPTED episode with a GREEN gate and mock the chapterize model."""
    from btcedu.core.qa_reviewer import narration_sha256
    from btcedu.models.episode import Episode, EpisodeStatus

    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.transcripts_dir = str(tmp_path / "transcripts")
    settings.claude_model = "claude-x"
    settings.claude_temperature = 0.2
    settings.claude_max_tokens = 16384
    settings.dry_run = False

    ep_dir = Path(settings.outputs_dir) / "ep_lock"
    ep_dir.mkdir(parents=True)
    (ep_dir / "script.adapted.tr.md").write_text(_CANONICAL, encoding="utf-8")

    # A GREEN translation quality gate locked to the current canonical narration.
    gate = {
        "episode_id": "ep_lock",
        "decision": "green",
        "status": "green",
        "narration_sha256": narration_sha256(settings, "ep_lock"),
        "narration_approved": True,
        "findings": [],
    }
    (ep_dir / "translation_quality_gate.json").write_text(
        json.dumps(gate), encoding="utf-8"
    )

    episode = Episode(
        episode_id="ep_lock",
        source="tagesschau_rss",
        title="Test",
        url="https://x",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.text = json.dumps(_chapter_doc(model_chapters))
    mock_response.input_tokens = 10
    mock_response.output_tokens = 10
    mock_response.cost_usd = 0.01
    return settings, mock_response


def _patch_registry(mock_registry):
    version = MagicMock()
    version.version = 1
    mock_registry.return_value.register_version.return_value = version
    mock_registry.return_value.resolve_template_path.return_value = Path("chapterize.md")
    mock_registry.return_value.load_template.return_value = (
        "",
        "# System\n\n# Input\n\n{{episode_id}}\n{{adapted_script}}",
    )
    mock_registry.return_value.compute_hash.return_value = "prompt_hash"


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_passes_when_narration_is_faithful_partition(
    mock_registry, mock_claude, db_session, tmp_path
):
    from btcedu.core.chapterizer import chapterize_script
    from btcedu.models.episode import Episode, EpisodeStatus

    # Split canonical across three chapters WITHOUT changing content.
    parts = [
        "Almanya'da bugün önemli gelişmeler yaşandı.",
        "Ekonomi bakanı 25 milyar euroluk paketi açıkladı.",
        "Hava durumu yarın için yağmur öngörüyor.",
    ]
    chapters = [_chapter(f"ch0{i+1}", i + 1, p) for i, p in enumerate(parts)]
    settings, mock_response = _setup_locked_episode(db_session, tmp_path, chapters)
    mock_claude.return_value = mock_response
    _patch_registry(mock_registry)

    result = chapterize_script(db_session, "ep_lock", settings, force=True)

    # Short chapters may be merged (a boundary change) but narration content is
    # preserved, so the lock passes and chapters/status/provenance are written.
    assert result.chapter_count >= 1
    chapters_path = Path(settings.outputs_dir) / "ep_lock" / "chapters.json"
    assert chapters_path.exists()
    ep = db_session.query(Episode).filter(Episode.episode_id == "ep_lock").first()
    assert ep.status == EpisodeStatus.CHAPTERIZED

    # Provenance records the locked, matching narration hashes.
    prov = json.loads(
        (Path(settings.outputs_dir) / "ep_lock" / "provenance" / "chapterize_provenance.json")
        .read_text()
    )
    assert prov["narration_locked"] is True
    assert prov["approved_narration_sha256"] == prov["composed_narration_sha256"]


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_fails_closed_when_narration_changed(
    mock_registry, mock_claude, db_session, tmp_path
):
    from btcedu.core.chapterizer import NarrationLockError, chapterize_script
    from btcedu.models.episode import Episode, EpisodeStatus

    # The model silently changes 25 -> 50 milyar and adds a CTA sentence.
    tampered = [
        _chapter("ch01", 1, "Almanya'da bugün önemli gelişmeler yaşandı."),
        _chapter("ch02", 2, "Ekonomi bakanı 50 milyar euroluk paketi açıkladı."),
        _chapter(
            "ch03",
            3,
            "Hava durumu yarın için yağmur öngörüyor. Kanala abone olmayı unutmayın!",
        ),
    ]
    settings, mock_response = _setup_locked_episode(db_session, tmp_path, tampered)
    mock_claude.return_value = mock_response
    _patch_registry(mock_registry)

    with pytest.raises(NarrationLockError):
        chapterize_script(db_session, "ep_lock", settings, force=True)

    # No chapters written, status not advanced, error recorded, RED diagnostic present.
    ep_dir = Path(settings.outputs_dir) / "ep_lock"
    assert not (ep_dir / "chapters.json").exists()
    ep = db_session.query(Episode).filter(Episode.episode_id == "ep_lock").first()
    assert ep.status == EpisodeStatus.ADAPTED
    assert ep.error_message and "narration" in ep.error_message.lower()
    diag = ep_dir / "provenance" / "chapterize_narration_lock.json"
    assert diag.exists()
    data = json.loads(diag.read_text())
    assert data["status"] == "red" and data["matches"] is False


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_no_lock_without_quality_gate(
    mock_registry, mock_claude, db_session, tmp_path
):
    """Legacy profiles without a QA gate keep hook/intro/outro freedom (no lock)."""
    from btcedu.core.chapterizer import chapterize_script
    from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun  # noqa: F401

    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.transcripts_dir = str(tmp_path / "transcripts")
    settings.claude_model = "claude-x"
    settings.claude_temperature = 0.2
    settings.claude_max_tokens = 16384
    settings.dry_run = False

    ep_dir = Path(settings.outputs_dir) / "ep_free"
    ep_dir.mkdir(parents=True)
    (ep_dir / "script.adapted.tr.md").write_text(_CANONICAL, encoding="utf-8")
    # NO translation_quality_gate.json -> lock does not apply.

    episode = Episode(
        episode_id="ep_free",
        source="youtube_rss",
        title="Test",
        url="https://x",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Chapters with a brand-new hook + CTA that are NOT in the source.
    chapters = _chapter_doc(
        [
            _chapter("ch01", 1, "Bugün her şeyi değiştirecek bir haber!", "title_card", None),
            _chapter("ch02", 2, _CANONICAL),
        ]
    )
    chapters["episode_id"] = "ep_free"
    resp = MagicMock()
    resp.text = json.dumps(chapters)
    resp.input_tokens = resp.output_tokens = 5
    resp.cost_usd = 0.01
    mock_claude.return_value = resp
    _patch_registry(mock_registry)

    result = chapterize_script(db_session, "ep_free", settings, force=True)
    assert result.chapter_count == 2
    assert (ep_dir / "chapters.json").exists()
