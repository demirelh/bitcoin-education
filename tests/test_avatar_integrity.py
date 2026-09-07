"""Byte-level integrity of presenter clips (WP-6A).

The anchor approval used to be bound to a manifest, and the manifest to a path.
These tests hold the line that was missing: the bytes themselves. Every trust
boundary — download, digest, approval, local render, remote packing, remote
unpacking — has to notice when the file behind an approved clip is no longer
the file that was approved, and none of them may respond by buying a new one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from btcedu.core import avatar_integrity as integrity

CLIP = b"\x1a\x45\xdf\xa3" + b"presenter" * 64


def _manifest(base: Path, *, scenes: int = 2, record_hash: bool = True) -> dict:
    anchor = base / "anchor"
    anchor.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(scenes):
        scene_id = f"sc_{index:03d}"
        payload = CLIP + scene_id.encode()
        (anchor / f"{scene_id}.mp4").write_bytes(payload)
        entries.append(
            {
                "scene_id": scene_id,
                "chapter_id": "ch_01",
                "video_path": f"anchor/{scene_id}.mp4",
                "status": "completed",
                "file_sha256": hashlib.sha256(payload).hexdigest() if record_hash else "",
            }
        )
    manifest = {"schema_version": "2.0", "scenes": entries, "segments": []}
    (anchor / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class TestTheHashItself:
    def test_it_matches_hashlib(self, tmp_path):
        target = tmp_path / "clip.mp4"
        target.write_bytes(CLIP)
        assert integrity.hash_file(target) == hashlib.sha256(CLIP).hexdigest()

    def test_it_reads_in_chunks_rather_than_whole(self, tmp_path, monkeypatch):
        """A presenter clip is tens of megabytes; it must never be one read().

        The check is behavioural rather than cosmetic: the file is handed over
        through a wrapper that records every read size, so a future refactor to
        ``path.read_bytes()`` fails here instead of on the Pi under memory
        pressure.
        """
        payload = b"x" * (3 * integrity.CHUNK_BYTES + 17)
        target = tmp_path / "big.mp4"
        target.write_bytes(payload)

        reads: list[int] = []
        real_open = open

        class _Counting:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                chunk = self._handle.read(size)
                reads.append(len(chunk))
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

        def fake_open(path, mode="rb"):
            return _Counting(real_open(path, mode))

        monkeypatch.setattr("builtins.open", fake_open)
        digest = integrity.hash_file(target)
        monkeypatch.undo()

        assert digest == hashlib.sha256(payload).hexdigest()
        assert max(reads) <= integrity.CHUNK_BYTES
        assert len(reads) >= 4


class TestVerifyingAManifest:
    def test_untouched_clips_are_intact(self, tmp_path):
        manifest = _manifest(tmp_path)
        results = integrity.verify_manifest(manifest, base_dir=tmp_path)
        assert [item.status for item in results] == [integrity.INTEGRITY_OK] * 2
        assert integrity.problems(results) == []

    def test_one_changed_byte_is_a_mismatch(self, tmp_path):
        manifest = _manifest(tmp_path)
        clip = tmp_path / "anchor" / "sc_000.mp4"
        data = bytearray(clip.read_bytes())
        data[10] ^= 0x01
        clip.write_bytes(bytes(data))

        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert [item.scene_id for item in broken] == ["sc_000"]
        assert broken[0].status == integrity.INTEGRITY_MISMATCH
        assert broken[0].remedy

    def test_a_wholly_replaced_clip_is_a_mismatch(self, tmp_path):
        manifest = _manifest(tmp_path)
        (tmp_path / "anchor" / "sc_001.mp4").write_bytes(b"a different presenter entirely")
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert [item.status for item in broken] == [integrity.INTEGRITY_MISMATCH]

    def test_identical_content_at_the_same_path_stays_valid(self, tmp_path):
        """Rewriting the same bytes is not tampering; only content counts."""
        manifest = _manifest(tmp_path)
        clip = tmp_path / "anchor" / "sc_000.mp4"
        clip.write_bytes(clip.read_bytes())
        assert integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path)) == []

    def test_identical_content_after_an_atomic_replace_stays_valid(self, tmp_path):
        manifest = _manifest(tmp_path)
        clip = tmp_path / "anchor" / "sc_000.mp4"
        staged = clip.with_suffix(".mp4.part")
        staged.write_bytes(clip.read_bytes())
        staged.replace(clip)
        assert integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path)) == []

    def test_a_deleted_clip_is_missing(self, tmp_path):
        manifest = _manifest(tmp_path)
        (tmp_path / "anchor" / "sc_000.mp4").unlink()
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert broken[0].status == integrity.INTEGRITY_MISSING

    def test_a_legacy_manifest_without_a_hash_is_not_trusted(self, tmp_path):
        manifest = _manifest(tmp_path, record_hash=False)
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert [item.status for item in broken] == [integrity.INTEGRITY_UNRECORDED] * 2
        assert "re-run" in broken[0].remedy

    def test_a_path_outside_the_episode_is_refused(self, tmp_path):
        manifest = _manifest(tmp_path)
        manifest["scenes"][0]["video_path"] = "../elsewhere/clip.mp4"
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert broken[0].status == integrity.INTEGRITY_UNSAFE

    def test_a_symlink_leaving_the_episode_is_refused(self, tmp_path):
        manifest = _manifest(tmp_path)
        outside = tmp_path.parent / "outside.mp4"
        outside.write_bytes(CLIP)
        clip = tmp_path / "anchor" / "sc_000.mp4"
        clip.unlink()
        clip.symlink_to(outside)
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        assert broken[0].status == integrity.INTEGRITY_UNSAFE

    def test_a_chapter_manifest_has_nothing_to_check(self, tmp_path):
        """The D-ID path has no scenes and must pass through untouched."""
        assert integrity.require_intact(
            {"segments": [{"chapter_id": "ch_01"}]}, base_dir=tmp_path, context="render"
        ) == []

    def test_require_intact_names_the_boundary_and_the_scene(self, tmp_path):
        manifest = _manifest(tmp_path)
        (tmp_path / "anchor" / "sc_000.mp4").write_bytes(b"tampered")
        with pytest.raises(integrity.ClipIntegrityError) as excinfo:
            integrity.require_intact(manifest, base_dir=tmp_path, context="scene render")
        assert "scene render refused" in str(excinfo.value)
        assert "sc_000" in str(excinfo.value)
        assert len(excinfo.value.problems) == 1

    def test_the_operator_view_carries_no_full_digest(self, tmp_path):
        manifest = _manifest(tmp_path)
        (tmp_path / "anchor" / "sc_000.mp4").write_bytes(b"tampered")
        broken = integrity.problems(integrity.verify_manifest(manifest, base_dir=tmp_path))
        payload = broken[0].to_dict()
        assert payload["status"] == integrity.INTEGRITY_MISMATCH
        assert payload["remedy"]
        # Shortened, so a dashboard shows a recognisable fingerprint and not a
        # wall of hex that invites copy-paste into a support ticket.
        assert len(payload["expected"]) <= 16
        assert payload["expected"] not in (broken[0].expected,)
