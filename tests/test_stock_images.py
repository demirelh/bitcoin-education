"""Tests for stock image search and selection (Pexels integration)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.stock_images import (
    StockSearchResult,
    StockSelectResult,
    _compute_chapters_hash,
    _derive_search_query,
    _has_locked_selection,
    _is_search_current,
    auto_select_best,
    finalize_selections,
    search_stock_images,
    select_stock_image,
)
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun


@pytest.fixture
def settings():
    """Minimal settings mock for stock image tests."""
    s = MagicMock()
    s.outputs_dir = ""  # Will be overridden per test
    s.pexels_api_key = "test-pexels-key"
    s.pexels_results_per_chapter = 3
    s.pexels_orientation = "landscape"
    s.pexels_download_size = "large2x"
    s.image_gen_provider = "pexels"
    s.max_episode_cost_usd = 10.0
    return s


@pytest.fixture
def mock_chapter():
    """Create a mock Chapter object."""
    ch = MagicMock()
    ch.chapter_id = "ch01"
    ch.title = "Zenginlik ve Üretim"
    ch.visual = MagicMock()
    ch.visual.type = "b_roll"
    ch.visual.description = "Zenginlik ve üretim süreçlerini gösteren görüntüler"
    ch.visual.image_prompt = None
    ch.narration = MagicMock()
    ch.narration.text = "Bu bölümde zenginlik ve üretim konusu ele alınacak."
    return ch


@pytest.fixture
def mock_chapter_title_card():
    """Create a mock Chapter object with title_card visual."""
    ch = MagicMock()
    ch.chapter_id = "ch15"
    ch.title = "Sonuç"
    ch.visual = MagicMock()
    ch.visual.type = "title_card"
    ch.visual.description = "Sonuç kartı"
    return ch


@pytest.fixture
def mock_chapter_diagram():
    """Create a mock Chapter object with diagram visual."""
    ch = MagicMock()
    ch.chapter_id = "ch03"
    ch.title = "1971: Para Sistemindeki Değişim"
    ch.visual = MagicMock()
    ch.visual.type = "diagram"
    ch.visual.description = "1971 öncesi ve sonrası ekonomik grafikler"
    ch.visual.image_prompt = None
    ch.narration = MagicMock()
    ch.narration.text = "1971'de para sistemi büyük bir değişim geçirdi."
    return ch


@pytest.fixture
def sample_candidates_manifest(tmp_path):
    """Create a sample candidates manifest."""
    manifest = {
        "episode_id": "TEST_EP",
        "schema_version": "1.0",
        "searched_at": "2026-03-15T12:00:00+00:00",
        "chapters_hash": "abc123",
        "chapters": {
            "ch01": {
                "search_query": "wealth production factory finance",
                "candidates": [
                    {
                        "pexels_id": 12345,
                        "photographer": "John Doe",
                        "photographer_url": "https://www.pexels.com/@johndoe",
                        "source_url": "https://www.pexels.com/photo/12345/",
                        "download_url": "https://images.pexels.com/photos/12345/large2x.jpeg",
                        "local_path": "images/candidates/ch01/pexels_12345.jpg",
                        "alt_text": "Factory production line",
                        "width": 1880,
                        "height": 1253,
                        "size_bytes": 234567,
                        "downloaded_at": "2026-03-15T12:00:00+00:00",
                        "selected": False,
                        "locked": False,
                    },
                    {
                        "pexels_id": 12346,
                        "photographer": "Jane Smith",
                        "photographer_url": "https://www.pexels.com/@janesmith",
                        "source_url": "https://www.pexels.com/photo/12346/",
                        "download_url": "https://images.pexels.com/photos/12346/large2x.jpeg",
                        "local_path": "images/candidates/ch01/pexels_12346.jpg",
                        "alt_text": "Wealth concept coins",
                        "width": 4000,
                        "height": 2667,
                        "size_bytes": 345678,
                        "downloaded_at": "2026-03-15T12:00:01+00:00",
                        "selected": False,
                        "locked": False,
                    },
                ],
            },
            "ch02": {
                "search_query": "money inflation chart finance",
                "candidates": [
                    {
                        "pexels_id": 22345,
                        "photographer": "Finance Photos",
                        "photographer_url": "https://www.pexels.com/@finance",
                        "source_url": "https://www.pexels.com/photo/22345/",
                        "download_url": "https://images.pexels.com/photos/22345/large2x.jpeg",
                        "local_path": "images/candidates/ch02/pexels_22345.jpg",
                        "alt_text": "Inflation chart on screen",
                        "width": 1880,
                        "height": 1253,
                        "size_bytes": 123456,
                        "downloaded_at": "2026-03-15T12:00:02+00:00",
                        "selected": False,
                        "locked": False,
                    },
                ],
            },
        },
    }

    # Set up directory structure
    ep_dir = tmp_path / "TEST_EP"
    candidates_dir = ep_dir / "images" / "candidates"
    candidates_dir.mkdir(parents=True)

    # Write manifest
    manifest_path = candidates_dir / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Create fake image files
    for ch_id in ["ch01", "ch02"]:
        ch_dir = candidates_dir / ch_id
        ch_dir.mkdir(parents=True, exist_ok=True)

    for photo_id in [12345, 12346]:
        img_path = candidates_dir / "ch01" / f"pexels_{photo_id}.jpg"
        img_path.write_bytes(b"fake-jpeg-" * 100)

    img_path = candidates_dir / "ch02" / "pexels_22345.jpg"
    img_path.write_bytes(b"fake-jpeg-" * 50)

    return tmp_path


class TestDeriveSearchQuery:
    def test_translates_turkish_terms(self, mock_chapter):
        query = _derive_search_query(mock_chapter)
        # "zenginlik" -> "wealth", "üretim" -> "production"
        assert "wealth" in query
        assert "production" in query

    def test_adds_visual_type_modifiers(self, mock_chapter):
        query = _derive_search_query(mock_chapter)
        # b_roll -> "photo"
        assert "photo" in query

    def test_adds_diagram_modifiers(self, mock_chapter_diagram):
        query = _derive_search_query(mock_chapter_diagram)
        # diagram -> "chart graph infographic"
        assert "chart" in query

    def test_appends_finance_domain(self, mock_chapter):
        query = _derive_search_query(mock_chapter)
        assert "finance" in query

    def test_caps_at_8_keywords(self, mock_chapter):
        query = _derive_search_query(mock_chapter)
        words = query.split()
        assert len(words) <= 8

    def test_deduplicates(self, mock_chapter):
        query = _derive_search_query(mock_chapter)
        words = query.lower().split()
        assert len(words) == len(set(words))

    def test_filters_stop_words(self):
        ch = MagicMock()
        ch.chapter_id = "ch01"
        ch.title = "Giriş ve Sonuç"
        ch.visual = MagicMock()
        ch.visual.type = "b_roll"
        ch.visual.description = "bir grafik ve tablo"
        query = _derive_search_query(ch)
        # "ve" and "bir" should be filtered as stop words
        assert "ve" not in query.split()
        assert "bir" not in query.split()

    def test_preserves_numbers(self, mock_chapter_diagram):
        query = _derive_search_query(mock_chapter_diagram)
        assert "1971" in query


class TestIsSearchCurrent:
    def test_returns_false_when_no_manifest(self, tmp_path):
        assert _is_search_current(tmp_path / "nonexistent.json", "hash") is False

    def test_returns_false_on_stale_marker(self, tmp_path):
        manifest = tmp_path / "candidates_manifest.json"
        manifest.write_text(json.dumps({"chapters_hash": "hash"}))
        stale = tmp_path / "candidates_manifest.json.stale"
        stale.write_text("{}")

        assert _is_search_current(manifest, "hash") is False

    def test_returns_false_on_hash_mismatch(self, tmp_path):
        manifest = tmp_path / "candidates_manifest.json"
        manifest.write_text(json.dumps({"chapters_hash": "old_hash"}))

        assert _is_search_current(manifest, "new_hash") is False

    def test_returns_true_when_current(self, tmp_path):
        manifest = tmp_path / "candidates_manifest.json"
        manifest.write_text(json.dumps({"chapters_hash": "current_hash"}))

        assert _is_search_current(manifest, "current_hash") is True


class TestHasLockedSelection:
    def test_no_locked(self):
        ch_data = {"candidates": [{"selected": True, "locked": False}]}
        assert _has_locked_selection(ch_data) is False

    def test_has_locked(self):
        ch_data = {"candidates": [{"selected": True, "locked": True}]}
        assert _has_locked_selection(ch_data) is True

    def test_empty_candidates(self):
        assert _has_locked_selection({"candidates": []}) is False
        assert _has_locked_selection({}) is False


class TestSelectStockImage:
    def test_select_marks_candidate(self, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()

        select_stock_image(session, "TEST_EP", "ch01", 12345, settings)

        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        ch01 = manifest["chapters"]["ch01"]["candidates"]

        assert ch01[0]["selected"] is True
        assert ch01[0]["locked"] is False
        assert ch01[1]["selected"] is False

    def test_select_with_lock(self, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()

        select_stock_image(session, "TEST_EP", "ch01", 12346, settings, lock=True)

        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        ch01 = manifest["chapters"]["ch01"]["candidates"]

        assert ch01[0]["selected"] is False
        assert ch01[1]["selected"] is True
        assert ch01[1]["locked"] is True

    def test_select_invalid_photo_raises(self, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()

        with pytest.raises(ValueError, match="not found"):
            select_stock_image(session, "TEST_EP", "ch01", 99999, settings)

    def test_select_invalid_chapter_raises(self, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()

        with pytest.raises(ValueError, match="not found"):
            select_stock_image(session, "TEST_EP", "ch99", 12345, settings)

    def test_select_no_manifest_raises(self, tmp_path, settings):
        settings.outputs_dir = str(tmp_path)
        session = MagicMock()

        with pytest.raises(FileNotFoundError, match="No candidates manifest"):
            select_stock_image(session, "MISSING_EP", "ch01", 12345, settings)


class TestAutoSelectBest:
    @patch("btcedu.core.stock_images.finalize_selections")
    def test_selects_first_candidate(self, mock_finalize, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()
        mock_finalize.return_value = StockSelectResult(
            episode_id="TEST_EP",
            images_path=Path("/tmp/images"),
            manifest_path=Path("/tmp/manifest.json"),
            selected_count=2,
            placeholder_count=0,
        )

        auto_select_best(session, "TEST_EP", settings)

        # Verify first candidate is selected
        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())

        ch01 = manifest["chapters"]["ch01"]["candidates"]
        assert ch01[0]["selected"] is True
        assert ch01[1]["selected"] is False

        ch02 = manifest["chapters"]["ch02"]["candidates"]
        assert ch02[0]["selected"] is True

    @patch("btcedu.core.stock_images.finalize_selections")
    def test_preserves_locked_selection(self, mock_finalize, sample_candidates_manifest, settings):
        settings.outputs_dir = str(sample_candidates_manifest)
        session = MagicMock()
        mock_finalize.return_value = StockSelectResult(
            episode_id="TEST_EP",
            images_path=Path("/tmp/images"),
            manifest_path=Path("/tmp/manifest.json"),
            selected_count=2,
            placeholder_count=0,
        )

        # Pre-lock ch01 to second candidate
        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        manifest["chapters"]["ch01"]["candidates"][1]["selected"] = True
        manifest["chapters"]["ch01"]["candidates"][1]["locked"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2))

        auto_select_best(session, "TEST_EP", settings)

        # Verify locked selection is preserved
        manifest = json.loads(manifest_path.read_text())
        ch01 = manifest["chapters"]["ch01"]["candidates"]
        assert ch01[1]["selected"] is True
        assert ch01[1]["locked"] is True

    def test_no_manifest_raises(self, tmp_path, settings):
        settings.outputs_dir = str(tmp_path)
        session = MagicMock()

        with pytest.raises(FileNotFoundError, match="No candidates manifest"):
            auto_select_best(session, "MISSING", settings)


class TestSearchStockImages:
    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    @patch("btcedu.services.pexels_service.PexelsService")
    def test_search_creates_candidates(
        self, MockPexelsService, mock_get_episode, mock_load_chapters,
        tmp_path, settings, mock_chapter
    ):
        settings.outputs_dir = str(tmp_path)

        # Mock episode
        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_get_episode.return_value = mock_ep

        # Mock chapters (one b_roll chapter)
        mock_doc = MagicMock()
        mock_doc.chapters = [mock_chapter]
        mock_load_chapters.return_value = mock_doc

        # Mock Pexels service
        mock_svc = MagicMock()
        mock_photo = MagicMock()
        mock_photo.id = 99999
        mock_photo.photographer = "Test"
        mock_photo.photographer_url = "https://pexels.com/@test"
        mock_photo.url = "https://pexels.com/photo/99999"
        mock_photo.src_large2x = "https://images.pexels.com/99999/large2x.jpeg"
        mock_photo.alt = "Test photo"
        mock_photo.width = 1880
        mock_photo.height = 1253
        mock_svc.search.return_value = MagicMock(photos=[mock_photo])
        mock_svc.download_photo.return_value = tmp_path / "test.jpg"
        MockPexelsService.return_value = mock_svc

        # Create fake downloaded file
        ch_dir = tmp_path / "TEST_EP" / "images" / "candidates" / "ch01"
        ch_dir.mkdir(parents=True)
        (ch_dir / "pexels_99999.jpg").write_bytes(b"photo-data")

        session = MagicMock()
        session.add = MagicMock()
        session.commit = MagicMock()

        result = search_stock_images(session, "TEST_EP", settings)

        assert isinstance(result, StockSearchResult)
        assert result.chapters_searched == 1
        assert result.total_candidates == 1

        # Verify manifest written
        manifest_path = tmp_path / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert "ch01" in manifest["chapters"]

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    def test_search_skips_title_card(
        self, mock_get_episode, mock_load_chapters,
        tmp_path, settings, mock_chapter_title_card
    ):
        settings.outputs_dir = str(tmp_path)

        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_get_episode.return_value = mock_ep

        mock_doc = MagicMock()
        mock_doc.chapters = [mock_chapter_title_card]
        mock_load_chapters.return_value = mock_doc

        session = MagicMock()
        session.add = MagicMock()
        session.commit = MagicMock()

        result = search_stock_images(session, "TEST_EP", settings)

        assert result.chapters_searched == 0
        assert result.skipped_chapters == 1

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    def test_search_idempotent(
        self, mock_get_episode, mock_load_chapters,
        tmp_path, settings
    ):
        settings.outputs_dir = str(tmp_path)

        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_get_episode.return_value = mock_ep

        # Create mock chapters doc with known hash
        mock_doc = MagicMock()
        mock_doc.chapters = []
        mock_load_chapters.return_value = mock_doc

        chapters_hash = _compute_chapters_hash(mock_doc)

        # Create existing manifest with matching hash
        candidates_dir = tmp_path / "TEST_EP" / "images" / "candidates"
        candidates_dir.mkdir(parents=True)
        manifest = {
            "episode_id": "TEST_EP",
            "chapters_hash": chapters_hash,
            "chapters": {"ch01": {"candidates": [{"pexels_id": 1}]}},
        }
        (candidates_dir / "candidates_manifest.json").write_text(json.dumps(manifest))

        session = MagicMock()

        result = search_stock_images(session, "TEST_EP", settings)

        # Should skip because manifest is current
        assert result.total_candidates == 1
        assert result.chapters_searched == 1


class TestFinalizeSelections:
    @patch("btcedu.core.stock_images._create_placeholder_entry")
    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    def test_produces_dalle_compatible_manifest(
        self, mock_get_episode, mock_load_chapters, mock_placeholder,
        sample_candidates_manifest, settings
    ):
        settings.outputs_dir = str(sample_candidates_manifest)

        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_ep.status = EpisodeStatus.CHAPTERIZED
        mock_get_episode.return_value = mock_ep

        # Mock chapters matching the candidates — use SimpleNamespace for JSON-serializable attrs
        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Zenginlik"
        ch1.visual = MagicMock()
        ch1.visual.type = "b_roll"
        ch1.visual.description = "Wealth production images"

        ch2 = MagicMock()
        ch2.chapter_id = "ch02"
        ch2.title = "Enflasyon"
        ch2.visual = MagicMock()
        ch2.visual.type = "diagram"
        ch2.visual.description = "Inflation chart"

        mock_doc = MagicMock()
        mock_doc.chapters = [ch1, ch2]
        mock_doc.schema_version = "1.0"
        mock_load_chapters.return_value = mock_doc

        # Pre-select candidates
        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        manifest["chapters"]["ch01"]["candidates"][0]["selected"] = True
        manifest["chapters"]["ch02"]["candidates"][0]["selected"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2))

        session = MagicMock()
        session.add = MagicMock()
        session.commit = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = mock_ep

        result = finalize_selections(session, "TEST_EP", settings)

        assert isinstance(result, StockSelectResult)
        assert result.selected_count == 2

        # Check manifest format matches DALL-E format
        output_manifest_path = sample_candidates_manifest / "TEST_EP" / "images" / "manifest.json"
        assert output_manifest_path.exists()
        output_manifest = json.loads(output_manifest_path.read_text())

        assert output_manifest["episode_id"] == "TEST_EP"
        assert "images" in output_manifest
        assert len(output_manifest["images"]) == 2

        img = output_manifest["images"][0]
        assert "chapter_id" in img
        assert "chapter_title" in img
        assert "visual_type" in img
        assert "file_path" in img
        assert "generation_method" in img
        assert img["generation_method"] == "pexels"
        assert "mime_type" in img
        assert "size_bytes" in img
        assert "metadata" in img
        assert "pexels_id" in img["metadata"]
        assert "photographer" in img["metadata"]
        assert "license" in img["metadata"]

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    def test_creates_provenance(
        self, mock_get_episode, mock_load_chapters,
        sample_candidates_manifest, settings
    ):
        settings.outputs_dir = str(sample_candidates_manifest)

        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_get_episode.return_value = mock_ep

        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Test"
        ch1.visual = MagicMock()
        ch1.visual.type = "b_roll"
        ch1.visual.description = "Test visual"

        mock_doc = MagicMock()
        mock_doc.chapters = [ch1]
        mock_load_chapters.return_value = mock_doc

        # Pre-select
        manifest_path = (
            sample_candidates_manifest / "TEST_EP" / "images" / "candidates" / "candidates_manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        manifest["chapters"]["ch01"]["candidates"][0]["selected"] = True
        manifest_path.write_text(json.dumps(manifest))

        session = MagicMock()
        session.add = MagicMock()
        session.commit = MagicMock()

        finalize_selections(session, "TEST_EP", settings)

        provenance_path = sample_candidates_manifest / "TEST_EP" / "provenance" / "imagegen_provenance.json"
        assert provenance_path.exists()
        prov = json.loads(provenance_path.read_text())
        assert prov["stage"] == "imagegen"
        assert prov["generation_method"] == "pexels"
        assert prov["cost_usd"] == 0.0

    @patch("btcedu.core.stock_images._load_chapters")
    @patch("btcedu.core.stock_images._get_episode")
    def test_updates_episode_status(
        self, mock_get_episode, mock_load_chapters,
        sample_candidates_manifest, settings
    ):
        settings.outputs_dir = str(sample_candidates_manifest)

        mock_ep = MagicMock()
        mock_ep.episode_id = "TEST_EP"
        mock_get_episode.return_value = mock_ep

        mock_doc = MagicMock()
        mock_doc.chapters = []
        mock_load_chapters.return_value = mock_doc

        # Pre-select (empty chapters = immediate finalize)
        session = MagicMock()
        session.add = MagicMock()
        session.commit = MagicMock()

        finalize_selections(session, "TEST_EP", settings)

        assert mock_ep.status == EpisodeStatus.IMAGES_GENERATED
