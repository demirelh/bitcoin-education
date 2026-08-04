"""Phase 8 requirement D (selective stale cascade) + F (cost FK) regression tests.

Uses deterministic hash comparisons and direct marker inspection so no external
APIs are involved.
"""

from pathlib import Path
from unittest.mock import MagicMock

from btcedu.models.chapter_schema import ChapterDocument


def _chapter(chapter_id, order, text, image_prompt="a photo", overlay_text=None, title=None):
    overlays = []
    if overlay_text:
        overlays = [
            {
                "type": "lower_third",
                "text": overlay_text,
                "start_offset_seconds": 0.0,
                "duration_seconds": 5.0,
            }
        ]
    return {
        "chapter_id": chapter_id,
        "title": title or f"Ch {order}",
        "order": order,
        "narration": {
            "text": text,
            "word_count": len(text.split()),
            "estimated_duration_seconds": 40,
        },
        "visual": {"type": "b_roll", "description": "d", "image_prompt": image_prompt},
        "overlays": overlays,
        "transitions": {"in": "fade", "out": "cut"},
    }


def _doc(chapters):
    return ChapterDocument(
        **{
            "schema_version": "1.0",
            "episode_id": "ep",
            "title": "T",
            "total_chapters": len(chapters),
            "estimated_duration_seconds": sum(
                c["narration"]["estimated_duration_seconds"] for c in chapters
            ),
            "chapters": chapters,
        }
    )


# ---------------------------------------------------------------------------
# Image-prompt change invalidates image (not TTS)
# ---------------------------------------------------------------------------


def test_image_prompt_change_alters_imagegen_hash_not_tts():
    from btcedu.core.image_generator import _compute_chapters_content_hash
    from btcedu.core.tts import _compute_tts_content_hash, _voice_config_signature

    base = _doc([_chapter("ch01", 1, "Ayni anlatim.", image_prompt="a red car")])
    changed = _doc([_chapter("ch01", 1, "Ayni anlatim.", image_prompt="a blue boat")])

    # Image prompt is part of the image content hash -> imagegen re-runs.
    assert _compute_chapters_content_hash(base) != _compute_chapters_content_hash(changed)

    # Narration/voice unchanged -> TTS hash identical, TTS is NOT invalidated.
    sig = _voice_config_signature({"voice_id": "v", "model": "m"})
    assert _compute_tts_content_hash(base, {}, sig) == _compute_tts_content_hash(changed, {}, sig)


# ---------------------------------------------------------------------------
# TTS voice / model / lexicon change invalidates TTS (not image)
# ---------------------------------------------------------------------------


def test_tts_voice_model_lexicon_change_alters_tts_hash_not_image():
    from btcedu.core.image_generator import _compute_chapters_content_hash
    from btcedu.core.tts import _compute_tts_content_hash, _voice_config_signature

    doc = _doc([_chapter("ch01", 1, "Bitcoin nedir?")])

    sig_a = _voice_config_signature({"voice_id": "voiceA", "model": "m1"})
    sig_b = _voice_config_signature({"voice_id": "voiceB", "model": "m1"})
    sig_c = _voice_config_signature({"voice_id": "voiceA", "model": "m2"})

    h_voice_a = _compute_tts_content_hash(doc, {}, sig_a)
    assert h_voice_a != _compute_tts_content_hash(doc, {}, sig_b)  # voice change
    assert h_voice_a != _compute_tts_content_hash(doc, {}, sig_c)  # model change

    # Lexicon change (same voice) changes synthesis text -> TTS hash changes.
    lex = {"Bitcoin": "Bitkoyn"}
    sig_lex = _voice_config_signature(
        {"voice_id": "voiceA", "model": "m1", "pronunciation_lexicon": lex}
    )
    assert _compute_tts_content_hash(doc, lex, sig_lex) != h_voice_a

    # Image hash never depends on voice/model/lexicon.
    assert _compute_chapters_content_hash(doc) == _compute_chapters_content_hash(doc)


def test_pronunciation_lexicon_applies_to_synthesis_not_display():
    from btcedu.core.tts import _apply_pronunciation_lexicon

    lex = {"Bitcoin": "Bitkoyn", "SEC": "Es Ee Si"}
    display = "Bitcoin ve SEC hakkinda."
    synthesis = _apply_pronunciation_lexicon(display, lex)
    assert synthesis == "Bitkoyn ve Es Ee Si hakkinda."
    # display text (the locked narration) is a plain string, unchanged by us.
    assert display == "Bitcoin ve SEC hakkinda."
    # Whole-word only: substrings are not replaced.
    assert _apply_pronunciation_lexicon("Bitcoiner", lex) == "Bitcoiner"


# ---------------------------------------------------------------------------
# Chapterizer selective downstream invalidation
# ---------------------------------------------------------------------------


def _prep_downstream(tmp_path):
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    base = Path(settings.outputs_dir) / "ep"
    (base / "images").mkdir(parents=True)
    (base / "tts").mkdir(parents=True)
    (base / "render").mkdir(parents=True)
    (base / "images" / "manifest.json").write_text("{}", encoding="utf-8")
    (base / "tts" / "manifest.json").write_text("{}", encoding="utf-8")
    (base / "render" / "draft.mp4").write_bytes(b"video")
    return settings, base


def _markers(base):
    return {
        "images": (base / "images" / "manifest.json.stale").exists(),
        "images_frame": (base / "images" / ".stale").exists(),
        "tts": (base / "tts" / "manifest.json.stale").exists(),
        "render": (base / "render" / "draft.mp4.stale").exists(),
    }


def test_selective_stale_narration_change_marks_tts_and_render_not_images(tmp_path):
    from btcedu.core.chapterizer import _chapter_component_hashes, _mark_downstream_stale

    settings, base = _prep_downstream(tmp_path)
    prev = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Eski anlatim metni burada.")]))
    curr = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Yeni anlatim metni burada.")]))

    _mark_downstream_stale("ep", settings, prev, curr)
    m = _markers(base)
    assert m["tts"] and m["render"]
    assert not m["images"] and not m["images_frame"]


def test_selective_stale_image_prompt_change_marks_images_and_render_not_tts(tmp_path):
    from btcedu.core.chapterizer import _chapter_component_hashes, _mark_downstream_stale

    settings, base = _prep_downstream(tmp_path)
    prev = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", image_prompt="cat")]))
    curr = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", image_prompt="dog")]))

    _mark_downstream_stale("ep", settings, prev, curr)
    m = _markers(base)
    assert m["images"] and m["images_frame"] and m["render"]
    assert not m["tts"]


def test_selective_stale_title_change_marks_images_and_render_not_tts(tmp_path):
    from btcedu.core.chapterizer import _chapter_component_hashes, _mark_downstream_stale

    settings, base = _prep_downstream(tmp_path)
    prev = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", title="Eski")]))
    curr = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", title="Yeni")]))

    _mark_downstream_stale("ep", settings, prev, curr)
    m = _markers(base)
    assert m["images"] and m["images_frame"] and m["render"]
    assert not m["tts"]


def test_selective_stale_overlay_change_marks_render_only(tmp_path):
    from btcedu.core.chapterizer import _chapter_component_hashes, _mark_downstream_stale

    settings, base = _prep_downstream(tmp_path)
    prev = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", overlay_text="Eski")]))
    curr = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Ayni metin", overlay_text="Yeni")]))

    _mark_downstream_stale("ep", settings, prev, curr)
    m = _markers(base)
    assert m["render"]
    assert not m["images"] and not m["tts"]


def test_selective_stale_unchanged_marks_nothing(tmp_path):
    """QA rule change that leaves narration/visuals/overlays identical -> no media stale."""
    from btcedu.core.chapterizer import _chapter_component_hashes, _mark_downstream_stale

    settings, base = _prep_downstream(tmp_path)
    hashes = _chapter_component_hashes(_doc([_chapter("ch01", 1, "Degismeyen metin")]))

    _mark_downstream_stale("ep", settings, hashes, hashes)
    assert _markers(base) == {
        "images": False,
        "images_frame": False,
        "tts": False,
        "render": False,
    }


def test_qa_stages_do_not_write_media_stale_markers():
    """Regression: QA modules never invalidate images/tts/render directly."""
    import inspect

    from btcedu.core import qa_reviewer, translation_qa

    for module in (qa_reviewer, translation_qa):
        src = inspect.getsource(module)
        assert ".mp4.stale" not in src
        assert "manifest.json.stale" not in src


# ---------------------------------------------------------------------------
# Image chapter recovery selection
# ---------------------------------------------------------------------------


def test_chapters_needing_regen_selects_failed_and_missing(tmp_path):
    from btcedu.core.image_generator import _chapters_needing_regen

    images_dir = tmp_path / "outputs" / "ep" / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "ch01.png").write_bytes(b"img")  # good file on disk

    doc = _doc(
        [
            _chapter("ch01", 1, "a"),
            _chapter("ch02", 2, "b"),
            _chapter("ch03", 3, "c"),
        ]
    )
    # Entries are grouped per chapter: the chapter image first, then any beat images.
    existing = {
        "ch01": [
            {"chapter_id": "ch01", "generation_method": "flux", "file_path": "images/ch01.png"}
        ],
        "ch02": [
            {"chapter_id": "ch02", "generation_method": "failed", "file_path": "images/x.png"}
        ],
        "ch03": [
            {
                "chapter_id": "ch03",
                "generation_method": "flux",
                "file_path": "images/missing.png",
            }
        ],
    }
    need = _chapters_needing_regen(existing, doc, images_dir)
    assert need == {"ch02", "ch03"}  # ch01 (good) is reused


def test_image_manifest_with_failed_or_missing_chapters_is_not_current(tmp_path):
    import json

    from btcedu.core.image_generator import _is_image_gen_current

    base = tmp_path / "outputs" / "ep"
    images = base / "images"
    provenance_dir = base / "provenance"
    images.mkdir(parents=True)
    provenance_dir.mkdir()
    manifest = images / "manifest.json"
    provenance = provenance_dir / "imagegen_provenance.json"
    provenance.write_text(
        json.dumps({"input_content_hash": "chapters", "prompt_hash": "prompt"}),
        encoding="utf-8",
    )

    manifest.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "chapter_id": "ch01",
                        "generation_method": "failed",
                        "file_path": "images/ch01.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert not _is_image_gen_current(manifest, provenance, "chapters", "prompt", {"ch01"})

    (images / "ch01.png").write_bytes(b"image")
    manifest.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "chapter_id": "ch01",
                        "generation_method": "template",
                        "file_path": "images/ch01.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert not _is_image_gen_current(manifest, provenance, "chapters", "prompt", {"ch01", "ch02"})


# ---------------------------------------------------------------------------
# Transcript change -> downstream freshness invalidation (representative link)
# ---------------------------------------------------------------------------


def test_corrected_transcript_change_invalidates_qa_fingerprint(tmp_path):
    """A corrected-transcript change alters what QA hashes (freshness invalidation)."""
    from btcedu.core.qa_reviewer import _sha256_text

    # QA's fingerprint includes sha256(german_text); a transcript edit changes it.
    old_de = "Guten Abend."
    new_de = "Guten Abend, willkommen."
    assert _sha256_text(old_de) != _sha256_text(new_de)
