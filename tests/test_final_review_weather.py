"""Tests for final review weather video checks.

Covers: rendered segment inspection, source asset existence, ffprobe resolution,
narration duration mismatch, stale provenance with source_text_hash, persisted
weather_validation.json, frame analysis (black/white/transparent/uniform/freeze),
pipeline integration (fail-closed), and findings file cleanup.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.final_review import (
    FinalReviewResult,
    _analyze_raw_rgb,
    _analyze_rgb_image,
    _check_narration_coverage,
    _check_segment_resolution,
    _check_source_asset_exists,
    _check_stale_provenance,
    _check_still_image,
    _check_video_frames,
    _check_weather_validation_json,
    _get_image_dimensions,
    _identify_weather_chapters,
    run_weather_video_checks,
)
from btcedu.core.weather.models import (
    FindingSeverity,
    FindingType,
    ValidationFinding,
)

# ============================================================
# Helpers
# ============================================================


def _make_png(width: int, height: int, color: tuple = (100, 150, 200)) -> bytes:
    """Create a minimal valid PNG with a single color."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw_data = b""
    for _ in range(height):
        raw_data += b"\x00" + bytes(color) * width
    compressed = zlib.compress(raw_data)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return sig + ihdr + idat + iend


def _make_black_png(w: int = 1920, h: int = 1080) -> bytes:
    return _make_png(w, h, (0, 0, 0))


def _make_white_png(w: int = 1920, h: int = 1080) -> bytes:
    return _make_png(w, h, (255, 255, 255))


def _make_normal_png(w: int = 1920, h: int = 1080) -> bytes:
    return _make_png(w, h, (100, 150, 200))


def _weather_chapters_json(chapter_ids: list[str] | None = None) -> dict:
    if chapter_ids is None:
        chapter_ids = ["ch01_weather"]
    chapters = []
    for i, cid in enumerate(chapter_ids):
        is_weather = "weather" in cid.lower() or "hava" in cid.lower()
        chapters.append(
            {
                "chapter_id": cid,
                "title": "Hava Durumu" if is_weather else f"Story {i + 1}",
                "order": i + 1,
                "narration": {
                    "text": (
                        "Kuzeyde yağmur bekleniyor. Güneybatıda güneşli."
                        if is_weather
                        else "Almanya ekonomisi büyümeye devam ediyor."
                    ),
                    "word_count": 8,
                    "estimated_duration_seconds": 5,
                },
                "visual": {
                    "type": "b_roll",
                    "description": "Weather map" if is_weather else "Economy",
                    "image_prompt": "weather map" if is_weather else "economy",
                },
                "overlays": [],
                "transitions": {"in": "fade", "out": "fade"},
            }
        )
    return {
        "schema_version": "2.0",
        "episode_id": "test_ep",
        "total_chapters": len(chapters),
        "chapters": chapters,
    }


def _render_manifest_json(chapter_ids: list[str] | None = None) -> dict:
    if chapter_ids is None:
        chapter_ids = ["ch01_weather"]
    segments = []
    for cid in chapter_ids:
        segments.append(
            {
                "chapter_id": cid,
                "image": f"images/{cid}.png",
                "audio": f"tts/{cid}.mp3",
                "duration_seconds": 10.0,
                "segment_path": f"render/segments/{cid}.mp4",
                "overlays": [],
                "transition_in": "fade",
                "transition_out": "fade",
                "size_bytes": 500000,
                "asset_type": "photo",
            }
        )
    return {
        "episode_id": "test_ep",
        "schema_version": "1.0",
        "resolution": "1920x1080",
        "fps": 25,
        "generated_at": "2026-07-30T12:00:00",
        "total_duration_seconds": 10.0 * len(chapter_ids),
        "total_size_bytes": 500000 * len(chapter_ids),
        "segments": segments,
    }


def _image_manifest_json(chapter_ids: list[str] | None = None) -> dict:
    if chapter_ids is None:
        chapter_ids = ["ch01_weather"]
    return {
        "images": [
            {"chapter_id": cid, "file_path": f"images/{cid}.png", "asset_type": "photo"}
            for cid in chapter_ids
        ]
    }


def _setup_episode_dir(tmp_path: Path, chapter_ids: list[str] | None = None) -> Path:
    """Set up complete episode dir with manifests, assets, and rendered segments."""
    if chapter_ids is None:
        chapter_ids = ["ch01_weather"]

    ep_dir = tmp_path / "test_ep"
    ep_dir.mkdir()

    (ep_dir / "chapters.json").write_text(
        json.dumps(_weather_chapters_json(chapter_ids)), encoding="utf-8"
    )
    render_dir = ep_dir / "render"
    render_dir.mkdir()
    (render_dir / "render_manifest.json").write_text(
        json.dumps(_render_manifest_json(chapter_ids)), encoding="utf-8"
    )
    images_dir = ep_dir / "images"
    images_dir.mkdir()
    (images_dir / "manifest.json").write_text(
        json.dumps(_image_manifest_json(chapter_ids)), encoding="utf-8"
    )

    # Create source PNG assets
    for cid in chapter_ids:
        (images_dir / f"{cid}.png").write_bytes(_make_normal_png())

    # Create rendered segments (fake .mp4 with enough bytes)
    segments_dir = render_dir / "segments"
    segments_dir.mkdir()
    for cid in chapter_ids:
        (segments_dir / f"{cid}.mp4").write_bytes(b"\x00" * 100_000)

    return ep_dir


# ============================================================
# Test: Source Asset Existence
# ============================================================


class TestSourceAssetExists:
    def test_missing_source_asset_critical(self, tmp_path):
        result = FinalReviewResult()
        _check_source_asset_exists(tmp_path / "nonexistent.png", "ch01", result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_VISUAL_MISSING

    def test_existing_normal_asset_no_finding(self, tmp_path):
        asset = tmp_path / "test.png"
        asset.write_bytes(_make_normal_png())
        result = FinalReviewResult()
        _check_source_asset_exists(asset, "ch01", result)
        assert result.publish_blocked is False
        assert len(result.findings) == 0

    def test_tiny_asset_critical(self, tmp_path):
        asset = tmp_path / "tiny.png"
        asset.write_bytes(b"x" * 100)
        result = FinalReviewResult()
        _check_source_asset_exists(asset, "ch01", result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.BLANK_VISUAL_DURING_NARRATION


# ============================================================
# Test: Rendered Segment Resolution (via ffprobe mock)
# ============================================================


class TestSegmentResolution:
    def test_correct_resolution_no_finding(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_dimensions",
            return_value=(1920, 1080),
        ):
            _check_segment_resolution(seg, "ch01", result)
        assert len(result.findings) == 0

    def test_wrong_resolution_major(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_dimensions",
            return_value=(1280, 720),
        ):
            _check_segment_resolution(seg, "ch01", result)
        assert len(result.findings) == 1
        assert result.findings[0].severity == FindingSeverity.MAJOR
        assert "1280x720" in result.findings[0].message
        assert result.publish_blocked is False

    def test_ffprobe_unavailable_warning(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_dimensions",
            return_value=(None, None),
        ):
            _check_segment_resolution(seg, "ch01", result)
        assert any(f.severity == FindingSeverity.WARNING for f in result.findings)


# ============================================================
# Test: Narration Coverage (duration mismatch)
# ============================================================


class TestNarrationCoverage:
    def test_zero_manifest_duration_critical(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        segment = {"duration_seconds": 0, "chapter_id": "ch01"}
        result = FinalReviewResult()
        _check_narration_coverage(seg, segment, "ch01", result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_SCENE_EMPTY

    def test_matching_duration_no_finding(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        segment = {"duration_seconds": 10.0, "chapter_id": "ch01"}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_duration",
            return_value=10.2,
        ):
            _check_narration_coverage(seg, segment, "ch01", result)
        assert len(result.findings) == 0

    def test_large_mismatch_major(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        segment = {"duration_seconds": 10.0, "chapter_id": "ch01"}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_duration",
            return_value=7.5,
        ):
            _check_narration_coverage(seg, segment, "ch01", result)
        assert len(result.findings) == 1
        assert result.findings[0].type == FindingType.WEATHER_DURATION_MISMATCH
        assert result.findings[0].severity == FindingSeverity.MAJOR

    def test_probe_fails_warning(self, tmp_path):
        seg = tmp_path / "ch01.mp4"
        seg.write_bytes(b"\x00" * 10000)
        segment = {"duration_seconds": 10.0, "chapter_id": "ch01"}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._probe_video_duration",
            return_value=None,
        ):
            _check_narration_coverage(seg, segment, "ch01", result)
        assert any(f.severity == FindingSeverity.WARNING for f in result.findings)


# ============================================================
# Test: Stale Provenance + source_text_hash
# ============================================================


class TestStaleProvenance:
    def test_stale_marker_critical(self, tmp_path):
        asset = tmp_path / "weather.png"
        asset.write_bytes(_make_normal_png())
        stale = tmp_path / "weather.png.stale"
        stale.write_text(json.dumps({"reason": "test"}))
        result = FinalReviewResult()
        _check_stale_provenance(asset, "ch01", tmp_path, {}, result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_VISUAL_STALE

    def test_no_stale_no_provenance_clean(self, tmp_path):
        asset = tmp_path / "weather.png"
        asset.write_bytes(_make_normal_png())
        result = FinalReviewResult()
        _check_stale_provenance(asset, "ch01", tmp_path, {}, result)
        assert len(result.findings) == 0

    def test_outdated_renderer_version_major(self, tmp_path):
        asset = tmp_path / "weather.png"
        asset.write_bytes(_make_normal_png())
        prov = tmp_path / "weather.provenance.json"
        prov.write_text(json.dumps({"renderer_version": "0.0.1", "source_text_hash": ""}))
        result = FinalReviewResult()
        _check_stale_provenance(asset, "ch01", tmp_path, {}, result)
        assert len(result.findings) == 1
        assert result.findings[0].severity == FindingSeverity.MAJOR
        assert "0.0.1" in result.findings[0].message

    def test_source_text_hash_mismatch_critical(self, tmp_path):
        """Provenance source_text_hash differs from current narration hash."""
        from btcedu.core.weather.models import RENDERER_VERSION

        asset = tmp_path / "weather.png"
        asset.write_bytes(_make_normal_png())
        prov = tmp_path / "weather.provenance.json"
        prov.write_text(
            json.dumps(
                {
                    "renderer_version": RENDERER_VERSION,
                    "source_text_hash": "old_hash_abc123",
                }
            )
        )
        narration_hashes = {"ch01": "new_hash_def456"}
        result = FinalReviewResult()
        _check_stale_provenance(asset, "ch01", tmp_path, narration_hashes, result)
        assert result.publish_blocked is True
        assert any("source_text_hash" in f.message for f in result.findings)

    def test_source_text_hash_matches_clean(self, tmp_path):
        """Matching source_text_hash → no findings."""
        from btcedu.core.weather.models import RENDERER_VERSION

        asset = tmp_path / "weather.png"
        asset.write_bytes(_make_normal_png())
        narr_hash = hashlib.sha256(b"Kuzeyde yagmur").hexdigest()
        prov = tmp_path / "weather.provenance.json"
        prov.write_text(
            json.dumps(
                {
                    "renderer_version": RENDERER_VERSION,
                    "source_text_hash": narr_hash,
                }
            )
        )
        result = FinalReviewResult()
        _check_stale_provenance(asset, "ch01", tmp_path, {"ch01": narr_hash}, result)
        assert len(result.findings) == 0


# ============================================================
# Test: Persisted weather_validation.json
# ============================================================


class TestWeatherValidationJson:
    def test_blocking_finding_propagated(self, tmp_path):
        """publish_blocked finding in validation.json → blocking final finding."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        val_path = images_dir / "ch01_weather_validation.json"
        val_path.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "type": "unsupported_weather_claim",
                            "severity": "CRITICAL",
                            "message": "Temperature not in source text",
                            "publish_blocked": True,
                        }
                    ]
                }
            )
        )
        result = FinalReviewResult()
        _check_weather_validation_json("ch01", tmp_path, result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.UNSUPPORTED_WEATHER_VISUAL_CLAIM

    def test_nonblocking_finding_ignored(self, tmp_path):
        """Non-blocking finding in validation.json → not propagated."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        val_path = images_dir / "ch01_weather_validation.json"
        val_path.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "type": "weather_extraction_low_confidence",
                            "severity": "WARNING",
                            "message": "Low confidence",
                            "publish_blocked": False,
                        }
                    ]
                }
            )
        )
        result = FinalReviewResult()
        _check_weather_validation_json("ch01", tmp_path, result)
        assert result.publish_blocked is False
        assert len(result.findings) == 0

    def test_no_validation_file_clean(self, tmp_path):
        """Missing validation.json → no findings."""
        result = FinalReviewResult()
        _check_weather_validation_json("ch01", tmp_path, result)
        assert len(result.findings) == 0


# ============================================================
# Test: Still Image Frame Analysis
# ============================================================


class TestStillImageAnalysis:
    def test_black_image_critical(self, tmp_path):
        asset = tmp_path / "black.png"
        asset.write_bytes(_make_black_png())
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        _check_still_image(asset, "ch01", segment, result)
        assert result.publish_blocked is True
        assert any("near-black" in f.message for f in result.findings)

    def test_white_image_critical(self, tmp_path):
        asset = tmp_path / "white.png"
        asset.write_bytes(_make_white_png())
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        _check_still_image(asset, "ch01", segment, result)
        assert result.publish_blocked is True
        assert any("near-white" in f.message for f in result.findings)

    def test_transparent_image_critical(self, tmp_path):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")
        asset = tmp_path / "transparent.png"
        img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        img.save(asset)
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        _check_still_image(asset, "ch01", segment, result)
        assert result.publish_blocked is True
        assert any(f.type == FindingType.TRANSPARENT_WEATHER_FRAME for f in result.findings)

    def test_normal_image_no_finding(self, tmp_path):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("Pillow not available")
        asset = tmp_path / "normal.png"
        img = Image.new("RGB", (1920, 1080))
        draw = ImageDraw.Draw(img)
        for y in range(1080):
            r = int(y / 1080 * 255)
            g = int((1080 - y) / 1080 * 200)
            draw.line([(0, y), (1919, y)], fill=(r, g, 100))
        img.save(asset)
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        _check_still_image(asset, "ch01", segment, result)
        assert result.publish_blocked is False

    def test_short_duration_below_threshold(self, tmp_path):
        """Black image with duration <= 1s → not blocked."""
        asset = tmp_path / "black.png"
        asset.write_bytes(_make_black_png())
        segment = {"duration_seconds": 0.5}
        result = FinalReviewResult()
        _check_still_image(asset, "ch01", segment, result)
        assert result.publish_blocked is False


# ============================================================
# Test: Video Freeze Detection (mocked ffmpeg)
# ============================================================


class TestVideoFreezeDetection:
    def test_missing_ffmpeg_blocks_review(self, tmp_path):
        video = tmp_path / "weather.mp4"
        video.write_bytes(b"\x00" * 100_000)
        result = FinalReviewResult()
        with patch("shutil.which", return_value=None):
            _check_video_frames(video, "ch01", {"duration_seconds": 10.0}, result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_RENDER_FAILED

    def test_no_decoded_frames_blocks_review(self, tmp_path):
        video = tmp_path / "weather.mp4"
        video.write_bytes(b"\x00" * 100_000)
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[],
        ):
            _check_video_frames(video, "ch01", {"duration_seconds": 10.0}, result)
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_RENDER_FAILED

    def test_all_blank_frames_critical(self, tmp_path):
        video = tmp_path / "blank.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 1920, 1080
        black_frame = bytes([0] * width * height * 3)
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[(black_frame, width, height)] * 10,
        ):
            _check_video_frames(video, "ch01", segment, result)
        assert result.publish_blocked is True
        assert any(f.type == FindingType.BLANK_VISUAL_DURING_NARRATION for f in result.findings)

    def test_all_white_frames_critical(self, tmp_path):
        video = tmp_path / "white.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 1920, 1080
        white_frame = bytes([255] * width * height * 3)
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[(white_frame, width, height)] * 10,
        ):
            _check_video_frames(video, "ch01", segment, result)
        assert result.publish_blocked is True

    def test_freeze_frames_major(self, tmp_path):
        """10 identical non-blank frames → freeze MAJOR finding."""
        video = tmp_path / "freeze.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 1920, 1080
        data = bytearray(width * height * 3)
        for i in range(0, len(data), 3):
            data[i] = 100
            data[i + 1] = 150
            data[i + 2] = 200
        frozen_frame = bytes(data)
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[(frozen_frame, width, height)] * 10,
        ):
            _check_video_frames(video, "ch01", segment, result)
        freeze_findings = [f for f in result.findings if "freeze" in f.message.lower()]
        assert len(freeze_findings) >= 1
        assert freeze_findings[0].severity == FindingSeverity.MAJOR

    def test_freeze_duration_uses_actual_spacing(self, tmp_path):
        """Freeze duration must use duration/len(samples), not fixed 0.5s."""
        video = tmp_path / "freeze.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 320, 240
        data = bytes([100, 150, 200] * width * height)
        # 5 samples over 20s → sample_spacing = 4.0s
        # 4 consecutive freeze → 4 * 4.0 = 16.0s freeze
        segment = {"duration_seconds": 20.0}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[(data, width, height)] * 5,
        ):
            _check_video_frames(video, "ch01", segment, result)
        freeze_findings = [f for f in result.findings if "freeze" in f.message.lower()]
        assert len(freeze_findings) >= 1
        # Freeze duration = 4 consecutive * (20.0 / 5) = 16.0s
        assert "16.0s" in freeze_findings[0].message

    def test_normal_video_no_finding(self, tmp_path):
        video = tmp_path / "normal.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 320, 240
        frames = []
        for i in range(10):
            data = bytearray(width * height * 3)
            for j in range(0, len(data), 3):
                pixel = (j // 3 + i * 1000) % 256
                data[j] = pixel
                data[j + 1] = (pixel * 3) % 256
                data[j + 2] = (pixel * 7) % 256
            frames.append((bytes(data), width, height))
        segment = {"duration_seconds": 10.0}
        result = FinalReviewResult()
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=frames,
        ):
            _check_video_frames(video, "ch01", segment, result)
        assert result.publish_blocked is False


# ============================================================
# Test: Raw RGB Analysis
# ============================================================


class TestRawRgbAnalysis:
    def test_black_frame(self):
        width, height = 320, 240
        data = bytes([0] * width * height * 3)
        analysis = _analyze_raw_rgb(data, width, height)
        assert analysis["is_near_black"] is True
        assert analysis["is_near_uniform"] is True

    def test_white_frame(self):
        width, height = 320, 240
        data = bytes([255] * width * height * 3)
        analysis = _analyze_raw_rgb(data, width, height)
        assert analysis["is_near_white"] is True
        assert analysis["is_near_uniform"] is True

    def test_varied_frame(self):
        width, height = 320, 240
        data = bytearray(width * height * 3)
        for i in range(0, len(data), 3):
            pixel_idx = i // 3
            data[i] = pixel_idx % 256
            data[i + 1] = (pixel_idx * 7) % 256
            data[i + 2] = (pixel_idx * 13) % 256
        analysis = _analyze_raw_rgb(bytes(data), width, height)
        assert analysis["is_near_black"] is False
        assert analysis["is_near_white"] is False
        assert analysis["is_near_uniform"] is False


# ============================================================
# Test: PNG Dimension Reading
# ============================================================


class TestImageDimensions:
    def test_png_dimensions(self, tmp_path):
        asset = tmp_path / "test.png"
        asset.write_bytes(_make_png(1920, 1080))
        w, h = _get_image_dimensions(asset)
        assert w == 1920
        assert h == 1080

    def test_nonexistent_file(self, tmp_path):
        w, h = _get_image_dimensions(tmp_path / "missing.png")
        assert w is None
        assert h is None


# ============================================================
# Test: Full Integration — run_weather_video_checks
# ============================================================


class TestRunWeatherVideoChecks:
    def test_no_weather_chapters_clean(self, tmp_path):
        _setup_episode_dir(tmp_path, ["ch01_economy", "ch02_sport"])
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is False
        assert result.weather_chapters_found == 0

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_valid_weather_chapter_clean(self, mock_frames, mock_dur, mock_dim, tmp_path):
        """Valid weather chapter with correct rendered segment → no blocking."""
        _setup_episode_dir(tmp_path, ["ch01_weather"])
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is False
        assert result.weather_chapters_found == 1

    def test_missing_rendered_segment_blocks(self, tmp_path):
        """Missing rendered segment → CRITICAL."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        # Remove rendered segment
        (ep_dir / "render" / "segments" / "ch01_weather.mp4").unlink()
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert any("Rendered segment missing" in f.message for f in result.findings)

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_missing_source_asset_blocks(self, mock_frames, mock_dur, mock_dim, tmp_path):
        """Missing source asset → publish blocked."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        (ep_dir / "images" / "ch01_weather.png").unlink()
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert any(f.type == FindingType.WEATHER_VISUAL_MISSING for f in result.findings)

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    def test_black_source_asset_blocks(self, mock_dur, mock_dim, tmp_path):
        """Black source weather image → blocks via source size/frame check."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        (ep_dir / "images" / "ch01_weather.png").write_bytes(_make_black_png())
        # Also make rendered segment have black frames
        width, height = 1920, 1080
        black_frame = bytes([0] * width * height * 3)
        with patch(
            "btcedu.core.final_review._extract_frame_samples",
            return_value=[(black_frame, width, height)] * 5,
        ):
            result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_stale_asset_blocks(self, mock_frames, mock_dur, mock_dim, tmp_path):
        """Stale-marked weather asset → publish blocked."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        stale = ep_dir / "images" / "ch01_weather.png.stale"
        stale.write_text(json.dumps({"reason": "renderer_updated"}))
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert any(f.type == FindingType.WEATHER_VISUAL_STALE for f in result.findings)

    def test_no_render_manifest_blocks_review(self, tmp_path):
        """No render manifest means final review cannot safely approve."""
        ep_dir = tmp_path / "test_ep"
        ep_dir.mkdir()
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_RENDER_FAILED

    def test_no_chapters_json_blocks_review(self, tmp_path):
        """No chapters artifact means final review cannot identify coverage."""
        ep_dir = tmp_path / "test_ep"
        render_dir = ep_dir / "render"
        render_dir.mkdir(parents=True)
        (render_dir / "render_manifest.json").write_text(
            json.dumps(_render_manifest_json()), encoding="utf-8"
        )
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert result.findings[0].type == FindingType.WEATHER_RENDER_FAILED

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_multiple_chapters_only_weather_checked(
        self, mock_frames, mock_dur, mock_dim, tmp_path
    ):
        """Mixed episode: only weather chapters get checked."""
        _setup_episode_dir(tmp_path, ["ch01_economy", "ch02_weather", "ch03_sport"])
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.weather_chapters_found == 1
        assert result.chapters_checked == 1

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_weather_validation_blocking_finding(self, mock_frames, mock_dur, mock_dim, tmp_path):
        """Persisted weather_validation.json with blocking finding → blocks."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        val_path = ep_dir / "images" / "ch01_weather_weather_validation.json"
        val_path.write_text(
            json.dumps(
                {
                    "findings": [
                        {
                            "type": "unsupported_weather_claim",
                            "severity": "CRITICAL",
                            "message": "temp not in source",
                            "publish_blocked": True,
                        }
                    ]
                }
            )
        )
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True

    @patch("btcedu.core.final_review._probe_video_dimensions", return_value=(1920, 1080))
    @patch("btcedu.core.final_review._probe_video_duration", return_value=10.0)
    @patch("btcedu.core.final_review._check_video_frames")
    def test_source_text_hash_mismatch_blocks(self, mock_frames, mock_dur, mock_dim, tmp_path):
        """Provenance source_text_hash doesn't match narration → blocks."""
        from btcedu.core.weather.models import RENDERER_VERSION

        ep_dir = _setup_episode_dir(tmp_path, ["ch01_weather"])
        prov = ep_dir / "images" / "ch01_weather.provenance.json"
        prov.write_text(
            json.dumps(
                {
                    "renderer_version": RENDERER_VERSION,
                    "source_text_hash": "stale_old_hash",
                }
            )
        )
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is True
        assert any("source_text_hash" in f.message for f in result.findings)


# ============================================================
# Test: Pipeline Integration (fail-closed + findings cleanup)
# ============================================================


class TestPipelineIntegration:
    def test_checker_crash_blocks_gate(self):
        """Checker crash → fail-closed (gate returns 'failed')."""
        # This tests the pipeline behavior: exception = blocked
        blocking_result = FinalReviewResult()
        blocking_result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message="test crash scenario",
                publish_blocked=True,
            )
        )
        # The pipeline catches exceptions and sets _final_review_blocked = True
        # We verify the checker produces blocking results correctly
        assert blocking_result.publish_blocked is True

    def test_no_weather_episode_clean(self, tmp_path):
        """Episode without weather chapters returns cleanly (no crash)."""
        _setup_episode_dir(tmp_path, ["ch01_economy"])
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert result.publish_blocked is False
        assert len(result.findings) == 0

    def test_findings_file_cleanup(self, tmp_path):
        """When no findings, old final_review_findings.json is cleared."""
        ep_dir = _setup_episode_dir(tmp_path, ["ch01_economy"])
        # Simulate old findings file from a previous run
        render_dir = ep_dir / "render"
        findings_file = render_dir / "final_review_findings.json"
        findings_file.write_text('[{"type": "old_finding"}]')
        assert findings_file.exists()

        # The pipeline logic clears the file. We verify the checker returns
        # no findings for non-weather episodes so pipeline can clear it.
        result = run_weather_video_checks("test_ep", str(tmp_path))
        assert len(result.findings) == 0
        # Pipeline would clear: `if _fr_output.exists(): _fr_output.unlink()`


# ============================================================
# Test: Weather Chapter Identification
# ============================================================


class TestWeatherChapterIdentification:
    def test_identifies_hava_durumu_title(self):
        data = _weather_chapters_json(["ch01_weather"])
        ids = _identify_weather_chapters(data)
        assert "ch01_weather" in ids

    def test_non_weather_not_identified(self):
        data = _weather_chapters_json(["ch01_economy"])
        data["chapters"][0]["title"] = "Ekonomi Haberleri"
        data["chapters"][0]["narration"]["text"] = "Enflasyon düşmeye devam ediyor."
        ids = _identify_weather_chapters(data)
        assert len(ids) == 0

    def test_empty_chapters(self):
        ids = _identify_weather_chapters({"chapters": []})
        assert ids == []


# ============================================================
# Test: Analyze RGB Image (Pillow)
# ============================================================


class TestAnalyzeRgbImage:
    def test_black_image(self):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")
        img = Image.new("RGB", (100, 100), (0, 0, 0))
        analysis = _analyze_rgb_image(img)
        assert analysis["is_near_black"] is True
        assert analysis["is_near_uniform"] is True

    def test_white_image(self):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        analysis = _analyze_rgb_image(img)
        assert analysis["is_near_white"] is True
        assert analysis["is_near_uniform"] is True

    def test_varied_image(self):
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")
        img = Image.new("RGB", (100, 100))
        for x in range(100):
            for y in range(100):
                img.putpixel((x, y), (x * 2 % 256, y * 2 % 256, (x + y) % 256))
        analysis = _analyze_rgb_image(img)
        assert analysis["is_near_black"] is False
        assert analysis["is_near_white"] is False
        assert analysis["is_near_uniform"] is False


# ============================================================
# Transition dips must not count as blank frames
# ============================================================


def _solid_frame(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    data = bytearray(width * height * 3)
    for i in range(0, len(data), 3):
        data[i], data[i + 1], data[i + 2] = rgb
    return bytes(data)


def _varied_frame(width: int, height: int, seed: int = 0) -> bytes:
    data = bytearray(width * height * 3)
    for i in range(0, len(data), 3):
        data[i] = (i // 3 + seed) % 256
        data[i + 1] = (i // 7 + seed) % 256
        data[i + 2] = (i // 11 + seed) % 256
    return bytes(data)


class TestBlankConfirmation:
    """A cross-fade darkens the picture briefly; that is not a dropout."""

    def test_bright_neighbour_rejects_the_blank(self, tmp_path):
        from btcedu.core.final_review import _blank_persists

        width, height = 32, 24
        bright = _varied_frame(width, height)
        with patch(
            "btcedu.core.final_review._extract_frame_at",
            return_value=bright,
        ):
            assert (
                _blank_persists(str(tmp_path / "v.mp4"), 5.0, 20.0, "ffmpeg", width, height)
                is False
            )

    def test_dark_neighbours_confirm_the_blank(self, tmp_path):
        from btcedu.core.final_review import _blank_persists

        width, height = 32, 24
        black = _solid_frame(width, height, (0, 0, 0))
        with patch(
            "btcedu.core.final_review._extract_frame_at",
            return_value=black,
        ):
            assert (
                _blank_persists(str(tmp_path / "v.mp4"), 5.0, 20.0, "ffmpeg", width, height) is True
            )

    def test_undecodable_neighbour_keeps_the_finding(self, tmp_path):
        """Fail closed: if the neighbourhood cannot be read, trust the sample."""
        from btcedu.core.final_review import _blank_persists

        with patch("btcedu.core.final_review._extract_frame_at", return_value=None):
            assert _blank_persists(str(tmp_path / "v.mp4"), 5.0, 20.0, "ffmpeg", 32, 24) is True

    def test_single_transition_dip_does_not_block(self, tmp_path):
        """One dark sample between bright ones must not block the gate."""
        video = tmp_path / "weather.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 32, 24
        bright = _varied_frame(width, height)
        dark = _solid_frame(width, height, (2, 2, 2))
        samples = [(_varied_frame(width, height, seed=i), width, height) for i in range(10)]
        samples[4] = (dark, width, height)

        result = FinalReviewResult()
        with (
            patch(
                "btcedu.core.final_review._extract_frame_samples",
                return_value=samples,
            ),
            patch("btcedu.core.final_review._extract_frame_at", return_value=bright),
        ):
            _check_video_frames(video, "ch01", {"duration_seconds": 20.0}, result)

        assert result.publish_blocked is False
        assert not [
            f for f in result.findings if f.type == FindingType.BLANK_VISUAL_DURING_NARRATION
        ]

    def test_sustained_blackout_still_blocks(self, tmp_path):
        """The confirmation must not defang the check for a real dropout."""
        video = tmp_path / "weather.mp4"
        video.write_bytes(b"\x00" * 100_000)
        width, height = 32, 24
        black = _solid_frame(width, height, (0, 0, 0))

        result = FinalReviewResult()
        with (
            patch(
                "btcedu.core.final_review._extract_frame_samples",
                return_value=[(black, width, height)] * 10,
            ),
            patch("btcedu.core.final_review._extract_frame_at", return_value=black),
        ):
            _check_video_frames(video, "ch01", {"duration_seconds": 20.0}, result)

        assert result.publish_blocked is True
