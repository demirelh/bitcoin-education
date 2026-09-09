"""Continuing stories: proposals, merges and reversible operator decisions (N6).

The hard part is not linking two broadcasts, it is refusing to. A shared date
or a shared name is the normal case in a news bulletin — two unrelated items
about the same minister on the same evening — so neither counts on its own.
A proposal is therefore never a merge: an operator decides, and the decision
stays reversible because a wrong fusion is otherwise unrepairable.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from btcedu.models.editorial import SourceRevision, Topic, TopicSource
from btcedu.models.topic_graph import (
    AliasKind,
    MergeStatus,
    ProposalStatus,
    TopicAlias,
    TopicMerge,
    UpdateProposal,
)

# Words that carry no topic identity. A bulletin is full of them, and matching
# on them alone links every item of an evening to every other.
STOPWORDS = frozenset(
    {
        "ve",
        "ile",
        "için",
        "bir",
        "bu",
        "da",
        "de",
        "der",
        "die",
        "das",
        "und",
        "mit",
        "von",
        "the",
        "and",
        "for",
    }
)

MIN_KEYWORD_OVERLAP = 2
MIN_SIGNALS = 2
NEARBY_DAYS = 7


class TopicGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchSignals:
    keywords: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    date_distance_days: int | None = None

    @property
    def names(self) -> tuple[str, ...]:
        found = []
        if len(self.keywords) >= MIN_KEYWORD_OVERLAP:
            found.append("keywords")
        if self.entities:
            found.append("entity")
        if self.date_distance_days is not None and self.date_distance_days <= NEARBY_DAYS:
            found.append("date")
        return tuple(found)

    @property
    def score(self) -> float:
        return round(
            min(1.0, 0.2 * len(self.keywords) + 0.3 * len(self.entities)),
            3,
        )

    def sufficient(self) -> bool:
        """Two independent signals, and never date or entity on their own.

        A keyword overlap is what distinguishes "the same story" from "the same
        evening": without it, one minister's name would fuse a resignation and
        an unrelated visit into a single running topic.
        """
        return "keywords" in self.names and len(self.names) >= MIN_SIGNALS


def normalize_terms(text: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = [word for word in re.split(r"[^a-z0-9]+", ascii_only) if len(word) > 2]
    return tuple(word for word in words if word not in STOPWORDS)


def aliases_for(session: Session, topic: Topic) -> list[TopicAlias]:
    return session.query(TopicAlias).filter_by(topic_id=topic.id).all()


def add_alias(
    session: Session, topic: Topic, *, kind: AliasKind, value: str
) -> TopicAlias:
    value = value.strip()
    if not value:
        raise ValueError("An alias needs a value")
    existing = (
        session.query(TopicAlias)
        .filter_by(topic_id=topic.id, kind=kind.value, value=value)
        .one_or_none()
    )
    if existing is not None:
        return existing
    alias = TopicAlias(topic_id=topic.id, kind=kind.value, value=value)
    session.add(alias)
    session.commit()
    return alias


def match_signals(
    session: Session, topic: Topic, revision: SourceRevision
) -> MatchSignals:
    aliases = aliases_for(session, topic)
    entities = {
        alias.value.casefold() for alias in aliases if alias.kind == AliasKind.ENTITY.value
    }
    keywords = {
        alias.value.casefold()
        for alias in aliases
        if alias.kind == AliasKind.KEYWORD.value
    }
    keywords |= set(normalize_terms(topic.title))
    # The entity's own name must not also count as a keyword. Otherwise one
    # matched name produces two "independent" signals and a minister's every
    # appearance joins one running topic.
    entity_terms = {term for entity in entities for term in normalize_terms(entity)}
    keywords -= entity_terms

    candidate_terms = set(normalize_terms(revision.title))
    candidate_terms |= set(normalize_terms(revision.source_text[:2000]))

    matched_keywords = tuple(sorted(keywords & candidate_terms))
    matched_entities = tuple(
        sorted(entity for entity in entities if entity in candidate_terms)
    )

    distance = None
    dates = [
        alias.value
        for alias in aliases
        if alias.kind == AliasKind.EVENT_DATE.value
    ]
    if revision.published_at is not None and dates:
        for value in dates:
            try:
                anchor = datetime.fromisoformat(value).date()
            except ValueError:
                continue
            days = abs((revision.published_at.date() - anchor).days)
            distance = days if distance is None else min(distance, days)

    return MatchSignals(
        keywords=matched_keywords,
        entities=matched_entities,
        date_distance_days=distance,
    )


def propose_updates(
    session: Session,
    revision: SourceRevision,
    *,
    topics: Iterable[Topic] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_proposals: int = 5,
) -> list[UpdateProposal]:
    """Suggest that a new broadcast continues topics that already exist.

    A repeated report becomes a proposal, never a second article: publishing
    the same story twice is the failure mode a reader notices first.
    """
    candidates = list(topics) if topics is not None else session.query(Topic).all()
    scored: list[tuple[MatchSignals, Topic]] = []
    for topic in candidates:
        if _is_merged_away(session, topic):
            continue
        if _already_carries(session, topic, revision):
            continue
        signals = match_signals(session, topic, revision)
        if signals.sufficient():
            scored.append((signals, topic))

    scored.sort(key=lambda pair: pair[0].score, reverse=True)
    created: list[UpdateProposal] = []
    for signals, topic in scored[:max_proposals]:
        existing = (
            session.query(UpdateProposal)
            .filter_by(topic_id=topic.id, source_revision_id=revision.id)
            .one_or_none()
        )
        if existing is not None:
            created.append(existing)
            continue
        proposal = UpdateProposal(
            proposal_id=str(uuid.uuid4()),
            topic_id=topic.id,
            source_revision_id=revision.id,
            score=signals.score,
            signals=json.dumps(
                {
                    "keywords": list(signals.keywords),
                    "entities": list(signals.entities),
                    "date_distance_days": signals.date_distance_days,
                },
                ensure_ascii=False,
            ),
            created_at=now(),
        )
        session.add(proposal)
        created.append(proposal)
    session.commit()
    return created


def _already_carries(session: Session, topic: Topic, revision: SourceRevision) -> bool:
    return (
        session.query(TopicSource)
        .filter_by(topic_id=topic.id, source_revision_id=revision.id)
        .count()
        > 0
    )


def _is_merged_away(session: Session, topic: Topic) -> bool:
    return (
        session.query(TopicMerge)
        .filter_by(merged_topic_id=topic.id, status=MergeStatus.ACTIVE.value)
        .count()
        > 0
    )


def accept_proposal(
    session: Session,
    proposal: UpdateProposal,
    *,
    operator_ref: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TopicSource:
    """Attach the new source to the topic. This publishes nothing."""
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Accepting a proposal needs a named operator")
    if proposal.status != ProposalStatus.OPEN.value:
        raise TopicGraphError(f"Proposal is already {proposal.status}")
    link = (
        session.query(TopicSource)
        .filter_by(
            topic_id=proposal.topic_id, source_revision_id=proposal.source_revision_id
        )
        .one_or_none()
    )
    if link is None:
        link = TopicSource(
            topic_id=proposal.topic_id,
            source_revision_id=proposal.source_revision_id,
        )
        session.add(link)
    proposal.status = ProposalStatus.ACCEPTED.value
    proposal.operator_ref = operator_ref
    proposal.decided_at = now()
    session.commit()
    from btcedu.core.editorial.recheck import scan_changes

    scan_changes(session, topic_ids=[proposal.topic_id])
    return link


def reject_proposal(
    session: Session,
    proposal: UpdateProposal,
    *,
    operator_ref: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> UpdateProposal:
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Rejecting a proposal needs a named operator")
    if proposal.status != ProposalStatus.OPEN.value:
        raise TopicGraphError(f"Proposal is already {proposal.status}")
    proposal.status = ProposalStatus.REJECTED.value
    proposal.operator_ref = operator_ref
    proposal.decided_at = now()
    session.commit()
    return proposal


def merge_topics(
    session: Session,
    *,
    primary: Topic,
    merged: Topic,
    operator_ref: str,
    rationale: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TopicMerge:
    """Declare two topics one story, without losing either of them.

    Sources are linked to the primary topic; the merged topic keeps its rows so
    a revert is a decision change rather than a reconstruction.
    """
    if primary.id == merged.id:
        raise TopicGraphError("A topic cannot be merged into itself")
    if not operator_ref or not operator_ref.strip():
        raise ValueError("A merge needs a named operator")
    if not rationale.strip():
        raise ValueError("A merge needs a rationale that can be read later")
    if _is_merged_away(session, primary):
        raise TopicGraphError("The primary topic is itself merged away")
    if _is_merged_away(session, merged):
        raise TopicGraphError("The secondary topic is already merged away")

    added: list[int] = []
    for link in session.query(TopicSource).filter_by(topic_id=merged.id).all():
        exists = (
            session.query(TopicSource)
            .filter_by(topic_id=primary.id, source_revision_id=link.source_revision_id)
            .count()
        )
        if not exists:
            session.add(
                TopicSource(
                    topic_id=primary.id, source_revision_id=link.source_revision_id
                )
            )
            added.append(link.source_revision_id)

    merge = TopicMerge(
        merge_id=str(uuid.uuid4()),
        primary_topic_id=primary.id,
        merged_topic_id=merged.id,
        status=MergeStatus.ACTIVE.value,
        operator_ref=operator_ref,
        rationale=rationale.strip(),
        added_source_ids=json.dumps(sorted(added)),
        created_at=now(),
    )
    session.add(merge)
    session.commit()
    return merge


def revert_merge(
    session: Session,
    merge: TopicMerge,
    *,
    operator_ref: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TopicMerge:
    """Split the topics again, leaving the wrong decision on the record."""
    if not operator_ref or not operator_ref.strip():
        raise ValueError("Reverting a merge needs a named operator")
    if merge.status != MergeStatus.ACTIVE.value:
        raise TopicGraphError(f"Merge is already {merge.status}")

    added = set(json.loads(merge.added_source_ids or "[]"))
    for link in (
        session.query(TopicSource).filter_by(topic_id=merge.primary_topic_id).all()
    ):
        if link.source_revision_id in added:
            session.delete(link)

    merge.status = MergeStatus.REVERTED.value
    merge.reverted_at = now()
    merge.reverted_by = operator_ref
    session.commit()
    return merge


def canonical_topic(session: Session, topic: Topic) -> Topic:
    """Follow active merges to the topic that currently carries the story."""
    seen: set[int] = set()
    current = topic
    while True:
        if current.id in seen:
            raise TopicGraphError("Merge cycle detected")
        seen.add(current.id)
        merge = (
            session.query(TopicMerge)
            .filter_by(merged_topic_id=current.id, status=MergeStatus.ACTIVE.value)
            .order_by(TopicMerge.id.desc())
            .first()
        )
        if merge is None:
            return current
        current = session.get(Topic, merge.primary_topic_id)


def recent_revisions(
    session: Session, *, since: datetime, limit: int = 50
) -> Sequence[SourceRevision]:
    return (
        session.query(SourceRevision)
        .filter(SourceRevision.retrieved_at >= since)
        .order_by(SourceRevision.id.desc())
        .limit(limit)
        .all()
    )


def default_window(now: datetime, *, days: int = NEARBY_DAYS) -> datetime:
    return now - timedelta(days=days)
