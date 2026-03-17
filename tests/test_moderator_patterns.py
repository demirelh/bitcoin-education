"""Tests for the moderator content cleaning patterns module."""

from btcedu.core.moderator_patterns import (
    BROADCAST_NAMES_TR,
    MODERATOR_NAMES,
    clean_moderator_names,
    has_moderator_content,
)


class TestCleanModeratorNames:
    """Tests for clean_moderator_names() regex cleaning."""

    def test_removes_known_moderator_ben_pattern(self):
        result = clean_moderator_names("Ben Jens Riewa, günün haberleri.")
        assert "Jens Riewa" not in result
        assert "haberleri" in result

    def test_removes_moderator_ile_pattern(self):
        result = clean_moderator_names("Jens Riewa ile günün haberleri")
        assert "Jens Riewa" not in result
        assert "günün haberleri" in result

    def test_removes_moderator_burada_pattern(self):
        result = clean_moderator_names("Susanne Daubner burada, haberlere hoş geldiniz")
        assert "Susanne Daubner" not in result
        assert "haberlere" in result

    def test_removes_standalone_moderator_name(self):
        result = clean_moderator_names("Bu akşam Judith Rakers sunuyor")
        assert "Judith Rakers" not in result

    def test_removes_multiple_moderators(self):
        text = "Susanne Daubner burada. Yarın Jens Riewa ile görüşeceğiz."
        result = clean_moderator_names(text)
        assert "Susanne Daubner" not in result
        assert "Jens Riewa" not in result

    def test_preserves_politician_names(self):
        text = "Başbakan Olaf Scholz bugün açıkladı"
        assert clean_moderator_names(text) == text

    def test_preserves_expert_names(self):
        text = "Ekonomist Lars Feld'e göre enflasyon artıyor"
        assert clean_moderator_names(text) == text

    def test_removes_broadcast_name_tagesschau(self):
        result = clean_moderator_names("tagesschau haberlerine hoş geldiniz")
        assert "tagesschau" not in result

    def test_removes_broadcast_name_tagesthemen(self):
        result = clean_moderator_names("tagesthemen programından merhaba")
        assert "tagesthemen" not in result

    def test_empty_input(self):
        assert clean_moderator_names("") == ""

    def test_none_passthrough(self):
        """None-like empty string returns empty."""
        assert clean_moderator_names("") == ""

    def test_text_without_moderator_unchanged(self):
        text = "Almanya'da ekonomik büyüme yüzde iki olarak gerçekleşti."
        assert clean_moderator_names(text) == text

    def test_normalizes_whitespace(self):
        result = clean_moderator_names("Jens Riewa  ile   haberlere hoş geldiniz")
        assert "  " not in result

    def test_fixes_orphaned_leading_punctuation(self):
        result = clean_moderator_names(", Jens Riewa ile haberler")
        assert not result.startswith(",")

    def test_all_known_moderators_removed(self):
        """Every moderator in the list should be cleaned."""
        for name in MODERATOR_NAMES:
            text = f"Ben {name}, haberler."
            result = clean_moderator_names(text)
            assert name not in result, f"Moderator '{name}' was not removed"

    def test_all_broadcast_names_removed(self):
        """Every broadcast name in the list should be cleaned."""
        for name in BROADCAST_NAMES_TR:
            text = f"{name} haberlerine hoş geldiniz"
            result = clean_moderator_names(text)
            assert name not in result, f"Broadcast name '{name}' was not removed"


class TestHasModeratorContent:
    """Tests for has_moderator_content() detection."""

    def test_detects_guten_abend(self):
        assert has_moderator_content("Guten Abend, meine Damen und Herren")

    def test_detects_willkommen(self):
        assert has_moderator_content("Willkommen zur tagesschau")

    def test_detects_hier_ist_moderator(self):
        assert has_moderator_content("Hier ist Jens Riewa mit der tagesschau")

    def test_detects_moderator_name(self):
        assert has_moderator_content("Susanne Daubner begrüßt Sie")

    def test_detects_das_wars(self):
        assert has_moderator_content("Das war's von der tagesschau")

    def test_detects_ich_wuensche(self):
        assert has_moderator_content("Ich wünsche Ihnen einen schönen Abend")

    def test_no_false_positive_on_news(self):
        assert not has_moderator_content("Der Bundeskanzler hat heute erklärt")

    def test_no_false_positive_on_report(self):
        assert not has_moderator_content("In Berlin haben heute tausende Menschen demonstriert.")

    def test_detects_damen_und_herren(self):
        assert has_moderator_content("Meine Damen und Herren, hier die Nachrichten")

    def test_detects_morgen_begruesst(self):
        assert has_moderator_content("Morgen begrüßt Sie dann Susanne Daubner")
