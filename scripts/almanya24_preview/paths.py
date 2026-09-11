"""Where the preview programs live and where their data lives.

These two are deliberately not the same place. The programs are source and
belong in the repository; everything they produce — the newsroom database, the
generated site, fetched source documents, screenshots and manifests — is
runtime data that must stay out of git. Keeping the split explicit here is what
stops a future edit from quietly reintroducing the coupling that made the
branch unreproducible: a committed systemd unit pointing at a file that only
existed on one machine.

The data root can be redirected with ``ALMANYA24_PREVIEW_DATA_DIR``, which is
what the clean-checkout verification uses so it never touches the real
acceptance data.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that redirects the runtime data root.
DATA_DIR_ENV = "ALMANYA24_PREVIEW_DATA_DIR"

#: Data root relative to the repository, used when the variable is unset.
DEFAULT_DATA_SUBPATH = Path("data") / "almanya24-preview"


def repo_root() -> Path:
    """The repository checkout these programs are part of."""
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """The runtime data root; created on demand so a fresh checkout works."""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    root = Path(override).expanduser().resolve() if override else repo_root() / DEFAULT_DATA_SUBPATH
    root.mkdir(parents=True, exist_ok=True)
    return root
