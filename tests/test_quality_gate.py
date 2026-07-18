"""Phase 7 — merged translation quality gate, escalation, retries, and gating."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.qa_reviewer import (
    generate_qa_review,
    load_quality_gate,
    resolve_translation_quality_gate,
)
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.prompt_version import PromptVersion  # noqa: F401 — register table
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.services.claude_service import ClaudeResponse
from btcedu.services.errors import ErrorCategory, PipelineError


def _resp(payload: dict | str) -> ClaudeResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return ClaudeResponse(text=text, input_tokens=10, output_tokens=10, cost_usd=0.0, model="m")


def _qa(findings=None, disputed=None) -> ClaudeResponse:
    return _resp({"findings": findings or [], "disputed_deterministic_categories": disputed or []})


def _story(story_id, order, de, tr, *, lead=False):
    return {
        "story_id": story_id,
        "order": order,
        "headline_de": f"H {story_id}",
        "category": "politik",
        "story_type": "meldung",
        "text_de": de,
        "source_text": de,
        "word_count": len(de.split()),
        "estimated_duration_seconds": 10,
        "source_segment_ids": [f"seg-{order:04d}"],
        "is_lead_story": lead,
        "text_tr": tr,
        "headline_tr": f"H {story_id}",
        "text_adapted_tr": tr,
        "adaptation_operations": [],
    }


def _doc(stories):
    return {
        "schema_version": "1.0",
        "episode_id": "ep-gate",
        "broadcast_date": "2026-07-18",
        "source_attribution": {"source": "tagesschau"},
        "total_stories": len(stories),
        "total_duration_seconds": 10 * len(stories),
        "stories": stories,
    }


def _make_episode(db_session, tmp_path, pairs, *, profile=None, episode_id="ep-gate"):
    """Create a story-mode ADAPTED episode with source/translated/adapted artifacts.

    ``pairs`` is a list of (story_id, german, turkish, lead) tuples.
    """
    t_dir = tmp_path / "transcripts" / episode_id
    t_dir.mkdir(parents=True, exist_ok=True)
    o_dir = tmp_path / "outputs" / episode_id
    o_dir.mkdir(parents=True, exist_ok=True)

    stories = [_story(sid, i + 1, de, tr, lead=lead) for i, (sid, de, tr, lead) in enumerate(pairs)]
    (t_dir / "transcript.corrected.de.txt").write_text(
        "\n\n".join(de for _, de, _, _ in pairs), encoding="utf-8"
    )
    (o_dir / "stories.json").write_text(json.dumps(_doc(stories)), encoding="utf-8")
    (o_dir / "stories_translated.json").write_text(json.dumps(_doc(stories)), encoding="utf-8")
    (o_dir / "stories_adapted.json").write_text(json.dumps(_doc(stories)), encoding="utf-8")
    (o_dir / "script.adapted.tr.md").write_text(
        "\n\n".join(tr for _, _, tr, _ in pairs), encoding="utf-8"
    )

    episode = Episode(
        episode_id=episode_id,
        source="youtube_rss",
        title="Gate Test",
        url=f"https://youtube.com/watch?v={episode_id}",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
        content_profile=profile,
    )
    db_session.add(episode)
    db_session.commit()

    settings = Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        dry_run=False,
        anthropic_api_key="k",
        qa_review_enabled=True,
        qa_model="gpt-5.6-sol",
        pipeline_version=2,
        profiles_dir="btcedu/profiles",
    )
    return episode, settings


# ---------------------------------------------------------------------------
# Standard QA + GREEN / YELLOW / RED
# ---------------------------------------------------------------------------


def test_standard_qa_green_records_narration_hash(db_session, tmp_path):
    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Die Lage bleibt ruhig.", "Durum sakin.", True)]
    )
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa()
        result = generate_qa_review(db_session, "ep-gate", settings)

    assert mock_call.call_count == 1  # standard only, no escalation configured
    assert result.decision == "green"
    gate = load_quality_gate(settings, "ep-gate")
    expected = hashlib.sha256(
        (Path(settings.outputs_dir) / "ep-gate" / "script.adapted.tr.md").read_text().encode()
    ).hexdigest()
    assert gate["narration_sha256"] == expected
    assert gate["narration_approved"] is True


def test_missing_findings_array_cannot_produce_green(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    malformed_shape = _resp({"assessment": "unable to assess"})
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=malformed_shape) as mock_call:
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert mock_call.call_count == 2
    assert result.decision == "yellow"
    gate = load_quality_gate(settings, "ep-gate")
    assert any(f["category"] == "qa_model_error" for f in gate["findings"])


def test_malformed_finding_entry_cannot_produce_green(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    malformed = _resp(
        {"findings": ["critical hallucination"], "disputed_deterministic_categories": []}
    )
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=malformed):
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert result.decision == "yellow"
    gate = load_quality_gate(settings, "ep-gate")
    assert any(f["category"] == "qa_model_error" for f in gate["findings"])


def test_cache_fingerprint_includes_unresolved_transcript(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    blocking = [
        {
            "category": "unresolved_negation",
            "severity": "critical",
            "blocking": True,
            "segment_ids": ["seg-0001"],
            "message": "Negation unresolved",
        }
    ]
    with (
        patch("btcedu.core.qa_reviewer.call_claude", return_value=_qa()) as mock_call,
        patch(
            "btcedu.core.qa_reviewer._load_transcript_unresolved",
            side_effect=[[], blocking],
        ),
    ):
        first = generate_qa_review(db_session, "ep-gate", settings)
        second = generate_qa_review(db_session, "ep-gate", settings)
    assert first.decision == "green"
    assert second.decision == "red"
    assert mock_call.call_count == 2


def test_corrupted_cached_gate_is_regenerated(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=_qa()) as mock_call:
        first = generate_qa_review(db_session, "ep-gate", settings)
        assert first.decision == "green"
        gate_path = Path(settings.outputs_dir) / "ep-gate" / "translation_quality_gate.json"
        gate_path.write_text('{"decision":"green"}', encoding="utf-8")
        second = generate_qa_review(db_session, "ep-gate", settings)
    assert second.decision == "green"
    assert second.skipped is False
    assert mock_call.call_count == 2


def test_standard_qa_yellow_on_major(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    finding = {
        "story_id": "s01",
        "category": "meaning_error",
        "severity": "major",
        "source_excerpt": "a",
        "target_excerpt": "b",
        "explanation": "sense flipped",
        "required_action": "fix",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([finding])
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert result.decision == "yellow"
    assert result.narration_sha256 is None


def test_standard_qa_red_on_hallucination(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    finding = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented deaths",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([finding])
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert result.decision == "red"
    assert result.blocked is True


def test_major_hallucination_is_red(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    finding = {
        "story_id": "s01",
        "category": "invented_fact",
        "severity": "major",
        "source_excerpt": "",
        "target_excerpt": "Erfundene Angabe",
        "explanation": "unsupported addition",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=_qa([finding])):
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert result.decision == "red"
    assert "critical_factual_risk" in result.reasons


# ---------------------------------------------------------------------------
# Deterministic authority + dedup + contradictions
# ---------------------------------------------------------------------------


def test_deterministic_casualty_critical_not_downgraded(db_session, tmp_path):
    # Source casualty count 3, target casualty count 2 -> deterministic critical.
    _, settings = _make_episode(
        db_session,
        tmp_path,
        [("s01", "Bei dem Unglück starben 3 Menschen.", "Kazada 2 kişi öldü.", True)],
    )
    # LLM disputes the casualty finding and reports nothing itself.
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _resp(
            {"findings": [], "disputed_deterministic_categories": ["casualty"]}
        )
        result = generate_qa_review(db_session, "ep-gate", settings)

    gate = load_quality_gate(settings, "ep-gate")
    casualty = [f for f in gate["findings"] if "casualty" in f["category"]]
    assert casualty, "deterministic casualty finding expected"
    # Severity stays critical despite the model dispute.
    assert all(f["severity"] == "critical" for f in casualty)
    # The disputed deterministic finding is flagged as contradicted, not removed.
    assert any(f["contradiction"] for f in casualty)
    assert result.decision == "red"


def test_llm_finding_deduped_against_deterministic(db_session, tmp_path):
    # Deterministic flags a casualty change (3 -> 5). The LLM reports the same
    # issue; its finding must be deduped (dismissed) rather than double-counted.
    _, settings = _make_episode(
        db_session,
        tmp_path,
        [("s10", "Es starben 3 Menschen.", "5 kişi öldü.", True)],
        episode_id="ep-dedup",
    )
    llm_dupe = {
        "story_id": "s10",
        "category": "casualty_claim",
        "severity": "major",
        "source_excerpt": "3",
        "target_excerpt": "5",
        "explanation": "casualty changed",
        "required_action": "fix",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _resp(
            {"findings": [llm_dupe], "disputed_deterministic_categories": []}
        )
        generate_qa_review(db_session, "ep-dedup", settings)
    gate = load_quality_gate(settings, "ep-dedup")
    llm_findings = [f for f in gate["findings"] if f["detector"] == "llm"]
    # The LLM's duplicate casualty finding is dismissed (covered by deterministic).
    assert llm_findings and all(f["status"] == "dismissed" for f in llm_findings)
    assert gate["summary"]["dismissed_count"] >= 1


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


def test_escalation_triggers_for_hallucination(db_session, tmp_path):
    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    responses = [
        _qa([halluc]),
        _qa([halluc]),
    ]
    with patch("btcedu.core.qa_reviewer.call_claude", side_effect=responses) as mock_call:
        generate_qa_review(db_session, "ep-gate", settings)
    assert mock_call.call_count == 2  # standard + escalation
    gate = load_quality_gate(settings, "ep-gate")
    kinds = {c["kind"] for c in gate["model_calls"]}
    assert kinds == {"standard", "escalation"}
    esc = next(c for c in gate["model_calls"] if c["kind"] == "escalation")
    assert esc["model"] == "claude-opus-4.6"
    assert "suspected_hallucination" in esc["triggered_by"]


def test_no_escalation_for_minor_only(db_session, tmp_path):
    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    minor = {
        "story_id": "s01",
        "category": "neutralization_gap",
        "severity": "minor",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "greeting",
        "required_action": "remove greeting",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([minor])
        generate_qa_review(db_session, "ep-gate", settings)
    assert mock_call.call_count == 1  # no escalation
    gate = load_quality_gate(settings, "ep-gate")
    assert all(c["kind"] == "standard" for c in gate["model_calls"])


# ---------------------------------------------------------------------------
# Cost limit
# ---------------------------------------------------------------------------


def test_cost_limit_blocks_and_is_a_real_failure(db_session, tmp_path):
    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    settings.max_episode_cost_usd = 1.0
    db_session.add(
        PipelineRun(
            episode_id=episode.id,
            stage=PipelineStage.TRANSLATE,
            status=RunStatus.SUCCESS,
            estimated_cost_usd=99.0,
        )
    )
    db_session.commit()
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        result = generate_qa_review(db_session, "ep-gate", settings)
    mock_call.assert_not_called()  # cost guard blocks the call
    assert result.decision == "red"
    gate = load_quality_gate(settings, "ep-gate")
    assert any(f["category"] == "cost_limit" for f in gate["findings"])
    assert any(c["error"] == "cost_limit" for c in gate["model_calls"])
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.COST_LIMIT


def test_cost_guard_runs_between_json_retries(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    settings.max_episode_cost_usd = 0.5
    invalid = ClaudeResponse(
        text="not-json",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.6,
        model="m",
    )
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=invalid) as mock_call:
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert mock_call.call_count == 1
    assert result.decision == "red"
    gate = load_quality_gate(settings, "ep-gate")
    assert gate["model_calls"][0]["error"] == "cost_limit"


def test_standard_cost_blocks_escalation_call(db_session, tmp_path):
    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    settings.max_episode_cost_usd = 0.5
    finding = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "invented",
        "explanation": "invented",
        "required_action": "remove",
    }
    costly = ClaudeResponse(
        text=json.dumps({"findings": [finding], "disputed_deterministic_categories": []}),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.6,
        model="m",
    )
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=costly) as mock_call:
        result = generate_qa_review(db_session, "ep-gate", settings)
    assert mock_call.call_count == 1
    assert result.decision == "red"
    gate = load_quality_gate(settings, "ep-gate")
    escalation = next(call for call in gate["model_calls"] if call["kind"] == "escalation")
    assert escalation["error"] == "cost_limit"


# ---------------------------------------------------------------------------
# Bounded retries (success / exhaustion) + resolved history
# ---------------------------------------------------------------------------


def _mutate_narration(settings, episode_id, marker):
    path = Path(settings.outputs_dir) / episode_id / "script.adapted.tr.md"
    path.write_text(f"Metin {marker}.", encoding="utf-8")


def test_retry_success_marks_resolved_history(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    calls = {"n": 0}
    first_finding_ids = []

    def qa_side(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(
                {
                    "findings": [
                        {
                            "story_id": "s01",
                            "category": "meaning_error",
                            "severity": "major",
                            "source_excerpt": "a",
                            "target_excerpt": "b",
                            "explanation": "flipped",
                            "required_action": "fix",
                        }
                    ],
                    "disputed_deterministic_categories": [],
                }
            )
        return _qa()

    def fake_repair(session, episode_id, settings, gate):
        first_finding_ids.extend(
            finding["finding_id"] for finding in gate["findings"] if finding["status"] == "open"
        )
        _mutate_narration(settings, episode_id, "repaired")
        return ["s01"]

    with (
        patch("btcedu.core.qa_reviewer.call_claude", side_effect=qa_side),
        patch("btcedu.core.qa_reviewer.apply_targeted_repair", side_effect=fake_repair),
    ):
        result = resolve_translation_quality_gate(db_session, "ep-gate", settings)

    assert result.decision == "green"
    assert result.generation == 1
    gate = load_quality_gate(settings, "ep-gate")
    assert gate["retry_generation"] == 1
    assert len(gate["retry_history"]) == 1
    assert gate["retry_history"][0]["resulting_status"] == "green"
    assert gate["retry_history"][0]["model"] == settings.claude_model
    assert gate["retry_history"][0]["cost_usd"] == 0
    # The previously-open finding is retained as resolved (never deleted).
    resolved = [f for f in gate["findings"] if f["status"] == "resolved"]
    assert resolved
    assert resolved[0]["finding_id"] in first_finding_ids


def test_retry_exhaustion_stays_yellow(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    counter = {"n": 0}

    def qa_side(*a, **k):
        return _resp(
            {
                "findings": [
                    {
                        "story_id": "s01",
                        "category": "meaning_error",
                        "severity": "major",
                        "source_excerpt": "a",
                        "target_excerpt": "b",
                        "explanation": "flipped",
                        "required_action": "fix",
                    }
                ],
                "disputed_deterministic_categories": [],
            }
        )

    def fake_repair(session, episode_id, settings, gate):
        counter["n"] += 1
        _mutate_narration(settings, episode_id, f"try{counter['n']}")
        return ["s01"]

    with (
        patch("btcedu.core.qa_reviewer.call_claude", side_effect=qa_side),
        patch("btcedu.core.qa_reviewer.apply_targeted_repair", side_effect=fake_repair),
    ):
        result = resolve_translation_quality_gate(db_session, "ep-gate", settings)

    assert result.decision == "yellow"
    assert result.generation == 2  # default max_automatic_retries
    assert counter["n"] == 2


def test_forced_gate_rerun_preserves_retry_budget_and_history(db_session, tmp_path):
    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    gate_path = Path(settings.outputs_dir) / "ep-gate" / "translation_quality_gate.json"
    gate_path.write_text(
        json.dumps(
            {
                "decision": "red",
                "retry_generation": 2,
                "retry_history": [
                    {"generation": 1, "resulting_status": "yellow"},
                    {"generation": 2, "resulting_status": "red"},
                ],
                "findings": [],
            }
        ),
        encoding="utf-8",
    )

    with patch(
        "btcedu.core.qa_reviewer.generate_qa_review",
        return_value=SimpleNamespace(decision="red", generation=2, skipped=False),
    ) as generate:
        result = resolve_translation_quality_gate(
            db_session,
            "ep-gate",
            settings,
            force=True,
            allow_retry=False,
        )

    assert result.decision == "red"
    assert generate.call_args.kwargs["generation"] == 2
    assert len(generate.call_args.kwargs["retry_history"]) == 2
    assert generate.call_args.kwargs["previous_gate"]["decision"] == "red"


# ---------------------------------------------------------------------------
# Selected-story preservation (targeted rerun)
# ---------------------------------------------------------------------------


def test_translate_targeted_preserves_untargeted_story(db_session, tmp_path):
    from btcedu.core.translator import translate_transcript

    _, settings = _make_episode(
        db_session,
        tmp_path,
        [("s01", "Erste.", "Birinci ORIG.", True), ("s02", "Zweite.", "Ikinci ORIG.", False)],
        profile="tagesschau_tr",
    )

    def tr_resp(*a, **k):
        return _resp(
            {
                "story_id": "s02",
                "source_segment_ids": ["seg-0002"],
                "translated_headline": "H s02",
                "translated_text": "Ikinci DUZELTILDI.",
                "translator_flags": [],
                "omitted_uncertain_details": [],
                "glossary_terms_used": [],
            }
        )

    findings = {
        "s02": [
            {
                "finding_id": "qa-0001",
                "category": "meaning_error",
                "severity": "major",
                "explanation": "fix",
                "source": "Zweite",
                "target": "Ikinci",
                "required_action": "correct",
            }
        ]
    }
    with patch("btcedu.core.translator.call_claude", side_effect=tr_resp) as mock_call:
        translate_transcript(
            db_session,
            "ep-gate",
            settings,
            force=True,
            target_story_ids=["s02"],
            structured_findings=findings,
        )

    translated = json.loads(
        (Path(settings.outputs_dir) / "ep-gate" / "stories_translated.json").read_text()
    )
    by_id = {s["story_id"]: s for s in translated["stories"]}
    assert by_id["s01"]["text_tr"] == "Birinci ORIG."  # untargeted preserved
    assert by_id["s02"]["text_tr"] == "Ikinci DUZELTILDI."  # targeted updated
    # Only one LLM call (for s02).
    assert mock_call.call_count == 1
    # The per-story structured finding was injected into the prompt.
    sent = mock_call.call_args.kwargs["user_message"]
    assert "QA-Korrekturen für DIESE Story" in sent


def test_targeted_translation_checks_budget_before_json_retry(db_session, tmp_path):
    from btcedu.core.translator import translate_transcript

    _, settings = _make_episode(
        db_session,
        tmp_path,
        [
            ("s01", "Erste Meldung.", "Birinci ORIG.", True),
            ("s02", "Zweite Meldung.", "Ikinci ORIG.", False),
        ],
        profile="tagesschau_tr",
    )
    invalid = ClaudeResponse(
        text="not-json",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.6,
        model="producer",
    )
    with patch("btcedu.core.translator.call_claude", return_value=invalid) as mock_call:
        with pytest.raises(PipelineError) as exc_info:
            translate_transcript(
                db_session,
                "ep-gate",
                settings,
                force=True,
                target_story_ids=["s02"],
                structured_findings={"s02": []},
                budget_check=lambda pending: pending < 0.5,
            )
    assert exc_info.value.category == ErrorCategory.PERMANENT_COST_LIMIT
    assert mock_call.call_count == 1


def test_explicit_qa_provider_refusal_does_not_fallback(tmp_path):
    from btcedu.services.claude_service import ModelRefusalError, call_claude

    settings = Settings(
        dry_run=False,
        anthropic_api_key="anthropic-key",
        llm_provider="anthropic",
    )
    refusal = ClaudeResponse(
        text="I'm the GitHub Copilot CLI, a terminal assistant.",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0,
        model="qa-model",
    )
    with patch(
        "btcedu.services.claude_service._call_copilot_cli",
        return_value=refusal,
    ) as mock_call:
        with pytest.raises(ModelRefusalError, match="provider fallback is disabled"):
            call_claude(
                "system",
                "user",
                settings,
                provider_override="copilot_cli",
                model_override="qa-model",
            )
    mock_call.assert_called_once()


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("github_models", "GITHUB_TOKEN"),
    ],
)
def test_explicit_provider_requires_its_own_credentials(provider, message):
    from btcedu.services.claude_service import call_claude

    settings = Settings(
        dry_run=False,
        anthropic_api_key="",
        openai_api_key="",
        github_token="",
    )
    with pytest.raises(ValueError, match=message):
        call_claude("system", "user", settings, provider_override=provider)


def test_failed_targeted_repair_persists_partial_cost_and_cost_limit(db_session, tmp_path):
    from btcedu.core.qa_reviewer import apply_targeted_repair

    episode, settings = _make_episode(
        db_session,
        tmp_path,
        [("s01", "Meldung.", "Haber.", True)],
        profile="tagesschau_tr",
    )
    settings.max_episode_cost_usd = 0.5
    invalid = ClaudeResponse(
        text="not-json",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.6,
        model="producer",
    )
    gate = {
        "findings": [
            {
                "finding_id": "qa-0001",
                "story_id": "s01",
                "category": "meaning_error",
                "severity": "major",
                "source_excerpt": "Meldung",
                "target_excerpt": "Haber",
                "explanation": "wrong meaning",
                "required_action": "fix",
                "status": "open",
            }
        ]
    }
    with patch("btcedu.core.translator.call_claude", return_value=invalid):
        with pytest.raises(PipelineError):
            apply_targeted_repair(db_session, "ep-gate", settings, gate)
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.COST_LIMIT
    failed = (
        db_session.query(PipelineRun)
        .filter(
            PipelineRun.episode_id == episode.id,
            PipelineRun.stage == PipelineStage.TRANSLATE,
            PipelineRun.status == RunStatus.FAILED,
        )
        .one()
    )
    assert failed.input_tokens == 10
    assert failed.output_tokens == 20
    assert failed.estimated_cost_usd == pytest.approx(0.6)


def test_targeted_repair_skips_authoritative_reviewed_sidecar(db_session, tmp_path):
    from btcedu.core.qa_reviewer import apply_targeted_repair

    _, settings = _make_episode(
        db_session,
        tmp_path,
        [("s01", "Meldung.", "Haber.", True)],
        profile="tagesschau_tr",
    )
    review_dir = Path(settings.outputs_dir) / "ep-gate" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "script.adapted.reviewed.tr.md").write_text(
        "Manuell freigegeben.", encoding="utf-8"
    )
    gate = {
        "findings": [
            {
                "finding_id": "qa-0001",
                "story_id": "s01",
                "category": "meaning_error",
                "severity": "major",
                "source_excerpt": "Meldung",
                "target_excerpt": "Haber",
                "explanation": "wrong meaning",
                "required_action": "fix",
                "status": "open",
            }
        ]
    }
    with (
        patch("btcedu.core.translator.translate_transcript") as translate,
        patch("btcedu.core.adapter.adapt_script") as adapt,
    ):
        repaired = apply_targeted_repair(db_session, "ep-gate", settings, gate)
    assert repaired == []
    translate.assert_not_called()
    adapt.assert_not_called()


# ---------------------------------------------------------------------------
# Pipeline review_gate_2 decisions
# ---------------------------------------------------------------------------


def _run_gate(db_session, episode, settings):
    from btcedu.core.pipeline import _run_stage

    return _run_stage(db_session, episode, settings, "review_gate_2")


def test_review_gate_2_green_auto_continues(db_session, tmp_path):
    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Ruhig.", "Sakin.", True)])
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa()
        result = _run_gate(db_session, episode, settings)
    assert result.status == "success"
    assert "GREEN" in result.detail


def test_review_gate_2_red_creates_translation_qa_review(db_session, tmp_path):
    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([halluc])
        result = _run_gate(db_session, episode, settings)
    assert result.status == "review_pending"
    task = (
        db_session.query(ReviewTask)
        .filter(ReviewTask.episode_id == "ep-gate", ReviewTask.stage == "translation_qa")
        .first()
    )
    assert task is not None and task.status == ReviewStatus.PENDING.value


def test_unrelated_pending_review_does_not_suppress_red_gate_task(db_session, tmp_path):
    from btcedu.core.reviewer import create_review_task

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    adapted = Path(settings.outputs_dir) / "ep-gate" / "script.adapted.tr.md"
    create_review_task(
        db_session,
        "ep-gate",
        stage="adapt",
        artifact_paths=[str(adapted)],
    )
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude", return_value=_qa([halluc])):
        result = _run_gate(db_session, episode, settings)
    assert result.status == "review_pending"
    assert (
        db_session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == "ep-gate",
            ReviewTask.stage == "translation_qa",
        )
        .first()
        is not None
    )


def test_review_gate_2_tagesschau_autoapprove_red_still_blocks(db_session, tmp_path):
    episode, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([halluc])
        result = _run_gate(db_session, episode, settings)
    # Auto-approve profiles must NOT bypass a RED factual gate.
    assert result.status == "review_pending"
    task = (
        db_session.query(ReviewTask)
        .filter(ReviewTask.episode_id == "ep-gate", ReviewTask.stage == "translation_qa")
        .first()
    )
    assert task is not None


def test_review_gate_2_manual_approval_allows_continue(db_session, tmp_path):
    from btcedu.core.qa_reviewer import gate_review_artifacts
    from btcedu.core.reviewer import approve_review, create_review_task

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([halluc])
        _run_gate(db_session, episode, settings)  # writes RED gate + review task

    # A human approves the artifact-bound translation_qa review.
    artifacts = gate_review_artifacts(settings, "ep-gate")
    task = create_review_task(db_session, "ep-gate", "translation_qa", artifacts)
    approve_review(db_session, task.id)

    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([halluc])
        result = _run_gate(db_session, episode, settings)
    assert result.status == "success"
    assert "manually approved" in result.detail


# ---------------------------------------------------------------------------
# Chapterizer factual gate
# ---------------------------------------------------------------------------


def test_chapterize_blocked_by_red_gate(db_session, tmp_path):
    from btcedu.core.chapterizer import chapterize_script

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    # script.adapted.tr.md exists, so chapterize is NOT story mode -> reads adapted.
    halluc = {
        "story_id": "s01",
        "category": "hallucination",
        "severity": "critical",
        "source_excerpt": "",
        "target_excerpt": "",
        "explanation": "invented",
        "required_action": "remove",
    }
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa([halluc])
        generate_qa_review(db_session, "ep-gate", settings)

    with pytest.raises(ValueError, match="quality gate"):
        chapterize_script(db_session, "ep-gate", settings, force=True)


def test_chapterize_allowed_when_no_gate(db_session, tmp_path):
    # Backward compatibility: no gate artifact -> enforcement is a no-op.
    from btcedu.core.chapterizer import _enforce_translation_quality_gate

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    gate_path = Path(settings.outputs_dir) / "ep-gate" / "translation_quality_gate.json"
    assert not gate_path.exists()
    # Should not raise.
    _enforce_translation_quality_gate(db_session, "ep-gate", settings)


def test_chapterize_fails_closed_when_profile_requires_missing_gate(db_session, tmp_path):
    from btcedu.core.chapterizer import _enforce_translation_quality_gate

    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    with pytest.raises(ValueError, match="requires a valid translation quality gate"):
        _enforce_translation_quality_gate(db_session, "ep-gate", settings)


def test_chapterize_rejects_malformed_green_gate(db_session, tmp_path):
    from btcedu.core.chapterizer import _enforce_translation_quality_gate

    _, settings = _make_episode(
        db_session, tmp_path, [("s01", "Text.", "Metin.", True)], profile="tagesschau_tr"
    )
    gate_path = Path(settings.outputs_dir) / "ep-gate" / "translation_quality_gate.json"
    gate_path.write_text(
        json.dumps({"decision": "green", "narration_sha256": "0" * 64}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires a valid translation quality gate"):
        _enforce_translation_quality_gate(db_session, "ep-gate", settings)


# ---------------------------------------------------------------------------
# Web restart jobs
# ---------------------------------------------------------------------------


def test_web_qa_rerun_stops_after_fresh_qa(db_session, tmp_path):
    from btcedu.web.jobs import Job, JobManager

    _, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    manager = JobManager(str(tmp_path / "logs"))
    job = Job(job_id="j1", episode_id="ep-gate", action="qa_rerun", force=True)

    with (
        patch("btcedu.core.qa_reviewer.call_claude") as mock_call,
        patch("btcedu.core.pipeline.run_episode_pipeline") as mock_pipeline,
    ):
        mock_call.return_value = _qa()
        manager._do_qa_rerun(job, db_session, settings)

    # Stops after QA — never invokes the full pipeline.
    mock_pipeline.assert_not_called()
    assert job.result is not None
    assert job.result["success"] is True
    assert job.result["decision"] in ("green", "yellow", "red")


def test_web_qa_rerun_all_proceeds_only_when_green(db_session, tmp_path):
    from btcedu.web.jobs import Job, JobManager

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    manager = JobManager(str(tmp_path / "logs"))
    job = Job(job_id="j2", episode_id="ep-gate", action="qa_rerun_all", force=True)

    report = SimpleNamespace(
        success=True,
        total_cost_usd=0.5,
        error=None,
        stages=[SimpleNamespace(stage="review_gate_2", status="success")],
    )

    with (
        patch(
            "btcedu.core.qa_reviewer.resolve_translation_quality_gate",
            return_value=SimpleNamespace(decision="green", generation=0, skipped=False),
        ) as mock_gate,
        patch("btcedu.core.pipeline.run_episode_pipeline", return_value=report) as mock_pipeline,
        patch("btcedu.core.pipeline.write_report"),
    ):
        manager._do_qa_rerun_all(job, db_session, settings)

    mock_pipeline.assert_called_once()
    assert mock_gate.call_args.kwargs["force"] is True
    assert job.result["proceeded"] is True


def test_web_qa_rerun_all_blocks_when_red(db_session, tmp_path):
    from btcedu.web.jobs import Job, JobManager

    episode, settings = _make_episode(db_session, tmp_path, [("s01", "Text.", "Metin.", True)])
    manager = JobManager(str(tmp_path / "logs"))
    job = Job(job_id="j3", episode_id="ep-gate", action="qa_rerun_all", force=True)

    report = SimpleNamespace(
        success=True,
        total_cost_usd=0.0,
        error=None,
        stages=[SimpleNamespace(stage="review_gate_2", status="review_pending")],
    )

    with (
        patch(
            "btcedu.core.qa_reviewer.resolve_translation_quality_gate",
            return_value=SimpleNamespace(decision="red", generation=0, skipped=False),
        ),
        patch("btcedu.core.pipeline.run_episode_pipeline", return_value=report),
        patch("btcedu.core.pipeline.write_report"),
    ):
        manager._do_qa_rerun_all(job, db_session, settings)

    assert job.result["proceeded"] is False
