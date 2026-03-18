"""Frame analysis service: anchor detection via OpenCV Haar cascades."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class AnchorDetection:
    """Result of anchor detection on a single frame."""

    has_anchor: bool
    confidence: float  # 0.0–1.0
    face_region: tuple[int, int, int, int] | None  # (x, y, w, h) or None
    frame_width: int
    frame_height: int


class FrameAnalyzer(Protocol):
    """Protocol for frame analysis implementations."""

    def detect_anchor(self, frame_path: str | Path) -> AnchorDetection: ...

    def compute_crop_region(self, detection: AnchorDetection) -> tuple[int, int, int, int] | None:
        """Return ``(x, y, w, h)`` crop that excludes the anchor, or *None*."""
        ...


class OpenCVFrameAnalyzer:
    """Anchor detection using OpenCV Haar cascades.

    Lightweight, works on Raspberry Pi without GPU.  Identifies the typical
    Tagesschau anchor position: a face in the lower-center of the frame.
    """

    def __init__(self) -> None:
        try:
            import cv2

            self._cv2 = cv2
            self._face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
        except ImportError:
            logger.warning("opencv-python-headless not installed — anchor detection disabled")
            self._cv2 = None
            self._face_cascade = None

    @property
    def available(self) -> bool:
        return self._cv2 is not None

    def detect_anchor(self, frame_path: str | Path) -> AnchorDetection:
        """Detect a news anchor in *frame_path*.

        Heuristic — a face is treated as an *anchor* when:

        * The face centre is in the lower 40 % of the frame.
        * The face width is > 8 % of the frame width (not a far-away crowd shot).
        * The face centre is within the horizontal middle 60 % of the frame.
        """
        if self._cv2 is None:
            return AnchorDetection(False, 0.0, None, 0, 0)

        cv2 = self._cv2
        img = cv2.imread(str(frame_path))
        if img is None:
            return AnchorDetection(False, 0.0, None, 0, 0)

        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(w // 12, w // 12)
        )

        for fx, fy, fw, fh in faces:
            face_cy = fy + fh / 2
            face_cx = fx + fw / 2

            in_lower_part = face_cy > h * 0.6
            is_large = fw > w * 0.08
            is_centred = 0.2 * w < face_cx < 0.8 * w

            if in_lower_part and is_large and is_centred:
                return AnchorDetection(
                    has_anchor=True,
                    confidence=0.85,
                    face_region=(int(fx), int(fy), int(fw), int(fh)),
                    frame_width=w,
                    frame_height=h,
                )

        return AnchorDetection(False, 0.0, None, w, h)

    def compute_crop_region(self, detection: AnchorDetection) -> tuple[int, int, int, int] | None:
        """Crop to the area *above* the detected anchor.

        Returns ``(x, y, width, crop_height)`` or *None* when no anchor is
        present.
        """
        if not detection.has_anchor or detection.face_region is None:
            return None

        _, fy, _, _ = detection.face_region
        w = detection.frame_width
        h = detection.frame_height

        # Keep the area above the face with a small margin.
        crop_height = max(int(fy * 0.95), h // 3)
        return (0, 0, w, crop_height)


class NullFrameAnalyzer:
    """No-op analyser used when anchor detection is disabled."""

    def detect_anchor(self, frame_path: str | Path) -> AnchorDetection:
        return AnchorDetection(False, 0.0, None, 0, 0)

    def compute_crop_region(self, detection: AnchorDetection) -> tuple[int, int, int, int] | None:
        return None
