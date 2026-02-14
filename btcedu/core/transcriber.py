"""Core logic for transcription and chunking pipeline stages."""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.chunker import chunk_text, persist_chunks, write_chunks_jsonl
from btcedu.models.episode import Chunk, Episode, EpisodeStatus

logger = logging.getLogger(__name__)


def transcribe_episode(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> str:
    """Transcribe audio for an episode via Whisper API.

    Stores both raw and cleaned transcripts under transcripts_dir/{episode_id}/.

    Returns:
        Path to the cleaned transcript file.

    Raises:
        ValueError: If episode not found or not in DOWNLOADED state.
    """
    from btcedu.services.transcription_service import clean_transcript, transcribe_audio

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.DOWNLOADED, EpisodeStatus.TRANSCRIBED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'downloaded'. Use --force to override."
        )

    transcript_dir = Path(settings.transcripts_dir) / episode_id
    raw_path = transcript_dir / "transcript.de.txt"
    clean_path = transcript_dir / "transcript.clean.de.txt"

    # Skip if already transcribed
    if clean_path.exists() and not force:
        logger.info("Transcript exists: %s (use --force to re-transcribe)", clean_path)
        if episode.status == EpisodeStatus.DOWNLOADED:
            episode.transcript_path = str(clean_path)
            episode.status = EpisodeStatus.TRANSCRIBED
            session.commit()
        return str(clean_path)

    if not episode.audio_path:
        raise ValueError(f"No audio file for episode {episode_id}")

    api_key = settings.effective_whisper_api_key
    if not api_key:
        raise ValueError("No Whisper API key configured. Set WHISPER_API_KEY or OPENAI_API_KEY.")

    # Transcribe
    raw_text = transcribe_audio(
        audio_path=episode.audio_path,
        api_key=api_key,
        model=settings.whisper_model,
        language=settings.whisper_language,
        max_chunk_mb=settings.max_audio_chunk_mb,
    )

    # Save raw transcript
    transcript_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(raw_text, encoding="utf-8")
    logger.info("Raw transcript saved: %s", raw_path)

    # Clean and save
    cleaned = clean_transcript(raw_text)
    clean_path.write_text(cleaned, encoding="utf-8")
    logger.info("Clean transcript saved: %s", clean_path)

    # Update DB
    episode.transcript_path = str(clean_path)
    episode.status = EpisodeStatus.TRANSCRIBED
    session.commit()

    return str(clean_path)


def chunk_episode(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> int:
    """Chunk a transcript and persist to DB + JSONL.

    Returns:
        Number of chunks created.

    Raises:
        ValueError: If episode not found or not in TRANSCRIBED state.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.TRANSCRIBED, EpisodeStatus.CHUNKED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'transcribed'. Use --force to override."
        )

    chunks_dir = Path(settings.chunks_dir) / episode_id
    jsonl_path = chunks_dir / "chunks.jsonl"

    # Skip if already chunked
    if jsonl_path.exists() and not force:
        logger.info("Chunks exist: %s (use --force to re-chunk)", jsonl_path)
        if episode.status == EpisodeStatus.TRANSCRIBED:
            episode.status = EpisodeStatus.CHUNKED
            session.commit()
        return session.query(Chunk).filter_by(episode_id=episode_id).count()

    if not episode.transcript_path:
        raise ValueError(f"No transcript for episode {episode_id}")

    transcript_text = Path(episode.transcript_path).read_text(encoding="utf-8")

    # Chunk the text
    chunks = chunk_text(
        text=transcript_text,
        episode_id=episode_id,
        chunk_size=settings.chunk_size,
        overlap_ratio=settings.chunk_overlap,
    )

    # Write JSONL
    write_chunks_jsonl(chunks, str(chunks_dir))

    # Persist to DB + FTS
    count = persist_chunks(session, chunks, episode_id)

    # Update episode status
    episode.status = EpisodeStatus.CHUNKED
    session.commit()

    return count
