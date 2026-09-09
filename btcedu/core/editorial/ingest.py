"""Idempotently snapshot a pipeline story for editorial work."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from btcedu.models.editorial import (
    Claim,
    ClaimOrigin,
    ClaimRevision,
    SourceItem,
    SourceRevision,
    SourceSpan,
    Topic,
    TopicSource,
)
from btcedu.models.editorial_schema import ClaimDraft
from btcedu.models.story_schema import Story


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ImportedStory:
    source_item: SourceItem
    source_revision: SourceRevision
    source_span: SourceSpan
    topic: Topic
    claim_revisions: tuple[ClaimRevision, ...]


def import_story(
    session: Session,
    *,
    episode_id: str,
    story: Story,
    source_language: str = "de",
    source_uri: str | None = None,
    published_at: datetime | None = None,
    claims: Iterable[ClaimDraft] = (),
    target_topic: Topic | None = None,
) -> ImportedStory:
    """Create immutable revisions while reusing an identical prior import."""
    source_key = f"episode:{episode_id}:story:{story.story_id}"
    source_item = session.query(SourceItem).filter_by(source_key=source_key).first()
    if source_item is None:
        source_item = SourceItem(
            source_id=_new_id(),
            source_key=source_key,
            source_type="episode_story",
            episode_id=episode_id,
        )
        session.add(source_item)
        session.flush()

    source_text = story.source_text or story.text_de
    revision_payload = {
        "schema_version": "1.0",
        "language": source_language,
        "title": story.headline_de,
        "source_text": source_text,
        "source_uri": source_uri,
        "published_at": published_at.isoformat() if published_at else None,
        "source_segment_ids": story.source_segment_ids,
        "source_start_seconds": story.source_start_seconds,
        "source_end_seconds": story.source_end_seconds,
    }
    revision_hash = canonical_hash(revision_payload)
    source_revision = (
        session.query(SourceRevision)
        .filter_by(source_item_id=source_item.id, content_hash=revision_hash)
        .first()
    )
    if source_revision is None:
        source_revision = SourceRevision(
            revision_id=_new_id(),
            source_item_id=source_item.id,
            content_hash=revision_hash,
            schema_version="1.0",
            language=source_language,
            title=story.headline_de,
            source_text=source_text,
            source_uri=source_uri,
            published_at=published_at,
        )
        session.add(source_revision)
        session.flush()

    span_key = f"story:{story.story_id}"
    source_span = (
        session.query(SourceSpan)
        .filter_by(source_revision_id=source_revision.id, span_key=span_key)
        .first()
    )
    if source_span is None:
        source_span = SourceSpan(
            span_id=_new_id(),
            source_revision_id=source_revision.id,
            span_key=span_key,
            story_id=story.story_id,
            source_segment_ids_json=json.dumps(
                story.source_segment_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            source_text=source_text,
            text_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            start_seconds=story.source_start_seconds,
            end_seconds=story.source_end_seconds,
        )
        session.add(source_span)
        session.flush()

    topic = target_topic or session.query(Topic).filter_by(topic_key=source_key).first()
    if topic is None:
        topic = Topic(topic_id=_new_id(), topic_key=source_key, title=story.headline_de)
        session.add(topic)
        session.flush()
    if (
        session.query(TopicSource)
        .filter_by(topic_id=topic.id, source_revision_id=source_revision.id)
        .first()
        is None
    ):
        session.add(TopicSource(topic_id=topic.id, source_revision_id=source_revision.id))

    claim_revisions = tuple(
        _import_claim(session, topic=topic, span=source_span, draft=draft)
        for draft in claims
    )
    session.commit()
    return ImportedStory(source_item, source_revision, source_span, topic, claim_revisions)


def _import_claim(
    session: Session,
    *,
    topic: Topic,
    span: SourceSpan,
    draft: ClaimDraft,
) -> ClaimRevision:
    claim = session.query(Claim).filter_by(topic_id=topic.id, claim_key=draft.claim_key).first()
    if claim is None:
        claim = Claim(claim_id=_new_id(), topic_id=topic.id, claim_key=draft.claim_key)
        session.add(claim)
        session.flush()

    payload = draft.model_dump(mode="json", exclude={"claim_key"})
    content_hash = canonical_hash(payload)
    revision = (
        session.query(ClaimRevision)
        .filter_by(claim_id=claim.id, content_hash=content_hash)
        .first()
    )
    if revision is None:
        latest = (
            session.query(ClaimRevision.revision_number)
            .filter_by(claim_id=claim.id)
            .order_by(ClaimRevision.revision_number.desc())
            .first()
        )
        revision = ClaimRevision(
            revision_id=_new_id(),
            claim_id=claim.id,
            content_hash=content_hash,
            revision_number=(latest[0] if latest else 0) + 1,
            **payload,
        )
        session.add(revision)
        session.flush()
    if (
        session.query(ClaimOrigin)
        .filter_by(claim_revision_id=revision.id, source_span_id=span.id)
        .first()
        is None
    ):
        session.add(ClaimOrigin(claim_revision_id=revision.id, source_span_id=span.id))
    return revision
