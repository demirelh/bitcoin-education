"""Shared Turkish weather vocabulary for extraction and grounding validation."""

from collections import defaultdict

from btcedu.core.weather.models import WeatherCondition

CONDITION_LEXICON_TR: dict[str, WeatherCondition] = {
    "güneşli": WeatherCondition.SUNNY,
    "güneş": WeatherCondition.SUNNY,
    "açık": WeatherCondition.SUNNY,
    "çoğunlukla güneşli": WeatherCondition.MOSTLY_SUNNY,
    "parçalı bulutlu": WeatherCondition.PARTLY_CLOUDY,
    "bulutlu": WeatherCondition.CLOUDY,
    "kapalı": WeatherCondition.OVERCAST,
    "yağmur": WeatherCondition.RAIN,
    "yağmurlu": WeatherCondition.RAIN,
    "yağış": WeatherCondition.RAIN,
    "sağanak": WeatherCondition.SHOWERS,
    "sağanak yağış": WeatherCondition.SHOWERS,
    "şiddetli yağış": WeatherCondition.HEAVY_RAIN,
    "şiddetli yağmur": WeatherCondition.HEAVY_RAIN,
    "gök gürültülü": WeatherCondition.THUNDERSTORMS,
    "fırtına": WeatherCondition.STORM,
    "fırtınalı": WeatherCondition.STORM,
    "kar": WeatherCondition.SNOW,
    "kar yağışı": WeatherCondition.SNOW,
    "sis": WeatherCondition.FOG,
    "sisli": WeatherCondition.FOG,
    "rüzgârlı": WeatherCondition.WINDY,
    "rüzgarlı": WeatherCondition.WINDY,
    "rüzgâr": WeatherCondition.WINDY,
    "rüzgar": WeatherCondition.WINDY,
    "sıcak": WeatherCondition.HOT,
    "serin": WeatherCondition.COLD,
    "soğuk": WeatherCondition.COLD,
}

_keywords: defaultdict[str, list[str]] = defaultdict(list)
for phrase, condition in CONDITION_LEXICON_TR.items():
    _keywords[condition.value].append(phrase)

CONDITION_KEYWORDS_TR: dict[str, tuple[str, ...]] = {
    condition: tuple(sorted(phrases, key=len, reverse=True))
    for condition, phrases in _keywords.items()
}
