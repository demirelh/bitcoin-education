# Plan: Video-Frame-Extraktions-Pipeline

## Zusammenfassung

Frames aus den Original-Tagesschau-Videos extrahieren und als visuelle Assets nutzen,
anstatt (oder zusätzlich zu) Stock-Fotos und DALL-E-generierten Bildern.

**Entscheidungen:**
- Sprecher-Erkennung: **OpenCV Haar Cascades** (bewährt, ARM-kompatibel, ~40MB)
- Sprecher-Entfernung: **Smart Crop** (kostenlos, Pi-freundlich, kein Qualitätsverlust)
- Stil-Modifikation: **Hybrid** (ffmpeg-Filter default + optionale DALL-E Edit API)
- Pipeline-Integration: **Neuer `frameextract`-Stage** zwischen `chapterize` und `imagegen`
- Fallback: Wenn Frame-Extraktion fehlschlägt → `imagegen` nutzt Stock/DALL-E wie bisher

---

## Phase 0: Architektur-Entscheidungen

### Crop vs. Inpainting vs. Hybrid

| Kriterium | Crop | Inpainting | Hybrid |
|-----------|------|------------|--------|
| Kosten | $0 | ~$0.04/Frame | $0 default |
| Pi-Performance | <1s/Frame | 5-10s/Frame (API) | <1s/Frame |
| Qualität | Gut (720p → ~480p effektiv) | Sehr gut | Gut default |
| Komplexität | Niedrig | Hoch | Mittel |
| Neue Dependencies | Keine | Keine (API) | Keine |

**Entscheidung: Smart Crop als Primary.**

Tagesschau-Layout ist konsistent: Sprecher im unteren Drittel, Hintergrund-Grafik
im oberen 2/3. Smart Crop nutzt die OpenCV-Gesichtserkennung um die optimale
Crop-Region zu bestimmen:
- Wenn Gesicht erkannt → Crop auf Region oberhalb des Gesichts
- Wenn kein Gesicht → Vollframe verwenden (vermutlich B-Roll/Grafik)
- Output immer auf 1920x1080 skaliert (upscale des Crops)

### Stil-Modifikation: Hybrid-Ansatz

**Default (kostenlos):** ffmpeg-Filter-Chain:
```
hue=h=15:s=1.1, curves=vintage, vignette=PI/4, eq=contrast=1.05
```
- Leichte Farbverschiebung + Vintage-Kurve + Vignette
- Verändert Bild genug für Fair-Use-Argument
- ~50ms pro Frame auf Pi

**Optional (per Config):** DALL-E Edit API:
- `style_modification_provider: str = "ffmpeg"` (default) oder `"dalle_edit"`
- Nutzt bestehende OpenAI-API-Key-Infrastruktur
- Nur bei expliziter Aktivierung

### Pipeline-Position

```
chapterize → [NEU: frameextract] → imagegen → review_gate_stock → tts → render
```

Der `frameextract`-Stage produziert **Frame-Kandidaten** pro Chapter.
Der bestehende `imagegen`-Stage wählt dann die beste Quelle:
1. Extrahierte Frames (wenn verfügbar und passend)
2. Pexels Stock (Fallback)
3. DALL-E (Fallback)

Dies erhält die bestehende Architektur: `imagegen` bleibt der einzige Stage der
`images/manifest.json` produziert, und der Renderer konsumiert nur diese Manifest.

---

## Phase 1: Download-Service erweitern

### Datei: `btcedu/services/download_service.py`

Neue Funktion `download_video()` neben der bestehenden `download_audio()`:

```python
def download_video(
    url: str,
    output_dir: str,
    max_height: int = 720,
) -> str:
    """Download video from URL using yt-dlp.

    Downloads best video+audio stream up to max_height resolution.
    Tagesschau videos are typically 720p, so 720 is a good default.

    Args:
        url: Video URL to download.
        output_dir: Directory to save the video file.
        max_height: Maximum video height in pixels (default: 720).

    Returns:
        Path to the downloaded video file.

    Raises:
        RuntimeError: If yt-dlp fails.
    """
```

**yt-dlp Kommando:**
```python
cmd = [
    ytdlp,
    "--format", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
    "--merge-output-format", "mp4",
    "--output", str(out_path / "video.%(ext)s"),
    "--no-playlist",
    "--quiet",
    "--no-warnings",
    url,
]
```

**Wichtig:** Video-Download ist optional — nur wenn `frame_extraction_enabled = True`.
Die bestehende `download_audio()` bleibt unverändert.

### Datei: `btcedu/core/detector.py` (bestehende Download-Orchestrierung)

`download_episode()` erweitern: nach Audio-Download optional auch Video downloaden.

```python
# In download_episode(), nach dem Audio-Download:
if settings.frame_extraction_enabled:
    video_path = download_video(url, raw_dir, max_height=settings.frame_extract_video_height)
    # Speichere Video-Pfad in Episode-Metadata oder als separate Datei
    video_meta_path = Path(raw_dir) / "video_meta.json"
    video_meta_path.write_text(json.dumps({
        "video_path": str(video_path),
        "downloaded_at": _utcnow().isoformat(),
    }))
```

---

## Phase 2: FFmpeg-Service erweitern

### Datei: `btcedu/services/ffmpeg_service.py`

Drei neue Funktionen hinzufügen:

#### 2a) Scene Detection + Frame-Extraktion

```python
@dataclass
class ExtractedFrame:
    """Metadata for a single extracted keyframe."""
    frame_path: Path          # Pfad zum extrahierten Frame (PNG)
    timestamp_seconds: float  # Position im Video
    scene_score: float        # Scene-Change-Score (0-1)
    width: int
    height: int
    size_bytes: int


def extract_keyframes(
    video_path: str | Path,
    output_dir: str | Path,
    scene_threshold: float = 0.3,
    min_interval_seconds: float = 2.0,
    max_frames: int = 100,
    timeout: int = 300,
) -> list[ExtractedFrame]:
    """Extract keyframes using ffmpeg scene detection.

    Uses the scene change detection filter to find significant visual transitions.
    Filters out frames too close together (min_interval_seconds).

    ffmpeg command:
        ffmpeg -i {video} -filter:v "select='gt(scene,{threshold})',showinfo"
               -vsync vfr {output_dir}/frame_%04d.png

    Args:
        video_path: Input video file.
        output_dir: Directory to write frame PNGs.
        scene_threshold: Scene change detection threshold (0.0-1.0).
            Higher = fewer frames. 0.3 is good for news broadcasts.
        min_interval_seconds: Minimum time between extracted frames.
        max_frames: Maximum number of frames to extract.
        timeout: Process timeout in seconds.

    Returns:
        List of ExtractedFrame with metadata, sorted by timestamp.
    """
```

**Implementierungsdetails:**
- Zwei-Pass-Ansatz:
  1. `ffprobe` mit `select='gt(scene,{threshold})',showinfo` → parse Timestamps aus stderr
  2. `ffmpeg` extrahiert Frames an den gefundenen Timestamps
- Alternative (einfacher): Ein-Pass mit `-vsync vfr` und Post-Processing der Frame-Liste
- Frames als PNG (verlustfrei) für maximale Qualität bei Weiterverarbeitung

#### 2b) Frame-Cropping

```python
@dataclass
class CroppedFrame:
    """Metadata for a cropped frame."""
    frame_path: Path
    original_path: Path
    crop_region: tuple[int, int, int, int]  # x, y, width, height
    has_anchor: bool  # Whether anchor was detected
    width: int
    height: int


def crop_frame(
    input_path: str | Path,
    output_path: str | Path,
    crop_region: tuple[int, int, int, int] | None = None,
    target_size: tuple[int, int] = (1920, 1080),
    timeout: int = 30,
) -> CroppedFrame:
    """Crop a frame and scale to target resolution.

    If crop_region is None, uses full frame.
    Always scales output to target_size (default 1920x1080).

    ffmpeg command:
        ffmpeg -i {input} -filter:v "crop={w}:{h}:{x}:{y},scale={tw}:{th}"
               -frames:v 1 {output}

    Args:
        input_path: Input frame image.
        output_path: Output cropped/scaled image.
        crop_region: (x, y, width, height) or None for full frame.
        target_size: (width, height) to scale to.
        timeout: Process timeout in seconds.

    Returns:
        CroppedFrame with metadata.
    """
```

#### 2c) Stil-Filter

```python
def apply_style_filter(
    input_path: str | Path,
    output_path: str | Path,
    filter_preset: str = "news_recolor",
    timeout: int = 30,
) -> Path:
    """Apply visual style modification to a frame via ffmpeg filters.

    Available presets:
        "news_recolor": hue shift + saturation boost + vintage curves + vignette
        "warm_tint": warm color temperature shift
        "cool_tint": cool/blue color temperature shift
        "sketch": edge detection + reduced saturation (artistic)

    ffmpeg command (news_recolor):
        ffmpeg -i {input} -filter:v
            "hue=h=15:s=1.1,curves=vintage,vignette=PI/4,eq=contrast=1.05"
            {output}

    Args:
        input_path: Input frame image.
        output_path: Output styled image.
        filter_preset: Name of the filter preset.
        timeout: Process timeout in seconds.

    Returns:
        Path to the styled output image.
    """
```

**Filter-Presets als Dict im Modul:**
```python
_STYLE_FILTER_PRESETS = {
    "news_recolor": "hue=h=15:s=1.1,curves=vintage,vignette=PI/4,eq=contrast=1.05",
    "warm_tint": "colortemperature=temperature=6500,eq=saturation=1.1",
    "cool_tint": "colortemperature=temperature=4500,eq=saturation=0.9:contrast=1.05",
    "sketch": "edgedetect=mode=colormix:high=0.1,eq=saturation=0.3",
}
```

---

## Phase 3: Frame-Extraction-Service (Anchor-Erkennung)

### Neue Datei: `btcedu/services/frame_extraction_service.py`

Service-Layer mit Protocol (wie andere Services):

```python
"""Frame extraction and anchor detection service using OpenCV."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class FrameAnalyzer(Protocol):
    """Protocol for frame analysis (anchor detection)."""

    def detect_anchor(self, frame_path: str | Path) -> AnchorDetection:
        """Analyze a frame and detect if a news anchor is present."""
        ...

    def compute_crop_region(
        self, frame_path: str | Path, detection: AnchorDetection
    ) -> tuple[int, int, int, int] | None:
        """Compute optimal crop region to exclude anchor. Returns (x, y, w, h) or None."""
        ...


@dataclass
class AnchorDetection:
    """Result of anchor detection on a single frame."""
    has_anchor: bool
    confidence: float           # 0.0-1.0
    face_region: tuple[int, int, int, int] | None  # (x, y, w, h) of detected face
    frame_width: int
    frame_height: int


@dataclass
class FrameCandidate:
    """A processed frame candidate ready for chapter assignment."""
    frame_path: Path            # Pfad zum finalen (gecroppt + gestylt) Frame
    original_path: Path         # Pfad zum Original-Frame
    timestamp_seconds: float    # Position im Quellvideo
    scene_score: float          # Scene-Change-Score
    has_anchor: bool            # Wurde ein Sprecher erkannt?
    was_cropped: bool           # Wurde der Frame gecroppt?
    style_applied: str          # Welcher Stil-Filter ("ffmpeg:news_recolor", "none", etc.)


class OpenCVFrameAnalyzer:
    """Anchor detection using OpenCV Haar cascades.

    Lightweight, works on Raspberry Pi without GPU.
    Uses frontal face detection + position heuristics
    (news anchors typically in lower-center of frame).
    """

    def __init__(self):
        import cv2
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )

    def detect_anchor(self, frame_path: str | Path) -> AnchorDetection:
        """Detect news anchor using face detection + position heuristics.

        Heuristic: A face is considered an "anchor" if:
        1. Face is in the lower 40% of the frame vertically
        2. Face width is > 8% of frame width (not too small/far away)
        3. Face is roughly centered horizontally (center 60% of frame)

        These thresholds match typical Tagesschau anchor positioning.
        """
        import cv2

        img = cv2.imread(str(frame_path))
        if img is None:
            return AnchorDetection(False, 0.0, None, 0, 0)

        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(w // 12, w // 12)
        )

        for (fx, fy, fw, fh) in faces:
            face_center_y = fy + fh / 2
            face_center_x = fx + fw / 2
            in_lower_half = face_center_y > h * 0.6
            is_large_enough = fw > w * 0.08
            is_centered = 0.2 * w < face_center_x < 0.8 * w

            if in_lower_half and is_large_enough and is_centered:
                return AnchorDetection(
                    has_anchor=True,
                    confidence=0.85,
                    face_region=(int(fx), int(fy), int(fw), int(fh)),
                    frame_width=w,
                    frame_height=h,
                )

        return AnchorDetection(False, 0.0, None, w, h)

    def compute_crop_region(
        self, frame_path: str | Path, detection: AnchorDetection
    ) -> tuple[int, int, int, int] | None:
        """Compute crop to exclude anchor, keeping maximum useful area.

        For Tagesschau: crop to upper 60-65% of frame (above anchor).
        Returns (x, y, width, height) or None if no crop needed.
        """
        if not detection.has_anchor or detection.face_region is None:
            return None  # No crop needed

        _, fy, _, _ = detection.face_region
        w = detection.frame_width
        h = detection.frame_height

        # Crop to area above the face, with small margin
        crop_height = max(int(fy * 0.95), h // 3)  # At least upper third
        return (0, 0, w, crop_height)
```

**Neue Dependency in `pyproject.toml`:**
```toml
[project.optional-dependencies]
frames = ["opencv-python-headless>=4.8"]
```

`opencv-python-headless` (ohne GUI) ist ~40MB auf ARM, deutlich leichter als `opencv-python`.

---

## Phase 4: Core Frame-Extractor Stage

### Neue Datei: `btcedu/core/frame_extractor.py`

Folgt exakt dem bestehenden Stage-Pattern (wie `tts.py`, `stock_images.py`):

```python
"""Frame extraction from source video: keyframe detection, anchor removal, styling."""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class FrameExtractionResult:
    """Summary of frame extraction for one episode."""
    episode_id: str
    frames_dir: Path
    manifest_path: Path
    provenance_path: Path
    total_frames: int = 0
    anchor_frames: int = 0      # Frames with detected anchor
    cropped_frames: int = 0     # Frames that were cropped
    styled_frames: int = 0      # Frames with style filter applied
    assigned_frames: int = 0    # Frames assigned to chapters
    cost_usd: float = 0.0       # Only if API style modification used
    skipped: bool = False


def extract_frames(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> FrameExtractionResult:
    """Extract and process frames from source video for an episode.

    Stage flow:
    1. Load video path from download metadata
    2. Extract keyframes via ffmpeg scene detection
    3. Analyze each frame for anchor presence (OpenCV)
    4. Crop anchor frames (keep background graphics)
    5. Apply style filter (ffmpeg default or DALL-E API)
    6. Assign frames to chapters based on timeline
    7. Write frames manifest + provenance

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Pipeline settings.
        force: If True, re-extract even if current.

    Returns:
        FrameExtractionResult with paths and counts.
    """
```

### Stage-Ablauf im Detail

```
1. VALIDATION
   - Episode exists, pipeline_version == 2
   - Status == CHAPTERIZED (oder FRAMES_EXTRACTED wenn force)
   - Video-Datei existiert (downloaded in Phase 1)

2. IDEMPOTENCY CHECK
   - Hash des Quellvideos (Dateigröße + mtime als Proxy, nicht voller SHA-256)
   - Provenance-Check: frames_provenance.json
   - .stale Marker Check

3. KEYFRAME EXTRACTION
   - ffmpeg_service.extract_keyframes(video_path, frames_dir, threshold=0.3)
   - Typisch: 30-80 Frames für eine 15-min Tagesschau

4. ANCHOR DETECTION + CROP
   - Für jeden Frame: analyzer.detect_anchor(frame_path)
   - Wenn Anchor: ffmpeg_service.crop_frame(frame_path, crop_region)
   - Wenn kein Anchor: Frame unverändert übernehmen

5. STYLE MODIFICATION
   - Default: ffmpeg_service.apply_style_filter(frame_path, "news_recolor")
   - Optional: DALL-E Edit API (wenn settings.style_modification_provider == "dalle_edit")
   - Cost Guard prüfen vor API-Calls

6. CHAPTER ASSIGNMENT (Timeline-Mapping)
   - Lade chapters.json + stories.json (für Timeline)
   - Jeder Chapter hat estimated_duration_seconds
   - Kumulative Timeline berechnen: Chapter 1 = 0-30s, Chapter 2 = 30-55s, etc.
   - Frames dem Chapter zuordnen, dessen Zeitfenster den Frame-Timestamp enthält
   - Pro Chapter: besten Frame auswählen (höchster scene_score, kein Anchor)

7. OUTPUT
   - frames/manifest.json (Frame-Kandidaten pro Chapter)
   - frames/provenance/frames_provenance.json
   - Status → FRAMES_EXTRACTED (neuer EpisodeStatus)
   - Cascade: .stale Marker für images/ Verzeichnis
```

### Frame-Manifest Schema

```json
{
  "episode_id": "ep_2024_01_15",
  "schema_version": "1.0",
  "source_video": "data/raw/ep_2024_01_15/video.mp4",
  "extraction_params": {
    "scene_threshold": 0.3,
    "style_filter": "ffmpeg:news_recolor",
    "anchor_detection": "opencv_haar"
  },
  "total_frames_extracted": 45,
  "total_frames_with_anchor": 12,
  "chapter_assignments": [
    {
      "chapter_id": "ch01",
      "assigned_frame": "frames/styled/frame_0003.png",
      "timestamp_seconds": 5.2,
      "scene_score": 0.72,
      "has_anchor": false,
      "was_cropped": false,
      "alternative_frames": [
        "frames/styled/frame_0004.png",
        "frames/styled/frame_0005.png"
      ]
    }
  ],
  "unassigned_frames": ["frames/styled/frame_0042.png"]
}
```

---

## Phase 5: Pipeline-Integration

### 5a) Neuer EpisodeStatus

**Datei: `btcedu/models/episode.py`**

```python
class EpisodeStatus(str, enum.Enum):
    # ... existing ...
    CHAPTERIZED = "chapterized"
    FRAMES_EXTRACTED = "frames_extracted"  # NEU
    IMAGES_GENERATED = "images_generated"
    # ...
```

```python
class PipelineStage(str, enum.Enum):
    # ... existing ...
    CHAPTERIZE = "chapterize"
    FRAMEEXTRACT = "frameextract"  # NEU
    IMAGEGEN = "imagegen"
    # ...
```

### 5b) Pipeline-Stages anpassen

**Datei: `btcedu/core/pipeline.py`**

```python
_STATUS_ORDER = {
    # ... existing ...
    EpisodeStatus.CHAPTERIZED: 13,
    EpisodeStatus.FRAMES_EXTRACTED: 13.5,  # NEU: zwischen CHAPTERIZED und IMAGES_GENERATED
    EpisodeStatus.IMAGES_GENERATED: 14,
    # ...
}

_V2_STAGES = [
    # ... existing bis chapterize ...
    ("chapterize", EpisodeStatus.ADAPTED),
    ("frameextract", EpisodeStatus.CHAPTERIZED),      # NEU
    ("imagegen", EpisodeStatus.FRAMES_EXTRACTED),      # GEÄNDERT: war CHAPTERIZED
    ("review_gate_stock", EpisodeStatus.FRAMES_EXTRACTED),  # GEÄNDERT
    ("tts", EpisodeStatus.IMAGES_GENERATED),
    # ... rest unchanged ...
]
```

**In `_run_stage()`:**
```python
elif stage_name == "frameextract":
    from btcedu.core.frame_extractor import extract_frames
    result = extract_frames(session, episode.episode_id, settings, force=force)
    elapsed = time.monotonic() - t0
    if result.skipped:
        return StageResult("frameextract", "skipped", elapsed, "frames current")
    detail = (
        f"Extracted {result.total_frames} frames, "
        f"{result.assigned_frames} assigned to chapters"
    )
    if result.cost_usd > 0:
        detail += f" (${result.cost_usd:.4f})"
    return StageResult("frameextract", "success", elapsed, detail)
```

### 5c) Imagegen-Stage anpassen

**Datei: `btcedu/core/image_generator.py` (oder `stock_images.py`)**

Der bestehende `imagegen`-Stage muss die Frame-Kandidaten als **bevorzugte Quelle** nutzen:

```python
def _get_image_for_chapter(chapter, episode_id, settings, ...):
    """Get best image for a chapter, trying sources in priority order.

    Priority:
    1. Extracted frame (from frames/manifest.json) — kostenlos, authentisch
    2. Pexels stock photo/video — kostenlos (API), generisch
    3. DALL-E generation — ~$0.04/Bild, custom aber teuer
    """
    # 1. Check extracted frames
    frames_manifest = _load_frames_manifest(episode_id, settings)
    if frames_manifest:
        frame = _get_frame_for_chapter(frames_manifest, chapter.chapter_id)
        if frame:
            return ImageEntry(
                chapter_id=chapter.chapter_id,
                visual_type=chapter.visual.type.value,
                file_path=frame["assigned_frame"],
                generation_method="frame_extraction",
                # ...
            )

    # 2. Fallback to existing stock/DALL-E logic
    # ... (existing code unchanged) ...
```

**Wichtig:** Die `generation_method` im Manifest-Eintrag wird `"frame_extraction"`.
Der Renderer behandelt dies identisch zu `"pexels_photo"` (statisches Bild → Segment).

### 5d) Conditional Stage (Feature Flag)

Wenn `frame_extraction_enabled = False` (default initially), wird der Stage übersprungen:

```python
# In _get_stages():
if not _should_include_frameextract(settings, episode):
    stages = [(s, st) for s, st in stages if s != "frameextract"]
    # Restore imagegen required status to CHAPTERIZED
    stages = [
        (s, EpisodeStatus.CHAPTERIZED if s == "imagegen" and st == EpisodeStatus.FRAMES_EXTRACTED else st)
        for s, st in stages
    ]
```

Alternativ (einfacher): In `_run_stage()` prüfen und sofort "skipped" returnen:

```python
elif stage_name == "frameextract":
    if not settings.frame_extraction_enabled:
        episode.status = EpisodeStatus.FRAMES_EXTRACTED
        session.commit()
        return StageResult("frameextract", "skipped", 0, "frame extraction disabled")
    # ... actual extraction ...
```

---

## Phase 6: Config-Erweiterungen

### Datei: `btcedu/config.py`

```python
class Settings(BaseSettings):
    # ... existing ...

    # Frame Extraction (Phase: Frame-Extraction-Pipeline)
    frame_extraction_enabled: bool = False  # Feature flag, default off
    frame_extract_video_height: int = 720   # Max video download height
    frame_extract_scene_threshold: float = 0.3  # Scene detection sensitivity (0-1)
    frame_extract_min_interval: float = 2.0  # Min seconds between frames
    frame_extract_max_frames: int = 100  # Max frames to extract per video
    frame_extract_style_preset: str = "news_recolor"  # ffmpeg filter preset
    frame_extract_style_provider: str = "ffmpeg"  # "ffmpeg" or "dalle_edit"
    frame_extract_anchor_detection: bool = True  # Enable OpenCV anchor detection
    frame_extract_crop_anchor: bool = True  # Crop frames with detected anchor
```

---

## Phase 7: Migration

### Datei: `btcedu/migrations/__init__.py`

Neue Migration (Migration 8) hinzufügen:

```python
class Migration0008AddFramesExtractedStatus(Migration):
    """Add FRAMES_EXTRACTED status support.

    Since EpisodeStatus is a Python enum and SQLite stores strings,
    no actual DB migration is needed for the new enum value.
    However, we document it here for tracking and add any needed indexes.
    """
    version = 8

    def check(self, engine) -> bool:
        # Check if any episode has the new status (migration already applied)
        # Or check a migration marker
        ...

    def apply(self, engine) -> None:
        # SQLite string columns accept new enum values without ALTER TABLE.
        # This migration is mainly a documentation marker.
        pass
```

**Hinweis:** Da SQLite Strings für Enums speichert und SQLAlchemy `Enum` mit
`native_enum=False` (Standard für SQLite), braucht es keine tatsächliche
Schema-Migration. Der neue Status-Wert funktioniert automatisch.

---

## Phase 8: Fallback-Strategie

### Mehrstufige Fallbacks

```
1. Video nicht verfügbar → Skip frameextract, imagegen nutzt Stock/DALL-E
   (episode.status wird auf FRAMES_EXTRACTED gesetzt mit leerer Manifest)

2. ffmpeg scene detection schlägt fehl → Gleichmäßig verteilte Frames extrahieren
   (Fallback: 1 Frame pro N Sekunden statt scene detection)

3. OpenCV nicht installiert → Anchor-Detection überspringen, Vollframes verwenden
   (try/except ImportError auf cv2)

4. Kein Frame für einen Chapter → imagegen fällt auf Stock/DALL-E zurück
   (pro-Chapter Fallback, nicht global)

5. Stil-API (DALL-E Edit) schlägt fehl → ffmpeg-Filter als Fallback
   (try/except auf API-Call)
```

### Implementierung im imagegen-Stage:

```python
# Pseudo-Code für die Auswahl-Logik:
for chapter in chapters:
    image = None

    # 1. Try extracted frame
    if frames_manifest and chapter.chapter_id in frame_assignments:
        frame_path = resolve_frame_path(frame_assignments[chapter.chapter_id])
        if frame_path.exists():
            image = create_image_entry_from_frame(frame_path, chapter)

    # 2. Try stock if no frame or visual type doesn't match
    if image is None and settings.pexels_api_key:
        image = search_and_download_stock(chapter, settings)

    # 3. Try DALL-E generation
    if image is None and settings.openai_api_key:
        image = generate_dalle_image(chapter, settings)

    # 4. Placeholder as last resort
    if image is None:
        image = create_placeholder(chapter)
```

---

## Phase 9: Test-Strategie

### Neue Test-Dateien

#### `tests/test_frame_extractor.py` (~300 Zeilen)

```python
"""Tests for frame extraction pipeline stage."""

# Fixtures:
# - In-memory SQLite mit Episode in CHAPTERIZED Status
# - Fake video_meta.json mit Video-Pfad
# - Fake chapters.json
# - Mock für ffmpeg_service (extract_keyframes, crop_frame, apply_style_filter)
# - Mock für frame_extraction_service (OpenCVFrameAnalyzer)

class TestExtractFrames:
    def test_basic_extraction_happy_path(self, session, episode, settings, tmp_path):
        """Full pipeline: extract → detect → crop → style → assign."""

    def test_skips_when_current(self, session, episode, settings, tmp_path):
        """Idempotency: skips if provenance hash matches."""

    def test_force_reextracts(self, session, episode, settings, tmp_path):
        """force=True bypasses idempotency check."""

    def test_no_video_creates_empty_manifest(self, session, episode, settings, tmp_path):
        """Graceful degradation when video file is missing."""

    def test_anchor_detection_disabled(self, session, episode, settings, tmp_path):
        """frame_extract_anchor_detection=False skips OpenCV."""

    def test_chapter_assignment_by_timeline(self, session, episode, settings, tmp_path):
        """Frames are correctly assigned to chapters based on timestamps."""

    def test_cost_guard_with_dalle_edit(self, session, episode, settings, tmp_path):
        """Cost guard triggers when using DALL-E Edit API."""

    def test_cascade_invalidation(self, session, episode, settings, tmp_path):
        """Creates .stale marker for images/ directory."""

    def test_pipeline_run_record(self, session, episode, settings, tmp_path):
        """PipelineRun record created with correct stage and status."""

    def test_disabled_feature_flag(self, session, episode, settings, tmp_path):
        """frame_extraction_enabled=False skips stage."""
```

#### `tests/test_frame_extraction_service.py` (~200 Zeilen)

```python
"""Tests for anchor detection service."""

class TestOpenCVFrameAnalyzer:
    def test_detect_anchor_with_face_in_lower_third(self, tmp_path):
        """Detects anchor when face is in lower-center region."""
        # Create synthetic test image with cv2.rectangle for face region
        # Or use a small test fixture image

    def test_no_anchor_on_graphics_frame(self, tmp_path):
        """No anchor detected on a pure graphics/chart frame."""

    def test_small_face_not_anchor(self, tmp_path):
        """Small faces (crowd shots) are not considered anchors."""

    def test_face_at_top_not_anchor(self, tmp_path):
        """Face at top of frame is not an anchor (interview partner)."""

    def test_compute_crop_region_with_anchor(self):
        """Crop region excludes anchor, keeps upper portion."""

    def test_compute_crop_region_no_anchor(self):
        """Returns None when no anchor present."""

    def test_opencv_import_error_handled(self):
        """Graceful fallback when OpenCV is not installed."""
```

#### `tests/test_ffmpeg_frame_extraction.py` (~150 Zeilen)

```python
"""Tests for ffmpeg frame extraction functions."""

class TestExtractKeyframes:
    def test_extracts_frames_with_scene_detection(self, tmp_path):
        """Mocks subprocess for ffmpeg scene detection."""

    def test_respects_max_frames_limit(self, tmp_path):
        """Stops after max_frames reached."""

    def test_min_interval_filtering(self, tmp_path):
        """Filters frames closer than min_interval_seconds."""

class TestCropFrame:
    def test_crop_and_scale(self, tmp_path):
        """Crops and scales to target resolution."""

class TestApplyStyleFilter:
    def test_news_recolor_preset(self, tmp_path):
        """Applies news_recolor filter chain."""

    def test_invalid_preset_raises(self, tmp_path):
        """Unknown preset name raises ValueError."""
```

#### `tests/test_download_video.py` (~80 Zeilen)

```python
"""Tests for video download functionality."""

class TestDownloadVideo:
    def test_downloads_video_with_ytdlp(self, tmp_path):
        """Mocks subprocess for yt-dlp video download."""

    def test_max_height_parameter(self, tmp_path):
        """Verifies format string includes height limit."""

    def test_missing_ytdlp_raises(self, tmp_path):
        """RuntimeError when yt-dlp not found."""
```

### Mock-Strategie

```python
# OpenCV Mock (für Tests ohne opencv-python-headless installiert):
@pytest.fixture
def mock_cv2(monkeypatch):
    """Mock cv2 module for tests without OpenCV installed."""
    mock_module = MagicMock()
    mock_module.data.haarcascades = "/fake/path/"
    mock_module.CascadeClassifier.return_value = MagicMock()
    mock_module.imread.return_value = np.zeros((1080, 1920, 3), dtype=np.uint8)
    mock_module.cvtColor.return_value = np.zeros((1080, 1920), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_module)
    return mock_module

# ffmpeg Mock (bestehend, erweitern):
@pytest.fixture
def mock_ffmpeg(monkeypatch):
    """Mock ffmpeg subprocess calls."""
    # Ähnlich wie in test_renderer.py und test_stock_video.py
```

---

## Phase 10: Implementierungs-Reihenfolge

```
Sprint A (Foundation):
  1. download_service.py → download_video()
  2. ffmpeg_service.py → extract_keyframes(), crop_frame(), apply_style_filter()
  3. config.py → neue Settings
  4. Tests für Phase 1+2

Sprint B (Core Logic):
  5. frame_extraction_service.py → OpenCVFrameAnalyzer
  6. frame_extractor.py → extract_frames() Stage
  7. episode.py → FRAMES_EXTRACTED Status + PipelineStage
  8. pipeline.py → Stage-Integration
  9. Tests für Phase 3+4+5

Sprint C (Integration):
  10. imagegen anpassen → Frame-Kandidaten als Quelle
  11. Fallback-Logik implementieren
  12. Migration hinzufügen
  13. Integration-Tests

Sprint D (Polish):
  14. DALL-E Edit API Option (optional style provider)
  15. CLI-Kommando: `btcedu extract-frames <episode_id>`
  16. Web-Dashboard: Frame-Preview in Review-Gate
  17. Dokumentation
```

---

## Risiken & Mitigationen

| Risiko | Wahrscheinlichkeit | Mitigation |
|--------|-------------------|------------|
| OpenCV ARM-Performance | Niedrig | Haar Cascades sind CPU-optimiert, ~50ms/Frame |
| Video-Download vergrößert Speicher | Mittel | 720p MP4 ~200MB/Episode, Config für max_height |
| Tagesschau-Layout ändert sich | Niedrig | Heuristik-Parameter konfigurierbar, Fallback auf Stock |
| Scene detection liefert zu wenig Frames | Mittel | Threshold konfigurierbar, Fallback auf gleichmäßige Verteilung |
| Copyright-Bedenken trotz Stil-Filter | Mittel | Hybrid-Ansatz: ffmpeg + optional DALL-E für stärkere Modifikation |
| opencv-python-headless auf Pi | Niedrig | `pip install opencv-python-headless` funktioniert auf ARM64 |

---

## Dateien-Übersicht (Neu/Geändert)

**Neue Dateien:**
- `btcedu/services/frame_extraction_service.py` — Anchor-Erkennung (OpenCV)
- `btcedu/core/frame_extractor.py` — Pipeline-Stage
- `tests/test_frame_extractor.py` — Stage-Tests
- `tests/test_frame_extraction_service.py` — Service-Tests
- `tests/test_ffmpeg_frame_extraction.py` — FFmpeg-Erweiterungs-Tests
- `tests/test_download_video.py` — Video-Download-Tests

**Geänderte Dateien:**
- `btcedu/services/download_service.py` — `download_video()` hinzufügen
- `btcedu/services/ffmpeg_service.py` — 3 neue Funktionen
- `btcedu/config.py` — 9 neue Settings
- `btcedu/models/episode.py` — `FRAMES_EXTRACTED` Status + `FRAMEEXTRACT` Stage
- `btcedu/core/pipeline.py` — Stage-Integration, Status-Order
- `btcedu/core/image_generator.py` oder `btcedu/core/stock_images.py` — Frame-Quelle
- `btcedu/migrations/__init__.py` — Migration 8
- `pyproject.toml` — `[frames]` optional dependency

**Geschätzte Größe:** ~1500 Zeilen neuer Code, ~200 Zeilen geändert, ~750 Zeilen Tests.
