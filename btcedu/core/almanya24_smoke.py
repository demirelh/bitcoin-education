"""A whole ALMANYA24 bulletin, synthesised, so the wiring can be run for free.

This module builds a complete, self-contained episode — studio, media, narration,
look pool, consent record and profile — and then drives it through the *real*
pipeline from ``sceneplan`` to ``publish``. Every external system is replaced by
a local double: HeyGen, YouTube and the remote render transport. Nothing here
opens a socket, and nothing here costs anything.

Why it exists: the avatar pipeline is now eleven stages of interlocking
promises — one look per episode, one clip per scene, two review gates, a
fail-closed renderer — and none of those promises can be checked by a unit test
that only ever sees one of them. The parts have been correct for a while; what
was never demonstrated is that they are correct *together*.

What it is not: a rehearsal of the real HeyGen pilot. The clips here are flat
colour, the studio is a rectangle, and the presenter is a red block with an
alpha channel. Everything about *structure, ordering, hashes, money and
refusals* is real; everything about *how it looks* is not, and no claim about
picture quality may be derived from these fixtures.

The assets carry a deliberate marker (``SMOKE-``) so they can never be mistaken
for the phase-1 artwork: the real profile still holds placeholder look IDs, and
``btcedu anchor-readiness`` continues to refuse it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from btcedu.core.studio_manifest import ALPHA_MODE_OPAQUE, ALPHA_MODE_WEBM

#: Prefix on every synthetic identifier. Grep-able, and impossible to confuse
#: with a real HeyGen look id.
SMOKE_MARKER = "SMOKE"
SMOKE_PROFILE = "almanya24_smoke"
SMOKE_EPISODE_ID = "ep_smoke_almanya24"

#: Small on purpose. A 1920x1080 fixture would make the ffmpeg pass take
#: minutes for no additional truth; the geometry is proportional and every
#: assertion is written in terms of the configured size.
WIDTH, HEIGHT, FPS = 320, 180, 25

#: Five synthetic outfits, so rotation has something to choose between and
#: "the same look was kept" is a statement with content.
LOOK_IDS = [f"{SMOKE_MARKER}-LOOK-{index:02d}" for index in range(1, 6)]

#: Roles as the chapterizer records them.
ROLE_ANCHOR = "anchor_female"
ROLE_REPORTER = "reporter_male"


class SmokeError(RuntimeError):
    """The synthetic run could not be prepared or did not behave as promised."""


def ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_binary() -> str | None:
    return shutil.which("ffprobe")


def _run(cmd: list[str], *, timeout: int = 300) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise SmokeError(f"fixture command failed: {' '.join(cmd[:6])}…\n{result.stderr[-800:]}")


def probe(path: Path) -> dict:
    """ffprobe as a dict, for the media assertions."""
    binary = ffprobe_binary()
    if binary is None:
        raise SmokeError("ffprobe is not installed")
    result = subprocess.run(
        [binary, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The bulletin's structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One chapter of the synthetic bulletin."""

    chapter_id: str
    title: str
    roles: tuple[str, ...]
    purpose: str = "news"
    is_weather: bool = False
    #: Seconds of narration per speaker block. Deliberately short.
    part_seconds: tuple[float, ...] = ()

    @property
    def durations(self) -> tuple[float, ...]:
        return self.part_seconds or tuple(0.6 for _ in self.roles)


def bulletin_sections() -> list[Section]:
    """Eleven sections shaped like a real ALMANYA24 broadcast.

    The point of the shape is the handovers, not the word count: an opening,
    a headline block, an anchor→reporter, an anchor→reporter→anchor, a bare
    reporter continuation, the weather handover and a closing are exactly the
    transitions where the scene planner, the avatar ledger and the compositor
    can each disagree with one another.
    """
    return [
        Section("ch_01", "Açılış", (ROLE_ANCHOR,), purpose="opening", part_seconds=(0.8,)),
        Section("ch_02", "Manşetler", (ROLE_ANCHOR,), purpose="headlines", part_seconds=(0.9,)),
        Section("ch_03", "Avrupa", (ROLE_ANCHOR, ROLE_REPORTER), part_seconds=(0.5, 0.7)),
        Section(
            "ch_04",
            "Ekonomi",
            (ROLE_ANCHOR, ROLE_REPORTER, ROLE_ANCHOR),
            part_seconds=(0.5, 0.6, 0.4),
        ),
        Section("ch_05", "Devam", (ROLE_REPORTER,), part_seconds=(0.8,)),
        Section("ch_06", "Almanya", (ROLE_ANCHOR,), part_seconds=(0.7,)),
        Section("ch_07", "Dünya", (ROLE_ANCHOR, ROLE_REPORTER), part_seconds=(0.5, 0.6)),
        Section("ch_08", "Kültür", (ROLE_ANCHOR,), part_seconds=(0.6,)),
        Section("ch_09", "Spor", (ROLE_REPORTER,), part_seconds=(0.7,)),
        Section(
            "ch_10",
            "Hava durumu",
            (ROLE_ANCHOR, ROLE_REPORTER),
            purpose="weather",
            is_weather=True,
            part_seconds=(0.4, 0.8),
        ),
        Section("ch_11", "Kapanış", (ROLE_ANCHOR,), purpose="closing", part_seconds=(0.7,)),
    ]


# ---------------------------------------------------------------------------
# The synthetic world
# ---------------------------------------------------------------------------


@dataclass
class SmokeWorld:
    """Everything one synthetic run needs, on disk and in a private database."""

    root: Path
    outputs_dir: Path
    episode_dir: Path
    studio_dir: Path
    profiles_dir: Path
    rights_file: Path
    settings: Any
    session: Any
    engine: Any
    episode_id: str = SMOKE_EPISODE_ID
    sections: list[Section] = field(default_factory=bulletin_sections)
    real_media: bool = False
    alpha_mode: str = ALPHA_MODE_OPAQUE

    def close(self) -> None:
        try:
            self.session.close()
        finally:
            self.engine.dispose()

    # -- convenience readers used by tests and by the CLI ------------------

    def read_json(self, relative: str) -> dict:
        return json.loads((self.episode_dir / relative).read_text(encoding="utf-8"))

    @property
    def scene_plan(self) -> dict:
        return self.read_json("scene_plan.json")

    @property
    def anchor_manifest(self) -> dict:
        return self.read_json("anchor/manifest.json")

    @property
    def render_manifest(self) -> dict:
        return self.read_json("render/render_manifest.json")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _colour_png(path: Path, colour: str, width: int, height: int) -> None:
    binary = ffmpeg_binary()
    if binary is None:
        raise SmokeError("ffmpeg is required to build the synthetic assets")
    _run([binary, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={width}x{height}",
          "-frames:v", "1", str(path)])


def _tone_mp3(path: Path, seconds: float, frequency: int) -> None:
    binary = ffmpeg_binary()
    if binary is None:
        raise SmokeError("ffmpeg is required to build the synthetic assets")
    _run([binary, "-v", "error", "-y", "-f", "lavfi",
          "-i", f"sine=frequency={frequency}:duration={seconds:.3f}",
          "-ar", "44100", "-ac", "1", str(path)])


def _concat_mp3(path: Path, parts: list[Path], gap_seconds: float) -> None:
    """Glue the speaker parts into the chapter MP3 the renderer will use."""
    binary = ffmpeg_binary()
    if binary is None:
        raise SmokeError("ffmpeg is required to build the synthetic assets")
    inputs: list[str] = []
    labels: list[str] = []
    index = 0
    for position, part in enumerate(parts):
        inputs += ["-i", str(part)]
        labels.append(f"[{index}:a]")
        index += 1
        if position != len(parts) - 1:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={gap_seconds}"]
            labels.append(f"[{index}:a]")
            index += 1
    filters = "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    _run([binary, "-v", "error", "-y", *inputs, "-filter_complex", filters,
          "-map", "[out]", "-ar", "44100", "-ac", "1", str(path)])


def _detailed_png(path: Path, width: int, height: int) -> None:
    """A picture with enough structure to survive a blank-frame check."""
    binary = ffmpeg_binary()
    if binary is None:
        raise SmokeError("ffmpeg is not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run([binary, "-v", "error", "-y", "-f", "lavfi",
          "-i", f"testsrc=size={width}x{height}:rate=1", "-frames:v", "1", str(path)])


def _stub(path: Path, payload: bytes = b"synthetic-fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


#: The pause TTS already leaves between two speaker parts. The chapter MP3
#: contains it once; nothing downstream may add it a second time.
PART_GAP_SECONDS = 0.2


def build_studio(
    studio_dir: Path, *, real_media: bool, alpha_mode: str = ALPHA_MODE_OPAQUE
) -> dict:
    """A studio that is a file, sized for a fast test rather than for air.

    ``alpha_mode`` defaults to the opaque fallback, not to the preferred
    transparent WebM, and that is a property of the machine rather than of the
    design: no ffmpeg build available here can *encode* a WebM whose alpha
    channel ffprobe will report, so an alpha fixture could only ever be a clip
    that the downloader is right to reject. The transparent path is therefore
    covered by its refusals — an opaque clip offered to an alpha job — and by
    passing ``alpha_mode="alpha_webm"`` in the structural tests that never
    reach ffmpeg. Nothing here should be read as evidence that the WebM route
    has been exercised end to end; only the real pilot can show that.
    """
    (studio_dir / "plate").mkdir(parents=True, exist_ok=True)
    (studio_dir / "fallback").mkdir(parents=True, exist_ok=True)

    if real_media:
        for name, colour in (("bg", "navy"), ("wide", "teal"), ("loop", "purple")):
            _colour_png(studio_dir / "plate" / f"{name}.png", colour, WIDTH, HEIGHT)
        _colour_png(studio_dir / "plate" / "desk.png", "black", WIDTH, 40)
        _colour_png(studio_dir / "fallback" / "neutral.png", "gray", 200, 100)
        _colour_png(studio_dir / "fallback" / "logo.png", "white", 50, 20)
    else:
        for relative in ("plate/bg.png", "plate/wide.png", "plate/loop.png", "plate/desk.png",
                         "fallback/neutral.png", "fallback/logo.png"):
            _stub(studio_dir / relative)

    manifest = {
        "schema_version": 1,
        "studio_version": f"{SMOKE_MARKER}-1.0.0",
        "asset_version": f"{SMOKE_MARKER}-1.0.0",
        "name": "ALMANYA24 synthetic studio (test fixture, not for air)",
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "alpha_mode": alpha_mode,
        "background": {"path": "plate/bg.png", "kind": "image"},
        "intro_asset": {"path": "plate/wide.png", "kind": "image"},
        "loop_asset": {"path": "plate/loop.png", "kind": "image"},
        "display_zone": {
            "zone_id": "main_wall",
            # Right of the presenter and clear of her, so "the monitor is not
            # empty" can be read off a frame without the presenter covering it.
            "rect": {"x": 160, "y": 10, "width": 140, "height": 79},
            "fit_mode": "cover",
            "focus_point": [0.5, 0.5],
            "presenter_free": True,
        },
        "presenter": {"anchor_x": 70, "anchor_y": HEIGHT, "scale": 1.0},
        "fallback_display_media": {"path": "fallback/neutral.png", "kind": "image"},
        "logo_zone": {"x": 260, "y": 5, "width": 50, "height": 20},
        "safe_areas": {
            "lower_third": {"x": 20, "y": 130, "width": 200, "height": 22},
            "ticker": {"x": 0, "y": 160, "width": WIDTH, "height": 20},
            "subtitle": {"x": 40, "y": 152, "width": 240, "height": 8},
        },
    }
    _write_json(studio_dir / "manifest.json", manifest)
    return manifest


def build_rights_record(path: Path, *, today: date | None = None) -> dict:
    """A consent record that is valid today and obviously synthetic."""
    today = today or datetime.now(UTC).date()
    record = {
        "schema_version": 1,
        "record_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "presenter_rights_id": f"{SMOKE_MARKER}-presenter-01",
        "consent_confirmed": True,
        "voice_likeness_confirmed": True,
        "synthetic_video_confirmed": True,
        "permitted_channels": ["almanya24-youtube"],
        "permitted_territories": ["DE", "TR"],
        "valid_from": (today - timedelta(days=30)).isoformat(),
        "valid_until": (today + timedelta(days=365)).isoformat(),
        "revoked": False,
        "revoked_on": None,
        "contract_reference": f"{SMOKE_MARKER}-CONTRACT-0001",
        "operator_approval": {
            "approved": True,
            "approved_by_ref": f"{SMOKE_MARKER}-ops",
            "approved_on": today.isoformat(),
        },
        "ai_disclosure_text": "Bu videodaki sunucu yapay zeka ile olusturulmustur.",
    }
    _write_json(path, record)
    return record


def build_profile(
    profiles_dir: Path,
    *,
    studio_dir: Path,
    rights_file: Path,
    look_ids: list[str] | None = None,
    overrides: dict | None = None,
    alpha_mode: str = ALPHA_MODE_OPAQUE,
) -> Path:
    """A copy of the real ALMANYA24 profile with the phase-1 gaps filled in.

    Deliberately a *copy* under a different name. The shipped profile keeps its
    placeholder look IDs and its empty rights record, so ``anchor-readiness``
    still refuses it — a synthetic asset must never make the production profile
    look ready.
    """
    source = Path(__file__).resolve().parent.parent / "profiles" / "tagesschau_tr.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))

    data["name"] = SMOKE_PROFILE
    data["description"] = "SYNTHETIC TEST PROFILE — ALMANYA24 smoke run, never for air"

    anchor = data.setdefault("stage_config", {}).setdefault("anchor", {})
    anchor["looks"] = [
        {"name": f"look_{index + 1:02d}", "avatar_look_id": look_id, "active": True}
        for index, look_id in enumerate(look_ids or LOOK_IDS)
    ]
    anchor["studio"] = {
        "asset_dir": str(studio_dir),
        "manifest": "manifest.json",
        "required_version": 1,
    }
    anchor["rights"] = {
        "consent_documented": True,
        "consent_reference": f"{SMOKE_MARKER}-CONTRACT-0001",
        "permitted_channels": ["almanya24-youtube"],
        "permitted_territories": ["DE"],
        "revoked": False,
        "ai_disclosure_required": True,
        "record_file": str(rights_file),
        "channel": "almanya24-youtube",
        "territory": "DE",
    }
    anchor["review_required"] = True
    # The two halves of one decision: a presenter can only be keyed into the
    # studio if she arrives with an alpha channel, so the opaque fallback also
    # implies the baked studio. Setting one without the other is refused by the
    # profile loader, which is the correct place for it to be refused.
    if alpha_mode == ALPHA_MODE_WEBM:
        anchor["output_format"] = "webm"
        anchor["studio_mode"] = "composite"
    else:
        anchor["output_format"] = "mp4"
        anchor["studio_mode"] = "baked"

    # The shipped profile deliberately refuses to upload by itself. The point of
    # this run is to prove the last stage works, so the copy enables it and
    # sends the result to the private test target — never production.
    data["auto_publish"] = True
    data["auto_approve_reviews"] = False
    youtube = data.setdefault("youtube", {})
    youtube["default_target"] = "test"

    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value

    profiles_dir.mkdir(parents=True, exist_ok=True)
    target = profiles_dir / f"{SMOKE_PROFILE}.yaml"
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def build_episode_files(
    episode_dir: Path,
    sections: list[Section],
    *,
    real_media: bool,
) -> dict:
    """Chapters, narration audio, pictures and one topic video."""
    (episode_dir / "images").mkdir(parents=True, exist_ok=True)
    (episode_dir / "tts" / "parts").mkdir(parents=True, exist_ok=True)
    (episode_dir / "video").mkdir(parents=True, exist_ok=True)

    chapters: list[dict] = []
    tts_segments: list[dict] = []
    images: list[dict] = []
    palette = ["green", "maroon", "olive", "teal", "purple", "navy",
               "darkgreen", "brown", "darkslategray", "indigo", "darkred"]

    for index, section in enumerate(sections):
        segments = [
            {
                "role": role,
                "text": f"{section.title} bölüm {position + 1}.",
                "purpose": section.purpose,
            }
            for position, role in enumerate(section.roles)
        ]
        narration = " ".join(segment["text"] for segment in segments)

        chapters.append(
            {
                "chapter_id": section.chapter_id,
                "title": section.title,
                "order": index + 1,
                "narration": {
                    "text": narration,
                    "word_count": len(narration.split()),
                    # ChapterDocument wants whole seconds here; the exact
                    # lengths live in the TTS manifest, which is what every
                    # timing decision downstream actually reads.
                    "estimated_duration_seconds": max(1, round(sum(section.durations))),
                },
                "visual": {
                    "type": "b_roll",
                    "description": f"{section.title} görseli",
                    "image_prompt": section.title,
                },
                "overlays": [],
                "transitions": {"in": "cut", "out": "cut"},
                "metadata": {
                    "speaker_segments": segments,
                    "is_weather": section.is_weather,
                },
            }
        )

        part_files: list[Path] = []
        parts: list[dict] = []
        for position, (role, seconds) in enumerate(zip(section.roles, section.durations)):
            name = f"{section.chapter_id}_p{position}.mp3"
            part_path = episode_dir / "tts" / "parts" / name
            if real_media:
                _tone_mp3(part_path, seconds, 300 + 120 * position)
            else:
                _stub(part_path)
            part_files.append(part_path)
            parts.append({"role": role, "duration_seconds": seconds, "file": name})

        chapter_mp3 = episode_dir / "tts" / f"{section.chapter_id}.mp3"
        gaps = PART_GAP_SECONDS * (len(part_files) - 1)
        total = round(sum(section.durations) + gaps, 3)
        if real_media:
            if len(part_files) == 1:
                shutil.copyfile(part_files[0], chapter_mp3)
            else:
                _concat_mp3(chapter_mp3, part_files, PART_GAP_SECONDS)
        else:
            _stub(chapter_mp3)

        tts_segments.append(
            {
                "chapter_id": section.chapter_id,
                "file_path": f"tts/{section.chapter_id}.mp3",
                "duration_seconds": total,
                "text_hash": f"{SMOKE_MARKER}-text-{section.chapter_id}",
                "metadata": {"speaker_parts": parts},
            }
        )

        picture = episode_dir / "images" / f"{section.chapter_id}.png"
        if real_media:
            # The final review refuses a weather visual under 5 kB as probably
            # blank, and a flat colour compresses far below that. The weather
            # card therefore gets real detail rather than a larger flat field —
            # raising the fixture's size without giving it content would only
            # teach the check to pass on nothing.
            if section.is_weather:
                _detailed_png(picture, 640, 400)
            else:
                _colour_png(picture, palette[index % len(palette)], 200, 120)
        else:
            _stub(picture)
        images.append(
            {
                "chapter_id": section.chapter_id,
                "file_path": f"images/{section.chapter_id}.png",
                "generation_method": f"{SMOKE_MARKER}-fixture",
                "asset_type": "photo",
                "content_hash": f"{SMOKE_MARKER}-img-{section.chapter_id}",
                "metadata": {"beat_index": 0},
            }
        )

    # One moving topic medium, so "a video in the monitor" is covered too.
    topic_video = episode_dir / "video" / "topic.mp4"
    if real_media:
        binary = ffmpeg_binary()
        _run([binary, "-v", "error", "-y", "-f", "lavfi",
              "-i", f"testsrc=size=200x120:rate={FPS}:duration=1.5",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", str(topic_video)])
    else:
        _stub(topic_video)

    _write_json(
        episode_dir / "chapters.json",
        {
            "schema_version": "1.0",
            "episode_id": SMOKE_EPISODE_ID,
            "title": "ALMANYA24 sentetik bülten",
            "total_chapters": len(chapters),
            "estimated_duration_seconds": max(
                1, round(sum(s["duration_seconds"] for s in tts_segments))
            ),
            "chapters": chapters,
        },
    )
    _write_json(
        episode_dir / "images" / "manifest.json",
        {"episode_id": SMOKE_EPISODE_ID, "images": images},
    )
    _write_json(
        episode_dir / "tts" / "manifest.json",
        {"episode_id": SMOKE_EPISODE_ID, "segments": tts_segments},
    )
    return {"chapters": chapters, "segments": tts_segments, "images": images}


def presenter_clip_suffix(alpha_mode: str) -> str:
    return ".webm" if alpha_mode == ALPHA_MODE_WEBM else ".mp4"


def build_narration_and_qa_gate(episode_dir: Path, sections: list[Section], settings) -> dict:
    """The approved narration and a GREEN translation-QA gate over it.

    Publishing refuses an episode whose narration was never through the QA gate,
    and rightly so. The fixture therefore carries a real gate document bound to
    the real hash of the real narration file, computed with the production
    helper rather than pasted in — a gate whose hash did not match would prove
    nothing about the check it is meant to satisfy.
    """
    from btcedu.core.qa_reviewer import QUALITY_GATE_ARTIFACT, narration_sha256

    stories = [
        {
            "story_id": section.chapter_id,
            "order": index + 1,
            "headline_tr": section.title,
            "text_adapted_tr": " ".join(
                f"{section.title} bölüm {position + 1}." for position in range(len(section.roles))
            ),
        }
        for index, section in enumerate(sections)
    ]
    _write_json(
        episode_dir / "stories_translated.json",
        {"episode_id": SMOKE_EPISODE_ID, "stories": stories},
    )

    digest = narration_sha256(settings, SMOKE_EPISODE_ID)
    gate = {
        "schema_version": 1,
        "episode_id": SMOKE_EPISODE_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "decision": "green",
        "status": "green",
        "blocked": False,
        "deterministic_status": "green",
        "reasons": [f"{SMOKE_MARKER}: synthetic fixture gate"],
        "findings": [],
        "summary": {
            "info_count": 0,
            "minor_count": 0,
            "major_count": 0,
            "critical_count": 0,
            "open_count": 0,
            "resolved_count": 0,
            "dismissed_count": 0,
            "contradiction_count": 0,
        },
        "model_calls": [],
        "retry_history": [],
        "retry_generation": 0,
        "standard_cost_usd": 0.0,
        "escalation_cost_usd": 0.0,
        "total_cost_usd": 0.0,
        "narration_sha256": digest,
        "narration_approved": True,
        "gate_config": {"source": f"{SMOKE_MARKER}-fixture"},
    }
    _write_json(episode_dir / QUALITY_GATE_ARTIFACT, gate)
    return gate


def build_presenter_clips(
    clip_dir: Path,
    scene_ids: list[str],
    *,
    real_media: bool,
    alpha_mode: str = ALPHA_MODE_OPAQUE,
) -> None:
    """Transparent presenter clips, plus one opaque MP4 for the fallback path.

    VP9 alpha cannot be *encoded* by every ffmpeg build, and this has to work on
    the Pi. QuickTime RLE carries a genuine alpha channel and is what the probe
    actually inspects, so the transparency being tested is real even though the
    container is not the production one.
    """
    clip_dir.mkdir(parents=True, exist_ok=True)
    suffix = presenter_clip_suffix(alpha_mode)
    if not real_media:
        for scene_id in scene_ids:
            _stub(clip_dir / f"{scene_id}{suffix}")
        _stub(clip_dir / "opaque.mp4")
        return

    binary = ffmpeg_binary()
    plate = clip_dir / "presenter_plate.png"
    _colour_png(plate, "red", 60, 110)
    for scene_id in scene_ids:
        target = clip_dir / f"{scene_id}{suffix}"
        if suffix == ".webm":
            _run([binary, "-v", "error", "-y", "-loop", "1", "-i", str(plate),
                  "-t", "1.5", "-r", str(FPS), "-c:v", "libvpx", "-pix_fmt", "yuva420p",
                  "-auto-alt-ref", "0", "-metadata:s:v:0", "alpha_mode=1", str(target)])
        else:
            _run([binary, "-v", "error", "-y", "-loop", "1", "-i", str(plate),
                  "-t", "1.5", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                  # All-intra and lossless. A flat colour compresses to about
                  # 2.5 kB otherwise, and the downloader is right to refuse
                  # anything that small as an implausible clip.
                  "-qp", "0", "-g", "1", str(target)])
    opaque = clip_dir / "opaque.mp4"
    if not opaque.exists():
        _run([binary, "-v", "error", "-y", "-loop", "1", "-i", str(plate),
              "-t", "1.5", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-qp", "0", "-g", "1", str(opaque)])


# ---------------------------------------------------------------------------
# The provider doubles
# ---------------------------------------------------------------------------


class FakeHeyGenService:
    """HeyGen, as far as the pipeline can tell, with none of the money.

    It implements the granular surface (``upload_audio_asset`` / ``create_video``
    / ``poll_video_once``) so the coordinator's real retry, idempotency and
    concurrency machinery is exercised rather than bypassed.
    """

    def __init__(self, clip_source: Path, *, cost_per_second_usd: float = 0.0167):
        self.clip_source = Path(clip_source)
        self.cost_per_second_usd = cost_per_second_usd
        self.uploads: list[str] = []
        self.orders: list[dict] = []
        self.looks_seen: list[str] = []
        self.polls: list[str] = []
        self._counter = 0

    def upload_audio_asset(self, path: str) -> str:
        self.uploads.append(path)
        return f"{SMOKE_MARKER}-asset-{len(self.uploads)}"

    def create_video(self, asset_id: str, *, title: str, idempotency_key: str) -> str:
        self._counter += 1
        job_id = f"{SMOKE_MARKER}-job-{self._counter}"
        self.orders.append(
            {
                "asset_id": asset_id,
                "title": title,
                "idempotency_key": idempotency_key,
                "provider_job_id": job_id,
            }
        )
        return job_id

    def poll_video_once(self, provider_job_id: str):
        from btcedu.services.anchor_service import STATE_COMPLETED, ProviderVideoStatus

        self.polls.append(provider_job_id)
        return ProviderVideoStatus(
            state=STATE_COMPLETED,
            raw_status="completed",
            video_url=f"file://{self.clip_source}",
            duration_seconds=1.5,
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(duration_seconds * self.cost_per_second_usd, 6)


class FakeYouTubeService:
    """Accepts exactly one upload and records what it was handed."""

    def __init__(self):
        self.uploads: list[dict] = []

    def upload_video(self, req, progress_callback=None, accepted_callback=None):
        from btcedu.services.youtube_service import YouTubeUploadResponse

        self.uploads.append(
            {
                "video_path": str(req.video_path),
                "title": req.title,
                "privacy_status": req.privacy_status,
                "subtitle_path": str(req.subtitle_path) if req.subtitle_path else None,
                "description": req.description,
            }
        )
        video_id = f"{SMOKE_MARKER}-VIDEO-{len(self.uploads)}"
        if accepted_callback:
            accepted_callback(video_id)
        if progress_callback:
            progress_callback(1, 1)
        return YouTubeUploadResponse(
            video_id=video_id,
            video_url=f"https://youtu.be/{video_id}",
            status="uploaded",
            privacy_status=req.privacy_status,
        )


def install_fake_downloader(monkeypatch, clip_source: Path) -> list[str]:
    """Replace the clip download with a local copy, keeping the validation.

    The point is that ``download_and_validate`` is the only place a URL is ever
    opened; swapping the *opener* rather than the whole function keeps the
    streaming, hashing and ffprobe checks in the run.
    """
    requested: list[str] = []
    from btcedu.core import avatar_coordinator as coordinator_module
    from btcedu.core import avatar_download as download_module

    real = download_module.download_and_validate

    def local_download(url, destination, expectation, **kwargs):
        requested.append(str(url))

        def opener(target, **_kwargs):
            return _FileResponse(Path(clip_source))

        return real(url, destination, expectation, opener=opener, **kwargs)

    monkeypatch.setattr(coordinator_module, "download_and_validate", local_download)
    return requested


class _FileResponse:
    """The little that ``stream_to_file`` needs from a response object."""

    def __init__(self, path: Path, chunk_bytes: int = 64 * 1024):
        self._path = path
        self._chunk = chunk_bytes
        self.status_code = 200
        # Deliberately unhelpful, because the real one often is: the validator
        # must decide from ffprobe, not from what the server claims.
        self.headers = {"Content-Type": "application/octet-stream"}

    def iter_content(self, chunk_size=None):
        with self._path.open("rb") as handle:
            while True:
                block = handle.read(chunk_size or self._chunk)
                if not block:
                    return
                yield block

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Assembling the world
# ---------------------------------------------------------------------------


def build_settings(root: Path, profiles_dir: Path, **overrides):
    """Settings pointed entirely at the temporary directory."""
    from btcedu.config import Settings

    values = {
        "outputs_dir": str(root / "outputs"),
        "reports_dir": str(root / "reports"),
        "logs_dir": str(root / "logs"),
        "profiles_dir": str(profiles_dir),
        "database_url": f"sqlite:///{root / 'smoke.db'}",
        "default_content_profile": SMOKE_PROFILE,
        "anchor_enabled": True,
        # Local only. A remote render is exercised separately, with a fake
        # transport, so the default path never reaches for GitHub.
        "render_execution_mode": "local",
        "render_resolution": f"{WIDTH}x{HEIGHT}",
        "render_fps": FPS,
        "render_crf": 35,
        "render_preset": "ultrafast",
        "render_audio_bitrate": "96k",
        "render_timeout_segment": 600,
        "max_episode_cost_usd": 15.0,
        "youtube_default_target": "test",
        "dry_run": False,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _import_all_models() -> None:
    """Import every model module so ``Base.metadata`` is complete.

    ``btcedu.models.__init__`` exports the long-standing models but not all of
    the avatar ones, and a table that is merely absent from the metadata fails
    much later, as a confusing missing-table error mid-stage.
    """
    import importlib
    import pkgutil

    import btcedu.models as models_package

    for module in pkgutil.iter_modules(models_package.__path__):
        importlib.import_module(f"{models_package.__name__}.{module.name}")


def build_database(settings):
    """A private in-file SQLite database. Never the production one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from btcedu.db import Base
    from btcedu.models.media_asset import Base as MediaBase

    _import_all_models()

    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def build_world(
    root: Path,
    *,
    real_media: bool = False,
    sections: list[Section] | None = None,
    profile_overrides: dict | None = None,
    look_ids: list[str] | None = None,
    settings_overrides: dict | None = None,
    alpha_mode: str = ALPHA_MODE_OPAQUE,
) -> SmokeWorld:
    """Build one complete synthetic ALMANYA24 episode, ready for ``sceneplan``."""
    from btcedu.models.episode import Episode, EpisodeStatus

    root = Path(root)
    sections = sections or bulletin_sections()
    studio_dir = root / "studio"
    profiles_dir = root / "profiles"
    rights_file = root / "rights" / "anchor-rights.json"

    build_studio(studio_dir, real_media=real_media, alpha_mode=alpha_mode)
    build_rights_record(rights_file)
    build_profile(
        profiles_dir,
        studio_dir=studio_dir,
        rights_file=rights_file,
        look_ids=look_ids,
        overrides=profile_overrides,
        alpha_mode=alpha_mode,
    )

    settings = build_settings(root, profiles_dir, **(settings_overrides or {}))
    engine, session = build_database(settings)

    episode_dir = Path(settings.outputs_dir) / SMOKE_EPISODE_ID
    episode_dir.mkdir(parents=True, exist_ok=True)
    build_episode_files(episode_dir, sections, real_media=real_media)

    build_narration_and_qa_gate(episode_dir, sections, settings)

    episode = Episode(
        episode_id=SMOKE_EPISODE_ID,
        title="ALMANYA24 sentetik bülten (test fixture)",
        url="https://example.invalid/smoke",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
        content_profile=SMOKE_PROFILE,
        source="local_recorder",
        published_at=datetime.now(UTC),
    )
    session.add(episode)
    session.commit()

    from btcedu.profiles import reset_registry

    reset_registry()

    return SmokeWorld(
        root=root,
        outputs_dir=Path(settings.outputs_dir),
        episode_dir=episode_dir,
        studio_dir=studio_dir,
        profiles_dir=profiles_dir,
        rights_file=rights_file,
        settings=settings,
        session=session,
        engine=engine,
        sections=sections,
        real_media=real_media,
        alpha_mode=alpha_mode,
    )


def anchor_scene_ids(world: SmokeWorld) -> list[str]:
    """The scene ids the planner would give the presenter, for pre-seeding."""
    from btcedu.core.scene_planner import ROLE_ANCHOR as PLANNER_ANCHOR

    plan = world.scene_plan
    return [
        scene["scene_id"]
        for scene in plan["scenes"]
        if scene.get("speaker_role") == PLANNER_ANCHOR and scene.get("needs_avatar")
    ]


# ---------------------------------------------------------------------------
# Driving the real pipeline
# ---------------------------------------------------------------------------


class Patcher:
    """A minimal monkeypatch, so the runner works outside pytest too."""

    def __init__(self):
        self._undo: list[tuple[Any, str, Any]] = []

    def setattr(self, target: Any, name: str, value: Any) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._undo:
            target, name, original = self._undo.pop()
            setattr(target, name, original)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.undo()
        return False


def install_fake_providers(patcher: Patcher, world: SmokeWorld) -> dict:
    """Point every outbound call at a local double. Returns the doubles."""
    from btcedu.core import anchor_generator
    from btcedu.services import youtube_service

    fixtures = world.episode_dir / "fixtures"
    clip_source = fixtures / f"presenter{presenter_clip_suffix(world.alpha_mode)}"
    if not clip_source.exists():
        build_presenter_clips(
            fixtures, ["presenter"], real_media=world.real_media, alpha_mode=world.alpha_mode
        )

    heygen = FakeHeyGenService(clip_source)
    youtube = FakeYouTubeService()

    def fake_service(config, settings, anchor_dir, avatar_id=None):
        heygen.cost_per_second_usd = getattr(config, "cost_per_second_usd", 0.0167)
        if avatar_id:
            heygen.looks_seen.append(avatar_id)
        return heygen

    patcher.setattr(anchor_generator, "_create_anchor_service", fake_service)
    patcher.setattr(youtube_service, "YouTubeDataAPIService", lambda **kwargs: youtube)

    requested = install_fake_downloader(patcher, clip_source)
    return {"heygen": heygen, "youtube": youtube, "downloads": requested}


@dataclass
class PhaseResult:
    """One observable step of the dry run."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class SmokeResult:
    phases: list[PhaseResult] = field(default_factory=list)
    video_path: Path | None = None
    uploads: list[dict] = field(default_factory=list)
    doubles: dict = field(default_factory=dict)
    anchor_orders: int = 0
    anchor_cost_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return all(phase.ok for phase in self.phases)

    def add(self, name: str, ok: bool, detail: str = "") -> PhaseResult:
        phase = PhaseResult(name, ok, detail)
        self.phases.append(phase)
        return phase


def run_stage(world: SmokeWorld, stage: str, *, force: bool = False):
    """One real pipeline stage against the synthetic world."""
    from btcedu.core.pipeline import _run_stage
    from btcedu.models.episode import Episode

    episode = (
        world.session.query(Episode)
        .filter(Episode.episode_id == world.episode_id)
        .first()
    )
    return _run_stage(world.session, episode, world.settings, stage, force=force)


def approve_anchor_gate(world: SmokeWorld, *, notes: str = "synthetic run") -> str:
    """Approve the presenter clips against their current fingerprint."""
    from btcedu.core.anchor_review import approve, review_hash

    digest = review_hash(world.session, world.episode_id, world.settings)
    approve(
        world.session,
        world.episode_id,
        world.settings,
        notes=notes,
        expected_review_hash=digest,
        operator_ref=f"{SMOKE_MARKER}-operator",
    )
    return digest


def approve_final_gate(world: SmokeWorld) -> None:
    """Approve the finished programme, exactly as the dashboard would."""
    from btcedu.core.reviewer import auto_approve_stage

    draft = world.episode_dir / "render" / "draft.mp4"
    auto_approve_stage(world.session, world.episode_id, "render", [str(draft)])
    world.session.commit()


def run_dry_run(world: SmokeWorld, *, echo=None, doubles: dict | None = None) -> SmokeResult:
    """Walk the whole bulletin from shot list to simulated upload.

    Deliberately stage by stage rather than through ``run_episode_pipeline``:
    the two review gates are supposed to *stop* the run, and stopping twice on
    purpose is the behaviour under test, not an obstacle to it.
    """
    from btcedu.core.avatar_jobs import episode_avatar_cost

    echo = echo or (lambda message: None)
    result = SmokeResult()

    with Patcher() as patcher:
        # A caller that already installed the doubles keeps them, so it can
        # inspect the very objects the run used rather than a second set that
        # was patched over and never called.
        doubles = doubles if doubles is not None else install_fake_providers(patcher, world)

        echo("[1/8] sceneplan — cutting the bulletin into scenes")
        stage = run_stage(world, "sceneplan")
        plan = world.scene_plan if stage.status != "failed" else {}
        result.add(
            "sceneplan",
            stage.status in {"success", "skipped"},
            stage.error or f"{len(plan.get('scenes', []))} scenes",
        )
        if stage.status == "failed":
            return result

        echo("[2/8] anchorgen — ordering presenter clips from the fake provider")
        stage = run_stage(world, "anchorgen")
        result.add("anchorgen", stage.status in {"success", "skipped"},
                   stage.error or stage.detail)
        if stage.status == "failed":
            return result
        result.anchor_orders = len(doubles["heygen"].orders)
        result.anchor_cost_usd = episode_avatar_cost(world.session, world.episode_id)

        echo("[3/8] review_gate_anchor — expected to stop and wait for a human")
        stage = run_stage(world, "review_gate_anchor")
        result.add(
            "review_gate_anchor stops",
            stage.status == "review_pending",
            stage.detail or stage.status,
        )

        echo("[4/8] approving the presenter clips against their fingerprint")
        digest = approve_anchor_gate(world)
        stage = run_stage(world, "review_gate_anchor")
        result.add("review_gate_anchor passes", stage.status in {"success", "skipped"},
                   f"digest {digest[:12]}…")
        if stage.status not in {"success", "skipped"}:
            return result

        echo("[5/8] render — compositing the studio, the monitor and the reporter media")
        stage = run_stage(world, "render")
        result.add("render", stage.status in {"success", "skipped"},
                   stage.error or stage.detail)
        if stage.status == "failed":
            return result
        draft = world.episode_dir / "render" / "draft.mp4"
        result.video_path = draft if draft.exists() else None

        echo("[6/8] review_gate_3 — expected to stop for the final broadcast review")
        stage = run_stage(world, "review_gate_3")
        result.add("review_gate_3 stops", stage.status == "review_pending",
                   stage.detail or stage.status)

        echo("[7/8] approving the finished programme")
        approve_final_gate(world)
        stage = run_stage(world, "review_gate_3")
        result.add("review_gate_3 passes", stage.status in {"success", "skipped"},
                   stage.detail or stage.status)

        echo("[8/8] publish — one simulated private upload, no network")
        stage = run_stage(world, "publish")
        result.add("publish", stage.status in {"success", "skipped"},
                   stage.error or stage.detail)
        result.uploads = list(doubles["youtube"].uploads)
        result.doubles = doubles

    return result
