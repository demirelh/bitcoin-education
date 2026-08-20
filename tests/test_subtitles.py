"""Tests for subtitle cue construction (btcedu/core/subtitles.py)."""

from btcedu.core.subtitles import (
    MAX_CUE_SECONDS,
    MAX_LINE_CHARS,
    MIN_CUE_SECONDS,
    Cue,
    SubtitleStyle,
    TimedWord,
    align_written_to_spoken,
    build_cues,
    chapter_cues,
    shift,
    spread_words,
    to_ass,
    to_srt,
    window,
)


def _spoken(pairs):
    return [TimedWord(word, start, end) for word, start, end in pairs]


class TestAlignWrittenToSpoken:
    def test_identical_words_keep_their_own_times(self):
        spoken = _spoken([("Merhaba", 0.0, 0.5), ("dünya", 0.5, 1.0)])
        result = align_written_to_spoken("Merhaba dünya", spoken)
        assert [(w.word, w.start, w.end) for w in result] == [
            ("Merhaba", 0.0, 0.5),
            ("dünya", 0.5, 1.0),
        ]

    def test_written_number_inherits_the_whole_spoken_run(self):
        # The speech normaliser turns "1987" into four spoken words; the viewer
        # must still read the digits, timed across all four.
        spoken = _spoken(
            [
                ("Yıl", 0.0, 0.4),
                ("bin", 0.4, 0.7),
                ("dokuz", 0.7, 1.0),
                ("yüz", 1.0, 1.3),
                ("seksen", 1.3, 1.7),
                ("yedi", 1.7, 2.0),
                ("idi.", 2.0, 2.3),
            ]
        )
        result = align_written_to_spoken("Yıl 1987 idi.", spoken)
        assert [w.word for w in result] == ["Yıl", "1987", "idi."]
        assert result[1].start == 0.4
        assert result[1].end == 2.0

    def test_punctuation_and_case_do_not_break_the_match(self):
        spoken = _spoken([("bugun", 0.0, 0.5), ("haberler", 0.5, 1.2)])
        result = align_written_to_spoken("Bugün, haberler:", spoken)
        assert result[1].start == 0.5

    def test_written_word_with_no_spoken_counterpart_gets_the_seam(self):
        spoken = _spoken([("bir", 0.0, 0.4), ("iki", 0.4, 0.8)])
        result = align_written_to_spoken("bir (%) iki", spoken)
        assert len(result) == 3
        assert 0.4 <= result[1].start <= result[1].end <= 0.8

    def test_no_spoken_words_yields_nothing(self):
        assert align_written_to_spoken("bir iki", []) == []

    def test_empty_text_yields_nothing(self):
        assert align_written_to_spoken("   ", _spoken([("bir", 0.0, 0.4)])) == []


class TestSpreadWords:
    def test_words_cover_the_whole_duration(self):
        result = spread_words("bir iki üç", 3.0)
        assert result[0].start == 0.0
        assert abs(result[-1].end - 3.0) < 1e-9

    def test_longer_words_get_more_time(self):
        result = spread_words("a uzunkelime", 2.0)
        assert result[1].end - result[1].start > result[0].end - result[0].start

    def test_zero_duration_yields_nothing(self):
        assert spread_words("bir iki", 0.0) == []


class TestBuildCues:
    def test_sentence_boundary_always_ends_a_cue(self):
        words = _spoken(
            [("Kısa.", 0.0, 0.5), ("İkinci", 0.5, 1.0), ("cümle.", 1.0, 1.5)],
        )
        cues = build_cues(words)
        assert len(cues) == 2
        assert cues[0].text == "Kısa."
        assert cues[1].text == "İkinci cümle."

    def test_lines_stay_within_the_character_limit(self):
        words = [TimedWord("kelime", i * 0.4, i * 0.4 + 0.4) for i in range(20)]
        cues = build_cues(words)
        assert cues
        for cue in cues:
            assert len(cue.lines) <= 2
            for line in cue.lines:
                assert len(line) <= MAX_LINE_CHARS

    def test_a_long_run_is_split_at_a_comma(self):
        words = _spoken(
            [
                ("Berlin'de", 0.0, 0.5),
                ("bugün", 0.5, 1.0),
                ("toplanan", 1.0, 1.5),
                ("kabinenin", 1.5, 2.0),
                ("ardından,", 2.0, 2.5),
                ("hükümet", 2.5, 3.0),
                ("sözcüsünün", 3.0, 3.5),
                ("açıklamasına", 3.5, 4.0),
                ("göre", 4.0, 4.5),
                ("değişiklik", 4.5, 5.0),
                ("bekleniyor", 5.0, 5.5),
            ]
        )
        cues = build_cues(words)
        assert len(cues) > 1
        assert cues[0].text.endswith("ardından,")

    def test_no_cue_runs_longer_than_the_maximum(self):
        words = [TimedWord("söz", i * 1.5, i * 1.5 + 1.5) for i in range(10)]
        cues = build_cues(words)
        for cue in cues:
            assert cue.end - cue.start <= MAX_CUE_SECONDS + 1.0

    def test_a_short_cue_is_held_long_enough_to_read(self):
        words = _spoken([("Evet.", 0.0, 0.3)])
        cues = build_cues(words)
        assert cues[0].end - cues[0].start >= MIN_CUE_SECONDS

    def test_a_cue_never_outlives_the_next_one(self):
        words = _spoken([("Evet.", 0.0, 0.3), ("Hayır.", 0.5, 0.9)])
        cues = build_cues(words)
        assert cues[0].end <= cues[1].start

    def test_no_words_yields_no_cues(self):
        assert build_cues([]) == []


class TestChapterCues:
    def test_word_timings_are_used_when_present(self):
        cues = chapter_cues(
            "Merhaba dünya.",
            [{"w": "Merhaba", "s": 1.0, "e": 1.5}, {"w": "dünya.", "s": 1.5, "e": 2.0}],
            duration=10.0,
        )
        assert cues[0].start == 1.0

    def test_missing_timings_fall_back_to_the_duration(self):
        cues = chapter_cues("Merhaba dünya.", None, duration=4.0)
        assert cues
        assert cues[0].start == 0.0

    def test_malformed_timing_entries_are_ignored(self):
        cues = chapter_cues("Merhaba dünya.", [{"w": "Merhaba"}, "junk"], duration=4.0)
        assert cues
        assert cues[0].start == 0.0


class TestShiftAndWindow:
    def test_shift_moves_every_cue(self):
        cues = [Cue(1.0, 2.0, ["bir"])]
        moved = shift(cues, 10.0)
        assert (moved[0].start, moved[0].end) == (11.0, 12.0)
        assert cues[0].start == 1.0

    def test_window_rebases_to_the_start_of_the_shot(self):
        cues = [Cue(0.0, 1.0, ["a"]), Cue(2.0, 3.0, ["b"]), Cue(5.0, 6.0, ["c"])]
        result = window(cues, 2.0, 4.0)
        assert len(result) == 1
        assert (result[0].start, result[0].end) == (0.0, 1.0)

    def test_a_cue_straddling_the_cut_is_trimmed(self):
        cues = [Cue(1.0, 5.0, ["uzun"])]
        result = window(cues, 2.0, 4.0)
        assert (result[0].start, result[0].end) == (0.0, 2.0)


class TestSerialisation:
    def test_srt_uses_comma_milliseconds_and_numbers_from_one(self):
        text = to_srt([Cue(0.0, 1.5, ["bir"]), Cue(2.0, 3.25, ["iki", "üç"])])
        assert text.startswith("1\n00:00:00,000 --> 00:00:01,500\nbir")
        assert "2\n00:00:02,000 --> 00:00:03,250\niki\nüç" in text

    def test_empty_srt_is_empty(self):
        assert to_srt([]) == ""

    def test_ass_carries_the_style_and_escapes_the_line_break(self):
        style = SubtitleStyle(font="Roboto Condensed", font_size=48, margin_v=210)
        text = to_ass([Cue(0.0, 1.0, ["bir", "iki"])], style)
        assert "PlayResX: 1920" in text
        assert "Roboto Condensed,48," in text
        assert ",210,1" in text
        assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,bir\\Niki" in text

    def test_a_top_cue_overrides_the_alignment_inline(self):
        """Weather cards carry their numbers low in the frame."""
        text = to_ass([Cue(0.0, 1.0, ["bir"], at_top=True)])
        assert ",,{\\an8}bir" in text

    def test_a_normal_cue_carries_no_override(self):
        assert "an8" not in to_ass([Cue(0.0, 1.0, ["bir"])])

    def test_shift_and_window_keep_the_position(self):
        cue = Cue(1.0, 5.0, ["bir"], at_top=True)
        assert shift([cue], 2.0)[0].at_top is True
        assert window([cue], 2.0, 4.0)[0].at_top is True

    def test_ass_without_cues_still_has_a_usable_header(self):
        text = to_ass([])
        assert "[Events]" in text
        assert "Dialogue:" not in text
