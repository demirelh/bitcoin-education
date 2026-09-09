"""Turn an approved article into a video edition (N7).

The rule this module exists to enforce is simple: a video says what the article
says. Dramaturgy may differ — who speaks, in what order, how long — but no
sentence is written here that was not already checked as part of the article.
The adapter therefore never calls a model. It rearranges approved text and
attaches the claims each part rests on.

Two decisions stay apart on purpose. Approving a script is not approving a
finished video, and neither is publishing: nothing in this module uploads
anything, and no article approval starts a render.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.editorial.article import current_article_state
from btcedu.core.editorial.media import approved_revision_media
from btcedu.core.editorial.public import export_blockers
from btcedu.models.article import (
    ArticleParagraph,
    ArticleParagraphClaim,
    ArticleRevision,
    ArticleStatus,
    ParagraphKind,
)
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_rights import (
    LicenseEvidence,
    MediaRole,
    MediaUseDecision,
    NewsroomMediaAsset,
)
from btcedu.models.script_schema import (
    BroadcastScript,
    ScriptStory,
    SegmentPurpose,
    SpeakerRole,
    SpeakerSegment,
)
from btcedu.models.video_edition import (
    EditionDecision,
    EditionDecisionKind,
    EditionDecisionType,
    EditionMedia,
    EditionSegment,
    EditionSegmentClaim,
    EditionStatus,
    VideoEdition,
)

#: On-screen label required for anything that is not footage of the event.
#: An archive picture that looks like today's event is the most ordinary way to
#: mislead a viewer without writing a single false sentence.
ROLE_NOTICE = {
    MediaRole.EVENT.value: "",
    MediaRole.ARCHIVE.value: "ARŞİV",
    MediaRole.SYMBOLIC.value: "SEMBOL GÖRSEL",
    MediaRole.PORTRAIT.value: "ARŞİV",
}

#: Directory names that hold research evidence, licence correspondence and
#: contracts. None of it may travel with a render package.
PRIVATE_PATH_MARKERS = (
    "evidence",
    "research",
    "contracts",
    "licence-correspondence",
    "license-correspondence",
    "auth",
)


class EditionError(RuntimeError):
    """The edition cannot be built or advanced in its current state."""


class EditionBlocked(EditionError):
    """The article is not in a state a video may be derived from."""


class StaleEditionApproval(EditionError):
    """The edition moved after it was approved."""


class PrivateMaterialInPackage(EditionError):
    """A render package would have carried something private."""


@dataclass(frozen=True)
class SegmentPlan:
    position: int
    role: SpeakerRole
    purpose: SegmentPurpose
    text: str
    source_paragraph_position: int | None
    claim_revision_ids: tuple[int, ...]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def edition_routing_enabled(profile) -> bool:
    """Whether this profile builds videos from editorial revisions.

    Off unless a profile says otherwise, so every existing production profile
    keeps adapting the original broadcast exactly as before.
    """
    if profile is None:
        return False
    config = getattr(profile, "stage_config", None) or {}
    section = config.get("editorial_video") or {}
    return bool(isinstance(section, dict) and section.get("enabled"))


def _paragraph_claims(session: Session, paragraph: ArticleParagraph) -> tuple[int, ...]:
    rows = (
        session.query(ArticleParagraphClaim)
        .filter_by(article_paragraph_id=paragraph.id)
        .order_by(ArticleParagraphClaim.id)
        .all()
    )
    return tuple(row.claim_revision_id for row in rows)


def plan_segments(session: Session, article: ArticleRevision) -> tuple[SegmentPlan, ...]:
    """Assign the article's own sentences to voices and purposes.

    Nothing is written, nothing is summarised, nothing is dropped: the segments
    together are exactly the article's paragraphs. What the video adds is who
    reads which part, which is a dramaturgical choice and not an editorial one.
    """
    paragraphs = (
        session.query(ArticleParagraph)
        .filter_by(article_revision_id=article.id)
        .order_by(ArticleParagraph.position)
        .all()
    )
    plans: list[SegmentPlan] = [
        SegmentPlan(
            position=1,
            role=SpeakerRole.ANCHOR,
            purpose=SegmentPurpose.OPENING,
            text=article.title.strip(),
            source_paragraph_position=None,
            claim_revision_ids=(),
        ),
        SegmentPlan(
            position=2,
            role=SpeakerRole.ANCHOR,
            purpose=SegmentPurpose.INTRODUCTION,
            text=article.lede.strip(),
            source_paragraph_position=None,
            claim_revision_ids=(),
        ),
    ]
    body_index = 0
    for paragraph in paragraphs:
        if paragraph.kind == ParagraphKind.CONTEXT.value:
            role, purpose = SpeakerRole.ANCHOR, SegmentPurpose.ANALYSIS
            body_index += 1
        else:
            role = SpeakerRole.REPORTER if body_index % 2 == 0 else SpeakerRole.ANCHOR
            purpose = SegmentPurpose.REPORT
            body_index += 1
        plans.append(
            SegmentPlan(
                position=len(plans) + 1,
                role=role,
                purpose=purpose,
                text=paragraph.text.strip(),
                source_paragraph_position=paragraph.position,
                claim_revision_ids=_paragraph_claims(session, paragraph),
            )
        )
    if len(plans) < 3:
        raise EditionBlocked("The article has no paragraphs to speak")
    return tuple(plans)


def script_hash(plans: Iterable[SegmentPlan]) -> str:
    payload = [
        {
            "position": plan.position,
            "role": plan.role.value,
            "purpose": plan.purpose.value,
            "text": plan.text,
            "claims": list(plan.claim_revision_ids),
        }
        for plan in plans
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _media_plan(session: Session, revision: EditorialRevision) -> list[dict]:
    entries: list[dict] = []
    for row in approved_revision_media(session, revision):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        evidence = session.get(LicenseEvidence, decision.license_evidence_id)
        notice = ROLE_NOTICE.get(decision.role, "ARŞİV")
        credit = decision.attribution_text.strip()
        if evidence is not None and evidence.attribution_required and not credit:
            raise EditionBlocked(
                f"Media decision {decision.decision_id} needs attribution but carries none"
            )
        entries.append(
            {
                "media_use_decision_id": decision.id,
                "position": row.position,
                "role": decision.role,
                "caption": row.caption,
                "on_screen_credit": credit,
                "on_screen_notice": notice,
                "requires_notice": bool(notice),
                "content_hash": asset.content_hash if asset else "",
                "blob_path": asset.blob_path if asset else "",
                "license_id": evidence.license_id if evidence else "",
            }
        )
    return entries


def build_edition(
    session: Session,
    article: ArticleRevision,
    *,
    research_run,
    profile=None,
    show_name: str = "",
    now=_utcnow,
) -> VideoEdition:
    """Derive a video edition from an approved article revision.

    Refuses on anything but a still-valid approval. A video is the most widely
    shared form the newsroom produces; deriving one from a draft or from an
    article whose evidence has since weakened would put the least reviewable
    output in front of the largest audience.
    """
    if article.status != ArticleStatus.APPROVED.value:
        raise EditionBlocked(f"Article revision is {article.status}, not approved")
    blockers = export_blockers(session, article)
    if blockers:
        raise EditionBlocked("; ".join(blockers))

    content, evidence, media = current_article_state(
        session, article, research_run=research_run
    )
    drift = [
        name
        for name, stored, current in (
            ("text", article.content_hash, content),
            ("evidence", article.evidence_hash, evidence),
            ("media", article.media_hash, media),
        )
        if stored != current
    ]
    if drift:
        raise EditionBlocked(
            "The approved article no longer matches its stored state: " + ", ".join(drift)
        )

    revision = session.get(EditorialRevision, article.editorial_revision_id)
    plans = plan_segments(session, article)
    digest = script_hash(plans)
    media_entries = _media_plan(session, revision)

    existing = (
        session.query(VideoEdition)
        .filter_by(article_revision_id=article.id, script_hash=digest, media_hash=media)
        .one_or_none()
    )
    if existing is not None:
        if existing.profile != (getattr(profile, "name", "") or ""):
            raise EditionBlocked("An existing edition belongs to another profile")
        return existing

    edition = VideoEdition(
        edition_id=str(uuid.uuid4()),
        article_revision_id=article.id,
        language=article.language,
        profile=getattr(profile, "name", "") or "",
        show_name=show_name or getattr(profile, "display_name", "") or "",
        script_hash=digest,
        content_hash=content,
        evidence_hash=evidence,
        media_hash=media,
        status=EditionStatus.DRAFT.value,
        created_at=now(),
    )
    session.add(edition)
    session.flush()

    for plan in plans:
        segment = EditionSegment(
            video_edition_id=edition.id,
            position=plan.position,
            role=plan.role.value,
            purpose=plan.purpose.value,
            text=plan.text,
            source_paragraph_position=plan.source_paragraph_position,
        )
        session.add(segment)
        session.flush()
        for claim_revision_id in plan.claim_revision_ids:
            session.add(
                EditionSegmentClaim(
                    edition_segment_id=segment.id,
                    claim_revision_id=claim_revision_id,
                )
            )

    for entry in media_entries:
        session.add(
            EditionMedia(
                video_edition_id=edition.id,
                media_use_decision_id=entry["media_use_decision_id"],
                position=entry["position"],
                role=entry["role"],
                caption=entry["caption"],
                on_screen_credit=entry["on_screen_credit"],
                on_screen_notice=entry["on_screen_notice"],
                requires_notice=entry["requires_notice"],
                content_hash=entry["content_hash"],
            )
        )

    session.commit()
    return edition


def edition_segments(session: Session, edition: VideoEdition) -> Sequence[EditionSegment]:
    return (
        session.query(EditionSegment)
        .filter_by(video_edition_id=edition.id)
        .order_by(EditionSegment.position)
        .all()
    )


def edition_media(session: Session, edition: VideoEdition) -> Sequence[EditionMedia]:
    return (
        session.query(EditionMedia)
        .filter_by(video_edition_id=edition.id)
        .order_by(EditionMedia.position)
        .all()
    )


def segment_claims(session: Session, segment: EditionSegment) -> tuple[int, ...]:
    rows = (
        session.query(EditionSegmentClaim)
        .filter_by(edition_segment_id=segment.id)
        .order_by(EditionSegmentClaim.id)
        .all()
    )
    return tuple(row.claim_revision_id for row in rows)


def broadcast_script(session: Session, edition: VideoEdition) -> BroadcastScript:
    """The edition in the schema the existing renderer already understands.

    Reusing ``BroadcastScript`` is the whole point: scene planning, TTS and the
    renderer stay untouched, so the new route cannot quietly become a second
    production path with its own bugs.
    """
    article = session.get(ArticleRevision, edition.article_revision_id)
    segments = edition_segments(session, edition)
    story = ScriptStory(
        story_id=edition.edition_id,
        source_story_id=article.article_revision_id,
        order=1,
        display_headline=article.title,
        display_summary=article.lede,
        speaker_sequence=[
            SpeakerSegment(
                role=SpeakerRole(segment.role),
                purpose=SegmentPurpose(segment.purpose),
                text=segment.text,
            )
            for segment in segments
        ],
        grounding_references=[article.article_revision_id],
    )
    return BroadcastScript(
        episode_id=_episode_identifier(session, edition),
        profile=edition.profile,
        show_name=edition.show_name,
        stories=[story],
        generated_by="editorial_adapter",
    )


def edition_blockers(
    session: Session,
    edition: VideoEdition,
    *,
    research_run,
) -> tuple[str, ...]:
    """Everything that stands between this edition and a decision."""
    reasons: list[str] = []
    article = session.get(ArticleRevision, edition.article_revision_id)
    if article is None:
        return ("The article revision this edition was derived from is gone",)
    if article.status != ArticleStatus.APPROVED.value:
        reasons.append(f"Article revision is {article.status}, not approved")
    reasons.extend(export_blockers(session, article))

    content, evidence, media = current_article_state(
        session, article, research_run=research_run
    )
    if content != edition.content_hash:
        reasons.append("The article text changed after this edition was built")
    if evidence != edition.evidence_hash:
        reasons.append("The evidence behind the article changed")
    if media != edition.media_hash:
        reasons.append("The cleared media changed")

    try:
        plans = plan_segments(session, article)
    except EditionBlocked as exc:
        reasons.append(str(exc))
    else:
        if script_hash(plans) != edition.script_hash:
            reasons.append("The script no longer follows from the article")
        stored_plans = tuple(
            SegmentPlan(
                row.position, SpeakerRole(row.role), SegmentPurpose(row.purpose),
                row.text, row.source_paragraph_position, segment_claims(session, row),
            )
            for row in edition_segments(session, edition)
        )
        if stored_plans != plans:
            reasons.append("Stored speaker parts or claim mappings changed")

    expected_media = _media_plan(session, session.get(
        EditorialRevision, article.editorial_revision_id
    ))
    actual_media = edition_media(session, edition)
    if len(actual_media) != len(expected_media):
        reasons.append("Edition media set changed")
    for row in actual_media:
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        if decision is None or decision.revoked_at is not None:
            reasons.append(f"Media at position {row.position} is no longer cleared")
        if row.requires_notice and not row.on_screen_notice:
            reasons.append(f"Media at position {row.position} needs a visible notice")
        expected = next(
            (entry for entry in expected_media if entry["position"] == row.position), None
        )
        if expected is None or any(
            getattr(row, key) != expected[key]
            for key in (
                "media_use_decision_id", "role", "caption", "on_screen_credit",
                "on_screen_notice", "requires_notice", "content_hash",
            )
        ):
            reasons.append(f"Media at position {row.position} differs from its approved use")
    return tuple(reasons)


def refresh_edition_status(
    session: Session,
    edition: VideoEdition,
    *,
    research_run,
) -> VideoEdition:
    """Let a change upstream take an approval away again."""
    reasons = edition_blockers(session, edition, research_run=research_run)
    if reasons:
        edition.status = EditionStatus.BLOCKED.value
        edition.block_reason = "; ".join(reasons)
    elif edition.status == EditionStatus.BLOCKED.value:
        edition.status = EditionStatus.DRAFT.value
        edition.block_reason = None
    session.commit()
    return edition


def _record(
    session: Session,
    edition: VideoEdition,
    *,
    kind: EditionDecisionKind,
    decision: EditionDecisionType,
    operator_ref: str,
    note: str,
    video_path: str | None = None,
    now=_utcnow,
) -> EditionDecision:
    row = EditionDecision(
        decision_id=str(uuid.uuid4()),
        video_edition_id=edition.id,
        kind=kind.value,
        decision=decision.value,
        operator_ref=operator_ref,
        note=note,
        script_hash=edition.script_hash,
        content_hash=edition.content_hash,
        evidence_hash=edition.evidence_hash,
        media_hash=edition.media_hash,
        video_path=video_path,
        created_at=now(),
    )
    session.add(row)
    return row


def approve_script(
    session: Session,
    edition: VideoEdition,
    *,
    operator_ref: str,
    research_run,
    note: str = "",
    now=_utcnow,
) -> EditionDecision:
    """A named person accepts the spoken text. This renders nothing."""
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Approving a script needs a named operator")
    reasons = edition_blockers(session, edition, research_run=research_run)
    if reasons:
        raise StaleEditionApproval("; ".join(reasons))
    decision = _record(
        session,
        edition,
        kind=EditionDecisionKind.SCRIPT,
        decision=EditionDecisionType.APPROVE,
        operator_ref=operator_ref,
        note=note,
        now=now,
    )
    edition.status = EditionStatus.SCRIPT_APPROVED.value
    edition.block_reason = None
    session.commit()
    return decision


def reject_script(
    session: Session,
    edition: VideoEdition,
    *,
    operator_ref: str,
    note: str,
    now=_utcnow,
) -> EditionDecision:
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Rejecting a script needs a named operator")
    if not note.strip():
        raise ValueError("A rejection needs a reason that can be read later")
    decision = _record(
        session,
        edition,
        kind=EditionDecisionKind.SCRIPT,
        decision=EditionDecisionType.REJECT,
        operator_ref=operator_ref,
        note=note,
        now=now,
    )
    edition.status = EditionStatus.BLOCKED.value
    edition.block_reason = note
    session.commit()
    return decision


def approve_final_video(
    session: Session,
    edition: VideoEdition,
    *,
    operator_ref: str,
    video_path: str | Path,
    research_run,
    note: str = "",
    now=_utcnow,
) -> EditionDecision:
    """A named person accepts one specific rendered file.

    Deliberately separate from the script decision and from publication: this
    approves a file, it does not hand it to anyone. Nothing in this module
    talks to a video platform.
    """
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Approving a video needs a named operator")
    if edition.status != EditionStatus.SCRIPT_APPROVED.value:
        raise EditionError(
            f"The script must be approved first; edition is {edition.status}"
        )
    path = Path(video_path)
    if not path.is_file():
        raise EditionError(f"No rendered file at {path}")
    reasons = edition_blockers(session, edition, research_run=research_run)
    if reasons:
        raise StaleEditionApproval("; ".join(reasons))
    require_script_approval(session, edition, research_run=research_run)
    require_video_media_approval(session, edition)
    video_digest = file_hash(path)
    input_digest = None
    if edition.episode_id:
        from btcedu.core.editorial.ingest import canonical_hash
        from btcedu.core.editorial.production import production_inputs

        if path.name != "draft.mp4" or path.parent.name != "render" or path.is_symlink():
            raise EditionError("Approve the bound episode's render/draft.mp4")
        if path.parent.parent.name != _episode_identifier(session, edition):
            raise EditionError("Final video belongs to another episode")
        input_digest = canonical_hash(production_inputs(path.parent.parent))
    decision = _record(
        session,
        edition,
        kind=EditionDecisionKind.FINAL_VIDEO,
        decision=EditionDecisionType.APPROVE,
        operator_ref=operator_ref,
        note=note,
        video_path=str(path),
        now=now,
    )
    decision.video_sha256 = video_digest
    decision.render_input_hash = input_digest
    edition.status = EditionStatus.FINAL_APPROVED.value
    edition.final_video_path = str(path)
    session.commit()
    return decision


def assert_no_private_material(paths: Iterable[Path]) -> None:
    """Refuse to ship research evidence or contracts to a remote runner.

    The remote renderer is outside the newsroom's trust boundary. What it needs
    is pictures and audio; what it must never receive is the correspondence
    that establishes why they may be used.
    """
    for path in paths:
        parts = {part.lower() for part in Path(path).resolve().parts}
        hit = parts.intersection(PRIVATE_PATH_MARKERS)
        if hit:
            raise PrivateMaterialInPackage(
                f"{path} lies under private material ({', '.join(sorted(hit))})"
            )


def collect_render_inputs(
    session: Session,
    edition: VideoEdition,
    *,
    dest: Path,
    research_run,
) -> dict:
    """Write exactly the approved inputs a render needs, and nothing else.

    Returns the manifest as plain data. Media files are copied under
    ``dest/media`` so the package can be inspected as a whole before it is sent
    anywhere.
    """
    reasons = edition_blockers(session, edition, research_run=research_run)
    if reasons:
        raise StaleEditionApproval("; ".join(reasons))
    require_script_approval(session, edition, research_run=research_run)
    require_video_media_approval(session, edition)

    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise EditionError("Render input destination must be empty")
    media_dir = dest / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    script = broadcast_script(session, edition)
    entries: list[dict] = []
    for row in edition_media(session, edition):
        decision = session.get(MediaUseDecision, row.media_use_decision_id)
        asset = session.get(NewsroomMediaAsset, decision.media_asset_id)
        source = Path(asset.blob_path)
        assert_no_private_material([source])
        target = media_dir / f"{row.position:02d}{source.suffix}"
        if not source.is_file() or source.is_symlink():
            raise EditionError("Approved media file is missing or is a symlink")
        if file_hash(source) != row.content_hash:
            raise EditionError("Approved media bytes changed")
        shutil.copy2(source, target)
        entries.append(
            {
                "position": row.position,
                "file": str(target.relative_to(dest)),
                "role": row.role,
                "caption": row.caption,
                "credit": row.on_screen_credit,
                "notice": row.on_screen_notice,
                "content_hash": row.content_hash,
            }
        )

    manifest = {
        "edition_id": edition.edition_id,
        "language": edition.language,
        "profile": edition.profile,
        "script_hash": edition.script_hash,
        "content_hash": edition.content_hash,
        "evidence_hash": edition.evidence_hash,
        "media_hash": edition.media_hash,
        "script": script.model_dump(mode="json"),
        "media": entries,
    }
    (dest / "render_inputs.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert_no_private_material(sorted(dest.rglob("*")))
    return manifest


def file_hash(path: str | Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _episode_identifier(session: Session, edition: VideoEdition) -> str:
    from btcedu.models.episode import Episode

    episode = session.get(Episode, edition.episode_id) if edition.episode_id else None
    return episode.episode_id if episode else edition.edition_id


def require_script_approval(session: Session, edition: VideoEdition, *, research_run) -> None:
    reasons = edition_blockers(session, edition, research_run=research_run)
    decision = (
        session.query(EditionDecision)
        .filter_by(video_edition_id=edition.id, kind=EditionDecisionKind.SCRIPT.value)
        .order_by(EditionDecision.id.desc()).first()
    )
    if reasons:
        raise StaleEditionApproval("; ".join(reasons))
    if decision is None or decision.decision != EditionDecisionType.APPROVE.value:
        raise EditionBlocked("A separate script approval is required")
    if any(getattr(decision, name) != getattr(edition, name) for name in (
        "script_hash", "content_hash", "evidence_hash", "media_hash"
    )):
        raise StaleEditionApproval("Script approval no longer matches the edition")


def approve_video_media(
    session: Session, edition: VideoEdition, *, operator_ref: str, research_run,
    note: str,
) -> EditionDecision:
    """Explicitly clear the selected pictures for the named video profile and crop."""
    if not operator_ref.strip() or not note.strip() or not edition.profile:
        raise EditionBlocked("Video media approval requires operator, rationale and profile")
    reasons = edition_blockers(session, edition, research_run=research_run)
    if reasons:
        raise StaleEditionApproval("; ".join(reasons))
    for row in edition_media(session, edition):
        use = session.get(MediaUseDecision, row.media_use_decision_id)
        license = session.get(LicenseEvidence, use.license_evidence_id)
        if not license or not license.commercial_use_allowed or not license.derivatives_allowed:
            raise EditionBlocked("Media is not cleared for commercial video/crop")
    decision = _record(
        session, edition, kind=EditionDecisionKind.MEDIA,
        decision=EditionDecisionType.APPROVE, operator_ref=operator_ref,
        note=json.dumps({"profile": edition.profile, "rationale": note}),
    )
    session.commit()
    return decision


def require_video_media_approval(session: Session, edition: VideoEdition) -> None:
    if not edition_media(session, edition):
        return
    decision = (
        session.query(EditionDecision)
        .filter_by(video_edition_id=edition.id, kind=EditionDecisionKind.MEDIA.value)
        .order_by(EditionDecision.id.desc()).first()
    )
    if (
        decision is None or decision.decision != EditionDecisionType.APPROVE.value
        or decision.media_hash != edition.media_hash
        or json.loads(decision.note).get("profile") != edition.profile
    ):
        raise EditionBlocked("A separate video/profile media approval is required")


def edition_metadata(session: Session, edition: VideoEdition) -> dict:
    """Description fields for a later, manual upload.

    Credits and archive notices belong in the description as well as on the
    picture: a viewer who checks where a picture came from should not have to
    pause the video to read it.
    """
    article = session.get(ArticleRevision, edition.article_revision_id)
    credits = []
    notices = []
    for row in edition_media(session, edition):
        if row.on_screen_credit:
            credits.append(f"{row.position}. {row.on_screen_credit}")
        if row.on_screen_notice:
            notices.append(f"{row.position}. {row.on_screen_notice}")
    return {
        "title": article.title,
        "summary": article.lede,
        "language": edition.language,
        "credits": credits,
        "notices": notices,
        "article_revision_id": article.article_revision_id,
        "edition_id": edition.edition_id,
    }
