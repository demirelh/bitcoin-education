"""Bind editorial editions to the existing TTS and video renderer."""

from __future__ import annotations

import json
import math
import shutil
import textwrap
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.editorial.ingest import canonical_hash
from btcedu.core.editorial.public import latest_research_run
from btcedu.core.editorial.video import (
    EditionBlocked,
    StaleEditionApproval,
    broadcast_script,
    edition_media,
    file_hash,
    require_script_approval,
    require_video_media_approval,
)
from btcedu.models.article import ArticleRevision
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.editorial import EditorialRevision
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.media_rights import MediaUseDecision, NewsroomMediaAsset
from btcedu.models.video_edition import EditionDecision, EditionDecisionKind, VideoEdition

SOURCE = "editorial_revision"
STAGES = [
    ("script", EpisodeStatus.NEW),
    ("tts", EpisodeStatus.IMAGES_GENERATED),
    ("render", EpisodeStatus.TTS_DONE),
    ("review_gate_3", EpisodeStatus.RENDERED),
]


def research_for(session, edition):
    article = session.get(ArticleRevision, edition.article_revision_id)
    revision = session.get(EditorialRevision, article.editorial_revision_id)
    run = latest_research_run(session, revision.topic_id)
    if run is None:
        raise EditionBlocked("No research run for this edition")
    return run


def edition_for(session: Session, episode: Episode) -> VideoEdition | None:
    edition = session.query(VideoEdition).filter_by(episode_id=episode.id).one_or_none()
    if episode.source == SOURCE and edition is None:
        raise EditionBlocked("Editorial episode has no edition binding")
    return edition


def bind_episode(session: Session, edition: VideoEdition, settings) -> Episode:
    _require_profile(edition, settings)
    require_script_approval(session, edition, research_run=research_for(session, edition))
    require_video_media_approval(session, edition)
    if edition.episode_id:
        return session.get(Episode, edition.episode_id)
    article = session.get(ArticleRevision, edition.article_revision_id)
    episode = Episode(
        episode_id=f"editorial-{edition.edition_id}",
        source=SOURCE,
        title=article.title,
        url=f"editorial:{edition.edition_id}",
        pipeline_version=2,
        content_profile=edition.profile,
        status=EpisodeStatus.NEW,
    )
    session.add(episode)
    session.flush()
    edition.episode_id = episode.id
    session.commit()
    return episode


def _require_profile(edition, settings):
    from btcedu.core.editorial.video import edition_routing_enabled
    from btcedu.core.profile_validation import resolve_profile

    if not settings.newsroom_enabled or settings.anchor_enabled:
        raise EditionBlocked("Enable newsroom explicitly; avatar generation must remain disabled")
    profile = resolve_profile(settings, edition.profile)
    if (
        not edition_routing_enabled(profile)
        or profile.auto_publish
        or profile.auto_approve_reviews
        or profile.stage_config.get("anchor", {}).get("enabled", False)
    ):
        raise EditionBlocked("Edition requires an editorial profile with manual decisions")


def _inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or path.is_symlink():
        raise EditionBlocked("Unsafe edition artifact path")
    if not path.resolve().is_relative_to(root.resolve()):
        raise EditionBlocked("Edition artifact escapes the episode directory")
    return path


def production_inputs(root: Path) -> dict[str, str]:
    """Actual bytes, not caller-supplied hashes, including original TTS."""
    files = ["chapters.json", "script_broadcast.json", "images/manifest.json"]
    tts = root / "tts/manifest.json"
    if tts.is_file():
        files.append("tts/manifest.json")
    for manifest, key in (("images/manifest.json", "images"), ("tts/manifest.json", "segments")):
        path = root / manifest
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data[key]:
            relative = entry["file_path"]
            allowed_suffixes = (
                {".png", ".jpg", ".jpeg", ".webp"} if key == "images" else {".mp3", ".m4a", ".wav"}
            )
            if (
                not relative.startswith(manifest.split("/")[0] + "/")
                or Path(relative).suffix.lower() not in allowed_suffixes
            ):
                raise EditionBlocked(
                    "Manifest references a file outside the render media allowlist"
                )
            files.append(relative)
    return {relative: file_hash(_inside(root, relative)) for relative in sorted(set(files))}


def require_production(session, episode, settings, *, prepared=True) -> VideoEdition | None:
    edition = edition_for(session, episode)
    if edition is None:
        return None
    _require_profile(edition, settings)
    if not settings.newsroom_enabled or settings.anchor_enabled:
        raise EditionBlocked("Editorial production is disabled or avatars are enabled")
    if edition.profile != episode.content_profile:
        raise EditionBlocked("Edition belongs to a different video profile")
    require_script_approval(session, edition, research_run=research_for(session, edition))
    require_video_media_approval(session, edition)
    if prepared:
        root = Path(settings.outputs_dir) / episode.episode_id
        expected = json.loads((root / "editorial_inputs.json").read_text(encoding="utf-8"))
        actual = production_inputs(root)
        if expected["edition_id"] != edition.edition_id or any(
            actual.get(name) != digest for name, digest in expected["files"].items()
        ):
            raise StaleEditionApproval("Prepared script or picture bytes changed")
    return edition


def prepare_episode(session, episode, settings) -> None:
    """Serialize approved text/media to the existing chapter and image contracts."""
    edition = require_production(session, episode, settings, prepared=False)
    if edition is None:
        raise EditionBlocked("Not an editorial production")
    root = Path(settings.outputs_dir) / episode.episode_id
    if (root / "editorial_inputs.json").is_file():
        require_production(session, episode, settings)
        episode.status = EpisodeStatus.IMAGES_GENERATED
        session.commit()
        return
    script = broadcast_script(session, edition)
    media = list(edition_media(session, edition))
    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    chapters, images = [], []
    for index, segment in enumerate(script.stories[0].speaker_sequence, start=1):
        chapter_id = f"ch{index:02d}"
        overlays = []
        target = root / "images" / f"{chapter_id}.png"
        if media:
            selected = media[(index - 1) % len(media)]
            use = session.get(MediaUseDecision, selected.media_use_decision_id)
            asset = session.get(NewsroomMediaAsset, use.media_asset_id)
            source = Path(asset.blob_path)
            if source.is_symlink() or file_hash(source) != selected.content_hash:
                raise EditionBlocked("Cleared media bytes changed")
            target = target.with_suffix(source.suffix)
            shutil.copyfile(source, target)
            credit = "\n".join(
                textwrap.wrap(
                    " · ".join(
                        filter(None, [selected.on_screen_notice, selected.on_screen_credit])
                    ),
                    width=72,
                )
            )
            if credit:
                overlays = [
                    {
                        "type": "lower_third",
                        "text": credit,
                        "start_offset_seconds": 0,
                        "duration_seconds": 86400,
                    }
                ]
        else:
            from PIL import Image

            Image.new("RGB", (1280, 720), "#18202e").save(target)
        chapters.append(
            {
                "chapter_id": chapter_id,
                "title": script.stories[0].display_headline,
                "order": index,
                "narration": {
                    "text": segment.text,
                    "word_count": segment.word_count,
                    "estimated_duration_seconds": max(
                        1, math.ceil(segment.estimated_duration_seconds)
                    ),
                },
                "visual": {"type": "title_card", "description": "Editorially cleared illustration"},
                "overlays": overlays,
                "transitions": {"in": "cut", "out": "cut"},
                "metadata": {"speaker_segments": [segment.model_dump(mode="json")]},
            }
        )
        images.append(
            {
                "chapter_id": chapter_id,
                "file_path": str(target.relative_to(root)),
                "generation_method": "editorial",
                "content_hash": file_hash(target),
            }
        )
    document = ChapterDocument(
        schema_version="1.0",
        episode_id=episode.episode_id,
        title=episode.title,
        total_chapters=len(chapters),
        estimated_duration_seconds=sum(
            c["narration"]["estimated_duration_seconds"] for c in chapters
        ),
        chapters=chapters,
    )
    (root / "chapters.json").write_text(document.model_dump_json(by_alias=True), encoding="utf-8")
    (root / "script_broadcast.json").write_text(script.model_dump_json(), encoding="utf-8")
    (root / "images/manifest.json").write_text(
        json.dumps(
            {
                "episode_id": episode.episode_id,
                "images": images,
            }
        ),
        encoding="utf-8",
    )
    (root / "editorial_inputs.json").write_text(
        json.dumps(
            {
                "edition_id": edition.edition_id,
                "files": production_inputs(root),
            }
        ),
        encoding="utf-8",
    )
    episode.status = EpisodeStatus.IMAGES_GENERATED
    session.commit()


def require_final(session, episode, settings) -> None:
    edition = require_production(session, episode, settings)
    if edition is None:
        return
    decision = (
        session.query(EditionDecision)
        .filter_by(video_edition_id=edition.id, kind=EditionDecisionKind.FINAL_VIDEO.value)
        .order_by(EditionDecision.id.desc())
        .first()
    )
    root = Path(settings.outputs_dir) / episode.episode_id
    if (
        decision is None
        or decision.decision != "approve"
        or not decision.video_sha256
        or (
            decision.script_hash,
            decision.content_hash,
            decision.evidence_hash,
            decision.media_hash,
        )
        != (edition.script_hash, edition.content_hash, edition.evidence_hash, edition.media_hash)
        or decision.video_sha256 != file_hash(root / "render/draft.mp4")
        or decision.render_input_hash != canonical_hash(production_inputs(root))
    ):
        raise StaleEditionApproval("Final video requires a separate current byte-bound decision")
