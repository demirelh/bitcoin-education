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
from btcedu.core.script_qa import (
    ScriptQAConfig,
    _FindingFactory,
    broadcast_story_count,
    check_balance_and_duration,
    check_closing_card,
    check_headline_reading,
    check_hedging,
    check_lower_thirds,
    check_neutral_language,
    check_repetition,
    check_segment_lengths,
    check_short_news_transition,
    describe_duration,
    run_script_qa,
)
from btcedu.core.scripter import _build_script_stories, _frame_stories, turkish_dative
from btcedu.core.story_ranking import RankingBudget, rank_stories
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.script_schema import (
    BroadcastScript,
    ScriptStory,
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

    def test_invented_name_is_caught(self, source_stories):
        script, by_id, selected = _script(source_stories)
        target = next(s for s in script.stories if not s.source_story_id.startswith("__"))
        target.speaker_sequence.append(
            SpeakerSegment(
                role=SpeakerRole.ANCHOR,
                purpose=SegmentPurpose.ANALYSIS,
                text="Konuyu Fransa'da Cumhurbaşkanı Macron da değerlendirdi.",
            )
        )
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        result = run_script_qa(script, approved, selected, ScriptQAConfig())
        invented = [f for f in result.findings if f.category == "invented_name"]
        assert invented
        assert "macron" in invented[0].explanation


class TestGroundingHeuristics:
    """Turkish capitalisation and suffixes must not look like invented names."""

    SOURCE = (
        "Ren Nehri'nde su seviyesi düştü. Gemiler yükünü azaltmak zorunda kaldı. "
        "Bakan Wissing açıklama yaptı. Piyasalarda endeks yükseldi."
    )

    def _invented(self, spoken: str) -> list[str]:
        from btcedu.core.script_qa import _is_grounded, _proper_names, _vocabulary

        vocabulary = _vocabulary(self.SOURCE)
        return sorted(name for name in _proper_names(spoken) if not _is_grounded(name, vocabulary))

    def test_all_caps_headline_is_not_a_name(self):
        assert self._invented("REN NEHRİNDE KURAKLIK. KISA ARA.") == []

    def test_sentence_initial_word_is_not_a_name(self):
        assert self._invented("Fabrika üretimi durdurdu. Orman yangını sürüyor.") == []

    def test_turkish_suffix_still_matches_the_stem(self):
        assert self._invented("Gemilerden bazıları limanda bekliyor.") == []
        assert self._invented("Piyasalardaki hareketlilik sürüyor.") == []

    def test_title_before_a_name_is_not_itself_a_name(self):
        assert self._invented("Bugün Bakan Wissing konuştu.") == []

    def test_a_genuinely_new_name_is_reported(self):
        assert "macron" in self._invented("Görüşmeye Cumhurbaşkanı Macron katıldı.")


class TestDurationGate:
    def test_a_substantial_shortfall_requires_revision(self, source_stories):
        script, by_id, selected = _script(source_stories)
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        config = ScriptQAConfig(
            min_total_seconds=script.estimated_duration_seconds + 120.0,
            duration_revision_below=script.estimated_duration_seconds + 60.0,
        )
        result = run_script_qa(script, approved, selected, config)
        assert "duration_too_short" in result.revision_reasons
        short = [f for f in result.findings if f.category == "duration_too_short"]
        assert short and short[0].severity == "major"

    def test_a_small_shortfall_stays_advisory(self, source_stories):
        script, by_id, selected = _script(source_stories)
        approved = {sid: by_id[sid]["text_adapted_tr"] for sid in selected}
        duration = script.estimated_duration_seconds
        config = ScriptQAConfig(
            min_total_seconds=duration + 30.0,
            duration_revision_below=duration - 30.0,
        )
        result = run_script_qa(script, approved, selected, config)
        assert "duration_too_short" not in result.revision_reasons
        short = [f for f in result.findings if f.category == "duration_too_short"]
        assert short and short[0].severity == "minor"

    def test_the_revision_threshold_follows_the_minimum(self):
        config = ScriptQAConfig.from_stage_config({"min_total_seconds": 600})
        assert config.duration_revision_below == 540.0


class TestDeliveryFactor:
    """The editorial pass condenses, so airtime is allocated with headroom."""

    def test_allocation_exceeds_the_plain_target(self, source_stories):
        from btcedu.core.story_ranking import RankingBudget, rank_stories

        def allocated(factor: float) -> float:
            rankings = rank_stories(source_stories, budget=RankingBudget(delivery_factor=factor))
            return sum(
                r.estimated_duration_seconds for r in rankings if r.priority != StoryPriority.OMIT
            )

        assert allocated(0.88) > allocated(1.0)

    def test_allocation_stays_below_the_upper_bound(self, source_stories):
        from btcedu.core.story_ranking import RankingBudget, rank_stories

        budget = RankingBudget(delivery_factor=0.88)
        rankings = rank_stories(source_stories, budget=budget)
        total = (
            sum(r.estimated_duration_seconds for r in rankings if r.priority != StoryPriority.OMIT)
            + budget.overhead_seconds
        )
        assert total <= budget.max_seconds


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

    def test_image_prompt_loses_the_source_name(self):
        prompt = (
            "Photorealistic editorial news illustration in the visual style of a "
            "European public-broadcaster newscast (ARD/Tagesschau). Neutral tone."
        )
        cleaned, removed = branding_guard.sanitize_image_prompt(prompt, BRANDING)
        assert removed == ["tagesschau"]
        assert "tagesschau" not in cleaned.lower()
        assert "Neutral tone." in cleaned
        assert "  " not in cleaned

    def test_image_prompt_keeps_the_own_show_name(self):
        prompt = f"{BRANDING['show_name']} studio, clean broadcast composition"
        cleaned, removed = branding_guard.sanitize_image_prompt(prompt, BRANDING)
        assert removed == []
        assert cleaned == prompt

    def test_image_prompt_is_untouched_for_permissive_profiles(self):
        prompt = "newscast in the style of ARD Tagesschau"
        permissive = {"show_name": "X", "visible_source_attribution": True}
        cleaned, removed = branding_guard.sanitize_image_prompt(prompt, permissive)
        assert removed == []
        assert cleaned == prompt


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

    def test_short_chapter_merging_must_not_touch_script_chapters(self, source_stories):
        """The merge rewrites narration in place, so it must not run at all.

        The closing is far shorter than the merge threshold. Running the merge
        appended it to the preceding chapter while the closing chapter itself
        remained, which silently duplicated it and broke the narration lock.
        """
        from btcedu.core.chapterizer import _merge_short_chapters

        script, _, _ = _script(source_stories)
        doc = _document(script)
        before = [c.narration.text for c in doc.chapters]
        _merge_short_chapters(list(doc.chapters), 30)
        after = [c.narration.text for c in doc.chapters]
        assert after != before, "expected the merge to mutate in place"

        doc = _document(script)
        result = check_narration_lock(script.narration, compose_chapter_narration(doc.chapters))
        assert result.matches, result.summary()

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

    def test_weather_is_narrated_by_the_anchor_alone(self, source_stories):
        """The forecast never hands over to the reporter mid-way."""
        script, _, _ = _script(source_stories)
        doc = _document(script)
        weather = [c for c in doc.chapters if c.metadata.get("is_weather")]
        assert len(weather) == 1
        roles = {s["role"] for s in weather[0].metadata["speaker_segments"]}
        assert roles == {"anchor_female"}

    def test_weather_story_without_the_category_is_still_the_anchor(self):
        """The visual side detects weather from the text; the voice must agree."""
        from btcedu.core.scripter import _is_weather_story

        story = {
            "story_id": "s99",
            "category": "vermischtes",
            "story_type": "meldung",
            "headline_tr": "Hava Tahmini",
            "text_adapted_tr": (
                "Yarın kuzeyde yağmur bekleniyor. Güneyde güneşli hava hakim olacak. "
                "Sıcaklıklar 18 ile 26 derece arasında seyredecek."
            ),
        }
        assert _is_weather_story(story, "Hava Tahmini") is True

    def test_an_ordinary_story_is_not_taken_for_the_weather(self):
        from btcedu.core.scripter import _is_weather_story

        story = {
            "story_id": "s98",
            "category": "politik",
            "story_type": "meldung",
            "headline_tr": "Bakanlar kurulu toplandı",
            "text_adapted_tr": (
                "Bakanlar kurulu bugün Berlin'de toplandı. Görüşmelerin ardından "
                "bir açıklama yapılması bekleniyor."
            ),
        }
        assert _is_weather_story(story, "Bakanlar kurulu toplandı") is False

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
        from btcedu.models.script_schema import WORDS_PER_MINUTE

        half = " ".join(["kelime"] * (WORDS_PER_MINUTE // 2))
        full = " ".join(["kelime"] * WORDS_PER_MINUTE)
        assert estimate_duration_seconds(half) == pytest.approx(30.0, abs=1.0)
        assert estimate_duration_seconds(full) == pytest.approx(60.0, abs=1.0)

    def test_empty_text_has_no_duration(self):
        assert estimate_duration_seconds("   ") == 0.0


class TestTwoLineOverlays:
    """The chapter overlay carries a headline and a summary line."""

    @staticmethod
    def _chapter_with_subtext():
        from btcedu.models.chapter_schema import (
            Chapter,
            Narration,
            Overlay,
            Transitions,
            Visual,
        )

        return Chapter(
            chapter_id="ch01",
            title="Test",
            order=1,
            narration=Narration(text="metin", word_count=1, estimated_duration_seconds=5),
            visual=Visual(type="b_roll", description="d", image_prompt="p"),
            transitions=Transitions(**{"in": "fade", "out": "cut"}),
            overlays=[
                Overlay(
                    type="lower_third",
                    text="Ren Nehri'nde su seviyesi düştü",
                    subtext="Kuraklık nedeniyle yük gemileri kapasitesinin altında çalışıyor.",
                    start_offset_seconds=1.0,
                    duration_seconds=6.0,
                )
            ],
        )

    def test_static_renderer_gets_one_spec_per_line(self):
        from btcedu.core.renderer import _chapter_to_overlay_specs

        specs = _chapter_to_overlay_specs(
            self._chapter_with_subtext(), "Roboto", animated_lower_thirds=False
        )
        assert len(specs) == 2
        assert specs[0].position == "lower_third_headline"
        assert specs[1].position == "lower_third_subtext"
        assert specs[1].fontsize < specs[0].fontsize
        assert specs[0].end == specs[1].end

    def test_animated_renderer_gets_a_single_two_line_spec(self):
        from btcedu.core.renderer import _chapter_to_overlay_specs

        specs = _chapter_to_overlay_specs(
            self._chapter_with_subtext(), "Roboto", animated_lower_thirds=True
        )
        assert len(specs) == 1
        assert "\\n" in specs[0].text

    def test_overlay_without_subtext_is_unchanged(self):
        from btcedu.core.renderer import _chapter_to_overlay_specs

        chapter = self._chapter_with_subtext()
        chapter.overlays[0].subtext = None
        for animated in (False, True):
            specs = _chapter_to_overlay_specs(chapter, "Roboto", animated_lower_thirds=animated)
            assert len(specs) == 1
            assert specs[0].position == "bottom_center"

    def test_long_summary_is_shortened_to_fit(self):
        from btcedu.core.renderer import _shorten_overlay_text

        result = _shorten_overlay_text("kelime " * 40, max_chars=62)
        assert len(result) <= 62
        assert result.endswith("…")

    def test_short_summary_is_left_alone(self):
        from btcedu.core.renderer import _shorten_overlay_text

        assert _shorten_overlay_text("Kısa bir özet.") == "Kısa bir özet."

    def test_two_line_bar_is_taller_than_the_single_line_bar(self):
        from btcedu.services.ffmpeg_service import (
            OverlaySpec,
            _build_animated_lower_third,
        )

        def bar_height(text):
            spec = OverlaySpec(
                text=text,
                overlay_type="lower_third",
                fontsize=52,
                fontcolor="white",
                font="Roboto",
                position="bottom_center",
                start=0.0,
                end=5.0,
            )
            drawbox = _build_animated_lower_third(spec, "/font.ttf")[0]
            return int(drawbox.split(":h=")[1].split(":")[0])

        assert bar_height("Başlık\\nÖzet cümlesi.") > bar_height("Başlık")


class TestPresentationOrder:
    def test_every_story_starts_with_the_anchor(self, source_stories):
        script, _, _ = _script(source_stories)
        for story in script.stories:
            assert story.speaker_sequence, story.story_id
            assert story.speaker_sequence[0].role is SpeakerRole.ANCHOR, story.story_id

    def test_opening_and_closing_belong_to_the_anchor(self, source_stories):
        script, _, _ = _script(source_stories)
        frames = [s for s in script.stories if s.source_story_id in ("__opening__", "__closing__")]
        assert len(frames) == 2
        for story in frames:
            roles = {segment.role for segment in story.speaker_sequence}
            assert roles == {SpeakerRole.ANCHOR}

    def test_a_reporter_led_story_is_reported(self, source_stories):
        from btcedu.core.script_qa import _FindingFactory, check_presentation_order

        script, _, _ = _script(source_stories)
        target = next(s for s in script.stories if len({x.role for x in s.speaker_sequence}) == 2)
        target.speaker_sequence = list(reversed(target.speaker_sequence))
        findings = check_presentation_order(script, _FindingFactory())
        assert [f.category for f in findings] == ["reporter_opens_story"]
        assert findings[0].severity == "major"


class TestVisualBeats:
    def test_a_speaker_change_creates_a_new_beat(self, source_stories):
        from btcedu.core.chapterizer import _visual_beats

        script, _, _ = _script(source_stories)
        story = next(
            s for s in script.stories if len({seg.role for seg in s.speaker_sequence}) == 2
        )
        beats = _visual_beats(story)
        assert len(beats) >= 2
        roles = [beat["role"] for beat in beats]
        assert all(a != b for a, b in zip(roles, roles[1:], strict=False))
        joined = " ".join(beat["text"] for beat in beats)
        assert joined == " ".join(seg.text for seg in story.speaker_sequence)

    def test_a_single_speaker_needs_no_beats(self, source_stories):
        from btcedu.core.chapterizer import _visual_beats

        script, _, _ = _script(source_stories)
        story = next(
            s for s in script.stories if len({seg.role for seg in s.speaker_sequence}) == 1
        )
        assert _visual_beats(story) == []

    def test_consecutive_segments_of_one_speaker_share_a_beat(self, source_stories):
        from btcedu.core.chapterizer import _visual_beats

        script, _, _ = _script(source_stories)
        story = next(s for s in script.stories if s.source_story_id == "__opening__")
        story.speaker_sequence.append(
            SpeakerSegment(
                role=SpeakerRole.REPORTER,
                purpose=SegmentPurpose.REPORT,
                text="Muhabir burada konuşuyor.",
            )
        )
        beats = _visual_beats(story)
        assert [b["role"] for b in beats] == ["anchor_female", "reporter_male"]
        assert beats[0]["segment_indices"] == [0, 1]

    def test_beat_durations_add_up_to_the_chapter(self):
        from btcedu.core.renderer import _beat_durations

        beats = [
            {"segment_indices": [0], "text": "bir"},
            {"segment_indices": [1], "text": "iki"},
            {"segment_indices": [2], "text": "üç"},
        ]
        parts = [
            {"duration_seconds": 30.0},
            {"duration_seconds": 70.0},
            {"duration_seconds": 20.0},
        ]
        durations = _beat_durations(beats, parts, 150.0)
        assert sum(durations) == pytest.approx(150.0)
        assert durations[0] < durations[1]

    def test_beat_durations_fall_back_to_word_counts(self):
        from btcedu.core.renderer import _beat_durations

        beats = [
            {"segment_indices": [0], "text": "bir iki üç dört"},
            {"segment_indices": [1], "text": "bir iki"},
        ]
        durations = _beat_durations(beats, [], 60.0)
        assert sum(durations) == pytest.approx(60.0)
        assert durations[0] > durations[1]

    def test_beat_images_are_ordered_by_beat_index(self):
        from btcedu.core.renderer import _beat_images

        manifest = {
            "images": [
                {"chapter_id": "ch02", "file_path": "images/b1.png", "metadata": {"beat_index": 1}},
                {"chapter_id": "ch02", "file_path": "images/b0.png", "metadata": {"beat_index": 0}},
                {"chapter_id": "ch03", "file_path": "images/other.png", "metadata": {}},
                {
                    "chapter_id": "ch02",
                    "file_path": "images/bad.png",
                    "generation_method": "failed",
                    "metadata": {"beat_index": 2},
                },
            ]
        }
        assert _beat_images("ch02", manifest) == ["images/b0.png", "images/b1.png"]


SPOKEN_BRANDING = {
    **BRANDING,
    "display_name": "ALMANYA24",
    "spoken_name": "Almanya Yirmi Dört",
}


def _segment_texts(script, purpose):
    return [
        segment.text
        for story in script.stories
        for segment in story.speaker_sequence
        if segment.purpose == purpose
    ]


class TestSpokenBrandName:
    def test_dative_follows_vowel_harmony(self):
        assert turkish_dative("Almanya Yirmi Dört") == "Almanya Yirmi Dört'e"
        assert turkish_dative("Almanya") == "Almanya'ya"
        assert turkish_dative("Kanal Bir") == "Kanal Bir'e"
        assert turkish_dative("Bulut") == "Bulut'a"

    def test_opening_uses_the_spoken_name_not_the_logo(self, source_stories):
        script, _, _ = _script(source_stories)
        body = [s for s in script.stories if not s.source_story_id.startswith("__")]
        framed = _frame_stories(body, "ep-test", SPOKEN_BRANDING, {})
        opening = " ".join(
            s.text for st in framed for s in st.speaker_sequence if s.purpose.value == "opening"
        )
        assert "ALMANYA24" not in opening
        assert "Almanya Yirmi Dört" in opening
        assert "Dört'na" not in opening

    def test_the_screen_keeps_the_logo_spelling(self, source_stories):
        script, _, _ = _script(source_stories)
        body = [s for s in script.stories if not s.source_story_id.startswith("__")]
        framed = _frame_stories(body, "ep-test", SPOKEN_BRANDING, {})
        assert framed[0].display_headline == "ALMANYA24"

    def test_qa_flags_the_logo_inside_spoken_text(self, source_stories):
        script, by_id, selected = _script(source_stories)
        script.stories[0].speaker_sequence[0].text = "İyi akşamlar. ALMANYA24'na hoş geldiniz."
        result = run_script_qa(
            script,
            {sid: by_id[sid]["text_adapted_tr"] for sid in selected},
            selected,
            ScriptQAConfig(),
            spoken_show_name="Almanya Yirmi Dört",
            display_show_name="ALMANYA24",
        )
        assert any(f.category == "invalid_spoken_brand_suffix" for f in result.findings)


class TestOpeningAndClosing:
    def test_headlines_are_sentences_not_screen_titles(self, source_stories):
        script, _, _ = _script(source_stories)
        headlines = " ".join(_segment_texts(script, SegmentPurpose.HEADLINES))
        assert headlines
        # The block is announced and closes with a hand-over that names the
        # first topic instead of a content-free "let's begin".
        assert headlines.startswith("Bülteni açan başlıklar şöyle.")
        assert headlines.strip().endswith("başlıyoruz.")
        first_topic = next(
            story.display_headline for story in script.stories if story.source_story_id == "s01"
        )
        from btcedu.core.scripter import turkish_sentence_case

        assert turkish_sentence_case(first_topic) in headlines
        for word in headlines.split():
            letters = [c for c in word if c.isalpha()]
            assert not (len(letters) > 5 and all(c == c.upper() for c in letters))

    def test_the_opening_frames_the_news_day(self, source_stories):
        script, _, _ = _script(source_stories)
        opening = " ".join(_segment_texts(script, SegmentPurpose.OPENING))
        # Greeting plus a framing sentence: a bare "good evening" is a machine
        # starting a file, not a programme opening.
        assert "İyi akşamlar" in opening
        assert len([s for s in opening.split(".") if s.strip()]) >= 2

    def test_the_closing_promises_continued_coverage(self, source_stories):
        script, _, _ = _script(source_stories)
        closing = " ".join(_segment_texts(script, SegmentPurpose.CLOSING))
        assert "iyi akşamlar" in closing.lower()
        assert "devam ede" in closing.lower() or "sürdür" in closing.lower()

    def test_opening_has_no_empty_promise(self, source_stories):
        script, _, _ = _script(source_stories)
        opening = " ".join(_segment_texts(script, SegmentPurpose.OPENING))
        assert "İşte ayrıntılar" not in opening


class TestShortNewsTransition:
    def _brief_label_count(self, stories):
        return sum(
            1
            for story in stories
            for segment in story.speaker_sequence
            if segment.text == "Kısa haberlerle devam ediyoruz."
        )

    def test_single_brief_story_gets_no_block_announcement(self, source_stories):
        script, _, _ = _script(source_stories)
        stories = [s for s in script.stories if not s.source_story_id.startswith("__")]
        briefs = [s for s in stories if s.priority == StoryPriority.BRIEF and not s.is_weather]
        for story in briefs[1:]:
            story.priority = StoryPriority.NORMAL
        framed = _frame_stories(stories, "ep-test", BRANDING, {})
        assert self._brief_label_count(framed) == 0

    def test_a_real_block_is_still_announced_once(self, source_stories):
        script, _, _ = _script(source_stories)
        stories = [s for s in script.stories if not s.source_story_id.startswith("__")]
        for story in stories:
            if not story.is_weather:
                story.priority = StoryPriority.BRIEF
        framed = _frame_stories(stories, "ep-test", BRANDING, {})
        assert self._brief_label_count(framed) == 1


def _duration_reasons(reasons):
    return [r for r in reasons if "duration" in r or "episode_" in r]


class TestEditorialDuration:
    def _script_with_share(self):
        """A script whose anchor share is fine, so only duration is judged."""
        return BroadcastScript(
            episode_id="ep",
            stories=[
                ScriptStory(
                    story_id="ch01",
                    source_story_id="s01",
                    order=1,
                    priority=StoryPriority.TOP,
                    category="politik",
                    display_headline="BAŞLIK",
                    display_summary="Özet.",
                    speaker_sequence=[
                        SpeakerSegment(
                            role=SpeakerRole.ANCHOR,
                            purpose=SegmentPurpose.INTRODUCTION,
                            text=" ".join(f"a{n}" for n in range(45)),
                        ),
                        SpeakerSegment(
                            role=SpeakerRole.REPORTER,
                            purpose=SegmentPurpose.REPORT,
                            text=" ".join(f"r{n}" for n in range(55)),
                        ),
                    ],
                )
            ],
        )

    def _config(self):
        return ScriptQAConfig.from_stage_config(
            {
                "editorial": {
                    "minimum_duration_seconds": 540,
                    "preferred_duration_seconds": 600,
                    "soft_maximum_duration_seconds": 720,
                    "hard_maximum_duration_seconds": None,
                    "allow_longer_if_editorially_justified": True,
                }
            }
        )

    def test_long_but_dense_is_not_sent_back(self):
        findings, revision, reasons = check_balance_and_duration(
            self._script_with_share(),
            self._config(),
            _FindingFactory(),
            actual_duration_seconds=800,
            redundancy_detected=False,
        )
        assert revision is False
        assert _duration_reasons(reasons) == []
        categories = [
            f.category for f in findings if "duration" in f.category or "longer" in f.category
        ]
        assert categories == ["episode_longer_than_preferred"]

    def test_long_and_repetitive_is_sent_back(self):
        _, revision, reasons = check_balance_and_duration(
            self._script_with_share(),
            self._config(),
            _FindingFactory(),
            actual_duration_seconds=800,
            redundancy_detected=True,
        )
        assert revision is True
        assert _duration_reasons(reasons) == ["episode_overlong_due_to_redundancy"]

    def test_short_episode_is_reported_but_never_padded(self):
        findings, revision, reasons = check_balance_and_duration(
            self._script_with_share(),
            self._config(),
            _FindingFactory(),
            actual_duration_seconds=400,
            redundancy_detected=False,
        )
        assert revision is False
        assert _duration_reasons(reasons) == []
        finding = next(f for f in findings if f.category == "episode_below_editorial_minimum")
        assert "dolgu" in finding.required_action

    def test_without_the_editorial_block_the_hard_limits_stay(self):
        _, revision, reasons = check_balance_and_duration(
            self._script_with_share(),
            ScriptQAConfig(),
            _FindingFactory(),
            actual_duration_seconds=800,
            redundancy_detected=False,
        )
        assert revision is True
        assert _duration_reasons(reasons) == ["duration_too_long"]


class TestRepetitionChecks:
    def _story_with(self, segments):
        return ScriptStory(
            story_id="ch01",
            source_story_id="s01",
            order=1,
            priority=StoryPriority.TOP,
            category="politik",
            display_headline="BAŞLIK",
            display_summary="Özet cümlesi.",
            speaker_sequence=segments,
        )

    def test_the_same_fact_from_both_presenters_is_flagged(self):
        story = self._story_with(
            [
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR,
                    purpose=SegmentPurpose.INTRODUCTION,
                    text=(
                        "Federal hükümet emeklilik reformunu bugün kabul etti ve "
                        "düzenleme gelecek yıl yürürlüğe girecek."
                    ),
                ),
                SpeakerSegment(
                    role=SpeakerRole.REPORTER,
                    purpose=SegmentPurpose.REPORT,
                    text=(
                        "Emeklilik reformunu federal hükümet bugün kabul etti, "
                        "düzenleme gelecek yıl yürürlüğe girecek."
                    ),
                ),
            ]
        )
        script = BroadcastScript(episode_id="ep", stories=[story])
        findings = check_repetition(script, _FindingFactory())
        assert any(f.category == "cross_speaker_repetition" for f in findings)

    def test_distinct_information_is_not_flagged(self):
        story = self._story_with(
            [
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR,
                    purpose=SegmentPurpose.INTRODUCTION,
                    text="Federal hükümet emeklilik reformunu bugün kabul etti.",
                ),
                SpeakerSegment(
                    role=SpeakerRole.REPORTER,
                    purpose=SegmentPurpose.REPORT,
                    text="Sendikalar düzenlemenin genç çalışanları zorlayacağını savunuyor.",
                ),
            ]
        )
        script = BroadcastScript(episode_id="ep", stories=[story])
        assert check_repetition(script, _FindingFactory()) == []


class TestSegmentLengths:
    def test_a_very_long_reporter_segment_is_flagged(self):
        story = ScriptStory(
            story_id="ch01",
            source_story_id="s01",
            order=1,
            priority=StoryPriority.TOP,
            category="politik",
            display_headline="BAŞLIK",
            display_summary="Özet.",
            speaker_sequence=[
                SpeakerSegment(
                    role=SpeakerRole.REPORTER,
                    purpose=SegmentPurpose.REPORT,
                    text=" ".join(f"kelime{n}" for n in range(260)),
                )
            ],
        )
        script = BroadcastScript(episode_id="ep", stories=[story])
        findings = check_segment_lengths(script, _FindingFactory())
        assert [f.category for f in findings] == ["reporter_segment_too_verbose"]


class TestStoryCounting:
    def test_framing_is_not_counted_as_a_story(self, source_stories):
        script, _, _ = _script(source_stories)
        assert broadcast_story_count(script) == len(script.stories) - 2


class TestHeadlineReading:
    def _story(self, text):
        return ScriptStory(
            story_id="ch01",
            source_story_id="s01",
            order=1,
            priority=StoryPriority.TOP,
            category="politik",
            display_headline="LEIPZIG'DE İHA ALARMI",
            display_summary="Patlayıcılı İHA havalimanındaki uçuşları durdurdu.",
            speaker_sequence=[
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.INTRODUCTION, text=text
                )
            ],
        )

    def test_a_screen_title_spoken_verbatim_is_flagged(self):
        script = BroadcastScript(episode_id="ep", stories=[self._story("LEIPZIG'DE İHA ALARMI.")])
        findings = check_headline_reading(script, _FindingFactory())
        assert [f.category for f in findings] == ["headline_read_as_script"]

    def test_a_natural_sentence_is_accepted(self):
        script = BroadcastScript(
            episode_id="ep",
            stories=[
                self._story(
                    "Leipzig-Halle Havalimanı'nda patlayıcı taşıdığı belirtilen bir İHA "
                    "uçuşları saatlerce durdurdu."
                )
            ],
        )
        assert check_headline_reading(script, _FindingFactory()) == []


class TestFalseShortNewsTransition:
    def _script(self, brief_count):
        stories = []
        for index in range(brief_count):
            stories.append(
                ScriptStory(
                    story_id=f"ch{index:02d}",
                    source_story_id=f"s{index:02d}",
                    order=index + 1,
                    priority=StoryPriority.BRIEF,
                    category="politik",
                    display_headline="BAŞLIK",
                    display_summary="Özet.",
                    speaker_sequence=[
                        SpeakerSegment(
                            role=SpeakerRole.ANCHOR,
                            purpose=SegmentPurpose.TRANSITION,
                            text="Kısa haberlerle devam ediyoruz." if index == 0 else "Devam.",
                        )
                    ],
                )
            )
        return BroadcastScript(episode_id="ep", stories=stories)

    def test_announcement_without_a_block_is_flagged(self):
        findings = check_short_news_transition(
            self._script(1), _FindingFactory(), "Kısa haberlerle devam ediyoruz."
        )
        assert [f.category for f in findings] == ["false_short_news_transition"]

    def test_a_real_block_is_accepted(self):
        findings = check_short_news_transition(
            self._script(3), _FindingFactory(), "Kısa haberlerle devam ediyoruz."
        )
        assert findings == []


class TestNeutralLanguageAndOverlays:
    def _story(self, story_id, text, summary="Sekiz sanığa ceza verildi.", order=1):
        return ScriptStory(
            story_id=story_id,
            source_story_id=story_id.replace("ch", "s"),
            order=order,
            priority=StoryPriority.NORMAL,
            category="international",
            display_headline="BAŞLIK",
            display_summary=summary,
            speaker_sequence=[
                SpeakerSegment(role=SpeakerRole.REPORTER, purpose=SegmentPurpose.REPORT, text=text)
            ],
        )

    def test_repeated_loaded_wording_is_flagged(self):
        script = BroadcastScript(
            episode_id="ep",
            stories=[
                self._story("ch01", "Tahran rejimi açıklama yaptı."),
                self._story("ch02", "İran rejimi kararı reddetti.", order=2),
            ],
        )
        findings = check_neutral_language(script, _FindingFactory())
        assert [f.category for f in findings] == ["loaded_language_repeated"]

    def test_single_use_is_not_flagged(self):
        script = BroadcastScript(
            episode_id="ep", stories=[self._story("ch01", "Tahran yönetimi açıklama yaptı.")]
        )
        assert check_neutral_language(script, _FindingFactory()) == []

    def test_lower_third_repeating_the_first_sentence_is_flagged(self):
        sentence = "Sekiz sanığa terör ve saldırı suçlamalarıyla hapis cezası verildi."
        script = BroadcastScript(
            episode_id="ep", stories=[self._story("ch01", sentence, summary=sentence)]
        )
        findings = check_lower_thirds(script, _FindingFactory())
        assert [f.category for f in findings] == ["lower_third_duplicates_first_sentence"]

    def test_a_complementary_lower_third_is_accepted(self):
        script = BroadcastScript(
            episode_id="ep",
            stories=[
                self._story(
                    "ch01",
                    "Hamburg Eyalet Mahkemesi bugün kararını açıkladı ve sanıklar "
                    "tutuklu yargılanmaya devam edecek.",
                    summary="Sekiz sanığa terör suçlamasıyla ceza verildi.",
                )
            ],
        )
        assert check_lower_thirds(script, _FindingFactory()) == []


class TestClosingCard:
    def _script(self):
        return BroadcastScript(
            episode_id="ep",
            stories=[
                ScriptStory(
                    story_id="ch99",
                    source_story_id="__closing",
                    order=1,
                    priority=StoryPriority.NORMAL,
                    category="closing",
                    display_headline="ALMANYA24",
                    display_summary="Almanya'nın nabzı burada atıyor.",
                    speaker_sequence=[
                        SpeakerSegment(
                            role=SpeakerRole.ANCHOR,
                            purpose=SegmentPurpose.CLOSING,
                            text=(
                                "Bugünün gündemi bu kadar. Bizi izlediğiniz için "
                                "teşekkür ederiz. Yeniden görüşmek üzere, iyi akşamlar."
                            ),
                        )
                    ],
                )
            ],
        )

    def test_card_repeating_the_spoken_thanks_is_flagged(self):
        findings = check_closing_card(
            self._script(),
            _FindingFactory(),
            "ALMANYA24 — Bizi izlediğiniz için teşekkürler",
        )
        assert [f.category for f in findings] == ["redundant_closing_card_text"]

    def test_a_slogan_card_is_accepted(self):
        findings = check_closing_card(
            self._script(),
            _FindingFactory(),
            "ALMANYA24 — Almanya'nın nabzı burada atıyor.",
        )
        assert findings == []


class TestDurationJustification:
    def test_a_short_episode_records_why(self, source_stories):
        script, _, _ = _script(source_stories)
        config = ScriptQAConfig.from_stage_config({"editorial": {"minimum_duration_seconds": 900}})
        assessment = describe_duration(script, config, 400.0, [])
        assert assessment["verdict"] == "below_minimum"
        assert "dolgu" in assessment["justification"]
        assert assessment["broadcast_story_count"] == broadcast_story_count(script)

    def test_a_long_clean_episode_is_justified_by_content(self, source_stories):
        script, _, _ = _script(source_stories)
        config = ScriptQAConfig.from_stage_config(
            {"editorial": {"soft_maximum_duration_seconds": 720}}
        )
        assessment = describe_duration(script, config, 800.0, [])
        assert assessment["verdict"] == "longer_than_preferred"
        assert "gerekçeli" in assessment["justification"]


class TestProfileGuarantees:
    def test_the_profile_never_publishes_automatically(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        assert profile.auto_publish is False

    def test_the_topic_card_hides_the_technical_counter(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        render = profile.stage_config["render"]
        assert render["topic_intro_show_counter"] is False
        assert "teşekkür" not in render["outro_text"]

    def test_the_editorial_band_is_configured(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        editorial = profile.stage_config["script"]["editorial"]
        assert editorial["minimum_duration_seconds"] == 540
        assert editorial["hard_maximum_duration_seconds"] is None


class TestWeatherAndPromptContract:
    def _prompt(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        return (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(encoding="utf-8")

    def test_the_handover_leads_into_the_weather(self):
        from btcedu.core.scripter import DEFAULT_WEATHER_HANDOVERS

        assert DEFAULT_WEATHER_HANDOVERS
        for variant in DEFAULT_WEATHER_HANDOVERS:
            assert "hava" in variant.lower()

    def test_the_prompt_asks_for_tonight_tomorrow_outlook(self):
        prompt = self._prompt()
        assert "Bu gece" in prompt
        assert "Yarın" in prompt
        assert "Sonraki günler" in prompt
        assert prompt.index("Bu gece") < prompt.index("Sonraki günler")

    def test_the_prompt_no_longer_orders_the_model_to_fill_the_airtime(self):
        prompt = self._prompt()
        assert "süreyi doldur" not in prompt
        assert "Süre bir sonuçtur" in prompt

    def test_the_prompt_bans_reading_the_screen_title(self):
        assert "Ekran başlığını okuma" in self._prompt()

    def test_the_prompt_asks_for_neutral_wording(self):
        prompt = self._prompt()
        assert "TARAFSIZ DİL" in prompt
        assert "İran yönetimi" in prompt


class TestAnchorAnalysisLength:
    def test_a_long_analysis_is_flagged_but_a_short_one_is_not(self):
        def _story(words):
            return ScriptStory(
                story_id="ch01",
                source_story_id="s01",
                order=1,
                priority=StoryPriority.TOP,
                category="politik",
                display_headline="BAŞLIK",
                display_summary="Özet.",
                speaker_sequence=[
                    SpeakerSegment(
                        role=SpeakerRole.ANCHOR,
                        purpose=SegmentPurpose.ANALYSIS,
                        text="Leipzig " + " ".join(f"kelime{n}" for n in range(words)),
                    )
                ],
            )

        long_script = BroadcastScript(episode_id="ep", stories=[_story(90)])
        short_script = BroadcastScript(episode_id="ep", stories=[_story(25)])
        assert any(
            f.category == "anchor_analysis_too_abstract"
            for f in check_segment_lengths(long_script, _FindingFactory())
        )
        assert check_segment_lengths(short_script, _FindingFactory()) == []


class TestNoAggressiveTrimming:
    def test_the_editorial_band_keeps_more_stories_than_the_old_hard_box(self, source_stories):
        editorial = ScriptQAConfig.from_stage_config(
            {
                "editorial": {
                    "minimum_duration_seconds": 540,
                    "preferred_duration_seconds": 600,
                    "soft_maximum_duration_seconds": 720,
                }
            }
        )
        wide = rank_stories(
            source_stories,
            budget=RankingBudget(
                target_seconds=editorial.preferred_duration_seconds,
                min_seconds=editorial.editorial_minimum_seconds,
                max_seconds=editorial.soft_maximum_seconds,
            ),
        )
        narrow = rank_stories(
            source_stories,
            budget=RankingBudget(target_seconds=540, min_seconds=480, max_seconds=630),
        )
        kept = sum(1 for r in wide if r.priority != StoryPriority.OMIT)
        old_kept = sum(1 for r in narrow if r.priority != StoryPriority.OMIT)
        assert kept >= old_kept


class TestLegacyPipelineUnaffected:
    def test_the_bitcoin_profile_has_no_broadcast_script(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("bitcoin_podcast")
        assert not (profile.stage_config.get("script") or {}).get("enabled", False)
        assert not (profile.branding or {}).get("spoken_name")

    def test_a_profile_without_the_editorial_block_keeps_the_hard_limits(self):
        config = ScriptQAConfig.from_stage_config({})
        assert config.editorial_duration_mode is False
        assert config.min_total_seconds == 480.0
        assert config.max_total_seconds == 630.0


class TestAcceptedDurationRange:
    def _config(self):
        return ScriptQAConfig.from_stage_config(
            {
                "editorial": {
                    "minimum_duration_seconds": 540,
                    "preferred_duration_seconds": 600,
                    "soft_maximum_duration_seconds": 720,
                }
            }
        )

    @pytest.mark.parametrize("seconds", [545.0, 600.0, 700.0, 719.0])
    def test_nine_to_twelve_minutes_is_accepted_without_a_finding(self, seconds):
        findings, revision, reasons = check_balance_and_duration(
            TestEditorialDuration()._script_with_share(),
            self._config(),
            _FindingFactory(),
            actual_duration_seconds=seconds,
            redundancy_detected=False,
        )
        assert revision is False
        assert _duration_reasons(reasons) == []
        assert [f for f in findings if "duration" in f.category or "episode_" in f.category] == []

    def test_an_overlong_reporter_block_counts_as_padding(self):
        story = ScriptStory(
            story_id="ch01",
            source_story_id="s01",
            order=1,
            priority=StoryPriority.BRIEF,
            category="politik",
            display_headline="BAŞLIK",
            display_summary="Özet cümlesi burada.",
            speaker_sequence=[
                SpeakerSegment(
                    role=SpeakerRole.ANCHOR,
                    purpose=SegmentPurpose.INTRODUCTION,
                    text=" ".join(f"a{n}" for n in range(60)),
                ),
                SpeakerSegment(
                    role=SpeakerRole.REPORTER,
                    purpose=SegmentPurpose.BRIEF,
                    text=" ".join(f"r{n}" for n in range(120)),
                ),
            ],
        )
        script = BroadcastScript(episode_id="ep", stories=[story])
        result = run_script_qa(script, {"s01": "kaynak"}, ["s01"], self._config(), 900.0)
        assert "episode_overlong_due_to_redundancy" in result.revision_reasons


class TestHedgingAndStrapLength:
    def _story(self, text, summary="Kısa ve somut bir özet cümlesi."):
        return ScriptStory(
            story_id="ch01",
            source_story_id="s01",
            order=1,
            priority=StoryPriority.NORMAL,
            category="politik",
            display_headline="BAŞLIK",
            display_summary=summary,
            speaker_sequence=[
                SpeakerSegment(role=SpeakerRole.REPORTER, purpose=SegmentPurpose.REPORT, text=text)
            ],
        )

    def test_contradictory_hedging_is_flagged(self):
        script = BroadcastScript(
            episode_id="ep",
            stories=[
                self._story("Anlaşma teorik olarak resmî biçimde yürürlüğe girmiş sayılıyor.")
            ],
        )
        findings = check_hedging(script, _FindingFactory())
        assert [f.category for f in findings] == ["contradictory_hedging"]

    def test_a_clear_sentence_is_accepted(self):
        script = BroadcastScript(
            episode_id="ep",
            stories=[self._story("Anlaşma bugün resmî olarak yürürlüğe girdi.")],
        )
        assert check_hedging(script, _FindingFactory()) == []

    def test_an_overlong_strap_is_flagged(self):
        long_summary = (
            "Hamburg Eyalet Mahkemesi aşırı sağcı bir yapılanmanın sekiz üyesine terör "
            "örgütü kurmak ve saldırı hazırlığı suçlamalarıyla hapis cezası verdi."
        )
        script = BroadcastScript(
            episode_id="ep", stories=[self._story("Başka bir cümle.", summary=long_summary)]
        )
        assert any(
            f.category == "lower_third_too_long"
            for f in check_lower_thirds(script, _FindingFactory())
        )


class TestWeatherStaysDeterministic:
    def test_the_profile_never_takes_weather_data_from_outside_the_narration(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        weather = profile.stage_config["weather"]
        assert weather["extraction"]["allow_external_weather_data"] is False
        assert weather["extraction"]["provider"] == "deterministic"
        assert weather["review"]["block_on_unsupported_claim"] is True
        assert weather["fallback"]["prohibit_blank_frames"] is True

    def test_weather_visuals_are_not_routed_to_an_image_model(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        weather = profile.stage_config["weather"]
        assert weather["enabled"] is True
        assert weather["rendering"]["engine"] == "html_svg_chromium"


class TestLegacyPipelineVersionOne:
    def test_a_v1_episode_cannot_enter_the_script_stage(self, tmp_path):
        from btcedu.config import Settings
        from btcedu.core.scripter import script_enabled

        class _Episode:
            content_profile = "bitcoin_podcast"
            pipeline_version = 1

        assert script_enabled(Settings(), _Episode()) is False

    def test_the_bundled_v1_profile_still_loads(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("bitcoin_podcast")
        assert profile.name == "bitcoin_podcast"
        assert profile.prompt_namespace == "bitcoin_podcast"


class TestPromptTargetMatchesTheBand:
    def test_the_prompt_is_told_the_preferred_length_not_the_old_target(self):
        from btcedu.core.scripter import _preferred_seconds

        editorial = ScriptQAConfig.from_stage_config(
            {"target_total_seconds": 540, "editorial": {"preferred_duration_seconds": 600}}
        )
        assert _preferred_seconds(editorial) == 600.0
        assert _preferred_seconds(ScriptQAConfig.from_stage_config({})) == 540.0

    def test_the_prompt_says_the_length_is_not_a_quota(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        prompt = (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(
            encoding="utf-8"
        )
        assert "Bu bir kota değil" in prompt


def _news_script(*, closing_purpose=SegmentPurpose.ANALYSIS, transition_text="") -> BroadcastScript:
    """A one-story broadcast produced by the editorial model."""
    sequence = [
        SpeakerSegment(
            role=SpeakerRole.ANCHOR,
            purpose=SegmentPurpose.INTRODUCTION,
            text=(
                transition_text
                or "İç politikadan ekonomi gündemine geçiyoruz. Emeklilik reformu bugün "
                "yeniden Bundestag'ın önüne geldi."
            ),
        ),
        SpeakerSegment(
            role=SpeakerRole.REPORTER,
            purpose=SegmentPurpose.REPORT,
            text="Yetkililerin açıklamasına göre düzenleme 2027 yılında yürürlüğe girecek.",
        ),
    ]
    if closing_purpose is not None:
        sequence.append(
            SpeakerSegment(
                role=SpeakerRole.ANCHOR,
                purpose=closing_purpose,
                text="Bundestag'daki oylamanın sonucu önümüzdeki hafta belli olacak.",
            )
        )
    return BroadcastScript(
        episode_id="ep-x",
        generated_by="llm",
        stories=[
            ScriptStory(
                story_id="n01",
                source_story_id="s01",
                order=1,
                priority=StoryPriority.TOP,
                display_headline="EMEKLİLİK REFORMU",
                speaker_sequence=sequence,
            )
        ],
    )


class TestStoryTransitions:
    def test_an_empty_hand_over_formula_is_flagged(self):
        from btcedu.core.script_qa import check_story_transitions

        script = _news_script(transition_text="Sıradaki haberimiz Berlin'den geliyor.")
        findings = check_story_transitions(script, _FindingFactory())
        assert [f.category for f in findings] == ["empty_transition"]
        assert findings[0].severity == "major"

    def test_a_topic_naming_transition_passes(self):
        from btcedu.core.script_qa import check_story_transitions

        assert check_story_transitions(_news_script(), _FindingFactory()) == []

    def test_the_prompt_forbids_content_free_transitions(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        prompt = (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(
            encoding="utf-8"
        )
        assert "KONU GEÇİŞLERİ (ZORUNLU)" in prompt
        assert "İç politikadan ekonomi gündemine geçiyoruz." in prompt


class TestAnchorClosure:
    def test_a_story_ending_with_the_reporter_is_flagged(self):
        from btcedu.core.script_qa import check_anchor_closure

        script = _news_script(closing_purpose=None)
        findings = check_anchor_closure(script, _FindingFactory())
        assert [f.category for f in findings] == ["missing_anchor_closure"]
        assert findings[0].structural_invariant is True

    def test_an_anchor_analysis_closes_the_story(self):
        from btcedu.core.script_qa import check_anchor_closure

        assert check_anchor_closure(_news_script(), _FindingFactory()) == []

    def test_the_deterministic_fallback_is_exempt(self):
        from btcedu.core.script_qa import check_anchor_closure

        script = _news_script(closing_purpose=None)
        script.generated_by = "deterministic"
        assert check_anchor_closure(script, _FindingFactory()) == []

    def test_the_prompt_requires_the_anchor_to_take_over_again(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        prompt = (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(
            encoding="utf-8"
        )
        assert "MUHABİRDEN SONRA SUNUCU DEVRALIR" in prompt
        assert "haber muhabirle bitemez" in prompt


class TestModeratorIdentity:
    def test_the_prompt_describes_the_anchor_as_a_moderator(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        prompt = (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(
            encoding="utf-8"
        )
        assert "ana sunucu / moderatör" in prompt
        assert "MUHABİR NASIL BAŞLAR" in prompt
        assert "YAYININ KİMLİĞİ" in prompt

    def test_the_anchor_share_band_comes_from_the_profile(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        config = ScriptQAConfig.from_stage_config(profile.stage_config["script"])
        assert (config.anchor_share_min, config.anchor_share_max) == (0.35, 0.45)

    def test_the_system_half_of_the_prompt_sees_the_configured_band(self):
        from btcedu.core.prompt_registry import TEMPLATES_DIR

        prompt = (TEMPLATES_DIR / "tagesschau_tr" / "script_broadcast.md").read_text(
            encoding="utf-8"
        )
        # The placeholders must be rendered for the system half as well, or the
        # model reads a literal "{{anchor_share_min}}".
        system_half = prompt.split("# Input")[0]
        assert "{{anchor_share_min}}" in system_half


class TestProgrammeLength:
    def test_the_profile_targets_nine_to_eleven_minutes(self):
        from btcedu.config import Settings
        from btcedu.profiles import get_registry

        profile = get_registry(Settings()).get("tagesschau_tr")
        config = ScriptQAConfig.from_stage_config(profile.stage_config["script"])
        assert config.editorial_minimum_seconds == 540
        assert config.preferred_duration_seconds == 600
        assert config.soft_maximum_seconds == 660


class TestShotVariety:
    def _beat(self, index, role="reporter_male"):
        return {"beat_index": index, "role": role}

    def test_consecutive_shots_of_a_chapter_differ(self):
        from types import SimpleNamespace

        from btcedu.core.image_generator import _shot_hint

        chapter = SimpleNamespace(chapter_id="ch03")
        hints = {
            _shot_hint(chapter, self._beat(0, "anchor_female")),
            _shot_hint(chapter, self._beat(1, "reporter_male")),
            _shot_hint(chapter, self._beat(2, "anchor_female")),
        }
        assert len(hints) == 3

    def test_neighbouring_chapters_do_not_open_with_the_same_shot(self):
        from types import SimpleNamespace

        from btcedu.core.image_generator import _shot_hint

        first = _shot_hint(SimpleNamespace(chapter_id="ch01"), self._beat(0, "anchor_female"))
        second = _shot_hint(SimpleNamespace(chapter_id="ch02"), self._beat(0, "anchor_female"))
        assert first != second

    def test_every_shot_hint_keeps_the_presenters_off_screen(self):
        from types import SimpleNamespace

        from btcedu.core.image_generator import _SHOT_LADDER, _shot_hint

        for index in range(len(_SHOT_LADDER)):
            hint = _shot_hint(SimpleNamespace(chapter_id=f"ch{index:02d}"), self._beat(index))
            assert "No news studio" in hint
            assert "Framing:" in hint


class TestWeatherWithoutAForecast:
    """The 2026-08-07 failure mode: a weather chapter with nothing in it.

    The recorder cut the broadcast a minute early, so the transcript ended on
    the hand-over — "Und damit zur Wettervorhersage für morgen" — and the
    forecast itself never reached the pipeline. Every stage then behaved
    correctly on that input and still produced a weather chapter that told the
    audience nothing. The substitute closes that gap with named external data.
    """

    WITHOUT_FORECAST = (
        "Bültenimizi hava durumuyla tamamlıyoruz."
        " Bu gece için ayrıntı verilmedi."
        " Yarın, 8 Ağustos Cumartesi günü için hava tahmini bulunuyor."
        " Sonraki günlere ilişkin ayrıntı verilmedi."
    )
    WITH_FORECAST = (
        "Yarın kuzeyde bulutlu, sıcaklık 18 ile 24 derece arasında."
        " Güneyde güneşli, en yüksek 27 derece."
    )

    @staticmethod
    def _weather_story(text):
        return ScriptStory(
            story_id="w1",
            source_story_id="s07",
            order=1,
            category="wetter",
            display_headline="HAVA DURUMU",
            is_weather=True,
            speaker_sequence=[
                SpeakerSegment(role=SpeakerRole.ANCHOR, purpose=SegmentPurpose.WEATHER, text=text)
            ],
        )

    @staticmethod
    def _forecasts(day):
        from btcedu.core.weather.models import CityForecast

        return [
            CityForecast(
                city_id=cid, label_tr=label, date_iso=day, temperature_max_c=temp
            )
            for cid, label, temp in [
                ("hamburg", "Hamburg", 22),
                ("munich", "Münih", 27),
            ]
        ]

    def _frame(self, monkeypatch, text, forecasts):
        import btcedu.services.meteo_service as meteo

        class _Stub:
            source_label = "Open-Meteo / DWD ICON"

            def __init__(self, **kwargs):
                pass

            def fetch_city_forecasts(self, cities, dates):
                return forecasts

        monkeypatch.setattr(meteo, "OpenMeteoService", _Stub)
        return _frame_stories(
            [self._weather_story(text)],
            "ep-test",
            BRANDING,
            {},
            weather_config={"city_temperatures": {"enabled": True}},
            broadcast_date="2026-08-07",
        )

    def _weather_text(self, framed):
        # Framing also adds the opening and closing to this single story; only
        # the forecast body itself is under test here.
        return " ".join(
            s.text
            for story in framed
            for s in story.speaker_sequence
            if s.purpose in {SegmentPurpose.WEATHER, SegmentPurpose.WEATHER_EXTERNAL}
        )

    def test_a_real_forecast_is_never_replaced(self, monkeypatch):
        framed = self._frame(monkeypatch, self.WITH_FORECAST, self._forecasts("2026-08-08"))
        assert self._weather_text(framed) == self.WITH_FORECAST
        purposes = {s.purpose for s in framed[0].speaker_sequence}
        assert SegmentPurpose.WEATHER_EXTERNAL not in purposes

    def test_an_empty_forecast_is_filled_from_the_external_provider(self, monkeypatch):
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-08"))
        spoken = self._weather_text(framed)
        assert "Hamburg 22" in spoken
        assert "Münih 27" in spoken
        assert "ayrıntı verilmedi" not in spoken

    def test_the_substitute_names_its_source(self, monkeypatch):
        # The user allowed external data on the condition that it is declared,
        # and the profile forbids passing it off as broadcast content.
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-08"))
        spoken = self._weather_text(framed)
        assert spoken.count("Open-Meteo / DWD ICON") >= 2

    def test_the_substitute_keeps_the_day_announced_on_air(self, monkeypatch):
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-08"))
        assert "8 Ağustos Cumartesi" in self._weather_text(framed)

    def test_a_silent_provider_leaves_the_original_wording_alone(self, monkeypatch):
        # Better an honest "no details given" than an unsourced invention.
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, [])
        assert self._weather_text(framed) == self.WITHOUT_FORECAST

    def test_forecasts_for_another_day_are_not_used(self, monkeypatch):
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-09"))
        assert self._weather_text(framed) == self.WITHOUT_FORECAST

    def test_the_handover_still_opens_the_chapter(self, monkeypatch):
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-08"))
        weather = next(st for st in framed if st.is_weather)
        purposes = [s.purpose for s in weather.speaker_sequence]
        assert (
            purposes.index(SegmentPurpose.WEATHER_HANDOVER)
            < purposes.index(SegmentPurpose.WEATHER_EXTERNAL)
        )
        assert all(s.role == SpeakerRole.ANCHOR for s in weather.speaker_sequence)

    def test_the_substitute_survives_script_qa(self, monkeypatch):
        # A substitute the QA rejects would block the gate instead of saving it.
        framed = self._frame(monkeypatch, self.WITHOUT_FORECAST, self._forecasts("2026-08-08"))
        script = BroadcastScript(
            episode_id="ep-test",
            stories=framed,
            rankings=[],
            show_name=BRANDING["show_name"],
        )
        result = run_script_qa(
            script, {"s07": self.WITHOUT_FORECAST}, ["s07"], ScriptQAConfig()
        )
        blocking = [f for f in result.findings if f.severity == "error"]
        assert not [f for f in blocking if "s07" in str(f.location or "")]
