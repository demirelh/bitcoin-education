"""Stock image search and selection using Pexels API."""

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType

logger = logging.getLogger(__name__)

# Visual types that need stock photos (same as DALL-E image_generator)
VISUAL_TYPES_NEEDING_IMAGES = {"diagram", "b_roll", "screen_share"}

# Turkish → English domain-specific translation table
_TR_TO_EN = {
    # Economics
    "para": "money",
    "ekonomi": "economy",
    "ekonomik": "economic",
    "enflasyon": "inflation",
    "deflasyon": "deflation",
    "zenginlik": "wealth",
    "gelir": "income",
    "dağılım": "distribution",
    "faiz": "interest rate",
    "borç": "debt",
    "vergi": "tax",
    "bütçe": "budget",
    "tasarruf": "savings",
    "yatırım": "investment",
    "piyasa": "market",
    "ticaret": "trade",
    "ihracat": "export",
    "ithalat": "import",
    "maaş": "salary",
    "ücret": "wage",
    "fiyat": "price",
    "maliyet": "cost",
    "kâr": "profit",
    "zarar": "loss",
    # Bitcoin / crypto
    "bitcoin": "bitcoin",
    "kripto": "cryptocurrency",
    "blokzincir": "blockchain",
    "madencilik": "mining",
    "cüzdan": "wallet",
    "işlem": "transaction",
    # Finance
    "banka": "bank",
    "merkez bankası": "central bank",
    "dolar": "dollar",
    "altın": "gold",
    "hisse": "stock",
    "tahvil": "bond",
    # Society / politics
    "toplum": "society",
    "eşitsizlik": "inequality",
    "siyaset": "politics",
    "hükümet": "government",
    "devlet": "state",
    # Production / work
    "üretim": "production",
    "tüketim": "consumption",
    "fabrika": "factory",
    "sanayi": "industry",
    # Visuals
    "grafik": "chart graph",
    "tablo": "table",
    "karşılaştırma": "comparison",
    "diyagram": "diagram",
    # General
    "sistem": "system",
    "değişim": "change",
    "tarih": "history",
    "teknoloji": "technology",
    "dijital": "digital",
    "güvenlik": "security",
    "özgürlük": "freedom",
    "gayrimenkul": "real estate",
    "konut": "housing",
    "ev": "house",
    # Phase 3 additions — polysemous / frequently-missed terms
    "makas": "gap divide",
    "baskı": "pressure",
    "köpük": "bubble",
    "balon": "bubble",
    "boşluk": "gap void",
    "çukur": "pit downturn",
    "dalga": "wave cycle",
    "patlama": "boom explosion",
    "daralma": "contraction",
    "aşınma": "erosion decline",
    "tavan": "ceiling cap",
    "taban": "floor base",
    "kaldıraç": "leverage",
    "çıpa": "anchor peg",
    "sürdürülebilir": "sustainable",
    # Phase 4 additions — news/political domain terms
    # Politics & Government
    "meclis": "parliament",
    "başbakan": "prime minister",
    "cumhurbaşkanı": "president",
    "bakan": "minister",
    "seçim": "election",
    "oy": "vote",
    "parti": "political party",
    "koalisyon": "coalition",
    "muhalefet": "opposition",
    "yasa": "law",
    "anayasa": "constitution",
    # International
    "savaş": "war",
    "barış": "peace",
    "mülteci": "refugee",
    "göç": "migration",
    "diplomatik": "diplomatic",
    # Society & Infrastructure
    "hastane": "hospital",
    "okul": "school",
    "eğitim": "education",
    "ulaşım": "transportation",
    "trafik": "traffic",
    "çevre": "environment",
    "iklim": "climate",
    "deprem": "earthquake",
    "sel": "flood",
    # Weather
    "hava": "weather",
    "yağmur": "rain",
    "fırtına": "storm",
    "sıcaklık": "temperature",
    "kar": "snow",
    "güneş": "sunshine",
    "bulut": "cloud",
}

# Turkish stop words to filter out
_TR_STOP_WORDS = {
    "ve", "ile", "bir", "bu", "da", "de", "den", "dan", "için", "olan",
    "gibi", "çok", "daha", "hem", "ama", "fakat", "veya", "ya", "ki",
    "ne", "nasıl", "neden", "mi", "mu", "mı", "mü", "dir", "dır",
    "dür", "dur", "tır", "tir", "lar", "ler", "nin", "nın", "nün",
    "nun", "giriş", "sonuç", "arasındaki", "arasında", "olan", "olarak",
}

# Visual-type modifiers for better search results
_VISUAL_TYPE_MODIFIERS = {
    "diagram": "chart graph infographic",
    "b_roll": "photo",
    "screen_share": "screenshot interface",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StockCandidate:
    """A candidate photo for a chapter."""

    chapter_id: str
    pexels_id: int
    photographer: str
    photographer_url: str
    source_url: str
    download_url: str
    local_path: str
    alt_text: str
    search_query: str
    downloaded_at: str
    size_bytes: int
    width: int
    height: int
    selected: bool = False
    locked: bool = False


@dataclass
class StockSearchResult:
    """Summary of stock image search for one episode."""

    episode_id: str
    candidates_dir: Path
    candidates_manifest_path: Path
    chapters_searched: int
    total_candidates: int
    skipped_chapters: int
    cost_usd: float = 0.0


@dataclass
class RankResult:
    """Summary of LLM-based candidate ranking for one episode."""

    episode_id: str
    chapters_ranked: int
    chapters_skipped: int  # locked or no candidates
    total_cost_usd: float


@dataclass
class IntentResult:
    """Summary of chapter intent extraction for one episode."""

    episode_id: str
    chapters_analyzed: int
    cost_usd: float
    intent_path: Path


@dataclass
class StockSelectResult:
    """Summary after finalizing selections."""

    episode_id: str
    images_path: Path
    manifest_path: Path
    selected_count: int
    placeholder_count: int


def search_stock_images(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> StockSearchResult:
    """Search Pexels for candidate images per chapter.

    For each chapter with visual type in {diagram, b_roll, screen_share}:
    1. Derive search query from chapter metadata
    2. Search Pexels API
    3. Download candidates to data/outputs/{ep_id}/images/candidates/{ch_id}/
    4. Write candidates_manifest.json
    """
    from btcedu.services.pexels_service import PexelsService

    _get_episode(session, episode_id)  # validate episode exists
    chapters_doc = _load_chapters(episode_id, settings)
    chapters_hash = _compute_chapters_hash(chapters_doc)

    # Load profile for domain tag (falls back to "finance" for bitcoin_podcast)
    _profile = _load_episode_profile(session, episode_id, settings)
    _domain_tag = getattr(_profile, "domain", None) or "finance"

    output_base = Path(settings.outputs_dir) / episode_id
    candidates_dir = output_base / "images" / "candidates"
    manifest_path = candidates_dir / "candidates_manifest.json"

    # Idempotency check
    if not force and _is_search_current(manifest_path, chapters_hash):
        logger.info("Stock image search is current for %s (use --force to re-search)", episode_id)
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        total = sum(
            len(ch_data.get("candidates", []))
            for ch_data in existing.get("chapters", {}).values()
        )
        return StockSearchResult(
            episode_id=episode_id,
            candidates_dir=candidates_dir,
            candidates_manifest_path=manifest_path,
            chapters_searched=len(existing.get("chapters", {})),
            total_candidates=total,
            skipped_chapters=0,
        )

    # Create pipeline run
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="imagegen",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        service = PexelsService(api_key=settings.pexels_api_key)

        # Load existing manifest to preserve locked selections
        existing_manifest = {}
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        chapters_data = {}
        total_candidates = 0
        skipped = 0
        searched = 0

        for chapter in chapters_doc.chapters:
            visual = chapter.visual
            if not visual or visual.type not in VISUAL_TYPES_NEEDING_IMAGES:
                skipped += 1
                continue

            # Check for locked selections
            ch_existing = existing_manifest.get("chapters", {}).get(chapter.chapter_id, {})
            if not force and _has_locked_selection(ch_existing):
                chapters_data[chapter.chapter_id] = ch_existing
                total_candidates += len(ch_existing.get("candidates", []))
                skipped += 1
                continue

            # Derive search query (domain tag from profile)
            query = _derive_search_query(chapter, domain_tag=_domain_tag)

            # Search Pexels
            per_page = settings.pexels_results_per_chapter
            result = service.search(
                query=query,
                per_page=per_page,
                orientation=settings.pexels_orientation,
            )

            # Download candidates
            ch_candidates = []
            ch_dir = candidates_dir / chapter.chapter_id
            ch_dir.mkdir(parents=True, exist_ok=True)

            for photo in result.photos:
                filename = f"pexels_{photo.id}.jpg"
                local_path = ch_dir / filename
                rel_path = f"images/candidates/{chapter.chapter_id}/{filename}"

                try:
                    service.download_photo(
                        photo, local_path, size=settings.pexels_download_size
                    )
                    file_size = local_path.stat().st_size
                except Exception as e:
                    logger.warning(f"Failed to download Pexels photo {photo.id}: {e}")
                    continue

                ch_candidates.append({
                    "pexels_id": photo.id,
                    "asset_type": "photo",
                    "photographer": photo.photographer,
                    "photographer_url": photo.photographer_url,
                    "source_url": photo.url,
                    "download_url": photo.src_large2x,
                    "local_path": rel_path,
                    "alt_text": photo.alt,
                    "width": photo.width,
                    "height": photo.height,
                    "size_bytes": file_size,
                    "downloaded_at": _utcnow().isoformat(),
                    "selected": False,
                    "locked": False,
                })

            # Phase 4: Search for video candidates (b_roll only, when enabled)
            has_video_candidates = False
            if (
                getattr(settings, "pexels_video_enabled", False)
                and visual.type == "b_roll"
                and not _has_locked_selection(
                    existing_manifest.get("chapters", {}).get(chapter.chapter_id, {})
                )
            ):
                try:
                    video_result = service.search_videos(
                        query=query,
                        per_page=getattr(settings, "pexels_video_per_chapter", 2),
                        orientation=settings.pexels_orientation,
                    )
                    max_duration = getattr(settings, "pexels_video_max_duration", 30)
                    preferred_quality = getattr(
                        settings, "pexels_video_preferred_quality", "hd"
                    )

                    for video in video_result.videos:
                        # Skip videos longer than max duration
                        if video.duration > max_duration:
                            logger.debug(
                                "Skipping Pexels video %d: duration %ds > max %ds",
                                video.id, video.duration, max_duration,
                            )
                            continue

                        # Select the best video file
                        selected_file = service._select_video_file(video, preferred_quality)
                        if not selected_file:
                            continue

                        # Download video file
                        vid_filename = f"pexels_v_{video.id}.mp4"
                        vid_local_path = ch_dir / vid_filename
                        vid_rel_path = (
                            f"images/candidates/{chapter.chapter_id}/{vid_filename}"
                        )

                        # Download preview thumbnail
                        preview_filename = f"pexels_v_{video.id}_preview.jpg"
                        preview_local_path = ch_dir / preview_filename
                        preview_rel_path = (
                            f"images/candidates/{chapter.chapter_id}/{preview_filename}"
                        )

                        try:
                            service.download_video(video, vid_local_path, preferred_quality)
                            vid_size = vid_local_path.stat().st_size
                        except Exception as e:
                            logger.warning(
                                "Failed to download Pexels video %d: %s", video.id, e
                            )
                            continue

                        try:
                            service.download_video_preview(video, preview_local_path)
                        except Exception as e:
                            logger.warning(
                                "Failed to download Pexels video preview %d: %s",
                                video.id, e,
                            )
                            # Preview is optional — continue with empty path
                            preview_rel_path = ""

                        ch_candidates.append({
                            "pexels_id": video.id,
                            "asset_type": "video",
                            "photographer": video.user_name,
                            "photographer_url": video.user_url,
                            "source_url": video.url,
                            "download_url": selected_file.link,
                            "local_path": vid_rel_path,
                            "preview_url": video.image,
                            "preview_path": preview_rel_path,
                            "alt_text": "",
                            "width": selected_file.width,
                            "height": selected_file.height,
                            "duration_seconds": float(video.duration),
                            "fps": selected_file.fps,
                            "size_bytes": vid_size,
                            "downloaded_at": _utcnow().isoformat(),
                            "selected": False,
                            "locked": False,
                        })
                        has_video_candidates = True

                except Exception as e:
                    logger.warning(
                        "Video search failed for chapter %s: %s", chapter.chapter_id, e
                    )

            chapters_data[chapter.chapter_id] = {
                "search_query": query,
                "candidates": ch_candidates,
            }
            total_candidates += len(ch_candidates)
            searched += 1
            _ = has_video_candidates  # used for schema version bump below

        # Write candidates manifest
        # Bump schema version if any video candidates were found
        has_any_video = any(
            c.get("asset_type") == "video"
            for ch_data in chapters_data.values()
            for c in ch_data.get("candidates", [])
        )
        schema_version = "3.1" if has_any_video else "1.0"

        candidates_dir.mkdir(parents=True, exist_ok=True)
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": schema_version,
            "searched_at": _utcnow().isoformat(),
            "chapters_hash": chapters_hash,
            "chapters": chapters_data,
        }
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Update pipeline run
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = 0.0
        session.commit()

        logger.info(
            f"Stock image search for {episode_id}: {searched} chapters searched, "
            f"{total_candidates} candidates downloaded"
        )

        return StockSearchResult(
            episode_id=episode_id,
            candidates_dir=candidates_dir,
            candidates_manifest_path=manifest_path,
            chapters_searched=searched,
            total_candidates=total_candidates,
            skipped_chapters=skipped,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        session.commit()
        raise


def rank_candidates(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> RankResult:
    """Use LLM to rank stock photo candidates per chapter.

    Reads candidates_manifest.json, calls LLM for each chapter with candidates,
    writes rank + rank_reason back to manifest, selects rank=1 as default pick.

    Skips chapters with locked selections unless force=True.
    """
    from btcedu.services.claude_service import call_claude

    _get_episode(session, episode_id)
    chapters_doc = _load_chapters(episode_id, settings)

    output_base = Path(settings.outputs_dir) / episode_id
    candidates_manifest_path = (
        output_base / "images" / "candidates" / "candidates_manifest.json"
    )

    if not candidates_manifest_path.exists():
        raise FileNotFoundError(
            f"No candidates manifest for {episode_id}. Run 'btcedu stock search' first."
        )

    manifest = json.loads(candidates_manifest_path.read_text(encoding="utf-8"))
    chapters_data = manifest.get("chapters", {})

    # Build chapter lookup for context
    chapter_lookup = {ch.chapter_id: ch for ch in chapters_doc.chapters}

    # Load profile-aware system prompt
    _profile = _load_episode_profile(session, episode_id, settings)
    _prompt_ns = getattr(_profile, "prompt_namespace", None) if _profile else None
    try:
        registry = PromptRegistry(session)
        _tmpl_path = registry.resolve_template_path("stock_rank.md", profile=_prompt_ns)
        if _tmpl_path.exists():
            _meta, _body = registry.load_template(_tmpl_path)
            system_prompt = _body.strip()
        else:
            system_prompt = (
                "You are an editorial assistant selecting the best stock photo "
                "for a YouTube video chapter. The video covers Bitcoin and "
                "cryptocurrency education, targeting a Turkish audience."
            )
    except Exception:
        system_prompt = (
            "You are an editorial assistant selecting the best stock photo "
            "for a YouTube video chapter. The video covers Bitcoin and "
            "cryptocurrency education, targeting a Turkish audience."
        )

    chapters_ranked = 0
    chapters_skipped = 0
    total_cost = 0.0

    # Extract intents for the full episode (cached if already done)
    intent_result = extract_chapter_intents(session, episode_id, settings, force=force)
    intent_map: dict = {}  # ch_id -> intent dict
    if intent_result.intent_path.exists():
        try:
            intent_data_doc = json.loads(intent_result.intent_path.read_text(encoding="utf-8"))
            intent_map = intent_data_doc.get("chapters", {})
        except (json.JSONDecodeError, KeyError):
            pass
    total_cost += intent_result.cost_usd

    selected_so_far: set[int] = set()  # track selected Pexels IDs across chapters

    for ch_id, ch_data in chapters_data.items():
        candidates = ch_data.get("candidates", [])

        if not candidates:
            chapters_skipped += 1
            continue

        # Skip locked chapters unless forced
        if not force and any(c.get("locked") for c in candidates):
            chapters_skipped += 1
            continue

        # Single candidate: auto-rank as 1, no LLM call needed
        if len(candidates) == 1:
            candidates[0]["rank"] = 1
            candidates[0]["rank_reason"] = "Only candidate available"
            candidates[0]["selected"] = True
            ch_data["pinned_by"] = "llm_rank"
            chapters_ranked += 1
            continue

        chapter = chapter_lookup.get(ch_id)
        if not chapter:
            chapters_skipped += 1
            continue

        # Build user message for LLM ranking
        narration_text = ""
        if hasattr(chapter, "narration") and chapter.narration:
            narration_text = getattr(chapter.narration, "text", "") or ""

        def _fmt_candidate(c: dict) -> str:
            asset_type = c.get("asset_type", "photo")
            base = (
                f"- Pexels ID: {c['pexels_id']}, Asset type: {asset_type}"
            )
            if asset_type == "video":
                dur = c.get("duration_seconds", "")
                base += f" ({dur}s clip)"
                base += ", Alt: (video clip — no description available)"
            else:
                base += f", Alt: {c.get('alt_text', '')}"
            base += (
                f", Dimensions: {c.get('width', 0)}x{c.get('height', 0)}"
                f", Photographer: {c.get('photographer', '')}"
            )
            return base

        candidate_list = "\n".join(_fmt_candidate(c) for c in candidates)

        # Extract intent data for this chapter
        ch_intents = intent_map.get(ch_id, {})
        intents = ch_intents.get("intents", [])
        allowed_motifs = ch_intents.get("allowed_motifs", [])
        disallowed_motifs = ch_intents.get("disallowed_motifs", [])
        literal_traps = ch_intents.get("literal_traps", [])

        # Format literal traps for prompt
        traps_text = ""
        if literal_traps:
            traps_text = "\n".join(
                f'  - "{t.get("word","")}" means "{t.get("intended","")}"'
                f' here, NOT "{t.get("trap","")}"'
                for t in literal_traps
            )

        # Already selected IDs for dedup hint
        already_selected = list(selected_so_far) if selected_so_far else []

        visual_type_str = chapter.visual.type if chapter.visual else "unknown"
        user_message = (
            f"## Chapter Context\n"
            f"- Title: {chapter.title}\n"
            f"- Visual type: {visual_type_str}\n"
            f"- Visual description: "
            f"{chapter.visual.description if chapter.visual else ''}\n"
            f"- Narration excerpt: {narration_text[:200]}\n"
            f"- Search query: {ch_data.get('search_query', '')}\n\n"
            f"## Semantic Intent\n"
            f"- Intents: {', '.join(intents) if intents else 'not available'}\n"
            f"- Allowed motifs: "
            f"{', '.join(allowed_motifs) if allowed_motifs else 'not available'}\n"
            f"- Disallowed motifs (DO NOT select): "
            f"{', '.join(disallowed_motifs) if disallowed_motifs else 'none'}\n"
        )
        if traps_text:
            user_message += f"- Literal traps to avoid:\n{traps_text}\n"
        if already_selected:
            already_str = ", ".join(str(x) for x in already_selected)
            user_message += (
                f"\n## Variety Preference\n"
                f"Already selected in other chapters: {already_str}\n"
            )
        # Phase 4: Motion preference hint based on visual type
        if visual_type_str == "b_roll":
            user_message += (
                "\n## Motion Preference\n"
                "This chapter uses B-roll visual type. "
                "Short video clips are preferred over still photos "
                "if they are semantically relevant and avoid literal traps.\n"
            )
        elif visual_type_str in ("diagram", "screen_share"):
            user_message += (
                f"\n## Asset Type Preference\n"
                f"This chapter uses {visual_type_str} visual type. "
                "Static images (photos) are preferred over video clips "
                "for data graphics and technical content.\n"
            )
        user_message += (
            f"\n## Candidates\n{candidate_list}\n\n"
            f"## Task\n"
            f"Rank ALL candidates. Check disallowed motifs first. "
            f"Set trap_flag=true for any candidate "
            f"matching a disallowed motif or literal trap. "
            f"Return ONLY valid JSON with trap_flag field:\n"
            '{{"rankings": [{{"pexels_id": ..., "rank": 1, '
            '"reason": "...", "trap_flag": false}}, ...]}}'
        )

        if settings.dry_run:
            # Dry-run: rank by candidate order
            for i, c in enumerate(candidates):
                c["rank"] = i + 1
                c["rank_reason"] = f"Dry-run rank {i + 1}"
                c["trap_flag"] = False
                c["selected"] = (i == 0)
            ch_data["pinned_by"] = "llm_rank"
            ch_data["intents"] = intents
            chapters_ranked += 1
            # Track selected for dedup
            for c in candidates:
                if c.get("selected"):
                    selected_so_far.add(c["pexels_id"])
                    break
            continue

        try:
            response = call_claude(
                system_prompt=system_prompt,
                user_message=user_message,
                settings=settings,
                json_mode=True,
                max_tokens=4096,
            )
            total_cost += response.cost_usd

            # Parse LLM response
            rankings = _parse_ranking_response(response.text, candidates)
            _apply_rankings(candidates, rankings)
            _validate_and_adjust_selection(candidates, ch_intents, selected_so_far)
            ch_data["pinned_by"] = "llm_rank"
            ch_data["intents"] = intents
            # Track selected Pexels ID for dedup
            for c in candidates:
                if c.get("selected"):
                    selected_so_far.add(c["pexels_id"])
                    break
            chapters_ranked += 1

        except Exception as e:
            logger.warning(
                "LLM ranking failed for %s/%s: %s — falling back to order",
                episode_id, ch_id, e,
            )
            # Fallback: rank by candidate order
            for i, c in enumerate(candidates):
                c["rank"] = i + 1
                c["rank_reason"] = f"Fallback rank {i + 1} (LLM error)"
                c["trap_flag"] = False
                c["selected"] = (i == 0)
            ch_data["pinned_by"] = "llm_rank"
            ch_data["intents"] = intents
            # Track selected for dedup
            for c in candidates:
                if c.get("selected"):
                    selected_so_far.add(c["pexels_id"])
                    break
            chapters_ranked += 1

    # Update manifest metadata
    manifest["ranked_at"] = _utcnow().isoformat()
    manifest["ranking_model"] = settings.claude_model
    manifest["ranking_cost_usd"] = total_cost - intent_result.cost_usd  # ranking cost only
    manifest["intent_analysis_cost_usd"] = intent_result.cost_usd
    manifest["schema_version"] = "3.0"

    candidates_manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info(
        "Ranked candidates for %s: %d chapters ranked, %d skipped, $%.4f",
        episode_id, chapters_ranked, chapters_skipped, total_cost,
    )

    return RankResult(
        episode_id=episode_id,
        chapters_ranked=chapters_ranked,
        chapters_skipped=chapters_skipped,
        total_cost_usd=total_cost,
    )


def extract_chapter_intents(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> IntentResult:
    """Extract semantic intents per chapter for better stock image ranking.

    Makes a single LLM call for the entire episode. Produces intent_analysis.json
    with per-chapter intents, allowed/disallowed motifs, literal traps, and search hints.
    """
    from btcedu.services.claude_service import call_claude

    chapters_doc = _load_chapters(episode_id, settings)
    chapters_hash = _compute_chapters_hash(chapters_doc)

    output_base = Path(settings.outputs_dir) / episode_id
    intent_path = output_base / "images" / "candidates" / "intent_analysis.json"

    # Idempotency check
    if not force and intent_path.exists():
        try:
            cached = json.loads(intent_path.read_text(encoding="utf-8"))
            if cached.get("chapters_hash") == chapters_hash:
                logger.info("Intent analysis is current for %s", episode_id)
                return IntentResult(
                    episode_id=episode_id,
                    chapters_analyzed=len(cached.get("chapters", {})),
                    cost_usd=cached.get("cost_usd", 0.0),
                    intent_path=intent_path,
                )
        except (json.JSONDecodeError, KeyError):
            pass

    # Load profile for profile-aware system prompt
    _profile = _load_episode_profile(session, episode_id, settings)
    _prompt_ns = getattr(_profile, "prompt_namespace", None) if _profile else None

    # Register prompt template via PromptRegistry for cost/version tracking
    try:
        registry = PromptRegistry(session)
        template_file = registry.resolve_template_path("intent_extract.md", profile=_prompt_ns)
        if not template_file.exists():
            template_file = TEMPLATES_DIR / "intent_extract.md"
        if template_file.exists():
            registry.register_version("intent_extract", template_file, set_default=True)
    except Exception as _reg_err:
        logger.debug("PromptRegistry registration skipped for intent_extract: %s", _reg_err)

    if settings.dry_run:
        # Dry-run: return empty intents structure
        intent_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "analyzed_at": _utcnow().isoformat(),
            "model": "dry_run",
            "cost_usd": 0.0,
            "chapters_hash": chapters_hash,
            "chapters": {
                ch.chapter_id: {
                    "intents": [],
                    "allowed_motifs": [],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": [],
                }
                for ch in chapters_doc.chapters
                if ch.visual and ch.visual.type in VISUAL_TYPES_NEEDING_IMAGES
            },
        }
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(
            json.dumps(intent_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return IntentResult(
            episode_id=episode_id,
            chapters_analyzed=len(intent_data["chapters"]),
            cost_usd=0.0,
            intent_path=intent_path,
        )

    # Build chapters data for prompt
    chapters_for_prompt = []
    for ch in chapters_doc.chapters:
        if not ch.visual or ch.visual.type not in VISUAL_TYPES_NEEDING_IMAGES:
            continue
        narration_text = ""
        if hasattr(ch, "narration") and ch.narration:
            narration_text = getattr(ch.narration, "text", "") or ""
        chapters_for_prompt.append({
            "chapter_id": ch.chapter_id,
            "title": ch.title,
            "visual_type": ch.visual.type if ch.visual else "unknown",
            "visual_description": ch.visual.description if ch.visual else "",
            "narration_excerpt": narration_text[:200],
        })

    # Load profile-namespaced system prompt if available
    try:
        registry = PromptRegistry(session)
        _tmpl_path = registry.resolve_template_path("intent_extract.md", profile=_prompt_ns)
        if _tmpl_path.exists():
            _meta, _body = registry.load_template(_tmpl_path)
            system_prompt = _body.strip()
        else:
            system_prompt = (
                "You are a visual editor for an educational YouTube channel about Bitcoin and "
                "cryptocurrency, targeting a Turkish audience. Your task is to analyze video "
                "chapters and extract semantic intents for stock photo selection."
            )
    except Exception:
        system_prompt = (
            "You are a visual editor for an educational YouTube channel about Bitcoin and "
            "cryptocurrency, targeting a Turkish audience. Your task is to analyze video "
            "chapters and extract semantic intents for stock photo selection."
        )

    # Build user message with all chapters
    chapters_text = ""
    for ch in chapters_for_prompt:
        chapters_text += (
            f"### {ch['chapter_id']}: {ch['title']}\n"
            f"- Visual type: {ch['visual_type']}\n"
            f"- Visual description: {ch['visual_description']}\n"
            f"- Narration excerpt: {ch['narration_excerpt']}\n\n"
        )

    user_message = (
        "For each chapter below, extract:\n"
        "1. `intents` (1-3): Core concepts/themes\n"
        "2. `allowed_motifs` (3-6): Appropriate visual motifs\n"
        "3. `disallowed_motifs` (2-4): Motifs a naive search might return but would be WRONG\n"
        "4. `literal_traps`: Words with alternate meanings that could mislead image search. "
        'Format: [{"word": "...", "intended": "...", "trap": "..."}]\n'
        "5. `search_hints` (2-4): English Pexels search terms for the RIGHT photo\n\n"
        f"## Chapters\n{chapters_text}\n"
        'Return ONLY valid JSON: {"chapters": {"ch01": {...}, ...}}'
    )

    try:
        response = call_claude(
            system_prompt=system_prompt,
            user_message=user_message,
            settings=settings,
            json_mode=True,
            max_tokens=4096,
        )
        cost = response.cost_usd

        # Parse response
        parsed = _parse_intent_response(response.text, chapters_for_prompt)

        intent_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "analyzed_at": _utcnow().isoformat(),
            "model": settings.claude_model,
            "cost_usd": cost,
            "chapters_hash": chapters_hash,
            "chapters": parsed,
        }

    except Exception as e:
        logger.warning("Intent extraction failed for %s: %s — using empty intents", episode_id, e)
        cost = 0.0
        intent_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "analyzed_at": _utcnow().isoformat(),
            "model": settings.claude_model,
            "cost_usd": 0.0,
            "chapters_hash": chapters_hash,
            "chapters": {
                ch["chapter_id"]: {
                    "intents": [],
                    "allowed_motifs": [],
                    "disallowed_motifs": [],
                    "literal_traps": [],
                    "search_hints": [],
                }
                for ch in chapters_for_prompt
            },
        }

    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text(json.dumps(intent_data, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "Intent extraction for %s: %d chapters, $%.4f",
        episode_id, len(intent_data["chapters"]), cost,
    )

    return IntentResult(
        episode_id=episode_id,
        chapters_analyzed=len(intent_data["chapters"]),
        cost_usd=cost,
        intent_path=intent_path,
    )


def _parse_intent_response(response_text: str, chapters: list[dict]) -> dict:
    """Parse LLM intent extraction response."""
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    _EMPTY_CHAPTER_INTENTS = {
        "intents": [],
        "allowed_motifs": [],
        "disallowed_motifs": [],
        "literal_traps": [],
        "search_hints": [],
    }

    try:
        data = json.loads(text)
        chapters_data = data.get("chapters", {})
        result = {}
        for ch in chapters:
            ch_id = ch["chapter_id"]
            ch_intents = chapters_data.get(ch_id, {})
            result[ch_id] = {
                "intents": ch_intents.get("intents", []),
                "allowed_motifs": ch_intents.get("allowed_motifs", []),
                "disallowed_motifs": ch_intents.get("disallowed_motifs", []),
                "literal_traps": ch_intents.get("literal_traps", []),
                "search_hints": ch_intents.get("search_hints", []),
            }
        return result
    except (json.JSONDecodeError, KeyError, TypeError):
        return {ch["chapter_id"]: dict(_EMPTY_CHAPTER_INTENTS) for ch in chapters}


def _validate_and_adjust_selection(
    candidates: list[dict],
    intent_data: dict,
    selected_so_far: set,
) -> None:
    """Post-rank validation: catch trap-flagged winners and cross-chapter duplicates.

    Mutates candidates in-place. May swap the selected candidate.
    """
    if not candidates:
        return

    disallowed_motifs = [m.lower() for m in intent_data.get("disallowed_motifs", [])]

    def _is_trap(candidate: dict) -> bool:
        """Check if candidate triggers a literal trap or disallowed motif."""
        if candidate.get("trap_flag"):
            return True
        alt_text = candidate.get("alt_text", "").lower()
        return any(motif in alt_text for motif in disallowed_motifs if motif)

    # Find current selection
    selected = [c for c in candidates if c.get("selected")]
    if not selected:
        return
    current = selected[0]

    # Check trap
    if _is_trap(current):
        # Find first non-trap alternative
        alternatives = [
            c for c in candidates
            if not c.get("selected") and not _is_trap(c)
        ]
        if alternatives:
            alt = alternatives[0]
            current["selected"] = False
            alt["selected"] = True
            logger.warning(
                "Trap detected in selected candidate %s (alt: %s...) — "
                "replaced with %s",
                current.get("pexels_id"),
                current.get("alt_text", "")[:60],
                alt.get("pexels_id"),
            )
            current = alt
        else:
            logger.warning(
                "Trap detected in selected candidate %s but no clean alternative available",
                current.get("pexels_id"),
            )

    # Check duplicate
    if current.get("pexels_id") in selected_so_far:
        # Find best non-duplicate, non-trap candidate within rank ≤ 3
        alternatives = [
            c for c in candidates
            if not c.get("selected")
            and c.get("pexels_id") not in selected_so_far
            and not _is_trap(c)
            and c.get("rank", 999) <= 3
        ]
        if alternatives:
            alt = alternatives[0]
            current["selected"] = False
            alt["selected"] = True
            alt["dedup_adjusted"] = True
            logger.info(
                "Duplicate avoided: chapter would repeat Pexels %s — "
                "switched to %s",
                current.get("pexels_id"),
                alt.get("pexels_id"),
            )


def _parse_ranking_response(
    response_text: str, candidates: list[dict]
) -> list[dict]:
    """Parse LLM JSON response into ranking list.

    Returns list of {"pexels_id": int, "rank": int, "reason": str}.
    Falls back to empty list on parse failure.
    """
    # Strip markdown code fences if present
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
        rankings = data.get("rankings", [])
        if not isinstance(rankings, list):
            return []
        return rankings
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _apply_rankings(candidates: list[dict], rankings: list[dict]) -> None:
    """Apply parsed rankings to candidate list.

    If rankings are invalid or incomplete, falls back to order-based ranking.
    """
    valid_ids = {c["pexels_id"] for c in candidates}

    # Filter to only valid rankings
    valid_rankings = [
        r for r in rankings
        if r.get("pexels_id") in valid_ids and isinstance(r.get("rank"), int)
    ]

    if not valid_rankings:
        # Full fallback
        for i, c in enumerate(candidates):
            c["rank"] = i + 1
            c["rank_reason"] = f"Fallback rank {i + 1} (invalid LLM response)"
            c["selected"] = (i == 0)
        return

    # Apply valid rankings
    rank_map = {r["pexels_id"]: r for r in valid_rankings}

    # Assign ranks for candidates in the LLM response
    next_fallback_rank = len(valid_rankings) + 1
    for c in candidates:
        if c["pexels_id"] in rank_map:
            r = rank_map[c["pexels_id"]]
            c["rank"] = r["rank"]
            c["rank_reason"] = r.get("reason", "")
            c["trap_flag"] = r.get("trap_flag", False)
        else:
            # Candidate not in LLM response — assign tail rank
            c["rank"] = next_fallback_rank
            c["rank_reason"] = "Not ranked by LLM"
            c["trap_flag"] = False
            next_fallback_rank += 1
        c["selected"] = False

    # Select rank=1 candidate
    candidates.sort(key=lambda c: c.get("rank", 999))
    if candidates:
        candidates[0]["selected"] = True


def auto_select_best(
    session: Session,
    episode_id: str,
    settings: Settings,
) -> StockSelectResult:
    """Auto-select the first candidate for each chapter.

    Does NOT mark as locked. User can later override with stock select.
    """
    output_base = Path(settings.outputs_dir) / episode_id
    candidates_manifest_path = output_base / "images" / "candidates" / "candidates_manifest.json"

    if not candidates_manifest_path.exists():
        raise FileNotFoundError(
            f"No candidates manifest for {episode_id}. Run 'btcedu stock search' first."
        )

    manifest = json.loads(candidates_manifest_path.read_text(encoding="utf-8"))
    chapters_data = manifest.get("chapters", {})

    selected_count = 0
    for ch_id, ch_data in chapters_data.items():
        candidates = ch_data.get("candidates", [])
        if not candidates:
            continue

        # Skip if already has a locked selection
        already_selected = [c for c in candidates if c.get("selected")]
        if any(c.get("locked") for c in already_selected):
            selected_count += 1
            continue

        # Deselect previous selections
        for c in candidates:
            c["selected"] = False

        # Select first candidate
        candidates[0]["selected"] = True
        selected_count += 1

    # Write back updated manifest
    candidates_manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Now finalize into images/manifest.json
    return finalize_selections(session, episode_id, settings)


def select_stock_image(
    session: Session,
    episode_id: str,
    chapter_id: str,
    pexels_id: int,
    settings: Settings,
    lock: bool = False,
) -> None:
    """Select a specific candidate photo for a chapter."""
    output_base = Path(settings.outputs_dir) / episode_id
    candidates_manifest_path = output_base / "images" / "candidates" / "candidates_manifest.json"

    if not candidates_manifest_path.exists():
        raise FileNotFoundError(
            f"No candidates manifest for {episode_id}. Run 'btcedu stock search' first."
        )

    manifest = json.loads(candidates_manifest_path.read_text(encoding="utf-8"))
    ch_data = manifest.get("chapters", {}).get(chapter_id)
    if not ch_data:
        raise ValueError(f"Chapter {chapter_id} not found in candidates manifest")

    candidates = ch_data.get("candidates", [])
    found = False
    for c in candidates:
        if c["pexels_id"] == pexels_id:
            c["selected"] = True
            c["locked"] = lock
            found = True
        else:
            c["selected"] = False

    if not found:
        raise ValueError(
            f"Pexels photo {pexels_id} not found in candidates for chapter {chapter_id}"
        )

    # Set pinned_by to track selection origin
    ch_data["pinned_by"] = "human"

    # Write back
    candidates_manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Mark render stale
    _mark_render_stale(episode_id, Path(settings.outputs_dir))

    logger.info(
        f"Selected Pexels photo {pexels_id} for {episode_id}/{chapter_id}"
        + (" (locked)" if lock else "")
    )


def finalize_selections(
    session: Session,
    episode_id: str,
    settings: Settings,
) -> StockSelectResult:
    """Build final images/manifest.json from selected candidates.

    Produces the same manifest format as DALL-E image_generator.
    """
    episode = _get_episode(session, episode_id)
    chapters_doc = _load_chapters(episode_id, settings)

    output_base = Path(settings.outputs_dir) / episode_id
    images_dir = output_base / "images"
    manifest_path = images_dir / "manifest.json"
    provenance_path = output_base / "provenance" / "imagegen_provenance.json"
    candidates_manifest_path = images_dir / "candidates" / "candidates_manifest.json"

    if not candidates_manifest_path.exists():
        raise FileNotFoundError(
            f"No candidates manifest for {episode_id}. Run 'btcedu stock search' first."
        )

    candidates_manifest = json.loads(candidates_manifest_path.read_text(encoding="utf-8"))
    chapters_candidates = candidates_manifest.get("chapters", {})

    # Build image entries (same schema as DALL-E manifest)
    image_entries = []
    selected_count = 0
    placeholder_count = 0

    for chapter in chapters_doc.chapters:
        visual = chapter.visual
        if not visual:
            continue

        if visual.type not in VISUAL_TYPES_NEEDING_IMAGES:
            # Create placeholder for title_card/talking_head
            entry = _create_placeholder_entry(chapter, images_dir)
            image_entries.append(entry)
            placeholder_count += 1
            continue

        # Find selected candidate
        ch_data = chapters_candidates.get(chapter.chapter_id, {})
        candidates = ch_data.get("candidates", [])
        selected = [c for c in candidates if c.get("selected")]

        if not selected:
            # No selection — use first candidate if available
            if candidates:
                selected = [candidates[0]]
                candidates[0]["selected"] = True
            else:
                # No candidates at all — create placeholder
                entry = _create_placeholder_entry(chapter, images_dir)
                image_entries.append(entry)
                placeholder_count += 1
                continue

        candidate = selected[0]
        asset_type = candidate.get("asset_type", "photo")

        src_path = output_base / candidate["local_path"]

        if asset_type == "video":
            # Phase 4: Normalize and finalize video candidate
            dest_filename = f"{chapter.chapter_id}_selected.mp4"
            dest_path = images_dir / dest_filename
            normalized_path = images_dir / "candidates" / f"{chapter.chapter_id}_normalized.mp4"

            if src_path.exists():
                try:
                    from btcedu.services.ffmpeg_service import normalize_video_clip

                    normalize_video_clip(
                        input_path=str(src_path),
                        output_path=str(normalized_path),
                        target_duration=None,  # Trimming happens at render time
                        resolution=getattr(settings, "render_resolution", "1920x1080"),
                        fps=getattr(settings, "render_fps", 30),
                        crf=getattr(settings, "render_crf", 23),
                        preset=getattr(settings, "render_preset", "medium"),
                        timeout_seconds=getattr(settings, "render_timeout_segment", 300),
                        dry_run=getattr(settings, "dry_run", False),
                    )
                    # Copy normalized clip to images/
                    shutil.copy2(normalized_path, dest_path)
                    file_size = dest_path.stat().st_size
                except Exception as e:
                    logger.warning(
                        "Video normalization failed for %s/%s: %s — using placeholder",
                        episode_id, chapter.chapter_id, e,
                    )
                    entry = _create_placeholder_entry(chapter, images_dir)
                    image_entries.append(entry)
                    placeholder_count += 1
                    continue
            else:
                logger.warning(f"Selected video not found: {src_path}")
                entry = _create_placeholder_entry(chapter, images_dir)
                image_entries.append(entry)
                placeholder_count += 1
                continue

            entry = {
                "chapter_id": chapter.chapter_id,
                "chapter_title": chapter.title,
                "visual_type": visual.type,
                "asset_type": "video",
                "file_path": f"images/{dest_filename}",
                "prompt": None,
                "generation_method": "pexels_video",
                "model": None,
                "size": f"{candidate['width']}x{candidate['height']}",
                "mime_type": "video/mp4",
                "size_bytes": file_size,
                "duration_seconds": candidate.get("duration_seconds"),
                "metadata": {
                    "pexels_id": candidate["pexels_id"],
                    "photographer": candidate["photographer"],
                    "photographer_url": candidate["photographer_url"],
                    "source_url": candidate["source_url"],
                    "license": "Pexels License (free for commercial use)",
                    "search_query": ch_data.get("search_query", ""),
                    "alt_text": "",
                    "downloaded_at": candidate.get("downloaded_at", ""),
                    "normalized": True,
                    "original_duration": candidate.get("duration_seconds"),
                },
            }
            image_entries.append(entry)
            selected_count += 1

            # Create MediaAsset record with VIDEO type
            _create_media_asset(
                session, episode_id, chapter.chapter_id, entry,
                asset_type_override=MediaAssetType.VIDEO,
            )

        else:
            # Photo candidate: existing behavior
            dest_filename = f"{chapter.chapter_id}_selected.jpg"
            dest_path = images_dir / dest_filename

            if src_path.exists():
                shutil.copy2(src_path, dest_path)
                file_size = dest_path.stat().st_size
            else:
                logger.warning(f"Selected image not found: {src_path}")
                entry = _create_placeholder_entry(chapter, images_dir)
                image_entries.append(entry)
                placeholder_count += 1
                continue

            entry = {
                "chapter_id": chapter.chapter_id,
                "chapter_title": chapter.title,
                "visual_type": visual.type,
                "asset_type": "photo",
                "file_path": f"images/{dest_filename}",
                "prompt": None,
                "generation_method": "pexels",
                "model": None,
                "size": f"{candidate['width']}x{candidate['height']}",
                "mime_type": "image/jpeg",
                "size_bytes": file_size,
                "metadata": {
                    "pexels_id": candidate["pexels_id"],
                    "photographer": candidate["photographer"],
                    "photographer_url": candidate["photographer_url"],
                    "source_url": candidate["source_url"],
                    "license": "Pexels License (free for commercial use)",
                    "search_query": ch_data.get("search_query", ""),
                    "alt_text": candidate.get("alt_text", ""),
                    "downloaded_at": candidate.get("downloaded_at", ""),
                },
            }
            image_entries.append(entry)
            selected_count += 1

            # Create MediaAsset record
            _create_media_asset(session, episode_id, chapter.chapter_id, entry)

    # Write manifest (same format as DALL-E)
    manifest_data = {
        "episode_id": episode_id,
        "schema_version": "1.0",
        "generated_at": _utcnow().isoformat(),
        "images": image_entries,
    }
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Write provenance
    chapters_hash = _compute_chapters_hash(chapters_doc)
    provenance_data = {
        "stage": "imagegen",
        "episode_id": episode_id,
        "timestamp": _utcnow().isoformat(),
        "generation_method": "pexels",
        "input_content_hash": chapters_hash,
        "input_files": [
            str(Path(settings.outputs_dir) / episode_id / "chapters.json")
        ],
        "output_files": [str(manifest_path)],
        "image_count": len(image_entries),
        "selected_count": selected_count,
        "placeholder_count": placeholder_count,
        "cost_usd": 0.0,
    }
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Create ContentArtifact
    artifact = ContentArtifact(
        episode_id=episode_id,
        artifact_type="images",
        file_path=str(manifest_path.relative_to(output_base)),
        model="pexels",
        prompt_hash=chapters_hash,
        created_at=_utcnow(),
    )
    session.add(artifact)

    # Mark downstream stale
    _mark_render_stale(episode_id, Path(settings.outputs_dir))

    # Update episode status
    episode.status = EpisodeStatus.IMAGES_GENERATED
    session.commit()

    logger.info(
        f"Finalized stock images for {episode_id}: "
        f"{selected_count} selected, {placeholder_count} placeholders"
    )

    return StockSelectResult(
        episode_id=episode_id,
        images_path=images_dir,
        manifest_path=manifest_path,
        selected_count=selected_count,
        placeholder_count=placeholder_count,
    )


def _derive_search_query(
    chapter, search_hints: list[str] | None = None, domain_tag: str = "finance"
) -> str:
    """Extract search keywords from chapter metadata and translate to English.

    Strategy:
    1. If search_hints provided, use them as primary terms (Phase 3)
    2. Otherwise start with visual.description
    3. Add chapter title keywords
    4. Filter Turkish stop-words
    5. Translate key terms to English
    6. Add visual-type modifiers
    7. Append domain tag (profile-derived: "finance", "news", etc.)
    8. Deduplicate and cap at 8 keywords
    """
    # If LLM-generated search hints are available, use them as primary terms
    if search_hints:
        hints_query = " ".join(search_hints[:4])
        # Still add visual type modifier
        visual = chapter.visual
        if visual and visual.type in _VISUAL_TYPE_MODIFIERS:
            hints_query += " " + _VISUAL_TYPE_MODIFIERS[visual.type]
        return hints_query

    visual = chapter.visual
    words = []

    # Extract words from visual description
    if visual and visual.description:
        words.extend(_tokenize(visual.description))

    # Add chapter title words
    words.extend(_tokenize(chapter.title))

    # Filter stop words and short words
    words = [w for w in words if w.lower() not in _TR_STOP_WORDS and len(w) > 2]

    # Translate Turkish terms to English
    translated = []
    for word in words:
        lower = word.lower()
        if lower in _TR_TO_EN:
            translated.append(_TR_TO_EN[lower])
        elif word.isascii():
            translated.append(word.lower())
        # Drop untranslatable Turkish words

    # Add visual-type modifiers
    if visual and visual.type in _VISUAL_TYPE_MODIFIERS:
        translated.extend(_VISUAL_TYPE_MODIFIERS[visual.type].split())

    # Append domain tag from profile (default "finance" for backward compat)
    translated.append(domain_tag)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for term in translated:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique.append(term)

    # Cap at 8 keywords
    return " ".join(unique[:8])


def _tokenize(text: str) -> list[str]:
    """Split text into word tokens, removing punctuation."""
    return re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ0-9]+", text)


def _get_episode(session: Session, episode_id: str) -> Episode:
    """Get episode or raise ValueError."""
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    return episode


def _load_episode_profile(session: Session, episode_id: str, settings: Settings):
    """Load the ContentProfile for an episode. Returns None on any error."""
    try:
        from btcedu.profiles import get_registry

        episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
        if not episode:
            return None
        profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        registry = get_registry(settings)
        return registry.get(profile_name)
    except Exception:
        return None


def _load_chapters(episode_id: str, settings: Settings) -> ChapterDocument:
    """Load chapter document from JSON file."""
    from pydantic import ValidationError

    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not chapters_path.exists():
        raise FileNotFoundError(f"Chapters file not found: {chapters_path}")

    try:
        data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json: {e}") from e


def _compute_chapters_hash(chapters_doc: ChapterDocument) -> str:
    """Compute SHA-256 hash of chapter fields relevant to image search."""
    relevant = {
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "title": ch.title,
                "visual": (
                    {"type": ch.visual.type, "description": ch.visual.description}
                    if ch.visual
                    else None
                ),
            }
            for ch in chapters_doc.chapters
        ],
    }
    content_str = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _is_search_current(manifest_path: Path, chapters_hash: str) -> bool:
    """Check if stock image search is current (idempotency)."""
    if not manifest_path.exists():
        return False

    # Check for .stale marker
    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return manifest.get("chapters_hash") == chapters_hash
    except (json.JSONDecodeError, KeyError):
        return False


def _has_locked_selection(ch_data: dict) -> bool:
    """Check if chapter data has any locked selection."""
    for c in ch_data.get("candidates", []):
        if c.get("locked"):
            return True
    return False


def _create_placeholder_entry(chapter, images_dir: Path) -> dict:
    """Create a placeholder entry for title_card/talking_head chapters."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1920, 1080
    bg_color = (
        (247, 147, 26) if chapter.visual.type == "title_card" else (200, 200, 200)
    )

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80
        )
    except OSError:
        font = ImageDraw.getfont()

    text = chapter.title
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((width - text_width) // 2, (height - text_height) // 2)
    draw.text(position, text, fill=(255, 255, 255), font=font)

    filename = f"{chapter.chapter_id}_placeholder.png"
    target_path = images_dir / filename
    images_dir.mkdir(parents=True, exist_ok=True)
    img.save(target_path, "PNG")
    file_size = target_path.stat().st_size

    return {
        "chapter_id": chapter.chapter_id,
        "chapter_title": chapter.title,
        "visual_type": chapter.visual.type,
        "file_path": f"images/{filename}",
        "prompt": None,
        "generation_method": "template",
        "model": None,
        "size": "1920x1080",
        "mime_type": "image/png",
        "size_bytes": file_size,
        "metadata": {
            "template_name": f"{chapter.visual.type}_placeholder",
            "background_color": f"rgb{bg_color}",
            "text_overlay": chapter.title,
        },
    }


def _create_media_asset(
    session: Session,
    episode_id: str,
    chapter_id: str,
    entry: dict,
    asset_type_override: MediaAssetType | None = None,
) -> None:
    """Create MediaAsset database record for a stock image or video."""
    resolved_type = asset_type_override or MediaAssetType.IMAGE
    media_asset = MediaAsset(
        episode_id=episode_id,
        asset_type=resolved_type,
        chapter_id=chapter_id,
        file_path=entry["file_path"],
        mime_type=entry["mime_type"],
        size_bytes=entry["size_bytes"],
        duration_seconds=entry.get("duration_seconds"),
        meta=entry.get("metadata"),
        created_at=_utcnow(),
    )
    session.add(media_asset)


def _mark_render_stale(episode_id: str, outputs_dir: Path) -> None:
    """Mark render draft as stale."""
    render_draft = outputs_dir / episode_id / "render" / "draft.mp4"
    if render_draft.exists():
        stale_data = {
            "invalidated_at": _utcnow().isoformat(),
            "invalidated_by": "stock_images",
            "reason": "images_changed",
        }
        stale_marker = render_draft.with_suffix(".mp4.stale")
        stale_marker.write_text(json.dumps(stale_data, ensure_ascii=False))
        logger.info(f"Marked render draft as stale: {stale_marker}")
