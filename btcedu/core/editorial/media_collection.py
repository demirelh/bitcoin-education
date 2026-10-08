"""A curated collection of freely licensed pictures, matched to stories by name.

A free-text catalogue search with a whole headline finds nothing usable, so the
newsroom keeps its own short list: politicians and other public figures by
portrait, places and recurring topics by one representative picture. Every entry
pins a Commons file; its licence is still read live and judged by
:func:`btcedu.core.editorial.media.assess_candidate`, so a file whose rights
change on Commons drops out instead of being published on stale metadata.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from btcedu.core.editorial.media import MediaRequirement
from btcedu.models.media_rights import MediaRole
from btcedu.services.commons_service import (
    MediaCandidate,
    MediaCatalogError,
    WikimediaCommonsProvider,
)

#: Width of the Commons thumbnail that is downloaded instead of the original.
THUMB_WIDTH = 1280


@dataclass(frozen=True)
class CollectionEntry:
    key: str
    label: str
    kind: str  # "person", "place" or "topic"
    commons_file: str
    #: Lower-case, Turkish and German. A trailing ``*`` allows suffixes
    #: ("okul*" matches "okullarda"); otherwise the name must end there, which
    #: still admits Turkish case endings after an apostrophe ("Merz'in").
    aliases: tuple[str, ...]
    caption_tr: str = ""
    #: When set, at least one of these must also occur somewhere in the story.
    requires: tuple[str, ...] = ()

    @property
    def role(self) -> MediaRole:
        return MediaRole.PORTRAIT if self.kind == "person" else MediaRole.SYMBOLIC

    @property
    def caption(self) -> str:
        return self.caption_tr or self.label


def _p(key, label, file, *aliases, caption=""):
    return CollectionEntry(key, label, "person", file, tuple(aliases), caption)


def _t(key, label, kind, file, aliases, caption, requires=()):
    return CollectionEntry(key, label, kind, file, tuple(aliases), caption, tuple(requires))


_SPAIN = ("ispanya", "spanien", "madrid")
_FRANCE = ("fransa", "frankreich", "paris")

COLLECTION: tuple[CollectionEntry, ...] = (
    # People. Files are the Wikidata portrait (P18) at curation time.
    _p("trump", "Donald Trump",
       "Official Presidential Portrait of President Donald J. Trump (2025).jpg", "trump"),
    _p("merz", "Friedrich Merz",
       "2024-08-21 Friedrich Merz in Erfurt 2024 STP 3041 by Stepro (3x4 cropped).jpg", "merz"),
    _p("steinmeier", "Frank-Walter Steinmeier",
       "Frank-Walter Steinmeier in 2025 (cropped).jpg", "steinmeier"),
    _p("spahn", "Jens Spahn",
       "2025-05-05 Unterzeichnung des Koalitionsvertrages der 21. Wahlperiode des "
       "Bundestages (Martin Rulsch) 170.jpg", "jens spahn", "spahn"),
    _p("musk", "Elon Musk", "Elon Musk (54816836217) (cropped 2) (b).jpg", "elon musk", "musk"),
    _p("amodei", "Dario Amodei",
       "Dario Amodei at TechCrunch Disrupt 2023 02 (cropped).jpg", "amodei"),
    _p("lula", "Luiz Inácio Lula da Silva",
       "Foto oficial de Luiz Inácio Lula da Silva (estreita).jpg", "lula"),
    _p("sanchez", "Pedro Sánchez",
       "Obisk španskega predsednika vlade v Sloveniji (53658605930) (cropped).jpg",
       "pedro sánchez", "sánchez", "sanchez"),
    _p("pistorius", "Boris Pistorius",
       "2025-05-05 Unterzeichnung des Koalitionsvertrages der 21. Wahlperiode des "
       "Bundestages by Sandro Halank–036.jpg", "pistorius"),
    _p("siegmund", "Ulrich Siegmund",
       "(2026) AfD Ulrich Siegmund 160856370 cropped.jpg", "ulrich siegmund", "siegmund"),
    _p("scholz", "Olaf Scholz", "Olaf Scholz 2024.jpg", "olaf scholz", "scholz"),
    _p("klingbeil", "Lars Klingbeil",
       "Wahlkampf Landtagswahl NRW 2022 - SPD - Roncalliplatz Köln 2022-05-13-4265 (cropped).jpg",
       "klingbeil"),
    _p("weidel", "Alice Weidel", "Alice Weidel at CPAC Hungary.jpg", "weidel"),
    _p("chrupalla", "Tino Chrupalla", "Tino Chrupalla, 2020 (cropped).jpg", "chrupalla"),
    _p("soeder", "Markus Söder",
       "2023-10-08 Wahlabend Bayern by Sandro Halank–052.jpg", "söder", "soeder"),
    _p("putin", "Wladimir Putin", "Putin V 2026 08 10.png", "putin"),
    _p("selenskyj", "Wolodymyr Selenskyj",
       "New Year's Eve from President of Ukraine Volodymyr Zelensky (31-12-2025) (cropped).jpg",
       "selenskyj", "zelenski", "zelensky", "selenski"),
    _p("macron", "Emmanuel Macron", "Emmanuel Macron 2025 (cropped).jpg", "macron"),
    _p("erdogan", "Recep Tayyip Erdoğan", "Recep Tayyip Erdogan in Ukraine.jpg",
       "erdoğan", "erdogan"),
    _p("von_der_leyen", "Ursula von der Leyen", "Ursula von der Leyen 2024.jpg",
       "von der leyen"),
    _p("netanjahu", "Benjamin Netanjahu", "Benjamin Netanyahu, February 2023.jpg",
       "netanjahu", "netanyahu"),
    _p("xi", "Xi Jinping", "Xi Jinping on August 18, 2026 (cropped).jpg",
       "xi jinping", "şi cinping"),
    _p("bas", "Bärbel Bas", "BaerbelBas.JPG", "bärbel bas"),
    _p("wadephul", "Johann Wadephul", "Wadephul, Johann-1249.jpg", "wadephul"),
    _p("dobrindt", "Alexander Dobrindt", "Alexander Dobrindt 2011.JPG", "dobrindt"),
    _p("warken", "Nina Warken", "2020-02-13 Deutscher Bundestag IMG 3091 by Stepro.jpg",
       "nina warken", "warken"),
    _p("reiche", "Katherina Reiche",
       "AC SO Karlspreis-Europa-Forum 2026 13. Mai Katherina Reiche III.jpg", "katherina reiche"),
    _p("meloni", "Giorgia Meloni", "Giorgia Meloni Official 2024 (cropped).jpg", "meloni"),
    _p("baerbock", "Annalena Baerbock",
       "2024-05-27 Event, Konferenz, re-publica STP 5327 by Stepro (cropped).jpg", "baerbock"),
    _p("habeck", "Robert Habeck", "Maischberger - 2018-06-20-6596.jpg", "habeck"),
    _p("wagenknecht", "Sahra Wagenknecht",
       "2014-09-11 - Sahra Wagenknecht MdB - 8301.jpg", "wagenknecht"),
    _p("wuest", "Hendrik Wüst", "Eröffnung ICE-Instandhaltungswerk Köln-Nippes-9251 (cropped).jpg",
       "hendrik wüst"),
    _p("chamenei", "Ali Chamenei", "Ali Khamenei Nowruz message official portrait 1397 02.jpg",
       "chamenei", "khamenei", "hamaney"),
    _p("bin_salman", "Mohammed bin Salman",
       "الصورة الرسمية للأمير محمد بن سلمان بن عبدالعزيز آل سعود (مقصوصة).jpg",
       "bin salman", "bin selman"),
    _p("jair_bolsonaro", "Jair Bolsonaro", "Presidente Jair Messias Bolsonaro.jpg",
       "jair bolsonaro"),
    _p("hubig", "Stefanie Hubig", "2016-11-17 - Stefanie Hubig - 0363.jpg", "hubig"),
    # Places and institutions.
    _t("reichstag", "Reichstagsgebäude", "place", "Reichstagsgebäude von Westen.jpg",
       ("bundestag", "reichstag", "federal meclis"), "Reichstag binası, Berlin"),
    _t("landtag_sachsen_anhalt", "Landtag von Sachsen-Anhalt", "place",
       "Landtagsprojekt Sachsen-Anhalt by-RaBoe 2012 009.jpg",
       ("sachsen-anhalt", "saksonya-anhalt", "magdeburg"),
       "Sachsen-Anhalt eyalet meclisi, Magdeburg"),
    _t("white_house", "Weißes Haus", "place", "WhiteHouseSouthFacade.JPG",
       ("beyaz saray", "weißes haus", "weisses haus", "white house"), "Beyaz Saray, Washington"),
    _t("bnd", "Bundesnachrichtendienst", "place",
       "Zentrale des Bundesnachrichtendienst, Berlin.jpg",
       ("bnd", "bundesnachrichtendienst", "istihbarat*", "geheimdienst*"),
       "Federal İstihbarat Servisi (BND) merkezi, Berlin"),
    _t("emden", "Emden", "place", "Wind Lift I, Emder Hafen WhiteBalanced.jpg",
       ("emden",), "Emden limanı"),
    _t("mainz", "Staatskanzlei Mainz", "place",
       "Mainz, Landtag-Staatskanzlei foto2 2009-08-01 18.50.JPG",
       ("rheinland-pfalz", "renanya-pfalz", "mainz"), "Rheinland-Pfalz eyalet hükümeti, Mainz"),
    _t("oper_koeln", "Oper Köln", "place", "Oper Köln, Frontalansicht mit Offenbachplatz-8419.jpg",
       ("opera*", "oper*", "tiyatro*", "schauspiel*"), "Köln Operası", requires=("köln",)),
    _t("freiburg_stadium", "Europa-Park Stadion", "place",
       "Europa Park Stadion in Freiburg von der Georges-Köhler-Allee gesehen 8.jpg",
       ("sc freiburg", "freiburg"), "SC Freiburg'un stadyumu, Freiburg"),
    _t("riksdag", "Riksdagshuset", "place", "Riksdagshuset i Stockholm.jpg",
       ("isveç", "schweden", "stockholm", "riksdag"), "İsveç parlamentosu, Stockholm"),
    _t("sanaa", "Sanaa", "place", "San'a03 flickr.jpg",
       ("yemen", "jemen", "husi*", "huthi*", "sanaa", "sana"), "Yemen'in başkenti Sana"),
    _t("kyiv", "Kiew", "place", "Kyiv (234807751).jpeg",
       ("ukrayna", "ukraine", "kiev", "kiew", "kyiv"), "Kiev, Ukrayna"),
    _t("tehran", "Teheran", "place", "North of tehran.jpg",
       ("iran", "tahran", "teheran"), "Tahran, İran"),
    _t("moscow", "Moskau", "place", "Saint Basil's Cathedral and the Red Square.jpg",
       ("moskova", "moskau", "kremlin"), "Moskova, Kızıl Meydan"),
    _t("brasilia", "Brasília", "place", "Planalto Central (cropped).jpg",
       ("brezilya", "brasilien", "brasília", "brasilia"), "Brasília, Brezilya"),
    _t("spain_parliament", "Congreso de los Diputados", "place", "Palacio de los Diputados.jpg",
       ("parlamento*", "parlament*", "meclis*"), "İspanya parlamentosu, Madrid", requires=_SPAIN),
    _t("madrid", "Madrid", "place", "Madrid (38624991251).jpg", _SPAIN, "Madrid, İspanya"),
    _t("paris", "Paris", "place",
       "La Tour Eiffel vue de la Tour Saint-Jacques, Paris août 2014 (2).jpg",
       _FRANCE, "Paris, Fransa"),
    _t("hannover_berlin", "Schnellfahrstrecke Hannover–Berlin", "place",
       "ICE 1 Hannover-Berlin Gardelegen.jpg", ("hannover-berlin",),
       "Hannover–Berlin hızlı tren hattı"),
    # Recurring topics.
    _t("spain_housing", "Manifestación por la vivienda", "topic",
       "26S-Manifestación por la vivienda--09.jpg",
       ("konut*", "tahliye*", "wohnung*", "zwangsräumung*", "vivienda"),
       "İspanya'da konut protestosu", requires=_SPAIN),
    _t("france_school", "Lycée Turgot", "topic", "Lycée Turgot, Paris (façade).jpg",
       ("okul*", "öğrenci*", "schule*", "schüler*", "lycée*"), "Paris'te bir lise",
       requires=_FRANCE),
    _t("against_far_right", "Demonstration gegen Rechtsextremismus", "topic",
       "Demonstration gegen Rechtsextremismus in Frankfurt am 20. Januar 2024.png",
       ("aşırı sağ", "sağcılığa karşı", "gegen rechts", "rechtsextremismus"),
       "Aşırı sağa karşı gösteri, Frankfurt (2024)"),
    _t("oil", "Öltanker", "topic", "Supertanker AbQaiq.jpg",
       ("petrol*", "erdöl*", "rohöl*", "ölpreis*", "opec"), "Petrol tankeri"),
    _t("fuel", "Tankstelle", "topic",
       "Stuttgart 2008 - RAN Tankstelle Preisangabe - by-RaBoe 001.jpg",
       ("tankrabat*", "yakıt*", "benzin*", "kraftstoff*", "spritpreis*", "tankstelle*"),
       "Akaryakıt istasyonunda fiyat tabelası"),
    _t("card_payment", "Kontaktloses Bezahlen", "topic",
       "Two debit cards overlapping (2025-08-25).jpg",
       ("elektronik ödeme", "kartla ödeme", "kartenzahlung", "bargeldlos*", "kontaktlos*"),
       "Banka kartları"),
    _t("pension", "Deutsche Rentenversicherung", "topic",
       "2005-09-29, Stralsund, Logo Deutsche Rentenversicherung am Gebäude der Deutschen "
       "Rentenversicherung Bund.jpg",
       ("emeklilik*", "rentenreform*", "rentenversicherung*", "rente"),
       "Alman Emeklilik Sigortası binası"),
    _t("organ_donation", "Organspendeausweis", "topic", "Organspendeausweis kugelschreiber.JPG",
       ("organ bağış*", "organspende*"), "Organ bağışı kartı"),
    _t("care", "Pflegedienst", "topic", "Medizinischer Pflegedienst Mannheim-Käfertal.jpg",
       ("bakım sigortası*", "bakıma muhtaç*", "bakım ihtiyacı*", "pflegeversicherung*",
        "pflegebedürftig*", "pflegereform*", "pflegeheim*"),
       "Bakım hizmeti"),
    _t("health", "Krankenhaus", "topic", "Hospital room ubt.jpeg",
       ("sağlık reformu*", "sağlık sigortası*", "hastane*", "gesundheitsreform*",
        "krankenkasse*", "krankenhaus*"),
       "Hastane odası"),
    _t("rent", "Mietshaus", "topic", "Storchmühlenweg-Schobersmühlenweg Erfurt.JPG",
       ("mietpreisbremse", "kiracı*", "kira", "kiralık*", "miete*", "mieter*"),
       "Kiralık konutlar"),
    _t("galeria", "Galeria Kaufhof", "topic", "Galeria Kaufhof Neuss 2023-04-20 07-52-45.jpg",
       ("galleria", "galeria", "kaufhof", "karstadt"), "Galeria mağazası"),
)

#: Persons outrank places and topics at the same position: a story about a
#: politician gets the politician, not the building they spoke in.
_KIND_WEIGHT = {"person": 2, "place": 1, "topic": 1}


def normalize(text: str) -> str:
    text = text.replace("İ", "i").replace("’", "'").replace("–", "-").replace("—", "-")
    text = unicodedata.normalize("NFC", text.lower()).replace("\u0307", "")
    return text


def _pattern(alias: str) -> re.Pattern[str]:
    if alias.endswith("*"):
        return re.compile(r"(?<!\w)" + re.escape(alias[:-1]))
    return re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)")


_PATTERNS = {
    entry.key: tuple(_pattern(normalize(alias)) for alias in entry.aliases)
    for entry in COLLECTION
}
_REQUIRES = {
    entry.key: tuple(_pattern(normalize(alias)) for alias in entry.requires)
    for entry in COLLECTION
}


def match_collection(fields: Sequence[tuple[str, int]]) -> CollectionEntry | None:
    """The entry that best depicts a story, from ``(text, weight)`` fields.

    Higher weights mark more prominent text (headline over body). The best
    entry is the one found in the most prominent field, persons first, then
    entries with a context condition, then the earliest mention.
    """
    texts = [(normalize(text or ""), weight) for text, weight in fields]
    everything = " ".join(text for text, _ in texts)
    best: tuple | None = None
    best_entry: CollectionEntry | None = None
    for entry in COLLECTION:
        requires = _REQUIRES[entry.key]
        if requires and not any(p.search(everything) for p in requires):
            continue
        for text, weight in texts:
            positions = [m.start() for p in _PATTERNS[entry.key] if (m := p.search(text))]
            if not positions:
                continue
            rank = (weight * _KIND_WEIGHT[entry.kind], bool(requires), -min(positions))
            if best is None or rank > best:
                best, best_entry = rank, entry
            break
    return best_entry


def requirement_for(entry: CollectionEntry) -> MediaRequirement:
    return MediaRequirement(subject=entry.label, role=entry.role, caption=entry.caption)


class CollectionCommonsProvider(WikimediaCommonsProvider):
    """Resolves a collection label to its pinned Commons file, never a free search."""

    name = "wikimedia_commons_collection"

    def search(self, query: str, *, limit: int) -> tuple[MediaCandidate, ...]:
        entry = next((item for item in COLLECTION if item.label == query), None)
        if entry is None:
            return ()
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": f"File:{entry.commons_file}",
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|user",
            "iiurlwidth": str(THUMB_WIDTH),
        }
        document = self.fetcher.fetch(f"{self.api_url}?{urlencode(params)}")
        try:
            payload = json.loads(document.text or document.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MediaCatalogError("Commons returned an unreadable response") from exc
        pages = payload.get("query", {}).get("pages", []) or []
        candidates = []
        for page in pages:
            candidate = self._to_candidate(page)
            if candidate is None:
                continue
            info = (page.get("imageinfo") or [{}])[0]
            thumb = info.get("thumburl")
            candidates.append(
                MediaCandidate(
                    **{
                        **candidate.__dict__,
                        "file_url": thumb or candidate.file_url,
                        "width": info.get("thumbwidth") or candidate.width,
                        "height": info.get("thumbheight") or candidate.height,
                        "byte_size": None if thumb else candidate.byte_size,
                        "depicted_subject": entry.label,
                    }
                )
            )
        return tuple(candidates[: max(1, limit)])
