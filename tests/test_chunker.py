"""Tests for chunking logic: size, overlap, persistence, FTS search."""

from pathlib import Path

from btcedu.config import Settings
from btcedu.core.chunker import (
    ChunkRecord,
    chunk_text,
    estimate_tokens,
    persist_chunks,
    search_chunks_fts,
    write_chunks_jsonl,
)
from btcedu.core.transcriber import chunk_episode
from btcedu.models.episode import Chunk, Episode, EpisodeStatus

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_TRANSCRIPT = (FIXTURES / "sample_transcript_de.txt").read_text()


# ── Token estimation ───────────────────────────────────────────────


class TestEstimateTokens:
    def test_basic_estimate(self):
        assert estimate_tokens("Hello world test") == 4  # 16 chars / 4

    def test_empty_string(self):
        assert estimate_tokens("") == 1  # min 1

    def test_german_text(self):
        text = "Bitcoin ist eine dezentrale Waehrung"
        assert estimate_tokens(text) == len(text) // 4


# ── Chunking behavior ─────────────────────────────────────────────


class TestChunkText:
    def test_returns_chunks(self):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500, overlap_ratio=0.15)
        assert len(chunks) > 0
        assert all(isinstance(c, ChunkRecord) for c in chunks)

    def test_chunk_ids_correct_format(self):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        for i, c in enumerate(chunks):
            assert c.chunk_id == f"ep001_{i:03d}"
            assert c.ordinal == i
            assert c.episode_id == "ep001"

    def test_chunk_sizes_within_bounds(self):
        """Each chunk should be roughly within chunk_size (allow some flex for sentence breaks)."""
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        for c in chunks:
            # Allow up to 20% overshoot for sentence-aligned breaks
            assert len(c.text) <= 1500 * 1.2, f"Chunk {c.ordinal} too large: {len(c.text)}"

    def test_overlap_exists(self):
        """Adjacent chunks should overlap (share some text)."""
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500, overlap_ratio=0.15)
        if len(chunks) >= 2:
            # Check that the end of chunk N overlaps with the start of chunk N+1
            for i in range(len(chunks) - 1):
                # The last ~15% of chunk[i] should appear in chunk[i+1]
                tail = chunks[i].text[-50:]  # last 50 chars of this chunk
                assert tail in chunks[i + 1].text or chunks[i].end_char > chunks[i + 1].start_char

    def test_covers_full_text(self):
        """All chunks together should cover the full text."""
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        assert chunks[0].start_char == 0
        assert chunks[-1].end_char == len(SAMPLE_TRANSCRIPT)

    def test_token_estimates_populated(self):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        for c in chunks:
            assert c.token_estimate > 0
            assert c.token_estimate == len(c.text) // 4 or c.token_estimate == max(
                1, len(c.text) // 4
            )

    def test_deterministic(self):
        """Same input should produce same output."""
        chunks_a = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500, overlap_ratio=0.15)
        chunks_b = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500, overlap_ratio=0.15)
        assert len(chunks_a) == len(chunks_b)
        for a, b in zip(chunks_a, chunks_b):
            assert a.chunk_id == b.chunk_id
            assert a.text == b.text
            assert a.start_char == b.start_char
            assert a.end_char == b.end_char

    def test_empty_text(self):
        chunks = chunk_text("", "ep001")
        assert chunks == []

    def test_whitespace_only(self):
        chunks = chunk_text("   \n\n  ", "ep001")
        assert chunks == []

    def test_small_text_single_chunk(self):
        chunks = chunk_text("Short text.", "ep001", chunk_size=1500)
        assert len(chunks) == 1
        assert chunks[0].text == "Short text."

    def test_custom_chunk_size(self):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=500, overlap_ratio=0.10)
        # With 500 char chunks, should produce more chunks
        assert len(chunks) > 5


# ── JSONL output ───────────────────────────────────────────────────


class TestWriteChunksJSONL:
    def test_writes_jsonl_file(self, tmp_path):
        chunks = chunk_text("Test text for chunking.", "ep001", chunk_size=1500)
        path = write_chunks_jsonl(chunks, str(tmp_path / "chunks" / "ep001"))
        assert Path(path).exists()
        assert path.endswith("chunks.jsonl")

    def test_jsonl_line_count_matches(self, tmp_path):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        path = write_chunks_jsonl(chunks, str(tmp_path / "chunks" / "ep001"))
        import json

        lines = Path(path).read_text().strip().split("\n")
        assert len(lines) == len(chunks)
        # Verify each line is valid JSON
        for line in lines:
            record = json.loads(line)
            assert "chunk_id" in record
            assert "text" in record
            assert "token_estimate" in record


# ── SQLite + FTS persistence ───────────────────────────────────────


class TestPersistChunks:
    def test_persists_to_chunks_table(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        count = persist_chunks(db_session, chunks, "ep001")
        assert count == len(chunks)
        assert db_session.query(Chunk).count() == len(chunks)

    def test_idempotent_repersist(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")
        persist_chunks(db_session, chunks, "ep001")  # re-persist
        # Should not duplicate
        assert db_session.query(Chunk).count() == len(chunks)

    def test_chunk_fields_stored(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        stored = db_session.query(Chunk).filter_by(chunk_id="ep001_000").first()
        assert stored is not None
        assert stored.episode_id == "ep001"
        assert stored.ordinal == 0
        assert stored.token_estimate > 0
        assert stored.start_char == 0
        assert stored.end_char > 0
        assert len(stored.text) > 0


class TestFTSSearch:
    def test_fts_search_finds_chunks(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        results = search_chunks_fts(db_session, "Bitcoin")
        assert len(results) > 0
        assert all("chunk_id" in r for r in results)

    def test_fts_search_by_episode(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        results = search_chunks_fts(db_session, "Bitcoin", episode_id="ep001")
        assert len(results) > 0

    def test_fts_search_no_results(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        results = search_chunks_fts(db_session, "xyzzythisisnotaword")
        assert len(results) == 0

    def test_fts_search_german_words(self, db_session):
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        results = search_chunks_fts(db_session, "Blockchain")
        assert len(results) > 0

    def test_fts_cleared_on_repersist(self, db_session):
        """Re-persisting should clear old FTS entries."""
        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=1500)
        persist_chunks(db_session, chunks, "ep001")

        # Re-persist with different content
        new_chunks = [
            ChunkRecord(
                chunk_id="ep001_000",
                episode_id="ep001",
                ordinal=0,
                text="Completely different text about Lightning.",
                token_estimate=10,
                start_char=0,
                end_char=42,
            )
        ]
        persist_chunks(db_session, new_chunks, "ep001")

        # Old content should not be findable
        results = search_chunks_fts(db_session, "Blockchain")
        assert len(results) == 0

        # New content should be findable
        results = search_chunks_fts(db_session, "Lightning")
        assert len(results) > 0


# ── chunk_episode integration ──────────────────────────────────────


class TestChunkEpisode:
    def _seed_transcribed(self, db_session, tmp_path, episode_id="ep001"):
        transcript_dir = tmp_path / "transcripts" / episode_id
        transcript_dir.mkdir(parents=True)
        transcript_path = transcript_dir / "transcript.clean.de.txt"
        transcript_path.write_text(SAMPLE_TRANSCRIPT)

        ep = Episode(
            episode_id=episode_id,
            source="youtube_rss",
            title="Test Episode",
            url=f"https://youtube.com/watch?v={episode_id}",
            status=EpisodeStatus.TRANSCRIBED,
            transcript_path=str(transcript_path),
        )
        db_session.add(ep)
        db_session.commit()
        return ep

    def test_creates_jsonl_and_persists(self, db_session, tmp_path):
        self._seed_transcribed(db_session, tmp_path)
        settings = Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            chunks_dir=str(tmp_path / "chunks"),
            chunk_size=1500,
            chunk_overlap=0.15,
        )

        count = chunk_episode(db_session, "ep001", settings)

        assert count > 0
        jsonl = tmp_path / "chunks" / "ep001" / "chunks.jsonl"
        assert jsonl.exists()
        assert db_session.query(Chunk).count() == count

    def test_updates_status_to_chunked(self, db_session, tmp_path):
        self._seed_transcribed(db_session, tmp_path)
        settings = Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            chunks_dir=str(tmp_path / "chunks"),
        )

        chunk_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert ep.status == EpisodeStatus.CHUNKED

    def test_skips_if_jsonl_exists(self, db_session, tmp_path):
        self._seed_transcribed(db_session, tmp_path)
        settings = Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            chunks_dir=str(tmp_path / "chunks"),
        )

        # Pre-create jsonl
        jsonl_dir = tmp_path / "chunks" / "ep001"
        jsonl_dir.mkdir(parents=True)
        (jsonl_dir / "chunks.jsonl").write_text('{"test": true}\n')

        # Seed some chunks in DB so count returns > 0
        from btcedu.core.chunker import chunk_text, persist_chunks

        chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001")
        persist_chunks(db_session, chunks, "ep001")

        count = chunk_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert ep.status == EpisodeStatus.CHUNKED
        assert count > 0
