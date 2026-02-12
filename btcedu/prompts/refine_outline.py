"""Outline refinement prompt template — improves v1 outline using QA feedback."""


def build_user_prompt(
    episode_title: str, episode_id: str, outline_text: str, qa_text: str
) -> str:
    """Build user prompt for outline refinement.

    Args:
        episode_title: Original German episode title.
        episode_id: Episode identifier for citation format.
        outline_text: Previously generated outline v1 (Markdown).
        qa_text: QA/fact-check report (JSON).

    Returns:
        User prompt string.
    """
    return f"""\
## GOREV: Video Taslagini Iyilestir (Outline v2)

Asagidaki mevcut taslagi (v1) ve kalite kontrol raporunu (QA) inceleyerek, \
iyilestirilmis bir taslik olustur.

### BOLUM: "{episode_title}"

### MEVCUT TASLIK (v1):
{outline_text}

### KALITE KONTROL RAPORU (QA):
{qa_text}

### IYILESTIRME GEREKSINIMLERI:

1. **Giris guclendir**: Izleyiciyi yakalayacak daha guclu bir hook ekle
2. **Mantiksal akis**: Bolumlerin siralamasini kontrol et, gerekirse yeniden duzenle
3. **Eksik bolumler**: Gerekiyorsa ozet/sonuc bolumu veya yasal uyari bolumu ekle
4. **QA geri bildirimi**: QA raporundaki "unsupported" veya "kaynaklarda_yok" \
durumundaki iddialari tasliktan cikar veya guncelle
5. **Bolum yapisi**: Her bolumde net bir konu odagi olmali

### CIKTI FORMATI:

Ayni Markdown yapisi (v1 ile ayni format):
- 6-8 ana bolum
- Her bolum icin: baslik, 3-5 madde isareti ile ozet, ve kaynak alintilari
- Alinti formati: [{episode_id}_C####]
- Turkce yaz, teknik terimlerin DE/EN karsiligini parantez icinde belirt

### HATIRLATMA:
- YALNIZCA kaynaklardaki bilgileri kullan
- QA raporunda "unsupported" olan iddialari KALDIR veya "Kaynaklarda yok" olarak isaretle
- Her madde icin en az bir kaynak belirt [{episode_id}_C####]
- Finansal tavsiye VERME
- Yasal uyariyi sonuca ekle
"""
