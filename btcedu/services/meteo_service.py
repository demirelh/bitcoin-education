"""Open-Meteo client for per-city temperature data on the weather card.

Open-Meteo is free, requires no API key and serves the DWD ICON model for
Germany. Data fetched here is *external* — it is attributed on the visual and
never treated as a claim extracted from the narration.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

from btcedu.core.weather.cities import MAP_CITIES, MapCity
from btcedu.core.weather.models import CityForecast, WeatherCondition

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE_LABEL = "Open-Meteo / DWD ICON"

# WMO weather interpretation codes → internal condition ids.
_WMO_CONDITIONS: list[tuple[set[int], WeatherCondition]] = [
    ({0}, WeatherCondition.SUNNY),
    ({1}, WeatherCondition.MOSTLY_SUNNY),
    ({2}, WeatherCondition.PARTLY_CLOUDY),
    ({3}, WeatherCondition.OVERCAST),
    ({45, 48}, WeatherCondition.FOG),
    ({51, 53, 55, 56, 57}, WeatherCondition.RAIN),
    ({61, 63, 80, 81}, WeatherCondition.SHOWERS),
    ({65, 66, 67, 82}, WeatherCondition.HEAVY_RAIN),
    ({71, 73, 75, 77, 85, 86}, WeatherCondition.SNOW),
    ({95, 96, 99}, WeatherCondition.THUNDERSTORMS),
]


def condition_for_wmo_code(code: int | None) -> WeatherCondition | None:
    """Map a WMO weather code to an internal condition."""
    if code is None:
        return None
    for codes, condition in _WMO_CONDITIONS:
        if code in codes:
            return condition
    return None


class MeteoService(Protocol):
    """Protocol for external city temperature providers."""

    def fetch_city_forecasts(
        self,
        cities: tuple[MapCity, ...],
        dates: list[date],
    ) -> list[CityForecast]:  # pragma: no cover - protocol
        ...


class OpenMeteoService:
    """Fetches daily min/max temperatures for a set of cities."""

    def __init__(self, *, timeout: float = 20.0, model: str = "icon_seamless") -> None:
        self.timeout = timeout
        self.model = model

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL

    def fetch_city_forecasts(
        self,
        cities: tuple[MapCity, ...] = MAP_CITIES,
        dates: list[date] | None = None,
    ) -> list[CityForecast]:
        """Return per-city daily forecasts for the requested dates.

        Never raises: on any network/parsing problem an empty list is returned
        so the weather visual degrades to narration-only content.
        """
        if not cities or not dates:
            return []
        wanted = sorted({d for d in dates if d is not None})
        if not wanted:
            return []

        params = {
            "latitude": ",".join(f"{city.latitude:.4f}" for city in cities),
            "longitude": ",".join(f"{city.longitude:.4f}" for city in cities),
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": "Europe/Berlin",
            "start_date": wanted[0].isoformat(),
            "end_date": wanted[-1].isoformat(),
            "models": self.model,
        }

        try:
            payload = self._request(params)
        except Exception as error:  # noqa: BLE001 - visual must never break
            logger.warning("Open-Meteo request failed: %s", error)
            return []

        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or len(payload) != len(cities):
            logger.warning(
                "Open-Meteo returned %s entries for %s cities", len(payload), len(cities)
            )
            return []

        wanted_iso = {d.isoformat() for d in wanted}
        forecasts: list[CityForecast] = []
        for city, entry in zip(cities, payload, strict=True):
            daily = (entry or {}).get("daily") or {}
            times = daily.get("time") or []
            maxima = daily.get("temperature_2m_max") or []
            minima = daily.get("temperature_2m_min") or []
            codes = daily.get("weather_code") or []
            for index, day_iso in enumerate(times):
                if day_iso not in wanted_iso:
                    continue
                forecasts.append(
                    CityForecast(
                        city_id=city.city_id,
                        label_tr=city.label_tr,
                        date_iso=day_iso,
                        temperature_max_c=_as_int(maxima, index),
                        temperature_min_c=_as_int(minima, index),
                        condition=condition_for_wmo_code(_as_int(codes, index)),
                        map_x=city.map_x,
                        map_y=city.map_y,
                        anchor=city.anchor,
                    )
                )
        return forecasts

    def _request(self, params: dict[str, str]) -> object:
        import json
        import urllib.parse
        import urllib.request

        url = f"{OPEN_METEO_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "btcedu-weather/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))


def _as_int(values: list, index: int) -> int | None:
    """Return ``values[index]`` rounded to an int, tolerating gaps."""
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None
