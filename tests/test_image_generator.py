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
            "chapter_id": f"ch{i+1:02d}",
            "title": f"Chapter {i+1}",
            "order": i + 1,
            "narration": {
                "text": f"Narration for chapter {i+1}",
                "word_count": 4,
                "estimated_duration_seconds": 30,
            },
            "visual": {
                "type": visual_type,
                "description": f"Visual description for chapter {i+1}",
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

        manifest_path.write_text(json.dumps({
            "episode_id": "test_ep",
            "images": [],
        }))
        provenance_path.write_text(json.dumps({
            "input_content_hash": chapters_hash,
            "prompt_hash": prompt_hash,
        }))
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
        provenance.write_text(json.dumps({
            "input_content_hash": "h",
            "prompt_hash": "h",
        }))

        # Create manifest that references a missing image
        ep_dir = tmp_path / "images"
        ep_dir.mkdir(parents=True)
        manifest = tmp_path / "images" / "manifest.json"
        manifest.write_text(json.dumps({
            "images": [{
                "chapter_id": "ch01",
                "file_path": "images/ch01_nonexistent.png",
                "generation_method": "dalle3",
            }],
        }))

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

    def test_rejects_v1_episode(self, db_session):
        ep = Episode(
            episode_id="ep_v1",
            source="youtube_rss",
            title="V1 Episode",
            url="https://youtube.com/watch?v=v1",
            status=EpisodeStatus.CHAPTERIZED,
            pipeline_version=1,
        )
        db_session.add(ep)
        db_session.commit()

        from btcedu.config import Settings
        from btcedu.core.image_generator import generate_images

        settings = Settings(anthropic_api_key="test")
        with pytest.raises(ValueError, match="v1 pipeline"):
            generate_images(db_session, "ep_v1", settings)

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
