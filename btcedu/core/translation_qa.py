"""Deterministic, zero-cost quality checks for story-based translations."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, PipelineRun, PipelineStage, RunStatus
from btcedu.models.qa_schema import QAFinding, QASummary, TranslationQADocument
from btcedu.prompts.glossary_loader import load_glossary

DETECTOR_VERSION = "deterministic/translation-qa-v1"

_MONTHS = {
    "januar": 1,
    "ocak": 1,
    "februar": 2,
    "şubat": 2,
    "märz": 3,
    "mart": 3,
    "april": 4,
    "nisan": 4,
    "mai": 5,
    "mayıs": 5,
    "juni": 6,
    "haziran": 6,
    "juli": 7,
    "temmuz": 7,
    "august": 8,
    "ağustos": 8,
    "september": 9,
    "eylül": 9,
    "oktober": 10,
    "ekim": 10,
    "november": 11,
    "kasım": 11,
    "dezember": 12,
    "aralık": 12,
}
_DATE_RE = re.compile(
    r"\b(\d{1,2})[.]?\s+(" + "|".join(map(re.escape, _MONTHS)) + r")"
    r"(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\b(?:saat\s*)?([01]?\d|2[0-3])(?:[:.]([0-5]\d)|\s*uhr)\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(
    r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(?:%|prozent\b|yüzde\b)",
    re.IGNORECASE,
)
_PERCENT_PREFIX_RE = re.compile(
    r"\byüzde\s*(\d+(?:[.,]\d+)?)(?!\w)",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"(?<!\w)(\d+(?:[.,]\d+)?)\s*"
    r"(millionen?|milliarden?|milyon|milyar)?\s*"
    r"(euro|eur|€|dollar|usd|\$|tl|lira|₺)(?!\w)",
    re.IGNORECASE,
)
_TEMP_RE = re.compile(
    r"(?<!\w)(-?\d+(?:[.,]\d+)?)\s*(?:°\s*c|grad|derece)\b",
    re.IGNORECASE,
)
_SCORE_RE = re.compile(r"(?<!\w)(\d{1,2})\s*(?::|-|zu)\s*(\d{1,2})(?!\w)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")
_QUANTITY_WORD_RE = re.compile(
    r"\b(tausend|tausende|hundert|hunderte|bin|binlerce|yüz|yüzlerce)\b",
    re.IGNORECASE,
)
_SPORT_CONTEXT = {
    "spiel",
    "tor",
    "sieg",
    "verlor",
    "gewann",
    "maç",
    "gol",
    "skor",
    "kazandı",
}
_UNAMBIGUOUS_SPORT_CONTEXT = {
    "fußball",
    "halbzeit",
    "elfmeter",
    "finale",
    "turnier",
    "mannschaft",
    "maç",
    "gol",
    "skor",
}
_CASUALTY_CONTEXT = {
    "tot",
    "tote",
    "getötet",
    "starb",
    "starben",
    "opfer",
    "öldü",
    "ölü",
    "hayatını kaybet",
    "can kaybı",
}
_INJURY_CONTEXT = {
    "verletzt",
    "verletzte",
    "yaralı",
    "yaralandı",
}
_WORD_VALUES = {
    "tausend": "1000",
    "bin": "1000",
    "hundert": "100",
    "yüz": "100",
    "tausende": "vague:thousands",
    "binlerce": "vague:thousands",
    "hunderte": "vague:hundreds",
    "yüzlerce": "vague:hundreds",
}
_NEGATION_GROUPS = {
    "negative": {
        "de": ("nicht", "kein", "keine", "nie", "weder"),
        "tr": ("değil", "yok", "hiçbir", "asla", "ne ", " ne"),
    },
    "limiter": {"de": ("nur",), "tr": ("sadece", "yalnızca")},
    "pending": {
        "de": ("noch nicht", "unbestätigt"),
        "tr": ("henüz değil", "doğrulanmadı", "teyit edilmedi"),
    },
    "excluded": {
        "de": ("ausgeschlossen",),
        "tr": ("dışlandı", "hariç tutuldu", "ihtimal dışı"),
    },
}
_TR_VERBAL_NEGATION_RE = re.compile(
    r"\b[\wçğıöşü]+(?:ma|me)"
    r"(?:dı|di|du|dü|tı|ti|tu|tü|mış|miş|muş|müş|yor|yacak|yecek|z)"
    r"(?:m|n|k|nız|niz|lar|ler)?\b",
    re.IGNORECASE,
)
_CHRONOLOGY_GROUPS = {
    "before": {"de": ("vorher", "zuvor", "bevor"), "tr": ("önce", "daha önce")},
    "after": {"de": ("nachher", "danach", "anschließend"), "tr": ("sonra", "ardından")},
    "since": {"de": ("seit",), "tr": ("beri", "-den bu yana")},
    "until": {"de": ("bis",), "tr": ("kadar", "dek")},
    "yesterday": {"de": ("gestern",), "tr": ("dün",)},
    "today": {"de": ("heute",), "tr": ("bugün",)},
    "tomorrow": {"de": ("morgen",), "tr": ("yarın",)},
}
_ENTITY_ALIASES = {
    "deutschland": ("almanya",),
    "türkei": ("türkiye",),
    "russland": ("rusya",),
    "china": ("çin",),
    "usa": ("abd",),
    "münchen": ("münih",),
    "brüssel": ("brüksel",),
    "moskau": ("moskova",),
    "eu": ("ab",),
    "vereinte nationen": ("birleşmiş milletler", "bm"),
}
_ENTITY_STOPWORDS = {
    "am montag",
    "am dienstag",
    "am mittwoch",
    "am donnerstag",
    "am freitag",
    "am samstag",
    "am sonntag",
}
_ENTITY_LEADING_STOPWORDS = {"am", "der", "die", "das", "im", "in", "nach", "vor"}
_ENTITY_GENERIC_TOKENS = {
    "euro",
    "million",
    "millionen",
    "milliarde",
    "milliarden",
    "prozent",
}
_OFFICE_TITLES = {
    "bundeskanzler",
    "bundeskanzlerin",
    "bundespräsident",
    "präsident",
    "minister",
    "kanzler",
}
_MULTIWORD_ENTITY_RE = re.compile(r"\b[A-ZÄÖÜ][\wÄÖÜäöüß'-]+(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß'-]+)+\b")
_TITLE_ENTITY_RE = re.compile(
    r"\b(?:Bundeskanzler(?:in)?|Bundespräsident|Präsident|Minister|Kanzler)"
    r"\s+([A-ZÄÖÜ][\wÄÖÜäöüß'-]+"
    r"(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß'-]+)*)\b"
)
_ACRONYM_RE = re.compile(r"\b[A-ZÄÖÜ]{2,8}\b")
_WEATHER_TERMS = {
    "schauer": ("sağanak",),
    "regen": ("yağmur",),
    "sonne": ("güneş",),
    "sonnenschein": ("güneş",),
    "schnee": ("kar",),
    "wind": ("rüzgar", "rüzgâr"),
}
_WEATHER_REGIONS = {
    "südwesten": ("güneybatı",),
    "nordosten": ("kuzeydoğu",),
    "norden": ("kuzey",),
    "süden": ("güney",),
    "osten": ("doğu",),
    "westen": ("batı",),
    "ostsee": ("baltık denizi",),
    "erzgebirge": ("erzgebirge",),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class NumericFact:
    kind: str
    value: str
    raw: str


@dataclass
class TranslationQAResult:
    episode_id: str
    qa_path: str
    provenance_path: str
    status: str = "green"
    finding_count: int = 0
    critical_count: int = 0
    skipped: bool = False
    reason: str = ""


def run_translation_qa(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> TranslationQAResult:
    """Run all deterministic translation checks and persist a zero-cost artifact."""
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    qa_path = Path(settings.outputs_dir) / episode_id / "translation_qa.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "translation_qa_provenance.json"
    )
    source_path = Path(settings.outputs_dir) / episode_id / "stories.json"
    translated_path = Path(settings.outputs_dir) / episode_id / "stories_translated.json"
    target_path = translated_path
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(episode.content_profile)
        enabled = profile.stage_config.get("translation_qa", {}).get("enabled", True)
    except Exception:
        enabled = True
    if not enabled:
        return TranslationQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="translation QA disabled by profile",
        )
    if not source_path.exists() or not target_path.exists():
        return TranslationQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            skipped=True,
            reason="story-based source or target artifact missing",
        )

    input_hash = hashlib.sha256(
        source_path.read_bytes()
        + b"\0"
        + target_path.read_bytes()
        + b"\0"
        + DETECTOR_VERSION.encode()
    ).hexdigest()
    if not force and _is_current(qa_path, provenance_path, input_hash):
        document = TranslationQADocument.model_validate_json(qa_path.read_text(encoding="utf-8"))
        return TranslationQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            status=document.status,
            finding_count=len(document.findings),
            critical_count=document.summary.critical_count,
            skipped=True,
            reason="already current",
        )

    run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSLATION_QA,
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    started = time.monotonic()
    try:
        source_data = json.loads(source_path.read_text(encoding="utf-8"))
        target_data = json.loads(target_path.read_text(encoding="utf-8"))
        glossary = load_glossary(episode.content_profile) or {}
        document = evaluate_translation_documents(
            episode_id,
            source_data,
            target_data,
            glossary,
        )
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        qa_path.write_text(
            json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "translation_qa",
                    "episode_id": episode_id,
                    "timestamp": _utcnow().isoformat(),
                    "detector": DETECTOR_VERSION,
                    "input_content_hash": input_hash,
                    "input_files": [str(source_path), str(target_path)],
                    "output_files": [str(qa_path)],
                    "finding_count": len(document.findings),
                    "critical_count": document.summary.critical_count,
                    "cost_usd": 0.0,
                    "duration_seconds": round(time.monotonic() - started, 3),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _upsert_content_artifact(
            session,
            episode_id=episode_id,
            file_path=qa_path,
            input_hash=input_hash,
        )
        run.status = RunStatus.SUCCESS
        run.completed_at = _utcnow()
        run.estimated_cost_usd = 0.0
        session.commit()
        return TranslationQAResult(
            episode_id=episode_id,
            qa_path=str(qa_path),
            provenance_path=str(provenance_path),
            status=document.status,
            finding_count=len(document.findings),
            critical_count=document.summary.critical_count,
        )
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.completed_at = _utcnow()
        run.error_message = str(exc)[:1000]
        session.commit()
        raise


def load_translation_qa(settings: Settings, episode_id: str) -> dict | None:
    """Load a validated deterministic translation QA artifact."""
    path = Path(settings.outputs_dir) / episode_id / "translation_qa.json"
    if not path.exists():
        return None
    try:
        document = TranslationQADocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document.model_dump(mode="json")


def evaluate_translation_documents(
    episode_id: str,
    source_data: dict,
    target_data: dict,
    glossary: dict | None = None,
) -> TranslationQADocument:
    """Evaluate two story documents without database or filesystem access."""
    source_stories = source_data.get("stories") or []
    target_stories = target_data.get("stories") or []
    findings: list[QAFinding] = []

    def add(
        category: str,
        severity: Literal["info", "minor", "major", "critical"],
        explanation: str,
        required_action: str,
        *,
        story: dict | None = None,
        source_excerpt: str = "",
        target_excerpt: str = "",
    ) -> None:
        findings.append(
            QAFinding(
                finding_id=f"qa-{len(findings) + 1:04d}",
                story_id=story.get("story_id") if story else None,
                category=category,
                severity=severity,
                source_excerpt=source_excerpt[:500],
                target_excerpt=target_excerpt[:500],
                explanation=explanation,
                required_action=required_action,
                source_segment_ids=(story or {}).get("source_segment_ids") or [],
            )
        )

    story_coverage = _check_story_coverage(source_stories, target_stories, add)
    source_by_id = {
        story.get("story_id"): story for story in source_stories if story.get("story_id")
    }
    target_by_id = {
        story.get("story_id"): story for story in target_stories if story.get("story_id")
    }
    matched_ids = [story_id for story_id in source_by_id if story_id in target_by_id]

    source_fact_count = 0
    matched_fact_count = 0
    protected_checks = 0
    protected_matches = 0
    glossary = glossary or {}
    protected_terms = glossary.get("protected_terms") or {}

    for story_id in matched_ids:
        source_story = source_by_id[story_id]
        target_story = target_by_id[story_id]
        source_text = _source_text(source_story)
        target_text = _target_text(target_story)

        source_facts = extract_numeric_facts(source_text)
        target_facts = extract_numeric_facts(target_text)
        source_fact_count += len(source_facts)
        matched_fact_count += _compare_numeric_facts(
            source_story,
            source_text,
            target_text,
            source_facts,
            target_facts,
            add,
        )
        checks, matches = _check_protected_terms(
            source_story,
            source_text,
            target_text,
            protected_terms,
            add,
        )
        protected_checks += checks
        protected_matches += matches
        _check_entities(source_story, source_text, target_text, protected_terms, add)
        _check_marker_groups(
            source_story,
            source_text,
            target_text,
            _NEGATION_GROUPS,
            "negation_suspicion",
            add,
        )
        _check_marker_groups(
            source_story,
            source_text,
            target_text,
            _CHRONOLOGY_GROUPS,
            "chronology_suspicion",
            add,
        )
        _check_weather_pairs(source_story, source_text, target_text, add)

    summary = QASummary(
        info_count=sum(f.severity == "info" for f in findings),
        minor_count=sum(f.severity == "minor" for f in findings),
        major_count=sum(f.severity == "major" for f in findings),
        critical_count=sum(f.severity == "critical" for f in findings),
        open_count=len(findings),
    )
    status: Literal["green", "yellow", "red"]
    if summary.critical_count:
        status = "red"
    elif summary.major_count or summary.minor_count:
        status = "yellow"
    else:
        status = "green"
    numeric_categories = {
        *(
            f"{kind}_mismatch"
            for kind in (
                "date",
                "time",
                "percent",
                "money",
                "temperature",
                "score",
                "casualty",
                "injury",
                "number",
            )
        ),
        *(
            f"unexpected_{kind}"
            for kind in (
                "date",
                "time",
                "percent",
                "money",
                "temperature",
                "score",
                "casualty",
                "injury",
                "number",
            )
        ),
    }
    numeric_finding_count = sum(finding.category in numeric_categories for finding in findings)
    checker_results = {
        "story_completeness": {
            "passed": not any(
                finding.category
                in {
                    "missing_story_ids",
                    "duplicate_story_ids",
                    "unknown_story_ids",
                    "order_mismatch",
                }
                for finding in findings
            ),
            "finding_count": sum(
                finding.category
                in {
                    "missing_story_ids",
                    "duplicate_story_ids",
                    "unknown_story_ids",
                    "order_mismatch",
                }
                for finding in findings
            ),
        },
        "numbers": {
            "passed": numeric_finding_count == 0,
            "finding_count": numeric_finding_count,
        },
        "protected_terms": {
            "passed": protected_checks == protected_matches,
            "finding_count": protected_checks - protected_matches,
        },
        "entities_negation_chronology": {
            "passed": not any(
                finding.category
                in {
                    "entity_suspicion",
                    "negation_suspicion",
                    "chronology_suspicion",
                    "weather_region_mismatch",
                }
                for finding in findings
            )
        },
    }
    return TranslationQADocument(
        episode_id=episode_id,
        generated_at=_utcnow(),
        status=status,
        findings=findings,
        summary=summary,
        checker_results=checker_results,
        story_coverage=story_coverage,
        number_coverage={
            "source_fact_count": source_fact_count,
            "matched_fact_count": matched_fact_count,
            "coverage": (
                round(matched_fact_count / source_fact_count, 4) if source_fact_count else 1.0
            ),
        },
        glossary_coverage={
            "required_term_count": protected_checks,
            "matched_term_count": protected_matches,
            "coverage": (
                round(protected_matches / protected_checks, 4) if protected_checks else 1.0
            ),
        },
        cost_usd=0.0,
    )


def _check_story_coverage(source_stories: list[dict], target_stories: list[dict], add) -> dict:
    source_ids = [story.get("story_id") for story in source_stories if story.get("story_id")]
    target_ids = [story.get("story_id") for story in target_stories if story.get("story_id")]
    source_set = set(source_ids)
    target_set = set(target_ids)
    duplicates = sorted(story_id for story_id, count in Counter(target_ids).items() if count > 1)
    missing = [story_id for story_id in source_ids if story_id not in target_set]
    unknown = [story_id for story_id in target_ids if story_id not in source_set]
    source_by_id = {story.get("story_id"): story for story in source_stories}

    if missing:
        lead_missing = any(source_by_id[story_id].get("is_lead_story") for story_id in missing)
        add(
            "missing_story_ids",
            "critical" if lead_missing else "major",
            f"Translated document is missing stories: {', '.join(missing)}.",
            "Restore every missing story in its original position.",
            source_excerpt=", ".join(source_ids),
            target_excerpt=", ".join(target_ids),
        )
    if duplicates:
        add(
            "duplicate_story_ids",
            "major",
            f"Translated document contains duplicate story IDs: {', '.join(duplicates)}.",
            "Keep exactly one translated entry per source story.",
            source_excerpt=", ".join(source_ids),
            target_excerpt=", ".join(target_ids),
        )
    if unknown:
        add(
            "unknown_story_ids",
            "major",
            f"Translated document contains unknown story IDs: {', '.join(unknown)}.",
            "Remove invented stories or restore the correct source story ID.",
            source_excerpt=", ".join(source_ids),
            target_excerpt=", ".join(target_ids),
        )
    common_target_order = [story_id for story_id in target_ids if story_id in source_set]
    expected_order = [story_id for story_id in source_ids if story_id in target_set]
    if common_target_order != expected_order:
        add(
            "order_mismatch",
            "major",
            "Translated stories are not in source order.",
            "Restore the source story order without merging stories.",
            source_excerpt=", ".join(expected_order),
            target_excerpt=", ".join(common_target_order),
        )
    return {
        "source_story_ids": source_ids,
        "translated_story_ids": target_ids,
        "missing_story_ids": missing,
        "duplicate_story_ids": duplicates,
        "unknown_story_ids": unknown,
        "order_matches": common_target_order == expected_order,
    }


def extract_numeric_facts(text: str) -> list[NumericFact]:
    """Extract normalized facts, preferring typed local-context values."""
    facts: list[NumericFact] = []
    occupied: list[tuple[int, int]] = []

    def collect(pattern: re.Pattern, kind: str, normalizer) -> None:
        for match in pattern.finditer(text):
            facts.append(NumericFact(kind, normalizer(match), match.group(0)))
            occupied.append(match.span())

    collect(
        _DATE_RE,
        "date",
        lambda match: (
            f"{int(match.group(1)):02d}-{_MONTHS[_normalize(match.group(2))]:02d}"
            + (f"-{match.group(3)}" if match.group(3) else "")
        ),
    )
    collect(
        _TIME_RE,
        "time",
        lambda match: f"{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}",
    )
    collect(_PERCENT_RE, "percent", lambda match: _decimal(match.group(1)))
    collect(_PERCENT_PREFIX_RE, "percent", lambda match: _decimal(match.group(1)))
    collect(
        _MONEY_RE,
        "money",
        lambda match: (
            f"{_scaled_decimal(match.group(1), match.group(2))}:{_currency(match.group(3))}"
        ),
    )
    collect(_TEMP_RE, "temperature", lambda match: _decimal(match.group(1)))

    is_sport = _has_sport_context(text)
    if is_sport:
        collect(
            _SCORE_RE,
            "score",
            lambda match: f"{int(match.group(1))}-{int(match.group(2))}",
        )

    for match in list(_NUMBER_RE.finditer(text)) + list(_QUANTITY_WORD_RE.finditer(text)):
        overlaps_typed_fact = any(
            start <= match.start() < end or start < match.end() <= end for start, end in occupied
        )
        if overlaps_typed_fact:
            continue
        window = _normalize(text[max(0, match.start() - 55) : match.end() + 55])
        raw = match.group(0)
        normalized_raw = _normalize(raw)
        value = _WORD_VALUES[normalized_raw] if normalized_raw in _WORD_VALUES else _decimal(raw)
        if any(term in window for term in _CASUALTY_CONTEXT):
            kind = "casualty"
        elif any(term in window for term in _INJURY_CONTEXT):
            kind = "injury"
        else:
            kind = "number"
        facts.append(NumericFact(kind, value, raw))
    return facts


def _compare_numeric_facts(
    story: dict,
    source_text: str,
    target_text: str,
    source_facts: list[NumericFact],
    target_facts: list[NumericFact],
    add,
) -> int:
    source_counter = Counter((fact.kind, fact.value) for fact in source_facts)
    target_counter = Counter((fact.kind, fact.value) for fact in target_facts)
    matched = sum((source_counter & target_counter).values())
    for (kind, value), count in (source_counter - target_counter).items():
        severity: Literal["major", "critical"] = (
            "critical" if kind in {"casualty", "injury"} else "major"
        )
        add(
            f"{kind}_mismatch",
            severity,
            f"Source {kind} value {value!r} is not preserved ({count} occurrence(s)).",
            "Correct the value in this story without changing its local meaning.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )
    for (kind, value), count in (target_counter - source_counter).items():
        severity = "critical" if kind in {"casualty", "injury"} else "major"
        add(
            f"unexpected_{kind}",
            severity,
            f"Target adds or changes {kind} value {value!r} ({count} occurrence(s)).",
            "Remove invented precision or restore the source value.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )
    return matched


def _check_protected_terms(
    story: dict,
    source_text: str,
    target_text: str,
    protected_terms: dict,
    add,
) -> tuple[int, int]:
    checks = 0
    matches = 0
    normalized_source = _normalize(source_text)
    for source_term, config in protected_terms.items():
        if _normalize(source_term) not in normalized_source:
            continue
        checks += 1
        variants = (
            config.get("allowed_targets", [])
            if isinstance(config, dict)
            else ([config] if isinstance(config, str) else [])
        )
        if any(_contains_turkish_variant(target_text, variant) for variant in variants):
            matches += 1
            continue
        add(
            "protected_term_violation",
            "major",
            f"Protected term {source_term!r} has no allowed target variant.",
            f"Use one of: {', '.join(variants)}.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )
    return checks, matches


def _check_entities(
    story: dict,
    source_text: str,
    target_text: str,
    protected_terms: dict,
    add,
) -> None:
    protected_sources = {_normalize(term) for term in protected_terms}
    candidates = {
        match.group(0)
        for match in _MULTIWORD_ENTITY_RE.finditer(source_text)
        if _normalize(match.group(0)) not in _ENTITY_STOPWORDS
        and _normalize(match.group(0)).split()[0] not in _ENTITY_LEADING_STOPWORDS
        and _normalize(match.group(0)).split()[0] not in _OFFICE_TITLES
        and not (set(_normalize(match.group(0)).split()) & _ENTITY_GENERIC_TOKENS)
    }
    candidates.update(match.group(1) for match in _TITLE_ENTITY_RE.finditer(source_text))
    candidates.update(_ACRONYM_RE.findall(source_text))
    if story.get("location"):
        candidates.add(story["location"])
    normalized_source = _normalize(source_text)
    candidates.update(
        entity for entity in _ENTITY_ALIASES if _contains_phrase(normalized_source, entity)
    )
    for entity in sorted(candidates):
        normalized = _normalize(entity)
        if any(
            normalized == protected or normalized in protected.split("-")
            for protected in protected_sources
        ):
            continue
        variants = (entity, *_ENTITY_ALIASES.get(normalized, ()))
        if any(_contains_turkish_variant(target_text, variant) for variant in variants):
            continue
        add(
            "entity_suspicion",
            "minor",
            f"Entity {entity!r} from the source is not clearly present in the target.",
            "Verify the person, place, country, organisation, or office manually.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )


def _check_marker_groups(
    story: dict,
    source_text: str,
    target_text: str,
    groups: dict,
    category: str,
    add,
) -> None:
    source_normalized = _normalize(source_text)
    target_normalized = _normalize(target_text)
    for group, language_terms in groups.items():
        source_present = any(
            _contains_phrase(source_normalized, term) for term in language_terms["de"]
        )
        if not source_present:
            continue
        target_present = any(
            _contains_turkish_variant(target_normalized, term) for term in language_terms["tr"]
        )
        if group == "negative" and _TR_VERBAL_NEGATION_RE.search(target_normalized):
            target_present = True
        if target_present:
            continue
        add(
            category,
            "major" if category == "chronology_suspicion" else "minor",
            f"Source marker group {group!r} has no clear Turkish counterpart.",
            "Review the sentence for a possible meaning or timeline change.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )


def _check_weather_pairs(story: dict, source_text: str, target_text: str, add) -> None:
    source_normalized = _normalize(source_text)
    target_clauses = [_normalize(clause) for clause in re.split(r"[,.!?;]", target_text)]
    pairs: list[tuple[str, str]] = []
    for source_clause in re.split(r"[,.!?;]", source_normalized):
        regions = [region for region in _WEATHER_REGIONS if region in source_clause]
        conditions = [condition for condition in _WEATHER_TERMS if condition in source_clause]
        pairs.extend((region, condition) for region in regions for condition in conditions)
    for region, condition in pairs:
        region_variants = _WEATHER_REGIONS[region]
        condition_variants = _WEATHER_TERMS[condition]
        if any(
            any(variant in clause for variant in region_variants)
            and any(variant in clause for variant in condition_variants)
            for clause in target_clauses
        ):
            continue
        add(
            "weather_region_mismatch",
            "major",
            f"Weather assignment {region!r} → {condition!r} is not preserved locally.",
            "Keep each weather condition attached to its original region.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )


def _source_text(story: dict) -> str:
    return str(story.get("source_text") or story.get("text_de") or "")


def _target_text(story: dict) -> str:
    return str(story.get("text_adapted_tr") or story.get("text_tr") or "")


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text)).casefold().replace("\u0307", "")
    return re.sub(r"\s+", " ", normalized).strip()


def _contains_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = _normalize(phrase)
    return bool(re.search(rf"(?<!\w){re.escape(normalized_phrase)}(?!\w)", normalized_text))


def _contains_turkish_variant(text: str, variant: str) -> bool:
    normalized_text = _normalize(text)
    normalized_variant = _normalize(variant)
    return bool(
        re.search(
            rf"(?<!\w){re.escape(normalized_variant)}(?:['’]?[a-zçğıöşü]+)?(?!\w)",
            normalized_text,
        )
    )


def _has_sport_context(text: str) -> bool:
    normalized = _normalize(text)
    tokens = set(normalized.split())
    if tokens & _UNAMBIGUOUS_SPORT_CONTEXT:
        return True
    if len(tokens & _SPORT_CONTEXT) >= 2:
        return True
    return bool(
        re.search(
            r"\b(?:das|dieses)\s+spiel\b.{0,80}\b(?:endete|gewann|verlor)\b",
            normalized,
        )
    )


def _decimal(value: str) -> str:
    return format(float(value.replace(",", ".")), ".6f").rstrip("0").rstrip(".")


def _scaled_decimal(value: str, magnitude: str | None) -> str:
    multiplier = {
        "million": 1_000_000,
        "millionen": 1_000_000,
        "milyon": 1_000_000,
        "milliarde": 1_000_000_000,
        "milliarden": 1_000_000_000,
        "milyar": 1_000_000_000,
    }.get(_normalize(magnitude or ""), 1)
    return _decimal(str(float(value.replace(",", ".")) * multiplier))


def _currency(value: str) -> str:
    normalized = _normalize(value)
    if normalized in {"euro", "eur", "€"}:
        return "EUR"
    if normalized in {"dollar", "usd", "$"}:
        return "USD"
    return "TRY"


def _is_current(qa_path: Path, provenance_path: Path, input_hash: str) -> bool:
    if not qa_path.exists() or not provenance_path.exists():
        return False
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if provenance.get("input_content_hash") != input_hash:
        return False
    try:
        TranslationQADocument.model_validate_json(qa_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def _upsert_content_artifact(
    session: Session,
    *,
    episode_id: str,
    file_path: Path,
    input_hash: str,
) -> None:
    artifacts = (
        session.query(ContentArtifact)
        .filter(
            ContentArtifact.episode_id == episode_id,
            ContentArtifact.artifact_type == "translation_qa",
        )
        .order_by(ContentArtifact.id)
        .all()
    )
    if artifacts:
        artifact = artifacts[0]
        artifact.file_path = str(file_path)
        artifact.model = DETECTOR_VERSION
        artifact.prompt_hash = input_hash
        artifact.retrieval_snapshot_path = None
        for duplicate in artifacts[1:]:
            session.delete(duplicate)
        return
    session.add(
        ContentArtifact(
            episode_id=episode_id,
            artifact_type="translation_qa",
            file_path=str(file_path),
            model=DETECTOR_VERSION,
            prompt_hash=input_hash,
            retrieval_snapshot_path=None,
        )
    )
