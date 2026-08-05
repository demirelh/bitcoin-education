"""Editorial broadcast script stage (dual-presenter news format).

Sits between the approved translation and chapterization. It turns a flat list
of translated stories into an actual programme: a branded opening, a headline
block, ranked stories distributed between a female anchor and a male reporter,
a compact weather hand-over and a closing.

Design constraints:

* **Nothing is invented.** The model only rewrites *within* the approved
  translation of a story; :mod:`btcedu.core.script_qa` verifies numbers, names
  and opinion-free language deterministically afterwards.
* **Nothing disappears silently.** Every source story ends up either in the
  broadcast or in ``script_omissions.json`` with a score and a reason.
* **The frame is ours, not the model's.** Opening, headline block, weather
  hand-over and closing come from profile-configured variants, never from the
  model — so the programme cannot greet the audience differently every day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.core.script_qa import (
    ScriptQAConfig,
    ScriptQAResult,
    revision_feedback,
    run_script_qa,
    write_script_qa,
)
from btcedu.core.story_ranking import RankingBudget, rank_stories
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.script_schema import (
    WORDS_PER_MINUTE,
    BroadcastScript,
    OmittedStory,
    ScriptStory,
    SegmentPurpose,
    SpeakerRole,
    SpeakerSegment,
    StoryPriority,
    StoryRanking,
    estimate_duration_seconds,
)

logger = logging.getLogger(__name__)

SCRIPT_FILENAME = "script_broadcast.json"
NARRATION_FILENAME = "script.broadcast.tr.md"
OMISSIONS_FILENAME = "script_omissions.json"
OVERRIDES_FILENAME = "script_overrides.json"

# Fallback opening variants. Profiles should configure their own; these keep the
# stage usable (and testable) without any profile changes.
DEFAULT_OPENINGS: tuple[str, ...] = (
    "İyi akşamlar. {show_name}'na hoş geldiniz. Günün öne çıkan haberleriyle"
    " karşınızdayız. İşte ayrıntılar.",
    "İyi akşamlar. {show_name} başlıyor. Bugün Almanya'da ve dünyada öne çıkan"
    " gelişmeleri aktarıyoruz.",
    "İyi akşamlar. Almanya'nın gündemindeki önemli gelişmelerle karşınızdayız.",
    "İyi akşamlar, {show_name}'na hoş geldiniz. Bugünün öne çıkan başlıklarıyla başlıyoruz.",
)

DEFAULT_CLOSINGS: tuple[str, ...] = (
    "Bugün öne çıkan gelişmeler böyleydi. Bizi izlediğiniz için teşekkür ederiz."
    " Yeniden görüşmek üzere. İyi akşamlar.",
    "Günün özeti böyleydi. İzlediğiniz için teşekkürler. Yarın yeniden birlikte"
    " olmak üzere, iyi akşamlar.",
    "Bugünkü bültenimizin sonuna geldik. Bizi izlediğiniz için teşekkür ederiz. İyi akşamlar.",
)

DEFAULT_WEATHER_HANDOVERS: tuple[str, ...] = (
    "Şimdi hava durumuna geçelim.",
    "Bültenimizi hava durumuyla tamamlıyoruz.",
    "Gelelim hava durumuna.",
)

DEFAULT_BRIEFS_LABEL = "Kısa haberlerle devam ediyoruz."


class ScriptError(RuntimeError):
    """Raised when the broadcast script cannot be produced."""


@dataclass
class ScriptResult:
    """Result of the script stage."""

    episode_id: str
    script_path: str = ""
    narration_path: str = ""
    omissions_path: str = ""
    qa_path: str = ""
    provenance_path: str = ""
    story_count: int = 0
    omitted_count: int = 0
    anchor_share: float = 0.0
    estimated_duration_seconds: float = 0.0
    revisions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: bool = False
    qa_result: ScriptQAResult | None = field(default=None, repr=False)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pick(variants: tuple[str, ...] | list[str], episode_id: str, salt: str) -> str:
    """Deterministically pick one variant for an episode.

    Deterministic so the stage is idempotent, episode-dependent so the programme
    does not sound identical every single day.
    """
    if not variants:
        return ""
    digest = hashlib.sha256(f"{episode_id}:{salt}".encode()).digest()
    return variants[digest[0] % len(variants)]


def _story_config(settings: Settings, episode: Episode) -> tuple[dict, dict, str | None]:
    """Return (script_config, branding, prompt_namespace) for the episode profile."""
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(getattr(episode, "content_profile", ""))
    except Exception:
        return {}, {}, None
    return (
        dict(profile.stage_config.get("script", {}) or {}),
        dict(getattr(profile, "branding", {}) or {}),
        getattr(profile, "prompt_namespace", None),
    )


def script_enabled(settings: Settings, episode: Episode) -> bool:
    """True when the episode's profile opts into the dual-presenter script."""
    config, _, _ = _story_config(settings, episode)
    return bool(config.get("enabled"))


def _load_stories(settings: Settings, episode_id: str) -> tuple[Path, dict]:
    base = Path(settings.outputs_dir) / episode_id
    for name in ("stories_adapted.json", "stories_translated.json"):
        path = base / name
        if path.exists():
            return path, json.loads(path.read_text(encoding="utf-8"))
    raise ScriptError(
        f"No translated stories found for {episode_id}: expected stories_adapted.json "
        "or stories_translated.json"
    )


def _approved_text(story: dict[str, Any]) -> str:
    return str(story.get("text_adapted_tr") or story.get("text_tr") or "").strip()


def _load_overrides(settings: Settings, episode_id: str) -> dict[str, str]:
    path = Path(settings.outputs_dir) / episode_id / OVERRIDES_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    valid = {p.value for p in StoryPriority}
    return {str(k): str(v) for k, v in (data or {}).items() if str(v) in valid}


def _selected_stories_block(
    stories: list[dict[str, Any]],
    rankings: dict[str, StoryRanking],
) -> str:
    """Compact, model-friendly rendering of the stories that go on air."""
    blocks = []
    for story in stories:
        ranking = rankings[story["story_id"]]
        target_words = round(ranking.estimated_duration_seconds / 60 * WORDS_PER_MINUTE)
        min_words = round(target_words * 0.9)
        blocks.append(
            "\n".join(
                [
                    f"### {story['story_id']} — öncelik: {ranking.priority.value}",
                    f"kategori: {story.get('category', '')}",
                    (
                        f"hedef süre: {ranking.estimated_duration_seconds:.0f} saniye "
                        f"= yaklaşık {target_words} kelime (en az {min_words} kelime)"
                    ),
                    f"izleyici ilgisi: {ranking.reasoning_summary}",
                    "",
                    "onaylanmış Türkçe metin:",
                    _approved_text(story),
                    "",
                ]
            )
        )
    return "\n".join(blocks)


def _parse_model_stories(raw: str) -> list[dict[str, Any]]:
    """Extract the ``stories`` array from a model response."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ScriptError("Model response contained no JSON object")
    payload = json.loads(text[start : end + 1])
    stories = payload.get("stories")
    if not isinstance(stories, list) or not stories:
        raise ScriptError("Model response contained no stories")
    return stories


def _short_headline(text: str, max_words: int = 6) -> str:
    """Trim a headline to overlay length without cutting mid-word."""
    cleaned = " ".join(str(text or "").replace("\n", " ").split())
    cleaned = cleaned.split(" - ")[0].split(" — ")[0].strip(" .,:;")
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])
    return cleaned


def _trim_to_duration(text: str, target_seconds: float) -> str:
    """Keep whole leading sentences until the target duration is reached.

    Used for ``brief`` stories in the deterministic fallback: shortening by
    dropping trailing detail sentences never changes a number or a name that is
    still spoken, unlike paraphrasing would.
    """
    if target_seconds <= 0:
        return text
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if s.strip()
    ]
    if not sentences:
        return text
    kept: list[str] = []
    total = 0.0
    for sentence in sentences:
        duration = estimate_duration_seconds(sentence)
        if kept and total + duration > target_seconds:
            break
        kept.append(sentence)
        total += duration
    return " ".join(kept) if kept else sentences[0]


def _fallback_sequence(
    story: dict[str, Any], ranking: StoryRanking, index: int = 0
) -> list[dict[str, Any]]:
    """Deterministic speaker split used when no model is available (dry-run/tests).

    Splits the approved text at a sentence boundary: the anchor introduces, the
    reporter delivers the body. Never invents a word; ``brief`` stories are
    shortened by dropping trailing sentences, not by paraphrasing.
    """
    text = _approved_text(story)
    if not text:
        return []
    if ranking.priority == StoryPriority.BRIEF:
        # Briefs are trimmed to their ranked airtime. The anchor announces them
        # like every other story; the reporter only follows her.
        trimmed = _trim_to_duration(text, ranking.estimated_duration_seconds)
        brief_sentences = [
            s.strip() for s in re.split(r"(?<=[.!?])\s+", trimmed.replace("\n", " ")) if s.strip()
        ]
        if len(brief_sentences) < 2:
            return [{"role": SpeakerRole.ANCHOR.value, "purpose": "brief", "text": trimmed}]
        return [
            {
                "role": SpeakerRole.ANCHOR.value,
                "purpose": "transition",
                "text": brief_sentences[0],
            },
            {
                "role": SpeakerRole.REPORTER.value,
                "purpose": "brief",
                "text": " ".join(brief_sentences[1:]),
            },
        ]
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if s.strip()
    ]
    if len(sentences) < 2:
        return [{"role": SpeakerRole.ANCHOR.value, "purpose": "brief", "text": text}]
    # The anchor sets up the story (roughly the first 40% of it, at least one
    # sentence), the reporter delivers the detail. Nothing is added or dropped —
    # only the delivery is split — so the approved wording survives verbatim.
    split_at = max(1, round(len(sentences) * 0.4))
    split_at = min(split_at, len(sentences) - 1)
    head = " ".join(sentences[:split_at])
    body = " ".join(sentences[split_at:])
    return [
        {"role": SpeakerRole.ANCHOR.value, "purpose": "introduction", "text": head},
        {"role": SpeakerRole.REPORTER.value, "purpose": "report", "text": body},
    ]


def _coerce_role(value: Any) -> SpeakerRole:
    raw = str(value or "").strip().lower()
    if raw in {SpeakerRole.ANCHOR.value, "anchor", "sunucu", "moderator"}:
        return SpeakerRole.ANCHOR
    return SpeakerRole.REPORTER


def _coerce_purpose(value: Any) -> SegmentPurpose:
    raw = str(value or "").strip().lower()
    try:
        return SegmentPurpose(raw)
    except ValueError:
        return SegmentPurpose.REPORT


def _build_script_stories(
    model_stories: list[dict[str, Any]],
    source_by_id: dict[str, dict[str, Any]],
    rankings: dict[str, StoryRanking],
    selected_ids: list[str],
) -> list[ScriptStory]:
    """Convert model output into validated :class:`ScriptStory` objects."""
    by_source: dict[str, dict[str, Any]] = {}
    for entry in model_stories:
        source_id = str(entry.get("source_story_id") or entry.get("story_id") or "").strip()
        if source_id in source_by_id and source_id not in by_source:
            by_source[source_id] = entry

    result: list[ScriptStory] = []
    for order, source_id in enumerate(selected_ids, start=1):
        source = source_by_id[source_id]
        ranking = rankings[source_id]
        entry = by_source.get(source_id, {})
        raw_segments = entry.get("speaker_sequence") or []
        segments: list[SpeakerSegment] = []
        for raw in raw_segments:
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            segments.append(
                SpeakerSegment(
                    role=_coerce_role(raw.get("role")),
                    purpose=_coerce_purpose(raw.get("purpose")),
                    text=text,
                )
            )
        if not segments:
            for raw in _fallback_sequence(source, ranking, index=order):
                segments.append(
                    SpeakerSegment(
                        role=_coerce_role(raw["role"]),
                        purpose=_coerce_purpose(raw["purpose"]),
                        text=raw["text"],
                    )
                )
        if not segments:
            continue

        headline = _short_headline(entry.get("display_headline") or "")
        if not headline:
            headline = _short_headline(
                source.get("headline_tr") or source.get("headline_de") or source_id
            )
        summary = str(entry.get("display_summary") or "").strip()
        if not summary:
            summary = _first_sentence(_approved_text(source), max_words=15)

        result.append(
            ScriptStory(
                story_id=f"n{order:02d}",
                source_story_id=source_id,
                order=order,
                priority=ranking.priority,
                category=str(source.get("category") or ""),
                display_headline=headline[:80],
                display_summary=summary[:160],
                viewer_relevance=str(entry.get("viewer_relevance") or "").strip(),
                speaker_sequence=segments,
                source_segment_ids=list(source.get("source_segment_ids") or []),
                grounding_references=[source_id],
                is_weather=_is_weather_story(source, headline),
                overlay_duration_seconds=6.0,
                overlay_priority=1 if ranking.priority == StoryPriority.TOP else 0,
            )
        )
    return result


def _is_weather_story(source: dict[str, Any], headline: str) -> bool:
    """Whether a story is the weather forecast.

    The category is the primary signal, but it is not the only one: the visual
    side decides with the canonical multi-signal detector, and if the two
    disagree the reporter ends up narrating over the weather map. Both sides ask
    the same question about the same text instead.
    """
    if str(source.get("category") or "").lower() == "wetter":
        return True
    from btcedu.core.weather.detector import detect_weather_story

    return detect_weather_story(
        title=headline,
        story_type=str(source.get("category") or ""),
        narration_text=_approved_text(source),
    ).is_weather_story


def _first_sentence(text: str, max_words: int = 15) -> str:
    if not text:
        return ""
    sentence = text.replace("\n", " ").split(". ")[0].strip()
    words = sentence.split()
    if len(words) > max_words:
        sentence = " ".join(words[:max_words])
    return sentence.rstrip(",;:") + ("." if not sentence.endswith(".") else "")


def _headline_block(stories: list[ScriptStory], max_items: int = 4) -> str:
    """Two to four teasers for stories that really are broadcast."""
    teasers = [s for s in stories if s.priority == StoryPriority.TOP and not s.is_weather]
    if len(teasers) < 2:
        teasers = [s for s in stories if not s.is_weather][:max_items]
    teasers = teasers[:max_items]
    if len(teasers) < 2:
        return ""
    lines = [s.display_headline.rstrip(".") + "." for s in teasers]
    return "\n".join(lines)


def _frame_stories(
    script_stories: list[ScriptStory],
    episode_id: str,
    branding: dict[str, Any],
    config: dict[str, Any],
) -> list[ScriptStory]:
    """Prepend opening + headline block and append the closing."""
    show_name = str(branding.get("show_name") or "").strip()
    openings = [str(v) for v in (config.get("openings") or DEFAULT_OPENINGS)]
    closings = [str(v) for v in (config.get("closings") or DEFAULT_CLOSINGS)]
    handovers = [str(v) for v in (config.get("weather_handovers") or DEFAULT_WEATHER_HANDOVERS)]

    opening_text = _pick(tuple(openings), episode_id, "opening").format(show_name=show_name)
    closing_text = _pick(tuple(closings), episode_id, "closing").format(show_name=show_name)
    handover_text = _pick(tuple(handovers), episode_id, "weather")

    framed: list[ScriptStory] = []
    opening_segments = [
        SpeakerSegment(role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.OPENING, text=opening_text)
    ]
    headlines = _headline_block(script_stories)
    if headlines:
        opening_segments.append(
            SpeakerSegment(
                role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.HEADLINES, text=headlines
            )
        )
    framed.append(
        ScriptStory(
            story_id="n00",
            source_story_id="__opening__",
            order=1,
            priority=StoryPriority.NORMAL,
            category="opening",
            display_headline=show_name.upper() or "HABERLER",
            display_summary=str(branding.get("slogan") or ""),
            speaker_sequence=opening_segments,
            overlay_duration_seconds=5.0,
            overlay_priority=2,
        )
    )

    briefs_started = False
    for story in script_stories:
        if story.is_weather:
            # The weather belongs to the anchor from the handover to the last
            # word. A voice change in the middle of the forecast reads as a
            # second presenter taking over, which is not what happens: she
            # hands over to the map, not to the reporter.
            story.speaker_sequence = [
                segment.model_copy(update={"role": SpeakerRole.ANCHOR})
                if segment.role != SpeakerRole.ANCHOR
                else segment
                for segment in story.speaker_sequence
            ]
            story.speaker_sequence.insert(
                0,
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR,
                    purpose=SegmentPurpose.WEATHER_HANDOVER,
                    text=handover_text,
                ),
            )
        elif story.priority == StoryPriority.BRIEF and not briefs_started:
            briefs_started = True
            label = str(config.get("briefs_label") or DEFAULT_BRIEFS_LABEL)
            story.speaker_sequence.insert(
                0,
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.TRANSITION, text=label
                ),
            )
        framed.append(story)

    framed.append(
        ScriptStory(
            story_id="n99",
            source_story_id="__closing__",
            order=1,
            priority=StoryPriority.NORMAL,
            category="closing",
            display_headline=show_name.upper() or "HABERLER",
            display_summary=str(branding.get("slogan") or ""),
            speaker_sequence=[
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.CLOSING, text=closing_text
                )
            ],
            overlay_duration_seconds=5.0,
            overlay_priority=2,
        )
    )

    for index, story in enumerate(framed, start=1):
        story.order = index
        story.story_id = f"n{index:02d}"
    return framed


def _write_artifacts(
    settings: Settings,
    episode_id: str,
    script: BroadcastScript,
    omissions: list[OmittedStory],
) -> tuple[Path, Path, Path]:
    base = Path(settings.outputs_dir) / episode_id
    base.mkdir(parents=True, exist_ok=True)

    script_path = base / SCRIPT_FILENAME
    script_path.write_text(
        json.dumps(script.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    narration_path = base / NARRATION_FILENAME
    narration_path.write_text(script.narration + "\n", encoding="utf-8")

    omissions_path = base / OMISSIONS_FILENAME
    omissions_path.write_text(
        json.dumps(
            {
                "episode_id": episode_id,
                "generated_at": _utcnow().isoformat(),
                "omitted": [o.model_dump(mode="json") for o in omissions],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return script_path, narration_path, omissions_path


def _is_current(script_path: Path, provenance_path: Path, input_hash: str) -> bool:
    if not script_path.exists() or not provenance_path.exists():
        return False
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return provenance.get("input_hash") == input_hash


def generate_script(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> ScriptResult:
    """Produce the editorial broadcast script for an episode.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: Rebuild even when the existing script is current.

    Returns:
        ScriptResult with artifact paths and quality metrics.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    config, branding, prompt_namespace = _story_config(settings, episode)
    if not config.get("enabled"):
        logger.info("Broadcast script disabled by profile for %s", episode_id)
        return ScriptResult(episode_id=episode_id, skipped=True)

    stories_path, stories_doc = _load_stories(settings, episode_id)
    source_stories: list[dict[str, Any]] = list(stories_doc.get("stories") or [])
    if not source_stories:
        raise ScriptError(f"No stories in {stories_path}")

    base = Path(settings.outputs_dir) / episode_id
    script_path = base / SCRIPT_FILENAME
    provenance_path = base / "provenance" / "script_provenance.json"

    qa_config = ScriptQAConfig.from_stage_config(config)
    budget = RankingBudget(
        target_seconds=qa_config.target_total_seconds,
        min_seconds=qa_config.min_total_seconds,
        max_seconds=qa_config.max_total_seconds,
        overhead_seconds=float(config.get("overhead_seconds", 55)),
        delivery_factor=float(config.get("delivery_factor", 0.88)),
    )
    overrides = _load_overrides(settings, episode_id)

    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("script_broadcast.md", profile=prompt_namespace)
    prompt_name = "script_broadcast"
    if prompt_namespace and (TEMPLATES_DIR / prompt_namespace / "script_broadcast.md").exists():
        prompt_name = f"{prompt_namespace}/script_broadcast"
    registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_hash = registry.compute_hash(template_body)

    input_hash = hashlib.sha256(
        stories_path.read_bytes()
        + prompt_hash.encode()
        + json.dumps(config, sort_keys=True, default=str).encode()
        + json.dumps(branding, sort_keys=True, default=str).encode()
        + json.dumps(overrides, sort_keys=True).encode()
    ).hexdigest()

    if not force and _is_current(script_path, provenance_path, input_hash):
        logger.info("Broadcast script is current for %s (use --force to rebuild)", episode_id)
        existing = json.loads(script_path.read_text(encoding="utf-8"))
        script = BroadcastScript.model_validate(existing)
        return ScriptResult(
            episode_id=episode_id,
            script_path=str(script_path),
            narration_path=str(base / NARRATION_FILENAME),
            omissions_path=str(base / OMISSIONS_FILENAME),
            qa_path=str(base / "script_qa.json"),
            provenance_path=str(provenance_path),
            story_count=len(script.stories),
            anchor_share=script.anchor_share(),
            estimated_duration_seconds=script.estimated_duration_seconds,
            skipped=True,
        )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.SCRIPT,
        status=RunStatus.RUNNING,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        rankings_list = rank_stories(source_stories, budget=budget, overrides=overrides)
        rankings = {r.story_id: r for r in rankings_list}
        source_by_id = {str(s["story_id"]): s for s in source_stories}

        selected = [
            str(s["story_id"])
            for s in source_stories
            if rankings[str(s["story_id"])].priority != StoryPriority.OMIT and _approved_text(s)
        ]
        if not selected:
            raise ScriptError(f"Ranking omitted every story for {episode_id}")

        omissions = [
            OmittedStory(
                story_id=r.story_id,
                headline=str(
                    source_by_id.get(r.story_id, {}).get("headline_tr")
                    or source_by_id.get(r.story_id, {}).get("headline_de")
                    or ""
                ),
                total_score=r.total_score,
                reason=r.reasoning_summary,
                decision="omitted",
                manual_override=r.manual_override,
            )
            for r in rankings_list
            if r.priority == StoryPriority.OMIT
        ]

        approved_by_story = {sid: _approved_text(source_by_id[sid]) for sid in selected}
        selected_docs = [source_by_id[sid] for sid in selected]

        max_revisions = max(0, int(config.get("max_automatic_revisions", 2)))
        feedback = ""
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        script: BroadcastScript | None = None
        qa_result: ScriptQAResult | None = None

        for revision in range(max_revisions + 1):
            model_stories, usage = _request_model_script(
                session=session,
                settings=settings,
                episode_id=episode_id,
                template_body=template_body,
                config=config,
                broadcast_date=str(stories_doc.get("broadcast_date") or ""),
                selected_docs=selected_docs,
                rankings=rankings,
                qa_config=qa_config,
                feedback=feedback,
                fallback_source=source_by_id,
            )
            total_input_tokens += usage[0]
            total_output_tokens += usage[1]
            total_cost += usage[2]

            script_stories = _build_script_stories(model_stories, source_by_id, rankings, selected)
            framed = _frame_stories(script_stories, episode_id, branding, config)
            script = BroadcastScript(
                episode_id=episode_id,
                profile=str(getattr(episode, "content_profile", "") or ""),
                show_name=str(branding.get("show_name") or ""),
                slogan=str(branding.get("slogan") or ""),
                broadcast_date=str(stories_doc.get("broadcast_date") or ""),
                stories=framed,
                rankings=rankings_list,
                omissions=omissions,
                revision=revision,
                generated_by="llm" if model_stories else "deterministic",
            )
            qa_result = run_script_qa(script, approved_by_story, selected, qa_config)
            if not qa_result.revision_required or revision >= max_revisions:
                break
            if not model_stories:
                # The deterministic fallback only re-splits approved text; asking
                # it again would produce the identical script at no benefit.
                logger.warning(
                    "Script QA requested revision for %s but the deterministic "
                    "fallback cannot revise: %s",
                    episode_id,
                    ", ".join(qa_result.revision_reasons),
                )
                break
            feedback = revision_feedback(qa_result)
            logger.warning(
                "Script QA requested revision %d for %s: %s",
                revision + 1,
                episode_id,
                ", ".join(qa_result.revision_reasons),
            )

        assert script is not None and qa_result is not None  # loop always runs once

        script_path, narration_path, omissions_path = _write_artifacts(
            settings, episode_id, script, omissions
        )
        qa_path = write_script_qa(settings.outputs_dir, episode_id, qa_result, script.revision)

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "script",
                    "episode_id": episode_id,
                    "generated_at": _utcnow().isoformat(),
                    "input_hash": input_hash,
                    "input_file": str(stories_path.name),
                    "prompt_hash": prompt_hash,
                    "prompt_name": prompt_name,
                    "revision": script.revision,
                    "story_count": len(script.stories),
                    "omitted_count": len(omissions),
                    "anchor_share": script.anchor_share(),
                    "estimated_duration_seconds": script.estimated_duration_seconds,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "cost_usd": total_cost,
                    "generated_by": script.generated_by,
                    "output_files": [
                        str(script_path),
                        str(narration_path),
                        str(omissions_path),
                        str(qa_path),
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        _invalidate_downstream(settings, episode_id)

        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input_tokens
        pipeline_run.output_tokens = total_output_tokens
        pipeline_run.estimated_cost_usd = total_cost
        if episode.status in {EpisodeStatus.ADAPTED, EpisodeStatus.TRANSLATED}:
            episode.status = EpisodeStatus.SCRIPTED
        session.commit()

        logger.info(
            "Broadcast script for %s: %d stories, %d omitted, anchor %.0f%%, %.1f min",
            episode_id,
            len(script.stories),
            len(omissions),
            script.anchor_share() * 100,
            script.estimated_duration_seconds / 60,
        )

        return ScriptResult(
            episode_id=episode_id,
            script_path=str(script_path),
            narration_path=str(narration_path),
            omissions_path=str(omissions_path),
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            story_count=len(script.stories),
            omitted_count=len(omissions),
            anchor_share=script.anchor_share(),
            estimated_duration_seconds=script.estimated_duration_seconds,
            revisions=script.revision,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=total_cost,
            qa_result=qa_result,
        )

    except Exception as error:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(error)[:1000]
        session.commit()
        raise


def _request_model_script(
    *,
    session: Session,
    settings: Settings,
    episode_id: str,
    template_body: str,
    config: dict[str, Any],
    broadcast_date: str,
    selected_docs: list[dict[str, Any]],
    rankings: dict[str, StoryRanking],
    qa_config: ScriptQAConfig,
    feedback: str,
    fallback_source: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[int, int, float]]:
    """Ask the editorial model for the story bodies.

    Falls back to a deterministic anchor/reporter split when the model is
    unavailable, disabled or returns something unusable — the programme must
    never fail to exist because a provider hiccuped.
    """
    from btcedu.core.tts import _get_episode_total_cost  # local import: avoid cycle
    from btcedu.services.claude_service import call_claude
    from btcedu.services.errors import ErrorCategory, PipelineError

    body, _, user_part = template_body.partition("# Input")
    rendered = (
        user_part.replace("{{broadcast_date}}", broadcast_date)
        .replace("{{target_body_seconds}}", f"{qa_config.target_total_seconds:.0f}")
        .replace("{{anchor_share_min}}", f"{qa_config.anchor_share_min * 100:.0f}")
        .replace("{{anchor_share_max}}", f"{qa_config.anchor_share_max * 100:.0f}")
        .replace("{{selected_stories}}", _selected_stories_block(selected_docs, rankings))
        .replace("{{revision_feedback}}", feedback)
    )

    if settings.dry_run:
        logger.info("Dry-run: using deterministic speaker split for %s", episode_id)
        return [], (0, 0, 0.0)

    episode_cost = _get_episode_total_cost(session, episode_id)
    if episode_cost >= settings.max_episode_cost_usd:
        raise PipelineError(
            f"Episode cost limit reached before script generation: "
            f"${episode_cost:.4f} >= ${settings.max_episode_cost_usd:.4f}",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )

    try:
        response = call_claude(
            system_prompt=body.replace("# System", "").strip(),
            user_message=rendered,
            settings=settings,
            max_tokens=int(config.get("max_tokens", 16384)),
            json_mode=True,
            model_override=config.get("model"),
            provider_override=config.get("provider"),
        )
    except Exception as error:  # noqa: BLE001 - provider failure must not kill the show
        logger.warning(
            "Editorial model unavailable for %s (%s); using deterministic split",
            episode_id,
            error,
        )
        return [], (0, 0, 0.0)

    usage = (
        getattr(response, "input_tokens", 0) or 0,
        getattr(response, "output_tokens", 0) or 0,
        float(getattr(response, "cost_usd", 0.0) or 0.0),
    )
    try:
        return _parse_model_stories(response.text), usage
    except (ScriptError, json.JSONDecodeError) as error:
        logger.warning(
            "Editorial model returned unusable JSON for %s (%s); using deterministic split",
            episode_id,
            error,
        )
        return [], usage


def _invalidate_downstream(settings: Settings, episode_id: str) -> None:
    """Mark chapterize/TTS/render outputs stale after a new script."""
    base = Path(settings.outputs_dir) / episode_id
    for relative in ("chapters.json", "tts/manifest.json", "render/manifest.json"):
        target = base / relative
        if target.exists():
            marker = target.with_suffix(target.suffix + ".stale")
            try:
                marker.write_text(
                    json.dumps(
                        {
                            "reason": "broadcast script regenerated",
                            "stage": "script",
                            "marked_at": _utcnow().isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as error:
                logger.warning("Could not write stale marker %s: %s", marker, error)


def load_broadcast_script(settings: Settings, episode_id: str) -> BroadcastScript | None:
    """Load the broadcast script of an episode, if one exists."""
    path = Path(settings.outputs_dir) / episode_id / SCRIPT_FILENAME
    if not path.exists():
        return None
    try:
        return BroadcastScript.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not load broadcast script for %s: %s", episode_id, error)
        return None


def broadcast_narration(settings: Settings, episode_id: str) -> str:
    """The exact spoken text of the broadcast script, or ``""`` when there is none.

    A human-reviewed script under ``review/`` takes precedence over the
    generated one, mirroring the review conventions of the other stages.
    """
    base = Path(settings.outputs_dir) / episode_id
    for candidate in (
        base / "review" / "script.broadcast.reviewed.tr.md",
        base / NARRATION_FILENAME,
    ):
        if candidate.exists():
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            if text.strip():
                return text
    return ""


def estimate_script_duration(script: BroadcastScript) -> float:
    """Estimated spoken duration of a broadcast script in seconds."""
    return round(
        sum(
            estimate_duration_seconds(segment.text)
            for story in script.stories
            for segment in story.speaker_sequence
        ),
        2,
    )
