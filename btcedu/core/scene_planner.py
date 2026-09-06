"""Scene planning: turn a chapter's speaker blocks into a durable shot list.

The pipeline already knows who speaks, for how long and which picture belongs to
each block — but only in pieces, and only in memory. ``chapters.json`` holds the
speaker sequence, the TTS manifest holds the measured duration of every part,
and the image manifest holds one picture per block. The renderer stitches those
together on the fly and forgets the result.

That was enough while every block was rendered the same way. It stops being
enough once the presenter's blocks have to be sent to an avatar provider: the
anchor stage needs the same durations to project cost and order clips of the
right length, and the remote render runner needs them without a database. So the
plan becomes an artefact of its own, written once and read by both.

The plan changes nothing about the narration or the audio. It only records what
each block *is*.
"""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCENE_PLAN_FILENAME = "scene_plan.json"
SCHEMA_VERSION = 1

ROLE_ANCHOR = "anchor_female"
ROLE_REPORTER = "reporter_male"

# Deterministic templates. The profile owns their look; the planner only says
# which one applies, so a bulletin's framing never depends on a model's mood.
TEMPLATE_OPENING = "studio_opening_wide"
TEMPLATE_ANCHOR = "studio_anchor_medium"
TEMPLATE_ANCHOR_RETURN = "studio_anchor_return"
TEMPLATE_REPORTER = "reporter_fullscreen"
TEMPLATE_WEATHER = "studio_weather_handover"
TEMPLATE_CLOSING = "studio_closing"

VISUAL_MODE_STUDIO = "studio_composite"
VISUAL_MODE_FULLSCREEN = "fullscreen_media"
VISUAL_MODE_WEATHER = "weather_renderer"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Scene:
    """One uninterrupted block of a single speaker."""

    scene_id: str
    chapter_id: str
    order: int
    beat_index: int
    speaker_role: str
    purpose: str
    segment_indices: list[int]
    text_hash: str
    audio_file: str | None
    expected_duration_seconds: float
    visual_mode: str
    template_id: str
    background_asset: str | None
    background_asset_type: str
    display_zone_id: str | None
    display_fit_mode: str
    focus_point: list[float] | None
    transition_in: str
    overlays: dict = field(default_factory=dict)
    needs_avatar: bool = False


@dataclass
class ScenePlanResult:
    """Summary of scene planning for one episode."""

    episode_id: str
    plan_path: Path
    scene_count: int = 0
    anchor_scene_count: int = 0
    anchor_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0
    skipped: bool = False


def scene_plan_path(outputs_dir: str | Path, episode_id: str) -> Path:
    return Path(outputs_dir) / episode_id / SCENE_PLAN_FILENAME


def speaker_blocks(segments: list[dict]) -> list[dict]:
    """Group consecutive segments of one speaker into blocks.

    Mirrors the chapterizer's own grouping, with one deliberate difference: a
    chapter spoken entirely by one presenter yields one block rather than none.
    The chapterizer drops those because a single picture needs no beat list, but
    a single-speaker chapter is still a scene — and for the opening and closing,
    which the anchor presents alone, it is precisely the scene that has to reach
    the avatar.
    """
    blocks: list[dict] = []
    for position, segment in enumerate(segments):
        role = str(segment.get("role") or "").strip()
        if not role:
            continue
        text = str(segment.get("text") or "")
        if blocks and blocks[-1]["role"] == role:
            block = blocks[-1]
            block["purposes"].append(str(segment.get("purpose") or ""))
            block["segment_indices"].append(position)
            block["text"] = f"{block['text']} {text}".strip()
            continue
        blocks.append(
            {
                "beat_index": len(blocks),
                "role": role,
                "purposes": [str(segment.get("purpose") or "")],
                "segment_indices": [position],
                "text": text.strip(),
            }
        )
    return blocks


def block_durations(
    blocks: list[dict],
    parts: list[dict],
    total_duration: float,
) -> list[float]:
    """Split a chapter's running time across its blocks.

    Deliberately identical in spirit to the renderer's own split: weights come
    from the measured per-speaker audio, fall back to word counts, and are
    scaled so the blocks add up to exactly the chapter length however the pauses
    between speakers were distributed. Planning and rendering must not disagree
    about how long a block is, or the avatar clip and the picture would drift
    apart.
    """
    if not blocks:
        return []

    weights: list[float] = []
    for block in blocks:
        measured = [
            float(parts[i].get("duration_seconds") or 0.0)
            for i in block["segment_indices"]
            if 0 <= i < len(parts)
        ]
        if measured and sum(measured) > 0:
            weights.append(sum(measured))
        else:
            weights.append(float(max(1, len(str(block.get("text") or "").split()))))

    total_weight = sum(weights)
    if total_weight <= 0:
        share = total_duration / len(blocks)
        return [round(share, 3)] * len(blocks)

    durations = [total_duration * w / total_weight for w in weights]
    durations[-1] = round(total_duration - sum(durations[:-1]), 3)
    return [round(d, 3) for d in durations]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _part_file(parts: list[dict], indices: list[int]) -> str | None:
    """The audio file of a block, but only when the block is exactly one part.

    A block spanning several parts has no single file, and concatenating them
    here would invent an artefact the TTS stage never produced. The anchor stage
    handles that case by joining the parts itself.
    """
    if len(indices) != 1:
        return None
    index = indices[0]
    if not (0 <= index < len(parts)):
        return None
    name = str(parts[index].get("file") or "").strip()
    return f"tts/parts/{name}" if name else None


def _part_files(parts: list[dict], indices: list[int]) -> list[str]:
    files = []
    for index in indices:
        if 0 <= index < len(parts):
            name = str(parts[index].get("file") or "").strip()
            if name:
                files.append(f"tts/parts/{name}")
    return files


def _chapter_images(chapter_id: str, image_manifest: dict) -> list[str]:
    """Usable pictures of a chapter, ordered by presenter block."""
    entries = [
        img
        for img in image_manifest.get("images", [])
        if img.get("chapter_id") == chapter_id and img.get("generation_method") != "failed"
    ]
    entries.sort(key=lambda e: int((e.get("metadata") or {}).get("beat_index") or 0))
    return [str(e["file_path"]) for e in entries if e.get("file_path")]


def _template_for(
    role: str,
    *,
    is_weather: bool,
    is_first_chapter: bool,
    is_last_chapter: bool,
    is_first_block: bool,
) -> tuple[str, str]:
    """Pick the template and visual mode for one block."""
    if role != ROLE_ANCHOR:
        # The reporter is never seen. His blocks are the editorial media at full
        # frame, which is also what the pipeline already produces for them.
        return TEMPLATE_REPORTER, VISUAL_MODE_FULLSCREEN
    if is_weather:
        return TEMPLATE_WEATHER, VISUAL_MODE_STUDIO
    if is_first_chapter and is_first_block:
        return TEMPLATE_OPENING, VISUAL_MODE_STUDIO
    if is_last_chapter:
        return TEMPLATE_CLOSING, VISUAL_MODE_STUDIO
    if is_first_block:
        return TEMPLATE_ANCHOR, VISUAL_MODE_STUDIO
    # Coming back to the presenter after the reporter had the picture.
    return TEMPLATE_ANCHOR_RETURN, VISUAL_MODE_STUDIO


def build_scenes(
    chapters: list,
    audio_entries: dict[str, dict],
    image_manifest: dict,
    *,
    display_zone_id: str = "main_wall",
) -> list[Scene]:
    """Build the shot list for a whole episode."""
    scenes: list[Scene] = []
    order = 0
    last_chapter_index = len(chapters) - 1

    for chapter_index, chapter in enumerate(chapters):
        chapter_id = str(chapter.get("chapter_id") or "")
        metadata = chapter.get("metadata") or {}
        is_weather = bool(metadata.get("is_weather"))
        segments = metadata.get("speaker_segments") or []
        audio = audio_entries.get(chapter_id) or {}
        parts = ((audio.get("metadata") or {}).get("speaker_parts")) or []
        chapter_duration = float(audio.get("duration_seconds") or 0.0)
        images = _chapter_images(chapter_id, image_manifest)

        blocks = speaker_blocks(segments)
        if not blocks:
            # No speaker information: one block for the whole chapter, spoken by
            # whoever the chapter names, so the plan still covers every second
            # of the programme.
            blocks = [
                {
                    "beat_index": 0,
                    "role": ROLE_ANCHOR,
                    "purposes": [""],
                    "segment_indices": [],
                    "text": str((chapter.get("narration") or {}).get("text") or ""),
                }
            ]

        durations = block_durations(blocks, parts, chapter_duration)

        for block_position, (block, duration) in enumerate(zip(blocks, durations, strict=True)):
            role = block["role"]
            template_id, visual_mode = _template_for(
                role,
                is_weather=is_weather,
                is_first_chapter=chapter_index == 0,
                is_last_chapter=chapter_index == last_chapter_index,
                is_first_block=block_position == 0,
            )
            if is_weather and role != ROLE_ANCHOR:
                # The weather itself is rendered deterministically; the anchor
                # only hands over to it.
                visual_mode = VISUAL_MODE_WEATHER

            # One picture per block where the pipeline made them; otherwise the
            # chapter's single picture stands behind every block of it.
            if images:
                background = images[min(block["beat_index"], len(images) - 1)]
            else:
                background = None

            indices = block["segment_indices"]
            scenes.append(
                Scene(
                    scene_id=f"{chapter_id}_s{block['beat_index']:02d}",
                    chapter_id=chapter_id,
                    order=order,
                    beat_index=block["beat_index"],
                    speaker_role=role,
                    purpose=next((p for p in block["purposes"] if p), ""),
                    segment_indices=list(indices),
                    text_hash=_text_hash(block["text"]),
                    audio_file=_part_file(parts, indices),
                    expected_duration_seconds=duration,
                    visual_mode=visual_mode,
                    template_id=template_id,
                    background_asset=background,
                    background_asset_type="image" if background else "none",
                    display_zone_id=display_zone_id if visual_mode == VISUAL_MODE_STUDIO else None,
                    display_fit_mode="cover",
                    focus_point=None,
                    transition_in="cut",
                    overlays={"lower_third": block_position == 0, "ticker": True},
                    # Only the presenter is animated, and only when her block
                    # actually has audio to lip-sync to.
                    needs_avatar=(
                        role == ROLE_ANCHOR
                        and visual_mode == VISUAL_MODE_STUDIO
                        and duration > 0
                    ),
                )
            )
            order += 1

    return scenes


def compute_plan_hash(scenes: list[Scene], presenter_look_id: str) -> str:
    """Fingerprint what would make the plan mean something different.

    Includes the look, because a different presenter means different clips. The
    background pictures are in here too: swapping the picture behind a block
    changes the shot even when nothing was said differently.
    """
    payload = json.dumps(
        {
            "presenter_look_id": presenter_look_id,
            "scenes": [
                {
                    "scene_id": s.scene_id,
                    "role": s.speaker_role,
                    "template_id": s.template_id,
                    "text_hash": s.text_hash,
                    "duration": s.expected_duration_seconds,
                    "background": s.background_asset,
                }
                for s in scenes
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def anchor_scenes(scenes: list[Scene]) -> list[Scene]:
    """The blocks that go to the avatar provider — and nothing else.

    This is the single place that decides what gets sent, so the rule that the
    reporter's audio never reaches HeyGen is enforced once rather than at every
    call site.
    """
    return [s for s in scenes if s.needs_avatar and s.speaker_role == ROLE_ANCHOR]


def write_scene_plan(
    path: Path,
    episode_id: str,
    scenes: list[Scene],
    *,
    presenter_look_id: str,
    presenter_assignment_id: int | None,
) -> dict:
    """Write the plan atomically so a crash cannot leave half a shot list."""
    anchors = anchor_scenes(scenes)
    document = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id,
        "generated_at": _utcnow().isoformat(),
        "presenter_assignment_id": presenter_assignment_id,
        "presenter_look_id": presenter_look_id,
        "content_hash": compute_plan_hash(scenes, presenter_look_id),
        "scene_count": len(scenes),
        "anchor_scene_count": len(anchors),
        "anchor_duration_seconds": round(
            sum(s.expected_duration_seconds for s in anchors), 3
        ),
        "total_duration_seconds": round(
            sum(s.expected_duration_seconds for s in scenes), 3
        ),
        "scenes": [asdict(s) for s in scenes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return document


def load_scene_plan(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scenes_from_plan(document: dict) -> list[Scene]:
    return [Scene(**scene) for scene in document.get("scenes", [])]


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compute_input_hash(
    chapters_doc: dict,
    audio_entries: dict[str, dict],
    image_manifest: dict,
    presenter_look_id: str,
) -> str:
    """Hash the inputs the plan is derived from.

    Anything that would change a scene has to be in here: what is said, how long
    it takes, which picture stands behind it and who presents it.
    """
    relevant = {
        "presenter_look_id": presenter_look_id,
        "chapters": [
            {
                "chapter_id": ch.get("chapter_id"),
                "narration": (ch.get("narration") or {}).get("text"),
                "speaker_segments": (ch.get("metadata") or {}).get("speaker_segments"),
                "is_weather": (ch.get("metadata") or {}).get("is_weather"),
            }
            for ch in chapters_doc.get("chapters", [])
        ],
        "audio": {
            chapter_id: {
                "duration": entry.get("duration_seconds"),
                "parts": [
                    {"role": p.get("role"), "duration": p.get("duration_seconds")}
                    for p in ((entry.get("metadata") or {}).get("speaker_parts") or [])
                ],
            }
            for chapter_id, entry in sorted(audio_entries.items())
        },
        "images": sorted(
            (str(img.get("chapter_id") or ""), str(img.get("file_path") or ""))
            for img in image_manifest.get("images", [])
            if img.get("generation_method") != "failed"
        ),
    }
    payload = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_plan_current(plan_path: Path, provenance_path: Path, input_hash: str) -> bool:
    if not plan_path.exists() or not provenance_path.exists():
        return False
    if plan_path.with_suffix(".json.stale").exists():
        logger.info("Scene plan marked as stale")
        return False
    try:
        provenance = _load_json(provenance_path)
    except json.JSONDecodeError as exc:
        logger.warning("Could not verify scene plan provenance: %s", exc)
        return False
    if provenance.get("input_content_hash") != input_hash:
        logger.info("Scene plan inputs have changed")
        return False
    return True


def plan_scenes(
    session,
    episode_id: str,
    settings,
    force: bool = False,
):
    """Write the episode's shot list.

    Deterministic and free: no provider is contacted, so this can run on every
    episode whether or not the avatar is enabled. That is the point of it being
    its own stage — the renderer gets a plan either way, and enabling the avatar
    later does not change how the programme is cut.
    """
    from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(f"Episode {episode_id} is v1 pipeline. Scene planning requires v2.")

    if (
        episode.status not in (EpisodeStatus.TTS_DONE, EpisodeStatus.SCENE_PLANNED)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'scene_planned'. Use --force to override."
        )

    outputs_dir = Path(settings.outputs_dir) / episode_id
    chapters_path = outputs_dir / "chapters.json"
    tts_manifest_path = outputs_dir / "tts" / "manifest.json"
    image_manifest_path = outputs_dir / "images" / "manifest.json"
    plan_path = scene_plan_path(settings.outputs_dir, episode_id)
    provenance_path = outputs_dir / "provenance" / "sceneplan_provenance.json"

    if not chapters_path.exists():
        raise FileNotFoundError(f"Chapters file not found: {chapters_path}")
    if not tts_manifest_path.exists():
        raise FileNotFoundError(f"TTS manifest not found: {tts_manifest_path}")

    chapters_doc = _load_json(chapters_path)
    tts_manifest = _load_json(tts_manifest_path)
    image_manifest = (
        _load_json(image_manifest_path) if image_manifest_path.exists() else {"images": []}
    )
    audio_entries = {
        str(entry.get("chapter_id")): entry for entry in tts_manifest.get("segments", [])
    }

    # The presenter is only assigned when the avatar is actually in use; without
    # it the plan is still complete, it simply has no look to name.
    assignment = None
    presenter_look_id = ""
    if settings.anchor_enabled:
        from btcedu.core.anchor_config import resolve_anchor_config
        from btcedu.core.presenter_assignment import ensure_assignment

        config = resolve_anchor_config(episode.content_profile, settings)
        if config.provider == "heygen":
            assignment = ensure_assignment(session, episode_id, config, settings.outputs_dir)
            presenter_look_id = assignment.avatar_look_id

    input_hash = compute_input_hash(chapters_doc, audio_entries, image_manifest, presenter_look_id)

    if not force and _is_plan_current(plan_path, provenance_path, input_hash):
        logger.info("Scene plan is current for %s (use --force to regenerate)", episode_id)
        if episode.status == EpisodeStatus.TTS_DONE:
            episode.status = EpisodeStatus.SCENE_PLANNED
            session.commit()
        document = load_scene_plan(plan_path)
        return ScenePlanResult(
            episode_id=episode_id,
            plan_path=plan_path,
            scene_count=int(document.get("scene_count") or 0),
            anchor_scene_count=int(document.get("anchor_scene_count") or 0),
            anchor_duration_seconds=float(document.get("anchor_duration_seconds") or 0.0),
            total_duration_seconds=float(document.get("total_duration_seconds") or 0.0),
            skipped=True,
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="sceneplan",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        scenes = build_scenes(
            chapters_doc.get("chapters", []),
            audio_entries,
            image_manifest,
        )
        document = write_scene_plan(
            plan_path,
            episode_id,
            scenes,
            presenter_look_id=presenter_look_id,
            presenter_assignment_id=assignment.id if assignment else None,
        )

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "episode_id": episode_id,
                    "stage": "sceneplan",
                    "generated_at": _utcnow().isoformat(),
                    "input_content_hash": input_hash,
                    "plan_content_hash": document["content_hash"],
                    "presenter_look_id": presenter_look_id,
                    "scene_count": document["scene_count"],
                    "anchor_scene_count": document["anchor_scene_count"],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        stale = plan_path.with_suffix(".json.stale")
        if stale.exists():
            stale.unlink()

        # A changed shot list changes every clip made from it.
        _mark_downstream_stale(outputs_dir)

        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        if episode.status == EpisodeStatus.TTS_DONE:
            episode.status = EpisodeStatus.SCENE_PLANNED
        episode.error_message = None
        session.commit()

        anchors = anchor_scenes(scenes)
        logger.info(
            "Scene plan for %s: %d scenes, %d presenter (%.1fs)",
            episode_id,
            len(scenes),
            len(anchors),
            sum(s.expected_duration_seconds for s in anchors),
        )
        return ScenePlanResult(
            episode_id=episode_id,
            plan_path=plan_path,
            scene_count=len(scenes),
            anchor_scene_count=len(anchors),
            anchor_duration_seconds=float(document["anchor_duration_seconds"]),
            total_duration_seconds=float(document["total_duration_seconds"]),
        )
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        episode.error_message = str(exc)[:1000]
        session.commit()
        raise


def _mark_downstream_stale(outputs_dir: Path) -> None:
    """Invalidate what is derived from the shot list, and only that."""
    for relative in ("anchor/manifest.json", "render/render_manifest.json"):
        target = outputs_dir / relative
        if target.exists():
            target.with_suffix(".json.stale").write_text(
                json.dumps({"reason": "scene_plan_changed"}), encoding="utf-8"
            )
