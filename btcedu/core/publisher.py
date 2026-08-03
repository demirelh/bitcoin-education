"""YouTube publishing: safety checks, metadata, upload, provenance."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.publish_job import PublishJob, PublishJobStatus
from btcedu.models.review import ReviewStatus, ReviewTask

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass
class SafetyCheck:
    """Result of one pre-publish safety check."""

    name: str
    passed: bool
    message: str


@dataclass
class PublishResult:
    """Summary of publish operation for one episode."""

    episode_id: str
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    publish_job_id: int | None = None
    safety_checks: dict[str, str] = field(default_factory=dict)  # name → message
    skipped: bool = False
    dry_run: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


def _compute_hash_over_paths(paths: list[str]) -> str:
    """SHA-256 over sorted file contents (mirrors reviewer._compute_artifact_hash)."""
    h = hashlib.sha256()
    for path_str in sorted(paths):
        p = Path(path_str)
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def _get_approved_render_task(session: Session, episode_id: str) -> ReviewTask | None:
    """Return the latest approved render ReviewTask, or None."""
    return (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == "render",
            ReviewTask.status == ReviewStatus.APPROVED.value,
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )


def _check_approval_gate(session: Session, episode: Episode) -> SafetyCheck:
    """Check 1: Episode is APPROVED and an approved render review exists."""
    if episode.status != EpisodeStatus.APPROVED:
        return SafetyCheck(
            name="approval_gate",
            passed=False,
            message=f"Episode status is '{episode.status.value}', expected 'approved'",
        )
    task = _get_approved_render_task(session, episode.episode_id)
    if not task:
        return SafetyCheck(
            name="approval_gate",
            passed=False,
            message="No approved render ReviewTask found",
        )
    return SafetyCheck(
        name="approval_gate",
        passed=True,
        message=f"Approved render review task #{task.id} found",
    )


def _check_artifact_integrity(
    session: Session, episode: Episode, settings: Settings
) -> SafetyCheck:
    """Check 2: SHA-256 of artifact paths matches hash recorded at RG3 approval."""
    task = _get_approved_render_task(session, episode.episode_id)
    if not task or not task.artifact_hash:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="No artifact_hash found on approved render ReviewTask",
        )

    if not task.artifact_paths:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="No artifact_paths stored on review task",
        )

    try:
        paths = json.loads(task.artifact_paths)
    except (json.JSONDecodeError, TypeError):
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="Could not parse artifact_paths from review task",
        )

    current_hash = _compute_hash_over_paths(paths)
    if current_hash != task.artifact_hash:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message=(
                f"Artifact integrity mismatch: "
                f"approved={task.artifact_hash[:16]}... "
                f"current={current_hash[:16]}..."
            ),
        )
    return SafetyCheck(
        name="artifact_integrity",
        passed=True,
        message="SHA-256 hash matches approved artifact",
    )


def _check_metadata_completeness(title: str, description: str, tags: list[str]) -> SafetyCheck:
    """Check 3: Title, description, and tags are non-empty."""
    missing = []
    if not title or not title.strip():
        missing.append("title")
    if not description or not description.strip():
        missing.append("description")
    if not tags:
        missing.append("tags")
    if missing:
        return SafetyCheck(
            name="metadata_completeness",
            passed=False,
            message=f"Missing metadata: {', '.join(missing)}",
        )
    return SafetyCheck(
        name="metadata_completeness",
        passed=True,
        message="Title, description, and tags all present",
    )


def _check_cost_sanity(session: Session, episode: Episode, settings: Settings) -> SafetyCheck:
    """Check 4: Total episode cost is within max_episode_cost_usd."""
    total_cost = (
        session.query(func.sum(PipelineRun.estimated_cost_usd))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
    ) or 0.0

    if total_cost > settings.max_episode_cost_usd:
        return SafetyCheck(
            name="cost_sanity",
            passed=False,
            message=(
                f"Episode cost ${total_cost:.2f} exceeds "
                f"budget ${settings.max_episode_cost_usd:.2f}"
            ),
        )
    return SafetyCheck(
        name="cost_sanity",
        passed=True,
        message=f"Cost ${total_cost:.2f} within budget ${settings.max_episode_cost_usd:.2f}",
    )


def _publish_artifact_paths(episode_id: str, settings: Settings) -> list[str]:
    """The artifacts a final publish approval must be bound to.

    Binding to the final render, the QA quality gate, chapters and the canonical
    narration source means an approval becomes stale the instant any of them
    changes — an intermediate render approval can never authorize a later,
    different cut.
    """
    base = Path(settings.outputs_dir) / episode_id
    paths = [str(base / "render" / "draft.mp4")]
    for extra in (
        "translation_quality_gate.json",
        "chapters.json",
        "render/youtube_metadata.json",
        "render/publish_request.json",
    ):
        p = base / extra
        if p.exists():
            paths.append(str(p))
    try:
        from btcedu.core.qa_reviewer import canonical_narration

        narration_path, _ = canonical_narration(settings, episode_id)
        if narration_path is not None:
            paths.append(str(narration_path))
    except Exception:  # noqa: BLE001
        pass
    return paths


def _write_publish_request_artifact(
    episode: Episode,
    settings: Settings,
    privacy_status: str | None = None,
) -> Path:
    """Persist the effective upload settings that a final approval authorizes."""
    from btcedu.profiles import get_registry

    name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
    profile = get_registry(settings).get(name)
    youtube = profile.youtube or {}
    payload = {
        "episode_id": episode.episode_id,
        "content_profile": name,
        "privacy_status": (
            privacy_status
            or youtube.get("default_privacy")
            or getattr(settings, "youtube_default_privacy", "unlisted")
        ),
        "category_id": youtube.get("category_id", "25"),
        "default_language": youtube.get("default_language", "tr"),
    }
    path = Path(settings.outputs_dir) / episode.episode_id / "render" / "publish_request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _requires_manual_publish_review(episode: Episode, settings: Settings) -> bool:
    """True when the profile disables auto-publish (e.g. tagesschau news).

    Such profiles require an explicit, artifact-bound final-publish approval;
    the Review Gate 3 render approval is intermediate and never sufficient.
    """
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
        return not bool(get_registry(settings).get(name).auto_publish)
    except Exception:  # noqa: BLE001
        return True


def _requires_publish_qa(episode: Episode, settings: Settings) -> bool:
    """Whether publishing must have a valid QA gate and approved narration hash."""
    if _requires_manual_publish_review(episode, settings):
        return True
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
        profile = get_registry(settings).get(name)
        return bool((profile.stage_config.get("translation_qa", {}) or {}).get("enabled", False))
    except Exception:  # noqa: BLE001
        return True


def _check_qa_gate(session: Session, episode: Episode, settings: Settings) -> SafetyCheck:
    """Check 5: QA gate is GREEN for the current narration, or artifact-approved.

    Legacy profiles without QA enabled are unaffected.
    """
    from btcedu.core.qa_reviewer import gate_review_artifacts, load_quality_gate

    gate = load_quality_gate(settings, episode.episode_id)
    if gate is None:
        if _requires_publish_qa(episode, settings):
            return SafetyCheck("qa_gate", False, "Required Translation-QA gate is missing")
        return SafetyCheck("qa_gate", True, "No QA gate for profile — not required")

    decision = gate.get("decision") or gate.get("status")
    if decision == "green":
        return SafetyCheck("qa_gate", True, "QA gate is GREEN")

    from btcedu.core.reviewer import has_approved_review_for_artifacts

    artifacts = gate_review_artifacts(settings, episode.episode_id)
    if has_approved_review_for_artifacts(session, episode.episode_id, "translation_qa", artifacts):
        return SafetyCheck(
            "qa_gate",
            True,
            f"QA gate '{decision}' overridden by artifact-bound translation_qa review",
        )
    return SafetyCheck(
        "qa_gate",
        False,
        f"QA gate is '{decision}' and no artifact-bound QA review is approved",
    )


def _check_no_critical_findings(settings: Settings, episode: Episode) -> SafetyCheck:
    """Check 6: No unresolved (open) critical QA findings."""
    from btcedu.core.qa_reviewer import load_quality_gate

    gate = load_quality_gate(settings, episode.episode_id)
    if gate is None:
        if _requires_publish_qa(episode, settings):
            return SafetyCheck(
                "no_critical_findings", False, "Cannot verify critical findings: QA gate missing"
            )
        return SafetyCheck("no_critical_findings", True, "No QA gate — not applicable")

    open_critical = [
        f
        for f in (gate.get("findings") or [])
        if f.get("severity") == "critical" and (f.get("status") or "open") == "open"
    ]
    if open_critical:
        return SafetyCheck(
            "no_critical_findings",
            False,
            f"{len(open_critical)} unresolved critical QA finding(s)",
        )
    return SafetyCheck("no_critical_findings", True, "No unresolved critical findings")


def _check_narration_current(settings: Settings, episode: Episode) -> SafetyCheck:
    """Check 7: The approved narration hash still matches the current narration."""
    from btcedu.core.qa_reviewer import load_quality_gate, narration_sha256

    gate = load_quality_gate(settings, episode.episode_id)
    if gate is None:
        if _requires_publish_qa(episode, settings):
            return SafetyCheck(
                "narration_current", False, "Cannot verify narration: QA gate missing"
            )
        return SafetyCheck("narration_current", True, "No QA gate — not applicable")

    approved = gate.get("narration_sha256")
    if not approved:
        return SafetyCheck("narration_current", False, "QA gate has no approved narration hash")
    current = narration_sha256(settings, episode.episode_id)
    if current != approved:
        return SafetyCheck(
            "narration_current",
            False,
            f"Approved narration changed since QA (approved={approved[:12]}…, "
            f"current={str(current)[:12]}…)",
        )
    return SafetyCheck("narration_current", True, "Approved narration hash is current")


def _check_render_valid(session: Session, episode: Episode, settings: Settings) -> SafetyCheck:
    """Check 8: The draft render exists, is non-empty, validated, and current."""
    base = Path(settings.outputs_dir) / episode.episode_id
    draft = base / "render" / "draft.mp4"
    if not draft.exists() or draft.stat().st_size == 0:
        return SafetyCheck("render_valid", False, "Render draft.mp4 is missing or empty")

    try:
        from btcedu.core.renderer import render_is_current

        ok, reason = render_is_current(session, episode.episode_id, settings)
        if not ok:
            return SafetyCheck("render_valid", False, f"Render not current/validated: {reason}")
    except Exception as e:  # noqa: BLE001
        return SafetyCheck("render_valid", False, f"Render validation error: {e}")
    return SafetyCheck("render_valid", True, "Render draft exists, non-empty and validated")


def _check_profile_publish_permitted(episode: Episode, settings: Settings) -> SafetyCheck:
    """Check 9: The profile permits publishing (not explicitly disabled)."""
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
        profile = get_registry(settings).get(name)
        if (profile.youtube or {}).get("publish_enabled") is False:
            return SafetyCheck(
                "profile_publish_permitted",
                False,
                f"Profile '{name}' has publishing disabled",
            )
    except Exception as exc:  # noqa: BLE001
        return SafetyCheck(
            "profile_publish_permitted",
            False,
            f"Could not resolve publishing profile safely: {exc}",
        )
    return SafetyCheck("profile_publish_permitted", True, "Profile permits publishing")


def _check_manual_publish_approval(
    session: Session, episode: Episode, settings: Settings
) -> SafetyCheck:
    """Check 10 (auto_publish=False only): explicit final-publish approval exists.

    Requires an APPROVED ReviewTask with ``stage='publish'`` whose artifact hash
    matches the current final render + QA gate + narration source. The Review
    Gate 3 render approval is intermediate and does not satisfy this.
    """
    from btcedu.core.reviewer import has_approved_review_for_artifacts

    artifacts = _publish_artifact_paths(episode.episode_id, settings)
    if has_approved_review_for_artifacts(session, episode.episode_id, "publish", artifacts):
        return SafetyCheck(
            "manual_publish_approval", True, "Artifact-bound final-publish approval present"
        )
    return SafetyCheck(
        "manual_publish_approval",
        False,
        "No approved 'publish' review bound to the current render/gate/narration. "
        "Create and approve a final-publish review before publishing.",
    )


def _check_branding(episode: Episode, settings: Settings) -> SafetyCheck:
    """Block publishing when foreign attribution would be visible in the video.

    A last line of defence: ``render`` already guards this, but chapters.json
    can be edited by hand between render and publish.
    """
    from btcedu.core import branding_guard

    profile_name = getattr(episode, "content_profile", None) or "bitcoin_podcast"
    branding = branding_guard.branding_config(settings, profile_name)
    if not branding or branding.get("visible_source_attribution", True):
        return SafetyCheck("branding", True, "Profile permits visible source attribution")

    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"
    if not chapters_path.exists():
        return SafetyCheck("branding", True, "No chapter document to check")
    try:
        from btcedu.models.chapter_schema import ChapterDocument

        doc = ChapterDocument(**json.loads(chapters_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return SafetyCheck("branding", False, f"Could not read chapters.json: {exc}")

    branding_guard.sanitize_overlays(doc, branding)
    render_cfg: dict = {}
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(profile_name)
        render_cfg = (profile.stage_config.get("render", {}) if profile else {}) or {}
    except Exception:
        render_cfg = {}

    texts = branding_guard.collect_chapter_texts(doc)
    texts.extend(branding_guard.collect_render_texts(render_cfg))
    result = branding_guard.scan_texts(texts, branding)
    if result.ok:
        return SafetyCheck("branding", True, f"No forbidden visible text ({result.scanned} texts)")
    return SafetyCheck("branding", False, f"Forbidden visible text: {result.summary()}")


def _run_all_safety_checks(
    session: Session,
    episode: Episode,
    settings: Settings,
    title: str,
    description: str,
    tags: list[str],
) -> list[SafetyCheck]:
    """Run all pre-publish safety checks. Returns list of SafetyCheck.

    Legacy (auto_publish) profiles keep their original four checks plus the new
    ones which no-op when no QA gate exists. Profiles with ``auto_publish=False``
    (e.g. tagesschau) additionally require an explicit, artifact-bound
    final-publish approval so an intermediate render approval can never publish.
    """
    checks = [
        _check_approval_gate(session, episode),
        _check_artifact_integrity(session, episode, settings),
        _check_metadata_completeness(title, description, tags),
        _check_cost_sanity(session, episode, settings),
        _check_qa_gate(session, episode, settings),
        _check_no_critical_findings(settings, episode),
        _check_narration_current(settings, episode),
        _check_render_valid(session, episode, settings),
        _check_profile_publish_permitted(episode, settings),
        _check_branding(episode, settings),
    ]
    if _requires_manual_publish_review(episode, settings):
        checks.append(_check_manual_publish_approval(session, episode, settings))
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        logger.info("Safety check [%s] %s: %s", status, c.name, c.message)
    return checks


# ---------------------------------------------------------------------------
# Metadata construction
# ---------------------------------------------------------------------------

# Base tags always included
_BASE_TAGS = ["Bitcoin", "Kripto", "Blockchain", "Türkçe", "Eğitim", "Cryptocurrency"]


def _format_timestamp(total_seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS for YouTube chapter timestamps."""
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _load_tts_durations(episode_id: str, settings: Settings) -> dict[str, float]:
    """Load actual TTS durations from manifest. Returns {chapter_id: duration_seconds}."""
    manifest_path = Path(settings.outputs_dir) / episode_id / "tts" / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        segments = data.get("segments", [])
        return {s["chapter_id"]: float(s.get("duration_seconds", 0)) for s in segments}
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def _suggest_news_title(episode: Episode, chapters_list: list[dict]) -> str:
    """Build a topic-based YouTube title for a news episode.

    Combines the broadcast date (parsed from the episode title) with the
    most substantive chapter topics, e.g.
    "tagesschau 11.07.2026 — İran-ABD, Ukrayna, Srebrenica | Türkçe".
    Returns an empty string if no usable topics are found.
    """
    import re

    date_match = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", episode.title or "")
    date_str = date_match.group(1) if date_match else ""

    # Chapter titles to skip: intro/title cards and weather boilerplate.
    _skip = ("tagesschau", "hava durumu", "hava tahmini", "giriş", "wetter", "intro")

    topics: list[str] = []
    for ch in sorted(chapters_list, key=lambda c: c.get("order", 0)):
        ch_title = (ch.get("title") or "").strip()
        if not ch_title:
            continue
        low = ch_title.lower()
        if any(s in low for s in _skip):
            continue
        topics.append(ch_title)
        if len(topics) >= 3:
            break

    if not topics:
        return ""

    base = f"tagesschau {date_str}".strip()
    topic_str = ", ".join(topics)
    title = f"{base} — {topic_str} | Türkçe"
    return title[:100]


def _metadata_path(episode_id: str, settings: Settings) -> Path:
    return Path(settings.outputs_dir) / episode_id / "render" / "youtube_metadata.json"


def load_persisted_metadata(episode_id: str, settings: Settings) -> dict | None:
    """Load a previously generated/reviewed youtube_metadata.json, if present."""
    path = _metadata_path(episode_id, settings)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    return data


def generate_metadata_suggestion(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> dict:
    """Generate and persist proposed YouTube metadata for pre-publish review.

    Writes ``render/youtube_metadata.json`` with title, description, tags and
    publish settings (category/privacy/language) taken from the profile. If a
    file already exists it is returned unchanged unless ``force`` is set, so
    reviewer edits are never overwritten.

    Returns the metadata dict.
    """
    if not force:
        existing = load_persisted_metadata(episode_id, settings)
        if existing is not None:
            return existing

    episode = session.query(Episode).filter_by(episode_id=episode_id).first()
    if episode is None:
        raise ValueError(f"Episode not found: {episode_id}")

    title, description, tags = _build_youtube_metadata(episode, settings, session=session)

    # Pull publish defaults from the profile's youtube config.
    yt_config: dict = {}
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        profile = _get_profile_registry(settings).get(profile_name)
        yt_config = profile.youtube if profile else {}
    except Exception:
        yt_config = {}

    data = {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": str(yt_config.get("category_id", "22")),
        "privacy_status": yt_config.get("default_privacy", "unlisted"),
        "default_language": yt_config.get("default_language", "tr"),
        "generated_at": _utcnow().isoformat(),
        "source": "auto",
    }

    path = _metadata_path(episode_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def save_metadata_edits(
    episode_id: str,
    settings: Settings,
    updates: dict,
) -> dict:
    """Merge reviewer edits into the persisted youtube_metadata.json.

    Accepts ``title``, ``description`` and ``tags`` keys. Marks the source as
    ``edited`` so downstream knows a human adjusted the proposal.
    """
    path = _metadata_path(episode_id, settings)
    current = load_persisted_metadata(episode_id, settings) or {}

    if "title" in updates and updates["title"] is not None:
        current["title"] = str(updates["title"])[:100]
    if "description" in updates and updates["description"] is not None:
        current["description"] = str(updates["description"])[:5000]
    if "tags" in updates and updates["tags"] is not None:
        tags = updates["tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        current["tags"] = [str(t) for t in tags]

    if not current.get("title"):
        raise ValueError("Metadata must have a non-empty title")

    current["source"] = "edited"
    current["edited_at"] = _utcnow().isoformat()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def _build_youtube_metadata(
    episode: Episode,
    settings: Settings,
    session=None,
) -> tuple[str, str, list[str]]:
    """Build YouTube title, description, and tags from chapters.json.

    Returns:
        (title, description, tags)
    """
    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"

    # Load profile for profile-specific YouTube metadata
    _yt_config: dict = {}
    _profile_domain = "cryptocurrency"
    if session is not None:
        try:
            from btcedu.profiles import get_registry as _get_profile_registry

            _profile_name = (
                getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
            )
            _profile = _get_profile_registry(settings).get(_profile_name)
            _yt_config = _profile.youtube if _profile else {}
            _profile_domain = getattr(_profile, "domain", "cryptocurrency") or "cryptocurrency"
        except Exception:
            pass

    title = episode.title
    chapters_list = []

    if chapters_path.exists():
        try:
            chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
            # ChapterDocument top-level title
            if chapters_data.get("title"):
                title = chapters_data["title"]
            chapters_list = chapters_data.get("chapters", [])
        except (json.JSONDecodeError, OSError):
            pass

    # Truncate title to YouTube limit
    # For news content, prefer a topic-based title over the generic
    # "tagesschau … — Türkçe" placeholder so viewers see the actual stories.
    if _profile_domain == "news":
        news_title = _suggest_news_title(episode, chapters_list)
        if news_title:
            title = news_title
    title = title[:100]

    # Load TTS durations for accurate timestamps
    tts_durations = _load_tts_durations(episode.episode_id, settings)

    # Build chapter timestamps
    timestamp_lines = []
    cumulative_seconds = 0.0
    for ch in sorted(chapters_list, key=lambda c: c.get("order", 0)):
        ch_id = ch.get("chapter_id", "")
        ch_title = ch.get("title", ch_id)
        timestamp_lines.append(f"{_format_timestamp(cumulative_seconds)} {ch_title}")
        # Use TTS actual duration, fall back to estimated from narration
        duration = tts_durations.get(ch_id, 0.0)
        if duration == 0.0:
            narration = ch.get("narration") or {}
            duration = float(narration.get("estimated_duration_seconds", 60))
        cumulative_seconds += duration

    # Build description
    # Intro: first chapter narration excerpt (up to 300 chars)
    intro = ""
    if chapters_list:
        first_ch = sorted(chapters_list, key=lambda c: c.get("order", 0))[0]
        narration = first_ch.get("narration") or {}
        text = narration.get("text", "")
        intro = text[:300].strip()
        if len(text) > 300:
            intro += "..."

    description_parts = []
    if intro:
        description_parts.append(intro)
        description_parts.append("")

    if timestamp_lines:
        # YouTube auto-detects chapters if first timestamp is 0:00
        description_parts.append("📋 Bölümler / Chapters:")
        description_parts.extend(timestamp_lines)
        description_parts.append("")

    # Profile-specific hashtags / attribution
    profile_tags_list = _yt_config.get("tags", [])
    if profile_tags_list:
        hashtags_str = " ".join(f"#{t.replace(' ', '')}" for t in profile_tags_list[:5])
    else:
        hashtags_str = "#Bitcoin #Kripto #Türkçe #Eğitim #Blockchain"

    # For news domain, prepend source attribution
    if _profile_domain == "news":
        attribution = (
            "Kaynak: ARD tagesschau — Türkçe çeviri btcedu tarafından hazırlanmıştır.\n"
            "Source: ARD tagesschau — Turkish translation by btcedu.\n\n"
        )
        if description_parts:
            description_parts.insert(0, attribution)
        else:
            description_parts.append(attribution)

    description_parts.append(hashtags_str)
    description = "\n".join(description_parts)

    # Tags: profile tags or base tags, plus chapter titles
    base_tags = profile_tags_list if profile_tags_list else list(_BASE_TAGS)
    tags = list(base_tags)
    for ch in chapters_list[:5]:  # Limit extra tags from first 5 chapters
        ch_title = ch.get("title", "")
        if ch_title and len(ch_title) <= 30:
            tags.append(ch_title)

    # Enforce YouTube 500-char tag limit
    final_tags: list[str] = []
    total_chars = 0
    for tag in tags:
        if total_chars + len(tag) + 1 <= 500:
            final_tags.append(tag)
            total_chars += len(tag) + 1

    return title, description[:5000], final_tags


# ---------------------------------------------------------------------------
# Main publish function
# ---------------------------------------------------------------------------


def publish_video(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    privacy: str | None = None,
) -> PublishResult:
    """Publish approved video to YouTube.

    Processing flow:
    1. Validate episode (v2, APPROVED)
    2. Idempotency: skip if already published (unless force)
    3. Build metadata (title, description, tags)
    4. Run 4 pre-publish safety checks
    5. Create PublishJob (pending)
    6. Upload via YouTube service (or dry-run placeholder)
    7. On success: update Episode + PublishJob, write provenance
    8. Return PublishResult

    Args:
        session: SQLAlchemy session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: Skip idempotency check (re-publishes).
        privacy: Override privacy setting ("unlisted", "private", "public").
            Defaults to settings.youtube_default_privacy.

    Returns:
        PublishResult with video_id, url, and safety check results.

    Raises:
        ValueError: If episode not found, wrong pipeline version, wrong status,
            or any safety check fails.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "Publish is only supported for v2 pipeline."
        )

    # Allow APPROVED (or PUBLISHED with force) to proceed
    if episode.status != EpisodeStatus.APPROVED and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'approved'. Use --force to override."
        )

    # Idempotency: already published
    if episode.youtube_video_id and not force:
        logger.info(
            "Episode %s already published as %s (skipping)",
            episode_id,
            episode.youtube_video_id,
        )
        return PublishResult(
            episode_id=episode_id,
            youtube_video_id=episode.youtube_video_id,
            youtube_url=f"https://youtu.be/{episode.youtube_video_id}",
            skipped=True,
        )

    # Load profile for YouTube metadata overrides
    _yt_config: dict = {}
    try:
        from btcedu.profiles import get_registry as _get_pub_profile_registry

        _profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        _pub_profile = _get_pub_profile_registry(settings).get(_profile_name)
        _yt_config = _pub_profile.youtube if _pub_profile else {}
    except Exception:
        pass

    # Effective privacy setting (profile override > explicit arg > settings)
    effective_privacy = (
        privacy
        or _yt_config.get("default_privacy")
        or getattr(settings, "youtube_default_privacy", "unlisted")
    )
    _write_publish_request_artifact(episode, settings, effective_privacy)

    # Build metadata (pass session for profile-aware tags/category).
    # Prefer the metadata reviewed at Gate 3 so what was approved is published.
    _persisted = load_persisted_metadata(episode_id, settings)
    if _persisted is not None:
        title = _persisted["title"]
        description = _persisted.get("description", "")
        tags = _persisted.get("tags", [])
    else:
        title, description, tags = _build_youtube_metadata(episode, settings, session=session)

    # Run all 4 safety checks
    checks = _run_all_safety_checks(session, episode, settings, title, description, tags)
    check_results = {c.name: c.message for c in checks}
    failed_checks = [c for c in checks if not c.passed]

    if failed_checks:
        msg = "Pre-publish safety checks failed:\n" + "\n".join(
            f"  ✗ {c.name}: {c.message}" for c in failed_checks
        )
        raise ValueError(msg)

    # Create PublishJob (pending)
    publish_job = PublishJob(
        episode_id=episode_id,
        status=PublishJobStatus.PENDING.value,
    )
    session.add(publish_job)
    session.commit()

    # Find thumbnail (first chapter image)
    thumbnail_path: Path | None = None
    images_dir = Path(settings.outputs_dir) / episode_id / "images"
    chapters_path_file = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if images_dir.exists() and chapters_path_file.exists():
        try:
            chapters_data = json.loads(chapters_path_file.read_text(encoding="utf-8"))
            chapters_list = chapters_data.get("chapters", [])
            if chapters_list:
                first_ch = sorted(chapters_list, key=lambda c: c.get("order", 0))[0]
                first_ch_id = first_ch.get("chapter_id", "")
                candidate = images_dir / f"{first_ch_id}.png"
                if candidate.exists():
                    thumbnail_path = candidate
        except (json.JSONDecodeError, OSError, IndexError):
            pass

    draft_path = Path(settings.outputs_dir) / episode_id / "render" / "draft.mp4"

    # Build upload request
    from btcedu.services.youtube_service import (
        DryRunYouTubeService,
        YouTubeDataAPIService,
        YouTubeUploadRequest,
    )

    upload_req = YouTubeUploadRequest(
        video_path=draft_path,
        title=title,
        description=description,
        tags=tags,
        category_id=(
            _yt_config.get("category_id") or getattr(settings, "youtube_category_id", "27")
        ),
        default_language=(
            _yt_config.get("default_language")
            or getattr(settings, "youtube_default_language", "tr")
        ),
        privacy_status=effective_privacy,
        thumbnail_path=thumbnail_path,
    )

    is_dry_run = getattr(settings, "dry_run", False)

    if is_dry_run:
        youtube_svc = DryRunYouTubeService()
    else:
        credentials_path = getattr(
            settings,
            "youtube_credentials_path",
            "data/.youtube_credentials.json",
        )
        youtube_svc = YouTubeDataAPIService(
            credentials_path=credentials_path,
            chunk_size_bytes=getattr(settings, "youtube_upload_chunk_size_mb", 10) * 1024 * 1024,
        )

    # Update PublishJob to uploading
    publish_job.status = PublishJobStatus.UPLOADING.value
    session.commit()

    # Metadata snapshot
    metadata_snapshot = {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": upload_req.category_id,
        "default_language": upload_req.default_language,
        "privacy_status": effective_privacy,
    }

    def _progress_cb(uploaded: int, total: int) -> None:
        pct = int(uploaded / total * 100) if total else 100
        logger.info("YouTube upload: %d%% (%d / %d bytes)", pct, uploaded, total)

    try:
        response = youtube_svc.upload_video(upload_req, progress_callback=_progress_cb)
    except Exception as exc:
        # Record failure
        publish_job.status = PublishJobStatus.FAILED.value
        publish_job.error_message = str(exc)
        publish_job.metadata_snapshot = json.dumps(metadata_snapshot)
        session.commit()
        logger.error("YouTube upload failed for %s: %s", episode_id, exc)
        raise

    now = _utcnow()

    # Update PublishJob with success
    publish_job.status = PublishJobStatus.PUBLISHED.value
    publish_job.youtube_video_id = response.video_id
    publish_job.youtube_url = response.video_url
    publish_job.published_at = now
    publish_job.metadata_snapshot = json.dumps(metadata_snapshot)
    session.commit()

    # Update Episode
    if not is_dry_run:
        episode.youtube_video_id = response.video_id
        episode.published_at_youtube = now
        episode.status = EpisodeStatus.PUBLISHED
        session.commit()

    # Record PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="publish",
        status=RunStatus.SUCCESS.value,
        started_at=now,
        completed_at=now,
        estimated_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
    )
    session.add(pipeline_run)
    session.commit()

    # Write provenance
    _write_provenance(
        settings=settings,
        episode_id=episode_id,
        video_id=response.video_id,
        video_url=response.video_url,
        privacy=effective_privacy,
        safety_checks=checks,
        metadata_snapshot=metadata_snapshot,
        dry_run=is_dry_run,
    )

    logger.info(
        "Episode %s published%s: %s",
        episode_id,
        " (dry-run)" if is_dry_run else "",
        response.video_url,
    )

    return PublishResult(
        episode_id=episode_id,
        youtube_video_id=response.video_id,
        youtube_url=response.video_url,
        publish_job_id=publish_job.id,
        safety_checks=check_results,
        dry_run=is_dry_run,
    )


def _write_provenance(
    settings: Settings,
    episode_id: str,
    video_id: str,
    video_url: str,
    privacy: str,
    safety_checks: list[SafetyCheck],
    metadata_snapshot: dict,
    dry_run: bool,
) -> None:
    """Write provenance JSON for the publish operation."""
    prov_dir = Path(settings.outputs_dir) / episode_id / "provenance"
    prov_dir.mkdir(parents=True, exist_ok=True)
    prov_path = prov_dir / "publish.json"
    prov_data = {
        "episode_id": episode_id,
        "published_at": _utcnow().isoformat(),
        "youtube_video_id": video_id,
        "youtube_url": video_url,
        "privacy_status": privacy,
        "dry_run": dry_run,
        "safety_checks": {c.name: [c.passed, c.message] for c in safety_checks},
        "metadata_snapshot": metadata_snapshot,
    }
    prov_path.write_text(json.dumps(prov_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Provenance written: %s", prov_path)


# ---------------------------------------------------------------------------
# Query helpers (used by web API)
# ---------------------------------------------------------------------------


def get_latest_publish_job(session: Session, episode_id: str) -> PublishJob | None:
    """Return the most recent PublishJob for an episode."""
    return (
        session.query(PublishJob)
        .filter(PublishJob.episode_id == episode_id)
        .order_by(PublishJob.created_at.desc())
        .first()
    )


def request_publish_review(session: Session, episode_id: str, settings: Settings) -> ReviewTask:
    """Create (or return the pending) artifact-bound final-publish ReviewTask.

    Binds the approval to the current final render + QA gate + narration source so
    an approval is invalidated the moment any of them changes. Used by the CLI /
    web / pipeline so a human can authorize the actual YouTube upload for
    ``auto_publish=False`` profiles (e.g. tagesschau). Idempotent: if a pending
    publish review already exists it is returned unchanged.
    """
    from btcedu.core.reviewer import create_review_task

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is None:
        raise ValueError(f"Episode {episode_id} not found")
    _write_publish_request_artifact(episode, settings)

    existing = (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == "publish",
            ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value]),
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing

    artifact_paths = _publish_artifact_paths(episode_id, settings)
    return create_review_task(session, episode_id, stage="publish", artifact_paths=artifact_paths)
