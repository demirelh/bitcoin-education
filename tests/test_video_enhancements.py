"""Tests for video quality enhancements: Ken Burns, lower thirds, ticker, intro/outro, color."""



from btcedu.services.ffmpeg_service import (
    KEN_BURNS_PATTERNS,
    OverlaySpec,
    _build_animated_lower_third,
    _build_color_correction_filter,
    _build_kenburns_filter,
    _build_ticker_filters,
    create_intro_segment,
    create_outro_segment,
    create_segment,
    create_video_segment,
)

# ---------------------------------------------------------------------------
# Ken Burns Effect
# ---------------------------------------------------------------------------

class TestKenBurns:
    def test_build_kenburns_filter_zoom_in(self):
        f = _build_kenburns_filter("zoom_in", 10.0, "1920x1080", 30, 0.04)
        assert "zoompan=" in f
        assert "s=1920x1080" in f
        assert "fps=30" in f
        assert "d=300" in f  # 10s * 30fps

    def test_build_kenburns_filter_pan_left(self):
        f = _build_kenburns_filter("pan_left", 5.0, "1920x1080", 30, 0.04)
        assert "zoompan=" in f
        assert "1-on/" in f  # pan direction

    def test_build_kenburns_filter_pan_right(self):
        f = _build_kenburns_filter("pan_right", 5.0, "1920x1080", 30, 0.04)
        assert "zoompan=" in f
        assert "on/" in f

    def test_build_kenburns_filter_zoom_out(self):
        f = _build_kenburns_filter("zoom_out", 5.0, "1920x1080", 30, 0.04)
        assert "zoompan=" in f
        assert "cos" in f

    def test_build_kenburns_filter_pan_up(self):
        f = _build_kenburns_filter("pan_up", 5.0, "1920x1080", 30, 0.04)
        assert "zoompan=" in f

    def test_patterns_list(self):
        assert len(KEN_BURNS_PATTERNS) == 5
        assert "zoom_in" in KEN_BURNS_PATTERNS

    def test_create_segment_ken_burns_dry_run(self, tmp_path):
        """Ken Burns: zoompan in filter, no -loop 1 in command."""
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
            ken_burns_pattern="zoom_in",
            ken_burns_zoom_ratio=0.04,
        )

        cmd = result.ffmpeg_command
        assert "-loop" not in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "zoompan=" in fc

    def test_create_segment_no_ken_burns_by_default(self, tmp_path):
        """When ken_burns_pattern=None, use standard scale+pad with -loop 1."""
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
        )

        cmd = result.ffmpeg_command
        assert "-loop" in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "zoompan=" not in fc

    def test_video_segment_has_no_ken_burns(self, tmp_path):
        """create_video_segment does not accept ken_burns_pattern."""
        video = tmp_path / "vid.mp4"
        audio = tmp_path / "aud.mp3"
        video.write_bytes(b"mp4")
        audio.write_bytes(b"mp3")

        result = create_video_segment(
            video_path=str(video),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "zoompan=" not in fc


# ---------------------------------------------------------------------------
# Animated Lower Thirds
# ---------------------------------------------------------------------------

class TestAnimatedLowerThirds:
    def test_build_animated_lower_third_basic(self):
        overlay = OverlaySpec(
            text="Test Text",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="/tmp/font.ttf",
            position="bottom_center",
            start=2.0,
            end=7.0,
        )
        filters = _build_animated_lower_third(overlay, "/tmp/font.ttf")
        # Should have: 2 drawbox (gradient) + 1 drawbox (accent) + 1 drawtext
        assert len(filters) >= 4
        assert any("drawbox=" in f for f in filters)
        assert any("drawtext=" in f for f in filters)

    def test_animated_lower_third_slide_in_expression(self):
        overlay = OverlaySpec(
            text="Slide In",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="/tmp/font.ttf",
            position="bottom_center",
            start=1.0,
            end=5.0,
        )
        filters = _build_animated_lower_third(overlay, "/tmp/font.ttf", slide_duration=0.4)
        # At least one filter should have animated x expression
        drawtext_filters = [f for f in filters if "drawtext=" in f]
        assert any("if(lt(t-" in f for f in drawtext_filters)

    def test_animated_lower_third_two_line(self):
        overlay = OverlaySpec(
            text="Headline\\nSubtext",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="/tmp/font.ttf",
            position="bottom_center",
            start=1.0,
            end=5.0,
        )
        filters = _build_animated_lower_third(overlay, "/tmp/font.ttf")
        drawtext_filters = [f for f in filters if "drawtext=" in f]
        # Two lines -> two drawtext filters
        assert len(drawtext_filters) == 2

    def test_animated_lower_third_accent_color(self):
        overlay = OverlaySpec(
            text="Test",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="/tmp/font.ttf",
            position="bottom_center",
            start=0.0,
            end=3.0,
        )
        filters = _build_animated_lower_third(
            overlay, "/tmp/font.ttf", accent_color="#004B87"
        )
        assert any("#004B87" in f for f in filters)

    def test_create_segment_animated_lower_thirds_dry_run(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        overlay = OverlaySpec(
            text="Breaking News",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="NotoSans-Bold",
            position="bottom_center",
            start=1.0,
            end=5.0,
        )

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[overlay],
            dry_run=True,
            animated_lower_thirds=True,
            lower_third_slide_duration=0.4,
            lower_third_accent_color="#004B87",
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "drawbox=" in fc

    def test_create_segment_static_lower_thirds_when_disabled(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        overlay = OverlaySpec(
            text="Breaking News",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="NotoSans-Bold",
            position="bottom_center",
            start=1.0,
            end=5.0,
        )

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[overlay],
            dry_run=True,
            animated_lower_thirds=False,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        # Standard drawtext, no drawbox for gradient
        assert "drawtext=" in fc


# ---------------------------------------------------------------------------
# News Ticker
# ---------------------------------------------------------------------------

class TestNewsTicker:
    def test_build_ticker_filters(self):
        filters = _build_ticker_filters("Bitcoin steigt", "/tmp/font.ttf")
        assert len(filters) == 3  # separator + background + text
        assert any("drawbox=" in f for f in filters)
        assert any("drawtext=" in f and "mod(t*" in f for f in filters)

    def test_ticker_scroll_speed(self):
        filters = _build_ticker_filters("Test", "/tmp/f.ttf", speed=120)
        text_filter = [f for f in filters if "drawtext=" in f][0]
        assert "t*120" in text_filter

    def test_ticker_height(self):
        filters = _build_ticker_filters("Test", "/tmp/f.ttf", height=60)
        assert any("h-60" in f or "h-61" in f for f in filters)

    def test_create_segment_with_ticker_dry_run(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
            ticker_text="Bitcoin  |||  Ethereum",
            ticker_speed=80,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "mod(t*" in fc

    def test_no_ticker_when_disabled(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
            # ticker_text not set (None)
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "mod(t*" not in fc


# ---------------------------------------------------------------------------
# Intro/Outro Segments
# ---------------------------------------------------------------------------

class TestIntroOutro:
    def test_create_intro_segment_dry_run(self, tmp_path):
        result = create_intro_segment(
            output_path=str(tmp_path / "intro.mp4"),
            show_name="Bitcoin Haberleri",
            episode_title="Test Episode",
            episode_date="18.03.2026",
            duration=4.0,
            dry_run=True,
        )

        assert result.returncode == 0
        assert result.duration_seconds == 4.0
        cmd = result.ffmpeg_command
        assert "color=" in " ".join(cmd)
        assert "anullsrc=" in " ".join(cmd)

    def test_intro_contains_show_name(self, tmp_path):
        result = create_intro_segment(
            output_path=str(tmp_path / "intro.mp4"),
            show_name="Bitcoin Haberleri",
            episode_title="Ep1",
            episode_date="01.01.2026",
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "Bitcoin Haberleri" in fc

    def test_intro_has_staggered_timing(self, tmp_path):
        result = create_intro_segment(
            output_path=str(tmp_path / "intro.mp4"),
            show_name="Show",
            episode_title="Title",
            episode_date="Date",
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        # Staggered appearance: 0.5s, 1.0s, 1.5s
        assert "0.5" in fc
        assert "1.0" in fc
        assert "1.5" in fc

    def test_create_outro_segment_dry_run(self, tmp_path):
        result = create_outro_segment(
            output_path=str(tmp_path / "outro.mp4"),
            source_text="Kaynak: Einundzwanzig Podcast",
            duration=3.0,
            dry_run=True,
        )

        assert result.returncode == 0
        assert result.duration_seconds == 3.0
        cmd = result.ffmpeg_command
        assert "color=" in " ".join(cmd)

    def test_outro_contains_source_text(self, tmp_path):
        result = create_outro_segment(
            output_path=str(tmp_path / "outro.mp4"),
            source_text="Kaynak: Test Source",
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "Test Source" in fc

    def test_outro_contains_closing_text(self, tmp_path):
        result = create_outro_segment(
            output_path=str(tmp_path / "outro.mp4"),
            source_text="Source",
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "sonraki" in fc  # "Bir sonraki bölümde görüşürüz"

    def test_intro_with_custom_colors(self, tmp_path):
        result = create_intro_segment(
            output_path=str(tmp_path / "intro.mp4"),
            show_name="Show",
            episode_title="Title",
            episode_date="Date",
            bg_color="#112233",
            accent_color="#F7931A",
            dry_run=True,
        )

        cmd_str = " ".join(result.ffmpeg_command)
        assert "#112233" in cmd_str
        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "#F7931A" in fc


# ---------------------------------------------------------------------------
# Color Correction
# ---------------------------------------------------------------------------

class TestColorCorrection:
    def test_build_color_correction_filter(self):
        f = _build_color_correction_filter(0.85, 0.02, 0.05)
        assert "eq=saturation=0.85" in f
        assert "brightness=0.02" in f
        assert "colorbalance=" in f
        assert "bs=0.05" in f

    def test_color_correction_in_segment_dry_run(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
            color_correction=True,
            color_saturation=0.8,
            color_brightness=0.03,
            color_blue_shift=0.06,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "eq=saturation=0.8" in fc
        assert "colorbalance=" in fc

    def test_no_color_correction_by_default(self, tmp_path):
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "eq=saturation" not in fc
        assert "colorbalance" not in fc

    def test_color_correction_in_video_segment(self, tmp_path):
        video = tmp_path / "vid.mp4"
        audio = tmp_path / "aud.mp3"
        video.write_bytes(b"mp4")
        audio.write_bytes(b"mp3")

        result = create_video_segment(
            video_path=str(video),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
            color_correction=True,
        )

        fc = result.ffmpeg_command[
            result.ffmpeg_command.index("-filter_complex") + 1
        ]
        assert "eq=saturation" in fc
        assert "colorbalance=" in fc


# ---------------------------------------------------------------------------
# Combined features
# ---------------------------------------------------------------------------

class TestCombinedFeatures:
    def test_all_enhancements_together(self, tmp_path):
        """All features enabled simultaneously in a dry-run segment."""
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        overlay = OverlaySpec(
            text="Breaking News",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="NotoSans-Bold",
            position="bottom_center",
            start=1.0,
            end=5.0,
        )

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=30.0,
            overlays=[overlay],
            dry_run=True,
            ken_burns_pattern="zoom_in",
            animated_lower_thirds=True,
            ticker_text="BTC 100k  |||  ETH 5k",
            color_correction=True,
            fade_in_duration=0.5,
            fade_out_duration=0.5,
        )

        assert result.returncode == 0
        cmd = result.ffmpeg_command
        fc = cmd[cmd.index("-filter_complex") + 1]

        # Ken Burns
        assert "zoompan=" in fc
        assert "-loop" not in cmd
        # Color correction
        assert "eq=saturation" in fc
        assert "colorbalance=" in fc
        # Animated lower third
        assert "drawbox=" in fc
        # Ticker
        assert "mod(t*" in fc
        # Fades
        assert "fade=t=in" in fc

    def test_backward_compatibility_no_enhancements(self, tmp_path):
        """Without any enhancement params, filter chain matches original."""
        image = tmp_path / "img.png"
        audio = tmp_path / "aud.mp3"
        image.write_bytes(b"png")
        audio.write_bytes(b"mp3")

        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(tmp_path / "out.mp4"),
            duration=10.0,
            overlays=[],
            dry_run=True,
        )

        cmd = result.ffmpeg_command
        fc = cmd[cmd.index("-filter_complex") + 1]

        # Should have standard pipeline, no enhancement filters
        assert "-loop" in cmd
        assert "zoompan=" not in fc
        assert "eq=saturation" not in fc
        assert "drawbox=" not in fc
        assert "mod(t*" not in fc


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestConfigSettings:
    def test_new_settings_have_defaults(self):
        from btcedu.config import Settings

        s = Settings()
        assert s.render_ken_burns_enabled is False
        assert s.render_lower_thirds_animated is False
        assert s.render_ticker_enabled is False
        assert s.render_intro_enabled is False
        assert s.render_outro_enabled is False
        assert s.render_color_correction_enabled is False
        assert s.render_ken_burns_zoom_ratio == 0.04
        assert s.render_color_saturation == 0.85
        assert s.render_intro_show_name == "Bitcoin Haberleri"
        assert s.render_outro_text == "Kaynak: Einundzwanzig Podcast"


# ---------------------------------------------------------------------------
# Content hash includes enhancements
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_hash_changes_with_enhancements(self):
        from btcedu.core.renderer import _compute_render_content_hash
        from btcedu.models.chapter_schema import (
            Chapter,
            ChapterDocument,
            Narration,
            Transitions,
            Visual,
        )

        chapters_doc = ChapterDocument(
            schema_version="1.0",
            episode_id="ep001",
            title="Test Episode",
            total_chapters=1,
            estimated_duration_seconds=5,
            chapters=[
                Chapter(
                    chapter_id="ch01",
                    order=1,
                    title="Test",
                    summary="Test summary",
                    visual=Visual(
                        type="title_card",
                        description="Test",
                    ),
                    narration=Narration(
                        text="Test narration",
                        word_count=2,
                        estimated_duration_seconds=5.0,
                    ),
                    overlays=[],
                    transitions=Transitions(**{"in": "cut", "out": "cut"}),
                )
            ],
        )
        image_manifest = {"images": [{"chapter_id": "ch01", "file_path": "img.png"}]}
        tts_manifest = {
            "segments": [
                {"chapter_id": "ch01", "file_path": "audio.mp3", "duration_seconds": 5.0}
            ]
        }

        hash_without = _compute_render_content_hash(
            chapters_doc, image_manifest, tts_manifest,
        )
        hash_with = _compute_render_content_hash(
            chapters_doc, image_manifest, tts_manifest,
            enhancement_settings={"ken_burns": True},
        )

        assert hash_without != hash_with
