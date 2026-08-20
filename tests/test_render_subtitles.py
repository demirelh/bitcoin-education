"""Tests for wiring subtitles into the render.

The cue logic itself lives in ``tests/test_subtitles.py``; what is checked here
is that the cues reach the picture and the caption track intact.
"""

from types import SimpleNamespace
from unittest.mock import patch

from btcedu.config import Settings
from btcedu.core.renderer import (
    _chapter_subtitle_cues,
    _episode_cues,
    _resolve_subtitle_style,
    _write_episode_srt,
    _write_subtitled_video,
)
from btcedu.core.subtitles import Cue, SubtitleStyle
from btcedu.models.chapter_schema import Chapter, Narration, Transitions, Visual
from btcedu.services.ffmpeg_service import _build_subtitles_filter, _escape_subtitle_path


def _chapter(chapter_id: str, text: str) -> Chapter:
    return Chapter(
        chapter_id=chapter_id,
        order=1,
        title="T",
        narration=Narration(
            text=text,
            word_count=len(text.split()),
            estimated_duration_seconds=4,
        ),
        visual=Visual(type="b_roll", description="d", image_prompt="p"),
        transitions=Transitions(**{"in": "fade", "out": "fade"}),
    )


class TestSubtitleFilter:
    def test_a_colon_in_the_path_is_escaped(self):
        """An unescaped colon reads as an option separator and the render
        fails naming neither the file nor the reason."""
        assert _escape_subtitle_path("/a/b:c.ass") == "/a/b\\:c.ass"

    def test_filter_uses_libass(self):
        assert _build_subtitles_filter("/tmp/x.ass") == "ass=filename='/tmp/x.ass'"


class TestChapterCuesFromManifest:
    def _manifest(self, metadata=None):
        return {
            "segments": [
                {
                    "chapter_id": "ch01",
                    "duration_seconds": 4.0,
                    "metadata": metadata or {},
                }
            ]
        }

    def test_word_timings_from_the_manifest_are_used(self):
        doc = SimpleNamespace(chapters=[_chapter("ch01", "Merhaba dünya.")])
        cues = _chapter_subtitle_cues(
            doc,
            self._manifest(
                {
                    "word_timings": [
                        {"w": "Merhaba", "s": 1.0, "e": 1.5},
                        {"w": "dünya.", "s": 1.5, "e": 2.0},
                    ]
                }
            ),
        )
        assert cues["ch01"][0].start == 1.0

    def test_a_chapter_without_timings_still_gets_subtitles(self):
        doc = SimpleNamespace(chapters=[_chapter("ch01", "Merhaba dünya.")])
        cues = _chapter_subtitle_cues(doc, self._manifest())
        assert cues["ch01"]

    def test_a_chapter_with_no_audio_entry_is_skipped(self):
        doc = SimpleNamespace(chapters=[_chapter("ch99", "Merhaba.")])
        assert _chapter_subtitle_cues(doc, self._manifest()) == {}


class TestEpisodeCues:
    def _cues(self):
        return _chapter_subtitle_cues(
            SimpleNamespace(chapters=[_chapter("ch01", "Merhaba dünya.")]),
            {"segments": [{"chapter_id": "ch01", "duration_seconds": 4.0, "metadata": {}}]},
        )

    def test_cues_are_moved_onto_the_finished_timeline(self, tmp_path):
        """Intro and topic cards push every chapter later; cues timed against
        the chapter audio alone would run early by the length of everything in
        front of them."""
        timeline = [
            {"kind": "intro", "start_seconds": 0.0},
            {"kind": "chapter", "chapter_id": "ch01", "start_seconds": 12.5},
        ]
        cues = _episode_cues(timeline, self._cues())
        assert cues[0].start == 12.5

        rel = _write_episode_srt(tmp_path, cues)
        assert rel == "render/subtitles.tr.srt"
        assert "00:00:12,500 -->" in (tmp_path / "subtitles.tr.srt").read_text(encoding="utf-8")

    def test_a_timeline_part_that_is_not_a_chapter_contributes_nothing(self):
        timeline = [{"kind": "topic_card", "start_seconds": 0.0}]
        assert _episode_cues(timeline, self._cues()) == []

    def test_no_cues_writes_no_file(self, tmp_path):
        assert _episode_cues([], {}) == []
        assert _write_episode_srt(tmp_path, []) is None
        assert not (tmp_path / "subtitles.tr.srt").exists()


class TestSubtitleStyle:
    def test_the_style_follows_the_render_resolution(self):
        style = _resolve_subtitle_style(None, Settings(render_resolution="1280x720"))
        assert (style.play_res_x, style.play_res_y) == (1280, 720)

    def test_the_profile_overrides_single_fields_only(self):
        style = _resolve_subtitle_style({"margin_v": 210}, Settings())
        assert style.margin_v == 210
        assert style.font_size == 52

    def test_an_unknown_key_is_ignored(self):
        style = _resolve_subtitle_style({"nonsense": 1}, Settings())
        assert not hasattr(style, "nonsense")


class TestBurnIn:
    """The second version is written in one pass over the finished video, so
    the published one never pays for it."""

    def _draft(self, tmp_path):
        render_dir = tmp_path / "render"
        render_dir.mkdir()
        draft = render_dir / "draft.mp4"
        draft.write_bytes(b"video")
        return render_dir, draft

    def _cues(self):
        return [Cue(1.0, 3.0, ["Merhaba dünya."])]

    def test_a_second_file_is_written_with_the_audio_copied(self, tmp_path):
        render_dir, draft = self._draft(tmp_path)
        with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = (0, "success")
            (render_dir / "draft_subtitled.mp4").write_bytes(b"subbed")
            rel = _write_subtitled_video(
                render_dir, draft, self._cues(), SubtitleStyle(), Settings(dry_run=False)
            )
        assert rel == "render/draft_subtitled.mp4"
        assert (render_dir / "subtitles.tr.ass").exists()
        cmd = mock_ffmpeg.call_args[0][0]
        assert cmd[cmd.index("-vf") + 1].startswith("ass=filename=")
        assert cmd[cmd.index("-c:a") + 1] == "copy"
        assert str(draft) in cmd

    def test_a_failed_burn_in_does_not_lose_the_broadcast(self, tmp_path):
        """The plain video is already finished; the extra copy is not worth
        failing the stage over."""
        render_dir, draft = self._draft(tmp_path)
        with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = (1, "libass missing")
            rel = _write_subtitled_video(
                render_dir, draft, self._cues(), SubtitleStyle(), Settings(dry_run=False)
            )
        assert rel is None

    def test_without_cues_no_second_video_is_written(self, tmp_path):
        render_dir, draft = self._draft(tmp_path)
        with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_ffmpeg:
            assert _write_subtitled_video(
                render_dir, draft, [], SubtitleStyle(), Settings(dry_run=False)
            ) is None
        assert not mock_ffmpeg.called
