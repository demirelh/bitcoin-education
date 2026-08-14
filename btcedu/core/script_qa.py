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
from btcedu.models.script_schema import (
    BroadcastScript,
    ScriptStory,
    SegmentPurpose,
    SpeakerRole,
    StoryPriority,
)

logger = logging.getLogger(__name__)

SCRIPT_QA_FILENAME = "script_qa.json"

# Segments the system owns: greeting, headline block, transitions, weather
# hand-over and closing. They are profile-configured constants, not derived from
# a source story, so grounding them against a story would be meaningless. The
# external forecast joins them for the same reason: it is attributed to a named
# third party precisely because the broadcast did not supply it.
SYSTEM_PURPOSES: frozenset[SegmentPurpose] = frozenset(
    {
        SegmentPurpose.OPENING,
        SegmentPurpose.HEADLINES,
        SegmentPurpose.TRANSITION,
        SegmentPurpose.WEATHER_HANDOVER,
        SegmentPurpose.WEATHER_EXTERNAL,
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
    # Editorial duration band. When a profile configures ``script.editorial`` the
    # length stops being a hard box: below the minimum is a review note instead
    # of an order to pad, and above the soft maximum only matters when the extra
    # minutes come from repetition. Without the block the historic hard limits
    # stay in force, so existing profiles do not change behaviour.
    editorial_duration_mode: bool = False
    editorial_minimum_seconds: float = 480.0
    preferred_duration_seconds: float = 540.0
    soft_maximum_seconds: float = 630.0
    hard_maximum_seconds: float | None = None

    @classmethod
    def from_stage_config(cls, config: dict[str, Any] | None) -> ScriptQAConfig:
        config = config or {}
        share = config.get("anchor_share_target") or [0.40, 0.50]
        min_total = float(config.get("min_total_seconds", 480))
        editorial = dict(config.get("editorial") or {})
        hard_max = editorial.get("hard_maximum_duration_seconds")
        return cls(
            editorial_duration_mode=bool(editorial),
            editorial_minimum_seconds=float(editorial.get("minimum_duration_seconds", min_total)),
            preferred_duration_seconds=float(
                editorial.get("preferred_duration_seconds", config.get("target_total_seconds", 540))
            ),
            soft_maximum_seconds=float(
                editorial.get("soft_maximum_duration_seconds", config.get("max_total_seconds", 630))
            ),
            hard_maximum_seconds=None if hard_max is None else float(hard_max),
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
    # Why the episode is shorter or longer than preferred. Stored so a human
    # reviewer sees the editorial reason instead of only a number.
    duration_assessment: dict[str, Any] = field(default_factory=dict)

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
            "duration_assessment": self.duration_assessment,
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


def check_presentation_order(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """The main anchor opens the programme, every story and the close.

    The reporter contributes where the anchor hands over, never before her. A
    story that starts with the reporter leaves the viewer without an anchor
    introduction, which is what a newsreader is for.
    """
    findings: list[QAFinding] = []
    for story in script.stories:
        sequence = story.speaker_sequence
        if not sequence:
            continue
        first = sequence[0]
        if first.role is SpeakerRole.ANCHOR:
            continue
        findings.append(
            factory.make(
                category="reporter_opens_story",
                severity="major",
                story_id=story.story_id,
                target_excerpt=first.text[:300],
                explanation=("Haber muhabirle başlıyor; her haberi ana sunucu anons etmeli."),
                required_action=(
                    "Haberin başına ana sunucudan kısa bir anons ekle ve muhabiri "
                    "ondan sonra konuştur."
                ),
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
        findings += _overlap_findings(story, factory)
    findings += _headline_body_findings(script, factory)
    return findings


_STOPWORDS: frozenset[str] = frozenset(
    {
        "bir",
        "bu",
        "da",
        "de",
        "ve",
        "ile",
        "için",
        "olarak",
        "olan",
        "daha",
        "ama",
        "ancak",
        "the",
    }
)


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", _fold(text)) if len(t) > 3 and t not in _STOPWORDS}


def _sentences(text: str, min_chars: int = 40) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) >= min_chars]


def _overlap(first: str, second: str) -> float:
    """Share of the shorter sentence's content words that also appear in the other."""
    a, b = _content_tokens(first), _content_tokens(second)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _overlap_findings(story: ScriptStory, factory: _FindingFactory) -> list[QAFinding]:
    """Near-duplicate sentences inside one story.

    Verbatim repeats were already caught; the bulletin's real problem is the
    same fact said twice in slightly different words, across speakers or within
    a single segment.
    """
    findings: list[QAFinding] = []
    entries: list[tuple[SpeakerRole, str]] = []
    for segment in story.speaker_sequence:
        if segment.purpose in SYSTEM_PURPOSES:
            continue
        entries.extend((segment.role, sentence) for sentence in _sentences(segment.text))
    for index, (role, sentence) in enumerate(entries):
        for other_role, other in entries[index + 1 :]:
            if _overlap(sentence, other) < 0.7:
                continue
            cross = role != other_role
            findings.append(
                factory.make(
                    category="cross_speaker_repetition" if cross else "redundant_story_detail",
                    severity="major" if cross else "minor",
                    story_id=story.story_id,
                    target_excerpt=other[:300],
                    explanation=(
                        "Aynı bilgi iki farklı sunucu tarafından tekrarlanıyor."
                        if cross
                        else "Aynı ayrıntı aynı bölüm içinde tekrar ediliyor."
                    ),
                    required_action="Tekrarı sil, yerine yeni bilgi ver.",
                )
            )
            break
    return findings


def _headline_body_findings(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """A teaser may point at a story; it must not be its first sentence again."""
    findings: list[QAFinding] = []
    headlines = [
        segment.text
        for story in script.stories
        for segment in story.speaker_sequence
        if segment.purpose == SegmentPurpose.HEADLINES
    ]
    if not headlines:
        return findings
    teasers = _sentences(" ".join(headlines), min_chars=30)
    for story in script.stories:
        if is_frame_story(story):
            continue
        body = editorial_narration(story)
        first = next(iter(_sentences(body, min_chars=30)), "")
        if not first:
            continue
        for teaser in teasers:
            if _overlap(teaser, first) >= 0.75:
                findings.append(
                    factory.make(
                        category="headline_body_duplication",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=first[:300],
                        explanation="Haberin ilk cümlesi başlık bloğunun tekrarı.",
                        required_action="Habere yeni bir bilgiyle gir.",
                    )
                )
                break
    return findings


def _editorial_duration_revisions(
    duration: float, config: ScriptQAConfig, redundancy_detected: bool
) -> list[str]:
    """Only length that is both excessive and redundant is sent back."""
    if config.hard_maximum_seconds is not None and duration > config.hard_maximum_seconds:
        return ["duration_above_hard_maximum"]
    if duration > config.soft_maximum_seconds and redundancy_detected:
        return ["episode_overlong_due_to_redundancy"]
    return []


def _editorial_duration_findings(
    duration: float,
    config: ScriptQAConfig,
    factory: _FindingFactory,
    redundancy_detected: bool,
) -> list[QAFinding]:
    """Duration notes that never ask for filler text.

    Falling short is reported for the human review; it is never a reason to
    stretch the bulletin, because padding is what made the programme repetitive
    in the first place.
    """
    findings: list[QAFinding] = []
    if config.hard_maximum_seconds is not None and duration > config.hard_maximum_seconds:
        findings.append(
            factory.make(
                category="duration_above_hard_maximum",
                severity="major",
                explanation=(
                    f"Toplam süre {duration / 60:.1f} dk, kesin üst sınır "
                    f"{config.hard_maximum_seconds / 60:.1f} dk."
                ),
                required_action="Bir haberi kısa habere indir veya yayından çıkar.",
                structural_invariant=True,
            )
        )
        return findings
    if duration > config.soft_maximum_seconds:
        if redundancy_detected:
            findings.append(
                factory.make(
                    category="episode_overlong_due_to_redundancy",
                    severity="major",
                    explanation=(
                        f"Toplam süre {duration / 60:.1f} dk ve metinde tekrar var; "
                        "uzunluk içerikten değil tekrardan kaynaklanıyor."
                    ),
                    required_action="Tekrar eden bölümleri sil, süreyi böylece kısalt.",
                    structural_invariant=True,
                )
            )
        else:
            findings.append(
                factory.make(
                    category="episode_longer_than_preferred",
                    severity="minor",
                    explanation=(
                        f"Toplam süre {duration / 60:.1f} dk, tercih edilen süre "
                        f"{config.preferred_duration_seconds / 60:.1f} dk. İçerik "
                        "gerekçelendiriyorsa kabul edilebilir."
                    ),
                    required_action="Editoryal gerekçe yoksa en zayıf haberi kısalt.",
                )
            )
    elif duration < config.editorial_minimum_seconds:
        findings.append(
            factory.make(
                category="episode_below_editorial_minimum",
                severity="minor",
                explanation=(
                    f"Toplam süre {duration / 60:.1f} dk, hedeflenen alt sınır "
                    f"{config.editorial_minimum_seconds / 60:.1f} dk."
                ),
                required_action=(
                    "Haber değeri olan bir konuyu daha yayına al. Mevcut haberleri "
                    "uzatmak için dolgu cümle ekleme."
                ),
            )
        )
    return findings


def check_balance_and_duration(
    script: BroadcastScript,
    config: ScriptQAConfig,
    factory: _FindingFactory,
    actual_duration_seconds: float | None = None,
    redundancy_detected: bool = False,
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
    if config.editorial_duration_mode:
        findings.extend(
            _editorial_duration_findings(duration, config, factory, redundancy_detected)
        )
        revision_reasons.extend(
            _editorial_duration_revisions(duration, config, redundancy_detected)
        )
        return findings, bool(revision_reasons), revision_reasons

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


# Repetition categories that make a long bulletin a padded bulletin.
REDUNDANCY_CATEGORIES: frozenset[str] = frozenset(
    {
        "anchor_repeats_reporter",
        "cross_speaker_repetition",
        "redundant_story_detail",
        "headline_body_duplication",
    }
)

# Everything that makes a long bulletin a padded one rather than a full one:
# repeated information, over-long reporter blocks and rambling commentary.
PADDING_CATEGORIES: frozenset[str] = REDUNDANCY_CATEGORIES | {
    "reporter_segment_too_verbose",
    "anchor_analysis_too_abstract",
}


def check_brand_pronunciation(
    script: BroadcastScript,
    factory: _FindingFactory,
    spoken_name: str,
    display_name: str,
) -> list[QAFinding]:
    """The on-screen brand must never end up in spoken text.

    "ALMANYA24" read aloud produces both the wrong pronunciation and the wrong
    Turkish suffix, so the script has to use the written-out spoken form.
    """
    if not spoken_name or not display_name or spoken_name == display_name:
        return []
    findings: list[QAFinding] = []
    for story in script.stories:
        for segment in story.speaker_sequence:
            if display_name not in segment.text:
                continue
            findings.append(
                factory.make(
                    category="invalid_spoken_brand_suffix",
                    severity="major",
                    story_id=story.story_id,
                    target_excerpt=segment.text[:300],
                    explanation=(
                        f"Seslendirilen metinde ekran adı {display_name!r} geçiyor; "
                        f"okunuşu {spoken_name!r} olmalı."
                    ),
                    required_action=f"{display_name} yerine {spoken_name} yaz.",
                )
            )
    return findings


def check_story_counting(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Greeting and goodbye are framing, not stories.

    They carry no source story, so a script that gives them a real category
    would make every downstream count (topic cards, QA, dashboard) wrong.
    """
    findings: list[QAFinding] = []
    for story in script.stories:
        if not is_frame_story(story):
            continue
        if story.category not in FRAME_CATEGORIES:
            findings.append(
                factory.make(
                    category="non_story_counted_as_story",
                    severity="minor",
                    story_id=story.story_id,
                    explanation=(
                        f"Açılış/kapanış bölümü {story.category!r} kategorisiyle haber "
                        "gibi sayılıyor."
                    ),
                    required_action="Çerçeve bölümlerini haber sayımından çıkar.",
                )
            )
    return findings


FRAME_CATEGORIES: frozenset[str] = frozenset({"opening", "closing", "intro", "outro"})


# Upper word bounds for the reporter, per story rank. Beyond these a segment
# stops reporting and starts restating, which is what made the bulletin feel
# padded. They are guidance, so exceeding them is a minor finding, never a block.
MAX_REPORTER_WORDS_BY_PRIORITY: dict[StoryPriority, int] = {
    StoryPriority.TOP: 220,
    StoryPriority.NORMAL: 150,
    StoryPriority.BRIEF: 70,
}
MAX_REPORTER_WORDS = 220
MIN_ANCHOR_ANALYSIS_WORDS = 12
# Anchor commentary is spoken language: a handful of short sentences, not an
# essay.
MAX_ANCHOR_ANALYSIS_WORDS = 60

# Wordings that carry a judgement the source did not necessarily make. Repeating
# them sharpens the tone with every mention, so the second occurrence is flagged.
LOADED_TERMS: tuple[str, ...] = ("rejimi", "rejimin", "sözde", "terörist devlet")

# Hedges that cancel each other out. "teorik olarak resmî biçimde" claims to be
# both hypothetical and official, which tells the viewer nothing.
CONTRADICTORY_HEDGES: tuple[tuple[str, str], ...] = (
    ("teorik olarak", "resmî"),
    ("teorik olarak", "kesin"),
    ("iddiaya göre", "kesinlikle"),
    ("muhtemelen", "kesinlikle"),
)

# An on-screen strap is read in a couple of seconds. Beyond this it either gets
# cut off or competes with the subtitles.
MAX_LOWER_THIRD_CHARS = 120


def check_segment_lengths(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Reporter segments that run long and anchor analysis that says nothing."""
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story):
            continue
        for segment in story.speaker_sequence:
            if segment.purpose in SYSTEM_PURPOSES:
                continue
            words = len(segment.text.split())
            limit = MAX_REPORTER_WORDS_BY_PRIORITY.get(story.priority, MAX_REPORTER_WORDS)
            if segment.role == SpeakerRole.REPORTER and words > limit:
                findings.append(
                    factory.make(
                        category="reporter_segment_too_verbose",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=segment.text[:300],
                        explanation=f"Muhabir bölümü {words} kelime (üst sınır {limit}).",
                        required_action="Tekrar eden ayrıntıları çıkararak bölümü kısalt.",
                    )
                )
            if (
                segment.role == SpeakerRole.ANCHOR
                and segment.purpose == SegmentPurpose.ANALYSIS
                and words > MAX_ANCHOR_ANALYSIS_WORDS
            ):
                findings.append(
                    factory.make(
                        category="anchor_analysis_too_abstract",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=segment.text[:300],
                        explanation=(
                            f"Sunucu değerlendirmesi {words} kelime; konuşma dilinde "
                            f"en fazla {MAX_ANCHOR_ANALYSIS_WORDS} kelime olmalı."
                        ),
                        required_action="İki-dört kısa cümleye indir.",
                    )
                )
            if (
                segment.role == SpeakerRole.ANCHOR
                and segment.purpose == SegmentPurpose.ANALYSIS
                and words >= MIN_ANCHOR_ANALYSIS_WORDS
                and _is_abstract(segment.text)
            ):
                findings.append(
                    factory.make(
                        category="anchor_analysis_too_abstract",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=segment.text[:300],
                        explanation=(
                            "Sunucu değerlendirmesi somut bir isim, kurum, sayı veya "
                            "tarih içermiyor."
                        ),
                        required_action="Değerlendirmeyi haberdeki somut bir unsura bağla.",
                    )
                )
    return findings


def _is_abstract(text: str) -> bool:
    """True when a sentence names nothing concrete.

    A useful anchor comment ties back to a person, an institution, a number or a
    date; a comment with none of those is a phrase that would fit any story.
    """
    if any(ch.isdigit() for ch in text):
        return False
    words = text.split()
    for index, word in enumerate(words):
        stripped = word.strip(".,;:!?\"'()")
        if not stripped or not stripped[0].isalpha():
            continue
        if index > 0 and stripped[0] == stripped[0].upper() and stripped[0].isalpha():
            return False
    return True


def broadcast_story_count(script: BroadcastScript) -> int:
    """Number of real stories: framing segments do not count."""
    return sum(1 for story in script.stories if not is_frame_story(story))


def check_headline_reading(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """No presenter reads an on-screen caption out loud.

    ``display_headline`` is two to six words in capitals. Spoken as a sentence
    it sounds like a machine reading a chyron.
    """
    findings: list[QAFinding] = []
    for story in script.stories:
        caption = (story.display_headline or "").strip().rstrip(".")
        for segment in story.speaker_sequence:
            text = segment.text.strip()
            if not text:
                continue
            sentences = [s.strip().rstrip(".") for s in re.split(r"(?<=[.!?])\s+", text)]
            hit = caption and len(caption) > 5 and caption in sentences
            if not hit and not any(_is_shouted_caption(s) for s in sentences if s):
                continue
            findings.append(
                factory.make(
                    category="headline_read_as_script",
                    severity="major",
                    story_id=story.story_id,
                    target_excerpt=text[:300],
                    explanation="Ekran başlığı olduğu gibi seslendirme metnine girmiş.",
                    required_action="Başlığı tam bir cümle olarak yeniden yaz.",
                )
            )
            break
    return findings


def _is_shouted_caption(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 6 or len(text.split()) > 8:
        return False
    return all(ch == ch.upper() for ch in letters)


def check_short_news_transition(
    script: BroadcastScript, factory: _FindingFactory, briefs_label: str
) -> list[QAFinding]:
    """The short-news announcement must be followed by an actual block."""
    if not briefs_label:
        return []
    label = briefs_label.strip().rstrip(".")
    brief_stories = sum(
        1
        for story in script.stories
        if story.priority == StoryPriority.BRIEF and not story.is_weather
    )
    findings: list[QAFinding] = []
    for story in script.stories:
        for segment in story.speaker_sequence:
            if label not in segment.text or brief_stories >= 2:
                continue
            findings.append(
                factory.make(
                    category="false_short_news_transition",
                    severity="major",
                    story_id=story.story_id,
                    target_excerpt=segment.text[:300],
                    explanation=(
                        f"Kısa haber bloğu duyuruluyor ama yalnızca {brief_stories} kısa haber var."
                    ),
                    required_action="Duyuruyu kaldır, konuya uygun bir geçiş cümlesi yaz.",
                )
            )
    return findings


# Empty hand-over formulas: they announce that something follows without saying
# what. A transition either names the new topic or the link between two stories.
EMPTY_TRANSITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsıradaki\s+haber(?:imiz)?\b", re.IGNORECASE),
    re.compile(r"\bbir\s+diğer\s+haber\b", re.IGNORECASE),
    re.compile(r"\bdiğer\s+haberlere\s+geç", re.IGNORECASE),
    re.compile(r"\bbaşka\s+bir\s+konu(?:ya)?\b", re.IGNORECASE),
    re.compile(r"\bgelelim\s+diğer\s+haber", re.IGNORECASE),
    re.compile(r"\bşimdi\s+de\s+şu\s+haber\b", re.IGNORECASE),
)

# Purposes an anchor may use to open a story. The first spoken words of a story
# belong to the anchor (``check_presentation_order``); this check judges what
# those words do.
_ANCHOR_OPENING_PURPOSES: frozenset[SegmentPurpose] = frozenset(
    {SegmentPurpose.INTRODUCTION, SegmentPurpose.TRANSITION, SegmentPurpose.BRIEF}
)


def check_story_transitions(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Every story change is moderated, and never with an empty formula.

    A newscast does not cut from one topic to the next: the anchor names where
    the programme is going. ``Sıradaki haberimiz`` announces a change without
    naming anything, which is exactly the phrasing that makes a bulletin sound
    automated.
    """
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story):
            continue
        for segment in story.speaker_sequence:
            if segment.role is not SpeakerRole.ANCHOR:
                continue
            if segment.purpose not in _ANCHOR_OPENING_PURPOSES:
                continue
            match = next(
                (hit for hit in (p.search(segment.text) for p in EMPTY_TRANSITION_PATTERNS) if hit),
                None,
            )
            if not match:
                continue
            findings.append(
                factory.make(
                    category="empty_transition",
                    severity="major",
                    story_id=story.story_id,
                    target_excerpt=segment.text[:300],
                    explanation=(
                        f"Geçiş cümlesi konuyu adlandırmıyor: {match.group(0)!r} gibi "
                        "boş kalıplar kullanılmış."
                    ),
                    required_action=(
                        "Geçişi konuya bağla: yeni konunun adını ya da iki haber "
                        "arasındaki ilişkiyi söyle."
                    ),
                )
            )
            break
    return findings


def check_anchor_closure(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """After the reporter, the anchor takes the programme back.

    A report that ends with the reporter leaves the viewer at the end of a
    paragraph instead of at the end of a topic. The anchor closes the story with
    a short classification and, by doing so, owns the flow of the programme.

    The deterministic fallback only re-splits approved sentences and cannot
    write a closing line, so it is exempt: flagging it would produce findings no
    revision could ever fix.
    """
    if script.generated_by != "llm":
        return []
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story) or story.is_weather:
            continue
        if story.priority not in {StoryPriority.TOP, StoryPriority.NORMAL}:
            continue
        editorial = [s for s in story.speaker_sequence if s.purpose not in SYSTEM_PURPOSES]
        if not editorial:
            continue
        has_reporter = any(s.role is SpeakerRole.REPORTER for s in editorial)
        if not has_reporter:
            continue
        last = editorial[-1]
        if last.role is SpeakerRole.ANCHOR and last.purpose is SegmentPurpose.ANALYSIS:
            continue
        findings.append(
            factory.make(
                category="missing_anchor_closure",
                severity="major",
                story_id=story.story_id,
                target_excerpt=last.text[:300],
                explanation=(
                    "Haber muhabirle bitiyor; ana sunucu haberi devralıp değerlendirmiyor."
                ),
                required_action=(
                    "Muhabirin ardından ana sunucudan iki-üç cümlelik somut bir "
                    "değerlendirme (`analysis`) ekle."
                ),
                structural_invariant=True,
            )
        )
    return findings


def check_neutral_language(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Loaded wording repeated through the bulletin sharpens the tone."""
    findings: list[QAFinding] = []
    for term in LOADED_TERMS:
        hits = [
            story.story_id
            for story in script.stories
            for segment in story.speaker_sequence
            if term in segment.text.lower()
        ]
        if len(hits) < 2:
            continue
        findings.append(
            factory.make(
                category="loaded_language_repeated",
                severity="minor",
                story_id=hits[0],
                target_excerpt=term,
                explanation=f"{term!r} ifadesi bültende {len(hits)} kez geçiyor.",
                required_action="Kaynağın izin verdiği nötr karşılığı kullan.",
            )
        )
    return findings


def check_hedging(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """Sentences that hedge in two opposite directions at once."""
    findings: list[QAFinding] = []
    for story in script.stories:
        for segment in story.speaker_sequence:
            for sentence in _sentences(segment.text, min_chars=20):
                lowered = sentence.lower()
                hit = next(
                    (
                        pair
                        for pair in CONTRADICTORY_HEDGES
                        if pair[0] in lowered and pair[1] in lowered
                    ),
                    None,
                )
                if not hit:
                    continue
                findings.append(
                    factory.make(
                        category="contradictory_hedging",
                        severity="minor",
                        story_id=story.story_id,
                        target_excerpt=sentence[:300],
                        explanation=(
                            f"Cümlede {hit[0]!r} ve {hit[1]!r} birlikte kullanılmış; "
                            "ifade kendi içinde çelişiyor."
                        ),
                        required_action="Kaynağın izin verdiği kadar net tek bir ifade kullan.",
                    )
                )
    return findings


def check_lower_thirds(script: BroadcastScript, factory: _FindingFactory) -> list[QAFinding]:
    """The strap must add something the first spoken sentence does not."""
    findings: list[QAFinding] = []
    for story in script.stories:
        if is_frame_story(story):
            continue
        summary = (story.display_summary or "").strip()
        if not summary:
            findings.append(
                factory.make(
                    category="missing_lower_third_summary",
                    severity="minor",
                    story_id=story.story_id,
                    explanation="Alt bantta kısa özet yok.",
                    required_action="Tek cümlelik somut bir özet yaz.",
                )
            )
            continue
        if len(summary) > MAX_LOWER_THIRD_CHARS:
            findings.append(
                factory.make(
                    category="lower_third_too_long",
                    severity="minor",
                    story_id=story.story_id,
                    target_excerpt=summary[:300],
                    explanation=(
                        f"Alt bant {len(summary)} karakter (üst sınır {MAX_LOWER_THIRD_CHARS})."
                    ),
                    required_action="Tek cümlelik, 8-15 kelimelik bir özete indir.",
                )
            )
        first = next(iter(_sentences(editorial_narration(story), min_chars=30)), "")
        if first and _overlap(summary, first) >= 0.8:
            findings.append(
                factory.make(
                    category="lower_third_duplicates_first_sentence",
                    severity="minor",
                    story_id=story.story_id,
                    target_excerpt=summary[:300],
                    explanation="Alt bant ilk konuşulan cümlenin aynısı.",
                    required_action="Alt bandı farklı bir bilgiyle yaz.",
                )
            )
    return findings


def check_closing_card(
    script: BroadcastScript, factory: _FindingFactory, closing_card_text: str
) -> list[QAFinding]:
    """The closing card must not repeat what the anchor just said out loud."""
    card = (closing_card_text or "").strip()
    if not card:
        return []
    spoken = " ".join(
        segment.text
        for story in script.stories
        for segment in story.speaker_sequence
        if segment.purpose == SegmentPurpose.CLOSING
    )
    if not spoken:
        return []
    card_phrase = card.split("—")[-1].strip()
    if len(card_phrase) < 10 or _overlap(card_phrase, spoken) < 0.6:
        return []
    return [
        factory.make(
            category="redundant_closing_card_text",
            severity="minor",
            target_excerpt=card_phrase[:300],
            explanation="Kapanış kartı, seslendirilen teşekkürü tekrar ediyor.",
            required_action="Kartta marka ve sloganı bırak.",
        )
    ]


def describe_duration(
    script: BroadcastScript,
    config: ScriptQAConfig,
    duration: float,
    findings: list[QAFinding],
) -> dict[str, Any]:
    """Verdict plus the editorial reason for it.

    A duration outside the band is only meaningful together with its cause, so
    the reason is derived from the material that was actually available.
    """
    if config.editorial_duration_mode:
        minimum = config.editorial_minimum_seconds
        soft_max = config.soft_maximum_seconds
    else:
        minimum = config.min_total_seconds
        soft_max = config.max_total_seconds

    broadcast = broadcast_story_count(script)
    omitted = sum(1 for r in script.rankings if r.priority == StoryPriority.OMIT)
    redundancy = [f for f in findings if f.category in REDUNDANCY_CATEGORIES]
    verbose = [f for f in findings if f.category == "reporter_segment_too_verbose"]

    if duration < minimum:
        verdict = "below_minimum"
        justification = (
            f"Yayına değer {broadcast} haber işlendi, {omitted} haber alaka "
            "düşüklüğü nedeniyle çıkarıldı. Süre dolgu metinle uzatılmadı."
        )
    elif duration > soft_max:
        verdict = "longer_than_preferred"
        if redundancy or verbose:
            justification = (
                f"Uzunluk içerikten değil tekrardan kaynaklanıyor: "
                f"{len(redundancy)} tekrar, {len(verbose)} uzun muhabir bölümü."
            )
        else:
            justification = (
                f"{broadcast} haberin tamamı haber değeri taşıyor ve metinde "
                "tekrar bulunmadı; uzunluk editoryal olarak gerekçeli."
            )
    else:
        verdict = "within_band"
        justification = ""

    return {
        "seconds": round(duration, 1),
        "verdict": verdict,
        "minimum_seconds": minimum,
        "preferred_seconds": (
            config.preferred_duration_seconds
            if config.editorial_duration_mode
            else config.target_total_seconds
        ),
        "soft_maximum_seconds": soft_max,
        "hard_maximum_seconds": config.hard_maximum_seconds,
        "broadcast_story_count": broadcast,
        "omitted_story_count": omitted,
        "justification": justification,
    }


def run_script_qa(
    script: BroadcastScript,
    approved_by_story: dict[str, str],
    selected_story_ids: list[str],
    config: ScriptQAConfig,
    actual_duration_seconds: float | None = None,
    spoken_show_name: str = "",
    display_show_name: str = "",
    briefs_label: str = "",
    closing_card_text: str = "",
) -> ScriptQAResult:
    """Run every deterministic script check."""
    factory = _FindingFactory()
    findings: list[QAFinding] = []
    findings += check_completeness(script, selected_story_ids, factory)
    findings += check_presentation_order(script, factory)
    findings += check_grounding(script, approved_by_story, factory)
    findings += check_overlays(script, approved_by_story, factory)
    findings += check_opinion(script, factory)
    findings += check_headline_promises(script, factory)
    repetition_findings = check_repetition(script, factory)
    findings += repetition_findings
    findings += check_brand_pronunciation(script, factory, spoken_show_name, display_show_name)
    findings += check_story_counting(script, factory)
    length_findings = check_segment_lengths(script, factory)
    findings += length_findings
    findings += check_headline_reading(script, factory)
    findings += check_short_news_transition(script, factory, briefs_label)
    findings += check_story_transitions(script, factory)
    findings += check_anchor_closure(script, factory)
    findings += check_neutral_language(script, factory)
    findings += check_lower_thirds(script, factory)
    findings += check_hedging(script, factory)
    findings += check_closing_card(script, factory, closing_card_text)
    padding_findings = repetition_findings + length_findings
    redundancy_detected = any(f.category in PADDING_CATEGORIES for f in padding_findings)
    balance_findings, revision_required, reasons = check_balance_and_duration(
        script, config, factory, actual_duration_seconds, redundancy_detected
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
        duration_assessment=describe_duration(
            script,
            config,
            actual_duration_seconds or script.estimated_duration_seconds,
            findings,
        ),
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
