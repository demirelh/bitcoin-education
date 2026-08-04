"""Deterministic quality assurance for the editorial broadcast script.

Runs immediately after the script stage and again (for real durations) after
TTS. Every check is deterministic and free — no model call, no API cost.

What it guards:

* nothing is invented: numbers and proper names in the script must exist in the
  approved translation of the same story
* no story disappears silently: everything selected must be present, everything
  teased in the headline block must actually be broadcast
* the anchor really speaks (40-50% target, hard floors below that)
* the programme stays inside its 8:00-10:30 airtime window
* overlay texts are grounded in their story
* no invented personal political opinion
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from btcedu.models.qa_schema import QAFinding
from btcedu.models.script_schema import BroadcastScript, ScriptStory, SegmentPurpose, SpeakerRole

logger = logging.getLogger(__name__)

SCRIPT_QA_FILENAME = "script_qa.json"

# Segments the system owns: greeting, headline block, transitions, weather
# hand-over and closing. They are profile-configured constants, not derived from
# a source story, so grounding them against a story would be meaningless.
SYSTEM_PURPOSES: frozenset[SegmentPurpose] = frozenset(
    {
        SegmentPurpose.OPENING,
        SegmentPurpose.HEADLINES,
        SegmentPurpose.TRANSITION,
        SegmentPurpose.WEATHER_HANDOVER,
        SegmentPurpose.CLOSING,
    }
)

# Frame stories (opening/closing) carry no source story.
SYNTHETIC_SOURCE_PREFIX = "__"


def is_frame_story(story: ScriptStory) -> bool:
    """True for the programme's own opening/closing, which has no source story."""
    return story.source_story_id.startswith(SYNTHETIC_SOURCE_PREFIX)


def editorial_narration(story: ScriptStory) -> str:
    """Spoken text of a story excluding system-owned framing segments."""
    return " ".join(
        segment.text.strip()
        for segment in story.speaker_sequence
        if segment.purpose not in SYSTEM_PURPOSES and segment.text.strip()
    )


# Phrases that express a personal political opinion. The anchor may contextualise
# but never editorialise.
FORBIDDEN_OPINION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "first_person_opinion",
        re.compile(r"\b(bence|bana göre|kanaatimce|kanımca)\b", re.IGNORECASE),
    ),
    (
        "absolute_judgement",
        re.compile(r"\bkesinlikle\s+(adaletsiz|yanlış|haksız|doğru)\b", re.IGNORECASE),
    ),
    ("advocacy", re.compile(r"\b(destekliyoruz|karşıyız|kınıyoruz|reddediyoruz)\b", re.IGNORECASE)),
    ("blame", re.compile(r"\bhükümet\s+yanlış\s+yapıyor\b", re.IGNORECASE)),
)

_NUMBER_PATTERN = re.compile(r"\d[\d.,]*")
# A proper-name candidate: a capitalised token of at least three characters.
# Turkish uppercase letters are listed explicitly because a plain ``A-Z`` class
# does not cover them.
_PROPER_NAME_PATTERN = re.compile(r"\b[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü]{2,}\b")
# Any word, used to build the vocabulary a script is checked against.
_WORD_PATTERN = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
# A token that starts a sentence is capitalised by grammar, not because it names
# something. Sentences end at these characters or at a line break.
_SENTENCE_SPLIT = re.compile(r"(?:[.!?:;]|\n)+")
# Turkish is agglutinative: "gemi" becomes "gemilerden", "Almanya" becomes
# "Almanya'nın". A candidate counts as grounded when a source word shares this
# many leading characters with it, which matches the stem without needing a
# morphological analyser.
_STEM_PREFIX = 4

# Words that are capitalised for emphasis or belong to the programme itself
# rather than to the source report.
_STOPWORD_NAMES = {
    "bu",
    "bir",
    "ancak",
    "fakat",
    "çünkü",
    "ayrıca",
    "böylece",
    "buna",
    "burada",
    "şimdi",
    "bugün",
    "yarın",
    "peki",
    "yani",
    "işte",
    "önümüzdeki",
    "eleştirilerin",
    "almanya",
    "türkiye",
    "avrupa",
    "iyi",
    "akşamlar",
    "haberler",
    "gündem",
    # Titles are capitalised as a courtesy, they do not name anyone.
    "bakan",
    "başbakan",
    "cumhurbaşkanı",
    "şansölye",
    "başkan",
    "bakanı",
    "sözcüsü",
}


@dataclass
class ScriptQAConfig:
    """Thresholds for the script QA gate."""

    min_total_seconds: float = 480.0
    max_total_seconds: float = 630.0
    target_total_seconds: float = 540.0
    anchor_share_min: float = 0.40
    anchor_share_max: float = 0.50
    anchor_share_major_below: float = 0.35
    anchor_share_revision_below: float = 0.25
    # A programme that falls this far below the lower bound is not just a little
    # tight, it is a different programme than promised, so it is sent back.
    duration_revision_below: float = 432.0
    max_automatic_revisions: int = 2

    @classmethod
    def from_stage_config(cls, config: dict[str, Any] | None) -> ScriptQAConfig:
        config = config or {}
        share = config.get("anchor_share_target") or [0.40, 0.50]
        min_total = float(config.get("min_total_seconds", 480))
        return cls(
            min_total_seconds=min_total,
            max_total_seconds=float(config.get("max_total_seconds", 630)),
            target_total_seconds=float(config.get("target_total_seconds", 540)),
            anchor_share_min=float(share[0]),
            anchor_share_max=float(share[1]),
            anchor_share_major_below=float(config.get("anchor_share_major_below", 0.35)),
            anchor_share_revision_below=float(config.get("anchor_share_revision_below", 0.25)),
            duration_revision_below=float(
                config.get("duration_revision_below", round(min_total * 0.9, 3))
            ),
            max_automatic_revisions=int(config.get("max_automatic_revisions", 2)),
        )


@dataclass
class ScriptQAResult:
    """Outcome of a script QA run."""

    findings: list[QAFinding] = field(default_factory=list)
    anchor_share: float = 0.0
    estimated_duration_seconds: float = 0.0
    actual_duration_seconds: float | None = None
    revision_required: bool = False
    revision_reasons: list[str] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def major_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "major")

    @property
    def ok(self) -> bool:
        return self.critical_count == 0 and not self.revision_required

    def to_document(self, episode_id: str, revision: int) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "episode_id": episode_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "revision": revision,
            "anchor_share": self.anchor_share,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "actual_duration_seconds": self.actual_duration_seconds,
            "revision_required": self.revision_required,
            "revision_reasons": self.revision_reasons,
            "summary": {
                "info_count": sum(1 for f in self.findings if f.severity == "info"),
                "minor_count": sum(1 for f in self.findings if f.severity == "minor"),
                "major_count": self.major_count,
                "critical_count": self.critical_count,
                "open_count": sum(1 for f in self.findings if f.status == "open"),
            },
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


class _FindingFactory:
    def __init__(self) -> None:
        self._counter = 0

    def make(
        self,
        *,
        category: str,
        severity: str,
        explanation: str,
        required_action: str,
        story_id: str | None = None,
        source_excerpt: str = "",
        target_excerpt: str = "",
        structural_invariant: bool = False,
    ) -> QAFinding:
        self._counter += 1
        return QAFinding(
            finding_id=f"qa-{self._counter:04d}",
            story_id=story_id,
            category=category,
            severity=severity,  # type: ignore[arg-type]
            source_excerpt=source_excerpt[:400],
            target_excerpt=target_excerpt[:400],
            explanation=explanation,
            required_action=required_action,
            detector="deterministic",
            structural_invariant=structural_invariant,
        )


def _normalize_number(token: str) -> str:
    """Normalise a numeric token so 26.000 and 26,000 compare equal."""
    return re.sub(r"[.,]", "", token).lstrip("0") or "0"


def _numbers(text: str) -> set[str]:
    return {_normalize_number(m.group(0)) for m in _NUMBER_PATTERN.finditer(text)}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _proper_names(text: str) -> set[str]:
    """Extract tokens that genuinely look like proper names.

    Only mixed-case tokens that are not sentence-initial qualify. A token in all
    capitals is display styling (headlines are set in capitals) and a
    sentence-initial token is capitalised by grammar, so neither is evidence
    that a name was used.
    """
    names: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        first = True
        for match in re.finditer(r"[^\W_]+(?:['’][^\W_]+)?", sentence, re.UNICODE):
            token = match.group(0)
            is_first, first = first, False
            if not _PROPER_NAME_PATTERN.fullmatch(token.split("'")[0].split("’")[0]):
                continue
            if is_first:
                continue
            base = token.split("'")[0].split("’")[0]
            if base.isupper():
                continue
            if base.lower() in _STOPWORD_NAMES:
                continue
            names.add(_fold(base))
    return names


def _vocabulary(text: str) -> set[str]:
    """Every word of the source, folded, used as the grounding vocabulary."""
    return {_fold(m.group(0)) for m in _WORD_PATTERN.finditer(text)}


def _is_grounded(name: str, vocabulary: set[str]) -> bool:
    """True when a source word shares this name's stem.

    Exact containment is checked first; otherwise a shared leading run of
    ``_STEM_PREFIX`` characters accepts Turkish case suffixes such as
    "Almanya" versus "Almanya'nın".
    """
    if name in vocabulary:
        return True
    if len(name) < _STEM_PREFIX:
        return False
    prefix = name[:_STEM_PREFIX]
    return any(word.startswith(prefix) for word in vocabulary if len(word) >= _STEM_PREFIX)


def check_grounding(
    script: BroadcastScript,
    approved_by_story: dict[str, str],
    factory: _FindingFactory,
) -> list[QAFinding]:
    """Numbers and proper names in the script must exist in the source story."""
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story):
            continue
        source = approved_by_story.get(story.source_story_id, "")
        if not source:
            findings.append(
                factory.make(
                    category="missing_source",
                    severity="critical",
                    story_id=story.story_id,
                    explanation=(
                        f"Yayın haberi {story.story_id} için kaynak metin "
                        f"({story.source_story_id}) bulunamadı."
                    ),
                    required_action="Kaynak haberi bağla veya haberi yayından çıkar.",
                    structural_invariant=True,
                )
            )
            continue
        source_numbers = _numbers(source)
        source_vocabulary = _vocabulary(source)
        spoken = editorial_narration(story)
        invented_numbers = sorted(_numbers(spoken) - source_numbers)
        if invented_numbers:
            findings.append(
                factory.make(
                    category="invented_number",
                    severity="critical",
                    story_id=story.story_id,
                    source_excerpt=source[:300],
                    target_excerpt=spoken[:300],
                    explanation=(
                        "Yayın metninde kaynakta bulunmayan sayı(lar): "
                        + ", ".join(invented_numbers[:8])
                    ),
                    required_action="Sayıyı kaldır veya kaynaktaki değerle değiştir.",
                    structural_invariant=True,
                )
            )
        invented_names = sorted(
            name for name in _proper_names(spoken) if not _is_grounded(name, source_vocabulary)
        )
        if invented_names:
            findings.append(
                factory.make(
                    category="invented_name",
                    severity="major",
                    story_id=story.story_id,
                    source_excerpt=source[:300],
                    target_excerpt=spoken[:300],
                    explanation=(
                        "Yayın metninde kaynakta bulunmayan özel ad(lar): "
                        + ", ".join(invented_names[:8])
                    ),
                    required_action="Adı kaldır veya kaynaktaki adla değiştir.",
                )
            )
    return findings


def check_overlays(
    script: BroadcastScript,
    approved_by_story: dict[str, str],
    factory: _FindingFactory,
) -> list[QAFinding]:
    """Overlay headline and summary must be grounded and well-formed."""
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story):
            continue
        headline_words = len(story.display_headline.split())
        if not 1 <= headline_words <= 8:
            findings.append(
                factory.make(
                    category="overlay_headline_length",
                    severity="minor",
                    story_id=story.story_id,
                    target_excerpt=story.display_headline,
                    explanation=f"Başlık {headline_words} kelime (hedef 2-6).",
                    required_action="Başlığı kısalt veya genişlet.",
                )
            )
        if story.display_summary:
            summary_words = len(story.display_summary.split())
            if summary_words > 20:
                findings.append(
                    factory.make(
                        category="overlay_summary_length",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=story.display_summary,
                        explanation=f"Özet {summary_words} kelime (hedef 8-15).",
                        required_action="Özeti kısalt.",
                    )
                )
            source = approved_by_story.get(story.source_story_id, "")
            overlay_numbers = _numbers(story.display_headline + " " + story.display_summary)
            if source and overlay_numbers - _numbers(source):
                findings.append(
                    factory.make(
                        category="overlay_ungrounded",
                        severity="major",
                        story_id=story.story_id,
                        source_excerpt=source[:200],
                        target_excerpt=story.display_summary,
                        explanation="Alt yazıda kaynakta bulunmayan sayı var.",
                        required_action="Alt yazıyı kaynaktaki değerlerle düzelt.",
                    )
                )
    return findings


def check_opinion(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """No invented personal political opinion."""
    findings: list[QAFinding] = []
    for story in script.stories:
        for segment in story.speaker_sequence:
            if segment.purpose in SYSTEM_PURPOSES:
                continue
            for name, pattern in FORBIDDEN_OPINION_PATTERNS:
                match = pattern.search(segment.text)
                if match:
                    findings.append(
                        factory.make(
                            category=f"opinion_{name}",
                            severity="critical",
                            story_id=story.story_id,
                            target_excerpt=segment.text[:300],
                            explanation=(f"Kişisel siyasi görüş ifadesi: {match.group(0)!r}"),
                            required_action="İfadeyi tarafsız bir bağlam cümlesiyle değiştir.",
                            structural_invariant=True,
                        )
                    )
    return findings


def check_completeness(
    script: BroadcastScript,
    selected_story_ids: list[str],
    factory: _FindingFactory,
) -> list[QAFinding]:
    """Every selected story must appear; nothing may vanish silently."""
    findings: list[QAFinding] = []
    present = {s.source_story_id for s in script.stories}
    for story_id in selected_story_ids:
        if story_id not in present:
            findings.append(
                factory.make(
                    category="missing_story",
                    severity="critical",
                    story_id=story_id,
                    explanation=f"Seçilen haber {story_id} yayın metninde yok.",
                    required_action="Haberi metne ekle veya çıkarma gerekçesini kaydet.",
                    structural_invariant=True,
                )
            )
    return findings


def check_headline_promises(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Stories teased in the headline block must actually be broadcast."""
    findings: list[QAFinding] = []
    teased: list[str] = []
    for story in script.stories:
        for segment in story.speaker_sequence:
            if segment.purpose == SegmentPurpose.HEADLINES:
                teased.extend(
                    line.strip(" -•\t").strip()
                    for line in segment.text.splitlines()
                    if line.strip()
                )
    if not teased:
        return findings
    broadcast_text = _fold(script.narration)
    broadcast_headlines = {_fold(s.display_headline) for s in script.stories}
    for promise in teased:
        folded = _fold(promise)
        if not folded:
            continue
        if folded in broadcast_headlines:
            continue
        tokens = [t for t in re.findall(r"\w{4,}", folded)]
        if not tokens:
            continue
        hits = sum(1 for token in tokens if token in broadcast_text)
        if hits < max(1, len(tokens) // 2):
            findings.append(
                factory.make(
                    category="teased_but_missing",
                    severity="major",
                    target_excerpt=promise,
                    explanation=f"Başlıklarda duyurulan konu bültende yer almıyor: {promise!r}",
                    required_action="Konuyu bültene ekle veya başlıklardan çıkar.",
                )
            )
    return findings


def check_repetition(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """The anchor must not simply repeat what the reporter says."""
    findings: list[QAFinding] = []
    for story in script.stories:
        anchor_sentences = []
        reporter_text = ""
        for segment in story.speaker_sequence:
            if segment.purpose in SYSTEM_PURPOSES:
                continue
            if segment.role == SpeakerRole.ANCHOR:
                anchor_sentences.extend(
                    s.strip()
                    for s in re.split(r"(?<=[.!?])\s+", segment.text)
                    if len(s.strip()) > 30
                )
            else:
                reporter_text += " " + segment.text
        folded_reporter = _fold(reporter_text)
        for sentence in anchor_sentences:
            if _fold(sentence) and _fold(sentence) in folded_reporter:
                findings.append(
                    factory.make(
                        category="anchor_repeats_reporter",
                        severity="major",
                        story_id=story.story_id,
                        target_excerpt=sentence[:300],
                        explanation="Ana sunucu muhabirin cümlesini birebir tekrarlıyor.",
                        required_action="Sunucu metnini bağlam/değerlendirme olarak yeniden yaz.",
                    )
                )
    return findings


def check_balance_and_duration(
    script: BroadcastScript,
    config: ScriptQAConfig,
    factory: _FindingFactory,
    actual_duration_seconds: float | None = None,
) -> tuple[list[QAFinding], bool, list[str]]:
    """Anchor share and total airtime."""
    findings: list[QAFinding] = []
    revision_reasons: list[str] = []
    share = script.anchor_share()

    if share < config.anchor_share_revision_below:
        findings.append(
            factory.make(
                category="anchor_share",
                severity="critical",
                explanation=f"Ana sunucu payı %{share * 100:.1f} (alt sınır %25).",
                required_action="Metni yeniden üret ve sunucu bölümlerini genişlet.",
                structural_invariant=True,
            )
        )
        revision_reasons.append("anchor_share_below_25")
    elif share < config.anchor_share_major_below:
        findings.append(
            factory.make(
                category="anchor_share",
                severity="major",
                explanation=f"Ana sunucu payı %{share * 100:.1f} (hedef %40-50).",
                required_action="Sunucu tanıtım ve değerlendirme bölümlerini genişlet.",
            )
        )
    elif not (config.anchor_share_min <= share <= config.anchor_share_max):
        findings.append(
            factory.make(
                category="anchor_share",
                severity="minor",
                explanation=f"Ana sunucu payı %{share * 100:.1f} (hedef %40-50).",
                required_action="Dengeyi hedef aralığa yaklaştır.",
            )
        )

    duration = (
        actual_duration_seconds if actual_duration_seconds else script.estimated_duration_seconds
    )
    if duration > config.max_total_seconds:
        findings.append(
            factory.make(
                category="duration_too_long",
                severity="major",
                explanation=(
                    f"Toplam süre {duration / 60:.1f} dk "
                    f"(üst sınır {config.max_total_seconds / 60:.1f} dk)."
                ),
                required_action="Daha fazla haberi kısa habere indir veya çıkar.",
                structural_invariant=True,
            )
        )
        revision_reasons.append("duration_too_long")
    elif duration < config.min_total_seconds:
        substantial = duration < config.duration_revision_below
        findings.append(
            factory.make(
                category="duration_too_short",
                severity="major" if substantial else "minor",
                explanation=(
                    f"Toplam süre {duration / 60:.1f} dk "
                    f"(alt sınır {config.min_total_seconds / 60:.1f} dk)."
                ),
                required_action=(
                    "Çıkarılan haberlerden birini yayına al veya mevcut haberleri "
                    "daha ayrıntılı işle."
                ),
                structural_invariant=substantial,
            )
        )
        if substantial:
            revision_reasons.append("duration_too_short")

    return findings, bool(revision_reasons), revision_reasons


def run_script_qa(
    script: BroadcastScript,
    approved_by_story: dict[str, str],
    selected_story_ids: list[str],
    config: ScriptQAConfig,
    actual_duration_seconds: float | None = None,
) -> ScriptQAResult:
    """Run every deterministic script check."""
    factory = _FindingFactory()
    findings: list[QAFinding] = []
    findings += check_completeness(script, selected_story_ids, factory)
    findings += check_grounding(script, approved_by_story, factory)
    findings += check_overlays(script, approved_by_story, factory)
    findings += check_opinion(script, factory)
    findings += check_headline_promises(script, factory)
    findings += check_repetition(script, factory)
    balance_findings, revision_required, reasons = check_balance_and_duration(
        script, config, factory, actual_duration_seconds
    )
    findings += balance_findings

    if any(f.severity == "critical" and f.category != "anchor_share" for f in findings):
        revision_required = True
        reasons.append("critical_findings")

    return ScriptQAResult(
        findings=findings,
        anchor_share=script.anchor_share(),
        estimated_duration_seconds=script.estimated_duration_seconds,
        actual_duration_seconds=actual_duration_seconds,
        revision_required=revision_required,
        revision_reasons=reasons,
    )


def script_qa_path(outputs_dir: str | Path, episode_id: str) -> Path:
    return Path(outputs_dir) / episode_id / SCRIPT_QA_FILENAME


def write_script_qa(
    outputs_dir: str | Path, episode_id: str, result: ScriptQAResult, revision: int
) -> Path:
    path = script_qa_path(outputs_dir, episode_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_document(episode_id, revision), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_script_qa(outputs_dir: str | Path, episode_id: str) -> dict[str, Any] | None:
    path = script_qa_path(outputs_dir, episode_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def revision_feedback(result: ScriptQAResult) -> str:
    """Human-readable feedback block injected into the retry prompt."""
    if not result.findings:
        return ""
    lines = ["## Önceki taslaktaki sorunlar (lütfen düzelt)", ""]
    for finding in result.findings:
        if finding.severity in {"info", "minor"}:
            continue
        scope = f" [{finding.story_id}]" if finding.story_id else ""
        lines.append(
            f"- ({finding.severity}){scope} {finding.explanation} → {finding.required_action}"
        )
    if len(lines) == 2:
        return ""
    return "\n".join(lines)
