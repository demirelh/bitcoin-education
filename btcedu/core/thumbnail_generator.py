"""Thumbnail generator: creates 3 candidate YouTube thumbnails per episode.

Uses Ideogram v2 (best for readable text overlays in TR); falls back to DALL-E 3.
Saves candidates to data/outputs/{episode_id}/thumbnails/candidate_{1,2,3}.png.

A downstream job / manual selection or CTR A/B test decides which one to upload.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ThumbnailCandidate:
    variant: int
    hook_text: str
    file_path: str
    prompt: str
    provider: str
    cost_usd: float
    metadata: dict = field(default_factory=dict)


@dataclass
class ThumbnailResult:
    episode_id: str
    candidates: list[ThumbnailCandidate]
    total_cost_usd: float
    generated_at: str


# Thumbnail prompt templates per content profile
_TEMPLATE_BITCOIN = (
    "YouTube thumbnail, 16:9, 1920x1080. Bold high-contrast composition. "
    "Large Turkish text overlay: '{hook}'. "
    "Background: dramatic Bitcoin visual — golden BTC coin with glowing edge, "
    "dark blue-to-black gradient, small chart accent. "
    "Style: modern, click-worthy, professional YouTube finance channel. "
    "Turkish audience. Text must be crisp and readable at small size. "
    "Accent color: gold #F7931A."
)

_TEMPLATE_NEWS = (
    "YouTube thumbnail, 16:9, 1920x1080. Serious news-channel aesthetic. "
    "Large Turkish headline text: '{hook}'. "
    "Background: {topic_visual}. "
    "Style: Tagesschau-inspired but Turkish-language, blue accent #004B87, "
    "professional, credible. Text overlay high contrast (white with dark shadow). "
    "No sensationalism, but strong visual hook."
)


def _pick_hooks(episode_title: str, chapters: list, max_hooks: int = 3) -> list[str]:
    """Extract up to N short hook strings from title + first chapter narrations."""
    hooks: list[str] = []

    # Hook 1: shortened title
    title = (episode_title or "").strip()
    if title:
        # Trim to ~40 chars for thumbnail readability
        hooks.append(title[:45].rstrip(" ,.-"))

    # Hook 2 + 3: from chapter titles or narration
    for ch in chapters[:6]:
        if len(hooks) >= max_hooks:
            break
        ch_title = getattr(ch, "title", None)
        if ch_title and 6 <= len(ch_title) <= 55:
            candidate = ch_title.strip()
            if candidate not in hooks:
                hooks.append(candidate)

    # Fill up if fewer than max_hooks
    while len(hooks) < max_hooks and title:
        hooks.append(title[:45].rstrip(" ,.-"))

    return hooks[:max_hooks]


def _build_prompt(hook: str, content_profile: str, topic_hint: str = "") -> str:
    if content_profile in ("tagesschau_tr", "news_tr"):
        return _TEMPLATE_NEWS.format(
            hook=hook,
            topic_visual=topic_hint or "abstract news studio with blue tones, subtle map elements",
        )
    # Default: Bitcoin
    return _TEMPLATE_BITCOIN.format(hook=hook)


def generate_thumbnails(
    session,
    episode_id: str,
    settings,
    n_candidates: int = 3,
    force: bool = False,
) -> ThumbnailResult:
    """Generate N thumbnail candidates for an episode.

    Reads chapters.json to extract hook lines. Calls Ideogram (or DALL-E fallback)
    to produce 3 different thumbnails. Saves to outputs/{episode_id}/thumbnails/.
    """
    from btcedu.models.episode import Episode
    from btcedu.services.image_gen_service import ImageGenRequest
    from btcedu.services.image_provider_factory import get_image_service

    episode = (
        session.query(Episode).filter(Episode.episode_id == episode_id).first()
    )
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    content_profile = (
        getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
    )

    outputs_dir = Path(settings.outputs_dir) / episode_id
    thumb_dir = outputs_dir / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    # Load chapters
    chapters_path = outputs_dir / "chapters.json"
    chapters = []
    episode_title = episode.title or ""
    if chapters_path.exists():
        try:
            data = json.loads(chapters_path.read_text(encoding="utf-8"))
            # Try structured ChapterDocument shape first
            from types import SimpleNamespace

            for c in data.get("chapters", []):
                chapters.append(SimpleNamespace(title=c.get("title", "")))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not parse chapters.json: %s", e)

    hooks = _pick_hooks(episode_title, chapters, max_hooks=n_candidates)

    # Prefer Ideogram (text-in-image), fall back to DALL-E if missing key
    provider = "ideogram" if getattr(settings, "ideogram_api_key", "") else "dalle3"
    service = get_image_service(settings, provider=provider)

    candidates: list[ThumbnailCandidate] = []
    total_cost = 0.0

    for i, hook in enumerate(hooks, start=1):
        target_file = thumb_dir / f"candidate_{i}.png"
        if target_file.exists() and not force:
            logger.info("Thumbnail candidate %d already exists — skipping", i)
            candidates.append(
                ThumbnailCandidate(
                    variant=i,
                    hook_text=hook,
                    file_path=str(target_file.relative_to(outputs_dir)),
                    prompt="(cached)",
                    provider="cached",
                    cost_usd=0.0,
                )
            )
            continue

        prompt = _build_prompt(hook, content_profile)
        req = ImageGenRequest(
            prompt=prompt,
            size="1920x1080",
            quality="hd",
            style_prefix="",
        )
        try:
            resp = service.generate_image(req)
            type(service).download_image(resp.image_url, target_file)
            candidates.append(
                ThumbnailCandidate(
                    variant=i,
                    hook_text=hook,
                    file_path=str(target_file.relative_to(outputs_dir)),
                    prompt=prompt,
                    provider=resp.model,
                    cost_usd=resp.cost_usd,
                    metadata={"revised_prompt": resp.revised_prompt},
                )
            )
            total_cost += resp.cost_usd
            logger.info("Thumbnail candidate %d saved: %s ($%.3f)", i, target_file, resp.cost_usd)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to generate thumbnail candidate %d: %s", i, e)

    # Write manifest
    manifest = {
        "episode_id": episode_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "total_cost_usd": total_cost,
        "candidates": [
            {
                "variant": c.variant,
                "hook": c.hook_text,
                "file": c.file_path,
                "provider": c.provider,
                "cost_usd": c.cost_usd,
            }
            for c in candidates
        ],
    }
    (thumb_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(
        "Generated %d thumbnail candidates for %s (total cost $%.3f)",
        len(candidates), episode_id, total_cost,
    )

    return ThumbnailResult(
        episode_id=episode_id,
        candidates=candidates,
        total_cost_usd=total_cost,
        generated_at=manifest["generated_at"],
    )
