"""Tests for the frame extraction service (anchor detection)."""

import sys
from unittest.mock import MagicMock, patch

from btcedu.services.frame_extraction_service import (
    AnchorDetection,
    NullFrameAnalyzer,
    OpenCVFrameAnalyzer,
)


def _make_fake_image(height: int = 1080, width: int = 1920):
    """Return a MagicMock that behaves like a cv2 image (ndarray)."""
    img = MagicMock()
    img.shape = (height, width, 3)
    return img


def _make_mock_cv2(faces: list | None = None):
    """Build a mock cv2 module with optional face detection results."""
    mock_cv2 = MagicMock()
    mock_cv2.data = MagicMock()
    mock_cv2.data.haarcascades = "/fake/path/"
    mock_cv2.COLOR_BGR2GRAY = 6

    img = _make_fake_image()
    mock_cv2.imread.return_value = img
    mock_cv2.cvtColor.return_value = MagicMock()

    if faces is None:
        faces = []
    mock_cv2.CascadeClassifier.return_value.detectMultiScale.return_value = faces
    return mock_cv2


class TestNullFrameAnalyzer:
    def test_detect_anchor_always_false(self, tmp_path):
        analyzer = NullFrameAnalyzer()
        result = analyzer.detect_anchor(tmp_path / "any.png")
        assert result.has_anchor is False
        assert result.confidence == 0.0

    def test_compute_crop_region_always_none(self):
        analyzer = NullFrameAnalyzer()
        detection = AnchorDetection(False, 0.0, None, 0, 0)
        assert analyzer.compute_crop_region(detection) is None


class TestOpenCVFrameAnalyzer:
    def test_no_opencv_graceful_fallback(self):
        """When cv2 is missing, analyzer.available is False."""
        with patch.dict(sys.modules, {"cv2": None}):
            with patch("builtins.__import__", side_effect=ImportError("No cv2")):
                analyzer = OpenCVFrameAnalyzer()
                assert not analyzer.available
                result = analyzer.detect_anchor("/fake/path.png")
                assert result.has_anchor is False

    def test_detect_anchor_no_faces(self):
        """No faces detected -> has_anchor=False."""
        mock_cv2 = _make_mock_cv2(faces=[])
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            result = analyzer.detect_anchor("/fake/frame.png")
            assert result.has_anchor is False

    def test_detect_anchor_face_in_lower_center(self):
        """Face in lower-center -> detected as anchor."""
        # (x=800, y=750, w=200, h=200) — lower-center of 1920x1080
        mock_cv2 = _make_mock_cv2(faces=[[800, 750, 200, 200]])
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            result = analyzer.detect_anchor("/fake/frame.png")
            assert result.has_anchor is True
            assert result.confidence == 0.85
            assert result.face_region == (800, 750, 200, 200)

    def test_detect_anchor_face_at_top_not_anchor(self):
        """Face at top of frame -> not an anchor."""
        # (x=800, y=100, w=200, h=200) — top of frame
        mock_cv2 = _make_mock_cv2(faces=[[800, 100, 200, 200]])
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            result = analyzer.detect_anchor("/fake/frame.png")
            assert result.has_anchor is False

    def test_detect_anchor_small_face_not_anchor(self):
        """Small face (far away) -> not an anchor."""
        # Width 50px out of 1920 = 2.6% < 8% threshold
        mock_cv2 = _make_mock_cv2(faces=[[900, 800, 50, 50]])
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            result = analyzer.detect_anchor("/fake/frame.png")
            assert result.has_anchor is False

    def test_compute_crop_region_with_anchor(self):
        """Crop region excludes anchor area."""
        detection = AnchorDetection(
            has_anchor=True,
            confidence=0.85,
            face_region=(800, 700, 200, 250),
            frame_width=1920,
            frame_height=1080,
        )
        mock_cv2 = _make_mock_cv2()
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            region = analyzer.compute_crop_region(detection)
            assert region is not None
            x, y, w, crop_h = region
            assert x == 0
            assert y == 0
            assert w == 1920
            assert crop_h == int(700 * 0.95)

    def test_compute_crop_region_no_anchor(self):
        detection = AnchorDetection(False, 0.0, None, 1920, 1080)
        mock_cv2 = _make_mock_cv2()
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            analyzer = OpenCVFrameAnalyzer()
            assert analyzer.compute_crop_region(detection) is None
