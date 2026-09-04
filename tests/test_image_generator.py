"""Tests for Sprint 7: IMAGE_GEN stage implementation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.image_generator import (
    ImageEntry,
    ImageGenResult,
    _compute_chapters_content_hash,
    _is_image_gen_current,
    _mark_downstream_stale,
    _needs_generation,
    _slugify_filename_part,
    _split_prompt,
)
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.services.image_gen_service import (
    DALLE3_COST_STANDARD_1024,
    DALLE3_COST_STANDARD_1792,
    DallE3ImageService,
    ImageGenRequest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_chapters_json(
    episode_id="test_ep",
    visual_type="diagram",
    num_chapters=1,
    image_prompt="A Bitcoin diagram",
):
    """Helper to build a valid chapters.json dict."""
    chapters = []
    for i in range(num_chapters):
        ch = {
            "chapter_id": f"ch{i + 1:02d}",
            "title": f"Chapter {i + 1}",
            "order": i + 1,
            "narration": {
                "text": f"Narration for chapter {i + 1}",
                "word_count": 4,
                "estimated_duration_seconds": 30,
            },
            "visual": {
                "type": visual_type,
                "description": f"Visual description for chapter {i + 1}",
                "image_prompt": image_prompt if visual_type in ("diagram", "b_roll") else None,
            },
            "overlays": [],
            "transitions": {"in": "fade", "out": "fade"},
            "notes": "",
        }
        chapters.append(ch)

    total_duration = 30 * num_chapters
    return {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "title": "Test Episode",
        "total_chapters": num_chapters,
        "estimated_duration_seconds": total_duration,
        "chapters": chapters,
    }


# ---------------------------------------------------------------------------
# _needs_generation
# ---------------------------------------------------------------------------


class TestNeedsGeneration:
    def test_diagram_needs_generation(self):
        assert _needs_generation("diagram") is True


class TestSlugifyFilenamePart:
    def test_turkish_chars_transliterated(self):
        # ü→u, ı→i, ğ→g, ş→s must be preserved (not stripped)
        assert (
            _slugify_filename_part("selamlama ve gündem tanıtımı") == "selamlama_ve_gundem_tanitimi"
        )

    def test_apostrophe_and_comma_removed(self):
        assert _slugify_filename_part("rusya'nın ukrayna'ya") == "rusya_nin_ukrayna_ya"

    def test_result_survives_secure_filename(self):
        from werkzeug.utils import secure_filename

        for title in [
            "İspanya yarı finalde, DFB Klopp",
            "BAP 50. yıl dönümünü kutluyor",
            "hava tahmini",
            "srebrenitsa katliamı'nın anılm",
        ]:
            fname = f"ch01_{_slugify_filename_part(title)}.png"
            # secure_filename must not alter an already-safe filename
            assert secure_filename(fname) == fname

    def test_max_len_enforced(self):
        result = _slugify_filename_part("a" * 100)
        assert len(result) <= 30

    def test_empty_falls_back(self):
        assert _slugify_filename_part("") == "chapter"
        assert _slugify_filename_part("!!!") == "chapter"


class TestNeedsGenerationRest:
    def test_b_roll_needs_generation(self):
        assert _needs_generation("b_roll") is True

    def test_screen_share_needs_generation(self):
        assert _needs_generation("screen_share") is True

    def test_title_card_no_generation(self):
        assert _needs_generation("title_card") is False

    def test_talking_head_no_generation(self):
        assert _needs_generation("talking_head") is False

    def test_unknown_type_no_generation(self):
        assert _needs_generation("unknown") is False


# ---------------------------------------------------------------------------
# _route_provider_for_chapter (per-chapter smart routing)
# ---------------------------------------------------------------------------


class TestRouteProviderForChapter:
    def _chapter(self, visual_type):
        doc = ChapterDocument(**_make_chapters_json(visual_type=visual_type))
        return doc.chapters[0]

    def test_b_roll_routes_to_flux(self):
        from btcedu.core.image_generator import _route_provider_for_chapter

        assert _route_provider_for_chapter(self._chapter("b_roll")) == "flux"

    def test_title_card_routes_to_ideogram(self):
        from btcedu.core.image_generator import _route_provider_for_chapter

        assert _route_provider_for_chapter(self._chapter("title_card")) == "ideogram"

    def test_diagram_routes_to_ideogram(self):
        from btcedu.core.image_generator import _route_provider_for_chapter

        assert _route_provider_for_chapter(self._chapter("diagram")) == "ideogram"

    def test_stock_routes_to_dalle3(self):
        # "stock" isn't a valid ChapterDocument enum, but the router must still
        # map it via the raw-string path used by non-schema callers.
        from types import SimpleNamespace

        from btcedu.core.image_generator import _route_provider_for_chapter

        chapter = SimpleNamespace(visual=SimpleNamespace(type="stock"), overlays=[])
        assert _route_provider_for_chapter(chapter) == "dalle3"

    def test_talking_head_defaults_to_flux(self):
        from btcedu.core.image_generator import _route_provider_for_chapter

        assert _route_provider_for_chapter(self._chapter("talking_head")) == "flux"

    def test_unknown_defaults_to_flux(self):
        from btcedu.core.image_generator import _route_provider_for_chapter

        assert _route_provider_for_chapter(self._chapter("screen_share")) == "flux"


# ---------------------------------------------------------------------------
# _split_prompt
# ---------------------------------------------------------------------------


class TestSplitPrompt:
    def test_splits_at_input_marker(self):
        template = """System instructions here.

Some more system stuff.

# Input

User message template here with {{ variable }}.

# Output

Expected output format."""

        system, user = _split_prompt(template)
        assert "System instructions here" in system
        assert "# Input" not in system
        assert "User message template" in user
        assert "{{ variable }}" in user
        assert "# Output" in user

    def test_no_marker_returns_all_as_system(self):
        template = "Just system prompt, no user part."
        system, user = _split_prompt(template)
        assert system == template.strip()
        assert user == ""

    def test_empty_template(self):
        system, user = _split_prompt("")
        assert system == ""
        assert user == ""


# ---------------------------------------------------------------------------
# _compute_chapters_content_hash
# ---------------------------------------------------------------------------


class TestComputeChaptersContentHash:
    def test_deterministic_hash(self):
        doc = ChapterDocument(**_make_chapters_json())
        hash1 = _compute_chapters_content_hash(doc)
        hash2 = _compute_chapters_content_hash(doc)
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256

    def test_different_visual_different_hash(self):
        data1 = _make_chapters_json()
        data2 = _make_chapters_json()
        data2["chapters"][0]["visual"]["description"] = "Different description"

        hash1 = _compute_chapters_content_hash(ChapterDocument(**data1))
        hash2 = _compute_chapters_content_hash(ChapterDocument(**data2))
        assert hash1 != hash2

    def test_narration_change_does_not_change_hash(self):
        """Only visual fields should affect hash, not narration."""
        data1 = _make_chapters_json()
        data2 = _make_chapters_json()
        data2["chapters"][0]["narration"]["text"] = "Completely different narration"

        hash1 = _compute_chapters_content_hash(ChapterDocument(**data1))
        hash2 = _compute_chapters_content_hash(ChapterDocument(**data2))
        assert hash1 == hash2


# ---------------------------------------------------------------------------
# _is_image_gen_current (idempotency)
# ---------------------------------------------------------------------------


class TestIsImageGenCurrent:
    def _setup_provenance(self, tmp_path, chapters_hash, prompt_hash):
        """Create manifest and provenance files."""
        manifest_path = tmp_path / "manifest.json"
        provenance_path = tmp_path / "provenance.json"

        manifest_path.write_text(
            json.dumps(
                {
                    "episode_id": "test_ep",
                    "images": [],
                }
            )
        )
        provenance_path.write_text(
            json.dumps(
                {
                    "input_content_hash": chapters_hash,
                    "prompt_hash": prompt_hash,
                }
            )
        )
        return manifest_path, provenance_path

    def test_returns_true_when_current(self, tmp_path):
        manifest, provenance = self._setup_provenance(tmp_path, "hash_a", "hash_b")
        assert _is_image_gen_current(manifest, provenance, "hash_a", "hash_b") is True

    def test_returns_false_when_no_manifest(self, tmp_path):
        provenance_path = tmp_path / "provenance.json"
        provenance_path.write_text("{}")
        manifest_path = tmp_path / "no_manifest.json"
        assert _is_image_gen_current(manifest_path, provenance_path, "h", "h") is False

    def test_returns_false_when_no_provenance(self, tmp_path):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{}")
        provenance_path = tmp_path / "no_provenance.json"
        assert _is_image_gen_current(manifest_path, provenance_path, "h", "h") is False

    def test_returns_false_when_chapters_changed(self, tmp_path):
        manifest, provenance = self._setup_provenance(tmp_path, "old_hash", "prompt_hash")
        assert _is_image_gen_current(manifest, provenance, "new_hash", "prompt_hash") is False

    def test_returns_false_when_prompt_changed(self, tmp_path):
        manifest, provenance = self._setup_provenance(tmp_path, "ch_hash", "old_prompt")
        assert _is_image_gen_current(manifest, provenance, "ch_hash", "new_prompt") is False

    def test_returns_false_when_stale_marker(self, tmp_path):
        manifest, provenance = self._setup_provenance(tmp_path, "h", "h")
        stale = manifest.with_suffix(".json.stale")
        stale.write_text("{}")
        assert _is_image_gen_current(manifest, provenance, "h", "h") is False

    def test_returns_false_when_image_missing(self, tmp_path):
        """Manifest references an image that doesn't exist on disk."""
        provenance = tmp_path / "provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "input_content_hash": "h",
                    "prompt_hash": "h",
                }
            )
        )

        # Create manifest that references a missing image
        ep_dir = tmp_path / "images"
        ep_dir.mkdir(parents=True)
        manifest = tmp_path / "images" / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "images": [
                        {
                            "chapter_id": "ch01",
                            "file_path": "images/ch01_nonexistent.png",
                            "generation_method": "dalle3",
                        }
                    ],
                }
            )
        )

        assert _is_image_gen_current(manifest, provenance, "h", "h") is False


# ---------------------------------------------------------------------------
# _mark_downstream_stale
# ---------------------------------------------------------------------------


class TestMarkDownstreamStale:
    def test_marks_render_stale(self, tmp_path):
        ep_id = "ep_stale"
        render_dir = tmp_path / ep_id / "render"
        render_dir.mkdir(parents=True)
        draft = render_dir / "draft.mp4"
        draft.write_bytes(b"fake video")

        _mark_downstream_stale(ep_id, tmp_path)

        stale_marker = render_dir / "draft.mp4.stale"
        assert stale_marker.exists()
        data = json.loads(stale_marker.read_text())
        assert data["invalidated_by"] == "imagegen"

    def test_no_stale_when_no_render(self, tmp_path):
        """No-op if draft.mp4 doesn't exist."""
        ep_id = "ep_no_render"
        _mark_downstream_stale(ep_id, tmp_path)
        render_dir = tmp_path / ep_id / "render"
        assert not render_dir.exists()


# ---------------------------------------------------------------------------
# DallE3ImageService
# ---------------------------------------------------------------------------


class TestDallE3Service:
    def test_cost_standard_1024(self):
        service = DallE3ImageService(api_key="test_key")
        assert service._compute_cost("1024x1024", "standard") == DALLE3_COST_STANDARD_1024

    def test_cost_standard_1792(self):
        service = DallE3ImageService(api_key="test_key")
        assert service._compute_cost("1792x1024", "standard") == DALLE3_COST_STANDARD_1792

    def test_hd_costs_more(self):
        service = DallE3ImageService(api_key="test_key")
        assert service._compute_cost("1024x1024", "hd") > DALLE3_COST_STANDARD_1024
        assert service._compute_cost("1792x1024", "hd") > DALLE3_COST_STANDARD_1792

    @patch("openai.OpenAI")
    def test_generate_image_mock(self, mock_openai_class):
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "data": [
                {
                    "url": "https://example.com/generated_image.png",
                    "revised_prompt": "A professional diagram showing...",
                }
            ]
        }
        mock_client.images.generate.return_value = mock_response

        service = DallE3ImageService(api_key="test_key")
        request = ImageGenRequest(
            prompt="Generate a Bitcoin diagram",
            model="dall-e-3",
            size="1792x1024",
            quality="standard",
        )

        response = service.generate_image(request)
        assert response.image_url == "https://example.com/generated_image.png"
        assert response.revised_prompt == "A professional diagram showing..."
        assert response.cost_usd == DALLE3_COST_STANDARD_1792
        assert response.model == "dall-e-3"

        mock_client.images.generate.assert_called_once()
        call_args = mock_client.images.generate.call_args[1]
        assert call_args["model"] == "dall-e-3"
        assert call_args["prompt"] == "Generate a Bitcoin diagram"


# ---------------------------------------------------------------------------
# ImageGenRequest
# ---------------------------------------------------------------------------


class TestImageGenRequest:
    def test_defaults(self):
        request = ImageGenRequest(prompt="Test prompt")
        assert request.prompt == "Test prompt"
        assert request.model == "dall-e-3"
        assert request.size == "1792x1024"
        assert request.quality == "standard"
        assert request.style_prefix == ""


# ---------------------------------------------------------------------------
# ImageEntry dataclass
# ---------------------------------------------------------------------------


class TestImageEntry:
    def test_creates_entry(self):
        entry = ImageEntry(
            chapter_id="ch01",
            chapter_title="Intro",
            visual_type="diagram",
            file_path="images/ch01.png",
            prompt="A Bitcoin diagram",
            generation_method="dalle3",
            model="dall-e-3",
            size="1792x1024",
            mime_type="image/png",
            size_bytes=12345,
            metadata={"cost_usd": 0.08},
        )
        assert entry.chapter_id == "ch01"
        assert entry.generation_method == "dalle3"
        assert entry.metadata["cost_usd"] == 0.08


# ---------------------------------------------------------------------------
# ImageGenResult dataclass
# ---------------------------------------------------------------------------


class TestImageGenResult:
    def test_defaults(self, tmp_path):
        result = ImageGenResult(
            episode_id="ep01",
            images_path=tmp_path / "images",
            manifest_path=tmp_path / "manifest.json",
            provenance_path=tmp_path / "provenance.json",
        )
        assert result.image_count == 0
        assert result.generated_count == 0
        assert result.template_count == 0
        assert result.failed_count == 0
        assert result.cost_usd == 0.0
        assert result.skipped is False

    def test_skipped(self, tmp_path):
        result = ImageGenResult(
            episode_id="ep01",
            images_path=tmp_path / "images",
            manifest_path=tmp_path / "manifest.json",
            provenance_path=tmp_path / "provenance.json",
            skipped=True,
            image_count=3,
        )
        assert result.skipped is True
        assert result.image_count == 3


# ---------------------------------------------------------------------------
# generate_images integration tests
# ---------------------------------------------------------------------------


class TestGenerateImagesValidation:
    """Tests for generate_images() precondition checks."""

    def test_rejects_missing_episode(self, db_session):
        from btcedu.config import Settings

        settings = Settings(anthropic_api_key="test")
        with pytest.raises(ValueError, match="Episode not found"):
            from btcedu.core.image_generator import generate_images

            generate_images(db_session, "nonexistent", settings)

    def test_rejects_wrong_status(self, db_session):
        ep = Episode(
            episode_id="ep_wrong",
            source="youtube_rss",
            title="Wrong Status",
            url="https://youtube.com/watch?v=w",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        from btcedu.config import Settings
        from btcedu.core.image_generator import generate_images

        settings = Settings(anthropic_api_key="test")
        with pytest.raises(ValueError, match="expected 'chapterized'"):
            generate_images(db_session, "ep_wrong", settings)

    def test_accepts_frames_extracted_status(self, db_session, tmp_path):
        """frameextract runs right before imagegen in v2 and sets FRAMES_EXTRACTED;
        generate_images must accept it as a valid precondition (not raise on status).
        """
        ep = Episode(
            episode_id="ep_frames",
            source="youtube_rss",
            title="Frames Extracted",
            url="https://youtube.com/watch?v=f",
            status=EpisodeStatus.FRAMES_EXTRACTED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        from btcedu.config import Settings
        from btcedu.core.image_generator import generate_images

        settings = Settings(anthropic_api_key="test")
        # Should fail later (missing chapters.json), NOT on the status precondition.
        with pytest.raises(Exception) as exc_info:
            generate_images(db_session, "ep_frames", settings)
        assert "expected 'chapterized'" not in str(exc_info.value)

    def test_rejects_missing_chapters_json(self, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_no_ch",
            source="youtube_rss",
            title="No Chapters",
            url="https://youtube.com/watch?v=nc",
            status=EpisodeStatus.CHAPTERIZED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        from btcedu.config import Settings
        from btcedu.core.image_generator import generate_images

        settings = Settings(
            anthropic_api_key="test",
            outputs_dir=str(tmp_path / "outputs"),
        )
        with pytest.raises(FileNotFoundError, match="Chapters file not found"):
            generate_images(db_session, "ep_no_ch", settings)


@pytest.mark.parametrize("complete_manifest", [True, False])
def test_targeted_regeneration_keeps_complete_manifest(db_session, tmp_path, complete_manifest):
    """Targeted rerender preserves complete manifests and rebuilds incomplete ones."""
    from btcedu.config import Settings
    from btcedu.core.image_generator import generate_images

    episode_id = "ep_targeted"
    ep = Episode(
        episode_id=episode_id,
        source="youtube_rss",
        title="Targeted",
        url="https://youtube.com/watch?v=targeted",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()

    settings = Settings(
        anthropic_api_key="test",
        outputs_dir=str(tmp_path / "outputs"),
    )
    episode_dir = Path(settings.outputs_dir) / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "chapters.json").write_text(
        json.dumps(
            _make_chapters_json(
                episode_id=episode_id,
                visual_type="title_card",
                num_chapters=2,
            )
        )
    )
    images_dir = episode_dir / "images"
    images_dir.mkdir()
    untouched_path = images_dir / "ch02.png"
    untouched_path.write_bytes(b"existing")
    manifest_entries = [
        {
            "chapter_id": "ch01",
            "chapter_title": "Chapter 1",
            "visual_type": "title_card",
            "file_path": "images/ch01.png",
            "prompt": None,
            "generation_method": "template",
            "model": None,
            "size": "1792x1024",
            "mime_type": "image/png",
            "size_bytes": 1,
            "metadata": {},
        }
    ]
    if complete_manifest:
        manifest_entries.append(
            {
                "chapter_id": "ch02",
                "chapter_title": "Chapter 2",
                "visual_type": "title_card",
                "file_path": "images/ch02.png",
                "prompt": None,
                "generation_method": "template",
                "model": None,
                "size": "1792x1024",
                "mime_type": "image/png",
                "size_bytes": untouched_path.stat().st_size,
                "metadata": {"preserved": True},
            }
        )
    (images_dir / "manifest.json").write_text(
        json.dumps(
            {
                "episode_id": episode_id,
                "schema_version": "1.0",
                "images": manifest_entries,
            }
        )
    )

    with (
        patch("btcedu.services.image_provider_factory.get_image_service", return_value=MagicMock()),
        patch("btcedu.core.image_generator._create_media_asset_record"),
    ):
        result = generate_images(
            db_session,
            episode_id,
            settings,
            force=True,
            chapter_id="ch01",
        )

    manifest = json.loads(result.manifest_path.read_text())
    assert [entry["chapter_id"] for entry in manifest["images"]] == ["ch01", "ch02"]
    if complete_manifest:
        assert manifest["images"][1]["metadata"] == {"preserved": True}
        assert untouched_path.read_bytes() == b"existing"
    else:
        assert manifest["images"][1]["metadata"] != {"preserved": True}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_a_lower_third_does_not_route_the_picture_to_the_text_provider():
    """Overlays are drawn by the renderer, so they must not decide the provider."""
    from types import SimpleNamespace

    from btcedu.core.image_generator import _route_provider_for_chapter

    chapter = SimpleNamespace(
        visual=SimpleNamespace(type="b_roll"),
        overlays=[SimpleNamespace(text="EMEKLİLİK PAKETİ TARTIŞMASI")],
    )
    assert _route_provider_for_chapter(chapter) == "flux"


def _prepare_generative_episode(db_session, tmp_path, episode_id):
    """An episode ready for imagegen on the generative (fallback-capable) profile."""
    from btcedu.config import Settings

    ep = Episode(
        episode_id=episode_id,
        source="local_recorder",
        title="Generative",
        url="/tmp/recording.mp4",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(ep)
    db_session.commit()

    settings = Settings(anthropic_api_key="test", outputs_dir=str(tmp_path / "outputs"))
    episode_dir = Path(settings.outputs_dir) / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "chapters.json").write_text(
        json.dumps(
            _make_chapters_json(
                episode_id=episode_id,
                visual_type="b_roll",
                num_chapters=1,
                image_prompt="A quiet street at dusk",
            )
        )
    )
    return settings


def test_unresolved_chapter_image_fails_the_stage(db_session, tmp_path):
    """A dangling manifest entry must not be reported as a successful stage.

    The renderer only notices the missing file after tts/anchorgen, and a retry
    then resumes past imagegen and can never repair it.
    """
    from btcedu.core.image_generator import generate_images
    from btcedu.services.errors import PipelineError

    episode_id = "ep_unresolved"
    settings = _prepare_generative_episode(db_session, tmp_path, episode_id)

    with (
        patch("btcedu.services.image_provider_factory.get_image_service", return_value=MagicMock()),
        patch("btcedu.core.image_generator._create_media_asset_record"),
        patch(
            "btcedu.core.image_generator._generate_single_image",
            side_effect=RuntimeError("500 Server Error for url: https://cdn.example/x.jpg"),
        ),
        pytest.raises(PipelineError) as excinfo,
    ):
        generate_images(db_session, episode_id, settings, force=True)

    assert "ch01" in str(excinfo.value)

    # The manifest is still written so a rerun regenerates only this chapter.
    manifest = json.loads(
        (Path(settings.outputs_dir) / episode_id / "images" / "manifest.json").read_text()
    )
    assert manifest["images"][0]["generation_method"] == "failed"


def test_fallback_provider_rescues_a_failed_chapter_image(db_session, tmp_path):
    """The profile's fallback_provider must actually be used, not just configured."""
    from btcedu.core.image_generator import generate_images

    episode_id = "ep_fallback"
    settings = _prepare_generative_episode(db_session, tmp_path, episode_id)
    images_dir = Path(settings.outputs_dir) / episode_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "ch01_rescued.png").write_bytes(b"rescued")

    rescued = ImageEntry(
        chapter_id="ch01",
        chapter_title="Chapter 1",
        visual_type="b_roll",
        file_path="images/ch01_rescued.png",
        prompt="A quiet street at dusk",
        generation_method="ideogram",
        model="ideogram-v2",
        size="1792x1024",
        mime_type="image/png",
        size_bytes=7,
        metadata={"cost_usd": 0.08},
    )

    with (
        patch("btcedu.services.image_provider_factory.get_image_service", return_value=MagicMock()),
        patch("btcedu.core.image_generator._create_media_asset_record"),
        patch(
            "btcedu.core.image_generator._generate_single_image",
            side_effect=[RuntimeError("primary provider is down"), rescued],
        ) as mock_generate,
    ):
        result = generate_images(db_session, episode_id, settings, force=True)

    assert mock_generate.call_count == 2
    assert result.failed_count == 0
    manifest = json.loads((images_dir / "manifest.json").read_text())
    assert manifest["images"][0]["generation_method"] == "ideogram"
