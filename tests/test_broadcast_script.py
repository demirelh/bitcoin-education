"""Tests for the dual-presenter broadcast script: ranking, QA, branding, chapters.

No external provider is contacted: the deterministic assembly path is exercised
directly, which is also the fallback used when the editorial model is
unavailable.
"""

from __future__ import annotations

import pytest

from btcedu.core import branding_guard
from btcedu.core.chapterizer import _chapters_from_script
from btcedu.core.narration_lock import check_narration_lock, compose_chapter_narration
from btcedu.core.script_qa import ScriptQAConfig, run_script_qa
from btcedu.core.scripter import _build_script_stories, _frame_stories
from btcedu.core.story_ranking import RankingBudget, rank_stories
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.script_schema import (
    BroadcastScript,
    SegmentPurpose,
    SpeakerRole,
    SpeakerSegment,
    StoryPriority,
    estimate_duration_seconds,
)

BRANDING = {
    "show_name": "ALMANYA24",
    "slogan": "Almanya'nın nabzı burada atıyor.",
    "visible_source_attribution": False,
}


def _story(story_id, order, category, words, *, story_type="meldung", lead=False):
    # Realistic prose: several sentences, so the deterministic assembly can split
    # a story between the anchor and the reporter.
    tokens = [f"kelime{n}" for n in range(words)]
    text = " ".join(" ".join(tokens[i : i + 12]) + "." for i in range(0, len(tokens), 12))
    return {
        "story_id": story_id,
        "order": order,
        "category": category,
        "story_type": story_type,
        "is_lead_story": lead,
        "headline_tr": f"{category} başlığı {story_id}",
        "text_tr": text,
        "text_adapted_tr": text,
        "word_count": words,
    }


@pytest.fixture
def source_stories():
    return [
        _story("s00", 1, "meta", 20, story_type="intro"),
        _story("s01", 2, "politik", 300, lead=True),
        _story("s02", 3, "wirtschaft", 260),
        _story("s03", 4, "international", 240),
        _story("s04", 5, "gesellschaft", 200),
        _story("s05", 6, "kultur", 180),
        _story("s06", 7, "sport", 160),
        _story("s07", 8, "wetter", 120, story_type="wetter"),
        _story("s08", 9, "meta", 15, story_type="outro"),
    ]


def _script(source_stories, budget=None):
    rankings = rank_stories(source_stories, budget=budget or RankingBudget())
    by_id = {str(s["story_id"]): s for s in source_stories}
    by_rank = {r.story_id: r for r in rankings}
    selected = [
        sid
        for sid in by_id
        if by_rank[sid].priority != StoryPriority.OMIT and by_id[sid].get("text_adapted_tr")
    ]
    stories = _build_script_stories([], by_id, by_rank, selected)
    return (
        BroadcastScript(
            episode_id="ep-test",
            stories=_frame_stories(stories, "ep-test", BRANDING, {}),
            rankings=rankings,
            show_name=BRANDING["show_name"],
        ),
        by_id,
        selected,
    )


class TestStoryRanking:
    def test_source_intro_and_outro_are_never_broadcast(self, source_stories):
        rankings = {r.story_id: r for r in rank_stories(source_stories)}
        assert rankings["s00"].priority == StoryPriority.OMIT
        assert rankings["s08"].priority == StoryPriority.OMIT

    def test_weather_is_always_kept(self, source_stories):
        rankings = {r.story_id: r for r in rank_stories(source_stories)}
        assert rankings["s07"].priority != StoryPriority.OMIT

    def test_ranking_is_deterministic(self, source_stories):
        first = [(r.story_id, r.priority) for r in rank_stories(source_stories)]
        second = [(r.story_id, r.priority) for r in rank_stories(source_stories)]
        assert first == second

    def test_budget_is_respected(self, source_stories):
        budget = RankingBudget(target_seconds=300, min_seconds=240, max_seconds=360)
        rankings = rank_stories(source_stories, budget=budget)
        broadcast = sum(
            r.estimated_duration_seconds for r in rankings if r.priority != StoryPriority.OMIT
        )
        assert broadcast <= budget.max_seconds

    def test_lead_story_outranks_a_soft_story(self, source_stories):
        scores = {r.story_id: r.total_score for r in rank_stories(source_stories)}
        assert scores["s01"] > scores["s05"]


class TestScriptAssembly:
    def test_show_has_its_own_opening_and_closing(self, source_stories):
        script, _, _ = _script(source_stories)
        assert script.stories[0].source_story_id == "__opening__"
        assert script.stories[-1].source_story_id == "__closing__"
        opening = script.stories[0]
        assert opening.display_headline == BRANDING["show_name"]
        assert opening.speaker_sequence[0].role is SpeakerRole.ANCHOR

    def test_both_presenters_speak(self, source_stories):
        script, _, _ = _script(source_stories)
        roles = {segment.role for story in script.stories for segment in story.speaker_sequence}
        assert roles == {SpeakerRole.ANCHOR, SpeakerRole.REPORTER}

    def test_anchor_carries_a_meaningful_share(self, source_stories):
        script, _, _ = _script(source_stories)
        assert 0.25 <= script.anchor_share() <= 0.75

    def test_every_broadcast_story_has_overlay_texts(self, source_stories):
        script, _, _ = _script(source_stories)
        for story in script.stories:
            if story.source_story_id.startswith("__"):
                continue
            assert story.display_headline.strip()
            assert len(story.display_headline.split()) <= 8

    def test_fallback_never_invents_wording(self, source_stories):
        """The deterministic path only re-splits approved text."""
        script, by_id, _ = _script(source_stories)
        for story in script.stories:
            if story.source_story_id.startswith("__"):
                continue
            approved = by_id[story.source_story_id]["text_adapted_tr"]
            spoken = " ".join(s.text for s in story.speaker_sequence)
            for word in spoken.split():
                if word.startswith("kelime"):
                    assert word.rstrip(".") in approved


class TestScriptQA:
    def test_clean_script_passes(self, source_stories):
        script, by_id, selected = _script(source_stories)
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        result = run_script_qa(script, approved, selected, ScriptQAConfig())
        assert not result.revision_required, result.revision_reasons

    def test_invented_number_is_caught(self, source_stories):
        script, by_id, selected = _script(source_stories)
        target = next(s for s in script.stories if not s.source_story_id.startswith("__"))
        target.speaker_sequence.append(
            SpeakerSegment(
                role=SpeakerRole.ANCHOR,
                purpose=SegmentPurpose.ANALYSIS,
                text="Bu kararla birlikte 4711 kişi etkilenecek.",
            )
        )
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        result = run_script_qa(script, approved, selected, ScriptQAConfig())
        assert any(f.category == "invented_number" for f in result.findings)

    def test_opinion_is_caught(self, source_stories):
        script, by_id, selected = _script(source_stories)
        target = next(s for s in script.stories if not s.source_story_id.startswith("__"))
        target.speaker_sequence.append(
            SpeakerSegment(
                role=SpeakerRole.ANCHOR,
                purpose=SegmentPurpose.ANALYSIS,
                text="Bence bu karar kesinlikle yanlış.",
            )
        )
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        result = run_script_qa(script, approved, selected, ScriptQAConfig())
        assert any(f.category.startswith("opinion") for f in result.findings)


class TestBranding:
    def test_attribution_in_an_overlay_is_rejected(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        doc.chapters[1].overlays[0].text = "Kaynak: ARD Tagesschau"
        result = branding_guard.scan_texts(branding_guard.collect_chapter_texts(doc), BRANDING)
        assert not result.ok

    def test_legacy_attribution_is_sanitized(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        doc.title = "tagesschau 20:00 Uhr — Türkçe"
        doc.chapters[1].overlays[0].text = "Kaynak: ARD tagesschau — btcedu Türkçe"
        removed = branding_guard.sanitize_overlays(doc, BRANDING)
        assert removed >= 2
        assert doc.title == BRANDING["show_name"]
        assert branding_guard.scan_texts(branding_guard.collect_chapter_texts(doc), BRANDING).ok

    def test_own_show_name_is_allowed(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        assert branding_guard.scan_texts(branding_guard.collect_chapter_texts(doc), BRANDING).ok

    def test_profiles_that_allow_attribution_are_untouched(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        doc.chapters[1].overlays[0].text = "Kaynak: ARD Tagesschau"
        permissive = {"show_name": "X", "visible_source_attribution": True}
        assert branding_guard.scan_texts(branding_guard.collect_chapter_texts(doc), permissive).ok


def _document(script: BroadcastScript) -> ChapterDocument:
    chapters = _chapters_from_script(script, BRANDING["show_name"])
    return ChapterDocument(
        schema_version="1.0",
        episode_id=script.episode_id,
        title=BRANDING["show_name"],
        total_chapters=len(chapters),
        estimated_duration_seconds=max(
            1, sum(c["narration"]["estimated_duration_seconds"] for c in chapters)
        ),
        chapters=chapters,
    )


class TestDeterministicChapters:
    def test_narration_lock_holds_by_construction(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        result = check_narration_lock(script.narration, compose_chapter_narration(doc.chapters))
        assert result.matches, result.summary()

    def test_one_chapter_per_script_story(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        assert doc.total_chapters == len(script.stories)

    def test_speaker_segments_survive_into_chapters(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        for chapter in doc.chapters:
            segments = chapter.metadata["speaker_segments"]
            assert segments
            for segment in segments:
                assert segment["role"] in {"anchor_female", "reporter_male"}

    def test_weather_chapter_is_detectable(self, source_stories):
        from btcedu.core.weather.detector import detect_weather_story

        script, _, _ = _script(source_stories)
        doc = _document(script)
        weather = [c for c in doc.chapters if c.metadata.get("is_weather")]
        assert len(weather) == 1
        detection = detect_weather_story(
            title=weather[0].title,
            story_type=weather[0].story_type,
            narration_text=weather[0].narration.text,
            metadata=dict(weather[0].metadata),
        )
        assert detection.is_weather_story

    def test_chapters_carry_two_line_overlay_data(self, source_stories):
        script, _, _ = _script(source_stories)
        doc = _document(script)
        content = [c for c in doc.chapters if not c.metadata["source_story_id"].startswith("__")]
        assert content
        for chapter in content:
            assert chapter.display_headline
            assert chapter.overlays and chapter.overlays[0].text


class TestDurationEstimate:
    def test_estimate_scales_with_word_count(self):
        short = estimate_duration_seconds(" ".join(["kelime"] * 75))
        long = estimate_duration_seconds(" ".join(["kelime"] * 150))
        assert short == pytest.approx(30.0, abs=1.0)
        assert long == pytest.approx(60.0, abs=1.0)

    def test_empty_text_has_no_duration(self):
        assert estimate_duration_seconds("   ") == 0.0
