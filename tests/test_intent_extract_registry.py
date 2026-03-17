"""H-2: Tests verifying that extract_chapter_intents() registers the intent_extract
prompt template via PromptRegistry.

Kept in a separate file to avoid the autouse mock_extract_intents fixture in
test_stock_ranking.py (which would suppress the real function call).
"""

from unittest.mock import MagicMock, patch

import pytest

# Import PromptVersion at module level so SQLAlchemy registers it with Base.metadata
# before db_engine calls create_all().  Without this import the prompt_versions table
# is absent and PromptRegistry queries fail with "no such table".
from btcedu.models.prompt_version import PromptVersion  # noqa: F401


class TestIntentExtractPromptRegistry:
    """Verify that extract_chapter_intents() registers the intent_extract template
    via PromptRegistry, making it visible in `btcedu prompt list` and in the DB.
    """

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.services.claude_service.call_claude")
    def test_intent_extract_registers_prompt_version(
        self, mock_claude, mock_load_chapters, tmp_path, db_session
    ):
        """extract_chapter_intents() creates a PromptVersion record for 'intent_extract'."""
        from btcedu.core.stock_images import extract_chapter_intents

        settings = MagicMock()
        settings.outputs_dir = str(tmp_path)
        settings.claude_model = "claude-sonnet-4-20250514"
        settings.dry_run = False

        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Bitcoin Madenciliği"
        ch1.visual = MagicMock()
        ch1.visual.type = "b_roll"
        ch1.visual.description = "Mining hardware"
        ch1.narration = MagicMock()
        ch1.narration.text = "Madencilik hakkında."

        doc = MagicMock()
        doc.chapters = [ch1]
        mock_load_chapters.return_value = doc

        mock_claude.return_value = MagicMock(
            text=(
                '{"chapters": {"ch01": {"intents": [], "allowed_motifs": [], '
                '"disallowed_motifs": [], "literal_traps": [], "search_hints": []}}}'
            ),
            cost_usd=0.003,
            input_tokens=100,
            output_tokens=50,
        )

        extract_chapter_intents(db_session, "ep001", settings, force=True)

        # PromptVersion for 'intent_extract' must now exist in DB
        pv = (
            db_session.query(PromptVersion)
            .filter(PromptVersion.name == "intent_extract")
            .first()
        )
        assert pv is not None
        assert pv.name == "intent_extract"
        assert pv.is_default is True

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.services.claude_service.call_claude")
    def test_intent_extract_returns_cost(
        self, mock_claude, mock_load_chapters, tmp_path, db_session
    ):
        """extract_chapter_intents() captures and returns LLM cost in IntentResult."""
        from btcedu.core.stock_images import extract_chapter_intents

        settings = MagicMock()
        settings.outputs_dir = str(tmp_path)
        settings.claude_model = "claude-sonnet-4-20250514"
        settings.dry_run = False

        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Enflasyon"
        ch1.visual = MagicMock()
        ch1.visual.type = "diagram"
        ch1.visual.description = "Inflation chart"
        ch1.narration = MagicMock()
        ch1.narration.text = "Enflasyon hakkında."

        doc = MagicMock()
        doc.chapters = [ch1]
        mock_load_chapters.return_value = doc

        expected_cost = 0.0075
        mock_claude.return_value = MagicMock(
            text=(
                '{"chapters": {"ch01": {"intents": [], "allowed_motifs": [], '
                '"disallowed_motifs": [], "literal_traps": [], "search_hints": []}}}'
            ),
            cost_usd=expected_cost,
            input_tokens=150,
            output_tokens=80,
        )

        result = extract_chapter_intents(db_session, "ep001", settings, force=True)

        assert result.cost_usd == pytest.approx(expected_cost)
