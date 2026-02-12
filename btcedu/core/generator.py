"""Content generation orchestrator: retrieval + Claude calls + file output."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.chunker import search_chunks_fts
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Chunk,
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.services.claude_service import (
    ClaudeResponse,
    calculate_cost,
    call_claude,
    compute_prompt_hash,
)

logger = logging.getLogger(__name__)

# German stopwords to filter from search queries
DE_STOPWORDS = frozenset(
    "der die das ein eine einer eines einem einen und oder aber auch als "
    "am an auf aus bei bis durch fuer gegen im in ist mit nach nicht noch "
    "nun nur ob so ueber um und von vor waehrend wie wird zu zum zur "
    "dass den dem des doch er es hat haben ich ihr ihm ihn ihnen ihre ja "
    "kann man mich mir mehr mein meine meinem meinen meiner muss nicht "
    "schon sehr sich sie sind so sollte ueber uns unser unsere vom was "
    "weil wenn wer wir wird wohl zu".split()
)

ARTIFACT_TYPES = ("outline", "script", "shorts", "visuals", "qa", "publishing")

REFINE_ARTIFACT_TYPES = ("refine_outline", "refine_script", "refine_publishing")

ARTIFACT_FILENAMES = {
    "outline": "outline.tr.md",
    "script": "script.long.tr.md",
    "shorts": "shorts.tr.json",
    "visuals": "visuals.json",
    "qa": "qa.json",
    "publishing": "publishing_pack.json",
    "refine_outline": "outline.tr.v2.md",
    "refine_script": "script.long.tr.v2.md",
    "refine_publishing": "publishing_pack.v2.json",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class GenerationResult:
    """Summary of content generation for one episode."""

    episode_id: str
    output_dir: str
    artifacts: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


def build_query_terms(title: str) -> list[str]:
    """Extract search terms from episode title, filtering German stopwords.

    Returns:
        List of content words suitable for FTS5 OR query.
        Each term is double-quoted to prevent FTS5 from interpreting
        hyphens, brackets, or other characters as operators.
    """
    import re

    words = []
    for word in title.split():
        # Strip punctuation and brackets
        clean = word.strip(".,;:!?\"'()-/[]")
        # Split on internal hyphens (e.g. "Saylor-Kalkül" → ["Saylor", "Kalkül"])
        parts = re.split(r"[-/]", clean) if clean else []
        for part in parts:
            part = part.strip()
            if part and part.lower() not in DE_STOPWORDS and len(part) > 2:
                # Double-quote for FTS5 literal matching (safe from operator parsing)
                words.append(f'"{part}"')
    return words if words else [f'"{title.split()[0]}"'] if title.strip() else ['"Bitcoin"']


def retrieve_chunks(
    session: Session,
    episode_id: str,
    query_terms: list[str],
    top_k: int = 16,
) -> list[dict]:
    """Retrieve top_k chunks using FTS5 search + Chunk ORM for full text.

    Falls back to ordinal-based selection if FTS returns too few results.

    Returns:
        List of dicts: {chunk_id, episode_id, ordinal, text, rank}
    """
    # Build FTS5 OR query
    fts_query = " OR ".join(query_terms)
    fts_results = search_chunks_fts(session, fts_query, episode_id=episode_id)

    # Get unique chunk_ids preserving FTS rank order
    seen = set()
    ranked_ids = []
    for r in fts_results:
        cid = r["chunk_id"]
        if cid not in seen:
            seen.add(cid)
            ranked_ids.append(cid)
        if len(ranked_ids) >= top_k:
            break

    # If too few FTS results, fall back to ordinal-based selection
    if len(ranked_ids) < top_k // 2:
        all_chunks = (
            session.query(Chunk)
            .filter(Chunk.episode_id == episode_id)
            .order_by(Chunk.ordinal)
            .limit(top_k)
            .all()
        )
        for c in all_chunks:
            if c.chunk_id not in seen:
                seen.add(c.chunk_id)
                ranked_ids.append(c.chunk_id)
            if len(ranked_ids) >= top_k:
                break

    # Fetch full text from Chunk ORM
    chunks_orm = (
        session.query(Chunk)
        .filter(Chunk.chunk_id.in_(ranked_ids))
        .all()
    )
    chunk_map = {c.chunk_id: c for c in chunks_orm}

    # Build result list preserving rank order
    result = []
    for rank, cid in enumerate(ranked_ids):
        if cid in chunk_map:
            c = chunk_map[cid]
            result.append({
                "chunk_id": c.chunk_id,
                "episode_id": c.episode_id,
                "ordinal": c.ordinal,
                "text": c.text,
                "rank": rank,
            })

    return result


def format_chunks_for_prompt(chunks: list[dict], episode_id: str) -> str:
    """Format retrieved chunks into a text block for prompt insertion.

    Each chunk is labeled with its citation ID for the model to reference.
    """
    lines = []
    for c in chunks:
        cid = f"[{episode_id}_C{c['ordinal']:04d}]"
        lines.append(f"--- {cid} ---")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def save_retrieval_snapshot(
    chunks: list[dict],
    artifact_type: str,
    output_dir: Path,
    query_terms: list[str],
    top_k: int,
) -> str:
    """Save retrieval snapshot JSON for an artifact.

    Returns:
        Path to the snapshot file.
    """
    snapshot_dir = output_dir / "retrieval"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{artifact_type}_snapshot.json"

    snapshot = {
        "artifact_type": artifact_type,
        "episode_id": chunks[0]["episode_id"] if chunks else "",
        "timestamp": _utcnow().isoformat(),
        "top_k": top_k,
        "query_terms": query_terms,
        "chunks": [
            {
                "chunk_id": c["chunk_id"],
                "ordinal": c["ordinal"],
                "text": c["text"],
                "rank": c["rank"],
            }
            for c in chunks
        ],
    }

    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def generate_content(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    top_k: int = 16,
) -> GenerationResult:
    """Generate all Turkish content artifacts for a CHUNKED episode.

    Sequentially generates: outline -> script -> shorts -> visuals -> qa -> publishing.

    Returns:
        GenerationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in CHUNKED/GENERATED state.
    """
    episode = (
        session.query(Episode)
        .filter(Episode.episode_id == episode_id)
        .first()
    )
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.CHUNKED, EpisodeStatus.GENERATED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'chunked'. Use --force to override."
        )

    output_dir = Path(settings.outputs_dir) / episode_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.GENERATE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    result = GenerationResult(episode_id=episode_id, output_dir=str(output_dir))

    try:
        # Retrieve chunks
        query_terms = build_query_terms(episode.title)
        chunks = retrieve_chunks(session, episode_id, query_terms, top_k=top_k)

        if not chunks:
            raise ValueError(f"No chunks found for episode {episode_id}")

        chunks_text = format_chunks_for_prompt(chunks, episode_id)

        # Generate artifacts sequentially
        outline_resp = _generate_artifact(
            "outline", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
        )
        _accumulate(result, outline_resp)

        script_resp = _generate_artifact(
            "script", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
            outline_text=outline_resp["text"],
        )
        _accumulate(result, script_resp)

        shorts_resp = _generate_artifact(
            "shorts", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
            outline_text=outline_resp["text"],
        )
        _accumulate(result, shorts_resp)

        visuals_resp = _generate_artifact(
            "visuals", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
            outline_text=outline_resp["text"],
        )
        _accumulate(result, visuals_resp)

        qa_resp = _generate_artifact(
            "qa", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
            script_text=script_resp["text"],
        )
        _accumulate(result, qa_resp)

        pub_resp = _generate_artifact(
            "publishing", episode, chunks, chunks_text, query_terms,
            settings, output_dir, top_k, session, force,
            outline_text=outline_resp["text"],
            script_text=script_resp["text"],
        )
        _accumulate(result, pub_resp)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = result.total_input_tokens
        pipeline_run.output_tokens = result.total_output_tokens
        pipeline_run.estimated_cost_usd = result.total_cost_usd

        # Update Episode
        episode.status = EpisodeStatus.GENERATED
        episode.output_dir = str(output_dir)
        session.commit()

        logger.info(
            "Generated %d artifacts for %s ($%.4f)",
            len(result.artifacts), episode_id, result.total_cost_usd,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise

    return result


def refine_content(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> GenerationResult:
    """Refine generated content using QA feedback.

    Reads v1 outline, script, and qa.json to produce v2 artifacts:
    - outline.tr.v2.md (improved structure)
    - script.long.tr.v2.md (improved script)
    - publishing_pack.v2.json (regenerated from v2)

    Returns:
        GenerationResult with paths and usage stats.

    Raises:
        ValueError: If episode not found or not in GENERATED/REFINED state.
    """
    episode = (
        session.query(Episode)
        .filter(Episode.episode_id == episode_id)
        .first()
    )
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.GENERATED, EpisodeStatus.REFINED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'generated'. Use --force to override."
        )

    output_dir = Path(settings.outputs_dir) / episode_id

    # Read v1 artifacts (required inputs)
    outline_v1_path = output_dir / ARTIFACT_FILENAMES["outline"]
    script_v1_path = output_dir / ARTIFACT_FILENAMES["script"]
    qa_path = output_dir / ARTIFACT_FILENAMES["qa"]

    for path, label in [
        (outline_v1_path, "outline v1"),
        (script_v1_path, "script v1"),
        (qa_path, "qa.json"),
    ]:
        if not path.exists():
            raise ValueError(f"Missing required input: {label} ({path})")

    outline_v1 = outline_v1_path.read_text(encoding="utf-8")
    script_v1 = script_v1_path.read_text(encoding="utf-8")
    qa_text = qa_path.read_text(encoding="utf-8")

    # Create PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.REFINE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()

    result = GenerationResult(episode_id=episode_id, output_dir=str(output_dir))

    try:
        # We don't re-retrieve chunks for refinement — prompts use v1 text + QA
        # Use empty chunks list for snapshot/hash (no retrieval needed)
        empty_chunks: list[dict] = []
        empty_query_terms: list[str] = []

        # Step 1: Refine outline using v1 outline + QA
        outline_v2_resp = _generate_artifact(
            "refine_outline", episode, empty_chunks, "", empty_query_terms,
            settings, output_dir, 0, session, force,
            outline_text=outline_v1,
            qa_text=qa_text,
        )
        _accumulate(result, outline_v2_resp)

        # Step 2: Refine script using v1 script + v2 outline + QA
        script_v2_resp = _generate_artifact(
            "refine_script", episode, empty_chunks, "", empty_query_terms,
            settings, output_dir, 0, session, force,
            script_text=script_v1,
            outline_text=outline_v2_resp["text"],
            qa_text=qa_text,
        )
        _accumulate(result, script_v2_resp)

        # Step 3: Regenerate publishing pack from v2 outline + v2 script
        pub_v2_resp = _generate_artifact(
            "refine_publishing", episode, empty_chunks, "", empty_query_terms,
            settings, output_dir, 0, session, force,
            outline_text=outline_v2_resp["text"],
            script_text=script_v2_resp["text"],
        )
        _accumulate(result, pub_v2_resp)

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.input_tokens = result.total_input_tokens
        pipeline_run.output_tokens = result.total_output_tokens
        pipeline_run.estimated_cost_usd = result.total_cost_usd

        # Update Episode
        episode.status = EpisodeStatus.REFINED
        session.commit()

        logger.info(
            "Refined %d artifacts for %s ($%.4f)",
            len(result.artifacts), episode_id, result.total_cost_usd,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        raise

    return result


def _accumulate(result: GenerationResult, artifact_resp: dict) -> None:
    """Accumulate artifact response into GenerationResult."""
    result.artifacts.append(artifact_resp["path"])
    result.total_input_tokens += artifact_resp["input_tokens"]
    result.total_output_tokens += artifact_resp["output_tokens"]
    result.total_cost_usd += artifact_resp["cost"]


def _generate_artifact(
    artifact_type: str,
    episode: Episode,
    chunks: list[dict],
    chunks_text: str,
    query_terms: list[str],
    settings: Settings,
    output_dir: Path,
    top_k: int,
    session: Session,
    force: bool,
    outline_text: str = "",
    script_text: str = "",
    qa_text: str = "",
) -> dict:
    """Generate a single artifact. Returns dict with text, path, tokens, cost."""
    filename = ARTIFACT_FILENAMES[artifact_type]
    output_path = output_dir / filename

    # Idempotency: skip if file exists and not force
    if output_path.exists() and not force:
        logger.info("Artifact exists: %s (use --force to regenerate)", output_path)
        existing_text = output_path.read_text(encoding="utf-8")
        return {
            "text": existing_text,
            "path": str(output_path),
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }

    # Build prompt
    from btcedu.prompts.system import SYSTEM_PROMPT

    user_prompt = _build_prompt(
        artifact_type, episode.title, episode.episode_id,
        chunks_text, outline_text, script_text, qa_text,
    )

    # Compute prompt hash
    chunk_ids = [c["chunk_id"] for c in chunks]
    prompt_hash = compute_prompt_hash(
        user_prompt, settings.claude_model, settings.claude_temperature, chunk_ids,
    )

    # Dry-run path
    dry_run_path = output_dir / f"dry_run_{artifact_type}.json" if settings.dry_run else None

    # Call Claude
    response: ClaudeResponse = call_claude(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_prompt,
        settings=settings,
        dry_run_path=dry_run_path,
    )

    # Write output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(response.text, encoding="utf-8")

    # Save retrieval snapshot (skip for refine artifacts which don't retrieve chunks)
    snapshot_path = None
    if chunks:
        snapshot_path = save_retrieval_snapshot(
            chunks, artifact_type, output_dir, query_terms, top_k,
        )

    # Persist ContentArtifact
    artifact = ContentArtifact(
        episode_id=episode.episode_id,
        artifact_type=artifact_type,
        file_path=str(output_path),
        model=response.model,
        prompt_hash=prompt_hash,
        retrieval_snapshot_path=snapshot_path,
    )
    session.add(artifact)
    session.flush()

    return {
        "text": response.text,
        "path": str(output_path),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cost": response.cost_usd,
    }


def _build_prompt(
    artifact_type: str,
    episode_title: str,
    episode_id: str,
    chunks_text: str,
    outline_text: str = "",
    script_text: str = "",
    qa_text: str = "",
) -> str:
    """Build user prompt from the appropriate template."""
    if artifact_type == "outline":
        from btcedu.prompts.outline import build_user_prompt
        return build_user_prompt(episode_title, episode_id, chunks_text)

    elif artifact_type == "script":
        from btcedu.prompts.script import build_user_prompt
        return build_user_prompt(episode_title, episode_id, chunks_text, outline_text)

    elif artifact_type == "shorts":
        from btcedu.prompts.shorts import build_user_prompt
        return build_user_prompt(episode_title, episode_id, chunks_text, outline_text)

    elif artifact_type == "visuals":
        from btcedu.prompts.visuals import build_user_prompt
        return build_user_prompt(episode_title, episode_id, chunks_text, outline_text)

    elif artifact_type == "qa":
        from btcedu.prompts.qa import build_user_prompt
        return build_user_prompt(episode_title, episode_id, chunks_text, script_text)

    elif artifact_type == "publishing":
        from btcedu.prompts.publishing import build_user_prompt
        return build_user_prompt(episode_title, episode_id, outline_text, script_text)

    elif artifact_type == "refine_outline":
        from btcedu.prompts.refine_outline import build_user_prompt
        return build_user_prompt(episode_title, episode_id, outline_text, qa_text)

    elif artifact_type == "refine_script":
        from btcedu.prompts.refine_script import build_user_prompt
        return build_user_prompt(
            episode_title, episode_id, script_text, outline_text, qa_text,
        )

    elif artifact_type == "refine_publishing":
        from btcedu.prompts.publishing import build_user_prompt
        return build_user_prompt(episode_title, episode_id, outline_text, script_text)

    else:
        raise ValueError(f"Unknown artifact type: {artifact_type}")
