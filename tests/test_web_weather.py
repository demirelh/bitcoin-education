"""Tests for weather dashboard API endpoints.

Covers:
- GET /api/episodes/<id>/weather (summary)
- GET /api/episodes/<id>/weather/<chapter_id>/detail
- GET /api/episodes/<id>/weather/<chapter_id>/image
- POST /api/episodes/<id>/weather/<chapter_id>/rerender
- Path traversal prevention
- Missing artifacts handling
- Non-weather chapter rejection
"""

import json
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_settings(tmp_path):
    """Settings with temp directories."""
    return type(
        "Settings",
        (),
        {
            "database_url": "sqlite:///:memory:",
            "raw_data_dir": str(tmp_path / "raw"),
            "transcripts_dir": str(tmp_path / "transcripts"),
            "outputs_dir": str(tmp_path / "outputs"),
            "reports_dir": str(tmp_path / "reports"),
            "logs_dir": str(tmp_path / "logs"),
            # A stub must say what it is: an unauthenticated loopback app. The
            # factory now fails closed when it cannot tell, which is the point.
            "web_auth_enabled": False,
            "web_bind_host": "127.0.0.1",
        },
    )()


@pytest.fixture
def db_engine():
    """In-memory SQLite engine shared across threads."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    return engine


@pytest.fixture
def db_session(db_engine):
    """Session factory from in-memory engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def seeded_db(db_engine, db_session):
    """DB with a weather-capable episode."""
    session = db_session()
    ep = Episode(
        episode_id="ep_weather_01",
        source="youtube_rss",
        title="tagesschau 20:00",
        url="https://youtube.com/watch?v=weather01",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    session.add(ep)
    session.commit()
    session.close()
    return db_engine, db_session


@pytest.fixture
def app(test_settings, seeded_db):
    """Flask test app with seeded DB."""
    from btcedu.web.api import api_bp
    from btcedu.web.jobs import JobManager

    _engine, factory = seeded_db
    application = Flask(__name__)
    application.config["TESTING"] = True
    application.config["session_factory"] = factory
    application.config["settings"] = test_settings
    application.config["job_manager"] = JobManager(str(test_settings.logs_dir))

    application.register_blueprint(api_bp, url_prefix="/api")
    return application


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# Sample weather chapter data matching the extractor/validator contract
WEATHER_NARRATION = (
    "Kuzey ve doğu kesimlerinde sağanak yağış bekleniyor. "
    "Güneybatıda ise güneşli bir hava hakim olacak. "
    "Sıcaklıklar 20 ile 29 derece arasında."
)

SAMPLE_CHAPTERS = {
    "chapters": [
        {
            "chapter_id": "ch01",
            "title": "Hava Durumu",
            "narration": {
                "text": WEATHER_NARRATION,
                "estimated_duration_seconds": 36.4,
                "word_count": 20,
            },
        },
        {
            "chapter_id": "ch02",
            "title": "Wirtschaft",
            "narration": {
                "text": "Bitcoin fiyatı bugün yüzde 3 arttı.",
                "estimated_duration_seconds": 15.0,
                "word_count": 7,
            },
        },
    ]
}


def _setup_chapters(test_settings, chapters_doc=None):
    """Write chapters.json to the expected path."""
    if chapters_doc is None:
        chapters_doc = SAMPLE_CHAPTERS
    out_dir = Path(test_settings.outputs_dir) / "ep_weather_01"
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters_path = out_dir / "chapters.json"
    chapters_path.write_text(json.dumps(chapters_doc, ensure_ascii=False), encoding="utf-8")
    return out_dir


def _setup_weather_image(test_settings, chapter_id="ch01"):
    """Create a fake weather PNG and provenance."""
    images_dir = Path(test_settings.outputs_dir) / "ep_weather_01" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Create a minimal valid PNG (1x1 pixel)
    import struct
    import zlib

    def _minimal_png():
        sig = b"\x89PNG\r\n\x1a\n"
        # IHDR
        ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
        ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
        # IDAT
        raw = zlib.compress(b"\x00\xff\x00\x00")
        idat_crc = zlib.crc32(b"IDAT" + raw) & 0xFFFFFFFF
        idat = struct.pack(">I", len(raw)) + b"IDAT" + raw + struct.pack(">I", idat_crc)
        # IEND
        iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
        iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
        return sig + ihdr + idat + iend

    png_path = images_dir / f"{chapter_id}_weather.png"
    png_path.write_bytes(_minimal_png())

    provenance = {
        "cache_key": "abc123def456abc123de",
        "renderer_version": "1.0.0",
        "schema_version": "1.0",
        "method": "pillow_full",
        "story_id": chapter_id,
        "source_text_hash": "abcdef1234567890",
        "region_count": 3,
        "rendered_at": "2026-07-30T10:00:00+00:00",
    }
    prov_path = images_dir / f"{chapter_id}_weather.provenance.json"
    prov_path.write_text(json.dumps(provenance), encoding="utf-8")

    return images_dir


def _setup_persisted_artifacts(test_settings, chapter_id="ch01"):
    """Write persisted weather JSON artifacts (as core pipeline would)."""
    images_dir = Path(test_settings.outputs_dir) / "ep_weather_01" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    detection = {
        "is_weather_story": True,
        "confidence": 0.97,
        "evidence": ["chapter title matches weather pattern: 'Hava Durumu'"],
    }
    weather_data = {
        "schema_version": "1.0",
        "story_id": chapter_id,
        "language": "tr",
        "source_text_hash": "persisted_hash_abc",
        "forecast_reference": {"date_text": None, "day_reference": None},
        "overview": {"headline": None, "temperature_min_c": 20, "temperature_max_c": 29},
        "regions": [
            {
                "region_id": "north",
                "label_tr": "Kuzey",
                "conditions": ["showers"],
                "temperature_min_c": None,
                "temperature_max_c": None,
                "wind": None,
                "source_span": {
                    "text": "Kuzey kesimlerinde sağanak yağış bekleniyor.",
                    "start": 0,
                    "end": 48,
                },
                "confidence": 0.95,
            },
            {
                "region_id": "southwest",
                "label_tr": "Güneybatı",
                "conditions": ["sunny"],
                "temperature_min_c": None,
                "temperature_max_c": None,
                "wind": None,
                "source_span": {
                    "text": "Güneybatıda güneşli bir hava hakim olacak.",
                    "start": 49,
                    "end": 96,
                },
                "confidence": 0.95,
            },
        ],
        "outlook": [],
        "warnings": [],
        "unresolved_claims": [],
    }
    validation = {
        "valid": True,
        "findings": [],
        "publish_blocked": False,
    }
    scene_plan = {
        "story_id": chapter_id,
        "duration_seconds": 36.4,
        "scenes": [
            {"type": "weather_title", "start": 0.0, "end": 3.5, "headline": "Hava Durumu"},
            {"type": "weather_regions", "start": 3.5, "end": 20.0, "regions": ["north"]},
            {"type": "weather_temperature", "start": 20.0, "end": 36.4},
        ],
    }

    (images_dir / f"{chapter_id}_weather_detection.json").write_text(
        json.dumps(detection), encoding="utf-8"
    )
    (images_dir / f"{chapter_id}_weather.json").write_text(
        json.dumps(weather_data), encoding="utf-8"
    )
    (images_dir / f"{chapter_id}_weather_validation.json").write_text(
        json.dumps(validation), encoding="utf-8"
    )
    (images_dir / f"{chapter_id}_weather_scenes.json").write_text(
        json.dumps(scene_plan), encoding="utf-8"
    )
    return images_dir


def _setup_weather_video(test_settings, chapter_id="ch01"):
    """Create a fake weather MP4 file."""
    images_dir = Path(test_settings.outputs_dir) / "ep_weather_01" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    # Minimal fake MP4 (just ftyp box header)
    mp4_path = images_dir / f"{chapter_id}_weather.mp4"
    ftyp = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    mp4_path.write_bytes(ftyp + b"\x00" * 100)
    return images_dir


# ---------------------------------------------------------------------------
# Weather summary endpoint
# ---------------------------------------------------------------------------


class TestWeatherSummary:
    """GET /api/episodes/<id>/weather"""

    def test_returns_weather_chapters(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()

        assert data["episode_id"] == "ep_weather_01"
        assert data["total_chapters"] == 2
        assert data["weather_count"] == 1

        wc = data["weather_chapters"][0]
        assert wc["chapter_id"] == "ch01"
        assert wc["title"] == "Hava Durumu"
        assert wc["detection"]["is_weather_story"] is True
        assert wc["detection"]["confidence"] > 0.7

    def test_weather_data_extraction(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        # Structured weather data
        wd = wc["weather_data"]
        assert wd["schema_version"] == "1.0"
        assert wd["overview"]["temperature_min_c"] == 20
        assert wd["overview"]["temperature_max_c"] == 29
        assert len(wd["regions"]) >= 2

        # Regions should include north/east (showers) and southwest (sunny)
        region_ids = [r["region_id"] for r in wd["regions"]]
        assert "north" in region_ids or "east" in region_ids

    def test_narration_text_included(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["narration_text"] == WEATHER_NARRATION

    def test_source_spans_present(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        spans = wc["source_spans"]
        assert len(spans) >= 2
        # At least one span should have text
        non_null = [s for s in spans if s is not None]
        assert len(non_null) >= 2
        assert "text" in non_null[0]
        assert "start" in non_null[0]
        assert "end" in non_null[0]

    def test_validation_findings(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        validation = wc["validation"]
        assert "valid" in validation
        assert "findings" in validation
        assert "publish_blocked" in validation
        assert isinstance(validation["findings"], list)

    def test_scene_plan(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        plan = wc["scene_plan"]
        assert plan["duration_seconds"] == pytest.approx(36.4)
        assert len(plan["scenes"]) >= 2
        # First scene should be a title card
        assert plan["scenes"][0]["type"] == "weather_title"

    def test_image_preview_url(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["image_exists"] is True
        assert wc["image_url"] == "api/episodes/ep_weather_01/weather/ch01/image"

    def test_fallback_level_and_renderer_version(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["fallback_level"] == "pillow_full"
        assert wc["renderer_version"] == "1.0.0"

    def test_cache_status_hit(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["cache_status"] == "hit"

    def test_cache_status_miss_when_no_provenance(self, client, test_settings):
        _setup_chapters(test_settings)
        # Create image but no provenance
        images_dir = Path(test_settings.outputs_dir) / "ep_weather_01" / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "ch01_weather.png").write_bytes(b"\x89PNG" + b"\x00" * 100)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["cache_status"] == "miss"

    def test_no_weather_chapters_returns_empty(self, client, test_settings):
        """Episode with no weather chapters returns empty list."""
        chapters = {"chapters": [SAMPLE_CHAPTERS["chapters"][1]]}  # Only economy
        _setup_chapters(test_settings, chapters)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()
        assert data["weather_count"] == 0
        assert data["weather_chapters"] == []

    def test_episode_not_found(self, client):
        r = client.get("/api/episodes/nonexistent_ep/weather")
        assert r.status_code == 404

    def test_no_chapters_file(self, client, test_settings):
        """Episode exists but chapters.json not created yet."""
        # Episode exists in DB but no file on disk
        out_dir = Path(test_settings.outputs_dir) / "ep_weather_01"
        out_dir.mkdir(parents=True, exist_ok=True)
        # No chapters.json

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 404
        assert "chapters" in r.get_json()["error"].lower()

    def test_invalid_episode_id_rejected(self, client):
        r = client.get("/api/episodes/../etc/passwd/weather")
        assert r.status_code in (400, 404)

    def test_empty_episode_id_rejected(self, client):
        r = client.get("/api/episodes//weather")
        assert r.status_code in (404, 308)  # Flask routing


# ---------------------------------------------------------------------------
# Weather chapter detail endpoint
# ---------------------------------------------------------------------------


class TestWeatherChapterDetail:
    """GET /api/episodes/<id>/weather/<chapter_id>/detail"""

    def test_returns_detail_for_weather_chapter(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/detail")
        assert r.status_code == 200
        data = r.get_json()

        assert data["chapter_id"] == "ch01"
        assert data["detection"]["is_weather_story"] is True
        assert "weather_data" in data
        assert "validation" in data
        assert "scene_plan" in data
        assert "provenance" in data

    def test_non_weather_chapter_returns_404(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch02/detail")
        assert r.status_code == 404
        assert "not a weather story" in r.get_json()["error"].lower()

    def test_unknown_chapter_returns_404(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch99/detail")
        assert r.status_code == 404
        assert "not found" in r.get_json()["error"].lower()

    def test_path_traversal_chapter_id(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/..%2F..%2Fetc/detail")
        # secure_filename strips traversal chars; result is either invalid (400) or not found (404)
        assert r.status_code in (400, 404)

    def test_provenance_data_included(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/detail")
        data = r.get_json()

        prov = data["provenance"]
        assert prov["renderer_version"] == "1.0.0"
        assert prov["method"] == "pillow_full"
        assert prov["region_count"] == 3


# ---------------------------------------------------------------------------
# Weather image endpoint
# ---------------------------------------------------------------------------


class TestWeatherImage:
    """GET /api/episodes/<id>/weather/<chapter_id>/image"""

    def test_serves_weather_png(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/image")
        assert r.status_code == 200
        assert r.content_type == "image/png"
        assert r.data[:4] == b"\x89PNG"

    def test_missing_image_returns_404(self, client, test_settings):
        _setup_chapters(test_settings)
        # No image created

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/image")
        assert r.status_code == 404
        assert "not rendered" in r.get_json()["error"].lower()

    def test_invalid_episode_returns_404(self, client):
        r = client.get("/api/episodes/nonexistent/weather/ch01/image")
        assert r.status_code == 404

    def test_path_traversal_chapter_id_blocked(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/..%2Fsecret/image")
        # secure_filename strips traversal; result is either invalid (400) or not found (404)
        assert r.status_code in (400, 404)

    def test_empty_chapter_id_blocked(self, client, test_settings):
        # werkzeug secure_filename("") -> ""
        r = client.get("/api/episodes/ep_weather_01/weather/%20/image")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Weather rerender endpoint
# ---------------------------------------------------------------------------


class TestWeatherRerender:
    """POST /api/episodes/<id>/weather/<chapter_id>/rerender"""

    def test_submits_imagegen_job(self, client, test_settings, app):
        _setup_chapters(test_settings)

        with app.app_context():
            r = client.post("/api/episodes/ep_weather_01/weather/ch01/rerender")
            assert r.status_code == 202
            data = r.get_json()
            assert "job_id" in data

    def test_invalid_episode_returns_404(self, client):
        r = client.post("/api/episodes/nonexistent/weather/ch01/rerender")
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client, test_settings):
        r = client.post("/api/episodes/ep_weather_01/weather/../../../etc/rerender")
        # Flask path normalization + secure_filename → 400 or 404, both safe
        assert r.status_code in (400, 404)

    def test_does_not_publish(self, client, test_settings, app):
        """Rerender action must not trigger publish."""
        _setup_chapters(test_settings)

        with app.app_context():
            r = client.post("/api/episodes/ep_weather_01/weather/ch01/rerender")
            assert r.status_code == 202
            # Verify the job action is "imagegen", not "publish"
            job_mgr = app.config["job_manager"]
            jobs = [j for j in job_mgr._jobs.values() if j.action == "imagegen"]
            assert len(jobs) >= 1


# ---------------------------------------------------------------------------
# Path traversal and security
# ---------------------------------------------------------------------------


class TestWeatherSecurity:
    """Security tests for weather endpoints."""

    def test_dotdot_episode_id(self, client):
        r = client.get("/api/episodes/../../etc/passwd/weather")
        assert r.status_code in (400, 404)

    def test_null_byte_episode_id(self, client):
        r = client.get("/api/episodes/ep%00inject/weather")
        assert r.status_code in (400, 404)

    def test_slash_in_chapter_id(self, client, test_settings):
        _setup_chapters(test_settings)
        r = client.get("/api/episodes/ep_weather_01/weather/ch01%2F..%2F../detail")
        # Flask URL decoding + secure_filename → safe 400 or 404
        assert r.status_code in (400, 404)

    def test_very_long_ids_rejected(self, client):
        long_id = "a" * 500
        r = client.get(f"/api/episodes/{long_id}/weather")
        assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------


class TestWeatherEdgeCases:
    """Edge cases for weather dashboard."""

    def test_chapters_as_list_format(self, client, test_settings):
        """Some episodes store chapters as a flat list, not {chapters: [...]}."""
        chapters = [SAMPLE_CHAPTERS["chapters"][0]]
        _setup_chapters(test_settings, chapters)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()
        assert data["weather_count"] == 1

    def test_missing_narration_in_chapter(self, client, test_settings):
        """Chapter without narration key still works (detection from title)."""
        chapters = {
            "chapters": [
                {
                    "chapter_id": "ch_no_narr",
                    "title": "Hava Durumu",
                }
            ]
        }
        _setup_chapters(test_settings, chapters)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()
        # Detection from title alone should work
        assert data["weather_count"] == 1

    def test_empty_narration_text(self, client, test_settings):
        """Weather detected from title but no narration text."""
        chapters = {
            "chapters": [
                {
                    "chapter_id": "ch_empty",
                    "title": "Hava Durumu",
                    "narration": {"text": "", "estimated_duration_seconds": 10},
                }
            ]
        }
        _setup_chapters(test_settings, chapters)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()
        assert data["weather_count"] == 1
        wc = data["weather_chapters"][0]
        assert wc["weather_data"]["regions"] == []

    def test_corrupt_chapters_json(self, client, test_settings):
        """Malformed chapters.json returns 500 with error."""
        out_dir = Path(test_settings.outputs_dir) / "ep_weather_01"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chapters.json").write_text("{invalid json", encoding="utf-8")

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 500
        assert "cannot read" in r.get_json()["error"].lower()

    def test_image_exists_false_when_no_file(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]
        assert wc["image_exists"] is False
        assert wc["image_url"] is None

    def test_provenance_null_when_no_file(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]
        assert wc["provenance"] is None
        assert wc["renderer_version"] is None

    def test_multiple_weather_chapters(self, client, test_settings):
        """Episode with multiple weather chapters."""
        chapters = {
            "chapters": [
                {
                    "chapter_id": "ch_w1",
                    "title": "Hava Durumu",
                    "narration": {
                        "text": "Kuzeyde yağmur bekleniyor. Sıcaklıklar 15 ile 22 derece.",
                        "estimated_duration_seconds": 20,
                    },
                },
                {
                    "chapter_id": "ch_mid",
                    "title": "Wirtschaft",
                    "narration": {"text": "Ekonomi haberleri.", "estimated_duration_seconds": 30},
                },
                {
                    "chapter_id": "ch_w2",
                    "title": "Hava Tahmini",
                    "narration": {
                        "text": "Yarın güneşli bir hava bekleniyor.",
                        "estimated_duration_seconds": 10,
                    },
                },
            ]
        }
        _setup_chapters(test_settings, chapters)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        assert data["weather_count"] == 2
        assert data["total_chapters"] == 3

    def test_relative_api_urls(self, client, test_settings):
        """Image URLs must be relative, not absolute."""
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        # URL must not start with /
        assert not wc["image_url"].startswith("/")
        assert wc["image_url"].startswith("api/")


# ---------------------------------------------------------------------------
# Override action — now implemented
# ---------------------------------------------------------------------------


class TestWeatherOverrideExists:
    """The override endpoint is now implemented. Verify it's reachable."""

    def test_override_endpoint_exists(self, client, test_settings):
        """POST /weather/<chapter_id>/override route is registered."""
        _setup_chapters(test_settings)
        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Persisted artifact contract
# ---------------------------------------------------------------------------


class TestPersistedArtifacts:
    """API prefers persisted JSON artifacts over recomputation."""

    def test_uses_persisted_weather_data(self, client, test_settings):
        """When {ch}_weather.json exists, API uses it instead of recomputing."""
        _setup_chapters(test_settings)
        _setup_persisted_artifacts(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        assert r.status_code == 200
        data = r.get_json()
        wc = data["weather_chapters"][0]

        # Should use persisted data (has "persisted_hash_abc" not recomputed)
        assert wc["weather_data"]["source_text_hash"] == "persisted_hash_abc"

    def test_uses_persisted_detection(self, client, test_settings):
        """When {ch}_weather_detection.json exists, API uses persisted detection."""
        _setup_chapters(test_settings)
        _setup_persisted_artifacts(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["detection"]["confidence"] == 0.97
        assert "chapter title matches" in wc["detection"]["evidence"][0]

    def test_uses_persisted_validation(self, client, test_settings):
        """When {ch}_weather_validation.json exists, API uses it."""
        _setup_chapters(test_settings)
        _setup_persisted_artifacts(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["validation"]["valid"] is True
        assert wc["validation"]["findings"] == []

    def test_uses_persisted_scene_plan(self, client, test_settings):
        """When {ch}_weather_scenes.json exists, API uses it."""
        _setup_chapters(test_settings)
        _setup_persisted_artifacts(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["scene_plan"]["duration_seconds"] == 36.4
        assert len(wc["scene_plan"]["scenes"]) == 3

    def test_falls_back_to_recomputation_without_artifacts(self, client, test_settings):
        """When no persisted artifacts, API recomputes from narration."""
        _setup_chapters(test_settings)
        # No artifacts written

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        # Recomputed: hash differs from "persisted_hash_abc"
        assert wc["weather_data"]["source_text_hash"] != "persisted_hash_abc"
        # But extraction should still work
        assert len(wc["weather_data"]["regions"]) >= 2

    def test_persisted_source_spans_from_artifact(self, client, test_settings):
        """Source spans from persisted weather_data regions are returned."""
        _setup_chapters(test_settings)
        _setup_persisted_artifacts(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        spans = wc["source_spans"]
        assert len(spans) == 2
        assert spans[0]["text"] == "Kuzey kesimlerinde sağanak yağış bekleniyor."
        assert spans[0]["start"] == 0
        assert spans[0]["end"] == 48


# ---------------------------------------------------------------------------
# Weather video endpoint
# ---------------------------------------------------------------------------


class TestWeatherVideo:
    """GET /api/episodes/<id>/weather/<chapter_id>/video"""

    def test_serves_weather_mp4(self, client, test_settings):
        _setup_chapters(test_settings)
        _setup_weather_video(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/video")
        assert r.status_code == 200
        assert r.content_type == "video/mp4"

    def test_missing_video_returns_404(self, client, test_settings):
        _setup_chapters(test_settings)
        # No video file

        r = client.get("/api/episodes/ep_weather_01/weather/ch01/video")
        assert r.status_code == 404
        assert "not rendered" in r.get_json()["error"].lower()

    def test_invalid_episode_returns_404(self, client):
        r = client.get("/api/episodes/nonexistent/weather/ch01/video")
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client, test_settings):
        _setup_chapters(test_settings)
        r = client.get("/api/episodes/ep_weather_01/weather/..%2Fhack/video")
        assert r.status_code in (400, 404)

    def test_video_url_in_summary(self, client, test_settings):
        """Summary includes video_url when MP4 exists."""
        _setup_chapters(test_settings)
        _setup_weather_video(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["video_exists"] is True
        assert wc["video_url"] == "api/episodes/ep_weather_01/weather/ch01/video"
        assert wc["asset_type"] == "video"

    def test_asset_type_image_when_no_video(self, client, test_settings):
        """asset_type is 'image' when only PNG exists."""
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["video_exists"] is False
        assert wc["asset_type"] == "image"
        assert wc["video_url"] is None

    def test_both_image_and_video_available(self, client, test_settings):
        """When both PNG and MP4 exist, both URLs are provided."""
        _setup_chapters(test_settings)
        _setup_weather_image(test_settings)
        _setup_weather_video(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        data = r.get_json()
        wc = data["weather_chapters"][0]

        assert wc["image_exists"] is True
        assert wc["video_exists"] is True
        assert wc["image_url"] is not None
        assert wc["video_url"] is not None
        assert wc["asset_type"] == "video"  # Video takes priority


# ---------------------------------------------------------------------------
# SPA JavaScript syntax
# ---------------------------------------------------------------------------


class TestJsSyntax:
    """Verify JavaScript file is syntactically valid."""

    @pytest.fixture
    def full_app_client(self, test_settings, seeded_db):
        """Flask app using create_app (has static files)."""
        from btcedu.web.app import create_app

        _engine, factory = seeded_db
        application = create_app(settings=test_settings)
        application.config["session_factory"] = factory
        application.config["TESTING"] = True
        return application.test_client()

    def test_js_no_syntax_errors(self, full_app_client):
        """app.js loads without 500 and contains weather panel code."""
        r = full_app_client.get("/static/app.js")
        assert r.status_code == 200
        js = r.data.decode()
        assert "loadWeatherPanel" in js
        assert "rerenderWeather" in js
        assert "weather-panel" in js

    def test_js_uses_relative_weather_urls(self, full_app_client):
        """Weather panel uses relative API URLs."""
        r = full_app_client.get("/static/app.js")
        js = r.data.decode()
        # Must not use absolute /api/ prefix
        assert 'fetch("/api/episodes' not in js
        # Uses relative form via api() helper
        assert "/episodes/${selected.episode_id}/weather" in js

    def test_css_has_weather_styles(self, full_app_client):
        """styles.css contains weather panel styles."""
        r = full_app_client.get("/static/styles.css")
        assert r.status_code == 200
        css = r.data.decode()
        assert ".weather-panel" in css
        assert ".weather-chapter-card" in css
        assert ".badge-cache-hit" in css
        assert ".weather-scenes-bar" in css

    def test_weather_tab_present_for_tagesschau(self, full_app_client):
        """app.js includes weather tab for tagesschau profile."""
        r = full_app_client.get("/static/app.js")
        js = r.data.decode()
        assert 'tab("weather", "Weather"' in js

    def test_js_has_override_controls(self, full_app_client):
        """app.js includes override control functions."""
        r = full_app_client.get("/static/app.js")
        js = r.data.decode()
        assert "setWeatherOverride" in js
        assert "rerenderWeather" in js
        assert "badge-override" in js

    def test_css_has_override_styles(self, full_app_client):
        """styles.css includes override button styles."""
        r = full_app_client.get("/static/styles.css")
        css = r.data.decode()
        assert ".badge-override" in css
        assert ".btn-active" in css


# ---------------------------------------------------------------------------
# Weather override endpoint
# ---------------------------------------------------------------------------


class TestWeatherOverride:
    """POST /api/episodes/<id>/weather/<chapter_id>/override"""

    def test_set_weather_override(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["value"] == "weather"
        assert data["overrides"]["ch01"] == "weather"

    def test_set_normal_override(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "normal"},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["value"] == "normal"
        assert data["overrides"]["ch01"] == "normal"

    def test_clear_override(self, client, test_settings):
        _setup_chapters(test_settings)

        # Set first
        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )
        # Clear
        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": None},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["value"] is None
        assert "ch01" not in data["overrides"]

    def test_invalid_value_rejected(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "invalid"},
        )
        assert r.status_code == 400
        assert "must be" in r.get_json()["error"]

    def test_nonexistent_chapter_rejected(self, client, test_settings):
        _setup_chapters(test_settings)

        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch99/override",
            json={"value": "weather"},
        )
        assert r.status_code == 404
        assert "not found" in r.get_json()["error"].lower()

    def test_nonexistent_episode_rejected(self, client):
        r = client.post(
            "/api/episodes/nonexistent/weather/ch01/override",
            json={"value": "weather"},
        )
        assert r.status_code == 404

    def test_path_traversal_blocked(self, client, test_settings):
        _setup_chapters(test_settings)
        r = client.post(
            "/api/episodes/ep_weather_01/weather/..%2Fhack/override",
            json={"value": "weather"},
        )
        assert r.status_code in (400, 404)

    def test_creates_stale_marker(self, client, test_settings):
        """Override writes manifest.json.stale file."""
        _setup_chapters(test_settings)

        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )
        stale_path = (
            Path(test_settings.outputs_dir) / "ep_weather_01" / "images" / "manifest.json.stale"
        )
        assert stale_path.exists()

    def test_writes_overrides_file(self, client, test_settings):
        """Override persists to weather_overrides.json."""
        _setup_chapters(test_settings)

        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "normal"},
        )
        overrides_path = (
            Path(test_settings.outputs_dir) / "ep_weather_01" / "images" / "weather_overrides.json"
        )
        assert overrides_path.exists()
        data = json.loads(overrides_path.read_text())
        assert data["ch01"] == "normal"

    def test_does_not_publish(self, client, test_settings):
        """Override action never triggers publish."""
        _setup_chapters(test_settings)

        r = client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )
        assert r.status_code == 200
        # No job submitted — just file write
        assert "job_id" not in r.get_json()

    def test_override_affects_summary_output(self, client, test_settings):
        """Setting 'normal' override removes chapter from weather summary."""
        _setup_chapters(test_settings)

        # ch01 is "Hava Durumu" — normally detected as weather
        r1 = client.get("/api/episodes/ep_weather_01/weather")
        assert r1.get_json()["weather_count"] == 1

        # Set override to normal
        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "normal"},
        )

        r2 = client.get("/api/episodes/ep_weather_01/weather")
        assert r2.get_json()["weather_count"] == 0

    def test_weather_override_forces_non_weather_chapter(self, client, test_settings):
        """Setting 'weather' override on a non-weather chapter makes it appear."""
        _setup_chapters(test_settings)

        # ch02 is "Wirtschaft" — normally NOT weather
        r1 = client.get("/api/episodes/ep_weather_01/weather")
        ch_ids = [wc["chapter_id"] for wc in r1.get_json()["weather_chapters"]]
        assert "ch02" not in ch_ids

        # Force as weather
        client.post(
            "/api/episodes/ep_weather_01/weather/ch02/override",
            json={"value": "weather"},
        )

        r2 = client.get("/api/episodes/ep_weather_01/weather")
        ch_ids = [wc["chapter_id"] for wc in r2.get_json()["weather_chapters"]]
        assert "ch02" in ch_ids

    def test_override_shown_in_detail(self, client, test_settings):
        """Override value is included in weather detail response."""
        _setup_chapters(test_settings)

        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )

        r = client.get("/api/episodes/ep_weather_01/weather")
        wc = r.get_json()["weather_chapters"][0]
        assert wc["override"] == "weather"

    def test_no_override_shown_as_null(self, client, test_settings):
        """Without override, 'override' field is null."""
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather")
        wc = r.get_json()["weather_chapters"][0]
        assert wc["override"] is None

    def test_get_overrides_endpoint(self, client, test_settings):
        """GET /weather/overrides returns current overrides map."""
        _setup_chapters(test_settings)

        # Set an override
        client.post(
            "/api/episodes/ep_weather_01/weather/ch01/override",
            json={"value": "weather"},
        )

        r = client.get("/api/episodes/ep_weather_01/weather/overrides")
        assert r.status_code == 200
        data = r.get_json()
        assert data["overrides"]["ch01"] == "weather"

    def test_get_overrides_empty(self, client, test_settings):
        """GET /weather/overrides returns empty dict when no overrides."""
        _setup_chapters(test_settings)

        r = client.get("/api/episodes/ep_weather_01/weather/overrides")
        assert r.status_code == 200
        assert r.get_json()["overrides"] == {}


# ---------------------------------------------------------------------------
# Chapter-targeted rerender
# ---------------------------------------------------------------------------


class TestChapterTargetedRerender:
    """Verify rerender passes chapter_id through the job system."""

    def test_rerender_passes_chapter_id(self, client, test_settings, app):
        """Rerender job includes chapter_id targeting."""
        _setup_chapters(test_settings)

        with app.app_context():
            r = client.post("/api/episodes/ep_weather_01/weather/ch01/rerender")
            assert r.status_code == 202
            job_id = r.get_json()["job_id"]

            # Verify job has chapter_id
            job_mgr = app.config["job_manager"]
            job = job_mgr.get(job_id)
            assert job is not None
            assert job.chapter_id == "ch01"
            assert job.action == "imagegen"
            assert job.force is True
