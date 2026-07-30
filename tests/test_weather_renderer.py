"""Comprehensive tests for the deterministic weather renderer.

Covers detection, extraction, validation, scene planning, rendering,
fallback chain, and integration with the image generator.
"""

import json
from unittest.mock import MagicMock, patch

from btcedu.core.weather.detector import detect_weather_story
from btcedu.core.weather.extractor import extract_weather_data
from btcedu.core.weather.models import (
    SCHEMA_VERSION,
    FindingType,
    RegionId,
    WeatherCondition,
    WeatherData,
)
from btcedu.core.weather.renderer import (
    _compute_cache_key,
    _render_pillow_fallback,
    _validate_output,
    render_weather_scene_video,
    render_weather_visual,
)
from btcedu.core.weather.scene_planner import plan_weather_scenes
from btcedu.core.weather.validator import validate_weather_data

# ============================================================
# 1. Detection Tests
# ============================================================


class TestWeatherDetection:
    """Test weather story detection from various signals."""

    def test_turkish_title_hava_durumu(self):
        """Title 'Hava Durumu' yields high confidence."""
        result = detect_weather_story(title="Hava Durumu")
        assert result.is_weather_story is True
        assert result.confidence >= 0.75
        assert any("title" in e for e in result.evidence)

    def test_german_title_wetter(self):
        """German title 'Wetter' detected."""
        result = detect_weather_story(title="Wetter")
        assert result.is_weather_story is True
        assert result.confidence >= 0.75

    def test_narration_with_regions_and_temp(self):
        """Narration with temperature and regional patterns."""
        narration = (
            "Kuzeyde yağmur bekleniyor. Güneybatıda güneşli. Sıcaklıklar 20 ile 29 derece arasında."
        )
        result = detect_weather_story(narration_text=narration)
        assert result.is_weather_story is True
        assert result.confidence >= 0.75

    def test_non_weather_story(self):
        """Non-weather story not detected."""
        result = detect_weather_story(
            title="Ekonomi Haberleri",
            narration_text="Almanya'da enflasyon yüzde 3'e yükseldi.",
        )
        assert result.is_weather_story is False
        assert result.confidence < 0.75

    def test_story_type_metadata(self):
        """Story type 'weather' in metadata."""
        result = detect_weather_story(story_type="weather")
        assert result.is_weather_story is True

    def test_low_confidence_below_threshold(self):
        """Single weak signal not enough."""
        result = detect_weather_story(narration_text="Bugün sıcak bir gün.")
        assert result.confidence < 0.75

    def test_custom_min_confidence(self):
        """Custom min_confidence threshold."""
        result = detect_weather_story(
            title="Hava Durumu",
            min_confidence=0.99,
        )
        # Title alone gives ~0.80, below 0.99
        assert result.is_weather_story is False


# ============================================================
# 2. Extraction Tests
# ============================================================


class TestWeatherExtraction:
    """Test deterministic weather data extraction."""

    def test_north_rain_south_sunny(self):
        """Case 1: 'Kuzeyde yağmur, güneyde güneş bekleniyor.'"""
        text = "Kuzeyde yağmur, güneyde güneş bekleniyor."
        data = extract_weather_data(text)
        assert len(data.regions) >= 2
        north = next((r for r in data.regions if r.region_id == RegionId.NORTH), None)
        south = next((r for r in data.regions if r.region_id == RegionId.SOUTH), None)
        assert north is not None
        assert WeatherCondition.RAIN in north.conditions
        assert south is not None
        assert WeatherCondition.SUNNY in south.conditions

    def test_temperature_range(self):
        """Case 2: 'Sıcaklıklar 20 ile 29 derece arasında.'"""
        text = "Sıcaklıklar 20 ile 29 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == 20
        assert data.overview.temperature_max_c == 29

    def test_no_temperature_no_invention(self):
        """Case 3: Narration without temperature → no invented values."""
        text = "Kuzeyde yağmur bekleniyor."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c is None
        assert data.overview.temperature_max_c is None

    def test_no_region_no_invention(self):
        """Case 4: Narration without regional info → no invented regions."""
        text = "Bugün Almanya genelinde bulutlu bir hava hakim olacak."
        data = extract_weather_data(text)
        # Should not invent specific regions
        specific_regions = [
            r for r in data.regions if r.region_id not in (RegionId.GERMANY, RegionId.CENTRAL)
        ]
        # May detect general statement but not specific directional regions
        assert len(specific_regions) == 0

    def test_ambiguous_region_condition(self):
        """Case 5: Unclear region-condition association → unresolved."""
        text = "Yağmur ve güneş aynı anda olabilir."
        data = extract_weather_data(text)
        # No clear region → conditions should not be assigned to regions
        # (goes to unresolved or no regions)
        for region in data.regions:
            assert region.source_span is not None

    def test_tomorrow_reference(self):
        """Case 6: 'Yarın kuzeyde sağanak bekleniyor.'"""
        text = "Yarın kuzeyde sağanak bekleniyor."
        data = extract_weather_data(text)
        assert data.forecast_reference.day_reference == "tomorrow"
        north = next((r for r in data.regions if r.region_id == RegionId.NORTH), None)
        assert north is not None
        assert WeatherCondition.SHOWERS in north.conditions

    def test_date_wind_and_following_day_outlook(self):
        text = (
            "31 Temmuz Perşembe günü kuzeyde rüzgarlı hava bekleniyor. "
            "Ertesi gün yağışlar azalacak."
        )
        data = extract_weather_data(text)
        north = next(r for r in data.regions if r.region_id == RegionId.NORTH)
        assert data.forecast_reference.date_text == "31 Temmuz Perşembe"
        assert data.forecast_reference.day_reference == "following_day"
        assert north.wind == "rüzgarlı"
        assert data.outlook == ["Ertesi gün yağışlar azalacak"]

    def test_source_spans_present(self):
        """All regions have source spans."""
        text = "Kuzeyde yağmur. Güneyde güneş."
        data = extract_weather_data(text)
        for region in data.regions:
            assert region.source_span is not None
            assert region.source_span.text in text

    def test_schema_version(self):
        """Schema version is set correctly."""
        data = extract_weather_data("Test")
        assert data.schema_version == SCHEMA_VERSION

    def test_source_text_hash(self):
        """Source text hash is populated."""
        data = extract_weather_data("Kuzeyde yağmur.")
        assert len(data.source_text_hash) > 0

    def test_long_multi_region_forecast(self):
        """Case 16: Multiple regions."""
        text = (
            "Kuzey ve doğu kesimlerinde sağanak yağış bekleniyor. "
            "Güneybatıda ise güneşli bir hava hakim olacak. "
            "Batıda parçalı bulutlu. "
            "Sıcaklıklar 18 ile 32 derece arasında."
        )
        data = extract_weather_data(text)
        assert len(data.regions) >= 2
        assert data.overview.temperature_min_c == 18
        assert data.overview.temperature_max_c == 32

    def test_short_general_statement(self):
        """Case 17: Single general statement."""
        text = "Yarın Almanya genelinde güneşli bir hava bekleniyor."
        data = extract_weather_data(text)
        assert data.forecast_reference.day_reference == "tomorrow"


# ============================================================
# 3. Validation Tests
# ============================================================


class TestWeatherValidation:
    """Test grounding validation of extracted data."""

    def test_valid_extraction(self):
        """Well-grounded data passes validation."""
        text = "Kuzeyde yağmur bekleniyor. Sıcaklıklar 20 ile 29 derece arasında."
        data = extract_weather_data(text)
        result = validate_weather_data(data, text)
        assert result.valid is True
        assert result.publish_blocked is False

    def test_unsupported_temperature(self):
        """Temperature not in text blocks publishing."""
        from btcedu.core.weather.models import WeatherOverview

        data = WeatherData(
            overview=WeatherOverview(temperature_min_c=15, temperature_max_c=25),
            regions=[],
        )
        result = validate_weather_data(data, "Bugün sıcak olacak.")
        assert result.publish_blocked is True
        assert any(f.type == FindingType.UNSUPPORTED_TEMPERATURE for f in result.findings)

    def test_inverted_temperature_range(self):
        """Min > max is invalid."""
        from btcedu.core.weather.models import WeatherOverview

        data = WeatherData(
            overview=WeatherOverview(temperature_min_c=30, temperature_max_c=20),
            regions=[],
        )
        result = validate_weather_data(data, "30 ile 20 derece.")
        assert any(f.type == FindingType.INVALID_TEMPERATURE_RANGE for f in result.findings)

    def test_no_fabricated_claims(self):
        """Case 15: No info not in source spans."""
        text = "Kuzeyde yağmur."
        data = extract_weather_data(text)
        result = validate_weather_data(data, text)
        # Should not have unsupported claims
        blocking = [f for f in result.findings if f.publish_blocked]
        assert len(blocking) == 0


# ============================================================
# 4. Scene Planning Tests
# ============================================================


class TestScenePlanning:
    """Test deterministic scene plan generation."""

    def test_basic_scene_plan(self):
        """Basic plan with regions and temperature."""
        text = "Kuzeyde yağmur. Sıcaklıklar 20 ile 29 derece arasında."
        data = extract_weather_data(text)
        plan = plan_weather_scenes(data, 30.0)
        assert plan.duration_seconds == 30.0
        assert len(plan.scenes) >= 2  # title + at least one content scene
        # First scene is always title
        assert plan.scenes[0].type.value == "weather_title"

    def test_no_scene_under_2_seconds(self):
        """No scene shorter than 2 seconds."""
        text = "Kuzeyde yağmur. Güneyde güneş. Sıcaklıklar 15 ile 25 derece."
        data = extract_weather_data(text)
        plan = plan_weather_scenes(data, 20.0)
        for scene in plan.scenes:
            assert scene.end - scene.start >= 2.0

    def test_short_duration_does_not_create_one_second_scene(self):
        data = extract_weather_data("Kuzeyde yağmur bekleniyor.")
        plan = plan_weather_scenes(data, 3.0)
        assert len(plan.scenes) == 1
        assert plan.scenes[0].end - plan.scenes[0].start == 3.0

    def test_scene_order_follows_narration_claim_order(self):
        data = extract_weather_data(
            "Sıcaklıklar 20 ile 29 derece arasında. Kuzeyde yağmur bekleniyor."
        )
        plan = plan_weather_scenes(data, 12)
        assert [scene.type.value for scene in plan.scenes] == [
            "weather_title",
            "weather_temperature",
            "weather_regions",
        ]

    def test_scenes_fill_duration(self):
        """Scenes cover full duration."""
        text = "Kuzeyde yağmur."
        data = extract_weather_data(text)
        plan = plan_weather_scenes(data, 15.0)
        assert plan.scenes[-1].end == 15.0

    def test_empty_data_still_produces_scenes(self):
        """Even with no regions, a title + generic scene is produced."""
        data = WeatherData()
        plan = plan_weather_scenes(data, 10.0)
        assert len(plan.scenes) >= 1


# ============================================================
# 5. Rendering Tests
# ============================================================


class TestWeatherRendering:
    """Test the weather renderer with mocked Chromium."""

    def test_pillow_fallback_produces_output(self, tmp_path):
        """Pillow fallback always produces a non-blank PNG."""
        data = extract_weather_data("Kuzeyde yağmur. Sıcaklıklar 20 ile 29 derece arasında.")
        output = tmp_path / "weather.png"
        success = _render_pillow_fallback(data, output, fallback_level="full")
        assert success is True
        assert output.exists()
        assert output.stat().st_size > 5000

    def test_pillow_generic_fallback(self, tmp_path):
        """Generic fallback works with empty data."""
        data = WeatherData()
        output = tmp_path / "weather.png"
        success = _render_pillow_fallback(data, output, fallback_level="generic")
        assert success is True
        assert output.exists()
        assert output.stat().st_size > 5000

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_no_chromium_falls_to_pillow(self, mock_chromium, tmp_path):
        """Case 9: No Chromium → Pillow fallback, not failure."""
        data = extract_weather_data("Kuzeyde yağmur.")
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"
        result = render_weather_visual(data, plan, output)
        assert result.success is True
        assert output.exists()
        # Should use pillow method
        assert "pillow" in (result.metadata.get("method") or "")

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_render_idempotency(self, mock_chromium, tmp_path):
        """Case 13: Re-render with same data uses cache."""
        data = extract_weather_data("Kuzeyde yağmur.")
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"

        # First render
        result1 = render_weather_visual(data, plan, output)
        assert result1.success is True

        # Second render (should use cache)
        result2 = render_weather_visual(data, plan, output)
        assert result2.success is True
        assert result2.metadata.get("cached") is True

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_force_bypasses_cache(self, mock_chromium, tmp_path):
        """Force flag bypasses cache."""
        data = extract_weather_data("Kuzeyde yağmur.")
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"

        render_weather_visual(data, plan, output)
        result = render_weather_visual(data, plan, output, force=True)
        assert result.metadata.get("cached") is not True

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_renderer_version_change_invalidates_cache(self, mock_chromium, tmp_path):
        """Case 13: Version change → stale cache."""
        data = extract_weather_data("Kuzeyde yağmur.")
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"

        render_weather_visual(data, plan, output)

        # Tamper with provenance to simulate old version
        prov_path = output.with_suffix(".provenance.json")
        prov = json.loads(prov_path.read_text())
        prov["cache_key"] = "old_key"
        prov_path.write_text(json.dumps(prov))

        result = render_weather_visual(data, plan, output)
        assert result.metadata.get("cached") is not True

    def test_validate_output_missing_file(self, tmp_path):
        """Case 10: Missing asset produces CRITICAL finding."""
        findings = _validate_output(tmp_path / "nonexistent.png")
        assert len(findings) > 0
        assert findings[0].type == FindingType.WEATHER_VISUAL_MISSING
        assert findings[0].publish_blocked is True

    def test_validate_output_tiny_file(self, tmp_path):
        """Case 11/12: Tiny file flagged as blank."""
        tiny = tmp_path / "tiny.png"
        tiny.write_bytes(b"\x89PNG" + b"\x00" * 100)
        findings = _validate_output(tiny)
        assert any(f.publish_blocked for f in findings)

    def test_cache_key_deterministic(self):
        """Same inputs produce same cache key."""
        data = extract_weather_data("Kuzeyde yağmur.")
        plan = plan_weather_scenes(data, 10.0)
        k1 = _compute_cache_key(data, plan, profile="test")
        k2 = _compute_cache_key(data, plan, profile="test")
        assert k1 == k2

    def test_cache_key_changes_with_data(self):
        """Different narration produces different cache key."""
        data1 = extract_weather_data("Kuzeyde yağmur.")
        data2 = extract_weather_data("Güneyde güneş.")
        plan1 = plan_weather_scenes(data1, 10.0)
        plan2 = plan_weather_scenes(data2, 10.0)
        k1 = _compute_cache_key(data1, plan1)
        k2 = _compute_cache_key(data2, plan2)
        assert k1 != k2

    def test_scene_video_uses_planned_durations(self, tmp_path):
        text = "Kuzeyde yağmur. Güneyde güneş. Sıcaklıklar 20 ile 29 derece."
        data = extract_weather_data(text)
        plan = plan_weather_scenes(data, 12)
        output = tmp_path / "weather.mp4"

        def fake_render(_data, _plan, path, **_kwargs):
            path.write_bytes(b"png")
            from btcedu.core.weather.models import WeatherRenderResult

            return WeatherRenderResult(success=True, output_path=str(path))

        def fake_run(command, **_kwargs):
            output.write_bytes(b"video")
            completed = MagicMock()
            completed.returncode = 0
            completed.stderr = ""
            completed.args = command
            return completed

        with (
            patch(
                "btcedu.core.weather.renderer.render_weather_visual",
                side_effect=fake_render,
            ),
            patch("btcedu.core.weather.renderer.subprocess.run", side_effect=fake_run) as run,
            patch("btcedu.core.weather.renderer._write_provenance"),
        ):
            result = render_weather_scene_video(data, plan, output)

        assert result.success is True
        assert result.metadata["method"] == "ffmpeg_scene_video"
        assert len(result.metadata["scene_assets"]) == len(plan.scenes)
        command = run.call_args.args[0]
        for scene in plan.scenes:
            assert f"{scene.end - scene.start:.3f}" in command

    def test_title_scene_fallback_is_neutral(self, tmp_path):
        data = extract_weather_data("Kuzeyde yağmur bekleniyor.")
        output = tmp_path / "title.png"
        with patch("btcedu.core.weather.renderer._find_chromium", return_value=None):
            result = render_weather_visual(
                data.model_copy(update={"regions": []}),
                None,
                output,
                force=True,
                render_empty_template=True,
            )
        assert result.success is True
        assert result.metadata["method"] == "pillow_title"

    def test_empty_generic_fallback_has_no_sun_symbol(self, tmp_path):
        from PIL import Image

        output = tmp_path / "generic.png"
        assert _render_pillow_fallback(WeatherData(), output, fallback_level="generic")
        image = Image.open(output).convert("RGB")
        assert (255, 200, 50) not in set(image.get_flattened_data())


# ============================================================
# 6. Integration with Image Generator
# ============================================================


class TestImageGeneratorIntegration:
    """Test weather routing in the image generator."""

    def test_detect_weather_category(self):
        """Weather category detection from visual keywords."""
        from btcedu.core.image_generator import _detect_exact_data_category

        visual = MagicMock()
        visual.deterministic = None
        visual.description = "Hava durumu - Almanya geneli"
        visual.image_prompt = ""
        assert _detect_exact_data_category(visual) == "weather"

    def test_non_weather_not_routed(self):
        """Non-weather visuals not routed to weather renderer."""
        from btcedu.core.image_generator import _detect_exact_data_category

        visual = MagicMock()
        visual.deterministic = None
        visual.description = "A photo of the Bundestag building"
        visual.image_prompt = "Bundestag Berlin"
        assert _detect_exact_data_category(visual) != "weather"

    def test_title_and_narration_detect_weather_with_neutral_visual(self):
        from btcedu.core.image_generator import _weather_detection_for_chapter

        chapter = MagicMock()
        chapter.title = "Hava Durumu"
        chapter.narration.text = "Kuzeyde yağmur bekleniyor."
        chapter.story_type = None
        chapter.source_text = ""
        chapter.metadata = {}
        assert _weather_detection_for_chapter(chapter).is_weather_story is True

    def test_render_weather_chapter_function(self, tmp_path):
        """_render_weather_chapter produces a valid image entry."""
        from btcedu.core.image_generator import _render_weather_chapter

        chapter = MagicMock()
        chapter.chapter_id = "ch_weather_1"
        chapter.title = "Hava Durumu"
        chapter.visual = MagicMock()
        chapter.visual.type = "b_roll"
        chapter.narration = MagicMock()
        chapter.narration.text = "Kuzeyde yağmur bekleniyor. Sıcaklıklar 20 ile 29 derece arasında."
        chapter.narration.estimated_duration_seconds = 15

        with patch("btcedu.core.weather.renderer._find_chromium", return_value=None):
            entry = _render_weather_chapter(chapter, tmp_path)

        assert entry.chapter_id == "ch_weather_1"
        assert entry.generation_method == "deterministic"
        assert entry.metadata["provider"] == "weather_renderer"
        assert entry.metadata["category"] == "weather"
        assert entry.size_bytes > 5000
        assert (tmp_path / f"{chapter.chapter_id}_weather.png").exists()


# ============================================================
# 7. Case 7: Non-weather unchanged
# ============================================================


class TestNonWeatherUnchanged:
    """Verify normal pipeline behavior is not affected."""

    def test_non_weather_chapter_detection(self):
        """Normal news story not flagged as weather."""
        result = detect_weather_story(
            title="Almanya Ekonomisi",
            narration_text="Alman ekonomisi büyümeye devam ediyor.",
        )
        assert result.is_weather_story is False

    def test_episode_without_weather_chapter(self):
        """Case 18: Episode with no weather chapter."""
        chapters = [
            {"title": "Ekonomi", "text": "GDP büyüdü."},
            {"title": "Siyaset", "text": "Meclis toplandı."},
        ]
        for ch in chapters:
            result = detect_weather_story(
                title=ch["title"],
                narration_text=ch["text"],
            )
            assert result.is_weather_story is False


# ============================================================
# 8. Case 14: Weather re-render isolation
# ============================================================


class TestReRenderIsolation:
    """Verify weather re-render doesn't trigger upstream stages."""

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_weather_render_standalone(self, mock_chromium, tmp_path):
        """Weather render operates on narration text alone — no DB/API."""
        text = "Kuzeyde yağmur. Sıcaklıklar 20 ile 29 derece arasında."
        data = extract_weather_data(text, story_id="test_ep_ch1")
        plan = plan_weather_scenes(data, 20.0)
        output = tmp_path / "test.png"

        result = render_weather_visual(data, plan, output)
        assert result.success is True
        # No external API calls needed


# ============================================================
# 9. Fallback chain completeness
# ============================================================


class TestFallbackChain:
    """Test the full fallback chain never produces blank output."""

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_empty_data_produces_generic_fallback(self, mock_chromium, tmp_path):
        """Case 8: Failed extraction → generic weather card."""
        data = WeatherData()  # empty data
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"

        result = render_weather_visual(data, plan, output)
        assert result.success is True
        assert output.exists()
        assert output.stat().st_size > 5000
        assert result.fallback_level in ("generic", "pillow_fallback")

    @patch("btcedu.core.weather.renderer._find_chromium", return_value=None)
    def test_partial_data_uses_reduced(self, mock_chromium, tmp_path):
        """Partial extraction uses reduced render."""
        data = extract_weather_data("Kuzeyde yağmur bekleniyor.")
        plan = plan_weather_scenes(data, 10.0)
        output = tmp_path / "weather.png"

        result = render_weather_visual(data, plan, output)
        assert result.success is True
        assert output.exists()


# ============================================================
# 10. Clause-Level Association Regression Tests (Issue #1)
# ============================================================


class TestClauseLevelAssociation:
    """Regression: conditions must NOT be cross-assigned across regions."""

    def test_north_rain_south_sunny_no_cross_assignment(self):
        """CRITICAL: 'Kuzeyde yağmur, güneyde güneş' — north=rain ONLY, south=sunny ONLY."""
        text = "Kuzeyde yağmur, güneyde güneş bekleniyor."
        data = extract_weather_data(text)

        north = next((r for r in data.regions if r.region_id == RegionId.NORTH), None)
        south = next((r for r in data.regions if r.region_id == RegionId.SOUTH), None)

        assert north is not None, "North region must be extracted"
        assert south is not None, "South region must be extracted"

        # North gets ONLY rain — NOT sunny
        assert WeatherCondition.RAIN in north.conditions
        assert WeatherCondition.SUNNY not in north.conditions, (
            "REGRESSION: north must NOT get sunny from south clause"
        )

        # South gets ONLY sunny — NOT rain
        assert WeatherCondition.SUNNY in south.conditions
        assert WeatherCondition.RAIN not in south.conditions, (
            "REGRESSION: south must NOT get rain from north clause"
        )

    def test_shared_region_phrase_both_get_condition(self):
        """'Kuzey ve doğu kesimlerinde sağanak' — both north AND east share showers."""
        text = "Kuzey ve doğu kesimlerinde sağanak yağış bekleniyor."
        data = extract_weather_data(text)

        north = next((r for r in data.regions if r.region_id == RegionId.NORTH), None)
        east = next((r for r in data.regions if r.region_id == RegionId.EAST), None)

        assert north is not None, "North region must be extracted from shared phrase"
        assert east is not None, "East region must be extracted from shared phrase"

        # Both share showers
        assert WeatherCondition.SHOWERS in north.conditions
        assert WeatherCondition.SHOWERS in east.conditions


class TestWeatherImagegenInvalidation:
    def test_weather_keyword_does_not_match_weathered(self):
        from btcedu.core.image_generator import _detect_exact_data_category

        visual = MagicMock()
        visual.deterministic = None
        visual.description = "A weathered factory exterior"
        visual.image_prompt = ""
        assert _detect_exact_data_category(visual) is None

    def test_weather_config_changes_content_hash(self):
        from btcedu.core.image_generator import _compute_chapters_content_hash
        from btcedu.models.chapter_schema import ChapterDocument

        document = ChapterDocument.model_validate(
            {
                "schema_version": "1.0",
                "episode_id": "weather-hash",
                "title": "Weather Hash",
                "total_chapters": 1,
                "estimated_duration_seconds": 2,
                "chapters": [
                    {
                        "chapter_id": "weather",
                        "title": "Hava Durumu",
                        "order": 1,
                        "narration": {
                            "text": "Kuzeyde yağmur bekleniyor.",
                            "word_count": 3,
                            "estimated_duration_seconds": 2,
                        },
                        "visual": {
                            "type": "diagram",
                            "description": "Hava Durumu",
                            "image_prompt": "Weather map",
                        },
                        "overlays": [],
                        "transitions": {"in": "fade", "out": "fade"},
                        "notes": "",
                    }
                ],
            }
        )

        first = _compute_chapters_content_hash(
            document, weather_config={"enabled": True, "branding": {"title": "Hava Durumu"}}
        )
        second = _compute_chapters_content_hash(
            document, weather_config={"enabled": True, "branding": {"title": "Wetter"}}
        )
        assert first != second

    def test_weather_override_changes_content_hash(self):
        from btcedu.core.image_generator import _compute_chapters_content_hash
        from btcedu.models.chapter_schema import ChapterDocument
        from tests.test_image_generator import _make_chapters_json

        document = ChapterDocument.model_validate(
            _make_chapters_json(
                visual_type="diagram",
                image_prompt="Weather map",
            )
        )
        automatic = _compute_chapters_content_hash(document)
        forced_normal = _compute_chapters_content_hash(
            document,
            weather_overrides={"ch01": "normal"},
        )
        assert automatic != forced_normal

    def test_forced_weather_hash_includes_narration(self):
        from btcedu.core.image_generator import _compute_chapters_content_hash
        from btcedu.models.chapter_schema import ChapterDocument
        from tests.test_image_generator import _make_chapters_json

        first_data = _make_chapters_json(
            visual_type="diagram",
            image_prompt="Neutral information card",
        )
        second_data = json.loads(json.dumps(first_data))
        first_data["chapters"][0]["narration"]["text"] = "Kuzeyde yağmur bekleniyor."
        second_data["chapters"][0]["narration"]["text"] = "Güneyde güneş bekleniyor."
        first = _compute_chapters_content_hash(
            ChapterDocument.model_validate(first_data),
            weather_overrides={"ch01": "weather"},
        )
        second = _compute_chapters_content_hash(
            ChapterDocument.model_validate(second_data),
            weather_overrides={"ch01": "weather"},
        )
        assert first != second

    def test_accent_color_changes_renderer_cache_key(self):
        data = extract_weather_data("Kuzeyde yağmur bekleniyor.")
        plan = plan_weather_scenes(data, 10)
        blue = _compute_cache_key(data, plan, accent_color="#004B87")
        orange = _compute_cache_key(data, plan, accent_color="#F7931A")
        assert blue != orange

    def test_three_clauses_no_leakage(self):
        """Multi-clause sentence: each region gets only its clause's conditions."""
        text = (
            "Kuzey ve doğu kesimlerinde sağanak yağış bekleniyor. "
            "Güneybatıda ise güneşli bir hava hakim olacak. "
            "Batıda parçalı bulutlu."
        )
        data = extract_weather_data(text)

        north = next((r for r in data.regions if r.region_id == RegionId.NORTH), None)
        east = next((r for r in data.regions if r.region_id == RegionId.EAST), None)
        sw = next((r for r in data.regions if r.region_id == RegionId.SOUTHWEST), None)
        west = next((r for r in data.regions if r.region_id == RegionId.WEST), None)

        assert north is not None
        assert east is not None
        assert sw is not None
        assert west is not None

        # North/East: showers only
        assert WeatherCondition.SHOWERS in north.conditions
        assert WeatherCondition.SUNNY not in north.conditions
        assert WeatherCondition.PARTLY_CLOUDY not in north.conditions

        assert WeatherCondition.SHOWERS in east.conditions
        assert WeatherCondition.SUNNY not in east.conditions

        # Southwest: sunny only
        assert WeatherCondition.SUNNY in sw.conditions
        assert WeatherCondition.SHOWERS not in sw.conditions

        # West: partly_cloudy only
        assert WeatherCondition.PARTLY_CLOUDY in west.conditions
        assert WeatherCondition.SHOWERS not in west.conditions
        assert WeatherCondition.SUNNY not in west.conditions

    def test_and_joined_region_clauses_do_not_cross_assign(self):
        text = "Kuzeyde yağmur ve güneyde güneş bekleniyor."
        data = extract_weather_data(text)
        north = next(r for r in data.regions if r.region_id == RegionId.NORTH)
        south = next(r for r in data.regions if r.region_id == RegionId.SOUTH)
        assert north.conditions == [WeatherCondition.RAIN]
        assert south.conditions == [WeatherCondition.SUNNY]

    def test_condition_keywords_do_not_match_inside_words(self):
        text = "Batıda alınan karar yarın açıklanacak."
        data = extract_weather_data(text)
        assert data.regions == []

    def test_ise_keeps_region_condition_in_same_claim(self):
        text = "Güneybatıda ise güneşli bir hava hakim olacak."
        data = extract_weather_data(text)
        southwest = next(r for r in data.regions if r.region_id == RegionId.SOUTHWEST)
        assert southwest.conditions == [WeatherCondition.SUNNY]
        assert data.unresolved_claims == []

    def test_ambiguous_no_region_goes_unresolved(self):
        """Conditions without clear region association go to unresolved."""
        text = "Yağmur ve güneş aynı anda olabilir."
        data = extract_weather_data(text)
        # No region mentioned → no region entries, goes to unresolved
        assert len(data.regions) == 0
        assert len(data.unresolved_claims) > 0


# ============================================================
# 11. Signed Temperature Tests (Issue #2)
# ============================================================


class TestSignedTemperature:
    """Temperature regex must handle signed values and Unicode minus."""

    def test_negative_to_positive_range(self):
        """'-5 ile 2 derece' → min=-5, max=2."""
        text = "Sıcaklıklar -5 ile 2 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == -5
        assert data.overview.temperature_max_c == 2

    def test_unicode_minus(self):
        """'−5 ile 2 derece' (Unicode minus U+2212) → min=-5, max=2."""
        text = "Sıcaklıklar \u22125 ile 2 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == -5
        assert data.overview.temperature_max_c == 2

    def test_both_negative(self):
        """'-8 ile -2 derece' → min=-8, max=-2."""
        text = "Sıcaklıklar -8 ile -2 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == -8
        assert data.overview.temperature_max_c == -2

    def test_sign_preserved_in_source_span(self):
        """Signed temp values are grounded in the text."""
        text = "Kuzeyde kar yağışı. Sıcaklıklar -3 ile 5 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == -3
        assert data.overview.temperature_max_c == 5
        # Validate the grounded values are in narration
        validation = validate_weather_data(data, text)
        # -3 and 5 are both in text, so no unsupported_temperature
        temp_blockers = [
            f for f in validation.findings if f.type == FindingType.UNSUPPORTED_TEMPERATURE
        ]
        assert len(temp_blockers) == 0

    def test_standard_positive_range_unchanged(self):
        """Normal positive range still works: '20 ile 29 derece'."""
        text = "Sıcaklıklar 20 ile 29 derece arasında."
        data = extract_weather_data(text)
        assert data.overview.temperature_min_c == 20
        assert data.overview.temperature_max_c == 29


# ============================================================
# 12. Validation Rejection Tests (Issue #4)
# ============================================================


class TestValidationRejection:
    """_render_weather_chapter must reject publish_blocked validation."""

    def test_render_rejects_unsupported_claim(self, tmp_path):
        """Invented claims → RuntimeError, not silent success."""
        from btcedu.core.image_generator import _render_weather_chapter

        chapter = MagicMock()
        chapter.chapter_id = "ch_bad"
        chapter.title = "Hava Durumu"
        chapter.visual = MagicMock()
        chapter.visual.type = "b_roll"
        # Narration says "kuzeyde yağmur" but we'll mock validation to block
        chapter.narration = MagicMock()
        chapter.narration.text = "Bugün sıcak olacak."
        chapter.narration.estimated_duration_seconds = 10

        # Craft data that will have unsupported temperature (15°C not in text)
        with patch("btcedu.core.weather.renderer._find_chromium", return_value=None):
            with patch("btcedu.core.weather.extractor.extract_weather_data") as mock_extract:
                from btcedu.core.weather.models import (
                    WeatherData,
                    WeatherOverview,
                )

                mock_extract.return_value = WeatherData(
                    overview=WeatherOverview(temperature_min_c=15, temperature_max_c=25),
                    regions=[],
                )
                import pytest

                with pytest.raises(RuntimeError, match="blocked"):
                    _render_weather_chapter(chapter, tmp_path)
