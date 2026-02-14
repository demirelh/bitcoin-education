import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from btcedu.models.episode import Chunk

logger = logging.getLogger(__name__)


@dataclass
class ChunkRecord:
    """A single chunk with metadata."""

    chunk_id: str
    episode_id: str
    ordinal: int
    text: str
    token_estimate: int
    start_char: int
    end_char: int

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "episode_id": self.episode_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "token_estimate": self.token_estimate,
            "start_char": self.start_char,
            "end_char": self.end_char,
        }


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for German text."""
    return max(1, len(text) // 4)


def chunk_text(
    text: str,
    episode_id: str,
    chunk_size: int = 1500,
    overlap_ratio: float = 0.15,
) -> list[ChunkRecord]:
    """Split text into overlapping chunks.

    Args:
        text: Full transcript text.
        episode_id: Episode identifier for chunk IDs.
        chunk_size: Target chunk size in characters.
        overlap_ratio: Fraction of chunk_size to overlap (0.0 - 0.5).

    Returns:
        List of ChunkRecord objects.
    """
    if not text.strip():
        return []

    overlap = int(chunk_size * overlap_ratio)
    step = chunk_size - overlap
    chunks = []
    ordinal = 0
    pos = 0

    while pos < len(text):
        end = min(pos + chunk_size, len(text))

        # Try to break at a sentence boundary (. ! ? newline) within last 20% of chunk
        if end < len(text):
            search_start = pos + int(chunk_size * 0.8)
            best_break = None
            for i in range(end, search_start, -1):
                if i < len(text) and text[i - 1] in ".!?\n":
                    best_break = i
                    break
            if best_break is not None:
                end = best_break

        chunk_text_str = text[pos:end].strip()
        if chunk_text_str:
            chunk_id = f"{episode_id}_{ordinal:03d}"
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    episode_id=episode_id,
                    ordinal=ordinal,
                    text=chunk_text_str,
                    token_estimate=estimate_tokens(chunk_text_str),
                    start_char=pos,
                    end_char=end,
                )
            )
            ordinal += 1

        # Advance by step, but if we broke at a sentence, adjust
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = pos + step
        pos = next_pos

        # If remaining text is smaller than overlap, include it in last chunk
        if pos < len(text) and (len(text) - pos) < overlap:
            remainder = text[pos:].strip()
            if remainder and chunks:
                # Extend the last chunk to include remainder
                last = chunks[-1]
                extended = text[last.start_char : len(text)].strip()
                chunks[-1] = ChunkRecord(
                    chunk_id=last.chunk_id,
                    episode_id=last.episode_id,
                    ordinal=last.ordinal,
                    text=extended,
                    token_estimate=estimate_tokens(extended),
                    start_char=last.start_char,
                    end_char=len(text),
                )
            break

    return chunks


def write_chunks_jsonl(chunks: list[ChunkRecord], output_dir: str) -> str:
    """Write chunks to a JSONL file.

    Returns:
        Path to the written JSONL file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    jsonl_path = out / "chunks.jsonl"

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")

    logger.info("Wrote %d chunks to %s", len(chunks), jsonl_path)
    return str(jsonl_path)


def persist_chunks(
    session: Session,
    chunks: list[ChunkRecord],
    episode_id: str,
) -> int:
    """Persist chunks to SQLite chunks table + FTS5 index.

    Deletes existing chunks for the episode first (idempotent).

    Returns:
        Number of chunks persisted.
    """
    # Delete existing chunks for this episode
    session.query(Chunk).filter(Chunk.episode_id == episode_id).delete()
    session.execute(
        sql_text("DELETE FROM chunks_fts WHERE episode_id = :eid"),
        {"eid": episode_id},
    )

    # Insert new chunks
    for cr in chunks:
        chunk = Chunk(
            chunk_id=cr.chunk_id,
            episode_id=cr.episode_id,
            ordinal=cr.ordinal,
            text=cr.text,
            token_estimate=cr.token_estimate,
            start_char=cr.start_char,
            end_char=cr.end_char,
        )
        session.add(chunk)

    session.flush()

    # Populate FTS index
    for cr in chunks:
        session.execute(
            sql_text(
                "INSERT INTO chunks_fts (chunk_id, episode_id, text) VALUES (:cid, :eid, :txt)"
            ),
            {"cid": cr.chunk_id, "eid": cr.episode_id, "txt": cr.text},
        )

    session.commit()
    logger.info("Persisted %d chunks for episode %s", len(chunks), episode_id)
    return len(chunks)


def search_chunks_fts(session: Session, query: str, episode_id: str | None = None) -> list[dict]:
    """Search chunks using FTS5.

    Returns:
        List of dicts with chunk_id, episode_id, snippet.
    """
    if episode_id:
        rows = session.execute(
            sql_text(
                "SELECT chunk_id, episode_id, snippet(chunks_fts, 2, '>>>', '<<<', '...', 32) "
                "FROM chunks_fts WHERE episode_id = :eid AND chunks_fts MATCH :q"
            ),
            {"eid": episode_id, "q": query},
        ).fetchall()
    else:
        rows = session.execute(
            sql_text(
                "SELECT chunk_id, episode_id, snippet(chunks_fts, 2, '>>>', '<<<', '...', 32) "
                "FROM chunks_fts WHERE chunks_fts MATCH :q"
            ),
            {"q": query},
        ).fetchall()

    return [{"chunk_id": r[0], "episode_id": r[1], "snippet": r[2]} for r in rows]
