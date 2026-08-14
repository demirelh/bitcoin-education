"""Credit / balance checker for all external APIs used by the pipeline.

Combines two sources:
1. Live balance queries where APIs support it (ElevenLabs, fal.ai)
2. Cost-tracking from the local DB (pipeline_runs.estimated_cost_usd + media_assets.cost_usd)
   for providers without a public balance endpoint (OpenAI, Anthropic, Ideogram, Gemini).

Also emits a status/severity so the dashboard can highlight low-credit alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

# Thresholds for status colouring
_THRESHOLD_LOW_USD = 5.0  # < $5 remaining → warn
_THRESHOLD_CRITICAL_USD = 2.0  # < $2 remaining → alert
_THRESHOLD_LOW_CHARS = 30_000  # < 30k chars → warn (ElevenLabs)
_THRESHOLD_CRITICAL_CHARS = 10_000


@dataclass
class CreditStatus:
    provider: str
    kind: str  # "live_balance" | "usage_tracking" | "unavailable"
    display_name: str

    # Live-balance shape ($)
    balance_usd: float | None = None

    # Live-balance shape (chars, e.g. ElevenLabs)
    chars_used: int | None = None
    chars_limit: int | None = None
    tier: str | None = None

    # Usage-tracking shape (30-day rolling)
    spent_30d_usd: float | None = None
    spent_7d_usd: float | None = None
    spent_today_usd: float | None = None

    status: str = "unknown"  # "ok" | "warn" | "critical" | "unknown"
    dashboard_url: str = ""
    note: str = ""
    error: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Live balance queries
# ---------------------------------------------------------------------------


def _query_elevenlabs(api_key: str, label: str = "") -> CreditStatus:
    s = CreditStatus(
        provider="elevenlabs",
        kind="live_balance",
        display_name=f"ElevenLabs (TTS){label}",
        dashboard_url="https://elevenlabs.io/app/subscription",
    )
    if not api_key:
        s.status, s.error = "unknown", "API key not set"
        return s
    try:
        r = requests.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        s.chars_used = d.get("character_count")
        s.chars_limit = d.get("character_limit")
        s.tier = d.get("tier")
        remaining = (s.chars_limit or 0) - (s.chars_used or 0)
        if remaining < _THRESHOLD_CRITICAL_CHARS:
            s.status = "critical"
        elif remaining < _THRESHOLD_LOW_CHARS:
            s.status = "warn"
        else:
            s.status = "ok"
    except Exception as e:  # noqa: BLE001
        s.status, s.error = "unknown", str(e)
    return s


def _query_elevenlabs_accounts(settings) -> list[CreditStatus]:
    """One entry per configured account.

    Reporting only the primary would keep the dashboard red while a paid
    reserve sits unused — and, worse, hide the moment the reserve itself runs
    dry, which is the point at which the pipeline actually stops.
    """
    keys = getattr(settings, "elevenlabs_api_keys", None)
    if not isinstance(keys, list) or not keys:
        return [_query_elevenlabs(getattr(settings, "elevenlabs_api_key", ""))]
    if len(keys) == 1:
        return [_query_elevenlabs(keys[0])]

    accounts = []
    for index, key in enumerate(keys):
        label = " – Konto 1 (primär)" if index == 0 else f" – Konto {index + 1} (Reserve)"
        status = _query_elevenlabs(key, label=label)
        status.provider = "elevenlabs" if index == 0 else f"elevenlabs_{index + 1}"
        if index > 0:
            status.note = "Wird erst benutzt, wenn das vorherige Konto aufgebraucht ist."
        accounts.append(status)
    return accounts


def _query_fal(api_key: str) -> CreditStatus:
    s = CreditStatus(
        provider="fal_ai",
        kind="live_balance",
        display_name="fal.ai (Flux images)",
        dashboard_url="https://fal.ai/dashboard/billing",
    )
    if not api_key:
        s.status, s.error = "unknown", "API key not set"
        return s
    try:
        r = requests.get(
            "https://rest.alpha.fal.ai/billing/user_balance",
            headers={"Authorization": f"Key {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        val = r.text.strip()
        s.balance_usd = float(val)
        if s.balance_usd < _THRESHOLD_CRITICAL_USD:
            s.status = "critical"
        elif s.balance_usd < _THRESHOLD_LOW_USD:
            s.status = "warn"
        else:
            s.status = "ok"
    except Exception as e:  # noqa: BLE001
        s.status, s.error = "unknown", str(e)
    return s


# ---------------------------------------------------------------------------
# Usage tracking (from local DB)
# ---------------------------------------------------------------------------


def _sum_costs_from_db(session, provider_match: list[str] | None = None) -> dict:
    """Sum costs from pipeline_runs.estimated_cost_usd bucketed by 1d / 7d / 30d.

    provider_match is a list of substrings — a run is counted if its stage
    matches any of them. If None, all runs are counted.
    """
    from btcedu.models.episode import PipelineRun

    now = datetime.now(UTC)
    since_1d = now - timedelta(days=1)
    since_7d = now - timedelta(days=7)
    since_30d = now - timedelta(days=30)

    def _sum(cutoff):
        q = session.query(PipelineRun).filter(
            PipelineRun.completed_at >= cutoff,
            PipelineRun.estimated_cost_usd.isnot(None),
        )
        rows = q.all()
        if provider_match:
            rows = [r for r in rows if any(m in (r.stage or "").lower() for m in provider_match)]
        return sum((r.estimated_cost_usd or 0.0) for r in rows)

    return {
        "spent_today_usd": round(_sum(since_1d), 4),
        "spent_7d_usd": round(_sum(since_7d), 4),
        "spent_30d_usd": round(_sum(since_30d), 4),
    }


def _query_usage_only(
    session,
    provider: str,
    display_name: str,
    dashboard_url: str,
    stage_match: list[str] | None,
    note: str = "",
) -> CreditStatus:
    s = CreditStatus(
        provider=provider,
        kind="usage_tracking",
        display_name=display_name,
        dashboard_url=dashboard_url,
        note=note,
        status="ok",
    )
    try:
        totals = _sum_costs_from_db(session, stage_match)
        s.spent_today_usd = totals["spent_today_usd"]
        s.spent_7d_usd = totals["spent_7d_usd"]
        s.spent_30d_usd = totals["spent_30d_usd"]
    except Exception as e:  # noqa: BLE001
        s.status, s.error = "unknown", str(e)
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_credits(session, settings) -> list[CreditStatus]:
    """Return credit / usage status for every external API used by the pipeline."""
    results: list[CreditStatus] = []

    # 1. Live balance queries
    results.extend(_query_elevenlabs_accounts(settings))
    results.append(_query_fal(getattr(settings, "fal_api_key", "")))

    # 2. Usage-tracking-only providers
    results.append(
        _query_usage_only(
            session,
            provider="openai",
            display_name="OpenAI (Whisper + DALL-E)",
            dashboard_url="https://platform.openai.com/account/usage",
            stage_match=["transcribe", "chunk"],
            note="No public balance API. Shows tracked pipeline spend only.",
        )
    )
    results.append(
        _query_usage_only(
            session,
            provider="anthropic",
            display_name="Anthropic Claude (correct/translate/adapt/chapterize/generate)",
            dashboard_url="https://console.anthropic.com/settings/billing",
            stage_match=[
                "correct",
                "translate",
                "adapt",
                "chapterize",
                "generate",
                "refine",
                "review",
            ],
            note="No public balance API. Shows tracked pipeline spend only.",
        )
    )
    results.append(
        _query_usage_only(
            session,
            provider="ideogram",
            display_name="Ideogram v2 (thumbnails / text-in-image)",
            dashboard_url="https://ideogram.ai/manage-api",
            stage_match=["thumbnail", "imagegen"],
            note="No public balance API. Shows tracked pipeline spend only.",
        )
    )
    results.append(
        _query_usage_only(
            session,
            provider="gemini",
            display_name="Google Gemini (Tagesschau frame-edit)",
            dashboard_url="https://aistudio.google.com/apikey",
            stage_match=["frame_edit", "frame-edit", "imagegen"],
            note="No public balance API. Shows tracked pipeline spend only.",
        )
    )

    return results


def to_dict(status: CreditStatus) -> dict:
    return {
        "provider": status.provider,
        "display_name": status.display_name,
        "kind": status.kind,
        "status": status.status,
        "balance_usd": status.balance_usd,
        "chars_used": status.chars_used,
        "chars_limit": status.chars_limit,
        "chars_remaining": (
            (status.chars_limit - status.chars_used)
            if (status.chars_limit is not None and status.chars_used is not None)
            else None
        ),
        "tier": status.tier,
        "spent_today_usd": status.spent_today_usd,
        "spent_7d_usd": status.spent_7d_usd,
        "spent_30d_usd": status.spent_30d_usd,
        "dashboard_url": status.dashboard_url,
        "note": status.note,
        "error": status.error,
        "fetched_at": status.fetched_at,
    }
