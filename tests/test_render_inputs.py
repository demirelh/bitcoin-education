"""WP-8B: every render input is bound to its bytes.

The gap this closes is easy to state and was easy to walk through: until now a
picture and a narration take were identified in the render fingerprint by their
*path* and by a hash of the text that asked for them. Replacing either file
changed nothing anyone measured, so the renderer reported itself current and an
episode could be approved and published with a picture nobody reviewed.

These tests are therefore mostly about the negative: swap some bytes, and
insist that each boundary in turn refuses to cross.
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.render_input_collector import (
    build_roots,
    collect_render_inputs,
    recorded_block,
    require_episode_inputs_intact,
    roots_for_episode,
    verify_episode_inputs,
)
from btcedu.core.render_inputs import (
    INPUT_MISMATCH,
    INPUT_MISSING,
    INPUT_OK,
    INPUT_UNRECORDED,
    INPUT_UNSAFE,
    KIND_FONT,
    KIND_TOPIC_MEDIA,
    RENDER_INPUTS_KEY,
    ROOT_ASSETS,
    ROOT_EPISODE,
    ROOT_STUDIO,
    ROOT_SYSTEM,
    RenderInput,
    RenderInputError,
    RootSet,
    declared_mismatches,
    describe_file,
    hash_file,
    inputs_block,
    inputs_digest,
    is_recorded,
    problems,
    require_inputs_intact,
    safe_resolve,
    short_digest,
    summarize,
    verify_block,
)
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(outputs_dir=str(tmp_path / "outputs"), dry_run=True)


@pytest.fixture
def episode_dir(settings):
    base = Path(settings.outputs_dir) / "ep_inputs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _episode(db_session, episode_id="ep_inputs", profile="bitcoin_podcast") -> Episode:
    episode = Episode(
        episode_id=episode_id,
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.RENDERED,
        pipeline_version=2,
        content_profile=profile,
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def _image_manifest(base: Path, *, chapters=("ch01", "ch02")) -> dict:
    images = []
    for index, chapter in enumerate(chapters):
        relative = f"images/{chapter}.png"
        _write(base / relative, f"picture-{index}".encode())
        images.append(
            {
                "chapter_id": chapter,
                "file_path": relative,
                "generation_method": "template",
            }
        )
    return {"episode_id": base.name, "images": images}


def _tts_manifest(base: Path, *, chapters=("ch01", "ch02")) -> dict:
    segments = []
    for index, chapter in enumerate(chapters):
        relative = f"tts/{chapter}.mp3"
        _write(base / relative, f"narration-{index}".encode())
        part = f"{chapter}_00_anchor.mp3"
        _write(base / "tts" / part, f"part-{index}".encode())
        segments.append(
            {
                "chapter_id": chapter,
                "file_path": relative,
                "duration_seconds": 12.0,
                "metadata": {"speaker_parts": [{"file": part, "role": "anchor_female"}]},
            }
        )
    return {"episode_id": base.name, "segments": segments}


def _collect(base: Path, **kwargs):
    roots = build_roots(base)
    return collect_render_inputs(roots=roots, **kwargs)


# ---------------------------------------------------------------------------
# the primitives
# ---------------------------------------------------------------------------


class TestHashing:
    def test_the_digest_is_the_plain_sha256_of_the_file(self, tmp_path):
        import hashlib

        payload = os.urandom(4096)
        path = _write(tmp_path / "x.bin", payload)
        assert hash_file(path) == hashlib.sha256(payload).hexdigest()

    def test_a_tiny_chunk_size_produces_the_same_digest(self, tmp_path):
        """Streaming is an implementation detail, not a different hash."""
        path = _write(tmp_path / "x.bin", os.urandom(9000))
        assert hash_file(path, chunk_bytes=7) == hash_file(path)

    def test_the_file_is_never_read_whole(self, tmp_path, monkeypatch):
        """A draft.mp4 is hundreds of megabytes on a machine with 8 GB."""
        import builtins

        path = _write(tmp_path / "big.bin", b"x" * 200_000)
        sizes: list[int] = []
        real_open = builtins.open

        class _Counting:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                sizes.append(size)
                return self._handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

        def _fake_open(target, mode="r", *args, **kwargs):
            handle = real_open(target, mode, *args, **kwargs)
            if "b" in mode and str(target) == str(path):
                return _Counting(handle)
            return handle

        monkeypatch.setattr(builtins, "open", _fake_open)
        hash_file(path, chunk_bytes=1024)
        monkeypatch.undo()
        assert sizes and all(size == 1024 for size in sizes)

    def test_a_short_digest_is_recognisable_but_not_a_wall_of_hex(self):
        assert short_digest("a" * 64).endswith("aaaa")
        assert len(short_digest("a" * 64)) < 20
        assert short_digest("") == ""


class TestSafeResolution:
    def test_a_plain_relative_path_resolves(self, tmp_path):
        roots = RootSet().with_root(ROOT_EPISODE, tmp_path)
        _write(tmp_path / "images" / "a.png", b"x")
        assert safe_resolve(roots, ROOT_EPISODE, "images/a.png") == (
            tmp_path / "images" / "a.png"
        ).resolve()

    def test_dot_dot_is_refused_rather_than_normalised(self, tmp_path):
        roots = RootSet().with_root(ROOT_EPISODE, tmp_path)
        assert safe_resolve(roots, ROOT_EPISODE, "../secrets.env") is None
        assert safe_resolve(roots, ROOT_EPISODE, "images/../../secrets.env") is None

    def test_an_absolute_path_under_a_managed_root_is_refused(self, tmp_path):
        roots = RootSet().with_root(ROOT_EPISODE, tmp_path)
        assert safe_resolve(roots, ROOT_EPISODE, str(tmp_path / "a.png")) is None

    def test_a_symlink_out_of_the_root_is_caught_not_followed(self, tmp_path):
        outside = _write(tmp_path / "outside" / "real.png", b"secret")
        base = tmp_path / "episode"
        base.mkdir()
        link = base / "linked.png"
        link.symlink_to(outside)
        roots = RootSet().with_root(ROOT_EPISODE, base)
        assert safe_resolve(roots, ROOT_EPISODE, "linked.png") is None

    def test_a_symlink_inside_the_root_is_fine(self, tmp_path):
        base = tmp_path / "episode"
        target = _write(base / "images" / "real.png", b"x")
        link = base / "alias.png"
        link.symlink_to(target)
        roots = RootSet().with_root(ROOT_EPISODE, base)
        assert safe_resolve(roots, ROOT_EPISODE, "alias.png") == target.resolve()

    def test_an_unknown_root_resolves_to_nothing(self, tmp_path):
        roots = RootSet().with_root(ROOT_EPISODE, tmp_path)
        assert safe_resolve(roots, "invented", "a.png") is None

    def test_a_system_input_must_be_absolute(self, tmp_path):
        roots = RootSet()
        assert safe_resolve(roots, ROOT_SYSTEM, "relative/font.ttf") is None
        path = _write(tmp_path / "font.ttf", b"x")
        assert safe_resolve(roots, ROOT_SYSTEM, str(path)) == path.resolve()

    def test_an_empty_path_is_not_a_path(self, tmp_path):
        roots = RootSet().with_root(ROOT_EPISODE, tmp_path)
        assert safe_resolve(roots, ROOT_EPISODE, "") is None


class TestDescribingAFile:
    def test_it_records_bytes_size_type_and_provenance(self, tmp_path):
        base = tmp_path / "episode"
        _write(base / "images" / "a.png", b"picture")
        roots = build_roots(base)
        item = describe_file(
            "media:ch01",
            KIND_TOPIC_MEDIA,
            "images/a.png",
            roots=roots,
            provenance="image manifest",
            preferred_root=ROOT_EPISODE,
        )
        assert item is not None
        assert item.root == ROOT_EPISODE
        assert item.path == "images/a.png"
        assert item.sha256 == hash_file(base / "images" / "a.png")
        assert item.size_bytes == len(b"picture")
        assert item.media_type == "image/png"
        assert item.provenance == "image manifest"

    def test_a_missing_file_is_not_recorded_as_measured(self, tmp_path):
        roots = build_roots(tmp_path)
        assert (
            describe_file(
                "media:ch01",
                KIND_TOPIC_MEDIA,
                "images/gone.png",
                roots=roots,
                provenance="image manifest",
                preferred_root=ROOT_EPISODE,
            )
            is None
        )

    def test_a_directory_is_not_an_input(self, tmp_path):
        (tmp_path / "images").mkdir()
        roots = build_roots(tmp_path)
        assert (
            describe_file(
                "media:ch01",
                KIND_TOPIC_MEDIA,
                "images",
                roots=roots,
                provenance="image manifest",
                preferred_root=ROOT_EPISODE,
            )
            is None
        )

    def test_a_file_outside_every_root_becomes_a_system_input(self, tmp_path):
        font = _write(tmp_path / "fonts" / "Noto.ttf", b"font")
        roots = build_roots(tmp_path / "episode")
        item = describe_file(
            "font:overlay", KIND_FONT, font, roots=roots, provenance="render config font"
        )
        assert item is not None
        assert item.root == ROOT_SYSTEM
        assert Path(item.path).is_absolute()

    def test_a_traversing_relative_path_is_refused(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        _write(tmp_path / "secrets.env", b"KEY=1")
        roots = build_roots(base)
        assert (
            describe_file(
                "media:evil",
                KIND_TOPIC_MEDIA,
                "../secrets.env",
                roots=roots,
                provenance="image manifest",
                preferred_root=ROOT_EPISODE,
            )
            is None
        )


class TestTheSetDigest:
    def _input(self, key, sha, root=ROOT_EPISODE):
        return RenderInput(
            key=key,
            kind=KIND_TOPIC_MEDIA,
            root=root,
            path=f"images/{key}.png",
            sha256=sha,
            size_bytes=1,
            media_type="image/png",
            provenance="test",
        )

    def test_the_same_bytes_give_the_same_digest_regardless_of_order(self):
        one = [self._input("a", "1" * 64), self._input("b", "2" * 64)]
        assert inputs_digest(one) == inputs_digest(list(reversed(one)))

    def test_changed_bytes_change_the_digest(self):
        before = [self._input("a", "1" * 64)]
        after = [self._input("a", "3" * 64)]
        assert inputs_digest(before) != inputs_digest(after)

    def test_a_moved_file_with_identical_bytes_does_not_change_the_digest(self):
        """A directory rename is not a change to the video."""
        moved = self._input("a", "1" * 64)
        relocated = RenderInput(**{**moved.to_dict(), "path": "elsewhere/a.png"})
        assert inputs_digest([moved]) == inputs_digest([relocated])

    def test_a_system_font_is_recorded_but_stays_out_of_the_digest(self):
        """Otherwise every remote render disagrees with the Pi by design."""
        without = [self._input("a", "1" * 64)]
        with_font = without + [self._input("font", "9" * 64, root=ROOT_SYSTEM)]
        assert inputs_digest(without) == inputs_digest(with_font)
        block = inputs_block(with_font)
        assert block["count"] == 2
        assert any(entry["root"] == ROOT_SYSTEM for entry in block["entries"])

    def test_the_block_carries_a_schema_version_and_a_stable_order(self):
        block = inputs_block([self._input("b", "2" * 64), self._input("a", "1" * 64)])
        assert block["schema_version"]
        assert [entry["key"] for entry in block["entries"]] == ["a", "b"]


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


class TestVerification:
    def _prepared(self, tmp_path):
        base = tmp_path / "episode"
        image_manifest = _image_manifest(base)
        inputs, missing = _collect(base, image_manifest=image_manifest)
        assert not missing
        return base, inputs_block(inputs)

    def test_an_untouched_set_verifies(self, tmp_path):
        base, block = self._prepared(tmp_path)
        results = verify_block(block, roots=build_roots(base))
        assert results
        assert all(item.status == INPUT_OK for item in results)
        assert not problems(results)

    def test_a_changed_byte_is_a_mismatch(self, tmp_path):
        base, block = self._prepared(tmp_path)
        (base / "images" / "ch01.png").write_bytes(b"different picture entirely")
        results = verify_block(block, roots=build_roots(base))
        broken = problems(results)
        assert [item.status for item in broken] == [INPUT_MISMATCH]
        assert "changed on disk" in broken[0].detail
        assert broken[0].remedy

    def test_a_swapped_file_is_a_mismatch_even_at_the_same_size(self, tmp_path):
        """Same length, same name, different picture: the classic swap."""
        base, block = self._prepared(tmp_path)
        original = (base / "images" / "ch01.png").read_bytes()
        (base / "images" / "ch01.png").write_bytes(b"Y" * len(original))
        assert [item.status for item in problems(verify_block(block, roots=build_roots(base)))] == [
            INPUT_MISMATCH
        ]

    def test_a_deleted_file_is_missing(self, tmp_path):
        base, block = self._prepared(tmp_path)
        (base / "images" / "ch02.png").unlink()
        assert [item.status for item in problems(verify_block(block, roots=build_roots(base)))] == [
            INPUT_MISSING
        ]

    def test_an_entry_without_a_digest_is_unrecorded_and_refused(self, tmp_path):
        """A half-written contract is a bug, not history."""
        base, block = self._prepared(tmp_path)
        block["entries"][0]["sha256"] = ""
        assert [
            item.status for item in problems(verify_block(block, roots=build_roots(base)))
        ] == [INPUT_UNRECORDED]

    def test_a_traversing_recorded_path_is_unsafe(self, tmp_path):
        base, block = self._prepared(tmp_path)
        block["entries"][0]["path"] = "../../etc/passwd"
        assert [
            item.status for item in problems(verify_block(block, roots=build_roots(base)))
        ] == [INPUT_UNSAFE]

    def test_require_intact_raises_and_carries_the_problems(self, tmp_path):
        base, block = self._prepared(tmp_path)
        (base / "images" / "ch01.png").write_bytes(b"tampered")
        with pytest.raises(RenderInputError) as exc:
            require_inputs_intact(block, roots=build_roots(base), context="publish")
        assert "publish refused" in str(exc.value)
        assert exc.value.problems

    def test_a_legacy_manifest_has_nothing_to_verify_and_says_so(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        assert is_recorded(None) is False
        assert is_recorded({}) is False
        assert is_recorded({"entries": []}) is False
        # It passes rather than stranding every episode finished before WP-8B.
        assert require_inputs_intact(None, roots=build_roots(base), context="publish") == []

    def test_skip_roots_is_available_only_where_two_machines_meet(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        block = inputs_block(
            [
                RenderInput(
                    key="font:overlay",
                    kind=KIND_FONT,
                    root=ROOT_SYSTEM,
                    path="/nonexistent/font.ttf",
                    sha256="a" * 64,
                    size_bytes=1,
                    media_type="font/ttf",
                    provenance="render config font",
                )
            ]
        )
        assert problems(verify_block(block, roots=build_roots(base)))
        assert not problems(
            verify_block(block, roots=build_roots(base), skip_roots={ROOT_SYSTEM})
        )

    def test_a_declared_digest_that_contradicts_the_file_is_reported(self, tmp_path):
        base = tmp_path / "episode"
        _write(base / "images" / "a.png", b"picture")
        item = describe_file(
            "media:a",
            KIND_TOPIC_MEDIA,
            "images/a.png",
            roots=build_roots(base),
            provenance="studio manifest",
            declared_sha256="f" * 64,
            preferred_root=ROOT_EPISODE,
        )
        notes = declared_mismatches(inputs_block([item]))
        assert len(notes) == 1
        assert "manifest declares" in notes[0]

    def test_the_summary_counts_without_leaking_full_digests(self, tmp_path):
        base, block = self._prepared(tmp_path)
        (base / "images" / "ch01.png").write_bytes(b"tampered")
        summary = summarize(verify_block(block, roots=build_roots(base)))
        assert summary["total"] >= 2
        assert summary["counts"][INPUT_MISMATCH] == 1
        for problem in summary["problems"]:
            assert len(problem["expected"]) < 20
            assert len(problem["actual"]) < 20


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


class TestCollection:
    def test_topic_media_are_collected_and_measured(self, tmp_path):
        base = tmp_path / "episode"
        inputs, missing = _collect(base, image_manifest=_image_manifest(base))
        assert not missing
        keys = {item.key for item in inputs}
        assert keys == {"media:ch01", "media:ch02"}
        assert all(item.sha256 for item in inputs)

    def test_a_declared_but_absent_medium_is_reported_as_missing(self, tmp_path):
        base = tmp_path / "episode"
        manifest = _image_manifest(base)
        (base / "images" / "ch02.png").unlink()
        inputs, missing = _collect(base, image_manifest=manifest)
        assert len(inputs) == 1
        assert any("ch02" in note for note in missing)

    def test_narration_files_and_speaker_parts_are_both_collected(self, tmp_path):
        base = tmp_path / "episode"
        inputs, missing = _collect(base, tts_manifest=_tts_manifest(base))
        assert not missing
        keys = {item.key for item in inputs}
        assert "tts:ch01" in keys
        assert any(key.startswith("tts_part:ch01") for key in keys)

    def test_a_missing_speaker_part_is_not_a_broken_episode(self, tmp_path):
        """A chapter-only render never writes parts."""
        base = tmp_path / "episode"
        manifest = _tts_manifest(base)
        (base / "tts" / "ch01_00_anchor.mp3").unlink()
        _inputs, missing = _collect(base, tts_manifest=manifest)
        assert not missing

    def test_presenter_clips_are_part_of_the_set(self, tmp_path):
        base = tmp_path / "episode"
        _write(base / "anchor" / "scene_01.webm", b"clip")
        anchor = {
            "scenes": [
                {
                    "scene_id": "sc01",
                    "video_path": "anchor/scene_01.webm",
                    "file_sha256": hash_file(base / "anchor" / "scene_01.webm"),
                }
            ]
        }
        inputs, missing = _collect(base, anchor_manifest=anchor)
        assert not missing
        assert [item.key for item in inputs] == ["avatar:sc01"]
        assert not declared_mismatches(inputs_block(inputs))

    def test_a_missing_presenter_clip_is_reported(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        anchor = {"scenes": [{"scene_id": "sc01", "video_path": "anchor/gone.webm"}]}
        _inputs, missing = _collect(base, anchor_manifest=anchor)
        assert any("sc01" in note for note in missing)

    def test_stings_and_the_music_bed_are_collected_from_the_render_config(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        intro = _write(tmp_path / "assets" / "intro.mp3", b"sting")
        bed = _write(tmp_path / "assets" / "bed.mp3", b"bed")
        inputs, _missing = _collect(base, intro_audio=str(intro), music_bed=str(bed))
        keys = {item.key for item in inputs}
        assert {"sting:intro", "music:bed"} <= keys

    def test_a_configured_but_absent_sting_is_not_fatal(self, tmp_path):
        """The renderer already omits it; recording it as fatal would change that."""
        base = tmp_path / "episode"
        base.mkdir()
        inputs, missing = _collect(base, intro_audio=str(tmp_path / "nope.mp3"))
        assert not inputs
        assert not missing

    def test_the_overlay_font_is_measured_when_it_is_a_real_file(self, tmp_path, monkeypatch):
        base = tmp_path / "episode"
        base.mkdir()
        font = _write(tmp_path / "Noto.ttf", b"font bytes")
        monkeypatch.setattr(
            "btcedu.services.ffmpeg_service.find_font_path", lambda name: str(font)
        )
        inputs, _missing = _collect(base, font_name="NotoSans-Bold")
        entry = next(item for item in inputs if item.key == "font:overlay")
        assert entry.root == ROOT_SYSTEM
        assert entry.sha256 == hash_file(font)

    def test_a_fontconfig_name_is_recorded_by_its_absence(self, tmp_path, monkeypatch):
        """``find_font_path`` may hand a name to fontconfig; a name has no bytes."""
        base = tmp_path / "episode"
        base.mkdir()
        monkeypatch.setattr(
            "btcedu.services.ffmpeg_service.find_font_path", lambda name: "NotoSans-Bold"
        )
        inputs, missing = _collect(base, font_name="NotoSans-Bold")
        assert not any(item.key == "font:overlay" for item in inputs)
        assert not missing

    def test_studio_assets_are_measured_from_disk_not_from_their_declaration(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        studio_dir = tmp_path / "studio"
        plate = _write(studio_dir / "plates" / "wall.png", b"wall")

        class _Asset:
            def __init__(self, path, sha256=""):
                self.path = path
                self.sha256 = sha256
                self.placeholder = False

        class _Studio:
            def assets(self):
                return [_Asset("plates/wall.png", "0" * 64)]

            def asset_path(self, asset):
                return studio_dir / asset.path

        roots = build_roots(base, studio_dir=studio_dir)
        inputs, missing = collect_render_inputs(roots=roots, studio=_Studio())
        assert not missing
        entry = inputs[0]
        assert entry.root == ROOT_STUDIO
        assert entry.path == "plates/wall.png"
        assert entry.sha256 == hash_file(plate)
        assert entry.declared_sha256 == "0" * 64
        # Measured and declared disagree, and the disagreement is reported
        # rather than silently resolved in favour of either one.
        assert declared_mismatches(inputs_block(inputs))

    def test_a_placeholder_studio_asset_does_not_fail_every_render(self, tmp_path):
        base = tmp_path / "episode"
        base.mkdir()
        studio_dir = tmp_path / "studio"
        studio_dir.mkdir()

        class _Asset:
            path = "plates/missing.png"
            sha256 = ""
            placeholder = True

        class _Studio:
            def assets(self):
                return [_Asset()]

            def asset_path(self, asset):
                return studio_dir / asset.path

        roots = build_roots(base, studio_dir=studio_dir)
        _inputs, missing = collect_render_inputs(roots=roots, studio=_Studio())
        assert not missing

    def test_two_declarations_of_the_same_file_stay_one_entry(self, tmp_path):
        base = tmp_path / "episode"
        manifest = _image_manifest(base, chapters=("ch01",))
        manifest["images"].append(dict(manifest["images"][0]))
        inputs, _missing = _collect(base, image_manifest=manifest)
        assert len(inputs) == 1

    def test_an_extra_declared_asset_can_be_added_by_the_caller(self, tmp_path):
        base = tmp_path / "episode"
        _write(base / "render" / "fallback.png", b"fallback card")
        inputs, _missing = _collect(
            base, extra={"card:opening": ("render/fallback.png", "card")}
        )
        assert [item.key for item in inputs] == ["card:opening"]
        assert inputs[0].kind == "card"


# ---------------------------------------------------------------------------
# episode-level boundary helpers
# ---------------------------------------------------------------------------


class TestEpisodeBoundary:
    def _rendered(self, db_session, settings, episode_dir, *, record=True):
        episode = _episode(db_session)
        image_manifest = _image_manifest(episode_dir)
        tts_manifest = _tts_manifest(episode_dir)
        inputs, _missing = _collect(
            episode_dir, image_manifest=image_manifest, tts_manifest=tts_manifest
        )
        manifest = {"episode_id": episode.episode_id, "segments": []}
        if record:
            manifest[RENDER_INPUTS_KEY] = inputs_block(inputs)
        _write(
            episode_dir / "render" / "render_manifest.json",
            json.dumps(manifest).encode("utf-8"),
        )
        return episode

    def test_the_recorded_block_is_read_back_from_the_render_manifest(
        self, db_session, settings, episode_dir
    ):
        self._rendered(db_session, settings, episode_dir)
        block = recorded_block("ep_inputs", settings)
        assert is_recorded(block)
        assert block["digest"]

    def test_a_legacy_render_reads_back_as_nothing_recorded(
        self, db_session, settings, episode_dir
    ):
        self._rendered(db_session, settings, episode_dir, record=False)
        assert not is_recorded(recorded_block("ep_inputs", settings))

    def test_an_absent_render_manifest_is_not_a_crash(self, settings):
        assert recorded_block("never_rendered", settings) is None

    def test_verification_passes_on_an_untouched_episode(
        self, db_session, settings, episode_dir
    ):
        episode = self._rendered(db_session, settings, episode_dir)
        _block, results = verify_episode_inputs("ep_inputs", settings, episode)
        assert results and all(item.status == INPUT_OK for item in results)

    def test_verification_refuses_after_a_picture_is_swapped(
        self, db_session, settings, episode_dir
    ):
        episode = self._rendered(db_session, settings, episode_dir)
        (episode_dir / "images" / "ch01.png").write_bytes(b"a different picture")
        with pytest.raises(RenderInputError) as exc:
            require_episode_inputs_intact(
                "ep_inputs", settings, episode, context="final review"
            )
        assert "final review refused" in str(exc.value)

    def test_verification_refuses_after_a_narration_take_is_swapped(
        self, db_session, settings, episode_dir
    ):
        episode = self._rendered(db_session, settings, episode_dir)
        (episode_dir / "tts" / "ch02.mp3").write_bytes(b"a different take")
        with pytest.raises(RenderInputError):
            require_episode_inputs_intact("ep_inputs", settings, episode, context="publish")

    def test_a_legacy_episode_crosses_the_boundary_and_is_flagged_as_unchecked(
        self, db_session, settings, episode_dir
    ):
        episode = self._rendered(db_session, settings, episode_dir, record=False)
        block, results = require_episode_inputs_intact(
            "ep_inputs", settings, episode, context="publish"
        )
        assert not is_recorded(block)
        assert results == []

    def test_the_roots_of_an_episode_without_a_studio_are_still_usable(
        self, db_session, settings
    ):
        episode = _episode(db_session)
        roots = roots_for_episode("ep_inputs", settings, episode)
        assert roots.get(ROOT_EPISODE) is not None
        assert roots.get(ROOT_ASSETS) is not None
