"""Basic tests for Sprint 7: IMAGE_GEN stage implementation."""

from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.image_generator import (
    _compute_chapters_content_hash,
    _needs_generation,
    _split_prompt,
)
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.services.image_gen_service import (
    DALLE3_COST_STANDARD_1024,
    DALLE3_COST_STANDARD_1792,
    DallE3ImageService,
    ImageGenRequest,
)


def test_needs_generation():
    """Test visual type filtering for generation."""
    assert _needs_generation("diagram") is True
    assert _needs_generation("b_roll") is True
    assert _needs_generation("screen_share") is True
    assert _needs_generation("title_card") is False
    assert _needs_generation("talking_head") is False


def test_split_prompt():
    """Test prompt template splitting at # Input marker."""
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


def test_split_prompt_no_marker():
    """Test prompt splitting when no # Input marker."""
    template = "Just system prompt, no user part."
    system, user = _split_prompt(template)

    assert system == template.strip()
    assert user == ""


def test_compute_chapters_content_hash():
    """Test chapter content hash computation."""
    # Create minimal chapter document
    chapters_json = {
        "schema_version": "1.0",
        "episode_id": "test_ep",
        "title": "Test Episode",
        "total_chapters": 1,
        "estimated_duration_seconds": 100,
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Chapter 1",
                "order": 1,
                "narration": {
                    "text": "Test narration",
                    "word_count": 2,
                    "estimated_duration_seconds": 100,
                },
                "visual": {
                    "type": "diagram",
                    "description": "Test diagram",
                    "image_prompt": "A test diagram showing Bitcoin",
                },
                "overlays": [],
                "transitions": {"in": "fade", "out": "fade"},
                "notes": "",
            }
        ],
    }

    doc = ChapterDocument(**chapters_json)
    hash1 = _compute_chapters_content_hash(doc)

    # Same content should produce same hash
    doc2 = ChapterDocument(**chapters_json)
    hash2 = _compute_chapters_content_hash(doc2)
    assert hash1 == hash2

    # Different visual description should produce different hash
    chapters_json["chapters"][0]["visual"]["description"] = "Different description"
    doc3 = ChapterDocument(**chapters_json)
    hash3 = _compute_chapters_content_hash(doc3)
    assert hash1 != hash3


def test_dalle3_service_cost_computation():
    """Test cost calculation for different sizes and qualities."""
    service = DallE3ImageService(api_key="test_key")

    # Standard quality costs
    assert service._compute_cost("1024x1024", "standard") == DALLE3_COST_STANDARD_1024
    assert service._compute_cost("1792x1024", "standard") == DALLE3_COST_STANDARD_1792

    # HD quality costs (double standard for same size)
    assert service._compute_cost("1024x1024", "hd") > DALLE3_COST_STANDARD_1024
    assert service._compute_cost("1792x1024", "hd") > DALLE3_COST_STANDARD_1792


@patch("openai.OpenAI")
def test_dalle3_service_generate_image_mock(mock_openai_class):
    """Test image generation with mocked OpenAI API."""
    # Mock the OpenAI client and response
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

    # Create service and generate image
    service = DallE3ImageService(api_key="test_key")
    request = ImageGenRequest(
        prompt="Generate a Bitcoin diagram",
        model="dall-e-3",
        size="1792x1024",
        quality="standard",
    )

    response = service.generate_image(request)

    # Verify response
    assert response.image_url == "https://example.com/generated_image.png"
    assert response.revised_prompt == "A professional diagram showing..."
    assert response.cost_usd == DALLE3_COST_STANDARD_1792
    assert response.model == "dall-e-3"

    # Verify OpenAI API was called correctly
    mock_client.images.generate.assert_called_once()
    call_args = mock_client.images.generate.call_args[1]
    assert call_args["model"] == "dall-e-3"
    assert call_args["prompt"] == "Generate a Bitcoin diagram"
    assert call_args["size"] == "1792x1024"
    assert call_args["quality"] == "standard"


def test_image_gen_request_defaults():
    """Test ImageGenRequest dataclass defaults."""
    request = ImageGenRequest(prompt="Test prompt")

    assert request.prompt == "Test prompt"
    assert request.model == "dall-e-3"
    assert request.size == "1792x1024"
    assert request.quality == "standard"
    assert request.style_prefix == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
