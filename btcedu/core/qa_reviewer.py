"""Independent second-opinion QA and factual quality gate for the target script.

After the adapt stage produces the final Turkish narration, this module runs a
restricted, independent quality gate:

1. Deterministic translation QA (zero cost) runs first.
2. A *standard* independent model (different from the producer) critiques the
   target fassung against the German source, story structure, glossary and the
   deterministic findings, returning **structured findings only**.
3. High-risk findings escalate to a stronger model for a second opinion.
4. All findings are merged/deduped into a single ``QualityGateDocument`` whose
   decision (GREEN/YELLOW/RED) gates the pipeline.

Unlike the previous advisory critique, RED blocks the pipeline and GREEN records
the SHA-256 of the exact narration consumed downstream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import PromptRegistry
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.qa_schema import (
    QAFinding,
    QAFindingEvent,
    QAModelCall,
    QualityGateDocument,
    QualityGateSummary,
)
from btcedu.services.claude_service import (
    ClaudeResponse,
    _extract_json_object,
    call_claude,
)

logger = logging.getLogger(__name__)

QUALITY_GATE_ARTIFACT = "translation_quality_gate.json"

# Escalation triggers the profile may enable.
ESCALATION_TRIGGERS = frozenset(
    {
        "suspected_hallucination",
        "missing_story",
        "casualty_claim",
        "legal_claim",
        "unresolved_transcript",
        "contradictory_findings",
    }
)

# Categories the independent model may emit (kept broad but enumerated in prompt).
_LLM_CATEGORIES = frozenset(
    {
        "hallucination",
        "invented_fact",
        "missing_story",
        "meaning_error",
        "casualty_claim",
        "legal_claim",
        "number_error",
        "name_error",
        "neutralization_gap",
        "register",
        "fluency",
        "other",
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class QaReviewResult:
    """Result of an independent QA quality gate evaluation."""

    episode_id: str
    json_path: str
    markdown_path: str
    model: str
    overall_score: float | None = None
    issue_count: int = 0
    gate_path: str = ""
    decision: str = ""
    blocked: bool = False
    deterministic_status: str = ""
    critical_count: int = 0
    major_count: int = 0
    minor_count: int = 0
    generation: int = 0
    escalated: bool = False
    narration_sha256: str | None = None
    reasons: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: bool = False
    reason: str = ""


def _split_prompt(template_body: str) -> tuple[str, str]:
    """Split rendered template into system prompt and user message at '# Input'."""
    marker = "# Input"
    idx = template_body.find(marker)
    if idx == -1:
        return ("", template_body)
    return (template_body[:idx].strip(), template_body[idx:].strip())


def _qa_paths(settings: Settings, episode_id: str) -> tuple[Path, Path, Path]:
    base = Path(settings.outputs_dir) / episode_id
    json_path = base / "qa_review.json"
    md_path = base / "qa_review.md"
    prov_path = base / "provenance" / "qa_provenance.json"
    return json_path, md_path, prov_path


def load_qa_review(settings: Settings, episode_id: str) -> dict | None:
    """Return the persisted legacy QA review dict for an episode, or None.

    Kept for backward compatibility: old ``qa_review.json`` files load as-is.
    New pipelines should prefer :func:`load_quality_gate`.
    """
    json_path, _, _ = _qa_paths(settings, episode_id)
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_quality_gate(
    settings: Settings,
    episode_id: str,
    *,
    strict: bool = False,
) -> dict | None:
    """Return the validated merged quality gate document, or None if absent."""
    path = Path(settings.outputs_dir) / episode_id / QUALITY_GATE_ARTIFACT
    if not path.exists():
        return None
    try:
        document = QualityGateDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if strict:
            return None
        # Fall back to the raw JSON so a slightly-out-of-schema legacy gate still
        # loads for display rather than vanishing.
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return document.model_dump(mode="json")


def _render_markdown(data: dict, model: str) -> str:
    """Render the structured QA dict as a human-readable Markdown report."""
    lines: list[str] = []
    score = data.get("overall_score")
    score_str = f"{score}/10" if isinstance(score, (int, float)) else "—"
    lines.append(f"# QA-Zweitmeinung (Modell: {model})\n")
    lines.append(f"**Gesamtscore: {score_str}**\n")
    if data.get("summary"):
        lines.append(f"{data['summary']}\n")

    top_fixes = data.get("top_fixes") or []
    if top_fixes:
        lines.append("## Wichtigste Korrekturen\n")
        for fix in top_fixes:
            lines.append(f"- {fix}")
        lines.append("")

    missing = data.get("missing_content") or []
    if missing:
        lines.append("## Fehlende Inhalte\n")
        for m in missing:
            lines.append(f"- {m}")
        lines.append("")

    halluc = data.get("hallucinations") or []
    if halluc:
        lines.append("## Halluzinationen / erfundene Fakten\n")
        for h in halluc:
            lines.append(f"- {h}")
        lines.append("")

    neut = data.get("neutralization_gaps") or []
    if neut:
        lines.append("## Neutralisierungslücken\n")
        for n in neut:
            lines.append(f"- {n}")
        lines.append("")

    stories = data.get("stories") or []
    if stories:
        lines.append("## Bewertung pro Abschnitt\n")
        for st in stories:
            title = st.get("title", "?")
            s = st.get("score")
            s_str = f"{s}/10" if isinstance(s, (int, float)) else "—"
            lines.append(f"### {title}: {s_str}\n")
            for issue in st.get("issues") or []:
                lines.append(f"- {issue}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def _count_issues(data: dict) -> int:
    count = 0
    for key in ("missing_content", "hallucinations", "neutralization_gaps"):
        count += len(data.get(key) or [])
    for st in data.get("stories") or []:
        count += len(st.get("issues") or [])
    return count


def format_qa_feedback(
    settings: Settings,
    episode_id: str,
    *,
    max_story_issues: int = 2,
) -> str | None:
    """Format the latest QA critique as an actionable correction block.

    Reads ``qa_review.json`` (written by a prior pipeline pass) and turns the
    critical findings into a compact German instruction block that can be
    injected into the ``{{ reviewer_feedback }}`` placeholder of the translate
    and adapt prompts. This closes the loop: the QA second opinion from the
    previous run becomes correction guidance for the re-run.

    Returns ``None`` when no QA report exists or it contains nothing
    actionable.
    """
    data = load_qa_review(settings, episode_id)
    if not data:
        return None

    model = data.get("model") or getattr(settings, "qa_model", "")
    score = data.get("overall_score")
    score_str = f"{score}/10" if isinstance(score, (int, float)) else "—"

    sections: list[str] = []

    top_fixes = [str(x).strip() for x in (data.get("top_fixes") or []) if str(x).strip()]
    if top_fixes:
        sections.append("WICHTIGSTE KORREKTUREN:\n" + "\n".join(f"- {x}" for x in top_fixes))

    halluc = [str(x).strip() for x in (data.get("hallucinations") or []) if str(x).strip()]
    if halluc:
        sections.append(
            "ERFUNDENE FAKTEN (entfernen oder vorsichtig neutralisieren, "
            "niemals eigene Fakten ergänzen):\n" + "\n".join(f"- {x}" for x in halluc)
        )

    missing = [str(x).strip() for x in (data.get("missing_content") or []) if str(x).strip()]
    if missing:
        sections.append(
            "FEHLENDE INHALTE (müssen wenigstens zusammengefasst enthalten sein):\n"
            + "\n".join(f"- {x}" for x in missing)
        )

    neut = [str(x).strip() for x in (data.get("neutralization_gaps") or []) if str(x).strip()]
    if neut:
        sections.append(
            "NEUTRALISIERUNG (entfernen: Moderatorennamen, Sendungsname, "
            "Verabschiedung, Programmhinweise):\n" + "\n".join(f"- {x}" for x in neut)
        )

    story_lines: list[str] = []
    for st in data.get("stories") or []:
        issues = [str(x).strip() for x in (st.get("issues") or []) if str(x).strip()]
        if not issues:
            continue
        title = str(st.get("title", "?")).strip() or "?"
        for issue in issues[:max_story_issues]:
            story_lines.append(f"- [{title}] {issue}")
    if story_lines:
        sections.append("WEITERE HINWEISE PRO ABSCHNITT:\n" + "\n".join(story_lines))

    if not sections:
        return None

    header = (
        f"## QA-Zweitmeinung des vorherigen Laufs (Modell: {model}, Score: {score_str})\n"
        "Ein unabhängiges Modell hat die vorherige Fassung geprüft. Korrigiere gezielt "
        "die unten genannten Punkte. Ändere Passagen, die nicht bemängelt wurden, nicht. "
        "Erfinde keine Fakten und übernimm diesen Hinweistext nicht wörtlich in die Ausgabe."
    )
    return header + "\n\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Phase 7 — merged quality gate
# ---------------------------------------------------------------------------


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gate_path(settings: Settings, episode_id: str) -> Path:
    return Path(settings.outputs_dir) / episode_id / QUALITY_GATE_ARTIFACT


def _default_qa_config(settings: Settings) -> dict:
    provider = getattr(settings, "llm_provider", "anthropic")
    model = getattr(settings, "qa_model", "") or getattr(settings, "claude_model", "")
    return {
        "deterministic_checks": True,
        "standard": {"provider": provider, "model": model},
        "escalation": {
            "enabled": False,
            "provider": provider,
            "model": model,
            "trigger_on": [],
        },
        "quality_gate": {
            "max_automatic_retries": 2,
            "block_on_critical": True,
            "block_on_missing_story": True,
            "block_on_unresolved_transcript": True,
            "max_minor_findings": 5,
        },
    }


def resolve_qa_config(settings: Settings, episode: Episode) -> dict:
    """Merge the profile ``stage_config.qa`` over safe defaults.

    Model/provider names live entirely in configuration — never hardcoded in
    business logic. Missing keys fall back to the defaults derived from
    ``settings``.
    """
    config = _default_qa_config(settings)
    profile_qa: dict = {}
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None)
        if name:
            profile_qa = get_registry(settings).get(name).stage_config.get("qa", {}) or {}
    except Exception:  # noqa: BLE001
        profile_qa = {}
    if "deterministic_checks" in profile_qa:
        config["deterministic_checks"] = bool(profile_qa["deterministic_checks"])
    for key in ("standard", "escalation", "quality_gate"):
        override = profile_qa.get(key)
        if isinstance(override, dict):
            config[key] = {**config[key], **override}
    return config


def _cumulative_cost(session: Session, episode: Episode) -> float:
    return float(
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
        or 0.0
    )


def _cost_exceeded(session: Session, episode: Episode, settings: Settings) -> bool:
    return _cumulative_cost(session, episode) >= float(
        getattr(settings, "max_episode_cost_usd", 15.0)
    )


def _downstream_narration(settings: Settings, episode_id: str) -> tuple[Path | None, str]:
    """Return the (path, text) of the exact narration the next stage consumes.

    Mirrors the input-selection logic in ``chapterizer.chapterize_script`` so the
    gate hashes precisely what chapterization will read.
    """
    base = Path(settings.outputs_dir) / episode_id
    stories_translated = base / "stories_translated.json"
    adapted = base / "script.adapted.tr.md"
    reviewed_adapted = base / "review" / "script.adapted.reviewed.tr.md"
    stories_reviewed = base / "review" / "stories_translated.reviewed.json"
    use_story_mode = stories_translated.exists() and not adapted.exists()
    if use_story_mode:
        path: Path | None = stories_reviewed if stories_reviewed.exists() else stories_translated
    elif adapted.exists():
        path = reviewed_adapted if reviewed_adapted.exists() else adapted
    else:
        transcript = Path(settings.transcripts_dir) / episode_id / "transcript.tr.txt"
        path = transcript if transcript.exists() else None
    if path is None or not path.exists():
        return None, ""
    return path, path.read_text(encoding="utf-8")


def _extract_story_narration(path: Path) -> str:
    """Ordered spoken narration from a ``stories_*.json`` file.

    Uses the adapted Turkish text when present, else the translated Turkish
    text, concatenated in story order. Never the raw JSON — that also carries
    the German source and structural keys, which are not narration. Mirrors the
    ``adapted_text`` assembled in ``adapter.py`` so the canonical narrative is
    identical regardless of which artifact carries it.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    parts: list[str] = []
    for story in data.get("stories") or []:
        text = story.get("text_adapted_tr") or story.get("text_tr") or ""
        if text and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def canonical_narration(settings: Settings, episode_id: str) -> tuple[Path | None, str]:
    """Return (path, canonical spoken narration text) downstream must preserve.

    Single source of truth for the *approved narrative*: QA hashes it
    (:func:`narration_sha256`) and chapterization locks its composed chapters
    against it. For story mode the ordered ``text_adapted_tr``/``text_tr`` is
    extracted; for adapted/transcript mode the file text is used verbatim (so
    those hashes are byte-identical to the legacy behaviour).
    """
    path, raw = _downstream_narration(settings, episode_id)
    if path is None:
        return None, ""
    if path.suffix == ".json":
        return path, _extract_story_narration(path)
    return path, raw


def narration_sha256(settings: Settings, episode_id: str) -> str | None:
    """SHA-256 of the current canonical downstream narration, or None."""
    _, text = canonical_narration(settings, episode_id)
    if not text.strip():
        return None
    return _sha256_text(text)


def _load_story_structure(settings: Settings, episode_id: str) -> list[dict]:
    base = Path(settings.outputs_dir) / episode_id
    for name in ("stories_adapted.json", "stories_translated.json", "stories.json"):
        path = base / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        structure = []
        for story in data.get("stories") or []:
            structure.append(
                {
                    "story_id": story.get("story_id"),
                    "headline_de": story.get("headline_de"),
                    "source_text": (story.get("source_text") or story.get("text_de") or "")[:1200],
                    "target_text": (story.get("text_adapted_tr") or story.get("text_tr") or "")[
                        :1200
                    ],
                    "source_segment_ids": story.get("source_segment_ids") or [],
                    "is_lead_story": bool(story.get("is_lead_story", False)),
                }
            )
        return structure
    return []


def _load_transcript_unresolved(settings: Settings, episode_id: str) -> list[dict]:
    try:
        from btcedu.core.transcript_qa import load_transcript_qa

        document = load_transcript_qa(settings, episode_id)
    except Exception:  # noqa: BLE001
        document = None
    if not document:
        return []
    unresolved = []
    for finding in document.get("findings") or []:
        if finding.get("blocking") or finding.get("severity") in ("critical", "major"):
            unresolved.append(
                {
                    "category": finding.get("category"),
                    "severity": finding.get("severity"),
                    "blocking": bool(finding.get("blocking")),
                    "segment_ids": finding.get("segment_ids") or [],
                    "message": finding.get("message") or "",
                }
            )
    return unresolved


def _category_bucket(category: str) -> str:
    c = (category or "").lower()
    if "cost" in c:
        return "cost"
    if "transcript" in c or "unresolved_transcript" in c:
        return "transcript"
    if "legal" in c:
        return "legal"
    if any(k in c for k in ("casualty", "injury")):
        return "casualty"
    if any(
        k in c
        for k in (
            "missing_story",
            "missing_main",
            "duplicate_story",
            "unknown_story",
            "order_mismatch",
        )
    ):
        return "story"
    if any(k in c for k in ("hallucinat", "invented", "unexpected_")):
        return "hallucination"
    if any(k in c for k in ("date", "time", "percent", "money", "temperature", "score", "number")):
        return "number"
    if any(k in c for k in ("entity", "name", "protected_term")):
        return "entity"
    if any(
        k in c
        for k in ("neutral", "moderator", "anchor", "register", "fluency", "chronology", "negation")
    ):
        return "style"
    return c or "other"


def _deterministic_findings(deterministic_qa: dict | None) -> list[dict]:
    if not deterministic_qa:
        return []
    findings = []
    for finding in deterministic_qa.get("findings") or []:
        findings.append(
            {
                "story_id": finding.get("story_id"),
                "category": finding.get("category") or "other",
                "severity": finding.get("severity") or "minor",
                "source_excerpt": finding.get("source_excerpt") or "",
                "target_excerpt": finding.get("target_excerpt") or "",
                "explanation": finding.get("explanation") or finding.get("category") or "finding",
                "required_action": finding.get("required_action") or "Review manually.",
                "source_segment_ids": finding.get("source_segment_ids") or [],
            }
        )
    return findings


def _transcript_findings(transcript_unresolved: list[dict]) -> list[dict]:
    findings = []
    for item in transcript_unresolved:
        findings.append(
            {
                "story_id": None,
                "category": f"unresolved_transcript_{item.get('category', 'item')}",
                "severity": "major",
                "source_excerpt": item.get("message") or "",
                "target_excerpt": ", ".join(item.get("segment_ids") or []),
                "explanation": (
                    f"Transcript QA left this {item.get('severity', 'issue')} unresolved: "
                    f"{item.get('message') or item.get('category')}."
                ),
                "required_action": "Resolve the transcript issue before publishing.",
                "source_segment_ids": item.get("segment_ids") or [],
            }
        )
    return findings


def _model_error_finding(provider: str, model: str) -> dict:
    return {
        "story_id": None,
        "category": "qa_model_error",
        "severity": "major",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "The independent QA model did not return valid structured findings.",
        "required_action": "Re-run QA or review the target manually.",
        "_detector": "llm",
        "_provider": provider,
        "_model": model,
    }


def _cost_limit_finding() -> dict:
    return {
        "story_id": None,
        "category": "cost_limit",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "The cumulative episode cost limit was reached before QA could verify.",
        "required_action": "Raise max_episode_cost_usd or reduce upstream cost, then re-run QA.",
        "source_segment_ids": [],
    }


def _build_qa_prompt(
    session: Session,
    episode: Episode,
    settings: Settings,
    german_text: str,
    story_structure: list[dict],
    turkish_text: str,
    deterministic_qa: dict | None,
    transcript_unresolved: list[dict],
    escalation_note: str,
) -> tuple[str, str]:
    profile_namespace = None
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None)
        if name:
            profile_namespace = getattr(get_registry(settings).get(name), "prompt_namespace", None)
    except Exception:  # noqa: BLE001
        profile_namespace = None

    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("qa_review.md", profile=profile_namespace)
    _, template_body = registry.load_template(template_file)
    system_prompt, user_template = _split_prompt(template_body)

    glossary_terms: dict = {}
    try:
        from btcedu.prompts.glossary_loader import load_glossary

        glossary = load_glossary(getattr(episode, "content_profile", None)) or {}
        glossary_terms = glossary.get("protected_terms", {}) or {}
    except Exception:  # noqa: BLE001
        glossary_terms = {}

    det_findings = (deterministic_qa or {}).get("findings", [])
    user_message = (
        user_template.replace("{{ german_source }}", german_text)
        .replace(
            "{{ story_structure }}",
            json.dumps(story_structure, ensure_ascii=False, indent=2),
        )
        .replace("{{ turkish_final }}", turkish_text)
        .replace("{{ glossary }}", json.dumps(glossary_terms, ensure_ascii=False, indent=2))
        .replace(
            "{{ deterministic_findings }}",
            json.dumps(det_findings, ensure_ascii=False, indent=2, default=str),
        )
        .replace(
            "{{ transcript_unresolved }}",
            json.dumps(transcript_unresolved, ensure_ascii=False, indent=2),
        )
        .replace("{{ escalation_note }}", escalation_note or "")
    )
    return system_prompt, user_message


def _parse_llm_findings(text: str) -> dict | None:
    try:
        data = json.loads(_extract_json_object(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("findings"), list):
        return None
    disputed_raw = data.get("disputed_deterministic_categories", [])
    if not isinstance(disputed_raw, list):
        return None
    findings = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            return None
        if item.get("story_id") is not None and not isinstance(item.get("story_id"), str):
            return None
        category_value = item.get("category")
        severity_value = item.get("severity")
        explanation_value = item.get("explanation")
        action_value = item.get("required_action")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (category_value, severity_value, explanation_value, action_value)
        ):
            return None
        if not isinstance(item.get("source_excerpt", ""), str) or not isinstance(
            item.get("target_excerpt", ""), str
        ):
            return None
        category = category_value.strip()
        severity = severity_value.strip().lower()
        if severity not in ("info", "minor", "major", "critical"):
            return None
        findings.append(
            {
                "story_id": item.get("story_id") or None,
                "category": category,
                "severity": severity,
                "source_excerpt": str(item.get("source_excerpt") or "")[:500],
                "target_excerpt": str(item.get("target_excerpt") or "")[:500],
                "explanation": explanation_value.strip(),
                "required_action": action_value.strip(),
            }
        )
    disputed = {str(entry).strip().lower() for entry in disputed_raw if str(entry).strip()}
    return {"findings": findings, "disputed": disputed}


def _call_llm_qa(
    system_prompt: str,
    user_message: str,
    settings: Settings,
    provider: str,
    model: str,
    max_tokens: int = 4000,
    before_call=None,
) -> tuple[dict | None, list[ClaudeResponse], bool]:
    """Call the QA model, validating JSON with a single bounded retry."""
    responses: list[ClaudeResponse] = []
    active_message = user_message
    for attempt in range(2):
        if before_call is not None and not before_call(sum(r.cost_usd for r in responses)):
            return None, responses, True
        response = call_claude(
            system_prompt=system_prompt,
            user_message=active_message,
            settings=settings,
            max_tokens=max_tokens,
            json_mode=True,
            model_override=model or None,
            provider_override=provider or None,
        )
        responses.append(response)
        parsed = _parse_llm_findings(response.text)
        if parsed is not None:
            return parsed, responses, False
        active_message = (
            user_message
            + "\n\nThe previous answer was not a valid JSON object with a 'findings' array. "
            "Return ONLY the JSON object described above."
        )
    return None, responses, False


def _escalation_note(triggers: set[str]) -> str:
    return (
        "ESKALATION: Diese Prüfung wurde wegen erhöhter Risikohinweise angefordert "
        f"({', '.join(sorted(triggers))}). Prüfe diese Punkte besonders streng und "
        "bestätige oder widerlege sie konkret."
    )


def _merge_findings(
    det_findings: list[dict],
    model_raw: list[dict],
    disputed: set[str],
    previous_gate: dict | None,
    generation: int,
) -> list[QAFinding]:
    findings: list[QAFinding] = []
    previous = list((previous_gate or {}).get("findings") or [])
    previous_by_identity: dict[tuple, list[dict]] = {}
    used_previous_ids: set[str] = set()
    max_id = 0
    for item in previous:
        finding_id = str(item.get("finding_id") or "")
        if finding_id.startswith("qa-") and finding_id[3:].isdigit():
            max_id = max(max_id, int(finding_id[3:]))
        detector_group = "deterministic" if item.get("detector") == "deterministic" else "llm"
        identity = (
            item.get("story_id"),
            _category_bucket(item.get("category", "")),
            detector_group,
            str(item.get("source_excerpt", "")).strip().casefold(),
            str(item.get("target_excerpt", "")).strip().casefold(),
            str(item.get("explanation", "")).strip().casefold(),
        )
        previous_by_identity.setdefault(identity, []).append(item)

    def _next_id() -> str:
        nonlocal max_id
        max_id += 1
        return f"qa-{max_id:04d}"

    def _identity(
        story_id: str | None,
        category: str,
        detector: str,
        source_excerpt: str,
        target_excerpt: str,
        explanation: str,
    ) -> tuple:
        detector_group = "deterministic" if detector == "deterministic" else "llm"
        return (
            story_id,
            _category_bucket(category),
            detector_group,
            source_excerpt.strip().casefold(),
            target_excerpt.strip().casefold(),
            explanation.strip().casefold(),
        )

    def _history_and_id(
        story_id: str | None,
        category: str,
        detector: str,
        status: str,
        source_excerpt: str,
        target_excerpt: str,
        explanation: str,
        note: str | None = None,
    ) -> tuple[str, list[QAFindingEvent]]:
        candidates = previous_by_identity.get(
            _identity(
                story_id,
                category,
                detector,
                source_excerpt,
                target_excerpt,
                explanation,
            ),
            [],
        )
        previous_item = next(
            (item for item in candidates if item.get("finding_id") not in used_previous_ids),
            None,
        )
        if previous_item is None:
            return _next_id(), [QAFindingEvent(generation=generation, status=status, note=note)]
        finding_id = previous_item["finding_id"]
        used_previous_ids.add(finding_id)
        history = [QAFindingEvent(**event) for event in previous_item.get("history") or []]
        history.append(QAFindingEvent(generation=generation, status=status, note=note))
        return finding_id, history

    det_keys: set[tuple] = set()
    disputed_buckets = {_category_bucket(entry) for entry in disputed} | disputed

    for det in det_findings:
        bucket = _category_bucket(det["category"])
        det_keys.add((det.get("story_id"), bucket))
        contradiction = bucket in disputed_buckets or det["category"].lower() in disputed
        finding_id, history = _history_and_id(
            det.get("story_id"),
            det["category"],
            "deterministic",
            "open",
            det.get("source_excerpt", ""),
            det.get("target_excerpt", ""),
            det["explanation"],
        )
        findings.append(
            QAFinding(
                finding_id=finding_id,
                story_id=det.get("story_id"),
                category=det["category"],
                severity=det["severity"],
                source_excerpt=det.get("source_excerpt", ""),
                target_excerpt=det.get("target_excerpt", ""),
                explanation=det["explanation"],
                required_action=det["required_action"],
                source_segment_ids=det.get("source_segment_ids", []),
                detector="deterministic",
                status="open",
                provider="deterministic",
                model="translation-qa",
                retry_generation=generation,
                contradiction=contradiction,
                history=history,
            )
        )

    seen_model_keys: set[tuple] = set()
    for model_finding in model_raw:
        bucket = _category_bucket(model_finding["category"])
        evidence_key = (
            model_finding.get("story_id"),
            bucket,
            model_finding.get("source_excerpt", "").strip().casefold(),
            model_finding.get("target_excerpt", "").strip().casefold(),
        )
        duplicate = (
            model_finding.get("story_id"),
            bucket,
        ) in det_keys or evidence_key in seen_model_keys
        seen_model_keys.add(evidence_key)
        status = "dismissed" if duplicate else "open"
        note = "duplicate finding" if duplicate else None
        finding_id, history = _history_and_id(
            model_finding.get("story_id"),
            model_finding["category"],
            model_finding.get("_detector", "llm"),
            status,
            model_finding.get("source_excerpt", ""),
            model_finding.get("target_excerpt", ""),
            model_finding["explanation"],
            note,
        )
        findings.append(
            QAFinding(
                finding_id=finding_id,
                story_id=model_finding.get("story_id"),
                category=model_finding["category"],
                severity=model_finding["severity"],
                source_excerpt=model_finding.get("source_excerpt", ""),
                target_excerpt=model_finding.get("target_excerpt", ""),
                explanation=model_finding["explanation"],
                required_action=model_finding["required_action"],
                source_segment_ids=model_finding.get("source_segment_ids", []),
                detector=model_finding.get("_detector", "llm"),
                status=status,
                provider=model_finding.get("_provider"),
                model=model_finding.get("_model"),
                retry_generation=generation,
                contradiction=(
                    (model_finding.get("story_id"), bucket) in det_keys
                    and (
                        bucket in disputed_buckets or model_finding["category"].lower() in disputed
                    )
                ),
                history=history,
            )
        )

    if previous:
        for prev in previous:
            if prev.get("finding_id") in used_previous_ids:
                continue
            previous_status = prev.get("status", "open")
            status = "resolved" if previous_status == "open" else previous_status
            history = [QAFindingEvent(**event) for event in (prev.get("history") or [])]
            if status != previous_status:
                history.append(
                    QAFindingEvent(
                        generation=generation,
                        status="resolved",
                        note="resolved after targeted repair",
                    )
                )
            findings.append(
                QAFinding(
                    finding_id=prev.get("finding_id") or _next_id(),
                    story_id=prev.get("story_id"),
                    category=prev.get("category", "other"),
                    severity=prev.get("severity", "minor"),
                    source_excerpt=prev.get("source_excerpt", ""),
                    target_excerpt=prev.get("target_excerpt", ""),
                    explanation=prev.get("explanation", "historical finding"),
                    required_action=prev.get("required_action", "No action needed."),
                    source_segment_ids=prev.get("source_segment_ids", []),
                    detector=prev.get("detector", "deterministic"),
                    status=status,
                    provider=prev.get("provider"),
                    model=prev.get("model"),
                    retry_generation=generation,
                    contradiction=bool(prev.get("contradiction", False)),
                    history=history,
                )
            )
    return findings


def _evaluate_triggers(
    findings: list[QAFinding],
    transcript_unresolved: list[dict],
    config: dict,
) -> set[str]:
    triggers: set[str] = set()
    open_findings = [f for f in findings if f.status == "open"]
    buckets = {_category_bucket(f.category) for f in open_findings}
    categories = {f.category.lower() for f in open_findings}
    if "hallucination" in buckets:
        triggers.add("suspected_hallucination")
    if any("missing_story" in c or "missing_main" in c for c in categories):
        triggers.add("missing_story")
    if "casualty" in buckets:
        triggers.add("casualty_claim")
    if "legal" in buckets or any("legal" in c for c in categories):
        triggers.add("legal_claim")
    if any(
        item.get("blocking") or item.get("severity") in ("critical", "major")
        for item in transcript_unresolved
    ):
        triggers.add("unresolved_transcript")
    if any(f.contradiction for f in findings):
        triggers.add("contradictory_findings")
    configured = set(config.get("escalation", {}).get("trigger_on") or [])
    return triggers & configured & ESCALATION_TRIGGERS


def _evaluate_decision(
    findings: list[QAFinding],
    transcript_unresolved: list[dict],
    config: dict,
) -> tuple[str, list[str]]:
    gate = config.get("quality_gate", {})
    open_findings = [f for f in findings if f.status == "open"]
    critical = [f for f in open_findings if f.severity == "critical"]
    major = [f for f in open_findings if f.severity == "major"]
    minor = [f for f in open_findings if f.severity == "minor"]
    max_minor = int(gate.get("max_minor_findings", 5))
    reasons: list[str] = []
    red = False

    if gate.get("block_on_critical", True) and critical:
        red = True
        reasons.append("critical_findings")
    missing = [
        f
        for f in open_findings
        if _category_bucket(f.category) == "story"
        and ("missing_story" in f.category.lower() or "missing_main" in f.category.lower())
    ]
    if gate.get("block_on_missing_story", True) and missing:
        red = True
        reasons.append("missing_story")
    transcript_block = any(
        item.get("blocking") or item.get("severity") == "critical" for item in transcript_unresolved
    )
    if gate.get("block_on_unresolved_transcript", True) and transcript_block:
        red = True
        reasons.append("unresolved_transcript")
    if any(
        _category_bucket(f.category) == "hallucination"
        or f.category.lower()
        in {
            "factual_deviation",
            "unsupported_addition",
            "date_mismatch",
            "score_mismatch",
            "result_mismatch",
            "semantic_role_disagreement",
        }
        for f in open_findings
    ):
        red = True
        reasons.append("critical_factual_risk")
    if any(_category_bucket(f.category) == "cost" for f in open_findings):
        red = True
        reasons.append("cost_limit")

    if red:
        return "red", sorted(set(reasons))
    if critical or major or len(minor) > max_minor:
        if critical:
            reasons.append("critical_findings")
        if major:
            reasons.append("major_findings")
        if len(minor) > max_minor:
            reasons.append("too_many_minor_findings")
        return "yellow", sorted(set(reasons))
    return "green", []


def _summarize(findings: list[QAFinding]) -> QualityGateSummary:
    open_findings = [f for f in findings if f.status == "open"]
    return QualityGateSummary(
        info_count=sum(f.severity == "info" for f in open_findings),
        minor_count=sum(f.severity == "minor" for f in open_findings),
        major_count=sum(f.severity == "major" for f in open_findings),
        critical_count=sum(f.severity == "critical" for f in open_findings),
        open_count=len(open_findings),
        resolved_count=sum(f.status == "resolved" for f in findings),
        dismissed_count=sum(f.status == "dismissed" for f in findings),
        contradiction_count=sum(f.contradiction for f in findings),
    )


def _render_gate_markdown(gate: QualityGateDocument) -> str:
    lines = [f"# Übersetzungs-Qualitätsgate: {gate.decision.upper()}\n"]
    lines.append(
        "Findings offen: "
        f"critical={gate.summary.critical_count}, major={gate.summary.major_count}, "
        f"minor={gate.summary.minor_count} · gelöst={gate.summary.resolved_count} · "
        f"Kosten=${gate.total_cost_usd:.4f}\n"
    )
    if gate.reasons:
        lines.append("**Gründe:** " + ", ".join(gate.reasons) + "\n")
    open_findings = [f for f in gate.findings if f.status == "open"]
    if open_findings:
        lines.append("## Offene Findings\n")
        for finding in open_findings:
            story = f"[{finding.story_id}] " if finding.story_id else ""
            lines.append(
                f"- {finding.severity.upper()} · {story}{finding.category} — {finding.explanation}"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _legacy_view(gate: QualityGateDocument, model: str) -> dict:
    """Backward-compatible ``qa_review.json`` projection of the gate."""
    open_findings = [f for f in gate.findings if f.status == "open"]
    return {
        "model": model,
        "generated_at": gate.generated_at.isoformat(),
        "quality_gate": True,
        "decision": gate.decision,
        "status": gate.decision,
        "overall_score": None,
        "summary": (
            f"Quality gate: {gate.decision.upper()}"
            + (f" ({', '.join(gate.reasons)})" if gate.reasons else "")
        ),
        "reasons": gate.reasons,
        "findings": [f.model_dump(mode="json") for f in gate.findings],
        "missing_content": [
            f.explanation for f in open_findings if _category_bucket(f.category) == "story"
        ],
        "hallucinations": [
            f.explanation for f in open_findings if _category_bucket(f.category) == "hallucination"
        ],
        "neutralization_gaps": [
            f.explanation for f in open_findings if _category_bucket(f.category) == "style"
        ],
        "top_fixes": [f.required_action for f in open_findings[:5]],
        "stories": [],
    }


def _result_from_gate(
    gate: dict,
    json_path: str,
    md_path: str,
    gate_path: str,
    model: str,
    *,
    skipped: bool,
    reason: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    escalated: bool = False,
) -> QaReviewResult:
    summary = gate.get("summary", {})
    return QaReviewResult(
        episode_id=gate.get("episode_id", ""),
        json_path=json_path,
        markdown_path=md_path,
        gate_path=gate_path,
        model=model,
        decision=gate.get("decision", ""),
        blocked=bool(gate.get("blocked", gate.get("decision") == "red")),
        deterministic_status=gate.get("deterministic_status", ""),
        issue_count=int(summary.get("open_count", 0)),
        critical_count=int(summary.get("critical_count", 0)),
        major_count=int(summary.get("major_count", 0)),
        minor_count=int(summary.get("minor_count", 0)),
        generation=int(gate.get("retry_generation", 0)),
        escalated=escalated
        or any(c.get("kind") == "escalation" for c in gate.get("model_calls", [])),
        narration_sha256=gate.get("narration_sha256"),
        reasons=list(gate.get("reasons", [])),
        cost_usd=float(gate.get("total_cost_usd", 0.0)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        skipped=skipped,
        reason=reason,
    )


def generate_qa_review(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    *,
    previous_gate: dict | None = None,
    generation: int = 0,
    retry_history: list[dict] | None = None,
) -> QaReviewResult:
    """Run the merged deterministic + independent-model quality gate.

    Produces ``translation_quality_gate.json`` (the authoritative artifact) plus
    backward-compatible ``qa_review.json``/``qa_review.md``. RED blocks the
    pipeline; GREEN records the SHA-256 of the exact downstream narration.
    """
    json_path, md_path, prov_path = _qa_paths(settings, episode_id)
    gate_path = _gate_path(settings, episode_id)
    model_label = getattr(settings, "qa_model", "") or getattr(settings, "claude_model", "")

    def _skip(reason: str) -> QaReviewResult:
        logger.info("QA gate skipped for %s: %s", episode_id, reason)
        return QaReviewResult(
            episode_id=episode_id,
            json_path=str(json_path),
            markdown_path=str(md_path),
            gate_path=str(gate_path),
            model=model_label,
            skipped=True,
            reason=reason,
        )

    if not getattr(settings, "qa_review_enabled", False):
        return _skip("qa_review_enabled=False")
    if settings.dry_run:
        return _skip("dry_run")

    adapted_path, turkish_text = _downstream_narration(settings, episode_id)
    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if adapted_path is None or not turkish_text.strip():
        return _skip(f"target narration missing for {episode_id}")
    if not corrected_path.exists():
        return _skip(f"corrected German transcript missing: {corrected_path}")

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        return _skip(f"episode not found: {episode_id}")

    config = resolve_qa_config(settings, episode)
    standard = config["standard"]
    std_provider = standard.get("provider") or getattr(settings, "llm_provider", "anthropic")
    std_model = standard.get("model") or model_label

    # 1. Deterministic checks first.
    deterministic_qa: dict | None = None
    if config.get("deterministic_checks", True):
        from btcedu.core.translation_qa import load_translation_qa, run_translation_qa

        det_result = run_translation_qa(session, episode_id, settings, force=force)
        deterministic_qa = load_translation_qa(settings, episode_id)
        allowed_skip = {
            "story-based source or target artifact missing",
            "translation QA disabled by profile",
        }
        if deterministic_qa is None and getattr(det_result, "reason", "") not in allowed_skip:
            raise RuntimeError("Deterministic translation QA did not produce its artifact")

    german_text = corrected_path.read_text(encoding="utf-8")
    story_structure = _load_story_structure(settings, episode_id)
    transcript_unresolved = _load_transcript_unresolved(settings, episode_id)
    standard_system_prompt, standard_user_message = _build_qa_prompt(
        session,
        episode,
        settings,
        german_text,
        story_structure,
        turkish_text,
        deterministic_qa,
        transcript_unresolved,
        escalation_note="",
    )

    fingerprint = _sha256_text(
        ":".join(
            [
                _sha256_text(german_text),
                _sha256_text(turkish_text),
                _sha256_text(json.dumps(config, sort_keys=True, default=str)),
                _sha256_text(json.dumps(deterministic_qa or {}, sort_keys=True, default=str)),
                _sha256_text(standard_system_prompt),
                _sha256_text(standard_user_message),
            ]
        )
    )
    if not force and gate_path.exists() and prov_path.exists():
        try:
            previous_prov = json.loads(prov_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous_prov = {}
        if previous_prov.get("fingerprint") == fingerprint:
            existing = load_quality_gate(settings, episode_id, strict=True)
            if existing is not None:
                logger.info("QA gate current for %s (use force to re-run)", episode_id)
                return _result_from_gate(
                    existing,
                    str(json_path),
                    str(md_path),
                    str(gate_path),
                    std_model,
                    skipped=True,
                    reason="already current",
                    input_tokens=previous_prov.get("input_tokens", 0),
                    output_tokens=previous_prov.get("output_tokens", 0),
                )
            logger.warning("QA gate cache is invalid for %s; regenerating", episode_id)

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.REVIEW,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()
    try:
        det_findings = _deterministic_findings(deterministic_qa)
        det_findings.extend(_transcript_findings(transcript_unresolved))
        model_raw: list[dict] = []
        disputed: set[str] = set()
        model_calls: list[QAModelCall] = []
        total_input = 0
        total_output = 0
        spent_before_qa = _cumulative_cost(session, episode)
        current_qa_cost = 0.0
        cost_limit = float(getattr(settings, "max_episode_cost_usd", 15.0))

        def _budget_available(attempt_cost: float = 0.0) -> bool:
            return spent_before_qa + current_qa_cost + attempt_cost < cost_limit

        # 2. Standard independent model — cost-guarded.
        if not _budget_available():
            det_findings.append(_cost_limit_finding())
            model_calls.append(
                QAModelCall(
                    kind="standard",
                    provider=std_provider,
                    model=std_model,
                    generation=generation,
                    ok=False,
                    error="cost_limit",
                )
            )
        else:
            parsed, responses, call_cost_limited = _call_llm_qa(
                standard_system_prompt,
                standard_user_message,
                settings,
                std_provider,
                std_model,
                before_call=_budget_available,
            )
            std_cost = sum(r.cost_usd for r in responses)
            current_qa_cost += std_cost
            total_input += sum(r.input_tokens for r in responses)
            total_output += sum(r.output_tokens for r in responses)
            model_calls.append(
                QAModelCall(
                    kind="standard",
                    provider=std_provider,
                    model=std_model,
                    generation=generation,
                    input_tokens=sum(r.input_tokens for r in responses),
                    output_tokens=sum(r.output_tokens for r in responses),
                    cost_usd=std_cost,
                    ok=parsed is not None and not call_cost_limited,
                    error=(
                        "cost_limit"
                        if call_cost_limited
                        else None
                        if parsed is not None
                        else "invalid_json"
                    ),
                )
            )
            if call_cost_limited:
                det_findings.append(_cost_limit_finding())
            elif parsed is None:
                model_raw.append(_model_error_finding(std_provider, std_model))
            else:
                disputed.update(parsed["disputed"])
                for item in parsed["findings"]:
                    model_raw.append(
                        {**item, "_detector": "llm", "_provider": std_provider, "_model": std_model}
                    )

        # 3. Escalation — only for configured triggers, cost-guarded.
        merged = _merge_findings(det_findings, model_raw, disputed, previous_gate, generation)
        escalated = False
        escalation = config.get("escalation", {})
        triggers = _evaluate_triggers(merged, transcript_unresolved, config)
        if triggers and escalation.get("enabled"):
            esc_provider = escalation.get("provider") or std_provider
            esc_model = escalation.get("model") or std_model
            if not _budget_available():
                det_findings.append(_cost_limit_finding())
                model_calls.append(
                    QAModelCall(
                        kind="escalation",
                        provider=esc_provider,
                        model=esc_model,
                        generation=generation,
                        triggered_by=sorted(triggers),
                        ok=False,
                        error="cost_limit",
                    )
                )
            else:
                system_prompt, user_message = _build_qa_prompt(
                    session,
                    episode,
                    settings,
                    german_text,
                    story_structure,
                    turkish_text,
                    deterministic_qa,
                    transcript_unresolved,
                    escalation_note=_escalation_note(triggers),
                )
                parsed, responses, call_cost_limited = _call_llm_qa(
                    system_prompt,
                    user_message,
                    settings,
                    esc_provider,
                    esc_model,
                    before_call=_budget_available,
                )
                esc_cost = sum(r.cost_usd for r in responses)
                current_qa_cost += esc_cost
                total_input += sum(r.input_tokens for r in responses)
                total_output += sum(r.output_tokens for r in responses)
                escalated = True
                model_calls.append(
                    QAModelCall(
                        kind="escalation",
                        provider=esc_provider,
                        model=esc_model,
                        generation=generation,
                        triggered_by=sorted(triggers),
                        input_tokens=sum(r.input_tokens for r in responses),
                        output_tokens=sum(r.output_tokens for r in responses),
                        cost_usd=esc_cost,
                        ok=parsed is not None and not call_cost_limited,
                        error=(
                            "cost_limit"
                            if call_cost_limited
                            else None
                            if parsed is not None
                            else "invalid_json"
                        ),
                    )
                )
                if call_cost_limited:
                    det_findings.append(_cost_limit_finding())
                elif parsed is None:
                    model_raw.append(_model_error_finding(esc_provider, esc_model))
                else:
                    disputed.update(parsed["disputed"])
                    for item in parsed["findings"]:
                        model_raw.append(
                            {
                                **item,
                                "_detector": "llm_escalation",
                                "_provider": esc_provider,
                                "_model": esc_model,
                            }
                        )
            merged = _merge_findings(det_findings, model_raw, disputed, previous_gate, generation)

        decision, reasons = _evaluate_decision(merged, transcript_unresolved, config)
        summary = _summarize(merged)
        deterministic_status = (deterministic_qa or {}).get("status", "green")

        narration_hash = None
        narration_approved = False
        if decision == "green":
            # Lock the *canonical* narrative (ordered adapted/translated text),
            # not the raw turkish_text/JSON — so QA and chapterization agree on
            # exactly which characters are approved.
            narration_hash = narration_sha256(settings, episode_id) or _sha256_text(turkish_text)
            narration_approved = True

        std_cost_total = sum(c.cost_usd for c in model_calls if c.kind == "standard")
        esc_cost_total = sum(c.cost_usd for c in model_calls if c.kind == "escalation")
        history_records = list(retry_history or [])
        for record in history_records:
            if record.get("resulting_status") is None:
                record["resulting_status"] = decision

        gate = QualityGateDocument(
            episode_id=episode_id,
            generated_at=_utcnow(),
            decision=decision,
            status=decision,
            blocked=decision == "red",
            deterministic_status=deterministic_status,
            reasons=reasons,
            findings=merged,
            summary=summary,
            model_calls=model_calls,
            retry_history=history_records,
            retry_generation=generation,
            standard_cost_usd=std_cost_total,
            escalation_cost_usd=esc_cost_total,
            total_cost_usd=std_cost_total + esc_cost_total,
            narration_sha256=narration_hash,
            narration_approved=narration_approved,
            gate_config=config,
        )

        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(
            json.dumps(gate.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        json_path.write_text(
            json.dumps(_legacy_view(gate, std_model), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        md_path.write_text(_render_gate_markdown(gate), encoding="utf-8")

        prov_path.parent.mkdir(parents=True, exist_ok=True)
        prov_path.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "decision": decision,
                    "generation": generation,
                    "standard_model": std_model,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "cost_usd": std_cost_total + esc_cost_total,
                    "generated_at": _utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        cost_limited = "cost_limit" in reasons
        pipeline_run.status = RunStatus.FAILED if cost_limited else RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = total_input
        pipeline_run.output_tokens = total_output
        pipeline_run.estimated_cost_usd = std_cost_total + esc_cost_total
        if cost_limited:
            pipeline_run.error_message = "Episode cost limit reached during translation QA"
            episode.status = EpisodeStatus.COST_LIMIT
        session.flush()

        logger.info(
            "QA gate for %s: %s (gen=%d, critical=%d, major=%d, minor=%d, $%.4f, %.1fs)",
            episode_id,
            decision.upper(),
            generation,
            summary.critical_count,
            summary.major_count,
            summary.minor_count,
            std_cost_total + esc_cost_total,
            time.monotonic() - t0,
        )
        return QaReviewResult(
            episode_id=episode_id,
            json_path=str(json_path),
            markdown_path=str(md_path),
            gate_path=str(gate_path),
            model=std_model,
            decision=decision,
            blocked=decision == "red",
            deterministic_status=deterministic_status,
            issue_count=summary.open_count,
            critical_count=summary.critical_count,
            major_count=summary.major_count,
            minor_count=summary.minor_count,
            generation=generation,
            escalated=escalated,
            narration_sha256=narration_hash,
            reasons=reasons,
            input_tokens=total_input,
            output_tokens=total_output,
            cost_usd=std_cost_total + esc_cost_total,
        )
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        session.flush()
        logger.warning("QA gate failed for %s: %s", episode_id, exc)
        raise


def build_story_findings(gate: dict) -> dict[str, list[dict]]:
    """Compact per-story structured findings for targeted translate/adapt reruns."""
    story_findings: dict[str, list[dict]] = {}
    for finding in gate.get("findings") or []:
        if finding.get("status") != "open":
            continue
        story_id = finding.get("story_id")
        if not story_id:
            continue
        if _category_bucket(finding.get("category", "")) in {"cost", "transcript"}:
            continue
        if finding.get("category") == "qa_model_error":
            continue
        story_findings.setdefault(story_id, []).append(
            {
                "finding_id": finding.get("finding_id"),
                "category": finding.get("category"),
                "severity": finding.get("severity"),
                "explanation": finding.get("explanation"),
                "source": finding.get("source_excerpt"),
                "target": finding.get("target_excerpt"),
                "required_action": finding.get("required_action"),
            }
        )
    return story_findings


def target_story_ids_from_gate(gate: dict) -> list[str]:
    """Story IDs with open, repairable findings (excludes transcript/cost/model errors)."""
    return sorted(build_story_findings(gate).keys())


def apply_targeted_repair(
    session: Session,
    episode_id: str,
    settings: Settings,
    gate: dict,
) -> list[str]:
    """Re-run translate + adapt for only the stories carrying open findings."""
    story_findings = build_story_findings(gate)
    targets = sorted(story_findings.keys())
    if not targets:
        return []
    review_dir = Path(settings.outputs_dir) / episode_id / "review"
    reviewed_sidecars = [
        review_dir / "script.adapted.reviewed.tr.md",
        review_dir / "stories_translated.reviewed.json",
    ]
    if any(path.exists() for path in reviewed_sidecars):
        logger.warning(
            "Targeted repair skipped for %s because reviewed narration is authoritative",
            episode_id,
        )
        return []

    from btcedu.core.adapter import adapt_script
    from btcedu.core.translator import translate_transcript
    from btcedu.services.errors import ErrorCategory, PipelineError

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is None:
        raise ValueError(f"Episode not found: {episode_id}")
    cost_limit = float(getattr(settings, "max_episode_cost_usd", 15.0))

    def _budget_check(base_cost: float):
        return lambda pending_cost=0.0: base_cost + pending_cost < cost_limit

    translate_base = _cumulative_cost(session, episode)
    if not _budget_check(translate_base)():
        episode.status = EpisodeStatus.COST_LIMIT
        session.commit()
        raise PipelineError(
            "Episode cost limit reached before targeted translation repair",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )

    try:
        translate_transcript(
            session,
            episode_id,
            settings,
            force=True,
            target_story_ids=targets,
            structured_findings=story_findings,
            budget_check=_budget_check(translate_base),
        )
    except PipelineError as exc:
        if exc.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
            session.commit()
        raise
    adapt_base = _cumulative_cost(session, episode)
    if not _budget_check(adapt_base)():
        episode.status = EpisodeStatus.COST_LIMIT
        session.commit()
        raise PipelineError(
            "Episode cost limit reached before targeted adaptation repair",
            ErrorCategory.PERMANENT_COST_LIMIT,
        )
    try:
        adapt_script(
            session,
            episode_id,
            settings,
            force=True,
            target_story_ids=targets,
            structured_findings=story_findings,
            budget_check=_budget_check(adapt_base),
        )
    except PipelineError as exc:
        if exc.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
            session.commit()
        raise
    return targets


def gate_review_artifacts(settings: Settings, episode_id: str) -> list[str]:
    """Artifact paths that bind a manual translation_qa review to current content.

    Includes the quality gate document and the exact downstream narration file so
    an approval becomes stale the moment either changes.
    """
    artifacts = [str(_gate_path(settings, episode_id))]
    narration_path, _ = _downstream_narration(settings, episode_id)
    if narration_path is not None:
        artifacts.append(str(narration_path))
    return artifacts


def resolve_translation_quality_gate(
    session: Session,
    episode_id: str,
    settings: Settings,
    *,
    max_retries: int | None = None,
    allow_retry: bool = True,
    force: bool = False,
) -> QaReviewResult:
    """Evaluate the gate and apply bounded targeted repairs on YELLOW.

    Returns the final gate result. RED or an exhausted non-green gate is left for
    the caller (pipeline / web) to convert into a blocking review task. Retry
    state persists across pipeline runs via the gate's ``retry_generation`` so the
    automatic-repair budget is never exceeded.
    """
    result = generate_qa_review(session, episode_id, settings, force=force, generation=0)
    if result.skipped and not result.decision:
        # QA disabled / dry-run / artifacts missing — no active gate.
        return result

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    config = resolve_qa_config(settings, episode) if episode else {}
    if max_retries is None:
        max_retries = int(config.get("quality_gate", {}).get("max_automatic_retries", 2))

    gate = load_quality_gate(settings, episode_id) or {}
    generation = int(gate.get("retry_generation", 0))
    retry_history: list[dict] = list(gate.get("retry_history", []))
    while allow_retry and gate.get("decision") == "yellow" and generation < max_retries:
        targets = target_story_ids_from_gate(gate)
        if not targets:
            break
        finding_ids = [
            finding.get("finding_id")
            for finding in gate.get("findings", [])
            if finding.get("status") == "open" and finding.get("story_id") in targets
        ]
        repair_cost_before = _cumulative_cost(session, episode) if episode else 0.0
        repaired = apply_targeted_repair(session, episode_id, settings, gate)
        if not repaired:
            break
        repair_cost_after = _cumulative_cost(session, episode) if episode else repair_cost_before
        generation += 1
        retry_history.append(
            {
                "generation": generation,
                "action": "translate_adapt",
                "target_story_ids": repaired,
                "finding_ids": [fid for fid in finding_ids if fid],
                "provider": getattr(settings, "llm_provider", None),
                "model": getattr(settings, "claude_model", None),
                "cost_usd": max(0.0, repair_cost_after - repair_cost_before),
                "resulting_status": None,
            }
        )
        result = generate_qa_review(
            session,
            episode_id,
            settings,
            force=True,
            previous_gate=gate,
            generation=generation,
            retry_history=retry_history,
        )
        gate = load_quality_gate(settings, episode_id) or {}
    return result
