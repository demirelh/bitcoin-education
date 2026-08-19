"""Reuse audio for lines the engine has already spoken.

Every bulletin opens and closes with the same words. Sending them to
ElevenLabs again each evening buys nothing — the same text with the same
voice and the same parameters is the same audio — but it is billed every
time, and each take carries the small chance of coming back with a noise
bed that costs another take to escape.

So the take is kept. The key covers everything that shapes the sound, which
means a changed word, a changed voice or a changed parameter simply misses
and pays for a fresh recording. There is no rule about intros here and no
list of fixed phrases: anything repeated verbatim is reused, and anything
that differs is not.

Only takes that passed the noise check are stored. A hissy one would
otherwise be frozen into every future episode, which is the opposite of
what the retry logic is for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from btcedu.services.elevenlabs_service import TTSRequest, TTSResponse

logger = logging.getLogger(__name__)

# Bumped when the stored payload or the key's meaning changes, so old entries
# fall out of reach instead of being misread.
#
# "2": takes are levelled to a fixed loudness before they are stored. Version
# "1" entries were kept at whatever level the engine happened to produce, and
# because the recurring lines are precisely the cached ones, the greeting and
# the sign-off were frozen up to 8 dB below the rest of the bulletin.
_CACHE_VERSION = "2"

_META_SUFFIX = ".json"
_AUDIO_SUFFIX = ".mp3"


@dataclass
class CacheHit:
    """A stored take, ready to be handed back in place of a paid one."""

    response: TTSResponse
    noise_floor_db: float | None


def cache_key(request: TTSRequest) -> str:
    """Fingerprint everything that decides how the line sounds.

    Anything left out here would let two different recordings collide, so the
    whole request is folded in rather than a chosen subset.
    """
    payload = json.dumps(
        {
            "v": _CACHE_VERSION,
            "text": request.text,
            "voice_id": request.voice_id,
            "model": request.model,
            "stability": request.stability,
            "similarity_boost": request.similarity_boost,
            "style": request.style,
            "use_speaker_boost": request.use_speaker_boost,
            "speed": request.speed,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _paths(cache_dir: Path, key: str) -> tuple[Path, Path]:
    return cache_dir / f"{key}{_AUDIO_SUFFIX}", cache_dir / f"{key}{_META_SUFFIX}"


def _timings_from_meta(raw: object) -> list | None:
    """Rebuild word timings from a sidecar, or ``None`` if it has none.

    Entries written before timings existed simply lack the field; they stay
    usable and their chapter falls back to an even spread.
    """
    from btcedu.services.elevenlabs_service import WordTiming

    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [WordTiming(str(item["w"]), float(item["s"]), float(item["e"])) for item in raw]
    except (KeyError, TypeError, ValueError):
        return None


def lookup(cache_dir: Path, key: str, target: Path) -> CacheHit | None:
    """Copy a stored take to *target* and rebuild its response.

    Returns ``None`` for anything unusable — a missing pair, an unreadable
    sidecar, an empty file. A cache is an optimisation; a damaged entry must
    cost a new synthesis, never an exception.
    """
    from btcedu.services.elevenlabs_service import TTSResponse

    audio_path, meta_path = _paths(cache_dir, key)
    if not audio_path.exists() or not meta_path.exists():
        return None

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if audio_path.stat().st_size == 0:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio_path, target)
    except (OSError, ValueError) as exc:
        logger.warning("TTS cache entry %s unusable (%s); synthesizing instead", key, exc)
        return None

    try:
        response = TTSResponse(
            audio_bytes=audio_path.read_bytes(),
            duration_seconds=float(meta["duration_seconds"]),
            sample_rate=int(meta["sample_rate"]),
            model=str(meta["model"]),
            voice_id=str(meta["voice_id"]),
            character_count=int(meta.get("character_count", 0)),
            # A reused take is not billed. Reporting the original price would
            # make the episode look more expensive than it was and would
            # count against the cost guard for money nobody spent.
            cost_usd=0.0,
            word_timings=_timings_from_meta(meta.get("word_timings")),
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        logger.warning("TTS cache metadata %s unreadable (%s); synthesizing instead", key, exc)
        return None

    _touch(meta_path, audio_path)
    floor = meta.get("noise_floor_db")
    return CacheHit(response=response, noise_floor_db=None if floor is None else float(floor))


def store(
    cache_dir: Path,
    key: str,
    source: Path,
    response: TTSResponse,
    *,
    noise_floor_db: float | None,
    text: str,
) -> None:
    """Keep *source* under *key*. Failures are logged, never raised."""
    audio_path, meta_path = _paths(cache_dir, key)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a crash mid-write cannot
        # leave a half-file that a later run would happily play.
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False, suffix=".part") as tmp:
            tmp_path = Path(tmp.name)
        shutil.copyfile(source, tmp_path)
        os.replace(tmp_path, audio_path)

        meta_path.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "duration_seconds": response.duration_seconds,
                    "sample_rate": response.sample_rate,
                    "model": response.model,
                    "voice_id": response.voice_id,
                    "character_count": response.character_count,
                    "noise_floor_db": noise_floor_db,
                    # Kept with the take: without them a reused line would be
                    # the one part of the broadcast that cannot be subtitled
                    # accurately, and re-recording it just for its timings
                    # would defeat the cache.
                    "word_timings": [
                        {"w": item.word, "s": round(item.start, 3), "e": round(item.end, 3)}
                        for item in (response.word_timings or [])
                    ]
                    or None,
                    # For a human reading the directory. Never parsed back.
                    "text_preview": text[:120],
                    "text_length": len(text),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not cache TTS take %s (%s)", key, exc)


def _touch(*paths: Path) -> None:
    """Mark entries as recently used so pruning drops the idle ones."""
    for path in paths:
        try:
            os.utime(path, None)
        except OSError:
            pass


def _drop_stale_versions(cache_dir: Path) -> int:
    """Remove entries written under an older cache version.

    Bumping the version already puts them out of reach of any lookup, but
    without this they would sit on disk until the size cap happened to evict
    them — which, well under the cap, is never.
    """
    removed = 0
    for meta_path in cache_dir.glob(f"*{_META_SUFFIX}"):
        try:
            version = json.loads(meta_path.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            version = None
        if version == _CACHE_VERSION:
            continue
        for path in (meta_path, meta_path.with_suffix(_AUDIO_SUFFIX)):
            try:
                path.unlink()
            except OSError:
                pass
        removed += 1
    if removed:
        logger.info("Dropped %d TTS cache entries from an older cache version", removed)
    return removed


def prune(cache_dir: Path, max_bytes: int) -> int:
    """Drop the least recently used entries until the cache fits.

    Returns the number of entries removed. ``max_bytes <= 0`` means no limit,
    but entries from a superseded cache version are dropped either way: they
    can never be read again.
    """
    if not cache_dir.is_dir():
        return 0

    removed = _drop_stale_versions(cache_dir)
    if max_bytes <= 0:
        return removed
    entries = []
    total = 0
    for audio_path in cache_dir.glob(f"*{_AUDIO_SUFFIX}"):
        try:
            stat = audio_path.stat()
        except OSError:
            continue
        meta_path = audio_path.with_suffix(_META_SUFFIX)
        size = stat.st_size + (meta_path.stat().st_size if meta_path.exists() else 0)
        entries.append((stat.st_atime, size, audio_path, meta_path))
        total += size

    if total <= max_bytes:
        return removed

    for _atime, size, audio_path, meta_path in sorted(entries):
        if total <= max_bytes:
            break
        for path in (audio_path, meta_path):
            try:
                path.unlink()
            except OSError:
                pass
        total -= size
        removed += 1

    if removed:
        logger.info("Pruned %d TTS cache entries to stay under %d bytes", removed, max_bytes)
    return removed
