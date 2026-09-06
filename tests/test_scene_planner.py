"""Scene planning: one shot per speaker block, decided once and written down."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.anchor_config import AnchorConfig, PresenterLook, StudioConfig
from btcedu.core.renderer import _beat_durations
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    ROLE_REPORTER,
    TEMPLATE_ANCHOR,
    TEMPLATE_ANCHOR_RETURN,
    TEMPLATE_CLOSING,
    TEMPLATE_OPENING,
    TEMPLATE_REPORTER,
    TEMPLATE_WEATHER,
    VISUAL_MODE_FULLSCREEN,
    VISUAL_MODE_STUDIO,
    anchor_scenes,
    block_durations,
    build_scenes,
    compute_input_hash,
    load_scene_plan,
    plan_scenes,
    scene_plan_path,
    scenes_from_plan,
    speaker_blocks,
)
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.presenter_assignment import PresenterAssignment

EPISODE_ID = "ep_scene_test"


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(outputs_dir=str(tmp_path / "outputs"), _env_file=None, **overrides)


def _segment(role: str, text: str, purpose: str = "") -> dict:
    return {"role": role, "text": text, "purpose": purpose}


def _chapter(
    chapter_id: str,
    segments: list[dict],
    *,
    is_weather: bool = False,
    narration: str = "narration text",
) -> dict:
    return {
        "chapter_id": chapter_id,
        "narration": {"text": narration},
        "metadata": {"speaker_segments": segments, "is_weather": is_weather},
    }


def _audio(chapter_id: str, duration: float, parts: list[dict]) -> dict:
    return {
        "chapter_id": chapter_id,
        "duration_seconds": duration,
        "metadata": {"speaker_parts": parts},
    }


def _part(role: str, duration: float, file: str) -> dict:
    return {"role": role, "duration_seconds": duration, "file": file}


# ---------------------------------------------------------------------------
# speaker_blocks
# ---------------------------------------------------------------------------


class TestSpeakerBlocks:
    def test_consecutive_segments_of_one_speaker_become_one_block(self):
        blocks = speaker_blocks(
            [
                _segment(ROLE_ANCHOR, "eins"),
                _segment(ROLE_ANCHOR, "zwei"),
                _segment(ROLE_REPORTER, "drei"),
            ]
        )

        assert [b["role"] for b in blocks] == [ROLE_ANCHOR, ROLE_REPORTER]
        assert blocks[0]["text"] == "eins zwei"
        assert blocks[0]["segment_indices"] == [0, 1]
        assert blocks[1]["segment_indices"] == [2]

    def test_speaker_returning_later_opens_a_new_block(self):
        blocks = speaker_blocks(
            [
                _segment(ROLE_ANCHOR, "a"),
                _segment(ROLE_REPORTER, "b"),
                _segment(ROLE_ANCHOR, "c"),
            ]
        )

        assert [b["role"] for b in blocks] == [ROLE_ANCHOR, ROLE_REPORTER, ROLE_ANCHOR]
        assert [b["beat_index"] for b in blocks] == [0, 1, 2]

    def test_a_chapter_with_one_speaker_still_yields_a_block(self):
        """The chapterizer drops these; the presenter's opening must not be lost."""
        blocks = speaker_blocks([_segment(ROLE_ANCHOR, "guten abend")])

        assert len(blocks) == 1
        assert blocks[0]["role"] == ROLE_ANCHOR

    def test_segments_without_a_role_are_ignored(self):
        blocks = speaker_blocks([_segment("", "x"), _segment(ROLE_ANCHOR, "y")])

        assert len(blocks) == 1
        assert blocks[0]["segment_indices"] == [1]


# ---------------------------------------------------------------------------
# block_durations
# ---------------------------------------------------------------------------


class TestBlockDurations:
    def test_blocks_add_up_to_the_chapter_length(self):
        blocks = speaker_blocks(
            [_segment(ROLE_ANCHOR, "a b c"), _segment(ROLE_REPORTER, "d e")]
        )
        parts = [_part(ROLE_ANCHOR, 6.0, "p0.mp3"), _part(ROLE_REPORTER, 4.0, "p1.mp3")]

        durations = block_durations(blocks, parts, 12.0)

        assert sum(durations) == pytest.approx(12.0, abs=0.01)
        assert durations[0] == pytest.approx(7.2, abs=0.01)

    def test_planner_and_renderer_agree_on_block_length(self):
        """A disagreement here would drift the avatar clip against the picture."""
        blocks = speaker_blocks(
            [
                _segment(ROLE_ANCHOR, "eins zwei"),
                _segment(ROLE_REPORTER, "drei vier fünf"),
                _segment(ROLE_ANCHOR, "sechs"),
            ]
        )
        parts = [
            _part(ROLE_ANCHOR, 3.0, "p0.mp3"),
            _part(ROLE_REPORTER, 9.0, "p1.mp3"),
            _part(ROLE_ANCHOR, 2.0, "p2.mp3"),
        ]

        planned = block_durations(blocks, parts, 15.0)
        rendered = _beat_durations(blocks, parts, 15.0)

        assert planned == pytest.approx(rendered, abs=0.002)

    def test_word_counts_stand_in_for_missing_audio(self):
        blocks = speaker_blocks(
            [_segment(ROLE_ANCHOR, "a b c d"), _segment(ROLE_REPORTER, "e f g h")]
        )

        durations = block_durations(blocks, [], 10.0)

        assert durations[0] == pytest.approx(5.0, abs=0.01)
        assert sum(durations) == pytest.approx(10.0, abs=0.01)

    def test_no_blocks_means_no_durations(self):
        assert block_durations([], [], 10.0) == []


# ---------------------------------------------------------------------------
# build_scenes / templates
# ---------------------------------------------------------------------------


def _three_chapter_episode():
    chapters = [
        _chapter("ch01", [_segment(ROLE_ANCHOR, "iyi aksamlar")]),
        _chapter(
            "ch02",
            [
                _segment(ROLE_ANCHOR, "ilk haber"),
                _segment(ROLE_REPORTER, "detaylar"),
                _segment(ROLE_ANCHOR, "tesekkurler"),
            ],
        ),
        _chapter("ch03", [_segment(ROLE_ANCHOR, "iyi geceler")]),
    ]
    audio = {
        "ch01": _audio("ch01", 8.0, [_part(ROLE_ANCHOR, 8.0, "ch01_p0.mp3")]),
        "ch02": _audio(
            "ch02",
            30.0,
            [
                _part(ROLE_ANCHOR, 10.0, "ch02_p0.mp3"),
                _part(ROLE_REPORTER, 15.0, "ch02_p1.mp3"),
                _part(ROLE_ANCHOR, 5.0, "ch02_p2.mp3"),
            ],
        ),
        "ch03": _audio("ch03", 6.0, [_part(ROLE_ANCHOR, 6.0, "ch03_p0.mp3")]),
    }
    images = {
        "images": [
            {
                "chapter_id": "ch02",
                "file_path": "images/ch02_b0.png",
                "metadata": {"beat_index": 0},
            },
            {
                "chapter_id": "ch02",
                "file_path": "images/ch02_b1.png",
                "metadata": {"beat_index": 1},
            },
        ]
    }
    return chapters, audio, images


class TestTemplates:
    def test_the_bulletin_opens_and_closes_with_the_presenter(self):
        chapters, audio, images = _three_chapter_episode()

        scenes = build_scenes(chapters, audio, images)

        assert scenes[0].template_id == TEMPLATE_OPENING
        assert scenes[0].speaker_role == ROLE_ANCHOR
        assert scenes[-1].template_id == TEMPLATE_CLOSING
        assert scenes[-1].speaker_role == ROLE_ANCHOR

    def test_a_mixed_chapter_alternates_studio_and_fullscreen(self):
        chapters, audio, images = _three_chapter_episode()

        scenes = [s for s in build_scenes(chapters, audio, images) if s.chapter_id == "ch02"]

        assert [s.template_id for s in scenes] == [
            TEMPLATE_ANCHOR,
            TEMPLATE_REPORTER,
            TEMPLATE_ANCHOR_RETURN,
        ]
        assert [s.visual_mode for s in scenes] == [
            VISUAL_MODE_STUDIO,
            VISUAL_MODE_FULLSCREEN,
            VISUAL_MODE_STUDIO,
        ]

    def test_the_reporter_never_gets_a_studio_display_zone(self):
        chapters, audio, images = _three_chapter_episode()

        for scene in build_scenes(chapters, audio, images):
            if scene.speaker_role == ROLE_REPORTER:
                assert scene.display_zone_id is None
                assert scene.needs_avatar is False

    def test_a_weather_chapter_hands_over_rather_than_being_generated(self):
        chapters = [
            _chapter("ch01", [_segment(ROLE_ANCHOR, "merhaba")]),
            _chapter("ch09", [_segment(ROLE_ANCHOR, "hava durumu")], is_weather=True),
            _chapter("ch10", [_segment(ROLE_ANCHOR, "iyi geceler")]),
        ]
        audio = {
            "ch09": _audio("ch09", 20.0, [_part(ROLE_ANCHOR, 20.0, "ch09_p0.mp3")]),
        }

        scenes = build_scenes(chapters, audio, {"images": []})
        weather = next(s for s in scenes if s.chapter_id == "ch09")

        assert weather.template_id == TEMPLATE_WEATHER
        assert weather.visual_mode == VISUAL_MODE_STUDIO

    def test_every_block_gets_the_picture_made_for_it(self):
        chapters, audio, images = _three_chapter_episode()

        scenes = [s for s in build_scenes(chapters, audio, images) if s.chapter_id == "ch02"]

        assert scenes[0].background_asset == "images/ch02_b0.png"
        assert scenes[1].background_asset == "images/ch02_b1.png"
        # Third block falls back to the last available picture rather than none.
        assert scenes[2].background_asset == "images/ch02_b1.png"

    def test_a_failed_picture_is_never_planned_behind_a_block(self):
        chapters, audio, _ = _three_chapter_episode()
        images = {
            "images": [
                {
                    "chapter_id": "ch02",
                    "file_path": "images/ch02_failed.png",
                    "generation_method": "failed",
                    "metadata": {"beat_index": 0},
                }
            ]
        }

        scenes = build_scenes(chapters, audio, images)

        assert all(s.background_asset != "images/ch02_failed.png" for s in scenes)

    def test_a_chapter_without_speaker_information_is_still_covered(self):
        chapters = [_chapter("ch01", [], narration="tek parca")]
        audio = {"ch01": _audio("ch01", 12.0, [])}

        scenes = build_scenes(chapters, audio, {"images": []})

        assert len(scenes) == 1
        assert scenes[0].speaker_role == ROLE_ANCHOR
        assert scenes[0].expected_duration_seconds == pytest.approx(12.0)

    def test_a_single_part_block_names_its_audio_file(self):
        chapters, audio, images = _three_chapter_episode()

        scenes = build_scenes(chapters, audio, images)

        assert scenes[0].audio_file == "tts/parts/ch01_p0.mp3"

    def test_a_multi_part_block_invents_no_audio_file(self):
        chapters = [
            _chapter(
                "ch01",
                [_segment(ROLE_ANCHOR, "eins"), _segment(ROLE_ANCHOR, "zwei")],
            )
        ]
        audio = {
            "ch01": _audio(
                "ch01",
                10.0,
                [_part(ROLE_ANCHOR, 5.0, "a.mp3"), _part(ROLE_ANCHOR, 5.0, "b.mp3")],
            )
        }

        scenes = build_scenes(chapters, audio, {"images": []})

        assert len(scenes) == 1
        assert scenes[0].audio_file is None


# ---------------------------------------------------------------------------
# anchor_scenes — the gate that keeps the reporter away from the provider
# ---------------------------------------------------------------------------


class TestAnchorScenes:
    def test_the_reporter_never_reaches_the_avatar_provider(self):
        chapters, audio, images = _three_chapter_episode()

        anchors = anchor_scenes(build_scenes(chapters, audio, images))

        assert anchors
        assert all(s.speaker_role == ROLE_ANCHOR for s in anchors)
        assert all(s.needs_avatar for s in anchors)

    def test_a_silent_presenter_block_is_not_sent(self):
        chapters = [_chapter("ch01", [_segment(ROLE_ANCHOR, "merhaba")])]
        audio = {"ch01": _audio("ch01", 0.0, [_part(ROLE_ANCHOR, 0.0, "a.mp3")])}

        anchors = anchor_scenes(build_scenes(chapters, audio, {"images": []}))

        assert anchors == []


# ---------------------------------------------------------------------------
# input hash
# ---------------------------------------------------------------------------


class TestInputHash:
    def test_the_same_inputs_hash_the_same(self):
        chapters, audio, images = _three_chapter_episode()

        first = compute_input_hash({"chapters": chapters}, audio, images, "look-a")
        second = compute_input_hash({"chapters": chapters}, audio, images, "look-a")

        assert first == second

    def test_a_different_outfit_invalidates_the_plan(self):
        chapters, audio, images = _three_chapter_episode()

        assert compute_input_hash(
            {"chapters": chapters}, audio, images, "look-a"
        ) != compute_input_hash({"chapters": chapters}, audio, images, "look-b")

    def test_a_replaced_picture_invalidates_the_plan(self):
        chapters, audio, images = _three_chapter_episode()
        changed = json.loads(json.dumps(images))
        changed["images"][0]["file_path"] = "images/ch02_b0_v2.png"

        assert compute_input_hash(
            {"chapters": chapters}, audio, images, ""
        ) != compute_input_hash({"chapters": chapters}, audio, changed, "")

    def test_a_failed_picture_does_not_move_the_hash(self):
        chapters, audio, images = _three_chapter_episode()
        with_failure = json.loads(json.dumps(images))
        with_failure["images"].append(
            {
                "chapter_id": "ch02",
                "file_path": "images/ch02_failed.png",
                "generation_method": "failed",
                "metadata": {"beat_index": 5},
            }
        )

        assert compute_input_hash(
            {"chapters": chapters}, audio, images, ""
        ) == compute_input_hash({"chapters": chapters}, audio, with_failure, "")


# ---------------------------------------------------------------------------
# plan_scenes — the stage
# ---------------------------------------------------------------------------


def _prepare_episode(db_session, tmp_path, status=EpisodeStatus.TTS_DONE) -> Settings:
    episode = Episode(
        episode_id=EPISODE_ID,
        source="youtube_rss",
        title="Scene plan test",
        url="https://youtube.com/watch?v=scene",
        status=status,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    chapters, audio, images = _three_chapter_episode()
    episode_dir = tmp_path / "outputs" / EPISODE_ID
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "chapters.json").write_text(
        json.dumps({"chapters": chapters}), encoding="utf-8"
    )
    (episode_dir / "tts").mkdir(parents=True, exist_ok=True)
    (episode_dir / "tts" / "manifest.json").write_text(
        json.dumps({"segments": list(audio.values())}), encoding="utf-8"
    )
    (episode_dir / "images").mkdir(parents=True, exist_ok=True)
    (episode_dir / "images" / "manifest.json").write_text(
        json.dumps(images), encoding="utf-8"
    )
    return _settings(tmp_path)


class TestPlanScenesStage:
    def test_it_writes_a_plan_and_advances_the_episode(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)

        result = plan_scenes(db_session, EPISODE_ID, settings)

        assert result.skipped is False
        assert result.scene_count == 5
        assert result.plan_path.exists()

        episode = db_session.query(Episode).filter_by(episode_id=EPISODE_ID).first()
        assert episode.status == EpisodeStatus.SCENE_PLANNED

        document = load_scene_plan(result.plan_path)
        assert document["schema_version"] == 1
        assert len(scenes_from_plan(document)) == 5

    def test_it_costs_nothing_and_needs_no_avatar(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        assert settings.anchor_enabled is False

        result = plan_scenes(db_session, EPISODE_ID, settings)

        document = load_scene_plan(result.plan_path)
        assert document["presenter_look_id"] == ""
        assert document["presenter_assignment_id"] is None
        # The plan still knows which blocks would need her.
        assert document["anchor_scene_count"] == 4

    def test_a_second_run_changes_nothing(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)

        first = plan_scenes(db_session, EPISODE_ID, settings)
        first_hash = load_scene_plan(first.plan_path)["content_hash"]
        second = plan_scenes(db_session, EPISODE_ID, settings)

        assert second.skipped is True
        assert second.scene_count == first.scene_count
        assert load_scene_plan(second.plan_path)["content_hash"] == first_hash

    def test_force_rebuilds_even_when_current(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        plan_scenes(db_session, EPISODE_ID, settings)

        again = plan_scenes(db_session, EPISODE_ID, settings, force=True)

        assert again.skipped is False

    def test_a_changed_picture_forces_a_new_plan(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        plan_scenes(db_session, EPISODE_ID, settings)

        manifest = tmp_path / "outputs" / EPISODE_ID / "images" / "manifest.json"
        images = json.loads(manifest.read_text(encoding="utf-8"))
        images["images"][0]["file_path"] = "images/ch02_b0_v2.png"
        manifest.write_text(json.dumps(images), encoding="utf-8")

        again = plan_scenes(db_session, EPISODE_ID, settings)

        assert again.skipped is False
        plan = load_scene_plan(again.plan_path)
        assert any(
            s["background_asset"] == "images/ch02_b0_v2.png" for s in plan["scenes"]
        )

    def test_a_stale_marker_forces_a_new_plan(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        result = plan_scenes(db_session, EPISODE_ID, settings)
        stale = result.plan_path.with_suffix(".json.stale")
        stale.write_text("{}", encoding="utf-8")

        again = plan_scenes(db_session, EPISODE_ID, settings)

        assert again.skipped is False
        assert not stale.exists()

    def test_a_new_plan_invalidates_what_was_cut_from_the_old_one(
        self, db_session, tmp_path
    ):
        settings = _prepare_episode(db_session, tmp_path)
        episode_dir = tmp_path / "outputs" / EPISODE_ID
        for relative in ("anchor/manifest.json", "render/render_manifest.json"):
            target = episode_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")

        plan_scenes(db_session, EPISODE_ID, settings)

        assert (episode_dir / "anchor" / "manifest.json.stale").exists()
        assert (episode_dir / "render" / "render_manifest.json.stale").exists()

    def test_it_leaves_a_pipeline_run_record(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)

        plan_scenes(db_session, EPISODE_ID, settings)

        run = db_session.query(PipelineRun).filter_by(stage="sceneplan").first()
        assert run is not None
        assert run.status == RunStatus.SUCCESS.value

    def test_provenance_records_the_inputs_it_was_built_from(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)

        plan_scenes(db_session, EPISODE_ID, settings)

        provenance = json.loads(
            (
                tmp_path
                / "outputs"
                / EPISODE_ID
                / "provenance"
                / "sceneplan_provenance.json"
            ).read_text(encoding="utf-8")
        )
        assert provenance["stage"] == "sceneplan"
        assert provenance["input_content_hash"]
        assert provenance["scene_count"] == 5

    def test_a_v1_episode_is_refused(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        episode = db_session.query(Episode).filter_by(episode_id=EPISODE_ID).first()
        episode.pipeline_version = 1
        db_session.commit()

        with pytest.raises(ValueError, match="v1 pipeline"):
            plan_scenes(db_session, EPISODE_ID, settings)

    def test_a_wrong_status_is_refused(self, db_session, tmp_path):
        settings = _prepare_episode(
            db_session, tmp_path, status=EpisodeStatus.CHAPTERIZED
        )

        with pytest.raises(ValueError, match="expected 'tts_done'"):
            plan_scenes(db_session, EPISODE_ID, settings)

    def test_a_missing_episode_is_refused(self, db_session, tmp_path):
        settings = _settings(tmp_path)

        with pytest.raises(ValueError, match="Episode not found"):
            plan_scenes(db_session, "nope", settings)

    def test_a_missing_tts_manifest_is_refused(self, db_session, tmp_path):
        settings = _prepare_episode(db_session, tmp_path)
        (tmp_path / "outputs" / EPISODE_ID / "tts" / "manifest.json").unlink()

        with pytest.raises(FileNotFoundError, match="TTS manifest"):
            plan_scenes(db_session, EPISODE_ID, settings)

    def test_the_plan_lands_where_downstream_stages_look_for_it(self, tmp_path):
        assert scene_plan_path(tmp_path, "ep_x").name == "scene_plan.json"
        assert scene_plan_path(tmp_path, "ep_x").parent.name == "ep_x"


class TestPlanScenesWithAvatarEnabled:
    """With the avatar on, the plan names the outfit every clip must be made in."""

    def _heygen_config(self, provider: str = "heygen") -> AnchorConfig:
        return AnchorConfig(
            provider=provider,
            engine="avatar_iii",
            source_image="",
            source_image_url="",
            avatar_id="",
            avatar_type="digital_twin",
            expression="",
            output_format="webm",
            resolution="1080p",
            aspect_ratio="16:9",
            cost_per_second_usd=0.0167,
            max_cost_usd=7.0,
            studio_mode="composite",
            max_concurrent_jobs=4,
            rotation_strategy="least_recently_used",
            looks=(
                PresenterLook(name="look_01", avatar_look_id="look-id-01", active=True),
                PresenterLook(name="look_02", avatar_look_id="look-id-02", active=True),
            ),
            studio=StudioConfig(asset_dir="assets/almanya24/studio"),
        )

    def test_the_chosen_outfit_is_written_into_the_plan(
        self, db_session, tmp_path, monkeypatch
    ):
        settings = _prepare_episode(db_session, tmp_path)
        settings.anchor_enabled = True
        monkeypatch.setattr(
            "btcedu.core.anchor_config.resolve_anchor_config",
            lambda profile, s: self._heygen_config(),
        )

        result = plan_scenes(db_session, EPISODE_ID, settings)
        document = load_scene_plan(result.plan_path)

        assert document["presenter_look_id"] in {"look-id-01", "look-id-02"}
        assert document["presenter_assignment_id"] is not None
        assert db_session.query(PresenterAssignment).count() == 1

    def test_a_rerun_keeps_the_same_outfit(self, db_session, tmp_path, monkeypatch):
        settings = _prepare_episode(db_session, tmp_path)
        settings.anchor_enabled = True
        monkeypatch.setattr(
            "btcedu.core.anchor_config.resolve_anchor_config",
            lambda profile, s: self._heygen_config(),
        )

        first = load_scene_plan(plan_scenes(db_session, EPISODE_ID, settings).plan_path)
        forced = plan_scenes(db_session, EPISODE_ID, settings, force=True)

        assert load_scene_plan(forced.plan_path)["presenter_look_id"] == (
            first["presenter_look_id"]
        )
        assert db_session.query(PresenterAssignment).count() == 1

    def test_a_non_heygen_provider_gets_no_outfit_assignment(
        self, db_session, tmp_path, monkeypatch
    ):
        """The legacy D-ID path has no look pool and must not acquire one."""
        settings = _prepare_episode(db_session, tmp_path)
        settings.anchor_enabled = True
        monkeypatch.setattr(
            "btcedu.core.anchor_config.resolve_anchor_config",
            lambda profile, s: self._heygen_config(provider="d-id"),
        )

        result = plan_scenes(db_session, EPISODE_ID, settings)

        assert load_scene_plan(result.plan_path)["presenter_look_id"] == ""
        assert db_session.query(PresenterAssignment).count() == 0
