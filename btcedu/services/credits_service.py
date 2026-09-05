"""Credit / balance checker for all external APIs used by the pipeline.

Combines two sources:
1. Live balance queries where APIs support it (ElevenLabs, fal.ai)
2. Cost-tracking from the local DB (pipeline_runs.estimated_cost_usd + media_assets.cost_usd)
   for providers without a public balance endpoint (OpenAI, Anthropic, Ideogram, Gemini).

Also emits a status/severity so the dashboard can highlight low-credit alerts.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

OPENAI_CREDIT_SNAPSHOT_KEY = "openai_credit_snapshot"

# Thresholds for status colouring
_THRESHOLD_LOW_USD = 5.0  # < $5 remaining → warn
_THRESHOLD_CRITICAL_USD = 2.0  # < $2 remaining → alert
_THRESHOLD_LOW_CHARS = 30_000  # < 30k chars → warn (ElevenLabs)
_THRESHOLD_CRITICAL_CHARS = 10_000
_EPISODE_WARNING_THRESHOLD = 2
_RECENT_EPISODE_SAMPLE_SIZE = 10
_QUOTA_FAILURE_MAX_AGE_DAYS = 7
_OPENAI_TRANSCRIPTION_STAGES = ["transcribe", "transcript_verify"]
_QUOTA_ERROR_RE = re.compile(
    r"credit[_\s-]?balance[_\s-]?exhausted|insufficient[_\s-]?quota|"
    r"no credits remaining|quota[_\s-]?exceeded",
    re.IGNORECASE,
)


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
    spent_total_usd: float | None = None

    average_episode_cost_usd: float | None = None
    estimated_episodes_remaining: int | None = None
    warning_episode_threshold: int | None = None
    credit_snapshot_at: str | None = None
    quota_exhausted: bool = False

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


def _stage_name(run) -> str:
    stage = run.stage
    return str(getattr(stage, "value", stage) or "").lower()


def _matches_stage(run, provider_match: list[str] | None) -> bool:
    return not provider_match or any(match in _stage_name(run) for match in provider_match)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cost_rows(session):
    from btcedu.models.episode import PipelineRun

    return (
        session.query(PipelineRun)
        .filter(
            PipelineRun.completed_at.isnot(None),
            PipelineRun.estimated_cost_usd.isnot(None),
            PipelineRun.estimated_cost_usd > 0,
        )
        .all()
    )


def _sum_costs_from_db(session, provider_match: list[str] | None = None) -> dict:
    """Sum costs from pipeline_runs.estimated_cost_usd bucketed by 1d / 7d / 30d.

    provider_match is a list of substrings — a run is counted if its stage
    matches any of them. If None, all runs are counted.
    """
    now = datetime.now(UTC)
    since_1d = now - timedelta(days=1)
    since_7d = now - timedelta(days=7)
    since_30d = now - timedelta(days=30)
    rows = [row for row in _cost_rows(session) if _matches_stage(row, provider_match)]

    def _sum(cutoff):
        return sum(
            (row.estimated_cost_usd or 0.0)
            for row in rows
            if row.completed_at is not None and _as_utc(row.completed_at) >= cutoff
        )

    return {
        "spent_today_usd": round(_sum(since_1d), 4),
        "spent_7d_usd": round(_sum(since_7d), 4),
        "spent_30d_usd": round(_sum(since_30d), 4),
        "spent_total_usd": round(sum((row.estimated_cost_usd or 0.0) for row in rows), 4),
    }


def _average_episode_cost(session, provider_match: list[str]) -> float | None:
    totals: dict[int, tuple[datetime, float]] = {}
    for row in _cost_rows(session):
        if not _matches_stage(row, provider_match) or row.completed_at is None:
            continue
        row_completed_at = _as_utc(row.completed_at)
        completed_at, total = totals.get(row.episode_id, (row_completed_at, 0.0))
        totals[row.episode_id] = (
            max(completed_at, row_completed_at),
            total + float(row.estimated_cost_usd or 0.0),
        )
    recent = sorted(totals.values(), key=lambda item: item[0], reverse=True)[
        :_RECENT_EPISODE_SAMPLE_SIZE
    ]
    if not recent:
        return None
    return round(sum(total for _, total in recent) / len(recent), 4)


def _latest_quota_failure_at(session, provider_match: list[str]) -> datetime | None:
    from btcedu.models.episode import PipelineRun, RunStatus

    rows = (
        session.query(PipelineRun)
        .filter(
            PipelineRun.status == RunStatus.FAILED,
            PipelineRun.error_message.isnot(None),
            PipelineRun.started_at
            >= datetime.now(UTC) - timedelta(days=_QUOTA_FAILURE_MAX_AGE_DAYS),
        )
        .order_by(PipelineRun.completed_at.desc(), PipelineRun.started_at.desc())
        .all()
    )
    for row in rows:
        if _matches_stage(row, provider_match) and _QUOTA_ERROR_RE.search(row.error_message or ""):
            return row.completed_at or row.started_at
    return None


def _parse_snapshot_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def save_openai_credit_snapshot(session, balance_usd: float) -> dict:
    """Record the current OpenAI balance and the matching local spend baseline."""
    from btcedu.models.app_setting import set_setting

    if not math.isfinite(balance_usd) or balance_usd < 0:
        raise ValueError("balance_usd must be a non-negative finite number")
    totals = _sum_costs_from_db(session, _OPENAI_TRANSCRIPTION_STAGES)
    snapshot = {
        "balance_usd": round(balance_usd, 4),
        "tracked_spend_usd": totals["spent_total_usd"],
        "set_at": datetime.now(UTC).isoformat(),
    }
    set_setting(session, OPENAI_CREDIT_SNAPSHOT_KEY, json.dumps(snapshot))
    return snapshot


def _apply_openai_runway(session, status: CreditStatus) -> None:
    from btcedu.models.app_setting import get_setting

    average_cost = _average_episode_cost(session, _OPENAI_TRANSCRIPTION_STAGES)
    status.average_episode_cost_usd = average_cost
    status.warning_episode_threshold = _EPISODE_WARNING_THRESHOLD

    raw_snapshot = get_setting(session, OPENAI_CREDIT_SNAPSHOT_KEY)
    snapshot = None
    if raw_snapshot:
        try:
            candidate = json.loads(raw_snapshot)
            if isinstance(candidate, dict):
                snapshot = candidate
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid OpenAI credit snapshot")

    snapshot_at = None
    if snapshot is not None:
        try:
            recorded_balance = float(snapshot["balance_usd"])
            baseline_spend = float(snapshot["tracked_spend_usd"])
            snapshot_at = _parse_snapshot_timestamp(snapshot.get("set_at"))
        except (KeyError, TypeError, ValueError):
            snapshot = None
        else:
            current_spend = float(status.spent_total_usd or 0.0)
            remaining_balance = recorded_balance - (current_spend - baseline_spend)
            status.balance_usd = round(max(0.0, remaining_balance), 4)
            status.credit_snapshot_at = snapshot.get("set_at")
            if status.balance_usd <= 0:
                status.estimated_episodes_remaining = 0
                status.status = "critical"
            elif average_cost and average_cost > 0:
                status.estimated_episodes_remaining = max(
                    0, math.floor((status.balance_usd / average_cost) + 1e-9)
                )
                if status.estimated_episodes_remaining <= 1:
                    status.status = "critical"
                elif status.estimated_episodes_remaining <= _EPISODE_WARNING_THRESHOLD:
                    status.status = "warn"

    quota_failure_at = _latest_quota_failure_at(session, _OPENAI_TRANSCRIPTION_STAGES)
    if quota_failure_at is not None:
        quota_failure_at = _as_utc(quota_failure_at)
        if snapshot_at is None or quota_failure_at > snapshot_at:
            status.quota_exhausted = True
            status.balance_usd = 0.0
            status.estimated_episodes_remaining = 0
            status.status = "critical"


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
        s.spent_total_usd = totals["spent_total_usd"]
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
    openai = _query_usage_only(
        session,
        provider="openai",
        display_name="OpenAI (Transkription)",
        dashboard_url="https://platform.openai.com/settings/organization/billing/overview",
        stage_match=_OPENAI_TRANSCRIPTION_STAGES,
        note=(
            "OpenAI bietet keine Guthaben-API. Nach einer Aufladung den aktuellen "
            "Dollarstand unten speichern; danach wird er aus lokalen Episodenkosten geschätzt."
        ),
    )
    _apply_openai_runway(session, openai)
    results.append(openai)
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
        "spent_total_usd": status.spent_total_usd,
        "average_episode_cost_usd": status.average_episode_cost_usd,
        "estimated_episodes_remaining": status.estimated_episodes_remaining,
        "warning_episode_threshold": status.warning_episode_threshold,
        "credit_snapshot_at": status.credit_snapshot_at,
        "quota_exhausted": status.quota_exhausted,
        "dashboard_url": status.dashboard_url,
        "note": status.note,
        "error": status.error,
        "fetched_at": status.fetched_at,
    }
