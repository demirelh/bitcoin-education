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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, PipelineRun, PipelineStage, RunStatus
from btcedu.models.qa_schema import QAFinding, QASummary, TranslationQADocument
from btcedu.prompts.glossary_loader import load_glossary

DETECTOR_VERSION = "deterministic/translation-qa-v4"

_HARM_KINDS = {"casualty_count", "injury_count"}
_EXACT_STRUCTURAL_KINDS = {
    "casualty_count",
    "injury_count",
    "age",
    "date",
    "time",
    "year",
    "score",
    "money",
    "percentage",
}
_KIND_CATEGORY = {
    "casualty_count": "casualty",
    "injury_count": "injury",
    "generic_count": "number",
}

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
_TURKISH_HOUR_RE = re.compile(
    r"\bsaat\s*([01]?\d|2[0-3])(?:['’](?:den|dan|de|da|ye|ya|e|a))?(?![\w.:])",
    re.IGNORECASE,
)
_NUMERIC_TOKEN = r"(?:\d{1,3}(?:[.\u00a0\u202f ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)"
_PERCENT_RE = re.compile(
    rf"(?<!\w)({_NUMERIC_TOKEN})\s*(?:%|prozent\b|yüzde\b)",
    re.IGNORECASE,
)
_PERCENT_PREFIX_RE = re.compile(
    rf"\byüzde\s*({_NUMERIC_TOKEN})(?!\w)",
    re.IGNORECASE,
)
_PERCENT_RANGE_RE = re.compile(
    rf"(?:(?:yüzde|%)\s*)?({_NUMERIC_TOKEN})\s*(?:[-–]|ila|ile)\s*"
    rf"({_NUMERIC_TOKEN})\s*%"
    rf"|(?:yüzde|%)\s*({_NUMERIC_TOKEN})\s*(?:[-–]|ila|ile)\s*"
    rf"({_NUMERIC_TOKEN})(?!\w)",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    rf"(?<!\w)({_NUMERIC_TOKEN})\s*"
    r"(hundert|tausend|million(?:en)?|milliarden?|mio\.?|mrd\.?|"
    r"yüz|bin(?:i|e|den|in)?|milyon|milyar)?\s*"
    r"(euro|avro(?:dan|den|ya|ye|nun|nün)?|eur|€|dollar|usd|\$|tl|lira|₺)(?!\w)",
    re.IGNORECASE,
)
_TURKISH_COMPOUND_THOUSANDS_MONEY_RE = re.compile(
    rf"(?<!\w)({_NUMERIC_TOKEN})\s+bin\s+({_NUMERIC_TOKEN})\s*"
    r"(euro|avro(?:dan|den|ya|ye|nun|nün)?|eur|€|dollar|usd|\$|tl|lira|₺)(?!\w)",
    re.IGNORECASE,
)
_SCALED_NUMBER_RE = re.compile(
    rf"(?<!\w)({_NUMERIC_TOKEN})\s*"
    r"(hundert|tausend|million(?:en)?|milliarden?|mio\.?|mrd\.?|"
    r"yüz|bin(?:i|e|den|in)?|milyon|milyar)(?!\w)",
    re.IGNORECASE,
)
_TEMP_RE = re.compile(
    rf"(?<!\w)(-?{_NUMERIC_TOKEN})\s*"
    rf"(?:°(?:\s*c)?|grad|derece(?:ye|yi|de|den|nin)?)(?!\w)",
    re.IGNORECASE,
)
_TEMP_RANGE_RE = re.compile(
    rf"(?<!\w)(-?{_NUMERIC_TOKEN})\s*(?:bis|ila|ile|-)\s*(-?{_NUMERIC_TOKEN})\s*"
    rf"(?:°(?:\s*c)?|grad|derece(?:ye|yi|de|den|nin)?)(?!\w)",
    re.IGNORECASE,
)
_SCORE_RE = re.compile(r"(?<!\w)(\d{1,2})\s*(?::|-|zu)\s*(\d{1,2})(?!\w)", re.IGNORECASE)
_COMPOUND_SCORE_RE = re.compile(
    r"(?<!\w)(\d{1,2})\s+und\s+(\d{1,2})\s+zu\s+(\d{1,2})(?!\w)",
    re.IGNORECASE,
)
_TURKISH_COMPOUND_NUMBER_RE = re.compile(
    r"\b(on|yirmi|otuz|kırk|elli|altmış|yetmiş|seksen|doksan)\s+"
    r"(iki|üç|dört|beş|altı|yedi|sekiz|dokuz)\b",
    re.IGNORECASE,
)
_TURKISH_COMPOUND_THOUSANDS_RE = re.compile(
    rf"(?<!\w)({_NUMERIC_TOKEN})\s+bin\s+({_NUMERIC_TOKEN})(?!\w)",
    re.IGNORECASE,
)
_TURKISH_TENS = {
    "on": 10,
    "yirmi": 20,
    "otuz": 30,
    "kırk": 40,
    "elli": 50,
    "altmış": 60,
    "yetmiş": 70,
    "seksen": 80,
    "doksan": 90,
}
_NUMBER_RE = re.compile(rf"(?<!\w){_NUMERIC_TOKEN}(?!\w)")
_QUANTITY_WORD_RE = re.compile(
    (
        r"\b(tausend|tausende|hundert|hunderte|bin|binlerce|yüz|yüzlerce|"
        r"zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf|"
        r"iki|ikisi\w*|üç|dört|dördü|beş|altı|yedi|sekiz|dokuz|on|"
        r"beid\w*|zweit\w*|dritt\w*|ikinci\w*|üçüncü\w*)\b"
    ),
    re.IGNORECASE,
)
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_AGE_CONTEXT_RE = re.compile(
    r"(?:im\s+alter\s+von\s+|)(?:\d+|[\wäöüßçğıöşü]+)\s+"
    r"(?:jahre(?:n)?\s+alt|jahren\b)"
    r"|(?:\d+|[\wäöüßçğıöşü]+)\s+yaş(?:ında|indaydı|ındaydı|ında)?\b",
    re.IGNORECASE,
)
_DURATION_UNITS_RE = re.compile(
    r"\b(?:sekunden?|minuten?|stunden?|tage?|wochen?|monate?|jahre?n?|"
    r"saniye|dakika|saat|gün|hafta|ay|yıl)(?:dır|dir|dur|dür|dan|den)?\b",
    re.IGNORECASE,
)
_CASUALTY_ROLE_RE = re.compile(
    r"\b(?:tote[nr]?|getötet(?:e[nr]?)?|tötet\w*|starben|ums\s+leben|opfer|"
    r"kişi\s+hayatını\s+kaybet\w*|hayatını\s+kaybeden|kişi\s+öldü|"
    r"ölü(?:m)?|can\s+kaybı)\b",
    re.IGNORECASE,
)
_INJURY_ROLE_RE = re.compile(
    r"\b(?:verletzte[nr]?|verletzt(?:e[nr]?)?|yaralı|yaralandı|yaralanan)\b",
    re.IGNORECASE,
)
_PERSON_COUNT_RE = re.compile(
    r"\b(?:menschen?|personen?|tote[nr]?|verletzte[nr]?|kişi|kişinin|"
    r"insan|insanın|yaralı)\b",
    re.IGNORECASE,
)
_COUNT_NOUN_RE = re.compile(
    r"\b(?:menschen?|personen?|drohnen?|raketen?|fahrzeuge?|häuser?|plätze?|"
    r"kişi|insan|insansız\s+hava\s+aracı|füze|araç|ev)\b",
    re.IGNORECASE,
)
_QUALIFIER_PATTERNS = (
    ("at_least", re.compile(r"\b(?:mindestens|wenigstens|en\s+az)\s*$", re.IGNORECASE)),
    (
        "more_than",
        re.compile(r"\b(?:mehr\s+als|über|fazla|aşkın)\s*$", re.IGNORECASE),
    ),
    (
        "approximate",
        re.compile(r"\b(?:rund|etwa|ungefähr|circa|ca\.?|yaklaşık)\s*$", re.IGNORECASE),
    ),
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
    "ikinci": "2",
    "ikincisi": "2",
    "zwei": "2",
    "iki": "2",
    "drei": "3",
    "üç": "3",
    "vier": "4",
    "dört": "4",
    "dördü": "4",
    "fünf": "5",
    "beş": "5",
    "sechs": "6",
    "altı": "6",
    "sieben": "7",
    "yedi": "7",
    "acht": "8",
    "sekiz": "8",
    "neun": "9",
    "dokuz": "9",
    "zehn": "10",
    "on": "10",
    "elf": "11",
    "zwölf": "12",
    "üçüncü": "3",
    "üçüncüsü": "3",
}
# Spelled-out cardinal numbers (e.g. German "Zwölf Millionen Euro" or Turkish
# "on milyon Euro") combine with a magnitude word exactly like their digit
# counterparts above. Without a dedicated pattern, extract_numeric_facts()
# only recognizes the bare cardinal ("12") and drops the magnitude/currency,
# producing a false "protected fact changed" mismatch whenever the source
# spells the number out but the (correct) translation uses digits — see
# regression tests for story s03 of episode GG18EjVz8x8 ("Zwölf Millionen
# Euro" vs. translated "12 milyon Euro").
_WORD_CARDINAL_VALUES = {
    word: value
    for word, value in _WORD_VALUES.items()
    if value.isdigit() and int(value) <= 12 and not word.startswith(("ikinci", "üçüncü"))
}
_WORD_CARDINAL_FRAGMENT = "|".join(
    re.escape(word) for word in sorted(_WORD_CARDINAL_VALUES, key=len, reverse=True)
)
_WORD_MONEY_RE = re.compile(
    rf"(?<!\w)({_WORD_CARDINAL_FRAGMENT})\s+"
    r"(hundert|tausend|million(?:en)?|milliarden?|mio\.?|mrd\.?|"
    r"yüz|bin(?:i|e|den|in)?|milyon|milyar)\s+"
    r"(euro|eur|€|dollar|usd|\$|tl|lira|₺)(?!\w)",
    re.IGNORECASE,
)
_WORD_SCALED_NUMBER_RE = re.compile(
    rf"(?<!\w)({_WORD_CARDINAL_FRAGMENT})\s+"
    r"(hundert|tausend|million(?:en)?|milliarden?|mio\.?|mrd\.?|"
    r"yüz|bin(?:i|e|den|in)?|milyon|milyar)(?!\w)",
    re.IGNORECASE,
)
_NEGATION_GROUPS = {
    "negative": {
        "de": ("nicht", "kein", "keine", "nie", "weder"),
        "tr": ("değil", "yok", "hiçbir", "asla"),
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
    r"\b[\wçğıöşü]+(?:ma|me|mı|mi|mu|mü)"
    r"(?:dı|di|du|dü|tı|ti|tu|tü|mış|miş|muş|müş|yor|yacak|yecek|z)"
    r"(?:m|n|k|nız|niz|lar|ler)?\b",
    re.IGNORECASE,
)
_TR_NEITHER_RE = re.compile(r"\bne\b(?:(?![.!?]).){1,80}\bne\s+de\b", re.IGNORECASE)
_CHRONOLOGY_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    "before": {
        "de": (r"\b(?:vorher|zuvor|bevor)\b",),
        "tr": (r"\b(?:daha\s+önce|önceki|önce)\b",),
    },
    "after": {
        "de": (
            r"\b(?:nachher|anschließend)\b",
            r"\bdanach\b(?!\s+gefragt)",
            r"\bnach\s+(?:ihrem\s+)?sieg\b",
            r"\bnach\s+einem\s+offiziellen\s+empfang\b",
            r"\bnach\s+(?:dem\s+)?spiel\b",
        ),
        "tr": (r"\b(?:ardından|sonrasında|sonra)\b",),
    },
    "since": {
        "de": (r"\bseit\b",),
        "tr": (
            r"\b(?:beri|bu\s+yana|süredir)\b",
            r"\b\w+(?:ler|lar)?(?:dır|dir|dur|dür|tır|tir|tur|tür)\b",
        ),
    },
    "until": {"de": (r"\bbis\b",), "tr": (r"\b(?:kadar|dek)\b",)},
    "yesterday": {"de": (r"\bgestern\b",), "tr": (r"\bdün\b",)},
    "today": {
        "de": (r"\bheute\b",),
        "tr": (
            r"\bbugün\b",
            r"\bbugüne\s+kadar\b",
            r"\bbu\s+(?:akşam|gece)\b",
            r"\bgece\s+saatlerinde\b",
        ),
    },
    "tomorrow": {"de": (r"\bmorgen\b",), "tr": (r"\byarın\b",)},
}
_CHRONOLOGY_GROUPS = {group: {"de": (), "tr": ()} for group in _CHRONOLOGY_PATTERNS}
_ENTITY_ALIASES = {
    "deutschland": ("almanya",),
    "türkei": ("türkiye",),
    "russland": ("rusya",),
    "ukraine": ("ukrayna",),
    "china": ("çin",),
    "usa": ("abd",),
    "us": ("abd",),
    "nrw": ("kuzey ren-vestfalya", "kuzey ren vestfalya"),
    "zelensky": ("zelenskiy",),
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
    "regen": ("yağmur", "yağış"),
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
    qualifier: str = "exact"
    claim_index: int = 0
    start: int = 0
    end: int = 0
    confidence: Literal["high", "medium", "low"] = "high"


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
        source_unreliable_segment_ids = _load_source_unreliable_segments(settings, episode_id)
        document = evaluate_translation_documents(
            episode_id,
            source_data,
            target_data,
            glossary,
            source_unreliable_segment_ids,
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


def _load_source_unreliable_segments(settings: Settings, episode_id: str) -> set[str]:
    """Segments whose transcript evidence is unresolved or critical."""
    try:
        from btcedu.core.transcript_qa import load_transcript_qa

        document = load_transcript_qa(settings, episode_id)
    except Exception:  # noqa: BLE001
        return set()
    if not document:
        return set()
    segments: set[str] = set()
    for finding in document.get("findings") or []:
        if finding.get("blocking") or finding.get("severity") in {"critical", "major"}:
            segments.update(finding.get("segment_ids") or [])
    return segments


def evaluate_translation_documents(
    episode_id: str,
    source_data: dict,
    target_data: dict,
    glossary: dict | None = None,
    source_unreliable_segment_ids: set[str] | None = None,
) -> TranslationQADocument:
    """Evaluate two story documents without database or filesystem access."""
    source_stories = source_data.get("stories") or []
    target_stories = target_data.get("stories") or []
    findings: list[QAFinding] = []
    unreliable_segments = source_unreliable_segment_ids or set()

    def add(
        category: str,
        severity: Literal["info", "minor", "major", "critical"],
        explanation: str,
        required_action: str,
        *,
        story: dict | None = None,
        source_excerpt: str = "",
        target_excerpt: str = "",
        structural_invariant: bool = False,
        review_required: bool = False,
    ) -> None:
        story_segments = set((story or {}).get("source_segment_ids") or [])
        affected_unreliable = sorted(story_segments & unreliable_segments)
        source_unreliable = bool(affected_unreliable)
        if source_unreliable and severity in {"critical", "major"}:
            severity = "minor"
            structural_invariant = False
            review_required = True
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
                source_segment_ids=(
                    affected_unreliable
                    if source_unreliable
                    else (story or {}).get("source_segment_ids") or []
                ),
                structural_invariant=structural_invariant,
                source_unreliable=source_unreliable,
                review_required=review_required,
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
        if source_story.get("story_type") in {"intro", "outro"} and not target_text.strip():
            continue

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
        if source_story.get("story_type") not in {"intro", "outro"}:
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
                "percentage",
                "money",
                "temperature",
                "score",
                "casualty",
                "injury",
                "age",
                "year",
                "duration",
                "number",
            )
        ),
        *(
            f"unexpected_{kind}"
            for kind in (
                "date",
                "time",
                "percentage",
                "money",
                "temperature",
                "score",
                "casualty",
                "injury",
                "age",
                "year",
                "duration",
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
            structural_invariant=True,
        )
    if duplicates:
        add(
            "duplicate_story_ids",
            "major",
            f"Translated document contains duplicate story IDs: {', '.join(duplicates)}.",
            "Keep exactly one translated entry per source story.",
            source_excerpt=", ".join(source_ids),
            target_excerpt=", ".join(target_ids),
            structural_invariant=True,
        )
    if unknown:
        add(
            "unknown_story_ids",
            "major",
            f"Translated document contains unknown story IDs: {', '.join(unknown)}.",
            "Remove invented stories or restore the correct source story ID.",
            source_excerpt=", ".join(source_ids),
            target_excerpt=", ".join(target_ids),
            structural_invariant=True,
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
            structural_invariant=True,
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
    """Extract canonical, claim-local numeric facts from German or Turkish."""
    facts: list[NumericFact] = []
    occupied: list[tuple[int, int]] = []

    def append_fact(match: re.Match, kind: str, value: str) -> None:
        facts.append(
            NumericFact(
                kind=kind,
                value=value,
                raw=match.group(0),
                qualifier=_numeric_qualifier(text, match.start(), match.end()),
                claim_index=_claim_index(text, match.start()),
                start=match.start(),
                end=match.end(),
                confidence="high",
            )
        )
        occupied.append(match.span())

    def collect(pattern: re.Pattern, kind: str, normalizer) -> None:
        for match in pattern.finditer(text):
            if _overlaps(match.span(), occupied):
                continue
            append_fact(match, kind, normalizer(match))

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
    collect(
        _TURKISH_HOUR_RE,
        "time",
        lambda match: f"{int(match.group(1)):02d}:00",
    )
    for match in _PERCENT_RANGE_RE.finditer(text):
        left = match.group(1) or match.group(3)
        right = match.group(2) or match.group(4)
        facts.extend(
            [
                NumericFact(
                    "percentage",
                    _decimal(left),
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
                NumericFact(
                    "percentage",
                    _decimal(right),
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
            ]
        )
        occupied.append(match.span())
    collect(_PERCENT_RE, "percentage", lambda match: _decimal(match.group(1)))
    collect(_PERCENT_PREFIX_RE, "percentage", lambda match: _decimal(match.group(1)))
    for match in _TURKISH_COMPOUND_THOUSANDS_MONEY_RE.finditer(text):
        value = str(
            int(Decimal(_decimal(match.group(1))) * 1_000) + int(Decimal(_decimal(match.group(2))))
        )
        append_fact(match, "money", f"{value}:{_currency(match.group(3))}")
    collect(
        _MONEY_RE,
        "money",
        lambda match: (
            f"{_scaled_decimal(match.group(1), match.group(2))}:{_currency(match.group(3))}"
        ),
    )
    collect(
        _WORD_MONEY_RE,
        "money",
        lambda match: (
            f"{_scaled_decimal(_WORD_CARDINAL_VALUES[_normalize(match.group(1))], match.group(2))}"
            f":{_currency(match.group(3))}"
        ),
    )
    for match in _TEMP_RANGE_RE.finditer(text):
        facts.extend(
            [
                NumericFact(
                    "temperature",
                    _decimal(match.group(1)),
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
                NumericFact(
                    "temperature",
                    _decimal(match.group(2)),
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
            ]
        )
        occupied.append(match.span())
    collect(_TEMP_RE, "temperature", lambda match: _decimal(match.group(1)))
    for match in _TURKISH_COMPOUND_THOUSANDS_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        value = str(
            int(Decimal(_decimal(match.group(1))) * 1_000) + int(Decimal(_decimal(match.group(2))))
        )
        append_fact(match, _classify_numeric_role(text, match.start(), match.end(), value), value)
    for match in _SCALED_NUMBER_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        value = _scaled_decimal(match.group(1), match.group(2))
        append_fact(match, _classify_numeric_role(text, match.start(), match.end(), value), value)
    for match in _WORD_SCALED_NUMBER_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        value = _scaled_decimal(_WORD_CARDINAL_VALUES[_normalize(match.group(1))], match.group(2))
        append_fact(match, _classify_numeric_role(text, match.start(), match.end(), value), value)

    for match in _COMPOUND_SCORE_RE.finditer(text):
        facts.extend(
            [
                NumericFact(
                    "score",
                    f"{int(match.group(1))}-{int(match.group(3))}",
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
                NumericFact(
                    "score",
                    f"{int(match.group(2))}-{int(match.group(3))}",
                    match.group(0),
                    claim_index=_claim_index(text, match.start()),
                    start=match.start(),
                    end=match.end(),
                ),
            ]
        )
        occupied.append(match.span())

    is_sport = _has_sport_context(text)
    if is_sport:
        for match in _SCORE_RE.finditer(text):
            if _overlaps(match.span(), occupied):
                continue
            append_fact(match, "score", f"{int(match.group(1))}-{int(match.group(2))}")

    for match in _TURKISH_COMPOUND_NUMBER_RE.finditer(text):
        value = _TURKISH_TENS[_normalize(match.group(1))] + int(
            _WORD_VALUES[_normalize(match.group(2))]
        )
        append_fact(
            match,
            _classify_numeric_role(text, match.start(), match.end(), str(value)),
            str(value),
        )

    for match in list(_NUMBER_RE.finditer(text)) + list(_QUANTITY_WORD_RE.finditer(text)):
        if _overlaps(match.span(), occupied):
            continue
        raw = match.group(0)
        normalized_raw = _normalize(raw)
        if normalized_raw.startswith(("beid", "zweit", "ikisi")):
            value = "2"
        elif normalized_raw.startswith("dritt") or normalized_raw.startswith("üçüncü"):
            value = "3"
        elif normalized_raw.startswith("ikinci"):
            value = "2"
        else:
            value = (
                _WORD_VALUES[normalized_raw] if normalized_raw in _WORD_VALUES else _decimal(raw)
            )
        append_fact(match, _classify_numeric_role(text, match.start(), match.end(), value), value)
    return sorted(facts, key=lambda fact: (fact.start, fact.end, fact.kind))


def _compare_numeric_facts(
    story: dict,
    source_text: str,
    target_text: str,
    source_facts: list[NumericFact],
    target_facts: list[NumericFact],
    add,
) -> int:
    source_remaining = _dedupe_repeated_claim_facts(source_text, source_facts)
    target_remaining = _dedupe_repeated_claim_facts(target_text, target_facts)
    matched = 0

    # First consume exact role/value/quantifier matches. Nearest claim wins so
    # repeated values remain attached to their local statements.
    for source_fact in list(source_remaining):
        candidate = _nearest_numeric_fact(
            source_fact,
            target_remaining,
            lambda target: (
                target.kind == source_fact.kind
                and target.value == source_fact.value
                and target.qualifier == source_fact.qualifier
            ),
        )
        if candidate is None:
            continue
        source_remaining.remove(source_fact)
        target_remaining.remove(candidate)
        matched += 1

    # Pair remaining facts locally by semantic role. A changed value produces
    # one mismatch finding (never a duplicate missing+unexpected pair).
    for source_fact in list(source_remaining):
        target_fact = _nearest_numeric_fact(
            source_fact,
            target_remaining,
            lambda target: target.kind == source_fact.kind,
            max_claim_distance=1,
        )
        if target_fact is None:
            continue
        source_remaining.remove(source_fact)
        target_remaining.remove(target_fact)
        category = _kind_category(source_fact.kind)
        source_claim = _claim_text(source_text, source_fact.claim_index)
        target_claim = _claim_text(target_text, target_fact.claim_index)
        if source_fact.value != target_fact.value:
            severity = _numeric_mismatch_severity(source_fact)
            add(
                f"{category}_mismatch",
                severity,
                f"Local {source_fact.kind} changed from {source_fact.value!r} "
                f"to {target_fact.value!r}.",
                "Restore the source value in this local claim.",
                story=story,
                source_excerpt=source_claim,
                target_excerpt=target_claim,
                structural_invariant=(
                    source_fact.kind in _EXACT_STRUCTURAL_KINDS and source_fact.confidence == "high"
                ),
            )
        elif source_fact.qualifier != target_fact.qualifier:
            add(
                f"{category}_quantifier_mismatch",
                "major" if source_fact.kind in _HARM_KINDS else "minor",
                f"Value {source_fact.value!r} changed quantifier from "
                f"{source_fact.qualifier!r} to {target_fact.qualifier!r}.",
                "Preserve qualifiers such as at least, more than, or approximately.",
                story=story,
                source_excerpt=source_claim,
                target_excerpt=target_claim,
                review_required=True,
            )
        else:
            matched += 1

    # Same local value but different role is classification uncertainty, not a
    # factual critical. This is intentionally at most MINOR.
    for source_fact in list(source_remaining):
        target_fact = _nearest_numeric_fact(
            source_fact,
            target_remaining,
            lambda target: target.value == source_fact.value,
            max_claim_distance=1,
        )
        if target_fact is None:
            continue
        if source_fact.kind in _HARM_KINDS and target_fact.kind in _HARM_KINDS:
            source_remaining.remove(source_fact)
            target_remaining.remove(target_fact)
            add(
                f"{_kind_category(source_fact.kind)}_mismatch",
                "critical",
                f"Local harm role changed from {source_fact.kind} to {target_fact.kind} "
                f"for value {source_fact.value!r}.",
                "Preserve whether people were killed or injured.",
                story=story,
                source_excerpt=_claim_text(source_text, source_fact.claim_index),
                target_excerpt=_claim_text(target_text, target_fact.claim_index),
                structural_invariant=True,
            )
            continue
        source_remaining.remove(source_fact)
        target_remaining.remove(target_fact)
        matched += 1
        add(
            "numeric_role_review",
            "minor",
            f"Value {source_fact.value!r} is preserved locally but its role is "
            f"uncertain ({source_fact.kind} vs {target_fact.kind}).",
            "Review the numeric role; do not automatically rewrite the translation.",
            story=story,
            source_excerpt=_claim_text(source_text, source_fact.claim_index),
            target_excerpt=_claim_text(target_text, target_fact.claim_index),
            review_required=True,
        )

    for source_fact in source_remaining:
        category = _kind_category(source_fact.kind)
        add(
            f"{category}_mismatch",
            _numeric_mismatch_severity(source_fact),
            f"Source {source_fact.kind} value {source_fact.value!r} "
            f"({source_fact.qualifier}) is not preserved in its local claim.",
            "Restore the source value and qualifier in this local claim.",
            story=story,
            source_excerpt=_claim_text(source_text, source_fact.claim_index),
            target_excerpt=target_text,
            structural_invariant=source_fact.kind in _HARM_KINDS,
        )
    for target_fact in target_remaining:
        category = _kind_category(target_fact.kind)
        add(
            f"unexpected_{category}",
            _numeric_mismatch_severity(target_fact),
            f"Target adds local {target_fact.kind} value {target_fact.value!r} "
            f"({target_fact.qualifier}).",
            "Remove invented precision or restore the corresponding source claim.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=_claim_text(target_text, target_fact.claim_index),
            structural_invariant=target_fact.kind in _HARM_KINDS,
        )
    return matched


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in occupied)


def _claim_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _CLAIM_SPLIT_RE.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans or [(0, len(text))]


def _claim_index(text: str, position: int) -> int:
    for index, (start, end) in enumerate(_claim_spans(text)):
        if start <= position <= end:
            return index
    return max(0, len(_claim_spans(text)) - 1)


def _claim_text(text: str, index: int) -> str:
    spans = _claim_spans(text)
    if not spans:
        return text
    start, end = spans[min(max(index, 0), len(spans) - 1)]
    return text[start:end].strip()


def _numeric_qualifier(text: str, start: int, end: int) -> str:
    prefix = _normalize(text[max(0, start - 30) : start])
    for qualifier, pattern in _QUALIFIER_PATTERNS:
        if pattern.search(prefix):
            return qualifier
    suffix = _normalize(text[end : min(len(text), end + 30)])
    if re.match(
        r"(?:['’]?[a-zçğıöşü]+\s+|\w+(?:dan|den)?\s+)?"
        r"(?:fazla|aşkın|üzerinde(?:ki)?|üstünde(?:ki)?)\b",
        suffix,
    ):
        return "more_than"
    if re.match(r"\s+(?:civarında|yaklaşık)\b", suffix):
        return "approximate"
    return "exact"


def _classify_numeric_role(text: str, start: int, end: int, value: str) -> str:
    claim = _numeric_context(text, start, end)
    raw = _normalize(text[start:end])

    # Age always wins over nearby death language. "Er starb im Alter von 75
    # Jahren" and "75 yaşında vefat etti" are age claims, never casualty counts.
    if (
        re.search(rf"\bim\s+alter\s+von\s+{re.escape(raw)}\s+jahren?\b", claim)
        or re.search(rf"\b{re.escape(raw)}\s+jahre?\s+alt\b", claim)
        or re.search(rf"\b{re.escape(raw)}\s+yaş(?:ında|indaydı|ındaydı|ında)?\b", claim)
    ):
        return "age"

    person_linked = bool(_PERSON_COUNT_RE.search(claim))
    casualty_linked = bool(_CASUALTY_ROLE_RE.search(claim))
    if casualty_linked and (
        person_linked or any(term in claim for term in ("can kaybı", "opfer", "tote"))
    ):
        return "casualty_count"
    if person_linked and _INJURY_ROLE_RE.search(claim):
        return "injury_count"

    if value.isdigit() and 1900 <= int(value) <= 2099 and not _COUNT_NOUN_RE.search(claim):
        return "year"

    if _DURATION_UNITS_RE.search(claim) and any(
        marker in claim
        for marker in (
            "seit",
            "lang",
            "dauert",
            "dauerte",
            "länger",
            "süredir",
            "sürdü",
            "sürüyor",
            "boyunca",
            "daha uzun",
            "fazla",
        )
    ):
        return "duration"

    if re.search(r"\b(?:temperatur|sıcaklık)\w*|\b(?:grad|derece)\b|°", claim):
        return "temperature"
    return "generic_count"


def _dedupe_repeated_claim_facts(text: str, facts: list[NumericFact]) -> list[NumericFact]:
    """Collapse verbatim repeated claims while retaining distinct local claims."""
    seen_claims: dict[tuple[str, str, str, str], int] = {}
    result: list[NumericFact] = []
    for fact in facts:
        key = (
            _normalize(_claim_text(text, fact.claim_index)),
            fact.kind,
            fact.value,
            fact.qualifier,
        )
        if key in seen_claims and seen_claims[key] != fact.claim_index:
            continue
        seen_claims.setdefault(key, fact.claim_index)
        result.append(fact)
    return result


def _nearest_numeric_fact(
    source: NumericFact,
    candidates: list[NumericFact],
    predicate,
    *,
    max_claim_distance: int | None = None,
) -> NumericFact | None:
    eligible = [candidate for candidate in candidates if predicate(candidate)]
    if max_claim_distance is not None:
        eligible = [
            candidate
            for candidate in eligible
            if abs(candidate.claim_index - source.claim_index) <= max_claim_distance
        ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            abs(candidate.claim_index - source.claim_index),
            abs(candidate.start - source.start),
        ),
    )


def _kind_category(kind: str) -> str:
    return _KIND_CATEGORY.get(kind, kind)


def _numeric_mismatch_severity(
    fact: NumericFact,
) -> Literal["minor", "major", "critical"]:
    if fact.confidence != "high":
        return "minor"
    if fact.kind in _HARM_KINDS:
        return "critical"
    return "major"


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
    candidates: set[str] = set()
    candidates.update(match.group(1) for match in _TITLE_ENTITY_RE.finditer(source_text))
    candidates.update(_ACRONYM_RE.findall(source_text))
    if story.get("location") and not re.search(r"[,/]", story["location"]):
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
        if category == "chronology_suspicion":
            chronology_source = source_normalized
            if group == "until":
                chronology_source = re.sub(
                    r"\b\d+(?:[.,]\d+)?\s+bis\s+\d+(?:[.,]\d+)?\b",
                    "",
                    chronology_source,
                )
            source_present = _chronology_marker_present(chronology_source, group, "de")
            if not source_present:
                continue
            target_present = _chronology_marker_present(target_normalized, group, "tr")
            if group in {"today", "tomorrow", "yesterday"}:
                source_dates = {
                    fact.value for fact in extract_numeric_facts(source_text) if fact.kind == "date"
                }
                target_dates = {
                    fact.value for fact in extract_numeric_facts(target_text) if fact.kind == "date"
                }
                if source_dates and source_dates == target_dates:
                    target_present = True
            if target_present:
                continue
            add(
                category,
                "minor",
                f"Source marker group {group!r} has no clear Turkish counterpart.",
                "Review the local sentence for a possible timeline change.",
                story=story,
                source_excerpt=source_text,
                target_excerpt=target_text,
                review_required=True,
            )
            continue
        marker_source = source_normalized
        if group == "until":
            marker_source = re.sub(
                r"\b\d+(?:[.,]\d+)?\s+bis\s+\d+(?:[.,]\d+)?\b",
                "",
                marker_source,
            )
        source_present = any(_contains_phrase(marker_source, term) for term in language_terms["de"])
        if not source_present:
            continue
        target_present = any(
            _contains_turkish_variant(target_normalized, term) for term in language_terms["tr"]
        )
        if group == "negative" and (
            _TR_VERBAL_NEGATION_RE.search(target_normalized)
            or _TR_NEITHER_RE.search(target_normalized)
        ):
            target_present = True
        if group in {"today", "tomorrow", "yesterday"}:
            source_dates = {
                fact.value for fact in extract_numeric_facts(source_text) if fact.kind == "date"
            }
            target_dates = {
                fact.value for fact in extract_numeric_facts(target_text) if fact.kind == "date"
            }
            if source_dates and source_dates == target_dates:
                target_present = True
        if target_present:
            continue
        add(
            category,
            "minor",
            f"Source marker group {group!r} has no clear Turkish counterpart.",
            "Review the sentence for a possible meaning or timeline change.",
            story=story,
            source_excerpt=source_text,
            target_excerpt=target_text,
        )


def _chronology_marker_present(text: str, group: str, language: str) -> bool:
    return any(
        re.search(pattern, text)
        for pattern in _CHRONOLOGY_PATTERNS.get(group, {}).get(language, ())
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


def _numeric_context(text: str, start: int, end: int) -> str:
    """Return only the sentence containing a number to avoid distant fact labels."""
    left = max(text.rfind(mark, 0, start) for mark in ".!?;") + 1
    boundaries = [position for mark in ".!?;" if (position := text.find(mark, end)) >= 0]
    right = min(boundaries) if boundaries else len(text)
    return _normalize(text[left:right])


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
    normalized = value.strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif "." in normalized:
        groups = normalized.split(".")
        if len(groups[0]) <= 3 and all(len(group) == 3 for group in groups[1:]):
            normalized = "".join(groups)
    try:
        number = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid localized number: {value!r}") from exc
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _scaled_decimal(value: str, magnitude: str | None) -> str:
    normalized_magnitude = _normalize(magnitude or "").rstrip(".")
    if normalized_magnitude.startswith("bin"):
        normalized_magnitude = "bin"
    multiplier = Decimal(
        {
            "hundert": 100,
            "yüz": 100,
            "tausend": 1_000,
            "bin": 1_000,
            "million": 1_000_000,
            "millionen": 1_000_000,
            "mio": 1_000_000,
            "milyon": 1_000_000,
            "milliarde": 1_000_000_000,
            "milliarden": 1_000_000_000,
            "mrd": 1_000_000_000,
            "milyar": 1_000_000_000,
        }.get(normalized_magnitude, 1)
    )
    return _decimal(str(Decimal(_decimal(value)) * multiplier))


def _currency(value: str) -> str:
    normalized = _normalize(value)
    if normalized in {"euro", "eur", "€"} or normalized.startswith("avro"):
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
