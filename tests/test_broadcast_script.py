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
    check_repetition,
    check_segment_lengths,
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
        target = next(
            s for s in script.stories if len({x.role for x in s.speaker_sequence}) == 2
        )
        target.speaker_sequence = list(reversed(target.speaker_sequence))
        findings = check_presentation_order(script, _FindingFactory())
        assert [f.category for f in findings] == ["reporter_opens_story"]
        assert findings[0].severity == "major"


class TestVisualBeats:
    def test_a_speaker_change_creates_a_new_beat(self, source_stories):
        from btcedu.core.chapterizer import _visual_beats

        script, _, _ = _script(source_stories)
        story = next(
            s
            for s in script.stories
            if len({seg.role for seg in s.speaker_sequence}) == 2
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
            s
            for s in script.stories
            if len({seg.role for seg in s.speaker_sequence}) == 1
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
        assert headlines.strip().endswith("Ayrıntılarla başlıyoruz.")
        for word in headlines.split():
            letters = [c for c in word if c.isalpha()]
            assert not (len(letters) > 5 and all(c == c.upper() for c in letters))

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
                    text=" ".join(f"kelime{n}" for n in range(200)),
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
