"""Local copy of the freely licensed photos the news chapters show.

During a broadcast the pipeline reads pictures and their licence data from
``data/media-library`` and does not ask Commons. The library is kept current by
``btcedu media-sync`` (nightly timer): it lists every collection entry's
photos again, downloads new ones and drops files that disappeared or whose
licence no longer clears. A person the library has never seen is looked up
live once and added, so a new name still gets a picture the same evening.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DIRNAME = "media-library"
# Photos kept per person beyond the pinned portrait.
MAX_DEPICTED = 20


def library_root(settings) -> Path:
    return Path(settings.outputs_dir).parent / DIRNAME


def _candidate_to_json(candidate) -> dict:
    data = asdict(candidate)
    data["captured_at"] = candidate.captured_at.isoformat() if candidate.captured_at else None
    data["categories"] = list(candidate.categories)
    data["metadata"] = dict(candidate.metadata)
    return data


def _candidate_from_json(data: dict):
    from btcedu.services.commons_service import MediaCandidate

    data = dict(data)
    captured = data.get("captured_at")
    data["captured_at"] = datetime.fromisoformat(captured) if captured else None
    data["categories"] = tuple(data.get("categories") or ())
    return MediaCandidate(**data)


def _entry_from_json(key: str, data: dict):
    from btcedu.core.editorial.media_collection import CollectionEntry

    return CollectionEntry(
        key=key,
        label=data["label"],
        kind=data["kind"],
        commons_file=data["commons_file"],
        aliases=tuple(data.get("aliases") or ()),
        caption_tr=data.get("label", ""),
    )


class MediaLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        try:
            self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._index = {"entries": {}}

    def known(self, key: str) -> bool:
        return key in self._index["entries"]

    def entries(self) -> dict[str, dict]:
        return self._index["entries"]

    def photos(self, key: str) -> list[tuple[object, Path, str]]:
        """(candidate, local file, "pinned"|"depicted"), pinned portrait first."""
        result = []
        for photo in (self._index["entries"].get(key) or {}).get("photos", []):
            path = self.root / photo["file"]
            if path.exists():
                result.append((_candidate_from_json(photo["candidate"]), path, photo["role"]))
        return result

    def sync_entry(self, entry, fetcher, *, pause: float = 0.0, sleep=time.sleep) -> int:
        """List the entry's photos on Commons again and mirror them. Returns new files."""
        from btcedu.core.editorial.media import (
            ALLOWED_IMAGE_TYPES,
            MAX_IMAGE_BYTES,
            assess_candidate,
        )
        from btcedu.core.editorial.media_collection import (
            WIKIDATA_API_URL,
            CollectionCommonsProvider,
            _person_qid,
            depicted_photos,
            requirement_for,
        )

        previous = self._index["entries"].get(entry.key) or {}
        pinned = CollectionCommonsProvider(fetcher, extra=(entry,)).search(entry.label, limit=1)
        depicted: tuple = ()
        qid = previous.get("qid")
        if entry.kind == "person":
            qid = qid or _person_qid(
                fetcher, entry, WIKIDATA_API_URL, "https://commons.wikimedia.org/w/api.php"
            )
            depicted = depicted_photos(fetcher, entry, qid=qid)[:MAX_DEPICTED] if qid else ()
        old = {photo["candidate"]["offer_key"]: photo for photo in previous.get("photos", [])}
        if not depicted and any(p["role"] == "depicted" for p in old.values()):
            # An empty listing is more likely a hiccup than every photo vanishing at once.
            depicted = tuple(
                _candidate_from_json(p["candidate"])
                for p in old.values()
                if p["role"] == "depicted"
            )

        requirement = requirement_for(entry)
        photos: list[dict] = []
        new = 0
        folder = self.root / "photos" / hashlib.sha1(entry.key.encode()).hexdigest()[:12]
        for role, candidate in [("pinned", c) for c in pinned] + [
            ("depicted", c) for c in depicted
        ]:
            if any(p["candidate"]["offer_key"] == candidate.offer_key for p in photos):
                continue
            if not assess_candidate(candidate, requirement).eligible:
                continue
            name = hashlib.sha1(candidate.offer_key.encode()).hexdigest()[:16] + ".jpg"
            path = folder / name
            if not path.exists():
                if pause:
                    sleep(pause)
                body = fetcher.fetch_binary(
                    candidate.file_url,
                    allowed_content_types=ALLOWED_IMAGE_TYPES,
                    max_bytes=MAX_IMAGE_BYTES,
                ).body
                folder.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(body)
                temporary.replace(path)
                new += 1
            photos.append(
                {
                    "role": role,
                    "file": str(path.relative_to(self.root)),
                    "candidate": _candidate_to_json(candidate),
                }
            )
        kept = {p["file"] for p in photos}
        for photo in old.values():
            if photo["file"] not in kept:
                (self.root / photo["file"]).unlink(missing_ok=True)
        self._index["entries"][entry.key] = {
            "label": entry.label,
            "kind": entry.kind,
            "commons_file": entry.commons_file,
            "aliases": list(entry.aliases),
            "qid": qid,
            "photos": photos,
            "synced_at": datetime.now(UTC).isoformat(),
        }
        self._save()
        return new

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)


def sync_library(settings, *, only: str | None = None, pause: float = 3.0) -> dict:
    """Mirror every collection entry, plus people the library learned on the way."""
    from btcedu.core.editorial.media_collection import COLLECTION
    from btcedu.services.document_fetcher import DocumentFetcher, DocumentRateLimited

    library = MediaLibrary(library_root(settings))
    fetcher = DocumentFetcher.from_settings(settings)
    entries = {entry.key: entry for entry in COLLECTION}
    for key, data in library.entries().items():
        entries.setdefault(key, _entry_from_json(key, data))
    if only:
        entries = {key: entry for key, entry in entries.items() if key == only}
    # Longest unsynced first: a run cut short by Commons throttling resumes there next night.
    order = sorted(entries, key=lambda key: library.entries().get(key, {}).get("synced_at", ""))
    entries = {key: entries[key] for key in order}
    summary = {"entries": 0, "new_files": 0, "failed": [], "stopped": None}
    for entry in entries.values():
        try:
            summary["new_files"] += library.sync_entry(entry, fetcher, pause=pause)
            summary["entries"] += 1
        except DocumentRateLimited as exc:
            summary["stopped"] = f"rate limited at {entry.key}: {exc}"
            break
        except Exception as exc:  # noqa: BLE001 - one entry must not stop the rest
            logger.warning("Media library sync of %s failed: %s", entry.key, exc)
            summary["failed"].append(entry.key)
    return summary
