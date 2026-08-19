"""Tests for wiring subtitles into the render.

The cue logic itself lives in ``tests/test_subtitles.py``; what is checked here
is that the cues reach the picture and the caption track intact.
"""

from types import SimpleNamespace

from btcedu.config import Settings
from btcedu.core.renderer import (
    _chapter_subtitle_cues,
    _resolve_subtitle_style,
    _write_episode_srt,
)
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


class TestEpisodeSrt:
    def test_cues_are_moved_onto_the_finished_timeline(self, tmp_path):
        """Intro and topic cards push every chapter later; a caption track
        timed against the chapter audio alone would run early by the length of
        everything in front of it."""
        cues = _chapter_subtitle_cues(
            SimpleNamespace(chapters=[_chapter("ch01", "Merhaba dünya.")]),
            {"segments": [{"chapter_id": "ch01", "duration_seconds": 4.0, "metadata": {}}]},
        )
        timeline = [
            {"kind": "intro", "start_seconds": 0.0},
            {"kind": "chapter", "chapter_id": "ch01", "start_seconds": 12.5},
        ]
        rel = _write_episode_srt(tmp_path, timeline, cues)
        assert rel == "render/subtitles.tr.srt"
        text = (tmp_path / "subtitles.tr.srt").read_text(encoding="utf-8")
        assert "00:00:12,500 -->" in text

    def test_no_cues_writes_no_file(self, tmp_path):
        assert _write_episode_srt(tmp_path, [], {}) is None
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
    def _inputs(self, tmp_path):
        image = tmp_path / "pic.png"
        audio = tmp_path / "voice.mp3"
        subs = tmp_path / "ch01.ass"
        for path, payload in ((image, b"img"), (audio, b"aud"), (subs, b"ass")):
            path.write_bytes(payload)
        return image, audio, subs

    def _filter_complex(self, mock_ffmpeg):
        cmd = mock_ffmpeg.call_args[0][0]
        return cmd[cmd.index("-filter_complex") + 1]

    def test_create_segment_burns_the_ass_in_before_the_fade(self, tmp_path):
        """Subtitles have to fade out with the picture; drawn after the fade
        they would sit at full brightness over a black frame."""
        from unittest.mock import patch

        from btcedu.services.ffmpeg_service import create_segment

        image, audio, subs = self._inputs(tmp_path)
        with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = (0, "success")
            create_segment(
                image_path=str(image),
                audio_path=str(audio),
                output_path=str(tmp_path / "out.mp4"),
                duration=5.0,
                overlays=[],
                fade_out_duration=0.5,
                subtitle_path=str(subs),
                dry_run=False,
            )
        graph = self._filter_complex(mock_ffmpeg)
        assert f"ass=filename='{subs}'" in graph
        assert graph.index("ass=filename") < graph.index("fade=t=out")

    def test_no_subtitle_path_leaves_the_graph_untouched(self, tmp_path):
        from unittest.mock import patch

        from btcedu.services.ffmpeg_service import create_segment

        image, audio, _ = self._inputs(tmp_path)
        with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = (0, "success")
            create_segment(
                image_path=str(image),
                audio_path=str(audio),
                output_path=str(tmp_path / "out.mp4"),
                duration=5.0,
                overlays=[],
                dry_run=False,
            )
        assert "ass=filename" not in self._filter_complex(mock_ffmpeg)
