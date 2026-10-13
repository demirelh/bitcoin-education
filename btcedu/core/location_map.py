"""Small Germany map marking the city a news chapter is about.

Places come from a bundled GeoNames extract (``data/de_cities.tsv``), the
outline from the Natural Earth map the weather cards already use. Nothing is
inferred: a city is marked only when the narration names it, and the map
appears when that name is spoken.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent / "data" / "de_cities.tsv"
_OUTLINE = Path(__file__).parent / "weather" / "assets" / "germany_map.svg"
# Named in nearly every political story; marked only when the headline is about it.
_HEADLINE_ONLY = frozenset({"berlin"})
# GeoNames variants that are ordinary words in Turkish news ("Hamas" is listed for Hamm).
_NOT_PLACES = frozenset({"hamas"})
# Without a headline match, a city must be named this early to be the subject.
_EARLY_WORDS = 80
_APOSTROPHES = "'’"
SHOW_SECONDS = 7.0
PANEL_SIZE = (300, 430)
PANEL_POSITION = (64, 80)


@dataclass(frozen=True)
class City:
    name: str
    latitude: float
    longitude: float
    population: int


@dataclass(frozen=True)
class Place:
    city: City
    label: str
    word_index: int


def fold(text: str) -> str:
    text = text.replace("ı", "i").replace("İ", "I").replace("ß", "ss")
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return re.sub(r"[^a-z0-9 -]", "", text.casefold()).strip()


@lru_cache(maxsize=1)
def _index() -> dict[str, City]:
    index: dict[str, City] = {}
    for line in _DATA.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, lat, lon, pop, names = line.split("\t")
        city = City(name, float(lat), float(lon), int(pop))
        own = fold(name)[:3]
        for alias in names.split("|") + [re.split(r"[ (]", name)[0]]:
            key = fold(alias)
            # Foreign-language variants are kept only when they still look like the name.
            if len(key) < 4 or key in _NOT_PLACES or not key.startswith(own):
                continue
            if key not in index or index[key].population < city.population:
                index[key] = city
    return index


def _mentions(text: str) -> list[tuple[int, str, City]]:
    """(word index, name as written, city) for each city named in *text*."""
    index = _index()
    words = text.split()
    found = []
    for position, word in enumerate(words):
        stem = re.split(f"[{_APOSTROPHES}]", word, maxsplit=1)[0].strip('.,;:!?"()«»“”')
        if not stem or not stem[0].isupper():
            continue
        suffixed = any(mark in word for mark in _APOSTROPHES)
        sentence_start = position == 0 or words[position - 1][-1:] in ".!?:"
        if not suffixed and sentence_start:
            continue
        city = index.get(fold(stem))
        if city is not None:
            found.append((position, stem, city))
    return found


def find_place(chapter, previous_narration: str = "") -> Place | None:
    """The city the chapter is about, or None.

    A city in the headline or summary wins. Otherwise the first city named
    early in the narration, unless the previous chapter already named it -- the
    opening transition refers back to the story before. The map is timed to
    the first spoken mention.
    """
    narration = (getattr(chapter.narration, "text", "") or "").strip()
    spoken = _mentions(narration)
    if not spoken:
        return None
    caption = " ".join(
        part
        for part in (
            getattr(chapter, "display_headline", "") or "",
            getattr(chapter, "display_summary", "") or "",
        )
        if part
    )
    headline_cities = {city.name for _, _, city in _mentions(caption)}
    for position, label, city in spoken:
        if city.name in headline_cities:
            return Place(city, label, position)
    carried_over = {city.name for _, _, city in _mentions(previous_narration)}
    for position, label, city in spoken:
        if position >= _EARLY_WORDS:
            break
        if fold(city.name) not in _HEADLINE_ONLY and city.name not in carried_over:
            return Place(city, label, position)
    return None


@lru_cache(maxsize=1)
def _outline() -> list[tuple[float, float]]:
    svg = _OUTLINE.read_text(encoding="utf-8")
    path = re.search(r'id="germany-outline" d="([^"]+)"', svg).group(1)
    return [(float(x), float(y)) for x, y in re.findall(r"(-?[\d.]+),(-?[\d.]+)", path)]


def render_map(place: Place, path: Path, font_path: str | None, accent: str) -> Path:
    """Write the map panel as a transparent PNG and return its path."""
    from PIL import Image, ImageDraw, ImageFont

    from btcedu.core.weather.cities import project

    scale = 2
    width, height = PANEL_SIZE
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius=18 * scale, fill=(10, 16, 28, 190)
    )

    points = _outline()
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    label_h, pad = 64, 18
    box_w, box_h = width - 2 * pad, height - 2 * pad - label_h
    factor = min(box_w / (max(xs) - min(xs)), box_h / (max(ys) - min(ys)))
    off_x = pad + (box_w - (max(xs) - min(xs)) * factor) / 2
    off_y = pad + (box_h - (max(ys) - min(ys)) * factor) / 2

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (
            (off_x + (x - min(xs)) * factor) * scale,
            (off_y + (y - min(ys)) * factor) * scale,
        )

    red, green, blue = (int(accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    draw.polygon(
        [to_px(x, y) for x, y in points],
        fill=(red, green, blue, 200),
        outline=(255, 255, 255, 230),
        width=2 * scale,
    )
    cx, cy = to_px(*project(place.city.latitude, place.city.longitude))
    for radius, colour in (
        (17, (227, 6, 19, 90)),
        (10, (255, 255, 255, 255)),
        (7, (227, 6, 19, 255)),
    ):
        r = radius * scale
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)

    try:
        font = ImageFont.truetype(font_path, 34 * scale)
    except Exception:  # noqa: BLE001 - a missing font must not cost the map
        font = ImageFont.load_default()
    label = place.label
    text_w = draw.textlength(label, font=font)
    draw.text(
        ((width * scale - text_w) / 2, (height - label_h - 4) * scale),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.resize((width, height), Image.LANCZOS).save(path)
    return path
