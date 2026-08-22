"""Ingest source for locally recorded broadcasts.

A separate recorder (``ard-recorder``) captures the tagesschau live stream every
evening and publishes it to a directory tree. That file is ready roughly 20
minutes after the broadcast, whereas the YouTube upload of the same broadcast
appears one to two hours later. Reading the local file therefore removes the
single largest delay in the pipeline.

The recorder's output contract, which this module depends on::

    <base_dir>/<YYYY-MM-DD>/tagesschau_<YYYY-MM-DD>_2000.mp4            published
    <base_dir>/<YYYY-MM-DD>/tagesschau_<YYYY-MM-DD>_2000.metadata.json  metadata
    <base_dir>/<YYYY-MM-DD>/tagesschau_<YYYY-MM-DD>_2000.DONE           commit marker
    <base_dir>/<YYYY-MM-DD>/tagesschau_<YYYY-MM-DD>_2000.raw.mp4        untrimmed copy

Two properties of that contract carry the whole design:

* The ``.DONE`` marker is written last and is the recorder's commit point. A
  video without it is either still being written or failed verification, so it
  must never be ingested — reading it would transcribe a truncated broadcast.
* The ``.raw.mp4`` is the untrimmed capture kept as a safety net. It brackets the
  broadcast with the end of the previous programme and the start of the next
  one, so it must be excluded explicitly rather than by a loose ``*.mp4`` glob.

The published ``.mp4`` is already cut to the programme boundaries, so no
additional trimming happens here.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from btcedu.models.schemas import EpisodeInfo

logger = logging.getLogger(__name__)

#: The recorder timestamps in local German time.
_BERLIN = ZoneInfo("Europe/Berlin")

DONE_SUFFIX = ".DONE"
METADATA_SUFFIX = ".metadata.json"

#: The untrimmed safety copy. Never a pipeline input.
RAW_MARKER = ".raw"

#: Directory names the scanner accepts, so a stray directory cannot be read as a
#: broadcast day.
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SOURCE_NAME = "local_recorder"


@dataclass(frozen=True)
class WeatherCheck:
    """The recorder's verdict on whether its cut kept the closing forecast.

    ``present`` is three-valued on purpose. "No forecast" and "could not tell"
    are different facts: the first may be a lost broadcast, the second is only
    a missing transcript. ``truncated`` separates the two ways ``present`` can
    be false — a forecast continuing past the cut means material was lost,
    while an edition that simply had none is nothing anyone can fix.
    """

    present: bool | None
    truncated: bool = False
    evidence: str = ""


@dataclass(frozen=True)
class LocalRecording:
    """One finished recording found on disk."""

    day: date
    slug: str
    video: Path
    metadata: dict

    @property
    def title(self) -> str:
        """Title from the recorder's metadata, or one derived from the date.

        The derived form matches the wording of the YouTube titles this pipeline
        already ingests, so the profile's ``title_include`` filter keeps working
        unchanged for locally sourced episodes.
        """
        title = self.metadata.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        return f"tagesschau 20:00 Uhr, {self.day.strftime('%d.%m.%Y')}"

    @property
    def published_at(self) -> datetime:
        """When the broadcast aired — not when the file happened to be written.

        Anchoring on the scheduled start keeps ordering and retention consistent
        with the YouTube-sourced episodes of the same broadcast.
        """
        scheduled = self.metadata.get("scheduled_start")
        if isinstance(scheduled, str):
            try:
                return datetime.fromisoformat(scheduled)
            except ValueError:
                logger.warning("unparsable scheduled_start %r in %s", scheduled, self.slug)
        return datetime(self.day.year, self.day.month, self.day.day, 20, 0, tzinfo=_BERLIN)

    @property
    def duration_seconds(self) -> int | None:
        value = self.metadata.get("duration_seconds")
        try:
            return int(round(float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @property
    def node_role(self) -> str | None:
        value = self.metadata.get("node_role")
        if value is None:
            extra = self.metadata.get("extra")
            if isinstance(extra, dict):
                value = extra.get("node_role")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return None

    @property
    def source_kind(self) -> str | None:
        value = self.metadata.get("source_kind")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return None

    @property
    def provenance(self):
        value = self.metadata.get("provenance")
        if value not in (None, "", {}, []):
            return value
        extra = self.metadata.get("extra")
        if not isinstance(extra, dict):
            return None
        source = extra.get("source_provenance")
        item_id = extra.get("item_id")
        if source or item_id:
            return {"source": source, "item_id": item_id}
        return None

    @property
    def completion_verified(self) -> bool:
        """Whether the recorder proved both live programme boundaries.

        A technically valid MP4 is not necessarily a complete bulletin. The
        2026-08-20 capture passed duration and stream checks after losing more
        than three minutes, so the recorder now writes an explicit semantic
        verdict. Missing verdicts are deliberately unsafe rather than being
        grandfathered in.
        """
        extra = self.metadata.get("extra")
        if not isinstance(extra, dict):
            return False
        return str(extra.get("completion_verified", "")).lower() == "true"

    @property
    def weather(self) -> WeatherCheck:
        """What the recorder found when it checked its own cut.

        The recorder transcribes the tail around the cut and reports whether the
        closing forecast is inside the published file. Reading it here is what
        turns that check into something a person sees: the forecast is a chapter
        of the finished video, and a cut that ate it produces a video that is
        wrong in a way no later stage can detect — the transcript is coherent,
        every gate passes, and the weather is simply gone.
        """
        extra = self.metadata.get("extra")
        if not isinstance(extra, dict):
            return WeatherCheck(None)
        state = extra.get("weather_verified")
        present = {"true": True, "false": False}.get(str(state).lower()) if state else None
        # A truncated cut has no evidence *before* it — that is what truncated
        # means — so the recorder names what it found after it instead. Reading
        # only ``weather_evidence`` left the alarm without the one detail that
        # makes it actionable: where the lost forecast starts in the capture.
        evidence = str(extra.get("weather_evidence") or extra.get("weather_after_cut") or "")
        return WeatherCheck(
            present=present,
            truncated=str(extra.get("weather_truncated", "")).lower() == "true",
            evidence=evidence,
        )

    def to_episode_info(self) -> EpisodeInfo:
        """Adapt to the shape the detector already consumes.

        ``url`` carries a filesystem path rather than an HTTP URL. That is what
        ``source`` distinguishes, and every consumer that would otherwise try to
        download it checks the source first.
        """
        return EpisodeInfo(
            episode_id=self.slug,
            title=self.title,
            published_at=self.published_at,
            url=str(self.video),
            source=SOURCE_NAME,
        )


def _read_metadata(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cannot read recorder metadata %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def metadata_for_video(path: str | Path) -> dict:
    """Read the recorder metadata that sits next to *path*, if present."""
    video = Path(path)
    metadata_path = video.with_name(f"{video.stem}{METADATA_SUFFIX}")
    if not metadata_path.is_file():
        return {}
    return _read_metadata(metadata_path)


def _recording_from_marker(done: Path) -> LocalRecording | None:
    """Build a recording from its ``.DONE`` marker, or ``None`` if unusable."""
    slug = done.name[: -len(DONE_SUFFIX)]
    if slug.endswith(RAW_MARKER):
        return None

    directory = done.parent
    try:
        day = date.fromisoformat(directory.name)
    except ValueError:
        logger.warning("recorder directory %s is not a broadcast date", directory)
        return None

    video = directory / f"{slug}.mp4"
    if not video.is_file():
        logger.warning("no video next to completion marker %s", done)
        return None
    if video.stat().st_size == 0:
        logger.warning("recorder produced an empty file: %s", video)
        return None

    metadata_file = directory / f"{slug}{METADATA_SUFFIX}"
    metadata = _read_metadata(metadata_file) if metadata_file.is_file() else {}
    recording = LocalRecording(day=day, slug=slug, video=video, metadata=metadata)
    if not recording.completion_verified:
        logger.error(
            "recorder output %s is not completion-verified; ignoring local source",
            slug,
        )
        return None
    return recording


def scan_recordings(base_dir: str | Path, *, since: date | None = None) -> list[LocalRecording]:
    """Every finished recording under *base_dir*, newest first.

    Only recordings carrying a ``.DONE`` marker are returned; anything else is
    still in flight. A missing base directory is not an error — it simply means
    the recorder is not installed, and the caller falls back to YouTube.

    Args:
        base_dir: Root of the recorder's output tree.
        since: Ignore broadcasts older than this date (retention).
    """
    root = Path(base_dir)
    if not root.is_dir():
        logger.debug("recorder directory %s does not exist", root)
        return []

    recordings: list[LocalRecording] = []
    for day_dir in sorted(root.iterdir(), reverse=True):
        if not day_dir.is_dir() or not _DAY_DIR.match(day_dir.name):
            continue
        for done in sorted(day_dir.glob(f"*{DONE_SUFFIX}")):
            recording = _recording_from_marker(done)
            if recording is None:
                continue
            if since is not None and recording.day < since:
                continue
            recordings.append(recording)

    recordings.sort(key=lambda r: (r.day, r.slug), reverse=True)
    return recordings


def find_recording(base_dir: str | Path, day: date) -> LocalRecording | None:
    """The finished recording for one broadcast day, if the recorder has it."""
    root = Path(base_dir) / day.isoformat()
    if not root.is_dir():
        return None
    for done in sorted(root.glob(f"*{DONE_SUFFIX}")):
        recording = _recording_from_marker(done)
        if recording is not None:
            return recording
    return None


def is_local_source(source: str | None) -> bool:
    """True for episodes that came from the recorder rather than a feed."""
    return source == SOURCE_NAME


def extract_audio(
    video: str | Path,
    destination: str | Path,
    *,
    audio_format: str = "m4a",
    timeout: int = 1800,
    force: bool = False,
) -> Path:
    """Extract the audio track of a recording into *destination*.

    The recorder stores AAC audio in an MP4 container, so extracting to ``m4a``
    is a stream copy: no re-encoding, a few seconds on a Raspberry Pi, and no
    generational quality loss before transcription. Any other target format
    falls back to re-encoding.

    ``force`` re-extracts over an existing file. The reuse shortcut is keyed on
    the file merely existing, so without it a corrected recording is invisible:
    the recording is replaced on disk, the pipeline is asked to run again, and
    it transcribes the audio of the old cut regardless. Re-extraction costs a
    few seconds of stream copy, which is the cheapest stage there is.

    Returns the path to the extracted audio.
    """
    source_path = Path(video)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and target.stat().st_size > 0 and not force:
        logger.info("audio already extracted: %s", target)
        return target

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to ingest local recordings but was not found")

    # A temporary name keeps a crashed run from leaving a half-written file that
    # a later run would mistake for a finished extraction.
    partial = target.with_name(f"{target.name}.partial")
    partial.unlink(missing_ok=True)

    codec = ["-c:a", "copy"] if audio_format == "m4a" else []
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        *codec,
        "-f",
        _container_for(audio_format),
        str(partial),
    ]

    logger.info("Extracting audio: %s -> %s", source_path, target)
    result = subprocess.run(  # noqa: S603 — argument list, no shell
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg failed to extract audio from {source_path} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )

    partial.replace(target)
    return target


def _container_for(audio_format: str) -> str:
    """Map an audio extension to the ffmpeg muxer name."""
    return {"m4a": "ipod", "mp3": "mp3", "wav": "wav", "opus": "opus", "flac": "flac"}.get(
        audio_format, audio_format
    )


def resolve_local_video(url: str, base_dir: str | Path) -> Path:
    """Validate and return the video path stored on a local episode.

    ``url`` on a local episode is a filesystem path, so it is confined to
    *base_dir*: a path from the database must never be able to point the
    pipeline at an arbitrary file.
    """
    root = Path(base_dir).resolve()
    candidate = Path(url).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"local recording {candidate} is outside {root}")
    if candidate.name.endswith(f"{RAW_MARKER}.mp4"):
        raise ValueError(f"refusing the untrimmed capture {candidate.name}")
    if not candidate.is_file():
        raise FileNotFoundError(f"local recording has disappeared: {candidate}")
    return candidate
