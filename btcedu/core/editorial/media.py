"""Asset-bound rights decisions for editorial illustrations.

Ranking never repairs a rights or context problem. Candidates that are not
demonstrably usable are excluded before anything is ranked, and "no suitable
picture" is a valid, final outcome — this module never falls back to a paid or
generated substitute.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.core.editorial.ingest import canonical_hash
from btcedu.models.editorial import EditorialRevision
from btcedu.models.media_rights import (
    LicenseEvidence,
    LicenseFamily,
    MediaRole,
    MediaSourceOffer,
    MediaUseDecision,
    MediaUseStatus,
    NewsroomMediaAsset,
    RevisionMedia,
)
from btcedu.services.commons_service import MediaCandidate, MediaCatalogProvider
from btcedu.services.document_fetcher import DocumentFetcher, DocumentFetchError

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000


@dataclass(frozen=True)
class LicensePolicy:
    family: LicenseFamily
    license_id: str
    version: str | None
    attribution_required: bool
    share_alike: bool
    commercial_use_allowed: bool
    derivatives_allowed: bool
    usable: bool
    reason: str


@dataclass(frozen=True)
class MediaRequirement:
    """What the article actually needs a picture to show."""

    subject: str
    role: MediaRole
    event_date: datetime | None = None
    location: str | None = None
    date_tolerance_days: int = 3
    allow_cropping: bool = True
    caption: str = ""


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: MediaCandidate
    policy: LicensePolicy
    eligible: bool
    reasons: tuple[str, ...]
    score: int = 0


@dataclass
class MediaSelection:
    """Outcome of one bounded illustration search."""

    decision: MediaUseDecision | None = None
    assessments: tuple[CandidateAssessment, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    reason: str = ""
    from_cache: bool = False

    @property
    def has_picture(self) -> bool:
        return self.decision is not None and self.decision.status == MediaUseStatus.APPROVED.value


_LICENSE_PATTERNS: tuple[tuple[re.Pattern[str], LicenseFamily], ...] = (
    (re.compile(r"\bcc[\s\-_]*0\b|creative commons zero", re.I), LicenseFamily.CC0),
    (re.compile(r"public\s*domain|\bpd[\s\-]", re.I), LicenseFamily.PUBLIC_DOMAIN),
    (re.compile(r"\bcc[\s\-_]*by[\s\-_]*nc", re.I), LicenseFamily.CC_BY_NC),
    (re.compile(r"\bcc[\s\-_]*by[\s\-_]*nd", re.I), LicenseFamily.CC_BY_ND),
    (re.compile(r"\bcc[\s\-_]*by[\s\-_]*sa", re.I), LicenseFamily.CC_BY_SA),
    (re.compile(r"\bcc[\s\-_]*by\b", re.I), LicenseFamily.CC_BY),
)

_FAMILY_RULES: dict[LicenseFamily, tuple[bool, bool, bool, bool, bool]] = {
    # family: (attribution_required, share_alike, commercial, derivatives, usable)
    LicenseFamily.CC0: (False, False, True, True, True),
    LicenseFamily.PUBLIC_DOMAIN: (False, False, True, True, True),
    LicenseFamily.CC_BY: (True, False, True, True, True),
    LicenseFamily.CC_BY_SA: (True, True, True, True, True),
    LicenseFamily.CC_BY_NC: (True, False, False, True, False),
    LicenseFamily.CC_BY_ND: (True, False, True, False, True),
    LicenseFamily.UNKNOWN: (True, False, False, False, False),
}

_VERSION = re.compile(r"(\d+\.\d+)")


def evaluate_license(candidate: MediaCandidate) -> LicensePolicy:
    """Read a catalogue licence claim into an explicit permission set.

    Several licences may be offered at once. The most permissive *recognised*
    one is used, but an unrecognised statement is never treated as permissive:
    an unclear licence stays unusable rather than defaulting to free.
    """
    statement = (candidate.license_id or "").strip()
    if not statement:
        return _policy(LicenseFamily.UNKNOWN, "", None, "No licence was stated")

    parts = [part.strip() for part in re.split(r"[,;/]| or ", statement) if part.strip()]
    matched: list[tuple[LicenseFamily, str]] = []
    unrecognised: list[str] = []
    for part in parts:
        family = _match_family(part)
        if family is None:
            unrecognised.append(part)
        else:
            matched.append((family, part))

    if not matched:
        return _policy(
            LicenseFamily.UNKNOWN,
            statement,
            candidate.license_version,
            f"Licence statement {statement!r} is not a recognised free licence",
        )

    order = [
        LicenseFamily.CC0,
        LicenseFamily.PUBLIC_DOMAIN,
        LicenseFamily.CC_BY,
        LicenseFamily.CC_BY_SA,
        LicenseFamily.CC_BY_ND,
        LicenseFamily.CC_BY_NC,
    ]
    family, part = min(matched, key=lambda item: order.index(item[0]))
    version = candidate.license_version or _version_of(part)
    reason = f"Licence {part!r} accepted"
    if unrecognised:
        reason = f"Licence {part!r} accepted; unrecognised alternatives kept on record"
    return _policy(family, part, version, reason)


def _match_family(text: str) -> LicenseFamily | None:
    if re.search(r"\bcc[\s\-_]*by\b", text, re.I):
        if re.search(r"\bnc\b|non[\s-]*commercial", text, re.I):
            return LicenseFamily.CC_BY_NC
        if re.search(r"\bnd\b|no[\s-]*derivatives", text, re.I):
            return LicenseFamily.CC_BY_ND
    for pattern, family in _LICENSE_PATTERNS:
        if pattern.search(text):
            return family
    return None


def _version_of(text: str) -> str | None:
    found = _VERSION.search(text)
    return found.group(1) if found else None


def _policy(
    family: LicenseFamily,
    license_id: str,
    version: str | None,
    reason: str,
) -> LicensePolicy:
    attribution, share_alike, commercial, derivatives, usable = _FAMILY_RULES[family]
    return LicensePolicy(
        family=family,
        license_id=license_id,
        version=version,
        attribution_required=attribution,
        share_alike=share_alike,
        commercial_use_allowed=commercial,
        derivatives_allowed=derivatives,
        usable=usable,
        reason=reason,
    )


def assess_candidate(
    candidate: MediaCandidate,
    requirement: MediaRequirement,
) -> CandidateAssessment:
    """Decide whether a candidate may illustrate this requirement, and why."""
    policy = evaluate_license(candidate)
    reasons: list[str] = []
    if not policy.usable:
        reasons.append(f"rights unclear or restricted: {policy.reason}")
    if not policy.commercial_use_allowed:
        reasons.append("licence excludes commercial use")
    if requirement.allow_cropping and not policy.derivatives_allowed:
        reasons.append("licence forbids the derivative this placement needs")

    haystack = " ".join(
        part
        for part in (
            candidate.title,
            candidate.description,
            candidate.depicted_subject or "",
            " ".join(candidate.categories),
        )
        if part
    ).casefold()
    subject = requirement.subject.casefold().strip()
    if subject and subject not in haystack:
        reasons.append(f"candidate does not depict {requirement.subject!r}")

    if requirement.location:
        location = requirement.location.casefold()
        location_text = f"{haystack} {(candidate.depicted_location or '').casefold()}"
        if location not in location_text:
            reasons.append(f"candidate is not located in {requirement.location!r}")

    if requirement.role == MediaRole.EVENT:
        if candidate.captured_at is None:
            reasons.append("no capture date, so it cannot document this event")
        elif requirement.event_date is not None:
            delta = abs(candidate.captured_at - requirement.event_date)
            if delta > timedelta(days=requirement.date_tolerance_days):
                reasons.append(
                    "capture date "
                    f"{candidate.captured_at.date().isoformat()} does not match the event"
                )

    if candidate.mime_type and candidate.mime_type not in ALLOWED_IMAGE_TYPES:
        reasons.append(f"unsupported image type {candidate.mime_type}")
    if candidate.byte_size and candidate.byte_size > MAX_IMAGE_BYTES:
        reasons.append("file is larger than the newsroom image limit")

    score = 0
    if candidate.captured_at is not None:
        score += 2
    if candidate.author:
        score += 1
    if candidate.width and candidate.height:
        score += 1
    return CandidateAssessment(
        candidate=candidate,
        policy=policy,
        eligible=not reasons,
        reasons=tuple(reasons),
        score=score,
    )


class MediaBlobStore:
    """Content-addressed store: identical bytes are written exactly once."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def from_settings(cls, settings) -> MediaBlobStore:
        return cls(Path(settings.newsroom_data_dir) / "media")

    def path_for(self, content_hash: str, suffix: str) -> Path:
        return self.root / content_hash[:2] / f"{content_hash}{suffix}"

    def store(self, content_hash: str, body: bytes, suffix: str) -> Path:
        if hashlib.sha256(body).hexdigest() != content_hash:
            raise BrokenImage("Media bytes do not match the content address")
        path = self.path_for(content_hash, suffix)
        if path.exists():
            if path.is_symlink():
                raise BrokenImage("Stored media must not be a symlink")
            with path.open("rb") as stream:
                stored_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if stored_hash != content_hash:
                raise BrokenImage("Stored media bytes changed; explicit recovery is required")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_bytes(body)
        temporary.replace(path)
        return path


_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class BrokenImage(RuntimeError):
    pass


def inspect_image(body: bytes, content_type: str) -> tuple[int, int]:
    """Confirm the bytes really are the declared image and report its size."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            width, height = image.size
            actual = Image.MIME.get(image.format or "", "")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BrokenImage("Image bytes could not be decoded") from exc
    if actual and actual != content_type:
        raise BrokenImage(
            f"Declared content type {content_type} does not match the bytes ({actual})"
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise BrokenImage("Image exceeds the newsroom pixel limit")
    return width, height


def build_attribution(
    candidate: MediaCandidate,
    policy: LicensePolicy,
    *,
    edit_note: str | None = None,
) -> str:
    """Compose the credit line, including any edit that was applied.

    A crop is a change to someone else's work, so it belongs in the credit even
    when the licence would not have required attribution at all.
    """
    parts: list[str] = []
    author = (candidate.author or candidate.credit or candidate.uploader or "").strip()
    if author:
        parts.append(author)
    if candidate.title:
        parts.append(f"“{candidate.title}”")
    parts.append(candidate.page_url)
    if policy.license_id:
        parts.append(policy.license_id if not policy.version else f"{policy.license_id}")
    if edit_note:
        parts.append(edit_note)
    return ", ".join(part for part in parts if part)


def select_media_for_revision(
    session: Session,
    *,
    editorial_revision: EditorialRevision,
    requirement: MediaRequirement,
    provider: MediaCatalogProvider,
    fetcher: DocumentFetcher,
    blob_store: MediaBlobStore,
    query: str | None = None,
    max_candidates: int = 8,
    edit_note: str | None = None,
    decided_by: str = "auto",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MediaSelection:
    """Find, verify and record one illustration for a revision.

    An already approved decision is returned unchanged, so a restart neither
    searches nor downloads again. When nothing survives the rights and context
    checks the revision simply keeps no picture.
    """
    existing = (
        session.query(MediaUseDecision)
        .filter_by(
            editorial_revision_id=editorial_revision.id,
            status=MediaUseStatus.APPROVED.value,
        )
        .order_by(MediaUseDecision.id)
        .first()
    )
    if existing is not None:
        return MediaSelection(decision=existing, reason="Existing decision reused", from_cache=True)

    candidates = tuple(provider.search(query or requirement.subject, limit=max_candidates))
    assessments = tuple(assess_candidate(candidate, requirement) for candidate in candidates)
    rejected = [
        (item.candidate.offer_key, "; ".join(item.reasons))
        for item in assessments
        if not item.eligible
    ]
    eligible = sorted(
        (item for item in assessments if item.eligible),
        key=lambda item: (-item.score, item.candidate.offer_key),
    )

    for assessment in eligible:
        candidate = assessment.candidate
        try:
            binary = fetcher.fetch_binary(
                candidate.file_url,
                allowed_content_types=ALLOWED_IMAGE_TYPES,
                max_bytes=MAX_IMAGE_BYTES,
            )
            width, height = inspect_image(binary.body, binary.content_type)
        except (DocumentFetchError, BrokenImage) as exc:
            rejected.append((candidate.offer_key, f"file could not be used: {exc}"))
            continue

        asset = _store_asset(session, binary=binary, width=width, height=height, store=blob_store)
        offer = _store_offer(session, asset=asset, candidate=candidate, retrieved_at=now())
        evidence = _store_license_evidence(
            session,
            offer=offer,
            candidate=candidate,
            policy=assessment.policy,
            retrieved_at=now(),
        )
        decision = _store_decision(
            session,
            editorial_revision=editorial_revision,
            asset=asset,
            offer=offer,
            evidence=evidence,
            requirement=requirement,
            candidate=candidate,
            policy=assessment.policy,
            edit_note=edit_note,
            decided_by=decided_by,
            now=now(),
        )
        _attach_to_revision(
            session,
            editorial_revision=editorial_revision,
            decision=decision,
            caption=requirement.caption or candidate.title,
        )
        session.commit()
        return MediaSelection(
            decision=decision,
            assessments=assessments,
            rejected=tuple(rejected),
            reason=assessment.policy.reason,
        )

    session.commit()
    return MediaSelection(
        decision=None,
        assessments=assessments,
        rejected=tuple(rejected),
        reason="No candidate satisfied the rights and context requirements",
    )


def _store_asset(session, *, binary, width, height, store: MediaBlobStore) -> NewsroomMediaAsset:
    asset = (
        session.query(NewsroomMediaAsset).filter_by(content_hash=binary.content_hash).first()
    )
    suffix = _SUFFIXES.get(binary.content_type, ".bin")
    path = store.store(binary.content_hash, binary.body, suffix)
    if asset is not None:
        return asset
    asset = NewsroomMediaAsset(
        asset_id=str(uuid.uuid4()),
        content_hash=binary.content_hash,
        mime_type=binary.content_type,
        byte_size=len(binary.body),
        width=width,
        height=height,
        blob_path=str(path),
    )
    session.add(asset)
    session.flush()
    return asset


def _store_offer(
    session,
    *,
    asset: NewsroomMediaAsset,
    candidate: MediaCandidate,
    retrieved_at: datetime,
) -> MediaSourceOffer:
    offer = (
        session.query(MediaSourceOffer)
        .filter_by(provider=candidate.provider, offer_key=candidate.offer_key)
        .first()
    )
    if offer is not None:
        if offer.media_asset_id != asset.id:
            raise ValueError("Catalogue offer changed its bytes; record a new offer revision")
        return offer
    offer = MediaSourceOffer(
        offer_id=str(uuid.uuid4()),
        media_asset_id=asset.id,
        provider=candidate.provider,
        offer_key=candidate.offer_key,
        page_url=candidate.page_url,
        file_url=candidate.file_url,
        title=candidate.title or None,
        description=candidate.description or None,
        uploader=candidate.uploader,
        author=candidate.author,
        captured_at=candidate.captured_at,
        captured_at_text=candidate.captured_at_text,
        depicted_subject=candidate.depicted_subject,
        depicted_location=candidate.depicted_location,
        retrieved_at=retrieved_at,
        metadata_json=json.dumps(dict(candidate.metadata), ensure_ascii=False, sort_keys=True),
    )
    session.add(offer)
    session.flush()
    return offer


def _store_license_evidence(
    session,
    *,
    offer: MediaSourceOffer,
    candidate: MediaCandidate,
    policy: LicensePolicy,
    retrieved_at: datetime,
) -> LicenseEvidence:
    evidence_hash = canonical_hash(
        {
            "license_family": policy.family.value,
            "license_id": policy.license_id,
            "license_version": policy.version,
            "license_url": candidate.license_url,
            "raw_statement": candidate.license_id,
            "author": candidate.author,
            "credit": candidate.credit,
        }
    )
    evidence = (
        session.query(LicenseEvidence)
        .filter_by(media_source_offer_id=offer.id, evidence_hash=evidence_hash)
        .first()
    )
    if evidence is not None:
        return evidence
    evidence = LicenseEvidence(
        evidence_id=str(uuid.uuid4()),
        media_source_offer_id=offer.id,
        license_family=policy.family.value,
        license_id=policy.license_id or "unknown",
        license_version=policy.version,
        license_url=candidate.license_url,
        attribution_required=policy.attribution_required,
        share_alike=policy.share_alike,
        commercial_use_allowed=policy.commercial_use_allowed,
        derivatives_allowed=policy.derivatives_allowed,
        credit_author=candidate.author or candidate.credit,
        credit_source=candidate.page_url,
        raw_statement=candidate.license_id or None,
        evidence_hash=evidence_hash,
        retrieved_at=retrieved_at,
    )
    session.add(evidence)
    session.flush()
    return evidence


def _store_decision(
    session,
    *,
    editorial_revision: EditorialRevision,
    asset: NewsroomMediaAsset,
    offer: MediaSourceOffer,
    evidence: LicenseEvidence,
    requirement: MediaRequirement,
    candidate: MediaCandidate,
    policy: LicensePolicy,
    edit_note: str | None,
    decided_by: str,
    now: datetime,
) -> MediaUseDecision:
    decision = (
        session.query(MediaUseDecision)
        .filter_by(editorial_revision_id=editorial_revision.id, media_asset_id=asset.id)
        .first()
    )
    attribution = build_attribution(candidate, policy, edit_note=edit_note)
    if decision is None:
        decision = MediaUseDecision(
            decision_id=str(uuid.uuid4()),
            editorial_revision_id=editorial_revision.id,
            media_asset_id=asset.id,
            media_source_offer_id=offer.id,
            license_evidence_id=evidence.id,
            status=MediaUseStatus.APPROVED.value,
            role=requirement.role.value,
            rationale=policy.reason,
            attribution_text=attribution,
            edit_note=edit_note,
            decided_by=decided_by,
            decided_at=now,
        )
        session.add(decision)
        session.flush()
        return decision
    decision.status = MediaUseStatus.APPROVED.value
    decision.media_source_offer_id = offer.id
    decision.license_evidence_id = evidence.id
    decision.role = requirement.role.value
    decision.rationale = policy.reason
    decision.attribution_text = attribution
    decision.edit_note = edit_note
    decision.revoked_at = None
    decision.revoked_reason = None
    session.flush()
    return decision


def _attach_to_revision(
    session,
    *,
    editorial_revision: EditorialRevision,
    decision: MediaUseDecision,
    caption: str,
) -> RevisionMedia:
    link = (
        session.query(RevisionMedia)
        .filter_by(
            editorial_revision_id=editorial_revision.id,
            media_use_decision_id=decision.id,
        )
        .first()
    )
    if link is not None:
        return link
    used = (
        session.query(RevisionMedia)
        .filter_by(editorial_revision_id=editorial_revision.id)
        .count()
    )
    link = RevisionMedia(
        editorial_revision_id=editorial_revision.id,
        media_use_decision_id=decision.id,
        position=used,
        caption=caption or "",
    )
    session.add(link)
    session.flush()
    return link


def revoke_media_decision(
    session: Session,
    decision: MediaUseDecision,
    *,
    reason: str,
    revoked_by: str = "editor",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MediaUseDecision:
    """Withdraw a use decision and detach it from its revision.

    The decision itself is kept, so why a picture disappeared stays readable,
    but the revision no longer carries it and it cannot reach publication.
    """
    decision.status = MediaUseStatus.REVOKED.value
    decision.revoked_at = now()
    decision.revoked_reason = reason
    decision.decided_by = revoked_by
    session.query(RevisionMedia).filter_by(media_use_decision_id=decision.id).delete()
    session.commit()
    from btcedu.core.editorial.recheck import scan_changes

    revision = session.get(EditorialRevision, decision.editorial_revision_id)
    scan_changes(session, topic_ids=[revision.topic_id])
    return decision


def approved_revision_media(
    session: Session,
    editorial_revision: EditorialRevision,
) -> tuple[RevisionMedia, ...]:
    """Ordered illustrations that are actually cleared for publication."""
    rows = (
        session.query(RevisionMedia)
        .join(MediaUseDecision, RevisionMedia.media_use_decision_id == MediaUseDecision.id)
        .filter(
            RevisionMedia.editorial_revision_id == editorial_revision.id,
            MediaUseDecision.status == MediaUseStatus.APPROVED.value,
            MediaUseDecision.revoked_at.is_(None),
        )
        .order_by(RevisionMedia.position)
        .all()
    )
    return tuple(rows)
