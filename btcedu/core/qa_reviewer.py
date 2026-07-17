"""Independent second-opinion QA of the adapted script.

After the adapt stage produces ``script.adapted.tr.md``, this module asks a
*different* model (default gpt-5.6 via Copilot CLI) to critique the target
fassung against the German source — completeness, hallucinations, meaning
errors, neutralization gaps and idiomatic quality. The critique is advisory:
it is written as an artifact and surfaced at Review Gate 2 / in the dashboard,
but it never blocks the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.episode import (
    Episode,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.services.claude_service import (
    ClaudeResponse,
    _extract_json_object,
    call_claude,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class QaReviewResult:
    """Result of an independent QA second opinion."""

    episode_id: str
    json_path: str
    markdown_path: str
    model: str
    overall_score: float | None
    issue_count: int
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
    """Return the persisted QA review dict for an episode, or None if absent."""
    json_path, _, _ = _qa_paths(settings, episode_id)
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


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


def generate_qa_review(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> QaReviewResult:
    """Generate an independent QA second opinion of the adapted script.

    Reads the German corrected transcript and the adapted Turkish script,
    asks ``settings.qa_model`` (a different model than the adaptation) for a
    structured critique, and writes ``qa_review.json`` + ``qa_review.md``.

    Advisory only — the caller must treat any exception as non-fatal.
    """
    json_path, md_path, prov_path = _qa_paths(settings, episode_id)

    def _skip(reason: str) -> QaReviewResult:
        logger.info("QA review skipped for %s: %s", episode_id, reason)
        return QaReviewResult(
            episode_id=episode_id,
            json_path=str(json_path),
            markdown_path=str(md_path),
            model=getattr(settings, "qa_model", ""),
            overall_score=None,
            issue_count=0,
            skipped=True,
            reason=reason,
        )

    if not getattr(settings, "qa_review_enabled", False):
        return _skip("qa_review_enabled=False")
    if settings.dry_run:
        return _skip("dry_run")

    adapted_path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    corrected_path = Path(settings.transcripts_dir) / episode_id / "transcript.corrected.de.txt"
    if not adapted_path.exists():
        return _skip(f"adapted script missing: {adapted_path}")
    if not corrected_path.exists():
        return _skip(f"corrected German transcript missing: {corrected_path}")

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        return _skip(f"episode not found: {episode_id}")

    turkish_text = adapted_path.read_text(encoding="utf-8")
    german_text = corrected_path.read_text(encoding="utf-8")

    qa_model = getattr(settings, "qa_model", "gpt-5.6")

    # Resolve profile-namespaced prompt (falls back to base qa_review.md)
    content_profile = getattr(episode, "content_profile", None)
    profile_namespace: str | None = None
    try:
        from btcedu.profiles import get_registry as get_profile_registry

        pr = get_profile_registry(settings)
        profile_obj = pr.get(content_profile) if content_profile else None
        if profile_obj is not None:
            profile_namespace = getattr(profile_obj, "prompt_namespace", None)
    except Exception:  # noqa: BLE001
        profile_namespace = None

    registry = PromptRegistry(session)
    template_file = registry.resolve_template_path("qa_review.md", profile=profile_namespace)
    prompt_name = "qa_review"
    if profile_namespace and (TEMPLATES_DIR / profile_namespace / "qa_review.md").exists():
        prompt_name = f"{profile_namespace}/qa_review"
    prompt_version = registry.register_version(prompt_name, template_file, set_default=True)
    _, template_body = registry.load_template(template_file)
    prompt_content_hash = registry.compute_hash(template_body)

    # Idempotency: skip if inputs + model + prompt unchanged
    german_hash = hashlib.sha256(german_text.encode("utf-8")).hexdigest()
    turkish_hash = hashlib.sha256(turkish_text.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        f"{german_hash}:{turkish_hash}:{qa_model}:{prompt_content_hash}".encode()
    ).hexdigest()

    if not force and json_path.exists() and prov_path.exists():
        try:
            prev = json.loads(prov_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
        if prev.get("fingerprint") == fingerprint:
            existing = load_qa_review(settings, episode_id) or {}
            logger.info("QA review current for %s (use force to re-run)", episode_id)
            return QaReviewResult(
                episode_id=episode_id,
                json_path=str(json_path),
                markdown_path=str(md_path),
                model=qa_model,
                overall_score=existing.get("overall_score"),
                issue_count=_count_issues(existing),
                input_tokens=prev.get("input_tokens", 0),
                output_tokens=prev.get("output_tokens", 0),
                cost_usd=prev.get("cost_usd", 0.0),
                skipped=True,
                reason="already current",
            )

    system_prompt, user_template = _split_prompt(template_body)
    user_message = user_template.replace("{{ german_source }}", german_text).replace(
        "{{ turkish_final }}", turkish_text
    )

    _template_max = getattr(prompt_version, "max_tokens", None) or 0
    _effective_max = max(int(_template_max or 0), 4000)

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.REVIEW,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    t0 = time.monotonic()
    try:
        response: ClaudeResponse = call_claude(
            system_prompt=system_prompt,
            user_message=user_message,
            settings=settings,
            max_tokens=_effective_max,
            json_mode=True,
            model_override=qa_model,
        )

        raw = _extract_json_object(response.text)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("QA response is not a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("QA response was not valid JSON for %s: %s", episode_id, exc)
            data = {
                "overall_score": None,
                "summary": "QA-Modell lieferte kein gültiges JSON. Rohtext siehe unten.",
                "stories": [],
                "missing_content": [],
                "hallucinations": [],
                "neutralization_gaps": [],
                "top_fixes": [],
                "raw_response": response.text.strip()[:4000],
            }

        data.setdefault("model", qa_model)
        data.setdefault("generated_at", _utcnow().isoformat())

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md_path.write_text(_render_markdown(data, qa_model), encoding="utf-8")

        prov_path.parent.mkdir(parents=True, exist_ok=True)
        prov_path.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "model": qa_model,
                    "prompt_content_hash": prompt_content_hash,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cost_usd": response.cost_usd,
                    "generated_at": _utcnow().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = response.input_tokens
        pipeline_run.output_tokens = response.output_tokens
        pipeline_run.estimated_cost_usd = response.cost_usd
        session.flush()

        elapsed = time.monotonic() - t0
        overall = data.get("overall_score")
        issue_count = _count_issues(data)
        logger.info(
            "QA review complete for %s (%s): score=%s, %d issues, %.1fs, $%.4f",
            episode_id,
            qa_model,
            overall,
            issue_count,
            elapsed,
            response.cost_usd,
        )
        return QaReviewResult(
            episode_id=episode_id,
            json_path=str(json_path),
            markdown_path=str(md_path),
            model=qa_model,
            overall_score=overall if isinstance(overall, (int, float)) else None,
            issue_count=issue_count,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
        )
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        session.flush()
        logger.warning("QA review failed for %s: %s", episode_id, exc)
        raise
