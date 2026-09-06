"""Read-only HeyGen queries for the readiness command.

Deliberately a separate class from ``HeyGenService``: that one exists to buy
videos, this one must be incapable of it. There is no create, no upload and no
poll here — only the two GETs the readiness check needs, each with a timeout and
each translating the provider's answer into something an operator can act on.

The distinctions that matter operationally are kept apart on purpose. A rejected
key is a configuration fault and blocks; a timeout or a DNS failure is the
network having a bad minute and only warns; a 429 carries the provider's own
Retry-After and is repeated back rather than guessed at. Collapsing those three
into "HeyGen check failed" would make the command useless exactly when it is
most needed.
"""

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

HEYGEN_API_BASE = "https://api.heygen.com"
DEFAULT_TIMEOUT_SECONDS = 20

# Why a failure happened, which decides whether it blocks or merely warns.
KIND_AUTH = "auth"
KIND_MISSING = "missing"
KIND_RATE_LIMIT = "rate_limit"
KIND_NETWORK = "network"
KIND_TIMEOUT = "timeout"
KIND_PROTOCOL = "protocol"


class HeyGenReadOnlyError(RuntimeError):
    """A read-only provider check could not be completed."""

    def __init__(self, kind: str, message: str, remedy: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.remedy = remedy
        self.retry_after = retry_after


@dataclass(frozen=True)
class AvatarLook:
    """What the provider says about one look.

    ``supported_api_engines`` is ``None`` when the provider did not report it.
    That is not the same as an empty list, and the difference is the whole
    point: an absent field means unknown, and a readiness check that turned
    unknown into "does not support Avatar III" would block on nothing at all.
    """

    look_id: str
    avatar_id: str = ""
    name: str = ""
    supported_api_engines: frozenset[str] | None = None


class HeyGenReadOnlyClient:
    """GET-only HeyGen access. Cannot create, upload or generate anything."""

    def __init__(self, api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS, session=None):
        if not api_key:
            raise ValueError("HeyGen read-only checks require an API key")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})

    def _get(self, path: str) -> dict:
        url = f"{HEYGEN_API_BASE}{path}"
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.Timeout as exc:
            raise HeyGenReadOnlyError(
                KIND_TIMEOUT,
                f"HeyGen did not answer within {self.timeout}s",
                "Retry later; offline readiness is unaffected by this.",
            ) from exc
        except requests.RequestException as exc:
            # The message may quote the URL but never the key: it lives in a
            # header, and requests does not put headers in its exception text.
            raise HeyGenReadOnlyError(
                KIND_NETWORK,
                f"HeyGen could not be reached: {type(exc).__name__}",
                "Check network access; offline readiness is unaffected by this.",
            ) from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status in (401, 403):
            raise HeyGenReadOnlyError(
                KIND_AUTH,
                f"HeyGen rejected the API key (HTTP {status})",
                "Check HEYGEN_API_KEY. This is a configuration fault, not an outage.",
            )
        if status == 404:
            raise HeyGenReadOnlyError(
                KIND_MISSING,
                "HeyGen does not know this resource (HTTP 404)",
                "Check the configured look ID against the HeyGen dashboard.",
            )
        if status == 429:
            retry_after = _retry_after(response)
            raise HeyGenReadOnlyError(
                KIND_RATE_LIMIT,
                "HeyGen rate limit reached (HTTP 429)"
                + (f", retry after {retry_after:g}s" if retry_after else ""),
                "Wait for the provider's Retry-After and run the check again.",
                retry_after=retry_after,
            )
        if status >= 400:
            raise HeyGenReadOnlyError(
                KIND_PROTOCOL,
                f"HeyGen returned HTTP {status}",
                "Unexpected provider response; nothing was created or billed.",
            )

        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise HeyGenReadOnlyError(
                KIND_PROTOCOL,
                "HeyGen returned a response that is not JSON",
                "Unexpected provider response; nothing was created or billed.",
            ) from exc
        if not isinstance(payload, dict):
            raise HeyGenReadOnlyError(
                KIND_PROTOCOL,
                "HeyGen returned a non-object response",
                "Unexpected provider response; nothing was created or billed.",
            )
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {"items": data}

    def authenticate(self) -> None:
        """Confirm the key is accepted, using the cheapest read there is."""
        self._get("/v2/avatars")

    def look(self, look_id: str) -> AvatarLook:
        """Look up one avatar look. Read-only, and free."""
        if not look_id.strip():
            raise HeyGenReadOnlyError(
                KIND_MISSING, "No look ID given", "Configure a real HeyGen look ID."
            )
        data = self._get(f"/v2/avatars/{look_id.strip()}")
        return _parse_look(look_id.strip(), data)


def _retry_after(response) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_look(look_id: str, data: dict) -> AvatarLook:
    engines = data.get("supported_api_engines")
    parsed_engines: frozenset[str] | None
    if isinstance(engines, list) and all(isinstance(e, str) for e in engines):
        parsed_engines = frozenset(e.strip().lower() for e in engines)
    else:
        # Absent, or in a shape we do not recognise. Reported as unknown rather
        # than invented: guessing here would either block a working look or
        # wave through a broken one.
        parsed_engines = None
    return AvatarLook(
        look_id=look_id,
        avatar_id=str(data.get("avatar_id") or "").strip(),
        name=str(data.get("avatar_name") or data.get("name") or "").strip(),
        supported_api_engines=parsed_engines,
    )
