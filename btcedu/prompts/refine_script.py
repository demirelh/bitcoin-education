"""Script refinement prompt template — improves v1 script using QA feedback and v2 outline."""


def build_user_prompt(
    episode_title: str,
    episode_id: str,
    script_text: str,
    outline_v2_text: str,
    qa_text: str,
) -> str:
    """Build user prompt for script refinement.

    Args:
        episode_title: Original German episode title.
        episode_id: Episode identifier for citation format.
        script_text: Previously generated script v1 (Markdown).
        outline_v2_text: Refined outline v2 (Markdown).
        qa_text: QA/fact-check report (JSON).

    Returns:
        User prompt string.
    """
    return f"""\
## GOREV: Video Senaryosunu Iyilestir (Script v2)

Asagidaki mevcut senaryoyu (v1), iyilestirilmis taslagi (v2) ve kalite kontrol \
raporunu (QA) kullanarak, iyilestirilmis bir senaryo yaz.

### BOLUM: "{episode_title}"

### MEVCUT SENARYO (v1):
{script_text}

### IYILESTIRILMIS TASLIK (v2):
{outline_v2_text}

### KALITE KONTROL RAPORU (QA):
{qa_text}

### IYILESTIRME GEREKSINIMLERI:

1. **Yapiyi guncelle**: Iyilestirilmis taslaga (v2) uy, bolum siralamasini takip et
2. **Desteklenmeyen iddialari kaldir**: QA raporundaki "unsupported" veya \
"kaynaklarda_yok" durumundaki iddialari senaryodan cikar
3. **Kaynak alintilari koru**: Mevcut [{episode_id}_C####] alintilari koru ve dogru kullan
4. **Giris ve sonucu guclendir**: Daha etkili bir acilis ve kapanisla baslat/bitir
5. **Akis ve netlik**: Cumleleri daha dogal ve anlasilir yap
6. **Egitim tonu**: Sohbet tarzinda, ogretici bir dil kullan
7. **Finansal tavsiye YASAK**: Fiyat tahmini, alim/satim tavsiyesi VERME

### CIKTI FORMATI:

Ayni Markdown yapisi (v1 ile ayni format):
- Taslaktaki bolumleri takip et
- Her bolum icin konusmaci metni (dogal, sohbet tarzinda)
- Her onemli iddia icin kaynak alintisi [{episode_id}_C####]
- Turkce yaz, teknik terimlerin DE/EN karsiligini parantez icinde belirt
- ~2000-2500 kelime (12-15 dakikalik video icin uygun)

### HATIRLATMA:
- YALNIZCA kaynaklardaki bilgileri kullan
- QA raporunda dogrulanan iddialari koru, desteklenmeyenleri cikar
- Her bolumdeki her iddia icin kaynak belirt [{episode_id}_C####]
- Kaynakta olmayan bilgi icin "Kaynaklarda yok" yaz
- Dogal Turkce kullan, tercume havasi verme
- Yasal uyariyi sonuna ekle
"""
