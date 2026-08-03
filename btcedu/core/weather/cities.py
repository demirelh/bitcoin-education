"""City registry for the Germany weather map.

The bundled ``germany_map.svg`` uses a plain equirectangular projection, so map
coordinates can be derived from latitude/longitude with a linear transform. The
constants below reproduce the pre-existing city dots exactly (residual < 0.03
user units), which keeps new cities aligned with the country outline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# germany_map.svg viewBox is "0 0 400 520".
_LON_SCALE = 33.795575
_LON_OFFSET = -152.729692
_LAT_SCALE = -53.917992
_LAT_OFFSET = 3018.868093


def project(latitude: float, longitude: float) -> tuple[float, float]:
    """Project WGS84 coordinates into the map SVG user space."""
    return (
        round(_LON_SCALE * longitude + _LON_OFFSET, 1),
        round(_LAT_SCALE * latitude + _LAT_OFFSET, 1),
    )


@dataclass(frozen=True)
class MapCity:
    """A city rendered on the weather map."""

    city_id: str
    label_tr: str
    latitude: float
    longitude: float
    anchor: Literal["start", "end"] = "start"

    @property
    def map_x(self) -> float:
        return project(self.latitude, self.longitude)[0]

    @property
    def map_y(self) -> float:
        return project(self.latitude, self.longitude)[1]


# Six well-distributed cities matching the dots already present in the SVG.
MAP_CITIES: tuple[MapCity, ...] = (
    MapCity("hamburg", "Hamburg", 53.5511, 9.9937, "start"),
    MapCity("berlin", "Berlin", 52.5200, 13.4050, "end"),
    MapCity("cologne", "Köln", 50.9375, 6.9603, "start"),
    MapCity("frankfurt", "Frankfurt", 50.1109, 8.6821, "start"),
    MapCity("stuttgart", "Stuttgart", 48.7758, 9.1829, "end"),
    MapCity("munich", "Münih", 48.1351, 11.5820, "start"),
)

CITIES_BY_ID: dict[str, MapCity] = {city.city_id: city for city in MAP_CITIES}
