"""Tests for chapter schema validation (Sprint 6)."""

import pytest
from pydantic import ValidationError

from btcedu.models.chapter_schema import (
    Chapter,
    ChapterDocument,
    Narration,
    Overlay,
    OverlayType,
    Transitions,
    TransitionType,
    Visual,
    VisualType,
)


# ---------------------------------------------------------------------------
# Test Pydantic Models
# ---------------------------------------------------------------------------


def test_narration_model_valid():
    """Test valid Narration model."""
    narration = Narration(
        text="Merhaba arkadaşlar, bugün Bitcoin hakkında konuşacağız.",
        word_count=6,
        estimated_duration_seconds=2,
    )
    assert narration.text != ""
    assert narration.word_count == 6
    assert narration.estimated_duration_seconds == 2


def test_narration_model_duration_validation():
    """Test Narration model accepts reasonable duration variance."""
    # 150 words at 150 words/min = 60 seconds
    # Allow 20% variance (48-72 seconds acceptable)
    narration = Narration(
        text=" ".join(["word"] * 150), word_count=150, estimated_duration_seconds=55
    )
    assert narration.estimated_duration_seconds == 55

    narration2 = Narration(
        text=" ".join(["word"] * 150), word_count=150, estimated_duration_seconds=70
    )
    assert narration2.estimated_duration_seconds == 70


def test_visual_model_diagram_requires_prompt():
    """Test Visual model: diagram type requires image_prompt."""
    with pytest.raises(ValidationError) as exc_info:
        Visual(type=VisualType.DIAGRAM, description="Bitcoin flow diagram", image_prompt=None)
    assert "requires image_prompt" in str(exc_info.value)


def test_visual_model_title_card_no_prompt():
    """Test Visual model: title_card type should not have image_prompt."""
    visual = Visual(
        type=VisualType.TITLE_CARD,
        description="Channel logo with episode title",
        image_prompt=None,
    )
    assert visual.type == VisualType.TITLE_CARD
    assert visual.image_prompt is None


def test_visual_model_b_roll_requires_prompt():
    """Test Visual model: b_roll type requires image_prompt."""
    with pytest.raises(ValidationError) as exc_info:
        Visual(type=VisualType.B_ROLL, description="City street at night", image_prompt=None)
    assert "requires image_prompt" in str(exc_info.value)


def test_overlay_model_valid():
    """Test valid Overlay model."""
    overlay = Overlay(
        type=OverlayType.LOWER_THIRD,
        text="Bitcoin Nedir?",
        start_offset_seconds=2.0,
        duration_seconds=5.0,
    )
    assert overlay.type == OverlayType.LOWER_THIRD
    assert overlay.start_offset_seconds == 2.0
    assert overlay.duration_seconds == 5.0


def test_transitions_model_with_aliases():
    """Test Transitions model uses field aliases."""
    transitions = Transitions(
        **{"in": TransitionType.FADE, "out": TransitionType.CUT}  # Use dict unpacking for keywords
    )
    assert transitions.in_transition == TransitionType.FADE
    assert transitions.out_transition == TransitionType.CUT


def test_chapter_model_valid():
    """Test valid Chapter model."""
    chapter = Chapter(
        chapter_id="ch01",
        title="Giriş",
        order=1,
        narration=Narration(
            text="Merhaba arkadaşlar.", word_count=2, estimated_duration_seconds=1
        ),
        visual=Visual(
            type=VisualType.TITLE_CARD,
            description="Channel logo",
            image_prompt=None,
        ),
        overlays=[],
        transitions=Transitions(
            **{"in": TransitionType.FADE, "out": TransitionType.CUT}
        ),
    )
    assert chapter.chapter_id == "ch01"
    assert chapter.order == 1


def test_chapter_document_valid():
    """Test valid ChapterDocument model."""
    doc = ChapterDocument(
        schema_version="1.0",
        episode_id="abc123",
        title="Bitcoin Nedir?",
        total_chapters=2,
        estimated_duration_seconds=120,
        chapters=[
            Chapter(
                chapter_id="ch01",
                title="Intro",
                order=1,
                narration=Narration(
                    text="Intro text.", word_count=2, estimated_duration_seconds=60
                ),
                visual=Visual(
                    type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                ),
                overlays=[],
                transitions=Transitions(
                    **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                ),
            ),
            Chapter(
                chapter_id="ch02",
                title="Content",
                order=2,
                narration=Narration(
                    text="Content text.", word_count=2, estimated_duration_seconds=60
                ),
                visual=Visual(
                    type=VisualType.DIAGRAM,
                    description="Bitcoin diagram",
                    image_prompt="Clean diagram of Bitcoin",
                ),
                overlays=[],
                transitions=Transitions(
                    **{"in": TransitionType.CUT, "out": TransitionType.FADE}
                ),
            ),
        ],
    )
    assert doc.total_chapters == 2
    assert len(doc.chapters) == 2


def test_chapter_document_total_chapters_mismatch():
    """Test ChapterDocument validation: total_chapters must match array length."""
    with pytest.raises(ValidationError) as exc_info:
        ChapterDocument(
            schema_version="1.0",
            episode_id="abc123",
            title="Test",
            total_chapters=3,  # Wrong count
            estimated_duration_seconds=60,
            chapters=[
                Chapter(
                    chapter_id="ch01",
                    title="Intro",
                    order=1,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                    ),
                ),
            ],
        )
    assert "total_chapters" in str(exc_info.value)


def test_chapter_document_duplicate_chapter_ids():
    """Test ChapterDocument validation: chapter_id must be unique."""
    with pytest.raises(ValidationError) as exc_info:
        ChapterDocument(
            schema_version="1.0",
            episode_id="abc123",
            title="Test",
            total_chapters=2,
            estimated_duration_seconds=120,
            chapters=[
                Chapter(
                    chapter_id="ch01",  # Duplicate
                    title="Intro",
                    order=1,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                    ),
                ),
                Chapter(
                    chapter_id="ch01",  # Duplicate
                    title="Content",
                    order=2,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.CUT, "out": TransitionType.FADE}
                    ),
                ),
            ],
        )
    assert "Duplicate chapter_id" in str(exc_info.value)


def test_chapter_document_non_sequential_order():
    """Test ChapterDocument validation: order must be sequential."""
    with pytest.raises(ValidationError) as exc_info:
        ChapterDocument(
            schema_version="1.0",
            episode_id="abc123",
            title="Test",
            total_chapters=2,
            estimated_duration_seconds=120,
            chapters=[
                Chapter(
                    chapter_id="ch01",
                    title="Intro",
                    order=1,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                    ),
                ),
                Chapter(
                    chapter_id="ch02",
                    title="Content",
                    order=3,  # Should be 2
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.CUT, "out": TransitionType.FADE}
                    ),
                ),
            ],
        )
    assert "order must be sequential" in str(exc_info.value)


def test_chapter_document_duration_mismatch():
    """Test ChapterDocument validation: estimated_duration_seconds must match sum."""
    with pytest.raises(ValidationError) as exc_info:
        ChapterDocument(
            schema_version="1.0",
            episode_id="abc123",
            title="Test",
            total_chapters=2,
            estimated_duration_seconds=200,  # Wrong total (should be 120)
            chapters=[
                Chapter(
                    chapter_id="ch01",
                    title="Intro",
                    order=1,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                    ),
                ),
                Chapter(
                    chapter_id="ch02",
                    title="Content",
                    order=2,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.CUT, "out": TransitionType.FADE}
                    ),
                ),
            ],
        )
    assert "estimated_duration_seconds" in str(exc_info.value)


def test_chapter_document_schema_version_pattern():
    """Test ChapterDocument schema_version must match pattern."""
    with pytest.raises(ValidationError) as exc_info:
        ChapterDocument(
            schema_version="v1",  # Invalid format
            episode_id="abc123",
            title="Test",
            total_chapters=1,
            estimated_duration_seconds=60,
            chapters=[
                Chapter(
                    chapter_id="ch01",
                    title="Intro",
                    order=1,
                    narration=Narration(
                        text="Text.", word_count=1, estimated_duration_seconds=60
                    ),
                    visual=Visual(
                        type=VisualType.TITLE_CARD, description="Title", image_prompt=None
                    ),
                    overlays=[],
                    transitions=Transitions(
                        **{"in": TransitionType.FADE, "out": TransitionType.CUT}
                    ),
                ),
            ],
        )
    assert "schema_version" in str(exc_info.value)
