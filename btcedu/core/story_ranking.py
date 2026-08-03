"""Deterministic relevance ranking for news stories.

The ranking decides how much airtime a story gets in the finished broadcast.
It is fully deterministic and free: no model call, no API cost, reproducible for
the same input. The editorial LLM later *refines* wording, never the ranking —
so a story can never disappear because a model felt like it.

Audience: Turkish-speaking viewers living in Germany. Topics that touch their
everyday life (pensions, taxes, rent, migration, energy, fuel, work) outrank
generic international or celebrity coverage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from btcedu.models.script_schema import StoryPriority, StoryRanking, estimate_duration_seconds

logger = logging.getLogger(__name__)

# Topic keyword groups scored 0-10 on "how much does this affect our viewers".
# Matched case-insensitively against German headline + Turkish headline + text.
HIGH_RELEVANCE_TOPICS: dict[str, tuple[str, ...]] = {
    "rente": ("rente", "rentner", "pension", "emekli", "altersvorsorge"),
    "steuern": ("steuer", "vergi", "abgabe", "mehrwertsteuer"),
    "sozialleistungen": ("bürgergeld", "sozialhilfe", "kindergeld", "hartz", "sosyal yardım"),
    "migration": (
        "migration",
        "asyl",
        "flüchtling",
        "einbürgerung",
        "göçmen",
        "sığınmacı",
        "aufenthalt",
    ),
    "sicherheit": ("polizei", "anschlag", "terror", "kriminalität", "güvenlik", "saldırı"),
    "wohnen": ("miete", "mieter", "wohnung", "wohnraum", "kira", "konut"),
    "arbeit": ("arbeitsmarkt", "arbeitslos", "tarif", "streik", "gewerkschaft", "işçi", "grev"),
    "energie": ("strompreis", "gaspreis", "energie", "heizung", "enerji", "elektrik"),
    "auto_kraftstoff": ("benzin", "diesel", "spritpreis", "kraftstoff", "akaryakıt", "kfz"),
    "deutsche_politik": (
        "bundestag",
        "bundesregierung",
        "koalition",
        "kanzler",
        "bundesrat",
        "ministerium",
    ),
    "deutschland_tuerkei": ("türkei", "türkiye", "ankara", "istanbul", "erdoğan", "erdogan"),
    "preise": ("inflation", "preise", "verbraucher", "enflasyon", "zam"),
}

MEDIUM_RELEVANCE_TOPICS: dict[str, tuple[str, ...]] = {
    "eu": ("eu-", "europäische union", "brüssel", "avrupa birliği"),
    "konflikt": ("ukraine", "krieg", "gaza", "israel", "nahost", "savaş"),
    "katastrophe": ("erdbeben", "hochwasser", "waldbrand", "unwetter", "deprem", "sel", "yangın"),
    "gesellschaft": ("gedenken", "demonstration", "protest", "anma", "protesto"),
}

LOW_RELEVANCE_TOPICS: dict[str, tuple[str, ...]] = {
    "prominente": ("star", "promi", "royal", "schauspieler", "ünlü"),
    "kultur": ("festival", "ausstellung", "museum", "sergi", "konser"),
    "sport_routine": ("bundesliga", "spieltag", "turnier", "meisterschaft", "şampiyona", "maç"),
}

# Category baseline scores: (germany, diaspora, daily_life, economic, public_interest)
CATEGORY_BASELINE: dict[str, tuple[int, int, int, int, int]] = {
    "politik": (8, 6, 5, 4, 7),
    "wirtschaft": (7, 6, 6, 8, 6),
    "gesellschaft": (6, 5, 4, 2, 6),
    "international": (3, 4, 2, 3, 6),
    "kultur": (2, 2, 1, 1, 3),
    "sport": (2, 2, 1, 1, 4),
    "wetter": (7, 7, 8, 1, 7),
    "meta": (0, 0, 0, 0, 0),
}

_DEFAULT_BASELINE = (4, 4, 3, 3, 5)

# Weights for the total score. Diaspora relevance and daily-life impact are
# weighted highest: that is the entire editorial point of this programme.
_WEIGHTS = {
    "germany_relevance": 1.4,
    "diaspora_relevance": 1.5,
    "daily_life_impact": 1.3,
    "economic_impact": 0.9,
    "urgency": 1.0,
    "public_interest": 0.8,
}


@dataclass
class RankingBudget:
    """Airtime budget the ranking has to fill."""

    target_seconds: float = 540.0
    min_seconds: float = 480.0
    max_seconds: float = 630.0
    # Reserved for opening, headlines block and closing.
    overhead_seconds: float = 55.0


def _haystack(story: dict[str, Any]) -> str:
    parts = [
        str(story.get("headline_de") or ""),
        str(story.get("headline_tr") or ""),
        str(story.get("text_de") or "")[:1200],
        str(story.get("text_adapted_tr") or story.get("text_tr") or "")[:1200],
    ]
    return " ".join(parts).lower()


def _topic_hits(haystack: str, groups: dict[str, tuple[str, ...]]) -> list[str]:
    hits = []
    for name, keywords in groups.items():
        if any(keyword in haystack for keyword in keywords):
            hits.append(name)
    return hits


def _urgency(story: dict[str, Any], haystack: str) -> int:
    score = 4
    if story.get("is_lead_story"):
        score += 4
    if re.search(r"\b(heute|bugün|soeben|az önce|akşam)\b", haystack):
        score += 1
    if any(k in haystack for k in ("eilmeldung", "son dakika", "tot", "öldü", "verletzt")):
        score += 2
    return min(score, 10)


def score_story(story: dict[str, Any], topic_config: dict[str, Any] | None = None) -> StoryRanking:
    """Score a single story dictionary (from ``stories_adapted.json``)."""
    haystack = _haystack(story)
    category = str(story.get("category") or "").lower()
    baseline = CATEGORY_BASELINE.get(category, _DEFAULT_BASELINE)
    germany, diaspora, daily, economic, public = baseline

    high = _topic_hits(haystack, HIGH_RELEVANCE_TOPICS)
    medium = _topic_hits(haystack, MEDIUM_RELEVANCE_TOPICS)
    low = _topic_hits(haystack, LOW_RELEVANCE_TOPICS)

    if high:
        bonus = min(3 + len(high), 5)
        germany += bonus
        diaspora += bonus
        daily += bonus
    if medium:
        germany += 1
        public += 1
    if low and not high:
        germany -= 2
        diaspora -= 2
        daily -= 2

    if "deutschland_tuerkei" in high:
        diaspora = 10
    if category == "wetter":
        daily = 9

    clamp = lambda v: max(0, min(10, int(round(v))))  # noqa: E731
    germany, diaspora, daily = clamp(germany), clamp(diaspora), clamp(daily)
    economic, public = clamp(economic), clamp(public)
    urgency = _urgency(story, haystack)

    values = {
        "germany_relevance": germany,
        "diaspora_relevance": diaspora,
        "daily_life_impact": daily,
        "economic_impact": economic,
        "urgency": urgency,
        "public_interest": public,
    }
    total = round(sum(values[k] * w for k, w in _WEIGHTS.items()), 2)

    text = str(story.get("text_adapted_tr") or story.get("text_tr") or "")
    reasons = []
    if high:
        reasons.append("yüksek ilgi: " + ", ".join(sorted(high)))
    if medium:
        reasons.append("orta ilgi: " + ", ".join(sorted(medium)))
    if low and not high:
        reasons.append("düşük ilgi: " + ", ".join(sorted(low)))
    if story.get("is_lead_story"):
        reasons.append("kaynak yayında manşet")
    reasons.append(f"kategori: {category or 'bilinmiyor'}")

    return StoryRanking(
        story_id=str(story.get("story_id") or ""),
        **values,
        total_score=total,
        reasoning_summary="; ".join(reasons),
        estimated_duration_seconds=estimate_duration_seconds(text),
    )


def _body_seconds(story: dict[str, Any]) -> float:
    """Estimated spoken duration of a story's approved Turkish body."""
    return estimate_duration_seconds(
        str(story.get("text_adapted_tr") or story.get("text_tr") or "")
    )


def _is_meta(story: dict[str, Any]) -> bool:
    return str(story.get("category") or "").lower() == "meta" or str(
        story.get("story_type") or ""
    ).lower() in {"intro", "outro"}


def _is_weather(story: dict[str, Any]) -> bool:
    return (
        str(story.get("category") or "").lower() == "wetter"
        or str(story.get("story_type") or "").lower() == "wetter"
    )


def rank_stories(
    stories: list[dict[str, Any]],
    budget: RankingBudget | None = None,
    overrides: dict[str, str] | None = None,
) -> list[StoryRanking]:
    """Rank all stories and assign ``top``/``normal``/``brief``/``omit``.

    The airtime budget is filled greedily by score. Weather is always kept and
    always compact; the source broadcast's own intro/outro (``meta``) is always
    omitted because the programme has its own opening and closing.
    """
    budget = budget or RankingBudget()
    overrides = overrides or {}
    rankings: list[StoryRanking] = []

    scored: list[tuple[StoryRanking, dict[str, Any]]] = []
    for story in stories:
        ranking = score_story(story)
        if _is_meta(story):
            ranking.priority = StoryPriority.OMIT
            ranking.reasoning_summary = (
                "kaynak yayının kendi açılış/kapanışı; program kendi jeneriğini kullanıyor"
            )
            rankings.append(ranking)
            continue
        scored.append((ranking, story))

    weather = [(r, s) for r, s in scored if _is_weather(s)]
    regular = [(r, s) for r, s in scored if not _is_weather(s)]
    regular.sort(key=lambda pair: (-pair[0].total_score, pair[1].get("order", 0)))

    # Weather is compact and mandatory: reserve its (capped) airtime up front.
    weather_seconds = 0.0
    for ranking, _story in weather:
        ranking.priority = StoryPriority.NORMAL
        ranking.estimated_duration_seconds = min(ranking.estimated_duration_seconds, 70.0)
        weather_seconds += ranking.estimated_duration_seconds

    available = budget.target_seconds - budget.overhead_seconds - weather_seconds
    used = 0.0
    # A "top" story keeps its full body plus anchor intro and analysis (~35%
    # extra); a "brief" is compressed to roughly half of its body.
    for index, (ranking, _story) in enumerate(regular):
        full = ranking.estimated_duration_seconds
        top_cost = full * 1.35
        brief_cost = max(20.0, full * 0.45)
        if index < 3 and used + top_cost <= available:
            ranking.priority = StoryPriority.TOP
            ranking.estimated_duration_seconds = round(top_cost, 2)
            used += top_cost
        elif used + full <= available:
            ranking.priority = StoryPriority.NORMAL
            used += full
        elif used + brief_cost <= available:
            ranking.priority = StoryPriority.BRIEF
            ranking.estimated_duration_seconds = round(brief_cost, 2)
            used += brief_cost
        else:
            ranking.priority = StoryPriority.OMIT
            ranking.reasoning_summary += "; yayın süresi dolduğu için çıkarıldı"

    # Second pass: the greedy fill can leave a large gap when several long
    # stories did not fit. Re-admit the best omitted stories as briefs and
    # restore compressed briefs to full length while airtime remains, so the
    # programme reaches its minimum length instead of ending early.
    floor = budget.min_seconds - budget.overhead_seconds - weather_seconds
    for ranking, story in regular:
        if used >= floor:
            break
        if ranking.priority != StoryPriority.OMIT:
            continue
        cost = max(20.0, _body_seconds(story) * 0.45)
        if used + cost <= available:
            ranking.priority = StoryPriority.BRIEF
            ranking.estimated_duration_seconds = round(cost, 2)
            ranking.reasoning_summary = ranking.reasoning_summary.replace(
                "; yayın süresi dolduğu için çıkarıldı", "; kısa haber olarak yeniden alındı"
            )
            used += cost
    for ranking, story in regular:
        if used >= floor:
            break
        if ranking.priority != StoryPriority.BRIEF:
            continue
        full = _body_seconds(story)
        delta = full - ranking.estimated_duration_seconds
        if delta > 0 and used + delta <= available:
            ranking.priority = StoryPriority.NORMAL
            ranking.estimated_duration_seconds = round(full, 2)
            used += delta

    for ranking, _story in [*regular, *weather]:
        override = overrides.get(ranking.story_id)
        if override in {p.value for p in StoryPriority}:
            ranking.priority = StoryPriority(override)
            ranking.manual_override = True
            ranking.reasoning_summary += "; manuel geçersiz kılma"
        rankings.append(ranking)

    order_index = {str(s.get("story_id")): i for i, s in enumerate(stories)}
    rankings.sort(key=lambda r: order_index.get(r.story_id, 10**6))
    logger.info(
        "Ranked %d stories: %s",
        len(rankings),
        {p.value: sum(1 for r in rankings if r.priority == p) for p in StoryPriority},
    )
    return rankings
