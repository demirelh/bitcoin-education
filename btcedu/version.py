"""Git revision helpers so pipeline runs are traceable to a code version."""

import subprocess
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def get_git_commit(short: bool = True) -> str:
    """Return the current git commit SHA of the deployed code.

    Cached for the process lifetime (the code does not change while running).
    Tries ``git rev-parse`` first, then falls back to reading ``.git`` directly
    so it still works when git is unavailable. Returns ``"unknown"`` when the
    revision cannot be determined.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        if sha:
            return sha[:12] if short else sha
    except (subprocess.SubprocessError, OSError):
        pass

    sha = _read_git_head()
    if sha:
        return sha[:12] if short else sha
    return "unknown"


def _read_git_head() -> str | None:
    """Resolve HEAD by reading the .git directory (no git binary needed)."""
    git_dir = _REPO_ROOT / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1].strip()
        ref_path = git_dir / ref
        try:
            return ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            # Fall back to packed-refs
            try:
                for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.startswith("#") or " " not in line:
                        continue
                    sha, name = line.split(" ", 1)
                    if name.strip() == ref:
                        return sha.strip()
            except OSError:
                return None
        return None
    return head or None
