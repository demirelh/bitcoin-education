from btcedu.core.translation_glossary import fix_translation_glossary


class TestFixTranslationGlossary:
    def test_tankstasyon_dative_replaced(self):
        text = "İnsanlar bu çatışmayı her gün tankstasyona gittiklerinde hissediyor."
        out = fix_translation_glossary(text)
        assert "tankstasyon" not in out.lower()
        assert "benzin istasyonuna" in out

    def test_tankstasyon_locative_replaced(self):
        out = fix_translation_glossary("Fiyatlar tankstasyonda arttı.")
        assert "benzin istasyonunda" in out
        assert "tankstasyon" not in out.lower()

    def test_tankstasyon_bare_replaced(self):
        out = fix_translation_glossary("Yakınlarda bir tankstasyon var.")
        assert "benzin istasyonu" in out
        assert "tankstasyon" not in out.lower()

    def test_case_insensitive(self):
        out = fix_translation_glossary("Tankstasyona gitti.")
        assert "benzin istasyonuna" in out

    def test_clean_text_unchanged(self):
        text = "İnsanlar benzin istasyonuna gitti ve fiyatları gördü."
        assert fix_translation_glossary(text) == text

    def test_empty(self):
        assert fix_translation_glossary("") == ""
